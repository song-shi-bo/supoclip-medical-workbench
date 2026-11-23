"""局部任务：切片路径/规则快照/skill 上下文/转写分段与字幕 override 工具。"""

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
    from ..studio_clip_locator import clip_dir as locate_clip_dir
    clip_dir = locate_clip_dir(_tasks_root(), render_task_id, clip_id)
    return entry, clip_dir, render_task_id


def _cover_path(record: dict[str, Any], render_task_id: str, clip_id: str) -> str:
    from ..studio_clip_locator import clip_asset
    return clip_asset(_tasks_root(), render_task_id, clip_id, "cover.jpg", required=False).relative_to(
        _tasks_root().resolve()).as_posix()


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
        return studio_workflow.snapshot_dir(_tasks_root(), task_id)
    except studio_workflow.WorkflowError:
        return _tasks_root() / LOCALTASKS_DIRNAME / str(task_id)


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
        document = _read_json(_tasks_root() / directory / f"{render_task_id}.json")
        if document:
            return document
    return {}


def _render_source_path(render_task_id: str) -> Path | None:
    """该 AUTO_EDIT 任务物化后的源视频绝对路径（相对任务根解析）。"""
    raw = str(_render_task_document(render_task_id).get("raw_video_path") or "")
    if not raw:
        return None
    resolved = (_tasks_root() / raw).resolve()
    if _tasks_root() not in resolved.parents or not resolved.is_file():
        return None
    return resolved


def clip_segments_from_render_task(
    render_task_id: str, clip_id: str, *, source_start: float | None = None,
) -> list[dict[str, Any]]:
    """从成片产物里取该 clip 的转写分段（成片时间基，已含 0.8s 片头时移）。

    转写 seed 与字幕合成共用同一份「成片自带分段」：用户的成片一旦渲染完成，
    Inspector 的转写/字幕两条链路不必再单独调一次「建转写基线」就能工作（惰性 seed）。
    两条产物来源：
    * 旧文件队列 ``autoedit_*`` 任务：``read_auto_edit_task`` 的 clips[].segments；
    * 新渲染管线（32 位 hex job id，没有旧任务 JSON）：回退读
      ``tasks/plans/<job_id>/clip_refinement.json`` 的逐切片精修分段（源时间基
      ``start_ms/end_ms`` + ``refined_text`` + ``speaker``，LLM 精修逐段带说话人），
      按 ``source_start`` 平移到成片基；缺 source_start 时不自造时间基，如实返回空表。
    ``read_auto_edit_task`` 的局部 import 避免模块级循环；任务不在/产物已回收时
    按「没有分段」如实返回空表，不抛错。
    """
    from ..studio_auto_edit_bridge import read_auto_edit_task  # 局部 import：避免模块级循环

    if not render_task_id or not clip_id:
        return []
    try:
        view = read_auto_edit_task(render_task_id)
    except (FileNotFoundError, ValueError, OSError):
        view = None
    if view is not None:
        for clip in view.get("clips") or []:
            if str(clip.get("clip_id")) == str(clip_id):
                return [item for item in (clip.get("segments") or []) if isinstance(item, dict)]
        return []
    return refinement_segments_for_clip(render_task_id, clip_id, source_start=source_start)


def refinement_segments_for_clip(
    job_id: str, clip_id: str, *, source_start: float | None = None,
) -> list[dict[str, Any]]:
    """新渲染管线的逐切片分段（成片时间基，已含 0.8s 片头偏移）。

    真源是 ``tasks/plans/<job_id>/clip_refinement.json`` 的 ``clips[clip_id].segments``
    （源时间基 ``start_ms/end_ms`` + ``refined_text`` + ``speaker``）。这里平移到
    成片时间基并保留源 ``segment_id``（后续说话人 overlay 可按 id 精确合并）。
    文件/切片缺失、source_start 未给 → 空表，不抛错。
    """
    if not job_id or not clip_id or source_start is None:
        return []
    try:
        from ..clip_refinement import refinements_root  # 局部 import：避免模块级循环

        record = json.loads(
            (refinements_root() / job_id / "clip_refinement.json").read_text(encoding="utf-8"))
        entry = (record.get("clips") or {}).get(clip_id)
        if not isinstance(entry, dict):
            return []
        result: list[dict[str, Any]] = []
        for item in entry.get("segments") or []:
            if not isinstance(item, dict):
                continue
            try:
                start = float(item.get("start_ms")) / 1000.0
                end = float(item.get("end_ms")) / 1000.0
            except (TypeError, ValueError):
                continue
            text = str(item.get("refined_text") or item.get("text") or "").strip()
            if not text or end <= source_start:
                continue
            result.append({
                "segment_id": str(item.get("segment_id") or ""),
                "start": round(max(start, source_start) - source_start + 0.8, 3),
                "end": round(end - source_start + 0.8, 3),
                "text": text,
                "speaker": str(item.get("speaker") or "未标注"),
            })
        return result
    except (FileNotFoundError, ValueError, OSError):
        return []


def _clip_transcript_window(record: dict[str, Any], render_task_id: str) -> list[dict[str, Any]]:
    """本条 clip 的转写分段（成片时间基，已含 0.8s 片头时移）。"""
    return clip_segments_from_render_task(
        render_task_id, str(record["clip_id"]), source_start=record.get("source_start"))


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
        return Path(str(path)).relative_to(_tasks_root()).as_posix()
    except (TypeError, ValueError):
        return None


