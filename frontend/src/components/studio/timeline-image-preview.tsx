"use client";

/**
 * 播放预览的时间线封面静帧层：播放头进入图片实例窗口 [start, start+duration) 时，
 * 画面叠显示该成片封面（与既有封面预览层同款 9:16 画布），由页面负责在窗口内静音。
 *
 * 这是**预览近似**：导出时片头实例会替换内嵌旧片头、插入实例会把正片真实切开让位，
 * 导出文件的时间轴与预览略有出入；本层只回答「这几秒显示的是我的封面」。
 */

import { TIMELINE_COVER_DRAG_TYPE, type TimelineImageClip } from "./timeline-tracks";

export type { TimelineImageClip };

const formatDuration = (seconds: number): string => {
  const total = Math.max(0, seconds);
  const minutes = Math.floor(total / 60);
  const secs = Math.floor(total % 60);
  return `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
};

export function TimelineImagePreview({ instance, clipName, coverUrl }: {
  instance: TimelineImageClip;
  clipName: string | null;
  coverUrl: string;
}) {
  const head = instance.role === "cover_intro";
  return (
    <div
      className="cover-frame-view timeline-image-preview"
      role="img"
      aria-label={`${clipName || "成片"} ${head ? "片头封面" : "时间线封面静帧"}`}
      data-testid="timeline-image-frame-view"
      data-role={instance.role}
      data-start={instance.start_s}
      data-duration={instance.duration_s}
    >
      <div className="cover-frame-canvas" data-testid="timeline-image-frame-canvas">
        <img src={coverUrl} alt={`${clipName || "成片"} 封面静帧`} />
      </div>
      <span className="cover-frame-badge">
        {head
          ? `片头封面 · ${formatDuration(instance.duration_s)}`
          : `封面静帧 @ ${formatDuration(instance.start_s)} · ${formatDuration(instance.duration_s)}`}
      </span>
    </div>
  );
}

export { TIMELINE_COVER_DRAG_TYPE };
