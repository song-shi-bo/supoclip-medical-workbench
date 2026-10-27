"""Non-destructive "JianYing-style" frame transform engine for Studio clips.

A timeline clip carries one fixed ``transform`` document (no keyframes in this
version).  The document is pure metadata — the original video file is never
rewritten.  The same document drives two consumers that must agree pixel for
pixel:

* the browser preview (CSS transform, phase B) — via :func:`transform_to_canvas_rect`;
* the FFmpeg export render — via :func:`build_filter_chain`.

Both derive their geometry from :func:`transform_to_canvas_rect`, so the
preview composition and the exported ``output.mp4`` are guaranteed to share
one set of maths.  The wire format (camelCase, stored inside project JSON,
auto-edit task JSON and export requests) is::

    {"scale": 1, "positionX": 0, "positionY": 0,
     "crop": {"left": 0, "top": 0, "right": 1, "bottom": 1},
     "fitMode": "cover", "rotation": 0}

``crop`` is normalised (0..1 of the source frame) so one document survives
resolution changes.  ``rotation`` is reserved and must stay ``0`` in this
version; the field exists so later versions can extend the format without a
schema break.

Rounding contract (mirror this in TypeScript for the preview):
* half-away-from-zero rounding: ``Math.floor(value + 0.5)`` for positive values;
* every pixel *size* is rounded down to an even number (H.264/yuv420p rule,
  same convention as ``video_utils.round_to_even``), minimum 2px;
* offsets are plain integers and may be negative (overlay placement).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = [
    "BACKGROUND_MODES",
    "BLUR_SIGMA",
    "CANVAS_HEIGHT",
    "CANVAS_WIDTH",
    "FIT_MODES",
    "MAX_SCALE",
    "MAX_POSITION_OFFSET",
    "MIN_CROP_FRACTION",
    "MIN_SCALE",
    "PICTURE_FILTERS",
    "CanvasRect",
    "ClipTransform",
    "CropRect",
    "FilterChainPlan",
    "PixelRect",
    "TransformValidationError",
    "build_filter_chain",
    "compute_base_scale",
    "compute_crop_pixels",
    "default_transform",
    "is_default_transform",
    "is_neutral_picture",
    "is_neutral_transition",
    "parse_transform",
    "picture_filter_parts",
    "round_to_even",
    "transform_to_canvas_rect",
    "transition_filter_parts",
]

CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1920
MIN_SCALE = 0.1
MAX_SCALE = 8.0
#: Smallest allowed crop edge as a fraction of the source frame edge.
MIN_CROP_FRACTION = 0.05
#: positionX/Y are clamped to +-2 canvas sides; anything beyond that is noise.
MAX_POSITION_OFFSET = 2.0
FIT_MODES = ("cover", "contain")
BACKGROUND_MODES = ("blur", "color")
BLUR_SIGMA = 20.0

# ---------------------------------------------------------------------------
# 成片风格微调（Inspector「效果」标签）—— 颜色 / 转场
# ---------------------------------------------------------------------------
#: 画面调整里的命名滤镜（与 clip_config.PICTURE_FILTER_NAMES 对齐）。
PICTURE_FILTERS = ("none", "warm", "cool", "bw", "vivid")
#: 中性画面调整（不产生任何滤镜片段）。
NEUTRAL_PICTURE: dict[str, Any] = {
    "brightness": 0.0,
    "contrast": 1.0,
    "saturation": 1.0,
    "sharpen": 0.0,
    "temperature": 0.0,
    "filter": "none",
}
#: colortemperature 的中性白点（Kelvin）；低于它偏暖、高于它偏冷。
NEUTRAL_KELVIN = 6500.0
#: 数值色温（-1..1）映射到 Kelvin 的幅度。
TEMPERATURE_KELVIN_SPAN = 2000.0


def _ff(value: float) -> str:
    """FFmpeg 参数里最短的可读数字（``1`` 而不是 ``1.0``）。"""

    return f"{value:g}"


def _picture_value(picture: dict[str, Any], key: str, default: float) -> float:
    value = picture.get(key)
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    return number if math.isfinite(number) else default


def is_neutral_picture(picture: Any) -> bool:
    """True when a picture document would not change a single pixel."""

    if not isinstance(picture, dict):
        return True
    if str(picture.get("filter") or "none") != "none":
        return False
    return (
        abs(_picture_value(picture, "brightness", 0.0)) < 1e-6
        and abs(_picture_value(picture, "contrast", 1.0) - 1.0) < 1e-6
        and abs(_picture_value(picture, "saturation", 1.0) - 1.0) < 1e-6
        and abs(_picture_value(picture, "sharpen", 0.0)) < 1e-6
        and abs(_picture_value(picture, "temperature", 0.0)) < 1e-6
    )


def picture_filter_parts(picture: Any) -> list[str]:
    """FFmpeg filter fragments realising the picture adjustments (in order).

    * ``eq``        — brightness / contrast / saturation（与前端 CSS filter 同参数）；
    * ``unsharp``   — 锐化（0..1 → luma amount 0..1.5；预览不体现，已在 UI 标注）；
    * ``colortemperature`` — 色温（数值色温与 warm/cool 命名滤镜都映射到 Kelvin）；
    * ``hue=s=0``   — 黑白；``eq`` — vivid。

    Returns an empty list for a neutral document so the untouched chain stays
    byte-for-byte identical (control group).
    """

    if is_neutral_picture(picture):
        return []
    assert isinstance(picture, dict)  # is_neutral_picture guarantees a dict here
    parts: list[str] = []
    brightness = _picture_value(picture, "brightness", 0.0)
    contrast = _picture_value(picture, "contrast", 1.0)
    saturation = _picture_value(picture, "saturation", 1.0)
    if (abs(brightness) > 1e-6 or abs(contrast - 1.0) > 1e-6
            or abs(saturation - 1.0) > 1e-6):
        parts.append(
            f"eq=brightness={_ff(brightness)}:contrast={_ff(contrast)}"
            f":saturation={_ff(saturation)}"
        )
    sharpen = _picture_value(picture, "sharpen", 0.0)
    if sharpen > 1e-6:
        parts.append(f"unsharp=5:5:{_ff(min(1.5, sharpen * 1.5))}")
    temperature = _picture_value(picture, "temperature", 0.0)
    if abs(temperature) > 1e-6:
        kelvin = NEUTRAL_KELVIN - temperature * TEMPERATURE_KELVIN_SPAN
        kelvin = max(2000.0, min(20000.0, kelvin))
        parts.append(f"colortemperature=temperature={_ff(round(kelvin))}")
    name = str(picture.get("filter") or "none")
    if name == "warm":
        parts.append("colortemperature=temperature=5600")
    elif name == "cool":
        parts.append("colortemperature=temperature=7800")
    elif name == "bw":
        parts.append("hue=s=0")
    elif name == "vivid":
        parts.append("eq=saturation=1.35:contrast=1.08")
    return parts


def is_neutral_transition(transition: Any) -> bool:
    if not isinstance(transition, dict):
        return True
    if not transition.get("enabled"):
        return True
    return str(transition.get("type") or "none") != "fade"


def transition_filter_parts(transition: Any, duration_s: float | None) -> list[str]:
    """片段首尾 ``fade``（转场=淡入淡出）。无法定位时长时只做片头淡入。"""

    if is_neutral_transition(transition):
        return []
    assert isinstance(transition, dict)
    raw_duration = transition.get("duration_s")
    length = 0.5
    if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool):
        if math.isfinite(float(raw_duration)) and float(raw_duration) > 0:
            length = float(raw_duration)
    if duration_s is None or not math.isfinite(float(duration_s)) or float(duration_s) <= 0:
        return [f"fade=t=in:st=0:d={_ff(length)}"]
    length = min(length, float(duration_s) / 2)
    if length <= 0:
        return []
    out_start = max(0.0, float(duration_s) - length)
    return [
        f"fade=t=in:st=0:d={_ff(length)}",
        f"fade=t=out:st={_ff(out_start)}:d={_ff(length)}",
    ]


class TransformValidationError(ValueError):
    """Raised when a transform document is structurally invalid.

    Missing fields fall back to defaults and out-of-range numbers are clamped;
    only wrong types (string/bool/NaN/inf), an unknown ``fitMode`` or a
    non-zero ``rotation`` reach this error, so the caller can surface a 4xx.
    """


@dataclass(frozen=True)
class CropRect:
    """Normalised crop window inside the source frame (0..1 per axis)."""

    left: float
    top: float
    right: float
    bottom: float

    def to_payload(self) -> dict[str, float]:
        return {"left": self.left, "top": self.top, "right": self.right, "bottom": self.bottom}

    @property
    def width_fraction(self) -> float:
        return self.right - self.left

    @property
    def height_fraction(self) -> float:
        return self.bottom - self.top


@dataclass(frozen=True)
class ClipTransform:
    """One fixed transform for a timeline clip (no keyframes in this version)."""

    scale: float = 1.0
    position_x: float = 0.0
    position_y: float = 0.0
    crop: CropRect = CropRect(0.0, 0.0, 1.0, 1.0)
    fit_mode: str = "cover"
    rotation: int = 0

    def to_payload(self) -> dict[str, Any]:
        # 6 decimals == sub-micron precision at 4K; keeps the stored JSON clean.
        return {
            "scale": round(self.scale, 6),
            "positionX": round(self.position_x, 6),
            "positionY": round(self.position_y, 6),
            "crop": {
                "left": round(self.crop.left, 6),
                "top": round(self.crop.top, 6),
                "right": round(self.crop.right, 6),
                "bottom": round(self.crop.bottom, 6),
            },
            "fitMode": self.fit_mode,
            "rotation": self.rotation,
        }


@dataclass(frozen=True)
class PixelRect:
    """A rectangle in source pixels."""

    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class CanvasRect:
    """Where the (cropped, scaled) frame sits on the output canvas.

    ``x``/``y`` may be negative or overflow the canvas: that is the pan the
    user drew, and FFmpeg's overlay simply clips whatever falls outside.
    """

    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class FilterChainPlan:
    """Everything a renderer needs to apply one transform via FFmpeg.

    ``vf`` is set when the whole render fits one ``-vf`` chain (no background
    visible).  ``filter_complex`` is set when a background branch is required
    (letterboxing / blurred backdrop); it ends with the ``[v]`` output label.
    Exactly one of the two is non-``None``.
    """

    source_width: int
    source_height: int
    canvas_width: int
    canvas_height: int
    crop: PixelRect
    frame: CanvasRect
    background: str | None
    foreground: tuple[str, ...]
    vf: str | None
    filter_complex: str | None


def default_transform() -> ClipTransform:
    """The reset transform: full crop, 1x scale, centred, cover fit."""

    return ClipTransform()


def round_to_even(value: float, minimum: int = 2) -> int:
    """Round to the nearest even integer (half away from zero), floor-even.

    Mirrors ``video_utils.round_to_even`` (``value - value % 2``) but accepts
    floats and never returns less than ``minimum`` so yuv420p always gets a
    usable plane size.
    """

    rounded = math.floor(value + 0.5)
    even = rounded - (rounded % 2)
    return max(minimum, even)


def _round_half_away(value: float) -> int:
    """Deterministic half-up rounding for offsets (see module docstring)."""

    return math.floor(value + 0.5)


def _number(value: Any, field_name: str) -> float:
    """Coerce one JSON scalar to a finite float; anything else is an error."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TransformValidationError(f"{field_name} 必须是数字，收到: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise TransformValidationError(f"{field_name} 必须是有限数字，收到: {value!r}")
    return number


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _repair_crop(raw: Any) -> CropRect:
    """Normalise one crop document: defaults, clamping, ordering, min area."""

    if raw is None:
        return CropRect(0.0, 0.0, 1.0, 1.0)
    if not isinstance(raw, dict):
        raise TransformValidationError("crop 必须是 JSON 对象")

    left = _clamp(_number(raw.get("left", 0.0), "crop.left"), 0.0, 1.0)
    top = _clamp(_number(raw.get("top", 0.0), "crop.top"), 0.0, 1.0)
    right = _clamp(_number(raw.get("right", 1.0), "crop.right"), 0.0, 1.0)
    bottom = _clamp(_number(raw.get("bottom", 1.0), "crop.bottom"), 0.0, 1.0)

    # Dragged handles can invert; sort so left<=right / top<=bottom always holds.
    left, right = sorted((left, right))
    top, bottom = sorted((top, bottom))

    # Minimum-area guard: widen symmetrically around the centre, then shift
    # back inside the frame if the widened window pokes out either side.
    def widen(low: float, high: float) -> tuple[float, float]:
        if high - low >= MIN_CROP_FRACTION:
            return low, high
        centre = (low + high) / 2
        low, high = centre - MIN_CROP_FRACTION / 2, centre + MIN_CROP_FRACTION / 2
        if low < 0.0:
            low, high = 0.0, MIN_CROP_FRACTION
        elif high > 1.0:
            low, high = 1.0 - MIN_CROP_FRACTION, 1.0
        return low, high

    left, right = widen(left, right)
    top, bottom = widen(top, bottom)
    return CropRect(left=left, top=top, right=right, bottom=bottom)


def parse_transform(raw: Any) -> ClipTransform:
    """Parse and normalise one transform document.

    Missing fields become defaults, numeric out-of-range values are clamped,
    inverted crop edges are sorted and degenerate crops get the minimum-area
    guard.  Structural mistakes raise :class:`TransformValidationError`.
    """

    if raw is None:
        return default_transform()
    if not isinstance(raw, dict):
        raise TransformValidationError("transform 必须是 JSON 对象")

    def scalar(key: str, fallback: float) -> Any:
        # JS clients often send explicit nulls; treat them as missing.
        value = raw.get(key)
        return fallback if value is None else value

    scale = _clamp(_number(scalar("scale", 1.0), "scale"), MIN_SCALE, MAX_SCALE)
    position_x = _clamp(
        _number(scalar("positionX", 0.0), "positionX"),
        -MAX_POSITION_OFFSET * CANVAS_WIDTH,
        MAX_POSITION_OFFSET * CANVAS_WIDTH,
    )
    position_y = _clamp(
        _number(scalar("positionY", 0.0), "positionY"),
        -MAX_POSITION_OFFSET * CANVAS_HEIGHT,
        MAX_POSITION_OFFSET * CANVAS_HEIGHT,
    )
    crop = _repair_crop(raw.get("crop"))

    fit_mode = raw.get("fitMode", "cover")
    if fit_mode is None:
        fit_mode = "cover"
    if not isinstance(fit_mode, str) or fit_mode not in FIT_MODES:
        raise TransformValidationError(f"fitMode 只能是 {'/'.join(FIT_MODES)}，收到: {fit_mode!r}")

    rotation_raw = raw.get("rotation", 0)
    if rotation_raw is None:
        rotation_raw = 0
    rotation = _number(rotation_raw, "rotation")
    if rotation != 0:
        # Reserved for a later version; the field is kept in the schema so a
        # future bump never changes the stored document shape.
        raise TransformValidationError("rotation 本版本恒为 0，暂不支持旋转")

    return ClipTransform(
        scale=scale,
        position_x=position_x,
        position_y=position_y,
        crop=crop,
        fit_mode=fit_mode,
        rotation=int(rotation),
    )


def is_default_transform(transform: ClipTransform) -> bool:
    """True when the transform matches the reset state exactly."""

    return (
        transform.scale == 1.0
        and transform.position_x == 0.0
        and transform.position_y == 0.0
        and transform.crop == CropRect(0.0, 0.0, 1.0, 1.0)
        and transform.fit_mode == "cover"
        and transform.rotation == 0
    )


def compute_crop_pixels(crop: CropRect, source_width: int, source_height: int) -> PixelRect:
    """Convert the normalised crop into an even-sized FFmpeg ``crop`` rect."""

    width = round_to_even(crop.width_fraction * source_width)
    height = round_to_even(crop.height_fraction * source_height)
    width = min(width, round_to_even(float(source_width)))
    height = min(height, round_to_even(float(source_height)))
    x = _clamp(_round_half_away(crop.left * source_width), 0, source_width - width)
    y = _clamp(_round_half_away(crop.top * source_height), 0, source_height - height)
    return PixelRect(x=x, y=y, w=width, h=height)


def compute_base_scale(
    crop_width: int, crop_height: int, canvas_width: int, canvas_height: int, fit_mode: str
) -> float:
    """Base fit of the cropped region inside the canvas, before user scale.

    * ``cover``  — scale up so the crop fully covers the canvas; the dimension
      that would stick out is clipped later.  With scale=1 and a centred frame
      this reproduces the renderer's
      ``scale=...:force_original_aspect_ratio=increase,crop=...`` look.
    * ``contain`` — scale so the whole crop is visible; the leftover canvas
      area shows the background (blur or colour).
    """

    if crop_width <= 0 or crop_height <= 0:
        raise TransformValidationError("裁剪区像素尺寸必须大于 0")
    width_ratio = canvas_width / crop_width
    height_ratio = canvas_height / crop_height
    if fit_mode == "cover":
        return max(width_ratio, height_ratio)
    if fit_mode == "contain":
        return min(width_ratio, height_ratio)
    raise TransformValidationError(f"fitMode 只能是 {'/'.join(FIT_MODES)}，收到: {fit_mode!r}")


def _scaled_frame_size(
    crop: PixelRect, transform: ClipTransform, canvas_width: int, canvas_height: int
) -> tuple[int, int]:
    """Even-aligned on-canvas size of the cropped region after user scale."""

    base = compute_base_scale(crop.w, crop.h, canvas_width, canvas_height, transform.fit_mode)
    width = round_to_even(crop.w * base * transform.scale)
    height = round_to_even(crop.h * base * transform.scale)
    if transform.fit_mode == "cover" and transform.scale >= 1.0:
        # Even-rounding may shave a borderline frame 1-2px below the canvas and
        # expose a hairline background seam; snap those back onto the canvas.
        width = max(width, round_to_even(float(canvas_width)))
        height = max(height, round_to_even(float(canvas_height)))
    return width, height


def transform_to_canvas_rect(
    source_width: int,
    source_height: int,
    transform: ClipTransform | dict[str, Any],
    canvas_width: int = CANVAS_WIDTH,
    canvas_height: int = CANVAS_HEIGHT,
    *,
    pixel_align: bool = True,
) -> CanvasRect:
    """THE preview/export contract: where the frame lands on the canvas.

    The FFmpeg filter chain and the browser preview (phase B) must both derive
    their geometry from this function.  With ``pixel_align=True`` the returned
    integers are exactly the pixels FFmpeg produces (even sizes, half-up
    offsets); the TypeScript preview should call the mirrored maths with the
    same flag so preview and export never drift apart.
    """

    if isinstance(transform, dict):
        transform = parse_transform(transform)
    crop = compute_crop_pixels(transform.crop, source_width, source_height)
    if pixel_align:
        width, height = _scaled_frame_size(crop, transform, canvas_width, canvas_height)
        x = _round_half_away((canvas_width - width) / 2 + transform.position_x)
        y = _round_half_away((canvas_height - height) / 2 + transform.position_y)
        return CanvasRect(x=float(x), y=float(y), w=float(width), h=float(height))
    base = compute_base_scale(crop.w, crop.h, canvas_width, canvas_height, transform.fit_mode)
    width = crop.w * base * transform.scale
    height = crop.h * base * transform.scale
    return CanvasRect(
        x=(canvas_width - width) / 2 + transform.position_x,
        y=(canvas_height - height) / 2 + transform.position_y,
        w=width,
        h=height,
    )


def _covers_canvas(frame: CanvasRect, canvas_width: int, canvas_height: int) -> bool:
    return (
        frame.x <= 0
        and frame.y <= 0
        and frame.x + frame.w >= canvas_width
        and frame.y + frame.h >= canvas_height
    )


def build_filter_chain(
    source_width: int,
    source_height: int,
    transform: ClipTransform | dict[str, Any],
    canvas_width: int = CANVAS_WIDTH,
    canvas_height: int = CANVAS_HEIGHT,
    background_mode: str = "blur",
    *,
    picture: Any = None,
    transition: Any = None,
    duration_s: float | None = None,
) -> FilterChainPlan:
    """Build the FFmpeg filter plan that realises one transform on the canvas.

    Pipeline (all sizes even, all maths from :func:`transform_to_canvas_rect`):

    1. ``crop=w:h:x:y``   — normalised crop converted to source pixels;
    2. ``scale=w:h``      — crop region scaled to its on-canvas size;
    3. ``overlay=x:y``    — the frame placed on a 1080x1920 canvas at the
       contract rectangle (negative offsets pan the frame off-canvas);
    4. background branch  — only when the frame leaves canvas visible:
       ``blur`` re-uses the source as a blurred full-canvas backdrop,
       ``color`` uses a solid ``color=black`` source (``shortest=1`` ends the
       graph with the video input);
    5. when nothing but canvas pixels are visible the chain collapses into a
       single ``-vf`` (``crop`` -> ``scale`` -> centred ``crop`` to canvas),
       and the untouched default transform yields the renderer's minimal
       ``scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920``.

    ``rotation`` must be 0 (reserved).  The caller owns encoding flags such as
    ``-pix_fmt yuv420p``; subtitle burning (if any) appends after this plan.
    """

    if background_mode not in BACKGROUND_MODES:
        raise TransformValidationError(f"background_mode 只能是 {'/'.join(BACKGROUND_MODES)}，收到: {background_mode!r}")
    if source_width < 2 or source_height < 2:
        raise TransformValidationError(f"源分辨率过小: {source_width}x{source_height}")

    if isinstance(transform, dict):
        transform = parse_transform(transform)

    picture_parts = picture_filter_parts(picture)
    fade_parts = transition_filter_parts(transition, duration_s)

    crop = compute_crop_pixels(transform.crop, source_width, source_height)
    frame = transform_to_canvas_rect(
        source_width, source_height, transform, canvas_width, canvas_height, pixel_align=True
    )

    if is_default_transform(transform):
        minimal = (
            f"scale={canvas_width}:{canvas_height}:force_original_aspect_ratio=increase,"
            f"crop={canvas_width}:{canvas_height}"
        )
        # 中性效果不追加任何片段：未改任何项时的滤镜链与旧行为逐字节一致（对照组）。
        chain = ",".join([minimal, *picture_parts, *fade_parts])
        return FilterChainPlan(
            source_width=source_width,
            source_height=source_height,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            crop=crop,
            frame=frame,
            background=None,
            foreground=(minimal,),
            vf=chain,
            filter_complex=None,
        )

    foreground: list[str] = []
    full_frame = crop.w >= source_width and crop.h >= source_height and crop.x == 0 and crop.y == 0
    if not full_frame:
        foreground.append(f"crop={crop.w}:{crop.h}:{crop.x}:{crop.y}")
    if (round(frame.w), round(frame.h)) != (crop.w, crop.h):
        foreground.append(f"scale={round(frame.w)}:{round(frame.h)}")

    if _covers_canvas(frame, canvas_width, canvas_height):
        # The frame overflows the canvas; a centred crop trims the overflow.
        offset_x = -round(frame.x)
        offset_y = -round(frame.y)
        foreground.append(
            f"crop={canvas_width}:{canvas_height}:{offset_x}:{offset_y}"
        )
        chain = ",".join([*foreground, *picture_parts, *fade_parts])
        return FilterChainPlan(
            source_width=source_width,
            source_height=source_height,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            crop=crop,
            frame=frame,
            background=None,
            foreground=tuple(foreground),
            vf=chain,
            filter_complex=None,
        )

    # 前景分支带上画面调整（背景虚化保持原样）；淡入淡出作用在合成后的整幅画面上。
    foreground_chain = ",".join([*foreground, *picture_parts]) if (foreground or picture_parts) else "null"
    overlay = f"overlay=x={round(frame.x)}:y={round(frame.y)}"
    overlay_output = "[vpre]" if fade_parts else "[v]"
    fade_tail = f";[vpre]{','.join(fade_parts)}[v]" if fade_parts else ""
    if background_mode == "blur":
        filter_complex = (
            f"[0:v]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={canvas_width}:{canvas_height}:force_original_aspect_ratio=increase,"
            f"crop={canvas_width}:{canvas_height},gblur=sigma={BLUR_SIGMA:g}[bg];"
            f"[fgsrc]{foreground_chain}[fg];"
            f"[bg][fg]{overlay}{overlay_output}{fade_tail}"
        )
    else:
        filter_complex = (
            f"color=c=black:s={canvas_width}x{canvas_height}[bg];"
            f"[0:v]{foreground_chain}[fg];"
            f"[bg][fg]{overlay}:shortest=1{overlay_output}{fade_tail}"
        )
    return FilterChainPlan(
        source_width=source_width,
        source_height=source_height,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        crop=crop,
        frame=frame,
        background=background_mode,
        foreground=tuple(foreground),
        vf=None,
        filter_complex=filter_complex,
    )
