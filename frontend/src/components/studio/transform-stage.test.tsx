// 变换舞台测试（vitest + RTL）。
//
// 回归目标：
//  1) 素材被删除后必须**真正清空** <video>（去 src + load()）——只把 src 设空时
//     浏览器会保留最后一帧，用户看到的现象是「素材删了，画面还在」。
//  2) 裁切/重构图模式必须有 8 个控制点（四角 + 四边中点）。
//  3) 裁剪拖拽规则：四角等比、四边自由、Shift 锁比例。
//  4) 预览比例（viewerAspect）只是观看层，不改动任何 transform。

import { fireEvent, render } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CROP_HANDLES, TransformStage, cropDataAttribute, resolveCropDrag } from "./transform-stage";
import { type CropRect } from "@/lib/clip-transform";

const TRANSFORM = {
  scale: 1,
  positionX: 0,
  positionY: 0,
  crop: { left: 0, top: 0, right: 1, bottom: 1 },
  fitMode: "cover" as const,
  rotation: 0 as const,
};

const renderStage = (videoUrl: string | null, overrides: Record<string, unknown> = {}) => {
  const videoRef = createRef<HTMLVideoElement>();
  const props = {
    videoRef,
    sourceWidth: 1080,
    sourceHeight: 1920,
    transform: TRANSFORM,
    background: "blur" as const,
    transformMode: false,
    viewerZoom: 100,
    onTransformChange: vi.fn(),
    ...overrides,
  };
  const view = render(<TransformStage {...props} videoUrl={videoUrl} />);
  return {
    videoRef,
    view,
    rerenderWith: (nextUrl: string | null) => view.rerender(<TransformStage {...props} videoUrl={nextUrl} />),
  };
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("TransformStage 素材移除", () => {
  it("有素材时主画面带 src", () => {
    const { videoRef } = renderStage("http://example.test/a.mp4");
    expect(videoRef.current?.getAttribute("src")).toBe("http://example.test/a.mp4");
  });

  it("素材被移除后清空 src、暂停并调用 load()（否则旧帧会留在画面上）", () => {
    const loadSpy = vi.spyOn(window.HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    const pauseSpy = vi.spyOn(window.HTMLMediaElement.prototype, "pause").mockImplementation(() => {});

    const { videoRef, rerenderWith } = renderStage("http://example.test/a.mp4");
    expect(videoRef.current?.getAttribute("src")).toBe("http://example.test/a.mp4");

    rerenderWith(null);

    expect(videoRef.current?.hasAttribute("src")).toBe(false);
    expect(loadSpy).toHaveBeenCalled();
    expect(pauseSpy).toHaveBeenCalled();
  });

  it("从一份素材切到另一份素材时保留新 src（不误清）", () => {
    const { videoRef, rerenderWith } = renderStage("http://example.test/a.mp4");
    rerenderWith("http://example.test/b.mp4");
    expect(videoRef.current?.getAttribute("src")).toBe("http://example.test/b.mp4");
  });
});

describe("TransformStage 裁切/重构图 8 个控制点", () => {
  it("非编辑模式不渲染控制点（普通播放器态）", () => {
    const { view } = renderStage("http://example.test/a.mp4");
    expect(view.container.querySelectorAll(".crop-handle")).toHaveLength(0);
  });

  it("编辑模式渲染四角 + 四边中点共 8 个控制点，且可访问名齐全", () => {
    const { view } = renderStage("http://example.test/a.mp4", { transformMode: true });
    const handles = view.container.querySelectorAll(".crop-handle");
    expect(handles).toHaveLength(8);
    expect(CROP_HANDLES).toHaveLength(8);
    expect(view.container.querySelectorAll(".crop-handle.corner-handle")).toHaveLength(4);
    expect(view.container.querySelectorAll(".crop-handle.edge-handle")).toHaveLength(4);
    for (const handle of CROP_HANDLES) {
      expect(view.container.querySelector(`.crop-handle.handle-${handle}`)).not.toBeNull();
    }
    const labels = Array.from(handles).map((element) => element.getAttribute("aria-label"));
    expect(new Set(labels).size).toBe(8);
  });

  it("没有素材时即使处于编辑模式也不渲染控制点", () => {
    const { view } = renderStage(null, { transformMode: true });
    expect(view.container.querySelectorAll(".crop-handle")).toHaveLength(0);
  });

  it("编辑模式渲染导出边界与裁剪框", () => {
    const { view } = renderStage("http://example.test/a.mp4", { transformMode: true });
    expect(view.container.querySelector(".export-boundary")).not.toBeNull();
    expect(view.container.querySelector(".crop-overlay")).not.toBeNull();
  });
});

describe("resolveCropDrag 拖拽规则", () => {
  const start: CropRect = { left: 0.2, top: 0.2, right: 0.8, bottom: 0.8 };
  const base = { startCrop: start, sourceWidth: 1080, sourceHeight: 1920, ratioLock: false, shiftKey: false };

  it("拖四边中点只改该方向的范围（自由调整）", () => {
    const east = resolveCropDrag({ ...base, handle: "e", deltaNormX: -0.1, deltaNormY: 0.3 });
    expect(east.right).toBeCloseTo(0.7, 6);
    expect(east.top).toBeCloseTo(start.top, 6);
    expect(east.bottom).toBeCloseTo(start.bottom, 6);
    const north = resolveCropDrag({ ...base, handle: "n", deltaNormX: 0.3, deltaNormY: 0.1 });
    expect(north.top).toBeCloseTo(0.3, 6);
    expect(north.left).toBeCloseTo(start.left, 6);
    expect(north.right).toBeCloseTo(start.right, 6);
  });

  it("拖四角是等比缩放（宽高按源像素比例同步变化）", () => {
    const result = resolveCropDrag({ ...base, handle: "se", deltaNormX: -0.1, deltaNormY: 0 });
    const widthBefore = (start.right - start.left) * 1080;
    const heightBefore = (start.bottom - start.top) * 1920;
    const widthAfter = (result.right - result.left) * 1080;
    const heightAfter = (result.bottom - result.top) * 1920;
    expect(widthAfter).toBeLessThan(widthBefore);
    expect(widthAfter / heightAfter).toBeCloseTo(widthBefore / heightBefore, 4);
  });

  it("Shift 让四边也锁定宽高比", () => {
    const free = resolveCropDrag({ ...base, handle: "e", deltaNormX: -0.15, deltaNormY: 0 });
    const locked = resolveCropDrag({ ...base, handle: "e", deltaNormX: -0.15, deltaNormY: 0, shiftKey: true });
    expect(free.bottom).toBeCloseTo(start.bottom, 6);
    expect(locked.bottom).not.toBeCloseTo(start.bottom, 6);
    const widthBefore = (start.right - start.left) * 1080;
    const heightBefore = (start.bottom - start.top) * 1920;
    expect(((locked.right - locked.left) * 1080) / ((locked.bottom - locked.top) * 1920)).toBeCloseTo(widthBefore / heightBefore, 4);
  });

  it("面板比例锁打开时四边也锁定宽高比", () => {
    const free = resolveCropDrag({ ...base, handle: "s", deltaNormX: 0, deltaNormY: -0.15 });
    expect(free.left).toBeCloseTo(start.left, 6);
    expect(free.right).toBeCloseTo(start.right, 6);
    const locked = resolveCropDrag({ ...base, handle: "s", deltaNormX: 0, deltaNormY: -0.15, ratioLock: true });
    // 锁比例后：高度被改，宽度按同一宽高比围绕中心联动。
    expect(locked.bottom).toBeCloseTo(0.65, 6);
    expect(locked.left).not.toBeCloseTo(start.left, 6);
    const widthBefore = (start.right - start.left) * 1080;
    const heightBefore = (start.bottom - start.top) * 1920;
    expect(((locked.right - locked.left) * 1080) / ((locked.bottom - locked.top) * 1920)).toBeCloseTo(widthBefore / heightBefore, 4);
  });

  it("裁剪区最小跨度与画布边界都被钳制", () => {
    const tiny = resolveCropDrag({ ...base, handle: "se", deltaNormX: -0.9, deltaNormY: -0.9 });
    expect(tiny.right - tiny.left).toBeGreaterThanOrEqual(0.05 - 1e-9);
    expect(tiny.bottom - tiny.top).toBeGreaterThanOrEqual(0.05 - 1e-9);
    const outside = resolveCropDrag({ ...base, handle: "w", deltaNormX: -0.5, deltaNormY: 0 });
    expect(outside.left).toBeGreaterThanOrEqual(0);
    expect(outside.right).toBeLessThanOrEqual(1);
  });
});

describe("cropDataAttribute 观测钩子", () => {
  it("把归一化裁剪区暴露成 data 属性（只读，供浏览器接受测试断言）", () => {
    expect(cropDataAttribute({
      scale: 1, positionX: 0, positionY: 0, fitMode: "cover", rotation: 0,
      crop: { left: 0, top: 0.3019234, right: 0.05, bottom: 0.3519229 },
    })).toBe("0,0.3019,0.05,0.3519");
  });
});

describe("TransformStage 预览比例（viewer-only）", () => {
  it("viewerAspect 只体现在预览框上，不改动 transform", () => {
    const onTransformChange = vi.fn();
    const stage = renderStage("http://example.test/a.mp4", { viewerAspect: "1:1", onTransformChange });
    expect(stage.view.container.querySelector(".transform-viewport")?.getAttribute("data-viewer-aspect")).toBe("1:1");
    expect(stage.view.container.querySelector(".viewer-frame")?.getAttribute("data-viewer-aspect")).toBe("1:1");
    expect(onTransformChange).not.toHaveBeenCalled();
  });

  it("默认预览比例是 9:16", () => {
    const { view } = renderStage("http://example.test/a.mp4");
    expect(view.container.querySelector(".viewer-frame")?.getAttribute("data-viewer-aspect")).toBe("9:16");
  });

  it("普通播放态滚轮改的是查看缩放（viewerZoom），不是片段变换", () => {
    const onTransformChange = vi.fn();
    const onViewerZoomChange = vi.fn();
    const { view } = renderStage("http://example.test/a.mp4", { onTransformChange, onViewerZoomChange });
    const canvas = view.container.querySelector(".transform-canvas") as HTMLElement;
    fireEvent.wheel(canvas, { deltaY: -100 });
    expect(onViewerZoomChange).toHaveBeenCalled();
    expect(onTransformChange).not.toHaveBeenCalled();
  });

  it("编辑模式滚轮改的是片段缩放（影响导出）", () => {
    const onTransformChange = vi.fn();
    const onViewerZoomChange = vi.fn();
    const { view } = renderStage("http://example.test/a.mp4", {
      transformMode: true, onTransformChange, onViewerZoomChange,
    });
    const canvas = view.container.querySelector(".transform-canvas") as HTMLElement;
    fireEvent.wheel(canvas, { deltaY: -100 });
    expect(onTransformChange).toHaveBeenCalledWith(expect.objectContaining({ scale: expect.any(Number) }));
    expect(onViewerZoomChange).not.toHaveBeenCalled();
  });
});
