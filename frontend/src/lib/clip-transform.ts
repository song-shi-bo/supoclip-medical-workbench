// 预览/导出同源数学：backend/src/clip_transform.py 的 TypeScript 镜像。
//
// 播放器预览（CSS 定位）与后端 FFmpeg 导出（build_filter_chain）都必须从
// transformToCanvasRect 的同一套推导取值，保证输出 9:16 时构图逐像素一致。
// 舍入契约（与 Python 端严格一致）：
//   * 偏移四舍五入：Math.floor(value + 0.5)
//   * 尺寸向下取偶（H.264/yuv420p），最小 2px
// 任何一端的修改必须同步另一端，并跑两边的一致性测试
// （frontend clip-transform.test.ts × backend test_clip_transform.py）。

export const CANVAS_WIDTH = 1080;
export const CANVAS_HEIGHT = 1920;
export const MIN_SCALE = 0.1;
export const MAX_SCALE = 8.0;
export const MIN_CROP_FRACTION = 0.05;
export const MAX_POSITION_OFFSET = 2.0;

export type FitMode = "cover" | "contain";
export type BackgroundMode = "blur" | "color";

export type CropRect = { left: number; top: number; right: number; bottom: number };

export type ClipTransform = {
  scale: number;
  positionX: number;
  positionY: number;
  crop: CropRect;
  fitMode: FitMode;
  rotation: 0;
};

export type PixelRect = { x: number; y: number; w: number; h: number };
export type CanvasRect = { x: number; y: number; w: number; h: number };

const roundHalfAway = (value: number): number => Math.floor(value + 0.5);

const roundToEven = (value: number, minimum = 2): number => {
  const rounded = roundHalfAway(value);
  const even = rounded - (rounded % 2);
  return Math.max(minimum, even);
};

export const clamp = (value: number, low: number, high: number): number => Math.max(low, Math.min(high, value));

const toFiniteNumber = (value: unknown, field: string): number => {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${field} 必须是有限数字`);
  }
  return value;
};

export function defaultTransform(): ClipTransform {
  return {
    scale: 1,
    positionX: 0,
    positionY: 0,
    crop: { left: 0, top: 0, right: 1, bottom: 1 },
    fitMode: "cover",
    rotation: 0,
  };
}

const repairCrop = (raw: unknown): CropRect => {
  if (raw == null) return { left: 0, top: 0, right: 1, bottom: 1 };
  if (typeof raw !== "object") throw new Error("crop 必须是对象");
  const source = raw as Record<string, unknown>;
  const scalar = (key: string, fallback: number): number =>
    source[key] == null ? fallback : clamp(toFiniteNumber(source[key] as number, `crop.${key}`), 0, 1);
  let left = scalar("left", 0);
  let right = scalar("right", 1);
  let top = scalar("top", 0);
  let bottom = scalar("bottom", 1);
  if (left > right) [left, right] = [right, left];
  if (top > bottom) [top, bottom] = [bottom, top];
  const widen = (low: number, high: number): [number, number] => {
    if (high - low >= MIN_CROP_FRACTION) return [low, high];
    const centre = (low + high) / 2;
    let nextLow = centre - MIN_CROP_FRACTION / 2;
    let nextHigh = centre + MIN_CROP_FRACTION / 2;
    if (nextLow < 0) {
      nextLow = 0;
      nextHigh = MIN_CROP_FRACTION;
    } else if (nextHigh > 1) {
      nextLow = 1 - MIN_CROP_FRACTION;
      nextHigh = 1;
    }
    return [nextLow, nextHigh];
  };
  [left, right] = widen(left, right);
  [top, bottom] = widen(top, bottom);
  return { left, top, right, bottom };
};

// 解析并归一化一个 transform 载荷（后端 wire 格式）；缺省补默认、越界钳制、
// 乱序修复。与后端 parse_transform 行为一致。
export function parseTransform(raw: unknown): ClipTransform {
  if (raw == null) return defaultTransform();
  if (typeof raw !== "object") throw new Error("transform 必须是对象");
  const source = raw as Record<string, unknown>;
  const scalar = (key: string, fallback: number): number =>
    source[key] == null ? fallback : toFiniteNumber(source[key] as number, key);
  const scale = clamp(scalar("scale", 1), MIN_SCALE, MAX_SCALE);
  const positionX = clamp(scalar("positionX", 0), -MAX_POSITION_OFFSET * CANVAS_WIDTH, MAX_POSITION_OFFSET * CANVAS_WIDTH);
  const positionY = clamp(scalar("positionY", 0), -MAX_POSITION_OFFSET * CANVAS_HEIGHT, MAX_POSITION_OFFSET * CANVAS_HEIGHT);
  const fitModeRaw = source.fitMode == null ? "cover" : source.fitMode;
  if (fitModeRaw !== "cover" && fitModeRaw !== "contain") throw new Error("fitMode 只能是 cover/contain");
  const rotationRaw = source.rotation == null ? 0 : toFiniteNumber(source.rotation as number, "rotation");
  if (rotationRaw !== 0) throw new Error("rotation 本版本恒为 0");
  return { scale, positionX, positionY, crop: repairCrop(source.crop), fitMode: fitModeRaw, rotation: 0 };
}

export function isDefaultTransform(transform: ClipTransform): boolean {
  const reset = defaultTransform();
  return (
    transform.scale === reset.scale &&
    transform.positionX === reset.positionX &&
    transform.positionY === reset.positionY &&
    transform.fitMode === reset.fitMode &&
    transform.rotation === reset.rotation &&
    transform.crop.left === reset.crop.left &&
    transform.crop.top === reset.crop.top &&
    transform.crop.right === reset.crop.right &&
    transform.crop.bottom === reset.crop.bottom
  );
}

// 归一化 crop → 源像素 crop 矩形（偶数尺寸，偏移钳在画面内）。
export function computeCropPixels(crop: CropRect, sourceWidth: number, sourceHeight: number): PixelRect {
  let width = roundToEven((crop.right - crop.left) * sourceWidth);
  let height = roundToEven((crop.bottom - crop.top) * sourceHeight);
  width = Math.min(width, roundToEven(sourceWidth));
  height = Math.min(height, roundToEven(sourceHeight));
  const x = clamp(roundHalfAway(crop.left * sourceWidth), 0, sourceWidth - width);
  const y = clamp(roundHalfAway(crop.top * sourceHeight), 0, sourceHeight - height);
  return { x, y, w: width, h: height };
}

// fitMode 基准缩放：cover=撑满画布（溢出裁掉）；contain=完整放入（露背景）。
export function computeBaseScale(cropWidth: number, cropHeight: number, canvasWidth: number, canvasHeight: number, fitMode: FitMode): number {
  if (cropWidth <= 0 || cropHeight <= 0) throw new Error("裁剪区像素尺寸必须大于 0");
  const widthRatio = canvasWidth / cropWidth;
  const heightRatio = canvasHeight / cropHeight;
  return fitMode === "cover" ? Math.max(widthRatio, heightRatio) : Math.min(widthRatio, heightRatio);
}

const scaledFrameSize = (crop: PixelRect, transform: ClipTransform, canvasWidth: number, canvasHeight: number): [number, number] => {
  const base = computeBaseScale(crop.w, crop.h, canvasWidth, canvasHeight, transform.fitMode);
  let width = roundToEven(crop.w * base * transform.scale);
  let height = roundToEven(crop.h * base * transform.scale);
  if (transform.fitMode === "cover" && transform.scale >= 1) {
    // 取偶可能把贴边帧削小 1-2px 而露出黑缝；回贴画布（与后端一致）。
    width = Math.max(width, roundToEven(canvasWidth));
    height = Math.max(height, roundToEven(canvasHeight));
  }
  return [width, height];
};

// 预览/导出契约：帧在画布上的矩形。pixelAlign=true 时返回的整数与后端
// FFmpeg 产物逐像素一致，前端预览必须使用该模式。
export function transformToCanvasRect(
  sourceWidth: number,
  sourceHeight: number,
  transform: ClipTransform,
  canvasWidth: number = CANVAS_WIDTH,
  canvasHeight: number = CANVAS_HEIGHT,
  pixelAlign: boolean = true,
): CanvasRect {
  const crop = computeCropPixels(transform.crop, sourceWidth, sourceHeight);
  if (pixelAlign) {
    const [width, height] = scaledFrameSize(crop, transform, canvasWidth, canvasHeight);
    return {
      x: roundHalfAway((canvasWidth - width) / 2 + transform.positionX),
      y: roundHalfAway((canvasHeight - height) / 2 + transform.positionY),
      w: width,
      h: height,
    };
  }
  const base = computeBaseScale(crop.w, crop.h, canvasWidth, canvasHeight, transform.fitMode);
  const width = crop.w * base * transform.scale;
  const height = crop.h * base * transform.scale;
  return {
    x: (canvasWidth - width) / 2 + transform.positionX,
    y: (canvasHeight - height) / 2 + transform.positionY,
    w: width,
    h: height,
  };
}

const round6 = (value: number): number => Math.round(value * 1e6) / 1e6;

// 线上 wire 格式（camelCase），与后端 to_payload() 一致，用于保存/导出请求。
export function transformPayload(transform: ClipTransform): Record<string, unknown> {
  return {
    scale: round6(transform.scale),
    positionX: round6(transform.positionX),
    positionY: round6(transform.positionY),
    crop: {
      left: round6(transform.crop.left),
      top: round6(transform.crop.top),
      right: round6(transform.crop.right),
      bottom: round6(transform.crop.bottom),
    },
    fitMode: transform.fitMode,
    rotation: transform.rotation,
  };
}
