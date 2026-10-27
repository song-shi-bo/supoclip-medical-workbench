/**
 * `lib/timeline-view.ts` 与后端 `backend/src/timeline_view.py` 的**对照测试**。
 *
 * `timeline-view.vectors.json` 是用真实 Python 模块跑出来的（生成脚本见本文件底部注释）。
 * 因此这里断言的不是「实现和自己的期望一致」，而是「JS 与 Python 逐点同值」——
 * 前端算出的缩放/滚动与后端落盘的 `timeline/view.json` 必须能被同一套公式解释。
 */

import { describe, expect, it } from "vitest";

import vectors from "./timeline-view.vectors.json";
import {
  DEFAULT_AUDIO_TRACK_HEIGHT_PX,
  DEFAULT_TIMELINE_VIEW,
  DEFAULT_WAVEFORM_RATIO,
  DEFAULT_ZOOM_PX_PER_SECOND,
  TRACK_HEIGHT_MAX_PX,
  TRACK_HEIGHT_MIN_PX,
  WAVEFORM_RATIOS,
  ZOOM_MAX_PX_PER_SECOND,
  ZOOM_MIN_PX_PER_SECOND,
  ZOOM_STEP_FACTOR,
  applyZoomAnchored,
  clampTrackHeight,
  clampZoom,
  contentWidthPx,
  fitToWindowPxPerSecond,
  nextZoomStep,
  normaliseTimelineView,
  peakIndexAtAssetTime,
  playheadViewportPx,
  pxToTime,
  timeToPx,
  trackContentPx,
  trackRecordRange,
  trackSpanSeconds,
  visibleSeconds,
  waveformColumns,
  waveformFraction,
  waveformHeightPx,
  waveformVisualHeightPx,
  zoomAnchored,
  type AudioTrackLike,
} from "./timeline-view";

const approx = (value: number, expected: number, digits = 6) =>
  expect(value).toBeCloseTo(expected, digits);

describe("常量与后端 timeline_view.py 一致", () => {
  it("缩放边界 / 默认值 / 步进倍率", () => {
    expect(ZOOM_MIN_PX_PER_SECOND).toBe(vectors.constants.ZOOM_MIN_PX_PER_SECOND);
    expect(ZOOM_MAX_PX_PER_SECOND).toBe(vectors.constants.ZOOM_MAX_PX_PER_SECOND);
    expect(DEFAULT_ZOOM_PX_PER_SECOND).toBe(vectors.constants.DEFAULT_ZOOM_PX_PER_SECOND);
    expect(ZOOM_STEP_FACTOR).toBe(vectors.constants.ZOOM_STEP_FACTOR);
  });

  it("轨高边界 / 默认值 / 波形占比三档", () => {
    expect(TRACK_HEIGHT_MIN_PX).toBe(vectors.constants.TRACK_HEIGHT_MIN_PX);
    expect(TRACK_HEIGHT_MAX_PX).toBe(vectors.constants.TRACK_HEIGHT_MAX_PX);
    expect(DEFAULT_AUDIO_TRACK_HEIGHT_PX).toBe(vectors.constants.DEFAULT_AUDIO_TRACK_HEIGHT_PX);
    expect(WAVEFORM_RATIOS).toEqual(vectors.constants.WAVEFORM_RATIOS);
    expect(DEFAULT_WAVEFORM_RATIO).toBe(vectors.constants.DEFAULT_WAVEFORM_RATIO);
    expect(DEFAULT_TIMELINE_VIEW).toEqual({
      zoom_px_per_second: 100, audio_track_height: 72, waveform_ratio: "standard",
      scroll_left: null,
    });
  });

  it("waveform_fraction 逐档同值", () => {
    for (const [key, value] of Object.entries(vectors.waveform_fraction)) {
      expect(waveformFraction(key as keyof typeof WAVEFORM_RATIOS)).toBe(value);
    }
  });
});

describe("clamp_zoom / next_zoom_step / fit_to_window 与 Python 同值", () => {
  it.each(vectors.clamp_zoom)("clamp_zoom($in) === $out", ({ in: input, out }) => {
    expect(clampZoom(input)).toBe(out);
  });

  it.each(vectors.next_zoom_step)(
    "next_zoom_step($px_per_second, zoom_in=$zoom_in) === $out",
    ({ px_per_second, zoom_in, out }) => {
      expect(nextZoomStep(px_per_second, { zoomIn: zoom_in })).toBe(out);
    },
  );

  it.each(vectors.fit_to_window_px_per_second)(
    "fit_to_window(duration=$duration_s, width=$width_px) === $out",
    ({ duration_s, width_px, out }) => {
      expect(fitToWindowPxPerSecond(duration_s, width_px)).toBe(out);
    },
  );

  it("适合窗口把整条时间线铺满视口（未触边界时）", () => {
    const rate = fitToWindowPxPerSecond(45, 900);
    expect(rate).toBe(20); // 900/45 = 20，正好落在下边界
    expect(contentWidthPx(45, rate)).toBeCloseTo(900, 6);
  });
});

describe("time_to_px / px_to_time / visible_seconds 与 Python 同值", () => {
  it.each(vectors.time_to_px)(
    "time_to_px($seconds, $px_per_second, $scroll_left) === $out",
    ({ seconds, px_per_second, scroll_left, out }) => {
      approx(timeToPx(seconds, px_per_second, scroll_left), out, 6);
    },
  );

  it.each(vectors.px_to_time)(
    "px_to_time($px, $px_per_second, $scroll_left) === $out",
    ({ px, px_per_second, scroll_left, out }) => {
      approx(pxToTime(px, px_per_second, scroll_left), out, 6);
    },
  );

  it.each(vectors.visible_seconds)(
    "visible_seconds($width_px, $px_per_second, $scroll_left)",
    ({ width_px, px_per_second, scroll_left, out }) => {
      const [start, end] = visibleSeconds(width_px, px_per_second, scroll_left);
      approx(start, out[0], 6);
      approx(end, out[1], 6);
    },
  );

  it("两个方向互为逆运算", () => {
    for (const seconds of [0, 0.5, 12.5, 438.17]) {
      for (const rate of [20, 100, 300]) {
        for (const scroll of [0, 250.75, 5000]) {
          approx(pxToTime(timeToPx(seconds, rate, scroll), rate, scroll), seconds, 9);
        }
      }
    }
  });
});

describe("zoom_anchored：播放头像素不变（验收 4 的数学根据）", () => {
  it.each(vectors.zoom_anchored)(
    "zoom_anchored($prev, $next, t=$playhead_s, scroll=$scroll_left, min=$min_scroll_left) === $out",
    ({ prev, next, playhead_s, scroll_left, min_scroll_left, out }) => {
      approx(
        zoomAnchored(prev, next, playhead_s, scroll_left,
          min_scroll_left === null ? undefined : { minScrollLeft: min_scroll_left }),
        out,
        6,
      );
    },
  );

  it("缩放前后 time_to_px(playhead) 严格相等（未钳制时无条件成立）", () => {
    const cases = [
      { prev: 100, next: 125, t: 12.5, scroll: 0 },
      { prev: 100, next: 80, t: 12.5, scroll: 0 },
      { prev: 100, next: 125, t: 12.5, scroll: 250 },
      { prev: 100, next: 300, t: 400, scroll: 5000 },
      { prev: 300, next: 20, t: 20, scroll: 100 },
      { prev: 20, next: 300, t: 0, scroll: 0 },
      { prev: 100, next: 100, t: 7.25, scroll: 33.5 },
    ];
    for (const { prev, next, t, scroll } of cases) {
      const before = timeToPx(t, prev, scroll);
      const after = timeToPx(t, next, zoomAnchored(prev, next, t, scroll));
      approx(after, before, 6);
    }
  });

  it("applyZoomAnchored 保持播放头视口像素不变，且 scrollLeft 恒非负", () => {
    const view = { ...DEFAULT_TIMELINE_VIEW, scroll_left: 250 };
    const zoomed = applyZoomAnchored(view, 200, 12.5);
    expect(zoomed.zoom_px_per_second).toBe(200);
    expect(zoomed.scroll_left).toBeGreaterThanOrEqual(0);
    approx(playheadViewportPx(12.5, zoomed), playheadViewportPx(12.5, view), 6);
  });

  it("滚动条语义下的边界：播放头已在视口左边缘之外时钳到 0", () => {
    const anchored = applyZoomAnchored(
      { ...DEFAULT_TIMELINE_VIEW, zoom_px_per_second: 100, scroll_left: 100 }, 20, 20);
    expect(anchored.scroll_left).toBe(0);
    // 锚点不变量在这个边界情形下让位于「视口不出现负坐标」。
    expect(timeToPx(20, 20, anchored.scroll_left ?? 0)).toBeGreaterThanOrEqual(0);
  });
});

describe("clampTrackHeight / contentWidthPx", () => {
  it("轨高钳到 44–180 的整数", () => {
    expect(clampTrackHeight(20)).toBe(44);
    expect(clampTrackHeight(44)).toBe(44);
    expect(clampTrackHeight(72)).toBe(72);
    expect(clampTrackHeight(180)).toBe(180);
    expect(clampTrackHeight(400)).toBe(180);
    expect(clampTrackHeight(72.6)).toBe(73);
    expect(clampTrackHeight(Number.NaN)).toBe(72);
  });

  it("内容宽度随缩放线性变化，导出时长不受影响", () => {
    expect(contentWidthPx(45, 100)).toBeCloseTo(4500, 6);
    expect(contentWidthPx(45, 20)).toBeCloseTo(900, 6);
    expect(contentWidthPx(45, 300)).toBeCloseTo(13500, 6);
    expect(contentWidthPx(0, 300)).toBe(0);
  });
});

describe("normaliseTimelineView 防御式归一化", () => {
  it("空值 / 垃圾值退回默认", () => {
    expect(normaliseTimelineView(null)).toEqual(DEFAULT_TIMELINE_VIEW);
    expect(normaliseTimelineView("nope")).toEqual(DEFAULT_TIMELINE_VIEW);
    expect(normaliseTimelineView({})).toEqual(DEFAULT_TIMELINE_VIEW);
    expect(normaliseTimelineView({ zoom_px_per_second: "fast" })).toEqual(DEFAULT_TIMELINE_VIEW);
  });

  it("越界值被钳制，字符串数字被接受", () => {
    const view = normaliseTimelineView({
      zoom_px_per_second: 9999, audio_track_height: 5,
      waveform_ratio: "expanded", scroll_left: -40,
    });
    expect(view).toEqual({
      zoom_px_per_second: 300, audio_track_height: 44,
      waveform_ratio: "expanded", scroll_left: 0,
    });
    expect(normaliseTimelineView({ zoom_px_per_second: "140" }).zoom_px_per_second).toBe(140);
  });

  it("后端返回的默认 view.json 原样通过（刷新后恢复路径）", () => {
    expect(normaliseTimelineView({
      zoom_px_per_second: 100, audio_track_height: 72,
      waveform_ratio: "standard", scroll_left: null,
    })).toEqual(DEFAULT_TIMELINE_VIEW);
  });
});

// ---------------------------------------------------------------------------
// 波形对齐几何
// ---------------------------------------------------------------------------

const sourceTrack: AudioTrackLike = {
  track_id: "voice_source_1", kind: "voice", path: "raw/source.mp4",
  source_start_s: 0, source_end_s: 45, record_start_s: 0, origin: "source",
};

const bgmTrack: AudioTrackLike = {
  track_id: "bgm_1", kind: "bgm", path: "timeline/audio/bgm-x.wav",
  source_start_s: 5, source_end_s: 25, record_start_s: 12, origin: "user",
};

describe("轨道区间：track_span / record_range / 内容像素落点", () => {
  it("span = 出点 − 入点", () => {
    expect(trackSpanSeconds(sourceTrack)).toBe(45);
    expect(trackSpanSeconds(bgmTrack)).toBe(20);
    expect(trackSpanSeconds({ ...bgmTrack, source_start_s: 30, source_end_s: 30 })).toBe(0);
  });

  it("record 区间 = 落点 + span", () => {
    expect(trackRecordRange(sourceTrack)).toEqual([0, 45]);
    expect(trackRecordRange(bgmTrack)).toEqual([12, 32]);
  });

  it("内容像素：left/width 完全由 record_start_s 与缩放决定（刻度/块/波形同源）", () => {
    expect(trackContentPx(sourceTrack, 100)).toEqual({ left: 0, width: 4500 });
    expect(trackContentPx(bgmTrack, 100)).toEqual({ left: 1200, width: 2000 });
    expect(trackContentPx(bgmTrack, 20)).toEqual({ left: 240, width: 400 });
    // 时间线坐标与「刻度位置」用同一函数：视频块、字幕块、波形、播放头都不可能错位。
    for (const rate of [20, 100, 300]) {
      expect(trackContentPx(bgmTrack, rate).left).toBeCloseTo(timeToPx(12, rate, 0), 6);
      expect(trackContentPx(bgmTrack, rate).left + trackContentPx(bgmTrack, rate).width)
        .toBeCloseTo(timeToPx(32, rate, 0), 6);
    }
  });
});

describe("peakIndexAtAssetTime：peaks 下标与素材时刻对齐", () => {
  it("把素材时长等分成 peakCount 段", () => {
    expect(peakIndexAtAssetTime(0, 45, 800)).toBe(0);
    expect(peakIndexAtAssetTime(44.9, 45, 800)).toBe(798);
    expect(peakIndexAtAssetTime(44.999, 45, 800)).toBe(799);
    expect(peakIndexAtAssetTime(45, 45, 800)).toBe(799);
    expect(peakIndexAtAssetTime(-0.1, 45, 800)).toBe(-1);
    expect(peakIndexAtAssetTime(22.5, 45, 2)).toBe(1);
  });

  it("非法输入不产生越界下标", () => {
    expect(peakIndexAtAssetTime(1, 0, 800)).toBe(-1);
    expect(peakIndexAtAssetTime(1, 45, 0)).toBe(-1);
    expect(peakIndexAtAssetTime(Number.NaN, 45, 800)).toBe(0);
  });
});

describe("waveformColumns：逐像素对齐（验收 1/6 的数学证据）", () => {
  const peaks = Array.from({ length: 45 }, (_, index) => (index + 1) / 45);

  it("视口每一列都落在正确的 peaks 下标上", () => {
    const columns = waveformColumns(sourceTrack, peaks, {
      pxPerSecond: 20, scrollLeft: 0, viewportWidthPx: 90, assetDurationSeconds: 45,
    });
    expect(columns).toHaveLength(90);
    // 源声从 0 开始、覆盖整条轨道：每一列都有波形。
    expect(columns.every((column) => column !== null)).toBe(true);
    // x=0 → t=0 → asset 0s → 第 0 桶；x=89 → t=4.45s → 第 4 桶（45 桶覆盖 45s）。
    expect(columns[0]?.index).toBe(0);
    expect(columns[89]?.index).toBe(4);
    expect(columns[0]?.amplitude).toBeCloseTo(1 / 45, 6);
    expect(columns[89]?.amplitude).toBeCloseTo(5 / 45, 6);
  });

  it("轨道之外的列不画（record 区间边界精确到像素）", () => {
    const columns = waveformColumns(bgmTrack, peaks, {
      pxPerSecond: 20, scrollLeft: 0, viewportWidthPx: 800, assetDurationSeconds: 45,
    });
    // record 12s → 32s，20px/s → [240, 640) px。
    expect(columns[239]).toBeNull();
    expect(columns[240]).not.toBeNull();
    expect(columns[639]).not.toBeNull();
    expect(columns[640]).toBeNull();
  });

  it("滚动后同一时间点仍映射到同一 peaks 下标（横向滚动严格同步）", () => {
    const base = { pxPerSecond: 20, viewportWidthPx: 200, assetDurationSeconds: 45 };
    const withoutScroll = waveformColumns(sourceTrack, peaks, { ...base, scrollLeft: 0 });
    const scrolled = waveformColumns(sourceTrack, peaks, { ...base, scrollLeft: 100 });
    // 视口内 x 在 scrollLeft=100 时对应内容坐标 x+100，因此下标序列是「右移 100 列」。
    for (let x = 0; x < 100; x += 1) {
      expect(scrolled[x]?.index).toBe(withoutScroll[x + 100]?.index);
    }
  });

  it("缩放只改密度，不改对齐：同一列的时间由 pxToTime 唯一决定", () => {
    for (const rate of [20, 100, 300]) {
      const columns = waveformColumns(sourceTrack, peaks, {
        pxPerSecond: rate, scrollLeft: 0, viewportWidthPx: 64, assetDurationSeconds: 45,
      });
      for (const x of [0, 1, 17, 63]) {
        const expectedTime = timeToPx(pxToTime(x, rate, 0), rate, 0);
        expect(expectedTime).toBeCloseTo(x, 9);
        const index = columns[x]?.index ?? -1;
        expect(index).toBe(peakIndexAtAssetTime(pxToTime(x, rate, 0), 45, peaks.length));
      }
    }
  });

  it("没有 peaks 时不画任何列（不伪造波形）", () => {
    const empty = waveformColumns(sourceTrack, [], {
      pxPerSecond: 100, scrollLeft: 0, viewportWidthPx: 40, assetDurationSeconds: 45,
    });
    expect(empty.every((column) => column === null)).toBe(true);
    const undefined_ = waveformColumns(sourceTrack, undefined, {
      pxPerSecond: 100, scrollLeft: 0, viewportWidthPx: 40, assetDurationSeconds: 45,
    });
    expect(undefined_.every((column) => column === null)).toBe(true);
  });
});

describe("波形视觉高度：轨高 × 占比（验收 5/6 的像素证据）", () => {
  it("轨高 44/72/180 与三档占比的组合严格单调", () => {
    expect(waveformHeightPx(72, "compact")).toBeCloseTo(25.2, 6);
    expect(waveformHeightPx(72, "standard")).toBeCloseTo(43.2, 6);
    expect(waveformHeightPx(72, "expanded")).toBeCloseTo(61.2, 6);
    expect(waveformHeightPx(180, "expanded")).toBeCloseTo(153, 6);
    expect(waveformHeightPx(44, "compact")).toBeCloseTo(15.4, 6);
    expect(waveformHeightPx(20, "standard")).toBeCloseTo(44 * 0.6, 6); // 钳到 44
  });

  it("峰到峰视觉高度 = 振幅 × 波形高度，且恒 ≥ 1px", () => {
    expect(waveformVisualHeightPx(1, 180, "expanded")).toBeCloseTo(153, 6);
    expect(waveformVisualHeightPx(0.5, 72, "standard")).toBeCloseTo(21.6, 6);
    expect(waveformVisualHeightPx(0, 72, "standard")).toBe(1);
    expect(waveformVisualHeightPx(2, 72, "standard")).toBeCloseTo(43.2, 6); // 振幅钳到 1
  });

  it("占比只影响显示高度，不进入任何导出参数（只用轨高与档位两个显示输入）", () => {
    // 签名即契约：没有 duration / volume / export 参数可传。
    expect(waveformHeightPx.length).toBe(2);
    expect(waveformVisualHeightPx.length).toBe(3);
  });
});

/*
 * 重新生成对照向量（后端改动常量或公式时执行）：
 *
 *   cd backend && .venv/bin/python - <<'PY'
 *   import json, sys; from pathlib import Path
 *   sys.path.insert(0, "."); from src import timeline_view as tv
 *   ...见本任务报告中的生成命令...
 *   PY
 *
 * 生成结果写入 `frontend/src/lib/timeline-view.vectors.json`；本测试随即失败，
 * 从而强制前端与后端同一次改动里保持一致。
 */
