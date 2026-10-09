"""Local worker for Studio's fixed medical short-video production workflow.

The worker deliberately consumes Studio's already-corrected timestamped text;
it reuses Studio's corrected ASR instead of repeating transcription. It selects continuous source
ranges around the supplied golden sentences, locks a 9:16 crop per clip, burns
ASS captions, builds a source-frame cover intro, and writes QC artifacts.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageFont


ROOT = Path("/app/tasks")
FONT = Path("/app/fonts/MicrosoftYaHeiBold.ttc")
FALLBACK_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
FILLER_ONLY = re.compile(r"^[嗯呃啊，。！？!?,\s]+$")
# A cover is read in less than a second.  Conversation openers make poor titles,
# and, more importantly, long source sentences must never be allowed to escape
# the 9:16 safe area just because they happen to be the first ASR sentence.
COVER_LEADING_FILLERS = re.compile(
    r"^(?:(?:第[一二三四五六七八九十]+(?:个|点|呢)?|首先|然后|那么|嗯|呃|啊|就是|就是说|因为|所以|这个|那个)[，,、：:\s]*)+"
)


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def update_status(task_id: str, status: str, stage: str, progress: float, error: str | None = None) -> None:
    path = ROOT / "status" / f"{task_id}.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    payload = {
        "task_id": task_id, "status": status, "stage": stage,
        "progress": round(progress, 3), "created_at": previous.get("created_at", now()),
        "updated_at": now(), "artifacts": [],
    }
    if error:
        payload["error"] = error[-5000:]
    write_json(path, payload)


def run(command: list[str], *, cwd: Path | None = None) -> None:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True, encoding="utf-8", errors="replace")
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout)[-5000:])


def probe(path: Path) -> dict[str, Any]:
    completed = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height", "-of", "json", str(path)], text=True, capture_output=True, encoding="utf-8", check=True)
    return json.loads(completed.stdout)


def ass_time(seconds: float) -> str:
    centiseconds = round(max(0, seconds) * 100)
    hours, centiseconds = divmod(centiseconds, 360000)
    minutes, centiseconds = divmod(centiseconds, 6000)
    sec, cs = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02}:{sec:02}.{cs:02}"


def srt_time(seconds: float) -> str:
    millis = round(max(0, seconds) * 1000)
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    sec, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{sec:02},{millis:03}"


def clip_text(text: str, limit: int = 16) -> str:
    compact = re.sub(r"\s+", "", text).strip("，。！？；：")
    return compact[:limit] or "视频重点"


def cover_text(text: str, limit: int) -> str:
    """Return a short, source-grounded cover phrase that always fits one line.

    This intentionally does not invent a marketing headline.  It removes only
    spoken transitions, then keeps the first meaningful source phrase.  The
    renderer below performs a second pixel-width check before any text is drawn.
    """
    compact = re.sub(r"\s+", "", str(text or "")).strip("，。！？；：,.!?;:")
    compact = COVER_LEADING_FILLERS.sub("", compact).strip("，。！？；：,.!?;:")
    # A punctuation boundary is a much better cover boundary than a raw ASR
    # character stream; it keeps the title semantically connected to the source.
    first_phrase = re.split(r"[，。！？；：,.!?;:]", compact, maxsplit=1)[0]
    candidate = (first_phrase or compact).strip("，。！？；：,.!?;:")
    return candidate[:limit] or "视频重点"


def select_plans(segments: list[dict[str, Any]], goldens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pick up to two continuous, non-overlapping clips around Studio highlights."""
    seeds = sorted((item for item in goldens if isinstance(item, dict)), key=lambda item: float(item.get("start", 0)))
    if not seeds:
        seeds = [{"start": segments[0]["start"], "end": segments[min(len(segments) - 1, 5)]["end"], "text": segments[0]["text"]}]
    plans: list[dict[str, Any]] = []
    for seed in seeds:
        centre = (float(seed.get("start", 0)) + float(seed.get("end", 0))) / 2
        index = min(range(len(segments)), key=lambda i: abs((segments[i]["start"] + segments[i]["end"]) / 2 - centre))
        first = last = index
        while first > 0 and segments[last]["end"] - segments[first]["start"] < 19:
            if segments[first]["start"] - segments[first - 1]["end"] > 4: break
            first -= 1
        while last < len(segments) - 1 and segments[last]["end"] - segments[first]["start"] < 27:
            if segments[last + 1]["start"] - segments[last]["end"] > 4: break
            last += 1
        start, end = segments[first]["start"], segments[last]["end"]
        if end - start < 8:
            continue
        if any(max(start, existing["start"]) < min(end, existing["end"]) for existing in plans):
            continue
        chosen = segments[first:last + 1]
        title = cover_text(str(seed.get("text") or chosen[0]["text"]), 9)
        plans.append({
            "clip_id": f"clip_{len(plans) + 1:02}", "start": round(start, 3), "end": round(end, 3),
            "topic": title, "cover_title": title,
            "hooks": [cover_text(chosen[0]["text"], 11), cover_text(chosen[-1]["text"], 11)],
            "opening_context": chosen[0]["text"], "core_points": [segment["text"] for segment in chosen],
            "closing_thought": chosen[-1]["text"],
            "evidence_segment_ids": list(range(first, last + 1)),
            "cut_in_reason": "从金句附近的完整时间戳语义段向前扩展，保留开场上下文。",
            "cut_out_reason": "在同一连续语义段的自然收束后结束。",
            "completeness": "warning",
            "warning": "自动规划基于当前金句与分段时间戳；请在工作台审核开场上下文与结尾完整性。",
        })
        if len(plans) == 2:
            break
    return plans


def write_subtitles(segments: list[dict[str, Any]], plan: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    selected = [segment for segment in segments if segment["end"] > plan["start"] and segment["start"] < plan["end"] and not FILLER_ONLY.fullmatch(segment["text"])]
    ass_lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "WrapStyle: 2", "",
        "[V4+ Styles]", "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        "Style: Default,Microsoft YaHei,52,&H00FFFFFF,&H000000FF,&H00101010,&H80000000,-1,0,0,0,100,100,1,0,1,4,1,2,86,86,190,1", "",
        "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    srt_lines: list[str] = []
    provenance = []
    for number, segment in enumerate(selected, 1):
        start, end = max(segment["start"], plan["start"]) - plan["start"], min(segment["end"], plan["end"]) - plan["start"]
        text = segment["text"].replace("{", "（").replace("}", "）")
        # 14 CJK glyphs at 52px, with the configured margins and outline, stay
        # inside 1080px even on a narrow player / exported vertical frame.
        display = "\\N".join(text[i:i + 14] for i in range(0, len(text), 14))
        ass_lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{display}")
        srt_lines.extend([str(number), f"{srt_time(start)} --> {srt_time(end)}", display.replace("\\N", "\n"), ""])
        provenance.append({"source_start": segment["start"], "source_end": segment["end"], "caption": text, "speaker": segment.get("speaker", "未标注")})
    (out_dir / "subtitle.ass").write_text("\n".join(ass_lines) + "\n", encoding="utf-8-sig")
    (out_dir / "subtitle.srt").write_text("\n".join(srt_lines), encoding="utf-8-sig")
    write_json(out_dir / "subtitle_provenance.json", {"source": "Studio corrected transcript", "alignment": "segment timestamps inherited from faster-whisper; word-level alignment unavailable in bridge payload", "cues": provenance})
    return {"cues": len(provenance), "warning": "字幕按现有分段时间戳生成；需对成片做播放复核以确认逐词同步。"}


def make_cover(source: Path, plan: dict[str, Any], out_dir: Path) -> Path:
    frame = out_dir / "cover_source_frame.jpg"
    frame_time = round((plan["start"] + plan["end"]) / 2, 3)
    run(["ffmpeg", "-y", "-ss", str(frame_time), "-i", str(source), "-frames:v", "1", "-q:v", "2", str(frame)])
    original = Image.open(frame).convert("RGB")
    background = ImageOps.fit(original, (1080, 1920), method=Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(20)).convert("RGBA")
    foreground = ImageOps.contain(original, (960, 1160), method=Image.Resampling.LANCZOS).convert("RGBA")
    background.alpha_composite(foreground, ((1080 - foreground.width) // 2, 430))
    font_file = FONT if FONT.exists() else FALLBACK_FONT
    draw = ImageDraw.Draw(background)
    layout: list[dict[str, Any]] = []

    def center(text: str, y: int, size: int, fill: str, stroke: int, role: str) -> None:
        # This is deliberately a hard safe-area gate, not merely a visual hint.
        # Text has already been shortened by cover_text; the measured-width pass
        # protects us from font-specific CJK glyph widths and outline expansion.
        max_width = 840
        while True:
            font = ImageFont.truetype(str(font_file), size)
            box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
            width = box[2] - box[0]
            if width <= max_width or size <= 38:
                break
            size -= 2
        if width > max_width:
            raise RuntimeError(f"Cover {role} exceeds the {max_width}px safe width")
        left = (1080 - width) / 2
        right = left + width
        if left < 120 or right > 960:
            raise RuntimeError(f"Cover {role} leaves the horizontal safe area")
        draw.text((left, y), text, font=font, fill=fill, stroke_width=stroke, stroke_fill="#090b0c")
        layout.append({"role": role, "text": text, "x": round(left, 1), "y": y, "width": width, "font_size": size, "safe": True})

    center(plan["cover_title"], 108, 72, "white", 6, "title")
    center(plan["hooks"][0], 1516, 62, "#FFD51A", 8, "hook_1")
    center(plan["hooks"][1], 1635, 62, "#FFD51A", 8, "hook_2")
    cover = out_dir / "cover.jpg"
    background.convert("RGB").save(cover, quality=94)
    write_json(out_dir / "cover_layout.json", {"template_id": "medical-cover-v3-fixed-frame", "source_time_s": frame_time, "background": "same frame gaussian blur", "foreground": "same source frame; subject extraction unavailable", "title": plan["cover_title"], "hooks": plan["hooks"], "text_layout": layout, "safe_area_px": [120, 960], "doctor_sticker": "not requested"})
    return cover


def render_clip(source: Path, segments: list[dict[str, Any],], plan: dict[str, Any], output: Path) -> dict[str, Any]:
    out_dir = output / plan["clip_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "representative_frames").mkdir(exist_ok=True)
    for index, factor in enumerate((0.1, 0.5, 0.9), 1):
        sample = plan["start"] + (plan["end"] - plan["start"]) * factor
        run(["ffmpeg", "-y", "-ss", str(sample), "-i", str(source), "-frames:v", "1", "-q:v", "3", str(out_dir / "representative_frames" / f"frame_{index}.jpg")])
    write_json(out_dir / "fixed_frame_plan.json", {"项目": {"画幅": "9:16", "策略": "连续镜头固定画框"}, "画框段": [{"时间": f"{plan['start']:.3f}-{plan['end']:.3f}", "原因": "当前桥接器使用固定居中 9:16 画框；已抽取三张代表帧供 QC。", "裁剪框": "scale to fill + centre crop", "动态跟随": False, "人工复核": True}], "质检": ["固定画框不漂移", "代表帧需确认人物完整", "字幕不遮挡脸和手"]})
    subtitle = write_subtitles(segments, plan, out_dir)
    cover = make_cover(source, plan, out_dir)
    duration = plan["end"] - plan["start"]
    body, intro, final = out_dir / "body.mp4", out_dir / "intro.mp4", out_dir / "clip_final.mp4"
    run(["ffmpeg", "-y", "-ss", str(plan["start"]), "-i", str(source), "-t", f"{duration:.3f}", "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,ass=subtitle.ass:fontsdir=/app/fonts", "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(body)], cwd=out_dir)
    run(["ffmpeg", "-y", "-loop", "1", "-t", "0.8", "-i", str(cover), "-f", "lavfi", "-t", "0.8", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000", "-vf", "scale=1080:1920,fade=t=out:st=0.62:d=0.18,format=yuv420p", "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-crf", "19", "-c:a", "aac", "-shortest", str(intro)])
    run(["ffmpeg", "-y", "-i", str(intro), "-i", str(body), "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v];[0:a][1:a]concat=n=2:v=0:a=1[a]", "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(final)])
    metadata = probe(final)
    run(["ffmpeg", "-v", "error", "-i", str(final), "-f", "null", "-"])
    video = next(item for item in metadata["streams"] if item["codec_type"] == "video")
    report = {"clip_id": plan["clip_id"], "status": "completed_with_warnings", "source_range_s": [plan["start"], plan["end"]], "output": str(final), "technical": {"decode_pass": True, "width": video.get("width"), "height": video.get("height"), "duration_s": float(metadata["format"]["duration"])}, "subtitle": subtitle, "warnings": [plan["warning"], "未执行逐词强制对齐，字幕同步状态为待播放复核。", "固定居中裁剪的三张代表帧已输出，需核验人物和手势未被裁切。"]}
    write_json(out_dir / "render_manifest.json", {"source": str(source), "source_range_s": report["source_range_s"], "cover_intro_s": 0.8, "crop": "fixed centre crop 9:16", "subtitle_source": "corrected Studio transcript", "output": str(final)})
    write_json(out_dir / "qc_report.json", report)
    return report


def process(task_file: Path) -> None:
    task = json.loads(task_file.read_text(encoding="utf-8"))
    task_id = str(task["task_id"])
    processing = ROOT / "processing" / task_file.name
    task_file.replace(processing)
    update_status(task_id, "running", "固化流程：读取纠错转写并生成切片方案", 0.12)
    source = (ROOT / task["raw_video_path"]).resolve()
    output = (ROOT / task["output_dir"]).resolve()
    if ROOT.resolve() not in source.parents or not source.is_file(): raise FileNotFoundError("Shared task source video is missing")
    output.mkdir(parents=True, exist_ok=True)
    segments = list(task["asr_segments"])
    source_probe = probe(source)
    write_json(output / "task_snapshot.json", {"source": str(source), "preflight": source_probe, "requested_clip_count": 2, "rule_set": "medical-video-production fixed workflow", "asr": {"model": "small", "device": "cpu", "compute_type": "int8", "vad_filter": True, "reused_from_studio": True}, "local_only": True, "started_at": now()})
    write_json(output / "corrected_transcript.json", {"segments": segments, "source": "Studio corrected transcript"})
    write_json(output / "retrieved_rules.json", {"source": "local fallback", "version": "medical-video-production-v1", "rules": ["fixed frame", "corrected transcript only", "ASS captions", "cover intro", "local QC"]})
    plans = select_plans(segments, list(task.get("golden_sentences") or []))
    if not plans: raise RuntimeError("No continuous timestamped clip could be planned")
    write_json(output / "clip_plan.json", {"highlights": plans, "keywords": task.get("keywords", [])})
    reports = []
    for index, plan in enumerate(plans, 1):
        update_status(task_id, "running", f"固化流程：渲染 {plan['clip_id']}（固定画框、ASS 字幕、封面）", 0.25 + index / len(plans) * 0.6)
        reports.append(render_clip(source, segments, plan, output))
    write_json(output / "qc_report.json", {"status": "completed_with_warnings", "clips": reports, "local_only": True})
    (output / "done.ok").write_text(f"completed_at={now()}\n", encoding="utf-8")
    update_status(task_id, "completed", "固化剪辑流程完成：成片、字幕、封面与 QC 已回传工作台", 1.0)
    processing.replace(ROOT / "completed" / processing.name)


def main() -> None:
    for directory in ("in", "processing", "completed", "failed", "status", "media", "output"):
        (ROOT / directory).mkdir(parents=True, exist_ok=True)
    while True:
        task = next(iter(sorted((ROOT / "in").glob("*.json"), key=lambda path: path.stat().st_mtime)), None)
        if not task:
            time.sleep(2)
            continue
        task_id = task.stem
        try:
            process(task)
        except Exception as exc:
            update_status(task_id, "failed", "固化剪辑流程技术失败", 1.0, f"{exc}\n{traceback.format_exc()}")
            processing = ROOT / "processing" / task.name
            if processing.exists(): processing.replace(ROOT / "failed" / processing.name)
            elif task.exists(): task.replace(ROOT / "failed" / task.name)


if __name__ == "__main__":
    main()
