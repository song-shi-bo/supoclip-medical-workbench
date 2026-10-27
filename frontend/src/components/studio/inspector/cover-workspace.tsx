"use client";

/**
 * 封面图片区（2026-10-21 精简版封面工作流）：
 *
 * - 上方图片区：已生成封面（含版型草稿）显示 80×142 竖版缩略图；没有时显示简洁占位提示；
 * - 点击缩略图 → 出现「应用」「删除」两个操作（仅**未应用的版型草稿**提供；
 *   已应用到成片的封面不提供删除，后端也会拒绝）；
 * - 「已应用到成片 / 待应用」等状态不再用独立信息面板展示（2026-10-21 用户决定）：
 *   应用状态仍由 cover_workflow（台账 + cover_replace state）在系统内部保存并驱动
 *   应用/删除的正确性，仅通过 data-pending / data-applied 暴露给测试；
 * - 应用任务的进行中进度（阶段 + 百分比）是唯一保留的瞬态提示；
 * - 生成/应用/删除的进行中状态由页面透传，本组件不持有业务状态。
 */

import { useEffect, useState } from "react";
import { CheckCircle2, ImageIcon, LoaderCircle } from "lucide-react";

import { TIMELINE_COVER_DRAG_TYPE } from "@/components/studio/timeline-tracks";

export type CoverWorkspaceProps = {
  /** null = 未选中切片（或原片视图）：显示占位提示。 */
  clipId: string | null;
  clipName: string | null;
  /** 当前封面图 URL（页面按 cover_path+缓存版本计算；null = 无封面）。 */
  coverImageUrl: string | null;
  /** 台账卡片工作流状态：封面在位/待应用/已应用（只在内部决定操作与测试标记）。 */
  workflow: { hasCover: boolean | null; pending: boolean; applied: boolean; revision?: number | null; style?: string | null };
  busy: boolean;
  /** 应用渲染任务的实时进度（阶段 + 百分比）；null = 没有进行中的应用任务。 */
  applyProgress?: { stage: string; percent: number } | null;
  onApply: () => void;
  onDelete: () => void;
};

export function CoverWorkspace(props: CoverWorkspaceProps) {
  const { clipId, clipName, coverImageUrl, workflow, busy, applyProgress, onApply, onDelete } = props;
  const [actionsOpen, setActionsOpen] = useState(false);
  const pending = Boolean(clipId) && workflow.pending;
  const applied = Boolean(clipId) && workflow.applied;

  const toggleActions = () => {
    // 只有未应用的草稿提供应用/删除；已应用到成片的封面点击缩略图不做任何事。
    if (!pending || !coverImageUrl || busy) return;
    setActionsOpen((value) => !value);
  };
  // 应用/删除完成后（pending 离开）自动收起操作条。
  useEffect(() => { if (!pending) setActionsOpen(false); }, [pending]);

  return <section className="inspector-block" aria-label="封面图片" data-testid="cover-workspace">
    <header><strong>封面</strong></header>

    {!clipId && <p className="inspector-hint" data-testid="cover-workspace-no-clip">
      先在左侧选中一条剪辑成片，再制作封面。
    </p>}

    {clipId && (coverImageUrl
      ? <div className={`cover-workspace-image${pending ? " pending" : ""}`} data-testid="cover-workspace-image"
          data-pending={pending ? "true" : "false"} data-applied={applied ? "true" : "false"}
          role={pending ? "button" : undefined} tabIndex={pending ? 0 : undefined}
          aria-label={pending ? `${clipName || "成片"}封面，点击展开应用/删除操作` : `${clipName || "成片"}封面`}
          title={pending ? "点击展开应用 / 删除操作；也可拖到下方时间线「图片」轨插入封面静帧" : "拖到下方时间线「图片」轨可插入封面静帧"}
          draggable
          onClick={toggleActions}
          onDragStart={(event) => {
            event.dataTransfer.setData(TIMELINE_COVER_DRAG_TYPE, JSON.stringify({ clip_id: clipId }));
            event.dataTransfer.effectAllowed = "copy";
          }}
          onKeyDown={(event) => { if (pending && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); toggleActions(); } }}>
          <img src={coverImageUrl} alt={`${clipName || "成片"}封面`} />
        </div>
      : <div className="cover-workspace-placeholder" data-testid="cover-workspace-placeholder">
          <ImageIcon size={22} />
          <span>{clipName ? `${clipName} 尚未生成封面` : "尚未生成封面"}</span>
          <small>在下方选择版型后点击「制作封面」</small>
        </div>)}

    {clipId && coverImageUrl && applyProgress && <div className="cover-workspace-progress" role="status" aria-live="polite"
        data-testid="cover-workspace-progress" data-percent={applyProgress.percent}>
      <LoaderCircle size={12} />
      <b>{applyProgress.stage || "正在渲染中"}</b>
      <progress max={100} value={applyProgress.percent} aria-label="应用进度" />
      <span>{Math.max(0, Math.min(100, Math.round(applyProgress.percent)))}%</span>
    </div>}

    {clipId && coverImageUrl && pending && actionsOpen && <div className="cover-workspace-actions" role="group" aria-label="封面操作" data-testid="cover-workspace-actions">
      <button type="button" className="inspector-primary" disabled={busy}
          data-testid="cover-workspace-apply"
          title="把该封面作为 0.8s 片头加入当前成片并生成新成片；渲染失败会保留原成片。"
          onClick={onApply}>
        {busy ? <LoaderCircle size={12} /> : <CheckCircle2 size={12} />}应用
      </button>
      <button type="button" disabled={busy} data-testid="cover-workspace-delete"
          title="只删除这份未应用的版型封面素材及其后端文件；切片、成片与其它数据不受影响。"
          onClick={onDelete}>
        {busy ? <LoaderCircle size={12} /> : null}删除
      </button>
    </div>}
  </section>;
}
