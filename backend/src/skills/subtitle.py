"""字幕合成 Skill 适配器：字幕在 video-brain 固定流程内执行，独立 adapter 待 Phase B。"""

from __future__ import annotations

from typing import Any

from .base import SkillContext, SkillResult, missing_capability

SKILL_ID = "subtitle_composition"

PHASE_B_NOTE = (
    "字幕合成由 video-brain app/subtitles.py（build/ass_content/srt_content）在渲染流程内调用，"
    "当前没有可跨服务调用的端点。"
    "Phase B：为 video-brain 增 /v1/tools/subtitle.compose 端点（或允许工作台以子进程方式调用该模块）后实现；"
    "字幕样式参数届时从 subtitle_rules 读。"
)


def input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["segments", "clip_start", "clip_end"],
        "properties": {
            "segments": {"type": "array"},
            "clip_start": {"type": "number"},
            "clip_end": {"type": "number"},
            "style": {"type": "object"},
        },
    }


def output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["ass_path", "srt_path"],
        "properties": {"ass_path": {"type": "string"}, "srt_path": {"type": "string"}},
    }


def run_subtitle_composition_skill(
    ctx: SkillContext,
    *,
    segments: list[dict[str, Any]] | None = None,
    clip_start: float = 0.0,
    clip_end: float = 0.0,
    style: dict[str, Any] | None = None,
) -> SkillResult:
    """未实现：显式失败，避免绕过 render_rules.subtitle_policy 私自生成字幕文件。"""
    raise missing_capability(SKILL_ID, PHASE_B_NOTE)
