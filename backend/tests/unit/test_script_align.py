"""script_align（对稿成片 P0：文稿-实拍句级对齐）单元测试。

纯单元：不连库、不调模型。覆盖：
* 逐句精确匹配（coverage 1.0，无 extra/missing）；
* 语气词/标点差异归一化后仍匹配；
* 医生多说的段落 → extra_segments，不影响文稿句匹配；
* 医生跳句 → 该句 missing，后续句仍按序匹配；
* 完全无关转写 → 覆盖率 0、is_script_shot=False；
* match_script_library 在多篇候选中取覆盖率最高者。
"""

from src.script_align import (
    COVERAGE_SCRIPT_SHOT,
    align_transcript_to_script,
    match_script_library,
    normalize_text,
    split_script_sentences,
)

SCRIPT = {
    "id": "s-001",
    "title": "免疫治疗副作用别急着停药",
    "version": 2,
    "hook": "免疫治疗出现副作用，先别急着停药。",
    "body": "很多患者一看不良反应就自己把药停了。其实大多数副作用是可控的。关键是第一时间告诉你的主治医生。",
    "cta": "具体问题，评论区留言。",
}


def _seg(sid: int, start: int, end: int, text: str) -> dict:
    return {"segment_id": sid, "start_ms": start, "end_ms": end, "corrected_text": text}


def test_normalize_and_sentence_split():
    assert normalize_text("嗯，免疫治—疗！") == "免疫治疗"
    sentences = split_script_sentences(SCRIPT)
    fields = [s["field"] for s in sentences]
    assert fields[0] == "hook" and fields[-1] == "cta"
    assert len(sentences) == 5  # hook1 + body3 + cta1


def test_exact_match_full_coverage():
    transcript = [
        _seg(0, 0, 4000, "免疫治疗出现副作用，先别急着停药。"),
        _seg(1, 4000, 9000, "很多患者一看不良反应就自己把药停了。"),
        _seg(2, 9000, 14000, "其实大多数副作用是可控的。"),
        _seg(3, 14000, 20000, "关键是第一时间告诉你的主治医生。"),
        _seg(4, 20000, 24000, "具体问题，评论区留言。"),
    ]
    report = align_transcript_to_script(transcript, SCRIPT)
    assert report["coverage_ratio"] == 1.0
    assert report["is_script_shot"] is True
    assert report["script_sentences"]["missing"] == []
    assert report["extra_segments"] == []
    assert report["sentences"][0]["segment_ids"] == [0]
    assert report["sentences"][0]["start_ms"] == 0


def test_fillers_and_punctuation_still_match():
    transcript = [
        _seg(0, 0, 4500, "嗯，免疫治疗出现副作用，呃，先别急着停药啊。"),
        _seg(1, 4500, 9500, "就是很多患者一看不良反应，就自己把药停了。"),
        _seg(2, 9500, 15000, "其实呢，大多数副作用是可控的。"),
        _seg(3, 15000, 21000, "关键是第一时间告诉你的主治医生。"),
        _seg(4, 21000, 25000, "具体问题，评论区留言。"),
    ]
    report = align_transcript_to_script(transcript, SCRIPT)
    assert report["coverage_ratio"] == 1.0
    assert report["is_script_shot"] is True


def test_extra_remark_flagged_but_sentences_still_match():
    transcript = [
        _seg(0, 0, 4000, "免疫治疗出现副作用，先别急着停药。"),
        _seg(1, 4000, 9000, "很多患者一看不良反应就自己把药停了。"),
        _seg(2, 9000, 13000, "顺便说一句，我们科室下周有患者教育活动。"),
        _seg(3, 13000, 18000, "其实大多数副作用是可控的。"),
        _seg(4, 18000, 24000, "关键是第一时间告诉你的主治医生。"),
        _seg(5, 24000, 28000, "具体问题，评论区留言。"),
    ]
    report = align_transcript_to_script(transcript, SCRIPT)
    assert report["coverage_ratio"] == 1.0
    extras = report["extra_segments"]
    assert len(extras) == 1 and extras[0]["segment_id"] == 2
    assert "患者教育活动" in extras[0]["text"]


def test_missing_sentence_reported():
    transcript = [
        _seg(0, 0, 4000, "免疫治疗出现副作用，先别急着停药。"),
        _seg(1, 4000, 9000, "很多患者一看不良反应就自己把药停了。"),
        _seg(2, 9000, 14000, "关键是第一时间告诉你的主治医生。"),
        _seg(3, 14000, 18000, "具体问题，评论区留言。"),
    ]
    report = align_transcript_to_script(transcript, SCRIPT)
    missing = report["script_sentences"]["missing"]
    assert len(missing) == 1
    assert report["sentences"][missing[0]]["status"] == "missing"
    assert report["sentences"][missing[0]]["start_ms"] is None
    assert report["coverage_ratio"] >= COVERAGE_SCRIPT_SHOT  # 5/6 仍判按稿拍


def test_unrelated_transcript_not_script_shot():
    transcript = [
        _seg(0, 0, 8000, "今天我们聊聊肺癌筛查的低剂量螺旋CT。"),
        _seg(1, 8000, 16000, "四十岁以上的吸烟人群建议每年查一次。"),
        _seg(2, 16000, 24000, "发现结节先别慌，大部分是良性的。"),
    ]
    report = align_transcript_to_script(transcript, SCRIPT)
    assert report["coverage_ratio"] < COVERAGE_SCRIPT_SHOT
    assert report["is_script_shot"] is False
    assert len(report["extra_segments"]) == 3


def test_match_script_library_picks_best():
    other = dict(SCRIPT, id="s-002", title="胃癌的三次阻断机会", version=1,
                 hook="胃癌不是一天形成的。", body="从胃炎到胃癌有三次阻断机会。", cta="转发给家人。")
    transcript = [
        _seg(0, 0, 4000, "免疫治疗出现副作用，先别急着停药。"),
        _seg(1, 4000, 9000, "很多患者一看不良反应就自己把药停了。"),
        _seg(2, 9000, 14000, "其实大多数副作用是可控的。"),
        _seg(3, 14000, 20000, "关键是第一时间告诉你的主治医生。"),
        _seg(4, 20000, 24000, "具体问题，评论区留言。"),
    ]
    result = match_script_library(transcript, [other, SCRIPT])
    assert result["best"]["script"]["id"] == "s-001"
    assert result["candidate_pool_size"] == 2
    assert result["candidates"][0]["script_id"] == "s-001"
    assert result["candidates"][0]["coverage_ratio"] >= result["candidates"][1]["coverage_ratio"]
    assert result["warnings"] == []

    empty = match_script_library(transcript, [])
    assert empty["best"]["is_script_shot"] is False
    assert empty["best"]["warnings"] == ["文稿库为空或未提供候选"]
