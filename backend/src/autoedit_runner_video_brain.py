"""DSH 端「固化剪辑流程执行器」：把 Studio 自动剪辑队列接到 video-brain。

角色与本地 Codex 执行器一致，消费 ``studio_auto_edit_bridge`` 写入的文件队列：

    tasks/in/autoedit_xxx.json        # Studio 提交的不可变任务
    tasks/processing/…                # 执行器认领后
    tasks/completed/…  tasks/failed/… # 终态
    tasks/status/{task_id}.json       # 进度（前端轮询）
    tasks/output/{task_id}/{clip}/    # clip_final.mp4 + cover.jpg + clip_plan.json

链路：认领任务 → POST video-brain /v1/tasks → 轮询状态并翻译成 Studio 进度
→ 拷贝切片产物 → 合成 clip_plan.json（桥接据此还原 Studio 时间线）→ 终态回写。

video-brain 渲染器与桥接契约天然对齐：clip_final.mp4 / cover.jpg 同名，
封面片头同为 0.8s 静帧，因此桥接里 +0.8s 的字幕时移无需改动。

用法（与后端共享同一个 STUDIO_TASKS_ROOT）::

    STUDIO_TASKS_ROOT=/domain/supoclip-medical-workbench/tasks \
    VIDEO_BRAIN_URL=http://127.0.0.1:8940 \
    python backend/src/autoedit_runner_video_brain.py

仅依赖标准库；需在同一台机器上能直接读到 video-brain 的
``data/tasks/{vb_task_id}/clips/`` 产物目录（VIDEO_BRAIN_HOME 可覆盖）。
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from studio_runtime import process_lock

TASK_ID_PATTERN = re.compile(r"^autoedit_[a-f0-9]{12}$")

TASKS_ROOT = Path(os.getenv("STUDIO_TASKS_ROOT", "/app/tasks")).resolve()
VIDEO_BRAIN_URL = os.getenv("VIDEO_BRAIN_URL", "http://127.0.0.1:8940").rstrip("/")
VIDEO_BRAIN_HOME = Path(os.getenv("VIDEO_BRAIN_HOME", "/domain/video-brain")).resolve()
POLL_SECONDS = float(os.getenv("AUTOEDIT_POLL_SECONDS", "2"))
IDLE_SECONDS = float(os.getenv("AUTOEDIT_IDLE_SECONDS", "3"))
TASK_TIMEOUT_SECONDS = float(os.getenv("AUTOEDIT_TASK_TIMEOUT_SECONDS", "3600"))

# video-brain 规划器可接受的最短切片：复渲染在钉定窗口时把下界抬到该值，
# 因此短于它的固定切片无论怎么规划都不可能成立，必须在提交前显式拒绝。
MIN_FIXED_CLIP_SECONDS = 1.0
# HH:MM:SS(.mmm) 时钟格式：执行器与 video-brain 的时间基是「秒」，
# 收到时钟格式说明上游换了时间基，必须显式失败而不是猜着换算。
CLOCK_TIME_PATTERN = re.compile(r"^\d{1,3}:\d{1,2}(:\d{1,2})?(\.\d+)?$")

STAGE_ZH = {
    "queued": "任务已受理（video-brain）",
    "media_preflight": "媒体预检",
    "transcribing": "语音转写",
    "correcting": "医学同音纠错",
    "retrieving_rules": "规则检索",
    "planning": "切片规划",
    "validating_plan": "规划校验",
    "quality_checking": "成片质检",
    "completed": "成片完成",
    "partial_failed": "部分切片失败",
    "failed": "任务失败",
    "cancelled": "任务取消",
}

_shutdown = False


def _log(message: str) -> None:
    print(f"[autoedit-runner {time.strftime('%H:%M:%S')}] {message}", flush=True)


# video-brain 预检/建任务错误码 → 中文可读原因（详情仍保留原始 detail）。
VB_ERROR_ZH = {
    "E_DISK_SPACE": "数据盘剩余空间不足（video-brain 要求至少 500MB 空闲），请清理磁盘后重新制作",
    "E_SOURCE_MISSING": "任务源视频在 video-brain 侧不可读",
    "E_VIDEO_CORRUPT": "任务源视频损坏（无法解码首帧）",
    "E_NO_AUDIO": "任务源视频没有音轨（口播科普必须有原声）",
}


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """安全读取 HTTPError 的响应体文本，让失败日志/状态带上可读原因。"""
    try:
        return exc.read().decode("utf-8", "replace").strip()[:500]
    except Exception:
        return ""


def _vb_error_zh(detail: str) -> str:
    """把 video-brain 422 响应体翻译成「中文原因 + 错误码 + 原始 detail」。

    错误码必须保真：排障时靠它检索（如 E_DISK_SPACE），只给中文描述会把可检索的
    机器码丢掉。解析失败或结构不认识时原样返回，绝不吞掉响应体。
    """
    try:
        payload = json.loads(detail)
    except (ValueError, AttributeError):
        return detail
    inner = payload.get("detail") if isinstance(payload, dict) else None
    if not isinstance(inner, dict):
        return detail
    code = str(inner.get("code") or "")
    raw = str(inner.get("detail") or "") or detail
    if code in VB_ERROR_ZH:
        return f"{VB_ERROR_ZH[code]}（{code}: {raw}）"
    if code:
        return f"{raw}（{code}）"
    return detail


def _seconds(value: Any, *, field: str, label: str) -> float:
    """把时间边界统一成「秒」的浮点数；格式/时间基不对就显式报错。

    容忍数值与数值字符串（"16.3"）；显式拒绝 HH:MM:SS 时钟格式、布尔的
    True/False（float(True)==1.0 这类静默陷阱）以及 NaN/Inf。
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label}: {field} 必须是数值秒，收到 {value!r}")
    text = str(value).strip()
    if CLOCK_TIME_PATTERN.fullmatch(text):
        raise ValueError(
            f"{label}: {field} 是时钟格式 {text!r}，与秒时间基不一致（应为数值秒，如 16.3）"
        )
    try:
        number = float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}: {field} 不是合法数值秒: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label}: {field} 不是有限数值: {value!r}")
    return number


def _request(method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 30) -> Any:
    request = urllib.request.Request(
        f"{VIDEO_BRAIN_URL}{path}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else None


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{uuid_hex()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def uuid_hex() -> str:
    return os.urandom(8).hex()


def _task_dirs(task_id: str) -> dict[str, Path]:
    return {
        "incoming": TASKS_ROOT / "in" / f"{task_id}.json",
        "processing": TASKS_ROOT / "processing" / f"{task_id}.json",
        "completed": TASKS_ROOT / "completed" / f"{task_id}.json",
        "failed": TASKS_ROOT / "failed" / f"{task_id}.json",
        "status": TASKS_ROOT / "status" / f"{task_id}.json",
        "output": TASKS_ROOT / "output" / task_id,
    }


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _update_status(task_id: str, *, status: str, stage: str, progress: float,
                   created_at: str, error: str | None = None,
                   project_id: str | None = None) -> None:
    status_path = _task_dirs(task_id)["status"]
    # **合并**而不是覆盖：Studio 提交任务时写进状态镜像的规则版本字段
    # （workflow_version / skill_versions / workflow_snapshot_path，Phase B）
    # 必须活到任务终态，否则「任务详情 API」读不到这条成片用的是哪版规则。
    payload: dict[str, Any] = _read_json(status_path) or {}
    payload.update({
        "task_id": task_id,
        # 任务表里必须能一眼区分「自动剪辑」与局部任务（封面/标题/字幕/时间线）。
        "task_type": "AUTO_EDIT",
        # project_id 让「项目任务表」端点也能列出整片自动剪辑任务（不只是局部任务）。
        "project_id": project_id or payload.get("project_id") or None,
        "status": status,
        "stage": stage,
        "progress": round(max(0.0, min(progress, 1.0)), 4),
        "created_at": created_at,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "artifacts": payload.get("artifacts") or [],
    })
    payload.pop("error", None)
    if error:
        payload["error"] = error
    _atomic_json(status_path, payload)


def _vb_step_stage(view: dict[str, Any]) -> tuple[str, float]:
    """把 video-brain 的步骤账本翻译成（中文阶段, 进度 0-1）。"""
    if view.get("status") == "pending_review":
        return "等待审核：" + str(view.get("error") or "请处理审核项"), 0.5
    steps = view.get("steps") or []
    total = len(steps) or 1
    done = sum(1 for step in steps if step.get("status") == "done")
    running = next((step for step in steps if step.get("status") == "running"), None)
    current = running or next((step for step in reversed(steps) if step.get("status") == "pending"), None)
    name = str((current or {}).get("step") or view.get("status") or "")
    progress = (done + (0.5 if running else 0.0)) / total
    if name.startswith("rendering_clip"):
        rendered = sum(1 for clip in view.get("clips") or [] if clip.get("status") in {"rendered", "qc_passed", "qc_failed"})
        total_clips = max(len(view.get("clips") or []) or (view.get("snapshot") or {}).get("resolved_clip_count") or 1, 1)
        stage = f"渲染切片 {min(rendered + 1, total_clips)}/{total_clips}"
        substep = view.get("current_substep")
        if substep == "assembling_final_video":
            stage += "·合成成片"
        elif substep == "building_subtitles":
            stage += "·烧录字幕"
        elif substep == "rendering_cover":
            stage += "·生成封面"
        return stage, progress
    return STAGE_ZH.get(name, name or "处理中"), progress


def _claim_next_task() -> tuple[str, Path, dict[str, Any]] | None:
    for candidate in sorted((TASKS_ROOT / "in").glob("autoedit_*.json")):
        task_id = candidate.stem
        if not TASK_ID_PATTERN.fullmatch(task_id):
            continue
        target = _task_dirs(task_id)["processing"]
        try:
            os.replace(candidate, target)
        except OSError:
            continue  # 被其它执行器抢走
        return task_id, target, _read_json(target)
    return None


def _fixed_clips(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Validated fixed-clip subset (single-clip re-render), empty when absent.

    2026-10 故障加固：旧实现把解析失败的边界 ``continue`` 掉，等于把用户点名的
    切片悄悄丢掉——复渲染会「成功」但少交一条成片。固定切片是显式意图，任何
    结构/格式/时间基问题都必须显式失败。
    """
    fixed = task.get("fixed_clips")
    if not isinstance(fixed, list):
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(fixed[:4], 1):
        if not isinstance(item, dict):
            raise ValueError(f"fixed_clips[{index}] 不是对象: {item!r}")
        clip_id = str(item.get("clip_id") or "")
        label = clip_id or f"fixed_clips[{index}]"
        if "start" not in item or "end" not in item:
            raise ValueError(f"{label}: fixed_clips 缺少 start/end 边界")
        start = _seconds(item["start"], field="start", label=label)
        end = _seconds(item["end"], field="end", label=label)
        if not 0 <= start < end:
            raise ValueError(
                f"{label}: 切片边界非法（start={start}, end={end}），要求 0 <= start < end"
            )
        if end - start < MIN_FIXED_CLIP_SECONDS:
            raise ValueError(
                f"{label}: 切片时长 {end - start:.3f}s 低于 video-brain 可渲染下限 "
                f"{MIN_FIXED_CLIP_SECONDS}s，无法复渲染"
            )
        result.append({"clip_id": clip_id, "start": start, "end": end})
    return result


def _transcript_segments(segments: list[Any]) -> list[dict[str, Any]]:
    """把任务的 asr_segments 规范成 video-brain 的 source_transcript 载荷。

    逐段丢弃空文本 / 非法边界的坏段，而不是让整单转写失败；边界统一走
    :func:`_seconds`，时钟格式段会被剔除（不会被静默当成另一种时间基）。
    """
    result: list[dict[str, Any]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        # video-brain 的 SourceTranscriptSegment 要求 text 非空白，否则整单 422
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        try:
            start = _seconds(segment.get("start", 0), field="start", label="asr_segments")
            end = _seconds(segment.get("end", 0), field="end", label="asr_segments")
        except ValueError:
            continue
        if start < 0 or end <= start:
            continue
        result.append({"start": start, "end": end, "text": text})
    return result


def _start_video_brain_task(task_id: str, task: dict[str, Any]) -> str:
    media = (TASKS_ROOT / str(task.get("raw_video_path") or "")).resolve()
    if TASKS_ROOT not in media.parents or not media.is_file():
        raise FileNotFoundError(f"任务源视频缺失: {task.get('raw_video_path')}")
    bridge = task.get("bridge") or {}
    fixed = _fixed_clips(task)
    request: dict[str, Any] = {
        "source_video": str(media),
        "required_clip_count": "auto",
        "platform": "douyin" if bridge.get("ratio", "9:16") in {"9:16", "9:16"} else "douyin",
        "idempotency_key": f"studio-{task_id}",
    }
    # 封面风格透传：任务 JSON（顶层或 bridge）里带了 cover_style_id 就传给
    # video-brain 的 TaskCreateRequest.style_id，驱动整片复渲染的封面风格；
    # 不带则维持 video-brain 的配置缺省风格（存量行为不变）。
    cover_style_id = str(task.get("cover_style_id") or bridge.get("cover_style_id") or "").strip()
    if cover_style_id:
        request["style_id"] = cover_style_id
    # Phase B 接线：任务的规则快照摘要随建任务载荷一起提交（video-brain 忽略未知字段）。
    # 这样「本次成片用了哪版规则」在两边都可追溯，且不改变既有渲染行为。
    snapshot = task.get("workflow_snapshot")
    if isinstance(snapshot, dict) and snapshot:
        request["workflow_snapshot"] = snapshot
    # AUTO_EDIT 复用工作台已纠错转写：bridge 在任务 JSON 顶层标了
    # transcript_pre_corrected=true 时，向 video-brain 声明
    # source_transcript_corrected —— 其 _step_correct 跳过重复纠错（不再消耗纠错 LLM 配额）。
    if bool(task.get("transcript_pre_corrected")) or bool(bridge.get("transcript_pre_corrected")):
        request["source_transcript_corrected"] = True
    raw_segments = task.get("asr_segments")
    if isinstance(raw_segments, list) and raw_segments:
        segments = _transcript_segments(raw_segments)
        if not segments:
            raise ValueError("asr_segments 过滤空文本段/非法边界后为空，无法提交转写")
        window = segments
        if fixed:
            # 单切片复渲染：把转写裁到固定切片窗口内，并把切片数/时长范围钉在
            # 原切片边界上——规划器在该窗口内唯一满足时长约束的候选就是原切片。
            lo = min(item["start"] for item in fixed)
            hi = max(item["end"] for item in fixed)
            window = [
                segment for segment in segments
                if segment["end"] > lo + 0.02 and segment["start"] < hi - 0.02
            ]
            if not window:
                raise ValueError(f"固定切片窗口 [{lo:.2f}, {hi:.2f}] 内没有可用转写段")
            durations = [item["end"] - item["start"] for item in fixed]
            request["required_clip_count"] = max(1, len(fixed))
            # 下界抬到 MIN_FIXED_CLIP_SECONDS，上界再兜一次下界：旧实现对 <1.05s
            # 的切片会算出 lo>hi 的倒挂区间，规划器必然找不到候选。
            lower = round(max(MIN_FIXED_CLIP_SECONDS, min(durations) - 0.05), 3)
            upper = round(max(max(durations) + 0.05, lower + 0.05), 3)
            request["duration_range"] = [lower, upper]
            # 钉窗（不是默认目标）：复渲染必须复现原切片窗口，不拆分、不例外放行
            request["duration_pinned"] = True
        request["source_transcript"] = window
    view = _request("POST", "/v1/tasks", request)
    return str(view["task_id"])


def _export_artifacts(studio_task_id: str, vb_task_id: str, view: dict[str, Any], task: dict[str, Any]) -> dict[str, int]:
    clips_root = VIDEO_BRAIN_HOME / "data" / "tasks" / vb_task_id / "clips"
    output_dir = _task_dirs(studio_task_id)["output"]
    highlights: list[dict[str, Any]] = []
    copied = failed = 0
    rejected: list[dict[str, Any]] = []
    for clip in view.get("clips") or []:
        clip_id = str(clip.get("clip_id") or "").replace("/", "_")
        source_dir = clips_root / clip_id
        final = source_dir / "clip_final.mp4"
        # B 项：四类门槛未通过的该条**不输出**（不是静默留着当正常成片）
        if clip.get("rejected"):
            rejected.append({
                "clip_id": clip_id,
                "stage": "reject_clip",
                "reasons": list(clip.get("rejected_reasons") or []),
                "plan_source": str(clip.get("plan_source") or ""),
            })
            continue
        if not final.is_file():
            if clip.get("status") in {"failed", "pending_recomposition"}:
                failed += 1
            continue
        target_dir = output_dir / clip_id
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(final, target_dir / "clip_final.mp4")
        cover = source_dir / "cover.jpg"
        if cover.is_file():
            shutil.copy2(cover, target_dir / "cover.jpg")
        # 封面版面（B 项来源/限额/降级 + 风格贯通）随成片回传：工作台据此核对
        # 「封面用的是哪版风格、文案来自 llm 还是 rules、有没有降级」。
        layout = source_dir / "cover_layout.json"
        if layout.is_file():
            shutil.copy2(layout, target_dir / "cover_layout.json")
        highlights.append({
            "clip_id": clip_id,
            "start": round(float(clip.get("start") or 0), 3),
            "end": round(float(clip.get("end") or 0), 3),
            "topic": str(clip.get("topic") or ""),
            "cover_title": str(clip.get("title") or clip.get("topic") or "医疗科普切片"),
            # ---- 规则闸门与分档标记（任务详情/卡片据此显示「降级产物」）----
            "plan_source": str(clip.get("plan_source") or ""),
            "validated": bool(clip.get("validated", True)),
            "needs_review": bool(clip.get("needs_review")),
            "needs_review_reasons": list(clip.get("needs_review_reasons") or []),
            "theme_type": str(clip.get("theme_type") or ""),
            "duration_target": list(clip.get("duration_target") or []),
            "duration_exception": bool(clip.get("duration_exception")),
            "duration_exception_reason": str(clip.get("duration_exception_reason") or ""),
            "source_segment_ids": dict(clip.get("source_segment_ids") or {}),
            "gates": dict(clip.get("gates") or {}),
            "hook_not_verbatim": bool(clip.get("hook_not_verbatim")),
            "hook_duplicate": bool(clip.get("hook_duplicate")),
            "cover_copy_source": str(clip.get("cover_copy_source") or ""),
            "cover_copy_degraded": bool(clip.get("cover_copy_degraded")),
        })
        copied += 1
    _atomic_json(output_dir / "clip_plan.json", {
        "engine": "medical-video-production",
        "video_brain_task_id": vb_task_id,
        "highlights": highlights,
        "golden_sentences": task.get("golden_sentences") or [],
        "keywords": task.get("keywords") or [],
        # 规则版本 + 任务级闸门（纠错门禁 / 规划闸门）随计划一起落盘
        "workflow_version": task.get("workflow_version"),
        "skill_versions": task.get("skill_versions") or {},
        "workflow_snapshot_path": task.get("workflow_snapshot_path"),
        "cover_style_id": str(task.get("cover_style_id") or ""),
        "correction_gate": view.get("correction_gate"),
        "plan_gate": view.get("plan_gate"),
        "rejected_clips": rejected,
    })
    # G.6：把本次生效的规则版本 + 每条的四类校验结论 + 例外标记写进任务规则快照，
    # 任务详情读取 workflow_snapshot.json 即可看到「这次是按哪版规则、哪些判定出的片」。
    _write_snapshot_audit(studio_task_id, task, view, highlights, rejected)
    return {"copied": copied, "failed": failed, "rejected": len(rejected)}


def _write_snapshot_audit(studio_task_id: str, task: dict[str, Any],
                          view: dict[str, Any], highlights: list[dict[str, Any]],
                          rejected: list[dict[str, Any]]) -> None:
    """把四类门槛结论与例外标记追加进任务级 workflow_snapshot.json（best-effort）。"""
    try:
        from skill_registry import append_snapshot_audit  # noqa: PLC0415
    except ImportError:  # 缺注册表模块时不影响成片交付
        return
    try:
        audit = {
            "video_brain_task_id": view.get("task_id"),
            "workflow_version": task.get("workflow_version"),
            "skill_versions": task.get("skill_versions") or {},
            "cover_style_id": str(task.get("cover_style_id") or ""),
            "correction_gate": view.get("correction_gate"),
            "plan_gate": view.get("plan_gate"),
            "clips": [
                {"clip_id": item["clip_id"],
                 "gates": item.get("gates") or {},
                 "plan_source": item.get("plan_source"),
                 "validated": item.get("validated"),
                 "needs_review": item.get("needs_review"),
                 "needs_review_reasons": item.get("needs_review_reasons") or [],
                 "duration_target": item.get("duration_target") or [],
                 "duration_exception": item.get("duration_exception"),
                 "duration_exception_reason": item.get("duration_exception_reason") or "",
                 "source_segment_ids": item.get("source_segment_ids") or {},
                 "theme_type": item.get("theme_type"),
                 "cover_copy_source": item.get("cover_copy_source"),
                 "cover_copy_degraded": item.get("cover_copy_degraded")}
                for item in highlights],
            "rejected_clips": rejected,
        }
        append_snapshot_audit(_task_dirs(studio_task_id)["output"], audit)
    except Exception as exc:  # noqa: BLE001 — 审计写入失败不能拖垮成片交付
        _log(f"{studio_task_id}: 规则快照审计写入失败（不影响成片）：{exc!r}")


def _workflow_versions(task_id: str) -> dict[str, Any]:
    """本次任务的规则版本（读该任务目录里的规则快照；没有则返回空 dict）。

    执行器以**顶层模块**方式运行（``python backend/src/autoedit_runner_video_brain.py``），
    因此这里只 import 纯标准库的 ``skill_registry``，不引入带包相对导入的 ``studio_workflow``。
    旧任务（Phase B 之前）没有快照 → 不写版本字段，台账形状保持不变。
    """
    try:
        from skill_registry import read_workflow_snapshot, task_versions  # noqa: PLC0415
    except ImportError:  # 缺注册表模块时不影响成片交付
        return {}
    snapshot = read_workflow_snapshot(_task_dirs(task_id)["output"])
    if not snapshot:
        return {}
    versions = task_versions(snapshot)
    return {
        "workflow_version": versions.get("workflow_version"),
        "skill_versions": versions.get("skill_versions") or {},
        "workflow_snapshot_path": versions.get("snapshot_path"),
    }


def _register_project_clips(task_id: str, task: dict[str, Any]) -> None:
    """把本次 AUTO_EDIT 的成片登记进项目台账（Project → SourceVideo → Clip）。

    这是「一条 clip 一张卡」的写入口：首次登记时 `render_task_id` 就钉在该 clip 上，
    之后所有局部任务（封面/标题/字幕/时间线）只更新条目字段，不再新建卡片。
    登记失败不影响成片交付（best-effort）。
    """
    project_id = str((task.get("bridge") or {}).get("project_id") or "")
    if not project_id:
        return
    try:
        from studio_project_store import register_clips, register_source_video  # noqa: PLC0415

        register_source_video(project_id, path=str(task.get("raw_video_path") or "") or None)
        output_dir = _task_dirs(task_id)["output"]
        plan_path = output_dir / "clip_plan.json"
        if not plan_path.is_file():
            return
        plan = _read_json(plan_path)
        # 本次任务的规则版本：随 clip 一起登记，台账据此显示「这条成片用的规则版本」。
        versions = _workflow_versions(task_id)
        clips: list[dict[str, Any]] = []
        for item in plan.get("highlights") or []:
            if not isinstance(item, dict):
                continue
            clip_id = str(item.get("clip_id") or "")
            if not clip_id:
                continue
            cover_rel = f"output/{task_id}/{clip_id}/cover.jpg"
            cover_abs = TASKS_ROOT / cover_rel
            clips.append({
                "clip_id": clip_id,
                "title": str(item.get("cover_title") or item.get("topic") or ""),
                "topic": str(item.get("topic") or ""),
                "source_start": item.get("start"),
                "source_end": item.get("end"),
                "duration": round(float(item.get("end") or 0) - float(item.get("start") or 0) + 0.8, 3),
                "cover_path": cover_rel if cover_abs.is_file() else None,
                "cover_bytes": cover_abs.stat().st_size if cover_abs.is_file() else None,
                "cover_style_id": str(item.get("cover_style_id")
                                      or task.get("cover_style_id") or ""),
                # 质量闸门与例外标记：卡片/任务详情据此显示「降级产物」
                "plan_source": str(item.get("plan_source") or ""),
                "validated": bool(item.get("validated", True)),
                "needs_review": bool(item.get("needs_review")),
                "needs_review_reasons": list(item.get("needs_review_reasons") or []),
                "theme_type": str(item.get("theme_type") or ""),
                "duration_target": list(item.get("duration_target") or []),
                "duration_exception": bool(item.get("duration_exception")),
                "cover_copy_source": str(item.get("cover_copy_source") or ""),
                "cover_copy_degraded": bool(item.get("cover_copy_degraded")),
                **versions,
            })
        if clips:
            register_clips(project_id, task_id, clips)
            _log(f"{task_id}: 已登记 {len(clips)} 条成片到项目台账 {project_id}")
    except Exception as exc:  # noqa: BLE001 — 台账登记失败不能拖垮成片交付
        _log(f"{task_id}: 项目台账登记失败（不影响成片）：{exc!r}")


def _finish_task(task_id: str, task: dict[str, Any], view: dict[str, Any], created_at: str) -> None:
    dirs = _task_dirs(task_id)
    project_id = str((task.get("bridge") or {}).get("project_id") or "") or None
    vb_status = str(view.get("status"))
    counts = _export_artifacts(task_id, str(view["task_id"]), view, task)
    if vb_status in {"completed", "partial_failed"} and counts["copied"] > 0:
        _register_project_clips(task_id, task)
        stage = STAGE_ZH[vb_status]
        if vb_status == "partial_failed" or counts["failed"]:
            stage = f"部分切片渲染失败（成功 {counts['copied']} 条，失败 {counts['failed']} 条），成功成片已回传"
        _update_status(task_id, status="completed", stage=stage, progress=1.0,
                       created_at=created_at, project_id=project_id)
        os.replace(dirs["processing"], dirs["completed"])
        _log(f"{task_id}: 完成，回传 {counts['copied']} 条切片")
    else:
        step_errors = [str(step['error']) for step in view.get('steps', [])
                       if step.get('status') == 'failed' and step.get('error')]
        error = str(view.get("error") or '; '.join(step_errors) or f"video-brain 终态 {vb_status} 且无可用成片")
        _update_status(task_id, status="failed", stage=STAGE_ZH.get(vb_status, vb_status),
                       progress=1.0, created_at=created_at, error=error, project_id=project_id)
        os.replace(dirs["processing"], dirs["failed"])
        _log(f"{task_id}: 失败 — {error}")


def _process(task_id: str, task_file: Path, task: dict[str, Any]) -> None:
    dirs = _task_dirs(task_id)
    created_at = str(task.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z")
    bridge = task.setdefault("bridge", {})
    project_id = str(bridge.get("project_id") or "") or None
    vb_task_id = str(bridge.get("video_brain_task_id") or "")

    if not vb_task_id:
        _update_status(task_id, status="processing", stage="已接入 DSH 固化剪辑流程（video-brain）",
                       progress=0.02, created_at=created_at, project_id=project_id)
        try:
            vb_task_id = _start_video_brain_task(task_id, task)
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc)
            reason = _vb_error_zh(detail)
            _update_status(task_id, status="failed", stage="video-brain 建任务失败",
                           progress=1.0, created_at=created_at, project_id=project_id,
                           error=f"HTTP {exc.code}: {reason}" if reason != detail
                           else f"HTTP {exc.code}: {detail}")
            os.replace(dirs["processing"], dirs["failed"])
            # 失败行必须携带响应体 detail（如 E_DISK_SPACE），否则排障只见状态码不见原因
            _log(f"{task_id}: 建任务失败 HTTP {exc.code}: {reason}")
            return
        bridge["video_brain_task_id"] = vb_task_id
        bridge["runner"] = "dsh-video-brain"
        _atomic_json(task_file, task)  # 崩溃恢复依据
        _log(f"{task_id}: video-brain 任务 {vb_task_id}")

    started = time.time()
    last_stage = ""
    while not _shutdown:
        try:
            view = _request("GET", f"/v1/tasks/{vb_task_id}")
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            if time.time() - started > TASK_TIMEOUT_SECONDS:
                _update_status(task_id, status="failed", stage="video-brain 不可达超时",
                               progress=1.0, created_at=created_at, error=str(exc),
                               project_id=project_id)
                os.replace(dirs["processing"], dirs["failed"])
                return
            time.sleep(POLL_SECONDS)
            continue

        vb_status = str(view.get("status"))
        if vb_status in {"completed", "partial_failed", "failed", "cancelled"}:
            _finish_task(task_id, task, view, created_at)
            return
        stage, progress = _vb_step_stage(view)
        if stage != last_stage:
            _update_status(task_id, status="processing", stage=stage, progress=progress,
                           created_at=created_at, project_id=project_id)
            last_stage = stage
        if time.time() - started > TASK_TIMEOUT_SECONDS:
            try:
                _request("POST", f"/v1/tasks/{vb_task_id}/cancel")
            except urllib.error.URLError:
                pass
            _update_status(task_id, status="failed", stage="任务超时", progress=1.0,
                           created_at=created_at, project_id=project_id,
                           error=f"超过 {TASK_TIMEOUT_SECONDS:.0f}s 未完成")
            os.replace(dirs["processing"], dirs["failed"])
            return
        time.sleep(POLL_SECONDS)


def _handle_signal(signum: int, _frame: Any) -> None:
    global _shutdown
    _shutdown = True
    _log(f"收到信号 {signum}，本轮处理完退出")


def main() -> int:
    try:
        with process_lock(TASKS_ROOT / ".autoedit-runner.lock", blocking=False):
            return _main_loop()
    except BlockingIOError:
        _log("已有桥接执行器运行，本进程退出")
        return 0


def _main_loop() -> int:
    for directory in ("in", "processing", "completed", "failed", "status", "output"):
        (TASKS_ROOT / directory).mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    _log(f"执行器启动 tasks_root={TASKS_ROOT} video_brain={VIDEO_BRAIN_URL}")
    # 重启恢复：processing 里遗留的任务（含 vb_task_id）继续推进
    for leftover in sorted((TASKS_ROOT / "processing").glob("autoedit_*.json")):
        if _shutdown:
            break
        task_id = leftover.stem
        if TASK_ID_PATTERN.fullmatch(task_id):
            _log(f"恢复遗留任务 {task_id}")
            _process(task_id, leftover, _read_json(leftover))
    while not _shutdown:
        claimed = _claim_next_task()
        if claimed is None:
            time.sleep(IDLE_SECONDS)
            continue
        task_id, task_file, task = claimed
        _log(f"认领任务 {task_id}")
        try:
            _process(task_id, task_file, task)
        except Exception as exc:  # 兜底：任何异常都不能让执行器退出
            dirs = _task_dirs(task_id)
            try:
                _update_status(task_id, status="failed", stage="执行器内部错误", progress=1.0,
                               created_at=str(task.get("created_at") or ""), error=repr(exc))
                if task_file.is_file():
                    os.replace(task_file, dirs["failed"])
            except OSError:
                pass
            _log(f"{task_id}: 执行器异常 {exc!r}")
    _log("执行器退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
