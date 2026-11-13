#!/usr/bin/env python3
"""一次性维护脚本（2026-11-12，用户已授权「做接线，只重新渲染字幕」）。

对 run c6cd115b 的 5 条已发布成片做字幕重烧（同 plan / 同 sub_ranges /
同源片 / 同编码参数，只有字幕层变化）：
1. 备份已发布副本（可回滚）；
2. clip_render.render_plan(words=...) 原位重渲（指纹 words-align/1 自动失效）；
3. 逐条校验：可解码、时长与计划一致、words 留痕、SRT 与 A/B 期望一致；
4. 覆盖已发布副本（studio-artifacts attempt 目录，原子替换）；
5. clips.json published 的 size_bytes/sha256 刷新为真实新值（project lock）。
不重建任务、不调 LLM、不激活、不清扫；artifact 表行按系统约定保持首次登记。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path("/domain/supoclip-medical-workbench/backend")
os.chdir(BASE)
sys.path.insert(0, str(BASE))
for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    k, v = k.strip(), v.strip().strip('"').strip("'")
    if k and v and k not in os.environ:
        os.environ[k] = v
os.environ.setdefault("STUDIO_TASKS_ROOT", "/domain/supoclip-medical-workbench/tasks")

TASKS = Path(os.environ["STUDIO_TASKS_ROOT"])
RUN = "c6cd115b51e348e89ec6aa8a13f9e583"
PROJECT = "timeline_a01488a3894a"
SOURCE = Path("/domain/supoclip-medical-workbench/backend/temp/"
              "studio-uploads/0a320b542d1c427288506a271dbfd70b.mp4")
TRANSCRIPT_V2 = TASKS / "transcripts" / "2070f41f051ac5b2" / "v2.json"
AB_DIR = Path("/domain/subtitle-ab-demo")
STAMP = time.strftime("%Y%m%d-%H%M%S")

out_root = TASKS / "output" / RUN
attempt = BASE / "studio-artifacts" / RUN / "attempt-52985cb9497e"
backup_dir = out_root / f".pre-subtitle-reburn-{STAMP}"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ffprobe_duration(p: Path) -> float | None:
    try:
        out = subprocess.run(
            [str(Path(sys.executable).parent / "ffprobe") if False else "ffprobe",
             "-v", "error", "-show_entries", "format=duration", "-of",
             "default=nw=1:nk=1", str(p)], capture_output=True, text=True, timeout=60)
        return float(out.stdout.strip())
    except Exception:
        return None


def main() -> int:
    from src import clip_render

    transcript = json.loads(TRANSCRIPT_V2.read_text(encoding="utf-8"))
    plan = json.loads((TASKS / "plans" / RUN / "clip_plan.json").read_text(encoding="utf-8"))
    refinement = json.loads((TASKS / "plans" / RUN / "clip_refinement.json").read_text(encoding="utf-8"))
    words = list(transcript.get("words") or []) or None
    assert words, "v2 转写缺 words，拒绝继续（会静默回退插值）"

    # ---- 1) 备份已发布副本 ----
    backup_dir.mkdir(parents=True, exist_ok=False)
    for clip_dir in sorted(attempt.glob("clip-*")):
        dst = backup_dir / clip_dir.name
        shutil.copytree(clip_dir, dst)
    print(f"[backup] published copies -> {backup_dir.name}")

    # ---- 2) 原位重渲（同 run 输出目录） ----
    t0 = time.time()
    aggregate = clip_render.render_plan(
        plan, source=SOURCE, out_root=out_root,
        corrected_segments=list(transcript.get("asr_segments") or []),
        transcript_ref={"transcript_id": transcript.get("transcript_id"),
                        "transcript_version": transcript.get("transcript_version")},
        rule_snapshot_id=plan.get("rule_snapshot_id"),
        refinement=refinement,
        words=words,
    )
    print(f"[render] job_status={aggregate['job_status']} counts={aggregate['counts']} "
          f"elapsed={time.time()-t0:.0f}s")
    for e in aggregate["entries"]:
        print(f"  {e['clip_id']}: {e['status']}"
              + (f" warn={e.get('render_warnings')}" if e.get("render_warnings") else ""))
    if aggregate["job_status"] == "technical_failed":
        print("[abort] 全部渲染技术失败，已发布副本未触碰", flush=True)
        return 2

    # ---- 3) 逐条校验 ----
    ok = True
    for clip in plan.get("clips") or []:
        cid = clip["clip_id"]
        base = out_root / "clips" / cid
        final, warn = base / "clip_final.mp4", base / "warnings.json"
        entry = next((e for e in aggregate["entries"] if e["clip_id"] == cid), {})
        if entry.get("status") not in {"rendered", "skipped_playable"}:
            print(f"[verify] {cid}: 渲染态异常 {entry.get('status')} / {entry.get('technical_error')}")
            ok = False
            continue
        playable, why = clip_render._ffprobe_playable(final)
        dur = ffprobe_duration(final)
        # 时长基准：与「旧发布副本」一致（净化 concat 后的实际时长 ≠ 计划包络
        # end-start；包络只是规划窗口）。备份缺失时退回 sub_ranges 区间总和。
        old_final = backup_dir / cid / "clip_final.mp4"
        if old_final.is_file():
            expect_ms = (ffprobe_duration(old_final) or 0.0) * 1000.0
        else:
            ranges = clip.get("sub_ranges") or []
            expect_ms = sum(int(r.get("end_ms") or 0) - int(r.get("start_ms") or 0)
                            for r in ranges) or (int(clip.get("end_ms") or 0)
                                                 - int(clip.get("start_ms") or 0))
        doc = json.loads(warn.read_text(encoding="utf-8"))
        prov_words = (doc.get("subtitle_provenance") or {}).get("words") or {}
        fp_version_ok = "words-align/1" in str(doc.get("render_fingerprint")) or True  # 指纹是哈希，版本看下面
        # 指纹与「同输入无 words」指纹不同（words-align/1 生效的间接证据 = words.used）
        ab_new = AB_DIR / cid / "new.srt"
        srt_same = ab_new.is_file() and (base / "subtitles.srt").read_text(encoding="utf-8") == \
            ab_new.read_text(encoding="utf-8")
        checks = {
            "playable": playable,
            "duration_ok": dur is not None and abs(dur * 1000 - expect_ms) < 80,
            "words_used": prov_words.get("used") is True,
            "words_count>0": (prov_words.get("count") or 0) > 0,
            "subtitle_source": (doc.get("subtitle_provenance") or {}).get("subtitle_source") == "clip_refinement",
            "srt==AB": srt_same,
        }
        bad = [k for k, v in checks.items() if not v]
        print(f"[verify] {cid}: dur={dur and round(dur,2)}s expect={expect_ms/1000:.2f}s "
              f"words={prov_words} " + ("OK" if not bad else f"FAIL {bad}"))
        if bad:
            ok = False
    if not ok:
        print("[abort] 校验未全过：已发布副本未触碰（渲染产物保留在 output 供排查）", flush=True)
        return 3

    # ---- 4) 覆盖已发布副本（原子替换） ----
    published_rows = []
    for clip in plan.get("clips") or []:
        cid = clip["clip_id"]
        base = out_root / "clips" / cid
        dst_dir = attempt / cid
        final = dst_dir / "clip_final.mp4"
        for name in ("clip_final.mp4", "warnings.json", "subtitles.srt"):
            src, dst = base / name, dst_dir / name
            tmp = dst.with_suffix(dst.suffix + f".new-{STAMP}")
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
        published_rows.append({"clip_id": cid, "sha": sha256_file(final),
                               "size": final.stat().st_size,
                               "object_key": f"local:{dst_dir / 'clip_final.mp4'}"})
        print(f"[publish] {cid}: sha={published_rows[-1]['sha'][:12]} size={published_rows[-1]['size']}")

    # ---- 5) clips.json published 真实哈希刷新（project lock） ----
    from src import studio_project_store as store
    with store.project_lock(PROJECT):
        manifest = store.load_manifest(PROJECT)
        for row in published_rows:
            entry = (manifest.get("clips") or {}).get(row["clip_id"])
            if entry is None:
                print(f"[manifest] 警告：{row['clip_id']} 不在台账，跳过")
                continue
            pub = entry.get("published") or {}
            assert pub.get("object_key") == row["object_key"], f"{row['clip_id']} 发布路径漂移，拒绝改写"
            pub["sha256"] = row["sha"]
            pub["size_bytes"] = row["size"]
            entry["published"] = pub
        store.save_manifest(PROJECT, manifest)
    print("[manifest] published sha256/size 已刷新")

    # ---- 6) 终验：发布副本与台账一致、可播放 ----
    for row in published_rows:
        p = Path(str(row["object_key"]).removeprefix("local:"))
        assert sha256_file(p) == row["sha"], f"{row['clip_id']} 发布副本哈希不一致"
    print("[done] 5 条成片字幕重烧完成（词级对齐），旧副本备份于", backup_dir.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
