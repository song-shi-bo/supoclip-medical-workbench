"use client";

// 播放器底部控制栏（卡片第三层，固定 64px，不遮挡画面）：
//   左：当前时间 / 总时长
//   中：上一帧、后退、播放/暂停（主按钮）、前进、下一帧、音量
//   右（从左到右）：原画、裁切/重构图、比例、全屏、更多（窄窗才出现）
//
// 语义红线（体现在 title / aria-label 上）：
//  * 「原画」= 片段设置（contain + 完整裁剪 + 缩放 100% + 位置归零），会写入当前
//    片段并影响导出；
//  * 「裁切/重构图」= 进入片段变换编辑（拖动 / 滚轮 / 8 手柄只在该模式生效，
//    且只有点「应用」才会写入后端）；
//  * 「比例」= **只看**：仅改变预览画布的形状，导出仍按流水线 9:16；
//  * 「音量 / 全屏」= 只看：只影响本地观看。
//
// 响应式（容器查询 previewpane）：宽窗全部按钮平铺；窄窗把次要按钮折叠进
// 「更多」，但**播放、时间、全屏**在任何宽度都保留。所有图标按钮点击区
// 32~36px、间距 12px，正常态不出现缩放滑块 / 百分比 / 重置按钮。

import { useEffect, useRef, useState } from "react";
import {
  ChevronLeft, ChevronRight, Crop, FastForward, Maximize, Minimize, MoreHorizontal, Pause,
  PictureInPicture2, Play, Proportions, Rewind, Volume2, VolumeX,
} from "lucide-react";

import {
  VIEWER_ASPECT_OPTIONS,
  VIEWER_ASPECT_TOOLTIP,
  type ViewerAspect,
} from "@/components/studio/viewer-aspect";

type PlayerControlBarProps = {
  currentTime: number;
  duration: number;
  playing: boolean;
  hasMedia: boolean;
  volume: number;
  muted: boolean;
  viewerAspect: ViewerAspect;
  transformMode: boolean;
  transformLocked?: boolean;
  onTogglePlay: () => void;
  onSeekBy: (deltaSeconds: number) => void;
  onSeekFrame: (direction: -1 | 1) => void;
  onVolumeChange: (volume: number) => void;
  onToggleMute: () => void;
  onOriginalRatio: () => void;
  onToggleTransformMode: () => void;
  onToggleFullscreen: () => void;
  onViewerAspectChange: (aspect: ViewerAspect) => void;
};

const pad = (value: number) => String(Math.max(0, Math.floor(value))).padStart(2, "0");

/** 完整时间码：HH:MM:SS */
export const formatTimecode = (seconds: number) => {
  const total = Math.max(0, Math.floor(seconds || 0));
  return [Math.floor(total / 3600), Math.floor((total % 3600) / 60), total % 60].map(pad).join(":");
};

/** 窄窗用的紧凑时间码：MM:SS（超过一小时才退回 HH:MM:SS） */
export const formatTimecodeCompact = (seconds: number) => {
  const total = Math.max(0, Math.floor(seconds || 0));
  if (total >= 3600) return formatTimecode(total);
  return `${pad(total / 60)}:${pad(total % 60)}`;
};

export function PlayerControlBar(props: PlayerControlBarProps) {
  const {
    currentTime, duration, playing, hasMedia, volume, muted, viewerAspect,
    transformMode, transformLocked = false,
    onTogglePlay, onSeekBy, onSeekFrame, onVolumeChange, onToggleMute,
    onOriginalRatio, onToggleTransformMode, onToggleFullscreen, onViewerAspectChange,
  } = props;
  const [moreOpen, setMoreOpen] = useState(false);
  const [aspectOpen, setAspectOpen] = useState(false);
  const [volumeOpen, setVolumeOpen] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const moreRef = useRef<HTMLDivElement>(null);
  const aspectRef = useRef<HTMLDivElement>(null);
  const volumeRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const sync = () => setIsFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);

  // 点击外部关闭所有浮层（更多 / 比例 / 音量）。
  useEffect(() => {
    if (!moreOpen && !aspectOpen && !volumeOpen) return undefined;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (moreOpen && !moreRef.current?.contains(target)) setMoreOpen(false);
      if (aspectOpen && !aspectRef.current?.contains(target)) setAspectOpen(false);
      if (volumeOpen && !volumeRef.current?.contains(target)) setVolumeOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [moreOpen, aspectOpen, volumeOpen]);

  const volumePercent = Math.round((muted ? 0 : volume) * 100);
  const stepTitle = "逐帧微调（1/30 秒）：只移动播放头，不改动任何片段设置";
  const aspectLabel = VIEWER_ASPECT_OPTIONS.find((option) => option.id === viewerAspect)?.label ?? "9:16";

  const closeAll = () => { setMoreOpen(false); setAspectOpen(false); setVolumeOpen(false); };

  // 更多菜单里的次要操作：与平铺按钮同源回调，只是窄窗下的落点。
  const moreActions = (
    <>
      <p>窄窗折叠的次要操作</p>
      <button type="button" role="menuitem" disabled={!hasMedia} title={stepTitle} onClick={() => { closeAll(); onSeekFrame(-1); }}><ChevronLeft size={14} />上一帧</button>
      <button type="button" role="menuitem" disabled={!hasMedia} title="后退 5 秒" onClick={() => { closeAll(); onSeekBy(-5); }}><Rewind size={14} />后退 5 秒</button>
      <button type="button" role="menuitem" disabled={!hasMedia} title="前进 5 秒" onClick={() => { closeAll(); onSeekBy(5); }}><FastForward size={14} />前进 5 秒</button>
      <button type="button" role="menuitem" disabled={!hasMedia} title={stepTitle} onClick={() => { closeAll(); onSeekFrame(1); }}><ChevronRight size={14} />下一帧</button>
      <button
        type="button" role="menuitem" disabled={!hasMedia}
        title="原画：恢复素材原始比例与位置（适配改为「适应」、缩放 100%、位置归零、裁剪完整）。这是片段设置，会写入当前片段并影响导出。"
        onClick={() => { closeAll(); onOriginalRatio(); }}
      >
        <PictureInPicture2 size={14} />原画
      </button>
      <button
        type="button" role="menuitem" disabled={!hasMedia || transformLocked}
        title={transformLocked
          ? "该视频带旋转标记，暂不能编辑画面变换（导出不受影响）"
          : "裁切/重构图：进入后可拖动画面移动位置、滚轮缩放、拖 8 个控制点调整裁切；点「应用」才会保存。影响导出。"}
        onClick={() => { closeAll(); onToggleTransformMode(); }}
      >
        <Crop size={14} />裁切/重构图
      </button>
      <p>预览画布比例（{VIEWER_ASPECT_TOOLTIP}）</p>
      {VIEWER_ASPECT_OPTIONS.map((option) => (
        <button
          key={option.id}
          type="button"
          role="menuitemradio"
          aria-checked={viewerAspect === option.id}
          title={`${option.hint}。${VIEWER_ASPECT_TOOLTIP}`}
          onClick={() => { closeAll(); onViewerAspectChange(option.id); }}
        >
          <Proportions size={14} />比例 {option.label}<em>{viewerAspect === option.id ? "当前" : ""}</em>
        </button>
      ))}
    </>
  );

  return (
    <div className="player-control-bar" role="toolbar" aria-label="播放控制栏">
      <div className="player-zone player-zone-left">
        <span className="player-timecode" title="当前时间 / 总时长">
          <b className="player-timecode-full">{formatTimecode(currentTime)} <i>/</i> {formatTimecode(duration)}</b>
          <b className="player-timecode-compact">{formatTimecodeCompact(currentTime)}<i>/</i>{formatTimecodeCompact(duration)}</b>
        </span>
      </div>

      <div className="player-zone player-zone-center">
        <button type="button" className="player-icon-button player-foldable player-step" onClick={() => onSeekFrame(-1)} disabled={!hasMedia} aria-label="上一帧" title={stepTitle}><ChevronLeft size={17} /></button>
        <button type="button" className="player-icon-button player-foldable player-jump" onClick={() => onSeekBy(-5)} disabled={!hasMedia} aria-label="后退五秒" title="后退 5 秒"><Rewind size={17} /></button>
        <button
          type="button"
          className="player-icon-button play-button"
          onClick={onTogglePlay}
          disabled={!hasMedia}
          aria-label={playing ? "暂停" : "播放"}
          title={playing ? "暂停（空格）" : "播放（空格）"}
        >
          {playing ? <Pause size={19} fill="currentColor" /> : <Play size={19} fill="currentColor" />}
        </button>
        <button type="button" className="player-icon-button player-foldable player-jump" onClick={() => onSeekBy(5)} disabled={!hasMedia} aria-label="前进五秒" title="前进 5 秒"><FastForward size={17} /></button>
        <button type="button" className="player-icon-button player-foldable player-step" onClick={() => onSeekFrame(1)} disabled={!hasMedia} aria-label="下一帧" title={stepTitle}><ChevronRight size={17} /></button>
        <div className="player-popover-anchor player-volume-anchor" ref={volumeRef}>
          <button
            type="button"
            className={`player-icon-button${volumeOpen ? " active" : ""}`}
            onClick={() => setVolumeOpen((value) => !value)}
            disabled={!hasMedia}
            aria-label={muted ? "取消静音" : "音量"}
            aria-expanded={volumeOpen}
            title="音量（只影响本地预览试听，不写入片段与导出）"
          >
            {muted || volume === 0 ? <VolumeX size={17} /> : <Volume2 size={17} />}
          </button>
          {volumeOpen && (
            <div className="player-popover player-volume-popover" role="group" aria-label="音量">
              <input
                className="player-volume"
                type="range"
                min={0}
                max={100}
                step={1}
                value={volumePercent}
                disabled={!hasMedia}
                aria-label="音量"
                title="音量（只影响本地预览试听，不写入片段与导出）"
                onChange={(event) => onVolumeChange(Number(event.target.value) / 100)}
              />
              <button type="button" className="player-popover-action" onClick={onToggleMute}>{muted ? "取消静音" : "静音"}</button>
            </div>
          )}
        </div>
      </div>

      <div className="player-zone player-zone-right">
        <button
          type="button"
          className="player-icon-button player-foldable player-clip-tool"
          onClick={onOriginalRatio}
          disabled={!hasMedia}
          aria-label="原画"
          title="原画：恢复素材原始比例与位置（适配改为「适应」、缩放 100%、位置归零、裁剪完整）。这是片段设置，会写入当前片段并影响导出。"
        >
          <PictureInPicture2 size={16} />
        </button>
        <button
          type="button"
          className={`player-icon-button player-foldable player-clip-tool transform-toggle${transformMode ? " active" : ""}`}
          onClick={onToggleTransformMode}
          disabled={!hasMedia || transformLocked}
          aria-label="裁切/重构图"
          aria-pressed={transformMode}
          title={transformLocked
            ? "该视频带旋转标记，暂不能编辑画面变换（导出不受影响）"
            : transformMode
              ? "退出裁切/重构图（Esc）：未点「应用」的改动会回滚，已保存的片段设置保留"
              : "裁切/重构图：进入后拖动画面移动位置、滚轮缩放、拖 8 个控制点调整裁切（点「应用」才保存，影响导出）"}
        >
          <Crop size={16} />
        </button>
        <div className="player-popover-anchor player-foldable player-clip-tool" ref={aspectRef}>
          <button
            type="button"
            className={`player-icon-button${aspectOpen ? " active" : ""}`}
            onClick={() => setAspectOpen((value) => !value)}
            disabled={!hasMedia}
            aria-label="比例"
            aria-expanded={aspectOpen}
            title={`预览画布比例（当前 ${aspectLabel}）。${VIEWER_ASPECT_TOOLTIP}`}
          >
            <Proportions size={16} />
          </button>
          {aspectOpen && (
            <div className="player-popover player-aspect-menu" role="menu" aria-label="预览画布比例">
              <p>{VIEWER_ASPECT_TOOLTIP}</p>
              {VIEWER_ASPECT_OPTIONS.map((option) => (
                <button
                  key={option.id}
                  type="button"
                  role="menuitemradio"
                  aria-checked={viewerAspect === option.id}
                  title={`${option.hint}。${VIEWER_ASPECT_TOOLTIP}`}
                  onClick={() => { setAspectOpen(false); onViewerAspectChange(option.id); }}
                >
                  {option.label}<em>{viewerAspect === option.id ? "当前" : option.hint}</em>
                </button>
              ))}
            </div>
          )}
        </div>
        <button
          type="button"
          className="player-icon-button"
          onClick={onToggleFullscreen}
          disabled={!hasMedia}
          aria-label={isFullscreen ? "退出全屏" : "全屏"}
          title={isFullscreen ? "退出全屏预览" : "全屏预览（只影响查看方式）"}
        >
          {isFullscreen ? <Minimize size={16} /> : <Maximize size={16} />}
        </button>
        <div className="player-popover-anchor player-collapsed-only" ref={moreRef}>
          <button
            type="button"
            className={`player-icon-button${moreOpen ? " active" : ""}`}
            onClick={() => setMoreOpen((value) => !value)}
            aria-label="更多"
            aria-expanded={moreOpen}
            title="更多：窄窗折叠的次要操作与预览比例（都只影响观看，或需要点「应用」才写入片段）"
          >
            <MoreHorizontal size={16} />
          </button>
          {moreOpen && (
            <div className="player-popover player-more-menu" role="menu" aria-label="更多操作">
              {moreActions}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
