// 播放控制栏测试（vitest + RTL）：三区布局、语义红线、响应式折叠钩子。
//
// 语义红线断言：
//  * 「原画」= 片段操作（onOriginalRatio，会写入片段并影响导出）；
//  * 「比例」= 只看（onViewerAspectChange，tooltip 必须写明导出仍 9:16）；
//  * 音量/全屏/更多都只是观看操作。

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PlayerControlBar } from "./player-control-bar";
import { VIEWER_ASPECT_TOOLTIP } from "./viewer-aspect";

const renderBar = (overrides: Partial<Parameters<typeof PlayerControlBar>[0]> = {}) => {
  const handlers = {
    onTogglePlay: vi.fn(),
    onSeekBy: vi.fn(),
    onSeekFrame: vi.fn(),
    onVolumeChange: vi.fn(),
    onToggleMute: vi.fn(),
    onOriginalRatio: vi.fn(),
    onToggleTransformMode: vi.fn(),
    onToggleFullscreen: vi.fn(),
    onViewerAspectChange: vi.fn(),
  };
  const props = {
    currentTime: 12,
    duration: 605,
    playing: false,
    hasMedia: true,
    volume: 1,
    muted: false,
    viewerAspect: "9:16" as const,
    transformMode: false,
    ...handlers,
    ...overrides,
  };
  const rendered = render(<PlayerControlBar {...props} />);
  return { props, handlers, unmount: rendered.unmount };
};

describe("PlayerControlBar", () => {
  it("左区常驻显示当前时间与总时长（完整 + 紧凑两种格式，CSS 决定显示哪个）", () => {
    renderBar();
    expect(screen.getByText(/00:00:12/)).toBeInTheDocument();
    expect(screen.getByText(/00:10:05/)).toBeInTheDocument();
    expect(screen.getByText(/^00:12/)).toBeInTheDocument();
  });

  it("中区：播放/暂停主按钮、后退/前进 5 秒、逐帧微调都走各自回调", () => {
    const { handlers } = renderBar();
    fireEvent.click(screen.getByRole("button", { name: "播放" }));
    expect(handlers.onTogglePlay).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "后退五秒" }));
    expect(handlers.onSeekBy).toHaveBeenLastCalledWith(-5);
    fireEvent.click(screen.getByRole("button", { name: "前进五秒" }));
    expect(handlers.onSeekBy).toHaveBeenLastCalledWith(5);
    fireEvent.click(screen.getByRole("button", { name: "上一帧" }));
    expect(handlers.onSeekFrame).toHaveBeenLastCalledWith(-1);
    fireEvent.click(screen.getByRole("button", { name: "下一帧" }));
    expect(handlers.onSeekFrame).toHaveBeenLastCalledWith(1);
    expect(screen.getByRole("button", { name: "播放" }).getAttribute("title")).toContain("空格");
  });

  it("播放态下主按钮切换为暂停", () => {
    renderBar({ playing: true });
    expect(screen.getByRole("button", { name: "暂停" })).toBeInTheDocument();
  });

  it("音量浮层里的滑块与静音按钮走预览音量回调（不写片段）", () => {
    const { handlers } = renderBar();
    fireEvent.click(screen.getByRole("button", { name: "音量" }));
    const slider = screen.getByRole("slider", { name: "音量" });
    expect(slider.getAttribute("title")).toContain("不写入片段与导出");
    fireEvent.change(slider, { target: { value: "40" } });
    expect(handlers.onVolumeChange).toHaveBeenLastCalledWith(0.4);
    fireEvent.click(screen.getByRole("button", { name: "静音" }));
    expect(handlers.onToggleMute).toHaveBeenCalledTimes(1);
  });

  it("原画是片段操作，tooltip 写明会写入片段并影响导出", () => {
    const { handlers } = renderBar();
    const button = screen.getByRole("button", { name: "原画" });
    expect(button.getAttribute("title")).toContain("影响导出");
    fireEvent.click(button);
    expect(handlers.onOriginalRatio).toHaveBeenCalledTimes(1);
  });

  it("比例菜单只影响预览，绝不触发片段变换", () => {
    const { handlers } = renderBar();
    const trigger = screen.getByRole("button", { name: "比例" });
    expect(trigger.getAttribute("title")).toContain("导出仍按流水线 9:16");
    fireEvent.click(trigger);
    const menu = screen.getByRole("menu", { name: "预览画布比例" });
    expect(menu.textContent).toContain(VIEWER_ASPECT_TOOLTIP);
    for (const label of ["9:16", "1:1", "16:9", "原始比例"]) {
      expect(screen.getByRole("menuitemradio", { name: new RegExp(`^${label.replace(":", "\\:")}`) })).toBeInTheDocument();
    }
    fireEvent.click(screen.getByRole("menuitemradio", { name: /^1:1/ }));
    expect(handlers.onViewerAspectChange).toHaveBeenLastCalledWith("1:1");
    expect(handlers.onOriginalRatio).not.toHaveBeenCalled();
  });

  it("比例菜单标记当前项", () => {
    renderBar({ viewerAspect: "16:9" });
    fireEvent.click(screen.getByRole("button", { name: "比例" }));
    expect(screen.getByRole("menuitemradio", { name: /^16:9/ }).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("menuitemradio", { name: /^9:16/ }).getAttribute("aria-checked")).toBe("false");
  });

  it("裁切/重构图按钮反映激活态、可进入，锁定态给出说明", () => {
    const { handlers, unmount } = renderBar();
    const button = screen.getByRole("button", { name: "裁切/重构图" });
    expect(button.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(button);
    expect(handlers.onToggleTransformMode).toHaveBeenCalledTimes(1);
    unmount();
    const active = renderBar({ transformMode: true });
    expect(screen.getByRole("button", { name: "裁切/重构图" }).getAttribute("aria-pressed")).toBe("true");
    active.unmount();
    renderBar({ transformLocked: true });
    const locked = screen.getByRole("button", { name: "裁切/重构图" });
    expect(locked).toBeDisabled();
    expect(locked.getAttribute("title")).toContain("导出不受影响");
  });

  it("全屏按钮走全屏回调", () => {
    const { handlers } = renderBar();
    fireEvent.click(screen.getByRole("button", { name: "全屏" }));
    expect(handlers.onToggleFullscreen).toHaveBeenCalledTimes(1);
  });

  it("没有素材时传输按钮禁用，但时间码仍常驻", () => {
    renderBar({ hasMedia: false });
    expect(screen.getByRole("button", { name: "播放" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "全屏" })).toBeDisabled();
    expect(screen.getByText(/00:00:12/)).toBeInTheDocument();
  });

  it("响应式折叠钩子：播放/全屏不折叠，次要按钮带折叠标记并出现在「更多」里", () => {
    renderBar();
    expect(screen.getByRole("button", { name: "播放" }).className).not.toContain("player-foldable");
    expect(screen.getByRole("button", { name: "全屏" }).className).not.toContain("player-foldable");
    for (const name of ["上一帧", "下一帧", "后退五秒", "前进五秒", "原画", "裁切/重构图"]) {
      expect(screen.getByRole("button", { name }).className).toContain("player-foldable");
    }
    // 「更多」按钮只在窄窗由 CSS 显示，但必须存在于 DOM 且包含全部折叠项。
    const more = screen.getByRole("button", { name: "更多" });
    expect(more.closest(".player-collapsed-only")).not.toBeNull();
    fireEvent.click(more);
    const menu = screen.getByRole("menu", { name: "更多操作" });
    expect(menu.textContent).toContain("上一帧");
    expect(menu.textContent).toContain("原画");
    expect(menu.textContent).toContain("裁切/重构图");
    expect(menu.textContent).toContain(VIEWER_ASPECT_TOOLTIP);
  });

  it("更多菜单里的次要操作与平铺按钮同源回调", () => {
    const { handlers } = renderBar();
    fireEvent.click(screen.getByRole("button", { name: "更多" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "上一帧" }));
    expect(handlers.onSeekFrame).toHaveBeenLastCalledWith(-1);
    fireEvent.click(screen.getByRole("button", { name: "更多" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "原画" }));
    expect(handlers.onOriginalRatio).toHaveBeenCalledTimes(1);
  });
});
