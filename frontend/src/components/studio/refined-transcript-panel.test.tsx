/**
 * 右侧「切片精修文案」面板（refined-transcript-panel）组件测试。
 *
 * 锁定（2026-11-05 用户需求）：
 *  - 表头固定四列：# / 时间码 / 角色 / 上屏字幕；时间为切片相对时间（00:00–00:02）；
 *  - fallback 如实标注「原始纠错转写」，无时间码行显示 —（绝不编造）；
 *    空角色显示「未标注」；清单/程序留痕折叠；markdown 下载链接指向同源端点；
 *  - 加载 / 404 / 错误分态诚实展示；切换切片丢弃迟到响应（防串片）。
 * 数据用 MSW 拦截，不触网。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import RefinedTranscriptPanel, { formatRelativeTimecode } from "./refined-transcript-panel";
import { server } from "@/test/setup";

const API = "/video-api";
const endpoint = (taskId: string, clipId: string) =>
  `${API}/studio/auto-edit/${taskId}/clips/${clipId}/refined-transcript`;

const llmPayload = {
  task_id: "autoedit_t1", clip_id: "clip-abc1234567", theme: "误区一：规范治疗",
  source: "llm", fallback_reason: null, prompt_version: "refine/v1", model: "fixture",
  rows: [
    { index: 1, start_ms: 0, end_ms: 5500, speaker: "医生", text: "确诊高血压就要规范治疗" },
    { index: 2, start_ms: 5500, end_ms: 13000, speaker: "", text: "别信偏方" },
  ],
  warnings: ["w1"], checklist_pending: ["原音存疑：剂量"], checklist_compliance: ["删除绝对化用语"],
  ending_cards: ["遵医嘱，因人而异"],
};

describe("formatRelativeTimecode", () => {
  it("毫秒 → mm:ss（负值钳 0）", () => {
    expect(formatRelativeTimecode(0)).toBe("00:00");
    expect(formatRelativeTimecode(5500)).toBe("00:05");
    expect(formatRelativeTimecode(13000)).toBe("00:13");
    expect(formatRelativeTimecode(125000)).toBe("02:05");
    expect(formatRelativeTimecode(-300)).toBe("00:00");
  });
});

describe("RefinedTranscriptPanel：LLM 精修态", () => {
  it("四列表头 + 相对时间码 + 空角色未标注 + 折叠清单 + markdown 下载", async () => {
    server.use(http.get(endpoint("autoedit_t1", "clip-abc1234567"), () =>
      HttpResponse.json(llmPayload)));
    render(<RefinedTranscriptPanel taskId="autoedit_t1" clipId="clip-abc1234567" apiBase={API} />);

    const table = await screen.findByRole("table");
    const headers = [...table.querySelectorAll("thead th")].map((th) => th.textContent);
    expect(headers).toEqual(["#", "时间码", "角色", "上屏字幕"]);
    expect(screen.getByText("模型精修")).toBeTruthy();
    expect(screen.getByText(/误区一：规范治疗/)).toBeTruthy();
    const timeCells = [...table.querySelectorAll("tbody .refined-time")].map((td) => td.textContent);
    expect(timeCells).toEqual(["00:00–00:05", "00:05–00:13"]);
    expect(table.querySelector(".refined-speaker.doctor")?.textContent).toBe("医生");
    expect(table.querySelector(".refined-speaker:not(.doctor):not(.patient)")?.textContent).toBe("未标注");
    // 清单默认折叠（details 无 open），展开后可见
    const pending = screen.getByText("待核对清单（1）");
    expect(pending.closest("details")?.open).toBe(false);
    fireEvent.click(pending);
    expect(screen.getByText("原音存疑：剂量")).toBeTruthy();
    expect(screen.getByText("结尾文字卡建议（1）")).toBeTruthy();
    expect(screen.getByText("程序留痕（1）")).toBeTruthy();
    const download = screen.getByText("下载精修文案（markdown）") as HTMLAnchorElement;
    expect(download.getAttribute("href")).toBe(
      `${API}/studio/auto-edit/autoedit_t1/clips/clip-abc1234567/refined-copy.md`);
  });
});

describe("RefinedTranscriptPanel：fallback 与空行", () => {
  it("fallback 标注原始纠错转写，无时间码显示 —", async () => {
    server.use(http.get(endpoint("autoedit_t2", "clip-fb"), () => HttpResponse.json({
      ...llmPayload, task_id: "autoedit_t2", clip_id: "clip-fb", source: "fallback",
      fallback_reason: "llm_unavailable",
      rows: [{ index: 1, start_ms: null, end_ms: null, speaker: "", text: "原文一" }],
    })));
    render(<RefinedTranscriptPanel taskId="autoedit_t2" clipId="clip-fb" apiBase={API} />);
    expect(await screen.findByText("原始纠错转写")).toBeTruthy();
    expect(screen.getByText(/llm_unavailable/)).toBeTruthy();
    expect(screen.getByText("—")).toBeTruthy();
    expect(screen.getByText("未标注")).toBeTruthy();
    expect(screen.getByText("原文一")).toBeTruthy();
  });

  it("rows 为空时显示空态，不编造行", async () => {
    server.use(http.get(endpoint("autoedit_t3", "clip-empty"), () => HttpResponse.json({
      ...llmPayload, task_id: "autoedit_t3", clip_id: "clip-empty",
      rows: [], checklist_pending: [], checklist_compliance: [], ending_cards: [], warnings: [],
    })));
    render(<RefinedTranscriptPanel taskId="autoedit_t3" clipId="clip-empty" apiBase={API} />);
    expect(await screen.findByText("该切片没有可展示的精修行")).toBeTruthy();
  });
});

describe("RefinedTranscriptPanel：诚实分态与防串片", () => {
  it("404 → 精修产物不存在提示（成片字幕不受影响）", async () => {
    server.use(http.get(endpoint("autoedit_old", "clip-x"), () =>
      HttpResponse.json({ detail: "精修产物不存在（任务可能早于两步制运行，重新规划后可用）" },
        { status: 404 })));
    render(<RefinedTranscriptPanel taskId="autoedit_old" clipId="clip-x" apiBase={API} />);
    expect(await screen.findByText(/精修产物不存在/)).toBeTruthy();
    expect(screen.getByText(/成片字幕不受影响/)).toBeTruthy();
  });

  it("非 404 错误 → 错误态 + 重试可恢复", async () => {
    server.use(
      http.get(endpoint("autoedit_e", "clip-e"), () =>
        new HttpResponse("boom", { status: 500 }), { once: true }),
      http.get(endpoint("autoedit_e", "clip-e"), () => HttpResponse.json({
        ...llmPayload, task_id: "autoedit_e", clip_id: "clip-e" })));
    render(<RefinedTranscriptPanel taskId="autoedit_e" clipId="clip-e" apiBase={API} />);
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("HTTP 500");
    fireEvent.click(screen.getByText("重试"));
    expect(await screen.findByText("确诊高血压就要规范治疗")).toBeTruthy();
  });

  it("切换切片丢弃迟到响应：只渲染新切片内容", async () => {
    let resolveFirst: (value: Response) => void = () => {};
    server.use(
      http.get(endpoint("autoedit_s", "clip-a"), () => new Promise<Response>((resolve) => {
        resolveFirst = resolve;
      })),
      http.get(endpoint("autoedit_s", "clip-b"), () => HttpResponse.json({
        ...llmPayload, task_id: "autoedit_s", clip_id: "clip-b", rows: [
          { index: 1, start_ms: 0, end_ms: 2000, speaker: "医生", text: "这是切片B的内容" }],
      })));
    const { rerender } = render(
      <RefinedTranscriptPanel taskId="autoedit_s" clipId="clip-a" apiBase={API} />);
    // 切到 clip-b 并完成渲染后，clip-a 的迟到响应才到达：载荷是**真实的 clip-a
    // 数据**（id 与其自身请求一致），只能靠组件的 superseded 守卫拦下——
    // 这正是 abort 失效场景下的主保险。
    rerender(<RefinedTranscriptPanel taskId="autoedit_s" clipId="clip-b" apiBase={API} />);
    expect(await screen.findByText("这是切片B的内容")).toBeTruthy();
    resolveFirst(HttpResponse.json({
      ...llmPayload, task_id: "autoedit_s", clip_id: "clip-a", rows: [
        { index: 1, start_ms: 0, end_ms: 900, speaker: "医生", text: "迟到的切片A内容" }],
    }));
    await waitFor(() => new Promise((r) => setTimeout(r, 0)));
    expect(screen.queryByText("迟到的切片A内容")).toBeNull();
    expect(screen.getByText("这是切片B的内容")).toBeTruthy();
  });

  it("迟到响应 clip_id 与当前不一致时丢弃并显示错误（abort 之外的双保险）", async () => {
    server.use(http.get(endpoint("autoedit_g", "clip-now"), () => HttpResponse.json({
      ...llmPayload, clip_id: "clip-other", rows: [
        { index: 1, start_ms: 0, end_ms: 1000, speaker: "", text: "串片内容不应出现" }],
    })));
    render(<RefinedTranscriptPanel taskId="autoedit_g" clipId="clip-now" apiBase={API} />);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("与当前切片不一致");
    expect(screen.queryByText("串片内容不应出现")).toBeNull();
  });
});
