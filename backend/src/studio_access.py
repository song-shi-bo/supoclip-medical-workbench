"""3445 工作台可信身份与持久对象权限。客户端身份头不参与授权。"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from contextlib import contextmanager
import json
import os
import re
from pathlib import Path
import sqlite3
import threading
import uuid
import hashlib
import time
from typing import Any

from fastapi import HTTPException, Request, APIRouter
from starlette.responses import JSONResponse

DOMAIN_ID = "bdd01c9e-1ace-47b1-8288-bee275fa4ad0"
AUTH_SOCKET = os.environ.get("STUDIO_AUTH_SOCKET", "/profile/.studio-auth/auth.sock")
ACL_PATH = os.environ.get("STUDIO_ACL_PATH", "/domain/.studio-resources/access/access.sqlite3")
_principal: ContextVar[dict | None] = ContextVar("studio_principal", default=None)
_access: ContextVar[Any] = ContextVar("studio_access_ledger", default=None)
KINDS = frozenset({"project", "job", "upload", "source", "task", "artifact"})


def _identifier(value: Any) -> str:
    value = str(value or "")
    if not value or len(value) > 4096 or "\x00" in value:
        raise HTTPException(400, "invalid_object_identifier")
    return value


def current_principal(request: Request | None = None) -> dict:
    principal = getattr(request.state, "studio_principal", None) if request else _principal.get()
    if not principal or principal.get("domain_id") != DOMAIN_ID:
        raise HTTPException(401, "studio_auth_required")
    if int(principal.get("expires_at_ms", 0)) <= int(time.time() * 1000):
        raise HTTPException(401, "studio_session_expired")
    return principal


async def query_principal(cookie: str, method: str, origin: str = "", *, socket_path: str | None = None) -> dict:
    """仅将当前请求的 Cookie 在内存中交专用认证服务，不保存或记录。"""
    if not cookie:
        raise HTTPException(401, "studio_auth_required")
    if any("\r" in value or "\n" in value for value in (cookie, method, origin)):
        raise HTTPException(400, "invalid_auth_request")
    writer = None
    try:
        # 调用时读取 env（与既有 STUDIO_AUTH_SOCKET 配置项一致；默认值不变）
        socket_path = socket_path or os.environ.get("STUDIO_AUTH_SOCKET", AUTH_SOCKET)
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(socket_path), 3)
        payload = ("POST /principal HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
                   f"Cookie: {cookie}\r\nX-Forwarded-Method: {method.upper()}\r\n"
                   f"X-Video-Origin: {origin}\r\nContent-Length: 0\r\n\r\n").encode("latin1")
        writer.write(payload)
        await asyncio.wait_for(writer.drain(), 3)
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
        if len(header) > 16384:
            raise ValueError("oversized auth response")
        status = int(header.split(b" ", 2)[1])
        if status in (401, 403):
            raise HTTPException(status, "studio_auth_rejected")
        if status != 200:
            raise ValueError("auth unavailable")
        headers = dict(line.split(b":", 1) for line in header.split(b"\r\n")[1:] if b":" in line)
        length = int(next((v.strip() for k, v in headers.items() if k.lower() == b"content-length"), b"0"))
        if not 0 < length <= 16384:
            raise ValueError("invalid auth response length")
        data = json.loads(await asyncio.wait_for(reader.readexactly(length), 3))
        principal = {key: data[key] for key in ("subject", "slug", "domain_id", "expires_at_ms")}
        if not isinstance(principal["subject"], str) or not principal["subject"] or principal["domain_id"] != DOMAIN_ID:
            raise ValueError("invalid auth principal")
        if int(principal["expires_at_ms"]) <= int(time.time() * 1000):
            raise HTTPException(401, "studio_session_expired")
        return principal
    except HTTPException:
        raise
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        raise HTTPException(503, "studio_auth_unavailable") from None
    finally:
        if writer:
            writer.close()


class AccessLedger:
    """域内 SQLite ACL；事务保护认领、配额与撤销，无首次访问认领。"""
    def __init__(self, path: str | Path = ACL_PATH):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=10)
        os.chmod(self.path, 0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS objects(
          domain TEXT NOT NULL,kind TEXT NOT NULL,object_id TEXT NOT NULL,
          owner TEXT NOT NULL,project_id TEXT NOT NULL DEFAULT '',bytes INTEGER NOT NULL DEFAULT 0,
          created_ms INTEGER NOT NULL,provenance TEXT NOT NULL,
          PRIMARY KEY(domain,kind,object_id));
        CREATE TABLE IF NOT EXISTS grants(
          domain TEXT NOT NULL,kind TEXT NOT NULL,object_id TEXT NOT NULL,subject TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('reader','editor')),revoked INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(domain,kind,object_id,subject));
        CREATE TABLE IF NOT EXISTS quotas(
          domain TEXT NOT NULL,subject TEXT NOT NULL,bytes_limit INTEGER NOT NULL,
          concurrent_limit INTEGER NOT NULL,PRIMARY KEY(domain,subject));
        CREATE TABLE IF NOT EXISTS reservations(
          domain TEXT NOT NULL,operation_id TEXT NOT NULL,subject TEXT NOT NULL,
          bytes INTEGER NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 1,
          expires_ms INTEGER NOT NULL DEFAULT 0,counts_concurrent INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY(domain,operation_id));
        CREATE TABLE IF NOT EXISTS upload_parts(
          domain TEXT NOT NULL,upload_id TEXT NOT NULL,part_index INTEGER NOT NULL,bytes INTEGER NOT NULL,
          PRIMARY KEY(domain,upload_id,part_index));
        """)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(reservations)")}
        for name, ddl in (("expires_ms", "INTEGER NOT NULL DEFAULT 0"), ("counts_concurrent", "INTEGER NOT NULL DEFAULT 1")):
            if name not in columns:
                connection.execute(f"ALTER TABLE reservations ADD COLUMN {name} {ddl}")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, kind: str, object_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM objects WHERE domain=? AND kind=? AND object_id=?",
                             (DOMAIN_ID, kind, _identifier(object_id))).fetchone()
            return dict(row) if row else None

    def register(self, kind: str, object_id: str, principal: dict, *, project_id: str = "", size_bytes: int = 0,
                 provenance: str = "created") -> None:
        if kind not in KINDS or size_bytes < 0:
            raise HTTPException(400, "invalid_acl_object")
        object_id = _identifier(object_id)
        if principal.get("domain_id") != DOMAIN_ID or not principal.get("subject"):
            raise HTTPException(403, "studio_wrong_domain")
        subject = str(principal["subject"])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if project_id and kind != "project":
                project = self._require(db, principal, "project", project_id, True)
                subject = project["owner"]
            existing = db.execute("SELECT owner,project_id FROM objects WHERE domain=? AND kind=? AND object_id=?",
                                  (DOMAIN_ID, kind, object_id)).fetchone()
            if existing:
                if existing["owner"] != subject or existing["project_id"] != project_id:
                    raise HTTPException(403, "studio_object_owned_elsewhere")
                return
            db.execute("INSERT INTO objects VALUES(?,?,?,?,?,?,?,?)", (DOMAIN_ID, kind, object_id, subject,
                       project_id, int(size_bytes), int(time.time() * 1000), provenance))

    def _require(self, db, principal, kind, object_id, write):
        if principal.get("domain_id") != DOMAIN_ID or not principal.get("subject"):
            raise HTTPException(403, "studio_wrong_domain")
        row = db.execute("SELECT * FROM objects WHERE domain=? AND kind=? AND object_id=?",
                         (DOMAIN_ID, kind, _identifier(object_id))).fetchone()
        if not row:
            raise HTTPException(403, "studio_object_unregistered")
        if row["owner"] == principal["subject"]:
            return dict(row)
        grant = db.execute("SELECT role,revoked FROM grants WHERE domain=? AND kind=? AND object_id=? AND subject=?",
                           (DOMAIN_ID, kind, object_id, principal["subject"])).fetchone()
        if not grant and row["project_id"]:
            grant = db.execute("SELECT role,revoked FROM grants WHERE domain=? AND kind='project' AND object_id=? AND subject=?",
                               (DOMAIN_ID, row["project_id"], principal["subject"])).fetchone()
        if grant and not grant["revoked"] and (not write or grant["role"] == "editor"):
            return dict(row)
        raise HTTPException(403, "studio_object_forbidden")

    def require(self, principal: dict, kind: str, object_id: str, *, write: bool = False) -> dict:
        with self.connect() as db:
            return self._require(db, principal, kind, object_id, write)

    def grant(self, principal: dict, kind: str, object_id: str, subject: str, role: str = "reader", *, revoke=False):
        with self.connect() as db:
            row = self._require(db, principal, kind, object_id, True)
            if row["owner"] != principal["subject"] or role not in {"reader", "editor"} or not str(subject):
                raise HTTPException(403, "studio_owner_required")
            db.execute("INSERT INTO grants VALUES(?,?,?,?,?,?) ON CONFLICT(domain,kind,object_id,subject) "
                       "DO UPDATE SET role=excluded.role,revoked=excluded.revoked",
                       (DOMAIN_ID, kind, object_id, str(subject), role, int(revoke)))

    def filter_visible(self, principal: dict, kind: str, records: list[dict], *, id_key="project_id") -> list[dict]:
        result = []
        for record in records:
            try:
                self.require(principal, kind, str(record.get(id_key) or record.get("id") or ""))
            except HTTPException:
                continue
            result.append(record)
        return result

    def reserve(self, principal: dict, operation_id: str, size_bytes: int = 0, *, counts_concurrent=True, ttl_ms=900000, allow_active=True):
        if size_bytes < 0:
            raise HTTPException(400, "invalid_reservation_size")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = int(time.time() * 1000)
            db.execute("UPDATE reservations SET active=0,bytes=0 WHERE active=1 AND expires_ms<=?", (now,))
            existing = db.execute("SELECT * FROM reservations WHERE domain=? AND operation_id=?",
                                  (DOMAIN_ID, _identifier(operation_id))).fetchone()
            if existing:
                if existing["subject"] != principal["subject"]:
                    raise HTTPException(403, "studio_operation_owned_elsewhere")
                if existing["bytes"] != size_bytes and existing["active"]:
                    raise HTTPException(409, "studio_reservation_conflict")
                if existing["active"]:
                    if not allow_active:
                        raise HTTPException(409, "studio_operation_in_progress")
                    return
            quota = db.execute("SELECT bytes_limit,concurrent_limit FROM quotas WHERE domain=? AND subject=?",
                               (DOMAIN_ID, principal["subject"])).fetchone()
            bytes_limit, concurrent_limit = (quota[0], quota[1]) if quota else (100 * 1024**3, 2)
            used = db.execute("SELECT COALESCE(SUM(bytes),0) FROM objects WHERE domain=? AND owner=?",
                              (DOMAIN_ID, principal["subject"])).fetchone()[0]
            reserved, active = db.execute("SELECT COALESCE(SUM(bytes),0),COALESCE(SUM(counts_concurrent),0) FROM reservations WHERE domain=? AND subject=? AND active=1",
                                         (DOMAIN_ID, principal["subject"])).fetchone()
            if used + reserved + size_bytes > bytes_limit:
                raise HTTPException(429, "studio_user_storage_quota")
            if counts_concurrent and active >= concurrent_limit:
                raise HTTPException(429, "studio_user_concurrency_quota")
            db.execute("INSERT INTO reservations(domain,operation_id,subject,bytes,active,expires_ms,counts_concurrent) VALUES(?,?,?,?,1,?,?) "
                       "ON CONFLICT(domain,operation_id) DO UPDATE SET bytes=excluded.bytes,active=1,expires_ms=excluded.expires_ms,counts_concurrent=excluded.counts_concurrent",
                       (DOMAIN_ID, operation_id, principal["subject"], size_bytes, now + ttl_ms, int(counts_concurrent)))

    def release(self, operation_id: str):
        with self.connect() as db:
            db.execute("UPDATE reservations SET active=0,bytes=0 WHERE domain=? AND operation_id=?", (DOMAIN_ID, operation_id))

    def renew(self, operation_id: str, *, ttl_ms=900000):
        with self.connect() as db:
            db.execute("UPDATE reservations SET expires_ms=? WHERE domain=? AND operation_id=? AND active=1",
                       (int(time.time()*1000)+ttl_ms, DOMAIN_ID, operation_id))

    def record_upload_part(self, principal: dict, upload_id: str, index: int, size_bytes: int):
        if index < 0 or size_bytes < 0:
            raise HTTPException(400, "invalid_upload_part")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require(db, principal, "upload", upload_id, True)
            db.execute("INSERT INTO upload_parts VALUES(?,?,?,?) ON CONFLICT(domain,upload_id,part_index) DO UPDATE SET bytes=excluded.bytes",
                       (DOMAIN_ID, upload_id, index, size_bytes))
            size = db.execute("SELECT COALESCE(SUM(bytes),0) FROM upload_parts WHERE domain=? AND upload_id=?", (DOMAIN_ID, upload_id)).fetchone()[0]
            db.execute("UPDATE objects SET bytes=? WHERE domain=? AND kind='upload' AND object_id=?", (size, DOMAIN_ID, upload_id))

    def set_size(self, kind: str, object_id: str, size_bytes: int):
        if size_bytes < 0:
            raise HTTPException(400, "invalid_object_size")
        with self.connect() as db:
            db.execute("UPDATE objects SET bytes=? WHERE domain=? AND kind=? AND object_id=?", (int(size_bytes), DOMAIN_ID, kind, object_id))

    def attach(self, principal: dict, kind: str, object_id: str, project_id: str):
        """只绑定先前未定项目的本主体对象；不改已有其它项目归属。"""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            obj = self._require(db, principal, kind, object_id, True)
            project = self._require(db, principal, "project", project_id, True)
            if obj["owner"] != principal["subject"] or project["owner"] != principal["subject"] or obj["project_id"] not in {"", project_id}:
                raise HTTPException(403, "studio_object_binding_forbidden")
            db.execute("UPDATE objects SET project_id=? WHERE domain=? AND kind=? AND object_id=?", (project_id, DOMAIN_ID, kind, object_id))


def ledger(request: Request | None = None) -> AccessLedger:
    if request is not None:
        return getattr(request.app.state, "studio_access_ledger", None) or _access.get() or AccessLedger()
    return _access.get() or AccessLedger()


def register_created_object(kind: str, object_id: str, project_id: str = "", *, size_bytes=0, principal=None, access=None):
    """创建点调用；worker只能从已登记project派生owner，不能接受config中的主体。"""
    principal = principal or _principal.get()
    access = access or ledger()
    if principal is None:
        project = access.get("project", project_id) if project_id else None
        if not project:
            raise HTTPException(403, "studio_unknown_worker_project")
        principal = {"subject": project["owner"], "domain_id": DOMAIN_ID}
    access.register(kind, object_id, principal, project_id=project_id, size_bytes=size_bytes)


def reserve_job_operation(job_id: str, project_id: str, object_key: str):
    access = ledger()
    principal = _principal.get()
    if principal is None:
        raise HTTPException(401, "studio_job_requires_authenticated_creator")
    project = access.require(principal, "project", project_id, write=True)
    source = access.require(principal, "source", object_key)
    owner = {"subject": project["owner"], "domain_id": DOMAIN_ID}
    access.reserve(owner, job_id, int(source["bytes"]) * 2)


def release_operation(operation_id: str):
    ledger().release(operation_id)


def renew_operation(operation_id: str):
    ledger().renew(operation_id)


@contextmanager
def upload_part_quota(upload_id: str, index: int, *, maximum_bytes=8 * 1024 * 1024):
    if not 0 <= index < 20000:
        raise HTTPException(400, "invalid_upload_part")
    principal, access = current_principal(), ledger()
    access.require(principal, "upload", upload_id, write=True)
    operation_id = f"upload:{upload_id}:part:{index}"
    access.reserve(principal, operation_id, maximum_bytes, counts_concurrent=False, allow_active=False)
    try:
        yield
    finally:
        access.release(operation_id)


def record_upload_part(upload_id: str, index: int, size_bytes: int):
    ledger().record_upload_part(current_principal(), upload_id, index, size_bytes)


def complete_upload_accounting(upload_id: str):
    """成功组装并登记真实source bytes后，已删除chunks不再重复计费。"""
    ledger().set_size("upload", upload_id, 0)


def prepare_project_operation(task_id: str, project_id: str, *, expected_bytes: int = 0) -> AccessLedger:
    """本地后台任务创建前预留；内部调用只能派生已登记项目的owner。"""
    # 删除期不可变守卫（2026-10-20 项目生命周期）：deleting/delete_failed/deleted
    # 的项目拒绝本地任务/自动剪辑/重剪/封面等一切重型操作。
    from .project_lifecycle import LifecycleError, assert_mutable
    try:
        assert_mutable(project_id)
    except LifecycleError as exc:
        raise HTTPException(409, detail={"code": exc.code, "message": exc.message})
    access=ledger()
    actor=_principal.get()
    project=access.require(actor,"project",project_id,write=True) if actor else access.get("project",project_id)
    if not project:
        raise HTTPException(403,"studio_unknown_task_project")
    owner={"subject":project["owner"],"domain_id":DOMAIN_ID}
    access.reserve(owner,task_id,expected_bytes,allow_active=False)
    try:
        access.register("task",task_id,owner,project_id=project_id)
    except Exception:
        access.release(task_id)
        raise
    return access


@contextmanager
def heavy_operation(*, project_id: str = "", task_id: str = "", expected_bytes: int = 0):
    """只包实际计算分支；缓存命中/普通下载不调用。"""
    access=ledger();actor=_principal.get();record=None
    if task_id:
        record=access.require(actor,"task",task_id) if actor else access.get("task",task_id)
        if record:project_id=record["project_id"]
    elif project_id:
        record=access.require(actor,"project",project_id) if actor else access.get("project",project_id)
    if (task_id or project_id) and not record:
        raise HTTPException(403,"studio_unknown_compute_object")
    if not record and not actor:
        raise HTTPException(401,"studio_compute_auth_required")
    owner={"subject":record["owner"] if record else actor["subject"],"domain_id":DOMAIN_ID}
    operation_id="compute:"+uuid.uuid4().hex
    access.reserve(owner,operation_id,expected_bytes)
    try:
        yield access,owner,project_id
    finally:
        access.release(operation_id)


def account_derived_output(context, path: Path):
    access,owner,project_id=context
    artifact_id="derived:"+hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()
    access.register("artifact",artifact_id,owner,project_id=project_id,size_bytes=Path(path).stat().st_size)
    access.set_size("artifact",artifact_id,Path(path).stat().st_size)


_legacy_watchers: set[tuple[str,str]]=set()
_legacy_watcher_lock=threading.Lock()
_TERMINAL_TASK_STATUSES=frozenset({"completed","done","failed","error","cancelled","canceled","completed_with_warnings",
                                  "partial_succeeded","technical_failed","completed_with_cleanup_warning"})


def watch_legacy_operation(task_id: str, status_path: Path, *, access=None, poll_seconds=5.0, missing_grace_seconds=60.0):
    """只观察既有队列元数据；不改任务状态，不执行或重跑任务。"""
    access=access or ledger()
    key=(str(access.path),task_id)
    with _legacy_watcher_lock:
        if key in _legacy_watchers:
            return None
        _legacy_watchers.add(key)
    def runner():
        missing_since=None
        try:
            while True:
                if status_path.is_file():
                    missing_since=None
                    try:
                        status=json.loads(status_path.read_text(encoding="utf8"))
                        if status.get("status") in _TERMINAL_TASK_STATUSES:
                            return
                    except (OSError,ValueError,AttributeError):
                        pass  # 元数据暂不可读时保守保留槽，不猜任务已经结束。
                else:
                    queue_present=any((status_path.parent.parent/folder/(task_id+".json")).is_file()
                                      for folder in ("in","processing","completed","failed"))
                    if not queue_present:
                        missing_since=missing_since or time.monotonic()
                        if time.monotonic()-missing_since>=missing_grace_seconds:
                            return
                access.renew(task_id)
                time.sleep(poll_seconds)
        finally:
            try:
                access.release(task_id)
            finally:
                with _legacy_watcher_lock:
                    _legacy_watchers.discard(key)
    thread=threading.Thread(target=runner,name="studio-quota-"+task_id,daemon=True)
    try:
        thread.start()
    except Exception:
        with _legacy_watcher_lock:
            _legacy_watchers.discard(key)
        raise
    return thread


def resume_legacy_quota_watchers(tasks_root: Path):
    """后端启动时恢复已持久登记的legacy队列配额观察，不重放业务。"""
    access=ledger()
    with access.connect() as db:
        tasks=db.execute("SELECT o.object_id FROM objects o JOIN reservations r ON o.domain=r.domain AND o.object_id=r.operation_id "
                         "WHERE o.domain=? AND o.kind='task' AND r.active=1 AND o.owner=r.subject",(DOMAIN_ID,)).fetchall()
    for row in tasks:
        task_id=row["object_id"]
        if re.fullmatch(r"autoedit_[0-9a-f]{12}",task_id):
            watch_legacy_operation(task_id,Path(tasks_root)/"status"/(task_id+".json"),access=access)


async def require_source_access(request: Request, object_key: str) -> None:
    ledger(request).require(current_principal(request), "source", object_key, write=request.method not in {"GET", "HEAD"})


def reserve_new_upload(request: Request, upload_id: str, existing_directory: Path):
    """仅首次POST新随机ID可创建；未知历史目录不因访问而认领。"""
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id or ""):
        raise HTTPException(400, "invalid_upload_id")
    access, principal = ledger(request), current_principal(request)
    if access.get("upload", upload_id):
        return access.require(principal, "upload", upload_id, write=True)
    if existing_directory.exists():
        raise HTTPException(403, "studio_unowned_existing_upload")
    access.register("upload", upload_id, principal)
    return access.require(principal, "upload", upload_id, write=True)


def reserve_new_project(request: Request, project_id: str, existing_paths: list[Path]):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", project_id or ""):
        raise HTTPException(400, "invalid_project_id")
    access, principal = ledger(request), current_principal(request)
    if access.get("project", project_id):
        return access.require(principal, "project", project_id, write=True)
    if any(path.exists() for path in existing_paths):
        raise HTTPException(403, "studio_unowned_existing_project")
    access.register("project", project_id, principal)
    return access.require(principal, "project", project_id, write=True)


async def require_reference(request: Request, kind: str, value: str, write: bool):
    access, principal = ledger(request), current_principal(request)
    if kind == "task" and not access.get("task", value) and re.fullmatch(r"[0-9a-f]{32}", value):
        # staged job 的task别名只由真实DB关联派生，不能从客户端project参数猜归属。
        from .studio_job_store import get_job
        job = await get_job(value)
        if job and job.get("project_id"):
            project = access.require(principal, "project", str(job["project_id"]), write=write)
            owner = {"subject": project["owner"], "domain_id": DOMAIN_ID}
            access.register("task", value, owner, project_id=job["project_id"], provenance="verified_job_alias")
    return access.require(principal, kind, value, write=write)


async def enforce_studio_access(request: Request):
    """路由解析后检查路径/查询/JSON对象引用；不读取上传chunk或multipart正文。"""
    if _is_worker_request(request.method, request.url.path):
        return  # 仅精确worker路径由认证middleware和原_require_worker双重检查。
    principal = current_principal(request)
    access = ledger(request)
    write = request.method not in {"GET", "HEAD", "OPTIONS"}
    refs = list(request.path_params.items())
    refs.extend(request.query_params.multi_items())
    path=request.url.path
    binary_upload=(request.method=="POST" and (path in {"/upload","/fonts/upload","/studio/transcribe","/studio/transcribe/start","/studio/transcribe/chunk"}
                    or bool(re.fullmatch(r"/studio/timeline/[^/]+/assets/audio",path))
                    or bool(re.fullmatch(r"/studio/clips/[^/]+/[^/]+/audio/bgm",path))))
    if write and not binary_upload:
        raw=await request.body()
        content_type=request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if raw and content_type!="application/json":
            raise HTTPException(415,"studio_json_content_type_required")
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise HTTPException(400, "invalid_json_body") from None
        if not isinstance(body,dict):
            raise HTTPException(400,"studio_json_object_required")
        if isinstance(body, dict):
            for key in ("project_id", "job_id", "task_id", "upload_id", "object_key"):
                if body.get(key):
                    refs.append((key, body[key]))
    mapping = {"project_id": "project", "job_id": "job", "task_id": "task", "upload_id": "upload", "session_id": "upload", "object_key": "source"}
    path = request.url.path
    if write and (path.startswith("/fonts") or path.startswith("/studio/workflow/skills/")) and principal["subject"] != "8":
        raise HTTPException(403, "studio_domain_owner_required")
    if path == "/studio/copilot" and not any(key == "project_id" and value for key, value in refs):
        raise HTTPException(400, "studio_project_required")
    if request.method == "POST" and path == "/studio/transcribe/chunk":
        from .config import get_config
        upload_id = request.query_params.get("upload_id", "")
        reserve_new_upload(request, upload_id, Path(get_config().temp_dir) / "studio-chunks" / upload_id)
    # 先校验其它已有对象，避免跨用户assemble先产生新project ACL。
    for key, value in refs:
        if key in mapping and key != "project_id" and value:
            await require_reference(request, mapping[key], _identifier(value), write)
    if request.method == "POST" and path in {"/api/uploads", "/studio/transcribe/assemble"}:
        from .config import get_config
        from .studio_project_store import TASKS_ROOT
        project_id = next((str(value) for key, value in refs if key == "project_id"), "")
        if path == "/studio/transcribe/assemble":
            upload_id = next((str(value) for key, value in refs if key == "upload_id"), "")
            upload = access.get("upload", upload_id) if upload_id else None
            if upload and upload["project_id"] and upload["project_id"] != project_id:
                raise HTTPException(409, "studio_upload_project_conflict")
        if project_id:
            reserve_new_project(request, project_id, [Path(get_config().temp_dir) / "studio-projects" / project_id,
                                                      TASKS_ROOT / "projects" / project_id])
    for key, value in refs:
        if key in mapping and value:
            await require_reference(request, mapping[key], _identifier(value), write)
    request.state.studio_checked_refs = refs


def _is_worker_request(method: str, path: str) -> bool:
    if method == "GET" and re.fullmatch(r"/api/internal/jobs/[A-Za-z0-9_-]+/artifacts", path):
        return True
    return method == "POST" and (path in {"/api/internal/heartbeat", "/api/internal/jobs/claim"}
                                 or bool(re.fullmatch(r"/api/internal/jobs/[A-Za-z0-9_-]+/(state|artifacts|activate)", path)))


_ba_pool = None


async def _better_auth_principal(request: Request) -> dict:
    """隔离环境分支（STUDIO_AUTH_BACKEND=better-auth）：
    Better Auth 会话 cookie → principal（subject=Better Auth user.id），
    并幂等创建个人 workspace + 冻结配额 (0,0)（D14，见 studio_workspace.py）。
    生产默认分支不进入本函数，行为零变化。
    """
    global _ba_pool
    import asyncpg
    from . import better_auth_session
    from .studio_workspace import ensure_workspace

    cookie = request.headers.get("cookie", "")
    secret = os.environ.get("BETTER_AUTH_SECRET", "")
    if not secret:
        raise HTTPException(503, "studio_auth_unavailable")
    # 阶段三 fail-closed 加固：签名/会话/过期/用户有效性失败 → 401；
    # PG/ACL 基础设施故障 → 503；任何认证失败都不降级为匿名、不旁路校验。
    try:
        if _ba_pool is None:
            dsn = os.environ.get("BETTER_AUTH_DATABASE_URL",
                                 os.environ.get("DATABASE_URL", "").replace("+asyncpg", ""))
            if not dsn:
                raise HTTPException(503, "studio_auth_unavailable")
            _ba_pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, timeout=5)
        async with _ba_pool.acquire() as conn:
            # 每请求全量校验：签名 + 会话存在 + 未过期 + 用户存在；无任何缓存，
            # 登出/撤销（session 行删除）后下一请求立即 401。
            principal = await better_auth_session.principal_from_cookie(cookie, secret, conn, DOMAIN_ID)
        if not principal:
            raise HTTPException(401, "studio_auth_required")
        with ledger(request).connect() as db:
            ensure_workspace(db, principal)  # 首次登录幂等创建；重复调用零副作用
    except HTTPException:
        raise
    except (asyncpg.PostgresError, OSError, asyncio.TimeoutError, sqlite3.Error):
        # 基础设施不可用：显式 503 fail-closed（绝不放行匿名请求）
        raise HTTPException(503, "studio_auth_unavailable") from None
    return principal


class StudioAuthenticationMiddleware:
    """纯ASGI身份middleware，在TCP/Next/Unix socket所有入口执行同一校验。"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in {"/", "/health"}:
            return await self.app(scope, receive, send)
        request = Request(scope, receive=receive)
        path = scope.get("path", "")
        if _is_worker_request(request.method, path):
            # 不做普通请求worker旁路；精确internal端点仍执行原router依赖。
            return await self.app(scope, receive, send)
        try:
            auth_backend = os.environ.get("STUDIO_AUTH_BACKEND", "")
            cookie = request.headers.get("cookie", "")
            if auth_backend == "better-auth":
                # 单通道：仅 Better Auth（隔离环境默认）
                principal = await _better_auth_principal(request)
            elif auth_backend == "dual":
                # 双通道（生产灰度用）：请求携带 Better Auth 会话 cookie → user.id 校验
                # （fail-closed：cookie 存在但无效/过期 → 401，不降级平台）；
                # 否则 → 平台认证 socket（存量平台用户零影响）。
                from . import better_auth_session
                if better_auth_session.extract_session_cookie(cookie):
                    principal = await _better_auth_principal(request)
                else:
                    principal = await query_principal(cookie, request.method,
                                                      request.headers.get("origin", ""))
            else:
                # 平台认证（生产默认/回滚通道）
                principal = await query_principal(cookie, request.method,
                                                  request.headers.get("origin", ""))
        except HTTPException as exc:
            return await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)
        scope.setdefault("state", {})["studio_principal"] = principal
        # 旧路由也不能从客户端头取得第二套身份。
        forbidden = {b"x-supoclip-user-id", b"x-user-id", b"x-dsh-user-id", b"user_id", b"x-principal", b"x-video-subject"}
        scope["headers"] = [(key, value) for key, value in scope.get("headers", []) if key.lower() not in forbidden]
        token = _principal.set(principal)
        access_token = _access.set(ledger(request))
        try:
            if path == "/studio/projects" and request.method == "GET":
                messages = []
                async def collect(message):
                    messages.append(message)
                await self.app(scope, receive, collect)
                start = next((m for m in messages if m["type"] == "http.response.start"), None)
                if start and start["status"] == 200:
                    records = json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))
                    if not isinstance(records, list):
                        raise ValueError("invalid project list")
                    records = ledger(request).filter_visible(principal, "project", records)
                    response = JSONResponse(records, headers={k.decode(): v.decode() for k, v in start["headers"]
                                                               if k.lower() not in {b"content-length", b"content-type"}})
                    await response(scope, receive, send)
                else:
                    for message in messages:
                        await send(message)
            else:
                await self.app(scope, receive, send)
        finally:
            _access.reset(access_token)
            _principal.reset(token)


access_router = APIRouter(prefix="/api/studio", tags=["studio-access"])


@access_router.get("/access")
async def access_status(request: Request):
    principal, access = current_principal(request), ledger(request)
    with access.connect() as db:
        quota = db.execute("SELECT bytes_limit,concurrent_limit FROM quotas WHERE domain=? AND subject=?",
                           (DOMAIN_ID, principal["subject"])).fetchone()
        used = db.execute("SELECT COALESCE(SUM(bytes),0) FROM objects WHERE domain=? AND owner=?",
                          (DOMAIN_ID, principal["subject"])).fetchone()[0]
        reserved, active = db.execute("SELECT COALESCE(SUM(bytes),0),COALESCE(SUM(counts_concurrent),0) FROM reservations "
                                     "WHERE domain=? AND subject=? AND active=1 AND expires_ms>?",
                                     (DOMAIN_ID, principal["subject"], int(time.time()*1000))).fetchone()
    return {"principal": principal, "quota": {"bytes_limit": quota[0] if quota else 100*1024**3,
            "concurrent_limit": quota[1] if quota else 2, "used_bytes": used, "reserved_bytes": reserved, "active_operations": active},
            "admission_policy": "host_managed_membership", "acl_role_policy": ["owner", "editor", "reader"]}


@access_router.get("/capacity-status")
async def capacity_status(request: Request):
    """监控视图（阶段四）：数据盘水位、floor 配置与放行状态（只读，需认证）。"""
    from .capacity_guard import floor_bytes, site_floor_status
    return {"floor_bytes": floor_bytes(), "floor_enabled": floor_bytes() > 0,
            "site": site_floor_status()}


@access_router.get("/projects/{project_id}/access")
async def get_project_access(project_id: str, request: Request):
    principal, access = current_principal(request), ledger(request)
    project = access.require(principal, "project", project_id)
    if project["owner"] != principal["subject"]:
        raise HTTPException(403, "studio_owner_required")
    with access.connect() as db:
        rows = db.execute("SELECT subject,role,revoked FROM grants WHERE domain=? AND kind='project' AND object_id=?",
                          (DOMAIN_ID, project_id)).fetchall()
    return {"owner": project["owner"], "grants": [dict(row) for row in rows], "admission_managed_by_host": True}


@access_router.post("/projects/{project_id}/access")
async def set_project_access(project_id: str, request: Request):
    data = await request.json()
    if (not isinstance(data, dict) or set(data) - {"subject", "role", "enabled"}
            or not isinstance(data.get("subject"), str) or not re.fullmatch(r"[1-9][0-9]*", data["subject"])
            or data.get("role") not in {"reader", "editor"} or type(data.get("enabled")) is not bool):
        raise HTTPException(422, "invalid_project_grant")
    ledger(request).grant(current_principal(request), "project", project_id, str(data["subject"]), data["role"], revoke=not data["enabled"])
    return {"accepted": True, "subject": str(data["subject"]), "role": data["role"], "enabled": data["enabled"],
            "admission_managed_by_host": True}
