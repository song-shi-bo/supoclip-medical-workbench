"""转写 Skill 适配器：包装 video-brain 既有转写端点的（热词注入在那边实现）。"""

from __future__ import annotations

from typing import Any

from .base import SkillContext, SkillExecutionError, SkillResult, call_video_brain, rule_value, skill_version

SKILL_ID = "transcription"


def input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["media_path"],
        "properties": {
            "media_path": {"type": "string"},
            "language": {"type": "string"},
        },
    }


def output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["segments"],
        "properties": {"segments": {"type": "array"}, "notes": {"type": "array"}},
    }


def run_transcription_skill(
    ctx: SkillContext,
    *,
    media_path: str | None = None,
    language: str | None = None,
) -> SkillResult:
    """按 manifest 的 transcription_rules 执行转写（热词表由 video-brain 侧加载）。"""
    target = media_path or ctx.source_video
    if not target:
        raise SkillExecutionError("E_SKILL_INPUT", "缺少 media_path / ctx.source_video")

    lang, lang_source = rule_value(ctx, "transcription_rules", "language", default="zh")
    payload = {"media_path": target, "language": language or lang}
    view = call_video_brain(ctx, "/v1/tools/transcript.transcribe", payload)
    segments = view.get("segments") or []
    if not segments:
        raise SkillExecutionError("E_SKILL_EMPTY", "转写返回空 segments")
    ctx.segments = segments
    return SkillResult(
        skill_id=SKILL_ID,
        version=skill_version(SKILL_ID, ctx),
        ok=True,
        output={"segments": segments, "notes": view.get("notes") or []},
        notes=list(view.get("notes") or []),
        rules_source={"transcription_rules.language": lang_source},
    )
