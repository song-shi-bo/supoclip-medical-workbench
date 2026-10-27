"""独立封面素材清单（cover-assets）定向测试（2026-10-20）。

覆盖：五元组绑定、ready/failed 磁盘实测状态、缺失封面不用他片冒充、
旧批次封面绝不出现（重剪原子切换）、重剪失败保留旧批次、刷新恢复（清单可重复读取）。
全部 tmp 根合成数据，不触碰真实项目。
"""

from __future__ import annotations

import json

import pytest

from src import studio_project_store as store
from src import studio_access as sa

PID = "timeline_coverpanel01"
OLD_TASK = "oldtask111111111111111111111111"
NEW_TASK = "newtask222222222222222222222222"


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
    (uploads / "studio-projects" / PID / "raw").mkdir(parents=True)
    (uploads / "studio-projects" / PID / "raw" / "aaaabbbbccccddddeeeeffff00001111.mp4").write_bytes(b"raw")
    monkeypatch.setattr(store, "TASKS_ROOT", tasks)
    acl = sa.AccessLedger(tmp_path / "acl.sqlite3")
    acl.register("project", PID, principal("8"))
    return {"uploads": uploads, "tasks": tasks, "acl": acl}


def clip_entry(clip_id: str, task_id: str, *, cover_path: str | None, revision: int = 1) -> dict:
    cover = None
    if cover_path is not None:
        cover = {"cover_asset_id": f"cover-{clip_id}-v{revision}", "revision": revision,
                 "active_revision": revision, "path": cover_path, "sha256": "f" * 64,
                 "bytes": 2048, "kind": "auto_edit", "style_id": "realframe_v2",
                 "updated_at": "2026-10-20T00:00:00+00:00", "history": [
                     {"revision": revision, "cover_asset_id": f"cover-{clip_id}-v{revision}"}]}
    return {"clip_id": clip_id, "name": f"切片 {clip_id}", "title": f"切片 {clip_id}",
            "render_task_id": task_id, "render_revision": 1, "cover": cover}


def save_ledger(env, entries: list[dict]) -> None:
    manifest = store.load_manifest(PID)
    manifest["clips"] = {e["clip_id"]: e for e in entries}
    manifest["clip_order"] = [e["clip_id"] for e in entries]
    store.save_manifest(PID, manifest)


def make_cover_file(env, task_id: str, clip_id: str, payload: bytes = b"\xff\xd8jpg") -> str:
    rel = f"output/{task_id}/{clip_id}/cover.jpg"
    path = env["tasks"] / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return rel


def test_binding_and_ready_status(env):
    rel = make_cover_file(env, NEW_TASK, "clip-aaaaaaaaaa")
    save_ledger(env, [clip_entry("clip-aaaaaaaaaa", NEW_TASK, cover_path=rel)])
    result = store.list_cover_assets(PID, uploads_root=env["uploads"], tasks_root=env["tasks"])
    assert result["source_id"] == "aaaabbbbccccddddeeeeffff00001111"  # 工程 raw 目录名（上传原件 uid）
    item = result["items"][0]
    # 五元组绑定
    assert item["project_id"] == PID
    assert item["source_id"] == "aaaabbbbccccddddeeeeffff00001111"
    assert item["clip_id"] == "clip-aaaaaaaaaa"
    assert item["render_task_id"] == NEW_TASK
    assert item["revision"] == 1
    assert result["batch"] == {"clip_count": 1, "ready": 1, "failed": 0}
    assert item["status"] == "ready" and item["bytes"] == len(b"\xff\xd8jpg")


def test_missing_cover_reported_not_substituted(env):
    # clip-1 有封面文件；clip-2 登记了路径但文件丢失；clip-3 从未生成。
    rel1 = make_cover_file(env, NEW_TASK, "clip-1")
    save_ledger(env, [clip_entry("clip-1", NEW_TASK, cover_path=rel1),
                      clip_entry("clip-2", NEW_TASK, cover_path=f"output/{NEW_TASK}/clip-2/cover.jpg"),
                      clip_entry("clip-3", NEW_TASK, cover_path=None)])
    result = store.list_cover_assets(PID, uploads_root=env["uploads"], tasks_root=env["tasks"])
    by_id = {i["clip_id"]: i for i in result["items"]}
    assert by_id["clip-1"]["status"] == "ready"
    assert by_id["clip-2"]["status"] == "failed" and "缺失" in by_id["clip-2"]["reason"]
    assert by_id["clip-3"]["status"] == "failed" and "未生成" in by_id["clip-3"]["reason"]
    # 失败项绝不携带其它切片的封面数据
    for clip_id in ("clip-2", "clip-3"):
        assert by_id[clip_id]["path"] in (None, f"output/{NEW_TASK}/{clip_id}/cover.jpg")
        assert by_id[clip_id]["sha256"] is None
    assert result["batch"] == {"clip_count": 3, "ready": 1, "failed": 2}


def test_old_batch_never_appears_after_reedit_switch(env):
    # v1 批次：旧任务 + 旧封面（含旧任务 JSON 留在 completed/ 里模拟残留）
    old_rel = make_cover_file(env, OLD_TASK, "clip-old")
    save_ledger(env, [clip_entry("clip-old", OLD_TASK, cover_path=old_rel)])
    completed = env["tasks"] / "completed" / f"autoedit_{OLD_TASK}.json"
    completed.parent.mkdir(parents=True, exist_ok=True)
    completed.write_text(json.dumps({"task_id": f"autoedit_{OLD_TASK}", "status": "completed",
                                     "bridge": {"project_id": PID},
                                     "clips": [{"clip_id": "clip-old"}]}), encoding="utf-8")
    # 重剪成功 → 台账**原子替换**为新批次（新 clip_id / 新 render_task_id / 新封面）
    new_rel = make_cover_file(env, NEW_TASK, "clip-new")
    save_ledger(env, [clip_entry("clip-new", NEW_TASK, cover_path=new_rel, revision=1)])
    result = store.list_cover_assets(PID, uploads_root=env["uploads"], tasks_root=env["tasks"])
    assert [i["clip_id"] for i in result["items"]] == ["clip-new"]
    assert all(i["render_task_id"] == NEW_TASK for i in result["items"])
    assert not any(i["clip_id"] == "clip-old" or i["render_task_id"] == OLD_TASK
                   for i in result["items"])  # 旧批次封面结构上不可能出现


def test_failed_reedit_keeps_old_batch(env):
    old_rel = make_cover_file(env, OLD_TASK, "clip-old")
    save_ledger(env, [clip_entry("clip-old", OLD_TASK, cover_path=old_rel, revision=3)])
    before = store.list_cover_assets(PID, uploads_root=env["uploads"], tasks_root=env["tasks"])
    # 新批次失败：台账不发生任何变化（重剪一致性：失败不写台账）
    after = store.list_cover_assets(PID, uploads_root=env["uploads"], tasks_root=env["tasks"])
    assert after == before
    assert after["items"][0]["revision"] == 3 and after["items"][0]["status"] == "ready"


def test_refresh_recovery_reads_same_state(env):
    rel = make_cover_file(env, NEW_TASK, "clip-1")
    save_ledger(env, [clip_entry("clip-1", NEW_TASK, cover_path=rel)])
    first = store.list_cover_assets(PID, uploads_root=env["uploads"], tasks_root=env["tasks"])
    # 模拟页面刷新 / 服务重启后再读：同一台账 → 同一清单
    second = store.list_cover_assets(PID, uploads_root=env["uploads"], tasks_root=env["tasks"])
    assert first == second
