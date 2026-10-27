"""字幕合成（工作台侧 ASS/SRT）：与 ``video-brain/app/subtitles.py`` 的 ASS 约定兼容。

契约：``docs/inspector-phase1-contract.md`` §3.3。

本模块只吃 **plain dict / Path**，既不 import ``clip_config`` 也不 import
``studio_project_store`` —— 配置由调用方（路由 / 局部任务）校验后传进来，
目录由调用方给出，因此可以独立单测、也不会产生循环导入。

ASS 兼容性（Phase 2 可直接喂渲染）
----------------------------------
输出头与 ``video-brain/app/subtitles.py`` 保持一致：
``ScriptType: v4.00+`` / ``PlayResX: 1080`` / ``PlayResY: 1920`` / ``WrapStyle: 0``，
单一样式名 ``Speak``、``Alignment 2``（底部居中）、``BorderStyle 1``（背景开启时 3）、
``Shadow 1``、``MarginV = percent_to_pixels(position, w, h)["bottom"]``（默认 420），
时间格式 ``H:MM:SS.cc``。

关于词级高亮
------------
本阶段**没有词级时间戳**（转写只有段级 ``start``/``end``），因此
``word_highlight=True`` 时按「一个 cue 的总时长在它自己那些 token 之间**平均分配**」
生成 ``{\\k<cs>}`` 卡拉OK 标签（token = 空白分隔的词 + 每个 CJK 字符），
并保留 ``\\an2``。这不是精确对齐，只是让未说出的字先显示 SecondaryColour。

关于 ``Position.x_pct``
-----------------------
``Alignment 2`` 只保证「居中」；``x_pct != 50`` 时本模块在每条 Dialogue 上追加
``\\pos(x, height - margin_v)`` 把锚点挪到配置位置（``x`` 与前端预览共用同一
``percent_to_pixels`` 公式）。``x_pct == 50`` 时不加任何内联标签，输出的 Dialogue
与 video-brain 逐字节一致。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_FONT_NAME",
    "DEFAULT_FONT_SIZE",
    "DEFAULT_HEIGHT",
    "DEFAULT_MARGIN_LR",
    "DEFAULT_WIDTH",
    "DEFAULT_Y_PCT",
    "META_FILENAME",
    "SCHEMA_VERSION",
    "STYLE_ID",
    "STYLE_NAME",
    "SUBTITLE_DIRNAME",
    "SubtitleComposeError",
    "ass_content",
    "build_cues",
    "compose",
    "config_hash",
    "list_revisions",
    "next_revision",
    "normalize_config",
    "percent_to_pixels",
    "revision_dir",
    "srt_content",
    "subtitles_dir",
]

DEFAULT_WIDTH, DEFAULT_HEIGHT = 1080, 1920
DEFAULT_Y_PCT = 78.125          # → MarginV 420，与 video-brain app/subtitles.py 一致
#: 44px ≈ 画宽的 4.1%（subtitles@3 起与 video-brain 一致；QC 门禁上限 64）。旧值 54px 白字偏大扎眼。
DEFAULT_FONT_SIZE = 44
DEFAULT_FONT_NAME = "Noto Sans CJK SC"   # 仅当 config.font_family 为空时用于 ASS 头
#: 金黄 #FFD700（subtitles@3 起与 video-brain PrimaryColour 一致），近黑描边下清晰醒目。
DEFAULT_TEXT_COLOR = "#FFD700"
DEFAULT_OUTLINE_COLOR = "#101010"
#: 3px ≈ 字号的 5.5%；旧值 4px 配 Bold 视觉过重。
DEFAULT_OUTLINE_WIDTH = 3
DEFAULT_MARGIN_LR = 70
DEFAULT_BACKGROUND_COLOR = "#000000"
DEFAULT_BACKGROUND_OPACITY = 0.5
#: word_highlight=false 时与 video-brain 完全一致的 BackColour（半透明黑）。
DEFAULT_BACK_COLOUR = "&H80000000"
#: 卡拉OK「未说出」字色（video-brain 同值，保持 Phase 2 输出一致）。
SECONDARY_COLOUR = "&H000000FF"
#: 14 字 × 44px = 616px（占 1080 宽的 57%，左右各余 232px）；旧值 18 字会顶到画布边缘。
DEFAULT_MAX_CHARS_PER_LINE = 14

SUBTITLE_DIRNAME = "subtitles"
META_FILENAME = "meta.json"
STYLE_NAME = "Speak"
STYLE_ID = "workbench_v1"
SCHEMA_VERSION = 1

REVISION_DIR_PATTERN = re.compile(r"^rev-(\d+)$")
CLIP_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
HEX_COLOR_PATTERN = re.compile(r"^#?[0-9A-Fa-f]{6}$")

_CONFIG_KEYS = (
    "font_size_px", "font_family", "text_color", "outline_color", "outline_width",
    "background", "position", "word_highlight", "max_chars_per_line",
)


class SubtitleComposeError(ValueError):
    """带 E_ 码的显式失败（沿用工作台 ValueError → HTTP 4xx 的既有映射）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


E_SUBTITLE_CONFIG_INVALID = "E_SUBTITLE_CONFIG_INVALID"
E_SUBTITLE_NO_CUES = "E_SUBTITLE_NO_CUES"


# ---------------------------------------------------------------------------
# 几何：与前端预览共用同一换算
# ---------------------------------------------------------------------------

def _number(value: Any, default: float) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def percent_to_pixels(
    position: dict[str, Any] | None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> dict[str, int]:
    """百分比位置 → 1080x1920 坐标系像素（**与前端字幕叠加层同一公式**）。

    ``x = round(width * x_pct / 100)``、``bottom = round(height * (100 - y_pct) / 100)``。
    默认 ``{"x_pct": 50, "y_pct": 78.125}`` → ``{"x": 540, "bottom": 420}``。

    纯几何助手：缺字段取默认、非数字取默认、越界钳到 0..100，不抛错
    （配置的严格校验在 :func:`normalize_config`）。
    """

    raw = position if isinstance(position, dict) else {}
    x_pct = _clamp(_number(raw.get("x_pct"), 50.0), 0.0, 100.0)
    y_pct = _clamp(_number(raw.get("y_pct"), DEFAULT_Y_PCT), 0.0, 100.0)
    return {
        "x": round(int(width) * x_pct / 100.0),
        "bottom": round(int(height) * (100.0 - y_pct) / 100.0),
    }


# ---------------------------------------------------------------------------
# 配置校验（plain dict 进，规范化 dict 出）
# ---------------------------------------------------------------------------

def _require_bool(value: Any, name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise SubtitleComposeError(E_SUBTITLE_CONFIG_INVALID, f"{name} 必须是布尔值，收到: {value!r}")
    return value


def _require_number(value: Any, name: str, default: float, low: float, high: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SubtitleComposeError(E_SUBTITLE_CONFIG_INVALID, f"{name} 必须是数字，收到: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise SubtitleComposeError(E_SUBTITLE_CONFIG_INVALID, f"{name} 必须是有限数字，收到: {value!r}")
    if number < low or number > high:
        raise SubtitleComposeError(
            E_SUBTITLE_CONFIG_INVALID, f"{name} 必须在 {low}..{high} 之间，收到: {number}")
    return number


def _require_int(value: Any, name: str, default: int, low: int, high: int) -> int:
    number = _require_number(value, name, float(default), float(low), float(high))
    return int(round(number))


def _require_color(value: Any, name: str, default: str) -> str:
    if value is None or value == "":
        return default
    if not isinstance(value, str) or not HEX_COLOR_PATTERN.match(value.strip()):
        raise SubtitleComposeError(
            E_SUBTITLE_CONFIG_INVALID, f"{name} 必须是 #RRGGBB，收到: {value!r}")
    return "#" + value.strip().lstrip("#").upper()


def normalize_config(raw: Any) -> dict[str, Any]:
    """合并默认 + 严格校验（类型/范围错 → ``E_SUBTITLE_CONFIG_INVALID``）。

    返回的字典只含 §2 ``subtitle_config`` 的已知键，且**已填满默认值**，
    因此 ``config_hash`` 对「缺省字段」与「显式写默认值」是同一个值。
    """

    config = raw if isinstance(raw, dict) else {}
    if raw is not None and not isinstance(raw, dict):
        raise SubtitleComposeError(
            E_SUBTITLE_CONFIG_INVALID, f"subtitle_config 必须是 JSON 对象，收到: {type(raw).__name__}")

    background_raw = config.get("background") if isinstance(config.get("background"), dict) else {}
    if config.get("background") is not None and not isinstance(config.get("background"), dict):
        raise SubtitleComposeError(E_SUBTITLE_CONFIG_INVALID, "background 必须是 JSON 对象")

    position_raw = config.get("position") if isinstance(config.get("position"), dict) else {}
    if config.get("position") is not None and not isinstance(config.get("position"), dict):
        raise SubtitleComposeError(E_SUBTITLE_CONFIG_INVALID, "position 必须是 JSON 对象")

    family = config.get("font_family")
    if family is None:
        family = ""
    if not isinstance(family, str):
        raise SubtitleComposeError(
            E_SUBTITLE_CONFIG_INVALID, f"font_family 必须是字符串，收到: {family!r}")

    normalized = {
        "font_size_px": _require_int(config.get("font_size_px"), "font_size_px", DEFAULT_FONT_SIZE, 1, 400),
        "font_family": family.strip(),
        "text_color": _require_color(config.get("text_color"), "text_color", DEFAULT_TEXT_COLOR),
        "outline_color": _require_color(config.get("outline_color"), "outline_color", DEFAULT_OUTLINE_COLOR),
        "outline_width": _require_int(config.get("outline_width"), "outline_width", DEFAULT_OUTLINE_WIDTH, 0, 12),
        "background": {
            "enabled": _require_bool(background_raw.get("enabled"), "background.enabled", False),
            "color": _require_color(background_raw.get("color"), "background.color", DEFAULT_BACKGROUND_COLOR),
            "opacity": _require_number(
                background_raw.get("opacity"), "background.opacity", DEFAULT_BACKGROUND_OPACITY, 0.0, 1.0),
        },
        "position": {
            "x_pct": _require_number(position_raw.get("x_pct"), "position.x_pct", 50.0, 0.0, 100.0),
            "y_pct": _require_number(position_raw.get("y_pct"), "position.y_pct", DEFAULT_Y_PCT, 0.0, 100.0),
        },
        "word_highlight": _require_bool(config.get("word_highlight"), "word_highlight", False),
        "max_chars_per_line": _require_int(
            config.get("max_chars_per_line"), "max_chars_per_line", DEFAULT_MAX_CHARS_PER_LINE, 0, 400),
    }
    assert set(normalized) == set(_CONFIG_KEYS)
    return normalized


def config_hash(config: dict[str, Any]) -> str:
    """规范化配置的 sha256（键排序、无空格），用于版本比对。"""

    payload = json.dumps(normalize_config(config), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 颜色 / 文本
# ---------------------------------------------------------------------------

def _hex_to_ass(value: str) -> str:
    """``#RRGGBB`` → ASS ``BBGGRR``（不含 &H 与 alpha）。"""

    value = (value or DEFAULT_TEXT_COLOR).strip().lstrip("#")
    red, green, blue = value[0:2], value[2:4], value[4:6]
    return f"{blue}{green}{red}".upper()


def _alpha_from_opacity(opacity: float) -> int:
    """不透明度 0..1 → ASS alpha 字节（``00`` 全不透明，``FF`` 全透明）。

    opacity=0.5 → ``80``（与 video-brain 的半透明黑 ``&H80000000`` 同档）。
    """

    return int(round((1.0 - _clamp(opacity, 0.0, 1.0)) * 255))


_ASS_ESCAPES = (("\\", "\\\\"), ("{", "\\{"), ("}", "\\}"))


def _escape_ass_text(value: str, *, strip: bool = True) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ")
    for needle, replacement in _ASS_ESCAPES:
        text = text.replace(needle, replacement)
    return text.strip() if strip else text


def _is_wide(char: str) -> bool:
    """CJK / 全角字符：每个字自己算一个 token（与前端逐字高亮一致）。"""

    return unicodedata.east_asian_width(char) in {"W", "F"}


def _tokenize(text: str) -> list[str]:
    """切词：空白分隔的西文词 + 每个 CJK 字符；空白并入前一个 token（保留可见空格）。"""

    tokens: list[str] = []
    buffer = ""
    for char in str(text or ""):
        if char.isspace():
            if buffer:
                tokens.append(buffer)
                buffer = ""
            if tokens:
                tokens[-1] += char
            continue
        if _is_wide(char):
            if buffer:
                tokens.append(buffer)
                buffer = ""
            tokens.append(char)
            continue
        buffer += char
    if buffer:
        tokens.append(buffer)
    return tokens


def _karaoke_spans(tokens: list[str], start: float, end: float) -> list[int]:
    """把 cue 时长（厘秒）**平均分配**给各 token（无词级时间戳，见模块 docstring）。

    用累积取整保证各 span 之和恰好等于总厘秒数（不会因为四舍五入丢/多时间）。
    """

    total_cs = max(0, int(round((float(end) - float(start)) * 100)))
    count = len(tokens)
    if count <= 0:
        return []
    return [
        round(total_cs * (index + 1) / count) - round(total_cs * index / count)
        for index in range(count)
    ]


# ---------------------------------------------------------------------------
# 确定性折行（宽度感知）
# ---------------------------------------------------------------------------
# 实测本机 ffmpeg 的 libass 对「纯 CJK 无空格长行」不做自动折行（WrapStyle 0
# 也会溢出画布），因此折行必须在这里显式完成（ASS 用 ``\N``，SRT 用换行）。

def _wrap_limit(config: dict[str, Any], width: int) -> int:
    """每行字符数上限 = min(配置 max_chars_per_line, (宽 − 2×左右边距) / 字号)。

    ``max_chars_per_line <= 0`` 表示显式关闭折行（返回 0）。
    """

    cap = int(config.get("max_chars_per_line") or 0)
    if cap <= 0:
        return 0
    size = int(config.get("font_size_px") or DEFAULT_FONT_SIZE)
    per_line = max(1, (int(width) - 2 * DEFAULT_MARGIN_LR) // max(1, size))
    return max(1, min(cap, per_line))


def _wrap_display_lines(text: str, limit: int) -> list[str]:
    """按字符预算确定性折行：CJK 逐字可折，ASCII 词保持完整；换行符视为硬折。"""

    if limit <= 0:
        return [str(text or "")]
    lines: list[str] = []
    for paragraph in str(text or "").splitlines() or [""]:
        current: list[str] = []
        used = 0
        index = 0
        while index < len(paragraph):
            char = paragraph[index]
            if char.isascii() and (char.isalnum() or char in ".,!?%:/+-"):
                end = index + 1
                while end < len(paragraph) and paragraph[end].isascii() and (
                        paragraph[end].isalnum() or paragraph[end] in ".,!?%:/+-"):
                    end += 1
                token = paragraph[index:end]
                index = end
            else:
                token = char
                index += 1
            if current and used + len(token) > limit:
                lines.append("".join(current))
                current, used = [token], len(token)
            else:
                current.append(token)
                used += len(token)
        lines.append("".join(current))
    return lines


def _fmt_ass_time(seconds: float) -> str:
    """``H:MM:SS.cc``（与 video-brain ``_fmt_ass`` 同格式）。"""

    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int(seconds % 3600 // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def _fmt_srt_time(seconds: float) -> str:
    """``HH:MM:SS,mmm``（与 video-brain ``_fmt_srt`` 同格式）。"""

    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int(seconds % 3600 // 60)
    whole = int(seconds % 60)
    milliseconds = int(round((seconds - int(seconds)) * 1000))
    if milliseconds >= 1000:  # 0.9999 秒这类四舍五入到 1000ms 的边界
        milliseconds = 999
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{milliseconds:03d}"


# ---------------------------------------------------------------------------
# cue 构建
# ---------------------------------------------------------------------------

def _optional_seconds(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return max(0.0, number)


def _cue_text(raw: Any) -> str:
    for key in ("text", "corrected_text", "raw_text"):
        value = raw.get(key)
        if value is not None:
            return str(value)
    return ""


def build_cues(
    segments: list[dict[str, Any]],
    *,
    overrides: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """段 → cue（``[{index,segment_id,start,end,text,speaker}]``，按 start 升序，空文本跳过）。

    ``overrides`` 按 ``segment_id`` 覆盖 ``text`` / ``start`` / ``end``（另支持
    ``speaker``）；带 ``start`` + ``end`` 却匹配不到任何 ``segment_id`` 的 override
    作为**额外 cue 追加**（缺失的 ``segment_id`` 补成 ``manual-001``）。
    覆盖发生在过滤空文本**之前**，所以「原文为空但有手工文本」的段也会成 cue。
    """

    base: list[dict[str, Any]] = []
    for index, raw in enumerate(segments or []):
        if not isinstance(raw, dict):
            continue
        segment_id = str(raw.get("segment_id") or raw.get("id") or "").strip()
        if not segment_id:
            segment_id = f"seg-{index + 1:03d}"
        base.append({
            "segment_id": segment_id,
            "start": _optional_seconds(raw.get("start")) or 0.0,
            "end": _optional_seconds(raw.get("end")) or 0.0,
            "text": _cue_text(raw),
            "speaker": str(raw.get("speaker") or ""),
        })

    known = {item["segment_id"] for item in base}
    patches: dict[str, dict[str, Any]] = {}
    extras: list[dict[str, Any]] = []
    for raw in overrides or []:
        if not isinstance(raw, dict):
            continue
        segment_id = str(raw.get("segment_id") or "").strip()
        start = _optional_seconds(raw.get("start"))
        end = _optional_seconds(raw.get("end"))
        text = raw.get("text")
        speaker = raw.get("speaker")
        if segment_id and segment_id in known:
            patch = patches.setdefault(segment_id, {})
            if text is not None:
                patch["text"] = str(text)
            if start is not None:
                patch["start"] = start
            if end is not None:
                patch["end"] = end
            if speaker is not None:
                patch["speaker"] = str(speaker)
            continue
        if start is not None and end is not None:
            extras.append({
                "segment_id": segment_id or f"manual-{len(extras) + 1:03d}",
                "start": start,
                "end": end,
                "text": str(text or ""),
                "speaker": str(speaker or ""),
            })

    cues: list[dict[str, Any]] = []
    for item in [*base, *extras]:
        merged = dict(item)
        merged.update(patches.get(item["segment_id"], {}))
        text = str(merged.get("text") or "").strip()
        if not text:
            continue
        start = float(merged["start"])
        end = float(merged["end"])
        if end <= start:
            end = start + 0.5   # 与 video-brain 一致：零长度 cue 至少显示 0.5s
        cues.append({
            "index": 0,
            "segment_id": merged["segment_id"],
            "start": start,
            "end": end,
            "text": text,
            "speaker": merged.get("speaker") or "",
        })

    cues.sort(key=lambda cue: cue["start"])
    for index, cue in enumerate(cues, 1):
        cue["index"] = index
    return cues


# ---------------------------------------------------------------------------
# ASS / SRT 文本
# ---------------------------------------------------------------------------

def _ass_overrides(cue: dict[str, Any], config: dict[str, Any], width: int, height: int,
                   margin_v: int) -> str:
    """该 cue 需要的内联 ASS 标签（无则空串）。

    * ``word_highlight`` → 一律保留 ``\\an2``（契约要求）；
    * ``x_pct != 50`` → 追加 ``\\pos``。
    """

    anchor_x = percent_to_pixels(config["position"], width, height)["x"]
    tags: list[str] = []
    if config["word_highlight"] or anchor_x != round(int(width) / 2):
        tags.append("\\an2")
    if anchor_x != round(int(width) / 2):
        tags.append(f"\\pos({anchor_x},{int(height) - int(margin_v)})")
    return "".join(tags)


def _dialogue_text(cue: dict[str, Any], config: dict[str, Any], width: int, height: int,
                   margin_v: int) -> str:
    inline = _ass_overrides(cue, config, width, height, margin_v)
    limit = _wrap_limit(config, width)
    if not config["word_highlight"]:
        prefix = f"{{{inline}}}" if inline else ""
        lines = _wrap_display_lines(cue["text"], limit)
        return prefix + "\\N".join(_escape_ass_text(line) for line in lines)

    tokens = _tokenize(cue["text"])
    if not tokens:
        prefix = f"{{{inline}}}" if inline else ""
        return prefix + _escape_ass_text(cue["text"])
    spans = _karaoke_spans(tokens, cue["start"], cue["end"])
    # 逐 token 分行（宽度预算同上），行间插 \N；\k 时长不受影响。
    token_lines: list[list[int]] = [[]]
    line_used = 0
    for index, token in enumerate(tokens):
        length = len(token)
        if limit > 0 and token_lines[-1] and line_used + length > limit:
            token_lines.append([index])
            line_used = length
        else:
            token_lines[-1].append(index)
            line_used += length
    blocks: list[str] = []
    first = True
    for group in token_lines:
        if not first:
            blocks.append("\\N")
        for index in group:
            tags = inline if first else ""
            blocks.append(f"{{{tags}\\k{spans[index]}}}" + _escape_ass_text(tokens[index], strip=False))
            first = False
    return "".join(blocks)


def ass_content(
    cues: list[dict[str, Any]],
    config: dict[str, Any],
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> str:
    """生成 ASS 文本（头与 video-brain ``app/subtitles.py`` 兼容，见模块 docstring）。"""

    normalized = normalize_config(config)
    pixels = percent_to_pixels(normalized["position"], width, height)
    margin_v = pixels["bottom"]
    background = normalized["background"]
    border_style = 3 if background["enabled"] else 1
    back_colour = DEFAULT_BACK_COLOUR
    if background["enabled"]:
        alpha = _alpha_from_opacity(background["opacity"])
        back_colour = f"&H{alpha:02X}{_hex_to_ass(background['color'])}"
    font_name = normalized["font_family"] or DEFAULT_FONT_NAME

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {int(width)}
PlayResY: {int(height)}
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: {STYLE_NAME},{font_name},{normalized['font_size_px']},&H00{_hex_to_ass(normalized['text_color'])},{SECONDARY_COLOUR},&H00{_hex_to_ass(normalized['outline_color'])},{back_colour},1,0,0,0,100,100,0,0,{border_style},{normalized['outline_width']},1,2,{DEFAULT_MARGIN_LR},{DEFAULT_MARGIN_LR},{margin_v},1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [head]
    for cue in cues or []:
        text = _dialogue_text(cue, normalized, width, height, margin_v)
        lines.append(
            f"Dialogue: 0,{_fmt_ass_time(cue['start'])},{_fmt_ass_time(cue['end'])},"
            f"{STYLE_NAME},,0,0,0,,{text}\n"
        )
    return "".join(lines)


def srt_content(cues: list[dict[str, Any]], config: dict[str, Any] | None = None,
                width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT) -> str:
    """生成 SRT 文本（编号按传入顺序，缺 ``index`` 时按位置补）。

    与 ASS 同一套确定性折行（真实换行符）；``config`` 缺省按默认画布样式算。
    """

    normalized = normalize_config(config)
    limit = _wrap_limit(normalized, width)
    out: list[str] = []
    for position, cue in enumerate(cues or [], 1):
        try:
            index = int(cue.get("index") or position)
        except (TypeError, ValueError):
            index = position
        text = str(cue.get("text") or "").strip()
        wrapped = "\n".join(_wrap_display_lines(text, limit)) if text else ""
        out.append(
            f"{index}\n{_fmt_srt_time(cue['start'])} --> {_fmt_srt_time(cue['end'])}\n{wrapped}\n\n"
        )
    return "".join(out)


# ---------------------------------------------------------------------------
# 版本目录：subtitles/rev-{NNN}
# ---------------------------------------------------------------------------

def subtitles_dir(clip_dir: Path) -> Path:
    return Path(clip_dir) / SUBTITLE_DIRNAME


def revision_dir(clip_dir: Path, revision: int) -> Path:
    return subtitles_dir(clip_dir) / f"rev-{int(revision):03d}"


def list_revisions(clip_dir: Path) -> list[dict[str, Any]]:
    """读各版本的 ``meta.json``，按 revision 升序；目录缺失/损坏时返回 ``[]``。"""

    directory = subtitles_dir(clip_dir)
    if not directory.is_dir():
        return []
    revisions: list[dict[str, Any]] = []
    for child in sorted(directory.iterdir(), key=lambda path: path.name):
        if not child.is_dir() or not REVISION_DIR_PATTERN.match(child.name):
            continue
        meta_path = child / META_FILENAME
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        try:
            meta["revision"] = int(meta.get("revision") or 0)
        except (TypeError, ValueError):
            continue
        meta.setdefault("dir", str(child))
        revisions.append(meta)
    revisions.sort(key=lambda meta: int(meta.get("revision") or 0))
    return revisions


def next_revision(clip_dir: Path) -> int:
    """下一个版本号 = ``max(已有)+1``（**永不重用**：重存一定产生新 rev）。"""

    highest = 0
    directory = subtitles_dir(clip_dir)
    if directory.is_dir():
        for child in directory.iterdir():
            match = REVISION_DIR_PATTERN.match(child.name)
            if match and child.is_dir():
                highest = max(highest, int(match.group(1)))
    for meta in list_revisions(clip_dir):
        highest = max(highest, int(meta.get("revision") or 0))
    return highest + 1


# ---------------------------------------------------------------------------
# 原子写 / compose
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.urandom(6).hex()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _finalize_cues(cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """compose 入口的 cue 归一化：只保留 ASS/SRT 需要的字段，按 start 排序后重编号。"""

    finalized: list[dict[str, Any]] = []
    for raw in cues or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        start = _optional_seconds(raw.get("start")) or 0.0
        end = _optional_seconds(raw.get("end")) or 0.0
        if end <= start:
            end = start + 0.5
        finalized.append({
            "index": 0,
            "segment_id": str(raw.get("segment_id") or ""),
            "start": start,
            "end": end,
            "text": text,
            "speaker": str(raw.get("speaker") or ""),
        })
    finalized.sort(key=lambda cue: cue["start"])
    for index, cue in enumerate(finalized, 1):
        cue["index"] = index
    return finalized


def _rollback_failed_write(directory: Path, existed: bool, paths: list[Path]) -> None:
    """失败清理：本次新建的版本目录整体删掉，否则只删本次可能写了一半的文件。"""

    if not existed:
        shutil.rmtree(directory, ignore_errors=True)
        return
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 清理失败不掩盖原始异常
            pass


def compose(
    clip_dir: Path,
    *,
    clip_id: str,
    cues: list[dict[str, Any]],
    config: dict[str, Any],
    revision: int | None = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    task_id: str | None = None,
) -> dict[str, Any]:
    """把一个字幕版本原子地写进 ``clip_dir/subtitles/rev-{NNN}/``。

    写 ``{clip_id}.ass`` / ``{clip_id}.srt`` / ``meta.json``；``revision`` 缺省取
    :func:`next_revision`（``max+1``，所以每次保存都是新版本，旧版本保留）。
    **任何一步失败都会清掉本次新建的版本目录**，不留半个文件。

    返回 ``{"revision","dir","ass_path","srt_path","cue_count","config_hash",
    "font_size_px","margin_v","style_id":"workbench_v1"}``（路径为字符串；
    ``meta.json`` 里同时记录相对 ``clip_dir`` 的 ``ass_rel`` / ``srt_rel``）。

    ``cues`` 为空 → ``E_SUBTITLE_NO_CUES``（没有内容就没有可追溯的字幕版本）。
    """

    prefix = str(clip_id or "").strip()
    if not CLIP_ID_PATTERN.match(prefix):
        raise SubtitleComposeError(E_SUBTITLE_CONFIG_INVALID, f"非法 clip_id: {clip_id!r}")

    normalized = normalize_config(config)
    finalized = _finalize_cues(list(cues or []))
    if not finalized:
        raise SubtitleComposeError(E_SUBTITLE_NO_CUES, "没有可合成的字幕 cue（文本为空？）")

    clip_dir = Path(clip_dir)
    target_revision = int(revision) if revision is not None else next_revision(clip_dir)
    directory = revision_dir(clip_dir, target_revision)
    ass_path = directory / f"{prefix}.ass"
    srt_path = directory / f"{prefix}.srt"
    meta_path = directory / META_FILENAME

    existed = directory.exists()
    directory.mkdir(parents=True, exist_ok=True)
    pixels = percent_to_pixels(normalized["position"], width, height)
    meta: dict[str, Any] = {}
    try:
        ass_text = ass_content(finalized, normalized, width, height)
        srt_text = srt_content(finalized, config=normalized, width=width, height=height)
        meta = {
            "schema_version": SCHEMA_VERSION,
            "revision": target_revision,
            "clip_id": prefix,
            "task_id": task_id,
            "created_at": _utc_now(),
            "cue_count": len(finalized),
            "config_hash": config_hash(normalized),
            "font_size_px": normalized["font_size_px"],
            "font_name": normalized["font_family"] or DEFAULT_FONT_NAME,
            "margin_v": pixels["bottom"],
            "margin_lr": DEFAULT_MARGIN_LR,
            "style_id": STYLE_ID,
            "style_name": STYLE_NAME,
            "width": int(width),
            "height": int(height),
            "word_highlight": normalized["word_highlight"],
            "position": dict(normalized["position"]),
            "config": normalized,
            "ass_path": str(ass_path),
            "srt_path": str(srt_path),
            "meta_path": str(meta_path),
            "ass_rel": str(ass_path.relative_to(clip_dir)),
            "srt_rel": str(srt_path.relative_to(clip_dir)),
        }
        _atomic_write_text(ass_path, ass_text)
        _atomic_write_text(srt_path, srt_text)
        _atomic_write_json(meta_path, meta)
    except BaseException:
        _rollback_failed_write(directory, existed, [ass_path, srt_path, meta_path])
        raise

    return {
        "revision": target_revision,
        "dir": str(directory),
        "ass_path": str(ass_path),
        "srt_path": str(srt_path),
        "meta_path": str(meta_path),
        "cue_count": len(finalized),
        "config_hash": meta["config_hash"],
        "font_size_px": normalized["font_size_px"],
        "margin_v": pixels["bottom"],
        "style_id": STYLE_ID,
        "created_at": meta["created_at"],
        "task_id": task_id,
    }
