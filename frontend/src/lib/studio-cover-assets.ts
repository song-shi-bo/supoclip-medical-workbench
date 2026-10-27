/**
 * 独立封面素材（右侧「封面」板块）纯逻辑（2026-10-20，可单测）。
 *
 * 数据契约：GET /studio/projects/{id}/cover-assets（后端 studio_project_store.
 * list_cover_assets）——只从**当前台账**构建，五元组绑定，状态按磁盘实测。
 * 本模块只做纯换算：比例判定（fit 类名）、批次过滤、汇总；不发请求、不碰 DOM。
 */

export type CoverAssetStatus = "ready" | "failed";

export type CoverAssetItem = {
  project_id: string;
  source_id: string | null;
  clip_id: string;
  clip_name: string;
  render_task_id: string | null;
  revision: number;
  active_revision?: number | null;
  cover_asset_id?: string | null;
  status: CoverAssetStatus;
  reason?: string | null;
  path?: string | null;
  bytes?: number | null;
  kind?: string | null;
  style_id?: string | null;
  updated_at?: string | null;
};

export type CoverAssetsPayload = {
  project_id: string;
  source_id: string | null;
  batch: { clip_count: number; ready: number; failed: number };
  items: CoverAssetItem[];
};

/** 标准竖屏比例 9:16（0.5625）。判定阈值 ±2% 覆盖 1080×1920 / 1086×1920 等取整差异。 */
export const VERTICAL_RATIO = 9 / 16;
const RATIO_TOLERANCE = 0.02;

/**
 * 按媒体**真实宽高**决定适配方式（需求 6/8）：
 * - 标准 9:16 → ``fit-cover``（object-fit: cover，铺满 9:16 容器）；
 * - 非标准历史素材 → ``fit-contain``（object-fit: contain + 背景填充，绝不拉伸）；
 * - 尺寸未知（0/负值/未加载）一律按 contain 防拉伸（宁可留边不可变形）。
 * 只信 naturalWidth/naturalHeight（img）或 videoWidth/videoHeight（video），
 * 不根据文件名或 CSS 猜测。
 */
export function coverFitClass(width: number, height: number): "fit-cover" | "fit-contain" {
  if (!(Number.isFinite(width) && Number.isFinite(height)) || width <= 0 || height <= 0) {
    return "fit-contain";
  }
  return Math.abs(width / height - VERTICAL_RATIO) <= RATIO_TOLERANCE ? "fit-cover" : "fit-contain";
}

/**
 * 批次过滤第二道闸：只保留当前台账 clip 集合内的封面项。
 * 后端清单本就只从当前台账构建（结构上无旧批次）；此函数用于前端在
 * 「台账已切换、封面清单仍在途」的窗口期丢弃过期响应，防止旧批次封面闪现。
 */
export function filterCurrentBatch<T extends { clip_id: string }>(
  items: T[], currentClipIds: ReadonlySet<string>): T[] {
  return items.filter((item) => currentClipIds.has(String(item.clip_id || "")));
}

/** 板块标题的批次汇总（就绪 n / 失败 m）。 */
export function summarizeCoverBatch(items: CoverAssetItem[]): { ready: number; failed: number } {
  let ready = 0;
  let failed = 0;
  items.forEach((item) => {
    if (item.status === "ready") ready += 1;
    else failed += 1;
  });
  return { ready, failed };
}
