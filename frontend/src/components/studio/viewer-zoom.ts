// 查看比例（viewerZoom）：只改变画布在预览区里的显示尺寸。
//
// 语义红线（与 tooltip 文案一致）：
//  * viewerZoom 是「观看」参数，绝不写入片段 transform，也绝不进入导出参数；
//  * 片段变换（scale / positionX / positionY / crop）才影响导出构图；
//  * 「适合画布」= 把 viewerZoom 复位到 FIT_VIEWER_ZOOM（查看操作）。
//
// 本模块只做百分比钳制，不含任何导出相关数学（导出数学在 lib/clip-transform.ts）。

export const MIN_VIEWER_ZOOM = 25;
export const MAX_VIEWER_ZOOM = 400;
export const FIT_VIEWER_ZOOM = 100;

export const clampViewerZoom = (value: number): number => {
  if (!Number.isFinite(value)) return FIT_VIEWER_ZOOM;
  return Math.min(MAX_VIEWER_ZOOM, Math.max(MIN_VIEWER_ZOOM, Math.round(value)));
};
