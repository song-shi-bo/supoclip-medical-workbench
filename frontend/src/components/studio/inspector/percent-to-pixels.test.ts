// percent→pixel 公式单测（契约 §6）：预览与 Phase 2 导出必须得到同一组像素。
// 已知用例：{x_pct:50, y_pct:78.125} on 1080x1920 → left 540, bottom 420, font 62。

import { describe, expect, it } from "vitest";

import { overlayPixels, percentToPixels, withOpacity } from "./percent-to-pixels";
import { DEFAULT_SUBTITLE_CONFIG } from "./types";

describe("percentToPixels", () => {
  it("默认底部居中基准（1080x1920）→ x=540 / bottom=420（对应 ASS MarginV=420）", () => {
    expect(percentToPixels({ x_pct: 50, y_pct: 78.125 })).toEqual({ x: 540, bottom: 420 });
  });

  it("left = W*x_pct/100，bottom = H*(100-y_pct)/100（按显示画布尺寸缩放）", () => {
    expect(percentToPixels({ x_pct: 50, y_pct: 78.125 }, 540, 960)).toEqual({ x: 270, bottom: 210 });
    expect(percentToPixels({ x_pct: 25, y_pct: 50 }, 1080, 1920)).toEqual({ x: 270, bottom: 960 });
  });

  it("四舍五入到整数像素（与后端 round 一致）", () => {
    // 1080*33.3/100 = 359.64 → 360；1920*33.3/100 = 639.36 → 639。
    expect(percentToPixels({ x_pct: 33.3, y_pct: 66.7 }, 1080, 1920)).toEqual({ x: 360, bottom: 639 });
  });
});

describe("overlayPixels", () => {
  it("已知配置：{x_pct:50, y_pct:78.125} on 1080x1920 → left 540 / bottom 420 / font 62", () => {
    expect(overlayPixels(DEFAULT_SUBTITLE_CONFIG, 1080, 1920)).toEqual({ left: 540, bottom: 420, fontSize: 62 });
  });

  it("fontSize = font_size_px * (W/1080)：半宽画布 → 字号减半", () => {
    expect(overlayPixels(DEFAULT_SUBTITLE_CONFIG, 540, 960)).toEqual({ left: 270, bottom: 210, fontSize: 31 });
  });

  it("自定义字号与位置（纯公式，无隐藏偏移）", () => {
    const pixels = overlayPixels(
      { ...DEFAULT_SUBTITLE_CONFIG, font_size_px: 44, position: { x_pct: 25, y_pct: 80 } },
      1080,
      1920,
    );
    expect(pixels).toEqual({ left: 270, bottom: 384, fontSize: 44 });
  });
});

describe("withOpacity", () => {
  it("#RRGGBB → rgba()", () => {
    expect(withOpacity("#000000", 0.5)).toBe("rgba(0, 0, 0, 0.5)");
    expect(withOpacity("#FFFFFF", 1)).toBe("rgba(255, 255, 255, 1)");
  });

  it("非法颜色原样返回（交给浏览器忽略，不改写配置）", () => {
    expect(withOpacity("red", 0.5)).toBe("red");
  });
});
