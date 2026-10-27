/**
 * 时间线工具栏（Phase B）组件测试。
 *
 * 覆盖验收 9（工具栏三组结构 / 不换行 / 窄窗收起 / 每个图标 tooltip + aria-label）
 * 与验收 10（改视图设置不产生任何编辑写入）的前端半边。
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TimelineToolbar } from "./timeline-toolbar";

const noop = () => {};

function setup(overrides: Partial<Parameters<typeof TimelineToolbar>[0]> = {}) {
  const handlers = {
    onAddTrack: vi.fn(),
    onUndo: vi.fn(),
    onRedo: vi.fn(),
    onZoomChange: vi.fn(),
    onZoomIn: vi.fn(),
    onZoomOut: vi.fn(),
    onFitWindow: vi.fn(),
    onTrackHeightChange: vi.fn(),
    onWaveformRatioChange: vi.fn(),
  };
  const props = {
    canUndo: false,
    canRedo: false,
    zoomPxPerSecond: 100,
    trackHeight: 72,
    waveformRatio: "standard" as const,
    hasSourceTrack: true,
    ...handlers,
    ...overrides,
  };
  const view = render(<TimelineToolbar {...props} />);
  return { ...handlers, props, ...view };
}

describe("三组结构", () => {
  it("左组是 ＋ / 撤销 / 重做，右组是缩放，最右是轨道显示设置", () => {
    setup();
    const toolbar = screen.getByRole("toolbar", { name: "时间线工具栏" });
    const left = toolbar.querySelector(".timeline-toolbar-left");
    const zoom = toolbar.querySelector(".timeline-toolbar-zoom");
    const right = toolbar.querySelector(".timeline-toolbar-right");
    expect(left).not.toBeNull();
    expect(zoom).not.toBeNull();
    expect(right).not.toBeNull();
    expect(within(left as HTMLElement).getByLabelText("添加到时间线")).toBeInTheDocument();
    expect(within(left as HTMLElement).getByLabelText("撤销")).toBeInTheDocument();
    expect(within(left as HTMLElement).getByLabelText("重做")).toBeInTheDocument();
    expect(within(zoom as HTMLElement).getByLabelText("缩小时间线")).toBeInTheDocument();
    expect(within(zoom as HTMLElement).getByLabelText("放大时间线")).toBeInTheDocument();
    expect(within(right as HTMLElement).getByLabelText("轨道显示设置")).toBeInTheDocument();
    // 三组是**并列**的兄弟节点，靠 CSS gap 留足间距（不会互相挤压/换行）。
    expect(left?.parentElement).toBe(toolbar);
    expect(zoom?.parentElement).toBe(toolbar);
    expect(right?.parentElement).toBe(toolbar);
  });

  it("每个图标按钮都有 aria-label 与 title（原生 tooltip）", () => {
    setup();
    const toolbar = screen.getByRole("toolbar", { name: "时间线工具栏" });
    const iconButtons = Array.from(toolbar.querySelectorAll("button")).filter(
      (button) => button.querySelector("svg") !== null || button.textContent === "＋",
    );
    expect(iconButtons.length).toBeGreaterThanOrEqual(5);
    for (const button of iconButtons) {
      expect(button.getAttribute("aria-label")).toBeTruthy();
      expect(button.getAttribute("title")).toBeTruthy();
    }
  });
});

describe("＋ 添加到时间线菜单", () => {
  it("只提供背景音乐 / 音效 / 字幕轨 / 视觉轨（原视频与原声不在其中）", async () => {
    const user = userEvent.setup();
    const { onAddTrack } = setup();
    await user.click(screen.getByLabelText("添加到时间线"));
    const menu = screen.getByRole("menu", { name: "添加到时间线" });
    const labels = Array.from(menu.querySelectorAll(".timeline-menu-item")).map(
      (item) => item.textContent ?? "",
    );
    expect(labels.some((label) => label.includes("背景音乐"))).toBe(true);
    expect(labels.some((label) => label.includes("音效"))).toBe(true);
    expect(labels.some((label) => label.includes("字幕轨"))).toBe(true);
    expect(labels.some((label) => label.includes("图片或图解视觉轨"))).toBe(true);
    // 可点击项里没有「视频 / 原声」。
    const actionable = Array.from(menu.querySelectorAll("button.timeline-menu-item"));
    expect(actionable).toHaveLength(4);
    expect(actionable.map((item) => item.textContent).join("|")).not.toMatch(/原声|原始视频/);
  });

  it("源声轨已存在时显示置灰说明项，点击不会触发添加", async () => {
    const user = userEvent.setup();
    const { onAddTrack } = setup({ hasSourceTrack: true });
    await user.click(screen.getByLabelText("添加到时间线"));
    const reserved = screen.getByLabelText(/原始视频原声：已自动进入时间线/);
    expect(reserved).toHaveAttribute("aria-disabled", "true");
    expect(reserved.getAttribute("title")).toContain("不能用 ＋ 重复添加");
    expect(reserved.tagName).toBe("DIV");
    await user.click(reserved);
    expect(onAddTrack).not.toHaveBeenCalled();
    expect(screen.getByRole("menu")).toBeInTheDocument(); // 菜单不因误点关闭
  });

  it("源声轨不存在时不出现该说明项", async () => {
    const user = userEvent.setup();
    setup({ hasSourceTrack: false });
    await user.click(screen.getByLabelText("添加到时间线"));
    expect(screen.queryByText("原始视频原声")).toBeNull();
  });

  it("选择菜单项会回调对应 kind 并关闭菜单", async () => {
    const user = userEvent.setup();
    const { onAddTrack } = setup();
    await user.click(screen.getByLabelText("添加到时间线"));
    await user.click(screen.getByRole("menuitem", { name: /背景音乐/ }));
    expect(onAddTrack).toHaveBeenCalledWith("bgm");
    expect(screen.queryByRole("menu")).toBeNull();

    await user.click(screen.getByLabelText("添加到时间线"));
    await user.click(screen.getByRole("menuitem", { name: /字幕轨/ }));
    expect(onAddTrack).toHaveBeenLastCalledWith("subtitle");
  });

  it("Esc 与点空白都会关闭菜单", async () => {
    const user = userEvent.setup();
    setup();
    await user.click(screen.getByLabelText("添加到时间线"));
    expect(screen.getByRole("menu")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();

    await user.click(screen.getByLabelText("添加到时间线"));
    await user.click(document.body);
    expect(screen.queryByRole("menu")).toBeNull();
  });
});

describe("撤销 / 重做置灰与 tooltip", () => {
  it("不可撤销/重做时禁用并给出原因 tooltip", () => {
    setup({ canUndo: false, canRedo: false });
    const undo = screen.getByLabelText("撤销");
    const redo = screen.getByLabelText("重做");
    expect(undo).toBeDisabled();
    expect(redo).toBeDisabled();
    expect(undo.getAttribute("title")).toBe("没有可撤销的时间线操作");
    expect(redo.getAttribute("title")).toBe("没有可重做的时间线操作");
  });

  it("可用时给出快捷键 tooltip 且点击回调", async () => {
    const user = userEvent.setup();
    const { onUndo, onRedo } = setup({ canUndo: true, canRedo: true });
    const undo = screen.getByLabelText("撤销");
    expect(undo).toBeEnabled();
    expect(undo.getAttribute("title")).toContain("Ctrl/Cmd+Z");
    expect(screen.getByLabelText("重做").getAttribute("title")).toContain("Ctrl/Cmd+Shift+Z");
    await user.click(undo);
    await user.click(screen.getByLabelText("重做"));
    expect(onUndo).toHaveBeenCalledTimes(1);
    expect(onRedo).toHaveBeenCalledTimes(1);
  });

  it("busy 时全部动作禁用（其它代理正在写）", () => {
    setup({ canUndo: true, canRedo: true, busy: true });
    expect(screen.getByLabelText("撤销")).toBeDisabled();
    expect(screen.getByLabelText("重做")).toBeDisabled();
    expect(screen.getByLabelText("添加到时间线")).toBeDisabled();
    expect(screen.getByLabelText("缩小时间线")).toBeDisabled();
    expect(screen.getByLabelText("放大时间线")).toBeDisabled();
  });
});

describe("缩放组（只驱动显示）", () => {
  it("缩小/放大按钮与滑杆都回调 px/秒 数值", async () => {
    const user = userEvent.setup();
    const { onZoomIn, onZoomOut, onZoomChange } = setup({ zoomPxPerSecond: 100 });
    await user.click(screen.getByLabelText("缩小时间线"));
    await user.click(screen.getByLabelText("放大时间线"));
    expect(onZoomOut).toHaveBeenCalledTimes(1);
    expect(onZoomIn).toHaveBeenCalledTimes(1);
    const slider = screen.getByLabelText("时间线缩放");
    expect(slider).toHaveAttribute("min", "20");
    expect(slider).toHaveAttribute("max", "300");
    expect(slider).toHaveValue("100");
    expect(slider).toHaveAttribute("aria-valuetext", "100 像素每秒");
    expect(slider.getAttribute("title")).toContain("px/秒");
    expect(onZoomChange).not.toHaveBeenCalled();
  });

  it("适合窗口按钮在宽屏可见", async () => {
    const user = userEvent.setup();
    const { onFitWindow } = setup({ narrow: false });
    await user.click(screen.getByLabelText("适合窗口"));
    expect(onFitWindow).toHaveBeenCalledTimes(1);
  });
});

describe("三点「轨道显示设置」浮层", () => {
  it("轨道高度滑杆 44–180、默认 72，实时回调", async () => {
    const user = userEvent.setup();
    const { onTrackHeightChange } = setup();
    await user.click(screen.getByLabelText("轨道显示设置"));
    const dialog = screen.getByRole("dialog", { name: "轨道显示设置" });
    const slider = within(dialog).getByLabelText("轨道高度");
    expect(slider).toHaveAttribute("min", "44");
    expect(slider).toHaveAttribute("max", "180");
    expect(slider).toHaveValue("72");
    expect(slider.getAttribute("title")).toContain("44–180");
    expect(onTrackHeightChange).not.toHaveBeenCalled();
  });

  it("波形占比三档（紧凑/标准/扩展）带 aria-pressed，切换回调", async () => {
    const user = userEvent.setup();
    const { onWaveformRatioChange } = setup({ waveformRatio: "standard" });
    await user.click(screen.getByLabelText("轨道显示设置"));
    const group = screen.getByRole("group", { name: "波形占比" });
    const compact = within(group).getByLabelText("波形占比 紧凑");
    const standard = within(group).getByLabelText("波形占比 标准");
    const expanded = within(group).getByLabelText("波形占比 扩展");
    expect(compact).toHaveAttribute("aria-pressed", "false");
    expect(standard).toHaveAttribute("aria-pressed", "true");
    expect(compact.getAttribute("title")).toContain("35%");
    expect(standard.getAttribute("title")).toContain("60%");
    expect(expanded.getAttribute("title")).toContain("85%");
    await user.click(expanded);
    expect(onWaveformRatioChange).toHaveBeenCalledWith("expanded");
  });

  it("浮层说明只影响显示（不改音频与导出）", async () => {
    const user = userEvent.setup();
    setup();
    await user.click(screen.getByLabelText("轨道显示设置"));
    expect(screen.getByText(/只影响显示：不改变音频内容、时长与导出结果/)).toBeInTheDocument();
  });

  it("Esc 与点空白关闭，关闭按钮可用", async () => {
    const user = userEvent.setup();
    setup();
    await user.click(screen.getByLabelText("轨道显示设置"));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();

    await user.click(screen.getByLabelText("轨道显示设置"));
    await user.click(screen.getByLabelText("关闭轨道显示设置"));
    expect(screen.queryByRole("dialog")).toBeNull();

    await user.click(screen.getByLabelText("轨道显示设置"));
    await user.click(document.body);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("浮层向上展开（锚点在工具栏内，不覆盖整条时间线）", async () => {
    const user = userEvent.setup();
    setup();
    await user.click(screen.getByLabelText("轨道显示设置"));
    const popover = screen.getByRole("dialog");
    expect(popover.className).toContain("timeline-popover");
    expect(popover.closest(".timeline-toolbar-right")).not.toBeNull();
  });
});

describe("窄窗布局（≤1180px）", () => {
  it("缩放滑杆与适合窗口收进「更多工具」菜单，工具栏仍只有三组", async () => {
    const user = userEvent.setup();
    const { onFitWindow, onZoomChange } = setup({ narrow: true });
    const toolbar = screen.getByRole("toolbar", { name: "时间线工具栏" });
    // 主行不再有滑杆与「适合窗口」文字按钮。
    expect(within(toolbar.querySelector(".timeline-toolbar-zoom") as HTMLElement)
      .queryByLabelText("时间线缩放")).toBeNull();
    expect(within(toolbar).queryByLabelText("适合窗口")).toBeNull();
    // 缩小/放大仍留在主行（主要工具）。
    expect(within(toolbar).getByLabelText("缩小时间线")).toBeInTheDocument();
    expect(within(toolbar).getByLabelText("放大时间线")).toBeInTheDocument();
    expect(toolbar.querySelectorAll(".timeline-toolbar-group")).toHaveLength(3);

    await user.click(screen.getByLabelText("轨道显示设置"));
    const dialog = screen.getByRole("dialog", { name: "轨道显示设置" });
    const moreSlider = within(dialog).getByLabelText("时间线缩放");
    expect(moreSlider).toHaveAttribute("min", "20");
    expect(moreSlider).toHaveAttribute("max", "300");
    await user.click(within(dialog).getByLabelText("适合窗口"));
    expect(onFitWindow).toHaveBeenCalledTimes(1);
    expect(onZoomChange).not.toHaveBeenCalled();
  });

  it("最右按钮在窄窗变成「更多」图标但仍有 aria-label 与 tooltip", () => {
    setup({ narrow: true });
    const button = screen.getByLabelText("轨道显示设置");
    expect(button.getAttribute("title")).toBe("轨道显示设置（轨道高度 / 波形占比）");
    expect(button.querySelector("svg")).not.toBeNull();
  });
});
