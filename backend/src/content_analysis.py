"""AUTO_EDIT 内容分析（2026-10-14 第三步）：DSH 模型结构化内容提取。

定位：用户点击「开始自动剪辑」后，读取**已保存的纠错转写**（绝不重新上传/
ASR/纠错），用一次结构化模型调用提取 keywords / golden_quotes / topic_units /
candidate_intervals，结果落盘即停（本阶段不进入时间线工程与渲染）。

硬约束（与需求逐条对应）：
* 模型只读取「纠错文本 + 时间戳 + 段 id」——不读视频、不逐帧、不联网；
* 金句必须**逐字**摘自转写原话：代码侧做空白归一后的逐字校验，模型改写/润色/
  拼接/发明的一律丢弃并留痕（notes）；
* 所有时间戳（start_ms/end_ms）由被引用分段的原始数据重算，**模型无权产生时间戳**；
* topic_units 不依赖任何预设词表（不出现「健康科普」式固定标签）——标题由模型
  从内容归纳；本地回退按停顿边界切分、用位置命名；
* 模型失败/超时/输出不合法/配额不足 → 自动本地回退（plan_source=
  heuristic_fallback），**不等待人工确认、不让任务失败**；
* 优先单次调用；仅当分段数超过 CONTEXT_SEG_LIMIT 才分批并合并（当前阈值下
  479 段素材单次即可完成）。

真实调用位置：本模块 :func:`analyze_content` → :func:`_run_analysis_agent` →
``pydantic_ai.Agent(model=config.llm)``（与 studio_copilot / 纠错模块同一条
受控凭据通道：backend/.env 的 ``LLM`` / ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL``）。
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import inspect
from contextvars import ContextVar
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import llm_budget

# 单次调用的分段上限；超过才分批（合并结果）。479 段实测单次可完成。
CONTEXT_SEG_LIMIT = 600
BATCH_SIZE = 400
# 单次调用超时：优先分析专用环境变量，其次复用共享 LLM_TIMEOUT_SECONDS
LLM_CALL_TIMEOUT_SECONDS = float(
    os.getenv("CONTENT_ANALYSIS_LLM_TIMEOUT_SECONDS")
    or os.getenv("LLM_TIMEOUT_SECONDS") or "240"
)
# 输出 token 预算（2026-10-15 额度优化：从 16384 降到 4096——网关按
# prompt+max_tokens 预留，分析输出 JSON 实测 ≈2-4k tokens；超上限报
# token limit 时提一档到 RETRY 重试一次，仍失败才本地回退）。
MAX_OUTPUT_TOKENS = max(1024, int(os.getenv("CONTENT_ANALYSIS_MAX_TOKENS", "4096")))
MAX_OUTPUT_TOKENS_RETRY = max(MAX_OUTPUT_TOKENS, int(os.getenv("CONTENT_ANALYSIS_MAX_TOKENS_RETRY", "6144")))
# 思考控制：内容分析不是复杂推理任务，默认请求关闭思考（最低推理强度，
# GLM OpenAI 兼容参数 thinking.type=disabled）以大幅降低时延；若网关以 4xx 拒绝
# 该参数（429 配额除外），自动降级为不带该参数重试并记住，不影响自动回退语义。
THINKING_DISABLED = os.getenv("CONTENT_ANALYSIS_THINKING", "disabled").strip().lower() in {
    "disabled", "off", "0", "false"}
_thinking_supported: bool | None = None

_GOLDEN_SYSTEM_PROMPT = (
    "你是中文视频内容分析器。输入为已纠错转写分段（segment_id|start_ms|end_ms|text）；"
    "只见文本，无视频、不联网。只输出严格 JSON，不写任何解释。"
    "字段：keywords（3-10个，原文用词）；golden_quotes（≤12条，逐字复制原文，"
    "给 segment_ids 与 reason，禁止改写润色、禁止发明原文没有的观点）；"
    "topic_units（≤10个不同的完整主题，标题从内容归纳、禁用固定词表标签，给 segment_ids 与 reason）；"
    "candidate_intervals（≤12个，start_ms/end_ms 落在转写范围内且 end>start，给 topic_title 与 reason）。"
    "不改诊断/药名/剂量；跳过隐私片段。")
# 各字段产出上限（约束输出 token，也防止模型刷量）
MAX_KEYWORDS = 10
MAX_GOLDEN_QUOTES = 12
MAX_TOPIC_UNITS = 10
MAX_CANDIDATE_INTERVALS = 12

class _GoldenQuoteOut(BaseModel):
    text: str = Field(min_length=1, max_length=400)
    segment_ids: list[str] = Field(default_factory=list, max_length=50)
    reason: str = Field(default="", max_length=300)


class _TopicUnitOut(BaseModel):
    title: str = Field(min_length=1, max_length=60)
    segment_ids: list[str] = Field(default_factory=list, max_length=5000)
    reason: str = Field(default="", max_length=300)


class _CandidateIntervalOut(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    topic_title: str = Field(default="", max_length=60)
    reason: str = Field(default="", max_length=300)


class _AnalysisOut(BaseModel):
    keywords: list[str] = Field(default_factory=list, max_length=20)
    golden_quotes: list[_GoldenQuoteOut] = Field(default_factory=list, max_length=20)
    topic_units: list[_TopicUnitOut] = Field(default_factory=list, max_length=20)
    candidate_intervals: list[_CandidateIntervalOut] = Field(default_factory=list, max_length=20)


def _model_ready() -> tuple[bool, str, str]:
    """与纠错模块同一判据：模型与密钥齐备才走 llm 路径。"""
    from .ai import _get_missing_llm_key_error
    from .config import get_config

    config = get_config()
    model_name = str(config.llm or "").strip()
    if not model_name:
        return False, "", "LLM 未配置"
    error = _get_missing_llm_key_error(model_name, config)
    if error:
        return False, model_name, error
    return True, model_name, ""


def _analysis_model_settings() -> dict[str, Any]:
    """模型设置：显式 max_tokens + 低温度 + 请求关闭思考（网关拒参时自动降级）。"""
    settings: dict[str, Any] = {"max_tokens": MAX_OUTPUT_TOKENS, "temperature": 0.2}
    if THINKING_DISABLED and _thinking_supported is not False:
        settings["extra_body"] = {"thinking": {"type": "disabled"}}
    return settings


_analysis_call_metadata: ContextVar[dict[str, Any] | None] = ContextVar("analysis_call_metadata", default=None)


def _run_analysis_agent(model_name: str, user_prompt: str) -> _AnalysisOut:
    """单次结构化模型调用。独立成函数便于测试替身替换与调用计数。

    重试链（各自至多一次）：输出上限不足（token limit）→ max_tokens 提档到
    ``MAX_OUTPUT_TOKENS_RETRY``；思考关闭参数被网关 4xx 拒绝（429 配额除外）→
    降级为不带该参数重试并记住，后续调用不再附加；其余异常原样抛出
    （由上层自动回退）。真实 usage 通过每次调用的 ContextVar 返回给外层审计。
    """
    from .ai import _build_transcript_model, apply_settings_to_process_env
    from . import llm_budget
    from .config import get_config
    from pydantic_ai import Agent

    global _thinking_supported
    _analysis_call_metadata.set(None)

    config = get_config()
    apply_settings_to_process_env(config.as_runtime_settings())
    agent: Agent[None, _AnalysisOut] = Agent(
        model=_build_transcript_model(config),
        output_type=_AnalysisOut,
        system_prompt=_GOLDEN_SYSTEM_PROMPT,
        output_retries=2,
    )

    async def _call(settings: dict[str, Any]) -> Any:
        return await asyncio.wait_for(
            agent.run(user_prompt, model_settings=settings),
            timeout=LLM_CALL_TIMEOUT_SECONDS)

    max_tokens = MAX_OUTPUT_TOKENS
    dropped_extra = False
    last_exc: Exception | None = None
    for _ in range(3):
        settings: dict[str, Any] = {"max_tokens": max_tokens, "temperature": 0.2}
        if THINKING_DISABLED and _thinking_supported is not False and not dropped_extra:
            settings["extra_body"] = {"thinking": {"type": "disabled"}}
        try:
            result = asyncio.run(_call(settings))
            _analysis_call_metadata.set({"usage": llm_budget.extract_usage_details(result),
                                         "settings": dict(settings)})
            return result.output
        except Exception as exc:  # noqa: BLE001 — 仅两类可重试异常，其余原样抛出
            last_exc = exc
            status = getattr(exc, "status_code", None)
            if "extra_body" in settings and isinstance(status, int) and 400 <= status < 500 and status != 429:
                _thinking_supported = False
                continue
            if llm_budget.is_token_limit_error(exc) and max_tokens < MAX_OUTPUT_TOKENS_RETRY:
                max_tokens = MAX_OUTPUT_TOKENS_RETRY
                continue
            raise
    raise last_exc  # pragma: no cover — 循环必然 return 或 raise


def _norm(text: str) -> str:
    """逐字比较的归一化：只去空白，不做任何其他改写。"""
    return re.sub(r"\s+", "", str(text or ""))


def _ms(seg: dict[str, Any], key_ms: str, key_s: str) -> int:
    """毫秒优先，缺省从秒换算（时间戳只来自原始分段数据）。"""
    if seg.get(key_ms) is not None:
        return int(round(float(seg[key_ms])))
    return int(round(float(seg.get(key_s, 0.0)) * 1000))


class _SegIndex:
    """分段索引：id ↔ 位置、时间戳、文本；模型引用一律回到这里重算。"""

    def __init__(self, segments: list[dict[str, Any]]):
        self.entries: list[dict[str, Any]] = []
        self.by_id: dict[str, dict[str, Any]] = {}
        for offset, seg in enumerate(segments):
            text = str(seg.get("text", "")).strip()
            if not text:
                continue
            start_ms = _ms(seg, "start_ms", "start")
            end_ms = max(_ms(seg, "end_ms", "end"), start_ms)
            entry = {
                "segment_id": str(seg.get("segment_id") or f"seg_{offset:04d}"),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": text,
                "_norm": _norm(text),
            }
            self.entries.append(entry)
            self.by_id.setdefault(entry["segment_id"], entry)
        self.bounds = (
            min((e["start_ms"] for e in self.entries), default=0),
            max((e["end_ms"] for e in self.entries), default=0),
        )
        self.joined = "".join(e["_norm"] for e in self.entries)

    def resolve(self, segment_ids: list[str]) -> list[dict[str, Any]]:
        """过滤到真实存在的分段，保持转写顺序、去重。"""
        picked = {self.by_id[sid]["start_ms"]: self.by_id[sid]
                  for sid in segment_ids if sid in self.by_id}
        return [picked[key] for key in sorted(picked)]

    def locate(self, text: str) -> list[dict[str, Any]]:
        """按逐字原文反查分段（模型给的 ids 缺失/不可靠时的自愈路径）。"""
        needle = _norm(text)
        if not needle:
            return []
        exact_single = [e for e in self.entries if e["_norm"] == needle]
        if exact_single:
            return exact_single
        # 金句是单个分段内的逐段子串（最常见）：返回该段，时间跨度取整段
        containing = [e for e in self.entries if needle in e["_norm"]]
        if containing:
            return containing
        window: list[dict[str, Any]] = []
        window_norm = ""
        for entry in self.entries:
            window.append(entry)
            window_norm += entry["_norm"]
            while window and window_norm not in needle:
                window_norm = window_norm[len(window[0]["_norm"]):]
                window.pop(0)
            if window and window_norm == needle:
                return list(window)
        partial = [e for e in self.entries if e["_norm"] and e["_norm"] in needle]
        return partial

    def span_of(self, picked: list[dict[str, Any]]) -> tuple[int, int]:
        if not picked:
            return 0, 0
        return picked[0]["start_ms"], picked[-1]["end_ms"]


def _build_user_prompt(index: _SegIndex, slice_entries: list[dict[str, Any]] | None) -> str:
    entries = slice_entries if slice_entries is not None else index.entries
    lines = [f"请分析以下 {len(entries)} 段已纠错转写（全篇共 {len(index.entries)} 段）。只输出结构化 JSON。"]
    if slice_entries is not None:
        head = "".join(e["text"] for e in index.entries[:40])[:1500]
        if head:
            lines.append(f"【全篇开头背景（仅供理解，勿越界引用）】{head}")
    for entry in entries:
        lines.append(f"{entry['segment_id']}|{entry['start_ms']}|{entry['end_ms']}|{entry['text']}")
    return "\n".join(lines)


def _heuristic_topic_units(index: _SegIndex) -> list[dict[str, Any]]:
    """本地回退：按停顿边界（段间空隙 >1.5s）切主题块，位置命名，不用词表。"""
    units: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous_end: int | None = None
    for entry in index.entries:
        if previous_end is not None and entry["start_ms"] - previous_end > 1500 and current:
            units.append(current)
            current = []
        current.append(entry)
        previous_end = entry["end_ms"]
    if current:
        units.append(current)
    result: list[dict[str, Any]] = []
    for number, unit in enumerate(units[:MAX_TOPIC_UNITS], start=1):
        result.append({
            "title": f"主题块 {number}",
            "segment_ids": [e["segment_id"] for e in unit],
            "start_ms": unit[0]["start_ms"],
            "end_ms": unit[-1]["end_ms"],
            "reason": "本地回退：按转写停顿边界切分（>1.5s 空隙），无词表依赖。",
        })
    return result


def _heuristic_fallback(index: _SegIndex, llm_error: str, *, llm_requested: bool,
                        notes: list[str], transcript_version: int | None,
                        job_id: str | None, project_id: str | None) -> dict[str, Any]:
    """模型不可用/失败/配额不足：确定性回退，绝不阻塞任务。"""
    from .studio_insights import _fallback_insights

    entries_as_segments = [
        {"start": e["start_ms"] / 1000.0, "end": e["end_ms"] / 1000.0, "text": e["text"],
         "segment_id": e["segment_id"]}
        for e in index.entries
    ]
    insights = _fallback_insights(entries_as_segments, notice=None)
    if not isinstance(insights, dict):
        insights = insights.model_dump()
    golden_quotes: list[dict[str, Any]] = []
    for sentence in insights.get("golden_sentences", [])[:MAX_GOLDEN_QUOTES]:
        picked = index.locate(str(sentence.get("text", "")))
        if not picked:
            continue
        start_ms, end_ms = index.span_of(picked)
        golden_quotes.append({
            "text": str(sentence.get("text", "")),
            "segment_ids": [e["segment_id"] for e in picked],
            "start_ms": start_ms,
            "end_ms": end_ms,
            "reason": str(sentence.get("reason", "")) or "本地规则候选，请人工确认。",
        })
    topic_units = _heuristic_topic_units(index)
    candidate_intervals = [
        {"start_ms": unit["start_ms"], "end_ms": unit["end_ms"],
         "topic_title": unit["title"], "reason": unit["reason"]}
        for unit in topic_units
    ]
    return _record(
        llm_requested=llm_requested, plan_source="heuristic_fallback", llm_error=llm_error,
        transcript_version=transcript_version,
        keywords=[str(k) for k in insights.get("keywords", [])][:MAX_KEYWORDS],
        golden_quotes=golden_quotes, topic_units=topic_units,
        candidate_intervals=candidate_intervals, notes=notes,
        job_id=job_id, project_id=project_id,
    )


def _record(*, llm_requested: bool, plan_source: str, llm_error: str | None,
            keywords: list[str], golden_quotes: list[dict[str, Any]],
            topic_units: list[dict[str, Any]], candidate_intervals: list[dict[str, Any]],
            notes: list[str], model: str | None = None, llm_calls: int = 0,
            llm_duration_ms: int = 0, transcript_version: int | None = None,
            job_id: str | None = None, project_id: str | None = None,
            llm_reserved_tokens_estimate: int = 0,
            llm_prompt_tokens: int = 0, llm_completion_tokens: int = 0,
            llm_reasoning_tokens: int = 0) -> dict[str, Any]:
    """需求模板字段 + 诚实留痕字段（含 token 预留估算与真实 usage）。"""
    return {
        "llm_requested": llm_requested,
        "llm_enabled": plan_source == "llm",
        "plan_source": plan_source,
        "llm_error": llm_error,
        "transcript_version": transcript_version,
        "keywords": keywords,
        "golden_quotes": golden_quotes,
        "topic_units": topic_units,
        "candidate_intervals": candidate_intervals,
        "model": model,
        "llm_calls": llm_calls,
        "llm_duration_ms": llm_duration_ms,
        "llm_reserved_tokens_estimate": llm_reserved_tokens_estimate,
        "llm_prompt_tokens": llm_prompt_tokens,
        "llm_completion_tokens": llm_completion_tokens,
        "llm_reasoning_tokens": llm_reasoning_tokens,
        "notes": notes,
        "job_id": job_id,
        "project_id": project_id,
        "created_at_utc": datetime.now(UTC).isoformat(),
    }


def _merge_llm_result(result: _AnalysisOut, index: _SegIndex, notes: list[str],
                      quotes: list, topics: list, intervals: list,
                      keywords: list) -> None:
    """模型输出 → 逐字校验 + 分段重对齐 + 时间戳重算（模型无权产生时间戳）。"""
    keywords.extend(result.keywords)
    for quote in result.golden_quotes:
        picked = index.resolve(quote.segment_ids) or index.locate(quote.text)
        if not picked:
            notes.append(f"金句被丢弃（引用分段不存在且原文不可定位）：{quote.text[:40]}")
            continue
        if _norm(quote.text) not in index.joined:
            notes.append(f"金句被丢弃（非逐字原话）：{quote.text[:40]}")
            continue
        start_ms, end_ms = index.span_of(picked)
        if end_ms <= start_ms:
            continue
        quotes.append({
            "text": str(quote.text).strip(),
            "segment_ids": [e["segment_id"] for e in picked],
            "start_ms": start_ms, "end_ms": end_ms,
            "reason": str(quote.reason or "").strip(),
        })
    seen_titles: set[str] = set()
    for unit in result.topic_units:
        picked = index.resolve(unit.segment_ids)
        if not picked:
            notes.append(f"主题块被丢弃（引用分段不存在）：{unit.title}")
            continue
        title = str(unit.title).strip()[:60]
        key = _norm(title)
        if not key or key in seen_titles:
            continue
        seen_titles.add(key)
        start_ms, end_ms = index.span_of(picked)
        topics.append({
            "title": title,
            "segment_ids": [e["segment_id"] for e in picked],
            "start_ms": start_ms, "end_ms": end_ms,
            "reason": str(unit.reason or "").strip(),
        })
    for interval in result.candidate_intervals:
        lo, hi = index.bounds
        start_ms = max(int(interval.start_ms), lo)
        end_ms = min(int(interval.end_ms), hi)
        if end_ms <= start_ms:
            notes.append(f"候选区间被丢弃（越界或倒挂）：{interval.start_ms}-{interval.end_ms}")
            continue
        intervals.append({
            "start_ms": start_ms, "end_ms": end_ms,
            "topic_title": str(interval.topic_title or "").strip()[:60],
            "reason": str(interval.reason or "").strip(),
        })


def _analyze_content(corrected_segments: list[dict[str, Any]], *,
                    transcript_version: int | None = None,
                    job_id: str | None = None, project_id: str | None = None,
                    progress=None) -> dict[str, Any]:
    """内容分析主入口：优先一次结构化模型调用，失败自动本地回退。

    返回记录即需求模板：``llm_requested / plan_source / llm_error /
    transcript_version / keywords / golden_quotes / topic_units /
    candidate_intervals``（另附 llm_enabled / model / llm_calls /
    llm_duration_ms / notes 留痕）。
    """
    index = _SegIndex(corrected_segments)
    notes: list[str] = []
    if not index.entries:
        return _record(llm_requested=False, plan_source="heuristic_fallback",
                       llm_error="纠错转写为空", transcript_version=transcript_version,
                       keywords=[], golden_quotes=[], topic_units=[],
                       candidate_intervals=[], notes=["输入分段为空"],
                       job_id=job_id, project_id=project_id)

    ready, model_name, reason = _model_ready()
    if not ready:
        return _heuristic_fallback(index, reason, llm_requested=False, notes=notes,
                                   transcript_version=transcript_version,
                                   job_id=job_id, project_id=project_id)

    started = time.monotonic()
    calls = 0
    quotes: list[dict[str, Any]] = []
    topics: list[dict[str, Any]] = []
    intervals: list[dict[str, Any]] = []
    keywords: list[str] = []
    est_reserved_total = 0
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    usage_records: list[dict[str, Any]] = []
    call_settings: list[dict[str, Any]] = []
    batch_slices = [None] if len(index.entries) <= CONTEXT_SEG_LIMIT else [
        index.entries[start:start + BATCH_SIZE]
        for start in range(0, len(index.entries), BATCH_SIZE)
    ]
    llm_error: str | None = None
    try:
        for number, slice_entries in enumerate(batch_slices, start=1):
            if progress and len(batch_slices) > 1:
                progress(f"内容分析（批次 {number}/{len(batch_slices)}）")
            calls += 1
            prompt_text = _build_user_prompt(index, slice_entries)
            prompt_est = llm_budget.estimate_reserve(prompt_text, MAX_OUTPUT_TOKENS_RETRY)
            est_reserved_total += prompt_est
            token = _analysis_call_metadata.set(None)
            try:
                result = _run_analysis_agent(model_name, prompt_text)
                metadata = _analysis_call_metadata.get() or {}
                usage = dict(metadata.get("usage") or llm_budget.extract_usage_details(None))
                settings_used = dict(metadata.get("settings") or {})
            finally:
                _analysis_call_metadata.reset(token)
            usage_records.append(usage)
            call_settings.append(settings_used)
            for key in usage_totals:
                usage_totals[key] += int(usage.get(key) or 0)
            # 只记成功调用（失败/429 未被网关接受，不消耗预留）
            llm_budget.record_call("content_analysis", job_id=job_id,
                                   est_reserved=prompt_est, usage=usage, model=model_name)
            _merge_llm_result(result, index, notes, quotes, topics, intervals, keywords)
    except Exception as exc:  # noqa: BLE001 — 模型失败自动回退，任务继续（需求 12）
        llm_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        if len(usage_records) < calls:
            usage_records.append(llm_budget.extract_usage_details(None))

    duration_ms = int((time.monotonic() - started) * 1000)
    if llm_error is not None:
        fallback = _heuristic_fallback(index, llm_error, llm_requested=True, notes=notes,
                                       transcript_version=transcript_version,
                                       job_id=job_id, project_id=project_id)
        fallback.update({"model": model_name, "llm_calls": calls, "llm_duration_ms": duration_ms,
                         "llm_reserved_tokens_estimate": est_reserved_total,
                         "llm_usage": _aggregate_usage(usage_records),
                         "llm_parameters": call_settings,
                         **{f"llm_{k}": v for k, v in usage_totals.items()}})
        return fallback

    deduped_keywords: list[str] = []
    for raw in keywords:
        keyword = re.sub(r"\s+", "", str(raw)).strip("，。；：、")
        if keyword and keyword not in deduped_keywords:
            deduped_keywords.append(keyword[:24])
    quotes.sort(key=lambda q: q["start_ms"])
    topics.sort(key=lambda t: t["start_ms"])
    intervals.sort(key=lambda i: i["start_ms"])
    record = _record(
        llm_requested=True, plan_source="llm", llm_error=None,
        transcript_version=transcript_version,
        keywords=deduped_keywords[:MAX_KEYWORDS],
        golden_quotes=quotes[:MAX_GOLDEN_QUOTES],
        topic_units=topics[:MAX_TOPIC_UNITS],
        candidate_intervals=intervals[:MAX_CANDIDATE_INTERVALS],
        notes=notes, model=model_name, llm_calls=calls, llm_duration_ms=duration_ms,
        job_id=job_id, project_id=project_id,
        llm_reserved_tokens_estimate=est_reserved_total,
        llm_prompt_tokens=usage_totals["prompt_tokens"],
        llm_completion_tokens=usage_totals["completion_tokens"],
        llm_reasoning_tokens=usage_totals["reasoning_tokens"],
    )

    record["llm_usage"] = _aggregate_usage(usage_records)
    record["llm_parameters"] = call_settings
    return record


def analysis_root() -> Path:
    """分析记录目录：``STUDIO_TASKS_ROOT/analysis``。"""
    base = os.getenv("STUDIO_TASKS_ROOT")
    if not base:
        base = str(Path(__file__).resolve().parent.parent / "tasks")
    return Path(base) / "analysis"


def save_analysis_record(job_id: str, record: dict[str, Any], *,
                         provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    """分析记录原子落盘：``tasks/analysis/<job_id>.json``（.part + os.replace）。

    落盘前把 ``analysis_artifact_path`` 写进记录本体——这份字段是 worker
    重启恢复（第四步）与审计追溯的依据之一。
    """
    if provenance is not None:
        record["source_provenance"] = json.loads(json.dumps(provenance))
    record["cache_verification"] = verify_analysis_provenance(record)
    directory = analysis_root()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{job_id}.json"
    record["analysis_artifact_path"] = str(target)
    temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.part")
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"path": str(target), "size_bytes": target.stat().st_size}


def load_analysis_record(job_id: str, *, expected_provenance: dict[str, Any] | None = None,
                         require_verified: bool = False) -> dict[str, Any] | None:
    """读取本任务已持久化的分析结果（worker 重启恢复用）。

    文件缺失 / JSON 损坏 / 不是含 ``plan_source`` 的对象 → ``None``，
    调用方才允许重新分析；**恢复路径绝不因分析结果已存在而再次调用模型**。
    """
    target = analysis_root() / f"{job_id}.json"
    if not target.is_file():
        return None
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or "plan_source" not in record:
        return None
    verification = verify_analysis_provenance(record, expected_provenance)
    if (expected_provenance is not None or require_verified) and not verification["reusable"]:
        return None
    record["cache_verification"] = verification
    return record


ANALYSIS_SCHEMA_VERSION = "analysis/v2"
ANALYSIS_PROMPT_VERSION = "analysis-prompt/2026-10-17"
ANALYSIS_HELPER_VERSION = "analysis-helper/v2"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def build_analysis_provenance(segments: list[dict[str, Any]], *,
                              transcript_version: Any = None, model: str | None = None,
                              parameters: Any = None, rules_snapshot: Any = None,
                              prompt_version: str = ANALYSIS_PROMPT_VERSION,
                              prompt_text: Any = None,
                              schema_version: str = ANALYSIS_SCHEMA_VERSION,
                              helper_version: str = ANALYSIS_HELPER_VERSION) -> dict[str, Any]:
    """来源分量分开存档；缺少任何必需证据时不可作为缓存命中。"""
    missing = [key for key, value in {
        "transcript_version": transcript_version, "model": model,
        "parameters": parameters, "rules_snapshot": rules_snapshot,
        "prompt_version": prompt_version, "prompt_text": prompt_text,
        "schema_version": schema_version, "helper_version": helper_version,
    }.items() if value is None or value == ""]
    result = {"transcript_content_sha256": _digest(segments),
              "transcript_version": transcript_version, "model": model,
              "prompt_version": prompt_version, "prompt_sha256": _digest(prompt_text),
              "parameters": parameters, "parameters_sha256": _digest(parameters),
              "rules_snapshot": rules_snapshot, "rules_sha256": _digest(rules_snapshot),
              "analysis_schema_version": schema_version, "helper_version": helper_version,
              "missing": missing, "complete": not missing}
    result["input_fingerprint"] = _digest(result)
    return result


def verify_analysis_provenance(record: dict[str, Any],
                               expected: dict[str, Any] | None = None) -> dict[str, Any]:
    """读取与命中分开：无调用方上下文永远不宣称 verified。"""
    source = record.get("source_provenance")
    if not isinstance(source, dict):
        return {"status": "legacy_unverified", "reusable": False}
    required = {"transcript_content_sha256", "transcript_version", "model",
                "prompt_version", "prompt_sha256", "parameters", "parameters_sha256",
                "rules_snapshot", "rules_sha256", "analysis_schema_version",
                "helper_version", "missing", "complete", "input_fingerprint"}
    def complete(value: Any) -> bool:
        if not isinstance(value, dict) or not required.issubset(value):
            return False
        try:
            return (value["complete"] is True and value["missing"] == []
                    and all(value[k] is not None and value[k] != "" for k in required - {"missing"})
                    and value["parameters_sha256"] == _digest(value["parameters"])
                    and value["rules_sha256"] == _digest(value["rules_snapshot"])
                    and value["input_fingerprint"] == _digest({k: v for k, v in value.items()
                                                              if k != "input_fingerprint"}))
        except (TypeError, ValueError):
            return False
    if not complete(source):
        return {"status": "incomplete_unverified", "reusable": False}
    if expected is None:
        return {"status": "context_unverified", "reusable": False}
    if not complete(expected):
        return {"status": "expected_incomplete", "reusable": False}
    matched = source == expected
    return {"status": "verified" if matched else "mismatch", "reusable": matched}


def _aggregate_usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {key: (sum(row[key] for row in records) if records and
                    all(row.get(key) is not None for row in records) else None)
              for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens")}
    totals["usage_status"] = ("known" if all(totals[k] is not None for k in
                                            ("prompt_tokens", "completion_tokens")) else
                              "partial" if any(row.get(k) is not None for row in records for k in
                                  ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens"))
                              else "unknown")
    totals["calls"] = records
    return totals


def build_current_analysis_provenance(corrected_segments: list[dict[str, Any]], *,
                                      transcript_version: int | None = None,
                                      model: str | None = None,
                                      rules_snapshot: Any = None) -> dict[str, Any]:
    """模型调用前独立构造缓存identity；实际调用设置仅作审计记录。"""
    if model is None:
        _, model, _ = _model_ready()
    index = _SegIndex(corrected_segments)
    slices = [None] if len(index.entries) <= CONTEXT_SEG_LIMIT else [
        index.entries[start:start + BATCH_SIZE] for start in range(0, len(index.entries), BATCH_SIZE)]
    if rules_snapshot is None:
        # 摘要实际规则实现与依赖代码内容，不把版本名当规则快照。
        fallback_file = Path(__file__).with_name("studio_insights.py")
        rules_snapshot = {"topic_gap_ms": 1500, "limits": [MAX_KEYWORDS, MAX_GOLDEN_QUOTES,
            MAX_TOPIC_UNITS, MAX_CANDIDATE_INTERVALS], "context_limit": CONTEXT_SEG_LIMIT,
            "batch_size": BATCH_SIZE,
            "implementation_sha256": _digest([inspect.getsource(_heuristic_topic_units),
                inspect.getsource(_heuristic_fallback), inspect.getsource(_merge_llm_result),
                inspect.getsource(_SegIndex)]),
            "fallback_source_sha256": hashlib.sha256(fallback_file.read_bytes()).hexdigest()}
    parameters = {"requested": _analysis_model_settings(),
                  "retry_max_tokens": MAX_OUTPUT_TOKENS_RETRY,
                  "timeout_seconds": LLM_CALL_TIMEOUT_SECONDS,
                  "output_retries": 2, "max_setting_attempts": 3}
    return build_analysis_provenance(corrected_segments,
        transcript_version=transcript_version, model=model,
        parameters=parameters, rules_snapshot=rules_snapshot,
        prompt_text={"system": _GOLDEN_SYSTEM_PROMPT,
                     "user": [_build_user_prompt(index, group) for group in slices]})


def analyze_content(corrected_segments: list[dict[str, Any]], *,
                    transcript_version: int | None = None,
                    job_id: str | None = None, project_id: str | None = None,
                    progress=None, rules_snapshot: Any = None) -> dict[str, Any]:
    """兼容原入口，使用调用前即可独立重建的来源；actual_calls只留审计。"""
    source = build_current_analysis_provenance(corrected_segments,
        transcript_version=transcript_version, rules_snapshot=rules_snapshot)
    record = _analyze_content(corrected_segments, transcript_version=transcript_version,
                              job_id=job_id, project_id=project_id, progress=progress)
    record.setdefault("llm_usage", llm_budget.extract_usage_details(None))
    record["source_provenance"] = source
    record["cache_verification"] = verify_analysis_provenance(record)
    return record
