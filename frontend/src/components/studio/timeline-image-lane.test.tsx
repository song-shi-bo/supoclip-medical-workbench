/**
 * 时间线图片轨（封面静帧拖拽）组件测试。
 *
 * 覆盖：图片轨渲染（实例块按 start_s 排位、开头=片头封面样式、空态提示）、
 * 拖放落点回调（像素数学 timeToPx 与 seek 同一套坐标）、移动实例时携带
 * instance_id、删除按钮只删实例。
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { createRef } from "react";

import {
  TimelineTracks, TIMELINE_COVER_DRAG_TYPE, type TimelineImageClip,
} from "./timeline-tracks";
import type { TimelineView } from "@/lib/timeline-view";

const VIEW: TimelineView = {
  zoom_px_per_second: 100,
  audio_track_height: 72,
  waveform_ratio: "standard",
  scroll_left: 0,
};

const IMAGES: TimelineImageClip[] = [
  { id: "img_head", role: "cover_intro", start_s: 0, duration_s: 1.5 },
  { id: "img_mid", role: "image", start_s: 4, duration_s: 1 },
];

function setup(imageClips: TimelineImageClip[] = IMAGES) {
  const handlers = {
    onSeek: vi.fn(),
    onSelectSegment: vi.fn(),
    onViewportWidthChange: vi.fn(),
    onDropCoverImage: vi.fn(),
    onRemoveImageClip: vi.fn(),
  };
  const view = render(
    <TimelineTracks
      duration={20}
      currentTime={0}
      view={VIEW}
      ranges={[{ id: "r1", source_start: 0, source_end: 20 }]}
      cues={[]}
      segments={[]}
      selectedSegment={null}
      audioTracks={[]}
      peaksByTrack={{}}
      peaksDurationByTrack={{}}
      activeMediaName="剪辑成片 01"
      imageClips={imageClips}
      scrollElementRef={createRef<HTMLDivElement>()}
      viewportWidthPx={1200}
      scrollLeft={0}
      {...handlers}
    />,
  );
  return { ...handlers, ...view };
}

describe("时间线图片轨", () => {
  /** jsdom 的 DragEvent 对 clientX 初始化不稳，直接构造 Event + Object.assign 保证确定性。 */
  const fireDrag = (element: Element, type: string, init: Record<string, unknown>) => {
    const event = new Event(type, { bubbles: true, cancelable: true });
    Object.assign(event, init);
    fireEvent(element, event);
  };
  const dragData = (payload: Record<string, unknown>) => ({
    types: [TIMELINE_COVER_DRAG_TYPE],
    getData: (type: string) => (type === TIMELINE_COVER_DRAG_TYPE ? JSON.stringify(payload) : ""),
    dropEffect: "none",
  });

  it("渲染实例块：开头实例带片头封面标记，位置/宽度按 start_s/duration 落位", () => {
    const { container } = setup();
    const lane = screen.getByTestId("timeline-image-lane");
    expect(lane).toBeTruthy();
    expect(lane.getAttribute("data-image-count")).toBe("2");
    const head = screen.getByTestId("timeline-image-clip-img_head");
    expect(head.className).toContain("is-head");
    expect(head.getAttribute("data-role")).toBe("cover_intro");
    expect(head.style.left).toBe("0px");
    expect(head.style.width).toBe("150px"); // 1.5s × 100px/s
    const mid = screen.getByTestId("timeline-image-clip-img_mid");
    expect(mid.className).not.toContain("is-head");
    expect(mid.style.left).toBe("400px"); // 4s × 100px/s
    expect(mid.style.width).toBe("100px");
    // 每个实例都有删除按钮
    expect(container.querySelectorAll(".timeline-image-remove").length).toBe(2);
  });

  it("空实例时显示拖拽提示文案", () => {
    setup([]);
    expect(screen.getByTestId("timeline-image-lane").textContent).toContain("把封面图片拖到这里");
  });

  it("拖放到图片轨：落点 x 换算成秒回调；载荷带 clip_id（新增）", () => {
    const rect = { left: 0, top: 0, right: 1154, bottom: 100, width: 1154, height: 100 } as DOMRect;
    const spy = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(rect);
    const { onDropCoverImage } = setup();
    const lane = screen.getByTestId("timeline-image-lane");
    // 轨道局部像素 = clientX − 154（标签列）；可用宽 = 1154 − 154 = 1000。
    // x = 154 + 0.25 × 1000 → ratio 0.25 → t = 0.25 × 20s = 5s
    fireDrag(lane, "dragover", { dataTransfer: dragData({ clip_id: "clip-1" }) });
    fireDrag(lane, "drop", { clientX: 404, dataTransfer: dragData({ clip_id: "clip-1" }) });
    expect(onDropCoverImage).toHaveBeenCalledTimes(1);
    expect(onDropCoverImage).toHaveBeenCalledWith(5, { clip_id: "clip-1" });
    spy.mockRestore();
  });

  it("拖放已有实例：载荷带 instance_id（移动），视频轨也是合法落点", () => {
    const rect = { left: 0, top: 0, right: 1154, bottom: 100, width: 1154, height: 100 } as DOMRect;
    const spy = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(rect);
    const { onDropCoverImage } = setup();
    const videoLane = screen.getByTestId("timeline-video-lane");
    // x = 154 → ratio 0 → t = 0
    fireDrag(videoLane, "drop", { clientX: 154, dataTransfer: dragData({ instance_id: "img_mid" }) });
    expect(onDropCoverImage).toHaveBeenCalledWith(0, { instance_id: "img_mid" });
    spy.mockRestore();
  });

  it("无关拖拽（非封面载荷）不触发落点回调", () => {
    const { onDropCoverImage } = setup();
    const lane = screen.getByTestId("timeline-image-lane");
    fireDrag(lane, "drop", {
      clientX: 200,
      dataTransfer: { types: ["text/plain"], getData: () => "hello", dropEffect: "none" },
    });
    expect(onDropCoverImage).not.toHaveBeenCalled();
  });

  it("删除按钮只删对应实例，不冒泡到 seek", () => {
    const { onRemoveImageClip, onSeek } = setup();
    fireEvent.click(screen.getByTestId("timeline-image-remove-img_mid"));
    expect(onRemoveImageClip).toHaveBeenCalledWith("img_mid");
    expect(onSeek).not.toHaveBeenCalled();
  });
});

describe("拖拽反馈", () => {
  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
      { left: 0, top: 0, right: 1154, bottom: 100, width: 1154, height: 100 } as DOMRect);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("dragenter 高亮图片轨，drop/dragleave 取消高亮", () => {
    const { onDropCoverImage } = setup();
    const lane = screen.getByTestId("timeline-image-lane");
    const dataTransfer = {
      types: [TIMELINE_COVER_DRAG_TYPE],
      getData: () => JSON.stringify({ clip_id: "clip-1" }),
      dropEffect: "none",
    };
    fireEvent.dragEnter(lane, { dataTransfer });
    expect(lane.className).toContain("drop-hover");
    fireEvent.drop(lane, { clientX: 154, dataTransfer });
    expect(lane.className).not.toContain("drop-hover");
    expect(onDropCoverImage).toHaveBeenCalledTimes(1);
    fireEvent.dragEnter(lane, { dataTransfer });
    fireEvent.dragLeave(lane, { dataTransfer });
    expect(lane.className).not.toContain("drop-hover");
  });
});
