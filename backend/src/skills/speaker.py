"""说话人 Skill 适配器：当前流水线没有独立说话人分离能力，显式声明未实现（不伪实现）。"""

from __future__ import annotations

from typing import Any

from .base import SkillContext, SkillResult, missing_capability

SKILL_ID = "speaker_diarization"

PHASE_B_NOTE = (
    "video-brain 目前无独立说话人分离模块（无 diarization/segment 模型）；"
    "说话人标签由工作台元数据标注（copilot 的 set_speaker）承担。"
    "Phase B：接入说话人分离（或把 /v1/tools/transcript.* 的输出扩展 speaker 字段）后再实现本适配器。"
)


def input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["segments"],
        "properties": {"segments": {"type": "array"}, "media_path": {"type": "string"}},
    }


def output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["segments"],
        "properties": {"segments": {"type": "array"}, "speakers": {"type": "array"}},
    }


def run_speaker_diarization_skill(
    ctx: SkillContext,
    *,
    segments: list[dict[str, Any]] | None = None,
    media_path: str | None = None,
) -> SkillResult:
    """未实现：调用即显式失败（E_SKILL_NOT_IMPLEMENTED），避免静默产出无说话人标注的结果。"""
    raise missing_capability(SKILL_ID, PHASE_B_NOTE)
