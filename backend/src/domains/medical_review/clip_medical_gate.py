"""医学知识匹配与规划闸门：分析阶段贴标签，规划阶段做取舍。

用户 2026-11-10 三项拍板：
- 执法强度：**硬剔除**——医学密度不达标的切片（如医生患者聊家常）规划后由程序
  移入 ``discarded_segments``（type=非医学闲聊），宁缺毋滥；
- 指南来源：公开权威来源（NCI PDQ 患教/指南摘要）整理的结构化种子条目；
- 生效策略：**直接生效 + 事后复核**（条目 review_status=pending_review，可事后复核）。

职责映射（对应指南库四项职责）：
1. 找医学主题片段 → :func:`run_gate` 对 topic_units 逐块匹配（向量+词法）；
2. 前提/适用范围/风险边界 → 匹配条目的结构化字段随 ``matched`` 透出（只标注，
   绝不写入字幕/文案——与提示词B只删不改契约无交集）；
3. 拼接门控 → :func:`enforce_clips` 规划后执法；前置/风险句吸附属阶段二；
4. 候选择优 → 切片 ``medical_gate`` 标注供排序/展示消费。

**领域激活规则（防误伤）**：只有全片至少一个主题块 **strong** 级命中指南才进入
``enforce`` 模式；weak 级命中（可能是库外领域的语义邻近）与零命中一样走
``annotate_only``——库不覆盖该视频领域时无判断依据，只标注不剔除
（避免把库外领域的合法医学视频全片杀光）。

红线：不生成新诊疗意见；条目文本不进规划提示词（提示词A「信息忠实」——
拼装层只透出对转写分段的程序标注标签）；闸门任何失败降级不阻断任务。
"""
from __future__ import annotations

import os
from typing import Any

from . import medical_kb

PROFILE_VERSION = "medical-gate-profile/v1"
# 版本别名：clip_planning 指纹规则与调用方引用 med_gate.GATE_VERSION / 阈值常量
# （与 medical_kb 同源，闸门版本与阈值随检索模块走）。
GATE_VERSION = medical_kb.GATE_VERSION
STRONG_THRESHOLD = medical_kb.STRONG_THRESHOLD
WEAK_THRESHOLD = medical_kb.WEAK_THRESHOLD
LEXICAL_WEIGHT = medical_kb.LEXICAL_WEIGHT

# 硬剔除阈值：切片内"医学分段"占比低于该值且主题块级别为 none → 剔除。
DISCARD_BELOW = max(0.0, min(1.0, float(os.getenv("MEDICAL_GATE_DISCARD_BELOW", "0.2"))))
DISCARD_TYPE = "非医学闲聊"


def current_identity() -> dict[str, Any]:
    """当前医学知识匹配身份，供内容分析 provenance 冻结。

    规划 provenance 不调用本函数，只读取分析工件中已持久化的 identity。
    """
    try:
        return medical_kb.snapshot_identity()
    except Exception:  # noqa: BLE001 — 身份获取失败按禁用降级，绝不阻断规划
        return {"enabled": False, "gate_version": medical_kb.GATE_VERSION,
                "snapshot_sha256": "", "entry_count": 0}


def _unit_text(unit: dict[str, Any], segment_text_of: dict[str, str]) -> str:
    """主题块匹配文本：标题 + 钩子/收尾引用 + 首末分段原文。"""
    ids = [str(s) for s in (unit.get("segment_ids") or [])]
    parts = [str(unit.get("title") or ""), str(unit.get("hook_quote") or ""),
             str(unit.get("ending_quote") or "")]
    for sid in (ids[:2] + ids[-2:]):
        text = segment_text_of.get(sid)
        if text:
            parts.append(text)
    return "\n".join(p for p in parts if p.strip())[:2000]


def analyze_topics(corrected_segments: list[dict[str, Any]],
                   analysis: dict[str, Any]) -> dict[str, Any]:
    """分析增强：逐主题块医学匹配 + 逐分段词法标注。

    这里只描述原视频中的医学相关性，不决定候选切片的保留、淘汰或发布。
    任何失败均降级为 disabled，绝不阻断内容分析。
    """
    profile: dict[str, Any] = {
        "profile_version": PROFILE_VERSION,
        "gate_version": medical_kb.GATE_VERSION,
        "identity": {"enabled": False, "gate_version": medical_kb.GATE_VERSION},
        "enabled": False,
        "unit_annotations": [],
        "medical_segment_ids": [],
        "notes": [],
    }
    try:
        identity = medical_kb.snapshot_identity()
        profile["identity"] = identity
        profile["enabled"] = bool(identity.get("enabled"))
        if not profile["enabled"]:
            profile["notes"].append("medical_gate 禁用：指南快照缺失（materialize_medical_kb.py 未跑）")
            return profile
        state = medical_kb.load_snapshot()
        if state is None:
            profile["enabled"] = False
            profile["notes"].append("medical_gate 禁用：快照读取失败")
            return profile
        segment_text_of = {str(s.get("segment_id") or ""): str(s.get("text") or "")
                           for s in corrected_segments}
        annotations: list[dict[str, Any]] = []
        medical_segment_ids: list[str] = []
        for unit in (analysis.get("topic_units") or [])[:20]:
            if not isinstance(unit, dict):
                continue
            result = medical_kb.match_text(_unit_text(unit, segment_text_of), state)
            annotation = {
                "title": str(unit.get("title") or "")[:60],
                "segment_ids": [str(s) for s in (unit.get("segment_ids") or [])][:200],
                "level": result["level"],
                "score": result["score"],
                "matched": medical_kb.matched_entries_for(state, result),
            }
            annotations.append(annotation)
            if annotation["level"] in ("strong", "weak"):
                medical_segment_ids.extend(annotation["segment_ids"])
        # 词法补标：命中任一条目关键词的分段也算医学分段（主题块边界之外的散段）。
        in_unit = {sid for ann in annotations for sid in ann["segment_ids"]}
        for segment in corrected_segments[:4000]:
            sid = str(segment.get("segment_id") or "")
            if not sid or sid in in_unit:
                continue
            ratio, _ = medical_kb.lexical_ratio(str(segment.get("text") or ""), state["entries"])
            if ratio > 0:
                medical_segment_ids.append(sid)
        profile["unit_annotations"] = annotations
        profile["medical_segment_ids"] = sorted(set(medical_segment_ids))
        return profile
    except Exception as exc:  # noqa: BLE001 — 标注失败绝不阻断内容分析
        profile.update({"enabled": False,
                        "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        profile["notes"].append("medical_gate 医学标注异常禁用（不阻断内容分析）")
        return profile


def attach_analysis_annotations(analysis: dict[str, Any],
                                medical_analysis: dict[str, Any]) -> dict[str, Any]:
    """把医学匹配事实写入正式内容分析产物，不写任何淘汰决定。"""
    analysis["medical_annotations"] = list(medical_analysis.get("unit_annotations") or [])
    analysis["medical_analysis"] = medical_analysis
    return analysis


def planning_profile_from_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    """由已持久化的分析标注构造本次规划策略画像。

    规划阶段只消费分析事实，不重新加载知识库或重新匹配主题。领域激活属于本次
    规划决策：存在 strong 主题才 enforce，否则 annotate_only；分析缺失或禁用时
    明确降级为 disabled。
    """
    stored = analysis.get("medical_analysis")
    if not isinstance(stored, dict):
        return {
            "profile_version": PROFILE_VERSION,
            "gate_version": medical_kb.GATE_VERSION,
            "identity": {"enabled": False, "gate_version": medical_kb.GATE_VERSION},
            "enabled": False,
            "mode": "disabled",
            "unit_annotations": [],
            "medical_segment_ids": [],
            "notes": ["内容分析产物缺少 medical_analysis，规划医学过滤禁用（不现场重算）"],
        }
    profile = {
        "profile_version": stored.get("profile_version") or PROFILE_VERSION,
        "gate_version": stored.get("gate_version") or medical_kb.GATE_VERSION,
        "identity": dict(stored.get("identity") or {}),
        "enabled": bool(stored.get("enabled")),
        "unit_annotations": list(stored.get("unit_annotations") or []),
        "medical_segment_ids": list(stored.get("medical_segment_ids") or []),
        "notes": list(stored.get("notes") or []),
    }
    if stored.get("error"):
        profile["error"] = stored["error"]
    if not profile["enabled"]:
        profile["mode"] = "disabled"
        return profile
    # 领域激活是规划策略，不回写分析事实：只有 strong 主题才执行医学淘汰。
    profile["mode"] = "enforce" if any(
        ann.get("level") == "strong" for ann in profile["unit_annotations"]
        if isinstance(ann, dict)) else "annotate_only"
    if profile["mode"] == "annotate_only":
        profile["notes"].append(
            "全片无 strong 级主题块命中（库未覆盖或证据不足），本次规划仅标注不剔除（防误伤）")
    return profile


def run_gate(corrected_segments: list[dict[str, Any]],
             analysis: dict[str, Any]) -> dict[str, Any]:
    """兼容入口：分析匹配后构造规划画像。

    新主链必须在内容分析阶段调用 :func:`analyze_topics` 并持久化结果；规划阶段应
    调用 :func:`planning_profile_from_analysis`，不得通过本入口重新匹配。
    """
    enriched = dict(analysis)
    attach_analysis_annotations(enriched, analyze_topics(corrected_segments, analysis))
    return planning_profile_from_analysis(enriched)


_LEVEL_LABEL = {"strong": "医学", "weak": "弱相关", "none": "非医学"}


def context_lines(profile: dict[str, Any]) -> list[str]:
    """主题块医学标注 → 提示词A拼装层背景行（提示词A原文不动，只加程序标注）。"""
    if not profile.get("enabled"):
        return []
    lines = ["【医学主题匹配（程序标注，仅供规划参考，不得引用为医学结论）】"]
    for ann in profile.get("unit_annotations", [])[:12]:
        label = _LEVEL_LABEL.get(ann.get("level"), "未知")
        matched = "、".join(str(m.get("topic") or "") for m in (ann.get("matched") or [])[:2])
        suffix = f"，对应指南主题：{matched}" if matched else ""
        lines.append(f"  「{ann.get('title', '')}」: {label}（匹配度 {ann.get('score', 0):.2f}{suffix}）")
    return lines


def _clip_positions(index: Any, clip: dict[str, Any]) -> list[int]:
    """切片 → 覆盖的分段位置（start..end 连续区间，按索引定位；异常返回空）。"""
    entries = index.entries
    position_of = {entry["segment_id"]: pos for pos, entry in enumerate(entries)}
    start_pos = position_of.get(clip.get("start_segment_id"))
    end_pos = position_of.get(clip.get("end_segment_id"))
    if start_pos is None or end_pos is None or start_pos > end_pos:
        return []
    return list(range(start_pos, end_pos + 1))


def enforce_clips(profile: dict[str, Any], clips: list[dict[str, Any]], index: Any,
                  notes: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                                             list[dict[str, Any]]]:
    """规划后执法（硬剔除）。返回 (保留切片, 移入discarded的记录, 逐切片决策)。

    判定：切片覆盖主题块的最强级别为 none 且医学分段密度 < DISCARD_BELOW → 剔除。
    annotate_only / disabled 模式只记录 ``would_discard`` 决策，不动切片。
    """
    decisions: list[dict[str, Any]] = []
    if not profile.get("enabled"):
        return clips, [], decisions
    medical_ids = set(profile.get("medical_segment_ids") or [])
    # 每个分段位置 → 所在主题块的最强级别（none 之外的级别即"医学背书"）。
    position_of = {entry["segment_id"]: pos for pos, entry in enumerate(index.entries)}
    unit_level_at: dict[int, str] = {}
    for ann in profile.get("unit_annotations", []):
        for sid in ann.get("segment_ids") or []:
            pos = position_of.get(sid)
            if pos is None:
                continue
            current = unit_level_at.get(pos, "none")
            if ann["level"] == "strong" or (ann["level"] == "weak" and current == "none"):
                unit_level_at[pos] = ann["level"]
    kept: list[dict[str, Any]] = []
    moved: list[dict[str, Any]] = []
    enforcing = profile.get("mode") == "enforce"
    for clip in clips:
        positions = _clip_positions(index, clip)
        total = len(positions)
        medical_count = sum(1 for pos in positions if index.entries[pos]["segment_id"] in medical_ids)
        density = (medical_count / total) if total else 0.0
        best_level = "none"
        for pos in positions:
            level = unit_level_at.get(pos, "none")
            if level == "strong" or (level == "weak" and best_level != "strong"):
                best_level = level
        matched_topics = []
        for ann in profile.get("unit_annotations", []):
            if any(position_of.get(sid) in positions for sid in (ann.get("segment_ids") or [])):
                matched_topics.extend(m.get("topic") or "" for m in (ann.get("matched") or [])[:1])
        should_discard = bool(enforcing and best_level == "none" and density < DISCARD_BELOW)
        decision = {
            "clip_id": clip.get("clip_id") or "",
            "topic": clip.get("topic") or "",
            "density": round(density, 4),
            "best_unit_level": best_level,
            "decision": "discarded_non_medical" if should_discard else "kept",
            "would_discard": should_discard or (best_level == "none" and density < DISCARD_BELOW),
            "matched_topics": sorted({t for t in matched_topics if t})[:4],
        }
        decisions.append(decision)
        clip["medical_gate"] = {"density": decision["density"], "best_unit_level": best_level,
                                "decision": decision["decision"],
                                "matched_topics": decision["matched_topics"],
                                "mode": profile.get("mode")}
        if should_discard:
            notes.append(f"医学闸门剔除非医学切片：「{clip.get('topic')}」"
                         f"（医学密度 {density:.0%}，主题块级别 none，阈值 {DISCARD_BELOW:.0%}）")
            moved.append({
                "segment_ids": [index.entries[pos]["segment_id"] for pos in positions],
                "type": DISCARD_TYPE,
                "reason": f"医学闸门硬剔除：切片「{clip.get('topic')}」无指南条目命中"
                          f"且医学密度 {density:.0%} 低于阈值 {DISCARD_BELOW:.0%}",
            })
        else:
            kept.append(clip)
    return kept, moved, decisions


def record_summary(profile: dict[str, Any], decisions: list[dict[str, Any]],
                   moved_count: int) -> dict[str, Any]:
    """进 clip_plan.json 的闸门摘要（含身份，供溯源与缓存判读）。"""
    identity = profile.get("identity") or {}
    return {
        "profile_version": profile.get("profile_version"),
        "gate_version": profile.get("gate_version"),
        "mode": profile.get("mode"),
        "snapshot_sha256": identity.get("snapshot_sha256") or "",
        "entry_count": identity.get("entry_count") or 0,
        "thresholds": {
            "strong": identity.get("strong_threshold"),
            "weak": identity.get("weak_threshold"),
            "lexical_weight": identity.get("lexical_weight"),
            "discard_below": DISCARD_BELOW,
        },
        "unit_annotation_count": len(profile.get("unit_annotations") or []),
        "medical_segment_count": len(profile.get("medical_segment_ids") or []),
        "moved_count": moved_count,
        "decisions": decisions,
        "notes": profile.get("notes") or [],
        "error": profile.get("error"),
    }
