"""内容标签维度契约测试（统一词表 + D2 意图 / D4 受众 / D5 形式规则，纯逻辑）。"""
from __future__ import annotations

from src.content_tags import (
    AUDIENCE_VOCAB,
    DIMENSION_VOCAB,
    FORMAT_VOCAB,
    INTENT_PRIORITY,
    INTENT_VOCAB,
    classify_audience,
    classify_format,
    classify_intent,
)


def test_dimension_vocab_is_unified_from_seed():
    """统一词表唯一来源：seed/tag_dimensions.json。"""
    assert set(DIMENSION_VOCAB) >= {"intent", "hook_style", "mechanism",
                                    "audience", "format", "topic"}
    assert list(INTENT_VOCAB) == DIMENSION_VOCAB["intent"]
    assert list(AUDIENCE_VOCAB) == DIMENSION_VOCAB["audience"]
    assert list(FORMAT_VOCAB) == DIMENSION_VOCAB["format"]
    assert set(INTENT_VOCAB) == {"症状识别", "就医决策", "方案解释", "误区辟谣",
                                 "就医指南", "临床研究", "预防生活方式"}


def test_intent_vocabulary_is_patient_decision_chain():
    assert INTENT_PRIORITY[0] == "临床研究"


def test_real_library_titles_classify_to_expected_intents():
    """用库里真实标题验证规则（来自热点库/标题库/成片台账）。"""
    cases = [
        ("肺结节是怎么一步步变成肺癌的？大约多少肺结节会变成肺癌", "症状识别"),
        ("什么人最容易得肺癌啊？", "症状识别"),
        ("天冷后，心脏最怕的10个'猝死瞬间'！", "症状识别"),
        ("专家门诊和特需门诊有什么区别？", "就医指南"),
        ("全国肾病前10家医院", "就医指南"),
        ("45岁以上体检，就查这几项！", "就医决策"),
        ("得了脂肪肝，应该怎么治？", "方案解释"),
        ("临床研究名额用完后用药安排", "临床研究"),
        ("很多中国老人是被错误的清淡饮食送走的", "误区辟谣"),
        ("掌握衰老的真相", "误区辟谣"),
        ("最好的养生 记住这4点", "预防生活方式"),
    ]
    for title, expected in cases:
        assert classify_intent(title) == expected, title


def test_unmatched_titles_return_empty():
    assert classify_intent("") == ""
    assert classify_intent("OK OK") == ""


def test_priority_breaks_ties_deterministically():
    # 同时命中临床研究与方案解释 → 临床研究（PRIORITY 首位）
    assert classify_intent("临床研究名额用完后用药安排") == "临床研究"
    assert INTENT_PRIORITY[0] == "临床研究"


def test_audience_rules_hit_real_titles():
    assert "儿童家长" in classify_audience("儿子手指变形，家长焦虑")
    assert "家属" in classify_audience("我爸是不是已经转移过了")
    assert "中老年" in classify_audience("很多中国老人是被错误的清淡饮食送走的")
    assert "高危人群" in classify_audience("什么人最容易得肺癌啊？")
    assert "随访复查患者" in classify_audience("45岁以上体检，就查这几项！")
    assert "治疗中患者" in classify_audience("免疫治疗的不良反应概率")
    assert classify_audience("") == []


def test_audience_max_three_and_deterministic():
    blob = "孩子确诊黑色素瘤，家长带他来复查，老人也跟着担心"
    tags = classify_audience(blob)
    assert 0 < len(tags) <= 3
    assert tags == classify_audience(blob)


def test_format_rules_hit_examples():
    assert classify_format("深度对话上海新华医院何继业医生") == "问诊实录"
    assert classify_format("普通人必会急救知识top 10") == "图文科普"
    assert classify_format("今天天气不错") == ""
