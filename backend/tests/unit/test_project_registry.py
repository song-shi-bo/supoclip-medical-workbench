"""统一项目创建入口（project_registry.ensure_project）定向单测。

覆盖 2026-10-19 统一入口的四类不变量（离线、tmp 根、注入桩 timeline 工厂）：
1. 幂等：二次 ensure 走 reused，工厂只被调一次；
2. 防孤儿：工程无法成立且台账不存在 → incomplete 且**绝不落 clips.json**；
3. 租户隔离：fixture 强制 tenant=test，生产 list_projects 服务端排除；
4. 历史孤儿：台账在而工程缺 → ledger_only，不静默补建。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import project_registry as registry
from src import studio_project_store as store

PID = "timeline_abc123def456"


@pytest.fixture
def roots(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    tasks = tmp_path / "tasks"
    uploads.mkdir()
    (tasks / "projects").mkdir(parents=True)
    # 仓库约定：台账根经 store.TASKS_ROOT 注入（与既有 store 测试同一模式）。
    monkeypatch.setattr(store, "TASKS_ROOT", tasks)
    return uploads, tasks


def stub_factory(calls: list):
    """桩 timeline 工厂：写最小 project.json，记录调用；重复调用直接炸（幂等可见）。"""
    def factory(*, project_id, video_path, raw_segments, corrected_segments, uploads_root):
        if any(c["project_id"] == project_id for c in calls):
            raise AssertionError("timeline 工厂被重复调用（幂等破坏）")
        calls.append({"project_id": project_id})
        target = uploads_root / "studio-projects" / project_id / "project.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"id": project_id, "source": {"name": video_path.name}}),
                          encoding="utf-8")
        return {"id": project_id}
    return factory


def source_file(tmp_path: Path) -> Path:
    src = tmp_path / "source.mp4"
    src.write_bytes(b"\x00\x00\x00 fake")
    return src


def ledger_ready(tasks: Path, pid: str = PID) -> bool:
    return (tasks / "projects" / pid / "clips.json").is_file()


def test_create_legal_then_idempotent_reuse(roots, tmp_path):
    uploads, tasks = roots
    calls: list = []
    first = registry.ensure_project(PID, origin="import", source_path=source_file(tmp_path),
                                    corrected_segments=[{"start": 0, "end": 1, "text": "段"}],
                                    uploads_root=uploads,
                                    timeline_factory=stub_factory(calls))
    assert first["state"] == "created"
    assert sorted(first["created"]) == ["ledger", "timeline_project"]
    assert first["tenant"] == "production"
    assert ledger_ready(tasks)
    manifest = json.loads((tasks / "projects" / PID / "clips.json").read_text(encoding="utf-8"))
    assert manifest["origin"] == "import" and manifest["tenant"] == "production"

    # 二次 ensure：reused，工厂不再被调
    second = registry.ensure_project(PID, origin="import", source_path=source_file(tmp_path),
                                     corrected_segments=[{"start": 0, "end": 1, "text": "段"}],
                                     uploads_root=uploads,
                                     timeline_factory=stub_factory(calls))
    assert second["state"] == "reused" and second["created"] == []
    assert len(calls) == 1


def test_incomplete_never_creates_orphan_ledger(roots):
    uploads, tasks = roots
    result = registry.ensure_project(PID, origin="bridge", uploads_root=uploads)
    assert result["state"] == "incomplete"
    assert "timeline_project" in result["missing"]
    assert not ledger_ready(tasks)  # 防孤儿不变量：绝不只落 clips.json


def test_ledger_only_history_orphan_not_silently_patched(roots):
    uploads, tasks = roots
    # 预置历史孤儿：只有台账（无工程），无 tenant 字段。
    led_dir = tasks / "projects" / PID
    led_dir.mkdir(parents=True)
    (led_dir / "clips.json").write_text(json.dumps({"project_id": PID, "clips": {}}),
                                        encoding="utf-8")
    result = registry.ensure_project(PID, origin="bridge", uploads_root=uploads)
    assert result["state"] == "ledger_only"
    assert not registry.timeline_project_path(PID, uploads).is_file()  # 不静默补建


def test_fixture_forces_test_tenant_and_list_excludes(roots, tmp_path, monkeypatch):
    uploads, tasks = roots
    monkeypatch.setattr(store, "TASKS_ROOT", tasks)
    result = registry.ensure_project("timeline_fixture01", origin="fixture",
                                     uploads_root=uploads)
    assert result["tenant"] == "test" and result["state"] == "created"
    manifest = json.loads((tasks / "projects" / "timeline_fixture01" / "clips.json")
                          .read_text(encoding="utf-8"))
    assert manifest["tenant"] == "test"

    # 生产项目一个正常 + 一个 test 租户：列表只返回前者（服务端过滤，不靠名称）。
    normal = tasks / "projects" / "timeline_prod01"
    normal.mkdir(parents=True)
    (normal / "clips.json").write_text(json.dumps({"project_id": "timeline_prod01", "clips": {}}),
                                       encoding="utf-8")
    listed = [p["project_id"] for p in store.list_projects()]
    assert "timeline_prod01" in listed
    assert "timeline_fixture01" not in listed


def test_origin_validation(roots):
    uploads, tasks = roots
    with pytest.raises(ValueError, match="E_PROJECT_ORIGIN"):
        registry.ensure_project(PID, origin="somewhere", uploads_root=uploads)


def test_state_readonly(roots):
    uploads, tasks = roots
    state = registry.project_state(PID, uploads_root=uploads)
    assert state == {"project_id": PID, "has_ledger": False, "has_timeline": False,
                     "tenant": None, "missing": ["timeline_project", "ledger"]}
    assert not ledger_ready(tasks)


# ---------------------------------------------------------------------------
# 真实接入测试（requirement 4，2026-10-19）：导入即建工程+空台账、bridge 首剪、
# 幂等不重复、台账写失败可恢复（原子替换）、跨租户不可列表/复用。
# ---------------------------------------------------------------------------

def test_import_appears_in_management_immediately(roots, monkeypatch, tmp_path):
    """工作台导入：create_timeline_project 成功后 ensure → 工程+空台账即刻可列表。"""
    uploads, tasks = roots
    monkeypatch.setattr(store, "TASKS_ROOT", tasks)
    calls: list = []
    # 模拟导入：timeline 已由 create_timeline_project 建立（stub 工厂同款落盘）。
    stub_factory(calls)(project_id=PID, video_path=tmp_path / "src.mp4",
                        raw_segments=[], corrected_segments=[], uploads_root=uploads)
    result = registry.ensure_project(PID, origin="import", uploads_root=uploads)
    # timeline 已在 → 只补空台账（created 指 ledger；工程未被重复创建）
    assert result["state"] == "created"
    assert result["created"] == ["ledger"]
    assert ledger_ready(tasks)
    listed = [p["project_id"] for p in store.list_projects()]
    assert PID in listed                       # 导入完成即刻可见
    cards = store.list_cards(PID)
    assert cards == []                          # 空台账（0 成片也可见）


def test_bridge_first_clip_success_and_no_duplicate(roots, monkeypatch, tmp_path):
    uploads, tasks = roots
    monkeypatch.setattr(store, "TASKS_ROOT", tasks)
    calls: list = []
    registry.ensure_project(PID, origin="bridge", source_path=source_file(tmp_path),
                            corrected_segments=[{"start": 0, "end": 1, "text": "段"}],
                            uploads_root=uploads, timeline_factory=stub_factory(calls))
    clips = [{"clip_id": "clip001", "title": "首剪", "duration": 10.0,
              "source_start": 0.0, "source_end": 10.0}]
    store.register_clips(PID, "a" * 32, clips)
    store.register_clips(PID, "a" * 32, clips)  # 重复提交同一任务：幂等
    cards = store.list_cards(PID)
    assert len(cards) == 1 and cards[0]["clip_id"] == "clip001"
    # 第二次自动剪辑（新任务）→ 新卡，不覆盖首卡
    store.register_clips(PID, "b" * 32, [{"clip_id": "clip002", "title": "二剪",
                                          "duration": 5.0, "source_start": 10.0, "source_end": 15.0}])
    assert len(store.list_cards(PID)) == 2


def test_ledger_write_failure_recovers_via_atomic_replace(roots, monkeypatch, tmp_path):
    """timeline 已建成但台账写失败（模拟 _atomic_json 异常）→ 可恢复、无半成品。"""
    uploads, tasks = roots
    monkeypatch.setattr(store, "TASKS_ROOT", tasks)
    calls: list = []
    factory = stub_factory(calls)
    # 第一次：台账写入失败
    real_atomic = store._atomic_json
    def boom(path, payload):
        raise OSError("disk-full-simulated")
    monkeypatch.setattr(store, "_atomic_json", boom)
    with pytest.raises(OSError):
        registry.ensure_project(PID, origin="import", source_path=source_file(tmp_path),
                                corrected_segments=[{"start": 0, "end": 1, "text": "段"}],
                                uploads_root=uploads, timeline_factory=factory)
    assert not ledger_ready(tasks)              # 失败不留半成品台账
    leftovers = list((tasks / "projects" / PID).glob("*.tmp")) if (tasks / "projects" / PID).exists() else []
    assert leftovers == []
    # 恢复：解除注入后重试 → timeline 复用（工厂不重复调用），台账建成
    monkeypatch.setattr(store, "_atomic_json", real_atomic)
    result = registry.ensure_project(PID, origin="import", source_path=source_file(tmp_path),
                                     corrected_segments=[{"start": 0, "end": 1, "text": "段"}],
                                     uploads_root=uploads, timeline_factory=factory)
    assert result["state"] == "created"
    assert len(calls) == 1                      # 工程未重复创建
    assert ledger_ready(tasks)


def test_cross_tenant_cannot_list_or_reuse(roots, monkeypatch, tmp_path):
    uploads, tasks = roots
    monkeypatch.setattr(store, "TASKS_ROOT", tasks)
    registry.ensure_project("timeline_t1", origin="fixture", uploads_root=uploads)
    # 列表排除
    assert all(p["project_id"] != "timeline_t1" for p in store.list_projects())
    # 复用拒绝（tenant_mismatch），台账租户未被改写
    result = registry.ensure_project("timeline_t1", origin="import", uploads_root=uploads)
    assert result["state"] == "tenant_mismatch"
    manifest = json.loads((tasks / "projects" / "timeline_t1" / "clips.json").read_text(encoding="utf-8"))
    assert manifest["tenant"] == "test"
