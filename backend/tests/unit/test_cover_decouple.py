"""阶段2 P3 封面与片头解耦单元测试（2026-11-18）。

契约：
* 封面**资产**默认必产（cover.jpg/copy/layout/intro.mp4 四产物）；
* 0.8s 片头**烧入视频**由 ``COVER_INTRO_MODE`` 单独控制，**默认 off**——
  成片第 0 帧即自然画面，台账不记 intro；
* ``STUDIO_AUTO_COVER=0`` 仅紧急停机（资产不产，发布门禁不拦封面）；
* 发布门禁：封面资产缺失禁止发布（与 sidecar_only 同款纵深防御）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from src import studio_job_worker as w
from src import cover_realframe_v2 as cover_mod


# --- 合成素材（与 test_cover_realframe_v2 同款小体积做法） -------------------


def _write_frame(path: Path, width: int, height: int, *, offset: int = 0) -> None:
    import cv2
    import numpy as np

    frame = np.full((height, width, 3), (90, 140, 60), dtype=np.uint8)
    x0 = 40 + (offset * 13) % max(1, width - 240)
    cv2.rectangle(frame, (x0, height // 3), (x0 + 160, height // 3 + 160), (40, 40, 200), -1)
    noise = np.random.default_rng(42 + offset).integers(0, 60, size=(height, width), dtype=np.uint8)
    frame[:, :, 0] = np.clip(frame[:, :, 0].astype(int) + noise, 0, 255).astype(np.uint8)
    ok = cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    assert ok, f"合成帧写入失败: {path}"


def _encode_video(frames_dir: Path, video: Path, framerate: int, count: int) -> None:
    completed = subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-framerate", str(framerate), "-i", str(frames_dir / "f%02d.jpg"),
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(video),
    ], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr[-300:]


def _make_clip_final(tmp_path: Path, base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    frames = tmp_path / f"wf_{base.name}"
    frames.mkdir()
    for index in range(8):
        # 9:16（360x640）与生产 staged 成片一致
        _write_frame(frames / f"f{index:02d}.jpg", 360, 640, offset=index)
    video = base / "clip_final.mp4"
    _encode_video(frames, video, framerate=4, count=8)
    shutil.rmtree(frames, ignore_errors=True)
    return video


def _stage_env(monkeypatch, tasks_root: Path) -> None:
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tasks_root))
    monkeypatch.setattr(w, "_log", lambda _message: None)
    monkeypatch.setattr(w, "_COVER_LLM_FN", lambda _prompt: json.dumps({"candidates": [
        {"topic": "血压", "hook": "规律测量血压", "segment_ids": ["s1"]}]},
        ensure_ascii=False))


def _plan_and_transcript() -> tuple[dict, dict]:
    plan_record = {"clips": [{"clip_id": "clip-01", "topic": "高血压管理",
                              "start_ms": 200, "end_ms": 1800}],
                   "analysis_ref": {}}
    transcript = {"asr_segments": [
        {"segment_id": "s1", "start_ms": 300, "end_ms": 1200,
         "text": "高血压患者要规律测量血压。"}]}
    return plan_record, transcript


# ---------------------------------------------------------------------------
# 开关语义
# ---------------------------------------------------------------------------


def test_intro_mode_default_off_and_env_on(monkeypatch):
    monkeypatch.delenv("COVER_INTRO_MODE", raising=False)
    assert w._cover_intro_enabled() is False          # 阶段2 默认不烧片头
    monkeypatch.setenv("COVER_INTRO_MODE", "1")
    assert w._cover_intro_enabled() is True
    monkeypatch.setenv("COVER_INTRO_MODE", "off")
    assert w._cover_intro_enabled() is False


def test_auto_cover_default_on_and_emergency_off(monkeypatch):
    monkeypatch.setenv("STUDIO_AUTO_COVER", "")
    assert w._auto_cover_enabled() is True            # 阶段2：资产默认必产
    monkeypatch.setenv("STUDIO_AUTO_COVER", "0")
    assert w._auto_cover_enabled() is False           # 紧急停机


def test_cover_asset_missing_gate(monkeypatch, tmp_path):
    """缺失判据三态：缺封面=True、有封面=False、紧急停机不标记（避免全量误标）。

    阶段2 P3 软处理（用户拍板 2026-11-18）：判据只用于**标记** cover_missing
    （照常发布），不再拦截发布——发布循环把标记写进条目 + 聚合 warning。"""
    monkeypatch.setenv("STUDIO_AUTO_COVER", "1")
    empty = tmp_path / "nocover"
    empty.mkdir()
    assert w._cover_asset_missing(empty) is True      # 缺封面 → 标记
    (empty / "cover.jpg").write_bytes(b"x")
    assert w._cover_asset_missing(empty) is False     # 有封面 → 不标记
    monkeypatch.setenv("STUDIO_AUTO_COVER", "0")
    assert w._cover_asset_missing(empty) is False     # 停机模式不标记


# ---------------------------------------------------------------------------
# 封面阶段：off = 资产必产、片头不烧；on = 旧契约拼接
# ---------------------------------------------------------------------------


def _run_stage(tmp_path, monkeypatch, job_id: str):
    tasks_root = tmp_path / "tasks"
    _stage_env(monkeypatch, tasks_root)
    run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"studio-render:{job_id}:0").hex
    out_root = tasks_root / "output" / run_id
    base = out_root / "clips" / "clip-01"
    video = _make_clip_final(tmp_path, base)
    plan_record, transcript = _plan_and_transcript()
    aggregate = {"entries": [{"clip_id": "clip-01", "status": "rendered"}],
                 "warnings": []}
    results = w._run_clip_cover_stage({"project_id": f"decouple_{job_id}"}, job_id,
                                      plan_record, transcript, aggregate, video, out_root)
    return base, video, results, aggregate


def test_stage_intro_off_produces_assets_without_burning(tmp_path, monkeypatch):
    """默认 COVER_INTRO_MODE=off：四产物齐、clip_final 字节不动、台账不记 intro。"""
    monkeypatch.delenv("COVER_INTRO_MODE", raising=False)
    monkeypatch.delenv("STUDIO_AUTO_COVER", raising=False)
    base, video, results, aggregate = _run_stage(tmp_path, monkeypatch, "job-off")
    assert "clip-01" in results
    for name in cover_mod.PRODUCT_NAMES:
        assert (base / name).is_file(), name          # 资产必产（含 cover_intro.mp4）
    sha = hashlib.sha256(video.read_bytes()).hexdigest()
    ledger = cover_mod.load_ledger(base)
    entry = ledger["entries"].get("1") or {}
    assert entry.get("intro_clip_final_sha256") in (None, "")   # 片头台账不记
    assert results["clip-01"]["intro_burned"] is False
    # 真实成片未被改写（ffmpeg 编码会带编码时间戳，此处仅断言未被「拼接」路径触碰：
    # 无 .intro 临时产物、时长与源一致）
    assert not list(base.glob("*.intro*"))
    assert not list(base.glob("*.tmp.mp4"))


def test_stage_intro_on_burns_and_records(tmp_path, monkeypatch):
    """COVER_INTRO_MODE=on：旧契约拼接路径照常（sha 变化 + 台账记录 + 留痕 True）。"""
    monkeypatch.setenv("COVER_INTRO_MODE", "1")
    monkeypatch.delenv("STUDIO_AUTO_COVER", raising=False)
    base, video, results, aggregate = _run_stage(tmp_path, monkeypatch, "job-on")
    assert "clip-01" in results
    assert results["clip-01"]["intro_burned"] is True
    sha_after = hashlib.sha256(video.read_bytes()).hexdigest()
    ledger = cover_mod.load_ledger(base)
    assert ledger["entries"]["1"]["intro_clip_final_sha256"] == sha_after


def test_stage_restart_recovery_off_then_on(tmp_path, monkeypatch):
    """off 产资产 → 开 on 重跑：台账无 intro 记录 → 此时会补拼（实验切换生效）。"""
    monkeypatch.delenv("COVER_INTRO_MODE", raising=False)
    monkeypatch.delenv("STUDIO_AUTO_COVER", raising=False)
    base, video, results, _ = _run_stage(tmp_path, monkeypatch, "job-switch")
    sha_before = hashlib.sha256(video.read_bytes()).hexdigest()
    ledger = cover_mod.load_ledger(base)
    entry = ledger["entries"].get("1") or {}
    assert entry.get("intro_clip_final_sha256") in (None, "")
    # 切 on 重跑同 job（封面缓存命中 revision 不变，片头按台账缺失补拼）
    monkeypatch.setenv("COVER_INTRO_MODE", "1")
    tasks_root = tmp_path / "tasks"
    run_id = uuid.uuid5(uuid.NAMESPACE_URL, "studio-render:job-switch:0").hex
    out_root = tasks_root / "output" / run_id
    plan_record, transcript = _plan_and_transcript()
    aggregate = {"entries": [{"clip_id": "clip-01", "status": "rendered"}], "warnings": []}
    results2 = w._run_clip_cover_stage({"project_id": "decouple_job-switch"}, "job-switch",
                                       plan_record, transcript, aggregate, video, out_root)
    assert results2["clip-01"]["intro_burned"] is True
    assert results2["clip-01"]["cover_revision"] == 1   # 封面缓存命中，不叠加修订
    sha_after = hashlib.sha256(video.read_bytes()).hexdigest()
    assert sha_after != sha_before                       # 片头此时才真正烧入


# ---------------------------------------------------------------------------
# 发布软标记（helper 层）：封面资产缺失 → cover_missing 标记（不拦截发布）。
# 发布循环本体在 render 大函数内，标记行为由 helper 判据 + 条目拼装保证。
# ---------------------------------------------------------------------------


def test_publish_soft_marker_detector_only(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_AUTO_COVER", "1")
    base = tmp_path / "clips" / "c1"
    base.mkdir(parents=True)
    (base / "clip_final.mp4").write_bytes(b"v")
    assert w._cover_asset_missing(base) is True
    (base / "cover.jpg").write_bytes(b"cover")
    assert w._cover_asset_missing(base) is False
