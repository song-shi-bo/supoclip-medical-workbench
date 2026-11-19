"""医学关键词克制强调（阶段2 P2 2026-11-18）：字幕里的术语加粗/着色。

**克制规则（写死，进规范文档）**
1. 每条 cue **最多强调 1 处**；
2. 单处 **≤6 字**（超长术语不强调——整行强调=没有强调）；
3. **数字/单位永不强调**（0.5公分这类数字 token 本身已醒目）；
4. 换行跨行的术语不强调（ASS 换行 ``\\N`` 无法作为一个整体套标签）；
5. 强调形态全局三选一 ``SUBTITLE_EMPHASIS=off|bold|color``（默认 **bold**）：
   bold=``{\\b1}``，color=低饱和暖金 ``{\\1c&H00D7FF&}``（RGB 255,215,0）——
   **禁用红/绿**（医疗内容里红绿有高危色彩语义）。

**术语来源（只匹配渲染，绝不把指南库条目正文写进字幕——医学闸门红线）**
- ``medical_lexicon`` TSV 全部类型（drug/disease/proc/symptom/term）的标准词；
- 医学指南库快照 approved 条目的 ``keywords`` 字段（只取关键词列表）；
- 项目热词表（``project_glossary.load_glossary``）——渲染链目前不带 project
  上下文，传 ``project_id`` 时启用，None 时自动跳过。
任何来源缺失/损坏 → 静默跳过该来源，绝不抛错（字幕链路不因词典阻断）。
"""

from __future__ import annotations

import os
import re
from functools import lru_cache as _lru_cache

from . import subtitle_cueing as _cueing

#: 强调色（ASS &HBBGGRR&：BGR 00/D7/FF = 暖金 RGB 255,215,0）
EMPHASIS_COLOR_ASS = "&H00D7FF&"
#: 主色复位（烧录规范白字）
_RESET_COLOR_ASS = "&HFFFFFF&"
#: 单处强调最大字数（克制规则 2）
_MAX_SPAN_CHARS = 6
#: 术语最短字数（单字泛匹配风险）
_MIN_TERM_CHARS = 2

_NUM_MATCH_RE = _cueing._NUM_RE


def emphasis_mode(env: dict | None = None) -> str:
    """强调形态：``off|bold|color``（默认 bold；非法值回落 bold）。"""
    e = os.environ if env is None else env
    mode = str(e.get("SUBTITLE_EMPHASIS") or "").strip().lower()
    return mode if mode in {"off", "bold", "color"} else "bold"


@_lru_cache(maxsize=1)
def _term_pattern() -> "re.Pattern[str] | None":
    """术语交替式（长名优先；与药名保护词同款机制，缓存一次）。"""
    terms: set[str] = set()
    try:
        from . import medical_lexicon
        for entry in medical_lexicon.load_lexicon().get("entries") or []:
            if isinstance(entry, dict):
                word = str(entry.get("standard") or entry.get("word") or "").strip()
                if _MIN_TERM_CHARS <= len(word) <= _MAX_SPAN_CHARS:
                    terms.add(word)
    except Exception:
        pass
    try:
        from . import medical_kb
        state = medical_kb.load_snapshot()
        for entry in (state or {}).get("entries") or []:
            for kw in (entry.get("keywords") or []):
                word = str(kw or "").strip()
                if _MIN_TERM_CHARS <= len(word) <= _MAX_SPAN_CHARS:
                    terms.add(word)
    except Exception:
        pass
    if not terms:
        return None
    return re.compile("|".join(re.escape(w) for w in
                               sorted(terms, key=len, reverse=True)))


def load_project_terms(project_id: str) -> list[str]:
    """项目热词表术语（渲染链带 project 上下文时接入；失败返回空）。"""
    try:
        from . import project_glossary
        terms = project_glossary.load_glossary(project_id) or []
        return [t for t in (str(x or "").strip() for x in terms)
                if _MIN_TERM_CHARS <= len(t) <= _MAX_SPAN_CHARS]
    except Exception:
        return []


def _overlap_numeric(text: str, start: int, end: int) -> bool:
    """命中区间与数字/单位 token 重叠 → 不得强调（克制规则 3）。"""
    for m in _NUM_MATCH_RE.finditer(text):
        if m.start() < end and start < m.end():
            return True
    return False


def cue_emphasis_span(text: str, extra_terms: tuple[str, ...] = (),
                      mode: str = "bold") -> tuple[int, int] | None:
    """单条 cue 文本 → 恰一个强调区间 (start, end)，无合规命中返回 None。

    规则（克制）：每条最多 1 处（取**最先出现**的合规术语，不做多处）；
    区间不得越行（跨 ``\\n`` 跳过）；不与数字/单位重叠；2-6 字。
    """
    if mode == "off" or not text:
        return None
    pat = _term_pattern()
    candidates: list[tuple[int, int]] = []
    if pat is not None:
        candidates.extend(pat.finditer(text))
    if extra_terms:
        extra = re.compile("|".join(re.escape(w) for w in
                                    sorted(set(extra_terms), key=len, reverse=True)))
        candidates.extend(extra.finditer(text))
    candidates.sort(key=lambda m: m.start())  # 首现优先
    for m in candidates:
        s, t = m.start(), m.end()
        if t - s > _MAX_SPAN_CHARS:
            continue
        if "\n" in text[s:t]:
            continue  # 跨行术语不强调
        if _overlap_numeric(text, s, t):
            continue
        return (s, t)
    return None


def apply_emphasis_tags(line_text: str, span: tuple[int, int] | None,
                        mode: str = "bold") -> str:
    """给单行文本套 ASS 行内标签（span 以本行内偏移计；None 原样返回）。"""
    if not span or mode == "off":
        return line_text
    s, t = span
    if not (0 <= s < t <= len(line_text)):
        return line_text
    if mode == "color":
        tag_open = f"{{\\1c{EMPHASIS_COLOR_ASS}}}"
        tag_close = f"{{\\1c{_RESET_COLOR_ASS}}}"
    else:
        tag_open, tag_close = "{\\b1}", "{\\b0}"
    return line_text[:s] + tag_open + line_text[s:t] + tag_close + line_text[t:]


def cue_lines_with_emphasis(cue_text: str, extra_terms: tuple[str, ...] = (),
                            mode: str = "bold") -> list[str]:
    """cue 全文（含 ``\\n`` 换行）→ 逐行套用强调标签后的行列表。

    全文恰好命中一个区间；区间落进某一行则该行套标签，其余行原样。
    """
    lines = str(cue_text).split("\n")
    if mode == "off":
        return lines
    span = cue_emphasis_span(cue_text, extra_terms, mode)
    if span is None:
        return lines
    # 区间换算到行内偏移
    offset = 0
    for i, line in enumerate(lines):
        line_start, line_end = offset, offset + len(line)
        if span[0] >= line_start and span[1] <= line_end:
            lines[i] = apply_emphasis_tags(
                line, (span[0] - line_start, span[1] - line_start), mode)
            break
        offset = line_end + 1  # +1 = 换行符
    return lines
