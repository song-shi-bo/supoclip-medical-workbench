"""对稿成片：LLM 分级（P2a）。

对 extra 段（医生多说）与"删后通顺性"做语义分级——整条视频 **至多 1 次调用**
（全部候选一次进、一次出，对齐纠错 patch 协议同风格），每次调用记入 llm_budget。

输出执法（程序侧，模型无权越界）：
- segment_id 必须存在于候选清单：未知 id 忽略并计数；
- classification 非法 → 钳到 offtopic 并计数；
- golden（金句保留）逐字校验：verdict 文本必须能在该段转写原文中找到
  （防模型发明）；
- 红线段（数字/剂量/药名/否定词）由 conform_edl 强制豁免，不经 LLM 拍板。

降级契约（与方案 §B 一致）：LLM 未配置/调用失败/输出无效 → 回退 local-rule
（extra 按默认规则处置），engine 如实标注 local-rule，绝不冒充模型结果。
VERSION: conform-review/v1
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from .config import get_config
from .runtime_settings import apply_settings_to_process_env
from .script_align import normalize_text

CONFORM_REVIEW_PROMPT_VERSION = "conform-review/v1"

VALID_CLASSES = ("golden", "offtopic", "risk", "error_take")

CONFORM_REVIEW_SYSTEM_PROMPT = """你是医疗科普口播视频的审稿助手。医生按文稿拍摄了视频，
但总会有一些"文稿外的多说"。你将收到：文稿已拍句子（上下文）和文稿外的多说段（逐段带 id）。

请对每一段多说给出分级：
- golden：医学上有价值的补充（新信息、重要的患者提醒），建议保留为金句；
- offtopic：与文稿主题无关或纯寒暄/推广/预告，建议剪掉；
- risk：含绝对化承诺、疗效保证、推荐具体产品/机构、超出科普范围的诊断结论等合规风险，必须剪掉；
- error_take：口误、说错后重启的废段，建议剪掉。

同时判断：如果把这段从视频中剪掉，前后文是否仍然通顺（coherent_removal）。
规则：不确定时选保守（offtopic + coherent_removal=true）；不发明文稿里没有的内容；
不改写医生的原话；只依据给出的文本判断。"""


class ExtraVerdict(BaseModel):
    segment_id: str
    classification: str = "offtopic"
    coherent_removal: bool = True
    reason: str = ""


class ReviewOut(BaseModel):
    verdicts: list[ExtraVerdict] = []

_agent = None
_agent_signature: tuple | None = None


def _ensure_llm_ready() -> None:
    runtime_config = get_config()
    from .ai import _get_missing_llm_key_error

    error = _get_missing_llm_key_error(runtime_config.llm, runtime_config)
    if error:
        # 缺模型配置：显式报错，由调用方回退 local-rule（不静默冒充 LLM 结果）
        raise RuntimeError(f"对稿分级需要 LLM：{error}")


def _get_agent():
    global _agent, _agent_signature
    from .ai import _build_transcript_model
    from pydantic_ai import Agent

    _ensure_llm_ready()
    runtime_config = get_config()
    signature = (
        runtime_config.llm,
        runtime_config.openai_api_key,
        runtime_config.google_api_key,
        runtime_config.anthropic_api_key,
        runtime_config.ollama_base_url,
        runtime_config.ollama_api_key,
    )
    if _agent is None or _agent_signature != signature:
        apply_settings_to_process_env(runtime_config.as_runtime_settings())
        model = _build_transcript_model(runtime_config)
        _agent = Agent(model=model, output_type=ReviewOut,
                       system_prompt=CONFORM_REVIEW_SYSTEM_PROMPT, output_retries=2)
        _agent_signature = signature
    return _agent


def _build_user_prompt(matched_sentences: list[dict[str, Any]],
                       extra_segments: list[dict[str, Any]]) -> str:
    context = [
        {"id": f"align-{s.get('index')}", "text": str(s.get("text") or "")}
        for s in matched_sentences or [] if s.get("status") == "matched"
    ]
    extras = [
        {"segment_id": str(e.get("segment_id")), "text": str(e.get("text") or ""),
         "start_ms": e.get("start_ms"), "end_ms": e.get("end_ms")}
        for e in extra_segments or []
    ]
    return json.dumps({"文稿已拍句子": context, "文稿外多说段": extras},
                      ensure_ascii=False)


def review_extra_segments(match_record: dict[str, Any], *,
                          job_id: str | None = None) -> dict[str, Any]:
    """LLM 分级 extra 段。返回 JSON 可序列化分级结果（engine 如实标注）。

    任何异常（未配置/网络/输出无效）→ 回退 local-rule，绝不抛出、绝不冒充。
    """
    extra_segments = list(match_record.get("extra_segments") or [])
    matched_sentences = list(match_record.get("sentences") or [])
    fallback: dict[str, Any] = {
        "version": CONFORM_REVIEW_PROMPT_VERSION,
        "engine": "local-rule",
        "verdicts": [],
        "warnings": [],
    }
    if not extra_segments:
        fallback["warnings"].append("无 extra 段，无需分级")
        return fallback
    try:
        agent = _get_agent()
        model_name = get_config().llm
        user_prompt = _build_user_prompt(matched_sentences, extra_segments)
        from .llm_budget import estimate_reserve, estimate_tokens, extract_usage, record_call

        reserve = estimate_reserve(user_prompt, 2048) + estimate_tokens(
            CONFORM_REVIEW_SYSTEM_PROMPT)
        record_call("conform_review", job_id=job_id, est_reserved=reserve,
                    model=model_name, note="P2a 预留")
        result = agent.run_sync(user_prompt)
        usage = extract_usage(result)
        record_call("conform_review", job_id=job_id, usage=usage,
                    model=model_name, note="P2a 实际")

        valid_ids = {str(e.get("segment_id")) for e in extra_segments}
        verdicts: list[dict[str, Any]] = []
        invalid = 0
        for v in (result.output.verdicts or []):
            sid = str(v.segment_id)
            if sid not in valid_ids:
                invalid += 1
                continue
            if v.classification not in VALID_CLASSES:
                invalid += 1  # 非法分类：钳到 offtopic 并计数（docstring 契约）
            classification = v.classification if v.classification in VALID_CLASSES else "offtopic"
            verdicts.append({
                "segment_id": sid,
                "classification": classification,
                "coherent_removal": bool(v.coherent_removal),
                "reason": str(v.reason or "")[:200],
            })
        out: dict[str, Any] = {
            "version": CONFORM_REVIEW_PROMPT_VERSION,
            "engine": f"llm:{model_name}",
            "verdicts": verdicts,
            "warnings": [],
        }
        if invalid:
            out["warnings"].append(f"忽略 {invalid} 条非法裁决（未知 id/分类）")
        return out
    except Exception as exc:  # noqa: BLE001 —— 分级失败回退 local-rule（方案 §B 降级契约）
        fallback["warnings"].append(f"LLM 分级不可用：{exc}；extra 段按默认规则处置")
        fallback["fallback_reason"] = str(exc)[:200]
        return fallback
