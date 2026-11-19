"""阶段2 P4 人脸/主体重构图 MVP 单元测试（2026-11-18）。

覆盖：
* 纯决策函数：纵横比决策（横宽 pan_x / 竖高 pan_y / 9:16 直通 / 零尺寸）；
  手势并入（近手并入、远手忽略）；上半身静态 y 定位与钳制；
  多人保守判据（持续 ≥2s 触发、短暂同框不触发）；检出率；
* spec 生成（monkeypatch 注入采样）：动态跟踪 / 多人保守回退 / 稀疏回退 /
  竖高静态定位 + 字幕避让提示 / 直通不触发分析；
* crop 滤镜串：动态 keys（SmoothDamp 表达式）、静态居中偶数像素、pan_y；
* 指纹：off（默认）与历史逐字节一致、auto 触发新指纹；
* 渲染接线端到端（真实 ffmpeg，横宽 testsrc2 源无人脸 → 静态居中裁切，
  成片变竖屏，reframe.json/warnings.reframe 落盘）。
"""

from __future__ import annotations

import contextlib
import json
import subprocess as _sp
from pathlib import Path

import pytest

from src import clip_render as cr
from src import reframe as rf


# ---------------------------------------------------------------------------
# 纯决策函数
# ---------------------------------------------------------------------------


def test_decide_axes_landscape_pan_x():
    axes, w, h = rf.decide_axes(1920, 1080)
    assert axes == "pan_x" and (w, h) == (608, 1080)   # 与工作台文档 608×1080 口径一致
    assert w % 2 == 0 and h % 2 == 0


def test_decide_axes_vertical_pan_y_and_passthrough():
    assert rf.decide_axes(720, 1600)[0] == "pan_y"     # 竖高源
    assert rf.decide_axes(1080, 1920)[:1] == ("passthrough",)
    assert rf.decide_axes(720, 1280)[:1] == ("passthrough",)   # 恰好 9:16
    assert rf.decide_axes(0, 0)[:1] == ("passthrough",)        # 探测失败


def test_merge_wrist_center_near_in_far_out():
    # 近手（0.55/0.6 距脸 0.05）→ 并入，目标中心偏移
    assert rf.merge_wrist_center(0.5, (0.55, 0.60), 0.5625) == pytest.approx(0.55)
    # 远手（0.95 距脸 0.45 > 1.2×半宽）→ 忽略，保持脸中心
    assert rf.merge_wrist_center(0.5, (0.95, 0.97), 0.5625) == pytest.approx(0.5)
    assert rf.merge_wrist_center(0.5, None, 0.5625) == pytest.approx(0.5)


def test_static_y_for_face_and_clamp():
    # 脸中心 0.3、脸高 0.1 → face_top=0.25，留 10%×crop_h 头顶 → y=0.17
    assert rf.static_y_for_face(0.3, 0.1, 0.8) == pytest.approx(0.17)
    y = rf.static_y_for_face(0.05, 0.05, 0.8)          # 脸贴顶 → 原始值为负，
    assert y < 0                                        # 由调用方钳到 [0, 1-crop_h]
    assert max(0.0, min(y, 0.2)) == 0.0


def _samples(specs: list[tuple]) -> list[rf.Sample]:
    out = []
    for (t, cx, cy, fh, second) in specs:
        s = rf.Sample(t=t)
        if cx is not None:
            s.cx, s.cy, s.fh, s.area, s.second_ratio = cx, cy, fh, fh * 0.7, second
        out.append(s)
    return out


def test_multi_person_requires_sustained_second_face():
    sustained = _samples([(t, 0.4, 0.3, 0.1, 0.7) for t in
                          [i * 0.33 for i in range(8)]])     # ~2.6s 全程第二脸
    assert rf.is_multi_person(sustained) is True
    brief = _samples([(t, 0.4, 0.3, 0.1, 0.7) for t in [0.0, 0.33, 0.66]] +
                     [(t, 0.4, 0.3, 0.1, 0.0) for t in [1.0, 1.33, 2.0, 2.33]])
    assert rf.is_multi_person(brief) is False                # 短暂同框不触发


def test_detected_ratio():
    samples = _samples([(0.0, 0.5, 0.3, 0.1, 0.0), (0.33, None, 0, 0, 0),
                        (0.66, 0.5, 0.3, 0.1, 0.0), (1.0, None, 0, 0, 0)])
    assert rf.detected_ratio(samples) == pytest.approx(0.5)
    assert rf.detected_ratio([]) == 0.0


# ---------------------------------------------------------------------------
# spec 生成（monkeypatch 注入采样，不依赖真实人脸）
# ---------------------------------------------------------------------------


def _patch_probe(monkeypatch, w: int, h: int):
    monkeypatch.setattr(rf, "probe_dims", lambda source, env=None: (w, h))


def test_spec_pan_x_dynamic_tracking(tmp_path, monkeypatch):
    _patch_probe(monkeypatch, 1920, 1080)
    samples = _samples([(i * 0.33, 0.30 + i * 0.05, 0.35, 0.12, 0.0)
                        for i in range(8)])
    monkeypatch.setattr(rf, "analyze_track", lambda *a, **k: (samples, []))
    spec = rf.build_reframe_spec(tmp_path / "src.mp4", 0, 2_600)
    assert spec["mode"] == "pan_x"
    assert spec["crop_w"] == 608 and spec["crop_h"] == 1080
    assert spec["x_keys"], "动态跟踪应产出 keyframes"
    xs = [x for _, x in spec["x_keys"]]
    assert all(0 <= x <= 1920 - 608 for x in xs)          # 轨迹在可行域内
    filt = rf.crop_filter(spec)
    assert filt.startswith("crop=608:1080:'") and "trunc(" in filt


def test_spec_multi_person_falls_back_static(tmp_path, monkeypatch):
    _patch_probe(monkeypatch, 1920, 1080)
    samples = _samples([(i * 0.33, 0.4, 0.3, 0.1, 0.7) for i in range(8)])
    monkeypatch.setattr(rf, "analyze_track", lambda *a, **k: (samples, []))
    spec = rf.build_reframe_spec(tmp_path / "src.mp4", 0, 2_600)
    assert spec["mode"] == "static"
    assert "multi_person" in spec["reason"]
    assert spec["stats"]["multi_person"] is True
    # 定帧主脸开头位置：cx 0.4 → crop 左沿 = 0.4 - 608/1920/2
    assert spec["static_x_frac"] == pytest.approx(0.4 - 608 / 1920 / 2, abs=0.01)


def test_spec_sparse_falls_back_static(tmp_path, monkeypatch):
    _patch_probe(monkeypatch, 1920, 1080)
    samples = _samples([(0.0, 0.5, 0.3, 0.1, 0.0)] +
                       [(i * 0.33, None, 0, 0, 0) for i in range(1, 10)])
    monkeypatch.setattr(rf, "analyze_track", lambda *a, **k: (samples, []))
    spec = rf.build_reframe_spec(tmp_path / "src.mp4", 0, 3_000)
    assert spec["mode"] == "static"
    assert "sparse_track" in spec["reason"]
    # 有检出 → 定帧首检出位置（优于盲目居中）；零检出才是居中（None）
    assert spec["static_x_frac"] == pytest.approx(0.5 - 608 / 1920 / 2, abs=0.01)


def test_spec_pan_y_static_with_subtitle_hint(tmp_path, monkeypatch):
    _patch_probe(monkeypatch, 720, 1600)
    # 脸在画面下部（cy 0.8）→ crop 后脸下沿应落入字幕带 → 触发避让提示
    samples = _samples([(i * 0.33, 0.5, 0.80, 0.10, 0.0) for i in range(8)])
    monkeypatch.setattr(rf, "analyze_track", lambda *a, **k: (samples, []))
    spec = rf.build_reframe_spec(tmp_path / "src.mp4", 0, 2_600)
    assert spec["mode"] == "pan_y"
    upper = 1.0 - spec["crop_h"] / 1600.0
    assert 0.0 <= spec["static_y_frac"] <= upper + 1e-9
    assert spec["static_y_frac"] == pytest.approx(0.2, abs=1e-3)
    assert any("字幕避让" in w for w in spec["warnings"])
    filt = rf.crop_filter(spec)
    assert filt.startswith("crop=720:")


def test_spec_passthrough_skips_analysis(tmp_path, monkeypatch):
    _patch_probe(monkeypatch, 1080, 1920)
    called = {"n": 0}

    def _forbidden(*a, **k):
        called["n"] += 1
        return [], []

    monkeypatch.setattr(rf, "analyze_track", _forbidden)
    spec = rf.build_reframe_spec(tmp_path / "src.mp4", 0, 2_000)
    assert spec["mode"] == "passthrough" and called["n"] == 0   # 竖屏零开销


def test_spec_detector_missing_falls_back(tmp_path, monkeypatch):
    _patch_probe(monkeypatch, 1920, 1080)
    monkeypatch.setattr(rf, "analyze_track",
                        lambda *a, **k: ([], ["重构图依赖缺失，回退静态裁切"]))
    spec = rf.build_reframe_spec(tmp_path / "src.mp4", 0, 2_000)
    assert spec["mode"] == "static" and spec["static_x_frac"] is None  # 居中


# ---------------------------------------------------------------------------
# 指纹：默认 off 不影响历史缓存；auto 触发新指纹
# ---------------------------------------------------------------------------


def test_fingerprint_off_keeps_baseline(tmp_path, monkeypatch):
    plan = {"clip_id": "c", "start_ms": 0, "end_ms": 1000}
    words = [{"text": "词", "start": 0.0, "end": 0.5}]
    monkeypatch.delenv("REFRAME_MODE", raising=False)
    fp_default = cr._render_fingerprint(plan, tmp_path / "s.mp4", [], {}, None, None, words)
    monkeypatch.setenv("REFRAME_MODE", "off")
    fp_off = cr._render_fingerprint(plan, tmp_path / "s.mp4", [], {}, None, None, words)
    assert fp_default == fp_off                          # off（默认）不影响存量缓存
    monkeypatch.setenv("REFRAME_MODE", "auto")
    fp_auto = cr._render_fingerprint(plan, tmp_path / "s.mp4", [], {}, None, None, words)
    assert fp_auto != fp_default                         # auto → 自动失效重渲


# ---------------------------------------------------------------------------
# 渲染接线端到端（真实 ffmpeg：横宽 testsrc2 源，无人脸 → 静态居中裁切成竖屏）
# ---------------------------------------------------------------------------


def test_render_reframe_auto_landscape_becomes_vertical(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "reserve_capacity",
                        lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setenv("REFRAME_MODE", "auto")
    try:
        source = tmp_path / "src.mp4"
        _sp.run([cr._resolve(cr.FFMPEG_BIN), "-y",
                 "-f", "lavfi", "-i", "testsrc2=duration=2:size=320x240:rate=10",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                 "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                 "-shortest", str(source)], check=True, capture_output=True)
        clip = {"clip_id": "reframe-clip", "start_ms": 0, "end_ms": 1500, "warnings": []}
        segments = [{"segment_id": "s1", "start_ms": 100, "end_ms": 1200,
                     "text": "横转竖裁切测试字幕。"}]
        entry = cr.render_one(clip, source=source, out_dir=tmp_path / "out",
                              corrected_segments=segments,
                              transcript_ref={"transcript_id": "t", "transcript_version": "v1"},
                              rule_snapshot_id=None)
        assert entry["status"] in {"rendered", "skipped_playable"}
        final = tmp_path / "out" / "clip_final.mp4"
        assert final.is_file()
        # 成片必须是竖屏（横宽源被裁成 9:16 再放大）
        probe = _sp.run([cr._resolve(cr.FFPROBE_BIN), "-v", "error",
                         "-select_streams", "v:0", "-show_entries",
                         "stream=width,height", "-of", "json", str(final)],
                        capture_output=True, text=True)
        dims = json.loads(probe.stdout)["streams"][0]
        assert dims["height"] > dims["width"]
        assert dims["width"] % 2 == 0 and dims["height"] % 2 == 0
        # 审计落盘：reframe.json + warnings.reframe 摘要
        reframe_doc = json.loads((tmp_path / "out" / "reframe.json").read_text(encoding="utf-8"))
        assert reframe_doc["mode"] in {"static", "pan_x"}   # testsrc2 无人脸 → static
        warnings_doc = json.loads(
            (tmp_path / "out" / "warnings.json").read_text(encoding="utf-8"))
        assert warnings_doc["reframe"]["mode"] == reframe_doc["mode"]
        assert any(w.startswith("重构图:") for w in warnings_doc["render_warnings"])
    finally:
        monkeypatch.delenv("REFRAME_MODE", raising=False)


def test_render_reframe_off_no_reframe_artifacts(tmp_path, monkeypatch):
    """默认 off：零裁切零审计文件（行为与历史一致）。"""
    monkeypatch.setattr(cr, "reserve_capacity",
                        lambda *a, **k: contextlib.nullcontext())
    monkeypatch.delenv("REFRAME_MODE", raising=False)
    source = tmp_path / "src.mp4"
    _sp.run([cr._resolve(cr.FFMPEG_BIN), "-y",
             "-f", "lavfi", "-i", "testsrc2=duration=1:size=320x240:rate=10",
             "-c:v", "libx264", "-preset", "ultrafast", str(source)],
            check=True, capture_output=True)
    clip = {"clip_id": "off-clip", "start_ms": 0, "end_ms": 800, "warnings": []}
    entry = cr.render_one(clip, source=source, out_dir=tmp_path / "out",
                          corrected_segments=[],
                          transcript_ref={"transcript_id": "t", "transcript_version": "v1"},
                          rule_snapshot_id=None)
    assert entry["status"] in {"rendered", "skipped_playable"}
    assert not (tmp_path / "out" / "reframe.json").exists()
    warnings_doc = json.loads(
        (tmp_path / "out" / "warnings.json").read_text(encoding="utf-8"))
    assert "reframe" not in warnings_doc
