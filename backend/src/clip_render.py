"""第五步（2026-10-14）：clip_plan → 本地 FFmpeg 渲染，生成多个视频成品。

定位：把第四步的 ``clip_plan.json`` 逐条渲染成可解码、可播放的成片。
**不复用 video-brain 的任务规划**（那是另一条 legacy 链）；这里只做：

* 每条切片一个**连续源视频区间**（start_ms/end_ms 来自 clip_plan，程序不改动）；
* FFmpeg 重编码 H.264 + AAC（保留自然音频，+faststart，偶数像素）；
* 字幕：复用「纠错转写 + 时间平移」的确定性流程——文字来自指定版本的
  corrected transcript，时间减去 clip 的 start_ms，绝不使用其他切片的文字、
  绝不让模型重写。**渲染即烧录**（2026-10-29 固化，``SUBTITLE_HARDBURN=true``
  默认）：同一遍 ffmpeg 在 scale 之后追加固化样式 ASS（白字/纯黑 2px 描边/
  ``Shadow=0``），成片产出即 ``subtitle_mode=burned``，并同产 sidecar
  ``subtitles.srt/.ass`` 供审计；burned 底片导出跳过二次烧录。
  ``SUBTITLE_HARDBURN=false`` 为紧急停机开关：有 cue 的切片硬失败
  （2026-11-04 契约：切片预览必须显示字幕）；详见 docs/subtitle-burn-convention.md；
* 产物：``tasks/output/<job_id>/clips/{clip_id}/
  {clip_final.mp4, subtitles.srt, subtitles.ass, warnings.json, clip_plan.json}``；
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

from . import clip_purify

FFMPEG_BIN = os.getenv("CONTENT_RENDER_FFMPEG", "ffmpeg")
FFPROBE_BIN = os.getenv("CONTENT_RENDER_FFPROBE", "ffprobe")
FFPROBE_TIMEOUT_SECONDS = 120
FFMPEG_TIMEOUT_SECONDS = 3600
from .capacity_guard import reserve_capacity, estimate_render_peak, CapacityInsufficientError
from . import subtitle_compose as _subtitle_compose
from . import subtitle_cueing as _subtitle_cueing
_FF_FALLBACK_DIRS = ("/domain/dsh-home/bin", "/usr/local/bin", "/usr/bin")

# 资源/性能参数（刻意保守：可解码可播放优先，不做画质竞赛）
# 2026-10-15：源长边先压到 1920（只缩不放、保持纵横比），避免 4K 源触发
# 容器 cgroup OOM（6GB 上限，x264 4K 编码内存峰值 ~2GB+，实测被 SIGKILL）。
_VIDEO_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-pix_fmt", "yuv420p",
               "-vf", "scale=1920:1920:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2"]
_AUDIO_ARGS = ["-c:a", "aac", "-b:a", "128k"]
_BURN_FONTSDIR = os.getenv("BURN_FONTSDIR", "/usr/share/fonts/truetype/wqy")


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


def build_srt(corrected_segments: list[dict[str, Any]], start_ms: int, end_ms: int,
              words: list[dict[str, Any]] | None = None,
              ) -> tuple[str, list[str], list[str]]:
    """从纠错转写构建切片内字幕：cue 合成层（subtitle_cueing）+ SRT 序列化。

    返回 ``(srt_text, used_segment_ids, warnings)``。空Range/无命中只记
    warning，绝不抛错（字幕问题不阻止视频生成）。

    cue 合成规则（2026-10-29 字幕切分改造，详见 src/subtitle_cueing.py 模块头）：
    语音停顿/标点切句 → 口语冗余清理（可开关）→ 子句装箱（同屏 ≤2 行）→
    原子 token 折行（数字单位/保护词不拆、行首无标点、每行 ≤16 字）→
    时间轴整平（每条 ≥0.8s、不重叠、无缝隙）。可调参数走环境变量
    ``SUBTITLE_MAX_LINE_CHARS`` / ``SUBTITLE_MAX_LINES`` /
    ``SUBTITLE_MIN_CUE_SECONDS`` / ``SUBTITLE_STRIP_FILLERS`` /
    ``SUBTITLE_PUNCT_STYLE`` / ``SUBTITLE_SENTENCE_PAUSE_MS``。

    词级对齐（2026-11-12 字幕优化）：``words`` 提供词级时间（秒，绝对值）时，
    token 时间走真实词对齐——cue 尾部停在最后一个词、长停顿字幕消失；
    未提供回退段内线性插值（旧行为）。
    """
    duration_ms = max(1, end_ms - start_ms)
    cfg = _subtitle_cueing.config_from_env()
    words_ms = [{"text": str(w.get("text") or ""),
                 "start": float(w.get("start") or 0.0) * 1000.0,
                 "end": float(w.get("end") or 0.0) * 1000.0}
                for w in (words or [])] or None
    cues, used, warnings = _subtitle_cueing.build_cues_from_segments(
        corrected_segments, float(start_ms), float(end_ms), cfg, words_ms=words_ms)
    blocks: list[str] = []
    window_s = duration_ms / 1000.0
    for i, cue in enumerate(cues, start=1):
        s = max(0.0, min(float(cue["start"]), window_s))
        e = max(s, min(float(cue["end"]), window_s))
        blocks.append(f"{i}\n{_srt_ts(s * 1000)} --> {_srt_ts(e * 1000)}\n{cue['text']}\n")
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


def _apply_refined_segments(corrected_segments: list[dict[str, Any]],
                            refined_entry: dict[str, Any] | None,
                            ) -> tuple[list[dict[str, Any]], int]:
    """烧录文本替换为精修稿（2026-11-09 用户拍板：烧录改用精修稿）。

    仅当该切片精修 ``source=llm`` 时生效：按 segment_id（空 id 回退 start_ms
    精确匹配）把纠错分段文本替换为 ``refined_text``（语气词/无价值句已删）。
    ``refined_text`` 为空的分段（整句删除）保持空文本——cue 层自然跳过，
    时间码仍用原区间不压缩（遵守字幕规范 §1 规则 4）。只动副本，不改纠错
    转写本体；精修缺失/fallback 时不替换（按纠错转写渲染，如实回退）。

    返回 ``(替换后的分段列表, 实际替换为非空精修文本的分段数)``。
    """
    if not refined_entry or str(refined_entry.get("source") or "") != "llm":
        return corrected_segments, 0
    by_id: dict[str, str] = {}
    by_start: dict[int, str] = {}
    for seg in refined_entry.get("segments") or []:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("refined_text") or "").strip()
        seg_id = str(seg.get("segment_id") or "")
        if seg_id:
            by_id[seg_id] = text
        else:
            try:
                by_start[int(seg.get("start_ms"))] = text
            except (TypeError, ValueError):
                continue
    out: list[dict[str, Any]] = []
    replaced = 0
    for seg in corrected_segments:
        seg_id = str(seg.get("segment_id") or "")
        try:
            start = int(seg.get("start_ms") or 0)
        except (TypeError, ValueError):
            start = None
        text = by_id.get(seg_id) if seg_id else None
        if text is None and start is not None:
            text = by_start.get(start)           # 空 id / id 未命中 → start_ms 精确回退
        if text is None:
            out.append(seg)
            continue
        copy = dict(seg)
        copy["text"] = text
        if "corrected_text" in copy:
            copy["corrected_text"] = text
        out.append(copy)
        if text:
            replaced += 1
    return out, replaced


def _refined_identity(refined_entry: dict[str, Any] | None) -> str | None:
    """精修条目参与渲染指纹的身份串（仅 source=llm；缺失/fallback 为 None）。"""
    if not refined_entry or str(refined_entry.get("source") or "") != "llm":
        return None
    payload = {"source": "llm", "segments": refined_entry.get("segments") or []}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _render_fingerprint(clip, source, corrected_segments, transcript_ref, rule_snapshot_id,
                        refined_entry=None, words=None):
    try:
        stat = source.stat()
        identity = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
    except OSError:
        identity = [str(source)]
    # 版本串参与指纹：purify-concat/1 = 问答化净化重组多区间渲染（2026-11-11
    # 问答化 P1：clip.sub_ranges 有序子区间 concat，字幕按虚拟时间轴平移）。
    # refined-burn/1 及更早的旧缓存必须全部失效重渲（多区间契约变更）。
    # words-align/1 = 词级对齐烧录（2026-11-12）：传 words 时cue 时间走真实词
    # 首尾，旧无词缓存（purify-concat/1）指纹不匹配 → 自动失效重渲。
    version = "words-align/1" if words else "purify-concat/1"
    payload = {"version": version, "source": identity,
               "start_ms": clip.get("start_ms"), "end_ms": clip.get("end_ms"),
               "sub_ranges": clip.get("sub_ranges"),
               "transcript": transcript_ref, "segments": corrected_segments,
               "refine": _refined_identity(refined_entry),
               "rule": rule_snapshot_id, "video_args": _VIDEO_ARGS, "audio_args": _AUDIO_ARGS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _cached_entry(clip, source, out_dir, segments, transcript_ref, rule_snapshot_id,
                  refined_entry=None, words=None):
    final = out_dir / "clip_final.mp4"
    warnings = out_dir / "warnings.json"
    srt = out_dir / "subtitles.srt"
    if not (final.is_file() and warnings.is_file() and srt.is_file()):
        return None
    try:
        doc = json.loads(warnings.read_text(encoding="utf-8"))
        if doc.get("render_fingerprint") != _render_fingerprint(clip, source, segments, transcript_ref, rule_snapshot_id, refined_entry, words):
            return None
        if doc.get("srt_sha256") != hashlib.sha256(srt.read_bytes()).hexdigest():
            return None
    except (OSError, ValueError):
        return None
    if doc.get("subtitle_mode") == "sidecar_only":
        # 2026-11-04 固化契约（切片预览必须显示字幕）：旧 sidecar_only 缓存一律
        # 视为过期，返回 None 触发强制重渲染为 burned。
        return None
    if not _ffprobe_playable(final)[0]:
        return None
    return {"clip_id": clip["clip_id"], "status": "skipped_playable", "path": str(final),
            "warnings": list(clip.get("warnings") or []), "render_warnings": doc.get("render_warnings") or [],
            "subtitle_provenance": doc.get("subtitle_provenance") or {},
            "segments": doc.get("subtitle_segments") or [], "subtitle_mode": doc.get("subtitle_mode")}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _purify_ranges(clip: dict[str, Any], start_ms: int, end_ms: int
                   ) -> list[tuple[int, int]] | None:
    """clip.sub_ranges → 校验后的绝对毫秒区间列表；无效/未净化返回 None。

    校验：有序、互不重叠、都在 [start_ms, end_ms] 界内、且不等价于整段
    （等价于整段 = noop，走单区间老路径）。任何异常返回 None（回退老路径）。
    """
    try:
        raw = clip.get("sub_ranges") or []
        spans = [(int(r.get("start_ms") or 0), int(r.get("end_ms") or 0))
                 for r in raw if isinstance(r, dict)]
        if not spans:
            return None
        for prev, cur in zip(spans, spans[1:]):
            if cur[0] < prev[1]:
                return None
        for s, e in spans:
            if s < start_ms or e > end_ms or e <= s:
                return None
        if len(spans) == 1 and spans[0][0] <= start_ms and spans[0][1] >= end_ms:
            return None
        return spans
    except Exception:
        return None


def _shift_segments_for_ranges(segments: list[dict[str, Any]],
                               ranges: list[tuple[int, int]]
                               ) -> tuple[list[dict[str, Any]], int]:
    """分段映射到虚拟时间轴：完整落在子区间内的按累计偏移平移，其余剔除。"""
    shifted: list[dict[str, Any]] = []
    offset = 0
    for (s, e) in ranges:
        for seg in segments:
            seg_s = int(seg.get("start_ms") or 0)
            seg_e = int(seg.get("end_ms") or 0)
            if seg_s >= s and seg_e <= e:
                item = dict(seg)
                item["start_ms"] = seg_s - s + offset
                item["end_ms"] = seg_e - s + offset
                shifted.append(item)
        offset += e - s
    return shifted, offset


def _shift_words_for_ranges(words_ms: list[dict[str, Any]],
                            ranges: list[tuple[int, int]]) -> list[dict[str, Any]]:
    """词级时间与 ``_shift_segments_for_ranges`` 同映射平移到虚拟时间轴。

    跨界词按保留区间裁剪（取与区间交集），保证词中点落在平移后分段窗口内、
    词对齐不被边界词缺失拉偏；词时间单位毫秒（绝对源轴 → 虚拟轴）。"""
    out: list[dict[str, Any]] = []
    offset = 0
    for (s, e) in ranges:
        for w in words_ms:
            try:
                ws, we = float(w.get("start") or 0.0), float(w.get("end") or 0.0)
            except (TypeError, ValueError):
                continue
            if we <= s or ws >= e:
                continue
            c0, c1 = max(ws, s), min(we, e)
            out.append({"text": str(w.get("text") or ""),
                        "start": c0 - s + offset, "end": c1 - s + offset})
        offset += e - s
    return out


def _render_one_reserved(clip: dict[str, Any], *, source: Path, out_dir: Path,
               corrected_segments: list[dict[str, Any]], transcript_ref: dict[str, Any],
               rule_snapshot_id: str | None, force: bool = False,
               refined_entry: dict[str, Any] | None = None,
               words: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """渲染单条切片（幂等）。返回条目状态：``rendered`` / ``skipped_playable``。

    技术故障抛 :class:`ClipRenderError`（调用方隔离，不中断其他切片）。
    ``words``：词级时间（秒，绝对源轴，{text,start,end}，来自纠错转写 v2）——
    提供时烧录字幕 cue 时间走词对齐（2026-11-12 字幕优化）；缺失回退插值。
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
    # 净化重组（2026-11-11 问答化 P1）：sub_ranges → 虚拟时间轴（子区间 concat），
    # 分段/字幕全部平移到虚拟时间轴；无效 sub_ranges → None 走单区间老路径。
    ranges = _purify_ranges(clip, start_ms, end_ms)

    _write_json(plan_path, clip)
    # 烧录源（2026-11-09 用户拍板：烧录改用精修稿）：该切片精修 source=llm 时，
    # 文本替换为 refined_text（已删语气词/无价值句）；缺失/fallback 如实回退
    # 纠错转写（subtitle_provenance.subtitle_source 记录实际来源）。
    burn_segments, refined_count = _apply_refined_segments(corrected_segments, refined_entry)
    # 词级时间与分段同链路：净化重组时平移到虚拟时间轴（2026-11-12 words 接线），
    # build_srt 约定收秒（内部自行 ×1000 → ms）
    words_used: list[dict[str, Any]] | None = None
    if words:
        words_ms = [{"text": str(w.get("text") or ""),
                     "start": float(w.get("start") or 0.0) * 1000.0,
                     "end": float(w.get("end") or 0.0) * 1000.0}
                    for w in words]
        if ranges:
            words_ms = _shift_words_for_ranges(words_ms, ranges)
        words_used = [{"text": w["text"], "start": w["start"] / 1000.0,
                       "end": w["end"] / 1000.0} for w in words_ms] or None
    if ranges:
        burn_segments, duration_ms = _shift_segments_for_ranges(burn_segments, ranges)
        srt_start, srt_end = 0, duration_ms
    else:
        srt_start, srt_end = start_ms, end_ms
    srt_text, used_ids, subtitle_warnings = build_srt(
        burn_segments, srt_start, srt_end, words=words_used)
    srt_path.write_text(srt_text, encoding="utf-8")

    render_warnings: list[str] = list(subtitle_warnings)
    video_args = list(_VIDEO_ARGS)
    # 2026-10-29 字幕烧录固化（用户指令「固化烧录链」）：渲染同一遍 ffmpeg 直接烧入
    # 固化样式 ASS（白字/纯黑 2px 描边/Shadow=0，PlayRes 1080x1920 随输出分辨率等比
    # 缩放，滤镜链位于 scale 之后保证字幕在最终像素上成型）。成片即 burned：
    # 工作台预览/下载/抖音投递所见即所得；studio export-transformed 对 burned 底片
    # 自动跳过二次烧录（_subtitle_burn_spec 按 subtitle_mode=burned 返回 None）。
    # SUBTITLE_HARDBURN=false 仅为紧急停机开关：有 cue 的切片会硬失败
    # （ClipRenderError），不再静默降级 sidecar_only（2026-11-04 固化契约：
    # 切片预览必须显示字幕）。
    burn_ass_path: Path | None = None
    if srt_text and _subtitle_cueing.hardburn_enabled():
        burn_ass_path = out_dir / "subtitles.ass"
        burn_ass_path.write_text(
            _subtitle_cueing.srt_to_styled_ass(srt_text), encoding="utf-8")
        vf_idx = video_args.index("-vf")
        video_args[vf_idx + 1] = (
            f"{video_args[vf_idx + 1]},"
            f"subtitles=filename='{_filter_value(str(burn_ass_path))}'"
            f":fontsdir='{_filter_value(_BURN_FONTSDIR)}'")
        subtitle_mode = "burned"
    elif srt_text:
        # 2026-11-04 固化契约（用户指令「切片预览必须显示字幕」）：有 cue 的切片必须
        # 同遍烧录。SUBTITLE_HARDBURN=false 不再静默降级 sidecar_only，而是硬失败
        # （render_plan 按 technical_error 隔离，该条绝不发布），杜绝预览无字幕回归。
        raise ClipRenderError(
            "切片含字幕 cue 但 SUBTITLE_HARDBURN 已关闭：预览/成片必须烧录字幕，"
            "拒绝产出 sidecar_only 底片（2026-11-04 固化契约）")
    else:
        subtitle_mode = "absent"
    try:
        if ranges:
            # 多区间 concat（purify-concat/1）：输入侧粗定位（首段起、末段止），
            # filter_complex 逐段 trim+concat 成虚拟时间轴，再接原 -vf 链
            # （scale/字幕烧录，字幕时间已是虚拟时间轴，无需改动）。
            vf_value = video_args[video_args.index("-vf") + 1]
            base = ranges[0][0]
            rel = [(s - base, e - base) for s, e in ranges]
            n = len(rel)
            parts: list[str] = []
            for i, (s, e) in enumerate(rel):
                parts.append(f"[0:v]trim=start={s / 1000:.3f}:end={e / 1000:.3f},"
                             f"setpts=PTS-STARTPTS[v{i}]")
            for i, (s, e) in enumerate(rel):
                parts.append(f"[0:a]atrim=start={s / 1000:.3f}:end={e / 1000:.3f},"
                             f"asetpts=PTS-STARTPTS[a{i}]")
            v_labels = "".join(f"[v{i}]" for i in range(n))
            a_labels = "".join(f"[a{i}]" for i in range(n))
            parts.append(f"{v_labels}concat=n={n}:v=1:a=0[vcat]")
            parts.append(f"{a_labels}concat=n={n}:v=0:a=1[acat]")
            parts.append(f"[vcat]{vf_value}[vout]")
            vi = video_args.index("-vf")
            codec_args = video_args[:vi] + video_args[vi + 2:]
            _run_ffmpeg([
                "-ss", f"{ranges[0][0] / 1000:.3f}",
                "-t", f"{(ranges[-1][1] - ranges[0][0]) / 1000:.3f}",
                "-i", str(source),
                "-filter_complex", ";".join(parts),
                "-map", "[vout]", "-map", "[acat]",
                *codec_args, *_AUDIO_ARGS,
                "-movflags", "+faststart", str(working_path),
            ])
        else:
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
                  "time_shift": ("subtitle_ms = purify 虚拟时间轴（子区间 concat 平移）"
                                 if ranges else "subtitle_ms = transcript_ms - clip.start_ms"),
                  "subtitle_mode": subtitle_mode,
                  # 烧录文本来源（2026-11-09 精修稿契约）：clip_refinement=精修稿
                  # （source=llm，已删语气词/无价值句）；corrected_transcript=精修
                  # 缺失/fallback 时的如实回退。refined_segment_count=实际替换为
                  # 非空精修文本的分段数。
                  "subtitle_source": "clip_refinement" if refined_count else "corrected_transcript",
                  "refined_segment_count": refined_count,
                  # 词级对齐留痕（2026-11-12 words 接线）：used=是否提供词级时间；
                  # count=平移后仍在窗口内的词数；virtual_timeline=是否随净化
                  # sub_ranges 平移到虚拟时间轴。
                  **({"words": {"used": True, "count": len(words_used),
                                "virtual_timeline": bool(ranges)}}
                     if words_used else {"words": {"used": False}}),
                  **({"purify": {"version": clip_purify.PURIFY_VERSION,
                                 "ranges": [[s, e] for s, e in ranges],
                                 "dropped_segment_ids": list(
                                     (clip.get("purify") or {}).get("dropped_segment_ids") or [])}}
                     if ranges else {})}
    subtitle_segments = _subtitle_cues(burn_segments, srt_start, srt_end)
    warnings_doc = {
        "render_fingerprint": _render_fingerprint(clip, source, corrected_segments, transcript_ref, rule_snapshot_id, refined_entry, words),
        "srt_sha256": hashlib.sha256(srt_text.encode()).hexdigest(),
        "subtitle_mode": subtitle_mode, "subtitle_segments": subtitle_segments,
        "clip_id": clip_id,
        **({"subtitle_hard_burn": {
                "style": ("ASS PlayRes 1080x1920: Primary=white, OutlineColour=black, "
                          "BorderStyle=1, Outline=2, Shadow=0"),
                "font": _subtitle_cueing.BURN_FONT_NAME,
                "font_size_px": _subtitle_cueing.BURN_FONT_SIZE_PX,
                "max_line_chars": _subtitle_cueing.config_from_env().max_line_chars,
                "ass": str(burn_ass_path)}}
           if burn_ass_path is not None else {}),
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
               rule_snapshot_id, force=False, refined_entry=None, words=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", str(clip.get("clip_id") or "")):
        raise ClipRenderError("非法clip_id，拒绝渲染路径")
    final = out_dir / "clip_final.mp4"
    if not force:
        cached = _cached_entry(clip, source, out_dir, corrected_segments, transcript_ref, rule_snapshot_id, refined_entry, words)
        if cached is not None:
            return cached
    duration = int(clip.get("end_ms") or 0) - int(clip.get("start_ms") or 0)
    try:
        with reserve_capacity("render", estimate_render_peak(duration), path=out_dir):
            return _render_one_reserved(clip, source=source, out_dir=out_dir,
                                       corrected_segments=corrected_segments,
                                       transcript_ref=transcript_ref,
                                       rule_snapshot_id=rule_snapshot_id, force=force,
                                       refined_entry=refined_entry, words=words)
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
                render_one_fn=None,
                refinement: dict[str, Any] | None = None,
                words: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """逐条渲染整个计划（隔离 + 幂等），返回聚合结果与最终任务状态。

    ``refinement``：clip_refinement.json 记录（2026-11-09 烧录精修稿契约）。
    按切片取 ``clips[clip_id]`` 条目传给渲染：source=llm 的条目替换烧录文本；
    缺失/fallback 条目按纠错转写渲染（render 层如实回退，不冒充）。

    ``words``：词级时间（秒，绝对源轴，来自纠错转写 v2）——透传给每条切片
    ``render_one``（2026-11-12 words 接线）；None/缺路径回退段内插值。

    状态聚合（需求 6）：
    * 全部成功且无警告 → ``completed``；
    * 全部成功且有普通警告 → ``completed_with_warnings``；
    * 存在真实技术故障 → ``partial_succeeded``（成功文件保留）；
    * 全部切片都技术故障 → ``technical_failed``。
    """
    clips = list(plan_record.get("clips") or [])
    plan_warnings = [str(w) for w in (plan_record.get("warnings") or [])]
    render = render_one_fn or render_one
    refinement_clips = (refinement or {}).get("clips") or {}
    entries: list[dict[str, Any]] = []
    for number, clip in enumerate(clips, start=1):
        clip_id = str(clip.get("clip_id") or f"clip-{number}")
        if progress:
            progress(f"渲染切片 {number}/{len(clips)}：{clip.get('topic', clip_id)}")
        out_dir = out_root / "clips" / clip_id
        try:
            # words 仅在有值时透传：兼容测试注入的旧签名 render_one_fn 替身
            extra = {"words": list(words)} if words else {}
            entry = render(clip, source=source, out_dir=out_dir,
                               corrected_segments=corrected_segments,
                               transcript_ref=transcript_ref,
                               rule_snapshot_id=rule_snapshot_id,
                               refined_entry=refinement_clips.get(clip_id), **extra)
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
