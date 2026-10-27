/**
 * 左侧「项目素材」面板布局重构（left-assets-v2）的回归契约。
 *
 * 背景：面板原来有一列 120px 的竖向分类导航（全部/视频/音频/图片/字幕），其中
 * 「音频 0 / 图片 0 / 字幕 0」永远为空。重构后整列**真删除**，改由三个带计数的
 * 分区标题（原始素材 / 剪辑成片 / 封面与视觉素材）承载，原始素材卡变成铺满面板
 * 宽度的大卡片（16:9 缩略图 + 文件名 / 时长 / 状态 + 右上角删除）。
 *
 * 这里只锁 DOM 与 class 契约（分类按钮不存在、分区标题与计数、大卡片结构与删除），
 * 几何断言（卡片宽度增幅、16:9、列表自身滚动）在真实浏览器验收里给出。
 */
import { describe, expect, it, vi, beforeAll, beforeEach } from "vitest";
import { render, fireEvent, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "@/test/setup";
// 页面直接 import 了 studio.css；jsdom 用例不需要真实样式，而该文件走 Tailwind v4
// 的 PostCSS 配置，交给 Vite 处理会在加载阶段直接失败。这里把它替换成空模块。
vi.mock("./studio.css", () => ({}));
import StudioPage from "./page";

// 面板挂载时会拉规则中心与台账；统一用 404 兜底，避免 msw 的
// onUnhandledRequest: "error" 让渲染直接失败。setup.ts 的 afterEach 会
// resetHandlers，所以每个用例前都要重新注册。
beforeEach(() => {
  server.use(http.all("*", () => HttpResponse.json({}, { status: 404 })));
});

// jsdom 既没有 createObjectURL（导入视频时用于本地预览），也没有媒体元数据加载。
beforeAll(() => {
  if (!("createObjectURL" in URL)) {
    Object.defineProperty(URL, "createObjectURL", { writable: true, value: vi.fn(() => "blob:studio-test") });
  }
  if (!("revokeObjectURL" in URL)) {
    Object.defineProperty(URL, "revokeObjectURL", { writable: true, value: vi.fn() });
  }
});

const importSampleVideo = (container: HTMLElement) => {
  const input = container.querySelector('input[type="file"]');
  if (!input) throw new Error("找不到导入素材的 input[type=file]");
  const file = new File([new Uint8Array([0, 0, 0, 24, 102, 116, 121, 112])], "source.mp4", { type: "video/mp4" });
  fireEvent.change(input, { target: { files: [file] } });
  return file;
};

const sourceCard = (container: HTMLElement) => container.querySelector(".asset-card.source-card");

describe("左侧项目素材面板（left-assets-v2）", () => {
  it("不再渲染竖向分类导航，也不残留「音频 0 / 图片 0 / 字幕 0」空分类", () => {
    const { container } = render(<StudioPage />);
    expect(container.querySelectorAll(".asset-types").length).toBe(0);
    const panel = container.querySelector(".media-browser");
    expect(panel).not.toBeNull();
    const text = (panel as HTMLElement).textContent || "";
    expect(text).not.toContain("音频 0");
    expect(text).not.toContain("图片 0");
    expect(text).not.toContain("字幕 0");
    // 空态仍然给出导入入口，且原始素材分区标题带计数（0）。
    expect(within(panel as HTMLElement).getByText("原始素材（0）")).toBeTruthy();
  });

  it("素材面板是单列：.asset-body 只包住 .asset-list，没有 120px 分类栏占位", () => {
    const { container } = render(<StudioPage />);
    const body = container.querySelector(".asset-body");
    expect(body).not.toBeNull();
    expect(body?.children.length).toBe(1);
    expect(body?.children[0].classList.contains("asset-list")).toBe(true);
    expect(container.querySelectorAll(".asset-body > .asset-types").length).toBe(0);
  });

  it("左侧素材区只保留两个分区：原始素材 / 剪辑成片（不再有封面与视觉素材）", () => {
    const { container } = render(<StudioPage />);
    const labels = [...container.querySelectorAll(".asset-section-label")].map((node) => node.textContent);
    expect(labels).toEqual(["原始素材（0）", "剪辑成片（0）"]);
    // 封面不再作为左侧素材管理：分区标题、空状态、占位一律不得出现。
    expect(container.querySelector(".asset-section-label.visual")).toBeNull();
    expect(container.textContent).not.toContain("封面与视觉素材");
    expect(container.textContent).not.toContain("暂无封面或视觉素材");
    // 剪辑成片为空时仍给简短空状态（该空状态属于成片分区）。
    expect(container.querySelector(".asset-subsection-empty")?.textContent).toBe("自动剪辑完成后，成片会显示在这里。");
  });

  it("导入原片后渲染大卡片：16:9 缩略图 + 文件名 + 时长 + 真实状态 + 右上角删除按钮", () => {
    const { container } = render(<StudioPage />);
    expect(sourceCard(container)).toBeNull();

    importSampleVideo(container);

    const card = sourceCard(container);
    expect(card).not.toBeNull();
    // 点击选中契约：沿用既有的 role=button / tabIndex 与 .active 高亮。
    expect(card?.getAttribute("role")).toBe("button");
    expect(card?.getAttribute("tabindex")).toBe("0");
    expect(card?.classList.contains("active")).toBe(true);
    expect(card?.querySelector("video")).not.toBeNull();
    expect(card?.querySelector("strong")?.textContent).toBe("source.mp4");
    // 时长未就绪时沿用既有「读取媒体中…」，状态按现有状态变量推导（未转写 → 待转写）。
    expect(card?.querySelector(".asset-card-duration")?.textContent).toBe("读取媒体中…");
    expect(card?.querySelector(".asset-status")?.textContent).toBe("待转写");
    const remove = card?.querySelector('button[aria-label="移除素材"]');
    expect(remove).not.toBeNull();
    expect(card?.querySelector(".asset-card-body")?.contains(remove as Node)).toBe(false);

    // 计数随导入更新，成片分区仍为 0。
    expect(container.querySelector(".asset-section-label")?.textContent).toBe("原始素材（1）");
  });

  it("删除按钮移除素材并清空原片卡片，播放器回到空态", () => {
    const { container } = render(<StudioPage />);
    importSampleVideo(container);
    expect(sourceCard(container)).not.toBeNull();

    const remove = container.querySelector('.asset-card.source-card button[aria-label="移除素材"]');
    fireEvent.click(remove as Element);

    expect(sourceCard(container)).toBeNull();
    expect(container.querySelector(".asset-section-label")?.textContent).toBe("原始素材（0）");
    expect(container.querySelector(".asset-list .asset-empty")).not.toBeNull();
    expect(container.querySelector(".player-card")?.getAttribute("data-empty")).toBe("true");
  });

  it("Enter 键与点击都能触发原片卡片的选中回调（沿用 selectSourceMedia 语义）", () => {
    const { container } = render(<StudioPage />);
    importSampleVideo(container);
    const card = sourceCard(container) as HTMLElement;
    // 没有源工作区时 selectSourceMedia 会按既有语义早退：点击/回车都不应抛错或改结构。
    expect(() => fireEvent.click(card)).not.toThrow();
    expect(() => fireEvent.keyDown(card, { key: "Enter" })).not.toThrow();
    expect(sourceCard(container)).not.toBeNull();
    expect(sourceCard(container)?.classList.contains("active")).toBe(true);
  });
});
