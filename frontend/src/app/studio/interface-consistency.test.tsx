import { StrictMode, type RefObject, type ReactEventHandler } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import StudioPage from "./page";
import { loadSourceWorkspace } from "./source-workspace";
import { configView, renderConfigView, subtitleView } from "@/components/studio/inspector/test-utils";
import type { ClipTransform } from "@/lib/clip-transform";

const pageControls = vi.hoisted(() => ({ openProject: null as ((id: string) => void) | null }));
// 与同目录其它整页用例一致：jsdom 不处理真实样式，studio.css 走 Tailwind v4 PostCSS，
// 不 mock 会在 vite:css 加载阶段直接失败（补齐于 2026-10-19 项目管理弹层窗口）。
vi.mock("./studio.css", () => ({}));
// 现产品只有挂载时显式URL入口；观测真实useCallback以调用同一打开函数，不添加产品测试入口。
vi.mock("react", async (original) => {
  const react = await original<typeof import("react")>();
  return { ...react, useCallback: (callback: (...args: never[]) => unknown, deps: import("react").DependencyList) => {
    const result = react.useCallback(callback, deps);
    if (String(callback).includes("已打开历史项目")) pageControls.openProject = result as unknown as (id: string) => void;
    return result;
  } };
});
// 页面与真实 Inspector/TransformPanel 联动；仅隔离画布媒体播放和无关协作面板。
vi.mock("./studio-copilot", () => ({ StudioCopilot: () => null }));
vi.mock("@/lib/studio-upload", () => ({ studioUploadBinding: () => ({ uploadId: "synthetic-only" }), uploadStudioVideo: async () => ({ job_id: "old-A", object_key: "local:synthetic-source" }) }));
vi.mock("@/components/studio/transform-stage", () => ({ TransformStage: (p: {
  videoUrl: string | null; videoRef: RefObject<HTMLVideoElement>; transform: ClipTransform;
  background: string; onTimeUpdate?: ReactEventHandler<HTMLVideoElement>;
}) => <video ref={p.videoRef} src={p.videoUrl || undefined} onTimeUpdate={p.onTimeUpdate} data-testid="player" data-scale={p.transform.scale} data-background={p.background} /> }));

const pid = "timeline_fixture_one";
const bid = "timeline_fixture_two";
const credential = { project_id: pid, object_key: "local:synthetic-source", transcript_id: "tr-fixture", transcript_version: 2 };
const document = { ...credential, source_project_id: "timeline_original_registration", asr_segments: [{ start: 0, end: 2, text: "有效人工版本二" }], raw_segments: [{ start: 0, end: 2, text: "原始识别" }] };
const credentialB = { ...credential, project_id: bid, object_key: "local:synthetic-B", transcript_id: "tr-B" };
const documentB = { ...document, ...credentialB, asr_segments: [{ start: 0, end: 2, text: "项目B有效稿" }] };
const project = { id: pid, source: { name: "合成原片.mp4", duration: 10, width: 1080, height: 1920 }, timeline: { ranges: [{ id: "range-a", source_start: 0, source_end: 5 }, { id: "range-b", source_start: 5, source_end: 10 }] }, transcript: [{ start: 0, end: 1, text: "过期内嵌稿" }], artifacts: { edl: "edit/edl.json", srt: "timeline.srt", fcpxml: "timeline.fcpxml" } };
const cards = ["clip01", "clip02"].map((id) => ({ clip_id: id, name: `合成${id}`, title: null, topic: "合成", keywords: [], duration: 3, source_start: 0, source_end: 3, task_id: "a".repeat(32), render_task_id: "a".repeat(32), render_revision: 1, status: "completed", video_path: `output/${id}.mp4`, cover: null, task_history: [] }));
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
function deferred<T>() { let resolve!: (v: T) => void; const promise = new Promise<T>((r) => { resolve = r; }); return { promise, resolve }; }
let refStatus: number, revisionStatus: number, configStatus: number, sidecarStatus: number;
let revisionWait: ReturnType<typeof deferred<Response>> | null;
let configWait: ReturnType<typeof deferred<Response>> | null;
let initialTimelineWait: ReturnType<typeof deferred<Response>> | null;
let timelineReads: number;
let savedScale: number;
let sourcePostWait: ReturnType<typeof deferred<Response>> | null;
let workflowWait: ReturnType<typeof deferred<Response>> | null;
let missingProject: string | null;
let resolverWaitA: ReturnType<typeof deferred<Response>> | null;
let resolverWaitB: ReturnType<typeof deferred<Response>> | null;
let requests: { url: string; method: string; body?: unknown }[];
let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  window.history.replaceState({}, "", `/studio?source_id=${pid}`);
  refStatus = revisionStatus = configStatus = sidecarStatus = 200;
  revisionWait = configWait = initialTimelineWait = sourcePostWait = workflowWait = resolverWaitA = resolverWaitB = null; requests = []; timelineReads = 0; savedScale = 1; pageControls.openProject = null; missingProject = null;
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input), method = init?.method || "GET";
    requests.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (method === "POST" && url.endsWith("/api/studio/transcripts/revisions")) return sourcePostWait ? sourcePostWait.promise : json({ document });
    if (url.endsWith("/api/jobs/old-A")) return workflowWait ? workflowWait.promise : json({ status: "completed", artifacts: [] });
    if (method === "PUT" && url.endsWith("/config")) {
      if (configWait) return configWait.promise;
      if (configStatus === 200) savedScale = (JSON.parse(String(init?.body)).transform?.scale as number | undefined) || 1;
      return json(configStatus === 200 ? configView() : { detail: "禁止保存配置" }, configStatus);
    }
    if (method === "PUT" && url.endsWith("/transform")) return json({ detail: "合成同步结果" }, sidecarStatus);
    if (url.includes("/api/studio/transcripts/revisions?") && url.includes("transcript_id=tr-B")) return json({ document: documentB });
    if (url.includes("/api/studio/transcripts/revisions?")) return revisionWait ? revisionWait.promise : json(revisionStatus === 200 ? { document } : { detail: "指定版本不可读取" }, revisionStatus);
    if (url.endsWith(`/studio/projects/${bid}/transcript-ref`)) return resolverWaitB ? resolverWaitB.promise.then((r) => r.clone()) : missingProject === bid ? json({ detail: "当前项目B没有转写" }, 404) : json(credentialB);
    if (url.endsWith(`/studio/projects/${pid}/transcript-ref`) && resolverWaitA) return resolverWaitA.promise.then((r) => r.clone());
    if (url.endsWith("/studio/projects")) return json([{ project_id: pid }]);
    if (url.endsWith("/transcript-ref")) return json(refStatus === 200 ? credential : { detail: { code: "E_TRANSCRIPT_NOT_FOUND", message: "没有已纠错转写" } }, refStatus);
    if (url.endsWith(`/studio/timeline/${pid}`)) { timelineReads += 1; return initialTimelineWait && timelineReads === 1 ? initialTimelineWait.promise : json(project); }
    if (url.endsWith(`/studio/timeline/${bid}`)) return json({ ...project, id: bid, source: { ...project.source, name: "项目B原片.mp4" }, transcript: [] });
    if (url.endsWith(`/studio/projects/${bid}/clips`)) return json({ project_id: bid, cards: cards.map((c) => ({ ...c, name: `B${c.clip_id}` })), card_count: cards.length });
    if (url.endsWith(`/studio/projects/${pid}/clips`)) return json({ project_id: pid, cards, card_count: cards.length });
    if (url.endsWith("/config")) { const view = configView(); view.config.transform = { ...view.config.transform!, scale: savedScale }; return json(view); }
    if (url.endsWith("/subtitles")) return json(subtitleView());
    if (url.endsWith("/render-config")) return json(renderConfigView());
    if (url.includes("/transcript/revisions")) return json({ detail: "E_TRANSCRIPT_NOT_SEEDED: 无成片文档" }, 422);
    if (url.endsWith("/studio/workflow")) return json({ banner: { label: "合成规则" }, summary: { skills: [] } });
    if (url.includes("/history")) return json({ detail: "无合成历史" }, 404);
    return json({ artifacts: [], covers: [], tasks: [] });
  });
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("URL", class extends URL { static createObjectURL() { return "blob:synthetic"; } static revokeObjectURL() {} });
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
});
afterEach(async () => { cleanup(); await Promise.resolve(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
async function openClip(id = "clip01") {
  const label = await screen.findByText(`合成${id}`, { selector: ".asset-card strong" });
  fireEvent.click(label.closest("article")!);
  await waitFor(() => expect(label.closest("article")).toHaveClass("active"));
}
async function coverTab() {
  // 2026-10-21：封面页只保留封面图片与版型卡片，画面/裁切重构图整体移到「效果」页；
  // 这里对账的 transform/view/config PUT 契约不变，只是入口换了标签。
  fireEvent.click(screen.getByRole("tab", { name: "效果设置" }));
  await screen.findByRole("button", { name: "放大片段" });
  await act(async () => { await Promise.resolve(); });
}
// active-clip（2026-10-21 切片选择）是选中新通道，与这里对账的 transform/view/config PUT 无关，
// 排除后既有「PUT 次数」断言保持原语义。
const puts = () => requests.filter((r) => r.method === "PUT" && !r.url.endsWith("/active-clip"));
const exportHref = () => documentNode().querySelector<HTMLAnchorElement>(".rendered-card.active a[download], .rendered-card.active a.rendered-export, .rendered-card.active > a")?.getAttribute("href");
const documentNode = () => window.document;

describe("显式返回原片", () => {
  it("bare studio保持空白，不自动读取任何项目历史", async () => {
    window.history.replaceState({}, "", "/studio"); render(<StrictMode><StudioPage /></StrictMode>);
    await act(async () => { await Promise.resolve(); });
    expect(requests.some((r) => /\/studio\/(timeline|projects)\//.test(r.url))).toBe(false);
  });
  it("刷新后返回本项目原片与指定v2有效稿，不伪造File或取旧内嵌稿", async () => {
    render(<StrictMode><StudioPage /></StrictMode>); await openClip();
    fireEvent.click(screen.getByRole("button", { name: "返回原片" }));
    await waitFor(() => expect(screen.getByTestId("player")).toHaveAttribute("src", `/video-api/studio/timeline/${pid}/source`));
    expect(screen.getByText("原始素材（1）")).toBeInTheDocument();
    expect(screen.getByText("有效人工版本二", { selector: ".transcript-text" })).toBeInTheDocument();
    expect(screen.queryByText("过期内嵌稿", { selector: ".transcript-text" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "核听与人工修订" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "开始转写" })).not.toBeInTheDocument();
    expect(requests.find((r) => r.url.includes("/api/studio/transcripts/revisions?"))?.url).toContain("transcript_version=2");
    // 三态播放器：返回原片前幂等触发预览代理（native→原片直通；ready→/preview；缺代理回退 /source）。
    expect(requests.some((r) => r.url.includes("/preview") && r.method === "POST")).toBe(true);
    expect(puts()).toHaveLength(0);
  });
  it.each([403, 422])("revision HTTP%s真实错误可见且保留成片", async (status) => {
    revisionStatus = status; render(<StudioPage />); await openClip();
    const before = screen.getByTestId("player").getAttribute("src");
    fireEvent.click(screen.getByRole("button", { name: "返回原片" }));
    expect(await screen.findByTestId("source-return-error")).toHaveTextContent("指定版本不可读取");
    expect(screen.getByTestId("player")).toHaveAttribute("src", before!);
    expect(screen.getByRole("button", { name: "返回原片" })).toBeEnabled();
  });
  it.each(["ref", "revision"])("404缺%s清凭据，仅恢复只读源片", async (missing) => {
    if (missing === "ref") refStatus = 404; else revisionStatus = 404;
    render(<StudioPage />); await openClip(); fireEvent.click(screen.getByRole("button", { name: "返回原片" }));
    await waitFor(() => expect(screen.getByTestId("player")).toHaveAttribute("src", `/video-api/studio/timeline/${pid}/source`));
    expect(screen.queryByRole("button", { name: "核听与人工修订" })).not.toBeInTheDocument();
    expect(screen.getAllByText("原片已恢复，尚无可用转写版本；当前仅可查看原片。")[0]).toBeInTheDocument();
    expect(screen.queryByText("过期内嵌稿", { selector: ".transcript-text" })).not.toBeInTheDocument();
  });
  it("返回请求迟到后已切另一成片，不覆盖新绑定", async () => {
    revisionWait = deferred(); render(<StudioPage />); await openClip(); fireEvent.click(screen.getByRole("button", { name: "返回原片" }));
    await waitFor(() => expect(requests.some((r) => r.url.includes("/api/studio/transcripts/revisions?"))).toBe(true));
    await openClip("clip02"); const before = screen.getByTestId("player").getAttribute("src");
    await act(async () => { revisionWait!.resolve(json({ document })); await Promise.resolve(); });
    expect(screen.getByTestId("player")).toHaveAttribute("src", before!);
    expect(screen.getByRole("button", { name: "返回原片" })).toBeInTheDocument();
  });
  it("helper拒绝错project凭据和不匹配revision", async () => {
    fetchMock.mockImplementationOnce(async () => json(project)).mockImplementationOnce(async () => json({ ...credential, project_id: "timeline_wrong" }));
    await expect(loadSourceWorkspace("/video-api", pid)).rejects.toThrow("不属于当前项目");
    fetchMock.mockImplementationOnce(async () => json(project)).mockImplementationOnce(async () => json(credential)).mockImplementationOnce(async () => json({ document: { ...document, transcript_version: 3 } }));
    await expect(loadSourceWorkspace("/video-api", pid)).rejects.toThrow("指定转写版本不匹配");
  });
  it("首次历史timeline迟到不能覆盖返回后的指定有效稿", async () => {
    initialTimelineWait = deferred(); render(<StudioPage />); await openClip();
    fireEvent.click(screen.getByRole("button", { name: "返回原片" }));
    await screen.findByText("有效人工版本二", { selector: ".transcript-text" });
    await act(async () => { initialTimelineWait!.resolve(json(project)); await Promise.resolve(); });
    expect(screen.getByText("有效人工版本二", { selector: ".transcript-text" })).toBeInTheDocument();
    expect(screen.queryByText("过期内嵌稿", { selector: ".transcript-text" })).not.toBeInTheDocument();
  });
  it("同project缓存往返复用指定版本，无重复恢复GET", async () => {
    render(<StudioPage />); await openClip(); fireEvent.click(screen.getByRole("button", { name: "返回原片" }));
    await screen.findByText("有效人工版本二", { selector: ".transcript-text" });
    const count = requests.filter((r) => r.url.includes("/api/studio/transcripts/revisions?")).length;
    await openClip("clip02"); fireEvent.click(screen.getByRole("button", { name: "返回原片" }));
    await screen.findByText("有效人工版本二", { selector: ".transcript-text" });
    expect(requests.filter((r) => r.url.includes("/api/studio/transcripts/revisions?"))).toHaveLength(count);
  });
  it("卸载后返回请求迟到不恢复到新页面", async () => {
    revisionWait = deferred(); const view = render(<StudioPage />); await openClip(); fireEvent.click(screen.getByRole("button", { name: "返回原片" }));
    await waitFor(() => expect(requests.some((r) => r.url.includes("/api/studio/transcripts/revisions?"))).toBe(true));
    view.unmount(); window.history.replaceState({}, "", "/studio"); render(<StudioPage />);
    await act(async () => { revisionWait!.resolve(json({ document })); await Promise.resolve(); });
    expect(screen.getByTestId("player")).not.toHaveAttribute("src");
    expect(screen.queryByText("原始素材（1）")).not.toBeInTheDocument();
  });
});

describe("真实页面与Inspector草稿联动", () => {
  it("预览缩放等待超过700ms零写，Apply才保存并更新导出", async () => {
    render(<StrictMode><StudioPage /></StrictMode>); await openClip(); await coverTab(); const before = exportHref();
    fireEvent.click(screen.getByRole("button", { name: "放大片段" }));
    expect(screen.getByRole("button", { name: "应用" })).toBeEnabled();
    expect(screen.getByTestId("player")).toHaveAttribute("data-scale", "1.15");
    await act(async () => { await new Promise((r) => setTimeout(r, 780)); });
    expect(puts()).toHaveLength(0); expect(exportHref()).toBe(before);
    fireEvent.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(screen.getByTestId("inspector-transform-state")).toHaveTextContent("已保存"));
    expect(puts().map((r) => r.url.endsWith("/config") ? "config" : "transform")).toEqual(["config", "transform"]);
    expect(screen.getByRole("button", { name: "应用" })).toBeDisabled();
    expect(exportHref()).not.toBe(before);
  });
  it.each(["cancel", "escape", "switch"])("%s清理草稿且不产生持久写", async (action) => {
    render(<StudioPage />); await openClip(); await coverTab(); fireEvent.click(screen.getByRole("button", { name: "放大片段" }));
    if (action === "cancel") fireEvent.click(screen.getByRole("button", { name: "取消" }));
    if (action === "escape") fireEvent.keyDown(window, { key: "Escape" });
    if (action === "switch") { await openClip("clip02"); await coverTab(); }
    await waitFor(() => expect(screen.getByRole("button", { name: "应用" })).toBeDisabled());
    expect(screen.getByTestId("player")).toHaveAttribute("data-scale", "1"); expect(puts()).toHaveLength(0);
  });
  it("config失败不写sidecar、不更新applied导出且保留dirty", async () => {
    configStatus = 403; render(<StudioPage />); await openClip(); await coverTab(); const before = exportHref();
    fireEvent.click(screen.getByRole("button", { name: "放大片段" })); fireEvent.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(screen.getByTestId("inspector-transform-state")).toHaveTextContent("禁止保存配置"));
    expect(puts()).toHaveLength(1); expect(exportHref()).toBe(before);
    expect(screen.getByRole("button", { name: "应用" })).toBeEnabled();
  });
  it("第二PUT失败明确部分保存，保留同draft可重试，成功才更新导出", async () => {
    sidecarStatus = 503; render(<StudioPage />); await openClip(); await coverTab(); const before = exportHref();
    fireEvent.click(screen.getByRole("button", { name: "放大片段" })); fireEvent.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(screen.getByTestId("inspector-transform-state")).toHaveTextContent("部分保存：片段配置已写入，渲染变换未完成"));
    expect(exportHref()).toBe(before); expect(screen.getByRole("button", { name: "应用" })).toBeEnabled();
    sidecarStatus = 200; fireEvent.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(screen.getByTestId("inspector-transform-state")).toHaveTextContent("已保存"));
    expect(puts()).toHaveLength(4); expect(puts()[0].body).toEqual(puts()[2].body); expect(exportHref()).not.toBe(before);
  });
  it("A的迟到Apply不吞B的draft或成功状态", async () => {
    configWait = deferred(); render(<StudioPage />); await openClip(); await coverTab();
    fireEvent.click(screen.getByRole("button", { name: "放大片段" })); fireEvent.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(puts()).toHaveLength(1)); await openClip("clip02"); await coverTab();
    fireEvent.click(screen.getByRole("button", { name: "放大片段" }));
    await act(async () => { configWait!.resolve(json(configView())); await Promise.resolve(); });
    expect(screen.getByRole("button", { name: "应用" })).toBeEnabled();
    expect(screen.queryByTestId("inspector-transform-state")).not.toBeInTheDocument();
    expect(screen.getByTestId("player")).toHaveAttribute("data-scale", "1.15");
  });
  it.each(["原画", "铺满画布", "重置"])("快捷动作%s遵循草稿而非自动保存", async (name) => {
    render(<StudioPage />); await openClip(); await coverTab();
    fireEvent.click(screen.getByRole("button", { name: "放大片段" }));
    if (name === "铺满画布") fireEvent.click(within(screen.getByRole("group", { name: "片段快速设置" })).getByRole("button", { name: "原画" }));
    fireEvent.click(within(screen.getByRole("group", { name: "片段快速设置" })).getByRole("button", { name }));
    await act(async () => { await new Promise((r) => setTimeout(r, 750)); });
    expect(puts()).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.getByTestId("player")).toHaveAttribute("data-scale", "1");
  });
  it("背景只预览、取消恢复，Apply全成功才更新导出参数", async () => {
    render(<StudioPage />); await openClip(); await coverTab();
    fireEvent.click(within(screen.getByRole("group", { name: "片段快速设置" })).getByRole("button", { name: "原画" }));
    fireEvent.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(screen.getByTestId("inspector-transform-state")).toHaveTextContent("已保存"));
    expect(screen.getByTestId("inspector-background-scope")).toHaveTextContent("背景应用于本次工作区导出，刷新后恢复默认");
    const before = exportHref(), count = puts().length;
    fireEvent.click(screen.getByRole("button", { name: "纯色" }));
    expect(screen.getByRole("button", { name: "应用" })).toBeEnabled();
    expect(screen.getByTestId("player")).toHaveAttribute("data-background", "color");
    await act(async () => { await new Promise((r) => setTimeout(r, 750)); });
    expect(exportHref()).toBe(before); expect(puts()).toHaveLength(count);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.getByTestId("player")).toHaveAttribute("data-background", "blur");
    expect(screen.getByRole("button", { name: "应用" })).toBeDisabled();
    sidecarStatus = 503;
    fireEvent.click(screen.getByRole("button", { name: "纯色" })); fireEvent.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(screen.getByTestId("inspector-transform-state")).toHaveTextContent("部分保存"));
    expect(exportHref()).toBe(before); expect(screen.getByRole("button", { name: "应用" })).toBeEnabled();
    sidecarStatus = 200; fireEvent.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(screen.getByTestId("inspector-transform-state")).toHaveTextContent("已保存"));
    expect(exportHref()).toContain("background=color"); expect(puts()).toHaveLength(count + 4);
  });
  it.each(["escape", "switch"])("背景%s清预览且不改变已应用导出", async (action) => {
    render(<StudioPage />); await openClip(); await coverTab(); fireEvent.click(within(screen.getByRole("group", { name: "片段快速设置" })).getByRole("button", { name: "原画" }));
    fireEvent.click(screen.getByRole("button", { name: "纯色" }));
    if (action === "escape") fireEvent.keyDown(window, { key: "Escape" }); else { await openClip("clip02"); await coverTab(); }
    expect(screen.getByTestId("player")).toHaveAttribute("data-background", "blur");
    expect(exportHref()).toContain("background=blur"); expect(puts()).toHaveLength(0);
  });
  it("原片同数值range切换清draft，不串到下一片段", async () => {
    render(<StudioPage />); await openClip(); fireEvent.click(screen.getByRole("button", { name: "返回原片" }));
    await waitFor(() => expect(screen.getByTestId("player")).toHaveAttribute("src", `/video-api/studio/timeline/${pid}/source`));
    await coverTab(); fireEvent.click(screen.getByRole("button", { name: "放大片段" }));
    fireEvent.timeUpdate(screen.getByTestId("player"), { target: { currentTime: 6 } });
    await waitFor(() => expect(screen.getByRole("button", { name: "应用" })).toBeDisabled());
    expect(screen.getByTestId("player")).toHaveAttribute("data-scale", "1"); expect(puts()).toHaveLength(0);
  });
  it("刷新读取已保存transform同步播放器和导出，background沿既有默认边界", async () => {
    const view = render(<StudioPage />); await openClip(); await coverTab();
    fireEvent.click(within(screen.getByRole("group", { name: "片段快速设置" })).getByRole("button", { name: "原画" }));
    fireEvent.click(screen.getByRole("button", { name: "放大片段" })); fireEvent.click(screen.getByRole("button", { name: "纯色" }));
    fireEvent.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(screen.getByTestId("inspector-transform-state")).toHaveTextContent("已保存"));
    expect(exportHref()).toContain("background=color");
    view.unmount(); const count = puts().length; render(<StudioPage />); await openClip(); await coverTab();
    await waitFor(() => expect(screen.getByTestId("player")).toHaveAttribute("data-scale", "1.15"));
    expect(screen.getByRole("button", { name: "应用" })).toBeDisabled();
    expect(exportHref()).toContain("background=blur"); expect(puts()).toHaveLength(count);
  });
  it("Apply在途锁取消、关闭和Escape，完成后才可开始新草稿", async () => {
    configWait = deferred(); render(<StudioPage />); await openClip(); await coverTab();
    fireEvent.click(screen.getByRole("button", { name: "放大片段" })); fireEvent.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(screen.getByTestId("inspector-transform-state")).toHaveTextContent("提交尚未完成"));
    expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "退出裁剪变换" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "取消" })); fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.getByTestId("inspector-transform-state")).toHaveTextContent("提交尚未完成");
    expect(puts()).toHaveLength(1);
    await act(async () => { configWait!.resolve(json(configView())); await Promise.resolve(); });
    await waitFor(() => expect(screen.getByTestId("inspector-transform-state")).toHaveTextContent("已保存"));
    fireEvent.click(screen.getByRole("button", { name: "放大片段" }));
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByRole("button", { name: "应用" })).toBeEnabled();
    expect(Number(screen.getByTestId("player").getAttribute("data-scale"))).toBeCloseTo(1.3225);
    expect(puts()).toHaveLength(2);
  });
});

describe("项目切换代际与重新剪辑来源", () => {
  async function sourceA() {
    await openClip(); fireEvent.click(screen.getByRole("button", { name: "返回原片" }));
    await screen.findByText("有效人工版本二", { selector: ".transcript-text" });
  }
  async function switchB() {
    expect(pageControls.openProject).toBeTypeOf("function");
    act(() => pageControls.openProject!(bid));
    await screen.findByText("Bclip01", { selector: ".asset-card strong" });
  }
  async function sourceB() {
    const label = await screen.findByText("Bclip01", { selector: ".asset-card strong" }); fireEvent.click(label.closest("article")!);
    fireEvent.click(screen.getByRole("button", { name: "返回原片" }));
    await screen.findByText("项目B有效稿", { selector: ".transcript-text" });
  }
  it("A核听GET迟到，打开B后不写B文档或指定版本", async () => {
    render(<StudioPage />); await sourceA(); revisionWait = deferred();
    fireEvent.click(screen.getByRole("button", { name: "核听与人工修订" }));
    await waitFor(() => expect(requests.filter((r) => r.url.includes("/api/studio/transcripts/revisions?")).length).toBe(2));
    await switchB(); await sourceB();
    await act(async () => { revisionWait!.resolve(json({ document: { ...document, transcript_version: 3, asr_segments: [{ start: 0, end: 2, text: "A迟到核听稿" }] } })); await Promise.resolve(); });
    fireEvent.click(screen.getByRole("button", { name: "核听与人工修订" }));
    await waitFor(() => expect(requests.filter((r) => r.url.includes("transcript_id=tr-B")).length).toBe(2));
    expect(requests.filter((r) => r.url.includes("transcript_id=tr-B")).at(-1)?.url).toContain("transcript_version=2");
    expect(screen.getByText("项目B有效稿", { selector: ".transcript-text" })).toBeInTheDocument();
    expect(screen.queryByText("A迟到核听稿")).not.toBeInTheDocument();
  });
  it("A核听POST迟到不回写B segments/ref/版本", async () => {
    render(<StudioPage />); await sourceA(); fireEvent.click(screen.getByRole("button", { name: "核听与人工修订" }));
    const input = await screen.findByRole("textbox", { name: "修订第1段" });
    await waitFor(() => expect(screen.getByRole("button", { name: "核对指定版本" })).toBeEnabled());
    sourcePostWait = deferred(); fireEvent.change(input, { target: { value: "A提交的新文本" } });
    fireEvent.click(screen.getByRole("button", { name: "保存人工核听新版本" }));
    await waitFor(() => expect(requests.some((r) => r.method === "POST" && r.url.endsWith("/api/studio/transcripts/revisions"))).toBe(true));
    await switchB(); await sourceB();
    await act(async () => { sourcePostWait!.resolve(json({ document: { ...document, transcript_version: 3, asr_segments: [{ start: 0, end: 2, text: "A迟到保存稿" }] } })); await Promise.resolve(); });
    expect(screen.getByText("项目B有效稿", { selector: ".transcript-text" })).toBeInTheDocument();
    expect(screen.queryByText("A迟到保存稿", { selector: ".transcript-text" })).not.toBeInTheDocument();
    expect(screen.getByTestId("player")).toHaveAttribute("src", `/video-api/studio/timeline/${bid}/source`);
  });
  it("A旧workflow轮询迟到不接回A、不读旧artifact且不自动cancel/delete后台任务", async () => {
    render(<StudioPage />); workflowWait = deferred();
    fireEvent.change(window.document.querySelector('input[type="file"][accept="video/*"]')!, { target: { files: [new File(["synthetic"], "oldA.mp4", { type: "video/mp4" })] } });
    fireEvent.click(screen.getByRole("button", { name: "开始转写" }));
    await waitFor(() => expect(requests.some((r) => r.url.endsWith("/api/jobs/old-A"))).toBe(true), { timeout: 4000 });
    await switchB();
    await act(async () => { workflowWait!.resolve(json({ status: "completed", project_id: pid, artifacts: [{ kind: "transcript_json", url: "/old-A-transcript.json" }] })); await Promise.resolve(); });
    expect(screen.getByText("Bclip01", { selector: ".asset-card strong" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "当前工作流任务" })).not.toBeInTheDocument();
    expect(requests.some((r) => r.url.includes("old-A-transcript.json") || r.method === "DELETE" || /\/cancel$/.test(r.url))).toBe(false);
  });
  it("B404不能扫描全projects借A凭据，明确显示当前项目缺转写", async () => {
    render(<StudioPage />); await sourceA(); missingProject = bid; await switchB();
    fireEvent.click(screen.getByRole("button", { name: "重新剪辑并替换旧切片" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "确认重新剪辑" })).toBeDisabled());
    await waitFor(() => expect(screen.getByText(/未找到该项目的已纠错转写/)).toBeInTheDocument());
    expect(requests.some((r) => r.url.endsWith("/studio/projects"))).toBe(false);
    const afterB = requests.filter((r) => r.url.endsWith("/transcript-ref")).slice(-2);
    expect(afterB.every((r) => r.url.includes(bid))).toBe(true);
  });
  it("A resolver晚返不能解锁B确认或借旧modal/session凭据提交", async () => {
    render(<StudioPage />); await sourceA(); resolverWaitA = deferred(); resolverWaitB = deferred();
    const before = requests.filter((r) => r.url.endsWith(`/studio/projects/${pid}/transcript-ref`)).length;
    fireEvent.click(screen.getByRole("button", { name: "重新剪辑并替换旧切片" }));
    await waitFor(() => expect(requests.filter((r) => r.url.endsWith(`/studio/projects/${pid}/transcript-ref`)).length).toBe(before + 1));
    await switchB(); fireEvent.click(screen.getByRole("button", { name: "重新剪辑并替换旧切片" }));
    await waitFor(() => expect(requests.filter((r) => r.url.endsWith(`/studio/projects/${bid}/transcript-ref`)).length).toBeGreaterThan(1));
    await act(async () => { resolverWaitA!.resolve(json(credential)); await Promise.resolve(); });
    expect(screen.getByRole("button", { name: "确认重新剪辑" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "确认重新剪辑" }));
    await act(async () => { resolverWaitB!.resolve(json({ detail: "当前项目B没有转写" }, 404)); await Promise.resolve(); });
    await screen.findByText(/未找到该项目的已纠错转写/);
    expect(screen.getByRole("button", { name: "确认重新剪辑" })).toBeDisabled();
    expect(requests.some((r) => r.url.endsWith("/api/jobs/re-edit") || r.url.endsWith("/studio/projects"))).toBe(false);
  });
});
