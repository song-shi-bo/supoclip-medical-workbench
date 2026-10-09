"""Adapter between the local Studio API and video-timeline-copilot's EDL helpers.

The browser never talks to a video editor directly.  It reads and updates the
same JSON EDL that produces the portable SRT and FCPXML deliverables.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Any

from helpers.export_fcpxml import write_fcpxml
from helpers.export_srt import build_srt_for_timeline
from helpers.inventory import ffprobe
from helpers.validate_edl import validate


MIN_RANGE_SECONDS = 0.8


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return cleaned or "studio_timeline"


def _frame_rate(value: Any) -> float:
    try:
        rate = float(Fraction(str(value)))
        return rate if rate > 0 else 30.0
    except (ValueError, ZeroDivisionError):
        return 30.0


def _link_original(source: Path, destination: Path) -> None:
    """Create a project-local original-media reference without transcoding it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        # This fallback only occurs when the Docker volume cannot hard-link.
        # The editing source remains the original user upload, never a preview.
        shutil.copy2(source, destination)


def _transcript_payload(
    raw_segments: list[dict[str, Any]], corrected_segments: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    corrected_by_index = {index: item for index, item in enumerate(corrected_segments)}
    normalized_segments: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []

    for index, raw in enumerate(raw_segments):
        corrected = corrected_by_index.get(index, raw)
        item_words = [
            {
                "text": str(word.get("word", word.get("text", ""))).strip(),
                "start": round(float(word.get("start", 0)), 3),
                "end": round(float(word.get("end", 0)), 3),
            }
            for word in raw.get("words") or []
            if str(word.get("word", word.get("text", ""))).strip()
        ]
        normalized = {
            "start": round(float(raw.get("start", 0)), 3),
            "end": round(float(raw.get("end", 0)), 3),
            "text": str(corrected.get("text", raw.get("text", ""))).strip(),
            "speaker": str(corrected.get("speaker", "未标注")),
            "words": item_words,
        }
        if normalized["end"] > normalized["start"] and normalized["text"]:
            normalized_segments.append(normalized)
            words.extend(item_words)

    return {"segments": normalized_segments, "words": words}


def _project_payload(project_dir: Path) -> dict[str, Any]:
    manifest = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    edl_path = project_dir / "edit" / "edl.json"
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    transcript = json.loads((project_dir / "edit" / "transcripts" / f"{manifest['source_stem']}.json").read_text(encoding="utf-8"))
    return {
        "id": manifest["id"],
        "source": manifest["source"],
        "timeline": edl["timelines"][0],
        "edl_path": "edit/edl.json",
        "transcript": transcript["segments"],
        "artifacts": manifest["artifacts"],
    }


def _write_outputs(project_dir: Path) -> None:
    edl_path = project_dir / "edit" / "edl.json"
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    validation_errors = validate(edl_path)
    if validation_errors:
        raise ValueError("EDL validation failed: " + "; ".join(validation_errors))

    timeline = edl["timelines"][0]
    subtitle_path = project_dir / "edit" / "subtitles" / "timeline.srt"
    build_srt_for_timeline(edl, timeline, edl_path.parent, subtitle_path)
    write_fcpxml(edl_path, project_dir / "edit" / "timeline.fcpxml")


def create_timeline_project(
    *,
    video_path: Path,
    raw_segments: list[dict[str, Any]],
    corrected_segments: list[dict[str, Any]],
    uploads_root: Path,
) -> dict[str, Any]:
    """Create the durable source + transcript + EDL project for one Studio upload."""
    project_id = f"timeline_{uuid.uuid4().hex[:12]}"
    project_dir = uploads_root / "studio-projects" / project_id
    raw_dir = project_dir / "raw"
    edit_dir = project_dir / "edit"
    source_target = raw_dir / video_path.name
    _link_original(video_path, source_target)

    media = ffprobe(source_target)
    width, height = int(media.get("width") or 1920), int(media.get("height") or 1080)
    duration = round(float(media.get("duration") or 0), 3)
    if duration <= 0:
        raise ValueError("Unable to read the uploaded video's duration")

    transcript = _transcript_payload(raw_segments, corrected_segments)
    (edit_dir / "transcripts").mkdir(parents=True, exist_ok=True)
    transcript_path = edit_dir / "transcripts" / f"{source_target.stem}.json"
    transcript_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")

    project_name = _safe_name(video_path.stem)
    edl = {
        "version": 1,
        "project_name": project_name,
        "fps": _frame_rate(media.get("avg_frame_rate")),
        "timelines": [
            {
                "name": "完整时间线",
                "resolution": [width, height],
                "sources": {"A001": f"raw/{source_target.name}"},
                "ranges": [
                    {
                        "id": "t001-r0001",
                        "source": "A001",
                        "source_start": 0.0,
                        "source_end": duration,
                        "record_start": 0.0,
                        "track": 1,
                        "beat": "SOURCE",
                        "quote": "原始视频完整时间线",
                        "reason": "先保留完整原片，后续由可视化时间线选择并调整片段。",
                    }
                ],
                "subtitles": {"mode": "srt", "path": "edit/subtitles/timeline.srt"},
                "markers": True,
            }
        ],
    }
    edit_dir.mkdir(parents=True, exist_ok=True)
    (edit_dir / "edl.json").write_text(json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_outputs(project_dir)

    manifest = {
        "id": project_id,
        "source_stem": source_target.stem,
        "source": {
            "name": video_path.name,
            "duration": duration,
            "width": width,
            "height": height,
            "fps": edl["fps"],
        },
        "artifacts": {
            "edl": "edit/edl.json",
            "srt": "edit/subtitles/timeline.srt",
            "fcpxml": "edit/timeline.fcpxml",
        },
    }
    (project_dir / "project.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return _project_payload(project_dir)


def load_timeline_project(*, project_id: str, uploads_root: Path) -> dict[str, Any]:
    project_dir = (uploads_root / "studio-projects" / project_id).resolve()
    root = (uploads_root / "studio-projects").resolve()
    if root not in project_dir.parents or not (project_dir / "project.json").exists():
        raise FileNotFoundError("Timeline project not found")
    return _project_payload(project_dir)


def update_timeline_project(
    *, project_id: str, uploads_root: Path, ranges: list[dict[str, Any]]
) -> dict[str, Any]:
    """Persist source ranges from the visual editor, then regenerate exports."""
    project_dir = (uploads_root / "studio-projects" / project_id).resolve()
    root = (uploads_root / "studio-projects").resolve()
    if root not in project_dir.parents or not (project_dir / "project.json").exists():
        raise FileNotFoundError("Timeline project not found")

    edl_path = project_dir / "edit" / "edl.json"
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    source_duration = float(json.loads((project_dir / "project.json").read_text(encoding="utf-8"))["source"]["duration"])
    normalized: list[dict[str, Any]] = []
    record_start = 0.0
    for index, item in enumerate(sorted(ranges, key=lambda value: float(value.get("source_start", 0)))):
        start = max(0.0, min(float(item.get("source_start", 0)), source_duration))
        end = max(start, min(float(item.get("source_end", 0)), source_duration))
        if end - start < MIN_RANGE_SECONDS:
            continue
        normalized.append(
            {
                "id": f"t001-r{index + 1:04d}",
                "source": "A001",
                "source_start": round(start, 3),
                "source_end": round(end, 3),
                "record_start": round(record_start, 3),
                "track": 1,
                "beat": str(item.get("beat", "SELECTED")),
                "quote": str(item.get("quote", "")),
                "reason": str(item.get("reason", "在可视化时间线中保存。")),
            }
        )
        record_start += end - start
    if not normalized:
        raise ValueError("At least one timeline segment of 0.8 seconds is required")

    edl["timelines"][0]["ranges"] = normalized
    edl_path.write_text(json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_outputs(project_dir)
    return _project_payload(project_dir)
