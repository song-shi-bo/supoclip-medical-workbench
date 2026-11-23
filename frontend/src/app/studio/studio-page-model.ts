/**
 * Studio 页面的纯模型层（阶段5·点2 拆分，2026-11-23）。
 *
 * 从 page.tsx 原地外移：领域类型、封面风格常量、轮询常量与纯辅助函数。
 * 不包含任何 React 状态/副作用——page.tsx 的 StudioPage 组件行为保持逐行等价。
 */

import { adaptCardToClip } from "@/lib/studio-clip-cards";
import type { RevisionDocument } from "@/lib/studio-workflow";

export type SpeakerRole = "未标注" | "医生" | "患者";
// 拆分后转写 artifact 的分段自带纠错信息：raw_text 是原始转写文本，
// correction_status 标记该句是否被纠错（corrected/no_change/pending_review）。
export type Segment = { start: number; end: number; text: string; speaker?: SpeakerRole; raw_text?: string; correction_status?: "corrected" | "no_change" | "pending_review"; segment_id?: string; start_ms?: number; end_ms?: number; uncertain?: boolean; correction_reason?: string };
// 「开始转写」job 完成后的凭据：自动剪辑 job（独立任务）只靠它提交，不再依赖
// 前端内存里的时间线/金句。四个字段与 POST /api/jobs/auto-edit 的请求体一一对应。
export type TranscriptRef = {
  object_key: string;
  transcript_id: string;
  transcript_version: number;
  project_id: string;
};
export type TimelineRange = { id: string; source_start: number; source_end: number; transform?: Record<string, unknown> | null };
export type TimelineProject = { id: string; timeline: { ranges: TimelineRange[] }; artifacts: { edl: string; srt: string; fcpxml: string } };
export type GoldenSentence = { start: number; end: number; text: string; reason: string };
export type StudioInsights = { golden_sentences: GoldenSentence[]; keywords: string[]; engine: "llm" | "local-fallback"; notice?: string | null };
export type TranscriptCredential = { object_key: string; transcript_id: string; transcript_version: number; project_id: string };
export type RenderedClip = {
  task_id: string;
  clip_id: string;
  name: string;
  topic: string;
  source_start: number;
  source_end: number;
  duration: number;
  video_path: string;
  cover_path?: string | null;
  segments: Segment[];
  subtitle_url?: string | null;
  subtitle_notice?: string | null;
  subtitle_transcript_version?: number;
  subtitle_mode?: string;
  subtitle_provenance?: { transcript_id?: string; transcript_version?: number };
  video_artifact_verified?: boolean;
  // 已保存的画面变换（来自 bridge sidecar / 任务 JSON）；无则缺省（重置态）。
  transform?: Record<string, unknown> | null;
  // 项目台账卡片（GET /studio/projects/{id}/clips）随附的封面版本信息。
  // 旧 auto-edit 回退链路没有这个字段时，封面视图退回 cover_path。
  // 第五步 staged 渲染：条目级质量警告与 completeness（只展示，不拦截）。
  warnings?: string[];
  completeness?: string;
  cover_asset_id?: string | null;
  cover_revision?: number | null;
  cover_active_revision?: number | null;
  // 规则版本（台账卡片透传）：卡片与成片详情显示「使用规则版本：workflow vX / cover vX …」。
  workflow_version?: string | null;
  skill_versions?: Record<string, string | null> | null;
};
export type AutoEditTask = {
  task_id: string;
  status: "queued" | "running" | "completed" | "failed";
  stage: string;
  progress: number;
  error?: string;
  artifacts: { name: string; path: string; size_bytes: number }[];
  clips?: RenderedClip[];
};
export type SourceWorkspace = { projectId: string | null; name: string; credential: TranscriptRef | null; document: RevisionDocument | null; previewUrl: string; duration: number; segments: Segment[]; timeline: TimelineProject | null; insights: StudioInsights | null; previewRotation: 0 | 90 | -90 | 180; fallbackNotice?: boolean };
export type TimelinePointer = { clientX: number; currentTarget: HTMLDivElement };

// 封面风格：两版成员风格（与后端 cover_replace.SUPPORTED_COVER_STYLES 一致）。
export type CoverStyleId = "realframe_v2" | "cutout_v3";
export type CoverStyleOption = { id: CoverStyleId; name: string; tip: string };
export const COVER_STYLES: CoverStyleOption[] = [
  { id: "realframe_v2", name: "第二版·真实帧", tip: "真实帧大字：保留真实人物与诊室场景，仅轻微增强对比度，顶部白字主题 + 人物嘴下两行黄字钩子。无需额外依赖，永远可用。" },
  { id: "cutout_v3", name: "第三版·人物抠像", tip: "抠像大字：背景强虚化 + 人物白描边前景，缩略图更抢眼。依赖 u2net 抠像权重；本机未安装时会把 E_CUTOUT_WEIGHTS_MISSING 与安装指引原样返回，不会偷偷降级成第二版。" },
];
// 「沿用已生成的封面」结果 / 台账（GET .../cover）。
export type ClipCoverState = {
  task_id: string; clip_id: string; cover_path?: string | null; cover_url?: string | null;
  has_cover: boolean; has_baseline: boolean; can_restore: boolean;
  active?: string | null; active_kind?: string | null; active_style_id?: string | null;
  supported_styles?: string[]; default_style_id?: string;
};
// 封面通道（重制 / 沿用 / 回滚）返回体：含可读原因、QC 结论与来源说明。
export type CoverMutationResult = ClipCoverState & {
  kind: "generated" | "adopted" | "restore";
  style_id?: string | null; replace_intro: boolean; idempotent: boolean;
  cover_revision?: number | null;
  source?: { type: string; task_id?: string; clip_id?: string } | null;
  qc?: { passed?: boolean; failures?: string[]; checks?: { check: string; passed: boolean; detail: string }[] } | null;
  detail?: string;
};
export type ProjectCoverEntry = {
  clip_id: string; name?: string; topic?: string; cover_path?: string | null;
  has_cover: boolean; active?: string | null; active_kind?: string | null; cover_url?: string | null;
};

// ===== 项目级成片台账（GET /studio/projects/{project_id}/clips）=====
// 卡片的事实源是「项目 + 稳定 clip_id」，不是自动剪辑任务：一条 clip 永远只有
// 一张卡，封面/标题/字幕/时间线局部任务只原地刷新该卡，绝不新增卡片。
export type ClipCoverVersion = {
  cover_asset_id: string | null;
  revision: number;
  active_revision: number;
  style_id: string | null;
  path: string | null;
  sha256: string | null;
  bytes: number | null;
  kind: string | null;
  updated_at: string | null;
  intro_burned: boolean | null;
  can_undo: boolean;
  undo_revision: number | null;
  history: ClipCoverHistoryEntry[];
};
// 端点 6 的版本条目直接来自封面快照的 revision.json：revision/created_at/kind 一定有，
// style_id / cover_asset_id / instruction 只在部分版本里写入，按可选处理。
export type ClipCoverHistoryEntry = {
  revision: number; created_at: string | null; kind: string | null;
  style_id?: string | null; cover_asset_id?: string | null; sha256?: string | null;
  instruction?: string | null;
};
export type ProjectClipCard = {
  video_path?: string | null;
  segments?: Segment[];
  subtitle_url?: string | null;
  subtitle_transcript_version?: number;
  subtitle_mode?: string;
  subtitle_provenance?: { transcript_id?: string; transcript_version?: number };
  clip_id: string;
  name: string;
  title: string | null;
  topic: string | null;
  keywords: string[];
  duration: number;
  source_start: number;
  source_end: number;
  task_id: string;
  render_task_id: string;
  render_revision: number;
  status: string;
  cover: ClipCoverVersion;
  subtitle_style: unknown | null;
  timeline: unknown | null;
  // 任务历史（台账 card_view）：逐次局部/渲染任务，含规则溯源字段（后端 studio_local_tasks
  // 写入 task_history 的 workflow_version / skill_versions / workflow_snapshot_path / rules_source）。
  task_history: {
    task_id: string; task_type: string; status: string; stage: string; instruction: string | null;
    created_at: string; summary: string; error: string | null;
    workflow_version?: string | null; skill_versions?: Record<string, string | null> | null;
    workflow_snapshot_path?: string | null; rules_source?: Record<string, unknown> | null;
  }[];
  // 规则版本（Phase B 接线）：台账 card_view 原样透出；旧成片为 null/{}。
  workflow_version?: string | null;
  skill_versions?: Record<string, string | null> | null;
  workflow_snapshot_path?: string | null;
  // 封面版型选择（2026-10-21 阶段）：台账 card_view 透出的三元组；未选择为 null/缺省。
  cover_template?: { template_id: string; registry_version: string; updated_at: string } | null;
  // 版型封面工作流状态（草稿→应用/删除）：card_view 经 cover_template_state_for_card 透出。
  cover_workflow?: {
    has_cover: boolean; pending: boolean; applied: boolean;
    revision?: number | null; style?: string | null;
  } | null;
  // 时间线图片素材（真正的时间线拖拽）：card_view 原样透出 timeline_image_clips 实例。
  timeline_image_clips?: { id: string; role: string; start_s: number; duration_s: number; asset_path?: string | null }[] | null;
};
// active_clip_id（2026-10-21 阶段）：服务端记录的项目唯一选中切片，随卡片载荷透出；
// 前端点击时 PUT 持久化。2026-10-29 起打开项目一律原片视图，不再据此自动恢复成片。
export type ProjectClipsPayload = { project_id?: string; card_count?: number; cards?: ProjectClipCard[]; active_clip_id?: string | null; active_clip_updated_at?: string | null; detail?: string };
// ===== 规则中心（剪辑规则注册表 / Phase B）=====
// GET /studio/workflow 每次实时读清单：外部改 skill 文件或启停 skill 后，面板刷新即见新版本。
export type WorkflowSkillStatus = {
  skill_id: string; version: string; enabled: boolean;
  status: "ok" | "stale" | "missing" | "invalid" | "disabled" | string;
  detail?: string; paths?: string[];
};
export type WorkflowSummary = {
  workflow_version?: string | null; enabled?: boolean; updated_at?: string | null;
  validation_ok?: boolean; warnings?: string[]; skills?: WorkflowSkillStatus[];
  rules?: Record<string, unknown>;
};
export type WorkflowBanner = {
  current_version?: string; label?: string; update_hint?: string;
  stale_skills?: string[]; validation_ok?: boolean;
};
export type WorkflowPayload = { banner?: WorkflowBanner; summary?: WorkflowSummary; detail?: string };
export type WorkflowSnapshotPayload = {
  task_id?: string; found?: boolean; version_line?: string; notice?: string; detail?: string;
  snapshot?: {
    workflow_version?: string; created_at?: string; manifest_path?: string;
    validation_ok?: boolean; stale_skills?: string[];
    skills?: { skill_id: string; version: string; checksum?: string | null; priority?: number }[];
    rules?: Record<string, unknown>;
  };
};
// 局部任务（cover_/title_/subtitle_/edit_ 前缀，各带 E_ 错误码）轮询状态。
export type LocalTaskStatus = {
  task_id?: string; task_type?: string; project_id?: string; clip_id?: string;
  status?: "queued" | "processing" | "completed" | "failed"; stage?: string; progress?: number;
  error?: string | null; detail?: string;
  result?: {
    cover_revision?: number; cover_asset_id?: string; style_id?: string; title?: string;
    applied?: boolean; idempotent?: boolean; intro_duration_s?: number; intro_replaced?: boolean;
    cover_path?: string; copy_report?: { reason?: string } | null; clip_final_unchanged?: boolean;
    can_undo?: boolean; undo_revision?: number; card?: ProjectClipCard;
    ranges_count?: number; render_revision?: number; pending_rerender?: boolean;
    video_brain_task_id?: string | null; workflow_version?: string | null;
  } | null;
};
export type ClipCoverHistoryPayload = {
  project_id?: string; clip_id?: string; cover?: ClipCoverVersion | null;
  revisions?: ClipCoverHistoryEntry[]; detail?: string;
};
// 局部任务类型：封面/标题/字幕/时间线/音频。AUTO_EDIT 被后端 422 拒绝，绝不从这里提交。
export type LocalTaskType = "REGENERATE_COVER" | "APPLY_COVER" | "UPDATE_TITLE" | "UPDATE_SUBTITLE_STYLE" | "UPDATE_TIMELINE" | "UPDATE_AUDIO";
export type LocalTaskSpec = {
  task_type: LocalTaskType;
  instruction?: string;
  payload?: Record<string, unknown>;
  style_id?: string;
  replace_intro?: boolean;
  cover_copy?: Record<string, unknown>;
  title?: string;
  /** 每次轮询回调（应用封面的进度 UI 用；其它任务忽略）。 */
  onProgress?: (status: LocalTaskStatus) => void;
  /** 轮询上限（默认 2 分钟；应用封面这种重编码任务显式放宽）。 */
  timeoutMs?: number;
};

export const LOCAL_TASK_POLL_INTERVAL_MS = 800;
export const LOCAL_TASK_POLL_TIMEOUT_MS = 120_000;
// 应用封面 = 真的重编码整条成片（3~4 分钟切片要几十秒到几分钟），不能沿用 2 分钟上限。
export const APPLY_COVER_POLL_TIMEOUT_MS = 15 * 60_000;

// 「一条 clip 一张卡」的核心保证（按 clip_id 去重）与卡片→素材项换算都在这个纯模块里，
// 并有回归测试覆盖：src/lib/studio-clip-cards.ts。
// adaptCardToClip 负责把 segments 兜底成 []（台账卡片没有转写分段）：只有 projectId 的
// 会话恢复场景下没有 previous 可继承，undefined 会让选中成片后的 `segments.length` 崩溃。
export const renderedClipFromCard = (card: ProjectClipCard, previous?: RenderedClip | null): RenderedClip => (
  { ...adaptCardToClip(card, previous),
    video_artifact_verified: Boolean(card.video_path && card.render_task_id) || previous?.video_artifact_verified,
    segments: card.segments ?? previous?.segments ?? [],
    subtitle_url: card.subtitle_url ?? previous?.subtitle_url,
    subtitle_transcript_version: card.subtitle_provenance?.transcript_version ?? card.subtitle_transcript_version ?? previous?.subtitle_transcript_version,
    subtitle_mode: card.subtitle_mode ?? previous?.subtitle_mode,
    subtitle_provenance: card.subtitle_provenance ?? previous?.subtitle_provenance,
  } as RenderedClip
);

// 台账卡片 → 既有封面工具条的状态形状，让工具条（状态文案/可回滚）在新数据源下继续工作。
export function clipCoverStateFromCard(card: ProjectClipCard): ClipCoverState {
  const cover = card.cover;
  return {
    task_id: card.render_task_id || card.task_id,
    clip_id: card.clip_id,
    cover_path: cover?.path ?? null,
    cover_url: cover?.path ?? null,
    has_cover: Boolean(cover?.path),
    has_baseline: cover?.can_undo ?? false,
    can_restore: cover?.can_undo ?? false,
    active: cover?.cover_asset_id ?? null,
    active_kind: cover?.kind ?? null,
    active_style_id: cover?.style_id ?? null,
  };
}

// 刷新恢复会话指针：只存 id，页面挂载时用现有 GET 端点拉回项目与成片状态
// （变换数据本身持久化在项目 EDL 与成片 sidecar，服务端是唯一事实来源）。
// 2026-10-16：工作台不再持久化/恢复任何会话指针（supoclip-studio-session 已废除，
// 原 ENABLE_SESSION_RESTORE 常量一并移除）。打开或刷新 /studio 一律空白起步：
// 不自动请求最近项目/切片/转写；历史项目只能经「打开历史视频」按钮或
// URL 参数 ?source_id=<project_id> 显式加载（见挂载区的 source_id effect）。
export const clipTransformKey = (clip: { task_id: string; clip_id: string }) => `${clip.task_id}:${clip.clip_id}`;

/**
 * Phone recordings often store a landscape-sized pixel buffer together with a
 * 90° display matrix. Browsers honour that matrix while laying the video out
 * from the raw dimensions, which can crop the top and bottom in a wide editor
 * preview. Read the small MP4 header/tail only — never the whole source file —
 * so the preview box can use the displayed orientation.
 */
export async function readMp4Rotation(file: File): Promise<0 | 90 | -90 | 180> {
  // Walk the top-level MP4 atoms first. `moov` can live after a multi-gigabyte
  // `mdat`, so probing just the file head/tail is not reliable.
  let atomOffset = 0;
  while (atomOffset + 8 <= file.size) {
    const header = new DataView(await file.slice(atomOffset, atomOffset + 16).arrayBuffer());
    const declaredSize = header.getUint32(0);
    const atomType = String.fromCharCode(header.getUint8(4), header.getUint8(5), header.getUint8(6), header.getUint8(7));
    const atomSize = declaredSize === 1 && header.byteLength >= 16
      ? Number(header.getBigUint64(8))
      : declaredSize === 0 ? file.size - atomOffset : declaredSize;
    if (!Number.isFinite(atomSize) || atomSize < 8 || atomOffset + atomSize > file.size) break;
    if (atomType === "moov") {
      // A movie header is tiny compared with the source media. Keep a strict
      // ceiling so merely importing a large video never reads the video payload.
      if (atomSize > 16 * 1024 * 1024) return 0;
      const bytes = new Uint8Array(await file.slice(atomOffset, atomOffset + atomSize).arrayBuffer());
      for (let offset = 0; offset <= bytes.length - 48; offset += 1) {
      if (bytes[offset] !== 0x74 || bytes[offset + 1] !== 0x6b || bytes[offset + 2] !== 0x68 || bytes[offset + 3] !== 0x64) continue;
      const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      const dataStart = offset + 4;
      const version = view.getUint8(dataStart);
      const matrixStart = dataStart + (version === 1 ? 52 : 40);
      if (matrixStart + 20 > bytes.length) continue;
      const a = view.getInt32(matrixStart);
      const b = view.getInt32(matrixStart + 4);
      const c = view.getInt32(matrixStart + 12);
      const d = view.getInt32(matrixStart + 16);
      const unit = 65536;
      if (Math.abs(a) < unit / 8 && Math.abs(d) < unit / 8 && b > unit / 2 && c < -unit / 2) return 90;
      if (Math.abs(a) < unit / 8 && Math.abs(d) < unit / 8 && b < -unit / 2 && c > unit / 2) return -90;
      if (a < -unit / 2 && d < -unit / 2) return 180;
      }
      return 0;
    }
    atomOffset += atomSize;
  }
  return 0;
}

export const formatTime = (seconds: number) => {
  const total = Math.max(0, Math.floor(seconds || 0));
  return [Math.floor(total / 3600), Math.floor((total % 3600) / 60), total % 60].map((value) => String(value).padStart(2, "0")).join(":");
};
