"""转写修订层：原始 ASR / 纠错稿不可变，所有人工修改以修订（revision）叠加。

契约：``docs/inspector-phase1-contract.md`` §3.2。

三态（阶段 1 的核心）
--------------------
一条成片的转写有**三个互不覆盖**的视图：

= ``raw``        ``raw_segments``：ASR 原文，seed 之后**永不改写**。
= ``corrected``  ``corrected_segments``：自动纠错稿，同样是不可变的基线。
= ``effective``  在 ``corrected_segments``（没有则 ``raw_segments``）之上，
                  叠加 **approved_revision** 那一条修订里逐段的
                  ``after`` / ``speaker_after``。

``effective`` 只叠加「被批准的那一条」修订，而不是把所有修订累积起来 ——
所以修订里的 ``after`` 是**绝对文本**而非增量，``rollback`` 到 v1 就能让
生效文本回到 v1 的状态，而 ``raw`` / ``corrected`` 两个基线一动不动。

落盘
----
``{STUDIO_TASKS_ROOT}/projects/{project_id}/transcripts/{clip_id}.json``
（原子写：临时文件 + ``os.replace``；刷新/崩溃后读到的永远是完整 JSON）。

与 ``studio_project_store`` 的关系
----------------------------------
本模块复用它的 ``validate_project_id`` / ``validate_clip_id`` 与任务根目录
（延迟 import，避免循环导入），并在 ``add_revision`` / ``rollback`` 后把
``clips.json`` 里该 clip 的 ``transcript_revision`` 同步成被批准的修订号
（即 §3.2 的 mirror）。mirror 是**最佳努力**：

* clip 未登记（``E_CLIP_NOT_REGISTERED``）时**不影响**修订落盘 —— 转写文档
  本身才是事实源，返回值里用 ``mirror="skipped"`` + ``mirror_error`` 可读原因
  如实上报，调用方（HTTP 路由 / 局部任务）可以据此提示用户；
* 调用方若确实需要「mirror 必须成功」，传 ``strict_mirror=True`` 即改为抛出
  （``strict_mirror`` 是本实现追加的**可选**关键字参数，契约里的调用方式
  一字不改，故不影响签名兼容性）。
"""

from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_SPEAKER",
    "DOCUMENT_SUFFIX",
    "E_TRANSCRIPT_CHANGES_INVALID",
    "E_TRANSCRIPT_NOT_SEEDED",
    "E_TRANSCRIPT_REVISION_UNKNOWN",
    "E_TRANSCRIPT_SEED_INVALID",
    "E_TRANSCRIPT_VARIANT_UNKNOWN",
    "PROJECTS_DIRNAME",
    "SCHEMA_VERSION",
    "TASKS_ROOT",
    "TRANSCRIPTS_DIRNAME",
    "VARIANTS",
    "TranscriptRevisionError",
    "add_revision",
    "document_path",
    "document_view",
    "effective_segments",
    "ensure_document",
    "list_revisions",
    "load_document",
    "rollback",
    "seed_document",
]

PROJECTS_DIRNAME = "projects"
TRANSCRIPTS_DIRNAME = "transcripts"
DOCUMENT_SUFFIX = ".json"
SCHEMA_VERSION = 1

#: ``effective_segments`` 接受的三种视图。
VARIANTS = ("raw", "corrected", "effective")
DEFAULT_SPEAKER = "未标注"

# 与 studio_project_store / studio_local_tasks 同源：同一个 STUDIO_TASKS_ROOT。
_ENV_TASKS_ROOT = Path(os.getenv("STUDIO_TASKS_ROOT", "/app/tasks")).resolve()
TASKS_ROOT = _ENV_TASKS_ROOT

# $ raw / corrected 两个基线不可变，取文本时的键优先级（兼容 video-brain 的
# raw_text/corrected_text 与工作台内部统一后的 text）。
_RAW_TEXT_KEYS = ("text", "raw_text", "corrected_text")
_CORRECTED_TEXT_KEYS = ("text", "corrected_text", "raw_text")
_CHANGE_KEYS = ("before", "after", "speaker_before", "speaker_after")


class TranscriptRevisionError(ValueError):
    """带 E_ 码的显式失败（沿用工作台 ValueError → HTTP 4xx 的既有映射）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


E_TRANSCRIPT_NOT_SEEDED = "E_TRANSCRIPT_NOT_SEEDED"
E_TRANSCRIPT_REVISION_UNKNOWN = "E_TRANSCRIPT_REVISION_UNKNOWN"
E_TRANSCRIPT_CHANGES_INVALID = "E_TRANSCRIPT_CHANGES_INVALID"
# 契约只列了上面三个码；下面两个是本实现对「seed 入参非法」「variant 非法」
# 的细分（仍走同类错误 → 路由层同样是 422），避免用 CHANGES_INVALID 去描述
# 与 changes 无关的失败。
E_TRANSCRIPT_SEED_INVALID = "E_TRANSCRIPT_SEED_INVALID"
E_TRANSCRIPT_VARIANT_UNKNOWN = "E_TRANSCRIPT_VARIANT_UNKNOWN"


# ---------------------------------------------------------------------------
# 基础设施：任务根 / 原子写 / 时间戳
# ---------------------------------------------------------------------------

def _store():
    """延迟 import ``studio_project_store``（双模式：包内 / 顶层模块）。"""

    try:  # 包内（Studio 后端）
        from . import studio_project_store as store
    except ImportError:  # pragma: no cover - 执行器以顶层模块方式导入
        import studio_project_store as store  # type: ignore[no-redef]

    return store


def _tasks_root() -> Path:
    """任务根目录。

    默认与 ``studio_project_store.TASKS_ROOT`` 取同一个值（同一事实源），因此
    测试或调用方把任意一边打桩到临时目录都能生效；本模块自己的 ``TASKS_ROOT``
    被显式改过时以本模块为准。
    """

    if TASKS_ROOT != _ENV_TASKS_ROOT:
        return TASKS_ROOT
    try:
        return Path(_store().TASKS_ROOT)
    except Exception:  # pragma: no cover - store 不可用时退回本模块常量
        return TASKS_ROOT


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写 JSON：先写同目录临时文件，再 ``os.replace`` 覆盖目标。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{os.urandom(6).hex()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _validated_ids(project_id: str, clip_id: str) -> tuple[str, str]:
    store = _store()
    return store.validate_project_id(project_id), store.validate_clip_id(clip_id)


def document_path(project_id: str, clip_id: str) -> Path:
    """转写文档路径：``projects/{project_id}/transcripts/{clip_id}.json``。"""

    project_id, clip_id = _validated_ids(project_id, clip_id)
    return (_tasks_root() / PROJECTS_DIRNAME / project_id / TRANSCRIPTS_DIRNAME
            / f"{clip_id}{DOCUMENT_SUFFIX}")


# ---------------------------------------------------------------------------
# 段 / 文本 归一化（seed 与 effective 共用；纯函数）
# ---------------------------------------------------------------------------

def _coerce_time(value: Any) -> float:
    """段时间的宽松解析：非法/负数/NaN → 0.0（历史数据不因一个坏字段整篇读不出）。"""

    if value is None or isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number) or number < 0.0:
        return 0.0
    return round(number, 3)


def _segment_text(raw: dict[str, Any], text_keys: tuple[str, ...]) -> str:
    for key in text_keys:
        value = raw.get(key)
        if value is not None:
            return str(value)
    return ""


def _normalize_seed_segment(
    raw: Any, index: int, text_keys: tuple[str, ...]
) -> dict[str, Any] | None:
    """把一个入参段归一化成存储 schema；不是对象则跳过（返回 ``None``）。"""

    if not isinstance(raw, dict):
        return None
    segment_id = str(raw.get("segment_id") or raw.get("id") or "").strip()
    if not segment_id:
        segment_id = f"seg-{index + 1:03d}"
    return {
        "segment_id": segment_id,
        "start": _coerce_time(raw.get("start")),
        "end": _coerce_time(raw.get("end")),
        "text": _segment_text(raw, text_keys),
        "speaker": str(raw.get("speaker") or DEFAULT_SPEAKER),
    }


def _segment_view(raw: Any) -> dict[str, Any] | None:
    """把存储里的段投影成对外五字段（``segment_id/start/end/text/speaker``）。"""

    if not isinstance(raw, dict):
        return None
    segment_id = str(raw.get("segment_id") or raw.get("id") or "").strip()
    if not segment_id:
        return None
    return {
        "segment_id": segment_id,
        "start": _coerce_time(raw.get("start")),
        "end": _coerce_time(raw.get("end")),
        "text": str(raw.get("text") or ""),
        "speaker": str(raw.get("speaker") or DEFAULT_SPEAKER),
    }


def _segment_views(items: Any) -> list[dict[str, Any]]:
    views = [view for view in (_segment_view(item) for item in (items or [])) if view is not None]
    views.sort(key=lambda item: item["start"])
    return views


def _join_texts(segments: list[dict[str, Any]]) -> str:
    return "\n".join(str(segment.get("text") or "") for segment in segments)


def _repair_document(document: dict[str, Any], project_id: str, clip_id: str) -> dict[str, Any]:
    """读路径容错：补齐缺字段、修掉类型错误，但绝不改写 raw/corrected 的内容。"""

    document["schema_version"] = int(document.get("schema_version") or SCHEMA_VERSION)
    document["project_id"] = project_id
    document["clip_id"] = clip_id
    for key in ("raw_text", "corrected_text"):
        if not isinstance(document.get(key), str):
            document[key] = "" if document.get(key) is None else str(document[key])
    for key in ("raw_segments", "corrected_segments", "revisions"):
        if not isinstance(document.get(key), list):
            document[key] = []
    try:
        document["approved_revision"] = max(0, int(document.get("approved_revision") or 0))
    except (TypeError, ValueError):
        document["approved_revision"] = 0
    if not isinstance(document.get("updated_at"), str):
        document["updated_at"] = ""
    return document


# ---------------------------------------------------------------------------
# 读取 / 落盘
# ---------------------------------------------------------------------------

def load_document(project_id: str, clip_id: str) -> dict[str, Any] | None:
    """读转写文档；不存在或损坏时返回 ``None``（读路径永不抛 JSON 错）。"""

    path = document_path(project_id, clip_id)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    project_id, clip_id = _validated_ids(project_id, clip_id)
    return _repair_document(document, project_id, clip_id)


def _save_document(project_id: str, clip_id: str, document: dict[str, Any]) -> dict[str, Any]:
    path = document_path(project_id, clip_id)
    _atomic_json(path, document)
    return document


# ---------------------------------------------------------------------------
# seed / 幂等
# ---------------------------------------------------------------------------

def seed_document(
    project_id: str,
    clip_id: str,
    *,
    raw_segments: list[dict[str, Any]],
    corrected_segments: list[dict[str, Any]] | None = None,
    raw_text: str | None = None,
    corrected_text: str | None = None,
    author: str = "system",
) -> dict[str, Any]:
    """建立 ``raw`` / ``corrected`` 基线（幂等，**绝不覆盖**已有基线）。

    * 已存在且 ``raw_segments`` 非空 → 原样返回，第二次 seed 传什么都不会写进去；
      ``corrected_segments`` / ``corrected_text`` 同样是「一次定终身」，
      后续修正一律走 :func:`add_revision`。
    * 已存在但 ``raw_segments`` 为空（半成品文档）→ 补齐基线，同时保留已有的
      ``revisions`` / ``approved_revision``。

    ``author`` 仅为签名对称而接收：seed 本身不是一条修订，按 §3.2 的落盘 schema
    不额外写字段（要留作者请用 ``add_revision(author=...)``）。
    """

    if not isinstance(raw_segments, list):
        raise TranscriptRevisionError(
            E_TRANSCRIPT_SEED_INVALID, "raw_segments 必须是列表（每项是一个转写段）")
    if corrected_segments is not None and not isinstance(corrected_segments, list):
        raise TranscriptRevisionError(E_TRANSCRIPT_SEED_INVALID, "corrected_segments 必须是列表")

    project_id, clip_id = _validated_ids(project_id, clip_id)
    existing = load_document(project_id, clip_id)
    if existing is not None and existing.get("raw_segments"):
        return existing

    normalized_raw = [
        segment for segment in (
            _normalize_seed_segment(item, index, _RAW_TEXT_KEYS)
            for index, item in enumerate(raw_segments)
        ) if segment is not None
    ]
    normalized_corrected = [
        segment for segment in (
            _normalize_seed_segment(item, index, _CORRECTED_TEXT_KEYS)
            for index, item in enumerate(corrected_segments or [])
        ) if segment is not None
    ]

    stamp = _utc_now()
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "clip_id": clip_id,
        "raw_text": str(raw_text) if raw_text is not None else _join_texts(normalized_raw),
        "corrected_text": (
            str(corrected_text) if corrected_text is not None else _join_texts(normalized_corrected)
        ),
        "raw_segments": normalized_raw,
        "corrected_segments": normalized_corrected,
        "revisions": list(existing.get("revisions") or []) if existing else [],
        "approved_revision": int(existing.get("approved_revision") or 0) if existing else 0,
        "updated_at": stamp,
    }
    return _save_document(project_id, clip_id, document)


def ensure_document(project_id: str, clip_id: str, *, segments: list[dict[str, Any]]) -> dict[str, Any]:
    """便捷入口：以成片 ``segments`` 作为 ``raw`` 基线 seed（已 seed 则原样返回）。"""

    return seed_document(project_id, clip_id, raw_segments=segments or [])


# ---------------------------------------------------------------------------
# 三态视图（纯函数，无 I/O）
# ---------------------------------------------------------------------------

def _approved_revision_entry(document: dict[str, Any]) -> dict[str, Any] | None:
    """被批准的那条修订；``approved_revision=0``（尚未有任何修订）时返回 ``None``。"""

    try:
        approved = int(document.get("approved_revision") or 0)
    except (TypeError, ValueError):
        return None
    if approved <= 0:
        return None
    for entry in document.get("revisions") or []:
        if not isinstance(entry, dict):
            continue
        try:
            revision = int(entry.get("revision") or 0)
        except (TypeError, ValueError):
            continue
        if revision == approved:
            return entry
    return None


def effective_segments(document: dict[str, Any], variant: str = "effective") -> list[dict[str, Any]]:
    """三态段列表（**纯函数**：不读盘、不改传入的 document）。

    * ``raw``       → ``raw_segments``
    * ``corrected`` → ``corrected_segments``
    * ``effective`` → ``corrected_segments``（为空则 ``raw_segments``）叠加
      ``approved_revision`` 的逐段 ``after`` / ``speaker_after``
    """

    if variant not in VARIANTS:
        raise TranscriptRevisionError(
            E_TRANSCRIPT_VARIANT_UNKNOWN,
            f"variant 只能是 {'/'.join(VARIANTS)}，收到: {variant!r}")
    if not isinstance(document, dict):
        raise TranscriptRevisionError(E_TRANSCRIPT_NOT_SEEDED, "转写文档必须是 JSON 对象")

    raw = _segment_views(document.get("raw_segments"))
    if variant == "raw":
        return raw
    corrected = _segment_views(document.get("corrected_segments"))
    if variant == "corrected":
        return corrected

    base = corrected or raw
    revision = _approved_revision_entry(document)
    if revision is None:
        return base

    patches: dict[str, dict[str, Any]] = {}
    for change in revision.get("changes") or []:
        if not isinstance(change, dict):
            continue
        segment_id = str(change.get("segment_id") or "").strip()
        if segment_id:
            patches[segment_id] = change

    result: list[dict[str, Any]] = []
    for segment in base:
        item = dict(segment)
        change = patches.get(segment["segment_id"])
        if change is not None:
            after = change.get("after")
            if after is not None:
                item["text"] = str(after)
            speaker_after = change.get("speaker_after")
            if speaker_after is not None:
                item["speaker"] = str(speaker_after)
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# 修订追加 / 回滚
# ---------------------------------------------------------------------------

def _normalize_changes(changes: Any, document: dict[str, Any]) -> list[dict[str, Any]]:
    """校验并规范化 changes；任何结构错误都抛 ``E_TRANSCRIPT_CHANGES_INVALID``。"""

    if not isinstance(changes, list) or not changes:
        raise TranscriptRevisionError(
            E_TRANSCRIPT_CHANGES_INVALID, "changes 必须是非空列表（[{segment_id, after...}]）")
    known = {
        segment["segment_id"]
        for segment in (
            _segment_views(document.get("raw_segments")) + _segment_views(document.get("corrected_segments"))
        )
    }
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(changes):
        if not isinstance(raw, dict):
            raise TranscriptRevisionError(
                E_TRANSCRIPT_CHANGES_INVALID, f"changes[{index}] 必须是 JSON 对象")
        segment_id = str(raw.get("segment_id") or "").strip()
        if not segment_id:
            raise TranscriptRevisionError(
                E_TRANSCRIPT_CHANGES_INVALID, f"changes[{index}] 缺少 segment_id")
        if segment_id not in known:
            raise TranscriptRevisionError(
                E_TRANSCRIPT_CHANGES_INVALID,
                f"changes[{index}] 的 segment_id={segment_id} 不在该 clip 的转写段里")
        if raw.get("after") is None and raw.get("speaker_after") is None:
            raise TranscriptRevisionError(
                E_TRANSCRIPT_CHANGES_INVALID,
                f"changes[{index}] 至少需要 after 或 speaker_after 之一")
        entry: dict[str, Any] = {"segment_id": segment_id}
        for key in _CHANGE_KEYS:
            value = raw.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise TranscriptRevisionError(
                    E_TRANSCRIPT_CHANGES_INVALID,
                    f"changes[{index}].{key} 必须是字符串，收到: {type(value).__name__}")
            entry[key] = value
        normalized.append(entry)
    return normalized


def _sync_transcript_revision(
    project_id: str, clip_id: str, revision: int, strict: bool
) -> tuple[str, str | None]:
    """把 ``clips.json`` 里该 clip 的 ``transcript_revision`` 同步成被批准的修订号。

    返回 ``(mirror 状态, 可读错误)``：``"synced"`` / ``"skipped"``。
    clip 未登记（或台账写失败）时默认**不**打断已经落盘的修订，只如实上报；
    ``strict=True``（调用方明确要求 mirror）则原样抛出。
    """

    try:
        store = _store()
        store.update_fields(project_id, clip_id, {"transcript_revision": int(revision)})
        return "synced", None
    except Exception as error:  # noqa: BLE001 - 见 docstring：mirror 失败不吞掉修订
        if strict:
            raise
        return "skipped", f"clip {clip_id} 未登记或台账不可写，transcript_revision 未镜像: {error}"


def add_revision(
    project_id: str,
    clip_id: str,
    *,
    changes: list[dict[str, Any]],
    reason: str | None = None,
    author: str = "editor",
    strict_mirror: bool = False,
) -> dict[str, Any]:
    """追加一条修订（``revision = max+1``），批准它，并把 mirror 同步进台账。

    返回 ``**document`` 加三个瞬时字段：``revision``（新修订号）、
    ``mirror``（``synced`` / ``skipped``）、``mirror_error``（可读原因或 ``None``）。
    这三个字段**不落盘**，文档 JSON 严格保持 §3.2 的 schema。

    未 seed（文档不存在或 ``raw_segments`` 为空）→ ``E_TRANSCRIPT_NOT_SEEDED``。
    """

    project_id, clip_id = _validated_ids(project_id, clip_id)
    document = load_document(project_id, clip_id)
    if document is None or not document.get("raw_segments"):
        raise TranscriptRevisionError(
            E_TRANSCRIPT_NOT_SEEDED,
            f"clip {clip_id} 还没有转写基线（raw_segments 为空），请先 seed_document/ensure_document")

    normalized = _normalize_changes(changes, document)
    existing = document.get("revisions") or []
    revision = max(
        [int(entry.get("revision") or 0) for entry in existing if isinstance(entry, dict)] or [0]
    ) + 1
    stamp = _utc_now()
    document["revisions"] = [*existing, {
        "revision": revision,
        "author": str(author),
        "created_at": stamp,
        "reason": None if reason is None else str(reason),
        "changes": normalized,
    }]
    document["approved_revision"] = revision
    document["updated_at"] = stamp
    _save_document(project_id, clip_id, document)

    mirror, mirror_error = _sync_transcript_revision(project_id, clip_id, revision, strict_mirror)
    return {**document, "revision": revision, "mirror": mirror, "mirror_error": mirror_error}


def rollback(
    project_id: str,
    clip_id: str,
    revision: int,
    *,
    strict_mirror: bool = False,
) -> dict[str, Any]:
    """把 ``approved_revision`` 指回一条**已存在**的修订号（不新增修订）。

    注意：``approved_revision=0`` 只是「还没有任何修订」的初始态，不是一条修订，
    因此不能通过本函数回到 0 —— 契约要求 revision 必须存在，否则
    ``E_TRANSCRIPT_REVISION_UNKNOWN``。返回值形状与 :func:`add_revision` 一致。
    """

    project_id, clip_id = _validated_ids(project_id, clip_id)
    document = load_document(project_id, clip_id)
    if document is None or not document.get("raw_segments"):
        raise TranscriptRevisionError(
            E_TRANSCRIPT_NOT_SEEDED, f"clip {clip_id} 还没有转写基线，无法回滚")

    try:
        target = int(revision)
    except (TypeError, ValueError):
        target = -1
    known = [
        int(entry.get("revision") or 0)
        for entry in document.get("revisions") or []
        if isinstance(entry, dict)
    ]
    if target not in known:
        raise TranscriptRevisionError(
            E_TRANSCRIPT_REVISION_UNKNOWN,
            f"修订 v{revision} 不存在（已有: {sorted(known) or '空'}）")

    document["approved_revision"] = target
    document["updated_at"] = _utc_now()
    _save_document(project_id, clip_id, document)

    mirror, mirror_error = _sync_transcript_revision(project_id, clip_id, target, strict_mirror)
    return {**document, "revision": target, "mirror": mirror, "mirror_error": mirror_error}


def list_revisions(project_id: str, clip_id: str) -> list[dict[str, Any]]:
    """修订列表（按 revision 升序）；未 seed 时返回 ``[]``。"""

    document = load_document(project_id, clip_id)
    if document is None:
        return []
    revisions = [entry for entry in (document.get("revisions") or []) if isinstance(entry, dict)]
    revisions.sort(key=lambda entry: int(entry.get("revision") or 0))
    return [dict(entry) for entry in revisions]


# ---------------------------------------------------------------------------
# 前端视图
# ---------------------------------------------------------------------------

def document_view(
    project_id: str,
    clip_id: str,
    *,
    seed_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """一次给出三态稿 + 修订列表（前端 Inspector「转写」标签的数据源）。

    ``seed_segments`` 给了就先 ``ensure_document``（未 seed 或基线为空时才写入，
    已有基线绝不被覆盖）；两者都没有时抛 ``E_TRANSCRIPT_NOT_SEEDED``。
    ``transcript_revision`` 与 ``approved_revision`` 都取文档里的被批准修订号
    （文档是事实源，台账里的 ``transcript_revision`` 只是它的 mirror）。
    """

    project_id, clip_id = _validated_ids(project_id, clip_id)
    document = load_document(project_id, clip_id)
    if seed_segments is not None and (
        document is None or not document.get("raw_segments")
    ):
        document = seed_document(project_id, clip_id, raw_segments=list(seed_segments))
    if document is None:
        raise TranscriptRevisionError(
            E_TRANSCRIPT_NOT_SEEDED,
            f"clip {clip_id} 还没有转写文档，请先 POST 转写基线或传 seed_segments")

    raw_segments = effective_segments(document, "raw")
    corrected_segments = effective_segments(document, "corrected")
    effective = effective_segments(document, "effective")
    approved = int(document.get("approved_revision") or 0)
    return {
        "project_id": project_id,
        "clip_id": clip_id,
        "transcript_revision": approved,
        "approved_revision": approved,
        "raw_text": str(document.get("raw_text") or ""),
        "corrected_text": str(document.get("corrected_text") or ""),
        "effective_text": _join_texts(effective),
        "revisions": list_revisions(project_id, clip_id),
        "raw_segments": raw_segments,
        "corrected_segments": corrected_segments,
        "effective_segments": effective,
        "updated_at": str(document.get("updated_at") or ""),
    }
