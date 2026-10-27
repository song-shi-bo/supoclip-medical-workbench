import { describe, expect, it } from "vitest";

import {
  CLIP_VIDEO_MISSING_HINT,
  VIDEO_LOAD_FAILED_HINT,
  adaptCardToClip,
  isClipArtifact,
  isPlayableVideoPath,
  shouldSurfaceVideoError,
} from "./studio-clip-cards";

/**
 * 播放健壮性守卫（C 线）契约测试：
 *
 *  * ``isPlayableVideoPath`` —— selectRenderedClip 的播放守卫：非法 video_path
 *    （空/JSON/非 mp4）绝不进播放器，卡片仍选中但走占位提示；
 *  * ``isClipArtifact`` —— fetchStagedClips 的 artifacts 过滤契约：只有成片本体
 *    （kind=clip/clip_final 且 .mp4）能当 video 源；
 *  * ``shouldSurfaceVideoError`` —— video error 事件只上报一次（防循环）。
 *
 * 场景背景：台账/历史项目里存在 video_path 缺省（未发布）、被清理、误存 JSON 的条目；
 * 任务 artifacts 里混有 content_analysis / clip_plan 等 JSON 与 cover 封面，这些一旦
 * 进入 <video src> 就是 404/JSON 响应 → 黑屏坏控件。
 */
describe("isPlayableVideoPath（video_path 播放守卫）", () => {
  it("空字符串/空白字符串不可播放", () => {
    expect(isPlayableVideoPath("")).toBe(false);
    expect(isPlayableVideoPath("   ")).toBe(false);
  });

  it("null/undefined（旧台账缺省 video_path）不可播放", () => {
    expect(isPlayableVideoPath(null)).toBe(false);
    expect(isPlayableVideoPath(undefined)).toBe(false);
  });

  it(".mp4 路径可播放", () => {
    expect(isPlayableVideoPath("/data/projects/timeline_x/clips/clip_01/clip_final.mp4")).toBe(true);
  });

  it(".MP4 大小写不敏感，且允许带查询串", () => {
    expect(isPlayableVideoPath("/data/clips/clip_01/clip_final.MP4")).toBe(true);
    expect(isPlayableVideoPath("/data/clips/clip_01/clip_final.MP4?raw=1")).toBe(true);
    expect(isPlayableVideoPath("/data/clips/clip_01/clip_final.mp4?raw=1")).toBe(true);
  });

  it("JSON 路径与封面等非 mp4 资产不可播放", () => {
    expect(isPlayableVideoPath("/data/clips/clip_01/clip_final.mp4.json")).toBe(false);
    expect(isPlayableVideoPath("/data/clips/clip_01/clip_plan.json")).toBe(false);
    expect(isPlayableVideoPath("/data/clips/clip_01/cover.jpg")).toBe(false);
  });
});

describe("isClipArtifact（任务 artifacts 过滤契约）", () => {
  it("kind=clip 且 .mp4 url → 进播放器", () => {
    expect(isClipArtifact("clip", "/files/clips/clip_01/clip_final.mp4")).toBe(true);
  });

  it("历史数据 kind=clip_final 且带查询串的 .mp4 → 进播放器", () => {
    expect(isClipArtifact("clip_final", "/files/clips/clip_01/clip_final.mp4?v=2")).toBe(true);
  });

  it("content_analysis / cover 等 JSON 与封面 artifact → 不进播放器", () => {
    expect(isClipArtifact("content_analysis", "/files/analysis.json")).toBe(false);
    expect(isClipArtifact("cover", "/files/clips/clip_01/cover.jpg")).toBe(false);
  });

  it("kind 不符时即使 url 是 .mp4 也过滤掉", () => {
    expect(isClipArtifact("clip_plan", "/files/clips/clip_01/clip_final.mp4")).toBe(false);
    expect(isClipArtifact(undefined, "/files/clips/clip_01/clip_final.mp4")).toBe(false);
  });

  it("url 缺失/非 mp4 时过滤掉（不挑 kind）", () => {
    expect(isClipArtifact("clip", "")).toBe(false);
    expect(isClipArtifact("clip", undefined)).toBe(false);
    expect(isClipArtifact("clip", "/files/clip_plan.json")).toBe(false);
    expect(isClipArtifact("clip_final", "/files/clips/clip_01/cover.jpg")).toBe(false);
  });
});

describe("shouldSurfaceVideoError（error 事件只上报一次）", () => {
  it("首次失败的 src 需要上报", () => {
    expect(shouldSurfaceVideoError(null, "/a/clip_final.mp4")).toBe(true);
    expect(shouldSurfaceVideoError(undefined, "/a/clip_final.mp4")).toBe(true);
    expect(shouldSurfaceVideoError("", "/a/clip_final.mp4")).toBe(true);
  });

  it("同一 src 已上报过 → 不重复上报（防重渲染/load() 触发循环）", () => {
    expect(shouldSurfaceVideoError("/a/clip_final.mp4", "/a/clip_final.mp4")).toBe(false);
  });

  it("换成新 src → 允许再次上报", () => {
    expect(shouldSurfaceVideoError("/a/clip_01.mp4", "/a/clip_02.mp4")).toBe(true);
  });

  it("src 为空不算加载失败（未选素材态，video 重置会调 load()）", () => {
    expect(shouldSurfaceVideoError(null, "")).toBe(false);
    expect(shouldSurfaceVideoError(null, undefined)).toBe(false);
    expect(shouldSurfaceVideoError("/a/clip_01.mp4", "")).toBe(false);
  });
});

describe("守卫文案与卡片适配的组合契约", () => {
  it("文案常量非空且与 page.tsx 展示保持一致", () => {
    expect(CLIP_VIDEO_MISSING_HINT).toBe("该成片缺少可播放的视频文件（未发布或已清理）");
    expect(VIDEO_LOAD_FAILED_HINT).toBe("视频加载失败：源文件可能已被清理，请尝试重新剪辑或打开历史项目");
  });

  it("无封面且无 video_path 的卡片（reedit 新链）adapt 出空路径 → 守卫判为不可播放", () => {
    const clip = adaptCardToClip({ clip_id: "clip-reedit-01", cover: null });
    expect(clip.video_path).toBe("");
    expect(isPlayableVideoPath(clip.video_path)).toBe(false);
  });

  it("封面反推出的 clip_final.mp4 → 守卫判为可播放", () => {
    const clip = adaptCardToClip({
      clip_id: "clip-741ef5-01",
      cover: { path: "/data/projects/timeline_x/clips/clip_01/cover_v1.png" },
    });
    expect(clip.video_path).toBe("/data/projects/timeline_x/clips/clip_01/clip_final.mp4");
    expect(isPlayableVideoPath(clip.video_path)).toBe(true);
  });
});
