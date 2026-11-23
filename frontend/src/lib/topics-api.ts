// 选题卡（阶段3 M4）—— typed API client。
// 对应后端 api/routes/topics.py；路径/方法/请求体逐字对应：
//   GET    /api/v1/topics                       列表（status 逗号分隔 / q / limit / offset）
//   GET    /api/v1/topics/scripts               文稿列表（上传「关联文稿」选择器）
//   GET    /api/v1/topics/{topic_id}            详情（含关联文稿）
//   POST   /api/v1/topics/{topic_id}/brief      保存选题卡
//   POST   /api/v1/topics/{topic_id}/brief/draft 拆解草稿（LLM，不落库）
//   POST   /api/v1/topics/{topic_id}/approve    选题卡医学审批（医生白名单）
//   POST   /api/v1/topics/{topic_id}/generate-script 生稿（硬校验+审批前置）
//   POST   /api/v1/topics/scripts/{script_id}/approve 文稿审批
//   POST   /api/v1/topics/scripts/{script_id}/link-job 拍摄上传挂链
//   GET/POST/DELETE /api/v1/topics/reviewers    审批白名单（仅管理员）

/** 选题卡状态机（与后端 topic_pipeline.py 常量一致）。看板列顺序即流转方向。 */
export const TOPIC_STATUSES = [
  "new",
  "briefed",
  "doctor_approved",
  "scheduled",
  "used",
  "archived",
] as const;

export type TopicStatus = (typeof TOPIC_STATUSES)[number];

export const TOPIC_STATUS_LABELS: Record<TopicStatus, string> = {
  new: "待拆解",
  briefed: "待医生审批",
  doctor_approved: "已批准",
  scheduled: "已排期（已生稿）",
  used: "已拍摄",
  archived: "已弃用",
};

export const SCRIPT_ORIGIN_LABELS: Record<string, string> = {
  transcript_reference: "参考稿",
  shooting_script: "原创稿",
};

export const SCRIPT_STATUS_LABELS: Record<string, string> = {
  draft: "草稿",
  approved: "医生已批",
  shooting: "拍摄中",
  uploaded: "素材已传",
  rendered: "已出片",
  published: "已发布",
  archived: "已弃用",
};

export interface TopicBrief {
  user_question?: string;
  suggested_duration_seconds?: number;
  target_audience?: string;
  content_type?: string;
  suggested_hook_type?: string;
  suggested_structure?: string;
  suggested_cta?: string;
  business_goal?: string;
  [key: string]: unknown;
}

export interface TopicItem {
  id: string;
  title: string;
  summary: string;
  status: string;
  brief: TopicBrief;
  core_conclusion: string;
  risk_boundary: string;
  prerequisites: string[];
  forbidden_expr: string[];
  approved_by: string | null;
  approved_at: string | null;
  heat_score: number | null;
  source_platform: string | null;
  source_url: string | null;
  missing_required: string[];
  /** 仅 saveBrief 响应携带：审批后改卡会被重置回待审批。 */
  reapproval_required?: boolean;
  created_at: string | null;
  updated_at: string | null;
  scripts?: ScriptItem[];
}

export interface ScriptItem {
  id: string;
  topic_id: string;
  topic_title?: string | null;
  origin?: string;
  version: number;
  title: string;
  status: string;
  task_id: string | null;
  created_by: string | null;
  created_at: string | null;
}

export interface ReviewerItem {
  user_id: string;
  name: string | null;
  email: string | null;
  created_at: string | null;
}

export interface BriefPayload {
  brief: TopicBrief;
  core_conclusion: string;
  risk_boundary: string;
  prerequisites: string[];
  forbidden_expr: string[];
}

export interface BriefDraftResult {
  topic_id: string;
  draft: {
    target_audience: string;
    content_type: string;
    user_question: string;
    core_conclusion: string;
    prerequisites: string[];
    suggested_hook_type: string;
    suggested_duration_seconds: number;
    suggested_structure: string;
    suggested_cta: string;
    key_points: string[];
  };
  note: string;
}

export interface GenerateScriptResult {
  ok: true;
  script_id: string;
  version: number;
  topic_status: string;
  draft: {
    title: string;
    hook: string;
    body: string;
    cta: string;
    shooting_notes: string;
    refs: string[];
  };
  warnings: string[];
}

export interface ScriptDetail extends ScriptItem {
  hook: string;
  body: string;
  cta: string;
  shooting_notes: string;
  refs: string[];
}

/** 修改沉淀 9 类归因（与后端 REASON_CLASSES 一致）。 */
export const REASON_CLASS_LABELS: Record<string, string> = {
  asr_error: "ASR错误",
  medical_term: "医学术语",
  premise_missing: "前提缺失",
  conclusion_rigor: "医学结论不严谨",
  privacy: "隐私",
  subtitle_style: "字幕表达",
  slice_boundary: "切片边界",
  stitching: "拼接问题",
  hook_issue: "钩子问题",
};

export interface ModificationInput {
  topic_id?: string;
  script_id?: string;
  reason_class: string;
  note?: string;
  payload?: Record<string, unknown>;
}

export interface ModificationItem {
  id: string;
  topic_id: string | null;
  script_id: string | null;
  reason_class: string;
  label: string;
  note: string;
  created_by: string;
  routing_target: string;
  routing_status: string;
  routing_detail: string;
  resolved: boolean;
  created_at: string | null;
}

/** 后端 detail 为可读中文（FastAPI HTTPException），原样透出给 UI。 */
export class TopicsApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "TopicsApiError";
    this.status = status;
  }
}

function parseDetail(payload: unknown, status: number): TopicsApiError {
  const detail =
    payload && typeof payload === "object" && "detail" in payload
      ? (payload as { detail?: unknown }).detail
      : null;
  if (typeof detail === "string" && detail.trim()) return new TopicsApiError(detail, status);
  if (detail && typeof detail === "object" && "message" in detail) {
    const message = String((detail as { message?: unknown }).message || "请求失败");
    const violations = (detail as { violations?: unknown }).violations;
    const suffix = Array.isArray(violations) ? `：${violations.join("；")}` : "";
    return new TopicsApiError(message + suffix, status);
  }
  return new TopicsApiError(`请求失败（HTTP ${status}）`, status);
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text) as unknown;
    } catch {
      payload = null;
    }
  }
  if (!response.ok) throw parseDetail(payload, response.status);
  return payload as T;
}

/** 看板列分组：未知状态归入 archived 列，避免卡片丢失。 */
export function groupTopicsByStatus(topics: TopicItem[]): Record<TopicStatus, TopicItem[]> {
  const grouped = {
    new: [],
    briefed: [],
    doctor_approved: [],
    scheduled: [],
    used: [],
    archived: [],
  } as Record<TopicStatus, TopicItem[]>;
  for (const topic of topics) {
    const key = (TOPIC_STATUSES as readonly string[]).includes(topic.status)
      ? (topic.status as TopicStatus)
      : "archived";
    grouped[key].push(topic);
  }
  return grouped;
}

export function topicsPath(base: string): string {
  return `${base.endsWith("/") ? base.slice(0, -1) : base}/api/v1/topics`;
}

export function createTopicsApi(baseUrl: string) {
  const base = topicsPath(baseUrl);
  return {
    async list(status = "", q = "", limit = 200): Promise<{ total: number; items: TopicItem[] }> {
      const params = new URLSearchParams();
      if (status) params.set("status", status);
      if (q) params.set("q", q);

      const items: TopicItem[] = [];
      let total = 0;
      do {
        const pageSize = Math.min(200, limit - items.length);
        params.set("limit", String(pageSize));
        if (items.length > 0) params.set("offset", String(items.length));
        const page = await requestJson<{ total: number; items: TopicItem[] }>(
          `${base}?${params.toString()}`,
        );
        total = page.total;
        items.push(...page.items);
        if (page.items.length < pageSize) break;
      } while (items.length < Math.min(limit, total));

      return { total, items };
    },
    get(topicId: string): Promise<TopicItem> {
      return requestJson(`${base}/${encodeURIComponent(topicId)}`);
    },
    saveBrief(topicId: string, payload: BriefPayload): Promise<TopicItem> {
      return requestJson(`${base}/${encodeURIComponent(topicId)}/brief`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
    },
    draftBrief(topicId: string): Promise<BriefDraftResult> {
      return requestJson(`${base}/${encodeURIComponent(topicId)}/brief/draft`, { method: "POST", body: "{}" });
    },
    approveTopic(topicId: string, extra?: { reason_class?: string; feedback?: string }): Promise<{ ok: true; status: string; approved_by: string }> {
      const body = JSON.stringify({ reason_class: extra?.reason_class ?? "", feedback: extra?.feedback ?? "" });
      return requestJson(`${base}/${encodeURIComponent(topicId)}/approve`, { method: "POST", body });
    },
    generateScript(topicId: string, doctorNotes = ""): Promise<GenerateScriptResult> {
      return requestJson(`${base}/${encodeURIComponent(topicId)}/generate-script`, {
        method: "POST",
        body: JSON.stringify({ doctor_notes: doctorNotes }),
      });
    },
    approveScript(scriptId: string, extra?: { reason_class?: string; feedback?: string }): Promise<{ ok: true; status: string }> {
      const body = JSON.stringify({ note: "", reason_class: extra?.reason_class ?? "", feedback: extra?.feedback ?? "" });
      return requestJson(`${base}/scripts/${encodeURIComponent(scriptId)}/approve`, {
        method: "POST",
        body,
      });
    },
    recordModification(input: ModificationInput): Promise<{ ok: true; label: string; routing_target: string; routing_status: string; routing_detail: string }> {
      return requestJson(`${base}/modifications`, { method: "POST", body: JSON.stringify(input) });
    },
    listModifications(reasonClass = "", limit = 50): Promise<{ items: ModificationItem[] }> {
      const params = new URLSearchParams({ limit: String(limit) });
      if (reasonClass) params.set("reason_class", reasonClass);
      return requestJson(`${base}/modifications?${params.toString()}`);
    },
    linkJob(scriptId: string, jobId: string): Promise<{ ok: true; script_status: string }> {
      return requestJson(`${base}/scripts/${encodeURIComponent(scriptId)}/link-job`, {
        method: "POST",
        body: JSON.stringify({ job_id: jobId }),
      });
    },
    listScripts(status = "approved"): Promise<{ items: ScriptItem[] }> {
      const params = new URLSearchParams({ status });
      return requestJson(`${base}/scripts?${params.toString()}`);
    },
    scriptDetail(scriptId: string): Promise<ScriptDetail> {
      return requestJson(`${base}/scripts/${encodeURIComponent(scriptId)}`);
    },
    listReviewers(): Promise<{ items: ReviewerItem[] }> {
      return requestJson(`${base}/reviewers`);
    },
    addReviewer(userId: string): Promise<{ ok: true }> {
      return requestJson(`${base}/reviewers`, { method: "POST", body: JSON.stringify({ user_id: userId }) });
    },
    removeReviewer(userId: string): Promise<{ ok: true }> {
      return requestJson(`${base}/reviewers/${encodeURIComponent(userId)}`, { method: "DELETE" });
    },
  };
}
