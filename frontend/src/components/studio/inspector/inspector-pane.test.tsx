// Inspector 外壳测试（契约 §1 / §5 / §6）：
// 固定 5 标签、切换标签、绑定到当前选中对象（成片 clip_id / 源片哨兵 source）、
// 换绑定重新拉取该 clip 的数据、字幕状态上抛（叠加层与时间线字幕轨的数据源）、
// 「封面与画面」的应用 → PUT config、取消 → 不写。

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "@/test/setup";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { defaultTransform } from "@/lib/clip-transform";

import { InspectorPane, type InspectorTabId } from "./inspector-pane";
import { CLIP_ID, PROJECT_ID, configView, inspectorHandlers, subtitleView } from "./test-utils";

// 用 setup.ts 提供的全局 msw server（已在 beforeAll 里 listen，afterEach 里
// resetHandlers）：同一个进程里二次 setupServer()/listen() 会让请求体被消费两次
// （msw 打印 "Body is unusable"，用例仍通过但输出被污染），所以这里只注册合同
// 端点的默认 handler，不再自己建 server。
beforeEach(() => server.use(...inspectorHandlers()));

function renderPane(overrides: Partial<Parameters<typeof InspectorPane>[0]> = {}) {
  const handlers = {
    onTabChange: vi.fn(),
    onSeek: vi.fn(),
    onSubtitleStateChange: vi.fn(),
    onPlayerVolumeChange: vi.fn(),
    onRegenerateCover: vi.fn(),
    onEditTitle: vi.fn(),
    onShowCoverHistory: vi.fn(),
    onAdoptCover: vi.fn(),
    onUndoCover: vi.fn(),
    onAdoptSourceChange: vi.fn(),
    onTransformDraftChange: vi.fn(),
    onTransformBackgroundChange: vi.fn(),
    onRatioLockChange: vi.fn(),
    onViewerZoomChange: vi.fn(),
    onFitView: vi.fn(),
    onOriginalRatio: vi.fn(),
    onCanvasMode: vi.fn(),
    onResetTransform: vi.fn(),
    onViewerAspectChange: vi.fn(),
    onApplyTransform: vi.fn().mockResolvedValue(undefined),
    onCancelTransform: vi.fn(),
  };
  const props: Parameters<typeof InspectorPane>[0] = {
    projectId: PROJECT_ID,
    clipId: CLIP_ID,
    baseUrl: "",
    activeTab: "transcript",
    fallbackSegments: [],
    currentTime: 0,
    duration: 10,
    boundToActiveClip: true,
    clipName: "剪辑成片 01",
    coverImageUrl: null,
    coverAssetId: null,
    coverRevision: 1,
    coverVersionLabel: "v1",
    coverStatusText: "原始成片封面",
    title: "标题",
    coverBusy: false,
    canUndoCover: false,
    coverUndoRevision: null,
    coverHistory: [],
    coverHistoryVisible: false,
    coverHistoryLoading: false,
    adoptOptions: [{ clip_id: CLIP_ID, name: "剪辑成片 01" }],
    adoptSource: CLIP_ID,
    transform: defaultTransform(),
    transformLocked: false,
    transformBackground: "blur",
    ratioLock: false,
    viewerZoom: 100,
    viewerAspect: "9:16",
    ...handlers,
    ...overrides,
  };
  const rendered = render(<InspectorPane {...props} />);
  return { handlers, props, ...rendered };
}

/** 受控标签的包装：让 onTabChange 真的切换内容（和页面接线一致）。 */
function ControlledPane(props: Parameters<typeof InspectorPane>[0]) {
  const [tab, setTab] = useState<InspectorTabId>(props.activeTab);
  return <InspectorPane {...props} activeTab={tab} onTabChange={(next) => { setTab(next); props.onTabChange(next); }} />;
}

describe("InspectorPane", () => {
  it("固定 3 个单行标签：转写｜封面｜效果（完整名称在 title/aria-label）", () => {
    renderPane();
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((node) => node.textContent)).toEqual(["转写", "封面", "效果"]);
    // 短文案不得丢失完整语义：封面→封面与画面、效果→效果设置
    const titles = tabs.map((node) => node.getAttribute("title"));
    expect(titles).toEqual(["转写", "封面与画面", "效果设置"]);
    tabs.forEach((node) => expect(node.getAttribute("aria-label")).toBe(node.getAttribute("title")));
  });

  it("切标签：点「效果」触发 onTabChange 且内容跟着切换", async () => {
    const handlers = {
      onTabChange: vi.fn(),
    };
    render(<ControlledPane {...renderPaneProps(handlers)} />);
    fireEvent.click(screen.getByTestId("inspector-tab-effects"));
    expect(handlers.onTabChange).toHaveBeenCalledWith("effects");
    await waitFor(() => expect(screen.getByTestId("inspector-effects-tab")).toBeInTheDocument());
  });

  it("「字幕」「音频」人工编辑入口已移除：标签不存在，但 cue/音频配置仍会拉取并上抛", async () => {
    const { handlers } = renderPane();
    expect(screen.queryByTestId("inspector-tab-subtitle")).toBeNull();
    expect(screen.queryByTestId("inspector-tab-audio")).toBeNull();
    // 数据流保留：配置到达后 cue 仍上抛（播放器叠加层与时间线字幕轨的数据源）。
    await waitFor(() => expect(handlers.onSubtitleStateChange).toHaveBeenCalled());
  });

  it("绑定成片 clip_id：GET config / subtitles / render-config 都指向该 clip", async () => {
    const urls: string[] = [];
    server.use(
      http.get("*/studio/clips/*", ({ request }) => {
        urls.push(new URL(request.url).pathname);
        if (request.url.includes("/config")) return HttpResponse.json(configView());
        if (request.url.includes("/subtitles")) return HttpResponse.json(subtitleView());
        return HttpResponse.json({ project_id: PROJECT_ID, clip_id: CLIP_ID, binding: "clip", effective: [], sources: {}, export: { resolution: "1080x1920", deferred: true, note: "" }, config: configView().config });
      }),
      ...inspectorHandlers(),
    );
    renderPane({ activeTab: "subtitle" });
    await waitFor(() => expect(urls.length).toBeGreaterThanOrEqual(3));
    expect(urls).toContain(`/studio/clips/${PROJECT_ID}/${CLIP_ID}/config`);
    expect(urls).toContain(`/studio/clips/${PROJECT_ID}/${CLIP_ID}/subtitles`);
  });

  it("换绑定（另一个 clip_id）→ 按新 clip 重新拉取，并把新字幕状态上抛", async () => {
    const urls: string[] = [];
    server.use(
      http.get("*/studio/clips/*", ({ request }) => {
        const path = new URL(request.url).pathname;
        urls.push(path);
        const other = path.includes("clip-bbbb-02");
        if (path.endsWith("/config")) return HttpResponse.json(configView({ clip_id: other ? "clip-bbbb-02" : CLIP_ID }));
        if (path.endsWith("/subtitles")) return HttpResponse.json(subtitleView({
          clip_id: other ? "clip-bbbb-02" : CLIP_ID,
          cues: [{ segment_id: other ? "seg-b" : "seg-a", start: 0, end: 1, text: other ? "B 字幕" : "A 字幕" }],
        }));
        return HttpResponse.json({ project_id: PROJECT_ID, clip_id: CLIP_ID, binding: "clip", effective: [], sources: {}, export: { resolution: "1080x1920", deferred: true, note: "" }, config: configView().config });
      }),
      ...inspectorHandlers(),
    );
    const onSubtitleStateChange = vi.fn();
    const { rerender, props } = renderPane({ activeTab: "subtitle", onSubtitleStateChange });
    await waitFor(() => expect(onSubtitleStateChange).toHaveBeenLastCalledWith(expect.objectContaining({
      cues: [expect.objectContaining({ text: "A 字幕" })],
    })));

    rerender(<InspectorPane {...props} activeTab="subtitle" clipId="clip-bbbb-02" onSubtitleStateChange={onSubtitleStateChange} />);
    await waitFor(() => expect(urls).toContain(`/studio/clips/${PROJECT_ID}/clip-bbbb-02/config`));
    await waitFor(() => expect(onSubtitleStateChange).toHaveBeenLastCalledWith(expect.objectContaining({
      cues: [expect.objectContaining({ text: "B 字幕" })],
    })));
    expect(screen.getByTestId("inspector-pane").getAttribute("data-binding")).toBe(`${PROJECT_ID}::clip-bbbb-02`);
  });

  it("未选成片但已载入素材 → 绑定哨兵 source（配置读源片级）", async () => {
    const urls: string[] = [];
    server.use(
      http.get("*/studio/clips/*", ({ request }) => {
        urls.push(new URL(request.url).pathname);
        if (request.url.includes("/config")) return HttpResponse.json(configView({ clip_id: "source", binding: "source" }));
        if (request.url.includes("/subtitles")) return HttpResponse.json(subtitleView({ clip_id: "source" }));
        return HttpResponse.json({ project_id: PROJECT_ID, clip_id: "source", binding: "source", effective: [], sources: {}, export: { resolution: "1080x1920", deferred: true, note: "" }, config: configView().config });
      }),
      ...inspectorHandlers(),
    );
    renderPane({ clipId: "source", boundToActiveClip: false });
    expect(screen.getByTestId("inspector-source-binding").textContent).toContain("source_config");
    await waitFor(() => expect(urls).toContain(`/studio/clips/${PROJECT_ID}/source/config`));
  });

  it("没有可编辑对象时不发任何请求（只显示提示）", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    renderPane({ projectId: null, clipId: null, boundToActiveClip: false });
    expect(screen.getByTestId("inspector-no-binding")).toBeInTheDocument();
    expect(screen.getByTestId("inspector-pane").getAttribute("data-binding")).toBe("none");
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it("「封面与画面」应用 → PUT /studio/clips/{p}/{c}/config（body 带 transform），取消 → 不写", async () => {
    const calls: { method: string; url: string; body: unknown }[] = [];
    server.use(
      http.put("*/studio/clips/*", async ({ request }) => {
        calls.push({ method: request.method, url: new URL(request.url).pathname, body: await request.json() });
        return HttpResponse.json({ changed: ["transform"], config: configView().config, revisions: { transcript_revision: 1, render_revision: 5 }, task: null, render_deferred: true, render_note: "Phase 2" });
      }),
      ...inspectorHandlers(),
    );
    const applied: unknown[] = [];
    const { props } = renderPane({
      activeTab: "effects",
      onApplyTransform: async (next) => {
        applied.push(next);
        const { putClipConfig } = await import("./api");
        return putClipConfig("", PROJECT_ID, CLIP_ID, { transform: next as unknown as Record<string, unknown> });
      },
    });
    await waitFor(() => expect(screen.getByTestId("inspector-cover-picture-tab")).toBeInTheDocument());
    await waitFor(() => expect(document.querySelector(".transform-panel")).toBeTruthy());
    // 先在面板里改一次（缩放 +15%）→ 草稿变脏（预览实时），此时还没有任何写请求。
    fireEvent.click(screen.getByRole("button", { name: "放大片段" }));
    expect(calls).toHaveLength(0);
    // 「应用」才走持久化：页面回调 → PUT /studio/clips/{p}/{c}/config。
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "应用" }));
    });
    expect(applied).toHaveLength(1);
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].method).toBe("PUT");
    expect(calls[0].url).toBe(`/studio/clips/${PROJECT_ID}/${CLIP_ID}/config`);
    expect(calls[0].body).toMatchObject({ transform: expect.objectContaining({ scale: expect.closeTo(1.15, 5) }) });

    // 取消：只回调页面（回滚草稿），不产生任何写请求。
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(props.onCancelTransform).toHaveBeenCalled();
    expect(calls).toHaveLength(1);
  });

  it("保存失败（PUT 422）→ 显示失败原因，不静默", async () => {
    server.use(
      http.put("*/studio/clips/*", () => HttpResponse.json({ detail: "E_CONFIG_INVALID: 非法 max_chars_per_line" }, { status: 422 })),
      ...inspectorHandlers(),
    );
    renderPane({
      activeTab: "effects",
      onApplyTransform: async (next) => {
        const { putClipConfig } = await import("./api");
        return putClipConfig("", PROJECT_ID, CLIP_ID, { transform: next as unknown as Record<string, unknown> });
      },
    });
    await waitFor(() => expect(screen.getByTestId("inspector-cover-picture-tab")).toBeInTheDocument());
    await waitFor(() => expect(document.querySelector(".transform-panel")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "放大片段" }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "应用" }));
    });
    await waitFor(() => expect(screen.getByTestId("inspector-transform-state").textContent).toContain("非法 max_chars_per_line"));
  });
});

// 便捷：给受控标签用例复用默认 props。
function renderPaneProps(overrides: Partial<Parameters<typeof InspectorPane>[0]>) {
  const base: Parameters<typeof InspectorPane>[0] = {
    projectId: PROJECT_ID,
    clipId: CLIP_ID,
    baseUrl: "",
    activeTab: "transcript",
    onTabChange: () => {},
    fallbackSegments: [],
    currentTime: 0,
    duration: 10,
    onSeek: () => {},
    onSubtitleStateChange: () => {},
    boundToActiveClip: true,
    clipName: "剪辑成片 01",
    coverImageUrl: null,
    coverAssetId: null,
    coverRevision: 1,
    coverVersionLabel: "v1",
    coverStatusText: "原始成片封面",
    title: "标题",
    coverBusy: false,
    canUndoCover: false,
    coverUndoRevision: null,
    onRegenerateCover: () => {},
    onEditTitle: () => {},
    onShowCoverHistory: () => {},
    onAdoptCover: () => {},
    onUndoCover: () => {},
    coverHistory: [],
    coverHistoryVisible: false,
    coverHistoryLoading: false,
    adoptOptions: [],
    adoptSource: null,
    onAdoptSourceChange: () => {},
    transform: defaultTransform(),
    transformLocked: false,
    transformBackground: "blur",
    ratioLock: false,
    viewerZoom: 100,
    viewerAspect: "9:16",
    onTransformDraftChange: () => {},
    onTransformBackgroundChange: () => {},
    onRatioLockChange: () => {},
    onViewerZoomChange: () => {},
    onFitView: () => {},
    onOriginalRatio: () => {},
    onCanvasMode: () => {},
    onResetTransform: () => {},
    onViewerAspectChange: () => {},
    onApplyTransform: () => {},
    onCancelTransform: () => {},
    ...overrides,
  };
  return base;
}
