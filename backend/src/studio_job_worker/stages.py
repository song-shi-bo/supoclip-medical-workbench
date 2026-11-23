"""Studio worker：五阶段实现（downloading/transcribing/correcting/analyzing/editing/rendering）。"""

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
    _guarded_upload_file,
    _job_owner_subject,
    _local_object_path,
    _sha256_file,
    _transcribe_recovery,
    materialise_source,
    stage_downloading,
)


def stage_transcribing(video_path: Path, config: dict, project_id: str | None = None) -> tuple[list[dict], dict]:
    from src.config import get_config
    from src.video_utils import transcribe_with_faster_whisper

    # 云端转写分支（火山豆包 ASR 录音文件识别 2.0）：TRANSCRIPTION_PROVIDER=volc_cloud 时启用；
    # 默认仍是本地 faster-whisper。云端不受本机 3GB 内存 cgroup 限制，准确率更高。
    if (getattr(config, "transcription_provider", "") or "") == "volc_cloud":
        return stage_transcribing_cloud(video_path, project_id)

    hotwords = None
    try:
        from src.project_glossary import hotwords_string
        hotwords = hotwords_string(project_id or "")
    except Exception:  # noqa: BLE001 — 词表故障绝不影响转写主链路
        hotwords = None
    _state(_current_job_id(), "transcribing",
           "语音转写（faster-whisper small/CPU/int8/VAD/zh，词级时间戳）" + ("，含项目热词" if hotwords else ""), 0.15)
    runtime_config = get_config()
    transcript = transcribe_with_faster_whisper(video_path, runtime_config, hotwords=hotwords)
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


def _accepts_keyword(callable_obj, keyword: str) -> bool:
    """Keep compatibility with legacy stage replacements used by tests/plugins."""
    return any(
        parameter.name == keyword or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in inspect.signature(callable_obj).parameters.values()
    )


def _call_stage_transcribing(video_path: Path, config: dict,
                             project_id: str | None) -> tuple[list[dict], dict]:
    if _accepts_keyword(stage_transcribing, "project_id"):
        return stage_transcribing(video_path, config, project_id=project_id)
    return stage_transcribing(video_path, config)


def stage_transcribing_cloud(video_path: Path, project_id: str | None = None) -> tuple[list[dict], dict]:
    """云端转写（豆包 ASR 录音文件识别 2.0）：提音频 → 经 95 投递 → 公网 URL → 提交任务 → 轮询。

    前置：① 95 侧已把 drop 收件文件以 HTTPS 暴露（VOLC_ASR_AUDIO_URL_TEMPLATE）；
          ② VOLC_ASR_API_KEY；③ 术语表自动作为 ASR 热词直传（corpus.context）。
    """
    import time as _time

    from src import volc_asr

    job_id = _current_job_id()
    glossary_terms: list[str] = []
    try:
        from src.project_glossary import load_glossary
        glossary_terms = load_glossary(project_id or "")
    except Exception:  # noqa: BLE001 — 词表故障不影响转写
        glossary_terms = []

    _state(job_id, "transcribing", "提取音频（云端转写用）", 0.08)
    audio = volc_asr.extract_audio_mp3(video_path)

    _state(job_id, "transcribing", "上传音频到中转（95）", 0.12)
    remote = volc_asr.upload_audio(audio, _job_owner_subject(project_id or ""), project_id or "")

    audio_url = volc_asr.audio_public_url(remote)
    _state(job_id, "transcribing", "提交云端转写任务（豆包大模型 ASR）", 0.18)
    task_id = volc_asr.submit_task(audio_url, glossary_terms)

    _state(job_id, "transcribing", "云端转写中（每 20 秒查询，长视频约数分钟）", 0.22)
    deadline = _time.time() + 3600
    while _time.time() < deadline:
        q = volc_asr.query_task(task_id)
        done = volc_asr.is_done(q)
        if done is True:
            parsed = volc_asr.parse_result(q.get("json") or {})
            segs = parsed["asr_segments"]
            if not segs:
                raise RuntimeError("云端转写返回空结果")
            # 中转音频自动清理：95 侧文件 + 本地临时 mp3（成功即删，不占存储）
            try:
                volc_asr.cleanup_remote_audio(remote)
            finally:
                audio.unlink(missing_ok=True)
            _state(job_id, "transcribing", f"云端转写完成：{len(segs)} 段（中转音频已清理）", 0.3)
            return segs, {"language": parsed.get("language", "zh-CN"), "words": [],
                          "segments": segs, "provider": "volc_cloud"}
        if done is None:
            raise RuntimeError(f"云端转写任务异常：{str(q)[:200]}")
        _time.sleep(20)
    raise RuntimeError("云端转写超时（>1 小时）")


def stage_correcting(raw_segments: list[dict], job_id: str, project_id: str | None = None) -> dict:
    """纠错：DSH 文本模型按完整上下文做普通中文校对（见 transcript_correction_llm）。

    * 只修错别字/同音字/漏字/重复字/标点/断句，不润色、不强制医学术语表；
    * 项目术语表注入 prompt（有参照才改术语，拿不准保留标 uncertain）；
    * 拿不准保留原文并标 uncertain；时间戳由原始分段复制，模型无权改动；
    * 模型未配置/单批失败 → 保守规整回退，engine/fallback_reasons 留痕。
    """
    from src.transcript_correction_llm import correct_segments_with_model

    glossary_terms: list[str] = []
    try:
        from src.project_glossary import load_glossary
        glossary_terms = load_glossary(project_id or "")
    except Exception:  # noqa: BLE001 — 词表故障绝不影响纠错主链路
        glossary_terms = []

    def _progress(message: str) -> None:
        _state(_current_job_id(), "correcting", message, 0.35)

    return correct_segments_with_model([
        {"start": round(float(item["start"]), 3), "end": round(float(item["end"]), 3), "text": str(item["text"]).strip()}
        for item in raw_segments
    ], progress=_progress, job_id=job_id or _current_job_id(), glossary_terms=glossary_terms)


def _call_stage_correcting(raw_segments: list[dict], job_id: str,
                           project_id: str | None) -> dict:
    if _accepts_keyword(stage_correcting, "project_id"):
        return stage_correcting(raw_segments, job_id, project_id=project_id)
    return stage_correcting(raw_segments, job_id)


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


def _stage_metadata(stage,record):
    metadata=_current_job().setdefault("metadata",{})
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


