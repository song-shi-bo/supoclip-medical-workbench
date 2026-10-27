"""原片预览代理测试（纯逻辑，不真实转码）。

覆盖：
- 编码判定：H.264 源片标记 native（不转码），HEVC 标记需要代理；
- 代理存在时直接 ready；
- 并发触发幂等（第二次调用返回 building，不重复启动）；
- 代理路径与 .part 原子改名约定；
- 缺源片/多源片时显式失败，不静默。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import preview_proxy as pp


def _make_project(tmp_path: Path, *, files: int = 1) -> Path:
    project = tmp_path / "studio-projects" / "timeline_test"
    raw = project / "raw"
    raw.mkdir(parents=True)
    (project / "project.json").write_text(json.dumps({"id": "timeline_test"}), encoding="utf-8")
    for index in range(files):
        (raw / f"source{index}.mp4").write_bytes(b"\x00" * 32)
    return project


def _stub_probe(monkeypatch, codec: str) -> None:
    monkeypatch.setattr(pp, "probe_video", lambda path: {"codec_name": codec, "width": 1920, "height": 1080})


def test_native_when_source_is_browser_friendly(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _stub_probe(monkeypatch, "h264")
    status = pp.status(project)
    assert status.state == "native"
    assert status.path == pp.source_video(project)
    # native 情况下不应触发任何构建
    assert pp.start_build(project).state == "native"


def test_hevc_without_proxy_reports_failed_then_builds(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _stub_probe(monkeypatch, "hevc")
    assert pp.status(project).state == "failed"  # 尚无代理：显式告知原因

    started = pp.start_build(project)
    assert started.state == "building"
    # 第二次触发幂等：仍是 building，不会重复进线程
    assert pp.start_build(project).state == "building"

    # 模拟后台转码完成（直接落产物）
    target = pp.proxy_path(project)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00" * 64)
    assert pp.status(project).state == "ready"
    assert pp.status(project).path == target


def test_ready_proxy_short_circuits(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _stub_probe(monkeypatch, "hevc")
    target = pp.proxy_path(project)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00" * 64)
    assert pp.start_build(project).state == "ready"
    assert pp.status(project).path == target


def test_proxy_path_is_per_project_and_height_scoped(tmp_path):
    project = _make_project(tmp_path)
    assert pp.proxy_path(project) == project / "preview" / f"preview-{pp.PREVIEW_MAX_HEIGHT}.mp4"


def test_missing_source_fails_loudly(tmp_path):
    project = _make_project(tmp_path, files=0)
    status = pp.status(project)
    assert status.state == "failed"
    assert "source" in status.detail.lower()
    with pytest.raises(pp.PreviewProxyError):
        pp.source_video(project)


def test_ambiguous_source_fails_loudly(tmp_path):
    project = _make_project(tmp_path, files=2)
    with pytest.raises(pp.PreviewProxyError):
        pp.source_video(project)


def test_build_writes_part_then_renames(tmp_path, monkeypatch):
    """转码命令写 .part，成功后原子改名，且不残留 .part。"""
    project = _make_project(tmp_path)
    src = pp.source_video(project)
    captured: dict = {}

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(command, capture_output=True, text=True, timeout=0):
        captured["cmd"] = command
        out = Path(command[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00" * 128)  # ffmpeg 产出
        return Result()

    monkeypatch.setattr(pp.subprocess, "run", fake_run)
    pp._build(project, src)
    target = pp.proxy_path(project)
    assert target.is_file()
    assert not target.with_suffix(".part.mp4").exists()
    assert "libx264" in captured["cmd"] and "+faststart" in captured["cmd"]
    assert f"scale=-2:{pp.PREVIEW_MAX_HEIGHT}" in captured["cmd"]
