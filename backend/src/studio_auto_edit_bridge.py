"""Local file-queue bridge from Studio to the medical production executor.

The Studio backend and render worker both run locally but independently. A
bind-mounted ``tasks`` folder is their contract: Studio writes an immutable
JSON task plus a copy of the source file, and the production worker owns all
rendering work and status updates.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


TASKS_ROOT = Path(os.getenv("STUDIO_TASKS_ROOT", "/app/tasks"))
TASK_ID_PATTERN = re.compile(r"^autoedit_[a-f0-9]{12}$")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _task_paths(task_id: str) -> dict[str, Path]:
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError("Invalid auto-edit task id")
    root = TASKS_ROOT.resolve()
    return {
        "root": root,
        "incoming": root / "in" / f"{task_id}.json",
        "processing": root / "processing" / f"{task_id}.json",
        "completed": root / "completed" / f"{task_id}.json",
        "failed": root / "failed" / f"{task_id}.json",
        "status": root / "status" / f"{task_id}.json",
        "media": root / "media" / task_id,
        "output": root / "output" / task_id,
    }


def _project_dir(project_id: str, uploads_root: Path) -> Path:
    if not project_id.startswith("timeline_"):
        raise FileNotFoundError("Timeline project not found")
    root = (uploads_root / "studio-projects").resolve()
    project = (root / project_id).resolve()
    if root not in project.parents or not (project / "project.json").is_file():
        raise FileNotFoundError("Timeline project not found")
    return project


def _source_for_project(project_dir: Path) -> Path:
    raw_dir = project_dir / "raw"
    candidates = [path for path in raw_dir.iterdir() if path.is_file()]
    if len(candidates) != 1:
        raise FileNotFoundError("Timeline project source video not found")
    return candidates[0]


def _normalise_segments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("asr_segments must be a list")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        try:
            start, end = round(float(item.get("start", 0)), 3), round(float(item.get("end", 0)), 3)
        except (TypeError, ValueError):
            continue
        if text and end > start >= 0:
            result.append({
                "start": start,
                "end": end,
                "text": text,
                "speaker": str(item.get("speaker") or "未标注"),
            })
    if not result:
        raise ValueError("No usable timestamped transcript segments")
    return result


def submit_auto_edit_task(
    *,
    project_id: str,
    uploads_root: Path,
    asr_segments: Any,
    golden_sentences: Any = None,
    keywords: Any = None,
) -> dict[str, Any]:
    """Copy the durable Studio source to the shared queue and enqueue a task.

    The relative paths in JSON are intentional.  The exact same task works on
    Windows and in Docker; each side resolves it against its own shared task
    root rather than leaking a container-only path to the local executor.
    """
    project = _project_dir(project_id, uploads_root)
    source = _source_for_project(project)
    segments = _normalise_segments(asr_segments)
    task_id = f"autoedit_{uuid.uuid4().hex[:12]}"
    paths = _task_paths(task_id)

    for directory in (paths["incoming"].parent, paths["processing"].parent, paths["completed"].parent,
                      paths["failed"].parent, paths["status"].parent, paths["media"], paths["output"]):
        directory.mkdir(parents=True, exist_ok=True)

    destination = paths["media"] / source.name
    shutil.copy2(source, destination)
    relative_source = destination.relative_to(paths["root"]).as_posix()
    relative_output = paths["output"].relative_to(paths["root"]).as_posix()
    goldens = golden_sentences if isinstance(golden_sentences, list) else []
    keyword_list = [str(keyword).strip() for keyword in (keywords or []) if str(keyword).strip()]
    task = {
        "task_id": task_id,
        "created_at": _utc_now(),
        "raw_video_path": relative_source,
        "asr_segments": segments,
        "golden_sentences": goldens,
        "keywords": keyword_list,
        "output_dir": relative_output,
        "create_cover": True,
        "add_subtitle": True,
        "bridge": {
            "engine": "medical-video-production",
            "project_id": project_id,
            "mode": "vertical-highlight",
            "caption_style": "bold_pop",
            "ratio": "9:16",
            "note": "The local runner reuses Studio's corrected transcript and resolves paths relative to the shared tasks folder.",
        },
    }
    _atomic_json(paths["status"], {
        "task_id": task_id,
        "status": "queued",
        "stage": "任务已写入本机队列，等待固化剪辑流程执行器",
        "progress": 0.0,
        "created_at": task["created_at"],
        "updated_at": task["created_at"],
        "artifacts": [],
    })
    _atomic_json(paths["incoming"], task)
    return read_auto_edit_task(task_id)


def rerender_auto_edit_task(task_id: str) -> dict[str, Any]:
    """Create a fresh task using newer rules while preserving old exports."""
    original_paths = _task_paths(task_id)
    task_path = next((candidate for candidate in (
        original_paths["completed"], original_paths["failed"], original_paths["processing"], original_paths["incoming"],
    ) if candidate.is_file()), None)
    if task_path is None:
        raise FileNotFoundError("Auto-edit task not found")
    original = json.loads(task_path.read_text(encoding="utf-8"))
    source = (original_paths["root"] / str(original.get("raw_video_path") or "")).resolve()
    if original_paths["root"] not in source.parents or not source.is_file():
        raise FileNotFoundError("Original task source video is missing")

    new_id = f"autoedit_{uuid.uuid4().hex[:12]}"
    paths = _task_paths(new_id)
    for directory in (paths["incoming"].parent, paths["processing"].parent, paths["completed"].parent,
                      paths["failed"].parent, paths["status"].parent, paths["media"], paths["output"]):
        directory.mkdir(parents=True, exist_ok=True)
    destination = paths["media"] / source.name
    shutil.copy2(source, destination)
    cloned = dict(original)
    cloned["task_id"] = new_id
    cloned["created_at"] = _utc_now()
    cloned["raw_video_path"] = destination.relative_to(paths["root"]).as_posix()
    cloned["output_dir"] = paths["output"].relative_to(paths["root"]).as_posix()
    bridge = dict(cloned.get("bridge") or {})
    bridge["rerender_of"] = task_id
    bridge["note"] = "Fresh local re-render using the latest fixed production rules; previous artifacts are preserved."
    cloned["bridge"] = bridge
    _atomic_json(paths["status"], {
        "task_id": new_id, "status": "queued",
        "stage": "已按最新固化规则创建复渲染任务，旧成片保持不变", "progress": 0.0,
        "created_at": cloned["created_at"], "updated_at": cloned["created_at"], "artifacts": [],
    })
    _atomic_json(paths["incoming"], cloned)
    return read_auto_edit_task(new_id)


def read_auto_edit_task(task_id: str) -> dict[str, Any]:
    paths = _task_paths(task_id)
    status_path = paths["status"]
    if not status_path.is_file():
        raise FileNotFoundError("Auto-edit task not found")
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    artifacts: list[dict[str, Any]] = []
    if paths["output"].exists():
        for path in sorted(paths["output"].rglob("*")):
            if path.is_file():
                artifacts.append({
                    "name": path.name,
                    "path": path.relative_to(paths["root"]).as_posix(),
                    "size_bytes": path.stat().st_size,
                })
    payload["artifacts"] = artifacts
    # A rendered clip is more than a downloadable file: the Studio needs its
    # own, clip-relative timeline when the user selects it in the media bin.
    # Keep that presentation data derived from the immutable task + plan so it
    # cannot drift from the FFmpeg input range.
    payload["clips"] = _read_rendered_clips(paths)
    return payload


def _read_rendered_clips(paths: dict[str, Path]) -> list[dict[str, Any]]:
    plan_path = paths["output"] / "clip_plan.json"
    if not plan_path.is_file():
        return []

    try:
        plan_document = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    task: dict[str, Any] = {}
    for candidate in (paths["completed"], paths["processing"], paths["incoming"], paths["failed"]):
        if candidate.is_file():
            try:
                task = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                task = {}
            break
    source_segments = task.get("asr_segments") if isinstance(task.get("asr_segments"), list) else []
    clips: list[dict[str, Any]] = []
    for index, item in enumerate(plan_document.get("highlights", []), 1):
        if not isinstance(item, dict):
            continue
        try:
            source_start = float(item["start"])
            source_end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        clip_id = str(item.get("clip_id") or f"clip_{index:02}")
        clip_dir = paths["output"] / clip_id
        video = clip_dir / "clip_final.mp4"
        cover = clip_dir / "cover.jpg"
        if not video.is_file():
            continue
        # The renderer prepends a 0.8s silent cover intro.  Translate every
        # source transcript segment into this exact final-video timebase.
        clip_segments: list[dict[str, Any]] = []
        for segment in source_segments:
            if not isinstance(segment, dict):
                continue
            try:
                start, end = float(segment.get("start", 0)), float(segment.get("end", 0))
            except (TypeError, ValueError):
                continue
            text = str(segment.get("text") or "").strip()
            if not text or end <= source_start or start >= source_end:
                continue
            clip_segments.append({
                "start": round(max(start, source_start) - source_start + 0.8, 3),
                "end": round(min(end, source_end) - source_start + 0.8, 3),
                "text": text,
                "speaker": str(segment.get("speaker") or "未标注"),
            })
        clips.append({
            "task_id": paths["status"].stem,
            "clip_id": clip_id,
            "name": f"剪辑成片 {index:02}",
            "topic": str(item.get("topic") or item.get("cover_title") or "自动剪辑成片"),
            "source_start": round(source_start, 3),
            "source_end": round(source_end, 3),
            "duration": round(source_end - source_start + 0.8, 3),
            "video_path": video.relative_to(paths["root"]).as_posix(),
            "cover_path": cover.relative_to(paths["root"]).as_posix() if cover.is_file() else None,
            "segments": clip_segments,
        })
    return clips


def resolve_auto_edit_artifact(task_id: str, artifact_path: str) -> Path:
    """Return one exported artifact without allowing traversal outside its task."""
    if not artifact_path or Path(artifact_path).is_absolute():
        raise FileNotFoundError("Artifact not found")
    paths = _task_paths(task_id)
    output_root = paths["output"].resolve()
    relative = Path(artifact_path)
    if relative.parts[:2] == ("output", task_id):
        relative = Path(*relative.parts[2:])
    artifact = (output_root / relative).resolve()
    if output_root not in artifact.parents or not artifact.is_file():
        raise FileNotFoundError("Artifact not found")
    return artifact
