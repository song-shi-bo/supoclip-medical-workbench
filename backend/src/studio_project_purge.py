"""清空**当前项目**的任务数据：删任务记录与产物，保留源片与已完成的转写。

为什么要有这个模块
------------------
工作台刷新后仍看到上次的任务卡片，是因为台账（``projects/{id}/clips.json``）
与任务记录都是**落盘持久**的。想回到干净起点就必须真的删文件，而"真的删文件"
在这个仓库里有两个必须尊重的既有事实：

1. **卡片由任务 JSON 反填**：``studio_project_store.sync_from_tasks`` 读
   ``tasks/{completed,processing,in,failed}/autoedit_*.json`` 回填台账。
   只清 ``clips.json`` 而不删任务 JSON，下一次加载卡片会**原样复活**。
   所以清理必须从任务 JSON 下手，台账只是最后一步。
2. **源片住在任务产物目录里**：``source_video`` 形如
   ``media/autoedit_<id>/source.mp4``，它是项目唯一的输入。删了它这个项目就
   连"重新剪辑"都做不了。因此归属该项目源片的 ``media/`` 目录**受保护**。

安全设计（缺一不可）
--------------------
* **只在终态执行**：``tasks/in`` / ``tasks/processing`` 里有该项目的任务，或状态
  文件是非终态，一律拒绝（``E_PURGE_ACTIVE_TASKS``）——删活动任务会让 worker
  回写失败。
* **卡片基数校验**：调用方必须带 ``expected_card_count``，与台账实际卡片数不符
  返回 ``E_PURGE_CARD_COUNT_MISMATCH``。这是幂等键，也是"页面看到 2 张卡、
  点确认时其实已经出了第 3 张"的防误删闸门。
* **先搬后台账、再物理删（两阶段）**：先把每个目标 ``os.replace`` 到
   ``tasks/.purge-trash/<ts>-<project_id>/``，全部搬完才写台账；任何一步失败就
   **反向搬回**并保持旧台账不动。已存在且被跳过的目标，``os.replace`` 会静默
   覆盖，于是清理是**幂等**的：重复执行不报错、不留半删状态。写台账成功后才
  物理删除回收站内容（``purge=False`` 保留，用于排障）。
* **保留项不碰**：``projects/<id>/`` 台账本体、``transcripts/`` 转写、
  ``tasks/media/<源片任务>/`` 源片、其他项目的一切。

诚实的边界
----------
* 产物是**真删**：写台账成功后物理删除回收站，磁盘空间会释放，因此不可撤销。
  排障期可传 ``purge=False`` 保留回收站内容；物理删除失败时接口仍算成功
  （台账与任务记录已干净），响应里的 ``trash_dir`` 指明残留位置。
* 清理只覆盖**项目名下**的任务。同一个 ``source`` 被多个项目引用时，源片留在
  ``media/`` 里不动；删另一个项目不会影响本项目。
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:  # 包内（Studio 后端）
    from . import studio_project_store as store
except ImportError:  # pragma: no cover - 执行器以顶层模块方式导入
    import studio_project_store as store  # type: ignore[no-redef]

TRASH_DIRNAME = ".purge-trash"

# 这些目录里出现该项目的任务 JSON = 任务还没跑完，绝不能删。
_ACTIVE_TASK_DIRS = ("in", "processing")

# 允许清空的终态。其余（queued/running/downloading/...）一律视为活动任务。
_TERMINAL_STATUS = frozenset({"completed", "failed", "cancelled", "transcript_ready", "rejected"})

# 任务 JSON 可能落在这四个目录里（与 sync_from_tasks 的扫描面保持一致）。
_TASK_RECORD_DIRS = ("completed", "failed", "in", "processing")

E_PURGE_ACTIVE_TASKS = "E_PURGE_ACTIVE_TASKS"
E_PURGE_CARD_COUNT_MISMATCH = "E_PURGE_CARD_COUNT_MISMATCH"
E_PURGE_CONFIRM_REQUIRED = "E_PURGE_CONFIRM_REQUIRED"
E_PURGE_FAILED = "E_PURGE_FAILED"
E_PURGE_RESTORE_FAILED = "E_PURGE_RESTORE_FAILED"


class PurgeError(ValueError):
    """带 ``E_`` 码的清空错误（路由据此映射 HTTP 状态，错误码原样透出）。"""

    def __init__(self, code: str, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


def _tasks_root() -> Path:
    """任务根：**每次调用**读 ``studio_project_store.TASKS_ROOT``。

    测试与部署都通过 monkeypatch 这个属性来换根目录（见 test_studio_project_store），
    这里若在 import 期取一次常量就会绕过打桩。
    """
    return Path(store.TASKS_ROOT)


def _trash_root() -> Path:
    return _tasks_root() / TRASH_DIRNAME


def _task_record_paths(task_id: str) -> list[Path]:
    root = _tasks_root()
    return [root / directory / f"{task_id}.json" for directory in _TASK_RECORD_DIRS]


def _task_side_paths(task_id: str) -> list[Path]:
    """任务除任务 JSON 之外的落盘位置（状态、产物、媒体、局部任务记录与快照）。"""
    root = _tasks_root()
    return [
        root / "status" / f"{task_id}.json",
        root / "output" / task_id,
        root / "media" / task_id,
        root / "localtasks" / f"{task_id}.json",
        root / "localtasks" / task_id,
    ]


def _is_non_terminal_status(path: Path) -> bool:
    """状态文件存在且不是终态 → 活动任务。读不出来时按**活动**处理（保守）。"""
    if not path.is_file():
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(document, dict):
        return True
    status = str(document.get("status") or "").strip().lower()
    if not status:
        return True
    return status not in _TERMINAL_STATUS


def collect_task_ids(project_id: str) -> list[str]:
    """该项目名下**所有**任务 id（台账引用 + 任务 JSON 里 ``bridge.project_id`` 命中的）。"""
    project_id = store.validate_project_id(project_id)
    manifest = store.load_manifest(project_id)
    ordered: list[str] = []
    seen: set[str] = set()
    for entry in manifest["clips"].values():
        if not isinstance(entry, dict):
            continue
        for task_id in store.card_task_ids(entry):
            if task_id not in seen:
                seen.add(task_id)
                ordered.append(task_id)
    # 台账没引用但确实属于本项目的任务（例如刚跑完还没回填、或卡片已被手工清掉）。
    for directory in _TASK_RECORD_DIRS:
        base = _tasks_root() / directory
        if not base.is_dir():
            continue
        for path in sorted(base.glob("autoedit_*.json")):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(document, dict):
                continue
            if str((document.get("bridge") or {}).get("project_id") or "") != project_id:
                continue
            task_id = str(document.get("task_id") or path.stem)
            if task_id not in seen:
                seen.add(task_id)
                ordered.append(task_id)
    return ordered


def blocking_tasks(project_id: str, task_ids: list[str]) -> list[str]:
    """仍未结束的任务 id（脏数据里可能是别的项目，也照拦不误）。"""
    active: list[str] = []
    for task_id in task_ids:
        if any(path.is_file() for path in
               (_tasks_root() / directory / f"{task_id}.json" for directory in _ACTIVE_TASK_DIRS)):
            active.append(task_id)
            continue
        if _is_non_terminal_status(_tasks_root() / "status" / f"{task_id}.json"):
            active.append(task_id)
    return active


def source_media_dirs(project_id: str) -> list[str]:
    """``media/<task_id>/`` 中**必须保留**的目录名（源片所在处）。

    两个来源合并，任一命中即保护 —— 只认台账 ``source_video`` 是不够的：
    存量项目里源片经常只写在任务 JSON 的 ``raw_video_path``，台账那条登记是
    ``sync_from_tasks`` 之后才补的。少保护一个目录 = 这个项目失去唯一输入。
    """
    project_id = store.validate_project_id(project_id)
    manifest = store.load_manifest(project_id)
    protected: list[str] = []

    def _remember(raw: str) -> None:
        parts = Path(raw).parts
        if len(parts) >= 2 and parts[0] == "media" and parts[1] not in protected:
            protected.append(parts[1])

    source = manifest.get("source_video")
    if isinstance(source, dict):
        _remember(str(source.get("path") or "").strip())
    # 兜底：扫本项目任务 JSON 的 raw_video_path（不依赖台账是否已回填）。
    for task_id in collect_task_ids(project_id):
        for path in _task_record_paths(task_id):
            if not path.is_file():
                continue
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(document, dict):
                continue
            if str((document.get("bridge") or {}).get("project_id") or "") != project_id:
                continue
            _remember(str(document.get("raw_video_path") or "").strip())
            break
    return protected


def _directory_size(path: Path) -> int:
    """目录占用字节数（用于"将释放多少"，失败时按 0 处理）。"""
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _directory_size_for(paths: list[Path]) -> int:
    """一组已存在的目标路径合计占用（文件计自身大小，目录递归求和）。"""
    total = 0
    for path in paths:
        total += path.stat().st_size if path.is_file() else _directory_size(path)
    return total


def summarize(project_id: str) -> dict[str, Any]:
    """清空前的只读预览（UI 二次确认要展示"将删什么、保留什么"）。"""
    project_id = store.validate_project_id(project_id)
    clip_ids = store.card_clip_ids(project_id)
    task_ids = collect_task_ids(project_id)
    protected = source_media_dirs(project_id)
    root = _tasks_root()
    target_paths: list[Path] = []
    for task_id in task_ids:
        target_paths.extend(_task_record_paths(task_id))
        target_paths.extend(_task_side_paths(task_id))
    existing = [path for path in target_paths if path.exists()]
    transcript_dir = root / "transcripts"
    manifest = store.load_manifest(project_id)
    source = manifest.get("source_video")
    return {
        "project_id": project_id,
        "card_count": len(clip_ids),
        "clip_ids": clip_ids,
        "task_ids": task_ids,
        "applied_task_count": len(task_ids),
        "artifact_count": len(existing),
        "reclaimable_bytes": _directory_size_for(existing),
        "active_task_ids": blocking_tasks(project_id, task_ids),
        "keep": {
            "ledger": str(store.manifest_path(project_id)),
            "source_video": (source or {}).get("path") if isinstance(source, dict) else None,
            "protected_media_dirs": protected,
            "transcripts_present": transcript_dir.is_dir(),
        },
    }


def _slash(paths: list[Path], root: Path) -> list[str]:
    out: list[str] = []
    for path in paths:
        try:
            out.append(path.relative_to(root).as_posix())
        except ValueError:
            out.append(str(path))
    return out


def _move(src: Path, dst: Path) -> None:
    """把 ``src`` 搬进回收站；``src`` 不存在则跳过，``dst`` 已存在则覆盖。

    ``os.replace`` 对目录要求目标不存在或为空目录 —— 回收站路径带时间戳，正常
    情况下不会撞；撞上（同一秒重复清理）时先删掉旧的空壳再搬。
    """
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if dst.is_dir() and not any(dst.iterdir()):
            dst.rmdir()
        elif dst.is_file():
            dst.unlink()
        else:
            shutil.rmtree(dst, ignore_errors=True)
    os.replace(src, dst)


def _prune_task_log(task_ids: set[str]) -> None:
    """从 ``logs/local_tasks.jsonl`` 里剔除这些任务的日志行（全删则删文件）。"""
    log_path = _tasks_root() / "logs" / "local_tasks.jsonl"
    if not log_path.is_file():
        return
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    kept: list[str] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            document = json.loads(text)
        except ValueError:
            kept.append(line)
            continue
        task_id = str((document or {}).get("task_id") or "") if isinstance(document, dict) else ""
        if task_id and task_id in task_ids:
            continue
        kept.append(line)
    if kept:
        log_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    else:
        log_path.unlink(missing_ok=True)


def clear_project_tasks(project_id: str, *, expected_card_count: int,
                        confirm: bool = False, purge: bool = True) -> dict[str, Any]:
    """清空该项目的任务记录与产物，保留源片与转写，并把台账卡片清空。

    ``purge=True``（默认）：写台账成功后物理删除回收站内容 —— 磁盘真正释放。
    ``purge=False``：保留回收站内容（排障用），此时可人工搬回。

    返回删除统计；任何失败都在内部完成还原，且**不写台账**（旧卡片原样可用）。
    """
    project_id = store.validate_project_id(project_id)
    if not confirm:
        raise PurgeError(E_PURGE_CONFIRM_REQUIRED,
                         "清空项目任务数据需要显式确认（confirm=true）")

    root = _tasks_root()
    clip_ids = store.card_clip_ids(project_id)
    if int(expected_card_count) != len(clip_ids):
        raise PurgeError(
            E_PURGE_CARD_COUNT_MISMATCH,
            f"项目卡片数已变化（当前 {len(clip_ids)} 张，请求基于 {int(expected_card_count)} 张）"
            "，为避免误删请刷新后重试",
            detail={"current_card_count": len(clip_ids),
                    "expected_card_count": int(expected_card_count)},
        )

    task_ids = collect_task_ids(project_id)
    active = blocking_tasks(project_id, task_ids)
    if active:
        raise PurgeError(
            E_PURGE_ACTIVE_TASKS,
            "该项目还有未完成的任务，请等它们结束再清空（避免 worker 回写失败）",
            detail={"active_task_ids": active},
        )

    protected_media = set(source_media_dirs(project_id))
    targets: list[Path] = []
    for task_id in task_ids:
        targets.extend(_task_record_paths(task_id))
        targets.extend(_task_side_paths(task_id))
    # 源片的 media 目录受保护：它是项目唯一的输入，不在清空范围内。
    targets = [path for path in targets
               if not (path.parent.name == "media" and path.name in protected_media)]

    trash = _trash_root() / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{project_id}"
    moved: list[tuple[Path, Path]] = []
    try:
        for index, path in enumerate(targets):
            if not path.exists():
                continue
            destination = trash / f"{index:03d}-{path.name}"
            _move(path, destination)
            moved.append((path, destination))
        _prune_task_log(set(task_ids))
        manifest = store.load_manifest(project_id)
        manifest["clips"] = {}
        manifest["clip_order"] = []
        store.save_manifest(project_id, manifest)
    except Exception as exc:  # noqa: BLE001 — 任何失败都要还原，绝不留下半删状态
        failed: list[str] = []
        for original, destination in reversed(moved):
            try:
                _move(destination, original)
            except OSError:
                failed.append(str(original))
        if failed:
            raise PurgeError(
                E_PURGE_RESTORE_FAILED,
                "清空失败且部分文件未能还原，请人工检查回收站与以下路径",
                detail={"unrestored": failed, "trash": str(trash), "cause": str(exc)},
            ) from exc
        raise PurgeError(E_PURGE_FAILED,
                         f"清空失败，已还原全部改动：{exc}",
                         detail={"trash": str(trash)}) from exc

    source_video = store.load_manifest(project_id).get("source_video")
    # 台账已落盘干净 → 才允许物理删除。到这里为止任何失败都还能还原；从这里起不可撤销。
    freed_bytes = 0
    trash_cleaned = False
    if purge:
        freed_bytes = _directory_size(trash)
        try:
            shutil.rmtree(trash)
            # 清掉空的回收站根目录，别在任务根里留下无意义的 .purge-trash 壳。
            parent = trash.parent
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
            trash_cleaned = True
        except OSError:
            trash_cleaned = False
    return {
        "project_id": project_id,
        "cleared_card_count": len(clip_ids),
        "cleared_clip_ids": clip_ids,
        "cleared_task_count": len(task_ids),
        "cleared_task_ids": task_ids,
        "moved_path_count": len(moved),
        "moved_paths": _slash([origin for origin, _ in moved], root),
        "kept_source_video": (source_video or {}).get("path") if isinstance(source_video, dict) else None,
        "kept_media_dirs": sorted(protected_media),
        "trash_dir": str(trash),
        "recoverable": not trash_cleaned,
        "freed_bytes": freed_bytes,
        "note": ("产物已物理删除，磁盘已释放；源片与转写保持不动。" if trash_cleaned
                 else f"台账与任务记录已清空，但回收站未能物理删除（{trash}），磁盘未释放。"),
    }


def _directory_size(path: Path) -> int:
    """回收站占用字节数（仅用于响应里的"释放了多少"，失败时按 0 处理）。"""
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total
