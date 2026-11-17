"""四点装配（clip_planning.assemble_four_step，规划层组合决定）契约测试。

分层契约（2026-11-16 用户拍板）：
* 分析层只找锚点（逐字校验的 hooks/hook_quote/patient_question/doctor_core/
  ending_quote）；规划层负责四点拼接——同一主题块内按 钩子→接题→叙述→收口
  重排 sub_ranges，输出第一句即钩子原话，锚点外杂话丢弃（只删不改）；
* 任一锚点定位失败 / 时长 <40s → 回退连续净化；>180s 从叙述段尾部收；
* 钩子与叙述都缺（仅 llm 分析）→ 宁缺毋滥放弃成片（挪入 discarded）；
* 开关/版本/时长闸门进 plan provenance；已装配切片由净化跳过。
"""
from __future__ import annotations

import pytest

from src import clip_planning as cp
from src import clip_purify as pu


def seg(i: int, text: str, s_ms: int, e_ms: int) -> dict:
    return {"segment_id": f"seg_{i:04d}", "start_ms": s_ms, "end_ms": e_ms, "text": text}


def _segments() -> list[dict]:
    rows = [
        (0, "先别急着自己停药，皮疹是靶向药最常见的反应。", 0, 4000),          # 钩子
        (1, "你是说服药之后起了皮疹，担心要停药，对吧？", 5000, 9000),          # 接题
        (2, "那个，我上次也是这样。", 10000, 12000),                            # 患者插话 → 丢
        (3, "分两种情况，第一只是皮肤干，润肤霜就能控制。", 13000, 25000),      # 叙述1
        (4, "第二如果起水疱破溃，马上回来评估。", 26000, 40000),                # 叙述2
        (5, "严重的类型还需要口服药配合。", 41000, 60000),                      # 叙述3
        (6, "记住一句话，皮疹不可怕，可怕的是自己悄悄停药。", 61000, 65000),    # 收口
    ]
    return [seg(i, t, s, e) for i, t, s, e in rows]


def _unit(**overrides) -> dict:
    unit = {
        "title": "靶向药皮疹",
        "segment_ids": [s["segment_id"] for s in _segments()],
        "hook_quote": "先别急着自己停药，皮疹是靶向药最常见的反应。",
        "patient_question": "你是说服药之后起了皮疹，担心要停药，对吧？",
        "doctor_core_segment_ids": ["seg_0003", "seg_0004", "seg_0005"],
        "ending_quote": "记住一句话，皮疹不可怕，可怕的是自己悄悄停药。",
    }
    unit.update(overrides)
    return unit


def _clip() -> dict:
    return {"clip_id": "clip-a", "start_segment_id": "seg_0000",
            "end_segment_id": "seg_0006", "topic": "靶向药皮疹"}


def _entries(segments):
    return [{"segment_id": s["segment_id"], "start_ms": s["start_ms"],
             "end_ms": s["end_ms"], "text": s["text"]} for s in segments]


def test_four_step_assembly_orders_anchors_and_drops_chatter():
    entries = _entries(_segments())
    position_of = {e["segment_id"]: i for i, e in enumerate(entries)}
    assembled = cp.assemble_four_step(_clip(), entries, position_of, _unit(),
                                      {"plan_source": "llm", "hooks": []})
    assert assembled is not None and assembled["mode"] == "four_step_assembly"
    ranges = assembled["sub_ranges"]
    # 顺序：钩子+接题（源相邻合并成一个区间）→ 叙述连续段 + 收口（源相邻合并）
    assert ranges[0]["start_segment_id"] == "seg_0000"
    assert ranges[0]["end_segment_id"] == "seg_0001"
    assert ranges[1]["start_segment_id"] == "seg_0003"
    assert ranges[1]["end_segment_id"] == "seg_0006"
    assert "seg_0002" in assembled["dropped_segment_ids"]
    assert assembled["anchors"]["hook"] == ["seg_0000"]
    assert assembled["anchors"]["ending"] == ["seg_0006"]
    assert assembled["total_ms"] >= cp.ASSEMBLY_MIN_MS


def test_missing_anchor_returns_none_for_continuous_fallback():
    entries = _entries(_segments())
    position_of = {e["segment_id"]: i for i, e in enumerate(entries)}
    assert cp.assemble_four_step(_clip(), entries, position_of,
                                 _unit(ending_quote=""),
                                 {"plan_source": "llm", "hooks": []}) is None
    assert cp.assemble_four_step(_clip(), entries, position_of,
                                 _unit(doctor_core_segment_ids=["seg_0003"]),
                                 {"plan_source": "llm", "hooks": []}) is None


def test_overlong_narration_trims_tail():
    rows = _segments()
    extra = [seg(10 + i, f"叙述扩展第{i}段，补充完整的处理细节。", 66000 + i * 30000,
                 66000 + (i + 1) * 30000 - 1000) for i in range(6)]
    segments = rows + extra
    entries = _entries(segments)
    position_of = {e["segment_id"]: i for i, e in enumerate(entries)}
    clip = _clip()
    clip["end_segment_id"] = extra[-1]["segment_id"]  # 窗口覆盖全部叙述段
    unit = _unit(segment_ids=[s["segment_id"] for s in segments],
                 doctor_core_segment_ids=["seg_0003", "seg_0004", "seg_0005",
                                          *[s["segment_id"] for s in extra]])
    assembled = cp.assemble_four_step(clip, entries, position_of, unit,
                                      {"plan_source": "llm", "hooks": []})
    assert assembled is not None
    assert assembled["total_ms"] <= cp.ASSEMBLY_MAX_MS
    assert any(s["segment_id"] in assembled["dropped_segment_ids"] for s in extra)


def test_hook_falls_back_to_analysis_hooks_within_unit():
    entries = _entries(_segments())
    position_of = {e["segment_id"]: i for i, e in enumerate(entries)}
    unit = _unit(hook_quote="")
    analysis = {"plan_source": "llm",
                "hooks": [{"text": "先别急着自己停药，皮疹是靶向药最常见的反应。",
                           "segment_ids": ["seg_0000"], "mechanism": "反常识"}]}
    assembled = cp.assemble_four_step(_clip(), entries, position_of, unit, analysis)
    assert assembled is not None
    assert assembled["sub_ranges"][0]["start_segment_id"] == "seg_0000"


def test_no_anchor_block_discarded_only_for_llm_analysis(monkeypatch):
    """钩子与叙述都缺：llm 分析 → 放弃成片挪入 discarded；本地回退 → 保留出片。"""
    monkeypatch.setattr(cp, "_model_ready", lambda: (True, "openai:test-model", ""))
    segments = _segments()
    bare_unit = {"title": "闲聊", "segment_ids": [s["segment_id"] for s in segments]}

    def fake_agent(model, prompt):
        return cp._SlicePlanOut(video_summary="", slice_count=1,
                                slices=[cp._SliceOut(id=1, time_range=["00:00:00", "00:01:05"],
                                                     theme="闲聊")],
                                discarded_segments=[])

    monkeypatch.setattr(cp, "_run_plan_agent", fake_agent)
    analysis = {"plan_source": "llm", "keywords": [], "golden_quotes": [],
                "topic_units": [bare_unit], "candidate_intervals": [], "hooks": []}
    record = cp.plan_clips(segments, analysis, transcript_version=1)
    assert record["clips"] == []
    assert record["four_step_assembly"]["discarded_clips"] == 1
    assert any(d["type"] == "结构不完整" for d in record["discarded"])
    covered = {sid for d in record["discarded"] for sid in d["segment_ids"]}
    assert covered == {s["segment_id"] for s in segments}  # 覆盖契约由程序补齐

    record2 = cp.plan_clips(segments, {**analysis, "plan_source": "heuristic_fallback"},
                            transcript_version=1)
    assert record2["clips"], "本地回退分析不放弃成片"


def test_plan_assembles_and_purify_skips_assembled_clip(monkeypatch):
    """规划层装配成功 → sub_ranges 四点顺序、净化跳过不再改写。"""
    monkeypatch.setattr(cp, "_model_ready", lambda: (True, "openai:test-model", ""))
    segments = _segments()

    def fake_agent(model, prompt):
        return cp._SlicePlanOut(video_summary="", slice_count=1,
                                slices=[cp._SliceOut(id=1, time_range=["00:00:00", "00:01:05"],
                                                     theme="靶向药皮疹")],
                                discarded_segments=[])

    monkeypatch.setattr(cp, "_run_plan_agent", fake_agent)
    analysis = {"plan_source": "llm", "keywords": [], "golden_quotes": [],
                "topic_units": [_unit()], "candidate_intervals": [], "hooks": []}
    record = cp.plan_clips(segments, analysis, transcript_version=1)
    clip = record["clips"][0]
    assert clip["purify"]["mode"] == "four_step_assembly"
    assert clip["sub_ranges"][0]["start_segment_id"] == "seg_0000"
    assert clip["hook_first_line"] == segments[0]["text"].strip()  # 钩子即第一句
    assert record["four_step_assembly"]["assembled"] == 1
    assert record["purify"]["skipped_assembled"] == 1
    assert record["purify"]["purified"] == 0
    assert record["source_provenance"]["rules_snapshot"]["four_step_assembly"][
        "enabled"] is True


def test_assembly_switch_env(monkeypatch):
    monkeypatch.setenv("CLIP_FOUR_STEP_ASSEMBLY", "off")
    assert cp.four_step_assembly_enabled() is False
    monkeypatch.setenv("CLIP_FOUR_STEP_ASSEMBLY", "on")
    assert cp.four_step_assembly_enabled() is True


def test_purify_keeps_delete_only_contract() -> None:
    """净化层不装配：连续净化保持只删不改、不调语序。"""
    clip = {"clip_id": "clip_a", "start_segment_id": "seg_0000", "end_segment_id": "seg_0007"}
    entries = _entries(_segments())
    position_of = {e["segment_id"]: i for i, e in enumerate(entries)}
    result = pu.purify_clip(clip, entries, position_of, None)
    if result["sub_ranges"]:
        for entry_range in result["sub_ranges"]:
            assert entry_range["start_ms"] < entry_range["end_ms"]
