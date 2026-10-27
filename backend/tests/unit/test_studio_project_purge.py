"""studio_project_purge 单元测试：清空当前项目任务数据（保留源片与转写）。

覆盖点（每条都对应一个真实故障模式，不是覆盖率凑数）：

1. ``summarize`` 只读、报出将删卡片/任务/产物与保留项，且**不修改任何文件**；
2. 清空后卡片为零、任务 JSON/状态/产物/localtask/日志一并消失；
3. 源片（``media/<task>/``）与 ``transcripts/`` **保留**；
4. 清空后 ``sync_from_tasks`` 不得把卡片复活（卡片由任务 JSON 反填，这是真会踩的坑）；
5. 活动任务（``in``/``processing`` 有 JSON 或状态非终态）一律拒绝；
6. ``expected_card_count`` 不符返回 E_PURGE_CARD_COUNT_MISMATCH（防误删闸门）；
7. 缺少 confirm 返回 E_PURGE_CONFIRM_REQUIRED；
8. 幂等：重复清空不报错；
9. 失败还原：台账写入失败时文件搬回、旧台账不动；
10. 其他项目不受影响。
"""

from __future__ import annotations

import json

import pytest

from src import studio_project_purge as purge
from src import studio_project_store as store

PROJECT = "timeline_abc123def456"
OTHER_PROJECT = "timeline_999999999999"
TASK = "autoedit_aaaaaaaaaaaa"
COVER_TASK = "cover_111122223333"


@pytest.fixture
def root(tmp_path, monkeypatch):
    """临时任务根：purge 每次都读 store.TASKS_ROOT，所以打桩 store 即可。"""
    monkeypatch.setattr(store, "TASKS_ROOT", tmp_path / "tasks")
    return store.TASKS_ROOT


def _write(path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed(root, *, project: str = PROJECT, task: str = TASK,
          status: str = "completed", include_source: bool = True) -> None:
    """造一个「已完成任务 + 一张卡片 + 源片 + 转写」的最小项目。"""
    _write(root / "completed" / f"{task}.json", {
        "task_id": task, "status": status,
        "bridge": {"project_id": project}, "raw_video_path": f"media/{task}/source.mp4",
    })
    _write(root / "status" / f"{task}.json", {"task_id": task, "status": status})
    clip_dir = root / "output" / task / "clip-001"
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / "clip_final.mp4").write_bytes(b"f" * 2048)
    _write(root / "output" / task / "clip_plan.json", {"highlights": []})
    _write(root / "output" / task / "workflow_snapshot.json", {"rule_version": "test"})
    if include_source:
        (root / "media" / task).mkdir(parents=True, exist_ok=True)
        (root / "media" / task / "source.mp4").write_bytes(b"s" * 4096)
        store.register_source_video(project, path=f"media/{task}/source.mp4")
    store.register_clips(project, task, [{"clip_id": "clip-001", "title": "主题",
                                          "duration": 30.0}])
    (root / "transcripts" / "src-1").mkdir(parents=True, exist_ok=True)
    _write(root / "transcripts" / "src-1" / "v1.json", {"schema": "studio.corrected_transcript/1"})


# ---------------------------------------------------------------------------
# 1. 只读预览
# ---------------------------------------------------------------------------

def test_summarize_reports_scope_and_keeps_files_untouched(root):
    _seed(root)
    before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())

    summary = purge.summarize(PROJECT)

    assert summary["card_count"] == 1
    assert summary["clip_ids"] == ["clip-001"]
    assert summary["applied_task_count"] == 1
    assert summary["artifact_count"] > 0
    assert summary["reclaimable_bytes"] > 0
    assert summary["active_task_ids"] == []
    assert summary["keep"]["source_video"] == f"media/{TASK}/source.mp4"
    assert summary["keep"]["protected_media_dirs"] == [TASK]
    assert summary["keep"]["transcripts_present"] is True
    after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
    assert after == before, "summarize 必须是纯只读"


# ---------------------------------------------------------------------------
# 2 & 3. 清空：删任务与产物，留源片与转写
# ---------------------------------------------------------------------------

def test_clear_removes_tasks_and_artifacts_but_keeps_source_and_transcript(root):
    _seed(root)

    result = purge.clear_project_tasks(PROJECT, expected_card_count=1, confirm=True)

    assert result["cleared_card_count"] == 1
    assert result["cleared_task_count"] == 1
    assert store.load_manifest(PROJECT)["clips"] == {}
    assert store.load_manifest(PROJECT)["clip_order"] == []
    assert not (root / "completed" / f"{TASK}.json").exists()
    assert not (root / "status" / f"{TASK}.json").exists()
    assert not (root / "output" / TASK).exists()
    # 源片与转写必须还在：删了它们这个项目就没法重新剪辑了。
    assert (root / "media" / TASK / "source.mp4").is_file()
    assert (root / "transcripts" / "src-1" / "v1.json").is_file()
    # 台账本体保留（只清卡片，不删项目）。
    assert (root / "projects" / PROJECT / "clips.json").is_file()
    assert result["kept_source_video"] == f"media/{TASK}/source.mp4"
    assert result["recoverable"] is False, "默认 purge=true 应物理删除并释放磁盘"
    assert not (root / ".purge-trash").exists()
    assert result["freed_bytes"] > 0


def test_clear_with_purge_false_keeps_trash_for_debugging(root):
    _seed(root)

    result = purge.clear_project_tasks(PROJECT, expected_card_count=1, confirm=True, purge=False)

    assert result["recoverable"] is True
    assert (root / ".purge-trash").is_dir()
    assert not (root / "completed" / f"{TASK}.json").exists()


def test_clear_removes_local_task_record_and_log_lines(root):
    _seed(root)
    _write(root / "localtasks" / f"{COVER_TASK}.json",
           {"task_id": COVER_TASK, "status": "completed"})
    (root / "localtasks" / COVER_TASK).mkdir(parents=True, exist_ok=True)
    manifest = store.load_manifest(PROJECT)
    manifest["clips"]["clip-001"]["task_history"] = [
        {"task_id": COVER_TASK, "task_type": "REGENERATE_COVER"}]
    store.save_manifest(PROJECT, manifest)
    log = root / "logs" / "local_tasks.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"task_id": COVER_TASK}) + "\n"
                   + json.dumps({"task_id": "cover_other"}) + "\n", encoding="utf-8")

    result = purge.clear_project_tasks(PROJECT, expected_card_count=1, confirm=True)

    assert COVER_TASK in result["cleared_task_ids"]
    assert not (root / "localtasks" / f"{COVER_TASK}.json").exists()
    assert not (root / "localtasks" / COVER_TASK).exists()
    kept = log.read_text(encoding="utf-8")
    assert "cover_other" in kept and COVER_TASK not in kept


# ---------------------------------------------------------------------------
# 4. 清空后卡片不得复活
# ---------------------------------------------------------------------------

def test_cleared_cards_do_not_come_back_after_sync(root):
    """卡片由任务 JSON 反填：只清台账不清任务 JSON，下一次加载就会复活。"""
    _seed(root)

    purge.clear_project_tasks(PROJECT, expected_card_count=1, confirm=True)
    store.sync_from_tasks(PROJECT)

    assert store.list_cards(PROJECT) == []
    assert store.card_clip_ids(PROJECT) == []


# ---------------------------------------------------------------------------
# 5 & 6 & 7. 安全闸门
# ---------------------------------------------------------------------------

def test_active_task_in_processing_blocks_clear(root):
    _seed(root)
    other = "autoedit_bbbbbbbbbbbb"
    _write(root / "processing" / f"{other}.json",
           {"task_id": other, "status": "processing", "bridge": {"project_id": PROJECT}})
    _write(root / "status" / f"{other}.json", {"task_id": other, "status": "processing"})
    store.register_clips(PROJECT, other, [{"clip_id": "clip-002", "title": "二", "duration": 30.0}])

    with pytest.raises(purge.PurgeError) as excinfo:
        purge.clear_project_tasks(PROJECT, expected_card_count=2, confirm=True)

    assert excinfo.value.code == purge.E_PURGE_ACTIVE_TASKS
    assert other in excinfo.value.detail["active_task_ids"]
    # 一个字节都不许动。
    assert (root / "output" / TASK / "clip-001" / "clip_final.mp4").is_file()
    assert len(store.card_clip_ids(PROJECT)) == 2


def test_non_terminal_status_blocks_clear(root):
    """任务 JSON 已不在 in/processing，但状态文件还是非终态 —— 同样拦。"""
    _seed(root)
    _write(root / "status" / f"{TASK}.json", {"task_id": TASK, "status": "transcribing"})

    with pytest.raises(purge.PurgeError) as excinfo:
        purge.clear_project_tasks(PROJECT, expected_card_count=1, confirm=True)

    assert excinfo.value.code == purge.E_PURGE_ACTIVE_TASKS


def test_card_count_mismatch_is_refused(root):
    _seed(root)

    with pytest.raises(purge.PurgeError) as excinfo:
        purge.clear_project_tasks(PROJECT, expected_card_count=3, confirm=True)

    assert excinfo.value.code == purge.E_PURGE_CARD_COUNT_MISMATCH
    assert excinfo.value.detail == {"current_card_count": 1, "expected_card_count": 3}
    assert store.card_clip_ids(PROJECT) == ["clip-001"]


def test_confirm_required(root):
    _seed(root)

    with pytest.raises(purge.PurgeError) as excinfo:
        purge.clear_project_tasks(PROJECT, expected_card_count=1)

    assert excinfo.value.code == purge.E_PURGE_CONFIRM_REQUIRED
    assert store.card_clip_ids(PROJECT) == ["clip-001"]


# ---------------------------------------------------------------------------
# 8 & 9 & 10. 幂等 / 失败还原 / 项目隔离
# ---------------------------------------------------------------------------

def test_clear_is_idempotent(root):
    _seed(root)

    purge.clear_project_tasks(PROJECT, expected_card_count=1, confirm=True)
    second = purge.clear_project_tasks(PROJECT, expected_card_count=0, confirm=True)

    assert second["cleared_card_count"] == 0
    assert second["moved_path_count"] == 0
    assert second["cleared_task_count"] == 0


def test_manifest_write_failure_restores_everything(root, monkeypatch):
    _seed(root)

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "save_manifest", _boom)

    with pytest.raises(purge.PurgeError) as excinfo:
        purge.clear_project_tasks(PROJECT, expected_card_count=1, confirm=True)

    assert excinfo.value.code == purge.E_PURGE_FAILED
    # 全部搬回：任务 JSON、产物、状态一个不少，旧卡片仍可用。
    assert (root / "completed" / f"{TASK}.json").is_file()
    assert (root / "output" / TASK / "clip-001" / "clip_final.mp4").is_file()
    assert store.card_clip_ids(PROJECT) == ["clip-001"]
    assert (root / "media" / TASK / "source.mp4").is_file()


def test_other_projects_are_untouched(root):
    _seed(root)
    other_task = "autoedit_cccccccccccc"
    _seed(root, project=OTHER_PROJECT, task=other_task)

    purge.clear_project_tasks(PROJECT, expected_card_count=1, confirm=True)

    assert store.card_clip_ids(PROJECT) == []
    assert store.card_clip_ids(OTHER_PROJECT) == ["clip-001"]
    assert (root / "output" / other_task / "clip-001" / "clip_final.mp4").is_file()
    assert (root / "media" / other_task / "source.mp4").is_file()


def test_source_registered_only_in_task_json_is_still_protected(root):
    """台账没登记 source_video 时，靠任务 JSON 的 raw_video_path 兜底保护源片。"""
    _seed(root, include_source=False)
    (root / "media" / TASK).mkdir(parents=True, exist_ok=True)
    (root / "media" / TASK / "source.mp4").write_bytes(b"s" * 1024)
    assert (store.load_manifest(PROJECT).get("source_video") is None)

    assert purge.source_media_dirs(PROJECT) == [TASK]
    purge.clear_project_tasks(PROJECT, expected_card_count=1, confirm=True)

    assert (root / "media" / TASK / "source.mp4").is_file(), "源片是这个项目唯一的输入，不能删"
