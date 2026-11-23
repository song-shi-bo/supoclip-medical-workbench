"""局部任务：常量、E_ 码、落盘路径与原子读写（阶段5·点2 拆分）。"""

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



logger = logging.getLogger("studio_local_tasks")

TASKS_ROOT = Path(os.getenv("STUDIO_TASKS_ROOT", "/app/tasks")).resolve()


def _tasks_root() -> Path:
    """_tasks_root() 的调用时解析：兼容测试对**包命名空间**的 monkeypatch。

    单文件时代 _tasks_root() 是唯一模块全局，测试 `setattr(tasks, "_tasks_root()", root)`
    直接生效；拆包后各子模块持有各自绑定——函数体一律经本 helper 取值，
    从 sys.modules 里的包对象实时读取（未 patch 时即为原值）。
    """
    import sys
    pkg_name = __name__.rsplit(".", 1)[0]
    return getattr(sys.modules[pkg_name], "TASKS_ROOT", TASKS_ROOT)

TASK_AUTO_EDIT = "AUTO_EDIT"
TASK_REGENERATE_COVER = "REGENERATE_COVER"
TASK_UPDATE_TITLE = "UPDATE_TITLE"
TASK_UPDATE_SUBTITLE_STYLE = "UPDATE_SUBTITLE_STYLE"
TASK_UPDATE_TIMELINE = "UPDATE_TIMELINE"
# Inspector Phase 1（契约 §4）：五个新的局部类型，前缀与 clip_config.TASK_PREFIX_BY_TYPE 对齐。
TASK_UPDATE_SUBTITLE = "UPDATE_SUBTITLE"
TASK_UPDATE_TRANSFORM = "UPDATE_TRANSFORM"
TASK_UPDATE_AUDIO = "UPDATE_AUDIO"
TASK_UPDATE_EFFECTS = "UPDATE_EFFECTS"
TASK_RENDER_CLIP = "RENDER_CLIP"
# 封面「应用」（2026-10-21）：把待应用草稿烧成 0.8s 片头并原子替换当前成片。
# 与 REGENERATE_COVER 的区别：那个只出/换封面图（可选换片头），这个是一次真正的
# 片头渲染任务（可轮询进度、失败保原成片、重复点击不重复建任务）。
TASK_APPLY_COVER = "APPLY_COVER"

TASK_TYPES: tuple[str, ...] = (
    TASK_AUTO_EDIT, TASK_REGENERATE_COVER, TASK_APPLY_COVER, TASK_UPDATE_TITLE,
    TASK_UPDATE_SUBTITLE_STYLE, TASK_UPDATE_TIMELINE,
    TASK_UPDATE_SUBTITLE, TASK_UPDATE_TRANSFORM, TASK_UPDATE_AUDIO,
    TASK_UPDATE_EFFECTS, TASK_RENDER_CLIP,
)

TASK_PREFIX: dict[str, str] = {
    TASK_AUTO_EDIT: "autoedit",
    TASK_REGENERATE_COVER: "cover",
    TASK_UPDATE_TITLE: "title",
    TASK_UPDATE_SUBTITLE_STYLE: "subtitle",
    TASK_UPDATE_TIMELINE: "edit",
    TASK_UPDATE_SUBTITLE: "msub",
    TASK_UPDATE_TRANSFORM: "xfm",
    TASK_UPDATE_AUDIO: "aud",
    TASK_UPDATE_EFFECTS: "fx",
    TASK_RENDER_CLIP: "rnd",
    TASK_APPLY_COVER: "coverapply",
}
PREFIX_TASK: dict[str, str] = {prefix: task_type for task_type, prefix in TASK_PREFIX.items()}

# 既有前缀（含老任务的 ``subtitle``）保持不变，只把新前缀加进同一 alternation：
# 老任务仍能被 task_type_of 识别，新前缀与 task_type 保持一一对应。
# 注意顺序：coverapply 必须排在 cover 前面，否则正则先吃掉 "cover" 再要求下划线，
# "coverapply_xxx" 会判成非法 id。
TASK_ID_PATTERN = re.compile(
    r"^(autoedit|coverapply|cover|title|subtitle|edit|msub|xfm|aud|fx|rnd)_[a-f0-9]{12}$")

# 只有 AUTO_EDIT 会「重跑自动剪辑」；其余类型都必须走局部通道。
FULL_PIPELINE_TYPES = {TASK_AUTO_EDIT}

LOCALTASKS_DIRNAME = "localtasks"
STATUS_DIRNAME = "status"
LOGS_DIRNAME = "logs"
LOG_FILENAME = "local_tasks.jsonl"
CLIP_TIMELINE_FILENAME = "clip_timeline.json"
CLIP_SUBTITLE_STYLE_FILENAME = "subtitle_style.json"

# 未实现能力：如实上报，绝不伪实现（字幕合成适配器 Phase A 即显式抛 E_SKILL_NOT_IMPLEMENTED）。
SUBTITLE_ADAPTER_GAP = "E_SKILL_NOT_IMPLEMENTED"

#: RENDER_CLIP 的固定中文原因（契约 §4 原文；HTTP/UI 都直接透出，避免各处各写一份）。
RENDER_DEFERRED_REASON = "需 Phase 2 渲染流程支持"
#: 导出侧尚未生效的统一标注：配置/版本已落地，烧录/混音/滤镜/转场属 Phase 2。
EXPORT_PHASE2 = "phase2"

STAGE_ZH = {
    "queued": "任务已受理",
    "processing": "处理中",
    "completed": "已完成",
    "failed": "失败",
}


class LocalTaskError(ValueError):
    """带 E_ 码的显式失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


E_TASK_TYPE_UNKNOWN = "E_TASK_TYPE_UNKNOWN"
E_TASK_NOT_FOUND = "E_TASK_NOT_FOUND"
E_CLIP_NOT_REGISTERED = "E_CLIP_NOT_REGISTERED"
E_TASK_NOT_RUNNABLE = "E_TASK_NOT_RUNNABLE"


# ---------------------------------------------------------------------------
# 路径与基础读写
# ---------------------------------------------------------------------------

def task_type_of(task_id: str) -> str | None:
    match = TASK_ID_PATTERN.fullmatch(str(task_id or ""))
    if not match:
        return None
    return PREFIX_TASK.get(match.group(1))


def new_task_id(task_type: str) -> str:
    prefix = TASK_PREFIX.get(task_type)
    if not prefix:
        raise LocalTaskError(E_TASK_TYPE_UNKNOWN,
                             f"未知任务类型 {task_type!r}；可用: {list(TASK_TYPES)}")
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _record_path(task_id: str) -> Path:
    return _tasks_root() / LOCALTASKS_DIRNAME / f"{task_id}.json"


def _status_path(task_id: str) -> Path:
    return _tasks_root() / STATUS_DIRNAME / f"{task_id}.json"


def _log_path() -> Path:
    return _tasks_root() / LOGS_DIRNAME / LOG_FILENAME


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{os.urandom(6).hex()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _log_line(record: dict[str, Any]) -> None:
    """追加式任务日志：``grep REGENERATE_COVER`` 就是「只建了局部任务」的直接证据。"""
    entry = {
        "at": store.utc_now(),
        "task_id": record.get("task_id"),
        "task_type": record.get("task_type"),
        "project_id": record.get("project_id"),
        "clip_id": record.get("clip_id"),
        "status": record.get("status"),
        "stage": record.get("stage"),
        "instruction": record.get("instruction"),
        "error": record.get("error"),
    }
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    logger.info("[studio-local-task] %s %s %s/%s %s", entry["task_type"], entry["task_id"],
                entry["project_id"], entry["clip_id"], entry["status"])


def _write_status(record: dict[str, Any]) -> None:
    """状态镜像写进与 AUTO_EDIT 同一张任务表（``status/{task_id}.json``）。"""
    _atomic_json(_status_path(str(record["task_id"])), {
        "task_id": record.get("task_id"),
        "task_type": record.get("task_type"),
        "project_id": record.get("project_id"),
        "clip_id": record.get("clip_id"),
        "status": record.get("status"),
        "stage": record.get("stage"),
        "progress": record.get("progress", 0.0),
        "error": record.get("error"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "artifacts": record.get("artifacts") or [],
        # 规则版本随状态镜像一起走：任务详情 API 与任务表都能直接显示「用的哪版规则」。
        "workflow_version": record.get("workflow_version"),
        "skill_versions": record.get("skill_versions"),
        "workflow_snapshot_path": record.get("workflow_snapshot_path"),
    })


def _save(record: dict[str, Any]) -> dict[str, Any]:
    record["updated_at"] = store.utc_now()
    _atomic_json(_record_path(str(record["task_id"])), record)
    _write_status(record)
    return record


