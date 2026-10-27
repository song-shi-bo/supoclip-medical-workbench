import { afterEach, describe, expect, it, vi } from "vitest";
import { generateProjectId, downloadWorkflowArtifact, isEmbeddedPreview, workflowClipExportUrl, normalizeWorkflowJob, workflowSourceLabel, workflowSource, workflowArtifactUrl,
  isWorkflowVideoArtifact, parseWorkflowSrt, isWorkflowTerminal, revisionRequest,
  revisionEdits, reusableWorkflowRequest, readableWorkflowError, subtitleModeLabel, revisionWarnings,
  revisionWarningLabel, type RevisionDocument } from "./studio-workflow";
import { studioUploadBinding, uploadStudioVideo } from "./studio-upload";

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

describe("workflow facts and actions", () => {
  it("always uses the effect-aware export route, preserving saved state when local transform is absent", () => {
    const absent = new URL(workflowClipExportUrl("/video-api/studio/auto-edit", "job", "clip"), "http://test");
    expect(absent.pathname).toBe("/video-api/studio/auto-edit/job/clips/clip/export-transformed");
    expect(absent.searchParams.has("transform")).toBe(false);
    expect(absent.searchParams.get("download")).toBe("true");
    const reset = new URL(workflowClipExportUrl("/api", "j", "c", { scale: 1 }, "color"), "http://test");
    expect(JSON.parse(reset.searchParams.get("transform")!)).toEqual({ scale: 1 });
    expect(reset.searchParams.get("background")).toBe("color");
    const edited = new URL(workflowClipExportUrl("/api", "j", "c", { scale: 1.15 }), "http://test");
    expect(JSON.parse(edited.searchParams.get("transform")!)).toEqual({ scale: 1.15 });
  });
  it("downloads through an ordinary authenticated anchor and removes it without opening a popup", () => {
    const click = vi.fn(), remove = vi.fn(), appendChild = vi.fn();
    const anchor = { href: "", download: "unset", hidden: false, click, remove };
    const createElement = vi.fn(() => anchor);
    vi.stubGlobal("document", { createElement, body: { appendChild } });
    vi.stubGlobal("window", { open: vi.fn() });
    const url = "/video-api/studio/auto-edit/job/artifact?path=clip.mp4&download=true";
    downloadWorkflowArtifact(url);
    expect(createElement).toHaveBeenCalledWith("a");
    expect(anchor).toMatchObject({ href: url, download: "", hidden: true });
    expect(appendChild).toHaveBeenCalledWith(anchor);
    expect(click).toHaveBeenCalledOnce(); expect(remove).toHaveBeenCalledOnce();
    expect(window.open).not.toHaveBeenCalled();
    click.mockImplementation(() => { throw new Error("download failed"); });
    expect(() => downloadWorkflowArtifact(url)).toThrow("download failed");
    expect(remove).toHaveBeenCalledTimes(2);
  });
  /**
   * 工作台内嵌预览（iframe）场景：浏览器不给 sandbox iframe 下载权限（没有 allow-downloads 时
   * anchor download 被静默丢弃：后端日志 200、下载栏空）。此时必须改开顶层新标签页，
   * 并把「被拦住」如实返回给调用方提示用户。
   */
  it("embedded preview opens the export in a top-level tab instead of a silently blocked anchor", () => {
    const createElement = vi.fn();
    const open = vi.fn(() => ({ closed: false }));
    vi.stubGlobal("document", { createElement, body: { appendChild: vi.fn() } });
    vi.stubGlobal("window", { self: {}, top: {}, open });
    const url = "/video-api/studio/auto-edit/job/clips/clip/export-transformed?download=true";
    expect(isEmbeddedPreview()).toBe(true);
    expect(downloadWorkflowArtifact(url)).toBe("opened_new_tab");
    expect(open).toHaveBeenCalledWith(url, "_blank", "noopener,noreferrer");
    expect(createElement).not.toHaveBeenCalled();
    // 弹窗也被沙箱拦住 → 如实返回 blocked（调用方据此提示复制链接到浏览器地址栏）
    vi.stubGlobal("window", { self: {}, top: {}, open: vi.fn(() => null) });
    expect(downloadWorkflowArtifact(url)).toBe("blocked");
  });
  it("top-level page keeps the anchor download path", () => {
    const click = vi.fn(), remove = vi.fn(), appendChild = vi.fn();
    const anchor = { href: "", download: "unset", hidden: false, click, remove };
    const same = {};
    vi.stubGlobal("document", { createElement: vi.fn(() => anchor), body: { appendChild } });
    vi.stubGlobal("window", { self: same, top: same, open: vi.fn() });
    expect(isEmbeddedPreview()).toBe(false);
    expect(downloadWorkflowArtifact("/video-api/x")).toBe("downloaded");
    expect(click).toHaveBeenCalledOnce();
  });
  it("keeps absent sources, usage and capabilities unknown", () => {
    const job = normalizeWorkflowJob({ status: "failed", error: "真实错误" }, "j", "auto_edit");
    expect(job.available_actions).toEqual([]);
    expect(job.recovery_supported).toBe(false);
    expect(job.cancel_supported).toBe(false);
    expect(job.plan_source).toBeNull(); expect(job.usage_status).toBeNull();
    expect(job.error).toBe("真实错误");
    expect(workflowSourceLabel(null)).toBe("来源未知");
  });
  it("accepts exact supported server actions and stage sources", () => {
    const job = normalizeWorkflowJob({ available_actions: ["resume_render", "replan", "cleanup_only", "cancel", "unknown"],
      stage_sources: { planning: "mixed" }, degradation_reasons: ["模型窗口失败"] }, "j", "reedit");
    expect(job.available_actions).toEqual(["resume_render", "replan", "cleanup_only"]);
    expect(job.cancel_supported).toBe(true);
    expect(workflowSourceLabel(job.stage_sources.planning)).toBe("模型与规则混合");
    expect(job.degradation_reasons).toEqual(["模型窗口失败"]);
    for (const status of ["partial_succeeded", "partial_failed", "cancelled", "completed_with_cleanup_warning"])
      expect(isWorkflowTerminal(status)).toBe(true);
    expect(isWorkflowTerminal("rendering")).toBe(false);
  });
  it("reads real model/fallback error and nullable usage status", () => {
    expect(workflowSource({ plan_source: "mixed", llm_error: "429 quota", llm_prompt_tokens: 0,
      llm_usage: { prompt_tokens: null, usage_status: "unknown" } })).toMatchObject({
        source: "mixed", error: "429 quota", usage_status: "unknown" });
    expect(readableWorkflowError({ detail: "版本冲突" }, "failed")).toBe("版本冲突");
  });
  it("uses artifact kind for opaque authenticated file URLs", () => {
    const opaque = "/api/jobs/j/artifacts/a/file";
    expect(isWorkflowVideoArtifact({ kind: "clip", clip_id: "c", url: opaque })).toBe(true);
    expect(isWorkflowVideoArtifact({ kind: "clip_plan", url: "bad.mp4" })).toBe(false);
    expect(isWorkflowVideoArtifact({ kind: "srt", url: opaque })).toBe(false);
    expect(workflowArtifactUrl("/video-api", opaque)).toBe(`/video-api${opaque}`);
    expect(workflowArtifactUrl("/video-api", "/video-api/api/file")).toBe("/video-api/api/file");
  });
  it("parses actual relative SRT cues and rejects empty/malformed subtitle evidence", () => {
    const cues = parseWorkflowSrt("\uFEFF1\r\n00:00:00,250 --> 00:00:02,500\r\n纠错字幕\r\n第二行\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\n结尾\r\n");
    expect(cues).toEqual([{ start: 0.25, end: 2.5, text: "纠错字幕\n第二行" }, { start: 3, end: 4, text: "结尾" }]);
    expect(() => parseWorkflowSrt("not srt")).toThrow("未包含有效字幕段");
    expect(subtitleModeLabel("burned")).toContain("服务端记录");
    expect(subtitleModeLabel("sidecar_only")).toContain("未烧录");
    expect(subtitleModeLabel(undefined)).toContain("未知");
  });
});

describe("explicit source revision requests", () => {
  it("accepts the backend warning object without losing summary, errors or coverage", () => {
    const flag = { segment_index: 0, category: "dose_change", before: "5mg", after: "50mg" };
    const raw = { risk_flags: [flag], summary: { medication_check: "skipped", coverage: { checked: 2 } },
      validation_errors: [{ kind: "comparison_alignment_unavailable" }], comparison_source: "raw_asr" };
    const normalized = revisionWarnings(raw);
    expect(normalized.risk_flags.map(revisionWarningLabel)).toEqual(["第 1 段：剂量变化（5mg → 50mg）"]);
    expect(normalized.summary).toEqual(raw.summary);
    expect(normalized.validation_errors).toEqual(raw.validation_errors);
    expect(normalized.coverage).toEqual({ checked: 2 });
    expect(revisionWarningLabel(normalized.validation_errors[0])).toContain("未完成");
    expect(revisionWarnings(undefined).risk_flags).toEqual([]);
    expect(revisionWarnings([flag]).risk_flags).toEqual([flag]);
    expect(revisionWarnings({ risk_flags: {} }).risk_flags).toEqual([]);
  });
  const document: RevisionDocument = { transcript_id: "t", transcript_version: 3,
    asr_segments: [{ segment_id: "s", start: 0, end: 1, text: "纠错后文本" }],
    raw_segments: [{ start: 0, end: 1, text: "原文" }] };
  it("sends only text edits and CAS version, never client actor/timestamp mutations", () => {
    const original = JSON.stringify(document);
    const body = revisionRequest(document, "local:source", { 0: "人工核听文本" }, "req", "核听后修订");
    expect(body).toEqual({ object_key: "local:source", transcript_id: "t", base_version: 3,
      edits: [{ segment_index: 0, text: "人工核听文本" }], request_id: "req", reason: "核听后修订" });
    expect(JSON.stringify(document)).toBe(original);
    expect(revisionEdits(document, { 0: "纠错后文本" })).toEqual([]);
  });
  it("retains the same request identity after a failed unchanged request and changes it for new edits", () => {
    let count = 0;
    vi.stubGlobal("crypto", { randomUUID: () => `request-${++count}` });
    const first = reusableWorkflowRequest(null, "base3:editA");
    expect(reusableWorkflowRequest(first, "base3:editA")).toBe(first);
    expect(reusableWorkflowRequest(first, "base3:editB").id).not.toBe(first.id);
    const draft = { 0: "人工核听文本" };
    const retry = revisionRequest(document, "local:source", draft, first.id, "reason");
    expect(retry.request_id).toBe(first.id); expect(draft).toEqual({ 0: "人工核听文本" });
  });
});

describe("visible File upload recovery", () => {
  it("reuses only the same File and project binding; a new import/session gets a fresh id", () => {
    let count = 0;
    vi.stubGlobal("crypto", { randomUUID: () => String(++count).padStart(32, "0") });
    const file = new File(["video"], "v.mp4", { type: "video/mp4" });
    const first = studioUploadBinding(file, "p", null);
    expect(studioUploadBinding(file, "p", first)).toBe(first);
    expect(studioUploadBinding(new File(["video"], "v.mp4"), "p", first).uploadId).not.toBe(first.uploadId);
    expect(studioUploadBinding(file, "other", first).uploadId).not.toBe(first.uploadId);
    expect(studioUploadBinding(file, "p", null).uploadId).not.toBe(first.uploadId);
  });
  it("unknown GET 404/403 permits first chunk and explicit retry resumes only missing bytes", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("AbortSignal", { timeout: () => new AbortController().signal, any: (signals: AbortSignal[]) => signals[0] });
    const file = { name: "v.mp4", size: 8 * 1024 * 1024, slice: () => new Blob(["synthetic"]) } as File;
    const id = "a".repeat(32), sent: number[] = [], complete: unknown[] = [];
    let phase = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes("/chunks/")) return { ok: phase > 0, status: phase ? 200 : 404,
        json: async () => ({ parts: [{ index: 0, bytes: 4 * 1024 * 1024 }] }) };
      if (url.includes("/chunk?")) {
        expect(url).toContain(`upload_id=${id}`);
        const index = Number(new URL(`http://test${url}`).searchParams.get("index"));
        sent.push(index);
        if (!phase && index === 1) throw new Error("connection interrupted");
        return { ok: true };
      }
      complete.push(JSON.parse(String(init?.body)));
      return { ok: true, json: async () => ({ job_id: "j", object_key: "local:v" }) };
    }));
    const first = uploadStudioVideo(file, "/video-api", "p", () => {}, { uploadId: id }).catch((error: Error) => error);
    await vi.runAllTimersAsync();
    expect(await first).toBeInstanceOf(Error);
    expect(sent.filter((index) => index === 0)).toHaveLength(1);
    phase = 1; sent.length = 0;
    const second = await uploadStudioVideo(file, "/video-api", "p", () => {}, { uploadId: id });
    expect(second.job_id).toBe("j"); expect(sent).toEqual([1]);
    expect(complete).toEqual([{ upload_id: id, filename: "v.mp4", total: 2, size_bytes: file.size, project_id: "p" }]);
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      return { ok: false, status: 403 };
    }));
    const single = { ...file, size: 1 } as File;
    const call = uploadStudioVideo(single, "/video-api", "p", () => {}, { uploadId: id }).catch((error: Error) => error);
    await vi.runAllTimersAsync();
    // 403的未知会话探测继续尝试首POST，而非前端错误认定为登录失效。
    expect((fetch as ReturnType<typeof vi.fn>).mock.calls.some((args) => String(args[0]).includes("/chunk?"))).toBe(true);
    expect(await call).toBeInstanceOf(Error);
  });
  it("returns an already assembled same-session job without uploading/reassembling", async () => {
    vi.stubGlobal("AbortSignal", { timeout: () => new AbortController().signal });
    const request = vi.fn(async () => ({ ok: true, json: async () => ({ job_id: "j", object_key: "local:v" }) }));
    vi.stubGlobal("fetch", request);
    const file = new File(["video"], "v.mp4");
    expect(await uploadStudioVideo(file, "/video-api", "p", () => {}, { uploadId: "a".repeat(32) })).toEqual({ job_id: "j", object_key: "local:v" });
    expect(request).toHaveBeenCalledTimes(1);
  });
});

 it("generates an isolated project id in a library instead of exporting a Page field", () => {
  vi.stubGlobal("crypto", { getRandomValues: (bytes: Uint8Array) => { bytes.fill(15); return bytes; } });
  expect(generateProjectId()).toBe("timeline_0f0f0f0f0f0f");
});
