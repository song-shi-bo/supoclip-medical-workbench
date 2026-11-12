"""钩子模式库（2026-11-11 问答化·逐字前提，用户拍板：钩子必须逐字来自转写）。

模式库只存「开头手法结构」（从 16 条参考转写开头人工提炼），**不含任何参考素材
原句**——加载器不读取 reference_hint 示例字段，结构上保证示例文本不可能进入
任何提示词（防抄袭/防同质化/防串味）。

角色分工（用户 2026-11-11 定稿）：
- **模式库 = 筛选标尺**：对本片转写逐句做结构归类打分（提问/数字/反差/点名/案例…），
  产出每条切片的「逐字钩子候选清单（已排序）」；
- **提示词A = 确认者**：从候选中选定钩子（A 本来就要求钩子只能用切片内信息）；
- **执法 = 逐字比对**：计划产出的 hook_first_line 必须与转写某句逐字一致
  （去标点归一后包含校验），不一致 → 替换为排序第一的候选 + warning 留痕。

身份契约：``identity()`` 基于**模式库文件内容 sha256**，确定性可复现——worker
expected 与 plan_clips 内部两次构建必然相等（E_CACHE_CONTEXT_CHANGED 契约）；
模式库文件变化 → 旧计划缓存自动失效。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

HOOK_PATTERNS_VERSION_KEY = "version"

_MAX_HOOK_CHARS = 30

_PUNCT_RE = re.compile(r"[，。！？；：、,.!?;:\s\[\]（）()\"\"''「」…—~～\-]")
_DIGIT_RE = re.compile(r"(一|二|两|三|四|五|六|七|八|九|十|\d)\s*(个|大|条|种|点|招|次|毫米|公分|月)")
_CONTRAST_RE = re.compile(r"(很多人以为|其实|不是[^，。；]{1,12}(而是|而是)|却|反而|千万别|别再)")
_CONSEQUENCE_RE = re.compile(r"(癌变|去世|致命|风险|危险|错过|别等|敲门|求救|转移|晚期|警惕)")
_AUDIENCE_RE = re.compile(r"(岁的|上了年纪|老人|家长|女性|男性|患者|家人们|你|你们)")

_lock = threading.Lock()
_cache: dict[str, Any] = {}


def default_library_path() -> Path:
    custom = os.getenv("HOOK_PATTERNS_PATH")
    if custom:
        return Path(custom)
    return Path(__file__).resolve().parent.parent / "hook_patterns" / "hook_patterns.json"


def load_library(path: Path | None = None) -> dict[str, Any] | None:
    """加载模式库（按路径+mtime 缓存）；缺失/损坏 → None（钩子注入降级禁用）。"""
    target = path or default_library_path()
    key = str(target)
    try:
        mtime_ns = target.stat().st_mtime_ns
    except OSError:
        return None
    cached = _cache.get(key)
    if cached and cached["mtime_ns"] == mtime_ns:
        return cached["state"]
    with _lock:
        cached = _cache.get(key)
        if cached and cached["mtime_ns"] == mtime_ns:
            return cached["state"]
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            patterns = [p for p in raw.get("patterns", [])
                        if str(p.get("status") or "active") == "active"]
            state = {"raw": raw, "patterns": patterns}
        except (OSError, ValueError):
            state = None
        _cache[key] = {"mtime_ns": mtime_ns, "state": state}
        return state


def library_sha256(path: Path | None = None) -> str | None:
    state = load_library(path)
    if state is None:
        return None
    canonical = json.dumps(
        [{"id": p.get("id"), "structure": p.get("structure"),
          "mechanisms": p.get("mechanisms"), "status": p.get("status")}
         for p in state["patterns"]],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def identity(path: Path | None = None) -> dict[str, Any]:
    """钩子模式库身份（进 plan 指纹）：确定性——同一文件两次构建必然相等。"""
    sha = library_sha256(path)
    return {
        "enabled": sha is not None,
        "sha256": sha or "",
        "max_hook_chars": _MAX_HOOK_CHARS,
    }


def _norm(text: str) -> str:
    return _PUNCT_RE.sub("", str(text or ""))


def score_sentence(text: str) -> list[tuple[str, float]]:
    """逐句结构打分：返回 [(模式id, 命中分0~1)]，按分降序。P7 比喻仅人工。"""
    cleaned = _norm(text)
    if not cleaned:
        return []
    hits: dict[str, float] = {}
    if re.search(r"？|\b为什么\b|怎么", cleaned):
        hits["P4"] = hits.get("P4", 0) + 0.6
    if _DIGIT_RE.search(cleaned):
        hits["P3"] = hits.get("P3", 0) + 0.7
    if _CONTRAST_RE.search(cleaned):
        hits["P2"] = hits.get("P2", 0) + 0.5
    if _CONSEQUENCE_RE.search(cleaned):
        hits["P1"] = hits.get("P1", 0) + 0.5
    if _AUDIENCE_RE.search(cleaned[:14]):
        hits["P6"] = hits.get("P6", 0) + 0.4
    if re.search(r"(我|我们).{0,14}(癌症|癌|肿瘤|得了)", cleaned):
        hits["P5"] = hits.get("P5", 0) + 0.5
    ranked = sorted(hits.items(), key=lambda kv: kv[1], reverse=True)
    return [(pid, round(min(1.0, s), 3)) for pid, s in ranked]


def rank_candidates(texts: list[str], top: int = 8) -> list[dict[str, Any]]:
    """候选句打分排序：逐字原句 → 模式标签 + 分数 + 用途标注。"""
    state = load_library()
    known = {p["id"] for p in (state or {}).get("patterns", [])} if state else set()
    candidates: list[dict[str, Any]] = []
    for text in texts:
        cleaned_len = len(_norm(text))
        if not 4 <= cleaned_len <= _MAX_HOOK_CHARS + 6:
            continue
        scored = score_sentence(text)
        best_pid, best_score = (scored[0] if scored else ("", 0.0))
        candidates.append({
            "text": str(text).strip(),
            "pattern": best_pid if best_pid in known else "",
            "pattern_name": "",
            "score": best_score,
            "all_scores": scored,
        })
    name_of = {p["id"]: p.get("name") or "" for p in (state or {}).get("patterns", [])} \
        if state else {}
    for c in candidates:
        c["pattern_name"] = name_of.get(c["pattern"], "")
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:max(1, top)]
