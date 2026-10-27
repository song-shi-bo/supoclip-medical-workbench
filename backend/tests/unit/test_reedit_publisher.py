"""`_upload_reedit_clip_artifacts`（Step 3 发布契约）单元测试.

只测发布事务本身：真实复制 → 副本存在/大小/ffprobe 验证 → 带 sha256 登记。
覆盖：payload 契约、发布失败向上抛、空发布、产物缺失、副本损坏（大小不符/
不可解码）一律 raise——由调用方终止替换（旧切片保留），绝不带病激活。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src import clip_render as cr
from src import r2
from src import studio_job_worker as worker


@pytest.fixture(scope="module")
def clip_videos(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    """三条真实可解码的小成片（合成素材，ffprobe 可过）。"""
    root = tmp_path_factory.mktemp("pub-media")
    videos: list[Path] = []
    for index in range(3):
        path = root / f"clip-pub{index + 1}.mp4"
        subprocess.run(
            [cr._resolve(cr.FFMPEG_BIN), "-y",
             "-f", "lavfi", "-i", f"testsrc2=duration=2:size=160x120:rate=10",
             "-f", "lavfi", "-i", f"sine=frequency=440:duration=2",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
             str(path)],
            check=True, capture_output=True)
        videos.append(path)
    return videos


def _sha(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _install_fake_object_store(monkeypatch: pytest.MonkeyPatch, root: Path):
    """r2 → 真实落盘桩：`<root>/objectstore/<key>`（local:/R2 key 通吃）。"""

    def local_path_for(object_key: str) -> Path:
        raw = (object_key[len(r2.LOCAL_SCHEME):]
               if object_key.startswith(r2.LOCAL_SCHEME) else object_key)
        return root / "objectstore" / raw.lstrip("/").replace("..", "_")

    def upload_file(path, key):
        target = local_path_for(str(key))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(path), str(target))
        return {"object_key": str(key), "size": Path(path).stat().st_size}

    monkeypatch.setattr("src.r2.local_path_for", local_path_for)
    monkeypatch.setattr("src.r2.upload_file", upload_file)
    return upload_file


def _capture_http(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    posts: list[dict] = []

    def capture(method, url, payload=None, timeout=60):
        if method == "POST" and str(url).endswith("/artifacts") and isinstance(payload, dict):
            posts.append(dict(payload))
        return {}

    monkeypatch.setattr(worker, "_http", capture)
    return posts


def _entry(clip_id: str, status: str, path: Path | None) -> dict:
    return {"clip_id": clip_id, "status": status,
            "path": str(path) if path else "", "warnings": [], "render_warnings": []}


def test_publish_success_registers_payload_and_verified_copy(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clip_videos: list[Path]):
    _install_fake_object_store(monkeypatch, tmp_path)
    posts = _capture_http(monkeypatch)
    job_id = "autoedit_publish1"
    aggregate = {"job_status": "completed", "entries": [
        _entry("clip-pub1", "rendered", clip_videos[0]),
        _entry("clip-pub2", "skipped_playable", clip_videos[1]),
        _entry("clip-pub3", "technical_error", None),  # 技术故障条目不发布
    ], "counts": {"succeeded": 2, "technical_error": 1, "skipped_playable": 0,
                  "total": 3}, "warnings": []}

    published = worker._upload_reedit_clip_artifacts(job_id, aggregate)

    # 返回值：只含成功发布的两条，字段齐全
    assert [item["clip_id"] for item in published] == ["clip-pub1", "clip-pub2"]
    for item in published:
        assert item["object_key"] == f"studio-artifacts/{job_id}/{item['clip_id']}/clip_final.mp4"
        assert item["size_bytes"] > 0 and len(item["sha256"]) == 64

    # 登记 payload 契约：kind/clip_id/object_key/size/sha256 与真实副本一致
    clip_posts = {p["clip_id"]: p for p in posts if p.get("kind") == "clip"}
    assert set(clip_posts) == {"clip-pub1", "clip-pub2"}
    source_by_clip = {"clip-pub1": clip_videos[0], "clip-pub2": clip_videos[1]}
    for clip_id, payload in clip_posts.items():
        source = source_by_clip[clip_id]
        copy = tmp_path / "objectstore" / "studio-artifacts" / job_id / clip_id / "clip_final.mp4"
        assert payload["object_key"].startswith(f"studio-artifacts/{job_id}/")
        assert payload["object_key"].endswith("/clip_final.mp4")
        assert copy.is_file()
        assert payload["size_bytes"] == source.stat().st_size == copy.stat().st_size
        assert payload["sha256"] == _sha(source) == _sha(copy)
        playable, reason = cr._ffprobe_playable(copy)
        assert playable, reason

    # 技术故障条目：没有登记、没有副本
    assert not (tmp_path / "objectstore" / "studio-artifacts" / job_id
                / "clip-pub3").exists()
    assert all(p.get("clip_id") != "clip-pub3" for p in posts)


def test_publish_upload_failure_raises_and_stops(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clip_videos: list[Path]):
    fake_upload = _install_fake_object_store(monkeypatch, tmp_path)
    posts = _capture_http(monkeypatch)
    job_id = "autoedit_publish2"

    def flaky_upload(path, key):
        if "clip-pub2" in str(key):
            raise OSError("模拟对象存储写入失败")
        return fake_upload(path, key)

    monkeypatch.setattr("src.r2.upload_file", flaky_upload)
    aggregate = {"job_status": "completed", "entries": [
        _entry("clip-pub1", "rendered", clip_videos[0]),
        _entry("clip-pub2", "rendered", clip_videos[1]),
    ], "counts": {}, "warnings": []}

    with pytest.raises(OSError, match="模拟对象存储写入失败"):
        worker._upload_reedit_clip_artifacts(job_id, aggregate)

    # 失败条目：无登记、无发布副本；前面成功条目的副本已落盘（调用方负责终止替换）
    assert all(p.get("clip_id") != "clip-pub2" for p in posts)
    assert not (tmp_path / "objectstore" / "studio-artifacts" / job_id
                / "clip-pub2").exists()
    assert (tmp_path / "objectstore" / "studio-artifacts" / job_id
            / "clip-pub1" / "clip_final.mp4").is_file()


def test_publish_raises_when_no_publishable_entries(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clip_videos: list[Path]):
    _install_fake_object_store(monkeypatch, tmp_path)
    posts = _capture_http(monkeypatch)
    aggregate = {"job_status": "technical_failed", "entries": [
        _entry("clip-pub1", "technical_error", None),
    ], "counts": {}, "warnings": []}

    with pytest.raises(RuntimeError, match="没有任何可发布的切片产物"):
        worker._upload_reedit_clip_artifacts("autoedit_publish3", aggregate)
    assert posts == []


def test_publish_raises_when_artifact_file_missing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clip_videos: list[Path]):
    _install_fake_object_store(monkeypatch, tmp_path)
    posts = _capture_http(monkeypatch)
    aggregate = {"job_status": "completed", "entries": [
        _entry("clip-pub1", "rendered", None),  # 渲染层声称成功但产物缺失
    ], "counts": {}, "warnings": []}

    with pytest.raises(RuntimeError, match="新切片产物缺失"):
        worker._upload_reedit_clip_artifacts("autoedit_publish4", aggregate)
    assert posts == []


def test_publish_rejects_size_mismatched_copy(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clip_videos: list[Path]):
    def broken_upload(path, key):
        target = tmp_path / "objectstore" / str(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = Path(path).read_bytes()
        target.write_bytes(data[: len(data) // 2])  # 截断副本
        return {"object_key": str(key), "size": Path(path).stat().st_size}

    monkeypatch.setattr("src.r2.local_path_for",
                        lambda key: tmp_path / "objectstore" / str(key).lstrip("/"))
    monkeypatch.setattr("src.r2.upload_file", broken_upload)
    _capture_http(monkeypatch)
    aggregate = {"job_status": "completed", "entries": [
        _entry("clip-pub1", "rendered", clip_videos[0]),
    ], "counts": {}, "warnings": []}

    with pytest.raises(RuntimeError, match="发布副本大小不一致"):
        worker._upload_reedit_clip_artifacts("autoedit_publish5", aggregate)


def test_publish_rejects_undecodable_copy(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clip_videos: list[Path]):
    def garbage_upload(path, key):
        # 大小一致的“假副本”：非视频字节，ffprobe 必然失败
        target = tmp_path / "objectstore" / str(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00" * Path(path).stat().st_size)
        return {"object_key": str(key), "size": Path(path).stat().st_size}

    monkeypatch.setattr("src.r2.local_path_for",
                        lambda key: tmp_path / "objectstore" / str(key).lstrip("/"))
    monkeypatch.setattr("src.r2.upload_file", garbage_upload)
    _capture_http(monkeypatch)
    aggregate = {"job_status": "completed", "entries": [
        _entry("clip-pub1", "rendered", clip_videos[0]),
    ], "counts": {}, "warnings": []}

    with pytest.raises(RuntimeError, match="发布副本不可解码"):
        worker._upload_reedit_clip_artifacts("autoedit_publish6", aggregate)
