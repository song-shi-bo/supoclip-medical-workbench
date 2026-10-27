"""Local file-queue bridge from Studio to the medical production executor.

The Studio backend and render worker both run locally but independently. A
bind-mounted ``tasks`` folder is their contract: Studio writes an immutable
JSON task plus a copy of the source file, and the production worker owns all
rendering work and status updates.
"""

from __future__ import annotations

import json
import math
import os
import re
# ``os`` / ``shutil`` 保留为模块属性是刻意的：既有用例通过 ``bridge.os.link``、
# ``bridge.shutil.disk_usage``、``bridge.shutil.copy2`` 给底层文件系统调用打桩，
# 而 source_store 与这里共享同一模块对象，打桩因此对两边同时生效。
import shutil  # noqa: F401
import uuid
from . import studio_clip_locator as clip_locator
from datetime import UTC, datetime
from pathlib import Path
from .studio_access import prepare_project_operation, watch_legacy_operation
from typing import Any

from . import skill_registry, studio_workflow
from .clip_transform import TransformValidationError, parse_transform
from .source_store import DEFAULT_MIN_FREE_MB, materialise_source


TASKS_ROOT = Path(os.getenv("STUDIO_TASKS_ROOT", "/app/tasks"))
TASK_ID_PATTERN = re.compile(r"^autoedit_[a-f0-9]{12}$")
# artifact 读取放行旧 autoedit_* 任务与 32 位 hex job id（新渲染管线/reedit 的
# render_task_id）；仅用于 resolve_auto_edit_artifact，任务 JSON 读写仍用上面严格模式。
_ARTIFACT_ID_PATTERN = re.compile(r"^(?:autoedit_[a-f0-9]{12}|[a-f0-9]{32})$")
CLIP_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# Post-render, non-destructive frame transforms live in this sidecar so the
# immutable task JSON stays untouched after completion.
CLIP_TRANSFORM_SIDECAR = "clip_transforms.json"
# 成片风格微调（效果/音频）的渲染镜像：与 transform sidecar 同构，导出通道读取。
CLIP_EFFECTS_SIDECAR = "clip_effects.json"

# 时间基是「秒」：HH:MM:SS 出现即说明上游换了时间基，必须显式报错而不是猜着换算。
CLOCK_TIME_PATTERN = re.compile(r"^\d{1,3}:\d{1,2}(:\d{1,2})?(\.\d+)?$")
# 退回复制时才需要的安全余量（硬链接路径不消耗额外空间）。保留同名常量仅为
# 兼容既有引用；真正的实现与默认值都在 source_store.DEFAULT_MIN_FREE_MB。
COPY_SPACE_MARGIN_BYTES = DEFAULT_MIN_FREE_MB * 1024 * 1024


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _boundary_seconds(value: Any, *, field: str, clip_id: str) -> float:
    """把 clip_plan 的切片边界解析成「秒」；格式/时间基不对就显式报错。

    容忍数值与数值字符串（"16.3"）；显式拒绝 HH:MM:SS 时钟格式与 bool，
    避免 ``float("00:00:16.300")`` 这类晦涩异常或 ``float(True) == 1.0`` 的静默陷阱。
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"任务 clip_plan 边界非法（{clip_id}）: {field} 必须是数值秒")
    text = str(value).strip()
    if CLOCK_TIME_PATTERN.fullmatch(text):
        raise ValueError(
            f"任务 clip_plan 边界非法（{clip_id}）: {field} 是时钟格式 {text!r}，"
            "与秒时间基不一致（应为数值秒，如 16.3）"
        )
    try:
        number = float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"任务 clip_plan 边界非法（{clip_id}）: start/end 必须是数值秒"
        ) from exc
    if not math.isfinite(number):
        raise ValueError(f"任务 clip_plan 边界非法（{clip_id}）: {field} 不是有限数值")
    return number


def _materialise_source(source: Path, destination: Path) -> None:
    """把源视频放进任务的 media 目录：同盘硬链接优先（零额外占用），否则安全复制。

    2026-10 故障加固：旧实现无条件 ``shutil.copy2``，而源片有 3.07GB——每复渲染
    一次就复制一份，直接把数据盘写到只剩 197MB，video-brain 预检据此以
    HTTP 422 E_DISK_SPACE 拒收建任务；复制被 ENOSPC 打断还会留下一个 204MB 的
    截断 source.mp4（看似有效，实则后续探测才失败）。video-brain 只读该路径
    （resolve_asset / ffprobe 输入），因此硬链接语义等价。

    实现已收敛到 :mod:`src.source_store`（全链路唯一的源片物化入口）：同盘
    ``os.link``，跨设备回退到 ``.part`` + 原子改名的受保护复制，空间不足抛
    ``E_DISK_SPACE``。**源片只读契约见 source_store 模块 docstring。**
    """
    materialise_source(source, destination)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def attach_workflow_snapshot(
    task: dict[str, Any], *, task_id: str, snapshot_dir: Path
) -> dict[str, Any]:
    """生成任务级规则快照并挂到任务 JSON 上（Phase B 接线点 1）。

    - ``task_versions(snapshot)`` 写入任务 JSON：``workflow_version`` / ``skill_versions`` /
      ``workflow_snapshot_path`` —— 任务详情 API 与项目台账据此展示「这条成片用了哪版规则」；
    - ``attach_snapshot_to_payload(...)`` 把快照摘要挂成 ``workflow_snapshot`` 字段，
      执行器把它透传给 video-brain 的建任务载荷（未知字段被忽略，向后兼容）。

    快照目录就是该任务自己的目录（AUTO_EDIT：``output/{task_id}``）。清单缺失/损坏会
    显式抛 ``SkillRegistryError``，不让任务在「无规则」状态下悄悄跑。
    """
    snapshot = studio_workflow.build_task_snapshot(task_id, snapshot_dir=snapshot_dir)
    versions = skill_registry.task_versions(snapshot)
    task["workflow_version"] = versions.get("workflow_version")
    task["skill_versions"] = versions.get("skill_versions") or {}
    task["workflow_snapshot_path"] = versions.get("snapshot_path")
    return skill_registry.attach_snapshot_to_payload(task, snapshot)


def _task_paths(task_id: str) -> dict[str, Path]:
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError("Invalid auto-edit task id")
    root = TASKS_ROOT.resolve()
    return {
        "root": root,
        "incoming": root / "in" / f"{task_id}.json",
        "processing": root / "processing" / f"{task_id}.json",
        "completed": root / "completed" / f"{task_id}.json",
        "failed": root / "failed" / f"{task_id}.json",
        "status": root / "status" / f"{task_id}.json",
        "media": root / "media" / task_id,
        "output": root / "output" / task_id,
    }


def _artifact_task_paths(task_id: str) -> dict[str, Path]:
    """仅产物/侧车读写接受 staged job；不会放宽 legacy 队列提交 ID。"""
    output = clip_locator.output_root(TASKS_ROOT, task_id)
    root = TASKS_ROOT.resolve()
    return {"root": root, "output": output,
            **{name: root / directory / f"{task_id}.json" for name, directory in
               (("incoming", "in"), ("processing", "processing"),
                ("completed", "completed"), ("failed", "failed"), ("status", "status"))}}


def _project_dir(project_id: str, uploads_root: Path) -> Path:
    if not project_id.startswith("timeline_"):
        raise FileNotFoundError("Timeline project not found")
    root = (uploads_root / "studio-projects").resolve()
    project = (root / project_id).resolve()
    if root not in project.parents or not (project / "project.json").is_file():
        raise FileNotFoundError("Timeline project not found")
    return project


def _source_for_project(project_dir: Path) -> Path:
    raw_dir = project_dir / "raw"
    candidates = [path for path in raw_dir.iterdir() if path.is_file()]
    if len(candidates) != 1:
        raise FileNotFoundError("Timeline project source video not found")
    return candidates[0]


def project_source_path(project_id: str, uploads_root: Path) -> Path:
    """Resolve one Studio project's durable original video (never transcoded)."""

    return _source_for_project(_project_dir(project_id, uploads_root))


def _normalise_clip_transforms(value: Any) -> dict[str, dict[str, Any]]:
    """Validate an optional ``clip_transforms`` map into wire-format payloads."""

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("clip_transforms 必须是 JSON 对象")
    result: dict[str, dict[str, Any]] = {}
    for raw_clip_id, transform in value.items():
        clip_id = str(raw_clip_id)
        if not CLIP_ID_PATTERN.fullmatch(clip_id):
            raise ValueError(f"clip_transforms 的 clip_id 非法: {clip_id!r}")
        if transform is None:
            continue
        try:
            result[clip_id] = parse_transform(transform).to_payload()
        except TransformValidationError as exc:
            raise ValueError(f"成片 {clip_id} 的 transform 非法: {exc}") from exc
    return result


def _normalise_segments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("asr_segments must be a list")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        try:
            start, end = round(float(item.get("start", 0)), 3), round(float(item.get("end", 0)), 3)
        except (TypeError, ValueError):
            continue
        if text and end > start >= 0:
            result.append({
                "start": start,
                "end": end,
                "text": text,
                "speaker": str(item.get("speaker") or "未标注"),
            })
    if not result:
        raise ValueError("No usable timestamped transcript segments")
    return result


def submit_auto_edit_task(
    *,
    project_id: str,
    uploads_root: Path,
    asr_segments: Any,
    golden_sentences: Any = None,
    keywords: Any = None,
    clip_transforms: Any = None,
    style_id: str | None = None,
    style_source: str | None = None,
    transcript_pre_corrected: bool = False,
) -> dict[str, Any]:
    """Copy the durable Studio source to the shared queue and enqueue a task.

    The relative paths in JSON are intentional.  The exact same task works on
    Windows and in Docker; each side resolves it against its own shared task
    root rather than leaking a container-only path to the local executor.
    ``clip_transforms`` optionally pins pre-known per-clip frame transforms
    into the immutable task; post-render edits go through
    :func:`save_clip_transform` (sidecar) instead.

    ``style_id``（封面风格）由调用方从**规则**解析后传入
    （``studio_workflow.cover_style_id``：任务快照 cover_rules.default_style_id，
    无快照的旧路径回退清单现值）。它写进不可变任务 JSON（顶层 ``cover_style_id``
    + ``bridge.cover_style_id``），执行器既有透传逻辑据此把 ``style_id`` 传给
    video-brain。不传时回退到本任务刚冻结的快照生效值，绝不静默退回 legacy。

    ``transcript_pre_corrected``：asr_segments 来自工作台 TRANSCRIBE_ONLY 的已纠错
    转写存储时置 True，写进任务 JSON 顶层；执行器据此向 video-brain 传
    ``source_transcript_corrected=true``，跳过重复纠错（AUTO_EDIT 不再出现
    transcribing/correcting 的改写步骤，也不消耗纠错 LLM 配额）。
    """
    project = _project_dir(project_id, uploads_root)
    source = _source_for_project(project)
    segments = _normalise_segments(asr_segments)
    transforms = _normalise_clip_transforms(clip_transforms)
    task_id = f"autoedit_{uuid.uuid4().hex[:12]}"
    paths = _task_paths(task_id)

    access = prepare_project_operation(task_id, project_id, expected_bytes=source.stat().st_size * 2)
    try:
        for directory in (paths["incoming"].parent, paths["processing"].parent, paths["completed"].parent,
                          paths["failed"].parent, paths["status"].parent, paths["media"], paths["output"]):
            directory.mkdir(parents=True, exist_ok=True)

        destination = paths["media"] / source.name
        _materialise_source(source, destination)
        relative_source = destination.relative_to(paths["root"]).as_posix()
        relative_output = paths["output"].relative_to(paths["root"]).as_posix()
        goldens = golden_sentences if isinstance(golden_sentences, list) else []
        keyword_list = [str(keyword).strip() for keyword in (keywords or []) if str(keyword).strip()]
        task = {
            "task_id": task_id,
            "created_at": _utc_now(),
            "raw_video_path": relative_source,
            "asr_segments": segments,
            "golden_sentences": goldens,
            "keywords": keyword_list,
            "output_dir": relative_output,
            "create_cover": True,
            "add_subtitle": True,
        }
        if transcript_pre_corrected:
            task["transcript_pre_corrected"] = True
        if transforms:
            task["clip_transforms"] = transforms
        task["bridge"] = {
            "engine": "medical-video-production",
            "project_id": project_id,
            "mode": "vertical-highlight",
            "caption_style": "bold_pop",
            "ratio": "9:16",
            "note": "The local runner reuses Studio's corrected transcript and resolves paths relative to the shared tasks folder.",
        }
        # Phase B 接线点 1：本任务启动即冻结规则版本（快照落在本任务目录），并把摘要挂进
        # 提交给 video-brain 的载荷；后续所有步骤只认这份快照，规则更新只影响新任务。
        task = attach_workflow_snapshot(task, task_id=task_id, snapshot_dir=paths["output"])
        # 封面风格贯通（A 项）：优先用调用方按规则解析出的值；调用方没给时用本任务
        # 刚冻结快照里的 cover_rules.default_style_id（回退清单现值，最后才是缺省常量）。
        resolved_style = _normalise_style_id(style_id) or _style_id_from_task(task)
        resolved_source = (str(style_source or "").strip()
                           or (studio_workflow.SOURCE_USER_SPECIFIED if style_id
                               else studio_workflow.SOURCE_AUTO_EDIT_DEFAULT))
        task["cover_style_id"] = resolved_style
        # 风格来源可追溯：auto_edit_default / regenerate_default / user_specified
        task["cover_style_source"] = resolved_source
        task["bridge"]["cover_style_id"] = resolved_style
        task["bridge"]["cover_style_source"] = resolved_source
        _atomic_json(paths["status"], {
            "task_id": task_id,
            "task_type": "AUTO_EDIT",
            "project_id": project_id,
            "status": "queued",
            "stage": "任务已写入本机队列，等待固化剪辑流程执行器",
            "progress": 0.0,
            "created_at": task["created_at"],
            "updated_at": task["created_at"],
            "artifacts": [],
            # 任务详情 API（status 文件）与任务 JSON 一并带版本与封面风格，避免两处不一致。
            "workflow_version": task.get("workflow_version"),
            "skill_versions": task.get("skill_versions"),
            "workflow_snapshot_path": task.get("workflow_snapshot_path"),
            "cover_style_id": resolved_style,
            "cover_style_source": resolved_source,
        })
        _atomic_json(paths["incoming"], task)
        watch_legacy_operation(task_id, paths["status"], access=access)
        return read_auto_edit_task(task_id)
    except Exception:
        if paths["incoming"].is_file() or paths["processing"].is_file():
            watch_legacy_operation(task_id, paths["status"], access=access)
        else:
            access.release(task_id)
        raise



def _normalise_style_id(value: Any) -> str:
    """把调用方给的封面风格收敛成安全字符串（非法即显式报错，不写坏任务）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if not re.fullmatch(r"[a-z0-9_]{1,40}", text):
        raise ValueError(f"封面风格 style_id 非法: {value!r}")
    return text


def _style_id_from_task(task: dict[str, Any]) -> str:
    """从本任务冻结的规则快照取封面风格（无快照的旧路径回退清单现值）。"""
    snapshot = task.get("workflow_snapshot")
    rules = (snapshot or {}).get("rules") if isinstance(snapshot, dict) else None
    section = (rules or {}).get("cover_rules") if isinstance(rules, dict) else None
    value = str(((section or {}).get("default_style_id")) or "").strip()
    if value:
        return value
    try:
        return studio_workflow.cover_style_id(None)
    except Exception:  # noqa: BLE001 — 清单不可读时不阻断入队，用模块兜底
        return studio_workflow.FALLBACK_COVER_STYLE_ID


def _read_plan_highlights(paths: dict[str, Path]) -> list[dict[str, Any]]:
    """Read the highlight list a finished task exported into clip_plan.json.

    真实结构是顶层 ``{"engine", "video_brain_task_id", "highlights": [...],
    "golden_sentences", "keywords"}`` —— 切片列表在 ``highlights`` 下（**不是**
    ``clips``）。2026-10 故障中按 ``plan["clips"]`` 取值恒为 None，导致复渲染
    拿不到任何切片边界。只保留带 clip_id 的条目：没有 id 的条目无法被点名复渲染。
    """
    plan_path = paths["output"] / "clip_plan.json"
    if not plan_path.is_file():
        return []
    try:
        plan_document = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(plan_document, dict):
        return []
    highlights = plan_document.get("highlights")
    if not isinstance(highlights, list):
        return []
    return [
        item for item in highlights
        if isinstance(item, dict) and str(item.get("clip_id") or "").strip()
    ]


def rerender_auto_edit_task(task_id: str, clip_ids: list[str] | None = None,
                            style_id: str | None = None) -> dict[str, Any]:
    """Create a fresh task using newer rules while preserving old exports.

    With ``clip_ids`` the new task renders only those previously produced
    clips (single-clip re-render used by the Studio Copilot make_cover /
    render_clip actions).  Clip boundaries are resolved from the original
    task's clip_plan.json, never from client-supplied timestamps.

    ``style_id``（可选）钉定封面风格：写入不可变任务 JSON（``bridge.cover_style_id``
    与顶层 ``cover_style_id``），执行器在建 video-brain 任务时透传为
    ``TaskCreateRequest.style_id``，因此整片复渲染也会走该风格；不传则沿用
    video-brain 的配置缺省风格（存量行为不变）。
    """
    original_paths = _task_paths(task_id)
    task_path = next((candidate for candidate in (
        original_paths["completed"], original_paths["failed"], original_paths["processing"], original_paths["incoming"],
    ) if candidate.is_file()), None)
    if task_path is None:
        raise FileNotFoundError("Auto-edit task not found")
    original = json.loads(task_path.read_text(encoding="utf-8"))
    source = (original_paths["root"] / str(original.get("raw_video_path") or "")).resolve()
    if original_paths["root"] not in source.parents or not source.is_file():
        raise FileNotFoundError("Original task source video is missing")

    new_id = f"autoedit_{uuid.uuid4().hex[:12]}"
    paths = _task_paths(new_id)
    project_id = str((original.get("bridge") or {}).get("project_id") or "")
    access = prepare_project_operation(new_id, project_id, expected_bytes=source.stat().st_size * 2)
    try:
        for directory in (paths["incoming"].parent, paths["processing"].parent, paths["completed"].parent,
                          paths["failed"].parent, paths["status"].parent, paths["media"], paths["output"]):
            directory.mkdir(parents=True, exist_ok=True)
        destination = paths["media"] / source.name
        _materialise_source(source, destination)
        cloned = dict(original)
        cloned["task_id"] = new_id
        cloned["created_at"] = _utc_now()
        cloned["raw_video_path"] = destination.relative_to(paths["root"]).as_posix()
        cloned["output_dir"] = paths["output"].relative_to(paths["root"]).as_posix()
        cloned.pop("fixed_clips", None)
        bridge = dict(cloned.get("bridge") or {})
        bridge.pop("video_brain_task_id", None)
        bridge.pop("runner", None)
        bridge["rerender_of"] = task_id
        bridge["note"] = "Fresh local re-render using the latest fixed production rules; previous artifacts are preserved."
        if clip_ids:
            wanted = [str(value).strip() for value in clip_ids if str(value).strip()][:4]
            known = {str(item.get("clip_id")): item for item in _read_plan_highlights(original_paths)}
            picked = [known[value] for value in wanted if value in known]
            if not picked:
                raise ValueError(f"clip_ids 未匹配到任务 {task_id} 产物中的成片: {', '.join(wanted)}")
            fixed_clips: list[dict[str, Any]] = []
            for item in picked:
                clip_id = str(item.get("clip_id"))
                # 边界必须是数值秒且 0 <= start < end；HH:MM:SS 之类时间基不一致在这里
                # 显式报错，而不是让 float() 甩出晦涩异常或把 nan 写进任务 JSON。
                start = _boundary_seconds(item.get("start", 0), field="start", clip_id=clip_id)
                end = _boundary_seconds(item.get("end", 0), field="end", clip_id=clip_id)
                if not 0 <= start < end:
                    raise ValueError(
                        f"任务 {task_id} 的 clip_plan 边界非法（{clip_id}）: "
                        f"要求 0 <= start < end，收到 start={start}, end={end}"
                    )
                fixed_clips.append({
                    "clip_id": clip_id,
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "topic": str(item.get("topic") or ""),
                    "cover_title": str(item.get("cover_title") or ""),
                })
            cloned["fixed_clips"] = fixed_clips
            bridge["note"] = (
                f"Single-clip re-render of {','.join(str(item.get('clip_id')) for item in picked)} "
                "from the original plan; the clip window is pinned by transcript window and duration range."
            )
        # 封面风格：显式传入优先（Copilot/成员的复渲染指令）；否则按**原任务冻结的
        # 规则快照**解析（旧成片的历史规则可复现），再回退到清单现值。
        original_snapshot = studio_workflow.read_snapshot(original_paths["root"], task_id)
        if style_id:
            rerender_style = _normalise_style_id(style_id)
            rerender_source = studio_workflow.SOURCE_USER_SPECIFIED
        else:
            rerender_style, rerender_source = studio_workflow.cover_style_choice(
                original_snapshot, purpose="regenerate")
        cloned["cover_style_id"] = rerender_style
        cloned["cover_style_source"] = rerender_source
        bridge["cover_style_id"] = rerender_style
        bridge["cover_style_source"] = rerender_source
        cloned["bridge"] = bridge
        # 复渲染同样冻结「本次」的规则版本：新 revision 用的是新规则，旧成片的快照保持不动。
        cloned = attach_workflow_snapshot(cloned, task_id=new_id, snapshot_dir=paths["output"])
        _atomic_json(paths["status"], {
            "task_id": new_id, "task_type": "AUTO_EDIT", "project_id": str(bridge.get("project_id") or ""),
            "status": "queued",
            "stage": "已按最新固化规则创建复渲染任务，旧成片保持不变", "progress": 0.0,
            "created_at": cloned["created_at"], "updated_at": cloned["created_at"], "artifacts": [],
            "workflow_version": cloned.get("workflow_version"),
            "skill_versions": cloned.get("skill_versions"),
            "workflow_snapshot_path": cloned.get("workflow_snapshot_path"),
            "cover_style_id": rerender_style,
            "cover_style_source": rerender_source,
        })
        _atomic_json(paths["incoming"], cloned)
        watch_legacy_operation(new_id, paths["status"], access=access)
        return read_auto_edit_task(new_id)
    except Exception:
        if paths["incoming"].is_file() or paths["processing"].is_file():
            watch_legacy_operation(new_id, paths["status"], access=access)
        else:
            access.release(new_id)
        raise



def read_auto_edit_task(task_id: str) -> dict[str, Any]:
    paths = _task_paths(task_id)
    status_path = paths["status"]
    if not status_path.is_file():
        raise FileNotFoundError("Auto-edit task not found")
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    artifacts: list[dict[str, Any]] = []
    if paths["output"].exists():
        for path in sorted(paths["output"].rglob("*")):
            # 规则快照是任务元数据（由任务详情/规则中心端点单独读），不进产物清单：
            # 既有 UI 与「导出成片」的产物列表保持与接线前逐项一致。
            if path.is_file() and path.name != skill_registry.SNAPSHOT_FILENAME:
                artifacts.append({
                    "name": path.name,
                    "path": path.relative_to(paths["root"]).as_posix(),
                    "size_bytes": path.stat().st_size,
                })
    payload["artifacts"] = artifacts
    # A rendered clip is more than a downloadable file: the Studio needs its
    # own, clip-relative timeline when the user selects it in the media bin.
    # Keep that presentation data derived from the immutable task + plan so it
    # cannot drift from the FFmpeg input range.
    payload["clips"] = _read_rendered_clips(paths)
    return payload


def _clip_transform_index(paths: dict[str, Path]) -> dict[str, dict[str, Any]]:
    """Merge task-pinned and sidecar clip transforms; the sidecar wins.

    The sidecar is the post-render editing surface: users adjust transforms in
    the Studio player after the fixed pipeline already produced clip_final.mp4,
    and those edits must never rewrite the immutable task JSON.
    """

    task_map: dict[str, Any] = {}
    for candidate in (paths["completed"], paths["processing"], paths["incoming"], paths["failed"]):
        if candidate.is_file():
            try:
                loaded = json.loads(candidate.read_text(encoding="utf-8")).get("clip_transforms")
            except (OSError, json.JSONDecodeError):
                loaded = None
            if isinstance(loaded, dict):
                task_map = loaded
            break
    sidecar_path = paths["output"] / CLIP_TRANSFORM_SIDECAR
    sidecar: dict[str, Any] = {}
    if sidecar_path.is_file():
        try:
            loaded = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                sidecar = loaded
        except (OSError, json.JSONDecodeError):
            sidecar = {}
    merged: dict[str, dict[str, Any]] = {}
    for clip_id, transform in {**task_map, **sidecar}.items():
        if isinstance(transform, dict):
            merged[str(clip_id)] = transform
    return merged


def save_clip_transform(task_id: str, clip_id: str, transform: Any) -> dict[str, Any]:
    """Persist (or clear, with ``None``) one rendered clip's frame transform.

    Transforms are validated and stored in wire format inside the task's
    output sidecar; the full merged map is returned.
    """

    if not CLIP_ID_PATTERN.fullmatch(clip_id or ""):
        raise ValueError(f"clip_id 非法: {clip_id!r}")
    paths = _artifact_task_paths(task_id)
    if not paths["output"].is_dir():
        raise FileNotFoundError("Auto-edit task not found")
    clip_locator.clip_dir(TASKS_ROOT, task_id, clip_id, require_video=True)
    try:
        current: dict[str, Any] = dict(_clip_transform_index(paths))
    except (OSError, json.JSONDecodeError):
        current = {}
    if transform is None:
        current.pop(clip_id, None)
    else:
        try:
            current[clip_id] = parse_transform(transform).to_payload()
        except TransformValidationError as exc:
            raise ValueError(f"成片 {clip_id} 的 transform 非法: {exc}") from exc
    _atomic_json(paths["output"] / CLIP_TRANSFORM_SIDECAR, current)
    return current


def read_clip_transform(task_id: str, clip_id: str) -> dict[str, Any] | None:
    """Return one clip's saved transform, or ``None`` when it has none."""

    if not CLIP_ID_PATTERN.fullmatch(clip_id or ""):
        raise ValueError(f"clip_id 非法: {clip_id!r}")
    return _clip_transform_index(_artifact_task_paths(task_id)).get(clip_id)


def _read_effects_sidecar(paths: dict[str, Path]) -> dict[str, Any]:
    sidecar_path = paths["output"] / CLIP_EFFECTS_SIDECAR
    if not sidecar_path.is_file():
        return {}
    try:
        loaded = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(key): value for key, value in loaded.items() if isinstance(value, dict)}


def save_clip_render_settings(task_id: str, clip_id: str, settings: Any) -> dict[str, Any]:
    """Persist (or clear, with ``None``) one rendered clip's effects/audio snapshot.

    The snapshot is the render-side mirror of the clip config: the export
    channel reads it to apply picture/transition/audio filters.  ``None``
    removes the entry so a reset clip exports byte-identically to the original.
    """

    if not CLIP_ID_PATTERN.fullmatch(clip_id or ""):
        raise ValueError(f"clip_id 非法: {clip_id!r}")
    paths = _artifact_task_paths(task_id)
    if not paths["output"].is_dir():
        raise FileNotFoundError("Auto-edit task not found")
    clip_locator.clip_dir(TASKS_ROOT, task_id, clip_id, require_video=True)
    current = dict(_read_effects_sidecar(paths))
    if settings is None:
        current.pop(clip_id, None)
    else:
        if not isinstance(settings, dict):
            raise ValueError("render settings 必须是 JSON 对象")
        current[clip_id] = dict(settings)
    _atomic_json(paths["output"] / CLIP_EFFECTS_SIDECAR, current)
    return current


def read_clip_render_settings(task_id: str, clip_id: str) -> dict[str, Any] | None:
    """Return one clip's effects/audio snapshot, or ``None`` when it has none."""

    if not CLIP_ID_PATTERN.fullmatch(clip_id or ""):
        raise ValueError(f"clip_id 非法: {clip_id!r}")
    return _read_effects_sidecar(_artifact_task_paths(task_id)).get(clip_id)


def _read_rendered_clips(paths: dict[str, Path]) -> list[dict[str, Any]]:
    plan_path = paths["output"] / "clip_plan.json"
    if not plan_path.is_file():
        return []

    try:
        plan_document = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    task: dict[str, Any] = {}
    for candidate in (paths["completed"], paths["processing"], paths["incoming"], paths["failed"]):
        if candidate.is_file():
            try:
                task = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                task = {}
            break
    source_segments = task.get("asr_segments") if isinstance(task.get("asr_segments"), list) else []
    saved_transforms = _clip_transform_index(paths)
    clips: list[dict[str, Any]] = []
    for index, item in enumerate(plan_document.get("highlights", []), 1):
        if not isinstance(item, dict):
            continue
        try:
            source_start = float(item["start"])
            source_end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        clip_id = str(item.get("clip_id") or f"clip_{index:02}")
        clip_dir = clip_locator.clip_dir(paths["root"], paths["status"].stem, clip_id)
        video = clip_dir / "clip_final.mp4"
        cover = clip_dir / "cover.jpg"
        if not video.is_file():
            continue
        # The renderer prepends a 0.8s silent cover intro.  Translate every
        # source transcript segment into this exact final-video timebase.
        clip_segments: list[dict[str, Any]] = []
        for segment in source_segments:
            if not isinstance(segment, dict):
                continue
            try:
                start, end = float(segment.get("start", 0)), float(segment.get("end", 0))
            except (TypeError, ValueError):
                continue
            text = str(segment.get("text") or "").strip()
            if not text or end <= source_start or start >= source_end:
                continue
            clip_segments.append({
                "start": round(max(start, source_start) - source_start + 0.8, 3),
                "end": round(min(end, source_end) - source_start + 0.8, 3),
                "text": text,
                "speaker": str(segment.get("speaker") or "未标注"),
            })
        clip_entry: dict[str, Any] = {
            "task_id": paths["status"].stem,
            "clip_id": clip_id,
            "name": f"剪辑成片 {index:02}",
            "topic": str(item.get("topic") or item.get("cover_title") or "自动剪辑成片"),
            "source_start": round(source_start, 3),
            "source_end": round(source_end, 3),
            "duration": round(source_end - source_start + 0.8, 3),
            "video_path": video.relative_to(paths["root"]).as_posix(),
            "cover_path": cover.relative_to(paths["root"]).as_posix() if cover.is_file() else None,
            "segments": clip_segments,
        }
        # Carry the saved frame transform so the Studio player/export restores
        # it after refresh or re-opening the project.
        if clip_id in saved_transforms:
            clip_entry["transform"] = saved_transforms[clip_id]
        clips.append(clip_entry)
    return clips


def resolve_auto_edit_artifact(task_id: str, artifact_path: str) -> Path:
    """Return one exported artifact without allowing traversal outside its task.

    2026-10-16 修复：放行两类任务标识 —— 旧 ``autoedit_*`` 任务与 32 位 hex
    job id（新渲染管线/reedit 卡片的 ``render_task_id``）。此前这里复用
    ``_task_paths`` 的 ``TASK_ID_PATTERN``，job id 一律 400，导致台账卡片
    指向的新成片永远无法经 /studio/auto-edit/{id}/artifact 播放。
    仅放宽 artifact 读取；任务 JSON 的读写仍走 ``_task_paths`` 严格校验。
    """
    if not artifact_path or Path(artifact_path).is_absolute():
        raise FileNotFoundError("Artifact not found")
    if not _ARTIFACT_ID_PATTERN.fullmatch(str(task_id or "")):
        raise ValueError("Invalid auto-edit task id")
    output_root = clip_locator.output_root(TASKS_ROOT, task_id)
    relative = Path(artifact_path)
    if relative.parts[:2] == ("output", task_id):
        relative = Path(*relative.parts[2:])
    elif relative.parts and relative.parts[0] == "output":
        raise FileNotFoundError("Artifact not found")
    if ".." in relative.parts:
        raise FileNotFoundError("Artifact not found")
    if relative.parts and relative.parts[0] == "clips":
        if len(relative.parts) < 3:
            raise FileNotFoundError("Artifact not found")
        return clip_locator.clip_asset(TASKS_ROOT, task_id, relative.parts[1],
                                       Path(*relative.parts[2:]).as_posix())
    if len(relative.parts) >= 2 and CLIP_ID_PATTERN.fullmatch(relative.parts[0]):
        # 旧链接仍只指向本 run 的同一 clip；staged 权威布局缺片时不能回退。
        return clip_locator.clip_asset(TASKS_ROOT, task_id, relative.parts[0],
                                       Path(*relative.parts[1:]).as_posix())
    artifact = (output_root / relative).resolve()
    if artifact != output_root / relative or output_root not in artifact.parents or not artifact.is_file():
        raise FileNotFoundError("Artifact not found")
    return artifact


def auto_edit_clip_dir(task_id: str, clip_id: str) -> Path:
    """成片目录 ``tasks/output/{task_id}/{clip_id}``（可写；调用方自行确认文件存在）。

    clip_id 用与成片产物同一套规则校验，避免 ``../`` 之类穿越写坏别人的任务目录。
    """
    if not CLIP_ID_PATTERN.fullmatch(str(clip_id or "")):
        raise ValueError("Invalid clip id")
    return clip_locator.clip_dir(TASKS_ROOT, task_id, clip_id)
