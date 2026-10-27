"""``timeline_view`` 单元测试：默认值/边界、比例映射、原子读写、缩放锚点不变量。

纯逻辑为主，不依赖真实媒体、不依赖数据库、不依赖 ffmpeg。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import timeline_view as tv


# ---------------------------------------------------------------------------
# 默认值与字段边界
# ---------------------------------------------------------------------------

def test_default_view_values():
    view = tv.TimelineView()
    assert view.zoom_px_per_second == 100.0
    assert view.audio_track_height == 72
    assert view.waveform_ratio == "standard"
    assert view.scroll_left is None
    assert view.to_payload() == {
        "zoom_px_per_second": 100.0,
        "audio_track_height": 72,
        "waveform_ratio": "standard",
        "scroll_left": None,
    }


@pytest.mark.parametrize("zoom", [20, 20.0, 100, 299.9, 300, 300.0])
def test_zoom_within_bounds_is_accepted(zoom):
    assert tv.validate_view({"zoom_px_per_second": zoom}).zoom_px_per_second == float(zoom)


@pytest.mark.parametrize("zoom", [19, 19.999, 0, -5, 301, 1000])
def test_zoom_out_of_bounds_is_rejected(zoom):
    with pytest.raises(tv.TimelineViewError) as excinfo:
        tv.validate_view({"zoom_px_per_second": zoom})
    assert excinfo.value.code == tv.E_TIMELINE_VIEW_INVALID
    assert tv.E_TIMELINE_VIEW_INVALID in str(excinfo.value)


@pytest.mark.parametrize("zoom", ["100", True, None, float("nan"), float("inf")])
def test_zoom_wrong_type_is_rejected(zoom):
    with pytest.raises(tv.TimelineViewError) as excinfo:
        tv.validate_view({"zoom_px_per_second": zoom})
    assert excinfo.value.code == tv.E_TIMELINE_VIEW_INVALID


@pytest.mark.parametrize("height", [44, 72, 180])
def test_track_height_within_bounds_is_accepted(height):
    assert tv.validate_view({"audio_track_height": height}).audio_track_height == height


@pytest.mark.parametrize("height", [43, 181, 0, -1, 1000])
def test_track_height_out_of_bounds_is_rejected(height):
    with pytest.raises(tv.TimelineViewError) as excinfo:
        tv.validate_view({"audio_track_height": height})
    assert excinfo.value.code == tv.E_TIMELINE_VIEW_INVALID


@pytest.mark.parametrize("height", [72.5, "72", True, None])
def test_track_height_non_integer_is_rejected(height):
    with pytest.raises(tv.TimelineViewError):
        tv.validate_view({"audio_track_height": height})


def test_track_height_accepts_integral_float():
    assert tv.validate_view({"audio_track_height": 88.0}).audio_track_height == 88


@pytest.mark.parametrize("ratio", ["compact", "standard", "expanded"])
def test_waveform_ratio_valid_values(ratio):
    assert tv.validate_view({"waveform_ratio": ratio}).waveform_ratio == ratio


@pytest.mark.parametrize("ratio", ["wide", "", "COMPACT", None, 3])
def test_waveform_ratio_invalid_values(ratio):
    with pytest.raises(tv.TimelineViewError) as excinfo:
        tv.validate_view({"waveform_ratio": ratio})
    assert excinfo.value.code == tv.E_TIMELINE_VIEW_INVALID


def test_waveform_ratio_mapping_is_exact():
    assert tv.WAVEFORM_RATIOS == {"compact": 0.35, "standard": 0.60, "expanded": 0.85}
    assert tv.waveform_fraction("compact") == 0.35
    assert tv.waveform_fraction("standard") == 0.60
    assert tv.waveform_fraction("expanded") == 0.85
    assert tv.TimelineView(waveform_ratio="expanded").waveform_fraction == 0.85


def test_waveform_fraction_unknown_code():
    with pytest.raises(tv.TimelineViewError) as excinfo:
        tv.waveform_fraction("huge")
    assert excinfo.value.code == tv.E_TIMELINE_VIEW_INVALID
    assert tv.E_TIMELINE_VIEW_INVALID in str(excinfo.value)


def test_scroll_left_validation():
    assert tv.validate_view({"scroll_left": 0}).scroll_left == 0.0
    assert tv.validate_view({"scroll_left": 12.5}).scroll_left == 12.5
    assert tv.validate_view({}).scroll_left is None
    with pytest.raises(tv.TimelineViewError):
        tv.validate_view({"scroll_left": -1})


def test_validate_view_accepts_model_and_none():
    view = tv.TimelineView(zoom_px_per_second=250)
    assert tv.validate_view(view) is view
    assert tv.validate_view(None).zoom_px_per_second == 100.0


@pytest.mark.parametrize("raw", [[], "view", 3])
def test_validate_view_rejects_non_dict(raw):
    with pytest.raises(tv.TimelineViewError) as excinfo:
        tv.validate_view(raw)
    assert excinfo.value.code == tv.E_TIMELINE_VIEW_INVALID


def test_validate_view_ignores_unknown_keys():
    view = tv.validate_view({"zoom_px_per_second": 120, "future_key": "x"})
    assert view.to_payload() == {
        "zoom_px_per_second": 120.0,
        "audio_track_height": 72,
        "waveform_ratio": "standard",
        "scroll_left": None,
    }


# ---------------------------------------------------------------------------
# 缩放 / 坐标纯函数
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [(5, 20.0), (20, 20.0), (100, 100.0), (300, 300.0), (9999, 300.0)],
)
def test_clamp_zoom(value, expected):
    assert tv.clamp_zoom(value) == expected
    assert tv.pixels_per_second_clamped(value) == expected


@pytest.mark.parametrize("value", ["100", None, True, float("nan")])
def test_clamp_zoom_rejects_garbage(value):
    with pytest.raises(tv.TimelineViewError) as excinfo:
        tv.clamp_zoom(value)
    assert excinfo.value.code == tv.E_TIMELINE_VIEW_INVALID


def test_next_zoom_step_in_and_out():
    assert tv.next_zoom_step(100, zoom_in=True) == 125.0
    assert tv.next_zoom_step(100, zoom_in=False) == 80.0
    # 边界饱和，不会越界。
    assert tv.next_zoom_step(300, zoom_in=True) == 300.0
    assert tv.next_zoom_step(20, zoom_in=False) == 20.0
    assert tv.next_zoom_step(1, zoom_in=False) == 20.0


def test_fit_to_window():
    assert tv.fit_to_window_px_per_second(10, 1000) == 100.0
    assert tv.fit_to_window_px_per_second(600, 1200) == 20.0  # 2px/s → 钳到下限
    assert tv.fit_to_window_px_per_second(1, 1000) == 300.0  # 1000px/s → 钳到上限
    assert tv.fit_to_window_px_per_second(0, 1000) == 100.0
    assert tv.fit_to_window_px_per_second(10, 0) == 100.0


@pytest.mark.parametrize("seconds", [0, 1, 42.5, 3600])
@pytest.mark.parametrize("scroll", [0, 250, 4800])
def test_time_to_px_matches_definition(seconds, scroll):
    assert tv.time_to_px(seconds, 120, scroll) == seconds * 120 - scroll


@pytest.mark.parametrize("px", [0, 12.5, 1000])
@pytest.mark.parametrize("scroll", [0, 250])
def test_px_to_time_is_inverse_of_time_to_px(px, scroll):
    seconds = tv.px_to_time(px, 120, scroll)
    assert tv.time_to_px(seconds, 120, scroll) == pytest.approx(px)


def test_px_to_time_rejects_zero_rate():
    with pytest.raises(tv.TimelineViewError):
        tv.px_to_time(10, 0)


def test_visible_seconds():
    start, end = tv.visible_seconds(1000, 100, 250)
    assert start == pytest.approx(2.5)
    assert end == pytest.approx(12.5)


def test_visible_seconds_clamps_rate():
    start, end = tv.visible_seconds(300, 10, 0)
    assert start == 0.0
    assert end == 15.0  # 10 → 钳到 20px/s


def test_clamp_scroll_left():
    assert tv.clamp_scroll_left(-30) == 0.0
    assert tv.clamp_scroll_left(12) == 12.0
    assert tv.clamp_scroll_left(-30, min_scroll_left=-100) == -30.0


# ---------------------------------------------------------------------------
# 缩放锚点不变量（核心）
# ---------------------------------------------------------------------------

ANCHOR_CASES = [
    # (prev_px_per_second, next_px_per_second, playhead_seconds, scroll_left)
    (100.0, 250.0, 42.0, 0.0),
    (100.0, 250.0, 42.0, 1200.0),
    (250.0, 100.0, 42.0, 4800.0),
    (20.0, 300.0, 0.0, 0.0),
    (300.0, 20.0, 7.25, 999.5),
    (100.0, 100.0, 33.0, 500.0),
    (125.0, 200.0, 180.5, 12345.75),
]


@pytest.mark.parametrize("prev,next_,playhead,scroll", ANCHOR_CASES)
def test_zoom_anchored_keeps_playhead_pixel_position(prev, next_, playhead, scroll):
    """缩放前后 ``time_to_px(playhead)`` 必须逐位相等（含 scroll_left 变化）。"""

    before = tv.time_to_px(playhead, prev, scroll)
    new_scroll = tv.zoom_anchored(prev, next_, playhead, scroll)
    after = tv.time_to_px(playhead, next_, new_scroll)

    assert after == pytest.approx(before, abs=1e-9)
    assert new_scroll != scroll or prev == next_ or playhead == 0.0


def test_zoom_anchored_formula():
    # scroll_next = scroll_prev + t * (next - prev)
    assert tv.zoom_anchored(100.0, 250.0, 42.0, 1200.0) == pytest.approx(1200.0 + 42.0 * 150.0)
    # 缩放比不变（prev == next）时滚动位置不变。
    assert tv.zoom_anchored(150.0, 150.0, 42.0, 999.0) == pytest.approx(999.0)


def test_zoom_anchored_equals_playhead_px_when_scroll_makes_it_zero():
    # 播放头恰好在视口左边缘（px = 0）时，scroll_next = t * next。
    assert tv.zoom_anchored(100.0, 200.0, 10.0, 1000.0) == pytest.approx(2000.0)


def test_zoom_anchored_translation_invariance():
    """scroll_left 平移 k，锚点结果同样平移 k（纯平移不变式）。"""

    base = tv.zoom_anchored(100.0, 200.0, 30.0, 500.0)
    shifted = tv.zoom_anchored(100.0, 200.0, 30.0, 500.0 + 777.0)
    assert shifted - base == pytest.approx(777.0)


def test_zoom_anchored_zoom_in_then_out_returns_original_scroll():
    start_scroll = tv.zoom_anchored(100.0, 250.0, 42.0, 1000.0)
    back = tv.zoom_anchored(250.0, 100.0, 42.0, start_scroll)
    assert back == pytest.approx(1000.0)


def test_zoom_anchored_without_clamp_allows_negative_scroll():
    """播放头靠近原点、缩小视图时锚点解为负 —— 默认不钳制，不变量成立。"""

    new_scroll = tv.zoom_anchored(300.0, 20.0, 1.0, 0.0)
    assert new_scroll == pytest.approx(1.0 * (20.0 - 300.0))
    assert new_scroll < 0
    assert tv.time_to_px(1.0, 20.0, new_scroll) == pytest.approx(tv.time_to_px(1.0, 300.0, 0.0))


def test_zoom_anchored_with_min_scroll_left_clamps():
    """显式要求滚动条语义时才钳制（此时放弃最左端的锚点，换取非负坐标）。"""

    exact = tv.zoom_anchored(300.0, 20.0, 1.0, 0.0)
    clamped = tv.zoom_anchored(300.0, 20.0, 1.0, 0.0, min_scroll_left=0.0)
    assert clamped == 0.0
    assert clamped > exact

    # 视图内（理想解 ≥ 0）时，钳制不生效，不变量原样成立。
    scroll = 5000.0
    anchored = tv.zoom_anchored(100.0, 180.0, 42.0, scroll, min_scroll_left=0.0)
    assert anchored == pytest.approx(tv.zoom_anchored(100.0, 180.0, 42.0, scroll))
    assert tv.time_to_px(42.0, 180.0, anchored) == pytest.approx(
        tv.time_to_px(42.0, 100.0, scroll))


@pytest.mark.parametrize("zoom_in", [True, False])
def test_zoom_anchored_after_button_step(zoom_in):
    prev = 100.0
    nxt = tv.next_zoom_step(prev, zoom_in=zoom_in)
    playhead, scroll = 15.0, 1300.0
    new_scroll = tv.zoom_anchored(prev, nxt, playhead, scroll)
    assert tv.time_to_px(playhead, nxt, new_scroll) == pytest.approx(
        tv.time_to_px(playhead, prev, scroll))


# ---------------------------------------------------------------------------
# 原子写 / 回读 / 刷新后保留
# ---------------------------------------------------------------------------

def test_view_path(tmp_path: Path):
    assert tv.view_path(tmp_path) == tmp_path / "timeline" / "view.json"


def test_load_view_missing_returns_defaults(tmp_path: Path):
    assert tv.load_view(tmp_path).to_payload() == tv.TimelineView().to_payload()
    assert not tv.view_path(tmp_path).exists()  # 读不产生副作用


def test_save_then_load_roundtrip(tmp_path: Path):
    saved = tv.save_view(tmp_path, {
        "zoom_px_per_second": 240.5,
        "audio_track_height": 120,
        "waveform_ratio": "expanded",
        "scroll_left": 1234.5,
    })
    reloaded = tv.load_view(tmp_path)
    assert reloaded == saved
    assert reloaded.zoom_px_per_second == 240.5
    assert reloaded.audio_track_height == 120
    assert reloaded.waveform_ratio == "expanded"
    assert reloaded.scroll_left == 1234.5


def test_saved_file_shape_is_atomic_json(tmp_path: Path):
    tv.save_view(tmp_path, {"zoom_px_per_second": 150})
    path = tv.view_path(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == tv.VIEW_SCHEMA_VERSION
    assert payload["kind"] == "timeline_view"
    assert payload["updated_at"].endswith("Z")
    assert payload["view"]["zoom_px_per_second"] == 150.0
    # 没有残留临时文件。
    assert [item.name for item in path.parent.iterdir() if item.name != "view.json"] == []


def test_save_view_rejects_invalid_without_writing(tmp_path: Path):
    tv.save_view(tmp_path, {"zoom_px_per_second": 100})
    before = tv.view_path(tmp_path).read_text(encoding="utf-8")
    with pytest.raises(tv.TimelineViewError) as excinfo:
        tv.save_view(tmp_path, {"zoom_px_per_second": 301})
    assert excinfo.value.code == tv.E_TIMELINE_VIEW_INVALID
    assert tv.view_path(tmp_path).read_text(encoding="utf-8") == before


def test_save_view_from_model(tmp_path: Path):
    view = tv.TimelineView(zoom_px_per_second=20, audio_track_height=44)
    assert tv.save_view(tmp_path, view) == view
    assert tv.load_view(tmp_path) == view


@pytest.mark.parametrize("ratio", ["compact", "standard", "expanded"])
def test_every_ratio_survives_refresh(tmp_path: Path, ratio):
    tv.save_view(tmp_path, {"waveform_ratio": ratio})
    assert tv.load_view(tmp_path).waveform_ratio == ratio


def test_save_view_patch_merges(tmp_path: Path):
    tv.save_view(tmp_path, {"zoom_px_per_second": 150, "waveform_ratio": "compact"})
    updated = tv.save_view_patch(tmp_path, {"audio_track_height": 180})
    assert updated.zoom_px_per_second == 150.0
    assert updated.waveform_ratio == "compact"
    assert updated.audio_track_height == 180
    assert tv.load_view(tmp_path).audio_track_height == 180


def test_save_view_patch_rejects_invalid_without_clobber(tmp_path: Path):
    tv.save_view(tmp_path, {"zoom_px_per_second": 150})
    with pytest.raises(tv.TimelineViewError):
        tv.save_view_patch(tmp_path, {"waveform_ratio": "bogus"})
    assert tv.load_view(tmp_path).zoom_px_per_second == 150.0


def test_save_view_patch_requires_dict(tmp_path: Path):
    with pytest.raises(tv.TimelineViewError):
        tv.save_view_patch(tmp_path, ["zoom_px_per_second", 100])


def test_load_view_corrupt_json_raises_code(tmp_path: Path):
    path = tv.view_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(tv.TimelineViewError) as excinfo:
        tv.load_view(tmp_path)
    assert excinfo.value.code == tv.E_TIMELINE_VIEW_INVALID


def test_load_view_out_of_range_value_raises_code(tmp_path: Path):
    path = tv.view_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"view": {"zoom_px_per_second": 5000}}), encoding="utf-8")
    with pytest.raises(tv.TimelineViewError) as excinfo:
        tv.load_view(tmp_path)
    assert excinfo.value.code == tv.E_TIMELINE_VIEW_INVALID


def test_load_view_accepts_bare_payload(tmp_path: Path):
    """兼容直接写视图字段（没有 view 信封）的文件。"""

    path = tv.view_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"zoom_px_per_second": 88}), encoding="utf-8")
    assert tv.load_view(tmp_path).zoom_px_per_second == 88.0


def test_view_has_no_render_inputs():
    """护栏：视图设置的字段集合只有显示参数（防止未来被塞进导出参数）。"""

    assert set(tv.TimelineView().to_payload()) == {
        "zoom_px_per_second",
        "audio_track_height",
        "waveform_ratio",
        "scroll_left",
    }
