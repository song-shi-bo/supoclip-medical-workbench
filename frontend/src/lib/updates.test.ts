/**
 * 更新日志数据模型与过滤/分组纯函数的回归契约。
 * 重点锁定：视图过滤规则（user=公开+完成）、URL 参数往返、时间范围边界、
 * 分组排序与「最新变化」的选择逻辑。
 */
import { describe, expect, it } from "vitest";
import {
  DEFAULT_UPDATE_FILTER,
  filterUpdateEntries,
  formatUpdateDayHeading,
  groupUpdatesByDay,
  isWithinUpdateRange,
  parseUpdateFilter,
  updateFilterToParams,
  updateHeadline,
  type TaggedUpdateEntry,
} from "./updates";

// 固定「今天」：2026-10-27（周二），保证时间范围断言不受真实时钟影响。
const NOW = new Date("2026-10-27T12:00:00");

const entries: TaggedUpdateEntry[] = [
  {
    id: "cover-generator",
    date: "2026-10-27",
    type: "feature",
    status: "done",
    title: "封面生成器上线",
    summary: "选模板即时预览",
    modules: ["cover"],
    visibility: "public",
  },
  {
    id: "timeline-multitrack",
    date: "2026-10-25",
    type: "feature",
    status: "done",
    title: "时间轴多轨编辑",
    summary: "多轨拖拽与吸附",
    modules: ["workbench", "timeline"],
    highlight: true,
    visibility: "public",
  },
  {
    id: "export-queue-tuning",
    date: "2026-10-20",
    type: "improvement",
    status: "done",
    title: "导出并发提升",
    summary: "等待时间减半",
    modules: ["export"],
    version: "v0.9.2",
    visibility: "public",
  },
  {
    id: "public-wip-entry",
    date: "2026-10-19",
    type: "improvement",
    status: "wip",
    title: "尚未完成的公开条目",
    summary: "进行中",
    modules: ["workbench"],
    visibility: "public",
  },
  {
    id: "old-fix",
    date: "2026-08-15",
    type: "fix",
    status: "done",
    title: "修复旧问题",
    summary: "早已发布",
    modules: ["timeline"],
    visibility: "public",
  },
  {
    id: "internal-jobs-split",
    date: "2026-10-27",
    type: "improvement",
    status: "done",
    title: "任务管线拆分",
    summary: "jobs 与 worker 解耦",
    modules: ["export"],
    techDetails: "内部实现说明",
    visibility: "internal",
  },
  {
    id: "internal-experiment",
    date: "2026-10-19",
    type: "experiment",
    status: "experimental",
    title: "字幕自动对齐实验",
    summary: "效果不稳定",
    modules: ["subtitle"],
    visibility: "internal",
  },
];

describe("parseUpdateFilter", () => {
  it("空参数回落默认值：user 视图 + 全部", () => {
    expect(parseUpdateFilter(null)).toEqual(DEFAULT_UPDATE_FILTER);
    expect(parseUpdateFilter(new URLSearchParams())).toEqual(DEFAULT_UPDATE_FILTER);
  });

  it("解析合法参数；user 视图忽略 visibility", () => {
    const filter = parseUpdateFilter(
      new URLSearchParams("view=dev&type=fix&module=timeline&range=week&q=封面&visibility=internal"),
    );
    expect(filter).toEqual({
      view: "dev",
      type: "fix",
      module: "timeline",
      range: "week",
      q: "封面",
      visibility: "internal",
    });
    const userView = parseUpdateFilter(new URLSearchParams("view=user&visibility=internal"));
    expect(userView.visibility).toBe("all");
  });

  it("非法值回落默认而不是抛错", () => {
    const filter = parseUpdateFilter(new URLSearchParams("view=x&type=hack&module=nope&range=year"));
    expect(filter.view).toBe("user");
    expect(filter.type).toBe("all");
    expect(filter.module).toBe("all");
    expect(filter.range).toBe("all");
  });
});

describe("updateFilterToParams", () => {
  it("默认值全部省略，输出可分享的干净 URL", () => {
    expect(updateFilterToParams(DEFAULT_UPDATE_FILTER)).toEqual({});
  });

  it("dev 视图输出全部非默认键", () => {
    const params = updateFilterToParams({
      view: "dev",
      type: "fix",
      module: "timeline",
      range: "week",
      q: "封面",
      visibility: "internal",
    });
    expect(params).toEqual({
      view: "dev",
      type: "fix",
      module: "timeline",
      range: "week",
      q: "封面",
      visibility: "internal",
    });
  });
});

describe("filterUpdateEntries", () => {
  it("user 视图只保留公开且已完成的条目", () => {
    const visible = filterUpdateEntries(entries, { ...DEFAULT_UPDATE_FILTER }, NOW);
    const ids = visible.map((e) => e.id);
    expect(ids).toContain("cover-generator");
    expect(ids).not.toContain("public-wip-entry"); // wip：公开也不能见
    expect(ids).not.toContain("internal-jobs-split"); // 内部条目
    expect(ids).not.toContain("internal-experiment");
  });

  it("dev 视图包含全部条目，visibility 过滤生效", () => {
    const dev = { ...DEFAULT_UPDATE_FILTER, view: "dev" as const };
    const all = filterUpdateEntries(entries, dev, NOW);
    expect(all.map((e) => e.id)).toContain("internal-experiment");

    const internalOnly = filterUpdateEntries(
      entries,
      { ...dev, visibility: "internal" as const },
      NOW,
    );
    expect(internalOnly.every((e) => e.visibility === "internal")).toBe(true);
  });

  it("type / module / 关键词过滤可组合", () => {
    const filter = {
      ...DEFAULT_UPDATE_FILTER,
      type: "feature" as const,
      module: "workbench",
    };
    expect(filterUpdateEntries(entries, filter, NOW).map((e) => e.id)).toEqual(["timeline-multitrack"]);

    const search = filterUpdateEntries(
      entries,
      { ...DEFAULT_UPDATE_FILTER, view: "dev" as const, q: "对齐" },
      NOW,
    );
    expect(search.map((e) => e.id)).toEqual(["internal-experiment"]); // 仅 dev 数据里有
    // user 视图下同一关键词只能命中公开条目
    expect(
      filterUpdateEntries(entries, { ...DEFAULT_UPDATE_FILTER, q: "导出" }, NOW).map((e) => e.id),
    ).toEqual(["export-queue-tuning"]);
    // dev 视图能同时命中公开与内部条目
    expect(
      filterUpdateEntries(entries, { ...DEFAULT_UPDATE_FILTER, view: "dev", q: "导出" }, NOW).map((e) => e.id),
    ).toEqual(["export-queue-tuning", "internal-jobs-split"]);
  });

  it("时间范围：本周 7 天 / 本月 31 天边界", () => {
    const week = filterUpdateEntries(entries, { ...DEFAULT_UPDATE_FILTER, range: "week" as const }, NOW);
    expect(week.map((e) => e.id)).not.toContain("old-fix");
    expect(week.map((e) => e.id)).toContain("export-queue-tuning"); // 10-20，7 天边界上

    const month = filterUpdateEntries(entries, { ...DEFAULT_UPDATE_FILTER, range: "month" as const }, NOW);
    expect(month.map((e) => e.id)).not.toContain("old-fix"); // 8 月的进不来
  });
});

describe("isWithinUpdateRange", () => {
  it("按「当天结束」计：最近 7 个自然日内的日期都算本周", () => {
    // NOW = 10-27 12:00；条目日期按当天 23:59:59 计入，
    // 因此 10-20（第 7 个自然日前）全天都在本周内，10-18 已出界。
    expect(isWithinUpdateRange("2026-10-21", "week", NOW)).toBe(true);
    expect(isWithinUpdateRange("2026-10-20", "week", NOW)).toBe(true);
    expect(isWithinUpdateRange("2026-10-18", "week", NOW)).toBe(false);
    expect(isWithinUpdateRange("bad-date", "week", NOW)).toBe(false);
  });
});

describe("groupUpdatesByDay", () => {
  it("按日期倒序分组，版本号取当天第一个非空值", () => {
    const groups = groupUpdatesByDay(entries);
    expect(groups.map((g) => g.date)).toEqual([
      "2026-10-27",
      "2026-10-25",
      "2026-10-20",
      "2026-10-19",
      "2026-08-15",
    ]);
    expect(groups.find((g) => g.date === "2026-10-20")?.version).toBe("v0.9.2");
    expect(groups.find((g) => g.date === "2026-10-27")?.entries.map((e) => e.id)).toEqual([
      "cover-generator",
      "internal-jobs-split",
    ]);
  });
});

describe("updateHeadline", () => {
  it("取最新一天里第一条公开+完成的高亮/新功能", () => {
    const headline = updateHeadline(entries);
    expect(headline?.id).toBe("cover-generator");
  });

  it("没有高亮/新功能时回落到最新公开条目", () => {
    const onlyFix = entries.filter((e) => e.id === "old-fix");
    expect(updateHeadline(onlyFix)?.id).toBe("old-fix");
    expect(updateHeadline([])).toBeNull();
  });
});

describe("formatUpdateDayHeading", () => {
  it("渲染「YYYY-MM-DD · 周X」", () => {
    expect(formatUpdateDayHeading("2026-10-27")).toBe("2026-10-27 · 周二");
    expect(formatUpdateDayHeading("not-a-date")).toBe("not-a-date");
  });
});
