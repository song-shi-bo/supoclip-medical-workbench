"""重剪替换：稳定clip身份与run实例分离。
准备清单固定项目generation；本人视频发布验证后，一次原子台账提交切换活跃实例。
恢复以台账receipt为真相，仅继续清理；精确旧实例与路径逐步持久搬入trash，
失败保留新片和可恢复位置，按唯一对象与实际最后unlink统计释放。
源片、转写和当前引用保持保护，绝不按stable clip_id删除新卡片。
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import studio_project_purge as purge
from . import studio_project_store as store
from .clip_render import _ffprobe_playable

REEDIT_MANIFEST = "purge_manifest.json"
ACTIVATED_MARKER = "ACTIVATED"
PURGE_REPORT = "reedit_report.json"

# 清理只允许落在这些白名单根下（相对 STUDIO_TASKS_ROOT；绝不包含 media/transcripts）
_ALLOWED_TARGET_ROOTS = ("output", "completed", "failed", "status", "localtasks", "logs")
_TASK_RECORD_DIRS = ("completed", "failed", "in", "processing")


class ReeditError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _tasks_root() -> Path:
    base = os.getenv("STUDIO_TASKS_ROOT")
    if not base:
        base = str(Path(__file__).resolve().parent.parent / "tasks")
    return Path(base)


def staging_root(run_id: str) -> Path:
    if not _safe_id(run_id):
        raise ReeditError("E_REEDIT_RUN_INVALID", "非法运行实例")
    return _tasks_root() / "output" / run_id


def _task_target_paths(task_id: str) -> list[Path]:
    root = _tasks_root()
    candidates = [
        root / "output" / task_id,
        *(root / directory / f"{task_id}.json" for directory in _TASK_RECORD_DIRS),
        root / "status" / f"{task_id}.json",
        root / "localtasks" / f"{task_id}.json",
        root / "localtasks" / task_id,
    ]
    return [path for path in candidates if path.exists()]


def build_old_manifest(project_id: str, run_id: str) -> dict[str, Any]:
    """激活前快照：旧 clip 集、旧任务集与精确删除目标（落盘到 staging）。"""
    with store.project_lock(project_id):
        manifest = store.load_manifest(project_id)
    clips = manifest.get("clips") or {}
    old_clips: dict[str, Any] = {}
    for clip_id, entry in clips.items():
        if str(entry.get("render_task_id") or "").strip() != run_id:
            old_clips[clip_id] = entry
    old_task_ids = sorted({str(entry.get("render_task_id") or "").strip()
                           for entry in old_clips.values()
                           if str(entry.get("render_task_id") or "").strip()})
    # 引用计数：该任务只要被任何「非旧切片」引用，就整个剔除出删除集（需求 9）
    deletable_task_ids = [
        task_id for task_id in old_task_ids
        if all(clip_id in old_clips for clip_id, entry in clips.items()
               if str(entry.get("render_task_id") or "") == task_id)]

    targets: list[str] = []
    for task_id in deletable_task_ids:
        for path in _task_target_paths(task_id):
            targets.append(str(path))

    document = {
        "schema": "reedit.purge_manifest/1",
        "project_id": project_id,
        "run_id": run_id,
        "expected_generation": int(manifest.get("generation") or 0),
        "old_entries": {cid: {"project_id": project_id, "clip_id": cid,
                              "render_task_id": e.get("render_task_id")}
                        for cid, e in old_clips.items()},
        "old_clip_ids": sorted(old_clips),
        "old_task_ids": deletable_task_ids,
        "skipped_referenced_task_ids": sorted(set(old_task_ids) - set(deletable_task_ids)),
        "targets": targets,
        "created_at_utc": datetime.now(UTC).isoformat(),
    }
    from . import r2
    artifact_roots = [Path(__file__).resolve().parent.parent / "studio-artifacts",
                      r2.local_path_for("studio-artifacts")]
    for artifact_root in artifact_roots:
        for tid in deletable_task_ids:
            if _safe_id(tid) and (artifact_root / tid).is_dir():
                document["targets"].append(str(artifact_root / tid))
    document["targets"] = list(dict.fromkeys(document["targets"]))
    for entry in document["old_entries"].values():
        entry["paths"] = [raw for raw in document["targets"]
                          if _target_task(Path(raw)) == entry["render_task_id"]]
    out_dir = staging_root(run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / REEDIT_MANIFEST).write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return document


def load_old_manifest(run_id: str) -> dict[str, Any] | None:
    path = staging_root(run_id) / REEDIT_MANIFEST
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("schema") != "reedit.purge_manifest/1":
        return None
    return document


def verify_new_outputs(run_id: str) -> tuple[int, int]:
    """校验 staging 里至少一条可解码、可播放的 clip_final.mp4。"""
    clips_dir = staging_root(run_id) / "clips"
    if not clips_dir.is_dir():
        return 0, 0
    playable = 0
    total = 0
    for final in clips_dir.glob("*/clip_final.mp4"):
        total += 1
        ok, _ = _ffprobe_playable(final)
        if ok:
            playable += 1
    return playable, total


def discard_staging(run_id: str) -> dict[str, Any]:
    """新批次失败：把 staging 整体移入回收站并删除（旧切片不动）。"""
    source = staging_root(run_id)
    if not source.exists():
        return {"removed": False, "failures": []}
    trash = _tasks_root() / purge.TRASH_DIRNAME / f"staging-{run_id}-{uuid.uuid4().hex[:8]}"
    trash.parent.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    try:
        shutil.move(str(source), str(trash))
    except OSError as exc:
        failures.append(f"staging 移动失败: {exc}")
        return {"removed": False, "failures": failures}
    try:
        shutil.rmtree(trash)
    except OSError as exc:
        failures.append(f"staging 回收站删除失败（保留 {trash}）: {exc}")
    return {"removed": True, "failures": failures, "trash": str(trash)}


def _safe_id(value):
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,80}", str(value)))


def _activation_receipt(project_id, run_id):
    return (store.load_manifest(project_id).get("replacement_runs") or {}).get(run_id)


def activate_new_run(project_id, run_id, plan_record, *, published=None):
    """已发布且可读条目才激活，一次持久台账提交切换实例和活跃集合。"""
    if not _safe_id(run_id):
        raise ReeditError("E_REEDIT_RUN_INVALID", "非法运行实例")
    old = load_old_manifest(run_id)
    if not old or old.get("project_id") != project_id:
        raise ReeditError("E_REEDIT_MANIFEST_MISSING", "缺少准备基线")
    # Worker 必须提供已发布回执；独立调用也必须验证本人视频，不按目录存在猜成功。
    if published is None:
        published = [{"clip_id": c.get("clip_id"),
                      "path": str(staging_root(run_id) / "clips" / str(c.get("clip_id")) / "clip_final.mp4")}
                     for c in plan_record.get("clips") or []]
    verified = {}
    for item in published:
        cid = str(item.get("clip_id") or "")
        path = Path(str(item.get("path") or ""))
        own = staging_root(run_id) / "clips" / cid / "clip_final.mp4" if _safe_id(cid) else None
        if (own is not None and path.is_file() and own.is_file()
                and path.name == "clip_final.mp4" and path.parent.name == cid
                and run_id in path.parts and _ffprobe_playable(path)[0]
                and _ffprobe_playable(own)[0]
                and path.stat().st_size == own.stat().st_size
                and store.sha256_file(path) == store.sha256_file(own)):
            verified[cid] = item
    planned = [c for c in plan_record.get("clips") or [] if str(c.get("clip_id")) in verified]
    if not planned:
        raise ReeditError("E_REEDIT_NOT_PUBLISHED", "没有已发布且验证可读的新切片")
    with store.project_lock(project_id):
        ledger = store.load_manifest(project_id)
        if (ledger.get("replacement_runs") or {}).get(run_id):
            return ledger
        if int(ledger.get("generation") or 0) != int(old.get("expected_generation") or 0):
            raise ReeditError("E_REEDIT_CONFLICT", "项目已由其它操作更新，请重新准备替换")
        entries = {}
        versions = store._versions_for_task(run_id)
        for number, clip in enumerate(planned, 1):
            cid = store.validate_clip_id(clip["clip_id"])
            previous = ledger["clips"].get(cid) or {}
            entries[cid] = {"clip_id": cid, "name": previous.get("name") or f"剪辑成片 {number:02d}",
                            "title": str(clip.get("topic") or ""), "topic": str(clip.get("topic") or ""),
                            "source_start": float(clip.get("start_ms") or 0)/1000,
                            "source_end": float(clip.get("end_ms") or 0)/1000,
                            "duration": float(clip.get("duration_ms") or 0)/1000,
                            "render_task_id": run_id,
                            "render_revision": int(previous.get("render_revision") or 0)+1,
                            "created_at": store.utc_now(), "task_history": [],
                            "published": verified[cid], **versions}
        ledger["clips"] = entries
        ledger["clip_order"] = list(entries)
        ledger["active_run_id"] = run_id
        ledger.setdefault("replacement_runs", {})[run_id] = {
            "state": "activated", "clip_ids": list(entries), "created_at": store.utc_now()}
        result = store.save_manifest(project_id, ledger)
    # 派生标记允许丢失；恢复以持久台账 receipt 为真相。
    try:
        marker = staging_root(run_id) / ACTIVATED_MARKER
        marker.write_text(store.utc_now(), encoding="utf-8")
    except OSError:
        pass
    return result


def activation_done(run_id):
    old = load_old_manifest(run_id)
    return bool(old and _activation_receipt(old["project_id"], run_id))


def _referenced_tasks():
    found = set()
    for path in (store.TASKS_ROOT / "projects").glob("*/clips.json"):
        try:
            ledger = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            # 损坏台账不能证明没有引用，宁可延后清理。
            raise ReeditError("E_REEDIT_REFERENCE_UNKNOWN", "其它项目台账不可读，暂缓清理")
        for entry in (ledger.get("clips") or {}).values():
            found.update(store.card_task_ids(entry))
            def nested_references(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key in {"render_task_id", "task_id"} and _safe_id(child):
                            found.add(str(child))
                        if key in {"path", "video_path", "cover_path", "object_key"} and isinstance(child, str):
                            parts = Path(child.removeprefix("local:")).parts
                            for offset, part in enumerate(parts[:-1]):
                                if part in {"output", "studio-artifacts"} and _safe_id(parts[offset + 1]):
                                    found.add(parts[offset + 1])
                        nested_references(child)
                elif isinstance(value, list):
                    for child in value:
                        nested_references(child)
            nested_references(entry)
    return found


def _target_task(path):
    root = _tasks_root().resolve()
    from . import r2
    artifact_roots = [(Path(__file__).resolve().parent.parent / "studio-artifacts").resolve(),
                      r2.local_path_for("studio-artifacts").resolve()]
    absolute = path.absolute()
    # 拒绝穿越、符号链接及白名单根自身；不把任意深度路径当任务根。
    if ".." in path.parts or absolute.resolve() != absolute:
        return None
    try:
        rel = absolute.relative_to(root)
        if len(rel.parts) != 2 or rel.parts[0] not in _ALLOWED_TARGET_ROOTS + ("in", "processing"):
            return None
        tid = rel.parts[1][:-5] if rel.parts[1].endswith(".json") else rel.parts[1]
    except ValueError:
        rel = None
        for artifacts in artifact_roots:
            try:
                rel = absolute.relative_to(artifacts)
                break
            except ValueError:
                pass
        if rel is None or len(rel.parts) != 1:
            return None
        tid = rel.parts[0]
    return tid if _safe_id(tid) else None


def _guarded_targets(manifest):
    references = _referenced_tasks()
    old_ids = set(manifest.get("old_task_ids") or [])
    return [Path(raw) for raw in manifest.get("targets") or []
            if _target_task(Path(raw)) in old_ids - references
            and _target_task(Path(raw)) != manifest.get("run_id")]


def _tree_stats(path: Path) -> tuple[int, int]:
    files, total_bytes = 0, 0
    if path.is_file():
        return 1, path.stat().st_size
    for item in path.rglob("*"):
        if item.is_file():
            files += 1
            try:
                total_bytes += item.stat().st_size
            except OSError:
                pass
    return files, total_bytes


def purge_old_clips(manifest, *, delete_artifacts_fn=None):
    """每一步持久化并按实例校验；激活后失败只产生清理警告。"""
    run_id, project_id = str(manifest.get("run_id") or ""), str(manifest.get("project_id") or "")
    saved = load_old_manifest(run_id)
    if saved != manifest or not activation_done(run_id):
        raise ReeditError("E_REEDIT_NOT_ACTIVATED", "清理必须使用已激活实例的持久精确清单")
    state_path = staging_root(run_id) / "cleanup_state.json"
    report_path = staging_root(run_id) / PURGE_REPORT
    with store.project_lock(project_id):
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
        else:
            state = {"targets": {}, "db_done": False, "log_done": False}
        def persist():
            store._atomic_json(state_path, state)
        failures = []
        references = _referenced_tasks()
        old_entries = {cid: e for cid, e in (manifest.get("old_entries") or {}).items()
                       if e.get("project_id") == project_id and e.get("clip_id") == cid}
        old_ids = set(manifest.get("old_task_ids") or []) & {
            e.get("render_task_id") for e in old_entries.values()}
        exact_paths = {raw for e in old_entries.values() for raw in e.get("paths") or []}
        eligible = sorted(old_ids - references - {run_id})
        trash = _tasks_root() / purge.TRASH_DIRNAME / ("reedit-" + run_id)
        for raw in manifest.get("targets") or []:
            source = Path(raw)
            tid = _target_task(source)
            if tid not in eligible or raw not in exact_paths:
                if tid in references:
                    failures.append(f"旧实例仍被当前台账引用，暂缓清理: {tid}")
                elif tid is None or raw not in exact_paths:
                    failures.append("拒绝不符合精确旧实例范围的路径")
                continue
            import hashlib
            destination = trash / hashlib.sha256(raw.encode()).hexdigest()[:20] / source.name
            item = state["targets"].setdefault(raw, {"source": raw, "trash": str(destination),
                                                   "moved": False, "done": False,
                                                   "files_deleted": 0, "bytes_freed": 0,
                                                   "inventory": []})
            if item["done"]:
                continue
            destination = Path(item["trash"])
            try:
                if not item["moved"]:
                    if source.exists():
                        # 硬链接以唯一inode记实际释放：还有外部引用的对象计0释放字节。
                        paths = [source] if source.is_file() else list(source.rglob("*"))
                        inodes = {}
                        for f in paths:
                            if f.is_symlink():
                                raise OSError("旧目标含符号链接，拒绝递归清理")
                            if f.is_file():
                                st = f.stat(); key = (st.st_dev, st.st_ino)
                                obj = inodes.setdefault(key, {"identity": f"{st.st_dev}:{st.st_ino}",
                                                             "bytes": st.st_size, "nlink": st.st_nlink, "paths": []})
                                obj["paths"].append(str(f.relative_to(source)) if f != source else ".")
                        item["inventory"] = list(inodes.values())
                        persist()  # 确定性trash位置先落盘，move后崩溃仍能找回。
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(source), str(destination))
                    elif not destination.exists():
                        item["done"] = True; persist(); continue
                    item["moved"] = True; persist()
                # 跨项目/后续实例引用复查，不能仅依赖激活时快照。
                if tid in _referenced_tasks():
                    raise OSError("旧实例重新被引用，保留回收站")
                for obj in item["inventory"]:
                    for relative in obj["paths"]:
                        target = destination if relative == "." else destination / relative
                        steps = obj.setdefault("steps", {})
                        step = steps.setdefault(relative, {"done": False})
                        if step["done"]:
                            continue
                        if target.exists():
                            if target.is_symlink():
                                raise OSError("回收站目标已变成符号链接，拒绝删除")
                            info = target.stat()
                            step["observed"] = True
                            step["freed_if_removed"] = info.st_size if info.st_nlink == 1 else 0
                            persist()
                            target.unlink()
                        # 只在unlink成功/已持久记录的pending目标缺失后确认释放。
                        if step.get("observed"):
                            step["done"] = True
                            item["files_deleted"] += 1
                            item["bytes_freed"] += step.get("freed_if_removed", 0)
                            persist()
                    if not obj.get("done"):
                        obj["done"] = True
                        persist()
                if destination.is_dir():
                    for directory in sorted((p for p in destination.rglob("*") if p.is_dir()),
                                            key=lambda p: len(p.parts), reverse=True):
                        if directory.is_symlink():
                            raise OSError("回收站出现符号链接，拒绝删除")
                        directory.rmdir()
                    destination.rmdir()  # 未在清单里的后来文件使其失败并保留，不能顺带递归删除。
                item["done"] = True; persist()
            except OSError as exc:
                failures.append(f"清理失败（保留可恢复位置）: {source}: {exc}")
        # 台账已在激活一次提交完成；这里绝不按stable clip_id删除任何卡片。
        if not state["db_done"] and delete_artifacts_fn is not None:
            try:
                delete_artifacts_fn(eligible)
                state["db_done"] = True; persist()
            except Exception as exc:
                failures.append(f"数据库artifact清理失败: {exc}")
        if not state["db_done"] and eligible:
            failures.append("数据库artifact清理待worker恢复")
        if not state["log_done"]:
            try:
                purge._prune_task_log(set(eligible))
                state["log_done"] = True; persist()
            except Exception as exc:
                failures.append(f"日志清理失败: {exc}")
        objects = {o.get("identity") or raw + ":" + str(number): o
                   for raw, v in state["targets"].items() for number, o in enumerate(v["inventory"])}
        removed_objects = {o.get("identity") or raw + ":" + str(number)
                           for raw, v in state["targets"].items() for number, o in enumerate(v["inventory"])
                           if any(step.get("done") for step in o.get("steps", {}).values())}
        report = {"clips_deleted": len(old_entries),
                  "files_deleted": len(removed_objects),
                  "paths_unlinked": sum(v["files_deleted"] for v in state["targets"].values()),
                  "bytes_before": sum(o["bytes"] for o in objects.values()),
                  "bytes_freed": sum(v["bytes_freed"] for v in state["targets"].values()),
                  "failures": failures, "trash_dir": str(trash), "duration_s": 0}
        store._atomic_json(report_path, report)
        return report


def retry_cleanup(project_id, run_id, *, delete_artifacts_fn=None):
    manifest = load_old_manifest(run_id)
    if manifest is None or manifest.get("project_id") != project_id:
        raise ReeditError("E_REEDIT_PROJECT_MISMATCH", "清单与项目不匹配或不存在")
    report = purge_old_clips(manifest, delete_artifacts_fn=delete_artifacts_fn)
    report["retry"] = True
    store._atomic_json(staging_root(run_id) / PURGE_REPORT, report)
    return report


def purge_old_job_artifact_dirs(old_task_ids):
    """旧接口不再允许无项目/实例清单直接删除；统一由持久清理状态机处理。"""
    return {"artifact_dirs_removed": [], "artifact_dirs_bytes_freed": 0,
            "artifact_dirs_failures": ["请通过已激活批次的精确清单清理"],
            "artifact_dirs_skipped": list(old_task_ids or [])}
