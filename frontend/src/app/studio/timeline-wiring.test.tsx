/**
 * 底部时间线接线（Phase B）回归契约。
 *
 * 工具栏与轨道层来自 `TimelineToolbar` / `TimelineTracks` 组件（组件本体另有契约测试）；
 * 这里只锁**页面接线**：静态占位工具栏必须消失、视图设置 GET/PUT view、
 * history 的 can_undo/can_redo 置灰与撤销/重做回写、＋ 菜单加轨请求、
 * 真实波形 peaks（buckets=800）。断言的是「真的发了哪个请求、UI 是否按响应变化」。
 */
import { describe, expect, it, beforeEach } from "vitest";
import { render, fireEvent, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "@/test/setup";
// 页面直接 import 了 studio.css；jsdom 用例不需要真实样式（同 asset-panel.test.tsx）。
vi.mock("./studio.css", () => ({}));
import StudioPage from "./page";

const PROJECT = "timeline_wiring";

const voiceTrack = {
  track_id: "track-voice", kind: "voice", path: "audio/source.m4a", asset_id: "asset-1",
  source_start_s: 0, source_end_s: 45, record_start_s: 0, volume: 1, muted: false,
  fade_in_s: 0, fade_out_s: 0, label: "原始视频原声", origin: "source" as "source" | "user",
};

type Track = typeof voiceTrack;

const revisionOf = (audioTracks: Track[], revision: number) => ({
  revision, updated_at: "2026-01-01T00:00:00Z", label: "auto-edit", content_hash: "hash-1",
  ranges: [{ id: "clip-1", source_start: 0, source_end: 45 }],
  audio_tracks: audioTracks, subtitles: null,
});

const historyPayload = (options: { audioTracks?: Track[]; canUndo?: boolean; canRedo?: boolean; revision?: number } = {}) => {
  const audio = options.audioTracks ?? [voiceTrack];
  const rev = options.revision ?? 3;
  return {
    project_id: PROJECT,
    summary: {
      version: 1, limit: 50, total: 3, index: 2, current_revision: rev, current_label: "auto-edit",
      undo_depth: options.canUndo ? 1 : 0, redo_depth: options.canRedo ? 1 : 0,
      can_undo: options.canUndo ?? false, can_redo: options.canRedo ?? false, labels: [],
    },
    current_revision: revisionOf(audio, rev),
    revision: rev,
    audio_tracks: audio,
    can_undo: options.canUndo ?? false,
    can_redo: options.canRedo ?? false,
  };
};

type Recorded = { label: string; url: URL; body?: unknown };
const recorded: Recorded[] = [];
const resetRecorded = () => { recorded.length = 0; };
const findRecorded = (label: string) => recorded.find((entry) => entry.label === label);

// history 端点 handler 工厂：覆盖（server.use）也走这里，保证「GET history 已发生」
// 这个就绪信号在任何 history 载荷下都成立。
const historyHandler = (payload = historyPayload()) => http.get(`*/studio/timeline/${PROJECT}/history`, ({ request }) => {
  recorded.push({ label: "GET history", url: new URL(request.url) });
  return HttpResponse.json(payload);
});

const timelineHandlers = (history = historyPayload()) => [
  http.get(`*/studio/timeline/${PROJECT}`, () => HttpResponse.json({
    id: PROJECT,
    timeline: { ranges: [{ id: "clip-1", source_start: 0, source_end: 45 }] },
    transcript: [{ start: 0, end: 2, text: "血压高要吃三种药。" }],
  })),
  http.post(`*/studio/timeline/${PROJECT}/preview`, () => new HttpResponse(null, { status: 200 })),
  http.get(`*/studio/timeline/${PROJECT}/view`, () => HttpResponse.json({
    project_id: PROJECT,
    view: { zoom_px_per_second: 120, audio_track_height: 90, waveform_ratio: "expanded", scroll_left: 40 },
  })),
  http.put(`*/studio/timeline/${PROJECT}/view`, async ({ request }) => {
    recorded.push({ label: "PUT view", url: new URL(request.url), body: await request.json() });
    return HttpResponse.json({ project_id: PROJECT, view: await request.json() });
  }),
  historyHandler(history),
  http.get(`*/studio/timeline/${PROJECT}/assets/peaks`, ({ request }) => {
    const url = new URL(request.url);
    recorded.push({ label: "GET peaks", url });
    return HttpResponse.json({
      project_id: PROJECT, path: url.searchParams.get("path"), duration_s: 45,
      peaks: [0.2, 0.8, 0.5, 0.1], peak_count: 4, cached: true, cache_key: "k", source: "ffmpeg",
    });
  }),
  http.post(`*/studio/timeline/${PROJECT}/assets/audio`, ({ request }) => {
    recorded.push({ label: "POST audio", url: new URL(request.url) });
    return HttpResponse.json({
      project_id: PROJECT, path: "timeline/audio/bgm-1234.wav", filename: "bgm.wav",
      asset_id: "bgm-1234", duration_s: 5,
    });
  }),
  http.post(`*/studio/timeline/${PROJECT}/tracks`, async ({ request }) => {
    const body = (await request.json()) as { kind?: string };
    recorded.push({ label: "POST tracks", url: new URL(request.url), body });
    const added: Track = {
      ...voiceTrack, track_id: "track-bgm-1", kind: "bgm", path: "timeline/audio/bgm-1234.wav",
      origin: "user", label: "bgm.wav", record_start_s: 0,
    };
    return HttpResponse.json(historyPayload({
      audioTracks: [...(history.audio_tracks ?? []), added], canUndo: true, revision: 4,
    }));
  }),
  http.post(`*/studio/timeline/${PROJECT}/undo`, ({ request }) => {
    recorded.push({ label: "POST undo", url: new URL(request.url) });
    return HttpResponse.json(historyPayload({ audioTracks: [], canUndo: false, canRedo: true, revision: 2 }));
  }),
  http.post(`*/studio/timeline/${PROJECT}/redo`, ({ request }) => {
    recorded.push({ label: "POST redo", url: new URL(request.url) });
    return HttpResponse.json(historyPayload({ audioTracks: [voiceTrack], canUndo: true, canRedo: false, revision: 3 }));
  }),
];

beforeEach(() => {
  resetRecorded();
  window.localStorage.clear();
  server.use(...timelineHandlers(), http.all("*", () => HttpResponse.json({}, { status: 404 })));
});

// 页面靠显式 URL 参数打开历史项目 → activeProjectId 就绪后时间线接线才会拉数据。
const mountWiredPage = async () => {
  window.history.pushState({}, "", `/?source_id=${PROJECT}`);
  const screen = render(<StudioPage />);
  await screen.findByRole("toolbar", { name: "时间线工具栏" });
  await waitFor(() => expect(findRecorded("GET history")).toBeTruthy());
  return screen;
};

const query = (container: HTMLElement, selector: string) => container.querySelector(selector) as HTMLElement | null;

describe("底部时间线接线（工具栏 + 轨道层挂载）", () => {
  it("静态占位工具栏被 TimelineToolbar 取代：撤销/重做按 can_undo/can_redo 置灰", async () => {
    const { container, unmount } = await mountWiredPage();
    // 旧占位的死按钮（选择/剪刀/删除/适合窗口占位）不再出现。
    expect(query(container, '[aria-label="剪刀"]')).toBeNull();
    expect(query(container, '[aria-label="选择"]')).toBeNull();
    expect(query(container, '[aria-label="删除"]')).toBeNull();
    // can_undo=false / can_redo=false → 置灰 + tooltip。
    await waitFor(() => expect(findRecorded("GET peaks")).toBeTruthy());
    expect((query(container, '[aria-label="撤销"]') as HTMLButtonElement).disabled).toBe(true);
    expect((query(container, '[aria-label="重做"]') as HTMLButtonElement).disabled).toBe(true);
    expect(query(container, '[aria-label="撤销"]')?.getAttribute("title")).toContain("没有可撤销");
    unmount();
    // history 返回 can_undo=true → 撤销可用。
    server.use(historyHandler(historyPayload({ canUndo: true })));
    const second = render(<StudioPage />);
    await second.findByRole("toolbar", { name: "时间线工具栏" });
    await waitFor(() => {
      const undo = second.container.querySelector('[aria-label="撤销"]') as HTMLButtonElement;
      expect(undo.disabled).toBe(false);
    });
    second.unmount();
  });

  it("GET view 恢复：缩放滑杆 / 轨高 / 波形占比档位按服务端值还原", async () => {
    const { container } = await mountWiredPage();
    const slider = query(container, 'input[aria-label="时间线缩放"]') as HTMLInputElement;
    await waitFor(() => expect(slider.value).toBe("120"));
    // 打开轨道显示设置浮层检查轨高与波形占比。
    fireEvent.click(query(container, '[aria-label="轨道显示设置"]') as HTMLElement);
    const heightSlider = await waitFor(() => {
      const node = query(container, 'input[aria-label="轨道高度"]') as HTMLInputElement;
      expect(node).toBeTruthy();
      return node;
    });
    expect(heightSlider.value).toBe("90");
    const expanded = query(container, '[aria-label="波形占比 扩展"]');
    expect(expanded?.getAttribute("aria-pressed")).toBe("true");
  });

  it("点放大：zoom_px_per_second 增大并防抖 PUT view（scroll_left 非负，播放头 t=0 时不跳变）", async () => {
    const { container } = await mountWiredPage();
    const slider = query(container, 'input[aria-label="时间线缩放"]') as HTMLInputElement;
    await waitFor(() => expect(slider.value).toBe("120"));
    fireEvent.click(query(container, '[aria-label="放大时间线"]') as HTMLElement);
    // 120 × 1.25 = 150（ZOOM_STEP_FACTOR）。
    await waitFor(() => expect(slider.value).toBe("150"));
    await waitFor(() => {
      const puts = recorded.filter((entry) => entry.label === "PUT view");
      expect(puts.some((entry) => (entry.body as { zoom_px_per_second: number }).zoom_px_per_second === 150)).toBe(true);
      expect(puts.every((entry) => Number((entry.body as { scroll_left: number }).scroll_left) >= 0)).toBe(true);
    }, { timeout: 2000 });
  });

  it("＋ 菜单不含可点的「原始视频」项：origin=source 时只出现置灰说明项；字幕轨走 POST /tracks", async () => {
    const { container } = await mountWiredPage();
    fireEvent.click(query(container, '[aria-label="添加到时间线"]') as HTMLElement);
    const menu = query(container, '[role="menu"][aria-label="添加到时间线"]') as HTMLElement;
    expect(menu).toBeTruthy();
    // 原始视频/原声不是可添加动作：只有一条 aria-disabled 的说明项。
    const reserved = within(menu).getByRole("menuitem", { name: /原始视频原声/ });
    expect(reserved.getAttribute("aria-disabled")).toBe("true");
    expect(reserved.tagName).toBe("DIV");
    fireEvent.click(within(menu).getByRole("menuitem", { name: /字幕轨/ }));
    await waitFor(() => {
      const post = findRecorded("POST tracks");
      expect(post?.body).toEqual({ kind: "subtitle" });
    });
  });

  it("无 source 轨时 ＋ 菜单连说明项都不出现", async () => {
    server.use(historyHandler(historyPayload({ audioTracks: [] })));
    const { container } = await mountWiredPage();
    fireEvent.click(query(container, '[aria-label="添加到时间线"]') as HTMLElement);
    const menu = query(container, '[role="menu"][aria-label="添加到时间线"]') as HTMLElement;
    await waitFor(() => expect(menu.textContent).toContain("背景音乐"));
    expect(menu.textContent).not.toContain("原始视频");
  });

  it("点 BGM：先 POST /assets/audio 上传，再 POST /tracks（kind=bgm），成功后出现第二条音轨", async () => {
    const { container } = await mountWiredPage();
    // 走真实路径：＋ 菜单 → 背景音乐（页面弹出音频选择器）→ 文件 chosen。
    fireEvent.click(query(container, '[aria-label="添加到时间线"]') as HTMLElement);
    const menu = query(container, '[role="menu"][aria-label="添加到时间线"]') as HTMLElement;
    fireEvent.click(within(menu).getByRole("menuitem", { name: /背景音乐/ }));
    const audioInput = query(container, 'input[accept="audio/*"]') as HTMLInputElement;
    expect(audioInput).toBeTruthy();
    fireEvent.change(audioInput, { target: { files: [new File([new Uint8Array([1, 2, 3])], "bgm.wav", { type: "audio/wav" })] } });
    await waitFor(() => {
      const post = findRecorded("POST tracks");
      expect(post?.body).toMatchObject({
        kind: "bgm", path: "timeline/audio/bgm-1234.wav", label: "bgm.wav", record_start_s: 0,
      });
      expect(findRecorded("POST audio")).toBeTruthy();
    });
    await waitFor(() => expect(query(container, '[data-testid="timeline-audio-lane-track-bgm-1"]')).toBeTruthy());
  });

  it("音轨渲染 + 真实波形：peaks 请求带 path 与 buckets=800", async () => {
    const { container } = await mountWiredPage();
    await waitFor(() => expect(query(container, '[data-testid="timeline-audio-lane-track-voice"]')).toBeTruthy());
    const peaks = findRecorded("GET peaks");
    expect(peaks?.url.searchParams.get("path")).toBe(voiceTrack.path);
    expect(peaks?.url.searchParams.get("buckets")).toBe("800");
    expect(query(container, '[data-testid="timeline-waveform-track-voice"]')).toBeTruthy();
  });

  it("撤销按钮：POST /undo 后按响应回写音轨（BGM 轨消失）且 revision 更新", async () => {
    // 先造一条 BGM 轨（history canUndo=true 且带两条轨），撤销响应把它移除。
    const bgmTrack: Track = { ...voiceTrack, track_id: "track-bgm-1", kind: "bgm", origin: "user", label: "bgm.wav" };
    server.use(historyHandler(historyPayload({ audioTracks: [voiceTrack, bgmTrack], canUndo: true, revision: 4 })));
    const { container } = await mountWiredPage();
    await waitFor(() => expect(query(container, '[data-testid="timeline-audio-lane-track-bgm-1"]')).toBeTruthy());
    fireEvent.click(query(container, '[aria-label="撤销"]') as HTMLElement);
    await waitFor(() => expect(findRecorded("POST undo")).toBeTruthy());
    // undo 响应 audio_tracks=[] → 音轨行回到空态，撤销按钮按 can_undo=false 置灰。
    await waitFor(() => expect(query(container, '[data-testid="timeline-audio-lane-track-bgm-1"]')).toBeNull());
    await waitFor(() => expect((query(container, '[aria-label="撤销"]') as HTMLButtonElement).disabled).toBe(true));
    expect((query(container, '[aria-label="重做"]') as HTMLButtonElement).disabled).toBe(false);
  });

  it("快捷键：Ctrl+Z 撤销（焦点在输入框时不拦截）、Ctrl+Shift+Z 重做", async () => {
    server.use(historyHandler(historyPayload({ audioTracks: [voiceTrack], canUndo: true, canRedo: true, revision: 4 })));
    const { container } = await mountWiredPage();
    // 焦点在滑杆（INPUT）上：不拦截。
    const slider = query(container, 'input[aria-label="时间线缩放"]') as HTMLInputElement;
    fireEvent.keyDown(slider, { key: "z", ctrlKey: true });
    expect(findRecorded("POST undo")).toBeUndefined();
    // 焦点在页面上：Ctrl+Z → undo；undo 响应 can_undo=false → 再按不再发请求。
    // 先等撤销按钮解禁（保证快捷键 effect 已按 can_undo=true 的闭包重新订阅）。
    await waitFor(() => expect((query(container, '[aria-label="撤销"]') as HTMLButtonElement).disabled).toBe(false));
    fireEvent.keyDown(window, { key: "z", ctrlKey: true });
    await waitFor(() => expect(findRecorded("POST undo")).toBeTruthy());
    // undo 响应 can_undo=false → 等按钮置灰后再按键，确认不再发第二个 undo。
    await waitFor(() => expect((query(container, '[aria-label="撤销"]') as HTMLButtonElement).disabled).toBe(true));
    fireEvent.keyDown(window, { key: "z", ctrlKey: true });
    expect(recorded.filter((entry) => entry.label === "POST undo")).toHaveLength(1);
    // Ctrl+Shift+Z → redo（等重做按钮解禁，can_redo=true 来自 undo 响应）。
    await waitFor(() => expect((query(container, '[aria-label="重做"]') as HTMLButtonElement).disabled).toBe(false));
    fireEvent.keyDown(window, { key: "z", ctrlKey: true, shiftKey: true });
    await waitFor(() => expect(findRecorded("POST redo")).toBeTruthy());
  });

  it("原生滚动写回视图状态并防抖 PUT（scroll_left 持久化）", async () => {
    const { container } = await mountWiredPage();
    const scrollBox = query(container, ".timeline-scroll") as HTMLElement;
    expect(scrollBox).toBeTruthy();
    // jsdom 不做真实布局：用可写的实例属性盖住原型上的 scrollLeft（组件回写也依赖可写）。
    Object.defineProperty(scrollBox, "scrollLeft", { configurable: true, writable: true, value: 80 });
    fireEvent.scroll(scrollBox);
    await waitFor(() => {
      const puts = recorded.filter((entry) => entry.label === "PUT view");
      expect(puts.some((entry) => (entry.body as { scroll_left: number }).scroll_left === 80)).toBe(true);
    }, { timeout: 2000 });
  });
});
