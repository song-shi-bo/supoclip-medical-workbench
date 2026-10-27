/**
 * 项目管理弹层的纯展示工具（2026-10-19）：时间格式化 + 状态标签。
 * 独立成纯模块便于单测（不依赖 React / 网络）。
 */

export type ProjectSortKey = "updated_desc" | "updated_asc" | "name" | "cards";

/** updated_at → "YYYY-MM-DD HH:mm"（本地时区）；解析失败原样返回，不猜时间。 */
export function formatProjectTime(updatedAt: string): string {
  if (!updatedAt) return "—";
  const date = new Date(updatedAt);
  if (Number.isNaN(date.getTime())) return updatedAt;
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 项目卡片的状态标签（PG studio_jobs 最新一条；null = 还没有任何任务）。 */
export function projectStatusChip(status: string | null | undefined):
  { label: string; tone: "idle" | "running" | "done" | "warn" | "bad" } {
  switch (status) {
    case null:
    case undefined:
    case "":
      return { label: "未开始", tone: "idle" };
    case "queued":
      return { label: "排队中", tone: "running" };
    case "extracting_highlights":
      return { label: "内容分析中", tone: "running" };
    case "transcript_ready":
      return { label: "转写就绪", tone: "done" };
    case "analysis_ready":
      return { label: "分析完成", tone: "done" };
    case "plan_ready":
      return { label: "规划完成", tone: "done" };
    case "completed":
      return { label: "已完成", tone: "done" };
    case "completed_with_warnings":
      return { label: "已完成（有警告）", tone: "warn" };
    case "completed_with_cleanup_warning":
      return { label: "已完成（清理有警告）", tone: "warn" };
    case "partial_succeeded":
      return { label: "部分成功", tone: "warn" };
    case "failed":
      return { label: "失败", tone: "bad" };
    case "technical_failed":
      return { label: "技术失败", tone: "bad" };
    case "cancelled":
      return { label: "已取消", tone: "idle" };
    default:
      return { label: `状态：${status}`, tone: "running" };
  }
}
