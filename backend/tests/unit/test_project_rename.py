"""项目重命名（display_name 内联编辑）定向测试。

覆盖：保存持久化、幂等、去空格、1–60 校验、乐观并发冲突、deleting/deleted 拒绝、
archived 允许、目录/ID/路径不变（不重命名磁盘文件夹）、列表名称同步。
"""

from __future__ import annotations

import json

import pytest

from src import studio_access as sa
from src import studio_project_store as store
from src.studio_access import AccessLedger

PID = "timeline_rename01"


def principal(subject="8"):
    import time
    return {"subject": subject, "slug": subject, "domain_id": sa_domain(), "expires_at_ms": 0}


def sa_domain():
    return sa.DOMAIN_ID


@pytest.fixture
def env(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    tasks = tmp_path / "tasks"
    uploads.mkdir()
    (tasks / "projects").mkdir(parents=True)
    monkeypatch.setattr(store, "TASKS_ROOT", tasks)
    # timeline 工程（模拟导入产物：含源片名，供回退名链路）
    tl = uploads / "studio-projects" / PID
    tl.mkdir(parents=True)
    (tl / "project.json").write_text(json.dumps(
        {"id": PID, "source": {"name": "临床讲解.mp4", "duration": 30}}), encoding="utf-8")
    # 空台账（导入即建）
    store.save_manifest(PID, store.load_manifest(PID))
    acl = AccessLedger(tmp_path / "acl.sqlite3")
    acl.register("project", PID, principal("8"))
    return {"uploads": uploads, "tasks": tasks, "acl": acl}


def ledger(env):
    return json.loads((env["tasks"] / "projects" / PID / "clips.json").read_text(encoding="utf-8"))


def test_rename_persists_and_syncs_list(env):
    first = store.rename_project(PID, "  临床讲解·修订版  ")
    assert first["display_name"] == "临床讲解·修订版"
    led = ledger(env)
    assert led["display_name"] == "临床讲解·修订版"
    assert "display_name_updated_at" in led
    # 磁盘目录/工程文件不动（不重命名磁盘文件夹）
    assert (env["uploads"] / "studio-projects" / PID / "project.json").is_file()
    # 名称同步经路由层 _project_display_meta（meta 回退链测试覆盖）


def test_rename_idempotent_same_name(env):
    store.rename_project(PID, "临床讲解·修订版")
    first = ledger(env)
    store.rename_project(PID, "临床讲解·修订版")
    second = ledger(env)
    assert second["display_name"] == "临床讲解·修订版"
    assert first["updated_at"] == second["updated_at"]  # 同名保存不抖动版本


def test_rename_strips_and_validates_length(env):
    with pytest.raises(store.ProjectStoreError) as ei:
        store.validate_project_name("   ")
    assert ei.value.code == "E_PROJECT_NAME_INVALID"
    with pytest.raises(store.ProjectStoreError):
        store.validate_project_name("x" * 61)
    assert store.validate_project_name("x" * 60) == "x" * 60
    # 允许重复名称（身份仍是 project_id）
    store.rename_project(PID, "同名A")
    store.rename_project(PID + "x", "同名A")


def test_rename_conflict_on_stale_updated_at(env):
    store.rename_project(PID, "名字V1")
    current = ledger(env)
    # 旧页面带着过期 updated_at 提交 → 冲突，且不覆盖新名字
    with pytest.raises(store.ProjectStoreError) as ei:
        store.rename_project(PID, "旧标签页的名字",
                             expected_updated_at="2000-01-01T00:00:00+00:00")
    assert ei.value.code == "E_PROJECT_RENAME_CONFLICT"
    assert ledger(env)["display_name"] == "名字V1"
    # 匹配当前 updated_at → 成功
    store.rename_project(PID, "名字V2", expected_updated_at=current["updated_at"])
    assert ledger(env)["display_name"] == "名字V2"


def test_rename_blocked_for_deleting_and_deleted(env):
    store.rename_project(PID, "V1")
    led = ledger(env)
    led["status"] = "deleting"
    (env["tasks"] / "projects" / PID / "clips.json").write_text(json.dumps(led), encoding="utf-8")
    with pytest.raises(store.ProjectStoreError) as ei:
        store.rename_project(PID, "V2")
    assert ei.value.code == "E_PROJECT_NOT_MUTABLE"
    led["status"] = "deleted"
    (env["tasks"] / "projects" / PID / "clips.json").write_text(json.dumps(led), encoding="utf-8")
    with pytest.raises(store.ProjectStoreError):
        store.rename_project(PID, "V3")


def test_rename_allowed_for_archived(env):
    led = ledger(env)
    led["status"] = "archived"
    (env["tasks"] / "projects" / PID / "clips.json").write_text(json.dumps(led), encoding="utf-8")
    store.rename_project(PID, "归档里也能改名")
    assert ledger(env)["display_name"] == "归档里也能改名"


def test_rename_does_not_touch_ids_or_sources(env):
    store.register_source_video(PID, path="media/src/src.mp4")
    before = ledger(env)
    store.rename_project(PID, "新名字")
    after = ledger(env)
    assert before["source_video"] == after["source_video"]
    assert before["project_id"] == after["project_id"] == PID
    assert before["clip_order"] == after["clip_order"]
    assert before["clips"] == after["clips"]


def test_name_meta_fallback_chain(env):
    meta = store.get_project_name_meta(PID, uploads_root=env["uploads"])
    assert meta["display_name"] is None and meta["fallback_name"] == "临床讲解.mp4"
    store.rename_project(PID, "显式命名")
    meta = store.get_project_name_meta(PID, uploads_root=env["uploads"])
    assert meta["display_name"] == "显式命名" and "fallback_name" not in meta
