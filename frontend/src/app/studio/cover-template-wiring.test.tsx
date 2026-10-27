/**
 * 封面版型库 + 封面工作流（2026-10-21 精简版）整页接线契约。
 *
 * 锁定验收：
 *   ① 四张模板卡来自版本化注册表，显示效果缩略图（后端路由出图）与名称，单选；
 *   ② 未选切片：只有提示，无卡片无「制作封面」；
 *   ③ 选版式 → 制作封面：POST cover（template_id + replace_intro=false，只出草稿不动成片）；
 *   ④ 草稿（待应用）状态下点击封面图片只出现「应用/删除」；
 *      应用 → POST cover-template/apply；删除 → POST cover-template/delete；
 *      两个操作都按 clip 隔离（不串片），刷新后按台账载荷恢复；
 *   ⑤ 无多余封面管理模块（无版本列表/独立封面网格等）。
 *
 * 基建同 clip-selection.test.tsx：jsdom 不跑真实样式、msw onUnhandledRequest=error、
 * 媒体 API 最小桩；不涉及真实渲染（端点 mock 返回确定性响应）。
 */
import { describe, expect, it, vi, beforeAll, beforeEach } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "@/test/setup";

vi.mock("./studio.css", () => ({}));
import StudioPage from "./page";

const PROJECT_ID = "timeline_coverlib01";
const TASK_ID = "autoedit_aaaabbbbcccc";
const CLIP_A = "clip-aaaaaaaaaa";
const CLIP_B = "clip-bbbbbbbbbb";
const REGISTRY_VERSION = "2026-10-21.3";

const TEMPLATES = [
  { template_id: "cute", name: "可爱版", version: "2", example_path: "/studio/cover-templates/cute/example.svg", description: "柔和", sort: 1 },
  { template_id: "cool", name: "酷炫版", version: "2", example_path: "/studio/cover-templates/cool/example.svg", description: "霓虹", sort: 2 },
  { template_id: "anime_layout", name: "动漫排版", version: "2", example_path: "/studio/cover-templates/anime_layout/example.svg", description: "分格", sort: 3 },
  { template_id: "yellow_bold", name: "黄色大字", version: "2", example_path: "/studio/cover-templates/yellow_bold/example.svg", description: "压屏", sort: 4 },
];

const clipCard = (clipId: string, name: string) => ({
  clip_id: clipId, name, title: null, topic: "血压科普", keywords: [],
  duration: 8, source_start: 0, source_end: 8, task_id: TASK_ID,
  render_task_id: TASK_ID, render_revision: 1, status: "ready",
  video_path: `output/${TASK_ID}/${clipId}/clip_final.mp4`,
  cover: { path: `output/${TASK_ID}/${clipId}/cover.jpg`, revision: 1 },
  task_history: [],
  segments: [{ start: 0, end: 2, text: `${name}的转写句子。` }],
  cover_path: `output/${TASK_ID}/${clipId}/cover.jpg`,
});

/** 模拟台账：Make/Apply/Delete 更新这里，模拟刷新（重挂页面）后从服务端读回。 */
const ledger: Record<string, string | null> = { [CLIP_A]: null, [CLIP_B]: null };
/** 待应用草稿（值 = revision，>0 = 有草稿待应用）。 */
const pendingDraft: Record<string, number> = {};

/** 台账卡片载荷（应用成功后 applied 置真：前端据此显示「已应用到成片」）。 */
const cardsPayload = () => ({
  project_id: PROJECT_ID, card_count: 2, active_clip_id: null,
  cards: [clipCard(CLIP_A, "剪辑成片 01"), clipCard(CLIP_B, "剪辑成片 02")].map((card) => ({
    ...card,
    cover_template: ledger[card.clip_id]
      ? { template_id: ledger[card.clip_id], registry_version: REGISTRY_VERSION, updated_at: "t" }
      : null,
    cover_path: (pendingDraft[card.clip_id] ?? 0) > 0 || ledger[card.clip_id]
      ? `output/${TASK_ID}/${card.clip_id}/cover.jpg`
      : null,
    cover_workflow: {
      has_cover: true,
      pending: (pendingDraft[card.clip_id] ?? 0) > 0 && !appliedClips[card.clip_id],
      applied: Boolean(appliedClips[card.clip_id]),
      revision: (pendingDraft[card.clip_id] ?? 0) || null,
      style: ledger[card.clip_id] ? `template:${ledger[card.clip_id]}` : null,
    },
  })),
});
const coverTemplatePuts: { clip_id: string; template_id: string | null }[] = [];
const makePosts: { clip_id: string; template_id: string; replace_intro: boolean }[] = [];
const applyTaskBodies: { task_type?: string; clip_id: string }[] = [];
const applyTaskByClip: Record<string, string> = {};
const taskClip: Record<string, string> = {};
const appliedClips: Record<string, boolean> = {};
const taskPolls: Record<string, number> = {};
const deletePosts: string[] = [];
let failMake = false;
let failApply = false;

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
  ledger[CLIP_A] = null;
  ledger[CLIP_B] = null;
  pendingDraft[CLIP_A] = 0;
  pendingDraft[CLIP_B] = 0;
  coverTemplatePuts.length = 0;
  makePosts.length = 0;
  applyTaskBodies.length = 0;
  deletePosts.length = 0;
  failMake = false;
  failApply = false;
  for (const key of Object.keys(appliedClips)) delete appliedClips[key];
  for (const key of Object.keys(taskPolls)) delete taskPolls[key];
  for (const key of Object.keys(applyTaskByClip)) delete applyTaskByClip[key];
  for (const key of Object.keys(taskClip)) delete taskClip[key];
  window.localStorage.clear();
  window.history.pushState({}, "", `/?source_id=${PROJECT_ID}`);
  server.use(
    http.get("*/studio/cover-templates", () => HttpResponse.json({
      registry_version: REGISTRY_VERSION, templates: TEMPLATES,
    })),
    // 切片选择（上一阶段）：点卡片会 PUT active-clip，放行 200 避免失败回滚干扰本阶段断言
    http.put("*/studio/projects/:projectId/active-clip", async ({ request }) => {
      const body = await request.json() as { clip_id?: string | null };
      return HttpResponse.json({ project_id: PROJECT_ID, active_clip_id: body.clip_id ?? null, changed: true });
    }),
    http.get("*/studio/cover-templates/:templateId/example.svg", ({ params }) => {
      const id = String(params.templateId ?? "");
      if (!TEMPLATES.some((t) => t.template_id === id)) {
        return HttpResponse.json({ detail: { code: "E_TEMPLATE_UNKNOWN" } }, { status: 404 });
      }
      return HttpResponse.json({ svg: true }, { status: 200 });
    }),
    http.put("*/studio/projects/:projectId/clips/:clipId/cover-template", async ({ request, params }) => {
      const body = await request.json() as { template_id?: string | null };
      const clipId = String(params.clipId ?? "");
      coverTemplatePuts.push({ clip_id: clipId, template_id: body.template_id ?? null });
      ledger[clipId] = body.template_id ?? null;
      return HttpResponse.json({ project_id: PROJECT_ID, clip_id: clipId, changed: true,
        cover_template: body.template_id ? { template_id: body.template_id, registry_version: REGISTRY_VERSION, updated_at: "t" } : null });
    }),
    http.post("*/studio/auto-edit/:taskId/clips/:clipId/cover", async ({ request, params }) => {
      const body = await request.json() as { template_id?: string; replace_intro?: boolean };
      const clipId = String(params.clipId ?? "");
      const templateId = String(body.template_id ?? "");
      makePosts.push({ clip_id: clipId, template_id: templateId, replace_intro: body.replace_intro === true });
      if (failMake) return HttpResponse.json({ detail: "E_RENDER_FAILED" }, { status: 500 });
      pendingDraft[clipId] = 2;
      return HttpResponse.json({ kind: "generated", style_id: `template:${templateId}`,
        idempotent: false, cover_revision: 2, replace_intro: false, qc: { passed: true },
        clip_id: clipId, task_id: String(params.taskId ?? "") });
    }),
    // 应用 = 真实的渲染任务：POST /studio/projects/{p}/clips/{c}/tasks (APPLY_COVER)
    // 之后轮询 GET /studio/tasks/{task_id}（先 processing 带进度，再 completed 带最新卡片）。
    http.post("*/studio/projects/:projectId/clips/:clipId/tasks", async ({ request, params }) => {
      const clipId = String(params.clipId ?? "");
      const body = await request.json() as { task_type?: string };
      applyTaskBodies.push({ task_type: body.task_type, clip_id: clipId });
      const taskId = `coverapply_${String(clipId).replace(/[^a-z0-9]/gi, "").slice(-8).padStart(8, "0")}`;
      applyTaskByClip[clipId] = taskId;
      taskClip[taskId] = clipId;
      taskPolls[clipId] = 0;
      return HttpResponse.json({ task_id: taskId, task_type: "APPLY_COVER", project_id: PROJECT_ID,
        clip_id: clipId, status: "queued", stage: "任务已受理", created_at: "t" }, { status: 202 });
    }),
    http.get("*/studio/tasks/:taskId", ({ params }) => {
      const taskId = String(params.taskId ?? "");
      const clipId = taskClip[taskId] ?? "";
      if (failApply) {
        return HttpResponse.json({ task_id: taskId, task_type: "APPLY_COVER", clip_id: clipId,
          status: "failed", stage: "失败", progress: 1,
          error: "E_COVER_RENDER_FAILED: 片头渲染失败，已保留原成片" });
      }
      const polls = (taskPolls[clipId] ?? 0) + 1;
      taskPolls[clipId] = polls;
      if (polls <= 2) {
        return HttpResponse.json({ task_id: taskId, task_type: "APPLY_COVER", clip_id: clipId,
          status: "processing", stage: "渲染 0.8s 封面片头并拼接正片", progress: 0.45 });
      }
      appliedClips[clipId] = true;
      pendingDraft[clipId] = 0;
      return HttpResponse.json({ task_id: taskId, task_type: "APPLY_COVER", clip_id: clipId,
        status: "completed", stage: "已完成", progress: 1,
        result: { applied: true, idempotent: false, intro_duration_s: 0.8,
                  card: cardsPayload().cards.find((card) => card.clip_id === clipId) } });
    }),
    http.post("*/studio/auto-edit/:taskId/clips/:clipId/cover-template/delete", ({ params }) => {
      const clipId = String(params.clipId ?? "");
      deletePosts.push(clipId);
      pendingDraft[clipId] = 0;
      return HttpResponse.json({ task_id: String(params.taskId), clip_id: clipId, deleted_revision: 2, current_revision: 1 });
    }),
    http.get("*/studio/timeline/:projectId", () => HttpResponse.json({
      id: PROJECT_ID, timeline: { ranges: [{ id: CLIP_A, source_start: 0, source_end: 8 }] }, transcript: [],
    })),
    http.get("*/studio/projects/:projectId/clips", () => HttpResponse.json(cardsPayload())),
    http.all("*", () => HttpResponse.json({}, { status: 404 })),
  );
});

const waitCards = async () => {
  await waitFor(() => expect(document.querySelectorAll(".asset-card.rendered-card").length).toBe(2));
};
const cardOf = (name: string) => {
  const found = Array.from(document.querySelectorAll<HTMLElement>(".asset-card.rendered-card"))
    .find((card) => card.querySelector("strong")?.textContent === name);
  if (!found) throw new Error(`成片卡 ${name} 尚未渲染`);
  return found;
};
const checkedCards = () => Array.from(document.querySelectorAll<HTMLElement>('[role="radio"][aria-checked="true"]'))
  .map((node) => node.getAttribute("data-testid"));

const selectClip = async (name: string) => {
  fireEvent.click(cardOf(name));
  await waitFor(() => expect(cardOf(name).classList.contains("active")).toBe(true));
};
const openCoverTab = async () => {
  fireEvent.click(await screen.findByRole("tab", { name: "封面与画面" }));
  await waitFor(() => expect(screen.getByTestId("cover-template-grid")).toBeTruthy());
};
/** 封面缩略图上的内部状态标记（状态不再用可见面板展示）。 */
const thumbState = () => {
  const thumb = screen.getByTestId("cover-workspace-image");
  return { pending: thumb.getAttribute("data-pending") === "true", applied: thumb.getAttribute("data-applied") === "true" };
};

describe("封面版型库 + 工作流整页接线", () => {
  it("未选切片：封面区与版型区都只有提示，无生成入口", async () => {
    render(<StudioPage />);
    await waitCards();
    fireEvent.click(await screen.findByRole("tab", { name: "封面与画面" }));
    await waitFor(() => expect(screen.getByTestId("cover-workspace-no-clip")).toBeTruthy());
    expect(screen.getByTestId("cover-workspace-no-clip").textContent).toContain("先在左侧选中一条剪辑成片");
    expect(screen.queryByTestId("cover-template-grid")).toBeNull();
    expect(screen.queryByTestId("cover-template-generate")).toBeNull();
  });

  it("选中切片后四张模板卡单选展示；选择版式后可制作封面（replace_intro=false）", async () => {
    render(<StudioPage />);
    await waitCards();
    await selectClip("剪辑成片 01");
    await openCoverTab();
    const grid = await waitFor(() => {
      const found = screen.getByTestId("cover-template-grid");
      if (found.querySelectorAll('[role="radio"]').length !== 4) throw new Error("cards not ready");
      return found;
    });
    expect(grid.querySelectorAll(".cover-template-frame img").length).toBe(4);
    for (const name of ["可爱版", "酷炫版", "动漫排版", "黄色大字"]) {
      expect(screen.getByText(name)).toBeInTheDocument();
    }
    expect(checkedCards()).toHaveLength(0);
    fireEvent.click(screen.getByTestId("cover-template-card-cool"));
    await waitFor(() => expect(coverTemplatePuts).toEqual([{ clip_id: CLIP_A, template_id: "cool" }]));
    await waitFor(() => expect(checkedCards()).toEqual(["cover-template-card-cool"]));
    expect(checkedCards()).toHaveLength(1);

    const button = await screen.findByTestId("cover-template-generate");
    expect(button).toHaveTextContent("使用酷炫版制作封面");
    fireEvent.click(button);
    await waitFor(() => expect(makePosts).toEqual([
      { clip_id: CLIP_A, template_id: "cool", replace_intro: false },
    ]));
  });

  /**
   * 封面图片区（2026-10-21 尺寸修复）：缩略图容器是 9:16 的 .cover-workspace-image
   * （CSS 80×142、img contain，几何由 headless 验收脚本实测），点缩略图才展开应用/删除。
   */
  it("待应用：缩略图点击展开应用/删除（容器是 9:16 竖版缩略图，不是巨幅封面）", async () => {
    pendingDraft[CLIP_A] = 2;
    render(<StudioPage />);
    await waitCards();
    await selectClip("剪辑成片 01");
    await openCoverTab();
    await waitFor(() => expect(thumbState().pending).toBe(true), { timeout: 4000 });
    const thumb = screen.getByTestId("cover-workspace-image");
    expect(thumb.className).toContain("cover-workspace-image");
    expect(thumb.querySelector("img")?.getAttribute("src")).toContain("cover.jpg");
    // 未点击前不出现操作条
    expect(screen.queryByTestId("cover-workspace-actions")).toBeNull();
    fireEvent.click(thumb);
    await waitFor(() => expect(screen.getByTestId("cover-workspace-actions")).toBeTruthy());
    expect(screen.getByTestId("cover-workspace-apply")).toBeTruthy();
    expect(screen.getByTestId("cover-workspace-delete")).toBeTruthy();
  });

  it("应用：创建 APPLY_COVER 渲染任务、显示进度，完成后内部转已应用（不再展示状态面板）", async () => {
    pendingDraft[CLIP_A] = 2;
    render(<StudioPage />);
    await waitCards();
    await selectClip("剪辑成片 01");
    await openCoverTab();
    await waitFor(() => expect(thumbState().pending).toBe(true), { timeout: 4000 });
    fireEvent.click(screen.getByTestId("cover-workspace-image"));
    fireEvent.click(await screen.findByTestId("cover-workspace-apply"));

    // 真实渲染任务：POST /tasks 带 APPLY_COVER（不是同步 apply 端点）
    await waitFor(() => expect(applyTaskBodies.length).toBe(1));
    expect(applyTaskBodies[0]).toEqual({ task_type: "APPLY_COVER", clip_id: CLIP_A });
    // 轮询 processing 时显示阶段 + 进度
    const progress = await waitFor(() => screen.getByTestId("cover-workspace-progress"));
    expect(progress.textContent).toContain("渲染 0.8s 封面片头");
    expect(progress.textContent).toContain("45%");
    // 完成后：内部转已应用；状态面板（待应用/已应用徽标与说明）不再展示
    await waitFor(() => expect(thumbState().applied).toBe(true), { timeout: 5000 });
    expect(thumbState().pending).toBe(false);
    expect(screen.queryByTestId("cover-workspace-pending")).toBeNull();
    expect(screen.queryByTestId("cover-workspace-applied")).toBeNull();
    expect(screen.queryByTestId("cover-workspace-applied-hint")).toBeNull();
    expect(screen.queryByTestId("cover-workspace-applied-inline")).toBeNull();
    expect(document.querySelector('[data-testid="cover-workspace"]')?.textContent ?? "").not.toContain("已应用到成片");
    expect(document.querySelector('[data-testid="cover-workspace"]')?.textContent ?? "").not.toContain("不能删除");
    // 应用后必须退出「封面首帧预览层」：新成片开头就是封面，预览层会挡住播放画面
    await waitFor(() => expect(screen.queryByTestId("cover-frame-view")).toBeNull());
    expect(document.querySelector(".player-stage")?.getAttribute("data-preview-mode")).toBe("video");
    // 完成提示可见（全局状态条），而不是只写进看不见的 state
    expect(screen.getByTestId("studio-message").textContent).toContain("片头已加入成片");
    // 成片文件被原地替换 → 播放器 URL 必须换版本号，否则会继续播旧画面
    const videoSrc = document.querySelector(".preview-frame video")?.getAttribute("src") || "";
    expect(videoSrc).toContain("&v=");
    expect(screen.queryByTestId("cover-workspace-delete")).toBeNull();     // 已被成片使用 → 不放删除
    expect(screen.queryByTestId("cover-workspace-actions")).toBeNull();
  });

  it("应用失败：展示后端原因、保留待应用状态（不假报成功）", async () => {
    pendingDraft[CLIP_A] = 2;
    failApply = true;
    render(<StudioPage />);
    await waitCards();
    await selectClip("剪辑成片 01");
    await openCoverTab();
    await waitFor(() => expect(thumbState().pending).toBe(true), { timeout: 4000 });
    fireEvent.click(screen.getByTestId("cover-workspace-image"));
    fireEvent.click(await screen.findByTestId("cover-workspace-apply"));
    // 失败后仍是待应用；不出现「已应用」
    await waitFor(() => expect(screen.queryByTestId("cover-workspace-progress")).toBeNull(), { timeout: 5000 });
    expect(thumbState().pending).toBe(true);
    expect(thumbState().applied).toBe(false);
    expect(applyTaskBodies.length).toBe(1);
    // 失败原因必须可见（全局状态条），且明确原成片未动
    const toast = screen.getByTestId("studio-message");
    expect(toast.textContent).toContain("E_COVER_RENDER_FAILED");
    expect(toast.textContent).toContain("原成片保持不变");
  });

  it("切换切片不串片：01 的待应用状态不跟随到 02", async () => {
    pendingDraft[CLIP_A] = 2;
    ledger[CLIP_B] = "cute";
    render(<StudioPage />);
    await waitCards();
    await selectClip("剪辑成片 01");
    await openCoverTab();
    // 01：有待应用草稿（内部标记，无可见徽标；无版型选择，radiogroup 无选中）
    await waitFor(() => expect(thumbState().pending).toBe(true), { timeout: 4000 });
    fireEvent.click(cardOf("剪辑成片 02"));
    await waitFor(() => expect(cardOf("剪辑成片 02").classList.contains("active")).toBe(true));
    // 02：无待应用（01 的不跟随），02 的台账选择是 cute
    await waitFor(() => expect(thumbState().pending).toBe(false), { timeout: 4000 });
    await waitFor(() => expect(checkedCards()).toEqual(["cover-template-card-cute"]));
  });

  it("刷新后恢复：重挂页面按台账载荷恢复选择与待应用状态", async () => {
    ledger[CLIP_B] = "anime_layout";
    pendingDraft[CLIP_B] = 2;
    const first = render(<StudioPage />);
    await waitCards();
    first.unmount();
    cleanup();
    render(<StudioPage />);
    await waitCards();
    await selectClip("剪辑成片 02");
    await openCoverTab();
    await waitFor(() => expect(checkedCards()).toEqual(["cover-template-card-anime_layout"]));
    expect(thumbState().pending).toBe(true);
  });

  /**
   * 封面预览比例（2026-10-21 修）整页契约：
   * 封面层与普通视频播放显式分开（player-stage 的 data-preview-mode），
   * 且封面 img 被包在独立 9:16 画布容器里（几何由 headless 验收脚本用真实
   * 1080×1920 封面实测：画布 258×458、img contain 撑满、改窗口比例不变）。
   * jsdom 不跑真实样式，这里锁定结构与状态切换，防止回退成「img 直接铺在横版网格里」。
   */
  it("封面预览：独立 9:16 画布 + data-preview-mode=cover；切回普通播放不残留", async () => {
    render(<StudioPage />);
    await waitCards();
    await selectClip("剪辑成片 01");
    await openCoverTab();
    expect(document.querySelector(".player-stage")?.getAttribute("data-preview-mode")).toBe("video");
    expect(screen.queryByTestId("cover-frame-view")).toBeNull();

    fireEvent.click(screen.getByTestId("cover-template-card-cool"));
    await waitFor(() => expect(coverTemplatePuts.length).toBe(1));
    fireEvent.click(await screen.findByTestId("cover-template-generate"));
    await waitFor(() => expect(makePosts.length).toBe(1));

    const view = await waitFor(() => screen.getByTestId("cover-frame-view"));
    expect(document.querySelector(".player-stage")?.getAttribute("data-preview-mode")).toBe("cover");
    const canvas = view.querySelector(".cover-frame-canvas");
    expect(canvas).toBeTruthy();
    // img 必须在画布容器内（此前 img 直接作为 .cover-frame-view 的网格子项，height:100% 退化被裁切）
    expect(canvas?.querySelector("img")?.getAttribute("src")).toContain("cover.jpg");
    expect(view.querySelector(":scope > img")).toBeNull();

    // 播放即退出封面预览：否则用户点播放后画面一直停在封面（音频在下面播、画面不动）
    fireEvent.play(document.querySelector(".preview-frame video")!);
    await waitFor(() => expect(screen.queryByTestId("cover-frame-view")).toBeNull());
    expect(document.querySelector(".player-stage")?.getAttribute("data-preview-mode")).toBe("video");

    // 重新生成一次封面让预览层回来，继续验证「应用」也会退出预览
    fireEvent.click(await screen.findByTestId("cover-template-generate"));
    await waitFor(() => expect(makePosts.length).toBe(2));
    await waitFor(() => expect(screen.getByTestId("cover-frame-view")).toBeTruthy());

    // 切到无封面的 02：显示层与状态一起退出，普通播放画布照旧
    fireEvent.click(cardOf("剪辑成片 02"));
    await waitFor(() => expect(cardOf("剪辑成片 02").classList.contains("active")).toBe(true));
    await waitFor(() => expect(screen.queryByTestId("cover-frame-view")).toBeNull());
    expect(document.querySelector(".player-stage")?.getAttribute("data-preview-mode")).toBe("video");
  });
});
