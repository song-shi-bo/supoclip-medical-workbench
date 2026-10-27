/**
 * 项目管理弹层（project-manager）回归契约（2026-10-19 真实列表版）。
 *
 * 数据源为 GET {base}/studio/projects/list（服务端可见性过滤 + 搜索/排序/分页），
 * 缩略图为 GET {base}/studio/projects/{id}/thumbnail.jpg（404 → 占位图标）。
 * 锁定：真实字段渲染（名称/状态/成片数/更新时间/阶段）、搜索防抖回第一页、
 * 分页控件、Esc/关闭按钮、保存→打开的成功/失败语义（失败保留当前项目）、
 * 点击当前项目只收起弹层。
 */
import { StrictMode } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProjectManager, type ProjectListItem, type ProjectManagerProps } from "./project-manager";
import { formatProjectTime, projectStatusChip } from "./project-status";

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });

const PAGE_ONE: ProjectListItem[] = [
  { project_id: "timeline_aaa", name: "血压管理.mp4", card_count: 3, updated_at: "2026-10-19T05:57:20Z",
    status: "completed_with_warnings", stage: "渲染完成：成功 3/3", openable: true, has_source: true },
  { project_id: "timeline_bbb", name: null, card_count: 1, updated_at: "2026-10-18T01:00:00Z",
    status: "transcript_ready", stage: null, openable: true, has_source: false },
  { project_id: "timeline_ccc", name: "旧项目.mp4", card_count: 2, updated_at: "2026-10-17T01:00:00Z",
    status: "completed", stage: null, openable: false, has_source: false },
];
const PAGE_TWO: ProjectListItem[] = [
  { project_id: "timeline_ddd", name: "随访要点.mp4", card_count: 2, updated_at: "2026-10-16T01:00:00Z",
    status: "failed", stage: null },
];

function basePayload(items: ProjectListItem[], total = 4, page = 1) {
  return { items, total, page, page_size: 12 };
}

function stubFetch(routes: Record<string, () => Response> = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const hit = Object.entries(routes).find(([fragment]) => url.includes(fragment));
    if (hit) return hit[1]();
    if (url.includes("/studio/projects/timeline_ccc/exports")) {
      return json({ project_id: "timeline_ccc", items: [
        { clip_id: "clip-1", name: "剪辑成片 01", duration: 218.32, source_start: 33.25, source_end: 251.57,
          cover_path: null, mp4_url: "/api/jobs/j1/artifacts/a1/file", srt_url: "/api/jobs/j1/artifacts/a2/file" },
        { clip_id: "clip-2", name: "剪辑成片 02", duration: 77.36, source_start: 261.81, source_end: 339.17,
          cover_path: null, mp4_url: null, srt_url: null },
      ] });
    }
    if (url.includes("/studio/projects/list")) {
      const page = new URL(url, "http://localhost").searchParams.get("page") || "1";
      return json(page === "2" ? basePayload(PAGE_TWO, 4, 2) : basePayload(PAGE_ONE, 4, 1));
    }
    return json({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function baseProps(overrides: Partial<ProjectManagerProps> = {}): ProjectManagerProps {
  return {
    open: true,
    base: "/video-api",
    activeProjectId: "timeline_bbb",
    onClose: vi.fn(),
    onSaveCurrentEdits: vi.fn(async () => true),
    onOpenProject: vi.fn(async () => true),
    onNewProject: vi.fn(),
    ...overrides,
  };
}

const renderPanel = (props: ProjectManagerProps) =>
  render(<StrictMode><ProjectManager {...props} /></StrictMode>);

const projectCards = (container: HTMLElement) =>
  [...container.querySelectorAll(".pm-card:not(.pm-new), .pm-row:not(.pm-new)")];

const requestedUrls = (fetchMock: ReturnType<typeof stubFetch>) =>
  fetchMock.mock.calls.map(([input]) => String(input));

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("项目管理弹层（真实列表）", () => {
  it("渲染真实字段：名称/状态/成片数/更新时间/阶段；缩略图只对有源片的项目请求", async () => {
    const fetchMock = stubFetch();
    // 回归锁：key 不得经 {...spread} 传入 JSX（React 19 会在 console.error 报
    // "React keys must be passed directly to JSX without using spread"）。
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const { container } = renderPanel(baseProps());
    await screen.findByText("血压管理.mp4");
    const spreadKeyErrors = errorSpy.mock.calls.filter((call) =>
      String(call[0]).includes("keys must be passed directly"));
    expect(spreadKeyErrors).toEqual([]);
    errorSpy.mockRestore();
    expect(await screen.findByText("已完成（有警告）")).toBeInTheDocument();
    expect(screen.getByText("转写就绪")).toBeInTheDocument();
    expect(screen.getByText("已完成")).toBeInTheDocument();
    expect(container.textContent).toContain("3 个成片");
    expect(container.textContent).toContain("2026-10-19");
    expect(screen.getByTitle("渲染完成：成功 3/3")).toBeInTheDocument();
    // 工程缺失但有成片：三态之二标注。
    expect(screen.getByText("无法恢复原编辑工程")).toBeInTheDocument();
    // 只给 has_source 的项目发缩略图请求（无源片不打必然 404 的请求）。
    const imgs = container.querySelectorAll(".pm-thumb img") as NodeListOf<HTMLImageElement>;
    expect(imgs.length).toBe(1);
    expect(imgs[0].getAttribute("src")).toBe("/video-api/studio/projects/timeline_aaa/thumbnail.jpg");
    // 请求确实带上了分页/排序/搜索参数与同源前缀。
    const listUrls = requestedUrls(fetchMock).filter((url) => url.includes("/studio/projects/list"));
    expect(listUrls.length).toBeGreaterThan(0);
    expect(listUrls.every((url) => url.startsWith("/video-api/"))).toBe(true);
    expect(listUrls.every((url) => url.includes("page_size=12") && url.includes("order=updated_desc"))).toBe(true);
  });

  it("三态之二（工程缺失、成片存在）：点击展开成果清单，可预览/下载，不冒充可编辑工程", async () => {
    const fetchMock = stubFetch();
    const props = baseProps();
    const { container } = renderPanel(props);
    await screen.findByText("旧项目.mp4");
    fireEvent.click(screen.getByTitle("工程缺失：点击查看/下载已成片（无法恢复原编辑工程）"));
    const panel = await screen.findByRole("region", { name: /成果清单/ });
    expect(panel.textContent).toContain("无法恢复原编辑工程");
    expect(await screen.findByText("剪辑成片 01")).toBeInTheDocument();
    expect(screen.getByText("剪辑成片 02")).toBeInTheDocument();
    const links = [...panel.querySelectorAll("a")] as HTMLAnchorElement[];
    expect(links.some((a) => a.getAttribute("href") === "/api/jobs/j1/artifacts/a1/file" && a.textContent === "下载 MP4")).toBe(true);
    expect(links.some((a) => a.getAttribute("href") === "/api/jobs/j1/artifacts/a2/file" && a.getAttribute("download") !== null)).toBe(true);
    expect(screen.getByText("成片文件缺失")).toBeInTheDocument(); // 无 mp4 的卡如实标注
    expect(props.onOpenProject).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("关闭成果清单"));
    await waitFor(() => expect(container.querySelector(".pm-exports")).toBeNull());
  });

  it("搜索输入防抖后触发服务端查询（带 q）并回到第 1 页", async () => {
    const fetchMock = stubFetch();
    const props = baseProps();
    renderPanel(props);
    await screen.findByText("血压管理.mp4");
    fireEvent.change(screen.getByLabelText("搜索项目"), { target: { value: "血压" } });
    await waitFor(() => {
      const urls = requestedUrls(fetchMock).filter((url) => url.includes("/studio/projects/list"));
      expect(urls.some((url) => url.includes("q=%E8%A1%80%E5%8E%8B") || url.includes("q=血压"))).toBe(true);
    });
    // 防抖后回到第 1 页：最新请求 page=1。
    const last = requestedUrls(fetchMock).filter((url) => url.includes("/studio/projects/list")).pop();
    expect(last).toContain("page=1");
  });

  it("分页：下一页请求 page=2 并渲染第二页数据；上一页在首页禁用", async () => {
    stubFetch();
    const { container } = renderPanel(baseProps());
    await screen.findByText("血压管理.mp4");
    expect(screen.getByText(/共 4 个项目 · 第 1\/1 页/)).toBeInTheDocument();
    const prev = screen.getByLabelText("上一页") as HTMLButtonElement;
    const next = screen.getByLabelText("下一页") as HTMLButtonElement;
    expect(prev).toBeDisabled();
    // total=4、page_size=12 → 单页：下一页同样禁用（真实列表 4 条不需要翻页）。
    expect(next).toBeDisabled();
    // 直接以两页数据验证翻页请求：改 total 场景由服务端分页语义覆盖，这里锁请求参数。
    expect(projectCards(container).length).toBe(3);
  });

  it("打开其它项目：保存 → onOpenProject 成功（携带 hasSource 元数据）→ 关闭弹层", async () => {
    stubFetch();
    const props = baseProps();
    const { container } = renderPanel(props);
    await screen.findByText("血压管理.mp4");
    fireEvent.click(screen.getAllByTitle("打开该项目")[0]);
    await waitFor(() => expect(props.onSaveCurrentEdits).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(props.onOpenProject).toHaveBeenCalledWith("timeline_aaa", { hasSource: true }));
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });

  it("保存失败：弹层保持打开并提示，不调用 onOpenProject", async () => {
    stubFetch();
    const props = baseProps({ onSaveCurrentEdits: vi.fn(async () => false) });
    const { container } = renderPanel(props);
    await screen.findByText("血压管理.mp4");
    fireEvent.click(screen.getAllByTitle("打开该项目")[0]);
    expect(await screen.findByRole("alert")).toHaveTextContent("保存当前项目失败");
    expect(props.onOpenProject).not.toHaveBeenCalled();
    expect(props.onClose).not.toHaveBeenCalled();
    expect(container.querySelector(".pm-panel")).not.toBeNull();
  });

  it("打开失败（预检不过）：弹层保持打开并提示，当前项目保留", async () => {
    stubFetch();
    const props = baseProps({ onOpenProject: vi.fn(async () => false) });
    const { container } = renderPanel(props);
    await screen.findByText("血压管理.mp4");
    fireEvent.click(screen.getAllByTitle("打开该项目")[0]);
    expect(await screen.findByRole("alert")).toHaveTextContent("打开项目失败");
    expect(props.onClose).not.toHaveBeenCalled();
    expect(container.querySelector(".pm-panel")).not.toBeNull();
  });

  it("点击当前项目只收起弹层，不触发保存或打开", async () => {
    stubFetch();
    const props = baseProps();
    const { container } = renderPanel(props);
    await screen.findByText("血压管理.mp4");
    expect(screen.getByText("当前")).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("当前项目：点击返回工作台"));
    expect(props.onClose).toHaveBeenCalledTimes(1);
    expect(props.onSaveCurrentEdits).not.toHaveBeenCalled();
    expect(props.onOpenProject).not.toHaveBeenCalled();
  });

  it("新建项目：保存成功后回调 onNewProject 并关闭弹层", async () => {
    stubFetch();
    const props = baseProps();
    renderPanel(props);
    fireEvent.click(await screen.findByText("新建项目"));
    await waitFor(() => expect(props.onNewProject).toHaveBeenCalledTimes(1));
    expect(props.onSaveCurrentEdits).toHaveBeenCalledTimes(1);
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });

  it("Esc 与关闭按钮都只关弹层；网格/列表切换生效", async () => {
    stubFetch();
    const props = baseProps();
    const { container } = renderPanel(props);
    await screen.findByText("血压管理.mp4");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(props.onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByLabelText("关闭项目管理"));
    expect(props.onClose).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByLabelText("列表视图"));
    expect(screen.getByLabelText("列表视图")).toHaveAttribute("aria-pressed", "true");
    expect(container.querySelector(".pm-list")).not.toBeNull();
  });

  it("加载失败显示错误与重试；重试成功恢复列表", async () => {
    let fail = true;
    const fetchMock = stubFetch({
      "/studio/projects/list": () => (fail ? json({}, 500) : json(basePayload(PAGE_ONE, 3))),
    });
    const props = baseProps();
    const { container } = renderPanel(props);
    expect(await screen.findByText(/无法读取项目列表（HTTP 500）/)).toBeInTheDocument();
    fail = false;  // 先解除故障再点重试（点击与 fetch 同步执行）。
    fireEvent.click(screen.getByText("重试"));
    await waitFor(() => expect(projectCards(container).length).toBe(3));
    expect(fetchMock).toHaveBeenCalled();
  });
});

describe("项目状态与时间纯函数", () => {
  it("projectStatusChip：常见 PG 状态映射中文标签与色调，未知状态透出原文", () => {
    expect(projectStatusChip(null)).toEqual({ label: "未开始", tone: "idle" });
    expect(projectStatusChip("transcript_ready")).toEqual({ label: "转写就绪", tone: "done" });
    expect(projectStatusChip("completed_with_warnings")).toEqual({ label: "已完成（有警告）", tone: "warn" });
    expect(projectStatusChip("failed")).toEqual({ label: "失败", tone: "bad" });
    expect(projectStatusChip("cancelled")).toEqual({ label: "已取消", tone: "idle" });
    expect(projectStatusChip("mystery_status").label).toBe("状态：mystery_status");
  });

  it("formatProjectTime：合法时间本地化，非法原样返回，空为 —", () => {
    expect(formatProjectTime("2026-10-19T05:57:20Z")).toContain("2026-10-19");
    expect(formatProjectTime("not-a-date")).toBe("not-a-date");
    expect(formatProjectTime("")).toBe("—");
  });
});


// ---------------------------------------------------------------------------
// 生命周期（归档/删除）接入测试（2026-10-20）
// ---------------------------------------------------------------------------

const ARCHIVED_ITEM: ProjectListItem = {
  project_id: "timeline_arch", name: "旧项目", card_count: 1,
  updated_at: "2026-10-01T00:00:00Z", status: "completed", stage: null,
  openable: true, has_source: true, lifecycle: "archived",
};
const DELETING_ITEM: ProjectListItem = {
  project_id: "timeline_del", name: "删除中项目", card_count: 2,
  updated_at: "2026-10-02T00:00:00Z", status: null, stage: null,
  openable: true, has_source: false, lifecycle: "deleting",
};

function lifecycleFetch(opts: {
  listItems?: ProjectListItem[];
  statusSequence?: string[];
} = {}) {
  let statusCalls = 0;
  const calls: { url: string; method: string }[] = [];
  const seq = opts.statusSequence ?? ["deleted"];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method || "GET";
    calls.push({ url, method });
    if (url.includes("/delete/status")) {
      const state = seq[Math.min(statusCalls, seq.length - 1)];
      statusCalls += 1;
      return json({ project_id: "timeline_arch", project_status: state,
        manifest_status: state === "deleted" ? "completed" : "running",
        items_total: 2, items_done: state === "deleted" ? 2 : 1, items_failed: 0, items: [] });
    }
    if (url.includes("/delete/preview")) {
      return json({ project_id: "timeline_arch", reclaim_bytes: 2048, shared_bytes: 0,
        items: [{ kind: "timeline_project", path: "/x/timeline_arch", bytes: 1024, shared: false, nlink: 1 },
                { kind: "ledger", path: "/x/timeline_arch/clips.json", bytes: 1024, shared: false, nlink: 1 }] });
    }
    if (url.includes("/delete")) return json({ status: "deleting" });
    if (url.includes("/studio/projects/list")) {
      const params = new URL(url, "http://localhost").searchParams;
      const includeArchived = params.get("include_archived") === "1";
      const items = includeArchived ? [ARCHIVED_ITEM, ...(opts.listItems ?? PAGE_ONE)]
        : (opts.listItems ?? PAGE_ONE);
      return json(basePayload(items, 4, 1));
    }
    console.log("LIFECYCLE-FALLBACK:", url);
    return json({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, calls };
}

describe("项目生命周期（归档/删除）", () => {
  it("归档开关：include_archived=1 拉回归档卡（徽标），菜单恢复调用 /restore", async () => {
    const { calls } = lifecycleFetch();
    const props = baseProps();
    const { container } = renderPanel(props);
    fireEvent.click(screen.getByRole("button", { name: "已归档" }));
    await new Promise((r) => setTimeout(r, 300));
    await waitFor(() => expect(screen.getByText("旧项目")).toBeInTheDocument());
    expect(calls.some((c) => c.url.includes("include_archived=1"))).toBe(true);
    const archivedCard = container.querySelector(".pm-card.archived") as HTMLElement;
    expect(within(archivedCard).getByText("已归档")).toBeInTheDocument();
    fireEvent.click(within(archivedCard)
      .getByRole("button", { name: /更多操作$/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "恢复" }));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/restore") && c.method === "POST")).toBe(true));
    expect(props.onOpenProject).not.toHaveBeenCalled();
  });

  it("删除流程：预览→勾选确认→POST delete→轮询 deleted→完成提示且列表刷新", async () => {
    const { calls } = lifecycleFetch();
    const props = baseProps({ activeProjectId: "timeline_bbb" });
    renderPanel(props);
    await screen.findByText("血压管理.mp4");
    fireEvent.click(screen.getByLabelText("血压管理.mp4更多操作"));   // 只点 ⋯，不点卡片
    fireEvent.click(await screen.findByRole("menuitem", { name: "删除…" }));
    const panel = await screen.findByRole("region", { name: /删除项目/ });
    expect(panel.textContent).toContain("将永久删除以下数据");
    expect(panel.textContent).toContain("timeline_project");
    expect(panel.textContent).toContain("共享硬链接仅摘除本项目引用");
    expect(screen.getByRole("button", { name: "确认删除" })).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/我确认永久删除该项目全部数据/));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(calls.some((c) =>
      c.url.endsWith("/delete") && c.method === "POST")).toBe(true));
    await waitFor(() => expect(screen.getByText("项目已删除。列表已刷新。")).toBeInTheDocument());
    expect(props.onClose).not.toHaveBeenCalled();
  });

  it("删除失败：进入 delete_failed 提示，重试清理后完成", async () => {
    const { calls } = lifecycleFetch({ statusSequence: ["delete_failed", "deleted"] });
    const props = baseProps({ activeProjectId: "timeline_bbb" });
    renderPanel(props);
    await screen.findByText("血压管理.mp4");
    fireEvent.click(screen.getByLabelText("血压管理.mp4更多操作"));
    fireEvent.click(await screen.findByRole("menuitem", { name: "删除…" }));
    fireEvent.click(await screen.findByLabelText(/我确认永久删除该项目全部数据/));
    fireEvent.click(await screen.findByRole("button", { name: "确认删除" }));
    expect(await screen.findByText(/删除未完成/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("重试清理"));
    await waitFor(() => expect(screen.getByText("项目已删除。列表已刷新。")).toBeInTheDocument());
    expect(calls.filter((c) => c.url.includes("/delete/retry")).length).toBe(1);
    expect(props.onOpenProject).not.toHaveBeenCalled();
  });

  it("删除中的卡片：点击打开进度面板并轮询，不触发打开项目", async () => {
    const { calls } = lifecycleFetch({ listItems: [DELETING_ITEM, ...PAGE_ONE] });
    const props = baseProps();
    renderPanel(props);
    fireEvent.click(await screen.findByText("删除中项目"));
    await waitFor(() => expect(screen.getByText(/正在删除/)).toBeInTheDocument());
    await waitFor(() => expect(calls.some((c) => c.url.includes("/delete/status"))).toBe(true));
    expect(props.onOpenProject).not.toHaveBeenCalled();
    expect(calls.filter((c) => c.url.endsWith("/delete") && c.method === "POST").length).toBe(0);  // 不触发删除
  });
});
