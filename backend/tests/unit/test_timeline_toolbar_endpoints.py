"""底部时间线 Phase B「音轨工具栏 + 轨道显示设置」HTTP 端点单元测试。

纯文件系统、不联 DB、不跑 ffmpeg（peaks 只在白名单测试里打桩）。

覆盖任务书要求的四件事：
1. **路由注册**：view / history / undo / redo / revision / tracks(POST/PATCH/DELETE) /
   assets/peaks / assets/audio 全部挂在 media router 上（少一个前端就 404）；
2. **错误映射**：非法视图 → 422 ``E_TIMELINE_VIEW_INVALID``；撤销/重做不可用 →
   **409**；``E_TIMELINE_AUDIO_SOURCE_TRACK_RESERVED`` → **409** +「原声已自动进入」；
   非法 kind → 422；peaks 路径穿越 → **400**；
3. **行为**：原声自动进时间线（导入落地）、＋ 加 BGM 进独立音轨、移动可撤销/重做、
   轨高/占比/缩放读写、refresh 回读；
4. **tripwire**：视图与历史端点**绝不**进入渲染/导出（EDL 字节不变 +
   ``update_timeline_project`` 被调用即失败）。

风格沿用 ``test_inspector_endpoints.py``：这些路由没有 DB 依赖，直接 ``await``
端点协程；需要 body/form 的地方用最小 stub；系统配置打桩到 ``tmp_path``。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile as StarletteUploadFile

from src import clip_audio, timeline_audio, timeline_history, timeline_view
from src.api.routes import media

PROJECT_ID = "timeline_phaseb0001"
SOURCE_NAME = "source.mp4"
SOURCE_DURATION = 438.17

EXPECTED_ROUTES = {
    ("GET", "/studio/timeline/{project_id}/view"),
    ("PUT", "/studio/timeline/{project_id}/view"),
    ("GET", "/studio/timeline/{project_id}/history"),
    ("POST", "/studio/timeline/{project_id}/undo"),
    ("POST", "/studio/timeline/{project_id}/redo"),
    ("POST", "/studio/timeline/{project_id}/revision"),
    ("POST", "/studio/timeline/{project_id}/tracks"),
    ("PATCH", "/studio/timeline/{project_id}/tracks/{track_id}"),
    ("DELETE", "/studio/timeline/{project_id}/tracks/{track_id}"),
    ("GET", "/studio/timeline/{project_id}/assets/peaks"),
    ("POST", "/studio/timeline/{project_id}/assets/audio"),
}


# ---------------------------------------------------------------------------
# 测试脚手架
# ---------------------------------------------------------------------------

class _StubRequest:
    """只实现端点用到的那一小块 Request API（``body()``）。"""

    def __init__(self, payload: bytes = b"") -> None:
        self._payload = payload

    async def body(self) -> bytes:
        return self._payload


def _json_request(payload: dict) -> _StubRequest:
    return _StubRequest(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _upload(name: str = "bgm.wav", data: bytes = b"RIFF-fake-audio") -> StarletteUploadFile:
    return StarletteUploadFile(file=io.BytesIO(data), filename=name)


class _StubConfig:
    def __init__(self, temp_dir: Path) -> None:
        self.temp_dir = str(temp_dir)
        self.max_video_upload_bytes = 1024 * 1024


@pytest.fixture
def env(tmp_path, monkeypatch):
    """一个结构完整的假工程：project.json + edit/edl.json + raw/<source>。"""
    temp_dir = tmp_path / "temp"
    project_dir = temp_dir / "studio-projects" / PROJECT_ID
    (project_dir / "edit").mkdir(parents=True)
    (project_dir / "raw").mkdir(parents=True)
    (project_dir / "raw" / SOURCE_NAME).write_bytes(b"FAKE-SOURCE-VIDEO" * 8)
    (project_dir / "project.json").write_text(json.dumps({
        "id": PROJECT_ID,
        "source_stem": "source",
        "source": {"name": SOURCE_NAME, "duration": SOURCE_DURATION,
                   "width": 1080, "height": 1920, "fps": 30.0},
        "artifacts": {"edl": "edit/edl.json", "srt": "edit/subtitles/timeline.srt",
                      "fcpxml": "edit/timeline.fcpxml"},
    }, ensure_ascii=False), encoding="utf-8")
    (project_dir / "edit" / "edl.json").write_text(json.dumps({
        "version": 1, "project_name": "source", "fps": 30.0,
        "timelines": [{"name": "完整时间线", "resolution": [1080, 1920],
                       "sources": {"A001": f"raw/{SOURCE_NAME}"},
                       "ranges": [{"id": "t001-r0001", "source": "A001",
                                   "source_start": 0.0, "source_end": SOURCE_DURATION,
                                   "record_start": 0.0, "track": 1}]}],
    }, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(media, "get_config", lambda: _StubConfig(temp_dir))
    # ffprobe 不在单测里跑：源片返回与 project.json 一致的时长，其它音频固定 12.5s。
    monkeypatch.setattr(clip_audio, "probe_duration", _stub_probe)
    return {"temp_dir": temp_dir, "project_dir": project_dir}


def _stub_probe(path) -> float:
    """假 ffprobe：源片与 project.json 对齐，其余素材固定 12.5s。"""
    return SOURCE_DURATION if Path(path).name == SOURCE_NAME else 12.5


def _timeline_json(env, name: str = "history.json") -> dict:
    path = env["project_dir"] / "timeline" / name
    assert path.is_file(), f"缺少 {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _view_json(env) -> dict:
    return _timeline_json(env, "view.json")["view"]


def _current_tracks(env) -> list[dict]:
    revision = timeline_history.current_revision(env["project_dir"])
    return [] if revision is None else list(revision.audio_tracks)


def _edl_digest(env) -> str:
    import hashlib
    return hashlib.sha256(
        (env["project_dir"] / "edit" / "edl.json").read_bytes()).hexdigest()


def _forbid_render(monkeypatch) -> None:
    """tripwire：任何视图/历史端点调用渲染入口都立刻失败。"""
    def _boom(*_args, **_kwargs):
        raise AssertionError("视图/历史端点不得进入渲染/导出路径")

    monkeypatch.setattr(media, "update_timeline_project", _boom)


# ---------------------------------------------------------------------------
# 1. 路由注册
# ---------------------------------------------------------------------------

def test_all_phase_b_method_path_pairs_are_registered_on_media_router():
    registered = {(method, route.path)
                  for route in media.router.routes
                  for method in (route.methods or set())}
    assert EXPECTED_ROUTES <= registered
    assert len(EXPECTED_ROUTES) == 11
    assert len({path for _method, path in EXPECTED_ROUTES}) == 9


def test_existing_timeline_routes_are_untouched():
    """新端点必须与既有 timeline 端点**并列**，不能遮住它们。"""
    paths = {route.path for route in media.router.routes}
    assert "/studio/timeline/{project_id}" in paths
    assert "/studio/timeline/{project_id}/preview" in paths
    assert "/studio/timeline/{project_id}/source" in paths
    # 既有 clip 级 BGM 端点原样保留（Phase B 不改它）。
    assert "/studio/clips/{project_id}/{clip_id}/audio/bgm" in paths
    assert "/studio/clips/{project_id}/{clip_id}/audio/bgm/{asset_id}" in paths


# ---------------------------------------------------------------------------
# 2. 错误映射
# ---------------------------------------------------------------------------

def test_view_error_maps_to_422_with_code():
    exc = timeline_view.TimelineViewError(
        timeline_view.E_TIMELINE_VIEW_INVALID, "E_TIMELINE_VIEW_INVALID: 缩放越界")
    http = media._inspector_error(exc)
    assert http.status_code == 422
    assert "E_TIMELINE_VIEW_INVALID" in http.detail


def test_history_errors_map_to_422_with_code():
    exc = timeline_history.TimelineHistoryError(
        timeline_history.E_TIMELINE_HISTORY_INVALID, "历史文件损坏")
    http = media._inspector_error(exc)
    assert http.status_code == 422
    assert "E_TIMELINE_HISTORY_INVALID" in http.detail


def test_audio_track_not_found_maps_to_422():
    exc = timeline_audio.TimelineAudioError(
        timeline_audio.E_TIMELINE_AUDIO_TRACK_NOT_FOUND, "找不到音轨: 'x'")
    http = media._inspector_error(exc)
    assert http.status_code == 422
    assert "E_TIMELINE_AUDIO_TRACK_NOT_FOUND" in http.detail


# ---------------------------------------------------------------------------
# 3. 视图设置：读写 / 默认值 / 刷新回读 / 不触发渲染
# ---------------------------------------------------------------------------

async def test_view_defaults_before_any_write(env):
    payload = await media.studio_timeline_view(PROJECT_ID)
    assert payload["project_id"] == PROJECT_ID
    assert payload["view"] == {
        "zoom_px_per_second": timeline_view.DEFAULT_ZOOM_PX_PER_SECOND,
        "audio_track_height": timeline_view.DEFAULT_AUDIO_TRACK_HEIGHT_PX,
        "waveform_ratio": timeline_view.DEFAULT_WAVEFORM_RATIO,
        "scroll_left": None,
    }
    # 纯读：不落盘。
    assert not (env["project_dir"] / "timeline" / "view.json").exists()


async def test_view_patch_round_trips_and_survives_reload(env, monkeypatch):
    _forbid_render(monkeypatch)
    before = _edl_digest(env)
    saved = await media.studio_save_timeline_view(
        PROJECT_ID, _json_request({
            "zoom_px_per_second": 180,
            "audio_track_height": 132,
            "waveform_ratio": "expanded",
            "scroll_left": 420.5,
        }))
    assert saved["view"]["zoom_px_per_second"] == 180
    assert saved["view"]["audio_track_height"] == 132
    assert saved["view"]["waveform_ratio"] == "expanded"

    # 局部更新只动传进来的键。
    patched = await media.studio_save_timeline_view(
        PROJECT_ID, _json_request({"waveform_ratio": "compact"}))
    assert patched["view"]["waveform_ratio"] == "compact"
    assert patched["view"]["audio_track_height"] == 132  # 未被覆盖

    # 刷新后回读（GET view 是前端恢复路径）。
    reloaded = await media.studio_timeline_view(PROJECT_ID)
    assert reloaded["view"] == _view_json(env)
    assert reloaded["view"]["zoom_px_per_second"] == 180
    assert _edl_digest(env) == before, "改视图设置不得重写 EDL"


async def test_view_patch_rejects_illegal_values_without_writing(env):
    for patch in ({"zoom_px_per_second": 9999}, {"zoom_px_per_second": 1},
                  {"audio_track_height": 20}, {"audio_track_height": 400},
                  {"waveform_ratio": "巨大"}, {"audio_track_height": 72.5},
                  {"zoom_px_per_second": "fast"}):
        with pytest.raises(HTTPException) as caught:
            await media.studio_save_timeline_view(PROJECT_ID, _json_request(patch))
        assert caught.value.status_code == 422
        assert "E_TIMELINE_VIEW_INVALID" in str(caught.value.detail)
    assert not (env["project_dir"] / "timeline" / "view.json").exists()


# ---------------------------------------------------------------------------
# 4. 导入落地：原声自动进时间线（seed_source_tracks + push）
# ---------------------------------------------------------------------------

def test_seed_source_audio_puts_the_original_audio_on_the_timeline(env):
    timeline = {"id": PROJECT_ID}
    revision = media._seed_timeline_source_audio(env["temp_dir"], timeline)
    assert revision is not None
    tracks = _current_tracks(env)
    assert len(tracks) == 1
    track = tracks[0]
    assert track["kind"] == "voice"
    assert track["origin"] == "source"
    assert track["path"] == f"raw/{SOURCE_NAME}"
    assert track["source_start_s"] == 0.0
    assert track["source_end_s"] == SOURCE_DURATION  # 与视频时长对齐
    assert track["record_start_s"] == 0.0
    assert track["label"] == media.TIMELINE_SOURCE_TRACK_LABEL
    # 版本基底带上了 EDL ranges。
    assert revision["ranges"][0]["source_end"] == SOURCE_DURATION
    assert revision["label"] == "导入"


def test_seed_source_audio_is_idempotent(env):
    media._seed_timeline_source_audio(env["temp_dir"], {"id": PROJECT_ID})
    first = timeline_history.history_summary(env["project_dir"])
    media._seed_timeline_source_audio(env["temp_dir"], {"id": PROJECT_ID})
    again = timeline_history.history_summary(env["project_dir"])
    assert first["total"] == again["total"] == 1
    assert len(_current_tracks(env)) == 1  # 重跑导入不会叠加第二条原声轨


def test_seed_source_audio_skips_unknown_project(env):
    assert media._seed_timeline_source_audio(env["temp_dir"], {"id": "timeline_missing"}) is None
    assert media._seed_timeline_source_audio(env["temp_dir"], None) is None


def test_import_landing_sites_call_the_seed_helper():
    """两个导入落地处（同步 / 异步转写）都必须接线，否则原声不会自动进时间线。"""
    source = Path(media.__file__).read_text(encoding="utf-8")
    # 1 处定义 + 2 处调用（同步转写 / 异步转写落地）。
    assert source.count("_seed_timeline_source_audio") >= 3
    assert source.count('_timeline_commit(project_dir, data, "导入")') == 1


# ---------------------------------------------------------------------------
# 5. 历史：GET / undo / redo / 409
# ---------------------------------------------------------------------------

async def test_history_reports_button_state(env):
    media._seed_timeline_source_audio(env["temp_dir"], {"id": PROJECT_ID})
    payload = await media.studio_timeline_history(PROJECT_ID)
    assert payload["can_undo"] is False and payload["can_redo"] is False
    assert payload["summary"]["total"] == 1
    assert payload["current_revision"]["revision"] == 1
    assert len(payload["audio_tracks"]) == 1


async def test_undo_and_redo_return_409_when_unavailable(env):
    with pytest.raises(HTTPException) as undo_error:
        await media.studio_timeline_undo(PROJECT_ID)
    assert undo_error.value.status_code == 409
    assert "E_TIMELINE_UNDO_UNAVAILABLE" in str(undo_error.value.detail)

    with pytest.raises(HTTPException) as redo_error:
        await media.studio_timeline_redo(PROJECT_ID)
    assert redo_error.value.status_code == 409
    assert "E_TIMELINE_REDO_UNAVAILABLE" in str(redo_error.value.detail)


async def test_add_move_delete_bgm_is_undoable_and_redoable(env, monkeypatch):
    _forbid_render(monkeypatch)
    media._seed_timeline_source_audio(env["temp_dir"], {"id": PROJECT_ID})
    edl_before = _edl_digest(env)
    bgm_path = "timeline/audio/bgm-abcdef12.wav"
    (env["project_dir"] / bgm_path).parent.mkdir(parents=True, exist_ok=True)
    (env["project_dir"] / bgm_path).write_bytes(b"RIFF-fake-bgm")

    added = await media.studio_timeline_add_track(
        PROJECT_ID, _json_request({"kind": "bgm", "path": bgm_path, "label": "背景音乐"}))
    assert added["revision"] == 2
    assert added["can_undo"] is True and added["can_redo"] is False
    assert [t["kind"] for t in added["audio_tracks"]] == ["voice", "bgm"]  # 与原声共存
    bgm_id = added["audio_tracks"][1]["track_id"]

    moved = await media.studio_timeline_patch_track(
        PROJECT_ID, bgm_id, _json_request({"record_start_s": 12.5, "volume": 0.4}))
    assert moved["revision"] == 3
    bgm = timeline_audio.find_track(moved["current_revision"], bgm_id)
    assert bgm["record_start_s"] == 12.5 and bgm["volume"] == 0.4

    # 撤销两次回到「只有原声」，再重做两次回到移动后的状态。
    step1 = await media.studio_timeline_undo(PROJECT_ID)
    assert step1["revision"] == 2 and step1["can_redo"] is True
    assert timeline_audio.find_track(step1["current_revision"], bgm_id)["record_start_s"] == 0.0
    step2 = await media.studio_timeline_undo(PROJECT_ID)
    assert step2["revision"] == 1
    assert [t["kind"] for t in step2["audio_tracks"]] == ["voice"]
    assert step2["can_undo"] is False

    redo1 = await media.studio_timeline_redo(PROJECT_ID)
    assert redo1["revision"] == 2
    assert [t["kind"] for t in redo1["audio_tracks"]] == ["voice", "bgm"]
    redo2 = await media.studio_timeline_redo(PROJECT_ID)
    assert redo2["revision"] == 3
    assert redo2["can_redo"] is False

    removed = await media.studio_timeline_remove_track(PROJECT_ID, bgm_id)
    assert removed["revision"] == 4
    assert [t["kind"] for t in removed["audio_tracks"]] == ["voice"]
    await media.studio_timeline_undo(PROJECT_ID)
    assert [t["kind"] for t in _current_tracks(env)] == ["voice", "bgm"]

    assert _edl_digest(env) == edl_before, "音轨编辑不得重写 EDL"


async def test_new_edit_after_undo_clears_the_redo_branch(env, monkeypatch):
    _forbid_render(monkeypatch)
    media._seed_timeline_source_audio(env["temp_dir"], {"id": PROJECT_ID})
    await media.studio_timeline_revision(
        PROJECT_ID, _json_request({"ranges": [{"id": "a"}], "label": "改片段"}))
    await media.studio_timeline_undo(PROJECT_ID)
    again = await media.studio_timeline_revision(
        PROJECT_ID, _json_request({"ranges": [{"id": "b"}], "label": "另一条分支"}))
    assert again["can_redo"] is False
    # 新编辑丢弃 redo 分支；保留下来的标签只有「导入」+ 这条新分支。
    assert again["summary"]["total"] == 2
    assert again["summary"]["labels"] == ["导入", "另一条分支"]
    numbers = [item["revision"]
               for item in timeline_history.load_history(env["project_dir"])["revisions"]]
    assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers)


async def test_revision_is_idempotent_for_identical_content(env, monkeypatch):
    _forbid_render(monkeypatch)
    media._seed_timeline_source_audio(env["temp_dir"], {"id": PROJECT_ID})
    payload = {"ranges": [{"id": "same"}], "label": "同内容"}
    first = await media.studio_timeline_revision(PROJECT_ID, _json_request(payload))
    second = await media.studio_timeline_revision(PROJECT_ID, _json_request(payload))
    assert first["revision"] == second["revision"] == 2
    assert second["summary"]["total"] == 2


async def test_revision_normalises_tracks_before_push(env, monkeypatch):
    _forbid_render(monkeypatch)
    media._seed_timeline_source_audio(env["temp_dir"], {"id": PROJECT_ID})
    with pytest.raises(HTTPException) as caught:
        await media.studio_timeline_revision(PROJECT_ID, _json_request({
            "audio_tracks": [{"track_id": "bad", "kind": "bgm",
                              "source_start_s": 5.0, "source_end_s": 1.0}]}))
    assert caught.value.status_code == 422
    assert "E_TIMELINE_AUDIO_TRACK_INVALID" in str(caught.value.detail)
    # 非法 payload 不落盘：历史仍是导入那一条。
    assert timeline_history.history_summary(env["project_dir"])["total"] == 1


# ---------------------------------------------------------------------------
# 6. ＋ 菜单：bgm / sfx / 字幕轨 / 视觉轨 / 原声保留守卫
# ---------------------------------------------------------------------------

async def test_plus_menu_refuses_to_re_add_the_source_audio(env, monkeypatch):
    _forbid_render(monkeypatch)
    media._seed_timeline_source_audio(env["temp_dir"], {"id": PROJECT_ID})
    with pytest.raises(HTTPException) as caught:
        await media.studio_timeline_add_track(
            PROJECT_ID, _json_request({"kind": "voice", "path": f"raw/{SOURCE_NAME}"}))
    assert caught.value.status_code == 409
    assert "E_TIMELINE_AUDIO_SOURCE_TRACK_RESERVED" in str(caught.value.detail)
    assert "原声已自动进入" in str(caught.value.detail)


async def test_plus_menu_rejects_reserved_source_asset_even_with_bgm_kind(env, monkeypatch):
    _forbid_render(monkeypatch)
    media._seed_timeline_source_audio(env["temp_dir"], {"id": PROJECT_ID})
    with pytest.raises(HTTPException) as caught:
        await media.studio_timeline_add_track(PROJECT_ID, _json_request({
            "kind": "bgm", "asset_id": SOURCE_NAME, "path": f"raw/{SOURCE_NAME}"}))
    assert caught.value.status_code == 409
    assert "E_TIMELINE_AUDIO_SOURCE_TRACK_RESERVED" in str(caught.value.detail)


async def test_plus_menu_rejects_unknown_kind(env):
    with pytest.raises(HTTPException) as caught:
        await media.studio_timeline_add_track(PROJECT_ID, _json_request({"kind": "video"}))
    assert caught.value.status_code == 422
    assert "E_TIMELINE_AUDIO_TRACK_INVALID" in str(caught.value.detail)


async def test_plus_menu_requires_a_path_for_audio_tracks(env):
    with pytest.raises(HTTPException) as caught:
        await media.studio_timeline_add_track(PROJECT_ID, _json_request({"kind": "sfx"}))
    assert caught.value.status_code == 422
    assert "path" in str(caught.value.detail)


async def test_plus_menu_adds_sfx_on_its_own_track(env, monkeypatch):
    _forbid_render(monkeypatch)
    media._seed_timeline_source_audio(env["temp_dir"], {"id": PROJECT_ID})
    sfx_path = "timeline/audio/bgm-sfx00001.wav"
    (env["project_dir"] / sfx_path).parent.mkdir(parents=True, exist_ok=True)
    (env["project_dir"] / sfx_path).write_bytes(b"RIFF-fake-sfx")
    payload = await media.studio_timeline_add_track(
        PROJECT_ID, _json_request({"kind": "sfx", "path": sfx_path, "record_start_s": 3.0}))
    kinds = [t["kind"] for t in payload["audio_tracks"]]
    assert kinds == ["voice", "sfx"]
    sfx = payload["audio_tracks"][1]
    assert sfx["source_start_s"] == 0.0
    assert sfx["source_end_s"] == 12.5  # 时长来自 probe，不是 0
    assert sfx["record_start_s"] == 3.0
    assert sfx["origin"] == "user"


async def test_plus_menu_declares_a_subtitle_track_and_removes_it(env, monkeypatch):
    _forbid_render(monkeypatch)
    added = await media.studio_timeline_add_track(
        PROJECT_ID, _json_request({"kind": "subtitle"}))
    assert added["current_revision"]["subtitles"] == []

    removed = await media.studio_timeline_remove_track(
        PROJECT_ID, media.TIMELINE_SUBTITLE_TRACK_ID)
    assert removed["current_revision"]["subtitles"] is None
    assert removed["revision"] > added["revision"]


async def test_plus_menu_declares_an_image_visual_track(env, monkeypatch):
    _forbid_render(monkeypatch)
    added = await media.studio_timeline_add_track(
        PROJECT_ID, _json_request({"kind": "visual", "label": "图解剖图",
                                   "visual_kind": "image"}))
    visual = added["current_revision"]["visual_tracks"]
    assert len(visual) == 1
    assert visual[0]["kind"] == "image" and visual[0]["label"] == "图解剖图"
    assert visual[0]["origin"] == "user"
    track_id = visual[0]["track_id"]

    renamed = await media.studio_timeline_patch_track(
        PROJECT_ID, track_id, _json_request({"label": "图解剖图（改）"}))
    assert renamed["current_revision"]["visual_tracks"][0]["label"] == "图解剖图（改）"

    dropped = await media.studio_timeline_remove_track(PROJECT_ID, track_id)
    assert dropped["current_revision"]["visual_tracks"] == []


async def test_patch_unknown_track_is_422(env):
    with pytest.raises(HTTPException) as caught:
        await media.studio_timeline_patch_track(
            PROJECT_ID, "nope", _json_request({"volume": 0.5}))
    assert caught.value.status_code == 422
    assert "E_TIMELINE_AUDIO_TRACK_NOT_FOUND" in str(caught.value.detail)


async def test_patch_without_any_field_is_422(env, monkeypatch):
    _forbid_render(monkeypatch)
    media._seed_timeline_source_audio(env["temp_dir"], {"id": PROJECT_ID})
    track_id = _current_tracks(env)[0]["track_id"]
    with pytest.raises(HTTPException) as caught:
        await media.studio_timeline_patch_track(PROJECT_ID, track_id, _json_request({}))
    assert caught.value.status_code == 422


async def test_patch_muted_and_fades_round_trip(env, monkeypatch):
    _forbid_render(monkeypatch)
    media._seed_timeline_source_audio(env["temp_dir"], {"id": PROJECT_ID})
    track_id = _current_tracks(env)[0]["track_id"]
    payload = await media.studio_timeline_patch_track(
        PROJECT_ID, track_id,
        _json_request({"muted": True, "fade_in_s": 1.5, "fade_out_s": 2.0,
                       "source_start_s": 4.0}))
    track = timeline_audio.find_track(payload["current_revision"], track_id)
    assert track["muted"] is True
    assert track["fade_in_s"] == 1.5 and track["fade_out_s"] == 2.0
    # 只改入点必须保持区间长度（移动不是裁剪）：438.17 → 出点被钳到素材末尾。
    assert track["source_start_s"] == 4.0
    assert track["source_end_s"] == SOURCE_DURATION
    assert round(track["source_end_s"] - track["source_start_s"], 3) == round(
        SOURCE_DURATION - 4.0, 3)


async def test_delete_unknown_track_is_422(env):
    with pytest.raises(HTTPException) as caught:
        await media.studio_timeline_remove_track(PROJECT_ID, "nope")
    assert caught.value.status_code == 422
    assert "E_TIMELINE_AUDIO_TRACK_NOT_FOUND" in str(caught.value.detail)


# ---------------------------------------------------------------------------
# 7. peaks 端点：白名单 / 防穿越
# ---------------------------------------------------------------------------

@pytest.fixture
def peaks_spy(monkeypatch):
    """捕获 peaks_for_asset 收到的**解析后**路径（不跑 ffmpeg）。"""
    seen: dict = {}

    def _fake(project_dir, asset_path, **kwargs):
        seen["project_dir"] = project_dir
        seen["asset_path"] = Path(asset_path)
        seen["kwargs"] = kwargs
        return {"duration_s": SOURCE_DURATION, "peaks": [0.1, 0.4, 0.9],
                "peak_count": 3, "max_peaks": 800, "truncated": False,
                "buckets": kwargs.get("buckets"), "cache_key": "deadbeef",
                "cached": False, "asset": {"name": Path(asset_path).name},
                "cache_path": "timeline/peaks/deadbeef.json"}

    monkeypatch.setattr(timeline_audio, "peaks_for_asset", _fake)
    return seen


async def test_peaks_accepts_a_project_relative_path(env, peaks_spy):
    payload = await media.studio_timeline_asset_peaks(
        PROJECT_ID, path=f"raw/{SOURCE_NAME}", buckets=400)
    assert peaks_spy["asset_path"] == (env["project_dir"] / "raw" / SOURCE_NAME)
    assert peaks_spy["kwargs"]["buckets"] == 400
    assert payload["path"] == f"raw/{SOURCE_NAME}"
    assert payload["peaks"] == [0.1, 0.4, 0.9]


async def test_peaks_accepts_an_absolute_path_inside_the_project(env, peaks_spy):
    absolute = str(env["project_dir"] / "raw" / SOURCE_NAME)
    await media.studio_timeline_asset_peaks(PROJECT_ID, path=absolute)
    assert peaks_spy["asset_path"] == Path(absolute)


async def test_peaks_accepts_a_path_under_the_material_root(env, peaks_spy):
    material = env["temp_dir"] / "studio-uploads"
    material.mkdir(parents=True, exist_ok=True)
    (material / "uploaded.mp3").write_bytes(b"RIFF-material")
    await media.studio_timeline_asset_peaks(PROJECT_ID, path=str(material / "uploaded.mp3"))
    assert peaks_spy["asset_path"] == material / "uploaded.mp3"


@pytest.mark.parametrize("bad", [
    "",
    "../../../../etc/passwd",
    "raw/../../../etc/passwd",
    "/etc/passwd",
    "/domain/.env",
    "..",
    "../",
])
async def test_peaks_rejects_traversal_and_out_of_scope_paths(env, peaks_spy, bad):
    with pytest.raises(HTTPException) as caught:
        await media.studio_timeline_asset_peaks(PROJECT_ID, path=bad)
    assert caught.value.status_code in (400, 404)
    if bad:
        assert caught.value.status_code == 400, f"{bad!r} 必须是 400（越界）"
    assert peaks_spy == {}, "非法路径绝不允许到达 peaks 提取"


async def test_peaks_404s_for_a_missing_file_inside_the_project(env, peaks_spy):
    with pytest.raises(HTTPException) as caught:
        await media.studio_timeline_asset_peaks(PROJECT_ID, path="raw/nope.mp4")
    assert caught.value.status_code == 404
    assert peaks_spy == {}


async def test_peaks_rejects_symlink_escaping_the_project(env, peaks_spy, tmp_path):
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"RIFF-outside")
    link = env["project_dir"] / "raw" / "link.mp3"
    link.symlink_to(outside)
    with pytest.raises(HTTPException) as caught:
        await media.studio_timeline_asset_peaks(PROJECT_ID, path="raw/link.mp3")
    assert caught.value.status_code == 400
    assert peaks_spy == {}


# ---------------------------------------------------------------------------
# 8. 音频上传（＋ 菜单的 BGM 来源）+ 未知工程 404
# ---------------------------------------------------------------------------

async def test_audio_upload_lands_in_the_timeline_dir(env, monkeypatch):
    written: dict = {}

    def _fake_save_bgm(clip_dir, *, clip_id, filename, source_path, max_bytes):
        clip_dir = Path(clip_dir)
        assert clip_dir == env["project_dir"] / "timeline"
        assert clip_id == PROJECT_ID
        assert Path(source_path).is_file(), "暂存文件必须在调用前落盘"
        written["staged_bytes"] = Path(source_path).read_bytes()
        target = clip_dir / "audio" / "bgm-deadbeef.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(written["staged_bytes"])
        return {"id": "bgm-deadbeef", "asset_id": "bgm-deadbeef",
                "path": "audio/bgm-deadbeef.wav", "filename": filename,
                "kind": "bgm", "duration_s": 12.5,
                "id3": None}

    monkeypatch.setattr(clip_audio, "save_bgm", _fake_save_bgm)
    payload = await media.studio_timeline_upload_audio(
        PROJECT_ID, uploaded_file=_upload("我的音乐.wav", b"RIFF-real-bytes"))

    assert payload["path"] == "timeline/audio/bgm-deadbeef.wav"
    assert payload["asset_id"] == "bgm-deadbeef"
    assert payload["duration_s"] == 12.5
    assert written["staged_bytes"] == b"RIFF-real-bytes"
    # 暂存文件必须清理干净。
    assert list((env["project_dir"] / "timeline").glob(".upload-*")) == []


async def test_audio_upload_rejects_a_missing_filename(env):
    with pytest.raises(HTTPException) as caught:
        await media.studio_timeline_upload_audio(PROJECT_ID, uploaded_file=_upload(""))
    assert caught.value.status_code == 400


@pytest.mark.parametrize("route", ["view", "history", "revision", "tracks", "peaks"])
async def test_unknown_project_is_404(env, route):
    missing = "timeline_doesnotexist"
    with pytest.raises(HTTPException) as caught:
        if route == "view":
            await media.studio_timeline_view(missing)
        elif route == "history":
            await media.studio_timeline_history(missing)
        elif route == "revision":
            await media.studio_timeline_revision(missing, _json_request({}))
        elif route == "tracks":
            await media.studio_timeline_add_track(missing, _json_request({"kind": "bgm"}))
        else:
            await media.studio_timeline_asset_peaks(missing, path="raw/source.mp4")
    assert caught.value.status_code == 404


# ---------------------------------------------------------------------------
# 9. tripwire：整条 Phase B 只写 timeline/，不碰渲染与导出产物
# ---------------------------------------------------------------------------

async def test_view_and_history_never_call_render_and_never_touch_edl(env, monkeypatch):
    _forbid_render(monkeypatch)
    media._seed_timeline_source_audio(env["temp_dir"], {"id": PROJECT_ID})
    edl_before = _edl_digest(env)

    await media.studio_timeline_view(PROJECT_ID)
    await media.studio_save_timeline_view(
        PROJECT_ID, _json_request({"zoom_px_per_second": 260, "audio_track_height": 96,
                                   "waveform_ratio": "expanded"}))
    await media.studio_timeline_history(PROJECT_ID)
    await media.studio_timeline_revision(
        PROJECT_ID, _json_request({"ranges": [{"id": "x"}]}))
    await media.studio_timeline_undo(PROJECT_ID)

    assert _edl_digest(env) == edl_before
    # 也没有 srt / fcpxml 被重写（本 fixture 里它们本来就不存在）。
    edit_dir = env["project_dir"] / "edit"
    assert not (edit_dir / "timeline.fcpxml").exists()
    assert not (edit_dir / "subtitles" / "timeline.srt").exists()
    assert not list(env["project_dir"].rglob("autoedit*"))


def test_timeline_dir_holds_only_view_and_history_state(env, monkeypatch):
    """落盘位置契约：``<project_dir>/timeline/{view.json,history.json,peaks/}``。"""
    media._seed_timeline_source_audio(env["temp_dir"], {"id": PROJECT_ID})
    assert timeline_history.history_path(env["project_dir"]).is_file()
    timeline_view.save_view(env["project_dir"], {"zoom_px_per_second": 140})
    assert timeline_view.view_path(env["project_dir"]).is_file()
    assert timeline_audio.peaks_cache_dir(env["project_dir"]).parent == (
        env["project_dir"] / "timeline")
    # peaks/ 是惰性创建的（只有请求过波形才有），但 timeline/ 下不得出现别的状态。
    assert sorted(item.name for item in (env["project_dir"] / "timeline").iterdir()) == [
        "history.json", "view.json"]
