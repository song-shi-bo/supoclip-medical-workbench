"""问答化净化重组（2026-11-11 P1，用户拍板：问答化为主/独白化为辅/拼接要合理）。

定位：夹在切片规划与精修之间的**规划后净化阶段**——把问诊对话切片里
"口播感"的敌人（应答碎片、患者插话、寒暄确认）从渲染时间轴上删除，
产出有序子区间 ``sub_ranges``：

- **问答化（主）**：开场保留患者实质提问当钩子；正文保留医生成段解释；
- **净化规则（P1 保守构造）**：只删除"纯应答段"（对/嗯/啊/这这这 类短应答
  与重复确认）——这类段不携带实质语义，删除后前后拼接**由构造保证合理**
  （同主题/同会诊对象/结论方向天然不变，拼接红线逐条满足）；
- **独白化（辅，P3）**：全删患者话语的可选风格，P1 不启用；
- **绝不阻断**：任何失败/条件不满足 → 整片回退原连续区间，如实留痕。

硬边界：
- 不改写、不调语序、不删医生话语（P1）——与提示词B"只删不改"哲学一致：
  多点拼接在语义上就是"删除中间碎片段"；
- 提示词A 输出的切片区间（start/end）**不变**——sub_ranges 是渲染级精化，
  全覆盖契约（切片+弃用段覆盖全片）不受影响；
- 纯度/占比阈值只产 warning，无 reject。

身份契约：``identity(speaker_fingerprint)`` 进 plan provenance
（clip_planning.build_current_plan_provenance.rules.clip_purify）——说话人
标注变化或阈值调整都会使旧计划缓存失效；渲染指纹亦含 purify 版本。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

PURIFY_VERSION = "clip-purify/v1"

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
# 说话人纯度：医生话语占比低于该值仅记 warning（P1 不淘汰，无 reject 契约）。
MIN_DOCTOR_RATIO = max(0.0, min(1.0, float(os.getenv("MEDICAL_PURIFY_MIN_DOCTOR_RATIO", "0.6"))))


def identity(speaker_fingerprint: str | None) -> dict[str, Any]:
    """净化身份（进 plan 指纹）：版本 + 说话人标注指纹 + 阈值。"""
    return {
        "enabled": True,
        "version": PURIFY_VERSION,
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

    任何失败 → (None, None)（净化降级为纯应答词 heuristic，绝不阻断）。
    """
    if not source_id:
        return None, None
    try:
        from . import speaker_labeling  # 局部导入避免环
        record = speaker_labeling.load_labels(transcripts_root, str(source_id))
        labels = record.get("labels") if isinstance(record, dict) else None
        if not isinstance(labels, dict) or not labels:
            return None, None
        clean = {str(k): str(v) for k, v in labels.items() if k and v}
        return clean or None, speaker_overlay_fingerprint(clean)
    except Exception:  # noqa: BLE001
        return None, None


_PUNCT_RE = re.compile(r"[，。！？；：、,.!?;:\s\[\]（）()\"\"''「」…—~～-]+")


def is_pure_response(text: str) -> bool:
    """纯应答段判定：去标点/空白后为空，或仅由应答词的 1~6 次重复构成。"""
    cleaned = _PUNCT_RE.sub("", str(text or ""))
    if not cleaned:
        return True
    if len(cleaned) > 12:
        return False
    if cleaned in RESPONSE_WORDS:
        return True
    return bool(_PURE_RESPONSE_RE.fullmatch(cleaned))


def _clip_positions(entries: list[dict[str, Any]], position_of: dict[str, int],
                    start_segment_id: str, end_segment_id: str) -> list[int] | None:
    start_pos = position_of.get(start_segment_id)
    end_pos = position_of.get(end_segment_id)
    if start_pos is None or end_pos is None or start_pos > end_pos:
        return None
    return list(range(start_pos, end_pos + 1))


def purify_clip(clip: dict[str, Any], entries: list[dict[str, Any]],
                position_of: dict[str, int],
                speaker_labels: dict[str, str] | None) -> dict[str, Any]:
    """单切片净化 → {sub_ranges, dropped, doctor_ratio, mode, fallback_reason}。

    保守规则（P1）：只删纯应答段；首段吸附（前导纯应答自然被删）；
    尾段永不删（保留结论收尾）；删除占比超限或切片无内容 → 整片回退。
    """
    result: dict[str, Any] = {"mode": "qa_purify", "sub_ranges": None,
                              "dropped_segment_ids": [], "doctor_ratio": None,
                              "fallback_reason": None}
    positions = _clip_positions(entries, position_of,
                                str(clip.get("start_segment_id") or ""),
                                str(clip.get("end_segment_id") or ""))
    if not positions:
        result["fallback_reason"] = "segment_range_missing"
        return result
    total_ms = entries[positions[-1]]["end_ms"] - entries[positions[0]]["start_ms"]
    kept: list[int] = []
    dropped: list[int] = []
    for pos in positions:
        # 尾段（结论收尾）永不删；其余纯应答段删。
        if pos != positions[-1] and is_pure_response(entries[pos].get("text") or ""):
            dropped.append(pos)
        else:
            kept.append(pos)
    if not kept:
        result["fallback_reason"] = "all_dropped"
        return result
    dropped_ms = sum(entries[p]["end_ms"] - entries[p]["start_ms"] for p in dropped)
    if total_ms <= 0 or dropped_ms / total_ms > MIN_KEEP_RATIO:
        result["fallback_reason"] = (f"keep_ratio_below_floor"
                                     f"（净删占比 {dropped_ms / max(1, total_ms):.0%} > 上限"
                                     f" {1 - MIN_KEEP_RATIO:.0%}）")
        return result
    # 说话人纯度（有标注才算，只记 warning 不淘汰）
    if speaker_labels:
        labeled = [p for p in positions if str(entries[p].get("segment_id")) in speaker_labels]
        if labeled:
            doctor = sum(1 for p in labeled
                         if speaker_labels[str(entries[p]["segment_id"])] == "医生")
            result["doctor_ratio"] = round(doctor / len(labeled), 4)
    # 连续 kept 段 → 子区间（单区间且无删除 → 不产 sub_ranges，向后兼容）
    runs: list[list[int]] = [[kept[0]]]
    for pos in kept[1:]:
        if pos == runs[-1][-1] + 1:
            runs[-1].append(pos)
        else:
            runs.append([pos])
    if not dropped:
        result["mode"] = "noop"
        return result
    result["dropped_segment_ids"] = [str(entries[p]["segment_id"]) for p in dropped]
    result["sub_ranges"] = [{
        "start_segment_id": str(entries[run[0]]["segment_id"]),
        "end_segment_id": str(entries[run[-1]]["segment_id"]),
        "start_ms": int(entries[run[0]]["start_ms"]),
        "end_ms": int(entries[run[-1]]["end_ms"]),
    } for run in runs]
    return result


def purify_clips(clips: list[dict[str, Any]], corrected_segments: list[dict[str, Any]],
                 speaker_labels: dict[str, str] | None) -> dict[str, Any]:
    """整计划净化：逐切片 purify_clip，回退如实留痕，绝不抛异常。"""
    entries = [{"segment_id": str(s.get("segment_id") or ""),
                "start_ms": int(s.get("start_ms") or 0),
                "end_ms": int(s.get("end_ms") or 0),
                "text": str(s.get("text") or s.get("corrected_text") or "")}
               for s in corrected_segments]
    position_of = {e["segment_id"]: pos for pos, e in enumerate(entries)}
    decisions: list[dict[str, Any]] = []
    purified = 0
    try:
        for clip in clips:
            try:
                result = purify_clip(clip, entries, position_of, speaker_labels)
            except Exception as exc:  # noqa: BLE001 — 单片失败整片回退
                result = {"mode": "qa_purify", "sub_ranges": None,
                          "dropped_segment_ids": [], "doctor_ratio": None,
                          "fallback_reason": f"{type(exc).__name__}: {str(exc)[:120]}"}
            sub_ranges = result.get("sub_ranges")
            if sub_ranges:
                clip["sub_ranges"] = sub_ranges
                purified += 1
            clip["purify"] = {
                "mode": result.get("mode"),
                "sub_range_count": len(sub_ranges) if sub_ranges else 0,
                "dropped_segment_ids": result.get("dropped_segment_ids") or [],
                "doctor_ratio": result.get("doctor_ratio"),
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
        return {"version": PURIFY_VERSION, "mode": "disabled", "purified": 0,
                "speaker_source": "overlay" if speaker_labels else "none",
                "thresholds": {"min_keep_ratio": MIN_KEEP_RATIO,
                               "min_doctor_ratio": MIN_DOCTOR_RATIO},
                "decisions": [], "notes": [f"purify 异常禁用：{type(exc).__name__}"],
                "error": str(exc)[:200]}
    notes: list[str] = []
    doctor_warns = [d["warning"] for d in decisions if d.get("warning")]
    notes.extend(doctor_warns)
    return {
        "version": PURIFY_VERSION,
        "mode": "qa_purify",
        "purified": purified,
        "speaker_source": "overlay" if speaker_labels else "none",
        "thresholds": {"min_keep_ratio": MIN_KEEP_RATIO,
                       "min_doctor_ratio": MIN_DOCTOR_RATIO},
        "decisions": decisions,
        "notes": notes,
    }
