// 裁切/重构图浮层测试（vitest + RTL）：片段缩放、原画、铺满、重置、
// 适配/背景/比例锁，「查看比例」与「片段变换」两条轴的分离，
// 以及**应用 / 取消**（草稿语义：只有「应用」会写后端）。

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TransformPanel } from "./transform-panel";
import { defaultTransform, type ClipTransform } from "@/lib/clip-transform";

const edited: ClipTransform = {
  scale: 1.5, positionX: 20, positionY: -10,
  crop: { left: 0.1, top: 0, right: 0.9, bottom: 1 },
  fitMode: "cover", rotation: 0,
};

const renderPanel = (overrides: Partial<Parameters<typeof TransformPanel>[0]> = {}) => {
  const handlers = {
    onTransformChange: vi.fn(),
    onBackgroundChange: vi.fn(),
    onRatioLockChange: vi.fn(),
    onViewerZoomChange: vi.fn(),
    onFitView: vi.fn(),
    onOriginalRatio: vi.fn(),
    onFillCanvas: vi.fn(),
    onResetTransform: vi.fn(),
    onApply: vi.fn(),
    onCancel: vi.fn(),
    onToggleCollapse: vi.fn(),
  };
  const props = {
    transform: edited,
    background: "blur" as const,
    ratioLock: false,
    viewerZoom: 100,
    clipLabel: "片段 1",
    ...handlers,
    ...overrides,
  };
  const rendered = render(<TransformPanel {...props} />);
  return { props, handlers, unmount: rendered.unmount };
};

describe("TransformPanel", () => {
  it("放大/缩小按钮按倍率调整片段 scale 并钳制", () => {
    const { handlers } = renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "放大片段" }));
    expect(handlers.onTransformChange).toHaveBeenLastCalledWith(expect.objectContaining({ scale: 1.5 * 1.15 }));
    fireEvent.click(screen.getByRole("button", { name: "缩小片段" }));
    expect(handlers.onTransformChange).toHaveBeenLastCalledWith(expect.objectContaining({ scale: 1.5 / 1.15 }));
  });

  it("片段缩放滑杆按百分比设置 scale，并显示缩放百分比", () => {
    const { handlers } = renderPanel();
    fireEvent.change(screen.getByRole("slider", { name: "片段缩放" }), { target: { value: "200" } });
    expect(handlers.onTransformChange).toHaveBeenLastCalledWith(expect.objectContaining({ scale: 2 }));
    expect(screen.getByTitle("当前片段缩放百分比").textContent).toBe("150%");
  });

  it("原画与铺满画布走片段回调，并写明影响导出", () => {
    const { handlers } = renderPanel();
    const original = screen.getByRole("button", { name: "原画" });
    expect(original.getAttribute("title")).toContain("影响导出");
    fireEvent.click(original);
    expect(handlers.onOriginalRatio).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "铺满画布" }));
    expect(handlers.onFillCanvas).toHaveBeenCalledTimes(1);
  });

  it("重置按钮在默认态禁用，点击触发重置回调", () => {
    const { handlers, unmount } = renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "重置" }));
    expect(handlers.onResetTransform).toHaveBeenCalledTimes(1);
    unmount();
    renderPanel({ transform: defaultTransform() });
    expect(screen.getByRole("button", { name: "重置" })).toBeDisabled();
  });

  it("适合画布只触发查看回调，不触碰片段变换", () => {
    const { handlers } = renderPanel();
    const button = screen.getByRole("button", { name: "适合画布" });
    expect(button.getAttribute("title")).toContain("不影响导出");
    fireEvent.click(button);
    expect(handlers.onFitView).toHaveBeenCalledTimes(1);
    expect(handlers.onTransformChange).not.toHaveBeenCalled();
  });

  it("查看比例滑杆走独立回调，不进片段变换", () => {
    const { handlers } = renderPanel();
    const slider = screen.getByRole("slider", { name: "查看比例" });
    expect(slider.getAttribute("title")).toContain("绝不进入片段变换与导出参数");
    fireEvent.change(slider, { target: { value: "160" } });
    expect(handlers.onViewerZoomChange).toHaveBeenLastCalledWith(160);
    expect(handlers.onTransformChange).not.toHaveBeenCalled();
  });

  it("适配切换 cover/contain，cover 下背景按钮禁用", () => {
    const { handlers, unmount } = renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "适应" }));
    expect(handlers.onTransformChange).toHaveBeenLastCalledWith({ ...edited, fitMode: "contain" });
    unmount();
    renderPanel({ transform: { ...edited, fitMode: "cover" } });
    expect(screen.getByRole("button", { name: "模糊" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "纯色" })).toBeDisabled();
  });

  it("背景切换仅 contain 下可用且触发回调", () => {
    const { handlers } = renderPanel({ transform: { ...edited, fitMode: "contain" } });
    fireEvent.click(screen.getByRole("button", { name: "纯色" }));
    expect(handlers.onBackgroundChange).toHaveBeenCalledWith("color");
  });

  it("比例锁与退出按钮（退出 = 取消，回滚快照）", () => {
    const { handlers } = renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "比例" }));
    expect(handlers.onRatioLockChange).toHaveBeenCalledWith(true);
    fireEvent.click(screen.getByRole("button", { name: "退出裁剪变换" }));
    expect(handlers.onCancel).toHaveBeenCalledTimes(1);
    expect(handlers.onApply).not.toHaveBeenCalled();
  });

  it("「应用」是唯一的落盘路径，「取消」不写后端", () => {
    const { handlers, unmount } = renderPanel({ dirty: true });
    fireEvent.click(screen.getByRole("button", { name: "应用" }));
    expect(handlers.onApply).toHaveBeenCalledTimes(1);
    expect(handlers.onCancel).not.toHaveBeenCalled();
    unmount();
    const clean = renderPanel({ dirty: false });
    expect(screen.getByRole("button", { name: "应用" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(clean.handlers.onCancel).toHaveBeenCalledTimes(1);
    expect(clean.handlers.onApply).not.toHaveBeenCalled();
  });

  it("已改动时提示「有未应用的改动」，未改动时提示「没有改动」", () => {
    const { unmount } = renderPanel({ dirty: true });
    expect(screen.getByText("有未应用的改动")).toBeInTheDocument();
    unmount();
    renderPanel({ dirty: false });
    expect(screen.getByText("没有改动")).toBeInTheDocument();
  });

  it("折叠只隐藏设置行，展开/折叠按钮带说明", () => {
    const { handlers, unmount } = renderPanel();
    const collapse = screen.getByRole("button", { name: "折叠裁剪变换设置" });
    expect(collapse.getAttribute("title")).toContain("让画面与裁剪框显示得更大");
    fireEvent.click(collapse);
    expect(handlers.onToggleCollapse).toHaveBeenCalledTimes(1);
    unmount();
    renderPanel({ collapsed: true });
    expect(screen.getByRole("button", { name: "展开裁剪变换设置" })).toBeInTheDocument();
    expect(document.querySelector(".transform-panel")?.className).toContain("collapsed");
  });

  it("锁定/禁用态下所有片段编辑入口不可用，应用也不可点", () => {
    renderPanel({ transformLocked: true, dirty: true });
    expect(screen.getByRole("button", { name: "放大片段" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "原画" })).toBeDisabled();
    expect(screen.getByRole("slider", { name: "片段缩放" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "应用" })).toBeDisabled();
    // 查看比例属于观看操作，旋转锁定下仍可用。
    expect(screen.getByRole("slider", { name: "查看比例" })).not.toBeDisabled();
  });
});
