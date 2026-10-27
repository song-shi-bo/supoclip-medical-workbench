"""W06A usage 独立局部证据，不访问模型、数据库或真实台账。"""
import json
import sys
from types import SimpleNamespace

import pytest

from src import content_analysis as ca
from src import llm_budget as budget


@pytest.mark.parametrize("value,prompt,completion,reasoning,total,status", [
    (None, None, None, None, None, "unknown"),
    (object(), None, None, None, None, "unknown"),
    ({"usage": {"input_tokens": 0, "output_tokens": 0}}, 0, 0, None, 0, "known"),
    ({"request_tokens": 15, "response_tokens": 4, "details": {"reasoning_tokens": 2}},
     15, 4, 2, 19, "known"),
    ({"usage": {"prompt_tokens": 8, "completion_tokens": 3,
                "completion_tokens_details": {"reasoning_tokens": 1}}}, 8, 3, 1, 11, "known"),
    ({"usage": {"total_tokens": 7}}, None, None, None, 7, "partial"),
    ({"usage": {"input_tokens": -1, "output_tokens": float("nan")}},
     None, None, None, None, "unknown"),
    ({"usage": {"input_tokens": True, "output_tokens": 2.5}},
     None, None, None, None, "unknown"),
])
def test_details_shapes(value, prompt, completion, reasoning, total, status):
    result = budget.extract_usage_details(value)
    assert [result[k] for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens",
                                "total_tokens", "usage_status")] == [prompt, completion, reasoning, total, status]


def test_usage_method_property_real_pydantic_and_legacy_view():
    from pydantic_ai.usage import RunUsage
    real = RunUsage(input_tokens=12, output_tokens=3, details={"reasoning_tokens": 1})
    assert budget.extract_usage_details(SimpleNamespace(usage=lambda: real))["total_tokens"] == 15
    assert budget.extract_usage_details(SimpleNamespace(usage=real))["prompt_tokens"] == 12
    assert budget.extract_usage(None) == {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    assert budget.extract_usage(SimpleNamespace(usage=real)) == {
        "prompt_tokens": 12, "completion_tokens": 3, "reasoning_tokens": 1}
    def broken():
        raise RuntimeError("usage unavailable")
    assert budget.extract_usage_details(SimpleNamespace(usage=broken))["usage_status"] == "unknown"


def test_ledger_preserves_unknown_and_true_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "_ledger_root", lambda: tmp_path)
    budget.record_call("unknown", est_reserved=2)
    budget.record_call("zero", est_reserved=3, usage=budget.extract_usage_details(
        {"usage": {"input_tokens": 0, "output_tokens": 0}}))
    entries = json.loads(budget.ledger_path().read_text())["entries"]
    assert entries[0]["usage_details"]["prompt_tokens"] is None
    assert entries[0]["usage_details"]["usage_status"] == "unknown"
    assert entries[1]["usage_details"]["prompt_tokens"] == 0
    assert entries[1]["usage_details"]["usage_status"] == "known"
    assert budget.today_totals() == (5, 2)


def test_analysis_extracts_run_result_before_output(monkeypatch):
    import pydantic_ai
    output = ca._AnalysisOut(keywords=["测试"])
    run_result = SimpleNamespace(output=output,
        usage=lambda: SimpleNamespace(input_tokens=17, output_tokens=5, details={"reasoning_tokens": 2}))
    class FakeAgent:
        def __class_getitem__(cls, _):
            return cls
        def __init__(self, **kwargs):
            pass
        async def run(self, *args, **kwargs):
            return run_result
    config = SimpleNamespace(as_runtime_settings=lambda: {})
    monkeypatch.setitem(sys.modules, "src.ai", SimpleNamespace(
        _build_transcript_model=lambda c: None, apply_settings_to_process_env=lambda c: None))
    monkeypatch.setitem(sys.modules, "src.config", SimpleNamespace(get_config=lambda: config))
    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    assert ca._run_analysis_agent("fixture", "test") is output
    assert ca._analysis_call_metadata.get()["usage"]["prompt_tokens"] == 17
    assert ca._analysis_call_metadata.get()["usage"]["completion_tokens"] == 5
    assert ca._analysis_call_metadata.get()["usage"]["usage_status"] == "known"


def test_aggregate_missing_call_does_not_turn_unknown_into_zero():
    result = ca._aggregate_usage([budget.extract_usage_details(None),
        budget.extract_usage_details({"input_tokens": 0, "output_tokens": 0})])
    assert result["usage_status"] == "partial"
    assert result["prompt_tokens"] is None


def test_failed_batch_usage_stays_partial_and_does_not_leak_previous_call(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "_model_ready", lambda: (True, "fixture", ""))
    monkeypatch.setattr(budget, "_ledger_root", lambda: tmp_path)
    monkeypatch.setattr(ca, "_heuristic_fallback", lambda *a, **k: {"plan_source": "heuristic_fallback"})
    calls = 0
    def callback(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second batch failed")
        ca._analysis_call_metadata.set({"usage": budget.extract_usage_details({"input_tokens": 17, "output_tokens": 5}), "settings": {}})
        return ca._AnalysisOut()
    monkeypatch.setattr(ca, "_run_analysis_agent", callback)
    source = [{"segment_id": f"s{i}", "start_ms": i * 1000, "end_ms": (i + 1) * 1000,
               "text": "原文"} for i in range(601)]
    record = ca._analyze_content(source, transcript_version=1)
    assert record["llm_calls"] == 2
    assert record["llm_usage"]["usage_status"] == "partial"
    assert record["llm_usage"]["prompt_tokens"] is None
    assert record["llm_usage"]["calls"][1]["usage_status"] == "unknown"


def test_analysis_concurrent_calls_keep_usage_and_settings_isolated(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier = Barrier(2)
    monkeypatch.setattr(ca, "_model_ready", lambda: (True, "fixture", ""))
    monkeypatch.setattr(budget, "record_call", lambda *a, **k: None)
    def callback(model, prompt):
        tag = 17 if "first" in prompt else 99
        ca._analysis_call_metadata.set({"usage": budget.extract_usage_details(
            {"input_tokens": tag, "output_tokens": 0}), "settings": {"tag": tag}})
        barrier.wait(timeout=5)
        return ca._AnalysisOut()
    monkeypatch.setattr(ca, "_run_analysis_agent", callback)
    def execute(text):
        result = ca.analyze_content([{"segment_id": "s", "start_ms": 0, "end_ms": 1000,
                                     "text": text}], transcript_version=1)
        assert ca._analysis_call_metadata.get() is None
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(execute, ["first", "second"]))
    assert [row["llm_usage"]["prompt_tokens"] for row in rows] == [17, 99]
    assert [row["llm_parameters"][0]["tag"] for row in rows] == [17, 99]
