import { describe, expect, it } from "vitest";
import {
  adaptCardToClip,
  cardCoverCanUndo,
  coverPathOf,
  coverVersionLabel,
  dedupeByClipId,
  tagVersion,
  videoPathFromCover,
  workflowVersionLine,
  type ClipCardLike,
} from "./studio-clip-cards";

/**
 * 回归测试：左侧「剪辑成片」必须按稳定 clip_id 渲染。
 *
 * 改造前列表按 `${task_id}:${clip_id}` 聚合，每次「重新制作封面」都会新建一个
 * autoedit_xxxx 任务，于是同一个 clip_id 会随任务数长出多张重复卡片。这里的用例把
 * 「局部任务原地刷新后仍然只有一张卡」固定下来。
 */
describe("dedupeByClipId（一条 clip 一张卡）", () => {
  it("同一 clip_id 的后续版本覆盖原条目，卡片数不增长", () => {
    const coverV1 = { clip_id: "clip-741ef5-01", task_id: "autoedit_a", name: "剪辑成片 01", render_revision: 1 };
    const coverV2 = { clip_id: "clip-741ef5-01", task_id: "autoedit_a", name: "剪辑成片 01", render_revision: 2 };
    const merged = dedupeByClipId([coverV1, coverV2]);
    expect(merged).toHaveLength(1);
    expect(merged[0]?.render_revision).toBe(2);
  });

  it("即使来自不同自动剪辑任务（旧行为会新增卡片）也只保留一条", () => {
    const first = { clip_id: "clip-741ef5-01", task_id: "autoedit_a" };
    const duplicateFromNewTask = { clip_id: "clip-741ef5-01", task_id: "autoedit_b" };
    const other = { clip_id: "clip-741ef5-02", task_id: "autoedit_b" };
    const merged = dedupeByClipId([first, duplicateFromNewTask, other]);
    expect(merged.map((item) => item.clip_id)).toEqual(["clip-741ef5-01", "clip-741ef5-02"]);
    expect(merged[0]?.task_id).toBe("autoedit_b");
  });

  it("保留首次出现的顺序，并丢弃没有 clip_id 的脏数据", () => {
    const merged = dedupeByClipId([
      { clip_id: "clip-b" },
      { clip_id: "" },
      { clip_id: "clip-a" },
      { clip_id: "clip-b" },
    ]);
    expect(merged.map((item) => item.clip_id)).toEqual(["clip-b", "clip-a"]);
  });
});

const baseCard: ClipCardLike = {
  clip_id: "clip-741ef5-01",
  name: "剪辑成片 01",
  title: "高血压三件事",
  topic: "高血压科普",
  duration: 16.96,
  source_start: 0.14,
  source_end: 16.3,
  task_id: "autoedit_064d806d3218",
  render_task_id: "autoedit_064d806d3218",
  cover: {
    cover_asset_id: "cover-clip-741ef5-01-v1",
    revision: 1,
    active_revision: 1,
    path: "output/autoedit_064d806d3218/clip-741ef5-01/cover.jpg",
  },
};

describe("卡片 → 素材项换算", () => {
  it("从封面路径反推同目录的 clip_final.mp4，并保留本地转写分段与画面变换", () => {
    const adapted = adaptCardToClip(baseCard, {
      video_path: "",
      segments: [{ start: 0, end: 1, text: "本地转写" }],
      transform: { scale: 1.2 },
      cover_path: null,
    });
    expect(adapted.video_path).toBe("output/autoedit_064d806d3218/clip-741ef5-01/clip_final.mp4");
    expect(adapted.segments).toHaveLength(1);
    expect(adapted.transform).toEqual({ scale: 1.2 });
    expect(adapted.task_id).toBe("autoedit_064d806d3218");
    expect(adapted.cover_asset_id).toBe("cover-clip-741ef5-01-v1");
  });

  it("已有 video_path 的旧条目不被覆盖（不能凭空造出不存在的文件路径）", () => {
    const adapted = adaptCardToClip({ ...baseCard, cover: null }, {
      video_path: "output/autoedit_legacy/clip-741ef5-01/clip_final.mp4",
      cover_path: "output/autoedit_legacy/clip-741ef5-01/cover.jpg",
    });
    expect(adapted.video_path).toBe("output/autoedit_legacy/clip-741ef5-01/clip_final.mp4");
    expect(adapted.cover_path).toBe("output/autoedit_legacy/clip-741ef5-01/cover.jpg");
  });

  it("卡片自带 video_path 必须最优先——无封面切片（cover=null）不再被推导成空路径", () => {
    // 回归 2026-10-16：reedit/新链卡片 cover=null，旧逻辑忽略 card.video_path 且封面
    // 反推得 null → video_path 为空 → 播放器请求 artifact?path= 404（无画面/播放不了）。
    const stagedPath = "output/192a7b28a47c41059fb147651593d480/clips/clip-741ef5-01/clip_final.mp4";
    const adapted = adaptCardToClip({ ...baseCard, cover: null, video_path: stagedPath }, null);
    expect(adapted.video_path).toBe(stagedPath);
  });

  it("videoPathFromCover 只处理带目录的路径", () => {
    expect(videoPathFromCover("cover.jpg")).toBeNull();
    expect(videoPathFromCover(null)).toBeNull();
    expect(videoPathFromCover("a/b/cover.jpg")).toBe("a/b/clip_final.mp4");
  });
});

describe("封面资产与版本徽标", () => {
  it("画布封面视图与导出封面都解析到卡片的 cover.path", () => {
    const clip = adaptCardToClip(baseCard);
    expect(coverPathOf(clip, baseCard)).toBe(baseCard.cover?.path);
    // 卡片还没刷新时回退到成片自身的 cover_path。
    expect(coverPathOf(clip, null)).toBe(baseCard.cover?.path);
    expect(coverPathOf({ cover_path: "x/y/cover.jpg" }, null)).toBe("x/y/cover.jpg");
    expect(coverPathOf({ cover_path: null }, null)).toBeNull();
  });

  it("版本徽标显示 v{active_revision}：撤销到 v1 后不再显示 v2", () => {
    expect(coverVersionLabel(adaptCardToClip({ ...baseCard, cover: { ...baseCard.cover, revision: 2, active_revision: 2 } }))).toBe("v2");
    expect(coverVersionLabel(adaptCardToClip({ ...baseCard, cover: { ...baseCard.cover, revision: 2, active_revision: 1 } }))).toBe("v1");
    expect(coverVersionLabel(adaptCardToClip({ ...baseCard, cover: { ...baseCard.cover, revision: 0, active_revision: 0 } }))).toBeNull();
  });

  it("只有多于一个封面版本时才提供「撤销到 v1」", () => {
    expect(cardCoverCanUndo(adaptCardToClip(baseCard))).toBe(false);
    expect(cardCoverCanUndo(adaptCardToClip({ ...baseCard, cover: { ...baseCard.cover, revision: 2, active_revision: 2 } }))).toBe(true);
  });
});

/**
 * 规则版本可见性（Phase B）：卡片要能把台账里的 workflow_version / skill_versions
 * 原样透出，并在成片详情显示一行中文版本说明；旧成片没有快照时明确显示「未知」。
 */
describe("规则版本透传与展示行", () => {
  it("adaptCardToClip 透传 workflow_version 与 skill_versions", () => {
    const adapted = adaptCardToClip({
      ...baseCard,
      workflow_version: "2026-10-12.1",
      skill_versions: { clip_planning: "v1", cover: "v1", render: "v1" },
    });
    expect(adapted.workflow_version).toBe("2026-10-12.1");
    expect(adapted.skill_versions).toEqual({ clip_planning: "v1", cover: "v1", render: "v1" });
  });

  it("卡片没有版本字段时保留本地已有值（不把版本抹成 null）", () => {
    const adapted = adaptCardToClip(baseCard, {
      workflow_version: "2026-10-12.1",
      skill_versions: { cover: "v1" },
    });
    expect(adapted.workflow_version).toBe("2026-10-12.1");
    expect(adapted.skill_versions).toEqual({ cover: "v1" });
  });

  it("版本行：workflow + 各 skill，版本号统一带 v 前缀且不重复叠加", () => {
    expect(tagVersion("1")).toBe("v1");
    expect(tagVersion("v1")).toBe("v1");
    expect(tagVersion(null)).toBe("vunknown");
    expect(workflowVersionLine({
      workflow_version: "2026-10-12.1",
      skill_versions: { clip_planning: "1", cover: "v2" },
    })).toBe("使用规则版本：workflow v2026-10-12.1 / clip_planning v1 / cover v2");
  });

  it("legacy_default 是最低优先级默认层，不进展示行", () => {
    expect(workflowVersionLine({
      workflow_version: "2026-10-12.1",
      skill_versions: { legacy_default: "v3", cover: "v1" },
    })).toBe("使用规则版本：workflow v2026-10-12.1 / cover v1");
  });

  it("旧成片（Phase B 之前没有快照）显示未知，绝不编造版本号", () => {
    expect(workflowVersionLine({ workflow_version: null, skill_versions: null }))
      .toBe("使用规则版本：未知（无规则快照）");
    expect(workflowVersionLine({})).toBe("使用规则版本：未知（无规则快照）");
  });
});

/**
 * 回归：刷新后只凭会话里的 projectId 恢复时，台账卡片没有 previous 可继承转写分段。
 * 若这里是 undefined，选中成片会把 undefined 灌进页面状态，随后 `segments.length` 崩溃
 * （2026-10-12 浏览器实测到该崩溃）。
 */
describe("segments 兜底（选中成片不能崩）", () => {
  it("没有本地条目时给空数组，而不是 undefined", () => {
    const adapted = adaptCardToClip(baseCard);
    expect(Array.isArray(adapted.segments)).toBe(true);
    expect(adapted.segments).toEqual([]);
  });

  it("有本地条目时保留已加载的转写分段", () => {
    const segments = [{ start: 0, end: 1, text: "本地转写" }];
    expect(adaptCardToClip(baseCard, { segments }).segments).toEqual(segments);
  });
});
