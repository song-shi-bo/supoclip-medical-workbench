"""项目生命周期：归档 / 删除预览 / 删除清单 / 幂等清理 / 状态守卫（2026-10-20）。

状态机（台账 manifest 的 ``status`` 字段，缺省 = active）::

    active ⇄ archived
       ↓（确认删除：先持久化 deletion_manifest.json，再异步清理）
    deleting → deleted
       ↓（任一项失败）
    delete_failed →（只重试清理）→ deleting

硬规则：
* **防孤儿镜像**：台账不存在时本模块不做任何删除（删除以合法台账为前提）；
* **清单先行**：任何文件删除前，deletion_manifest.json 必须已落盘；
* **共享安全**：源片/硬链接 nlink>1 时只摘除本项目链接，绝不破坏其它项目；
* **容量后返还**：文件实际删除成功后才清零 ACL 字节（objects.bytes=0）；
* **只删不重跑**：重试仅做清理，绝不触发 ASR/纠错/分析/规划/渲染；
* 所有可变操作入口经 :func:`assert_mutable` 守卫（deleting/delete_failed/deleted 拒绝）。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from . import studio_project_store as store
from .studio_access import ACL_PATH

STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"
STATUS_DELETING = "deleting"
STATUS_DELETE_FAILED = "delete_failed"
STATUS_DELETED = "deleted"
#: 默认项目列表（生产视图）不出现这些状态
HIDDEN_FROM_LIST = {STATUS_ARCHIVED, STATUS_DELETING, STATUS_DELETE_FAILED, STATUS_DELETED}
MUTABLE_STATUSES = {STATUS_ACTIVE, STATUS_ARCHIVED}

DELETION_MANIFEST = "deletion_manifest.json"

# ---------------------------------------------------------------------------
# PG 事实源访问（可注入：单测 monkeypatch 这三个钩子即可，无需真实数据库）
# ---------------------------------------------------------------------------

#: 返回 [{id, object_key, object_size, deleted_at_utc}]；默认走 studio_job_store（best-effort）。
_PG_PROJECT_JOBS: Any = None
#: 返回仍在引用某上传原件的未删除项目 id 列表。
_PG_REFERENCE_PROJECTS: Any = None
#: 打墓碑，返回 job id 列表。
_PG_TOMBSTONE_JOBS: Any = None


def pg_dsn() -> str | None:
    """asyncpg DSN：环境变量优先，其次 backend/.env；``+asyncpg`` 方言前缀剥除。"""
    url = os.environ.get("DATABASE_URL")
    if not url:
        env_file = Path(__file__).resolve().parent.parent / ".env"
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("DATABASE_URL="):
                    url = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except OSError:
            return None
    if not url:
        return None
    return url.replace("+asyncpg", "")


async def _pg_connect():
    """短连接（asyncpg 直连）：**不复用** SQLAlchemy 引擎池——清理脚本按次
    ``asyncio.run`` 换事件循环，池化连接跨循环即失联（2026-10-20 实测隔次失败）。"""
    import asyncpg

    dsn = pg_dsn()
    if not dsn:
        raise OSError("DATABASE_URL 未配置")
    return await asyncpg.connect(dsn, timeout=10)


def _pg_project_jobs(project_id: str) -> list[dict[str, Any]]:
    if _PG_PROJECT_JOBS is not None:
        return list(_PG_PROJECT_JOBS(project_id))

    async def _run() -> list[dict[str, Any]]:
        conn = await _pg_connect()
        try:
            rows = await conn.fetch(
                "SELECT id, object_key, COALESCE(object_size,0) AS object_size, deleted_at_utc "
                "FROM studio_jobs WHERE project_id=$1 ORDER BY created_at", project_id)
            return [dict(r) for r in rows]
        finally:
            await conn.close()

    try:
        import asyncio

        return asyncio.run(_run())
    except Exception:  # noqa: BLE001 — PG 不可用时覆盖面退化为磁盘证据（预览尽力而为）
        return []


def _pg_reference_projects(upload_id: str, exclude: set[str]) -> list[str]:
    if _PG_REFERENCE_PROJECTS is not None:
        return list(_PG_REFERENCE_PROJECTS(upload_id, exclude))

    async def _run() -> list[str]:
        conn = await _pg_connect()
        try:
            rows = await conn.fetch(
                "SELECT DISTINCT project_id FROM studio_jobs "
                "WHERE object_key LIKE $1 AND deleted_at_utc IS NULL", f"%{upload_id}%")
            return sorted(str(r[0]) for r in rows if str(r[0]) not in exclude)
        finally:
            await conn.close()

    try:
        import asyncio

        return asyncio.run(_run())
    except Exception:  # noqa: BLE001
        return []


def _pg_tombstone_jobs(project_id: str, via: str) -> list[str]:
    if _PG_TOMBSTONE_JOBS is not None:
        return list(_PG_TOMBSTONE_JOBS(project_id, via))

    async def _run() -> list[str]:
        conn = await _pg_connect()
        try:
            rows = await conn.fetch(
                "UPDATE studio_jobs SET deleted_at_utc=NOW(), deleted_via=$2 "
                "WHERE project_id=$1 AND deleted_at_utc IS NULL RETURNING id",
                project_id, via[:512])
            ids = [str(r[0]) for r in rows]
        finally:
            await conn.close()
        # 墓碑行的容量预约一并释放（可审计保留，但预约必须归零）
        for job_id in ids:
            try:
                from .studio_access import release_operation

                release_operation(job_id)
            except Exception:  # noqa: BLE001 — 字节本应为 0，失败不阻塞
                pass
        return ids

    try:
        import asyncio

        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 — 墓碑失败必须可见（项失败可重试）
        raise OSError(f"pg_tombstone: {exc}") from exc


class LifecycleError(ValueError):
    """带机器码的生命周期错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _manifest_file(tasks_root: Path, project_id: str) -> Path:
    return Path(tasks_root) / "projects" / project_id / "clips.json"


def read_status(project_id: str, *, tasks_root: Path | None = None) -> str:
    root = Path(tasks_root) if tasks_root else store.TASKS_ROOT
    data = _load_json(_manifest_file(root, project_id))
    if data is not None:
        return str(data.get("status") or STATUS_ACTIVE)
    # 台账已按清单删除：以删除清单状态为权威口径（deleting/delete_failed/deleted）。
    dm = _load_json(root / "projects" / project_id / DELETION_MANIFEST)
    if dm:
        return {"running": STATUS_DELETING, "pending": STATUS_DELETING,
                "failed": STATUS_DELETE_FAILED,
                "completed": STATUS_DELETED}.get(str(dm.get("status")), STATUS_DELETED)
    return STATUS_ACTIVE


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def set_status(project_id: str, status: str, *, tasks_root: Path | None = None,
               expected: set[str] | None = None) -> dict[str, Any]:
    """翻转台账状态（保留全部既有数据）；expected 给定时做乐观校验。

    台账已随清单删除（delete_failed/deleted 收尾）时，写最小状态记录维持口径。"""
    path = _manifest_file(Path(tasks_root) if tasks_root else store.TASKS_ROOT, project_id)
    manifest = _load_json(path)
    if manifest is None:
        root = Path(tasks_root) if tasks_root else store.TASKS_ROOT
        if (root / "projects" / project_id / DELETION_MANIFEST).is_file():
            manifest = {"project_id": project_id, "status": status}
        else:
            raise LifecycleError("E_PROJECT_NOT_FOUND", f"项目台账不存在: {project_id}")
    current = str(manifest.get("status") or STATUS_ACTIVE)
    if expected is not None and current not in expected:
        raise LifecycleError("E_PROJECT_STATUS_CONFLICT",
                             f"状态冲突: 当前 {current}，期望 {'/'.join(sorted(expected))}")
    manifest["status"] = status
    manifest["status_changed_at"] = _now()
    _write_json_atomic(path, manifest)
    return manifest


def assert_mutable(project_id: str, *, tasks_root: Path | None = None) -> str:
    """变更型操作（上传/自动剪辑/局部任务/重剪/渲染）的前置守卫。"""
    status = read_status(project_id, tasks_root=tasks_root)
    if status in {STATUS_DELETING, STATUS_DELETE_FAILED, STATUS_DELETED}:
        raise LifecycleError("E_PROJECT_NOT_MUTABLE",
                             f"项目 {project_id} 当前 {status}：禁止上传/重剪/渲染/局部任务")
    return status


# ---------------------------------------------------------------------------
# 归档 / 恢复（文件与容量不动，仅列表可见性）
# ---------------------------------------------------------------------------

def archive_project(project_id: str, *, tasks_root: Path | None = None) -> dict[str, Any]:
    manifest = set_status(project_id, STATUS_ARCHIVED, tasks_root=tasks_root,
                          expected={STATUS_ACTIVE})
    return {"project_id": project_id, "status": manifest["status"]}


def restore_project(project_id: str, *, tasks_root: Path | None = None) -> dict[str, Any]:
    manifest = set_status(project_id, STATUS_ACTIVE, tasks_root=tasks_root,
                          expected={STATUS_ARCHIVED})
    return {"project_id": project_id, "status": manifest["status"]}


# ---------------------------------------------------------------------------
# 删除预览 / 删除清单 / 幂等清理
# ---------------------------------------------------------------------------

def _dir_size(path: Path) -> int:
    total = 0
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    return total


def _add_item(items: list[dict[str, Any]], seen: set[str], kind: str, path: Path,
              note: str = "") -> None:
    """登记删除项。目录递归求和，**硬链接(nlink>1)文件的单边字节计入 shared_bytes**
    而不计入 reclaim_bytes——摘除本项目链接即可，数据仍被其它引用持有。"""
    key = str(path.resolve())
    if key in seen or not path.exists():
        return
    seen.add(key)
    st = path.stat()
    if path.is_file():
        total = st.st_size
        shared_flag = st.st_nlink > 1
        reclaim = 0 if shared_flag else total
        shared = total if shared_flag else 0
        nlink = st.st_nlink
    else:
        total = reclaim = shared = 0
        shared_flag = False
        nlink = None
        for item in path.rglob("*"):
            try:
                if not item.is_file():
                    continue
                st_i = item.stat()
                total += st_i.st_size
                if st_i.st_nlink > 1:
                    shared += st_i.st_size
                    shared_flag = True
                else:
                    reclaim += st_i.st_size
            except OSError:
                continue
    items.append({"kind": kind, "path": str(path), "bytes": total,
                  "reclaim_bytes": reclaim, "shared_bytes": shared,
                  "shared": shared_flag,
                  "nlink": nlink, "note": note})


def build_delete_preview(project_id: str, *, uploads_root: Path | None = None,
                         tasks_root: Path | None = None) -> dict[str, Any]:
    """删除前只读预览：逐类列出将删除的文件、字节数与共享资产（不动任何文件）。"""
    uploads = Path(uploads_root) if uploads_root else Path(get_temp_dir())
    tasks = Path(tasks_root) if tasks_root else Path(store.TASKS_ROOT)
    items = _coverage_items(project_id, uploads, tasks)
    reclaim = sum(i["reclaim_bytes"] for i in items)
    shared = sum(i["shared_bytes"] for i in items)
    return {"project_id": project_id, "items": items,
            "reclaim_bytes": reclaim, "shared_bytes": shared,
            "note": "shared=True 的文件为硬链接共享，仅摘除本项目链接，不影响其它项目；"
                    "source_upload/job_recovery 带引用检查，被其它保留项目引用时只保留不删"}


def _project_task_ids(project_id: str, tasks: Path, pg_jobs: list[dict[str, Any]]) -> list[str]:
    """项目名下任务 id：磁盘证据（台账+任务 JSON）∪ PG studio_jobs 行。"""
    ordered: list[str] = []
    try:
        from .studio_project_purge import collect_task_ids

        ordered.extend(collect_task_ids(project_id))
    except Exception:  # noqa: BLE001 — 墓碑台账无 clips 时磁盘证据为空，PG 兜底
        pass
    for row in pg_jobs:
        tid = str(row.get("id") or "")
        if tid and tid not in ordered:
            ordered.append(tid)
    return ordered


def _project_upload_ids(project_id: str, uploads: Path, pg_jobs: list[dict[str, Any]]) -> list[str]:
    """项目源片上传原件 id：PG object_key ∪ 工程 raw/ 目录名。"""
    ordered: list[str] = []
    for row in pg_jobs:
        key = str(row.get("object_key") or "")
        name = key.rsplit("/", 1)[-1]
        if name.endswith(".mp4"):
            name = name[:-4]
        if name and name not in ordered:
            ordered.append(name)
    raw_dir = uploads / "studio-projects" / project_id / "raw"
    if raw_dir.is_dir():
        for f in sorted(raw_dir.iterdir()):
            if f.stem and f.stem not in ordered:
                ordered.append(f.stem)
    return ordered


def _upload_owner_evidence(upload_id: str, project_id: str,
                           pg_jobs: list[dict[str, Any]], uploads: Path) -> list[str]:
    """owner 证据：该上传原件确实属于本项目（缺失则绝不纳入删除）。"""
    evidence: list[str] = []
    for row in pg_jobs:
        if upload_id in str(row.get("object_key") or ""):
            evidence.append(f"pg-job:{row.get('id')}")
    if (uploads / "studio-projects" / project_id / "raw" / f"{upload_id}.mp4").exists():
        evidence.append("project-raw")
    return evidence


def _source_reference_blockers(upload_id: str, project_id: str, *, exclude: set[str],
                               uploads: Path, tasks: Path,
                               pg_jobs: list[dict[str, Any]] | None = None) -> list[str]:
    """保留项目引用检查：任何**非删除集**项目仍引用该源片 → 阻止删除（共享实体不误删）。"""
    blockers: list[str] = []
    skip = set(exclude) | {project_id}
    raw_root = uploads / "studio-projects"
    if raw_root.is_dir():
        for d in sorted(raw_root.iterdir()):
            if d.name in skip:
                continue
            if (d / "raw" / f"{upload_id}.mp4").exists():
                blockers.append(f"project-dir:{d.name}")
    led_root = tasks / "projects"
    if led_root.is_dir():
        for f in sorted(led_root.glob("*/clips.json")):
            other = f.parent.name
            if other in skip:
                continue
            doc = _load_json(f)
            if not doc or str(doc.get("status") or STATUS_ACTIVE) in HIDDEN_FROM_LIST:
                continue  # 墓碑/隐藏台账无源片引用语义
            src = doc.get("source_video")
            if isinstance(src, dict) and upload_id in (
                    f"{src.get('path') or ''}{src.get('sha256') or ''}"):
                blockers.append(f"ledger:{other}")
    for pid in _pg_reference_projects(upload_id, skip):
        blockers.append(f"pg:{pid}")
    return blockers


def _coverage_items(project_id: str, uploads: Path, tasks: Path) -> list[dict[str, Any]]:
    """删除清单全量覆盖（2026-10-20 修复）：在原十类之上补 plans / studio-jobs 工作目录 /
    studio-uploads 源片原件 / job-recovery / PG studio_jobs 墓碑，五类此前遗漏的残留。"""
    from .studio_project_purge import _task_record_paths, _task_side_paths

    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(kind: str, path: Path, note: str = "") -> None:
        _add_item(items, seen, kind, path, note)

    add("timeline_project", uploads / "studio-projects" / project_id,
        "含 project.json/raw/edit/preview 代理缓存")
    add("ledger", tasks / "projects" / project_id / "clips.json")
    add("deletion_manifest", tasks / "projects" / project_id / DELETION_MANIFEST)
    pg_jobs = _pg_project_jobs(project_id)
    task_ids = _project_task_ids(project_id, tasks, pg_jobs)
    artifacts_root = Path(__file__).resolve().parent.parent / "studio-artifacts"
    for tid in task_ids:
        for p in _task_record_paths(tid):
            add("task_record", p)
        for p in _task_side_paths(tid):
            add("task_side", p)
        add("task_media", tasks / "media" / tid)
        add("task_output", tasks / "output" / tid)
        add("published_artifacts", artifacts_root / tid, "已发布成片/字幕副本")
        add("clip_plan", tasks / "plans" / tid, "切片规划（tasks/plans/，此前未覆盖）")
        add("studio_job_dir", uploads / "studio-jobs" / tid,
            "任务工作目录（含硬链接源片副本，此前未覆盖）")
    # 转写：tasks/transcripts/*/v*.json 中 source_project_id 指向本项目的目录
    transcripts_dir = tasks / "transcripts"
    if transcripts_dir.is_dir():
        for tdir in transcripts_dir.iterdir():
            matched = False
            for tfile in sorted(tdir.glob("v*.json")):
                doc = _load_json(tfile)
                if doc and str(doc.get("source_project_id") or "") == project_id:
                    matched = True
                    break
            if matched:
                add("transcripts", tdir)
    add("thumbnails", uploads / "studio-thumbnails" / f"{project_id}.jpg")
    # 源片上传原件与恢复目录：owner 证据 + 引用检查（被其它保留项目引用 → 只保留不删）
    for uid in _project_upload_ids(project_id, uploads, pg_jobs):
        evidence = _upload_owner_evidence(uid, project_id, pg_jobs, uploads)
        if not evidence:
            continue  # owner 证据不足：不纳入本项目的删除清单（防误删他人源片）
        blockers = _source_reference_blockers(uid, project_id, exclude=set(),
                                              uploads=uploads, tasks=tasks, pg_jobs=pg_jobs)
        note = ("owner=" + ",".join(evidence) + "; " +
                ("引用检查通过" if not blockers else "保留：被引用 " + ",".join(blockers)))
        add("source_upload", uploads / "studio-uploads" / f"{uid}.mp4", note)
        add("job_recovery", tasks / "job-recovery" / uid, note)
    # PG studio_jobs：可审计墓碑（行保留，打 deleted_at_utc/deleted_via；不计容量返还）
    pending_rows = [r for r in pg_jobs if not r.get("deleted_at_utc")]
    if pending_rows:
        marker = f"pg://studio_jobs/project/{project_id}"
        if marker not in seen:
            seen.add(marker)
            items.append({"kind": "pg_jobs_tombstone", "path": marker, "bytes": 0,
                          "reclaim_bytes": 0, "shared_bytes": 0, "shared": False,
                          "nlink": None,
                          "note": f"PG studio_jobs 墓碑化 {len(pending_rows)} 行（可审计保留）"})
    return items


def _freed_bytes(path: Path) -> int:
    """删除后将**真实释放**的字节：硬链接(nlink>1)文件数据仍被其它链接持有 → 计 0。"""
    try:
        st = path.stat()
    except OSError:
        return 0
    if path.is_file():
        return st.st_size if st.st_nlink <= 1 else 0
    total = 0
    for item in path.rglob("*"):
        try:
            if not item.is_file():
                continue
            s = item.stat()
            if s.st_nlink <= 1:
                total += s.st_size
        except OSError:
            continue
    return total


def confirm_deletion(project_id: str, *, confirmed_by: str,
                     uploads_root: Path | None = None,
                     tasks_root: Path | None = None) -> dict[str, Any]:
    """用户确认后：先持久化 deletion_manifest（pending），再置 status=deleting。

    清理任务由调用方异步启动；本函数自身不删任何文件。
    """
    uploads = Path(uploads_root) if uploads_root else Path(get_temp_dir())
    tasks = Path(tasks_root) if tasks_root else Path(store.TASKS_ROOT)
    preview = build_delete_preview(project_id, uploads_root=uploads, tasks_root=tasks)
    manifest = {
        "schema_version": 1,
        "project_id": project_id,
        "requested_at_utc": _now(),
        "confirmed_by": confirmed_by,
        "status": "pending",
        "reclaim_bytes": preview["reclaim_bytes"],
        "shared_bytes": preview["shared_bytes"],
        "items": [{**item, "state": "pending", "error": None} for item in preview["items"]],
    }
    _write_json_atomic(tasks / "projects" / project_id / DELETION_MANIFEST, manifest)
    set_status(project_id, STATUS_DELETING, tasks_root=tasks,
               expected={STATUS_ACTIVE, STATUS_ARCHIVED, STATUS_DELETE_FAILED})
    return manifest


def _delete_item(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def run_deletion_task(project_id: str, *, uploads_root: Path | None = None,
                      tasks_root: Path | None = None,
                      acl_path: Path | None = None,
                      fail_paths: set[str] | None = None,
                      co_deleted: set[str] | frozenset[str] | None = None,
                      allow_completed: bool = False) -> dict[str, Any]:
    """幂等清理执行器：逐项处理清单，done 跳过、失败/待处理重试。

    ``fail_paths`` 仅供测试注入失败路径（模拟删除失败 → delete_failed）。
    ``co_deleted``：同批删除的项目集合（引用检查时排除，批内互引不算保留引用）。
    """
    uploads = Path(uploads_root) if uploads_root else Path(get_temp_dir())
    tasks = Path(tasks_root) if tasks_root else Path(store.TASKS_ROOT)
    mpath = tasks / "projects" / project_id / DELETION_MANIFEST
    manifest = _load_json(mpath)
    if manifest is None:
        raise LifecycleError("E_DELETE_MANIFEST_MISSING", f"删除清单不存在: {project_id}")
    if manifest.get("status") == "completed" and not allow_completed:
        return manifest
    manifest["status"] = "running"
    manifest["started_at_utc"] = manifest.get("started_at_utc") or _now()
    _write_json_atomic(mpath, manifest)
    fail_paths = fail_paths or set()
    co = frozenset(co_deleted or ()) | {project_id}
    any_failed = False
    # 覆盖补扫（2026-10-20）：确认/上一轮可能遗漏的全部类目，存在且未知即追加为待删项。
    # 老清单（schema v1，缺 plans/studio-jobs/uploads/job-recovery/PG 墓碑）由此升级。
    try:
        known = {str(Path(i["path"])) for i in manifest["items"]}
        known_resolved = set()
        for p in list(known):
            try:
                known_resolved.add(str(Path(p).resolve()))
            except OSError:
                continue
        for cov in _coverage_items(project_id, uploads, tasks):
            p = str(cov["path"])
            if p in known:
                continue
            try:
                if str(Path(p).resolve()) in known_resolved:
                    continue
            except OSError:
                pass
            manifest["items"].append({**cov, "state": "pending", "error": None})
            known.add(p)
        _write_json_atomic(mpath, manifest)
    except Exception:  # noqa: BLE001 — 补扫失败不阻塞主流程
        pass
    ref_kinds = {"source_upload", "job_recovery"}
    for item in manifest["items"]:
        if item.get("state") in {"done", "kept"}:
            continue
        kind = str(item.get("kind") or "")
        try:
            if kind == "pg_jobs_tombstone":
                via = f"deletion_manifest:{mpath}"
                tombstoned = _pg_tombstone_jobs(project_id, via)
                item["state"] = "done"
                item["error"] = None
                item["freed_bytes"] = 0
                item["note"] = (item.get("note") or "") + f"; tombstoned={len(tombstoned)}"
                _write_json_atomic(mpath, manifest)
                continue
            path = Path(item["path"])
            if str(path) in fail_paths:
                raise OSError("injected failure")
            if path.exists():
                if kind in ref_kinds:
                    uid = str(item.get("upload_id") or path.stem)
                    blockers = _source_reference_blockers(
                        uid, project_id, exclude=co, uploads=uploads, tasks=tasks)
                    if blockers:
                        # 共享实体：只保留不删、不计容量返还（不错误返还字节）。
                        item["state"] = "kept"
                        item["error"] = None
                        item["freed_bytes"] = 0
                        item["note"] = (item.get("note") or "") + f"; 保留（仍被引用）{','.join(blockers)}"
                        _write_json_atomic(mpath, manifest)
                        continue
                freed = _freed_bytes(path)
                _delete_item(path)
                item["state"] = "done"
                item["error"] = None
                item["freed_bytes"] = freed
            else:
                item["state"] = "done"
                item["error"] = None
                item.setdefault("freed_bytes", 0)
        except Exception as exc:  # noqa: BLE001 — 逐项容错，失败继续其余项
            item["state"] = "failed"
            item["error"] = f"{type(exc).__name__}: {exc}"[:200]
            any_failed = True
        _write_json_atomic(mpath, manifest)  # 每项落盘：进度可查、断点可续
    # 容量返还：本项目 ACL 对象字节清零（文件确已删除后才执行）
    if not any_failed:
        try:
            conn = sqlite3.connect(str(acl_path or ACL_PATH), timeout=10)
            try:
                conn.execute("UPDATE objects SET bytes=0 WHERE project_id=?", (project_id,))
                # 项目自身的 objects 行 project_id 为空串，需按 object_id 单独清零
                conn.execute("UPDATE objects SET bytes=0 WHERE kind='project' AND object_id=?",
                             (project_id,))
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 — 记为失败项，可重试
            any_failed = True
            msg = f"capacity_release: {exc}"
            errs = manifest.setdefault("errors", [])
            if not errs or errs[-1] != msg:
                errs.append(msg)
            _write_json_atomic(mpath, manifest)
    # 释放字节对账（2026-10-20）：freed 只按删除瞬间 nlink==1 的真实释放计，
    # 共享/保留项为 0 —— manifest 合计 == 实际磁盘释放。
    manifest["freed_bytes_total"] = sum(int(i.get("freed_bytes") or 0)
                                        for i in manifest["items"] if i.get("state") == "done")
    manifest["kept_items"] = [i.get("path") for i in manifest["items"] if i.get("state") == "kept"]
    manifest["status"] = "failed" if any_failed else "completed"
    manifest["finished_at_utc"] = _now()
    _write_json_atomic(mpath, manifest)
    if any_failed:
        # 台账可能已按清单删除（ledger 项先行）——不设 expected 前置。
        set_status(project_id, STATUS_DELETE_FAILED, tasks_root=tasks)
    else:
        # 台账写成墓碑：数据已删，仅保留审计记录；列表按状态排除。
        tomb = {"project_id": project_id, "status": STATUS_DELETED,
                "deleted_at_utc": _now(),
                "deletion_manifest": DELETION_MANIFEST}
        _write_json_atomic(tasks / "projects" / project_id / "clips.json", tomb)
        set_status(project_id, STATUS_DELETED, tasks_root=tasks)
    return manifest


def retry_cleanup(project_id: str, *, co_deleted: set[str] | frozenset[str] | None = None,
                  uploads_root: Path | None = None, tasks_root: Path | None = None,
                  acl_path: Path | None = None) -> dict[str, Any]:
    """对**已删除/删除失败**项目重新补扫全部覆盖类目并继续清理（2026-10-20）。

    仅做清理，绝不触发 ASR/纠错/分析/规划/渲染；绝不触碰清单外路径；
    共享引用实体（源片原件等）只保留不删、不计容量。completed 的老清单由此
    升级到全量覆盖（plans/studio-jobs/studio-uploads/job-recovery/PG 墓碑）。
    """
    tasks = Path(tasks_root) if tasks_root else Path(store.TASKS_ROOT)
    if not (tasks / "projects" / project_id / DELETION_MANIFEST).is_file():
        raise LifecycleError("E_DELETE_MANIFEST_MISSING", f"删除清单不存在: {project_id}")
    status = read_status(project_id, tasks_root=tasks)
    if status not in {STATUS_DELETED, STATUS_DELETE_FAILED}:
        raise LifecycleError("E_DELETE_RETRY_INVALID",
                             f"仅已删除（deleted）或 delete_failed 支持清理重试，当前 {status}")
    return run_deletion_task(project_id, uploads_root=uploads_root, tasks_root=tasks,
                             acl_path=acl_path,
                             co_deleted=frozenset(co_deleted or ()) | {project_id},
                             allow_completed=True)


def retry_deletion(project_id: str, *, uploads_root: Path | None = None,
                   tasks_root: Path | None = None,
                   acl_path: Path | None = None) -> dict[str, Any]:
    """只重试清理（delete_failed → deleting → 继续清单），绝不重跑分析/渲染。"""
    if read_status(project_id, tasks_root=tasks_root) != STATUS_DELETE_FAILED:
        raise LifecycleError("E_DELETE_RETRY_INVALID", "仅 delete_failed 状态支持重试清理")
    return run_deletion_task(project_id, uploads_root=uploads_root, tasks_root=tasks_root,
                             acl_path=acl_path)


def get_temp_dir() -> str:
    from .config import get_config

    return get_config().temp_dir


def read_deletion_status(project_id: str, *, tasks_root: Path | None = None) -> dict[str, Any]:
    """删除进度（只读）：清单状态 + 逐项 state/error + 项目生命周期状态。"""
    tasks = Path(tasks_root) if tasks_root else Path(store.TASKS_ROOT)
    manifest = _load_json(tasks / "projects" / project_id / DELETION_MANIFEST) or {}
    items = [{"kind": i.get("kind"), "path": i.get("path"), "bytes": i.get("bytes"),
              "shared": i.get("shared"), "state": i.get("state"), "error": i.get("error")}
             for i in (manifest.get("items") or [])]
    done = sum(1 for i in items if i.get("state") == "done")
    failed = sum(1 for i in items if i.get("state") == "failed")
    return {"project_id": project_id,
            "project_status": read_status(project_id, tasks_root=tasks),
            "manifest_status": manifest.get("status"),
            "items_total": len(items), "items_done": done, "items_failed": failed,
            "items": items,
            "reclaim_bytes": manifest.get("reclaim_bytes"),
            "freed_bytes_total": manifest.get("freed_bytes_total"),
            "kept_items": manifest.get("kept_items") or [],
            "errors": manifest.get("errors") or []}


def assert_owner(project_id: str, subject: str, *, acl_path: Path | None = None) -> None:
    """仅项目所有者可执行归档/恢复/删除/重试（subject 来自认证主体，非客户端）。"""
    import sqlite3

    from .studio_access import ACL_PATH

    conn = sqlite3.connect(str(acl_path or ACL_PATH), timeout=10)
    try:
        row = conn.execute("SELECT owner FROM objects WHERE kind='project' AND object_id=?",
                           (project_id,)).fetchone()
    finally:
        conn.close()
    if row is None or str(row[0]) != str(subject):
        raise LifecycleError("E_PROJECT_OWNER_REQUIRED", "仅项目所有者可执行该操作")
