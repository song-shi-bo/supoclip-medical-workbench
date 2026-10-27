/**
 * 项目成片卡片的纯逻辑（可单测）。
 *
 * 背景：改造前左侧「剪辑成片」按**自动剪辑任务**聚合，任何「重新制作封面」都会新建一个
 * ``autoedit_xxxx`` 任务，于是列表里多出一张重复的「剪辑成片 01」卡片。现在卡片的事实源是
 * 项目台账（``GET /studio/projects/{id}/clips``）里稳定不变的 ``clip_id``：
 *
 * - ``dedupeByClipId`` 是「一条 clip 一张卡」的唯一入口，键**只**用 ``clip_id``；
 * - 台账卡片 → 播放器素材项（``adaptCardToClip``）只在封面/版本字段上覆盖旧值，
 *   转写分段与画面变换等本地状态由调用方保留。
 *
 * 这个模块刻意不依赖 React 与页面状态，回归测试见 ``studio-clip-cards.test.ts``。
 */

/** 台账卡片里本模块需要的字段（与 page.tsx 的 ProjectClipCard 结构兼容）。 */
export type ClipCardLike = {
  clip_id: string;
  name?: string | null;
  title?: string | null;
  topic?: string | null;
  duration?: number | null;
  source_start?: number | null;
  source_end?: number | null;
  task_id?: string | null;
  render_task_id?: string | null;
  /** 成片相对路径（后端 card_view 按真实落盘布局计算的唯一事实源；旧台账可能缺省）。 */
  video_path?: string | null;
  /** 规则版本（Phase B 接线）：台账 card_view 原样透出，旧成片为 null/{}。 */
  workflow_version?: string | null;
  skill_versions?: Record<string, string | null> | null;
  cover?: {
    cover_asset_id?: string | null;
    revision?: number | null;
    active_revision?: number | null;
    path?: string | null;
    kind?: string | null;
    style_id?: string | null;
    can_undo?: boolean | null;
  } | null;
};

/** 播放器/导出需要的成片素材项（page.tsx 的 RenderedClip 子集）。 */
export type ClipLike = {
  task_id: string;
  clip_id: string;
  name: string;
  topic: string;
  source_start: number;
  source_end: number;
  duration: number;
  video_path: string;
  cover_path?: string | null;
  cover_asset_id?: string | null;
  cover_revision?: number | null;
  cover_active_revision?: number | null;
  workflow_version?: string | null;
  skill_versions?: Record<string, string | null> | null;
  // 转写分段：台账卡片本身没有，只能从既有本地条目继承；缺省给 [] 而不是 undefined
  // （选中成片时会把该字段灌进页面状态，undefined 会让 `segments.length` 直接崩溃）。
  segments?: unknown[] | null;
};

/**
 * 左侧「剪辑成片」列表的去重入口：严格按 ``clip_id`` 作键，后写入的同 id 条目覆盖先写入的
 * （等价于「原地刷新」），顺序沿用首次出现的顺序。改造前按 ``${task_id}:${clip_id}`` 作键，
 * 正是重复卡片与「卡片数随局部任务增长」的来源。
 */
export function dedupeByClipId<T extends { clip_id: string }>(items: T[]): T[] {
  const order: string[] = [];
  const byClipId = new Map<string, T>();
  items.forEach((item) => {
    const clipId = String(item.clip_id || "");
    if (!clipId) return;
    if (!byClipId.has(clipId)) order.push(clipId);
    byClipId.set(clipId, item);
  });
  return order.map((clipId) => byClipId.get(clipId)).filter((item): item is T => Boolean(item));
}

/**
 * 台账卡片只给了封面路径；成片视频固定是同一 clip 目录下的 ``clip_final.mp4``
 * （后端 ``studio_local_tasks._clip_paths`` 与 ``studio_auto_edit_bridge._read_rendered_clips``
 * 使用同一布局）。卡片里没有 video_path 字段，只能这样反推。
 */
export function videoPathFromCover(coverPath: string | null | undefined): string | null {
  if (!coverPath) return null;
  const slash = coverPath.lastIndexOf("/");
  if (slash < 0) return null;
  return `${coverPath.slice(0, slash)}/clip_final.mp4`;
}

/**
 * 素材卡缩略图地址 = 切片视频真实第一帧（2026-10-21 阶段契约）。
 *
 * 后端 ``GET /studio/projects/{id}/clips/{clip_id}/first-frame.jpg`` 直接从
 * ``clip_final.mp4`` 抽第 0 帧（磁盘缓存、重渲自动失效）——**不是**生成封面：
 * 卡片不再用封面冒充缩略图。项目必须是 ``timeline_*``（路径参数与后端校验一致），
 * 否则返回 null（调用方回退占位图标，绝不猜一个 URL 出去）。
 */
export function clipFirstFrameUrl(projectId: string | null | undefined, clipId: string): string | null {
  const pid = String(projectId || "");
  const cid = String(clipId || "").trim();
  if (!pid.startsWith("timeline_") || !cid) return null;
  return `/video-api/studio/projects/${encodeURIComponent(pid)}/clips/${encodeURIComponent(cid)}/first-frame.jpg`;
}

/**
 * 成片自身的封面路径（回退链路用）。
 */
export function clipCoverPath(clip: Pick<ClipLike, "cover_path">): string | null {
  return clip.cover_path || videoPathFromCover(clip.cover_path);
}

/**
 * 封面资产路径的解析优先级：项目台账卡片（唯一事实源）→ 回退到成片自身的 cover_path。
 * 画布封面视图与「导出封面」都必须走这个函数，重新生成/撤销后才会指向新资产。
 */
export function coverPathOf(clip: Pick<ClipLike, "cover_path">, card: ClipCardLike | null | undefined): string | null {
  return card?.cover?.path || clipCoverPath(clip);
}

/** 卡片封面版本徽标：revision > 0 时显示 v{active_revision}（撤销后可能小于 revision）。 */
export function coverVersionLabel(clip: Pick<ClipLike, "cover_revision" | "cover_active_revision">): string | null {
  const revision = clip.cover_revision ?? 0;
  if (revision <= 0) return null;
  return `v${clip.cover_active_revision || revision}`;
}

/** 是否可「撤销到上一版」：台账历史多于一个版本（或卡片显式标记 can_undo）。 */
export function cardCoverCanUndo(clip: Pick<ClipLike, "cover_revision" | "cover_active_revision">): boolean {
  return (clip.cover_revision ?? 0) > 1;
}

/**
 * 版本号统一带 v 前缀，且不重复叠加（清单里写 v1 或 1 都得到 v1）。
 * 与后端 ``skill_registry._tagged`` 同一规则，保证前后端展示一致。
 */
export function tagVersion(version: string | null | undefined): string {
  const text = String(version ?? "").trim() || "unknown";
  return text.startsWith("v") ? text : `v${text}`;
}

/**
 * 成片详情/卡片用的中文版本行（与后端 ``skill_registry.workflow_version_line`` 对齐）：
 *
 * - ``使用规则版本：workflow v2026-10-12.1 / cover v1 / clip_planning v1``
 * - 旧成片（Phase B 之前没有规则快照）→ ``使用规则版本：未知（无规则快照）``，
 *   绝不编造一个看起来正常的版本号。
 *
 * ``legacy_default`` 是最低优先级的系统默认层，不进展示行。
 */
export function workflowVersionLine(
  clip: Pick<ClipLike, "workflow_version" | "skill_versions">,
): string {
  const workflow = clip.workflow_version;
  const skills = clip.skill_versions || {};
  const skillIds = Object.keys(skills).filter((id) => id && id !== "legacy_default");
  if (!workflow && skillIds.length === 0) return "使用规则版本：未知（无规则快照）";
  const parts = [`workflow ${tagVersion(workflow)}`];
  skillIds.forEach((id) => parts.push(`${id} ${tagVersion(skills[id])}`));
  return `使用规则版本：${parts.join(" / ")}`;
}

/**
 * 台账卡片 → 播放器素材项。台账不含转写分段与画面变换，因此只在旧条目存在时继承它们，
 * 其余字段（封面资产、版本号、时长/边界/名称）都以台账为准。
 */
export function adaptCardToClip<T extends Partial<ClipLike>>(card: ClipCardLike, previous?: T | null): ClipLike & T {
  const previousCover = previous?.cover_path ?? null;
  const coverPath = card.cover?.path ?? previousCover;
  const derivedVideo = videoPathFromCover(coverPath);
  return {
    ...(previous || {}),
    task_id: card.render_task_id || card.task_id || previous?.task_id || "",
    clip_id: card.clip_id,
    name: card.name || previous?.name || card.clip_id,
    topic: card.topic || card.title || previous?.topic || "",
    source_start: card.source_start ?? previous?.source_start ?? 0,
    source_end: card.source_end ?? previous?.source_end ?? 0,
    duration: card.duration || previous?.duration || 0,
    // 2026-10-16 修复：卡片自带的 video_path 是后端按真实落盘布局算出的唯一事实源，
    // 必须最优先（旧注释「卡片里没有 video_path 字段」已过时）。封面反推只做兜底——
    // 无封面切片（cover=null，如 reedit 新链）此前会被推导成空路径 → 播放器 400 无画面。
    video_path: card.video_path || previous?.video_path || derivedVideo || "",
    cover_path: coverPath,
    cover_asset_id: card.cover?.cover_asset_id ?? null,
    cover_revision: card.cover?.revision ?? 0,
    cover_active_revision: card.cover?.active_revision ?? 0,
    // 规则版本原样透传：卡片/成片详情据此显示「使用规则版本：workflow vX / cover vX …」。
    workflow_version: card.workflow_version ?? previous?.workflow_version ?? null,
    skill_versions: card.skill_versions ?? previous?.skill_versions ?? {},
    // 台账卡片没有转写分段：只继承既有本地条目，绝不留下 undefined（见 ClipLike.segments 注释）。
    segments: previous?.segments ?? [],
  } as ClipLike & T;
}

// ===== 播放健壮性守卫（C 线，2026-10-16）=====
// 「非法视频源绝不能进播放器」的契约集中到这组纯函数，供 page.tsx 的
// fetchStagedClips / selectRenderedClip / video error 处理复用；
// 单测见 src/lib/video-playback-guard.test.ts。

/** 台账卡片/成片缺少可播放视频文件时的用户提示（selectRenderedClip 守卫）。 */
export const CLIP_VIDEO_MISSING_HINT = "该成片缺少可播放的视频文件（未发布或已清理）";

/** video 元素加载失败（error 事件）时的用户提示（含建议动作）。 */
export const VIDEO_LOAD_FAILED_HINT = "视频加载失败：源文件可能已被清理，请尝试重新剪辑或打开历史项目";

/**
 * video_path 是否可直接进播放器：非空且以 .mp4 结尾（忽略大小写，允许带查询串）。
 * 台账/历史项目里存在 video_path 缺省（未发布）、已被清理或误存 JSON 路径的条目，
 * 这类地址一旦 setPreviewUrl 会让 <video> 请求到 404/JSON → 黑屏；调用方应改走占位提示。
 */
export function isPlayableVideoPath(path: string | null | undefined): boolean {
  const value = String(path ?? "").trim();
  return /\.mp4(\?|$)/i.test(value);
}

/**
 * 任务 artifacts → 播放器源过滤契约（fetchStagedClips 使用）：
 * kind 必须是 "clip"（后端登记口径；历史数据兼容 "clip_final"），且 url 以 .mp4 结尾
 * （忽略大小写、允许查询串）——分析/规划 JSON、封面图、空地址绝不允许进入播放器。
 */
export function isClipArtifact(kind: string | null | undefined, url: string | null | undefined): boolean {
  const artifactKind = String(kind ?? "");
  const artifactUrl = String(url ?? "");
  return (artifactKind === "clip" || artifactKind === "clip_final") && /\.mp4(\?|$)/i.test(artifactUrl);
}

/**
 * video error 事件只上报一次（防循环）：src 为空不算加载失败（未选素材态）；
 * 同一 src 已提示过就不再重复（重渲染 / load() 可能让 error 重复触发）。
 * 返回 true = 本次需要展示降级占位与提示。
 */
export function shouldSurfaceVideoError(seenSrc: string | null | undefined, src: string | null | undefined): boolean {
  const value = String(src ?? "");
  return Boolean(value) && String(seenSrc ?? "") !== value;
}
