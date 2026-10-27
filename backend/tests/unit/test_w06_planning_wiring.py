"""W06B真实plan_clips入口，模型为纯fixture；不运行worker/API/渲染。"""
import copy
import json
import sys
from types import SimpleNamespace

import pytest

from src import clip_planning as cp
from src import content_analysis as ca
from src import llm_budget


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "_model_ready", lambda: (True, "fixture", ""))
    monkeypatch.setattr(llm_budget, "_ledger_root", lambda: tmp_path / "ledger")
    monkeypatch.setattr(ca, "_thinking_supported", None)


def segments(count):
    return [{"segment_id": f"s{i:04d}", "start_ms": i * 1000,
             "end_ms": (i + 1) * 1000, "text": f"第{i}段完整内容。"} for i in range(count)]


def analysis(count):
    return {"keywords": ["内容"], "golden_quotes": [],
        "topic_units": [{"title": "全篇主题", "segment_ids": [f"s{i:04d}" for i in range(count)],
                         "start_ms": 0, "end_ms": count * 1000}],
        "candidate_intervals": [{"start_ms": 0, "end_ms": count * 1000, "topic_title": "全篇主题"}]}


def ids_of(prompt):
    return [line.split("|")[0] for line in prompt.splitlines() if line.startswith("s") and "|" in line]


def out_for(ids):
    return cp._PlanOut(clips=[cp._PlanClipOut(topic="完整主题", start_segment_id=ids[0],
                                             end_segment_id=ids[-1])])


def test_long_input_real_entry_covers_last_window_bounded_prompt_and_stable_ids(monkeypatch):
    source, summary, calls = segments(1307), analysis(1307), []
    before = copy.deepcopy([source, summary])
    def callback(model, prompt):
        ids = ids_of(prompt)
        calls.append(ids)
        assert len(ids) <= 400
        assert prompt.count("全篇主题概览") == 1
        return out_for(ids)
    monkeypatch.setattr(cp, "_run_plan_agent", callback)
    result = cp.plan_clips(source, summary, transcript_version=3, project_id="p")
    assert result["plan_source"] == "llm" and result["llm_calls"] == len(calls) == 4
    assert {sid for window in calls for sid in window} == {row["segment_id"] for row in source}
    assert result["clips"][-1]["end_segment_id"] == "s1306"
    assert all(clip["clip_id"] == cp._clip_id("p", clip["start_ms"], clip["end_ms"])
               for clip in result["clips"])
    assert [source, summary] == before
    assert result["llm_usage"]["prompt_tokens"] is None


def test_normal_entry_keeps_single_call(monkeypatch):
    calls = []
    def callback(model, prompt):
        calls.append(ids_of(prompt))
        return out_for(calls[-1])
    monkeypatch.setattr(cp, "_run_plan_agent", callback)
    result = cp.plan_clips(segments(479), analysis(479), transcript_version=1)
    assert result["plan_source"] == "llm" and result["llm_calls"] == 1
    assert len(calls[0]) == 479 and cp.LLM_CALL_TIMEOUT_SECONDS >= 240


def test_failed_window_only_is_split_successful_window_not_replayed(monkeypatch):
    calls = []
    def callback(model, prompt):
        ids = ids_of(prompt)
        calls.append(ids)
        if 360 <= int(ids[0][1:]) and int(ids[-1][1:]) < 760 and len(ids) > 200:
            raise RuntimeError("local window too large")
        cp._plan_call_metadata.set({"usage": llm_budget.extract_usage_details(
            {"input_tokens": 0, "output_tokens": 0}), "settings": {"actual": "fixture"}})
        return out_for(ids)
    monkeypatch.setattr(cp, "_run_plan_agent", callback)
    result = cp.plan_clips(segments(1307), analysis(1307), transcript_version=1)
    assert sum(ids[0] == "s0000" for ids in calls) == 1
    assert result["failed_windows"] == [] and "local_split_retry" in result["degradations"]
    assert any(len(ids) < 400 and 360 <= int(ids[0][1:]) < 760 for ids in calls)
    assert result["clips"][-1]["end_segment_id"] == "s1306"
    assert result["llm_usage"]["usage_status"] == "partial"
    assert result["llm_usage"]["prompt_tokens"] is None
    assert result["llm_calls"] <= result["callback_budget"]


def test_terminal_local_failure_becomes_mixed_and_all_failure_rule_source(monkeypatch):
    def partial(model, prompt):
        ids = ids_of(prompt)
        if 360 <= int(ids[0][1:]) and int(ids[-1][1:]) < 760:
            raise RuntimeError("local permanent failure")
        return out_for(ids)
    monkeypatch.setattr(cp, "_run_plan_agent", partial)
    result = cp.plan_clips(segments(1307), analysis(1307), transcript_version=1)
    assert result["plan_source"] == "mixed" and not result["llm_enabled"]
    assert result["failed_windows"] and result["warnings"]
    assert any(clip["plan_source"] == "llm" for clip in result["clips"])
    def fail(*args):
        raise RuntimeError("all windows failed")
    monkeypatch.setattr(cp, "_run_plan_agent", fail)
    failed = cp.plan_clips(segments(1307), analysis(1307), transcript_version=1)
    assert failed["plan_source"] == "heuristic_fallback" and failed["clips"]
    assert all(clip["plan_source"] == "heuristic_fallback" for clip in failed["clips"])
    assert failed["failed_windows"] and failed["llm_calls"] <= failed["callback_budget"]
    assert failed["llm_usage"]["usage_status"] == "unknown"


def test_invalid_ids_rejected_and_overlap_deduplicated_at_actual_entry(monkeypatch):
    def callback(model, prompt):
        return cp._PlanOut(clips=[
            cp._PlanClipOut(topic="valid", start_segment_id="s0001", end_segment_id="s0005"),
            cp._PlanClipOut(topic="duplicate", start_segment_id="s0002", end_segment_id="s0005"),
            cp._PlanClipOut(topic="invalid", start_segment_id="missing", end_segment_id="s0009")])
    monkeypatch.setattr(cp, "_run_plan_agent", callback)
    result = cp.plan_clips(segments(10), analysis(10), transcript_version=1)
    assert len(result["clips"]) == 1 and result["clips"][0]["start_ms"] == 1000
    assert result["hierarchical_planning"]["rejected_candidates"]
    assert result["hierarchical_planning"]["duplicates"]


def test_run_result_usage_before_output_zero_and_missing(monkeypatch):
    import pydantic_ai
    output = out_for(["s0000"])
    result = SimpleNamespace(output=output, usage=lambda: SimpleNamespace(input_tokens=0, output_tokens=0))
    class Agent:
        def __class_getitem__(cls, _):
            return cls
        def __init__(self, **kwargs):
            pass
        async def run(self, *args, **kwargs):
            return result
    monkeypatch.setitem(sys.modules, "src.ai", SimpleNamespace(
        _build_transcript_model=lambda c: None, apply_settings_to_process_env=lambda c: None))
    monkeypatch.setitem(sys.modules, "src.config", SimpleNamespace(
        get_config=lambda: SimpleNamespace(as_runtime_settings=lambda: {})))
    monkeypatch.setattr(pydantic_ai, "Agent", Agent)
    assert cp._run_plan_agent("fixture", "test") is output
    assert cp._plan_call_metadata.get()["usage"]["usage_status"] == "known"
    assert cp._plan_call_metadata.get()["usage"]["prompt_tokens"] == 0
    result.usage = lambda: None
    cp._run_plan_agent("fixture", "test")
    assert cp._plan_call_metadata.get()["usage"]["usage_status"] == "unknown"


def test_plan_cache_independent_current_identity_and_each_change_invalidates(tmp_path, monkeypatch):
    source, summary = segments(10), analysis(10)
    monkeypatch.setattr(cp, "plans_root", lambda: tmp_path)
    before = cp.build_current_plan_provenance(source, summary, transcript_version=1,
                                            rules_snapshot={"actual_rule": 1})
    def callback(model, prompt):
        cp._plan_call_metadata.set({"usage": llm_budget.extract_usage_details(
            {"input_tokens": 10, "output_tokens": 2}), "settings": {"max_tokens": 6144}})
        return out_for(ids_of(prompt))
    monkeypatch.setattr(cp, "_run_plan_agent", callback)
    record = cp.plan_clips(source, summary, transcript_version=1, rules_snapshot={"actual_rule": 1})
    assert record["source_provenance"] == before
    assert record["actual_calls"][0]["parameters"] == {"max_tokens": 6144}
    assert "actual_calls" not in before["parameters"]
    cp.save_plan_record("j", record)
    assert cp.load_plan_record("j", expected_provenance=before)["cache_verification"]["reusable"]
    assert cp.load_plan_record("j")["cache_verification"]["status"] == "context_unverified"
    assert cp.load_plan_record("j", require_verified=True) is None
    contexts = [cp.build_current_plan_provenance(source, summary, transcript_version=2, rules_snapshot={"actual_rule": 1}),
                cp.build_current_plan_provenance(source, summary, transcript_version=1, rules_snapshot={"actual_rule": 2}),
                cp.build_current_plan_provenance(source, summary, transcript_version=1, model="other", rules_snapshot={"actual_rule": 1})]
    for expected in contexts:
        assert cp.load_plan_record("j", expected_provenance=expected) is None
    for attr in ("PLAN_MAX_OUTPUT_TOKENS", "_PLAN_SYSTEM_PROMPT", "PLAN_SCHEMA_VERSION", "PLAN_ADAPTER_VERSION"):
        old = getattr(cp, attr)
        monkeypatch.setattr(cp, attr, old + 1 if isinstance(old, int) else old + "changed")
        expected = cp.build_current_plan_provenance(source, summary, transcript_version=1, rules_snapshot={"actual_rule": 1})
        assert cp.load_plan_record("j", expected_provenance=expected) is None
        monkeypatch.setattr(cp, attr, old)
    cp.save_plan_record("legacy", {"clips": [], "plan_source": "llm"})
    assert cp.load_plan_record("legacy")["cache_verification"]["status"] == "legacy_unverified"
    assert cp.load_plan_record("legacy", expected_provenance=before) is None


def test_planning_concurrent_usage_and_actual_settings_isolated(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier = Barrier(2)
    monkeypatch.setattr(llm_budget, "record_call", lambda *a, **k: None)
    def callback(model, prompt):
        tag = 17 if "first" in prompt else 99
        cp._plan_call_metadata.set({"usage": llm_budget.extract_usage_details(
            {"input_tokens": tag, "output_tokens": 0}), "settings": {"tag": tag}})
        barrier.wait(timeout=5)
        return out_for(ids_of(prompt))
    monkeypatch.setattr(cp, "_run_plan_agent", callback)
    def execute(text):
        source = segments(2)
        source[0]["text"] = text
        prior = cp._plan_call_metadata.get()
        result = cp.plan_clips(source, analysis(2), transcript_version=1)
        assert cp._plan_call_metadata.get() is prior
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, ["first", "second"]))
    assert [row["llm_usage"]["prompt_tokens"] for row in results] == [17, 99]
    assert [row["actual_calls"][0]["parameters"]["tag"] for row in results] == [17, 99]


def test_discarded_overlap_with_selected_neighbor_clip_is_removed(monkeypatch):
    def callback(model, prompt):
        ids = ids_of(prompt)
        return cp._PlanOut(clips=[cp._PlanClipOut(topic="完整内容", start_segment_id=ids[0],
            end_segment_id=ids[-1])], discarded=[cp._DiscardedOut(segment_ids=[ids[0]],
                reason="模型矛盾舍弃", type="重复表达")])
    monkeypatch.setattr(cp, "_run_plan_agent", callback)
    record = cp.plan_clips(segments(1307), analysis(1307), transcript_version=1)
    assert record["discarded"] == []
