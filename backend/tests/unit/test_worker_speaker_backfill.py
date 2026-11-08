"""精修降级时的源级说话人标注兜底：触发阈值、后台执行与参数透传。

不调真实模型：speaker_labeling 的 LLM 入口整体打桩，只验证 worker 侧触发器。
"""
from __future__ import annotations

import threading

from src import speaker_labeling as sl
from src.studio_job_worker import _spawn_speaker_labeling_backfill


def _record(speaker: str, n: int = 2) -> dict:
    return {"clips": {"clip-1": {"segments": [
        {"start": 0.8, "end": 1.5, "text": f"t{i}", "speaker": speaker}
        for i in range(n)]}}}


def test_degraded_refinement_spawns_backfill(monkeypatch):
    calls: dict = {}
    saved: dict = {}
    monkeypatch.setattr(sl, "load_labels", lambda root, sid: {"labels": {}})
    monkeypatch.setattr(sl, "save_labels",
                        lambda root, sid, record: saved.setdefault("sid", sid))

    def fake_label(segments, **kw):
        calls.update(segments=segments, note=kw.get("note"),
                     resume_from=kw.get("resume_from"),
                     transcript_id=kw.get("transcript_id"))
        return {"labels": {}}

    monkeypatch.setattr(sl, "label_transcript", fake_label)
    job = {"job_id": "job-1", "object_key": "local:src.mp4"}
    transcript = {"asr_segments": [{"start": 0, "end": 1, "text": "x"}],
                  "transcript_id": "tr-1", "transcript_version": 2}
    thread = _spawn_speaker_labeling_backfill(job, transcript, _record("未标注"))
    assert isinstance(thread, threading.Thread)
    thread.join(timeout=5)
    assert saved["sid"]  # source_id_for("local:src.mp4") 非空
    assert calls["note"] == "worker:refine_degraded_backfill"
    assert calls["resume_from"] == {"labels": {}}
    assert calls["transcript_id"] == "tr-1"
    assert calls["segments"] == transcript["asr_segments"]


def test_empty_refinement_hard_failure_still_backfills(monkeypatch):
    """精修整体失败（clips 为空）也要兜底：overlay 是此时唯一的说话人来源。"""
    monkeypatch.setattr(sl, "load_labels", lambda root, sid: None)
    monkeypatch.setattr(sl, "save_labels", lambda root, sid, record: "/tmp/x.json")
    monkeypatch.setattr(sl, "label_transcript", lambda segments, **kw: {"labels": {}})
    thread = _spawn_speaker_labeling_backfill(
        {"job_id": "job-2", "object_key": "local:src.mp4"},
        {"asr_segments": [{"start": 0, "end": 1, "text": "x"}]},
        {"clips": {}})
    assert thread is not None
    thread.join(timeout=5)


def test_healthy_refinement_skips_backfill(monkeypatch):
    """精修自带足够说话人（≥一半）：不触发、不建线程、不花额度。"""
    def fail(*args, **kwargs):
        raise AssertionError("健康精修不应触发说话人标注兜底")

    monkeypatch.setattr(sl, "label_transcript", fail)
    monkeypatch.setattr(sl, "save_labels", fail)
    thread = _spawn_speaker_labeling_backfill(
        {"job_id": "job-3", "object_key": "local:src.mp4"},
        {"asr_segments": [{"start": 0, "end": 1, "text": "x"}]},
        _record("医生", 4))
    assert thread is None


def test_missing_object_key_or_segments_is_noop():
    assert _spawn_speaker_labeling_backfill(
        {"job_id": "j"}, {"asr_segments": [{"start": 0, "end": 1}]}, _record("未标注")) is None
    assert _spawn_speaker_labeling_backfill(
        {"job_id": "j", "object_key": "local:src.mp4"}, {}, _record("未标注")) is None
