// 预览/导出同源数学对照测试：向量由 backend/src/clip_transform.py 生成
// （clip-transform.vectors.json），断言 TS 镜像与后端 FFmpeg 滤镜链用的
// 契约矩形/裁剪像素逐值一致 —— 这是「预览与导出构图完全一致」的守护测试。

import { describe, expect, it } from "vitest";

import {
  computeCropPixels,
  defaultTransform,
  isDefaultTransform,
  parseTransform,
  transformPayload,
  transformToCanvasRect,
  type CanvasRect,
  type ClipTransform,
  type PixelRect,
} from "./clip-transform";
import vectors from "./clip-transform.vectors.json";

type Vector = {
  name: string;
  sourceWidth: number;
  sourceHeight: number;
  input: Record<string, unknown> | null;
  parsed: ReturnType<typeof transformPayload>;
  crop: PixelRect;
  rect: CanvasRect;
};

describe("clip-transform 与后端契约同源", () => {
  it.each((vectors as Vector[]).map((vector) => [vector.name, vector] as const))(
    "%s：解析、裁剪像素与画布矩形逐值一致",
    (_name, vector) => {
      const parsed = parseTransform(vector.input);
      // 后端 payload 是 6 位小数舍入后的 wire 格式，前端解析结果应逐字段相等。
      const payload = transformPayload(parsed);
      expect(payload).toEqual(vector.parsed);

      const crop = computeCropPixels(parsed.crop, vector.sourceWidth, vector.sourceHeight);
      expect(crop).toEqual(vector.crop);

      const rect = transformToCanvasRect(vector.sourceWidth, vector.sourceHeight, parsed);
      expect(rect).toEqual(vector.rect);
    },
  );

  it("默认/重置变换与后端 reset 语义一致", () => {
    expect(isDefaultTransform(defaultTransform())).toBe(true);
    const edited = parseTransform({ scale: 2.5, positionX: 30, crop: { left: 0.1, right: 0.6 } });
    expect(isDefaultTransform(edited)).toBe(false);
    const reset: ClipTransform = defaultTransform();
    expect(transformPayload(reset)).toEqual({
      scale: 1,
      positionX: 0,
      positionY: 0,
      crop: { left: 0, top: 0, right: 1, bottom: 1 },
      fitMode: "cover",
      rotation: 0,
    });
  });

  it("非法输入显式报错，缺省字段补默认", () => {
    expect(() => parseTransform("scale=2")).toThrow();
    expect(() => parseTransform({ scale: Number.NaN })).toThrow();
    expect(() => parseTransform({ fitMode: "fill" })).toThrow();
    expect(() => parseTransform({ rotation: 90 })).toThrow();
    // 乱序 crop 被修复而不是报错
    expect(parseTransform({ crop: { left: 0.8, right: 0.2 } }).crop).toEqual({
      left: 0.2, top: 0, right: 0.8, bottom: 1,
    });
  });
});
