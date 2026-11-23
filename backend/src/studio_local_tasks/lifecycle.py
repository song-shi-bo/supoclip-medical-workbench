"""局部任务：创建/查询/列举与项目解析。"""

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


# ---------------------------------------------------------------------------
# 创建 / 查询
# ---------------------------------------------------------------------------

def create_task(*, project_id: str, clip_id: str, task_type: str,
                instruction: str | None = None,
                payload: dict[str, Any] | None = None,
                run: bool = True) -> dict[str, Any]:
    """建一个**有类型**的局部任务（并立即在后台执行）。

    这是助手/UI 唯一允许的「改某条成片」的入口：封面改封面、标题改标题、字幕改字幕、
    时间线改时间线，绝不因为「改封面」而重跑 AUTO_EDIT。
    """
    project_id = store.validate_project_id(project_id)
    clip_id = store.validate_clip_id(clip_id)
    if task_type not in TASK_TYPES:
        raise LocalTaskError(E_TASK_TYPE_UNKNOWN,
                             f"未知任务类型 {task_type!r}；可用: {list(TASK_TYPES)}")
    if task_type == TASK_AUTO_EDIT:
        raise LocalTaskError(
            E_TASK_TYPE_UNKNOWN,
            "AUTO_EDIT 只能由「开始自动剪辑」入口提交（转写完成且尚未首次生成成片时）；"
            "已有成片的封面/标题/字幕/时间线修改请用局部任务类型")
    # 局部任务必须作用在台账里已存在的 clip 上：没有成片就没有可改的资产。
    store.entry_for(project_id, clip_id)
    if task_type == TASK_APPLY_COVER:
        existing = _existing_apply_cover_task(project_id, clip_id)
        if existing is not None:
            return existing

    task_id = new_task_id(task_type)
    # Phase B 接线点 3：每个局部任务创建时也冻结自己的规则快照（快照目录 = 本任务目录），
    # 于是「改封面/标题/字幕/时间线」用的是**提交这一版规则**，而不是跑的时候磁盘上的规则。
    # 清单缺失/损坏会显式报错（SkillRegistryError），不让任务在无规则状态下悄悄跑。
    access = prepare_project_operation(task_id, project_id)
    try:
        snapshot = studio_workflow.build_task_snapshot(task_id, snapshot_dir=_snapshot_dir(task_id))
        versions = skill_registry.task_versions(snapshot)
        record: dict[str, Any] = {
            "task_id": task_id,
            "task_type": task_type,
            "project_id": project_id,
            "clip_id": clip_id,
            "instruction": (instruction or "")[:1000] or None,
            "payload": payload or {},
            "status": "queued",
            "stage": STAGE_ZH["queued"],
            "progress": 0.0,
            "created_at": store.utc_now(),
            "updated_at": store.utc_now(),
            "result": None,
            "error": None,
            "artifacts": [],
            "workflow_version": versions.get("workflow_version"),
            "skill_versions": versions.get("skill_versions") or {},
            "workflow_snapshot_path": versions.get("snapshot_path"),
        }
        # 用户在本次任务里显式指定的规则（用户指令层）也冻结进快照，便于事后复现「当时到底用什么风格」。
        record["rules_source"] = dict(snapshot.get("rules_source") or {})
        _save(record)
        _log_line(record)
        if run:
            from .runner import _submit  # 惰性导入避免 lifecycle→runner→handlers→lifecycle 循环
            _submit(record)
        return record
    except Exception:
        access.release(task_id)
        raise



def get_task(task_id: str) -> dict[str, Any]:
    record = _read_json(_record_path(task_id))
    if record is None:
        status = _read_json(_status_path(task_id))
        if status is None:
            raise LocalTaskError(E_TASK_NOT_FOUND, f"任务不存在: {task_id}")
        return status
    return record


def _resolve_project_id(task_id: str) -> str | None:
    """旧任务的 status 文件可能没写 project_id：回退读任务 JSON 的 bridge.project_id。"""
    if not TASK_ID_PATTERN.fullmatch(task_id) or not task_id.startswith("autoedit_"):
        return None
    for directory in ("completed", "processing", "in", "failed"):
        candidate = _tasks_root() / directory / f"{task_id}.json"
        document = _read_json(candidate)
        if document:
            return str((document.get("bridge") or {}).get("project_id") or "") or None
    return None


def _existing_apply_cover_task(project_id: str, clip_id: str) -> dict[str, Any] | None:
    """「应用封面」的幂等闸门（2026-10-21）：绝不因重复点击重复创建渲染任务。

    1. 同一 clip 已有 queued/processing 的应用任务 → 直接复用（前端轮询同一个任务）；
    2. 该封面草稿**已经应用过**且没有新草稿 → 复用最后一次成功的应用任务，
       前端立刻拿到 completed（不会再次渲染、不会再次替换成片）。
    """
    try:
        entry = store.entry_for(project_id, clip_id)
        render_task_id = str(entry.get("render_task_id") or "")
        pending = template_pending_state(render_task_id, clip_id) if render_task_id else None
    except Exception:  # noqa: BLE001 — 查不到草稿就按「没有可复用任务」处理，交给后续显式校验
        pending = None
    records = [t for t in list_tasks(project_id=project_id, clip_id=clip_id)
               if str(t.get("task_type") or "") == TASK_APPLY_COVER]
    for item in records:
        if str(item.get("status") or "") in ("queued", "processing"):
            return item
    if isinstance(pending, dict) and pending.get("applied"):
        done = [t for t in records if str(t.get("status") or "") == "completed"]
        if done:
            return done[0]   # list_tasks 按 created_at 倒序：第一条就是最近一次
    return None


def list_tasks(project_id: str | None = None, clip_id: str | None = None,
               limit: int = 100) -> list[dict[str, Any]]:
    """统一任务表：局部任务 + AUTO_EDIT 状态文件，按创建时间倒序。"""
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in (_tasks_root() / LOCALTASKS_DIRNAME).glob("*.json"):
        record = _read_json(path)
        if not record:
            continue
        seen.add(str(record.get("task_id")))
        tasks.append(record)
    for path in (_tasks_root() / STATUS_DIRNAME).glob("*.json"):
        task_id = path.stem
        if task_id in seen or not TASK_ID_PATTERN.fullmatch(task_id):
            continue
        status = _read_json(path)
        if not status:
            continue
        status.setdefault("task_type", task_type_of(task_id))
        if not status.get("project_id"):
            status["project_id"] = _resolve_project_id(task_id)
        tasks.append(status)
    if project_id:
        tasks = [t for t in tasks if str(t.get("project_id") or "") == project_id]
    if clip_id:
        tasks = [t for t in tasks if str(t.get("clip_id") or "") == clip_id]
    tasks.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return tasks[:limit]


