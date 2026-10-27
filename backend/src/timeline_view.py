"""时间线「视图设置」的纯展示层模型（底部时间线音轨工具栏 Phase A）。

设计边界（务必遵守）
--------------------
本模块只描述**怎么看时间线**，不描述**时间线里有什么**：

* 属于这里的：``zoom_px_per_second``（时间轴缩放）、``audio_track_height``
  （轨道高度）、``waveform_ratio``（波形占轨道的比例）、``scroll_left``（横向滚动）。
* 不属于这里的：素材、起止、轨道增删、音量、淡入淡出（那些是真实编辑数据，落
  :mod:`src.timeline_history` 的 ``timelineRevision``）。

**本模块的任何输出都不得进入渲染 / 转码 / 导出参数。** 缩放只改刻度与波形密度，
不改画面缩放、音量、时长或导出结果；改变这里的值不得触发转码、重渲染或导出。
Phase B 的路由只能把它当 UI 状态读写，不要把它混进 EDL / 渲染请求里。

落盘
----
``<project_dir>/timeline/view.json``，原子写（临时文件 + ``os.replace``），
刷新页面后缩放 / 音轨高度 / 波形占比仍然保留。

坐标系约定
----------
``scroll_left`` 是**视口左边缘在时间线坐标系里的像素位置**（滚动条语义，非负）。
因此某个时间 ``t`` 的像素位置是 ``t * px_per_second - scroll_left``；反过来
``px_to_time`` 是它的严格逆运算。两个方向都是仿射映射，缩放时以播放头为锚点即可
保持播放头像素位置不变（见 :func:`zoom_anchored`）。
"""

from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

__all__ = [
    "DEFAULT_AUDIO_TRACK_HEIGHT_PX",
    "DEFAULT_SCROLL_LEFT",
    "DEFAULT_WAVEFORM_RATIO",
    "DEFAULT_ZOOM_PX_PER_SECOND",
    "E_TIMELINE_VIEW_INVALID",
    "TIMELINE_DIRNAME",
    "TRACK_HEIGHT_MAX_PX",
    "TRACK_HEIGHT_MIN_PX",
    "VIEW_FILENAME",
    "VIEW_SCHEMA_VERSION",
    "WAVEFORM_RATIOS",
    "ZOOM_MAX_PX_PER_SECOND",
    "ZOOM_MIN_PX_PER_SECOND",
    "ZOOM_STEP_FACTOR",
    "TimelineView",
    "TimelineViewError",
    "clamp_scroll_left",
    "clamp_zoom",
    "fit_to_window_px_per_second",
    "load_view",
    "next_zoom_step",
    "px_to_time",
    "pixels_per_second_clamped",
    "save_view",
    "save_view_patch",
    "time_to_px",
    "validate_view",
    "visible_seconds",
    "view_path",
    "waveform_fraction",
    "zoom_anchored",
]

TIMELINE_DIRNAME = "timeline"
VIEW_FILENAME = "view.json"
VIEW_SCHEMA_VERSION = 1

#: 时间轴缩放的硬边界（px / 秒）：20 最密、300 最疏。
ZOOM_MIN_PX_PER_SECOND = 20.0
ZOOM_MAX_PX_PER_SECOND = 300.0
DEFAULT_ZOOM_PX_PER_SECOND = 100.0
#: 「放大 / 缩小」按钮每档的倍率。
ZOOM_STEP_FACTOR = 1.25

#: 单条音轨高度（px）边界与默认值。
TRACK_HEIGHT_MIN_PX = 44
TRACK_HEIGHT_MAX_PX = 180
DEFAULT_AUDIO_TRACK_HEIGHT_PX = 72

#: 波形占轨道高度的三档比例（只影响显示密度，不影响任何音频参数）。
WAVEFORM_RATIOS: dict[str, float] = {
    "compact": 0.35,
    "standard": 0.60,
    "expanded": 0.85,
}
DEFAULT_WAVEFORM_RATIO = "standard"

#: 滚动条语义下 scroll_left 的最小值（0 = 视口左边缘对齐时间线原点）。
DEFAULT_SCROLL_LEFT = 0.0

E_TIMELINE_VIEW_INVALID = "E_TIMELINE_VIEW_INVALID"

WaveformRatio = Literal["compact", "standard", "expanded"]


class TimelineViewError(ValueError):
    """带机器码的显式失败（沿用工作台 ``ValueError`` → HTTP 4xx 的既有映射）。

    ``message`` 以机器码开头（``E_TIMELINE_VIEW_INVALID: ...``），Phase B 的
    路由可以直接把 ``str(exc)`` 塞进 HTTP 422 的 ``detail``，前端按码分支。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 数字校验助手（拒绝 bool / 字符串 / 非有限值，绝不静默强转）
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_number(value: Any, name: str) -> float:
    if value is None:
        raise ValueError(f"{name} 不能为空")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数字，收到 {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} 必须是有限数字，收到 {value!r}")
    return number


def _require_int_px(value: Any, name: str) -> int:
    if value is None:
        raise ValueError(f"{name} 不能为空")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是整数像素，收到 {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError(f"{name} 必须是整数像素，收到 {value!r}")
    return int(number)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class TimelineView(BaseModel):
    """时间线视图设置（纯 UI 状态，绝不影响渲染/导出）。"""

    model_config = ConfigDict(extra="ignore", frozen=True)

    zoom_px_per_second: float = Field(
        default=DEFAULT_ZOOM_PX_PER_SECOND,
        ge=ZOOM_MIN_PX_PER_SECOND,
        le=ZOOM_MAX_PX_PER_SECOND,
    )
    audio_track_height: int = Field(
        default=DEFAULT_AUDIO_TRACK_HEIGHT_PX,
        ge=TRACK_HEIGHT_MIN_PX,
        le=TRACK_HEIGHT_MAX_PX,
    )
    waveform_ratio: WaveformRatio = DEFAULT_WAVEFORM_RATIO
    #: 横向滚动位置（px）。``None`` 表示前端尚未写入过滚动位置。
    scroll_left: float | None = Field(default=None, ge=DEFAULT_SCROLL_LEFT)

    @field_validator("zoom_px_per_second", mode="before")
    @classmethod
    def _validate_zoom(cls, value: Any) -> float:
        return _require_number(value, "zoom_px_per_second")

    @field_validator("audio_track_height", mode="before")
    @classmethod
    def _validate_track_height(cls, value: Any) -> int:
        return _require_int_px(value, "audio_track_height")

    @field_validator("waveform_ratio", mode="before")
    @classmethod
    def _validate_waveform_ratio(cls, value: Any) -> str:
        if not isinstance(value, str) or value not in WAVEFORM_RATIOS:
            raise ValueError(
                "waveform_ratio 只能是 " + "/".join(WAVEFORM_RATIOS) + f"，收到 {value!r}")
        return value

    @field_validator("scroll_left", mode="before")
    @classmethod
    def _validate_scroll_left(cls, value: Any) -> float | None:
        if value is None:
            return None
        return _require_number(value, "scroll_left")

    def to_payload(self) -> dict[str, Any]:
        """前端消费用的 JSON 形状（snake_case，与既有 audio_tracks 字段风格一致）。"""

        return self.model_dump(mode="json")

    @property
    def waveform_fraction(self) -> float:
        """当前档位对应的波形占比（0.35 / 0.60 / 0.85）。"""

        return WAVEFORM_RATIOS[self.waveform_ratio]


def validate_view(raw: Any) -> TimelineView:
    """严格校验视图设置；任何非法值 → ``TimelineViewError(E_TIMELINE_VIEW_INVALID)``。

    ``TimelineView`` 实例原样通过（不复制、不重校验）；``None`` 取默认值。
    """

    if raw is None:
        return TimelineView()
    if isinstance(raw, TimelineView):
        return raw
    if hasattr(raw, "model_dump") and not isinstance(raw, dict):  # 兼容近似模型
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        raise TimelineViewError(
            E_TIMELINE_VIEW_INVALID,
            f"{E_TIMELINE_VIEW_INVALID}: 视图设置必须是 JSON 对象，收到 {type(raw).__name__}",
        )
    try:
        return TimelineView.model_validate(raw)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['msg']}"
            for item in error.errors()
        )
        raise TimelineViewError(
            E_TIMELINE_VIEW_INVALID,
            f"{E_TIMELINE_VIEW_INVALID}: 非法的时间线视图设置: {details}",
        ) from error


# ---------------------------------------------------------------------------
# 三档比例映射
# ---------------------------------------------------------------------------

def waveform_fraction(ratio: str) -> float:
    """档位名 → 占比；未知档位 → ``E_TIMELINE_VIEW_INVALID``。"""

    if not isinstance(ratio, str) or ratio not in WAVEFORM_RATIOS:
        raise TimelineViewError(
            E_TIMELINE_VIEW_INVALID,
            f"{E_TIMELINE_VIEW_INVALID}: waveform_ratio 只能是 "
            + "/".join(WAVEFORM_RATIOS) + f"，收到 {ratio!r}",
        )
    return WAVEFORM_RATIOS[ratio]


# ---------------------------------------------------------------------------
# 缩放 / 坐标纯函数
# ---------------------------------------------------------------------------

def clamp_zoom(value: float) -> float:
    """把 px/秒 钳到 [20, 300]；非法输入 → ``E_TIMELINE_VIEW_INVALID``。"""

    number = _require_number_raise(value, "px_per_second")
    return max(ZOOM_MIN_PX_PER_SECOND, min(ZOOM_MAX_PX_PER_SECOND, number))


def _require_number_raise(value: Any, name: str) -> float:
    try:
        return _require_number(value, name)
    except ValueError as error:
        raise TimelineViewError(
            E_TIMELINE_VIEW_INVALID, f"{E_TIMELINE_VIEW_INVALID}: {error}") from error


def pixels_per_second_clamped(value: float) -> float:
    """:func:`clamp_zoom` 的语义化别名（给「缩小/滑杆/放大/适合窗口」共用）。"""

    return clamp_zoom(value)


def next_zoom_step(px_per_second: float, *, zoom_in: bool) -> float:
    """「放大 / 缩小」按钮：乘/除 :data:`ZOOM_STEP_FACTOR` 后钳到边界。"""

    current = clamp_zoom(px_per_second)
    step = current * ZOOM_STEP_FACTOR if zoom_in else current / ZOOM_STEP_FACTOR
    return clamp_zoom(step)


def fit_to_window_px_per_second(duration_seconds: float, viewport_width_px: float) -> float:
    """「适合窗口」：整条时间线铺满视口宽度所需的 px/秒，并钳到 [20, 300]。

    只影响刻度密度，不改变导出时长——导出永远是 ``duration_seconds``。
    """

    duration = _require_number_raise(duration_seconds, "duration_seconds")
    width = _require_number_raise(viewport_width_px, "viewport_width_px")
    if duration <= 0 or width <= 0:
        return clamp_zoom(DEFAULT_ZOOM_PX_PER_SECOND)
    return clamp_zoom(width / duration)


def time_to_px(
    seconds: float, px_per_second: float, scroll_left: float = DEFAULT_SCROLL_LEFT
) -> float:
    """时间 → 视口像素：``seconds * px_per_second - scroll_left``。"""

    time_s = _require_number_raise(seconds, "seconds")
    rate = _require_number_raise(px_per_second, "px_per_second")
    scroll = _require_number_raise(scroll_left, "scroll_left")
    return time_s * rate - scroll


def px_to_time(
    px: float, px_per_second: float, scroll_left: float = DEFAULT_SCROLL_LEFT
) -> float:
    """视口像素 → 时间：``(px + scroll_left) / px_per_second``（:func:`time_to_px` 的逆）。"""

    position = _require_number_raise(px, "px")
    rate = _require_number_raise(px_per_second, "px_per_second")
    if rate == 0:
        raise TimelineViewError(
            E_TIMELINE_VIEW_INVALID,
            f"{E_TIMELINE_VIEW_INVALID}: px_per_second 不能为 0",
        )
    scroll = _require_number_raise(scroll_left, "scroll_left")
    return (position + scroll) / rate


def visible_seconds(
    viewport_width_px: float, px_per_second: float, scroll_left: float = DEFAULT_SCROLL_LEFT
) -> tuple[float, float]:
    """视口覆盖的时间区间 ``(start, end)``（给虚拟化/刻度生成用）。"""

    width = _require_number_raise(viewport_width_px, "viewport_width_px")
    start = px_to_time(0.0, px_per_second, scroll_left)
    return start, start + width / clamp_zoom(px_per_second)


def clamp_scroll_left(value: float, *, min_scroll_left: float = DEFAULT_SCROLL_LEFT) -> float:
    """滚动条语义下的钳制（非负）。缩放锚点本身不做钳制，见 :func:`zoom_anchored`。"""

    return max(
        _require_number_raise(min_scroll_left, "min_scroll_left"),
        _require_number_raise(value, "scroll_left"),
    )


def zoom_anchored(
    prev_px_per_second: float,
    next_px_per_second: float,
    playhead_seconds: float,
    scroll_left: float = DEFAULT_SCROLL_LEFT,
    *,
    min_scroll_left: float | None = None,
) -> float:
    """以播放头为锚点缩放，返回缩放后应使用的 ``scroll_left``。

    数学
    ----
    缩放前后播放头像素位置必须相等::

        t * prev - scroll_prev == t * next - scroll_next

    解出::

        scroll_next = scroll_prev + t * (next - prev)

    这是 ``scroll_prev`` 的平移不变式（``+1`` 像素的滚动 → ``+1`` 像素的结果），
    也对 ``t``、``next`` 线性。默认**不做钳制**，因此不变量恒成立；若调用方需要
    滚动条语义（``scroll_left >= 0``），传 ``min_scroll_left=0.0`` —— 此时播放头
    本来已滚出视口左边缘（``time_to_px(t) < 0``）的情况下结果会被抬到 0，
    不变量在该边界情形下会让位于「视口不出现负坐标」。

    该函数是纯函数：只用于显示层，绝不影响渲染/导出参数。
    """

    prev = _require_number_raise(prev_px_per_second, "prev_px_per_second")
    nxt = _require_number_raise(next_px_per_second, "next_px_per_second")
    playhead = _require_number_raise(playhead_seconds, "playhead_seconds")
    scroll = _require_number_raise(scroll_left, "scroll_left")

    anchored = scroll + playhead * (nxt - prev)
    if min_scroll_left is None:
        return anchored
    return clamp_scroll_left(anchored, min_scroll_left=min_scroll_left)


# ---------------------------------------------------------------------------
# 落盘（原子写 / 回读）
# ---------------------------------------------------------------------------

def view_path(project_dir: str | os.PathLike[str]) -> Path:
    """``<project_dir>/timeline/view.json``。"""

    return Path(project_dir) / TIMELINE_DIRNAME / VIEW_FILENAME


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """临时文件 + ``os.replace``（刷新/崩溃后读到的永远是完整 JSON）。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.urandom(6).hex()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def load_view(project_dir: str | os.PathLike[str]) -> TimelineView:
    """读取视图设置；文件缺失 → 默认值；JSON 损坏/字段非法 → ``E_TIMELINE_VIEW_INVALID``。"""

    path = view_path(project_dir)
    if not path.is_file():
        return TimelineView()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TimelineViewError(
            E_TIMELINE_VIEW_INVALID,
            f"{E_TIMELINE_VIEW_INVALID}: 视图设置文件不可读或不是合法 JSON: {error}",
        ) from error
    if isinstance(raw, dict) and isinstance(raw.get("view"), dict):
        raw = raw["view"]
    return validate_view(raw)


def save_view(
    project_dir: str | os.PathLike[str], view: TimelineView | dict[str, Any]
) -> TimelineView:
    """原子写入视图设置并返回校验后的对象；非法值 → ``E_TIMELINE_VIEW_INVALID``（不落盘）。"""

    validated = validate_view(view)
    payload = {
        "version": VIEW_SCHEMA_VERSION,
        "kind": "timeline_view",
        "updated_at": _utc_now(),
        "view": validated.to_payload(),
    }
    _atomic_json(view_path(project_dir), payload)
    return validated


def save_view_patch(
    project_dir: str | os.PathLike[str], patch: dict[str, Any]
) -> TimelineView:
    """局部更新（滑杆拖动等场景）：读 → 合并 → 校验 → 写，非法 patch 不落盘。"""

    if not isinstance(patch, dict):
        raise TimelineViewError(
            E_TIMELINE_VIEW_INVALID,
            f"{E_TIMELINE_VIEW_INVALID}: patch 必须是 JSON 对象，收到 {type(patch).__name__}",
        )
    current = load_view(project_dir).to_payload()
    current.update(patch)
    return save_view(project_dir, current)
