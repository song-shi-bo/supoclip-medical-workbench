"use client";

// 剪映式变换舞台：9:16 画布内按 clip-transform 契约矩形摆放视频帧。
// 预览几何与后端 FFmpeg 导出（build_filter_chain）同源 —— 全部坐标来自
// transformToCanvasRect / computeCropPixels 的 pixel-align 整数。
//
// 三条互不干扰的轴（语义红线）：
//  1) viewerZoom（查看比例，只影响显示尺寸）+ viewerAspect（预览框形状，
//     9:16 / 1:1 / 16:9 / 素材原始比例）——两者都**绝不**写入 transform，
//     也绝不进入导出参数；真实导出画布恒为 9:16，被完整 contain 在预览框里。
//  2) clip scale / position / crop（片段变换，走 onTransformChange）——影响导出构图。
//  3) 应用/取消：本组件只把改动交给 onTransformChange（页面写入草稿），
//     是否持久化由页面的「应用」决定。
//
// 交互分层：
//  * 普通播放：拖动画面不改变素材；滚轮 = 查看缩放（viewerZoom）。
//  * 裁切/重构图模式：拖画面内部 = 移动片段位置；滚轮 = 片段缩放；
//    8 控制点（四角 + 四边中点）= 调整裁剪区：四角恒等比、四边自由、
//    按住 Shift（或面板比例锁）锁定宽高比；Esc 由调用方退出并回滚草稿。

import { CSSProperties, PointerEvent as ReactPointerEvent, SyntheticEvent, useEffect, useRef, useState, type RefObject } from "react";

import {
  CANVAS_HEIGHT,
  CANVAS_WIDTH,
  MIN_CROP_FRACTION,
  MAX_SCALE,
  MIN_SCALE,
  computeCropPixels,
  clamp,
  transformToCanvasRect,
  type BackgroundMode,
  type ClipTransform,
  type CropRect,
} from "@/lib/clip-transform";
import { clampViewerZoom } from "@/components/studio/viewer-zoom";
import { viewerAspectRatio, viewerFrameSize, type ViewerAspect } from "@/components/studio/viewer-aspect";

export type CropHandleId = "nw" | "n" | "ne" | "e" | "se" | "s" | "sw" | "w";

type TransformStageProps = {
  videoUrl: string | null;
  videoRef: RefObject<HTMLVideoElement | null>;
  sourceWidth: number;
  sourceHeight: number;
  transform: ClipTransform;
  background: BackgroundMode;
  // 裁切/重构图模式：白色边框 + 8 控制点 + 允许拖动画面与滚轮缩放片段。
  transformMode: boolean;
  // 查看比例（百分比，100 = 适合画布）。只影响显示尺寸，不进导出。
  viewerZoom: number;
  // 预览框形状（9:16 / 1:1 / 16:9 / 素材原始比例）。只影响预览，不进导出。
  viewerAspect?: ViewerAspect;
  // 裁切模式下为浮层预留的像素高度（只影响观看布局，不改变换）。
  bottomReserve?: number;
  ratioLock?: boolean;
  showGuides?: boolean;
  loop?: boolean;
  transformLocked?: boolean;
  onTransformChange: (next: ClipTransform) => void;
  onViewerZoomChange?: (percent: number) => void;
  onLoadedMetadata?: (event: SyntheticEvent<HTMLVideoElement>) => void;
  onLoadedData?: (event: SyntheticEvent<HTMLVideoElement>) => void;
  onTimeUpdate?: (event: SyntheticEvent<HTMLVideoElement>) => void;
  onPlay?: () => void;
  onPause?: () => void;
};

const HANDLE_CURSORS: Record<CropHandleId, string> = {
  nw: "nwse-resize", n: "ns-resize", ne: "nesw-resize", e: "ew-resize",
  se: "nwse-resize", s: "ns-resize", sw: "nesw-resize", w: "ew-resize",
};
// 四角 + 四边中点 = 8 个控制点（剪映式）。
export const CROP_HANDLES: CropHandleId[] = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
const HANDLE_LABELS: Record<CropHandleId, string> = {
  nw: "左上角裁剪手柄", n: "上边裁剪手柄", ne: "右上角裁剪手柄", e: "右边裁剪手柄",
  se: "右下角裁剪手柄", s: "下边裁剪手柄", sw: "左下角裁剪手柄", w: "左边裁剪手柄",
};

export const isCornerHandle = (handle: CropHandleId): boolean => handle.length === 2;

// 只读观测钩子：把归一化裁剪区暴露成 data 属性，便于真实浏览器接受测试断言
// （不影响渲染与持久化；持久化仍走页面既有的 transformPayload）。
export const cropDataAttribute = (transform: ClipTransform): string =>
  [transform.crop.left, transform.crop.top, transform.crop.right, transform.crop.bottom]
    .map((value) => Math.round(value * 10000) / 10000)
    .join(",");

const repairAxis = (low: number, high: number): [number, number] => {
  let nextLow = low;
  let nextHigh = high;
  if (nextLow > nextHigh) [nextLow, nextHigh] = [nextHigh, nextLow];
  if (nextHigh - nextLow < MIN_CROP_FRACTION) {
    const centre = (nextLow + nextHigh) / 2;
    nextLow = centre - MIN_CROP_FRACTION / 2;
    nextHigh = centre + MIN_CROP_FRACTION / 2;
  }
  if (nextLow < 0) [nextLow, nextHigh] = [0, MIN_CROP_FRACTION];
  if (nextHigh > 1) [nextLow, nextHigh] = [1 - MIN_CROP_FRACTION, 1];
  return [nextLow, nextHigh];
};

export function TransformStage(props: TransformStageProps) {
  const {
    videoUrl, videoRef, sourceWidth, sourceHeight, transform, background, transformMode,
    viewerZoom, viewerAspect = "9:16", ratioLock = false, showGuides = false, loop = false,
    bottomReserve = 0, transformLocked = false, onTransformChange, onViewerZoomChange,
    onLoadedMetadata, onLoadedData, onTimeUpdate, onPlay, onPause,
  } = props;
  const viewportRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const blurVideoRef = useRef<HTMLVideoElement>(null);
  const panState = useRef<{ pointerId: number; startX: number; startY: number; originX: number; originY: number } | null>(null);
  const cropState = useRef<{ pointerId: number; handle: CropHandleId; startCrop: CropRect; startX: number; startY: number; canvasPerNormX: number; canvasPerNormY: number } | null>(null);
  const [stageWidth, setStageWidth] = useState(360);
  // 播放器画面区（.transform-viewport）尺寸：用来把真实 9:16 画布按所选的
  // 预览框形状 contain 进去（竖屏左右留黑边 / 横屏上下留黑边）。
  const [viewport, setViewport] = useState({ width: 0, height: 0 });
  // 手势期间冻结的画面拟合比例：拖动/裁剪时若实时重算拟合，画面会跟着缩放，
  // 指针与画面的 1:1 跟随关系会被破坏。手势结束即恢复实时拟合。
  const [frozenFitScale, setFrozenFitScale] = useState<number | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;
    // 画布尺寸已含 viewerZoom（CSS 高度倍数），所以这里的宽度天然是「屏幕像素」，
    // 拖动/裁剪换算随查看比例自动一致，无需在数学里再乘一次。
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width;
      if (width) setStageWidth(width);
    });
    observer.observe(canvas);
    return () => observer.disconnect();
  }, []);

  // 画面区尺寸：预览框是绝对定位/由 JS 定尺寸，不受画布影响，不会形成测量回环。
  useEffect(() => {
    const viewportEl = viewportRef.current;
    if (!viewportEl) return undefined;
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect?.width) setViewport({ width: rect.width, height: rect.height });
    });
    observer.observe(viewportEl);
    return () => observer.disconnect();
  }, []);

  // 滚轮：非 passive 监听才能阻止页面滚动。
  // 普通播放 = 查看缩放（viewerZoom，不影响导出）；变换模式 = 片段缩放（影响导出）。
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const factor = event.deltaY < 0 ? 1.08 : 1 / 1.08;
      if (transformMode) {
        if (transformLocked) return;
        onTransformChange({ ...transform, scale: clamp(transform.scale * factor, MIN_SCALE, MAX_SCALE) });
        return;
      }
      onViewerZoomChange?.(clampViewerZoom(viewerZoom * factor));
    };
    canvas.addEventListener("wheel", onWheel, { passive: false });
    return () => canvas.removeEventListener("wheel", onWheel);
  }, [transform, transformMode, transformLocked, viewerZoom, onTransformChange, onViewerZoomChange]);

  // 模糊背景副本与主画面的播放/ seek 同步（导出侧由 FFmpeg gblur 精确合成）。
  useEffect(() => {
    const main = videoRef.current;
    const blurred = blurVideoRef.current;
    if (!main || !blurred) return undefined;
    blurred.currentTime = main.currentTime;
    const syncPlay = () => { void blurred.play().catch(() => undefined); };
    const syncPause = () => blurred.pause();
    const syncSeek = () => {
      if (Math.abs(blurred.currentTime - main.currentTime) > 0.25) blurred.currentTime = main.currentTime;
    };
    main.addEventListener("play", syncPlay);
    main.addEventListener("pause", syncPause);
    main.addEventListener("seeked", syncSeek);
    return () => {
      main.removeEventListener("play", syncPlay);
      main.removeEventListener("pause", syncPause);
      main.removeEventListener("seeked", syncSeek);
    };
  }, [videoRef, videoUrl, background, transform.fitMode]);

  // 素材被移除时（videoUrl 变空）必须**显式重置** <video>：只把 src 设空，浏览器仍会
  // 保留最后一帧画面——用户看到的现象就是「素材删掉了，播放器里画面还在」。
  // 这里 pause → 去 src → load()，让元素回到无帧状态并释放已解码数据。
  useEffect(() => {
    if (videoUrl) return;
    for (const element of [videoRef.current, blurVideoRef.current]) {
      if (!element) continue;
      try { element.pause(); } catch { /* 忽略：元素可能尚未加载 */ }
      element.removeAttribute("src");
      try { element.load(); } catch { /* 忽略：测试环境或不支持 load() */ }
    }
  }, [videoUrl, videoRef, blurVideoRef]);

  const scale = stageWidth / CANVAS_WIDTH;
  const width = sourceWidth >= 2 ? sourceWidth : CANVAS_WIDTH;
  const height = sourceHeight >= 2 ? sourceHeight : CANVAS_HEIGHT;
  const rect = transformToCanvasRect(width, height, transform);
  const crop = computeCropPixels(transform.crop, width, height);
  const sourceScale = rect.w / crop.w;

  // 预览框：只由 viewerAspect + 画面区尺寸决定，与 transform 无关（不进导出）。
  const frame = viewerFrameSize(viewport.width, viewport.height, viewerAspectRatio(viewerAspect, width, height));
  const frameHasSize = frame.width > 0 && frame.height > 0;

  // 拖拽处理器的实时几何上下文：指针事件可能在两次渲染之间到达，
  // 必须读 ref（当前渲染值），不能依赖事件处理器闭包里的旧几何。
  const dragGeometryRef = useRef({ transform, rect, scale, width, height });
  dragGeometryRef.current = { transform, rect, scale, width, height };

  const frameStyle = {
    left: `${rect.x * scale}px`,
    top: `${rect.y * scale}px`,
    width: `${rect.w * scale}px`,
    height: `${rect.h * scale}px`,
  };
  const videoStyle = {
    width: `${width * sourceScale * scale}px`,
    height: `${height * sourceScale * scale}px`,
    left: `${-crop.x * sourceScale * scale}px`,
    top: `${-crop.y * sourceScale * scale}px`,
  };
  // 裁剪框在画布坐标系里就是帧矩形本身（crop 左上角 = rect 左上角）。
  const cropBoxStyle = {
    left: frameStyle.left,
    top: frameStyle.top,
    width: frameStyle.width,
    height: frameStyle.height,
  };

  // 普通播放模式下拖动画面不做任何事（点画面不改变素材）；
  // 只有进入裁切/重构图模式才允许拖动画面 = 移动片段位置。
  const beginPan = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!transformMode || transformLocked || !videoUrl) return;
    setFrozenFitScale(computedFitScale || null);
    panState.current = {
      pointerId: event.pointerId, startX: event.clientX, startY: event.clientY,
      originX: transform.positionX, originY: transform.positionY,
    };
    // 合成/重放的指针事件可能携带无效 pointerId；捕获失败不应中断平移状态。
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      /* 无捕获时依赖 move 事件命中画布 */
    }
  };
  const movePan = (event: ReactPointerEvent<HTMLDivElement>) => {
    const state = panState.current;
    if (!state || state.pointerId !== event.pointerId) return;
    const { transform: current, scale: k } = dragGeometryRef.current;
    onTransformChange({
      ...current,
      positionX: state.originX + (event.clientX - state.startX) / k,
      positionY: state.originY + (event.clientY - state.startY) / k,
    });
  };
  const endPan = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (panState.current?.pointerId === event.pointerId) {
      panState.current = null;
      setFrozenFitScale(null);
    }
  };

  const beginCrop = (handle: CropHandleId) => (event: ReactPointerEvent<HTMLElement>) => {
    event.stopPropagation();
    event.preventDefault();
    const { rect: currentRect, transform: current } = dragGeometryRef.current;
    // 手势内冻结拟合比例，保证拖动手柄时裁剪框随指针 1:1 移动。
    setFrozenFitScale(computedFitScale || null);
    const cropWNorm = Math.max(current.crop.right - current.crop.left, MIN_CROP_FRACTION);
    const cropHNorm = Math.max(current.crop.bottom - current.crop.top, MIN_CROP_FRACTION);
    cropState.current = {
      pointerId: event.pointerId, handle, startCrop: { ...current.crop },
      startX: event.clientX, startY: event.clientY,
      // 按下瞬间冻结换算比例：拖拽期间 rect 随裁剪实时变化，不能实时取。
      canvasPerNormX: currentRect.w / cropWNorm,
      canvasPerNormY: currentRect.h / cropHNorm,
    };
    // 合成/重放的指针事件可能携带无效 pointerId；捕获失败不应中断裁剪状态。
    try {
      (event.currentTarget.parentElement as HTMLDivElement).setPointerCapture(event.pointerId);
    } catch {
      /* 无捕获时依赖 move 事件命中裁剪层 */
    }
  };
  const moveCrop = (event: ReactPointerEvent<HTMLDivElement>) => {
    const state = cropState.current;
    if (!state || state.pointerId !== event.pointerId) return;
    const { transform: current, scale: k, width, height } = dragGeometryRef.current;
    // canvasPerNorm 在按下瞬间冻结：拖拽期间 rect 随裁剪变化，不能实时取。
    const nextCrop = resolveCropDrag({
      handle: state.handle,
      startCrop: state.startCrop,
      // 指针位移 → 归一化裁剪位移（先除画布显示比例，再除当前裁剪跨度）。
      deltaNormX: (event.clientX - state.startX) / k / state.canvasPerNormX,
      deltaNormY: (event.clientY - state.startY) / k / state.canvasPerNormY,
      sourceWidth: width,
      sourceHeight: height,
      ratioLock,
      shiftKey: event.shiftKey,
    });
    onTransformChange({ ...current, crop: nextCrop });
  };
  const endCrop = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (cropState.current?.pointerId === event.pointerId) {
      cropState.current = null;
      setFrozenFitScale(null);
    }
  };

  const showBlurBackground = background === "blur" && transform.fitMode === "contain" && !!videoUrl;
  const stageClass = `transform-canvas${transformMode ? " crop-mode" : ""}${transformLocked ? " locked" : ""}`;
  // 查看比例只改显示尺寸（CSS 变量），不改 transform，也不进导出。
  // 裁切模式额外把「整幅画面 + 裁剪框」缩放到预览可见范围内（剪映在裁剪页同样
  // 显示整幅画面），否则 cover 适配下画面左右溢出、四个角的手柄会被画布裁掉。
  // 这条拟合只影响观看尺寸，不写回任何变换值。
  // 裁切模式下浮层会占据预览区右下角，这里给它留出空间，保证 8 个控制点都不被遮挡。
  // 拟合依据是裁剪框（rect）相对画布中心的伸出距离：画布仍居中，画面/裁剪框随
  // 拖动实时移动（拖动可见），且四个角与四边中点的手柄始终落在可见范围内。
  const HANDLE_SAFE_PADDING = 22;
  const availWidth = Math.max(40, (frameHasSize ? frame.width : viewport.width) - HANDLE_SAFE_PADDING * 2);
  const availHeight = Math.max(40, (frameHasSize ? frame.height : viewport.height) - bottomReserve - HANDLE_SAFE_PADDING * 2);
  const halfCanvasW = CANVAS_WIDTH / 2;
  const halfCanvasH = CANVAS_HEIGHT / 2;
  const reachX = Math.max(halfCanvasW - rect.x, rect.x + rect.w - halfCanvasW, 1);
  const reachY = Math.max(halfCanvasH - rect.y, rect.y + rect.h - halfCanvasH, 1);
  const computedFitScale = transformMode && viewport.width > 0 && viewport.height > 0
    ? Math.max(0.01, Math.min(availWidth / 2 / reachX, availHeight / 2 / reachY))
    : 0;
  const fitScale = frozenFitScale && transformMode ? frozenFitScale : computedFitScale;
  const displayScale = fitScale > 0 ? fitScale * (viewerZoom / 100) : 0;
  // CSS 自定义属性需要显式类型（CSSProperties 不含 --* 键）。
  type StageStyle = CSSProperties & { "--viewer-zoom": number };
  const stageStyle: StageStyle = displayScale > 0
    ? {
      "--viewer-zoom": viewerZoom / 100,
      // 显式定位：画布在「预览框减去浮层高度」的可用区域里居中。
      left: `${HANDLE_SAFE_PADDING + (availWidth - CANVAS_WIDTH * displayScale) / 2}px`,
      top: `${HANDLE_SAFE_PADDING + (availHeight - CANVAS_HEIGHT * displayScale) / 2}px`,
      width: `${CANVAS_WIDTH * displayScale}px`,
      height: `${CANVAS_HEIGHT * displayScale}px`,
      transform: "none",
    }
    : { "--viewer-zoom": viewerZoom / 100 };

  return (
    <div className="transform-viewport" ref={viewportRef} data-viewer-aspect={viewerAspect}>
      <div
        className={`viewer-frame${transformMode ? " crop-mode" : ""}`}
        data-viewer-aspect={viewerAspect}
        style={frameHasSize ? { width: `${frame.width}px`, height: `${frame.height}px` } : { width: "100%", height: "100%" }}
      >
        <div
          ref={canvasRef}
          className={stageClass}
          style={stageStyle}
          data-background={background}
          onPointerDown={beginPan}
          onPointerMove={(event) => { movePan(event); moveCrop(event); }}
          onPointerUp={(event) => { endPan(event); endCrop(event); }}
          onPointerCancel={(event) => { endPan(event); endCrop(event); }}
        >
          {showBlurBackground && (
            <div className="canvas-bg-clip" aria-hidden>
              <video
                ref={blurVideoRef}
                src={videoUrl || undefined}
                className="transform-blur-bg"
                muted
                playsInline
                aria-hidden
                tabIndex={-1}
              />
            </div>
          )}
          <div className="transform-frame" style={frameStyle}>
            <video
              ref={videoRef}
              src={videoUrl || undefined}
              className="transform-video"
              playsInline
              preload="auto"
              loop={loop}
              onLoadedMetadata={onLoadedMetadata}
              onLoadedData={onLoadedData}
              onTimeUpdate={onTimeUpdate}
              onPlay={onPlay}
              onPause={onPause}
              style={videoStyle}
            />
          </div>
          {showGuides && videoUrl && <i className="canvas-guides" aria-hidden />}
          {transformMode && videoUrl && (
            <>
              {/* 导出边界：白线内是导出画面，线外被裁掉（纯观看提示）。 */}
              <i className="export-boundary" aria-hidden />
              {/* data-crop 是只读观测钩子（归一化裁剪区），供真实浏览器接受测试断言使用，
                不参与任何渲染/持久化逻辑。 */}
            <div className="crop-overlay" data-crop={cropDataAttribute(transform)} style={cropBoxStyle}>
                <i className="crop-grid" aria-hidden />
                {CROP_HANDLES.map((handle) => (
                  <button
                    key={handle}
                    type="button"
                    aria-label={HANDLE_LABELS[handle]}
                    title={`${HANDLE_LABELS[handle]}：拖动调整裁剪区（${isCornerHandle(handle) ? "四角等比缩放" : "四边自由调整"}；按住 Shift 锁定比例）`}
                    className={`crop-handle handle-${handle}${isCornerHandle(handle) ? " corner-handle" : " edge-handle"}`}
                    style={{ cursor: HANDLE_CURSORS[handle] }}
                    onPointerDown={beginCrop(handle)}
                  />
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function isHorizontalResize(handle: CropHandleId): boolean {
  return handle.includes("w") || handle.includes("e");
}
function isVerticalResize(handle: CropHandleId): boolean {
  return handle === "n" || handle === "s" || handle === "nw" || handle === "ne" || handle === "sw" || handle === "se";
}

/**
 * 裁剪框拖拽的纯函数（可单测，不依赖 DOM）。规则与 tooltip 一致：
 *  * 四角（nw/ne/se/sw）= 始终等比缩放；
 *  * 四边中点（n/e/s/w）= 默认只改该方向的范围；
 *  * 按住 Shift 或面板比例锁打开 = 四边也锁定宽高比。
 */
export function resolveCropDrag(input: {
  handle: CropHandleId;
  startCrop: CropRect;
  deltaNormX: number;
  deltaNormY: number;
  sourceWidth: number;
  sourceHeight: number;
  ratioLock: boolean;
  shiftKey: boolean;
}): CropRect {
  const { handle, startCrop, deltaNormX, deltaNormY, sourceWidth, sourceHeight, ratioLock, shiftKey } = input;
  let { left, top, right, bottom } = startCrop;
  if (handle.includes("w")) left += deltaNormX;
  if (handle.includes("e")) right += deltaNormX;
  if (handle === "n" || handle === "nw" || handle === "ne") top += deltaNormY;
  if (handle === "s" || handle === "sw" || handle === "se") bottom += deltaNormY;
  let nextCrop: CropRect = { left, top, right, bottom };
  const lockAspect = isCornerHandle(handle) || ratioLock || shiftKey;
  if (lockAspect && (isHorizontalResize(handle) || isVerticalResize(handle))) {
    // 锁定用的宽高比取**按下瞬间**的裁剪区（不是已被本次位移改过的值）。
    const width0 = Math.max(startCrop.right - startCrop.left, MIN_CROP_FRACTION);
    const height0 = Math.max(startCrop.bottom - startCrop.top, MIN_CROP_FRACTION);
    const lockedAspect = (width0 * sourceWidth) / (height0 * sourceHeight);
    if (isHorizontalResize(handle)) {
      const newWNorm = Math.max(MIN_CROP_FRACTION, nextCrop.right - nextCrop.left);
      const wantHNorm = clamp((newWNorm * sourceWidth) / lockedAspect / sourceHeight, MIN_CROP_FRACTION, 1);
      nextCrop = keepAspectAroundCentre(nextCrop, (top + bottom) / 2, wantHNorm, "y");
    } else {
      const newHNorm = Math.max(MIN_CROP_FRACTION, nextCrop.bottom - nextCrop.top);
      const wantWNorm = clamp((newHNorm * sourceHeight * lockedAspect) / sourceWidth, MIN_CROP_FRACTION, 1);
      nextCrop = keepAspectAroundCentre(nextCrop, (left + right) / 2, wantWNorm, "x");
    }
  }
  const [fixedLeft, fixedRight] = repairAxis(nextCrop.left, nextCrop.right);
  const [fixedTop, fixedBottom] = repairAxis(nextCrop.top, nextCrop.bottom);
  return { left: fixedLeft, top: fixedTop, right: fixedRight, bottom: fixedBottom };
}

// 围绕指定轴的当前中心，把另一轴的跨度调整为 wantSpan（含边界钳制）。
function keepAspectAroundCentre(crop: CropRect, centre: number, wantSpan: number, axis: "x" | "y"): CropRect {
  let low = centre - wantSpan / 2;
  let high = centre + wantSpan / 2;
  if (low < 0) [low, high] = [0, wantSpan];
  if (high > 1) [low, high] = [1 - wantSpan, 1];
  return axis === "x" ? { ...crop, left: low, right: high } : { ...crop, top: low, bottom: high };
}
