"""clip_config 单元测试（Inspector Phase 1，纯文件系统、不联网、不跑 ffmpeg）。

覆盖契约 §3.1 的全部语义：
1. 默认值补齐（老 clips.json 没有新键也能读、card_view 不抛错、形状不破坏）；
2. 部分更新：逐段深合并、只写显式段、值相等不算变化；
3. 严格校验：类型错 / 未知段 / 未知键 / 越界 / 坏枚举 / 坏颜色 / 坏 transform
   → ClipConfigError（E_ 码 + 中文原因），且**失败不写盘**；
4. 版本号：只有当「需要重渲的段」真的变化时才 bump（title 永不 bump）；
5. clip / source 双绑定与 render_config_summary 的来源（user/project/skill_default）；
6. **不触发 AUTO_EDIT**：update_config 是纯配置写，不建任何 autoedit_* 任务；
   transform 变化只镜像既有 sidecar。
"""

from __future__ import annotations

import json
import re

import pytest

from src import clip_config as clip
from src import studio_auto_edit_bridge as bridge
from src import studio_project_store as store

PROJECT_ID = "timeline_abc123def456"
RENDER_TASK = "autoedit_0123456789ab"
CLIP_ID = "clip-741ef5-01"
LEGACY_KEYS = ("title", "subtitle_config", "subtitle_overrides", "transform", "audio_tracks",
               "voice_volume", "muted", "effects", "transcript_revision",
               "cover_asset_id", "cover_revision")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """假任务根目录 + 一条已登记的成片（含成片/封面固定字节，路径可校验）。"""
    root = tmp_path / "tasks"
    monkeypatch.setattr(store, "TASKS_ROOT", root)
    monkeypatch.setattr(bridge, "TASKS_ROOT", root)

    clip_dir = root / "output" / RENDER_TASK / CLIP_ID
    clip_dir.mkdir(parents=True)
    (clip_dir / "clip_final.mp4").write_bytes(b"ORIGINAL-VIDEO" * 10)
    (clip_dir / "cover.jpg").write_bytes(b"ORIGINAL-COVER")
    store.register_clips(PROJECT_ID, RENDER_TASK, [{
        "clip_id": CLIP_ID, "name": "剪辑成片 01", "title": "胰腺癌早期信号",
        "topic": "健康科普", "source_start": 0.14, "source_end": 16.3, "duration": 16.96,
        "cover_path": f"output/{RENDER_TASK}/{CLIP_ID}/cover.jpg",
    }])
    return {"root": root, "clip_dir": clip_dir, "project_id": PROJECT_ID,
            "render_task": RENDER_TASK, "clip_id": CLIP_ID}


def _entry(env) -> dict:
    return store.load_manifest(env["project_id"])["clips"][env["clip_id"]]


def _raw_manifest(env) -> str:
    return store.manifest_path(env["project_id"]).read_text(encoding="utf-8")


def _history(env) -> list[dict]:
    return store.load_manifest(env["project_id"])["clips"][env["clip_id"]]["task_history"]


# ---------------------------------------------------------------------------
# 1. 默认值补齐 / 向后兼容
# ---------------------------------------------------------------------------

def test_legacy_entry_gets_full_defaults_and_is_a_copy(env):
    entry = _entry(env)
    assert not any(key in entry for key in LEGACY_KEYS[1:])  # 老条目只有 title

    filled = clip.with_clip_defaults(entry)
    assert filled is not entry and filled["title"] == "胰腺癌早期信号"
    assert filled["subtitle_config"] == clip.DEFAULT_SUBTITLE_CONFIG
    assert filled["subtitle_config"]["position"] == {"x_pct": 50.0, "y_pct": 78.125}
    assert filled["effects"] == clip.DEFAULT_EFFECTS
    assert filled["subtitle_overrides"] == [] and filled["audio_tracks"] == []
    assert filled["transform"] is None and filled["voice_volume"] == 1.0
    assert filled["muted"] is False and filled["transcript_revision"] == 0
    # 封面投影（只读）：读自 entry["cover"]，不在条目上新增副本
    assert filled["cover_asset_id"] == f"cover-{CLIP_ID}-v1"
    assert filled["cover_revision"] == 1

    # 纯投影：不改原条目、不落盘新键
    filled["subtitle_config"]["font_size_px"] = 1
    assert "subtitle_config" not in entry
    assert "cover_asset_id" not in _entry(env)


def test_card_view_gains_phase1_keys_without_breaking_existing_shape(env):
    card = store.list_cards(env["project_id"])[0]
    # 既有键取值逐项保持
    assert card["clip_id"] == CLIP_ID
    assert card["title"] == "胰腺癌早期信号"
    assert card["render_task_id"] == RENDER_TASK
    assert card["render_revision"] == 1
    assert card["video_path"] == f"output/{RENDER_TASK}/{CLIP_ID}/clip_final.mp4"
    assert card["cover"]["cover_asset_id"] == f"cover-{CLIP_ID}-v1"
    # 新增的 Phase 1 键
    assert card["subtitle_config"]["font_size_px"] == 44
    assert card["effects"]["export_resolution"] == "1080x1920"
    assert card["subtitle_overrides"] == [] and card["audio_tracks"] == []
    assert card["voice_volume"] == 1.0 and card["muted"] is False
    assert card["transform"] is None and card["transcript_revision"] == 0
    assert card["cover_asset_id"] == f"cover-{CLIP_ID}-v1"
    assert card["cover_revision"] == 1


def test_card_view_tolerates_legacy_and_corrupt_entries(env):
    """没有任何新键、甚至没有 cover / 值是坏数据时，card_view 也不能抛错。"""
    assert store.card_view({"clip_id": "clip-legacy"})["subtitle_config"] == clip.DEFAULT_SUBTITLE_CONFIG
    corrupt = {
        "clip_id": "clip-legacy", "render_revision": 3,
        "subtitle_config": {"font_size_px": "huge", "position": "bottom"},
        "effects": {"filter": {"name": "sepia"}},
        "transform": {"fitMode": "zoom"},
        "voice_volume": "loud", "muted": "yes", "transcript_revision": "x",
    }
    card = store.card_view(corrupt)
    assert card["subtitle_config"] == clip.DEFAULT_SUBTITLE_CONFIG
    assert card["effects"] == clip.DEFAULT_EFFECTS
    assert card["transform"] is None
    assert card["voice_volume"] == 1.0 and card["muted"] is False
    assert card["transcript_revision"] == 0
    assert card["cover_asset_id"] is None and card["cover_revision"] == 0


# ---------------------------------------------------------------------------
# 2. 部分更新语义
# ---------------------------------------------------------------------------

def test_partial_update_writes_only_the_touched_section(env):
    result = clip.update_config(env["project_id"], env["clip_id"],
                                {"subtitle_config": {"font_size_px": 70}})
    assert result["changed"] == ["subtitle_config"]
    assert result["binding"] == "clip"
    assert result["render_deferred"] is True
    assert result["render_note"] == clip.RENDER_NOTE
    entry = _entry(env)
    assert entry["subtitle_config"]["font_size_px"] == 70
    assert entry["subtitle_config"]["position"] == clip.DEFAULT_SUBTITLE_CONFIG["position"]
    assert entry["subtitle_config"]["text_color"] == "#FFD700"
    # 没碰的段不落盘（保持「未显式写过」→ 仍可继承源片级）
    assert "effects" not in entry and "voice_volume" not in entry
    assert any(h["task_type"] == "UPDATE_SUBTITLE" for h in _history(env))


def test_partial_nested_merge_keeps_inherited_values(env):
    """源片级先定 position，clip 只改字号 → 合并后 position 仍是源片级的值。"""
    clip.update_config(env["project_id"], "source",
                       {"subtitle_config": {"position": {"y_pct": 60.0}}})
    result = clip.update_config(env["project_id"], env["clip_id"],
                                {"subtitle_config": {"font_size_px": 90}})
    assert result["config"]["subtitle_config"]["font_size_px"] == 90
    assert result["config"]["subtitle_config"]["position"]["y_pct"] == 60.0
    assert _entry(env)["subtitle_config"]["position"]["y_pct"] == 60.0


def test_config_view_and_summary_read_inherited_config(env):
    clip.update_config(env["project_id"], "source", {"voice_volume": 0.4, "muted": True})
    view = clip.config_view(env["project_id"], env["clip_id"])
    assert view["binding"] == "clip"
    assert view["config"]["voice_volume"] == 0.4 and view["config"]["muted"] is True
    assert view["sources"]["voice_volume"] == "project"
    assert view["sources"]["subtitle_config"] == "skill_default"
    assert view["revisions"] == {"transcript_revision": 0, "render_revision": 1}


def test_explicit_null_transform_wins_over_inherited_transform(env):
    """``transform: null`` 是「清除」而不是「未设置」：不得被源片级变换继承回来。"""
    clip.update_config(env["project_id"], "source", {"transform": {"scale": 3}})
    assert clip.config_view(env["project_id"], env["clip_id"])["config"]["transform"]["scale"] == 3.0

    cleared = clip.update_config(env["project_id"], env["clip_id"], {"transform": None})
    assert cleared["changed"] == ["transform"]
    assert cleared["config"]["transform"] is None
    view = clip.config_view(env["project_id"], env["clip_id"])
    assert view["config"]["transform"] is None
    assert view["sources"]["transform"] == "user"
    # 源片级本身不受影响
    assert clip.config_view(env["project_id"], "source")["config"]["transform"]["scale"] == 3.0


def test_config_view_matches_update_config_result(env):
    result = clip.update_config(env["project_id"], env["clip_id"],
                                {"subtitle_config": {"outline_width": 8},
                                 "voice_volume": 0.6, "muted": True})
    view = clip.config_view(env["project_id"], env["clip_id"])
    assert view["config"] == result["config"]
    assert view["revisions"] == result["revisions"]
    assert view["sources"]["subtitle_config"] == "user"
    assert view["sources"]["voice_volume"] == "user"


def test_update_config_unknown_clip_raises(env):
    with pytest.raises(clip.ClipConfigError) as excinfo:
        clip.update_config(env["project_id"], "clip-missing", {"title": "x"})
    assert excinfo.value.code == clip.E_CONFIG_UNKNOWN_CLIP
    with pytest.raises(clip.ClipConfigError) as excinfo:
        clip.config_view(env["project_id"], "clip-missing")
    assert excinfo.value.code == clip.E_CONFIG_UNKNOWN_CLIP


# ---------------------------------------------------------------------------
# 3. 严格校验：类型 / 未知段 / 未知键 / 越界 / 枚举 / 颜色 / transform
# ---------------------------------------------------------------------------

BAD_PATCHES = [
    # 类型错
    ({"title": 3}, clip.E_CONFIG_INVALID),
    ({"subtitle_config": {"font_size_px": "big"}}, clip.E_CONFIG_INVALID),
    ({"subtitle_config": {"font_size_px": 70.5}}, clip.E_CONFIG_INVALID),
    ({"subtitle_config": {"word_highlight": 1}}, clip.E_CONFIG_INVALID),
    ({"subtitle_config": {"font_family": None}}, clip.E_CONFIG_INVALID),
    ({"subtitle_config": "big"}, clip.E_CONFIG_INVALID),
    ({"subtitle_overrides": "nope"}, clip.E_CONFIG_INVALID),
    ({"subtitle_overrides": [{"text": "没有 segment_id"}]}, clip.E_CONFIG_INVALID),
    ({"audio_tracks": {"id": "x"}}, clip.E_CONFIG_INVALID),
    ({"audio_tracks": [{"volume": 0.5}]}, clip.E_CONFIG_INVALID),
    ({"audio_tracks": [{"id": "x", "volume": 2}]}, clip.E_CONFIG_INVALID),
    ({"audio_tracks": [{"id": "x", "kind": "music"}]}, clip.E_CONFIG_INVALID),
    ({"audio_tracks": [{"id": "x", "peaks": [0.1] * 801}]}, clip.E_CONFIG_INVALID),
    ({"voice_volume": "loud"}, clip.E_CONFIG_INVALID),
    ({"voice_volume": True}, clip.E_CONFIG_INVALID),
    ({"muted": 1}, clip.E_CONFIG_INVALID),
    ({"effects": {"sticker": {"enabled": "yes"}}}, clip.E_CONFIG_INVALID),
    # 未知段 / 未知键
    ({"subtitle_style": {"config": {}}}, clip.E_CONFIG_UNKNOWN_SECTION),
    ({"cover_asset_id": "cover-x-v2"}, clip.E_CONFIG_UNKNOWN_SECTION),
    ({"subtitle_config": {"nope": 1}}, clip.E_CONFIG_INVALID),
    ({"effects": {"nope": 1}}, clip.E_CONFIG_INVALID),
    ({"effects": {"filter": {"nope": 1}}}, clip.E_CONFIG_INVALID),
    # 越界
    ({"subtitle_config": {"font_size_px": 4}}, clip.E_CONFIG_INVALID),
    ({"subtitle_config": {"font_size_px": 400}}, clip.E_CONFIG_INVALID),
    ({"subtitle_config": {"outline_width": 20}}, clip.E_CONFIG_INVALID),
    ({"subtitle_config": {"outline_width": -1}}, clip.E_CONFIG_INVALID),
    ({"subtitle_config": {"max_chars_per_line": 2}}, clip.E_CONFIG_INVALID),
    ({"subtitle_config": {"position": {"x_pct": 120}}}, clip.E_CONFIG_INVALID),
    ({"subtitle_config": {"position": {"y_pct": -1}}}, clip.E_CONFIG_INVALID),
    ({"subtitle_config": {"background": {"opacity": 3}}}, clip.E_CONFIG_INVALID),
    ({"voice_volume": 1.5}, clip.E_CONFIG_INVALID),
    ({"effects": {"filter": {"strength": 2}}}, clip.E_CONFIG_INVALID),
    ({"effects": {"end_card": {"duration_s": 60}}}, clip.E_CONFIG_INVALID),
    # 坏枚举
    ({"effects": {"filter": {"name": "sepia"}}}, clip.E_CONFIG_INVALID),
    ({"effects": {"transition": {"type": "slide"}}}, clip.E_CONFIG_INVALID),
    ({"effects": {"sticker": {"position": "top"}}}, clip.E_CONFIG_INVALID),
    ({"effects": {"export_resolution": "720x1280"}}, clip.E_CONFIG_INVALID),
    # 坏颜色
    ({"subtitle_config": {"text_color": "red"}}, clip.E_CONFIG_INVALID),
    ({"subtitle_config": {"outline_color": "#GGGGGG"}}, clip.E_CONFIG_INVALID),
    ({"subtitle_config": {"background": {"color": "#FFF"}}}, clip.E_CONFIG_INVALID),
    # 坏 transform
    ({"transform": "reset"}, clip.E_CONFIG_INVALID),
    ({"transform": {"fitMode": "zoom"}}, clip.E_CONFIG_INVALID),
    ({"transform": {"rotation": 90}}, clip.E_CONFIG_INVALID),
    ({"transform": {"scale": "2"}}, clip.E_CONFIG_INVALID),
    ({"transform": {"positionX": float("inf")}}, clip.E_CONFIG_INVALID),
    # patch 本身不是对象
    ("not-a-dict", clip.E_CONFIG_INVALID),
]


@pytest.mark.parametrize("patch,code", BAD_PATCHES)
def test_invalid_patch_raises_clip_config_error(env, patch, code):
    before = _raw_manifest(env)
    with pytest.raises(clip.ClipConfigError) as excinfo:
        clip.update_config(env["project_id"], env["clip_id"], patch)
    assert excinfo.value.code == code
    assert excinfo.value.message  # 中文可读原因
    assert _raw_manifest(env) == before  # 校验失败绝不写盘


def test_invalid_patch_message_is_readable_chinese(env):
    with pytest.raises(clip.ClipConfigError) as excinfo:
        clip.update_config(env["project_id"], env["clip_id"],
                           {"subtitle_config": {"font_size_px": "big"}})
    message = excinfo.value.message
    assert "subtitle_config.font_size_px" in message
    assert "必须是整数" in message


def test_failed_validation_is_all_or_nothing(env):
    """一个段合法、另一个段非法：整体拒绝，合法段也不得落盘。"""
    before = _raw_manifest(env)
    with pytest.raises(clip.ClipConfigError):
        clip.update_config(env["project_id"], env["clip_id"],
                           {"title": "应该不生效", "effects": {"filter": {"strength": 5}}})
    assert _raw_manifest(env) == before
    assert _entry(env)["title"] == "胰腺癌早期信号"


def test_valid_audio_track_passes_through_with_ranges(env):
    track = {"id": "bgm-1", "kind": "bgm", "asset_id": "bgm-1", "path": "a/bgm.mp3",
             "filename": "bgm.mp3", "volume": 0.3, "fade_in_s": 1, "fade_out_s": 2.5,
             "duration_s": 30, "peaks": [0.1, 0.2], "sha256": "d", "bytes": 10,
             "created_at": "2026-10-13T00:00:00Z", "extra_extension": "保留"}
    result = clip.update_config(env["project_id"], env["clip_id"], {"audio_tracks": [track]})
    stored = _entry(env)["audio_tracks"][0]
    assert result["changed"] == ["audio_tracks"]
    assert stored["volume"] == 0.3 and stored["fade_out_s"] == 2.5
    assert stored["peaks"] == [0.1, 0.2] and stored["asset_id"] == "bgm-1"
    assert stored["extra_extension"] == "保留"


def test_transform_delegates_to_clip_transform_layer(env):
    result = clip.update_config(env["project_id"], env["clip_id"],
                                {"transform": {"scale": 2, "positionX": 30, "fitMode": "contain"}})
    assert result["changed"] == ["transform"]
    assert result["config"]["transform"]["positionX"] == 30.0
    # 委托既有数学层：wire format 由 parse_transform().to_payload() 决定
    from src import clip_transform
    assert result["config"]["transform"] == clip_transform.parse_transform(
        {"scale": 2, "positionX": 30, "fitMode": "contain"}).to_payload()


# ---------------------------------------------------------------------------
# 4. 版本号：只在真有变化时 bump
# ---------------------------------------------------------------------------

def test_needs_render_bump_excludes_title_only():
    assert clip.needs_render_bump([]) is False
    assert clip.needs_render_bump(["title"]) is False
    for section in ("subtitle_config", "subtitle_overrides", "transform",
                    "audio_tracks", "voice_volume", "muted", "effects"):
        assert clip.needs_render_bump([section]) is True


def test_repeated_identical_update_does_not_bump_revision(env):
    first = clip.update_config(env["project_id"], env["clip_id"],
                               {"subtitle_config": {"font_size_px": 70}})
    assert first["changed"] == ["subtitle_config"] and first["revisions"]["render_revision"] == 2

    same = clip.update_config(env["project_id"], env["clip_id"],
                              {"subtitle_config": {"font_size_px": 70}})
    assert same["changed"] == []
    assert same["revisions"]["render_revision"] == 2
    assert _entry(env)["render_revision"] == 2

    different = clip.update_config(env["project_id"], env["clip_id"],
                                   {"subtitle_config": {"font_size_px": 71}})
    assert different["changed"] == ["subtitle_config"]
    assert different["revisions"]["render_revision"] == 3


def test_apply_patch_reports_only_real_changes():
    current = clip.default_config()
    updated, changed = clip.apply_patch(current, {"subtitle_config": {"font_size_px": 44}})
    assert changed == [] and updated == current
    updated, changed = clip.apply_patch(current, {"voice_volume": 1, "muted": False})
    assert changed == [] and updated["voice_volume"] == 1.0
    _, changed = clip.apply_patch(current, {"title": "", "muted": True})
    assert changed == ["muted"]


def test_title_never_bumps_render_revision(env):
    result = clip.update_config(env["project_id"], env["clip_id"], {"title": "新标题"})
    assert result["changed"] == ["title"]
    assert result["revisions"]["render_revision"] == 1
    assert result["task"]["task_type"] == "UPDATE_TITLE"
    assert re.fullmatch(r"title_[a-f0-9]{12}", result["task"]["task_id"])
    assert result["task"]["status"] == "completed"
    assert _entry(env)["title"] == "新标题"


def test_task_history_mapping_per_section(env):
    cases = [
        ({"subtitle_overrides": [{"segment_id": "seg-1", "text": "改"}]}, "UPDATE_SUBTITLE", "msub"),
        ({"transform": {"scale": 1.5}}, "UPDATE_TRANSFORM", "xfm"),
        ({"voice_volume": 0.2}, "UPDATE_AUDIO", "aud"),
        ({"muted": True}, "UPDATE_AUDIO", "aud"),
        ({"effects": {"filter": {"name": "warm", "strength": 0.5}}}, "UPDATE_EFFECTS", "fx"),
    ]
    for patch, task_type, prefix in cases:
        result = clip.update_config(env["project_id"], env["clip_id"], patch)
        assert result["task"]["task_type"] == task_type
        assert result["task"]["task_id"].startswith(f"{prefix}_")
        assert result["task"]["summary"] and result["task"]["stage"]


# ---------------------------------------------------------------------------
# 5. source 绑定与导出摘要
# ---------------------------------------------------------------------------

def test_source_binding_writes_source_config_without_history(env):
    result = clip.update_config(env["project_id"], "source",
                                {"subtitle_config": {"max_chars_per_line": 20}, "voice_volume": 0.5})
    assert result["binding"] == "source"
    assert result["clip_id"] == "source"
    assert result["changed"] == ["subtitle_config", "voice_volume"]
    assert result["revisions"] == {"transcript_revision": 0, "render_revision": 0}
    assert result["render_deferred"] is True
    assert result["task"] is None

    manifest = store.load_manifest(env["project_id"])
    assert manifest["source_config"]["subtitle_config"]["max_chars_per_line"] == 20
    assert manifest["source_config"]["voice_volume"] == 0.5
    # 不写 task_history、也不新增/改动 clip
    assert _history(env) == []
    assert "subtitle_config" not in _entry(env)

    view = clip.config_view(env["project_id"], "source")
    assert view["binding"] == "source" and view["revisions"] == {"transcript_revision": 0,
                                                                 "render_revision": 0}
    assert view["sources"]["subtitle_config"] == "user"
    assert view["sources"]["effects"] == "skill_default"
    assert view["config"]["subtitle_config"]["max_chars_per_line"] == 20

    # store 层的默认值补齐视图
    assert store.source_config(env["project_id"])["voice_volume"] == 0.5
    assert store.source_config(env["project_id"])["effects"] == clip.DEFAULT_EFFECTS


def test_source_binding_invalid_patch_does_not_write(env):
    clip.update_config(env["project_id"], "source", {"voice_volume": 0.5})
    before = _raw_manifest(env)
    with pytest.raises(clip.ClipConfigError):
        clip.update_config(env["project_id"], "source", {"voice_volume": 5})
    assert _raw_manifest(env) == before


def test_render_config_summary_reports_sources(env):
    clip.update_config(env["project_id"], "source", {"voice_volume": 0.4})
    clip.update_config(env["project_id"], env["clip_id"], {"subtitle_config": {"font_size_px": 70}})
    clip.update_config(env["project_id"], env["clip_id"], {"title": "源片标题"})

    summary = clip.render_config_summary(env["project_id"], env["clip_id"])
    assert summary["binding"] == "clip"
    assert summary["export"] == {
        "resolution": "1080x1920", "deferred": True, "note": clip.EXPORT_NOTE}
    assert summary["sources"]["subtitle_config"] == "user"      # clip 显式写过
    assert summary["sources"]["voice_volume"] == "project"      # 只来自 source_config
    assert summary["sources"]["muted"] == "skill_default"       # 默认值
    assert summary["sources"]["title"] == "user"

    by_key = {item["key"]: item for item in summary["effective"]}
    assert by_key["subtitle_config.font_size_px"] == {
        "key": "subtitle_config.font_size_px", "value": 70, "source": "user"}
    assert by_key["voice_volume"] == {"key": "voice_volume", "value": 0.4, "source": "project"}
    assert by_key["effects.export_resolution"]["value"] == "1080x1920"
    assert by_key["effects.export_resolution"]["source"] == "skill_default"
    assert summary["config"]["subtitle_config"]["font_size_px"] == 70


def test_render_config_summary_for_source_binding(env):
    summary = clip.render_config_summary(env["project_id"], "source")
    assert summary["binding"] == "source"
    by_key = {item["key"]: item for item in summary["effective"]}
    assert by_key["subtitle_config.font_size_px"]["source"] == "skill_default"
    assert summary["export"]["deferred"] is True


# ---------------------------------------------------------------------------
# 6. 不触发 AUTO_EDIT：纯配置写 + sidecar 镜像
# ---------------------------------------------------------------------------

def test_update_config_never_creates_autoedit_task(env):
    clip.update_config(env["project_id"], env["clip_id"], {"title": "t"})
    clip.update_config(env["project_id"], env["clip_id"], {"subtitle_config": {"font_size_px": 66}})
    clip.update_config(env["project_id"], env["clip_id"], {"transform": {"scale": 1.2}})
    clip.update_config(env["project_id"], env["clip_id"], {"effects": {"transition": {"enabled": True,
                                                                                     "type": "fade"}}})
    clip.update_config(env["project_id"], "source", {"voice_volume": 0.3})

    root = env["root"]
    assert list(root.rglob("autoedit_*.json")) == []          # 不新建自动剪辑任务
    assert not (root / "localtasks").exists()                 # 也不伪装成局部任务文件
    assert not (root / "status").exists()
    assert all(h["task_type"] != "AUTO_EDIT" for h in _history(env))
    assert {h["task_type"] for h in _history(env)} == {
        "UPDATE_TITLE", "UPDATE_SUBTITLE", "UPDATE_TRANSFORM", "UPDATE_EFFECTS"}
    for record in _history(env):
        assert re.fullmatch(r"(title|msub|xfm|aud|fx)_[a-f0-9]{12}", record["task_id"])
        assert record["status"] == "completed"


def test_update_config_never_calls_auto_edit_entries(env, monkeypatch):
    """显式见证：局部配置写绝不触碰自动剪辑/复渲染入口，只镜像既有 sidecar。"""
    def boom(*args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("update_config 绝不允许触发 AUTO_EDIT")

    calls: list[tuple] = []
    monkeypatch.setattr(bridge, "submit_auto_edit_task", boom, raising=False)
    monkeypatch.setattr(bridge, "rerender_auto_edit_task", boom, raising=False)
    monkeypatch.setattr(bridge, "save_clip_transform",
                        lambda task_id, clip_id, transform: calls.append((task_id, clip_id, transform)))

    clip.update_config(env["project_id"], env["clip_id"], {"transform": {"scale": 1.25}})
    clip.update_config(env["project_id"], env["clip_id"],
                       {"subtitle_config": {"font_size_px": 64}, "voice_volume": 0.7})
    clip.update_config(env["project_id"], env["clip_id"],
                       {"effects": {"sticker": {"enabled": True, "position": "right"}}})
    clip.update_config(env["project_id"], env["clip_id"], {"title": "只改标题"})
    clip.update_config(env["project_id"], "source", {"muted": True})

    # 只镜像了一次 transform，且参数是「稳定渲染任务 + clip_id + 规范化 wire payload」
    assert len(calls) == 1
    task_id, clip_id, transform = calls[0]
    assert (task_id, clip_id) == (env["render_task"], env["clip_id"])
    assert transform["scale"] == 1.25 and transform["fitMode"] == "cover"
    assert not (env["root"] / "localtasks").exists()


def test_transform_change_mirrors_existing_sidecar(env):
    result = clip.update_config(env["project_id"], env["clip_id"], {"transform": {"scale": 2}})
    assert result["changed"] == ["transform"]
    sidecar = env["root"] / "output" / env["render_task"] / bridge.CLIP_TRANSFORM_SIDECAR
    assert sidecar.is_file()
    assert json.loads(sidecar.read_text(encoding="utf-8"))[env["clip_id"]]["scale"] == 2.0
    # 与 store 条目一致（配置是事实源，sidecar 只是镜像）
    assert _entry(env)["transform"]["scale"] == 2.0

    cleared = clip.update_config(env["project_id"], env["clip_id"], {"transform": None})
    assert cleared["changed"] == ["transform"]
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {}
    assert _entry(env)["transform"] is None


def test_transform_without_output_dir_still_saves_config(env):
    """成片产物被回收也要能写配置（镜像失败不阻断）。"""
    import shutil
    shutil.rmtree(env["root"] / "output" / env["render_task"])
    result = clip.update_config(env["project_id"], env["clip_id"], {"transform": {"scale": 1.5}})
    assert result["changed"] == ["transform"]
    assert _entry(env)["transform"]["scale"] == 1.5


# ---------------------------------------------------------------------------
# 7. 转写修订号
# ---------------------------------------------------------------------------

def test_set_transcript_revision_writes_without_bumping_render(env):
    result = clip.set_transcript_revision(env["project_id"], env["clip_id"], 3)
    assert result["transcript_revision"] == 3
    entry = _entry(env)
    assert entry["transcript_revision"] == 3
    assert entry["render_revision"] == 1
    assert clip.config_view(env["project_id"], env["clip_id"])["revisions"]["transcript_revision"] == 3
    assert _history(env) == []  # 修订号不是「任务」


def test_set_transcript_revision_rejects_bad_input(env):
    with pytest.raises(clip.ClipConfigError) as excinfo:
        clip.set_transcript_revision(env["project_id"], env["clip_id"], -1)
    assert excinfo.value.code == clip.E_CONFIG_INVALID
    with pytest.raises(clip.ClipConfigError) as excinfo:
        clip.set_transcript_revision(env["project_id"], env["clip_id"], "3")
    assert excinfo.value.code == clip.E_CONFIG_INVALID
    with pytest.raises(clip.ClipConfigError) as excinfo:
        clip.set_transcript_revision(env["project_id"], "clip-missing", 1)
    assert excinfo.value.code == clip.E_CONFIG_UNKNOWN_CLIP
    with pytest.raises(clip.ClipConfigError) as excinfo:
        clip.set_transcript_revision(env["project_id"], "source", 1)
    assert excinfo.value.code == clip.E_CONFIG_UNKNOWN_CLIP


# ---------------------------------------------------------------------------
# 8. 规范化视图
# ---------------------------------------------------------------------------

def test_normalize_config_fills_defaults_and_falls_back_per_section():
    raw = {"subtitle_config": {"font_size_px": 70}, "effects": {"filter": {"name": "sepia"}},
           "voice_volume": 2, "title": "T"}
    config = clip.normalize_config(raw)
    assert config["subtitle_config"]["font_size_px"] == 70
    assert config["subtitle_config"]["outline_width"] == 3
    assert config["effects"] == clip.DEFAULT_EFFECTS   # 坏段退化成默认
    assert config["voice_volume"] == 1.0
    assert config["title"] == "T"


def test_normalize_patch_keeps_partial_sections_partial():
    patch = clip.normalize_patch({"subtitle_config": {"font_size_px": 70},
                                  "effects": {"filter": {"strength": 0.3}}})
    assert patch["subtitle_config"] == {"font_size_px": 70}
    assert patch["effects"] == {"filter": {"strength": 0.3}}
    # 没写的段不出现在 patch 里（apply_patch 才补默认值/继承值）
    assert set(patch) == {"subtitle_config", "effects"}
