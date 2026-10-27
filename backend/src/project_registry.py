"""统一项目创建入口（2026-10-19）：唯一、幂等的 ensure_project 服务。

## 背景（孤儿台账事故）

bridge 提交只写任务 JSON；成片台账由 ``sync_from_tasks`` 在**读路径**按需补建——
timeline 工程不存在时也会落 ``clips.json``，于是产生「有台账、无工程」的孤儿项目
（timeline_b677b1639a1c 即实例，后经既有资产重建恢复）。本模块把「项目记录」的
定义与创建收敛到一个状态机入口，供导入 / bridge / 迁移 / 测试四类调用方共用。

## 合法项目（legal）的定义

同时具备：
* timeline 工程  ``<uploads_root>/studio-projects/{id}/project.json``
  （含源片引用、转写快照、EDL）；
* 成片台账      ``<tasks_root>/projects/{id}/clips.json``（初始可为空台账 + 租户标记）。

## 规则

* 已合法 → ``reused``，不重写任何文件（幂等）；
* 需要新建但数据不足（缺源片或转写段）且台账不存在 → ``incomplete``，
  **绝不落 clips.json**（防孤儿不变量）；
* 台账已存在而工程缺失（历史孤儿）→ ``ledger_only``，交由迁移入口显式处理，
  本服务不静默补建；
* ``origin="fixture"`` 强制 ``tenant="test"``，写进台账 ``tenant`` 字段；
  ``list_projects`` 服务端过滤 test 租户——不靠项目名称判断。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from . import studio_project_store as store

TEST_TENANT = "test"
DEFAULT_TENANT = "production"
ORIGINS = frozenset({"import", "bridge", "migration", "fixture", "manual"})

# timeline 工程的创建器：默认走既有 create_timeline_project（ffprobe 实测 +
# 硬链接源片 + 转写快照）；测试注入桩，避免真实 ffmpeg 依赖。
TimelineFactory = Callable[..., dict]


def _default_timeline_factory(*, project_id: str, video_path: Path,
                              raw_segments: list[dict[str, Any]],
                              corrected_segments: list[dict[str, Any]],
                              uploads_root: Path) -> dict:
    """既有创建入口：ffprobe 实测 + 硬链接源片 + project.json + 转写快照。"""
    from .timeline_copilot import create_timeline_project

    return create_timeline_project(
        project_id=project_id, video_path=video_path,
        raw_segments=raw_segments, corrected_segments=corrected_segments,
        uploads_root=uploads_root,
    )


def _uploads(uploads_root: Path | None) -> Path:
    return Path(uploads_root) if uploads_root else Path(get_temp_dir())


def get_temp_dir() -> str:
    from .config import get_config

    return get_config().temp_dir


def timeline_project_path(project_id: str, uploads_root: Path) -> Path:
    return uploads_root / "studio-projects" / project_id / "project.json"


def project_state(project_id: str, *, uploads_root: Path | None = None) -> dict[str, Any]:
    """只读状态盘点：不创建、不修改任何文件。台账侧以 store.TASKS_ROOT 为准（测试注入）。"""
    uploads = _uploads(uploads_root)
    has_timeline = timeline_project_path(project_id, uploads).is_file()
    manifest_path = Path(store.TASKS_ROOT) / "projects" / project_id / "clips.json"
    has_ledger = manifest_path.is_file()
    tenant = None
    if has_ledger:
        try:
            tenant = (json_load(manifest_path) or {}).get("tenant")
        except (OSError, ValueError):
            tenant = None
    return {"project_id": project_id, "has_ledger": has_ledger,
            "has_timeline": has_timeline, "tenant": tenant,
            "missing": [name for name, ok in (("timeline_project", has_timeline),
                                              ("ledger", has_ledger)) if not ok]}


def json_load(path: Path) -> dict[str, Any] | None:
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ensure_project(
    project_id: str,
    *,
    origin: str,
    source_path: Path | None = None,
    raw_segments: list[dict[str, Any]] | None = None,
    corrected_segments: list[dict[str, Any]] | None = None,
    uploads_root: Path,
    owner_subject: str | None = None,
    timeline_factory: TimelineFactory | None = None,
) -> dict[str, Any]:
    """唯一幂等的项目记录入口。返回::

        {"project_id", "state", "created", "missing", "tenant", "detail"}

    * ``state="created"``  本次补齐了缺失侧（timeline / ledger），项目现为合法；
    * ``state="reused"``   项目本已合法，未写任何文件；
    * ``state="ledger_only"`` 台账存在而工程缺失（历史孤儿）：本服务不静默补建，
      调用方应走迁移入口（依据台账区间+转写库重建工程）；
    * ``state="incomplete"`` 输入不足以补齐工程且台账不存在：**未写任何文件**，
      missing 指明缺什么——调用方必须显式失败，不得继续创建台账；
    * ``state="tenant_mismatch"`` 台账租户与来源不一致：拒绝复用/改写，
      走 ACL 与迁移入口处理。

    ``owner_subject`` 必须取自可信来源（认证主体 / job 归属 / DB 记录），
    只作为审计元数据写入台账；本服务不接收、也不从 manifest 读取租户声明。
    """
    if origin not in ORIGINS:
        raise ValueError(f"E_PROJECT_ORIGIN: 未知来源 {origin!r}（允许 {sorted(ORIGINS)}）")
    resolved_tenant = TEST_TENANT if origin == "fixture" else DEFAULT_TENANT
    store.validate_project_id(project_id)
    uploads = Path(uploads_root)
    # 生命周期守卫：删除中/已删除/删除失败的项目不接受新的 ensure（读台账 status）。
    _ledger_file = Path(store.TASKS_ROOT) / "projects" / project_id / "clips.json"
    _existing = None
    if _ledger_file.is_file():
        try:
            _existing = json.loads(_ledger_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _existing = None
    if isinstance(_existing, dict) and str(_existing.get("status") or "active") in {
            "deleting", "delete_failed", "deleted"}:
        return {"project_id": project_id, "state": "not_mutable", "created": [],
                "missing": [], "tenant": resolved_tenant,
                "detail": f"项目状态 {_existing.get('status')}：禁止创建/复用"}

    timeline_path = timeline_project_path(project_id, uploads)
    ledger_path = Path(store.TASKS_ROOT) / "projects" / project_id / "clips.json"
    created: list[str] = []
    missing: list[str] = []

    # 1) timeline 工程：已存在则复用；不存在时用既有创建入口（可注入桩）。
    if not timeline_path.is_file():
        can_build = bool(source_path and Path(source_path).is_file()
                         and corrected_segments is not None)
        if not can_build:
            missing.append("timeline_project")
        else:
            factory = timeline_factory or _default_timeline_factory
            factory(project_id=project_id, video_path=Path(source_path),
                    raw_segments=raw_segments or [],
                    corrected_segments=corrected_segments,
                    uploads_root=uploads)
            created.append("timeline_project")
        has_timeline = timeline_path.is_file()
    else:
        has_timeline = True

    # 2) 台账：已存在则复用；不存在时——只有在工程已就位（或本就允许 fixture）
    #    的情况下才落盘，否则触发防孤儿不变量。
    if ledger_path.is_file():
        existing = json_load(ledger_path) or {}
        existing_tenant = str(existing.get("tenant") or DEFAULT_TENANT)
        if existing_tenant != resolved_tenant:
            return {"project_id": project_id, "state": "tenant_mismatch",
                    "created": [], "missing": [], "tenant": existing_tenant,
                    "detail": f"台账租户 {existing_tenant!r} 与来源 {origin!r}（{resolved_tenant!r}）不一致：拒绝复用，走 ACL/迁移入口"}
    if not ledger_path.is_file():
        if not has_timeline and resolved_tenant != TEST_TENANT:
            missing.append("ledger")
            return {"project_id": project_id, "state": "incomplete",
                    "created": created, "missing": missing,
                    "tenant": resolved_tenant,
                    "detail": "timeline 工程缺失且无源片/转写输入：拒绝创建孤儿台账"}
        manifest = store.load_manifest(project_id)
        manifest["tenant"] = resolved_tenant
        manifest["origin"] = origin
        if owner_subject:
            manifest["owner_subject"] = str(owner_subject)
        store.save_manifest(project_id, manifest)
        created.append("ledger")
        has_ledger = True
    else:
        has_ledger = True

    if created:
        return {"project_id": project_id, "state": "created", "created": created,
                "missing": [], "tenant": resolved_tenant, "detail": None}
    if has_timeline and has_ledger:
        return {"project_id": project_id, "state": "reused", "created": [],
                "missing": [], "tenant": resolved_tenant, "detail": None}
    state = "ledger_only" if has_ledger else "timeline_only"
    return {"project_id": project_id, "state": state, "created": created,
            "missing": missing or ["timeline_project"], "tenant": resolved_tenant,
            "detail": "历史孤儿：工程与台账不齐，走迁移入口显式恢复"}
