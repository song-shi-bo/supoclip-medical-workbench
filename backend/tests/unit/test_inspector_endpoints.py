"""Inspector Phase 1 HTTP 端点单元测试（契约 §5，纯文件系统、不联 DB、不跑 ffmpeg）。

覆盖任务书要求的三件事：
1. **路由注册**：11 个 method+path 组合都在 media router 上（少一个前端就 404）；
2. **错误映射**：五类新 ValueError 子类 → 422 ``CODE: 可读原因``，
   ``ProjectStoreError(E_CLIP_NOT_REGISTERED)`` → 404，``FileNotFoundError`` → 404，
   坏 JSON / 非对象 body → 400；未登记 clip 绝不 500；
3. **行为**：GET/PUT config、三态转写（含惰性 seed 与修订/回滚）、
   GET/POST 字幕（新版本、版本列表、无 cue 时 422）、BGM 上传/删除（幂等）、
   render-config 的来源标注，以及 **不创建 autoedit_* 文件** 的 tripwire。

风格：这些路由没有 DB 依赖（不需要 auth/session），因此直接 ``await`` 端点协程；
需要 body/form 的地方用最小 stub（``_StubRequest`` / ``_upload``），
需要系统配置的地方打桩 ``media.get_config``（避免写到真实 /tmp）。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile as StarletteUploadFile

from src import clip_audio, clip_config, studio_auto_edit_bridge as bridge
from src import studio_project_store as store
from src import subtitle_compose, transcript_revisions
from src.api.routes import media

PROJECT_ID = "timeline_abc123def456"
RENDER_TASK = "autoedit_0123456789ab"
CLIP_ID = "clip-741ef5-01"

#: 契约 §5 表格的全部 method+path 组合（7 条路径：GET|PUT config 计 2、
#: GET|POST transcript/revisions 计 2、GET|POST subtitles 计 2，加 rollback/bgm/delete/render-config）。
EXPECTED_ROUTES = {
    ("GET", "/studio/clips/{project_id}/{clip_id}/config"),
    ("PUT", "/studio/clips/{project_id}/{clip_id}/config"),
    ("GET", "/studio/clips/{project_id}/{clip_id}/transcript/revisions"),
    ("POST", "/studio/clips/{project_id}/{clip_id}/transcript/revisions"),
    ("POST", "/studio/clips/{project_id}/{clip_id}/transcript/rollback"),
    ("GET", "/studio/clips/{project_id}/{clip_id}/subtitles"),
    ("POST", "/studio/clips/{project_id}/{clip_id}/subtitles"),
    ("POST", "/studio/clips/{project_id}/{clip_id}/audio/bgm"),
    ("DELETE", "/studio/clips/{project_id}/{clip_id}/audio/bgm/{asset_id}"),
    ("GET", "/studio/clips/{project_id}/{clip_id}/render-config"),
}


# ---------------------------------------------------------------------------
# 测试脚手架（stub Request / UploadFile / config）
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
    """最小 Config：只暴露 BGM 端点用到的两个字段（默认写进临时任务根）。"""

    def __init__(self, temp_dir: Path) -> None:
        self.temp_dir = str(temp_dir)
        self.max_video_upload_bytes = 1024 * 1024


@pytest.fixture
def env(tmp_path, monkeypatch):
    """假任务根 + 一条已登记的成片 + 打桩的系统配置（全部落在 tmp_path 内）。"""
    root = tmp_path / "tasks"
    monkeypatch.setattr(store, "TASKS_ROOT", root)
    monkeypatch.setattr(bridge, "TASKS_ROOT", root)

    clip_dir = root / "output" / RENDER_TASK / CLIP_ID
    clip_dir.mkdir(parents=True)
    (clip_dir / "clip_final.mp4").write_bytes(b"ORIGINAL-VIDEO" * 10)
    (clip_dir / "cover.jpg").write_bytes(b"ORIGINAL-COVER")
    store.register_clips(PROJECT_ID, RENDER_TASK, [{
        "clip_id": CLIP_ID, "name": "剪辑成片 01", "title": "胰腺癌早期信号",
        "topic": "健康科普", "source_start": 0.0, "source_end": 5.0, "duration": 5.8,
        "cover_path": f"output/{RENDER_TASK}/{CLIP_ID}/cover.jpg",
    }])
    monkeypatch.setattr(media, "get_config", lambda: _StubConfig(root / "tmp"))
    return {"root": root, "clip_dir": clip_dir, "project_id": PROJECT_ID,
            "render_task": RENDER_TASK, "clip_id": CLIP_ID}


def _autoedit_artifacts(root: Path) -> set[str]:
    """任务根下所有 ``autoedit*`` 路径（任务 JSON / 输出目录）——「没建过」的直接证据。"""
    return {path.relative_to(root).as_posix() for path in root.rglob("autoedit*")}


def _entry(env) -> dict:
    return store.entry_for(env["project_id"], env["clip_id"])


def _write_render_artifacts(env, segments: list[dict]) -> None:
    """补一份真实存在的成片产物（clip_plan + clip_final + asr_segments）。"""
    root = env["root"]
    task_output = root / "output" / env["render_task"]
    task_output.mkdir(parents=True, exist_ok=True)
    (task_output / "clip_plan.json").write_text(json.dumps({
        "highlights": [{"clip_id": env["clip_id"], "start": 0.0, "end": 5.0, "topic": "健康科普"}],
    }, ensure_ascii=False), encoding="utf-8")
    (root / "status").mkdir(parents=True, exist_ok=True)
    (root / "status" / f"{env['render_task']}.json").write_text(
        json.dumps({"task_id": env["render_task"]}), encoding="utf-8")
    (root / "completed").mkdir(parents=True, exist_ok=True)
    (root / "completed" / f"{env['render_task']}.json").write_text(
        json.dumps({"task_id": env["render_task"], "asr_segments": segments},
                   ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. 路由注册（11 个 method+path）
# ---------------------------------------------------------------------------

def test_all_eleven_method_path_pairs_are_registered_on_media_router():
    registered = {(method, route.path)
                  for route in media.router.routes
                  for method in (route.methods or set())}
    assert EXPECTED_ROUTES <= registered
    assert len(EXPECTED_ROUTES) == 10
    assert len({path for _method, path in EXPECTED_ROUTES}) == 7


def test_source_sentinel_still_allowed_on_config_and_render_config_routes():
    """config / render-config 必须有 clip_id 路径参数（``source`` 哨兵走同一路由）。"""
    paths = {route.path for route in media.router.routes}
    assert "/studio/clips/{project_id}/{clip_id}/config" in paths
    assert "/studio/clips/{project_id}/{clip_id}/render-config" in paths


# ---------------------------------------------------------------------------
# 2. 错误映射
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc,status,needle", [
    (clip_config.ClipConfigError("E_CONFIG_INVALID", "font_size_px 不得大于 200"),
     422, "E_CONFIG_INVALID: font_size_px 不得大于 200"),
    (clip_config.ClipConfigError("E_CONFIG_UNKNOWN_CLIP", "没有该成片"),
     422, "E_CONFIG_UNKNOWN_CLIP"),
    (transcript_revisions.TranscriptRevisionError("E_TRANSCRIPT_NOT_SEEDED", "还没有基线"),
     422, "E_TRANSCRIPT_NOT_SEEDED"),
    (subtitle_compose.SubtitleComposeError("E_SUBTITLE_NO_CUES", "没有 cue"),
     422, "E_SUBTITLE_NO_CUES"),
    (clip_audio.ClipAudioError("E_AUDIO_TRACK_INVALID", "音量越界"),
     422, "E_AUDIO_TRACK_INVALID"),
    (store.ProjectStoreError("E_CLIP_NOT_REGISTERED", "项目里没有这条成片"),
     404, "E_CLIP_NOT_REGISTERED"),
    (store.ProjectStoreError("E_CLIP_ID_INVALID", "非法 clip_id"),
     422, "E_CLIP_ID_INVALID"),
    (FileNotFoundError("Auto-edit task not found"), 404, "Auto-edit task not found"),
])
def test_domain_errors_map_to_readable_http_status(exc, status, needle):
    http_error = media._inspector_error(exc)
    assert http_error.status_code == status
    assert needle in http_error.detail


def test_unexpected_error_maps_to_500_with_readable_reason():
    http_error = media._inspector_error(RuntimeError("磁盘炸了"))
    assert http_error.status_code == 500
    assert "磁盘炸了" in http_error.detail


@pytest.mark.asyncio
async def test_json_body_accepts_empty_and_object():
    assert await media._json_body(_StubRequest(b"")) == {}
    assert await media._json_body(_json_request({"a": 1})) == {"a": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b"[1, 2]", b"not-json", b'"text"', b"42"])
async def test_json_body_rejects_malformed_or_non_object_body(raw):
    with pytest.raises(HTTPException) as excinfo:
        await media._json_body(_StubRequest(raw))
    assert excinfo.value.status_code == 400


def test_reject_source_binding_is_a_readable_422():
    with pytest.raises(HTTPException) as excinfo:
        media._reject_source_binding("source", "字幕版本")
    assert excinfo.value.status_code == 422
    assert media.E_SOURCE_BINDING_UNSUPPORTED in excinfo.value.detail
    assert "config / render-config" in excinfo.value.detail
    media._reject_source_binding(CLIP_ID, "字幕版本")  # 普通 clip 不拦


# ---------------------------------------------------------------------------
# 3. GET/PUT config（含 source 哨兵与「不建 autoedit_*」tripwire）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_config_returns_binding_revisions_and_sources(env):
    payload = await media.studio_clip_config(env["project_id"], env["clip_id"])
    assert payload["project_id"] == env["project_id"]
    assert payload["clip_id"] == env["clip_id"]
    assert payload["binding"] == "clip"
    assert payload["revisions"] == {"transcript_revision": 0, "render_revision": 1}
    assert payload["config"]["subtitle_config"]["font_size_px"] == 44
    assert payload["sources"]["subtitle_config"] == "skill_default"


@pytest.mark.asyncio
async def test_put_config_applies_patch_and_creates_no_autoedit_artifact(env):
    before = _autoedit_artifacts(env["root"])
    payload = await media.studio_update_clip_config(
        env["project_id"], env["clip_id"],
        _json_request({"effects": {"filter": {"name": "cool", "strength": 0.3}}}))

    assert payload["changed"] == ["effects"]
    assert payload["config"]["effects"]["filter"] == {"name": "cool", "strength": 0.3}
    assert payload["revisions"]["render_revision"] == 2
    assert payload["render_deferred"] is True
    # 局部任务 id 走 §4 前缀（fx_ = UPDATE_EFFECTS），绝不是 autoedit_*。
    assert payload["task"]["task_type"] == "UPDATE_EFFECTS"
    assert payload["task"]["task_id"].startswith("fx_")
    assert _autoedit_artifacts(env["root"]) == before
    history = _entry(env)["task_history"]
    assert [item["task_type"] for item in history] == ["UPDATE_EFFECTS"]


@pytest.mark.asyncio
async def test_put_config_maps_validation_error_to_422(env):
    with pytest.raises(HTTPException) as excinfo:
        await media.studio_update_clip_config(
            env["project_id"], env["clip_id"],
            _json_request({"effects": {"filter": {"name": "sepia"}}}))
    assert excinfo.value.status_code == 422
    assert "E_CONFIG_INVALID" in excinfo.value.detail
    # 失败不写盘：effects 仍是默认值
    assert _entry(env).get("effects") is None


@pytest.mark.asyncio
async def test_put_config_rejects_unknown_section(env):
    with pytest.raises(HTTPException) as excinfo:
        await media.studio_update_clip_config(env["project_id"], env["clip_id"],
                                              _json_request({"nope": 1}))
    assert excinfo.value.status_code == 422
    assert "E_CONFIG_UNKNOWN_SECTION" in excinfo.value.detail


@pytest.mark.asyncio
async def test_config_and_render_config_accept_source_sentinel(env):
    written = await media.studio_update_clip_config(
        env["project_id"], "source", _json_request({"voice_volume": 0.5}))
    assert written["binding"] == "source"
    assert written["task"] is None                       # 源片级不写 task_history
    assert written["revisions"] == {"transcript_revision": 0, "render_revision": 0}

    fetched = await media.studio_clip_config(env["project_id"], "source")
    assert fetched["binding"] == "source"
    assert fetched["config"]["voice_volume"] == 0.5      # 读回来还是源片级的值

    summary = await media.studio_clip_render_config(env["project_id"], "source")
    assert summary["binding"] == "source"
    assert summary["config"]["voice_volume"] == 0.5
    assert summary["export"]["deferred"] is True


@pytest.mark.asyncio
async def test_config_view_for_unknown_clip_is_422_with_config_code(env):
    with pytest.raises(HTTPException) as excinfo:
        await media.studio_clip_config(env["project_id"], "clip-nope")
    assert excinfo.value.status_code == 422
    assert "E_CONFIG_UNKNOWN_CLIP" in excinfo.value.detail


# ---------------------------------------------------------------------------
# 4. render-config：来源标注（user / project / skill_default）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_render_config_summary_reports_sources_and_export_note(env):
    initial = await media.studio_clip_render_config(env["project_id"], env["clip_id"])
    effective = {item["key"]: item for item in initial["effective"]}
    assert effective["subtitle_config.font_size_px"] == {"key": "subtitle_config.font_size_px",
                                                         "value": 44, "source": "skill_default"}
    assert initial["export"]["deferred"] is True
    assert initial["export"]["resolution"] == "1080x1920"
    # 画面调整 / 转场 / 音频已接入「导出成片（按变换重渲）」通道：由 FFmpeg 精确生效，
    # 不再标「Phase 2」；颜色类预览是 CSS 近似，画幅比例仍只影响预览。
    assert "FFmpeg" in initial["export"]["note"]
    assert "画幅比例当前仅影响预览" in initial["export"]["note"]

    await media.studio_update_clip_config(
        env["project_id"], env["clip_id"],
        _json_request({"subtitle_config": {"font_size_px": 70}}))
    after = await media.studio_clip_render_config(env["project_id"], env["clip_id"])
    effective_after = {item["key"]: item for item in after["effective"]}
    assert effective_after["subtitle_config.font_size_px"]["source"] == "user"
    assert effective_after["subtitle_config.font_size_px"]["value"] == 70
    # 源片级写过的段在 clip 视图中标为 project（不是 user）
    await media.studio_update_clip_config(env["project_id"], "source",
                                          _json_request({"effects": {"filter": {"name": "warm"}}}))
    third = await media.studio_clip_render_config(env["project_id"], env["clip_id"])
    assert third["sources"]["effects"] == "project"
    assert third["config"]["effects"]["filter"]["name"] == "warm"


# ---------------------------------------------------------------------------
# 5. 转写：惰性 seed、三态视图、修订与回滚
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transcript_get_seeds_lazily_from_rendered_clip(env):
    _write_render_artifacts(env, [
        {"start": 0.0, "end": 2.0, "text": "胰腺癌早期信号", "speaker": "医生"},
        {"start": 2.0, "end": 4.0, "text": "别当成胃病", "speaker": "医生"},
    ])
    view = await media.studio_clip_transcript(env["project_id"], env["clip_id"])
    # 成片时间基 = 源时间 + 0.8s 片头
    assert [segment["start"] for segment in view["raw_segments"]] == [0.8, 2.8]
    assert [segment["text"] for segment in view["raw_segments"]] == ["胰腺癌早期信号", "别当成胃病"]
    assert view["transcript_revision"] == 0
    assert view["view"] == "effective"
    assert view["view_text"] == view["effective_text"]
    # 文档确实落盘（惰性 seed 只发生一次，且基线永不覆盖）
    assert transcript_revisions.document_path(env["project_id"], env["clip_id"]).is_file()

    raw_view = await media.studio_clip_transcript(env["project_id"], env["clip_id"], "raw")
    assert raw_view["view"] == "raw"
    assert raw_view["view_segments"] == raw_view["raw_segments"]
    assert raw_view["view_text"] == raw_view["raw_text"]

    corrected_view = await media.studio_clip_transcript(
        env["project_id"], env["clip_id"], "corrected")
    assert corrected_view["view"] == "corrected"
    # 纠错稿同样建立了基线（没有 correction_audit 时与原始稿同源，绝不伪造差异）
    assert corrected_view["view_segments"] == corrected_view["corrected_segments"]
    assert [segment["text"] for segment in corrected_view["view_segments"]] == ["胰腺癌早期信号", "别当成胃病"]


@pytest.mark.asyncio
async def test_transcript_get_rejects_unknown_view_and_unseeded_clip(env):
    with pytest.raises(HTTPException) as excinfo:
        await media.studio_clip_transcript(env["project_id"], env["clip_id"], "bogus")
    assert excinfo.value.status_code == 422
    assert "E_TRANSCRIPT_VARIANT_UNKNOWN" in excinfo.value.detail

    # 没有转写文档、产物里也没有分段 → 可读的 E_TRANSCRIPT_NOT_SEEDED（不是 500）
    with pytest.raises(HTTPException) as unseeded:
        await media.studio_clip_transcript(env["project_id"], env["clip_id"])
    assert unseeded.value.status_code == 422
    assert "E_TRANSCRIPT_NOT_SEEDED" in unseeded.value.detail


@pytest.mark.asyncio
async def test_transcript_revision_add_and_rollback(env):
    _write_render_artifacts(env, [{"start": 0.0, "end": 2.0, "text": "胰腺癌", "speaker": "医生"}])
    await media.studio_clip_transcript(env["project_id"], env["clip_id"])   # 惰性 seed

    added = await media.studio_add_clip_transcript_revision(
        env["project_id"], env["clip_id"],
        _json_request({"changes": [{"segment_id": "seg-001", "after": "胰腺癌（早期）"}],
                       "reason": "补全术语", "author": "医生"}))
    assert added["revision"] == 1
    assert added["approved_revision"] == 1
    assert added["mirror"] == "synced"
    assert added["effective_segments"][0]["text"] == "胰腺癌（早期）"
    assert added["raw_segments"][0]["text"] == "胰腺癌"          # 基线不可变
    assert _entry(env)["transcript_revision"] == 1               # 台账镜像

    rolled = await media.studio_rollback_clip_transcript(
        env["project_id"], env["clip_id"], _json_request({"revision": 1}))
    assert rolled["approved_revision"] == 1

    with pytest.raises(HTTPException) as missing:
        await media.studio_rollback_clip_transcript(
            env["project_id"], env["clip_id"], _json_request({}))
    assert missing.value.status_code == 422
    assert "E_TRANSCRIPT_REVISION_UNKNOWN" in missing.value.detail

    with pytest.raises(HTTPException) as unknown:
        await media.studio_rollback_clip_transcript(
            env["project_id"], env["clip_id"], _json_request({"revision": 9}))
    assert unknown.value.status_code == 422
    assert "E_TRANSCRIPT_REVISION_UNKNOWN" in unknown.value.detail


@pytest.mark.asyncio
async def test_transcript_revision_rejects_unknown_segment(env):
    _write_render_artifacts(env, [{"start": 0.0, "end": 2.0, "text": "胰腺癌", "speaker": "医生"}])
    with pytest.raises(HTTPException) as excinfo:
        await media.studio_add_clip_transcript_revision(
            env["project_id"], env["clip_id"],
            _json_request({"changes": [{"segment_id": "seg-999", "after": "x"}]}))
    assert excinfo.value.status_code == 422
    assert "E_TRANSCRIPT_CHANGES_INVALID" in excinfo.value.detail


@pytest.mark.asyncio
async def test_transcript_and_subtitles_reject_source_sentinel(env):
    for call in (media.studio_clip_transcript, media.studio_clip_subtitles):
        with pytest.raises(HTTPException) as excinfo:
            await call(env["project_id"], "source")
        assert excinfo.value.status_code == 422
        assert media.E_SOURCE_BINDING_UNSUPPORTED in excinfo.value.detail


# ---------------------------------------------------------------------------
# 6. 字幕：POST 生成新版本、GET 列出、无 cue 显式失败
# ---------------------------------------------------------------------------

def _cues_payload(**extra) -> _StubRequest:
    body = {
        "config": {"font_size_px": 70},
        "cues": [{"segment_id": "seg-1", "start": 0.5, "end": 2.0, "text": "术后复查"}],
        "reason": "手动调整",
    }
    body.update(extra)
    return _json_request(body)


@pytest.mark.asyncio
async def test_subtitle_post_creates_revision_and_get_lists_it(env):
    saved = await media.studio_save_clip_subtitles(
        env["project_id"], env["clip_id"], _cues_payload())
    assert saved["revision"] == 1
    assert saved["ass_path"] == f"subtitles/rev-001/{CLIP_ID}.ass"
    assert saved["srt_path"] == f"subtitles/rev-001/{CLIP_ID}.srt"
    assert saved["cue_count"] == 1
    assert saved["export_burn_in"] == "phase2"
    assert "Phase 2" in saved["note"]
    assert saved["config"]["font_size_px"] == 70
    assert saved["overrides"][0]["segment_id"] == "seg-1"
    assert saved["reason"] == "手动调整"
    # cue 转 overrides 后真的落了盘，且配置写进了该 clip 条目
    assert (env["clip_dir"] / "subtitles" / "rev-001" / f"{CLIP_ID}.ass").is_file()
    assert _entry(env)["subtitle_config"]["font_size_px"] == 70
    assert _entry(env)["subtitle_overrides"][0]["text"] == "术后复查"
    # 配置变更推进 render_revision（clip_config 语义：真变化才 bump），且不建 autoedit_*
    assert _entry(env)["render_revision"] == 2
    assert {item["task_type"] for item in _entry(env)["task_history"]} == {"UPDATE_SUBTITLE"}

    fetched = await media.studio_clip_subtitles(env["project_id"], env["clip_id"])
    assert fetched["latest_revision"] == 1
    assert fetched["revisions"][0]["ass_path"] == saved["ass_path"]
    assert fetched["revisions"][0]["cue_count"] == 1
    assert fetched["cues"][0]["text"] == "术后复查"

    # 再存一次 → 新版本（旧版本保留，不覆盖），且再推进一版
    again = await media.studio_save_clip_subtitles(
        env["project_id"], env["clip_id"], _json_request({"config": {"font_size_px": 80}}))
    assert again["revision"] == 2
    assert [item["revision"] for item in again["revisions"]] == [1, 2]
    assert again["config"]["font_size_px"] == 80
    assert _entry(env)["render_revision"] == 3
    assert {item["task_type"] for item in _entry(env)["task_history"]} == {"UPDATE_SUBTITLE"}


@pytest.mark.asyncio
async def test_subtitle_post_without_cues_or_transcript_is_422(env):
    with pytest.raises(HTTPException) as excinfo:
        await media.studio_save_clip_subtitles(
            env["project_id"], env["clip_id"], _json_request({"cues": []}))
    assert excinfo.value.status_code == 422
    assert "E_SUBTITLE_NO_CUES" in excinfo.value.detail
    # 失败不写盘：条目上不该出现 subtitle_overrides
    assert "subtitle_overrides" not in _entry(env)


@pytest.mark.asyncio
async def test_subtitle_post_rejects_bad_config_with_422(env):
    with pytest.raises(HTTPException) as excinfo:
        await media.studio_save_clip_subtitles(
            env["project_id"], env["clip_id"],
            _json_request({"config": {"font_size_px": 9999}}))
    assert excinfo.value.status_code == 422
    assert "E_CONFIG_INVALID" in excinfo.value.detail


@pytest.mark.asyncio
async def test_subtitle_post_recomposes_from_transcript_segments(env):
    _write_render_artifacts(env, [
        {"start": 0.0, "end": 2.0, "text": "第一句", "speaker": "医生"},
        {"start": 2.0, "end": 4.0, "text": "第二句", "speaker": "医生"},
    ])
    saved = await media.studio_save_clip_subtitles(
        env["project_id"], env["clip_id"], _json_request({"config": {"outline_width": 6}}))
    assert saved["cue_count"] == 2
    assert [cue["text"] for cue in saved["cues"]] == ["第一句", "第二句"]


# ---------------------------------------------------------------------------
# 7. BGM：上传（表单覆盖 + 峰值）、删除幂等、未登记 clip 404
# ---------------------------------------------------------------------------

@pytest.fixture
def fast_audio(monkeypatch):
    """不调用真实 ffmpeg：固定时长与峰值，让断言只看端点行为。"""
    monkeypatch.setattr(clip_audio, "probe_duration", lambda _path: 12.5)
    monkeypatch.setattr(clip_audio, "extract_peaks",
                        lambda _path, points=clip_audio.MAX_PEAKS: [0.25, 0.5])


@pytest.mark.asyncio
async def test_bgm_upload_registers_track_and_returns_peaks(env, fast_audio):
    before = _autoedit_artifacts(env["root"])
    payload = await media.studio_upload_clip_bgm(
        env["project_id"], env["clip_id"], _upload(), 0.4, 1.0, 2.0)

    track = payload["track"]
    assert payload["peaks"] == [0.25, 0.5]
    assert track["path"].startswith("audio/bgm-")
    assert track["asset_id"].startswith("bgm-")
    assert (track["volume"], track["fade_in_s"], track["fade_out_s"]) == (0.4, 1.0, 2.0)
    assert track["duration_s"] == 12.5
    assert (env["clip_dir"] / track["path"]).is_file()
    assert payload["audio_tracks"][0]["asset_id"] == track["asset_id"]
    assert payload["export_mix"] == "phase2"
    assert _entry(env)["audio_tracks"][0]["asset_id"] == track["asset_id"]
    assert _entry(env)["render_revision"] == 2
    assert _entry(env)["task_history"][-1]["task_type"] == "UPDATE_AUDIO"
    assert _autoedit_artifacts(env["root"]) == before


@pytest.mark.asyncio
async def test_bgm_upload_validates_before_copying_the_file(env, fast_audio):
    with pytest.raises(HTTPException) as excinfo:
        await media.studio_upload_clip_bgm(env["project_id"], env["clip_id"],
                                           _upload(), 2.5, None, None)
    assert excinfo.value.status_code == 422
    assert "E_AUDIO_TRACK_INVALID" in excinfo.value.detail
    # 越界音量在复制文件之前失败 → audio/ 目录里没有孤儿文件
    assert not (env["clip_dir"] / "audio").exists()
    assert _entry(env).get("audio_tracks") is None


@pytest.mark.asyncio
async def test_bgm_upload_rejects_unsupported_format(env, fast_audio):
    with pytest.raises(HTTPException) as excinfo:
        await media.studio_upload_clip_bgm(env["project_id"], env["clip_id"],
                                           _upload("notes.txt"), None, None, None)
    assert excinfo.value.status_code == 422
    assert "E_AUDIO_UNSUPPORTED_FORMAT" in excinfo.value.detail


@pytest.mark.asyncio
async def test_bgm_delete_is_idempotent_and_updates_the_record(env, fast_audio):
    uploaded = await media.studio_upload_clip_bgm(
        env["project_id"], env["clip_id"], _upload(), None, None, None)
    asset_id = uploaded["track"]["asset_id"]

    removed = await media.studio_delete_clip_bgm(env["project_id"], env["clip_id"], asset_id)
    assert removed["removed"] is True
    assert removed["asset_id"] == asset_id
    assert removed["audio_tracks"] == []
    assert not (env["clip_dir"] / "audio" / f"{asset_id}.wav").exists()
    assert _entry(env)["audio_tracks"] == []

    again = await media.studio_delete_clip_bgm(env["project_id"], env["clip_id"], asset_id)
    assert again["removed"] is False          # 幂等：文件本来就不在
    assert again["audio_tracks"] == []


@pytest.mark.asyncio
async def test_bgm_delete_rejects_traversal_asset_id(env, fast_audio):
    with pytest.raises(HTTPException) as excinfo:
        await media.studio_delete_clip_bgm(env["project_id"], env["clip_id"], "..%2Fsecret")
    assert excinfo.value.status_code == 422
    assert "E_AUDIO_TRACK_INVALID" in excinfo.value.detail


@pytest.mark.asyncio
async def test_unregistered_clip_is_404_not_500(env, fast_audio):
    with pytest.raises(HTTPException) as subtitles:
        await media.studio_clip_subtitles(env["project_id"], "clip-nope")
    assert subtitles.value.status_code == 404

    with pytest.raises(HTTPException) as transcript:
        await media.studio_clip_transcript(env["project_id"], "clip-nope")
    assert transcript.value.status_code == 404

    with pytest.raises(HTTPException) as bgm:
        await media.studio_upload_clip_bgm(env["project_id"], "clip-nope",
                                           _upload(), None, None, None)
    assert bgm.value.status_code == 404

    with pytest.raises(HTTPException) as removal:
        await media.studio_delete_clip_bgm(env["project_id"], "clip-nope", "bgm-deadbeef")
    assert removal.value.status_code == 404


# ---------------------------------------------------------------------------
# 转写惰性 seed：原始 ASR（correction_audit）与纠错稿必须是**两个**基线
# ---------------------------------------------------------------------------

def _write_correction_artifact(env, entries: list[dict]) -> None:
    """把转写 artifact 放到 ``_correction_audit`` 的查找路径之一（按项目前缀匹配）。"""
    prefix = env["project_id"][len("timeline_"):]
    job_dir = env["root"] / "tmp" / "studio-jobs" / f"{prefix}0000"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "transcript.json").write_text(json.dumps({
        "timeline": {"id": env["project_id"]},
        "correction_audit": entries,
    }, ensure_ascii=False), encoding="utf-8")


def test_transcript_seed_keeps_original_asr_apart_from_corrected(env):
    """raw = correction_audit 的原始 ASR，corrected = 成片产物文本，两者不得混为一谈。

    背景：video-brain 会把任务 JSON 的 ``asr_segments`` 换成**纠错后**文本，所以只从成片
    产物 seed 会让「原始稿 == 纠错稿」，三态切换失去对照意义（任务书要求证明原始 ASR
    没有被覆盖）。这里断言两个基线真的不同，且 effective 跟随 corrected。
    """
    _write_render_artifacts(env, [
        {"start": 0.0, "end": 2.0, "text": "大家好今天我们聊高血压"},
        {"start": 2.0, "end": 5.0, "text": "第二个误区是擅自停药"},
    ])
    _write_correction_artifact(env, [
        {"segment_id": "seg_0000", "start": 0.0, "end": 2.0,
         "raw_text": "大家好 今天我们聊高血压", "corrected_text": "大家好今天我们聊高血压"},
        {"segment_id": "seg_0001", "start": 2.0, "end": 5.0,
         "raw_text": "第二个物区是擅自停药", "corrected_text": "第二个误区是擅自停药"},
        {"segment_id": "seg_0002", "start": 8.0, "end": 9.0,
         "raw_text": "切片窗口外的段", "corrected_text": "切片窗口外的段"},
    ])

    view = media._transcript_view(env["project_id"], env["clip_id"], None)
    assert len(view["raw_segments"]) == 2 and len(view["corrected_segments"]) == 2
    assert view["raw_segments"][0]["text"] == "大家好 今天我们聊高血压"
    assert view["corrected_segments"][0]["text"] == "大家好今天我们聊高血压"
    assert view["raw_text"] != view["corrected_text"]
    # 窗口外的段不进本 clip
    assert all("窗口外" not in item["text"] for item in view["raw_segments"])
    # 有效稿跟随纠错稿（且未批准修订时等于纠错稿）
    assert view["effective_segments"] == view["corrected_segments"]
    # 第二次读取绝不覆盖已建立的基线
    again = media._transcript_view(env["project_id"], env["clip_id"], None)
    assert again["raw_text"] == view["raw_text"]


def test_transcript_seed_without_audit_degrades_honestly(env):
    """拿不到 correction_audit 时 raw == corrected（不伪造一份「原始稿」）。"""
    _write_render_artifacts(env, [{"start": 0.0, "end": 2.0, "text": "只有纠错稿"}])
    view = media._transcript_view(env["project_id"], env["clip_id"], None)
    assert view["raw_text"] == view["corrected_text"] == "只有纠错稿"
