import { StrictMode } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { defaultTransform } from "@/lib/clip-transform";
import { InspectorPane } from "./inspector-pane";
import { TranscriptTab } from "./transcript-tab";
import { CLIP_ID, PROJECT_ID, configView, subtitleView, transcriptDoc } from "./test-utils";

const fallback = [{ segment_id: "fallback-1", start: 0, end: 1, text: "已加载旧分段" }];
const tabProps = { projectId: PROJECT_ID, clipId: CLIP_ID, baseUrl: "", fallbackSegments: fallback, onSeek: vi.fn() };
const loadedDoc = (text: string) => transcriptDoc({ approved_revision: 0, transcript_revision: 0,
  effective_segments: [{ segment_id: "seg-loaded", start: 0, end: 1, text }] });
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
function panePayload(url: string) {
  if (url.endsWith("/config")) return configView();
  if (url.endsWith("/subtitles")) return subtitleView();
  if (url.endsWith("/render-config")) return { config: configView().config, effective: [], sources: {}, export: { deferred: true } };
  return loadedDoc("StrictMode已读取");
}
afterEach(() => vi.unstubAllGlobals());

function paneProps(overrides: Partial<Parameters<typeof InspectorPane>[0]> = {}) {
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
  return props;
}

describe("Inspector读取生命周期", () => {
  it("真实StrictMode重放后200响应结束loading，成片修订v0保持原含义", async () => {
    const fetch = vi.fn(async () => json(loadedDoc("StrictMode已读取")));
    vi.stubGlobal("fetch", fetch);
    render(<StrictMode><TranscriptTab {...tabProps} /></StrictMode>);
    await screen.findByText("StrictMode已读取");
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("正在读取转写修订…")).not.toBeInTheDocument();
    expect(screen.getByTestId("inspector-transcript-revision")).toHaveTextContent("修订 v0");
  });

  it.each([403, 422])("StrictMode HTTP%s真实错误可见，不展示旧稿或承诺首版可写", async (status) => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ detail: status === 403 ? "E_FORBIDDEN: 当前对象没有访问权限" : "E_SOURCE_BINDING_UNSUPPORTED: 当前源片不支持转写编辑" }, status)));
    render(<StrictMode><TranscriptTab {...tabProps} /></StrictMode>);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(status === 403 ? "当前对象没有访问权限" : "当前源片不支持转写编辑"));
    expect(screen.queryByText("正在读取转写修订…")).not.toBeInTheDocument();
    expect(screen.queryByText("已加载旧分段")).not.toBeInTheDocument();
    expect(screen.queryByText(/仍然可以提交第一版/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /编辑分段/ })).not.toBeInTheDocument();
  });

  it("StrictMode pane配置/cues均到达；currentTime及新fallback数组不重复load", async () => {
    const fetch = vi.fn(async (input: RequestInfo | URL) => json(panePayload(String(input))));
    vi.stubGlobal("fetch", fetch);
    const props = paneProps({ fallbackSegments: fallback });
    const { rerender } = render(<StrictMode><InspectorPane {...props} /></StrictMode>);
    await screen.findByText("StrictMode已读取");
    await waitFor(() => expect(props.onPlayerVolumeChange).toHaveBeenCalled());
    await waitFor(() => expect(props.onSubtitleStateChange).toHaveBeenLastCalledWith(expect.objectContaining({ cues: subtitleView().cues })));
    expect(screen.queryByText("正在读取 clip 配置…")).not.toBeInTheDocument();
    const count = fetch.mock.calls.length;
    expect(fetch.mock.calls.filter(([url]) => String(url).includes("transcript/revisions"))).toHaveLength(2);
    for (let currentTime = 1; currentTime <= 5; currentTime++) {
      rerender(<StrictMode><InspectorPane {...props} currentTime={currentTime} fallbackSegments={[...fallback]} /></StrictMode>);
    }
    await act(async () => { await Promise.resolve(); });
    expect(fetch).toHaveBeenCalledTimes(count);
    expect(screen.getByText("StrictMode已读取")).toBeInTheDocument();
  });

  it("已有有效稿时fallback引用变化不重复fetch或有效稿回报", async () => {
    const fetch = vi.fn(async () => json(loadedDoc("稳定有效稿")));
    vi.stubGlobal("fetch", fetch);
    const onEffectiveSegments = vi.fn();
    const { rerender } = render(<StrictMode><TranscriptTab {...tabProps} onEffectiveSegments={onEffectiveSegments} /></StrictMode>);
    await screen.findByText("稳定有效稿");
    const count = fetch.mock.calls.length;onEffectiveSegments.mockClear();
    rerender(<StrictMode><TranscriptTab {...tabProps} fallbackSegments={[...fallback]} onEffectiveSegments={onEffectiveSegments} /></StrictMode>);
    await act(async () => { await Promise.resolve(); });
    expect(fetch).toHaveBeenCalledTimes(count);
    expect(onEffectiveSegments).not.toHaveBeenCalled();
  });

  it("卸载后迟到转写响应不回报有效稿或修改已卸载视图", async () => {
    const pending: ReturnType<typeof deferred<Response>>[] = [];
    vi.stubGlobal("fetch", vi.fn(() => { const response = deferred<Response>(); pending.push(response); return response.promise; }));
    const onEffectiveSegments = vi.fn();
    const { unmount, container } = render(<StrictMode><TranscriptTab {...tabProps} onEffectiveSegments={onEffectiveSegments} /></StrictMode>);
    unmount();onEffectiveSegments.mockClear();
    await act(async () => { pending.forEach((response) => response.resolve(json(loadedDoc("迟到旧稿")))); });
    expect(onEffectiveSegments).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });

  it("卸载后迟到pane配置/cues不触发播放器回调", async () => {
    const pending: { url: string; response: ReturnType<typeof deferred<Response>> }[] = [];
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => { const response = deferred<Response>(); pending.push({ url: String(input), response }); return response.promise; }));
    const props = paneProps();
    const { unmount, container } = render(<StrictMode><InspectorPane {...props} /></StrictMode>);
    unmount();vi.mocked(props.onSubtitleStateChange).mockClear();vi.mocked(props.onPlayerVolumeChange!).mockClear();
    await act(async () => { pending.forEach(({ url, response }) => response.resolve(json(panePayload(url)))); });
    expect(props.onSubtitleStateChange).not.toHaveBeenCalled();
    expect(props.onPlayerVolumeChange).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });

  it("切换clip后A迟到响应不能覆盖B", async () => {
    const old: ReturnType<typeof deferred<Response>>[] = [];
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes(CLIP_ID)) { const response = deferred<Response>(); old.push(response); return response.promise; }
      return Promise.resolve(json(loadedDoc("B当前稿件")));
    }));
    const { rerender } = render(<StrictMode><TranscriptTab {...tabProps} /></StrictMode>);
    rerender(<StrictMode><TranscriptTab {...tabProps} clipId="clip-other" /></StrictMode>);
    await screen.findByText("B当前稿件");
    await act(async () => { old.forEach((response) => response.resolve(json(loadedDoc("A迟到稿件")))); });
    expect(screen.getByText("B当前稿件")).toBeInTheDocument();
    expect(screen.queryByText("A迟到稿件")).not.toBeInTheDocument();
  });

  it("切换variant后旧请求的422不能覆盖已成功有效稿", async () => {
    const old: ReturnType<typeof deferred<Response>>[] = [];
    let rawRequested = false;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      if (!rawRequested) { const response = deferred<Response>(); old.push(response); return response.promise; }
      return Promise.resolve(json(transcriptDoc({ raw_segments: [{ segment_id: "raw-current", start: 0, end: 1, text: "原始稿已读取" }] })));
    }));
    render(<StrictMode><TranscriptTab {...tabProps} /></StrictMode>);
    rawRequested = true;fireEvent.click(screen.getByTestId("inspector-variant-raw"));
    await screen.findByText("原始稿已读取");
    await act(async () => { old.forEach((response) => response.resolve(json({ detail: "旧请求失败" }, 422))); });
    expect(screen.getByText("原始稿已读取")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("字幕接口422在pane显式显示真实原因", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => String(input).endsWith("/subtitles")
      ? json({ detail: "E_SOURCE_BINDING_UNSUPPORTED: 源片不支持字幕配置" }, 422) : json(panePayload(String(input)))));
    render(<StrictMode><InspectorPane {...paneProps()} /></StrictMode>);
    await waitFor(() => expect(screen.getByTestId("inspector-subtitles-error")).toHaveTextContent("源片不支持字幕配置"));
  });
  it.each([404, 422])("HTTP%s缺基线先返回，后到三段无ID SRT只读显示且seek可用，不重发GET", async (status) => {
    const fetch = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => String(input).includes("transcript/revisions")
      ? json({ detail: "E_TRANSCRIPT_NOT_SEEDED: 还没有转写文档，请先 POST 转写基线或传 seed_segments" }, status) : json(panePayload(String(input))));
    vi.stubGlobal("fetch", fetch);
    const props = paneProps({ fallbackSegments: [] });
    const { rerender } = render(<StrictMode><InspectorPane {...props} /></StrictMode>);
    await screen.findByTestId("inspector-transcript-fallback");
    expect(screen.queryByTestId("inspector-subtitle-0")).not.toBeInTheDocument();
    const srt = [
      { start: 0, end: 1, text: "后到字幕第一段" },
      { start: 1, end: 2, text: "后到字幕第二段" },
      { start: 2, end: 3, text: "后到字幕第三段" },
    ];
    const before = JSON.stringify(srt);
    const count = fetch.mock.calls.length;
    rerender(<StrictMode><InspectorPane {...props} fallbackSegments={srt} currentTime={1} /></StrictMode>);
    await screen.findByText("后到字幕第三段");
    expect(screen.getAllByTestId(/^inspector-subtitle-/)).toHaveLength(3);
    expect(screen.getByTestId("inspector-transcript-fallback")).toHaveTextContent("尚无可编辑的成片修订记录；请在源片的核听与人工修订入口编辑");
    expect(screen.getByTestId("inspector-transcript-fallback")).not.toHaveTextContent(/POST|seed_segments/);
    expect(screen.queryByRole("button", { name: /编辑分段/ })).not.toBeInTheDocument();
    screen.getAllByRole("combobox").forEach((input) => expect(input).toBeDisabled());
    fireEvent.click(screen.getByTestId("inspector-subtitle-1"));
    expect(props.onSeek).toHaveBeenCalledWith(1);
    rerender(<StrictMode><InspectorPane {...props} fallbackSegments={[...srt]} currentTime={2} /></StrictMode>);
    await act(async () => { await Promise.resolve(); });
    expect(fetch).toHaveBeenCalledTimes(count);
    expect(JSON.stringify(srt)).toBe(before);
    expect(fetch.mock.calls.every(([, init]) => !init || init.method !== "POST")).toBe(true);
    expect(screen.getByTestId("inspector-transcript-revision")).toHaveTextContent("修订 v0");
  });

  it("404 fallback即使带ID也不冒充服务端可编辑文档", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ detail: "还没有成片修订文档" }, 404)));
    render(<StrictMode><TranscriptTab {...tabProps} /></StrictMode>);
    await screen.findByText("已加载旧分段");
    expect(screen.queryByRole("button", { name: /编辑分段/ })).not.toBeInTheDocument();
    expect(screen.getByRole("combobox")).toBeDisabled();
    expect(screen.queryByText(/提交第一版/)).not.toBeInTheDocument();
  });

  it("真实有ID的v0文档仍可编辑，POST沿用该真实ID并返回成片修订v1", async () => {
    const posts: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        posts.push(JSON.parse(String(init.body)));
        return json(transcriptDoc({ approved_revision: 1, transcript_revision: 1,
          effective_segments: [{ segment_id: "seg-loaded", start: 0, end: 1, text: "文档已修订" }] }));
      }
      return json(loadedDoc("真实文档原文"));
    }));
    render(<StrictMode><TranscriptTab {...tabProps} /></StrictMode>);
    await screen.findByText("真实文档原文");
    fireEvent.click(screen.getByRole("button", { name: "编辑分段 seg-loaded" }));
    fireEvent.change(screen.getByRole("textbox", { name: "分段文本 seg-loaded" }), { target: { value: "文档已修订" } });
    fireEvent.click(screen.getByRole("button", { name: "保存修订" }));
    await screen.findByText("文档已修订");
    expect(posts).toHaveLength(1);
    expect(posts[0]).toMatchObject({ changes: [{ segment_id: "seg-loaded", before: "真实文档原文", after: "文档已修订" }] });
    expect(screen.getByTestId("inspector-transcript-revision")).toHaveTextContent("修订 v1");
  });

  it.each([
    { status: 403, detail: "E_TRANSCRIPT_NOT_SEEDED: 权限不足，不能读取基线" },
    { status: 422, detail: "E_TRANSCRIPT_SEED_INVALID: 基线数据非法" },
    { status: 422, detail: "还没有转写文档，但未返回专用错误码" },
  ])("HTTP$status/$detail不进入只读fallback", async ({ status, detail }) => {
    const fetch = vi.fn(async () => json({ detail }, status));
    vi.stubGlobal("fetch", fetch);
    const { rerender } = render(<StrictMode><TranscriptTab {...tabProps} fallbackSegments={[]} /></StrictMode>);
    await screen.findByRole("alert");
    const count = fetch.mock.calls.length;
    rerender(<StrictMode><TranscriptTab {...tabProps} fallbackSegments={[{ start: 0, end: 1, text: "不能显示的旧字幕" }]} /></StrictMode>);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByRole("alert")).toHaveTextContent("读取转写失败");
    expect(screen.queryByText("不能显示的旧字幕")).not.toBeInTheDocument();
    expect(screen.queryByText(/当前显示成片字幕/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /编辑分段/ })).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(count);
  });

});
