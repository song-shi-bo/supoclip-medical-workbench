"""切片规划 Skill 适配器：包装 video-brain 规划端点（成员裁定版提示词在那边生效）。"""

from __future__ import annotations

from typing import Any

from .base import SkillContext, SkillExecutionError, SkillResult, call_video_brain, rule_value, skill_version

SKILL_ID = "clip_planning"


def input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["segments"],
        "properties": {
            "segments": {"type": "array"},
            "n_clips": {},
            "duration_range": {"type": "array"},
            "llm_enabled": {"type": "boolean"},
            "source_duration_seconds": {"type": "number"},
        },
    }


def output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["clips"],
        "properties": {"clips": {"type": "array"}, "validated": {"type": "boolean"}},
    }


def run_clip_planning_skill(
    ctx: SkillContext,
    *,
    segments: list[dict[str, Any]] | None = None,
    n_clips: Any = "auto",
    duration_range: list[float] | None = None,
    llm_enabled: bool | None = None,
    source_duration: float | None = None,
) -> SkillResult:
    """按 clip_planning_rules 规划切片；时长/重叠/证据等硬校验在 video-brain 侧执行。"""
    source = segments if segments is not None else ctx.segments
    if not source:
        raise SkillExecutionError("E_SKILL_INPUT", "缺少已纠错 segments（请先跑纠错 skill）")

    default_range, range_source = rule_value(
        ctx, "clip_planning_rules", "duration_range_default", default=[25, 45]
    )
    enabled, enabled_source = rule_value(ctx, "clip_planning_rules", "llm_enabled", default=True)
    span = max((float(item.get("end") or 0.0) for item in source), default=0.0)

    payload = {
        "segments": source,
        "n_clips": n_clips,
        "duration_range": duration_range or default_range,
        "llm_enabled": bool(llm_enabled if llm_enabled is not None else enabled),
    }
    if ctx.task_id:
        payload["task_id"] = ctx.task_id
    view = call_video_brain(ctx, "/v1/tools/clip.plan_highlights", payload)
    clips = view.get("clips") or []
    if not clips:
        raise SkillExecutionError("E_SKILL_EMPTY", "规划返回空 clips")

    notes: list[str] = []
    if source_duration is not None and span and source_duration > span + 1:
        notes.append(f"源视频时长 {source_duration:.1f}s 与转写覆盖 {span:.1f}s 不一致，已按转写规划")
    return SkillResult(
        skill_id=SKILL_ID,
        version=skill_version(SKILL_ID, ctx),
        ok=True,
        output={
            "clips": clips,
            "validated": bool(view.get("validated", False)),
            "planning_meta": view.get("planning_meta"),
        },
        notes=notes,
        rules_source={
            "clip_planning_rules.duration_range_default": range_source,
            "clip_planning_rules.llm_enabled": enabled_source,
        },
    )
