"""Studio Job 持久化与协调：PostgreSQL 为事实源，Redis 负责队列/锁/心跳/进度事件."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from sqlalchemy import text

from .database import get_session_maker

logger = logging.getLogger("studio_job_store")

WORKER_LEASE_SECONDS = 120
WORKER_MAX_ATTEMPTS = 3
WORKER_HEARTBEAT_TTL = 60
EVENT_CHANNEL = "studio:jobs:events"


def _redis():
    from .workers.job_queue import JobQueue

    return JobQueue.get_pool()


async def create_job(project_id, object_key, object_size, config, *, job_id=None):
    import hashlib
    job_id = job_id or uuid.uuid4().hex
    config = {k:v for k,v in (config or {}).items() if not k.startswith("_")}
    fingerprint = hashlib.sha256(json.dumps({"project_id":project_id,"object_key":object_key,
        "object_size":object_size,"config":config},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    maker = get_session_maker()
    reserved = False
    try:
        async with maker() as session:
            # 同请求ID的首次创建也必须串行，避免不存在行时ON CONFLICT隐藏不同body。
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:id,0))"),{"id":job_id})
            found = await session.execute(text("SELECT * FROM studio_jobs WHERE id=:id FOR UPDATE"),{"id":job_id})
            existing = found.mappings().first()
            if existing:
                if lifecycle(dict(existing)).get("creation_fingerprint") != fingerprint:
                    raise JobLifecycleError("E_IDEMPOTENCY_CONFLICT","相同请求键已绑定不同任务内容")
                _access().ledger().require(_access().current_principal(),"job",job_id,write=True)
                return job_id
            _access().reserve_job_operation(job_id,project_id,object_key)
            reserved = True
            _access().register_created_object("job",job_id,project_id)
            _access().register_created_object("task",job_id,project_id)
            config["_lifecycle"]={"creation_fingerprint":fingerprint,"generation":0,"render_generation":0,
                "stage_sources":{k:"unknown" for k in ("correction","analysis","planning")},"operations":{},"attempt_history":[]}
            await session.execute(text("""INSERT INTO studio_jobs(id,project_id,object_key,object_size,config)
                VALUES(:id,:project_id,:object_key,:object_size,CAST(:config AS JSONB))"""),
                {"id":job_id,"project_id":project_id,"object_key":object_key,"object_size":object_size,
                 "config":json.dumps(config,ensure_ascii=False)})
            await session.commit()
    except Exception:
        if reserved: _access().release_operation(job_id)
        raise
    await _enqueue(job_id)
    await publish_event(job_id,"queued","",0)
    return job_id



async def create_upload_session(project_id: str, object_key: str, filename: str,
                                size_bytes: int, part_size: int, parts_total: int,
                                r2_upload_id: str, *, session_id=None) -> str:
    session_id = session_id or uuid.uuid4().hex
    prepare_upload_session(session_id,project_id,size_bytes)
    maker = get_session_maker()
    try:
        async with maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO studio_upload_sessions
                        (id, project_id, object_key, filename, size_bytes, part_size, parts_total, r2_upload_id)
                    VALUES (:id, :project_id, :object_key, :filename, :size_bytes, :part_size, :parts_total, :r2_upload_id)
                    """
                ),
                {"id": session_id, "project_id": project_id, "object_key": object_key,
                 "filename": filename[:255], "size_bytes": size_bytes, "part_size": part_size,
                 "parts_total": parts_total, "r2_upload_id": r2_upload_id},
            )
            await session.commit()
    except Exception:
        _access().release_operation("upload:"+session_id)
        raise
    return session_id


async def get_upload_session(session_id: str) -> dict[str, Any] | None:
    maker = get_session_maker()
    async with maker() as session:
        result = await session.execute(
            text("SELECT * FROM studio_upload_sessions WHERE id = :id"),
            {"id": session_id},
        )
        row = result.mappings().first()
    return dict(row) if row else None


async def complete_upload_session(session_id: str, *, verified_size=None) -> None:
    upload = await get_upload_session(session_id)
    if upload is None: raise JobLifecycleError("E_UPLOAD_MISSING", "上传会话不存在",404)
    if verified_size is None or int(verified_size)!=int(upload["size_bytes"]):
        raise JobLifecycleError("E_UPLOAD_SIZE", "上传实际字节未验证")
    _access().register_created_object("source",upload["object_key"],upload["project_id"],size_bytes=int(verified_size))
    maker = get_session_maker()
    async with maker() as session:
        await session.execute(
            text("UPDATE studio_upload_sessions SET status='completed' WHERE id=:id"),
            {"id": session_id},
        )
        await session.commit()
    _access().release_operation("upload:"+session_id)


async def get_job(job_id: str) -> dict[str, Any] | None:
    maker = get_session_maker()
    async with maker() as session:
        result = await session.execute(
            text("SELECT * FROM studio_jobs WHERE id = :id"),
            {"id": job_id},
        )
        row = result.mappings().first()
    return dict(row) if row else None


async def project_job_facts(project_id: str) -> list[dict[str, Any]]:
    """项目名下全部 job 行的事实数据（id/object_key/object_size/墓碑态），删除清单与引用扫描共用。"""
    maker = get_session_maker()
    async with maker() as session:
        result = await session.execute(text(
            "SELECT id, object_key, COALESCE(object_size,0) AS object_size, deleted_at_utc "
            "FROM studio_jobs WHERE project_id = :pid ORDER BY created_at"),
            {"pid": project_id})
        return [dict(r) for r in result.mappings().all()]


async def tombstone_project_jobs(project_id: str, *, deleted_via: str) -> list[str]:
    """项目删除的可审计墓碑（2026-10-20）：不物理删除 studio_jobs 行。

    打 ``deleted_at_utc``/``deleted_via`` 后行保留完整审计链；claim/重试路径以
    ``deleted_at_utc IS NULL`` 过滤，墓碑行永不复活、永不回填。返回打标的 job id。
    """
    maker = get_session_maker()
    async with maker() as session:
        result = await session.execute(text(
            "UPDATE studio_jobs SET deleted_at_utc=NOW(), deleted_via=:via "
            "WHERE project_id=:pid AND deleted_at_utc IS NULL RETURNING id"),
            {"pid": project_id, "via": deleted_via[:512]})
        ids = [str(r[0]) for r in result.all()]
        await session.commit()
    for job_id in ids:
        try:
            _access().release_operation(job_id)
        except Exception:  # noqa: BLE001 — 预约释放失败不阻塞墓碑（字节本应为 0）
            pass
    return ids


async def pg_source_reference_projects(upload_id: str, exclude_projects: set[str]) -> list[str]:
    """仍在引用某上传原件的**未删除**项目（deleted_at_utc IS NULL），用于删除前引用检查。"""
    maker = get_session_maker()
    async with maker() as session:
        result = await session.execute(text(
            "SELECT DISTINCT project_id FROM studio_jobs "
            "WHERE object_key LIKE :pattern AND deleted_at_utc IS NULL"),
            {"pattern": f"%{upload_id}%"})
        return sorted(str(r[0]) for r in result.all()
                      if str(r[0]) not in exclude_projects)


async def claim_next_job(worker_id):
    maker = get_session_maker()
    async with maker() as session:
        result = await session.execute(text("""SELECT * FROM studio_jobs
            WHERE (status IN ('queued','reedit_requested') OR
              (status <> ALL(CAST(:terminal AS text[])) AND lease_expires_at < NOW()))
              AND deleted_at_utc IS NULL
            ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED"""),{"terminal":list(TERMINAL_STATUSES)})
        row=result.mappings().first()
        if not row: return None
        job=dict(row);config=_configuration(job);state=config.setdefault("_lifecycle",{})
        attempts=int(job.get("attempts") or 0)
        if attempts-int(state.get("claim_base") or 0)>=WORKER_MAX_ATTEMPTS:
            await session.execute(text("UPDATE studio_jobs SET status='failed',error='超过自动恢复重试次数',worker_id=NULL,lease_expires_at=NULL WHERE id=:id"),{"id":job["id"]})
            _access().release_operation(job["id"])
            await session.commit()
            return None
        state["generation"]=int(state.get("generation") or 0)+1
        state["lease_token"]=uuid.uuid4().hex
        state.setdefault("attempt_history",[]).append({"attempt":attempts+1,"generation":state["generation"],"started_at":time.time()})
        _worker_quota(job)
        job["config"]=config
        _access().register_created_object("task",render_run_id(job),job["project_id"])
        await session.execute(text("""UPDATE studio_jobs SET status='downloading',stage='准备恢复执行',progress=0.02,
            worker_id=:worker_id,lease_expires_at=NOW()+(:lease_seconds || ' seconds')::interval,
            attempts=attempts+1,error=NULL,config=CAST(:config AS JSONB),updated_at=NOW() WHERE id=:id"""),
            {"id":job["id"],"worker_id":worker_id,"lease_seconds":str(WORKER_LEASE_SECONDS),"config":json.dumps(config,ensure_ascii=False)})
        await session.commit()
    return await get_job(job["id"])



async def _enqueue_lease_extend(job_id: str) -> None:
    """租约续期由 Worker 侧定时 PEXPIRE；此处仅确保键存在。"""
    try:
        pool = await _redis()
        await pool.expire(f"studio:lease:{job_id}", WORKER_LEASE_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("lease key skipped: %s", exc)


async def renew_lease(job_id, worker_id, lease_token=""):
    maker=get_session_maker()
    async with maker() as session:
        result=await session.execute(text("""UPDATE studio_jobs SET lease_expires_at=NOW()+(:seconds || ' seconds')::interval
            WHERE id=:id AND worker_id=:worker_id AND lease_expires_at>NOW()
            AND COALESCE(config#>>'{_lifecycle,lease_token}','')=:lease_token
            AND status <> ALL(CAST(:terminal AS text[])) AND status NOT IN ('queued','reedit_requested')"""),
            {"id":job_id,"worker_id":worker_id,"lease_token":lease_token,"seconds":str(WORKER_LEASE_SECONDS),"terminal":list(TERMINAL_STATUSES)})
        if result.rowcount!=1: return False
        _access().renew_operation(job_id)
        await session.commit()
    return True



async def mark_reedit_requested(job_id: str) -> bool:
    """「重新剪辑并替换旧切片」任务创建后立即置初始状态（替代 queued 展示）。"""
    maker = get_session_maker()
    async with maker() as session:
        result = await session.execute(
            text("UPDATE studio_jobs SET status='reedit_requested',"
                 " stage='待执行：复用源片与转写，成功后替换旧切片', updated_at=NOW() "
                 "WHERE id=:id AND status='queued'"),
            {"id": job_id})
        await session.commit()
        return result.rowcount == 1


async def delete_artifacts_for_jobs(job_ids: list[str]) -> int:
    """删除一组旧任务（job/task id）的 artifact 关联记录（重替换清理步骤）。

    只按精确 job_id 集合删除，绝不使用模糊匹配。
    """
    if not job_ids:
        return 0
    maker = get_session_maker()
    async with maker() as session:
        result = await session.execute(
            text("DELETE FROM studio_artifacts WHERE job_id = ANY(:ids)"),
            {"ids": job_ids})
        await session.commit()
        return int(result.rowcount or 0)


async def retry_failed_job(job_id, *, mode="resume_render", idempotency_key=None):
    return await request_job_operation(job_id,mode,idempotency_key or uuid.uuid4())



async def update_progress(job_id,status,stage,progress,error=None,extend_lease=True,worker_id="",lease_token="",metadata=None):
    maker=get_session_maker()
    async with maker() as session:
        job=await _locked_job(session,job_id)
        config=_configuration(job);state=config.setdefault("_lifecycle",{})
        if metadata:
            for key in ("stage_sources","degradation_reasons","llm_usage"):
                if key in metadata: state[key]=metadata[key]
        if status in {"failed","technical_failed","partial_succeeded"}:
            state["failure_stage"]=job.get("status")
        terminal=status in TERMINAL_STATUSES
        if terminal:
            history=state.setdefault("attempt_history",[])
            if history: history[-1].update({"ended_at":time.time(),"status":status})
        result=await session.execute(text("""UPDATE studio_jobs
            SET status=:status,stage=:stage,progress=:progress,error=:error,config=CAST(:config AS JSONB),
                lease_expires_at=CASE WHEN :terminal THEN NULL ELSE lease_expires_at END,
                worker_id=CASE WHEN :terminal THEN NULL ELSE worker_id END,updated_at=NOW()
            WHERE id=:id AND worker_id=:worker_id AND lease_expires_at>NOW()
              AND COALESCE(config#>>'{_lifecycle,lease_token}','')=:lease_token
              AND status <> ALL(CAST(:terminal_states AS text[])) AND status NOT IN ('queued','reedit_requested')"""),
            {"id":job_id,"worker_id":worker_id,"lease_token":lease_token,"status":status,"stage":stage[:120],
             "progress":max(0,min(float(progress),1)),"error":error,"config":json.dumps(config,ensure_ascii=False),
             "terminal":terminal,"terminal_states":list(TERMINAL_STATUSES)})
        if result.rowcount!=1: return False
        if terminal: _access().release_operation(job_id)
        await session.commit()
    await publish_event(job_id,status,stage,progress)
    return True



async def save_artifact(job_id,kind,object_key,size_bytes,clip_id=None,sha256=None,worker_id="",lease_token=""):
    maker=get_session_maker()
    async with maker() as session:
        owner=await session.execute(text("""SELECT * FROM studio_jobs WHERE id=:id AND worker_id=:worker_id
            AND lease_expires_at>NOW() AND COALESCE(config#>>'{_lifecycle,lease_token}','')=:lease_token
            AND status <> ALL(CAST(:terminal AS text[])) AND status NOT IN ('queued','reedit_requested') FOR UPDATE"""),
            {"id":job_id,"worker_id":worker_id,"lease_token":lease_token,"terminal":list(TERMINAL_STATUSES)})
        row=owner.mappings().first()
        if row is None: return None
        job=dict(row)
        existing=await session.execute(text("""SELECT id FROM studio_artifacts WHERE job_id=:job_id AND kind=:kind
            AND clip_id IS NOT DISTINCT FROM :clip_id AND object_key=:object_key LIMIT 1"""),
            {"job_id":job_id,"kind":kind,"clip_id":clip_id,"object_key":object_key})
        found=existing.first()
        if found: return str(found[0])
        artifact_id=uuid.uuid4().hex
        await session.execute(text("""INSERT INTO studio_artifacts(id,job_id,kind,clip_id,object_key,size_bytes,sha256)
            VALUES(:id,:job_id,:kind,:clip_id,:object_key,:size_bytes,:sha256)"""),
            {"id":artifact_id,"job_id":job_id,"kind":kind,"clip_id":clip_id,"object_key":object_key,"size_bytes":size_bytes,"sha256":sha256})
        _access().register_created_object("artifact",artifact_id,job["project_id"],size_bytes=size_bytes)
        await session.commit()
    return artifact_id



async def list_artifacts(job_id):
    job=await get_job(job_id)
    if job is None: return []
    prefix=f"studio-artifacts/{render_run_id(job)}/"
    maker=get_session_maker()
    async with maker() as session:
        result=await session.execute(text("SELECT id,kind,clip_id,object_key,size_bytes,sha256 FROM studio_artifacts WHERE job_id=:job_id ORDER BY created_at ASC"),{"job_id":job_id})
        rows=[dict(row) for row in result.mappings().all()]
    return [row for row in rows if row.get("kind") not in {"clip","clip_final","srt","clip_warnings"}
            or str(row.get("object_key") or "").removeprefix("local:").startswith(prefix)]



async def mark_worker_heartbeat(worker_id: str) -> None:
    try:
        pool = await _redis()
        await pool.set(f"studio:worker:{worker_id}", str(int(time.time())), ex=WORKER_HEARTBEAT_TTL)
    except Exception as exc:  # noqa: BLE001
        logger.debug("worker heartbeat skipped: %s", exc)


async def worker_online() -> bool:
    """60 秒内有任一执行器心跳即视为在线。"""
    try:
        pool = await _redis()
        keys = await pool.keys("studio:worker:*")
        return bool(keys)
    except Exception:  # noqa: BLE001
        return False


async def _enqueue(job_id: str) -> None:
    try:
        pool = await _redis()
        await pool.lpush("studio:queue", job_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("job enqueue failed (executor heartbeat path will still claim): %s", exc)


async def publish_event(job_id: str, status: str, stage: str, progress: float) -> None:
    try:
        pool = await _redis()
        await pool.publish(EVENT_CHANNEL, json.dumps(
            {"job_id": job_id, "status": status, "stage": stage, "progress": round(progress, 4),
             "ts": time.time()}, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        logger.debug("event publish skipped: %s", exc)


TERMINAL_STATUSES = frozenset({"completed", "failed", "transcript_ready", "analysis_ready", "plan_ready",
    "completed_with_warnings", "partial_succeeded", "technical_failed", "completed_with_cleanup_warning", "cancelled"})
RETRY_MODES = frozenset({"resume_render", "replan", "cleanup_only"})


class JobLifecycleError(RuntimeError):
    def __init__(self, code, message, status_code=409):
        super().__init__(message)
        self.code, self.status_code = code, status_code


def _configuration(job):
    value = job.get("config") or {}
    if isinstance(value, str):
        value = json.loads(value)
    return json.loads(json.dumps(value))


def lifecycle(job):
    return _configuration(job).get("_lifecycle") or {}


def render_run_id(job):
    generation = int(lifecycle(job).get("render_generation") or 0)
    job_id = str(job.get("id") or job.get("job_id") or "")
    return uuid.uuid5(uuid.NAMESPACE_URL,f"studio-render:{job_id}:{generation}").hex if generation else job_id


def _is_activated(job):
    from . import reedit_replace
    return reedit_replace.activation_done(render_run_id(job))


def job_capabilities(job):
    state = lifecycle(job)
    sources = {stage: (state.get("stage_sources") or {}).get(stage, "unknown")
               for stage in ("correction", "analysis", "planning")}
    sources = {k: v if v in {"llm", "rules", "mixed", "unknown"} else "unknown" for k, v in sources.items()}
    status = job.get("status")
    if _is_activated(job):
        actions = ["cleanup_only"] if status == "completed_with_cleanup_warning" else []
    elif status not in TERMINAL_STATUSES:
        actions = ["cancel"]
    elif status in {"failed", "technical_failed", "partial_succeeded", "cancelled", "analysis_ready", "plan_ready"}:
        actions = ["resume_render", "replan"]
    elif status == "completed_with_warnings" and sources["planning"] in {"rules", "mixed"}:
        actions = ["replan"]
    else:
        actions = []
    if _configuration(job).get("job_kind")=="transcribe_only":
        actions=[action for action in actions if action!="replan"]
    return {"available_actions": actions, "stage_sources": sources,
            "degradation_reasons": list(state.get("degradation_reasons") or []),
            "attempts": int(job.get("attempts") or 0), "generation": int(state.get("generation") or 0),
            "render_generation": int(state.get("render_generation") or 0),
            "retry_mode": state.get("retry_mode"), "failure_stage": state.get("failure_stage"),
            "attempt_history": list(state.get("attempt_history") or []),
            "llm_usage": state.get("llm_usage") or {"status": "unknown", "total_tokens": None}}


def _access():
    from . import studio_access
    return studio_access


def _worker_quota(job):
    access = _access()
    ledger = access.ledger()
    registered = ledger.get("job", str(job["id"]))
    project = ledger.get("project", str(job["project_id"]))
    source = ledger.get("source", str(job["object_key"]))
    if not registered or not project or not source or registered["project_id"] != job["project_id"] or registered["owner"] != project["owner"]:
        raise JobLifecycleError("E_JOB_SCOPE", "任务缺少可信项目/源片归属", 403)
    principal = {"subject": registered["owner"], "domain_id": access.DOMAIN_ID}
    ledger.reserve(principal, job["id"], int(source["bytes"]) * 2)
    ledger.renew(job["id"])


async def _locked_job(session, job_id):
    result = await session.execute(text("SELECT * FROM studio_jobs WHERE id=:id FOR UPDATE"), {"id": job_id})
    row = result.mappings().first()
    if row is None:
        raise JobLifecycleError("E_JOB_NOT_FOUND", "任务不存在", 404)
    return dict(row)


def _lease_valid(job, worker_id, lease_token):
    state = lifecycle(job)
    return (job.get("worker_id") == worker_id and
            str(state.get("lease_token") or "") == str(lease_token or "") and
            job.get("status") not in TERMINAL_STATUSES and job.get("status") not in {"queued", "reedit_requested"})


async def check_worker_lease(job_id, worker_id, lease_token=""):
    maker = get_session_maker()
    async with maker() as session:
        result = await session.execute(text("""SELECT * FROM studio_jobs WHERE id=:id AND worker_id=:worker_id
            AND lease_expires_at > NOW() AND COALESCE(config#>>'{_lifecycle,lease_token}','')=:lease_token
            AND status <> ALL(CAST(:terminal AS text[])) AND status NOT IN ('queued','reedit_requested')"""),
            {"id": job_id, "worker_id": worker_id, "lease_token": lease_token, "terminal": list(TERMINAL_STATUSES)})
        row = result.mappings().first()
    return dict(row) if row else None


async def request_job_operation(job_id, mode, idempotency_key):
    if mode not in RETRY_MODES | {"cancel"}:
        raise JobLifecycleError("E_RETRY_MODE", "未知重试意图")
    key = str(uuid.UUID(str(idempotency_key)))
    fingerprint = json.dumps({"mode": mode}, sort_keys=True)
    maker = get_session_maker()
    reserved = False
    try:
        async with maker() as session:
            job = await _locked_job(session, job_id)
            if job.get("deleted_at_utc") is not None:
                # 已删除项目的任务行是墓碑：绝不复活、绝不重试（2026-10-20 覆盖范围修复）。
                raise JobLifecycleError("E_PROJECT_DELETED", "任务所属项目已删除：行已打墓碑，禁止重试/恢复")
            config = _configuration(job)
            state = config.setdefault("_lifecycle", {})
            operations = state.setdefault("operations", {})
            previous = operations.get(key)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise JobLifecycleError("E_IDEMPOTENCY_CONFLICT", "相同请求键不能用于不同操作")
                return {**previous["response"], "reused": True}
            if mode not in job_capabilities(job)["available_actions"]:
                raise JobLifecycleError("E_JOB_OPERATION_CONFLICT", "当前状态不适用该操作")
            if len(operations) >= 512:
                raise JobLifecycleError("E_JOB_OPERATION_LIMIT", "本任务操作历史已达上限，请新建任务")
            if mode != "cancel":
                if mode != "cleanup_only":
                    from . import r2
                    if not r2.is_r2_enabled() and not r2.local_path_for(job["object_key"]).is_file():
                        raise JobLifecycleError("E_SOURCE_GONE", "原视频不可用，请重新导入", 410)
                _access().reserve_job_operation(job_id, job["project_id"], job["object_key"])
                reserved = True
            state["generation"] = int(state.get("generation") or 0) + 1
            state["lease_token"] = None
            state["retry_mode"] = mode
            state["claim_base"] = int(job.get("attempts") or 0)
            if mode == "replan":
                state["render_generation"] = int(state.get("render_generation") or 0) + 1
            status = "cancelled" if mode == "cancel" else "queued"
            stage = "用户已取消，旧执行器写入权限已撤销" if mode == "cancel" else f"等待执行：{mode}"
            response = {"job_id": job_id, "accepted": True, "status": status, "mode": mode,
                        "generation": state["generation"], "idempotency_key": key,
                        "dispatch": "database_claimable" if mode != "cancel" else "lease_revoked"}
            operations[key] = {"fingerprint": fingerprint, "response": response}
            await session.execute(text("""UPDATE studio_jobs SET status=:status,stage=:stage,progress=0,
                error=NULL,worker_id=NULL,lease_expires_at=NULL,config=CAST(:config AS JSONB),updated_at=NOW()
                WHERE id=:id"""), {"id":job_id,"status":status,"stage":stage,"config":json.dumps(config,ensure_ascii=False)})
            if mode=="cancel": _access().release_operation(job_id)
            await session.commit()
    except Exception:
        if reserved:
            _access().release_operation(job_id)
        raise
    if mode != "cancel":
        await _enqueue(job_id)
    await publish_event(job_id, status, stage, 0)
    return response


async def activate_job_run(job_id, worker_id, lease_token, plan_record, published):
    """取消和激活使用同一DB行锁；取消先提交时绝不激活文件台账。"""
    from . import reedit_replace
    from fastapi.concurrency import run_in_threadpool
    maker = get_session_maker()
    async with maker() as session:
        job = await _locked_job(session, job_id)
        valid = await session.execute(text("""SELECT id FROM studio_jobs WHERE id=:id
            AND worker_id=:worker_id AND lease_expires_at > NOW()
            AND COALESCE(config#>>'{_lifecycle,lease_token}','')=:lease_token
            AND status <> ALL(CAST(:terminal AS text[]))"""),
            {"id":job_id,"worker_id":worker_id,"lease_token":lease_token,"terminal":list(TERMINAL_STATUSES)})
        if valid.first() is None or not _lease_valid(job, worker_id, lease_token):
            raise JobLifecycleError("E_LEASE_LOST", "任务租约已撤销或换代")
        config = _configuration(job)
        if config.get("replacement_mode") == "delete_old_after_success":
            result = await run_in_threadpool(reedit_replace.activate_new_run, job["project_id"],
                                            render_run_id(job), plan_record, published=published)
            config.setdefault("_lifecycle", {})["activated_run_id"] = render_run_id(job)
        else:
            result = await run_in_threadpool(_register_rendered_clips, job, plan_record, published)
        await session.execute(text("UPDATE studio_jobs SET config=CAST(:config AS JSONB) WHERE id=:id"),
                              {"id":job_id,"config":json.dumps(config,ensure_ascii=False)})
        await session.commit()
    return {"accepted":True,"run_id":render_run_id(job),"clip_ids":list(result["clips"])}


def _register_rendered_clips(job, plan_record, published):
    """普通成片登记也在服务端任务行锁内，校验同run源产物与发布副本。"""
    import hashlib
    from . import r2, clip_render, studio_project_store
    from .studio_clip_locator import clip_video_path
    plans={str(c.get("clip_id")):c for c in plan_record.get("clips") or []}
    rows=[]
    for item in published:
        cid=str(item.get("clip_id") or "")
        if cid not in plans: raise JobLifecycleError("E_CLIP_PROOF", "发布切片不属于当前计划")
        try:
            original=clip_video_path(studio_project_store.TASKS_ROOT,render_run_id(job),cid)
            key=str(item["object_key"]).removeprefix("local:")
            if not key.startswith(f"studio-artifacts/{render_run_id(job)}/"):
                raise ValueError("foreign published run")
            copy_path=r2.local_path_for(item["object_key"])
            with original.open("rb") as stream: digest=hashlib.file_digest(stream,"sha256").hexdigest()
            if not copy_path.is_file() or copy_path.stat().st_size!=original.stat().st_size:
                raise ValueError("published bytes mismatch")
            with copy_path.open("rb") as stream: copied_digest=hashlib.file_digest(stream,"sha256").hexdigest()
            if copied_digest!=digest: raise ValueError("published bytes mismatch")
            if not clip_render._ffprobe_playable(copy_path)[0]: raise ValueError("unplayable")
        except (KeyError,ValueError,OSError) as exc:
            raise JobLifecycleError("E_CLIP_PROOF", "成片发布校验失败") from exc
        c=plans[cid]
        rows.append({"clip_id":cid,"topic":c.get("topic") or "", "source_start":float(c.get("start_ms") or 0)/1000,
            "source_end":float(c.get("end_ms") or 0)/1000,"duration":float(c.get("duration_ms") or 0)/1000})
    if rows: studio_project_store.register_clips(job["project_id"],render_run_id(job),rows)
    return {"clips":{row["clip_id"]:row for row in rows}}


def prepare_upload_session(session_id,project_id,size_bytes):
    access=_access();principal=access.current_principal();ledger=access.ledger()
    project=ledger.require(principal,"project",project_id,write=True)
    owner={"subject":project["owner"],"domain_id":access.DOMAIN_ID}
    ledger.reserve(owner,"upload:"+session_id,int(size_bytes)*2)
    access.register_created_object("upload",session_id,project_id,size_bytes=0)
