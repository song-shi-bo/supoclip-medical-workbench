"""执行型 AI 剪辑助手（Studio Copilot）— 白名单工具调用 + 本地规则降级.

设计约束（与产品要求一致）：
- 模型只在后端调用；输出必须是结构化 Action JSON（pydantic 严格校验）。
- 所有 Action 类型属于白名单；时间、索引、成片 ID 在真实项目上下文中二次校验。
- 未配置模型或模型失败时降级为“本机规则助手”，绝不 500、绝不假装成功。
- set_speaker / export_active_clip / make_cover / render_clip 属于内容/外部影响类动作，
  只能以 ``execution="confirm"`` 返回，由用户在界面上点击确认后才执行。
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .ai import _build_transcript_model, _get_missing_llm_key_error
from .config import get_config

LLM_CALL_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "45"))
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|token|secret)\s*[=:]\s*\S+"),
)


def _sanitize_error(exc: Exception) -> str:
    """错误信息脱敏：绝不把密钥片段带进对话或日志。"""
    text = f"{type(exc).__name__}: {exc}"[:200]
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[已脱敏]", text)
    return text

# ---------------------------------------------------------------------------
# 白名单工具定义
# ---------------------------------------------------------------------------

# 纯工作区操作：用户明确指令即可执行
WORKSPACE_ACTION_TYPES = {
    "jump_to_time",
    "play",
    "pause",
    "select_source_media",
    "select_rendered_clip",
    "select_transcript_segment",
    "highlight_time_range",
    "extract_insights",
    "start_auto_edit",
    "rerender_auto_edit",
}
# 内容/外部影响类：必须用户在界面点击确认后才执行
CONFIRM_ACTION_TYPES = {"set_speaker", "export_active_clip", "make_cover", "adopt_cover",
                        "render_clip", "regenerate_cover", "update_title",
                        "update_subtitle_style", "update_timeline", "update_bgm"}
ALLOWED_ACTION_TYPES = WORKSPACE_ACTION_TYPES | CONFIRM_ACTION_TYPES

# ---------------------------------------------------------------------------
# 任务类型 ↔ 助手动作：改某条成片时**必须**路由到局部任务，禁止误跑 AUTO_EDIT
# ---------------------------------------------------------------------------
# 与 studio_local_tasks.TASK_* 一一对应（此处用字面量，避免 copilot 依赖重型模块）。
TASK_TYPE_AUTO_EDIT = "AUTO_EDIT"
TASK_TYPE_REGENERATE_COVER = "REGENERATE_COVER"
TASK_TYPE_UPDATE_TITLE = "UPDATE_TITLE"
TASK_TYPE_UPDATE_SUBTITLE_STYLE = "UPDATE_SUBTITLE_STYLE"
TASK_TYPE_UPDATE_TIMELINE = "UPDATE_TIMELINE"
TASK_TYPE_UPDATE_AUDIO = "UPDATE_AUDIO"

# 动作类型 → 落到后端的任务类型（缺省 = 纯前端工作区动作，不建后端任务）。
ACTION_TASK_TYPE = {
    "regenerate_cover": TASK_TYPE_REGENERATE_COVER,
    "make_cover": TASK_TYPE_REGENERATE_COVER,
    "update_title": TASK_TYPE_UPDATE_TITLE,
    "update_subtitle_style": TASK_TYPE_UPDATE_SUBTITLE_STYLE,
    "update_timeline": TASK_TYPE_UPDATE_TIMELINE,
    # BGM 音量/淡入淡出/移除 → 既有 UPDATE_AUDIO 局部任务（payload.audio_tracks
    # 由前端基于当前音轨状态计算；新 BGM 文件上传仍走 Inspector/音轨上传端点）。
    "update_bgm": TASK_TYPE_UPDATE_AUDIO,
}
TASK_TYPE_ZH = {
    TASK_TYPE_REGENERATE_COVER: "重新制作封面",
    TASK_TYPE_UPDATE_TITLE: "更新标题",
    TASK_TYPE_UPDATE_SUBTITLE_STYLE: "更新字幕样式",
    TASK_TYPE_UPDATE_TIMELINE: "更新时间线",
    TASK_TYPE_UPDATE_AUDIO: "更新音频/BGM",
}

# 封面风格白名单（与 cover_replace.SUPPORTED_COVER_STYLES 一致）。
# 成员两版风格：realframe_v2=第二版·真实帧大字封面（缺省），cutout_v3=第三版·人物抠像大字封面。
COVER_STYLE_IDS = ("realframe_v2", "cutout_v3")
DEFAULT_COVER_STYLE_ID = "realframe_v2"
COVER_STYLE_NAMES = {"realframe_v2": "第二版·真实帧大字封面",
                     "cutout_v3": "第三版·人物抠像大字封面"}

CONFIRM_HINTS = {
    "set_speaker": "说话人标签会影响字幕与文稿内容，请确认后执行。",
    "export_active_clip": "导出会生成一个对外文件，请确认后执行。",
    "make_cover": "按风格重制封面会替换该成片的封面图与 0.8s 片头（原文件先备份，可一键回滚），"
                  "不重新渲染正片，秒级完成，请确认后执行。",
    "adopt_cover": "沿用已生成的封面只替换封面图、不改片头（原文件先备份，可一键回滚），"
                   "请确认后执行。",
    "render_clip": "剪辑渲染会重新渲染该成片（旧成片保留），消耗本机渲染资源，请确认后执行。",
    "regenerate_cover": "只重新生成该成片的封面（封面图 + 0.8s 烧录片头），"
                        "按固化规则把主钩子压到 12 个汉字、最多两行；不重剪正片、不重转写、"
                        "不新建卡片。原封面先备份，可一键撤销到上一版，请确认后执行。",
    "update_title": "只更新该成片的标题/主题/关键词与封面文字（封面文字烧录在 0.8s 片头，"
                    "因此会连同封面一起重出，正片画面不变），请确认后执行。",
    "update_subtitle_style": "只更新该成片的字幕样式配置（为本 clip 建立新的 render_revision），"
                             "不重新剪辑视频，请确认后执行。",
    "update_timeline": "只修改当前成片的时间线片段（不影响原素材、其它成片与转写），"
                       "请确认后执行。",
    "update_bgm": "只调整该成片已上传 BGM 的音量/淡入淡出，或移除 BGM（不重剪正片；"
                  "混音在导出侧生效）；上传新的 BGM 文件请在 Inspector 效果页操作，"
                  "请确认后执行。",
}


class CopilotAction(BaseModel):
    """前端执行器可执行的最小指令单元。"""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    type: str
    label: str = Field(min_length=1, max_length=80)
    execution: Literal["run", "confirm"] = "run"
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, ge=0)
    index: int | None = Field(default=None, ge=0)
    task_id: str | None = None
    clip_id: str | None = None
    speaker: str | None = None
    note: str | None = Field(default=None, max_length=200)
    # 封面风格（make_cover 用）：realframe_v2 / cutout_v3；缺省 = 第二版真实帧
    style_id: str | None = None
    # 「沿用已生成的封面」（adopt_cover 用）：来源成片 clip_id，缺省 = 目标成片自身
    cover_source_clip_id: str | None = None
    # make_cover 是否同时替换 0.8s 片头（缺省 True）；沿用封面缺省 False
    replace_intro: bool | None = None
    # 局部任务类型（regenerate_cover / update_title / update_subtitle_style / update_timeline）
    task_type: str | None = None
    # 用户的原始修改要求：后端据此生成/压缩封面文字、解析字幕缩放与时间线删除区间。
    instruction: str | None = Field(default=None, max_length=1000)
    # 显式新标题（update_title 用）；缺省时后端按固化规则重排一版
    title: str | None = Field(default=None, max_length=200)
    # 时间线删除区间（update_timeline 用）：[[start, end], ...]
    remove_ranges: list[list[float]] | None = None
    # 字幕相对缩放（update_subtitle_style 用）
    font_scale: float | None = None
    # BGM 调整（update_bgm 用）：音量 0..1、淡入/淡出秒（0..10）、移除全部 BGM 轨。
    # 三者至少给一项；「换一首」需上传文件，动作提示里引导去效果页。
    bgm_volume: float | None = None
    bgm_fade_in_s: float | None = None
    bgm_fade_out_s: float | None = None
    bgm_remove: bool | None = None


class CopilotTurn(BaseModel):
    """POST /studio/copilot 请求体（宽松接收，服务端二次校验）。"""

    message: str = Field(min_length=1, max_length=2000)
    project_id: str | None = None
    current_time: float = Field(default=0, ge=0)
    duration: float = Field(default=0, ge=0)
    selected_segment_index: int | None = Field(default=None, ge=0)
    asr_segments: list[dict[str, Any]] = Field(default_factory=list, max_length=4000)
    rendered_clips: list[dict[str, Any]] = Field(default_factory=list, max_length=40)
    timeline: dict[str, Any] = Field(default_factory=dict)
    insights: dict[str, Any] = Field(default_factory=dict)
    task: dict[str, Any] = Field(default_factory=dict)
    viewing: Literal["source", "clip"] = "source"
    # 左侧**当前选中的成片**：助手始终绑定它（不再默认取列表最后一条）。
    active_clip_id: str | None = None
    active_task_id: str | None = None
    # 当前成片是否已经首次生成过成片（「开始自动剪辑」按钮的 gating 依据）。
    has_rendered_clips: bool = False


# 生成/渲染类动作：执行结果文本必须带规则版本，助手消息据此显示
# 「已按当前规则生成，规则版本：vX」（后端生成文案，前端只透传）。
RULE_VERSION_ACTION_TYPES = frozenset({
    "start_auto_edit", "rerender_auto_edit", "render_clip",
    "make_cover", "regenerate_cover", "adopt_cover", "update_title",
})


def current_rule_version() -> str:
    """当前生效的规则版本（读成员规则清单；读不到返回 unknown）。"""
    try:
        from . import skill_registry
        return str(skill_registry.load_manifest().get("workflow_version") or "unknown")
    except Exception:  # noqa: BLE001 — 版本不可读不能打断助手
        return "unknown"


def rule_version_line(version: str | None = None) -> str:
    """"已按当前规则生成，规则版本：vX" —— 供助手消息与动作结果直接展示。"""
    return f"已按当前规则生成，规则版本：v{version or current_rule_version()}"


def decorate_result_text(reply: str, actions: list[dict[str, Any]] | None,
                         version: str | None = None) -> str:
    """给生成类动作的结果文本补上规则版本（不重复追加）。"""
    kinds = {str((a or {}).get("type") or "") for a in (actions or [])}
    if not (kinds & RULE_VERSION_ACTION_TYPES):
        return reply
    line = rule_version_line(version)
    if line in reply:
        return reply
    return f"{reply}（{line}）"


class CopilotResult(BaseModel):
    reply: str = Field(min_length=1, max_length=2000)
    engine: Literal["llm", "rules"]
    notice: str | None = Field(default=None, max_length=300)
    actions: list[CopilotAction] = Field(default_factory=list, max_length=12)


# ---------------------------------------------------------------------------
# 上下文二次校验：模型输出永远不能直接落地
# ---------------------------------------------------------------------------

def _clamp_seconds(value: float | None, duration: float) -> float | None:
    if value is None:
        return None
    ceiling = duration if duration > 0 else max(value, 0)
    return round(max(0.0, min(float(value), ceiling)), 3)


def _validate_actions(
    raw_actions: list[Any], turn: CopilotTurn
) -> tuple[list[CopilotAction], list[str]]:
    """把模型输出转成可执行白名单动作；越界数据被修复或丢弃并记录原因。"""
    validated: list[CopilotAction] = []
    dropped: list[str] = []
    clip_keys = {
        (str(item.get("task_id")), str(item.get("clip_id")))
        for item in turn.rendered_clips
    }
    segment_count = len(turn.asr_segments)
    for raw in raw_actions[:8]:
        if isinstance(raw, BaseModel):
            raw = raw.model_dump(exclude_none=True)
        if not isinstance(raw, dict):
            dropped.append("丢弃了非对象动作")
            continue
        action_type = str(raw.get("type", "")).strip()
        if action_type not in ALLOWED_ACTION_TYPES:
            dropped.append(f"未知动作类型 {action_type or '(空)'}")
            continue
        label = str(raw.get("label") or action_type).strip()[:80]
        # 尊重归一化层给出的 execution（LLM 路径对 start_auto_edit 等强制 confirm），
        # 同时保证内容/外部影响类动作永远不可能降级为直接执行。
        requested = str(raw.get("execution") or "run")
        execution = "confirm" if (action_type in CONFIRM_ACTION_TYPES or requested == "confirm") else "run"
        action = CopilotAction(
            type=action_type,
            label=label,
            execution=execution,
            note=str(raw.get("note"))[:200] if raw.get("note") else None,
            speaker=str(raw["speaker"])[:8] if raw.get("speaker") else None,
        )
        if action_type in {"jump_to_time", "highlight_time_range"}:
            start = raw.get("start", raw.get("seconds"))
            if start is None:
                dropped.append(f"{action_type} 缺少时间点")
                continue
            action.start = _clamp_seconds(float(start), turn.duration)
            if raw.get("end") is not None:
                action.end = _clamp_seconds(float(raw["end"]), turn.duration)
            if action.end is not None and action.end <= (action.start or 0):
                action.end = None
        if action_type == "select_transcript_segment" or action_type == "set_speaker":
            index = raw.get("index", turn.selected_segment_index)
            if index is None:
                dropped.append(f"{action_type} 缺少片段序号")
                continue
            index = int(index)
            if index < 0 or index >= segment_count:
                dropped.append(f"片段序号 {index} 超出转写范围（共 {segment_count} 段）")
                continue
            action.index = index
        if action_type == "set_speaker":
            speaker = str(raw.get("speaker") or "").strip()
            if speaker not in {"医生", "患者", "未标注"}:
                dropped.append(f" unsupported speaker: {speaker or '(空)'}")
                continue
            action.speaker = speaker
        if action_type in {"select_rendered_clip", "make_cover", "adopt_cover", "render_clip",
                           "regenerate_cover", "update_title", "update_subtitle_style",
                           "update_timeline", "update_bgm"}:
            task_id = str(raw.get("task_id") or "")
            clip_id = str(raw.get("clip_id") or "")
            if not clip_id and action_type in ACTION_TASK_TYPE:
                # 局部任务作用在**选中的成片**上：模型漏给 clip_id 时用当前选中的那条，
                # 而不是随便挑一条，避免改错片子。
                active = _active_clip(turn)
                if active is not None:
                    task_id = str(active.get("task_id") or task_id)
                    clip_id = str(active.get("clip_id") or "")
            if (task_id, clip_id) not in clip_keys:
                dropped.append(f"成片 {clip_id or task_id or '(空)'} 不在当前项目成片列表中")
                continue
            action.task_id, action.clip_id = task_id, clip_id
        if action_type in ACTION_TASK_TYPE:
            action.task_type = ACTION_TASK_TYPE[action_type]
            instruction = str(raw.get("instruction") or "").strip()
            action.instruction = instruction[:1000] or None
        if action_type in {"make_cover", "regenerate_cover"}:
            # 风格白名单：模型给出非法风格时回落到缺省（第二版真实帧），
            # 不把非法值透传下去（唯一合法来源是模型输出，必须二次校验）。
            style = str(raw.get("style_id") or "").strip()
            action.style_id = style if style in COVER_STYLE_IDS else DEFAULT_COVER_STYLE_ID
            replace_intro = raw.get("replace_intro")
            action.replace_intro = bool(replace_intro) if isinstance(replace_intro, bool) else True
        if action_type == "update_title":
            title = str(raw.get("title") or "").strip()
            action.title = title[:200] or None
        if action_type == "update_subtitle_style":
            scale = raw.get("font_scale")
            if isinstance(scale, (int, float)) and not isinstance(scale, bool):
                action.font_scale = round(max(0.5, min(2.0, float(scale))), 3)
        if action_type == "update_bgm":
            # BGM 调整参数：volume 0..1、fade 0..10、remove 布尔；三者必须至少
            # 给一项，否则这条动作没有可执行语义，直接丢弃（宁缺毋滥）。
            volume = raw.get("bgm_volume", raw.get("volume"))
            if isinstance(volume, (int, float)) and not isinstance(volume, bool):
                action.bgm_volume = round(max(0.0, min(1.0, float(volume))), 3)
            fade_in = raw.get("bgm_fade_in_s", raw.get("fade_in_s"))
            if isinstance(fade_in, (int, float)) and not isinstance(fade_in, bool):
                action.bgm_fade_in_s = round(max(0.0, min(10.0, float(fade_in))), 3)
            fade_out = raw.get("bgm_fade_out_s", raw.get("fade_out_s"))
            if isinstance(fade_out, (int, float)) and not isinstance(fade_out, bool):
                action.bgm_fade_out_s = round(max(0.0, min(10.0, float(fade_out))), 3)
            remove = raw.get("bgm_remove", raw.get("remove"))
            if isinstance(remove, bool):
                action.bgm_remove = remove
            if (action.bgm_volume is None and action.bgm_fade_in_s is None
                    and action.bgm_fade_out_s is None and action.bgm_remove is None):
                dropped.append("update_bgm 缺少可执行参数（音量/淡入淡出/移除至少一项）")
                continue
        if action_type == "update_timeline":
            ranges = raw.get("remove_ranges") or raw.get("remove")
            cleaned: list[list[float]] = []
            for item in (ranges if isinstance(ranges, list) else [])[:8]:
                if isinstance(item, dict):
                    try:
                        low, high = float(item.get("start")), float(item.get("end"))
                    except (TypeError, ValueError):
                        continue
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    try:
                        low, high = float(item[0]), float(item[1])
                    except (TypeError, ValueError):
                        continue
                else:
                    continue
                if low < high:
                    cleaned.append([round(low, 3), round(high, 3)])
            action.remove_ranges = cleaned or None
        if action_type == "adopt_cover":
            # 沿用已生成的封面：来源成片必须也在当前项目成片列表里
            source_clip = str(raw.get("cover_source_clip_id") or raw.get("source_clip_id")
                              or "").strip()
            if source_clip and (action.task_id, source_clip) not in clip_keys:
                dropped.append(f"来源成片 {source_clip} 不在当前项目成片列表中")
                continue
            action.cover_source_clip_id = source_clip or action.clip_id
            replace_intro = raw.get("replace_intro")
            action.replace_intro = bool(replace_intro) if isinstance(replace_intro, bool) else False
        validated.append(action)
    return validated, dropped


# ---------------------------------------------------------------------------
# 本机规则助手（无模型时的完整降级实现）
# ---------------------------------------------------------------------------

_CN_DIGITS = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_number(text: str) -> int | None:
    """解析 1-99 的简体中文数字（含“十”组合），失败返回 None。"""
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total, seen = 0, False
    for char in text:
        if char == "十":
            total = (total or 1) * 10
            seen = True
        elif char in _CN_DIGITS:
            total += _CN_DIGITS[char]
            seen = True
        else:
            return None
    return total if seen else None


def _parse_time_expression(message: str) -> float | None:
    """支持 1分20秒 / 01:20 / 1:20 / 90秒 / 80 / 一分二十秒 / 开头 / 结尾。"""
    if re.search(r"开头|开始处|第一帧", message):
        return 0.0
    duration_match = re.search(r"结尾|最后", message)
    if duration_match:
        return -1.0  # 调用方用 -1 标记“跳到结尾”，随后按真实时长换算
    match = re.search(r"(\d{1,3}):([0-5]?\d)(?::([0-5]?\d))?", message)
    if match:
        seconds = int(match.group(1)) * 60 + int(match.group(2))
        if match.group(3):
            seconds = seconds * 60 + int(match.group(3))
        return float(seconds)
    match = re.search(r"(\d+(?:\.\d+)?)\s*分(?:钟)?\s*(?:(\d+(?:\.\d+)?)\s*秒?)?", message)
    if match:
        minutes = float(match.group(1))
        seconds = float(match.group(2)) if match.group(2) else 0.0
        return minutes * 60 + seconds
    match = re.search(r"(?:跳到|跳转|定位到|到)\s*(\d+(?:\.\d+)?)\s*秒?", message)
    if match:
        return float(match.group(1))
    cn = re.search(r"([零一二两三四五六七八九十]+)\s*分(?:钟)?\s*([零一二两三四五六七八九十]+)?\s*秒?", message)
    if cn:
        minutes = _cn_number(cn.group(1))
        seconds = _cn_number(cn.group(2) or "") or 0
        if minutes is not None:
            return minutes * 60 + seconds
    plain = re.search(r"(?:跳到|跳转|定位到)\s*(\d+(?:\.\d+)?)", message)
    if plain:
        return float(plain.group(1))
    return None


def _clip_index_from_message(message: str, clip_count: int) -> int | None:
    if clip_count <= 0:
        return None
    ordinal = re.search(r"第\s*([0-9一二两三四五六七八九十]+)\s*(?:个|条)?", message)
    if ordinal:
        index = _cn_number(ordinal.group(1))
        if index is not None and 1 <= index <= clip_count:
            return index - 1
    return 0 if re.search(r"(第一个成片|最新成片|成片一)", message) else None


_HELP_REPLY = (
    "我是本机规则助手，可以理解这些指令：跳到 1分20秒（并播放）、播放、暂停、显示原片、"
    "打开第 1 个成片、选中第 2 句、提取金句和关键词、找 3 个适合做短视频的片段、"
    "把当前这句标记为医生（需确认）、开始自动剪辑、重新按固定规则生成、"
    "用第二版风格重制封面（默认；说「用第三版/抠像风格」切到 cutout_v3）、"
    "沿用这条成片已生成的封面、重新渲染这个成片（需确认）、导出成片（需确认）、"
    "当前成片的主题是什么。配置 LLM 后可自由对话。"
)


def _active_clip(turn: CopilotTurn) -> dict[str, Any] | None:
    """左侧**当前选中**的成片；没有选中信息时退回列表最后一条（存量行为）。

    助手必须始终绑定用户选中的那条成片：否则「重新制作封面」会改到别的片子上，
    用户看到的卡片封面不变（本轮要修的核心体验问题）。
    """
    clips = turn.rendered_clips or []
    if not clips:
        return None
    if turn.active_clip_id:
        for clip in clips:
            if str(clip.get("clip_id") or "") == str(turn.active_clip_id):
                if not turn.active_task_id or str(clip.get("task_id") or "") == str(turn.active_task_id):
                    return clip
        for clip in clips:
            if str(clip.get("clip_id") or "") == str(turn.active_clip_id):
                return clip
    return clips[-1]


def _cover_style_from_message(message: str) -> str:
    """从自然语言里识别封面风格；识别不到就用缺省（第二版·真实帧）。

    「第二版」「真实帧」「v2」→ realframe_v2；「第三版」「抠像」「v3」→ cutout_v3；
    **否定表达优先**：「不要人物抠图 / 别用抠像」= 明确要求真实帧版（realframe_v2）。
    cutout_v3 依赖 u2net 权重，缺失时下游会显式报 E_CUTOUT_WEIGHTS_MISSING 与安装
    指引——规则助手不做静默降级，只负责如实翻译用户的意图。
    """
    lowered = message.lower()
    if re.search(r"(不要|别|不用|去掉|取消|不想).{0,6}(抠像|抠图|白描边|cutout)", message):
        return "realframe_v2"
    if re.search(r"第三版|第3版|三版|抠像|抠图|白描边|cutout|v3", message + lowered):
        return "cutout_v3"
    if re.search(r"第二版|第2版|两版|真实帧|真实感|realframe|v2", message + lowered):
        return "realframe_v2"
    return DEFAULT_COVER_STYLE_ID


def _title_from_message(message: str) -> str | None:
    """从指令里抽出显式新标题（「标题改成：xxx」/「标题叫『xxx』」）；抽不到返回 None。"""
    patterns = (
        r"(?:标题|题目|主题)\s*(?:改成|改为|换成|换成|叫做|叫|是|设为|设置成)\s*[:：]?\s*[「『\"“]?([^」』\"”\n，。；;]{2,24})",
        r"[「『\"“]([^」』\"”\n]{2,24})[」』\"”]\s*(?:作为|当做|当|做)\s*(?:标题|题目)",
    )
    for pattern in patterns:
        match = re.search(pattern, message)
        if match:
            title = match.group(1).strip()
            if title:
                return title[:200]
    return None


def _font_scale_from_message(message: str) -> float | None:
    """「字幕放大一点」→ 1.15；「字幕缩小到 0.9 倍」→ 0.9；识别不到返回 None。"""
    explicit = re.search(r"(\d+(?:\.\d+)?)\s*(?:倍|×|x)", message)
    if explicit:
        try:
            return round(max(0.5, min(2.0, float(explicit.group(1)))), 3)
        except ValueError:
            return None
    percent = re.search(r"(\d+)\s*%", message)
    if percent:
        return round(max(0.5, min(2.0, float(percent.group(1)) / 100)), 3)
    if re.search(r"放大|大一点|大点|更大", message):
        return 1.15
    if re.search(r"缩小|小一点|小点|更小", message):
        return 0.9
    return None


def _remove_ranges_from_message(message: str) -> list[list[float]]:
    """解析「删除第 8 秒到第 10 秒」→ [[8.0, 10.0]]；识别不到返回空列表。"""
    match = re.search(
        r"(?:删除|去掉|移除|剪掉)[^0-9]{0,12}(?:第)?\s*(\d+(?:\.\d+)?)\s*秒?"
        r"\s*(?:到|至|-|~|—|―)\s*(?:第)?\s*(\d+(?:\.\d+)?)\s*秒",
        message or "",
    )
    if not match:
        return []
    low, high = float(match.group(1)), float(match.group(2))
    if high <= low:
        return []
    return [[round(low, 3), round(high, 3)]]


BGM_KEYWORD_RE = re.compile(r"BGM|bgm|背景音乐|背景乐|配乐", re.I)


def _bgm_volume_from_message(message: str) -> float | None:
    """解析 BGM 音量意图：显式数值（0..1 或 百分数）优先， 否则小/大档位。"""
    text = message or ""
    explicit = re.search(r"音量[^0-9]{0,6}(\d+(?:\.\d+)?)\s*(%|％)?", text)
    if explicit:
        value = float(explicit.group(1))
        return round(min(1.0, value / 100.0 if explicit.group(2) else value), 3)
    if re.search(r"(小声|轻一点|轻些|小一点|低一点|调低|减小|弱一点)", text):
        return 0.6
    if re.search(r"(大声|响一点|高一点|调高|增强|强一点)", text):
        return 0.85
    return None


def _bgm_fade_from_message(message: str) -> tuple[float | None, float | None]:
    """解析「淡入 2 秒 / 淡出 1.5 秒」→ (fade_in, fade_out)；识别不到为 None。"""
    text = message or ""
    fade_in = fade_out = None
    match = re.search(r"淡入[^0-9]{0,6}(\d+(?:\.\d+)?)\s*秒?", text)
    if match:
        fade_in = round(max(0.0, min(10.0, float(match.group(1)))), 3)
    match = re.search(r"淡出[^0-9]{0,6}(\d+(?:\.\d+)?)\s*秒?", text)
    if match:
        fade_out = round(max(0.0, min(10.0, float(match.group(1)))), 3)
    if fade_in is None and re.search(r"加.{0,4}淡入|要淡入", text):
        fade_in = 1.5
    if fade_out is None and re.search(r"加.{0,4}淡出|要淡出|结尾.{0,4}淡出", text):
        fade_out = 2.0
    return fade_in, fade_out


def _bgm_remove_from_message(message: str) -> bool:
    """「去掉/移除/不要 BGM」→ True。"""
    return bool(re.search(r"(?:去掉|移除|删掉|删除|不要|关掉|取消)\s*(?:这?段?)?\s*"
                          r"(?:BGM|bgm|背景音乐|背景乐|配乐|音乐)", message or ""))


def _rule_based_copilot(turn: CopilotTurn) -> CopilotResult:
    message = turn.message.strip()
    lowered = message.lower()
    actions: list[CopilotAction] = []
    has_video = turn.duration > 0 or bool(turn.asr_segments)

    def action(action_type: str, label: str, **fields: Any) -> CopilotAction:
        return CopilotAction(type=action_type, label=label[:80], **fields)

    # —— 播放控制 ——
    if re.search(r"暂停|停止播放|别播", message):
        return CopilotResult(reply="已暂停播放。", engine="rules", actions=[action("pause", "暂停当前视频")])
    if re.fullmatch(r"\s*(播放|继续播放|play)\s*[。.。]?\s*", lowered) or re.fullmatch(r"\s*播放\s*", message):
        return CopilotResult(reply="已开始播放。", engine="rules", actions=[action("play", "播放当前视频")])

    # —— 时间跳转 ——
    # 「压缩到 12 字」「放大到 1.2 倍」这类资产修改指令里的「到」不是时间跳转，
    # 必须先排除，否则封面/标题/字幕/时间线的修改会被劫持成 seek。
    asset_edit = bool(re.search(r"封面|标题|题目|主题词|字幕|时间线|成片|BGM|bgm|背景音乐|配乐", message)) and \
        bool(re.search(r"封面|标题|题目|字幕|时间线|重|改|换|删|压|放大|缩小|不要|风格|小声|大声|音量|淡入|淡出|轻|响", message))
    target = None if asset_edit else _parse_time_expression(message)
    if target is not None and re.search(r"跳|定位|到|快进|回退|seek", message + lowered):
        if target < 0:
            target = max(0.0, turn.duration - 0.2) if turn.duration else 0.0
        elif turn.duration <= 0 and target >= 0:
            target = max(0.0, target)
        seek_action = action(
            "jump_to_time",
            f"跳到 {int(target // 60)} 分 {int(target % 60)} 秒",
            start=_clamp_seconds(target, turn.duration),
        )
        actions.append(seek_action)
        wants_play = bool(re.search(r"播放|播起来|play", message))
        if wants_play:
            actions.append(action("play", "播放当前视频"))
        reply = f"已跳到 {int(target // 60):02d}:{int(target % 60):02d}" + ("，并开始播放。" if wants_play else "。")
        if not has_video:
            reply = "当前还没有导入视频，跳转指令将在导入后可用。" if not turn.asr_segments else reply
        return CopilotResult(reply=reply, engine="rules", actions=actions)

    # —— 原片 / 成片切换 ——
    if re.search(r"原片|原始视频|源视频", message):
        return CopilotResult(reply="已切换回原始视频与原始时间线。", engine="rules", actions=[action("select_source_media", "显示原片")])
    # 问答类（主题/状态）必须在“成片切换”之前判断，否则被“成片”关键词劫持；
    # 但**改封面/标题/字幕/时间线**的指令虽然含“主题”二字，绝不能被当成绩问。
    if re.search(r"主题|讲的什么|内容是什么|关于什么", message) and not asset_edit:
        topics = [str(item.get("topic")) for item in turn.rendered_clips if item.get("topic")]
        if topics:
            return CopilotResult(reply="当前成片主题：" + "、".join(topics), engine="rules")
        keywords = turn.insights.get("keywords") or []
        if keywords:
            return CopilotResult(reply="根据关键词，当前素材围绕：" + "、".join(f"#{item}" for item in keywords[:6]), engine="rules")
        return CopilotResult(reply="还没有可总结的内容；完成转写后我再告诉你主题。", engine="rules")
    # 成片切换需要明确的“打开/切换”类动词，避免“导出成片”等短语误入。
    if re.search(r"(打开|切换|查看|载入|回到|看一?下?)\s*(第|最|成片|剪辑结果)", message) and re.search(r"成片|剪辑结果", message):
        clip_index = _clip_index_from_message(message, len(turn.rendered_clips))
        if clip_index is None:
            return CopilotResult(
                reply="当前还没有可打开的成片；可以先说“开始自动剪辑”。",
                engine="rules",
                actions=[action("start_auto_edit", "开始自动剪辑")] if turn.asr_segments else [],
            )
        clip = turn.rendered_clips[clip_index]
        topic = str(clip.get("topic") or "")
        return CopilotResult(
            reply=f"已打开{clip.get('name', '成片')}（{topic}，{clip.get('duration', 0)} 秒）。",
            engine="rules",
            actions=[
                action(
                    "select_rendered_clip",
                    f"打开{clip.get('name', '成片')}",
                    task_id=str(clip.get("task_id")),
                    clip_id=str(clip.get("clip_id")),
                )
            ],
        )

    # —— 转写片段 ——
    segment_match = re.search(r"(?:选中|定位|跳到|打开)?\s*第\s*([0-9一二两三四五六七八九十]+)\s*(?:句|段|条)", message)
    if segment_match and turn.asr_segments:
        index = _cn_number(segment_match.group(1))
        if index is not None and 1 <= index <= len(turn.asr_segments):
            segment = turn.asr_segments[index - 1]
            return CopilotResult(
                reply=f"已选中第 {index} 句：「{str(segment.get('text', ''))[:40]}」。", engine="rules",
                actions=[action("select_transcript_segment", f"选中第 {index} 句", index=index - 1)],
            )

    # —— 金句 / 关键词 ——
    if re.search(r"金句|关键词|highlight", lowered):
        actions.append(action("extract_insights", "提取金句和关键词"))
        if turn.insights.get("golden_sentences"):
            count = len(turn.insights["golden_sentences"])
            return CopilotResult(reply=f"正在重新提取金句与关键词（当前已有 {count} 条候选）。", engine="rules", actions=actions)
        return CopilotResult(reply="正在从当前转写文稿提取金句与关键词。", engine="rules", actions=actions)

    # —— 找适合短视频的片段 ——
    if re.search(r"找|推荐|适合短视频|哪些.*片段|几个片段", message):
        golden = turn.insights.get("golden_sentences") or []
        picked = [
            item for item in golden
            if isinstance(item, dict) and float(item.get("end", 0)) > float(item.get("start", 0))
        ][:3]
        if picked:
            spans = "、".join(
                f"「{str(item.get('text', ''))[:18]}」({float(item['start']):.0f}s)" for item in picked
            )
            actions = [
                action("highlight_time_range", f"定位 {float(item['start']):.0f}s 起的候选", start=float(item["start"]), end=float(item["end"]))
                for item in picked
            ]
            return CopilotResult(reply=f"结合金句候选，推荐这 {len(picked)} 段：{spans}。已逐个高亮定位。", engine="rules", actions=actions)
        if turn.asr_segments:
            return CopilotResult(
                reply="还没有金句候选，我先提取金句和关键词，再据此推荐片段。",
                engine="rules",
                actions=[action("extract_insights", "提取金句和关键词")],
            )
        return CopilotResult(reply="请先完成转写，我才能推荐适合短视频的片段。", engine="rules")

    # —— 说话人标注（必须确认） ——
    speaker_match = re.search(r"(?:标记|标注|设)(?:为|成|为)?\s*(医生|患者|未标注)", message)
    if speaker_match:
        speaker = speaker_match.group(1)
        index = turn.selected_segment_index
        if index is None or index < 0 or index >= len(turn.asr_segments):
            return CopilotResult(reply="请先在右侧转写列表中选中一句，我再把它标记为该身份。", engine="rules")
        return CopilotResult(
            reply=f"将把第 {index + 1} 句标记为「{speaker}」。{CONFIRM_HINTS['set_speaker']}",
            engine="rules",
            actions=[action("set_speaker", f"标记为{speaker}", execution="confirm", index=index, speaker=speaker)],
        )

    # —— 封面制作 / 单切片渲染（必须确认，作用于一个具体成片） ——
    # 「沿用已生成的封面」必须比「按风格重新生成封面」先判断：两者都命中
    # "封面" 关键词，但前者复用现有图、不调用生成链路。
    if re.search(r"沿用|复用|套用|使用.{0,6}(已生成|现有|原来的?)\s*封面|用.{0,6}(已生成|现有)的?封面", message):
        if not turn.rendered_clips:
            return CopilotResult(reply="还没有成片可套用封面；先完成一次自动剪辑。", engine="rules")
        clip = turn.rendered_clips[-1]
        name = str(clip.get("name") or "成片")
        return CopilotResult(
            reply=(f"将把「{name}」**已生成的封面图**套用为它的封面（复用现有图，"
                   f"不重新生成、不改片头）。{CONFIRM_HINTS['adopt_cover']}"),
            engine="rules",
            actions=[action(
                "adopt_cover", f"沿用{name}已生成的封面", execution="confirm",
                task_id=str(clip.get("task_id")), clip_id=str(clip.get("clip_id")),
                cover_source_clip_id=str(clip.get("clip_id")), replace_intro=False,
            )],
        )
    if (re.search(r"重新?生成封面|重做封面|重新?制作封面|重制封面|换.{0,6}封面|封面制作|做个封面|"
                  r"封面.{0,8}(太长|压缩|改|换)|封面主题", message)
            or ("封面" in message and re.search(r"不要|别用|不用|去掉|换成|换个|改|调|重|做|风格|"
                                                r"抠像|抠图|真实帧|第[二三]版|v[23]", message)
                and not re.search(r"封面.{0,6}(是什么|在哪|有没有|怎么样)", message))):
        if not turn.rendered_clips:
            return CopilotResult(reply="还没有成片可制作封面；先完成一次自动剪辑。", engine="rules")
        clip = _active_clip(turn)
        name = str(clip.get("name") or "成片")
        style_id = _cover_style_from_message(message)
        style_name = COVER_STYLE_NAMES.get(style_id, style_id)
        compress = bool(re.search(r"太长|压缩|不超过|不超过|字数|以内|12\s*个字|十二个字", message))
        plan = f"将按封面第三版规则重新生成，主题限制 12 字以内"
        if compress:
            plan = "将按固化封面规则重做主钩子：总长压到 12 个汉字以内、最多两行"
        # 结构化回答（任务书 §四）：1) 识别目标 → 2) 简短计划 → 3) 具体任务 → 6) 结果与撤销
        reply = (f"正在修改：{name}\n"
                 f"{plan}（风格：{style_name}）\n"
                 f"将创建 REGENERATE_COVER 局部任务：只换该成片的封面图与 0.8s 片头，"
                 f"不重剪正片、不重转写、不新建卡片。\n"
                 f"完成后原地刷新这张卡片的封面，并提供「撤销到上一版」。{CONFIRM_HINTS['regenerate_cover']}")
        return CopilotResult(
            reply=reply,
            engine="rules",
            actions=[action(
                "regenerate_cover", f"重新制作{name}的封面", execution="confirm",
                task_id=str(clip.get("task_id")), clip_id=str(clip.get("clip_id")),
                task_type=TASK_TYPE_REGENERATE_COVER, instruction=message,
                style_id=style_id, replace_intro=True,
            )],
        )
    # —— 标题 / 主题 / 封面文字（UPDATE_TITLE：只改文字，不重剪）——
    if re.search(r"标题|主题词|换个题目|重新?生成.{0,4}标题|改名", message) and \
            not re.search(r"自动剪辑|整片|全部成片", message):
        if not turn.rendered_clips:
            return CopilotResult(reply="还没有成片可改标题；先完成一次自动剪辑。", engine="rules")
        clip = _active_clip(turn)
        name = str(clip.get("name") or "成片")
        new_title = _title_from_message(message)
        detail = f"新标题「{new_title}」" if new_title else "按固化规则重排一版标题（≤12 汉字、最多两行）"
        return CopilotResult(
            reply=(f"正在修改：{name}\n"
                   f"将创建 UPDATE_TITLE 局部任务：{detail}；封面文字烧录在 0.8s 片头，"
                   f"因此会连同封面一起重出，正片画面逐帧不变。\n"
                   f"{CONFIRM_HINTS['update_title']}"),
            engine="rules",
            actions=[action(
                "update_title", f"更新{name}的标题", execution="confirm",
                task_id=str(clip.get("task_id")), clip_id=str(clip.get("clip_id")),
                task_type=TASK_TYPE_UPDATE_TITLE, instruction=message,
                title=new_title, style_id=_cover_style_from_message(message),
            )],
        )
    # —— 字幕样式（UPDATE_SUBTITLE_STYLE）——
    if re.search(r"字幕", message) and re.search(r"放大|缩小|大一点|小一点|字号|字体|样式|颜色|描边|位置", message):
        if not turn.rendered_clips:
            return CopilotResult(reply="还没有成片可调字幕；先完成一次自动剪辑。", engine="rules")
        clip = _active_clip(turn)
        name = str(clip.get("name") or "成片")
        scale = _font_scale_from_message(message)
        detail = f"字号 ×{scale:g}" if scale else "按指令更新样式配置"
        return CopilotResult(
            reply=(f"正在修改：{name}\n"
                   f"将创建 UPDATE_SUBTITLE_STYLE 局部任务：{detail}，只更新该成片的字幕样式"
                   f"并建立新的 render_revision，不重新剪辑视频。\n"
                   f"{CONFIRM_HINTS['update_subtitle_style']}"),
            engine="rules",
            actions=[action(
                "update_subtitle_style", f"更新{name}的字幕样式", execution="confirm",
                task_id=str(clip.get("task_id")), clip_id=str(clip.get("clip_id")),
                task_type=TASK_TYPE_UPDATE_SUBTITLE_STYLE, instruction=message,
                font_scale=scale,
            )],
        )
    # —— BGM 音量/淡入淡出/移除（UPDATE_AUDIO：只动 kind=bgm 的轨，原声不动）——
    if BGM_KEYWORD_RE.search(message) and not re.search(r"自动剪辑|整片|全部成片", message):
        if not turn.rendered_clips:
            return CopilotResult(reply="还没有成片可调 BGM；先完成一次自动剪辑。", engine="rules")
        clip = _active_clip(turn)
        name = str(clip.get("name") or "成片")
        remove = _bgm_remove_from_message(message)
        volume = None if remove else _bgm_volume_from_message(message)
        fade_in, fade_out = (None, None) if remove else _bgm_fade_from_message(message)
        if not remove and volume is None and fade_in is None and fade_out is None:
            if re.search(r"换|来一首|加点|加一段", message):
                return CopilotResult(
                    reply=(f"换/加新的 BGM 需要上传音频文件：请在 Inspector 效果页为「{name}」"
                           f"上传 BGM；上传后可以用自然语言让我调音量和淡入淡出。"),
                    engine="rules")
            return CopilotResult(
                reply=(f"想怎么调「{name}」的 BGM？可以说「BGM 小声一点」「淡入 2 秒」"
                       f"「去掉背景音乐」，我会创建 UPDATE_AUDIO 局部任务（只动 BGM 轨，"
                       f"不重剪正片，混音在导出侧生效）。"),
                engine="rules")
        if remove:
            detail = "移除全部 BGM 轨（原声保留）"
        else:
            parts = []
            if volume is not None:
                parts.append(f"音量 {volume:g}")
            if fade_in is not None:
                parts.append(f"淡入 {fade_in:g}s")
            if fade_out is not None:
                parts.append(f"淡出 {fade_out:g}s")
            detail = "、".join(parts) or "按指令更新音频配置"
        return CopilotResult(
            reply=(f"正在修改：{name}\n"
                   f"将创建 UPDATE_AUDIO 局部任务：{detail}，只动该成片的 BGM 轨"
                   f"（不重剪正片；混音在导出侧生效）。\n"
                   f"{CONFIRM_HINTS['update_bgm']}"),
            engine="rules",
            actions=[action(
                "update_bgm", f"调整{name}的BGM", execution="confirm",
                task_id=str(clip.get("task_id")), clip_id=str(clip.get("clip_id")),
                task_type=TASK_TYPE_UPDATE_AUDIO, instruction=message,
                **({"bgm_remove": True} if remove else {}),
                **({} if volume is None else {"bgm_volume": volume}),
                **({} if fade_in is None else {"bgm_fade_in_s": fade_in}),
                **({} if fade_out is None else {"bgm_fade_out_s": fade_out}),
            )],
        )
    # —— 时间线片段（UPDATE_TIMELINE）——
    remove_ranges = _remove_ranges_from_message(message)
    if remove_ranges or re.search(r"删除.{0,6}秒|剪掉.{0,6}秒|去掉.{0,6}段|时间线", message):
        if not turn.rendered_clips:
            return CopilotResult(reply="还没有成片可改时间线；先完成一次自动剪辑。", engine="rules")
        clip = _active_clip(turn)
        name = str(clip.get("name") or "成片")
        if not remove_ranges:
            return CopilotResult(
                reply=(f"正在修改：{name}\n"
                       f"想改时间线的哪一段？可以说「删除第 8 秒到第 10 秒」这样带秒数的区间，"
                       f"我再创建 UPDATE_TIMELINE 局部任务（只影响这一条成片，不动原素材与转写）。"),
                engine="rules")
        spans = "、".join(f"{low:g}s–{high:g}s" for low, high in remove_ranges)
        return CopilotResult(
            reply=(f"正在修改：{name}\n"
                   f"将创建 UPDATE_TIMELINE 局部任务：删除 {spans}，只影响这一条成片的时间线，"
                   f"不改原素材、其它成片与转写。\n"
                   f"{CONFIRM_HINTS['update_timeline']}"),
            engine="rules",
            actions=[action(
                "update_timeline", f"修改{name}的时间线", execution="confirm",
                task_id=str(clip.get("task_id")), clip_id=str(clip.get("clip_id")),
                task_type=TASK_TYPE_UPDATE_TIMELINE, instruction=message,
                remove_ranges=remove_ranges,
            )],
        )
    if re.search(r"重新?渲染这?[条个版]成片|重渲染这?[条个版]?成片|重[渲剪]这?[条个版]?成片", message):
        if not turn.rendered_clips:
            return CopilotResult(reply="还没有成片可重新渲染；先完成一次自动剪辑。", engine="rules")
        clip = _active_clip(turn)
        name = str(clip.get("name") or "成片")
        return CopilotResult(
            reply=f"将为「{name}」创建单切片渲染任务（旧成片保留）。{CONFIRM_HINTS['render_clip']}",
            engine="rules",
            actions=[action(
                "render_clip", f"重新渲染{name}", execution="confirm",
                task_id=str(clip.get("task_id")), clip_id=str(clip.get("clip_id")),
            )],
        )

    # —— 固定剪辑流程（AUTO_EDIT：只在**尚未生成成片**时可用）——
    if re.search(r"重新?(生成|剪辑|渲染)|按.*规则.*生成|再来一版", message):
        if turn.rendered_clips or turn.has_rendered_clips:
            # 已经有成片：绝不再重跑自动剪辑（那会多出重复卡片）。改文案/封面/字幕/时间线
            # 必须走局部任务，这里如实引导而不是乱触发重剪。
            return CopilotResult(
                reply=("当前项目已经生成过成片，我不会再重跑自动剪辑（那会多出一张重复卡片）。"
                       "请说明要改什么：重新制作封面 / 改标题 / 调字幕 / 改时间线，"
                       "我会创建对应的局部任务。"),
                engine="rules")
        if turn.task.get("task_id"):
            return CopilotResult(reply="正在按现有固定规则创建复渲染任务，旧成片会保留。", engine="rules", actions=[action("rerender_auto_edit", "按固定规则重新生成")])
        return CopilotResult(reply="还没有可复渲染的任务；我先创建一次新的自动剪辑。", engine="rules", actions=[action("start_auto_edit", "开始自动剪辑")])
    if re.search(r"自动剪辑|开始剪辑|剪一版|出片", message):
        if turn.rendered_clips or turn.has_rendered_clips:
            return CopilotResult(
                reply=("已经生成过成片了；要改封面/标题/字幕/时间线请在左侧选中对应的成片后直接说，"
                       "我会创建局部任务，不会重跑整段自动剪辑。"),
                engine="rules")
        if turn.viewing == "clip":
            return CopilotResult(reply="当前正在查看成片；请先切回原片（说“显示原片”）再开始新的自动剪辑。", engine="rules", actions=[action("select_source_media", "显示原片")])
        if turn.asr_segments:
            return CopilotResult(
                reply="已把当前时间轴、转写与金句交给本机固定剪辑流程，进度会在右侧任务面板更新。",
                engine="rules",
                actions=[action("start_auto_edit", "开始自动剪辑")],
            )
        return CopilotResult(reply="请先完成转写，自动剪辑会使用当前时间轴与转写文本。", engine="rules")

    # —— 导出（必须确认） ——
    if re.search(r"导出|下载成片", message):
        if not turn.rendered_clips:
            return CopilotResult(reply="还没有成片可导出；先完成一次自动剪辑。", engine="rules")
        return CopilotResult(reply=f"准备导出当前成片。{CONFIRM_HINTS['export_active_clip']}", engine="rules", actions=[action("export_active_clip", "导出当前成片", execution="confirm")])

    # —— 问答：任务状态（主题已在前面处理） ——
    if re.search(r"任务状态|进度|剪到哪", message):
        status = turn.task.get("status")
        if status:
            return CopilotResult(
                reply=f"当前自动剪辑任务 {turn.task.get('task_id', '')}：{turn.task.get('stage', status)}（{round(float(turn.task.get('progress', 0)) * 100)}%）。",
                engine="rules",
            )
        return CopilotResult(reply="当前没有进行中的自动剪辑任务。", engine="rules")
    if re.search(r"你能做什么|帮助|help|指令", lowered):
        return CopilotResult(reply=_HELP_REPLY, engine="rules")

    return CopilotResult(reply="这条我还不能安全执行。" + _HELP_REPLY, engine="rules")


# ---------------------------------------------------------------------------
# LLM 路径（pydantic-ai，结构化输出）
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """你是医疗科普短视频工作台里的执行型剪辑助手。

只能输出 JSON：{"reply": "...", "actions": [...], "requires_confirmation": false}
每个 action 形如 {"tool": "...", "arguments": {...}, "execution": "run|confirm", "label": "简短中文"}。

工具白名单（tool 只能取以下值，arguments 字段固定）：
- jump_to_time: {"seconds": 数值} —— 跳转播放位置（0 ≤ seconds ≤ duration）
- play / pause: {}
- select_source_media: {}
- select_rendered_clip: {"task_id": "...", "clip_id": "..."} —— 只能用 provided_clips 里真实存在的 ID
- select_transcript_segment: {"index": 整数} —— 0 基，范围 0..segment_count-1
- highlight_time_range: {"start": 秒, "end": 秒}
- extract_insights: {} —— 提取金句与关键词
- start_auto_edit: {} —— 启动项目既有固定剪辑流程；execution 必须为 "confirm"，requires_confirmation 必须 true
- rerender_auto_edit: {}
- make_cover: {"task_id": "...", "clip_id": "...", "style_id": "realframe_v2"|"cutout_v3", "replace_intro": true|false} —— 按封面风格重制指定成片的封面（封面图 + 0.8s 片头；style_id 缺省=realframe_v2 第二版·真实帧大字封面；用户说 v3/抠像才传 cutout_v3，其权重缺失时下游会显式报错，不得因此改用别的风格）；ID 只能用 provided_clips 里真实存在的组合；execution 必须为 "confirm"，requires_confirmation 必须 true
- adopt_cover: {"task_id": "...", "clip_id": "...", "cover_source_clip_id": "..."} —— 沿用某条成片**已生成**的 cover.jpg 作为目标成片的封面（纯复用，不重新生成、默认不改片头；replace_intro=true 才换片头）；用户说「沿用/复用/套用已生成的封面」时用它，ID 约束同上；execution 必须为 "confirm"，requires_confirmation 必须 true
- render_clip: {"task_id": "...", "clip_id": "..."} —— 对指定成片重新渲染（单切片渲染，旧成片保留）；ID 约束同上；execution 必须为 "confirm"，requires_confirmation 必须 true
- regenerate_cover: {"task_id": "...", "clip_id": "...", "style_id": "realframe_v2"|"cutout_v3", "replace_intro": true|false, "instruction": "用户原话"} —— **只在已有成片上改封面**：创建 REGENERATE_COVER 局部任务，只换该成片的封面图与 0.8s 烧录片头，按固化规则把主钩子压到 12 个汉字、最多两行；**不重剪正片、不重转写、不新建卡片**；用户说「重新制作封面 / 重做封面 / 封面主题太长压缩到 12 字内 / 封面不要人物抠图（→ realframe_v2）」时用它；style_id 缺省 realframe_v2，用户明确说第三版/抠像才传 cutout_v3（缺 u2net 权重会显式失败，不得偷偷换风格）；execution 必须为 "confirm"，requires_confirmation 必须 true
- update_title: {"task_id": "...", "clip_id": "...", "title": "新标题（可选）", "instruction": "用户原话"} —— 创建 UPDATE_TITLE 局部任务，只更新该成片的标题/主题/关键词与封面文字（封面文字烧录在 0.8s 片头，故会连同封面重出），**不重剪视频**；用户说「改标题 / 重新生成这一条成片的标题」时用它；execution 必须为 "confirm"，requires_confirmation 必须 true
- update_subtitle_style: {"task_id": "...", "clip_id": "...", "font_scale": 1.15, "instruction": "用户原话"} —— 创建 UPDATE_SUBTITLE_STYLE 局部任务，只更新该成片的字幕样式配置并建立新的 render_revision，**不重新剪辑视频**；用户说「字幕放大一点 / 字号小一点」时用它（放大≈1.15、缩小≈0.9）；execution 必须为 "confirm"，requires_confirmation 必须 true
- update_bgm: {"task_id": "...", "clip_id": "...", "bgm_volume": 0.6, "bgm_fade_in_s": 1.5, "bgm_fade_out_s": 2.0, "bgm_remove": false, "instruction": "用户原话"} —— 创建 UPDATE_AUDIO 局部任务，只调整该成片**已上传 BGM** 的音量（0..1，「小声一点」≈0.6）/淡入淡出秒数，或移除全部 BGM（bgm_remove=true）；参数至少给一项；**换新 BGM 文件或还没上传过 BGM 时不发这个动作**，改为文字引导用户在 Inspector 效果页上传；混音在导出侧生效；execution 必须为 "confirm"，requires_confirmation 必须 true
- update_timeline: {"task_id": "...", "clip_id": "...", "remove_ranges": [[8.0, 10.0]], "instruction": "用户原话"} —— 创建 UPDATE_TIMELINE 局部任务，只改当前成片的时间线片段（删除指定秒区间），不影响原素材、其它成片与转写；用户说「删除第 8 秒到第 10 秒」时用它；execution 必须为 "confirm"，requires_confirmation 必须 true
- set_speaker: {"index": 整数, "speaker": "医生"|"患者"|"未标注"} —— execution 必须为 "confirm"
- export_active_clip: {} —— execution 必须为 "confirm"，requires_confirmation 必须 true

规则：
1. 你是医疗短视频剪辑助手，只做剪辑工作区操作；禁止 Shell/Docker/SQL/任意文件或网络操作，你没有这些工具。
2. 医疗红线：不改写诊断、药名、剂量、病情结论或否定词；不凭语气推断说话人是医生还是患者。
3. 所有改动成片资产的动作（regenerate_cover、update_title、update_subtitle_style、update_timeline、make_cover、adopt_cover、render_clip、start_auto_edit、export_active_clip、set_speaker）永远返回 confirm（用户在界面点击确认后才真正执行）。
4. 时间一律用秒且必须在 duration 内；成片 ID 必须来自 provided_clips；不要发明工具或字段。
5. actions 最多 5 个、按执行顺序；无法安全完成时返回空 actions 并在 reply 说明原因。
6. 封面：用户没指定版本时用第二版真实帧风格（style_id="realframe_v2"）；只有用户明确说第三版/抠像时才用 "cutout_v3"（缺 u2net 权重时会显式失败，不要为了成功而偷偷换风格）；用户说「沿用/复用已生成的封面」时用 adopt_cover，不要用 make_cover；用户说「不要人物抠图/不要抠像」= 明确要 realframe_v2。
7. **已经有成片时绝不再启动自动剪辑**：改封面用 regenerate_cover、改标题用 update_title、改字幕用 update_subtitle_style、改时间线用 update_timeline。start_auto_edit / rerender_auto_edit 只在 provided_state 显示尚未生成过成片时才可用，否则必须返回空 actions 并说明原因（重跑会自动剪辑并多出一张重复卡片）。
8. reply 结构：1) 识别目标（例："正在修改：剪辑成片 01"）2) 简短计划（例："将按封面第三版规则重新生成，主题限制 12 字以内"）3) 说明创建的局部任务类型 4) 完成后会原地刷新同一张卡片的封面。
"""


class _LlmCopilotAction(BaseModel):
    """模型输出的动作（Agent 协议：tool + arguments + execution）。"""

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    execution: Literal["run", "confirm"] = "run"
    requires_confirmation: bool = False
    label: str | None = Field(default=None, max_length=80)


class _LlmCopilotTurn(BaseModel):
    """模型的结构化输出容器（严格校验，字段缺失/类型错误会触发重试）。"""

    reply: str = Field(min_length=1, max_length=1200)
    actions: list[_LlmCopilotAction] = Field(default_factory=list, max_length=8)
    requires_confirmation: bool = False


def _context_block(turn: CopilotTurn) -> str:
    """构建受限上下文：就近转写窗口（不全量发送）、金句、成片与任务摘要。"""
    clips = [
        {
            "task_id": item.get("task_id"),
            "clip_id": item.get("clip_id"),
            "name": item.get("name"),
            "topic": item.get("topic"),
            "duration": item.get("duration"),
        }
        for item in turn.rendered_clips[:10]
    ]
    task_summary = {key: turn.task.get(key) for key in ("task_id", "status", "stage", "progress") if turn.task.get(key) is not None}
    golden = [
        {"start": item.get("start"), "end": item.get("end"), "text": str(item.get("text", ""))[:40]}
        for item in (turn.insights.get("golden_sentences") or [])[:5]
        if isinstance(item, dict)
    ]
    keywords = [str(item) for item in (turn.insights.get("keywords") or [])[:6]]

    # 转写限长：优先当前播放/选中位置附近的窗口，最多 12 段，总量截断。
    segments = [item for item in turn.asr_segments if str(item.get("text", "")).strip()]
    focus = turn.selected_segment_index
    if focus is None or not (0 <= focus < len(segments)):
        focus = 0
        for index, item in enumerate(segments):
            if float(item.get("start", 0)) <= turn.current_time < float(item.get("end", 0)):
                focus = index
                break
    window: list[dict[str, Any]] = []
    if segments:
        half = 6
        lo = max(0, focus - half)
        hi = min(len(segments), focus + half + 1)
        window = [
            {"index": i, "start": round(float(segments[i].get("start", 0)), 1), "end": round(float(segments[i].get("end", 0)), 1), "text": str(segments[i].get("text", ""))[:40]}
            for i in range(lo, hi)
        ]

    parts = [
        f"user_message: {turn.message}",
        f"viewing: {turn.viewing}",
        f"current_time_seconds: {round(turn.current_time, 2)}",
        f"duration_seconds: {round(turn.duration, 2)}",
        f"segment_count: {len(segments)}（仅提供就近窗口，需要更多时用 select_transcript_segment 定位）",
        f"selected_segment_index: {turn.selected_segment_index}",
    ]
    if window:
        parts.append(f"transcript_window: {window}")
    if clips:
        parts.append(f"provided_clips: {clips}")
    # 左侧当前选中的成片：所有「改这条成片」的动作默认作用在它身上。
    active = _active_clip(turn)
    if active is not None:
        parts.append(
            "active_clip（左侧当前选中的成片，未显式指定时用它）: "
            f"{active.get('clip_id')} / {active.get('name')} / task_id={active.get('task_id')} / "
            f"封面版本 v{(active.get('cover') or {}).get('revision') if isinstance(active.get('cover'), dict) else '?'}")
    parts.append(
        f"has_rendered_clips: {bool(turn.rendered_clips or turn.has_rendered_clips)}"
        "（True 时禁止 start_auto_edit / rerender_auto_edit，只能建局部任务）")
    if task_summary:
        parts.append(f"auto_edit_task: {task_summary}")
    if golden:
        parts.append(f"golden_sentences: {golden}")
    if keywords:
        parts.append(f"keywords: {keywords}")
    block = "\n".join(parts)
    return block[:4000]


# LLM 产出的这些工具，无论模型如何声明，都必须经用户确认后才执行。
_LLM_CONFIRM_REQUIRED = {"set_speaker", "export_active_clip", "start_auto_edit", "make_cover",
                         "adopt_cover", "render_clip", "regenerate_cover", "update_title",
                         "update_subtitle_style", "update_timeline"}


def _normalize_llm_actions(turn: CopilotTurn, actions: list[_LlmCopilotAction], requires_confirmation: bool) -> tuple[list[dict[str, Any]], list[str]]:
    """把 Agent 协议（tool/arguments）归一化为内部白名单动作，并做二次校验。"""
    dropped: list[str] = []
    normalized: list[dict[str, Any]] = []
    clip_keys = {
        (str(item.get("task_id")), str(item.get("clip_id")))
        for item in turn.rendered_clips
    }
    segment_count = len(turn.asr_segments)
    arguments_map = {
        "jump_to_time": {"seconds"},
        "highlight_time_range": {"start", "end"},
        "select_rendered_clip": {"task_id", "clip_id"},
        "make_cover": {"task_id", "clip_id", "style_id", "replace_intro"},
        "adopt_cover": {"task_id", "clip_id", "cover_source_clip_id", "source_clip_id",
                        "replace_intro"},
        "render_clip": {"task_id", "clip_id"},
        "regenerate_cover": {"task_id", "clip_id", "style_id", "replace_intro", "instruction"},
        "update_title": {"task_id", "clip_id", "title", "instruction", "style_id"},
        "update_subtitle_style": {"task_id", "clip_id", "font_scale", "instruction"},
        "update_timeline": {"task_id", "clip_id", "remove_ranges", "instruction"},
        "select_transcript_segment": {"index"},
        "set_speaker": {"index", "speaker"},
    }
    for item in actions[:8]:
        tool = str(item.tool or "").strip()
        if tool not in ALLOWED_ACTION_TYPES:
            dropped.append(f"未知工具 {tool or '(空)'}")
            continue
        arguments = dict(item.arguments or {})
        allowed_keys = arguments_map.get(tool)
        if allowed_keys is not None:
            unknown = [key for key in arguments if key not in allowed_keys]
            if unknown:
                dropped.append(f"{tool} 收到未知参数 {','.join(unknown[:2])}")
            arguments = {key: value for key, value in arguments.items() if key in allowed_keys}
        execution: str = "confirm" if (tool in _LLM_CONFIRM_REQUIRED or item.requires_confirmation or requires_confirmation) else str(item.execution or "run")
        if execution not in {"run", "confirm"}:
            execution = "run"
        if tool in CONFIRM_ACTION_TYPES:
            execution = "confirm"
        normalized.append({
            "type": tool,
            "label": (item.label or tool)[:80],
            "execution": execution,
            **arguments,
        })
    validated, more_dropped = _validate_actions(normalized, turn)
    dropped.extend(more_dropped)
    return validated, dropped


async def _llm_copilot(turn: CopilotTurn) -> CopilotResult:
    config = get_config()
    configuration_error = _get_missing_llm_key_error(config.llm, config)
    if configuration_error:
        raise RuntimeError(configuration_error)
    import asyncio as _asyncio
    from pydantic_ai import Agent  # 延迟导入，未配置模型时不产生开销

    agent: Agent[None, _LlmCopilotTurn] = Agent(
        model=_build_transcript_model(config),
        output_type=_LlmCopilotTurn,
        system_prompt=_SYSTEM_PROMPT,
        output_retries=2,
    )
    result = await _asyncio.wait_for(
        agent.run(_context_block(turn)),
        timeout=LLM_CALL_TIMEOUT_SECONDS,
    )
    validated, dropped = _normalize_llm_actions(turn, result.output.actions, result.output.requires_confirmation)
    notice = f"已忽略模型输出中不安全/越界的部分：{'；'.join(dropped[:3])}。" if dropped else None
    return CopilotResult(reply=str(result.output.reply)[:1200], engine="llm", notice=notice, actions=validated)


async def run_copilot(turn: CopilotTurn) -> dict[str, Any]:
    """主入口：优先 LLM，失败或未配置一律降级为规则助手（不抛出、不 500）。"""
    try:
        result = await _llm_copilot(turn)
        if not result.actions and re.search(r"跳|播放|暂停|成片|金句|剪辑|导出|标记", turn.message):
            # 模型只回了话没有动作时，规则引擎兜底一次，保证“执行型”体验。
            fallback = _rule_based_copilot(turn)
            if fallback.actions:
                fallback.reply = result.reply
                fallback.notice = "模型未给出可执行动作，已由规则助手补齐。"
                result = fallback
        return result.model_dump()
    except Exception as exc:  # noqa: BLE001 —— 任何模型侧失败都不允许打断工作台
        fallback = _rule_based_copilot(turn)
        reason = _sanitize_error(exc) if "_sanitize_error" in globals() else str(exc)[:120]
        fallback.notice = f"模型暂不可用，已切换为规则模式（{reason}）。"
        return fallback.model_dump()
