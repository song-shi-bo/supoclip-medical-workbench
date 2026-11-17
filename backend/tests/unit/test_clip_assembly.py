"""四点装配（clip_purify.assemble_four_step）契约测试（2026-11-16 用户拍板）。

核心契约：
* 同一主题块内 钩子→接题→叙述→收口 锚点段按四点顺序重排 sub_ranges；
  输出第一句即钩子原话；锚点外的杂话段全部丢弃（只删不改，绝不发明原话）；
* 任一锚点定位失败 / 时长 <40s → 回退既有连续净化；>180s 从叙述段尾部收；
* 钩子与叙述锚点都缺（且分析来源为 llm）→ 宁缺毋滥放弃成片（规划层挪入 discarded）；
* 装配开关/版本进 purify 身份，缓存可复现。
"""
from __future__ import annotations

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


def _run(unit, *, plan_source="llm", segments=None):
    segments = segments or _segments()
    clips = [_clip()]
    summary = pu.purify_clips(clips, segments, None,
                              analysis={"topic_units": [unit], "plan_source": plan_source})
    return summary, clips[0]


def test_four_step_assembly_orders_anchors_and_drops_chatter():
    summary, clip = _run(_unit())
    assert summary["assembled"] == 1
    assert clip["purify"]["mode"] == "four_step_assembly"
    ranges = clip["sub_ranges"]
    # 顺序：钩子+接题（源相邻合并成一个区间）→ 叙述连续段 + 收口（源相邻合并）
    assert ranges[0]["start_segment_id"] == "seg_0000"
    assert ranges[0]["end_segment_id"] == "seg_0001"
    assert ranges[1]["start_segment_id"] == "seg_0003"
    assert ranges[1]["end_segment_id"] == "seg_0006"
    order = [sid for r in ranges
             for sid in (r["start_segment_id"], r["end_segment_id"])]
    assert order.index("seg_0000") < order.index("seg_0003") < order.index("seg_0006")
    assert "seg_0002" in clip["purify"]["dropped_segment_ids"]
    anchors = clip["purify"]["assembly"]["anchors"]
    assert anchors["hook"] == ["seg_0000"] and anchors["ending"] == ["seg_0006"]
    assert clip["purify"]["assembly"]["total_ms"] >= pu.ASSEMBLY_MIN_MS


def test_missing_anchor_falls_back_to_continuous_purify():
    unit = _unit(ending_quote="")  # 缺收口 → 回退连续净化
    summary, clip = _run(unit)
    assert summary["assembled"] == 0
    assert clip["purify"]["mode"] != "four_step_assembly"
    assert not clip["purify"].get("assembly")


def test_short_total_duration_falls_back():
    unit = _unit(doctor_core_segment_ids=["seg_0003"])  # 只有 12s 叙述 → 总长 <40s
    summary, clip = _run(unit)
    assert summary["assembled"] == 0
    assert clip["purify"]["mode"] != "four_step_assembly"


def test_overlong_narration_trims_tail():
    rows = _segments()
    extra = [seg(10 + i, f"叙述扩展第{i}段，补充完整的处理细节。", 66000 + i * 30000,
                 66000 + (i + 1) * 30000 - 1000) for i in range(6)]
    long_segments = rows + extra
    unit = _unit(segment_ids=[s["segment_id"] for s in long_segments],
                 doctor_core_segment_ids=["seg_0003", "seg_0004", "seg_0005",
                                          *[s["segment_id"] for s in extra]])
    clip = _clip()
    clip["end_segment_id"] = extra[-1]["segment_id"]  # 窗口覆盖全部叙述段
    clips = [clip]
    summary = pu.purify_clips(clips, long_segments, None,
                              analysis={"topic_units": [unit], "plan_source": "llm"})
    assert summary["assembled"] == 1
    assert clips[0]["purify"]["assembly"]["total_ms"] <= pu.ASSEMBLY_MAX_MS
    dropped = clips[0]["purify"]["dropped_segment_ids"]
    assert any(s["segment_id"] in dropped for s in extra)


def test_hook_falls_back_to_analysis_hooks_within_unit():
    unit = _unit(hook_quote="")  # 主题块缺 hook_quote
    analysis = {"plan_source": "llm",
                "hooks": [{"text": "先别急着自己停药，皮疹是靶向药最常见的反应。",
                           "segment_ids": ["seg_0000"], "mechanism": "反常识"}],
                "topic_units": [unit]}
    clips = [_clip()]
    summary = pu.purify_clips(clips, _segments(), None, analysis=analysis)
    assert summary["assembled"] == 1
    assert clips[0]["sub_ranges"][0]["start_segment_id"] == "seg_0000"


def test_no_anchor_block_discarded_only_for_llm_analysis():
    bare = {"title": "闲聊", "segment_ids": [s["segment_id"] for s in _segments()]}
    _, clip_llm = _run(bare, plan_source="llm")
    assert clip_llm["purify"].get("discard") is True
    # 非模型分析（回退/旧工件）绝不放弃成片
    _, clip_local = _run(bare, plan_source="heuristic_fallback")
    assert clip_local["purify"].get("discard") is not True


def test_assembly_switch_and_identity_are_reproducible(monkeypatch):
    assert pu.identity(None)["four_step_assembly"] is True
    assert pu.identity(None)["assembly_version"] == pu.ASSEMBLY_VERSION
    assert pu.PURIFY_VERSION == "clip-purify/v3"
    monkeypatch.setenv("MEDICAL_PURIFY_FOUR_STEP", "off")
    import importlib
    importlib.reload(pu)
    assert pu.identity(None)["four_step_assembly"] is False
    monkeypatch.setenv("MEDICAL_PURIFY_FOUR_STEP", "on")
    importlib.reload(pu)
    assert pu.identity(None)["four_step_assembly"] is True
