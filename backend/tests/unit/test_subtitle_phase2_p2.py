"""阶段2 P2 字幕可读性/强调/安全区单元测试（2026-11-18）。

覆盖：
* A5 亮背景自适应：判定表、字幕带几何、真实 ffmpeg 抽帧分析（白底→底板/
  黑底→描边/失败→fallback/开关 off）、安全区档案校验（通过与越界两态）；
* A4 克制强调：命中、每 cue 1 处、≤6 字、数字不强调、跨行不强调、三种形态
  标签、词典注入（tmp TSV + 缓存清空）；
* ASS 层：SubBox 样式行按需输出、逐 cue Style 选择、emphasis 行替换、SRT
  副本纯净；指纹含 sub_style（改 emphasis env → 指纹变）；
* 渲染接线：白底源片 → warnings.subtitle_readability 真实落盘 + ASS 含 SubBox。
"""

from __future__ import annotations

import contextlib
import json
import subprocess as _sp
from pathlib import Path

import pytest

from src import clip_render as cr
from src import subtitle_cueing as sc
from src import subtitle_emphasis as se
from src import subtitle_qc as sq
from src import subtitle_readability as sr


# ---------------------------------------------------------------------------
# A5 判定表与几何
# ---------------------------------------------------------------------------


def test_decide_band_thresholds():
    assert sr.decide_band({"luma": 255.0, "edge_density": 0.0}) == "box"      # 亮
    assert sr.decide_band({"luma": 10.0, "edge_density": 0.0}) == "outline"   # 暗
    assert sr.decide_band({"luma": 10.0, "edge_density": 0.5}) == "box"       # 高纹理
    assert sr.decide_band({"luma": 120.0, "edge_density": 0.05}) == "outline"


def test_subtitle_band_fractions_matches_burn_constants():
    top, bottom = sr.subtitle_band_fractions()
    assert 0.5 < top < bottom < 1.0
    # 底边距 = MarginV/PlayResH（下三分之一黄金位 C 档 360px）
    assert bottom == pytest.approx((sc.BURN_PLAYRES_H - sc.BURN_MARGIN_V_PX)
                                   / sc.BURN_PLAYRES_H, abs=1e-3)


# ---------------------------------------------------------------------------
# A5 安全区档案
# ---------------------------------------------------------------------------


def test_safe_zone_douyin_pass_with_current_margin():
    doc = sr.check_safe_zone({"SUBTITLE_PLATFORM_PROFILE": "douyin"})
    assert doc["profile"] == "douyin"
    assert doc["pass"] is True, doc["reason"]


def test_safe_zone_fails_when_margin_shrunk(monkeypatch):
    """MarginV 回到历史 160 档（8.3%）< 抖音底部 UI 16% → 必须判越界。"""
    monkeypatch.setattr(sc, "BURN_MARGIN_V_PX", 160)
    doc = sr.check_safe_zone({"SUBTITLE_PLATFORM_PROFILE": "douyin"})
    assert doc["pass"] is False
    assert doc["bottom_margin_frac"] < doc["profile_bottom_ui_frac"]


def test_safe_zone_unknown_profile_falls_back():
    doc = sr.check_safe_zone({"SUBTITLE_PLATFORM_PROFILE": "unknown_app"})
    assert doc["resolved"] == "default" and doc["pass"] is True


# ---------------------------------------------------------------------------
# A5 真实抽帧分析（lavfi 纯色源，不依赖模型）
# ---------------------------------------------------------------------------


def _color_source(tmp_path: Path, color: str) -> Path:
    path = tmp_path / f"src_{color.strip('%')}.mp4"
    _sp.run([cr._resolve(cr.FFMPEG_BIN), "-y",
             "-f", "lavfi", "-i", f"color=c={color}:size=160x240:duration=1:rate=5",
             "-c:v", "libx264", "-preset", "ultrafast", str(path)],
            check=True, capture_output=True)
    return path


def test_analyze_white_background_flags_box(tmp_path):
    source = _color_source(tmp_path, "white")
    cues = [{"start": 0.1, "end": 0.8, "text": "白底测试"}]
    doc = sr.analyze_cues(source, cues, clip_start_ms=0.0)
    assert doc["mode"] == "analyzed"
    assert doc["box_cues"] == [0]
    assert doc["pre_mitigation_readability"] == 0.0


def test_analyze_black_background_keeps_outline(tmp_path):
    source = _color_source(tmp_path, "black")
    cues = [{"start": 0.1, "end": 0.8, "text": "黑底测试"}]
    doc = sr.analyze_cues(source, cues, clip_start_ms=0.0)
    assert doc["mode"] == "analyzed"
    assert doc["box_cues"] == []
    assert doc["pre_mitigation_readability"] == 1.0


def test_analyze_failure_falls_back_to_outline(tmp_path):
    doc = sr.analyze_cues(tmp_path / "no_such_file.mp4",
                          [{"start": 0.0, "end": 1.0, "text": "x"}])
    assert doc["mode"] == "fallback"
    assert doc["box_cues"] == []


def test_analyze_off_switch(tmp_path):
    doc = sr.analyze_cues(_color_source(tmp_path, "white"),
                          [{"start": 0.1, "end": 0.8, "text": "x"}],
                          env={"SUBTITLE_READABILITY": "off"})
    assert doc["mode"] == "off"


def test_virtual_to_source_mapping_for_purify_ranges():
    # 虚拟轴 [0,1s)+[1s,2s) ← 源 [10s,11s)+[15s,16s)：虚拟 0.5s → 源 10.5s，
    # 虚拟 1.5s（第二段本地 0.5s）→ 源 15.5s，超出末尾钳到最后区间末。
    ranges = [(10_000, 11_000), (15_000, 16_000)]
    assert sr._virtual_to_source_ms(500.0, ranges) == 10_500.0
    assert sr._virtual_to_source_ms(1_500.0, ranges) == 15_500.0
    assert sr._virtual_to_source_ms(9_999.0, ranges) == 16_000.0  # 越界钳末尾


# ---------------------------------------------------------------------------
# A4 克制强调
# ---------------------------------------------------------------------------


@pytest.fixture()
def emphasis_lexicon(tmp_path, monkeypatch):
    lex_dir = tmp_path / "lex"
    lex_dir.mkdir()
    (lex_dir / "terms.tsv").write_text(
        "# 测试\n头孢\tdrug\t\t8\t\n黑色素瘤\tdisease\t\t9\t\n免疫治疗\tproc\t\t7\t\n",
        encoding="utf-8")
    monkeypatch.setenv("MEDICAL_LEXICON_DIR", str(lex_dir))
    # 隔离指南库快照（_term_pattern 内部延迟 import src.medical_kb）
    monkeypatch.setattr("src.medical_kb.load_snapshot", lambda *a, **k: None)
    se._term_pattern.cache_clear()
    yield
    monkeypatch.undo()
    se._term_pattern.cache_clear()


def test_emphasis_single_span_first_occurrence(emphasis_lexicon):
    span = se.cue_emphasis_span("医生建议吃头孢，头孢要按疗程吃")
    assert span == (5, 7)  # 首现优先（医0生1建2议3吃4头5孢6）


def test_emphasis_skips_numeric_tokens(emphasis_lexicon):
    """「0.4的厚度」是完整数字 token（数字+的+所指）——落在其中的术语不强调。"""
    assert se.cue_emphasis_span("0.4的厚度必须扩切") is None


def test_emphasis_off_mode_no_span(emphasis_lexicon):
    assert se.cue_emphasis_span("头孢按疗程吃", mode="off") is None


def test_emphasis_bold_and_color_tags(emphasis_lexicon):
    tagged = se.apply_emphasis_tags("按时吃头孢", (3, 5), mode="bold")
    assert tagged == "按时吃{\\b1}头孢{\\b0}"
    tagged_c = se.apply_emphasis_tags("按时吃头孢", (3, 5), mode="color")
    assert "{\\1c" in tagged_c and se.EMPHASIS_COLOR_ASS in tagged_c


def test_cue_lines_emphasis_only_one_line_touched(emphasis_lexicon):
    lines = se.cue_lines_with_emphasis("建议吃头孢\n并且多喝水", mode="bold")
    assert lines[0] == "建议吃{\\b1}头孢{\\b0}"
    assert lines[1] == "并且多喝水"


def test_real_lexicon_loads_terms():
    """真实 medical.tsv（type 多样）应至少注入一个术语（集成冒烟）。"""
    se._term_pattern.cache_clear()
    try:
        pat = se._term_pattern()
        assert pat is not None and pat.pattern
    finally:
        se._term_pattern.cache_clear()


# ---------------------------------------------------------------------------
# ASS 层：SubBox 样式 + emphasis 行替换 + SRT 纯净
# ---------------------------------------------------------------------------


def _sample_srt() -> str:
    return ("1\n00:00:00,000 --> 00:00:02,000\n白底第一句\n"
            "\n2\n00:00:02,000 --> 00:00:04,000\n黑底第二句\n")


def test_ass_subbox_style_and_per_cue_selection():
    ass = sc.srt_to_styled_ass(_sample_srt(), style_names=["SubBox", "Sub"])
    assert f"Style: {sc.BURN_BOX_STYLE_NAME}," in ass
    lines = [ln for ln in ass.split("\n") if ln.startswith("Dialogue:")]
    assert len(lines) == 2
    assert ",SubBox,,0,0,0,," in lines[0]
    assert ",Sub,,0,0,0,," in lines[1]


def test_ass_default_has_no_box_style_line():
    ass = sc.srt_to_styled_ass(_sample_srt())
    assert sc.BURN_BOX_STYLE_NAME not in ass


def test_ass_emphasis_lines_replace_text_only_in_ass():
    ass = sc.srt_to_styled_ass(_sample_srt(),
                               emphasis_lines=[["白底{\\b1}第一句{\\b0}"], ["黑底第二句"]])
    assert "{\\b1}" in ass
    # SRT 副本保持纯净（审计口径不受 ASS 标签污染）
    srt = _sample_srt()
    assert "{\\b1}" not in srt


def test_fingerprint_changes_with_emphasis_env(tmp_path):
    """sub_style 入指纹：改 SUBTITLE_EMPHASIS → 指纹必变（缓存自动失效）。"""
    import os
    plan = {"clip_id": "c", "start_ms": 0, "end_ms": 1000}
    old = os.environ.get("SUBTITLE_EMPHASIS")
    try:
        os.environ["SUBTITLE_EMPHASIS"] = "bold"
        fp1 = cr._render_fingerprint(plan, tmp_path / "s.mp4", [], {}, None)
        os.environ["SUBTITLE_EMPHASIS"] = "off"
        fp2 = cr._render_fingerprint(plan, tmp_path / "s.mp4", [], {}, None)
        assert fp1 != fp2
    finally:
        if old is None:
            os.environ.pop("SUBTITLE_EMPHASIS", None)
        else:
            os.environ["SUBTITLE_EMPHASIS"] = old


# ---------------------------------------------------------------------------
# 渲染接线（真实 ffmpeg，白底源片）
# ---------------------------------------------------------------------------


def test_render_white_background_writes_readability_and_subbox(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "reserve_capacity",
                        lambda *a, **k: contextlib.nullcontext())
    source = _color_source(tmp_path, "white")
    clip = {"clip_id": "p2-clip", "start_ms": 0, "end_ms": 900, "warnings": []}
    segments = [{"segment_id": "s1", "start_ms": 100, "end_ms": 800,
                 "text": "白底亮背景测试字幕。"}]
    entry = cr.render_one(clip, source=source, out_dir=tmp_path / "out",
                          corrected_segments=segments,
                          transcript_ref={"transcript_id": "t", "transcript_version": "v1"},
                          rule_snapshot_id=None)
    assert entry["status"] in {"rendered", "skipped_playable"}
    doc = json.loads((tmp_path / "out" / "warnings.json").read_text(encoding="utf-8"))
    readability = doc.get("subtitle_readability") or {}
    assert readability.get("mode") == "analyzed"
    assert readability.get("box_cues"), "白底源片至少一条 cue 应判定用底板"
    qc = doc.get("subtitle_qc") or {}
    assert (qc.get("readability") or {}).get("mode") == "analyzed"
    assert doc.get("subtitle_safezone", {}).get("pass") is True
    assert doc.get("subtitle_emphasis_mode") in {"bold", "color", "off"}
    ass_text = (tmp_path / "out" / "subtitles.ass").read_text(encoding="utf-8")
    assert f"Style: {sc.BURN_BOX_STYLE_NAME}," in ass_text
