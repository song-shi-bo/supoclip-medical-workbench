"""完整源区间的有界分层规划；模型由调用方显式 callback 提供。

本模块不调用模型、不生成规则切片。callback 可返回 clips 列表、带 clips 的
Mapping 或具有 model_dump 的对象。仅显式 plan_source=llm 计为模型成功；
规则来源透传，缺少来源标记为 callback_unverified。
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

HELPER_VERSION = "hierarchical-planning/v1"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _validated_segments(segments: list[dict]) -> list[dict]:
    if not isinstance(segments, list):
        raise ValueError("segments must be a source-ordered list")
    result, seen, previous = [], set(), -1
    for item in segments:
        if not isinstance(item, dict):
            raise ValueError("segment must be a dict")
        sid, start, end = item.get("segment_id"), item.get("start_ms"), item.get("end_ms")
        if not isinstance(sid, str) or not sid or sid in seen:
            raise ValueError("segment_id must be unique and nonempty")
        if (type(start) is not int or type(end) is not int or start < previous
                or start < 0 or end <= start or not isinstance(item.get("text"), str)):
            raise ValueError("invalid segment timestamps/text or source order")
        seen.add(sid)
        previous = start
        result.append(copy.deepcopy(item))
    return result


def _window(entries: list[dict], analysis: dict, window_id: str,
            *, parent_id: str | None = None, depth: int = 0) -> dict:
    ids = [entry["segment_id"] for entry in entries]
    start, end = entries[0]["start_ms"], max(entry["end_ms"] for entry in entries)
    id_set = set(ids)
    topics = [copy.deepcopy(unit) for unit in analysis.get("topic_units", [])
              if isinstance(unit, dict) and (id_set.intersection(unit.get("segment_ids") or [])
                or _intersects(unit, start, end))]
    intervals = [copy.deepcopy(unit) for unit in analysis.get("candidate_intervals", [])
                 if isinstance(unit, dict) and _intersects(unit, start, end)]
    summary = {"keywords": copy.deepcopy(analysis.get("keywords") or []),
               "topic_units": topics, "candidate_intervals": intervals}
    fingerprint = _digest({"segments": entries, "analysis": analysis,
                           "helper_version": HELPER_VERSION})
    return {"window_id": window_id, "parent_id": parent_id, "depth": depth,
            "segment_ids": ids, "segments": copy.deepcopy(entries),
            "start_ms": start, "end_ms": end, "topic_summary": summary,
            "input_fingerprint": fingerprint}


def _intersects(unit: dict, start: int, end: int) -> bool:
    lo, hi = unit.get("start_ms"), unit.get("end_ms")
    return (type(lo) is int and type(hi) is int and hi > lo
            and lo < end and hi > start)


def build_planning_windows(segments: list[dict], analysis: dict, *,
                           max_segments: int = 400, overlap_segments: int = 40) -> list[dict]:
    """主题结尾指导边界；稀疏/超长主题仍全覆盖，重叠只提供边界上下文。"""
    if (type(max_segments) is not int or type(overlap_segments) is not int
            or max_segments < 1 or not 0 <= overlap_segments < max_segments):
        raise ValueError("require max_segments > overlap_segments >= 0")
    entries = _validated_segments(segments)
    if not entries:
        return []
    positions = {entry["segment_id"]: i for i, entry in enumerate(entries)}
    boundaries = set()
    for unit in analysis.get("topic_units", []):
        if not isinstance(unit, dict):
            continue
        known = [positions[sid] for sid in unit.get("segment_ids", []) if sid in positions]
        if known:
            boundaries.add(max(known) + 1)
    for unit in analysis.get("candidate_intervals", []):
        if isinstance(unit, dict) and type(unit.get("end_ms")) is int:
            boundary = next((i + 1 for i, entry in reversed(list(enumerate(entries)))
                             if entry["end_ms"] <= unit["end_ms"]), None)
            if boundary:
                boundaries.add(boundary)
    windows, lo = [], 0
    while lo < len(entries):
        hi = min(lo + max_segments, len(entries))
        if hi < len(entries):
            # 至少保持半个窗口，保证主题碎片不会造成无界小窗口。
            lower = lo + max(overlap_segments + 1, (max_segments + 1) // 2)
            choices = [boundary for boundary in boundaries if lower <= boundary <= hi]
            if choices:
                hi = max(choices)
        windows.append(_window(entries[lo:hi], analysis, f"window_{len(windows):04d}"))
        if hi == len(entries):
            break
        lo = hi - overlap_segments
    return windows


def _mapping(value: Any) -> Any:
    return value.model_dump() if callable(getattr(value, "model_dump", None)) else value


def _validate_clip(candidate: Any, window: dict, positions: dict[str, int]) -> dict:
    candidate = _mapping(candidate)
    if not isinstance(candidate, Mapping):
        raise ValueError("clip must be a mapping")
    ids = window["segment_ids"]
    start, end = candidate.get("start_segment_id"), candidate.get("end_segment_id")
    listed = candidate.get("segment_ids")
    if listed is not None:
        if not isinstance(listed, list) or not listed:
            raise ValueError("segment_ids must be nonempty")
        if any(sid not in ids for sid in listed):
            raise ValueError("unknown/out-of-window segment ID")
        indexes = [positions[sid] for sid in listed]
        if indexes != list(range(indexes[0], indexes[-1] + 1)):
            raise ValueError("noncontinuous segment_ids")
        if start is None and end is None:
            start, end = listed[0], listed[-1]
        elif listed[0] != start or listed[-1] != end:
            raise ValueError("segment_ids disagree with boundaries")
    if start not in ids or end not in ids or ids.index(end) < ids.index(start):
        raise ValueError("invalid/out-of-window clip boundaries")
    selected = window["segments"][ids.index(start):ids.index(end) + 1]
    actual_ids = [entry["segment_id"] for entry in selected]
    evidence = candidate.get("evidence_segment_ids") or []
    if not isinstance(evidence, list) or any(sid not in actual_ids for sid in evidence):
        raise ValueError("evidence ID outside clip")
    lo, hi = selected[0]["start_ms"], max(entry["end_ms"] for entry in selected)
    for field, expected in (("start_ms", lo), ("end_ms", hi), ("duration_ms", hi - lo)):
        if field in candidate and (type(candidate[field]) is not int or candidate[field] != expected):
            raise ValueError("model timestamp disagrees with program source")
    clip = copy.deepcopy(dict(candidate))
    clip.update({"start_segment_id": start, "end_segment_id": end,
                 "segment_ids": actual_ids, "start_ms": lo, "end_ms": hi,
                 "duration_ms": hi - lo, "lineage": [window["window_id"]]})
    return clip


def plan_hierarchically(segments: list[dict], analysis: dict,
                       planner: Callable[[dict], Any], *, max_segments: int = 400,
                       overlap_segments: int = 40, min_segments: int = 30,
                       max_depth: int = 3, max_calls: int = 256) -> dict:
    """局部失败拆小重试；调用/深度有界，终止失败保留源区间与 lineage。"""
    if (type(min_segments) is not int or min_segments < 1
            or type(max_depth) is not int or not 0 <= max_depth <= 8
            or type(max_calls) is not int or not 1 <= max_calls <= 4096):
        raise ValueError("invalid retry bounds")
    windows = build_planning_windows(segments, analysis, max_segments=max_segments,
                                     overlap_segments=overlap_segments)
    positions = {entry["segment_id"]: i for i, entry in enumerate(segments)}
    lineage, failed, rejected, candidates, discarded = [], [], [], [], []
    calls = 0

    def visit(window: dict) -> None:
        nonlocal calls
        node = {k: copy.deepcopy(v) for k, v in window.items() if k != "segments"}
        lineage.append(node)
        error = "callback budget exhausted"
        if calls < max_calls:
            calls += 1
            node["call_number"] = calls
            try:
                response = _mapping(planner(copy.deepcopy(window)))
                source = "callback_unverified"
                if isinstance(response, Mapping):
                    source = str(response.get("plan_source") or "callback_unverified")
                    rows = response.get("clips")
                    node["fallback_reasons"] = copy.deepcopy(response.get("fallback_reasons") or [])
                    discard_rows = response.get("discarded") or []
                    if not isinstance(discard_rows, list):
                        raise ValueError("discarded must be a list")
                    valid_discarded = []
                    for number, raw in enumerate(discard_rows):
                        raw = _mapping(raw)
                        cited = raw.get("segment_ids") if isinstance(raw, Mapping) else None
                        if (not isinstance(cited, list) or not cited
                                or any(sid not in window["segment_ids"] for sid in cited)):
                            rejected.append({"window_id": window["window_id"],
                                "discarded_index": number, "reason": "invalid discarded IDs"})
                            continue
                        item = copy.deepcopy(dict(raw))
                        item["segment_ids"] = sorted(set(cited), key=positions.__getitem__)
                        item["lineage"] = [window["window_id"]]
                        valid_discarded.append(item)
                else:
                    rows = response
                if not isinstance(rows, list):
                    raise ValueError("planner must return clips list")
                accepted = []
                for number, raw in enumerate(rows):
                    try:
                        clip = _validate_clip(raw, window, positions)
                        clip["plan_source"] = source
                        accepted.append(clip)
                    except (ValueError, TypeError, KeyError) as exc:
                        rejected.append({"window_id": window["window_id"],
                                         "candidate_index": number, "reason": str(exc)})
                if rows and not accepted:
                    raise ValueError("all clip candidates invalid")
                candidates.extend(accepted)
                if isinstance(response, Mapping):
                    discarded.extend(valid_discarded)
                node.update(status="succeeded", plan_source=source, clip_count=len(accepted))
                return
            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc)[:300]}"
        node["error"] = error
        entries = window["segments"]
        if (calls < max_calls and window["depth"] < max_depth
                and len(entries) >= 2 * min_segments):
            node["status"] = "split_retry"
            middle = len(entries) // 2
            context = min(overlap_segments, max(0, (len(entries) - 2) // 4))
            for number, group in enumerate((entries[:middle + context], entries[middle - context:])):
                visit(_window(group, analysis, f"{window['window_id']}.{number}",
                              parent_id=window["window_id"], depth=window["depth"] + 1))
        else:
            node["status"] = "failed"
            failed.append({"window_id": window["window_id"], "segment_ids": window["segment_ids"],
                           "start_ms": window["start_ms"], "end_ms": window["end_ms"],
                           "reason": error, "plan_source": "failed"})

    for window in windows:
        visit(window)
    # 高重叠候选保留较完整的连续区间；绝不拼接不连续语音。
    selected, duplicates = [], []
    for clip in sorted(candidates, key=lambda row: (-len(row["segment_ids"]),
                                                   positions[row["start_segment_id"]])):
        ids = set(clip["segment_ids"])
        duplicate = next((existing for existing in selected if
                          len(ids.intersection(existing["segment_ids"])) /
                          min(len(ids), len(existing["segment_ids"])) > 0.5), None)
        if duplicate is not None:
            duplicate["lineage"] = sorted(set(duplicate["lineage"] + clip["lineage"]))
            duplicates.append({"segment_ids": clip["segment_ids"], "lineage": clip["lineage"],
                               "kept_segment_ids": duplicate["segment_ids"]})
        else:
            selected.append(clip)
    selected.sort(key=lambda row: positions[row["start_segment_id"]])
    sources = {node["plan_source"] for node in lineage if node.get("status") == "succeeded"}
    source = next(iter(sources)) if len(sources) == 1 else "mixed" if sources else "failed"
    degradations = []
    if any(node["status"] == "split_retry" for node in lineage):
        degradations.append("local_split_retry")
    if failed:
        degradations.append("failed_windows")
    if sources - {"llm"}:
        degradations.append("non_model_or_unverified_callback")
    if rejected:
        degradations.append("invalid_candidates_rejected")
    return {"clips": selected, "candidates": selected, "windows": lineage,
            "failed_windows": failed, "rejected_candidates": rejected,
            "duplicates": duplicates, "discarded": discarded, "degradations": degradations,
            "plan_source": source, "llm_enabled": source == "llm" and not failed,
            "callback_calls": calls, "callback_budget": max_calls,
            "helper_version": HELPER_VERSION, "input_fingerprint": _digest({
                "segments": segments, "analysis": analysis, "helper_version": HELPER_VERSION,
                "parameters": [max_segments, overlap_segments, min_segments, max_depth, max_calls]})}
