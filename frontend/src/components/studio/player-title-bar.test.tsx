// 播放器标题栏测试（vitest + RTL）：固定文案、只有一个「更多」按钮、
// 标题不换行（CSS 契约）、菜单里全是观看操作。

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DEFAULT_PLAYER_TITLE, PlayerTitleBar } from "./player-title-bar";

const renderTitleBar = (overrides: Partial<Parameters<typeof PlayerTitleBar>[0]> = {}) => {
  const handlers = {
    onFitView: vi.fn(),
    onToggleGuides: vi.fn(),
    onToggleLoop: vi.fn(),
  };
  const props = { guides: false, loop: false, ...handlers, ...overrides };
  const rendered = render(<PlayerTitleBar {...props} />);
  return { props, handlers, unmount: rendered.unmount };
};

describe("PlayerTitleBar", () => {
  it("左侧是固定文案「播放器 - 时间线01」，右侧只有一个按钮", () => {
    renderTitleBar();
    const title = screen.getByText(DEFAULT_PLAYER_TITLE);
    expect(title.className).toContain("player-title");
    expect(title.getAttribute("title")).toBe(DEFAULT_PLAYER_TITLE);
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0].getAttribute("aria-label")).toBe("播放器菜单");
  });

  it("标题不换行（由 .player-title 的 nowrap + 省略号保证）", () => {
    renderTitleBar({ title: "播放器 - 一个非常非常长的时间线名称甲乙丙丁戊己庚辛" });
    const title = screen.getByText("播放器 - 一个非常非常长的时间线名称甲乙丙丁戊己庚辛");
    expect(title.className).toContain("player-title");
    // 完整文案仍是 title 属性，视觉上用省略号截断。
    expect(title.getAttribute("title")).toContain("甲乙丙丁");
  });

  it("菜单里的三项都是观看操作（适合画布 / 网格参考线 / 循环播放）", () => {
    const { handlers } = renderTitleBar({ guides: true, loop: true });
    fireEvent.click(screen.getByRole("button", { name: "播放器菜单" }));
    const menu = screen.getByRole("menu", { name: "播放器菜单" });
    expect(menu.textContent).toContain("不影响导出");
    expect(screen.getByRole("menuitem", { name: "适合画布" }).getAttribute("title")).toContain("不影响导出");
    fireEvent.click(screen.getByRole("menuitem", { name: "适合画布" }));
    expect(handlers.onFitView).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("menuitemcheckbox", { name: /网格参考线/ }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: /网格参考线/ }));
    expect(handlers.onToggleGuides).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: /循环播放/ }));
    expect(handlers.onToggleLoop).toHaveBeenCalledTimes(1);
  });

  it("点击外部关闭菜单", () => {
    renderTitleBar();
    fireEvent.click(screen.getByRole("button", { name: "播放器菜单" }));
    expect(screen.getByRole("menu", { name: "播放器菜单" })).toBeInTheDocument();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("menu", { name: "播放器菜单" })).not.toBeInTheDocument();
  });
});
