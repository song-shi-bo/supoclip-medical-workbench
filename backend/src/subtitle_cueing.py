"""字幕 cue 合成层：转写分段 → 符合断句/折行/冗余/时间轴规则的 cue 列表。

规则（用户需求 2026-10-29，按优先级从高到低）：
1. 断句单位：先按语音停顿（分段间隔 ≥ ``sentence_pause_ms``）和标点
   （。？！，；）切句/切子句，超长子句再在语义短语边界二次切分；禁止按
   固定字数硬切。
2. 单行长度：竖屏每行 ≤ ``SUBTITLE_MAX_LINE_CHARS``（默认 16 汉字，可配置），
   超长时在最近的语义边界换行，不在词中间断。
3. 不可拆分：数字与单位完整（「0.5公分」「0.4的厚度」）；标点不出现在行首；
   常用连接词不拆（因为/所以/这样/原则上，见 :data:`PROTECTED_WORDS`）。
4. 口语冗余（开关 ``SUBTITLE_STRIP_FILLERS``，默认开）：去叠词（因为因为→因为）
   与填充词（是这样子的/来讲等）；去重后时间码仍用原区间，不压缩。
5. 时间轴：每条显示时长 ≥ ``SUBTITLE_MIN_CUE_SECONDS``（默认 0.8s），同一屏
   最多 ``SUBTITLE_MAX_LINES``（默认 2）行；相邻条不重叠、无缝隙。
6. 标点风格：``SUBTITLE_PUNCT_STYLE`` = ``omit``（默认，句尾标点全部省略）或
   ``keep``（全部保留），全局二选一。

时间实现：token 时间按字符位置在分段区间内线性插值（分段是云端 ASR 的
utterance 级切片，片内插值误差可接受）。``build_cues_from_segments`` 输出
逐条 ``{start, end, text}``（单位秒；text 内 ``\\n`` 为屏内换行，最多
``max_lines`` 行）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace as _dc_replace

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

_DEFAULT_MAX_LINE_CHARS = 16
_DEFAULT_MAX_LINES = 2
_DEFAULT_MIN_CUE_SECONDS = 0.8
_DEFAULT_SENTENCE_PAUSE_MS = 500


@dataclass(frozen=True)
class CueConfig:
    max_line_chars: int = _DEFAULT_MAX_LINE_CHARS
    max_lines: int = _DEFAULT_MAX_LINES
    min_cue_seconds: float = _DEFAULT_MIN_CUE_SECONDS
    strip_fillers: bool = True
    punct_style: str = "omit"          # omit | keep
    sentence_pause_ms: int = _DEFAULT_SENTENCE_PAUSE_MS

    @property
    def cue_capacity(self) -> int:
        return self.max_line_chars * self.max_lines


def config_from_env(env: dict | None = None) -> CueConfig:
    e = os.environ if env is None else env

    def _int(key: str, default: int) -> int:
        try:
            return int(e.get(key, "") or default)
        except ValueError:
            return default

    def _float(key: str, default: float) -> float:
        try:
            return float(e.get(key, "") or default)
        except ValueError:
            return default

    def _bool(key: str, default: bool) -> bool:
        raw = e.get(key)
        if raw is None or raw == "":
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    style = (e.get("SUBTITLE_PUNCT_STYLE") or "omit").strip().lower()
    if style not in {"omit", "keep"}:
        style = "omit"
    return CueConfig(
        max_line_chars=max(8, _int("SUBTITLE_MAX_LINE_CHARS", _DEFAULT_MAX_LINE_CHARS)),
        max_lines=max(1, min(3, _int("SUBTITLE_MAX_LINES", _DEFAULT_MAX_LINES))),
        min_cue_seconds=max(0.0, _float("SUBTITLE_MIN_CUE_SECONDS", _DEFAULT_MIN_CUE_SECONDS)),
        strip_fillers=_bool("SUBTITLE_STRIP_FILLERS", True),
        punct_style=style,
        sentence_pause_ms=max(0, _int("SUBTITLE_SENTENCE_PAUSE_MS", _DEFAULT_SENTENCE_PAUSE_MS)),
    )


# ---------------------------------------------------------------------------
# 词法：原子 token 与保护词
# ---------------------------------------------------------------------------

# 数字与单位必须完整（规则 3）：一个正则兼顾三种形态——
# 0.5公分 / 0.4个毫米 / 12% / 100毫克 / 0.4的厚度（数值+「的」+所指属性）
_NUM_RE = re.compile(
    r"\d+(?:\.\d+)?(?:[％%]|(?:[个只条次遍针轮])?"
    r"(?:公分|毫米汞柱|毫米|厘米|公里|毫升|毫克|千克|公斤|克|吨|米|里|亩|度|岁|"
    r"年|个月|月|周|天|日|小时|分钟|秒|倍|环|期|支|片|格|mmol|mol|mg|ml|μg|ug|cm|mm|km|kg)"
    r"|的[\u4e00-\u9fa5]{1,4})?"
)
# 拉丁字母串（PET-CT / BRAF）：含内部连字符
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]*")

PROTECTED_WORDS = (
    "原则上", "正常情况下", "总体上", "因为", "所以", "这样", "但是", "不过",
    "如果", "那么", "就是", "因此", "其实", "另外", "然后", "于是", "而且",
    "并且", "或者", "还是", "一般", "目前", "现在", "确实",
)

_STRONG_PUNCT = "。！？!?…；;"
_SOFT_PUNCT = "，、：,;:"
_PUNCT_CHARS = _STRONG_PUNCT + _SOFT_PUNCT + "（）()【】《》\"\"''\"'“”‘’…—·「」『』"

# 口语填充词 / 冗余从句（规则 4，开关默认开）。顺序即应用顺序；显式清单可审计。
# 「他要看XX呢」引入从句、「原则上他要求是扩切」的代词/系动词冗余，以验收
# case（2026-10-29 用户给定）为准。条目 = (pattern, replacement)。
_FILLER_SPANS = (
    (re.compile(r"是这样子的[，,]?"), ""),
    (re.compile(r"这样子的[，,]?"), ""),
    (re.compile(r"来讲[，,]?"), ""),
    (re.compile(r"的话[，,]?"), ""),
    (re.compile(r"[他她它]要看[^，。；！？,;]{0,8}呢[，,]?"), ""),
    # 连接语后的冗余主语代词：删代词、保留连接语本身
    (re.compile(r"(原则上|其实|一般|总体上|正常情况下)[，,]?[他她它](?=要求|建议|主张|说)"),
     r"\1"),
    # 口语系动词冗余：「要求是扩切」→「要求扩切」
    (re.compile(r"(?<=要求)是(?=扩|切|做|建|保)"), ""),
)
# 叠词去重例外（叠词本身是词义，不能去）：谢谢、常用 AABB/叠音词
_REPETITION_KEEP_RE = re.compile(r"谢谢|常常|刚刚|慢慢|渐渐|往往|天天|人人|轻轻|默默|多多")
_REPEAT_RE = re.compile(r"([\u4e00-\u9fa5]{1,3})\1+")


def _dedup_repeats(text: str) -> str:
    """相邻重复去叠词：因为因为→因为、对对对对对→对、非常非常→非常。"""
    out: list[str] = []
    pos = 0
    for m in _REPETITION_KEEP_RE.finditer(text):
        out.append(_dedup_chunk(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_dedup_chunk(text[pos:]))
    return "".join(out)


def _dedup_chunk(text: str) -> str:
    prev = None
    while prev != text:
        prev = text
        text = _REPEAT_RE.sub(r"\1", text)
    return text


def strip_filler_spans(text: str) -> str:
    """口语冗余清理（规则 4）：填充词/冗余从句删除 + 叠词去重。"""
    out = text
    for pat, repl in _FILLER_SPANS:
        out = pat.sub(repl, out)
    out = _dedup_repeats(out)
    out = re.sub(r"[，,]{2,}", "，", out)      # 清理后残留的连续标点收拢
    out = re.sub(r"^[，,、]+", "", out)         # 行首残留标点
    return out


# ---------------------------------------------------------------------------
# token 化
# ---------------------------------------------------------------------------


@dataclass
class Token:
    text: str                       # 原文片段（词或单个标点）
    kind: str                       # word | punct
    soft_before: bool = False       # 词前原文位置存在软标点（，、：）
    strong_before: bool = False     # 词前存在强标点（。？！；）
    t_start: float = 0.0            # 毫秒（相对切片，段内线性插值）
    t_end: float = 0.0


def _atomic_tokens(text: str) -> list[str]:
    """无标点文本 → 原子 token：数字单位/数字的名词、拉丁串、保护词、单字。"""
    tokens: list[str] = []
    pos = 0
    n = len(text)
    protected_re = re.compile("|".join(re.escape(w) for w in PROTECTED_WORDS))
    while pos < n:
        for pat in (_NUM_RE, _LATIN_RE, protected_re):
            m = pat.match(text, pos)
            if m and m.end() > pos:
                tokens.append(m.group(0))
                pos = m.end()
                break
        else:
            tokens.append(text[pos])
            pos += 1
    return tokens


def _tokenize_segment(text: str, seg_start_ms: float, seg_end_ms: float) -> list[Token]:
    """分段文本 → token 列表；词时间按字符位置段内线性插值。"""
    span = max(1.0, seg_end_ms - seg_start_ms)

    def at(offset: float) -> float:
        return seg_start_ms + span * (offset / len(text))

    tokens: list[Token] = []
    soft_before = False
    strong_before = False
    run_start: int | None = None
    pos = 0
    n = len(text)
    while pos <= n:
        ch = text[pos] if pos < n else ""
        if pos < n and ch not in _PUNCT_CHARS:
            if run_start is None:
                run_start = pos
            pos += 1
            continue
        if run_start is not None:
            run = text[run_start:pos]
            offset = run_start
            for tok in _atomic_tokens(run):
                tokens.append(Token(
                    text=tok, kind="word",
                    soft_before=soft_before, strong_before=strong_before,
                    t_start=at(offset), t_end=at(offset + len(tok)),
                ))
                soft_before = False
                strong_before = False
                offset += len(tok)
            run_start = None
        if pos < n:
            tokens.append(Token(
                text=ch, kind="punct",
                soft_before=soft_before, strong_before=strong_before,
                t_start=at(pos), t_end=at(pos + 1),
            ))
            if ch in _STRONG_PUNCT:
                strong_before = True
            else:
                soft_before = True
        pos += 1
    return tokens


# ---------------------------------------------------------------------------
# 分句 / 子句
# ---------------------------------------------------------------------------


@dataclass
class Clause:
    tokens: list[Token] = field(default_factory=list)   # word + 尾部 punct token
    hard_start: bool = True                             # 子句前是硬边界（句首/停顿）
    tail_punct: str = ""                                # 子句尾标点（keep 风格用）

    def words(self) -> list[Token]:
        return [t for t in self.tokens if t.kind == "word"]

    def plain(self) -> str:
        return "".join(t.text for t in self.words())


def build_clauses(segments: list[dict], start_ms: float, end_ms: float, cfg: CueConfig,
                  warnings: list[str]) -> tuple[list[Clause], list[str]]:
    """窗口内分段 → 子句列表与使用的分段 id。

    硬边界（新句）：强标点（。？！；）之后、分段间隔 ≥ sentence_pause_ms。
    软边界（同句内子句）：，、：。
    """
    clauses: list[Clause] = []
    current = Clause()
    used_ids: list[str] = []
    prev_seg_end: float | None = None
    for seg in segments:
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        s0 = float(seg.get("start_ms", (seg.get("start") or 0.0) * 1000))
        s1 = float(seg.get("end_ms", (seg.get("end") or 0.0) * 1000))
        rel0, rel1 = s0 - start_ms, s1 - start_ms
        if rel1 <= 0 or rel0 >= end_ms - start_ms:
            continue
        if rel1 - rel0 < 200:
            warnings.append(f"字幕段 {seg.get('segment_id', '?')} 落在切片内不足 200ms，已跳过")
            continue
        if cfg.strip_fillers:
            text = strip_filler_spans(text)
        if not text.strip(_PUNCT_CHARS + " "):
            continue
        used_ids.append(str(seg.get("segment_id") or ""))

        seg_start = max(0.0, rel0)
        seg_end = min(end_ms - start_ms, rel1)
        if prev_seg_end is not None and (seg_start - prev_seg_end) >= cfg.sentence_pause_ms:
            if current.tokens:
                clauses.append(current)
            current = Clause()
        prev_seg_end = seg_end

        for tok in _tokenize_segment(text, seg_start, seg_end):
            current.tokens.append(tok)
            if tok.kind == "punct":
                current.tail_punct = tok.text
                if tok.text in _STRONG_PUNCT:
                    clauses.append(current)
                    current = Clause()
                elif current.words():
                    # 软标点（，、：）→ 同句内子句边界（hard_start=False 可装箱合并）
                    clauses.append(current)
                    current = Clause(hard_start=False)
    if current.tokens:
        clauses.append(current)
    return clauses, [u for u in used_ids if u]


# ---------------------------------------------------------------------------
# 子句 → cue（装箱）→ 折行 → 时间轴整平
# ---------------------------------------------------------------------------


def _plain_len(tokens: list[Token]) -> int:
    return sum(len(t.text) for t in tokens if t.kind == "word")


def _arrange_lines(group: list[Clause], cfg: CueConfig, warnings: list[str]) -> list[str] | None:
    """把子句组排进 ≤max_lines 行、每行 ≤limit；不可行返回 None。

    超行子句用剩余行预算做词级折行（预算不足则整组不可行，由装箱切断）。"""
    limit, max_lines = cfg.max_line_chars, cfg.max_lines
    lines: list[str] = []
    cur = ""
    for clause in group:
        t = clause.plain()
        if not t:
            continue
        if len(t) > limit:
            if cur:
                lines.append(cur)
                cur = ""
            remaining = max_lines - len(lines)
            if remaining <= 0:
                return None
            lines.extend(_wrap_tokens(clause.words(), limit, remaining, warnings))
            continue
        if cur and len(cur) + len(t) <= limit:
            cur += t
        else:
            if cur:
                lines.append(cur.strip())
            cur = t.strip()
    if cur:
        lines.append(cur.strip())
    if len(lines) > max_lines:
        return None
    return [ln for ln in lines if ln]


def _split_overlong_clause(clause: Clause, cfg: CueConfig,
                           warnings: list[str]) -> list[Clause]:
    """超容量单子句（无标点长句）按词边界二次切分成多个 cue 容量的子句（规则 1）。"""
    words = clause.words()
    total = sum(len(t.text) for t in words)
    if total <= cfg.cue_capacity:
        return [clause]
    warnings.append("超长子句无标点边界，已按词边界二次切分（语义边界启发式）")
    parts: list[list[Token]] = []
    cur: list[Token] = []
    cur_len = 0
    for tok in words:
        tl = len(tok.text)
        if cur and cur_len + tl > cfg.cue_capacity:
            parts.append(cur)
            cur, cur_len = [], 0
        cur.append(tok)
        cur_len += tl
    if cur:
        parts.append(cur)
    out = [Clause(tokens=p, hard_start=(clause.hard_start if i == 0 else False))
           for i, p in enumerate(parts)]
    return out


def _pack_clauses(clauses: list[Clause], cfg: CueConfig, warnings: list[str]) -> list[list[Clause]]:
    """子句装箱成 cue：以「可排进 max_lines×limit」为可行性判据；跨硬边界不合并。"""
    expanded: list[Clause] = []
    for clause in clauses:
        expanded.extend(_split_overlong_clause(clause, cfg, warnings))
    groups: list[list[Clause]] = []
    run: list[Clause] = []
    for clause in expanded:
        if not clause.words():
            continue
        if run and (clause.hard_start or _arrange_lines(run + [clause], cfg, warnings) is None):
            groups.append(run)
            run = [clause]
        else:
            run = run + [clause]
    if run:
        groups.append(run)
    return groups


def _wrap_tokens(tokens: list[Token], limit: int, max_lines: int,
                 warnings: list[str]) -> list[str]:
    """词 token 折行：每行 ≤ limit、行数 ≤ max_lines、不在词中间断、
    语义边界偏好（保护词/数字起行加分；悬垂副词收尾、助词起行减分）。"""
    total = _plain_len(tokens)
    if total <= limit:
        return ["".join(t.text for t in tokens).strip()]
    best = None
    for k in range(1, len(tokens)):
        l1 = _plain_len(tokens[:k])
        l2 = total - l1
        if l1 > limit or l2 > limit:
            continue
        nxt = tokens[k].text
        bonus = 2 if (nxt and (nxt[0].isdigit() or any(nxt.startswith(w) for w in PROTECTED_WORDS))) else 0
        score = abs(l1 - l2) - bonus
        # 悬垂副词收尾（他也 /…）与助词起行（…/的 X）观感差，惩罚
        if tokens[k - 1].text in {"也", "就", "都", "还", "才", "又", "再"}:
            score += 3
        if nxt and nxt[0] in "的地得了呢吧吗啊":
            score += 2
        if best is None or score < best[0]:
            best = (score, k)
    if best is not None and max_lines >= 2:
        k = best[1]
        return ["".join(t.text for t in tokens[:k]).strip(),
                "".join(t.text for t in tokens[k:]).strip()]
    # 兜底：均分行（词边界），超长单 token 允许按字符断并记 warning
    warnings.append(f"子句 {total} 字超出 {max_lines}x{limit} 行容量，已按词边界均分行")
    chunks: list[list[Token]] = []
    cur: list[Token] = []
    cur_len = 0
    for tok in tokens:
        tl = len(tok.text)
        if tl > limit:
            if cur:
                chunks.append(cur)
                cur, cur_len = [], 0
            for i in range(0, tl, limit):
                chunks.append([_dc_replace(tok, text=tok.text[i:i + limit])])
            continue
        if cur_len + tl > limit and cur:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(tok)
        cur_len += tl
    if cur:
        chunks.append(cur)
    per = max(1, (len(chunks) + max_lines - 1) // max_lines)
    return ["".join(t.text for g in chunks[i:i + per] for t in g).strip()
            for i in range(0, len(chunks), per)]


def _clause_span(clauses: list[Clause]) -> tuple[float, float]:
    ts = [t for c in clauses for t in c.tokens]
    return min(t.t_start for t in ts), max(t.t_end for t in ts)


def _cue_text(group: list[Clause], cfg: CueConfig, warnings: list[str]) -> str:
    """cue 文本：子句贪心填行（子句边界=首选断点），单子句超行时词级折行。"""
    lines = _arrange_lines(group, cfg, warnings)
    if lines is None:
        # 装箱已保证可行；这里兜底防回归
        warnings.append("cue 行不可排布，回退词边界均分行")
        all_words: list[Token] = [t for c in group for t in c.words()]
        lines = _wrap_tokens(all_words, cfg.max_line_chars, cfg.max_lines, warnings)
    text = "\n".join(lines)
    if cfg.punct_style == "keep":
        tail = group[-1].tail_punct
        if tail and tail in _STRONG_PUNCT:
            text += tail
    return text


def build_cues_from_segments(segments: list[dict], start_ms: float, end_ms: float,
                             cfg: CueConfig | None = None,
                             ) -> tuple[list[dict], list[str], list[str]]:
    """窗口内转写分段 → (cues, used_segment_ids, warnings)。

    cue: ``{"start": 秒, "end": 秒, "text": "行1\\n行2"}``；相邻条不重叠、无缝隙。
    """
    cfg = cfg or config_from_env()
    warnings: list[str] = []
    window = max(1.0, float(end_ms) - float(start_ms))
    clauses, used_ids = build_clauses(segments, float(start_ms), float(end_ms), cfg, warnings)

    cues: list[dict] = []
    for group in _pack_clauses(clauses, cfg, warnings):
        text = _cue_text(group, cfg, warnings)
        if not text:
            continue
        t0, t1 = _clause_span(group)
        cues.append({"start": t0 / 1000.0, "end": t1 / 1000.0, "text": text})

    cues.sort(key=lambda c: (c["start"], c["end"]))
    _tile_cues(cues, cfg, warnings, window)
    return cues, used_ids, warnings


def _tile_cues(cues: list[dict], cfg: CueConfig, warnings: list[str], window: float) -> None:
    """时间轴整平（规则 5）：去重叠（钳制）、补缝隙（前条延伸）、短条并入相邻条。"""
    if not cues:
        return
    min_s = cfg.min_cue_seconds
    # 1) 去重叠：后条起点钳到前条终点
    for i in range(1, len(cues)):
        if cues[i]["start"] < cues[i - 1]["end"]:
            cues[i]["start"] = cues[i - 1]["end"]
            cues[i]["end"] = max(cues[i]["end"], cues[i]["start"])
    # 2) 补缝隙：前条终点延伸到后条起点（显示无缝）
    for i in range(1, len(cues)):
        if cues[i]["start"] - cues[i - 1]["end"] > 0.001:
            cues[i - 1]["end"] = cues[i]["start"]

    # 3) 短条处理：并入相邻条（文本合并不超容量才并），否则原地延伸
    def cap_ok(a: dict, b: dict) -> bool:
        joined = (a["text"] + b["text"]).replace("\n", "")
        return _visible_len(joined) <= cfg.cue_capacity + 1

    i = 0
    while i < len(cues):
        dur = cues[i]["end"] - cues[i]["start"]
        if dur >= min_s - 1e-6 or len(cues) == 1:
            i += 1
            continue
        prev_ok = i > 0 and cap_ok(cues[i - 1], cues[i])
        next_ok = i + 1 < len(cues) and cap_ok(cues[i], cues[i + 1])
        if next_ok and (not prev_ok or
                        (cues[i + 1]["end"] - cues[i + 1]["start"]) <=
                        (cues[i - 1]["end"] - cues[i - 1]["start"])):
            cues[i]["text"] = _join_texts(cues[i]["text"], cues[i + 1]["text"], cfg)
            cues[i]["end"] = cues[i + 1]["end"]
            del cues[i + 1]
        elif prev_ok:
            cues[i - 1]["text"] = _join_texts(cues[i - 1]["text"], cues[i]["text"], cfg)
            cues[i - 1]["end"] = cues[i]["end"]
            del cues[i]
        else:
            cues[i]["end"] = min(window, cues[i]["start"] + min_s)
            warnings.append(f"字幕条 {i + 1} 短于 {min_s}s 且无法合并，已原地延伸")
            i += 1
    # 4) 延伸产生的重叠/缝隙再收一次
    for i in range(1, len(cues)):
        if cues[i]["start"] < cues[i - 1]["end"]:
            cues[i - 1]["end"] = cues[i]["start"]


def _join_texts(a: str, b: str, cfg: CueConfig) -> str:
    """合并两条 cue 文本：优先行级拼接（a 末行 + b 首行，不新增行数），
    其次行堆叠；兜底单行串接。"""
    la, lb = a.split("\n"), b.split("\n")
    merged = la[:-1] + [la[-1] + lb[0]] + lb[1:]
    if (len(merged) <= cfg.max_lines
            and all(len(x) <= cfg.max_line_chars for x in merged)):
        return "\n".join(merged)
    if len(la) + len(lb) <= cfg.max_lines:
        return "\n".join(la + lb)
    return a + b


# ---------------------------------------------------------------------------
# 烧录样式固化（用户规范 2026-10-29）：白字 + 纯黑 2px 描边，无阴影。
# Style 行：BorderStyle=1（描边模式）/ Outline=2 / Shadow=0；
# PlayRes 与视频像素 1:1（1080x1920），Outline=2 即字面 2px。
# 此样式是硬字幕烧录的唯一规范样式；studio 金黄字幕模板（subtitle_compose）
# 是独立可配置功能，不受本规范约束。
# ---------------------------------------------------------------------------

BURN_PLAYRES_W, BURN_PLAYRES_H = 1080, 1920
BURN_FONT_NAME = "WenQuanYi Zen Hei"
BURN_FONT_SIZE_PX = 63
BURN_OUTLINE_PX = 2
BURN_SHADOW = 0
BURN_BORDER_STYLE = 1
BURN_MARGIN_V_PX = 160
BURN_PRIMARY_COLOR = "&H00FFFFFF"   # 白
BURN_OUTLINE_COLOR = "&H00000000"   # 纯黑
BURN_BACK_COLOR = "&H00000000"

_BURN_STYLE_LINE = (
    f"Style: Sub,{BURN_FONT_NAME},{BURN_FONT_SIZE_PX},"
    f"{BURN_PRIMARY_COLOR},&H00000000,{BURN_OUTLINE_COLOR},{BURN_BACK_COLOR},"
    f"0,0,0,0,100,100,0,0,{BURN_BORDER_STYLE},{BURN_OUTLINE_PX},{BURN_SHADOW},2,0,0,{BURN_MARGIN_V_PX},1"
)

_BURN_ASS_HEADER = f"""[Script Info]
; 字幕烧录规范（2026-10-29 固化）：白字 + 纯黑 2px 描边（BorderStyle=1/Outline=2），无阴影（Shadow=0）
ScriptType: v4.00+
PlayResX: {BURN_PLAYRES_W}
PlayResY: {BURN_PLAYRES_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{_BURN_STYLE_LINE}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def hardburn_enabled(env: dict | None = None) -> bool:
    """渲染时是否直接把固化样式字幕烧进成片（默认开，`SUBTITLE_HARDBURN=false` 关闭）。"""
    e = os.environ if env is None else env
    raw = e.get("SUBTITLE_HARDBURN")
    if raw is None or raw == "":
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}

_SRT_TS_RE = re.compile(r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})")


def _srt_ts_to_ass(ts: str) -> str:
    m = _SRT_TS_RE.match(ts.strip())
    if not m:
        return "0:00:00.00"
    h, mi, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4).ljust(3, "0"))
    return f"{h}:{mi:02d}:{s:02d}.{ms // 10:02d}"


def srt_to_styled_ass(srt_text: str) -> str:
    """规范 SRT（cue 合成产物，含预折行）→ 固化样式 ASS 文本。

    纯 Python 解析，不依赖 ffmpeg；样式全部来自 :data:`_BURN_ASS_HEADER`
    （白字 / 纯黑 Outline=2 / Shadow=0 / BorderStyle=1，PlayRes 1080x1920）。
    """
    dialogues: list[str] = []
    for block in re.split(r"\n\s*\n", srt_text.strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        t0, t1 = lines[1].split("-->")
        text = "\\N".join(ln.strip() for ln in lines[2:])
        dialogues.append(
            f"Dialogue: 0,{_srt_ts_to_ass(t0)},{_srt_ts_to_ass(t1)},Sub,,0,0,0,,{text}")
    return _BURN_ASS_HEADER + "\n".join(dialogues) + "\n"


def _visible_len(text: str) -> int:
    """近似显示宽度：CJK/全角计 1，其余计 0.6。"""
    return round(sum(1 if ord(ch) > 0x2E80 else 0.6 for ch in text))
