// Inspector Phase 1 —— typed API client。
//
// 只实现契约 §5 的 10 个端点，路径/方法/请求体逐字对应：
//   GET/PUT  /studio/clips/{project_id}/{clip_id}/config
//   GET/POST /studio/clips/{project_id}/{clip_id}/transcript/revisions
//   POST     /studio/clips/{project_id}/{clip_id}/transcript/rollback
//   GET/POST /studio/clips/{project_id}/{clip_id}/subtitles
//   POST     /studio/clips/{project_id}/{clip_id}/audio/bgm        (multipart)
//   DELETE   /studio/clips/{project_id}/{clip_id}/audio/bgm/{asset_id}
//   GET      /studio/clips/{project_id}/{clip_id}/render-config
//
// 错误映射（契约 §5）：后端 422 的 detail 形如 "E_CODE: 可读原因"，
// 这里解析成 code + message，UI 于是能把「失败原因」原样显示出来。

import type {
  BgmUploadResult,
  ClipConfig,
  ClipConfigUpdateResult,
  ClipConfigView,
  Effects,
  RenderConfigView,
  SubtitleConfig,
  SubtitleCue,
  SubtitleOverride,
  SubtitleSaveResult,
  SubtitleView,
  TranscriptDocument,
  TranscriptRevisionChange,
  TranscriptVariant,
} from "./types";

/** 后端带 E_ 错误码的失败，message 已经是可读中文。 */
export class InspectorApiError extends Error {
  code: string;
  status: number;

  constructor(message: string, code: string, status: number) {
    super(message);
    this.name = "InspectorApiError";
    this.code = code;
    this.status = status;
  }
}

/** "E_CONFIG_INVALID: 非法字号" → { code: "E_CONFIG_INVALID", message: "非法字号" }。 */
export function parseDetail(detail: unknown, status: number): InspectorApiError {
  const text = typeof detail === "string" ? detail.trim() : "";
  if (!text) return new InspectorApiError(`请求失败（HTTP ${status}）`, `E_HTTP_${status}`, status);
  const match = /^([A-Z][A-Z0-9_]+)\s*:\s*(.*)$/.exec(text);
  if (match) return new InspectorApiError(match[2] || text, match[1], status);
  return new InspectorApiError(text, `E_HTTP_${status}`, status);
}

export function errorText(error: unknown): string {
  if (error instanceof InspectorApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "未知错误";
}

/** 保持 "/video-api" 这类相对前缀；空串 = 同源相对路径（测试里 msw 直接拦）。 */
export function normalizeBaseUrl(baseUrl?: string | null): string {
  if (!baseUrl) return "";
  return baseUrl.endsWith("/") ? baseUrl.slice(0, -1) : baseUrl;
}

export function clipPath(baseUrl: string, projectId: string, clipId: string): string {
  return `${baseUrl}/studio/clips/${encodeURIComponent(projectId)}/${encodeURIComponent(clipId)}`;
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text) as unknown;
    } catch {
      payload = null;
    }
  }
  if (!response.ok) {
    const detail = payload && typeof payload === "object" && "detail" in payload
      ? (payload as { detail?: unknown }).detail
      : null;
    throw parseDetail(detail, response.status);
  }
  if (payload === null) throw new InspectorApiError("后端返回了空响应", "E_EMPTY_RESPONSE", response.status);
  return payload as T;
}

const JSON_HEADERS = { "Content-Type": "application/json" };

// ===== 配置 =====

export async function getClipConfig(baseUrl: string, projectId: string, clipId: string): Promise<ClipConfigView> {
  return requestJson<ClipConfigView>(`${clipPath(baseUrl, projectId, clipId)}/config`, { cache: "no-store" });
}

/** 部分更新：只传要改的 section（契约 §3.1 PATCH_SECTIONS）。 */
export async function putClipConfig(
  baseUrl: string,
  projectId: string,
  clipId: string,
  patch: Partial<Pick<ClipConfig,
    "title" | "subtitle_config" | "subtitle_overrides" | "transform"
    | "audio_tracks" | "voice_volume" | "muted" | "effects">> & Record<string, unknown>,
): Promise<ClipConfigUpdateResult> {
  return requestJson<ClipConfigUpdateResult>(`${clipPath(baseUrl, projectId, clipId)}/config`, {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify(patch),
  });
}

// ===== 转写 =====

export async function getTranscript(
  baseUrl: string,
  projectId: string,
  clipId: string,
  variant?: TranscriptVariant,
): Promise<TranscriptDocument> {
  const query = variant ? `?view=${encodeURIComponent(variant)}` : "";
  return requestJson<TranscriptDocument>(`${clipPath(baseUrl, projectId, clipId)}/transcript/revisions${query}`, { cache: "no-store" });
}

export async function addTranscriptRevision(
  baseUrl: string,
  projectId: string,
  clipId: string,
  body: { changes: TranscriptRevisionChange[]; author?: string; reason?: string },
): Promise<TranscriptDocument> {
  return requestJson<TranscriptDocument>(`${clipPath(baseUrl, projectId, clipId)}/transcript/revisions`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
}

export async function rollbackTranscript(
  baseUrl: string,
  projectId: string,
  clipId: string,
  revision: number,
): Promise<TranscriptDocument> {
  return requestJson<TranscriptDocument>(`${clipPath(baseUrl, projectId, clipId)}/transcript/rollback`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ revision }),
  });
}

// ===== 字幕 =====

export async function getSubtitles(baseUrl: string, projectId: string, clipId: string): Promise<SubtitleView> {
  return requestJson<SubtitleView>(`${clipPath(baseUrl, projectId, clipId)}/subtitles`, { cache: "no-store" });
}

export async function saveSubtitles(
  baseUrl: string,
  projectId: string,
  clipId: string,
  body: {
    config?: Partial<SubtitleConfig>;
    overrides?: SubtitleOverride[];
    cues?: Pick<SubtitleCue, "segment_id" | "start" | "end" | "text">[];
    reason?: string;
    task_id?: string | null;
  },
): Promise<SubtitleSaveResult> {
  return requestJson<SubtitleSaveResult>(`${clipPath(baseUrl, projectId, clipId)}/subtitles`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
}

// ===== 音频 =====

/**
 * multipart 上传 BGM（单文件上限由后端 config.max_video_upload_bytes 决定）。
 *
 * 用 XHR 而不是 fetch：需要真实的上传进度（进度条），fetch 在上传阶段拿不到
 * ProgressEvent。错误映射与 requestJson 一致（422 detail = "E_CODE: 可读原因"）。
 */
export function uploadBgm(
  baseUrl: string,
  projectId: string,
  clipId: string,
  file: File,
  options: { volume?: number; fadeInS?: number; fadeOutS?: number; onProgress?: (percent: number) => void } = {},
): Promise<BgmUploadResult> {
  const form = new FormData();
  form.append("file", file);
  if (options.volume !== undefined) form.append("volume", String(options.volume));
  if (options.fadeInS !== undefined) form.append("fade_in_s", String(options.fadeInS));
  if (options.fadeOutS !== undefined) form.append("fade_out_s", String(options.fadeOutS));
  const url = `${clipPath(baseUrl, projectId, clipId)}/audio/bgm`;

  return new Promise<BgmUploadResult>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && options.onProgress) {
        options.onProgress(Math.round(event.loaded / event.total * 100));
      }
    };
    xhr.onerror = () => reject(new InspectorApiError("BGM 上传网络中断，请重试", "E_UPLOAD_NETWORK", 0));
    xhr.onload = () => {
      let payload: unknown = null;
      if (xhr.responseText) {
        try {
          payload = JSON.parse(xhr.responseText) as unknown;
        } catch {
          payload = null;
        }
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        const detail = payload && typeof payload === "object" && "detail" in payload
          ? (payload as { detail?: unknown }).detail
          : null;
        reject(parseDetail(detail, xhr.status));
        return;
      }
      if (payload === null) {
        reject(new InspectorApiError("BGM 上传返回了空响应", "E_EMPTY_RESPONSE", xhr.status));
        return;
      }
      options.onProgress?.(100);
      resolve(payload as BgmUploadResult);
    };
    xhr.send(form);
  });
}

export async function deleteBgm(
  baseUrl: string,
  projectId: string,
  clipId: string,
  assetId: string,
): Promise<{ removed: boolean; asset_id: string }> {
  return requestJson<{ removed: boolean; asset_id: string }>(
    `${clipPath(baseUrl, projectId, clipId)}/audio/bgm/${encodeURIComponent(assetId)}`,
    { method: "DELETE" },
  );
}

// ===== 导出摘要 =====

export async function getRenderConfig(baseUrl: string, projectId: string, clipId: string): Promise<RenderConfigView> {
  return requestJson<RenderConfigView>(`${clipPath(baseUrl, projectId, clipId)}/render-config`, { cache: "no-store" });
}

/** 供「效果设置」保存后原地刷新摘要用（只改 effects 段的轻量路径）。 */
export function effectsPatch(effects: Effects): { effects: Effects } {
  return { effects };
}
