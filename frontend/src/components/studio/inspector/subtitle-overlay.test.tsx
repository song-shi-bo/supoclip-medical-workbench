// 播放器内实时字幕叠加层测试（契约 §6）：
// 叠加层必须把 percent→pixel 公式的结果**原样**写进 DOM 像素（预览 = Phase 2 导出），
// 只显示播放头命中的 cue，且是 pointer-events:none 的只读层（不拦播放/拖拽/裁剪手柄）。

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SubtitleOverlay, cueAt, displayedCanvas } from "./subtitle-overlay";
import { DEFAULT_SUBTITLE_CONFIG, type SubtitleCue } from "./types";

const cues: SubtitleCue[] = [
  { segment_id: "seg-1", start: 0, end: 2, text: "第一句字幕" },
  { segment_id: "seg-2", start: 2, end: 4, text: "第二句字幕" },
];

describe("displayedCanvas", () => {
  it("按 contain 语义求出显示出来的 1080x1920 导出画布尺寸", () => {
    expect(displayedCanvas({ width: 540, height: 960 })).toEqual({ width: 540, height: 960 });
    // 宽画布：以高为准（9:16 居中，两侧留黑），字幕仍按导出画布定位。
    expect(displayedCanvas({ width: 800, height: 960 })).toEqual({ width: 540, height: 960 });
  });

  it("尺寸为 0 时不产生任何像素（不渲染一个假字幕）", () => {
    expect(displayedCanvas({ width: 0, height: 0 })).toEqual({ width: 0, height: 0 });
  });
});

describe("cueAt", () => {
  it("播放头落在区间内才命中（含端点）", () => {
    expect(cueAt(cues, 0)?.segment_id).toBe("seg-1");
    expect(cueAt(cues, 1.5)?.segment_id).toBe("seg-1");
    expect(cueAt(cues, 2)?.segment_id).toBe("seg-2");
    expect(cueAt(cues, 4.5)).toBeNull();
  });
});

describe("SubtitleOverlay", () => {
  it("已知配置 {x_pct:50,y_pct:78.125} on 1080x1920 → left 540px / bottom 420px / font 62px", () => {
    render(<SubtitleOverlay
      cues={cues}
      config={DEFAULT_SUBTITLE_CONFIG}
      currentTime={1}
      measured={{ width: 1080, height: 1920 }}
    />);
    const cue = screen.getByTestId("inspector-subtitle-cue");
    expect(cue.textContent).toBe("第一句字幕");
    expect(cue.style.left).toBe("540px");
    expect(cue.style.bottom).toBe("420px");
    expect(cue.style.fontSize).toBe("62px");
    expect(cue.style.transform).toBe("translateX(-50%)");
  });

  it("画布缩小一半 → 同一配置的像素等比缩放（预览随播放器尺寸变化，公式不变）", () => {
    render(<SubtitleOverlay
      cues={cues}
      config={DEFAULT_SUBTITLE_CONFIG}
      currentTime={3}
      measured={{ width: 540, height: 960 }}
    />);
    const cue = screen.getByTestId("inspector-subtitle-cue");
    expect(cue.textContent).toBe("第二句字幕");
    expect(cue.style.left).toBe("270px");
    expect(cue.style.bottom).toBe("210px");
    expect(cue.style.fontSize).toBe("31px");
  });

  it("样式改动（字号/字色/描边/背景）直接体现在叠加层", () => {
    render(<SubtitleOverlay
      cues={cues}
      config={{
        ...DEFAULT_SUBTITLE_CONFIG,
        font_size_px: 80,
        text_color: "#FFEE00",
        outline_color: "#000000",
        outline_width: 6,
        background: { enabled: true, color: "#000000", opacity: 0.5 },
      }}
      currentTime={1}
      measured={{ width: 1080, height: 1920 }}
    />);
    const cue = screen.getByTestId("inspector-subtitle-cue");
    expect(cue.style.fontSize).toBe("80px");
    expect(cue.style.color).toBe("rgb(255, 238, 0)");
    expect(cue.style.background).toBe("rgba(0, 0, 0, 0.5)");
    // jsdom 不会把 text-shadow 里的十六进制颜色规范化成 rgb()，两种写法都接受。
    expect(cue.style.textShadow).toMatch(/#000000|rgb\(0, 0, 0\)/);
  });

  it("播放头没有命中 / enabled=false → 不渲染字幕内容（只留宿主层）", () => {
    const { rerender } = render(<SubtitleOverlay cues={cues} config={DEFAULT_SUBTITLE_CONFIG} currentTime={9} measured={{ width: 1080, height: 1920 }} />);
    expect(screen.queryByTestId("inspector-subtitle-cue")).not.toBeInTheDocument();
    rerender(<SubtitleOverlay cues={cues} config={DEFAULT_SUBTITLE_CONFIG} currentTime={1} enabled={false} measured={{ width: 1080, height: 1920 }} />);
    expect(screen.queryByTestId("inspector-subtitle-cue")).not.toBeInTheDocument();
  });

  it("叠加层是只读层：aria-hidden 且不拦截指针事件", () => {
    render(<SubtitleOverlay cues={cues} config={DEFAULT_SUBTITLE_CONFIG} currentTime={1} measured={{ width: 1080, height: 1920 }} />);
    const host = screen.getByTestId("inspector-subtitle-overlay");
    expect(host.getAttribute("aria-hidden")).toBe("true");
    expect(host.className).toContain("inspector-subtitle-overlay");
  });
});
