"""问答化净化重组（2026-11-11 P1/P2，用户拍板：问答化为主/独白化为辅/拼接要合理）。

定位：夹在切片规划与精修之间的**规划后净化阶段**——把问诊对话切片里
"口播感"的敌人（应答碎片、患者插话、寒暄确认）从渲染时间轴上删除，
产出有序子区间 ``sub_ranges``：

- **问答化（主，默认）**：开场保留患者实质提问当钩子；正文保留医生成段解释；
  删除其余患者插话与全部应答碎片；
- **独白化（辅，`MEDICAL_PURIFY_STYLE=mono`）**：患者话语全部删除（含提问）；
- **多点拼接（要合理）**：删除产生的段间拼接必须过拼接红线——P1/P2 的保守
  构造（只删患者话语与纯应答，医生话语原文原序且同一主题块内）天然满足
  同主题/同会诊对象/结论方向；医学指南条目佐证随 decision 透出（P3 LLM 裁判前）；
- **绝不阻断**：任何失败/条件不满足 → 整片回退原连续区间，如实留痕。

删除授权阶梯（防误删医生话语）：
1. 纯应答段（去标点后仅应答词重复）——无需任何标注，P1 起可删；
2. 患者话语实质删除（qa 删非提问插话 / mono 删全部）——**必须有说话人标注**
   （overlay labels 命中 患者/家属）；无标注时退回规则 1；
3. 医生话语（doctor_core_segment_ids 命中）——永不删除。

硬边界：不改写、不调语序；提示词A 输出的切片区间不变（sub_ranges 是渲染级
精化，全覆盖契约不受影响）；纯度/占比阈值只产 warning，无 reject。

身份契约：``identity(speaker_fingerprint)`` 进 plan provenance——说话人标注、
风格或阈值变化都会使旧计划缓存失效；渲染指纹亦含 purify 版本。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

PURIFY_VERSION = "clip-purify/v2"

# 风格：qa=问答化（保留患者提问当钩子）；mono=独白化（患者话语全删）。
STYLE = os.getenv("MEDICAL_PURIFY_STYLE", "qa").strip().lower()
if STYLE not in {"qa", "mono"}:
    STYLE = "qa"

PATIENT_SPEAKERS = {"患者", "家属"}

# 纯应答词表：整段（去标点/空白后）仅由这些词的重复构成才可删。
RESPONSE_WORDS = (
    "对", "嗯", "啊", "好", "是", "行", "哦", "噢", "喔", "哎", "唉", "诶", "呀",
    "对的", "是的", "好的", "行吧", "嗯呢",
)
# 尾缀粒子词（的呢吧啦了哈等）不改变应答性质："好好好的""是的是的"。
_PURE_RESPONSE_RE = re.compile(
    r"^(对|嗯|啊|好|是|行|哦|噢|喔|哎|唉|诶|呀){1,6}[的呢吧啦了哈]{0,3}$")

# 删除占比保护：净删时长超过切片时长该比例 → 整片回退（防止过度净化掏空切片）。
MIN_KEEP_RATIO = max(0.0, min(1.0, float(os.getenv("MEDICAL_PURIFY_MIN_KEEP_RATIO", "0.5"))))
# 说话人纯度：医生话语占比低于该值仅记 warning（无 reject 契约）。
MIN_DOCTOR_RATIO = max(0.0, min(1.0, float(os.getenv("MEDICAL_PURIFY_MIN_DOCTOR_RATIO", "0.6"))))

_PUNCT_RE = re.compile(r"[，。！？；：、,.!?;:\s\[\]（）()\"\"''「」…—~～-]+")


def identity(speaker_fingerprint: str | None) -> dict[str, Any]:
    """净化身份（进 plan 指纹）：版本 + 风格 + 说话人标注指纹 + 阈值。"""
    return {
        "enabled": True,
        "version": PURIFY_VERSION,
        "style": STYLE,
        "speaker_source": "overlay" if speaker_fingerprint else "none",
        "speaker_fingerprint": speaker_fingerprint or "",
        "min_keep_ratio": MIN_KEEP_RATIO,
        "min_doctor_ratio": MIN_DOCTOR_RATIO,
    }


def speaker_overlay_fingerprint(labels: dict[str, str] | None) -> str | None:
    if not labels:
        return None
    canonical = json.dumps(dict(sorted(labels.items())), ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def load_speaker_overlay(transcripts_root: Path, source_id: str | None
                         ) -> tuple[dict[str, str] | None, str | None]:
    """读源级说话人标注 overlay（speaker_labeling.load_labels 的薄包装）。

    source_id 兼容两种形态：config 里的 `tr-<id>` 与目录名的 `<id>`（2026-11-11
    修复：真实事故中 transcript_id 带 tr- 前缀导致 overlay 永远找不到）。
    任何失败 → (None, None)（净化降级为纯应答词 heuristic，绝不阻断）。
    """
    if not source_id:
        return None, None
    try:
        from . import speaker_labeling  # 局部导入避免环
        candidates = [str(source_id)]
        stripped = str(source_id).strip()
        if stripped.startswith("tr-"):
            candidates.append(stripped[3:])
        for candidate in candidates:
            record = speaker_labeling.load_labels(transcripts_root, candidate)
            labels = record.get("labels") if isinstance(record, dict) else None
            if isinstance(labels, dict) and labels:
                clean = {str(k): str(v) for k, v in labels.items() if k and v}
                if clean:
                    return clean, speaker_overlay_fingerprint(clean)
        return None, None
    except Exception:  # noqa: BLE001
        return None, None


def is_pure_response(text: str) -> bool:
    """纯应答段判定：去标点/空白后为空，或仅由应答词的重复（可带尾缀粒子）构成。"""
    cleaned = _PUNCT_RE.sub("", str(text or ""))
    if not cleaned:
        return True
    if len(cleaned) > 12:
        return False
    if cleaned in RESPONSE_WORDS:
        return True
    return bool(_PURE_RESPONSE_RE.fullmatch(cleaned))


def _norm_text(text: str) -> str:
    return _PUNCT_RE.sub("", str(text or ""))


def _clip_positions(entries: list[dict[str, Any]], position_of: dict[str, int],
                    start_segment_id: str, end_segment_id: str) -> list[int] | None:
    start_pos = position_of.get(start_segment_id)
    end_pos = position_of.get(end_segment_id)
    if start_pos is None or end_pos is None or start_pos > end_pos:
        return None
    return list(range(start_pos, end_pos + 1))


def _unit_for_clip(clip: dict[str, Any], analysis: dict[str, Any] | None,
                   position_of: dict[str, int]) -> dict[str, Any] | None:
    """切片 → 重叠最大的主题块（拿 patient_question 与 doctor_core_segment_ids）。"""
    if not analysis:
        return None
    clip_span: set[int] = set()
    for sid in (str(clip.get("start_segment_id") or ""),
                str(clip.get("end_segment_id") or "")):
        pos = position_of.get(sid)
        if pos is not None:
            clip_span.add(pos)
    if not clip_span:
        return None
    best: tuple[int, dict[str, Any]] | None = None
    for unit in (analysis.get("topic_units") or []):
        if not isinstance(unit, dict):
            continue
        ids = [position_of[sid] for sid in (unit.get("segment_ids") or [])
               if sid in position_of]
        if not ids:
            continue
        span = set(range(min(ids), max(ids) + 1))
        overlap = len(span & clip_span)
        if best is None or overlap > best[0]:
            best = (overlap, unit)
    return best[1] if best and best[0] > 0 else None


def purify_clip(clip: dict[str, Any], entries: list[dict[str, Any]],
                position_of: dict[str, int], speaker_labels: dict[str, str] | None,
                *, unit: dict[str, Any] | None = None,
                evidence_topics: list[str] | None = None,
                style: str | None = None) -> dict[str, Any]:
    """单切片净化 → {sub_ranges, dropped, doctor_ratio, mode, fallback_reason, …}。

    style：qa（默认，保留患者提问钩子）/ mono（患者话语全删）；None 取模块 STYLE。
    删除授权阶梯（见模块头）：纯应答段 < 患者话语（需标注，qa 保护提问载体，
    mono 全删）< 医生核心段（永不删）。任何失败 → fallback，绝不抛出。
    """
    style = (style or STYLE).strip().lower()
    result: dict[str, Any] = {
        "mode": f"{style}_purify", "sub_ranges": None, "dropped_segment_ids": [],
        "doctor_ratio": None, "question_kept": None, "fallback_reason": None,
        "evidence_topics": list(evidence_topics or []),
    }
    positions = _clip_positions(entries, position_of,
                                str(clip.get("start_segment_id") or ""),
                                str(clip.get("end_segment_id") or ""))
    if not positions:
        result["fallback_reason"] = "segment_range_missing"
        return result
    total_ms = entries[positions[-1]]["end_ms"] - entries[positions[0]]["start_ms"]
    # 提问载体：患者提问原话命中的分段（qa 模式保留当钩子）
    question = _norm_text((unit or {}).get("patient_question") or "")
    question_carriers: set[int] = set()
    if question:
        for pos in positions:
            if question and question in _norm_text(entries[pos].get("text") or ""):
                question_carriers.add(pos)
    doctor_core = {str(sid) for sid in ((unit or {}).get("doctor_core_segment_ids") or [])}

    def droppable(pos: int) -> bool:
        text = entries[pos].get("text") or ""
        sid = str(entries[pos].get("segment_id"))
        if pos in doctor_core:
            return False
        if pos in question_carriers and style == "qa":
            return False
        if is_pure_response(text):
            if pos == positions[-1] and not speaker_labels:
                return False  # P1 保守规则：无标注时尾段纯应答保留
            return True
        speaker = (speaker_labels or {}).get(sid)
        if speaker in PATIENT_SPEAKERS:
            return True  # qa 删非提问插话；mono 全删患者话语
        return False

    kept: list[int] = []
    dropped: list[int] = []
    for pos in positions:
        if pos != positions[-1] and droppable(pos):
            dropped.append(pos)
        elif pos == positions[-1] and speaker_labels and droppable(pos) \
                and (speaker_labels or {}).get(str(entries[pos]["segment_id"])) in PATIENT_SPEAKERS:
            dropped.append(pos)  # 尾段患者话语（含应答）在有标注时允许删除
        else:
            kept.append(pos)
    if not kept:
        result["fallback_reason"] = "all_dropped"
        return result
    dropped_ms = sum(entries[p]["end_ms"] - entries[p]["start_ms"] for p in dropped)
    if total_ms <= 0 or dropped_ms / total_ms > MIN_KEEP_RATIO:
        result["fallback_reason"] = (
            f"keep_ratio_below_floor"
            f"（净删占比 {dropped_ms / max(1, total_ms):.0%} > 上限 {1 - MIN_KEEP_RATIO:.0%}）")
        return result
    if speaker_labels:
        labeled = [p for p in positions if str(entries[p].get("segment_id")) in speaker_labels]
        if labeled:
            doctor = sum(1 for p in labeled
                         if speaker_labels[str(entries[p]["segment_id"])] == "医生")
            result["doctor_ratio"] = round(doctor / len(labeled), 4)
    result["question_kept"] = bool(question_carriers & set(kept))
    kept_ids = [str(entries[p]["segment_id"]) for p in kept]
    result["dropped_segment_ids"] = [str(entries[p]["segment_id"]) for p in dropped]
    runs: list[list[int]] = [[kept[0]]]
    for pos in kept[1:]:
        if pos == runs[-1][-1] + 1:
            runs[-1].append(pos)
        else:
            runs.append([pos])
    if not dropped:
        result["mode"] = "noop"
        return result
    result["sub_ranges"] = [{
        "start_segment_id": str(entries[run[0]]["segment_id"]),
        "end_segment_id": str(entries[run[-1]]["segment_id"]),
        "start_ms": int(entries[run[0]]["start_ms"]),
        "end_ms": int(entries[run[-1]]["end_ms"]),
    } for run in runs]
    return result


def purify_clips(clips: list[dict[str, Any]], corrected_segments: list[dict[str, Any]],
                 speaker_labels: dict[str, str] | None, *,
                 analysis: dict[str, Any] | None = None,
                 gate_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """整计划净化：逐切片 purify_clip，回退如实留痕，绝不抛异常。

    ``analysis`` 提供主题块 patient_question / doctor_core_segment_ids（P2）；
    ``gate_profile`` 提供医学条目佐证（拼接合理性的证据透出，P3-lite）。
    """
    entries = [{"segment_id": str(s.get("segment_id") or ""),
                "start_ms": int(s.get("start_ms") or 0),
                "end_ms": int(s.get("end_ms") or 0),
                "text": str(s.get("text") or s.get("corrected_text") or "")}
               for s in corrected_segments]
    position_of = {e["segment_id"]: pos for pos, e in enumerate(entries)}
    annotations = (gate_profile or {}).get("unit_annotations") or []
    decisions: list[dict[str, Any]] = []
    purified = 0
    try:
        for clip in clips:
            try:
                unit = _unit_for_clip(clip, analysis, position_of)
                evidence: list[str] = []
                if unit is not None:
                    want = position_of.get(str(clip.get("start_segment_id")))
                    for ann in annotations:
                        if str(ann.get("title") or "") == str(unit.get("title") or ""):
                            evidence = [str(m.get("topic") or "")
                                        for m in (ann.get("matched") or [])]
                            break
                else:
                    want = None
                del want
                result = purify_clip(clip, entries, position_of, speaker_labels,
                                     unit=unit, evidence_topics=evidence,
                                     style=STYLE)
            except Exception as exc:  # noqa: BLE001 — 单片失败整片回退
                result = {"mode": f"{STYLE}_purify", "sub_ranges": None,
                          "dropped_segment_ids": [], "doctor_ratio": None,
                          "question_kept": None, "fallback_reason":
                          f"{type(exc).__name__}: {str(exc)[:120]}",
                          "evidence_topics": []}
            sub_ranges = result.get("sub_ranges")
            if sub_ranges:
                clip["sub_ranges"] = sub_ranges
                purified += 1
            clip["purify"] = {
                "mode": result.get("mode"),
                "style": STYLE,
                "sub_range_count": len(sub_ranges) if sub_ranges else 0,
                "dropped_segment_ids": result.get("dropped_segment_ids") or [],
                "doctor_ratio": result.get("doctor_ratio"),
                "question_kept": result.get("question_kept"),
                "evidence_topics": result.get("evidence_topics") or [],
                "fallback_reason": result.get("fallback_reason"),
            }
            decisions.append({
                "clip_id": str(clip.get("clip_id") or ""),
                "topic": str(clip.get("topic") or ""),
                **clip["purify"],
                "warning": (f"说话人纯度 {result['doctor_ratio']:.0%} 低于下限"
                            f" {MIN_DOCTOR_RATIO:.0%}")
                if (result.get("doctor_ratio") is not None
                    and result["doctor_ratio"] < MIN_DOCTOR_RATIO) else None,
            })
    except Exception as exc:  # noqa: BLE001 — 整体失败：原样返回，绝不阻断
        return {"version": PURIFY_VERSION, "mode": "disabled", "style": STYLE,
                "purified": 0,
                "speaker_source": "overlay" if speaker_labels else "none",
                "thresholds": {"min_keep_ratio": MIN_KEEP_RATIO,
                               "min_doctor_ratio": MIN_DOCTOR_RATIO},
                "decisions": [], "notes": [f"purify 异常禁用：{type(exc).__name__}"],
                "error": str(exc)[:200]}
    notes = [d["warning"] for d in decisions if d.get("warning")]
    return {
        "version": PURIFY_VERSION,
        "mode": f"{STYLE}_purify",
        "style": STYLE,
        "purified": purified,
        "speaker_source": "overlay" if speaker_labels else "none",
        "thresholds": {"min_keep_ratio": MIN_KEEP_RATIO,
                       "min_doctor_ratio": MIN_DOCTOR_RATIO},
        "decisions": decisions,
        "notes": notes,
    }
