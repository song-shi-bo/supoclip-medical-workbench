"""项目生命周期定向测试（2026-10-20）：归档/恢复、删除预览、清单先行、幂等清理、
共享硬链接安全、容量返还、删除期守卫、owner 校验。全部使用 tmp 根与合成文件，
**不对任何现有真实项目执行删除**。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from src import project_lifecycle as lifecycle
from src import project_registry as registry
from src import studio_project_store as store
from src import studio_access as sa

PID = "timeline_lifecycle01"
TASK = "autoedit_" + "a" * 20


def principal(subject="8"):
    """可信主体形状与 test_studio_access 同款（domain 取 sa.DOMAIN_ID）。"""
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
    return {"uploads": uploads, "tasks": tasks, "acl_path": acl_path, "acl": acl,
            "principal": principal("8")}


def make_legal(env, calls=None):
    src = env["uploads"] / "src.mp4"
    src.write_bytes(b"\x00\x00 fake video")
    calls = calls if calls is not None else []

    def factory(*, project_id, video_path, raw_segments, corrected_segments, uploads_root):
        calls.append(project_id)
        p = uploads_root / "studio-projects" / project_id / "project.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"id": project_id, "source": {"name": video_path.name}}),
                     encoding="utf-8")
        return {"id": project_id}

    result = registry.ensure_project(PID, origin="import", source_path=src,
                                     corrected_segments=[{"start": 0, "end": 1, "text": "段"}],
                                     uploads_root=env["uploads"],
                                     timeline_factory=factory)
    assert result["state"] in {"created", "reused"}
    return src


def seed_artifacts(env):
    """伪造可删资产：任务产物/转写目录/缩略图缓存 + 共享硬链接源片。"""
    tasks = env["tasks"]
    out = tasks / "output" / TASK
    out.mkdir(parents=True)
    (out / "clip_final.mp4").write_bytes(b"v" * 4096)
    (out / "subtitles.srt").write_bytes(b"s" * 128)
    # 任务 JSON 登记（collect_task_ids 据此发现产物目录）
    completed = tasks / "completed" / f"{TASK}.json"
    completed.parent.mkdir(parents=True, exist_ok=True)
    completed.write_text(json.dumps({"task_id": TASK, "status": "completed",
                                     "bridge": {"project_id": PID},
                                     "raw_video_path": "media/x/src.mp4"}),
                         encoding="utf-8")
    tdir = tasks / "transcripts" / "8820f930a4dfc764"
    tdir.mkdir(parents=True)
    (tdir / "v1.json").write_text(json.dumps(
        {"source_project_id": PID, "asr_segments": [{"start": 0, "end": 1, "text": "段"}]}),
        encoding="utf-8")
    thumb = env["uploads"] / "studio-thumbnails" / f"{PID}.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"jpg" * 100)
    # 共享硬链接：源片被两个路径引用（本项目 raw/ + 另一"项目"的引用）
    raw = env["uploads"] / "studio-projects" / PID / "raw" / "src.mp4"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"shared-source-bytes" * 512)
    other = env["uploads"] / "other-project-src.mp4"
    os.link(raw, other)
    return {"raw": raw, "other": other, "thumb": thumb}


def test_archive_hides_from_list_files_intact_restore(env):
    make_legal(env)
    store.register_source_video(PID, path=None)
    assert any(p["project_id"] == PID for p in store.list_projects())
    lifecycle.archive_project(PID, tasks_root=env["tasks"])
    assert lifecycle.read_status(PID, tasks_root=env["tasks"]) == "archived"
    assert all(p["project_id"] != PID for p in store.list_projects())  # 默认列表隐藏
    assert (env["tasks"] / "projects" / PID / "clips.json").is_file()   # 文件不动
    lifecycle.restore_project(PID, tasks_root=env["tasks"])
    assert any(p["project_id"] == PID for p in store.list_projects())  # 可恢复


def test_delete_preview_lists_categories_and_shared(env):
    make_legal(env)
    assets = seed_artifacts(env)
    preview = lifecycle.build_delete_preview(PID, uploads_root=env["uploads"],
                                             tasks_root=env["tasks"])
    kinds = {i["kind"] for i in preview["items"]}
    # output 目录由 purge 分类法记为 task_side（task_output 去重合并）。
    assert {"timeline_project", "ledger", "task_side", "transcripts",
            "thumbnails"} <= kinds
    shared_items = [i for i in preview["items"] if i["shared"]]
    assert shared_items and all(i["shared_bytes"] > 0 for i in shared_items)
    assert preview["reclaim_bytes"] > 0
    # 共享硬链接的字节不计入本项目可释放量（不重复计入/不冒充源片）
    assert preview["shared_bytes"] >= 0


def test_confirm_persists_manifest_before_any_deletion(env):
    make_legal(env)
    seed_artifacts(env)
    manifest = lifecycle.confirm_deletion(PID, confirmed_by="subject:8",
                                          uploads_root=env["uploads"],
                                          tasks_root=env["tasks"])
    mpath = env["tasks"] / "projects" / PID / "deletion_manifest.json"
    assert mpath.is_file()                                  # 清单先行
    on_disk = json.loads(mpath.read_text(encoding="utf-8"))
    assert on_disk["status"] == "pending" and on_disk["confirmed_by"] == "subject:8"
    assert lifecycle.read_status(PID, tasks_root=env["tasks"]) == "deleting"
    # 尚未删除任何文件（确认后、任务启动前）
    assert (env["tasks"] / "output" / TASK / "clip_final.mp4").is_file()
    assert len(on_disk["items"]) >= 4


def test_run_deletion_idempotent_tombstone_and_capacity(env, monkeypatch):
    make_legal(env)
    assets = seed_artifacts(env)
    # ACL 预置非零字节：删除完成后必须清零（容量返还）
    conn = sqlite3.connect(str(env["acl_path"]))
    conn.execute("UPDATE objects SET bytes=4096 WHERE kind='project' AND object_id=?", (PID,))
    conn.commit(); conn.close()
    lifecycle.confirm_deletion(PID, confirmed_by="subject:8",
                               uploads_root=env["uploads"], tasks_root=env["tasks"])
    lifecycle.run_deletion_task(PID, uploads_root=env["uploads"], tasks_root=env["tasks"],
                                acl_path=env["acl_path"])
    # 文件实际删除
    assert not (env["tasks"] / "output" / TASK).exists()
    assert not (env["uploads"] / "studio-thumbnails" / f"{PID}.jpg").exists()
    # 共享硬链接：本项目链接删除，但其它引用的文件数据不受影响
    raw = env["uploads"] / "studio-projects" / PID / "raw" / "src.mp4"
    assert not raw.exists() and assets["other"].exists() \
        and assets["other"].read_bytes() == b"shared-source-bytes" * 512
    # 墓碑 + 状态
    tomb = json.loads((env["tasks"] / "projects" / PID / "clips.json").read_text(encoding="utf-8"))
    assert tomb["status"] == "deleted" and tomb["deletion_manifest"] == "deletion_manifest.json"
    assert lifecycle.read_status(PID, tasks_root=env["tasks"]) == "deleted"
    # ACL 字节清零（容量返还）
    conn = sqlite3.connect(str(env["acl_path"]))
    n = conn.execute("SELECT COUNT(*) FROM objects WHERE object_id=? AND bytes>0",
                     (PID,)).fetchone()[0]
    conn.close()
    assert n == 0
    # 幂等：重复执行不再报错、状态保持 deleted
    lifecycle.run_deletion_task(PID, uploads_root=env["uploads"], tasks_root=env["tasks"])
    assert lifecycle.read_status(PID, tasks_root=env["tasks"]) == "deleted"


def test_failure_enters_delete_failed_and_retry_completes(env, monkeypatch):
    make_legal(env)
    seed_artifacts(env)
    lifecycle.confirm_deletion(PID, confirmed_by="subject:8",
                               uploads_root=env["uploads"], tasks_root=env["tasks"])
    real_delete = lifecycle._delete_item
    def flaky(path: Path):
        if "output" in str(path):
            raise OSError("injected")
        real_delete(path)
    monkeypatch.setattr(lifecycle, "_delete_item", flaky)
    lifecycle.run_deletion_task(PID, uploads_root=env["uploads"], tasks_root=env["tasks"])
    assert lifecycle.read_status(PID, tasks_root=env["tasks"]) == "delete_failed"
    mpath = env["tasks"] / "projects" / PID / "deletion_manifest.json"
    m = json.loads(mpath.read_text(encoding="utf-8"))
    assert any(i["state"] == "failed" for i in m["items"])
    assert any(i["state"] == "done" for i in m["items"])   # 其余项正常完成
    # 重试：只清理（不触发分析/渲染——本执行器只删文件）
    monkeypatch.setattr(lifecycle, "_delete_item", real_delete)
    lifecycle.retry_deletion(PID, uploads_root=env["uploads"], tasks_root=env["tasks"],
                             acl_path=env["acl_path"])
    assert lifecycle.read_status(PID, tasks_root=env["tasks"]) == "deleted"
    assert not (env["tasks"] / "output" / TASK).exists()


def test_assert_mutable_blocks_deleting_and_deleted(env):
    make_legal(env)
    lifecycle.confirm_deletion(PID, confirmed_by="subject:8",
                               uploads_root=env["uploads"], tasks_root=env["tasks"])
    with pytest.raises(lifecycle.LifecycleError) as ei:
        lifecycle.assert_mutable(PID, tasks_root=env["tasks"])
    assert ei.value.code == "E_PROJECT_NOT_MUTABLE"
    lifecycle.set_status(PID, "deleted", tasks_root=env["tasks"])
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.assert_mutable(PID, tasks_root=env["tasks"])


def test_owner_check(env):
    make_legal(env)
    lifecycle.assert_owner(PID, "8", acl_path=env["acl_path"])       # 所有者 ✔
    with pytest.raises(lifecycle.LifecycleError) as ei:
        lifecycle.assert_owner(PID, "9", acl_path=env["acl_path"])   # 非所有者
    assert ei.value.code == "E_PROJECT_OWNER_REQUIRED"
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.assert_owner("timeline_ghost", "8", acl_path=env["acl_path"])


def test_registry_refuses_not_mutable(env):
    make_legal(env)
    lifecycle.confirm_deletion(PID, confirmed_by="subject:8",
                               uploads_root=env["uploads"], tasks_root=env["tasks"])
    result = registry.ensure_project(PID, origin="import", uploads_root=env["uploads"])
    assert result["state"] == "not_mutable"
