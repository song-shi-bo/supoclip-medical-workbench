"""Studio worker：任务主流程、心跳、主循环与子进程隔离。"""

import asyncio
import inspect
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from dotenv import load_dotenv  # noqa: E402
from src.source_store import materialise_source as _source_materialise  # noqa: E402
from src.studio_runtime import process_lock, terminate_with_parent  # noqa: E402


from .config import (  # noqa: F401
    _base_dir,
    BACKEND_INTERNAL_URL,
    JOB_TIMEOUT_SECONDS,
    POLL_SECONDS,
    STUDIO_TASKS_ROOT,
    VIDEO_BRAIN_HOME,
    VIDEO_BRAIN_URL,
    WORKER_ID,
    WORKER_QUEUES,
    WORKER_ROLE_TAG,
    WORKER_SHARED_SECRET,
)
from .reporting import (  # noqa: F401
    _current_job,
    _current_job_id,
    _http,
    _log,
    _state,
)
from .staging import (  # noqa: F401
    _save_recovery,
    _guarded_upload_file,
    _job_owner_subject,
    _local_object_path,
    _sha256_file,
    _transcribe_recovery,
    materialise_source,
    stage_downloading,
)
from .stages import (  # noqa: F401
    _accepts_keyword,
    _call_stage_correcting,
    _call_stage_transcribing,
    _stage_metadata,
    stage_analyzing,
    stage_correcting,
    stage_editing,
    stage_rendering,
    stage_transcribing,
    stage_transcribing_cloud,
)
from .pipeline import (  # noqa: F401
    _COVER_LLM_FN,
    _analysis_cache,
    _analysis_keywords_for_cover,
    _assert_audio_stream,
    _auto_cover_enabled,
    _bind_clip_covers,
    _clip_window_segments,
    _cover_artifact_kinds,
    _cover_asset_missing,
    _cover_intro_enabled,
    _cover_topic_unit_from_analysis,
    _existing_published,
    _fenced_activation,
    _frozen_rules,
    _generation,
    _job_kind,
    _job_run_id,
    _knowledge_context_for,
    _load_refinement_for_render,
    _materialise_source_for_job,
    _persist_workflow_snapshot,
    _plan_cache,
    _refined_segments_for_cover,
    _rule_snapshot_id,
    _rules_version,
    _run_clip_cover_stage,
    _run_clip_planning_stage,
    _run_clip_refinement_stage,
    _run_clip_render_stage,
    _run_content_analysis_stage,
    _run_id,
    _run_reedit_replace,
    _spawn_speaker_labeling_backfill,
    _upload_transcript_artifact,
)


def run_transcribe_only(job: dict) -> None:
    """TRANSCRIBE_ONLY：提取音轨 → 转写 → 纠错 → transcript_ready（终态）。

    明确禁止：金句/关键词提取、剪辑规则读取、切片规划、渲染、封面、台账写入。
    """
    from src import studio_transcript_store

    job_id = job["job_id"]
    started = time.time()
    _state(job_id, "extracting_audio", "提取音轨与准备源片", 0.05)
    video_path = _materialise_source_for_job(job["object_key"], job_id)
    _assert_audio_stream(video_path)

    recovery_path,recovery,identity=_transcribe_recovery(job,video_path)
    if recovery.get("raw_segments") is not None:
        raw_segments,transcript_meta=recovery["raw_segments"],recovery["transcript_meta"]
    else:
        raw_segments,transcript_meta=_call_stage_transcribing(video_path, job.get("config") or {}, job.get("project_id"))
        recovery.update(raw_segments=raw_segments,transcript_meta=transcript_meta)
        _save_recovery(recovery_path,recovery,identity)
    corrected=recovery.get("corrected")
    if corrected is None:
        corrected=_call_stage_correcting(raw_segments, job_id, job.get("project_id"))
        recovery["corrected"]=corrected
        _save_recovery(recovery_path,recovery,identity)

    # 纠错结果已统一带 segment_id/start_ms/end_ms/raw_text/uncertain/correction_reason
    # （transcript_correction_llm._enrich 补齐）；这里只兜底补 raw_text，保证字段齐全。
    audit = list(corrected.get("correction_audit") or [])
    merged: list[dict] = []
    for index, seg in enumerate(corrected["asr_segments"]):
        item = dict(seg)
        if not str(item.get("raw_text", "")).strip():
            if index < len(audit):
                item["raw_text"] = str(audit[index].get("raw_text", "")).strip()
            elif index < len(raw_segments):
                item["raw_text"] = str(raw_segments[index].get("text", "")).strip()
        merged.append(item)

    summary = dict(corrected.get("correction_summary") or {})
    _stage_metadata("correction",summary)
    ref = recovery.get("transcript_ref")
    if ref is None:
        ref = studio_transcript_store.save_corrected_transcript(
            job["object_key"], job["project_id"],
            asr_segments=merged,
            raw_segments=list(raw_segments),
            correction_audit=audit,
            correction_summary=summary,
            correction_provider=str(summary.get("engine") or "dictionary"),
            language=str(transcript_meta.get("language", "zh")),
            words=list(transcript_meta.get("words", [])),
        )

        recovery["transcript_ref"]=ref
        _save_recovery(recovery_path,recovery,identity)

    _state(job_id, "uploading", "回传转写产物", 0.9)
    _upload_transcript_artifact(job_id, video_path, {
        "asr_segments": merged,
        "correction_audit": audit,
        "correction_summary": summary,
        "language": transcript_meta.get("language", "zh"),
        "words": list(transcript_meta.get("words", []))[:20000],
        "insights": None,
        "timeline": None,
        "auto_edit_task": None,
        "transcript_id": ref["transcript_id"],
        "transcript_version": ref["transcript_version"],
    })
    _state(job_id, "transcript_ready", "转写与纠错已完成，可点击顶部「开始自动剪辑」", 1.0)
    _log(f"job {job_id}（transcribe_only）完成，用时 {time.time() - started:.0f}s")


def run_auto_edit_only(job: dict) -> None:
    """AUTO_EDIT：读取既有纠错转写 → 分析 → 规划 → 渲染（第五步正式链）。

    前置条件：transcript 存储里必须已有有效纠错转写，否则显式失败；
    全程禁止调用 stage_transcribing / stage_correcting。

    分阶段（config.stop_after 仅用于测试/开发诊断，正式任务不带该配置）：
    * stop_after="analysis"（兼容旧 analysis_only=true）：分析 → ``analysis_ready``；
    * stop_after="plan"：分析 → 规划 → ``plan_ready``（内部诊断停点）；
    * 正式（无 stop_after）：分析 → 规划 → 渲染，全自动不等待确认，终态
      ``completed`` / ``completed_with_warnings`` / ``partial_succeeded`` /
      ``technical_failed``。

    恢复语义：分析/计划工件已持久化则直接复用（不重复调用模型、不重新规划、
    不重新 ASR/纠错）；已可解码的成片直接跳过。
    """
    from src import studio_transcript_store

    job_id = job["job_id"]
    started = time.time()
    config = job.get("config") or {}
    if str(config.get("replacement_mode") or "") == "delete_old_after_success":
        from src import reedit_replace
        if reedit_replace.activation_done(_job_run_id(job,job_id)):
            _run_reedit_replace(job, job_id, {})
            return
    transcript = studio_transcript_store.load_corrected_transcript(
        job["object_key"],
        transcript_id=config.get("transcript_id") or None,
        transcript_version=config.get("transcript_version") or None,
    )
    if transcript is None:
        raise RuntimeError("E_TRANSCRIPT_REQUIRED: 请先完成转写与纠错。")

    if str(config.get("replacement_mode") or "") == "delete_old_after_success":
        _run_reedit_replace(job, job_id, transcript)
        return

    stop_after = str(config.get("stop_after") or "").strip().lower()
    if not stop_after and config.get("analysis_only"):
        stop_after = "analysis"  # 第三步旧开关兼容
    if stop_after in {"analysis", "plan"}:
        # 恢复优先（第四步）：同一任务此前已持久化分析结果 → 直接复用继续规划，
        # 绝不重新分析、不重新 ASR/纠错、不因恢复额外调用模型；
        # 只有分析工件确实缺失或损坏（load 返回 None）才重新分析。
        from src import content_analysis

        knowledge_context = _knowledge_context_for(job, job_id, transcript)
        job["_knowledge_context"] = knowledge_context
        analysis_record = _analysis_cache(job,job_id,transcript,knowledge_context)
        resumed = analysis_record is not None
        if resumed:
            analysis_record["_resumed"] = True
            _state(job_id, "extracting_highlights",
                   "检测到已保存的内容分析结果，恢复继续（不重新分析、不重新调用模型）", 0.4)
            _log(f"job {job_id} 恢复：复用已保存分析 {analysis_record.get('analysis_artifact_path')}")
        else:
            analysis_record = _run_content_analysis_stage(job, job_id, transcript)
        if stop_after == "analysis":
            if not resumed:
                summary = "内容分析完成"
            else:
                summary = "内容分析已存在（恢复复用）"
            _state(job_id, "analysis_ready", f"{summary}；本阶段到此为止，未渲染。", 1.0)
            return
        plan_record = _run_clip_planning_stage(job, job_id, transcript, analysis_record)
        # 两步制（2026-11-05）：plan_ready 停点后移——规划（提示词A）+ 精修
        # （提示词B）都完成后才发射诊断停点（用户拍板）。
        _run_clip_refinement_stage(job, job_id, transcript, plan_record)
        _state(job_id, "plan_ready",
               "clip_plan.json + clip_refinement.json 已保存（两步完成；诊断停点，正式任务自动继续渲染）。",
               0.75)
        return

    # 正式 AUTO_EDIT（第五步）：分析 → 规划 → 渲染，全自动不等待确认。
    # 恢复语义：分析/计划工件已存在则直接复用（不重复调用模型、不重新规划）。
    from src import clip_planning
    from src import content_analysis

    knowledge_context = _knowledge_context_for(job, job_id, transcript)
    job["_knowledge_context"] = knowledge_context
    analysis_record = _analysis_cache(job,job_id,transcript,knowledge_context)
    if analysis_record is not None:
        analysis_record["_resumed"] = True
        _state(job_id, "extracting_highlights",
               "检测到已保存的内容分析结果，恢复继续（不重新分析、不重新调用模型）", 0.4)
    else:
        analysis_record = _run_content_analysis_stage(job, job_id, transcript)

    plan_record = _plan_cache(job,job_id,transcript,analysis_record)
    if plan_record is not None:
        _state(job_id, "planning", "检测到已保存的切片计划，恢复继续渲染（不重新规划）。", 0.7)
    else:
        plan_record = _run_clip_planning_stage(job, job_id, transcript, analysis_record)

    # 两步制（2026-11-05）：规划完成后立即逐条精修，产物随规划落盘；渲染不动。
    _run_clip_refinement_stage(job, job_id, transcript, plan_record)
    _run_clip_render_stage(job, job_id, transcript, plan_record)
    _log(f"job {job_id}（auto_edit）全部阶段完成，用时 {time.time() - started:.0f}s")


def run_legacy_full(job: dict) -> None:
    """legacy 全链（config 无 job_kind 的存量/重试任务）：行为与拆分前完全一致。"""
    job_id = job["job_id"]
    started = time.time()
    object_key = job["object_key"]
    project_id = job["project_id"]

    video_path = stage_downloading(object_key, job_id)
    raw_segments, transcript_meta = _call_stage_transcribing(video_path, job.get("config") or {}, project_id)
    corrected = _call_stage_correcting(raw_segments, job_id, project_id)
    insights = stage_analyzing(corrected["asr_segments"], job_id)
    timeline = stage_editing(video_path, raw_segments, corrected, job_id, project_id=project_id)
    rendered, auto_edit_task = stage_rendering(project_id, video_path, corrected["asr_segments"], insights, job_id, project_override=project_id)

    _upload_auto_edit_artifacts(job_id, video_path, {
        "asr_segments": corrected["asr_segments"],
        "correction_audit": corrected.get("correction_audit", []),
        "language": transcript_meta.get("language", "zh"),
        "words": transcript_meta.get("words", [])[:20000],
        "insights": insights,
        "timeline": timeline,
        "auto_edit_task": auto_edit_task,
    }, timeline, rendered)
    _state(job_id, "completed", "成片与转写产物已就绪", 1.0)
    _log(f"job {job_id}（legacy_full）完成，用时 {time.time() - started:.0f}s")


def _upload_auto_edit_artifacts(job_id: str, video_path: Path, transcript_payload: dict,
                                timeline: dict, rendered: list[dict]) -> None:
    """uploading 阶段：转写/时间线/成片/封面上传对象存储并登记 artifact。"""
    from src import r2

    _state(job_id, "uploading", "回传转写与成片产物", 0.95)
    _upload_transcript_artifact(job_id, video_path, transcript_payload)
    for artifact in (timeline.get("artifacts") or {}).values():
        artifact_path = Path(artifact) if isinstance(artifact, str) else None
        if artifact_path and artifact_path.is_file() and artifact_path.suffix in {".srt", ".edl", ".fcpxml", ".json"}:
            key = f"studio-artifacts/{_run_id(job_id)}/timeline/{artifact_path.name}"
            stored = _guarded_upload_file(artifact_path, key)
            _http("POST", f"{BACKEND_INTERNAL_URL}/internal/jobs/{job_id}/artifacts",
                  {"kind": {".srt": "srt", ".edl": "edl", ".fcpxml": "fcpxml"}.get(artifact_path.suffix, "timeline_file"),
                   "object_key": stored["object_key"], "size_bytes": stored["size"]})
    for item in rendered:
        video_key = f"studio-artifacts/{_run_id(job_id)}/{item['clip_id']}/clip_final.mp4"
        stored_video = _guarded_upload_file(item["video"], video_key)
        _http("POST", f"{BACKEND_INTERNAL_URL}/internal/jobs/{job_id}/artifacts",
              {"kind": "clip", "clip_id": item["clip_id"], "object_key": stored_video["object_key"],
               "size_bytes": stored_video["size"]})
        if item["cover"]:
            cover_key = f"studio-artifacts/{_run_id(job_id)}/{item['clip_id']}/cover.jpg"
            stored_cover = _guarded_upload_file(item["cover"], cover_key)
            _http("POST", f"{BACKEND_INTERNAL_URL}/internal/jobs/{job_id}/artifacts",
                  {"kind": "cover", "clip_id": item["clip_id"], "object_key": stored_cover["object_key"],
                   "size_bytes": stored_cover["size"]})



def _upload_reedit_clip_artifacts(job_id, aggregate):
    from src.capacity_guard import reserve_capacity,estimate_publish_peak
    from src import r2
    pending=[]
    for entry in aggregate.get("entries") or []:
        if entry.get("status") not in {"rendered","skipped_playable"}: continue
        path=Path(entry.get("path") or "")
        if not path.is_file(): raise RuntimeError("E_REEDIT_PUBLISH: 新切片产物缺失")
        for kind,candidate in (("clip",path),("srt",path.parent/"subtitles.srt"),("clip_warnings",path.parent/"warnings.json")):
            if candidate.is_file() and _existing_published(job_id,kind,entry["clip_id"],candidate) is None:
                pending.append(candidate)
    if not pending: return _upload_reedit_clip_artifacts_reserved(job_id,aggregate)
    with reserve_capacity("publish",estimate_publish_peak(pending),path=r2.local_path_for(f"studio-artifacts/{_run_id(job_id)}")):
        return _upload_reedit_clip_artifacts_reserved(job_id,aggregate)





def _upload_reedit_clip_artifacts_reserved(job_id,aggregate):
    from src import r2
    from src.clip_render import _ffprobe_playable
    published=[]
    for entry in aggregate.get("entries") or []:
        if entry.get("status") not in {"rendered","skipped_playable"}: continue
        clip_id=str(entry.get("clip_id") or "");path=Path(entry.get("path") or "")
        if not clip_id or not path.is_file(): raise RuntimeError("E_REEDIT_PUBLISH: 新切片产物缺失")
        ready=_existing_published(job_id,"clip",clip_id,path)
        if ready is None:
            digest=_sha256_file(path);size=path.stat().st_size
            key=f"studio-artifacts/{_run_id(job_id)}/{clip_id}/clip_final.mp4"
            stored=r2.upload_file(path,key);copied=r2.local_path_for(stored["object_key"])
            if not copied.is_file() or copied.stat().st_size!=size or _sha256_file(copied)!=digest:
                raise RuntimeError("E_REEDIT_PUBLISH: 发布副本大小或摘要不一致")
            ok,reason=_ffprobe_playable(copied)
            if not ok: raise RuntimeError(f"E_REEDIT_PUBLISH: 发布副本不可解码: {reason}")
            _http("POST",f"{BACKEND_INTERNAL_URL}/internal/jobs/{job_id}/artifacts",
                  {"kind":"clip","clip_id":clip_id,"object_key":stored["object_key"],"size_bytes":size,"sha256":digest})
            ready={"clip_id":clip_id,"path":str(copied),"object_key":stored["object_key"],"size_bytes":size,"sha256":digest}
        for kind,name in (("srt","subtitles.srt"),("clip_warnings","warnings.json")):
            sidecar=path.parent/name
            if not sidecar.is_file() or _existing_published(job_id,kind,clip_id,sidecar) is not None: continue
            stored=r2.upload_file(sidecar,f"studio-artifacts/{_run_id(job_id)}/{clip_id}/{name}")
            copied=r2.local_path_for(stored["object_key"]);digest=_sha256_file(sidecar)
            if not copied.is_file() or _sha256_file(copied)!=digest:
                raise RuntimeError("E_REEDIT_PUBLISH: 字幕/来源侧车发布校验失败")
            _http("POST",f"{BACKEND_INTERNAL_URL}/internal/jobs/{job_id}/artifacts",
                {"kind":kind,"clip_id":clip_id,"object_key":stored["object_key"],"size_bytes":stored["size"],"sha256":digest})
        published.append(ready)
    if not published: raise RuntimeError("E_REEDIT_PUBLISH: 没有任何可发布的切片产物")
    return published


def process_job(job: dict) -> None:
    job_id = job["job_id"]
    _current_job()["id"] = job_id
    _current_job()["lease_token"] = job.get("lease_token")
    _current_job()["run_id"] = _job_run_id(job,job_id)
    _current_job()["metadata"] = {}
    kind = _job_kind(job)
    if kind == "transcribe_only":
        run_transcribe_only(job)
    elif kind == "auto_edit":
        run_auto_edit_only(job)
    else:
        run_legacy_full(job)


def _heartbeat(stop: threading.Event) -> None:
    last_success = time.monotonic()
    while not stop.is_set():
        heartbeat_job = _current_job()["id"]
        try:
            _http("POST", f"{BACKEND_INTERNAL_URL}/internal/heartbeat",
                  {"job_id": heartbeat_job}, timeout=10)
            last_success = time.monotonic()
        except Exception as exc:
            _log(f"心跳失败: {type(exc).__name__}")
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 409 and heartbeat_job == _current_job()["id"]:
                _LEASE_LOST.set()
            elif time.monotonic() - last_success > 80:
                _LEASE_LOST.set()
        stop.wait(20)


def main() -> int:
    try:
        with process_lock(STUDIO_TASKS_ROOT / f".studio-worker{WORKER_ROLE_TAG}.lock", blocking=False):
            stop = threading.Event()
            heartbeat = threading.Thread(target=_heartbeat, args=(stop,), daemon=True)
            heartbeat.start()
            try:
                return _main_loop()
            finally:
                stop.set()
                heartbeat.join(timeout=12)
    except BlockingIOError:
        _log("已有 Studio worker 运行，本进程退出")
        return 0


def _main_loop() -> int:
    if not WORKER_SHARED_SECRET:
        _log("未配置 WORKER_SHARED_SECRET，执行器退出")
        return 1
    _log(f"执行器启动 backend={BACKEND_INTERNAL_URL} worker={WORKER_ID} queues={list(WORKER_QUEUES) or '全部'}")
    while True:
        try:
            payload = _http("POST", f"{BACKEND_INTERNAL_URL}/internal/jobs/claim",
                           {"queues": list(WORKER_QUEUES) or None}, timeout=30)
            job = payload.get("job")
        except Exception as exc:  # noqa: BLE001
            _log(f"认领请求失败（{exc}），{POLL_SECONDS}s 后重试")
            time.sleep(POLL_SECONDS)
            continue
        if not job:
            time.sleep(POLL_SECONDS)
            continue
        _log(f"认领任务 {job['job_id']} (object_key={job['object_key'][:60]})")
        try:
            _current_job()["id"] = job["job_id"]
            _current_job()["lease_token"] = job.get("lease_token")
            _current_job()["run_id"] = _job_run_id(job,job["job_id"])
            _LEASE_LOST.clear()
            run_isolated_job(job)
        except Exception as exc:  # noqa: BLE001
            _log(f"任务失败: {exc}")
            try:
                _state(job["job_id"], "failed", "执行失败", 1.0, str(exc)[:400])
            except Exception as report_error:
                _log(f"任务失败上报未提交: {type(report_error).__name__}")
        _current_job().update({"id":None,"lease_token":None,"run_id":None,"metadata":{}})


_LEASE_LOST = threading.Event()


def run_isolated_job(job: dict) -> None:
    fd, error_name = tempfile.mkstemp(prefix='studio-job-error-', dir=STUDIO_TASKS_ROOT)
    os.close(fd)
    error_file = Path(error_name)
    child = subprocess.Popen([sys.executable, '-m', 'src.studio_job_worker', '--job'],
        stdin=subprocess.PIPE, env=dict(os.environ, STUDIO_WORKER_ID=WORKER_ID,
                                      STUDIO_JOB_ERROR_FILE=error_name,
                                      STUDIO_WORKER_PARENT_PID=str(os.getpid())),
        start_new_session=True)
    started = time.monotonic()
    try:
        child.stdin.write(json.dumps(job).encode())
        child.stdin.close()
        while child.poll() is None:
            if _LEASE_LOST.is_set():
                raise RuntimeError('任务租约失效，执行已停止')
            if time.monotonic() - started > JOB_TIMEOUT_SECONDS:
                raise TimeoutError('任务超过总执行时间限制')
            time.sleep(1)
        if child.returncode:
            detail = error_file.read_text(encoding='utf-8').strip()
            raise RuntimeError(detail or f'任务子进程退出（{child.returncode}）')
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        error_file.unlink(missing_ok=True)



