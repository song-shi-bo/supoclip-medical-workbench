"""时间线转写·说话人自动标注（2026-11-06）。

背景：纠错转写的 speaker 字段全部是「未标注」，过去只能在时间线里逐段手填。
本模块用窗口化 LLM 调用给每段标 {医生, 患者, 家属, 未知}，落盘为转写目录下的
overlay：``tasks/transcripts/<source_id>/speaker_labels.json``，**不改写 v2 本体**；
时间线载入时（timeline_copilot._project_payload）按 segment_id 非破坏合并显示。

契约（与 clip_refinement 的保守兜底一致）：
* 输出只允许四个标签；解析失败、未知 segment_id、非法标签一律回落「未知」；
* overlay 记录转写指纹（segment_id+文本 sha256），文本修订后旧 overlay 自动失效；
* LLM 失败不抛出：返回 llm_error + 全部「未知」，调用方如实展示原因；
* 每次调用记入 llm_budget 当日台账（kind=speaker_labeling）。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from . import llm_budget
from .content_analysis import _model_ready

SPEAKER_SCHEMA_VERSION = "speaker-labels/1"
SPEAKER_PROMPT_VERSION = "speaker-label-prompt/2026-11-06-a1"
SPEAKER_LABELS_FILENAME = "speaker_labels.json"
ALLOWED_LABELS = ("医生", "患者", "家属", "未知")
UNLABELED_VALUES = ("", "未标注", "unknown", "none", None)

# 窗口划分：537 段 ≈ 9 窗口；每窗 ≤60 段正文 + 上一窗尾部 4 段做对话延续上下文。
WINDOW_SEGMENTS = int(os.getenv("SPEAKER_LABEL_WINDOW", "60"))
CONTEXT_TAIL = 4
LLM_CALL_TIMEOUT_SECONDS = float(os.getenv("SPEAKER_LABEL_TIMEOUT_SECONDS", "180"))
SPEAKER_MAX_OUTPUT_TOKENS = max(512, int(os.getenv("SPEAKER_LABEL_MAX_TOKENS", "2048")))

# 测试替身：置为可调用 (user_prompt) -> str 时跳过真实 LLM。
_LABEL_LLM_FN: Callable[[str], str] | None = None

_SYSTEM_PROMPT = """你是医患对话转写的说话人标注员。输入是一段医疗门诊/咨询录音的转写片段，
按时间顺序排列，每行格式为 ``segment_id<TAB>文本``。请逐行判断该话轮的说话人。

判定要点：
- 医生：提问、解读检查结果、给出诊断/治疗方案、开处方、交代复查安排；
- 患者：描述自身症状、感受、病史，回答医生提问；
- 家属：替患者补充病情、询问费用/流程/陪护等事务性问题，语气站在患者一侧；
- 短应答（"嗯""好的""明白了"）按对话上下文跟随对方话轮的另一方；
- 儿科等"患者未到场、家属代述"场景，代述者仍按其身份（家属）标注；
- 证据不足时不要猜，标「未知」。

只输出严格 JSON，不要任何其他文字或代码块围栏：
{"labels": [{"segment_id": "seg_0001", "speaker": "医生", "reason": "≤12字依据"}], "context_note": "可选：本窗整体背景≤40字"}
speaker 只能取：医生 / 患者 / 家属 / 未知。输入里出现的每个 segment_id 都必须恰好输出一次。"""


# ---------------------------------------------------------------- 纯函数
def normalize_segments(segments: list[dict[str, Any]]) -> list[dict[str, str]]:
    """把转写段压成 (segment_id, text) 最小结构，丢弃空文本段。"""
    out: list[dict[str, str]] = []
    for seg in segments or []:
        text = str(seg.get("corrected_text") or seg.get("text") or "").strip()
        sid = str(seg.get("segment_id") or "").strip()
        if text and sid:
            out.append({"segment_id": sid, "text": text})
    return out


def transcript_fingerprint(segments: list[dict[str, Any]]) -> str:
    """指纹 = sha256(每段 segment_id + 规范化文本)。转写被人工修订后指纹变化，
    旧 overlay 合并时据此拒绝（防错位标签贴到改动后的文本上）。"""
    h = hashlib.sha256()
    for item in normalize_segments(segments):
        h.update(item["segment_id"].encode("utf-8"))
        h.update(b"\x1f")
        h.update(item["text"].encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()


def _parse_window(raw: str, expected_ids: list[str]) -> tuple[dict[str, str], str | None]:
    """解析一窗 LLM 输出 → ({segment_id: 合法标签}, 解析警告|None)。

    非法标签、集合外的 segment_id 一律丢弃（该段落回「未知」），不整体失败。
    """
    text = str(raw or "").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    try:
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001 — 单窗解析失败只影响该窗
        return {}, f"JSON 解析失败：{type(exc).__name__}"
    labels_raw = data.get("labels") if isinstance(data, dict) else None
    if not isinstance(labels_raw, list):
        return {}, "输出缺少 labels 数组"
    allowed = set(ALLOWED_LABELS[:3])  # 合并/统计时「未知」无意义，解析阶段只收前三
    labels: dict[str, str] = {}
    expected = set(expected_ids)
    for item in labels_raw:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("segment_id") or "").strip()
        speaker = str(item.get("speaker") or "").strip()
        if sid in expected and speaker in allowed:
            labels[sid] = speaker
    missing = [s for s in expected_ids if s not in labels]
    warn = None
    if missing:
        warn = f"{len(missing)} 段未返回或非法，按未知处理"
    return labels, warn


# ---------------------------------------------------------------- LLM 调用
def _run_label_agent(user_prompt: str) -> tuple[str, str]:
    """单窗口 LLM 调用，返回 (原始输出, model)。独立成函数便于测试替身替换。"""
    ready, model_name, reason = _model_ready()
    if not ready:
        raise RuntimeError(f"LLM 未配置：{reason}")
    if _LABEL_LLM_FN is not None:
        return str(_LABEL_LLM_FN(user_prompt)), model_name
    from .ai import _build_transcript_model, apply_settings_to_process_env
    from .config import get_config
    from pydantic_ai import Agent

    config = get_config()
    apply_settings_to_process_env(config.as_runtime_settings())
    settings: dict[str, Any] = {"max_tokens": SPEAKER_MAX_OUTPUT_TOKENS, "temperature": 0.1}
    from . import content_analysis as ca
    if ca.THINKING_DISABLED and ca._thinking_supported is not False:
        settings["extra_body"] = {"thinking": {"type": "disabled"}}
    agent: Agent[None, str] = Agent(
        model=_build_transcript_model(config),
        output_type=str,
        system_prompt=_SYSTEM_PROMPT,
    )
    import asyncio

    result = asyncio.run(asyncio.wait_for(
        agent.run(user_prompt, model_settings=settings), timeout=LLM_CALL_TIMEOUT_SECONDS))
    llm_budget.record_call("speaker_labeling", usage=llm_budget.extract_usage_details(result),
                           model=model_name)
    return str(result.output), model_name


def _window_prompt(window: list[dict[str, str]], context_tail: list[dict[str, str]]) -> str:
    lines = ["以下是转写片段（按时间顺序），逐行格式 segment_id<TAB>文本："]
    if context_tail:
        lines.append("（前文末尾，仅供参考，不要为它们输出标签）")
        lines.extend(f"{s['segment_id']}\t{s['text']}" for s in context_tail)
    lines.append("（需要标注的片段）")
    lines.extend(f"{s['segment_id']}\t{s['text']}" for s in window)
    lines.append("请输出严格 JSON。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 主入口
def label_transcript(segments: list[dict[str, Any]], *, transcript_id: str = "",
                     transcript_version: int | None = None, note: str = "",
                     resume_from: dict[str, Any] | None = None) -> dict[str, Any]:
    """对整条纠错转写跑说话人标注，返回可落盘的 overlay 记录。绝不抛出。

    ``resume_from``：上次同指纹 overlay——已标窗口直接复用，只对未覆盖窗口
    调 LLM（限流/中断后重试不再重复花钱）。
    """
    started = time.monotonic()
    items = normalize_segments(segments)
    fingerprint = transcript_fingerprint(segments)
    record: dict[str, Any] = {
        "schema": SPEAKER_SCHEMA_VERSION,
        "prompt_version": SPEAKER_PROMPT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "transcript_id": str(transcript_id or ""),
        "transcript_version": transcript_version,
        "fingerprint": fingerprint,
        "segment_count": len(items),
        "labels": {},
        "stats": {"医生": 0, "患者": 0, "家属": 0, "未知": len(items)},
        "llm_error": None,
        "model": "",
        "windows": {"total": 0, "failed": 0, "warnings": []},
        "note": str(note or ""),
    }
    prior_labels: dict[str, str] = {}
    if resume_from and str(resume_from.get("fingerprint") or "") == fingerprint:
        prior = resume_from.get("labels")
        if isinstance(prior, dict):
            allowed = set(ALLOWED_LABELS[:3])
            prior_labels = {str(k): str(v) for k, v in prior.items() if str(v) in allowed}
            record["model"] = str(resume_from.get("model") or "")
    if not items:
        record["llm_error"] = "转写没有可标注的文本段"
        return record
    try:
        ready, model_name, _ = _model_ready()
        if not ready:
            record["llm_error"] = "LLM 未配置"
            return record
        record["model"] = model_name
    except Exception as exc:  # noqa: BLE001 — 探测失败等同未配置
        record["llm_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return record

    stats = {"医生": 0, "患者": 0, "家属": 0}
    labels: dict[str, str] = dict(prior_labels)
    warnings: list[str] = []
    failed = 0
    total_windows = 0
    skipped_resumed = 0
    context_tail: list[dict[str, str]] = []
    for offset in range(0, len(items), max(1, WINDOW_SEGMENTS)):
        window = items[offset:offset + max(1, WINDOW_SEGMENTS)]
        total_windows += 1
        expected_ids = [s["segment_id"] for s in window]
        if prior_labels and all(prior_labels.get(sid) for sid in expected_ids):
            skipped_resumed += 1  # 断点续跑：该窗口上次已全部标好，不再调用
            context_tail = window[-CONTEXT_TAIL:] if CONTEXT_TAIL > 0 else []
            continue
        try:
            raw, model = _run_label_agent(_window_prompt(window, context_tail))
            record["model"] = model
            win_labels, warn = _parse_window(raw, expected_ids)
            labels.update(win_labels)
            if warn:
                warnings.append(f"窗口{total_windows}：{warn}")
        except Exception as exc:  # noqa: BLE001 — 单窗失败只影响该窗
            failed += 1
            warnings.append(f"窗口{total_windows} 调用失败：{str(exc)[:160]}")
        context_tail = window[-CONTEXT_TAIL:] if CONTEXT_TAIL > 0 else []
    for speaker in stats:
        stats[speaker] = sum(1 for v in labels.values() if v == speaker)
    known = sum(stats.values())
    record["labels"] = labels
    record["stats"] = {**stats, "未知": len(items) - known}
    record["windows"] = {"total": total_windows, "failed": failed,
                         "resumed": skipped_resumed, "warnings": warnings[:10]}
    if failed == total_windows and not prior_labels:
        record["llm_error"] = warnings[0] if warnings else "全部窗口调用失败"
        record["labels"] = {}
        record["stats"] = {"医生": 0, "患者": 0, "家属": 0, "未知": len(items)}
    elif failed:
        record["llm_error"] = None  # 部分窗口失败：保留已得标签，warnings 如实展示
    record["duration_seconds"] = round(time.monotonic() - started, 2)
    return record


# ---------------------------------------------------------------- 存取与合并
def labels_path(transcripts_root: Path, source_id: str) -> Path:
    return Path(transcripts_root) / source_id / SPEAKER_LABELS_FILENAME


def save_labels(transcripts_root: Path, source_id: str, record: dict[str, Any]) -> Path:
    """原子落盘（.part + os.replace），写后回读校验。"""
    path = labels_path(transcripts_root, source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if loaded.get("schema") != SPEAKER_SCHEMA_VERSION:
        raise ValueError("speaker_labels 落盘校验失败：schema 不符")
    return path


def load_labels(transcripts_root: Path, source_id: str) -> dict[str, Any] | None:
    """读 overlay；不存在/损坏/schema 不符返回 None（时间线侧静默跳过）。"""
    try:
        path = labels_path(transcripts_root, source_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != SPEAKER_SCHEMA_VERSION:
            return None
        return data
    except Exception:  # noqa: BLE001 — overlay 只是增强，读取失败不阻塞时间线
        return None


def merge_speaker_labels(segments: list[dict[str, Any]], record: dict[str, Any] | None) -> dict[str, Any]:
    """把 overlay 非破坏合并进转写段（返回摘要，不改入参结构）。

    只覆盖 speaker 为「未标注/空」的段；指纹不匹配（转写已修订）时拒绝合并；
    「未知」标签不覆盖（保持原显示）。返回 {merged, skipped_existing, reason}。
    """
    if not record or not isinstance(record, dict):
        return {"merged": 0, "skipped_existing": 0, "reason": "无 overlay"}
    fp_record = str(record.get("fingerprint") or "")
    fp_now = transcript_fingerprint(segments)
    if fp_record and fp_now and fp_record != fp_now:
        return {"merged": 0, "skipped_existing": 0, "reason": "转写已修订，标签指纹不匹配"}
    labels = record.get("labels") or {}
    if not isinstance(labels, dict):
        return {"merged": 0, "skipped_existing": 0, "reason": "overlay 标签为空"}
    merged = skipped = 0
    for seg in segments:
        sid = str(seg.get("segment_id") or "")
        if sid not in labels:
            continue
        current = seg.get("speaker")
        if current not in UNLABELED_VALUES:
            skipped += 1
            continue
        speaker = str(labels[sid])
        if speaker in ALLOWED_LABELS[:3]:
            seg["speaker"] = speaker
            merged += 1
    return {"merged": merged, "skipped_existing": skipped, "reason": ""}


def summary_for_payload(record: dict[str, Any] | None, merge_result: dict[str, Any]) -> dict[str, Any]:
    """时间线 payload 里附带的标注来源说明（前端可展示）。"""
    if not record:
        return {}
    return {
        "schema": SPEAKER_SCHEMA_VERSION,
        "transcript_id": record.get("transcript_id"),
        "transcript_version": record.get("transcript_version"),
        "stats": copy.deepcopy(record.get("stats") or {}),
        "llm_error": record.get("llm_error"),
        "merge": copy.deepcopy(merge_result),
    }


def annotate_clip_segments(
    labels: dict[str, Any] | None,
    source_segments: list[dict[str, Any]] | None,
    source_start: float,
    segments: list[dict[str, Any]],
    *,
    intro_offset: float = 0.8,
    min_overlap: float = 0.05,
) -> list[dict[str, Any]]:
    """把源级说话人 overlay 按**时间窗**映射到切片 seed 段（原位填充，返回入参）。

    为什么不走 :func:`merge_speaker_labels`：桥接层重组切片段时丢掉了源
    ``segment_id``（只留 start/end/text/speaker，且时间已平移到成片基），所以
    这里按「切片时间 − 片头偏移 + source_start」回溯源时间窗，取重叠最大的
    v2 段 id 再查 overlay。只填「未标注/空」，且只接受医生/患者/家属——
    「未知」与时间线合并语义一致，不落盘。数据缺失/形状不对一律原样返回：
    标注只是 seed 的增强，绝不让建基线失败。
    """
    if not segments or not isinstance(labels, dict) or not labels:
        return segments
    windows: list[tuple[float, float, str]] = []
    for seg in source_segments or []:
        if not isinstance(seg, dict):
            continue
        try:
            windows.append((float(seg["start"]), float(seg["end"]), str(seg.get("segment_id") or "")))
        except (KeyError, TypeError, ValueError):
            continue
    if not windows:
        return segments
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        if str(seg.get("speaker") or "").strip() not in UNLABELED_VALUES:
            continue
        try:
            src_a = float(seg["start"]) - intro_offset + source_start
            src_b = float(seg["end"]) - intro_offset + source_start
        except (KeyError, TypeError, ValueError):
            continue
        best_id, best_overlap = "", 0.0
        for a, b, sid in windows:
            if not sid:
                continue
            overlap = min(src_b, b) - max(src_a, a)
            if overlap > best_overlap:
                best_overlap, best_id = overlap, sid
        if not best_id or best_overlap < min_overlap:
            continue
        speaker = str(labels.get(best_id) or "")
        if speaker in ALLOWED_LABELS[:3]:
            seg["speaker"] = speaker
    return segments
