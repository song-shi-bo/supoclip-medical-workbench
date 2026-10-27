"""局部任务（Local Task）：把「什么都跑 AUTO_EDIT」拆成有类型的、按 clip 收敛的任务。

背景（改造要解决的问题）
------------------------
改造前助手的 `make_cover` / `render_clip` 动作都会走 `rerender_auto_edit_task()`：
新建一个 ``autoedit_xxxx`` 任务 → 新任务有自己的 output 目录 → 前端按任务聚合 clips
→ 左侧多出一张重复的「剪辑成片 01」卡片。文案/字幕/时间线类修改同样被粗暴地当成
「重跑自动剪辑」（3~4 分钟、磁盘增量 ~20MB）。

任务类型（本模块是唯一的事实源）
--------------------------------
======================  ==============  ==================================================
task_type               task_id 前缀   语义
======================  ==============  ==================================================
``AUTO_EDIT``           ``autoedit_``  转写完成后**首次**按既有固化规则生成成片（原有链路）
``REGENERATE_COVER``    ``cover_``     只换当前 clip 的封面图（+ 0.8s 片头），不重剪不重转写
``UPDATE_TITLE``        ``title_``     只更新标题/主题/关键词/封面文字
``UPDATE_SUBTITLE_STYLE`` ``subtitle_`` 只更新字幕样式配置
``UPDATE_TIMELINE``     ``edit_``      只改当前 clip 的时间线片段
``UPDATE_SUBTITLE``     ``msub``       字幕文本/时间/样式 → 写 config+overrides 并产出新 ASS/SRT 版本
``UPDATE_TRANSFORM``    ``xfm``        画面变换「应用」→ 写 clip ``transform`` + 既有 sidecar
``UPDATE_AUDIO``        ``aud``        BGM 音量/淡入出/原声音量/静音
``UPDATE_EFFECTS``      ``fx``         贴纸/片尾/滤镜/转场/导出分辨率
``RENDER_CLIP``         ``rnd``        本阶段只登记待渲染（真渲染属 Phase 2），不 bump 版本号
======================  ==============  ==================================================

Inspector Phase 1（契约 ``docs/inspector-phase1-contract.md`` §4）
----------------------------------------------------------------
后五个类型是右侧 Inspector 的**局部通道**：配置与版本落到位，但**导出侧真渲染属 Phase 2**
（响应里固定带 ``export_*: "phase2"`` / ``render_deferred`` 标注，绝不伪实现）。
它们的共同规则：

* 只改本 clip 的配置/资产（台账条目 + 该 clip 的 ``output/{render_task_id}/{clip_id}/``）；
* **绝不**调用 ``studio_auto_edit_bridge.submit_auto_edit_task`` / ``rerender_auto_edit_task``，
  也绝不创建 ``autoedit_*`` 任务（``FULL_PIPELINE_TYPES`` 仍只有 ``AUTO_EDIT``）；
* 校验复用 ``clip_config``（默认值/范围/枚举/transform 的唯一事实源），字幕合成复用
  ``subtitle_compose``，转写复用 ``transcript_revisions`` —— 本模块只负责编排与落盘。

落盘（刷新后可恢复；路径都在 ``STUDIO_TASKS_ROOT`` 下）
-------------------------------------------------------
- ``localtasks/{task_id}.json``   局部任务记录（含 instruction / payload / result）
- ``status/{task_id}.json``       状态镜像（与 AUTO_EDIT 共用同一张「任务表」）
- ``logs/local_tasks.jsonl``      追加式任务日志（日志可见：只建局部任务，不建 auto_edit）
- ``projects/{project_id}/clips.json``  项目台账（studio_project_store）
- ``output/{render_task_id}/{clip_id}/`` 成片与封面资产本体（路径不变）
- ``output/{render_task_id}/{clip_id}/cover_backup/rev-NNN/`` 封面历史版本（可撤销）

设计约束
--------
- **绝不新建 clip、绝不改 render_task_id**：所有局部任务都作用在台账里已有的 clip 上。
- 封面任务只走 ``cover_replace``（备份 + 片头局部拼接 + 幂等 + 可回滚）。
- 缺权重（``cutout_v3`` 的 u2net）→ 显式失败并带安装指引，不静默换风格。
- 任务在后台线程执行、状态可轮询；失败只影响自己那个字段，台账不会被写坏。
"""

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

from . import clip_audio, clip_config, subtitle_compose, transcript_revisions
from . import skill_registry, skills, studio_workflow
from . import studio_project_store as store
from .studio_access import prepare_project_operation, ledger as access_ledger
from .cover_replace import (
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
    return TASKS_ROOT / LOCALTASKS_DIRNAME / f"{task_id}.json"


def _status_path(task_id: str) -> Path:
    return TASKS_ROOT / STATUS_DIRNAME / f"{task_id}.json"


def _log_path() -> Path:
    return TASKS_ROOT / LOGS_DIRNAME / LOG_FILENAME


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
        candidate = TASKS_ROOT / directory / f"{task_id}.json"
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
    for path in (TASKS_ROOT / LOCALTASKS_DIRNAME).glob("*.json"):
        record = _read_json(path)
        if not record:
            continue
        seen.add(str(record.get("task_id")))
        tasks.append(record)
    for path in (TASKS_ROOT / STATUS_DIRNAME).glob("*.json"):
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


# ---------------------------------------------------------------------------
# 各类型处理器
# ---------------------------------------------------------------------------

def _clip_paths(record: dict[str, Any]) -> tuple[dict[str, Any], Path, str]:
    """返回（台账条目, 成片目录, 渲染任务 id）。"""
    project_id = str(record["project_id"])
    clip_id = str(record["clip_id"])
    entry = store.entry_for(project_id, clip_id)
    render_task_id = str(entry.get("render_task_id") or "")
    if not render_task_id:
        raise LocalTaskError(E_CLIP_NOT_REGISTERED,
                             f"clip {clip_id} 没有 render_task_id，无法定位成片")
    from .studio_clip_locator import clip_dir as locate_clip_dir
    clip_dir = locate_clip_dir(TASKS_ROOT, render_task_id, clip_id)
    return entry, clip_dir, render_task_id


def _cover_path(record: dict[str, Any], render_task_id: str, clip_id: str) -> str:
    from .studio_clip_locator import clip_asset
    return clip_asset(TASKS_ROOT, render_task_id, clip_id, "cover.jpg", required=False).relative_to(
        TASKS_ROOT.resolve()).as_posix()


def _clip_final_sha(clip_dir: Path) -> str | None:
    video = clip_dir / "clip_final.mp4"
    return _sha256(video) if video.is_file() else None


# ---------------------------------------------------------------------------
# 规则快照 → Skill 适配器（Phase B 接线点 3）
# ---------------------------------------------------------------------------

def _snapshot_dir(task_id: str) -> Path:
    """本任务自己的规则快照目录（用本模块的任务根，测试/部署可各自覆盖同一根）。

    已知边界缺口（如实兼容，不越界改别人的模块）：``studio_workflow.TASK_ID_PATTERN``
    的 id 白名单还没包含 Inspector Phase 1 的五个新前缀（msub/xfm/aud/fx/rnd），
    直接调 ``studio_workflow.snapshot_dir`` 会抛 ``E_TASK_ID_INVALID``，
    于是**任何**新类型局部任务在 ``create_task`` 阶段就建不出来。
    快照目录的约定与局部任务完全一致（``{root}/localtasks/{task_id}/``），
    因此这里按同一约定兜底解析 —— 绝不让「快照模块的 id 白名单」挡住整条局部编辑通道。
    """
    try:
        return studio_workflow.snapshot_dir(TASKS_ROOT, task_id)
    except studio_workflow.WorkflowError:
        return TASKS_ROOT / LOCALTASKS_DIRNAME / str(task_id)


def _record_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    """读回该任务**冻结**的规则快照（优先用记录里的路径，回退按任务 id 定位）。"""
    path = str(record.get("workflow_snapshot_path") or "")
    if path:
        document = skill_registry.read_workflow_snapshot(Path(path).parent)
        if document:
            return document
    try:
        return skill_registry.read_workflow_snapshot(_snapshot_dir(str(record["task_id"]))) or {}
    except studio_workflow.WorkflowError:  # 历史数据里的非法任务 id
        return {}


def _skill_ctx(record: dict[str, Any],
               user_instruction: dict[str, Any] | None = None
               ) -> tuple[skills.SkillContext, dict[str, Any]]:
    """按冻结快照构造适配器上下文（规则、skill 版本都来自快照，不读磁盘最新值）。"""
    snapshot = _record_snapshot(record)
    ctx = studio_workflow.skill_context(
        str(record["task_id"]), snapshot=snapshot, user_instruction=user_instruction or {})
    return ctx, snapshot


def _instruction_from_payload(payload: dict[str, Any],
                              mapping: dict[str, str]) -> dict[str, Any]:
    """把 payload 里的显式用户指令映射成「清单段.键」→ 值（四层优先级的最上层）。"""
    result: dict[str, Any] = {}
    for source_key, rule_key in mapping.items():
        value = payload.get(source_key)
        if value not in (None, ""):
            result[rule_key] = value
    return result


def _skill_line(ctx: skills.SkillContext, *skill_ids: str) -> dict[str, str]:
    """本次任务实际用到的各 skill 版本（来自快照，缺失时 unregistered）。"""
    return {skill_id: skills.skill_version(skill_id, ctx) for skill_id in skill_ids}


def _version_fields(record: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """任务结果里的版本字段（写进任务 JSON、状态镜像与台账 task_history）。"""
    fields: dict[str, Any] = {
        "workflow_version": record.get("workflow_version"),
        "skill_versions": record.get("skill_versions") or {},
        "workflow_snapshot_path": record.get("workflow_snapshot_path"),
    }
    fields.update(extra)
    return fields


def _render_task_document(render_task_id: str) -> dict[str, Any]:
    """AUTO_EDIT 的不可变任务 JSON（取源视频路径与转写用）。"""
    for directory in ("completed", "processing", "in", "failed"):
        document = _read_json(TASKS_ROOT / directory / f"{render_task_id}.json")
        if document:
            return document
    return {}


def _render_source_path(render_task_id: str) -> Path | None:
    """该 AUTO_EDIT 任务物化后的源视频绝对路径（相对任务根解析）。"""
    raw = str(_render_task_document(render_task_id).get("raw_video_path") or "")
    if not raw:
        return None
    resolved = (TASKS_ROOT / raw).resolve()
    if TASKS_ROOT not in resolved.parents or not resolved.is_file():
        return None
    return resolved


def clip_segments_from_render_task(render_task_id: str, clip_id: str) -> list[dict[str, Any]]:
    """从成片产物里取该 clip 的转写分段（成片时间基，已含 0.8s 片头时移）。

    转写 seed 与字幕合成共用同一份「成片自带分段」：用户的成片一旦渲染完成，
    Inspector 的转写/字幕两条链路不必再单独调一次「建转写基线」就能工作（惰性 seed）。
    ``read_auto_edit_task`` 的局部 import 避免模块级循环；任务不在/产物已回收时
    按「没有分段」如实返回空表，不抛错。
    """
    from .studio_auto_edit_bridge import read_auto_edit_task  # 局部 import：避免模块级循环

    if not render_task_id or not clip_id:
        return []
    try:
        view = read_auto_edit_task(render_task_id)
    except (FileNotFoundError, ValueError, OSError):
        return []
    for clip in view.get("clips") or []:
        if str(clip.get("clip_id")) == str(clip_id):
            return [item for item in (clip.get("segments") or []) if isinstance(item, dict)]
    return []


def _clip_transcript_window(record: dict[str, Any], render_task_id: str) -> list[dict[str, Any]]:
    """本条 clip 的转写分段（成片时间基，已含 0.8s 片头时移）。"""
    return clip_segments_from_render_task(render_task_id, str(record["clip_id"]))


def _cjk_chars(text: str) -> set[str]:
    return {char for char in str(text or "") if "\u4e00" <= char <= "\u9fff"}


def _is_grounded_in_transcript(title: str, transcript_text: str) -> bool:
    """「必须忠于转写」的机器可判定部分：标题的每个汉字都能在该片转写里找到。"""
    chars = _cjk_chars(title)
    if not chars or not transcript_text:
        return False
    return chars <= _cjk_chars(transcript_text)


def _forbidden_hits(title: str, forbidden: Any) -> list[str]:
    """禁用词表命中（清单 title_rules.forbidden）。"""
    if not isinstance(forbidden, (list, tuple)):
        return []
    return [str(term) for term in forbidden if str(term or "").strip() and str(term) in str(title or "")]


def _handle_regenerate_cover(record: dict[str, Any]) -> dict[str, Any]:
    """REGENERATE_COVER：只更新当前 clip 的封面（原地覆盖，不新建 clip/不重渲正片）。

    输入：clip_id、用户修改要求（instruction）、当前标题、当前封面版本、固化封面规则；
    输出：新 ``cover.jpg`` + ``cover_source_frame`` + ``cover_revision+1``。

    规则来源（Phase B）：快照 ``cover_rules``。风格按四层优先级解析
    —— 用户指令（payload.style_id）> 项目设置 > 已启用 Skill（``default_style_id``）> 系统默认，
    Skill 版本经 ``skills.skill_version`` 从快照读取；**渲染与修订落地仍由 cover_replace 唯一承担**
    （备份/revision/幂等/片头拼接），避免同一个 video-brain 封面端点被重复调用两次。
    """
    entry, clip_dir, render_task_id = _clip_paths(record)
    clip_id = str(record["clip_id"])
    payload = record.get("payload") or {}
    ctx, _snapshot = _skill_ctx(
        record, _instruction_from_payload(payload, {"style_id": "cover_rules.default_style_id"}))
    resolved_style, style_source = studio_workflow.resolve_key(
        ctx, "cover_rules", "default_style_id", default=DEFAULT_COVER_STYLE)
    style_id = validate_style_id(payload.get("style_id") or resolved_style)
    intro_s, intro_source = studio_workflow.resolve_key(
        ctx, "cover_rules", "intro_duration_s", default=None)
    rules_source = {
        "cover_rules.default_style_id": style_source,
        "cover_rules.intro_duration_s": intro_source,
    }
    skill_versions = _skill_line(ctx, "cover")
    replace_intro = payload.get("replace_intro")
    replace_intro = True if replace_intro is None else bool(replace_intro)
    override = payload.get("cover_copy") if isinstance(payload.get("cover_copy"), dict) else None

    record["stage"] = "生成新封面（真实帧/风格化）"
    record["progress"] = 0.3
    record["rules_source"] = rules_source
    _save(record)

    video_before = _clip_final_sha(clip_dir)
    result = replace_cover(render_task_id, clip_id, style_id,
                           replace_intro=replace_intro,
                           cover_copy_override=override)
    video_after = _clip_final_sha(clip_dir)

    cover_abs = Path(str(result.get("cover_abs_path") or ""))
    cover_rel = _cover_path(record, render_task_id, clip_id)
    new_sha = _sha256(cover_abs) if cover_abs.is_file() else None
    current_cover = (entry.get("cover") or {}) if isinstance(entry.get("cover"), dict) else {}
    if current_cover.get("sha256") and new_sha and current_cover["sha256"] == new_sha:
        # 幂等：产物与台账当前版本逐字节一致 → **不再叠加修订**、不改动历史。
        # 「重复执行结果一致」必须包括版本号不变，否则 UI 会显示「封面已更新 · v3」
        # 而用户看到的图一模一样。
        card = store.card_view(store.entry_for(str(record["project_id"]), clip_id))
        record["artifacts"] = []
        return _version_fields(record,
            clip_id=clip_id,
            cover_revision=int(current_cover.get("revision") or 1),
            cover_asset_id=current_cover.get("cover_asset_id"),
            style_id=style_id,
            cover_path=current_cover.get("path") or cover_rel,
            cover_sha256=new_sha,
            cover_source_frame=(result.get("source") or {}).get("frame_ts_in_base"),
            intro_replaced=bool(replace_intro),
            copy_report=result.get("copy_report"),
            cover_copy=result.get("cover_copy"),
            qc=result.get("qc"),
            idempotent=True,
            clip_final_unchanged=video_before == video_after,
            can_undo=card["cover"]["can_undo"],
            undo_revision=card["cover"]["undo_revision"],
            rules_source=rules_source,
            skills=skill_versions,
            cover_intro_duration_s=intro_s,
            note="同风格封面已生效且产物逐字节未变，未重复生成/备份，版本号不变",
            card=card,
        )
    revision = int(result.get("cover_revision") or 1)
    copy = result.get("cover_copy") if isinstance(result.get("cover_copy"), dict) else {}
    hook_text = " ".join(part for part in (str(copy.get("headline_line_1") or ""),
                                           str(copy.get("headline_line_2") or "")) if part)
    store.bump_cover(
        str(record["project_id"]), clip_id, task_id=str(record["task_id"]),
        style_id=style_id, cover_path=cover_rel,
        sha256=new_sha,
        bytes_=cover_abs.stat().st_size if cover_abs.is_file() else None,
        intro_burned=bool(replace_intro), kind="generated",
        instruction=record.get("instruction"),
        title=(str(copy.get("top_theme")) or None) if copy.get("top_theme") else None,
        hook=hook_text or None,
    )
    card = store.card_view(store.entry_for(str(record["project_id"]), clip_id))
    record["artifacts"] = [{
        "name": "cover.jpg", "path": cover_rel,
        "size_bytes": cover_abs.stat().st_size if cover_abs.is_file() else 0,
    }]
    return _version_fields(record,
        clip_id=clip_id,
        cover_revision=revision,
        cover_asset_id=card["cover"]["cover_asset_id"],
        style_id=style_id,
        cover_path=cover_rel,
        cover_sha256=card["cover"]["sha256"],
        cover_source_frame=(result.get("source") or {}).get("frame_ts_in_base"),
        intro_replaced=bool(replace_intro),
        copy_report=result.get("copy_report"),
        cover_copy=result.get("cover_copy"),
        qc=result.get("qc"),
        idempotent=result.get("idempotent"),
        clip_final_unchanged=video_before == video_after,
        can_undo=card["cover"]["can_undo"],
        undo_revision=card["cover"]["undo_revision"],
        rules_source=rules_source,
        skills=skill_versions,
        cover_intro_duration_s=intro_s,
        card=card,
    )


def _handle_update_title(record: dict[str, Any]) -> dict[str, Any]:
    """UPDATE_TITLE：只更新标题/主题/关键词/封面文字，**不重剪视频**。

    规则来源（Phase B）：快照 ``title_rules`` —— ``must_be_grounded_in_transcript``（必须忠于转写）
    与 ``forbidden``（禁用词表）。既有实现（只改文字 + 重做封面片头）保持不变，
    只把「规则从哪来」从写死的常量换成**冻结快照**，并把命中/溯源情况如实记进结果与任务历史。
    """
    entry, clip_dir, render_task_id = _clip_paths(record)
    project_id = str(record["project_id"])
    clip_id = str(record["clip_id"])
    payload = record.get("payload") or {}
    ctx, _snapshot = _skill_ctx(record)
    title_rules, title_rules_source = studio_workflow.resolve_section(ctx, "title_rules")
    forbidden = title_rules.get("forbidden")
    must_be_grounded = bool(title_rules.get("must_be_grounded_in_transcript", True))
    transcript_text = "".join(
        str(item.get("text") or "") for item in _clip_transcript_window(record, render_task_id))

    title = str(payload.get("title") or "").strip()
    if not title:
        # 指令里没给新标题就按固化规则从该片转写/已有封面文案里重排一版（≤12 汉字、两行）。
        title = _derive_title(entry, transcript_text, must_be_grounded=must_be_grounded)
    hits = _forbidden_hits(title, forbidden)
    if hits:
        # 禁用词表命中 → 显式拒绝（不静默改写用户的话，也不让违规标题上封面）。
        raise LocalTaskError(
            "E_TITLE_FORBIDDEN_TERM",
            f"标题命中清单 title_rules.forbidden 里的禁用项 {hits}，已拒绝：{title!r}",
        )
    grounded = _is_grounded_in_transcript(title, transcript_text) if must_be_grounded else None
    keywords = payload.get("keywords")
    if not isinstance(keywords, list):
        keywords = entry.get("keywords") or []

    record["stage"] = "更新标题与封面文字"
    record["progress"] = 0.4
    record["rules_source"] = {"title_rules": title_rules_source}
    _save(record)

    store.update_fields(project_id, clip_id, {
        "title": title,
        "topic": str(payload.get("topic") or entry.get("topic") or title),
        "keywords": [str(k) for k in keywords if str(k).strip()],
    })

    cover_revision = None
    copy_report = None
    if payload.get("refresh_cover", True):
        # 封面文字被烧录进 0.8s 片头：换文字必须只重做封面 + 该片头段，绝不重跑自动剪辑。
        head, tail = _two_lines(title)
        override = {"headline_line_1": head, "headline_line_2": tail}
        result = replace_cover(render_task_id, clip_id,
                               str(payload.get("style_id") or DEFAULT_COVER_STYLE),
                               replace_intro=bool(payload.get("replace_intro", True)),
                               cover_copy_override=override)
        cover_abs = Path(str(result.get("cover_abs_path") or ""))
        copy_report = result.get("copy_report")
        new_sha = _sha256(cover_abs) if cover_abs.is_file() else None
        current_cover = (entry.get("cover") or {}) if isinstance(entry.get("cover"), dict) else {}
        if not (current_cover.get("sha256") and new_sha and current_cover["sha256"] == new_sha):
            # 只有封面真的换了才推进修订；标题文字未改变封面文案时不叠加版本号。
            cover_revision = int(result.get("cover_revision") or 1)
            store.bump_cover(
                project_id, clip_id, task_id=str(record["task_id"]),
                style_id=str(payload.get("style_id") or DEFAULT_COVER_STYLE),
                cover_path=_cover_path(record, render_task_id, clip_id),
                sha256=new_sha,
                bytes_=cover_abs.stat().st_size if cover_abs.is_file() else None,
                intro_burned=True, kind="title", instruction=record.get("instruction"),
                title=title,
            )
    card = store.card_view(store.entry_for(project_id, clip_id))
    return _version_fields(record,
        clip_id=clip_id,
        title=title,
        topic=card["topic"],
        keywords=card["keywords"],
        cover_revision=cover_revision,
        copy_report=copy_report,
        video_rerendered=False,
        # 「必须忠于转写」的可判定证据：标题汉字是否都能在该片转写里找到。
        title_grounded_in_transcript=grounded,
        title_forbidden_checked=list(forbidden or []),
        rules_source={"title_rules": title_rules_source},
        skills=_skill_line(ctx, "clip_planning"),
        card=card,
    )


def _handle_update_subtitle_style(record: dict[str, Any]) -> dict[str, Any]:
    """UPDATE_SUBTITLE_STYLE：只更新字幕样式配置；仅为本 clip 建立新 render_revision。

    规则来源（Phase B）：快照 ``subtitle_rules``（``band`` / ``burn_in`` 等）。
    **已知缺口（如实记录，不伪实现）**：字幕专用适配器 ``run_subtitle_composition_skill``
    仍显式抛 ``E_SKILL_NOT_IMPLEMENTED``（video-brain 侧没有可跨服务调用的字幕合成端点，
    字幕在 ``app/subtitles.py`` 的固定流程内生成）。因此本处理器只落样式配置 + 记录该缺口，
    绝不私自生成 .ass/.srt 绕过 ``render_rules.subtitle_policy``。
    """
    entry, clip_dir, render_task_id = _clip_paths(record)
    project_id = str(record["project_id"])
    clip_id = str(record["clip_id"])
    payload = record.get("payload") or {}
    ctx, _snapshot = _skill_ctx(record)
    subtitle_rules, subtitle_rules_source = studio_workflow.resolve_section(ctx, "subtitle_rules")
    config = dict(entry.get("subtitle_style", {}).get("config") or {})
    # 快照里的字幕规则作为**基线**进配置（用户本次指令再覆盖），保证「样式来自清单」。
    for rule_key in ("band", "burn_in"):
        if subtitle_rules.get(rule_key) is not None and rule_key not in config:
            config[rule_key] = subtitle_rules[rule_key]
    config.update(payload.get("config") or {})
    # 「字幕放大一点」→ 解析相对缩放；默认 1.15（"一点"）。
    requested_scale = payload.get("font_scale")
    if requested_scale is None:
        requested_scale = _parse_font_scale(record.get("instruction") or "")
    if requested_scale is not None:
        config["font_scale"] = round(float(requested_scale), 3)
    if not config:
        config["font_scale"] = 1.15

    sidecar = clip_dir / CLIP_SUBTITLE_STYLE_FILENAME
    _atomic_json(sidecar, {
        "clip_id": clip_id,
        "updated_at": store.utc_now(),
        "task_id": record["task_id"],
        "instruction": record.get("instruction"),
        "config": config,
        "rules_source": {"subtitle_rules": subtitle_rules_source},
        "subtitle_rules": subtitle_rules,
    })
    store.update_fields(project_id, clip_id, {
        "subtitle_style": {"updated_at": store.utc_now(), "config": config,
                           "task_id": record["task_id"],
                           "rules_source": {"subtitle_rules": subtitle_rules_source}},
    })
    store.bump_render_revision(project_id, clip_id, task_id=str(record["task_id"]),
                               kind=TASK_UPDATE_SUBTITLE_STYLE,
                               instruction=record.get("instruction"))
    card = store.card_view(store.entry_for(project_id, clip_id))
    return _version_fields(record,
        clip_id=clip_id,
        config=config,
        render_revision=card["render_revision"],
        video_rerendered=False,
        sidecar=str(sidecar),
        rules_source={"subtitle_rules": subtitle_rules_source},
        subtitle_rules=subtitle_rules,
        # 缺口：字幕合成适配器未实现（任务书要求如实写明，不伪实现）。
        subtitle_adapter=SUBTITLE_ADAPTER_GAP,
        card=card,
    )


def _handle_update_timeline(record: dict[str, Any]) -> dict[str, Any]:
    """UPDATE_TIMELINE：只改当前 clip 的时间线片段；不影响原素材、其它 clip、转写。

    规则来源（Phase B）：快照 ``clip_planning_rules`` / ``render_rules``，两条真实链路：
    - **重新选段**（``payload.reselect=true``）→ ``skills.run_clip_planning_skill``
      （规划规则来自快照：时长区间、llm_enabled），返回的切片窗口成为新的时间线区间；
    - **真正重渲该 clip**（``payload.rerender=true``）→ ``skills.run_render_skill``
      把「单切片」渲染任务提交给 video-brain（转写窗口钉在本 clip、时长区间按新区间收窄），
      返回的 video-brain 任务号写进台账。未显式要求重渲时保持既有语义：
      只落配置 + ``pending_rerender=true``（不擅自触发数分钟的渲染）。
    """
    entry, clip_dir, render_task_id = _clip_paths(record)
    project_id = str(record["project_id"])
    clip_id = str(record["clip_id"])
    payload = record.get("payload") or {}
    ctx, _snapshot = _skill_ctx(record, _instruction_from_payload(
        payload, {"llm_enabled": "clip_planning_rules.llm_enabled"}))
    plan_rules, plan_rules_source = studio_workflow.resolve_section(ctx, "clip_planning_rules")
    render_rules, render_rules_source = studio_workflow.resolve_section(ctx, "render_rules")
    rules_source = {"clip_planning_rules": plan_rules_source, "render_rules": render_rules_source}
    planning_skill: dict[str, Any] | None = None
    transcript_window = _clip_transcript_window(record, render_task_id)

    ranges = payload.get("ranges")
    if (not isinstance(ranges, list) or not ranges) and payload.get("reselect"):
        # 「重新选段」：交给规划 Skill（时长/重叠/证据校验都在 video-brain 侧），
        # 再用规划结果的窗口替换本 clip 的时间线区间。拿不到转写就显式失败，不猜。
        if not transcript_window:
            raise LocalTaskError(
                "E_TIMELINE_NO_TRANSCRIPT",
                "重新选段需要该成片的转写分段，但任务产物里没有；请先完成转写/自动剪辑")
        plan = skills.run_clip_planning_skill(
            ctx,
            segments=[
                {"start": float(item.get("start") or 0.0),
                 "end": float(item.get("end") or 0.0),
                 "text": str(item.get("text") or "")}
                for item in transcript_window
            ],
            n_clips=payload.get("n_clips") or 1,
            duration_range=payload.get("duration_range") or plan_rules.get("duration_range_default"),
            llm_enabled=payload.get("llm_enabled"),
            source_duration=float(entry.get("duration") or 0.0),
        )
        planning_skill = plan.as_dict()
        ranges = [
            {"start": float(item.get("start") or 0.0), "end": float(item.get("end") or 0.0)}
            for item in (plan.output.get("clips") or [])
            if isinstance(item, dict)
        ]
    if not isinstance(ranges, list) or not ranges:
        # 没给显式 ranges 就用「删除区间」从当前窗口切出来（示例：删除第 8 秒到第 10 秒）。
        removals = payload.get("remove") or _parse_remove_ranges(record.get("instruction") or "")
        ranges = _subtract_ranges(
            float(entry.get("source_start") or 0.0),
            float(entry.get("source_end") or 0.0),
            removals,
        )
    normalised = _normalise_ranges(ranges, entry)
    if not normalised:
        raise LocalTaskError("E_TIMELINE_EMPTY", "修改后该成片没有任何时间线片段，已拒绝")

    timeline_fields: dict[str, Any] = {
        "updated_at": store.utc_now(), "ranges": normalised,
        "task_id": record["task_id"], "pending_rerender": True,
        "rules_source": rules_source,
    }
    render_skill: dict[str, Any] | None = None
    if payload.get("rerender"):
        # 真正重渲这一条 clip：走 render Skill 适配器（resolution/platform 来自快照 render_rules），
        # 转写窗口与时长区间钉在**新**区间上，等价于执行器的单切片复渲染请求。
        source_video = _render_source_path(render_task_id)
        if source_video is None:
            raise LocalTaskError(
                "E_TIMELINE_SOURCE_MISSING",
                f"找不到任务 {render_task_id} 的源视频，无法重渲该成片")
        lower = min(item["end"] - item["start"] for item in normalised)
        upper = max(item["end"] - item["start"] for item in normalised)
        lo = min(item["start"] for item in normalised) - 0.8
        hi = max(item["end"] for item in normalised) - 0.8
        window = [
            {"start": segment["start"], "end": segment["end"], "text": segment["text"]}
            for segment in transcript_window
            if segment["end"] > lo + 0.02 and segment["start"] < hi - 0.02
        ]
        render = skills.run_render_skill(
            ctx,
            source_video=str(source_video),
            required_clip_count=1,
            duration_range=[round(max(1.05, lower - 0.05), 3), round(max(upper + 0.05, lower + 0.05), 3)],
            source_transcript=window or None,
            idempotency_key=f"studio-{record['task_id']}",
        )
        render_skill = render.as_dict()
        timeline_fields.update({
            "pending_rerender": False,
            "render_submitted": True,
            "video_brain_task_id": render.output.get("task_id"),
            "render_rules": {
                "resolution": render_rules.get("resolution"),
                "platform": render_rules.get("platform"),
            },
        })

    sidecar = clip_dir / CLIP_TIMELINE_FILENAME
    _atomic_json(sidecar, {
        "clip_id": clip_id,
        "updated_at": store.utc_now(),
        "task_id": record["task_id"],
        "instruction": record.get("instruction"),
        "ranges": normalised,
        "rules_source": rules_source,
        "planning_skill": planning_skill,
        "render_skill": render_skill,
    })
    record["rules_source"] = rules_source
    store.update_fields(project_id, clip_id, {"timeline": timeline_fields})
    store.bump_render_revision(project_id, clip_id, task_id=str(record["task_id"]),
                               kind=TASK_UPDATE_TIMELINE,
                               instruction=record.get("instruction"))
    card = store.card_view(store.entry_for(project_id, clip_id))
    return _version_fields(record,
        clip_id=clip_id,
        ranges=normalised,
        ranges_count=len(normalised),
        render_revision=card["render_revision"],
        pending_rerender=bool(timeline_fields["pending_rerender"]),
        video_rerendered=False,
        sidecar=str(sidecar),
        rules_source=rules_source,
        skills=_skill_line(ctx, "clip_planning", "render"),
        planning_skill=planning_skill,
        render_skill=render_skill,
        video_brain_task_id=timeline_fields.get("video_brain_task_id"),
        card=card,
    )


# ---------------------------------------------------------------------------
# Inspector Phase 1（契约 §4）：字幕 / 画面 / 音频 / 效果 / 待渲染
#
# 共同约定：
# * 校验只走 ``clip_config``（默认值、范围、枚举、transform 的唯一事实源）；
# * 只改本 clip 的台账条目与该 clip 的资产目录；
# * 绝不调用 video-brain / 绝不创建 ``autoedit_*`` 任务；
# * 「导出侧真渲染」属 Phase 2 → 结果里固定带 ``export_*: "phase2"`` 标注。
# ---------------------------------------------------------------------------

def effective_transcript_segments(project_id: str, clip_id: str, *,
                                  segments: list[dict[str, Any]] | None = None
                                  ) -> list[dict[str, Any]]:
    """该成片的**有效**转写段（三态里的 ``effective``）。

    没有转写文档（或文档还没有 raw 基线）时用成片自带的 ``segments`` 惰性 seed：
    这样「第一次点开字幕标签就改字幕」不需要先单独调一次建基线接口。
    两个基线都没有时返回空表（调用方据 ``build_cues`` 的空结果显式失败，不猜内容）。
    """
    document = transcript_revisions.load_document(project_id, clip_id)
    if segments and (document is None or not document.get("raw_segments")):
        document = transcript_revisions.ensure_document(
            project_id, clip_id, segments=list(segments))
    if document is None:
        return []
    return transcript_revisions.effective_segments(document, "effective")


def subtitle_overrides_from_cues(cues: Any) -> list[dict[str, Any]]:
    """把「客户端直接给出的最终 cue」转成 §2 的 ``subtitle_overrides``。

    cue 只有落成 overrides（按 ``segment_id`` 键）才能被 ``subtitle_compose.build_cues``
    用同一套规则复现；没有 ``segment_id`` 的 cue 用 ``cue-NNN`` 兜底，``start``/``end``
    齐全时 ``build_cues`` 会把它作为额外 cue 追加（时间轴上的手工字幕）。
    """
    if cues is None:
        return []
    if not isinstance(cues, list):
        raise clip_config.ClipConfigError(clip_config.E_CONFIG_INVALID,
                                          f"cues 必须是数组，收到: {cues!r}")
    stamp = store.utc_now()
    overrides: list[dict[str, Any]] = []
    for index, raw in enumerate(cues, 1):
        if not isinstance(raw, dict):
            raise clip_config.ClipConfigError(
                clip_config.E_CONFIG_INVALID, f"cues[{index}] 必须是 JSON 对象，收到: {raw!r}")
        text = str(raw.get("text") or "").strip()
        if not text:
            continue  # 空文本 cue 不产生覆盖项（合成侧同样跳过空文本）
        entry: dict[str, Any] = {
            "segment_id": str(raw.get("segment_id") or "").strip() or f"cue-{index:03d}",
            "text": text,
            "updated_at": stamp,
        }
        for key in ("start", "end"):
            value = raw.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise clip_config.ClipConfigError(
                    clip_config.E_CONFIG_INVALID,
                    f"cues[{index}].{key} 必须是数字，收到: {value!r}")
            if float(value) < 0:
                raise clip_config.ClipConfigError(
                    clip_config.E_CONFIG_INVALID,
                    f"cues[{index}].{key} 不得小于 0，收到: {value!r}")
            entry[key] = round(float(value), 3)
        overrides.append(entry)
    return overrides


def merge_subtitle_overrides(explicit: Any, from_cues: Any) -> list[dict[str, Any]] | None:
    """合并「显式 overrides」与「cues 转出的 overrides」（按 segment_id 去重，cues 优先）。

    两边都没给 → ``None``（表示「不动现有 overrides」而不是「清空」）。
    """
    if explicit is None and from_cues is None:
        return None
    merged: dict[str, dict[str, Any]] = {}
    for item in (explicit or []):
        if isinstance(item, dict):
            merged[str(item.get("segment_id") or "")] = item
    for item in (from_cues or []):
        if isinstance(item, dict):
            merged[str(item.get("segment_id") or "")] = item
    return list(merged.values())


def _clip_relative(clip_dir: Path, path: Any) -> str | None:
    """把资产绝对路径投影成 clip 目录内的相对路径（前端只用相对路径，不泄露绝对路径）。"""
    try:
        return Path(str(path)).relative_to(Path(clip_dir)).as_posix()
    except (TypeError, ValueError):
        return None


def _task_relative(path: Any) -> str | None:
    """把资产绝对路径投影成任务根内相对路径（与既有 artifacts 的形状一致）。"""
    try:
        return Path(str(path)).relative_to(TASKS_ROOT).as_posix()
    except (TypeError, ValueError):
        return None


def _handle_update_subtitle(record: dict[str, Any]) -> dict[str, Any]:
    """UPDATE_SUBTITLE：字幕文本/时间/样式 → 写 config+overrides 并产出新 ASS/SRT 版本。

    与既有 ``UPDATE_SUBTITLE_STYLE`` 的关键差别：**不再**返回 ``SUBTITLE_ADAPTER_GAP``
    ——工作台侧的字幕合成（``subtitle_compose``）已经实现，这里真的生成
    ``subtitles/rev-NNN/{clip}.ass|.srt``；但「把字幕烧进导出成片」仍是 Phase 2，
    所以结果里固定带 ``export_burn_in="phase2"`` 如实标注，不假装导出已生效。

    输入：``payload = {config?, overrides?, cues?, reason?}``；``cues`` 会被转成
    ``subtitle_overrides``（segment_id 键）再与有效转写段一起重算 cue，保证
    「一次编辑只产生一版字幕」且版本可追溯。
    """
    entry, clip_dir, render_task_id = _clip_paths(record)
    project_id = str(record["project_id"])
    clip_id = str(record["clip_id"])
    payload = record.get("payload") or {}

    current = {
        "subtitle_config": entry.get("subtitle_config") or {},
        "subtitle_overrides": entry.get("subtitle_overrides") or [],
    }
    patch: dict[str, Any] = {}
    if payload.get("config") is not None:
        patch["subtitle_config"] = payload["config"]
    from_cues = (subtitle_overrides_from_cues(payload.get("cues"))
                 if payload.get("cues") is not None else None)
    merged_overrides = merge_subtitle_overrides(payload.get("overrides"), from_cues)
    if merged_overrides is not None:
        patch["subtitle_overrides"] = merged_overrides
    # 校验/深合并在 clip_config（唯一事实源）；没给任何字段时保留现有取值。
    if patch:
        merged, changed = clip_config.apply_patch(current, patch)
    else:
        merged, changed = clip_config.normalize_config(current), []
    config = merged["subtitle_config"]
    overrides = merged["subtitle_overrides"]

    record["stage"] = "合成字幕版本（ASS/SRT）"
    record["progress"] = 0.4
    _save(record)

    segments = effective_transcript_segments(
        project_id, clip_id, segments=clip_segments_from_render_task(render_task_id, clip_id))
    cues = subtitle_compose.build_cues(segments, overrides=overrides)
    # 没有 cue → E_SUBTITLE_NO_CUES（在写任何配置之前失败，台账不留半成品）。
    composed = subtitle_compose.compose(clip_dir, clip_id=clip_id, cues=cues, config=config,
                                        task_id=str(record["task_id"]))

    store.update_fields(project_id, clip_id, {
        "subtitle_config": config, "subtitle_overrides": overrides})
    # 新字幕版本 = 一次真实的成片内容变更 → 本 clip 的 render_revision 恰好 +1。
    store.bump_render_revision(project_id, clip_id, task_id=str(record["task_id"]),
                               kind=TASK_UPDATE_SUBTITLE, instruction=record.get("instruction"))
    card = store.card_view(store.entry_for(project_id, clip_id))
    record["artifacts"] = [
        {"name": Path(composed["ass_path"]).name,
         "path": _task_relative(composed["ass_path"]), "size_bytes": 0},
        {"name": Path(composed["srt_path"]).name,
         "path": _task_relative(composed["srt_path"]), "size_bytes": 0},
    ]
    return _version_fields(record,
        clip_id=clip_id,
        revision=composed["revision"],
        ass_path=composed["ass_path"],
        srt_path=composed["srt_path"],
        ass_rel=_clip_relative(clip_dir, composed["ass_path"]),
        srt_rel=_clip_relative(clip_dir, composed["srt_path"]),
        cue_count=composed["cue_count"],
        config=config,
        overrides=overrides,
        changed=changed,
        render_revision=card["render_revision"],
        video_rerendered=False,
        export_burn_in=EXPORT_PHASE2,
        export_note="字幕版本用于预览与版本追溯；烧录进导出成片属 Phase 2",
        card=card,
    )


def _handle_update_transform(record: dict[str, Any]) -> dict[str, Any]:
    """UPDATE_TRANSFORM：画面变换「应用」→ 写本 clip 的 ``transform`` + 既有 sidecar。

    校验委托 ``clip_config.normalize_patch``（最终落到 ``clip_transform.parse_transform``），
    因此 wire format 与播放器预览/导出完全一致；``transform: null`` 表示**清除**变换。
    导出侧按变换真渲染属 Phase 2（``export_transform="phase2"``），这里只落状态与版本号。
    """
    entry, clip_dir, render_task_id = _clip_paths(record)
    project_id = str(record["project_id"])
    clip_id = str(record["clip_id"])
    payload = record.get("payload") or {}
    raw = payload["transform"] if "transform" in payload else entry.get("transform")
    transform = clip_config.normalize_patch({"transform": raw})["transform"]

    record["stage"] = "应用画面变换"
    record["progress"] = 0.4
    _save(record)

    store.update_fields(project_id, clip_id, {"transform": transform})
    # 既有 sidecar 镜像：Studio 播放器与变换导出都从它读回（无侧车时容错跳过）。
    mirror, mirror_error = "skipped", None
    try:
        from .studio_auto_edit_bridge import save_clip_transform  # 局部 import：避免模块级循环
        save_clip_transform(render_task_id, clip_id, transform)
        mirror = "synced"
    except (FileNotFoundError, OSError, ValueError) as exc:
        mirror_error = str(exc)
    store.bump_render_revision(project_id, clip_id, task_id=str(record["task_id"]),
                               kind=TASK_UPDATE_TRANSFORM, instruction=record.get("instruction"))
    card = store.card_view(store.entry_for(project_id, clip_id))
    return _version_fields(record,
        clip_id=clip_id,
        applied=True,
        transform=transform,
        changed=["transform"],
        mirror=mirror,
        mirror_error=mirror_error,
        render_revision=card["render_revision"],
        video_rerendered=False,
        export_transform=EXPORT_PHASE2,
        note="变换已保存并镜像到成片 sidecar；按变换真渲染导出属 Phase 2",
        card=card,
    )


def _handle_update_audio(record: dict[str, Any]) -> dict[str, Any]:
    """UPDATE_AUDIO：原声音量/静音/BGM 音轨 → 写本 clip 的音频配置与版本号。

    校验走 ``clip_config``（``voice_volume`` 0..1、``muted`` 布尔、
    ``audio_tracks`` 结构与范围）；BGM 文件本体由 HTTP 上传端点落进该 clip 的
    ``audio/`` 目录，这里只负责配置层与 render_revision。混音导出属 Phase 2。
    """
    entry, clip_dir, render_task_id = _clip_paths(record)
    project_id = str(record["project_id"])
    clip_id = str(record["clip_id"])
    payload = record.get("payload") or {}
    current = {
        "voice_volume": entry.get("voice_volume"),
        "muted": entry.get("muted"),
        "audio_tracks": entry.get("audio_tracks") or [],
    }
    patch = {key: payload[key] for key in ("voice_volume", "muted", "audio_tracks")
             if key in payload}
    if patch:
        merged, changed = clip_config.apply_patch(current, patch)
    else:
        merged, changed = clip_config.normalize_config(current), []

    record["stage"] = "更新音频设置"
    record["progress"] = 0.4
    _save(record)

    # 只写用户显式给过的段（与 clip_config「sources: user 只在本层显式写过」一致）。
    if patch:
        store.update_fields(project_id, clip_id, {key: merged[key] for key in patch})
    store.bump_render_revision(project_id, clip_id, task_id=str(record["task_id"]),
                               kind=TASK_UPDATE_AUDIO, instruction=record.get("instruction"))
    card = store.card_view(store.entry_for(project_id, clip_id))
    return _version_fields(record,
        clip_id=clip_id,
        voice_volume=merged["voice_volume"],
        muted=merged["muted"],
        audio_tracks=merged["audio_tracks"],
        changed=changed,
        render_revision=card["render_revision"],
        video_rerendered=False,
        export_mix=EXPORT_PHASE2,
        note="音频设置已保存（BGM 混音/静音在导出侧生效属 Phase 2）",
        card=card,
    )


def _handle_update_effects(record: dict[str, Any]) -> dict[str, Any]:
    """UPDATE_EFFECTS：贴纸/片尾卡/滤镜/转场/导出分辨率 → 写本 clip 的 effects。

    校验走 ``clip_config.normalize_effects``（枚举与范围是唯一事实源）；
    这些效果作用在导出侧，Phase 1 只落配置与版本号（``export_rendering="phase2"``）。
    """
    entry, clip_dir, render_task_id = _clip_paths(record)
    project_id = str(record["project_id"])
    clip_id = str(record["clip_id"])
    payload = record.get("payload") or {}
    current = {"effects": entry.get("effects") or {}}
    patch = {"effects": payload["effects"]} if "effects" in payload else {}
    if patch:
        merged, changed = clip_config.apply_patch(current, patch)
    else:
        merged, changed = clip_config.normalize_config(current), []
    effects = merged["effects"]

    record["stage"] = "更新效果设置"
    record["progress"] = 0.4
    _save(record)

    if patch:
        store.update_fields(project_id, clip_id, {"effects": effects})
    store.bump_render_revision(project_id, clip_id, task_id=str(record["task_id"]),
                               kind=TASK_UPDATE_EFFECTS, instruction=record.get("instruction"))
    card = store.card_view(store.entry_for(project_id, clip_id))
    return _version_fields(record,
        clip_id=clip_id,
        effects=effects,
        changed=changed,
        render_revision=card["render_revision"],
        video_rerendered=False,
        export_rendering=EXPORT_PHASE2,
        note="效果设置已保存；贴纸/片尾/滤镜/转场/非 9:16 在导出侧生效属 Phase 2",
        card=card,
    )


def _handle_render_clip(record: dict[str, Any]) -> dict[str, Any]:
    """RENDER_CLIP：本阶段**只登记待渲染**，绝不真渲染、绝不 bump render_revision。

    做三件（且只有三件）事：
    1. 读一次 ``clip_config.render_config_summary``（每项生效值 + 来源，供 UI 展示
       「点渲染会发生什么」）；
    2. 把这次渲染请求登记到该 clip 条目的 ``render_request``（可追溯，重复提交覆盖）；
    3. 如实返回 ``render_deferred=True`` 与契约固定原因。

    硬约束：不调用 video-brain、不新建 ``autoedit_*`` 任务、不推进 render_revision
    ——「按配置真渲染」属于 Phase 2，登记阶段冒充已渲染会让 UI 与产物不一致。
    """
    entry, clip_dir, render_task_id = _clip_paths(record)
    project_id = str(record["project_id"])
    clip_id = str(record["clip_id"])
    summary = clip_config.render_config_summary(project_id, clip_id)

    pending = {
        "task_id": str(record["task_id"]),
        "render_task_id": render_task_id,
        "requested_at": store.utc_now(),
        "reason": RENDER_DEFERRED_REASON,
        "export_resolution": summary["export"]["resolution"],
    }
    record["stage"] = "登记待渲染（Phase 1 不真渲染）"
    record["progress"] = 0.6
    _save(record)

    store.update_fields(project_id, clip_id, {"render_request": pending})
    card = store.card_view(store.entry_for(project_id, clip_id))
    return _version_fields(record,
        clip_id=clip_id,
        render_deferred=True,
        reason=RENDER_DEFERRED_REASON,
        render_config=summary,
        export=summary["export"],
        pending_render=pending,
        render_revision=card["render_revision"],
        render_revision_bumped=False,
        video_rerendered=False,
        note="已登记待渲染：配置与版本已保存，真渲染需 Phase 2 渲染流程支持",
        card=card,
    )




def _handle_apply_cover(record: dict[str, Any]) -> dict[str, Any]:
    """APPLY_COVER：把待应用的版型封面烧成 0.8s 片头，生成更新后的成片。

    语义（对应「应用」按钮的验收）：
    - **只对已有未应用草稿的切片生效**：没草稿 → ``E_COVER_DRAFT_MISSING`` 显式失败；
    - 片头时长沿用既有片头规则（``cover_replace.apply_cover_duration_s`` = realframe_v2
      的 ``intro.duration_s``，当前 0.8s）；片头（封面）是静音的，正片音频与已烧录字幕
      都从 intro 之后开始，绝不被封面覆盖或提前播放；
    - 渲染 = ``cover_replace.apply_template_cover``（等长重编码感知拼接 + 音轨/时长 QC +
      原子替换）；失败抛错 → 本任务失败、成片保持原样，绝不假报成功；
    - 已应用过同一草稿 → 幂等返回（不重复渲染、不改版本）；
    - 成功后把这份封面登记进台账封面历史（bump_cover，intro_burned=True），
      并返回最新卡片（前端据此原地刷新成片与「已应用」状态）。
    """
    entry, clip_dir, render_task_id = _clip_paths(record)
    project_id = str(record["project_id"])
    clip_id = str(record["clip_id"])
    pending = template_pending_state(render_task_id, clip_id)
    if not pending:
        raise LocalTaskError(
            E_COVER_DRAFT_MISSING,
            "当前切片没有待应用的封面草稿：请先在「封面」里选择版型并制作封面，再点应用")
    revision = int(pending.get("revision") or 0)
    style_id = str(pending.get("style") or "") or None
    intro_s = apply_cover_duration_s()
    cover_file = clip_dir / "cover.jpg"
    if not cover_file.is_file():
        raise LocalTaskError(
            E_COVER_DRAFT_MISSING, "封面草稿文件缺失（cover.jpg 不存在），请重新制作封面")

    if pending.get("applied"):
        card = store.card_view(store.entry_for(project_id, clip_id))
        return _version_fields(record,
            clip_id=clip_id,
            cover_revision=revision,
            cover_asset_id=(card.get("cover") or {}).get("cover_asset_id"),
            style_id=style_id,
            cover_path=_cover_path(record, render_task_id, clip_id),
            applied=True,
            intro_duration_s=intro_s,
            intro_replaced=True,
            idempotent=True,
            clip_final_unchanged=True,
            note="这份封面之前已经应用过，未重复渲染、成片未再次改动",
            card=card,
        )

    record["stage"] = "校验封面草稿与成片"
    record["progress"] = 0.25
    _save(record)
    video_before = _clip_final_sha(clip_dir)

    record["stage"] = f"渲染 {intro_s:g}s 封面片头并拼接正片（长片需要几分钟）"
    record["progress"] = 0.45
    _save(record)
    previous_intro = float(entry.get("cover_intro_s") or 0.0)
    try:
        applied = apply_template_cover(render_task_id, clip_id, previous_intro_s=previous_intro)
    except Exception as exc:
        # 成片回滚由 apply_template_cover/splice 的原子替换保证；这里只如实上报原因。
        raise LocalTaskError(
            getattr(exc, "code", None) or "E_COVER_APPLY_FAILED",
            f"封面应用失败，原成片未改动：{exc}") from exc

    record["stage"] = "校验成片（时长 / 音轨 / 尾部）"
    record["progress"] = 0.85
    _save(record)
    video_after = _clip_final_sha(clip_dir)

    # 台账补齐：新的片头长度与成片时长（重复应用时先裁旧片头，不会越叠越长）。
    qc = (applied or {}).get("qc") or {}
    duration_after = (qc.get("after") or {}).get("duration_s")
    fields_update: dict[str, Any] = {"cover_intro_s": intro_s}
    if isinstance(duration_after, (int, float)):
        fields_update["duration"] = round(float(duration_after), 3)
    store.update_fields(project_id, clip_id, fields_update)

    # 登记台账：这份封面现在就是成片封面（intro_burned=True，进入封面历史，可撤销）。
    cover_sha = _sha256(cover_file) if cover_file.is_file() else None
    store.bump_cover(
        project_id, clip_id, task_id=str(record["task_id"]), style_id=style_id,
        cover_path=_cover_path(record, render_task_id, clip_id),
        sha256=cover_sha, bytes_=cover_file.stat().st_size if cover_file.is_file() else None,
        intro_burned=True, kind="template_apply",
        instruction=record.get("instruction"),
    )
    card = store.card_view(store.entry_for(project_id, clip_id))
    record["artifacts"] = [{
        "name": "clip_final.mp4", "path": f"output/{render_task_id}/{clip_id}/clip_final.mp4",
        "size_bytes": (clip_dir / "clip_final.mp4").stat().st_size if (clip_dir / "clip_final.mp4").is_file() else 0,
    }]
    return _version_fields(record,
        clip_id=clip_id,
        applied=True,
        idempotent=False,
        cover_revision=revision,
        cover_asset_id=(card.get("cover") or {}).get("cover_asset_id"),
        style_id=style_id,
        cover_path=_cover_path(record, render_task_id, clip_id),
        cover_sha256=cover_sha,
        intro_duration_s=intro_s,
        intro_replaced=True,
        qc=(applied or {}).get("qc"),
        clip_final_changed=video_before != video_after,
        note="封面已作为片头加入成片：正片音频与字幕从片头之后开始",
        card=card,
    )


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
    from .studio_clip_locator import clip_dir as locate_clip_dir, clip_asset
    clip_dir = locate_clip_dir(TASKS_ROOT, render_task_id, str(clip_id))
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
            image_path = clip_asset(TASKS_ROOT, render_task_id, clip_id,
                f"cover_revisions/rev-{number:03d}/{rev.get('cover_jpg')}",
                required=False).relative_to(TASKS_ROOT.resolve()).as_posix()
        elif rev.get("baseline_cover_jpg"):
            image_path = clip_asset(TASKS_ROOT, render_task_id, clip_id,
                f"cover_backup/{rev.get('baseline_cover_jpg')}",
                required=False).relative_to(TASKS_ROOT.resolve()).as_posix()
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
    "cover_history", "undo_cover", "activate_cover", "delete_cover", "TASKS_ROOT",
    "clip_segments_from_render_task", "effective_transcript_segments",
    "subtitle_overrides_from_cues", "merge_subtitle_overrides",
]
