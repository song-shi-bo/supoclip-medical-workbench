"""TRANSCRIBE_ONLY / AUTO_EDIT 任务拆分：worker 分派契约与禁止项验收。

不跑真实 ffmpeg/whisper/HTTP：阶段函数与状态上报全部打桩，
guardian 桩在被禁止的调用发生时直接使测试失败。
object_key 一律用 tmp_path 下真实存在的文件（store 的本地存在性校验是真契约）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import studio_job_worker as worker
from src import studio_transcript_store as store


class StateRecorder:
    def __init__(self):
        self.states: list[tuple[str, str, float]] = []

    def __call__(self, job_id: str, status: str, stage: str, progress: float, error=None):
        self.states.append((status, stage, progress))


@pytest.fixture()
def tasks_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "tasks"
    root.mkdir()
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(root))
    return root


@pytest.fixture()
def states(monkeypatch: pytest.MonkeyPatch) -> StateRecorder:
    recorder = StateRecorder()
    monkeypatch.setattr(worker, "_state", recorder)
    return recorder


@pytest.fixture()
def source_file(tmp_path: Path) -> Path:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake video bytes")
    return source


def _guard(name: str):
    def _fail(*args, **kwargs):
        raise AssertionError(f"禁止调用 {name}（拆分契约被破坏）")
    return _fail


def test_job_kind_dispatch():
    assert worker._job_kind({"config": {}}) == "legacy_full"
    assert worker._job_kind({"config": None}) == "legacy_full"
    assert worker._job_kind({"config": {"job_kind": "transcribe_only"}}) == "transcribe_only"
    assert worker._job_kind({"config": {"job_kind": "auto_edit"}}) == "auto_edit"
    assert worker._job_kind({"config": {"job_kind": "bogus"}}) == "legacy_full"


def test_transcribe_only_transcribes_corrects_and_never_reaches_editing(
        tasks_root, states, source_file, monkeypatch):
    uploads: list[dict] = []
    monkeypatch.setattr(worker, "_materialise_source_for_job", lambda key, jid: source_file)
    monkeypatch.setattr(worker, "_assert_audio_stream", lambda path: None)

    def fake_transcribing(path, config):
        worker._state("job-t1", "transcribing", "语音转写", 0.15)
        return [{"start": 0.0, "end": 1.0, "text": "原始文本"}], {"language": "zh", "words": [{"w": 1}]}

    def fake_correcting(raw, jid):
        worker._state(jid, "correcting", "医学同音纠错", 0.35)
        return {
            "asr_segments": [{"start": 0.0, "end": 1.0, "text": "纠正后文本"}],
            "correction_audit": [{"segment_id": "seg_0000", "start": 0.0, "end": 1.0, "raw_text": "原始文本"}],
            "correction_summary": {"engine": "local-conservative-rules"},
        }

    monkeypatch.setattr(worker, "stage_transcribing", fake_transcribing)
    monkeypatch.setattr(worker, "stage_correcting", fake_correcting)
    # 禁止项：金句/时间线/渲染任何一步被触碰即失败
    monkeypatch.setattr(worker, "stage_analyzing", _guard("stage_analyzing"))
    monkeypatch.setattr(worker, "stage_editing", _guard("stage_editing"))
    monkeypatch.setattr(worker, "stage_rendering", _guard("stage_rendering"))

    def fake_upload(job_id, video_path, payload):
        uploads.append(payload)
    monkeypatch.setattr(worker, "_upload_transcript_artifact", fake_upload)

    job = {"job_id": "job-t1", "object_key": f"local:{source_file}",
           "project_id": "timeline_e2e", "config": {"job_kind": "transcribe_only"}}
    worker.process_job(job)

    statuses = [s[0] for s in states.states]
    assert statuses == ["extracting_audio", "transcribing", "correcting", "uploading", "transcript_ready"]
    ref = store.load_corrected_transcript(f"local:{source_file}")
    assert ref is not None and ref["transcript_version"] == 1
    assert ref["asr_segments"][0]["raw_text"] == "原始文本"
    assert ref["source_project_id"] == "timeline_e2e"
    payload = uploads[-1]
    assert payload["auto_edit_task"] is None and payload["timeline"] is None
    assert payload["transcript_id"] == ref["transcript_id"]


def test_auto_edit_without_transcript_fails_explicitly(tasks_root, states, source_file, monkeypatch):
    monkeypatch.setattr(worker, "_materialise_source_for_job", lambda key, jid: source_file)
    monkeypatch.setattr(worker, "stage_rendering", _guard("stage_rendering"))
    job = {"job_id": "job-a0", "object_key": f"local:{source_file}",
           "project_id": "timeline_e2e", "config": {"job_kind": "auto_edit"}}
    with pytest.raises(RuntimeError, match="E_TRANSCRIPT_REQUIRED"):
        worker.process_job(job)


def test_auto_edit_reuses_transcript_and_never_retranscribes(
        tasks_root, states, source_file, monkeypatch):
    key = f"local:{source_file}"
    store.save_corrected_transcript(
        key, "timeline_e2e",
        asr_segments=[{"start": 0.0, "end": 1.0, "text": "纠正后文本", "raw_text": "原始文本"}],
        raw_segments=[{"start": 0.0, "end": 1.0, "text": "原始文本"}],
        correction_audit=[{"segment_id": "seg_0000", "raw_text": "原始文本"}],
        correction_summary={"engine": "local-conservative-rules"},
        correction_provider="local-conservative-rules", language="zh", words=[])
    monkeypatch.setattr(worker, "_materialise_source_for_job", lambda k, j: source_file)
    # 禁止项：转写/纠错任何一次被调用即失败（多次自动剪辑也不得重转写）
    monkeypatch.setattr(worker, "stage_transcribing", _guard("stage_transcribing"))
    monkeypatch.setattr(worker, "stage_correcting", _guard("stage_correcting"))

    from src import content_analysis
    from src import clip_planning
    from src import clip_render

    analyze_calls: list[dict] = []

    def fake_analyze(segments, **kwargs):
        analyze_calls.append({"segments": segments, "version": kwargs.get("transcript_version")})
        return {"plan_source": "heuristic_fallback", "keywords": ["关键词"], "golden_quotes": [],
                "topic_units": [], "candidate_intervals": []}

    plan_calls: list[dict] = []

    def fake_plan(segments, analysis, **kwargs):
        plan_calls.append({"segments": segments, "version": kwargs.get("transcript_version")})
        return {"plan_source": "heuristic_fallback", "llm_error": None,
                "clips": [{"clip_id": "clip-01", "topic": "主题", "start_segment_id": "seg_0000",
                           "end_segment_id": "seg_0000", "start_ms": 0, "end_ms": 1000,
                           "duration_ms": 1000, "opening_context": "", "core_points": [],
                           "closing_thought": "", "evidence_segment_ids": [], "cut_in_reason": "",
                           "cut_out_reason": "", "completeness": "pass", "warnings": []}],
                "discarded": [], "notes": []}

    render_calls: list[dict] = []

    def fake_render(plan, **kwargs):
        render_calls.append({"plan": plan})
        return {"job_status": "completed", "entries": [],
                "counts": {"total": 1, "succeeded": 1, "technical_error": 0, "skipped_playable": 0},
                "warnings": []}

    monkeypatch.setattr(content_analysis, "analyze_content", fake_analyze)
    monkeypatch.setattr(clip_planning, "plan_clips", fake_plan)
    monkeypatch.setattr(clip_render, "render_plan", fake_render)
    monkeypatch.setattr(worker, "_http", lambda *a, **k: {})
    monkeypatch.setattr("src.r2.upload_file", lambda path, key: {"object_key": key, "size": 1})

    job = {"job_id": "11111111111111111111111111111111", "object_key": key, "project_id": "timeline_e2e",
           "config": {"job_kind": "auto_edit", "transcript_id": store.transcript_id_for(key),
                      "transcript_version": 1}}
    worker.process_job(job)

    # 分析与规划读到的都是已保存的纠错转写（版本 1、纠正后文本）
    assert analyze_calls[0]["version"] == 1
    assert analyze_calls[0]["segments"][0]["text"] == "纠正后文本"
    assert plan_calls[0]["version"] == 1
    assert render_calls and render_calls[0]["plan"]["clips"][0]["clip_id"] == "clip-01"
    statuses = [s[0] for s in states.states]
    assert statuses[0] == "retrieving_rules"
    assert "planning" in statuses and "rendering" in statuses and statuses[-1] == "completed"
    assert "transcribing" not in statuses and "correcting" not in statuses


def test_auto_edit_second_run_still_does_not_retranscribe(tasks_root, states, source_file, monkeypatch):
    """同一视频重复点击自动剪辑：两次都不触碰转写/纠错，分析模型也只跑一次。"""
    key = f"local:{source_file}"
    store.save_corrected_transcript(
        key, "timeline_e2e",
        asr_segments=[{"start": 0.0, "end": 1.0, "text": "文本"}],
        raw_segments=[], correction_audit=[], correction_summary={},
        correction_provider="x", language="zh", words=[])
    monkeypatch.setattr(worker, "_materialise_source_for_job", lambda k, j: source_file)
    monkeypatch.setattr(worker, "stage_transcribing", _guard("stage_transcribing"))
    monkeypatch.setattr(worker, "stage_correcting", _guard("stage_correcting"))

    from src import content_analysis
    from src import clip_planning
    from src import clip_render

    analyze_calls: list[int] = []

    def fake_analyze(segments, **kwargs):
        analyze_calls.append(1)
        return {"plan_source": "heuristic_fallback", "keywords": [], "golden_quotes": [],
                "topic_units": [], "candidate_intervals": []}

    plan_calls: list[int] = []

    def fake_plan(segments, analysis, **kwargs):
        plan_calls.append(1)
        return {"plan_source": "heuristic_fallback", "clips": [], "discarded": [], "notes": []}

    render_calls: list[int] = []

    def fake_render(plan, **kwargs):
        render_calls.append(1)
        return {"job_status": "completed", "entries": [],
                "counts": {"total": 1, "succeeded": 1, "technical_error": 0, "skipped_playable": 0},
                "warnings": []}

    monkeypatch.setattr(content_analysis, "analyze_content", fake_analyze)
    monkeypatch.setattr(clip_planning, "plan_clips", fake_plan)
    monkeypatch.setattr(clip_render, "render_plan", fake_render)
    monkeypatch.setattr(worker, "_http", lambda *a, **k: {})
    monkeypatch.setattr("src.r2.upload_file", lambda path, key: {"object_key": key, "size": 1})

    job = {"job_id": "22222222222222222222222222222222", "object_key": key, "project_id": "timeline_e2e",
           "config": {"job_kind": "auto_edit", "transcript_id": store.transcript_id_for(key),
                      "transcript_version": 1}}
    worker.process_job(job)
    worker.process_job(job)  # 第二次点击
    assert [s[0] for s in states.states].count("completed") == 2
    assert len(analyze_calls) == 1   # 第二次复用已保存分析工件，不重新调用模型
    assert len(plan_calls) == 1      # 第二次复用已保存 clip_plan（需求 7：不重新规划）
    assert len(render_calls) == 2    # 渲染每次都执行（幂等跳过由 clip_render 内部保证）
