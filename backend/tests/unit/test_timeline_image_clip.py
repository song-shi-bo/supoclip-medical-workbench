"""第四步：封面图片素材进入时间线（image_clip）定向测试。

覆盖：
* store.set/get/delete_timeline_image_clip：时长钳制、role 判定（开头=cover_intro）、
  无封面素材显式失败、删除只清实例（**封面素材文件不动**）、card_view 暴露实例；
* PUT/DELETE 路由：归属校验（项目所有者）、成功写台账；
* 导出：时间线上的 cover_intro 实例参与最终渲染——
  输出 = 封面静帧 duration_s + 正片（内嵌 0.8s 旧片头被裁掉），片头段静音；
  删除实例后导出回到原片（不再前置）。

真实 ffmpeg 合成素材；不联网、不碰真实项目。
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from src import cover_realframe_v2 as crv
from src import studio_project_store as store
from src.api.routes import media

PID = "timeline_imgtl01"
RENDER = "aabbccddeeff00112233445566778899"
CLIP = "clip-imgtl01"
# 必须是真实可解码的 JPEG：ffmpeg `-loop 1` 对解不开的图片会陷入解码错误风暴
# （每轮重试刷一条 "No JPEG data found"），表现为进程挂死到 600s 超时。
# 这里内嵌 64x64 真实 JPEG（ffmpeg gradients 生成）。
COVER_BYTES = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAPTGF2YzYxLjMuMTAwAP/bAEMACAQEBAQEBQUFBQUFBgYGBgYG"
    "BgYGBgYGBgcHBwgICAcHBwYGBwcICAgICQkJCAgICAkJCgoKDAwLCw4ODhERFP/EAFwAAAMAAwEBAAAA"
    "AAAAAAAAAAAFBgQDBwECAQEBAAAAAAAAAAAAAAAAAAAABRAAAQUBAQEBAQAAAAAAAAAAACExBAUCAQMR"
    "QWERAQAAAAAAAAAAAAAAAAAAAAD/wAARCABAAEADARIAAhIAAxIA/9oADAMBAAIRAxEAPwCkNflJx6Fc"
    "SxsAAAAAAAAAAAAAAAAAAEUawZRFGsGUAKyPYff0RxrBlACn8/fHp+ieNYMoAOzDjz+dfoAZh849cbbo"
    "AfQAAAAAAAcpjWH9F/O/AAfxrBlEvlK359ACqjWDKIo1gygBWRrBlEcawZQArI1gyiKNYMoAVnhOzpxH"
    "GsGUAKTO86boqjWDKADYx/GZnfFADkIAAAAHvO94x4AG/wAZmsOaAAaxrBlFeday3QAo41gyiTwm6z1Q"
    "Aq41gyiKNYMoAVkawZRHGsGUAP/Z"
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "tasks"
    monkeypatch.setattr(store, "TASKS_ROOT", root)
    from src import studio_auto_edit_bridge as bridge
    monkeypatch.setattr(bridge, "TASKS_ROOT", root)
    monkeypatch.setattr(media, "TASKS_ROOT", root)

    clip_dir = root / "output" / RENDER / "clips" / CLIP
    clip_dir.mkdir(parents=True)
    (clip_dir / "clip_final.mp4").write_bytes(b"ORIGINAL" * 200)
    (clip_dir / "cover.jpg").write_bytes(COVER_BYTES)
    # staged 布局与真实项目一致：cover.path 必须带 clips/ 段（is_file 校验按此解析）
    store.register_clips(PID, RENDER, [{
        "clip_id": CLIP, "name": "剪辑成片 01", "topic": "健康科普",
        "source_start": 0.0, "source_end": 8.0, "duration": 8.0,
        "cover_path": f"output/{RENDER}/clips/{CLIP}/cover.jpg",
        "cover": {"path": f"output/{RENDER}/clips/{CLIP}/cover.jpg", "revision": 1,
                  "sha256": "x", "kind": "auto_edit", "intro_burned": True,
                  "cover_asset_id": f"cover-{CLIP}-v1"},
    }])
    # cover_intro_s 不在 register_clips 白名单里——生产里由应用封面任务
    # （studio_local_tasks fields_update）写入，这里按同一落点补写：
    manifest = store.load_manifest(PID)
    manifest["clips"][CLIP]["cover_intro_s"] = 0.8
    store.save_manifest(PID, manifest)
    return {"root": root, "clip_dir": clip_dir,
            "cover": clip_dir / "cover.jpg", "video": clip_dir / "clip_final.mp4"}


def test_set_clamps_duration_and_marks_head_role(env):
    item = store.set_timeline_image_clip(PID, CLIP, duration_s=99.0, start_s=0.0)
    assert item["role"] == "cover_intro"
    assert item["duration_s"] == store.TIMELINE_IMAGE_MAX_S      # 钳制到上限
    assert item["asset_path"].endswith("cover.jpg")
    items = store.get_timeline_image_clips(PID, CLIP)
    assert len(items) == 1 and items[0]["role"] == "cover_intro"
    card = store.card_view(store.entry_for(PID, CLIP))
    assert card["timeline_image_clips"][0]["duration_s"] == store.TIMELINE_IMAGE_MAX_S


def test_set_requires_cover_asset(env):
    (env["clip_dir"] / "cover.jpg").unlink()
    with pytest.raises(store.ProjectStoreError) as ei:
        store.set_timeline_image_clip(PID, CLIP, duration_s=1.5, start_s=0.0)
    assert ei.value.code == "E_TIMELINE_IMAGE_NO_ASSET"


def test_delete_removes_instance_but_keeps_cover_file(env):
    store.set_timeline_image_clip(PID, CLIP, duration_s=1.5, start_s=0.0)
    result = store.delete_timeline_image_clip(PID, CLIP)
    assert result["removed"] == 1
    assert store.get_timeline_image_clips(PID, CLIP) == []
    assert env["cover"].is_file() and env["cover"].read_bytes() == COVER_BYTES   # 素材不动
    assert env["video"].read_bytes() == b"ORIGINAL" * 200                        # 成片不动


def test_set_rejects_unregistered_clip(env):
    with pytest.raises(store.ProjectStoreError):
        store.set_timeline_image_clip(PID, "clip-not-registered", duration_s=1.5)


# ---------------------------------------------------------------------------
# 多实例（真正的时间线拖拽）：开头恒一个（重复放替换沿用 id）、多个片中实例、
# 带 instance_id 原位更新（拖拽移动）、按实例删除、未知实例显式报错。
# ---------------------------------------------------------------------------

def test_set_head_replace_keeps_id_and_allows_multiple_tails(env):
    head1 = store.set_timeline_image_clip(PID, CLIP, duration_s=1.0, start_s=0.0)
    head2 = store.set_timeline_image_clip(PID, CLIP, duration_s=2.0, start_s=0.0)
    assert head2["id"] == head1["id"] and head2["duration_s"] == 2.0
    tail_a = store.set_timeline_image_clip(PID, CLIP, duration_s=1.0, start_s=2.0)
    tail_b = store.set_timeline_image_clip(PID, CLIP, duration_s=0.5, start_s=4.0)
    items = store.get_timeline_image_clips(PID, CLIP)
    assert len(items) == 3
    assert sum(1 for item in items if item["role"] == "cover_intro") == 1
    # 拖拽移动：带 instance_id 原位更新，不新增实例
    moved = store.set_timeline_image_clip(PID, CLIP, duration_s=1.0, start_s=5.0,
                                          instance_id=tail_a["id"])
    assert moved["id"] == tail_a["id"] and moved["start_s"] == 5.0
    assert len(store.get_timeline_image_clips(PID, CLIP)) == 3
    # 位置钳到成片时长内（夹具成片 8s）
    clamped = store.set_timeline_image_clip(PID, CLIP, duration_s=1.0, start_s=99.0)
    assert clamped["start_s"] == 8.0
    assert tail_b["id"] in {item["id"] for item in store.get_timeline_image_clips(PID, CLIP)}


def test_delete_single_instance_keeps_others(env):
    store.set_timeline_image_clip(PID, CLIP, duration_s=1.0, start_s=0.0)
    tail = store.set_timeline_image_clip(PID, CLIP, duration_s=1.0, start_s=3.0)
    other = store.set_timeline_image_clip(PID, CLIP, duration_s=1.0, start_s=5.0)
    result = store.delete_timeline_image_clip(PID, CLIP, instance_id=tail["id"])
    assert result["removed"] == 1
    remaining_ids = {item["id"] for item in store.get_timeline_image_clips(PID, CLIP)}
    assert tail["id"] not in remaining_ids and other["id"] in remaining_ids
    assert env["cover"].read_bytes() == COVER_BYTES  # 封面素材文件不动
    with pytest.raises(store.ProjectStoreError) as ei:
        store.delete_timeline_image_clip(PID, CLIP, instance_id=tail["id"])
    assert ei.value.code == "E_TIMELINE_IMAGE_UNKNOWN"


@pytest.mark.asyncio
async def test_routes_put_delete_with_owner_check(env, monkeypatch):
    monkeypatch.setattr(media, "_assert_project_owner",
                        lambda request, project_id: None)
    body = media.TimelineImageRequest(duration_s=1.5, start_s=0.0)
    resp = await media.studio_set_timeline_image(PID, CLIP, None, body)
    assert resp["image_clip"]["duration_s"] == 1.5
    deleted = await media.studio_delete_timeline_image(PID, CLIP, None)
    assert deleted["deleted"] is True
    assert store.get_timeline_image_clips(PID, CLIP) == []


@pytest.mark.asyncio
async def test_routes_reject_when_not_owner(env, monkeypatch):
    def deny(request, project_id):
        raise HTTPException(status_code=403, detail={"code": "E_PROJECT_OWNER_REQUIRED"})
    monkeypatch.setattr(media, "_assert_project_owner", deny)
    with pytest.raises(HTTPException) as ei:
        await media.studio_set_timeline_image(PID, CLIP, None,
                                              media.TimelineImageRequest(duration_s=1.5))
    assert ei.value.status_code == 403
    assert store.get_timeline_image_clips(PID, CLIP) == []


@pytest.mark.asyncio
async def test_routes_instance_update_and_selective_delete(env, monkeypatch):
    """拖拽移动 = PUT 带 instance_id；删除单个实例 = DELETE ?instance_id=。"""
    monkeypatch.setattr(media, "_assert_project_owner", lambda request, project_id: None)
    first = await media.studio_set_timeline_image(
        PID, CLIP, None, media.TimelineImageRequest(duration_s=1.0, start_s=2.0))
    assert len(first["image_clips"]) == 1
    moved = await media.studio_set_timeline_image(
        PID, CLIP, None,
        media.TimelineImageRequest(duration_s=1.0, start_s=4.0,
                                   instance_id=first["image_clip"]["id"]))
    assert moved["image_clip"]["id"] == first["image_clip"]["id"]
    assert moved["image_clip"]["start_s"] == 4.0
    assert len(moved["image_clips"]) == 1
    deleted = await media.studio_delete_timeline_image(
        PID, CLIP, None, instance_id=first["image_clip"]["id"])
    assert deleted["removed"] == 1 and deleted["image_clips"] == []
    with pytest.raises(HTTPException) as ei:
        await media.studio_delete_timeline_image(
            PID, CLIP, None, instance_id=first["image_clip"]["id"])
    assert ei.value.status_code == 404


# ---------------------------------------------------------------------------
# 导出：时间线 cover_intro 实例参与最终渲染（真实 ffmpeg 合成素材）
# ---------------------------------------------------------------------------

def _make_real_clip(path: Path, seconds: float = 2.0) -> None:
    ffmpeg = "/domain/dsh-home/bin/ffmpeg"
    completed = subprocess.run([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=0x334455:s=360x640:rate=10:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr[-200:]


def test_export_prepends_timeline_cover_intro(env, tmp_path, monkeypatch):
    if not Path("/domain/dsh-home/bin/ffmpeg").is_file():
        pytest.skip("容器无 ffmpeg")
    _make_real_clip(env["video"])
    original_duration = 2.0
    # 台账登记：时间线上有一个 1.5s 的 cover_intro 实例
    entry = store.entry_for(PID, CLIP)
    entry["timeline_image_clips"] = [{
        "id": "img_test", "role": "cover_intro",
        "asset_path": f"output/{RENDER}/clips/{CLIP}/cover.jpg",
        "start_s": 0.0, "duration_s": 1.5, "updated_at": "t",
    }]
    manifest = store.load_manifest(PID)
    manifest["clips"][CLIP] = entry
    store.save_manifest(PID, manifest)

    from src import studio_transform_export as ste
    monkeypatch.setattr(ste, "clip_video_path", lambda task_id, clip_id: env["video"])
    monkeypatch.setattr(ste, "read_clip_transform", lambda task_id, clip_id: None)
    monkeypatch.setattr(ste, "read_clip_render_settings", lambda task_id, clip_id: {})
    monkeypatch.setattr(ste, "_bgm_tracks", lambda parent, settings: [])
    monkeypatch.setattr(ste, "_subtitle_burn_spec", lambda video: None)

    import types
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    monkeypatch.setattr(media, "get_config", lambda: types.SimpleNamespace(temp_dir=str(tmp_path / "timeline-exports")))
    output = ste.export_transformed_clip(
        task_id=RENDER, clip_id=CLIP, transform=None, background_mode="blur",
        cache_root=config_dir,
    )
    # 中性配置短路返回原片：时间线封面实例在导出层前置（旧片头长度按台账
    # cover_intro_s=0.8 解析：裁掉内嵌 0.8s、补 1.5s 封面）
    intro = media._timeline_cover_intro_for(RENDER, CLIP)
    assert intro is not None and intro["duration_s"] == 1.5
    previous = media._clip_cover_intro_s(RENDER, CLIP)
    assert previous == pytest.approx(0.8)
    final = media._apply_timeline_cover_intro(output, CLIP, intro, previous)
    ffprobe = "/domain/dsh-home/bin/ffprobe"
    duration = float(subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(final)],
        capture_output=True, text=True).stdout.strip())
    # 期望：内嵌 0.8s 旧片头被裁掉 + 1.5s 封面前置 → 2.0 - 0.8 + 1.5 = 2.7s
    assert duration == pytest.approx(original_duration - 0.8 + 1.5, abs=0.15)
    # 片头段静音（正片音频不被提前播放）
    peak = crv._head_audio_peak_db(final, 1.4)
    assert peak is not None and peak <= -45
    # 二次导出命中同参数缓存（同文件；缓存键含片头长度 p800）
    again = media._apply_timeline_cover_intro(output, CLIP, intro, previous)
    assert again == final


def test_export_inserts_timeline_image_at_position(env, tmp_path, monkeypatch):
    """真正的时间线拖拽：正片中间 2.5s 处插入 1.0s 封面静帧 → 总时长 +1.0s、插入段静音。"""
    if not Path("/domain/dsh-home/bin/ffmpeg").is_file():
        pytest.skip("容器无 ffmpeg")
    _make_real_clip(env["video"], seconds=6.0)
    original_duration = 6.0
    instance = {"id": "img_mid", "role": "image",
                "asset_path": f"output/{RENDER}/clips/{CLIP}/cover.jpg",
                "start_s": 2.5, "duration_s": 1.0, "updated_at": "t"}
    entry = store.entry_for(PID, CLIP)
    entry["timeline_image_clips"] = [instance]
    manifest = store.load_manifest(PID)
    manifest["clips"][CLIP] = entry
    store.save_manifest(PID, manifest)

    from src import studio_transform_export as ste
    monkeypatch.setattr(ste, "clip_video_path", lambda task_id, clip_id: env["video"])
    monkeypatch.setattr(ste, "read_clip_transform", lambda task_id, clip_id: None)
    monkeypatch.setattr(ste, "read_clip_render_settings", lambda task_id, clip_id: {})
    monkeypatch.setattr(ste, "_bgm_tracks", lambda parent, settings: [])
    monkeypatch.setattr(ste, "_subtitle_burn_spec", lambda video: None)

    import types
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    monkeypatch.setattr(media, "get_config", lambda: types.SimpleNamespace(temp_dir=str(tmp_path / "timeline-exports")))
    output = ste.export_transformed_clip(
        task_id=RENDER, clip_id=CLIP, transform=None, background_mode="blur",
        cache_root=config_dir,
    )
    final = media._apply_timeline_image_tails(output, CLIP, [instance])
    ffprobe = "/domain/dsh-home/bin/ffprobe"
    duration = float(subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(final)],
        capture_output=True, text=True).stdout.strip())
    assert duration == pytest.approx(original_duration + 1.0, abs=0.15)
    # 插入段静音（封面静帧期间不播原声）
    peak = crv._audio_peak_db(final, 2.6, 0.8)
    assert peak is not None and peak <= -45
    # 二次导出命中同参数缓存（缓存键 = 实例 id/位置/时长 摘要）
    again = media._apply_timeline_image_tails(output, CLIP, [instance])
    assert again == final


def test_export_tail_instances_sequential_and_end_append(env, tmp_path, monkeypatch):
    """两个实例：2.0s 处插 0.5s + 末尾(99s→钳到末尾)追加 0.5s → 总时长 +1.0s。"""
    if not Path("/domain/dsh-home/bin/ffmpeg").is_file():
        pytest.skip("容器无 ffmpeg")
    _make_real_clip(env["video"], seconds=4.0)
    instances = [
        {"id": "img_a", "role": "image",
         "asset_path": f"output/{RENDER}/clips/{CLIP}/cover.jpg",
         "start_s": 2.0, "duration_s": 0.5, "updated_at": "t"},
        {"id": "img_b", "role": "image",
         "asset_path": f"output/{RENDER}/clips/{CLIP}/cover.jpg",
         "start_s": 99.0, "duration_s": 0.5, "updated_at": "t"},
    ]
    entry = store.entry_for(PID, CLIP)
    entry["timeline_image_clips"] = list(instances)
    manifest = store.load_manifest(PID)
    manifest["clips"][CLIP] = entry
    store.save_manifest(PID, manifest)

    from src import studio_transform_export as ste
    monkeypatch.setattr(ste, "clip_video_path", lambda task_id, clip_id: env["video"])
    monkeypatch.setattr(ste, "read_clip_transform", lambda task_id, clip_id: None)
    monkeypatch.setattr(ste, "read_clip_render_settings", lambda task_id, clip_id: {})
    monkeypatch.setattr(ste, "_bgm_tracks", lambda parent, settings: [])
    monkeypatch.setattr(ste, "_subtitle_burn_spec", lambda video: None)

    import types
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    monkeypatch.setattr(media, "get_config", lambda: types.SimpleNamespace(temp_dir=str(tmp_path / "timeline-exports")))
    output = ste.export_transformed_clip(
        task_id=RENDER, clip_id=CLIP, transform=None, background_mode="blur",
        cache_root=config_dir,
    )
    final = media._apply_timeline_image_tails(output, CLIP, instances)
    ffprobe = "/domain/dsh-home/bin/ffprobe"
    duration = float(subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(final)],
        capture_output=True, text=True).stdout.strip())
    assert duration == pytest.approx(4.0 + 1.0, abs=0.2)
    # 中间插入段静音（窗口避开 AAC concat 边界 ~50ms 的提前量）
    peak = crv._audio_peak_db(final, 2.1, 0.25)
    assert peak is not None and peak <= -45
