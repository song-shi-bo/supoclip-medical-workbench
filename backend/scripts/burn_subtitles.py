#!/usr/bin/env python3
"""字幕硬烧录运维入口（2026-10-29 固化，规范见 docs/subtitle-burn-convention.md）。

子命令（在 backend/ 目录下用 .venv/bin/python 运行）:

  ass <in.srt> <out.ass>
      规范 SRT → 固化样式 ASS（白字/纯黑 2px 描边/无阴影，PlayRes 1080x1920）。

  burn <base.mp4> <in.ass> <out.mp4>
      把 ASS 烧进底片（底片 = 无字幕的干净渲染，勿用已烧字视频，防二次压字）。

  regen-srt <transcript_v2.json> <start_ms> <end_ms> <out.srt>
      按 cue 合成层（subtitle_cueing）从转写重出 SRT。

  meta <clip_dir>
      同步 clip 元数据：cover 账本 intro sha（防重复烧片头）+ warnings
      （srt_sha256 / subtitle_mode=burned / subtitle_hard_burn 样式备注）。

  auto <clip_dir> <base.mp4> <transcript_v2.json> <out.mp4>
      一条龙：regen-srt（读 clip_plan.json 起止）→ ass → burn → meta。

烧录样式与切分参数（SUBTITLE_* 环境变量）说明见 docs/subtitle-burn-convention.md。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from src import clip_render as cr            # noqa: E402
from src import subtitle_cueing as sc        # noqa: E402

FFMPEG = os.getenv("BURN_FFMPEG") or shutil.which("ffmpeg") or "/domain/dsh-home/bin/ffmpeg"
FONTSDIR = os.getenv("BURN_FONTSDIR", "/usr/share/fonts/truetype/wqy")


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def cmd_ass(srt: str, out: str) -> None:
    Path(out).write_text(sc.srt_to_styled_ass(Path(srt).read_text(encoding="utf-8")),
                         encoding="utf-8")
    print(f"ass written: {out}")


def cmd_burn(base: str, ass: str, out: str) -> None:
    vf = f"subtitles={ass}:fontsdir={FONTSDIR}"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", base, "-vf", vf,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                    "-c:a", "copy", "-movflags", "+faststart", out], check=True)
    print(f"burned: {out}")


def cmd_regen_srt(transcript: str, start_ms: int, end_ms: int, out: str) -> None:
    doc = json.loads(Path(transcript).read_text())
    segs = doc.get("asr_segments") or doc.get("segments") or []
    srt_text, used, warns = cr.build_srt(segs, int(start_ms), int(end_ms))
    Path(out).write_text(srt_text, encoding="utf-8")
    print(f"srt written: {out} (segments used: {len(used)}, warnings: {len(warns)})")
    for w in warns:
        print(f"  warn: {w}")


def cmd_meta(clip_dir: str) -> None:
    d = Path(clip_dir)
    final = d / "clip_final.mp4"
    led_p = d / "cover_realframe_v2.json"
    if led_p.is_file():
        led = json.loads(led_p.read_text())
        e = (led.get("entries") or {}).get(str(led.get("current_revision")), {})
        if e.get("intro_clip_final_sha256") and final.is_file():
            e["intro_clip_final_sha256"] = _sha256(final)
            led_p.write_text(json.dumps(led, ensure_ascii=False, indent=2))
            print(f"ledger intro sha resynced: {led_p.name}")
    w_p = d / "warnings.json"
    srt = d / "subtitles.srt"
    if w_p.is_file() and srt.is_file():
        w = json.loads(w_p.read_text())
        w["subtitle_mode"] = "burned"
        w["srt_sha256"] = hashlib.sha256(srt.read_bytes()).hexdigest()
        w["subtitle_hard_burn"] = {
            "at": "2026-10-29", "cue_logic": "subtitle_cueing v2",
            "style": ("ASS PlayRes 1080x1920: Primary=white, OutlineColour=black, "
                      "BorderStyle=1, Outline=2, Shadow=0"),
            "font": sc.BURN_FONT_NAME, "max_line_chars": 16,
            "font_size": sc.BURN_FONT_SIZE_PX, "ass": str(d / "subtitles.ass")}
        w_p.write_text(json.dumps(w, ensure_ascii=False, indent=2))
        print(f"warnings synced: {w_p.name}")


def cmd_auto(clip_dir: str, base: str, transcript: str, out: str) -> None:
    d = Path(clip_dir)
    plan = json.loads((d / "clip_plan.json").read_text())
    srt = d / "subtitles.srt"
    cmd_regen_srt(transcript, int(plan["start_ms"]), int(plan["end_ms"]), str(srt))
    ass = d / "subtitles.ass"
    cmd_ass(str(srt), str(ass))
    cmd_burn(base, str(ass), out)
    shutil.copy2(out, d / "clip_final.mp4")
    cmd_meta(str(d))
    print(f"clip_final.mp4 updated in {d}")


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__)
        sys.exit(2)
    cmd, rest = argv[0], argv[1:]
    if cmd == "ass" and len(rest) == 2:
        cmd_ass(*rest)
    elif cmd == "burn" and len(rest) == 3:
        cmd_burn(*rest)
    elif cmd == "regen-srt" and len(rest) == 4:
        cmd_regen_srt(rest[0], int(rest[1]), int(rest[2]), rest[3])
    elif cmd == "meta" and len(rest) == 1:
        cmd_meta(*rest)
    elif cmd == "auto" and len(rest) == 4:
        cmd_auto(*rest)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
