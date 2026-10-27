"use client";

/**
 * 时间线顶部工具栏（底部时间线 Phase B）。
 *
 * 分三组、留足间距：
 *   左  ＋ 添加到时间线 / 撤销 / 重做
 *   右  缩小 / 可拖动缩放滑杆 / 放大 / 适合窗口
 *   最右 三点「轨道显示设置」浮层（轨道高度滑杆 + 波形占比三档）
 *
 * 约束
 * ----
 * * 「原视频 / 原声」**不在** ＋ 菜单里：它们由导入/自动剪辑自动进入时间线。
 *   源声轨已存在时，菜单里只显示一条**置灰 + tooltip** 的说明项，点不动。
 * * 撤销/重做按 `canUndo` / `canRedo` 置灰 + tooltip。
 * * 浮层向上展开（时间线面板在页面最底部），因此**不遮挡整条时间线**；
 *   点空白或按 Esc 关闭。
 * * 每个图标按钮都有 `aria-label` + `title`（原生 tooltip）。
 * * 窄窗（≤1180px）把缩放滑杆 / 适合窗口 / 轨道显示设置收进「更多工具」菜单，
 *   工具栏 `flex-wrap: nowrap` 永不换行压坏时间线。
 * * 这里没有任何数值会进入导出参数：缩放只改刻度与波形密度。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Captions, Check, Film, ImageIcon, MoreHorizontal, Music, Redo2, Settings2,
  Undo2, Volume2, X, ZoomIn, ZoomOut,
} from "lucide-react";

import {
  TRACK_HEIGHT_MAX_PX,
  TRACK_HEIGHT_MIN_PX,
  WAVEFORM_RATIO_LABELS,
  WAVEFORM_RATIO_ORDER,
  ZOOM_MAX_PX_PER_SECOND,
  ZOOM_MIN_PX_PER_SECOND,
  waveformFraction,
  type WaveformRatio,
} from "@/lib/timeline-view";

/** ＋ 菜单里可添加的轨道种类（**不含**原视频/原声）。 */
export type TimelineAddKind = "bgm" | "sfx" | "subtitle" | "visual";

export type TimelineToolbarProps = {
  /** 撤销/重做是否可用（来自 history 的 can_undo / can_redo）。 */
  canUndo: boolean;
  canRedo: boolean;
  /** 当前缩放（px / 秒），20–300。 */
  zoomPxPerSecond: number;
  /** 当前轨道高度（44–180）。 */
  trackHeight: number;
  /** 当前波形占比档位。 */
  waveformRatio: WaveformRatio;
  /** 源声轨是否已自动进入时间线（决定 ＋ 菜单里的说明项可见性）。 */
  hasSourceTrack: boolean;
  /** 是否已有其它代理正在写（禁用全部动作）。 */
  busy?: boolean;
  /** 测试用：强制窄窗布局，避免依赖 jsdom 的 matchMedia。 */
  narrow?: boolean;
  onAddTrack: (kind: TimelineAddKind) => void;
  onUndo: () => void;
  onRedo: () => void;
  /** 滑杆/按钮缩放（父级负责以播放头为锚点重算 scroll_left）。 */
  onZoomChange: (pxPerSecond: number) => void;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onFitWindow: () => void;
  onTrackHeightChange: (px: number) => void;
  onWaveformRatioChange: (ratio: WaveformRatio) => void;
};

/** 窄窗判定：≤1180px 时把次要工具收进「更多工具」。 */
const NARROW_QUERY = "(max-width: 1180px)";

function useIsNarrow(): boolean {
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const query = window.matchMedia(NARROW_QUERY);
    const sync = () => setNarrow(query.matches);
    sync();
    query.addEventListener?.("change", sync);
    return () => query.removeEventListener?.("change", sync);
  }, []);
  return narrow;
}

/** 点外部 / Esc 关闭（两个浮层共用）。 */
function useDismiss(open: boolean, close: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent | TouchEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) close();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        close();
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("touchstart", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("touchstart", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open, close]);
  return ref;
}

const ADD_ITEMS: Array<{
  kind: TimelineAddKind;
  label: string;
  hint: string;
  Icon: typeof Music;
}> = [
  { kind: "bgm", label: "背景音乐", hint: "上传音频并放进一条新的音轨", Icon: Music },
  { kind: "sfx", label: "音效", hint: "短音频，落在播放头所在位置", Icon: Volume2 },
  { kind: "subtitle", label: "字幕轨", hint: "声明一条字幕轨（内容来自转写）", Icon: Captions },
  { kind: "visual", label: "图片或图解视觉轨", hint: "图片 / 图解轨（渲染在 Phase C 落地）", Icon: ImageIcon },
];

export function TimelineToolbar({
  canUndo, canRedo, zoomPxPerSecond, trackHeight, waveformRatio,
  hasSourceTrack, busy = false, narrow, onAddTrack, onUndo, onRedo,
  onZoomChange, onZoomIn, onZoomOut, onFitWindow,
  onTrackHeightChange, onWaveformRatioChange,
}: TimelineToolbarProps) {
  const detectedNarrow = useIsNarrow();
  const isNarrow = narrow ?? detectedNarrow;
  const [addOpen, setAddOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const closeAdd = useCallback(() => setAddOpen(false), []);
  const closeSettings = useCallback(() => setSettingsOpen(false), []);
  const addRef = useDismiss(addOpen, closeAdd);
  const settingsRef = useDismiss(settingsOpen, closeSettings);

  const undoHint = canUndo ? "撤销上一步时间线编辑（Ctrl/Cmd+Z）" : "没有可撤销的时间线操作";
  const redoHint = canRedo ? "重做被撤销的时间线编辑（Ctrl/Cmd+Shift+Z 或 Ctrl/Cmd+Y）"
    : "没有可重做的时间线操作";
  const zoomPercent = Math.round(zoomPxPerSecond);
  const ratioPercent = Math.round(waveformFraction(waveformRatio) * 100);

  const pickAdd = (kind: TimelineAddKind) => {
    setAddOpen(false);
    onAddTrack(kind);
  };

  return (
    <div className="timeline-toolbar" role="toolbar" aria-label="时间线工具栏">
      <strong className="timeline-toolbar-title">时间线</strong>

      {/* ── 左组：添加到时间线 / 撤销 / 重做 ───────────────────────── */}
      <div className="timeline-toolbar-group timeline-toolbar-left">
        <div className="timeline-popover-anchor" ref={addRef}>
          <button
            type="button"
            className="timeline-icon-button"
            aria-label="添加到时间线"
            aria-haspopup="menu"
            aria-expanded={addOpen}
            title="添加到时间线（背景音乐 / 音效 / 字幕轨 / 视觉轨）"
            disabled={busy}
            onClick={() => setAddOpen((value) => !value)}
          >
            <span aria-hidden="true" className="timeline-add-glyph">＋</span>
          </button>
          {addOpen && (
            <div className="timeline-popover timeline-add-menu" role="menu" aria-label="添加到时间线">
              <header>添加到时间线</header>
              {ADD_ITEMS.map(({ kind, label, hint, Icon }) => (
                <button
                  type="button"
                  key={kind}
                  role="menuitem"
                  className="timeline-menu-item"
                  title={hint}
                  aria-label={`${label}：${hint}`}
                  onClick={() => pickAdd(kind)}
                >
                  <Icon size={15} aria-hidden="true" />
                  <span>{label}</span>
                  <small>{hint}</small>
                </button>
              ))}
              {/* 原视频与原声**不在**可添加项：只有源声轨已自动进入时才显示这条只读说明。 */}
              {hasSourceTrack && (
                <div
                  className="timeline-menu-item is-reserved"
                  role="menuitem"
                  aria-disabled="true"
                  title="原始视频与原声在导入/自动剪辑后已自动进入时间线，不能用 ＋ 重复添加"
                  aria-label="原始视频原声：已自动进入时间线，不能用 ＋ 重复添加"
                >
                  <Film size={15} aria-hidden="true" />
                  <span>原始视频原声</span>
                  <small>已自动进入时间线</small>
                </div>
              )}
            </div>
          )}
        </div>

        <button
          type="button"
          className="timeline-icon-button"
          aria-label="撤销"
          title={undoHint}
          disabled={busy || !canUndo}
          onClick={onUndo}
        >
          <Undo2 size={16} aria-hidden="true" />
        </button>
        <button
          type="button"
          className="timeline-icon-button"
          aria-label="重做"
          title={redoHint}
          disabled={busy || !canRedo}
          onClick={onRedo}
        >
          <Redo2 size={16} aria-hidden="true" />
        </button>
      </div>

      {/* ── 右组：缩小 / 滑杆 / 放大 / 适合窗口 ────────────────────── */}
      <div className="timeline-toolbar-group timeline-toolbar-zoom">
        <button
          type="button"
          className="timeline-icon-button"
          aria-label="缩小时间线"
          title={`缩小时间线（当前 ${zoomPercent} px/秒）`}
          disabled={busy}
          onClick={onZoomOut}
        >
          <ZoomOut size={16} aria-hidden="true" />
        </button>
        {!isNarrow && (
          <input
            className="timeline-zoom-slider"
            type="range"
            aria-label="时间线缩放"
            title={`拖动调整缩放：${zoomPercent} px/秒`}
            min={ZOOM_MIN_PX_PER_SECOND}
            max={ZOOM_MAX_PX_PER_SECOND}
            step={1}
            value={Math.min(ZOOM_MAX_PX_PER_SECOND, Math.max(ZOOM_MIN_PX_PER_SECOND, zoomPercent))}
            aria-valuetext={`${zoomPercent} 像素每秒`}
            disabled={busy}
            onChange={(event) => onZoomChange(Number(event.target.value))}
          />
        )}
        <button
          type="button"
          className="timeline-icon-button"
          aria-label="放大时间线"
          title={`放大时间线（当前 ${zoomPercent} px/秒）`}
          disabled={busy}
          onClick={onZoomIn}
        >
          <ZoomIn size={16} aria-hidden="true" />
        </button>
        {!isNarrow && (
          <button
            type="button"
            className="timeline-text-button fit-button"
            aria-label="适合窗口"
            title="让整条时间线铺满当前视口宽度（只看显示，不改导出时长）"
            disabled={busy}
            onClick={onFitWindow}
          >
            适合窗口
          </button>
        )}
      </div>

      {/* ── 最右：三点「轨道显示设置」+ 窄窗「更多工具」──────────────── */}
      <div className="timeline-toolbar-group timeline-toolbar-right" ref={settingsRef}>
        <button
          type="button"
          className="timeline-icon-button"
          aria-label="轨道显示设置"
          aria-haspopup="dialog"
          aria-expanded={settingsOpen}
          title="轨道显示设置（轨道高度 / 波形占比）"
          onClick={() => setSettingsOpen((value) => !value)}
        >
          {isNarrow ? <MoreHorizontal size={16} aria-hidden="true" /> : <Settings2 size={16} aria-hidden="true" />}
        </button>
        {settingsOpen && (
          <div className="timeline-popover timeline-settings-menu" role="dialog" aria-label="轨道显示设置">
            <header>
              <strong>轨道显示设置</strong>
              <button type="button" aria-label="关闭轨道显示设置" title="关闭（Esc）" onClick={closeSettings}>
                <X size={13} aria-hidden="true" />
              </button>
            </header>
            {isNarrow && (
              <section className="timeline-settings-section">
                <label htmlFor="timeline-more-zoom">时间线缩放 · {zoomPercent} px/秒</label>
                <input
                  id="timeline-more-zoom"
                  type="range"
                  aria-label="时间线缩放"
                  min={ZOOM_MIN_PX_PER_SECOND}
                  max={ZOOM_MAX_PX_PER_SECOND}
                  step={1}
                  value={Math.min(ZOOM_MAX_PX_PER_SECOND, Math.max(ZOOM_MIN_PX_PER_SECOND, zoomPercent))}
                  onChange={(event) => onZoomChange(Number(event.target.value))}
                />
                <button
                  type="button"
                  className="timeline-text-button"
                  aria-label="适合窗口"
                  title="让整条时间线铺满当前视口宽度"
                  onClick={onFitWindow}
                >
                  适合窗口
                </button>
              </section>
            )}
            <section className="timeline-settings-section">
              <label htmlFor="timeline-track-height">轨道高度 · {trackHeight} px</label>
              <input
                id="timeline-track-height"
                type="range"
                aria-label="轨道高度"
                title={`轨道高度：${trackHeight} px（${TRACK_HEIGHT_MIN_PX}–${TRACK_HEIGHT_MAX_PX}）`}
                min={TRACK_HEIGHT_MIN_PX}
                max={TRACK_HEIGHT_MAX_PX}
                step={1}
                value={trackHeight}
                aria-valuetext={`${trackHeight} 像素`}
                onChange={(event) => onTrackHeightChange(Number(event.target.value))}
              />
            </section>
            <section className="timeline-settings-section">
              <span className="timeline-settings-label">波形占比 · {ratioPercent}%</span>
              <div className="timeline-ratio-group" role="group" aria-label="波形占比">
                {WAVEFORM_RATIO_ORDER.map((ratio) => (
                  <button
                    type="button"
                    key={ratio}
                    className={ratio === waveformRatio ? "timeline-ratio is-active" : "timeline-ratio"}
                    aria-pressed={ratio === waveformRatio}
                    aria-label={`波形占比 ${WAVEFORM_RATIO_LABELS[ratio]}`}
                    title={`波形占比：${WAVEFORM_RATIO_LABELS[ratio]}（${Math.round(waveformFraction(ratio) * 100)}%）`}
                    onClick={() => onWaveformRatioChange(ratio)}
                  >
                    {ratio === waveformRatio && <Check size={12} aria-hidden="true" />}
                    {WAVEFORM_RATIO_LABELS[ratio]}
                  </button>
                ))}
              </div>
            </section>
            <p className="timeline-settings-hint">只影响显示：不改变音频内容、时长与导出结果。</p>
          </div>
        )}
      </div>
    </div>
  );
}

export default TimelineToolbar;
