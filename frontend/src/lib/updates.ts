/**
 * 更新日志数据模型与过滤/分组纯函数。
 *
 * 设计要点（见 docs/updates-page.md 的取舍）：
 * - 单一数据源、两种视图：`user` 视图 = 公开 + 已完成条目；`dev` 视图 = 全部（含内部）。
 * - "工作台"是模块不是类型：类型保持互斥（feature/improvement/fix/experiment），
 *   视觉强化用 `highlight` 标记表达。
 * - 状态用枚举存储（done/wip/experimental），emoji 只在渲染层映射，不进数据。
 * - 所有函数都是纯函数，`now` 可注入，方便测试。
 */

export type UpdateType = "feature" | "improvement" | "fix" | "experiment";
export type UpdateStatus = "done" | "wip" | "experimental";
export type UpdateVisibility = "public" | "internal";
export type UpdateView = "user" | "dev";
export type UpdateRange = "week" | "month" | "all";

export interface UpdateLink {
  label: string;
  href: string;
}

export interface UpdateImage {
  src: string;
  alt: string;
}

/** 前后对比图：工作台交互类改动的首选展示形式。 */
export interface UpdateCompare {
  before: UpdateImage;
  after: UpdateImage;
}

export interface UpdateEntry {
  id: string;
  /** YYYY-MM-DD */
  date: string;
  type: UpdateType;
  status: UpdateStatus;
  /** 用「用户能做什么」来写，如「一键生成 3 种封面风格」。 */
  title: string;
  /** 30 字以内，扫读用。 */
  summary: string;
  /** 模块 id 列表，取值见 UPDATE_MODULES。 */
  modules: string[];
  /** 工作台核心改动置 true：卡片左侧高亮边框 + 图标。 */
  highlight?: boolean;
  /** 分组标题显示 v 前缀版本号；没有版本节奏时留空。 */
  version?: string;
  image?: UpdateImage;
  compare?: UpdateCompare;
  /** 2–4 句：怎么用、解决了什么问题、有什么限制。 */
  details?: string;
  /** 适用入口，如「工作台 → 右侧面板 → 封面」。新功能必填。 */
  entry?: string;
  links?: UpdateLink[];
  /** 实现要点 / 重构说明。仅 dev 视图展示。 */
  techDetails?: string;
}

/** 打了可见性标记的条目（合并公开与内部数据后的运行时形态）。 */
export interface TaggedUpdateEntry extends UpdateEntry {
  visibility: UpdateVisibility;
}

export const UPDATE_TYPES: ReadonlyArray<{ id: UpdateType; label: string }> = [
  { id: "feature", label: "新功能" },
  { id: "improvement", label: "优化" },
  { id: "fix", label: "修复" },
  { id: "experiment", label: "实验" },
];

export const UPDATE_MODULES: ReadonlyArray<{ id: string; label: string }> = [
  { id: "workbench", label: "工作台" },
  { id: "cover", label: "封面" },
  { id: "timeline", label: "时间轴" },
  { id: "export", label: "导出" },
  { id: "subtitle", label: "字幕" },
  { id: "assets", label: "素材" },
  { id: "account", label: "账号" },
];

export const UPDATE_RANGES: ReadonlyArray<{ id: UpdateRange; label: string }> = [
  { id: "week", label: "本周" },
  { id: "month", label: "本月" },
  { id: "all", label: "全部" },
];

export const STATUS_GLYPHS: Record<UpdateStatus, string> = {
  done: "✅",
  wip: "🔧",
  experimental: "🧪",
};

export const STATUS_LABELS: Record<UpdateStatus, string> = {
  done: "已完成",
  wip: "进行中",
  experimental: "实验性",
};

export interface UpdateFilter {
  view: UpdateView;
  type: UpdateType | "all";
  module: string | "all";
  range: UpdateRange;
  q: string;
  /** 仅 dev 视图生效。 */
  visibility: UpdateVisibility | "all";
}

export const DEFAULT_UPDATE_FILTER: UpdateFilter = {
  view: "user",
  type: "all",
  module: "all",
  range: "all",
  q: "",
  visibility: "all",
};

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"] as const;

function oneOf<T extends string>(value: string | null, allowed: ReadonlyArray<T>, fallback: T): T {
  return allowed.includes(value as T) ? (value as T) : fallback;
}

/** 从 URL 查询参数解析筛选状态；非法值回退默认。 */
export function parseUpdateFilter(params: URLSearchParams | null): UpdateFilter {
  if (!params) return { ...DEFAULT_UPDATE_FILTER };
  const typeIds = UPDATE_TYPES.map((t) => t.id);
  const rangeIds = UPDATE_RANGES.map((r) => r.id);
  const view = oneOf(params.get("view"), ["user", "dev"], "user");
  return {
    view,
    type: oneOf(params.get("type"), ["all", ...typeIds], "all"),
    module: oneOf(params.get("module"), ["all", ...UPDATE_MODULES.map((m) => m.id)], "all"),
    range: oneOf(params.get("range"), rangeIds, "all"),
    q: (params.get("q") ?? "").slice(0, 100),
    visibility: view === "dev" ? oneOf(params.get("visibility"), ["all", "public", "internal"], "all") : "all",
  };
}

/** 筛选状态 → 查询参数对象（与默认值相同的键被省略，保持 URL 干净、可分享）。 */
export function updateFilterToParams(filter: UpdateFilter): Record<string, string> {
  const params: Record<string, string> = {};
  if (filter.view !== DEFAULT_UPDATE_FILTER.view) params.view = filter.view;
  if (filter.type !== "all") params.type = filter.type;
  if (filter.module !== "all") params.module = filter.module;
  if (filter.range !== "all") params.range = filter.range;
  if (filter.q) params.q = filter.q;
  if (filter.view === "dev" && filter.visibility !== "all") params.visibility = filter.visibility;
  return params;
}

/** date(YYYY-MM-DD) 是否落在时间范围内。week=最近 7 天，month=最近 31 天。 */
export function isWithinUpdateRange(date: string, range: UpdateRange, now: Date = new Date()): boolean {
  if (range === "all") return true;
  const days = range === "week" ? 7 : 31;
  const entry = new Date(`${date}T23:59:59`);
  if (Number.isNaN(entry.getTime())) return false;
  return now.getTime() - entry.getTime() < days * 24 * 60 * 60 * 1000;
}

/** 核心过滤规则。user 视图 = 公开 + 已完成；dev 视图 = 全部，可再按可见性收窄。 */
export function filterUpdateEntries(
  entries: TaggedUpdateEntry[],
  filter: UpdateFilter,
  now: Date = new Date(),
): TaggedUpdateEntry[] {
  const q = filter.q.trim().toLowerCase();
  return entries.filter((entry) => {
    if (filter.view === "user") {
      if (entry.visibility !== "public" || entry.status !== "done") return false;
    } else if (filter.visibility !== "all" && entry.visibility !== filter.visibility) {
      return false;
    }
    if (filter.type !== "all" && entry.type !== filter.type) return false;
    if (filter.module !== "all" && !entry.modules.includes(filter.module)) return false;
    if (!isWithinUpdateRange(entry.date, filter.range, now)) return false;
    if (q) {
      // 模块同时索引 id 与中文标签：用户搜「导出」能命中 module=export 的条目。
      const haystack = [
        entry.title,
        entry.summary,
        entry.details ?? "",
        ...entry.modules.flatMap((id) => [id, moduleLabel(id)]),
        entry.id,
      ]
        .join(" ")
        .toLowerCase();
      if (!haystack.includes(q)) return false;
    }
    return true;
  });
}

export interface UpdateDayGroup {
  date: string;
  /** 当天任一条目带版本号时取第一个非空值，分组标题渲染为「v1.4.0 · 2026-10-27」。 */
  version?: string;
  entries: TaggedUpdateEntry[];
}

/** 按日期倒序分组；组内保持数据文件里的编辑顺序（首条即当天最重要的变化）。 */
export function groupUpdatesByDay(entries: TaggedUpdateEntry[]): UpdateDayGroup[] {
  const byDate = new Map<string, TaggedUpdateEntry[]>();
  for (const entry of entries) {
    const list = byDate.get(entry.date);
    if (list) list.push(entry);
    else byDate.set(entry.date, [entry]);
  }
  return [...byDate.entries()]
    .sort(([a], [b]) => (a < b ? 1 : a > b ? -1 : 0))
    .map(([date, list]) => ({
      date,
      version: list.find((e) => e.version)?.version,
      entries: list,
    }));
}

/** 顶部概览的「最近最重要的变化」：最新一天里第一条公开且完成的高亮/新功能。 */
export function updateHeadline(entries: TaggedUpdateEntry[]): TaggedUpdateEntry | null {
  const publicDone = entries
    .filter((e) => e.visibility === "public" && e.status === "done")
    .sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));
  return publicDone.find((e) => e.highlight || e.type === "feature") ?? publicDone[0] ?? null;
}

/** 「2026-10-27 · 周四」。非法日期原样返回。 */
export function formatUpdateDayHeading(date: string): string {
  const parsed = new Date(`${date}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return date;
  const weekday = WEEKDAYS[parsed.getDay()];
  return `${date} · ${weekday}`;
}

export function moduleLabel(id: string): string {
  return UPDATE_MODULES.find((m) => m.id === id)?.label ?? id;
}

export function typeLabel(type: UpdateType): string {
  return UPDATE_TYPES.find((t) => t.id === type)?.label ?? type;
}
