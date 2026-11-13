"""医学指南库（第三库）· 快照加载与条目匹配（2026-11-10 阶段一：文件快照版）。

定位（用户 2026-11-10 拍板的四项职责）：
1. 找出视频中与医学主题匹配的片段（向量 + 词法混合匹配）；
2. 标注片段是否带前提 / 适用范围 / 风险边界（条目结构化字段，随匹配透出）；
3. 为「哪些片段不得拼接 / 不得入选」提供依据（消费方 clip_medical_gate.enforce_clips）；
4. 在多候选择优时提供医学相关度依据。

硬边界（与规划模块同一红线）：
- **不生成新诊疗意见**：本模块只做匹配打分与条目透出，audience_note 仅参与匹配，
  绝不写入字幕 / 文案；条目 statement 不注入规划提示词（提示词A「信息忠实」约束：
  规划器只允许引用转写文本，背景只能是对转写的程序标注）。
- 快照是本地确定性文件：规划阶段不联网、不连库；入库 / 检索 / 复核走
  ``api/routes/medical_kb.py``（token 鉴权）+ ``scripts/materialize_medical_kb.py``。
- 任何失败（文件缺失 / 损坏 / 向量模型不可用）→ 闸门降级（词法或禁用），绝不阻断任务。

指纹契约：``snapshot_identity()["snapshot_sha256"]`` 必须进 plan provenance
（clip_planning.build_current_plan_provenance.rules.medical_gate）——库变了旧计划缓存失效。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from . import kb_embedding

GATE_VERSION = "medical-gate/v1"

# 匹配阈值（可通过环境变量调整；进 provenance，改了即缓存失效）。
# WEAK 默认 0.55：bge-small-zh 对**无关**中文短文本的余弦常落在 0.45~0.55，
# 低于 0.55 会把库外领域（如高血压视频 vs 黑色素瘤库）误判为弱命中。
STRONG_THRESHOLD = max(0.0, min(1.0, float(os.getenv("MEDICAL_GATE_STRONG", "0.62"))))
WEAK_THRESHOLD = max(0.0, float(os.getenv("MEDICAL_GATE_WEAK", "0.55")))
# 词法命中占比的折算上限：word hit 全中时词法单项得分（vec 路径不可用时的主力）。
LEXICAL_WEIGHT = max(0.0, min(1.0, float(os.getenv("MEDICAL_GATE_LEXICAL_WEIGHT", "0.8"))))
# 主题块级词法得分归一化：全库**去重**关键词命中 N 个记满分。真实医学对话
# 会在多条目上命中一组词（≥8 个很常见），闲聊文本命中为 0——区分度远高于
# 单条目占比（单条目占比受该条目词表大小影响）。
LEXICAL_HITS_NORM = max(1.0, float(os.getenv("MEDICAL_GATE_LEXICAL_HITS_NORM", "8")))

# ---------------------------------------------------------------------------
# 医学背景注入（2026-11-13 设计稿 docs/medical-background-injection-design.md）：
# 闸门命中卡片的结论/边界以受控背景块注入提示词A用户提示（拼装层），供模型
# 理解医学主题。默认 off（MEDICAL_BG_INJECT=on 开启）；块首自带「禁止复制到
# 任何输出」指令；输出侧另有泄漏扫描（clip_planning._scan_background_leak）。
# ---------------------------------------------------------------------------

BACKGROUND_TPL_VERSION = "medical-bg-tpl/1"
_BG_STATEMENT_MAX_CHARS = 120
_BG_BOUNDARY_MAX_CHARS = 60
_BG_DEFAULT_BUDGET = 1200
_BG_HEADER = ("【医学背景（仅供理解内容主题与医学逻辑，禁止复制其文字到任何输出；"
              "主题/钩子/摘要仍只能来自转写原文）】")


def background_config(env: dict[str, str] | None = None) -> dict[str, Any]:
    """背景注入配置：默认 off；非法值一律按 off/默认值处理。"""
    e = os.environ if env is None else env

    def _bool(raw: Any, default: bool = False) -> bool:
        if raw is None or str(raw) == "":
            return default
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def _int(raw: Any, default: int) -> int:
        try:
            return max(0, int(str(raw)))
        except (TypeError, ValueError):
            return default

    return {
        "inject": _bool(e.get("MEDICAL_BG_INJECT"), False),
        "max_entries": max(1, _int(e.get("MEDICAL_BG_MAX_ENTRIES"), 9)),
        "per_unit": max(1, _int(e.get("MEDICAL_BG_PER_UNIT"), 3)),
        "budget": max(200, _int(e.get("MEDICAL_BG_BUDGET"), _BG_DEFAULT_BUDGET)),
        "leak_threshold": max(4, _int(e.get("MEDICAL_BG_LEAK_THRESHOLD"), 12)),
    }


def _short_source(source_name: str) -> str:
    if "卫健委" in source_name:
        return "国家卫健委2018规范"
    if "PDQ" in source_name:
        return "NCI PDQ"
    if "Moles" in source_name or "fact" in source_name.lower():
        return "NCI事实页"
    if "MedlinePlus" in source_name:
        return "MedlinePlus"
    return source_name[:12]


def build_background_block(annotations: list[dict[str, Any]] | None,
                           env: dict[str, str] | None = None,
                           snapshot_path: Path | None = None,
                           ) -> tuple[str, list[str], list[str]]:
    """闸门标注 + 快照条目 → 背景块。

    返回 ``(块文本或空串, 注入 statements, 注入 entry_ids)``。每主题块取
    ``per_unit`` 条命中条目（按闸门排序），全片去重上限 ``max_entries``，
    顺序装配、预算尽即停（高分单元在前）。关闭/无命中/快照缺失 → 空串。
    确定性纯函数：同输入（标注+环境）必产同块——expected/actual 两边
    provenance 的 prompt_sha256 因此逐字节一致。"""
    cfg = background_config(env)
    empty = ("", [], [])
    if not cfg["inject"] or not annotations:
        return empty
    try:
        snap = load_snapshot(snapshot_path) or {}
    except Exception:
        return empty
    entries_by_id = {str(x.get("id") or ""): x for x in (snap.get("entries") or [])}
    if not entries_by_id:
        return empty

    seen: list[str] = []
    ordered: list[tuple[str, str, str, str]] = []   # (unit_title, line, statement, entry_id)
    for unit in annotations:
        ids: list[str] = []
        for m in (unit.get("matched") or []):
            eid = str(m.get("id") or "")
            if eid and eid in entries_by_id and eid not in ids and eid not in seen:
                ids.append(eid)
            if len(ids) >= cfg["per_unit"]:
                break
        title = str(unit.get("title") or "").strip()[:24] or "相关主题"
        for eid in ids:
            seen.append(eid)
            entry = entries_by_id[eid]
            st = str(entry.get("statement") or "").strip()[:_BG_STATEMENT_MAX_CHARS]
            rb = str(entry.get("risk_boundary") or "").strip()[:_BG_BOUNDARY_MAX_CHARS]
            line = f"  - ({eid}/{_short_source(str(entry.get('source_name') or ''))}) {st}"
            if rb:
                line += f" 边界提示：{rb}"
            ordered.append((title, line, st, eid))
    if not ordered:
        return empty

    block_lines = [_BG_HEADER]
    budget = cfg["budget"] - len(_BG_HEADER)
    statements: list[str] = []
    used_ids: list[str] = []
    last_title: str | None = None
    for title, line, st, eid in ordered:
        add = [line] if title == last_title else [f"· 主题「{title}」相关背景：", line]
        cost = sum(len(x) + 1 for x in add)
        if cost > budget:
            break
        block_lines.extend(add)
        budget -= cost
        statements.append(st)
        used_ids.append(eid)
        last_title = title
    if len(block_lines) <= 1:
        return empty
    return ("\n".join(block_lines), statements, used_ids)

_lock = threading.Lock()
_cache: dict[str, dict[str, Any]] = {}  # snapshot_path -> {"mtime_ns","identity","state"}


def default_snapshot_path() -> Path:
    custom = os.getenv("MEDICAL_KB_SNAPSHOT")
    if custom:
        return Path(custom)
    return Path(__file__).resolve().parent.parent / "medical_kb_seed" / "active_snapshot.json"


def _entry_text(entry: dict[str, Any]) -> str:
    """条目向量文本：statement + audience_note + 关键词（截 2000 字，与双库同策略）。"""
    parts = [str(entry.get("statement") or ""),
             str(entry.get("audience_note") or ""),
             " ".join(str(k) for k in (entry.get("keywords") or []))]
    return "\n".join(p for p in parts if p.strip())[:2000]


def load_snapshot(path: Path | None = None) -> dict[str, Any] | None:
    """加载快照（按路径 + mtime 缓存）；缺失 / 损坏 → None（闸门禁用，如实标注）。"""
    target = path or default_snapshot_path()
    try:
        mtime_ns = target.stat().st_mtime_ns
    except OSError:
        return None
    cached = _cache.get(str(target))
    if cached and cached["mtime_ns"] == mtime_ns:
        return cached["state"]
    with _lock:
        cached = _cache.get(str(target))
        if cached and cached["mtime_ns"] == mtime_ns:
            return cached["state"]
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            entries = [e for e in raw.get("entries", []) if str(e.get("status") or "approved") == "approved"]
            # 指纹按**实际加载的 approved 条目**现算（不信任文件里的声明值）：
            # 手改快照 / 库外编辑只要内容变了，指纹就变，旧计划缓存自动失效。
            canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"))
            state: dict[str, Any] = {
                "path": str(target),
                "snapshot_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
                "declared_sha256": str(raw.get("meta", {}).get("snapshot_sha256") or ""),
                "entries": entries,
                "entry_vectors": None,
            }
        except (OSError, ValueError):
            state = None  # type: ignore[assignment]
        _cache[str(target)] = {"mtime_ns": mtime_ns, "state": state}
        return state


def snapshot_identity(path: Path | None = None) -> dict[str, Any]:
    """闸门身份（进 plan 指纹）：版本 + 快照指纹 + 阈值；快照缺失时 enabled=False。"""
    state = load_snapshot(path)
    return {
        "enabled": state is not None,
        "gate_version": GATE_VERSION,
        "snapshot_sha256": state["snapshot_sha256"] if state else "",
        "entry_count": len(state["entries"]) if state else 0,
        "strong_threshold": STRONG_THRESHOLD,
        "weak_threshold": WEAK_THRESHOLD,
        "lexical_weight": LEXICAL_WEIGHT,
    }


def _ensure_vectors(state: dict[str, Any]) -> list[list[float]] | None:
    """条目向量（按快照指纹缓存）；fastembed 不可用 → None（调用方降级词法）。"""
    if state["entry_vectors"] is not None or state.get("vector_tried"):
        return state["entry_vectors"]
    with _lock:
        if state.get("vector_tried"):
            return state["entry_vectors"]
        state["vector_tried"] = True
        vectors = kb_embedding.embed_texts([_entry_text(e) for e in state["entries"]])
        state["entry_vectors"] = vectors
        return vectors


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def lexical_hits_distinct(text: str, entries: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """全库去重关键词命中数 + 命中条目 id（主题块级词法得分用）。

    真实医学对话会同时命中多条目的一组词（ABCDE、活检、复查…），去重计数
    归一化后区分度高；闲聊文本命中为 0。
    """
    lowered = text.lower()
    seen: set[str] = set()
    hit_entries: list[str] = []
    for entry in entries:
        words = [str(k).lower() for k in (entry.get("keywords") or []) if str(k).strip()]
        hits_here = [w for w in words if w in lowered]
        if hits_here:
            hit_entries.append(str(entry.get("id") or ""))
            seen.update(hits_here)
    return len(seen), hit_entries[:4]


def lexical_ratio(text: str, entries: list[dict[str, Any]]) -> tuple[float, list[str]]:
    """词法命中占比：命中的条目关键词数 / 全库关键词总数，命中条目一并返回。

    全库关键词动辄数百个，直接拿占比做分会偏小；这里按「有命中的条目内
    关键词命中率」取最大值——一篇讲 ABCDE 的对话应把对应条目打满。
    """
    lowered = text.lower()
    best_ratio = 0.0
    hit_entries: list[str] = []
    for entry in entries:
        words = [str(k).lower() for k in (entry.get("keywords") or []) if str(k).strip()]
        if not words:
            continue
        hits = sum(1 for w in words if w in lowered)
        if hits:
            hit_entries.append(str(entry.get("id") or ""))
            best_ratio = max(best_ratio, hits / len(words))
    return best_ratio, hit_entries[:4]


def match_text(text: str, state: dict[str, Any]) -> dict[str, Any]:
    """单段文本 → 匹配结果 {score, level, matched}。score = max(向量相似度, 词法折算)。"""
    lowered = text[:2000]
    distinct_hits, lex_hits = lexical_hits_distinct(lowered, state["entries"])
    lex_component = min(LEXICAL_WEIGHT, (distinct_hits / LEXICAL_HITS_NORM) * LEXICAL_WEIGHT)
    vector_sim = 0.0
    best_entry_id = ""
    vectors = _ensure_vectors(state)
    query = kb_embedding.embed_texts([lowered]) if vectors else None
    if vectors and query and query[0]:
        best_index, vector_sim = -1, 0.0
        for position, vec in enumerate(vectors):
            sim = _cosine(query[0], vec)
            if sim > vector_sim:
                best_index, vector_sim = position, sim
        if best_index >= 0 and vector_sim >= WEAK_THRESHOLD:
            best_entry_id = str(state["entries"][best_index].get("id") or "")
    score = max(vector_sim, lex_component)
    level = "strong" if score >= STRONG_THRESHOLD else ("weak" if score >= WEAK_THRESHOLD else "none")
    return {"score": round(score, 4), "level": level,
            "vector_sim": round(vector_sim, 4),
            "lexical_hits": distinct_hits,
            "vector_entry_id": best_entry_id,
            "lexical_entry_ids": lex_hits}


def matched_entries_for(state: dict[str, Any], result: dict[str, Any],
                        limit: int = 2) -> list[dict[str, Any]]:
    """匹配结果 → 透出的条目摘要（id/主题路径末级/类型/前提有无/风险有无）。"""
    by_id = {str(e.get("id") or ""): e for e in state["entries"]}
    ids = list(result.get("lexical_entry_ids") or [])
    vector_id = str(result.get("vector_entry_id") or "")
    if vector_id and vector_id not in ids:
        ids.insert(0, vector_id)
    out: list[dict[str, Any]] = []
    for entry_id in ids[:limit]:
        entry = by_id.get(entry_id)
        if not entry:
            continue
        topic_path = [str(p) for p in (entry.get("topic_path") or [])]
        out.append({
            "id": entry_id,
            "topic": topic_path[-1] if topic_path else (entry.get("entry_type") or ""),
            "entry_type": entry.get("entry_type") or "",
            "has_prerequisites": bool(str(entry.get("prerequisites") or "").strip()),
            "has_risk_boundary": bool(str(entry.get("risk_boundary") or "").strip()),
            "source_name": entry.get("source_name") or "",
        })
    return out
