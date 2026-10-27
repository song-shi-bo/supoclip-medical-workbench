"""Studio Job 管线 API：R2 直传会话、任务提交、进度查询、产物下载、执行器内部端点."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError

from ... import r2
from ... import studio_job_store as lifecycle_store
from ...config import get_config
from ...studio_job_store import (
    get_upload_session,
    claim_next_job,
    complete_upload_session as store_complete_upload_session,
    create_job,
    create_upload_session as store_create_upload_session,
    get_job,
    list_artifacts,
    mark_worker_heartbeat,
    renew_lease,
    retry_failed_job,
    save_artifact,
    update_progress,
    worker_online,
)

router = APIRouter(prefix="/api")

_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
_WORKER_SECRET = os.getenv("WORKER_SHARED_SECRET", "")

# 执行器进度状态白名单（POST /internal/jobs/{job_id}/state 校验用）。
# 必须与 studio_job_worker._state(...) 发射的状态一一对应：worker 侧新增
# 状态而这里漏配时，执行器上报会被 422「未知任务状态」拒收（2026-10-16
# 曾因漏掉 uploading_reedit_artifacts 导致 reedit Step 3 进度上报全挂）。
ALLOWED_JOB_STATUSES: frozenset[str] = frozenset({
    "downloading", "extracting_audio", "transcribing", "correcting", "transcript_ready",
    "retrieving_rules", "extracting_highlights", "planning", "analysis_ready", "plan_ready",
    "analyzing", "editing", "rendering", "uploading",
    "completed", "completed_with_warnings", "partial_succeeded", "technical_failed",
    "reedit_requested", "staging_run", "verifying_outputs", "activating_run",
    "purging_old_clips", "completed_with_cleanup_warning",
    # 「重新剪辑并替换旧切片」Step 3：发布新切片产物（复制并登记任务 artifact）
    "uploading_reedit_artifacts",
    "failed", "cancelled",
})


def _require_worker(request: Request) -> None:
    if not _WORKER_SECRET:
        raise HTTPException(status_code=503, detail="执行器通道未配置 WORKER_SHARED_SECRET")
    if request.headers.get("X-Worker-Secret", "") != _WORKER_SECRET:
        raise HTTPException(status_code=401, detail="执行器凭据无效")


class UploadSessionRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    project_id: str = Field(min_length=1, max_length=64)
    content_type: str = Field(default="video/mp4", max_length=64)


class CompleteRequest(BaseModel):
    parts: list[dict] = Field(min_length=1, max_length=10000)


class JobSubmitRequest(BaseModel):
    idempotency_key: UUID | None = None
    object_key: str = Field(min_length=1, max_length=512)
    project_id: str = Field(min_length=1, max_length=64)
    config: dict = Field(default_factory=dict)
    mode: str = Field(default="transcribe_only", max_length=32)


class AutoEditJobRequest(BaseModel):
    """AUTO_EDIT 任务提交：必须引用已落盘的纠错转写（transcript 存储校验）。"""

    object_key: str = Field(min_length=1, max_length=512)
    project_id: str = Field(min_length=1, max_length=64)
    transcript_id: str = Field(min_length=1, max_length=128)
    transcript_version: int = Field(ge=1, le=1000000)
    config: dict = Field(default_factory=dict)
    idempotency_key: UUID | None = None


class JobStateRequest(BaseModel):
    status: str
    stage: str = ""
    progress: float = 0
    error: str | None = None
    stage_sources: dict | None = None
    degradation_reasons: list[str] | None = None
    llm_usage: dict | None = None


class RetryRequest(BaseModel):
    mode: Literal["resume_render", "replan", "cleanup_only"]
    idempotency_key: UUID


class CancelRequest(BaseModel):
    idempotency_key: UUID


class ActivateRequest(BaseModel):
    plan_record: dict
    published: list[dict]


def _lifecycle_http(exc):
    return HTTPException(status_code=exc.status_code, detail={"code":exc.code,"message":str(exc)})


async def _create_public_job(*args,**kwargs):
    try:
        return await create_job(*args,**kwargs)
    except lifecycle_store.JobLifecycleError as exc:
        raise _lifecycle_http(exc) from exc


def _submission_id(body, kind):
    if body.idempotency_key is None:
        return None
    from ...studio_access import current_principal
    principal=current_principal()
    return uuid.uuid5(uuid.NAMESPACE_URL,f"{principal['domain_id']}:{body.project_id}:{kind}:{body.idempotency_key}").hex


class ArtifactRequest(BaseModel):
    kind: str
    object_key: str = Field(min_length=1, max_length=512)
    clip_id: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None


@router.get("/uploads/capabilities")
async def upload_capabilities():
    """前端能力探测：决定直传 R2 还是回退本机分片流。"""
    return {
        "storage": r2.storage_mode(),
        "r2": r2.is_r2_enabled(),
        "part_size_mb": r2.R2_PART_SIZE_MB,
        "max_concurrent_parts": 4,
    }


@router.post("/uploads")
async def create_upload_session(request: Request):
    if not r2.is_r2_enabled():
        raise HTTPException(status_code=503, detail="R2 未配置：请回退本机分片上传流程")
    try:
        payload = await request.json()
        body = UploadSessionRequest(**payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"参数不合法: {exc.errors()[:2]}") from exc

    extension = Path(body.filename).suffix.lower() or ".mp4"
    if extension not in _VIDEO_EXTS:
        raise HTTPException(status_code=400, detail="Unsupported video format")

    if body.size_bytes > get_config().max_video_upload_bytes:
        raise HTTPException(status_code=413, detail="Uploaded file is too large")
    part_size = r2.part_size_bytes()
    parts_total = max(1, -(-body.size_bytes // part_size))
    if parts_total > 10000:
        raise HTTPException(status_code=400, detail="视频过大，请压缩后上传")

    file_id = uuid.uuid4().hex
    object_key = f"studio-source/{body.project_id}/{file_id}{extension}"
    session_id=uuid.uuid4().hex
    lifecycle_store.prepare_upload_session(session_id,body.project_id,body.size_bytes)
    try:
        r2_upload_id = r2.r2_create_multipart(object_key)
        await store_create_upload_session(
            body.project_id, object_key, body.filename, body.size_bytes, part_size, parts_total, r2_upload_id,
            session_id=session_id)
    except Exception:
        lifecycle_store._access().release_operation("upload:"+session_id)
        raise
    parts = [
        {"index": index, "url": r2.r2_presign_part(object_key, r2_upload_id, index + 1)}
        for index in range(parts_total)
    ]
    return {
        "upload_id": session_id,
        "object_key": object_key,
        "part_size": part_size,
        "parts_total": parts_total,
        "parts": parts,
        "mode": "r2",
    }


@router.post("/uploads/{session_id}/complete")
async def complete_upload_session(session_id: str, request: Request):
    try:
        payload = await request.json()
        body = CompleteRequest(**payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"参数不合法: {exc.errors()[:2]}") from exc
    session = await get_upload_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Upload session not found")
    if session["status"] != "open":
        raise HTTPException(status_code=400, detail="该上传会话已完成")
    parts = sorted(body.parts, key=lambda part: int(part.get("index", 0)))
    if [part.get("index") for part in parts] != list(range(session["parts_total"])):
        raise HTTPException(status_code=400, detail="分片编号不完整或重复")
    if any(not str(part.get("etag", "")).strip('"') for part in parts):
        raise HTTPException(status_code=400, detail="分片缺少 ETag")
    r2_parts = [
        {"ETag": str(part.get("etag", "")).strip('"'), "PartNumber": int(part.get("index", 0)) + 1}
        for part in parts
    ]
    lifecycle_store.prepare_upload_session(session_id,session["project_id"],session["size_bytes"])
    try:
        r2.r2_complete_multipart(session["object_key"], session["r2_upload_id"], r2_parts)
        head = r2.r2_head(session["object_key"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"R2 complete failed: {exc}") from exc
    if head["size"] != session["size_bytes"]:
        raise HTTPException(status_code=400, detail="上传文件大小与声明不符")
    await store_complete_upload_session(session_id,verified_size=head["size"])
    return {"ok": True, "object_key": session["object_key"], "size": head["size"]}


@router.post("/jobs/transcribe")
async def submit_transcribe_job(request: Request):
    """创建 TRANSCRIBE_ONLY 任务：只做 转写+纠错，终态 transcript_ready。

    「开始转写」绝不串完整自动剪辑链；自动剪辑由 POST /api/jobs/auto-edit 独立提交。
    """
    try:
        payload = await request.json()
        body = JobSubmitRequest(**payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"参数不合法: {exc.errors()[:2]}") from exc
    if body.mode != "transcribe_only":
        raise HTTPException(status_code=422, detail=f"未知任务类型 mode={body.mode!r}（仅支持 transcribe_only）")
    config = {**(body.config or {}), "job_kind": "transcribe_only"}
    job_id = await _create_public_job(body.project_id, body.object_key, None, config, job_id=_submission_id(body, "transcribe"))
    return {"job_id": job_id, "status": "queued", "job_kind": "transcribe_only"}


@router.post("/jobs/auto-edit")
async def submit_auto_edit_job(request: Request):
    """创建 AUTO_EDIT 任务：读取既有纠错转写做后半段，绝不重跑转写/纠错。

    前置条件：transcript 存储中存在 object_key 对应的有效纠错转写
    （transcript_id/version 可定位，或缺省取最新版本）；否则 422。
    """
    try:
        payload = await request.json()
        body = AutoEditJobRequest(**payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"参数不合法: {exc.errors()[:2]}") from exc
    from ...studio_transcript_store import load_corrected_transcript

    document = await run_in_threadpool(
        load_corrected_transcript, body.object_key,
        transcript_id=body.transcript_id, transcript_version=body.transcript_version)
    if document is None:
        raise HTTPException(status_code=422, detail="请先完成转写与纠错。")
    # 第五步（2026-10-14）：正式 AUTO_EDIT 不带 stop_after——分析→规划→渲染全自动；
    # 显式传 stop_after=analysis/plan 仅用于测试或开发诊断。
    config = {**(body.config or {}), "job_kind": "auto_edit",
              "transcript_id": body.transcript_id, "transcript_version": body.transcript_version}
    if config.get("analysis_only") and not config.get("stop_after"):
        config["stop_after"] = "analysis"
    config.pop("analysis_only", None)
    job_id = await _create_public_job(body.project_id, body.object_key, None, config, job_id=_submission_id(body, "auto_edit"))
    return {"job_id": job_id, "status": "queued", "job_kind": "auto_edit"}


class ReEditJobRequest(BaseModel):
    """「重新剪辑并替换旧切片」：复用源片与指定版本转写，成功后永久删除旧切片。"""

    object_key: str = Field(min_length=1, max_length=512)
    project_id: str = Field(min_length=1, max_length=64)
    transcript_id: str = Field(min_length=1, max_length=128)
    transcript_version: int = Field(ge=1, le=1000000)
    reuse_transcript: bool = True
    force_new_run: bool = True
    replacement_mode: str = Field(default="delete_old_after_success", max_length=48)
    idempotency_key: UUID | None = None


@router.post("/jobs/re-edit")
async def submit_reedit_job(request: Request):
    """创建「重新剪辑并替换旧切片」任务：新 run_id，成功后激活新批次并清除旧切片。"""
    try:
        payload = await request.json()
        body = ReEditJobRequest(**payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"参数不合法: {exc.errors()[:2]}") from exc
    if not body.reuse_transcript or not body.force_new_run:
        raise HTTPException(status_code=422, detail="重新剪辑必须复用转写并强制新建运行。")
    if body.replacement_mode != "delete_old_after_success":
        raise HTTPException(status_code=422, detail=f"未知替换模式 replacement_mode={body.replacement_mode!r}")
    from ...studio_transcript_store import load_corrected_transcript

    document = await run_in_threadpool(
        load_corrected_transcript, body.object_key,
        transcript_id=body.transcript_id, transcript_version=body.transcript_version)
    if document is None:
        raise HTTPException(status_code=422, detail="请先完成转写与纠错。")
    idempotency_key = str(body.idempotency_key or uuid.uuid4())
    config = {"job_kind": "auto_edit", "replacement_mode": body.replacement_mode,
              "reuse_transcript": True, "force_new_run": True,
              "transcript_id": body.transcript_id, "transcript_version": body.transcript_version,
              "idempotency_key": idempotency_key}
    job_id = await _create_public_job(body.project_id, body.object_key, None, config, job_id=_submission_id(body, "reedit"))
    from ...studio_job_store import mark_reedit_requested
    await mark_reedit_requested(job_id)
    return {"job_id": job_id, "run_id": job_id, "status": "reedit_requested",
            "idempotency_key": idempotency_key}


@router.get("/jobs/{job_id}")
async def job_status(job_id: str):
    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    queued_waiting = job["status"] == "queued" and not await worker_online()
    artifacts = []
    for artifact in await list_artifacts(job_id):
        item = {
            "id": artifact["id"], "kind": artifact["kind"], "clip_id": artifact["clip_id"],
            "object_key": artifact["object_key"], "size_bytes": artifact["size_bytes"],
        }
        url = r2.presign_get(artifact["object_key"])
        if url:
            item["url"] = url
        else:
            item["url"] = f"/api/jobs/{job_id}/artifacts/{artifact['id']}/file"
        artifacts.append(item)
    return {
        "job_id": job_id,
        "job_kind": lifecycle_store._configuration(job).get("job_kind", "legacy_full"),
        "status": job["status"],
        "stage": job["stage"],
        "progress": job["progress"],
        "error": job["error"],
        "worker_online": await worker_online(),
        "waiting_for_executor": queued_waiting,
        "artifacts": artifacts,
        **lifecycle_store.job_capabilities(job),
    }


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str, body: RetryRequest):
    try:
        return await lifecycle_store.request_job_operation(job_id,body.mode,body.idempotency_key)
    except lifecycle_store.JobLifecycleError as exc:
        raise _lifecycle_http(exc) from exc



@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, body: CancelRequest):
    try:
        return await lifecycle_store.request_job_operation(job_id,"cancel",body.idempotency_key)
    except lifecycle_store.JobLifecycleError as exc:
        raise _lifecycle_http(exc) from exc


@router.get("/jobs/{job_id}/artifacts/{artifact_id}/file")
async def job_artifact_file(job_id: str, artifact_id: str):
    artifacts = await list_artifacts(job_id)
    artifact = next((item for item in artifacts if item["id"] == artifact_id), None)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    url = r2.presign_get(artifact["object_key"])
    if url:
        from fastapi.responses import RedirectResponse

        return RedirectResponse(url, status_code=307)
    path = r2.local_path_for(artifact["object_key"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Artifact file missing")
    media_type = "video/mp4" if path.suffix.lower() == ".mp4" else "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else None
    if path.suffix.lower()==".srt":
        media_type="application/x-subrip; charset=utf-8"
    from fastapi.responses import FileResponse
    return FileResponse(path, media_type=media_type, filename=path.name if path.suffix.lower()==".srt" else None)


# ---------------------------------------------------------------------------
# 执行器内部端点（WORKER_SHARED_SECRET 保护）
# ---------------------------------------------------------------------------

@router.post("/internal/heartbeat")
async def internal_heartbeat(request: Request):
    _require_worker(request)
    worker_id = request.headers.get("X-Worker-Id", "worker")
    await mark_worker_heartbeat(worker_id)
    payload = await request.json()
    job_id = payload.get("job_id")
    if job_id and not await renew_lease(job_id, worker_id, request.headers.get("X-Worker-Lease", "")):
        raise HTTPException(status_code=409, detail="任务租约不属于当前执行器")
    return {"ok": True}


@router.post("/internal/jobs/claim")
async def internal_claim(request: Request):
    _require_worker(request)
    worker_id = request.headers.get("X-Worker-Id", "worker")
    await mark_worker_heartbeat(worker_id)
    job = await claim_next_job(worker_id)
    if job is None:
        return {"job": None}
    payload = {
        "job": {
            "job_id": job["id"],
            "project_id": job["project_id"],
            "object_key": job["object_key"],
            "config": job["config"] if isinstance(job["config"], dict) else {},
            "lease_token": lifecycle_store.lifecycle(job).get("lease_token"),
            "generation": lifecycle_store.lifecycle(job).get("generation",0),
            "render_run_id": lifecycle_store.render_run_id(job),
        }
    }
    return payload


@router.post("/internal/jobs/{job_id}/state")
async def internal_job_state(job_id: str, request: Request):
    _require_worker(request)
    try:
        payload = await request.json()
        body = JobStateRequest(**payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"参数不合法: {exc.errors()[:2]}") from exc
    # 白名单见模块级 ALLOWED_JOB_STATUSES（reedit 流程状态、清理警告终态等都在内）。
    # 校验顺序保持：422 状态白名单先于 409 租约校验。
    if body.status not in ALLOWED_JOB_STATUSES:
        raise HTTPException(status_code=422, detail="未知任务状态")
    if not await update_progress(job_id, body.status, body.stage, body.progress, body.error,
                                 worker_id=request.headers.get("X-Worker-Id", ""),
                                 lease_token=request.headers.get("X-Worker-Lease", ""), metadata=body.model_dump(exclude_none=True)):
        raise HTTPException(status_code=409, detail="任务租约失效或不属于当前执行器")
    return {"ok": True}


@router.post("/internal/jobs/{job_id}/artifacts")
async def internal_job_artifact(job_id: str, request: Request):
    _require_worker(request)
    try:
        payload = await request.json()
        body = ArtifactRequest(**payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"参数不合法: {exc.errors()[:2]}") from exc
    artifact_id = await save_artifact(
        job_id, body.kind, body.object_key, body.size_bytes or 0, body.clip_id, body.sha256,
        worker_id=request.headers.get("X-Worker-Id", ""),
        lease_token=request.headers.get("X-Worker-Lease", "")
    )
    if artifact_id is None:
        raise HTTPException(status_code=409, detail="任务租约失效或不属于当前执行器")
    return {"ok": True, "artifact_id": artifact_id}


@router.get("/internal/jobs/{job_id}/artifacts")
async def internal_job_artifacts(job_id: str, request: Request):
    _require_worker(request)
    job=await lifecycle_store.check_worker_lease(job_id,request.headers.get("X-Worker-Id",""),request.headers.get("X-Worker-Lease",""))
    if job is None:
        raise HTTPException(409,"任务租约已失效")
    return {"artifacts":await list_artifacts(job_id),"render_run_id":lifecycle_store.render_run_id(job)}


@router.post("/internal/jobs/{job_id}/activate")
async def internal_activate_job(job_id: str, body: ActivateRequest, request: Request):
    _require_worker(request)
    try:
        return await lifecycle_store.activate_job_run(job_id,request.headers.get("X-Worker-Id",""),
            request.headers.get("X-Worker-Lease",""),body.plan_record,body.published)
    except lifecycle_store.JobLifecycleError as exc:
        raise _lifecycle_http(exc) from exc
