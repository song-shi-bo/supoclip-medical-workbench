"""clip_transform 纯逻辑测试：transform 归一化、滤镜链生成与预览契约。

不连数据库、不依赖网络；唯一的可选外部依赖是本机 ffmpeg（smoke 测试，
缺失时自动跳过）。
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess

import pytest

from src.clip_transform import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    ClipTransform,
    CropRect,
    TransformValidationError,
    build_filter_chain,
    compute_crop_pixels,
    default_transform,
    is_default_transform,
    parse_transform,
    round_to_even,
    transform_to_canvas_rect,
)

MINIMAL_DEFAULT_CHAIN = (
    "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
)


# ---------------------------------------------------------------------------
# transform 归一化：缺省值 / 越界钳制 / 乱序修复 / 最小面积
# ---------------------------------------------------------------------------

def test_parse_none_and_empty_return_default():
    for raw in (None, {}, {"scale": None, "positionX": None, "crop": None, "fitMode": None, "rotation": None}):
        transform = parse_transform(raw)
        assert transform == default_transform()
        assert transform.to_payload() == {
            "scale": 1.0,
            "positionX": 0.0,
            "positionY": 0.0,
            "crop": {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0},
            "fitMode": "cover",
            "rotation": 0,
        }


def test_parse_clamps_out_of_range_numbers():
    transform = parse_transform({"scale": 100, "positionX": 99999, "positionY": -99999})
    assert transform.scale == 8.0
    assert transform.position_x == 2 * CANVAS_WIDTH
    assert transform.position_y == -2 * CANVAS_HEIGHT

    small = parse_transform({"scale": -5})
    assert small.scale == 0.1

    clamped_crop = parse_transform({"crop": {"left": 1.2, "top": -0.5, "right": 2.0, "bottom": 0.9}})
    # 越界钳到边界后再做最小面积保护：右缘贴边扩到 [0.95, 1.0]。
    assert clamped_crop.crop == CropRect(left=0.95, top=0.0, right=1.0, bottom=0.9)


def test_parse_repairs_inverted_and_degenerate_crop():
    inverted = parse_transform({"crop": {"left": 0.8, "right": 0.2, "top": 0.9, "bottom": 0.1}})
    assert inverted.crop.left == 0.2 and inverted.crop.right == 0.8
    assert inverted.crop.top == 0.1 and inverted.crop.bottom == 0.9

    # 退化成线的裁剪框按中心扩到最小面积，而不是报错。
    degenerate = parse_transform({"crop": {"left": 0.3, "right": 0.3, "top": 0.7, "bottom": 0.7}})
    assert math.isclose(degenerate.crop.right - degenerate.crop.left, 0.05)
    assert math.isclose(degenerate.crop.bottom - degenerate.crop.top, 0.05)
    assert math.isclose((degenerate.crop.left + degenerate.crop.right) / 2, 0.3)
    assert math.isclose((degenerate.crop.top + degenerate.crop.bottom) / 2, 0.7)


def test_parse_rejects_structurally_invalid_input():
    for raw in (
        "scale=2",
        [1, 2, 3],
        {"scale": "2"},
        {"scale": True},
        {"scale": float("nan")},
        {"positionX": float("inf")},
        {"crop": "full"},
        {"crop": {"left": "0.2"}},
        {"fitMode": "fill"},
        {"fitMode": 1},
        {"rotation": 90},
        {"rotation": 0.5},
    ):
        with pytest.raises(TransformValidationError):
            parse_transform(raw)


def test_parse_keeps_non_default_fit_mode():
    assert parse_transform({"fitMode": "contain"}).fit_mode == "contain"


# ---------------------------------------------------------------------------
# 滤镜链生成
# ---------------------------------------------------------------------------

def test_default_transform_yields_minimal_chain():
    for source in ((1920, 1080), (1080, 1920)):
        plan = build_filter_chain(source[0], source[1], default_transform())
        assert plan.vf == MINIMAL_DEFAULT_CHAIN
        assert plan.filter_complex is None
        assert plan.background is None
        # 默认链的 foreground 只有一段（最小链）。
        assert plan.foreground == (MINIMAL_DEFAULT_CHAIN,)


def test_typical_crop_scale_offset_cover_chain():
    transform = parse_transform(
        {
            "scale": 1.2,
            "positionX": 0,
            "positionY": 0,
            "crop": {"left": 0.25, "top": 0, "right": 0.75, "bottom": 1},
            "fitMode": "cover",
        }
    )
    plan = build_filter_chain(1920, 1080, transform)
    # crop: 960x1080 @(480,0)；cover 基准 1920/1080 → scale 1.2 → 2048x2304（偶数）。
    assert (plan.crop.x, plan.crop.y, plan.crop.w, plan.crop.h) == (480, 0, 960, 1080)
    assert plan.foreground == (
        "crop=960:1080:480:0",
        "scale=2048:2304",
        "crop=1080:1920:484:192",
    )
    assert plan.vf == "crop=960:1080:480:0,scale=2048:2304,crop=1080:1920:484:192"
    assert plan.filter_complex is None
    # 契约矩形与滤镜链一致：居中偏移 (-484, -192)。
    assert plan.frame == transform_to_canvas_rect(1920, 1080, transform)


def test_cover_pan_keeps_single_chain_while_canvas_still_covered():
    transform = parse_transform({"scale": 1.2, "positionX": 100, "crop": {"left": 0.25, "right": 0.75}})
    plan = build_filter_chain(1920, 1080, transform)
    assert plan.frame.x == -384.0
    assert plan.foreground[-1] == "crop=1080:1920:384:192"
    assert plan.vf is not None and plan.filter_complex is None


def test_contain_branch_uses_blur_background_complex():
    transform = parse_transform({"fitMode": "contain"})
    plan = build_filter_chain(1920, 1080, transform)
    # contain：1920x1080 → 1080x608 居中，上下露出画布 → 需要 filter_complex。
    assert plan.vf is None
    assert plan.background == "blur"
    assert plan.foreground == ("scale=1080:608",)
    assert plan.filter_complex == (
        "[0:v]split=2[bgsrc][fgsrc];"
        "[bgsrc]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=20[bg];"
        "[fgsrc]scale=1080:608[fg];"
        "[bg][fg]overlay=x=0:y=656[v]"
    )


def test_contain_branch_color_background_uses_shortest():
    transform = parse_transform({"fitMode": "contain"})
    plan = build_filter_chain(1920, 1080, transform, background_mode="color")
    assert plan.background == "color"
    assert plan.filter_complex == (
        "color=c=black:s=1080x1920[bg];"
        "[0:v]scale=1080:608[fg];"
        "[bg][fg]overlay=x=0:y=656:shortest=1[v]"
    )


def test_invalid_background_mode_raises():
    with pytest.raises(TransformValidationError):
        build_filter_chain(1920, 1080, default_transform(), background_mode="green")


def test_odd_source_and_odd_crop_round_to_even_pixels():
    crop_pixels = compute_crop_pixels(parse_transform({}).crop, 1921, 1079)
    assert (crop_pixels.w, crop_pixels.h) == (1920, 1078)

    half = compute_crop_pixels(parse_transform({"crop": {"right": 0.5}}).crop, 1921, 1079)
    assert half.w == 960 and half.h == 1078

    assert round_to_even(607.5) == 608
    assert round_to_even(3) == 2


def test_filter_chain_accepts_raw_dict_transform():
    plan = build_filter_chain(1920, 1080, {"fitMode": "contain"})
    assert plan.foreground == ("scale=1080:608",)


# ---------------------------------------------------------------------------
# 预览/导出契约：cover 与 contain 两分支
# ---------------------------------------------------------------------------

def test_contract_cover_fills_canvas_contain_fits_canvas():
    crop = {"left": 0.25, "right": 0.75}
    # cover：放大后撑满并溢出画布。
    cover = transform_to_canvas_rect(1920, 1080, parse_transform({"crop": crop, "scale": 1.2}))
    assert cover.w >= CANVAS_WIDTH and cover.h >= CANVAS_HEIGHT
    # contain（scale<=1）：完整放入画布，不裁掉内容。
    contain = transform_to_canvas_rect(1920, 1080, parse_transform({"crop": crop, "fitMode": "contain"}))
    assert contain.w <= CANVAS_WIDTH and contain.h <= CANVAS_HEIGHT
    # contain + scale>1 是用户主动放大，允许超出画布，但必须仍小于 cover。
    contain_zoom = transform_to_canvas_rect(1920, 1080, parse_transform({"crop": crop, "scale": 1.2, "fitMode": "contain"}))
    assert contain_zoom.w < cover.w and contain_zoom.h < cover.h
    # 位置偏移 1:1 反映在契约矩形上。
    shifted = transform_to_canvas_rect(
        1920, 1080, parse_transform({"crop": crop, "scale": 1.2, "positionX": 120, "positionY": -60})
    )
    assert shifted.x == cover.x + 120
    assert shifted.y == cover.y - 60


def test_contract_pixel_align_matches_filter_chain_overlay():
    transform = parse_transform({"fitMode": "contain", "positionY": 40})
    frame = transform_to_canvas_rect(1920, 1080, transform)
    plan = build_filter_chain(1920, 1080, transform)
    assert f"overlay=x={round(frame.x)}:y={round(frame.y)}" in plan.filter_complex
    assert f"scale={round(frame.w)}:{round(frame.h)}" in plan.foreground


def test_float_contract_is_the_unaligned_source_of_pixel_contract():
    transform = parse_transform({"fitMode": "contain"})
    floats = transform_to_canvas_rect(1920, 1080, transform, pixel_align=False)
    aligned = transform_to_canvas_rect(1920, 1080, transform)
    assert floats.w == 1080.0
    assert aligned.w == 1080.0
    # 未对齐高度 607.5 → 取偶对齐到 608，居中 y 随之变化。
    assert floats.h == 607.5 and aligned.h == 608.0
    assert aligned.y == round((CANVAS_HEIGHT - aligned.h) / 2)


# ---------------------------------------------------------------------------
# 重置语义与载荷 round-trip
# ---------------------------------------------------------------------------

def test_reset_semantics_and_is_default():
    edited = parse_transform({"scale": 2.5, "positionX": 30, "crop": {"left": 0.1, "right": 0.6}})
    assert not is_default_transform(edited)
    # 载荷 round-trip 保持字段与取值完全一致。
    assert parse_transform(edited.to_payload()) == edited
    # 重置 = 默认变换：scale=1、位置 0、完整 crop、cover、rotation 0。
    reset = default_transform()
    assert is_default_transform(reset)
    assert reset.to_payload()["scale"] == 1
    assert reset.to_payload()["positionX"] == 0 and reset.to_payload()["positionY"] == 0
    assert reset.to_payload()["crop"] == {"left": 0, "top": 0, "right": 1, "bottom": 1}
    assert isinstance(reset, ClipTransform)


def test_payload_is_json_serializable_wire_format():
    payload = parse_transform({"scale": 1.5, "positionX": 12}).to_payload()
    decoded = json.loads(json.dumps(payload))
    assert set(decoded) == {"scale", "positionX", "positionY", "crop", "fitMode", "rotation"}
    assert set(decoded["crop"]) == {"left", "top", "right", "bottom"}


# ---------------------------------------------------------------------------
# 可选：ffmpeg 真渲染 smoke（无 ffmpeg 时跳过）
# ---------------------------------------------------------------------------

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="本机没有 ffmpeg/ffprobe，跳过真渲染 smoke")
def test_ffmpeg_smoke_render_contain_blur_matches_canvas(tmp_path):
    transform = parse_transform({"fitMode": "contain", "positionY": 40})
    plan = build_filter_chain(640, 360, transform, background_mode="blur")
    output = tmp_path / "smoke.mp4"
    subprocess.run(
        [
            FFMPEG, "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15:duration=1",
            "-filter_complex", plan.filter_complex,
            "-map", "[v]", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
            str(output),
        ],
        check=True,
        timeout=120,
    )
    probe = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "stream=width,height", "-of", "json", str(output)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["width"] == CANVAS_WIDTH
    assert stream["height"] == CANVAS_HEIGHT
