"""局部任务：各任务类型的执行 handler（cover/title/subtitle/timeline/audio/effects/render/apply）。"""

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

from .textutils import (  # noqa: F401
    _derive_title,
    _normalise_ranges,
    _parse_font_scale,
    _parse_remove_ranges,
    _subtract_ranges,
    _two_lines,
)

from .lifecycle import (  # noqa: F401
    _existing_apply_cover_task,
    _resolve_project_id,
    create_task,
    get_task,
    list_tasks,
)


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
        project_id, clip_id,
        segments=clip_segments_from_render_task(
            render_task_id, clip_id, source_start=record.get("source_start")))
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
        from ..studio_auto_edit_bridge import save_clip_transform  # 局部 import：避免模块级循环
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


