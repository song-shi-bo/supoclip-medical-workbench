"""问答化净化重组（clip_purify，P1）契约测试。

核心契约：
* 纯应答段删除：整段（去标点后）仅由应答词重复构成的分段被删，产 sub_ranges；
* 开头吸附：前导纯应答自然被删（首段吸附到下一句实质内容）；
* 尾段永不删（保留结论收尾）；删除占比超限 → 整片回退（防掏空）；
* noop（无应答段）不产 sub_ranges，向后兼容单区间渲染；
* 说话人标注缺失 → 降级纯应答词 heuristic；标注指纹进 plan 指纹；
* 提示词A/B 与只删不改契约不受影响（本模块只删纯应答段，医生话语原文原序）。
"""

from __future__ import annotations

import pytest

from src import clip_planning as cp
from src import clip_purify as pu


def seg(i: int, text: str, s_ms: int, e_ms: int) -> dict:
    return {"segment_id": f"seg_{i:04d}", "start_ms": s_ms, "end_ms": e_ms, "text": text}


def _segments() -> list[dict]:
    """8 段：医生成段解释夹着应答碎片与寒暄确认。"""
    rows = [
        (0, "咱们先说说这个黑色素瘤的复查安排。", 0, 6000),
        (1, "对。", 6000, 7000),                      # 纯应答 → 删
        (2, "复查第一年是每三个月一次。", 7000, 14000),
        (3, "嗯嗯。", 14000, 15000),                  # 纯应答 → 删
        (4, "第二年变成每半年一次。", 15000, 22000),
        (5, "对对对。", 22000, 23500),                # 纯应答 → 删
        (6, "五年以后就一年查一次就可以了。", 23500, 30000),
        (7, "好的好的。", 30000, 31000),              # 尾段纯应答 → 保留（尾段永不删）
    ]
    return [seg(i, t, s, e) for i, t, s, e in rows]


# ---------------------------------------------------------------------------
# 纯应答判定
# ---------------------------------------------------------------------------

def test_is_pure_response_recognizes_repeats_and_words() -> None:
    assert pu.is_pure_response("对。") is True
    assert pu.is_pure_response("嗯嗯，对对对。") is True
    assert pu.is_pure_response("好好好的。") is True
    assert pu.is_pure_response("  ") is True
    assert pu.is_pure_response("复查第一年是每三个月一次。") is False
    assert pu.is_pure_response("对的，但是要看厚度。") is False


# ---------------------------------------------------------------------------
# 单切片净化
# ---------------------------------------------------------------------------

def test_purify_clip_drops_responses_and_builds_sub_ranges() -> None:
    clip = {"clip_id": "clip_a", "start_segment_id": "seg_0000", "end_segment_id": "seg_0007"}
    entries = [{"segment_id": s["segment_id"], "start_ms": s["start_ms"],
                "end_ms": s["end_ms"], "text": s["text"]} for s in _segments()]
    position_of = {e["segment_id"]: i for i, e in enumerate(entries)}
    result = pu.purify_clip(clip, entries, position_of, None)
    dropped = result["dropped_segment_ids"]
    assert dropped == ["seg_0001", "seg_0003", "seg_0005"]
    # kept = [0,2,4,6,7]；位置 1/3/5 被删 → 4 个连续 run
    ranges = result["sub_ranges"]
    assert ranges and len(ranges) == 4
    assert ranges[0]["start_segment_id"] == "seg_0000"
    assert ranges[1]["start_segment_id"] == "seg_0002"
    assert ranges[2]["start_segment_id"] == "seg_0004"
    assert ranges[3]["start_segment_id"] == "seg_0006"
    assert ranges[3]["end_segment_id"] == "seg_0007"  # 尾段应答保留 → 并入末区间


def test_purify_noop_when_no_response_segments() -> None:
    rows = [
        (0, "黑色素瘤的分期看厚度。", 0, 8000),
        (1, "分期决定治疗方案。", 8000, 16000),
    ]
    entries = [{"segment_id": f"seg_{i:04d}", "start_ms": s, "end_ms": e, "text": t}
               for i, t, s, e in rows]
    position_of = {e["segment_id"]: i for i, e in enumerate(entries)}
    result = pu.purify_clip({"start_segment_id": "seg_0000", "end_segment_id": "seg_0001"},
                            entries, position_of, None)
    assert result["mode"] == "noop"
    assert result["sub_ranges"] is None


def test_purify_fallback_when_drop_ratio_exceeds_floor() -> None:
    rows = [
        (0, "正式内容只有一句。", 0, 5000),
        (1, "对对对。", 5000, 8000),
        (2, "嗯嗯嗯。", 8000, 11000),
        (3, "好好好。", 11000, 14000),
        (4, "是的是的。", 14000, 17000),
        (5, "行行行。", 17000, 20000),
        (6, "哦哦哦。", 20000, 23000),
    ]
    entries = [{"segment_id": f"seg_{i:04d}", "start_ms": s, "end_ms": e, "text": t}
               for i, t, s, e in rows]
    position_of = {e["segment_id"]: i for i, e in enumerate(entries)}
    result = pu.purify_clip({"start_segment_id": "seg_0000", "end_segment_id": "seg_0006"},
                            entries, position_of, None)
    assert result["sub_ranges"] is None
    assert "keep_ratio" in str(result["fallback_reason"])


def test_purify_tail_segment_never_dropped() -> None:
    entries = [{"segment_id": f"seg_{i:04d}", "start_ms": i * 5000, "end_ms": (i + 1) * 5000,
                "text": "好的好的" if i == 2 else f"医生第{i}句完整解释。"} for i in range(3)]
    position_of = {e["segment_id"]: i for i, e in enumerate(entries)}
    result = pu.purify_clip({"start_segment_id": "seg_0000", "end_segment_id": "seg_0002"},
                            entries, position_of, None)
    assert "seg_0002" not in result["dropped_segment_ids"]


# ---------------------------------------------------------------------------
# 说话人纯度与身份
# ---------------------------------------------------------------------------

def test_doctor_ratio_reported_with_overlay_labels() -> None:
    entries = [{"segment_id": f"seg_{i:04d}", "start_ms": i * 5000, "end_ms": (i + 1) * 5000,
                "text": f"医生第{i}句完整解释。"} for i in range(4)]
    position_of = {e["segment_id"]: i for i, e in enumerate(entries)}
    labels = {"seg_0000": "医生", "seg_0001": "患者", "seg_0002": "医生", "seg_0003": "医生"}
    result = pu.purify_clip({"start_segment_id": "seg_0000", "end_segment_id": "seg_0003"},
                            entries, position_of, labels)
    assert result["doctor_ratio"] == 0.75


def test_identity_changes_with_speaker_fingerprint() -> None:
    a = pu.identity(None)
    b = pu.identity("abc" * 20)
    assert a["speaker_source"] == "none"
    assert b["speaker_source"] == "overlay"
    assert a["speaker_fingerprint"] != b["speaker_fingerprint"]


# ---------------------------------------------------------------------------
# 与 plan_clips 集成：sub_ranges 进计划 + 指纹纳入 purify 身份
# ---------------------------------------------------------------------------

def _analysis() -> dict:
    return {"keywords": [], "golden_quotes": [], "topic_units": [
        {"title": "复查安排", "segment_ids": [f"seg_{i:04d}" for i in range(8)],
         "info_seconds": 31}], "candidate_intervals": []}


def test_plan_clips_attaches_sub_ranges(monkeypatch: pytest.MonkeyPatch,
                                        tmp_path) -> None:
    from src import llm_budget
    monkeypatch.setattr(llm_budget, "_ledger_root", lambda: tmp_path / "ledger")
    segments = _segments()
    monkeypatch.setattr(cp, "_model_ready", lambda: (True, "openai:test-model", ""))

    def fake_agent(model, prompt):
        return cp._SlicePlanOut.model_validate({
            "video_summary": "", "slice_count": 1,
            "slices": [{"id": 1, "time_range": ["00:00:01", "00:00:31"],
                        "theme": "复查安排", "hook_first_line": "", "cover_text": "",
                        "content_summary": "复查频率安排", "completeness_note": "",
                        "risk_flags": []}],
            "discarded_segments": [],
        })

    monkeypatch.setattr(cp, "_run_plan_agent", fake_agent)
    labels = {s["segment_id"]: "医生" for s in segments}
    fp = pu.speaker_overlay_fingerprint(labels)
    record = cp.plan_clips(segments, _analysis(), speaker_labels=labels,
                           speaker_fingerprint=fp)
    assert record["purify"]["purified"] == 1
    clip = record["clips"][0]
    assert clip["sub_ranges"] and len(clip["sub_ranges"]) >= 1
    assert record["source_provenance"]["rules_snapshot"]["clip_purify"][
        "speaker_fingerprint"] == fp


def test_plan_fingerprint_changes_with_speaker_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    segments, analysis = _segments(), _analysis()
    monkeypatch.setattr(cp, "_model_ready", lambda: (True, "openai:test-model", ""))
    fp_a = pu.speaker_overlay_fingerprint({"seg_0000": "医生"})
    fp_b = pu.speaker_overlay_fingerprint({"seg_0000": "患者"})
    pa = cp.build_current_plan_provenance(segments, analysis, model="m",
                                          purify_identity=pu.identity(fp_a))
    pb = cp.build_current_plan_provenance(segments, analysis, model="m",
                                          purify_identity=pu.identity(fp_b))
    assert pa["input_fingerprint"] != pb["input_fingerprint"]
