#!/usr/bin/env python3
"""字幕新旧 A/B 对比（词级对齐 + 短 cue 纪律）离线演示。

对指定任务目录下已发布的成片，**完整复刻生产字幕链路**（精修稿替换
``_apply_refined_segments`` → 净化 sub_ranges 虚拟时间轴平移
``_shift_segments_for_ranges`` → cue 合成 ``build_srt``），分别按
「旧行为（段内线性插值 + 无限补缝隙）」与「新行为（词级时间对齐 +
长停顿字幕消失 + 显示时长上限 SUBTITLE_MAX_CUE_SECONDS，默认 12 秒，
可配）」重生成 SRT，输出统计到对比目录——不改任何生产文件，供人工核验
后决定是否进渲染管线。

词级时间（2026-11-12）：``build_srt(words=...)`` 约定收「秒」（内部自行
×1000 → ms），调用方不得预换算（双重 ×1000 会让词时间漂出窗口、静默
回退插值）。words 与分段使用同一 range 映射平移到虚拟时间轴，跨界词按
保留区间裁剪，保证词中点与平移后分段窗口一致。

用法（backend 目录下）::

    python scripts/subtitle_ab_demo.py --task <32位job或run id> \
        --transcript-v2 <transcripts/<source>/v2.json> --out <对比输出目录>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys_root = Path(__file__).resolve().parent.parent
sys_path = str(sys_root)
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from src import clip_render  # noqa: E402

_SRT_TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def _parse_srt(text: str) -> list[dict]:
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 2:
            continue
        m = _SRT_TS.match(lines[1].strip())
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        cues.append({"start": (g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000),
                     "end": (g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000),
                     "text": " ".join(lines[2:])})
    return cues


def _stats(cues: list[dict]) -> dict:
    durs = [c["end"] - c["start"] for c in cues]
    return {"cue_count": len(cues),
            "avg_s": round(sum(durs) / len(durs), 2) if durs else 0.0,
            "max_s": round(max(durs), 2) if durs else 0.0,
            "over_6s": sum(1 for d in durs if d > 6)}


def _shift_words_for_ranges(words_ms: list[dict[str, float]],
                            ranges: list[tuple[int, int]]) -> list[dict[str, float]]:
    """词级时间与 ``_shift_segments_for_ranges`` 同映射平移到虚拟时间轴。

    跨界词按保留区间裁剪（保留与区间交集），保证词中点落在平移后的
    分段窗口内、词对齐不被边界词缺失拉偏。"""
    out: list[dict[str, float]] = []
    offset = 0
    for (s, e) in ranges:
        for w in words_ms:
            ws, we = float(w["start"]), float(w["end"])
            if we <= s or ws >= e:
                continue
            c0, c1 = max(ws, s), min(we, e)
            out.append({"text": str(w.get("text") or ""),
                        "start": c0 - s + offset, "end": c1 - s + offset})
        offset += e - s
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--transcript-v2", required=True)
    ap.add_argument("--out", default="/domain/subtitle-ab-demo")
    args = ap.parse_args()

    tasks_root = Path("/domain/supoclip-medical-workbench/tasks")
    run_dir = tasks_root / "output" / args.task
    plan = json.loads((tasks_root / "plans" / args.task /
                       "clip_plan.json").read_text(encoding="utf-8"))
    refine = json.loads((tasks_root / "plans" / args.task /
                         "clip_refinement.json").read_text(encoding="utf-8"))
    v2 = json.loads(Path(args.transcript_v2).read_text(encoding="utf-8"))
    # 纠错转写分段（asr_segments = 医学纠错后正文，含 segment_id/start_ms/end_ms）
    corrected_segments = [
        {"segment_id": str(s.get("segment_id") or ""),
         "start_ms": int(s.get("start_ms") or round(float(s["start"]) * 1000)),
         "end_ms": int(s.get("end_ms") or round(float(s["end"]) * 1000)),
         "text": str(s.get("text") or "").strip()}
        for s in (v2.get("asr_segments") or [])]
    # 词级时间：v2 存绝对秒 → 内部统一 ms 计算，调用 build_srt 时再转回秒
    words_ms = [{"text": w["text"], "start": float(w["start"]) * 1000,
                 "end": float(w["end"]) * 1000} for w in (v2.get("words") or [])]

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    report = []
    for clip in plan.get("clips") or []:
        clip_id = clip["clip_id"]
        refined_entry = (refine.get("clips") or {}).get(clip_id)
        burn_segments, _replaced = clip_render._apply_refined_segments(
            corrected_segments, refined_entry)
        start_ms, end_ms = int(clip["start_ms"]), int(clip["end_ms"])
        # 与生产渲染同一套净化重组：sub_ranges → 虚拟时间轴；words 同映射平移
        ranges = clip_render._purify_ranges(clip, start_ms, end_ms)
        if ranges:
            burn_segments, dur_ms = clip_render._shift_segments_for_ranges(
                burn_segments, ranges)
            srt_start, srt_end = 0, dur_ms
            words_v_ms = _shift_words_for_ranges(words_ms, ranges)
        else:
            srt_start, srt_end = start_ms, end_ms
            words_v_ms = words_ms
        words_s = [{"text": w["text"], "start": w["start"] / 1000.0,
                    "end": w["end"] / 1000.0} for w in words_v_ms]
        new_srt, _used, warns = clip_render.build_srt(
            burn_segments, srt_start, srt_end, words=words_s)
        d = out_root / clip_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "new.srt").write_text(new_srt, encoding="utf-8")
        old_path = run_dir / "clips" / clip_id / "subtitles.srt"
        if old_path.is_file():
            old_text = old_path.read_text(encoding="utf-8")
            (d / "old.srt").write_text(old_text, encoding="utf-8")
            old_cues = _parse_srt(old_text)
        else:
            old_cues = []
        new_cues = _parse_srt(new_srt)
        report.append({"clip_id": clip_id, "topic": clip.get("topic"),
                       "old": _stats(old_cues), "new": _stats(new_cues),
                       "new_warnings": len(warns),
                       "virtual_timeline": bool(ranges)})
        (d / "stats.json").write_text(json.dumps(
            {"old": _stats(old_cues), "new": _stats(new_cues),
             "new_warnings": warns, "virtual_timeline": bool(ranges)},
            ensure_ascii=False, indent=1), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"对比文件输出: {out_root}")


if __name__ == "__main__":
    main()
