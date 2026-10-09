"""Grounded golden-sentence and keyword extraction for the local Studio flow."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from .ai import _build_transcript_model, _get_missing_llm_key_error
from .config import get_config


class GoldenSentence(BaseModel):
    """A source-grounded line that can be highlighted in the timeline."""

    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str = Field(min_length=1, max_length=180)
    reason: str = Field(default="内容表达完整且有信息价值。", max_length=120)


class StudioInsights(BaseModel):
    golden_sentences: list[GoldenSentence] = Field(default_factory=list, max_length=5)
    keywords: list[str] = Field(default_factory=list, max_length=8)
    engine: Literal["llm", "local-fallback"]
    notice: str | None = None


class _LlmInsights(BaseModel):
    golden_sentences: list[GoldenSentence] = Field(default_factory=list, max_length=5)
    keywords: list[str] = Field(default_factory=list, max_length=8)


_PROMPT = """You extract editing metadata from a Chinese timestamped transcript.

Return only structured data that is grounded in the transcript.

Rules:
- Select 2 to 5 genuinely valuable, self-contained spoken sentences or very short adjacent spans.
- Each golden sentence must use its exact source wording; do not paraphrase, embellish, or invent facts.
- `start` and `end` must be taken from the selected source segment boundaries, and start must be less than end.
- A golden sentence should normally be no longer than 80 Chinese characters.
- Return 3 to 8 concise core keywords. Use terms that occur in, or are directly supported by, the transcript.
- Do not give medical advice or alter diagnoses, dosages, names, or other sensitive facts.
"""

_FILLER = ("大家好", "今天", "然后", "就是", "这个", "那个", "我们", "你们", "谢谢", "好的")
_COMMON_PHRASES = {"的时候", "就是说", "这个情况", "然后就是", "我们可以", "可能会", "因为这个", "什么问题"}


def _source_lines(segments: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[{float(item.get('start', 0)):08.3f} - {float(item.get('end', 0)):08.3f}] {str(item.get('text', '')).strip()}"
        for item in segments
        if str(item.get("text", "")).strip() and float(item.get("end", 0)) > float(item.get("start", 0))
    )


def _overlapping_source_text(
    segments: list[dict[str, Any]], start: float, end: float
) -> tuple[float, float, str] | None:
    matched = [
        item
        for item in segments
        if float(item.get("end", 0)) > start and float(item.get("start", 0)) < end
    ]
    if not matched:
        return None
    actual_start = float(matched[0]["start"])
    actual_end = float(matched[-1]["end"])
    text = "".join(str(item.get("text", "")).strip() for item in matched).strip()
    return actual_start, actual_end, text


def _fallback_keywords(text: str) -> list[str]:
    """Return conservative repeated Chinese phrase candidates when no LLM is configured."""
    candidates: Counter[str] = Counter()
    for span in re.findall(r"[\u4e00-\u9fff]{2,20}", text):
        for size in range(2, min(5, len(span)) + 1):
            for index in range(0, len(span) - size + 1):
                phrase = span[index : index + size]
                if phrase not in _COMMON_PHRASES and not any(word in phrase for word in _FILLER):
                    candidates[phrase] += 1
    ranked = sorted(
        ((phrase, count) for phrase, count in candidates.items() if count >= 2),
        key=lambda item: (item[1] * len(item[0]) ** 1.6, item[1], len(item[0])),
        reverse=True,
    )
    selected: list[str] = []
    for phrase, _ in ranked:
        # Do not surface nested fragments such as “时间”, “间轴”, “时间轴”.
        if any(phrase in existing or existing in phrase for existing in selected):
            continue
        selected.append(phrase)
        if len(selected) == 6:
            break
    return selected


def _fallback_insights(segments: list[dict[str, Any]], notice: str | None) -> StudioInsights:
    ranked: list[tuple[int, dict[str, Any]]] = []
    for item in segments:
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        score = min(len(text), 70)
        score += 20 if any(mark in text for mark in ("？", "！", "。")) else 0
        score += 18 if any(term in text for term in ("因为", "所以", "如果", "不要", "关键", "方法", "建议", "问题")) else 0
        score -= 35 if any(filler in text[:12] for filler in _FILLER) else 0
        ranked.append((score, item))
    selected = sorted(ranked, key=lambda value: value[0], reverse=True)[:4]
    golden = [
        GoldenSentence(
            start=float(item["start"]),
            end=float(item["end"]),
            text=str(item["text"]).strip()[:180],
            reason="本地规则候选，请人工确认。",
        )
        for _, item in selected
    ]
    all_text = "".join(str(item.get("text", "")) for item in segments)
    return StudioInsights(
        golden_sentences=golden,
        keywords=_fallback_keywords(all_text),
        engine="local-fallback",
        notice=notice or "未配置可用大模型，当前显示本地候选；配置模型后会自动使用模型提取。",
    )


async def extract_studio_insights(segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract exact-source highlights with a configured LLM, or a labelled fallback."""
    usable = [item for item in segments if str(item.get("text", "")).strip()]
    if not usable:
        return StudioInsights(engine="local-fallback", notice="没有可用于提取的转写内容。").model_dump()

    config = get_config()
    try:
        configuration_error = _get_missing_llm_key_error(config.llm, config)
        if configuration_error:
            raise RuntimeError(configuration_error)
        agent = Agent[None, _LlmInsights](
            model=_build_transcript_model(config),
            output_type=_LlmInsights,
            system_prompt=_PROMPT,
            output_retries=2,
        )
        result = await agent.run(
            "Transcript:\n" + _source_lines(usable) + "\n\nReturn golden_sentences and keywords."
        )
        deduped: list[GoldenSentence] = []
        seen: set[tuple[float, float]] = set()
        for candidate in result.output.golden_sentences:
            source = _overlapping_source_text(usable, candidate.start, candidate.end)
            if source is None:
                continue
            start, end, source_text = source
            key = (round(start, 3), round(end, 3))
            if key in seen or not source_text:
                continue
            seen.add(key)
            # Preserve exactly the text that will be used by the local timeline.
            deduped.append(GoldenSentence(start=start, end=end, text=source_text[:180], reason=candidate.reason))
        if not deduped:
            return _fallback_insights(usable, "模型未返回可对齐的金句，当前显示本地候选。").model_dump()
        keywords = []
        for raw_keyword in result.output.keywords:
            keyword = re.sub(r"\s+", "", raw_keyword).strip("，。；：、")
            if keyword and keyword not in keywords:
                keywords.append(keyword[:24])
        return StudioInsights(
            golden_sentences=deduped[:5],
            keywords=keywords[:8] or _fallback_keywords("".join(item["text"] for item in usable)),
            engine="llm",
        ).model_dump()
    except Exception as exc:  # A missing key or unavailable local model must not break ASR.
        return _fallback_insights(usable, f"模型提取暂不可用：{str(exc)[:160]}").model_dump()
