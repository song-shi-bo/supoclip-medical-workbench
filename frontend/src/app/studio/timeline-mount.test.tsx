/**
 * 底部时间线挂载区 DOM 契约（Phase B 接线收尾）。
 *
 * 与 `timeline-wiring.test.tsx`（请求接线）互补：这里只锁挂载结构 ——
 * `.timeline-pane` 内必须是「TimelineToolbar → .timeline-scroll(TimelineTracks) → footer」，
 * 静态占位（死按钮 / 860px 百分比坐标）彻底消失，滚动容器语义保留。
 */
import { describe, expect, it, beforeEach } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "@/test/setup";
vi.mock("./studio.css", () => ({}));
import StudioPage from "./page";

const PROJECT = "timeline_mount";

const projectHandlers = () => [
  http.get(`*/studio/timeline/${PROJECT}`, () => HttpResponse.json({
    id: PROJECT,
    timeline: { ranges: [{ id: "clip-1", source_start: 0, source_end: 45 }] },
    transcript: [
      { start: 0, end: 2, text: "血压高要吃三种药。" },
      { start: 2, end: 4, text: "记得定期复查。" },
    ],
  })),
  http.post(`*/studio/timeline/${PROJECT}/preview`, () => new HttpResponse(null, { status: 200 })),
  http.get(`*/studio/timeline/${PROJECT}/view`, () => HttpResponse.json({
    project_id: PROJECT,
    view: { zoom_px_per_second: 100, audio_track_height: 72, waveform_ratio: "standard", scroll_left: 0 },
  })),
  http.get(`*/studio/timeline/${PROJECT}/history`, () => HttpResponse.json({
    project_id: PROJECT,
    summary: { version: 1, limit: 50, total: 1, index: 0, current_revision: 1, current_label: "auto-edit", undo_depth: 0, redo_depth: 0, can_undo: false, can_redo: false, labels: [] },
    current_revision: { revision: 1, updated_at: "2026-01-01T00:00:00Z", label: "auto-edit", content_hash: "h", ranges: [{ id: "clip-1", source_start: 0, source_end: 45 }], audio_tracks: [], subtitles: null },
    revision: 1,
    audio_tracks: [],
    can_undo: false,
    can_redo: false,
  })),
  http.get(`*/studio/timeline/${PROJECT}/assets/peaks`, () => HttpResponse.json({
    project_id: PROJECT, path: "audio/source.m4a", duration_s: 45, peaks: [0.5], peak_count: 1,
    cached: true, cache_key: "k", source: "ffmpeg",
  })),
];

beforeEach(() => {
  window.localStorage.clear();
  // ?source_id= 会跨用例残留（jsdom 同一 document），逐用例重置回无参地址。
  window.history.pushState({}, "", "/");
  server.use(...projectHandlers(), http.all("*", () => HttpResponse.json({}, { status: 404 })));
});

const mountWithSession = async () => {
  // 2026-10-16：会话指针已废除，改为显式 URL 参数打开历史项目。
  window.history.pushState({}, "", `/?source_id=${PROJECT}`);
  const screen = render(<StudioPage />);
  await screen.findByRole("toolbar", { name: "时间线工具栏" });
  return screen;
};

const query = (container: HTMLElement, selector: string) => container.querySelector(selector);

describe("时间线挂载区 DOM 契约（timeline-pane）", () => {
  it("结构顺序：TimelineToolbar → .timeline-scroll（刻度+网格）→ .timeline-footer", async () => {
    const { container } = await mountWithSession();
    const pane = query(container, ".timeline-pane") as HTMLElement;
    expect(pane).toBeTruthy();
    const children = [...pane.children].map((node) => (
      node.classList.contains("timeline-toolbar") ? "toolbar"
        : node.classList.contains("timeline-scroll") ? "scroll"
          : node.classList.contains("timeline-footer") ? "footer" : "other"
    ));
    expect(children.filter((kind) => kind !== "other")).toEqual(["toolbar", "scroll", "footer"]);
    // 工具栏是组件根（role=toolbar），不是旧的 div 占位。
    expect(query(pane, '[role="toolbar"][aria-label="时间线工具栏"]')).toBeTruthy();
    // 轨道层来自 TimelineTracks：刻度与网格都挂在新结构里。
    const scroll = query(pane, ".timeline-scroll") as HTMLElement;
    expect(query(scroll, '[data-testid="timeline-ruler"]')).toBeTruthy();
    expect(query(scroll, '[data-testid="timeline-track-grid"]')).toBeTruthy();
  });

  it("旧占位彻底消失：没有 75–150 死滑杆、没有剪刀/删除死按钮、没有固定 860px 百分比时间轴", async () => {
    const { container } = await mountWithSession();
    expect(query(container, '[aria-label="剪刀"]')).toBeNull();
    expect(query(container, '[aria-label="删除"]')).toBeNull();
    const oldSlider = [...container.querySelectorAll('input[type="range"]')].find((node) => node.getAttribute("min") === "75");
    expect(oldSlider).toBeUndefined();
    // 新滑杆范围是 20–300（组件契约）。
    const zoomSlider = query(container, 'input[aria-label="时间线缩放"]') as HTMLInputElement;
    expect(zoomSlider.getAttribute("min")).toBe("20");
    expect(zoomSlider.getAttribute("max")).toBe("300");
  });

  it("滚动容器语义保留：.timeline-scroll 由页面持有（擦洗/onScroll/双击删除挂在它上面）", async () => {
    const { container } = await mountWithSession();
    const scroll = query(container, ".timeline-scroll") as HTMLElement;
    expect(scroll).toBeTruthy();
    // 页面级回调真实挂在滚动容器上（React 19 的 props 挂在 __reactProps$ 键下）。
    const propsKey = Object.keys(scroll).find((key) => key.startsWith("__reactProps$"));
    expect(propsKey).toBeTruthy();
    const props = (scroll as unknown as Record<string, Record<string, unknown>>)[propsKey as string];
    expect(typeof props.onPointerDown).toBe("function");
    expect(typeof props.onPointerMove).toBe("function");
    expect(typeof props.onPointerUp).toBe("function");
    expect(typeof props.onPointerCancel).toBe("function");
    expect(typeof props.onScroll).toBe("function");
    expect(typeof props.onDoubleClick).toBe("function");
    // 组件的刻度/网格在滚动容器内部（外层 overflow-x 生效的前提）。
    expect(scroll.contains(query(container, '[data-testid="timeline-track-grid"]') as Node)).toBe(true);
  });

  it("无项目（纯本地会话）时不发 history 请求：工具栏仍渲染，音轨行显示空态提示", async () => {
    let historyCalls = 0;
    server.use(http.get("*/studio/timeline/*/history", () => { historyCalls += 1; return HttpResponse.json({}, { status: 404 }); }));
    const screen = render(<StudioPage />);
    await screen.findByRole("toolbar", { name: "时间线工具栏" });
    expect(screen.container.querySelector('[data-testid="timeline-audio-empty"]')?.textContent).toContain("导入视频后原声会自动进入时间线");
    // 无项目时撤销/重做置灰（can_undo=false 缺省）。
    expect((screen.container.querySelector('[aria-label="撤销"]') as HTMLButtonElement).disabled).toBe(true);
    await waitFor(() => expect(screen.container.querySelector('[data-testid="timeline-audio-empty"]')).toBeTruthy());
    expect(historyCalls).toBe(0);
  });

  it("字幕轨 cue 块来自页面 segments（新像素坐标：left/width 是 px，不再是百分比）", async () => {
    const { container } = await mountWithSession();
    const lane = await waitFor(() => {
      const node = query(container, '[data-testid="timeline-caption-lane"]') as HTMLElement;
      expect(node).toBeTruthy();
      return node;
    });
    const blocks = lane.querySelectorAll("button");
    expect(blocks.length).toBe(2);
    const first = blocks[0] as HTMLElement;
    expect(first.style.left.endsWith("%")).toBe(false);
    expect(first.textContent).toContain("血压高要吃三种药");
  });

  it("播放头随行渲染：字幕/视频/音轨行各有 .track-playhead（内联像素 left）", async () => {
    const { container } = await mountWithSession();
    await waitFor(() => expect(query(container, '[data-testid="timeline-caption-lane"]')).toBeTruthy());
    const playheads = container.querySelectorAll(".track-playhead");
    expect(playheads.length).toBeGreaterThanOrEqual(3);
    const first = playheads[0] as HTMLElement;
    expect(first.style.left.trim()).not.toBe("");
    expect(first.style.left.endsWith("%")).toBe(false);
  });
});
