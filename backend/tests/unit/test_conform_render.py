"""conform_render（对稿成片 P1b：单成片渲染）单元测试 + ffmpeg 集成冒烟。

* 纯单元：伪 clip 构建 / 烧录段过滤 / 拼接点计算 / 音频滤镜链 / 审计修正；
* 集成冒烟（真实 ffmpeg）：合成 10s 源片 + EDL 两段保留 → 渲染 1 条成片，
  ffprobe 校验输出时长 ≈ keep 总和（容差 ±0.4s），审计修正落盘。
"""

import json
import shutil
from pathlib import Path

import pytest

from src import conform_render as cr
from src.domains.render import clip_render

HAS_FFMPEG = shutil.which("ffmpeg") is not None


# ---------------------------------------------------------------------------
# 纯单元
# ---------------------------------------------------------------------------

EDL = {
    "version": "conform-edl/v1",
    "source_span_ms": {"start": 0, "end": 10000},
    "keep_ranges_ms": [[0, 2000], [5000, 8000]],
    "output_duration_ms": 5000,
    "cut_ratio": 0.5,
}


def test_build_pseudo_clip_maps_sub_ranges():
    clip = cr.build_pseudo_clip(EDL)
    assert clip["clip_id"] == "conform"
    assert clip["start_ms"] == 0 and clip["end_ms"] == 10000
    assert clip["sub_ranges"] == [{"start_ms": 0, "end_ms": 2000},
                                  {"start_ms": 5000, "end_ms": 8000}]


def test_build_pseudo_clip_full_span_walks_single_range_path():
    edl = {"source_span_ms": {"start": 0, "end": 5000},
           "keep_ranges_ms": [[0, 5000]]}
    clip = cr.build_pseudo_clip(edl)
    assert "sub_ranges" not in clip  # 等价整段 → 单区间老路径


def test_build_burn_segments_only_matched():
    record = {"sentences": [
        {"index": 0, "status": "matched", "start_ms": 0, "end_ms": 1500, "text": "句子一"},
        {"index": 1, "status": "missing", "start_ms": None, "end_ms": None, "text": "句子二"},
        {"index": 2, "status": "matched", "start_ms": 3200, "end_ms": 4500, "text": "句子三"},
    ]}
    burn = cr.build_burn_segments(record)
    assert [b["segment_id"] for b in burn] == ["align-0", "align-2"]
    assert burn[0]["corrected_text"] == "句子一"


def test_internal_boundaries_and_audio_chain():
    assert cr.internal_boundaries([[0, 2000], [5000, 8000]]) == [2000]  # 虚拟轴唯一内部拼接点
    chain = cr.build_audio_filter_chain([2000, 7000], fade_ms=10, loudnorm=True)
    assert chain.count("afade=") == 4  # 每个拼接点 out+in
    assert "loudnorm=I=-16" in chain and "aresample=48000" in chain
    assert cr.build_audio_filter_chain([], fade_ms=0, loudnorm=False) is None


def test_apply_audio_post_noop_without_filters(tmp_path):
    src = tmp_path / "clip_final.mp4"
    src.write_bytes(b"x")
    assert cr.apply_audio_post(src, [], fade_ms=0, loudnorm=False) is False


def test_fix_subtitle_provenance(tmp_path):
    p = tmp_path / "warnings.json"
    p.write_text(json.dumps({"subtitle_provenance": {"subtitle_source": "corrected_transcript"}}),
                 encoding="utf-8")
    cr.fix_subtitle_provenance(p)
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["subtitle_provenance"]["subtitle_source"] == "script_align"
    assert doc["subtitle_provenance"]["align_version"] == "script-align/v1"


# ---------------------------------------------------------------------------
# 集成冒烟（真实 ffmpeg；合成源片，无需真实素材）
# ---------------------------------------------------------------------------

def _make_source(path: Path, seconds: int = 10) -> None:
    clip_render._run_ffmpeg([
        "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=608x1080:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ])


def _ffprobe_duration(path: Path) -> float:
    clip_render._run_ffmpeg.__wrapped__ if False else None
    import json
    import subprocess
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", str(path)],
        capture_output=True, text=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg 不可用")
def test_render_conform_final_end_to_end(monkeypatch, tmp_path):
    edl_path = tmp_path / "conform_edl.json"
    edl_path.write_text(json.dumps(EDL), encoding="utf-8")
    source = tmp_path / "source.mp4"
    _make_source(source)

    from src.studio_job_worker import pipeline as worker_pipeline
    monkeypatch.setattr(worker_pipeline, "_materialise_source_for_job",
                        lambda object_key, job_id: str(source))
    monkeypatch.setattr(clip_render, "_subtitle_readability", clip_render._subtitle_readability)

    match_record = {
        "conform_edl_path": str(edl_path),
        "is_script_shot": True,
        "sentences": [
            {"index": 0, "status": "matched", "start_ms": 200, "end_ms": 1800, "text": "前段台词。"},
            {"index": 1, "status": "matched", "start_ms": 5200, "end_ms": 7800, "text": "后段台词。"},
        ],
        "warnings": [],
    }
    job_id = "job-conform-render"
    job = {"job_id": job_id, "project_id": "p1",
           "object_key": "local:/tmp/source.mp4", "config": {"transcript_id": "t1",
                                                            "transcript_version": 1}}
    transcript = {"asr_segments": [], "words": [
        {"text": "前段台词。", "start": 0.2, "end": 1.8},
        {"text": "后段台词。", "start": 5.2, "end": 7.8},
    ]}

    info = cr.render_conform_final(job, job_id, transcript, match_record)
    final = Path(info["video_path"])
    assert final.is_file()
    assert info["entry_status"] in ("rendered", "skipped_playable")
    assert info["keep_ranges_count"] == 2 and info["audio_post_applied"] is True
    duration = _ffprobe_duration(final)
    assert abs(duration - 5.0) <= 0.4, f"成片时长应≈5s，实测 {duration}"
    warnings_doc = json.loads((final.parent / "warnings.json").read_text(encoding="utf-8"))
    assert warnings_doc["subtitle_provenance"]["subtitle_source"] == "script_align"


# ---------------------------------------------------------------------------
# P1c punch-in
# ---------------------------------------------------------------------------

def test_punchin_env_defaults(monkeypatch):
    monkeypatch.delenv("CONFORM_PUNCHIN", raising=False)
    assert cr.punchin_enabled() is True          # 默认 on
    assert cr.punchin_zoom() == 1.3
    monkeypatch.setenv("CONFORM_PUNCHIN", "off")
    assert cr.punchin_enabled() is False


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg 不可用")
def test_apply_punchin_source_alternates_and_probes(tmp_path):
    source = tmp_path / "source.mp4"
    _make_source(source, seconds=6)
    w, h, dur = cr._probe_dims_and_duration(source, None)
    assert abs(dur - 6.0) <= 0.4

    punchin, actual, info = cr.apply_punchin_source(
        source, [[0, 2000], [3000, 6000]], zoom=1.3,
        work_dir=tmp_path / "work", run_ffmpeg=clip_render._run_ffmpeg)
    assert punchin.is_file()
    assert len(actual) == 2
    total = sum(e - s for s, e in actual)
    pw, ph, pdur = cr._probe_dims_and_duration(punchin, None)
    assert (pw, ph) == (w, h)                    # 变焦后放大回源分辨率
    assert abs(pdur * 1000 - total) <= 200       # concat 时长 = 各段实测和
    assert abs(total - 5000) <= 400              # 请求 keep 和 ≈ 5000ms


def test_punchin_skipped_for_single_range(monkeypatch, tmp_path):
    edl_path = tmp_path / "conform_edl.json"
    edl_path.write_text(json.dumps({
        "source_span_ms": {"start": 0, "end": 5000},
        "keep_ranges_ms": [[0, 5000]], "output_duration_ms": 5000, "cut_ratio": 0.0,
    }), encoding="utf-8")
    source = tmp_path / "source.mp4"
    _make_source(source, seconds=5)

    from src.studio_job_worker import pipeline as worker_pipeline
    monkeypatch.setattr(worker_pipeline, "_materialise_source_for_job",
                        lambda object_key, job_id: str(source))
    monkeypatch.delenv("CONFORM_PUNCHIN", raising=False)
    match_record = {"conform_edl_path": str(edl_path), "is_script_shot": True,
                    "sentences": [], "warnings": []}
    job = {"job_id": "job-single", "project_id": "p1", "object_key": "local:/s.mp4",
           "config": {}}
    transcript = {"asr_segments": [], "words": []}
    info = cr.render_conform_final(job, "job-single", transcript, match_record)
    assert info["punchin"] is None               # 单区间不启用 punch-in
    assert abs(_ffprobe_duration(Path(info["video_path"])) - 5.0) <= 0.4
