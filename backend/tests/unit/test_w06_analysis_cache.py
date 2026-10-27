"""W06A 来源核对；所有保存使用临时目录。"""
import copy
import json

import pytest

from src import content_analysis as ca


SEGMENTS = [{"segment_id": "s1", "start_ms": 0, "end_ms": 1000, "text": "原文"}]


def context(**updates):
    args = dict(transcript_version=3, model="fixture", parameters={"temperature": 0.2},
                rules_snapshot={"actual_rule": {"gap_ms": 1500}}, prompt_text="actual prompt")
    args.update(updates)
    return ca.build_analysis_provenance(SEGMENTS, **args)


def test_save_load_matching_context_and_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "analysis_root", lambda: tmp_path)
    provenance = context()
    ca.save_analysis_record("j", {"plan_source": "llm"}, provenance=provenance)
    read = ca.load_analysis_record("j")
    assert read["cache_verification"] == {"status": "context_unverified", "reusable": False}
    verified = ca.load_analysis_record("j", expected_provenance=provenance)
    assert verified["cache_verification"]["reusable"] is True
    assert ca.load_analysis_record("j", require_verified=True) is None
    ca.save_analysis_record("legacy", {"plan_source": "heuristic_fallback"})
    assert ca.load_analysis_record("legacy")["cache_verification"]["status"] == "legacy_unverified"
    assert ca.load_analysis_record("legacy", expected_provenance=provenance) is None


@pytest.mark.parametrize("updates", [
    {"transcript_version": 4}, {"model": "other"},
    {"parameters": {"temperature": 0.3}},
    {"rules_snapshot": {"actual_rule": {"gap_ms": 1600}}},
    {"prompt_version": "other"}, {"prompt_text": "changed actual prompt"},
    {"schema_version": "other"}, {"helper_version": "other"},
])
def test_each_source_change_invalidates_cache(tmp_path, monkeypatch, updates):
    monkeypatch.setattr(ca, "analysis_root", lambda: tmp_path)
    ca.save_analysis_record("j", {"plan_source": "llm"}, provenance=context())
    assert ca.load_analysis_record("j", expected_provenance=context(**updates)) is None


def test_content_change_and_missing_evidence_and_tamper():
    original = context()
    changed = copy.deepcopy(SEGMENTS)
    changed[0]["text"] = "新原文"
    modified = ca.build_analysis_provenance(changed, transcript_version=3, model="fixture",
        parameters={"temperature": 0.2}, rules_snapshot={"actual_rule": {"gap_ms": 1500}},
        prompt_text="actual prompt")
    assert ca.verify_analysis_provenance({"source_provenance": original}, modified)["reusable"] is False
    missing = context(rules_snapshot=None)
    assert missing["missing"] == ["rules_snapshot"]
    assert ca.verify_analysis_provenance({"source_provenance": missing}, missing)["reusable"] is False
    bad = copy.deepcopy(original)
    bad["rules_snapshot"]["actual_rule"]["gap_ms"] = 7
    assert ca.verify_analysis_provenance({"source_provenance": bad}, bad)["status"] == "incomplete_unverified"


def test_analyze_compatible_entry_freezes_actual_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "_model_ready", lambda: (True, "fixture", ""))
    monkeypatch.setattr(ca, "_run_analysis_agent", lambda *a: ca._AnalysisOut(keywords=["原文"]))
    monkeypatch.setattr(ca.llm_budget, "_ledger_root", lambda: tmp_path)
    record = ca.analyze_content(SEGMENTS, transcript_version=3, job_id="j", project_id="p")
    source = record["source_provenance"]
    assert source["complete"] is True
    assert source["model"] == "fixture" and source["rules_snapshot"]["implementation_sha256"]
    assert record["llm_usage"]["usage_status"] == "unknown"
    assert record["cache_verification"]["reusable"] is False


def test_pre_call_source_independently_matches_saved_record(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "_model_ready", lambda: (True, "fixture", ""))
    monkeypatch.setattr(ca, "analysis_root", lambda: tmp_path)
    monkeypatch.setattr(ca.llm_budget, "record_call", lambda *a, **k: None)
    before = ca.build_current_analysis_provenance(SEGMENTS, transcript_version=3)
    def callback(*args):
        ca._analysis_call_metadata.set({"usage": ca.llm_budget.extract_usage_details(
            {"input_tokens": 7, "output_tokens": 0}), "settings": {"max_tokens": 6144}})
        return ca._AnalysisOut()
    monkeypatch.setattr(ca, "_run_analysis_agent", callback)
    record = ca.analyze_content(SEGMENTS, transcript_version=3)
    assert before == record["source_provenance"]
    assert "actual_calls" not in before["parameters"]
    assert record["llm_parameters"] == [{"max_tokens": 6144}]
    ca.save_analysis_record("j", record)
    assert ca.load_analysis_record("j", expected_provenance=before) is not None
    monkeypatch.setattr(ca, "MAX_OUTPUT_TOKENS", ca.MAX_OUTPUT_TOKENS + 1)
    current = ca.build_current_analysis_provenance(SEGMENTS, transcript_version=3)
    assert ca.load_analysis_record("j", expected_provenance=current) is None
