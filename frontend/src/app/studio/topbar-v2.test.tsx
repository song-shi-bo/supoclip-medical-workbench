/**
 * 精简顶栏（topbar-v2）回归契约。
 *
 * 改造前顶栏有两行内容：左侧「文件/编辑/视图/工具/帮助」菜单 + 折叠助手图标，
 * 右侧「按修订规则重新生成」+「规则中心」按钮 + 「当前使用：医疗短视频剪辑规则 vX」
 * 横幅（含「检测到新规则版本…」提示）+ 保存状态 + 开始自动剪辑 + 导出成片 + 设置。
 * 现在顶栏只保留核心操作：
 *   左：项目管理入口（小房子图标，2026-10-19 替代原「打开菜单」占位）+ 「AI 视频剪辑工作台」；
 *   右：已保存 + 开始自动剪辑（唯一主操作）+ 导出成片（次主操作）+ 设置图标。
 * 规则版本信息不再出现在顶栏（改由成片卡片、剪辑助手执行结果、规则快照/任务历史面板呈现）。
 *
 * 这里只锁 DOM 与 class 契约（元素不存在、可见控件清单、分隔线不残留）；
 * 顶栏宽度/不换行/几何断言在真实浏览器验收里给出。
 */
import { describe, expect, it, vi, beforeAll, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "@/test/setup";
// 页面直接 import 了 studio.css；jsdom 用例不需要真实样式，而该文件走 Tailwind v4
// 的 PostCSS 配置，交给 Vite 处理会在加载阶段直接失败。这里把它替换成空模块。
vi.mock("./studio.css", () => ({}));
import StudioPage from "./page";

// 特意让后端**返回**规则版本与「有新版本」提示：顶栏仍不得出现任何规则文本，
// 否则用 404 兜底会让「没有横幅」变成假通过。
beforeEach(() => {
  server.use(
    http.get("*/studio/workflow", () => HttpResponse.json({
      banner: { current_version: "2026-10-12.3", label: "当前使用：医疗短视频剪辑规则 v2026-10-12.3", update_hint: "检测到新规则版本，下一次自动剪辑将使用 v2026-10-12.4" },
      summary: { workflow_version: "2026-10-12.3", validation_ok: true, skills: [] },
    })),
    http.all("*", () => HttpResponse.json({}, { status: 404 })),
  );
});

beforeAll(() => {
  if (!("createObjectURL" in URL)) {
    Object.defineProperty(URL, "createObjectURL", { writable: true, value: vi.fn(() => "blob:studio-test") });
  }
  if (!("revokeObjectURL" in URL)) {
    Object.defineProperty(URL, "revokeObjectURL", { writable: true, value: vi.fn() });
  }
});

const topbar = (container: HTMLElement) => container.querySelector("header.editor-topbar") as HTMLElement;

const visibleControls = (bar: HTMLElement) => [...bar.querySelectorAll("button, span")].map((el) => ({
  tag: el.tagName,
  cls: typeof el.className === "string" ? el.className : "",
  label: el.getAttribute("aria-label") || "",
  text: (el.textContent || "").trim(),
}));

describe("精简顶栏（topbar-v2）", () => {
  it("删掉整组菜单：DOM 里没有 nav，顶栏文本不含 文件/编辑/视图/工具/帮助", () => {
    const { container } = render(<StudioPage />);
    const bar = topbar(container);
    expect(bar).not.toBeNull();
    expect(bar.querySelectorAll("nav").length).toBe(0);
    const text = bar.textContent || "";
    ["文件", "编辑", "视图", "工具", "帮助"].forEach((label) => expect(text).not.toContain(label));
  });

  it("删掉一切规则相关顶部提示：无规则中心按钮/入口，无版本横幅与新版本提示", () => {
    const { container } = render(<StudioPage />);
    const bar = topbar(container);
    expect(bar.querySelectorAll(".workflow-open").length).toBe(0);
    expect(bar.querySelectorAll(".workflow-banner").length).toBe(0);
    const text = bar.textContent || "";
    expect(text).not.toContain("规则中心");
    expect(text).not.toContain("2026-10-12.3");       // 版本横幅（后端已返回，仍不得出现在顶栏）
    expect(text).not.toContain("检测到新规则版本");
    // 「按修订规则重新生成」与折叠助手图标一并移出顶栏。
    expect(bar.querySelectorAll(".rerender-button").length).toBe(0);
    expect(bar.querySelectorAll(".copilot-toggle-on, .copilot-toggle-off").length).toBe(0);
  });

  it("顶栏只剩核心操作：项目入口（小房子）+ 标题（左）/ 已保存 + 开始自动剪辑 + 导出成片 + 设置（右）", () => {
    const { container } = render(<StudioPage />);
    const bar = topbar(container);
    // 顶栏结构：只有三个直接子节点，没有空占位容器或残留分隔线（<i>）。
    expect([...bar.children].map((el) => el.tagName)).toEqual(["BUTTON", "STRONG", "DIV"]);
    expect(bar.querySelector("i")).toBeNull();
    expect(bar.querySelectorAll(".top-actions > i").length).toBe(0);
    expect(visibleControls(bar)).toEqual([
      { tag: "BUTTON", cls: "top-icon", label: "项目管理", text: "" },
      { tag: "SPAN", cls: "save-state", label: "", text: "已保存" },
      { tag: "BUTTON", cls: "auto-edit-button", label: "请先完成转写与纠错。", text: "开始自动剪辑" },
      { tag: "BUTTON", cls: "export-button", label: "导出成片", text: "导出成片" },
      { tag: "BUTTON", cls: "top-icon", label: "设置", text: "" },
    ]);
    expect(bar.querySelector("strong")?.textContent).toBe("AI 视频剪辑工作台");
    // 小房子图标 + 提示文字（2026-10-19：原「打开菜单」汉堡图标改为项目管理入口）。
    const entry = bar.querySelector('button[aria-label="项目管理"]') as HTMLButtonElement;
    expect(entry).not.toBeNull();
    expect(entry.getAttribute("title")).toBe("项目管理");
    expect(entry.querySelector("svg")).not.toBeNull();
  });

  it("项目管理入口：点击在页内打开弹层（不跳转），Esc 关闭后弹层消失", async () => {
    const { container } = render(<StudioPage />);
    const bar = topbar(container);
    // 未打开时：没有项目管理弹层，也不发生路由跳转（URL 不变）。
    expect(container.querySelector(".pm-overlay")).toBeNull();
    fireEvent.click(bar.querySelector('button[aria-label="项目管理"]') as HTMLButtonElement);
    const dialog = await screen.findByRole("dialog", { name: "项目管理" });
    expect(dialog).not.toBeNull();
    expect(String(window.location.href)).not.toMatch(/project/);
    // 弹层覆盖在工作台之上：工作台顶栏仍挂在 DOM 里（不卸载、不刷新）。
    expect(topbar(container)).not.toBeNull();
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(container.querySelector(".pm-overlay")).toBeNull());
  });
});
