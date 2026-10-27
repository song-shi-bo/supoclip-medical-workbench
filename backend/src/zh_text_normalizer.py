"""中文转写文本繁→简规整（ASR 源头防护）。

背景：faster-whisper 的中文输出繁简混排是已知行为——同一份转写里
「文件」与「夾」「後」「記錄」混出（2026-10-15/17 实测均有复发）。
本模块在 ASR 结果入口统一做繁→简（OpenCC ``t2s``，离线、确定性、
零模型预算），保证下游（转写缓存 / LLM 纠错 / 字幕 / 搜索 /
artifact）只见简体。

失败姿态：opencc 缺失或转换异常时原样返回并记 warning，
绝不阻断转写主链路——规整是增强，不是依赖。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _converter() -> Any:
    """惰性加载 OpenCC t2s 转换器（opencc-python-reimplemented，纯 Python）。"""
    from opencc import OpenCC

    return OpenCC("t2s")


def to_simplified(text: str) -> str:
    """繁→简转换；任何异常都回退原文（不抛出，不阻断转写）。"""
    if not text:
        return text
    try:
        return _converter().convert(text)
    except Exception:  # pragma: no cover - 防御性兜底（依赖缺失/字典损坏）
        logger.warning(
            "zh_text_normalizer: t2s conversion failed; returning original text",
            exc_info=True,
        )
        return text


def normalize_asr_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """规整 faster-whisper 结果：``segments[].text`` 与 ``words[].word``。

    逐词转换保持词数与时间戳对齐（whisper 中文词级时间戳按词自带
    start/end，与字符数无关）；``text`` 由规整后的分段重建，保证一致。
    非 dict 输入原样返回。
    """
    if not isinstance(result, dict):
        return result
    segments = result.get("segments")
    if isinstance(segments, list):
        texts: list[str] = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg["text"] = to_simplified(str(seg.get("text", "")))
            texts.append(seg["text"])
            words = seg.get("words")
            if isinstance(words, list):
                for word in words:
                    if isinstance(word, dict) and "word" in word:
                        word["word"] = to_simplified(str(word.get("word", "")))
        if texts:
            result["text"] = "".join(texts).strip()
    elif isinstance(result.get("text"), str):
        result["text"] = to_simplified(result["text"])
    return result
