// 预览画布比例（viewer-only）：只改变「预览画布在播放器里的显示形状」。
//
// 语义红线（与 tooltip 文案一致）：
//  * 它是**观看**参数：既不写入 ClipTransform，也不进入导出参数；
//  * 导出仍由流水线固定输出 9:16（lib/clip-transform.ts 的 CANVAS_WIDTH:CANVAS_HEIGHT），
//    选择 1:1 / 16:9 / 原始比例 时，真实的 9:16 导出画布会**完整地**放在所选形状里
//    （左右或上下留黑边），绝不裁切、绝不拉伸，所以「预览 = 导出」的构图关系不变。
//
// 本模块只做比例换算，不含任何导出相关数学（导出数学在 lib/clip-transform.ts）。

export type ViewerAspect = "9:16" | "1:1" | "16:9" | "source";

export const DEFAULT_VIEWER_ASPECT: ViewerAspect = "9:16";

// 比例菜单的 tooltip 原文（验收报告需要逐字引用）。
export const VIEWER_ASPECT_TOOLTIP = "仅影响预览画布；导出仍按流水线 9:16（不改导出参数）。";

export type ViewerAspectOption = {
  id: ViewerAspect;
  label: string;
  hint: string;
};

export const VIEWER_ASPECT_OPTIONS: ViewerAspectOption[] = [
  { id: "9:16", label: "9:16", hint: "竖屏画布（与导出流水线一致）" },
  { id: "1:1", label: "1:1", hint: "方形预览框，9:16 导出画面居中完整显示" },
  { id: "16:9", label: "16:9", hint: "横屏预览框，9:16 导出画面居中完整显示" },
  { id: "source", label: "原始比例", hint: "按素材原始宽高比预览，9:16 导出画面居中完整显示" },
];

/**
 * 预览框的宽高比。
 * @param aspect 用户选择的预览比例
 * @param sourceWidth 素材像素宽（source 比例时使用）
 * @param sourceHeight 素材像素高（source 比例时使用）
 */
export function viewerAspectRatio(aspect: ViewerAspect, sourceWidth: number, sourceHeight: number): number {
  if (aspect === "1:1") return 1;
  if (aspect === "16:9") return 16 / 9;
  if (aspect === "source") {
    const width = Number.isFinite(sourceWidth) && sourceWidth > 0 ? sourceWidth : 1080;
    const height = Number.isFinite(sourceHeight) && sourceHeight > 0 ? sourceHeight : 1920;
    return width / height;
  }
  return 9 / 16;
}

/** 在给定视口里按 contain 语义求出预览框尺寸（取整，最小 1px）。 */
export function viewerFrameSize(
  viewportWidth: number,
  viewportHeight: number,
  ratio: number,
): { width: number; height: number } {
  const safeWidth = Number.isFinite(viewportWidth) && viewportWidth > 0 ? viewportWidth : 0;
  const safeHeight = Number.isFinite(viewportHeight) && viewportHeight > 0 ? viewportHeight : 0;
  const safeRatio = Number.isFinite(ratio) && ratio > 0 ? ratio : 9 / 16;
  if (safeWidth <= 0 || safeHeight <= 0) return { width: 0, height: 0 };
  const width = Math.min(safeWidth, safeHeight * safeRatio);
  return { width: Math.max(1, Math.round(width)), height: Math.max(1, Math.round(width / safeRatio)) };
}
