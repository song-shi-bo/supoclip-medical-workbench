#!/usr/bin/env python3
"""一次性维护脚本（2026-11-12，用户已授权）：字幕词级对齐重烧。

对项目 timeline_a01488a3894a 的 5 条已发布成片（run c6cd115b...）做
「只重渲字幕」替换：复用已验证的分析/计划/精修缓存（播种到新 run 名下，
运行时 worker 按同一 provenance 路径校验恢复，绝不重调模型/重规划），
只有渲染层以 words-align/1 指纹重跑（词级对齐烧录）。

不走 HTTP 会话（无用户凭据）；任务行按 create_job 的 DB 效果等价插入，
审计经 register_created_object 的 worker 无主体 owner 派生模式登记。
"""
import asyncio
import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

BASE = Path("/domain/supoclip-medical-workbench/backend")
os.chdir(BASE)
sys.path.insert(0, str(BASE))

BASE = Path("/domain/supoclip-medical-workbench/backend")
os.chdir(BASE)
for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    k, v = k.strip(), v.strip().strip('"').strip("'")
    if k and v and k not in os.environ:
        os.environ[k] = v
os.environ.setdefault("STUDIO_TASKS_ROOT", "/domain/supoclip-medical-workbench/tasks")

PROJECT = "timeline_a01488a3894a"
OLD_RUN = "c6cd115b51e348e89ec6aa8a13f9e583"
OBJECT_KEY = ("local:/domain/supoclip-medical-workbench/backend/temp/"
              "studio-uploads/0a320b542d1c427288506a271dbfd70b.mp4")
TRANSCRIPT_ID = "tr-2070f41f051ac5b2"
TRANSCRIPT_VERSION = 2
NEW_RUN = uuid.uuid5(uuid.NAMESPACE_URL, "subtitle-reburn:" + OLD_RUN).hex

config = {"job_kind": "auto_edit", "replacement_mode": "delete_old_after_success",
          "reuse_transcript": True, "force_new_run": True,
          "transcript_id": TRANSCRIPT_ID, "transcript_version": TRANSCRIPT_VERSION,
          "idempotency_key": str(uuid.uuid5(uuid.NAMESPACE_URL, "subtitle-reburn-key:" + OLD_RUN))}
clean = {k: v for k, v in config.items() if not k.startswith("_")}
fingerprint = hashlib.sha256(json.dumps(
    {"project_id": PROJECT, "object_key": OBJECT_KEY, "object_size": None, "config": clean},
    sort_keys=True, ensure_ascii=False).encode()).hexdigest()
row_config = {**clean, "_lifecycle": {
    "creation_fingerprint": fingerprint, "generation": 0, "render_generation": 0,
    "stage_sources": {k: "unknown" for k in ("correction", "analysis", "planning")},
    "operations": {}, "attempt_history": []}}

TASKS = Path(os.environ["STUDIO_TASKS_ROOT"])
SEEDS = [(TASKS / "analysis" / f"{OLD_RUN}.json", TASKS / "analysis" / f"{NEW_RUN}.json"),
         (TASKS / "plans" / OLD_RUN / "clip_plan.json",
          TASKS / "plans" / NEW_RUN / "clip_plan.json"),
         (TASKS / "plans" / OLD_RUN / "clip_refinement.json",
          TASKS / "plans" / NEW_RUN / "clip_refinement.json")]


async def main() -> None:
    from sqlalchemy import text
    from src import studio_job_store as store
    from src import studio_access

    maker = store.get_session_maker()
    async with maker() as session:
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:id,0))"),
                              {"id": NEW_RUN})
        found = await session.execute(text("SELECT id FROM studio_jobs WHERE id=:id"), {"id": NEW_RUN})
        if found.mappings().first() is not None:
            print(f"run {NEW_RUN} 已存在（幂等），跳过创建")
        else:
            await session.execute(text(
                "INSERT INTO studio_jobs(id, project_id, object_key, object_size, config) "
                "VALUES(:id, :project_id, :object_key, :object_size, CAST(:config AS JSONB))"),
                {"id": NEW_RUN, "project_id": PROJECT, "object_key": OBJECT_KEY,
                 "object_size": None, "config": json.dumps(row_config, ensure_ascii=False)})
            await session.commit()
            # 审计登记：worker 无主体模式（owner 从项目台账派生），不伪造用户身份
            studio_access.register_created_object("job", NEW_RUN, PROJECT)
            studio_access.register_created_object("task", NEW_RUN, PROJECT)
            print(f"job row inserted: {NEW_RUN}")
    ok = await store.mark_reedit_requested(NEW_RUN)
    print("reedit_requested:", ok)

    for src, dst in SEEDS:
        assert Path(src).is_file(), f"种子缺失: {src}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_file():
            print(f"seed exists (keep): {dst.name}")
            continue
        shutil.copy2(src, dst)
        print(f"seeded: {dst.relative_to(TASKS)}")

    row = await store.get_job(NEW_RUN) if hasattr(store, "get_job") else None
    print("job status:", (row or {}).get("status") if isinstance(row, dict) else "see DB")
    print(f"NEW_RUN={NEW_RUN}")


asyncio.run(main())
