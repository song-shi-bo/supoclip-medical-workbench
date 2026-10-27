"""AUTO_EDIT 完整内容切片规划（2026-10-14 第四步）。

定位：在第三步内容分析（keywords / golden_quotes / topic_units /
candidate_intervals）之后、渲染之前，把纠错转写规划为若干「可独立成立」的
完整切片，产出 ``clip_plan.json`` 即停（本阶段禁止渲染与封面）。

硬约束（与需求逐条对应）：
* 输入只有：指定版本的纠错转写分段 + 第三步分析产物四类；不读视频、不联网、
  绝不重新上传 / ASR / 纠错；
* 不平均切开、不要求覆盖全片；允许舍弃寒暄 / 重复表达 / 停顿 / 跑题 /
  无法独立成立 / 高度重复的内容；
* 切片数量不固定，以「有价值且能独立成立」为准，禁止凑数；
* 每条切片是**一个连续源区间**（start..end 之间全部分段归属该切片，程序侧
  按分段重算 start_ms / end_ms / duration_ms，模型无权产生时间戳、不能拼接
  不连续讲话）；
* 时长是柔性目标：优选 120–180s，90–210s 正常接受，内容完整允许自然偏离；
  禁止为凑时长加内容、为限时截断表达；
* 排序权重：语义完整度 35% / 主题集中度 20% / 开头上下文 15% / 结尾自然度
  15% / 金句与证据 10% / 时长匹配 5%；
* 一切质量与时长问题只记 warning（completeness=warning），**没有 reject、
  不阻断任务、不等人工确认**；
* 模型窗口失败按有界层级拆小重试，剩余失败区间 → 本地回退，保存当前最优计划
  （plan_source 如实标注）。

真实调用位置：本模块 :func:`plan_clips` → :func:`_run_plan_agent` →
``pydantic_ai.Agent(model=config.llm)``（与内容分析/纠错/Copilot 同一条受控
凭据通道）。思考关闭/降级策略复用 content_analysis 的网关探测结果。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import copy
from contextvars import ContextVar
import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import llm_budget
from . import content_analysis as ca
from . import hierarchical_planning as hp
from .content_analysis import (
    LLM_CALL_TIMEOUT_SECONDS,
    _SegIndex,
    _model_ready,
    _norm,
)

# 单次完整规划阈值；超出使用全局摘要指导的局部窗口规划。
PLAN_SINGLE_LIMIT = 1200
# 旧调用方兼容常量；新路径由helper深度/窗口预算控制，不重复相同prompt
PLAN_MAX_RETRIES = 1
# 显式输出上限（2026-10-15 额度优化）：初始 4096，输出超上限（token limit）时
# 提一档到 6144 重试一次，仍失败走本地回退。网关按 prompt+max_tokens 预留，
# 必须显式设置——未设置时按 provider 默认预留，会把请求顶到 429。
PLAN_MAX_OUTPUT_TOKENS = max(1024, int(os.getenv("CONTENT_PLAN_MAX_TOKENS", "4096")))
PLAN_MAX_OUTPUT_TOKENS_RETRY = max(PLAN_MAX_OUTPUT_TOKENS,
                                   int(os.getenv("CONTENT_PLAN_MAX_TOKENS_RETRY", "6144")))
# 时长柔性目标（毫秒）
DUR_PREFER_MS = (120_000, 180_000)
DUR_ACCEPT_MS = (90_000, 210_000)
# 结构性过短下限已废除（2026-10-14 第五轮修正）：时长不决定淘汰。
# <30s 只加 short_duration warning 并降低 duration_fit 得分；主题完整、
# 内容有价值的短切片仍可进入最终计划。只有内容语义原因才记入 discarded。
MIN_CLIP_MS = 30_000


def _max_clips() -> int:
    """切片数量上限：**资源保护上限，不是目标数量**（不要求凑满）。

    默认 12，可用 CONTENT_PLAN_MAX_CLIPS 调整；超过时按综合得分保留最优，
    并在计划级 warnings 记录。正常情况下切片数量由有效主题决定。
    """
    try:
        return max(1, int(os.getenv("CONTENT_PLAN_MAX_CLIPS", "12")))
    except ValueError:
        return 12
# 去重：与已选切片重叠超过较短者 50% 视为高度重复
DUP_OVERLAP_RATIO = 0.5

SCORE_WEIGHTS = {
    "completeness": 0.35,   # 语义完整度
    "topic_focus": 0.20,    # 主题集中度
    "opening": 0.15,        # 开头上下文
    "ending": 0.15,         # 结尾自然度
    "evidence": 0.10,       # 金句和证据
    "duration": 0.05,       # 时长匹配
}

_PLAN_SYSTEM_PROMPT = (
    "你是中文视频切片规划器。输入：已纠错转写分段（segment_id|start_ms|end_ms|text）"
    "与内容分析摘要（关键词/金句/主题块/候选区间）。只见文本，无视频、不联网。"
    "任务：选出若干「有价值且能独立成立」的完整切片，宁缺毋滥，不平均切分、"
    "不要求覆盖全片。规则："
    "1) 每条切片必须是一个连续区间（start_segment_id..end_segment_id 之间全部分段都属于它），"
    "禁止拼接不连续讲话；"
    "2) 一条一个明确主题：开头带必要上下文（不看前文也能懂），中间解释相对完整，"
    "结尾停在完整观点/结论/建议或自然停顿之后；"
    "3) 时长是柔性目标：优选120–180秒，90–210秒可接受，内容完整可自然偏离，"
    "禁止为凑时长加无关内容、禁止为限时截断表达；"
    "4) 舍弃：寒暄、重复表达、停顿、跑题、无法独立成立、与其他切片高度重复的内容，"
    "把舍弃部分及其类型写进 discarded（type：寒暄/重复表达/停顿/跑题/无法独立成立/高度重复）；"
    "5) 金句必须逐字摘自原段；不发明原文没有的观点；不改诊断/药名/剂量。"
    "只输出严格 JSON：{\"clips\":[{topic,start_segment_id,end_segment_id,opening_context,"
    "core_points[],closing_thought,evidence_segment_ids[],cut_in_reason,cut_out_reason}],"
    "\"discarded\":[{segment_ids[],type,reason}]}，不输出时间戳与任何解释散文。")


class _PlanClipOut(BaseModel):
    topic: str = Field(min_length=1, max_length=80)
    start_segment_id: str = Field(min_length=1, max_length=64)
    end_segment_id: str = Field(min_length=1, max_length=64)
    opening_context: str = Field(default="", max_length=400)
    core_points: list[str] = Field(default_factory=list, max_length=12)
    closing_thought: str = Field(default="", max_length=400)
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=50)
    cut_in_reason: str = Field(default="", max_length=300)
    cut_out_reason: str = Field(default="", max_length=300)


class _DiscardedOut(BaseModel):
    segment_ids: list[str] = Field(default_factory=list, max_length=5000)
    type: str = Field(default="跑题", max_length=24)
    reason: str = Field(default="", max_length=300)


class _PlanOut(BaseModel):
    clips: list[_PlanClipOut] = Field(default_factory=list, max_length=30)
    discarded: list[_DiscardedOut] = Field(default_factory=list, max_length=60)


_plan_call_metadata: ContextVar[dict[str, Any] | None] = ContextVar("plan_call_metadata", default=None)


def _plan_model_settings(max_tokens: int) -> dict[str, Any]:
    """规划模型设置：显式 max_tokens + 低温度 + 请求关闭思考（最低推理强度）。

    思考关闭与降级复用 content_analysis 的网关探测结论（分析模块先跑，结论已就绪）。
    """
    from . import content_analysis as ca

    settings: dict[str, Any] = {"max_tokens": max_tokens, "temperature": 0.2}
    if ca.THINKING_DISABLED and ca._thinking_supported is not False:
        settings["extra_body"] = {"thinking": {"type": "disabled"}}
    return settings


def _run_plan_agent(model_name: str, user_prompt: str) -> _PlanOut:
    """单次结构化规划调用。独立成函数便于测试替身替换。

    重试链（各自至多一次）：思考参数被网关 4xx 拒绝（429 配额除外）→ 去参重试；
    输出上限不足（token limit）→ max_tokens 提档到 ``PLAN_MAX_OUTPUT_TOKENS_RETRY``；
    其余异常原样抛出（由上层重试一次或本地回退）。真实 usage 通过每次调用 ContextVar 返回给审计。
    """
    from .ai import _build_transcript_model, apply_settings_to_process_env
    from . import llm_budget
    from .config import get_config
    from pydantic_ai import Agent

    _plan_call_metadata.set(None)

    config = get_config()
    apply_settings_to_process_env(config.as_runtime_settings())
    agent: Agent[None, _PlanOut] = Agent(
        model=_build_transcript_model(config),
        output_type=_PlanOut,
        system_prompt=_PLAN_SYSTEM_PROMPT,
        output_retries=2,
    )

    async def _call(settings: dict[str, Any]) -> Any:
        return await asyncio.wait_for(
            agent.run(user_prompt, model_settings=settings),
            timeout=LLM_CALL_TIMEOUT_SECONDS)

    max_tokens = PLAN_MAX_OUTPUT_TOKENS
    last_exc: Exception | None = None
    for _ in range(3):
        settings = _plan_model_settings(max_tokens)
        try:
            result = asyncio.run(_call(settings))
            _plan_call_metadata.set({"usage": llm_budget.extract_usage_details(result),
                                     "settings": copy.deepcopy(settings)})
            return result.output
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            status = getattr(exc, "status_code", None)
            if "extra_body" in settings and isinstance(status, int) and 400 <= status < 500 and status != 429:
                from . import content_analysis as ca

                ca._thinking_supported = False
                continue
            if llm_budget.is_token_limit_error(exc) and max_tokens < PLAN_MAX_OUTPUT_TOKENS_RETRY:
                max_tokens = PLAN_MAX_OUTPUT_TOKENS_RETRY
                continue
            raise
    raise last_exc  # pragma: no cover — 循环必然 return 或 raise


def _build_plan_prompt(index: _SegIndex, analysis: dict[str, Any]) -> str:
    lines = [f"请规划切片。转写共 {len(index.entries)} 段。只输出严格 JSON。"]
    keywords = analysis.get("keywords") or []
    if keywords:
        lines.append(f"【关键词】{'、'.join(str(k) for k in keywords[:10])}")
    quotes = analysis.get("golden_quotes") or []
    if quotes:
        lines.append("【金句（含所属分段）】")
        for quote in quotes[:_max_clips()]:
            ids = ",".join(str(s) for s in (quote.get("segment_ids") or [])[:4])
            lines.append(f"  [{ids}] {str(quote.get('text', ''))[:60]}")
    units = analysis.get("topic_units") or []
    if units:
        lines.append("【主题块】")
        for unit in units[:12]:
            ids = unit.get("segment_ids") or []
            lines.append(f"  {unit.get('title', '')}: {ids[0] if ids else '-'}..{ids[-1] if ids else '-'}"
                         f" ({len(ids)}段)")
    intervals = analysis.get("candidate_intervals") or []
    if intervals:
        lines.append("【候选区间】")
        for interval in intervals[:12]:
            lines.append(f"  {interval.get('start_ms')}-{interval.get('end_ms')}ms {interval.get('topic_title', '')}")
    lines.append("【转写分段】")
    for entry in index.entries:
        lines.append(f"{entry['segment_id']}|{entry['start_ms']}|{entry['end_ms']}|{entry['text']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 评分与规范化（时间戳/连续性/去重/警告全部由程序决定）
# ---------------------------------------------------------------------------

def _clip_id(project_id: str | None, start_ms: int, end_ms: int) -> str:
    digest = hashlib.md5(f"{project_id}|{start_ms}|{end_ms}".encode()).hexdigest()[:10]
    return f"clip-{digest}"


def _punctuation_end(text: str) -> bool:
    return bool(text) and text.rstrip()[-1:] in {"。", "！", "？", "；", "…"}


def _gap_after(index: _SegIndex, position: int) -> int:
    """分段 position 与下一段之间的空隙（毫秒）；结尾段返回一个大值。"""
    if position + 1 >= len(index.entries):
        return 10_000
    return index.entries[position + 1]["start_ms"] - index.entries[position]["end_ms"]


def _score_clip(entry: dict[str, Any], index: _SegIndex, start_pos: int, end_pos: int,
                analysis: dict[str, Any]) -> tuple[float, list[str]]:
    """按需求 9 的权重打分，并产出 warning 列表（只记 warning，绝不拒绝）。"""
    picked = index.entries[start_pos:end_pos + 1]
    clip_text = "".join(e["text"] for e in picked)
    first, last = picked[0], picked[-1]
    warnings: list[str] = []

    # 语义完整度 35%：结尾落在整句 + 之后有自然停顿
    end_punct = _punctuation_end(last["text"])
    pause = _gap_after(index, end_pos) >= 1200
    completeness = (0.6 if end_punct else 0.0) + (0.4 if pause else 0.0)

    # 主题集中度 20%：分析关键词在切片文本中的覆盖
    keywords = [str(k) for k in (analysis.get("keywords") or [])[:6]]
    topic_focus = (sum(1 for k in keywords if k and k in clip_text) / len(keywords)) if keywords else 0.5

    # 开头上下文 15%：模型给出开头依据 + 自然边界 + 首句完整
    gap_before = (first["start_ms"] - index.entries[start_pos - 1]["end_ms"]) if start_pos > 0 else 10_000
    opening = (0.5 if str(entry.get("opening_context") or "").strip() else 0.0) \
        + (0.3 if gap_before >= 1000 or start_pos == 0 else 0.0) \
        + (0.2 if _punctuation_end(first["text"]) else 0.0)

    # 结尾自然度 15%
    ending = (0.6 if end_punct else 0.0) + (0.4 if pause else 0.0)

    # 金句和证据 10%
    evidence = 0.0
    quote_ids = {sid for quote in (analysis.get("golden_quotes") or [])
                 for sid in (quote.get("segment_ids") or [])}
    picked_ids = {e["segment_id"] for e in picked}
    if quote_ids & picked_ids:
        evidence += 0.6
    evidence_ids = set(entry.get("evidence_segment_ids") or [])
    if evidence_ids and evidence_ids <= picked_ids:
        evidence += 0.4
    evidence = min(evidence, 1.0)

    # 时长匹配 5%（柔性：优选/可接受/偏离）；<30s 只降分 + short_duration warning
    duration_ms = last["end_ms"] - first["start_ms"]
    if duration_ms < MIN_CLIP_MS:
        duration = 0.2
        warnings.append(f"short_duration: 时长 {duration_ms / 1000:.0f}s 低于 30s"
                        "（时长不淘汰切片；主题完整且有价值仍可入选）")
    elif DUR_PREFER_MS[0] <= duration_ms <= DUR_PREFER_MS[1]:
        duration = 1.0
    elif DUR_ACCEPT_MS[0] <= duration_ms <= DUR_ACCEPT_MS[1]:
        duration = 0.7
    else:
        duration = 0.4
        warnings.append(
            f"时长 {duration_ms / 1000:.0f}s 偏离正常区间 90–210s（柔性目标 120–180s，仅记录不拦截）")

    score = sum(SCORE_WEIGHTS[key] * value for key, value in {
        "completeness": completeness, "topic_focus": topic_focus, "opening": opening,
        "ending": ending, "evidence": evidence, "duration": duration}.items())
    if not end_punct:
        warnings.append("结尾可能截断语义（末段无句末标点）")
    if duration_ms < DUR_ACCEPT_MS[0]:
        warnings.append(f"时长 {duration_ms / 1000:.0f}s 低于 90s，可能不足以独立成立（仅记录）")
    return score, warnings


def _normalize_clips(raw_clips: list[_PlanClipOut], index: _SegIndex, analysis: dict[str, Any],
                     notes: list[str]) -> list[dict[str, Any]]:
    """模型输出 → 结构性校验 + 连续区间展开 + 时间戳重算 + 评分排序 + 高度重复去重。"""
    candidates: list[dict[str, Any]] = []
    position_of = {e["segment_id"]: pos for pos, e in enumerate(index.entries)}
    for raw in raw_clips:
        start_pos = position_of.get(str(raw.start_segment_id))
        end_pos = position_of.get(str(raw.end_segment_id))
        if start_pos is None or end_pos is None:
            notes.append(f"切片被舍弃（引用分段不存在）：{raw.topic}")
            continue
        if start_pos > end_pos:
            notes.append(f"切片被舍弃（起止倒挂）：{raw.topic}")
            continue
        picked = index.entries[start_pos:end_pos + 1]  # 连续区间：中间分段全部归属
        start_ms, end_ms = picked[0]["start_ms"], picked[-1]["end_ms"]
        duration_ms = end_ms - start_ms
        # 时长不再决定淘汰（2026-10-14 第五轮修正）：过短只在 _score_clip 里
        # 记 short_duration warning 并降 duration_fit 得分；只有模型按内容语义
        # 判断无法独立成立时才进 discarded。
        score, warnings = _score_clip(raw.model_dump(), index, start_pos, end_pos, analysis)
        candidates.append({
            "topic": str(raw.topic).strip()[:80],
            "start_segment_id": picked[0]["segment_id"],
            "end_segment_id": picked[-1]["segment_id"],
            "start_ms": start_ms,
            "end_ms": end_ms,
            "duration_ms": duration_ms,
            "opening_context": str(raw.opening_context or "").strip(),
            "core_points": [str(p).strip()[:120] for p in (raw.core_points or []) if str(p).strip()],
            "closing_thought": str(raw.closing_thought or "").strip(),
            "evidence_segment_ids": [sid for sid in (raw.evidence_segment_ids or [])
                                     if sid in position_of],
            "cut_in_reason": str(raw.cut_in_reason or "").strip(),
            "cut_out_reason": str(raw.cut_out_reason or "").strip(),
            "score": round(score, 4),
            "_warnings": warnings,
            "_start_pos": start_pos,
            "_end_pos": end_pos,
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    kept: list[dict[str, Any]] = []
    cap_applied = False
    limit = _max_clips()
    for candidate in candidates:
        duplicated = False
        for existing in kept:
            overlap = min(candidate["end_ms"], existing["end_ms"]) - max(candidate["start_ms"], existing["start_ms"])
            if overlap > 0 and overlap / min(candidate["duration_ms"], existing["duration_ms"]) > DUP_OVERLAP_RATIO:
                notes.append(f"切片被舍弃（高度重复，保留得分更高者）：{candidate['topic']}")
                duplicated = True
                break
        if not duplicated:
            if len(kept) >= limit:
                notes.append(f"切片数量达到资源保护上限 {limit}，按综合得分保留最优（非目标数量）：{candidate['topic']}")
                cap_applied = True
            else:
                kept.append(candidate)
    return kept, cap_applied


def _finalize(entry: dict[str, Any]) -> dict[str, Any]:
    """输出需求模板字段；warnings 只增不减，completeness 只有 pass/warning。"""
    warnings = list(entry.pop("_warnings"))
    completeness = "warning" if warnings else "pass"
    entry["completeness"] = completeness
    entry["warnings"] = warnings
    for key in ("_start_pos", "_end_pos", "score"):
        entry.pop(key, None)
    ordered = {
        "clip_id": entry["clip_id"], "topic": entry["topic"],
        "start_segment_id": entry["start_segment_id"], "end_segment_id": entry["end_segment_id"],
        "start_ms": entry["start_ms"], "end_ms": entry["end_ms"], "duration_ms": entry["duration_ms"],
        "opening_context": entry["opening_context"], "core_points": entry["core_points"],
        "closing_thought": entry["closing_thought"], "evidence_segment_ids": entry["evidence_segment_ids"],
        "cut_in_reason": entry["cut_in_reason"], "cut_out_reason": entry["cut_out_reason"],
        "completeness": entry["completeness"], "warnings": entry["warnings"],
    }
    return ordered


# ---------------------------------------------------------------------------
# 本地回退规划器（模型不可用/失败时；绝不阻塞任务）
# ---------------------------------------------------------------------------

_GREETING_RE = re.compile(r"^(大家好|哈喽|hello|hi|各位|大家好呀|嗨)", re.IGNORECASE)
_CTA_RE = re.compile(r"(关注我|点赞|谢谢观看|下期见|喜欢的话)")


def _fallback_plan(index: _SegIndex, analysis: dict[str, Any],
                   notes: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """确定性回退：候选区间/主题块 → 去寒暄尾巴 → 同一评分排序去重。"""
    raw_ranges: list[tuple[int, int, str, str]] = []  # (start_pos, end_pos, topic, source)
    position_of = {e["segment_id"]: pos for pos, e in enumerate(index.entries)}
    ms_positions = [(e["start_ms"], e["end_ms"], pos) for pos, e in enumerate(index.entries)]

    def range_for_ms(start_ms: int, end_ms: int) -> tuple[int, int] | None:
        inside = [pos for s, e, pos in ms_positions if e > start_ms and s < end_ms]
        return (inside[0], inside[-1]) if inside else None

    for interval in (analysis.get("candidate_intervals") or []):
        got = range_for_ms(int(interval.get("start_ms", 0)), int(interval.get("end_ms", 0)))
        if got:
            raw_ranges.append((*got, str(interval.get("topic_title") or "候选切片"), "候选区间"))
    for unit in (analysis.get("topic_units") or []):
        ids = [position_of[sid] for sid in (unit.get("segment_ids") or []) if sid in position_of]
        if ids:
            raw_ranges.append((min(ids), max(ids), str(unit.get("title") or "主题块"), "主题块"))

    kept_ranges: list[tuple[int, int, str, str]] = []
    for start_pos, end_pos, topic, source in raw_ranges:
        overlapped = False
        for existing in kept_ranges:
            if start_pos <= existing[1] and end_pos >= existing[0]:
                overlapped = True
                break
        if not overlapped:
            kept_ranges.append((start_pos, end_pos, topic, source))

    candidates: list[dict[str, Any]] = []
    for start_pos, end_pos, topic, source in kept_ranges:
        # 去寒暄开头 / 引流结尾（保留至少 1 段）
        while start_pos < end_pos and _GREETING_RE.match(index.entries[start_pos]["text"][:6] or ""):
            notes.append(f"舍弃寒暄：{index.entries[start_pos]['segment_id']}")
            start_pos += 1
        while end_pos > start_pos and _CTA_RE.search(index.entries[end_pos]["text"][-20:]):
            notes.append(f"舍弃引流结尾：{index.entries[end_pos]['segment_id']}")
            end_pos -= 1
        picked = index.entries[start_pos:end_pos + 1]
        start_ms, end_ms = picked[0]["start_ms"], picked[-1]["end_ms"]
        duration_ms = end_ms - start_ms
        # 时长不淘汰（同 _normalize_clips）：过短由评分层记 short_duration warning
        entry = {
            "topic": topic[:80], "opening_context": "", "core_points": [],
            "closing_thought": "", "evidence_segment_ids": [],
            "cut_in_reason": f"本地回退：取自{source}的自然边界",
            "cut_out_reason": "本地回退：段落末尾或自然停顿处结束",
        }
        score, warnings = _score_clip(entry, index, start_pos, end_pos, analysis)
        candidates.append({
            "topic": topic, "start_segment_id": picked[0]["segment_id"],
            "end_segment_id": picked[-1]["segment_id"],
            "start_ms": start_ms, "end_ms": end_ms, "duration_ms": duration_ms,
            **entry, "score": round(score, 4), "_warnings": warnings,
            "_start_pos": start_pos, "_end_pos": end_pos,
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    kept: list[dict[str, Any]] = []
    cap_applied = False
    limit = _max_clips()
    for candidate in candidates:
        duplicated = False
        for existing in kept:
            overlap = min(candidate["end_ms"], existing["end_ms"]) - max(candidate["start_ms"], existing["start_ms"])
            if overlap > 0 and overlap / min(candidate["duration_ms"], existing["duration_ms"]) > DUP_OVERLAP_RATIO:
                notes.append(f"切片被舍弃（高度重复，保留得分更高者）：{candidate['topic']}")
                duplicated = True
                break
        if not duplicated:
            if len(kept) >= limit:
                notes.append(f"切片数量达到资源保护上限 {limit}，按综合得分保留最优（非目标数量）：{candidate['topic']}")
                cap_applied = True
            else:
                kept.append(candidate)

    # 未覆盖区间 → 舍弃记录（本地回退无法细分类型，如实标注）
    discarded: list[dict[str, Any]] = []
    covered = {pos for c in kept for pos in range(c["_start_pos"], c["_end_pos"] + 1)}
    uncovered = [pos for pos in range(len(index.entries)) if pos not in covered]
    if uncovered:
        blocks: list[list[int]] = [[uncovered[0]]]
        for pos in uncovered[1:]:
            if pos == blocks[-1][-1] + 1:
                blocks[-1].append(pos)
            else:
                blocks.append([pos])
        for block in blocks:
            head = index.entries[block[0]]["text"][:14]
            kind = "寒暄" if _GREETING_RE.match(head) else "停顿或跑题"
            discarded.append({
                "segment_ids": [index.entries[pos]["segment_id"] for pos in block],
                "type": kind,
                "reason": "本地回退：未入选切片的未覆盖区间（无法细分类型，如实标注）",
            })
    return kept, discarded, cap_applied


def _record(*, plan_source: str, llm_requested: bool, llm_error: str | None,
            clips: list[dict[str, Any]], discarded: list[dict[str, Any]],
            notes: list[str], model: str | None = None, llm_calls: int = 0,
            llm_duration_ms: int = 0, transcript_version: int | None = None,
            job_id: str | None = None, project_id: str | None = None,
            rule_snapshot_id: str | None = None, analysis_ref: dict[str, Any] | None = None,
            plan_warnings: list[str] | None = None,
            llm_reserved_tokens_estimate: int = 0,
            llm_prompt_tokens: int = 0, llm_completion_tokens: int = 0,
            llm_reasoning_tokens: int = 0) -> dict[str, Any]:
    return {
        "plan_source": plan_source,
        "llm_requested": llm_requested,
        "llm_enabled": plan_source == "llm",
        "llm_error": llm_error,
        "transcript_version": transcript_version,
        "rule_snapshot_id": rule_snapshot_id,
        "analysis_ref": analysis_ref or {},
        "warnings": plan_warnings or [],
        "model": model,
        "llm_calls": llm_calls,
        "llm_duration_ms": llm_duration_ms,
        "llm_reserved_tokens_estimate": llm_reserved_tokens_estimate,
        "llm_prompt_tokens": llm_prompt_tokens,
        "llm_completion_tokens": llm_completion_tokens,
        "llm_reasoning_tokens": llm_reasoning_tokens,
        "clips": clips,
        "discarded": discarded,
        "notes": notes,
        "job_id": job_id,
        "project_id": project_id,
        "created_at_utc": datetime.now(UTC).isoformat(),
    }


PLAN_SCHEMA_VERSION = "clip-plan/v2"
PLAN_PROMPT_VERSION = "clip-plan-prompt/2026-10-17"
PLAN_ADAPTER_VERSION = "clip-plan-adapter/v1"


def _analysis_input(analysis: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(analysis.get(key) or []) for key in
            ("keywords", "golden_quotes", "topic_units", "candidate_intervals")}


def _global_summary(analysis: dict[str, Any]) -> str:
    titles = [str(unit.get("title") or "")[:60] for unit in
              (analysis.get("topic_units") or [])[:12] if isinstance(unit, dict)]
    return ("全篇主题概览（仅背景，不得引用本窗口外分段）：" + "；".join(titles))[:900]


def _window_analysis(window: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """全局摘要限长，结构化引用只留下本窗口真实ID与程序边界。"""
    ids = set(window["segment_ids"])
    lo, hi = window["start_ms"], window["end_ms"]
    result = {"keywords": [str(k)[:24] for k in (analysis.get("keywords") or [])[:10]],
              "golden_quotes": [], "topic_units": [], "candidate_intervals": []}
    for quote in (analysis.get("golden_quotes") or [])[:12]:
        cited = quote.get("segment_ids") or []
        if cited and set(cited) <= ids:
            result["golden_quotes"].append(copy.deepcopy(quote))
    for unit in (analysis.get("topic_units") or []):
        cited = [sid for sid in (unit.get("segment_ids") or []) if sid in ids]
        if cited:
            result["topic_units"].append({"title": str(unit.get("title") or "")[:60],
                "segment_ids": cited, "reason": str(unit.get("reason") or "")[:300]})
    result["topic_units"] = result["topic_units"][:12]
    for interval in (analysis.get("candidate_intervals") or []):
        start, end = int(interval.get("start_ms", 0)), int(interval.get("end_ms", 0))
        if start < hi and end > lo:
            result["candidate_intervals"].append({
                "start_ms": max(start, lo), "end_ms": min(end, hi),
                "topic_title": str(interval.get("topic_title") or "")[:60],
                "reason": str(interval.get("reason") or "")[:300]})
    result["candidate_intervals"] = result["candidate_intervals"][:12]
    return result


def _window_prompt(window: dict[str, Any], analysis: dict[str, Any]) -> str:
    return _global_summary(analysis) + "\n" + _build_plan_prompt(
        _SegIndex(window["segments"]), _window_analysis(window, analysis))


def _hierarchy_parameters(count: int) -> dict[str, int]:
    size = max(1, count) if count <= PLAN_SINGLE_LIMIT else 400
    return {"max_segments": size, "overlap_segments": min(40, size - 1),
            "min_segments": min(30, max(1, count // 4)), "max_depth": 3, "max_calls": 256}


def build_current_plan_provenance(corrected_segments: list[dict[str, Any]],
                                   analysis: dict[str, Any], *,
                                   transcript_version: int | None = None,
                                   model: str | None = None,
                                   rule_snapshot_id: str | None = None,
                                   rules_snapshot: Any = None) -> dict[str, Any]:
    """当前输入独立生成计划缓存identity；不读取旧cache或actual_calls。"""
    if model is None:
        _, model, _ = _model_ready()
    index = _SegIndex(corrected_segments)
    hierarchy = _hierarchy_parameters(len(index.entries))
    windows = hp.build_planning_windows(index.entries, analysis,
        max_segments=hierarchy["max_segments"], overlap_segments=hierarchy["overlap_segments"])
    rules = {"rule_snapshot_id": rule_snapshot_id, "external_snapshot": copy.deepcopy(rules_snapshot),
             "score_weights": SCORE_WEIGHTS, "duration_prefer_ms": DUR_PREFER_MS,
             "duration_accept_ms": DUR_ACCEPT_MS, "min_clip_ms": MIN_CLIP_MS,
             "max_clips": _max_clips(), "duplicate_overlap_ratio": DUP_OVERLAP_RATIO,
             "greeting_pattern": _GREETING_RE.pattern, "cta_pattern": _CTA_RE.pattern,
             "program_sha256": ca._digest([inspect.getsource(fn) for fn in
                 (_score_clip, _normalize_clips, _fallback_plan, _clip_id,
                  _window_analysis, _global_summary, _build_plan_prompt)]),
             "helper_source_sha256": hashlib.sha256(Path(hp.__file__).read_bytes()).hexdigest(),
             "output_schema_sha256": ca._digest(_PlanOut.model_json_schema())}
    source = ca.build_analysis_provenance(corrected_segments,
        transcript_version=transcript_version, model=model,
        parameters={"requested": _plan_model_settings(PLAN_MAX_OUTPUT_TOKENS),
                    "retry_max_tokens": PLAN_MAX_OUTPUT_TOKENS_RETRY,
                    "max_setting_attempts": 3, "output_retries": 2,
                    "timeout_seconds": LLM_CALL_TIMEOUT_SECONDS, "hierarchy": hierarchy},
        rules_snapshot=rules, prompt_version=PLAN_PROMPT_VERSION,
        prompt_text={"system": _PLAN_SYSTEM_PROMPT,
                     "root_windows": [_window_prompt(window, analysis) for window in windows]},
        schema_version=PLAN_SCHEMA_VERSION,
        helper_version=f"{hp.HELPER_VERSION}/{PLAN_ADAPTER_VERSION}")
    source["analysis_content_sha256"] = ca._digest(_analysis_input(analysis))
    source["analysis_source_fingerprint"] = (analysis.get("source_provenance") or {}).get("input_fingerprint")
    source["input_fingerprint"] = ca._digest({key: value for key, value in source.items()
                                             if key != "input_fingerprint"})
    # identity与落盘JSON采用同一表示，避免tuple→list令独立expected误判。
    return json.loads(json.dumps(source, ensure_ascii=False))


def plan_clips(corrected_segments: list[dict[str, Any]], analysis: dict[str, Any], *,
               transcript_version: int | None = None,
               job_id: str | None = None, project_id: str | None = None,
               progress=None, rule_snapshot_id: str | None = None,
               analysis_ref: dict[str, Any] | None = None,
               rules_snapshot: Any = None) -> dict[str, Any]:
    """正常输入一次规划；长输入局部窗口，失败仅拆小失败窗口并标明降级。"""
    index = _SegIndex(corrected_segments)
    ready, model_name, reason = _model_ready() if index.entries else (False, "", "纠错转写为空")
    source = build_current_plan_provenance(corrected_segments, analysis,
        transcript_version=transcript_version, model=model_name,
        rule_snapshot_id=rule_snapshot_id, rules_snapshot=rules_snapshot)
    notes: list[str] = []
    started = time.monotonic()
    usage_records, actual_calls = [], []
    reserved_total = 0
    hierarchy = _hierarchy_parameters(len(index.entries))

    def planner(window: dict[str, Any]) -> dict[str, Any]:
        nonlocal reserved_total
        prompt = _window_prompt(window, analysis)
        estimate = llm_budget.estimate_reserve(prompt, PLAN_MAX_OUTPUT_TOKENS_RETRY)
        reserved_total += estimate
        if progress:
            progress(f"模型切片规划（窗口 {window['window_id']}，{len(window['segments'])} 段）")
        token = _plan_call_metadata.set(None)
        usage = llm_budget.extract_usage_details(None)
        metadata: dict[str, Any] = {}
        error = None
        try:
            result = _run_plan_agent(model_name, prompt)
            metadata = _plan_call_metadata.get() or {}
            usage = dict(metadata.get("usage") or usage)
            llm_budget.record_call("clip_planning", job_id=job_id,
                est_reserved=estimate, usage=usage, model=model_name)
            if not result.clips and not result.discarded:
                raise ValueError("模型未返回切片或舍弃说明")
            return {**result.model_dump(), "plan_source": "llm"}
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            raise
        finally:
            _plan_call_metadata.reset(token)
            usage_records.append(usage)
            actual_calls.append({"window_id": window["window_id"], "model": model_name,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "parameters": copy.deepcopy(metadata.get("settings") or {}),
                "usage": usage, "error": error, "reserved_tokens_estimate": estimate})

    raw_candidates, discarded, metadata_by_boundary = [], [], {}
    fallback_cap = False
    if ready:
        result = hp.plan_hierarchically(index.entries, analysis, planner, **hierarchy)
        for rejection in result["rejected_candidates"]:
            notes.append(f"切片被舍弃（引用分段不存在、越界或边界不合法）：{rejection}")
        for duplicate in result["duplicates"]:
            notes.append(f"切片被舍弃（高度重复，合并来源lineage）：{duplicate}")
        for candidate in result["clips"]:
            raw_candidates.append(_PlanClipOut.model_validate(candidate))
            metadata_by_boundary[(candidate["start_segment_id"], candidate["end_segment_id"])] = {
                "plan_source": "llm", "lineage": candidate["lineage"]}
        discarded.extend(result["discarded"])
        for failed in result["failed_windows"]:
            local_entries = [index.by_id[sid] for sid in failed["segment_ids"]]
            window = {**failed, "segments": local_entries}
            kept, local_discarded, local_cap = _fallback_plan(_SegIndex(local_entries),
                                                     _window_analysis(window, analysis), notes)
            fallback_cap = fallback_cap or local_cap
            discarded.extend(local_discarded)
            for candidate in kept:
                raw_candidates.append(_PlanClipOut.model_validate(candidate))
                metadata_by_boundary.setdefault((candidate["start_segment_id"], candidate["end_segment_id"]),
                    {"plan_source": "heuristic_fallback", "lineage": [failed["window_id"]]})
        successful = any(window["status"] == "succeeded" for window in result["windows"])
        plan_source = ("mixed" if successful and result["failed_windows"] else
                       "llm" if successful else "heuristic_fallback")
        errors = [window["reason"] for window in result["failed_windows"]]
        llm_error = "；".join(errors)[:1200] if errors else None
    else:
        kept, discarded, fallback_cap = _fallback_plan(index, analysis, notes) if index.entries else ([], [], False)
        raw_candidates = [_PlanClipOut.model_validate(candidate) for candidate in kept]
        for candidate in kept:
            metadata_by_boundary[(candidate["start_segment_id"], candidate["end_segment_id"])] = {
                "plan_source": "heuristic_fallback", "lineage": []}
        result = {"windows": [], "failed_windows": [], "degradations": ["model_unavailable"],
                  "rejected_candidates": [], "duplicates": [], "callback_calls": 0,
                  "callback_budget": hierarchy["max_calls"], "helper_version": hp.HELPER_VERSION}
        plan_source, llm_error = "heuristic_fallback", reason

    normalized, cap_applied = _normalize_clips(raw_candidates, index, analysis, notes)
    normalized.sort(key=lambda candidate: candidate["_start_pos"])
    clips = []
    for candidate in normalized:
        extra = metadata_by_boundary.get((candidate["start_segment_id"], candidate["end_segment_id"]), {})
        clip = _finalize(dict(candidate, clip_id=_clip_id(project_id, candidate["start_ms"], candidate["end_ms"])))
        clip.update(extra)
        clips.append(clip)
    position_of = {entry["segment_id"]: position for position, entry in enumerate(index.entries)}
    covered_ids = {index.entries[position]["segment_id"] for clip in clips for position in
                   range(position_of[clip["start_segment_id"]], position_of[clip["end_segment_id"]] + 1)}
    merged_discarded, seen_discarded = [], set()
    for item in discarded:
        ids = sorted({sid for sid in item.get("segment_ids", []) if sid in position_of
                      and sid not in covered_ids and sid not in seen_discarded}, key=position_of.__getitem__)
        if ids:
            seen_discarded.update(ids)
            merged_discarded.append({**item, "segment_ids": ids})
    discarded = merged_discarded
    warnings = ["切片数量达到资源保护上限，已按综合得分保留最优"] if cap_applied or fallback_cap else []
    if result["failed_windows"]:
        warnings.append(f"{len(result['failed_windows'])} 个模型窗口最终失败，相关区间使用规则回退；来源={plan_source}")
    if result["degradations"]:
        notes.append("分层规划状态：" + "、".join(result["degradations"]))
    usage = ca._aggregate_usage(usage_records)
    record = _record(plan_source=plan_source, llm_requested=ready, llm_error=llm_error,
        clips=clips, discarded=discarded, notes=notes, model=model_name or None,
        llm_calls=len(actual_calls), llm_duration_ms=int((time.monotonic() - started) * 1000),
        transcript_version=transcript_version, job_id=job_id, project_id=project_id,
        rule_snapshot_id=rule_snapshot_id, analysis_ref=analysis_ref, plan_warnings=warnings,
        llm_reserved_tokens_estimate=reserved_total,
        llm_prompt_tokens=sum(int(row.get("prompt_tokens") or 0) for row in usage_records),
        llm_completion_tokens=sum(int(row.get("completion_tokens") or 0) for row in usage_records),
        llm_reasoning_tokens=sum(int(row.get("reasoning_tokens") or 0) for row in usage_records))
    record.update(llm_usage=usage, actual_calls=actual_calls, source_provenance=source,
                  cache_verification=ca.verify_analysis_provenance({"source_provenance": source}),
                  hierarchical_planning={key: value for key, value in result.items()
                                         if key not in {"clips", "candidates", "discarded"}},
                  failed_windows=result["failed_windows"], degradations=result["degradations"],
                  callback_calls=result["callback_calls"], callback_budget=result["callback_budget"])
    return record


def plans_root() -> Path:
    """计划目录：``STUDIO_TASKS_ROOT/plans/<job_id>/clip_plan.json``。"""
    base = os.getenv("STUDIO_TASKS_ROOT")
    if not base:
        base = str(Path(__file__).resolve().parent.parent / "tasks")
    return Path(base) / "plans"


def save_plan_record(job_id: str, record: dict[str, Any], *,
                     provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    """clip_plan.json 原子落盘（.part + os.replace）。"""
    if provenance is not None:
        record["source_provenance"] = copy.deepcopy(provenance)
    record["cache_verification"] = ca.verify_analysis_provenance(record)
    directory = plans_root() / job_id
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "clip_plan.json"
    record["plan_artifact_path"] = str(target)
    temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.part")
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"path": str(target), "size_bytes": target.stat().st_size}


def load_plan_record(job_id: str, *, expected_provenance: dict[str, Any] | None = None,
                     require_verified: bool = False) -> dict[str, Any] | None:
    """读取本任务已持久化的切片计划（worker 重启恢复用，绝不重新规划）。

    文件缺失 / JSON 损坏 / 不是含 ``clips`` 列表的对象 → ``None``。
    """
    target = plans_root() / job_id / "clip_plan.json"
    if not target.is_file():
        return None
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("clips"), list):
        return None
    verification = ca.verify_analysis_provenance(record, expected_provenance)
    if (expected_provenance is not None or require_verified) and not verification["reusable"]:
        return None
    record["cache_verification"] = verification
    return record
