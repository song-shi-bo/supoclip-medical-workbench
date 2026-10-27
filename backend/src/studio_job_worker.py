"""Studio Job 执行器：认领 R2/本地任务，按任务类型执行并回传产物.

三类任务（config.job_kind 分派，缺省 legacy_full 兼容存量）：
  transcribe_only（TRANSCRIBE_ONLY）
    claim → extracting_audio（准备源片+音轨校验）
          → transcribing（faster-whisper small/CPU/int8/VAD/zh/word_timestamps=True）
          → correcting（DSH 模型全文纠错，普通中文校对；不可用回退保守规整）
          → transcript_ready（终态：纠错转写入版本化存储 + artifact 回传）
    禁止：金句/关键词、剪辑规则、切片规划、渲染、封面、台账写入。
  auto_edit（AUTO_EDIT，前置=已有有效纠错转写）
    config.stop_after="analysis"（第三步，兼容旧 analysis_only=true）：
          claim → retrieving_rules → extracting_highlights（DSH 模型内容分析）
          → analysis_ready（终态：tasks/analysis/<id>.json，不渲染）
    config.stop_after="plan"（第四步默认）：
          claim → retrieving_rules → extracting_highlights（内容分析）
          → planning（完整切片规划：连续区间/柔性时长/warning 不拦截）
          → plan_ready（终态：tasks/plans/<id>/clip_plan.json，不渲染）
    完整链（无停住配置）：claim → retrieving_rules（读取规则/冻结快照）→ extracting_highlights（金句/关键词）
          → editing（时间线工程）→ rendering（video-brain，transcript_pre_corrected 跳过重复纠错）
          → uploading → completed / failed
  legacy_full（存量全链，行为与拆分前一致）
    claim → downloading → transcribing → correcting → analyzing → editing
          → rendering → uploading → completed / failed

运行前提：与后端共用 backend/.env（R2_*/WORKER_SHARED_SECRET/BACKEND_INTERNAL_URL/STUDIO_TASKS_ROOT）。
"""

from __future__ import annotations

import asyncio
import threading
import json
import os
import signal
import subprocess
import tempfile
import traceback
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv  # noqa: E402
from src.source_store import materialise_source as _source_materialise  # noqa: E402
from src.studio_runtime import process_lock, terminate_with_parent  # noqa: E402

load_dotenv(BASE_DIR / ".env")

WORKER_ID = os.getenv("STUDIO_WORKER_ID") or f"worker-{uuid.uuid4().hex[:8]}"
BACKEND_INTERNAL_URL = os.getenv("BACKEND_INTERNAL_URL", "http://127.0.0.1:8000/api").rstrip("/")
WORKER_SHARED_SECRET = os.getenv("WORKER_SHARED_SECRET", "")
POLL_SECONDS = float(os.getenv("WORKER_POLL_SECONDS", "5"))
JOB_TIMEOUT_SECONDS = float(os.getenv("WORKER_JOB_TIMEOUT_SECONDS", "3600"))
VIDEO_BRAIN_URL = os.getenv("VIDEO_BRAIN_URL", "http://127.0.0.1:8940").rstrip("/")
VIDEO_BRAIN_HOME = Path(os.getenv("VIDEO_BRAIN_HOME", "/domain/video-brain"))
STUDIO_TASKS_ROOT = Path(os.getenv("STUDIO_TASKS_ROOT", str(BASE_DIR / "tasks")))


def _log(message: str) -> None:
    print(f"[studio-worker {WORKER_ID} {time.strftime('%H:%M:%S')}] {message}", flush=True)


def _http(method: str, url: str, payload: dict | None = None, timeout: float = 60) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json", "X-Worker-Id": WORKER_ID,
                 "X-Worker-Secret": WORKER_SHARED_SECRET,
                 "X-Worker-Lease": str(_CURRENT_JOB.get("lease_token") or "")},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode() or "{}")


# ---------------------------------------------------------------------------
# 各阶段实现
# ---------------------------------------------------------------------------

def materialise_source(canonical, target):
    from src.capacity_guard import reserve_capacity
    canonical, target = Path(canonical), Path(target)
    if target.exists():
        return target
    # 先试零副本硬链接，确实需要复制时再登记完整新增峰值。
    with reserve_capacity("source_link", 64 * 1024, path=target.parent):
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(canonical, target)
            return target
        except FileExistsError:
            return target
        except OSError:
            pass
    with reserve_capacity("source_copy", canonical.stat().st_size + 64 * 1024, path=target.parent):
        return _source_materialise(canonical, target)


def _guarded_upload_file(path, object_key):
    from src import r2
    from src.capacity_guard import reserve_capacity, estimate_publish_peak
    with reserve_capacity("publish", estimate_publish_peak([path]), path=r2.local_path_for(object_key)):
        return r2.upload_file(path, object_key)


def _local_object_path(object_key: str) -> Path | None:
    """本地对象模式下，object_key 指向本机一个已存在的文件时返回该路径。

    ``local:`` 前缀是对象存储的本地回退驱动（未配置 R2），此时「下载」本质
    只是同盘搬运——历史实现走 ``copy_file``，于是同一份 3GB 源片在
    ``studio-uploads/`` 之外又多出一份完整物理副本。返回真实路径后由
    :func:`src.source_store.materialise_source` 走硬链接，零额外占用。

    R2 模式（真·网络下载）和本地对象文件缺失时返回 ``None``，调用方退回
    既有下载路径，保持失败语义不变。
    """

    from src import r2

    if not object_key.startswith(r2.LOCAL_SCHEME):
        return None
    candidate = r2.local_path_for(object_key)
    return candidate if candidate.is_file() else None


def stage_downloading(object_key: str, job_id: str) -> Path:
    from src import r2

    _state(job_id, "downloading", "下载源视频", 0.05)
    target = Path(os.getenv("TEMP_DIR", str(BASE_DIR / "temp"))) / "studio-jobs" / job_id / "source.mp4"
    canonical = _local_object_path(object_key)
    if canonical is not None:
        # 本地对象就是源片本身：硬链接进 job 目录，链路仍只有一份物理数据。
        materialise_source(canonical, target)
    else:
        # R2 模式必须真正落盘一份——这是整条链路里唯一必要的源片写入。
        r2.download_to_file(object_key, target)
    return target


def stage_transcribing(video_path: Path, config: dict) -> tuple[list[dict], dict]:
    from src.config import get_config
    from src.video_utils import transcribe_with_faster_whisper

    _state(_current_job_id(), "transcribing", "语音转写（faster-whisper small/CPU/int8/VAD/zh，词级时间戳）", 0.15)
    runtime_config = get_config()
    transcript = transcribe_with_faster_whisper(video_path, runtime_config)
    # Rendering runs in another process; release this worker's unused ASR model.
    import gc
    import ctypes
    from src.video_utils import _FASTER_WHISPER_MODEL_CACHE
    _FASTER_WHISPER_MODEL_CACHE.clear()
    gc.collect()
    try:
        ctypes.CDLL(None).malloc_trim(0)
    except (AttributeError, OSError):
        pass
    raw_segments = transcript.get("segments", [])
    words = transcript.get("words", [])
    return raw_segments, {"language": transcript.get("language", "zh"), "words": words, "segments": raw_segments}


def stage_correcting(raw_segments: list[dict], job_id: str) -> dict:
    """纠错：DSH 文本模型按完整上下文做普通中文校对（见 transcript_correction_llm）。

    * 只修错别字/同音字/漏字/重复字/标点/断句，不润色、不强制医学术语表；
    * 拿不准保留原文并标 uncertain；时间戳由原始分段复制，模型无权改动；
    * 模型未配置/单批失败 → 保守规整回退，engine/fallback_reasons 留痕。
    """
    from src.transcript_correction_llm import correct_segments_with_model

    def _progress(message: str) -> None:
        _state(_current_job_id(), "correcting", message, 0.35)

    return correct_segments_with_model([
        {"start": round(float(item["start"]), 3), "end": round(float(item["end"]), 3), "text": str(item["text"]).strip()}
        for item in raw_segments
    ], progress=_progress, job_id=job_id or _current_job_id())


def stage_analyzing(corrected_segments: list[dict], job_id: str) -> dict:
    import asyncio as _asyncio
    from src.studio_insights import extract_studio_insights

    _state(_current_job_id(), "analyzing", "金句与关键词提取", 0.45)
    return _asyncio.run(extract_studio_insights(corrected_segments))


def stage_editing(video_path: Path, raw_segments: list[dict], corrected: dict, job_id: str, *,
                  project_id: str | None = None) -> dict:
    from src.timeline_copilot import create_timeline_project
    from src.config import get_config

    _state(_current_job_id(), "editing", "生成时间线（EDL/SRT/FCPXML）", 0.5)
    return create_timeline_project(
        video_path=video_path,
        raw_segments=raw_segments,
        corrected_segments=corrected["asr_segments"],
        uploads_root=Path(get_config().temp_dir),
        project_id=project_id,
    )


def stage_rendering(project_id: str, video_path: Path, corrected_segments: list[dict],
                    insights: dict, job_id: str, *, project_override: str | None = None,
                    transcript_pre_corrected: bool = False) -> tuple[list[dict], dict]:
    """提交既有固定剪辑流程（video-brain）并等待成片，返回本地成片文件列表。

    * ``project_override``：AUTO_EDIT 用它把时间线工程/bridge/台账固定到前端传入的
      ``timeline_*`` 项目 id；legacy 不传，仍为 ``timeline_{job_id[:12]}``（行为不变）；
    * ``transcript_pre_corrected``：asr_segments 来自工作台已纠错转写存储，透传给
      video-brain 跳过重复纠错（AUTO_EDIT 全程不出现 transcribing/correcting 改写）。
    """
    from src.studio_auto_edit_bridge import submit_auto_edit_task, read_auto_edit_task
    from src.config import get_config

    _state(_current_job_id(), "rendering", "固定剪辑流程渲染中", 0.6)
    ledger_project = str(project_override or f"timeline_{job_id[:12]}")
    uploads_root = Path(get_config().temp_dir) / "studio-jobs" / job_id
    project_dir = uploads_root / "studio-projects" / ledger_project
    (project_dir / "raw").mkdir(parents=True, exist_ok=True)
    source_copy = project_dir / "raw" / "source.mp4"
    if video_path.resolve() != source_copy.resolve():
        # 同盘硬链接：渲染项目不再为同一份源片多占一份 3GB 物理副本。
        materialise_source(video_path, source_copy)
    (project_dir / "project.json").write_text(json.dumps({"project_id": ledger_project}, ensure_ascii=False), encoding="utf-8")

    # 统一项目入口（2026-10-19）：写任务 JSON 前先确保**真实用户级**项目记录合法
    # （真 root 下的 timeline 工程 + 台账），堵住 bridge 路径只经 job 隔离目录、
    # 由 sync_from_tasks 回填出孤儿台账的缺口（timeline_b677b1639a1c 即实例）。
    # incomplete → 任务显式失败（E_PROJECT_INCOMPLETE），绝不留下只有 clips.json
    # 的台账；已合法 → reused；历史孤儿（ledger_only）→ 报错指引迁移入口，
    # 不静默补建。禁止在本步触发 ASR/纠错/分析/渲染。
    from src.project_registry import ensure_project
    ensured = ensure_project(
        ledger_project, origin="bridge",
        source_path=video_path,
        raw_segments=insights.get("raw_segments") or None,
        corrected_segments=corrected_segments,
        uploads_root=Path(get_config().temp_dir),
    )
    if ensured["state"] not in {"created", "reused"}:
        raise ValueError(
            f"E_PROJECT_INCOMPLETE: 项目 {ledger_project} 记录不完整"
            f"（state={ensured['state']}, missing={ensured['missing']}，{ensured.get('detail') or ''}）；"
            "已停止自动剪辑，未创建成片台账。历史孤儿项目请走迁移入口重建工程。"
        )

    task = submit_auto_edit_task(
        project_id=ledger_project,
        uploads_root=uploads_root,
        asr_segments=corrected_segments,
        golden_sentences=insights.get("golden_sentences", []),
        keywords=insights.get("keywords", []),
        transcript_pre_corrected=transcript_pre_corrected,
    )
    task_id = task["task_id"]
    started = time.time()
    while time.time() - started < JOB_TIMEOUT_SECONDS:
        status_path = STUDIO_TASKS_ROOT / "status" / f"{task_id}.json"
        if status_path.is_file():
            status = json.loads(status_path.read_text(encoding="utf-8"))
            _state(_current_job_id(), "rendering",
                   f"{status.get('stage', status.get('status', ''))}",
                   0.6 + 0.3 * float(status.get("progress", 0)))
            if status.get("status") in {"completed", "failed"}:
                if status.get("status") != "completed":
                    raise RuntimeError(str(status.get("error") or "固定剪辑流程失败"))
                break
        time.sleep(5)
    else:
        raise TimeoutError("固定剪辑流程超时")

    output_dir = STUDIO_TASKS_ROOT / "output" / task_id
    rendered = []
    for clip_dir in sorted(output_dir.glob("clip-*")):
        final = clip_dir / "clip_final.mp4"
        cover = clip_dir / "cover.jpg"
        if final.is_file():
            rendered.append({"clip_id": clip_dir.name, "video": final, "cover": cover if cover.is_file() else None})
    if not rendered:
        raise RuntimeError("固定剪辑流程完成但没有可用成片")
    _ = VIDEO_BRAIN_URL, VIDEO_BRAIN_HOME  # 渲染由既有执行器/桥接完成，此处仅消费其产物
    return rendered, read_auto_edit_task(task_id)


# ---------------------------------------------------------------------------
# 状态上报
# ---------------------------------------------------------------------------

_CURRENT_JOB = {"id": None, "lease_token": None, "run_id": None, "metadata": {}}


def _current_job_id() -> str:
    return _CURRENT_JOB["id"] or ""


def _state(job_id: str, status: str, stage: str, progress: float, error: str | None = None) -> None:
    try:
        _http("POST", f"{BACKEND_INTERNAL_URL}/internal/jobs/{job_id}/state",
              {"status": status, "stage": stage, "progress": progress, "error": error,
               **(_CURRENT_JOB.get("metadata") or {})})
    except Exception as exc:
        raise RuntimeError(f"无法提交任务状态: {type(exc).__name__}") from exc


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

def _job_kind(job: dict) -> str:
    """任务类型：config.job_kind 显式声明；缺省 legacy_full（旧全链，行为不变）。"""
    config = job.get("config") or {}
    kind = str(config.get("job_kind") or "").strip()
    return kind if kind in {"transcribe_only", "auto_edit"} else "legacy_full"


def _materialise_source_for_job(object_key: str, job_id: str) -> Path:
    """把源片物化进本任务工作区（本地模式=硬链接零拷贝；R2 模式=真下载落盘）。

    只做搬运不上报状态：阶段状态由各执行器按自己的状态机显式发射
    （TRANSCRIBE_ONLY=extracting_audio，AUTO_EDIT/legacy=downloading）。
    """
    from src import r2

    target = Path(os.getenv("TEMP_DIR", str(BASE_DIR / "temp"))) / "studio-jobs" / job_id / "source.mp4"
    canonical = _local_object_path(object_key)
    if canonical is not None:
        materialise_source(canonical, target)
    else:
        r2.download_to_file(object_key, target)
    return target


def _assert_audio_stream(video_path: Path) -> None:
    """TRANSCRIBE_ONLY 前置门：没有音轨的素材在这里显式失败，不进 whisper。"""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True, timeout=300)
    if probe.returncode != 0 or not probe.stdout.strip():
        raise RuntimeError("E_NO_AUDIO_STREAM: 未检测到音轨，无法转写")


def _upload_transcript_artifact(job_id: str, video_path: Path, payload: dict) -> None:
    """transcript.json 上传对象存储并登记 artifact（前端统一从该 artifact 读取结果）。"""
    from src import r2

    transcript_file = video_path.parent / "transcript.json"
    transcript_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    stored = _guarded_upload_file(transcript_file, f"studio-artifacts/{_run_id(job_id)}/transcript.json")
    _http("POST", f"{BACKEND_INTERNAL_URL}/internal/jobs/{job_id}/artifacts",
          {"kind": "transcript_json", "object_key": stored["object_key"],
           "size_bytes": stored["size"]})


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
        raw_segments,transcript_meta=stage_transcribing(video_path,job.get("config") or {})
        recovery.update(raw_segments=raw_segments,transcript_meta=transcript_meta)
        _save_recovery(recovery_path,recovery,identity)
    corrected=recovery.get("corrected")
    if corrected is None:
        corrected=stage_correcting(raw_segments,job_id)
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


def _generation(job):
    return int(((job.get("config") or {}).get("_lifecycle") or {}).get("render_generation") or 0)


def _run_id(job_id):
    if _CURRENT_JOB.get("id")!=job_id: return job_id
    run=str(_CURRENT_JOB.get("run_id") or job_id)
    token=_CURRENT_JOB.get("lease_token")
    return run+"/attempt-"+str(token)[:12] if token else run


def _job_run_id(job,job_id=""):
    from src.studio_job_store import render_run_id
    return str(job.get("render_run_id") or render_run_id({"job_id":job_id,**job}))


def _frozen_rules(job):
    """当前调用前冻结完整实际manifest、规则与启用实现摘要，不抄旧cache来源。"""
    if "_rules_snapshot" in job:
        return job["_rules_snapshot"]
    from src import skill_registry, content_analysis, clip_planning
    try:
        manifest=skill_registry.load_manifest()
        source_hashes={}
        for skill in skill_registry.enabled_skills(manifest):
            source=Path(str(skill.get("source_path") or ""))
            if source.is_file(): source_hashes[str(source)]=_sha256_file(source)
        snapshot={"manifest":manifest,"rules":{k:v for k,v in manifest.items() if k.endswith("_rules")},
                  "enabled_source_sha256":source_hashes,"read_status":"verified"}
    except Exception as exc:
        snapshot={"manifest":{},"rules":{},"read_status":"unavailable","reason":type(exc).__name__}
    snapshot["analysis_implementation_sha256"]=_sha256_file(Path(content_analysis.__file__))
    snapshot["planning_implementation_sha256"]=_sha256_file(Path(clip_planning.__file__))
    # JSON表示冻结，tuple/list不会在落盘后改变identity。
    job["_rules_snapshot"]=json.loads(json.dumps(snapshot,ensure_ascii=False))
    return job["_rules_snapshot"]


def _persist_workflow_snapshot(job,plan_record=None):
    """展示工件只由本次已保存计划的冻结规则生成，绝不以latest补历史。"""
    from src import skill_registry,studio_workflow
    if plan_record is not None:
        frozen=((plan_record.get("source_provenance") or {}).get("rules_snapshot") or {}).get("external_snapshot") or {}
    else:
        frozen=job.get("_rules_snapshot") or {}
    manifest=frozen.get("manifest")
    if not isinstance(manifest,dict) or not manifest:
        return None  # 无来源旧计划保持未知，不能补造版本。
    run_id=_job_run_id(job)
    root=Path(os.getenv("STUDIO_TASKS_ROOT",str(BASE_DIR/"tasks")))
    target=studio_workflow.snapshot_dir(root,run_id)
    if target.resolve()!=root.resolve()/"output"/run_id:
        raise RuntimeError("E_RULE_SNAPSHOT_PATH: 规则快照目录非法")
    existing=skill_registry.read_workflow_snapshot(target)
    if existing is not None:
        sections=("transcription_rules","correction_rules","speaker_rules","highlight_selection_rules",
            "clip_planning_rules","correction_gate_rules","subtitle_rules","cover_rules","title_rules","render_rules","qa_rules")
        expected_rules={key:manifest.get(key) for key in sections}
        fields=("skill_id","version","checksum","priority","source_path")
        expected_skills=[{key:item.get(key) for key in fields} for item in skill_registry.enabled_skills(manifest)]
        actual_skills=existing.get("skills") or []
        if (existing.get("workflow_version")!=manifest.get("workflow_version") or
            existing.get("rules")!=expected_rules or actual_skills!=expected_skills):
            raise RuntimeError("E_RULE_SNAPSHOT_CONFLICT: 已有规则快照与本次冻结计划不符")
        return existing
    if (target/skill_registry.SNAPSHOT_FILENAME).exists():
        raise RuntimeError("E_RULE_SNAPSHOT_CONFLICT: 已有规则快照不可读")
    return skill_registry.build_workflow_snapshot(run_id,snapshot_dir=target,
        manifest=json.loads(json.dumps(manifest,ensure_ascii=False)))


def _rules_version(job):
    return _frozen_rules(job)["manifest"].get("workflow_version")


def _analysis_cache(job,job_id,transcript):
    from src import content_analysis as ca
    expected=ca.build_current_analysis_provenance(list(transcript.get("asr_segments") or []),
        transcript_version=transcript.get("transcript_version"),rules_snapshot=_frozen_rules(job))
    record=ca.load_analysis_record(job_id,expected_provenance=expected,require_verified=True)
    if record is not None and int(record.get("plan_generation") or 0)==_generation(job):
        _stage_metadata("analysis",record)
        return record
    if ((job.get("config") or {}).get("_lifecycle") or {}).get("retry_mode")=="resume_render" and (ca.analysis_root()/f"{job_id}.json").is_file():
        raise RuntimeError("E_CACHE_CONTEXT_CHANGED: 当前转写/模型/规则与已保存分析不符，请重新模型规划")
    return None


def _plan_cache(job,job_id,transcript,analysis):
    from src import clip_planning as cp
    expected=cp.build_current_plan_provenance(list(transcript.get("asr_segments") or []),analysis,
        transcript_version=transcript.get("transcript_version"),rule_snapshot_id=_rules_version(job),rules_snapshot=_frozen_rules(job))
    record=cp.load_plan_record(job_id,expected_provenance=expected,require_verified=True)
    if record is not None and int(record.get("plan_generation") or 0)==_generation(job):
        _stage_metadata("planning",record)
        return record
    if ((job.get("config") or {}).get("_lifecycle") or {}).get("retry_mode")=="resume_render" and (cp.plans_root()/job_id/"clip_plan.json").is_file():
        raise RuntimeError("E_CACHE_CONTEXT_CHANGED: 当前输入与已保存计划不符，请重新模型规划")
    return None


def _stage_metadata(stage,record):
    metadata=_CURRENT_JOB.setdefault("metadata",{})
    sources=metadata.setdefault("stage_sources",{k:"unknown" for k in ("correction","analysis","planning")})
    raw=str(record.get("plan_source") or record.get("engine") or "unknown")
    source={"llm":"llm","mixed":"mixed","heuristic_fallback":"rules","rules":"rules","dictionary":"rules"}.get(raw,"unknown")
    reasons=list(record.get("fallback_reasons") or record.get("degradations") or [])
    if stage=="correction":
        if record.get("llm_batches") is not None:
            source="mixed" if record["llm_batches"]>0 and record.get("fallback_batches",0)>0 else "llm" if record["llm_batches"]>0 else "rules"
        elif raw in {"local-conservative-rules","dictionary"}: source="rules"
        elif source=="unknown":
            source="mixed" if reasons and ("model" in raw or "llm" in raw) else "llm" if "model" in raw or "llm" in raw else "unknown"
    sources[stage]=source
    old=metadata.setdefault("degradation_reasons",[])
    for reason in reasons:
        text=str(reason)
        if text not in old: old.append(text)
    if source in {"rules","mixed"} and not reasons:
        text=f"{stage}:{source}"
        if text not in old: old.append(text)
    usages=dict((metadata.get("llm_usage") or {}).get("by_stage") or {})
    usages[stage]=record.get("llm_usage") or {"status":"unknown","total_tokens":None}
    known=all(value.get("status")=="known" and isinstance(value.get("total_tokens"),(int,float)) for value in usages.values())
    metadata["llm_usage"]={"status":"known" if known else "unknown",
        "total_tokens":sum(value["total_tokens"] for value in usages.values()) if known else None,"by_stage":usages}


def _existing_published(job_id,kind,clip_id,path):
    """仅已验证本generation发布对象可复用，避免成功文件/DB artifact重复。"""
    manifest=_http("GET",f"{BACKEND_INTERNAL_URL}/internal/jobs/{job_id}/artifacts")
    from src import r2, clip_render
    candidates=[item for item in manifest.get("artifacts") or [] if item.get("kind")==kind and item.get("clip_id")==clip_id]
    if not candidates: return None
    digest=_sha256_file(path)
    for item in candidates:
        if item.get("sha256")==digest:
            published=r2.local_path_for(item["object_key"])
            if published.is_file() and published.stat().st_size==path.stat().st_size and _sha256_file(published)==digest and (kind not in {"clip","clip_final"} or clip_render._ffprobe_playable(published)[0]):
                return {"clip_id":clip_id,"path":str(published),"object_key":item["object_key"],
                        "size_bytes":published.stat().st_size,"sha256":digest,"reused":True}
    return None


def _fenced_activation(job,job_id,plan,published):
    # 所有实际激活均走服务端DB行锁和leasefence，客户端没有文件台账旁路。
    response=_http("POST",f"{BACKEND_INTERNAL_URL}/internal/jobs/{job_id}/activate",
                   {"plan_record":plan,"published":published})
    if response.get("accepted") is not True:
        raise RuntimeError("E_LEASE_LOST: 新批次激活未被实际接受")
    return response


def _rule_snapshot_id() -> str | None:
    """当前规则清单版本（作为轻量 rule_snapshot_id 记入分析/规划记录）。"""
    try:
        import json as _json

        from src.skill_registry import manifest_path

        data = _json.loads(Path(manifest_path()).read_text(encoding="utf-8"))
        value = data.get("workflow_version")
        return str(value) if value else None
    except Exception:  # noqa: BLE001 — 规则标识失败不阻塞分析/规划
        return None


def _run_content_analysis_stage(job: dict, job_id: str, transcript: dict) -> dict:
    """第三步（2026-10-14）：AUTO_EDIT 的模型内容分析阶段。

    * 只读已保存的纠错转写（object_key/transcript_id/transcript_version 定位），
      不重新上传、不 ASR、不纠错（本函数不做任何转写/纠错调用）；
    * 一次结构化模型调用提取 keywords / golden_quotes / topic_units /
      candidate_intervals（content_analysis.analyze_content）；
    * 结果原子落盘 tasks/analysis/<job_id>.json + artifact 回传；
    * stop_after=analysis 时终态 ``analysis_ready``（显式停住）；stop_after=plan
      时返回记录继续进入规划阶段（第四步）。
    """
    from src import content_analysis
    from src import r2

    started = time.time()
    _state(job_id, "retrieving_rules", "读取剪辑规则（分析阶段只读纠错转写，不重新转写/纠错）", 0.1)
    corrected_segments = list(transcript.get("asr_segments") or [])
    if not corrected_segments:
        raise RuntimeError("E_TRANSCRIPT_EMPTY: 已纠错转写为空，请重新转写")

    def _progress(message: str) -> None:
        _state(job_id, "extracting_highlights", message, 0.3)

    _state(job_id, "extracting_highlights", "模型内容分析（关键词/金句/主题块/候选区间）", 0.3)
    expected=content_analysis.build_current_analysis_provenance(corrected_segments,
        transcript_version=transcript.get("transcript_version"),rules_snapshot=_frozen_rules(job))
    record = content_analysis.analyze_content(
        corrected_segments,
        transcript_version=transcript.get("transcript_version"),
        job_id=job_id, project_id=job.get("project_id"),
        progress=_progress, rules_snapshot=_frozen_rules(job),
    )
    record["rule_snapshot_id"] = _rules_version(job)
    record["plan_generation"] = _generation(job)
    _stage_metadata("analysis",record)
    if record.get("source_provenance") is not None and record["source_provenance"]!=expected:
        raise RuntimeError("E_CACHE_CONTEXT_CHANGED: 分析调用来源与预先冻结输入不一致")
    saved = content_analysis.save_analysis_record(job_id, record,provenance=expected)

    analysis_file = Path(saved["path"])
    stored = _guarded_upload_file(analysis_file, f"studio-artifacts/{_run_id(job_id)}/content_analysis.json")
    _http("POST", f"{BACKEND_INTERNAL_URL}/internal/jobs/{job_id}/artifacts",
          {"kind": "content_analysis", "object_key": stored["object_key"],
           "size_bytes": stored["size"]})

    summary = (f"内容分析完成（plan_source={record.get('plan_source')}，"
               f"金句 {len(record.get('golden_quotes') or [])} 条 / 主题块 "
               f"{len(record.get('topic_units') or [])} 个 / 候选区间 "
               f"{len(record.get('candidate_intervals') or [])} 个）")
    _log(f"job {job_id}（auto_edit）内容分析完成：{saved['path']}，"
         f"用时 {time.time() - started:.0f}s")
    _state(job_id, "extracting_highlights", f"{summary}", 0.4)
    return record


def _run_clip_planning_stage(job: dict, job_id: str, transcript: dict,
                             analysis_record: dict) -> dict:
    """第四步（2026-10-14）：完整内容切片规划——产出 clip_plan.json。

    * 输入只有：指定版本纠错转写分段 + 第三步分析产物四类（内存传递，不重读）；
    * 每条切片是连续源区间，start_ms/end_ms/duration_ms 由分段重算；
    * 一切质量/时长问题只记 warning，无 reject、不阻断、不等人工确认；
    * 模型失败自动重试一次，仍失败本地回退，保存当前最优计划；
    * 返回计划记录；``plan_ready`` 终态由调用方按 stop_after 决定是否发射。
    """
    from src import clip_planning
    from src import r2

    started = time.time()

    def _progress(message: str) -> None:
        _state(job_id, "planning", message, 0.6)

    _state(job_id, "planning", "模型切片规划（连续区间/柔性时长/只记 warning）", 0.5)
    analysis_ref = {
        "path": analysis_record.get("analysis_artifact_path"),
        "plan_source": analysis_record.get("plan_source"),
        "llm_calls": analysis_record.get("llm_calls"),
        "llm_duration_ms": analysis_record.get("llm_duration_ms"),
        "model": analysis_record.get("model"),
        "rule_snapshot_id": analysis_record.get("rule_snapshot_id"),
        "resumed": bool(analysis_record.get("_resumed")),
    }
    expected=clip_planning.build_current_plan_provenance(list(transcript.get("asr_segments") or []),analysis_record,
        transcript_version=transcript.get("transcript_version"),rule_snapshot_id=_rules_version(job),rules_snapshot=_frozen_rules(job))
    record = clip_planning.plan_clips(
        list(transcript.get("asr_segments") or []), analysis_record,
        transcript_version=transcript.get("transcript_version"),
        job_id=job_id, project_id=job.get("project_id"),
        progress=_progress,
        rule_snapshot_id=_rules_version(job),
        analysis_ref=analysis_ref, rules_snapshot=_frozen_rules(job),
    )
    record["plan_generation"]=_generation(job)
    _stage_metadata("planning",record)
    if record.get("source_provenance") is not None and record["source_provenance"]!=expected:
        raise RuntimeError("E_CACHE_CONTEXT_CHANGED: 规划调用来源与预先冻结输入不一致")
    saved = clip_planning.save_plan_record(job_id, record,provenance=expected)
    _persist_workflow_snapshot(job,record)

    plan_file = Path(saved["path"])
    stored = _guarded_upload_file(plan_file, f"studio-artifacts/{_run_id(job_id)}/clip_plan.json")
    _http("POST", f"{BACKEND_INTERNAL_URL}/internal/jobs/{job_id}/artifacts",
          {"kind": "clip_plan", "object_key": stored["object_key"],
           "size_bytes": stored["size"]})

    clips = record.get("clips") or []
    summary = (f"切片规划完成（plan_source={record.get('plan_source')}，"
               f"{len(clips)} 条切片："
               + "；".join(f"{c.get('topic')}[{c.get('duration_ms', 0) / 1000:.0f}s]" for c in clips[:4])
               + ("…" if len(clips) > 4 else "") + "）")
    _state(job_id, "planning", summary, 0.7)
    _log(f"job {job_id}（auto_edit/plan）切片规划完成：{saved['path']}，"
         f"用时 {time.time() - started:.0f}s")
    return record


# 封面文案 LLM 注入口（测试桩）：None = 生产默认（真实文本 LLM，llm_budget 记账）。
_COVER_LLM_FN = None


def _clip_window_segments(corrected_segments: list[dict], start_ms: int, end_ms: int) -> list[dict]:
    """落在切片时间窗内的纠错转写分段（封面文案证据；与字幕同一裁剪语义）。"""

    def _start(item: dict) -> float:
        return float(item.get("start_ms", (item.get("start") or 0.0) * 1000))

    def _end(item: dict) -> float:
        return float(item.get("end_ms", (item.get("end") or 0.0) * 1000))

    return [dict(item) for item in corrected_segments
            if str(item.get("text") or "").strip() and _end(item) > start_ms and _start(item) < end_ms]


def _analysis_keywords_for_cover(plan_record: dict) -> list[str]:
    """从已保存的内容分析产物里取 keywords（读不到就返回空，规则回退兜底）。"""
    path = str((plan_record.get("analysis_ref") or {}).get("path") or "")
    if not path:
        return []
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    keywords = document.get("keywords")
    return [str(k).strip() for k in keywords if str(k).strip()] if isinstance(keywords, list) else []


def _cover_artifact_kinds(base: Path) -> list[tuple[str, str]]:
    """封面四产物的 artifact 登记项（文件存在才登记）。"""
    return [(kind, filename) for kind, filename in (
        ("cover_image", "cover.jpg"), ("cover_intro", "cover_intro.mp4"),
        ("cover_copy", "cover_copy.json"), ("cover_layout", "cover_layout.json"))
        if (base / filename).is_file()]


def _run_clip_cover_stage(job: dict, job_id: str, plan_record: dict, transcript: dict,
                          aggregate: dict, source: Path, out_root: Path) -> dict[str, dict]:
    """每条切片发布前的固化封面步骤（realframe_v2：真实帧+文字叠加，纯本地）。

    * 成功：四产物落在切片目录，并把 ``clip_final`` 前 0.8s 替换为封面片头
      （复用 ``cover_replace.splice_intro`` 时长不变契约；重启恢复按台账 sha
      防二次拼接）；
    * 失败：该切片照常发布、无封面，只记 warning（只警告不拦截）；
    * 同 clip 已有封面且产物不完整/需要重制时，新工具内部 revision+1、旧封面
      转 ``.purge-trash`` 回收站。
    返回 ``{clip_id: {sha256, bytes, topic, hook, cover_revision}}``（仅成功项）。
    """
    from src import cover_realframe_v2, cover_replace

    keywords = _analysis_keywords_for_cover(plan_record)
    plan_clips = {str(item.get("clip_id")): item for item in plan_record.get("clips") or []}
    llm_fn = _COVER_LLM_FN          # 测试桩注入口；None = 生产走真实文本 LLM（一次/切片）
    results: dict[str, dict] = {}
    for entry in aggregate["entries"]:
        if entry.get("status") not in {"rendered", "skipped_playable"}:
            continue
        clip_id = str(entry["clip_id"])
        clip = plan_clips.get(clip_id) or {}
        base = out_root / "clips" / clip_id
        clip_final = base / "clip_final.mp4"
        if not clip_final.is_file():
            continue
        start_ms = int(clip.get("start_ms") or 0)
        end_ms = int(clip.get("end_ms") or 0)
        try:
            produced = cover_realframe_v2.build_cover(
                source, project_id=str(job["project_id"]), clip_id=clip_id,
                clip_start_ms=start_ms, clip_end_ms=end_ms,
                segments=_clip_window_segments(list(transcript.get("asr_segments") or []),
                                               start_ms, end_ms),
                out_dir=base, analysis_keywords=keywords,
                topic_hint=str(clip.get("topic") or "") or None, job_id=job_id,
                llm_fn=llm_fn)
        except Exception as exc:  # noqa: BLE001 — 封面失败不是切片失败
            aggregate.setdefault("warnings", []).append(
                f"封面生成失败（{clip_id}，切片照常发布）: {type(exc).__name__}: {exc}"[:200])
            _log(f"job {job_id}（auto_edit/cover）{clip_id} 封面失败: {exc}")
            continue
        try:
            if not cover_realframe_v2.intro_already_applied(base, clip_final):
                intro_s = cover_replace.style_intro_duration("realframe_v2")
                fade_s = cover_replace.style_fade_in("realframe_v2")
                before = cover_replace.probe_streams(clip_final)
                plan = cover_replace.plan_intro_split(
                    before["fps"], before["frames"], cover_replace.keyframe_list(clip_final),
                    max(1, int(round(intro_s * before["fps"]))))
                if plan["mode"] == "exact_copy":
                    # 既有契约路径：片头边界恰在关键帧上 → 全程流拷贝无损
                    cover_replace.splice_intro(clip_final, base / "cover.jpg",
                                               intro_duration_s=intro_s, fade_in_s=fade_s)
                else:
                    # staged 成片 0.8s 处无关键帧（单次编码）→ 本地等长重编码回退
                    cover_realframe_v2.splice_intro_reencode(
                        clip_final, base / "cover.jpg",
                        intro_duration_s=intro_s, fade_in_s=fade_s)
            cover_realframe_v2.record_intro_applied(
                base, int(produced["cover_revision"]), _sha256_file(clip_final))
        except Exception as exc:  # noqa: BLE001 — 片头拼接失败同样只警告
            aggregate.setdefault("warnings", []).append(
                f"封面片头拼接失败（{clip_id}，切片照常发布无片头）: {exc}"[:200])
            _log(f"job {job_id}（auto_edit/cover）{clip_id} 片头拼接失败: {exc}")
        final_copy = produced.get("cover_copy") or {}
        results[clip_id] = {
            "sha256": (produced.get("products") or {}).get("cover.jpg", {}).get("sha256"),
            "bytes": (produced.get("products") or {}).get("cover.jpg", {}).get("bytes"),
            "cover_revision": produced.get("cover_revision"),
            "topic": final_copy.get("topic"),
            "hook": final_copy.get("hook"),
            "copy_source": produced.get("copy_source"),
        }
    return results


def _bind_clip_covers(job: dict, job_id: str, cover_results: dict[str, dict],
                      aggregate: dict) -> None:
    """激活成功后把封面绑定到卡片 cover 字段（复用既有 bump_cover 机制；幂等）。

    sha 与台账当前版本一致时不叠加修订（重复执行版本号不变）；项目台账不可用
    （未注册/存储异常）只记 warning，不影响已发布产物。
    """
    if not cover_results:
        return
    from src import studio_project_store as store
    from src.studio_clip_locator import clip_asset

    project_id = str(job["project_id"])
    run_dir = _job_run_id(job, job_id)
    for clip_id, info in cover_results.items():
        try:
            entry = store.entry_for(project_id, clip_id)
            current = entry.get("cover") if isinstance(entry.get("cover"), dict) else {}
            if current.get("sha256") and current.get("sha256") == info.get("sha256"):
                continue  # 幂等：同一封面已绑定，不叠加修订
            cover_rel = clip_asset(store.TASKS_ROOT, run_dir, clip_id, "cover.jpg",
                                   required=False).relative_to(store.TASKS_ROOT.resolve()).as_posix()
            hook_text = str(info.get("hook") or "")
            store.bump_cover(
                project_id, clip_id, task_id=job_id, style_id="realframe_v2",
                cover_path=cover_rel, sha256=info.get("sha256"),
                bytes_=info.get("bytes"), intro_burned=True, kind="auto_edit",
                title=str(info.get("topic") or "") or None,
                hook=hook_text or None)
        except Exception as exc:  # noqa: BLE001 — 卡片绑定失败不阻塞发布
            aggregate.setdefault("warnings", []).append(
                f"封面卡片绑定失败（{clip_id}）: {type(exc).__name__}: {exc}"[:200])
            _log(f"job {job_id}（auto_edit/cover）{clip_id} 卡片绑定失败: {exc}")


def _run_clip_render_stage(job: dict, job_id: str, transcript: dict,
                           plan_record: dict) -> None:
    """第五步（2026-10-14）：clip_plan → 本地 FFmpeg 渲染，生成多个视频成品。

    * 渲染输入只有：clip_id / start_ms / end_ms（来自 clip_plan，不改动）+
      指定版本纠错转写 + rule_snapshot_id；源片本地硬链接（不重新上传/下载）；
    * 每条切片独立渲染、独立隔离：真实技术故障记 technical_error 并继续其余；
    * 幂等：已存在且可解码的 clip_final.mp4 直接跳过（worker 重启恢复）；
    * 封面固化（2026-10-18）：每条切片发布前自动生成 realframe_v2 封面
      （真实视频帧+文字叠加，无 AI 图片生成）并把 clip_final 前 0.8s 替换为
      封面片头；四产物随切片登记 artifact、卡片 cover 字段绑定；封面失败
      只记 warning，该切片照常发布；
    * 无任何质量拦截；终态：completed / completed_with_warnings /
      partial_succeeded / technical_failed（全部切片技术故障时）。
    """
    from src import clip_render
    from src import r2

    started = time.time()
    _persist_workflow_snapshot(job,plan_record)
    _state(job_id, "rendering", "准备源片（本地硬链接，不重新上传/下载）", 0.7)
    source = _materialise_source_for_job(job["object_key"], job_id)
    out_root = Path(os.getenv("STUDIO_TASKS_ROOT",
                              str(BASE_DIR / "tasks"))) / "output" / _job_run_id(job,job_id)

    def _progress(message: str) -> None:
        _state(job_id, "rendering", message, 0.8)

    aggregate = clip_render.render_plan(
        plan_record, source=Path(source), out_root=out_root,
        corrected_segments=list(transcript.get("asr_segments") or []),
        transcript_ref={"transcript_id": transcript.get("transcript_id"),
                        "transcript_version": transcript.get("transcript_version")},
        rule_snapshot_id=plan_record.get("rule_snapshot_id") or _rule_snapshot_id(),
        progress=_progress,
    )

    # 封面固化（2026-10-18）：每条切片发布前自动生成 realframe_v2 封面并替换片头，
    # 失败只记 warning（aggregate.warnings），绝不拦截切片发布。
    _state(job_id, "rendering", "生成固化封面（realframe_v2：真实帧+文字叠加）并替换片头", 0.85)
    cover_results = _run_clip_cover_stage(job, job_id, plan_record, transcript,
                                          aggregate, Path(source), out_root)

    # 每条成片/警告登记 artifact（本地模式由后端回源文件，前端直接可播/可下载）
    published_clips=[]
    for entry in aggregate["entries"]:
        if entry.get("status") not in {"rendered", "skipped_playable"}:
            continue
        clip_id = entry["clip_id"]
        base = out_root / "clips" / clip_id
        kinds = [("clip_final", "clip_final.mp4"), ("clip_warnings", "warnings.json"), ("srt", "subtitles.srt")]
        kinds += _cover_artifact_kinds(base)
        for kind, filename in kinds:
            path = base / filename
            if not path.is_file():
                continue
            reused=_existing_published(job_id,kind,clip_id,path)
            if reused is not None:
                if kind=="clip_final": published_clips.append(reused)
                continue
            stored = _guarded_upload_file(path, f"studio-artifacts/{_run_id(job_id)}/{clip_id}/{filename}")
            _http("POST", f"{BACKEND_INTERNAL_URL}/internal/jobs/{job_id}/artifacts",
                  {"kind": kind, "clip_id": clip_id, "object_key": stored["object_key"],
                   "size_bytes": stored["size"], "sha256": _sha256_file(path)})
            if kind=="clip_final": published_clips.append({"clip_id":clip_id,"object_key":stored["object_key"]})

    if published_clips:
        _fenced_activation(job,job_id,plan_record,published_clips)
    if cover_results:
        # 激活成功后切片才在项目台账注册；此时把封面绑定到卡片 cover 字段（幂等）。
        _bind_clip_covers(job,job_id,cover_results,aggregate)
    counts = aggregate["counts"]
    final_status = aggregate["job_status"]
    if aggregate.get("warnings") and final_status == "completed":
        final_status = "completed_with_warnings"
    failed = [e for e in aggregate["entries"] if e["status"] == "technical_error"]
    if failed:
        detail = "；".join(f"{e['clip_id']}: {e.get('technical_error', '')[:60]}" for e in failed[:3])
        summary = (f"渲染完成：成功 {counts['succeeded']}/{counts['total']}，"
                   f"技术故障 {counts['technical_error']}（{detail}）")
    else:
        summary = (f"渲染完成：成功 {counts['succeeded']}/{counts['total']}"
                   f"（跳过已存在 {counts['skipped_playable']}）")
    if aggregate.get("warnings"):
        summary += f"；警告 {len(aggregate['warnings'])} 条（含封面降级）"
    _state(job_id, final_status, summary, 1.0)
    _log(f"job {job_id}（auto_edit/render）{final_status}：{counts}，用时 {time.time() - started:.0f}s")


def _run_reedit_replace(job: dict, job_id: str, transcript: dict) -> None:
    """重新剪辑并替换旧切片（2026-10-15，replacement_mode=delete_old_after_success）。

    复用源片与指定版本纠错转写（绝不重新上传/ASR/纠错）；重新执行内容分析、
    切片规划与渲染到独立 staging 目录；至少一条新切片可解码后才激活新批次并
    按激活前落盘的精确清单清除旧切片（两阶段回收站，报告含释放字节）。
    恢复：分析/计划工件与已可解码成片自动复用；已激活未清理只继续清理。
    """
    from src import clip_planning
    from src import clip_render
    from src import content_analysis
    from src import reedit_replace

    project_id = job["project_id"]
    run_id = _job_run_id(job,job_id)
    if reedit_replace.activation_done(run_id):
        _state(job_id, "purging_old_clips", "恢复已激活批次：仅继续清理旧实例", 0.92)
        def delete_old_artifacts(ids):
            import asyncio
            from src.studio_job_store import delete_artifacts_for_jobs
            asyncio.run(delete_artifacts_for_jobs(ids))
        report = reedit_replace.retry_cleanup(project_id, run_id, delete_artifacts_fn=delete_old_artifacts)
        status = "completed_with_cleanup_warning" if report["failures"] else "completed"
        _state(job_id, status, "已激活批次清理恢复完成", 1.0)
        return
    _state(job_id, "staging_run", "重新剪辑：复用源片与转写，新批次成功后替换旧切片", 0.02)
    _state(job_id, "staging_run", "创建独立 staging 目录（不触碰旧切片目录）", 0.05)
    reedit_replace.staging_root(run_id).mkdir(parents=True, exist_ok=True)
    manifest = reedit_replace.load_old_manifest(run_id)
    if manifest is None:
        manifest = reedit_replace.build_old_manifest(project_id, run_id)

    analysis_record = _analysis_cache(job,job_id,transcript)
    if analysis_record is not None:
        analysis_record["_resumed"] = True
        _state(job_id, "extracting_highlights",
               "检测到已保存的内容分析结果，恢复继续（不重新分析、不重新调用模型）", 0.2)
    else:
        analysis_record = _run_content_analysis_stage(job, job_id, transcript)

    plan_record = _plan_cache(job,job_id,transcript,analysis_record)
    if plan_record is not None:
        _state(job_id, "planning", "检测到已保存的切片计划，恢复继续渲染（不重新规划）。", 0.4)
    else:
        plan_record = _run_clip_planning_stage(job, job_id, transcript, analysis_record)

    _persist_workflow_snapshot(job,plan_record)
    _state(job_id, "rendering", "渲染新切片到 staging 目录", 0.5)
    source = _materialise_source_for_job(job["object_key"], job_id)
    aggregate = clip_render.render_plan(
        plan_record, source=Path(source), out_root=reedit_replace.staging_root(run_id),
        corrected_segments=list(transcript.get("asr_segments") or []),
        transcript_ref={"transcript_id": transcript.get("transcript_id"),
                        "transcript_version": transcript.get("transcript_version")},
        rule_snapshot_id=plan_record.get("rule_snapshot_id") or _rule_snapshot_id(),
        progress=lambda message: _state(job_id, "rendering", message, 0.6),
    )

    _state(job_id, "verifying_outputs", "校验新切片产物（至少一条可解码、可播放）", 0.75)
    playable, total = reedit_replace.verify_new_outputs(run_id)
    if playable < 1:
        cleanup = reedit_replace.discard_staging(run_id)
        detail = "；".join(e.get("technical_error", "")[:60]
                           for e in aggregate.get("entries", []) if e.get("technical_error"))
        _state(job_id, "technical_failed",
               f"新批次渲染失败（{total} 条中 {playable} 条可播放），staging 已清理"
               f"（残留 {len(cleanup.get('failures', []))} 项），旧切片全部保留。{detail}", 1.0)
        _log(f"job {job_id}（reedit）新批次失败，旧切片保留")
        return

    # Step 3 发布契约（2026-10-16 硬化）：先发布 + 验证副本可读，**全部成功**才允许
    # 激活并替换。发布失败 → 终止替换：旧切片原样保留、staging 保留可重试，
    # 绝不带病激活（否则旧成片已删而新成片无 artifact，前端必然无画面）。
    _state(job_id, "uploading_reedit_artifacts", "发布新切片产物（复制并登记任务 artifact）", 0.8)
    try:
        published = _upload_reedit_clip_artifacts(job_id, aggregate)
    except Exception as exc:  # noqa: BLE001 — 发布失败必须终止替换
        detail = str(exc)[:200]
        _state(job_id, "technical_failed",
               f"新切片发布失败，本次替换已中止：旧切片全部保留，新切片已保留在 staging，"
               f"可重试重新剪辑。原因：{detail}", 1.0)
        _log(f"job {job_id}（reedit）发布失败，替换中止：{detail}")
        return
    _log(f"job {job_id}（reedit）已发布 {len(published)} 条切片产物并验证可读")

    _state(job_id, "activating_run", "新批次通过校验，激活进项目素材区", 0.85)
    try:
        _fenced_activation(job,job_id,plan_record,published)
    except reedit_replace.ReeditError as exc:
        _state(job_id, "technical_failed", f"{exc.code}: {exc}；旧批次保持当前状态", 1.0)
        return

    _state(job_id, "purging_old_clips", "按激活前清单清除旧切片（两阶段回收站）", 0.92)

    def _delete_artifacts(task_ids: list[str]) -> None:
        import asyncio as _asyncio
        from src.studio_job_store import delete_artifacts_for_jobs
        _asyncio.run(delete_artifacts_for_jobs(task_ids))

    report = reedit_replace.purge_old_clips(manifest, delete_artifacts_fn=_delete_artifacts)
    if report["failures"]:
        _state(job_id, "completed_with_cleanup_warning",
               f"新切片已生效；旧切片清理未完成（{len(report['failures'])} 项），可重试清理。"
               f"释放 {report['bytes_freed'] / 1024 / 1024:.1f} MB", 1.0)
    else:
        tail = ("，" + "；".join(str(w) for w in (plan_record.get("warnings") or [])[:2])
                if plan_record.get("warnings") else "")
        final = "completed_with_warnings" if aggregate["job_status"] != "completed" or tail else "completed"
        _state(job_id, final,
               f"重新剪辑完成：新切片 {aggregate['counts'].get('succeeded', 0)} 条已发布并替换旧切片，"
               f"删除 {report['clips_deleted']} 条旧切片 / 释放 "
               f"{report['bytes_freed'] / 1024 / 1024:.1f} MB{tail}", 1.0)
    _log(f"job {job_id}（reedit）替换完成：{json.dumps(report, ensure_ascii=False)[:200]}")


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

        analysis_record = _analysis_cache(job,job_id,transcript)
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
        _state(job_id, "plan_ready", "clip_plan.json 已保存（诊断停点；正式任务自动继续渲染）。", 0.75)
        return

    # 正式 AUTO_EDIT（第五步）：分析 → 规划 → 渲染，全自动不等待确认。
    # 恢复语义：分析/计划工件已存在则直接复用（不重复调用模型、不重新规划）。
    from src import clip_planning
    from src import content_analysis

    analysis_record = _analysis_cache(job,job_id,transcript)
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

    _run_clip_render_stage(job, job_id, transcript, plan_record)
    _log(f"job {job_id}（auto_edit）全部阶段完成，用时 {time.time() - started:.0f}s")


def run_legacy_full(job: dict) -> None:
    """legacy 全链（config 无 job_kind 的存量/重试任务）：行为与拆分前完全一致。"""
    job_id = job["job_id"]
    started = time.time()
    object_key = job["object_key"]
    project_id = job["project_id"]

    video_path = stage_downloading(object_key, job_id)
    raw_segments, transcript_meta = stage_transcribing(video_path, job.get("config") or {})
    corrected = stage_correcting(raw_segments, job_id)
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


def _sha256_file(path: Path) -> str:
    """流式计算文件 SHA-256（发布登记用；1MB 分块，长视频安全）。"""
    import hashlib

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


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
    _CURRENT_JOB["id"] = job_id
    _CURRENT_JOB["lease_token"] = job.get("lease_token")
    _CURRENT_JOB["run_id"] = _job_run_id(job,job_id)
    _CURRENT_JOB["metadata"] = {}
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
        heartbeat_job = _CURRENT_JOB["id"]
        try:
            _http("POST", f"{BACKEND_INTERNAL_URL}/internal/heartbeat",
                  {"job_id": heartbeat_job}, timeout=10)
            last_success = time.monotonic()
        except Exception as exc:
            _log(f"心跳失败: {type(exc).__name__}")
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 409 and heartbeat_job == _CURRENT_JOB["id"]:
                _LEASE_LOST.set()
            elif time.monotonic() - last_success > 80:
                _LEASE_LOST.set()
        stop.wait(20)


def main() -> int:
    try:
        with process_lock(STUDIO_TASKS_ROOT / ".studio-worker.lock", blocking=False):
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
    _log(f"执行器启动 backend={BACKEND_INTERNAL_URL} worker={WORKER_ID}")
    while True:
        try:
            payload = _http("POST", f"{BACKEND_INTERNAL_URL}/internal/jobs/claim", {}, timeout=30)
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
            _CURRENT_JOB["id"] = job["job_id"]
            _CURRENT_JOB["lease_token"] = job.get("lease_token")
            _CURRENT_JOB["run_id"] = _job_run_id(job,job["job_id"])
            _LEASE_LOST.clear()
            run_isolated_job(job)
        except Exception as exc:  # noqa: BLE001
            _log(f"任务失败: {exc}")
            try:
                _state(job["job_id"], "failed", "执行失败", 1.0, str(exc)[:400])
            except Exception as report_error:
                _log(f"任务失败上报未提交: {type(report_error).__name__}")
        _CURRENT_JOB.update({"id":None,"lease_token":None,"run_id":None,"metadata":{}})


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



def _transcribe_recovery(job,source):
    """转写阶段完成点持久保存，回传失败恢复不再执行已成功ASR/纠错。"""
    import hashlib
    config={k:v for k,v in (job.get("config") or {}).items() if not k.startswith("_")}
    stat=source.stat()
    identity={"object_key":job["object_key"],"source":[stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns],
        "config":config,"worker_sha256":_sha256_file(Path(__file__))}
    path=Path(os.getenv("STUDIO_TASKS_ROOT",str(BASE_DIR/"tasks")))/"job-recovery"/job["job_id"]/"transcribe.json"
    data={}
    if path.is_file():
        try: data=json.loads(path.read_text(encoding="utf-8"))
        except (ValueError,OSError): data={}
    if data.get("identity")!=identity: data={}
    return path,data,identity


def _save_recovery(path,data,identity):
    import uuid
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    try:
        temporary.write_text(json.dumps({**data,"identity":identity},ensure_ascii=False),encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    if sys.argv[1:] == ['--job']:
        terminate_with_parent(int(os.environ['STUDIO_WORKER_PARENT_PID']))
        try:
            process_job(json.load(sys.stdin))
        except Exception as exc:
            error_file = os.getenv('STUDIO_JOB_ERROR_FILE')
            if error_file:
                Path(error_file).write_text(str(exc)[:400], encoding='utf-8')
            traceback.print_exc()
            sys.exit(1)
    else:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        sys.exit(main())

