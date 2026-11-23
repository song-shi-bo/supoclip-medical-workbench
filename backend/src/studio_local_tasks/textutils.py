"""局部任务：标题折行、指令解析、区间加减与规整（纯函数）。"""

from __future__ import annotations

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .. import clip_audio, clip_config, subtitle_compose, transcript_revisions
from .. import skill_registry, skills, studio_workflow
from .. import studio_project_store as store
from ..studio_access import prepare_project_operation, ledger as access_ledger
from ..cover_replace import (
    DEFAULT_COVER_STYLE,
    E_COVER_DRAFT_MISSING,
    CoverReplaceError,
    _sha256,
    activate_cover_revision,
    apply_cover_duration_s,
    apply_template_cover,
    delete_cover_revision,
    list_revisions,
    replace_cover,
    template_pending_state,
    undo_cover_revision,
    validate_style_id,
)


from .records import (  # noqa: F401
    _tasks_root,
    CLIP_SUBTITLE_STYLE_FILENAME,
    CLIP_TIMELINE_FILENAME,
    EXPORT_PHASE2,
    E_CLIP_NOT_REGISTERED,
    E_TASK_NOT_FOUND,
    E_TASK_NOT_RUNNABLE,
    E_TASK_TYPE_UNKNOWN,
    FULL_PIPELINE_TYPES,
    LOCALTASKS_DIRNAME,
    LOGS_DIRNAME,
    LOG_FILENAME,
    LocalTaskError,
    PREFIX_TASK,
    RENDER_DEFERRED_REASON,
    STAGE_ZH,
    STATUS_DIRNAME,
    SUBTITLE_ADAPTER_GAP,
    TASK_APPLY_COVER,
    TASK_AUTO_EDIT,
    TASK_ID_PATTERN,
    TASK_PREFIX,
    TASK_REGENERATE_COVER,
    TASK_RENDER_CLIP,
    TASK_TYPES,
    TASK_UPDATE_AUDIO,
    TASK_UPDATE_EFFECTS,
    TASK_UPDATE_SUBTITLE,
    TASK_UPDATE_SUBTITLE_STYLE,
    TASK_UPDATE_TIMELINE,
    TASK_UPDATE_TITLE,
    TASK_UPDATE_TRANSFORM,
    _atomic_json,
    _log_line,
    _log_path,
    _read_json,
    _record_path,
    _save,
    _status_path,
    _write_status,
    logger,
    new_task_id,
    task_type_of,
)




# ---------------------------------------------------------------------------
# 文案 / 时间线小工具（纯函数，便于单测）
# ---------------------------------------------------------------------------

def _two_lines(text: str, max_total: int = 12) -> tuple[str, str]:
    """把标题排成「两行、每行 ≥2 字、总计 ≤max_total 字」（与封面固化规则一致）。"""
    cleaned = "".join(ch for ch in str(text or "") if not ch.isspace())
    if not cleaned:
        return "医疗科普", "健康提示"
    if len(cleaned) <= 2:
        return cleaned, cleaned
    per_line = max(2, max_total // 2)
    head, tail = cleaned[:per_line], cleaned[per_line:max_total]
    if len(tail) < 2:
        cut = max(2, min(len(cleaned) - 2, per_line))
        head, tail = cleaned[:cut], cleaned[cut:max_total]
    if len(tail) < 2:                      # 太短：退化成「同句两行」而不是丢字
        tail = head
    return head, tail


def _derive_title(entry: dict[str, Any], transcript_text: str = "",
                  *, must_be_grounded: bool = True) -> str:
    """没给新标题时重排一版（≤12 汉字、两行）。

    规则（清单 ``title_rules.must_be_grounded_in_transcript``）：优先沿用既有来源
    （封面历史里的标题/指令/主题），但**只有在忠于该片转写时才用**；否则退回转写原文。
    拿不到转写时按既有回退链路出标题，并在结果里如实标记未校验。
    """
    text = ""
    cover = entry.get("cover") or {}
    history = cover.get("history") or []
    for item in reversed(history):
        if isinstance(item, dict) and item.get("title"):
            text = str(item["title"])
            break
    if not text:
        for item in reversed(history):
            if isinstance(item, dict):
                text = str(item.get("instruction") or "")
                break
    text = text or str(entry.get("title") or entry.get("topic") or "")
    if must_be_grounded and transcript_text:
        if not text or not _is_grounded_in_transcript(text, transcript_text):
            text = transcript_text
    elif not text:
        text = transcript_text
    head, tail = _two_lines(text)
    return head + tail


def _parse_font_scale(instruction: str) -> float | None:
    """「字幕放大一点」→ 1.15；「缩小到 0.9」→ 0.9；识别不到返回 None。"""
    if not instruction:
        return None
    explicit = re.search(r"(\d+(?:\.\d+)?)\s*(?:倍|×|x)", instruction)
    if explicit:
        try:
            return max(0.5, min(2.0, float(explicit.group(1))))
        except ValueError:
            return None
    percent = re.search(r"(\d+)\s*%", instruction)
    if percent:
        return max(0.5, min(2.0, float(percent.group(1)) / 100))
    if re.search(r"放大|大一点|大点|更大", instruction):
        return 1.15
    if re.search(r"缩小|小一点|小点|更小", instruction):
        return 0.9
    return None


def _parse_remove_ranges(instruction: str) -> list[list[float]]:
    """解析「删除第 8 秒到第 10 秒」→ [[8.0, 10.0]]。"""
    match = re.search(
        r"(?:删除|删掉|去掉|移除|剪掉)[^0-9]{0,12}(?:第)?\s*(\d+(?:\.\d+)?)\s*秒?"
        r"\s*(?:到|至|-|~|—|―)\s*(?:第)?\s*(\d+(?:\.\d+)?)\s*秒",
        instruction or "",
    )
    if not match:
        return []
    start, end = float(match.group(1)), float(match.group(2))
    if end <= start:
        return []
    return [[start, end]]


def _subtract_ranges(start: float, end: float,
                     removals: list[Any]) -> list[dict[str, float]]:
    """把删除区间从 [start, end) 里抠掉，返回剩下的片段（全部落在原窗口内）。"""
    segments: list[tuple[float, float]] = [(float(start), float(end))]
    for item in removals or []:
        if isinstance(item, dict):
            low, high = float(item.get("start") or 0), float(item.get("end") or 0)
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            low, high = float(item[0]), float(item[1])
        else:
            continue
        if high <= low:
            continue
        nxt: list[tuple[float, float]] = []
        for seg_start, seg_end in segments:
            if high <= seg_start or low >= seg_end:
                nxt.append((seg_start, seg_end))
                continue
            if seg_start < low:
                nxt.append((seg_start, low))
            if high < seg_end:
                nxt.append((high, seg_end))
        segments = nxt
    return [{"start": round(a, 3), "end": round(b, 3)} for a, b in segments if b - a > 0.05]


def _normalise_ranges(ranges: list[Any], entry: dict[str, Any]) -> list[dict[str, float]]:
    """校验区间必须落在原 clip 窗口内且 start<end；非法项显式拒绝而不是静默丢弃。"""
    lo = float(entry.get("source_start") or 0.0)
    hi = float(entry.get("source_end") or 0.0)
    result: list[dict[str, float]] = []
    for index, item in enumerate(ranges, 1):
        if not isinstance(item, (dict, list, tuple)):
            raise LocalTaskError("E_TIMELINE_RANGE_INVALID", f"ranges[{index}] 不是区间对象")
        if isinstance(item, dict):
            start, end = float(item.get("start") or 0), float(item.get("end") or 0)
        else:
            if len(item) != 2:
                raise LocalTaskError("E_TIMELINE_RANGE_INVALID", f"ranges[{index}] 必须是 [start, end]")
            start, end = float(item[0]), float(item[1])
        if not start < end:
            raise LocalTaskError("E_TIMELINE_RANGE_INVALID",
                                 f"ranges[{index}] 边界非法: start={start}, end={end}")
        if start < lo - 0.001 or end > hi + 0.001:
            raise LocalTaskError(
                "E_TIMELINE_RANGE_OUT_OF_CLIP",
                f"ranges[{index}] 超出该成片原窗口 [{lo}, {hi}]，已拒绝（时间线只在 clip 内改）")
        result.append({"start": round(start, 3), "end": round(end, 3)})
    return result


