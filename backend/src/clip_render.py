"""第五步（2026-10-14）：clip_plan → 本地 FFmpeg 渲染，生成多个视频成品。

定位：把第四步的 ``clip_plan.json`` 逐条渲染成可解码、可播放的成片。
**不复用 video-brain 的任务规划**（那是另一条 legacy 链）；这里只做：

* 每条切片一个**连续源视频区间**（start_ms/end_ms 来自 clip_plan，程序不改动）；
* FFmpeg 重编码 H.264 + AAC（保留自然音频，+faststart，偶数像素）；
* 字幕：复用「纠错转写 + 时间平移」的确定性流程——文字来自指定版本的
  corrected transcript，时间减去 clip 的 start_ms，绝不使用其他切片的文字、
  绝不让模型重写。**底片不烧录字幕**，只产 sidecar ``subtitles.srt``；
  烧录由 ``studio_transform_export`` 在 transform 之后的 1080x1920 画布上
  合成（字幕属于画布叠加层，zoom/pan 不再影响字幕大小与位置）；
* 产物：``tasks/output/<job_id>/clips/{clip_id}/
  {clip_final.mp4, subtitles.srt, warnings.json, clip_plan.json}``；
  本轮**不生成 cover.jpg**（缺封面不是错误）；
* **没有任何质量拦截**：时长/completeness/评分/字幕一般问题全部只进 warnings；
  只有真实技术故障（FFmpeg 失败、产物不可解码）才算 technical_error；
* 隔离与幂等：单条失败继续其余；已存在且可解码的 clip_final.mp4 直接跳过
  （worker 重启恢复）；clip_id 稳定 → 重试不产生重复成品。

本模块**不 import 任何模型客户端**（渲染与模型无关）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import hashlib
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

FFMPEG_BIN = os.getenv("CONTENT_RENDER_FFMPEG", "ffmpeg")
FFPROBE_BIN = os.getenv("CONTENT_RENDER_FFPROBE", "ffprobe")
FFPROBE_TIMEOUT_SECONDS = 120
FFMPEG_TIMEOUT_SECONDS = 3600
from .capacity_guard import reserve_capacity, estimate_render_peak, CapacityInsufficientError
from . import subtitle_compose as _subtitle_compose
_FF_FALLBACK_DIRS = ("/domain/dsh-home/bin", "/usr/local/bin", "/usr/bin")

# 资源/性能参数（刻意保守：可解码可播放优先，不做画质竞赛）
# 2026-10-15：源长边先压到 1920（只缩不放、保持纵横比），避免 4K 源触发
# 容器 cgroup OOM（6GB 上限，x264 4K 编码内存峰值 ~2GB+，实测被 SIGKILL）。
_VIDEO_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-pix_fmt", "yuv420p",
               "-vf", "scale=1920:1920:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2"]
_AUDIO_ARGS = ["-c:a", "aac", "-b:a", "128k"]


def _resolve(bin_name: str) -> str:
    if Path(bin_name).is_file() or shutil.which(bin_name):
        return bin_name if shutil.which(bin_name) else bin_name
    for directory in _FF_FALLBACK_DIRS:
        candidate = Path(directory) / bin_name
        if candidate.is_file():
            return str(candidate)
    return bin_name  # 交给调用方报错（技术故障路径）


def _ffprobe_playable(path: Path) -> tuple[bool, str]:
    """产物可解码校验：ffprobe 能读到视频流且时长 > 0。"""
    try:
        probe = subprocess.run(
            [_resolve(FFPROBE_BIN), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"ffprobe 技术失败/超时: {type(exc).__name__}"
    if probe.returncode != 0:
        return False, f"ffprobe 失败: {probe.stderr.strip()[:160]}"
    try:
        data = json.loads(probe.stdout or "{}")
        streams = data.get("streams") or []
        duration = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        return False, "ffprobe 输出无法解析"
    if not streams:
        return False, "产物没有视频流"
    if duration <= 0:
        return False, "产物时长为 0"
    return True, ""


def _srt_ts(ms: int) -> str:
    ms = max(0, int(ms))
    hours, remainder = divmod(ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def build_srt(corrected_segments: list[dict[str, Any]], start_ms: int, end_ms: int
              ) -> tuple[str, list[str], list[str]]:
    """从纠错转写构建切片内字幕：时间减 start_ms、裁剪到 [0, duration]。

    返回 ``(srt_text, used_segment_ids, warnings)``。空Range/无命中只记
    warning，绝不抛错（字幕问题不阻止视频生成）。

    文本按 subtitle_compose 默认画布样式**确定性折行**：本机 libass 对
    「纯 CJK 无空格长行」不做自动折行（WrapStyle 0 也溢出画布），不在这里
    折行的话烧录成片时字幕会超出 1080 宽被裁边。
    """
    duration_ms = max(1, end_ms - start_ms)
    used: list[str] = []
    warnings: list[str] = []
    blocks: list[str] = []
    _wrap_limit = _subtitle_compose._wrap_limit(
        _subtitle_compose.normalize_config(None), _subtitle_compose.DEFAULT_WIDTH)
    for offset, segment in enumerate(corrected_segments, start=1):
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        seg_start = float(segment.get("start_ms", (segment.get("start") or 0.0) * 1000))
        seg_end = float(segment.get("end_ms", (segment.get("end") or 0.0) * 1000))
        rel_start = seg_start - start_ms
        rel_end = seg_end - start_ms
        if rel_end <= 0 or rel_start >= duration_ms:
            continue  # 完全在切片之外：不允许使用其他切片/区间的文字
        clipped_start = max(0, rel_start)
        clipped_end = min(duration_ms, rel_end)
        if clipped_end - clipped_start < 200:
            warnings.append(f"字幕段 {segment.get('segment_id', offset)} 落在切片内不足 200ms，已跳过")
            continue
        used.append(str(segment.get("segment_id") or f"seg_{offset:04d}"))
        wrapped = "\n".join(_subtitle_compose._wrap_display_lines(text, _wrap_limit))
        blocks.append(
            f"{offset}\n{_srt_ts(clipped_start)} --> {_srt_ts(clipped_end)}\n{wrapped}\n")
    if not blocks:
        warnings.append("切片时间范围内没有命中任何转写分段，字幕文件为空（不影响视频）")
    return "\n".join(blocks), used, warnings


def _run_ffmpeg(args: list[str]) -> None:
    resolved = _resolve(FFMPEG_BIN)
    try:
        # subprocess.run 在 TimeoutExpired 前 kill+wait，避免遗留编码进程。
        probe = subprocess.run([resolved, "-hide_banner", "-loglevel", "error", "-y", *args],
                               capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"ffmpeg 技术失败/超时: {type(exc).__name__}") from exc
    if probe.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败: {probe.stderr.strip()[-400:]}")


def _subtitle_cues(segments, start_ms, end_ms):
    duration = max(1, end_ms - start_ms)
    cues = []
    for number, segment in enumerate(segments, 1):
        text = str(segment.get("text") or "").strip()
        start = max(0, float(segment.get("start_ms", (segment.get("start") or 0)*1000)) - start_ms)
        end = min(duration, float(segment.get("end_ms", (segment.get("end") or 0)*1000)) - start_ms)
        if text and end - start >= 200:
            cues.append({"segment_id": str(segment.get("segment_id") or f"seg_{number:04d}"),
                         "start": start/1000, "end": end/1000, "text": text})
    return cues


def _subtitle_font():
    from . import font_registry
    for name in ("MicrosoftYaHeiBold.ttc", "wqy-zenhei.ttc", "unifont.otf", "NotoSansCJKsc-Regular.otf", "SourceHanSansSC-Regular.otf"):
        font = font_registry.find_font_path(name)
        if font is not None and (font.resolve().is_relative_to(font_registry.FONTS_DIR.resolve())
                                 or font.resolve() in {p.resolve() for p in font_registry.SYSTEM_CJK_FONT_PATHS}):
            stat = font.stat()
            family = _font_family(str(font), stat.st_size, stat.st_mtime_ns)
            if family:
                return font, family
    return None, None


@lru_cache(maxsize=8)
def _font_family(path, size, mtime_ns):
    from .font_registry import get_font_family_name
    return get_font_family_name(Path(path))


def _filter_value(value):
    # libavfilter字符串参数转义，与shell无关；不执行拼接的命令。
    return str(value).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,").replace("[", "\\[").replace("]", "\\]")


def _render_fingerprint(clip, source, corrected_segments, transcript_ref, rule_snapshot_id):
    try:
        stat = source.stat()
        identity = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
    except OSError:
        identity = [str(source)]
    # 版本串参与指纹：sidecar-subtitles/2 = 默认字幕样式 54px/14字折行 + 侧车 SRT
    # 显式折行；旧「62px 不折行」底片/侧车缓存必须全部失效重渲。
    payload = {"version": "sidecar-subtitles/2", "source": identity,
               "start_ms": clip.get("start_ms"), "end_ms": clip.get("end_ms"),
               "transcript": transcript_ref, "segments": corrected_segments,
               "rule": rule_snapshot_id, "video_args": _VIDEO_ARGS, "audio_args": _AUDIO_ARGS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _cached_entry(clip, source, out_dir, segments, transcript_ref, rule_snapshot_id):
    final = out_dir / "clip_final.mp4"
    warnings = out_dir / "warnings.json"
    srt = out_dir / "subtitles.srt"
    if not (final.is_file() and warnings.is_file() and srt.is_file()):
        return None
    try:
        doc = json.loads(warnings.read_text(encoding="utf-8"))
        if doc.get("render_fingerprint") != _render_fingerprint(clip, source, segments, transcript_ref, rule_snapshot_id):
            return None
        if doc.get("srt_sha256") != hashlib.sha256(srt.read_bytes()).hexdigest():
            return None
    except (OSError, ValueError):
        return None
    if not _ffprobe_playable(final)[0]:
        return None
    return {"clip_id": clip["clip_id"], "status": "skipped_playable", "path": str(final),
            "warnings": list(clip.get("warnings") or []), "render_warnings": doc.get("render_warnings") or [],
            "subtitle_provenance": doc.get("subtitle_provenance") or {},
            "segments": doc.get("subtitle_segments") or [], "subtitle_mode": doc.get("subtitle_mode")}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _render_one_reserved(clip: dict[str, Any], *, source: Path, out_dir: Path,
               corrected_segments: list[dict[str, Any]], transcript_ref: dict[str, Any],
               rule_snapshot_id: str | None, force: bool = False) -> dict[str, Any]:
    """渲染单条切片（幂等）。返回条目状态：``rendered`` / ``skipped_playable``。

    技术故障抛 :class:`ClipRenderError`（调用方隔离，不中断其他切片）。
    """
    clip_id = str(clip.get("clip_id") or "").strip()
    if not clip_id:
        raise ClipRenderError("clip 缺少 clip_id")
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / "clip_final.mp4"
    working_path = out_dir / "clip_final.rendering.mp4"
    srt_path = out_dir / "subtitles.srt"
    warnings_path = out_dir / "warnings.json"
    plan_path = out_dir / "clip_plan.json"

    plan_warnings = [str(w) for w in (clip.get("warnings") or [])]
    start_ms = int(clip.get("start_ms") or 0)
    end_ms = int(clip.get("end_ms") or 0)
    duration_ms = max(1, end_ms - start_ms)

    _write_json(plan_path, clip)
    srt_text, used_ids, subtitle_warnings = build_srt(
        corrected_segments, start_ms, end_ms)
    srt_path.write_text(srt_text, encoding="utf-8")

    render_warnings: list[str] = list(subtitle_warnings)
    video_args = list(_VIDEO_ARGS)
    # 字幕烧录统一移到 transform 导出（studio_transform_export._subtitle_burn_spec）：
    # 底片只产 sidecar SRT，字幕作为画布叠加层在 1080x1920 成型后合成——
    # zoom/pan/背景模式不再放大或推出字幕（剪映同款模型）。
    subtitle_mode = "sidecar_only" if srt_text else "absent"
    try:
        _run_ffmpeg([
            "-ss", f"{start_ms / 1000:.3f}", "-i", str(source),
            "-t", f"{duration_ms / 1000:.3f}",
            *video_args, *_AUDIO_ARGS,
            "-movflags", "+faststart", str(working_path),
        ])
    except RuntimeError as exc:
        working_path.unlink(missing_ok=True)
        raise ClipRenderError(str(exc)) from exc

    playable, reason = _ffprobe_playable(working_path)
    if not playable:
        working_path.unlink(missing_ok=True)
        raise ClipRenderError(f"产物不可解码：{reason}")

    os.replace(working_path, final_path)
    provenance = {"transcript_id": transcript_ref.get("transcript_id"),
                  "transcript_version": transcript_ref.get("transcript_version"),
                  "segment_ids": used_ids,
                  "time_shift": "subtitle_ms = transcript_ms - clip.start_ms",
                  "subtitle_mode": subtitle_mode}
    subtitle_segments = _subtitle_cues(corrected_segments, start_ms, end_ms)
    warnings_doc = {
        "render_fingerprint": _render_fingerprint(clip, source, corrected_segments, transcript_ref, rule_snapshot_id),
        "srt_sha256": hashlib.sha256(srt_text.encode()).hexdigest(),
        "subtitle_mode": subtitle_mode, "subtitle_segments": subtitle_segments,
        "clip_id": clip_id,
        "completeness": str(clip.get("completeness") or "pass"),
        "warnings": plan_warnings,
        "render_warnings": render_warnings,
        "cover": None,  # 本阶段不生成封面；缺封面不是错误
        "subtitle_provenance": provenance,
        "rule_snapshot_id": rule_snapshot_id,
    }
    _write_json(warnings_path, warnings_doc)
    return {"clip_id": clip_id, "status": "rendered", "path": str(final_path),
            "warnings": plan_warnings, "render_warnings": render_warnings,
            "subtitle_provenance": provenance, "segments": subtitle_segments, "subtitle_mode": subtitle_mode}


def render_one(clip, *, source, out_dir, corrected_segments, transcript_ref,
               rule_snapshot_id, force=False):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", str(clip.get("clip_id") or "")):
        raise ClipRenderError("非法clip_id，拒绝渲染路径")
    final = out_dir / "clip_final.mp4"
    if not force:
        cached = _cached_entry(clip, source, out_dir, corrected_segments, transcript_ref, rule_snapshot_id)
        if cached is not None:
            return cached
    duration = int(clip.get("end_ms") or 0) - int(clip.get("start_ms") or 0)
    try:
        with reserve_capacity("render", estimate_render_peak(duration), path=out_dir):
            return _render_one_reserved(clip, source=source, out_dir=out_dir,
                                       corrected_segments=corrected_segments,
                                       transcript_ref=transcript_ref,
                                       rule_snapshot_id=rule_snapshot_id, force=force)
    except CapacityInsufficientError as exc:
        failure = ClipRenderError(str(exc))
        failure.capacity = exc.as_dict()
        raise failure from exc
    except (OSError, ValueError) as exc:
        raise ClipRenderError(f"渲染技术失败: {type(exc).__name__}: {exc}") from exc


class ClipRenderError(RuntimeError):
    """单条切片的真实技术故障（FFmpeg 失败/产物不可解码）。"""


def render_plan(plan_record: dict[str, Any], *, source: Path, out_root: Path,
                corrected_segments: list[dict[str, Any]], transcript_ref: dict[str, Any],
                rule_snapshot_id: str | None = None,
                progress: Callable[[str], None] | None = None,
                render_one_fn=None) -> dict[str, Any]:
    """逐条渲染整个计划（隔离 + 幂等），返回聚合结果与最终任务状态。

    状态聚合（需求 6）：
    * 全部成功且无警告 → ``completed``；
    * 全部成功且有普通警告 → ``completed_with_warnings``；
    * 存在真实技术故障 → ``partial_succeeded``（成功文件保留）；
    * 全部切片都技术故障 → ``technical_failed``。
    """
    clips = list(plan_record.get("clips") or [])
    plan_warnings = [str(w) for w in (plan_record.get("warnings") or [])]
    render = render_one_fn or render_one
    entries: list[dict[str, Any]] = []
    for number, clip in enumerate(clips, start=1):
        clip_id = str(clip.get("clip_id") or f"clip-{number}")
        if progress:
            progress(f"渲染切片 {number}/{len(clips)}：{clip.get('topic', clip_id)}")
        out_dir = out_root / "clips" / clip_id
        try:
            entry = render(clip, source=source, out_dir=out_dir,
                               corrected_segments=corrected_segments,
                               transcript_ref=transcript_ref,
                               rule_snapshot_id=rule_snapshot_id)
            entry["technical_error"] = None
        except ClipRenderError as exc:
            entry = {"clip_id": clip_id, "status": "technical_error",
                     "technical_error": str(exc)[:300],
                     "warnings": [str(w) for w in (clip.get("warnings") or [])],
                     "render_warnings": []}
            if hasattr(exc, "capacity"):
                entry["capacity"] = exc.capacity
        entries.append(entry)

    technical = [e for e in entries if e["status"] == "technical_error"]
    succeeded = [e for e in entries if e["status"] != "technical_error"]
    has_warnings = bool(plan_warnings) or any(
        e.get("warnings") or e.get("render_warnings") for e in entries)
    if clips and len(technical) == len(clips):
        job_status = "technical_failed"
    elif technical:
        job_status = "partial_succeeded"
    elif has_warnings:
        job_status = "completed_with_warnings"
    else:
        job_status = "completed"
    if not clips:
        plan_warnings = plan_warnings + ["clip_plan 中没有切片，无成品可渲染"]
        job_status = "completed_with_warnings"
    return {
        "job_status": job_status,
        "entries": entries,
        "counts": {"total": len(clips), "succeeded": len(succeeded),
                   "technical_error": len(technical),
                   "skipped_playable": sum(1 for e in entries if e["status"] == "skipped_playable")},
        "warnings": plan_warnings,
    }
