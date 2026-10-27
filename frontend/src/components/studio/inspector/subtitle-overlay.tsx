"use client";

// 播放器内实时字幕叠加层（契约 §6）。
//
// 预览 = 导出：位置与字号全部来自 percent-to-pixels.ts 的同一公式
//   left = W * x_pct/100，bottom = H * (100 - y_pct)/100，fontSize = font_size_px * (W/1080)。
// W/H 是「显示出来的 1080x1920 导出画布」尺寸：叠加层铺满预览框（两者都是 9:16），
// 用 ResizeObserver 读预览框实测尺寸换算，所以预览与 Phase 2 的 ASS 输出在同一像素网格上。
//
// 只读展示：叠加层 pointer-events:none，绝不拦截播放/拖拽/裁剪手柄的指针事件。

import { useEffect, useRef, useState } from "react";

import { overlayPixels, withOpacity } from "./percent-to-pixels";
import { DEFAULT_SUBTITLE_CONFIG, type SubtitleConfig, type SubtitleCue } from "./types";

type SubtitleOverlayProps = {
  cues: SubtitleCue[];
  config: SubtitleConfig | null | undefined;
  /** 播放头秒数：落在 cue 区间内才显示。 */
  currentTime: number;
  /** 关掉时不渲染（例如未选成片 / 字幕开关关闭）。 */
  enabled?: boolean;
  /** 覆盖实测：给出已测量的画布宽高时不再观察 DOM（单测用）。 */
  measured?: { width: number; height: number };
};

const CANVAS_RATIO = 1080 / 1920;

/** 在给定预览框尺寸里按 contain 语义求「显示出来的导出画布」尺寸。 */
export function displayedCanvas(measured: { width: number; height: number }): { width: number; height: number } {
  const width = Math.max(0, measured.width);
  const height = Math.max(0, measured.height);
  if (!width || !height) return { width: 0, height: 0 };
  const canvasWidth = Math.min(width, height * CANVAS_RATIO);
  return { width: canvasWidth, height: canvasWidth / CANVAS_RATIO };
}

/** 播放头命中的 cue（含端点；多个命中取最后一个，与「后写的覆盖」直觉一致）。 */
export function cueAt(cues: SubtitleCue[], currentTime: number): SubtitleCue | null {
  let hit: SubtitleCue | null = null;
  for (const cue of cues) {
    if (currentTime >= cue.start && currentTime <= cue.end) hit = cue;
  }
  return hit;
}

export function SubtitleOverlay({ cues, config, currentTime, enabled = true, measured }: SubtitleOverlayProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [observed, setObserved] = useState({ width: 0, height: 0 });

  useEffect(() => {
    if (measured) return undefined;
    const host = hostRef.current;
    if (!host) return undefined;
    const read = () => {
      const rect = host.getBoundingClientRect();
      const width = rect.width || host.clientWidth || 0;
      const height = rect.height || host.clientHeight || 0;
      setObserved((previous) => (previous.width === width && previous.height === height ? previous : { width, height }));
    };
    read();
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(read);
    observer.observe(host);
    return () => observer.disconnect();
  }, [measured]);

  const effective = measured ?? observed;
  const canvas = displayedCanvas(effective);
  const cue = cueAt(cues, currentTime);
  const style = config ?? DEFAULT_SUBTITLE_CONFIG;

  if (!enabled || !cue || canvas.width <= 0) {
    // 仍然渲染宿主层：它负责承载 ResizeObserver（宽度为 0 时不显示任何字幕内容）。
    return <div ref={hostRef} className="inspector-subtitle-overlay" aria-hidden="true" data-testid="inspector-subtitle-overlay" />;
  }

  const pixels = overlayPixels(style, canvas.width, canvas.height);
  const background = style.background?.enabled
    ? withOpacity(style.background.color, style.background.opacity)
    : "transparent";

  return <div ref={hostRef} className="inspector-subtitle-overlay" data-testid="inspector-subtitle-overlay" aria-hidden="true">
    <span
      className="inspector-subtitle-cue"
      data-segment-id={cue.segment_id}
      data-testid="inspector-subtitle-cue"
      style={{
        position: "absolute",
        left: `${pixels.left}px`,
        bottom: `${pixels.bottom}px`,
        transform: "translateX(-50%)",
        fontSize: `${pixels.fontSize}px`,
        lineHeight: 1.24,
        color: style.text_color,
        fontFamily: style.font_family || undefined,
        textShadow: `${style.outline_width > 0 ? "-1px -1px 0" : "0 0 0"} ${style.outline_color}, 1px -1px 0 ${style.outline_color}, -1px 1px 0 ${style.outline_color}, 1px 1px 0 ${style.outline_color}`,
        background,
        padding: style.background?.enabled ? "0.1em 0.35em" : 0,
        borderRadius: style.background?.enabled ? "0.18em" : 0,
        maxWidth: `${Math.round(canvas.width * 0.92)}px`,
        textAlign: "center",
        whiteSpace: "pre-wrap",
      }}
    >
      {cue.text}
    </span>
  </div>;
}
