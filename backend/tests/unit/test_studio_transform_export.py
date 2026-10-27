"""studio_transform_export + bridge clip_transform 的纯逻辑/小规模渲染测试。

不依赖真实任务队列：TASKS_ROOT 指向 pytest 临时目录；ffmpeg smoke 缺失时自动跳过。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src import studio_auto_edit_bridge as bridge
from src.studio_transform_export import _cache_key, export_transformed_clip


TASK_ID = "autoedit_aaaabbbbcccc"


@pytest.fixture()
def tasks_root(tmp_path, monkeypatch):
    root = tmp_path / "tasks"
    (root / "output" / TASK_ID / "clip_01").mkdir(parents=True)
    (root / "status").mkdir()
    (root / "status" / f"{TASK_ID}.json").write_text(
        json.dumps({"task_id": TASK_ID, "status": "completed", "artifacts": []}), encoding="utf-8"
    )
    monkeypatch.setattr(bridge, "TASKS_ROOT", root)
    return root


def _make_clip_video(clip_dir: Path) -> Path:
    video = clip_dir / "clip_final.mp4"
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("本机没有 ffmpeg")
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc2=size=640x360:rate=15:duration=1", "-pix_fmt", "yuv420p", str(video)],
        check=True, timeout=120,
    )
    return video


# ---------------------------------------------------------------------------
# bridge：clip_transforms 归一化 / sidecar 保存读取 / clips 合并
# ---------------------------------------------------------------------------

def test_normalise_clip_transforms_validates_and_skips_null():
    normalised = bridge._normalise_clip_transforms({
        "clip_01": {"scale": 2, "fitMode": "contain"},
        "clip_02": None,
    })
    assert normalised == {"clip_01": {
        "scale": 2.0, "positionX": 0.0, "positionY": 0.0,
        "crop": {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0},
        "fitMode": "contain", "rotation": 0,
    }}
    assert bridge._normalise_clip_transforms(None) == {}
    for bad in (["clip_01"], {"bad id!": {}}, {"clip_01": {"fitMode": "fill"}}):
        with pytest.raises(ValueError):
            bridge._normalise_clip_transforms(bad)


def test_save_and_read_clip_transform_sidecar_roundtrip(tasks_root):
    (tasks_root / "output" / TASK_ID / "clip_01" / "clip_final.mp4").write_bytes(b"synthetic-existing-video")
    saved = bridge.save_clip_transform(TASK_ID, "clip_01", {"scale": 1.5, "positionX": 30})
    assert saved["clip_01"]["scale"] == 1.5
    assert bridge.read_clip_transform(TASK_ID, "clip_01") == saved["clip_01"]

    # sidecar 文件落在任务 output 内、任务 JSON 不被改写
    sidecar = tasks_root / "output" / TASK_ID / "clip_transforms.json"
    assert sidecar.is_file()
    status_before = (tasks_root / "status" / f"{TASK_ID}.json").read_text(encoding="utf-8")
    assert bridge.save_clip_transform(TASK_ID, "clip_01", None) == {}
    assert bridge.read_clip_transform(TASK_ID, "clip_01") is None
    assert (tasks_root / "status" / f"{TASK_ID}.json").read_text(encoding="utf-8") == status_before

    for bad_clip in ("../evil", "", "a" * 100):
        with pytest.raises(ValueError):
            bridge.save_clip_transform(TASK_ID, bad_clip, {"scale": 2})
    with pytest.raises(ValueError):
        bridge.save_clip_transform(TASK_ID, "clip_01", {"fitMode": "fill"})


def test_rendered_clips_merge_sidecar_transform(tasks_root):
    plan = {"highlights": [{"clip_id": "clip_01", "start": 1.0, "end": 9.0, "topic": "测试"}]}
    (tasks_root / "output" / TASK_ID / "clip_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (tasks_root / "output" / TASK_ID / "clip_01" / "clip_final.mp4").write_bytes(b"\x00")

    # 无 sidecar → 无 transform 字段
    task = bridge.read_auto_edit_task(TASK_ID)
    assert "transform" not in task["clips"][0]

    bridge.save_clip_transform(TASK_ID, "clip_01", {"fitMode": "contain", "scale": 1.2})
    task = bridge.read_auto_edit_task(TASK_ID)
    assert task["clips"][0]["transform"]["fitMode"] == "contain"
    assert task["clips"][0]["transform"]["scale"] == 1.2


@pytest.mark.parametrize("save,value", [
    (bridge.save_clip_transform, {"scale": 1.2}),
    (bridge.save_clip_render_settings, {"volume": 0.5}),
    (bridge.save_clip_transform, None),
    (bridge.save_clip_render_settings, None),
])
def test_missing_clip_save_has_no_side_effects(tasks_root, save, value):
    root = tasks_root / "output" / TASK_ID
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with pytest.raises(FileNotFoundError):
        save(TASK_ID, "missing_clip", value)
    after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert after == before


# ---------------------------------------------------------------------------
# export_transformed_clip：参数校验 / 缓存键 / 真渲染 smoke
# ---------------------------------------------------------------------------

def test_export_rejects_bad_input(tasks_root):
    with pytest.raises(ValueError):
        export_transformed_clip(task_id=TASK_ID, clip_id="clip_01", transform={}, background_mode="green")
    with pytest.raises(FileNotFoundError):
        export_transformed_clip(task_id="autoedit_000000000000", clip_id="clip_01", transform={})
    (tasks_root / "output" / TASK_ID / "clip_01" / "clip_final.mp4").write_bytes(b"\x00")
    assert export_transformed_clip(task_id=TASK_ID, clip_id="clip_01", transform=None) == (
        tasks_root / "output" / TASK_ID / "clip_01" / "clip_final.mp4")


def test_omitted_neutral_transform_returns_original_without_probe_or_side_effects(tasks_root, monkeypatch):
    from src import studio_transform_export as export
    video = tasks_root / "output" / TASK_ID / "clip_01" / "clip_final.mp4"
    video.write_bytes(b"original-video-bytes")
    before = {str(p.relative_to(tasks_root)): p.read_bytes() for p in tasks_root.rglob("*") if p.is_file()}
    monkeypatch.setattr(export, "_probe_media", lambda *_: pytest.fail("neutral download must not probe or render"))
    assert export.export_transformed_clip(task_id=TASK_ID, clip_id="clip_01") == video
    after = {str(p.relative_to(tasks_root)): p.read_bytes() for p in tasks_root.rglob("*") if p.is_file()}
    assert before == after


@pytest.mark.parametrize("settings,transform", [
    ({"voice_volume": 0.5}, None),
    ({}, {"scale": 1.15}),
])
def test_omitted_transform_still_uses_saved_edit_or_audio_cache(tasks_root, monkeypatch, tmp_path, settings, transform):
    from src import studio_transform_export as export
    from src.clip_transform import parse_transform
    video = tasks_root / "output" / TASK_ID / "clip_01" / "clip_final.mp4"
    video.write_bytes(b"original-video-bytes")
    monkeypatch.setattr(export, "read_clip_render_settings", lambda *_: settings)
    monkeypatch.setattr(export, "read_clip_transform", lambda *_: transform)
    monkeypatch.setattr(export, "_probe_media", lambda *_: {"width": 640, "height": 360, "duration": 1, "has_audio": True})
    key = export._cache_key(parse_transform(transform).to_payload(), "blur", video, settings if settings else None)
    cached = tmp_path / TASK_ID / f"clip_01_{key}.mp4"
    cached.parent.mkdir(); cached.write_bytes(b"existing-effect-cache")
    monkeypatch.setattr(export.subprocess, "run", lambda *_a, **_k: pytest.fail("existing cache must not render"))
    assert export.export_transformed_clip(task_id=TASK_ID, clip_id="clip_01", cache_root=tmp_path) == cached


def test_cache_key_depends_on_transform_and_source(tasks_root):
    video = tasks_root / "output" / TASK_ID / "clip_01" / "clip_final.mp4"
    video.write_bytes(b"\x00")
    payload = {"scale": 1.5, "fitMode": "cover"}
    key = _cache_key(payload, "blur", video)
    assert key == _cache_key(dict(payload), "blur", video)
    assert key != _cache_key({**payload, "scale": 1.6}, "blur", video)
    assert key != _cache_key(payload, "color", video)


def test_export_full_flow_with_real_ffmpeg(tasks_root, tmp_path, monkeypatch):
    import contextlib

    from src import capacity_guard, studio_access

    # W07 计量/访问层要求真实登记任务，单元环境没有；旁路后聚焦本测试的目标：
    # ffmpeg 真渲染 + 缓存幂等（容量守卫与访问登记由各自模块的测试覆盖）。
    monkeypatch.setattr(studio_access, "heavy_operation",
                        lambda **kwargs: contextlib.nullcontext())
    monkeypatch.setattr(studio_access, "account_derived_output", lambda *a, **k: None)
    monkeypatch.setattr(capacity_guard, "reserve_capacity",
                        lambda *a, **k: contextlib.nullcontext())

    video = _make_clip_video(tasks_root / "output" / TASK_ID / "clip_01")
    ffprobe = shutil.which("ffprobe")
    cache_root = tmp_path / "cache"

    # 默认变换 → 短路返回原片（构图相同，无需重渲）
    assert export_transformed_clip(
        task_id=TASK_ID, clip_id="clip_01", transform={"scale": 1}, cache_root=cache_root
    ) == video

    transform = {"fitMode": "contain", "scale": 1.1, "positionY": 40}
    output = export_transformed_clip(
        task_id=TASK_ID, clip_id="clip_01", transform=transform,
        background_mode="blur", cache_root=cache_root,
    )
    assert output.is_file() and output.parent == cache_root / TASK_ID
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(output)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == (1080, 1920)

    # 二次导出命中缓存（mtime 不变）
    again = export_transformed_clip(
        task_id=TASK_ID, clip_id="clip_01", transform=transform,
        background_mode="blur", cache_root=cache_root,
    )
    assert again == output

    # transform=None 回退已保存 sidecar
    bridge.save_clip_transform(TASK_ID, "clip_01", transform)
    assert export_transformed_clip(
        task_id=TASK_ID, clip_id="clip_01", transform=None, cache_root=cache_root
    ) == output
