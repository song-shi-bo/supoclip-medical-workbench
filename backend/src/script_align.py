"""文稿-实拍句级对齐（对稿成片管线的 P0 发动机）。

职责边界：
- 输入：纠错后转写分段（segment_id/start_ms/end_ms/corrected_text）+ 一篇拍摄文稿
  （hook/body/cta）或文稿列表。
- 输出：JSON 可序列化的对齐报告——每句文稿 matched/missing（带时间区间）、
  转写中未被任何文稿句覆盖的 extra 段（医生多说/跑偏/口误重说候选）、
  覆盖率与 is_script_shot 判定。
- 不做剪辑决策、不读库、不调模型：匹配与分级是管线后续阶段的输入。

匹配算法（v1，按稿拍摄天然有序，故用单调贪心 + 窗口拼接）：
- 双侧归一化（NFKC→小写→去标点/空白/语气字 嗯呃）后按 SequenceMatcher 比相似度；
- 对每句文稿，在前方 SKIP_MAX 个转写分段内尝试 1~WINDOW_MAX 个连续分段拼接，
  取最高相似度 ≥ MATCH_RATIO 判 matched；未达阈值记 missing，指针不动
  （医生跳句后，后续文稿句仍能在原位序找到）；
- 贪心消费转写分段，未被消费的分段即 extra。

VERSION: script-align/v1
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

SCRIPT_ALIGN_VERSION = "script-align/v1"

# 单句匹配阈值：归一化相似度 ≥ 此值判 matched（宁可漏配进 missing，不硬凑）
MATCH_RATIO = 0.55
# 一句文稿最多拼接几个连续转写分段（ASR 分段可能把两三句合在一段）
WINDOW_MAX = 3
# 匹配时允许向前跳过的转写分段数（跳过的记 extra）
SKIP_MAX = 5
# is_script_shot 判定：文稿句覆盖率 ≥ 此值认为是按该稿拍摄
COVERAGE_SCRIPT_SHOT = 0.5

_SENT_SPLIT_RE = re.compile(r"[。！？；!?;\n]+")
_PUNCT_RE = re.compile(
    r"[\s，。！？；：、,.!?;:()（）\[\]【】<>《》\"'“”‘’…—·\-_%~～^|]+"
)
_FILLER_RE = re.compile(r"[嗯呃]")


def normalize_text(value: str) -> str:
    """匹配用归一化：NFKC 折叠全角→小写→去标点/空白→去语气字 嗯呃。"""
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = _PUNCT_RE.sub("", text)
    return _FILLER_RE.sub("", text)


def split_script_sentences(script: dict[str, Any]) -> list[dict[str, Any]]:
    """文稿 → 有序句列表（hook → body → cta），body/cta 按句切分。"""
    sentences: list[dict[str, Any]] = []
    for field in ("hook", "body", "cta"):
        raw = str(script.get(field) or "")
        for piece in _SENT_SPLIT_RE.split(raw):
            piece = piece.strip()
            if not piece:
                continue
            sentences.append({
                "index": len(sentences),
                "field": field,
                "text": piece,
                "norm": normalize_text(piece),
            })
    return sentences


def _normalize_segments(transcript_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segs: list[dict[str, Any]] = []
    for seg in transcript_segments:
        raw_text = str(seg.get("corrected_text") or seg.get("text") or "")
        segs.append({
            "segment_id": seg.get("segment_id"),
            "start_ms": int(seg.get("start_ms") or 0),
            "end_ms": int(seg.get("end_ms") or 0),
            "text": raw_text,
            "norm": normalize_text(raw_text),
        })
    return segs


def _similarity(sentence_norm: str, window_norm: str) -> float:
    """归一化相似度：SequenceMatcher 比率，句子被完整包含时给包含加成。"""
    if not sentence_norm or not window_norm:
        return 0.0
    ratio = SequenceMatcher(None, sentence_norm, window_norm).ratio()
    if sentence_norm in window_norm:
        # 句子完整出现在（拼接）分段里：保底 0.75，避免长分段稀释比率
        ratio = max(ratio, 0.75)
    return round(min(ratio, 1.0), 4)


def align_transcript_to_script(
    transcript_segments: list[dict[str, Any]],
    script: dict[str, Any],
) -> dict[str, Any]:
    """对齐一篇文稿。返回 JSON 可序列化报告（见模块 docstring）。"""
    sentences = split_script_sentences(script)
    segs = _normalize_segments(transcript_segments)

    sentence_results: list[dict[str, Any]] = []
    consumed: set[int] = set()
    cursor = 0
    for sentence in sentences:
        best: tuple[float, int, int] | None = None
        scan_end = min(cursor + SKIP_MAX, len(segs))
        for j in range(cursor, scan_end):
            for w in range(1, WINDOW_MAX + 1):
                if j + w > len(segs):
                    break
                window_norm = "".join(s["norm"] for s in segs[j:j + w])
                score = _similarity(sentence["norm"], window_norm)
                if best is None or score > best[0]:
                    best = (score, j, w)
        if best and best[0] >= MATCH_RATIO:
            score, j, w = best
            for k in range(j, j + w):
                consumed.add(k)
            sentence_results.append({
                "index": sentence["index"],
                "field": sentence["field"],
                "text": sentence["text"],
                "status": "matched",
                "ratio": score,
                "segment_ids": [segs[k]["segment_id"] for k in range(j, j + w)],
                "start_ms": segs[j]["start_ms"],
                "end_ms": segs[j + w - 1]["end_ms"],
            })
            cursor = j + w
        else:
            sentence_results.append({
                "index": sentence["index"],
                "field": sentence["field"],
                "text": sentence["text"],
                "status": "missing",
                "ratio": best[0] if best else 0.0,
                "segment_ids": [],
                "start_ms": None,
                "end_ms": None,
            })

    extra_segments = [
        {
            "segment_id": segs[k]["segment_id"],
            "start_ms": segs[k]["start_ms"],
            "end_ms": segs[k]["end_ms"],
            "text": segs[k]["text"],
        }
        for k in range(len(segs)) if k not in consumed
    ]

    matched_count = sum(1 for s in sentence_results if s["status"] == "matched")
    total = len(sentences)
    coverage = round(matched_count / total, 4) if total else 0.0
    return {
        "version": SCRIPT_ALIGN_VERSION,
        "script": {
            "id": script.get("id"),
            "title": script.get("title"),
            "version": script.get("version"),
        },
        "script_sentences": {
            "total": total,
            "matched": matched_count,
            "missing": [s["index"] for s in sentence_results if s["status"] == "missing"],
        },
        "coverage_ratio": coverage,
        "is_script_shot": coverage >= COVERAGE_SCRIPT_SHOT,
        "sentences": sentence_results,
        "extra_segments": extra_segments,
        "warnings": (
            ["文稿无任何句内容（hook/body/cta 均为空）"] if total == 0 else []
        ),
    }


def match_script_library(
    transcript_segments: list[dict[str, Any]],
    scripts: list[dict[str, Any]],
) -> dict[str, Any]:
    """转写 vs 文稿库：逐篇对齐取覆盖率最高。返回 best 报告 + 全部候选摘要（降序）。"""
    candidates: list[dict[str, Any]] = []
    best_report: dict[str, Any] | None = None
    for script in scripts:
        report = align_transcript_to_script(transcript_segments, script)
        candidates.append({
            "script_id": script.get("id"),
            "title": script.get("title"),
            "version": script.get("version"),
            "coverage_ratio": report["coverage_ratio"],
            "is_script_shot": report["is_script_shot"],
        })
        if best_report is None or report["coverage_ratio"] > best_report["coverage_ratio"]:
            best_report = report
    candidates.sort(key=lambda c: c["coverage_ratio"], reverse=True)
    if best_report is None:
        best_report = {
            "version": SCRIPT_ALIGN_VERSION,
            "script": {"id": None, "title": None, "version": None},
            "script_sentences": {"total": 0, "matched": 0, "missing": []},
            "coverage_ratio": 0.0,
            "is_script_shot": False,
            "sentences": [],
            "extra_segments": [],
            "warnings": ["文稿库为空或未提供候选"],
        }
    return {
        "version": SCRIPT_ALIGN_VERSION,
        "best": best_report,
        "candidates": candidates,
        "candidate_pool_size": len(scripts),
        "warnings": (
            ["最高覆盖率未达 is_script_shot 阈值"]
            if not (best_report and best_report["is_script_shot"]) and scripts
            else []
        ),
    }
