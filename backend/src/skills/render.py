"""渲染 Skill 适配器：包装 video-brain 任务端点（固定剪辑流程负责真实渲染）。"""

from __future__ import annotations

from typing import Any

from .base import SkillContext, SkillExecutionError, SkillResult, call_video_brain, rule_value, skill_version

SKILL_ID = "render"


def input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["source_video"],
        "properties": {
            "source_video": {"type": "string"},
            "required_clip_count": {},
            "duration_range": {"type": "array"},
            "source_transcript": {"type": "array"},
            "style_id": {"type": "string"},
        },
    }


def output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["task_id"],
        "properties": {"task_id": {"type": "string"}, "status": {"type": "string"}},
    }


def run_render_skill(
    ctx: SkillContext,
    *,
    source_video: str | None = None,
    required_clip_count: Any = "auto",
    duration_range: list[float] | None = None,
    source_transcript: list[dict[str, Any]] | None = None,
    style_id: str | None = None,
    idempotency_key: str | None = None,
) -> SkillResult:
    """按 render_rules 提交渲染任务（返回 video-brain 任务 id，供后续轮询/QA）。"""
    media = source_video or ctx.source_video
    if not media:
        raise SkillExecutionError("E_SKILL_INPUT", "缺少 source_video")

    resolution, resolution_source = rule_value(ctx, "render_rules", "resolution", default="1080x1920")
    platform, platform_source = rule_value(ctx, "render_rules", "platform", default="douyin")

    payload: dict[str, Any] = {
        "source_video": media,
        "required_clip_count": required_clip_count,
        "resolution": resolution,
        "platform": platform,
        "idempotency_key": idempotency_key or f"skill-{ctx.task_id}",
    }
    if duration_range:
        payload["duration_range"] = duration_range
    if source_transcript:
        payload["source_transcript"] = source_transcript
    if style_id:
        payload["style_id"] = style_id

    view = call_video_brain(ctx, "/v1/tasks", payload, timeout=600.0)
    task_id = view.get("task_id")
    if not task_id:
        raise SkillExecutionError("E_SKILL_BAD_RESPONSE", "渲染端点未返回 task_id")
    return SkillResult(
        skill_id=SKILL_ID,
        version=skill_version(SKILL_ID, ctx),
        ok=True,
        output={"task_id": task_id, "status": view.get("status")},
        notes=[f"已提交渲染任务 {task_id}"],
        rules_source={
            "render_rules.resolution": resolution_source,
            "render_rules.platform": platform_source,
        },
    )
