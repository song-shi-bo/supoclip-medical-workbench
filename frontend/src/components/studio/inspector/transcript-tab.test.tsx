// 转写标签页测试（契约 §3.2 / §5）：
// 搜索命中计数与高亮、点分段的 seek 回调、改文本 → POST 修订（断言请求体）、
// 三态切换（原始/纠错/有效，编辑控件只在有效稿可用）、修订历史与回滚。

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/setup";

import { TranscriptTab } from "./transcript-tab";
import { CLIP_ID, PROJECT_ID, clipUrl, inspectorHandlers } from "./test-utils";

// 用 setup.ts 提供的全局 msw server（已在 beforeAll 里 listen，afterEach 里
// resetHandlers）：同一个进程里二次 setupServer()/listen() 会让请求体被消费两次
// （msw 打印 "Body is unusable"，用例仍通过但输出被污染），所以这里只注册合同
// 端点的默认 handler，不再自己建 server。
beforeEach(() => server.use(...inspectorHandlers()));

function renderTab(overrides: Partial<Parameters<typeof TranscriptTab>[0]> = {}) {
  const handlers = {
    onSeek: vi.fn(),
    onChanged: vi.fn(),
    onEffectiveSegments: vi.fn(),
  };
  const rendered = render(<TranscriptTab
    projectId={PROJECT_ID}
    clipId={CLIP_ID}
    baseUrl=""
    fallbackSegments={[]}
    {...handlers}
    {...overrides}
  />);
  return { handlers, ...rendered };
}

describe("TranscriptTab", () => {
  it("显示「原始 ASR 不会被覆盖」提示、修订号与三态切换", async () => {
    server.use(...inspectorHandlers());
    renderTab();
    expect(screen.getByTestId("inspector-asr-notice").textContent).toContain("原始 ASR 不会被覆盖");
    await waitFor(() => expect(screen.getByTestId("inspector-transcript-revision").textContent).toContain("v1"));
    expect(screen.getByTestId("inspector-variant-raw")).toBeInTheDocument();
    expect(screen.getByTestId("inspector-variant-corrected")).toBeInTheDocument();
    expect(screen.getByTestId("inspector-variant-effective").getAttribute("aria-pressed")).toBe("true");
  });

  it("点分段 → seek 到该段起点（不写任何后端）", async () => {
    server.use(...inspectorHandlers());
    const { handlers } = renderTab();
    await waitFor(() => expect(screen.getByTestId("inspector-segment-seg-2")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("inspector-segment-seg-2"));
    expect(handlers.onSeek).toHaveBeenCalledWith(2);
  });

  it("关键词搜索：计数 当前/总数、命中高亮、下一个/上一个导航", async () => {
    server.use(...inspectorHandlers());
    const { handlers } = renderTab();
    await waitFor(() => expect(screen.getByTestId("inspector-segment-seg-1")).toBeInTheDocument());

    const input = screen.getByLabelText("搜索转写内容");
    fireEvent.change(input, { target: { value: "血压" } });
    // seg-1 命中 1 次，seg-2 命中 1 次 → 1/2。
    expect(screen.getByTestId("inspector-hit-count").textContent).toBe("1/2");

    const segmentOne = screen.getByTestId("inspector-segment-seg-1");
    expect(segmentOne.querySelectorAll("mark")).toHaveLength(1);
    expect(segmentOne.querySelector("mark")?.className).toContain("active");
    expect(segmentOne.querySelector("mark")?.textContent).toBe("血压");

    fireEvent.click(screen.getByRole("button", { name: "下一个命中" }));
    expect(screen.getByTestId("inspector-hit-count").textContent).toBe("2/2");
    expect(handlers.onSeek).toHaveBeenLastCalledWith(2);

    fireEvent.click(screen.getByRole("button", { name: "上一个命中" }));
    expect(screen.getByTestId("inspector-hit-count").textContent).toBe("1/2");
    expect(handlers.onSeek).toHaveBeenLastCalledWith(0);
  });

  it("无命中时计数为 0/0 且导航按钮禁用", async () => {
    server.use(...inspectorHandlers());
    renderTab();
    await waitFor(() => expect(screen.getByTestId("inspector-segment-seg-1")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("搜索转写内容"), { target: { value: "不存在的词" } });
    expect(screen.getByTestId("inspector-hit-count").textContent).toBe("0/0");
    expect(screen.getByRole("button", { name: "下一个命中" })).toBeDisabled();
  });

  it("改一段文本 → POST 修订（断言请求体）并显示 修订 v2", async () => {
    let posted: Record<string, unknown> | null = null;
    server.use(
      http.post(clipUrl("transcript/revisions"), async ({ request }) => {
        posted = JSON.parse(await request.text()) as Record<string, unknown>;
        return HttpResponse.json({
          project_id: PROJECT_ID,
          clip_id: CLIP_ID,
          transcript_revision: 2,
          approved_revision: 2,
          raw_segments: [],
          corrected_segments: [],
          effective_segments: [{ segment_id: "seg-2", start: 2, end: 4, text: "血压高要注意饮食与作息。" }],
          revisions: [],
        });
      }),
    );
    const { handlers } = renderTab();
    await waitFor(() => expect(screen.getByTestId("inspector-segment-seg-2")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "编辑分段 seg-2" }));
    fireEvent.change(screen.getByLabelText("分段文本 seg-2"), { target: { value: "血压高要注意饮食与作息。" } });
    fireEvent.click(screen.getByRole("button", { name: /保存修订/ }));

    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toMatchObject({
      author: "editor",
      reason: "手动纠错",
      changes: [{ segment_id: "seg-2", before: "血压高要注意饮食。", after: "血压高要注意饮食与作息。" }],
    });
    await waitFor(() => expect(screen.getByTestId("inspector-transcript-revision").textContent).toContain("v2"));
    expect(screen.getAllByTestId("inspector-save").some((badge) => badge.getAttribute("data-status") === "saved")).toBe(true);
    expect(handlers.onChanged).toHaveBeenCalledWith({ revision: 2 });
  });

  it("改说话人 → 也写成一条修订（speaker_before/after）", async () => {
    let posted: Record<string, unknown> | null = null;
    server.use(
      http.post(clipUrl("transcript/revisions"), async ({ request }) => {
        posted = JSON.parse(await request.text()) as Record<string, unknown>;
        return HttpResponse.json({
          project_id: PROJECT_ID, clip_id: CLIP_ID, transcript_revision: 3, approved_revision: 3,
          raw_segments: [], corrected_segments: [], effective_segments: [], revisions: [],
        });
      }),
    );
    renderTab();
    await waitFor(() => expect(screen.getByTestId("inspector-segment-seg-2")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("说话人 seg-2"), { target: { value: "患者" } });
    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toMatchObject({ changes: [{ segment_id: "seg-2", speaker_before: "未标注", speaker_after: "患者" }] });
  });

  it("三态切换：原始稿只读（没有编辑按钮）且重新按变体拉取", async () => {
    const requested: string[] = [];
    server.use(
      http.get(clipUrl("transcript/revisions"), ({ request }) => {
        requested.push(new URL(request.url).searchParams.get("view") || "effective");
        return HttpResponse.json({
          project_id: PROJECT_ID, clip_id: CLIP_ID, transcript_revision: 1, approved_revision: 1,
          raw_segments: [{ segment_id: "seg-1", start: 0, end: 2, text: "ASR 原文", speaker: "未标注" }],
          corrected_segments: [{ segment_id: "seg-1", start: 0, end: 2, text: "纠错后", speaker: "未标注" }],
          effective_segments: [{ segment_id: "seg-1", start: 0, end: 2, text: "有效稿", speaker: "未标注" }],
          revisions: [],
        });
      }),
    );
    renderTab();
    await waitFor(() => expect(screen.getByText("有效稿")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("inspector-variant-raw"));
    await waitFor(() => expect(screen.getByText("ASR 原文")).toBeInTheDocument());
    expect(requested).toContain("raw");
    // 原始稿不可编辑：没有「编辑分段」按钮。
    expect(screen.queryByRole("button", { name: "编辑分段 seg-1" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("inspector-variant-corrected"));
    await waitFor(() => expect(screen.getByText("纠错后")).toBeInTheDocument());
    expect(requested).toContain("corrected");
  });

  it("修订历史：列出 v1 并可回滚（POST rollback 带 revision）", async () => {
    let rolled: Record<string, unknown> | null = null;
    server.use(
      http.post(clipUrl("transcript/rollback"), async ({ request }) => {
        rolled = JSON.parse(await request.text()) as Record<string, unknown>;
        return HttpResponse.json({ project_id: PROJECT_ID, clip_id: CLIP_ID, transcript_revision: 1, approved_revision: 1, raw_segments: [], corrected_segments: [], effective_segments: [], revisions: [] });
      }),
    );
    renderTab();
    await waitFor(() => expect(screen.getByTestId("inspector-segment-seg-1")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("inspector-toggle-history"));
    const history = screen.getByTestId("inspector-transcript-history");
    expect(within(history).getByTestId("inspector-revision-1")).toBeInTheDocument();
    fireEvent.click(within(history).getByRole("button", { name: "回滚到 v1" }));
    await waitFor(() => expect(rolled).toEqual({ revision: 1 }));
  });

  it("转写端点不可用 → 退回页面分段并显示只读原因（不伪造修订历史）", async () => {
    server.use(
      http.get(clipUrl("transcript/revisions"), () => HttpResponse.json({ detail: "E_TRANSCRIPT_NOT_SEEDED: 尚未建立转写文档" }, { status: 404 })),
    );
    renderTab({ fallbackSegments: [{ segment_id: "seg-9", start: 1, end: 3, text: "页面分段" }] });
    const notice = await screen.findByTestId("inspector-transcript-fallback");
    expect(notice.textContent).toContain("尚未建立转写文档");
    expect(screen.getByText("页面分段")).toBeInTheDocument();
    expect(screen.getByTestId("inspector-transcript-revision").textContent).toContain("v0");
  });
});
