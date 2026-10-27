// percent → pixel 换算：**与后端 ASS 生成器同一公式**（契约 §3.3 / §6）。
//
//   left     = W * x_pct / 100
//   bottom   = H * (100 - y_pct) / 100
//   fontSize = font_size_px * (W / 1080)
//
// W/H 是「显示出来的画布尺寸」（播放器预览里 = 1080x1920 导出画布被 CSS 缩放到的大小）。
// 预览与 Phase 2 导出必须得到同一组像素，所以这里只放纯函数，方便单测钉死公式：
//   {x_pct:50, y_pct:78.125} on 1080x1920 → left 540, bottom 420, font 62。

import type { SubtitleConfig, SubtitlePosition } from "./types";

/** 后端 subtitle_compose.DEFAULT_WIDTH / DEFAULT_HEIGHT / DEFAULT_FONT_SIZE。 */
export const CANVAS_WIDTH_1080 = 1080;
export const CANVAS_HEIGHT_1920 = 1920;
export const DEFAULT_FONT_SIZE_PX = 62;

export type SubtitlePixels = {
  left: number;
  bottom: number;
  fontSize: number;
};

export function percentToPixels(
  position: SubtitlePosition,
  width: number = CANVAS_WIDTH_1080,
  height: number = CANVAS_HEIGHT_1920,
): { x: number; bottom: number } {
  return {
    x: Math.round(width * position.x_pct / 100),
    bottom: Math.round(height * (100 - position.y_pct) / 100),
  };
}

export function overlayPixels(
  config: SubtitleConfig,
  width: number = CANVAS_WIDTH_1080,
  height: number = CANVAS_HEIGHT_1920,
): SubtitlePixels {
  const { x, bottom } = percentToPixels(config.position, width, height);
  return {
    left: x,
    bottom,
    fontSize: config.font_size_px * (width / CANVAS_WIDTH_1080),
  };
}

/** #RRGGBB → rgba()（背景透明度用；解析失败时原样返回，交给浏览器忽略）。 */
export function withOpacity(color: string, opacity: number): string {
  const match = /^#?([0-9a-f]{6})$/i.exec(color.trim());
  if (!match) return color;
  const value = match[1];
  const r = parseInt(value.slice(0, 2), 16);
  const g = parseInt(value.slice(2, 4), 16);
  const b = parseInt(value.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${Math.max(0, Math.min(1, opacity))})`;
}
