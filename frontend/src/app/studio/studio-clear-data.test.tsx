/**
 * 交互用例：清空当前项目的任务数据（显式按钮 + 二次确认）。
 *
 * 这条链路刻意**不是**「刷新即删」——台账是落盘持久数据，只有用户在确认卡上点
 * 「确认清空」才会真的删。因此本用例逐项钉住：
 *   ① 顶栏按钮在有卡片时出现、无卡片时不出现；
 *   ② 点按钮只**预览**（GET /data），不发 POST；
 *   ③ 取消不发 POST；
 *   ④ 确认才 POST /data/clear，且带 confirm=true 与 expected_card_count（防误删闸门）；
 *   ⑤ 清空成功后本地卡片被清掉（否则 renderedClips 里的旧卡会继续显示）；
 *   ⑥ 409（卡片数已变）时不本地清空、给出可读提示。
 */
import { describe, expect, it, vi, beforeAll, beforeEach } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "@/test/setup";

vi.mock("./studio.css", () => ({}));
import StudioPage from "./page";

const PROJECT_ID = "timeline_cleardata01";
const TASK_ID = "autoedit_0000000000aa";
const CLIP_ID = "clip-clear-01";
// 2026-10-16：会话指针已废除——测试用 ?source_id= URL 参数显式打开历史项目。

const card = {
  clip_id: CLIP_ID,
  name: "剪辑成片 01",
  title: "血压管理·医疗科普",
  topic: "血压管理",
  keywords: [],
  duration: 30.5,
  source_start: 0,
  source_end: 30.5,
  task_id: TASK_ID,
  render_task_id: TASK_ID,
  render_revision: 1,
  status: "ready",
  video_path: `output/${TASK_ID}/${CLIP_ID}/clip_final.mp4`,
  cover_path: null,
  cover: null,
};

const preview = {
  project_id: PROJECT_ID,
  card_count: 1,
  clip_ids: [CLIP_ID],
  task_ids: [TASK_ID],
  applied_task_count: 1,
  artifact_count: 4,
  reclaimable_bytes: 8 * 1024 * 1024,
  active_task_ids: [],
  keep: { source_video: `media/${TASK_ID}/source.mp4`, protected_media_dirs: [TASK_ID] },
};

function mockTimelineAndCards(cards: unknown[] = [card]) {
  server.use(
    http.get("*/studio/timeline/:projectId", () => HttpResponse.json({
      id: PROJECT_ID,
      timeline: { ranges: [{ id: CLIP_ID, source_start: 0, source_end: 30.5 }] },
      transcript: [],
    })),
    http.get("*/studio/projects/:projectId/clips", () => HttpResponse.json({
      project_id: PROJECT_ID, card_count: cards.length, cards,
    })),
  );
}

beforeAll(() => {
  if (!("createObjectURL" in URL)) {
    Object.defineProperty(URL, "createObjectURL", { writable: true, value: vi.fn(() => "blob:studio-test") });
  }
  if (!("revokeObjectURL" in URL)) {
    Object.defineProperty(URL, "revokeObjectURL", { writable: true, value: vi.fn() });
  }
  Object.defineProperty(HTMLMediaElement.prototype, "load", { configurable: true, value: vi.fn() });
  Object.defineProperty(HTMLMediaElement.prototype, "play", { configurable: true, value: vi.fn(() => Promise.resolve()) });
  Object.defineProperty(HTMLMediaElement.prototype, "pause", { configurable: true, value: vi.fn() });
});

beforeEach(() => {
  window.localStorage.clear();
  server.use(http.all("*", () => HttpResponse.json({}, { status: 404 })));
});

async function renderStudio(container: HTMLElement) {
  const clearButton = await waitFor(() => {
    const found = container.querySelector('[aria-label="清空任务数据"]');
    if (!found) throw new Error("清空任务数据按钮尚未渲染");
    return found as HTMLElement;
  });
  return clearButton;
}

describe("清空项目任务数据（显式按钮 + 二次确认）", () => {
  it("点按钮只弹确认卡、不发删除请求；取消后不发任何 POST", async () => {
    window.history.pushState({}, "", `/?source_id=${PROJECT_ID}`);
    let posted = 0;
    mockTimelineAndCards();
    server.use(
      http.get("*/studio/projects/:projectId/data", () => HttpResponse.json(preview)),
      http.post("*/studio/projects/:projectId/data/clear", () => {
        posted += 1;
        return HttpResponse.json({});
      }),
    );

    const { container } = render(<StudioPage />);
    const button = await renderStudio(container);
    fireEvent.click(button);

    // 确认卡把「删什么 / 留什么」写清楚，且此时一个删除请求都还没发。
    const dialog = await waitFor(() => {
      const found = container.querySelector('[aria-label="确认清空任务数据"]');
      if (!found) throw new Error("确认卡未出现");
      return found as HTMLElement;
    });
    expect(dialog.textContent).toContain("1");
    expect(dialog.textContent).toContain("源片与已完成的转写会保留");
    expect(dialog.textContent).toContain("删除不可撤销");
    expect(posted).toBe(0);

    fireEvent.click(dialog.querySelector(".clear-confirm-actions button") as Element);
    await waitFor(() => expect(container.querySelector('[aria-label="确认清空任务数据"]')).toBeNull());
    expect(posted).toBe(0);
  });

  it("确认后按 expected_card_count 提交，并清空本地卡片", async () => {
    window.history.pushState({}, "", `/?source_id=${PROJECT_ID}`);
    let body: Record<string, unknown> | null = null;
    mockTimelineAndCards();
    server.use(
      http.get("*/studio/projects/:projectId/data", () => HttpResponse.json(preview)),
      http.post("*/studio/projects/:projectId/data/clear", async ({ request }) => {
        body = await request.json() as Record<string, unknown>;
        return HttpResponse.json({
          project_id: PROJECT_ID, cleared_card_count: 1, cleared_task_count: 1,
          freed_bytes: 8 * 1024 * 1024, kept_source_video: `media/${TASK_ID}/source.mp4`,
        });
      }),
    );

    const { container } = render(<StudioPage />);
    fireEvent.click(await renderStudio(container));
    const dialog = await waitFor(() => {
      const found = container.querySelector('[aria-label="确认清空任务数据"]');
      if (!found) throw new Error("确认卡未出现");
      return found as HTMLElement;
    });
    fireEvent.click(dialog.querySelector(".clear-confirm-actions button.danger") as Element);

    await waitFor(() => expect(body).not.toBeNull());
    expect(body).toMatchObject({ confirm: true, expected_card_count: 1 });
    // 卡片被真正清掉，而不是只更新了台账而留下本地旧卡。
    await waitFor(() => expect(container.querySelectorAll(".asset-card.rendered-card").length).toBe(0));
    expect(container.querySelector('[aria-label="清空任务数据"]')).toBeNull();
    await waitFor(() => expect(container.textContent).toContain("已清空"));
  });

  it("卡片数已被服务端判定变化（409）时不本地清空，并给出可读提示", async () => {
    window.history.pushState({}, "", `/?source_id=${PROJECT_ID}`);
    mockTimelineAndCards();
    server.use(
      http.get("*/studio/projects/:projectId/data", () => HttpResponse.json(preview)),
      http.post("*/studio/projects/:projectId/data/clear", () => HttpResponse.json(
        { detail: "E_PURGE_CARD_COUNT_MISMATCH: 项目卡片数已变化，请刷新后重试" }, { status: 409 })),
    );

    const { container } = render(<StudioPage />);
    fireEvent.click(await renderStudio(container));
    const dialog = await waitFor(() => {
      const found = container.querySelector('[aria-label="确认清空任务数据"]');
      if (!found) throw new Error("确认卡未出现");
      return found as HTMLElement;
    });
    fireEvent.click(dialog.querySelector(".clear-confirm-actions button.danger") as Element);

    await waitFor(() => expect(container.textContent).toContain("清空未执行"));
    expect(container.querySelectorAll(".asset-card.rendered-card").length).toBe(1);
  });

  it("项目没有卡片时不显示清空入口", async () => {
    window.history.pushState({}, "", `/?source_id=${PROJECT_ID}`);
    mockTimelineAndCards([]);

    const { container } = render(<StudioPage />);
    await waitFor(() => expect(container.querySelector(".editor-topbar")).not.toBeNull());
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(container.querySelector('[aria-label="清空任务数据"]')).toBeNull();
  });
});
