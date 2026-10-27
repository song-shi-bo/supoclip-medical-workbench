"use client";

/**
 * 时间线轨道层（底部时间线 Phase B）：刻度 / 字幕块 / 视频块 / 音轨行 + 播放头。
 *
 * 严格同步的实现方式（这是本组件的全部意义）
 * ------------------------------------------
 * 所有横向位置都用**同一套像素数学** `timeToPx(t, zoom_px_per_second)` 落到
 * **内容坐标系**（内容总宽 = `duration * zoom_px_per_second`），横向滚动交给外层
 * `.timeline-scroll` 的原生 `overflow-x`。因此刻度、字幕块、视频块、波形与播放头
 * 不可能各自算一套百分比而错位——它们共用同一个坐标原点。
 *
 * 波形来自后端 `GET /studio/timeline/{id}/assets/peaks`（真实 ffmpeg 峰值，复用
 * `clip_audio`），不是浏览器现场解码：canvas **只画视口可见的那一段**（按
 * `scrollLeft` 平移），因此 100k px 级的时间线也不会撞 canvas 宽度上限。
 * 逐像素映射见 `waveformColumns()`：`x → t → source_start_s + (t - record_start_s)
 * → peaks 下标`。
 *
 * 视图设置（轨高 / 波形占比 / 缩放 / 滚动）只影响显示，不进入任何导出参数。
 */

import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import {
  Captions, Eye, Film, Image as ImageIcon, Layers, LockKeyhole, Music, Volume2, X,
} from "lucide-react";

import {
  clampTrackHeight,
  contentWidthPx,
  timeToPx,
  trackContentPx,
  waveformColumns,
  waveformFraction,
  type AudioTrackLike,
  type TimelineView,
  type WaveformRatio,
} from "@/lib/timeline-view";

export type TimelineTrackKind = AudioTrackLike["kind"];

/** 时间线图片轨：封面静帧实例（后端台账 timeline_image_clips 的投影）。 */
export type TimelineImageClip = {
  id: string;
  role: string;
  start_s: number;
  duration_s: number;
};

/** 拖拽载荷 MIME：封面（素材卡/封面区）进时间线。 */
export const TIMELINE_COVER_DRAG_TYPE = "application/x-studio-cover-image";

/** currentTime 落进的图片实例（窗口 [start, start+duration)）；无 → null。
 *  播放预览据此叠显示封面静帧 + 静音（与导出渲染语义一致）。 */
export function findActiveTimelineImage(
  clips: TimelineImageClip[], currentTime: number,
): TimelineImageClip | null {
  for (const clip of clips) {
    const start = Number(clip.start_s) || 0;
    const end = start + (Number(clip.duration_s) || 0);
    if (currentTime >= start - 1e-6 && currentTime < end - 1e-6) return clip;
  }
  return null;
}

/**
 * 轨道标签列宽度（px）。滚动坐标系原点在整张网格左边，因此轨道内的**局部**像素
 * 位置 = 容器像素位置 − 154；canvas 与波形列映射都按局部坐标算（见下方注释）。
 */
export const TIMELINE_LABEL_GUTTER_PX = 154;
/** 轨道内容列的最小宽度（= 刻度最小宽 860 − 标签列 154）。
 *  挂载瞬间视口可能还没完成布局（clientWidth=0），直接写回会把第二列固化成 0px，
 *  表现为「视频/字幕/音频三行没有宽度」。这里钳住下限。 */
export const TIMELINE_MIN_CONTENT_PX = 706;

export type TimelineSubtitleCue = {
  segment_id: string;
  start: number;
  end: number;
  text: string;
};

export type TimelineVideoRange = {
  id: string;
  source_start: number;
  source_end: number;
};

export type TimelineTracksProps = {
  duration: number;
  currentTime: number;
  view: TimelineView;
  ranges: TimelineVideoRange[];
  cues: TimelineSubtitleCue[];
  segments: TimelineSubtitleCue[];
  selectedSegment: number | null;
  goldenSegments?: number[];
  copilotHighlight?: { start: number; end: number } | null;
  audioTracks: AudioTrackLike[];
  /** `track_id` → peaks（0..1）；缺失表示还没拿到真实波形（不画假波形）。 */
  peaksByTrack: Record<string, number[]>;
  /** `track_id` → peaks 覆盖的素材时长（秒）。 */
  peaksDurationByTrack: Record<string, number>;
  activeMediaName: string;
  /** 时间线图片轨：封面静帧实例（空 = 显示提示文案）。 */
  imageClips?: TimelineImageClip[];
  /**
   * 拖放落点：封面（或已有实例）拖到图片轨/视频轨时回调。
   * ``payload.instance_id`` 存在 = 移动已有实例；否则 = 新增。
   */
  onDropCoverImage?: (startSeconds: number,
    payload: { clip_id?: string | null; instance_id?: string | null }) => void;
  /** 删除一个时间线图片实例（不删封面素材文件）。 */
  onRemoveImageClip?: (instanceId: string) => void;
  /**
   * 外层滚动容器（页面的 `.timeline-scroll`）。组件自己不拥有滚动容器：
   * 页面负责指针擦洗与 `onScroll`，这里只负责观测宽度 + 写回 `scrollLeft`。
   */
  scrollElementRef: RefObject<HTMLDivElement | null>;
  /** 滚动容器可视宽度（px），用于只画可见区间。 */
  viewportWidthPx: number;
  /** 外层滚动容器的 `scrollLeft`（px），与 `view.scroll_left` 同义。 */
  scrollLeft: number;
  onSeek: (seconds: number) => void;
  onSelectSegment: (index: number) => void;
  onViewportWidthChange: (width: number) => void;
};

/** 轨道行的显示名 / 图标。 */
function trackMeta(track: AudioTrackLike) {
  if (track.kind === "voice") return { label: track.label || "原始视频原声", Icon: Volume2, cls: "is-voice" };
  if (track.kind === "bgm") return { label: track.label || "背景音乐", Icon: Music, cls: "is-bgm" };
  return { label: track.label || "音效", Icon: Layers, cls: "is-sfx" };
}

/** 刻度：按缩放选一个「好看」的步长，保证刻度间距落在 [60, 240] px。 */
function tickStepSeconds(pxPerSecond: number): number {
  const candidates = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  for (const step of candidates) {
    if (step * pxPerSecond >= 60) return step;
  }
  return candidates[candidates.length - 1];
}

function formatTime(seconds: number): string {
  const total = Math.max(0, seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = Math.floor(total % 60);
  const base = [hours, minutes, secs].map((unit) => String(unit).padStart(2, "0")).join(":");
  const fraction = total - Math.floor(total);
  return fraction > 0.001 ? `${base}.${String(Math.round(fraction * 10))}` : base;
}

/** 单条音轨的波形 canvas：只画视口可见列，按 `scrollLeft` 绝对定位。 */
function TrackWaveform({
  track, peaks, assetDuration, trackHeight, ratio, pxPerSecond, scrollLeft,
  viewportWidthPx,
}: {
  track: AudioTrackLike;
  peaks: number[] | undefined;
  assetDuration: number;
  trackHeight: number;
  ratio: WaveformRatio;
  pxPerSecond: number;
  scrollLeft: number;
  viewportWidthPx: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const height = clampTrackHeight(trackHeight);
  const waveHeight = Math.max(1, height * waveformFraction(ratio));

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const context = canvas.getContext("2d");
    if (!context) return;
    const ratioPx = window.devicePixelRatio || 1;
    const width = Math.max(0, Math.floor(viewportWidthPx));
    canvas.width = Math.max(1, Math.floor(width * ratioPx));
    canvas.height = Math.max(1, Math.floor(height * ratioPx));
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    context.setTransform(ratioPx, 0, 0, ratioPx, 0, 0);
    context.clearRect(0, 0, width, height);

    const columns = waveformColumns(track, peaks, {
      pxPerSecond, scrollLeft, viewportWidthPx: width, assetDurationSeconds: assetDuration,
    });
    const mid = height / 2;
    const half = waveHeight / 2;
    context.strokeStyle = track.kind === "voice" ? "#20ced0" : track.kind === "bgm" ? "#7bd6a8" : "#e0b96a";
    context.globalAlpha = track.muted ? 0.28 : 0.92;
    context.lineWidth = 1;
    context.beginPath();
    for (let x = 0; x < width; x += 1) {
      const column = columns[x];
      if (!column) continue;
      const amplitude = Math.max(1, column.amplitude * half);
      context.moveTo(x + 0.5, mid - amplitude);
      context.lineTo(x + 0.5, mid + amplitude);
    }
    context.stroke();
  }, [track, peaks, assetDuration, height, waveHeight, pxPerSecond, scrollLeft, viewportWidthPx]);

  return (
    <canvas
      ref={canvasRef}
      className="timeline-track-canvas"
      data-testid={`timeline-waveform-${track.track_id}`}
      data-track-id={track.track_id}
      data-track-height={height}
      data-waveform-height={waveHeight}
      data-scroll-left={scrollLeft}
      data-px-per-second={pxPerSecond}
      aria-label={`${track.label || track.kind} 波形（高度 ${height}px，波形占比 ${Math.round(waveformFraction(ratio) * 100)}%）`}
      style={{ position: "absolute", left: scrollLeft, top: 0, pointerEvents: "none" }}
    />
  );
}

export function TimelineTracks({
  duration, currentTime, view, ranges, cues, segments, selectedSegment,
  goldenSegments = [], copilotHighlight, audioTracks, peaksByTrack,
  peaksDurationByTrack, activeMediaName, imageClips = [], onDropCoverImage,
  onRemoveImageClip, scrollElementRef, viewportWidthPx, scrollLeft,
  onSeek, onSelectSegment, onViewportWidthChange,
}: TimelineTracksProps) {
  const trackGridRef = useRef<HTMLDivElement>(null);
  const pxPerSecond = view.zoom_px_per_second;
  const trackHeight = clampTrackHeight(view.audio_track_height);
  const width = contentWidthPx(duration, pxPerSecond);

  // 视口尺寸观测（只画可见区间需要它；ResizeObserver 缺失时退回 clientWidth）。
  useEffect(() => {
    const element = scrollElementRef.current;
    if (!element) return;
    const sync = () => onViewportWidthChange(element.clientWidth);
    sync();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", sync);
      return () => window.removeEventListener("resize", sync);
    }
    const observer = new ResizeObserver(sync);
    observer.observe(element);
    return () => observer.disconnect();
  }, [onViewportWidthChange, scrollElementRef]);

  // 缩放时把滚动位置写回 DOM（父级已按播放头锚点算好 scroll_left）。
  useEffect(() => {
    const element = scrollElementRef.current;
    if (!element) return;
    if (Math.abs(element.scrollLeft - scrollLeft) > 0.5) {
      // jsdom 把 scrollLeft 定义为只读（严格模式下赋值抛错），真实浏览器不受影响。
      try { element.scrollLeft = scrollLeft; } catch { /* 测试环境无真实滚动 */ }
    }
  }, [scrollLeft, pxPerSecond, scrollElementRef]);

  const ticks = useMemo(() => {
    const step = tickStepSeconds(pxPerSecond);
    const marks: { time: number; left: number; major: boolean }[] = [];
    if (duration <= 0) return marks;
    for (let time = 0; time <= duration + 1e-6; time += step) {
      const rounded = Math.round(time * 1000) / 1000;
      marks.push({
        time: rounded,
        left: timeToPx(rounded, pxPerSecond, 0),
        major: Math.abs(rounded / (step * 5) - Math.round(rounded / (step * 5))) < 1e-6,
      });
      if (marks.length > 4000) break;
    }
    return marks;
  }, [duration, pxPerSecond]);

  const playheadContentPx = duration > 0 ? timeToPx(currentTime, pxPerSecond, 0) : 0;
  // 轨道局部可见区间：容器可见 [S, S+C] ⇒ 轨道局部 [S-154, S-154+C]。
  const laneScrollLeft = scrollLeft - TIMELINE_LABEL_GUTTER_PX;
  const cueBlocks = cues.length ? cues : segments;

  // ===== 时间线图片轨（封面静帧）=====
  const [imageDropHover, setImageDropHover] = useState(false);
  const sortedImageClips = useMemo(
    () => [...imageClips].sort((a, b) => a.start_s - b.start_s || a.id.localeCompare(b.id)),
    [imageClips],
  );

  const timeFromClientX = (clientX: number): number => {
    const grid = trackGridRef.current;
    if (!grid || duration <= 0) return 0;
    const rect = grid.getBoundingClientRect();
    // 网格自身已被滚动，rect.left 已含 -scrollLeft，因此下面算出的是**轨道局部**像素。
    const laneLeft = rect.left + TIMELINE_LABEL_GUTTER_PX;
    const laneWidth = Math.max(1, rect.width - TIMELINE_LABEL_GUTTER_PX);
    const ratio = Math.min(1, Math.max(0, (clientX - laneLeft) / laneWidth));
    return ratio * duration;
  };
  const seekFromEvent = (event: React.MouseEvent<HTMLElement>) => {
    onSeek(timeFromClientX(event.clientX));
  };
  const isCoverDrag = (event: React.DragEvent<HTMLElement>) =>
    event.dataTransfer.types.includes(TIMELINE_COVER_DRAG_TYPE);
  const coverDragOver = (event: React.DragEvent<HTMLElement>) => {
    if (!onDropCoverImage || !isCoverDrag(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  };
  const coverDrop = (event: React.DragEvent<HTMLElement>) => {
    setImageDropHover(false);
    if (!onDropCoverImage || !isCoverDrag(event)) return;
    event.preventDefault();
    let payload: { clip_id?: string | null; instance_id?: string | null } = {};
    try {
      const raw = event.dataTransfer.getData(TIMELINE_COVER_DRAG_TYPE);
      if (raw) payload = JSON.parse(raw) as { clip_id?: string | null; instance_id?: string | null };
    } catch {
      payload = {};
    }
    onDropCoverImage(timeFromClientX(event.clientX), payload);
  };

  return (
    <>
      <div
        className="time-ruler"
        data-testid="timeline-ruler"
        data-px-per-second={pxPerSecond}
        style={{ width, marginLeft: TIMELINE_LABEL_GUTTER_PX }}
        onClick={seekFromEvent}
      >
        {ticks.map((mark) => (
          <span
            key={mark.time}
            className={mark.major ? "is-major" : undefined}
            style={{ left: mark.left }}
            data-time={mark.time}
          >
            {formatTime(mark.time)}
          </span>
        ))}
      </div>

      <div
        ref={trackGridRef}
        className="track-grid"
        data-testid="timeline-track-grid"
        style={{
          width: TIMELINE_LABEL_GUTTER_PX + Math.max(width, TIMELINE_MIN_CONTENT_PX),
          gridTemplateColumns: `${TIMELINE_LABEL_GUTTER_PX}px ${Math.max(width, TIMELINE_MIN_CONTENT_PX)}px`,
        }}
      >
        {/* 字幕轨 */}
        <div className="track-label">
          <Captions size={16} aria-hidden="true" />
          <span>字幕</span>
          <Eye size={14} aria-hidden="true" />
          <LockKeyhole size={13} aria-hidden="true" />
        </div>
        <div className="caption-track" onClick={seekFromEvent} data-testid="timeline-caption-lane">
          {cueBlocks.map((cue, index) => (
            <button
              key={`${cue.segment_id}-${index}`}
              type="button"
              className={[
                cues.length ? "inspector-cue-block" : "",
                selectedSegment === index ? "active" : "",
                goldenSegments.includes(index) ? "golden" : "",
              ].filter(Boolean).join(" ")}
              style={{
                left: timeToPx(cue.start, pxPerSecond, 0),
                width: Math.max(5, timeToPx(cue.end - cue.start, pxPerSecond, 0)),
              }}
              title={cue.text}
              onPointerDown={(event) => event.stopPropagation()}
              onClick={(event) => { event.stopPropagation(); onSelectSegment(index); onSeek(cue.start); }}
            >
              {cue.text}
            </button>
          ))}
          {copilotHighlight && duration > 0 && (
            <i
              className="copilot-highlight"
              aria-hidden="true"
              style={{
                left: timeToPx(Math.max(0, copilotHighlight.start), pxPerSecond, 0),
                width: Math.max(
                  1,
                  timeToPx(
                    Math.min(copilotHighlight.end, duration) - Math.max(0, copilotHighlight.start),
                    pxPerSecond, 0),
                ),
              }}
            />
          )}
          <i className="track-playhead" aria-hidden="true" style={{ left: playheadContentPx }} />
        </div>

        {/* 视频轨 */}
        <div className="track-label">
          <Film size={16} aria-hidden="true" />
          <span>视频 1</span>
          <Eye size={14} aria-hidden="true" />
          <LockKeyhole size={13} aria-hidden="true" />
        </div>
        <div className="video-track" onClick={seekFromEvent} data-testid="timeline-video-lane"
          onDragOver={coverDragOver} onDrop={coverDrop}>
          {ranges.map((range) => (
            <div
              className="video-clip"
              key={range.id}
              data-range-id={range.id}
              style={{
                left: timeToPx(range.source_start, pxPerSecond, 0),
                width: Math.max(2, timeToPx(range.source_end - range.source_start, pxPerSecond, 0)),
              }}
            >
              <b>{activeMediaName}</b>
              <span>{formatTime(range.source_start)} — {formatTime(range.source_end)}</span>
            </div>
          ))}
          <i className="track-playhead" aria-hidden="true" style={{ left: playheadContentPx }} />
        </div>

        {/* 图片轨（时间线拖拽落点）：封面静帧实例；开头实例=片头封面（导出时前置），其余为插入静帧。 */}
        <div className="track-label">
          <ImageIcon size={16} aria-hidden="true" />
          <span>图片</span>
          <Eye size={14} aria-hidden="true" />
          <LockKeyhole size={13} aria-hidden="true" />
        </div>
        <div
          className={`image-track${imageDropHover ? " drop-hover" : ""}`}
          data-testid="timeline-image-lane"
          data-image-count={sortedImageClips.length}
          onClick={seekFromEvent}
          onDragOver={coverDragOver}
          onDragEnter={(event) => { if (onDropCoverImage && isCoverDrag(event)) { event.preventDefault(); setImageDropHover(true); } }}
          onDragLeave={() => setImageDropHover(false)}
          onDrop={coverDrop}
        >
          {sortedImageClips.length === 0 && (
            <span className="image-track-hint">把封面图片拖到这里：落点在开头 = 片头封面，其余位置 = 插入静帧</span>
          )}
          {sortedImageClips.map((clip) => (
            <div
              key={clip.id}
              className={`timeline-image-clip${clip.role === "cover_intro" ? " is-head" : ""}`}
              data-testid={`timeline-image-clip-${clip.id}`}
              data-role={clip.role}
              data-start={clip.start_s}
              data-duration={clip.duration_s}
              title={clip.role === "cover_intro"
                ? `片头封面 ${formatTime(clip.duration_s)}（导出时前置并替换内嵌片头）`
                : `封面静帧 @ ${formatTime(clip.start_s)} · ${formatTime(clip.duration_s)}（导出时插入，段内静音）`}
              style={{
                left: timeToPx(clip.start_s, pxPerSecond, 0),
                width: Math.max(6, timeToPx(clip.duration_s, pxPerSecond, 0)),
              }}
            >
              <b>{clip.role === "cover_intro" ? "片头封面" : "封面"}</b>
              <span>{formatTime(clip.duration_s)}</span>
              {onRemoveImageClip && (
                <button
                  type="button"
                  className="timeline-image-remove"
                  aria-label={`删除时间线图片（${clip.role === "cover_intro" ? "片头封面" : `${formatTime(clip.start_s)} 处`}）`}
                  data-testid={`timeline-image-remove-${clip.id}`}
                  onPointerDown={(event) => event.stopPropagation()}
                  onClick={(event) => { event.stopPropagation(); onRemoveImageClip(clip.id); }}
                >
                  <X size={10} />
                </button>
              )}
            </div>
          ))}
          <i className="track-playhead" aria-hidden="true" style={{ left: playheadContentPx }} />
        </div>

        {/* 音轨行：一条音轨一行；波形 canvas 只画可见区间。 */}
        {audioTracks.length === 0 && (
          <>
            <div className="track-label">
              <Volume2 size={16} aria-hidden="true" />
              <span>音频</span>
              <Eye size={14} aria-hidden="true" />
              <LockKeyhole size={13} aria-hidden="true" />
            </div>
            <div className="waveform-track timeline-empty-track" data-testid="timeline-audio-empty"
              onClick={seekFromEvent}>
              <span>导入视频后原声会自动进入时间线</span>
              <i className="track-playhead" aria-hidden="true" style={{ left: playheadContentPx }} />
            </div>
          </>
        )}
        {audioTracks.map((track) => {
          const meta = trackMeta(track);
          const placement = trackContentPx(track, pxPerSecond);
          return (
            <div className="timeline-track-row" key={track.track_id} style={{ display: "contents" }}>
              <div className="track-label" data-track-id={track.track_id}>
                <meta.Icon size={16} aria-hidden="true" />
                <span title={meta.label}>{meta.label}</span>
                {track.muted && <em className="timeline-track-flag" title="该轨已静音">静音</em>}
                <Eye size={14} aria-hidden="true" />
                <LockKeyhole size={13} aria-hidden="true" />
              </div>
              <div
                className={`waveform-track timeline-audio-lane ${meta.cls}`}
                style={{ height: trackHeight }}
                data-testid={`timeline-audio-lane-${track.track_id}`}
                data-track-id={track.track_id}
                data-track-height={trackHeight}
                data-track-kind={track.kind}
                data-record-start={track.record_start_s}
                data-source-start={track.source_start_s}
                data-source-end={track.source_end_s}
                onMouseDown={(event) => { event.stopPropagation(); seekFromEvent(event); }}
                onClick={seekFromEvent}
              >
                <div
                  className="timeline-audio-clip"
                  data-testid={`timeline-audio-clip-${track.track_id}`}
                  style={{ left: placement.left, width: placement.width }}
                >
                  <TrackWaveform
                    track={track}
                    peaks={peaksByTrack[track.track_id]}
                    assetDuration={peaksDurationByTrack[track.track_id] ?? 0}
                    trackHeight={trackHeight}
                    ratio={view.waveform_ratio}
                    pxPerSecond={pxPerSecond}
                    scrollLeft={laneScrollLeft}
                    viewportWidthPx={viewportWidthPx}
                  />
                </div>
                <i className="track-playhead" aria-hidden="true" style={{ left: playheadContentPx }} />
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

export default TimelineTracks;
