"""QA Skill 适配器：读取 video-brain 任务的 QC 结果（不重实现质检规则）。"""

from __future__ import annotations

from typing import Any

from .base import SkillContext, SkillExecutionError, SkillResult, call_video_brain, rule_value, skill_version

SKILL_ID = "quality_assurance"


def input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["render_task_id"],
        "properties": {"render_task_id": {"type": "string"}},
    }


def output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["passed"],
        "properties": {
            "passed": {"type": "boolean"},
            "status": {"type": "string"},
            "checks": {"type": "array"},
            "error": {"type": "string"},
        },
    }


def run_quality_assurance_skill(
    ctx: SkillContext,
    *,
    render_task_id: str,
) -> SkillResult:
    """读取渲染任务的 QC/状态作为质检结论；failed 时按 qa_rules.fail_action 处理。"""
    if not render_task_id:
        raise SkillExecutionError("E_SKILL_INPUT", "缺少 render_task_id")

    view = call_video_brain(ctx, f"/v1/tasks/{render_task_id}", method="GET")
    status = str(view.get("status") or "")
    error = view.get("error")
    checks: list[dict[str, Any]] = []
    for step in view.get("steps") or []:
        if isinstance(step, dict) and step.get("step") in {"quality_checking", "rendering_clip_1"}:
            checks.append({"step": step.get("step"), "status": step.get("status"), "error": step.get("error")})

    passed = status == "completed" and not error
    fail_action, fail_source = rule_value(ctx, "qa_rules", "fail_action", default="reject_clip")
    notes = [] if passed else [f"QA 未通过（{status}）：{error or '见任务详情'}；处理动作={fail_action}"]
    return SkillResult(
        skill_id=SKILL_ID,
        version=skill_version(SKILL_ID, ctx),
        ok=passed,
        output={"passed": passed, "status": status, "checks": checks, "error": error},
        notes=notes,
        rules_source={"qa_rules.fail_action": fail_source},
    )
