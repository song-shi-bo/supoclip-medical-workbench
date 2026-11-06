"""对某个时间线项目的当前纠错转写跑说话人自动标注（可断点续跑）。

用法（限流中断后直接重跑同一命令，会复用已标窗口）：
    STUDIO_TASKS_ROOT=/domain/supoclip-medical-workbench/tasks \
      backend/.venv/bin/python backend/scripts/run_speaker_labels.py timeline_a01488a3894a

等价 API：POST /api/studio/transcripts/speaker-labels {"object_key": ...}
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 未显式指定时默认仓库根 tasks（与服务端共用同一账目目录）。
_REPO_TASKS = Path(__file__).resolve().parent.parent.parent / "tasks"
if not os.environ.get("STUDIO_TASKS_ROOT") and _REPO_TASKS.is_dir():
    os.environ["STUDIO_TASKS_ROOT"] = str(_REPO_TASKS)

from src import speaker_labeling as sl  # noqa: E402
from src.studio_transcript_store import (  # noqa: E402
    find_latest_by_project, load_corrected_transcript,
    source_id_for, transcripts_root,
)


def main(project_id: str) -> int:
    credential = find_latest_by_project(project_id)
    if not credential:
        print("E: 找不到项目转写凭据")
        return 1
    object_key = credential["object_key"]
    document = load_corrected_transcript(
        object_key,
        transcript_id=credential["transcript_id"],
        transcript_version=credential["transcript_version"],
    )
    if not document or not document.get("asr_segments"):
        print("E: 纠错转写不可读")
        return 1
    segments = document["asr_segments"]
    print(f"源片={object_key}")
    print(f"转写={document.get('transcript_id')} v{document.get('transcript_version')}"
          f" 段数={len(segments)}")
    resume = sl.load_labels(transcripts_root(), source_id_for(object_key))
    if resume:
        known = sum(1 for v in (resume.get("labels") or {}).values()
                    if v in ("医生", "患者", "家属"))
        print(f"续跑：复用上次 overlay（已标 {known} 段）")
    record = sl.label_transcript(
        segments,
        transcript_id=str(document.get("transcript_id") or ""),
        transcript_version=document.get("transcript_version"),
        note=f"script:{project_id}",
        resume_from=resume,
    )
    path = sl.save_labels(transcripts_root(), source_id_for(object_key), record)
    print(f"overlay={path}")
    print(json.dumps({k: record[k] for k in
                      ("segment_count", "stats", "llm_error", "model",
                       "windows", "duration_seconds")}, ensure_ascii=False, indent=2))
    known = sum(1 for v in record["labels"].values())
    quota = any("429" in w or "SetLimitExceeded" in w
                for w in record["windows"].get("warnings") or [])
    if known == len(segments):
        print(f"完成：{known}/{len(segments)} 段已标注")
        return 0
    print(f"未完成：{known}/{len(segments)} 段已标注"
          + ("（模型账户限额，恢复后重跑本命令续跑）" if quota else "（见 warnings）"))
    return 2


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    raise SystemExit(main(sys.argv[1]))
