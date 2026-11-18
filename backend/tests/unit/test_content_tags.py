"""内容意图（患者决策链）规则分类契约测试（纯逻辑，不依赖数据库）。"""
from __future__ import annotations

from src.content_tags import INTENT_PRIORITY, INTENT_VOCAB, classify_intent


def test_intent_vocabulary_is_patient_decision_chain():
    assert set(INTENT_VOCAB) == {"症状识别", "就医决策", "方案解释", "误区辟谣",
                                 "就医指南", "临床研究", "预防生活方式"}


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
