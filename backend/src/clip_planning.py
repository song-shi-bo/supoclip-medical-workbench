"""AUTO_EDIT 完整内容切片规划（2026-10-14 第四步；2026-11-05 两步制第一步）。

定位：在第三步内容分析（keywords / golden_quotes / topic_units /
candidate_intervals）之后、渲染之前，把纠错转写规划为若干「可独立成立」的
完整切片，产出 ``clip_plan.json`` 即停（本阶段禁止渲染与封面）。

两步制（2026-11-05 用户拍板，见 docs/two-step-planning-design.md）：
* 本模块是**第一步·范围规划**：系统提示词整体替换为用户提示词A
  （docs/prompts/step1-slice-planning.md 原文逐字，一字不动）；
  输出 schema 变为 video_summary / slices / discarded_segments，
  time_range 由程序解析后**吸附到最近分段边界**（带容差校验），
  权威毫秒永远由分段重算——模型时间只是定位线索；
* **全覆盖契约**：切片 + 弃用段拼起来必须覆盖全片时间轴；程序校验
  无重叠无缺口，偏差记 warning 并程序侧补齐（缺口 → discarded）；
* 第二步·切片精修在 ``clip_refinement.py``（提示词B），两份提示词
  绝不能混用（防混断言见 tests/unit/test_clip_refinement.py）。

硬约束（与需求逐条对应）：
* 输入只有：指定版本的纠错转写分段 + 第三步分析产物四类；不读视频、不联网、
  绝不重新上传 / ASR / 纠错；
* 全覆盖契约（提示词A）：不平均切开，但切片+弃用段必须覆盖全片；
  寒暄 / 重复 / 无信息量段落必须明确弃用，不允许静默丢段；
* 时长与数量（提示词A常量化，2026-11-05 第二次修订）：单切片 60~180 秒为宜、
  不产出低于 40 秒的切片；**切片数量不固定**——按视频实际时长与可切取的
  完整内容动态决定（程序侧只保留资源保护上限）；一切时长偏差只记 warning，
  **没有 reject、不阻断任务、不等人工确认**；
* 每条切片是**一个连续源区间**（start..end 之间全部分段归属该切片，程序侧
  按分段重算 start_ms / end_ms / duration_ms，模型无权产生时间戳、不能拼接
  不连续讲话）；
* 排序权重沿用综合评分（语义完整度 35% / 主题集中度 20% / 开头上下文 15% /
  结尾自然度 15% / 金句与证据 10% / 时长匹配 5%），最终按时间轴排序输出；
* 一切质量与时长问题只记 warning（completeness=warning），**没有 reject、
  不阻断任务、不等人工确认**；
* 模型失败自动重试一次（提示词A是全局单次规划，没有可拆小的窗口），
  仍失败 → 本地回退，保存当前最优计划（plan_source 如实标注）。

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
from . import clip_medical_gate as med_gate
from . import clip_purify
from . import content_analysis as ca
from . import hierarchical_planning as hp
from .content_analysis import (
    LLM_CALL_TIMEOUT_SECONDS,
    _SegIndex,
    _model_ready,
    _norm,
)

# 单次完整规划阈值（旧分层窗口机制已随提示词A退役）：提示词A是全局
# 全覆盖规划，必须一次看到整片时间轴，不再按 1200 段拆窗口。
# 超长转写（> SLICE_GLOBAL_MAX_SEGMENTS）放弃 LLM 路径走本地回退，防 token 爆炸。
PLAN_SINGLE_LIMIT = 1200
SLICE_GLOBAL_MAX_SEGMENTS = 4000
# 旧调用方兼容常量；全局单次调用外层只重试一次（无窗口可拆）。
PLAN_MAX_RETRIES = 1
# 显式输出上限（2026-10-15 额度优化）：初始 4096，输出超上限（token limit）时
# 提一档到 6144 重试一次，仍失败走本地回退。网关按 prompt+max_tokens 预留，
# 必须显式设置——未设置时按 provider 默认预留，会把请求顶到 429。
PLAN_MAX_OUTPUT_TOKENS = max(1024, int(os.getenv("CONTENT_PLAN_MAX_TOKENS", "4096")))
PLAN_MAX_OUTPUT_TOKENS_RETRY = max(PLAN_MAX_OUTPUT_TOKENS,
                                   int(os.getenv("CONTENT_PLAN_MAX_TOKENS_RETRY", "6144")))
# 时长柔性目标（毫秒）——2026-11-05 按提示词A常量化：60~180s 为宜、最低 40s。
DUR_PREFER_MS = (60_000, 180_000)
DUR_ACCEPT_MS = (40_000, 210_000)
# 提示词A「不产出低于 40 秒的切片」：程序侧仍是只记 warning 不淘汰（本模块
# 无 reject 契约不变），但 <40s 一定带 short_duration warning 并降低时长得分。
MIN_CLIP_MS = 40_000
# 提示词A数量规则（2026-11-05 第二次修订）：切片数量**不固定**，由视频实际
# 时长与可切取的完整内容决定（原「5~10 个」是示例口径，已从提示词移除）。
# 程序侧只保留资源保护上限（不要求凑满），超限按综合得分保留最优并记 warning。
# 提示词A time_range → 分段边界吸附：模型时间是定位线索，权威毫秒由分段重算。
# 声明边界距最近分段边界超过该容差 → 记 warning（仍吸附，不淘汰）。
SLICE_SNAP_TOLERANCE_MS = 2_500


def _max_clips() -> int:
    """切片数量上限：**资源保护上限，不是目标数量**（不要求凑满）。

    2026-11-05 起切片数量由提示词A按视频时长与内容动态决定，本上限只做
    配额/耗时保护：默认 12，可用 CONTENT_PLAN_MAX_CLIPS 调整；超过时按综合
    得分保留最优，并在计划级 warnings 记录。
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

# =====================================================================
# 第一步·范围规划系统提示词（2026-11-05 两步制）——用户提示词A原文逐字，
# 一字不动（防混契约：与 docs/prompts/step1-slice-planning.md 逐字节一致，
# 断言见 tests/unit/test_clip_refinement.py）。旧单步提示词已退役删除。
# 修订（2026-11-05 用户确认）：原文「约 29 分钟」为示例时长，已泛化为
# 「视频实际时长以输入说明为准」（拼装层在用户提示给出实际时长）。
# 注意：原文含 JSON 花括号，绝不可对齐提示词调用 str.format。
# =====================================================================
_PLAN_SYSTEM_PROMPT = """# 角色
你是医疗科普短视频的切片策划。输入是一段医患对话视频经转写纠错后的文本（带时间戳），视频实际时长以输入说明为准。你的任务是把它规划成若干个可独立发布的短视频切片，供下游逐条精修。

# 规划原则
1. 主题完整：每个切片只讲一个主题，且该主题在切片内讲完（有结论或明确收尾），不依赖其他切片才能看懂；切片之间不重复讲同一内容。
2. 切点自然：起止点选在话题转换处，不从半句话中间切；起点附近必须存在能当钩子的信息。
3. 时长与数量：单切片 60~180 秒为宜，不产出低于 40 秒的切片。切片数量不固定，根据视频实际时长以及其中可以切取的完整内容来定：能切出多少个主题完整、可独立成立的切片就切多少个，宁缺毋滥，不凑数、不硬切。
4. 钩子：为每个切片写 1 句钩子首句 + 1 句封面文字。钩子只能使用切片内实际存在的信息，机制从冲突、反常识、具体疑问、身份共鸣中选；禁止内容兑现不了的承诺，禁止绝对化、恐吓式表达。
5. 信息忠实：主题、钩子、摘要只能来自转写文本，不得引入文本外的医学结论、数据或案例。
6. 弃用与风险标注：寒暄、重复、无信息量段落标记为弃用；含绝对化表述、无出处数字、患者隐私信息的段落标记为风险，供第二步处理。

# 覆盖约束
所有切片与弃用段拼接后必须完整覆盖全片时间轴，无重叠、无缺口；相邻切片不共享同一段素材。

# 输出格式（严格输出以下 JSON，不附加其他文字）
{
  "video_summary": "一段话概括全片科室/病种与主要内容",
  "slice_count": 6,
  "slices": [
    {
      "id": 1,
      "time_range": ["00:03:12", "00:05:40"],
      "theme": "主题，15字内",
      "hook_first_line": "钩子首句",
      "cover_text": "封面文字，10字内",
      "content_summary": "3~5句概括该切片讲了什么、结论是什么",
      "completeness_note": "该切片为何自成一体，1句话",
      "risk_flags": ["00:04:31 出现'百分之百有效'类绝对化表述", "00:05:02 患者提及姓名"]
    }
  ],
  "discarded_segments": [
    {"time_range": ["00:00:00", "00:03:12"], "reason": "开场寒暄，无内容价值"}
  ]
}"""


class _SliceOut(BaseModel):
    """提示词A输出的一条切片（time_range 由程序解析吸附，模型无权定权威时间）。"""
    id: int = Field(default=0, ge=0, le=999)
    time_range: list[str] = Field(min_length=2, max_length=2)
    theme: str = Field(min_length=1, max_length=80)
    hook_first_line: str = Field(default="", max_length=200)
    cover_text: str = Field(default="", max_length=60)
    content_summary: str = Field(default="", max_length=800)
    completeness_note: str = Field(default="", max_length=300)
    risk_flags: list[str] = Field(default_factory=list, max_length=20)


class _SliceDiscardOut(BaseModel):
    """提示词A输出的弃用区间（同上，时间只是定位线索）。"""
    time_range: list[str] = Field(min_length=2, max_length=2)
    reason: str = Field(default="", max_length=300)


class _SlicePlanOut(BaseModel):
    """提示词A的完整输出 schema（与旧 _PlanOut 完全分离，防混用）。"""
    video_summary: str = Field(default="", max_length=900)
    slice_count: int = Field(default=0, ge=0, le=99)
    slices: list[_SliceOut] = Field(default_factory=list, max_length=30)
    discarded_segments: list[_SliceDiscardOut] = Field(default_factory=list, max_length=60)


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


def _run_plan_agent(model_name: str, user_prompt: str) -> _SlicePlanOut:
    """单次结构化规划调用（提示词A schema）。独立成函数便于测试替身替换。

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
    agent: Agent[None, _SlicePlanOut] = Agent(
        model=_build_transcript_model(config),
        output_type=_SlicePlanOut,
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


# （2026-11-05 两步制）旧 `_build_plan_prompt`（ms 时间戳、逐窗口拼装）已随分层
# 窗口机制退役；提示词A的用户提示统一由 `_slice_plan_prompt` 拼装（HH:MM:SS）。


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

    # 时长匹配 5%（柔性：优选/可接受/偏离）；<40s 只降分 + short_duration warning
    duration_ms = last["end_ms"] - first["start_ms"]
    if duration_ms < MIN_CLIP_MS:
        duration = 0.2
        warnings.append(f"short_duration: 时长 {duration_ms / 1000:.0f}s 低于提示词A下限 "
                        f"{MIN_CLIP_MS / 1000:.0f}s"
                        "（时长不淘汰切片；主题完整且有价值仍可入选）")
    elif DUR_PREFER_MS[0] <= duration_ms <= DUR_PREFER_MS[1]:
        duration = 1.0
    elif DUR_ACCEPT_MS[0] <= duration_ms <= DUR_ACCEPT_MS[1]:
        duration = 0.7
    else:
        duration = 0.4
        warnings.append(
            f"时长 {duration_ms / 1000:.0f}s 偏离正常区间 "
            f"{DUR_ACCEPT_MS[0] / 1000:.0f}–{DUR_ACCEPT_MS[1] / 1000:.0f}s"
            f"（提示词A柔性目标 {DUR_PREFER_MS[0] / 1000:.0f}–{DUR_PREFER_MS[1] / 1000:.0f}s，仅记录不拦截）")

    score = sum(SCORE_WEIGHTS[key] * value for key, value in {
        "completeness": completeness, "topic_focus": topic_focus, "opening": opening,
        "ending": ending, "evidence": evidence, "duration": duration}.items())
    if not end_punct:
        warnings.append("结尾可能截断语义（末段无句末标点）")
    if duration_ms < DUR_PREFER_MS[0]:
        warnings.append(f"时长 {duration_ms / 1000:.0f}s 低于提示词A目标下限 "
                        f"{DUR_PREFER_MS[0] / 1000:.0f}s，可能不足以独立成立（仅记录）")
    return score, warnings


def _normalize_clips(raw_clips: list[Any], index: _SegIndex, analysis: dict[str, Any],
                     notes: list[str]) -> list[dict[str, Any]]:
    """模型输出 → 结构性校验 + 连续区间展开 + 时间戳重算 + 评分排序 + 高度重复去重。

    ``raw_clips`` 元素允许 ``_PlanClipOut``（本地回退路径）或同形 dict
    （提示词A装配路径 ``_assemble_slice_plan``），字段消费完全一致。
    """
    candidates: list[dict[str, Any]] = []
    position_of = {e["segment_id"]: pos for pos, e in enumerate(index.entries)}
    for raw in raw_clips:
        entry = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw)
        start_pos = position_of.get(str(entry.get("start_segment_id")))
        end_pos = position_of.get(str(entry.get("end_segment_id")))
        if start_pos is None or end_pos is None:
            notes.append(f"切片被舍弃（引用分段不存在）：{entry.get('topic')}")
            continue
        if start_pos > end_pos:
            notes.append(f"切片被舍弃（起止倒挂）：{entry.get('topic')}")
            continue
        picked = index.entries[start_pos:end_pos + 1]  # 连续区间：中间分段全部归属
        start_ms, end_ms = picked[0]["start_ms"], picked[-1]["end_ms"]
        duration_ms = end_ms - start_ms
        # 时长不再决定淘汰（2026-10-14 第五轮修正）：过短只在 _score_clip 里
        # 记 short_duration warning 并降 duration_fit 得分；只有模型按内容语义
        # 判断无法独立成立时才进 discarded。
        score, warnings = _score_clip(entry, index, start_pos, end_pos, analysis)
        candidates.append({
            "topic": str(entry.get("topic") or "").strip()[:80],
            "start_segment_id": picked[0]["segment_id"],
            "end_segment_id": picked[-1]["segment_id"],
            "start_ms": start_ms,
            "end_ms": end_ms,
            "duration_ms": duration_ms,
            "opening_context": str(entry.get("opening_context") or "").strip(),
            "core_points": [str(p).strip()[:120] for p in (entry.get("core_points") or []) if str(p).strip()],
            "closing_thought": str(entry.get("closing_thought") or "").strip(),
            "evidence_segment_ids": [sid for sid in (entry.get("evidence_segment_ids") or [])
                                     if sid in position_of],
            "cut_in_reason": str(entry.get("cut_in_reason") or "").strip(),
            "cut_out_reason": str(entry.get("cut_out_reason") or "").strip(),
            "score": round(score, 4),
            "_warnings": warnings,
            "_start_pos": start_pos,
            "_end_pos": end_pos,
            # 提示词A策划字段原样透传（_PlanClipOut 回退路径该键为空 dict）
            "_slice_meta": dict(entry.get("_slice_meta") or {}),
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
    """输出需求模板字段；warnings 只增不减，completeness 只有 pass/warning。

    提示词A策划字段（hook_first_line/cover_text/content_summary/
    completeness_note/risk_flags/declared_time_range）原样透传——第二步精修
    （clip_refinement）按 clip_id 消费 theme/hook，右侧面板消费摘要。
    """
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
        # 提示词A策划字段（未给出时给稳定空默认，保持记录形状一致）
        "hook_first_line": str(entry.get("hook_first_line") or ""),
        "cover_text": str(entry.get("cover_text") or ""),
        "content_summary": str(entry.get("content_summary") or ""),
        "completeness_note": str(entry.get("completeness_note") or ""),
        "risk_flags": [str(r) for r in (entry.get("risk_flags") or [])],
        "declared_time_range": [str(v) for v in (entry.get("declared_time_range") or [])],
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


PLAN_SCHEMA_VERSION = "clip-plan/v3"
PLAN_PROMPT_VERSION = "clip-plan-prompt/2026-11-05-slice-a3"
PLAN_ADAPTER_VERSION = "clip-plan-adapter/v2"


def _analysis_input(analysis: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(analysis.get(key) or []) for key in
            ("keywords", "golden_quotes", "topic_units", "candidate_intervals")}


def _global_summary(analysis: dict[str, Any]) -> str:
    titles = [str(unit.get("title") or "")[:60] for unit in
              (analysis.get("topic_units") or [])[:12] if isinstance(unit, dict)]
    return ("全篇主题概览（仅背景，不得引用转写分段之外的内容）：" + "；".join(titles))[:900]


def _clock_of(ms: int) -> str:
    """毫秒 → HH:MM:SS（提示词A输入/输出的时间格式；向下取整到秒）。"""
    total = max(0, int(ms)) // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


_CLOCK_RE = re.compile(r"^(\d{1,3}):([0-5]?\d):([0-5]?\d)(?:[.,](\d{1,3}))?$")


def _parse_clock_ms(value: str) -> int | None:
    """HH:MM:SS（容忍 .mmm / ,mmm 毫秒尾缀）→ 毫秒；非法返回 None（不猜测）。"""
    if not isinstance(value, str):
        return None
    match = _CLOCK_RE.fullmatch(value.strip())
    if not match:
        return None
    h, m, s = int(match.group(1)), int(match.group(2)), int(match.group(3))
    ms_part = int((match.group(4) or "0").ljust(3, "0"))
    return ((h * 3600 + m * 60 + s) * 1000) + ms_part


def _analysis_context_lines(analysis: dict[str, Any], max_clips: int) -> list[str]:
    """分析四产物 → 提示词A用户提示里的背景段（提示词A原文不动，背景走拼装层）。"""
    lines: list[str] = []
    keywords = analysis.get("keywords") or []
    if keywords:
        lines.append(f"【关键词】{'、'.join(str(k) for k in keywords[:10])}")
    quotes = analysis.get("golden_quotes") or []
    if quotes:
        lines.append("【金句（含所属分段）】")
        for quote in quotes[:max_clips]:
            ids = ",".join(str(s) for s in (quote.get("segment_ids") or [])[:4])
            lines.append(f"  [{ids}] {str(quote.get('text', ''))[:60]}")
    units = analysis.get("topic_units") or []
    if units:
        lines.append("【主题块】")
        for unit in units[:12]:
            ids = unit.get("segment_ids") or []
            head = (f"  {unit.get('title', '')}: {ids[0] if ids else '-'}..{ids[-1] if ids else '-'}"
                    f" ({len(ids)}段")
            if unit.get("info_seconds"):
                head += f"，信息约{int(unit['info_seconds'])}秒"
            if unit.get("mechanism"):
                head += f"，机制：{unit['mechanism']}"
            lines.append(head + ")")
            # 簇级钩子句/收尾句/患者提问（阶段1逐字校验通过的引用）——提示词A原文
            # 不动，背景走拼装层；患者提问是问答化开场的钩子候选（2026-11-11 P2）。
            for key, label in (("hook_quote", "钩子句"), ("ending_quote", "收尾句"),
                               ("patient_question", "患者提问")):
                quote = str(unit.get(key) or "").strip()
                if quote:
                    lines.append(f"    {label}：{quote[:60]}")
    # 医学闸门标注（2026-11-10 第三库）：只透出对主题块的程序标注标签与匹配度，
    # 不注入指南条目正文（提示词A「信息忠实」：规划器只能引用转写文本）。
    annotations = analysis.get("medical_annotations") or []
    if annotations:
        lines.extend(med_gate.context_lines({"enabled": True, "unit_annotations": annotations}))
    intervals = analysis.get("candidate_intervals") or []
    if intervals:
        lines.append("【候选区间】")
        for interval in intervals[:12]:
            lines.append(f"  {_clock_of(int(interval.get('start_ms', 0)))}-"
                         f"{_clock_of(int(interval.get('end_ms', 0)))} "
                         f"{interval.get('topic_title', '')}")
    return lines


def _slice_plan_prompt(index: _SegIndex, analysis: dict[str, Any]) -> str:
    """提示词A用户提示（拼装层）：实际时长 + 分析背景 + 全量纠错分段。

    时间戳用 HH:MM:SS（与提示词A输出格式一致，向下取整到秒）；权威毫秒
    由程序按分段重算，模型时间只是定位线索（吸附容差 SLICE_SNAP_TOLERANCE_MS）。
    """
    entries = index.entries
    span_ms = index.bounds[1] - index.bounds[0]
    minutes = max(1, round(span_ms / 60000))
    lines = [f"请规划切片。视频实际时长 {_clock_of(index.bounds[1])}"
             f"（约 {minutes} 分钟），转写共 {len(entries)} 段。只输出严格 JSON。"]
    lines.extend(_analysis_context_lines(analysis, _max_clips()))
    lines.append("【转写分段】")
    for entry in entries:
        lines.append(f"{entry['segment_id']}|{_clock_of(entry['start_ms'])}|"
                     f"{_clock_of(entry['end_ms'])}|{entry['text']}")
    return "\n".join(lines)


def _snap_to_positions(index: _SegIndex, start_ms: int, end_ms: int,
                       warnings: list[str]) -> tuple[int, int] | None:
    """声明的 time_range → 分段位置区间（吸附 + 权威重算的取段语义）。

    吸附规则：取与 [start_ms, end_ms] 有时间交集的转写段集合，再收缩为
    「首段..末段」的连续位置区间；权威 start_ms/end_ms 之后由分段首尾重算。
    声明边界与最终权威边界差 > SLICE_SNAP_TOLERANCE_MS → 记 warning（不淘汰）。
    区间没有盖到任何段 → None（调用方丢弃该条并留痕）。
    """
    hits = [pos for pos, entry in enumerate(index.entries)
            if entry["end_ms"] > start_ms and entry["start_ms"] < end_ms]
    if not hits:
        return None
    start_pos, end_pos = hits[0], hits[-1]
    real_start = index.entries[start_pos]["start_ms"]
    real_end = index.entries[end_pos]["end_ms"]
    if abs(real_start - start_ms) > SLICE_SNAP_TOLERANCE_MS or \
            abs(real_end - end_ms) > SLICE_SNAP_TOLERANCE_MS:
        warnings.append(
            f"time_range 吸附偏差超容差：声明 "
            f"[{_clock_of(start_ms)},{_clock_of(end_ms)}] → 权威 "
            f"[{_clock_of(real_start)},{_clock_of(real_end)}]"
            f"（容差 {SLICE_SNAP_TOLERANCE_MS / 1000:.1f}s，已按分段边界重算）")
    return start_pos, end_pos


def _assemble_slice_plan(slice_plan: _SlicePlanOut, index: _SegIndex, notes: list[str]
                         ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[tuple[str, str], dict]]:
    """提示词A输出 → 候选切片 dict + 弃用记录 + A字段元数据（按分段边界键控）。"""
    warnings: list[str] = []
    candidates: list[dict[str, Any]] = []
    a_meta: dict[tuple[str, str], dict] = {}
    declared: list[tuple[int, int, _SliceOut]] = []
    for raw in slice_plan.slices:
        start_ms = _parse_clock_ms(raw.time_range[0])
        end_ms = _parse_clock_ms(raw.time_range[1])
        if start_ms is None or end_ms is None or end_ms <= start_ms:
            notes.append(f"切片被舍弃（time_range 无法解析或起止倒挂）：{raw.theme}")
            continue
        declared.append((start_ms, end_ms, raw))
    declared.sort(key=lambda item: item[0])
    prev_end_pos = -1
    for start_ms, end_ms, raw in declared:
        snapped = _snap_to_positions(index, start_ms, end_ms, warnings)
        if snapped is None:
            notes.append(f"切片被舍弃（time_range 未覆盖任何转写段）：{raw.theme}")
            continue
        start_pos, end_pos = snapped
        if start_pos <= prev_end_pos:
            # 覆盖契约：相邻切片不共享素材——重叠部分划归先声明（时间靠前）的切片
            start_pos = prev_end_pos + 1
            if start_pos > end_pos:
                notes.append(f"切片被舍弃（与前一条完全重叠，已归前者）：{raw.theme}")
                continue
            warnings.append(f"切片时间重叠已程序侧收缩：{raw.theme} 起点吸附到前一条末段之后")
        picked = index.entries[start_pos:end_pos + 1]
        candidates.append({
            "topic": str(raw.theme).strip()[:80],
            "start_segment_id": picked[0]["segment_id"],
            "end_segment_id": picked[-1]["segment_id"],
            "opening_context": str(raw.completeness_note or "").strip()[:400],
            "core_points": [part.strip()[:120] for part in
                            re.split(r"[。；;]", str(raw.content_summary or "")) if part.strip()][:12],
            "closing_thought": "",
            "evidence_segment_ids": [s["segment_id"] for s in picked[:50]],
            "cut_in_reason": f"提示词A time_range {_clock_of(start_ms)} 起（话题转换处切点）",
            "cut_out_reason": f"提示词A time_range {_clock_of(end_ms)} 止（完整收尾）",
            "_slice_meta": {
                "hook_first_line": str(raw.hook_first_line or ""),
                "cover_text": str(raw.cover_text or ""),
                "content_summary": str(raw.content_summary or ""),
                "completeness_note": str(raw.completeness_note or ""),
                "risk_flags": [str(r) for r in (raw.risk_flags or [])],
                "declared_time_range": [str(v) for v in raw.time_range],
            },
        })
        a_meta[(picked[0]["segment_id"], picked[-1]["segment_id"])] = {
            "plan_source": "llm", "lineage": ["global"]}
        prev_end_pos = end_pos
    discarded: list[dict[str, Any]] = []
    for item in slice_plan.discarded_segments:
        start_ms = _parse_clock_ms(item.time_range[0])
        end_ms = _parse_clock_ms(item.time_range[1])
        if start_ms is None or end_ms is None or end_ms <= start_ms:
            notes.append("弃用段被舍弃（time_range 无法解析）")
            continue
        hits = [pos for pos, entry in enumerate(index.entries)
                if entry["end_ms"] > start_ms and entry["start_ms"] < end_ms]
        if not hits:
            continue
        discarded.append({
            "segment_ids": [index.entries[pos]["segment_id"] for pos in hits],
            "type": "弃用",
            "reason": str(item.reason or "").strip()[:300] or "提示词A标记为弃用",
        })
    for text in warnings:
        notes.append(text)
    return candidates, discarded, a_meta


def _enforce_full_coverage(index: _SegIndex, clips: list[dict[str, Any]],
                           discarded: list[dict[str, Any]], warnings: list[str]) -> list[dict[str, Any]]:
    """全覆盖契约（提示词A）：切片 + 弃用段必须覆盖全片，无重叠无缺口。

    重叠已在 ``_normalize_clips`` 去重阶段消除；这里先剔除与已选切片**重叠**
    的弃用记录（切片优先，模型矛盾舍弃不进最终产物），再把**缺口**程序侧
    补齐成弃用记录（type=未覆盖），并记 coverage_gap_filled warning。
    """
    position_of = {entry["segment_id"]: position for position, entry in enumerate(index.entries)}
    covered: set[int] = set()
    for clip in clips:
        start_pos = position_of.get(clip.get("start_segment_id"))
        end_pos = position_of.get(clip.get("end_segment_id"))
        if start_pos is None or end_pos is None or start_pos > end_pos:
            continue
        covered.update(range(start_pos, end_pos + 1))
    kept_discarded: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in discarded:
        ids = sorted({position_of[sid] for sid in item.get("segment_ids", [])
                      if sid in position_of and position_of[sid] not in covered
                      and position_of[sid] not in seen})
        if not ids:
            continue
        seen.update(ids)
        kept_discarded.append({**item, "segment_ids": [index.entries[pos]["segment_id"]
                                                       for pos in ids]})
    uncovered = [pos for pos in range(len(index.entries))
                 if pos not in covered and pos not in seen]
    if uncovered:
        blocks: list[list[int]] = [[uncovered[0]]]
        for pos in uncovered[1:]:
            if pos == blocks[-1][-1] + 1:
                blocks[-1].append(pos)
            else:
                blocks.append([pos])
        for block in blocks:
            head = index.entries[block[0]]["text"][:14]
            kept_discarded.append({
                "segment_ids": [index.entries[pos]["segment_id"] for pos in block],
                "type": "未覆盖",
                "reason": f"程序补齐（全覆盖契约）：模型计划未覆盖「{head}…」区间，已按弃用记录兜底",
            })
        warnings.append(f"coverage_gap_filled: 模型计划有 {len(uncovered)} 段未覆盖，"
                        "已程序侧补齐进 discarded（全覆盖契约）")
    return kept_discarded


def build_current_plan_provenance(corrected_segments: list[dict[str, Any]],
                                   analysis: dict[str, Any], *,
                                   transcript_version: int | None = None,
                                   model: str | None = None,
                                   rule_snapshot_id: str | None = None,
                                   rules_snapshot: Any = None,
                                   medical_gate_identity: dict[str, Any] | None = None,
                                   purify_identity: dict[str, Any] | None = None
                                   ) -> dict[str, Any]:
    """当前输入独立生成计划缓存identity；不读取旧cache或actual_calls。"""
    if model is None:
        _, model, _ = _model_ready()
    # 医学闸门身份：未显式传入时现场取（同一快照 → 同一指纹，纯函数语义）。
    gate_identity = medical_gate_identity or med_gate.current_identity()
    # 净化重组身份（2026-11-11 问答化 P1）：说话人标注指纹变化 → 旧计划缓存失效。
    purify_id = purify_identity or clip_purify.identity(None)
    index = _SegIndex(corrected_segments)
    rules = {"rule_snapshot_id": rule_snapshot_id, "external_snapshot": copy.deepcopy(rules_snapshot),
             "score_weights": SCORE_WEIGHTS, "duration_prefer_ms": list(DUR_PREFER_MS),
             "duration_accept_ms": list(DUR_ACCEPT_MS), "min_clip_ms": MIN_CLIP_MS,
             "slice_snap_tolerance_ms": SLICE_SNAP_TOLERANCE_MS,
             "slice_global_max_segments": SLICE_GLOBAL_MAX_SEGMENTS,
             "max_clips": _max_clips(), "duplicate_overlap_ratio": DUP_OVERLAP_RATIO,
             "greeting_pattern": _GREETING_RE.pattern, "cta_pattern": _CTA_RE.pattern,
             "program_sha256": ca._digest([inspect.getsource(fn) for fn in
                 (_score_clip, _normalize_clips, _fallback_plan, _clip_id,
                  _global_summary, _analysis_context_lines, _slice_plan_prompt,
                  _parse_clock_ms, _clock_of, _snap_to_positions,
                  _assemble_slice_plan, _enforce_full_coverage)]),
             "helper_source_sha256": hashlib.sha256(Path(hp.__file__).read_bytes()).hexdigest(),
             # 医学闸门（2026-11-10 第三库）：身份含快照指纹与阈值——库/阈值变了旧计划缓存失效；
             # 闸门与检索模块源码指纹一并进 provenance。
             "medical_gate": gate_identity,
             "medical_gate_helper_sha256": hashlib.sha256(
                 Path(med_gate.__file__).read_bytes()).hexdigest(),
             "clip_purify": purify_id,
             "clip_purify_helper_sha256": hashlib.sha256(
                 Path(clip_purify.__file__).read_bytes()).hexdigest(),
             "output_schema_sha256": ca._digest(_SlicePlanOut.model_json_schema())}
    source = ca.build_analysis_provenance(corrected_segments,
        transcript_version=transcript_version, model=model,
        parameters={"requested": _plan_model_settings(PLAN_MAX_OUTPUT_TOKENS),
                    "retry_max_tokens": PLAN_MAX_OUTPUT_TOKENS_RETRY,
                    "max_setting_attempts": 3, "output_retries": 2,
                    "timeout_seconds": LLM_CALL_TIMEOUT_SECONDS,
                    "llm_call_mode": "single_global_slice_plan"},
        rules_snapshot=rules, prompt_version=PLAN_PROMPT_VERSION,
        prompt_text={"system": _PLAN_SYSTEM_PROMPT,
                     "root_windows": [_slice_plan_prompt(index, analysis)]},
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
               rules_snapshot: Any = None,
               speaker_labels: dict[str, str] | None = None,
               speaker_fingerprint: str | None = None,
               gate_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """提示词A全局一次规划（两步制第一步）；失败重试一次，仍失败本地回退。

    模型输出 time_range 由 :func:`_assemble_slice_plan` 解析吸附到分段边界，
    权威毫秒按分段重算；:func:`_enforce_full_coverage` 兜底全覆盖契约。
    规划后依次执行：医学闸门硬剔除 → 净化重组（clip_purify，问答化 P1）。
    """
    index = _SegIndex(corrected_segments)
    ready, model_name, reason = _model_ready() if index.entries else (False, "", "纠错转写为空")
    # 医学闸门（规划前标注）：worker 可预计算传入（gate_profile，单一数据源保证
    # expected/actual 两边指纹逐字节一致）；未传入则现场计算（独立调用路径）。
    if gate_profile is None:
        gate_profile = med_gate.run_gate(corrected_segments, analysis)
    # 净化重组身份：说话人标注指纹由调用方（worker）解析传入，两边必须一致。
    purify_id = clip_purify.identity(speaker_fingerprint)
    analysis_for_prompt = dict(analysis)
    if gate_profile.get("enabled"):
        analysis_for_prompt["medical_annotations"] = gate_profile.get("unit_annotations") or []
    source = build_current_plan_provenance(corrected_segments, analysis_for_prompt,
        transcript_version=transcript_version, model=model_name,
        rule_snapshot_id=rule_snapshot_id, rules_snapshot=rules_snapshot,
        medical_gate_identity=gate_profile.get("identity"),
        purify_identity=purify_id)
    notes: list[str] = []
    started = time.monotonic()
    usage_records, actual_calls = [], []
    reserved_total = 0
    plan_warnings: list[str] = []
    global_overflow = len(index.entries) > SLICE_GLOBAL_MAX_SEGMENTS
    if global_overflow:
        notes.append(f"转写 {len(index.entries)} 段超过全局规划上限 "
                     f"{SLICE_GLOBAL_MAX_SEGMENTS}，跳过提示词A改走本地回退")

    def _global_call() -> tuple[_SlicePlanOut, dict[str, Any]]:
        nonlocal reserved_total
        prompt = _slice_plan_prompt(index, analysis_for_prompt)
        estimate = llm_budget.estimate_reserve(prompt, PLAN_MAX_OUTPUT_TOKENS_RETRY)
        reserved_total += estimate
        if progress:
            progress(f"模型切片规划（提示词A全局一次，{len(index.entries)} 段）")
        token = _plan_call_metadata.set(None)
        usage = llm_budget.extract_usage_details(None)
        metadata: dict[str, Any] = {}
        try:
            result = _run_plan_agent(model_name, prompt)
            metadata = _plan_call_metadata.get() or {}
            usage = dict(metadata.get("usage") or usage)
            llm_budget.record_call("clip_planning", job_id=job_id,
                est_reserved=estimate, usage=usage, model=model_name)
            if not result.slices and not result.discarded_segments:
                raise ValueError("模型未返回切片或弃用说明")
            return result, metadata
        finally:
            _plan_call_metadata.reset(token)
            usage_records.append(usage)
            actual_calls.append({"window_id": "global", "model": model_name,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "parameters": copy.deepcopy(metadata.get("settings") or {}),
                "usage": usage, "reserved_tokens_estimate": estimate})

    slice_plan: _SlicePlanOut | None = None
    llm_error: str | None = None
    metadata: dict[str, Any] = {}
    if ready and not global_overflow:
        for attempt in range(2):
            try:
                slice_plan, metadata = _global_call()
                llm_error = None
                break
            except Exception as exc:  # noqa: BLE001 — 重试一次后本地回退
                llm_error = f"{type(exc).__name__}: {str(exc)[:400]}"
                if attempt == 0:
                    time.sleep(1.0)

    metadata_by_boundary: dict[tuple[str, str], dict] = {}
    raw_candidates: list[Any] = []
    discarded: list[dict[str, Any]] = []
    fallback_cap = False
    hierarchical: dict[str, Any] = {"mode": "single_global_slice_plan",
                                    "helper_version": hp.HELPER_VERSION,
                                    "windows": [], "failed_windows": [], "degradations": []}
    if slice_plan is not None:
        plan_source = "llm"
        candidates_llm, discarded_llm, a_meta = _assemble_slice_plan(slice_plan, index, notes)
        raw_candidates = candidates_llm
        discarded.extend(discarded_llm)
        metadata_by_boundary.update(a_meta)
    else:
        plan_source = "heuristic_fallback"
        if ready:
            hierarchical["degradations"] = ["slice_plan_failed"]
            if not llm_error:
                llm_error = reason or "提示词A全局规划未产出有效切片"
        else:
            hierarchical["degradations"] = ["model_unavailable"]
            llm_error = reason
        kept, discarded_fb, fallback_cap = _fallback_plan(index, analysis, notes) \
            if index.entries else ([], [], False)
        raw_candidates = [_PlanClipOut.model_validate(candidate) for candidate in kept]
        for candidate in kept:
            metadata_by_boundary[(candidate["start_segment_id"], candidate["end_segment_id"])] = {
                "plan_source": "heuristic_fallback", "lineage": []}
        discarded.extend(discarded_fb)

    normalized, cap_applied = _normalize_clips(raw_candidates, index, analysis, notes)
    normalized.sort(key=lambda candidate: candidate["_start_pos"])
    clips = []
    for candidate in normalized:
        boundary = (candidate["start_segment_id"], candidate["end_segment_id"])
        extra = metadata_by_boundary.get(boundary, {})
        slice_meta = candidate.pop("_slice_meta", None) or {}
        entry = dict(candidate, clip_id=_clip_id(project_id, candidate["start_ms"], candidate["end_ms"]),
                     **slice_meta)
        clip = _finalize(entry)
        clip["plan_source"] = extra.get("plan_source", plan_source)
        clip["lineage"] = extra.get("lineage", [])
        clips.append(clip)
    # 医学闸门·规划后硬剔除（2026-11-10 用户拍板）：医学密度不达标的切片（如聊家常）
    # 程序侧移入 discarded（type=非医学闲聊）；annotate_only/disabled 模式只标注不动。
    clips, moved_discarded, gate_decisions = med_gate.enforce_clips(
        gate_profile, clips, index, notes)
    if moved_discarded:
        plan_warnings.append(
            f"medical_gate: 硬剔除 {len(moved_discarded)} 条非医学切片"
            f"（闸门模式={gate_profile.get('mode')}）")
    # 净化重组（2026-11-11 问答化 P1/P2）：删除切片内纯应答碎片与患者插话
    # （qa 保护提问钩子；mono 全删患者话语），产出有序子区间；任何失败整片回退。
    purify_summary = clip_purify.purify_clips(clips, corrected_segments, speaker_labels,
                                              analysis=analysis, gate_profile=gate_profile)
    purify_warns = [n for n in (purify_summary.get("notes") or [])]
    plan_warnings.extend(purify_warns[:4])
    if purify_summary.get("purified"):
        plan_warnings.insert(0, f"clip_purify: {purify_summary['purified']} 条切片已净化重组（问答化）")
    discarded = _enforce_full_coverage(index, clips, discarded + moved_discarded, plan_warnings)
    warnings = plan_warnings
    if cap_applied or fallback_cap:
        warnings.append("切片数量达到资源保护上限，已按综合得分保留最优")
    if slice_plan is None and ready and not global_overflow:
        warnings.append(f"提示词A全局规划失败，已本地回退（来源={plan_source}）")
        if llm_error:
            notes.append(f"LLM 失败原因：{llm_error}")
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
    if slice_plan is not None:
        record["video_summary"] = str(slice_plan.video_summary or "")
        record["slice_count_declared"] = int(slice_plan.slice_count or 0)
    record.update(llm_usage=usage, actual_calls=actual_calls, source_provenance=source,
                  cache_verification=ca.verify_analysis_provenance({"source_provenance": source}),
                  hierarchical_planning=hierarchical,
                  failed_windows=hierarchical["failed_windows"],
                  degradations=hierarchical["degradations"],
                  medical_gate=med_gate.record_summary(gate_profile, gate_decisions,
                                                       len(moved_discarded)),
                  purify=purify_summary,
                  callback_calls=len(actual_calls), callback_budget=2)
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
