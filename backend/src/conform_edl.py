"""对稿成片：气口检测与 EDL 决策引擎（P1a，纯规则、确定性、可对账）。

职责边界：
- 输入：纠错转写（asr_segments + words 词级秒）+ 对齐报告（script_align 产物）+
  可选文稿；输出 conform_edl.json——源片时间轴上的 keep_ranges_ms + 逐条决策账。
- 只做"识别→判定"，不做渲染：keep_ranges 交由 conform_render 映射 sub_ranges。
- 确定性引擎：同样输入必得同样 EDL；LLM 分级（P2）只改写 extra/重复的处置标签，
  不产生任何时间戳。

气口五类处置（参数全部 env 可配，见 DEFAULT_PARAMS/_env_value）：
- 句间停顿 → 压缩保留 gap_sentence_ms；段落间（跨文稿句）→ gap_paragraph_ms；
- 短气口（< breath_max）含呼吸 → 整段保留；
- 语气词 → 词表整 token 删除（保守内置表：嗯/呃/唉 系；那个/就是/然后 等实义词
  默认不入表——医疗内容误删语义的风险大于收益）；
- 重复/自我修正 → 相邻分段归一化相似度 ≥ repeat_sim → 保最后一段；
- extra 段（医生多说，对齐产物）→ 整段删除（P2 LLM 可改标 golden 保留）。

红线豁免（硬规则，优先于一切剪切）：分段文本含 阿拉伯数字/全角数字、剂量单位、
药名词表、否定词 → 该分段整段保护，段内任何剪切（含气口压缩）全部放弃。
VERSION: conform-edl/v1
"""

from __future__ import annotations

import os
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from .script_align import normalize_keep_filler, normalize_text

CONFORM_EDL_VERSION = "conform-edl/v1"

# 保守内置语气词表：只收纯填充音节（嗯/呃/唉系）。
# 那个/这个/就是/然后 等"实词型口头语"默认不入表——医疗语义误删风险大于节奏收益，
# 确需扩展时经 params["filler_lexicon"] 覆盖并人工确认。
DEFAULT_FILLER_LEXICON: tuple[str, ...] = ("嗯", "呃", "唉", "嗯嗯", "呃呃", "唉唉")

DEFAULT_NEGATION_WORDS: tuple[str, ...] = (
    "不是", "没有", "并非", "未必", "不能", "不会", "不可以",
    "不建议", "不推荐", "切勿", "禁止", "不要", "别自行", "别擅自",
)

# 剂量单位/数值线索：出现即视为红线段（保守方向：多豁免、少剪切）
_DOSE_UNITS: tuple[str, ...] = (
    "毫克", "毫克每", "克/", "毫升", "微克", "单位", "%", "％",
    "mg", "ml", "μg", "iu",
)

DEFAULT_PARAMS: dict[str, Any] = {
    "gap_sentence_ms": 400,       # 句间保留（业界 300~500 取中）
    "gap_paragraph_ms": 700,      # 段落间保留（业界 600~800 取中）
    "breath_max_ms": 300,         # 气口短于此值整段保留（含呼吸不剪断）
    "min_keep_merge_ms": 120,     # 剪除段短于此值丢弃（防碎剪机关枪）
    "repeat_sim": 0.8,            # 重复判定归一化相似度
    "repeat_window_ms": 10000,    # 重复判定窗口
    "cut_ratio_warn": 0.30,       # 总剪除率超此值 → 黄警告"建议重拍而非硬剪"
    "head_keep_ms": 300,          # 片头保留（首词前）
    "tail_keep_ms": 500,          # 片尾保留（末词后）
}

_ENV_KEYS = {
    "gap_sentence_ms": "CONFORM_GAP_SENTENCE_MS",
    "gap_paragraph_ms": "CONFORM_GAP_PARAGRAPH_MS",
    "breath_max_ms": "CONFORM_BREATH_MAX_MS",
    "min_keep_merge_ms": "CONFORM_MIN_KEEP_MERGE_MS",
    "repeat_sim": "CONFORM_REPEAT_SIM",
    "repeat_window_ms": "CONFORM_REPEAT_WINDOW_MS",
    "cut_ratio_warn": "CONFORM_CUT_RATIO_WARN",
    "head_keep_ms": "CONFORM_HEAD_KEEP_MS",
    "tail_keep_ms": "CONFORM_TAIL_KEEP_MS",
}

_INT_PARAMS = {"gap_sentence_ms", "gap_paragraph_ms", "breath_max_ms",
               "min_keep_merge_ms", "repeat_window_ms", "head_keep_ms", "tail_keep_ms"}


def load_params(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """env 默认 → overrides 覆盖。数值参数容错解析，非法值回退默认。"""
    params = dict(DEFAULT_PARAMS)
    for key, env in _ENV_KEYS.items():
        raw = os.getenv(env)
        if not raw:
            continue
        try:
            params[key] = int(raw) if key in _INT_PARAMS else float(raw)
        except (TypeError, ValueError):
            params.setdefault("_warnings", []).append(f"env {env}={raw!r} 非法，回退默认")
    if isinstance(params.get("filler_lexicon"), (list, tuple)) is False:
        params["filler_lexicon"] = list(DEFAULT_FILLER_LEXICON)
    if overrides:
        for key, value in overrides.items():
            if key in params or key in ("filler_lexicon",):
                params[key] = value
    params["filler_lexicon"] = tuple(params.get("filler_lexicon") or DEFAULT_FILLER_LEXICON)
    return params


# ---------------------------------------------------------------------------
# 红线识别
# ---------------------------------------------------------------------------

_DIGIT_RE = re.compile(r"[0-9０-９]")
_TOKEN_PUNCT_RE = re.compile(
    r"[\s，。！？；：、,.!?;:()（）\[\]【】<>《》\"'“”‘’…—·\-_ %~～^|]+")


def _token_norm(value: str) -> str:
    """语气词 token 归一化：只去标点/空白，**保留 嗯呃 本体**（与 normalize_text 的
    语义归一化不同——把语气词抹掉的话词表匹配就永远落空了）。"""
    return _TOKEN_PUNCT_RE.sub("", unicodedata.normalize("NFKC", str(value or "")).lower())
_UNIT_RE = re.compile("|".join(re.escape(u) for u in _DOSE_UNITS), re.IGNORECASE)


def _is_redline_text(text: str, drug_terms: tuple[str, ...] | list[str]) -> bool:
    """红线段判定：阿拉伯/全角数字、剂量单位、药名词、否定词任一命中即豁免。"""
    raw = str(text or "")
    lowered = raw.lower()
    if _DIGIT_RE.search(raw) or _UNIT_RE.search(lowered):
        return True
    for neg in DEFAULT_NEGATION_WORDS:
        if neg in raw:
            return True
    for drug in drug_terms or ():
        if len(drug) >= 2 and drug in raw:
            return True
    return False


# ---------------------------------------------------------------------------
# 词/分段预处理
# ---------------------------------------------------------------------------

def _words_ms(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    words = transcript.get("words") or []
    out: list[dict[str, Any]] = []
    for w in words:
        try:
            start = int(float(w.get("start") or 0.0) * 1000.0)
            end = int(float(w.get("end") or 0.0) * 1000.0)
        except (TypeError, ValueError):
            continue
        text = str(w.get("text") or w.get("word") or "")
        if end < start:
            start, end = end, start
        out.append({"text": text, "token_norm": _token_norm(text), "start_ms": start, "end_ms": end})
    out.sort(key=lambda x: x["start_ms"])
    return out


def _segments(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    segs: list[dict[str, Any]] = []
    for seg in transcript.get("asr_segments") or []:
        segs.append({
            "segment_id": seg.get("segment_id"),
            "start_ms": int(seg.get("start_ms") or 0),
            "end_ms": int(seg.get("end_ms") or 0),
            "text": str(seg.get("corrected_text") or seg.get("text") or ""),
            "norm": normalize_text(str(seg.get("corrected_text") or seg.get("text") or "")),
        })
    segs.sort(key=lambda x: x["start_ms"])
    return segs


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# 候选生成
# ---------------------------------------------------------------------------

def _filler_cuts(words: list[dict[str, Any]], lexicon: tuple[str, ...],
                 protected: list[tuple[int, int]],
                 decisions: list[dict[str, Any]]) -> None:
    lex_set = {normalize_keep_filler(x) for x in lexicon}
    for w in words:
        if w["token_norm"] not in lex_set or not w["token_norm"]:
            continue
        span = (w["start_ms"], w["end_ms"])
        if any(p_start <= span[0] and span[1] <= p_end for p_start, p_end in protected):
            decisions.append({"type": "filler", "start_ms": span[0], "end_ms": span[1],
                              "action": "kept", "reason": "红线段豁免", "text": w["text"]})
            continue
        decisions.append({"type": "filler", "start_ms": span[0], "end_ms": span[1],
                          "action": "cut", "reason": "纯填充语气词", "text": w["text"]})


def _gap_cuts(words: list[dict[str, Any]], segs: list[dict[str, Any]],
              boundaries: list[int], params: dict[str, Any],
              protected: list[tuple[int, int]],
              decisions: list[dict[str, Any]]) -> None:
    """词间（无词时退化为段间）停顿压缩：保留分级策略，气口短于 breath_max 整段保留。"""
    keep_points: list[tuple[int, int, str]] = []  # (prev_end, next_start, kind)
    if len(words) >= 2:
        for prev, nxt in zip(words, words[1:]):
            keep_points.append((prev["end_ms"], nxt["start_ms"], "word"))
    else:
        for prev, nxt in zip(segs, segs[1:]):
            keep_points.append((prev["end_ms"], nxt["start_ms"], "segment"))

    sentence_keep = int(params["gap_sentence_ms"])
    paragraph_keep = int(params["gap_paragraph_ms"])
    breath_max = int(params["breath_max_ms"])
    for prev_end, next_start, _kind in keep_points:
        gap = next_start - prev_end
        if gap <= breath_max:
            continue
        is_paragraph = any(prev_end <= b <= next_start for b in boundaries)
        keep = paragraph_keep if is_paragraph else sentence_keep
        cut_start, cut_end = prev_end + keep, next_start
        if cut_end - cut_start <= 0:
            continue
        if any(not (cut_end <= p_start or cut_start >= p_end)
               for p_start, p_end in protected):
            decisions.append({"type": "gap", "start_ms": cut_start, "end_ms": cut_end,
                              "action": "kept", "reason": "红线段豁免", "gap_ms": gap})
            continue
        decisions.append({"type": "gap", "start_ms": cut_start, "end_ms": cut_end,
                          "action": "cut", "reason": "段落间停顿" if is_paragraph else "句间停顿",
                          "gap_ms": gap, "keep_ms": keep})


def _repeat_cuts(segs: list[dict[str, Any]], params: dict[str, Any],
                 protected: list[tuple[int, int]],
                 decisions: list[dict[str, Any]]) -> None:
    sim_threshold = float(params["repeat_sim"])
    window = int(params["repeat_window_ms"])
    for prev, nxt in zip(segs, segs[1:]):
        if nxt["start_ms"] - prev["end_ms"] > window:
            continue
        if not prev["norm"] or not nxt["norm"]:
            continue
        if _sim(prev["norm"], nxt["norm"]) >= sim_threshold:
            span = (prev["start_ms"], prev["end_ms"])
            if any(not (span[1] <= p_start or span[0] >= p_end) for p_start, p_end in protected):
                decisions.append({"type": "repeat", "start_ms": span[0], "end_ms": span[1],
                                  "action": "kept", "reason": "红线段豁免", "text": prev["text"]})
                continue
            decisions.append({"type": "repeat", "start_ms": span[0], "end_ms": span[1],
                              "action": "cut", "reason": "重复/自我修正：保留最后一段",
                              "text": prev["text"]})


def _extra_cuts(extra_segments: list[dict[str, Any]],
                protected: list[tuple[int, int]],
                decisions: list[dict[str, Any]],
                warnings: list[str] | None = None,
                review: dict[str, Any] | None = None) -> None:
    """extra 段处置：LLM 分级（P2a）裁决优先——golden 保留 / risk 合规剪 /
    断裂保留；无裁决 → 默认整段删除。红线保护仍优先于 golden（药名剂量段不保留）。"""
    verdicts = {str(v.get("segment_id")): v
                for v in (review or {}).get("verdicts") or []}
    for extra in extra_segments or []:
        span = (int(extra.get("start_ms") or 0), int(extra.get("end_ms") or 0))
        if span[1] <= span[0]:
            continue
        sid = str(extra.get("segment_id"))
        verdict = verdicts.get(sid)
        if verdict:
            classification = str(verdict.get("classification") or "offtopic")
            coherent = verdict.get("coherent_removal", True)
            reason = str(verdict.get("reason") or "")[:120]
            if classification == "golden":
                decisions.append({"type": "extra", "start_ms": span[0], "end_ms": span[1],
                                  "action": "kept", "reason": f"金句保留（LLM）：{reason}",
                                  "text": extra.get("text")})
                continue
            if not coherent:
                decisions.append({"type": "extra", "start_ms": span[0], "end_ms": span[1],
                                  "action": "kept", "reason": f"删后可能断裂，保留（LLM）：{reason}",
                                  "text": extra.get("text")})
                if warnings is not None:
                    warnings.append(f"extra 段 {sid} 剪除可能造成上下文断裂，已保留（LLM 判定）")
                continue
            decisions.append({"type": "extra", "start_ms": span[0], "end_ms": span[1],
                              "action": "cut",
                              "reason": f"LLM 分级 {classification}：{reason}",
                              "text": extra.get("text")})
            continue
        if any(not (span[1] <= p_start or span[0] >= p_end) for p_start, p_end in protected):
            decisions.append({"type": "extra", "start_ms": span[0], "end_ms": span[1],
                              "action": "kept", "reason": "红线段豁免", "text": extra.get("text")})
            continue
        decisions.append({"type": "extra", "start_ms": span[0], "end_ms": span[1],
                          "action": "cut", "reason": "文稿外多说段（对齐产物）",
                          "text": extra.get("text")})


# ---------------------------------------------------------------------------
# EDL 装配
# ---------------------------------------------------------------------------

def _assemble(cuts: list[tuple[int, int]], total_start: int, total_end: int,
              min_keep_merge_ms: int) -> list[list[int]]:
    """剪除段合并/去重 → 源片保留区间。剪除段短于阈值时丢弃（防碎剪）。"""
    merged: list[list[int]] = []
    for start, end in sorted((s, e) for s, e in cuts if e > s):
        if end - start < min_keep_merge_ms:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    keep: list[list[int]] = []
    cursor = total_start
    for cut_start, cut_end in merged:
        if cut_start > cursor:
            keep.append([cursor, min(cut_start, total_end)])
        cursor = max(cursor, cut_end)
    if cursor < total_end:
        keep.append([cursor, total_end])
    return [span for span in keep if span[1] > span[0]]


def build_conform_edl(
    transcript: dict[str, Any],
    alignment: dict[str, Any] | None,
    script: dict[str, Any] | None = None,
    *,
    params: dict[str, Any] | None = None,
    drug_terms: tuple[str, ...] | list[str] = (),
    job_id: str | None = None,
    review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建对稿精剪 EDL。返回 JSON 可序列化决策账（见模块 docstring）。"""
    cfg = load_params(params)
    segs = _segments(transcript)
    words = _words_ms(transcript)
    drug_tuple = tuple(drug_terms or ())

    protected: list[tuple[int, int]] = []
    redline_count = 0
    for seg in segs:
        if _is_redline_text(seg["text"], drug_tuple):
            protected.append((seg["start_ms"], seg["end_ms"]))
            redline_count += 1

    extra_segments = list((alignment or {}).get("extra_segments") or [])
    # 段落边界：对齐报告里 matched 文稿句的结束时间（下一句开始前的停顿即段落间）
    boundaries = [s["end_ms"] for s in (alignment or {}).get("sentences") or []
                  if s.get("status") == "matched" and s.get("end_ms") is not None]

    decisions: list[dict[str, Any]] = []
    warnings: list[str] = []
    _filler_cuts(words, cfg["filler_lexicon"], protected, decisions)
    _gap_cuts(words, segs, boundaries, cfg, protected, decisions)
    _repeat_cuts(segs, cfg, protected, decisions)
    _extra_cuts(extra_segments, protected, decisions, warnings, review)

    cut_spans = [(d["start_ms"], d["end_ms"]) for d in decisions if d["action"] == "cut"]
    total_start = min([s["start_ms"] for s in segs] + [w["start_ms"] for w in words] or [0])
    total_end = max([s["end_ms"] for s in segs] + [w["end_ms"] for w in words] or [0])
    head = min(int(cfg["head_keep_ms"]), max(total_end - total_start, 0))
    cut_spans.append((total_start, total_start + head))
    tail = min(int(cfg["tail_keep_ms"]), max(total_end - total_start, 0))
    cut_spans.append((max(total_end - tail, total_start), total_end))

    keep_ranges = _assemble(cut_spans, total_start, total_end,
                            int(cfg["min_keep_merge_ms"]))
    output_duration = sum(e - s for s, e in keep_ranges)
    total_span = max(total_end - total_start, 1)
    cut_ratio = round(1.0 - output_duration / total_span, 4)

    if not words:
        warnings.append("词级时间缺失：语气词跳过，仅做分段级停顿/重复/extra 处置")
    if cut_ratio >= float(cfg["cut_ratio_warn"]):
        warnings.append(f"总剪除率 {cut_ratio:.0%} 超过 {float(cfg['cut_ratio_warn']):.0%}："
                        "建议重拍而非硬剪")
    if script is None:
        warnings.append("未提供文稿：段落判定退化为句间策略")

    return {
        "version": CONFORM_EDL_VERSION,
        "job_id": job_id,
        "review_engine": (review or {}).get("engine"),
        "params": {k: (list(v) if isinstance(v, tuple) else v) for k, v in cfg.items()
                   if not k.startswith("_")},
        "keep_ranges_ms": keep_ranges,
        "output_duration_ms": output_duration,
        "source_span_ms": {"start": total_start, "end": total_end},
        "cut_ratio": cut_ratio,
        "redline": {"protected_segments": redline_count},
        "decisions": decisions,
        "stats": {
            "filler_cuts": sum(1 for d in decisions if d["type"] == "filler" and d["action"] == "cut"),
            "gap_cuts": sum(1 for d in decisions if d["type"] == "gap" and d["action"] == "cut"),
            "repeat_cuts": sum(1 for d in decisions if d["type"] == "repeat" and d["action"] == "cut"),
            "extra_cuts": sum(1 for d in decisions if d["type"] == "extra" and d["action"] == "cut"),
            "kept_by_redline": sum(1 for d in decisions if d["action"] == "kept"),
        },
        "warnings": warnings,
    }
