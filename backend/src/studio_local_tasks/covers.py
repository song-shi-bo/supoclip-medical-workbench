"""局部任务：封面版本历史、激活、删除与撤销。"""

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

from .context import (  # noqa: F401
    _cjk_chars,
    _clip_final_sha,
    _clip_paths,
    _clip_relative,
    _clip_transcript_window,
    _cover_path,
    _forbidden_hits,
    _instruction_from_payload,
    _is_grounded_in_transcript,
    _record_snapshot,
    _render_source_path,
    _render_task_document,
    _skill_ctx,
    _skill_line,
    _snapshot_dir,
    _task_relative,
    _version_fields,
    clip_segments_from_render_task,
    effective_transcript_segments,
    merge_subtitle_overrides,
    refinement_segments_for_clip,
    subtitle_overrides_from_cues,
)


def cover_history(project_id: str, clip_id: str) -> list[dict[str, Any]]:
    """封面版本管理页数据源：物理快照 ⊕ 台账历史（主题/钩子/来源/指令/qa_status）。

    - ``active``：是否当前使用中（台账 active_revision，缺省回退最新 revision）；
    - ``source``：``auto``（v1 自动剪辑首版）/ ``assistant_regenerate``（重制封面，
      卡片按钮与 AI 助手自然语言共用同一条 REGENERATE_COVER 链）；
    - ``deletable``：v1 是物理回滚锚点、当前使用中的版本，均不可删；
    - 老历史条目没有 qa_status 时显示 passed——cover_replace 对 QC 失败直接抛
      E_COVER_QC_FAILED、绝不落版本，「能落台账即过门禁」是既有不变量。
    """
    entry = store.entry_for(project_id, clip_id)
    render_task_id = str(entry.get("render_task_id") or "")
    from ..studio_clip_locator import clip_dir as locate_clip_dir, clip_asset
    clip_dir = locate_clip_dir(_tasks_root(), render_task_id, str(clip_id))
    cover = entry.get("cover") if isinstance(entry.get("cover"), dict) else {}
    history = {int(h.get("revision") or 0): h
               for h in (cover.get("history") or []) if isinstance(h, dict)}
    active_revision = int(cover.get("active_revision") or cover.get("revision") or 0)
    current_path = str(cover.get("path") or "")
    enriched: list[dict[str, Any]] = []
    for rev in list_revisions(clip_dir):
        number = int(rev.get("revision") or 0)
        ledger = history.get(number) or {}
        if number == active_revision and current_path:
            image_path = current_path
        elif number >= 2 and rev.get("cover_jpg"):
            image_path = clip_asset(_tasks_root(), render_task_id, clip_id,
                f"cover_revisions/rev-{number:03d}/{rev.get('cover_jpg')}",
                required=False).relative_to(_tasks_root().resolve()).as_posix()
        elif rev.get("baseline_cover_jpg"):
            image_path = clip_asset(_tasks_root(), render_task_id, clip_id,
                f"cover_backup/{rev.get('baseline_cover_jpg')}",
                required=False).relative_to(_tasks_root().resolve()).as_posix()
        else:
            image_path = ledger.get("path")
        enriched.append({
            "revision": number,
            "cover_asset_id": ledger.get("cover_asset_id")
            or (f"cover-{clip_id}-v{number}" if number else None),
            "created_at": rev.get("created_at") or ledger.get("created_at"),
            "kind": ledger.get("kind") or rev.get("kind"),
            "source": "auto" if number <= 1 else "assistant_regenerate",
            "title": ledger.get("title"),
            "hook": ledger.get("hook"),
            "instruction": ledger.get("instruction"),
            "qa_status": ledger.get("qa_status") or "passed",
            "style_id": ledger.get("style_id") or rev.get("style_id"),
            "path": image_path,
            "task_id": ledger.get("task_id") or render_task_id,
            "active": number == active_revision,
            "deletable": number >= 2 and number != active_revision,
        })
    return enriched


def activate_cover(project_id: str, clip_id: str, revision: int,
                   *, task_id: str | None = None) -> dict[str, Any]:
    """「设为当前封面」：任意历史版本原地生效，并同步台账（不新增 clip/revision）。"""
    entry = store.entry_for(project_id, clip_id)
    render_task_id = str(entry.get("render_task_id") or "")
    target = history_entry_qa(entry, revision)
    if str(target.get("qa_status") or "passed") not in ("", "passed"):
        raise LocalTaskError("E_COVER_QA_FAILED",
                             f"v{revision} 未通过封面 QA（{target.get('qa_status')}），不能设为当前封面")
    result = activate_cover_revision(render_task_id, clip_id, revision)
    store.set_cover_pointer(project_id, clip_id, revision=int(revision),
                            task_id=task_id or render_task_id)
    card = store.card_view(store.entry_for(project_id, clip_id))
    result["card"] = card
    result["cover_asset_id"] = card["cover"]["cover_asset_id"]
    return result


def delete_cover(project_id: str, clip_id: str, revision: int) -> dict[str, Any]:
    """删除未被当前使用的历史封面版本：先物理后台账，任一步失败都不落台账。"""
    entry = store.entry_for(project_id, clip_id)
    render_task_id = str(entry.get("render_task_id") or "")
    delete_cover_revision(render_task_id, clip_id, revision)
    store.prune_cover_history(project_id, clip_id, revision=int(revision))
    return {"project_id": project_id, "clip_id": clip_id,
            "deleted_revision": int(revision)}


def history_entry_qa(entry: dict[str, Any], revision: int) -> dict[str, Any]:
    """取台账历史里某版本的 QA 标记（没有台账条目时视为通过）。"""
    cover = entry.get("cover") if isinstance(entry.get("cover"), dict) else {}
    for item in cover.get("history") or []:
        if isinstance(item, dict) and int(item.get("revision") or 0) == int(revision):
            return item
    return {}


def undo_cover(project_id: str, clip_id: str, revision: int = 1,
               *, task_id: str | None = None) -> dict[str, Any]:
    """一键撤销封面到历史版本（默认 v1），并同步台账（原地生效，不新增 clip）。"""
    entry = store.entry_for(project_id, clip_id)
    render_task_id = str(entry.get("render_task_id") or "")
    result = undo_cover_revision(render_task_id, clip_id, revision)
    store.set_cover_pointer(project_id, clip_id, revision=int(revision),
                            task_id=task_id or render_task_id)
    card = store.card_view(store.entry_for(project_id, clip_id))
    result["card"] = card
    result["cover_asset_id"] = card["cover"]["cover_asset_id"]
    return result


__all__ = [
    "TASK_TYPES", "TASK_AUTO_EDIT", "TASK_REGENERATE_COVER", "TASK_UPDATE_TITLE",
    "TASK_UPDATE_SUBTITLE_STYLE", "TASK_UPDATE_TIMELINE",
    "TASK_UPDATE_SUBTITLE", "TASK_UPDATE_TRANSFORM", "TASK_UPDATE_AUDIO",
    "TASK_UPDATE_EFFECTS", "TASK_RENDER_CLIP",
    "TASK_PREFIX", "TASK_ID_PATTERN", "FULL_PIPELINE_TYPES", "RENDER_DEFERRED_REASON",
    "LocalTaskError", "create_task", "get_task", "list_tasks", "task_type_of",
    "cover_history", "undo_cover", "activate_cover", "delete_cover", "_tasks_root()",
    "clip_segments_from_render_task", "effective_transcript_segments",
    "subtitle_overrides_from_cues", "merge_subtitle_overrides",
]
