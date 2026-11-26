"""conform_edl（对稿成片 P1a：气口检测与 EDL 决策）单元测试。

纯单元：不连库、不调模型。覆盖：
* 干净口播 → 全保留（仅片头/片尾裁切）；
* 句间停顿压缩到 400ms；段落间（跨文稿句边界）压缩到 700ms；
* 短气口（<300ms，含呼吸）整段保留；
* 语气词整 token 删除；红线段（含数字/药名/否定词）内语气词豁免；
* 重复分段保最后一段；extra 段（对齐产物）整段删除；
* 碎剪合并（<120ms 剪除丢弃）；无词级时间降级；剪除率超阈值黄警告。
"""

from src.conform_edl import build_conform_edl

SCRIPT_ALIGN_REPORT = {
    "sentences": [
        {"index": 0, "status": "matched", "end_ms": 1500},
        {"index": 1, "status": "matched", "end_ms": 3500},
    ],
    "extra_segments": [
        {"segment_id": 99, "start_ms": 3200, "end_ms": 6000, "text": "顺便说一句下周有义诊。"},
    ],
}


def W(text: str, s: int, e: int) -> dict:
    return {"text": text, "start": s / 1000.0, "end": e / 1000.0}


def SEG(sid: int, s: int, e: int, text: str) -> dict:
    return {"segment_id": sid, "start_ms": s, "end_ms": e, "corrected_text": text}


def _cuts(edl: dict, cut_type: str | None = None) -> list[dict]:
    return [d for d in edl["decisions"]
            if d["action"] == "cut" and (cut_type is None or d["type"] == cut_type)]


def _kept(edl: dict, cut_type: str | None = None) -> list[dict]:
    return [d for d in edl["decisions"]
            if d["action"] == "kept" and (cut_type is None or d["type"] == cut_type)]


def _overlaps(edl: dict, start: int, end: int) -> bool:
    """保留区间是否完整覆盖 [start,end]（即这段没被剪）。"""
    for s, e in edl["keep_ranges_ms"]:
        if s <= start and end <= e:
            return True
    return False


def test_clean_take_full_keep():
    transcript = {"asr_segments": [SEG(0, 300, 5000, "免疫治疗出现副作用先别急着停药。")],
                  "words": [W("免疫治疗出现副作用", 300, 3800), W("先别急着停药", 3850, 5000)]}
    edl = build_conform_edl(transcript, None, script={"title": "t"})
    # 词间 50ms 气口 <= breath 300 → 不剪；只有片头 300 / 片尾 500 裁切
    assert edl["keep_ranges_ms"][0][0] == 600   # 源片起点 300 + 片头保留 300
    assert edl["keep_ranges_ms"][-1][1] == 4500  # 片尾保留 500ms（5000-500）
    assert edl["stats"]["filler_cuts"] == 0
    assert edl["warnings"] == []


def test_sentence_gap_compressed_to_400ms():
    transcript = {"asr_segments": [
        SEG(0, 0, 1500, "先说结论。"),
        SEG(1, 3500, 6000, "免疫治疗副作用可控。"),
    ], "words": [W("先说结论", 0, 1500), W("免疫治疗副作用可控", 3500, 6000)]}
    edl = build_conform_edl(transcript, None)
    gaps = _cuts(edl, "gap")
    assert len(gaps) == 1
    assert gaps[0]["start_ms"] == 1900 and gaps[0]["end_ms"] == 3500
    assert gaps[0]["keep_ms"] == 400  # 剪切点=词尾+保留值；无对齐报告 → 句间策略


def test_paragraph_gap_uses_paragraph_keep():
    transcript = {"asr_segments": [
        SEG(0, 0, 1500, "先说结论。"),
        SEG(1, 3500, 6000, "免疫治疗副作用可控。"),
    ], "words": [W("先说结论", 0, 1500), W("免疫治疗副作用可控", 3500, 6000)]}
    edl = build_conform_edl(transcript, SCRIPT_ALIGN_REPORT)
    gaps = _cuts(edl, "gap")
    assert gaps[0]["keep_ms"] == 700  # 边界 1500 落在 gap 内 → 段落间策略
    assert gaps[0]["start_ms"] == 2200 and gaps[0]["end_ms"] == 3500


def test_breath_gap_kept_entirely():
    transcript = {"asr_segments": [
        SEG(0, 0, 1500, "先说结论。"),
        SEG(1, 1750, 6000, "免疫治疗副作用可控。"),
    ], "words": [W("先说结论", 0, 1500), W("免疫治疗副作用可控", 1750, 6000)]}
    edl = build_conform_edl(transcript, None)
    assert _cuts(edl, "gap") == []  # 250ms < breath_max 300 → 保留呼吸


def test_filler_cut_and_redline_exempt():
    transcript = {"asr_segments": [
        SEG(0, 0, 3000, "免疫治疗出现副作用。"),
        SEG(1, 3200, 6000, "这个药5毫克每天两次。嗯"),       # 红线段（含数字+剂量单位）
        SEG(2, 6200, 8000, "定期复查很重要。嗯"),            # 普通段
    ], "words": [
        W("免疫治疗出现副作用", 0, 3000),
        W("这个药", 3200, 3800), W("5毫克", 3800, 4500), W("每天两次", 4500, 5500),
        W("嗯", 5500, 5800),
        W("定期复查很重要", 6200, 7800), W("嗯", 7800, 8000),
    ]}
    edl = build_conform_edl(transcript, None, drug_terms=("曲美替尼",))
    cut_fillers = _cuts(edl, "filler")
    kept_fillers = _kept(edl, "filler")
    assert len(cut_fillers) == 1 and cut_fillers[0]["start_ms"] == 7800
    assert len(kept_fillers) == 1 and kept_fillers[0]["reason"] == "红线段豁免"
    # 红线段内部（3200~6000）不得有任何剪切
    for d in _cuts(edl):
        assert not (d["start_ms"] < 6000 and d["end_ms"] > 3200), d


def test_repeat_keeps_last_take():
    transcript = {"asr_segments": [
        SEG(0, 0, 3000, "免疫治疗出现副作用先别急着停药"),
        SEG(1, 3200, 6200, "免疫治疗出现副作用先别急着停药"),
        SEG(2, 6400, 8000, "定期复查很重要"),
    ], "words": [W("免疫治疗出现副作用先别急着停药", 0, 3000),
                 W("免疫治疗出现副作用先别急着停药", 3200, 6200),
                 W("定期复查很重要", 6400, 8000)]}
    edl = build_conform_edl(transcript, None)
    repeats = _cuts(edl, "repeat")
    assert len(repeats) == 1
    assert repeats[0]["start_ms"] == 0 and repeats[0]["end_ms"] == 3000  # 前一段剪掉
    assert edl["keep_ranges_ms"] == [[3000, 7500]]  # 最后一段完整保留


def test_extra_segment_cut():
    transcript = {"asr_segments": [
        SEG(0, 0, 3000, "免疫治疗出现副作用。"),
        SEG(99, 3200, 6000, "顺便说一句下周有义诊。"),
        SEG(1, 6200, 8000, "定期复查很重要。"),
    ], "words": [W("免疫治疗出现副作用", 0, 3000), W("顺便说一句下周有义诊", 3200, 6000),
                 W("定期复查很重要", 6200, 8000)]}
    edl = build_conform_edl(transcript, SCRIPT_ALIGN_REPORT)
    extras = _cuts(edl, "extra")
    assert len(extras) == 1 and extras[0]["start_ms"] == 3200
    assert not _overlaps(edl, 3200, 6000)


def test_tiny_cut_dropped_by_min_merge():
    transcript = {"asr_segments": [
        SEG(0, 0, 3000, "免疫治疗出现副作用。嗯。"),
        SEG(1, 3200, 6000, "定期复查很重要。"),
    ], "words": [W("免疫治疗出现副作用", 0, 2800), W("嗯", 2800, 2880),
                 W("定期复查很重要", 3200, 6000)]}
    edl = build_conform_edl(transcript, None)
    assert _cuts(edl, "filler")  # 决策账里记录了剪除意图
    assert _overlaps(edl, 2800, 2880)  # 80ms < min_keep_merge 120 → 实际保留


def test_no_words_degrades_to_segment_gaps():
    transcript = {"asr_segments": [
        SEG(0, 0, 1500, "先说结论。"),
        SEG(1, 4000, 6000, "免疫治疗副作用可控。"),
    ], "words": []}
    edl = build_conform_edl(transcript, None)
    assert any("词级时间缺失" in w for w in edl["warnings"])
    gaps = _cuts(edl, "gap")
    assert gaps and gaps[0]["keep_ms"] == 400  # 段间停顿仍按句间策略压缩


def test_cut_ratio_warning():
    segs = []
    words = []
    start = 0
    for i in range(6):
        segs.append(SEG(i, start, start + 1000, f"第{i}句话。"))
        words.append(W(f"第{i}句话", start, start + 1000))
        start += 5000  # 每段后 4000ms 停顿
    transcript = {"asr_segments": segs, "words": words}
    edl = build_conform_edl(transcript, None)
    assert edl["cut_ratio"] > 0.30
    assert any("建议重拍而非硬剪" in w for w in edl["warnings"])


# ---------------------------------------------------------------------------
# P2a：LLM 分级裁决集成
# ---------------------------------------------------------------------------

def test_review_golden_keeps_extra():
    transcript = {"asr_segments": [
        SEG(0, 0, 3000, "免疫治疗出现副作用。"),
        SEG(99, 3200, 6000, "顺便提醒一句，复查前不要停药。"),
        SEG(1, 6200, 8000, "定期复查很重要。"),
    ], "words": [W("免疫治疗出现副作用", 0, 3000), W("复查前不要停药提醒", 3200, 6000),
                 W("定期复查很重要", 6200, 8000)]}
    review = {"engine": "llm:test", "verdicts": [
        {"segment_id": "99", "classification": "golden",
         "coherent_removal": True, "reason": "患者提醒，医学有价值"}]}
    edl = build_conform_edl(transcript, SCRIPT_ALIGN_REPORT, review=review)
    extras = _cuts(edl, "extra")
    assert extras == []                              # golden → 不剪
    assert _overlaps(edl, 3200, 6000)                # 完整保留
    kept = _kept(edl, "extra")
    assert kept and "金句保留" in kept[0]["reason"]
    assert edl["review_engine"] == "llm:test"


def test_review_risk_cuts_extra():
    transcript = {"asr_segments": [
        SEG(0, 0, 3000, "免疫治疗出现副作用。"),
        SEG(99, 3200, 6000, "在我们医院治疗保证有效率百分之九十。"),
        SEG(1, 6200, 8000, "定期复查很重要。"),
    ], "words": [W("免疫治疗出现副作用", 0, 3000), W("有效率承诺", 3200, 6000),
                 W("定期复查很重要", 6200, 8000)]}
    review = {"engine": "llm:test", "verdicts": [
        {"segment_id": "99", "classification": "risk",
         "coherent_removal": True, "reason": "疗效承诺，合规风险"}]}
    edl = build_conform_edl(transcript, SCRIPT_ALIGN_REPORT, review=review)
    extras = _cuts(edl, "extra")
    assert len(extras) == 1 and "合规风险" in extras[0]["reason"]
    assert not _overlaps(edl, 3200, 6000)


def test_review_incoherent_removal_keeps_with_warning():
    transcript = {"asr_segments": [
        SEG(0, 0, 3000, "免疫治疗出现副作用。"),
        SEG(99, 3200, 6000, "顺便说一句下周有义诊。"),
        SEG(1, 6200, 8000, "定期复查很重要。"),
    ], "words": [W("免疫治疗出现副作用", 0, 3000), W("义诊预告", 3200, 6000),
                 W("定期复查很重要", 6200, 8000)]}
    review = {"engine": "llm:test", "verdicts": [
        {"segment_id": "99", "classification": "offtopic",
         "coherent_removal": False, "reason": "剪掉后话锋转换突兀"}]}
    edl = build_conform_edl(transcript, SCRIPT_ALIGN_REPORT, review=review)
    assert _cuts(edl, "extra") == []
    assert _overlaps(edl, 3200, 6000)
    assert any("剪除可能造成上下文断裂" in w for w in edl["warnings"])


def test_no_review_default_cuts_extra():
    transcript = {"asr_segments": [
        SEG(0, 0, 3000, "免疫治疗出现副作用。"),
        SEG(99, 3200, 6000, "顺便说一句下周有义诊。"),
        SEG(1, 6200, 8000, "定期复查很重要。"),
    ], "words": [W("免疫治疗出现副作用", 0, 3000), W("义诊预告", 3200, 6000),
                 W("定期复查很重要", 6200, 8000)]}
    edl = build_conform_edl(transcript, SCRIPT_ALIGN_REPORT)  # 无 LLM 分级（P1 默认）
    assert len(_cuts(edl, "extra")) == 1
    assert edl["review_engine"] is None
