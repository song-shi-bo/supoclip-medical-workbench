/**
 * 回归：台账卡片 `cover` 为 `null` 时不得整页白屏。
 *
 * 背景（代理实测复现）：`page.tsx` 里对台账卡片 `cover` 的若干处访问没有可选链——
 * 只要项目台账里有一条 `cover: null` 的成片（旧任务、封面被 GC、或 cover 字段缺失），
 * 选中该成片后主渲染路径就会 `Cannot read properties of null (reading 'cover_asset_id')`
 * 抛在 render 阶段，React 直接卸载整棵树 → 用户看到白屏。
 *
 * 本用例用**真实页面**（不是重写的假组件）走完整链路：
 *   ?source_id= 显式打开项目 → GET /studio/projects/{id}/clips
 *   → 左侧渲染 cover=null 的成片卡片 → 点击选中 → 断言不抛错且卡片仍可用。
 *
 * 2026-10-21 切片选择阶段：缩略图改为切片视频真实第一帧（clipFirstFrameUrl），
 * 与 cover 解耦——cover=null 的卡也必须有第一帧 img；提取失败（error 事件）降级为
 * 占位图标，绝不用生成封面冒充。点击卡片额外 PUT active-clip（此处在 msw 里放行 200）。
 *
 * `studio.css` 走 Tailwind v4 PostCSS，在 jsdom 下由 Vite 处理会直接失败（asset-panel
 * 用例同款处理），这里替换成空模块。
 */
import { describe, expect, it, vi, beforeAll, beforeEach } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "@/test/setup";

vi.mock("./studio.css", () => ({}));
import StudioPage from "./page";

const PROJECT_ID = "timeline_nullcover01";
const TASK_ID = "autoedit_0000000000ff";
const CLIP_ID = "clip-nullcover-01";
const FIRST_FRAME_URL = `/video-api/studio/projects/${PROJECT_ID}/clips/${CLIP_ID}/first-frame.jpg`;

/** 台账卡片：cover 显式为 null（本次回归的触发条件）。 */
const nullCoverCard = {
  clip_id: CLIP_ID,
  name: "剪辑成片 01",
  title: "血压管理·医疗科普",
  topic: "血压管理",
  keywords: [],
  duration: 12.5,
  source_start: 0,
  source_end: 12.5,
  task_id: TASK_ID,
  render_task_id: TASK_ID,
  render_revision: 1,
  status: "ready",
  video_path: `output/${TASK_ID}/${CLIP_ID}/clip_final.mp4`,
  cover_path: null,
  cover: null,
};

beforeAll(() => {
  if (!("createObjectURL" in URL)) {
    Object.defineProperty(URL, "createObjectURL", { writable: true, value: vi.fn(() => "blob:studio-test") });
  }
  if (!("revokeObjectURL" in URL)) {
    Object.defineProperty(URL, "revokeObjectURL", { writable: true, value: vi.fn() });
  }
  // 播放器与预览代理会用到的媒体 API：jsdom 不实现，给最小桩避免无关异常。
  Object.defineProperty(HTMLMediaElement.prototype, "load", { configurable: true, value: vi.fn() });
  Object.defineProperty(HTMLMediaElement.prototype, "play", { configurable: true, value: vi.fn(() => Promise.resolve()) });
  Object.defineProperty(HTMLMediaElement.prototype, "pause", { configurable: true, value: vi.fn() });
});

beforeEach(() => {
  window.localStorage.clear();
  // 切片选择持久化（2026-10-21 阶段）：点击卡片会 PUT active-clip，放行 200 避免回滚干扰。
  server.use(
    http.put("*/studio/projects/:projectId/active-clip", async ({ request }) => {
      const body = await request.json() as { clip_id?: string | null };
      return HttpResponse.json({ project_id: PROJECT_ID, active_clip_id: body.clip_id ?? null, changed: true });
    }),
    // 兜底：未显式注册的请求一律 404（msw 配置为 onUnhandledRequest: error）。
    http.all("*", () => HttpResponse.json({}, { status: 404 })),
  );
});

const mountWithNullCoverCard = () => {
  window.history.pushState({}, "", `/?source_id=${PROJECT_ID}`);
  server.use(
    http.get("*/studio/timeline/:projectId", () => HttpResponse.json({
      id: PROJECT_ID,
      timeline: { ranges: [{ id: CLIP_ID, source_start: 0, source_end: 12.5 }] },
      transcript: [],
    })),
    http.get("*/studio/projects/:projectId/clips", () => HttpResponse.json({
      project_id: PROJECT_ID, card_count: 1, cards: [nullCoverCard],
    })),
  );
  return render(<StudioPage />);
};

describe("台账卡片 cover=null 的可选链回归", () => {
  it("渲染并选中 cover 为 null 的成片时整页不抛错（此前白屏）", async () => {
    const { container } = mountWithNullCoverCard();

    // ① 台账卡片渲染出来（缩略图与 cover 解耦：cover=null 也给第一帧 img）
    const card = await waitFor(() => {
      const found = container.querySelector(".asset-card.rendered-card");
      if (!found) throw new Error("台账成片卡片尚未渲染");
      return found as HTMLElement;
    });
    expect(card.textContent).toContain("剪辑成片 01");
    const thumb = card.querySelector(".rendered-cover img") as HTMLImageElement | null;
    expect(thumb).not.toBeNull();
    expect(thumb!.getAttribute("src")).toBe(FIRST_FRAME_URL);

    // ② 选中它 → activeCard.cover === null 走进封面工具条/Inspector 接线（原先在此抛错）
    expect(() => fireEvent.click(card)).not.toThrow();
    await waitFor(() => expect(card.classList.contains("active")).toBe(true));

    // ③ 仍然可用：卡片还在、封面工具条给出了「无封面」而不是崩掉
    expect(container.querySelectorAll(".asset-card.rendered-card").length).toBe(1);
    expect(container.querySelector(".asset-card.rendered-card")?.classList.contains("active")).toBe(true);
    expect(container.textContent).not.toContain("Cannot read properties of null");
  });

  it("cover=null 时进入卡片菜单（版本徽标/撤销项分支）同样不抛错", async () => {
    const { container } = mountWithNullCoverCard();
    const card = await waitFor(() => {
      const found = container.querySelector(".asset-card.rendered-card");
      if (!found) throw new Error("台账成片卡片尚未渲染");
      return found as HTMLElement;
    });

    const menuToggle = card.querySelector(".rendered-menu-toggle");
    expect(menuToggle).not.toBeNull();
    expect(() => fireEvent.click(menuToggle as Element)).not.toThrow();
    // 菜单打开后逐项渲染（含 cover=null 分支的「查看历史 / 撤销」判定）
    await waitFor(() => expect(container.querySelector(".rendered-menu")).not.toBeNull());
    expect(container.querySelector(".rendered-menu")?.textContent).toContain("播放");
  });

  it("第一帧提取失败（img error）降级为占位图标，绝不用生成封面冒充", async () => {
    const { container } = mountWithNullCoverCard();
    const card = await waitFor(() => {
      const found = container.querySelector(".asset-card.rendered-card");
      if (!found) throw new Error("台账成片卡片尚未渲染");
      return found as HTMLElement;
    });
    const thumb = await waitFor(() => {
      const img = card.querySelector(".rendered-cover img") as HTMLImageElement | null;
      if (!img) throw new Error("第一帧缩略图尚未渲染");
      return img;
    });
    fireEvent.error(thumb);
    // error 后：img 撤下、回到 Film 占位（cover=null 也没有封面可回退）。
    await waitFor(() => expect(card.querySelector(".rendered-cover img")).toBeNull());
    expect(card.querySelector(".rendered-cover svg")).not.toBeNull();
  });
});
