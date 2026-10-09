"""
Media API routes (fonts, transitions, uploads).
"""

from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pathlib import Path
from typing import Any, cast
import logging
import uuid
import json
import aiofiles

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
    read_auto_edit_task,
    rerender_auto_edit_task,
    resolve_auto_edit_artifact,
    submit_auto_edit_task,
)
from ...timeline_copilot import (
    create_timeline_project,
    load_timeline_project,
    update_timeline_project,
)
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

logger = logging.getLogger(__name__)
router = APIRouter(tags=["media"])
MAX_FONT_UPLOAD_BYTES = 10 * 1024 * 1024


async def _get_authenticated_user_id(request: Request, db: AsyncSession) -> str:
    config = get_config()
    return await resolve_authenticated_user_id(request, db, config)


async def _write_upload_to_disk(
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
        uploads_dir.mkdir(parents=True, exist_ok=True)

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
    uploads_dir.mkdir(parents=True, exist_ok=True)
    video_path = uploads_dir / f"{uuid.uuid4()}{extension}"

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
        return await run_in_threadpool(
            submit_auto_edit_task,
            project_id=project_id,
            uploads_root=Path(config.temp_dir),
            asr_segments=payload.get("asr_segments"),
            golden_sentences=payload.get("golden_sentences"),
            keywords=payload.get("keywords"),
        )
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
async def studio_rerender_auto_edit(task_id: str):
    """Queue a non-destructive re-render with the current local rule set."""
    try:
        return await run_in_threadpool(rerender_auto_edit_task, task_id)
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


@router.get("/studio/timeline/{project_id}")
async def studio_timeline(project_id: str):
    """Return one local Studio EDL plus its transcript and export inventory."""
    try:
        config = get_config()
        return await run_in_threadpool(
            load_timeline_project,
            project_id=project_id,
            uploads_root=Path(config.temp_dir),
        )
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
