/**
 * 封面微调面板（2026-11-09 P3）契约测试。
 *
 * 锁定四条：候选帧加载与缩略图条、点帧 → POST frame_time_rel_s、
 * 候选文案点选 → POST copy_override、无 taskId/clipId 不渲染。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, fireEvent, screen, waitFor } from "@testing-library/react";
import { CoverTunePanel } from "./cover-tune-panel";

const FRAMES_PAYLOAD = {
  timeline: "clip_final",
  selected_time_rel_s: 9,
  frames: [
    { file: "frame-00.jpg", time_rel_s: 9, score: 8.56, face_count: 1, brightness_mean: 136.7, selected: true, thumb_url: "/video-api/studio/auto-edit/job-1/clips/clip-1/cover/frame-candidates/frame-00.jpg" },
    { file: "frame-01.jpg", time_rel_s: 3, score: 8.49, face_count: 1, brightness_mean: 137.3, selected: false, thumb_url: "/video-api/studio/auto-edit/job-1/clips/clip-1/cover/frame-candidates/frame-01.jpg" },
  ],
};

const COPIES_PAYLOAD = {
  task_id: "job-1",
  clip_id: "clip-1",
  final: { topic: "基因检测解读", hook: "查看基因检测结果" },
  candidates: [
    { topic: "基因检测目的", hook: "做基因检测的目的", segment_ids: ["seg_0182"], source: "llm" },
    { topic: "基因检测解读", hook: "查看基因检测结果", segment_ids: ["seg_0187"], source: "llm" },
  ],
  validation_report: { ok: true },
  transcript_source: "refined",
};

const jsonResponse = (payload: unknown, status = 200) => ({
  ok: status < 400,
  status,
  json: async () => payload,
});

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const fetchMockFor = (handlers: Array<(url: string, init?: RequestInit) => any>) => {
  const mock = vi.fn(async (url: string, init?: RequestInit) => {
    for (const handler of handlers) {
      const matched = handler(url, init);
      if (matched) return matched;
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", mock);
  return mock;
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("CoverTunePanel", () => {
  it("无 taskId/clipId 时不渲染", () => {
    const { container } = render(<CoverTunePanel taskId={null} clipId={null} />);
    expect(container.querySelector('[data-testid="cover-tune-panel"]')).toBeNull();
  });

  it("加载候选帧 → 渲染缩略图条；点帧 POST frame_time_rel_s 并回调", async () => {
    const fetchMock = fetchMockFor([
      (url) => (url.includes("/cover/frame-candidates") && !url.endsWith(".jpg")
        ? jsonResponse(FRAMES_PAYLOAD) : null),
      (url, init) => (url.endsWith("/cover") && init?.method === "POST"
        ? jsonResponse({ cover_revision: 5 }) : null),
    ]);
    const onReplaced = vi.fn();
    render(<CoverTunePanel taskId="job-1" clipId="clip-1" onReplaced={onReplaced} />);
    fireEvent.click(screen.getByTestId("cover-tune-load-frames"));
    await waitFor(() => expect(screen.getByTestId("cover-tune-frames")).toBeInTheDocument());
    expect(screen.getAllByRole("img")).toHaveLength(2);
    fireEvent.click(screen.getByTestId("cover-tune-frame-frame-01.jpg"));
    await waitFor(() => expect(onReplaced).toHaveBeenCalled());
    const [postUrl, postInit] = fetchMock.mock.calls.find(([, init]) => init?.method === "POST") as [string, RequestInit];
    expect(postUrl).toContain("/studio/auto-edit/job-1/clips/clip-1/cover");
    expect(JSON.parse(String(postInit.body)).frame_time_rel_s).toBe(3);
    expect(onReplaced).toHaveBeenCalledWith(5, expect.stringContaining("3.0s"));
  });

  it("加载候选文案 → 点选 POST copy_override（当前文案高亮）", async () => {
    const fetchMock = fetchMockFor([
      (url) => (url.includes("/cover/copy-candidates") ? jsonResponse(COPIES_PAYLOAD) : null),
      (url, init) => (url.endsWith("/cover") && init?.method === "POST"
        ? jsonResponse({ cover_revision: 6 }) : null),
    ]);
    const onReplaced = vi.fn();
    render(<CoverTunePanel taskId="job-1" clipId="clip-1" onReplaced={onReplaced} />);
    fireEvent.click(screen.getByTestId("cover-tune-load-copies"));
    await waitFor(() => expect(screen.getByTestId("cover-tune-copies")).toBeInTheDocument());
    expect(screen.getByTestId("cover-tune-copy-1").className).toContain("active"); // 当前文案高亮
    fireEvent.click(screen.getByTestId("cover-tune-copy-0"));
    await waitFor(() => expect(onReplaced).toHaveBeenCalled());
    const [, postInit] = fetchMock.mock.calls.find(([, init]) => init?.method === "POST") as [string, RequestInit];
    const body = JSON.parse(String(postInit.body));
    expect(body.copy_override).toEqual({
      topic: "基因检测目的", hook: "做基因检测的目的", segment_ids: ["seg_0182"],
    });
    expect(body.replace_intro).toBe(true);
  });

  it("接口失败 → 显示错误不抛出", async () => {
    fetchMockFor([
      (url) => (url.includes("/cover/frame-candidates") ? jsonResponse({ detail: { message: "成片不存在" } }, 404) : null),
    ]);
    render(<CoverTunePanel taskId="job-1" clipId="clip-1" />);
    fireEvent.click(screen.getByTestId("cover-tune-load-frames"));
    await waitFor(() => expect(screen.getByTestId("cover-tune-error")).toHaveTextContent("成片不存在"));
  });
});
