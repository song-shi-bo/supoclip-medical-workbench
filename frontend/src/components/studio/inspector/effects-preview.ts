// 成片风格微调 —— 预览/导出的参数对照（纯函数，可单测）。
//
// 设计红线（与后端 clip_transform 同源）：
//  * 亮度/对比度/饱和度**数值与 FFmpeg eq 完全一致**：CSS 的 brightness(1+b)、
//    contrast(c)、saturate(s) 直接使用同一组数字；
//  * 色温与命名滤镜（warm/cool/bw/vivid）以及锐化在 CSS 里只能用近似表达，
//    因此 UI 必须标注「颜色类预览为近似、导出以 FFmpeg 为准」；
//  * 几何类（裁剪/缩放/位置/画幅）不走 CSS filter，由既有 transform 预览与
//    FFmpeg crop/scale/overlay 严格同源。

import type { EffectPreset, Effects, EffectsPicture, PictureFilterName } from "./types";

export const NEUTRAL_PICTURE: EffectsPicture = {
  brightness: 0, contrast: 1, saturation: 1, sharpen: 0, temperature: 0, filter: "none",
};

function round(value: number, digits = 3): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

function num(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/** 画面调整是否为中性（不改变任何像素）——导出短路判定的前端镜像。 */
export function isNeutralPicture(picture: EffectsPicture | null | undefined): boolean {
  if (!picture) return true;
  const name = picture.filter ?? "none";
  return name === "none"
    && num(picture.brightness, 0) === 0
    && num(picture.contrast, 1) === 1
    && num(picture.saturation, 1) === 1
    && num(picture.sharpen, 0) === 0
    && num(picture.temperature, 0) === 0;
}

/**
 * CSS filter 近似链（只作用于播放器里的视频元素，不进入导出参数）。
 *
 * 亮度/对比度/饱和度与 FFmpeg eq 同参数；色温/命名滤镜/锐化为近似。
 */
export function effectsPreviewFilter(picture: EffectsPicture | null | undefined): string {
  if (!picture) return "none";
  const parts: string[] = [];
  const brightness = num(picture.brightness, 0);
  const contrast = num(picture.contrast, 1);
  const saturation = num(picture.saturation, 1);
  const temperature = num(picture.temperature, 0);
  const name: PictureFilterName = (picture.filter ?? "none") as PictureFilterName;
  // 与 FFmpeg eq=brightness=b 对应：CSS brightness(1 + b)。
  if (brightness !== 0) parts.push(`brightness(${round(Math.max(0, 1 + brightness))})`);
  if (contrast !== 1) parts.push(`contrast(${round(Math.max(0, contrast))})`);
  if (saturation !== 1) parts.push(`saturate(${round(Math.max(0, saturation))})`);
  // 色温：CSS 没有 colortemperature；暖色用 sepia 近似，冷色用 hue-rotate 近似。
  if (temperature > 0) parts.push(`sepia(${round(Math.min(0.6, temperature * 0.5))})`);
  else if (temperature < 0) parts.push(`hue-rotate(${round(temperature * 12)}deg)`);
  switch (name) {
    case "warm": parts.push("sepia(0.35)", "saturate(1.15)"); break;
    case "cool": parts.push("hue-rotate(-12deg)", "saturate(1.05)"); break;
    case "bw": parts.push("grayscale(1)"); break;
    case "vivid": parts.push("saturate(1.35)", "contrast(1.08)"); break;
    default: break;
  }
  return parts.length ? parts.join(" ") : "none";
}

/** 相对预设的手改项（与后端 hand_edited_effects_keys 同一套比较）。 */
export function handEditedKeys(
  preset: EffectPreset | null | undefined,
  effects: Pick<Effects, "picture" | "transition" | "audio"> | null | undefined,
): string[] {
  if (!preset || !effects) return [];
  const edited: string[] = [];
  (Object.keys(preset.picture) as (keyof EffectsPicture)[]).forEach((key) => {
    if (effects.picture?.[key] !== preset.picture[key]) edited.push(`picture.${String(key)}`);
  });
  (["enabled", "type", "duration_s"] as const).forEach((key) => {
    if (effects.transition?.[key] !== preset.transition[key]) edited.push(`transition.${key}`);
  });
  (["ducking", "ducking_db"] as const).forEach((key) => {
    if (effects.audio?.[key] !== preset.audio[key]) edited.push(`audio.${key}`);
  });
  return edited;
}

/** 选中预设后立即生效的本地参数组（后端返回后以其为准）。 */
export function presetBundle(preset: EffectPreset | null | undefined): {
  picture: EffectsPicture;
  transition: EffectPreset["transition"];
  audio: EffectPreset["audio"];
} | null {
  if (!preset) return null;
  return {
    picture: { ...preset.picture },
    transition: { ...preset.transition },
    audio: { ...preset.audio },
  };
}
