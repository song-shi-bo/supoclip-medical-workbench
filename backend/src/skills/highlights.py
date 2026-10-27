"""金句与关键词 Skill 适配器：当前由规划/封面文案链路产出，独立 adapter 待 Phase B。"""

from __future__ import annotations

from typing import Any

from .base import SkillContext, SkillResult, missing_capability

SKILL_ID = "highlight_selection"

PHASE_B_NOTE = (
    "金句/关键词当前由 clip_plan.json 的 golden_sentences/keywords（规划链路）与工作台 "
    "studio_insights 产出，尚无稳定的独立入口签名。"
    "Phase B：确认 studio_insights 对外函数（或为 video-brain 增 /v1/tools/highlights.extract 端点）后实现。"
)


def input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["segments"],
        "properties": {"segments": {"type": "array"}, "top_n": {"type": "integer"}},
    }


def output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["golden_sentences", "keywords"],
        "properties": {
            "golden_sentences": {"type": "array"},
            "keywords": {"type": "array"},
        },
    }


def run_highlight_selection_skill(
    ctx: SkillContext,
    *,
    segments: list[dict[str, Any]] | None = None,
    top_n: int = 5,
) -> SkillResult:
    """未实现：显式失败，防止编排器把规划产出的关键词误当成独立金句提取结果。"""
    raise missing_capability(SKILL_ID, PHASE_B_NOTE)
