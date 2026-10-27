/** 工作流纯契约：缺失来源与能力保持未知；不把旧数值0当免费调用。 */
export type RecoveryMode = "resume_render" | "replan" | "cleanup_only";
export type WorkflowArtifact = { kind: string; url: string; clip_id?: string | null; id?: string;
  transcript_id?: string; transcript_version?: number; provenance?: Record<string, unknown> };
export type WorkflowJob = { job_id: string; status: string; stage: string; progress: number;
  error: string | null; warnings: string[]; plan_source: string | null; usage_status: string | null;
  artifacts: WorkflowArtifact[]; available_actions: RecoveryMode[]; cancel_supported: boolean;
  recovery_supported: boolean; stage_sources: Record<string, string>; degradation_reasons: string[];
  kind: "transcribe" | "auto_edit" | "reedit" };
export type WorkflowSource = { source: string | null; error: string | null; warnings: string[];
  usage_status: string | null; transcript_version?: number; model?: string | null };
export type RevisionSegment = { start: number; end: number; text: string; raw_text?: string;
  segment_id?: string; start_ms?: number; end_ms?: number; uncertain?: boolean;
  correction_status?: "corrected" | "no_change" | "pending_review"; correction_reason?: string };
export type RevisionDocument = { transcript_id: string; transcript_version: number;
  asr_segments: RevisionSegment[]; raw_segments?: RevisionSegment[];
  correction_audit?: unknown[]; correction_summary?: Record<string, unknown>;
  manual_revision?: unknown; review_warnings?: unknown };

export type RevisionWarnings = { risk_flags: unknown[]; summary: Record<string, unknown>;
  validation_errors: unknown[]; coverage: unknown; comparison_source: string | null };

/** 后端使用包含 risk_flags 的对象；兼容旧数组，未知结构不冒充检查成功。 */
export function revisionWarnings(value: unknown): RevisionWarnings {
  if (Array.isArray(value)) return { risk_flags: value, summary: {}, validation_errors: [],
    coverage: null, comparison_source: null };
  const data = value && typeof value === "object" ? value as Record<string, unknown> : {};
  const summary = data.summary && typeof data.summary === "object" && !Array.isArray(data.summary)
    ? data.summary as Record<string, unknown> : {};
  return { risk_flags: Array.isArray(data.risk_flags) ? data.risk_flags : [], summary,
    validation_errors: Array.isArray(data.validation_errors) ? data.validation_errors : [],
    coverage: data.coverage ?? summary.coverage ?? null,
    comparison_source: typeof data.comparison_source === "string" ? data.comparison_source : null };
}

export function revisionWarningLabel(value: unknown): string {
  if (!value || typeof value !== "object") return readableWorkflowError(value, "请核听这一版本。");
  const flag = value as Record<string, unknown>;
  if (flag.kind === "comparison_alignment_unavailable") return "原文与本版段落未能对齐，词汇变化检查未完成，请人工核听。";
  const names: Record<string, string> = { dose: "剂量变化", negation: "否定词变化",
    medical_term: "药名变化", uncertain: "识别不确定", dose_change: "剂量变化",
    negation_change: "否定词变化", medical_term_change: "药名变化" };
  const category = names[String(flag.category)] || "文本变化待复核";
  const segment = typeof flag.segment_index === "number" ? `第 ${flag.segment_index + 1} 段：` : "";
  return `${segment}${category}${typeof flag.before === "string" && typeof flag.after === "string"
    ? `（${flag.before} → ${flag.after}）` : ""}`;
}

export function reusableWorkflowRequest(previous: { key: string; id: string } | null, key: string) {
  return previous?.key === key ? previous : { key, id: crypto.randomUUID() };
}

export function readableWorkflowError(value: unknown, fallback: string): string {
  if (typeof value === "string" && value.trim()) return value;
  if (value && typeof value === "object") {
    const data = value as Record<string, unknown>;
    if (typeof data.message === "string") return data.message;
    if (data.detail !== undefined) return readableWorkflowError(data.detail, fallback);
    return JSON.stringify(value);
  }
  return fallback;
}

export function normalizeWorkflowJob(raw: Record<string, unknown>, jobId: string,
  kind: WorkflowJob["kind"]): WorkflowJob {
  const actions = Array.isArray(raw.available_actions) ? raw.available_actions : [];
  return { job_id: String(raw.job_id || jobId), status: String(raw.status || "queued"),
    stage: String(raw.stage || ""), progress: typeof raw.progress === "number" ? raw.progress : 0,
    error: raw.error ? readableWorkflowError(raw.error, "任务失败") : null,
    warnings: Array.isArray(raw.warnings) ? raw.warnings.map(String) : [],
    plan_source: typeof raw.plan_source === "string" ? raw.plan_source : null,
    usage_status: typeof raw.usage_status === "string" ? raw.usage_status : null,
    artifacts: Array.isArray(raw.artifacts) ? raw.artifacts as WorkflowArtifact[] : [],
    available_actions: actions.filter((item): item is RecoveryMode =>
      item === "resume_render" || item === "replan" || item === "cleanup_only"),
    recovery_supported: Array.isArray(raw.available_actions),
    cancel_supported: actions.includes("cancel"),
    stage_sources: raw.stage_sources && typeof raw.stage_sources === "object"
      ? raw.stage_sources as Record<string, string> : {},
    degradation_reasons: Array.isArray(raw.degradation_reasons) ? raw.degradation_reasons.map(String) : [], kind };
}

export const isWorkflowTerminal = (status: string) => new Set([
  "failed", "technical_failed", "partial_failed", "partial_succeeded", "cancelled",
  "completed", "completed_with_warnings", "completed_with_cleanup_warning", "analysis_ready", "plan_ready",
  "transcript_ready",
]).has(status);

export function workflowSourceLabel(source: string | null | undefined): string {
  if (source === "llm" || source === "model") return "模型";
  if (source === "mixed" || source === "hybrid") return "模型与规则混合";
  if (["rules", "dictionary", "heuristic_fallback", "local-fallback", "conservative"].includes(source || "")) return "规则回退";
  return source ? `来源待核对（${source}）` : "来源未知";
}

export function subtitleModeLabel(mode: string | null | undefined): string {
  return mode === "burned" ? "MP4 已烧录字幕（服务端记录）" :
    mode === "sidecar_only" ? "MP4 未烧录字幕，使用独立SRT" :
    mode === "absent" ? "本成片无字幕" : "MP4 字幕烧录状态未知";
}

export function workflowSource(raw: Record<string, unknown>): WorkflowSource {
  const usage = raw.llm_usage as { usage_status?: string } | undefined;
  return { source: typeof raw.plan_source === "string" ? raw.plan_source :
      typeof raw.engine === "string" ? raw.engine : null,
    error: raw.llm_error || raw.error || raw.fallback_reason ? readableWorkflowError(
      raw.llm_error || raw.error || raw.fallback_reason, "") : null,
    warnings: [...(Array.isArray(raw.warnings) ? raw.warnings.map(String) : []),
      ...(Array.isArray(raw.degradations) ? raw.degradations.map(String) : [])],
    usage_status: usage?.usage_status || (typeof raw.usage_status === "string" ? raw.usage_status : null),
    transcript_version: typeof raw.transcript_version === "number" ? raw.transcript_version : undefined,
    model: typeof raw.model === "string" ? raw.model : null };
}

export function workflowArtifactUrl(base: string, url: string): string {
  if (!url || url.startsWith(`${base}/`) || /^https?:\/\//i.test(url)) return url;
  return url.startsWith("/") ? `${base}${url}` : `${base}/${url}`;
}

export const isWorkflowVideoArtifact = (artifact: WorkflowArtifact) =>
  (artifact.kind === "clip" || artifact.kind === "clip_final") && Boolean(artifact.url);

export function parseWorkflowSrt(text: string): RevisionSegment[] {
  const stamp = (value: string) => {
    const found = value.trim().match(/^(\d+):(\d{2}):(\d{2})[,.](\d{3})$/);
    return found ? Number(found[1]) * 3600 + Number(found[2]) * 60 + Number(found[3]) + Number(found[4]) / 1000 : NaN;
  };
  const result: RevisionSegment[] = [];
  for (const block of text.replace(/^\uFEFF/, "").replace(/\r/g, "").split(/\n\s*\n/)) {
    const lines = block.trim().split("\n");
    const index = lines.findIndex((line) => line.includes("-->"));
    if (index < 0) continue;
    const times = lines[index].split("-->");
    const start = stamp(times[0]), end = stamp(times[1]);
    const content = lines.slice(index + 1).join("\n").trim();
    if (Number.isFinite(start) && Number.isFinite(end) && end > start && content) {
      result.push({ start, end, text: content });
    }
  }
  if (!result.length) throw new Error("SRT 未包含有效字幕段，成片字幕时间线尚未载入。");
  return result;
}

export function revisionEdits(document: RevisionDocument, draft: Record<number, string>) {
  return Object.entries(draft).filter(([index, text]) =>
    document.asr_segments[Number(index)] && document.asr_segments[Number(index)].text !== text)
    .map(([index, text]) => ({ segment_index: Number(index), text }));
}

export function revisionRequest(document: RevisionDocument, objectKey: string,
  draft: Record<number, string>, requestId: string, reason: string) {
  return { object_key: objectKey, transcript_id: document.transcript_id,
    base_version: document.transcript_version, edits: revisionEdits(document, draft),
    request_id: requestId, reason };
}

// 稳定项目 id（timeline_ + 12 位 hex）：导入素材时生成一次，转写与自动剪辑两个独立
// job 共用同一个 project_id；removeFile / 重新导入素材时重置为新值。
export const generateProjectId = (): string => {
  const bytes = new Uint8Array(6);
  crypto.getRandomValues(bytes);
  return "timeline_" + Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
};

/** 导出触发结果：直接下载成功 / 因为处在内嵌预览而改开新标签页 / 连新标签页都被拦。 */
export type ArtifactDownloadOutcome = "downloaded" | "opened_new_tab" | "blocked";

/**
 * 是否运行在「内嵌预览」里（工作台窗口的 iframe）。
 * 浏览器对沙箱 iframe 默认禁止下载（没有 allow-downloads 时 anchor download 会被静默
 * 丢弃：网络请求照发、下载栏什么都不出现）。这种场景不能再用 anchor，必须把导出
 * 链接交给顶层标签页，或提示用户到真实浏览器标签页里操作。
 */
export function isEmbeddedPreview(): boolean {
  try {
    if (typeof window === "undefined") return false;
    return window.self !== window.top;
  } catch {
    return true;   // 跨源访问 window.top 抛错 = 一定被嵌在别的页面里
  }
}

/** Use the same authenticated download route as the visible card link, without a popup. */
export function downloadWorkflowArtifact(url: string): ArtifactDownloadOutcome {
  if (isEmbeddedPreview()) {
    try {
      // 顶层新标签页不受内嵌框架的下载限制；noopener 不交出 window 引用。
      const opened = window.open(url, "_blank", "noopener,noreferrer");
      if (opened) return "opened_new_tab";
    } catch {
      /* 弹窗被沙箱拦住 → 交给调用方提示用户直接打开链接 */
    }
    return "blocked";
  }
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "";
  anchor.hidden = true;
  document.body.appendChild(anchor);
  try { anchor.click(); } finally { anchor.remove(); }
  return "downloaded";
}

export function workflowClipExportUrl(base: string, taskId: string, clipId: string,
  transform?: Record<string, unknown>, background = "blur"): string {
  const query = new URLSearchParams({ background, download: "true" });
  // Missing local state lets the server use the saved sidecar; it must not reset an unopened clip.
  if (transform !== undefined) query.set("transform", JSON.stringify(transform));
  return `${base}/${encodeURIComponent(taskId)}/clips/${encodeURIComponent(clipId)}/export-transformed?${query}`;
}

