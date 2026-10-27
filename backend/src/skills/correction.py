"""纠错 Skill 适配器：包装 video-brain 既有纠错端点（术语表 + 医疗红线在那边实现）。"""

from __future__ import annotations

from typing import Any

from .base import SkillContext, SkillExecutionError, SkillResult, call_video_brain, rule_value, skill_version

SKILL_ID = "correction"


def input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["segments"],
        "properties": {
            "segments": {"type": "array"},
            "llm_enabled": {"type": "boolean"},
        },
    }


def output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["segments"],
        "properties": {"segments": {"type": "array"}, "notes": {"type": "array"}},
    }


def run_transcript_correction_skill(
    ctx: SkillContext,
    *,
    segments: list[dict[str, Any]] | None = None,
    llm_enabled: bool | None = None,
) -> SkillResult:
    """按 correction_rules 执行纠错；无把握的词由 video-brain 标 uncertain 进审核闭环。"""
    source = segments if segments is not None else ctx.segments
    if not source:
        raise SkillExecutionError("E_SKILL_INPUT", "缺少待纠错 segments")

    # 2026-10-13 固化：转写后模型纠错为默认（correction_rules.llm_enabled=true）；
    # 代码兜底与清单现值一致，清单缺失该键时也默认启用。
    enabled, enabled_source = rule_value(ctx, "correction_rules", "llm_enabled", default=True)
    payload = {
        "segments": source,
        "llm_enabled": bool(llm_enabled if llm_enabled is not None else enabled),
    }
    view = call_video_brain(ctx, "/v1/tools/transcript.correct_full", payload)
    corrected = view.get("segments") or []
    if not corrected:
        raise SkillExecutionError("E_SKILL_EMPTY", "纠错返回空 segments")
    ctx.segments = corrected
    return SkillResult(
        skill_id=SKILL_ID,
        version=skill_version(SKILL_ID, ctx),
        ok=True,
        output={"segments": corrected, "notes": view.get("notes") or []},
        notes=list(view.get("notes") or []),
        rules_source={"correction_rules.llm_enabled": enabled_source},
    )
