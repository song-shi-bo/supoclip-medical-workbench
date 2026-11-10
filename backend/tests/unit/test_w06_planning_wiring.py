"""W06B 真实 plan_clips 入口（两步制第一步·提示词A），模型为纯 fixture。

2026-11-05 两步制改造：分层窗口机制退役，提示词A是全局一次规划
（time_range 吸附 + 全覆盖契约 + 单次重试）。本文件验证真实入口接线：
prompt 拼装、缓存身份、ContextVar 隔离、失败降级与来源标注。
"""
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
    return [line.split("|")[0] for line in prompt.splitlines()
            if line.startswith("s") and "|" in line and " --> " not in line]


def out_for(ids, video_summary="高血压常见误区科普"):
    """全局一次规划的正常输出：首尾时间码 = 首末分段（提示词A格式）。"""
    first = ids[0]
    last = ids[-1]
    start = cp._clock_of(next(int(s[1:]) for s in ids) * 1000)
    end = cp._clock_of((max(int(s[1:]) for s in ids) + 1) * 1000)
    return cp._SlicePlanOut(
        video_summary=video_summary, slice_count=1,
        slices=[cp._SliceOut(id=1, time_range=[start, end], theme="完整主题",
                             hook_first_line="钩子", cover_text="封面")],
        discarded_segments=[])


def test_long_input_single_global_call_covers_all_segments(monkeypatch):
    """长输入不再拆窗口：提示词A必须一次看到整片（≤全局上限时）。"""
    source, summary, prompts = segments(1307), analysis(1307), []
    before = copy.deepcopy([source, summary])

    def callback(model, prompt):
        ids = ids_of(prompt)
        prompts.append(ids)
        return out_for(ids)

    monkeypatch.setattr(cp, "_run_plan_agent", callback)
    result = cp.plan_clips(source, summary, transcript_version=3, project_id="p")
    assert result["plan_source"] == "llm" and result["llm_calls"] == len(prompts) == 1
    assert prompts[0] == [row["segment_id"] for row in source]      # 全量一次给到
    assert result["clips"][0]["end_segment_id"] == "s1306"
    assert result["clips"][0]["start_ms"] == 0 and result["clips"][0]["end_ms"] == 1307000
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


def test_llm_failure_retries_once_then_falls_back(monkeypatch):
    """失败外层只重试一次（无窗口可拆）；仍失败 → 本地回退，degradations 如实标注。"""
    calls = []

    def flaky(model, prompt):
        calls.append(1)
        raise RuntimeError("local permanent failure")

    monkeypatch.setattr(cp, "_run_plan_agent", flaky)
    monkeypatch.setattr(cp.time, "sleep", lambda s: None)
    failed = cp.plan_clips(segments(1307), analysis(1307), transcript_version=1)
    assert failed["plan_source"] == "heuristic_fallback" and failed["clips"]
    assert all(clip["plan_source"] == "heuristic_fallback" for clip in failed["clips"])
    assert failed["llm_calls"] == 2 and len(calls) == 2
    assert failed["failed_windows"] == [] and "slice_plan_failed" in failed["degradations"]
    assert "local permanent failure" in (failed["llm_error"] or "")
    assert failed["llm_usage"]["usage_status"] == "unknown"


def test_out_of_video_range_dropped_and_overlap_trimmed_at_actual_entry(monkeypatch):
    """越界 time_range 舍弃留痕；相邻切片重叠时后者收缩（覆盖契约：不共享素材）。"""
    def callback(model, prompt):
        ids = ids_of(prompt)
        return cp._SlicePlanOut(
            video_summary="s", slice_count=3,
            slices=[
                cp._SliceOut(id=1, time_range=[cp._clock_of(1000), cp._clock_of(5000)],
                             theme="valid"),                                   # s0001..s0004
                cp._SliceOut(id=2, time_range=[cp._clock_of(2000), cp._clock_of(9000)],
                             theme="duplicate"),                               # 重叠 → 收缩到 s0005..s0008
                cp._SliceOut(id=3, time_range=["99:59:00", "99:59:30"],
                             theme="越界"),                                    # 全片外 → 舍弃
            ],
            discarded_segments=[])
    monkeypatch.setattr(cp, "_run_plan_agent", callback)
    result = cp.plan_clips(segments(10), analysis(10), transcript_version=1)
    assert len(result["clips"]) == 2
    assert result["clips"][0]["start_segment_id"] == "s0001"
    assert result["clips"][0]["end_segment_id"] == "s0004"
    assert result["clips"][1]["start_segment_id"] == "s0005"
    assert result["clips"][1]["end_segment_id"] == "s0008"
    assert result["clips"][1]["start_ms"] == 5000
    assert any("越界" in note for note in result["notes"])
    # 全覆盖契约：缺口（s0000 / s0009）程序侧补齐进 discarded
    discarded_ids = {sid for d in result["discarded"] for sid in d["segment_ids"]}
    assert {"s0000", "s0009"} <= discarded_ids
    assert any(d["type"] == "未覆盖" for d in result["discarded"])


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
    # 2026-11-10 医学闸门：plan_clips 的 provenance 记录「含医学标注」的提示词；
    # 直接构建必须同样先过闸门富化，两条路径才逐字节相等（纯函数语义仍成立：
    # 同一转写 + 同一快照 → 同一标注 → 同一指纹）。
    from src import clip_medical_gate as gate_mod
    gate_profile = gate_mod.run_gate(source, summary)
    enriched = dict(summary)
    if gate_profile["enabled"]:
        enriched["medical_annotations"] = gate_profile["unit_annotations"]
    before = cp.build_current_plan_provenance(source, enriched, transcript_version=1,
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
    """模型矛盾舍弃（与已选切片重叠）不进最终 discarded；切片优先。"""
    def callback(model, prompt):
        ids = ids_of(prompt)
        return cp._SlicePlanOut(
            video_summary="s", slice_count=1,
            slices=[cp._SliceOut(id=1, time_range=[cp._clock_of(0), cp._clock_of(len(ids) * 1000)],
                                 theme="完整内容")],
            discarded_segments=[cp._SliceDiscardOut(
                time_range=[cp._clock_of(0), cp._clock_of(3000)], reason="模型矛盾舍弃")])
    monkeypatch.setattr(cp, "_run_plan_agent", callback)
    record = cp.plan_clips(segments(1307), analysis(1307), transcript_version=1)
    assert record["discarded"] == []
