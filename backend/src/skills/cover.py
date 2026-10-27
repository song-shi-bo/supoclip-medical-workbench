"""封面生成 Skill 适配器：包装 video-brain 封面端点；风格参数来自 cover_rules/apply manifest。"""

from __future__ import annotations

from typing import Any

from .base import SkillContext, SkillExecutionError, SkillResult, call_video_brain, rule_value, skill_version

SKILL_ID = "cover"


def input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["clip_id"],
        "properties": {
            "clip_id": {"type": "string"},
            "frame_ts": {"type": "number"},
            "cover_copy": {"type": "object"},
            "style_id": {"type": "string"},
        },
    }


def output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["cover_jpg"],
        "properties": {
            "cover_jpg": {"type": "string"},
            "cover_layout": {"type": "object"},
            "style_id": {"type": "string"},
        },
    }


def run_cover_generation_skill(
    ctx: SkillContext,
    *,
    clip_id: str,
    frame_ts: float = 0.0,
    cover_copy: dict[str, Any] | None = None,
    style_id: str | None = None,
    task_id: str | None = None,
) -> SkillResult:
    """按 cover_rules 生成封面。

    默认风格取 manifest 的 ``cover_rules.default_style_id``（当前 realframe_v2）；
    ``cutout_v3`` 需要 ``/domain/.u2net/u2net.onnx`` 权重，缺失时由 video-brain 显式报错，
    本适配器**不静默降级**到其它风格。
    """
    if not clip_id:
        raise SkillExecutionError("E_SKILL_INPUT", "缺少 clip_id")
    default_style, style_source = rule_value(ctx, "cover_rules", "default_style_id", default="realframe_v2")
    chosen = style_id or default_style
    intro, intro_source = rule_value(ctx, "cover_rules", "intro_duration_s", default=0.8)

    payload: dict[str, Any] = {
        "task_id": task_id or ctx.task_id,
        "clip_id": clip_id,
        "frame_ts": frame_ts,
        "cover_copy": cover_copy or {},
        "style_id": chosen,
    }
    view = call_video_brain(ctx, "/v1/tools/cover.render", payload)
    return SkillResult(
        skill_id=SKILL_ID,
        version=skill_version(SKILL_ID, ctx),
        ok=True,
        output={
            "cover_jpg": view.get("cover_jpg_artifact_id") or view.get("cover_jpg"),
            "cover_layout": view.get("layout") or {},
            "style_id": chosen,
        },
        notes=[f"封面风格 {chosen}（intro {intro}s）"],
        rules_source={
            "cover_rules.default_style_id": style_source,
            "cover_rules.intro_duration_s": intro_source,
        },
    )
