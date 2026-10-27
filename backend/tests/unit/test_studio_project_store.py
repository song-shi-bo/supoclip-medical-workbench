"""studio_project_store 单元测试：Project → SourceVideo → Clip 台账（纯文件系统）。

覆盖「一条 clip 一张卡」的数据结构保证：
1. register_clips 幂等 —— 同一 clip_id 重复登记不新增条目、且 render_task_id 不被改写；
2. card_view 输出封面版本号 / 可撤销标记；
3. bump_cover 推进 revision 并保留历史（可撤销）；
4. set_cover_pointer 只改「当前展示版本」、不新增 revision；
5. update_fields 不允许覆盖关键结构字段；
6. record_task 去重并追加任务历史；
7. 非法 project_id / clip_id 显式报 E_ 码。
"""

from __future__ import annotations

import json

import pytest

from src import studio_project_store as store


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "TASKS_ROOT", tmp_path / "tasks")
    return "timeline_abc123def456"


def _clips(task_id: str, count: int = 2) -> list[dict]:
    return [
        {
            "clip_id": f"clip-00{index + 1}",
            "name": f"剪辑成片 {index + 1:02d}",
            "title": f"主题{index + 1}",
            "topic": "健康科普",
            "source_start": 0.14 + index * 10,
            "source_end": 16.3 + index * 10,
            "duration": 16.96,
            "cover_path": f"output/{task_id}/clip-00{index + 1}/cover.jpg",
            "cover_bytes": 182035,
        }
        for index in range(count)
    ]


# ---------------------------------------------------------------------------
# 登记与去重：这是「不多出重复卡片」的核心
# ---------------------------------------------------------------------------

def test_register_clips_creates_one_entry_per_clip_id(project):
    manifest = store.register_clips(project, "autoedit_aaaaaaaaaaaa", _clips("autoedit_aaaaaaaaaaaa"))
    assert manifest["clip_order"] == ["clip-001", "clip-002"]
    assert set(manifest["clips"]) == {"clip-001", "clip-002"}
    assert manifest["clips"]["clip-001"]["render_task_id"] == "autoedit_aaaaaaaaaaaa"


def test_register_clips_is_idempotent_and_keeps_render_task(project):
    """第二次（例如复渲染任务回传同一条 clip）不得新增卡片、不得改 render_task_id。"""
    store.register_clips(project, "autoedit_aaaaaaaaaaaa", _clips("autoedit_aaaaaaaaaaaa"))
    manifest = store.register_clips(project, "autoedit_bbbbbbbbbbbb", _clips("autoedit_bbbbbbbbbbbb"))

    assert manifest["clip_order"] == ["clip-001", "clip-002"]
    # 关键：卡片仍指向首渲任务，封面路径不被新任务覆盖。
    assert manifest["clips"]["clip-001"]["render_task_id"] == "autoedit_aaaaaaaaaaaa"
    assert manifest["clips"]["clip-001"]["cover"]["path"] == (
        "output/autoedit_aaaaaaaaaaaa/clip-001/cover.jpg")
    assert len(manifest["clips"]) == 2


def test_register_clips_skips_invalid_clip_ids(project):
    manifest = store.register_clips(project, "autoedit_aaaaaaaaaaaa", [
        {"clip_id": "../escape"}, {"clip_id": ""}, {"clip_id": "clip-ok", "duration": 3},
    ])
    assert list(manifest["clips"]) == ["clip-ok"]


def test_clip_names_are_project_unique_across_regenerations(project):
    """同一项目跑第二次自动剪辑时，卡片名不得重复（否则用户无法分辨哪张是哪张）。"""
    store.register_clips(project, "autoedit_aaaaaaaaaaaa", _clips("autoedit_aaaaaaaaaaaa"))
    store.register_clips(project, "autoedit_cccccccccccc", [
        {"clip_id": "clip-003", "name": "剪辑成片 01", "duration": 5},
        {"clip_id": "clip-004", "name": "剪辑成片 02", "duration": 5},
    ])
    names = [card["name"] for card in store.list_cards(project)]
    assert names == ["剪辑成片 01", "剪辑成片 02", "剪辑成片 03", "剪辑成片 04"]
    assert len(set(names)) == 4


def test_register_clips_creates_v1_cover_asset(project):
    store.register_clips(project, "autoedit_aaaaaaaaaaaa", _clips("autoedit_aaaaaaaaaaaa"))
    card = store.list_cards(project)[0]
    assert card["cover"]["revision"] == 1
    assert card["cover"]["cover_asset_id"] == "cover-clip-001-v1"
    assert card["cover"]["can_undo"] is False
    assert card["task_id"] == "autoedit_aaaaaaaaaaaa"


# ---------------------------------------------------------------------------
# 封面修订：revision+1 / 历史 / 撤销指针
# ---------------------------------------------------------------------------

def test_bump_cover_increments_revision_and_keeps_history(project):
    store.register_clips(project, "autoedit_aaaaaaaaaaaa", _clips("autoedit_aaaaaaaaaaaa"))
    store.bump_cover(project, "clip-001", task_id="cover_111111111111",
                     style_id="realframe_v2", cover_path="output/x/clip-001/cover.jpg",
                     sha256="deadbeef", bytes_=1234, intro_burned=True, kind="generated")

    card = store.list_cards(project)[0]
    assert card["cover"]["revision"] == 2
    assert card["cover"]["cover_asset_id"] == "cover-clip-001-v2"
    assert card["cover"]["style_id"] == "realframe_v2"
    assert [h["revision"] for h in card["cover"]["history"]] == [1, 2]
    assert card["cover"]["can_undo"] is True
    assert card["cover"]["undo_revision"] == 1


def test_set_cover_pointer_switches_active_revision_without_bumping(project):
    store.register_clips(project, "autoedit_aaaaaaaaaaaa", _clips("autoedit_aaaaaaaaaaaa"))
    store.bump_cover(project, "clip-001", task_id="cover_111111111111",
                     style_id="realframe_v2", cover_path="output/x/cover.jpg",
                     sha256="new", bytes_=42, intro_burned=True, kind="generated")
    store.set_cover_pointer(project, "clip-001", revision=1, task_id="cover_111111111111")

    card = store.list_cards(project)[0]
    assert card["cover"]["revision"] == 2          # 最新版本仍是 v2
    assert card["cover"]["active_revision"] == 1   # 但当前展示回到 v1
    assert card["cover"]["cover_asset_id"] == "cover-clip-001-v1"


def test_set_cover_pointer_rejects_unknown_revision(project):
    store.register_clips(project, "autoedit_aaaaaaaaaaaa", _clips("autoedit_aaaaaaaaaaaa"))
    with pytest.raises(store.ProjectStoreError) as excinfo:
        store.set_cover_pointer(project, "clip-001", revision=7, task_id="x")
    assert excinfo.value.code == "E_COVER_REVISION_UNKNOWN"


# ---------------------------------------------------------------------------
# 局部字段更新与任务历史
# ---------------------------------------------------------------------------

def test_update_fields_cannot_overwrite_structural_keys(project):
    store.register_clips(project, "autoedit_aaaaaaaaaaaa", _clips("autoedit_aaaaaaaaaaaa"))
    store.update_fields(project, "clip-001", {
        "title": "新标题",
        "render_task_id": "hacked",
        "cover": {"revision": 99},
        "clip_id": "hacked",
    })
    entry = store.load_manifest(project)["clips"]["clip-001"]
    assert entry["title"] == "新标题"
    assert entry["render_task_id"] == "autoedit_aaaaaaaaaaaa"
    assert entry["cover"]["revision"] == 1
    assert entry["clip_id"] == "clip-001"


def test_record_task_dedupes_by_task_id(project):
    store.register_clips(project, "autoedit_aaaaaaaaaaaa", _clips("autoedit_aaaaaaaaaaaa"))
    for status in ("queued", "completed"):
        store.record_task(project, "clip-001", {
            "task_id": "cover_111111111111", "task_type": "REGENERATE_COVER",
            "status": status, "created_at": "2026-10-12T00:00:00Z",
        })
    history = store.load_manifest(project)["clips"]["clip-001"]["task_history"]
    assert len(history) == 1
    assert history[0]["status"] == "completed"


def test_bump_render_revision_only_touches_that_clip(project):
    store.register_clips(project, "autoedit_aaaaaaaaaaaa", _clips("autoedit_aaaaaaaaaaaa"))
    store.bump_render_revision(project, "clip-002", task_id="edit_222222222222",
                               kind="UPDATE_TIMELINE")
    manifest = store.load_manifest(project)
    assert manifest["clips"]["clip-002"]["render_revision"] == 2
    assert manifest["clips"]["clip-001"]["render_revision"] == 1


# ---------------------------------------------------------------------------
# 校验与容错
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("project_id", ["", "abc", "timeline_", "timeline_../../etc", "timeline_a/b"])
def test_invalid_project_ids_are_rejected(project_id):
    with pytest.raises(store.ProjectStoreError) as excinfo:
        store.validate_project_id(project_id)
    assert excinfo.value.code == "E_PROJECT_ID_INVALID"


@pytest.mark.parametrize("clip_id", ["", "../x", "a/b", "-bad", "x" * 80])
def test_invalid_clip_ids_are_rejected(clip_id):
    with pytest.raises(store.ProjectStoreError) as excinfo:
        store.validate_clip_id(clip_id)
    assert excinfo.value.code == "E_CLIP_ID_INVALID"


def test_register_source_video_records_hash_and_size(project, tmp_path):
    media = store.TASKS_ROOT / "media" / "autoedit_aaaaaaaaaaaa" / "source.mp4"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"SOURCE-VIDEO-BYTES")
    manifest = store.register_source_video(project, path="media/autoedit_aaaaaaaaaaaa/source.mp4")
    source = manifest["source_video"]
    assert source["path"].endswith("source.mp4")
    assert source["bytes"] == len(b"SOURCE-VIDEO-BYTES")
    assert source["sha256"] == store.sha256_file(media)
    # 重复登记保留首次的 registered_at，不重复计算
    again = store.register_source_video(project, path="media/autoedit_aaaaaaaaaaaa/source.mp4")
    assert again["source_video"]["registered_at"] == source["registered_at"]


def test_load_manifest_of_missing_project_is_empty_not_error(project):
    manifest = store.load_manifest(project)
    assert manifest["clips"] == {} and manifest["clip_order"] == []


def test_corrupt_manifest_does_not_raise(project):
    store.register_clips(project, "autoedit_aaaaaaaaaaaa", _clips("autoedit_aaaaaaaaaaaa"))
    store.manifest_path(project).write_text("{not json", encoding="utf-8")
    assert store.list_cards(project) == []


def test_card_view_exposes_video_path(project):
    """卡片自带成片路径：前端不必再从 cover.path 反推（契约缺口 2026-10 已补）。"""
    store.register_clips(project, "autoedit_aaaaaaaaaaaa", _clips("autoedit_aaaaaaaaaaaa"))
    card = store.list_cards(project)[0]
    assert card["video_path"] == "output/autoedit_aaaaaaaaaaaa/clip-001/clip_final.mp4"
    assert card["cover_path"] == "output/autoedit_aaaaaaaaaaaa/clip-001/cover.jpg"


def test_card_view_video_path_prefers_staged_layout(project):
    """新渲染管线布局（output/{task}/clips/{clip}/）存在时必须优先返回。

    回归 2026-10-16：reedit/新链成片落在 clips/ 子层，卡片公式少一层导致
    播放 404/无画面（timeline_790a6b0c9d87 的 clip-6e8dda26f6）。
    """
    job_id = "192a7b28a47c41059fb147651593d480"
    staged = (store.TASKS_ROOT / "output" / job_id / "clips" / "clip-x1"
              / store.CLIP_FILENAME)
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"0")
    card = store.card_view({"clip_id": "clip-x1", "render_task_id": job_id})
    assert card["video_path"] == f"output/{job_id}/clips/clip-x1/{store.CLIP_FILENAME}"


def test_card_view_video_path_layout_fallbacks(project):
    """只有旧 bridge 布局时返回旧路径；两者皆无时保持旧公式（行为不变）。"""
    legacy = (store.TASKS_ROOT / "output" / "autoedit_aaaaaaaaaaaa" / "clip-x1"
              / store.CLIP_FILENAME)
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"0")
    card = store.card_view({"clip_id": "clip-x1", "render_task_id": "autoedit_aaaaaaaaaaaa"})
    assert card["video_path"] == "output/autoedit_aaaaaaaaaaaa/clip-x1/" + store.CLIP_FILENAME
    missing = store.card_view({"clip_id": "clip-x2", "render_task_id": "autoedit_aaaaaaaaaaaa"})
    assert missing["video_path"] == "output/autoedit_aaaaaaaaaaaa/clip-x2/" + store.CLIP_FILENAME


# ---------------------------------------------------------------------------
# Inspector Phase 1：source_config / 配置投影 / 投影字段只读
# ---------------------------------------------------------------------------

def test_empty_and_legacy_manifest_default_source_config(project):
    """空台账与老 clips.json 都必须带顶层 source_config（读路径永不抛错）。"""
    assert store.load_manifest(project)["source_config"] == {}

    store.manifest_path(project).parent.mkdir(parents=True, exist_ok=True)
    store.manifest_path(project).write_text(json.dumps({
        "schema_version": 1, "project_id": project, "clip_order": ["clip-old"],
        "clips": {"clip-old": {"clip_id": "clip-old", "title": "老成片"}},
    }, ensure_ascii=False), encoding="utf-8")
    manifest = store.load_manifest(project)
    assert manifest["source_config"] == {}
    # 老条目没有任何 Phase 1 键 → card_view 补齐默认值、不抛错、既有键不变
    card = store.card_view(manifest["clips"]["clip-old"])
    assert card["title"] == "老成片" and card["render_revision"] == 1
    assert card["subtitle_config"]["font_size_px"] == 44
    assert card["effects"]["export_resolution"] == "1080x1920"
    assert card["transform"] is None and card["audio_tracks"] == []
    assert card["cover_asset_id"] is None and card["cover_revision"] == 0
    # 损坏的 source_config → 读成空字典，不抛错
    store.manifest_path(project).write_text(json.dumps(
        {"clips": {}, "source_config": "broken"}), encoding="utf-8")
    assert store.load_manifest(project)["source_config"] == {}


def test_save_manifest_preserves_source_config(project):
    manifest = store.load_manifest(project)
    manifest["source_config"] = {"voice_volume": 0.5}
    store.save_manifest(project, manifest)
    assert store.load_manifest(project)["source_config"] == {"voice_volume": 0.5}
    # 之后任何登记/局部更新都不得抹掉它
    store.register_clips(project, "autoedit_aaaaaaaaaaaa", _clips("autoedit_aaaaaaaaaaaa"))
    assert store.load_manifest(project)["source_config"] == {"voice_volume": 0.5}


def test_update_source_config_partial_merge_is_atomic(project):
    store.update_source_config(project, {"voice_volume": 0.4})
    assert store.source_config(project)["voice_volume"] == 0.4
    # 第二次只合并给过的段，之前的段保留
    store.update_source_config(project, {"subtitle_config": {"font_size_px": 70}})
    stored = store.load_manifest(project)["source_config"]
    assert stored["voice_volume"] == 0.4
    assert stored["subtitle_config"]["font_size_px"] == 70
    # 返回的是补齐默认值的完整配置
    normalized = store.source_config(project)
    assert normalized["muted"] is False and normalized["transform"] is None
    assert set(normalized) == {"title", "subtitle_config", "subtitle_overrides", "transform",
                               "audio_tracks", "voice_volume", "muted", "effects"}
    # 没有 clip 也能读写（哨兵场景：未选成片）
    assert store.list_cards(project) == []


def test_update_source_config_rejects_invalid_patch_without_writing(project):
    store.update_source_config(project, {"voice_volume": 0.4})
    before = store.manifest_path(project).read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        store.update_source_config(project, {"voice_volume": 5})
    assert store.manifest_path(project).read_text(encoding="utf-8") == before


def test_with_clip_defaults_returns_copy_with_cover_projection(project):
    store.register_clips(project, "autoedit_aaaaaaaaaaaa", _clips("autoedit_aaaaaaaaaaaa"))
    entry = store.load_manifest(project)["clips"]["clip-001"]
    filled = store.with_clip_defaults(entry)
    assert filled is not entry
    assert filled["subtitle_config"]["font_size_px"] == 44
    assert filled["transcript_revision"] == 0
    assert filled["cover_asset_id"] == "cover-clip-001-v1"
    assert filled["cover_revision"] == 1
    # 投影只读：不落盘副本
    on_disk = store.load_manifest(project)["clips"]["clip-001"]
    assert "cover_asset_id" not in on_disk and "cover_revision" not in on_disk
    assert "subtitle_config" not in on_disk


def test_card_view_exposes_phase1_config_sections(project):
    store.register_clips(project, "autoedit_aaaaaaaaaaaa", _clips("autoedit_aaaaaaaaaaaa"))
    card = store.list_cards(project)[0]
    assert card["subtitle_config"]["position"] == {"x_pct": 50.0, "y_pct": 78.125}
    assert card["subtitle_overrides"] == [] and card["audio_tracks"] == []
    assert card["effects"]["transition"] == {"enabled": False, "type": "none", "duration_s": 0.5}
    assert card["voice_volume"] == 1.0 and card["muted"] is False
    assert card["transform"] is None and card["transcript_revision"] == 0
    assert card["cover_asset_id"] == "cover-clip-001-v1" and card["cover_revision"] == 1


def test_update_fields_refuses_projection_keys(project):
    store.register_clips(project, "autoedit_aaaaaaaaaaaa", _clips("autoedit_aaaaaaaaaaaa"))
    store.update_fields(project, "clip-001", {
        "title": "新标题",
        "cover_asset_id": "cover-clip-001-v99",
        "cover_revision": 99,
        "subtitle_config": {"font_size_px": 70},
    })
    entry = store.load_manifest(project)["clips"]["clip-001"]
    assert entry["title"] == "新标题"
    assert "cover_asset_id" not in entry and "cover_revision" not in entry
    assert entry["cover"]["revision"] == 1
    assert entry["subtitle_config"] == {"font_size_px": 70}


def test_authoritative_staged_missing_clip_never_returns_existing_legacy(project):
    run="f"*32
    legacy=store.TASKS_ROOT/"output"/run/"clip-missing"/store.CLIP_FILENAME
    legacy.parent.mkdir(parents=True);legacy.write_bytes(b"wrong-old-instance")
    (store.TASKS_ROOT/"output"/run/"clips").mkdir()
    card=store.card_view({"clip_id":"clip-missing","render_task_id":run})
    assert card["video_path"] == f"output/{run}/clips/clip-missing/{store.CLIP_FILENAME}"
    assert not (store.TASKS_ROOT/card["video_path"]).exists()
