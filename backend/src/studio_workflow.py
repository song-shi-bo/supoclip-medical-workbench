"""Phase B 接线层：把「剪辑规则注册表（skill_registry）」接到工作室任务链路上的统一入口。

本模块**只做接线，不做规则实现**：

- 快照的生成 / 校验 / 读回全部复用 ``skill_registry``（Phase A 交付，不改其实现）；
- 任务级快照的**存放位置**在这里唯一定义（AUTO_EDIT 放产物目录，局部任务放自己的任务目录）；
- 适配器（``skills`` 包）只允许读**本次任务的冻结快照**，因此这里负责把快照的 ``rules``
  折成适配器认识的「清单视图」（适配器按清单段名读规则，见 skills/base.py::rule_value）。

为什么需要「清单视图」
----------------------
快照的落盘结构是 ``{"rules": {"cover_rules": {...}}, "skills": [...]}``，而适配器/注册表的
规则读取接口按**顶层段名**取（``manifest["cover_rules"]``）。把 ``snapshot["rules"]`` 摊平
到顶层、并带上 ``skills`` 列表，适配器就能**只**读到本次任务冻结的规则版本，
从而保证「规则更新只影响新任务、旧成片可复现」。

写盘约定：快照文件名与原子写由 skill_registry 负责，本模块不另起一套。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

# 双模式导入：本模块既被 Studio 后端按包导入（``src.studio_workflow``），
# 也被固化流程执行器按**顶层模块**导入（``python backend/src/autoedit_runner_video_brain.py``
# 时 ``backend/src`` 就在 sys.path 上）。用包相对导入会让执行器侧 ImportError，
# 于是「规则版本写台账」会整段静默失败——所以这里显式兼容两种加载方式。
try:  # 包内（Studio 后端）
    from . import skill_registry
except ImportError:  # pragma: no cover - 执行器以顶层模块方式导入
    import skill_registry  # type: ignore[no-redef]

# `skills` 包**只在需要适配器上下文时**惰性导入：它内部用包相对导入（``from .. import``），
# 顶层模块加载方式下不可用；而执行器只用版本查询（versions_for_root），不需要适配器。
if TYPE_CHECKING:  # pragma: no cover
    from . import skills as _skills_type


def _skills_module() -> Any:
    """取 ``skills`` 适配器包（兼容包内 / 顶层模块两种加载方式）。"""
    try:
        from . import skills as module
    except ImportError:  # pragma: no cover - 顶层模块加载
        import skills as module  # type: ignore[no-redef]
    return module

TASKS_ROOT = Path(os.getenv("STUDIO_TASKS_ROOT", "/app/tasks")).resolve()

LOCALTASKS_DIRNAME = "localtasks"
OUTPUT_DIRNAME = "output"

# 任务 id 白名单（与 studio_local_tasks.TASK_ID_PATTERN / studio_auto_edit_bridge 一致）：
# 快照目录由 task_id 直接拼出，必须在这里挡掉 ``../`` 之类的穿越。
# Phase 1 新增的局部任务前缀（msub/xfm/aud/fx/rnd）必须在这里同样放行：否则
# create_task 建规则快照时会以 E_TASK_ID_INVALID 失败，GET /studio/workflow/{task_id}/snapshot
# 也会把合法的新类型任务误判为非法 id（两处都走 validate_task_id）。
TASK_ID_PATTERN = re.compile(
    r"^(?:(autoedit|cover|title|subtitle|edit|msub|xfm|aud|fx|rnd)_[a-f0-9]{12}|[a-f0-9]{32})$")

# workflow_version 形如 2026-10-12.1：启停 skill 时递增末尾序号，让「规则中心」能看到新版本。
_VERSION_SUFFIX_PATTERN = re.compile(r"^(?P<head>.*?)(?P<serial>\d+)$")


class WorkflowError(ValueError):
    """带 E_ 码的接线层错误（路由层据此映射 4xx 并透出可读原因）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


E_TASK_ID_INVALID = "E_TASK_ID_INVALID"
E_SKILL_UNKNOWN = "E_SKILL_UNKNOWN"
E_MANIFEST_WRITE_FAILED = "E_MANIFEST_WRITE_FAILED"
E_MANIFEST_ROLLED_BACK = "E_MANIFEST_ROLLED_BACK"


# ---------------------------------------------------------------------------
# 快照位置与读写
# ---------------------------------------------------------------------------

def validate_task_id(task_id: str) -> str:
    value = str(task_id or "").strip()
    if not TASK_ID_PATTERN.fullmatch(value):
        raise WorkflowError(E_TASK_ID_INVALID, f"非法任务 id: {task_id!r}")
    return value


def snapshot_dir(root: str | Path, task_id: str) -> Path:
    """任务级规则快照目录（``root`` = 调用方自己的任务根）。

    - ``autoedit_*`` 或32位十六进制 job/run（自动剪辑）：``{root}/output/{task_id}/`` 就是该任务的目录；
    - 局部任务（``cover_/title_/subtitle_/edit_/msub_/xfm_/aud_/fx_/rnd_``）：
      ``{root}/localtasks/{task_id}/``。

    ``root`` 由调用方显式传入（而不是共用本模块的常量）：这样测试与部署可以各自覆盖
    任务根而不会写到别处，也不会出现「快照在一个根、台账在另一个根」的漂移。
    """
    value = validate_task_id(task_id)
    if value.startswith("autoedit_") or re.fullmatch(r"[a-f0-9]{32}",value):
        return Path(root) / OUTPUT_DIRNAME / value
    return Path(root) / LOCALTASKS_DIRNAME / value


def snapshot_dir_for_task(task_id: str) -> Path:
    """按本模块任务根（``STUDIO_TASKS_ROOT``）解析快照目录。"""
    return snapshot_dir(TASKS_ROOT, task_id)


def snapshot_path_for_task(task_id: str) -> Path:
    return snapshot_dir_for_task(task_id) / skill_registry.SNAPSHOT_FILENAME


def build_task_snapshot(
    task_id: str,
    *,
    snapshot_dir: str | Path | None = None,
    user_instruction: dict[str, Any] | None = None,
    project_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """为一次任务生成并落盘规则快照（复用 skill_registry，缺清单会显式报错）。

    清单缺失/损坏 → ``SkillRegistryError``（**不静默退化**，由调用方转成 4xx/5xx）。
    """
    return skill_registry.build_workflow_snapshot(
        task_id,
        snapshot_dir=Path(snapshot_dir) if snapshot_dir else snapshot_dir_for_task(task_id),
        user_instruction=user_instruction,
        project_settings=project_settings,
    )


def read_snapshot(root: str | Path, task_id: str) -> dict[str, Any] | None:
    """按任务根读回冻结快照；不存在返回 ``None``（旧任务没有快照是正常历史事实）。"""
    return skill_registry.read_workflow_snapshot(snapshot_dir(root, task_id))


def snapshot_for_task(task_id: str) -> dict[str, Any] | None:
    """按本模块任务根读回冻结快照。"""
    return read_snapshot(TASKS_ROOT, task_id)


def versions_for_root(root: str | Path, task_id: str) -> dict[str, Any]:
    """按任务根取要落库的版本字段。

    无快照（Phase B 前的旧任务）→ 返回空 dict：旧台账形状保持不变，不凭空编造版本号。
    ``workflow_snapshot_path`` 由快照目录直接算出，不依赖落盘文件里的字段
    （``build_workflow_snapshot`` 只在**内存返回值**里补 ``snapshot_path``，磁盘文件里没有）。
    """
    try:
        target = snapshot_dir(root, task_id)
        document = skill_registry.read_workflow_snapshot(target)
    except WorkflowError:
        return {}
    if not document:
        return {}
    versions = skill_registry.task_versions(document)
    return {
        "workflow_version": versions.get("workflow_version"),
        "skill_versions": versions.get("skill_versions") or {},
        "workflow_snapshot_path": str(target / skill_registry.SNAPSHOT_FILENAME),
    }


def versions_for_task(task_id: str) -> dict[str, Any]:
    """按本模块任务根取要落库的版本字段。"""
    return versions_for_root(TASKS_ROOT, task_id)


def version_line_for_task(task_id: str) -> str:
    """成片详情用的中文版本行（无快照时给出明确说明）。"""
    return skill_registry.workflow_version_line(snapshot_for_task(task_id))


# ---------------------------------------------------------------------------
# 规则读取：冻结快照 → 适配器可用的清单视图
# ---------------------------------------------------------------------------

def rule_section(snapshot: dict[str, Any] | None, section: str) -> dict[str, Any]:
    """取快照里某个规则段（不存在返回空 dict，不抛错）。"""
    rules = (snapshot or {}).get("rules")
    value = rules.get(section) if isinstance(rules, dict) else None
    return dict(value) if isinstance(value, dict) else {}


def skill_context(
    task_id: str,
    *,
    snapshot: dict[str, Any] | None = None,
    user_instruction: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
    source_video: str | None = None,
    segments: list[dict[str, Any]] | None = None,
) -> "_skills_type.SkillContext":
    """按**冻结快照**构造适配器上下文。

    适配器（``skills.run_*_skill``）与 ``skills.rule_value`` 都从 ``ctx.manifest_data()``
    按顶层段名取规则，因此这里把 ``snapshot["rules"]`` 摊平到顶层；``skills`` 列表来自
    快照本身，于是 ``skill_version()`` 报告的是**本次任务实际用的版本**，而不是磁盘上的最新版本。
    """
    skills = _skills_module()
    document = snapshot if snapshot is not None else (snapshot_for_task(task_id) or {})
    manifest_view: dict[str, Any] = dict(document.get("rules") or {})
    manifest_view["workflow_version"] = document.get("workflow_version")
    manifest_view["skills"] = list(document.get("skills") or [])
    return skills.SkillContext(
        task_id=str(task_id),
        source_video=source_video,
        segments=list(segments or []),
        settings=dict(settings or {}),
        user_instruction=dict(user_instruction or {}),
        manifest=manifest_view,
    )


def resolve_section(
    ctx: "_skills_type.SkillContext", section: str
) -> tuple[dict[str, Any], str]:
    """读整个规则段（走四层优先级：用户指令 > 项目设置 > 已启用 Skill > 系统默认）。"""
    value, source = _skills_module().rule_value(ctx, section, default={})
    return (dict(value) if isinstance(value, dict) else {}), source


def resolve_key(
    ctx: "_skills_type.SkillContext", section: str, key: str, *, default: Any = None
) -> tuple[Any, str]:
    """读单个规则项（同上优先级）。"""
    return _skills_module().rule_value(ctx, section, key, default=default)


# 封面风格的兜底值（清单缺失/字段缺失时使用；与清单现值一致）。
FALLBACK_COVER_STYLE_ID = "realframe_v2"


# 两条链路的规则字段名（配置化，不写死在代码里）：
#   首次自动剪辑 AUTO_EDIT      → cover_rules.style_for_auto_edit
#   用户主动「重新制作封面」    → cover_rules.style_for_regenerate
STYLE_KEY_BY_PURPOSE = {
    "auto_edit": "style_for_auto_edit",
    "regenerate": "style_for_regenerate",
}
# 风格来源标识（写进任务 JSON / 封面修订记录，便于追溯是谁定的风格）
SOURCE_AUTO_EDIT_DEFAULT = "auto_edit_default"
SOURCE_REGENERATE_DEFAULT = "regenerate_default"
SOURCE_USER_SPECIFIED = "user_specified"
SOURCE_FALLBACK_DEFAULT = "default_style_id_fallback"


def _cover_rules_view(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """规则段视图：任务快照优先（旧成片复现历史规则），否则清单现值。"""
    section = rule_section(snapshot, "cover_rules") if snapshot else {}
    if section:
        return section
    try:
        return dict(skill_registry.load_manifest().get("cover_rules") or {})
    except Exception:  # noqa: BLE001 — 清单读不到时由调用方用兜底值
        return {}


def cover_style_choice(snapshot: dict[str, Any] | None = None, *,
                       purpose: str = "auto_edit") -> tuple[str, str]:
    """解析封面风格 → ``(style_id, source)``。

    - ``purpose="auto_edit"``：读 ``cover_rules.style_for_auto_edit``（默认 realframe_v2）；
    - ``purpose="regenerate"``：读 ``cover_rules.style_for_regenerate``（默认 cutout_v3）；
    - 两者都缺时退回 ``default_style_id``，再退回 ``FALLBACK_COVER_STYLE_ID``。
    ``source`` ∈ auto_edit_default / regenerate_default / default_style_id_fallback。
    """
    rules = _cover_rules_view(snapshot)
    key = STYLE_KEY_BY_PURPOSE.get(purpose, "style_for_auto_edit")
    value = str(rules.get(key) or "").strip()
    if value:
        return value, (SOURCE_AUTO_EDIT_DEFAULT if purpose == "auto_edit"
                       else SOURCE_REGENERATE_DEFAULT)
    fallback = str(rules.get("default_style_id") or "").strip()
    if fallback:
        return fallback, SOURCE_FALLBACK_DEFAULT
    return FALLBACK_COVER_STYLE_ID, SOURCE_FALLBACK_DEFAULT


def cover_style_id(snapshot: dict[str, Any] | None = None, *,
                   purpose: str = "auto_edit") -> str:
    """按规则解析本次要用的封面风格（**来源是规则，不是代码常量**）。

    两条链路各读各自的规则字段（见 :func:`cover_style_choice`），于是成员在清单里
    改 ``style_for_auto_edit`` / ``style_for_regenerate`` 即可分别调整「首次自动剪辑」
    与「用户主动重新制作封面」的缺省封面风格。
    """
    return cover_style_choice(snapshot, purpose=purpose)[0]


# ---------------------------------------------------------------------------
# 规则中心：启停 skill（改清单前备份 → 写回 → 立刻校验 → 非法回滚）
# ---------------------------------------------------------------------------

def _bump_workflow_version(current: Any) -> str:
    """``2026-10-12.1`` → ``2026-10-12.2``；无法识别序号时退化为「加时间戳后缀」。"""
    text = str(current or "").strip()
    if not text:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d.1")
    match = _VERSION_SUFFIX_PATTERN.match(text)
    if match:
        return f"{match.group('head')}{int(match.group('serial')) + 1}"
    return f"{text}+{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"


def _backup_manifest(path: Path) -> Path:
    """备份清单为 ``<清单>.bak.<UTC 时间戳>``。

    同一秒内多次启停**不得**互相覆盖备份（否则丢掉真正的「改动前」状态）：
    文件名已存在时追加序号。
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak.{stamp}")
    serial = 1
    while backup.exists():
        serial += 1
        backup = path.with_name(f"{path.name}.bak.{stamp}-{serial}")
    shutil.copy2(path, backup)
    return backup


def set_skill_enabled(skill_id: str, enabled: bool | None = None) -> dict[str, Any]:
    """启停清单里的某个 skill，并返回规则中心最新的 banner + summary。

    安全阀（任务书要求）：
    1. 改清单**前**先备份为 ``<清单>.bak.<UTC 时间戳>``；
    2. 写回后**立刻** ``validate_manifest()``（读磁盘，含校验和）；
    3. 校验不通过 → 用备份回滚并抛 ``E_MANIFEST_ROLLED_BACK``（路由层 4xx）。
    """
    target = str(skill_id or "").strip()
    if not target:
        raise WorkflowError(E_SKILL_UNKNOWN, "缺少 skill_id")
    path = skill_registry.manifest_path()
    document = skill_registry.load_manifest(path)  # 缺失/损坏 → SkillRegistryError
    entries = document.get("skills")
    if not isinstance(entries, list):
        raise skill_registry.SkillRegistryError(
            skill_registry.E_MANIFEST_INVALID, "清单 skills 不是列表"
        )
    match = next(
        (item for item in entries
         if isinstance(item, dict) and str(item.get("skill_id") or "") == target),
        None,
    )
    if match is None:
        raise WorkflowError(E_SKILL_UNKNOWN, f"清单里没有 skill_id={target}")

    original = path.read_bytes()
    backup = _backup_manifest(path)
    before = bool(match.get("enabled", True))
    match["enabled"] = (not before) if enabled is None else bool(enabled)
    # 版本号推进：启停改变了「本次任务会用哪些 skill」，规则中心必须能看出这是新版本。
    previous_version = document.get("workflow_version")
    document["workflow_version"] = _bump_workflow_version(previous_version)
    document["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        path.write_bytes(original)
        raise WorkflowError(E_MANIFEST_WRITE_FAILED, f"清单写入失败：{exc}") from exc

    report = skill_registry.validate_manifest()
    if not report.ok:
        path.write_bytes(original)  # 回滚到备份内容（逐字节）
        raise WorkflowError(
            E_MANIFEST_ROLLED_BACK,
            f"启停 {target} 后清单校验不通过，已回滚：{report.errors}",
        )
    return {
        "skill_id": target,
        "enabled": bool(match["enabled"]),
        "previous_enabled": before,
        "workflow_version": document["workflow_version"],
        "previous_workflow_version": previous_version,
        "backup_path": str(backup),
        "manifest_path": str(path),
        "validation_ok": report.ok,
        "warnings": report.warnings,
        "banner": skill_registry.active_workflow_banner(document),
        "summary": skill_registry.describe_active_workflow(document),
    }


__all__ = [
    "E_MANIFEST_ROLLED_BACK",
    "E_MANIFEST_WRITE_FAILED",
    "E_SKILL_UNKNOWN",
    "E_TASK_ID_INVALID",
    "TASK_ID_PATTERN",
    "TASKS_ROOT",
    "WorkflowError",
    "build_task_snapshot",
    "read_snapshot",
    "resolve_key",
    "resolve_section",
    "set_skill_enabled",
    "skill_context",
    "snapshot_dir",
    "snapshot_dir_for_task",
    "snapshot_for_task",
    "snapshot_path_for_task",
    "validate_task_id",
    "version_line_for_task",
    "versions_for_root",
    "versions_for_task",
]
