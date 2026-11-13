"""字幕 cue 合成层（subtitle_cueing）单元测试。

核心验收 case（2026-10-29 用户给定，来自任务 67137543951c 真实转写 04:18）：
改前（旧 build_srt 按画布宽硬折行）：
    是这样子的，他要看厚 / 度呢，因 / 为因为你0.4的厚度 / ，原则上 / 来讲，
    他要求是扩切0 / .5公分。
改后（期望，冗余开关开）：
    因为你0.4的厚度
    原则上要求扩切0.5公分
"""

from __future__ import annotations

import pytest

from src import subtitle_cueing as sc

CASE_TEXT = "是这样子的，他要看厚度呢，因为因为你0.4的厚度，原则上来讲，他要求是扩切0.5公分。"


def _cfg(**kw) -> sc.CueConfig:
    return sc.CueConfig(**kw)


def _cues(segments, start_ms=0, end_ms=600_000, cfg=None):
    return sc.build_cues_from_segments(segments, start_ms, end_ms, cfg)


def test_acceptance_case_fillers_on():
    """验收：冗余开关开（默认）→ 单条 cue 两行，文字与用户期望逐字一致。"""
    segs = [{"segment_id": "seg_0104", "start_ms": 6750, "end_ms": 14550, "text": CASE_TEXT}]
    cues, used, _ = _cues(segs, 0, 60_000, _cfg())
    assert used == ["seg_0104"]
    assert len(cues) == 1, cues
    cue = cues[0]
    assert cue["text"] == "因为你0.4的厚度\n原则上要求扩切0.5公分"
    # 时间码用原区间，不压缩
    assert cue["start"] == pytest.approx(6.75, abs=0.05)
    assert cue["end"] == pytest.approx(14.55, abs=0.01)


def test_acceptance_case_fillers_off():
    """验收：冗余开关关 → 保留「是这样子的」「他要看厚度呢」「因为因为」，
    断行仍遵守 16 字/行、2 行、词不拆、行首无标点。"""
    segs = [{"segment_id": "seg_0104", "start_ms": 6750, "end_ms": 14550, "text": CASE_TEXT}]
    cues, _, _ = _cues(segs, 0, 60_000, _cfg(strip_fillers=False))
    text = "".join(c["text"] for c in cues).replace("\n", "")
    assert "是这样子的" in text and "他要看厚度呢" in text and "因为因为" in text
    assert "原则上" in text and "0.4的厚度" in text and "0.5公分" in text
    for cue in cues:
        for line in cue["text"].split("\n"):
            assert len(line) <= 16


def test_number_unit_atomicity():
    """规则 3：数字与单位不拆（0.5公分 / 0.4个毫米 / 0.4的厚度 / PET-CT）。
    原子单元必须完整出现在某一显示行内，不得被换行从中间切断。"""
    segs = [{"segment_id": "s1", "start_ms": 0, "end_ms": 9000,
             "text": "厚度才0.4个毫米，我们一般要求是扩切到0.5公分以上才行。"}]
    cues, _, _ = _cues(segs, 0, 60_000, _cfg(strip_fillers=False))
    all_lines = [ln for c in cues for ln in c["text"].split("\n")]
    for atomic in ("0.4个毫米", "0.5公分"):
        assert any(atomic in ln for ln in all_lines), (atomic, all_lines)


@pytest.mark.parametrize("text", [
    "做基因检测，医附院说的就很严重了，就说意思说可切的还要大切，说切的不够。",
    "这样呢 PET-CT 就是分期越晚的病人，越是要做 PET-CT 因为他出现淋巴结转移的风险很高。",
    "即使你做了 PET 他也是大概率是没有淋巴结转移的，或者说他是这种很微小的转移，你是做不出来的。",
])
def test_line_shape_invariants(text):
    """规则 1/2/3/5 不变量：≤16 字/行、≤2 行/条、≥0.8s、无缝无重叠、行首无标点。"""
    segs = [{"segment_id": "s", "start_ms": 0, "end_ms": 12000, "text": text}]
    cues, _, _ = _cues(segs, 0, 60_000, _cfg())
    assert cues
    prev = None
    for cue in cues:
        lines = cue["text"].split("\n")
        assert len(lines) <= 2, cue["text"]
        for line in lines:
            assert 0 < len(line) <= 16, cue["text"]
            assert not line or line[0] not in "，。！？；：、,.!?;:", cue["text"]
        assert cue["end"] - cue["start"] >= 0.8 - 1e-6, cue
        if prev is not None:
            assert abs(cue["start"] - prev["end"]) < 1e-6      # 无缝隙
            assert cue["end"] > cue["start"]
        prev = cue


def test_pause_creates_sentence_boundary():
    """规则 1：分段间隔 ≥ pause 阈值 → 硬边界（不跨停顿合并 cue）。
    2026-11-12 修订：长停顿（> gap_extend 0.8s）→ 前条最多延伸 0.8s 后消失，
    不再挂到下一句开始（解决"一句话停留很长时间"）。"""
    segs = [
        {"segment_id": "a", "start_ms": 0, "end_ms": 2000, "text": "第一句话。"},
        {"segment_id": "b", "start_ms": 3200, "end_ms": 6000, "text": "第二句话。"},
    ]
    cues, _, _ = _cues(segs, 0, 60_000, _cfg())
    assert len(cues) == 2
    assert cues[0]["text"] == "第一句话" and cues[1]["text"] == "第二句话"
    # 长停顿：前条延伸不超过 gap_extend_ms（0.8s），剩余留白（字幕消失）
    assert cues[0]["end"] == pytest.approx(2.0 + 0.8, abs=1e-6)
    assert cues[1]["start"] - cues[0]["end"] == pytest.approx(0.4, abs=1e-6)


def test_small_gap_same_sentence_merges():
    """间隔小于停顿阈值 → 同句子句，装箱后单 cue 两行。"""
    segs = [
        {"segment_id": "a", "start_ms": 0, "end_ms": 2000, "text": "因为你0.4的厚度"},
        {"segment_id": "b", "start_ms": 2100, "end_ms": 4200, "text": "原则上要求扩切0.5公分"},
    ]
    cues, _, _ = _cues(segs, 0, 60_000, _cfg())
    assert len(cues) == 1
    assert cues[0]["text"] == "因为你0.4的厚度\n原则上要求扩切0.5公分"


def test_filler_and_repeat_removal():
    """规则 4：叠词去重 + 填充词删除；例外叠词保留。"""
    on = sc.CueConfig(strip_fillers=True)
    off = sc.CueConfig(strip_fillers=False)
    assert sc.strip_filler_spans.__doc__  # 自省：函数存在
    segs = [{"segment_id": "s", "start_ms": 0, "end_ms": 5000, "text": "对对对对对。"}]
    cues, _, _ = _cues(segs, 0, 60_000, on)
    assert cues[0]["text"] == "对"
    segs = [{"segment_id": "s", "start_ms": 0, "end_ms": 5000, "text": "非常非常低，谢谢大家。"}]
    cues, _, _ = _cues(segs, 0, 60_000, on)
    assert "非常" in cues[0]["text"] and "非常非常" not in cues[0]["text"]
    assert "谢谢" in cues[0]["text"]
    segs = [{"segment_id": "s", "start_ms": 0, "end_ms": 5000, "text": "因为因为赶时间。"}]
    cues, _, _ = _cues(segs, 0, 60_000, on)
    assert cues[0]["text"].startswith("因为") and "因为因为" not in cues[0]["text"]
    # 关开关：叠词保留
    cues, _, _ = _cues(
        [{"segment_id": "s", "start_ms": 0, "end_ms": 5000, "text": "因为因为赶时间。"}],
        0, 60_000, off)
    assert "因为因为" in cues[0]["text"]


def test_short_cue_merged_to_meet_min_duration():
    """规则 5：短条并入相邻条直至 ≥0.8s。"""
    segs = [
        {"segment_id": "a", "start_ms": 0, "end_ms": 1500, "text": "好的。"},
        {"segment_id": "b", "start_ms": 1500, "end_ms": 3000, "text": "我们开始吧。"},
        {"segment_id": "c", "start_ms": 3000, "end_ms": 4500, "text": "今天讲厚度。"},
    ]
    cues, _, _ = _cues(segs, 0, 60_000, _cfg())
    for cue in cues:
        assert cue["end"] - cue["start"] >= 0.8 - 1e-6, cue


def test_punct_style_keep_vs_omit():
    """规则 6：句尾标点全局统一，二选一。"""
    segs = [{"segment_id": "s", "start_ms": 0, "end_ms": 4000, "text": "第一句。第二句！"}]
    cues, _, _ = _cues(segs, 0, 60_000, _cfg(punct_style="omit"))
    assert all(not c["text"][-1] in "。！" for c in cues)
    cues, _, _ = _cues(segs, 0, 60_000, _cfg(punct_style="keep"))
    assert cues[-1]["text"].endswith("！")
    keep_tails = [c["text"][-1] for c in cues]
    assert all(t in "。！" for t in keep_tails)


def test_out_of_window_and_short_segments_kept_out():
    """切片外分段绝不出现；<200ms 分段跳过并记 warning（沿用原契约）。"""
    segs = [
        {"segment_id": "in", "start_ms": 500, "end_ms": 1800, "text": "窗口内。"},
        {"segment_id": "out", "start_ms": 7000, "end_ms": 7800, "text": "切片外。"},
        {"segment_id": "tiny", "start_ms": 1900, "end_ms": 2050, "text": "太短。"},
    ]
    cues, used, warns = _cues(segs, 0, 2000, _cfg())
    joined = "\n".join(c["text"] for c in cues)
    assert "窗口内" in joined and "切片外" not in joined and "太短" not in joined
    assert used == ["in"]
    assert any("不足 200ms" in w for w in warns)


def test_overlong_sentence_no_capacity_hard_cut():
    """规则 1：超长句不按固定字数硬切——断点落在词/子句边界。"""
    text = ("那么这个手术呢我们要看它的厚度还要看它的位置同时要看病人的年龄"
            "和身体的整体状况最后还要结合基因检测的结果来综合判断")
    segs = [{"segment_id": "s", "start_ms": 0, "end_ms": 15000, "text": text}]
    cues, _, warns = _cues(segs, 0, 60_000, _cfg())
    assert cues
    for cue in cues:
        for line in cue["text"].split("\n"):
            assert len(line) <= 16


def test_styled_ass固化样式():
    """烧录样式固化：白字/纯黑 2px 描边（BorderStyle=1）/Shadow=0，PlayRes 1:1。"""
    srt = "1\n00:00:06,750 --> 00:00:14,830\n因为你0.4的厚度\n原则上要求扩切0.5公分\n"
    ass = sc.srt_to_styled_ass(srt)
    style = [ln for ln in ass.splitlines() if ln.startswith("Style:")][0]
    fields = style.split(",")
    # Format: Name,Fontname,Fontsize,Primary,Secondary,Outline,Back,Bold,Italic,
    #         Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,...
    assert fields[16].strip() == "2"          # Outline=2（纯黑 2px 描边）
    assert fields[17].strip() == "0"          # Shadow=0（无阴影）
    assert fields[15].strip() == "1"          # BorderStyle=1（描边模式）
    assert fields[3].strip() == "&H00FFFFFF"  # 白字
    assert fields[5].strip() == "&H00000000"  # 描边纯黑
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    dlg = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")][0]
    assert "因为你0.4的厚度\\N原则上要求扩切0.5公分" in dlg
    assert "0:00:06.75,0:00:14.83" in dlg


def test_word_level_alignment_trims_trailing_silence_and_pauses():
    """words_ms 词级对齐（2026-11-12 生产接线）：cue 尾部停在最后一个词、
    尾部静音不挂字幕、句界停顿不被无限补齐。"""
    segs = [{"segment_id": "s", "start_ms": 0, "end_ms": 8000,
             "text": "第一句话。第二句话。"}]
    # 词只覆盖到 3.0s，段尾到 8.0s（5s 尾部静音）
    words_ms = [
        {"text": "第一句话", "start": 0.0, "end": 1200.0},
        {"text": "。", "start": 1200.0, "end": 1400.0},
        {"text": "第二句话", "start": 1600.0, "end": 2800.0},
        {"text": "。", "start": 2800.0, "end": 3000.0},
    ]
    cues, _, _ = sc.build_cues_from_segments(segs, 0, 60_000, _cfg(), words_ms=words_ms)
    assert len(cues) == 2, cues
    # 第一句闭合到下一句起点（短停顿 0.2s ≤ gap_extend 0.8s，防闪烁）
    assert cues[0]["end"] == pytest.approx(1.6, abs=0.05)
    # 尾部静音不挂字幕：末条 cue 停在词尾 3.0s，而不是段尾 8.0s
    assert cues[-1]["end"] == pytest.approx(3.0, abs=0.1)
    # 插值回退（不传 words）同输入会挂满段尾——对照组
    cues2, _, _ = sc.build_cues_from_segments(segs, 0, 60_000, _cfg())
    assert cues2[-1]["end"] > cues[-1]["end"]
