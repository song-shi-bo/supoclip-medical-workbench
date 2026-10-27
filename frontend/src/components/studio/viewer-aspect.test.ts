// 预览画布比例（viewer-only）纯函数测试：比例换算 + contain 尺寸。
// 这里没有任何导出数学 —— 导出恒为 9:16，预览比例只影响观看形状。

import { describe, expect, it } from "vitest";

import {
  DEFAULT_VIEWER_ASPECT,
  VIEWER_ASPECT_OPTIONS,
  VIEWER_ASPECT_TOOLTIP,
  viewerAspectRatio,
  viewerFrameSize,
} from "./viewer-aspect";

describe("viewer-aspect", () => {
  it("默认是 9:16，且菜单里一定有 9:16 / 1:1 / 16:9 / 原始比例", () => {
    expect(DEFAULT_VIEWER_ASPECT).toBe("9:16");
    expect(VIEWER_ASPECT_OPTIONS.map((option) => option.id)).toEqual(["9:16", "1:1", "16:9", "source"]);
  });

  it("tooltip 原文写明只影响预览、导出仍按流水线 9:16", () => {
    expect(VIEWER_ASPECT_TOOLTIP).toBe("仅影响预览画布；导出仍按流水线 9:16（不改导出参数）。");
  });

  it("比例换算：9:16 / 1:1 / 16:9 / 素材原始比例", () => {
    expect(viewerAspectRatio("9:16", 1920, 1080)).toBeCloseTo(9 / 16, 9);
    expect(viewerAspectRatio("1:1", 1920, 1080)).toBe(1);
    expect(viewerAspectRatio("16:9", 1920, 1080)).toBeCloseTo(16 / 9, 9);
    expect(viewerAspectRatio("source", 1920, 1080)).toBeCloseTo(16 / 9, 9);
    expect(viewerAspectRatio("source", 1080, 1920)).toBeCloseTo(9 / 16, 9);
  });

  it("素材尺寸非法时回退到 9:16，不产生 NaN/0", () => {
    expect(viewerAspectRatio("source", 0, 0)).toBeCloseTo(1080 / 1920, 9);
  });

  it("预览框按 contain 语义取尺寸（宽视口竖屏左右留黑边）", () => {
    expect(viewerFrameSize(800, 400, 9 / 16)).toEqual({ width: 225, height: 400 });
  });

  it("预览框按 contain 语义取尺寸（高视口横屏上下留黑边）", () => {
    expect(viewerFrameSize(400, 800, 16 / 9)).toEqual({ width: 400, height: 225 });
  });

  it("方形预览框在任意视口内都不超出视口", () => {
    const square = viewerFrameSize(433, 300, 1);
    expect(square.width).toBeLessThanOrEqual(433);
    expect(square.height).toBeLessThanOrEqual(300);
    expect(square.width).toBe(square.height);
  });

  it("视口尚未测量（0）时返回 0 尺寸，由调用方回退到 CSS 兜底", () => {
    expect(viewerFrameSize(0, 0, 9 / 16)).toEqual({ width: 0, height: 0 });
  });
});
