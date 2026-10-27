"""``timeline_history`` 单元测试：push/undo/redo 语义、redo 分支、幂等、上限、落盘。

纯逻辑，不依赖真实媒体、不依赖数据库。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import timeline_history as th


def _state(segments: list[str], tracks: list[dict] | None = None) -> dict:
    return {
        "ranges": [{"id": item, "source": "A001"} for item in segments],
        "audio_tracks": list(tracks or []),
    }


def _bgm(track_id: str = "bgm_1", volume: float = 1.0) -> dict:
    return {"track_id": track_id, "kind": "bgm", "asset_id": "BGM1",
            "source_start_s": 0.0, "source_end_s": 5.0, "record_start_s": 1.0,
            "volume": volume, "muted": False, "fade_in_s": 0.0, "fade_out_s": 0.0}


# ---------------------------------------------------------------------------
# 空历史 / push
# ---------------------------------------------------------------------------

def test_empty_history_is_not_undoable(tmp_path: Path):
    assert th.load_history(tmp_path)["revisions"] == []
    assert th.can_undo(tmp_path) is False
    assert th.can_redo(tmp_path) is False
    assert th.current_revision(tmp_path) is None
    assert th.current_revision_number(tmp_path) == 0
    summary = th.history_summary(tmp_path)
    assert summary["total"] == 0
    assert summary["index"] == -1
    assert summary["can_undo"] is False and summary["can_redo"] is False


def test_push_creates_first_revision(tmp_path: Path):
    revision = th.push(tmp_path, _state(["t1"]), label="初始版本")
    assert revision.revision == 1
    assert revision.label == "初始版本"
    assert revision.updated_at.endswith("Z")
    assert revision.content_hash
    assert th.can_undo(tmp_path) is False
    assert th.can_redo(tmp_path) is False
    assert th.current_revision_number(tmp_path) == 1
    assert th.current_revision(tmp_path) == revision


def test_push_assigns_monotonic_numbers(tmp_path: Path):
    assert [th.push(tmp_path, _state([f"t{i}"]), label=f"step{i}").revision
            for i in range(1, 4)] == [1, 2, 3]
    summary = th.history_summary(tmp_path)
    assert summary["total"] == 3
    assert summary["index"] == 2
    assert summary["undo_depth"] == 2
    assert summary["redo_depth"] == 0
    assert summary["labels"] == ["step1", "step2", "step3"]


def test_push_ignores_caller_supplied_revision_number(tmp_path: Path):
    assert th.push(tmp_path, {**_state(["t1"]), "revision": 99}).revision == 1
    assert th.push(tmp_path, {**_state(["t2"]), "revision": 5}).revision == 2


def test_push_persists_ranges_and_tracks(tmp_path: Path):
    th.push(tmp_path, {"ranges": [{"id": "t1"}], "audio_tracks": [_bgm()],
                       "subtitles": [{"start": 0.0, "end": 1.0, "text": "嗨"}]},
            label="加字幕")
    current = th.current_revision(tmp_path)
    assert current.ranges == [{"id": "t1"}]
    assert current.audio_tracks == [_bgm()]
    assert current.subtitles == [{"start": 0.0, "end": 1.0, "text": "嗨"}]


def test_push_rejects_invalid_payload(tmp_path: Path):
    with pytest.raises(th.TimelineHistoryError) as excinfo:
        th.push(tmp_path, {"ranges": "not-a-list"})
    assert excinfo.value.code == th.E_TIMELINE_REVISION_INVALID
    assert th.E_TIMELINE_REVISION_INVALID in str(excinfo.value)
    assert not th.history_path(tmp_path).exists()


@pytest.mark.parametrize("payload", [[], "state", 3, None])
def test_push_rejects_non_dict(tmp_path: Path, payload):
    with pytest.raises(th.TimelineHistoryError) as excinfo:
        th.push(tmp_path, payload)
    assert excinfo.value.code == th.E_TIMELINE_REVISION_INVALID


def test_push_rejects_non_dict_track_elements(tmp_path: Path):
    with pytest.raises(th.TimelineHistoryError) as excinfo:
        th.push(tmp_path, {"ranges": [], "audio_tracks": ["bgm"]})
    assert excinfo.value.code == th.E_TIMELINE_REVISION_INVALID


def test_push_accepts_timeline_revision_object(tmp_path: Path):
    model = th.TimelineRevision(revision=1, updated_at="2026-01-01T00:00:00Z",
                                ranges=[{"id": "t1"}], audio_tracks=[])
    assert th.push(tmp_path, model).ranges == [{"id": "t1"}]


# ---------------------------------------------------------------------------
# undo / redo
# ---------------------------------------------------------------------------

def test_undo_and_redo_move_pointer(tmp_path: Path):
    th.push(tmp_path, _state(["t1"]), label="step1")
    th.push(tmp_path, _state(["t1", "t2"]), label="step2")

    assert th.undo(tmp_path).revision == 1
    assert th.can_undo(tmp_path) is False
    assert th.can_redo(tmp_path) is True
    assert th.current_revision(tmp_path).label == "step1"

    assert th.redo(tmp_path).revision == 2
    assert th.can_undo(tmp_path) is True
    assert th.can_redo(tmp_path) is False


def test_undo_restores_previous_payload(tmp_path: Path):
    th.push(tmp_path, {"ranges": [{"id": "t1"}], "audio_tracks": []}, label="base")
    th.push(tmp_path, {"ranges": [{"id": "t1"}, {"id": "t2"}], "audio_tracks": [_bgm()]},
            label="add bgm")
    rolled = th.undo(tmp_path)
    assert rolled.audio_tracks == []
    assert rolled.ranges == [{"id": "t1"}]
    assert th.revision_payload(rolled) == {
        "ranges": [{"id": "t1"}], "audio_tracks": [], "subtitles": None}


def test_undo_without_history_raises(tmp_path: Path):
    with pytest.raises(th.TimelineHistoryError) as excinfo:
        th.undo(tmp_path)
    assert excinfo.value.code == th.E_TIMELINE_UNDO_UNAVAILABLE
    assert th.E_TIMELINE_UNDO_UNAVAILABLE in str(excinfo.value)


def test_redo_without_future_raises(tmp_path: Path):
    th.push(tmp_path, _state(["t1"]))
    with pytest.raises(th.TimelineHistoryError) as excinfo:
        th.redo(tmp_path)
    assert excinfo.value.code == th.E_TIMELINE_REDO_UNAVAILABLE


def test_undo_at_oldest_raises_and_keeps_state(tmp_path: Path):
    th.push(tmp_path, _state(["t1"]))
    th.push(tmp_path, _state(["t2"]))
    th.undo(tmp_path)
    with pytest.raises(th.TimelineHistoryError):
        th.undo(tmp_path)
    assert th.current_revision_number(tmp_path) == 1
    assert th.can_redo(tmp_path) is True


def test_multiple_undo_redo_cycles(tmp_path: Path):
    for index in range(1, 6):
        th.push(tmp_path, _state([f"t{index}"]), label=f"step{index}")
    for _ in range(4):
        th.undo(tmp_path)
    assert th.current_revision_number(tmp_path) == 1
    assert th.can_undo(tmp_path) is False
    th.redo(tmp_path)
    th.redo(tmp_path)
    assert th.current_revision_number(tmp_path) == 3


def test_can_undo_can_redo_after_each_operation(tmp_path: Path):
    th.push(tmp_path, _state(["t1"]))
    assert (th.can_undo(tmp_path), th.can_redo(tmp_path)) == (False, False)
    th.push(tmp_path, _state(["t2"]))
    assert (th.can_undo(tmp_path), th.can_redo(tmp_path)) == (True, False)
    th.undo(tmp_path)
    assert (th.can_undo(tmp_path), th.can_redo(tmp_path)) == (False, True)


# ---------------------------------------------------------------------------
# redo 分支清理
# ---------------------------------------------------------------------------

def test_new_push_clears_redo_branch(tmp_path: Path):
    th.push(tmp_path, _state(["t1"]), label="step1")
    th.push(tmp_path, _state(["t2"]), label="step2")
    th.push(tmp_path, _state(["t3"]), label="step3")
    th.undo(tmp_path)
    th.undo(tmp_path)

    th.push(tmp_path, _state(["t1", "t9"]), label="branch")
    summary = th.history_summary(tmp_path)
    assert summary["total"] == 2
    assert summary["labels"] == ["step1", "branch"]
    assert summary["can_redo"] is False
    assert th.current_revision_number(tmp_path) == 2
    # 版本号单调递增，不复用被丢弃的 3。
    assert th.push(tmp_path, _state(["t10"]), label="next").revision == 3


def test_undo_then_push_then_redo_is_unavailable(tmp_path: Path):
    th.push(tmp_path, _state(["t1"]))
    th.push(tmp_path, _state(["t2"]))
    th.undo(tmp_path)
    th.push(tmp_path, _state(["t2b"]))
    with pytest.raises(th.TimelineHistoryError) as excinfo:
        th.redo(tmp_path)
    assert excinfo.value.code == th.E_TIMELINE_REDO_UNAVAILABLE


# ---------------------------------------------------------------------------
# 幂等（内容哈希）
# ---------------------------------------------------------------------------

def test_push_same_content_is_idempotent(tmp_path: Path):
    first = th.push(tmp_path, _state(["t1"]), label="第一次")
    second = th.push(tmp_path, _state(["t1"]), label="同样内容")
    assert second.revision == first.revision
    assert second.content_hash == first.content_hash
    summary = th.history_summary(tmp_path)
    assert summary["total"] == 1
    assert summary["labels"] == ["第一次"]  # 标签也不被改写


def test_idempotent_push_keeps_redo_branch(tmp_path: Path):
    th.push(tmp_path, _state(["t1"]))
    th.push(tmp_path, _state(["t2"]))
    th.undo(tmp_path)
    # 内容与「当前版本」相同 → 没有编辑发生，redo 分支必须保留。
    th.push(tmp_path, _state(["t1"]), label="空操作")
    assert th.can_redo(tmp_path) is True
    assert th.history_summary(tmp_path)["total"] == 2


def test_idempotent_push_is_key_order_insensitive(tmp_path: Path):
    th.push(tmp_path, {"ranges": [{"id": "t1", "track": 1}], "audio_tracks": []})
    th.push(tmp_path, {"ranges": [{"track": 1, "id": "t1"}], "audio_tracks": []})
    assert th.history_summary(tmp_path)["total"] == 1


def test_content_hash_ignores_meta_but_tracks_edits(tmp_path: Path):
    base = {"ranges": [{"id": "t1"}], "audio_tracks": []}
    assert th.content_hash(base) == th.content_hash(dict(base))
    assert th.content_hash(base) != th.content_hash({**base, "audio_tracks": [_bgm()]})
    assert th.content_hash(base) != th.content_hash({**base, "subtitles": [{"text": "x"}]})


def test_content_hash_ignores_timestamps_and_label():
    base = {"ranges": [], "audio_tracks": []}
    decorated = {**base, "revision": 7, "updated_at": "2030-01-01T00:00:00Z", "label": "x"}
    assert th.content_hash(base) == th.content_hash(decorated)


def test_extra_keys_participate_in_hash(tmp_path: Path):
    """Phase B 的扩展键（如 visual_tracks）变化必须产生新版本。"""

    th.push(tmp_path, {"ranges": [], "audio_tracks": [], "visual_tracks": [{"id": "img1"}]})
    th.push(tmp_path, {"ranges": [], "audio_tracks": [], "visual_tracks": [{"id": "img2"}]})
    assert th.history_summary(tmp_path)["total"] == 2
    assert th.current_revision(tmp_path).visual_tracks == [{"id": "img2"}]


def test_revision_payload_strips_meta_keys():
    payload = th.revision_payload({
        "revision": 3, "updated_at": "2026-01-01T00:00:00Z", "label": "x",
        "content_hash": "abc", "ranges": [{"id": "t1"}], "audio_tracks": [],
    })
    assert payload == {"ranges": [{"id": "t1"}], "audio_tracks": []}


def test_revision_payload_rejects_non_dict():
    with pytest.raises(th.TimelineHistoryError) as excinfo:
        th.revision_payload(["ranges"])
    assert excinfo.value.code == th.E_TIMELINE_REVISION_INVALID


# ---------------------------------------------------------------------------
# 上限策略（丢弃最旧，revision 号不回收）
# ---------------------------------------------------------------------------

def test_limit_drops_oldest_and_keeps_numbers_monotonic(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(th, "HISTORY_LIMIT", 3)
    for index in range(1, 6):
        th.push(tmp_path, _state([f"t{index}"]), label=f"step{index}")

    summary = th.history_summary(tmp_path)
    assert summary["limit"] == 3
    assert summary["total"] == 3
    assert summary["labels"] == ["step3", "step4", "step5"]
    assert summary["current_revision"] == 5
    assert summary["undo_depth"] == 2  # 可撤销 2 步
    assert summary["redo_depth"] == 0

    stored = th.load_history(tmp_path)
    assert [item["revision"] for item in stored["revisions"]] == [3, 4, 5]


def test_limit_bounds_undo_depth(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(th, "HISTORY_LIMIT", 4)
    for index in range(8):
        th.push(tmp_path, _state([f"t{index}"]))
    undos = 0
    while th.can_undo(tmp_path):
        th.undo(tmp_path)
        undos += 1
    assert undos == 3
    # 丢弃最旧后仍保留单调递增的 5..8 号，撤销到底就是 5 号。
    assert th.current_revision_number(tmp_path) == 5
    assert [item["revision"] for item in th.load_history(tmp_path)["revisions"]] == [5, 6, 7, 8]


def test_stored_limit_is_capped_by_constant(tmp_path: Path):
    path = th.history_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 1, "limit": 10_000, "index": 0,
        "revisions": [{"revision": 1, "updated_at": "2026-01-01T00:00:00Z",
                       "label": "x", "content_hash": "h", "ranges": [], "audio_tracks": []}],
    }), encoding="utf-8")
    assert th.load_history(tmp_path)["limit"] == th.HISTORY_LIMIT


# ---------------------------------------------------------------------------
# 落盘 / 回读 / 容错
# ---------------------------------------------------------------------------

def test_history_path(tmp_path: Path):
    assert th.history_path(tmp_path) == tmp_path / "timeline" / "history.json"


def test_history_is_atomically_written(tmp_path: Path):
    th.push(tmp_path, _state(["t1"]))
    path = th.history_path(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == th.HISTORY_SCHEMA_VERSION
    assert payload["index"] == 0
    assert len(payload["revisions"]) == 1
    assert payload["revisions"][0]["content_hash"]
    assert [item.name for item in path.parent.iterdir() if item.name != "history.json"] == []


def test_state_survives_reload(tmp_path: Path):
    th.push(tmp_path, _state(["t1"]), label="step1")
    th.push(tmp_path, _state(["t2"]), label="step2")
    th.undo(tmp_path)
    # 重新读盘（模拟进程重启）后指针位置保持。
    reloaded = th.load_history(tmp_path)
    assert reloaded["index"] == 0
    assert len(reloaded["revisions"]) == 2


def test_undo_redo_persist_pointer(tmp_path: Path):
    th.push(tmp_path, _state(["t1"]))
    th.push(tmp_path, _state(["t2"]))
    th.undo(tmp_path)
    assert json.loads(th.history_path(tmp_path).read_text(encoding="utf-8"))["index"] == 0
    th.redo(tmp_path)
    assert json.loads(th.history_path(tmp_path).read_text(encoding="utf-8"))["index"] == 1


def test_corrupt_history_raises_code(tmp_path: Path):
    path = th.history_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(th.TimelineHistoryError) as excinfo:
        th.load_history(tmp_path)
    assert excinfo.value.code == th.E_TIMELINE_HISTORY_INVALID
    assert th.E_TIMELINE_HISTORY_INVALID in str(excinfo.value)


@pytest.mark.parametrize("payload", [
    {"revisions": "nope"},
    {"revisions": ["nope"]},
    {"revisions": [{"revision": 0, "ranges": [], "audio_tracks": []}]},
    {"revisions": [{"updated_at": "x", "ranges": [], "audio_tracks": []}]},
    {"revisions": [{"revision": 1, "ranges": [], "audio_tracks": []}], "index": 5},
])
def test_invalid_history_document_raises_code(tmp_path: Path, payload):
    path = th.history_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(th.TimelineHistoryError) as excinfo:
        th.load_history(tmp_path)
    assert excinfo.value.code == th.E_TIMELINE_HISTORY_INVALID


def test_save_history_roundtrip(tmp_path: Path):
    history = {"limit": 5, "index": 0, "revisions": [
        {"revision": 1, "updated_at": "2026-01-01T00:00:00Z", "label": "x",
         "content_hash": "h", "ranges": [], "audio_tracks": []}]}
    th.save_history(tmp_path, history)
    loaded = th.load_history(tmp_path)
    assert loaded["limit"] == 5
    assert loaded["revisions"][0]["label"] == "x"


def test_save_history_rejects_non_dict(tmp_path: Path):
    with pytest.raises(th.TimelineHistoryError):
        th.save_history(tmp_path, ["nope"])


def test_clear_history(tmp_path: Path):
    th.push(tmp_path, _state(["t1"]))
    th.clear_history(tmp_path)
    assert th.load_history(tmp_path)["revisions"] == []
    assert th.can_undo(tmp_path) is False
    assert th.current_revision(tmp_path) is None
    assert th.push(tmp_path, _state(["t9"])).revision == 1


# ---------------------------------------------------------------------------
# summary / 模型
# ---------------------------------------------------------------------------

def test_history_summary_shape(tmp_path: Path):
    th.push(tmp_path, _state(["t1"]), label="a")
    th.push(tmp_path, _state(["t2"]), label="b")
    th.undo(tmp_path)
    summary = th.history_summary(tmp_path)
    assert summary == {
        "version": th.HISTORY_SCHEMA_VERSION,
        "limit": th.HISTORY_LIMIT,
        "total": 2,
        "index": 0,
        "current_revision": 1,
        "current_label": "a",
        "undo_depth": 0,
        "redo_depth": 1,
        "can_undo": False,
        "can_redo": True,
        "labels": ["a", "b"],
    }


def test_history_summary_does_not_write(tmp_path: Path):
    th.push(tmp_path, _state(["t1"]))
    before = th.history_path(tmp_path).read_text(encoding="utf-8")
    th.history_summary(tmp_path)
    th.can_undo(tmp_path)
    th.can_redo(tmp_path)
    assert th.history_path(tmp_path).read_text(encoding="utf-8") == before


def test_revision_model_requires_positive_integer():
    with pytest.raises(Exception):
        th.TimelineRevision(revision=0)
    assert th.TimelineRevision(revision=2).ranges == []


def test_revision_model_subtitles_default_none():
    assert th.TimelineRevision(revision=1).subtitles is None
    assert th.TimelineRevision(revision=1, subtitles=[]).subtitles == []


def test_can_undo_in_and_can_redo_in_match_disk(tmp_path: Path):
    th.push(tmp_path, _state(["t1"]))
    th.push(tmp_path, _state(["t2"]))
    history = th.load_history(tmp_path)
    th.undo(tmp_path)
    assert th.can_undo_in(th.load_history(tmp_path)) is False
    assert th.can_redo_in(th.load_history(tmp_path)) is True
    assert th.can_undo_in(history) is True
