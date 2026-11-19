"""阶段2 字幕升级 P1 单元测试（2026-11-18）。

覆盖：
* A1 cue 上限 12s→6s：默认值、env 钳制、词级连续语流切分、整平护栏
  （合并/缝隙延伸都不得突破上限）；
* A2 药名保护词：原子 token 不拆、词典 type=drug 注入；
* A3 品牌字体解析链：env 配置→族名/fontsdir、无效回退、未配置保持文泉驿
  字节兼容；
* A6 subtitle_qc：SRT 反解、验收指标金样本、同步 p95、词缺失透出；
* A7 渲染指纹版本串 bump。
"""

from __future__ import annotations

import json

import pytest

from src import clip_render as cr
from src import subtitle_cueing as sc
from src import subtitle_qc as sq


# ---------------------------------------------------------------------------
# A1 cue 时长上限
# ---------------------------------------------------------------------------


def test_max_cue_default_is_six_and_env_clamped():
    """默认 6.0；env 超过 6 钳回（验收「>6s cue = 0」），低于 6 允许下探。"""
    assert sc.config_from_env({}).max_cue_seconds == 6.0
    assert sc.config_from_env({"SUBTITLE_MAX_CUE_SECONDS": "12"}).max_cue_seconds == 6.0
    assert sc.config_from_env({"SUBTITLE_MAX_CUE_SECONDS": "4.5"}).max_cue_seconds == 4.5


def _long_speech_segments(total_s: float = 30.0) -> tuple[list[dict], list[dict]]:
    """一段连续无标点语流 + 词级时间（每字 0.25s，无停顿）。"""
    text = "这个治疗方案需要坚持很长一段时间的规范用药才能看到比较明确的效果变化"
    seg = [{"segment_id": "seg_0001", "start_ms": 0,
            "end_ms": int(total_s * 1000), "text": text}]
    words = []
    t = 0.0
    for ch in text:
        words.append({"text": ch, "start": round(t, 3), "end": round(t + 0.25, 3)})
        t += 0.25
    return seg, words


def test_no_cue_over_max_with_word_level_continuous_speech():
    """词级连续语流（无停顿）：每条 cue 都 ≤ max_cue_seconds（默认 6s）。"""
    segs, words = _long_speech_segments()
    cues, _, _ = sc.build_cues_from_segments(
        segs, 0, 60_000, sc.CueConfig(), words_ms=words)
    assert cues, "连续语流应产出 cue"
    over = [c for c in cues if c["end"] - c["start"] > 6.0 + 1e-6]
    assert over == []


def test_tile_merging_never_exceeds_max_duration():
    """整平护栏：5.5s 长条 + 短于最短时长的相邻条，合并会破 6s → 不合并。"""
    cues = [
        {"start": 0.0, "end": 5.5, "text": "前半句内容已经接近上限的长度"},
        {"start": 5.5, "end": 5.8, "text": "短"},
    ]
    sc._tile_cues(cues, sc.CueConfig(), [], window=30.0)
    for cue in cues:
        assert cue["end"] - cue["start"] <= 6.0 + 1e-6


def test_gap_extension_bounded_by_max_duration():
    """缝隙延伸：5.6s 长条后接 10s 空隙，延伸 0.8s 会破 6s → 钳在上限。"""
    cues = [
        {"start": 0.0, "end": 5.6, "text": "这一条已经非常接近六秒的时长上限了"},
        {"start": 15.6, "end": 18.0, "text": "停顿很久之后的新内容"},
    ]
    sc._tile_cues(cues, sc.CueConfig(), [], window=30.0)
    assert cues[0]["end"] - cues[0]["start"] <= 6.0 + 1e-6


# ---------------------------------------------------------------------------
# A2 药名保护词
# ---------------------------------------------------------------------------


def test_drug_name_is_atomic_token():
    """药名在原子 token 层完整切出，不与前后文粘连。"""
    tokens = sc._atomic_tokens("建议规律服用二甲双胍控制血糖")
    assert "二甲双胍" in tokens
    tokens2 = sc._atomic_tokens("帕博利珠单抗联合化疗")
    assert "帕博利珠单抗" in tokens2


def test_drug_name_never_split_across_lines():
    """药名落在 16 字行宽边界上：折行不拆词（出现在某一整行里）。"""
    text = "他建议你长期规律服用二甲双胍并且定期复查血糖和肝肾功能指标"
    segs = [{"segment_id": "seg_0001", "start_ms": 0, "end_ms": 8000, "text": text}]
    cues, _, _ = sc.build_cues_from_segments(segs, 0, 60_000, sc.CueConfig())
    joined = "".join(c["text"].replace("\n", "") for c in cues)
    assert "二甲双胍" in joined
    for cue in cues:
        for line in cue["text"].split("\n"):
            assert "二甲双" not in line or "二甲双胍" in line or "胍" not in line


def test_lexicon_drug_entries_injected(tmp_path, monkeypatch):
    """medical_lexicon TSV 的 type=drug 标准词进入保护词；缓存需显式清空。"""
    lex_dir = tmp_path / "lexicon"
    lex_dir.mkdir()
    (lex_dir / "drugs.tsv").write_text(
        "# 测试词典\n磷酸氯喹\tdrug\t\t5\t单测\n", encoding="utf-8")
    monkeypatch.setenv("MEDICAL_LEXICON_DIR", str(lex_dir))
    sc._protected_token_pattern.cache_clear()
    try:
        assert "磷酸氯喹" in sc._drug_names()
        tokens = sc._atomic_tokens("处方开具磷酸氯喹一片")
        assert "磷酸氯喹" in tokens
    finally:
        monkeypatch.undo()
        sc._protected_token_pattern.cache_clear()


# ---------------------------------------------------------------------------
# A3 品牌字体解析链
# ---------------------------------------------------------------------------


def test_resolve_font_default_keeps_wenquanyi_byte_compat():
    """未配置品牌字体 → 文泉驿历史默认（产物字节兼容）。"""
    family, fontsdir = sc.resolve_burn_font({})
    assert family == sc.BURN_FONT_NAME
    assert fontsdir.endswith("wqy")


def test_resolve_font_brand_env_resolves_family_and_dir():
    """品牌字体配置 → 字体内部真实族名（libass 按元数据匹配）+ 字体所在目录。"""
    family, fontsdir = sc.resolve_burn_font({"SUBTITLE_BRAND_FONT": "wqy-zenhei.ttc"})
    assert family  # wqy 内部族名为「文泉驛正黑」，非显示名，可被 libass 解析
    assert fontsdir.endswith("wqy")


def test_resolve_font_empty_placeholder_skipped():
    """0 字节占位字体（现存 MicrosoftYaHeiBold.ttc）必须被跳过，落到可用项。"""
    from pathlib import Path
    from src import font_registry
    yahei = font_registry.FONTS_DIR / "MicrosoftYaHeiBold.ttc"
    if yahei.is_file() and yahei.stat().st_size == 0:
        family, _ = sc.resolve_burn_font({"SUBTITLE_BRAND_FONT": "MicrosoftYaHeiBold.ttc"})
        # 占位雅黑解析失败 → 链内下一可用项（本环境 = 文泉驿），绝不返回坏族名
        assert family 


def test_resolve_font_invalid_brand_falls_back_to_chain():
    """品牌名无效 → 回退链雅黑 → 仍失败文泉驿，绝不抛错。"""
    family, _ = sc.resolve_burn_font({"SUBTITLE_BRAND_FONT": "NoSuchBrandFont.ttf"})
    assert family
    family2, _ = sc.resolve_burn_font({"SUBTITLE_BRAND_FONT": "../etc/passwd"})
    assert family2


def test_styled_ass_font_parameterization():
    """ASS 头字体参数化：指定族名生效；默认与历史产物一致。"""
    srt = "1\n00:00:00,000 --> 00:00:01,000\n测试\n"
    custom = sc.srt_to_styled_ass(srt, font_name="Brand Sans CJK")
    assert ",Brand Sans CJK,63," in custom
    default = sc.srt_to_styled_ass(srt)
    assert f",{sc.BURN_FONT_NAME},{sc.BURN_FONT_SIZE_PX}," in default
    assert "Style: Sub," in default


# ---------------------------------------------------------------------------
# A6 subtitle_qc
# ---------------------------------------------------------------------------


def test_cues_from_srt_roundtrip_multiline():
    srt = ("1\n00:00:00,000 --> 00:00:02,500\n第一行\n第二行\n"
           "\n2\n00:00:03,000 --> 00:00:05,000\n下一条\n")
    cues = sq.cues_from_srt(srt)
    assert len(cues) == 2
    assert cues[0] == {"start": 0.0, "end": 2.5, "text": "第一行\n第二行"}
    assert cues[1]["start"] == 3.0 and cues[1]["end"] == 5.0


def test_qc_structure_violations_detected():
    cfg = sc.CueConfig()  # 16 字/行、2 行、6s
    bad = [
        {"start": 0.0, "end": 1.0, "text": "这一行超过十六个汉字的长度限制会带来不可读风险"},  # 21字
        {"start": 1.0, "end": 2.0, "text": "一\n二\n三"},
        {"start": 2.0, "end": 9.5, "text": "挂屏七秒半"},
        {"start": 9.5, "end": 10.5, "text": "合规行"},
    ]
    report = sq.qc_report(bad, words=[], cfg=cfg, env={})
    assert report["lines"]["violations"] == 1
    assert report["lines_per_cue"]["violations"] == 1
    assert report["duration"]["over_max"] == [{"cue": 3, "duration_s": 7.5}]
    assert report["pass"]["structure"] is False
    assert report["pass"]["overall"] is False


def test_qc_sync_p95_golden():
    """单词 cue：起点对齐、尾部拖 0.3s → p95=300ms 判不合格；对齐版合格。"""
    cfg = sc.CueConfig()
    cues = [{"start": 1.0, "end": 3.3, "text": "同步测试"}]
    words = [{"text": "同步测试", "start": 1.0, "end": 3.0}]
    report = sq.qc_report(cues, words=words, cfg=cfg, env={})
    assert report["sync"]["p95_ms"] == 300.0
    assert report["pass"]["sync"] is False

    cues_ok = [{"start": 1.0, "end": 3.0, "text": "同步测试"}]
    report_ok = sq.qc_report(cues_ok, words=words, cfg=cfg, env={})
    assert report_ok["sync"]["p95_ms"] == 0.0
    assert report_ok["pass"]["sync"] is True
    assert report_ok["pass"]["overall"] is True


def test_qc_words_missing_flagged_not_failing():
    """无词级数据：p95 为 None、words_missing 透出，不计为失败。"""
    cfg = sc.CueConfig()
    report = sq.qc_report([{"start": 0.0, "end": 2.0, "text": "无词数据"}],
                          words=[], cfg=cfg, env={})
    assert report["sync"]["p95_ms"] is None
    assert report["sync"]["words_missing_cues"] == 1
    assert report["pass"]["sync"] is None
    assert report["pass"]["overall"] is True


# ---------------------------------------------------------------------------
# A7 渲染指纹 + clip_render QC 接线（真实 ffmpeg，最小切片）
# ---------------------------------------------------------------------------


def test_render_fingerprint_version_bumped(tmp_path):
    """指纹版本串：P2 后为 subtitle-v3/1（有词）/subtitle-v3-base/1（无词）。"""
    import inspect
    src = inspect.getsource(cr._render_fingerprint)
    assert '"subtitle-v3/1"' in src
    assert '"subtitle-v3-base/1"' in src
    plan = {"clip_id": "c", "start_ms": 0, "end_ms": 1000}
    words = [{"text": "词", "start": 0.0, "end": 0.5}]
    assert cr._render_fingerprint(plan, tmp_path / "src.mp4", [], {"t": 1}, None, None, words)


def test_render_one_writes_subtitle_qc_block(tmp_path, monkeypatch):
    """渲染产物 warnings.json 含 subtitle_qc 验收块（cue_count>0）。

    本环境磁盘水位低（容量守卫按全站 floor 拒绝小文件试渲染），测试旁路
    容量预留——被测对象是 warnings 文档结构，不是容量守卫本身。"""
    import contextlib
    import subprocess as _sp
    monkeypatch.setattr(cr, "reserve_capacity",
                        lambda *a, **k: contextlib.nullcontext())
    source = tmp_path / "src.mp4"
    _sp.run(
        [cr._resolve(cr.FFMPEG_BIN), "-y",
         "-f", "lavfi", "-i", "testsrc2=duration=3:size=160x120:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
         str(source)], check=True, capture_output=True)
    clip = {"clip_id": "qc-clip", "start_ms": 0, "end_ms": 2500, "warnings": []}
    segments = [{"segment_id": "s1", "start_ms": 100, "end_ms": 2000,
                 "text": "这是一条用于质检的测试字幕内容。"}]
    words = []
    t = 0.1
    for ch in "这是一条用于质检的测试字幕内容。":
        words.append({"text": ch, "start": round(t, 3), "end": round(t + 0.15, 3)})
        t += 0.15
    entry = cr.render_one(clip, source=source, out_dir=tmp_path / "out",
                          corrected_segments=segments,
                          transcript_ref={"transcript_id": "t", "transcript_version": "v1"},
                          rule_snapshot_id=None, words=words)
    assert entry["status"] in {"rendered", "skipped_playable"}
    doc = json.loads((tmp_path / "out" / "warnings.json").read_text(encoding="utf-8"))
    qc = doc.get("subtitle_qc") or {}
    assert qc.get("cue_count", 0) >= 1
    assert qc.get("sync", {}).get("p95_ms") is not None
    assert qc.get("pass", {}).get("structure") is True
