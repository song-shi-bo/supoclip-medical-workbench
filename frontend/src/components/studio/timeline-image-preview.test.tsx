/**
 * 时间线封面静帧预览测试：窗口判定纯函数 + 预览层渲染（片头/插入两种徽标）。
 * 播放预览与图片轨一致：播放头进入 [start, start+duration) → 显示封面、页面静音。
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { findActiveTimelineImage, type TimelineImageClip } from "./timeline-tracks";
import { TimelineImagePreview } from "./timeline-image-preview";

const CLIPS: TimelineImageClip[] = [
  { id: "img_head", role: "cover_intro", start_s: 0, duration_s: 1.5 },
  { id: "img_mid", role: "image", start_s: 4, duration_s: 1 },
];

describe("findActiveTimelineImage", () => {
  it("播放头落进窗口内 → 返回该实例（含左右开闭边界）", () => {
    expect(findActiveTimelineImage(CLIPS, 0)?.id).toBe("img_head");
    expect(findActiveTimelineImage(CLIPS, 1.4999)?.id).toBe("img_head");
    expect(findActiveTimelineImage(CLIPS, 4.5)?.id).toBe("img_mid");
    expect(findActiveTimelineImage(CLIPS, 4.9999)?.id).toBe("img_mid");
  });

  it("窗口外（结束点、间隙、超尾）→ null", () => {
    expect(findActiveTimelineImage(CLIPS, 1.5)).toBeNull();   // 恰在结束点
    expect(findActiveTimelineImage(CLIPS, 3)).toBeNull();     // 间隙
    expect(findActiveTimelineImage(CLIPS, 5)).toBeNull();     // 恰在末尾结束点
    expect(findActiveTimelineImage(CLIPS, 99)).toBeNull();
  });

  it("空实例列表 → null", () => {
    expect(findActiveTimelineImage([], 0.5)).toBeNull();
  });
});

describe("TimelineImagePreview", () => {
  it("插入实例：9:16 画布 + 位置/时长徽标 + 测试标记", () => {
    render(<TimelineImagePreview instance={CLIPS[1]} clipName="剪辑成片 01" coverUrl="http://x/cover.jpg" />);
    const view = screen.getByTestId("timeline-image-frame-view");
    expect(view.getAttribute("data-role")).toBe("image");
    expect(view.getAttribute("data-start")).toBe("4");
    expect(view.getAttribute("data-duration")).toBe("1");
    expect(screen.getByTestId("timeline-image-frame-canvas")).toBeTruthy();
    expect(screen.getByRole("img", { name: "剪辑成片 01 时间线封面静帧" })).toBeTruthy();
    expect(view.textContent).toContain("00:04");
  });

  it("片头实例：徽标为片头封面", () => {
    render(<TimelineImagePreview instance={CLIPS[0]} clipName="剪辑成片 01" coverUrl="http://x/cover.jpg" />);
    const view = screen.getByTestId("timeline-image-frame-view");
    expect(view.getAttribute("data-role")).toBe("cover_intro");
    expect(view.textContent).toContain("片头封面");
  });
});
