import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createTopicsApi,
  groupTopicsByStatus,
  SCRIPT_STATUS_LABELS,
  TOPIC_STATUS_LABELS,
  topicsPath,
  type TopicItem,
} from "./topics-api";

afterEach(() => { vi.unstubAllGlobals(); });

const topic = (overrides: Partial<TopicItem>): TopicItem => ({
  id: "t1",
  title: "测试选题",
  summary: "",
  status: "new",
  brief: {},
  core_conclusion: "",
  risk_boundary: "",
  prerequisites: [],
  forbidden_expr: [],
  approved_by: null,
  approved_at: null,
  heat_score: null,
  source_platform: null,
  source_url: null,
  missing_required: [],
  created_at: null,
  updated_at: null,
  ...overrides,
});

describe("topic board facts", () => {
  it("groups topics into kanban columns and parks unknown statuses in archived", () => {
    const grouped = groupTopicsByStatus([
      topic({ id: "a", status: "new" }),
      topic({ id: "b", status: "doctor_approved" }),
      topic({ id: "c", status: "not_a_status" }),
    ]);
    expect(grouped.new.map((t) => t.id)).toEqual(["a"]);
    expect(grouped.doctor_approved.map((t) => t.id)).toEqual(["b"]);
    // 未知状态不能丢卡片：归入 archived 列
    expect(grouped.archived.map((t) => t.id)).toEqual(["c"]);
    expect(grouped.briefed).toEqual([]);
  });

  it("keeps Chinese labels for every status so the board never shows raw keys", () => {
    for (const status of Object.keys(TOPIC_STATUS_LABELS)) {
      expect(TOPIC_STATUS_LABELS[status as keyof typeof TOPIC_STATUS_LABELS]).not.toEqual(status);
    }
    expect(SCRIPT_STATUS_LABELS.draft).toBe("草稿");
  });

  it("builds api paths under the same-origin video-api prefix", () => {
    expect(topicsPath("/video-api")).toBe("/video-api/api/v1/topics");
    expect(topicsPath("/video-api/")).toBe("/video-api/api/v1/topics");
  });
});

describe("topics api client", () => {
  it("lists topics and surfaces backend detail as TopicsApiError", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ total: 1, items: [topic({ id: "t1" })] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "status scheduled 不允许生稿（须先过医生审批）" }), { status: 409 }));
    vi.stubGlobal("fetch", fetchMock);

    const api = createTopicsApi("/video-api");
    const listed = await api.list("", "", 200);
    expect(listed.total).toBe(1);
    expect(fetchMock.mock.calls[0][0]).toBe("/video-api/api/v1/topics?limit=200");

    await expect(api.generateScript("t1")).rejects.toMatchObject({
      name: "TopicsApiError",
      status: 409,
      message: "status scheduled 不允许生稿（须先过医生审批）",
    });
  });

  it("flattens 422 hard-validation payloads into one readable message", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: { message: "生稿未通过硬校验（已重试一次）", violations: ["命中禁止表达：自行停药没问题"] } }),
      { status: 422 },
    )));
    const api = createTopicsApi("/video-api");
    await expect(api.generateScript("t1")).rejects.toMatchObject({
      status: 422,
      message: "生稿未通过硬校验（已重试一次）：命中禁止表达：自行停药没问题",
    });
  });

  it("link-job posts the studio job id", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true, script_status: "uploaded" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const api = createTopicsApi("/video-api");
    const result = await api.linkJob("s1", "job-9");
    expect(result.script_status).toBe("uploaded");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/video-api/api/v1/topics/scripts/s1/link-job");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ job_id: "job-9" });
  });
});
