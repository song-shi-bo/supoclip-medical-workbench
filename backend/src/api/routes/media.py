"""
Media API routes (fonts, transitions, uploads).
"""

from pydantic import BaseModel, Field, ValidationError
from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path
from typing import Any, cast
import logging
import uuid
import asyncio
import json
import hashlib
import threading
import shutil
import time
import re
import aiofiles
import os
import subprocess
import threading
from starlette.requests import ClientDisconnect
from ...studio_runtime import process_lock
from ...capacity_guard import reserve_capacity, estimate_upload_peak, CapacityInsufficientError
from ...studio_access import (
    current_principal, ledger as access_ledger, register_created_object,
    upload_part_quota, record_upload_part, complete_upload_accounting, heavy_operation,
)

from ...config import get_config
from ...database import get_db
from ...auth_headers import resolve_authenticated_user_id
from ...services.billing_service import BillingService
from ...font_registry import (
    FONTS_DIR,
    SUPPORTED_FONT_EXTENSIONS,
    build_user_font_stem,
    find_font_path,
    find_user_font_path,
    get_available_fonts as list_available_fonts,
    get_user_fonts_dir,
    sanitize_font_stem,
)
from ...video_utils import transcribe_with_faster_whisper
from ...transcript_correction import correct_segments
from ...studio_insights import extract_studio_insights
from ...studio_auto_edit_bridge import (
    project_source_path,
    read_auto_edit_task,
    read_clip_transform,
    rerender_auto_edit_task,
    resolve_auto_edit_artifact,
    save_clip_transform,
    submit_auto_edit_task,
)
from ...studio_transform_export import export_transformed_clip
from ...preview_proxy import status as preview_proxy_status, start_build as start_preview_proxy
from ...cover_replace import (
    CoverReplaceError,
    E_COVER_CLIP_MISSING,
    SUPPORTED_COVER_STYLES,
    adopt_cover,
    list_project_clips,
    read_cover_state,
    replace_cover,
    resolve_ffmpeg,
    restore_cover,
)
from ...studio_project_store import (
    ProjectStoreError,
    bump_render_revision as project_bump_render_revision,
    entry_for as project_entry_for,
    list_cards,
    record_task as project_record_task,
    sync_from_tasks,
    update_fields as project_update_fields,
)
from ... import clip_audio, clip_config, subtitle_compose, transcript_revisions
# 清空项目任务数据：删任务记录与产物、保留源片与转写（见 studio_project_purge 的模块注释）。
from ... import studio_project_purge
# 底部时间线 Phase B：视图设置 / 编辑历史 / 音轨（Phase A 地基，纯函数搬运）。
# timeline_view 只描述“怎么看”，timeline_history 描述“改了什么”，二者都不进渲染/导出。
from ... import timeline_audio, timeline_history, timeline_view
from ... import studio_project_store as project_store
from ... import project_lifecycle as lifecycle
from ...studio_local_tasks import (
    TASK_AUTO_EDIT,
    TASK_REGENERATE_COVER,
    TASK_TYPES,
    TASK_UPDATE_AUDIO,
    TASK_UPDATE_TIMELINE,
    TASKS_ROOT,
    LocalTaskError,
    activate_cover,
    clip_segments_from_render_task,
    cover_history,
    create_task as create_local_task,
    delete_cover,
    effective_transcript_segments,
    get_task as get_local_task,
    list_tasks as list_local_tasks,
    merge_subtitle_overrides,
    subtitle_overrides_from_cues,
    task_type_of,
    undo_cover as undo_cover_revision,
)
from ... import skill_registry, studio_workflow
# 封面版型库（2026-10-21 阶段）：版本化注册表 + 模拟结果 stub；不调用模型、不生成真实封面。
from ... import cover_templates
from ...timeline_copilot import (
    create_timeline_project,
    load_timeline_project,
    update_timeline_project,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from pydantic import ValidationError
from fastapi import Depends

logger = logging.getLogger(__name__)

# 复渲染可选风格白名单（任务 JSON 里记 style_id，执行器透传给 video-brain）。
ALL_RERENDER_STYLES = frozenset(SUPPORTED_COVER_STYLES)
router = APIRouter(tags=["media"])
MAX_FONT_UPLOAD_BYTES = 10 * 1024 * 1024


async def _get_authenticated_user_id(request: Request, db: AsyncSession) -> str:
    config = get_config()
    return await resolve_authenticated_user_id(request, db, config)


async def _write_upload_to_disk_reserved(
    uploaded_file: UploadFile,
    target_path: Path,
    max_bytes: int,
) -> None:
    chunk_size = 1024 * 1024
    written = 0

    try:
        async with aiofiles.open(target_path, "wb") as destination:
            while True:
                chunk = await uploaded_file.read(chunk_size)
                if not chunk:
                    break

                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413, detail="Uploaded file is too large"
                    )

                await destination.write(chunk)
    except Exception:
        if target_path.exists():
            target_path.unlink(missing_ok=True)
        raise


async def _write_upload_to_disk(uploaded_file, target_path, max_bytes):
    declared = getattr(uploaded_file, "size", None)
    size = min(max_bytes, declared) if declared is not None else max_bytes
    try:
        with reserve_capacity("upload", estimate_upload_peak(size), path=target_path.parent):
            target_path.parent.mkdir(parents=True, exist_ok=True)
            return await _write_upload_to_disk_reserved(uploaded_file, target_path, max_bytes)
    except CapacityInsufficientError as exc:
        raise HTTPException(status_code=507, detail=exc.as_dict()) from exc


@router.get("/fonts")
async def get_available_fonts_route(
    request: Request, db: AsyncSession = Depends(get_db)
):
    """Get list of available fonts."""
    try:
        user_id = await _get_authenticated_user_id(request, db)
        if not FONTS_DIR.exists():
            return {"fonts": [], "message": "Fonts directory not found"}

        fonts = list_available_fonts(user_id=user_id)
        logger.info(f"Found {len(fonts)} available fonts")
        return {"fonts": fonts}

    except Exception as e:
        logger.error(f"Error retrieving fonts: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error retrieving fonts: {str(e)}")


@router.get("/fonts/{font_name}")
async def get_font_file(
    font_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Serve a specific font file."""
    try:
        user_id = await _get_authenticated_user_id(request, db)
        font_path = find_font_path(font_name, user_id=user_id)

        if not font_path:
            raise HTTPException(status_code=404, detail="Font not found")

        media_type = "font/ttf" if font_path.suffix.lower() == ".ttf" else "font/otf"

        return FileResponse(
            path=str(font_path),
            media_type=media_type,
            headers={
                "Cache-Control": "public, max-age=31536000",
                "Access-Control-Allow-Origin": "*",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving font {font_name}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error serving font: {str(e)}")


@router.post("/fonts/upload")
async def upload_font(
    request: Request,
    uploaded_file: UploadFile = File(..., alias="file"),
    db: AsyncSession = Depends(get_db),
):
    """Upload a custom .ttf/.otf font so it appears in the font picker."""
    try:
        user_id = await _get_authenticated_user_id(request, db)
        billing_service = BillingService(db)
        summary = await billing_service.get_usage_summary(user_id)
        paid_access = not summary.get("monetization_enabled") or (
            summary.get("plan") in {"pro", "scale"}
            and summary.get("subscription_status") in {"active", "trialing"}
        )
        if not paid_access:
            raise HTTPException(
                status_code=403,
                detail="Custom font uploads are available for paid plans only",
            )

        if not uploaded_file.filename:
            raise HTTPException(status_code=400, detail="Missing file name")

        uploaded_filename = uploaded_file.filename or "font.ttf"
        extension = Path(uploaded_filename).suffix.lower()
        if extension not in SUPPORTED_FONT_EXTENSIONS:
            raise HTTPException(
                status_code=400, detail="Only .ttf and .otf fonts are supported"
            )

        user_fonts_dir = get_user_fonts_dir(user_id)
        user_fonts_dir.mkdir(parents=True, exist_ok=True)

        # 账号级字体数量限额（多账号阶段四补充）：默认 50 个/账号，env 可调。
        max_fonts = max(1, int(os.getenv("FONT_MAX_PER_USER", "50")))
        existing_fonts = [p for p in user_fonts_dir.glob("*") if p.is_file()]
        if len(existing_fonts) >= max_fonts:
            raise HTTPException(
                status_code=429,
                detail={"code": "E_FONT_QUOTA", "limit": max_fonts,
                        "message": f"每账号字体数量已达上限（{max_fonts}），请删除部分字体后再上传"},
            )

        original_stem = sanitize_font_stem(uploaded_filename)
        stored_stem = build_user_font_stem(user_id, original_stem)
        target_path = user_fonts_dir / f"{stored_stem}{extension}"
        suffix = 2
        while target_path.exists():
            target_path = user_fonts_dir / f"{stored_stem}-{suffix}{extension}"
            suffix += 1

        await _write_upload_to_disk(uploaded_file, target_path, MAX_FONT_UPLOAD_BYTES)

        logger.info(f"Uploaded font: {target_path.name}")

        return {
            "font": {
                "name": target_path.stem,
                "display_name": original_stem.replace("-", " ")
                .replace("_", " ")
                .title(),
                "filename": target_path.name,
                "format": extension.lstrip("."),
                "scope": "user",
            },
            "message": "Font uploaded successfully",
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error uploading font: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error uploading font: {str(e)}")


@router.delete("/fonts/{font_name}")
async def delete_font(
    font_name: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """Delete a custom font owned by the authenticated user."""
    try:
        user_id = await _get_authenticated_user_id(request, db)
        font_path = find_user_font_path(font_name, user_id)

        if font_path is None:
            if find_font_path(font_name) is not None:
                raise HTTPException(
                    status_code=403, detail="Bundled system fonts cannot be deleted"
                )
            raise HTTPException(status_code=404, detail="Custom font not found")

        deleted_name = font_path.stem
        font_path.unlink(missing_ok=True)
        logger.info("Deleted custom font %s for user %s", font_path.name, user_id)
        return {"font_name": deleted_name, "message": "Font deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error deleting font %s: %s", font_name, e)
        raise HTTPException(status_code=500, detail=f"Error deleting font: {str(e)}")


@router.get("/transitions")
async def get_available_transitions():
    """Get list of available transition effects."""
    try:
        from ...video_utils import get_available_transitions

        transitions = get_available_transitions()

        transition_info = []
        for transition_path in transitions:
            transition_file = Path(transition_path)
            transition_info.append(
                {
                    "name": transition_file.stem,
                    "display_name": transition_file.stem.replace("_", " ")
                    .replace("-", " ")
                    .title(),
                    "file_path": transition_path,
                }
            )

        logger.info(f"Found {len(transition_info)} available transitions")
        return {"transitions": transition_info}

    except Exception as e:
        logger.error(f"Error retrieving transitions: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Error retrieving transitions: {str(e)}"
        )


@router.get("/caption-templates")
async def get_caption_templates():
    """Get available caption templates.

    Returns a stable default list if optional template module is unavailable.
    """
    default_templates = [
        {
            "id": "default",
            "name": "Default",
            "description": "Clean subtitle style",
            "animation": "none",
            "font_family": "TikTokSans-Regular",
            "font_size": 24,
            "font_color": "#FFFFFF",
        }
    ]

    try:
        from ...caption_templates import get_template_info

        templates = get_template_info()
        return {"templates": templates or default_templates}
    except Exception:
        return {"templates": default_templates}


@router.get("/broll/status")
async def get_broll_status():
    """Return whether B-roll integrations are configured."""
    config = get_config()
    return {
        "configured": bool(config.pexels_api_key),
        "provider": "pexels" if config.pexels_api_key else None,
    }


@router.post("/upload")
async def upload_video(request: Request, db: AsyncSession = Depends(get_db)):
    """Upload a video to the server."""
    try:
        await _get_authenticated_user_id(request, db)
        config = get_config()

        # Get the form data
        form_data = await request.form()
        video_file = cast(Any, form_data.get("video"))

        if not getattr(video_file, "filename", None) or not hasattr(video_file, "read"):
            raise HTTPException(status_code=400, detail="No video file provided")

        upload = cast(UploadFile, video_file)
        upload_filename = upload.filename or "upload.mp4"

        # Create uploads directory
        uploads_dir = Path(config.temp_dir) / "uploads"

        # Generate unique filename
        file_extension = Path(upload_filename).suffix
        unique_filename = f"{uuid.uuid4()}{file_extension}"
        video_path = uploads_dir / unique_filename

        # Save the uploaded file
        max_bytes = get_config().max_video_upload_bytes
        await _write_upload_to_disk(upload, video_path, max_bytes)

        logger.info(f"✅ Video uploaded successfully to: {video_path}")

        return {
            "message": "Video uploaded successfully",
            "video_path": f"upload://{unique_filename}",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error uploading video: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error uploading video: {str(e)}")


@router.post("/studio/transcribe")
async def studio_transcribe_video(
    request: Request,
    uploaded_file: UploadFile = File(..., alias="video"),
):
    """Transcribe a local Studio upload without an account session.

    Docker exposes this backend only on 127.0.0.1 in the local workspace, and
    the endpoint is consumed by the Studio's same-machine proxy. It deliberately
    returns raw ASR segments instead of creating a clipping task.
    """
    if not uploaded_file.filename:
        raise HTTPException(status_code=400, detail="No video file provided")

    config = get_config()
    extension = Path(uploaded_file.filename).suffix.lower() or ".mp4"
    if extension not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
        raise HTTPException(status_code=400, detail="Unsupported video format")

    uploads_dir = Path(config.temp_dir) / "studio-uploads"
    video_path = uploads_dir / f"{uuid.uuid4()}{extension}"
    operation_id = "legacy-transcribe:" + uuid.uuid4().hex
    access = access_ledger(request)
    access.reserve(current_principal(request), operation_id,
                   int(uploaded_file.size or config.max_video_upload_bytes) * 2, ttl_ms=86400000)

    try:
        await _write_upload_to_disk(
            uploaded_file, video_path, config.max_video_upload_bytes
        )
        transcript = await run_in_threadpool(
            transcribe_with_faster_whisper, video_path, config
        )
        raw_segments = [
            {
                "start": round(float(segment["start"]), 3),
                "end": round(float(segment["end"]), 3),
                "text": str(segment["text"]).strip(),
            }
            for segment in transcript.get("segments", [])
            if str(segment.get("text", "")).strip()
        ]
        correction = correct_segments(raw_segments)
        insights = await extract_studio_insights(correction["asr_segments"])
        video_path.with_suffix(".corrected_transcript.json").write_text(
            json.dumps(correction["asr_segments"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        video_path.with_suffix(".correction_audit.json").write_text(
            json.dumps(correction["correction_audit"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        timeline = await run_in_threadpool(
            create_timeline_project,
            video_path=video_path,
            raw_segments=transcript.get("segments", []),
            corrected_segments=correction["asr_segments"],
            uploads_root=Path(config.temp_dir),
        )
        project_id = str(timeline.get("project_id") or "")
        register_created_object("project", project_id)
        register_created_object("source", f"local:{video_path}", project_id, size_bytes=video_path.stat().st_size)
        # Phase B：原声自动进时间线（seed_source_tracks + push(label="导入")）。
        # 只写 timeline/history.json，不碰 EDL / 不触发转码导出。
        await run_in_threadpool(
            _seed_timeline_source_audio, Path(config.temp_dir), timeline)
        return {
            **correction,
            "raw_asr_segments": raw_segments,
            "language": transcript.get("language", "zh"),
            "profile": {
                "engine": "faster-whisper",
                "model": config.faster_whisper_model,
                "device": config.faster_whisper_device,
                "compute_type": config.faster_whisper_compute_type,
                "vad_filter": config.faster_whisper_vad_filter,
            },
            "insights": insights,
            "timeline": timeline,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Studio transcription failed")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}")
    finally:
        access.release(operation_id)


# ---------------------------------------------------------------------------
# 异步转写：公网隧道（Cloudflare 免费版）有 100s 代理超时，同步转写（150s+）
# 会被边缘截断。改为“立即返回 job_id + 轮询取结果”，与自动剪辑的任务模式一致。
# ---------------------------------------------------------------------------

_TRANSCRIBE_JOBS: dict[str, dict[str, Any]] = {}
_TRANSCRIBE_JOBS_LOCK = threading.Lock()
_TRANSCRIBE_JOB_TTL_SECONDS = 3600


def _execute_transcription_job(video_path: Path, config: Any) -> dict[str, Any]:
    """在后台线程中执行完整转写流程（与同步版完全一致的产物结构）。"""
    transcript = transcribe_with_faster_whisper(video_path, config)
    raw_segments = [
        {
            "start": round(float(segment["start"]), 3),
            "end": round(float(segment["end"]), 3),
            "text": str(segment["text"]).strip(),
        }
        for segment in transcript.get("segments", [])
        if str(segment.get("text", "")).strip()
    ]
    correction = correct_segments(raw_segments)
    insights = asyncio.run(extract_studio_insights(correction["asr_segments"]))
    video_path.with_suffix(".corrected_transcript.json").write_text(
        json.dumps(correction["asr_segments"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    video_path.with_suffix(".correction_audit.json").write_text(
        json.dumps(correction["correction_audit"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    timeline = create_timeline_project(
        video_path=video_path,
        raw_segments=transcript.get("segments", []),
        corrected_segments=correction["asr_segments"],
        uploads_root=Path(config.temp_dir),
    )
    # Phase B：原声自动进时间线（与同步端点同一落地口径，幂等）。
    _seed_timeline_source_audio(Path(config.temp_dir), timeline)
    return {
        **correction,
        "raw_asr_segments": raw_segments,
        "language": transcript.get("language", "zh"),
        "profile": {
            "engine": "faster-whisper",
            "model": config.faster_whisper_model,
            "device": config.faster_whisper_device,
            "compute_type": config.faster_whisper_compute_type,
            "vad_filter": config.faster_whisper_vad_filter,
        },
        "insights": insights,
        "timeline": timeline,
    }


def _transcribe_job_worker(job_id: str, video_path: Path, filename: str, owner: dict, acl_path: str) -> None:
    from ...studio_access import AccessLedger
    access = AccessLedger(acl_path)
    try:
        config = get_config()
        result = _execute_transcription_job(video_path, config)
        project_id = str((result.get("timeline") or {}).get("project_id") or "")
        register_created_object("project", project_id, principal=owner, access=access)
        # 同一job/source初建时尚无timeline；明确绑定可信worker实际产物。
        access.attach(owner, "job", job_id, project_id)
        access.attach(owner, "source", f"local:{video_path}", project_id)
        # 统一项目入口（2026-10-19）：timeline 建成后立即创建空台账——项目导入完成
        # 即出现在项目管理（0 成片也可见）；重复调用幂等。owner_subject 取自认证
        # 主体（trusted），租户由来源派生，不接受客户端声明。
        from src.project_registry import ensure_project
        ensured = ensure_project(
            project_id, origin="import",
            source_path=video_path,
            raw_segments=result.get("raw_asr_segments") or [],
            corrected_segments=result.get("asr_segments") or [],
            uploads_root=Path(config.temp_dir),
            owner_subject=str((owner or {}).get("subject") or "") or None,
        )
        if ensured["state"] not in {"created", "reused"}:
            logger.warning("导入后项目记录异常 %s: state=%s detail=%s",
                           project_id, ensured["state"], ensured.get("detail"))
        with _TRANSCRIBE_JOBS_LOCK:
            job = _TRANSCRIBE_JOBS.get(job_id)
            if job is not None:
                job.update(status="done", result=result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Async studio transcription failed")
        with _TRANSCRIBE_JOBS_LOCK:
            job = _TRANSCRIBE_JOBS.get(job_id)
            if job is not None:
                job.update(status="error", error=str(exc)[:400])
    finally:
        access.release(job_id)


@router.post("/studio/transcribe/start")
async def studio_transcribe_start(
    request: Request,
    uploaded_file: UploadFile = File(..., alias="video"),
):
    """接收上传并立即返回 job_id；转写在后台线程执行，前端轮询取结果。"""
    if not uploaded_file.filename:
        raise HTTPException(status_code=400, detail="No video file provided")
    config = get_config()
    extension = Path(uploaded_file.filename).suffix.lower() or ".mp4"
    if extension not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
        raise HTTPException(status_code=400, detail="Unsupported video format")

    uploads_dir = Path(config.temp_dir) / "studio-uploads"
    video_path = uploads_dir / f"{uuid.uuid4()}{extension}"
    job_id = uuid.uuid4().hex
    owner = current_principal(request)
    access = access_ledger(request)
    access.reserve(owner, job_id, int(uploaded_file.size or config.max_video_upload_bytes) * 2, ttl_ms=86400000)
    try:
        await _write_upload_to_disk(uploaded_file, video_path, config.max_video_upload_bytes)
        register_created_object("job", job_id)
        register_created_object("source", f"local:{video_path}", size_bytes=video_path.stat().st_size)
    except CapacityInsufficientError as exc:
        # D11 全站磁盘水位：拒绝并释放 ACL 预留（与其他上传入口一致映射 507）
        access.release(job_id)
        raise HTTPException(status_code=507, detail=exc.as_dict()) from exc
    except Exception:
        access.release(job_id)
        raise
    with _TRANSCRIBE_JOBS_LOCK:
        # 顺手清理过期任务，避免长期驻留内存。
        stale = [key for key, job in _TRANSCRIBE_JOBS.items()
                 if time.time() - job.get("created_at", 0) > _TRANSCRIBE_JOB_TTL_SECONDS]
        for key in stale:
            _TRANSCRIBE_JOBS.pop(key, None)
        _TRANSCRIBE_JOBS[job_id] = {
            "job_id": job_id,
            "status": "processing",
            "created_at": time.time(),
            "filename": uploaded_file.filename,
        }
    thread = threading.Thread(
        target=_transcribe_job_worker,
        args=(job_id, video_path, uploaded_file.filename or "video.mp4", owner, str(access_ledger(request).path)),
        daemon=True,
        name=f"studio-transcribe-{job_id[:8]}",
    )
    try:
        thread.start()
    except Exception as exc:
        access.release(job_id)
        with _TRANSCRIBE_JOBS_LOCK:
            _TRANSCRIBE_JOBS[job_id].update(status="error", error="无法启动转写执行器")
        raise HTTPException(status_code=503, detail="无法启动转写执行器，可稍后重试") from exc
    return {"job_id": job_id}


def _chunk_directory(upload_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id or ""):
        raise HTTPException(status_code=400, detail="upload_id 非法")
    return Path(get_config().temp_dir) / "studio-chunks" / upload_id


@router.get("/studio/transcribe/chunks/{upload_id}")
async def studio_transcribe_chunks(upload_id: str):
    directory = _chunk_directory(upload_id)
    receipt = directory / "receipt.json"
    if receipt.is_file():
        return json.loads(receipt.read_text())
    return {"parts": [{"index": int(part.stem), "bytes": part.stat().st_size}
                      for part in sorted(directory.glob("*.part")) if part.stem.isdigit()]}


async def _studio_transcribe_chunk_reserved(request: Request):
    directory = _chunk_directory(request.query_params.get("upload_id", ""))
    index_raw = request.query_params.get("index", "")
    if not index_raw.isdigit() or not 0 <= int(index_raw) < 20000:
        raise HTTPException(status_code=400, detail="index 非法")
    if (directory / "receipt.json").exists():
        raise HTTPException(status_code=409, detail="上传已完成")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f"{index_raw}.{uuid.uuid4().hex}.uploading"
    size = 0
    try:
        async with aiofiles.open(temporary, "wb") as output:
            async for block in request.stream():
                size += len(block)
                if size > 8 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="分片超过 8MB")
                await output.write(block)
        if not size:
            raise HTTPException(status_code=400, detail="分片为空")
        with process_lock(directory / ".assembly.lock", blocking=False):
            if (directory / "receipt.json").exists():
                raise HTTPException(status_code=409, detail="上传已完成")
            os.replace(temporary, directory / f"{int(index_raw):06d}.part")
    except ClientDisconnect as exc:
        raise HTTPException(status_code=400, detail="上传连接中断，可重试当前分片") from exc
    except BlockingIOError as exc:
        raise HTTPException(status_code=409, detail="正在合并视频，请稍后重试") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return {"ok": True, "index": int(index_raw), "bytes": size}


@router.post("/studio/transcribe/chunk")
async def studio_transcribe_chunk(request: Request):
    directory = _chunk_directory(request.query_params.get("upload_id", ""))
    try:
        index = int(request.query_params.get("index", "-1"))
    except ValueError:
        raise HTTPException(status_code=400, detail="index 非法") from None
    try:
        with upload_part_quota(directory.name, index):
            with reserve_capacity("upload_chunk", estimate_upload_peak(8 * 1024 * 1024), path=directory):
                result = await _studio_transcribe_chunk_reserved(request)
                record_upload_part(directory.name, index, result["bytes"])
                return result
    except CapacityInsufficientError as exc:
        raise HTTPException(status_code=507, detail=exc.as_dict()) from exc


def _assemble_parts(directory: Path, video_path: Path, total: int, maximum: int, expected_size: int | None):
    expected = [directory / f"{index:06d}.part" for index in range(total)]
    if any(not part.is_file() for part in expected):
        raise HTTPException(status_code=400, detail="分片不完整，请继续上传")
    size = sum(part.stat().st_size for part in expected)
    if size > maximum:
        raise HTTPException(status_code=413, detail="Uploaded file is too large")
    if expected_size is not None and size != expected_size:
        raise HTTPException(status_code=400, detail="视频大小与声明不符，请重试上传")
    try:
        with reserve_capacity("assemble", estimate_upload_peak(size, merging=True), path=video_path.parent):
            return _assemble_parts_reserved(expected, video_path, size)
    except CapacityInsufficientError as exc:
        raise HTTPException(status_code=507, detail=exc.as_dict()) from exc


def _assemble_parts_reserved(expected, video_path, size):
    video_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = video_path.with_suffix(video_path.suffix + ".assembling")
    try:
        with temporary.open("wb") as target:
            for part in expected:
                with part.open("rb") as source:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        os.replace(temporary, video_path)
    finally:
        temporary.unlink(missing_ok=True)
    return size


@router.post("/studio/transcribe/assemble")
async def studio_transcribe_assemble(request: Request):
    config = get_config()
    try:
        payload = await request.json()
        total = int(payload.get("total", 0))
        expected_size = int(payload["size_bytes"]) if "size_bytes" in payload else None
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="请求参数无效") from exc
    upload_id = str(payload.get("upload_id", ""))
    directory = _chunk_directory(upload_id)
    extension = Path(str(payload.get("filename", ""))).suffix.lower() or ".mp4"
    if extension not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
        raise HTTPException(status_code=400, detail="Unsupported video format")
    if not 0 < total <= 20000:
        raise HTTPException(status_code=400, detail="total 非法")
    if not directory.is_dir():
        raise HTTPException(status_code=400, detail="分片不完整，请继续上传")
    # 授权必须早于合并/源注册，不能依赖Content-Type或后置attach的拒绝。
    actor=current_principal(request)
    access=access_ledger(request)
    project_id=str(payload.get("project_id") or "")
    upload=access.require(actor,"upload",upload_id,write=True)
    access.require(actor,"project",project_id,write=True)
    if upload["project_id"] and upload["project_id"]!=project_id:
        raise HTTPException(status_code=409,detail="studio_upload_project_conflict")
    # 删除期不可变守卫：deleting/delete_failed/deleted 的项目拒绝合并上传。
    lifecycle.assert_mutable(project_id)
    try:
        with process_lock(directory / ".assembly.lock", blocking=False):
            receipt = directory / "receipt.json"
            if receipt.is_file():
                return json.loads(receipt.read_text())
            uploads_dir = Path(config.temp_dir) / "studio-uploads"
            video_path = uploads_dir / f"{upload_id}{extension}"
            size = await run_in_threadpool(_assemble_parts, directory, video_path, total,
                                          config.max_video_upload_bytes, expected_size)
            from ...studio_job_store import create_job
            object_key = f"local:{video_path}"
            project_id = str(payload.get("project_id") or "")
            if not project_id:
                raise HTTPException(status_code=400, detail="project_id is required")
            register_created_object("source", object_key, project_id, size_bytes=size)
            access_ledger(request).attach(current_principal(request), "upload", upload_id, project_id)
            # 本地分片上传的「开始转写」= TRANSCRIBE_ONLY：绝不串完整自动剪辑链。
            job_id = await create_job(project_id, object_key,
                                      size, {"language": "zh", "source": "chunked_upload", "job_kind": "transcribe_only"},
                                      job_id=upload_id)
            result = {"job_id": job_id, "object_key": object_key, "status": "queued"}
            temporary = receipt.with_suffix(".tmp")
            temporary.write_text(json.dumps(result))
            os.replace(temporary, receipt)
            for part in directory.glob("*.part"):
                part.unlink()
            complete_upload_accounting(upload_id)
            return result
    except BlockingIOError as exc:
        raise HTTPException(status_code=409, detail="正在合并视频，请稍后重试") from exc


@router.get("/studio/transcribe/jobs/{job_id}")
async def studio_transcribe_job_status(job_id: str):
    with _TRANSCRIBE_JOBS_LOCK:
        job = _TRANSCRIBE_JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Transcription job not found")
        snapshot = dict(job)
    return {
        "job_id": job_id,
        "status": snapshot.get("status", "processing"),
        "elapsed_seconds": round(time.time() - float(snapshot.get("created_at", time.time())), 1),
        "result": snapshot.get("result"),
        "error": snapshot.get("error"),
    }


@router.post("/studio/insights")
async def studio_extract_insights(request: Request):
    """Extract highlights from the already-loaded Studio transcript, without ASR again."""
    try:
        payload = await request.json()
        segments = payload.get("asr_segments") if isinstance(payload, dict) else None
        if not isinstance(segments, list):
            raise HTTPException(status_code=400, detail="asr_segments must be a list")
        normalized = [
            {
                "start": round(float(item.get("start", 0)), 3),
                "end": round(float(item.get("end", 0)), 3),
                "text": str(item.get("text", "")).strip(),
            }
            for item in segments
            if isinstance(item, dict)
            and str(item.get("text", "")).strip()
            and float(item.get("end", 0)) > float(item.get("start", 0))
        ]
        if not normalized:
            raise HTTPException(status_code=400, detail="No usable timestamped transcript segments")
        with heavy_operation():
            return {"insights": await extract_studio_insights(normalized)}
    except HTTPException:
        raise
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid transcript segment: {exc}") from exc
    except Exception as exc:
        logger.exception("Studio insight extraction failed")
        raise HTTPException(status_code=500, detail=f"Insight extraction failed: {exc}") from exc


@router.post("/studio/auto-edit")
async def studio_start_auto_edit(request: Request):
    """Enqueue the current Studio timeline for the local fixed production executor.

    This route only writes a durable task package.  Rendering remains in the
    local queue worker so a long FFmpeg/AI job never blocks the Studio API.
    """
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Expected a JSON object")
        project_id = str(payload.get("project_id") or "").strip()
        if not project_id:
            raise HTTPException(status_code=400, detail="project_id is required; complete transcription first")
        config = get_config()
        # 封面风格贯通（A 项）：从**规则**取本次要用的封面风格——
        # 该任务的规则快照 cover_rules.default_style_id（无快照的旧路径回退清单现值），
        # 而不是让代码常量或 video-brain 的 legacy 缺省决定封面长什么样。
        style_id, style_source = studio_workflow.cover_style_choice(purpose="auto_edit")
        if style_id not in SUPPORTED_COVER_STYLES:
            raise HTTPException(
                status_code=422,
                detail=f"规则清单里的封面风格 style_id={style_id!r} 不受支持；"
                       f"可用: {sorted(SUPPORTED_COVER_STYLES)}"
                       f"（请在规则清单 cover_rules.default_style_id 里改正）",
            )
        result = await run_in_threadpool(
            submit_auto_edit_task,
            project_id=project_id,
            uploads_root=Path(config.temp_dir),
            asr_segments=payload.get("asr_segments"),
            golden_sentences=payload.get("golden_sentences"),
            keywords=payload.get("keywords"),
            clip_transforms=payload.get("clip_transforms"),
            style_id=style_id,
            style_source=style_source,
        )
        register_created_object("task", str(result["task_id"]), project_id)
        return result
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unable to enqueue Studio auto-edit task")
        raise HTTPException(status_code=500, detail=f"Auto-edit queue failed: {exc}") from exc


@router.get("/studio/auto-edit/{task_id}")
async def studio_auto_edit_status(task_id: str):
    """Return queue progress and files copied back by the local executor."""
    try:
        return await run_in_threadpool(read_auto_edit_task, task_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unable to read Studio auto-edit task")
        raise HTTPException(status_code=500, detail=f"Auto-edit status failed: {exc}") from exc


@router.post("/studio/auto-edit/{task_id}/rerender")
async def studio_rerender_auto_edit(task_id: str, request: Request):
    """Queue a non-destructive re-render with the current local rule set.

    An optional JSON body ``{"clip_ids": ["clip-02"], "style_id": "realframe_v2"}``
    restricts the new task to those previously produced clips (single-clip
    re-render backing the Studio Copilot make_cover / render_clip actions) and
    pins the cover style.  A missing or empty body keeps the legacy full
    re-render behaviour; ``style_id`` 缺省仍走 video-brain 的配置缺省风格。
    """
    try:
        clip_ids: list[str] | None = None
        style_id: str | None = None
        body = await request.body()
        if body.strip():
            try:
                payload = json.loads(body)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象") from exc
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
            raw_clip_ids = payload.get("clip_ids")
            if raw_clip_ids is not None:
                if (
                    not isinstance(raw_clip_ids, list)
                    or not raw_clip_ids
                    or not all(isinstance(item, str) and item.strip() for item in raw_clip_ids)
                ):
                    raise HTTPException(status_code=422, detail="clip_ids 必须是非空字符串数组")
                clip_ids = [item.strip() for item in raw_clip_ids][:4]
            raw_style = payload.get("style_id")
            if raw_style is not None:
                if not isinstance(raw_style, str) or not raw_style.strip():
                    raise HTTPException(status_code=422, detail="style_id 必须是非空字符串")
                style_id = raw_style.strip()
                if style_id not in ALL_RERENDER_STYLES:
                    # 白名单校验：非法风格在建任务前拒绝，而不是让执行器事后失败
                    raise HTTPException(
                        status_code=422,
                        detail=f"不支持的封面风格 style_id={style_id!r}；"
                               f"可用: {sorted(SUPPORTED_COVER_STYLES)}",
                    )
        return await run_in_threadpool(rerender_auto_edit_task, task_id, clip_ids, style_id)
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unable to queue Studio auto-edit re-render")
        raise HTTPException(status_code=500, detail=f"Auto-edit re-render failed: {exc}") from exc


@router.get("/studio/auto-edit/{task_id}/artifact")
async def studio_auto_edit_artifact(task_id: str, path: str, download: bool = False):
    """Serve a completed local artifact for inline Studio preview or download."""
    try:
        artifact = await run_in_threadpool(resolve_auto_edit_artifact, task_id, path)
        media_type = "video/mp4" if artifact.suffix.lower() == ".mp4" else None
        return FileResponse(
            artifact,
            media_type=media_type,
            filename=artifact.name if download else None,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 轻量封面通道：按风格重制 / 沿用已生成 / 回滚（秒级，不整片重渲）
# ---------------------------------------------------------------------------

def _cover_error(exc: Exception) -> HTTPException:
    """封面通道错误映射：沿用本文件既有风格（400/404/422/500），ValueError→400。"""
    if isinstance(exc, CoverReplaceError):
        if exc.code == E_COVER_CLIP_MISSING:
            return HTTPException(status_code=404, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=f"封面通道失败: {exc}")


async def _cover_body(request: Request) -> dict[str, Any]:
    body = await request.body()
    if not body.strip():
        return {}
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    return payload


@router.post("/studio/auto-edit/{task_id}/clips/{clip_id}/cover")
async def studio_replace_clip_cover(task_id: str, clip_id: str, request: Request):
    """按封面风格重制该成片的封面（cover.jpg + 0.8s 片头），可回滚。

    body ``{"style_id": "realframe_v2", "replace_intro": true}``；
    ``style_id`` 缺省 = ``realframe_v2``（第二版·真实帧大字封面），
    ``replace_intro`` 缺省 true（图与片头一起换）。非法风格 → 422；
    cutout_v3 权重缺失 → 400 + ``E_CUTOUT_WEIGHTS_MISSING``（不静默降级）。
    """
    payload = await _cover_body(request)
    style_id = payload.get("style_id")
    if style_id is not None and style_id not in SUPPORTED_COVER_STYLES:
        raise HTTPException(
            status_code=422,
            detail=f"不支持的封面风格 style_id={style_id!r}；可用: {sorted(SUPPORTED_COVER_STYLES)}",
        )
    if style_id is not None and not isinstance(style_id, str):
        raise HTTPException(status_code=422, detail="style_id 必须是字符串")
    # 封面版型（2026-10-21 阶段）：template_id 与 style_id 二选一；版型走本地渲染缝。
    template_id = payload.get("template_id")
    if template_id is not None and not isinstance(template_id, str):
        raise HTTPException(status_code=422, detail="template_id 必须是字符串")
    if template_id is not None and style_id is not None:
        raise HTTPException(status_code=422, detail="template_id 与 style_id 只能二选一")
    if template_id is not None:
        try:
            from ... import cover_templates
            cover_templates.template_render_spec(template_id)
        except cover_templates.CoverTemplateError as exc:
            raise HTTPException(status_code=404,
                                detail={"code": exc.code, "message": exc.message}) from exc
    replace_intro = payload.get("replace_intro", True)
    if not isinstance(replace_intro, bool):
        raise HTTPException(status_code=422, detail="replace_intro 必须是布尔值")
    try:
        result = await run_in_threadpool(replace_cover, task_id, clip_id, style_id,
                                         replace_intro=replace_intro, template_id=template_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("按风格重制封面失败")
        raise _cover_error(exc) from exc
    # 版型草稿：Make 成功后标记「待应用」（成片未动；应用走 cover-template/apply）
    if template_id is not None:
        from ... import cover_replace as _cr
        try:
            pending = await run_in_threadpool(
                _cr.mark_template_pending, task_id, clip_id,
                int(result.get("cover_revision") or 0), result.get("style_id"))
            result = {**result, "template_pending": pending}
        except Exception:
            logger.exception("标记版型草稿状态失败")
    return result


@router.post("/studio/auto-edit/{task_id}/clips/{clip_id}/cover-template/apply")
async def studio_apply_cover_template(task_id: str, clip_id: str):
    """应用版型封面草稿：烧录 0.8s 片头并原子替换当前成片；失败保留原成片。"""
    from ... import cover_replace as _cr
    try:
        return await run_in_threadpool(_cr.apply_template_cover, task_id, clip_id)
    except Exception as exc:
        logger.exception("应用版型封面失败")
        raise _cover_error(exc) from exc


@router.post("/studio/auto-edit/{task_id}/clips/{clip_id}/cover-template/delete")
async def studio_delete_cover_template(task_id: str, clip_id: str):
    """删除未应用的版型封面草稿（含其后端文件）；切片/成片/其它数据不动。"""
    from ... import cover_replace as _cr
    try:
        return await run_in_threadpool(_cr.delete_template_cover, task_id, clip_id)
    except Exception as exc:
        logger.exception("删除版型封面草稿失败")
        raise _cover_error(exc) from exc


@router.post("/studio/auto-edit/{task_id}/clips/{clip_id}/cover/adopt")
async def studio_adopt_clip_cover(task_id: str, clip_id: str, request: Request):
    """沿用「已生成的封面」：把来源成片的 cover.jpg 套用到目标成片（不重新生成）。

    body ``{"source_task_id": "...", "source_clip_id": "...", "replace_intro": false}``；
    同项目内跨成片套用，``replace_intro`` 默认 false（只换封面图，不动片头）。
    同来源重复执行幂等（不叠加备份）。
    """
    payload = await _cover_body(request)
    source_task_id = str(payload.get("source_task_id") or task_id).strip()
    source_clip_id = str(payload.get("source_clip_id") or "").strip()
    if not source_clip_id:
        raise HTTPException(status_code=422, detail="source_clip_id 必填（要沿用的封面所属成片）")
    if source_task_id != task_id:
        raise HTTPException(status_code=422, detail="仅支持同项目内成片之间沿用封面")
    replace_intro = payload.get("replace_intro", False)
    if not isinstance(replace_intro, bool):
        raise HTTPException(status_code=422, detail="replace_intro 必须是布尔值")
    try:
        return await run_in_threadpool(adopt_cover, task_id, clip_id,
                                       source_task_id, source_clip_id,
                                       replace_intro=replace_intro)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("沿用已生成封面失败")
        raise _cover_error(exc) from exc


@router.post("/studio/auto-edit/{task_id}/clips/{clip_id}/cover/restore")
async def studio_restore_clip_cover(task_id: str, clip_id: str):
    """一键回滚封面通道的改动（封面图 + 成片），回到首次改动前的原文件。"""
    try:
        return await run_in_threadpool(restore_cover, task_id, clip_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("封面回滚失败")
        raise _cover_error(exc) from exc


@router.get("/studio/auto-edit/{task_id}/clips/{clip_id}/cover")
async def studio_clip_cover_state(task_id: str, clip_id: str):
    """读该成片的封面台账：当前封面来源、可否回滚、支持的风格。"""
    try:
        return await run_in_threadpool(read_cover_state, task_id, clip_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("读取封面台账失败")
        raise _cover_error(exc) from exc


@router.get("/studio/auto-edit/{task_id}/covers")
async def studio_project_covers(task_id: str):
    """该项目所有成片的封面摘要（「沿用已生成封面」的来源选择列表）。"""
    try:
        return {"task_id": task_id, "clips": await run_in_threadpool(list_project_clips, task_id)}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("读取项目封面列表失败")
        raise _cover_error(exc) from exc


@router.put("/studio/auto-edit/{task_id}/clips/{clip_id}/transform")
async def studio_save_clip_transform(task_id: str, clip_id: str, request: Request):
    """Persist (or clear with ``transform: null``) one rendered clip's frame transform.

    Stored non-destructively in the task output sidecar; the Studio player and
    the transform export both read it back after refresh or project re-open.
    """
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
        transform = payload.get("transform")
        if transform is not None and not isinstance(transform, dict):
            raise HTTPException(status_code=422, detail="transform 必须是 JSON 对象或 null")
        saved = await run_in_threadpool(save_clip_transform, task_id, clip_id, transform)
        return {"clip_id": clip_id, "transform": saved.get(clip_id)}
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unable to save Studio clip transform")
        raise HTTPException(status_code=500, detail=f"Save clip transform failed: {exc}") from exc


def _timeline_image_instances_for(task_id: str, clip_id: str) -> list[dict[str, Any]]:
    """该成片时间线上的全部 image_clip 实例（找不到项目/台账缺失 → 空列表）。"""
    project_id = project_store.find_project_by_task(task_id)
    if not project_id:
        return []
    return project_store.get_timeline_image_clips(project_id, clip_id)


def _timeline_cover_intro_for(task_id: str, clip_id: str) -> dict[str, Any] | None:
    """第四步：按任务反查项目，取该成片时间线上 **role=cover_intro** 的 image_clip 实例。"""
    try:
        project_id = project_store.find_project_by_task(task_id)
        if not project_id:
            return None
        for item in project_store.get_timeline_image_clips(project_id, clip_id):
            if str(item.get("role") or "") == "cover_intro":
                return item
    except Exception:  # noqa: BLE001 — 台账缺失/损坏按「无时间线封面」处理
        return None
    return None


def _clip_cover_intro_s(task_id: str, clip_id: str) -> float:
    """该成片台账登记的内嵌封面片头长度（秒）；未登记/无字段 → 0.0。

    与首帧缩略图同口径：应用封面任务写入 ``cover_intro_s``，导出时按它裁掉
    内嵌旧片头，而不是硬编码 0.8s（无内嵌片头的成片不能误裁正片内容）。
    """
    try:
        project_id = project_store.find_project_by_task(task_id)
        if not project_id:
            return 0.0
        entry = project_store.entry_for(project_id, clip_id)
        return max(0.0, float(entry.get("cover_intro_s") or 0.0))
    except Exception:  # noqa: BLE001 — 台账缺失/损坏按「无内嵌片头」处理
        return 0.0


def _apply_timeline_cover_intro(source: Path, clip_id: str, instance: dict[str, Any],
                                previous_intro_s: float = 0.0) -> Path:
    """把时间线封面实例渲染进导出文件：前置时长 = 实例 duration_s（裁掉内嵌旧片头）。

    旧片头长度 = 台账 ``cover_intro_s``（调用方经 :func:`_clip_cover_intro_s` 解析）。
    在时间线导出缓存里工作（不碰成片原件/任务产物）；同参数命中直接复用。
    """
    import shutil
    from ... import cover_realframe_v2

    config = get_config()
    out_dir = Path(config.temp_dir) / "studio-timeline-exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    duration_s = max(0.3, min(10.0, float(instance.get("duration_s") or 1.5)))
    previous_s = max(0.0, float(previous_intro_s or 0.0))
    asset_rel = str(instance.get("asset_path") or "")
    cover = project_store.TASKS_ROOT / asset_rel
    if not cover.is_file():
        raise HTTPException(status_code=404, detail={
            "code": "E_TIMELINE_IMAGE_NO_ASSET",
            "message": "时间线封面素材文件缺失，无法导出"})
    work = out_dir / (f"{clip_id}_coverintro{int(round(duration_s * 1000))}"
                      f"p{int(round(previous_s * 1000))}.mp4")
    if work.is_file():
        return work
    tmp = out_dir / (f"{clip_id}_coverintro{int(round(duration_s * 1000))}"
                     f"p{int(round(previous_s * 1000))}.tmp.mp4")
    try:
        shutil.copy2(source, tmp)
        cover_realframe_v2.splice_intro_prepend(
            tmp, cover, intro_duration_s=duration_s, previous_intro_s=previous_s)
        os.replace(tmp, work)
    finally:
        tmp.unlink(missing_ok=True)
    return work


def _apply_timeline_image_tails(source: Path, clip_id: str,
                                instances: list[dict[str, Any]]) -> Path:
    """正片中间/之后的 image_clip 实例：按 start_s 升序在各自位置**真正插入**封面静帧。

    - 位置按**原始成片时间轴**给出；插入使时长增长时，后续实例自动顺延
      （effective_pos = max(start_s, 上一个已插入段的结束位置)，重叠实例按顺序衔接）；
    - 落点已在当前内容末尾（±50ms）→ 走片尾追加通道（append_image_still）；
    - 整条插入链一个缓存键（实例 id/位置/时长任一变化即换新缓存）。
    """
    from ... import cover_realframe_v2

    config = get_config()
    out_dir = Path(config.temp_dir) / "studio-timeline-exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    tails = sorted(
        (item for item in instances
         if str(item.get("role") or "") == "image"
         and (project_store.TASKS_ROOT / str(item.get("asset_path") or "")).is_file()),
        key=lambda item: (float(item.get("start_s") or 0), str(item.get("id") or "")))
    if not tails:
        return source
    digest = hashlib.sha1(json.dumps(
        [[str(item.get("id") or ""), round(float(item.get("start_s") or 0), 3),
          round(float(item.get("duration_s") or 1.5), 3)] for item in tails],
        ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    work = out_dir / f"{clip_id}_images-{digest}.mp4"
    if work.is_file():
        return work
    tmp = out_dir / f"{clip_id}_images-{digest}.tmp.mp4"
    shutil.copy2(source, tmp)
    try:
        base_duration = cover_realframe_v2._real_duration_s(tmp)
        current_end = max(0.0, float(base_duration or 0))
        for item in tails:
            duration_s = max(0.3, min(10.0, float(item.get("duration_s") or 1.5)))
            position = max(0.0, float(item.get("start_s") or 0))
            cover = project_store.TASKS_ROOT / str(item.get("asset_path"))
            if current_end <= 0 or position >= current_end - 0.05:
                cover_realframe_v2.append_image_still(tmp, cover, duration_s=duration_s)
            else:
                cover_realframe_v2.insert_image_still(
                    tmp, cover, start_s=position, duration_s=duration_s)
            current_end += duration_s
        os.replace(tmp, work)
    finally:
        tmp.unlink(missing_ok=True)
    return work


@router.get("/studio/auto-edit/{task_id}/clips/{clip_id}/export-transformed")
async def studio_export_transformed_clip(
    task_id: str,
    clip_id: str,
    transform: str | None = None,
    background: str = "blur",
    download: bool = True,
):
    """Download the clip re-rendered through its frame transform (FFmpeg).

    The ``transform`` query parameter carries the JSON document the player
    previewed with; when omitted the clip's saved transform is used.  The
    filter chain comes from clip_transform.build_filter_chain, so the exported
    1080x1920 frame matches the on-screen preview exactly.
    """
    try:
        parsed_transform: Any = None
        if transform:
            try:
                parsed_transform = json.loads(transform)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="transform 必须是合法 JSON") from exc
            if not isinstance(parsed_transform, dict):
                raise HTTPException(status_code=422, detail="transform 必须是 JSON 对象")
        config = get_config()
        output = await run_in_threadpool(
            export_transformed_clip,
            task_id=task_id,
            clip_id=clip_id,
            transform=parsed_transform,
            background_mode=background,
            cache_root=Path(config.temp_dir) / "studio-transform-exports",
        )
        # 第四步：时间线上的封面 image_clip 参与最终渲染——
        # 片头（role=cover_intro）按实例时长前置（按台账 cover_intro_s 裁掉内嵌旧片头，
        # 正片音频/字幕同步后移）；正片之后的实例按顺序追加封面静帧。
        # 删除时间线实例不影响封面素材文件。
        try:
            timeline_items = await run_in_threadpool(
                _timeline_image_instances_for, task_id, clip_id)
        except Exception:  # noqa: BLE001 — 时间线读取失败不阻断普通导出
            logger.exception("读取时间线 image_clip 失败")
            timeline_items = []
        if timeline_items:
            head = next((item for item in timeline_items
                         if str(item.get("role") or "") == "cover_intro"), None)
            if head:
                previous_intro_s = await run_in_threadpool(
                    _clip_cover_intro_s, task_id, clip_id)
                output = await run_in_threadpool(
                    _apply_timeline_cover_intro, output, clip_id, head,
                    previous_intro_s)
            tails = [item for item in timeline_items if str(item.get("role") or "") == "image"]
            if tails:
                output = await run_in_threadpool(
                    _apply_timeline_image_tails, output, clip_id, tails)
        return FileResponse(
            output,
            media_type="video/mp4",
            filename=output.name if download else None,
        )
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unable to export transformed Studio clip")
        raise HTTPException(status_code=500, detail=f"Transform export failed: {exc}") from exc


@router.get("/studio/timeline/{project_id}")
async def studio_timeline(project_id: str):
    """Return one local Studio EDL plus its transcript and export inventory."""
    try:
        config = get_config()
        payload = await run_in_threadpool(
            load_timeline_project,
            project_id=project_id,
            uploads_root=Path(config.temp_dir),
        )
        # 项目名称元数据（2026-10-20 内联重命名）：台账 display_name 优先，回退源片名。
        try:
            payload["project_name"] = await run_in_threadpool(
                project_store.get_project_name_meta, project_id)
        except Exception:  # noqa: BLE001 — 元数据失败不阻塞时间线载入
            logger.exception("project name meta failed %s", project_id)
        return payload
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unable to load Studio timeline")
        raise HTTPException(status_code=500, detail=f"Timeline load failed: {exc}") from exc


@router.put("/studio/timeline/{project_id}")
async def save_studio_timeline(project_id: str, request: Request):
    """Save selected source ranges and regenerate the portable timeline exports."""
    try:
        payload = await request.json()
        ranges = payload.get("ranges") if isinstance(payload, dict) else None
        if not isinstance(ranges, list):
            raise HTTPException(status_code=400, detail="ranges must be a list")
        config = get_config()
        return await run_in_threadpool(
            update_timeline_project,
            project_id=project_id,
            uploads_root=Path(config.temp_dir),
            ranges=ranges,
        )
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unable to save Studio timeline")
        raise HTTPException(status_code=500, detail=f"Timeline save failed: {exc}") from exc


def _studio_project_dir(project_id: str) -> Path:
    """解析 Studio 工程目录并做基本防穿越校验（无 project.json 视为不存在）。"""
    config = get_config()
    root = (Path(config.temp_dir) / "studio-projects").resolve()
    project = (root / project_id).resolve()
    if root not in project.parents or not (project / "project.json").is_file():
        raise HTTPException(status_code=404, detail="Timeline project not found")
    return project


@router.get("/studio/timeline/{project_id}/preview")
async def studio_timeline_preview(project_id: str):
    """返回浏览器可播放的原片预览。

    HEVC/4K 源片浏览器无法解码（``videoWidth`` 恒为 0 → 播放器中间画布黑屏），
    因此这里返回 H.264 预览代理；代理尚未生成时返回 202 供前端轮询，
    避免首帧长请求被网关超时切断。母带与导出链路不受影响。
    """
    project_dir = _studio_project_dir(project_id)
    try:
        current = await run_in_threadpool(preview_proxy_status, project_dir)
        if current.state in {"native", "ready"} and current.path:
            return FileResponse(current.path, media_type="video/mp4")
        started = await run_in_threadpool(start_preview_proxy, project_dir)
        if started.state == "ready" and started.path:  # 小文件可能瞬间完成
            return FileResponse(started.path, media_type="video/mp4")
        return JSONResponse(status_code=202, content=started.as_dict())
    except HTTPException:
        raise
    except CapacityInsufficientError as exc:
        raise HTTPException(status_code=507, detail=exc.as_dict()) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unable to serve Studio timeline preview")
        raise HTTPException(status_code=500, detail=f"Timeline preview failed: {exc}") from exc


@router.post("/studio/timeline/{project_id}/preview")
async def studio_timeline_preview_build(project_id: str):
    """幂等触发原片预览代理构建（前端恢复会话时先调用，减少首帧等待）。"""
    project_dir = _studio_project_dir(project_id)
    try:
        current = await run_in_threadpool(start_preview_proxy, project_dir)
    except CapacityInsufficientError as exc:
        raise HTTPException(status_code=507,detail=exc.as_dict()) from exc
    ready = current.state in {"native", "ready"}
    return JSONResponse(status_code=200 if ready else 202, content=current.as_dict())


@router.get("/studio/timeline/{project_id}/source")
async def studio_timeline_source(project_id: str):
    """Serve a Studio project's original upload for preview restore after refresh."""
    try:
        config = get_config()
        source = await run_in_threadpool(project_source_path, project_id, Path(config.temp_dir))
        return FileResponse(source, media_type="video/mp4")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unable to serve Studio timeline source")
        raise HTTPException(status_code=500, detail=f"Timeline source failed: {exc}") from exc


# ===========================================================================
# 底部时间线 Phase B：音轨工具栏 + 轨道显示设置
# ---------------------------------------------------------------------------
# 三块 Phase A 地基的 HTTP 搬运层（全部 run_in_threadpool，不阻塞事件循环）：
#   * timeline_view    → 视图设置（缩放 / 轨高 / 波形占比 / 滚动）
#   * timeline_history → 编辑历史（撤销 / 重做 / 版本号）
#   * timeline_audio   → 音轨增删改 + 真实 peaks（复用 clip_audio）
#
# 硬边界（务必保持）
# ~~~~~~~~~~~~~~~~~~
# 本节任何端点都**不调用** update_timeline_project / _write_outputs，也不写 edit/edl.json，
# 因此「改视图设置」「加/删音轨」不会重写 EDL、不会转码、不会导出。渲染权威始终是
# timeline_copilot 的 EDL；timeline/view.json 与 timeline/history.json 只服务 UI。
#
# 错误映射复用既有 _inspector_error（`.code` → 422，零新增映射代码）；只有两处按
# Phase B 契约单独抬起 HTTPException（_inspector_error 对 HTTPException 原样透传）：
#   * E_TIMELINE_AUDIO_SOURCE_TRACK_RESERVED → 409（＋ 菜单撞上自动进入的原声轨）
#   * E_TIMELINE_UNDO_UNAVAILABLE / E_TIMELINE_REDO_UNAVAILABLE → 409
# ===========================================================================

#: 自动进入时间线的原声轨标签（前端 tooltip 文案同源）。
TIMELINE_SOURCE_TRACK_LABEL = "原始视频原声"
#: 非音频轨在 revision 里的固定 id（字幕轨 / 视觉轨用）。
TIMELINE_SUBTITLE_TRACK_ID = "subtitles"
#: peaks 端点允许的「素材根」子目录（project_dir 之外唯一放行的根）。
TIMELINE_MATERIAL_ROOT_DIRNAME = "studio-uploads"


def _timeline_bad_path(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=f"E_TIMELINE_ASSET_PATH_INVALID: {detail}")


def _timeline_project_dir(project_id: str) -> Path:
    """复用既有的工程目录解析（含防穿越 + project.json 存在性 → 404）。"""
    return _studio_project_dir(project_id)


def _timeline_project_root(project_dir: Path) -> Path:
    return Path(project_dir).resolve()


def _timeline_material_roots() -> list[Path]:
    """素材根：``<temp_dir>/studio-uploads``（上传暂存区，project_dir 之外唯一放行）。"""
    config = get_config()
    return [(Path(config.temp_dir) / TIMELINE_MATERIAL_ROOT_DIRNAME).resolve()]


def _timeline_asset_path(
    project_dir: Path, raw_path: str, *, strict: bool = True
) -> Path | None:
    """把素材路径限制在 **project_dir 子树** 或 **素材根子树** 内（防穿越）。

    * 空路径 / 绝对路径越界 / ``..`` 穿越 / 软链指向白名单外 → ``strict`` 时 400；
    * 白名单内但文件不存在 → ``strict`` 时 404（区分“非法范围”与“确实没有”）。
    """

    text = str(raw_path or "").strip()
    if not text:
        if not strict:
            return None
        raise _timeline_bad_path("path 不能为空")
    candidate = Path(text)
    try:
        resolved = (
            candidate.resolve() if candidate.is_absolute() else (project_dir / candidate).resolve()
        )
    except (OSError, RuntimeError) as exc:
        if not strict:
            return None
        raise _timeline_bad_path(f"path 无法解析: {text!r}") from exc

    roots = [_timeline_project_root(project_dir), *_timeline_material_roots()]
    if not any(root == resolved or root in resolved.parents for root in roots):
        if not strict:
            return None
        raise _timeline_bad_path(
            f"path 越界，只允许工程目录或素材根下的文件: {text!r}")
    if not resolved.is_file():
        if not strict:
            return None
        raise HTTPException(status_code=404, detail=f"素材文件不存在: {text!r}")
    project_root=_timeline_project_root(project_dir)
    if resolved!=project_root and project_root not in resolved.parents:
        # 全局素材根白名单只解决路径安全；业务归属仍须独立source ACL。
        access_ledger().require(current_principal(),"source","local:"+str(resolved))
    return resolved


def _timeline_manifest(project_dir: Path) -> dict[str, Any]:
    try:
        return json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"工程清单不可读: {exc}") from exc


def _timeline_source_rel_path(project_dir: Path) -> tuple[str, str, float] | None:
    """返回 ``(相对工程路径, 素材名, 时长)``；源片不在磁盘上 → ``None``。"""
    source = _timeline_manifest(project_dir).get("source") or {}
    name = Path(str(source.get("name") or "")).name
    if not name:
        return None
    relative = f"raw/{name}"
    if not (project_dir / relative).is_file():
        return None
    try:
        duration = float(source.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return relative, name, duration


def _timeline_source_track_id(name: str) -> str:
    """稳定 track_id：同一源片重复 seed 命中同一条轨（配合幂等 seed）。"""
    return f"voice_source_{hashlib.sha256(name.encode('utf-8')).hexdigest()[:10]}"


def _timeline_source_seed(project_dir: Path) -> tuple[dict[str, Any], dict[str, float]] | None:
    """构造源片原声轨 + ``asset_durations``（越界钳制用）；源片缺失 → ``None``。"""
    resolved = _timeline_source_rel_path(project_dir)
    if resolved is None:
        return None
    relative, name, duration = resolved
    if duration <= 0:
        duration = clip_audio.probe_duration(project_dir / relative) or 0.0
    if duration <= 0:
        return None
    track = timeline_audio.source_auto_track(
        track_id=_timeline_source_track_id(name),
        kind="voice",
        asset_id=name,
        path=relative,
        duration_s=duration,
        label=TIMELINE_SOURCE_TRACK_LABEL,
    )
    return track, {relative: duration, name: duration}


def _timeline_base_revision(project_dir: Path) -> dict[str, Any]:
    """编辑数据基底：有历史取当前版本内容；空历史取 EDL ranges（**不落盘**）。"""
    current = timeline_history.current_revision(project_dir)
    if current is not None:
        return timeline_history.revision_payload(current)
    ranges: list[dict[str, Any]] = []
    edl_path = project_dir / "edit" / "edl.json"
    if edl_path.is_file():
        try:
            edl = json.loads(edl_path.read_text(encoding="utf-8"))
            timelines = edl.get("timelines") or [{}]
            ranges = list((timelines[0] or {}).get("ranges") or [])
        except (OSError, json.JSONDecodeError, TypeError, IndexError, AttributeError):
            ranges = []
    return {"ranges": ranges, "audio_tracks": [], "subtitles": None}


def _timeline_ensure_source(project_dir: Path, data: dict[str, Any]) -> dict[str, Any]:
    """写路径自愈：任何一次编辑都保证「原声已自动进入时间线」（幂等）。"""
    seed = _timeline_source_seed(project_dir)
    if seed is None:
        return data
    track, durations = seed
    return timeline_audio.seed_source_tracks(data, [track], asset_durations=durations)


def _timeline_audio_durations(
    project_dir: Path, tracks: list[dict[str, Any]]
) -> dict[str, float]:
    """只给**本工程/素材根内**的 ``path`` 做一次 ffprobe（越界钳制用），不做全盘扫描。"""
    durations: dict[str, float] = {}
    for track in tracks:
        relative = track.get("path")
        if not relative or str(relative) in durations:
            continue
        absolute = _timeline_asset_path(project_dir, str(relative), strict=False)
        if absolute is None:
            continue
        value = clip_audio.probe_duration(absolute)
        if value and value > 0:
            durations[str(relative)] = value
    return durations


def _timeline_normalise(project_dir: Path, data: dict[str, Any]) -> dict[str, Any]:
    """``normalise_tracks``（带素材时长钳制）后返回同一份编辑数据。"""
    tracks = timeline_audio.normalise_tracks(data.get("audio_tracks"))
    data["audio_tracks"] = timeline_audio.normalise_tracks(
        tracks, asset_durations=_timeline_audio_durations(project_dir, tracks))
    return data


def _timeline_commit(project_dir: Path, data: dict[str, Any], label: str):
    """统一的落点：``normalise_tracks`` → ``timeline_history.push``（不碰 EDL）。"""
    return timeline_history.push(project_dir, _timeline_normalise(project_dir, data), label=label)


def _seed_timeline_source_audio(
    uploads_root: Path, timeline: dict[str, Any] | None
) -> dict[str, Any] | None:
    """导入 / 自动剪辑落地后把原声自动放进时间线（**唯一**的系统侧入口，幂等）。

    只写 ``<project_dir>/timeline/history.json``；不重写 EDL、不触发转码/导出。
    重跑导入或重跑自动剪辑不会把原声叠加两次。
    """
    project_id = str((timeline or {}).get("id") or "")
    if not project_id:
        return None
    project_dir = Path(uploads_root) / "studio-projects" / project_id
    if not (project_dir / "project.json").is_file():
        return None
    seed = _timeline_source_seed(project_dir)
    if seed is None:
        return None
    track, durations = seed
    data = _timeline_base_revision(project_dir)
    data = timeline_audio.seed_source_tracks(data, [track], asset_durations=durations)
    return _timeline_commit(project_dir, data, "导入").model_dump(mode="json")


def _timeline_history_payload(
    project_dir: Path, project_id: str, revision: Any = None
) -> dict[str, Any]:
    """撤销/重做按钮 + 音轨渲染需要的**同一份**响应形状（GET 与所有写端点共用）。"""
    summary = timeline_history.history_summary(project_dir)
    if revision is None:
        revision = timeline_history.current_revision(project_dir)
    return {
        "project_id": project_id,
        "summary": summary,
        "current_revision": None if revision is None else revision.model_dump(mode="json"),
        "revision": None if revision is None else int(revision.revision),
        "audio_tracks": [] if revision is None else list(revision.audio_tracks),
        "can_undo": bool(summary["can_undo"]),
        "can_redo": bool(summary["can_redo"]),
    }


def _timeline_add_track_commit(
    project_dir: Path, project_id: str, kind: str, body: dict[str, Any], label: str | None
) -> dict[str, Any]:
    """`＋` 菜单落点：bgm/sfx → ``add_track``；subtitle/visual → revision 扩展键。"""
    if kind == "voice":
        # 原声由导入/自动剪辑自动进入 —— 与 add_track 的守卫同一语义与错误码。
        raise timeline_audio.TimelineAudioError(
            timeline_audio.E_TIMELINE_AUDIO_SOURCE_TRACK_RESERVED,
            "原声已自动进入时间线，不能用 ＋ 添加",
        )
    if kind in timeline_audio.AUDIO_KINDS:
        relative = str(body.get("path") or "").strip()
        if not relative:
            raise timeline_audio.TimelineAudioError(
                timeline_audio.E_TIMELINE_AUDIO_TRACK_INVALID,
                f"{timeline_audio.E_TIMELINE_AUDIO_TRACK_INVALID}: "
                "bgm/sfx 轨道必须提供 path（先用 POST .../assets/audio 上传）",
            )
        absolute = _timeline_asset_path(project_dir, relative)
        duration = clip_audio.probe_duration(absolute) or 0.0
        if duration <= 0:
            raise timeline_audio.TimelineAudioError(
                timeline_audio.E_TIMELINE_AUDIO_TRACK_INVALID,
                f"{timeline_audio.E_TIMELINE_AUDIO_TRACK_INVALID}: 无法读取音频时长: {relative!r}",
            )
        track: dict[str, Any] = {
            "track_id": str(body.get("track_id") or timeline_audio.new_track_id(kind)),
            "kind": kind,
            "asset_id": str(body.get("asset_id") or Path(relative).name),
            "path": relative,
            "source_start_s": body.get("source_start_s", 0.0),
            "source_end_s": body.get("source_end_s", duration),
            "record_start_s": body.get("record_start_s", 0.0),
            "volume": body.get("volume", timeline_audio.DEFAULT_VOLUME),
            "muted": body.get("muted", False),
            "fade_in_s": body.get("fade_in_s", 0.0),
            "fade_out_s": body.get("fade_out_s", 0.0),
            "label": str(body.get("label") or ("背景音乐" if kind == "bgm" else "音效")),
            "origin": "user",
        }
        data = _timeline_ensure_source(project_dir, _timeline_base_revision(project_dir))
        data = timeline_audio.add_track(data, track, asset_durations={relative: duration})
        return _timeline_history_payload(
            project_dir,
            project_id,
            _timeline_commit(project_dir, data, label or track["label"]),
        )

    data = _timeline_ensure_source(project_dir, _timeline_base_revision(project_dir))
    if kind == "subtitle":
        data["subtitles"] = list(data.get("subtitles") or [])
        return _timeline_history_payload(
            project_dir, project_id, _timeline_commit(project_dir, data, label or "添加字幕轨"))
    # 图片 / 图解视觉轨：走 TimelineRevision 的扩展键（extra="allow"，参与内容哈希）
    visual = [item for item in (data.get("visual_tracks") or []) if isinstance(item, dict)]
    visual.append({
        "track_id": str(body.get("track_id")
                        or f"visual_{uuid.uuid4().hex[:12]}"),
        "kind": str(body.get("visual_kind") or "image"),
        "label": label or "图片/图解",
        "origin": "user",
    })
    data["visual_tracks"] = visual
    return _timeline_history_payload(
        project_dir, project_id, _timeline_commit(project_dir, data, label or "添加视觉轨"))


def _timeline_patch_track_commit(
    project_dir: Path, project_id: str, track_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    """PATCH 一条轨：音频走 move/set_volume/set_muted/set_fades，非音频只改 label。"""
    data = _timeline_ensure_source(project_dir, _timeline_base_revision(project_dir))
    if timeline_audio.find_track(data, track_id) is not None:
        move = {key: body[key] for key in ("record_start_s", "source_start_s")
                if body.get(key) is not None}
        if move:
            data = timeline_audio.move_track(data, track_id, **move)
        if body.get("volume") is not None:
            data = timeline_audio.set_volume(data, track_id, body["volume"])
        if body.get("muted") is not None:
            data = timeline_audio.set_muted(data, track_id, body["muted"])
        fades = {key: body[key] for key in ("fade_in_s", "fade_out_s")
                 if body.get(key) is not None}
        if fades:
            data = timeline_audio.set_fades(data, track_id, **fades)
        if body.get("label") is not None:
            data = timeline_audio.update_track(data, track_id, {"label": body["label"]})
        if not (move or fades or body.get("volume") is not None
                or body.get("muted") is not None or body.get("label") is not None):
            raise timeline_audio.TimelineAudioError(
                timeline_audio.E_TIMELINE_AUDIO_TRACK_INVALID,
                f"{timeline_audio.E_TIMELINE_AUDIO_TRACK_INVALID}: "
                "PATCH 至少需要 record_start_s/source_start_s/volume/muted/fade_in_s/"
                "fade_out_s/label 之一",
            )
        return _timeline_history_payload(
            project_dir, project_id, _timeline_commit(project_dir, data, f"调整音轨 {track_id}"))

    if track_id == TIMELINE_SUBTITLE_TRACK_ID and data.get("subtitles") is not None:
        return _timeline_history_payload(
            project_dir, project_id, _timeline_commit(project_dir, data, "调整字幕轨"))
    for item in data.get("visual_tracks") or []:
        if isinstance(item, dict) and str(item.get("track_id")) == str(track_id):
            if body.get("label") is not None:
                item["label"] = str(body["label"])
            return _timeline_history_payload(
                project_dir, project_id, _timeline_commit(project_dir, data, "调整视觉轨"))
    raise timeline_audio.TimelineAudioError(
        timeline_audio.E_TIMELINE_AUDIO_TRACK_NOT_FOUND, f"找不到音轨: {track_id!r}")


def _timeline_remove_track_commit(
    project_dir: Path, project_id: str, track_id: str
) -> dict[str, Any]:
    """DELETE 一条轨：音频走 remove_track，字幕/视觉轨从 revision 里摘掉。"""
    data = _timeline_ensure_source(project_dir, _timeline_base_revision(project_dir))
    if timeline_audio.find_track(data, track_id) is not None:
        data = timeline_audio.remove_track(data, track_id)
        return _timeline_history_payload(
            project_dir, project_id, _timeline_commit(project_dir, data, f"删除音轨 {track_id}"))
    if track_id == TIMELINE_SUBTITLE_TRACK_ID and data.get("subtitles") is not None:
        data["subtitles"] = None
        return _timeline_history_payload(
            project_dir, project_id, _timeline_commit(project_dir, data, "删除字幕轨"))
    visual = [item for item in (data.get("visual_tracks") or []) if isinstance(item, dict)]
    remaining = [item for item in visual if str(item.get("track_id")) != str(track_id)]
    if len(remaining) != len(visual):
        data["visual_tracks"] = remaining
        return _timeline_history_payload(
            project_dir, project_id, _timeline_commit(project_dir, data, f"删除视觉轨 {track_id}"))
    raise timeline_audio.TimelineAudioError(
        timeline_audio.E_TIMELINE_AUDIO_TRACK_NOT_FOUND, f"找不到音轨: {track_id!r}")


@router.get("/studio/timeline/{project_id}/view")
async def studio_timeline_view(project_id: str):
    """读时间线视图设置（缩放 / 轨高 / 波形占比 / 滚动）。

    **纯读**：响应只回 ``view``，绝不触发渲染、转码或导出。刷新后前端据此恢复。
    """
    project_dir = _timeline_project_dir(project_id)
    try:
        view = await run_in_threadpool(timeline_view.load_view, project_dir)
        return {"project_id": project_id, "view": view.to_payload()}
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc


@router.put("/studio/timeline/{project_id}/view")
async def studio_save_timeline_view(project_id: str, request: Request):
    """局部更新视图设置（滑杆拖动）。**不写历史、不触发渲染/导出。**"""
    project_dir = _timeline_project_dir(project_id)
    patch = await _json_body(request)
    try:
        view = await run_in_threadpool(timeline_view.save_view_patch, project_dir, patch)
        return {"project_id": project_id, "view": view.to_payload()}
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc


@router.get("/studio/timeline/{project_id}/history")
async def studio_timeline_history(project_id: str):
    """撤销/重做按钮状态 + 当前版本（含 ``audio_tracks``），供置灰与音轨渲染。"""
    project_dir = _timeline_project_dir(project_id)
    try:
        return await run_in_threadpool(_timeline_history_payload, project_dir, project_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc


async def _timeline_step(project_dir: Path, project_id: str, step: str) -> dict[str, Any]:
    """undo / redo 的共用实现：不可用时按契约返回 409（前端置灰 + tooltip）。"""
    unavailable = {
        "undo": timeline_history.E_TIMELINE_UNDO_UNAVAILABLE,
        "redo": timeline_history.E_TIMELINE_REDO_UNAVAILABLE,
    }[step]
    try:
        revision = await run_in_threadpool(
            timeline_history.undo if step == "undo" else timeline_history.redo, project_dir)
    except HTTPException:
        raise
    except timeline_history.TimelineHistoryError as exc:
        if exc.code == unavailable:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise _inspector_error(exc) from exc
    except Exception as exc:
        raise _inspector_error(exc) from exc
    return _timeline_history_payload(project_dir, project_id, revision)


@router.post("/studio/timeline/{project_id}/undo")
async def studio_timeline_undo(project_id: str):
    """撤销一步时间线编辑（不改 EDL、不触发转码导出）。"""
    return await _timeline_step(_timeline_project_dir(project_id), project_id, "undo")


@router.post("/studio/timeline/{project_id}/redo")
async def studio_timeline_redo(project_id: str):
    """重做一步时间线编辑（不改 EDL、不触发转码导出）。"""
    return await _timeline_step(_timeline_project_dir(project_id), project_id, "redo")


@router.post("/studio/timeline/{project_id}/revision")
async def studio_timeline_revision(project_id: str, request: Request):
    """提交一条时间线版本：先 ``normalise_tracks`` 再 ``push``，回新 revision。

    请求体是**编辑内容**（``ranges`` / ``audio_tracks`` / ``subtitles`` / 扩展键），
    可选 ``label``。与当前版本内容相同则幂等（不新增、不清 redo 分支）。
    """
    project_dir = _timeline_project_dir(project_id)
    body = await _json_body(request)
    label = str(body.get("label") or "编辑时间线")
    payload = {key: value for key, value in body.items() if key != "label"}
    try:
        revision = await run_in_threadpool(
            _timeline_revision_commit, project_dir, payload, label)
        return _timeline_history_payload(project_dir, project_id, revision)
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc


def _timeline_revision_commit(project_dir: Path, payload: dict[str, Any], label: str):
    data = _timeline_base_revision(project_dir)
    for key, value in payload.items():
        data[key] = value
    data = _timeline_ensure_source(project_dir, data)
    return _timeline_commit(project_dir, data, label)


@router.post("/studio/timeline/{project_id}/tracks")
async def studio_timeline_add_track(project_id: str, request: Request):
    """`＋` 菜单落点：bgm/sfx（``add_track``）、字幕轨、图片/图解视觉轨，均 → ``push``。

    原视频/原声不在可添加项：``kind="voice"`` 或撞上同素材源轨 →
    ``E_TIMELINE_AUDIO_SOURCE_TRACK_RESERVED`` → 409「原声已自动进入」。
    """
    project_dir = _timeline_project_dir(project_id)
    body = await _json_body(request)
    kind = str(body.get("kind") or "").strip()
    label = str(body.get("label") or "").strip() or None
    allowed = (*timeline_audio.AUDIO_KINDS, "subtitle", "visual")
    if kind not in allowed:
        raise HTTPException(
            status_code=422,
            detail=(f"{timeline_audio.E_TIMELINE_AUDIO_TRACK_INVALID}: "
                    f"kind 只能是 {'/'.join(allowed)}，收到 {kind!r}"),
        )
    try:
        result = await run_in_threadpool(
            _timeline_add_track_commit, project_dir, project_id, kind, body, label)
    except HTTPException:
        raise
    except timeline_audio.TimelineAudioError as exc:
        if exc.code == timeline_audio.E_TIMELINE_AUDIO_SOURCE_TRACK_RESERVED:
            raise HTTPException(
                status_code=409,
                detail=f"{exc.code}: 原声已自动进入时间线，不能用 ＋ 重复添加",
            ) from exc
        raise _inspector_error(exc) from exc
    except Exception as exc:
        raise _inspector_error(exc) from exc
    return result


@router.patch("/studio/timeline/{project_id}/tracks/{track_id}")
async def studio_timeline_patch_track(project_id: str, track_id: str, request: Request):
    """移动 / 音量 / 静音 / 淡入淡出（音频轨）或改标签（字幕、视觉轨）→ ``push``。"""
    project_dir = _timeline_project_dir(project_id)
    body = await _json_body(request)
    try:
        result = await run_in_threadpool(
            _timeline_patch_track_commit, project_dir, project_id, track_id, body)
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc
    return result


@router.delete("/studio/timeline/{project_id}/tracks/{track_id}")
async def studio_timeline_remove_track(project_id: str, track_id: str):
    """删除一条轨（音频 / 字幕 / 视觉）→ ``push``；可随后撤销。"""
    project_dir = _timeline_project_dir(project_id)
    try:
        result = await run_in_threadpool(
            _timeline_remove_track_commit, project_dir, project_id, track_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc
    return result


@router.get("/studio/timeline/{project_id}/assets/peaks")
async def studio_timeline_asset_peaks(
    project_id: str,
    path: str,
    buckets: int = timeline_audio.PEAKS_DEFAULT_BUCKETS,
    track_id: str | None = None,
    kind: str = "bgm",
):
    """真实波形 peaks（ffmpeg，磁盘缓存）。

    ``path`` 走白名单：只允许 **该 project_dir 子树** 或 **素材根**
    (``<temp_dir>/studio-uploads``) 子树；绝对路径越界 / ``..`` 穿越 / 软链逃逸 → 400。
    """
    project_dir = _timeline_project_dir(project_id)
    absolute = _timeline_asset_path(project_dir, path)
    try:
        payload = await run_in_threadpool(
            timeline_audio.peaks_for_asset,
            project_dir,
            absolute,
            buckets=buckets,
            kind=kind,
            asset_id=Path(str(path)).name,
            track_id=track_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc
    return {"project_id": project_id, "path": path, **payload}


@router.post("/studio/timeline/{project_id}/assets/audio")
async def studio_timeline_upload_audio(
    project_id: str,
    uploaded_file: UploadFile = File(..., alias="audio"),
):
    """把 BGM / 音效上传到 ``<project_dir>/timeline/audio``（内容寻址），供 ＋ 菜单加轨。

    与既有 clip 级 BGM 端点并列、互不影响：文件落在**项目时间线**目录，返回工程相对
    ``path``，随后 ``POST .../tracks`` 用它建轨。**不触发渲染/导出。**
    """
    project_dir = _timeline_project_dir(project_id)
    config = get_config()
    filename = Path(str(uploaded_file.filename or "")).name
    if not filename:
        raise HTTPException(status_code=400, detail="No audio file provided")
    target_dir = project_dir / timeline_audio.TIMELINE_DIRNAME
    target_dir.mkdir(parents=True, exist_ok=True)
    staging = target_dir / f".upload-{uuid.uuid4().hex}{Path(filename).suffix.lower()}"
    try:
        await _write_upload_to_disk(
            uploaded_file, staging, config.max_video_upload_bytes)
        track = await run_in_threadpool(
            clip_audio.save_bgm,
            target_dir,
            clip_id=project_id,
            filename=filename,
            source_path=staging,
            max_bytes=config.max_video_upload_bytes,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc
    finally:
        staging.unlink(missing_ok=True)

    # clip_audio.save_bgm 的 path 相对 <project_dir>/timeline；这里补上 timeline/ 前缀，
    # 使 path 与其它轨道统一为「相对 project_dir」，peaks 端点可直接消费。
    relative = f"{timeline_audio.TIMELINE_DIRNAME}/{track['path']}"
    duration = clip_audio.probe_duration(project_dir / relative) or 0.0
    return {
        "project_id": project_id,
        "path": relative,
        "filename": track.get("filename"),
        "asset_id": track.get("asset_id"),
        "duration_s": duration,
        "track": track,
    }


@router.post("/studio/copilot")
async def studio_copilot_chat(request: Request):
    """执行型剪辑助手：LLM/规则引擎在后端完成，前端只执行白名单动作。

    请求体见 studio_copilot.CopilotTurn。任何模型侧失败都会降级为本机规则
    助手（engine=rules），绝不以 500 打断剪辑工作流。
    """
    from ...studio_copilot import CopilotTurn, run_copilot
    from ...studio_copilot_store import save_action_log, save_message

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from exc
    if not isinstance(payload, dict) or not str(payload.get("message", "")).strip():
        raise HTTPException(status_code=400, detail="message 不能为空")

    try:
        turn = CopilotTurn(**payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"copilot 请求参数不合法: {exc.errors()[:2]}") from exc

    project_id = turn.project_id or "timeline_default"
    await save_message(project_id, "user", turn.message)

    result = await run_copilot(turn)
    # 生成/渲染类动作的结果文本必须带规则版本：助手消息显示
    # 「已按当前规则生成，规则版本：vX」，而不是只有「已提交」。
    from ...studio_copilot import current_rule_version, decorate_result_text
    rule_version = current_rule_version()
    result = dict(result)
    result["rule_version"] = rule_version
    result["reply"] = decorate_result_text(
        str(result.get("reply", "")), result.get("actions"), rule_version)

    message_id = await save_message(
        project_id,
        "assistant",
        result.get("reply", ""),
        result.get("engine"),
        result.get("actions", []),
    )
    for action in result.get("actions", []):
        await save_action_log(project_id, action, str(action.get("execution", "run")), None, message_id)

    return {"copilot": result, "rule_version": rule_version}


@router.get("/studio/copilot/history")
async def studio_copilot_history(project_id: str):
    """按项目返回最近助手对话（Redis 缓存优先，回退 PostgreSQL）。"""
    from ...studio_copilot_store import load_history

    if not project_id.strip():
        raise HTTPException(status_code=400, detail="project_id 不能为空")
    return {"messages": await load_history(project_id.strip())}


@router.post("/studio/copilot/action-result")
async def studio_copilot_action_result(request: Request):
    """前端执行器回写动作执行结果，用于操作日志与审计。"""
    from ...studio_copilot_store import save_action_log

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from exc
    if not isinstance(payload, dict) or not str(payload.get("project_id", "")).strip():
        raise HTTPException(status_code=400, detail="project_id 不能为空")
    action = payload.get("action") or {}
    status = str(payload.get("status") or "executed")
    if status not in {"executed", "failed", "dismissed", "run", "confirm"}:
        raise HTTPException(status_code=422, detail=f"未知状态: {status}")
    # 动作执行结果文本带规则版本（助手消息「已按当前规则生成，规则版本：vX」）
    from ...studio_copilot import current_rule_version, decorate_result_text
    rule_version = current_rule_version()
    detail = decorate_result_text(str(payload.get("detail") or ""),
                                  [action] if isinstance(action, dict) else [],
                                  rule_version)
    await save_action_log(
        str(payload["project_id"]),
        action if isinstance(action, dict) else {},
        status,
        detail[:500] or None,
    )
    return {"ok": True, "detail": detail, "rule_version": rule_version,
            "result_text": detail or ("已按当前规则执行，规则版本：v" + rule_version)}


# ---------------------------------------------------------------------------
# 项目资产台账（Project → SourceVideo → Clip）+ 局部任务（类型化，不误跑 AUTO_EDIT）
# ---------------------------------------------------------------------------

def _project_error(exc: Exception) -> HTTPException:
    """把带 E_ 码的领域异常映射成可读 HTTP 错误（保真错误码，便于排障与 UI 展示）。"""
    code = str(getattr(exc, "code", "") or "")
    message = str(exc)
    if code and code not in message:
        message = f"{code}: {message}"
    status = 404 if code in {"E_CLIP_NOT_REGISTERED", "E_TASK_NOT_FOUND"} else 422
    return HTTPException(status_code=status, detail=message)


def _delete_confirmed_by(request: Request) -> str:
    from ...studio_access import current_principal
    principal = current_principal(request)
    return f"subject:{(principal or {}).get('subject')}"


def _assert_project_owner(request: Request, project_id: str) -> None:
    """归档/恢复/删除/重试仅限项目所有者（objects.owner == 认证主体）。"""
    import sqlite3
    from ...studio_access import ACL_PATH, current_principal
    principal = current_principal(request)
    subject = str((principal or {}).get("subject") or "")
    conn = sqlite3.connect(str(ACL_PATH), timeout=10)
    try:
        row = conn.execute("SELECT owner FROM objects WHERE kind='project' AND object_id=?",
                           (project_id,)).fetchone()
    finally:
        conn.close()
    if row is None or str(row[0]) != subject:
        raise HTTPException(status_code=403, detail={
            "code": "E_PROJECT_OWNER_REQUIRED",
            "message": "仅项目所有者可执行该操作"})


def _lifecycle_http_error(exc: "lifecycle.LifecycleError") -> HTTPException:
    code_map = {"E_PROJECT_NOT_FOUND": 404, "E_DELETE_MANIFEST_MISSING": 404,
                "E_PROJECT_STATUS_CONFLICT": 409, "E_DELETE_RETRY_INVALID": 409,
                "E_PROJECT_NOT_MUTABLE": 409}
    return HTTPException(status_code=code_map.get(exc.code, 422),
                         detail={"code": exc.code, "message": exc.message})


def _project_cards_payload(project_id: str) -> dict[str, Any]:
    """同步既有任务后返回项目卡片列表（**一条 clip 一张卡**，按 clip_id 去重）。"""
    sync_from_tasks(project_id)
    cards = list_cards(project_id)
    # 切片选择（2026-10-21）：随卡片载荷透出唯一 active_clip_id，前端据此恢复上次选中。
    manifest = project_store.load_manifest(project_id)
    return {
        "project_id": project_id,
        "card_count": len(cards),
        "active_clip_id": manifest.get("active_clip_id"),
        "active_clip_updated_at": manifest.get("active_clip_updated_at"),
        "cards": cards,
    }


@router.get("/studio/projects/{project_id}/clips")
async def studio_project_clips(project_id: str):
    """项目成片卡片（左侧素材列表的唯一事实源）。

    按**稳定 clip_id** 渲染：同一 clip 无论经历多少次封面/标题/字幕/时间线修改，
    都只有这一张卡；封面更新原地替换 card 的 cover 与缩略图。
    """
    try:
        return await run_in_threadpool(_project_cards_payload, project_id)
    except ProjectStoreError as exc:
        raise _project_error(exc) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/studio/projects/{project_id}/sync")
async def studio_project_sync(project_id: str):
    """把项目名下的 AUTO_EDIT 任务回填进台账（老项目无需重跑即可获得稳定卡片）。"""
    try:
        return await run_in_threadpool(_project_cards_payload, project_id)
    except ProjectStoreError as exc:
        raise _project_error(exc) from exc


def _purge_error(exc: Exception) -> HTTPException:
    """清空失败/被拦的错误映射：冲突类用 409（前端据此提示刷新重试）。"""
    code = str(getattr(exc, "code", "") or "")
    message = str(exc)
    if code and code not in message:
        message = f"{code}: {message}"
    conflict = {studio_project_purge.E_PURGE_ACTIVE_TASKS,
                studio_project_purge.E_PURGE_CARD_COUNT_MISMATCH}
    status = 409 if code in conflict else 422
    detail: Any = message
    extra = getattr(exc, "detail", None)
    if extra:
        detail = {"detail": message, **extra}
    return HTTPException(status_code=status, detail=detail)


class ProjectDataClearRequest(BaseModel):
    """清空**当前项目**任务数据的请求体。

    ``confirm`` 必须为 true（二次确认的机器可读凭证）；``expected_card_count`` 是
    防误删闸门——必须等于当前台账卡片数，否则 409 让用户刷新后重看一遍。
    """

    confirm: bool = False
    expected_card_count: int | None = None
    # 排障用：purge=false 时产物只移入回收站、不做物理删除。
    purge: bool = True


@router.get("/studio/projects/{project_id}/data")
async def studio_project_data(project_id: str):
    """清空前的只读预览：将删多少卡/多少产物、可释放多少空间、哪些东西会保留。"""
    try:
        return await run_in_threadpool(studio_project_purge.summarize, project_id)
    except ProjectStoreError as exc:
        raise _project_error(exc) from exc


@router.post("/studio/projects/{project_id}/data/clear")
async def studio_clear_project_data(project_id: str, body: ProjectDataClearRequest):
    """清空当前项目的任务记录与产物（保留源片与已完成转写）。

    为什么是显式端点而不是「刷新即删」：台账与任务记录都是落盘持久数据，删除不可
    撤销；刷新触发会把一次误按 F5 升级成数据丢失。终态闸门、卡片基数校验与两阶段
    删除见 ``studio_project_purge``。
    """
    if body.expected_card_count is None:
        raise HTTPException(
            status_code=422,
            detail="E_PURGE_CARD_COUNT_MISMATCH: 缺少 expected_card_count（需等于当前卡片数）",
        )
    try:
        return await run_in_threadpool(
            lambda: studio_project_purge.clear_project_tasks(
                project_id,
                expected_card_count=int(body.expected_card_count),
                confirm=bool(body.confirm),
                purge=bool(body.purge),
            )
        )
    except studio_project_purge.PurgeError as exc:
        raise _purge_error(exc) from exc
    except ProjectStoreError as exc:
        raise _project_error(exc) from exc


class LocalTaskRequest(BaseModel):
    """局部任务创建请求。

    ``task_type`` ∈ {REGENERATE_COVER, UPDATE_TITLE, UPDATE_SUBTITLE_STYLE, UPDATE_TIMELINE}；
    AUTO_EDIT 被显式拒绝（只能由「开始自动剪辑」入口提交）。
    """

    task_type: str
    instruction: str | None = Field(default=None, max_length=1000)
    payload: dict[str, Any] = Field(default_factory=dict)
    # 助手/UI 常直接把风格、文案、时间线放在顶层，这里兼容收进 payload。
    style_id: str | None = None
    replace_intro: bool | None = None
    cover_copy: dict[str, Any] | None = None
    title: str | None = Field(default=None, max_length=200)


@router.post("/studio/projects/{project_id}/clips/{clip_id}/tasks", status_code=202)
async def studio_create_clip_task(project_id: str, clip_id: str, body: LocalTaskRequest):
    """创建**类型化**的局部任务（封面/标题/字幕/时间线），逐条路由到正确通道。

    这是修复「重新制作封面 = 重跑自动剪辑 = 多一张重复卡片」的核心入口：
    REGENERATE_COVER 只更新该 clip 的封面资产，绝不新建任务卡片、绝不重渲正片。
    """
    task_type = str(body.task_type or "").strip().upper()
    if task_type == TASK_AUTO_EDIT:
        raise HTTPException(
            status_code=422,
            detail="E_TASK_TYPE_UNKNOWN: AUTO_EDIT 只能由「开始自动剪辑」入口提交；"
                   "已有成片的修改请用 REGENERATE_COVER / UPDATE_TITLE / "
                   "UPDATE_SUBTITLE_STYLE / UPDATE_TIMELINE")
    if task_type not in TASK_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"E_TASK_TYPE_UNKNOWN: 未知任务类型 {task_type!r}；可用: "
                   f"{[t for t in TASK_TYPES if t != TASK_AUTO_EDIT]}")
    payload = dict(body.payload or {})
    for key, value in (("style_id", body.style_id), ("replace_intro", body.replace_intro),
                       ("cover_copy", body.cover_copy), ("title", body.title)):
        if value is not None:
            payload.setdefault(key, value)
    try:
        record = await run_in_threadpool(
            lambda: create_local_task(project_id=project_id, clip_id=clip_id,
                                      task_type=task_type, instruction=body.instruction,
                                      payload=payload))
    except LocalTaskError as exc:
        raise _project_error(exc) from exc
    except ProjectStoreError as exc:
        raise _project_error(exc) from exc
    except skill_registry.SkillRegistryError as exc:
        # 规则源不可用（清单缺失/损坏）→ 显式失败：不按「无规则」创建局部任务。
        raise HTTPException(status_code=503,
                            detail=f"{exc.code}: {exc.message}") from exc
    register_created_object("task", str(record["task_id"]), project_id)
    return {
        "task_id": record["task_id"],
        "task_type": record["task_type"],
        "project_id": record["project_id"],
        "clip_id": record["clip_id"],
        "status": record["status"],
        "stage": record["stage"],
        "created_at": record["created_at"],
        # 本次任务冻结的规则版本（用户提交那一刻的规则，不是执行时的磁盘规则）。
        "workflow_version": record.get("workflow_version"),
        "skill_versions": record.get("skill_versions") or {},
        "workflow_snapshot_path": record.get("workflow_snapshot_path"),
    }


@router.get("/studio/tasks/{task_id}")
async def studio_local_task_status(task_id: str):
    """轮询一个局部任务的进度与结果（助手第 4/5 步：轮询 → 原地刷新卡片）。"""
    try:
        record = await run_in_threadpool(get_local_task, task_id)
    except LocalTaskError as exc:
        raise _project_error(exc) from exc
    return {
        "task_id": record.get("task_id"),
        "task_type": record.get("task_type") or task_type_of(task_id),
        "project_id": record.get("project_id"),
        "clip_id": record.get("clip_id"),
        "status": record.get("status"),
        "stage": record.get("stage"),
        "progress": record.get("progress", 0.0),
        "error": record.get("error"),
        "result": record.get("result"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        # 规则版本：任务详情 API 直接可见（验收：任务详情明确记录 workflow_version 与各 skill 版本）。
        "workflow_version": record.get("workflow_version"),
        "skill_versions": record.get("skill_versions") or {},
        "workflow_snapshot_path": record.get("workflow_snapshot_path"),
    }


@router.get("/studio/projects/{project_id}/tasks")
async def studio_project_tasks(project_id: str, clip_id: str | None = None):
    """项目任务表（含 task_type）：验证「只建局部任务、不建 auto_edit」的直接证据。"""
    try:
        tasks = await run_in_threadpool(
            lambda: list_local_tasks(project_id=project_id, clip_id=clip_id))
    except LocalTaskError as exc:
        raise _project_error(exc) from exc
    await run_in_threadpool(sync_from_tasks, project_id)
    return {"project_id": project_id, "count": len(tasks), "tasks": tasks}


@router.get("/studio/projects/{project_id}/clips/{clip_id}/cover-history")
async def studio_clip_cover_history(project_id: str, clip_id: str):
    """该 clip 的封面历史版本（UI：「封面已更新 · v2 / 撤销到 v1」）。"""
    try:
        revisions = await run_in_threadpool(cover_history, project_id, clip_id)
        cards = await run_in_threadpool(list_cards, project_id)
    except (LocalTaskError, ProjectStoreError) as exc:
        raise _project_error(exc) from exc
    card = next((item for item in cards if item.get("clip_id") == clip_id), None)
    return {
        "project_id": project_id,
        "clip_id": clip_id,
        "cover": (card or {}).get("cover"),
        "revisions": revisions,
    }


@router.post("/studio/projects/{project_id}/clips/{clip_id}/cover/undo")
async def studio_clip_cover_undo(project_id: str, clip_id: str, request: Request):
    """撤销封面到历史版本（默认 v1），**原地生效**：卡片/预览/导出同步回到该版本。"""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    revision = payload.get("revision", 1)
    try:
        revision = int(revision)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="revision 必须是整数") from exc
    try:
        return await run_in_threadpool(undo_cover_revision, project_id, clip_id, revision)
    except CoverReplaceError as exc:
        code = str(getattr(exc, "code", ""))
        raise HTTPException(status_code=422,
                            detail=f"{code}: {exc}" if code and code not in str(exc) else str(exc)) from exc
    except (LocalTaskError, ProjectStoreError) as exc:
        raise _project_error(exc) from exc


@router.post("/studio/projects/{project_id}/clips/{clip_id}/cover/activate")
async def studio_activate_clip_cover(project_id: str, clip_id: str, request: Request):
    """「设为当前封面」：把历史里任意一版原地设为当前（封面版本管理页）。

    - 不新增 revision、不新建 clip、不触发 AUTO_EDIT；
    - 未通过封面 QA 的版本显式拒绝（E_COVER_QA_FAILED），绝不静默换封面。
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    if "revision" not in payload:
        raise HTTPException(status_code=422, detail="缺少 revision")
    try:
        revision = int(payload.get("revision"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="revision 必须是整数") from exc
    try:
        return await run_in_threadpool(activate_cover, project_id, clip_id, revision)
    except CoverReplaceError as exc:
        code = str(getattr(exc, "code", ""))
        raise HTTPException(status_code=422,
                            detail=f"{code}: {exc}" if code and code not in str(exc) else str(exc)) from exc
    except (LocalTaskError, ProjectStoreError) as exc:
        raise _project_error(exc) from exc


@router.delete("/studio/projects/{project_id}/clips/{clip_id}/cover/revisions/{revision}")
async def studio_delete_clip_cover_revision(project_id: str, clip_id: str, revision: int):
    """删除未被当前使用的历史封面版本（v1 基线与当前版本拒绝删除）。"""
    try:
        return await run_in_threadpool(delete_cover, project_id, clip_id, revision)
    except CoverReplaceError as exc:
        code = str(getattr(exc, "code", ""))
        raise HTTPException(status_code=422,
                            detail=f"{code}: {exc}" if code and code not in str(exc) else str(exc)) from exc
    except (LocalTaskError, ProjectStoreError) as exc:
        raise _project_error(exc) from exc


# ---------------------------------------------------------------------------
# 规则中心（剪辑规则注册表 / Phase B 接线）
#
# 四条能力：
# 1. GET  /studio/workflow                     —— 规则中心数据源（**每次实时读清单**）
# 2. GET  /studio/workflow/{task_id}/snapshot  —— 某任务/成片当时用了哪些规则
# 3. POST /studio/workflow/skills/{id}/toggle  —— 启停 skill（备份 → 写回 → 校验 → 回滚）
# 4. POST /studio/workflow/regenerate/...      —— 按最新规则重新生成（新 revision，不新增卡片）
# ---------------------------------------------------------------------------


def _workflow_error(exc: Exception) -> HTTPException:
    """注册表 / 接线层异常 → 可读 HTTP 错误（保留 E_ 机器码）。"""
    code = str(getattr(exc, "code", "") or "")
    message = str(exc)
    if code and code not in message:
        message = f"{code}: {message}"
    status = 404 if code == studio_workflow.E_SKILL_UNKNOWN else 422
    return HTTPException(status_code=status, detail=message)


@router.get("/studio/workflow")
async def studio_workflow_registry():
    """规则中心数据源：当前规则版本 banner + 各 skill 版本/校验状态。

    **每次调用都实时读清单**（不缓存、不落盘）：外部改了 skill 文件或调了 toggle 端点后，
    规则中心刷新一次就能看到新版本 / 新的校验状态，无需重启浏览器或后端。
    """
    try:
        banner = await run_in_threadpool(skill_registry.active_workflow_banner)
        summary = await run_in_threadpool(skill_registry.describe_active_workflow)
    except skill_registry.SkillRegistryError as exc:
        # 规则源不可用（清单缺失/损坏）→ 显式 503，绝不返回一份编造的「默认规则」。
        raise HTTPException(status_code=503,
                            detail=f"{exc.code}: {exc.message}") from exc
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"E_MANIFEST_INVALID: {exc}") from exc
    return {"banner": banner, "summary": summary}


def _workflow_snapshot_payload(task_id: str) -> dict[str, Any]:
    """读某任务的规则快照（旧成片据此回看「当时用的是哪版规则」）。"""
    task_id = studio_workflow.validate_task_id(task_id)
    snapshot = skill_registry.read_workflow_snapshot(
        studio_workflow.snapshot_dir(TASKS_ROOT, task_id))
    if not snapshot:
        # 旧任务（Phase B 接线前生成）确实没有快照：如实说明，不伪造一份历史规则。
        return {
            "task_id": task_id,
            "found": False,
            "snapshot_path": str(studio_workflow.snapshot_dir(TASKS_ROOT, task_id)
                                 / skill_registry.SNAPSHOT_FILENAME),
            "notice": "该任务早于剪辑规则注册表接线（Phase B 之前），没有规则快照；"
                      "可查看项目台账里该成片 task_history 记录的版本字段。",
        }
    return {
        "task_id": task_id,
        "found": True,
        "version_line": skill_registry.workflow_version_line(snapshot),
        "snapshot": snapshot,
    }


@router.get("/studio/workflow/{task_id}/snapshot")
async def studio_workflow_snapshot(task_id: str):
    """该任务的规则快照（成片详情「查看本次成片实际使用了哪些规则」）。"""
    try:
        return await run_in_threadpool(_workflow_snapshot_payload, task_id)
    except studio_workflow.WorkflowError as exc:
        raise _workflow_error(exc) from exc


class WorkflowSkillToggleRequest(BaseModel):
    """启停 skill：``enabled`` 省略时取反当前状态。"""

    enabled: bool | None = None


@router.post("/studio/workflow/skills/{skill_id}/toggle")
async def studio_workflow_toggle_skill(skill_id: str,
                                       body: WorkflowSkillToggleRequest | None = None):
    """启停某个 skill（改清单前备份 → 写回 → 立刻校验 → 非法则回滚并 4xx）。"""
    enabled = body.enabled if body is not None else None
    try:
        return await run_in_threadpool(studio_workflow.set_skill_enabled, skill_id, enabled)
    except skill_registry.SkillRegistryError as exc:
        raise HTTPException(status_code=422, detail=f"{exc.code}: {exc.message}") from exc
    except studio_workflow.WorkflowError as exc:
        raise _workflow_error(exc) from exc


class WorkflowRegenerateRequest(BaseModel):
    """「按最新规则重新生成此成片」。

    ``mode=cover``（默认）：只重做封面（+0.8s 片头），cover revision +1；
    ``mode=render``：把该 clip 真正重渲一遍（提交 video-brain 单切片渲染任务）。
    两条通道都**只作用在台账里已有的 clip 上**：不新建无关卡片、旧成果保留在历史里。
    """

    mode: str = "cover"
    style_id: str | None = None
    instruction: str | None = Field(default=None, max_length=1000)


@router.post("/studio/workflow/regenerate/{project_id}/{clip_id}", status_code=202)
async def studio_workflow_regenerate(project_id: str, clip_id: str,
                                     body: WorkflowRegenerateRequest | None = None):
    """按**最新**规则重新生成一条已有成片：新增 revision，不覆盖旧成果、不新增卡片。"""
    request_body = body or WorkflowRegenerateRequest()
    mode = str(request_body.mode or "cover").strip().lower()
    if mode not in {"cover", "render"}:
        raise HTTPException(status_code=422, detail="mode 只能是 cover 或 render")
    try:
        entry = await run_in_threadpool(project_entry_for, project_id, clip_id)
    except ProjectStoreError as exc:
        raise _project_error(exc) from exc
    cards_before = await run_in_threadpool(list_cards, project_id)
    cover = entry.get("cover") if isinstance(entry.get("cover"), dict) else {}
    timeline = entry.get("timeline") if isinstance(entry.get("timeline"), dict) else {}
    payload: dict[str, Any] = {}
    task_type = TASK_REGENERATE_COVER
    if mode == "cover":
        if request_body.style_id:
            payload["style_id"] = request_body.style_id
        payload["replace_intro"] = True
    else:
        task_type = TASK_UPDATE_TIMELINE
        payload = {
            "ranges": timeline.get("ranges") or [{
                "start": float(entry.get("source_start") or 0.0),
                "end": float(entry.get("source_end") or 0.0),
            }],
            "rerender": True,
        }
    try:
        record = await run_in_threadpool(
            lambda: create_local_task(project_id=project_id, clip_id=clip_id,
                                      task_type=task_type,
                                      instruction=request_body.instruction,
                                      payload=payload))
    except (LocalTaskError, ProjectStoreError) as exc:
        raise _project_error(exc) from exc
    except skill_registry.SkillRegistryError as exc:
        # 规则源不可用（清单缺失/损坏）→ 显式失败，不按「无规则」重新生成。
        raise HTTPException(status_code=503,
                            detail=f"{exc.code}: {exc.message}") from exc
    register_created_object("task", str(record["task_id"]), project_id)
    return {
        "task_id": record["task_id"],
        "task_type": record["task_type"],
        "project_id": project_id,
        "clip_id": clip_id,
        "mode": mode,
        "status": record["status"],
        "stage": record["stage"],
        "created_at": record["created_at"],
        # 版本可见性：本次用的是**刚刚冻结**的新规则版本。
        "workflow_version": record.get("workflow_version"),
        "skill_versions": record.get("skill_versions") or {},
        "workflow_snapshot_path": record.get("workflow_snapshot_path"),
        # 前后对比证据：卡片数（不变）与该 clip 现有封面/画面 revision。
        "cards_before": len(cards_before),
        "cover_revision_before": int(cover.get("revision") or 0),
        "render_revision_before": int(entry.get("render_revision") or 1),
        "note": "已按最新规则创建重新生成任务：新增一个 revision，旧成果保留在历史里，"
                "不会新增成片卡片；完成后卡片数保持不变。",
    }


# ---------------------------------------------------------------------------
# Inspector Phase 1（契约 §5）：clip 配置 / 转写修订 / 字幕版本 / BGM / 导出摘要
#
# 与既有 /studio/projects/... 路由同风格：
# * 阻塞写盘一律 run_in_threadpool；
# * 领域异常 → 带 E_ 码的 422（detail = "CODE: 可读原因"）；
# * ProjectStoreError 沿用既有映射（E_CLIP_NOT_REGISTERED → 404），FileNotFoundError → 404，
#   坏 JSON / 非对象 body → 400；
# * 这些端点**只写配置与版本**，绝不创建 autoedit_* 任务（真渲染属 Phase 2）。
# ---------------------------------------------------------------------------

#: 转写 / 字幕是**成片级**资产：保留哨兵 clip_id="source" 只有 config / render-config 两层。
E_SOURCE_BINDING_UNSUPPORTED = "E_SOURCE_BINDING_UNSUPPORTED"
#: 字幕标签统一文案（与前端 §6 文案一致）。
SUBTITLE_PREVIEW_NOTE = "字幕版本用于预览与版本追溯；烧录进导出成片属 Phase 2"


def _inspector_error(exc: Exception) -> HTTPException:
    """Phase 1 领域异常 → HTTP：带 E_ 码 → 422，未登记 clip / 文件缺失 → 404，其它 → 400/500。

    与 ``_project_error`` 同一风格（错误码保真，UI 直接展示 ``CODE: 原因``）；
    局部任务/台账异常沿用既有映射，避免同一种失败在不同端点给出不同状态码。
    """
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (ProjectStoreError, LocalTaskError)):
        return _project_error(exc)   # E_CLIP_NOT_REGISTERED / E_TASK_NOT_FOUND → 404
    code = str(getattr(exc, "code", "") or "")
    if code:
        message = str(exc)
        if code not in message:
            message = f"{code}: {message}"
        return HTTPException(status_code=422, detail=message)
    if isinstance(exc, (clip_config.ClipConfigError, transcript_revisions.TranscriptRevisionError,
                        subtitle_compose.SubtitleComposeError, clip_audio.ClipAudioError)):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=f"Inspector 操作失败: {exc}")


async def _json_body(request: Request) -> dict[str, Any]:
    """读 JSON body：空体 → ``{}``；坏 JSON / 非对象 → 400。

    与 ``_cover_body`` 同一实现（封面通道先落地的那套语义），这里只是给 Inspector
    端点一个语义清晰的名字，避免两处各写一份 body 解析、两处各有一套状态码。
    """
    return await _cover_body(request)


def _tasks_root() -> Path:
    """任务根目录：与台账/局部任务同一事实源（测试可打桩 studio_project_store.TASKS_ROOT）。"""
    return Path(project_store.TASKS_ROOT)


def _clip_entry_and_dir(project_id: str, clip_id: str) -> tuple[dict[str, Any], Path]:
    """解析 clip 台账条目与其成片资产目录 ``output/{render_task_id}/{clip_id}``。

    未登记 → ``E_CLIP_NOT_REGISTERED``（404 可映射）；没有 ``render_task_id`` 的坏条目
    同样显式报同一个码，而不是 500 或写进一个猜出来的目录。
    """
    entry = project_entry_for(project_id, clip_id)
    render_task_id = str(entry.get("render_task_id") or "")
    if not render_task_id:
        raise LocalTaskError("E_CLIP_NOT_REGISTERED",
                             f"clip {clip_id} 没有 render_task_id，无法定位成片资产目录")
    from ...studio_clip_locator import clip_dir as locate_clip_dir
    return entry, locate_clip_dir(_tasks_root(),render_task_id,clip_id,require_video=True)


def _reject_source_binding(clip_id: str, feature: str) -> None:
    """转写 / 字幕是成片级资产：保留哨兵 ``source`` 只有配置层（config / render-config）。

    显式 422 + 可读原因，避免把 ``source`` 当成一个 clip_id 去查台账而变成 500。
    """
    if str(clip_id or "").strip() == clip_config.SOURCE_CLIP_ID:
        raise HTTPException(
            status_code=422,
            detail=f'{E_SOURCE_BINDING_UNSUPPORTED}: {feature} 以成片 clip 为单位，'
                   f'保留哨兵 clip_id="source" 只有 config / render-config 两层')


def _clip_seed_segments(entry: dict[str, Any], clip_id: str) -> list[dict[str, Any]]:
    """该成片产物自带的分段（转写惰性 seed 与字幕合成共用）。"""
    return clip_segments_from_render_task(str(entry.get("render_task_id") or ""), clip_id)


def _correction_audit(project_id: str) -> list[dict[str, Any]] | None:
    """该项目转写 artifact 里的 ``correction_audit``（**原始 ASR ↔ 纠错稿**对照）。

    为什么需要它：video-brain 会把任务 JSON 的 ``asr_segments`` 换成**纠错后**文本，
    所以只从成片产物 seed 的话「原始稿 == 纠错稿」，Inspector 的三态切换就没有对照意义
    （任务书要求给出「原始 ASR 未被覆盖」的证据）。原始 ASR 只留在转写 artifact 的
    ``correction_audit[].raw_text`` 里。

    查找顺序：``{temp_dir}/studio-jobs/{job}/transcript.json`` →
    ``{temp_dir}/objectstore/studio-artifacts/{job}/transcript.json`` →
    ``backend/studio-artifacts/{job}/transcript.json``；按 project_id 推导出的 job 前缀
    （``timeline_{job[:12]}``）过滤。找不到就返回 ``None``（调用方按「只有纠错稿」退化处理，
    绝不编造原始稿）。
    """
    prefix = project_id[len("timeline_"):] if project_id.startswith("timeline_") else project_id
    candidates: list[Path] = []
    try:
        temp_dir = Path(get_config().temp_dir)
        candidates.extend(sorted((temp_dir / "studio-jobs").glob("*/transcript.json")))
        candidates.extend(sorted((temp_dir / "objectstore" / "studio-artifacts").glob("*/transcript.json")))
    except Exception:  # noqa: BLE001 — 配置不可用不影响转写视图
        pass
    candidates.extend(sorted((Path(__file__).resolve().parents[3] / "studio-artifacts").glob("*/transcript.json")))
    for path in reversed(candidates):   # 同前缀取最新
        if prefix and not path.parent.name.startswith(prefix):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        audit = payload.get("correction_audit")
        if isinstance(audit, list) and audit:
            return audit
    return None


def _clip_seed_baseline(project_id: str, entry: dict[str, Any],
                        clip_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """转写基线 ``(原始 ASR 段, 纠错稿段)``，都已平移到**成片时间基**。

    纠错稿来自成片产物 ``segments``（含 0.8s 封面片头偏移，与播放器一致）；
    原始 ASR 从 ``correction_audit`` 按同一窗口/偏移重建。拿不到 audit 时
    ``raw == corrected``，诚实退化而不是伪造一份「原始稿」。
    """
    corrected = _clip_seed_segments(entry, clip_id)
    audit = _correction_audit(project_id)
    if not audit or not corrected:
        return corrected, corrected
    source_start = float(entry.get("source_start") or 0.0)
    source_end = float(entry.get("source_end") or 0.0)
    raw: list[dict[str, Any]] = []
    for index, item in enumerate(audit, 1):
        if not isinstance(item, dict):
            continue
        try:
            start, end = float(item.get("start")), float(item.get("end"))
        except (TypeError, ValueError):
            continue
        if end <= source_start or start >= source_end:
            continue
        text = str(item.get("raw_text") or "").strip()
        if not text:
            continue
        raw.append({
            "segment_id": f"seg-{len(raw) + 1:03d}",
            "start": round(max(start, source_start) - source_start + 0.8, 3),
            "end": round(min(end, source_end) - source_start + 0.8, 3),
            "text": text,
            "speaker": "未标注",
        })
    return (raw or corrected), corrected


def _seed_transcript_document(project_id: str, entry: dict[str, Any],
                              clip_id: str) -> dict[str, Any]:
    """惰性 seed 转写文档（**幂等**：已 seed 的文档绝不被覆盖，见 ``seed_document``）。

    同时写入两个基线，于是三态切换有真实对照：
    ``raw`` = 任务侧 ``correction_audit`` 的原始 ASR，``corrected`` = 成片产物的纠错文本。
    拿不到 audit 时两者相同（诚实退化，不伪造原始稿）。
    """
    raw, corrected = _clip_seed_baseline(project_id, entry, clip_id)
    if not raw and not corrected:
        # 没有任何基线可 seed：保持「未 seed」状态，让 document_view 抛
        # E_TRANSCRIPT_NOT_SEEDED（可读 422），而不是先落一个空文档。
        return None
    return transcript_revisions.seed_document(
        project_id, clip_id, raw_segments=raw, corrected_segments=corrected)


def _transcript_view(project_id: str, clip_id: str, view: str | None = None) -> dict[str, Any]:
    """三态稿 + 修订列表；``view=raw|corrected|effective`` 时附带该视图的段与文本。

    文档不存在时用成片自带 ``segments`` **惰性 seed**：新成片第一次打开转写标签就能用，
    不必先单独调一次建基线接口（已 seed 的文档绝不被覆盖）。
    """
    _reject_source_binding(clip_id, "转写修订")
    entry, _clip_dir = _clip_entry_and_dir(project_id, clip_id)
    variant = str(view or "effective").strip() or "effective"
    if variant not in transcript_revisions.VARIANTS:
        raise transcript_revisions.TranscriptRevisionError(
            transcript_revisions.E_TRANSCRIPT_VARIANT_UNKNOWN,
            f"view 只能是 {'/'.join(transcript_revisions.VARIANTS)}，收到: {view!r}")
    _seed_transcript_document(project_id, entry, clip_id)
    document = transcript_revisions.document_view(project_id, clip_id)
    document["view"] = variant
    document["view_text"] = document.get(f"{variant}_text") or ""
    document["view_segments"] = document.get(f"{variant}_segments") or []
    return document


def _add_transcript_revision(project_id: str, clip_id: str,
                             payload: dict[str, Any]) -> dict[str, Any]:
    """新增一条转写修订 ``{author?, reason?, changes[]}``（raw/corrected 基线永不改写）。

    文档未 seed 时先用成片分段惰性 seed（与 GET 同一规则）再追加；
    changes 的结构与归属校验在 ``transcript_revisions``
    （坏数据 → ``E_TRANSCRIPT_CHANGES_INVALID``，未 seed → ``E_TRANSCRIPT_NOT_SEEDED``）。
    """
    _reject_source_binding(clip_id, "转写修订")
    entry, _clip_dir = _clip_entry_and_dir(project_id, clip_id)
    _seed_transcript_document(project_id, entry, clip_id)
    result = transcript_revisions.add_revision(
        project_id, clip_id,
        changes=payload.get("changes"),
        reason=payload.get("reason"),
        author=str(payload.get("author") or "editor"),
    )
    # 返回**前端可直接渲染的视图**（三态稿 + 修订列表），再补上本次的 revision/mirror：
    # 只回文档本身的话，前端还得自己算 effective，容易出现两套合并规则。
    view = transcript_revisions.document_view(project_id, clip_id)
    return {**view, "revision": result["revision"], "mirror": result["mirror"],
            "mirror_error": result["mirror_error"]}


def _rollback_transcript(project_id: str, clip_id: str,
                         payload: dict[str, Any]) -> dict[str, Any]:
    """``{revision}`` 把批准号指回一条已存在的修订（只改生效稿，两个基线不动）。"""
    _reject_source_binding(clip_id, "转写修订")
    if payload.get("revision") is None:
        raise HTTPException(
            status_code=422,
            detail=f"{transcript_revisions.E_TRANSCRIPT_REVISION_UNKNOWN}: 缺少 revision"
                   "（要回滚到的已批准修订号）")
    result = transcript_revisions.rollback(project_id, clip_id, payload["revision"])
    view = transcript_revisions.document_view(project_id, clip_id)
    return {**view, "revision": result["revision"], "mirror": result["mirror"],
            "mirror_error": result["mirror_error"]}


def _relative_asset(clip_dir: Path, path: Any) -> str | None:
    """把资产绝对路径投影成 clip 目录内的相对路径（前端只消费相对路径）。"""
    try:
        return Path(str(path)).relative_to(Path(clip_dir)).as_posix()
    except (TypeError, ValueError):
        return None


def _subtitle_revisions(clip_dir: Path, clip_id: str) -> list[dict[str, Any]]:
    """字幕版本列表（读 ``subtitles/rev-NNN/meta.json``，按版本升序）。"""
    revisions: list[dict[str, Any]] = []
    for meta in subtitle_compose.list_revisions(Path(clip_dir)):
        revisions.append({
            "revision": int(meta.get("revision") or 0),
            "clip_id": str(meta.get("clip_id") or clip_id),
            "ass_path": meta.get("ass_rel") or _relative_asset(clip_dir, meta.get("ass_path")),
            "srt_path": meta.get("srt_rel") or _relative_asset(clip_dir, meta.get("srt_path")),
            "cue_count": int(meta.get("cue_count") or 0),
            "config_hash": meta.get("config_hash"),
            "style_id": meta.get("style_id"),
            "font_size_px": meta.get("font_size_px"),
            "margin_v": meta.get("margin_v"),
            "task_id": meta.get("task_id"),
            "created_at": meta.get("created_at"),
        })
    return revisions


def _subtitle_state(project_id: str, clip_id: str) -> dict[str, Any]:
    """字幕标签读写共用的状态：有效段 → cues、config、overrides、clip 目录。"""
    entry, clip_dir = _clip_entry_and_dir(project_id, clip_id)
    view = clip_config.config_view(project_id, clip_id)
    config = view["config"]["subtitle_config"]
    overrides = view["config"]["subtitle_overrides"]
    _seed_transcript_document(project_id, entry, clip_id)
    segments = effective_transcript_segments(project_id, clip_id)
    cues = subtitle_compose.build_cues(segments, overrides=overrides)
    return {"entry": entry, "clip_dir": clip_dir, "config": config,
            "overrides": overrides, "cues": cues}


def _clip_subtitles_payload(project_id: str, clip_id: str) -> dict[str, Any]:
    """GET .../subtitles：cues + config + overrides + 版本列表（契约 §5 形状）。"""
    _reject_source_binding(clip_id, "字幕版本")
    state = _subtitle_state(project_id, clip_id)
    revisions = _subtitle_revisions(state["clip_dir"], clip_id)
    latest = int(revisions[-1]["revision"]) if revisions else 0
    return {
        "project_id": project_id,
        "clip_id": clip_id,
        "binding": "clip",
        "config": state["config"],
        "overrides": state["overrides"],
        "cues": state["cues"],
        "revisions": revisions,
        "latest_revision": latest,
        "export_burn_in": "phase2",
        "note": SUBTITLE_PREVIEW_NOTE,
    }


def _save_clip_subtitles(project_id: str, clip_id: str,
                         payload: dict[str, Any]) -> dict[str, Any]:
    """POST .../subtitles：保存 config/overrides（cues 转 overrides）并生成新版 ASS/SRT。

    配置落盘走 ``clip_config.update_config``：严格校验、部分深合并、只对**真变化**的段
    bump ``render_revision``，并追加一条 ``<prefix>_<12hex>`` 的 task_history
    —— 绝不是 ``autoedit_*``（「改字幕 ≠ 重跑自动剪辑」）。
    """
    _reject_source_binding(clip_id, "字幕版本")
    from_cues = (subtitle_overrides_from_cues(payload.get("cues"))
                 if payload.get("cues") is not None else None)
    overrides = merge_subtitle_overrides(payload.get("overrides"), from_cues)
    patch: dict[str, Any] = {}
    if payload.get("config") is not None:
        patch["subtitle_config"] = payload["config"]
    if overrides is not None:
        patch["subtitle_overrides"] = overrides

    entry, clip_dir = _clip_entry_and_dir(project_id, clip_id)
    # 先**试算**（apply_patch 校验 + 深合并，不落盘）再合成：没有 cue 时
    # E_SUBTITLE_NO_CUES 在写任何配置之前抛出，台账里不留「配置改了但没版本」的半成品。
    current = clip_config.config_view(project_id, clip_id)["config"]
    probe, _changed = (clip_config.apply_patch(current, patch) if patch
                       else (current, []))
    _seed_transcript_document(project_id, entry, clip_id)
    segments = effective_transcript_segments(project_id, clip_id)
    cues = subtitle_compose.build_cues(segments, overrides=probe["subtitle_overrides"])
    task_id = str(payload.get("task_id") or "").strip() or None
    composed = subtitle_compose.compose(clip_dir, clip_id=clip_id, cues=cues,
                                        config=probe["subtitle_config"], task_id=task_id)

    # 版本已落地 → 才把配置写进台账（clip_config.update_config：只对真变化 bump 版本，
    # 并追加一条 <prefix>_<12hex> 的 task_history —— 绝不是 autoedit_*）。
    updated = (clip_config.update_config(project_id, clip_id, patch)
               if patch else clip_config.config_view(project_id, clip_id))
    config = updated["config"]["subtitle_config"]
    final_overrides = updated["config"]["subtitle_overrides"]
    return {
        "project_id": project_id,
        "clip_id": clip_id,
        "binding": "clip",
        "revision": composed["revision"],
        "ass_path": _relative_asset(clip_dir, composed["ass_path"]),
        "srt_path": _relative_asset(clip_dir, composed["srt_path"]),
        "cue_count": composed["cue_count"],
        "cues": cues,
        "config": config,
        "overrides": final_overrides,
        "changed": updated.get("changed") or [],
        "revisions": _subtitle_revisions(clip_dir, clip_id),
        "latest_revision": composed["revision"],
        "export_burn_in": "phase2",
        "note": SUBTITLE_PREVIEW_NOTE,
        # reason 只回显给 UI（字幕版本的 meta.json 不存原因字段，不在这里编造字段）。
        "reason": payload.get("reason"),
    }


def _save_clip_bgm(project_id: str, clip_id: str, *, clip_dir: Path, filename: str,
                   source_path: Path, volume: Any = None, fade_in_s: Any = None,
                   fade_out_s: Any = None, max_bytes: int) -> dict[str, Any]:
    """把上传的 BGM 落进该 clip 的 ``audio/`` 并登记进 ``audio_tracks``。

    顺序：表单里的 volume/fade 先过一遍范围校验（越界要在复制文件**之前**失败，
    否则 ``audio/`` 里会留下一个没有任何音轨引用的孤儿文件）→ ``clip_audio.save_bgm``
    （扩展名白名单 + 体积上限 + 内容寻址复制 + 时长/峰值）→ 应用表单覆盖值
    → 追加到该 clip 的 ``audio_tracks``（``clip_config`` 再校验一次）
    → render_revision+1 → 记一条 ``aud_*`` 任务历史（不是 autoedit_*）。
    混音导出属 Phase 2。
    """
    form_overrides = {key: value for key, value in
                      (("volume", volume), ("fade_in_s", fade_in_s),
                       ("fade_out_s", fade_out_s)) if value is not None}
    probe = clip_audio.normalize_track({
        "id": "probe", "asset_id": "probe", "kind": "bgm", "path": "probe",
        "filename": "probe", **form_overrides})

    track = clip_audio.save_bgm(Path(clip_dir), clip_id=clip_id, filename=filename,
                                source_path=Path(source_path), max_bytes=max_bytes)
    for key in form_overrides:
        track[key] = probe[key]
    track = clip_audio.normalize_track(track)

    entry = project_entry_for(project_id, clip_id)
    existing = clip_config.normalize_audio_tracks(entry.get("audio_tracks") or [])
    tracks = [item for item in existing
              if str(item.get("asset_id") or item.get("id")) != str(track["asset_id"])]
    tracks.append(track)
    tracks = clip_config.normalize_audio_tracks(tracks)
    project_update_fields(project_id, clip_id, {"audio_tracks": tracks})

    task_id = f"aud_{uuid.uuid4().hex[:12]}"
    stamp = project_store.utc_now()
    manifest = project_bump_render_revision(
        project_id, clip_id, task_id=task_id, kind=TASK_UPDATE_AUDIO,
        instruction=f"上传 BGM {track['filename']}")
    project_record_task(project_id, clip_id, {
        "task_id": task_id, "task_type": TASK_UPDATE_AUDIO, "status": "completed",
        "stage": "BGM 已上传（混音导出属 Phase 2）",
        "instruction": f"上传 BGM {track['filename']}",
        "created_at": stamp, "updated_at": stamp,
        "summary": f"BGM「{track['filename']}」已加入该成片（混音在导出侧生效属 Phase 2）",
        "error": None,
    })
    entry_after = manifest["clips"].get(clip_id) or {}
    waveform = clip_audio.peaks_payload(track)
    return {
        "project_id": project_id,
        "clip_id": clip_id,
        "track": track,
        "audio_tracks": tracks,
        "peaks": waveform["peaks"],
        "waveform": waveform,
        "render_revision": int(entry_after.get("render_revision") or 1),
        "export_mix": "phase2",
        "note": "BGM 已保存到该成片；混音在导出侧生效属 Phase 2",
    }


def _delete_clip_bgm(project_id: str, clip_id: str, *, clip_dir: Path,
                     asset_id: str) -> dict[str, Any]:
    """删除 BGM 资产文件并把它移出 ``audio_tracks``（文件已不在 → 幂等，不重复 bump）。"""
    entry = project_entry_for(project_id, clip_id)
    result = clip_audio.delete_bgm(Path(clip_dir), asset_id)
    existing = clip_config.normalize_audio_tracks(entry.get("audio_tracks") or [])
    tracks = [item for item in existing
              if str(item.get("asset_id") or item.get("id")) != str(result["asset_id"])]
    record_changed = len(tracks) != len(existing)
    render_revision = int(entry.get("render_revision") or 1)
    if record_changed:
        project_update_fields(project_id, clip_id, {"audio_tracks": tracks})
        task_id = f"aud_{uuid.uuid4().hex[:12]}"
        stamp = project_store.utc_now()
        manifest = project_bump_render_revision(
            project_id, clip_id, task_id=task_id, kind=TASK_UPDATE_AUDIO,
            instruction=f"删除 BGM {result['asset_id']}")
        project_record_task(project_id, clip_id, {
            "task_id": task_id, "task_type": TASK_UPDATE_AUDIO, "status": "completed",
            "stage": "BGM 已移除（混音导出属 Phase 2）",
            "instruction": f"删除 BGM {result['asset_id']}",
            "created_at": stamp, "updated_at": stamp,
            "summary": f"BGM「{result['asset_id']}」已从该成片移除（混音导出属 Phase 2）",
            "error": None,
        })
        render_revision = int((manifest["clips"].get(clip_id) or {}).get("render_revision")
                              or render_revision)
    return {
        "project_id": project_id,
        "clip_id": clip_id,
        "asset_id": result["asset_id"],
        "removed": bool(result["removed"]),
        "path": result["path"],
        "audio_tracks": tracks,
        "render_revision": render_revision,
        "export_mix": "phase2",
        "note": "BGM 资产已删除；混音导出属 Phase 2",
    }


@router.get("/studio/clips/{project_id}/{clip_id}/config")
async def studio_clip_config(project_id: str, clip_id: str):
    """绑定的完整配置 + 版本号 + 每段来源（``clip_id="source"`` → 源片级 source_config）。"""
    try:
        return await run_in_threadpool(clip_config.config_view, project_id, clip_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc


@router.put("/studio/clips/{project_id}/{clip_id}/config")
async def studio_update_clip_config(project_id: str, clip_id: str, request: Request):
    """部分更新配置（未知段/越界/坏枚举/坏 transform → 422 ``CODE: 可读原因``）。

    返回 ``config / revisions / changed / task``；``render_deferred`` 恒为 true
    —— 配置与版本已保存，按配置真渲染属 Phase 2。**不创建 autoedit_* 任务**。
    """
    payload = await _json_body(request)
    try:
        return await run_in_threadpool(clip_config.update_config, project_id, clip_id, payload)
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc


@router.get("/studio/clips/{project_id}/{clip_id}/transcript/revisions")
async def studio_clip_transcript(project_id: str, clip_id: str, view: str | None = None):
    """三态转写稿 + 修订列表；``?view=raw|corrected|effective`` 附带该视图的段与文本。

    未 seed 时用该成片自带 segments 惰性 seed（新成片第一次打开转写标签即可用）。
    """
    try:
        return await run_in_threadpool(_transcript_view, project_id, clip_id, view)
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc


@router.post("/studio/clips/{project_id}/{clip_id}/transcript/revisions")
async def studio_add_clip_transcript_revision(project_id: str, clip_id: str,
                                              request: Request):
    """新增一条转写修订 ``{author?, reason?, changes[]}``（raw/corrected 基线永不改写）。"""
    payload = await _json_body(request)
    try:
        return await run_in_threadpool(_add_transcript_revision, project_id, clip_id, payload)
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc


@router.post("/studio/clips/{project_id}/{clip_id}/transcript/rollback")
async def studio_rollback_clip_transcript(project_id: str, clip_id: str, request: Request):
    """``{revision}`` 把批准号指回一条**已存在**的修订（只改生效稿，两个基线不动）。"""
    payload = await _json_body(request)
    try:
        return await run_in_threadpool(_rollback_transcript, project_id, clip_id, payload)
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc


@router.get("/studio/clips/{project_id}/{clip_id}/subtitles")
async def studio_clip_subtitles(project_id: str, clip_id: str):
    """该成片的字幕：cues + config + overrides + 版本列表（ASS/SRT 版本可追溯）。"""
    try:
        return await run_in_threadpool(_clip_subtitles_payload, project_id, clip_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc


@router.post("/studio/clips/{project_id}/{clip_id}/subtitles")
async def studio_save_clip_subtitles(project_id: str, clip_id: str, request: Request):
    """保存字幕 config/overrides（客户端给的 cues 会转成 overrides）并生成新版 ASS/SRT。

    只写该 clip 的配置与该 clip 的 ``subtitles/rev-NNN/``；烧录进导出成片属 Phase 2。
    """
    payload = await _json_body(request)
    try:
        return await run_in_threadpool(_save_clip_subtitles, project_id, clip_id, payload)
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc


@router.post("/studio/clips/{project_id}/{clip_id}/audio/bgm")
async def studio_upload_clip_bgm(
    project_id: str,
    clip_id: str,
    uploaded_file: UploadFile = File(..., alias="file"),
    volume: float | None = Form(default=None),
    fade_in_s: float | None = Form(default=None),
    fade_out_s: float | None = Form(default=None),
):
    """multipart 上传 BGM（单文件上限 = ``config.max_video_upload_bytes``）并登记音轨。

    字节按内容寻址落在该 clip 的 ``audio/``；``audio_tracks`` 写回该 clip 条目。
    混音导出属 Phase 2（响应带 ``export_mix`` 标注）。
    """
    _reject_source_binding(clip_id, "BGM 音频轨")
    config = get_config()
    filename = str(uploaded_file.filename or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="缺少文件名（multipart 字段 file）")
    extension = Path(filename).suffix.lower()
    if extension not in clip_audio.SUPPORTED_BGM_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"{clip_audio.E_AUDIO_UNSUPPORTED_FORMAT}: 音频格式 "
                   f"{extension or '（无扩展名）'} 不受支持，仅支持 "
                   f"{'/'.join(clip_audio.SUPPORTED_BGM_EXTENSIONS)}")
    try:
        _entry, clip_dir = await run_in_threadpool(_clip_entry_and_dir, project_id, clip_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc
    staging = Path(config.temp_dir) / "studio-bgm" / f"{uuid.uuid4().hex}{extension}"
    staging.parent.mkdir(parents=True, exist_ok=True)
    try:
        await _write_upload_to_disk(uploaded_file, staging, config.max_video_upload_bytes)
        return await run_in_threadpool(
            _save_clip_bgm, project_id, clip_id,
            clip_dir=clip_dir, filename=filename, source_path=staging,
            volume=volume, fade_in_s=fade_in_s, fade_out_s=fade_out_s,
            max_bytes=config.max_video_upload_bytes)
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc
    finally:
        staging.unlink(missing_ok=True)


@router.delete("/studio/clips/{project_id}/{clip_id}/audio/bgm/{asset_id}")
async def studio_delete_clip_bgm(project_id: str, clip_id: str, asset_id: str):
    """删除该 clip 的 BGM 资产并移出 ``audio_tracks``（文件已不在 → 幂等 removed=false）。"""
    _reject_source_binding(clip_id, "BGM 音频轨")
    try:
        _entry, clip_dir = await run_in_threadpool(_clip_entry_and_dir, project_id, clip_id)
        return await run_in_threadpool(_delete_clip_bgm, project_id, clip_id,
                                       clip_dir=clip_dir, asset_id=asset_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc


@router.get("/studio/clips/{project_id}/{clip_id}/render-config")
async def studio_clip_render_config(project_id: str, clip_id: str):
    """导出摘要：每项生效值 + 来源（user/project/skill_default）；``source`` 哨兵可用。

    ``export.deferred`` 恒为 true —— 外部 ASS 烧录 / BGM 混音 / 滤镜 / 转场 / 非 9:16
    的导出渲染属 Phase 2，这里只如实汇报配置与来源。
    """
    try:
        return await run_in_threadpool(clip_config.render_config_summary, project_id, clip_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise _inspector_error(exc) from exc


# ===== 重新剪辑并替换旧切片：清理重试入口（第五步扩展，2026-10-15） =====
@router.post("/studio/projects/{project_id}/reedit/{run_id}/retry-cleanup")
async def studio_reedit_retry_cleanup(project_id: str, run_id: str):
    """新切片已生效但旧文件清理未完成时，只重试清理（不重新规划、不重新渲染）。"""
    from ... import reedit_replace

    def _delete_artifacts(task_ids: list[str]) -> None:
        import asyncio as _asyncio

        from ...studio_job_store import delete_artifacts_for_jobs

        _asyncio.run(delete_artifacts_for_jobs(task_ids))

    try:
        report = await run_in_threadpool(
            reedit_replace.retry_cleanup, project_id, run_id,
            delete_artifacts_fn=_delete_artifacts)
    except reedit_replace.ReeditError as exc:
        status_code = 409 if exc.code in {"E_REEDIT_NOT_ACTIVATED", "E_REEDIT_PROJECT_MISMATCH"} else 404
        raise HTTPException(status_code=status_code,
                            detail={"code": exc.code, "message": str(exc)}) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail={"code": "E_REEDIT_RETRY_FAILED",
                                                     "message": str(exc)[:200]}) from exc
    return report


@router.get("/studio/projects/{project_id}/transcript-ref")
async def studio_project_transcript_ref(project_id: str):
    """按项目找最新可用的纠错转写凭据（重新剪辑按钮的可用性依据）。"""
    from ... import studio_transcript_store

    ref = await run_in_threadpool(studio_transcript_store.find_latest_by_project, project_id)
    if ref is None:
        # 同源兜底：台账源片（硬链接）按 inode 反查上传 object_key，取该物理源的
        # 最新转写——转写可能登记在别的项目名下，但物理源相同即可复用。
        def _resolve_by_source():
            from ... import studio_project_store

            try:
                media_path = (studio_project_store.load_manifest(project_id).get("source_video") or {}).get("path")
            except Exception:  # noqa: BLE001 — 台账缺失/损坏按无转写处理
                return None
            if not media_path:
                return None
            full = Path(media_path) if Path(media_path).is_absolute() else studio_project_store.TASKS_ROOT / media_path
            object_key = studio_project_store.find_object_key_by_source_path(full)
            return studio_transcript_store.find_latest_by_object_key(object_key) if object_key else None

        ref = await run_in_threadpool(_resolve_by_source)
    if ref is None:
        raise HTTPException(status_code=404, detail={
            "code": "E_TRANSCRIPT_NOT_FOUND",
            "message": "该项目没有已纠错转写，请先完成转写与纠错。"})
    return {**ref, "project_id": project_id}


class TimelineImageRequest(BaseModel):
    """封面图片进时间线（第四步）：``start_s=0`` → role=cover_intro（片头，恒一个）。

    ``start_s>0`` 且带 ``instance_id`` → 原位更新该实例（时间线拖拽移动）。
    """
    duration_s: float
    start_s: float = 0.0
    instance_id: str | None = None


@router.put("/studio/projects/{project_id}/clips/{clip_id}/timeline-image")
async def studio_set_timeline_image(project_id: str, clip_id: str, request: Request,
                                    body: TimelineImageRequest):
    """把当前封面作为 image_clip 放进该成片的时间线（只写台账实例，不动封面文件）。"""
    _assert_project_owner(request, project_id)
    try:
        item = await run_in_threadpool(
            project_store.set_timeline_image_clip,
            project_id, clip_id,
            duration_s=float(body.duration_s), start_s=float(body.start_s),
            instance_id=body.instance_id)
    except ProjectStoreError as exc:
        code = str(getattr(exc, "code", "") or "")
        status = 404 if code in {"E_CLIP_NOT_REGISTERED", "E_TIMELINE_IMAGE_NO_ASSET",
                                 "E_TIMELINE_IMAGE_UNKNOWN"} else 400
        raise HTTPException(status_code=status, detail={"code": code or "E_TIMELINE_IMAGE_FAILED",
                                                        "message": str(exc)}) from exc
    clips = await run_in_threadpool(project_store.get_timeline_image_clips,
                                    project_id, clip_id)
    return {"project_id": project_id, "clip_id": clip_id,
            "image_clip": item, "image_clips": clips}


@router.delete("/studio/projects/{project_id}/clips/{clip_id}/timeline-image")
async def studio_delete_timeline_image(project_id: str, clip_id: str, request: Request,
                                       instance_id: str | None = None):
    """删除时间线上的封面 image_clip 实例（**封面素材文件不受影响**）。

    ``?instance_id=`` 只删那一个；缺省清空该成片全部实例。
    """
    _assert_project_owner(request, project_id)
    try:
        result = await run_in_threadpool(
            project_store.delete_timeline_image_clip, project_id, clip_id,
            instance_id)
    except ProjectStoreError as exc:
        raise HTTPException(status_code=404, detail={"code": str(getattr(exc, "code", "") or "E_CLIP_NOT_REGISTERED"),
                                                     "message": str(exc)}) from exc
    clips = await run_in_threadpool(project_store.get_timeline_image_clips,
                                    project_id, clip_id)
    return {**result, "deleted": True, "image_clips": clips}


@router.get("/studio/tasks/{task_id}/project")
async def studio_task_project(task_id: str):
    """按渲染任务 id 反查所属项目（legacy 会话恢复素材区/按钮可用性）。"""
    from ... import studio_project_store

    project_id = await run_in_threadpool(studio_project_store.find_project_by_task, task_id)
    if project_id is None:
        raise HTTPException(status_code=404, detail={
            "code": "E_PROJECT_NOT_FOUND",
            "message": "没有找到该任务归属的项目。"})
    return {"project_id": project_id}


@router.get("/studio/projects")
async def studio_projects_list():
    """项目列表（updated_at 倒序）：页面加载时自动接管最近项目。"""
    from ... import studio_project_store

    return await run_in_threadpool(studio_project_store.list_projects)


# ---------------------------------------------------------------------------
# 项目管理弹层（2026-10-19）：列表聚合 + 源视频缩略图（均只读）
# ---------------------------------------------------------------------------
# 硬边界：
#   * 不触发上传/ASR/纠错/渲染，不写台账/任务/PG 业务行（缩略图缓存文件除外）；
#   * 可见性与旧端点同一来源（W07 AccessLedger.filter_visible），但本端点路由内自行
#     过滤——旧端点的裸数组形状被 StudioAuthenticationMiddleware 特判处理，不能改；
#   * 项目名称 = project.json 的 source.name（导入时落盘的源片文件名）；没有
#     project.json 的旧台账项目如实返回 name=null，由前端显示 project_id。
# ---------------------------------------------------------------------------

def _project_display_meta(project_id: str) -> dict[str, Any]:
    """项目展示元数据（只读）：源片名 + 工程档案/源片存在性。

    - ``has_project``：timeline 工程（project.json）存在 → 可在工作台打开（打开预检
      就是 GET /studio/timeline/{id}，缺工程必 404）。只有台账没有工程的孤儿项目
      （测试残留 / bridge 路径产生）如实标出，前端置灰不再让用户点了才发现。
    - ``has_source``：源片文件可解析且存在 → 决定缩略图是否可生成（无源片不打 404）。
    """
    project_json = Path(get_config().temp_dir) / "studio-projects" / project_id / "project.json"
    has_project = project_json.is_file()
    name: str | None = None
    if has_project:
        try:
            manifest = json.loads(project_json.read_text(encoding="utf-8"))
            source = manifest.get("source") if isinstance(manifest, dict) else None
            name = str((source or {}).get("name") or "").strip() or None
        except (OSError, ValueError):
            name = None
    # 台账显式命名（重命名接口写入）优先于源片名——改名后列表立即同步。
    try:
        ledger_manifest = project_store.load_manifest(project_id)
        display = str(ledger_manifest.get("display_name") or "").strip()
        if display:
            name = display
    except Exception:  # noqa: BLE001 — 台账读取失败保持回退名
        pass
    try:
        has_source = project_source_path(project_id, Path(get_config().temp_dir)).is_file()
    except FileNotFoundError:
        has_source = False
    return {"name": name, "has_project": has_project, "has_source": has_source}


def _assemble_project_items(
    records: list[dict[str, Any]],
    name_by_id: dict[str, dict[str, Any]],
    status_by_id: dict[str, dict[str, Any]],
    *, q: str, order: str, page: int, page_size: int,
) -> dict[str, Any]:
    """纯函数：搜索（project_id/名称子串，大小写不敏感）→ 排序 → 分页。

    独立成纯函数便于单测（不碰 request/PG）；调用方负责可见性过滤。
    """
    keyword = (q or "").strip().casefold()
    items: list[dict[str, Any]] = []
    for record in records:
        pid = str(record.get("project_id") or "")
        meta = name_by_id.get(pid) or {}
        name = meta.get("name")
        job = status_by_id.get(pid) or {}
        if keyword and keyword not in pid.casefold() and keyword not in str(name or "").casefold():
            continue
        items.append({
            "project_id": pid,
            "name": name,
            "card_count": int(record.get("card_count") or 0),
            "updated_at": str(record.get("updated_at") or ""),
            "status": job.get("status"),
            "stage": job.get("stage"),
            "lifecycle": str(record.get("status") or "active"),
            "openable": bool(meta.get("has_project")),
            "has_source": bool(meta.get("has_source")),
        })
    if order == "updated_asc":
        items.sort(key=lambda item: item["updated_at"])
    elif order == "name":
        items.sort(key=lambda item: str(item["name"] or item["project_id"]).casefold())
    elif order == "cards":
        items.sort(key=lambda item: (-item["card_count"], item["updated_at"]), reverse=False)
    else:  # updated_desc（默认，list_projects 已按此排序，这里保持显式）
        items.sort(key=lambda item: item["updated_at"], reverse=True)
    total = len(items)
    safe_page = max(1, int(page))
    safe_size = min(50, max(1, int(page_size)))
    start = (safe_page - 1) * safe_size
    return {"items": items[start:start + safe_size], "total": total,
            "page": safe_page, "page_size": safe_size}


@router.get("/studio/projects/list")
async def studio_projects_list_page(
    request: Request,
    q: str = "",
    page: int = 1,
    page_size: int = 12,
    order: str = "updated_desc",
    include_archived: bool = False,
    session: AsyncSession = Depends(get_db),
):
    """项目管理弹层数据源：当前主体可见项目 + 名称/状态/成片数，支持搜索/排序/分页。

    状态 = PG ``studio_jobs`` 每项目最新一条（一次 DISTINCT ON 查询，不在循环里查库）；
    无任务的项目 status/stage 为 null（前端显示「未开始」）。
    """
    records = await run_in_threadpool(project_store.list_projects, include_archived)
    visible = access_ledger(request).filter_visible(current_principal(request), "project", records)
    name_by_id: dict[str, dict[str, Any]] = {}
    for record in visible:
        pid = str(record["project_id"])
        name_by_id[pid] = await run_in_threadpool(_project_display_meta, pid)
    status_by_id: dict[str, dict[str, Any]] = {}
    if visible:
        ids = [str(record["project_id"]) for record in visible]
        rows = (await session.execute(
            text("SELECT DISTINCT ON (project_id) project_id, status, stage "
                 "FROM studio_jobs WHERE project_id = ANY(:ids) "
                 "ORDER BY project_id, updated_at DESC"),
            {"ids": ids},
        )).mappings()
        status_by_id = {str(row["project_id"]): {"status": row["status"], "stage": row["stage"]}
                        for row in rows}
    return _assemble_project_items(visible, name_by_id, status_by_id,
                                   q=q, order=order, page=page, page_size=page_size)


def _extract_project_thumbnail(source: Path, target: Path) -> bool:
    """ffmpeg 从源片 1s 处抽一帧 → 360px 宽 JPG；先写临时文件再原子替换。

    临时文件必须保留 ``.jpg`` 扩展名（ffmpeg 按输出扩展名推断封装格式，
    ``.jpg.tmp`` 会直接报「Unable to find a suitable output format」）。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.stem + ".tmp.jpg")
    command = [
        resolve_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", "1", "-i", str(source), "-frames:v", "1",
        "-vf", "scale=360:-2", "-q:v", "4", str(tmp),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        tmp.unlink(missing_ok=True)
        return False
    if completed.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return False
    os.replace(tmp, target)
    return True


@router.get("/studio/projects/{project_id}/thumbnail.jpg")
async def studio_project_thumbnail(project_id: str):
    """项目源视频缩略图（只读）：源片抽帧 → 磁盘缓存；无源片/抽帧失败 → 404。

    缓存失效口径 = 源片 mtime 比缓存新（重新导入同一项目即刷新）。
    ACL 由路由级 enforce_studio_access 按 path ``project_id`` 执行（GET=read）。
    """
    config = get_config()
    cache = Path(config.temp_dir) / "studio-thumbnails" / f"{project_id}.jpg"
    try:
        source = await run_in_threadpool(project_source_path, project_id, Path(config.temp_dir))
        stale = not cache.is_file() or cache.stat().st_mtime < source.stat().st_mtime
        if stale:
            extracted = await run_in_threadpool(_extract_project_thumbnail, source, cache)
        else:
            extracted = True
        if extracted and cache.is_file():
            return FileResponse(cache, media_type="image/jpeg",
                                headers={"Cache-Control": "private, max-age=300"})
    except FileNotFoundError:
        pass
    except Exception:
        logger.exception("项目缩略图生成失败 %s", project_id)
    raise HTTPException(status_code=404, detail="E_PROJECT_THUMBNAIL_UNAVAILABLE")


def _extract_clip_first_frame(video: Path, target: Path, *, start_seconds: float = 0.0) -> bool:
    """ffmpeg 抽成片帧 → 360px 宽 JPG；先写临时文件再原子替换。

    与项目缩略图不同（那个取 1s 处），素材卡缩略图默认取切片视频的**真实第一帧**，
    因此 ``start_seconds=0`` 时不加 ``-ss``。2026-10-21 补充：应用封面后成片开头是
    0.8s 的封面片头（台账 ``cover_intro_s``），此时调用方传 ``start_seconds=0.8``，
    让缩略图跳过封面片头、仍然展示**正片内容**的第一帧——卡片不能变成一张黑封面。
    临时文件保留 ``.jpg`` 扩展名（ffmpeg 按输出扩展名推断封装）。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.stem + ".tmp.jpg")
    command = [
        resolve_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
    ]
    if start_seconds and start_seconds > 0:
        command += ["-ss", f"{float(start_seconds):.3f}"]
    command += [
        "-i", str(video), "-frames:v", "1",
        "-vf", "scale=360:-2", "-q:v", "4", str(tmp),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        tmp.unlink(missing_ok=True)
        return False
    if completed.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return False
    os.replace(tmp, target)
    return True


@router.get("/studio/projects/{project_id}/clips/{clip_id}/first-frame.jpg")
async def studio_clip_first_frame(project_id: str, clip_id: str, request: Request):
    """切片视频真实第一帧（素材卡缩略图，2026-10-21 阶段；只读）。

    与生成封面无关：直接从台账定位的 ``clip_final.mp4`` 抽第 0 帧，磁盘缓存，
    缓存失效口径 = 成片文件 mtime 比缓存新（重渲/重剪替换文件后自动刷新）。
    owner-only；不触发 ASR/分析/渲染，不修改成片文件。视频缺失/抽帧失败 → 404
    （前端回退占位图标，不用其它切片或封面冒充）。
    """
    _assert_project_owner(request, project_id)
    try:
        entry = await run_in_threadpool(project_entry_for, project_id, clip_id)
    except ProjectStoreError as exc:
        raise _project_error(exc) from exc
    # 应用封面后成片开头是封面片头（台账 cover_intro_s）：缩略图跳过片头，
    # 仍然展示正片内容的第一帧，否则素材卡会整卡变黑。
    try:
        intro_s = max(0.0, float(entry.get("cover_intro_s") or 0.0))
    except (TypeError, ValueError):
        intro_s = 0.0
    video_rel = project_store._resolve_clip_video_path(
        str(entry.get("render_task_id") or ""), clip_id)
    video = (project_store.TASKS_ROOT / video_rel) if video_rel else None
    if not video or not video.is_file():
        raise HTTPException(status_code=404, detail={
            "code": "E_CLIP_FIRST_FRAME_UNAVAILABLE",
            "message": "该切片没有可读取的成片视频文件，无法提取第一帧"})
    config = get_config()
    # 缓存文件名带片头长度：应用封面后 cover_intro_s 从 0 变 0.8 时必须换新缓存，
    # 否则旧的「第 0 帧纯黑缩略图」会因成片 mtime 未变而一直被判为新鲜（用户实测复现）。
    cache = (Path(config.temp_dir) / "studio-clip-frames"
             / f"{project_id}.{clip_id}.i{int(round(intro_s * 1000))}.jpg")
    try:
        stale = not cache.is_file() or cache.stat().st_mtime < video.stat().st_mtime
        if stale:
            extracted = await run_in_threadpool(
                _extract_clip_first_frame, video, cache, start_seconds=intro_s)
        else:
            extracted = True
        if extracted and cache.is_file():
            return FileResponse(cache, media_type="image/jpeg",
                                headers={"Cache-Control": "private, max-age=300"})
    except Exception:  # noqa: BLE001 — 抽帧链路任何失败都按 404 降级，不冒充
        logger.exception("切片第一帧提取失败 %s/%s", project_id, clip_id)
    raise HTTPException(status_code=404, detail={
        "code": "E_CLIP_FIRST_FRAME_UNAVAILABLE",
        "message": "第一帧提取失败（视频不可读或编码异常）"})


# ---------------------------------------------------------------------------
# 项目生命周期（2026-10-20）：归档 / 恢复 / 删除预览 / 删除 / 进度 / 重试
# ---------------------------------------------------------------------------
# 全部执行 owner/ACL 校验：路径 project_id 由 enforce_studio_access 做读写 ACL，
# 归档/恢复/删除/重试额外要求 objects.owner == 认证主体（仅所有者）。

def _lifecycle_guard(project_id: str) -> None:
    """删除期不可变守卫：deleting/delete_failed/deleted 拒绝上传与任务提交。"""
    lifecycle.assert_mutable(project_id)


@router.post("/studio/projects/{project_id}/archive")
async def studio_project_archive(project_id: str, request: Request):
    """归档：仅移出默认列表，文件与容量不变，可恢复（仅所有者）。"""
    _assert_project_owner(request, project_id)
    try:
        return await run_in_threadpool(lifecycle.archive_project, project_id)
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_http_error(exc) from exc


@router.post("/studio/projects/{project_id}/restore")
async def studio_project_restore(project_id: str, request: Request):
    """恢复已归档项目到默认列表（仅所有者）。"""
    _assert_project_owner(request, project_id)
    try:
        return await run_in_threadpool(lifecycle.restore_project, project_id)
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_http_error(exc) from exc


@router.get("/studio/projects/{project_id}/delete/preview")
async def studio_project_delete_preview(project_id: str, request: Request):
    """删除预览（只读）：逐类列出将删除的文件、字节数与共享资产。"""
    _assert_project_owner(request, project_id)
    try:
        return await run_in_threadpool(lifecycle.build_delete_preview, project_id)
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_http_error(exc) from exc


class ProjectDeleteRequest(BaseModel):
    confirmed: bool = False


@router.post("/studio/projects/{project_id}/delete")
async def studio_project_delete(project_id: str, request: Request,
                                body: ProjectDeleteRequest,
                                session: AsyncSession = Depends(get_db)):
    """确认删除：先持久化 deletion_manifest，再启动异步幂等清理（仅所有者）。

    清理期间该项目拒绝上传/自动剪辑/局部任务/重剪；进度经
    ``GET /studio/projects/{id}/delete/status`` 查询；失败进 delete_failed，
    仅可重试清理。本端点自身不删任何文件。
    """
    if not body.confirmed:
        raise HTTPException(status_code=422, detail={
            "code": "E_DELETE_CONFIRM_REQUIRED",
            "message": "缺少 confirmed=true：删除需用户显式确认"})
    _assert_project_owner(request, project_id)
    try:
        manifest = await run_in_threadpool(
            lifecycle.confirm_deletion, project_id, confirmed_by=_delete_confirmed_by(request))
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_http_error(exc) from exc
    threading.Thread(target=lifecycle.run_deletion_task, args=(project_id,),
                     name=f"project-delete-{project_id[:16]}", daemon=True).start()
    return {"project_id": project_id, "status": "deleting",
            "reclaim_bytes": manifest.get("reclaim_bytes"),
            "items": len(manifest.get("items") or [])}


@router.get("/studio/projects/{project_id}/name")
async def studio_project_get_name(project_id: str):
    """项目名称回显（只读）：display_name → 源片名回退；含乐观并发所需的 updated_at。"""
    config = get_config()
    try:
        meta = await run_in_threadpool(
            project_store.get_project_name_meta, project_id,
            uploads_root=Path(config.temp_dir))
    except ProjectStoreError as exc:
        raise _project_error(exc) from exc
    fallback_name = meta.pop("fallback_name", None)
    return {**meta, "fallback_name": fallback_name}


class ProjectRenameRequest(BaseModel):
    display_name: str
    expected_updated_at: str | None = None


@router.patch("/studio/projects/{project_id}")
async def studio_project_rename(project_id: str, request: Request,
                                body: ProjectRenameRequest):
    """项目重命名（2026-10-20 内联编辑）：仅改台账 display_name。

    owner-only；deleting/deleted 拒绝；archived 允许；expected_updated_at 乐观并发
    （多标签页防旧覆盖，冲突 409）。不改目录/源片路径/clip_id/转写引用/任务历史。
    """
    _assert_project_owner(request, project_id)
    try:
        name = project_store.validate_project_name(body.display_name)
    except ProjectStoreError as exc:
        raise HTTPException(status_code=422, detail={
            "code": "E_PROJECT_NAME_INVALID", "message": str(exc)}) from exc
    try:
        saved = await run_in_threadpool(
            project_store.rename_project, project_id, name,
            expected_updated_at=body.expected_updated_at)
    except ProjectStoreError as exc:
        detail = exc.as_dict() if hasattr(exc, "as_dict") else {"message": str(exc)}
        code = str(detail.get("code") or "")
        status = 409 if code in {"E_PROJECT_RENAME_CONFLICT", "E_PROJECT_NOT_MUTABLE"} else 404
        raise HTTPException(status_code=status, detail=detail) from exc
    return {"project_id": project_id, "display_name": name,
            "updated_at": saved.get("updated_at")}


class ProjectActiveClipRequest(BaseModel):
    """切换项目唯一 active 切片：``clip_id=None`` 表示清除选中（回到原片视图）。"""

    clip_id: str | None = None


@router.put("/studio/projects/{project_id}/active-clip")
async def studio_project_set_active_clip(project_id: str, request: Request,
                                         body: ProjectActiveClipRequest):
    """切片选择（2026-10-21 阶段）：把点击的素材卡持久化为项目唯一 active_clip_id。

    owner-only；刷新后经 ``GET /studio/projects/{id}/clips`` 的 ``active_clip_id``
    恢复上次选中。只写台账一个指针字段：不改成片文件、不动封面/转写/revision、
    绝不触发 ASR/分析/渲染。clip 未登记 → 404（E_CLIP_NOT_REGISTERED）；
    删除期项目 → 409（E_PROJECT_NOT_MUTABLE）；幂等（同值不递增 generation）。
    """
    _assert_project_owner(request, project_id)
    try:
        return await run_in_threadpool(project_store.set_active_clip, project_id, body.clip_id)
    except ProjectStoreError as exc:
        # ProjectStoreError 没有 as_dict：必须显式带上 code，否则下面的
        # E_CLIP_NOT_REGISTERED→404 / E_PROJECT_NOT_MUTABLE→409 映射永远走不到（全变 422）。
        detail = {"code": str(getattr(exc, "code", "") or ""), "message": str(exc)}
        code = str(detail.get("code") or "")
        status = 409 if code == "E_PROJECT_NOT_MUTABLE" else (
            404 if code in {"E_CLIP_NOT_REGISTERED", "E_PROJECT_NOT_FOUND"} else 422)
        raise HTTPException(status_code=status, detail=detail) from exc


# ---------------------------------------------------------------------------
# 封面版型库（2026-10-21 阶段）：版本化注册表 + 模拟生成 stub。
# 本阶段不调用模型、不生成真实封面：mock-result 返回确定性模拟载荷；
# 台账只写每条成片各自的 cover_template 三元组（真实生成是后续阶段）。
# ---------------------------------------------------------------------------

@router.get("/studio/cover-templates")
async def studio_cover_templates():
    """封面版型库（只读）：版本化注册表透出，前端版型卡的唯一事实源。"""
    return await run_in_threadpool(cover_templates.list_templates)


@router.get("/studio/cover-templates/{template_id}/example.svg")
async def studio_cover_template_example(template_id: str):
    """版型示例图（只读，SVG）：版面示意，非生成封面。

    文件在 ``backend/assets/cover-templates/{template_id}.svg``；未知版型或文件
    缺失 → 404，绝不用其它版型的示例顶替。
    """
    from fastapi.responses import FileResponse
    try:
        spec = cover_templates.template_render_spec(template_id)
    except cover_templates.CoverTemplateError as exc:
        raise HTTPException(status_code=404,
                            detail={"code": exc.code, "message": exc.message}) from exc
    assets = Path(__file__).resolve().parents[3] / "assets" / "cover-templates"
    example = assets / f"{spec['template_id']}.svg"
    if not example.is_file():
        raise HTTPException(status_code=404,
                            detail={"code": "E_TEMPLATE_EXAMPLE_MISSING",
                                    "message": f"版型示例图缺失：{example.name}"})
    return FileResponse(example, media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})


class CoverTemplateSelectRequest(BaseModel):
    """选择/清除一条成片的封面版型：``template_id=None`` 表示清除。"""

    template_id: str | None = None


@router.put("/studio/projects/{project_id}/clips/{clip_id}/cover-template")
async def studio_clip_set_cover_template(project_id: str, clip_id: str, request: Request,
                                         body: CoverTemplateSelectRequest):
    """选择/清除**单条成片**的封面版型（owner-only，写入台账 cover_template 三元组）。

    每条成片各自持有自己的选择（不串片）；未注册版型 → 404 E_TEMPLATE_UNKNOWN；
    删除期项目 → 409 E_PROJECT_NOT_MUTABLE；幂等。不触发任何渲染。
    """
    _assert_project_owner(request, project_id)
    try:
        return await run_in_threadpool(
            project_store.set_cover_template, project_id, clip_id, body.template_id)
    except cover_templates.CoverTemplateError as exc:
        raise HTTPException(status_code=404 if exc.code == "E_TEMPLATE_UNKNOWN" else 422,
                            detail={"code": exc.code, "message": exc.message}) from exc
    except ProjectStoreError as exc:
        detail = {"code": str(getattr(exc, "code", "") or ""), "message": str(exc)}
        code = str(detail.get("code") or "")
        status = 409 if code == "E_PROJECT_NOT_MUTABLE" else (
            404 if code in {"E_CLIP_NOT_REGISTERED", "E_PROJECT_NOT_FOUND"} else 422)
        raise HTTPException(status_code=status, detail=detail) from exc


@router.post("/studio/cover-templates/{template_id}/mock-result")
async def studio_cover_template_mock_result(template_id: str):
    """模拟生成结果（2026-10-21 阶段 stub）：确定性载荷，不调用模型、不写封面文件。

    前端「使用××版制作封面」按钮先接这里；后续真实渲染接入时只替换
    cover_templates.mock_result 的内部实现，路由与载荷形状不变。
    """
    try:
        return await run_in_threadpool(cover_templates.mock_result, template_id)
    except cover_templates.CoverTemplateError as exc:
        raise HTTPException(status_code=404 if exc.code == "E_TEMPLATE_UNKNOWN" else 422,
                            detail={"code": exc.code, "message": exc.message}) from exc


@router.get("/studio/projects/{project_id}/cover-assets")
async def studio_project_cover_assets(project_id: str, request: Request):
    """独立封面素材清单（右侧「封面」板块唯一事实源，2026-10-20）。

    owner-only；只读：从当前台账构建（旧批次封面结构上不可能出现），状态按磁盘
    实测（ready/failed+原因），不触发任何 ASR/分析/渲染，不动 clip_final.mp4。
    """
    _assert_project_owner(request, project_id)
    try:
        return await run_in_threadpool(
            project_store.list_cover_assets, project_id)
    except ProjectStoreError as exc:
        raise _project_error(exc) from exc


@router.get("/studio/projects/{project_id}/delete/status")
async def studio_project_delete_status(project_id: str, request: Request):
    """删除进度（只读）：清单逐项状态 + 项目生命周期状态。"""
    _assert_project_owner(request, project_id)
    return await run_in_threadpool(lifecycle.read_deletion_status, project_id)


@router.post("/studio/projects/{project_id}/delete/retry")
async def studio_project_delete_retry(project_id: str, request: Request):
    """delete_failed 后只重试清理（不重新分析/规划/渲染；仅所有者）。"""
    _assert_project_owner(request, project_id)
    try:
        manifest = await run_in_threadpool(lifecycle.retry_deletion, project_id)
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_http_error(exc) from exc
    threading.Thread(target=lifecycle.run_deletion_task, args=(project_id,),
                     name=f"project-delete-retry-{project_id[:16]}", daemon=True).start()
    return {"project_id": project_id, "status": "deleting",
            "manifest_status": manifest.get("status")}


@router.get("/studio/projects/{project_id}/exports")
async def studio_project_exports(project_id: str, session: AsyncSession = Depends(get_db)):
    """工程缺失项目的成果清单（只读）：每张卡的成片 MP4 / SRT 下载定位 + 源区间。

    服务「工程缺失但成片存在」的三态之二：可查看、下载成果，但**不冒充可编辑
    工程**（没有 project.json/EDL，就没有可恢复的编辑状态）。下载/预览复用既有
    ``GET /api/jobs/{job}/artifacts/{id}/file``（其自身按 job ACL 校验）；本端点
    只读台账（list_cards，不触发 sync）并定位 artifact id，不写任何数据。
    """
    cards = await run_in_threadpool(project_store.list_cards, project_id)
    items: list[dict[str, Any]] = []
    for card in cards:
        job_id = str(card.get("render_task_id") or "")
        entry: dict[str, Any] = {
            "clip_id": card.get("clip_id"),
            "name": card.get("name") or card.get("title") or card.get("clip_id"),
            "duration": card.get("duration"),
            "source_start": card.get("source_start"),
            "source_end": card.get("source_end"),
            "cover_path": (card.get("cover") or {}).get("path"),
            "mp4_url": None,
            "srt_url": None,
        }
        if job_id and entry["clip_id"]:
            rows = (await session.execute(
                text("SELECT id, kind FROM studio_artifacts "
                     "WHERE job_id = :job AND clip_id = :cid AND kind IN ('clip_final','srt')"),
                {"job": job_id, "cid": str(entry["clip_id"])},
            )).mappings()
            for row in rows:
                url = f"/api/jobs/{job_id}/artifacts/{row['id']}/file"
                if row["kind"] == "clip_final":
                    entry["mp4_url"] = url
                elif row["kind"] == "srt":
                    entry["srt_url"] = url
        items.append(entry)
    return {"project_id": project_id, "items": items}
