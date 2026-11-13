#!/usr/bin/env python3
"""医学背景注入 A/B 对比（2026-11-13 设计稿 §9 验证，用户已授权执行）。

对 melanoma 项目真实转写跑两次提示词A规划：MEDICAL_BG_INJECT=off（基线）与
=on（注入），对比：切片数/主题标题/边界、泄漏扫描告警、prompt_sha256 差异。
产物落 /tmp/ab_bg_{off,on}.json 供人工逐字段查看。需要 .env 的模型网关配置。
"""
import json
import os
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

RUN = "c6cd115b51e348e89ec6aa8a13f9e583"
TASKS = Path(os.environ["STUDIO_TASKS_ROOT"])


def main() -> None:
    from src import clip_medical_gate as gate
    from src import clip_planning as cp

    v2 = json.loads((TASKS / "transcripts" / "2070f41f051ac5b2" / "v2.json").read_text(encoding="utf-8"))
    segments = list(v2.get("asr_segments") or [])
    analysis = json.loads((TASKS / "analysis" / RUN / "analysis.json").read_text(encoding="utf-8")) \
        if (TASKS / "analysis" / RUN / "analysis.json").is_file() else \
        json.loads((TASKS / "analysis" / f"{RUN}.json").read_text(encoding="utf-8"))
    analysis_for_plan = dict(analysis)
    profile = gate.run_gate(segments, analysis_for_plan)
    if profile.get("enabled"):
        analysis_for_plan["medical_annotations"] = profile.get("unit_annotations") or []
    print(f"gate: enabled={profile.get('enabled')} units={len(analysis_for_plan.get('medical_annotations') or [])}")

    results = {}
    for mode, flag in (("off", "off"), ("on", "on")):
        os.environ["MEDICAL_BG_INJECT"] = flag
        t0 = time.time()
        record = cp.plan_clips(
            segments, analysis_for_plan, transcript_version=2,
            rule_snapshot_id=None, gate_profile=profile)
        elapsed = time.time() - t0
        clips = [{"topic": c.get("topic"), "title": c.get("title") or "",
                  "start": c.get("start_ms"), "end": c.get("end_ms"),
                  "hook": c.get("hook_first_line"),
                  "sub_ranges": len(c.get("sub_ranges") or []),
                  "duration": round((c.get("end_ms") or 0) / 1000.0, 1)}
                 for c in (record.get("clips") or [])]
        leak_warns = [w for w in (record.get("plan_warnings") or []) if "medical_background_leak" in w]
        results[mode] = {
            "status": record.get("plan_source"),
            "clip_count": len(clips),
            "clips": clips,
            "warnings": record.get("plan_warnings") or [],
            "leak_warnings": leak_warns,
            "prompt_sha": ((record.get("actual_calls") or [{}])[0].get("prompt_sha256") or "")[:16],
            "elapsed_s": round(elapsed),
        }
        Path(f"/tmp/ab_bg_{mode}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[{mode}] source={record.get('plan_source')} clips={len(clips)} "
              f"leaks={len(leak_warns)} prompt_sha={results[mode]['prompt_sha']} "
              f"elapsed={round(elapsed)}s")

    print("\n=== 对比摘要 ===")
    print(f"prompt_sha 不同（注入改变提示词）: {results['off']['prompt_sha'] != results['on']['prompt_sha']}")
    print(f"切片数 off/on: {results['off']['clip_count']} / {results['on']['clip_count']}")
    print(f"泄漏告警 off/on: {len(results['off']['leak_warnings'])} / {len(results['on']['leak_warnings'])}")
    for i in range(max(results["off"]["clip_count"], results["on"]["clip_count"])):
        o = results["off"]["clips"][i]["topic"] if i < results["off"]["clip_count"] else "-"
        n = results["on"]["clips"][i]["topic"] if i < results["on"]["clip_count"] else "-"
        print(f"  片{i+1}: off[{str(o)[:34]}]  on[{str(n)[:34]}]")
    print("\n详细记录: /tmp/ab_bg_off.json, /tmp/ab_bg_on.json")


main()
