"""局部任务：执行引擎（线程提交、租约配额、_run_task 分发、_HANDLERS 注册表）。"""

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

from .handlers import (  # noqa: F401
    _handle_apply_cover,
    _handle_regenerate_cover,
    _handle_render_clip,
    _handle_update_audio,
    _handle_update_effects,
    _handle_update_subtitle,
    _handle_update_subtitle_style,
    _handle_update_timeline,
    _handle_update_title,
    _handle_update_transform,
)

from .lifecycle import (  # noqa: F401
    _existing_apply_cover_task,
    _resolve_project_id,
    create_task,
    get_task,
    list_tasks,
)


# ---------------------------------------------------------------------------
# 后台执行
# ---------------------------------------------------------------------------

_executor_lock = threading.Lock()
_clip_locks: dict[str, threading.Lock] = {}


def _clip_lock(project_id: str, clip_id: str) -> threading.Lock:
    key = f"{project_id}:{clip_id}"
    with _executor_lock:
        lock = _clip_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _clip_locks[key] = lock
        return lock


def _submit(record: dict[str, Any], *, quota_renew_seconds: float = 5.0) -> None:
    """后台线程执行；同一 clip 串行（避免两个封面任务互相覆盖）。"""
    access = access_ledger()
    task_id = str(record["task_id"])
    def runner() -> None:
        stopped = threading.Event()
        def renew() -> None:
            while not stopped.wait(quota_renew_seconds):
                access.renew(task_id)
        heartbeat = threading.Thread(target=renew, name=f"local-quota-{task_id}", daemon=True)
        try:
            heartbeat.start()
            lock = _clip_lock(str(record["project_id"]), str(record["clip_id"]))
            with lock:
                _run_task(record)
        finally:
            stopped.set()
            if heartbeat.is_alive():
                heartbeat.join(timeout=1)
            access.release(task_id)

    thread = threading.Thread(target=runner, name=f"local-task-{record['task_id']}", daemon=True)
    thread.start()


def _run_task(record: dict[str, Any]) -> None:
    task_id = str(record["task_id"])
    handler = _HANDLERS.get(str(record["task_type"]))
    record["status"] = "processing"
    record["stage"] = STAGE_ZH["processing"]
    record["progress"] = 0.1
    record["started_at"] = store.utc_now()
    _save(record)
    _log_line(record)
    if handler is None:
        record.update({"status": "failed", "stage": STAGE_ZH["failed"], "progress": 1.0,
                       "error": f"没有实现的任务类型 {record['task_type']}"})
        _save(record)
        _log_line(record)
        return
    try:
        result = handler(record)
        record["result"] = result
        record["stage"] = STAGE_ZH["completed"]
        record["status"] = "completed"
        record["progress"] = 1.0
    except (CoverReplaceError, LocalTaskError, store.ProjectStoreError,
            skills.SkillExecutionError,
            # Inspector Phase 1：配置/字幕/转写/音频层的显式失败也必须保真 E_ 码，
            # 否则「bad transform / 坏枚举 / 越界音量」只会剩下一句裸异常名。
            clip_config.ClipConfigError, subtitle_compose.SubtitleComposeError,
            transcript_revisions.TranscriptRevisionError, clip_audio.ClipAudioError) as exc:
        code = getattr(exc, "code", None) or "E_LOCAL_TASK_FAILED"
        record["status"] = "failed"
        record["stage"] = STAGE_ZH["failed"]
        record["progress"] = 1.0
        # 错误码保真：cutout_v3 缺权重时用户必须看到 E_CUTOUT_WEIGHTS_MISSING 与安装指引；
        # skill 适配器失败时保留 E_SKILL_* 码（如字幕适配器未实现、video-brain 不可达）。
        record["error"] = f"{code}: {exc}" if code not in str(exc) else str(exc)
    except Exception as exc:  # noqa: BLE001 — 兜底：任何异常都不能让任务悬挂
        logger.exception("局部任务失败 %s", task_id)
        record["status"] = "failed"
        record["stage"] = STAGE_ZH["failed"]
        record["progress"] = 1.0
        record["error"] = f"{type(exc).__name__}: {exc}"
    _save(record)
    _log_line(record)
    try:
        if record["status"] == "completed" and record.get("workflow_version"):
            # 规则溯源（Phase B）：把本次任务生效的规则版本也写到 clip 条目上，
            # 卡片与成片详情因此能显示「使用规则版本：workflow vX / cover vX …」；
            # 逐次历史仍完整保留在 task_history 里。
            store.update_fields(str(record["project_id"]), str(record["clip_id"]), {
                "workflow_version": record.get("workflow_version"),
                "skill_versions": record.get("skill_versions") or {},
                "workflow_snapshot_path": record.get("workflow_snapshot_path"),
            })
        store.record_task(str(record["project_id"]), str(record["clip_id"]), {
            "task_id": task_id,
            "task_type": record["task_type"],
            "status": record["status"],
            "stage": record["stage"],
            "instruction": record.get("instruction"),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "summary": _summarise(record),
            "error": record.get("error"),
            # 规则溯源（Phase B）：台账 task_history 也带上本任务用的规则版本，
            # 于是「这条成片是哪版规则改出来的」在台账里可查、可审计。
            "workflow_version": record.get("workflow_version"),
            "skill_versions": record.get("skill_versions") or {},
            "workflow_snapshot_path": record.get("workflow_snapshot_path"),
            "rules_source": record.get("rules_source") or {},
        })
    except Exception:  # noqa: BLE001 — 台账登记失败不影响任务结果
        logger.warning("任务历史登记失败 %s", task_id, exc_info=True)


def _summarise(record: dict[str, Any]) -> str:
    result = record.get("result") or {}
    task_type = record.get("task_type")
    if record["status"] != "completed":
        return str(record.get("error") or "失败")
    if task_type == TASK_REGENERATE_COVER:
        return (f"封面已更新 · v{result.get('cover_revision')}"
                f"（{result.get('style_id') or '默认风格'}）")
    if task_type == TASK_APPLY_COVER:
        if result.get("idempotent"):
            return "封面早已应用，未重复渲染"
        return (f"封面已应用：{result.get('intro_duration_s')}s 片头已加入成片"
                f"（{result.get('style_id') or '版型封面'}）")
    if task_type == TASK_UPDATE_TITLE:
        return f"标题已更新为「{result.get('title')}」"
    if task_type == TASK_UPDATE_SUBTITLE_STYLE:
        return f"字幕样式已更新（render_revision {result.get('render_revision')}）"
    if task_type == TASK_UPDATE_TIMELINE:
        return f"时间线已更新（{result.get('ranges_count')} 段）"
    # ---- Inspector Phase 1 的五个新类型（中文摘要，UI/任务历史直接展示）----
    if task_type == TASK_UPDATE_SUBTITLE:
        return (f"字幕已更新并生成新版本 rev-{result.get('revision')}"
                f"（{result.get('cue_count')} 条 cue；烧录导出属 Phase 2）")
    if task_type == TASK_UPDATE_TRANSFORM:
        return (f"画面变换已应用（{'已镜像 sidecar' if result.get('mirror') == 'synced' else 'sidecar 未镜像'}；"
                f"导出侧生效属 Phase 2）")
    if task_type == TASK_UPDATE_AUDIO:
        state = "已静音" if result.get("muted") else f"原声 {result.get('voice_volume')}"
        return (f"音频设置已更新（{state}，{len(result.get('audio_tracks') or [])} 条 BGM 轨；"
                f"混音导出属 Phase 2）")
    if task_type == TASK_UPDATE_EFFECTS:
        effects = result.get("effects") or {}
        return (f"效果设置已更新（分辨率 {effects.get('export_resolution')}；"
                f"贴纸/片尾/滤镜/转场导出生效属 Phase 2）")
    if task_type == TASK_RENDER_CLIP:
        return (f"已登记待渲染（{result.get('reason')}）；"
                f"未推进 render_revision（render_revision {result.get('render_revision')}）")
    return "完成"


_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    TASK_REGENERATE_COVER: _handle_regenerate_cover,
    TASK_APPLY_COVER: _handle_apply_cover,
    TASK_UPDATE_TITLE: _handle_update_title,
    TASK_UPDATE_SUBTITLE_STYLE: _handle_update_subtitle_style,
    TASK_UPDATE_TIMELINE: _handle_update_timeline,
    TASK_UPDATE_SUBTITLE: _handle_update_subtitle,
    TASK_UPDATE_TRANSFORM: _handle_update_transform,
    TASK_UPDATE_AUDIO: _handle_update_audio,
    TASK_UPDATE_EFFECTS: _handle_update_effects,
    TASK_RENDER_CLIP: _handle_render_clip,
}
