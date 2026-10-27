"use client";

// 封面标签页（2026-10-21 精简版）：封面图片区与版式卡片区由 CoverWorkspace 承担，
// 本组件只保留「画面」子区（适合画布 / 比例 / 裁切缩放，沿用既有 TransformPanel）。
// 旧的封面版本管理（版本列表/历史/沿用/撤销/下载预览）已按需求移除。

import { useEffect, useState } from "react";
import { Crop, LoaderCircle, RotateCcw, Sparkles, Undo2 } from "lucide-react";

import { TransformPanel } from "@/components/studio/transform-panel";
import { VIEWER_ASPECT_OPTIONS, type ViewerAspect } from "@/components/studio/viewer-aspect";
import { MAX_VIEWER_ZOOM, MIN_VIEWER_ZOOM } from "@/components/studio/viewer-zoom";
import type { BackgroundMode, ClipTransform } from "@/lib/clip-transform";

import { PHASE2_LABELS, type ClipCoverHistoryEntry } from "./types";

export type CoverPictureTabProps = {
  projectId: string | null;
  clipId: string | null;
  /** 绑定的成片是否就是当前选中的成片（封面动作只对选中成片可用）。 */
  boundToActiveClip: boolean;
  clipName: string | null;
  /** 当前封面预览 URL（带缓存击穿版本号）。 */
  coverImageUrl: string | null;
  coverAssetId: string | null;
  coverRevision: number | null;
  coverVersionLabel: string | null;
  coverStatusText: string;
  title: string | null;
  busy: boolean;
  canUndoCover: boolean;
  undoRevision: number | null;
  onRegenerateCover: () => void;
  onEditTitle: () => void;
  onShowCoverHistory: () => void;
  onAdoptCover: (sourceClipId: string) => void;
  onUndoCover: () => void;
  coverHistory: ClipCoverHistoryEntry[];
  coverHistoryVisible: boolean;
  coverHistoryLoading: boolean;
  adoptOptions: { clip_id: string; name: string }[];
  adoptSource: string | null;
  onAdoptSourceChange: (clipId: string) => void;
  // ===== 封面版本管理（2026-10-13）=====
  coverTaskFailure?: { message: string; instruction: string } | null;
  onRegenerateWithInstruction?: (instruction: string) => void;
  onRetryCoverTask?: () => void;
  onActivateRevision?: (revision: number) => void;
  onDeleteRevision?: (revision: number) => void;
  onPreviewInPlayer?: () => void;
  /** 缺省兜底返回空串（组件内把空串当无图处理）。 */
  artifactUrlFor?: (taskId: string, path: string, download?: boolean) => string;
  coverCacheVersion?: number;

  // ===== 画面 =====
  transform: ClipTransform | null;
  transformLocked: boolean;
  background: BackgroundMode;
  ratioLock: boolean;
  viewerZoom: number;
  viewerAspect: ViewerAspect;
  dirty: boolean;
  onTransformChange: (next: ClipTransform) => void;
  onBackgroundChange: (next: BackgroundMode) => void;
  onRatioLockChange: (next: boolean) => void;
  onViewerZoomChange: (percent: number) => void;
  onFitView: () => void;
  onOriginalRatio: () => void;
  onCanvasMode: () => void;
  onResetTransform: () => void;
  onViewerAspectChange: (aspect: ViewerAspect) => void;
  onApply: () => void;
  onCancel: () => void;
};

const ASPECT_HINT = PHASE2_LABELS.aspect;
const SOURCE_LABELS: Record<string, string> = {
  auto: "自动生成",
  assistant_regenerate: "AI助手重做",
};
const INSTRUCTION_EXAMPLES = "例如：主题更短 / 把标题放在上方 / 不要遮挡人物脸部 / 换成更专业的医疗风格";

export function CoverPictureTab(props: CoverPictureTabProps) {
  const {
    clipId, boundToActiveClip, clipName, coverImageUrl, coverAssetId, coverRevision,
    coverVersionLabel, coverStatusText, title, busy, canUndoCover, undoRevision,
    onEditTitle, onShowCoverHistory, onAdoptCover, onUndoCover,
    coverHistory, coverHistoryLoading, adoptOptions, adoptSource, onAdoptSourceChange,
    coverTaskFailure, onRegenerateWithInstruction, onRetryCoverTask,
    onActivateRevision, onDeleteRevision, onPreviewInPlayer,
    artifactUrlFor, coverCacheVersion,
    transform, transformLocked, background, ratioLock, viewerZoom, viewerAspect, dirty,
    onTransformChange, onBackgroundChange, onRatioLockChange, onViewerZoomChange, onFitView,
    onOriginalRatio, onCanvasMode, onResetTransform, onViewerAspectChange, onApply, onCancel,
  } = props;

  const active = boundToActiveClip && Boolean(clipId);
  const current = transform;
  const rotation = current?.rotation ?? 0;

  const chooseAspect = (aspect: ViewerAspect) => {
    if (aspect === "9:16") {
      onCanvasMode();
      onViewerAspectChange("9:16");
      return;
    }
    if (aspect === "source") {
      onOriginalRatio();
      onViewerAspectChange("source");
      return;
    }
    onViewerAspectChange(aspect);
  };

  return <div className="inspector-cover-picture" data-testid="inspector-cover-picture-tab">
    

    <section className="inspector-block" aria-label="画面">
      <header><strong>画面</strong><span className="inspector-phase2" title={ASPECT_HINT} data-testid="inspector-aspect-tooltip">{ASPECT_HINT}</span></header>
      <div className="inspector-row" role="group" aria-label="预览比例">
        <span className="inspector-label">预览比例</span>
        <button type="button" className={`inspector-chip${viewerAspect === "9:16" ? " active" : ""}`} title={ASPECT_HINT} onClick={() => chooseAspect("9:16")} data-testid="inspector-aspect-9-16">9:16</button>
        <button type="button" className={`inspector-chip${viewerAspect === "1:1" ? " active" : ""}`} title={ASPECT_HINT} onClick={() => chooseAspect("1:1")} data-testid="inspector-aspect-1-1">1:1</button>
        <button type="button" className={`inspector-chip${viewerAspect === "16:9" ? " active" : ""}`} title={ASPECT_HINT} onClick={() => chooseAspect("16:9")} data-testid="inspector-aspect-16-9">16:9</button>
        <button type="button" className={`inspector-chip${viewerAspect === "source" ? " active" : ""}`} title={ASPECT_HINT} onClick={() => chooseAspect("source")} data-testid="inspector-aspect-source">原始比例</button>
        <button type="button" className="inspector-chip" title={ASPECT_HINT} onClick={onCanvasMode} data-testid="inspector-aspect-canvas">适合画布</button>
      </div>
      <p className="inspector-hint">{VIEWER_ASPECT_OPTIONS.map((option) => option.label).join(" / ")}：只改预览框形状；{ASPECT_HINT}</p>

      <div className="inspector-row" role="group" aria-label="旋转">
        <span className="inspector-label">旋转</span>
        <input
          aria-label="旋转角度"
          className="inspector-rotation"
          readOnly
          value={`${rotation}°`}
          title="本版本 ClipTransform.rotation 恒为 0（lib/clip-transform.ts 的契约：写入非 0 会被判非法）；预览旋转由素材自带的显示矩阵处理，属于观看参数，不改片段。"
          data-testid="inspector-rotation"
        />
        <em className="inspector-hint">本版本恒为 0°（写入其他角度不符合 ClipTransform 契约）</em>
        <button type="button" className="inspector-link" disabled={!active || transformLocked} onClick={onResetTransform} data-testid="inspector-transform-reset">
          <RotateCcw size={12} />重置变换
        </button>
      </div>

      {!current && <p className="inspector-hint">当前绑定没有可编辑的画面变换（先选中一条成片或载入素材）。</p>}
      {current && <>
        <div className="inspector-transform-host">
          <TransformPanel
            transform={current}
            background={background}
            ratioLock={ratioLock}
            viewerZoom={viewerZoom}
            clipLabel={clipName || "未选中片段"}
            disabled={!active}
            transformLocked={transformLocked}
            dirty={dirty}
            collapsed={false}
            onToggleCollapse={() => { /* 右栏面板不折叠：折叠只属于预览浮层。 */ }}
            onTransformChange={onTransformChange}
            onBackgroundChange={onBackgroundChange}
            onRatioLockChange={onRatioLockChange}
            onViewerZoomChange={(percent) => onViewerZoomChange(Math.max(MIN_VIEWER_ZOOM, Math.min(MAX_VIEWER_ZOOM, percent)))}
            onFitView={onFitView}
            onOriginalRatio={onOriginalRatio}
            onFillCanvas={onCanvasMode}
            onResetTransform={onResetTransform}
            onApply={onApply}
            onCancel={onCancel}
          />
        </div>
        <p className="inspector-hint"><Crop size={12} />裁切 / 缩放 / 位置 / 旋转在预览画布上直接拖拽或用滚轮；「应用」才写 config（取消不写）。</p>
      </>}
    </section>
  </div>;
}
