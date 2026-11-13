"""医学背景注入（2026-11-13 设计稿）单元测试：开关/预算/防泄漏/降级/指纹路径。

不调用任何 LLM；快照用真实 active_snapshot.json（ melanoma v2，MEL 编号）。
"""
from __future__ import annotations

import pytest

from src import clip_planning as cp
from src import content_analysis as ca
from src import medical_kb


ANN = [
    {"title": "淋巴结肿大患者复查与手部变形情况答疑", "level": "strong", "score": 0.8,
     "segment_ids": ["seg_0000", "seg_0001"],
     "matched": [{"id": "MEL-016"}, {"id": "MEL-052"}, {"id": "MEL-031"}]},
    {"title": "术后扩切与PET-CT说明", "level": "strong", "score": 0.75,
     "segment_ids": ["seg_0003"],
     "matched": [{"id": "MEL-057"}, {"id": "MEL-016"}]},   # MEL-016 跨块去重
]


@pytest.fixture(autouse=True)
def _bg_default_off(monkeypatch):
    """默认 off：确保测试间环境互不影响。"""
    monkeypatch.delenv("MEDICAL_BG_INJECT", raising=False)
    monkeypatch.delenv("MEDICAL_BG_MAX_ENTRIES", raising=False)
    monkeypatch.delenv("MEDICAL_BG_PER_UNIT", raising=False)
    monkeypatch.delenv("MEDICAL_BG_BUDGET", raising=False)
    monkeypatch.delenv("MEDICAL_BG_LEAK_THRESHOLD", raising=False)


def test_background_config_defaults_off_and_invalid_is_off(monkeypatch):
    cfg = medical_kb.background_config({})
    assert cfg["inject"] is False and cfg["max_entries"] == 9 and cfg["per_unit"] == 3
    monkeypatch.setenv("MEDICAL_BG_INJECT", "on")
    cfg = medical_kb.background_config()
    assert cfg["inject"] is True
    monkeypatch.setenv("MEDICAL_BG_INJECT", "banana")   # 非法值 → off
    assert medical_kb.background_config()["inject"] is False


def test_build_background_block_off_returns_empty():
    assert medical_kb.build_background_block(ANN) == ("", [], [])


def test_build_background_block_on_builds_controlled_block(monkeypatch):
    monkeypatch.setenv("MEDICAL_BG_INJECT", "on")
    block, statements, ids = medical_kb.build_background_block(ANN)
    assert block, "开启后必须产出背景块"
    assert medical_kb._BG_HEADER in block                     # 块首禁止复制指令
    assert "MEL-016" in block and "MEL-052" in block
    assert "边界提示" in block                                 # risk_boundary 白名单字段
    # 跨块去重：MEL-016 只注入一次；每块 per_unit=3 上限
    assert ids == ["MEL-016", "MEL-052", "MEL-031", "MEL-057"]
    assert len(statements) == len(ids)
    # statements 与块内容一致（供泄漏扫描比对）
    assert statements[0].startswith("")


def test_build_background_block_budget_truncation(monkeypatch):
    monkeypatch.setenv("MEDICAL_BG_INJECT", "on")
    monkeypatch.setenv("MEDICAL_BG_BUDGET", "260")            # 小预算：高分单元在前、预算尽即停
    block, statements, ids = medical_kb.build_background_block(ANN)
    assert len(block) <= 260                                  # 块总长不超预算
    assert ids and ids[0] == "MEL-016"                        # 命中序不变（高分在前）
    assert len(ids) < 4                                       # 相比全量 4 条发生截断
    assert medical_kb._BG_HEADER in block


def test_build_background_block_unknown_ids_degrade(monkeypatch):
    monkeypatch.setenv("MEDICAL_BG_INJECT", "on")
    anns = [{"title": "t", "matched": [{"id": "NOPE-404"}]}]
    assert medical_kb.build_background_block(anns) == ("", [], [])


def test_analysis_context_lines_inject_toggle(monkeypatch):
    analysis = {"medical_annotations": ANN}
    monkeypatch.setenv("MEDICAL_BG_INJECT", "on")
    lines_on = cp._analysis_context_lines(analysis, 4)
    assert any("医学背景" in ln for ln in lines_on)
    monkeypatch.delenv("MEDICAL_BG_INJECT")
    lines_off = cp._analysis_context_lines(analysis, 4)
    assert not any("医学背景" in ln for ln in lines_off)       # off 与现状一致


def test_slice_plan_prompt_inject_toggle(monkeypatch):
    segs = [
        {"segment_id": "seg_0000", "start_ms": 0, "end_ms": 4000, "text": "第一句话。"},
        {"segment_id": "seg_0001", "start_ms": 4000, "end_ms": 8000, "text": "第二句话。"},
    ]
    index = ca._SegIndex(segs)
    analysis = {"medical_annotations": ANN}
    monkeypatch.setenv("MEDICAL_BG_INJECT", "on")
    prompt_on = cp._slice_plan_prompt(index, analysis)
    assert "医学背景" in prompt_on
    monkeypatch.delenv("MEDICAL_BG_INJECT")
    assert "医学背景" not in cp._slice_plan_prompt(index, analysis)
    # 确定性：同输入两次构建逐字节一致（expected/actual 指纹对称的基础）
    monkeypatch.setenv("MEDICAL_BG_INJECT", "on")
    assert cp._slice_plan_prompt(index, analysis) == prompt_on


def test_max_common_run_and_leak_scan():
    assert cp._max_common_run("黑色素瘤治疗指南全文", "黑色素瘤治疗") == 6
    assert cp._max_common_run("abc", "xyz") == 0
    stmts = ["皮损在短期内出现明显变化时，应由专业人员评估检查判断"]
    # 拷贝 ≥12 字 → 告警
    clips = [{"topic": "可疑皮损就医判断", "hook_first_line": "皮损在短期内出现明显变化时，应由专业人员评估检查判断"}]
    warns = cp._scan_background_leak(clips, stmts, 12)
    assert len(warns) == 1 and "medical_background_leak" in warns[0]
    # 短重合（巧合）不告警
    clips_short = [{"topic": "痣的变化", "hook_first_line": "应由专业人员评估"}]
    assert cp._scan_background_leak(clips_short, stmts, 12) == []
    # statements 空 → 无扫描
    assert cp._scan_background_leak(clips, [], 12) == []
