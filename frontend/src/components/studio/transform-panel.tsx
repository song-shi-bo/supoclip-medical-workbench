"use client";

// 裁切/重构图浮层（宽 316px、圆角 12px、半透明深色）：只在进入
// 裁切/重构图模式后出现，**悬浮**在画面上（绝对定位），不挤压画面、
// 也不遮挡底部控制栏；窄屏可用标题行的折叠按钮收起。
//
// 面板里同时存在两条缩放轴，语义红线通过分组与 title 明确区分：
//  * 「片段」组（缩放 / 原始比例 / 铺满画布 / 重置 / 适配 / 背景 / 比例锁）
//    写入 ClipTransform 草稿，点「应用」后影响导出；
//  * 「查看」组（适合画布 + 查看比例滑块）只改 viewerZoom，不影响导出。
//
// 应用/取消（本轮新增，替代旧的「边拖边写」）：
//  * 进入模式时对当前 transform 做快照；
//  * 面板里的所有改动只改**本地草稿**，预览实时变化，但**不写后端**；
//  * 「应用」= 把草稿交给页面调用既有持久化（成片 → PUT clip transform；
//    时间线片段 → debounce PUT /studio/timeline）；
//  * 「取消」/Esc = 回滚到快照，不产生任何写请求。
//
// 底层数学不自造：所有按钮最终都只是 clip-transform 的既有变换组合。

import { ChevronDown, ChevronUp, Crop, Lock, LockOpen, Maximize2, PictureInPicture2, RotateCcw, X, ZoomIn, ZoomOut } from "lucide-react";

import {
  MAX_SCALE,
  MIN_SCALE,
  isDefaultTransform,
  type BackgroundMode,
  type ClipTransform,
  type FitMode,
} from "@/lib/clip-transform";
import { FIT_VIEWER_ZOOM, MAX_VIEWER_ZOOM, MIN_VIEWER_ZOOM } from "@/components/studio/viewer-zoom";

type TransformPanelProps = {
  transform: ClipTransform;
  background: BackgroundMode;
  ratioLock: boolean;
  viewerZoom: number;
  // 当前选中的时间线片段标签（进入模式时自动选中的那一段）。
  clipLabel: string;
  disabled?: boolean;
  transformLocked?: boolean;
  // 草稿是否与进入模式时的快照不同（决定「应用」是否可点）。
  dirty?: boolean;
  // 折叠：只留标题行，让裁剪模式里的画面尽量大（折叠不减功能，展开即可继续调）。
  collapsed?: boolean;
  onToggleCollapse: () => void;
  onTransformChange: (next: ClipTransform) => void;
  onBackgroundChange: (next: BackgroundMode) => void;
  onRatioLockChange: (next: boolean) => void;
  onViewerZoomChange: (percent: number) => void;
  onFitView: () => void;
  onOriginalRatio: () => void;
  onFillCanvas: () => void;
  onResetTransform: () => void;
  // 应用草稿（唯一会写后端的路径）；取消/Esc 走 onCancel（回滚快照）。
  onApply: () => void;
  onCancel: () => void;
};

export function TransformPanel(props: TransformPanelProps) {
  const {
    transform, background, ratioLock, viewerZoom, clipLabel,
    disabled = false, transformLocked = false, collapsed = false, dirty = false,
    onToggleCollapse, onTransformChange, onBackgroundChange, onRatioLockChange, onViewerZoomChange,
    onFitView, onOriginalRatio, onFillCanvas, onResetTransform, onApply, onCancel,
  } = props;
  const locked = disabled || transformLocked;
  const zoomPercent = Math.round(transform.scale * 100);
  const nudgeZoom = (factor: number) =>
    onTransformChange({ ...transform, scale: Math.min(MAX_SCALE, Math.max(MIN_SCALE, transform.scale * factor)) });

  return (
    <div className={collapsed ? "transform-panel collapsed" : "transform-panel"} aria-label="裁切与重构图设置">
      <header className="transform-panel-head">
        <strong><Crop size={14} />裁切/重构图</strong>
        <span title="当前选中的时间线片段">{clipLabel}</span>
        <button
          type="button" className="panel-collapse"
          aria-expanded={!collapsed}
          onClick={onToggleCollapse}
          aria-label={collapsed ? "展开裁剪变换设置" : "折叠裁剪变换设置"}
          title={collapsed ? "展开设置" : "折叠设置，让画面与裁剪框显示得更大"}
        >
          {collapsed ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </button>
        <button type="button" className="panel-close" onClick={onCancel} aria-label="退出裁剪变换" title="退出裁切/重构图（Esc）：未点「应用」的改动会回滚，不写后端"><X size={14} /></button>
      </header>
      <div className="transform-panel-body">
      <div className="transform-panel-row" role="group" aria-label="片段缩放">
        <span className="row-label" title="片段缩放（clip scale）：会写入当前片段，影响导出构图">片段缩放</span>
        <button type="button" disabled={locked} aria-label="缩小片段" title="缩小片段（也可以在画面上用滚轮）。影响导出。" onClick={() => nudgeZoom(1 / 1.15)}><ZoomOut size={14} /></button>
        <input
          aria-label="片段缩放"
          title="片段缩放（clip scale）：点「应用」后写入当前片段并影响导出，10%~800%"
          type="range" min={10} max={800} step={1}
          value={zoomPercent}
          disabled={locked}
          onChange={(event) => onTransformChange({ ...transform, scale: Number(event.target.value) / 100 })}
        />
        <button type="button" disabled={locked} aria-label="放大片段" title="放大片段（也可以在画面上用滚轮）。影响导出。" onClick={() => nudgeZoom(1.15)}><ZoomIn size={14} /></button>
        <span className="zoom-value" title="当前片段缩放百分比">{zoomPercent}%</span>
      </div>

        <div className="transform-panel-row actions" role="group" aria-label="片段快速设置">
          <button
            type="button" disabled={locked} className="panel-action"
            title="原画 / 原始比例：完整显示素材原始画面（解除裁剪并把适配改为「适应」，letterbox 黑边）。点「应用」后写入当前片段，影响导出。"
            onClick={onOriginalRatio}
          >
            <PictureInPicture2 size={13} />原画
          </button>
          <button
            type="button" disabled={locked} className="panel-action"
            title="铺满画布：保留当前裁剪，把片段缩放恢复 100% 且位置归零（cover 适配）。影响导出。"
            onClick={onFillCanvas}
          >
            <Maximize2 size={13} />铺满画布
          </button>
          <button
            type="button" disabled={locked || isDefaultTransform(transform)} className="panel-action"
            title="重置变换：清除当前片段的裁剪、缩放与位置（scale=1、位置 0、完整 crop）。点「应用」后写入片段并影响导出。"
            onClick={onResetTransform}
          >
            <RotateCcw size={13} />重置
          </button>
          <button
            type="button" className="panel-action viewer-action"
            title={`适合画布：复位查看比例（${FIT_VIEWER_ZOOM}%），让整个画布完整显示在播放器里。仅调整查看比例，绝不写入片段，也不影响导出。`}
            onClick={onFitView}
          >
            <Maximize2 size={13} />适合画布
          </button>
        </div>

        <div className="transform-panel-row" role="group" aria-label="适配与背景">
          <span className="row-label" title="适配方式决定裁剪区如何放进 9:16 画布，写入片段并影响导出">适配</span>
          {(["cover", "contain"] as FitMode[]).map((mode) => (
            <button
              key={mode}
              type="button" disabled={locked}
              className={`fit-mode-button${transform.fitMode === mode ? " active" : ""}`}
              title={mode === "cover" ? "填充：裁剪区铺满画布（溢出部分裁掉）。影响导出。" : "适应：完整放入画布（露出背景，letterbox）。影响导出。"}
              onClick={() => onTransformChange({ ...transform, fitMode: mode })}
            >
              {mode === "cover" ? "填充" : "适应"}
            </button>
          ))}
          <span className="transform-separator" aria-hidden />
          {(["blur", "color"] as BackgroundMode[]).map((mode) => (
            <button
              key={mode}
              type="button"
              disabled={locked || transform.fitMode !== "contain"}
              className={`bg-button${background === mode ? " active" : ""}`}
              title={transform.fitMode === "contain" ? (mode === "blur" ? "模糊背景填充黑边。影响导出。" : "纯色背景填充黑边。影响导出。") : "仅「适应」适配有背景可设置"}
              onClick={() => onBackgroundChange(mode)}
            >
              {mode === "blur" ? "模糊" : "纯色"}
            </button>
          ))}
          <button
            type="button" disabled={locked}
            className={`ratio-lock-button${ratioLock ? " active" : ""}`}
            aria-pressed={ratioLock}
            title={ratioLock ? "拖边手柄也锁定裁剪宽高比（拖四角始终等比；按住 Shift 可临时锁定）" : "拖边手柄自由调整裁剪范围（拖四角始终等比；按住 Shift 可临时锁定比例）"}
            onClick={() => onRatioLockChange(!ratioLock)}
          >
            {ratioLock ? <Lock size={13} /> : <LockOpen size={13} />}比例
          </button>
        </div>

        <div className="transform-panel-row viewer" role="group" aria-label="查看比例">
          <span className="row-label" title="查看比例（viewerZoom）：只改变画布在播放器里的显示大小，不写入片段，也不影响导出">查看比例</span>
          <input
            aria-label="查看比例"
            title="查看比例（viewerZoom）：只影响观看大小，绝不进入片段变换与导出参数"
            type="range" min={MIN_VIEWER_ZOOM} max={MAX_VIEWER_ZOOM} step={1}
            value={Math.round(viewerZoom)}
            onChange={(event) => onViewerZoomChange(Number(event.target.value))}
          />
          <span className="zoom-value" title="当前查看比例（不影响导出）">{Math.round(viewerZoom)}%</span>
        </div>

        <div className="transform-panel-row apply-row" role="group" aria-label="应用或取消">
          <span className="panel-dirty" title="草稿状态：只有点「应用」才会写后端">{dirty ? "有未应用的改动" : "没有改动"}</span>
          <button type="button" className="panel-cancel" onClick={onCancel} aria-label="取消" title="取消：回滚到进入裁切/重构图时的画面设置，不写后端（等同 Esc）">取消</button>
          <button
            type="button" className="panel-apply" onClick={onApply} disabled={!dirty || locked}
            aria-label="应用"
            title={dirty ? "应用：把当前画面设置写入该片段（成片 → PUT transform；时间线片段 → PUT /studio/timeline），导出与刷新恢复都按这组参数" : "没有未应用的改动，无需写入"}
          >应用</button>
        </div>
        <p className="transform-panel-note">片段设置点「应用」后写入并影响导出；查看比例只影响预览。</p>
      </div>
    </div>
  );
}
