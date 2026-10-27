"""剪辑规则注册表（Editing Workflow Registry）。

职责：
1. 读取并校验 ``editing-workflow.manifest.json``（缺失/损坏必须显式报错，绝不静默退化）；
2. 把「已启用的最新 Skill」编译成**任务级规则快照**并落盘，保证旧成片可复现、可查历史规则版本；
3. 提供规则优先级解析：用户本次明确指令 > 当前项目设置 > 已启用的最新 Skill > 系统默认值。

设计约束：
- 本模块只用标准库，避免给后端引入新依赖；
- 校验和不匹配**不阻断任务**，而是把该 skill 标记为 ``stale`` 并告警（成员更新词表/风格后应看到提示）；
- 旧硬编码规则只能作为最低优先级的默认层（``legacy_default``）。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 机器码：便于日志检索与前端提示
E_MANIFEST_INVALID = "E_MANIFEST_INVALID"
E_MANIFEST_MISSING = "E_MANIFEST_MISSING"
E_SNAPSHOT_WRITE_FAILED = "E_SNAPSHOT_WRITE_FAILED"

DEFAULT_MANIFEST_PATH = "/domain/video-brain/config/editing-workflow.manifest.json"
SNAPSHOT_FILENAME = "workflow_snapshot.json"

# 优先级来源标识（由高到低）
SOURCE_USER_INSTRUCTION = "user_instruction"
SOURCE_PROJECT_SETTINGS = "project_settings"
SOURCE_SKILL = "enabled_skill"
SOURCE_SYSTEM_DEFAULT = "system_default"


class SkillRegistryError(ValueError):
    """注册表错误基类：带机器码，路由层可映射为 4xx 并透出可读原因。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def manifest_path() -> Path:
    """清单路径：环境变量优先，便于测试与部署覆盖。"""
    override = os.getenv("EDITING_WORKFLOW_MANIFEST")
    if override:
        return Path(override)
    home = os.getenv("VIDEO_BRAIN_HOME")
    if home:
        candidate = Path(home) / "config" / "editing-workflow.manifest.json"
        if candidate.is_file():
            return candidate
    return Path(DEFAULT_MANIFEST_PATH)


def _sha256_16(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _schema_shape_ok(schema: Any) -> bool:
    """极简 JSON Schema 形状校验：只校验我们真正依赖的字段，避免引入 jsonschema 依赖。"""
    if schema is None:
        return True
    if not isinstance(schema, dict):
        return False
    if "type" in schema and not isinstance(schema["type"], str):
        return False
    if "properties" in schema and not isinstance(schema["properties"], dict):
        return False
    if "required" in schema and not (
        isinstance(schema["required"], list)
        and all(isinstance(item, str) for item in schema["required"])
    ):
        return False
    return True


@dataclass
class SkillStatus:
    """单个 skill 的校验结果。"""

    skill_id: str
    version: str
    enabled: bool
    status: str  # ok | stale | missing | invalid | disabled
    paths: list[str] = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "enabled": self.enabled,
            "status": self.status,
            "paths": self.paths,
            "detail": self.detail,
        }


@dataclass
class ValidationReport:
    """清单校验报告。"""

    workflow_version: str
    ok: bool
    skills: list[SkillStatus] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "workflow_version": self.workflow_version,
            "ok": self.ok,
            "skills": [item.as_dict() for item in self.skills],
            "errors": self.errors,
            "warnings": self.warnings,
        }

    @property
    def stale_skills(self) -> list[str]:
        return [item.skill_id for item in self.skills if item.status == "stale"]


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    """读取清单；缺失/损坏/结构非法 → SkillRegistryError（带机器码）。"""
    target = path or manifest_path()
    if not target.is_file():
        raise SkillRegistryError(E_MANIFEST_MISSING, f"找不到剪辑规则清单：{target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SkillRegistryError(E_MANIFEST_INVALID, f"清单无法解析：{target}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise SkillRegistryError(E_MANIFEST_INVALID, "清单根节点必须是对象")
    if not payload.get("workflow_version"):
        raise SkillRegistryError(E_MANIFEST_INVALID, "清单缺少 workflow_version")
    if not isinstance(payload.get("skills"), list) or not payload["skills"]:
        raise SkillRegistryError(E_MANIFEST_INVALID, "清单缺少非空 skills 列表")
    return payload


def validate_manifest(
    manifest: dict[str, Any] | None = None, *, verify_checksums: bool = True
) -> ValidationReport:
    """校验清单：文件可读、版本非空、校验和一致、输入输出 schema 形状合法。"""
    data = manifest if manifest is not None else load_manifest()
    report = ValidationReport(workflow_version=str(data.get("workflow_version", "")), ok=True)

    for raw in data.get("skills", []):
        if not isinstance(raw, dict):
            report.errors.append("skills 列表存在非对象项")
            report.ok = False
            continue
        skill_id = str(raw.get("skill_id") or "")
        version = str(raw.get("version") or "")
        enabled = bool(raw.get("enabled", True))
        status = SkillStatus(skill_id=skill_id, version=version, enabled=enabled, status="ok")

        if not skill_id:
            status.status = "invalid"
            status.detail = "缺少 skill_id"
            report.errors.append(status.detail)
            report.ok = False
        if not version:
            status.status = "invalid"
            status.detail = "缺少 version"
            report.errors.append(f"{skill_id or '(未命名)'}：缺少 version")
            report.ok = False
        if not _schema_shape_ok(raw.get("input_schema")) or not _schema_shape_ok(raw.get("output_schema")):
            status.status = "invalid"
            status.detail = "input_schema/output_schema 形状非法"
            report.errors.append(f"{skill_id}：schema 形状非法")
            report.ok = False

        if not enabled:
            status.status = "disabled"
            report.skills.append(status)
            continue

        paths = [str(raw.get("source_path") or "")]
        paths.extend(str(item) for item in (raw.get("extra_paths") or []))
        status.paths = [item for item in paths if item]
        expected = {str(raw.get("source_path") or ""): str(raw.get("checksum") or "")}
        expected.update({str(k): str(v) for k, v in (raw.get("extra_checksums") or {}).items()})

        for item in status.paths:
            target = Path(item)
            if not target.is_file():
                status.status = "missing"
                status.detail = f"文件不存在：{item}"
                report.errors.append(f"{skill_id}：{status.detail}")
                report.ok = False
                continue
            if verify_checksums and expected.get(item):
                actual = _sha256_16(target)
                if actual != expected[item]:
                    status.status = "stale" if status.status == "ok" else status.status
                    status.detail = f"校验和不匹配（清单 {expected[item]} / 磁盘 {actual}）：{item}"
                    report.warnings.append(f"{skill_id}：{status.detail}")
        report.skills.append(status)

    enabled_skills = [item for item in report.skills if item.enabled]
    if not enabled_skills:
        report.errors.append("没有任何已启用的 skill")
        report.ok = False
    return report


def enabled_skills(manifest: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """按 priority 升序返回已启用的 skill 定义（priority 小 = 越优先）。"""
    data = manifest if manifest is not None else load_manifest()
    items = [raw for raw in data.get("skills", []) if isinstance(raw, dict) and raw.get("enabled", True)]
    return sorted(items, key=lambda raw: (int(raw.get("priority") or 999), str(raw.get("skill_id") or "")))


def skill_definition(skill_id: str, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """取某个 skill 定义；不存在 → SkillRegistryError。"""
    for raw in enabled_skills(manifest):
        if str(raw.get("skill_id")) == skill_id:
            return raw
    raise SkillRegistryError(E_MANIFEST_INVALID, f"清单中没有已启用的 skill：{skill_id}")


def _rule_value(manifest: dict[str, Any], section: str, key: str | None = None) -> Any:
    """从某个规则段取参数（段不存在返回 None）。"""
    section_value = manifest.get(section)
    if not isinstance(section_value, dict):
        return None
    if key is None:
        return section_value
    return section_value.get(key)


def resolve_rule(
    key: str,
    *,
    section: str | None = None,
    user_instruction: dict[str, Any] | None = None,
    project_settings: dict[str, Any] | None = None,
    skill_value: Any = None,
    default: Any = None,
) -> tuple[Any, str]:
    """规则优先级解析：用户指令 > 项目设置 > 已启用 Skill > 系统默认。

    返回 ``(生效值, 来源标识)``；调用方应把来源写进快照/日志，便于复现与审计。
    """
    if user_instruction and key in user_instruction and user_instruction[key] is not None:
        return user_instruction[key], SOURCE_USER_INSTRUCTION
    if project_settings and key in project_settings and project_settings[key] is not None:
        return project_settings[key], SOURCE_PROJECT_SETTINGS
    if skill_value is not None:
        return skill_value, SOURCE_SKILL
    return default, SOURCE_SYSTEM_DEFAULT


def build_workflow_snapshot(
    task_id: str,
    *,
    snapshot_dir: str | Path,
    manifest: dict[str, Any] | None = None,
    user_instruction: dict[str, Any] | None = None,
    project_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成本次任务的规则快照并落盘（原子写）。

    快照记录 workflow_version + 每个启用 skill 的 id/version/checksum + 生效规则，
    后续步骤只允许读快照，从而保证「规则更新只影响新任务、旧成片可复现」。
    """
    data = manifest if manifest is not None else load_manifest()
    report = validate_manifest(data)
    rules_source: dict[str, str] = {}
    resolved: dict[str, Any] = {}
    for section in (
        "transcription_rules",
        "correction_rules",
        "speaker_rules",
        "highlight_selection_rules",
        "clip_planning_rules",
        "correction_gate_rules",
        "subtitle_rules",
        "cover_rules",
        "title_rules",
        "render_rules",
        "qa_rules",
    ):
        value, source = resolve_rule(
            section,
            user_instruction=user_instruction,
            project_settings=project_settings,
            skill_value=_rule_value(data, section),
            default=None,
        )
        resolved[section] = value
        rules_source[section] = source

    snapshot = {
        "task_id": task_id,
        "workflow_version": data.get("workflow_version"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest_path": str(manifest_path()),
        "validation_ok": report.ok,
        "validation_warnings": report.warnings,
        "stale_skills": report.stale_skills,
        "skills": [
            {
                "skill_id": raw.get("skill_id"),
                "version": raw.get("version"),
                "checksum": raw.get("checksum"),
                "priority": raw.get("priority"),
                "source_path": raw.get("source_path"),
            }
            for raw in enabled_skills(data)
        ],
        "rules": resolved,
        "rules_source": rules_source,
    }

    target_dir = Path(snapshot_dir)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / SNAPSHOT_FILENAME
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:  # 磁盘满等
        raise SkillRegistryError(E_SNAPSHOT_WRITE_FAILED, f"规则快照写入失败：{exc}") from exc
    snapshot["snapshot_path"] = str(target_dir / SNAPSHOT_FILENAME)
    return snapshot


AUDIT_KEY = "plan_audit"


def append_snapshot_audit(snapshot_dir: str | Path, audit: dict[str, Any]) -> dict[str, Any]:
    """把本次任务的闸门/例外审计追加进任务级规则快照（原子写）。

    G.6：``workflow_snapshot.json`` 必须包含「本次生效的规则版本 + 每条的四类校验
    结论 + 例外标记（duration_exception / needs_review）」，任务详情直接读它即可
    回答「这次是按哪版规则、哪些判定出的片」。旧任务没有快照时返回空 dict（不凭空造）。
    """
    target_dir = Path(snapshot_dir)
    target = target_dir / SNAPSHOT_FILENAME
    if not target.is_file():
        return {}
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillRegistryError(E_SNAPSHOT_WRITE_FAILED,
                                 f"规则快照不可读，无法写入审计：{exc}") from exc
    if not isinstance(document, dict):
        return {}
    history = [item for item in (document.get(AUDIT_KEY) or []) if isinstance(item, dict)]
    video_task_id = str(audit.get("video_brain_task_id") or "")
    history = [item for item in history
               if not video_task_id or str(item.get("video_brain_task_id")) != video_task_id]
    history.append(dict(audit))
    document[AUDIT_KEY] = history[-10:]
    document["audit_version"] = "audit@1"
    temporary = target.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        temporary.replace(target)
    except OSError as exc:
        raise SkillRegistryError(E_SNAPSHOT_WRITE_FAILED,
                                 f"规则快照审计写入失败：{exc}") from exc
    return document


def read_workflow_snapshot(snapshot_dir: str | Path) -> dict[str, Any] | None:
    """读回任务规则快照（用于「旧成片查看其历史规则版本」）。"""
    target = Path(snapshot_dir) / SNAPSHOT_FILENAME
    if not target.is_file():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def describe_active_workflow(manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """给「规则中心」用的只读摘要：当前版本 + 各 skill 版本/更新时间 + 失效提示。

    纯函数，不落盘、不改状态，供前端轮询；不重启浏览器即可看到新版本。
    """
    data = manifest if manifest is not None else load_manifest()
    report = validate_manifest(data)
    return {
        "workflow_version": data.get("workflow_version"),
        "enabled": bool(data.get("enabled", True)),
        "updated_at": data.get("updated_at"),
        "validation_ok": report.ok,
        "warnings": report.warnings,
        "skills": [
            {
                "skill_id": item.skill_id,
                "version": item.version,
                "enabled": item.enabled,
                "status": item.status,
                "detail": item.detail,
                "paths": item.paths,
            }
            for item in report.skills
        ],
        "rules": {
            section: data.get(section)
            for section in (
                "transcription_rules",
                "correction_rules",
                "speaker_rules",
                "highlight_selection_rules",
                "clip_planning_rules",
                "subtitle_rules",
                "cover_rules",
                "title_rules",
                "render_rules",
                "qa_rules",
            )
        },
    }


def snapshot_for_task(task_id: str, *, jobs_root: str | Path) -> dict[str, Any] | None:
    """按任务 id 定位并读取规则快照（工作台任务目录约定）。"""
    return read_workflow_snapshot(Path(jobs_root) / task_id)


# ---------------------------------------------------------------------------
# Phase B 接线契约：给编排器/任务记录/UI 用的稳定小接口（纯函数，无副作用）
# ---------------------------------------------------------------------------

def task_versions(snapshot: dict[str, Any]) -> dict[str, Any]:
    """抽出应写入任务记录的版本字段（workflow_version + 各 skill 版本）。"""
    return {
        "workflow_version": snapshot.get("workflow_version"),
        "skill_versions": {
            str(item.get("skill_id")): item.get("version")
            for item in (snapshot.get("skills") or [])
        },
        "snapshot_path": snapshot.get("snapshot_path"),
    }


def _tagged(version: Any) -> str:
    """版本号统一带 v 前缀，且不重复叠加（清单里写 v1 或 1 都得到 v1）。"""
    text = str(version or "unknown")
    return text if text.startswith("v") else f"v{text}"


def workflow_version_line(snapshot: dict[str, Any] | None) -> str:
    """成片详情展示用的中文版本行，例如：
    「使用规则版本：workflow v2026-10-12.1 / cover v1 / clip_planning v1」。
    """
    if not snapshot:
        return "使用规则版本：未知（无规则快照）"
    parts = [f"workflow {_tagged(snapshot.get('workflow_version'))}"]
    for item in snapshot.get("skills") or []:
        skill_id = str(item.get("skill_id") or "")
        if skill_id == "legacy_default":  # 最低优先级默认层不进展示行
            continue
        parts.append(f"{skill_id} {_tagged(item.get('version'))}")
    return "使用规则版本：" + " / ".join(parts)


def active_workflow_banner(manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """「开始自动剪辑」按钮旁的提示数据：当前版本 + 是否有新版本/失效提示。

    返回结构可直接喂给前端；不落盘、不改状态，可随浏览器轮询刷新。
    """
    data = manifest if manifest is not None else load_manifest()
    report = validate_manifest(data)
    version = str(data.get("workflow_version") or "unknown")
    if report.warnings:
        hint = f"检测到新规则版本，下一次自动剪辑将使用 v{version}。"
    else:
        hint = ""
    return {
        "current_version": version,
        "label": f"当前使用：医疗短视频剪辑规则 v{version}",
        "update_hint": hint,
        "stale_skills": report.stale_skills,
        "validation_ok": report.ok,
    }


def attach_snapshot_to_payload(
    payload: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    """把规则快照摘要挂到下游任务载荷（video-brain 会忽略未知字段，向后兼容）。"""
    payload = dict(payload)
    payload["workflow_snapshot"] = {
        "workflow_version": snapshot.get("workflow_version"),
        "skills": [
            {"skill_id": item.get("skill_id"), "version": item.get("version"), "checksum": item.get("checksum")}
            for item in (snapshot.get("skills") or [])
        ],
        "rules": snapshot.get("rules"),
        "created_at": snapshot.get("created_at"),
    }
    return payload
