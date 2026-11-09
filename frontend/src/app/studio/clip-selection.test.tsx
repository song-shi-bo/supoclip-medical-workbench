/**
 * 切片选择（2026-10-21 阶段）整页接线契约。
 *
 * 用**真实页面**走完整链路（?source_id= 打开项目 → GET 台账 → 素材卡），锁四条验收：
 *   ① 每张素材卡的缩略图 = 切片视频真实第一帧（first-frame.jpg），cover 存在也不用它冒充；
 *   ② 点击哪张就播哪张：PUT active-clip（唯一 active_clip_id）+ 播放器/精修转写面板/选中态一次切换；
 *      切片视图只保留精修文案面板，不再渲染下方转写分段列表（2026-11-09 用户指令）；
 *   ③ 切换失败（PUT 非 2xx）回滚保留原切片（播放器与转写跟着回到原切片）；
 *   ④ 刷新后恢复：台账载荷 active_clip_id 命中已有卡片时自动恢复，且不再回写 PUT；
 *      台账已不含该切片（悬空指针）时不恢复、不回写。
 *
 * 基建同 studio-null-cover.test.tsx：jsdom 不跑真实样式（studio.css mock 成空模块），
 * msw onUnhandledRequest=error，媒体 API 用最小桩。
 */
import { describe, expect, it, vi, beforeAll, beforeEach } from "vitest";
import { act, render, fireEvent, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "@/test/setup";

vi.mock("./studio.css", () => ({}));
import StudioPage from "./page";

const PROJECT_ID = "timeline_clipselect01";
const TASK_ID = "autoedit_0000000000aa";
const CLIP_A = "clip-select-aaaa";
const CLIP_B = "clip-select-bbbb";

const clipCard = (clipId: string, segmentText: string) => ({
  clip_id: clipId,
  name: clipId === CLIP_A ? "剪辑成片 01" : "剪辑成片 02",
  title: `标题-${clipId}`,
  topic: "血压科普",
  keywords: [],
  duration: clipId === CLIP_A ? 8 : 10,
  source_start: 0,
  source_end: clipId === CLIP_A ? 8 : 10,
  task_id: TASK_ID,
  render_task_id: TASK_ID,
  render_revision: 1,
  status: "ready",
  video_path: `output/${TASK_ID}/${clipId}/clip_final.mp4`,
  cover: null,
  task_history: [],
  segments: [{ start: 0, end: 2, text: segmentText }],
});

const CARDS = [clipCard(CLIP_A, "甲切片专属转写句子。"), clipCard(CLIP_B, "乙切片专属转写句子。")];

const firstFrameUrl = (clipId: string) => `/video-api/studio/projects/${PROJECT_ID}/clips/${clipId}/first-frame.jpg`;
const playerSrc = (clipId: string) =>
  `/video-api/studio/auto-edit/${TASK_ID}/artifact?path=${encodeURIComponent(`output/${TASK_ID}/${clipId}/clip_final.mp4`)}`;

const activeClipPuts: { clip_id: string | null }[] = [];
let activeClipStatus = 200;
let ledgerActiveClipId: string | null = null;
// 挂起中的 PUT（模拟慢请求）：armed 时下一次 PUT 不立即返回，由用例手动放行。
let hangPutArmed = false;
let hangPutResolver: ((response: Response) => void) | null = null;

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
  activeClipPuts.length = 0;
  activeClipStatus = 200;
  ledgerActiveClipId = null;
  hangPutArmed = false;
  hangPutResolver = null;
  window.localStorage.clear();
  window.history.pushState({}, "", `/?source_id=${PROJECT_ID}`);
  server.use(
    http.get("*/studio/timeline/:projectId", () => HttpResponse.json({
      id: PROJECT_ID, source: { name: "素材.mp4", duration: 30 }, timeline: { ranges: [{ id: CLIP_A, source_start: 0, source_end: 8 }] }, transcript: [],
    })),
    http.get("*/studio/projects/:projectId/clips", () => HttpResponse.json({
      project_id: PROJECT_ID, card_count: CARDS.length,
      active_clip_id: ledgerActiveClipId, cards: CARDS,
    })),
    http.put("*/studio/projects/:projectId/active-clip", async ({ request }) => {
      const body = await request.json() as { clip_id?: string | null };
      activeClipPuts.push({ clip_id: body.clip_id ?? null });
      if (activeClipStatus !== 200) {
        return HttpResponse.json({ detail: { code: "E_ACTIVE_CLIP_FAILED", message: "台账写入失败" } }, { status: activeClipStatus });
      }
      if (hangPutArmed) {
        hangPutArmed = false;
        return new Promise<Response>((resolvePromise) => { hangPutResolver = resolvePromise; });
      }
      ledgerActiveClipId = body.clip_id ?? null;
      return HttpResponse.json({ project_id: PROJECT_ID, active_clip_id: body.clip_id ?? null, changed: true });
    }),
    http.all("*", () => HttpResponse.json({}, { status: 404 })),
  );
});

const mountPage = () => render(<StudioPage />);

const cardOf = (container: HTMLElement, clipId: string) => {
  const found = Array.from(container.querySelectorAll<HTMLElement>(".asset-card.rendered-card"))
    .find((card) => card.getAttribute("data-clip-id") === clipId || card.querySelector("strong")?.textContent?.includes(clipId === CLIP_A ? "01" : "02"));
  if (!found) throw new Error(`成片卡 ${clipId} 尚未渲染`);
  return found;
};

const waitCards = async (container: HTMLElement) => {
  await waitFor(() => expect(container.querySelectorAll(".asset-card.rendered-card").length).toBe(2));
};

const playerVideo = (container: HTMLElement) =>
  container.querySelector(".player-stage video") as HTMLVideoElement | null;

describe("素材卡缩略图 = 切片视频真实第一帧", () => {
  it("每张卡的缩略图都指向 first-frame.jpg，cover 存在也不用生成封面冒充", async () => {
    // CLIP_A 带一张已生成的封面：缩略图仍必须是第一帧，不是 artifact 封面 URL。
    server.use(http.get("*/studio/projects/:projectId/clips", () => HttpResponse.json({
      project_id: PROJECT_ID, card_count: CARDS.length, active_clip_id: null,
      cards: [
        { ...CARDS[0], cover: { cover_asset_id: "cover-a", revision: 2, active_revision: 2,
          path: `output/${TASK_ID}/${CLIP_A}/cover.jpg`, sha256: "a".repeat(64), bytes: 2048,
          kind: "auto_edit", style_id: "realframe_v2", updated_at: "2026-10-21T00:00:00Z",
          intro_burned: true, can_undo: false, undo_revision: null, history: [] } },
        CARDS[1],
      ],
    })));
    const { container } = mountPage();
    await waitCards(container);
    // 2026-10-29：打开项目 = 原片视图，等原片载入完成（素材区出现原始素材（1））再交互。
    await waitFor(() => expect(container.textContent).toContain("原始素材（1）"));
    const imgs = Array.from(container.querySelectorAll<HTMLImageElement>(".rendered-cover img"));
    expect(imgs.map((img) => img.getAttribute("src"))).toEqual([firstFrameUrl(CLIP_A), firstFrameUrl(CLIP_B)]);
    // 冒充检查：生成封面的 artifact URL 绝不出现在缩略图上。
    expect(imgs.map((img) => img.getAttribute("src"))).not.toContain(expect.stringContaining("cover.jpg"));
    expect(imgs[0].getAttribute("alt")).toContain("第一帧");
  });
});

describe("点击卡片：唯一 active_clip_id + 播放器/精修转写一次切换", () => {
  it("点哪张播哪张：PUT active-clip、播放器 src、精修面板与选中态一致", async () => {
    const { container } = mountPage();
    await waitCards(container);
    // 2026-10-29：打开项目 = 原片视图，等原片载入完成（素材区出现原始素材（1））再交互。
    await waitFor(() => expect(container.textContent).toContain("原始素材（1）"));
    // 初始：无选中、无回写。
    expect(container.querySelectorAll(".asset-card.rendered-card.active")).toHaveLength(0);
    expect(activeClipPuts).toEqual([{ clip_id: null }]);

    fireEvent.click(cardOf(container, CLIP_A));
    await waitFor(() => expect(activeClipPuts).toEqual([{ clip_id: null }, { clip_id: CLIP_A }]));
    await waitFor(() => expect(playerVideo(container)?.getAttribute("src")).toBe(playerSrc(CLIP_A)));
    expect(container.querySelectorAll(".asset-card.rendered-card.active")).toHaveLength(1);
    expect(cardOf(container, CLIP_A).classList.contains("active")).toBe(true);
    // 2026-11-09 契约：切片视图只保留「切片精修文案」面板（医生/患者标注），
    // 不再渲染下方转写分段列表（重复板块，用户指令删除）。
    await waitFor(() => expect(container.querySelector(".refined-transcript-panel")).toBeTruthy());
    expect(container.querySelector(".transcript-text")).toBeNull();

    fireEvent.click(cardOf(container, CLIP_B));
    await waitFor(() => expect(activeClipPuts).toEqual([{ clip_id: null }, { clip_id: CLIP_A }, { clip_id: CLIP_B }]));
    await waitFor(() => expect(playerVideo(container)?.getAttribute("src")).toBe(playerSrc(CLIP_B)));
    expect(cardOf(container, CLIP_B).classList.contains("active")).toBe(true);
    expect(container.querySelectorAll(".asset-card.rendered-card.active")).toHaveLength(1);
    expect(container.querySelector(".refined-transcript-panel")).toBeTruthy();
    expect(container.querySelector(".transcript-text")).toBeNull();
  });

  it("切换失败（PUT 非 2xx）回滚保留原切片：播放器与转写回到原切片", async () => {
    const { container } = mountPage();
    await waitCards(container);
    // 2026-10-29：打开项目 = 原片视图，等原片载入完成（素材区出现原始素材（1））再交互。
    await waitFor(() => expect(container.textContent).toContain("原始素材（1）"));
    fireEvent.click(cardOf(container, CLIP_A));
    await waitFor(() => expect(cardOf(container, CLIP_A).classList.contains("active")).toBe(true));

    activeClipStatus = 500;
    fireEvent.click(cardOf(container, CLIP_B));
    // B 被回滚：A 仍是唯一选中切片，播放器回到 A（切片视图无分段转写列表）。
    await waitFor(() => expect(cardOf(container, CLIP_A).classList.contains("active")).toBe(true));
    expect(cardOf(container, CLIP_B).classList.contains("active")).toBe(false);
    expect(container.querySelectorAll(".asset-card.rendered-card.active")).toHaveLength(1);
    await waitFor(() => expect(playerVideo(container)?.getAttribute("src")).toBe(playerSrc(CLIP_A)));
    expect(container.querySelector(".transcript-text")).toBeNull();
    // 回滚是本地恢复，不再产生第二次回写（服务端仍是 A）。
    expect(activeClipPuts).toEqual([{ clip_id: null }, { clip_id: CLIP_A }, { clip_id: CLIP_B }]);
  });

  it("失败落地前用户已点开别的切片：迟到失败不倒车（以最新选择为准）", async () => {
    const { container } = mountPage();
    await waitCards(container);
    // 2026-10-29：打开项目 = 原片视图，等原片载入完成（素材区出现原始素材（1））再交互。
    await waitFor(() => expect(container.textContent).toContain("原始素材（1）"));
    fireEvent.click(cardOf(container, CLIP_A));
    await waitFor(() => expect(cardOf(container, CLIP_A).classList.contains("active")).toBe(true));

    // B 的 PUT 挂起（慢请求），用户等不及又点了 A。
    hangPutArmed = true;
    fireEvent.click(cardOf(container, CLIP_B));
    await waitFor(() => expect(activeClipPuts).toEqual([{ clip_id: null }, { clip_id: CLIP_A }, { clip_id: CLIP_B }]));
    fireEvent.click(cardOf(container, CLIP_A));
    await waitFor(() => expect(activeClipPuts).toEqual([{ clip_id: null }, { clip_id: CLIP_A }, { clip_id: CLIP_B }, { clip_id: CLIP_A }]));
    // 挂起的 B 请求最终以 500 落地：不是最新一次操作 → 不回滚、不报「已保留原切片」。
    hangPutResolver?.(HttpResponse.json({ detail: { code: "E_ACTIVE_CLIP_FAILED", message: "台账写入失败" } }, { status: 500 }));
    await act(async () => { await Promise.resolve(); });
    expect(container.textContent).not.toContain("已保留原切片");
    expect(cardOf(container, CLIP_A).classList.contains("active")).toBe(true);
    expect(playerVideo(container)?.getAttribute("src")).toBe(playerSrc(CLIP_A));
  });
});

describe("打开项目一律原片视图（2026-10-29 移除自动恢复上次选中）", () => {
  it("台账 active_clip_id 命中已有卡片：不再自动恢复成片视图，也不回写 PUT", async () => {
    ledgerActiveClipId = CLIP_B;
    const { container } = mountPage();
    await waitCards(container);
    // 2026-10-29：打开项目 = 原片视图，等原片载入完成（素材区出现原始素材（1））再交互。
    await waitFor(() => expect(container.textContent).toContain("原始素材（1）"));
    // 打开项目 = 原片视图：没有任何成片被自动选中（用户点卡片才选中）。
    await waitFor(() => expect(container.querySelectorAll(".asset-card.rendered-card.active")).toHaveLength(0));
    expect(cardOf(container, CLIP_B).classList.contains("active")).toBe(false);
    // 播放器不指向成片；服务端记录未被回写（没有选择动作就没有 PUT）。
    expect(playerVideo(container)?.getAttribute("src")).not.toBe(playerSrc(CLIP_B));
    expect(activeClipPuts).toEqual([{ clip_id: null }]);
  });

  it("台账 active_clip_id 悬空（切片已被清空/替换）：不恢复也不回写", async () => {
    ledgerActiveClipId = "clip-gone-0000";
    const { container } = mountPage();
    await waitCards(container);
    // 2026-10-29：打开项目 = 原片视图，等原片载入完成（素材区出现原始素材（1））再交互。
    await waitFor(() => expect(container.textContent).toContain("原始素材（1）"));
    await waitFor(() => expect(container.querySelectorAll(".asset-card.rendered-card.active")).toHaveLength(0));
    expect(activeClipPuts).toEqual([{ clip_id: null }]);
    expect(playerVideo(container)?.getAttribute("src")).toBe(`/video-api/studio/timeline/${PROJECT_ID}/source`);
  });

  it("台账未记录选中（null）：保持原片视图，不自动挑第一张卡", async () => {
    const { container } = mountPage();
    await waitCards(container);
    // 2026-10-29：打开项目 = 原片视图，等原片载入完成（素材区出现原始素材（1））再交互。
    await waitFor(() => expect(container.textContent).toContain("原始素材（1）"));
    await waitFor(() => expect(container.querySelectorAll(".asset-card.rendered-card.active")).toHaveLength(0));
    expect(activeClipPuts).toEqual([{ clip_id: null }]);
  });
});
