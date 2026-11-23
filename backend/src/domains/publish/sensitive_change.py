"""敏感变更检测：对两份分段文本按 segment_id 对齐并做确定性差异比对。

本模块是小范围纯 helper 包，设计边界如下（重要）：

- 规则只产出**候选 warning** 性质的 ``risk_flags``，仅用于提示人工复核；
  **不自动修订文本、不拦截/放行任何输出、不宣称任何医学正确性**；
- 不修改输入：函数返回后，两个输入列表与调用前**深相等**（只读访问）；
- 仅依赖 Python 标准库，不 import 本项目任何其它模块。

对齐与校验：
    按 ``segment_id`` 对齐 ``before_segments`` 与 ``after_segments``。出现下列
    任一情况时，相关条目写入返回值顶层 ``validation_errors``（每条至少含
    ``kind`` 与 ``segment_id``），此时 ``risk_flags`` 恒为 ``[]``，不对其余
    段落做任何猜测：

    - ``duplicate_segment_id``：同一列表内 id 重复（附 ``side``: before/after）；
    - ``missing_in_after``：id 只在 before 一侧存在；
    - ``missing_in_before``：id 只在 after 一侧存在；
    - ``uncertain_id_not_found``：``uncertain_segment_ids`` 中的 id 在两侧均
      不存在（id 至少在一侧出现即不算，单侧缺失由 missing_* 覆盖）。

    两个输入均为空列表属合法输入（返回零 flag）。

返回值固定三键；其中 ``validation_errors`` **恒存在**，无校验错误时为空列表::

    {
        "risk_flags": [...],
        "summary": {
            "segments_compared": int,
            "flags_by_category": {类别: 数量},   # 仅含数量 >= 1 的类别
            "coverage": {"medication": "enabled" | "skipped_no_terms"},
        },
        "validation_errors": [...],
    }

每条 risk_flag 固定六键，``dose_delta`` 额外携带 ``diff_tokens``（两侧 token
集合对称差排序后的列表）::

    {
        "segment_id": str,
        "category": "medication_term_change" | "dose_change" | "negation_change" | "uncertain",
        "rule_id": str,
        "before": str,           # before 原文摘录（可截断，保留差异词上下文）
        "after": str,            # after 原文摘录（同上）
        "requires_review": True,
    }

四类规则（全部确定性、候选 warning 性质）：

1. ``medication_term_change``：仅当调用方显式传入**非空** ``medical_terms``
   才启用；词在 after 出现而 before 没有 -> ``med_term_appearance``，反之 ->
   ``med_term_removal``。``medical_terms`` 为 None/空列表 -> 跳过该类，并在
   ``summary.coverage`` 标注 ``{"medication": "skipped_no_terms"}``。
2. ``dose_change``：抽取「数字+单位」token（单位表大小写不敏感；token 归一化
   为「数字+小写单位」，数字与单位间空白忽略；仅 ASCII 数字；集合语义，不含
   前置数字的孤立单位不算 token），两侧集合不一致 -> ``dose_delta``。
3. ``negation_change``：否定词按子串匹配（表内词存在重叠时各自独立计数）；
   词在 after 出现/在 before 消失 -> ``negation_appearance`` /
   ``negation_disappearance``。
4. ``uncertain``：调用方显式传入且对齐成功的 id -> ``caller_marked_uncertain``。

词表/单位表/否定词表为模块级常量，并允许通过 keyword-only 参数 ``dose_units``、
``negation_words`` 覆盖（默认值即模块常量，不传参时语义不变）。
"""

from __future__ import annotations

import re
from typing import Sequence

__all__ = [
    "DEFAULT_DOSE_UNITS",
    "DEFAULT_MEDICAL_TERMS",
    "DEFAULT_NEGATION_WORDS",
    "detect_sensitive_changes",
]

#: 药物词表默认为空：medication 规则必须由调用方显式提供词表才启用。
DEFAULT_MEDICAL_TERMS: tuple[str, ...] = ()

#: 剂量单位默认表（大小写不敏感）。
DEFAULT_DOSE_UNITS: tuple[str, ...] = (
    "mg", "μg", "ug", "mL", "ml", "片", "次", "每日", "日", "周", "次/日", "mg/日",
)

#: 否定词默认表（子串匹配）。
DEFAULT_NEGATION_WORDS: tuple[str, ...] = (
    "无", "不", "未", "否认", "无需", "没有", "未见",
)

#: 摘录最大宽度（字符数）。
_EXCERPT_WIDTH: int = 48

#: 「数字」片段（仅 ASCII 数字，支持小数）。
_NUMBER_PATTERN: str = r"\d+(?:\.\d+)?"


def _dedupe_keep_order(items: Sequence[str] | None) -> list[str]:
    """去重并保持首次出现顺序；None 或空视为空列表。"""
    if not items:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _build_dose_pattern(units: Sequence[str]) -> re.Pattern[str]:
    """根据单位表构建「数字+单位」匹配正则（大小写不敏感，长单位优先）。

    单位表为空时返回永不匹配的正则（等价于禁用剂量 token 抽取）。
    """
    unique_units = sorted(set(units), key=lambda unit: (-len(unit), unit))
    if not unique_units:
        return re.compile(r"(?!)")
    alternation = "|".join(re.escape(unit) for unit in unique_units)
    return re.compile(rf"({_NUMBER_PATTERN})\s*({alternation})", re.IGNORECASE)


def _extract_dose_tokens(text: str, pattern: re.Pattern[str]) -> set[str]:
    """抽取「数字+单位」token，归一化为「数字+小写单位」后以集合返回。"""
    tokens: set[str] = set()
    for match in pattern.finditer(text):
        tokens.add(f"{match.group(1)}{match.group(2).lower()}")
    return tokens


def _segment_text(segment: dict) -> str:
    """只读地取出段落文本；text 缺失或非字符串时按空串处理（不修改输入）。"""
    text = segment.get("text")
    return text if isinstance(text, str) else ""


def _excerpt(text: str, focus: str | None = None, width: int = _EXCERPT_WIDTH) -> str:
    """摘录原文本；超长时截断，并尽量保留差异词 focus 的上下文。

    截断处用「…」标注；focus 在原文中找不到时退化为头部截断。
    """
    if len(text) <= width:
        return text
    start = 0
    if focus:
        index = text.find(focus)
        if index >= 0:
            start = max(0, index - width // 3)
    end = min(len(text), start + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


def _make_flag(
    segment_id: str,
    category: str,
    rule_id: str,
    before_excerpt: str,
    after_excerpt: str,
    extra: dict | None = None,
) -> dict:
    """构造一条候选 warning flag（固定六键；extra 追加附加键）。"""
    flag: dict = {
        "segment_id": segment_id,
        "category": category,
        "rule_id": rule_id,
        "before": before_excerpt,
        "after": after_excerpt,
        "requires_review": True,
    }
    if extra:
        flag.update(extra)
    return flag


def detect_sensitive_changes(
    before_segments: list[dict],
    after_segments: list[dict],
    *,
    medical_terms: list[str] | None = None,
    uncertain_segment_ids: list[str] | None = None,
    dose_units: list[str] | None = None,
    negation_words: list[str] | None = None,
) -> dict:
    """对 before/after 两份分段文本做敏感变更检测（候选 warning，不自动修订）。

    本函数是确定性规则比对：产出的 ``risk_flags`` 全部为候选 warning，仅供
    人工复核提示；不自动修订、不拦截输出、不宣称医学正确。函数不修改输入，
    返回后两个输入列表与调用前深相等。

    Args:
        before_segments: 变更前分段列表，元素形如 ``{"segment_id": str,
            "text": str, ...}``（可带 start_ms/end_ms 等字段，只读）。
        after_segments: 变更后分段列表，结构同上。
        medical_terms: 药物词表覆盖；None 或空列表时跳过 medication 规则并在
            ``summary.coverage`` 标注 ``{"medication": "skipped_no_terms"}``。
        uncertain_segment_ids: 调用方显式标记为不确定的 segment id；其中在
            两侧均不存在的 id 记入 ``validation_errors``。
        dose_units: 剂量单位表覆盖（keyword-only，默认 ``DEFAULT_DOSE_UNITS``，
            语义不变；空列表等价于禁用剂量 token 抽取）。
        negation_words: 否定词表覆盖（keyword-only，默认
            ``DEFAULT_NEGATION_WORDS``，语义不变）。

    Returns:
        dict: 固定三键 ``{"risk_flags", "summary", "validation_errors"}``；
        ``validation_errors`` 恒存在，无校验错误时为空列表。存在任何校验错误
        时 ``risk_flags`` 恒为 ``[]``。规则细节见模块 docstring。
    """
    before_ids: list[str] = [segment["segment_id"] for segment in before_segments]
    after_ids: list[str] = [segment["segment_id"] for segment in after_segments]
    before_set = set(before_ids)
    after_set = set(after_ids)

    validation_errors: list[dict] = []

    # 校验 1：同一列表内重复 id（每个 (side, id) 只报一条）。
    for side, ids in (("before", before_ids), ("after", after_ids)):
        counts: dict[str, int] = {}
        for segment_id in ids:
            counts[segment_id] = counts.get(segment_id, 0) + 1
        reported: set[str] = set()
        for segment_id in ids:
            if counts[segment_id] > 1 and segment_id not in reported:
                reported.add(segment_id)
                validation_errors.append(
                    {"kind": "duplicate_segment_id", "segment_id": segment_id, "side": side}
                )

    # 校验 2：id 单侧缺失。
    for segment_id in _dedupe_keep_order(before_ids):
        if segment_id not in after_set:
            validation_errors.append({"kind": "missing_in_after", "segment_id": segment_id})
    for segment_id in _dedupe_keep_order(after_ids):
        if segment_id not in before_set:
            validation_errors.append({"kind": "missing_in_before", "segment_id": segment_id})

    # 校验 3：uncertain 名单中在两侧均不存在的 id。
    uncertain_ids = _dedupe_keep_order(uncertain_segment_ids)
    uncertain_set = set(uncertain_ids)
    for segment_id in uncertain_ids:
        if segment_id not in before_set and segment_id not in after_set:
            validation_errors.append(
                {"kind": "uncertain_id_not_found", "segment_id": segment_id}
            )

    # medication 规则：None/空列表 -> 跳过并在 coverage 标注。
    terms = _dedupe_keep_order(medical_terms)
    summary: dict = {
        "segments_compared": 0,
        "flags_by_category": {},
        "coverage": {"medication": "enabled" if terms else "skipped_no_terms"},
    }

    if validation_errors:
        # 存在校验错误：不猜测任何段落，risk_flags 恒为空。
        return {"risk_flags": [], "summary": summary, "validation_errors": validation_errors}

    before_by_id: dict[str, dict] = dict(zip(before_ids, before_segments))
    after_by_id: dict[str, dict] = dict(zip(after_ids, after_segments))
    aligned_ids = _dedupe_keep_order(before_ids)

    dose_pattern = _build_dose_pattern(
        DEFAULT_DOSE_UNITS if dose_units is None else dose_units
    )
    neg_words = _dedupe_keep_order(
        DEFAULT_NEGATION_WORDS if negation_words is None else negation_words
    )

    risk_flags: list[dict] = []
    flags_by_category: dict[str, int] = {}

    def _append_flag(flag: dict) -> None:
        risk_flags.append(flag)
        flags_by_category[flag["category"]] = flags_by_category.get(flag["category"], 0) + 1

    for segment_id in aligned_ids:
        before_text = _segment_text(before_by_id[segment_id])
        after_text = _segment_text(after_by_id[segment_id])

        # 规则 1：药物词出现/移除（仅显式提供非空词表时启用）。
        for term in terms:
            in_before = term in before_text
            in_after = term in after_text
            if in_before and not in_after:
                _append_flag(_make_flag(
                    segment_id,
                    "medication_term_change",
                    "med_term_removal",
                    _excerpt(before_text, focus=term),
                    _excerpt(after_text),
                ))
            elif in_after and not in_before:
                _append_flag(_make_flag(
                    segment_id,
                    "medication_term_change",
                    "med_term_appearance",
                    _excerpt(before_text),
                    _excerpt(after_text, focus=term),
                ))

        # 规则 2：剂量 token 集合差异。
        before_tokens = _extract_dose_tokens(before_text, dose_pattern)
        after_tokens = _extract_dose_tokens(after_text, dose_pattern)
        if before_tokens != after_tokens:
            _append_flag(_make_flag(
                segment_id,
                "dose_change",
                "dose_delta",
                _excerpt(before_text),
                _excerpt(after_text),
                extra={"diff_tokens": sorted(before_tokens ^ after_tokens)},
            ))

        # 规则 3：否定词出现/消失（子串匹配，按词表顺序产出）。
        before_neg = {word for word in neg_words if word in before_text}
        after_neg = {word for word in neg_words if word in after_text}
        for word in neg_words:
            if word in after_neg and word not in before_neg:
                _append_flag(_make_flag(
                    segment_id,
                    "negation_change",
                    "negation_appearance",
                    _excerpt(before_text),
                    _excerpt(after_text, focus=word),
                ))
            elif word in before_neg and word not in after_neg:
                _append_flag(_make_flag(
                    segment_id,
                    "negation_change",
                    "negation_disappearance",
                    _excerpt(before_text, focus=word),
                    _excerpt(after_text),
                ))

        # 规则 4：调用方显式标记的 uncertain 段。
        if segment_id in uncertain_set:
            _append_flag(_make_flag(
                segment_id,
                "uncertain",
                "caller_marked_uncertain",
                _excerpt(before_text),
                _excerpt(after_text),
            ))

    summary["segments_compared"] = len(aligned_ids)
    summary["flags_by_category"] = flags_by_category
    return {"risk_flags": risk_flags, "summary": summary, "validation_errors": []}
