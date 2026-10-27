/**
 * 底部时间线「视图设置」的坐标与缩放数学（Phase B 前端）。
 *
 * 与后端 `backend/src/timeline_view.py` **同公式**（对照测试见
 * `lib/timeline-view.test.ts` + `lib/timeline-view.vectors.json`，向量由真实 Python
 * 模块生成）。这里的任何数值都**只影响显示**：刻度密度、波形密度、轨道高度。
 * 它们绝不进入导出参数——导出时长永远是 `duration`，与 `zoomPxPerSecond` 无关。
 *
 * 坐标系约定（与后端一致）
 * ------------------------
 * * `scrollLeft` 是**视口左边缘在时间线坐标系里的像素位置**（滚动条语义，非负）。
 *   某个时间 `t` 的**视口内**像素位置 = `t * pxPerSecond - scrollLeft`。
 * * DOM 里不手动减 `scrollLeft`：内容宽度 = `duration * pxPerSecond`，交给浏览器
 *   原生横向滚动。因此 `timeToPx(t, pps, 0)` 是**内容内**坐标，而
 *   `timeToPx(t, pps, scrollLeft)` 是**视口内**坐标（断言用后者）。
 */

// ---------------------------------------------------------------------------
// 常量（与后端 timeline_view.py 一一对应）
// ---------------------------------------------------------------------------

/** 时间轴缩放硬边界（px / 秒）：20 最密、300 最疏。 */
export const ZOOM_MIN_PX_PER_SECOND = 20;
export const ZOOM_MAX_PX_PER_SECOND = 300;
export const DEFAULT_ZOOM_PX_PER_SECOND = 100;
/** 「放大 / 缩小」每档倍率。 */
export const ZOOM_STEP_FACTOR = 1.25;

/** 单条音轨高度（px）边界与默认值。 */
export const TRACK_HEIGHT_MIN_PX = 44;
export const TRACK_HEIGHT_MAX_PX = 180;
export const DEFAULT_AUDIO_TRACK_HEIGHT_PX = 72;

/** 波形占轨道高度的三档比例（只影响显示密度，不影响任何音频参数）。 */
export const WAVEFORM_RATIOS = {
  compact: 0.35,
  standard: 0.6,
  expanded: 0.85,
} as const;

export type WaveformRatio = keyof typeof WAVEFORM_RATIOS;
export const DEFAULT_WAVEFORM_RATIO: WaveformRatio = "standard";

export const WAVEFORM_RATIO_LABELS: Record<WaveformRatio, string> = {
  compact: "紧凑",
  standard: "标准",
  expanded: "扩展",
};

export const WAVEFORM_RATIO_ORDER: WaveformRatio[] = ["compact", "standard", "expanded"];

export type TimelineView = {
  zoom_px_per_second: number;
  audio_track_height: number;
  waveform_ratio: WaveformRatio;
  scroll_left: number | null;
};

export const DEFAULT_TIMELINE_VIEW: TimelineView = {
  zoom_px_per_second: DEFAULT_ZOOM_PX_PER_SECOND,
  audio_track_height: DEFAULT_AUDIO_TRACK_HEIGHT_PX,
  waveform_ratio: DEFAULT_WAVEFORM_RATIO,
  scroll_left: null,
};

// ---------------------------------------------------------------------------
// 缩放 / 坐标纯函数
// ---------------------------------------------------------------------------

const finite = (value: number, fallback: number) =>
  Number.isFinite(value) ? value : fallback;

/** 把 px/秒 钳到 [20, 300]；非有限值退回默认。 */
export function clampZoom(value: number): number {
  return Math.max(
    ZOOM_MIN_PX_PER_SECOND,
    Math.min(ZOOM_MAX_PX_PER_SECOND, finite(value, DEFAULT_ZOOM_PX_PER_SECOND)),
  );
}

/** 把轨道高度钳到 [44, 180] 的整数像素。 */
export function clampTrackHeight(value: number): number {
  const safe = Math.round(finite(value, DEFAULT_AUDIO_TRACK_HEIGHT_PX));
  return Math.max(TRACK_HEIGHT_MIN_PX, Math.min(TRACK_HEIGHT_MAX_PX, safe));
}

/** 「放大 / 缩小」按钮：乘 / 除 `ZOOM_STEP_FACTOR` 后钳到边界。 */
export function nextZoomStep(pxPerSecond: number, options: { zoomIn: boolean }): number {
  const current = clampZoom(pxPerSecond);
  return clampZoom(options.zoomIn ? current * ZOOM_STEP_FACTOR : current / ZOOM_STEP_FACTOR);
}

/**
 * 「适合窗口」：整条时间线铺满视口宽度所需的 px/秒，钳到 [20, 300]。
 * 只影响刻度密度，不改导出时长。
 */
export function fitToWindowPxPerSecond(
  durationSeconds: number,
  viewportWidthPx: number,
): number {
  if (!Number.isFinite(durationSeconds) || !Number.isFinite(viewportWidthPx)) {
    return DEFAULT_ZOOM_PX_PER_SECOND;
  }
  if (durationSeconds <= 0 || viewportWidthPx <= 0) return DEFAULT_ZOOM_PX_PER_SECOND;
  return clampZoom(viewportWidthPx / durationSeconds);
}

/** 时间 → 像素：`seconds * pxPerSecond - scrollLeft`。 */
export function timeToPx(
  seconds: number,
  pxPerSecond: number,
  scrollLeft = 0,
): number {
  return finite(seconds, 0) * finite(pxPerSecond, DEFAULT_ZOOM_PX_PER_SECOND) - finite(scrollLeft, 0);
}

/** 像素 → 时间：`(px + scrollLeft) / pxPerSecond`（`timeToPx` 的严格逆）。 */
export function pxToTime(px: number, pxPerSecond: number, scrollLeft = 0): number {
  const rate = finite(pxPerSecond, DEFAULT_ZOOM_PX_PER_SECOND);
  if (rate === 0) return 0;
  return (finite(px, 0) + finite(scrollLeft, 0)) / rate;
}

/** 视口覆盖的时间区间 `[start, end]`。 */
export function visibleSeconds(
  viewportWidthPx: number,
  pxPerSecond: number,
  scrollLeft = 0,
): [number, number] {
  const rate = clampZoom(pxPerSecond);
  const start = pxToTime(0, rate, scrollLeft);
  return [start, start + finite(viewportWidthPx, 0) / rate];
}

/**
 * 以播放头为锚点缩放，返回缩放后应使用的 `scrollLeft`。
 *
 * 不变量：`t * prev - scrollPrev === t * next - scrollNext`
 * 解出：`scrollNext = scrollPrev + t * (next - prev)`
 *
 * 默认**不钳制**，因此不变量恒成立。传 `minScrollLeft` 时按滚动条语义钳制
 * （播放头本来已滚出视口左边缘的边界情形会让位于「视口不出现负坐标」）。
 */
export function zoomAnchored(
  prevPxPerSecond: number,
  nextPxPerSecond: number,
  playheadSeconds: number,
  scrollLeft = 0,
  options?: { minScrollLeft?: number | null },
): number {
  const prev = finite(prevPxPerSecond, DEFAULT_ZOOM_PX_PER_SECOND);
  const next = finite(nextPxPerSecond, DEFAULT_ZOOM_PX_PER_SECOND);
  const anchored = finite(scrollLeft, 0) + finite(playheadSeconds, 0) * (next - prev);
  const min = options?.minScrollLeft;
  if (min === undefined || min === null) return anchored;
  return Math.max(finite(min, 0), anchored);
}

/** 档位名 → 占比。 */
export function waveformFraction(ratio: WaveformRatio): number {
  return WAVEFORM_RATIOS[ratio] ?? WAVEFORM_RATIOS[DEFAULT_WAVEFORM_RATIO];
}

/** 时间线内容的像素总宽（DOM 用；`scrollLeft` 由原生滚动承担）。 */
export function contentWidthPx(durationSeconds: number, pxPerSecond: number): number {
  return Math.max(0, finite(durationSeconds, 0) * clampZoom(pxPerSecond));
}

/** 播放头在**视口内**的像素位置（验收断言用：缩放前后必须相等）。 */
export function playheadViewportPx(currentTime: number, view: TimelineView): number {
  return timeToPx(currentTime, view.zoom_px_per_second, view.scroll_left ?? 0);
}

/**
 * 一次缩放后的完整视图状态：新 px/秒 + 让播放头像素不动的 scrollLeft。
 * 返回 `scrollLeft` 已按滚动条语义钳到非负（≥0 是 DOM 的硬约束）。
 */
export function applyZoomAnchored(
  view: TimelineView,
  nextPxPerSecond: number,
  playheadSeconds: number,
): TimelineView {
  const prev = clampZoom(view.zoom_px_per_second);
  const next = clampZoom(nextPxPerSecond);
  const scrollLeft = Math.max(
    0,
    zoomAnchored(prev, next, playheadSeconds, view.scroll_left ?? 0),
  );
  return { ...view, zoom_px_per_second: next, scroll_left: scrollLeft };
}

// ---------------------------------------------------------------------------
// 视图状态：防御式归一化 + 后端读写
// ---------------------------------------------------------------------------

function asNumber(value: unknown, fallback: number): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

/** 把任意来源（后端 JSON / localStorage）归一化成合法的 `TimelineView`。 */
export function normaliseTimelineView(raw: unknown): TimelineView {
  if (!raw || typeof raw !== "object") return { ...DEFAULT_TIMELINE_VIEW };
  const source = raw as Record<string, unknown>;
  const ratio = source.waveform_ratio;
  const scrollRaw = source.scroll_left;
  const scroll =
    scrollRaw === null || scrollRaw === undefined || !Number.isFinite(Number(scrollRaw))
      ? null
      : Math.max(0, Number(scrollRaw));
  return {
    zoom_px_per_second: clampZoom(
      asNumber(source.zoom_px_per_second, DEFAULT_ZOOM_PX_PER_SECOND),
    ),
    audio_track_height: clampTrackHeight(
      asNumber(source.audio_track_height, DEFAULT_AUDIO_TRACK_HEIGHT_PX),
    ),
    waveform_ratio:
      typeof ratio === "string" && ratio in WAVEFORM_RATIOS
        ? (ratio as WaveformRatio)
        : DEFAULT_WAVEFORM_RATIO,
    scroll_left: scroll,
  };
}

export type TimelineViewResponse = { project_id: string; view: TimelineView };

/** `GET /studio/timeline/{id}/view` —— 纯读，后端保证不触发渲染。 */
export async function loadTimelineView(
  baseUrl: string,
  projectId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<TimelineView> {
  const response = await fetchImpl(
    `${baseUrl}/studio/timeline/${encodeURIComponent(projectId)}/view`,
    { cache: "no-store" },
  );
  if (!response.ok) throw new Error(`读取时间线视图设置失败（${response.status}）`);
  const payload = (await response.json()) as Partial<TimelineViewResponse>;
  return normaliseTimelineView(payload.view);
}

/** `PUT /studio/timeline/{id}/view` —— 局部更新；不写历史、不触发渲染/导出。 */
export async function saveTimelineViewPatch(
  baseUrl: string,
  projectId: string,
  patch: Partial<TimelineView>,
  fetchImpl: typeof fetch = fetch,
): Promise<TimelineView> {
  const response = await fetchImpl(
    `${baseUrl}/studio/timeline/${encodeURIComponent(projectId)}/view`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    },
  );
  if (!response.ok) throw new Error(`保存时间线视图设置失败（${response.status}）`);
  const payload = (await response.json()) as Partial<TimelineViewResponse>;
  return normaliseTimelineView(payload.view);
}

// ---------------------------------------------------------------------------
// 波形对齐几何（canvas 逐像素绘制用；纯函数，可单测）
// ---------------------------------------------------------------------------

export type AudioTrackLike = {
  track_id: string;
  kind: "voice" | "bgm" | "sfx";
  path?: string | null;
  asset_id?: string | null;
  source_start_s: number;
  source_end_s: number;
  record_start_s: number;
  volume?: number;
  muted?: boolean;
  fade_in_s?: number;
  fade_out_s?: number;
  label?: string | null;
  origin?: "source" | "user";
};

/** 轨道在时间线上占的时长（秒）= 素材出点 − 入点。 */
export function trackSpanSeconds(track: AudioTrackLike): number {
  return Math.max(0, finite(track.source_end_s, 0) - finite(track.source_start_s, 0));
}

/** 轨道在时间线上的区间 `[record_start, record_start + span]`（秒）。 */
export function trackRecordRange(track: AudioTrackLike): [number, number] {
  const start = finite(track.record_start_s, 0);
  return [start, start + trackSpanSeconds(track)];
}

/**
 * 轨道在时间线**内容坐标系**里的像素区间（DOM 的 `left` / `width`）。
 * 这是「按 `record_start_s` + `zoom_px_per_second` 对齐」的落点。
 */
export function trackContentPx(
  track: AudioTrackLike,
  pxPerSecond: number,
): { left: number; width: number } {
  const [start, end] = trackRecordRange(track);
  const rate = clampZoom(pxPerSecond);
  return { left: start * rate, width: Math.max(0, (end - start) * rate) };
}

/** 素材时刻 → peaks 数组下标（peaks 覆盖整个素材时长）。 */
export function peakIndexAtAssetTime(
  assetSeconds: number,
  assetDurationSeconds: number,
  peakCount: number,
): number {
  if (peakCount <= 0) return -1;
  if (!Number.isFinite(assetDurationSeconds) || assetDurationSeconds <= 0) return -1;
  const ratio = finite(assetSeconds, 0) / assetDurationSeconds;
  if (ratio < 0 || ratio >= 1) return ratio >= 1 ? peakCount - 1 : -1;
  return Math.min(peakCount - 1, Math.floor(ratio * peakCount));
}

/**
 * 一列波的几何：视口内每个像素列对应的 peaks 下标与归一化振幅。
 *
 * `columns[i]` 对应视口横坐标 `i`（`i ∈ [0, viewportWidth)`）。
 * `null` 表示该列落在轨道区间之外（不画）。
 *
 * 对齐链路（逐像素、与 `source_start_s/source_end_s/record_start_s` + 缩放 + 滚动同源）：
 * `x(视口) → t = pxToTime(x + scrollLeft) → assetT = source_start_s + (t - record_start_s)
 *  → index = assetT / assetDuration * peakCount`
 */
export function waveformColumns(
  track: AudioTrackLike,
  peaks: number[] | null | undefined,
  options: {
    pxPerSecond: number;
    scrollLeft: number;
    viewportWidthPx: number;
    assetDurationSeconds: number;
  },
): Array<{ index: number; amplitude: number } | null> {
  const width = Math.max(0, Math.floor(finite(options.viewportWidthPx, 0)));
  const columns: Array<{ index: number; amplitude: number } | null> = new Array(width).fill(null);
  const values = Array.isArray(peaks) ? peaks : [];
  if (!values.length || width === 0) return columns;
  const rate = clampZoom(options.pxPerSecond);
  const [recordStart, recordEnd] = trackRecordRange(track);
  const assetStart = finite(track.source_start_s, 0);
  const assetDuration = finite(options.assetDurationSeconds, 0);

  for (let x = 0; x < width; x += 1) {
    const time = pxToTime(x, rate, options.scrollLeft);
    if (time < recordStart || time >= recordEnd) continue;
    const assetSeconds = assetStart + (time - recordStart);
    const index = peakIndexAtAssetTime(assetSeconds, assetDuration, values.length);
    if (index < 0) continue;
    const amplitude = Math.min(1, Math.max(0, Number(values[index]) || 0));
    columns[x] = { index, amplitude };
  }
  return columns;
}

/** 波形在轨道内的绘制高度（px）：轨高 × 占比档位。 */
export function waveformHeightPx(trackHeight: number, ratio: WaveformRatio): number {
  return Math.max(1, clampTrackHeight(trackHeight) * waveformFraction(ratio));
}

/**
 * 波形**视觉高度**（峰到峰，px）。验收 6 用它与轨高/占比一起作为前后对比证据。
 */
export function waveformVisualHeightPx(amplitude: number, trackHeight: number, ratio: WaveformRatio): number {
  const half = waveformHeightPx(trackHeight, ratio) / 2;
  return Math.max(1, Math.min(1, Math.max(0, amplitude)) * half * 2);
}
