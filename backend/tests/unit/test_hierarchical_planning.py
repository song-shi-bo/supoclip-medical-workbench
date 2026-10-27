"""W06A >1200段、重叠、边界和有界局部恢复的合成证据。"""
import copy

import pytest

from src.hierarchical_planning import build_planning_windows, plan_hierarchically


def segments(n):
    return [{"segment_id": f"s{i}", "start_ms": i * 1000,
             "end_ms": (i + 1) * 1000, "text": f"源段{i}"} for i in range(n)]


def plan(window):
    return {"plan_source": "llm", "clips": [{"topic": "内容", "start_segment_id": window["segment_ids"][0],
                                               "end_segment_id": window["segment_ids"][-1]}]}


def test_long_input_fully_covered_sparse_topics_immutable():
    source = segments(1307)
    analysis = {"topic_units": [{"title": "稀疏主题", "segment_ids": ["s20", "s90"]}],
                "candidate_intervals": [{"start_ms": 0, "end_ms": 100000}]}
    before = copy.deepcopy([source, analysis])
    windows = build_planning_windows(source, analysis, max_segments=200, overlap_segments=25)
    assert set().union(*(set(w["segment_ids"]) for w in windows)) == {s["segment_id"] for s in source}
    assert windows[-1]["segment_ids"][-1] == "s1306"
    assert all(len(w["segments"]) <= 200 for w in windows)
    assert all(windows[i]["segment_ids"][-25:] == windows[i+1]["segment_ids"][:25]
               for i in range(len(windows)-1))
    assert [source, analysis] == before


def test_topic_boundary_and_fingerprint():
    source = segments(20)
    analysis = {"topic_units": [{"title": "主题", "segment_ids": ["s0", "s6"]}]}
    windows = build_planning_windows(source, analysis, max_segments=10, overlap_segments=2)
    assert windows[0]["segment_ids"][-1] == "s6"
    changed = copy.deepcopy(source)
    changed[0]["text"] = "变化"
    assert build_planning_windows(changed, analysis, max_segments=10, overlap_segments=2)[0]["input_fingerprint"] != windows[0]["input_fingerprint"]


def test_local_failure_split_success_and_callback_cannot_mutate_source():
    source = segments(16)
    before = copy.deepcopy(source)
    def callback(window):
        if len(window["segments"]) > 4:
            raise RuntimeError("window too large")
        result = plan(window)
        window["segments"][0]["text"] = "mutated"
        return result
    result = plan_hierarchically(source, {}, callback, max_segments=16, overlap_segments=0,
                                min_segments=2, max_depth=3)
    assert result["callback_calls"] == 7
    assert len(result["clips"]) == 4
    assert result["failed_windows"] == []
    assert "local_split_retry" in result["degradations"]
    assert source == before
    assert result["llm_enabled"] is True


def test_terminal_failure_visible_and_callback_budget():
    def fail(window):
        raise RuntimeError("always fails")
    result = plan_hierarchically(segments(40), {}, fail, max_segments=20,
        overlap_segments=0, min_segments=1, max_depth=8, max_calls=5)
    assert result["callback_calls"] == 5
    assert result["failed_windows"] and not result["llm_enabled"]
    failed = set().union(*(set(w["segment_ids"]) for w in result["failed_windows"]))
    assert failed == {f"s{i}" for i in range(40)}
    assert any("budget" in w["reason"] for w in result["failed_windows"])


@pytest.mark.parametrize("clip", [
    {"start_segment_id": "missing", "end_segment_id": "s3"},
    {"start_segment_id": "s0", "end_segment_id": "s3", "end_ms": 999999},
    {"segment_ids": ["s0", "s2"]},
    {"start_segment_id": "s3", "end_segment_id": "s1"},
    {"start_segment_id": "s0", "end_segment_id": "s1", "evidence_segment_ids": ["s2"]},
])
def test_invalid_candidates_rejected(clip):
    result = plan_hierarchically(segments(4), {}, lambda w: {"plan_source": "llm", "clips": [clip]},
                                max_segments=4, overlap_segments=0, max_depth=0)
    assert result["clips"] == [] and result["rejected_candidates"] and result["failed_windows"]


def test_window_cannot_reference_other_root_source():
    result = plan_hierarchically(segments(8), {}, lambda w: {"clips": [
        {"start_segment_id": "s0", "end_segment_id": "s7"}]},
        max_segments=4, overlap_segments=0, max_depth=0)
    assert not result["clips"] and len(result["failed_windows"]) == 2


def test_overlap_deduplicated_with_lineage_and_source_order():
    def callback(w):
        return {"plan_source": "llm", "clips": [
            {"segment_ids": ["s4", "s5"]},
            {"segment_ids": ["s5"]}]}
    result = plan_hierarchically(segments(10), {}, callback, max_segments=6,
                                overlap_segments=2, max_depth=0)
    assert len(result["clips"]) == 1 and len(result["duplicates"]) == 3
    assert len(result["clips"][0]["lineage"]) == 2
    assert result["clips"][0]["start_ms"] == 4000
    assert result["clips"][0]["end_ms"] == 6000


def test_rule_callback_is_never_model_success_and_empty_model_plan_allowed():
    result = plan_hierarchically(segments(4), {}, lambda w: {**plan(w), "plan_source": "heuristic_fallback"},
                                max_segments=4, overlap_segments=0)
    assert result["plan_source"] == "heuristic_fallback" and not result["llm_enabled"]
    unverified = plan_hierarchically(segments(4), {}, lambda w: plan(w)["clips"],
                                    max_segments=4, overlap_segments=0)
    assert not unverified["llm_enabled"]
    empty = plan_hierarchically(segments(4), {}, lambda w: {"plan_source": "llm", "clips": []},
                               max_segments=4, overlap_segments=0)
    assert empty["failed_windows"] == [] and empty["clips"] == []


def test_invalid_input_rejected_before_callback():
    source = segments(2)
    source[1]["segment_id"] = "s0"
    with pytest.raises(ValueError):
        build_planning_windows(source, {})
    with pytest.raises(ValueError):
        build_planning_windows(segments(2), {}, max_segments=2, overlap_segments=2)


def test_long_planning_reaches_last_source_and_source_order():
    result = plan_hierarchically(segments(1307), {}, plan, max_segments=200, overlap_segments=25)
    assert result["clips"][-1]["end_segment_id"] == "s1306"
    assert set().union(*(set(clip["segment_ids"]) for clip in result["clips"])) == {f"s{i}" for i in range(1307)}
    assert [clip["start_ms"] for clip in result["clips"]] == sorted(clip["start_ms"] for clip in result["clips"])
    assert result["callback_calls"] == len(result["windows"])


def test_discarded_unknown_ids_do_not_escape_validator():
    result = plan_hierarchically(segments(4), {}, lambda w: {"plan_source": "llm", "clips": [],
        "discarded": [{"segment_ids": ["missing"], "reason": "bad"},
                      {"segment_ids": ["s1", "s0", "s1"], "reason": "valid"}]},
        max_segments=4, overlap_segments=0)
    assert result["discarded"][0]["segment_ids"] == ["s0", "s1"]
    assert len(result["discarded"]) == 1 and result["rejected_candidates"]
