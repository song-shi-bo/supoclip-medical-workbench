"""项目资产台账：Project → SourceVideo → Clip 的稳定层级（文件系统事实源）。

为什么需要它
------------
改造前工作台把「成片卡片」按 **任务** 聚合：``autoedit_xxx`` 任务有几条切片就渲染几张卡。
于是任何「重新制作封面」只要新建一个任务，前端就会在左侧多出一张重复的
「剪辑成片 01」卡片。本模块把卡片的事实源从「任务」搬到 **项目 + 稳定 clip_id**：

    {STUDIO_TASKS_ROOT}/projects/{project_id}/clips.json

一条 clip 在项目里**只登记一次**（``render_task_id`` 记首次生成成片的那个任务，
普通元数据登记时保留；成功重剪替换通过独立原子激活切换实例），后续局部任务只更新字段，
绝不新增条目 —— 从数据结构上消灭「重复卡片」。

落盘 schema（``clips.json``，``schema_version = 1``）
---------------------------------------------------
::

    {
      "schema_version": 1,
      "project_id": "timeline_a10463113f5f",
      "source_video": {"path": "media/autoedit_.../source.mp4", "sha256": "...",
                       "registered_at": "2026-10-12T02:20:37Z"},
      "updated_at": "...",
      "source_config": {"subtitle_config": {...}},   # 源片级配置（clip_id="source"，只存显式段）
      "clip_order": ["clip-741ef5-01"],
      "clips": {
        "clip-741ef5-01": {
          "clip_id": "clip-741ef5-01",
          "name": "剪辑成片 01",
          "title": "...", "topic": "...", "keywords": [...],
          "render_task_id": "autoedit_064d806d3218",      # 普通登记保留；成功替换时显式切换实例
          "source_start": 0.14, "source_end": 16.3, "duration": 16.96,
          "render_revision": 1,                            # 画面重渲次数（封面不计数）
          # Inspector Phase 1 配置段（缺省按 clip_config 默认值读；见 clip_config.py）：
          "transcript_revision": 0,
          "subtitle_config": {...}, "subtitle_overrides": [],
          "transform": null, "audio_tracks": [], "voice_volume": 1.0, "muted": false,
          "effects": {...},
          # cover_asset_id / cover_revision 是 cover 的**只读投影**，不落盘副本。
          "cover": {
            "cover_asset_id": "cover-clip-741ef5-01-v1",   # 卡片/导出/详情统一引用它
            "revision": 1,                                 # 封面版本号，UI 显示 v1/v2
            "style_id": "realframe_v2",
            "path": "output/autoedit_.../clip-741ef5-01/cover.jpg",
            "sha256": "...", "bytes": 182035,
            "updated_at": "...", "intro_burned": true,
            "history": [ {...每一次封面变更的完整快照，可撤销...} ]
          },
          "subtitle_style": {"updated_at": "...", "config": {...}, "render_revision": 1},
          "timeline": {"updated_at": "...", "ranges": [...], "render_task_id": "edit_..."},
          "task_history": [ {"task_id": "cover_ab12...", "task_type": "REGENERATE_COVER",
                             "status": "completed", "created_at": "...", "summary": "..."} ]
        }
      }
    }

写盘约定：全部走 ``_atomic_json``（临时文件 + ``os.replace``），刷新/崩溃后读到的
一定是完整 JSON；任何一次局部任务失败都只影响自己那个字段。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 双模式导入（同 studio_workflow）：本模块会被固化流程执行器按**顶层模块**导入，
# 包相对导入会让执行器侧的 ``from studio_project_store import register_clips`` 直接失败。
try:  # 包内（Studio 后端）
    from . import studio_workflow
except ImportError:  # pragma: no cover - 执行器以顶层模块方式导入
    import studio_workflow  # type: ignore[no-redef]

# 与 studio_auto_edit_bridge / autoedit_runner 共用同一个任务根目录。
TASKS_ROOT = Path(os.getenv("STUDIO_TASKS_ROOT", "/app/tasks")).resolve()

# backend 根目录（上传源片 studio-uploads 在 <backend>/temp/studio-uploads）。
BASE_DIR = Path(__file__).resolve().parent.parent

PROJECTS_DIRNAME = "projects"
MANIFEST_FILENAME = "clips.json"
SCHEMA_VERSION = 1
#: 测试租户标志（2026-10-19 统一项目入口）：台账 manifest 显式携带 ``tenant``
#: 字段时，生产项目列表在服务端排除——不靠项目名称/前缀判断。
TEST_TENANT = "test"
#: 项目生命周期状态（2026-10-20 归档/删除阶段）：manifest 的 ``status`` 字段，
#: 缺省 = active。这些状态的項目不出现在默认列表（archived 可显式列回）。
HIDDEN_PROJECT_STATUSES = {"archived", "deleting", "delete_failed", "deleted"}
#: 顶层「源片级配置」键（Inspector Phase 1）：clip_id == "source" 时的读写落点。
SOURCE_CONFIG_KEY = "source_config"
# 成片文件名（与 cover_replace.CLIP_FILENAME / autoedit_runner 的产物名一致）。
CLIP_FILENAME = "clip_final.mp4"

# 项目 id 同时是磁盘目录名，必须严格限制字符集（防目录穿越/注入）。
PROJECT_ID_PATTERN = re.compile(r"^timeline_[A-Za-z0-9]{1,48}$")
CLIP_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class ProjectStoreError(ValueError):
    """带 E_ 码的显式失败（沿用工作台 ValueError → HTTP 4xx 的既有映射）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


E_PROJECT_ID_INVALID = "E_PROJECT_ID_INVALID"
E_CLIP_ID_INVALID = "E_CLIP_ID_INVALID"


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_project_id(project_id: str) -> str:
    value = str(project_id or "").strip()
    if not PROJECT_ID_PATTERN.fullmatch(value):
        raise ProjectStoreError(E_PROJECT_ID_INVALID, f"非法项目 id: {project_id!r}")
    return value


def validate_clip_id(clip_id: str) -> str:
    value = str(clip_id or "").strip()
    if not CLIP_ID_PATTERN.fullmatch(value):
        raise ProjectStoreError(E_CLIP_ID_INVALID, f"非法 clip_id: {clip_id!r}")
    return value


def project_dir(project_id: str) -> Path:
    return TASKS_ROOT / PROJECTS_DIRNAME / validate_project_id(project_id)


def manifest_path(project_id: str) -> Path:
    return project_dir(project_id) / MANIFEST_FILENAME


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{os.urandom(6).hex()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.chmod((path.stat().st_mode & 0o777) if path.exists() else 0o600)
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _empty_manifest(project_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "source_video": None,
        # 源片级配置（未选成片时的编辑落点）：只存**用户显式写过**的段，
        # 读路径由 clip_config 补默认值，因此这里默认空字典而不是整份默认配置。
        SOURCE_CONFIG_KEY: {},
        "updated_at": utc_now(),
        "clip_order": [],
        "clips": {},
    }


def load_manifest(project_id: str) -> dict[str, Any]:
    """读项目台账；不存在/损坏时返回空台账（读路径永不抛错，UI 不会白屏）。"""
    project_id = validate_project_id(project_id)
    path = manifest_path(project_id)
    if not path.is_file():
        return _empty_manifest(project_id)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_manifest(project_id)
    if not isinstance(document, dict):
        return _empty_manifest(project_id)
    document.setdefault("schema_version", SCHEMA_VERSION)
    document.setdefault("project_id", project_id)
    document.setdefault("source_video", None)
    document.setdefault("clip_order", [])
    # 切片选择（2026-10-21）：老台账没有 active_clip_id → 补 None（读路径永不抛错）。
    if document.get("active_clip_id") is not None and not isinstance(document.get("active_clip_id"), str):
        document["active_clip_id"] = None
    document.setdefault("active_clip_id", None)
    if not isinstance(document.get("clips"), dict):
        document["clips"] = {}
    if not isinstance(document.get("clip_order"), list):
        document["clip_order"] = list(document["clips"].keys())
    # Inspector Phase 1：老 clips.json 没有 source_config → 补空字典（读路径永不抛错）。
    if not isinstance(document.get(SOURCE_CONFIG_KEY), dict):
        document[SOURCE_CONFIG_KEY] = {}
    return document


_project_thread_locks = {}
_project_lock_state = threading.local()


@contextmanager
def project_lock(project_id):
    """项目级跨进程锁；同线程嵌套复用已有文件锁。"""
    project_id = validate_project_id(project_id)
    try:
        from .studio_runtime import process_lock
    except ImportError:
        from studio_runtime import process_lock
    local = _project_thread_locks.setdefault(project_id, threading.RLock())
    with local:
        held = getattr(_project_lock_state, "held", set())
        if project_id in held:
            yield
            return
        with process_lock(TASKS_ROOT / ".project-locks" / (project_id + ".lock")):
            _project_lock_state.held = held | {project_id}
            try:
                yield
            finally:
                _project_lock_state.held = held


def save_manifest(project_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    with project_lock(project_id):
        current = load_manifest(project_id)
        generation = int(current.get("generation") or 0)
        if int(manifest.get("generation") or 0) != generation:
            raise ProjectStoreError("E_PROJECT_CHANGED", "项目已被其它任务更新，请重试当前操作")
        manifest["generation"] = generation + 1
        return _save_manifest_locked(project_id, manifest)


def _save_manifest_locked(project_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    manifest["schema_version"] = SCHEMA_VERSION
    manifest["updated_at"] = utc_now()
    # 源片级配置只允许由 source_config / update_source_config 维护；这里保证它一定存在
    # 且是 JSON 对象（调用方从 load_manifest 拿到的台账本就带这个键，等价于原样保留）。
    if not isinstance(manifest.get(SOURCE_CONFIG_KEY), dict):
        manifest[SOURCE_CONFIG_KEY] = {}
    # 顺序表与字典必须一致：以 clips 的插入顺序为准补齐缺项，去掉已删除项。
    order = [clip_id for clip_id in manifest.get("clip_order") or [] if clip_id in manifest["clips"]]
    for clip_id in manifest["clips"]:
        if clip_id not in order:
            order.append(clip_id)
    manifest["clip_order"] = order
    _atomic_json(manifest_path(project_id), manifest)
    return manifest


# ---------------------------------------------------------------------------
# Inspector Phase 1：clip 配置投影 + 源片级配置（规范/校验都在 clip_config）
# ---------------------------------------------------------------------------
#
# 循环导入边界：clip_config 允许 import studio_project_store（本模块），本模块只能
# **在函数内延迟 import** clip_config —— 这样 `import src.clip_config` 与
# `import src.studio_project_store` 两种入口都不会在 import 期互相等待。

def _clip_config_module() -> Any:
    """延迟取得 clip_config 模块（兼容「包内导入」与「顶层模块导入」两种模式）。"""
    try:  # 包内（Studio 后端 / 测试）
        from . import clip_config
    except ImportError:  # pragma: no cover - 固化流程执行器按顶层模块导入
        import clip_config  # type: ignore[no-redef]
    return clip_config


def with_clip_defaults(entry: dict[str, Any]) -> dict[str, Any]:
    """返回条目的**副本**，把 Phase 1 的配置段补齐默认值（含封面投影）。

    只是投影：``cover_asset_id`` / ``cover_revision`` 读自 ``entry["cover"]``，
    **不落盘副本**（封面事实源仍是 ``cover`` 结构）。
    """
    return _clip_config_module().with_clip_defaults(entry)


def source_config(project_id: str) -> dict[str, Any]:
    """源片级配置（``clip_id == "source"``）：补齐默认值后的规范化配置。"""
    manifest = load_manifest(project_id)
    raw = manifest.get(SOURCE_CONFIG_KEY)
    return _clip_config_module().normalize_config(raw if isinstance(raw, dict) else {})


def update_source_config(project_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """部分更新源片级配置（校验 → 合并 → 原子写），返回规范化后的完整配置。

    落盘只保留**用户显式写过的段**（overlay），默认值由读路径补齐 —— 否则
    ``render_config_summary`` 就无法区分 ``project`` 与 ``skill_default``。
    """
    clip_config = _clip_config_module()
    manifest = load_manifest(project_id)
    stored = manifest.get(SOURCE_CONFIG_KEY)
    stored = dict(stored) if isinstance(stored, dict) else {}
    normalized = clip_config.normalize_patch(patch)  # 非法 patch → ClipConfigError，不写盘
    new, _changed = clip_config.apply_patch(clip_config.normalize_config(stored), normalized)
    for section in normalized:
        stored[section] = new[section]
    manifest[SOURCE_CONFIG_KEY] = stored
    save_manifest(project_id, manifest)
    return clip_config.normalize_config(stored)


# ---------------------------------------------------------------------------
# 登记成片（首次自动剪辑）/ 刷新元数据
# ---------------------------------------------------------------------------

# 登记源视频时计算 sha256 的体积上限：超过就只记 size，避免为一集几百 MB / 几 GB 的
# 素材在每次读路径上重算哈希（原视频单副本化后位置固定，size+mtime 已足够做变更判定）。
SOURCE_HASH_MAX_BYTES = 256 * 1024 * 1024


def get_temp_dir() -> str:
    from .config import get_config

    return get_config().temp_dir


def get_project_name_meta(project_id: str, *, uploads_root: Path | None = None) -> dict[str, Any]:
    """只读名称元数据：台账 display_name → timeline 源片名 → None。

    不落盘、不自动创建；供列表/时间线载荷/名称编辑回显共用。
    """
    project_id = validate_project_id(project_id)
    manifest = load_manifest(project_id)  # 读路径：不存在返回空台账，不抛
    display = str(manifest.get("display_name") or "").strip()
    meta = {"project_id": project_id, "display_name": display or None,
            "display_name_updated_at": manifest.get("display_name_updated_at"),
            "updated_at": manifest.get("updated_at"),
            "status": str(manifest.get("status") or "active")}
    if not meta["display_name"]:
        root = Path(uploads_root) if uploads_root else Path(get_temp_dir())
        project_json = root / "studio-projects" / project_id / "project.json"
        try:
            tl_manifest = json.loads(project_json.read_text(encoding="utf-8"))
            source = tl_manifest.get("source") if isinstance(tl_manifest, dict) else None
            meta["fallback_name"] = str((source or {}).get("name") or "").strip() or None
        except (OSError, ValueError):
            meta["fallback_name"] = None
    return meta


def validate_project_name(name: str) -> str:
    """项目显示名规范：去首尾空格，1–60 字符；允许重复（身份仍是 project_id）。"""
    cleaned = str(name or "").strip()
    if not (1 <= len(cleaned) <= 60):
        raise ProjectStoreError("E_PROJECT_NAME_INVALID",
                                "项目名称需为 1–60 个字符（首尾空格自动去除）")
    return cleaned


def rename_project(project_id: str, display_name: str, *,
                   expected_updated_at: str | None = None) -> dict[str, Any]:
    """仅修改台账 display_name（幂等友好；乐观并发按 updated_at）。

    不改项目目录、源片路径、clip_id、转写引用与任务历史；generation 乐观锁 +
    updated_at 双重防旧页面覆盖。deleting/deleted 状态拒绝。
    """
    project_id = validate_project_id(project_id)
    display_name = validate_project_name(display_name)  # 去空格 + 1–60 校验
    path = manifest_path(project_id)
    current = load_manifest(project_id)
    status = str(current.get("status") or "active")
    if status in {"deleting", "deleted", "delete_failed"}:
        raise ProjectStoreError("E_PROJECT_NOT_MUTABLE", f"项目 {project_id} 当前 {status}：禁止重命名")
    if expected_updated_at is not None:
        disk = _load_json_file(path)
        disk_updated = str((disk or {}).get("updated_at") or "")
        if disk_updated and disk_updated != str(expected_updated_at):
            raise ProjectStoreError("E_PROJECT_RENAME_CONFLICT",
                                    "项目已在其它页面更新：请刷新后重试")
    current["display_name"] = display_name
    current["display_name_updated_at"] = utc_now()
    saved = save_manifest(project_id, current)
    return {"project_id": project_id, "display_name": display_name,
            "updated_at": saved.get("updated_at")}


def set_active_clip(project_id: str, clip_id: str | None) -> dict[str, Any]:
    """设置/清除项目唯一的 active_clip_id（素材卡点击选择，2026-10-21 阶段）。

    硬边界：
    * 只写台账顶层 ``active_clip_id`` / ``active_clip_updated_at`` 两个字段，
      不动 clips/源片/封面/revision/task_history，绝不触发 ASR/分析/渲染；
    * ``clip_id`` 必须是台账已登记的成片（E_CLIP_NOT_REGISTERED），``None`` 表示
      清除选中（回到原片视图），刷新后按该字段恢复；
    * deleting/deleted/delete_failed 拒绝（删除期不可变，E_PROJECT_NOT_MUTABLE）；
      archived 允许（归档只是移出列表）；
    * 幂等：值未变化时直接返回当前态，不递增 generation（避免无意义写盘竞态）。
    """
    project_id = validate_project_id(project_id)
    manifest = load_manifest(project_id)
    status = str(manifest.get("status") or "active")
    if status in {"deleting", "deleted", "delete_failed"}:
        raise ProjectStoreError("E_PROJECT_NOT_MUTABLE", f"项目 {project_id} 当前 {status}：禁止切换选中切片")
    if clip_id is not None:
        clip_id = validate_clip_id(clip_id)
        if clip_id not in manifest["clips"]:
            raise ProjectStoreError("E_CLIP_NOT_REGISTERED",
                                    f"项目 {project_id} 里没有 clip_id={clip_id} 的成片")
    current_value = manifest.get("active_clip_id")
    current_value = str(current_value) if current_value else None
    if current_value == clip_id and manifest.get("active_clip_updated_at"):
        return {"project_id": project_id, "active_clip_id": clip_id,
                "active_clip_updated_at": manifest.get("active_clip_updated_at"),
                "updated_at": manifest.get("updated_at"), "changed": False}
    manifest["active_clip_id"] = clip_id
    stamp = utc_now()
    manifest["active_clip_updated_at"] = stamp
    saved = save_manifest(project_id, manifest)
    return {"project_id": project_id, "active_clip_id": clip_id,
            "active_clip_updated_at": stamp,
            "updated_at": saved.get("updated_at"), "changed": True}


def set_cover_template(project_id: str, clip_id: str, template_id: str | None) -> dict[str, Any]:
    """设置/清除**单条成片**的封面版型选择（2026-10-21 封面版型库阶段）。

    硬边界：
    * 只写该 clip 条目的 ``cover_template`` 三元组（template_id/registry_version/
      updated_at），不动成片文件、封面资产、revision、转写，绝不触发 ASR/分析/渲染
      （真实封面生成是后续阶段，届时从这里读取选择）；
    * ``template_id`` 必须在版本化注册表（cover_templates）中（E_TEMPLATE_UNKNOWN /
      E_TEMPLATE_ID_INVALID），``None`` 表示清除选择；
    * 每条成片**各自持有**自己的选择：改 A 切片绝不触碰 B 切片（不串片）；
    * deleting/deleted/delete_failed 拒绝（E_PROJECT_NOT_MUTABLE）；archived 允许；
    * 幂等：同值直接返回当前态，不递增版本不重写时间戳。
    """
    project_id = validate_project_id(project_id)
    clip_id = validate_clip_id(clip_id)
    if template_id is not None:
        # 延迟导入：注册表模块保持零依赖，避免与 store 形成环。
        try:
            from .cover_templates import validate_template_id
        except ImportError:
            from cover_templates import validate_template_id
        template_id = validate_template_id(template_id)
    manifest = load_manifest(project_id)
    status = str(manifest.get("status") or "active")
    if status in {"deleting", "deleted", "delete_failed"}:
        raise ProjectStoreError("E_PROJECT_NOT_MUTABLE", f"项目 {project_id} 当前 {status}：禁止修改封面版型选择")
    entry = manifest["clips"].get(clip_id)
    if entry is None:
        raise ProjectStoreError("E_CLIP_NOT_REGISTERED",
                                f"项目 {project_id} 里没有 clip_id={clip_id} 的成片")
    current = entry.get("cover_template") if isinstance(entry.get("cover_template"), dict) else None
    current_id = str(current.get("template_id")) if current and current.get("template_id") else None
    if current_id == template_id and current.get("registry_version"):
        return {"project_id": project_id, "clip_id": clip_id,
                "cover_template": current, "changed": False}
    if template_id is None:
        entry.pop("cover_template", None)
    else:
        entry["cover_template"] = {
            "template_id": template_id,
            "registry_version": _cover_template_registry_version(),
            "updated_at": utc_now(),
        }
    save_manifest(project_id, manifest)
    saved = entry.get("cover_template")
    return {"project_id": project_id, "clip_id": clip_id,
            "cover_template": saved if isinstance(saved, dict) else None, "changed": True}


def _cover_template_registry_version() -> str:
    """记录写入时的注册表版本（审计：这个选择是按哪一版版型库做出的）。"""
    try:
        from .cover_templates import REGISTRY_VERSION
    except ImportError:
        from cover_templates import REGISTRY_VERSION
    return REGISTRY_VERSION


def _load_json_file(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def register_source_video(project_id: str, *, path: str | None, sha256: str | None = None) -> dict[str, Any]:
    manifest = load_manifest(project_id)
    if path:
        existing = manifest.get("source_video") or {}
        seed = {"path": str(path), "sha256": sha256 or existing.get("sha256"),
                "registered_at": existing.get("registered_at") or utc_now()}
        if existing.get("path") == str(path) and existing.get("sha256"):
            seed["sha256"] = existing["sha256"]
        absolute = TASKS_ROOT / str(path)
        if absolute.is_file():
            info = absolute.stat()
            seed["bytes"] = info.st_size
            seed["mtime"] = round(info.st_mtime, 3)
            if not seed.get("sha256") and info.st_size <= SOURCE_HASH_MAX_BYTES:
                try:
                    seed["sha256"] = sha256_file(absolute)
                except OSError:
                    pass
        manifest["source_video"] = seed
    return save_manifest(project_id, manifest)


def register_clips(project_id: str, task_id: str,
                   clips: list[dict[str, Any]]) -> dict[str, Any]:
    """把一次 AUTO_EDIT 的产物登记进项目台账（**幂等**：同一 clip_id 只登记一次）。

    已存在的 ``clip_id`` 只刷新展示元数据（title/topic/duration/边界），
    **保留首次的 ``render_task_id``**——这正是「一条 clip 一张卡」的关键：
    之后无论跑多少局部任务，卡片仍指向同一个渲染任务与同一张封面路径。

    规则版本（Phase B 接线点 2）：任务目录里有规则快照时，把
    ``workflow_version`` / ``skill_versions`` / ``workflow_snapshot_path`` 写进
    ``clips.{clip_id}``，并追加一条**按 task_id 去重**的 AUTO_EDIT 任务历史，
    于是「这条成片是哪版规则生成的」在台账里可查。没有快照的旧任务不写版本字段，
    台账形状与接线前完全一致。
    """
    project_id = validate_project_id(project_id)
    manifest = load_manifest(project_id)
    versions = _versions_for_task(task_id)
    for item in clips:
        clip_id = str(item.get("clip_id") or "").strip()
        if not CLIP_ID_PATTERN.fullmatch(clip_id):
            continue
        entry = manifest["clips"].get(clip_id)
        is_new = entry is None
        if is_new:
            entry = {
                "clip_id": clip_id,
                # 名称按**项目内**登记顺序编号，不沿用单个任务里的序号：否则同一项目
                # 跑第二次自动剪辑会再出现一张叫「剪辑成片 01」的卡，用户无法分辨。
                "name": f"剪辑成片 {len(manifest['clips']) + 1:02d}",
                "title": str(item.get("title") or item.get("topic") or ""),
                "topic": str(item.get("topic") or ""),
                "keywords": [str(k) for k in (item.get("keywords") or []) if str(k).strip()],
                "render_task_id": task_id,
                "source_start": float(item.get("source_start") or 0.0),
                "source_end": float(item.get("source_end") or 0.0),
                "duration": float(item.get("duration") or 0.0),
                "render_revision": int(item.get("render_revision") or 1),
                "created_at": utc_now(),
                "task_history": [],
            }
            manifest["clips"][clip_id] = entry
        else:
            # 只刷新可变的展示字段；render_task_id / cover / 历史一律保留。
            for key in ("title", "topic"):
                if item.get(key):
                    entry[key] = str(item[key])
            if item.get("keywords"):
                entry["keywords"] = [str(k) for k in item["keywords"] if str(k).strip()]
            for key in ("source_start", "source_end", "duration"):
                if item.get(key) is not None:
                    entry[key] = float(item[key])
            if not entry.get("name") and item.get("name"):
                entry["name"] = str(item["name"])
        # 版本字段：优先用调用方显式给的（复渲染任务自己带），否则用该任务快照里的。
        explicit = {key: item.get(key) for key in
                    ("workflow_version", "skill_versions", "workflow_snapshot_path")
                    if item.get(key)}
        applied = explicit or versions
        if applied:
            entry.update(applied)
            _record_auto_edit_history(manifest["clips"][clip_id], task_id, applied)
        # 质量闸门与分档标记（C/D/E 项）：随成片一起入台账，卡片与任务详情可读。
        # 旧任务没有这些字段 → 不写，台账形状与接线前一致。
        review = _review_record(item)
        if review:
            entry["review"] = review
        if item.get("cover_style_id"):
            entry["cover_style_id"] = str(item["cover_style_id"])
        _ensure_cover_entry(manifest["clips"][clip_id], item, task_id)
    return save_manifest(project_id, manifest)


def _review_record(item: dict[str, Any]) -> dict[str, Any]:
    """把一条成片的规则闸门/分档标记压成台账里的 review 记录（无标记返回空 dict）。"""
    keys = ("plan_source", "validated", "needs_review", "needs_review_reasons",
            "theme_type", "duration_target", "duration_exception",
            "duration_exception_reason", "gates", "source_segment_ids",
            "hook_not_verbatim", "hook_duplicate",
            "cover_copy_source", "cover_copy_degraded", "cover_style_id")
    record = {key: item[key] for key in keys if item.get(key) is not None and item.get(key) != []}
    if not record:
        return {}
    # 一眼可见的「降级产物」标记：needs_review / duration_exception / copy 降级
    record["degraded"] = bool(item.get("needs_review")
                              or item.get("duration_exception")
                              or item.get("cover_copy_degraded"))
    return record


def _versions_for_task(task_id: str) -> dict[str, Any]:
    """该任务的规则版本字段；任务 id 不合法或没有快照时返回空 dict。

    ``TASKS_ROOT`` 用本模块的（与卡片、台账同一根），避免「快照在一个根、台账在另一个根」。
    """
    try:
        return studio_workflow.versions_for_root(TASKS_ROOT, task_id)
    except studio_workflow.WorkflowError:
        return {}


def _record_auto_edit_history(entry: dict[str, Any], task_id: str,
                              versions: dict[str, Any]) -> None:
    """在 clip 的任务历史里记一条 AUTO_EDIT（按 task_id 去重，幂等重跑不刷屏）。"""
    history = [h for h in (entry.get("task_history") or []) if isinstance(h, dict)]
    history = [h for h in history if str(h.get("task_id")) != task_id]
    stamp = utc_now()
    history.append({
        "task_id": task_id,
        "task_type": "AUTO_EDIT",
        "status": "completed",
        "stage": "成片已登记进项目台账",
        "instruction": None,
        "created_at": stamp,
        "updated_at": stamp,
        "summary": "首次自动剪辑登记" if len(history) == 0 else "按当时的规则版本复渲染/登记",
        "error": None,
        "workflow_version": versions.get("workflow_version"),
        "skill_versions": versions.get("skill_versions") or {},
        "workflow_snapshot_path": versions.get("workflow_snapshot_path"),
    })
    entry["task_history"] = history[-40:]


def _ensure_cover_entry(entry: dict[str, Any], item: dict[str, Any], task_id: str) -> None:
    """首次登记时建立封面资产条目（v1 = AUTO_EDIT 产出的原始 cover.jpg）。"""
    cover_path = str(item.get("cover_path") or "").strip()
    if not cover_path:
        return
    cover = entry.get("cover")
    if isinstance(cover, dict) and cover.get("path"):
        # 已登记过封面：路径不变就不动它（局部任务负责推进 revision）。
        if not cover.get("cover_asset_id"):
            cover["cover_asset_id"] = cover_asset_id(entry["clip_id"], int(cover.get("revision") or 1))
        return
    entry["cover"] = {
        "cover_asset_id": cover_asset_id(entry["clip_id"], 1),
        "revision": 1,
        "style_id": None,
        "path": cover_path,
        "task_id": task_id,
        "sha256": item.get("cover_sha256"),
        "bytes": item.get("cover_bytes"),
        "updated_at": utc_now(),
        "intro_burned": True,
        "kind": "auto_edit",
        "history": [{
            "revision": 1,
            "cover_asset_id": cover_asset_id(entry["clip_id"], 1),
            "path": cover_path,
            "task_id": task_id,
            "style_id": None,
            "kind": "auto_edit",
            "sha256": item.get("cover_sha256"),
            "created_at": utc_now(),
        }],
    }


def cover_asset_id(clip_id: str, revision: int) -> str:
    """封面资产 id：``cover-{clip_id}-v{n}``（卡片、导出、成片详情统一引用它）。"""
    return f"cover-{clip_id}-v{int(revision)}"


# ---------------------------------------------------------------------------
# 从既有任务回填台账（幂等；旧任务无需重跑即可进入「一条 clip 一张卡」）
# ---------------------------------------------------------------------------

def _task_json_paths() -> list[Path]:
    paths: list[Path] = []
    for directory in ("completed", "processing", "in", "failed"):
        paths.extend(sorted((TASKS_ROOT / directory).glob("autoedit_*.json")))
    return paths


def project_tasks(project_id: str) -> list[dict[str, Any]]:
    """该项目名下所有 AUTO_EDIT 任务的任务 JSON（按创建时间升序，先建的先认领 clip）。"""
    project_id = validate_project_id(project_id)
    found: list[dict[str, Any]] = []
    for path in _task_json_paths():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        if str((document.get("bridge") or {}).get("project_id") or "") != project_id:
            continue
        found.append(document)
    found.sort(key=lambda item: str(item.get("created_at") or ""))
    return found


def sync_from_tasks(project_id: str) -> dict[str, Any]:
    """把该项目所有已产出成片的任务回填进台账（**幂等**：已有的 clip_id 不重复登记）。

    读路径上调用它，老项目（改造前完成的任务）也能立即获得稳定 clip_id 与卡片，
    无需重跑一次 3~4 分钟的自动剪辑。
    """
    from .studio_auto_edit_bridge import read_auto_edit_task

    project_id = validate_project_id(project_id)
    manifest = load_manifest(project_id)
    # 已删除/删除中项目**永不回填**（2026-10-20 覆盖范围修复）：墓碑台账只保留审计，
    # 若继续按任务 JSON 回填会把已删除项目原样复活（卡片/源片登记全部回来）。
    if str(manifest.get("status") or "active") in {
            "deleting", "delete_failed", "deleted"}:
        return manifest
    source_registered = False
    for task in project_tasks(project_id):
        task_id = str(task.get("task_id") or "")
        if not task_id:
            continue
        try:
            view = read_auto_edit_task(task_id)
        except (FileNotFoundError, ValueError):
            continue
        clips = view.get("clips") or []
        if not clips:
            continue
        if not source_registered and task.get("raw_video_path"):
            manifest = register_source_video(project_id, path=str(task["raw_video_path"]))
            manifest = load_manifest(project_id)
            source_registered = True
        # 回填时带上封面哈希：卡片要能在台账里自证「当前封面是哪一版」。
        enriched: list[dict[str, Any]] = []
        for clip in clips:
            item = dict(clip)
            cover_rel = str(item.get("cover_path") or "")
            if cover_rel:
                cover_abs = TASKS_ROOT / cover_rel
                if cover_abs.is_file():
                    item.setdefault("cover_sha256", None)
                    item["cover_bytes"] = cover_abs.stat().st_size
            enriched.append(item)
        register_clips(project_id, task_id, enriched)
        manifest = load_manifest(project_id)
    # 台账里首次登记的封面若还没哈希，补一次（保证「导出封面文件 = 该资产」可校验）。
    changed = False
    for entry in manifest["clips"].values():
        cover = entry.get("cover")
        if not isinstance(cover, dict) or cover.get("sha256") or not cover.get("path"):
            continue
        absolute = TASKS_ROOT / str(cover["path"])
        if absolute.is_file():
            cover["sha256"] = sha256_file(absolute)
            cover["bytes"] = absolute.stat().st_size
            for item in cover.get("history") or []:
                if isinstance(item, dict) and not item.get("sha256"):
                    item["sha256"] = cover["sha256"]
            changed = True
    if changed:
        manifest = save_manifest(project_id, manifest)
    return manifest


# ---------------------------------------------------------------------------
# 局部任务推进台账
# ---------------------------------------------------------------------------

def source_upload_id(project_id: str, *, uploads_root: Path | None = None) -> str | None:
    """项目的 source_id：工程 raw/ 目录名（上传原件 uid）优先，回退台账 sha256 前缀。"""
    manifest = load_manifest(project_id)
    root = Path(uploads_root) if uploads_root else Path(get_temp_dir())
    raw_dir = root / "studio-projects" / project_id / "raw"
    if raw_dir.is_dir():
        for f in sorted(raw_dir.iterdir()):
            if f.stem:
                return f.stem
    source = manifest.get("source_video")
    if isinstance(source, dict):
        sha = str(source.get("sha256") or "").strip()
        if sha:
            return sha[:32]
        raw = str(source.get("path") or "")
        parts = Path(raw).parts
        if len(parts) >= 2 and parts[0] == "media":
            return parts[1]
    return None


def list_cover_assets(project_id: str, *, uploads_root: Path | None = None,
                      tasks_root: Path | None = None) -> dict[str, Any]:
    """独立封面素材清单（工作台右侧「封面」板块的唯一事实源，2026-10-20）。

    硬规则：
    * **只从当前台账构建** —— 旧批次封面在结构上不可能出现（重剪原子替换台账后
      旧 clip 条目已不存在，旧任务 JSON 不参与本清单）；
    * **绑定五元组** —— 每项携带 project_id / source_id / clip_id /
      render_task_id / revision（+active_revision / cover_asset_id）；
    * **状态按磁盘实测** —— ready=文件在盘；failed=未生成或文件缺失（带原因），
      绝不用其它切片的封面冒充；
    * **只读** —— 不触发任何 ASR/分析/渲染，不动 clip_final.mp4 内嵌片头封面。
    """
    project_id = validate_project_id(project_id)
    manifest = load_manifest(project_id)
    tasks = Path(tasks_root) if tasks_root else TASKS_ROOT
    source_id = source_upload_id(project_id, uploads_root=uploads_root)
    if not source_id:
        source = manifest.get("source_video")
        if isinstance(source, dict) and source.get("sha256"):
            source_id = str(source["sha256"])[:32]
    items: list[dict[str, Any]] = []
    for clip_id in manifest["clip_order"]:
        entry = manifest["clips"].get(clip_id)
        if not isinstance(entry, dict):
            continue
        card = card_view(entry)
        cover = card["cover"] if isinstance(card.get("cover"), dict) else {}
        rel_path = str(cover.get("path") or "").strip() or None
        absolute = (tasks / rel_path) if rel_path else None
        exists = bool(absolute and absolute.is_file())
        size = absolute.stat().st_size if exists else None
        if exists:
            status, reason = "ready", None
        elif rel_path:
            status, reason = "failed", "封面文件缺失（登记于台账但不在磁盘）"
        else:
            status, reason = "failed", "未生成封面（渲染阶段未产出 cover.jpg）"
        items.append({
            "project_id": project_id,
            "source_id": source_id,
            "clip_id": clip_id,
            "clip_name": str(card.get("title") or card.get("name") or clip_id),
            "render_task_id": card.get("render_task_id"),
            "revision": cover.get("revision") or 0,
            "active_revision": cover.get("active_revision"),
            "cover_asset_id": cover.get("cover_asset_id"),
            "status": status,
            "reason": reason,
            "path": rel_path,
            "bytes": size,
            "sha256": cover.get("sha256") if exists else None,
            "kind": cover.get("kind"),
            "style_id": cover.get("style_id"),
            "updated_at": cover.get("updated_at"),
        })
    ready = sum(1 for i in items if i["status"] == "ready")
    return {"project_id": project_id, "source_id": source_id,
            "batch": {"clip_count": len(items), "ready": ready,
                      "failed": len(items) - ready},
            "items": items}


def entry_for(project_id: str, clip_id: str) -> dict[str, Any]:
    manifest = load_manifest(project_id)
    clip_id = validate_clip_id(clip_id)
    entry = manifest["clips"].get(clip_id)
    if entry is None:
        raise ProjectStoreError("E_CLIP_NOT_REGISTERED",
                                f"项目 {project_id} 里没有 clip_id={clip_id} 的成片")
    return entry


def bump_cover(project_id: str, clip_id: str, *, task_id: str, style_id: str | None,
               cover_path: str, sha256: str | None, bytes_: int | None,
               intro_burned: bool, kind: str, instruction: str | None = None,
               title: str | None = None, hook: str | None = None,
               qa_status: str | None = None) -> dict[str, Any]:
    """封面成功后**原子推进**一条修订：revision+1 并写历史（保留旧版可撤销）。"""
    manifest = load_manifest(project_id)
    clip_id = validate_clip_id(clip_id)
    entry = manifest["clips"].get(clip_id)
    if entry is None:
        raise ProjectStoreError("E_CLIP_NOT_REGISTERED",
                                f"项目 {project_id} 里没有 clip_id={clip_id} 的成片")
    cover = entry.get("cover") if isinstance(entry.get("cover"), dict) else {}
    revision = int(cover.get("revision") or 0) + 1
    history = [h for h in (cover.get("history") or []) if isinstance(h, dict)]
    history.append({
        "revision": revision,
        "cover_asset_id": cover_asset_id(clip_id, revision),
        "path": cover_path,
        "task_id": task_id,
        "style_id": style_id,
        "kind": kind,
        "sha256": sha256,
        "bytes": bytes_,
        "intro_burned": bool(intro_burned),
        "instruction": instruction,
        "created_at": utc_now(),
        "title": title,
        "hook": hook,
        # 封面门禁（文字安全区/限额/QC）在 cover_replace 内硬执行，能落台账即已通过；
        # 显式落 qa_status 供版本管理 UI 与「设为当前」门禁消费。
        "qa_status": qa_status or "passed",
    })
    entry["cover"] = {
        "cover_asset_id": cover_asset_id(clip_id, revision),
        "revision": revision,
        "style_id": style_id,
        "path": cover_path,
        "task_id": cover.get("task_id") or task_id,
        "sha256": sha256,
        "bytes": bytes_,
        "updated_at": utc_now(),
        "intro_burned": bool(intro_burned),
        "kind": kind,
        "history": history[-20:],
    }
    return save_manifest(project_id, manifest)


def set_cover_pointer(project_id: str, clip_id: str, *, revision: int,
                      task_id: str) -> dict[str, Any]:
    """撤销/回滚后把「当前展示的封面」指回历史里已有的某一版（不新增 revision）。"""
    manifest = load_manifest(project_id)
    clip_id = validate_clip_id(clip_id)
    entry = manifest["clips"].get(clip_id)
    if entry is None:
        raise ProjectStoreError("E_CLIP_NOT_REGISTERED",
                                f"项目 {project_id} 里没有 clip_id={clip_id} 的成片")
    cover = entry.get("cover") if isinstance(entry.get("cover"), dict) else {}
    history = [h for h in (cover.get("history") or []) if isinstance(h, dict)]
    target = next((h for h in history if int(h.get("revision") or 0) == int(revision)), None)
    if target is None:
        raise ProjectStoreError("E_COVER_REVISION_UNKNOWN",
                                f"封面历史里没有 v{int(revision)}，无法撤销")
    cover["active_revision"] = int(revision)
    cover["cover_asset_id"] = target.get("cover_asset_id") or cover_asset_id(clip_id, int(revision))
    cover["style_id"] = target.get("style_id")
    cover["path"] = target.get("path")
    cover["sha256"] = target.get("sha256")
    cover["bytes"] = target.get("bytes")
    cover["kind"] = target.get("kind")
    cover["task_id"] = target.get("task_id") or cover.get("task_id") or task_id
    cover["updated_at"] = utc_now()
    entry["cover"] = cover
    return save_manifest(project_id, manifest)


def prune_cover_history(project_id: str, clip_id: str, *, revision: int) -> dict[str, Any]:
    """删除未被当前使用的**历史**封面版本（台账侧）：修剪 cover.history 一条记录。

    - 当前使用中（active_revision，缺省回退 revision 最新值）→ 拒绝 `E_COVER_REVISION_ACTIVE`；
    - 目标版本不在历史里 → `E_COVER_REVISION_UNKNOWN`。
    物理快照（cover_revisions/rev-NNN）由 cover_replace.delete_cover_revision 先删，
    两边都成功才算删掉；任一步失败调用方不得落台账。
    """
    manifest = load_manifest(project_id)
    clip_id = validate_clip_id(clip_id)
    entry = manifest["clips"].get(clip_id)
    if entry is None:
        raise ProjectStoreError("E_CLIP_NOT_REGISTERED",
                                f"项目 {project_id} 里没有 clip_id={clip_id} 的成片")
    cover = entry.get("cover") if isinstance(entry.get("cover"), dict) else {}
    history = [h for h in (cover.get("history") or []) if isinstance(h, dict)]
    target = int(revision)
    active = int(cover.get("active_revision") or cover.get("revision") or 0)
    if target == active:
        raise ProjectStoreError("E_COVER_REVISION_ACTIVE",
                                f"v{target} 是当前使用中的封面，不能删除")
    remaining = [h for h in history if int(h.get("revision") or 0) != target]
    if len(remaining) == len(history):
        raise ProjectStoreError("E_COVER_REVISION_UNKNOWN",
                                f"封面历史里没有 v{target}，无法删除")
    cover["history"] = remaining[-20:]
    entry["cover"] = cover
    return save_manifest(project_id, manifest)


def bump_render_revision(project_id: str, clip_id: str, *, task_id: str,
                         kind: str, instruction: str | None = None,
                         render_task_id: str | None = None) -> dict[str, Any]:
    """画面重渲成功后 render_revision+1（封面更新**不**走这里）。"""
    manifest = load_manifest(project_id)
    clip_id = validate_clip_id(clip_id)
    entry = manifest["clips"].get(clip_id)
    if entry is None:
        raise ProjectStoreError("E_CLIP_NOT_REGISTERED",
                                f"项目 {project_id} 里没有 clip_id={clip_id} 的成片")
    entry["render_revision"] = int(entry.get("render_revision") or 1) + 1
    if render_task_id:
        entry["render_task_id"] = render_task_id
    entry.setdefault("render_history", []).append({
        "render_revision": entry["render_revision"],
        "task_id": task_id, "kind": kind, "instruction": instruction,
        "created_at": utc_now(),
    })
    entry["render_history"] = entry["render_history"][-20:]
    return save_manifest(project_id, manifest)


# ---------------------------------------------------------------------------
# 第四步：封面图片素材进入时间线（image_clip，role=cover_intro）
# ---------------------------------------------------------------------------

TIMELINE_IMAGE_MIN_S = 0.3
TIMELINE_IMAGE_MAX_S = 10.0
# 每条成片时间线上最多放多少个 image_clip 实例（开头 cover_intro 恒 1 个 + 若干片中/片尾）。
TIMELINE_IMAGE_MAX_INSTANCES = 12


def get_timeline_image_clips(project_id: str, clip_id: str) -> list[dict[str, Any]]:
    """读一条成片的时间线 image_clip 实例（纯读；没有就返回空列表）。"""
    manifest = load_manifest(project_id)
    clip_id = validate_clip_id(clip_id)
    entry = manifest["clips"].get(clip_id)
    if entry is None:
        raise ProjectStoreError("E_CLIP_NOT_REGISTERED",
                                f"项目 {project_id} 里没有 clip_id={clip_id} 的成片")
    items = entry.get("timeline_image_clips")
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict)]


def set_timeline_image_clip(project_id: str, clip_id: str, *, duration_s: float,
                            start_s: float = 0.0, instance_id: str | None = None,
                            ) -> dict[str, Any]:
    """upsert 当前封面在时间线上的 image_clip 实例（**只写时间线实例，不动封面素材文件**）。

    - ``start_s == 0``（放在开头）→ ``role="cover_intro"``，**恒只有一个**：重复放会
      替换已有开头实例（保留原 id，前端不需要换 key）；其余位置 ``role="image"``；
    - ``start_s > 0`` 且带 ``instance_id`` → 原位更新该实例（拖拽移动/改时长）；
    - 时长钳制 ``[TIMELINE_IMAGE_MIN_S, TIMELINE_IMAGE_MAX_S]``（当前 0.3–10s），默认 1.5s 由前端给；
      位置钳制 ``[0, 成片时长]``；
    - 素材 = 该成片当前封面（台账 ``cover.path``），封面缺失 → ``E_TIMELINE_IMAGE_NO_ASSET``；
    - 非开头新实例数量上限 ``TIMELINE_IMAGE_MAX_INSTANCES``，超出 → ``E_TIMELINE_IMAGE_LIMIT``。
    """
    import uuid
    manifest = load_manifest(project_id)
    clip_id = validate_clip_id(clip_id)
    entry = manifest["clips"].get(clip_id)
    if entry is None:
        raise ProjectStoreError("E_CLIP_NOT_REGISTERED",
                                f"项目 {project_id} 里没有 clip_id={clip_id} 的成片")
    duration = max(TIMELINE_IMAGE_MIN_S, min(TIMELINE_IMAGE_MAX_S, float(duration_s or 0)))
    start = max(0.0, float(start_s or 0.0))
    content_s = float(entry.get("duration") or 0)
    if content_s > 0:
        start = min(start, content_s)
    cover = entry.get("cover") if isinstance(entry.get("cover"), dict) else {}
    cover_path = str(cover.get("path") or "") or None
    if not cover_path or not (TASKS_ROOT / cover_path).is_file():
        raise ProjectStoreError(
            "E_TIMELINE_IMAGE_NO_ASSET",
            f"该成片还没有可用的封面素材（{TASKS_ROOT / str(cover_path or '')}），无法添加到时间线")
    role = "cover_intro" if start <= 0.001 else "image"
    items = [dict(item) for item in (entry.get("timeline_image_clips") or [])
             if isinstance(item, dict)]
    if role == "cover_intro":
        # 开头实例恒一个：替换（沿用原 id），其余 cover_intro 清掉。
        head = next((item for item in items if item.get("role") == "cover_intro"), None)
        if head is not None:
            items.remove(head)
            item_id = str(head.get("id") or f"img_{uuid.uuid4().hex[:12]}")
        else:
            item_id = f"img_{uuid.uuid4().hex[:12]}"
        items = [item for item in items if item.get("role") != "cover_intro"]
        item = {
            "id": item_id, "role": role, "asset_path": cover_path,
            "start_s": 0.0, "duration_s": round(duration, 3), "updated_at": utc_now(),
        }
        items.insert(0, item)
    else:
        if instance_id:
            target = next((item for item in items if item.get("id") == str(instance_id)), None)
            if target is None:
                raise ProjectStoreError("E_TIMELINE_IMAGE_UNKNOWN",
                                        f"时间线图片实例 {instance_id} 不存在，无法更新")
            target.update({
                "role": role, "asset_path": cover_path,
                "start_s": round(start, 3), "duration_s": round(duration, 3),
                "updated_at": utc_now(),
            })
            item = target
        else:
            if len(items) >= TIMELINE_IMAGE_MAX_INSTANCES:
                raise ProjectStoreError(
                    "E_TIMELINE_IMAGE_LIMIT",
                    f"时间线图片实例最多 {TIMELINE_IMAGE_MAX_INSTANCES} 个，请先删除再添加")
            item = {
                "id": f"img_{uuid.uuid4().hex[:12]}", "role": role,
                "asset_path": cover_path,
                "start_s": round(start, 3), "duration_s": round(duration, 3),
                "updated_at": utc_now(),
            }
            items.append(item)
    entry["timeline_image_clips"] = items
    manifest["updated_at"] = utc_now()
    save_manifest(project_id, manifest)
    return dict(item)


def delete_timeline_image_clip(project_id: str, clip_id: str,
                               instance_id: str | None = None) -> dict[str, Any]:
    """删除时间线 image_clip 实例——**绝不删除封面素材文件**（只清台账里的实例字段）。

    带 ``instance_id`` → 只删那一个（不存在 → ``E_TIMELINE_IMAGE_UNKNOWN``）；
    不带 → 清空该成片全部实例（兼容旧调用与「清空时间线图片」）。
    """
    manifest = load_manifest(project_id)
    clip_id = validate_clip_id(clip_id)
    entry = manifest["clips"].get(clip_id)
    if entry is None:
        raise ProjectStoreError("E_CLIP_NOT_REGISTERED",
                                f"项目 {project_id} 里没有 clip_id={clip_id} 的成片")
    removed = entry.get("timeline_image_clips")
    items = [dict(item) for item in (removed or []) if isinstance(item, dict)]
    if instance_id:
        remaining = [item for item in items if item.get("id") != str(instance_id)]
        if len(remaining) == len(items):
            raise ProjectStoreError("E_TIMELINE_IMAGE_UNKNOWN",
                                    f"时间线图片实例 {instance_id} 不存在，无法删除")
        entry["timeline_image_clips"] = remaining
        removed_count = len(items) - len(remaining)
    else:
        entry["timeline_image_clips"] = []
        removed_count = len(items)
    manifest["updated_at"] = utc_now()
    save_manifest(project_id, manifest)
    return {"project_id": project_id, "clip_id": clip_id, "removed": removed_count}


def update_fields(project_id: str, clip_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """局部任务更新可编辑字段（title/topic/keywords/subtitle_style/timeline/配置段）。

    ``cover_asset_id`` / ``cover_revision`` 是 ``cover`` 的**投影**，只读不写；
    ``render_task_id``/``cover``/``clip_id``/``task_history`` 仍只允许专用函数修改。
    """
    manifest = load_manifest(project_id)
    clip_id = validate_clip_id(clip_id)
    entry = manifest["clips"].get(clip_id)
    if entry is None:
        raise ProjectStoreError("E_CLIP_NOT_REGISTERED",
                                f"项目 {project_id} 里没有 clip_id={clip_id} 的成片")
    for key, value in (fields or {}).items():
        if key in {"render_task_id", "cover", "clip_id", "task_history",
                   "cover_asset_id", "cover_revision"}:
            continue  # 关键结构字段与投影字段只允许专用函数修改
        entry[key] = value
    return save_manifest(project_id, manifest)


def record_task(project_id: str, clip_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """把一次局部任务的**任务历史**追加到该 clip（任务表的事实源）。"""
    manifest = load_manifest(project_id)
    clip_id = validate_clip_id(clip_id)
    entry = manifest["clips"].get(clip_id)
    if entry is None:
        raise ProjectStoreError("E_CLIP_NOT_REGISTERED",
                                f"项目 {project_id} 里没有 clip_id={clip_id} 的成片")
    history = [h for h in (entry.get("task_history") or []) if isinstance(h, dict)]
    task_id = str(record.get("task_id") or "")
    history = [h for h in history if str(h.get("task_id")) != task_id]
    history.append(record)
    entry["task_history"] = history[-40:]
    return save_manifest(project_id, manifest)


def list_cards(project_id: str) -> list[dict[str, Any]]:
    """项目卡片列表：**一条 clip 一张卡**，按 clip_order 稳定排序。"""
    manifest = load_manifest(project_id)
    cards: list[dict[str, Any]] = []
    for clip_id in manifest.get("clip_order") or []:
        entry = manifest["clips"].get(clip_id)
        if isinstance(entry, dict):
            cards.append(card_view(entry))
    return cards


def card_clip_ids(project_id: str) -> list[str]:
    """台账里当前登记的 clip_id（按 clip_order 稳定排序；**纯读**，不落盘不补字段）。

    给「清空项目任务数据」算卡片基数与待删范围用：这里只读 ``clips``，不调用
    ``with_clip_defaults`` —— 清理动作不应该顺带写回任何投影默认值。
    """
    project_id = validate_project_id(project_id)
    manifest = load_manifest(project_id)
    return [str(clip_id) for clip_id in (manifest.get("clip_order") or [])
            if isinstance(manifest["clips"].get(clip_id), dict)]


def card_task_ids(entry: dict[str, Any]) -> list[str]:
    """一张卡片引用过的所有任务 id（首渲任务 + task_history 里追加的局部任务）。

    清空时必须按这些 id 反查落盘文件，否则 ``sync_from_tasks`` 下一次加载会把
    已删卡片**原样复活**（它读的是任务 JSON，不是台账）。
    """
    found: list[str] = []
    for candidate in (entry.get("render_task_id"),):
        text = str(candidate or "").strip()
        if text:
            found.append(text)
    for item in entry.get("task_history") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("task_id") or "").strip()
        if text:
            found.append(text)
    seen: set[str] = set()
    ordered: list[str] = []
    for task_id in found:
        if task_id not in seen:
            seen.add(task_id)
            ordered.append(task_id)
    return ordered


def _resolve_clip_video_path(render_task_id: str, clip_id: str) -> str | None:
    """与编辑、下载共用权威布局；staged缺片仍指向staged，不能播旧同名文件。"""
    if not render_task_id or not clip_id:
        return None
    try:
        try:
            from .studio_clip_locator import clip_dir
        except ImportError:
            from studio_clip_locator import clip_dir
        path = clip_dir(TASKS_ROOT, render_task_id, clip_id, require_video=False) / CLIP_FILENAME
        return str(path.relative_to(TASKS_ROOT.resolve()))
    except (ValueError, OSError):
        return None



def _card_subtitle_data(render_task_id, clip_id):
    try:
        try:
            from .studio_clip_locator import clip_asset
        except ImportError:
            from studio_clip_locator import clip_asset
        path = clip_asset(TASKS_ROOT, render_task_id, clip_id, "warnings.json", required=False)
        if path is None or not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def card_view(entry: dict[str, Any]) -> dict[str, Any]:
    """把台账条目压成前端卡片需要的形状（含封面版本号与可撤销标记）。

    Inspector Phase 1：额外透出 clip 配置段（默认值补齐、封面投影只读），
    既有键的取值逐项保持不变 —— 老 ``clips.json`` 读出来也不会抛错。
    """
    defaults = with_clip_defaults(entry)
    cover = entry.get("cover") if isinstance(entry.get("cover"), dict) else {}
    history = [h for h in (cover.get("history") or []) if isinstance(h, dict)]
    active_revision = int(cover.get("active_revision") or cover.get("revision") or 0)
    clip_id = str(entry.get("clip_id") or "")
    render_task_id = str(entry.get("render_task_id") or "")
    # 成片路径由「稳定渲染任务 + 稳定 clip_id」唯一确定，前端不必再自行推导。
    video_path = _resolve_clip_video_path(render_task_id, clip_id)
    subtitle = _card_subtitle_data(render_task_id, clip_id)
    return {
        "segments": subtitle.get("subtitle_segments") or [],
        "subtitle_provenance": subtitle.get("subtitle_provenance") or {},
        "subtitle_mode": subtitle.get("subtitle_mode") or "unknown",
        "clip_id": entry.get("clip_id"),
        "name": entry.get("name"),
        "title": entry.get("title"),
        "topic": entry.get("topic"),
        "keywords": entry.get("keywords") or [],
        "duration": entry.get("duration"),
        "source_start": entry.get("source_start"),
        "source_end": entry.get("source_end"),
        "task_id": entry.get("render_task_id"),
        "render_task_id": entry.get("render_task_id"),
        "render_revision": int(entry.get("render_revision") or 1),
        "status": "ready",
        "video_path": video_path,
        "cover_path": cover.get("path"),
        # ---- Inspector Phase 1 配置段（默认值补齐；不落盘副本）----
        "transcript_revision": defaults["transcript_revision"],
        "subtitle_config": defaults["subtitle_config"],
        "subtitle_overrides": defaults["subtitle_overrides"],
        "transform": defaults["transform"],
        "audio_tracks": defaults["audio_tracks"],
        "voice_volume": defaults["voice_volume"],
        "muted": defaults["muted"],
        "effects": defaults["effects"],
        # 封面投影：与 cover 结构同源（cover_asset_id / cover_revision 是只读投影）。
        "cover_asset_id": defaults["cover_asset_id"],
        "cover_revision": defaults["cover_revision"],
        "cover": {
            "cover_asset_id": cover.get("cover_asset_id"),
            "revision": int(cover.get("revision") or 0),
            "active_revision": active_revision,
            "style_id": cover.get("style_id"),
            "path": cover.get("path"),
            "sha256": cover.get("sha256"),
            "bytes": cover.get("bytes"),
            "kind": cover.get("kind"),
            "updated_at": cover.get("updated_at"),
            "intro_burned": cover.get("intro_burned"),
            "can_undo": len(history) > 1,
            "undo_revision": 1 if len(history) > 1 else None,
            "history": [
                {"revision": int(h.get("revision") or 0),
                 "cover_asset_id": h.get("cover_asset_id"),
                 "style_id": h.get("style_id"),
                 "kind": h.get("kind"),
                 "sha256": h.get("sha256"),
                 "created_at": h.get("created_at"),
                 "instruction": h.get("instruction")}
                for h in history
            ],
        },
        "subtitle_style": entry.get("subtitle_style"),
        "timeline": entry.get("timeline"),
        "task_history": entry.get("task_history") or [],
        # 质量闸门 / 分档 / 封面文案来源：卡片与任务详情据此显示「这是降级产物」。
        "review": entry.get("review") or {},
        "needs_review": bool((entry.get("review") or {}).get("degraded")),
        "plan_source": (entry.get("review") or {}).get("plan_source"),
        "cover_style_id": entry.get("cover_style_id") or cover.get("style_id"),
        # 封面版型选择（2026-10-21 阶段）：每条成片各自持有 {template_id, registry_version,
        # updated_at}；未选择时为 None。前端版型卡的选中态与刷新恢复都以卡片为事实源。
        "cover_template": entry.get("cover_template") if isinstance(entry.get("cover_template"), dict) else None,
        # 版型封面工作流状态（草稿→应用/删除）：封面在位 + 是否待应用；读不到时给安全缺省。
        "cover_workflow": _cover_workflow_for_card(str(entry.get("render_task_id") or ""), clip_id),
        # 第四步：时间线上的封面 image_clip 实例（role/duration/start），前端时间线块与播放器预览的事实源。
        "timeline_image_clips": [
            dict(item) for item in (entry.get("timeline_image_clips") or [])
            if isinstance(item, dict)
        ],
        # 渲染/导出记录：这条成片是哪版规则、哪份快照产出的
        "render_record": {
            "workflow_version": entry.get("workflow_version"),
            "skill_versions": entry.get("skill_versions") or {},
            "workflow_snapshot_path": entry.get("workflow_snapshot_path"),
            "cover_style_id": entry.get("cover_style_id") or cover.get("style_id"),
            "plan_source": (entry.get("review") or {}).get("plan_source"),
        },
        # 规则版本（Phase B）：卡片原样透出，前端「使用规则版本：workflow vX / cover vX」据此渲染。
        # 旧成片没有快照 → None / {}，前端显示「未知（无规则快照）」而不是编造版本号。
        "workflow_version": entry.get("workflow_version"),
        "skill_versions": entry.get("skill_versions") or {},
        "workflow_snapshot_path": entry.get("workflow_snapshot_path"),
    }



def _cover_workflow_for_card(render_task_id: str, clip_id: str) -> dict[str, Any]:
    """card_view 用：读 clip 目录的版型封面工作流状态（读路径永不抛错）。"""
    try:
        from .cover_replace import cover_template_state_for_card
        from .studio_clip_locator import clip_dir
        directory = clip_dir(TASKS_ROOT, render_task_id, clip_id, require_video=False)
        return cover_template_state_for_card(directory)
    except Exception:
        return {"has_cover": None, "pending": False, "applied": False, "revision": None, "style": None}


def find_project_by_task(task_id: str) -> str | None:
    """按渲染任务 id 反查所属项目（台账 clips.render_task_id 精确匹配）。"""
    root = TASKS_ROOT / "projects"
    if not root.is_dir():
        return None
    for project_dir in sorted(root.iterdir()):
        manifest_file = project_dir / MANIFEST_FILENAME
        if not manifest_file.is_file():
            continue
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for entry in (manifest.get("clips") or {}).values():
            if str(entry.get("render_task_id") or "") == task_id:
                return project_dir.name
    return None


def find_object_key_by_source_path(media_path: str | Path) -> str | None:
    """媒体路径（硬链接）→ 上传源文件 object_key（按 inode 反查单副本）。"""
    try:
        stat_target = Path(media_path).stat()
    except OSError:
        return None
    uploads = BASE_DIR / "temp" / "studio-uploads"
    if not uploads.is_dir():
        return None
    for candidate in uploads.iterdir():
        try:
            stat_candidate = candidate.stat()
        except OSError:
            continue
        if (stat_candidate.st_dev, stat_candidate.st_ino) == (stat_target.st_dev, stat_target.st_ino):
            return f"local:{candidate}"
    return None


def list_projects(include_archived: bool = False) -> list[dict[str, Any]]:
    """列出全部项目（按 updated_at 倒序）。

    默认排除生命周期隐藏态（archived/deleting/delete_failed/deleted）；
    ``include_archived=True`` 时放开 archived（恢复入口用）。
    """
    root = TASKS_ROOT / "projects"
    if not root.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for manifest_file in sorted(root.glob(f"*/{MANIFEST_FILENAME}")):
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # 测试租户（fixture 显式标记）在生产列表服务端排除，不靠名称判断。
        if str(manifest.get("tenant") or "") == TEST_TENANT:
            continue
        # 生命周期状态过滤：archived/deleting/delete_failed/deleted 不进默认列表；
        # include_archived 仅放开 archived（恢复入口），删除相关状态始终排除。
        status = str(manifest.get("status") or "active")
        if status in HIDDEN_PROJECT_STATUSES and not (
                include_archived and status == "archived"):
            continue
        manifest["status"] = status
        clips = manifest.get("clips") or {}
        result.append({
            "project_id": manifest_file.parent.name,
            "card_count": len(clips),
            "updated_at": str(manifest.get("updated_at") or ""),
        })
    result.sort(key=lambda item: item["updated_at"], reverse=True)
    return result
