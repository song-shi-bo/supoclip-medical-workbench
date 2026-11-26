"""对稿成片：单成片渲染（P1b）。

把 conform_edl 的 keep_ranges 映射为 clip_render 的伪 clip（sub_ranges 多区间），
完整复用既有渲染规范栈：横转竖、词级对齐字幕烧录（styled ASS + readability +
emphasis + subtitle_qc）、reframe 钩子、渲染指纹缓存（EDL 变化自动重渲）。

渲染后音频后处理（单遍、-c:v copy）：
- 剪切点微淡化（afade in/out 各 fade_ms/2，时长不变）消除拼接咔哒声；
- `CONFORM_LOUDNORM=on` 时追加单遍 loudnorm + 48kHz 重采样。
渲染后审计修正：warnings.json.subtitle_provenance.subtitle_source = "script_align"
（烧录文本来自对齐 matched 干净句，而非纠错转写全文）。

VERSION: conform-render/v1
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .domains.render import clip_render
from .script_align import SCRIPT_ALIGN_VERSION

CONFORM_RENDER_VERSION = "conform-render/v1"


def load_edl(match_record: dict[str, Any]) -> dict[str, Any]:
    path = match_record.get("conform_edl_path")
    if not path:
        raise FileNotFoundError("匹配报告缺少 conform_edl_path")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_pseudo_clip(edl: dict[str, Any]) -> dict[str, Any]:
    """EDL keep_ranges → clip_render 伪 clip（sub_ranges 多区间）。"""
    start, end = (edl.get("source_span_ms") or {}).get("start", 0), \
        (edl.get("source_span_ms") or {}).get("end", 0)
    keep = [[int(s), int(e)] for s, e in (edl.get("keep_ranges_ms") or [])]
    clip: dict[str, Any] = {
        "clip_id": "conform",
        "start_ms": int(start),
        "end_ms": int(end),
        "warnings": ["对稿成片（script_mode）"],
    }
    # 单区间等价整段 → sub_ranges 置空走单区间老路径（_purify_ranges 的 noop 判定一致）
    if len(keep) == 1 and keep[0][0] <= int(start) and keep[0][1] >= int(end):
        return clip
    clip["sub_ranges"] = [{"start_ms": s, "end_ms": e} for s, e in keep]
    return clip


def build_burn_segments(match_record: dict[str, Any]) -> list[dict[str, Any]]:
    """烧录文本 = 对齐 matched 干净句（语气词已除）；missing/extra 不上字幕。"""
    burn: list[dict[str, Any]] = []
    for s in match_record.get("sentences") or []:
        if s.get("status") != "matched" or s.get("start_ms") is None:
            continue
        burn.append({
            "segment_id": f"align-{s.get('index')}",
            "start_ms": int(s["start_ms"]),
            "end_ms": int(s["end_ms"]),
            "corrected_text": str(s.get("text") or ""),
        })
    return burn


def internal_boundaries(keep_ranges_ms: list[list[int]]) -> list[int]:
    """虚拟时间轴上的内部拼接点（毫秒）：首个/末个保留区间端点不算。"""
    boundaries: list[int] = []
    cursor = 0
    for s, e in keep_ranges_ms:
        cursor += e - s
        boundaries.append(cursor)
    return boundaries[:-1]


def build_audio_filter_chain(boundaries_ms: list[int], fade_ms: int,
                             loudnorm: bool) -> str | None:
    """拼接点微淡化（时长不变）+ 可选响度归一化。无任何滤波时返回 None。"""
    filters: list[str] = []
    if fade_ms > 0:
        seconds = fade_ms / 1000.0
        for b in boundaries_ms:
            t = b / 1000.0
            fade_out_start = max(t - seconds, 0.0)
            if fade_out_start > 0:
                filters.append(f"afade=t=out:st={fade_out_start:.4f}:d={seconds:.4f}")
            filters.append(f"afade=t=in:st={t:.4f}:d={seconds:.4f}")
    if loudnorm:
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
        filters.append("aresample=48000")
    return ",".join(filters) if filters else None


def apply_audio_post(video_path: Path, boundaries_ms: list[int], *,
                     fade_ms: int, loudnorm: bool, run_ffmpeg=None) -> bool:
    """音频后处理（-c:v copy 不重编码画面）。无滤波需求 → 原样返回 False。"""
    chain = build_audio_filter_chain(boundaries_ms, fade_ms, loudnorm)
    if not chain:
        return False
    runner = run_ffmpeg or clip_render._run_ffmpeg
    out_path = video_path.with_name("clip_final.audio-post.mp4")
    runner(["-y", "-i", str(video_path), "-filter:a", chain,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", str(out_path)])
    os.replace(out_path, video_path)
    return True


def fix_subtitle_provenance(warnings_path: Path) -> None:
    """审计修正：烧录文本来自对齐 matched 干净句（非纠错转写全文）。"""
    try:
        doc = json.loads(warnings_path.read_text(encoding="utf-8"))
        provenance = doc.get("subtitle_provenance")
        if isinstance(provenance, dict):
            provenance["subtitle_source"] = "script_align"
            provenance["align_version"] = SCRIPT_ALIGN_VERSION
        else:
            doc["subtitle_provenance"] = {"subtitle_source": "script_align",
                                          "align_version": SCRIPT_ALIGN_VERSION}
        warnings_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    except Exception:
        pass  # 审计元信息修正失败不影响成片


def render_conform_final(job: dict, job_id: str, transcript: dict,
                         match_record: dict[str, Any]) -> dict[str, Any]:
    """对稿模式单成片渲染。返回 {video_path, output_duration_ms, cut_ratio, ...}。

    技术故障抛 clip_render.ClipRenderError（调用方按既有隔离契约处理）。
    """
    edl = load_edl(match_record)
    keep = [[int(s), int(e)] for s, e in (edl.get("keep_ranges_ms") or [])]
    if not keep:
        raise clip_render.ClipRenderError("EDL 无保留区间，无法渲染")

    from .studio_job_worker.pipeline import (  # 惰性导入避免循环
        _materialise_source_for_job, _base_dir,
    )
    base = os.getenv("STUDIO_TASKS_ROOT", str(_base_dir() / "tasks"))
    out_dir = Path(base) / "output" / _run_id_of(job, job_id) / "conform"
    source = _materialise_source_for_job(job["object_key"], job_id)

    clip = build_pseudo_clip(edl)
    config = job.get("config") or {}
    entry = clip_render._render_one_reserved(
        clip, source=Path(source), out_dir=out_dir,
        corrected_segments=build_burn_segments(match_record),
        transcript_ref={"transcript_id": config.get("transcript_id"),
                        "transcript_version": config.get("transcript_version")},
        rule_snapshot_id=None, refined_entry=None,
        words=(transcript.get("words") or []) or None)

    final_path = out_dir / "clip_final.mp4"
    boundaries = internal_boundaries(keep)
    faded = apply_audio_post(
        final_path, boundaries,
        fade_ms=int(os.getenv("CONFORM_CROSSFADE_MS", "10")),
        loudnorm=os.getenv("CONFORM_LOUDNORM", "off").lower() in ("1", "true", "on"))
    fix_subtitle_provenance(out_dir / "warnings.json")

    return {
        "version": CONFORM_RENDER_VERSION,
        "video_path": str(final_path),
        "entry_status": entry.get("status"),
        "output_duration_ms": edl.get("output_duration_ms"),
        "cut_ratio": edl.get("cut_ratio"),
        "audio_post_applied": faded,
        "keep_ranges_count": len(keep),
    }


def _run_id_of(job: dict, job_id: str) -> str:
    try:
        from .studio_job_worker.pipeline import _job_run_id
        return _job_run_id(job, job_id)
    except Exception:
        return job_id
