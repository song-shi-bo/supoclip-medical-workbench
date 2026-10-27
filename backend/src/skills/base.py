"""Skill 适配器公共基座：上下文、结果、规则读取与 video-brain 调用。

设计要点：
- 适配器**不复制**既有实现，只包装（本机 HTTP 调 video-brain 端点，或延迟 import 既有模块）；
- 所有可调参数**从 manifest 读**（经 skill_registry 的优先级解析），不得在适配器里写死；
- 每个适配器必须声明 input_schema / output_schema，供注册表校验与编排器传参校验。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .. import skill_registry


class SkillExecutionError(RuntimeError):
    """适配器执行失败（带机器码，便于日志与前端提示）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class SkillContext:
    """一次任务内所有适配器共享的上下文。"""

    task_id: str
    source_video: str | None = None
    segments: list[dict[str, Any]] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    user_instruction: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] | None = None
    video_brain_url: str = field(
        default_factory=lambda: os.getenv("VIDEO_BRAIN_URL", "http://127.0.0.1:8940").rstrip("/")
    )

    def manifest_data(self) -> dict[str, Any]:
        if self.manifest is None:
            self.manifest = skill_registry.load_manifest()
        return self.manifest


@dataclass
class SkillResult:
    """适配器统一返回：结果 + 生效规则来源，便于写入任务记录与审计。"""

    skill_id: str
    version: str
    ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    rules_source: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "ok": self.ok,
            "output": self.output,
            "notes": self.notes,
            "rules_source": self.rules_source,
        }


def skill_definition(skill_id: str, ctx: SkillContext) -> dict[str, Any]:
    return skill_registry.skill_definition(skill_id, ctx.manifest_data())


def skill_version(skill_id: str, ctx: SkillContext) -> str:
    """取该 skill 的版本号用于记录/展示。

    未在清单注册时返回 ``unregistered``（不抛错）：适配器仍可执行（规则段是独立读取的），
    但清单校验器会单独报告该 skill 缺失，且用户能在成片详情看到 unregistered 这个异常版本号。
    """
    try:
        return str(skill_definition(skill_id, ctx).get("version") or "unknown")
    except Exception:  # noqa: BLE001 - 版本号仅作元数据，不应阻断执行
        return "unregistered"


def rule_value(
    ctx: SkillContext,
    section: str,
    key: str | None = None,
    *,
    default: Any = None,
) -> tuple[Any, str]:
    """读取规则：优先级 用户指令 > 项目设置 > 已启用 Skill > 系统默认。"""
    manifest = ctx.manifest_data()
    section_value = manifest.get(section)
    skill_value = None
    if isinstance(section_value, dict):
        skill_value = section_value if key is None else section_value.get(key)
    return skill_registry.resolve_rule(
        section if key is None else f"{section}.{key}",
        user_instruction=ctx.user_instruction,
        project_settings=ctx.settings,
        skill_value=skill_value,
        default=default,
    )


def call_video_brain(
    ctx: SkillContext,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    method: str = "POST",
    timeout: float = 300.0,
) -> dict[str, Any]:
    """调用 video-brain 既有端点（包装而非重实现）。"""
    url = f"{ctx.video_brain_url}{path}"
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise SkillExecutionError("E_SKILL_UPSTREAM", f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SkillExecutionError("E_SKILL_UNREACHABLE", f"{method} {path} 不可达：{exc}") from exc
    try:
        parsed = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError as exc:
        raise SkillExecutionError("E_SKILL_BAD_RESPONSE", f"{method} {path} 返回非 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise SkillExecutionError("E_SKILL_BAD_RESPONSE", f"{method} {path} 返回非对象")
    return parsed


def input_schema(skill_id: str, ctx: SkillContext) -> dict[str, Any]:
    return dict(skill_definition(skill_id, ctx).get("input_schema") or {})


def output_schema(skill_id: str, ctx: SkillContext) -> dict[str, Any]:
    return dict(skill_definition(skill_id, ctx).get("output_schema") or {})


def missing_capability(skill_id: str, detail: str) -> SkillExecutionError:
    """显式声明「本阶段未实现」，绝不伪实现。"""
    return SkillExecutionError("E_SKILL_NOT_IMPLEMENTED", f"{skill_id}: {detail}")
