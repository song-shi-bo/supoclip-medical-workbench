"""删除清单覆盖范围修复的定向测试（2026-10-20）。

覆盖：plans / studio-jobs 工作目录 / studio-uploads 源片原件 / job-recovery /
PG studio_jobs 墓碑五类此前遗漏的残留；源片 owner/引用计数/硬链接/保留引用检查；
freed_bytes 精确对账；老清单（v1）retry_cleanup 升级；sync_from_tasks 永不回填已删项目。
全部使用 tmp 根与合成文件 + 注入 PG 钩子，不触碰真实项目与真实数据库。
"""

from __future__ import annotations

import json
import os

import pytest

from src import project_lifecycle as lifecycle
from src import studio_project_store as store
from src import studio_access as sa

PID = "timeline_cover01"
OTHER = "timeline_keep01"
TASK = "0123abcd" * 4          # PG job id 形状（32 hex）
UID = "deadbeef" * 4           # 上传原件 id 形状（32 hex）


def principal(subject="8"):
    import time
    return {"subject": subject, "slug": subject, "domain_id": sa.DOMAIN_ID,
            "expires_at_ms": int(time.time() * 1000) + 60000}


@pytest.fixture
def env(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    tasks = tmp_path / "tasks"
    uploads.mkdir()
    (tasks / "projects").mkdir(parents=True)
    monkeypatch.setattr(store, "TASKS_ROOT", tasks)
    acl_path = tmp_path / "access.sqlite3"
    acl = sa.AccessLedger(acl_path)
    acl.register("project", PID, principal("8"))
    tombstoned: list[tuple[str, str]] = []

    monkeypatch.setattr(lifecycle, "_PG_PROJECT_JOBS", lambda pid: [
        {"id": TASK, "object_key": f"local:/x/studio-uploads/{UID}.mp4",
         "object_size": 1000, "deleted_at_utc": None}] if pid == PID else [])
    monkeypatch.setattr(lifecycle, "_PG_REFERENCE_PROJECTS",
                        lambda uid, exclude: [])
    monkeypatch.setattr(lifecycle, "_PG_TOMBSTONE_JOBS",
                        lambda pid, via: tombstoned.append((pid, via)) or [TASK])
    return {"uploads": uploads, "tasks": tasks, "acl_path": acl_path, "acl": acl,
            "tombstoned": tombstoned}


def seed_project(env, *, shared_with_other: bool = False):
    """合成一个含全部残留类别的项目：plans/studio-jobs/上传原件(硬链接)/job-recovery/产物。"""
    uploads, tasks = env["uploads"], env["tasks"]
    up = uploads / "studio-uploads" / f"{UID}.mp4"
    up.parent.mkdir(parents=True, exist_ok=True)
    up.write_bytes(b"U" * 2048)
    if shared_with_other:
        other_raw = uploads / "studio-projects" / OTHER / "raw"
        other_raw.mkdir(parents=True, exist_ok=True)
        os.link(up, other_raw / f"{UID}.mp4")
        other_led = tasks / "projects" / OTHER / "clips.json"
        other_led.parent.mkdir(parents=True, exist_ok=True)
        other_led.write_text(json.dumps({"project_id": OTHER, "status": "active",
                                         "clips": {}, "clip_order": [],
                                         "source_video": None}), encoding="utf-8")
    # 本项目工程目录 raw 硬链接（自身链接，nlink≥2 视共享）
    raw = uploads / "studio-projects" / PID / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    os.link(up, raw / f"{UID}.mp4")
    (uploads / "studio-projects" / PID / "project.json").write_text(
        json.dumps({"id": PID, "source": {"name": "源.mp4"}}), encoding="utf-8")
    jobdir = uploads / "studio-jobs" / TASK
    jobdir.mkdir(parents=True, exist_ok=True)
    os.link(up, jobdir / "source.mp4")
    (jobdir / "job.json").write_bytes(b"J" * 128)
    plans = tasks / "plans" / TASK
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "clip_plan.json").write_bytes(b"P" * 256)
    jr = tasks / "job-recovery" / UID
    jr.mkdir(parents=True, exist_ok=True)
    (jr / "transcribe.json").write_bytes(b"R" * 96)
    completed = tasks / "completed" / f"autoedit_{TASK}.json"
    completed.parent.mkdir(parents=True, exist_ok=True)
    completed.write_text(json.dumps({"task_id": f"autoedit_{TASK}", "status": "completed",
                                     "bridge": {"project_id": PID}}), encoding="utf-8")
    out = tasks / "output" / f"autoedit_{TASK}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "clip.mp4").write_bytes(b"V" * 512)
    artifacts = Path_of_artifacts() / TASK
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "a.mp4").write_bytes(b"A" * 64)
    # 台账（active）
    store.save_manifest(PID, store.load_manifest(PID))


def Path_of_artifacts():
    from pathlib import Path
    return Path(__file__).resolve().parents[2] / "studio-artifacts"


def test_preview_covers_all_new_kinds(env):
    seed_project(env)
    preview = lifecycle.build_delete_preview(
        PID, uploads_root=env["uploads"], tasks_root=env["tasks"])
    kinds = {i["kind"] for i in preview["items"]}
    assert {"clip_plan", "studio_job_dir", "source_upload", "job_recovery",
            "pg_jobs_tombstone"} <= kinds
    up = next(i for i in preview["items"] if i["kind"] == "source_upload")
    assert "owner=pg-job:" in up["note"]          # owner 证据来自 PG object_key
    assert up["shared"] and up["reclaim_bytes"] == 0  # 硬链接源片不计返还
    tomb = next(i for i in preview["items"] if i["kind"] == "pg_jobs_tombstone")
    assert tomb["reclaim_bytes"] == 0             # 墓碑不计容量


def test_full_delete_cleans_all_kinds_with_exact_freed(env):
    seed_project(env)
    manifest = lifecycle.confirm_deletion(PID, confirmed_by="subject:8",
                                          uploads_root=env["uploads"],
                                          tasks_root=env["tasks"])
    result = lifecycle.run_deletion_task(PID, uploads_root=env["uploads"],
                                         tasks_root=env["tasks"],
                                         acl_path=env["acl_path"])
    assert result["status"] == "completed"
    # 全部残留归零
    u = env["uploads"]
    t = env["tasks"]
    assert not (t / "plans" / TASK).exists()
    assert not (u / "studio-jobs" / TASK).exists()
    assert not (u / "studio-uploads" / f"{UID}.mp4").exists()
    assert not (t / "job-recovery" / UID).exists()
    assert not (u / "studio-projects" / PID).exists()
    assert not (t / "output" / f"autoedit_{TASK}").exists()
    # PG 墓碑已打标
    assert env["tombstoned"] and env["tombstoned"][0][0] == PID
    # 释放字节精确：源片数据 2048B 在**最后一个链接**（上传原件）被摘除时才真正释放，
    # 恰好计入该项（nlink==1 时刻）；此前共享链接项计 0 —— 合计 == 实际释放。
    kinds = {i["kind"]: i for i in result["items"] if i["state"] == "done"}
    expected = (2048       # 源片数据本体（最后一个链接摘除时释放）
                + 256      # clip_plan
                + 128      # studio-jobs/job.json
                + 96       # job-recovery/transcribe.json
                + 512      # output/clip.mp4
                + 64)      # published artifact
    # ledger/manifest 项的 freed 在删除时计入（尺寸随内容）
    assert result["freed_bytes_total"] >= expected
    assert result["freed_bytes_total"] == sum(
        i.get("freed_bytes", 0) for i in result["items"] if i["state"] == "done")
    assert kinds["source_upload"]["freed_bytes"] == 2048   # 最后链接：数据此刻真实释放
    assert kinds["studio_job_dir"]["freed_bytes"] == 128   # 仅 job.json（source.mp4 共享不计）
    led = json.loads((t / "projects" / PID / "clips.json").read_text())
    assert led["status"] == "deleted"


def test_shared_source_kept_when_other_project_references(env):
    seed_project(env, shared_with_other=True)
    other_link = env["uploads"] / "studio-projects" / OTHER / "raw" / f"{UID}.mp4"
    before = other_link.read_bytes()
    manifest = lifecycle.confirm_deletion(PID, confirmed_by="subject:8",
                                          uploads_root=env["uploads"],
                                          tasks_root=env["tasks"])
    result = lifecycle.run_deletion_task(PID, uploads_root=env["uploads"],
                                         tasks_root=env["tasks"],
                                         acl_path=env["acl_path"])
    up = next(i for i in result["items"] if i["kind"] == "source_upload")
    jr = next(i for i in result["items"] if i["kind"] == "job_recovery")
    assert up["state"] == "kept" and up["freed_bytes"] == 0
    assert jr["state"] == "kept"
    assert "project-dir:timeline_keep01" in up["note"]
    assert (env["uploads"] / "studio-uploads" / f"{UID}.mp4").exists()  # 本体未动
    assert other_link.read_bytes() == before                            # 保留项目字节不变
    assert result["kept_items"] and any("studio-uploads" in p for p in result["kept_items"])


def test_retry_cleanup_upgrades_v1_manifest(env):
    seed_project(env)
    # 伪造 v1 老清单：只有 ledger 一项且 completed，项目已是墓碑。
    proj = env["tasks"] / "projects" / PID
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "clips.json").write_text(json.dumps(
        {"project_id": PID, "status": "deleted", "deleted_at_utc": "2026-10-20T00:00:00+00:00",
         "deletion_manifest": "deletion_manifest.json"}), encoding="utf-8")
    mpath = proj / "deletion_manifest.json"
    mpath.write_text(json.dumps({
        "schema_version": 1, "project_id": PID, "status": "completed",
        "requested_at_utc": "2026-10-20T00:00:00+00:00", "confirmed_by": "subject:8",
        "reclaim_bytes": 10, "shared_bytes": 0,
        "items": [{"kind": "ledger", "path": str(proj / "clips.json"), "bytes": 10,
                   "reclaim_bytes": 10, "shared_bytes": 0, "shared": False,
                   "nlink": None, "note": "", "state": "done", "error": None}]}),
        encoding="utf-8")
    result = lifecycle.retry_cleanup(PID, uploads_root=env["uploads"],
                                     tasks_root=env["tasks"], acl_path=env["acl_path"])
    assert result["status"] == "completed"
    kinds = {i["kind"] for i in result["items"]}
    assert {"clip_plan", "studio_job_dir", "source_upload", "job_recovery",
            "pg_jobs_tombstone"} <= kinds
    assert not (env["tasks"] / "plans" / TASK).exists()          # 残留归零
    assert not (env["uploads"] / "studio-uploads" / f"{UID}.mp4").exists()
    assert env["tombstoned"] and env["tombstoned"][-1][0] == PID
    assert result["freed_bytes_total"] > 0
    led = json.loads((proj / "clips.json").read_text())
    assert led["status"] == "deleted"                            # 墓碑保持
    # 非删除状态拒绝
    with pytest.raises(lifecycle.LifecycleError) as ei:
        lifecycle.retry_cleanup("timeline_nomanifest", uploads_root=env["uploads"],
                                tasks_root=env["tasks"])
    assert ei.value.code == "E_DELETE_MANIFEST_MISSING"


def test_sync_from_tasks_never_backfills_deleted(env, monkeypatch):
    store.save_manifest(PID, store.load_manifest(PID))
    # 任务 JSON + 回填视图就绪：若不设守卫，会把卡片/源片登记回来。
    completed = env["tasks"] / "completed" / f"autoedit_{TASK}.json"
    completed.parent.mkdir(parents=True, exist_ok=True)
    completed.write_text(json.dumps({"task_id": f"autoedit_{TASK}", "status": "completed",
                                     "bridge": {"project_id": PID},
                                     "raw_video_path": "media/x/src.mp4"}), encoding="utf-8")
    from src import studio_auto_edit_bridge as bridge
    monkeypatch.setattr(bridge, "read_auto_edit_task", lambda tid: {
        "clips": [{"clip_id": "clip-aaaaaaaaaa", "title": "t", "duration": 1.0}],
        "raw_video_path": "media/x/src.mp4"})
    # 墓碑台账（deleted）
    led = env["tasks"] / "projects" / PID / "clips.json"
    led.write_text(json.dumps({"project_id": PID, "status": "deleted",
                               "deleted_at_utc": "2026-10-20T00:00:00+00:00"}), encoding="utf-8")
    manifest = store.sync_from_tasks(PID)
    assert manifest["clips"] == {}                     # 永不回填
    assert manifest.get("source_video") is None        # 源片登记同样不发生
    # 删除中同样拒绝
    led.write_text(json.dumps({"project_id": PID, "status": "deleting"}), encoding="utf-8")
    assert store.sync_from_tasks(PID)["clips"] == {}
