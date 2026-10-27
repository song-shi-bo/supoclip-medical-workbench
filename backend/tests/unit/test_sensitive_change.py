"""``backend/src/sensitive_change.py`` 的单元测试（纯合成数据，零真实素材）。

零网络、零模型、不连数据库；通过 ``--noconftest`` 运行时自行把 backend 根
目录加入 sys.path，不依赖任何 conftest。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from src.sensitive_change import detect_sensitive_changes  # noqa: E402


def _seg(segment_id: str, text: str, **extra: object) -> dict:
    """构造合成输入段（可携带 start_ms/end_ms 等只读附加字段）。"""
    segment: dict = {"segment_id": segment_id, "text": text}
    segment.update(extra)
    return segment


def test_medication_term_appearance_and_removal() -> None:
    """药物 A/B：before 有 after 无 -> 移除；反之 -> 出现。"""
    before = [_seg("s1", "患者服用药甲治疗。"), _seg("s2", "继续药乙方案。")]
    after = [_seg("s1", "患者服用药乙治疗。"), _seg("s2", "继续药丙方案。")]

    result = detect_sensitive_changes(before, after, medical_terms=["药甲", "药乙", "药丙"])

    assert result["validation_errors"] == []
    flags = result["risk_flags"]
    observed = [(f["segment_id"], f["rule_id"]) for f in flags]
    assert observed == [
        ("s1", "med_term_removal"),
        ("s1", "med_term_appearance"),
        ("s2", "med_term_removal"),
        ("s2", "med_term_appearance"),
    ]
    assert all(f["category"] == "medication_term_change" for f in flags)
    assert all(f["requires_review"] is True for f in flags)
    # 摘录保留差异词上下文。
    assert "药甲" in flags[0]["before"]
    assert "药乙" in flags[1]["after"]
    assert result["summary"]["coverage"] == {"medication": "enabled"}
    assert result["summary"]["flags_by_category"] == {"medication_term_change": 4}
    assert result["summary"]["segments_compared"] == 2


def test_dose_change_and_case_insensitive() -> None:
    """剂量 token 集合差异 -> dose_delta；单位大小写不敏感不误报。"""
    before = [_seg("d1", "每日口服 50mg，一日 3次。")]
    after = [_seg("d1", "每日口服 100mg，一日 3次。")]

    result = detect_sensitive_changes(before, after)

    assert result["validation_errors"] == []
    assert len(result["risk_flags"]) == 1
    flag = result["risk_flags"][0]
    assert flag["category"] == "dose_change"
    assert flag["rule_id"] == "dose_delta"
    assert flag["requires_review"] is True
    assert flag["diff_tokens"] == ["100mg", "50mg"]
    assert result["summary"]["flags_by_category"] == {"dose_change": 1}

    # 50Mg 与 50mg 归一化为同一 token，不产 flag。
    same = detect_sensitive_changes(
        [_seg("d2", "剂量 50Mg 每日")],
        [_seg("d2", "剂量 50mg 每日")],
    )
    assert same["risk_flags"] == []
    assert same["validation_errors"] == []


def test_negation_appearance_and_disappearance() -> None:
    """否定词在 after 出现 -> appearance；在 before 消失 -> disappearance。"""
    before = [_seg("n1", "发热三天。")]
    after = [_seg("n1", "无发热，否认咳嗽。")]

    appear = detect_sensitive_changes(before, after)
    assert [f["rule_id"] for f in appear["risk_flags"]] == [
        "negation_appearance",
        "negation_appearance",
    ]
    assert all(f["category"] == "negation_change" for f in appear["risk_flags"])

    before2 = [_seg("n2", "未见皮疹。")]
    after2 = [_seg("n2", "皮疹。")]
    disappear = detect_sensitive_changes(before2, after2)
    # 「未见」同时命中「未」与「未见」两个表内词，各自独立计数。
    assert sorted(f["rule_id"] for f in disappear["risk_flags"]) == [
        "negation_disappearance",
        "negation_disappearance",
    ]


def test_uncertain_caller_marked() -> None:
    """显式传入的 uncertain id -> caller_marked_uncertain。"""
    before = [_seg("u1", "此处听诊表述存疑。")]
    after = [_seg("u1", "此处听诊表述存疑。")]

    result = detect_sensitive_changes(before, after, uncertain_segment_ids=["u1"])

    assert result["validation_errors"] == []
    assert len(result["risk_flags"]) == 1
    flag = result["risk_flags"][0]
    assert flag["segment_id"] == "u1"
    assert flag["category"] == "uncertain"
    assert flag["rule_id"] == "caller_marked_uncertain"
    assert flag["requires_review"] is True
    assert result["summary"]["flags_by_category"] == {"uncertain": 1}
    assert result["summary"]["segments_compared"] == 1


def test_punctuation_only_change_no_flags() -> None:
    """正常标点变化不误报。"""
    before = [_seg("p1", "生命体征平稳，双肺呼吸音清。")]
    after = [_seg("p1", "生命体征平稳！双肺呼吸音清。")]

    result = detect_sensitive_changes(before, after)

    assert result["risk_flags"] == []
    assert result["validation_errors"] == []
    assert result["summary"]["flags_by_category"] == {}
    assert result["summary"]["segments_compared"] == 1


def test_identical_inputs_zero_flags() -> None:
    """输入完全一致 -> 零 flag。"""
    segments = [_seg("a1", "规律服用药甲。"), _seg("a2", "每日 1片。")]

    result = detect_sensitive_changes(list(segments), list(segments), medical_terms=["药甲"])

    assert result["risk_flags"] == []
    assert result["validation_errors"] == []
    assert result["summary"]["segments_compared"] == 2
    assert result["summary"]["flags_by_category"] == {}
    assert result["summary"]["coverage"] == {"medication": "enabled"}


def test_medical_terms_missing_or_empty_skips_with_coverage() -> None:
    """medical_terms 缺省/空列表 -> 跳过药物规则并在 coverage 标注。"""
    before = [_seg("m1", "服用药甲。")]
    after = [_seg("m1", "服用药乙。")]

    default_run = detect_sensitive_changes(before, after)
    assert default_run["risk_flags"] == []
    assert default_run["summary"]["coverage"] == {"medication": "skipped_no_terms"}

    empty_run = detect_sensitive_changes(before, after, medical_terms=[])
    assert empty_run["risk_flags"] == []
    assert empty_run["summary"]["coverage"] == {"medication": "skipped_no_terms"}


def test_both_empty_inputs_valid() -> None:
    """两个输入都为空列表属合法输入：零 flag、零校验错误。"""
    result = detect_sensitive_changes([], [])

    assert result == {
        "risk_flags": [],
        "summary": {
            "segments_compared": 0,
            "flags_by_category": {},
            "coverage": {"medication": "skipped_no_terms"},
        },
        "validation_errors": [],
    }


def test_duplicate_segment_id_blocks_flags() -> None:
    """同列表内重复 id -> validation_errors 且不猜测任何段落。"""
    before = [_seg("s1", "修改前文本。"), _seg("s1", "重复出现的段落。")]
    after = [_seg("s1", "修改后文本。")]

    result = detect_sensitive_changes(before, after)

    assert result["risk_flags"] == []
    duplicates = [
        entry for entry in result["validation_errors"]
        if entry["kind"] == "duplicate_segment_id"
    ]
    assert len(duplicates) == 1
    assert duplicates[0]["segment_id"] == "s1"
    assert duplicates[0]["side"] == "before"
    assert result["summary"]["segments_compared"] == 0


def test_missing_on_one_side() -> None:
    """id 单侧缺失 -> missing_in_after / missing_in_before。"""
    only_before = detect_sensitive_changes(
        [_seg("keep", "一致。"), _seg("gone", "仅在前段。")],
        [_seg("keep", "一致。")],
    )
    assert only_before["risk_flags"] == []
    assert {"kind": "missing_in_after", "segment_id": "gone"} in only_before["validation_errors"]

    only_after = detect_sensitive_changes(
        [_seg("keep", "一致。")],
        [_seg("keep", "一致。"), _seg("new", "仅在后段。")],
    )
    assert only_after["risk_flags"] == []
    assert {"kind": "missing_in_before", "segment_id": "new"} in only_after["validation_errors"]


def test_uncertain_id_not_found() -> None:
    """uncertain 名单中不存在的 id -> uncertain_id_not_found。"""
    result = detect_sensitive_changes(
        [_seg("ok", "一致。")],
        [_seg("ok", "一致。")],
        uncertain_segment_ids=["ghost"],
    )

    assert result["risk_flags"] == []
    assert {
        "kind": "uncertain_id_not_found", "segment_id": "ghost",
    } in result["validation_errors"]


def test_inputs_deep_equal_after_call() -> None:
    """调用前后输入深相等（含 start_ms/end_ms 等附加字段）。"""
    before = [
        {"segment_id": "i1", "text": "服用药甲 50mg，无不适。", "start_ms": 0, "end_ms": 1500},
    ]
    after = [
        {"segment_id": "i1", "text": "改服药乙 100mg。", "start_ms": 0, "end_ms": 1500},
    ]
    before_snapshot = copy.deepcopy(before)
    after_snapshot = copy.deepcopy(after)

    result = detect_sensitive_changes(
        before,
        after,
        medical_terms=["药甲", "药乙"],
        uncertain_segment_ids=["i1"],
    )

    assert before == before_snapshot
    assert after == after_snapshot
    categories = {f["category"] for f in result["risk_flags"]}
    assert categories == {
        "medication_term_change",
        "dose_change",
        "negation_change",
        "uncertain",
    }


def test_dose_units_and_negation_words_overrides() -> None:
    """keyword-only 覆盖参数生效；不传参时保持默认语义。"""
    before = [_seg("o1", "每日 2片，另 50mg。")]
    after = [_seg("o1", "每日 3片，另 50mg。")]

    default_run = detect_sensitive_changes(before, after)
    assert len(default_run["risk_flags"]) == 1
    assert default_run["risk_flags"][0]["diff_tokens"] == ["2片", "3片"]

    narrow_run = detect_sensitive_changes(before, after, dose_units=["片"])
    assert len(narrow_run["risk_flags"]) == 1
    assert narrow_run["risk_flags"][0]["diff_tokens"] == ["2片", "3片"]

    mg_only = detect_sensitive_changes(
        [_seg("o2", "剂量 2片。")],
        [_seg("o2", "剂量 3片。")],
        dose_units=["mg"],
    )
    assert mg_only["risk_flags"] == []

    before_neg = [_seg("o3", "症状缓解。")]
    after_neg = [_seg("o3", "症状加重。")]
    assert detect_sensitive_changes(before_neg, after_neg)["risk_flags"] == []
    override_run = detect_sensitive_changes(before_neg, after_neg, negation_words=["缓解"])
    assert [f["rule_id"] for f in override_run["risk_flags"]] == ["negation_disappearance"]


def test_flag_schema_and_excerpt_truncation() -> None:
    """flag 固定六键；长文本摘录截断并保留差异词上下文。"""
    filler = "一般情况可。" * 12
    before = [_seg("t1", filler + "服用药甲。")]
    after = [_seg("t1", filler + "停用前药。")]

    result = detect_sensitive_changes(before, after, medical_terms=["药甲"])

    assert len(result["risk_flags"]) == 1
    flag = result["risk_flags"][0]
    assert set(flag) == {
        "segment_id", "category", "rule_id", "before", "after", "requires_review",
    }
    assert flag["requires_review"] is True
    assert "药甲" in flag["before"]
    assert len(flag["before"]) <= 50
    assert flag["before"].startswith("…")
    assert flag["after"].endswith("…")
