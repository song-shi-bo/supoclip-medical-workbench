/**
 * 时间线**编辑数据**的 HTTP 客户端（Phase B）。
 *
 * 与 `lib/timeline-view.ts` 的分工，和后端 `timeline_history` / `timeline_view` 的分工
 * 完全一致：
 * * 本文件 → `timelineRevision`（音轨、片段、字幕轨）——**可撤销/可重做**，走历史；
 * * `timeline-view.ts` → `timelineView`（缩放/轨高/波形占比/滚动）——纯显示，不进历史。
 *
 * 所有写操作都只改 `<project_dir>/timeline/`，后端保证**不重写 EDL、不触发转码/导出**。
 */

import type { WaveformRatio } from "./timeline-view";

/** 一条音轨（与后端 `timeline_audio.AudioTrack` 同形）。 */
export type TimelineAudioTrack = {
  track_id: string;
  kind: "voice" | "bgm" | "sfx";
  asset_id?: string | null;
  path?: string | null;
  source_start_s: number;
  source_end_s: number;
  record_start_s: number;
  volume: number;
  muted: boolean;
  fade_in_s: number;
  fade_out_s: number;
  label?: string | null;
  origin: "source" | "user";
};

export type TimelineVisualTrack = {
  track_id: string;
  kind: string;
  label: string;
  origin: string;
};

export type TimelineRevision = {
  revision: number;
  updated_at: string;
  label: string;
  content_hash: string;
  ranges: Array<Record<string, unknown>>;
  audio_tracks: TimelineAudioTrack[];
  subtitles: Array<Record<string, unknown>> | null;
  visual_tracks?: TimelineVisualTrack[];
};

export type TimelineHistorySummary = {
  version: number;
  limit: number;
  total: number;
  index: number;
  current_revision: number | null;
  current_label: string | null;
  undo_depth: number;
  redo_depth: number;
  can_undo: boolean;
  can_redo: boolean;
  labels: string[];
};

/** `GET /history` 与所有写端点的**同一份**响应形状。 */
export type TimelineHistoryPayload = {
  project_id: string;
  summary: TimelineHistorySummary;
  current_revision: TimelineRevision | null;
  revision: number | null;
  audio_tracks: TimelineAudioTrack[];
  can_undo: boolean;
  can_redo: boolean;
};

export type TimelineAddTrackKind = "bgm" | "sfx" | "subtitle" | "visual";

export type TimelineAudioUpload = {
  project_id: string;
  /** 工程相对路径（`timeline/audio/bgm-xxxxxxxx.mp3`），直接喂给加轨接口。 */
  path: string;
  filename: string | null;
  asset_id: string | null;
  duration_s: number;
};

export type TimelineTrackPatch = {
  record_start_s?: number;
  source_start_s?: number;
  volume?: number;
  muted?: boolean;
  fade_in_s?: number;
  fade_out_s?: number;
  label?: string;
};

export type TimelinePeaksPayload = {
  project_id: string;
  path: string;
  duration_s: number;
  peaks: number[];
  peak_count: number;
  cached: boolean;
  cache_key: string;
  source: string;
};

/** 从响应体里取可读错误（沿用工作台 `CODE: 原因` 的 detail 约定）。 */
async function failure(response: Response, fallback: string): Promise<Error> {
  let detail = "";
  try {
    const payload = (await response.json()) as { detail?: unknown };
    if (typeof payload?.detail === "string") detail = payload.detail;
  } catch {
    detail = "";
  }
  return new Error(detail || `${fallback}（HTTP ${response.status}）`);
}

const timelineBase = (baseUrl: string, projectId: string) =>
  `${baseUrl}/studio/timeline/${encodeURIComponent(projectId)}`;

/** `GET /history`：撤销/重做按钮状态 + 当前音轨。 */
export async function fetchTimelineHistory(
  baseUrl: string,
  projectId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<TimelineHistoryPayload> {
  const response = await fetchImpl(`${timelineBase(baseUrl, projectId)}/history`,
    { cache: "no-store" });
  if (!response.ok) throw await failure(response, "读取时间线历史失败");
  return (await response.json()) as TimelineHistoryPayload;
}

/** `POST /revision`：提交一条版本（先 normalise_tracks 再 push）。 */
export async function postTimelineRevision(
  baseUrl: string,
  projectId: string,
  payload: Record<string, unknown>,
  fetchImpl: typeof fetch = fetch,
): Promise<TimelineHistoryPayload> {
  const response = await fetchImpl(`${timelineBase(baseUrl, projectId)}/revision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw await failure(response, "提交时间线版本失败");
  return (await response.json()) as TimelineHistoryPayload;
}

/** `POST /undo` / `POST /redo`：不可用时后端返回 409（前端据此置灰）。 */
export async function stepTimelineHistory(
  baseUrl: string,
  projectId: string,
  step: "undo" | "redo",
  fetchImpl: typeof fetch = fetch,
): Promise<TimelineHistoryPayload> {
  const response = await fetchImpl(`${timelineBase(baseUrl, projectId)}/${step}`,
    { method: "POST" });
  if (!response.ok) throw await failure(response, step === "undo" ? "撤销失败" : "重做失败");
  return (await response.json()) as TimelineHistoryPayload;
}

/** `POST /tracks`：＋ 菜单落点（音频轨 / 字幕轨 / 视觉轨）。 */
export async function postTimelineTrack(
  baseUrl: string,
  projectId: string,
  body: Record<string, unknown>,
  fetchImpl: typeof fetch = fetch,
): Promise<TimelineHistoryPayload> {
  const response = await fetchImpl(`${timelineBase(baseUrl, projectId)}/tracks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await failure(response, "添加轨道失败");
  return (await response.json()) as TimelineHistoryPayload;
}

/** `PATCH /tracks/{id}`：移动 / 音量 / 静音 / 淡入淡出 / 改名。 */
export async function patchTimelineTrack(
  baseUrl: string,
  projectId: string,
  trackId: string,
  patch: TimelineTrackPatch,
  fetchImpl: typeof fetch = fetch,
): Promise<TimelineHistoryPayload> {
  const response = await fetchImpl(
    `${timelineBase(baseUrl, projectId)}/tracks/${encodeURIComponent(trackId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
  if (!response.ok) throw await failure(response, "修改轨道失败");
  return (await response.json()) as TimelineHistoryPayload;
}

/** `DELETE /tracks/{id}`：删除一条轨（可撤销）。 */
export async function deleteTimelineTrack(
  baseUrl: string,
  projectId: string,
  trackId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<TimelineHistoryPayload> {
  const response = await fetchImpl(
    `${timelineBase(baseUrl, projectId)}/tracks/${encodeURIComponent(trackId)}`,
    { method: "DELETE" });
  if (!response.ok) throw await failure(response, "删除轨道失败");
  return (await response.json()) as TimelineHistoryPayload;
}

/** `POST /assets/audio`：上传 BGM / 音效到项目时间线目录（内容寻址）。 */
export async function uploadTimelineAudio(
  baseUrl: string,
  projectId: string,
  file: File,
  fetchImpl: typeof fetch = fetch,
): Promise<TimelineAudioUpload> {
  const form = new FormData();
  form.append("audio", file, file.name);
  const response = await fetchImpl(`${timelineBase(baseUrl, projectId)}/assets/audio`, {
    method: "POST",
    body: form,
  });
  if (!response.ok) throw await failure(response, "上传音频失败");
  return (await response.json()) as TimelineAudioUpload;
}

/** `GET /assets/peaks`：真实波形峰值（后端 ffmpeg + 磁盘缓存）。 */
export async function fetchTimelinePeaks(
  baseUrl: string,
  projectId: string,
  options: { path: string; trackId?: string; kind?: "voice" | "bgm" | "sfx"; buckets?: number },
  fetchImpl: typeof fetch = fetch,
): Promise<TimelinePeaksPayload> {
  const query = new URLSearchParams({ path: options.path, buckets: String(options.buckets ?? 800) });
  if (options.trackId) query.set("track_id", options.trackId);
  if (options.kind) query.set("kind", options.kind);
  const response = await fetchImpl(
    `${timelineBase(baseUrl, projectId)}/assets/peaks?${query.toString()}`,
    { cache: "no-store" });
  if (!response.ok) throw await failure(response, "读取波形失败");
  return (await response.json()) as TimelinePeaksPayload;
}

/** `POST /tracks` 的音频轨请求体（BGM / 音效）。 */
export function audioTrackRequest(options: {
  kind: "bgm" | "sfx";
  path: string;
  label: string;
  recordStartS?: number;
  volume?: number;
}): Record<string, unknown> {
  return {
    kind: options.kind,
    path: options.path,
    label: options.label,
    record_start_s: options.recordStartS ?? 0,
    ...(options.volume === undefined ? {} : { volume: options.volume }),
  };
}

/** 波形占比档位的可读名（与工具栏浮层一致）。 */
export const TIMELINE_RATIO_NAMES: Record<WaveformRatio, string> = {
  compact: "紧凑",
  standard: "标准",
  expanded: "扩展",
};
