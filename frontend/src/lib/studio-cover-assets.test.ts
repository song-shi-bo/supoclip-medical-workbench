/**
 * 独立封面素材纯逻辑测试（2026-10-20）。
 * 锁定：真实比例判定（fit 类名）、未知尺寸防拉伸、批次过滤（旧批次不出现）、批次汇总。
 */
import { describe, expect, it } from "vitest";
import {
  coverFitClass,
  filterCurrentBatch,
  summarizeCoverBatch,
  type CoverAssetItem,
} from "./studio-cover-assets";

const item = (clipId: string, status: "ready" | "failed" = "ready"): CoverAssetItem => ({
  project_id: "timeline_x", source_id: "src01", clip_id: clipId, clip_name: clipId,
  render_task_id: "task01", revision: 1, status,
});

describe("coverFitClass（比例只从媒体真实宽高判定）", () => {
  it("标准 9:16 素材用 object-fit: cover", () => {
    expect(coverFitClass(1080, 1920)).toBe("fit-cover");
    expect(coverFitClass(1086, 1920)).toBe("fit-cover"); // 取整差异 ±2% 内
    expect(coverFitClass(900, 1600)).toBe("fit-cover");
  });
  it("非标准历史素材用 contain（背景填充），绝不拉伸", () => {
    expect(coverFitClass(1920, 1080)).toBe("fit-contain"); // 16:9 横版
    expect(coverFitClass(1080, 1080)).toBe("fit-contain"); // 1:1
    expect(coverFitClass(750, 1920)).toBe("fit-contain");  // 更瘦的非标准比例
  });
  it("未知/非法尺寸一律 contain（宁可留边不可拉伸）", () => {
    expect(coverFitClass(0, 1920)).toBe("fit-contain");
    expect(coverFitClass(Number.NaN, 1920)).toBe("fit-contain");
    expect(coverFitClass(-1, 0)).toBe("fit-contain");
  });
});

describe("filterCurrentBatch（旧批次封面不出现——第二道闸）", () => {
  it("只保留当前台账 clip 集合内的项", () => {
    const items = [item("clip-new-1"), item("clip-old"), item("clip-new-2")];
    const current = new Set(["clip-new-1", "clip-new-2"]);
    expect(filterCurrentBatch(items, current).map((i) => i.clip_id))
      .toEqual(["clip-new-1", "clip-new-2"]);
  });
  it("空集合（项目切换中）返回空，不闪现任何旧封面", () => {
    expect(filterCurrentBatch([item("clip-old")], new Set())).toEqual([]);
  });
});

describe("summarizeCoverBatch", () => {
  it("就绪/失败计数", () => {
    expect(summarizeCoverBatch([item("a"), item("b", "failed"), item("c", "failed")]))
      .toEqual({ ready: 1, failed: 2 });
  });
});
