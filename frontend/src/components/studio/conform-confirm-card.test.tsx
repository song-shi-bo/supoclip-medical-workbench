/**
 * 对稿成片人工确认卡（conform-confirm-card）组件测试。
 *
 * 锁定（P2b 2026-11-25）：
 *  - 报告加载：覆盖率/预计时长/逐句 diff（绿 matched / 缺拍提示）可见；
 *  - EDL 决策账折叠可见（金句保留 vs 剪除着色区分）；
 *  - 确认渲染：POST approve 后轮询任务状态到 completed → 展示成片就绪；
 *  - 报告 404 → 错误分态诚实展示。
 * 数据用 MSW 拦截，不触网。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import ConformConfirmCard from "./conform-confirm-card";
import { server } from "@/test/setup";

const API = "/video-api";
const reportEndpoint = (jobId: string) => `${API}/api/jobs/conform/${jobId}/report`;
const approveEndpoint = (jobId: string) => `${API}/api/jobs/conform/${jobId}/approve`;
const jobEndpoint = (jobId: string) => `${API}/api/jobs/${jobId}`;

const reportPayload = {
  job_id: "job-conf-1",
  job_status: "conform_ready",
  conform_approved: false,
  alignment: {
    coverage_ratio: 0.8,
    script_sentences: { total: 5, matched: 4, missing: [3] },
    sentences: [
      { index: 0, status: "matched", text: "免疫治疗出现副作用，先别急着停药。", start_ms: 0, end_ms: 4000 },
      { index: 3, status: "missing", text: "其实是大多数副作用可控的。", start_ms: null, end_ms: null },
    ],
    extra_segments: [{ segment_id: 99, start_ms: 6000, end_ms: 8000, text: "顺便说一句下周有义诊。" }],
    warnings: [],
    review: { engine: "llm:test", verdicts: [{ segment_id: "99", classification: "offtopic" }] },
  },
  edl: {
    output_duration_ms: 42000,
    cut_ratio: 0.18,
    redline: { protected_segments: 2 },
    stats: { filler_cuts: 3, gap_cuts: 4, repeat_cuts: 1, extra_cuts: 1 },
    warnings: [],
    review_engine: "llm:test",
    decisions: [
      { type: "extra", start_ms: 6000, end_ms: 8000, action: "cut", reason: "LLM 分级 offtopic：推广预告", text: "义诊" },
    ],
  },
};

describe("ConformConfirmCard", () => {
  it("渲染覆盖率、逐句 diff 与决策账", async () => {
    server.use(
      http.get(reportEndpoint("job-conf-1"), () => HttpResponse.json(reportPayload)),
    );
    render(<ConformConfirmCard jobId="job-conf-1" />);
    await waitFor(() => expect(screen.getByText(/文稿覆盖率/)).toBeDefined());
    expect(screen.getByText(/80%/)).toBeDefined();
    expect(screen.getByText(/✓：免疫治疗出现副作用，先别急着停药。/)).toBeDefined();
    expect(screen.getByText(/✗ 缺拍（建议补拍）：其实是大多数副作用可控的。/)).toBeDefined();
    expect(screen.getByText(/LLM 分级 offtopic：推广预告/)).toBeDefined();
  });

  it("确认渲染：POST approve 后轮询到完成", async () => {
    let approved = false;
    server.use(
      http.get(reportEndpoint("job-conf-1"), () => HttpResponse.json(reportPayload)),
      http.post(approveEndpoint("job-conf-1"), () => {
        approved = true;
        return HttpResponse.json({ ok: true, status: "queued" });
      }),
      http.get(jobEndpoint("job-conf-1"), () =>
        HttpResponse.json(approved ? { status: "completed" } : { status: "queued" })),
    );
    render(<ConformConfirmCard jobId="job-conf-1" />);
    const button = await screen.findByRole("button", { name: "确认渲染" });
    fireEvent.click(button);
    await waitFor(() => expect(approved).toBe(true), { timeout: 5000 });
    await waitFor(() => expect(screen.getByText(/成片已完成，可在播放器中预览/)).toBeDefined(),
      { timeout: 6000 });
  });

  it("报告 404 → 错误分态", async () => {
    server.use(
      http.get(reportEndpoint("job-missing"), () =>
        HttpResponse.json({ detail: "对齐报告尚未生成" }, { status: 404 })),
    );
    render(<ConformConfirmCard jobId="job-missing" />);
    await waitFor(() => expect(screen.getByText(/对稿报告加载失败/)).toBeDefined());
  });
});
