"""切片选择（2026-10-21 阶段）定向测试：active_clip_id 持久化 + 素材卡真实第一帧。

覆盖：
* ``store.set_active_clip``：设置/切换/清除、幂等（同值不递增版本）、未登记 clip 拒绝、
  删除期项目拒绝、老台账缺字段读路径补 None；
* ``GET /studio/projects/{id}/clips`` 载荷透出 ``active_clip_id``（前端刷新恢复的唯一事实源）；
* ``PUT /studio/projects/{id}/active-clip`` 路由：成功写台账、未登记 404、非 mutable 409；
* ``GET /studio/projects/{id}/clips/{clip_id}/first-frame.jpg``：路由注册、磁盘缓存命中、
  成片文件更新后缓存失效、视频缺失 404、抽帧失败 404（绝不用别的切片/封面冒充），
  以及一条真实 ffmpeg 抽帧（环境缺 ffmpeg 时 skip）。

风格沿用 test_cover_assets / test_inspector_endpoints：tmp 根合成数据、直接 await 端点
协程、stub 认证与 config，不碰真实项目、不触发 ASR/分析/渲染、不改成片文件。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from src import studio_access as sa
from src import studio_project_store as store
from src.api.routes import media
from src.studio_access import AccessLedger

PID = "timeline_clipselect01"
OTHER = "timeline_clipselect02"
TASK = "autoedit_aaaabbbbcccc"
CLIP_A = "clip-select-aaaa"
CLIP_B = "clip-select-bbbb"


def principal(subject="8"):
    import time
    return {"subject": subject, "slug": subject, "domain_id": sa.DOMAIN_ID,
            "expires_at_ms": int(time.time() * 1000) + 60000}


@pytest.fixture
def env(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    (tasks / "projects").mkdir(parents=True)
    (tasks / "completed").mkdir(parents=True)
    monkeypatch.setattr(store, "TASKS_ROOT", tasks)
    monkeypatch.setattr(media, "TASKS_ROOT", tasks)  # studio_local_tasks 透出的同名常量
    acl = AccessLedger(tmp_path / "acl.sqlite3")
    for pid in (PID, OTHER):
        acl.register("project", pid, principal("8"))
    return {"tasks": tasks, "acl": acl}


def register_clip(clip_id: str, *, render_task_id: str = TASK) -> dict:
    return {"clip_id": clip_id, "name": clip_id, "title": clip_id,
            "render_task_id": render_task_id, "render_revision": 1, "cover": None}


def save_ledger(env, entries: list[dict], *, status: str = "active") -> None:
    manifest = store.load_manifest(PID)
    manifest["clips"] = {e["clip_id"]: e for e in entries}
    manifest["clip_order"] = [e["clip_id"] for e in entries]
    manifest["status"] = status
    store.save_manifest(PID, manifest)


def make_clip_video(env, clip_id: str, payload: bytes = b"\x00\x00\x00\x18ftypmp4") -> Path:
    path = env["tasks"] / "output" / TASK / clip_id / "clip_final.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


# ---------------------------------------------------------------------------
# store.set_active_clip：唯一指针字段的持久化语义
# ---------------------------------------------------------------------------

def test_set_active_clip_persists_switch_and_clear(env):
    save_ledger(env, [register_clip(CLIP_A), register_clip(CLIP_B)])
    first = store.set_active_clip(PID, CLIP_A)
    assert first["active_clip_id"] == CLIP_A and first["changed"] is True
    led = json.loads((env["tasks"] / "projects" / PID / "clips.json").read_text(encoding="utf-8"))
    assert led["active_clip_id"] == CLIP_A and led["active_clip_updated_at"] == first["active_clip_updated_at"]
    # 切换：A → B，唯一指针被覆盖
    second = store.set_active_clip(PID, CLIP_B)
    assert second["active_clip_id"] == CLIP_B and second["changed"] is True
    # 清除（回到原片视图）：None 合法
    cleared = store.set_active_clip(PID, None)
    assert cleared["active_clip_id"] is None and cleared["changed"] is True
    assert store.load_manifest(PID)["active_clip_id"] is None


def test_set_active_clip_idempotent_same_value(env):
    save_ledger(env, [register_clip(CLIP_A)])
    first = store.set_active_clip(PID, CLIP_A)
    again = store.set_active_clip(PID, CLIP_A)
    assert again["changed"] is False
    assert again["active_clip_updated_at"] == first["active_clip_updated_at"]
    led = json.loads((env["tasks"] / "projects" / PID / "clips.json").read_text(encoding="utf-8"))
    assert led["updated_at"] == store.load_manifest(PID)["updated_at"]


def test_set_active_clip_unknown_clip_rejected_and_state_untouched(env):
    save_ledger(env, [register_clip(CLIP_A)])
    store.set_active_clip(PID, CLIP_A)
    before = store.load_manifest(PID)
    with pytest.raises(store.ProjectStoreError) as ei:
        store.set_active_clip(PID, "clip-ghost")
    assert ei.value.code == "E_CLIP_NOT_REGISTERED"
    after = store.load_manifest(PID)
    assert after["active_clip_id"] == before["active_clip_id"] == CLIP_A
    assert after["updated_at"] == before["updated_at"]


def test_set_active_clip_blocked_while_deleting(env):
    save_ledger(env, [register_clip(CLIP_A)], status="deleting")
    with pytest.raises(store.ProjectStoreError) as ei:
        store.set_active_clip(PID, CLIP_A)
    assert ei.value.code == "E_PROJECT_NOT_MUTABLE"


def test_load_manifest_backfills_missing_or_invalid_active_clip(env):
    save_ledger(env, [register_clip(CLIP_A)])
    path = env["tasks"] / "projects" / PID / "clips.json"
    # 老台账：没有 active_clip_id 字段 → 读路径补 None，永不抛错
    document = json.loads(path.read_text(encoding="utf-8"))
    document.pop("active_clip_id", None)
    path.write_text(json.dumps(document), encoding="utf-8")
    assert store.load_manifest(PID)["active_clip_id"] is None
    # 字段类型损坏（数字/嵌套）→ 归 None，不抛错
    document["active_clip_id"] = {"oops": True}
    path.write_text(json.dumps(document), encoding="utf-8")
    assert store.load_manifest(PID)["active_clip_id"] is None


def test_cards_payload_exposes_active_clip_id(env):
    save_ledger(env, [register_clip(CLIP_A)])
    before = media._project_cards_payload(PID)
    assert before["active_clip_id"] is None  # 老台账缺省 = None，前端不恢复
    store.set_active_clip(PID, CLIP_A)
    payload = media._project_cards_payload(PID)
    assert payload["active_clip_id"] == CLIP_A
    assert payload["active_clip_updated_at"]
    assert {card["clip_id"] for card in payload["cards"]} == {CLIP_A}


# ---------------------------------------------------------------------------
# 路由层：PUT active-clip 与 GET first-frame.jpg
# ---------------------------------------------------------------------------

def _route_paths() -> set[tuple[str, str]]:
    paths = set()
    for route in media.router.routes:
        methods = getattr(route, "methods", None) or set()
        for method in methods:
            paths.add((method, getattr(route, "path", "")))
    return paths


def test_routes_registered():
    paths = _route_paths()
    assert ("PUT", "/studio/projects/{project_id}/active-clip") in paths
    assert ("GET", "/studio/projects/{project_id}/clips/{clip_id}/first-frame.jpg") in paths


@pytest.fixture
def no_owner_check(monkeypatch):
    monkeypatch.setattr(media, "_assert_project_owner", lambda request, project_id: None)


class _StubConfig:
    def __init__(self, temp_dir: Path) -> None:
        self.temp_dir = str(temp_dir)


@pytest.fixture
def stub_config(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "get_config", lambda: _StubConfig(tmp_path / "temp"))
    return tmp_path / "temp"


@pytest.mark.asyncio
async def test_put_active_clip_endpoint_success_and_404(env, no_owner_check):
    save_ledger(env, [register_clip(CLIP_A)])
    response = await media.studio_project_set_active_clip(
        PID, None, media.ProjectActiveClipRequest(clip_id=CLIP_A))
    assert response["active_clip_id"] == CLIP_A and response["changed"] is True
    assert store.load_manifest(PID)["active_clip_id"] == CLIP_A
    with pytest.raises(HTTPException) as ei:
        await media.studio_project_set_active_clip(
            PID, None, media.ProjectActiveClipRequest(clip_id="clip-ghost"))
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_put_active_clip_endpoint_409_while_deleting(env, no_owner_check):
    save_ledger(env, [register_clip(CLIP_A)], status="deleting")
    with pytest.raises(HTTPException) as ei:
        await media.studio_project_set_active_clip(
            PID, None, media.ProjectActiveClipRequest(clip_id=CLIP_A))
    assert ei.value.status_code == 409


def _patch_extractor(monkeypatch, calls: list[tuple[Path, Path]], ok: bool, payload: bytes = b"\xff\xd8stub"):
    starts: list[float] = []

    def fake_extract(video: Path, target: Path, *, start_seconds: float = 0.0) -> bool:
        calls.append((video, target))
        starts.append(float(start_seconds))
        if not ok:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return True
    monkeypatch.setattr(media, "_extract_clip_first_frame", fake_extract)
    monkeypatch.setattr(_patch_extractor, "starts", starts, raising=False)
    _patch_extractor.starts = starts


@pytest.mark.asyncio
async def test_first_frame_success_with_disk_cache(env, no_owner_check, stub_config, monkeypatch):
    video = make_clip_video(env, CLIP_A)
    save_ledger(env, [register_clip(CLIP_A)])
    calls: list[tuple[Path, Path]] = []
    _patch_extractor(monkeypatch, calls, ok=True)
    response = await media.studio_clip_first_frame(PID, CLIP_A, None)
    assert response.media_type == "image/jpeg"
    assert response.path.is_file() and response.path.stat().st_size > 0
    cache = Path(stub_config) / "studio-clip-frames" / f"{PID}.{CLIP_A}.i0.jpg"
    assert response.path == cache
    assert calls and calls[0][0] == video  # 抽帧输入是台账定位的 clip_final.mp4，不是封面
    # 第二次请求：缓存命中，不再抽帧
    again = await media.studio_clip_first_frame(PID, CLIP_A, None)
    assert again.path == cache
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_first_frame_skips_cover_intro_after_apply(env, no_owner_check, stub_config, monkeypatch):
    """应用封面后成片开头是 0.8s 封面片头：缩略图必须跳过片头，继续展示正片首帧，
    否则素材卡会整卡变成一张黑封面（用户实测「卡片变大变黑」）。"""
    video = make_clip_video(env, CLIP_A)
    save_ledger(env, [register_clip(CLIP_A)])
    store.update_fields(PID, CLIP_A, {"cover_intro_s": 0.8})
    calls: list[tuple[Path, Path]] = []
    _patch_extractor(monkeypatch, calls, ok=True)
    response = await media.studio_clip_first_frame(PID, CLIP_A, None)
    assert response.media_type == "image/jpeg"
    assert _patch_extractor.starts and _patch_extractor.starts[0] == pytest.approx(0.8)
    # 台账没有片头记录的切片（视频 mtime 更新使缓存失效）：仍然取第 0 帧（默认行为不变）
    save_ledger(env, [register_clip(CLIP_A)])
    import os as _os
    _os.utime(video, (video.stat().st_atime + 5, video.stat().st_mtime + 5))
    calls.clear()
    _patch_extractor.starts.clear()
    await media.studio_clip_first_frame(PID, CLIP_A, None)
    assert _patch_extractor.starts == [0.0]


@pytest.mark.asyncio
async def test_first_frame_cache_invalidated_when_video_newer(env, no_owner_check, stub_config, monkeypatch):
    import os
    make_clip_video(env, CLIP_A)
    save_ledger(env, [register_clip(CLIP_A)])
    calls: list[tuple[Path, Path]] = []
    _patch_extractor(monkeypatch, calls, ok=True)
    await media.studio_clip_first_frame(PID, CLIP_A, None)
    # 成片文件被替换（重渲/重剪）→ mtime 更新 → 缓存必须重抽，不返回旧画面
    video = env["tasks"] / "output" / TASK / CLIP_A / "clip_final.mp4"
    stat = video.stat()
    os.utime(video, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    await media.studio_clip_first_frame(PID, CLIP_A, None)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_first_frame_video_missing_is_404_not_substituted(env, no_owner_check, stub_config, monkeypatch):
    # A 有视频、B 登记了但文件缺失：B 必须 404，绝不拿 A 的帧或封面顶替
    make_clip_video(env, CLIP_A)
    save_ledger(env, [register_clip(CLIP_A), register_clip(CLIP_B)])
    calls: list[tuple[Path, Path]] = []
    _patch_extractor(monkeypatch, calls, ok=True)
    ok = await media.studio_clip_first_frame(PID, CLIP_A, None)
    assert ok.path.name == f"{PID}.{CLIP_A}.i0.jpg"   # 缓存名带片头长度（0=无片头）
    with pytest.raises(HTTPException) as ei:
        await media.studio_clip_first_frame(PID, CLIP_B, None)
    assert ei.value.status_code == 404
    assert ei.value.detail["code"] == "E_CLIP_FIRST_FRAME_UNAVAILABLE"
    assert len(calls) == 1  # 缺失切片根本没有触发抽帧


@pytest.mark.asyncio
async def test_first_frame_extraction_failure_is_404(env, no_owner_check, stub_config, monkeypatch):
    make_clip_video(env, CLIP_A)
    save_ledger(env, [register_clip(CLIP_A)])
    calls: list[tuple[Path, Path]] = []
    _patch_extractor(monkeypatch, calls, ok=False)
    with pytest.raises(HTTPException) as ei:
        await media.studio_clip_first_frame(PID, CLIP_A, None)
    assert ei.value.status_code == 404
    assert ei.value.detail["code"] == "E_CLIP_FIRST_FRAME_UNAVAILABLE"


@pytest.mark.asyncio
async def test_first_frame_unregistered_clip_404(env, no_owner_check, stub_config):
    save_ledger(env, [register_clip(CLIP_A)])
    with pytest.raises(HTTPException) as ei:
        await media.studio_clip_first_frame(PID, "clip-ghost", None)
    assert ei.value.status_code == 404


def test_real_ffmpeg_extracts_first_frame(env, stub_config):
    """真实抽帧冒烟：合成 9:16 迷你视频 → 第 0 帧 JPG 可解码尺寸正确（无 ffmpeg 则 skip）。"""
    try:
        ffmpeg = media.resolve_ffmpeg()
    except Exception:  # noqa: BLE001 — CoverReplaceError(E_COVER_FFMPEG_MISSING)
        pytest.skip("环境无 ffmpeg（STUDIO_FFMPEG 未配置且候选路径不存在）")
    import subprocess
    video = make_clip_video(env, CLIP_A, payload=b"")
    video.write_bytes(b"")  # 由 ffmpeg 生成真实 mp4 覆盖
    generated = subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=360x640:rate=10:duration=0.4",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", str(video)],
        capture_output=True, timeout=60)
    if generated.returncode != 0:
        pytest.skip(f"合成视频失败（可能缺 libx264）: {generated.stderr.decode()[-120:]}")
    target = stub_config / "studio-clip-frames" / f"{PID}.{CLIP_A}.jpg"
    assert media._extract_clip_first_frame(video, target) is True
    assert target.is_file() and target.stat().st_size > 0
    assert target.read_bytes()[:2] == b"\xff\xd8"  # JPEG magic
