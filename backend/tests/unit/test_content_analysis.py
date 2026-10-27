"""AUTO_EDIT 内容分析（content_analysis，第三步）契约测试。

不调真实模型：``_model_ready`` / ``_run_analysis_agent`` 打桩；worker 层把
editing/rendering/源片物化设为守卫桩，被调用即失败（证明本阶段不渲染）。
"""

from __future__ import annotations

import json

import pytest

from src import content_analysis as ca
from src import clip_planning as cp
from src import clip_render as cr
from src import llm_budget
from src import studio_job_worker as worker


@pytest.fixture(autouse=True)
def _isolated_llm_ledger(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """分析成功路径会写 llm_budget 台账；测试内重定向到临时目录。"""
    monkeypatch.setattr(llm_budget, "_ledger_root", lambda: tmp_path / "llm_usage")


def seg(i: int, text: str, s_ms: int, e_ms: int, legacy: bool = False) -> dict:
    if legacy:  # 步骤二之前的存量形状：只有秒级 start/end
        return {"start": s_ms / 1000.0, "end": e_ms / 1000.0, "text": text}
    return {"segment_id": f"seg_{i:04d}", "start_ms": s_ms, "end_ms": e_ms, "text": text}


def _segments() -> list[dict]:
    return [
        seg(0, "大家好，今天讲高血压的日常管理。", 0, 5200),
        seg(1, "第一，按时服药不能随便停药。", 5400, 11000),
        seg(2, "第二，定期测量血压并做好记录。", 13000, 19000),
        seg(3, "记住，血压平稳才能减少并发症。", 19200, 25000),
    ]


def _agent_result(quote_text: str | None = None, ids: list[str] | None = None):
    return ca._AnalysisOut(
        keywords=["高血压", "日常管理"],
        golden_quotes=[ca._GoldenQuoteOut(
            text=quote_text or "按时服药不能随便停药。",
            segment_ids=ids if ids is not None else ["seg_0001"],
            reason="完整的忠告句，信息完整。")],
        topic_units=[ca._TopicUnitOut(
            title="高血压日常管理要点", segment_ids=["seg_0000", "seg_0001", "seg_0002", "seg_0003"],
            reason="同一主题的完整展开。")],
        candidate_intervals=[ca._CandidateIntervalOut(
            start_ms=0, end_ms=999999, topic_title="高血压日常管理要点", reason="完整主题段。")],
    )


def test_llm_single_call_grounded_quotes_and_frozen_timestamps(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    def fake_agent(model, prompt):
        calls.append(prompt)
        return _agent_result()

    monkeypatch.setattr(ca, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(ca, "_run_analysis_agent", fake_agent)
    record = ca.analyze_content(_segments(), transcript_version=3, job_id="job-x", project_id="timeline_t")

    assert len(calls) == 1  # 需求 9：优先一次结构化调用
    assert record["llm_requested"] is True and record["llm_enabled"] is True
    assert record["plan_source"] == "llm" and record["llm_error"] is None
    assert record["transcript_version"] == 3
    assert record["model"] == "openai:test-model" and record["llm_calls"] == 1
    quote = record["golden_quotes"][0]
    assert quote["text"] == "按时服药不能随便停药。"          # 逐字原话
    assert quote["segment_ids"] == ["seg_0001"]
    assert (quote["start_ms"], quote["end_ms"]) == (5400, 11000)  # 由分段重算
    # 模型给的越界区间被钳回转写边界（时间戳来自数据，不来自模型）
    assert record["candidate_intervals"][0]["end_ms"] == 25000
    unit = record["topic_units"][0]
    assert unit["title"] == "高血压日常管理要点"              # 模型归纳标题，非词表
    assert unit["start_ms"] == 0 and unit["end_ms"] == 25000


def test_non_verbatim_quote_is_dropped_with_note(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ca, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(ca, "_run_analysis_agent",
                        lambda model, prompt: _agent_result(quote_text="坚持规范用药才能健康长寿。"))
    record = ca.analyze_content(_segments(), transcript_version=1)
    assert record["golden_quotes"] == []
    assert any("非逐字原话" in note for note in record["notes"])


def test_quote_without_ids_selfheals_from_verbatim_text(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ca, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(ca, "_run_analysis_agent",
                        lambda model, prompt: _agent_result(ids=[]))
    record = ca.analyze_content(_segments(), transcript_version=1)
    quote = record["golden_quotes"][0]
    assert quote["segment_ids"] == ["seg_0001"]              # 原文反查自愈
    assert (quote["start_ms"], quote["end_ms"]) == (5400, 11000)


def test_legacy_seconds_segments_get_millisecond_fields(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ca, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(ca, "_run_analysis_agent", lambda model, prompt: _agent_result())
    legacy = [seg(i, t, s, e, legacy=True) for i, (t, s, e) in enumerate([
        ("大家好，今天讲高血压的日常管理。", 0, 5200),
        ("第一，按时服药不能随便停药。", 5400, 11000),
        ("第二，定期测量血压并做好记录。", 13000, 19000),
        ("记住，血压平稳才能减少并发症。", 19200, 25000),
    ])]
    record = ca.analyze_content(legacy, transcript_version=1)
    quote = record["golden_quotes"][0]
    assert quote["segment_ids"] == ["seg_0001"]              # 存量形状补伪 id
    assert (quote["start_ms"], quote["end_ms"]) == (5400, 11000)


def test_model_failure_automatic_fallback_without_blocking(monkeypatch: pytest.MonkeyPatch):
    def boom(model, prompt):
        raise RuntimeError("配额不足：rate limit")

    monkeypatch.setattr(ca, "_model_ready", lambda: (True, "openai:test-model", ""))
    monkeypatch.setattr(ca, "_run_analysis_agent", boom)
    record = ca.analyze_content(_segments(), transcript_version=2)
    assert record["plan_source"] == "heuristic_fallback"
    assert record["llm_requested"] is True
    assert "配额不足" in (record["llm_error"] or "")
    assert record["llm_calls"] == 1                          # 尝试过才谈失败
    assert record["keywords"]                                # 本地关键词非空
    assert record["topic_units"], "停顿切分应产出主题块"
    assert all(u["title"].startswith("主题块") for u in record["topic_units"])  # 无词表依赖
    for interval in record["candidate_intervals"]:
        assert 0 <= interval["start_ms"] < interval["end_ms"] <= 25000


def test_model_not_configured_falls_back_without_request(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ca, "_model_ready", lambda: (False, "", "LLM 未配置"))
    record = ca.analyze_content(_segments(), transcript_version=1)
    assert record["plan_source"] == "heuristic_fallback"
    assert record["llm_requested"] is False
    assert record["llm_error"] == "LLM 未配置"
    assert record["llm_calls"] == 0


def test_fallback_titles_never_use_wordlist_labels(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ca, "_model_ready", lambda: (False, "", "LLM 未配置"))
    record = ca.analyze_content(_segments(), transcript_version=1)
    assert all("健康科普" not in u["title"] for u in record["topic_units"])


def test_empty_transcript_short_circuits(monkeypatch: pytest.MonkeyPatch):
    record = ca.analyze_content([], transcript_version=1)
    assert record["plan_source"] == "heuristic_fallback"
    assert record["llm_error"] == "纠错转写为空"


def test_model_settings_request_thinking_disabled_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ca, "_thinking_supported", None)
    settings = ca._analysis_model_settings()
    assert settings["max_tokens"] == ca.MAX_OUTPUT_TOKENS
    assert settings["temperature"] == 0.2
    assert settings["extra_body"] == {"thinking": {"type": "disabled"}}


def test_model_settings_downgrade_remembers_unsupported_gateway(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ca, "_thinking_supported", False)
    assert "extra_body" not in ca._analysis_model_settings()


# ---------------------------------------------------------------------------
# worker 集成：analysis_only 分支在渲染前停住
# ---------------------------------------------------------------------------

class _StateRecorder:
    def __init__(self):
        self.states: list[tuple[str, str]] = []

    def __call__(self, job_id, status, stage, progress, error=None):
        self.states.append((status, stage))


def test_worker_analysis_only_never_reaches_render(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path / "tasks"))

    def _guard(name):
        def _fail(*args, **kwargs):
            raise AssertionError(f"禁止调用 {name}（本阶段不渲染/不搬源片）")
        return _fail

    monkeypatch.setattr(worker, "stage_editing", _guard("stage_editing"))
    monkeypatch.setattr(worker, "stage_rendering", _guard("stage_rendering"))
    monkeypatch.setattr(worker, "_materialise_source_for_job", _guard("_materialise_source_for_job"))
    monkeypatch.setattr(worker, "_upload_auto_edit_artifacts", _guard("_upload_auto_edit_artifacts"))
    monkeypatch.setattr("src.studio_transcript_store.load_corrected_transcript",
                        lambda key, transcript_id=None, transcript_version=None: {
                            "asr_segments": _segments(), "raw_segments": [],
                            "transcript_id": "tr-x", "transcript_version": 7})
    analysis_calls: dict = {}

    def fake_analyze(segments, **kwargs):
        analysis_calls["segments"] = segments
        analysis_calls["transcript_version"] = kwargs.get("transcript_version")
        return {"llm_requested": True, "plan_source": "llm", "llm_error": None,
                "transcript_version": kwargs.get("transcript_version"),
                "keywords": ["高血压"], "golden_quotes": [], "topic_units": [],
                "candidate_intervals": []}

    monkeypatch.setattr(ca, "analyze_content", fake_analyze)
    saved_paths: list[str] = []
    monkeypatch.setattr(ca, "save_analysis_record",
                        lambda job_id, record: saved_paths.append(job_id) or {"path": "/tmp/x.json", "size_bytes": 10})
    artifact_posts: list[dict] = []
    monkeypatch.setattr(worker, "_http",
                        lambda method, url, payload=None, timeout=60: artifact_posts.append(payload) or {})
    monkeypatch.setattr("src.r2.upload_file",
                        lambda path, key: {"object_key": key, "size": 10})
    recorder = _StateRecorder()
    monkeypatch.setattr(worker, "_state", recorder)

    job = {"job_id": "autoedit_test01", "object_key": "local:/tmp/v.mp4",
           "project_id": "timeline_t", "config": {"job_kind": "auto_edit", "analysis_only": True}}
    worker.run_auto_edit_only(job)

    statuses = [status for status, _ in recorder.states]
    assert statuses[0] == "retrieving_rules"
    assert "extracting_highlights" in statuses
    assert statuses[-1] == "analysis_ready"
    assert "downloading" not in statuses and "rendering" not in statuses and "editing" not in statuses
    assert analysis_calls["transcript_version"] == 7         # 读取已存转写版本
    assert len(analysis_calls["segments"]) == 4
    assert saved_paths == ["autoedit_test01"]                # 分析已落盘
    assert artifact_posts and artifact_posts[0]["kind"] == "content_analysis"


def test_worker_formal_chain_runs_analysis_plan_render(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """第五步正式链（无 stop_after）：分析 → 规划 → 渲染；video-brain 旧阶段不再被调用。"""
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path / "tasks"))
    monkeypatch.setattr("src.studio_transcript_store.load_corrected_transcript",
                        lambda key, transcript_id=None, transcript_version=None: {
                            "asr_segments": _segments(), "raw_segments": [],
                            "transcript_id": "tr-x", "transcript_version": 7})
    monkeypatch.setattr(worker, "_state", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_rule_snapshot_id", lambda: "2026-10-13.10")

    def _guard(name):
        def _fail(*args, **kwargs):
            raise AssertionError(f"正式链不再调用 {name}（video-brain 旧链已被 staged 渲染取代）")
        return _fail
    monkeypatch.setattr(worker, "stage_analyzing", _guard("stage_analyzing"))
    monkeypatch.setattr(worker, "stage_editing", _guard("stage_editing"))
    monkeypatch.setattr(worker, "stage_rendering", _guard("stage_rendering"))
    monkeypatch.setattr(worker, "_upload_auto_edit_artifacts", _guard("_upload_auto_edit_artifacts"))
    monkeypatch.setattr(worker, "_materialise_source_for_job", lambda key, jid: "video.mp4")

    calls: list[str] = []

    def fake_analyze(segments, **kwargs):
        calls.append("analysis")
        return {"plan_source": "llm", "keywords": ["高血压"], "golden_quotes": [],
                "topic_units": [], "candidate_intervals": [],
                "analysis_artifact_path": str(tmp_path / "a.json")}
    monkeypatch.setattr(ca, "analyze_content", fake_analyze)
    monkeypatch.setattr(ca, "save_analysis_record", lambda job_id, record: {"path": "/tmp/a.json", "size_bytes": 1})

    def fake_plan(segments, analysis, **kwargs):
        calls.append("plan")
        return {"plan_source": "llm", "clips": [], "discarded": [], "notes": [],
                "rule_snapshot_id": "2026-10-13.10"}
    monkeypatch.setattr(cp, "plan_clips", fake_plan)
    monkeypatch.setattr(cp, "save_plan_record", lambda job_id, record: {"path": "/tmp/p.json", "size_bytes": 1})

    def fake_render(plan, **kwargs):
        calls.append("render")
        return {"job_status": "completed", "entries": [],
                "counts": {"total": 0, "succeeded": 0, "technical_error": 0, "skipped_playable": 0},
                "warnings": []}
    monkeypatch.setattr(cr, "render_plan", fake_render)
    monkeypatch.setattr(worker, "_http", lambda *a, **k: {})
    monkeypatch.setattr("src.r2.upload_file", lambda path, key: {"object_key": key, "size": 1})

    job = {"job_id": "autoedit_formal01", "object_key": "local:/tmp/v.mp4",
           "project_id": "timeline_t", "config": {"job_kind": "auto_edit"}}
    worker.run_auto_edit_only(job)
    assert calls == ["analysis", "plan", "render"]   # 全自动顺序，无停点


def test_saved_record_shape_matches_template(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path / "tasks"))
    record = ca._record(llm_requested=True, plan_source="llm", llm_error=None,
                        transcript_version=5, keywords=["k"], golden_quotes=[],
                        topic_units=[], candidate_intervals=[], notes=[])
    saved = ca.save_analysis_record("autoedit_shape01", record)
    data = json.loads((tmp_path / "tasks" / "analysis" / "autoedit_shape01.json").read_text())
    for key in ("llm_requested", "plan_source", "llm_error", "transcript_version",
                "keywords", "golden_quotes", "topic_units", "candidate_intervals"):
        assert key in data
    assert saved["size_bytes"] > 0
