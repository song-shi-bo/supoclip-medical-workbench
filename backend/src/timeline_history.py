"""时间线编辑历史：真实可撤销/重做的版本栈（Phase A 后端地基）。

与 :mod:`src.timeline_view` 的分工
----------------------------------
* :mod:`src.timeline_view`（``timelineView``）＝纯界面设置，**不进历史、不进渲染**。
* 本模块（``timelineRevision``）＝真实编辑数据（音频素材、起止、轨道、音量、淡入
  淡出等），**可撤销/可重做**，是渲染/导出真正读的那份数据的权威版本。

落盘
----
``<project_dir>/timeline/history.json``，原子写（临时文件 + ``os.replace``），可回读::

    {
      "version": 1,
      "limit": 200,
      "index": 2,                     # 当前版本在 revisions 里的下标
      "revisions": [
        {"revision": 1, "updated_at": "…", "label": "初始版本",
         "content_hash": "…", "ranges": […], "audio_tracks": […], "subtitles": […]},
        …
      ]
    }

容量策略（写死的 capability 上限）
----------------------------------
最多保留 :data:`HISTORY_LIMIT` = **200** 条 revision。第 201 条 push 时**丢弃最旧**
一条并把 ``index`` 左移，于是可撤销深度稳定在 **199 步**；``revision`` 号**单调递增
不回收**（新版本号 = 上一条 + 1），所以历史里出现过的号不会被复用，前端缓存不会串。

幂等
----
``content_hash`` = 编辑内容（ranges / audio_tracks / subtitles 及其它扩展键，**不含**
时间戳与操作标签）的规范化 JSON 的 sha256。与当前版本内容相同的 push **不产生新版本**，
也不清空 redo 分支（因为没有任何编辑发生）。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

__all__ = [
    "E_TIMELINE_HISTORY_INVALID",
    "E_TIMELINE_REDO_UNAVAILABLE",
    "E_TIMELINE_REVISION_INVALID",
    "E_TIMELINE_UNDO_UNAVAILABLE",
    "HISTORY_FILENAME",
    "HISTORY_LIMIT",
    "HISTORY_SCHEMA_VERSION",
    "TIMELINE_DIRNAME",
    "TimelineHistoryError",
    "TimelineRevision",
    "can_redo",
    "can_undo",
    "clear_history",
    "content_hash",
    "current_revision",
    "current_revision_number",
    "history_path",
    "history_summary",
    "load_history",
    "push",
    "redo",
    "revision_payload",
    "save_history",
    "undo",
]

TIMELINE_DIRNAME = "timeline"
HISTORY_FILENAME = "history.json"
HISTORY_SCHEMA_VERSION = 1
#: capability 上限：最多保留 200 条 revision（可撤销深度 199 步）。
HISTORY_LIMIT = 200

E_TIMELINE_HISTORY_INVALID = "E_TIMELINE_HISTORY_INVALID"
E_TIMELINE_REVISION_INVALID = "E_TIMELINE_REVISION_INVALID"
E_TIMELINE_UNDO_UNAVAILABLE = "E_TIMELINE_UNDO_UNAVAILABLE"
E_TIMELINE_REDO_UNAVAILABLE = "E_TIMELINE_REDO_UNAVAILABLE"

#: 与版本号/时间戳无关、只描述「编辑内容」的键；内容哈希只看这些。
_META_KEYS = ("revision", "updated_at", "label", "content_hash")

DEFAULT_LABEL = "编辑时间线"


class TimelineHistoryError(ValueError):
    """带机器码的显式失败（沿用工作台 ``ValueError`` → HTTP 4xx 的既有映射）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class TimelineRevision(BaseModel):
    """一条时间线版本（``timelineRevision``）。

    字段表
    ------
    ================  ========  ==================================================
    revision          int ≥ 1   单调递增的版本号（历史内部赋值，忽略调用方传入值）
    updated_at        str       ISO-8601 UTC（``…Z``）
    label             str       操作标签（如「添加背景音乐」），不参与内容哈希
    content_hash      str       编辑内容哈希，用于幂等判断
    ranges            list      视频片段区间（沿用 EDL ranges：source/record/track…）
    audio_tracks      list      音轨（结构见 :mod:`src.timeline_audio`）
    subtitles         list|None 字幕块；``None`` = 该项目本阶段没有字幕轨
    ================  ========  ==================================================

    ``extra="allow"``：Phase B 可能追加 ``visual_tracks`` 等新键；扩展键原样保留
    并**参与内容哈希**，所以不会出现「改了扩展键但历史认为没变」。
    """

    model_config = ConfigDict(extra="allow")

    revision: int = Field(ge=1)
    updated_at: str = ""
    label: str = DEFAULT_LABEL
    content_hash: str = ""
    ranges: list[dict[str, Any]] = Field(default_factory=list)
    audio_tracks: list[dict[str, Any]] = Field(default_factory=list)
    subtitles: list[dict[str, Any]] | None = None

    @field_validator("revision", mode="before")
    @classmethod
    def _validate_revision(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"revision 必须是整数，收到 {type(value).__name__}")
        number = float(value)
        if not number.is_integer() or number < 1:
            raise ValueError(f"revision 必须是 ≥1 的整数，收到 {value!r}")
        return int(number)

    @field_validator("ranges", "audio_tracks", mode="before")
    @classmethod
    def _validate_list(cls, value: Any, info: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(f"{info.field_name} 必须是数组，收到 {type(value).__name__}")
        for item in value:
            if not isinstance(item, dict):
                raise ValueError(
                    f"{info.field_name} 的元素必须是 JSON 对象，收到 {type(item).__name__}")
        return list(value)

    @field_validator("subtitles", mode="before")
    @classmethod
    def _validate_subtitles(cls, value: Any) -> list[dict[str, Any]] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            raise ValueError(f"subtitles 必须是数组或 null，收到 {type(value).__name__}")
        for item in value:
            if not isinstance(item, dict):
                raise ValueError(
                    f"subtitles 的元素必须是 JSON 对象，收到 {type(item).__name__}")
        return list(value)


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error(code: str, detail: str) -> TimelineHistoryError:
    return TimelineHistoryError(code, f"{code}: {detail}")


# ---------------------------------------------------------------------------
# 内容哈希（幂等 / redo 分支判定）
# ---------------------------------------------------------------------------

def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def revision_payload(revision: TimelineRevision | dict[str, Any]) -> dict[str, Any]:
    """取出一条版本的**编辑内容**（剔除 revision/updated_at/label/content_hash）。

    undo/redo 返回的 payload 用法：直接写回渲染侧的数据源，或作为下一次 push 的输入。
    """

    data = _as_dict(revision, "revision")
    return {key: value for key, value in data.items() if key not in _META_KEYS}


def content_hash(revision: TimelineRevision | dict[str, Any]) -> str:
    """编辑内容的 sha256（不含时间戳与操作标签）。"""

    return hashlib.sha256(_canonical(revision_payload(revision)).encode("utf-8")).hexdigest()


def _as_dict(value: Any, name: str) -> dict[str, Any]:
    if isinstance(value, TimelineRevision):
        return value.model_dump(mode="json")
    if hasattr(value, "model_dump") and not isinstance(value, dict):
        return value.model_dump(mode="json")
    if not isinstance(value, dict):
        raise _error(E_TIMELINE_REVISION_INVALID,
                     f"{name} 必须是 JSON 对象，收到 {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

def history_path(project_dir: str | os.PathLike[str]) -> Path:
    """``<project_dir>/timeline/history.json``。"""

    return Path(project_dir) / TIMELINE_DIRNAME / HISTORY_FILENAME


def _empty_history() -> dict[str, Any]:
    return {
        "version": HISTORY_SCHEMA_VERSION,
        "kind": "timeline_history",
        "limit": HISTORY_LIMIT,
        "index": -1,
        "revisions": [],
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.urandom(6).hex()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _validate_revision_entry(raw: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _error(E_TIMELINE_HISTORY_INVALID,
                     f"{where} 必须是 JSON 对象，收到 {type(raw).__name__}")
    try:
        return TimelineRevision.model_validate(raw).model_dump(mode="json")
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['msg']}"
            for item in error.errors()
        )
        raise _error(E_TIMELINE_HISTORY_INVALID, f"{where} 非法: {details}") from error


def load_history(project_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """读取历史文档；文件缺失 → 空历史；损坏/越界指针 → ``E_TIMELINE_HISTORY_INVALID``。"""

    path = history_path(project_dir)
    if not path.is_file():
        return _empty_history()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise _error(E_TIMELINE_HISTORY_INVALID,
                     f"历史文件不可读或不是合法 JSON: {error}") from error
    if not isinstance(raw, dict):
        raise _error(E_TIMELINE_HISTORY_INVALID,
                     f"历史文件顶层必须是 JSON 对象，收到 {type(raw).__name__}")

    revisions_raw = raw.get("revisions")
    if revisions_raw is None:
        revisions_raw = []
    if not isinstance(revisions_raw, list):
        raise _error(E_TIMELINE_HISTORY_INVALID, "revisions 必须是数组")
    revisions = [
        _validate_revision_entry(item, where=f"revisions[{index}]")
        for index, item in enumerate(revisions_raw)
    ]

    limit_raw = raw.get("limit", HISTORY_LIMIT)
    try:
        limit = max(1, int(limit_raw))
    except (TypeError, ValueError):
        limit = HISTORY_LIMIT
    limit = min(limit, HISTORY_LIMIT)

    index_raw = raw.get("index", len(revisions) - 1)
    try:
        index = int(index_raw)
    except (TypeError, ValueError):
        index = len(revisions) - 1
    if revisions and not (0 <= index < len(revisions)):
        raise _error(E_TIMELINE_HISTORY_INVALID,
                     f"index 越界: {index} 不在 0..{len(revisions) - 1}")
    if not revisions:
        index = -1

    return {
        "version": HISTORY_SCHEMA_VERSION,
        "kind": "timeline_history",
        "limit": limit,
        "index": index,
        "revisions": revisions,
    }


def save_history(project_dir: str | os.PathLike[str], history: dict[str, Any]) -> dict[str, Any]:
    """原子写回历史文档（调用方保证结构合法，写入前统一 re-read 校验不了）。"""

    if not isinstance(history, dict):
        raise _error(E_TIMELINE_HISTORY_INVALID,
                     f"history 必须是 JSON 对象，收到 {type(history).__name__}")
    payload = {
        "version": HISTORY_SCHEMA_VERSION,
        "kind": "timeline_history",
        "limit": int(history.get("limit", HISTORY_LIMIT)),
        "index": int(history.get("index", -1)),
        "revisions": history.get("revisions", []),
    }
    _atomic_json(history_path(project_dir), payload)
    return payload


# ---------------------------------------------------------------------------
# 读写语义
# ---------------------------------------------------------------------------

def _current_entry(history: dict[str, Any]) -> dict[str, Any] | None:
    index = history["index"]
    revisions = history["revisions"]
    if not revisions or index < 0:
        return None
    return revisions[index]


def push(
    project_dir: str | os.PathLike[str],
    revision_payload_in: dict[str, Any] | TimelineRevision,
    *,
    label: str = DEFAULT_LABEL,
) -> TimelineRevision:
    """追加一条版本（提交一次时间线编辑），并**清空 redo 分支**。

    * 与当前版本内容相同（``content_hash`` 相等）→ 幂等：不新增、不改指针、**不清
      redo 分支**，返回当前版本。
    * 超过 :data:`HISTORY_LIMIT` → 丢最旧、``index`` 左移，revision 号继续单调递增。
    * 非法 payload → ``E_TIMELINE_REVISION_INVALID``（不落盘）。
    """

    data = _as_dict(revision_payload_in, "revision_payload")
    try:
        candidate = TimelineRevision.model_validate(
            {**data, "revision": 1, "updated_at": data.get("updated_at") or _utc_now(),
             "label": str(label)}
        )
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['msg']}"
            for item in error.errors()
        )
        raise _error(E_TIMELINE_REVISION_INVALID, f"时间线版本非法: {details}") from error

    digest = content_hash(candidate)
    history = load_history(project_dir)
    current = _current_entry(history)
    if current is not None and current.get("content_hash") == digest:
        return TimelineRevision.model_validate(current)

    revisions = history["revisions"][: history["index"] + 1] if history["revisions"] else []
    next_number = (revisions[-1]["revision"] + 1) if revisions else 1
    entry = candidate.model_dump(mode="json")
    entry["revision"] = next_number
    entry["label"] = str(label)
    entry["content_hash"] = digest
    revisions.append(entry)

    index = len(revisions) - 1
    limit = history["limit"]
    while len(revisions) > limit:
        revisions.pop(0)
        index -= 1
    history["revisions"] = revisions
    history["index"] = max(0, index)
    save_history(project_dir, history)
    return TimelineRevision.model_validate(revisions[history["index"]])


def undo(project_dir: str | os.PathLike[str]) -> TimelineRevision:
    """指针左移一步并返回目标版本；无可撤销 → ``E_TIMELINE_UNDO_UNAVAILABLE``。"""

    history = load_history(project_dir)
    if not can_undo_in(history):
        raise _error(E_TIMELINE_UNDO_UNAVAILABLE, "没有可撤销的时间线操作")
    history["index"] -= 1
    save_history(project_dir, history)
    return TimelineRevision.model_validate(history["revisions"][history["index"]])


def redo(project_dir: str | os.PathLike[str]) -> TimelineRevision:
    """指针右移一步并返回目标版本；无可重做 → ``E_TIMELINE_REDO_UNAVAILABLE``。"""

    history = load_history(project_dir)
    if not can_redo_in(history):
        raise _error(E_TIMELINE_REDO_UNAVAILABLE, "没有可重做的时间线操作")
    history["index"] += 1
    save_history(project_dir, history)
    return TimelineRevision.model_validate(history["revisions"][history["index"]])


def can_undo_in(history: dict[str, Any]) -> bool:
    """已加载文档上的 ``can_undo``（省一次磁盘读，给 Phase B 批量用）。"""

    return bool(history["revisions"]) and history["index"] > 0


def can_redo_in(history: dict[str, Any]) -> bool:
    """已加载文档上的 ``can_redo``。"""

    return bool(history["revisions"]) and history["index"] < len(history["revisions"]) - 1


def can_undo(project_dir: str | os.PathLike[str]) -> bool:
    """是否可撤销（供按钮置灰；不可撤销时按钮禁用 + tooltip）。"""

    return can_undo_in(load_history(project_dir))


def can_redo(project_dir: str | os.PathLike[str]) -> bool:
    """是否可重做。"""

    return can_redo_in(load_history(project_dir))


def current_revision(project_dir: str | os.PathLike[str]) -> TimelineRevision | None:
    """当前版本；空历史 → ``None``（前端据此把撤销/重做都置灰）。"""

    entry = _current_entry(load_history(project_dir))
    return None if entry is None else TimelineRevision.model_validate(entry)


def current_revision_number(project_dir: str | os.PathLike[str]) -> int:
    """当前版本号；空历史 → 0。"""

    entry = _current_entry(load_history(project_dir))
    return 0 if entry is None else int(entry["revision"])


def history_summary(project_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """撤销/重做按钮需要的全部状态（一次读盘，不改盘）。"""

    history = load_history(project_dir)
    entry = _current_entry(history)
    revisions = history["revisions"]
    return {
        "version": HISTORY_SCHEMA_VERSION,
        "limit": history["limit"],
        "total": len(revisions),
        "index": history["index"],
        "current_revision": None if entry is None else int(entry["revision"]),
        "current_label": None if entry is None else str(entry.get("label", "")),
        "undo_depth": max(0, history["index"]),
        "redo_depth": max(0, len(revisions) - 1 - history["index"]),
        "can_undo": can_undo_in(history),
        "can_redo": can_redo_in(history),
        "labels": [str(item.get("label", "")) for item in revisions],
    }


def clear_history(project_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """清空历史（空文档落盘）；用于项目重建/测试。"""

    history = _empty_history()
    save_history(project_dir, history)
    return history
