"""Auditable, conservative correction for timestamped Chinese ASR segments."""

from __future__ import annotations

import re
from typing import Any


_PUNCTUATION = str.maketrans({",": "，", ".": "。", "?": "？", "!": "！", ";": "；", ":": "："})
_HIGH_RISK = (
    (r"\b\d+(?:\.\d+)?\s*(?:mg|ml|毫克|毫升|天|周|个月|次|片|针)\b", "剂量、疗程或次数需要核听"),
    (r"(?:诊断|肿瘤|癌|骨折|炎症|感染|血栓|手术|化疗|放疗|处方|药|针|片|检查|CT|MRI|核磁|报告)", "医学术语或治疗描述需要核听"),
)


def _normalize_low_risk(text: str) -> tuple[str, list[dict[str, str]]]:
    """Apply only mechanical, low-risk corrections without changing meaning."""
    normalized = re.sub(r"\s+", "", text).translate(_PUNCTUATION).strip()
    changes: list[dict[str, str]] = []
    if normalized != text:
        changes.append(
            {
                "type": "formatting",
                "from": text,
                "to": normalized,
                "basis": "移除 ASR 多余空格并统一中文标点",
            }
        )
    return normalized, changes


def correct_segments(raw_segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Audit every segment while preserving all source timestamps and raw text."""
    corrected_segments: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    pending_review = 0
    changed = 0

    for index, segment in enumerate(raw_segments):
        raw_text = str(segment.get("text", ""))
        corrected_text, changes = _normalize_low_risk(raw_text)
        issues = [
            {"type": "medical_or_numeric_uncertainty", "reason": reason, "text": match.group(0)}
            for pattern, reason in _HIGH_RISK
            for match in re.finditer(pattern, corrected_text, flags=re.IGNORECASE)
        ]
        status = "pending_review" if issues else ("corrected" if changes else "no_change")
        if issues:
            pending_review += 1
        if changes:
            changed += 1

        audit_entry = {
            "segment_id": f"seg_{index:04d}",
            "start": float(segment.get("start", 0)),
            "end": float(segment.get("end", 0)),
            "raw_text": raw_text,
            "corrected_text": corrected_text,
            "status": status,
            "changes": changes,
            "issues": issues,
            "reviewed_by_human": False,
        }
        audit.append(audit_entry)
        corrected_segments.append(
            {
                "start": audit_entry["start"],
                "end": audit_entry["end"],
                "text": corrected_text,
                "speaker": "未标注",
                "correction_status": status,
                "issues": issues,
            }
        )

    total = len(raw_segments)
    return {
        "asr_segments": corrected_segments,
        "correction_audit": audit,
        "correction_summary": {
            "segments_total": total,
            "segments_audited": total,
            "coverage": 1.0 if total else 0.0,
            "low_risk_changes": changed,
            "issues_pending_review": pending_review,
            "engine": "local-conservative-rules",
        },
    }
