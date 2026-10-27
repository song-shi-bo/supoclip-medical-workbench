"""Inspector Phase 1 —— clip / 源片级配置层（默认值、严格校验、部分更新）。

职责边界
--------
* **默认值与校验**只在这里：``DEFAULT_SUBTITLE_CONFIG`` / ``DEFAULT_EFFECTS`` 是唯一事实源；
  ``studio_project_store`` 通过 ``with_clip_defaults`` 做投影（本模块可以 import store，
  store 只能在函数内延迟 import 本模块 —— 这条单向依赖杜绝循环导入）。
* **源片级配置**（``clip_id == "source"``，保留哨兵）：配置读写落在
  ``projects/{project_id}/clips.json`` 顶层 ``source_config``；没有登记 clip 也允许读写。
* **pure config write**：``update_config`` 只改配置 + 版本号 + 台账任务历史，
  **绝不**新建 ``autoedit_*`` 任务、绝不调用自动剪辑/复渲染入口；
  按配置真渲染属 Phase 2（响应里用 ``render_deferred`` / ``render_note`` 如实标注）。
* ``transform`` 的结构校验**委托**既有数学层 :mod:`clip_transform`（``parse_transform``），
  不在这里重写一套（否则预览/导出会与 wire format 漂移）。

错误码（HTTP 层映射 422/404）
-----------------------------
* ``E_CONFIG_INVALID``          —— 值非法（类型/范围/颜色/枚举/transform）
* ``E_CONFIG_UNKNOWN_SECTION``  —— patch 里出现未知的配置段
* ``E_CONFIG_UNKNOWN_CLIP``     —— 项目台账里没有该 clip（404 可映射）
"""

from __future__ import annotations

import copy
import re
import uuid
from typing import Annotated, Any, Iterator

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

# 双模式导入（同 studio_project_store）：包内 / 固化流程执行器按顶层模块导入都要能用。
try:  # 包内（Studio 后端 / 测试）
    from . import clip_transform as transform_layer
    from . import studio_project_store as store
except ImportError:  # pragma: no cover - 执行器以顶层模块方式导入
    import clip_transform as transform_layer  # type: ignore[no-redef]
    import studio_project_store as store  # type: ignore[no-redef]


class ClipConfigError(ValueError):
    """带 E_ 码的显式失败（沿用工作台 ValueError → HTTP 4xx 的既有映射）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_CONFIG_UNKNOWN_SECTION = "E_CONFIG_UNKNOWN_SECTION"
E_CONFIG_UNKNOWN_CLIP = "E_CONFIG_UNKNOWN_CLIP"

#: ``clip_id`` 保留哨兵：绑定当前项目/源片（配置落在 clips.json 顶层 source_config）。
SOURCE_CLIP_ID = "source"

#: 贴纸位置（沿用契约名 SUBTITLE_POSITIONS）。
SUBTITLE_POSITIONS = ("left", "right", "bottom-left", "bottom-right")
FILTER_NAMES = ("none", "warm", "cool", "bw")
#: 成片风格微调 —— 画面调整的命名滤镜（比旧 filter 多一个 vivid）。
PICTURE_FILTER_NAMES = ("none", "warm", "cool", "bw", "vivid")
TRANSITION_TYPES = ("none", "fade")
EXPORT_RESOLUTIONS = ("1080x1920", "1080x1080", "1920x1080")
#: 成片风格微调 —— 画幅比例（当前仅影响预览，UI 与 render-config 都如实标注）。
RATIO_NAMES = ("9:16", "16:9", "1:1")
#: 成片风格微调 —— 四个风格预设（唯一事实源；前端从 render-config 读取同一份）。
EFFECT_PRESET_NAMES = ("medical", "clear", "science", "natural")
EFFECT_PRESETS: dict[str, dict[str, Any]] = {
    "medical": {
        "label": "医疗专业",
        "picture": {"brightness": 0.0, "contrast": 1.08, "saturation": 1.0,
                    "sharpen": 0.25, "temperature": 0.0, "filter": "none"},
        "transition": {"enabled": True, "type": "fade", "duration_s": 0.4},
        "audio": {"ducking": True, "ducking_db": -12.0},
    },
    "clear": {
        "label": "口播清晰",
        "picture": {"brightness": 0.03, "contrast": 1.12, "saturation": 1.06,
                    "sharpen": 0.5, "temperature": 0.03, "filter": "none"},
        "transition": {"enabled": False, "type": "none", "duration_s": 0.4},
        "audio": {"ducking": True, "ducking_db": -12.0},
    },
    "science": {
        "label": "科普重点",
        "picture": {"brightness": 0.02, "contrast": 1.15, "saturation": 1.2,
                    "sharpen": 0.35, "temperature": 0.12, "filter": "none"},
        "transition": {"enabled": True, "type": "fade", "duration_s": 0.5},
        "audio": {"ducking": True, "ducking_db": -10.0},
    },
    "natural": {
        "label": "原始自然",
        "picture": {"brightness": 0.0, "contrast": 1.0, "saturation": 1.0,
                    "sharpen": 0.0, "temperature": 0.0, "filter": "none"},
        "transition": {"enabled": False, "type": "none", "duration_s": 0.5},
        "audio": {"ducking": False, "ducking_db": -12.0},
    },
}
#: 可直接驱动 FFmpeg 导出的 effects 子段。
RENDER_EFFECT_SECTIONS = ("preset", "picture", "ratio", "transition", "audio")
#: 允许出现在部分更新 patch 里的段（顺序即 task 类型/摘要的优先级）。
PATCH_SECTIONS = ("title", "subtitle_config", "subtitle_overrides", "transform",
                  "audio_tracks", "voice_volume", "muted", "effects")
#: 真正改变这些段才需要 bump render_revision（title 不算「画面/字幕/音视频」变更）。
RENDER_BUMP_SECTIONS = ("subtitle_config", "subtitle_overrides", "transform",
                        "audio_tracks", "voice_volume", "muted", "effects")

#: §2 默认字幕样式（1080x1920 坐标系；y_pct=78.125 → MarginV=420，与 video-brain 一致）。
#: 字号/折行/描边与 subtitle_compose 的 DEFAULT_* 保持同步（subtitles@3：44px≈4.1% 画宽、
#: 金黄 #FFD700、14 字/行）。
DEFAULT_SUBTITLE_CONFIG: dict[str, Any] = {
    "font_size_px": 44,
    "font_family": "",
    "text_color": "#FFD700",
    "outline_color": "#101010",
    "outline_width": 3,
    "background": {"enabled": False, "color": "#000000", "opacity": 0.5},
    "position": {"x_pct": 50.0, "y_pct": 78.125},
    "word_highlight": False,
    "max_chars_per_line": 14,
}

DEFAULT_EFFECTS: dict[str, Any] = {
    # ===== 旧契约段（保留：旧 config 仍可读，legacy UI/端点继续可用）=====
    "sticker": {"enabled": False, "position": "left"},
    "end_card": {"enabled": False, "text": "", "duration_s": 3.0},
    "filter": {"name": "none", "strength": 0.0},
    "transition": {"enabled": False, "type": "none", "duration_s": 0.5},
    "export_resolution": "1080x1920",
    # ===== 成片风格微调（新）=====
    # 风格预设：一组 picture/transition/audio 取值（见 EFFECT_PRESETS）。
    "preset": "natural",
    "picture": {
        "brightness": 0.0,   # -1..1（FFmpeg eq brightness；CSS filter 同参数）
        "contrast": 1.0,     # 0..3 （FFmpeg eq contrast；CSS filter 同参数）
        "saturation": 1.0,   # 0..3 （FFmpeg eq saturation；CSS filter 同参数）
        "sharpen": 0.0,      # 0..1 （FFmpeg unsharp；预览不体现）
        "temperature": 0.0,  # -1..1（负=冷 / 正=暖；FFmpeg colortemperature）
        "filter": "none",    # none|warm|cool|bw|vivid
    },
    # 画幅比例：当前仅影响预览（导出画布恒为 9:16 1080x1920），UI 明确标注。
    "ratio": {"name": "9:16", "fit_canvas": True},
    # BGM 自动压低（ducking）：无 BGM 时为无操作。
    "audio": {"ducking": False, "ducking_db": -12.0},
}

DEFAULT_TITLE = ""
DEFAULT_TRANSFORM: dict[str, Any] | None = None
DEFAULT_VOICE_VOLUME = 1.0
DEFAULT_MUTED = False

#: BGM 音轨（与 clip_audio.BGM_KINDS 对齐；这里只做结构与范围校验，不碰文件）。
AUDIO_TRACK_KINDS = ("bgm",)
#: 与 clip_audio.MAX_PEAKS 对齐：波形点数上限。
AUDIO_MAX_PEAKS = 800
#: 音轨淡入/淡出上限（秒）。
AUDIO_MAX_FADE_S = 600.0

#: §3.1 固定文案（HTTP 与 UI 都直接透出，避免各处各写一份）。
RENDER_NOTE = "配置与版本已保存；按配置真渲染属 Phase 2"
EXPORT_NOTE = (
    "画面调整 / 转场 / 音频在「导出成片（按变换重渲）」通道由 FFmpeg 精确生效；"
    "颜色类预览为 CSS filter 近似，几何类预览与导出严格同源；画幅比例当前仅影响预览"
)
#: 预览近似 / 导出精确的说明（render-config 摘要直接透出给 UI 与 tooltip）。
PREVIEW_EXPORT_NOTE = {
    "preview_approximate": "颜色类（亮度/对比度/饱和度/色温/滤镜）预览为 CSS filter 近似；锐化不在预览中体现。",
    "export_exact": "导出以 FFmpeg 参数为准：eq/unsharp/colortemperature/fade/crop/scale/overlay/volume/amix/sidechaincompress/afade。",
    "geometry_note": "裁剪 / 缩放 / 位置 / 适合画布复用既有 transform 语义，预览与导出严格一致。",
    "ratio_note": "画幅比例（9:16 / 16:9 / 1:1）当前仅影响预览；导出画布恒为 9:16（1080×1920）。",
    "audio_note": "原声音量/BGM 音量复用 voice_volume 与 audio_tracks；ducking 用 sidechaincompress。",
    "approximate_keys": [
        "effects.picture.brightness", "effects.picture.contrast",
        "effects.picture.saturation", "effects.picture.temperature",
        "effects.picture.filter", "effects.picture.sharpen",
    ],
    "exact_keys": [
        "effects.picture.brightness", "effects.picture.contrast",
        "effects.picture.saturation", "effects.picture.temperature",
        "effects.picture.filter", "effects.picture.sharpen",
        "effects.transition", "effects.audio", "effects.ratio",
        "transform", "voice_volume", "audio_tracks",
    ],
}
STAGE_SAVED = "配置与版本已保存（按配置真渲染属 Phase 2）"

TASK_TYPE_BY_SECTION: dict[str, str] = {
    "title": "UPDATE_TITLE",
    "subtitle_config": "UPDATE_SUBTITLE",
    "subtitle_overrides": "UPDATE_SUBTITLE",
    "transform": "UPDATE_TRANSFORM",
    "audio_tracks": "UPDATE_AUDIO",
    "voice_volume": "UPDATE_AUDIO",
    "muted": "UPDATE_AUDIO",
    "effects": "UPDATE_EFFECTS",
}
#: 局部任务前缀（与 studio_local_tasks.TASK_PREFIX 的 §4 扩展保持一致）。
TASK_PREFIX_BY_TYPE: dict[str, str] = {
    "UPDATE_SUBTITLE": "msub",
    "UPDATE_TRANSFORM": "xfm",
    "UPDATE_AUDIO": "aud",
    "UPDATE_EFFECTS": "fx",
    "UPDATE_TITLE": "title",
}

_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


# ---------------------------------------------------------------------------
# 严格校验标量（pydantic 注解）
# ---------------------------------------------------------------------------

def _int_like(value: Any) -> Any:
    """整数段容忍「整数值的浮点」（62.0 → 62），但拒绝 bool / 字符串 / 62.5。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _color_like(value: Any) -> Any:
    if not isinstance(value, str) or not _HEX_COLOR.fullmatch(value):
        raise ValueError(f"颜色必须是 #RRGGBB 格式，收到: {value!r}")
    return value.upper()


def _enum_like(label: str, allowed: tuple[str, ...]) -> BeforeValidator:
    def check(value: Any) -> Any:
        if not isinstance(value, str) or value not in allowed:
            raise ValueError(f"{label} 只能是 {'/'.join(allowed)}，收到: {value!r}")
        return value

    return BeforeValidator(check)


IntNumber = Annotated[int, BeforeValidator(_int_like)]
ColorHex = Annotated[str, BeforeValidator(_color_like)]
StickerPosition = Annotated[str, _enum_like("sticker.position", SUBTITLE_POSITIONS)]
FilterName = Annotated[str, _enum_like("filter.name", FILTER_NAMES)]
PictureFilterName = Annotated[str, _enum_like("picture.filter", PICTURE_FILTER_NAMES)]
TransitionType = Annotated[str, _enum_like("transition.type", TRANSITION_TYPES)]
ExportResolution = Annotated[str, _enum_like("export_resolution", EXPORT_RESOLUTIONS)]
RatioName = Annotated[str, _enum_like("ratio.name", RATIO_NAMES)]
PresetName = Annotated[str, _enum_like("preset", EFFECT_PRESET_NAMES)]


class _StrictModel(BaseModel):
    """严格模式（不做数字字符串之类的隐式转换）+ 拒绝未知键。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class _BackgroundModel(_StrictModel):
    enabled: bool = False
    color: ColorHex = "#000000"
    opacity: float = Field(default=0.5, ge=0.0, le=1.0)


class _PositionModel(_StrictModel):
    x_pct: float = Field(default=50.0, ge=0.0, le=100.0)
    y_pct: float = Field(default=78.125, ge=0.0, le=100.0)


class SubtitleConfigModel(_StrictModel):
    """§2 subtitle_config：字段定义顺序即序列化顺序。"""

    font_size_px: IntNumber = Field(default=62, ge=8, le=200)
    font_family: str = ""
    text_color: ColorHex = "#FFFFFF"
    outline_color: ColorHex = "#101010"
    outline_width: IntNumber = Field(default=4, ge=0, le=12)
    background: _BackgroundModel = Field(default_factory=_BackgroundModel)
    position: _PositionModel = Field(default_factory=_PositionModel)
    word_highlight: bool = False
    max_chars_per_line: IntNumber = Field(default=18, ge=4, le=60)


class _StickerModel(_StrictModel):
    enabled: bool = False
    position: StickerPosition = "left"


class _EndCardModel(_StrictModel):
    enabled: bool = False
    text: str = ""
    duration_s: float = Field(default=3.0, ge=0.0, le=30.0)


class _FilterModel(_StrictModel):
    name: FilterName = "none"
    strength: float = Field(default=0.0, ge=0.0, le=1.0)


class _TransitionModel(_StrictModel):
    enabled: bool = False
    type: TransitionType = "none"
    duration_s: float = Field(default=0.5, ge=0.0, le=5.0)


class _PictureModel(_StrictModel):
    """成片风格微调 —— 画面调整（数值与 FFmpeg/CSS 同源）。"""

    brightness: float = Field(default=0.0, ge=-1.0, le=1.0)
    contrast: float = Field(default=1.0, ge=0.0, le=3.0)
    saturation: float = Field(default=1.0, ge=0.0, le=3.0)
    sharpen: float = Field(default=0.0, ge=0.0, le=1.0)
    temperature: float = Field(default=0.0, ge=-1.0, le=1.0)
    filter: PictureFilterName = "none"


class _RatioModel(_StrictModel):
    name: RatioName = "9:16"
    fit_canvas: bool = True


class _EffectsAudioModel(_StrictModel):
    """BGM 自动压低（ducking）；原声/BGM 音量与淡入淡出复用既有字段。"""

    ducking: bool = False
    ducking_db: float = Field(default=-12.0, ge=-30.0, le=0.0)


class EffectsModel(_StrictModel):
    # 旧字段（保留向后兼容）
    sticker: _StickerModel = Field(default_factory=_StickerModel)
    end_card: _EndCardModel = Field(default_factory=_EndCardModel)
    filter: _FilterModel = Field(default_factory=_FilterModel)
    transition: _TransitionModel = Field(default_factory=_TransitionModel)
    export_resolution: ExportResolution = "1080x1920"
    # 成片风格微调
    preset: PresetName = "natural"
    picture: _PictureModel = Field(default_factory=_PictureModel)
    ratio: _RatioModel = Field(default_factory=_RatioModel)
    audio: _EffectsAudioModel = Field(default_factory=_EffectsAudioModel)


# ---------------------------------------------------------------------------
# pydantic 错误 → 可读中文 ClipConfigError
# ---------------------------------------------------------------------------

def _reason(error: dict[str, Any]) -> str:
    kind = str(error.get("type") or "")
    context = error.get("ctx") or {}
    if kind == "extra_forbidden":
        return "未知字段"
    if kind == "missing":
        return "缺少字段"
    if kind in {"int_type", "int_parsing", "int_from_float"}:
        return "必须是整数"
    if kind in {"float_type", "float_parsing", "decimal_type"}:
        return "必须是数字"
    if kind == "bool_type":
        return "必须是布尔值"
    if kind in {"string_type", "string_too_short", "string_too_long"}:
        return "必须是字符串"
    if kind == "greater_than_equal":
        return f"不得小于 {context.get('ge')}"
    if kind == "greater_than":
        return f"必须大于 {context.get('gt')}"
    if kind == "less_than_equal":
        return f"不得大于 {context.get('le')}"
    if kind == "less_than":
        return f"必须小于 {context.get('lt')}"
    if kind in {"model_type", "dict_type"}:
        return f"必须是 JSON 对象，收到: {error.get('input')!r}"
    if kind == "list_type":
        return "必须是数组"
    if kind == "value_error":
        return str(error.get("msg") or "取值非法").removeprefix("Value error, ")
    return str(error.get("msg") or "取值非法")


def _validation_error(exc: ValidationError, prefix: str) -> ClipConfigError:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in (error.get("loc") or ()))
        label = f"{prefix}.{location}" if location else prefix
        parts.append(f"{label}: {_reason(error)}")
    detail = "；".join(parts) or "取值非法"
    return ClipConfigError(E_CONFIG_INVALID, detail)


def _as_mapping(raw: Any, label: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ClipConfigError(E_CONFIG_INVALID, f"{label} 必须是 JSON 对象，收到: {raw!r}")
    return raw


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """深合并（嵌套 dict 逐层合并；数组/标量整体替换）。"""
    result: dict[str, Any] = {key: copy.deepcopy(value) for key, value in base.items()}
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# ---------------------------------------------------------------------------
# 规范化：完整配置（读路径）与 patch（写路径）
# ---------------------------------------------------------------------------

def normalize_subtitle_config(raw: Any) -> dict[str, Any]:
    """合并默认值 + 严格校验，返回完整 subtitle_config。"""
    data = _as_mapping(raw, "subtitle_config")
    try:
        model = SubtitleConfigModel.model_validate(_merge(DEFAULT_SUBTITLE_CONFIG, data))
    except ValidationError as exc:
        raise _validation_error(exc, "subtitle_config") from exc
    return model.model_dump()


def normalize_effects(raw: Any) -> dict[str, Any]:
    """合并默认值 + 严格校验，返回完整 effects。"""
    data = _as_mapping(raw, "effects")
    try:
        model = EffectsModel.model_validate(_merge(DEFAULT_EFFECTS, data))
    except ValidationError as exc:
        raise _validation_error(exc, "effects") from exc
    return model.model_dump()


def _number(value: Any, label: str, *, minimum: float | None = None,
            maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClipConfigError(E_CONFIG_INVALID, f"{label} 必须是数字，收到: {value!r}")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ClipConfigError(E_CONFIG_INVALID, f"{label} 不得小于 {minimum}，收到: {number}")
    if maximum is not None and number > maximum:
        raise ClipConfigError(E_CONFIG_INVALID, f"{label} 不得大于 {maximum}，收到: {number}")
    return number


def normalize_audio_tracks(raw: Any) -> list[dict[str, Any]]:
    """音轨结构与范围校验（**不碰文件**）：volume 0..1、fade 0..600、peaks ≤ 800。

    未知键原样保留：track 的字段由 clip_audio 扩展，这里只保证「进得来、范围对」。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ClipConfigError(E_CONFIG_INVALID, f"audio_tracks 必须是数组，收到: {raw!r}")
    tracks: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        label = f"audio_tracks[{index}]"
        if not isinstance(item, dict):
            raise ClipConfigError(E_CONFIG_INVALID, f"{label} 必须是 JSON 对象，收到: {item!r}")
        track = dict(item)
        if not str(track.get("asset_id") or track.get("id") or "").strip():
            raise ClipConfigError(E_CONFIG_INVALID, f"{label} 缺少 asset_id/id")
        kind = track.get("kind")
        if kind is not None and kind not in AUDIO_TRACK_KINDS:
            raise ClipConfigError(
                E_CONFIG_INVALID,
                f"{label}.kind 只能是 {'/'.join(AUDIO_TRACK_KINDS)}，收到: {kind!r}")
        if "volume" in track and track["volume"] is not None:
            track["volume"] = _number(track["volume"], f"{label}.volume", minimum=0.0, maximum=1.0)
        for key in ("fade_in_s", "fade_out_s"):
            if track.get(key) is not None:
                track[key] = _number(track[key], f"{label}.{key}",
                                     minimum=0.0, maximum=AUDIO_MAX_FADE_S)
        if track.get("duration_s") is not None:
            track["duration_s"] = _number(track["duration_s"], f"{label}.duration_s", minimum=0.0)
        if track.get("bytes") is not None and (
                isinstance(track["bytes"], bool) or not isinstance(track["bytes"], int)):
            raise ClipConfigError(E_CONFIG_INVALID,
                                  f"{label}.bytes 必须是整数，收到: {track['bytes']!r}")
        for key in ("id", "asset_id", "path", "filename", "sha256", "created_at"):
            if track.get(key) is not None and not isinstance(track[key], str):
                raise ClipConfigError(E_CONFIG_INVALID,
                                      f"{label}.{key} 必须是字符串，收到: {track[key]!r}")
        peaks = track.get("peaks")
        if peaks is not None:
            if not isinstance(peaks, list):
                raise ClipConfigError(E_CONFIG_INVALID, f"{label}.peaks 必须是数组")
            if len(peaks) > AUDIO_MAX_PEAKS:
                raise ClipConfigError(
                    E_CONFIG_INVALID,
                    f"{label}.peaks 最多 {AUDIO_MAX_PEAKS} 个点，收到: {len(peaks)}")
            track["peaks"] = [_number(point, f"{label}.peaks[{position}]",
                                      minimum=0.0, maximum=1.0)
                              for position, point in enumerate(peaks)]
        tracks.append(track)
    return tracks


def _normalize_overrides(raw: Any) -> list[dict[str, Any]]:
    """字幕覆盖项：``[{segment_id, start?, end?, text, updated_at}]``（结构校验）。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ClipConfigError(E_CONFIG_INVALID, f"subtitle_overrides 必须是数组，收到: {raw!r}")
    overrides: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        label = f"subtitle_overrides[{index}]"
        if not isinstance(item, dict):
            raise ClipConfigError(E_CONFIG_INVALID, f"{label} 必须是 JSON 对象，收到: {item!r}")
        entry = dict(item)
        segment_id = str(entry.get("segment_id") or "").strip()
        if not segment_id:
            raise ClipConfigError(E_CONFIG_INVALID, f"{label}.segment_id 不能为空")
        entry["segment_id"] = segment_id
        for key in ("start", "end"):
            if entry.get(key) is not None:
                entry[key] = _number(entry[key], f"{label}.{key}", minimum=0.0)
        for key in ("text", "updated_at", "speaker"):
            if entry.get(key) is not None and not isinstance(entry[key], str):
                raise ClipConfigError(E_CONFIG_INVALID,
                                      f"{label}.{key} 必须是字符串，收到: {entry[key]!r}")
        overrides.append(entry)
    return overrides


def _normalize_transform(value: Any) -> dict[str, Any] | None:
    """结构校验**委托** clip_transform（wire format 的唯一事实源）。``None`` = 清除。"""
    if value is None:
        return None
    try:
        return transform_layer.parse_transform(value).to_payload()
    except transform_layer.TransformValidationError as exc:
        raise ClipConfigError(E_CONFIG_INVALID, f"transform 非法: {exc}") from exc


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ClipConfigError(E_CONFIG_INVALID, f"{label} 必须是字符串，收到: {value!r}")
    return value


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ClipConfigError(E_CONFIG_INVALID, f"{label} 必须是布尔值，收到: {value!r}")
    return value


def _partial_section(model: type[BaseModel], raw: Any, label: str) -> dict[str, Any]:
    """部分段校验：只返回**用户显式给过**的键（嵌套同样只保留显式键）。

    这样 patch 里没写的字段仍然继承 source_config / 默认值，不会被打回默认。
    """
    data = _as_mapping(raw, label)
    try:
        model_instance = model.model_validate(data)
    except ValidationError as exc:
        raise _validation_error(exc, label) from exc
    return model_instance.model_dump(exclude_unset=True)


def _normalize_section(section: str, value: Any) -> Any:
    if section == "title":
        return _require_str(value, "title")
    if section == "subtitle_config":
        return normalize_subtitle_config(value)
    if section == "subtitle_overrides":
        return _normalize_overrides(value)
    if section == "transform":
        return _normalize_transform(value)
    if section == "audio_tracks":
        return normalize_audio_tracks(value)
    if section == "voice_volume":
        return _number(value, "voice_volume", minimum=0.0, maximum=1.0)
    if section == "muted":
        return _require_bool(value, "muted")
    if section == "effects":
        return normalize_effects(value)
    raise ClipConfigError(E_CONFIG_UNKNOWN_SECTION,
                          f"未知配置段 {section!r}；可用: {list(PATCH_SECTIONS)}")


def default_config() -> dict[str, Any]:
    """一份完整的默认配置（深拷贝，调用方可随意修改）。"""
    return {
        "title": DEFAULT_TITLE,
        "subtitle_config": copy.deepcopy(DEFAULT_SUBTITLE_CONFIG),
        "subtitle_overrides": [],
        "transform": DEFAULT_TRANSFORM,
        "audio_tracks": [],
        "voice_volume": DEFAULT_VOICE_VOLUME,
        "muted": DEFAULT_MUTED,
        "effects": copy.deepcopy(DEFAULT_EFFECTS),
    }


def normalize_config(raw: Any) -> dict[str, Any]:
    """把（可能只写了几段的）落盘配置补成完整配置；单段损坏时回退该段默认值。

    读路径永不抛错：老 ``clips.json`` / 手改坏的值都只会退化成默认值，UI 不会白屏。
    """
    source = raw if isinstance(raw, dict) else {}
    config = default_config()
    for section in PATCH_SECTIONS:
        if section not in source or source[section] is None:
            if section == "transform":
                config["transform"] = None
            continue
        try:
            config[section] = _normalize_section(section, source[section])
        except ClipConfigError:
            config[section] = default_config()[section]
    return config


def normalize_patch(patch: Any) -> dict[str, Any]:
    """严格校验部分更新 patch；未知段/键、类型错、越界、坏枚举/颜色/transform 都抛错。"""
    if patch is None:
        return {}
    if not isinstance(patch, dict):
        raise ClipConfigError(E_CONFIG_INVALID, f"patch 必须是 JSON 对象，收到: {patch!r}")
    unknown = [key for key in patch if key not in PATCH_SECTIONS]
    if unknown:
        raise ClipConfigError(
            E_CONFIG_UNKNOWN_SECTION,
            f"未知配置段: {', '.join(sorted(unknown))}；可用: {list(PATCH_SECTIONS)}")
    normalized: dict[str, Any] = {}
    for section in PATCH_SECTIONS:
        if section not in patch:
            continue
        value = patch[section]
        if section == "subtitle_config":
            normalized[section] = _partial_section(SubtitleConfigModel, value, "subtitle_config")
        elif section == "effects":
            normalized[section] = _partial_section(EffectsModel, value, "effects")
        else:
            normalized[section] = _normalize_section(section, value)
    return normalized


# ---------------------------------------------------------------------------
# 部分更新（纯函数）与渲染 bump 判定
# ---------------------------------------------------------------------------

def _preset_bundle(preset: str) -> dict[str, Any]:
    """一个预设对应的参数组（picture/transition/audio 的取值组合）。"""

    entry = EFFECT_PRESETS.get(preset) or EFFECT_PRESETS["natural"]
    return {
        "picture": copy.deepcopy(entry["picture"]),
        "transition": copy.deepcopy(entry["transition"]),
        "audio": copy.deepcopy(entry["audio"]),
    }


def expand_effects_patch(value: dict[str, Any], base_effects: dict[str, Any]) -> dict[str, Any]:
    """把「只选了预设」的 effects 部分更新展开成完整参数组。

    规则：patch 里显式给过的子键永远优先（用户手改不被预设覆盖）；预设只补
    它没有显式给出的子段。这样「选中预设 → 再拖滑块」是确定性的。
    """

    merged = _merge(base_effects, value)
    preset = value.get("preset")
    if isinstance(preset, str) and preset in EFFECT_PRESETS:
        bundle = _preset_bundle(preset)
        merged = _merge(merged, {key: item for key, item in bundle.items() if key not in value})
    return merged


def apply_patch(current: dict[str, Any], patch: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """按段深合并 patch；返回（新配置, **真正变化**的段列表）。

    ``changed_sections`` 只包含规范化后取值确实不同的段：重复提交同一个值不会
    被误判成变更（这是「一次编辑只推进一版」的关键）。
    """
    base = normalize_config(current)
    normalized_patch = normalize_patch(patch)
    updated = copy.deepcopy(base)
    changed: list[str] = []
    for section in PATCH_SECTIONS:
        if section not in normalized_patch:
            continue
        value = normalized_patch[section]
        if section == "subtitle_config":
            candidate: Any = normalize_subtitle_config(_merge(base["subtitle_config"], value))
        elif section == "effects":
            candidate = normalize_effects(expand_effects_patch(value, base["effects"]))
        else:
            candidate = value
        if candidate != base[section]:
            changed.append(section)
        updated[section] = candidate
    return updated, changed


def needs_render_bump(changed_sections: list[str]) -> bool:
    """这些段变了才需要 bump render_revision（title 与空集合不 bump）。"""
    return any(section in RENDER_BUMP_SECTIONS for section in (changed_sections or []))


# ---------------------------------------------------------------------------
# 读视图（clip / source 双向绑定）
# ---------------------------------------------------------------------------

def _source_overlay(project_id: str) -> dict[str, Any]:
    manifest = store.load_manifest(project_id)
    raw = manifest.get(store.SOURCE_CONFIG_KEY)
    return raw if isinstance(raw, dict) else {}


def _effective_config(entry: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """clip 记录优先、其次源片级、最后默认值（逐段回退）。

    注意 ``transform``：``None`` 是**有意义的取值**（显式清除），因此只要键存在就用它，
    不会被源片级的变换「继承回来」；其它段的 ``None`` 视为未设置。
    """
    def present(mapping: dict[str, Any], section: str) -> bool:
        return section in mapping and (section == "transform" or mapping[section] is not None)

    config = default_config()
    for section in PATCH_SECTIONS:
        if present(entry, section):
            raw = entry[section]
        elif present(overlay, section):
            raw = overlay[section]
        else:
            continue
        try:
            config[section] = _normalize_section(section, raw)
        except ClipConfigError:
            config[section] = default_config()[section]
    return config


def _section_sources(entry: dict[str, Any], overlay: dict[str, Any], binding: str) -> dict[str, str]:
    """每段生效值的来源：user（本层显式写过）/ project（源片级）/ skill_default。"""
    sources: dict[str, str] = {}
    for section in PATCH_SECTIONS:
        if binding == "source":
            sources[section] = "user" if section in overlay else "skill_default"
        elif section in entry:
            sources[section] = "user"
        elif section in overlay:
            sources[section] = "project"
        else:
            sources[section] = "skill_default"
    return sources


def _binding(project_id: str, clip_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """解析绑定：返回（binding, clip 条目, 源片级 overlay）；未知 clip 显式报 E_ 码。"""
    clip_id = str(clip_id or "").strip()
    overlay = _source_overlay(project_id)
    if clip_id == SOURCE_CLIP_ID:
        return "source", {}, overlay
    manifest = store.load_manifest(project_id)
    entry = manifest["clips"].get(store.validate_clip_id(clip_id))
    if not isinstance(entry, dict):
        raise ClipConfigError(E_CONFIG_UNKNOWN_CLIP,
                              f"项目 {project_id} 里没有 clip_id={clip_id} 的成片")
    return "clip", entry, overlay


def config_view(project_id: str, clip_id: str) -> dict[str, Any]:
    """绑定的完整配置 + 版本号 + 每段来源（``clip_id="source"`` → 源片级）。"""
    binding, entry, overlay = _binding(project_id, clip_id)
    if binding == "source":
        config = normalize_config(overlay)
        revisions = {"transcript_revision": 0, "render_revision": 0}
    else:
        config = _effective_config(entry, overlay)
        revisions = {
            "transcript_revision": int(entry.get("transcript_revision") or 0),
            "render_revision": int(entry.get("render_revision") or 1),
        }
    return {
        "project_id": project_id,
        "clip_id": str(clip_id or "").strip(),
        "binding": binding,
        "config": config,
        "revisions": revisions,
        "sources": _section_sources(entry, overlay, binding),
    }


def _flatten(mapping: dict[str, Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    for key, value in mapping.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            yield from _flatten(value, f"{name}.")
        else:
            yield name, value


def render_config_summary(project_id: str, clip_id: str) -> dict[str, Any]:
    """导出摘要：每项生效值 + 来源 + 预览近似/导出精确说明 + 风格预设表。"""
    view = config_view(project_id, clip_id)
    config = view["config"]
    sources = view["sources"]
    effective: list[dict[str, Any]] = []
    for section in PATCH_SECTIONS:
        value = config.get(section)
        if isinstance(value, dict):
            effective.extend(
                {"key": f"{section}.{key}", "value": leaf,
                 "source": sources[section]}
                for key, leaf in _flatten(value))
        else:
            effective.append({"key": section, "value": value, "source": sources[section]})
    effects = config["effects"]
    return {
        "project_id": project_id,
        "clip_id": view["clip_id"],
        "binding": view["binding"],
        "effective": effective,
        "sources": sources,
        "export": {
            "resolution": effects["export_resolution"],
            "deferred": True,
            "note": EXPORT_NOTE,
        },
        # 成片风格微调的生效值与导出通道（UI 展示 + tooltip）。
        "presets": {
            name: copy.deepcopy(EFFECT_PRESETS[name]) for name in EFFECT_PRESET_NAMES
        },
        "effects_export": {
            "channel": "export-transformed",
            "endpoint": (
                "GET /studio/auto-edit/{task_id}/clips/{clip_id}/export-transformed"
            ),
            "preset": effects["preset"],
            "preset_label": EFFECT_PRESETS.get(effects["preset"], {}).get("label", ""),
            "hand_edited": hand_edited_effects_keys(effects),
            "picture": copy.deepcopy(effects["picture"]),
            "ratio": copy.deepcopy(effects["ratio"]),
            "ratio_preview_only": True,
            "transition": copy.deepcopy(effects["transition"]),
            "audio": copy.deepcopy(effects["audio"]),
            "voice_volume": config["voice_volume"],
            "muted": config["muted"],
            "bgm_track_count": len([t for t in config["audio_tracks"]
                                    if str((t or {}).get("kind") or "bgm") == "bgm"]),
            **copy.deepcopy(PREVIEW_EXPORT_NOTE),
        },
        "config": config,
    }


def hand_edited_effects_keys(effects: dict[str, Any]) -> list[str]:
    """相对当前预设，用户手改过的参数键（预设本身给的组合不算手改）。"""

    preset = str((effects or {}).get("preset") or "natural")
    if preset not in EFFECT_PRESETS:
        return []
    bundle = _preset_bundle(preset)
    edited: list[str] = []
    for section in ("picture", "transition", "audio"):
        current = effects.get(section) or {}
        expected = bundle.get(section) or {}
        for key, value in expected.items():
            if current.get(key) != value:
                edited.append(f"{section}.{key}")
    return edited


def effects_render_settings(config: dict[str, Any]) -> dict[str, Any]:
    """导出渲染需要的 effects/audio 快照（sidecar 镜像 + render-config 共用）。

    ``audio_tracks`` 去掉波形 peaks（渲染不需要，避免 sidecar 过大）。
    """

    effects = config.get("effects") or {}
    tracks: list[dict[str, Any]] = []
    for track in config.get("audio_tracks") or []:
        if not isinstance(track, dict):
            continue
        tracks.append({
            "kind": str(track.get("kind") or "bgm"),
            "asset_id": track.get("asset_id") or track.get("id") or "",
            "path": track.get("path") or "",
            "filename": track.get("filename") or "",
            "volume": track.get("volume", 1.0),
            "fade_in_s": track.get("fade_in_s", 0.0),
            "fade_out_s": track.get("fade_out_s", 0.0),
            "duration_s": track.get("duration_s", 0.0),
        })
    return {
        "effects": {
            "preset": effects.get("preset", "natural"),
            "picture": copy.deepcopy(effects.get("picture") or DEFAULT_EFFECTS["picture"]),
            "ratio": copy.deepcopy(effects.get("ratio") or DEFAULT_EFFECTS["ratio"]),
            "transition": copy.deepcopy(
                effects.get("transition") or DEFAULT_EFFECTS["transition"]),
            "audio": copy.deepcopy(effects.get("audio") or DEFAULT_EFFECTS["audio"]),
            # 旧 filter 作为兜底：新 picture 为中性但旧 filter 非 none 时也生效。
            "filter": copy.deepcopy(effects.get("filter") or DEFAULT_EFFECTS["filter"]),
        },
        "voice_volume": config.get("voice_volume", DEFAULT_VOICE_VOLUME),
        "muted": bool(config.get("muted", DEFAULT_MUTED)),
        "audio_tracks": tracks,
    }


# ---------------------------------------------------------------------------
# 写路径：update_config（纯配置写）/ set_transcript_revision
# ---------------------------------------------------------------------------

def _new_task_id(task_type: str) -> str:
    """与局部任务同构的 id：``<prefix>_<12 hex>``（不依赖 studio_local_tasks 的进度）。"""
    return f"{TASK_PREFIX_BY_TYPE[task_type]}_{uuid.uuid4().hex[:12]}"


def _task_type_for(sections: list[str]) -> str | None:
    """按 PATCH_SECTIONS 顺序取第一个命中段对应的局部任务类型。"""
    for section in PATCH_SECTIONS:
        if section in sections:
            return TASK_TYPE_BY_SECTION[section]
    return None


def _summary_for(task_type: str, config: dict[str, Any], changed: list[str]) -> str:
    if task_type == "UPDATE_TITLE":
        return f"标题已更新为「{config.get('title') or ''}」"
    if task_type == "UPDATE_SUBTITLE":
        return "字幕配置已保存（ASS/SRT 版本由字幕流程生成；烧录导出属 Phase 2）"
    if task_type == "UPDATE_TRANSFORM":
        return "画面变换已保存（已同步既有 sidecar；导出按变换重渲时生效）"
    if task_type == "UPDATE_AUDIO":
        return "音频设置已保存（原声/BGM 音量与 ducking 在导出重渲通道生效）"
    if task_type == "UPDATE_EFFECTS":
        return "效果设置已保存（画面调整/转场/音频在导出重渲通道由 FFmpeg 生效；画幅比例仅影响预览）"
    return f"配置已保存（{', '.join(changed) or '无变化'}）"


def _mirror_transform(entry: dict[str, Any], clip_id: str, transform: Any) -> None:
    """transform 变化时同步既有 sidecar（保持既有语义；``None`` = 清除）。"""
    render_task_id = str(entry.get("render_task_id") or "")
    if not render_task_id:
        return
    try:  # 延迟 import：只有真的改了 transform 才需要加载桥接层
        try:
            from . import studio_auto_edit_bridge as bridge
        except ImportError:  # pragma: no cover - 执行器以顶层模块方式导入
            import studio_auto_edit_bridge as bridge  # type: ignore[no-redef]
        bridge.save_clip_transform(render_task_id, clip_id, transform)
    except (FileNotFoundError, OSError, ValueError, ImportError):
        # 成片产物已被回收 / 任务 id 非法 / 桥接层不可导入（顶层模块模式）：
        # 配置本身仍是事实源，镜像失败不阻断写配置。
        return


def _mirror_render_settings(entry: dict[str, Any], clip_id: str,
                            config: dict[str, Any]) -> None:
    """效果/音频变化时同步渲染 sidecar（导出通道读取；**不触发任何渲染/剪辑**）。

    与 transform sidecar 同构：写失败（产物被回收 / 桥接不可导入）不阻断写配置，
    配置本身永远是事实源。
    """

    render_task_id = str(entry.get("render_task_id") or "")
    if not render_task_id:
        return
    try:
        try:
            from . import studio_auto_edit_bridge as bridge
        except ImportError:  # pragma: no cover - 执行器以顶层模块方式导入
            import studio_auto_edit_bridge as bridge  # type: ignore[no-redef]
        bridge.save_clip_render_settings(
            render_task_id, clip_id, effects_render_settings(config))
    except (FileNotFoundError, OSError, ValueError, ImportError):
        return


def update_config(project_id: str, clip_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """部分更新绑定配置：校验 → 应用 → 按需 bump 版本 → 镜像 → 记任务历史。

    **纯配置写**：绝不创建 ``autoedit_*`` 任务，也绝不调用自动剪辑/复渲染入口。
    """
    normalized_patch = normalize_patch(patch)
    binding, entry, overlay = _binding(project_id, clip_id)
    resolved_clip_id = str(clip_id or "").strip()
    sections = [section for section in PATCH_SECTIONS if section in normalized_patch]

    if binding == "source":
        new_config, changed = apply_patch(normalize_config(overlay), normalized_patch)
        # 落盘只保留显式写过的段（overlay），返回值仍是补齐默认值的完整配置。
        store.update_source_config(project_id, normalized_patch)
        return {
            "project_id": project_id,
            "clip_id": SOURCE_CLIP_ID,
            "binding": "source",
            "changed": changed,
            "config": new_config,
            "revisions": {"transcript_revision": 0, "render_revision": 0},
            "task": None,
            "render_deferred": True,
            "render_note": RENDER_NOTE,
        }

    current = _effective_config(entry, overlay)
    new_config, changed = apply_patch(current, normalized_patch)
    # 任务类型/摘要：优先看真正变化的段，没有变化时按 patch 里出现的段归类（仍如实记录一次写入）。
    task_type = _task_type_for(changed or sections)
    task: dict[str, Any] | None = None
    if task_type is not None:
        task_id = _new_task_id(task_type)
        stamp = store.utc_now()
        # 1) 配置本体（含显式写入的段：key 一旦落盘，render_config_summary 就记为 user）
        manifest = store.update_fields(
            project_id, resolved_clip_id,
            {section: new_config[section] for section in sections})
        # 2) 版本号：只有真正变化且属于「需要重渲」的段才 +1
        if changed and needs_render_bump(changed):
            manifest = store.bump_render_revision(
                project_id, resolved_clip_id, task_id=task_id, kind=task_type)
        # 3) 台账任务历史（按 task_id 去重、保留最近 40 条）
        task = {
            "task_id": task_id,
            "task_type": task_type,
            "status": "completed",
            "stage": STAGE_SAVED,
            "instruction": None,
            "created_at": stamp,
            "updated_at": stamp,
            "summary": _summary_for(task_type, new_config, changed),
            "error": None,
            "changed": list(changed),
        }
        manifest = store.record_task(project_id, resolved_clip_id, task)
        # 4) transform 变化 → 同步既有 sidecar（不生成 ASS/SRT、不触发渲染）
        if "transform" in changed:
            _mirror_transform(entry, resolved_clip_id, new_config["transform"])
        # 5) 效果/音频变化 → 同步渲染 sidecar，导出通道据此精确生效（纯文件镜像，
        #    绝不触发 AUTO_EDIT / 转写 / 自动剪辑）。
        if any(section in changed for section in
               ("effects", "audio_tracks", "voice_volume", "muted")):
            _mirror_render_settings(entry, resolved_clip_id, new_config)
    else:
        manifest = store.load_manifest(project_id)

    entry_after = manifest["clips"].get(resolved_clip_id) or entry
    return {
        "project_id": project_id,
        "clip_id": resolved_clip_id,
        "binding": "clip",
        "changed": changed,
        "config": new_config,
        "revisions": {
            "transcript_revision": int(entry_after.get("transcript_revision") or 0),
            "render_revision": int(entry_after.get("render_revision") or 1),
        },
        "task": task,
        "render_deferred": True,
        "render_note": RENDER_NOTE,
    }


def set_transcript_revision(project_id: str, clip_id: str, revision: Any) -> dict[str, Any]:
    """把有效转写修订号写到 clip 记录（**不** bump render_revision）。"""
    resolved_clip_id = str(clip_id or "").strip()
    if isinstance(revision, bool) or not isinstance(revision, (int, float)) or revision != int(revision):
        raise ClipConfigError(E_CONFIG_INVALID,
                              f"transcript_revision 必须是非负整数，收到: {revision!r}")
    value = int(revision)
    if value < 0:
        raise ClipConfigError(E_CONFIG_INVALID,
                              f"transcript_revision 必须是非负整数，收到: {revision!r}")
    if resolved_clip_id == SOURCE_CLIP_ID:
        raise ClipConfigError(E_CONFIG_UNKNOWN_CLIP,
                              "源片绑定没有转写修订层（transcript 以成片 clip 为单位）")
    manifest = store.load_manifest(project_id)
    if not isinstance(manifest["clips"].get(store.validate_clip_id(resolved_clip_id)), dict):
        raise ClipConfigError(E_CONFIG_UNKNOWN_CLIP,
                              f"项目 {project_id} 里没有 clip_id={resolved_clip_id} 的成片")
    store.update_fields(project_id, resolved_clip_id, {"transcript_revision": value})
    return {"project_id": project_id, "clip_id": resolved_clip_id, "transcript_revision": value}


# ---------------------------------------------------------------------------
# 条目投影（studio_project_store / card_view 用）
# ---------------------------------------------------------------------------

def _safe_revision(value: Any, fallback: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return fallback


def with_clip_defaults(entry: dict[str, Any]) -> dict[str, Any]:
    """返回条目副本，补齐 Phase 1 配置段默认值，并把 ``cover`` 投影成两个只读字段。

    投影字段（``cover_asset_id`` / ``cover_revision``）**不落盘**：它们读自
    ``entry["cover"]``，封面的事实源永远只有 ``cover`` 结构。
    """
    source = entry if isinstance(entry, dict) else {}
    result: dict[str, Any] = dict(source)
    result["title"] = str(source.get("title") or "")
    result["transcript_revision"] = _safe_revision(source.get("transcript_revision"))
    for section in PATCH_SECTIONS:
        if section == "title":
            continue
        if section in source and source[section] is not None:
            try:
                result[section] = _normalize_section(section, source[section])
                continue
            except ClipConfigError:
                pass  # 坏数据 → 退化成默认值（读路径不抛错）
        result[section] = default_config()[section]
    # transform 允许显式 None（= 无变换）
    if "transform" in source and source["transform"] is None:
        result["transform"] = None
    cover = source.get("cover") if isinstance(source.get("cover"), dict) else {}
    result["cover_asset_id"] = cover.get("cover_asset_id")
    result["cover_revision"] = _safe_revision(cover.get("revision"))
    return result
