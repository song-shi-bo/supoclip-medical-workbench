"""DSH 文本模型全文纠错：普通中文校对（patch 协议，2026-10-15 额度优化版）。

2026-10-14 需求口径（转写与纠错完善，不触碰切片算法）：

* faster-whisper 原始分段 → 受管文本模型按「完整上下文」做普通中文纠错；
* 只允许修正：错别字、同音字错误、漏字、重复字、标点、断句；
* 禁止：润色、扩写、总结、改写句式、按医学术语表强制替换词句；
* 拿不准的字词 → 保留原文并标 ``uncertain=true``；
* **时间戳永不来自模型**：``start/end``（秒）与 ``start_ms/end_ms``（毫秒）
  全部由原始分段复制，模型只回文本；**输入不携带时间戳**；
* 模型未配置 / 某批输出不合法 → 该批回退 :mod:`src.transcript_correction`
  的保守规整（``engine`` 与 ``fallback_reasons`` 留痕），绝不静默丢段、
  绝不改变分段数量；fallback 结果的 engine 永远不是 ``dsh-llm:*``。

2026-10-15 额度优化（token 预留制，见 :mod:`src.llm_budget`）：

* **patch 协议**：模型只返回「发生修改的分段」，未修改段不再逐段回显——
  输出 token 从 O(全部分段) 降到 O(修改分段)；
* **整篇紧凑输入**：≤ ``SINGLE_CALL_LIMIT`` 段且估算 prompt 在
  ``SINGLE_PROMPT_TOKEN_LIMIT`` 内时一次请求携带全部分段（每行「序号|文本」，
  无 JSON 包裹、无时间戳）；
* 确需分批时**不再每批重复全文开头**：改为「全篇开头摘要（默认 300 字）+
  当前批 + 上一批少量重叠段（默认 4 段）」；
* **显式 max_tokens**（网关按 prompt+max_tokens 预留）：默认 4096，输出超上限
  报 token limit 时提一档到 6144 重试一次；仍超且素材较大 → 整篇降级分批重跑；
* 每次（成功或 token-limit 失败）调用都把预计预留与真实 usage 记入
  :mod:`src.llm_budget` 当日台账。

真实调用位置：本模块 :func:`correct_segments_with_model` →
``pydantic_ai.Agent(model=config.llm)``（与 ``studio_copilot._llm_copilot``
同一条受控凭据通道：受保护 ``backend/.env`` 的 ``LLM`` /
``OPENAI_API_KEY`` / ``OPENAI_BASE_URL``；密钥值不读不入日志）。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Callable

from pydantic import BaseModel, Field

from . import llm_budget

# 每次请求最多校对的分段数（分批模式；可用 CONTENT_CORRECTION_BATCH_SIZE 调小）。
BATCH_SIZE = max(10, int(os.getenv("CONTENT_CORRECTION_BATCH_SIZE", "100")))
# ≤ 该段数优先「整篇单次」请求（patch 协议下输出只含修改段，可显著放大该阈值）
SINGLE_CALL_LIMIT = max(10, int(os.getenv("CONTENT_CORRECTION_SINGLE_CALL_LIMIT", "800")))
# 整篇模式的 prompt 估算上限（超过则强制分批）
SINGLE_PROMPT_TOKEN_LIMIT = max(2000, int(os.getenv("CONTENT_CORRECTION_SINGLE_PROMPT_TOKEN_LIMIT", "24000")))
# 分批模式下每批前置的「上一批重叠段数」（少量；只读参考，不要求输出）
CONTEXT_OVERLAP = max(0, int(os.getenv("CONTENT_CORRECTION_CONTEXT_OVERLAP", "4")))
# 全篇摘要的最大字符数（替代旧的每批 4000 字全文开头）
SUMMARY_CHARS = max(200, int(os.getenv("CONTENT_CORRECTION_HEAD_CHARS", "300")))
# 单批调用超时：优先纠错专用环境变量，其次复用 Copilot 的 LLM_TIMEOUT_SECONDS
LLM_CALL_TIMEOUT_SECONDS = float(
    os.getenv("CORRECTION_LLM_TIMEOUT_SECONDS") or os.getenv("LLM_TIMEOUT_SECONDS") or "180"
)
# 显式输出上限（网关按 prompt+max_tokens 预留；30 段批的 patch 输出远小于该值）。
# token limit 时提一档到 RETRY 重试一次。
MAX_OUTPUT_TOKENS = max(1024, int(os.getenv("CONTENT_CORRECTION_MAX_TOKENS", "4096")))
MAX_OUTPUT_TOKENS_RETRY = max(MAX_OUTPUT_TOKENS, int(os.getenv("CONTENT_CORRECTION_MAX_TOKENS_RETRY", "6144")))

_SYSTEM_PROMPT = """你是中文语音转写校对器。输入是 ASR 转写分段，每行格式「序号|文本」，常见同音字/近音字错误。
目标：结合上下文**只修正明显的转写错误**——只做纠错，不做改写。
1. 必须修正（上下文明确时）：同音字/近音字错误（例如谈用药时「停腰→停药」、
   谈血压数值时「拱柱→汞柱」）、漏字、重复字、明显错误的标点与断句、拼音混入、
   繁体字（一律转为对应简体字；这属于字形纠错，不算改写）。
2. 判断依据：前后句主题、词语搭配、数值与单位、口语表达习惯。
3. 禁止：润色、扩写、总结、改变句式或语序、把口语说法改成书面/医学术语、
   按任何词表批量替换词语。纠错恢复的是「说话人原本说出的词」，不是「更专业的词」。
4. **只输出发生修改的分段**：未修改的段落不要出现在输出里。
5. 上下文确实无法判断的字词：不修改，输出该段并设 uncertain=true、
   corrected_text 留空，reason 写明不确定原因（≤20字）；能明确判断时必须纠正。
6. index 使用输入行给出的全局序号；不输出时间戳；不增删合并分段。
只输出严格 JSON：{"changes":[{"index":0,"corrected_text":"校对后文本","reason":"修改原因(≤20字)","uncertain":false}]}"""

_last_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}


class _PatchItem(BaseModel):
    index: int = Field(ge=0)
    corrected_text: str = Field(default="", max_length=4000)
    reason: str = Field(default="", max_length=200)
    uncertain: bool = False


class _PatchBatch(BaseModel):
    changes: list[_PatchItem] = Field(default_factory=list, max_length=2000)


def _model_ready() -> tuple[bool, str, str]:
    """返回 ``(是否可用, 模型描述, 不可用原因)``。与 Copilot 同一判据。"""
    from .ai import _get_missing_llm_key_error
    from .config import get_config

    config = get_config()
    model_name = str(config.llm or "").strip()
    if not model_name:
        return False, "", "LLM 未配置"
    error = _get_missing_llm_key_error(model_name, config)
    if error:
        return False, model_name, error
    return True, model_name, ""


def _run_agent(model_name: str, user_prompt: str, max_tokens: int) -> _PatchBatch:
    """单次 patch 调用（显式 max_tokens）。独立成函数便于测试替身替换。"""
    global _last_usage
    from .ai import _build_transcript_model, apply_settings_to_process_env
    from .config import get_config
    from pydantic_ai import Agent

    config = get_config()
    apply_settings_to_process_env(config.as_runtime_settings())
    agent: Agent[None, _PatchBatch] = Agent(
        model=_build_transcript_model(config),
        output_type=_PatchBatch,
        system_prompt=_SYSTEM_PROMPT,
        output_retries=2,
    )

    async def _call() -> _PatchBatch:
        return (await asyncio.wait_for(
            agent.run(user_prompt, model_settings={
                "max_tokens": max_tokens, "temperature": 0.2}),
            timeout=LLM_CALL_TIMEOUT_SECONDS)).output

    result = asyncio.run(_call())
    _last_usage = llm_budget.extract_usage(result)
    return result


def _prepare(raw_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for item in raw_segments:
        prepared.append({
            "start": round(float(item.get("start", 0.0)), 3),
            "end": round(float(item.get("end", 0.0)), 3),
            "text": str(item.get("text", "")).strip(),
        })
    return prepared


def _lexicon_hint_lines(texts: list[str]) -> list[str]:
    """领域词典提示段（医疗词典 P1）：命中条目 → 提示行列表；无命中/异常 → 空列表。

    每行格式「  标准词（类型）常见错写：别名1、别名2」，无别名的条目省略
    「常见错写」部分。词典模块任何故障都不影响纠错主链路（吞异常）。
    """
    try:
        from . import medical_lexicon

        hits = medical_lexicon.match_segments(texts, limit=20)
    except Exception:  # noqa: BLE001  # 词典故障绝不影响主链路
        return []
    if not hits:
        return []
    lines = ["【领域词典（仅供校对参考；仅当原文词与条目同音/形近且语义吻合才修改；"
             "不确定保持原文并标 uncertain；不得凭空引入词典外的修改）】"]
    for entry in hits:
        aliases = [str(alias) for alias in (entry.get("aliases") or []) if str(alias)]
        line = f"  {entry.get('standard', '')}（{entry.get('type', '')}）"
        if aliases:
            line += f"常见错写：{'、'.join(aliases)}"
        lines.append(line)
    return lines


def _build_user_prompt(batch: list[dict[str, Any]], batch_start: int,
                       prepared: list[dict[str, Any]], batched_mode: bool) -> str:
    """紧凑输入：每行「全局序号|文本」，无 JSON 包裹、无时间戳。

    分批模式上下文 = 全篇开头摘要（SUMMARY_CHARS）+ 上一批少量重叠段，
    不再重复大段全文开头。任务说明行之后、摘要之前插入领域词典提示段
    （无命中时不插入，prompt 与无词典时逐字节一致）。
    """
    lines = [f"请校对以下 {len(batch)} 段转写（全局序号自 {batch_start} 起）。只输出发生修改的分段。"]
    lines.extend(_lexicon_hint_lines([seg["text"] for seg in batch]))
    if batched_mode:
        summary = "".join(seg["text"] for seg in prepared)[:SUMMARY_CHARS]
        if summary:
            lines.append(f"【全篇摘要（截取自开头，仅供理解，勿输出）】{summary}")
        context_from = max(0, batch_start - CONTEXT_OVERLAP)
        if context_from < batch_start:
            context = " ".join(f"{i}|{prepared[i]['text']}" for i in range(context_from, batch_start))
            lines.append(f"【前文重叠（仅供上下文，勿输出）】{context}")
    lines.append("【待校对分段（序号|文本；只返回有修改的 patch）】")
    lines.extend(f"{batch_start + offset}|{seg['text']}" for offset, seg in enumerate(batch))
    return "\n".join(lines)


def _choose_mode(prepared: list[dict[str, Any]]) -> str:
    """整篇单次 vs 分批：段数与估算 prompt 双门槛。"""
    if len(prepared) <= SINGLE_CALL_LIMIT:
        prompt = _build_user_prompt(prepared, 0, prepared, batched_mode=False)
        if llm_budget.estimate_tokens(prompt) <= SINGLE_PROMPT_TOKEN_LIMIT:
            return "single"
    return "batch"


def _batch_bounds(total: int) -> list[tuple[int, int]]:
    return [(start, min(start + BATCH_SIZE, total)) for start in range(0, total, BATCH_SIZE)]


def _run_patch_call(model_name: str, batch: list[dict[str, Any]], batch_start: int,
                    prepared: list[dict[str, Any]], batched_mode: bool,
                    progress: Callable[[str], None] | None, label: str,
                    job_id: str | None) -> dict[str, Any]:
    """一次 patch 请求（含 token-limit 提档重试一次）。返回 outcome 字典。

    * 成功 / token-limit 失败都会按「该次实际 max_tokens」把预留记入台账；
    * 非 token 上限的失败不重试（由调用方按批回退或整体回退）。
    """
    prompt = _build_user_prompt(batch, batch_start, prepared, batched_mode)
    est = llm_budget.estimate_reserve(prompt, MAX_OUTPUT_TOKENS_RETRY)
    last_error: str | None = None
    token_limit = False
    zero_usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    usage: dict[str, int] = dict(zero_usage)
    for max_tokens, is_retry in ((MAX_OUTPUT_TOKENS, False), (MAX_OUTPUT_TOKENS_RETRY, True)):
        try:
            if progress:
                progress(label + ("（输出上限提档重试）" if is_retry else ""))
            changes = _run_agent(model_name, prompt, max_tokens).changes
            usage = dict(_last_usage)
            llm_budget.record_call("transcript_correction", job_id=job_id,
                                   est_reserved=llm_budget.estimate_reserve(prompt, max_tokens),
                                   usage=usage, model=model_name)
            return {"items": changes, "error": None, "token_limit": False,
                    "est": est, "usage": usage}
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            usage = dict(zero_usage)  # 失败调用的真实 usage 不可得，按 0 计（不重复累计他批）
            token_limit = llm_budget.is_token_limit_error(exc)
            if token_limit:
                # 输出上限不足：该次请求已被网关按 max_tokens 预留，如实记账
                llm_budget.record_call("transcript_correction", job_id=job_id,
                                       est_reserved=llm_budget.estimate_reserve(prompt, max_tokens),
                                       usage=usage, model=model_name, note="token_limit_retry")
                continue
            break
    return {"items": None, "error": last_error, "token_limit": token_limit,
            "est": est, "usage": usage}


def _audit_entry(segment_id: str, seg: dict[str, Any], raw_text: str, text: str,
                 status: str, reason: str, uncertain: bool, engine: str) -> dict[str, Any]:
    changes: list[dict[str, str]] = []
    if text != raw_text:
        changes.append({"type": "llm" if engine.startswith("dsh-llm") else "formatting",
                        "from": raw_text, "to": text, "basis": reason or "标点/格式规整"})
    return {
        "segment_id": segment_id,
        "start": float(seg["start"]),
        "end": float(seg["end"]),
        "raw_text": raw_text,
        "corrected_text": text,
        "status": status,
        "changes": changes,
        "issues": [],
        "reviewed_by_human": False,
        "reason": reason,
        "uncertain": uncertain,
        "engine": engine,
    }


def _fallback_all(prepared: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    """模型不可用：整体走既有保守规整，并补齐新字段（形状与模型路径一致）。"""
    from .transcript_correction import correct_segments

    result = correct_segments(prepared)
    summary = dict(result["correction_summary"])
    summary["fallback_reason"] = reason
    summary["llm_batches"] = 0
    summary["fallback_batches"] = 1
    return {
        "asr_segments": _enrich(prepared, result["asr_segments"], result["correction_audit"]),
        "correction_audit": result["correction_audit"],
        "correction_summary": summary,
    }


def _enrich(prepared: list[dict[str, Any]], asr_segments: list[dict[str, Any]],
            audit: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给分段补齐 segment_id / start_ms / end_ms / raw_text / uncertain / correction_reason。

    时间戳只从 ``prepared``（原始分段）复制，幂等：已带新字段的结果重跑不变。
    """
    enriched: list[dict[str, Any]] = []
    for index, seg in enumerate(asr_segments):
        source = prepared[index] if index < len(prepared) else seg
        audit_entry = audit[index] if index < len(audit) else {}
        item = dict(seg)
        item["segment_id"] = f"seg_{index:04d}"
        item["start"] = float(source["start"])
        item["end"] = float(source["end"])
        item["start_ms"] = int(round(float(source["start"]) * 1000))
        item["end_ms"] = int(round(float(source["end"]) * 1000))
        item["raw_text"] = str(audit_entry.get("raw_text") or source.get("text") or "")
        item["uncertain"] = bool(audit_entry.get("uncertain", item.get("uncertain", False)))
        item["correction_reason"] = str(audit_entry.get("reason", "") or "")
        enriched.append(item)
    return enriched


def _llm_correct(prepared: list[dict[str, Any]], model_name: str,
                 progress: Callable[[str], None] | None,
                 job_id: str | None = None) -> dict[str, Any]:
    """模型纠错主流程（patch 协议）；单批失败仅该批回退。"""
    from .transcript_correction import correct_segments

    total = len(prepared)
    engine_llm = f"dsh-llm:{model_name}"
    mode = _choose_mode(prepared)
    segments_out: list[dict[str, Any]] = []
    audit_out: list[dict[str, Any]] = []
    fallback_reasons: list[str] = []
    llm_batches = 0
    fallback_batches = 0
    llm_changed = 0
    llm_uncertain = 0
    invalid_patches = 0
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    est_reserved_total = 0
    downgrade_reason: str | None = None

    def merge_usage(outcome: dict[str, Any]) -> None:
        nonlocal est_reserved_total
        est_reserved_total += int(outcome.get("est") or 0)
        for key in usage_totals:
            usage_totals[key] += int(outcome.get("usage", {}).get(key) or 0)

    def apply_changes(batch: list[dict[str, Any]], batch_start: int,
                      items: list[_PatchItem]) -> None:
        nonlocal llm_changed, llm_uncertain, invalid_patches
        patch_map: dict[int, _PatchItem] = {}
        for item in items:
            index = int(item.index)
            if batch_start <= index < batch_start + len(batch) and index not in patch_map:
                patch_map[index] = item
            else:
                invalid_patches += 1  # 越界/重复序号：忽略并计数（不回退整批）
        for offset, seg in enumerate(batch):
            global_index = batch_start + offset
            raw_text = seg["text"]
            item = patch_map.get(global_index)
            text, status, reason, uncertain = raw_text, "no_change", "", False
            if item is None:
                pass  # 无 patch：保持原文
            elif item.uncertain or not item.corrected_text.strip():
                # 没把握 / 空 patch：保留原文，标 pending_review 供人工核听。
                status = "pending_review"
                reason = item.reason.strip() or ("模型不确定，保留原文" if item.uncertain else "模型输出为空，保留原文")
                uncertain = True
            else:
                corrected = str(item.corrected_text or "").strip()
                if corrected != raw_text:
                    text, status, reason = corrected, "corrected", item.reason.strip()
            if status == "corrected":
                llm_changed += 1
            if uncertain:
                llm_uncertain += 1
            segment_id = f"seg_{global_index:04d}"
            segments_out.append({
                "start": float(seg["start"]),
                "end": float(seg["end"]),
                "text": text,
                "speaker": "未标注",
                "correction_status": status,
                "issues": [],
                "uncertain": uncertain,
                "correction_reason": reason,
            })
            audit_out.append(_audit_entry(segment_id, seg, raw_text, text, status,
                                          reason, uncertain, engine_llm))

    def run_batches() -> None:
        nonlocal llm_batches, fallback_batches
        bounds = _batch_bounds(total)
        for number, (start, end) in enumerate(bounds, start=1):
            batch = prepared[start:end]
            outcome = _run_patch_call(
                model_name, batch, start, prepared, batched_mode=True, progress=progress,
                label=f"模型全文纠错（批次 {number}/{len(bounds)}）", job_id=job_id)
            merge_usage(outcome)
            if outcome["items"] is None:
                fallback_batches += 1
                fallback_reasons.append(f"batch#{number}: {outcome['error']}")
                mech = correct_segments(batch)
                segments_out.extend(mech["asr_segments"])
                audit_out.extend(mech["correction_audit"])
                continue
            llm_batches += 1
            apply_changes(batch, start, outcome["items"])

    if mode == "single":
        outcome = _run_patch_call(model_name, prepared, 0, prepared, batched_mode=False,
                                  progress=progress, label="模型全文纠错（整篇）", job_id=job_id)
        merge_usage(outcome)
        if outcome["items"] is not None:
            llm_batches = 1
            apply_changes(prepared, 0, outcome["items"])
        elif outcome["token_limit"] and total > BATCH_SIZE:
            # 整篇输出超上限（含提档重试）：降级分批重跑——每批输出更小，必然可容
            downgrade_reason = f"整篇输出超上限，已降级分批重跑：{outcome['error']}"
            run_batches()
        else:
            result = _fallback_all(prepared, outcome["error"] or "模型输出不合法")
            result["correction_summary"].update({
                "mode": mode,
                "llm_reserved_tokens_estimate": est_reserved_total,
                "llm_prompt_tokens": usage_totals["prompt_tokens"],
                "llm_completion_tokens": usage_totals["completion_tokens"],
                "llm_reasoning_tokens": usage_totals["reasoning_tokens"],
            })
            return result
    else:
        run_batches()

    if llm_batches == 0:
        # 全部批次失败：引擎如实记为保守规整，保留逐批失败原因
        engine = "local-conservative-rules"
    elif fallback_batches == 0:
        engine = engine_llm
    else:
        engine = f"{engine_llm}+local-fallback"
    summary = {
        "segments_total": total,
        "segments_audited": total,
        "coverage": 1.0 if total else 0.0,
        "low_risk_changes": llm_changed,
        "issues_pending_review": llm_uncertain,
        "engine": engine,
        "model": model_name,
        "mode": mode,
        "llm_batches": llm_batches,
        "fallback_batches": fallback_batches,
        "fallback_reasons": fallback_reasons,
        "llm_changed": llm_changed,
        "llm_uncertain": llm_uncertain,
        "invalid_patches": invalid_patches,
        "llm_reserved_tokens_estimate": est_reserved_total,
        "llm_prompt_tokens": usage_totals["prompt_tokens"],
        "llm_completion_tokens": usage_totals["completion_tokens"],
        "llm_reasoning_tokens": usage_totals["reasoning_tokens"],
    }
    if downgrade_reason:
        summary["single_call_downgrade"] = downgrade_reason[:200]
    return {
        "asr_segments": _enrich(prepared, segments_out, audit_out),
        "correction_audit": audit_out,
        "correction_summary": summary,
    }


def correct_segments_with_model(raw_segments: list[dict[str, Any]],
                                progress: Callable[[str], None] | None = None,
                                job_id: str | None = None) -> dict[str, Any]:
    """转写纠错入口：DSH 模型全文纠错（patch 协议），不可用/失败回退保守规整。

    返回形状与 :func:`src.transcript_correction.correct_segments` 兼容
    （``asr_segments`` / ``correction_audit`` / ``correction_summary``），
    并保证每个分段带 ``segment_id`` / ``start_ms`` / ``end_ms`` /
    ``raw_text`` / ``uncertain`` / ``correction_reason``，时间戳与输入逐段一致。
    fallback 结果的 ``engine`` 如实为 ``local-conservative-rules``（绝不为 dsh-llm）。
    """
    global _last_usage
    _last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    prepared = _prepare(raw_segments)
    ready, model_name, reason = _model_ready()
    if not ready:
        result = _fallback_all(prepared, reason)
        result["correction_summary"]["model"] = model_name or None
        return result
    result = _llm_correct(prepared, model_name, progress, job_id=job_id)
    return result
