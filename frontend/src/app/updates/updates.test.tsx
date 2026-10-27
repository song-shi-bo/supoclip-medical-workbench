/**
 * 更新日志页面接线回归契约。
 *
 * 锁定的行为：user 视图默认只渲染公开+已完成条目（不发内部请求）；
 * 切换 dev 视图把 view=dev 写进 URL 并拉取内部条目；highlight 卡片带
 * 数据标记；搜索词写入 URL 并驱动过滤。next/navigation 被模拟成
 * 「replace 更新 currentParams」，测试里手动 rerender 模拟路由驱动的重渲染
 * （与 App Router 中 router.replace 更新 useSearchParams 的真实行为一致）。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "@/test/setup";
import type { UpdateEntry } from "@/lib/updates";

let currentParams = new URLSearchParams();
const replaceMock = vi.fn((url: string) => {
  const idx = url.indexOf("?");
  currentParams = new URLSearchParams(idx >= 0 ? url.slice(idx + 1) : "");
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: vi.fn() }),
  usePathname: () => "/updates",
  useSearchParams: () => new URLSearchParams(currentParams.toString()),
}));

import { UpdatesClient } from "./updates-client";

const publicEntries: UpdateEntry[] = [
  {
    id: "pub-cover",
    date: "2026-10-27",
    type: "feature",
    status: "done",
    title: "封面生成器上线",
    summary: "三套模板即时预览",
    modules: ["cover"],
  },
  {
    id: "pub-timeline",
    date: "2026-10-26",
    type: "feature",
    status: "done",
    title: "时间轴多轨编辑",
    summary: "多轨拖拽与吸附",
    modules: ["workbench"],
    highlight: true,
  },
  {
    id: "pub-wip",
    date: "2026-10-25",
    type: "improvement",
    status: "wip",
    title: "进行中的公开条目",
    summary: "用户视图不可见",
    modules: ["workbench"],
  },
];

const internalEntries: UpdateEntry[] = [
  {
    id: "int-jobs",
    date: "2026-10-27",
    type: "improvement",
    status: "done",
    title: "任务管线拆分",
    summary: "jobs 与 worker 解耦",
    modules: ["export"],
    techDetails: "worker 独立进程，队列表状态机交接。",
  },
  {
    id: "int-exp",
    date: "2026-10-27",
    type: "experiment",
    status: "experimental",
    title: "字幕对齐实验",
    summary: "效果不稳定",
    modules: ["subtitle"],
  },
];

function renderUpdates() {
  return render(<UpdatesClient publicEntries={publicEntries} />);
}

beforeEach(() => {
  currentParams = new URLSearchParams();
  replaceMock.mockClear();
});

describe("UpdatesClient · 用户更新视图", () => {
  it("默认渲染公开且已完成的条目；不发起内部请求", async () => {
    renderUpdates();
    expect(screen.getByText("封面生成器上线")).toBeInTheDocument();
    expect(screen.getByText("时间轴多轨编辑")).toBeInTheDocument();
    expect(screen.queryByText("进行中的公开条目")).not.toBeInTheDocument();
    expect(screen.queryByText("任务管线拆分")).not.toBeInTheDocument();
    // 概览区「最新变化」取最新公开条目
    expect(screen.getByText(/最新 · 2026-10-27/)).toBeInTheDocument();
    // 订阅入口
    expect(screen.getByRole("link", { name: /RSS 订阅/ })).toHaveAttribute("href", "/updates/rss.xml");
    // user 视图不 fetch 内部接口（msw error 模式下未处理请求会直接报错）
    await waitFor(() => expect(replaceMock).not.toHaveBeenCalled());
  });

  it("highlight 条目带 data-highlight 标记（工作台视觉强化的钩子）", () => {
    renderUpdates();
    const highlighted = document.querySelector('article[data-update-id="pub-timeline"][data-highlight="true"]');
    expect(highlighted).not.toBeNull();
  });
});

describe("UpdatesClient · 开发日志视图", () => {
  it("点击切换把 view=dev 写进 URL，并合并内部条目", async () => {
    server.use(
      http.get("*/api/updates/internal", () => HttpResponse.json({ entries: internalEntries })),
    );
    const { rerender } = renderUpdates();

    fireEvent.click(screen.getByRole("tab", { name: "开发日志" }));
    expect(replaceMock).toHaveBeenCalledWith("/updates?view=dev", expect.anything());
    // 模拟路由驱动重渲染（mock 已更新 currentParams）
    rerender(<UpdatesClient publicEntries={publicEntries} />);

    expect(await screen.findByText("任务管线拆分")).toBeInTheDocument();
    expect(screen.getByText("字幕对齐实验")).toBeInTheDocument();
    // 内部条目以 data-update-id 定位（「内部」字样与可见性筛选 chip 重名）
    expect(document.querySelector('article[data-update-id="int-jobs"]')).not.toBeNull();
    // dev 视图渲染技术细节折叠区（summary 文案）
    expect(screen.getByText("技术细节")).toBeInTheDocument();
  });

  it("未登录（401）时降级为公开条目并给出提示", async () => {
    server.use(
      http.get("*/api/updates/internal", () => HttpResponse.json({ error: "unauthorized" }, { status: 401 })),
    );
    currentParams = new URLSearchParams("view=dev");
    renderUpdates();

    expect(await screen.findByText(/登录后可查看内部条目/)).toBeInTheDocument();
    expect(screen.getByText("封面生成器上线")).toBeInTheDocument();
    expect(screen.queryByText("任务管线拆分")).not.toBeInTheDocument();
  });
});

describe("UpdatesClient · 筛选与 URL 状态", () => {
  it("搜索词写入 URL 参数并驱动过滤", () => {
    const { rerender } = renderUpdates();
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索更新" }), {
      target: { value: "时间轴" },
    });
    expect(replaceMock).toHaveBeenCalledWith(expect.stringContaining("q="), expect.anything());
    rerender(<UpdatesClient publicEntries={publicEntries} />); // 模拟路由重渲染

    expect(screen.getByText("时间轴多轨编辑")).toBeInTheDocument();
    expect(screen.queryByText("封面生成器上线")).not.toBeInTheDocument();
    expect(screen.queryByText("进行中的公开条目")).not.toBeInTheDocument();
  });

  it("点击模块筛选把 module 写进 URL（工作台快捷入口）", () => {
    renderUpdates();
    fireEvent.click(screen.getByRole("button", { name: "⚡ 工作台" }));
    expect(replaceMock).toHaveBeenCalledWith("/updates?module=workbench", expect.anything());
  });

  it("空结果渲染占位文案", () => {
    currentParams = new URLSearchParams("q=不存在的关键词");
    renderUpdates();
    expect(screen.getByText("没有符合条件的更新")).toBeInTheDocument();
  });
});
