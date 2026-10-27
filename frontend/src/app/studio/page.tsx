"use client";

import { loadSourceWorkspace } from "./source-workspace";
import { uploadStudioVideo, studioUploadBinding, type StudioUploadBinding } from "@/lib/studio-upload";
import { normalizeWorkflowJob, isWorkflowTerminal, workflowSourceLabel, workflowSource, subtitleModeLabel,
  readableWorkflowError, workflowArtifactUrl, isWorkflowVideoArtifact, parseWorkflowSrt,
  generateProjectId, downloadWorkflowArtifact, workflowClipExportUrl, revisionEdits, revisionRequest, reusableWorkflowRequest, revisionWarnings, revisionWarningLabel, type WorkflowJob, type WorkflowSource, type WorkflowArtifact,
  type RecoveryMode, type RevisionDocument } from "@/lib/studio-workflow";
import { ChangeEvent, DragEvent, MouseEvent as ReactMouseEvent, PointerEvent as ReactPointerEvent, SyntheticEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AudioLines, Check, Download, Film, FolderOpen, House, ListVideo, LoaderCircle, Menu, Plus, RotateCcw, Save, Scissors, Search, Settings2, Trash2, Upload, X } from "lucide-react";
import "./studio.css";
import { ProjectManager } from "@/components/studio/project-manager";
import { StudioCopilot, type CopilotAction, type CopilotMessage } from "./studio-copilot";
import { TimelineToolbar, type TimelineAddKind } from "@/components/studio/timeline-toolbar";
import { TimelineTracks, TIMELINE_COVER_DRAG_TYPE, findActiveTimelineImage, type TimelineSubtitleCue } from "@/components/studio/timeline-tracks";
import { TimelineImagePreview } from "@/components/studio/timeline-image-preview";
import {
  DEFAULT_TIMELINE_VIEW,
  applyZoomAnchored,
  clampTrackHeight,
  clampZoom,
  fitToWindowPxPerSecond,
  loadTimelineView,
  nextZoomStep,
  saveTimelineViewPatch,
  type TimelineView,
  type WaveformRatio,
} from "@/lib/timeline-view";
import {
  audioTrackRequest,
  deleteTimelineTrack,
  fetchTimelineHistory,
  fetchTimelinePeaks,
  postTimelineTrack,
  stepTimelineHistory,
  uploadTimelineAudio,
  type TimelineAudioTrack,
  type TimelineHistoryPayload,
} from "@/lib/timeline-edit";
import { TransformStage } from "@/components/studio/transform-stage";
import { TransformPanel } from "@/components/studio/transform-panel";
import { PlayerControlBar } from "@/components/studio/player-control-bar";
import { PlayerTitleBar } from "@/components/studio/player-title-bar";
import { FIT_VIEWER_ZOOM, clampViewerZoom } from "@/components/studio/viewer-zoom";
import { DEFAULT_VIEWER_ASPECT, type ViewerAspect } from "@/components/studio/viewer-aspect";
// ===== Inspector Phase 1：右侧真实编辑面板（唯一新 UI 归属）=====
// 只做接线：面板自己按 {projectId, clipId} 拉数据/存版本；页面只提供共享状态
// （字幕叠加层配置、时间线字幕轨、播放器 seek、既有封面/变换 handler）。
import { InspectorPane, type InspectorSubtitleState, type InspectorTabId } from "@/components/studio/inspector/inspector-pane";
import { SubtitleOverlay } from "@/components/studio/inspector/subtitle-overlay";
import { putClipConfig } from "@/components/studio/inspector/api";
import CoverAssetsPanel, { CoverLightbox } from "@/components/studio/cover-assets-panel";
import {
  coverTemplateSelection,
  findCoverTemplate,
  normalizeCoverTemplateLibrary,
  selectedCoverTemplateId,
  type CoverTemplateLibrary,
} from "@/lib/cover-template-library";
import {
  adaptCardToClip,
  cardCoverCanUndo,
  CLIP_VIDEO_MISSING_HINT,
  clipFirstFrameUrl,
  coverPathOf,
  coverVersionLabel,
  dedupeByClipId,
  isPlayableVideoPath,
  shouldSurfaceVideoError,
  VIDEO_LOAD_FAILED_HINT,
  workflowVersionLine,
} from "@/lib/studio-clip-cards";
import {
  defaultTransform,
  isDefaultTransform,
  parseTransform,
  transformPayload,
  type BackgroundMode,
  type ClipTransform,
} from "@/lib/clip-transform";

type SpeakerRole = "未标注" | "医生" | "患者";
// 拆分后转写 artifact 的分段自带纠错信息：raw_text 是原始转写文本，
// correction_status 标记该句是否被纠错（corrected/no_change/pending_review）。
type Segment = { start: number; end: number; text: string; speaker?: SpeakerRole; raw_text?: string; correction_status?: "corrected" | "no_change" | "pending_review"; segment_id?: string; start_ms?: number; end_ms?: number; uncertain?: boolean; correction_reason?: string };
// 「开始转写」job 完成后的凭据：自动剪辑 job（独立任务）只靠它提交，不再依赖
// 前端内存里的时间线/金句。四个字段与 POST /api/jobs/auto-edit 的请求体一一对应。
type TranscriptRef = {
  object_key: string;
  transcript_id: string;
  transcript_version: number;
  project_id: string;
};
type TimelineRange = { id: string; source_start: number; source_end: number; transform?: Record<string, unknown> | null };
type TimelineProject = { id: string; timeline: { ranges: TimelineRange[] }; artifacts: { edl: string; srt: string; fcpxml: string } };
type GoldenSentence = { start: number; end: number; text: string; reason: string };
type StudioInsights = { golden_sentences: GoldenSentence[]; keywords: string[]; engine: "llm" | "local-fallback"; notice?: string | null };
type TranscriptCredential = { object_key: string; transcript_id: string; transcript_version: number; project_id: string };
type RenderedClip = {
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
type AutoEditTask = {
  task_id: string;
  status: "queued" | "running" | "completed" | "failed";
  stage: string;
  progress: number;
  error?: string;
  artifacts: { name: string; path: string; size_bytes: number }[];
  clips?: RenderedClip[];
};
type SourceWorkspace = { projectId: string | null; name: string; credential: TranscriptRef | null; document: RevisionDocument | null; previewUrl: string; duration: number; segments: Segment[]; timeline: TimelineProject | null; insights: StudioInsights | null; previewRotation: 0 | 90 | -90 | 180; fallbackNotice?: boolean };
type TimelinePointer = { clientX: number; currentTarget: HTMLDivElement };

// 封面风格：两版成员风格（与后端 cover_replace.SUPPORTED_COVER_STYLES 一致）。
type CoverStyleId = "realframe_v2" | "cutout_v3";
type CoverStyleOption = { id: CoverStyleId; name: string; tip: string };
const COVER_STYLES: CoverStyleOption[] = [
  { id: "realframe_v2", name: "第二版·真实帧", tip: "真实帧大字：保留真实人物与诊室场景，仅轻微增强对比度，顶部白字主题 + 人物嘴下两行黄字钩子。无需额外依赖，永远可用。" },
  { id: "cutout_v3", name: "第三版·人物抠像", tip: "抠像大字：背景强虚化 + 人物白描边前景，缩略图更抢眼。依赖 u2net 抠像权重；本机未安装时会把 E_CUTOUT_WEIGHTS_MISSING 与安装指引原样返回，不会偷偷降级成第二版。" },
];
// 「沿用已生成的封面」结果 / 台账（GET .../cover）。
type ClipCoverState = {
  task_id: string; clip_id: string; cover_path?: string | null; cover_url?: string | null;
  has_cover: boolean; has_baseline: boolean; can_restore: boolean;
  active?: string | null; active_kind?: string | null; active_style_id?: string | null;
  supported_styles?: string[]; default_style_id?: string;
};
// 封面通道（重制 / 沿用 / 回滚）返回体：含可读原因、QC 结论与来源说明。
type CoverMutationResult = ClipCoverState & {
  kind: "generated" | "adopted" | "restore";
  style_id?: string | null; replace_intro: boolean; idempotent: boolean;
  cover_revision?: number | null;
  source?: { type: string; task_id?: string; clip_id?: string } | null;
  qc?: { passed?: boolean; failures?: string[]; checks?: { check: string; passed: boolean; detail: string }[] } | null;
  detail?: string;
};
type ProjectCoverEntry = {
  clip_id: string; name?: string; topic?: string; cover_path?: string | null;
  has_cover: boolean; active?: string | null; active_kind?: string | null; cover_url?: string | null;
};

// ===== 项目级成片台账（GET /studio/projects/{project_id}/clips）=====
// 卡片的事实源是「项目 + 稳定 clip_id」，不是自动剪辑任务：一条 clip 永远只有
// 一张卡，封面/标题/字幕/时间线局部任务只原地刷新该卡，绝不新增卡片。
type ClipCoverVersion = {
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
type ClipCoverHistoryEntry = {
  revision: number; created_at: string | null; kind: string | null;
  style_id?: string | null; cover_asset_id?: string | null; sha256?: string | null;
  instruction?: string | null;
};
type ProjectClipCard = {
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
// active_clip_id（2026-10-21 阶段）：服务端记录的项目唯一选中切片；随卡片载荷透出，
// 台账载入后据此恢复上次选中（null/缺省 = 原片视图，不恢复任何成片）。
type ProjectClipsPayload = { project_id?: string; card_count?: number; cards?: ProjectClipCard[]; active_clip_id?: string | null; active_clip_updated_at?: string | null; detail?: string };
// ===== 规则中心（剪辑规则注册表 / Phase B）=====
// GET /studio/workflow 每次实时读清单：外部改 skill 文件或启停 skill 后，面板刷新即见新版本。
type WorkflowSkillStatus = {
  skill_id: string; version: string; enabled: boolean;
  status: "ok" | "stale" | "missing" | "invalid" | "disabled" | string;
  detail?: string; paths?: string[];
};
type WorkflowSummary = {
  workflow_version?: string | null; enabled?: boolean; updated_at?: string | null;
  validation_ok?: boolean; warnings?: string[]; skills?: WorkflowSkillStatus[];
  rules?: Record<string, unknown>;
};
type WorkflowBanner = {
  current_version?: string; label?: string; update_hint?: string;
  stale_skills?: string[]; validation_ok?: boolean;
};
type WorkflowPayload = { banner?: WorkflowBanner; summary?: WorkflowSummary; detail?: string };
type WorkflowSnapshotPayload = {
  task_id?: string; found?: boolean; version_line?: string; notice?: string; detail?: string;
  snapshot?: {
    workflow_version?: string; created_at?: string; manifest_path?: string;
    validation_ok?: boolean; stale_skills?: string[];
    skills?: { skill_id: string; version: string; checksum?: string | null; priority?: number }[];
    rules?: Record<string, unknown>;
  };
};
// 局部任务（cover_/title_/subtitle_/edit_ 前缀，各带 E_ 错误码）轮询状态。
type LocalTaskStatus = {
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
type ClipCoverHistoryPayload = {
  project_id?: string; clip_id?: string; cover?: ClipCoverVersion | null;
  revisions?: ClipCoverHistoryEntry[]; detail?: string;
};
// 局部任务类型：封面/标题/字幕/时间线/音频。AUTO_EDIT 被后端 422 拒绝，绝不从这里提交。
type LocalTaskType = "REGENERATE_COVER" | "APPLY_COVER" | "UPDATE_TITLE" | "UPDATE_SUBTITLE_STYLE" | "UPDATE_TIMELINE" | "UPDATE_AUDIO";
type LocalTaskSpec = {
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

const LOCAL_TASK_POLL_INTERVAL_MS = 800;
const LOCAL_TASK_POLL_TIMEOUT_MS = 120_000;
// 应用封面 = 真的重编码整条成片（3~4 分钟切片要几十秒到几分钟），不能沿用 2 分钟上限。
const APPLY_COVER_POLL_TIMEOUT_MS = 15 * 60_000;

// 「一条 clip 一张卡」的核心保证（按 clip_id 去重）与卡片→素材项换算都在这个纯模块里，
// 并有回归测试覆盖：src/lib/studio-clip-cards.ts。
// adaptCardToClip 负责把 segments 兜底成 []（台账卡片没有转写分段）：只有 projectId 的
// 会话恢复场景下没有 previous 可继承，undefined 会让选中成片后的 `segments.length` 崩溃。
const renderedClipFromCard = (card: ProjectClipCard, previous?: RenderedClip | null): RenderedClip => (
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
function clipCoverStateFromCard(card: ProjectClipCard): ClipCoverState {
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
const clipTransformKey = (clip: { task_id: string; clip_id: string }) => `${clip.task_id}:${clip.clip_id}`;

/**
 * Phone recordings often store a landscape-sized pixel buffer together with a
 * 90° display matrix. Browsers honour that matrix while laying the video out
 * from the raw dimensions, which can crop the top and bottom in a wide editor
 * preview. Read the small MP4 header/tail only — never the whole source file —
 * so the preview box can use the displayed orientation.
 */
async function readMp4Rotation(file: File): Promise<0 | 90 | -90 | 180> {
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

const formatTime = (seconds: number) => {
  const total = Math.max(0, Math.floor(seconds || 0));
  return [Math.floor(total / 3600), Math.floor((total % 3600) / 60), total % 60].map((value) => String(value).padStart(2, "0")).join(":");
};

export default function StudioPage() {
  const inputRef = useRef<HTMLInputElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  // video error 只上报一次（防循环）：记录已展示过失败提示的 src（见 shouldSurfaceVideoError）。
  const videoErrorSrcRef = useRef<string | null>(null);
  const trackGridRef = useRef<HTMLDivElement>(null);
  const scrubbingRef = useRef(false);
  const [file, setFile] = useState<File | null>(null);
  const uploadBindingRef = useRef<StudioUploadBinding | null>(null);
  const workflowGenerationRef = useRef(0);
  const workflowArtifactCacheRef = useRef(new Map<string, Record<string, unknown>>());
  const [workflowJob, setWorkflowJob] = useState<WorkflowJob | null>(null);
  const [workflowSources, setWorkflowSources] = useState<Record<string, WorkflowSource>>({});
  const [workflowActionBusy, setWorkflowActionBusy] = useState(false);
  const workflowActionRef = useRef<{ key: string; id: string } | null>(null);
  const [sourceReviewOpen, setSourceReviewOpen] = useState(false);
  const [sourceReviewDocument, setSourceReviewDocument] = useState<RevisionDocument | null>(null);
  const [sourceReviewDraft, setSourceReviewDraft] = useState<Record<number, string>>({});
  const [sourceReviewVersion, setSourceReviewVersion] = useState("");
  const [sourceReviewReason, setSourceReviewReason] = useState("");
  const [sourceReviewMessage, setSourceReviewMessage] = useState("");
  const [sourceReviewBusy, setSourceReviewBusy] = useState(false);
  const revisionRequestRef = useRef<{ key: string; id: string } | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  // 播放器当前加载失败的提示文案（video error 降级占位；成功加载即清除）。
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [duration, setDuration] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [timeline, setTimeline] = useState<TimelineProject | null>(null);
  const [selectedSegment, setSelectedSegment] = useState<number | null>(null);
  const [transcriptionState, setTranscriptionState] = useState<"idle" | "processing" | "error">("idle");
  const [transcriptionStage, setTranscriptionStage] = useState("");
  const [message, setMessage] = useState("");
  const [scrubbing, setScrubbing] = useState(false);
  const [previewRotation, setPreviewRotation] = useState<0 | 90 | -90 | 180>(0);
  const [insights, setInsights] = useState<StudioInsights | null>(null);
  const [extractingInsights, setExtractingInsights] = useState(false);
  const [autoEditTask, setAutoEditTask] = useState<AutoEditTask | null>(null);
  const [autoEditing, setAutoEditing] = useState(false);
  const [renderedClips, setRenderedClips] = useState<RenderedClip[]>([]);
  const [activeRenderedClip, setActiveRenderedClip] = useState<RenderedClip | null>(null);
  // ===== 项目级成片卡片（按 clip_id 去重，局部任务只原地刷新）=====
  // projectCards 是左侧「剪辑成片」的唯一事实源；renderedClips 保留为回退链路
  // （台账端点不可用 / 旧任务）与转写分段、画面变换的继承来源。
  const [projectCards, setProjectCards] = useState<ProjectClipCard[]>([]);
  // 项目 id（timeline_*）单独留存：`timeline` 状态在选中成片后会被**成片局部时间线**
  // （id = rendered_{task}_{clip}）覆盖，任何项目级调用（台账 / 局部任务）都不能读它，
  // 否则选中成片后「重新制作封面」会因拿不到项目 id 而报「该项目没有台账」。
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null);
  const rememberProjectId = useCallback((candidate?: string | null) => {
    if (candidate?.startsWith("timeline_")) { if (workspaceProjectRef.current !== candidate) workspaceEpoch.current += 1; workspaceProjectRef.current = candidate; setActiveProjectId(candidate); }
  }, []);
  // ===== 转写 / 自动剪辑任务拆分（两个独立 job）=====
  // projectIdRef：导入素材时生成的稳定项目 id（timeline_ + 12 hex），转写与自动剪辑
  // 都用它当 project_id；removeFile / 重新导入时重置。只在会话内使用，不落盘。
  const [projectIdRef, setProjectIdRef] = useState<string | null>(null);
  // transcriptRef：转写 job 终态（transcript_ready）产出的凭据——自动剪辑 job 的唯一
  // 入参来源（object_key/transcript_id/transcript_version/project_id）。没有它时
  // 「开始自动剪辑」保持禁用，提示「请先完成转写与纠错。」
  const [transcriptRef, setTranscriptRef] = useState<TranscriptRef | null>(null);
  // 端点返回的卡片数（与前端去重后的卡片数一起显示，便于核对「一条 clip 一张卡」）。
  const [projectCardCount, setProjectCardCount] = useState(0);
  // 台账是否已成功加载：加载成功即视为事实源，未加载时回退到渲染任务列表。
  const [projectCardsLoaded, setProjectCardsLoaded] = useState(false);
  // 切片选择（2026-10-21 阶段）：台账载荷里的项目唯一 active_clip_id（服务端事实源）。
  const [serverActiveClip, setServerActiveClip] = useState<{ project_id: string; clip_id: string | null } | null>(null);
  // 每个项目工作区只做一次「刷新恢复」：用户本会话已有选中后，不再被台账自动抢走。
  const activeClipRestoreProjectRef = useRef<string | null>(null);
  // 切片切换持久化的代际：失败回滚只作用于「仍是最新一次点击、且当前还停在该切片」的情形。
  const activeClipPersistSeq = useRef(0);
  // 素材卡缩略图（2026-10-21 阶段）= 切片视频真实第一帧；提取失败(404)的 clip 记住降级为
  // 占位图标，绝不用生成封面冒充第一帧。项目切换时整体重置。
  const [firstFrameFailures, setFirstFrameFailures] = useState<Record<string, boolean>>({});
  // ===== 封面版型库（2026-10-21 阶段）=====
  // 版型集合：后端版本化注册表（GET /studio/cover-templates）拉一次；绝不前端硬编码。
  const [coverTemplateLibrary, setCoverTemplateLibrary] = useState<CoverTemplateLibrary | null>(null);
  const [coverTemplatesState, setCoverTemplatesState] = useState<{ loading: boolean; error: string | null }>({ loading: false, error: null });
  // 版型封面工作流（Make/Apply/Delete 共用）：generateBusy=Make 在途（值为 template_id），
  // actionBusy=应用/删除在途。
  const [coverTemplateGenerateBusy, setCoverTemplateGenerateBusy] = useState<string | null>(null);
  const [coverTemplateActionBusy, setCoverTemplateActionBusy] = useState(false);
  /** 应用封面渲染任务的实时进度（阶段 + 百分比）；null = 未在应用。 */
  const [coverApplyProgress, setCoverApplyProgress] = useState<{ stage: string; percent: number } | null>(null);
  /** 成片文件版本：应用封面/重渲会原地替换 clip_final.mp4，URL 不变会被浏览器缓存成旧画面。 */
  const [clipVideoVersion, setClipVideoVersion] = useState(0);
  // 版型选择的乐观写代际：失败回滚只作用于「仍是最新一次选择」的情形。
  const coverTemplateSelectSeq = useRef(0);
  // 清空项目任务数据（任务记录 + 产物；源片与转写保留）：预览态 + 执行中的忙碌态。
  // 「刷新即删」被刻意排除 —— 台账是落盘持久数据，只能由显式二次确认触发。
  const [clearDialog, setClearDialog] = useState<
    { cardCount: number; clipCount: number; taskCount: number; reclaimableBytes: number } | null
  >(null);
  const [clearBusy, setClearBusy] = useState(false);
  // 重新剪辑并替换旧切片（第五步扩展）：一次确认，成功后旧切片永久删除。
  const [reeditConfirmOpen, setReeditConfirmOpen] = useState(false);
  const [reeditBusy, setReeditBusy] = useState(false);
  const [cleanupRetryRun, setCleanupRetryRun] = useState<string | null>(null);
  // 项目管理弹层（2026-10-19）：左上角小房子入口；页内浮层，不跳转路由、不刷新工作台。
  const [projectManagerOpen, setProjectManagerOpen] = useState(false);
  // 项目名称内联编辑（2026-10-20）：顶栏中央显示名称，点击进入输入框；
  // 保存走 PATCH（owner + 乐观并发 expected_updated_at），空名禁止、Esc 取消、失败回滚。
  const [projectDisplayName, setProjectDisplayName] = useState<string | null>(null);
  const [projectNameUpdatedAt, setProjectNameUpdatedAt] = useState<string | null>(null);
  const [projectNameEditing, setProjectNameEditing] = useState(false);
  const [projectNameDraft, setProjectNameDraft] = useState("");
  const [projectNameSaving, setProjectNameSaving] = useState(false);
  const [projectNameSavedTick, setProjectNameSavedTick] = useState(false);
  const [projectNameError, setProjectNameError] = useState<string | null>(null);
  const projectNameTickTimer = useRef<number | null>(null);
  const [reeditDialogState, setReeditDialogState] = useState<
    { status: "loading" | "ready" | "missing"; credential?: TranscriptCredential }>({ status: "loading" });
  // 重新剪辑凭据：会话 transcriptRef 优先；旧项目按台账 id 从转写存储找回最新版。
  const [projectTranscriptRef, setProjectTranscriptRef] = useState<{
    object_key: string; transcript_id: string; transcript_version: number; project_id: string;
  } | null>(null);
  const [openCardMenu, setOpenCardMenu] = useState<string | null>(null);
  // 「查看历史」内联面板：只展开被点名的卡片。
  const [coverHistoryClipId, setCoverHistoryClipId] = useState<string | null>(null);
  const [coverHistory, setCoverHistory] = useState<ClipCoverHistoryEntry[]>([]);
  const [coverHistoryLoading, setCoverHistoryLoading] = useState(false);
  // 成片卡缩略图大图预览（2026-10-20 紧凑横向卡）：只有点缩略图才打开。
  const [coverBigPreview, setCoverBigPreview] = useState<{ url: string; name: string } | null>(null);
  const [localTaskBusy, setLocalTaskBusy] = useState(false);
  // ===== 规则中心状态（Phase B：规则版本可见性 + 启停 + 按最新规则重新生成）=====
  const [workflowBanner, setWorkflowBanner] = useState<WorkflowBanner | null>(null);
  const [workflowSummary, setWorkflowSummary] = useState<WorkflowSummary | null>(null);
  const [workflowPanelOpen, setWorkflowPanelOpen] = useState(false);
  const [workflowBusy, setWorkflowBusy] = useState(false);
  const [workflowMessage, setWorkflowMessage] = useState<string | null>(null);
  // 「查看本次成片实际使用了哪些规则」：该 clip 渲染任务的快照（旧成片会带 notice）。
  const [workflowSnapshot, setWorkflowSnapshot] = useState<WorkflowSnapshotPayload | null>(null);
  const [workflowSnapshotClipId, setWorkflowSnapshotClipId] = useState<string | null>(null);
  // 卸载守卫：局部任务轮询不得在组件销毁后写状态。
  const mountedRef = useRef(true);
  // 局部任务回调里读到的「最新」素材/卡片：避免把庞大的状态数组放进 useCallback 依赖。
  const renderedClipsRef = useRef<RenderedClip[]>([]);
  const projectCardsRef = useRef<ProjectClipCard[]>([]);
  const activeClipRef = useRef<RenderedClip | null>(null);
  // ===== 封面通道状态（轻量秒级：不整片重渲）=====
  // coverStyle：用户选定的「按风格重新生成」风格；coverBusy：封面请求进行中。
  const [coverStyle, setCoverStyle] = useState<CoverStyleId>("realframe_v2");
  const [coverBusy, setCoverBusy] = useState(false);
  // 画布封面视图：选中成片时可把画布切到「该成片已生成的 cover.jpg」当首帧看。
  const [showCoverFrame, setShowCoverFrame] = useState(false);
  // 封面缓存击穿版本号：封面文件路径固定，替换后必须让 <img> 重新拉取。
  const [coverVersion, setCoverVersion] = useState(0);
  const [coverState, setCoverState] = useState<ClipCoverState | null>(null);
  const [coverSource, setCoverSource] = useState<string>("");
  const [projectCovers, setProjectCovers] = useState<ProjectCoverEntry[]>([]);
  const [copilotMessages, setCopilotMessages] = useState<CopilotMessage[]>([]);
  const [copilotBusy, setCopilotBusy] = useState(false);
  const [copilotCollapsed, setCopilotCollapsed] = useState(false);
  const [copilotHighlight, setCopilotHighlight] = useState<{ start: number; end: number } | null>(null);
  // ===== Inspector Phase 1 接线状态（页面只存「共享」的那一份）=====
  // 当前标签：转写标签的既有 DOM 保持挂载，只切换显示，避免丢失搜索词与滚动位置。
  const [inspectorTab, setInspectorTab] = useState<InspectorTabId>("transcript");
  // 播放器内实时字幕叠加层 + 底部时间线字幕轨的唯一数据来源（由 InspectorPane 上抛）。
  const [inspectorSubtitles, setInspectorSubtitles] = useState<InspectorSubtitleState | null>(null);
  // 音频标签的预览音轨（原声音量/静音/BGM）：只影响本地观看，不重复落盘（配置由面板写）。
  const [inspectorVoiceConfig, setInspectorVoiceConfig] = useState<{ voice_volume: number; muted: boolean; audio_tracks: unknown[] } | null>(null);
  // 上抛回调放进 ref：InspectorPane 用内联 lambda 上报时不应该因身份变化触发重复上报。
  const inspectorSubtitlesRef = useRef<InspectorSubtitleState | null>(null);
  const handleInspectorSubtitlesChange = useCallback((state: InspectorSubtitleState | null) => {
    const previous = inspectorSubtitlesRef.current;
    // 内容没变就保持同一个引用：避免页面每次渲染都重设状态。
    if (previous === state) return;
    if (previous && state && previous.config === state.config && previous.cues === state.cues) return;
    inspectorSubtitlesRef.current = state;
    setInspectorSubtitles(state);
  }, []);
  const sourceWorkspaceRef = useRef<SourceWorkspace | null>(null);
  const [remoteSource, setRemoteSource] = useState<{ projectId: string; name: string; duration: number } | null>(null);
  const [sourceReturnBusy, setSourceReturnBusy] = useState(false);
  const [sourceReturnError, setSourceReturnError] = useState<string | null>(null);
  const sourceReturnSeq = useRef(0);
  const sourceReturnAbort = useRef<AbortController | null>(null);
  const workspaceEpoch = useRef(0);
  const workspaceProjectRef = useRef(activeProjectId);
  workspaceProjectRef.current = activeProjectId;
  const workspaceBindingRef = useRef("");
  workspaceBindingRef.current = `${activeProjectId || projectIdRef || ""}:${activeRenderedClip ? clipTransformKey(activeRenderedClip) : "source"}`;
  const [inspectorPreview, setInspectorPreview] = useState<{ binding: string; transform: ClipTransform } | null>(null);
  const inspectorSelectionRef = useRef("");
  const inspectorApplyInFlight = useRef(new Set<string>());
  const [inspectorBackgroundPreview, setInspectorBackgroundPreview] = useState<{ binding: string; background: BackgroundMode } | null>(null);
  // ===== 剪映式画面变换（非破坏性，预览与导出同源数学）=====
  const [clipTransforms, setClipTransforms] = useState<Record<string, ClipTransform>>({});
  const [rangeTransforms, setRangeTransforms] = useState<Record<number, ClipTransform>>({});
  // 裁剪/变换模式：进入后画布出现 8 控制点，拖动/滚轮/裁剪才生效。
  const [transformMode, setTransformMode] = useState(false);
  const [ratioLock, setRatioLock] = useState(false);
  // 背景沿既有客户端应用状态；本轮不新增后端字段或刷新持久化能力。
  const [transformBackground, setTransformBackground] = useState<BackgroundMode>("blur");
  const [videoDimensions, setVideoDimensions] = useState({ width: 1080, height: 1920 });
  // 查看比例（viewerZoom）：只改变画布在预览区的显示大小；
  // 与上面的片段变换完全分离，绝不写入 transform，也绝不进入导出参数。
  const [viewerZoom, setViewerZoom] = useState(FIT_VIEWER_ZOOM);
  // 预览画布比例（viewerAspect）：只改变预览框形状，同样是观看参数。
  const [viewerAspect, setViewerAspect] = useState<ViewerAspect>(DEFAULT_VIEWER_ASPECT);
  // 裁切/重构图草稿：进入模式时对当前 transform 做快照；模式内的所有改动只写草稿
  // （预览实时变化），只有点「应用」才走既有持久化；取消 / Esc 回滚快照、不写后端。
  const [transformDraft, setTransformDraft] = useState<ClipTransform | null>(null);
  const transformSnapshotRef = useRef<ClipTransform | null>(null);
  // 预览音量/静音、网格参考线、循环播放：都只影响本地观看，不写入片段。
  const [previewVolume, setPreviewVolume] = useState(1);
  const [previewMuted, setPreviewMuted] = useState(false);
  const [showGuides, setShowGuides] = useState(false);
  const [loopPlayback, setLoopPlayback] = useState(false);
  // 播放器卡片（标题栏 + 画面区 + 控制栏）：全屏目标，也是容器查询的作用域内元素。
  const playerCardRef = useRef<HTMLDivElement>(null);
  // 裁剪/变换浮层的实测高度：画布据此预留空间，保证 8 个手柄不被浮层遮挡。
  const transformFloatRef = useRef<HTMLDivElement>(null);
  const [transformFloatHeight, setTransformFloatHeight] = useState(0);
  // 折叠只影响浮层高度与观看空间，不改变任何变换值。
  const [transformPanelCollapsed, setTransformPanelCollapsed] = useState(false);
  const clipTransformTimer = useRef<number | null>(null);
  const timelineSaveTimer = useRef<number | null>(null);
  // ===== 底部时间线接线（Phase B）：TimelineToolbar / TimelineTracks =====
  // 视图状态只影响显示（缩放/轨高/波形占比/滚动），绝不进入任何导出参数；
  // 历史（revision）的 can_undo/can_redo 来自后端，音轨与 ranges 来自当前 revision。
  const [timelineView, setTimelineView] = useState<TimelineView>(DEFAULT_TIMELINE_VIEW);
  const [timelineViewportWidth, setTimelineViewportWidth] = useState(0);
  const [timelineCanUndo, setTimelineCanUndo] = useState(false);
  const [timelineCanRedo, setTimelineCanRedo] = useState(false);
  const [revisionAudioTracks, setRevisionAudioTracks] = useState<TimelineAudioTrack[]>([]);
  // 波形缓存：track_id → 后端 ffmpeg 真实峰值；缺失时不画假波形（组件契约）。
  const [peaksByTrack, setPeaksByTrack] = useState<Record<string, number[]>>({});
  const [peaksDurationByTrack, setPeaksDurationByTrack] = useState<Record<string, number>>({});
  const [timelineBusy, setTimelineBusy] = useState(false);
  const timelineScrollRef = useRef<HTMLDivElement>(null);
  const timelineViewSaveTimer = useRef<number | null>(null);
  // GET view 恢复完成的项目 id：恢复完成前不回写，避免用默认值覆盖已保存的视图。
  const timelineViewHydratedRef = useRef<string | null>(null);
  // 最近一次已保存/已恢复的视图（序列化）：相同内容不重复 PUT。
  const timelineViewSavedRef = useRef<string | null>(null);
  // 已取过 peaks 的 (track_id:path)：undo/redo 后同轨不重复请求。
  const timelinePeaksFetchedRef = useRef<Set<string>>(new Set());
  // ＋ 菜单的音频文件选择器（bgm/sfx 需要先选文件再上传）。
  const timelineAudioInputRef = useRef<HTMLInputElement>(null);
  const timelinePendingAddRef = useRef<"bgm" | "sfx" | null>(null);

  // 卸载守卫：局部任务轮询与卡片刷新都通过 mountedRef 判断是否还该写状态。
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; sourceReturnAbort.current?.abort(); };
  }, []);

  const chooseFile = (candidate?: File) => {
    if (!candidate || (!candidate.type.startsWith("video/") && !candidate.name.toLowerCase().endsWith(".mp4"))) return;
    resetWorkflowSession();
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    setFile(candidate); setPreviewUrl(URL.createObjectURL(candidate)); setDuration(0); setCurrentTime(0); setSegments([]); setTimeline(null); setInsights(null); setAutoEditTask(null); setAutoEditing(false); setRenderedClips([]); setActiveRenderedClip(null); sourceWorkspaceRef.current = null; setSelectedSegment(null); setTranscriptionState("idle"); setMessage(""); setPreviewRotation(0);
    setClipTransforms({}); setRangeTransforms({}); setTransformMode(false); setVideoDimensions({ width: 1080, height: 1920 });
    // 新素材 = 新项目：重置稳定项目 id 并清掉上一条素材的转写凭据（自动剪辑随之禁用，
    // 直到转写重新完成）。项目 id 只活在会话内，不再写入 localStorage。
    const projectRef = generateProjectId();
    setProjectIdRef(projectRef); setTranscriptRef(null);
  };
  const removeFile = () => { resetWorkflowSession(); if (previewUrl && !activeRenderedClip) URL.revokeObjectURL(previewUrl); setFile(null); setPreviewUrl(null); setDuration(0); setCurrentTime(0); setSegments([]); setTimeline(null); setInsights(null); setAutoEditTask(null); setAutoEditing(false); setRenderedClips([]); setActiveRenderedClip(null); sourceWorkspaceRef.current = null; setMessage(""); setPreviewRotation(0); setClipTransforms({}); setRangeTransforms({}); setTransformMode(false); setProjectIdRef(generateProjectId()); setTranscriptRef(null); };
  useEffect(() => {
    let cancelled = false;
    // A generated vertical clip has its own pixels and must never inherit the
    // display matrix of the imported phone recording.
    if (!file || activeRenderedClip) return undefined;
    void readMp4Rotation(file).then((rotation) => { if (!cancelled) setPreviewRotation(rotation); }).catch(() => { if (!cancelled) setPreviewRotation(0); });
    return () => { cancelled = true; };
  }, [file, activeRenderedClip]);
  // API 与页面同源；公网入口由 DSH 会话鉴权后直连服务器。
  const transcribeServiceBase = "/video-api";
  const resetWorkflowSession = () => {
    workflowGenerationRef.current += 1;
    sourceReturnSeq.current += 1;
    sourceReturnAbort.current?.abort(); setSourceReturnBusy(false);
    setRemoteSource(null); setInspectorPreview(null); setInspectorBackgroundPreview(null);
    workspaceEpoch.current += 1; workspaceProjectRef.current = null; setActiveProjectId(null);
    renderedClipsRef.current = []; projectCardsRef.current = []; setProjectCards([]); setProjectCardsLoaded(false);
    // 切片选择（2026-10-21 阶段）：会话重置同样清掉恢复通道与缩略图失败标记。
    activeClipRestoreProjectRef.current = null; setServerActiveClip(null); setFirstFrameFailures({});
    // 封面版型库（2026-10-21 阶段）：生成结果为会话态，随会话重置。
    setCoverTemplateGenerateBusy(null); setCoverTemplateActionBusy(false);
    uploadBindingRef.current = null;
    workflowArtifactCacheRef.current.clear();
    workflowActionRef.current = null;
    revisionRequestRef.current = null;
    setWorkflowJob(null); setWorkflowSources({}); setWorkflowActionBusy(false);
    setSourceReviewOpen(false); setSourceReviewDocument(null); setSourceReviewDraft({});
    setSourceReviewMessage(""); setSourceReviewVersion(""); setSourceReviewReason(""); setSourceReviewBusy(false);
    setTranscriptionState("idle"); setTranscriptionStage("");
  };

  const observeWorkflowJob = async (raw: Record<string, unknown>, jobId: string, kind: WorkflowJob["kind"]) => {
    const generation = workflowGenerationRef.current;
    const job = normalizeWorkflowJob(raw, jobId, kind);
    setWorkflowJob(job);
    await Promise.all(job.artifacts.filter((item) => ["content_analysis", "clip_plan", "transcript_json"].includes(item.kind)).map(async (item) => {
      const url = workflowArtifactUrl(transcribeServiceBase, item.url);
      try {
        let document = workflowArtifactCacheRef.current.get(url);
        if (!document) {
          const response = await fetch(url, { cache: "no-store" });
          if (!response.ok) throw new Error(`来源记录读取失败（${response.status}）`);
          document = await response.json() as Record<string, unknown>;
          if (generation !== workflowGenerationRef.current) return;
          workflowArtifactCacheRef.current.set(url, document);
        }
        if (generation !== workflowGenerationRef.current) return;
        const stage = item.kind === "content_analysis" ? "analysis" : item.kind === "clip_plan" ? "planning" : "correction";
        const evidence = stage === "correction" ? document.correction_summary as Record<string, unknown> | undefined : document;
        setWorkflowSources((old) => ({ ...old, [stage]: evidence ? workflowSource(evidence) :
          { source: null, error: "旧记录缺少纠错来源，当前来源未知。", warnings: [], usage_status: null } }));
      } catch (error) {
        if (generation !== workflowGenerationRef.current) return;
        const stage = item.kind === "content_analysis" ? "analysis" : item.kind === "clip_plan" ? "planning" : "correction";
        setWorkflowSources((old) => ({ ...old, [stage]: { source: null,
          error: error instanceof Error ? error.message : "来源记录读取失败", warnings: [], usage_status: null } }));
      }
    }));
    return job;
  };

  const pollWorkflowRecovery = async (jobId: string, kind: WorkflowJob["kind"], generation: number) => {
    const started = Date.now();
    for (;;) {
      const response = await fetch(`${transcribeServiceBase}/api/jobs/${encodeURIComponent(jobId)}`, { cache: "no-store" });
      const payload = await response.json() as Record<string, unknown>;
      if (generation !== workflowGenerationRef.current) return;
      if (!response.ok) throw new Error(readableWorkflowError(payload.detail, `读取任务失败（${response.status}）`));
      const job = await observeWorkflowJob(payload, jobId, kind);
      if (generation !== workflowGenerationRef.current) return;
      setMessage(job.stage || job.status);
      if (isWorkflowTerminal(job.status)) {
        const clips = await fetchStagedClips(job, jobId);
        if (generation !== workflowGenerationRef.current) return;
        if (clips.length) setRenderedClips(clips);
        if (activeProjectId && kind !== "transcribe") await loadProjectCards(activeProjectId);
        if (["failed", "technical_failed", "partial_failed"].includes(job.status)) {
          throw new Error(job.error || "任务仍有未完成部分；成功产物已保留。");
        }
        setMessage(job.status === "cancelled" ? "任务已取消；已有成功产物保留。" :
          job.error || job.stage || "任务操作已完成。");
        return;
      }
      if (Date.now() - started > 3600_000) throw new Error("读取任务超时；请稍后重试读取，任务状态以服务端为准。");
      await new Promise((resolve) => setTimeout(resolve, 2500));
    }
  };

  const requestWorkflowAction = async (action: RecoveryMode | "cancel") => {
    if (!workflowJob || workflowActionBusy) return;
    if (action === "cancel" ? !workflowJob.cancel_supported : !workflowJob.available_actions.includes(action)) return;
    const job = workflowJob, generation = workflowGenerationRef.current;
    const key = `${job.job_id}:${action}`;
    workflowActionRef.current = reusableWorkflowRequest(workflowActionRef.current, key);
    const id = workflowActionRef.current.id;
    setWorkflowActionBusy(true);
    try {
      const response = await fetch(`${transcribeServiceBase}/api/jobs/${encodeURIComponent(job.job_id)}/${action === "cancel" ? "cancel" : "retry"}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(action === "cancel" ? { idempotency_key: id } : { mode: action, idempotency_key: id }),
      });
      const payload = await response.json().catch(() => ({})) as Record<string, unknown>;
      if (generation !== workflowGenerationRef.current) return;
      if (!response.ok) throw new Error(readableWorkflowError(payload.detail, `任务操作失败（${response.status}）`));
      workflowActionRef.current = null;
      setMessage(action === "cancel" ? "取消请求已接受，正在核对服务端终态…" : "恢复请求已接受，正在读取同一任务…");
      await pollWorkflowRecovery(job.job_id, job.kind, generation);
    } catch (error) {
      if (generation === workflowGenerationRef.current) setMessage(error instanceof Error ? error.message : "任务操作失败；请重试。");
    } finally {
      if (generation === workflowGenerationRef.current) {
        setWorkflowActionBusy(false); setAutoEditing(false); setReeditBusy(false);
      }
    }
  };

  const refreshWorkflowStatus = async () => {
    if (!workflowJob || workflowActionBusy) return;
    const generation = workflowGenerationRef.current, job = workflowJob;
    try {
      const response = await fetch(`${transcribeServiceBase}/api/jobs/${encodeURIComponent(job.job_id)}`, { cache: "no-store" });
      const payload = await response.json() as Record<string, unknown>;
      if (generation !== workflowGenerationRef.current) return;
      if (!response.ok) throw new Error(readableWorkflowError(payload.detail, `任务状态读取失败（${response.status}）`));
      await observeWorkflowJob(payload, job.job_id, job.kind);
    } catch (error) {
      if (generation === workflowGenerationRef.current) setMessage(error instanceof Error ? error.message : "任务状态读取失败");
    }
  };

  const loadSourceRevision = async (latest: boolean) => {
    if (!transcriptRef?.object_key || sourceReviewBusy || activeRenderedClip) return;
    const generation = workflowGenerationRef.current;
    const query = new URLSearchParams({ object_key: transcriptRef.object_key, transcript_id: transcriptRef.transcript_id });
    const version = latest ? "" : sourceReviewVersion || String(transcriptRef.transcript_version);
    if (version) query.set("transcript_version", version);
    setSourceReviewBusy(true); setSourceReviewMessage("正在载入只读原文与核听版本…");
    try {
      const response = await fetch(`${transcribeServiceBase}/api/studio/transcripts/revisions?${query}`, { cache: "no-store" });
      const payload = await response.json() as { document?: RevisionDocument; detail?: unknown };
      if (generation !== workflowGenerationRef.current) return;
      if (!response.ok || !payload.document) throw new Error(readableWorkflowError(payload.detail, `修订版本读取失败（${response.status}）`));
      setSourceReviewDocument(payload.document);
      setSourceReviewVersion(String(payload.document.transcript_version));
      setSourceReviewMessage(`已载入 v${payload.document.transcript_version}；原始转写只读，修改仅在提交后保存为新版本。${latest ? "现有草稿已保留，请逐句核对冲突。" : ""}`);
    } catch (error) {
      if (generation === workflowGenerationRef.current) setSourceReviewMessage(error instanceof Error ? error.message : "修订版本读取失败");
    } finally { if (generation === workflowGenerationRef.current) setSourceReviewBusy(false); }
  };

  const saveSourceRevision = async () => {
    if (!sourceReviewDocument || !transcriptRef || sourceReviewBusy || activeRenderedClip) return;
    if (!revisionEdits(sourceReviewDocument, sourceReviewDraft).length) { setSourceReviewMessage("没有文本变化，不创建新版本。"); return; }
    const generation = workflowGenerationRef.current;
    const fingerprint = JSON.stringify({ ref: transcriptRef.object_key, version: sourceReviewDocument.transcript_version,
      edits: revisionEdits(sourceReviewDocument, sourceReviewDraft), reason: sourceReviewReason });
    revisionRequestRef.current = reusableWorkflowRequest(revisionRequestRef.current, fingerprint);
    const body = revisionRequest(sourceReviewDocument, transcriptRef.object_key, sourceReviewDraft,
      revisionRequestRef.current.id, sourceReviewReason);
    setSourceReviewBusy(true); setSourceReviewMessage("正在保存人工核听版本…");
    try {
      const response = await fetch(`${transcribeServiceBase}/api/studio/transcripts/revisions`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      const payload = await response.json() as { document?: RevisionDocument; transcript_id?: string; transcript_version?: number; detail?: unknown };
      if (generation !== workflowGenerationRef.current) return;
      if (!response.ok || !payload.document) throw new Error(readableWorkflowError(payload.detail,
        `修订保存失败（${response.status}）；草稿已保留。`));
      const document = payload.document;
      setSourceReviewDocument(document); setSourceReviewDraft({}); revisionRequestRef.current = null;
      setSourceReviewVersion(String(document.transcript_version));
      setSegments(document.asr_segments as Segment[]);
      const updatedRef = { ...transcriptRef, transcript_id: document.transcript_id, transcript_version: document.transcript_version };
      setTranscriptRef(updatedRef); setProjectTranscriptRef(updatedRef);
      setReeditDialogState({ status: "ready", credential: updatedRef });
      if (sourceWorkspaceRef.current?.projectId === transcriptRef.project_id) {
        sourceWorkspaceRef.current.segments = document.asr_segments as Segment[];
        sourceWorkspaceRef.current.credential = updatedRef; sourceWorkspaceRef.current.document = document;
      }
      setSourceReviewMessage(`人工核听已保存为 v${document.transcript_version}；后续自动剪辑使用这一版本。已有成片保持其原字幕版本。`);
      setMessage(`当前源转写已切换至人工核听 v${document.transcript_version}。`);
    } catch (error) {
      if (generation === workflowGenerationRef.current) setSourceReviewMessage(
        `${error instanceof Error ? error.message : "保存失败"}；草稿与本次请求标识保留，可直接重试；版本冲突时请载入最新版本核对。`);
    } finally { if (generation === workflowGenerationRef.current) setSourceReviewBusy(false); }
  };

  const hydrateClipSubtitles = async (clip: RenderedClip) => {
    const generation = workflowGenerationRef.current;
    try {
      const response = await fetch(`${transcribeServiceBase}/api/jobs/${encodeURIComponent(clip.task_id)}`, { cache: "no-store" });
      const payload = await response.json() as { artifacts?: WorkflowArtifact[]; detail?: unknown };
      if (!response.ok) throw new Error(readableWorkflowError(payload.detail, `成片字幕读取失败（${response.status}）`));
      const artifacts = (payload.artifacts || []).filter((item) => item.clip_id === clip.clip_id || item.kind === "clip_plan");
      const refreshed = (await fetchStagedClips({ artifacts }, clip.task_id))[0];
      const active = activeClipRef.current;
      if (!refreshed || generation !== workflowGenerationRef.current || active?.task_id !== clip.task_id || active.clip_id !== clip.clip_id) return;
      const updated = { ...clip, ...refreshed, name: clip.name, topic: clip.topic };
      setActiveRenderedClip(updated); setSegments(updated.segments);
      setRenderedClips((old) => old.map((item) => item.clip_id === updated.clip_id && item.task_id === updated.task_id ? updated : item));
    } catch (error) {
      const active = activeClipRef.current;
      if (generation === workflowGenerationRef.current && active?.task_id === clip.task_id && active.clip_id === clip.clip_id) {
        setActiveRenderedClip((old) => old ? { ...old, subtitle_notice: error instanceof Error ? error.message : "字幕尚未载入" } : old);
      }
    }
  };

  const focusSourceReviewSegment = (position: number) => {
    if (!sourceReviewDocument?.asr_segments.length) return;
    const index = Math.max(0, Math.min(sourceReviewDocument.asr_segments.length - 1, position));
    const segment = sourceReviewDocument.asr_segments[index];
    setSelectedSegment(index);
    seek(segment.start_ms !== undefined ? segment.start_ms / 1000 : segment.start);
  };


  const startTranscription = async () => {
    if (!file || transcriptionState === "processing") return;
    const generation = workflowGenerationRef.current;
    setTranscriptionState("processing");
    const transcribeBase = transcribeServiceBase;
    // 转写属于**项目级**操作：优先用导入素材时生成的稳定项目 id（转写与自动剪辑共用）；
    // 兜底链只服务旧会话恢复等 projectIdRef 缺失的场景。
    const projectId = projectIdRef || activeProjectId || (timeline?.id?.startsWith("timeline_") ? timeline.id : null) || "timeline_unsaved";
    try {
      // ① 能力探测：R2 已配置 → 浏览器直传 R2；否则本机分片回退
      let storage: "r2" | "local" = "local";
      try {
        const capResponse = await fetch(`${transcribeBase}/api/uploads/capabilities`, { cache: "no-store" });
        if (capResponse.ok) {
          const caps = await capResponse.json() as { storage?: string };
          storage = caps.storage === "r2" ? "r2" : "local";
        }
      } catch { /* 探测失败按本地模式 */ }

      const projectIdSafe = projectId;
      let jobId = "";
      // 拆分契约：自动剪辑 job 需要 object_key，它在两条上传路径里的来源不同
      // （R2 = 上传会话响应；本地 = assemble 响应），任务轮询响应里也可能带。
      let uploadObjectKey = "";

      if (storage === "r2") {
        // ---- R2 直传：会话 → 并发分片 PUT（单片重试）→ complete → 提交任务 ----
        setMessage("正在创建上传会话…");
        const sessionResponse = await fetch(`${transcribeBase}/api/uploads`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename: file.name, size_bytes: file.size, project_id: projectIdSafe, content_type: file.type || "video/mp4" }),
        });
        const sessionText = await sessionResponse.text();
        const session = sessionText ? JSON.parse(sessionText) as { upload_id?: string; object_key?: string; part_size?: number; parts?: { index: number; url: string }[]; detail?: string } : {};
        if (!sessionResponse.ok || !session.upload_id || !session.parts) throw new Error(session.detail || `上传会话创建失败（${sessionResponse.status}）`);
        const byteSize = session.part_size || 16 * 1024 * 1024;
        const etags = new Map<number, string>();
        const uploadPart = async (part: { index: number; url: string }, done: (label: string) => void) => {
          const blob = file.slice(part.index * byteSize, Math.min(file.size, (part.index + 1) * byteSize));
          let attempt = 0;
          for (;;) {
            try {
              const put = await fetch(part.url, { method: "PUT", body: blob });
              if (!put.ok) throw new Error(String(put.status));
              const etag = put.headers.get("ETag");
              if (!etag) throw new Error("对象存储未返回 ETag，请检查桶 CORS");
              etags.set(part.index, etag);
              done(`正在直传视频到对象存储（${part.index + 1}/${session.parts!.length} 片）…`);
              return;
            } catch {
              attempt += 1;
              if (attempt >= 3) throw new Error(`视频直传中断（第 ${part.index + 1}/${session.parts!.length} 片），请检查网络后重试。`);
              await new Promise((resolve) => setTimeout(resolve, 1200 * attempt));
            }
          }
        };
        let uploadedCount = 0;
        const workers = Array.from({ length: Math.min(4, session.parts.length) }, async (_, workerIndex) => {
          for (let index = workerIndex; index < session.parts!.length; index += Math.min(4, session.parts!.length)) {
            await uploadPart(session.parts![index], () => { uploadedCount += 1; setMessage(`正在直传视频到对象存储（${uploadedCount}/${session.parts!.length} 片）…`); });
          }
        });
        await Promise.all(workers);
        const completeResponse = await fetch(`${transcribeBase}/api/uploads/${session.upload_id}/complete`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ parts: session.parts.map((part) => ({ index: part.index, etag: etags.get(part.index) })) }),
        });
        if (!completeResponse.ok) throw new Error(`上传合并失败（${completeResponse.status}）`);
        // 拆分契约：body 增加 mode="transcribe_only"，转写 job 不再串完整链。
        const jobResponse = await fetch(`${transcribeBase}/api/jobs/transcribe`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ object_key: session.object_key, project_id: projectIdSafe, config: { language: "zh" }, mode: "transcribe_only" }),
        });
        const jobText = await jobResponse.text();
        const jobPayload = jobText ? JSON.parse(jobText) as { job_id?: string; detail?: string } : {};
        if (!jobResponse.ok || !jobPayload.job_id) throw new Error(jobPayload.detail || `任务提交失败（${jobResponse.status}）`);
        jobId = jobPayload.job_id;
        uploadObjectKey = session.object_key || "";
      } else {
        // 本地分片路径：后端 assemble 默认就是 transcribe_only；响应里的 object_key
        // 是自动剪辑 job 的必要凭据（旧的复用上传路径拿不到时再从任务轮询兜底）。
        const binding = studioUploadBinding(file, projectIdSafe, uploadBindingRef.current);
        uploadBindingRef.current = binding;
        const uploaded = binding.uploaded ?? await uploadStudioVideo(file, transcribeBase, projectIdSafe,
          (text) => { if (generation === workflowGenerationRef.current) setMessage(text); },
          { uploadId: binding.uploadId });
        binding.uploaded = uploaded;
        jobId = uploaded.job_id;
        uploadObjectKey = uploaded.object_key || "";
      }

      // ② 轮询统一任务状态（queued → 提取音轨/转写/纠错 → transcript_ready）；
      // 自动剪辑不再是这个 job 的一部分，完成处理只载入分段与凭据。
      if (generation !== workflowGenerationRef.current) return;
      setWorkflowJob(normalizeWorkflowJob({ status: "queued" }, jobId, "transcribe"));
      setMessage("已提交转写任务，等待本机执行器处理…");
      const startedAt = Date.now();
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, 2500));
        const pollResponse = await fetch(`${transcribeBase}/api/jobs/${jobId}`, { cache: "no-store" });
        const pollText = await pollResponse.text();
        const poll = pollText ? JSON.parse(pollText) as {
          status?: string; stage?: string; progress?: number; error?: string;
          waiting_for_executor?: boolean;
          object_key?: string;
          artifacts?: { kind: string; clip_id?: string | null; url: string }[];
          detail?: string;
        } : {};
        if (generation !== workflowGenerationRef.current) return;
        if (!pollResponse.ok) throw new Error(readableWorkflowError(poll.detail, "无法读取任务进度"));
        await observeWorkflowJob(poll, jobId, "transcribe");
        if (generation !== workflowGenerationRef.current) return;
        const status = poll.status || "queued";
        if (status === "queued" && poll.waiting_for_executor) {
          setMessage(`等待本机执行器（已等待 ${Math.round((Date.now() - startedAt) / 1000)} 秒）…`);
          continue;
        }
        setTranscriptionStage(status); setMessage(poll.stage || status);
        if (["failed", "technical_failed", "partial_failed", "cancelled"].includes(status)) {
          throw new Error(poll.error || (status === "cancelled" ? "转写任务已取消" : "转写任务失败"));
        }
        if (status === "transcript_ready" || status === "completed") {
          const artifact = (poll.artifacts || []).find((item) => item.kind === "transcript_json");
          let segments: Segment[] = []; let jobInsights: StudioInsights | null = null;
          let jobTimeline: TimelineProject | null = null; let jobAutoEdit: AutoEditTask | null = null;
          let transcriptId = ""; let transcriptVersion = 0;
          if (artifact) {
            const transcriptResponse = await fetch(artifact.url.startsWith("/") ? `${transcribeBase}${artifact.url}` : artifact.url, { cache: "no-store" });
            const transcript = await transcriptResponse.json() as {
              asr_segments?: Segment[]; insights?: StudioInsights; timeline?: TimelineProject; auto_edit_task?: AutoEditTask;
              transcript_id?: string; transcript_version?: number; correction_summary?: Record<string, unknown>;
            };
            if (generation !== workflowGenerationRef.current) return;
            if (transcript.correction_summary) setWorkflowSources((old) => ({ ...old, correction: workflowSource(transcript.correction_summary!) }));
            segments = transcript.asr_segments || []; jobInsights = transcript.insights || null;
            jobTimeline = transcript.timeline || null; jobAutoEdit = transcript.auto_edit_task || null;
            transcriptId = transcript.transcript_id || "";
            transcriptVersion = Number(transcript.transcript_version || 0);
          }
          if (status === "completed" && jobAutoEdit) {
            // 兼容旧链路：拆分前的 completed job artifact 自带完整自动剪辑结果
            // （恢复的旧会话 / 未升级后端），维持原有一次性载入行为，不得破坏。
            if (!segments.length) throw new Error("任务完成但转写内容为空");
            setSegments(segments); setInsights(jobInsights); setTranscriptionState("idle");
            setTimeline(jobTimeline); setAutoEditTask(jobAutoEdit);
            setMessage(`转写完成：${segments.length} 段已按时间戳载入时间线。${jobAutoEdit?.clips?.length ? "成片已加入项目素材，可选择预览和导出。" : ""}`);
            return;
          }
          // 拆分后的转写完成（transcript_ready，或无 auto_edit_task 的 completed）：
          // 只载入纠错后分段 + 记录凭据；不得 setTimeline / setAutoEditTask / setInsights，
          // 金句、时间线与成片交给用户手动触发的独立自动剪辑 job。
          if (!segments.length) throw new Error("任务完成但转写内容为空");
          setSegments(segments);
          setTranscriptionState("idle");
          setTranscriptRef({
            object_key: uploadObjectKey || poll.object_key || "",
            transcript_id: transcriptId,
            transcript_version: transcriptVersion,
            project_id: projectIdSafe,
          });
          setMessage("转写与纠错已完成，可查看时间戳后点击顶部“开始自动剪辑”。");
          return;
        }
      }
    } catch (error) {
      if (generation !== workflowGenerationRef.current) return;
      setTranscriptionState("error");
      setMessage(error instanceof Error ? error.message : "转写失败，请检查本机服务。");
    }
  };

  const UNUSED_startTranscription_legacy = async () => {
    if (!file) return;
    setTranscriptionState("processing"); setMessage("正在上传视频并启动本机转写…");
    try {
      // 大视频分片上传（每片 ≤4MB，逐片重试）：公网免费隧道对大单请求不可靠，
      // 小分片可稳定穿越并支持断点式重试；服务器合并后启动异步转写任务。
      const uploadId = crypto.randomUUID().replace(/-/g, "");
      const chunkSize = 4 * 1024 * 1024;
      const totalChunks = Math.max(1, Math.ceil(file.size / chunkSize));
      let uploadedChunks = 0;
      const uploadOneChunk = async (index: number) => {
        const blob = file.slice(index * chunkSize, Math.min(file.size, (index + 1) * chunkSize));
        let attempt = 0;
        for (;;) {
          try {
            const chunkResponse = await fetch(`${transcribeServiceBase}/studio/transcribe/chunk?upload_id=${uploadId}&index=${index}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: blob });
            if (!chunkResponse.ok) throw new Error(`分片上传失败（${chunkResponse.status}）`);
            uploadedChunks += 1;
            setMessage(`正在上传视频（${uploadedChunks}/${totalChunks} 片）…`);
            return;
          } catch {
            attempt += 1;
            if (attempt >= 3) throw new Error(`视频上传中断（第 ${index + 1}/${totalChunks} 片），请检查网络后重试。`);
            await new Promise((resolve) => setTimeout(resolve, 1200 * attempt));
          }
        }
      };
      // 3 路并发分片上传，弱网单分片失败只重试该分片。
      const workers = Array.from({ length: Math.min(3, totalChunks) }, async (_, workerIndex) => {
        for (let index = workerIndex; index < totalChunks; index += Math.min(3, totalChunks)) {
          await uploadOneChunk(index);
        }
      });
      await Promise.all(workers);
      const assembleResponse = await fetch(`${transcribeServiceBase}/studio/transcribe/assemble`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ upload_id: uploadId, filename: file.name, total: totalChunks }),
      });
      const startText = await assembleResponse.text();
      const startPayload = startText ? JSON.parse(startText) as { job_id?: string; detail?: string; error?: string } : {};
      if (!assembleResponse.ok || !startPayload.job_id) throw new Error(startPayload.detail || startPayload.error || `转写服务不可用（${assembleResponse.status}）`);
      setMessage("视频已上传，正在用本机 faster-whisper（small · CPU · int8 · VAD）转写并纠错…");
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, 3000));
        const pollResponse = await fetch(`${transcribeServiceBase}/studio/transcribe/jobs/${startPayload.job_id}`, { cache: "no-store" });
        const pollText = await pollResponse.text();
        const poll = pollText ? JSON.parse(pollText) as { status?: "processing" | "done" | "error"; result?: { asr_segments?: Segment[]; insights?: StudioInsights; timeline?: TimelineProject }; error?: string; detail?: string } : {};
        if (!pollResponse.ok) throw new Error(poll.detail || "无法读取转写进度。");
        if (poll.status === "error") throw new Error(poll.error || "转写失败，请检查视频音轨。");
        if (poll.status !== "done") continue;
        const result = poll.result;
        if (!result?.asr_segments?.length) throw new Error("未识别到可用人声，请检查视频音轨。");
        setSegments(result.asr_segments); setInsights(result.insights || null); setTimeline(result.timeline || null); setTranscriptionState("idle");
        setMessage(`转写完成：${result.asr_segments.length} 段已按时间戳载入时间线。`);
        return;
      }
    } catch (error) { setTranscriptionState("error"); setMessage(error instanceof Error ? error.message : "转写失败，请检查本机服务。"); }
  };
  const extractInsights = async () => {
    if (!segments.length) return;
    setExtractingInsights(true);
    setMessage("正在从当前转写文稿提取金句与关键词…");
    try {
      const response = await fetch(
        `${transcribeServiceBase}/studio/insights`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ asr_segments: segments }) },
      );
      const text = await response.text();
      const payload = text ? JSON.parse(text) as { insights?: StudioInsights; detail?: string; error?: string } : {};
      if (!response.ok || !payload.insights) throw new Error(payload.detail || payload.error || "金句提取服务暂不可用。");
      setInsights(payload.insights);
      setMessage(payload.insights.engine === "llm" ? "模型已提取金句与关键词。" : "当前显示本地候选；配置模型后会自动升级为模型提取。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "金句提取失败，请重试。"); }
    finally { setExtractingInsights(false); }
  };
  const autoEditBaseUrl = `${transcribeServiceBase}/studio/auto-edit`;
  // /studio/... 前缀的 API 基址：与 copilotBaseUrl 用同一条推导规则（同源同前缀）。
  const studioBase = autoEditBaseUrl.replace(/\/studio\/auto-edit$/, "");
  const artifactUrl = (taskId: string, artifactPath: string, download = false) => `${autoEditBaseUrl}/${taskId}/artifact?path=${encodeURIComponent(artifactPath)}${download ? "&download=true" : ""}`;
  // staged 渲染（第五步）的成片文件就是后端 artifact 直连 URL；legacy 成片仍走 runner 路由。
  const clipFileUrl = (clip: Pick<RenderedClip, "task_id" | "video_path">, download = false) => (
    clip.video_path.startsWith("/") || clip.video_path.startsWith("http")
      ? clip.video_path
      : `${artifactUrl(clip.task_id, clip.video_path, download)}${!download && clipVideoVersion > 0 ? `&v=${clipVideoVersion}` : ""}`
  );
  // 从任务 artifacts 构建 staged 成片卡片（clip_final + clip_plan 元数据）。
  const fetchStagedClips = async (
    poll: { artifacts?: WorkflowArtifact[] }, runId: string,
  ): Promise<RenderedClip[]> => {
    // artifact kind是事实来源；本地鉴权file URL没有.mp4/.srt后缀。
    const clipArtifacts = (poll.artifacts || []).filter(isWorkflowVideoArtifact);
    if (!clipArtifacts.length) return [];
    const planArtifact = (poll.artifacts || []).find((item) => item.kind === "clip_plan");
    type PlanClip = { clip_id?: string; topic?: string; start_ms?: number; end_ms?: number;
      duration_ms?: number; warnings?: string[]; completeness?: string };
    let planClips: PlanClip[] = [], transcriptVersion: number | undefined;
    if (planArtifact?.url) {
      try {
        const response = await fetch(workflowArtifactUrl(transcribeServiceBase, planArtifact.url), { cache: "no-store" });
        if (!response.ok) throw new Error(`计划读取失败（${response.status}）`);
        const plan = await response.json() as { clips?: PlanClip[]; transcript_version?: number };
        planClips = plan.clips || []; transcriptVersion = plan.transcript_version;
      } catch { /* 成片仍可播放，来源缺失保持未知。 */ }
    }
    const planById = new Map(planClips.map((item) => [String(item.clip_id), item]));
    return Promise.all(clipArtifacts.map(async (item) => {
      const planned = planById.get(String(item.clip_id)) || {};
      const subtitle = (poll.artifacts || []).find((artifact) =>
        artifact.kind === "srt" && artifact.clip_id === item.clip_id);
      const metadataArtifact = (poll.artifacts || []).find((artifact) =>
        artifact.kind === "clip_warnings" && artifact.clip_id === item.clip_id);
      let metadata: { subtitle_mode?: string; subtitle_segments?: Segment[];
        subtitle_provenance?: { transcript_id?: string; transcript_version?: number };
        render_warnings?: string[] } = {};
      if (metadataArtifact) {
        try {
          const response = await fetch(workflowArtifactUrl(transcribeServiceBase, metadataArtifact.url), { cache: "no-store" });
          if (response.ok) metadata = await response.json() as typeof metadata;
        } catch { /* 烧录与来源状态保持未知，不编造。 */ }
      }
      let cues: Segment[] = [], subtitleNotice: string | null = null;
      if (subtitle) {
        try {
          const response = await fetch(workflowArtifactUrl(transcribeServiceBase, subtitle.url), { cache: "no-store" });
          if (!response.ok) throw new Error(`字幕读取失败（${response.status}）`);
          cues = parseWorkflowSrt(await response.text()) as Segment[];
        } catch (error) { subtitleNotice = error instanceof Error ? error.message : "成片字幕尚未载入"; }
      } else { subtitleNotice = "此成片尚未登记独立SRT，字幕时间线来源未知。"; }
      if (!cues.length && metadata.subtitle_segments?.length) cues = metadata.subtitle_segments;
      return {
        task_id: runId, clip_id: String(item.clip_id), name: String(planned.topic || item.clip_id),
        topic: String(planned.topic || ""), source_start: (planned.start_ms ?? 0) / 1000,
        source_end: (planned.end_ms ?? 0) / 1000, duration: (planned.duration_ms ?? 0) / 1000,
        video_path: workflowArtifactUrl(transcribeServiceBase, item.url), video_artifact_verified: true,
        cover_path: null, segments: cues, subtitle_url: subtitle?.url || null,
        subtitle_transcript_version: metadata.subtitle_provenance?.transcript_version ?? subtitle?.transcript_version ?? transcriptVersion,
        subtitle_mode: metadata.subtitle_mode, subtitle_provenance: metadata.subtitle_provenance,
        subtitle_notice: subtitleNotice, transform: null,
        warnings: [...(planned.warnings ?? []), ...(metadata.render_warnings ?? []), ...(subtitleNotice ? [subtitleNotice] : [])],
        completeness: planned.completeness ?? "pass",
      };
    }));
  };

  // 2026-10-16：会话指针已废除。这里只保留会话内绑定——渲染成片局部时间线时
  // timeline.id 会变成 rendered_{task}_{clip}，用 projects 台账 id 记住当前项目，
  // 驱动「重新剪辑 / 清空」按钮可用性；不再写任何 localStorage。
  useEffect(() => {
    const timelineProjectId = timeline?.id?.startsWith("timeline_") ? timeline.id : null;
    if (timelineProjectId) setActiveProjectId(timelineProjectId);
  }, [timeline?.id]);
  // 重新剪辑只解析当前明确绑定的项目；同物理源复用由该项目端点判定。
  const resolveReeditCredential = async (): Promise<TranscriptCredential | null> => {
    const projectId = workspaceProjectRef.current;
    const generation = workflowGenerationRef.current, epoch = workspaceEpoch.current;
    const current = () => mountedRef.current && generation === workflowGenerationRef.current && epoch === workspaceEpoch.current && workspaceProjectRef.current === projectId;
    if (!projectId?.startsWith("timeline_")) {
      setReeditDialogState({ status: "missing" });
      setMessage("当前成片没有明确项目绑定，无法重新剪辑。");
      return null;
    }
    try {
      const response = await fetch(`${transcribeServiceBase}/studio/projects/${encodeURIComponent(projectId)}/transcript-ref`, { cache: "no-store" });
      const data = await response.json() as TranscriptCredential & { detail?: unknown };
      if (!current()) return null;
      if (!response.ok) throw new Error(readableWorkflowError(data.detail, `当前项目转写读取失败（${response.status}）`));
      if (data.project_id !== projectId || !data.object_key || !data.transcript_id || !Number.isInteger(data.transcript_version) || data.transcript_version < 1) {
        throw new Error("当前项目返回的转写凭据不匹配，无法重新剪辑。");
      }
      setReeditDialogState({ status: "ready", credential: data });
      return data;
    } catch (error) {
      if (current()) {
        setReeditDialogState({ status: "missing" });
        setMessage(error instanceof Error ? error.message : "当前项目尚无可用转写，无法重新剪辑。");
      }
      return null;
    }
  };
  // 重新剪辑凭据找回：会话没有 transcriptRef 时按项目 id 查转写存储最新版。
  useEffect(() => {
    const pid = activeProjectId;
    if (!pid?.startsWith("timeline_")) { setProjectTranscriptRef(null); return undefined; }
    let cancelled = false;
    fetch(`${transcribeServiceBase}/studio/projects/${encodeURIComponent(pid)}/transcript-ref`, { cache: "no-store" })
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => { if (!cancelled) setProjectTranscriptRef(data ?? null); })
      .catch(() => { if (!cancelled) setProjectTranscriptRef(null); });
    return () => { cancelled = true; };
  }, [activeProjectId]);
  // options.skipPersist：恢复上次选中/失败回滚等「服务端已是该值」的通道不再回写 PUT。
  const selectRenderedClip = (clip: RenderedClip, options?: { skipPersist?: boolean }) => {
    const previousClip = activeRenderedClip;
    sourceReturnSeq.current += 1; sourceReturnAbort.current?.abort(); setSourceReturnBusy(false); setSourceReturnError(null);
    setInspectorPreview(null); setInspectorBackgroundPreview(null);
    workspaceBindingRef.current = `${activeProjectId || projectIdRef || ""}:${clipTransformKey(clip)}`;
    if (!activeRenderedClip && previewUrl) {
      sourceWorkspaceRef.current = { projectId: activeProjectId || projectIdRef, name: file?.name || remoteSource?.name || "原始视频", credential: transcriptRef, document: sourceReviewDocument, previewUrl, duration, segments, timeline, insights, previewRotation };
    }
    setActiveRenderedClip(clip);
    // 播放守卫：video_path 为空/非 .mp4（缺省、已清理或误存 JSON）的成片绝不进播放器
    // （否则 <video> 请求到 404/JSON → 黑屏坏控件）。卡片仍选中（封面/详情可用），
    // previewUrl 置 null 走占位，并给出可读原因。
    const playable = clip.video_artifact_verified ? Boolean(clip.video_path) : isPlayableVideoPath(clip.video_path);
    videoErrorSrcRef.current = null;
    setPreviewError(null);
    setPreviewUrl(playable ? clipFileUrl(clip) : null);
    setDuration(clip.duration);
    setCurrentTime(0);
    setSegments(clip.segments || []);   // 兜底：台账卡片可能没有转写分段（不能把 undefined 灌进状态）
    setTimeline({ id: `rendered_${clip.task_id}_${clip.clip_id}`, timeline: { ranges: [{ id: clip.clip_id, source_start: 0, source_end: clip.duration }] }, artifacts: { edl: "", srt: "", fcpxml: "" } });
    setInsights(null);
    setSelectedSegment(null);
    setPreviewRotation(0);
    setTransformMode(false);
    if (clip.transform) {
      // 成片自带已保存变换（sidecar）→ 立即并入本地状态供预览/导出使用。
      setClipTransforms((existing) => ({ ...existing, [clipTransformKey(clip)]: parseTransform(clip.transform) }));
    }
    setMessage(playable ? `正在查看${clip.name}：${clip.topic}` : `已选中${clip.name}：${CLIP_VIDEO_MISSING_HINT}。`);
    // 封面视图与台账随成片切换重置后重新拉取：封面不存在时画布静默回退到播放视频。
    setShowCoverFrame(false);
    setCoverState(null);
    setCoverSource(clip.clip_id);
    void refreshCoverState(clip);
    if (!clip.subtitle_url) void hydrateClipSubtitles(clip);
    // 切片选择持久化（2026-10-21 阶段）：点击卡片 = 唯一 active_clip_id 切换；
    // 失败时回滚保留原切片（见 persistActiveClipSelection）。
    if (!options?.skipPersist) void persistActiveClipSelection(previousClip, clip);
  };
  /**
   * 把点击的素材卡持久化为项目唯一 active_clip_id（PUT /studio/projects/{id}/active-clip）。
   * 乐观切换在先（播放器/转写/切片状态一次点击同步切换）；PUT 失败 → 回滚到点击前的
   * 原切片（原来在原片视图则回到原片），本地与服务端状态永远一致。只写台账一个指针
   * 字段：不改成片文件、不触发 ASR/分析/渲染。
   */
  const persistActiveClipSelection = async (previousClip: RenderedClip | null, next: RenderedClip) => {
    const pid = activeProjectId || projectIdRef;
    if (!pid?.startsWith("timeline_")) return;
    const seq = ++activeClipPersistSeq.current;
    try {
      const response = await fetch(`${studioBase}/studio/projects/${encodeURIComponent(pid)}/active-clip`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clip_id: next.clip_id }),
      });
      if (!response.ok) {
        const text = await response.text();
        let detail: unknown = text;
        try { detail = text ? JSON.parse(text) : null; } catch { /* 原样作为文本报错 */ }
        throw new Error(readableWorkflowError(detail, `切换选中切片失败（HTTP ${response.status}）`));
      }
    } catch (error) {
      // 回滚守卫：只纠正「这次失败的切换仍是最新一次选择操作」的情形（seq 在每次
      // 点击/回原片时递增，后来的操作必然更大）；用户已点了其它切片/已切项目时，
      // 以最新操作为准，绝不开倒车。不用 activeClipRef 做守卫——它靠渲染后 effect
      // 同步，快速失败（微任务内 reject）会早于 effect 落地而误判。
      if (!mountedRef.current || seq !== activeClipPersistSeq.current) return;
      if (workspaceProjectRef.current !== pid) return;
      const reason = error instanceof Error ? error.message : "切换选中切片失败";
      if (previousClip) {
        selectRenderedClip(previousClip, { skipPersist: true });
        setMessage(`${reason}，已保留原切片。`);
      } else {
        void selectSourceMedia();
        setMessage(`${reason}，已退回原片视图。`);
      }
    }
  };
  const selectSourceMedia = async (explicitId?: string) => {
    const projectId = explicitId || activeProjectId || projectIdRef;
    const binding = workspaceBindingRef.current;
    const seq = ++sourceReturnSeq.current;
    sourceReturnAbort.current?.abort();
    const controller = new AbortController(); sourceReturnAbort.current = controller;
    const current = () => mountedRef.current && seq === sourceReturnSeq.current && binding === workspaceBindingRef.current;
    setSourceReturnBusy(true); setSourceReturnError(null);
    try {
      let source = sourceWorkspaceRef.current;
      if (!source || source.projectId !== projectId) {
        if (!projectId?.startsWith("timeline_")) throw new Error("无法返回原片：当前成片没有项目绑定。");
        const restored = await loadSourceWorkspace(transcribeServiceBase, projectId, controller.signal);
        if (!current()) return;
        const { project, credential, document } = restored;
        source = { projectId, name: project.source.name, credential, document, previewUrl: restored.previewUrl,
          duration: project.source.duration, segments: (document?.asr_segments || []) as Segment[],
          timeline: { id: project.id, timeline: project.timeline, artifacts: project.artifacts }, insights: null, previewRotation: 0,
          fallbackNotice: restored.fallbackNotice };
        setRemoteSource({ projectId, name: source.name, duration: source.duration });
        const transforms: Record<number, ClipTransform> = {};
        project.timeline.ranges.forEach((range, index) => { if (range.transform) transforms[index] = parseTransform(range.transform); });
        setRangeTransforms(transforms);
        if (project.source.width && project.source.height) setVideoDimensions({ width: project.source.width, height: project.source.height });
      }
      if (!current()) return;
      sourceWorkspaceRef.current = source;
      setTranscriptRef(source.credential); setProjectTranscriptRef(source.credential);
      setSourceReviewDocument(source.document); setSourceReviewVersion(source.document ? String(source.document.transcript_version) : "");
      setSourceReviewDraft({}); revisionRequestRef.current = null; setSourceReviewOpen(false);
      setSourceReviewMessage(source.credential ? "已载入当前项目指定转写版本，可进行核听与人工修订。" : "原片已恢复，但尚无可用的转写版本；当前仅可查看原片。");
      setActiveRenderedClip(null); setInspectorPreview(null); setInspectorBackgroundPreview(null); setPreviewError(null); videoErrorSrcRef.current = null;
      setPreviewUrl(source.previewUrl); setDuration(source.duration); setCurrentTime(0); setSegments(source.segments);
      setTimeline(source.timeline); setInsights(source.insights); setPreviewRotation(source.previewRotation);
      setSelectedSegment(null); setTransformMode(false);
      setMessage(source.credential ? `已切回原始视频与转写 v${source.credential.transcript_version}。` : "原片已恢复，尚无可用转写版本；当前仅可查看原片。");
      // 预览代理未就绪（已回退原片）时的明确提示：代理后台构建，就绪后重开本项目即流畅。
      if (source.fallbackNotice) {
        setMessage("预览代理正在后台生成：当前回退播放原片（高码率 HEVC/4K 原片可能卡顿）；代理就绪后重新打开本项目即可流畅播放。");
      }
      // 切片选择（2026-10-21 阶段）：回到原片 = 清除唯一 active_clip_id（clip_id=null）。
      // 只在切换确实生效（绑定未变）时写；失败不回滚视图（原片本身已恢复成功），
      // 但如实提示：刷新后将按服务端记录恢复上一次选中的切片。
      if (projectId?.startsWith("timeline_")) {
        const persistSeq = ++activeClipPersistSeq.current;
        void (async () => {
          try {
            const response = await fetch(`${studioBase}/studio/projects/${encodeURIComponent(projectId)}/active-clip`, {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ clip_id: null }),
            });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
          } catch {
            if (mountedRef.current && persistSeq === activeClipPersistSeq.current
                && workspaceProjectRef.current === projectId && !activeClipRef.current) {
              setMessage("已切回原片，但清除选中状态未保存：刷新后可能恢复上次选中的切片。");
            }
          }
        })();
      }
    } catch (error) {
      if (current()) { const text = error instanceof Error ? error.message : "返回原片失败，请重试。"; setSourceReturnError(text); setMessage(text); }
    } finally { if (current()) setSourceReturnBusy(false); }
  };
  // ===== 画面变换编辑与持久化（成片 → sidecar 端点；时间线片段 → 项目 EDL）=====
  const persistClipTransform = (clip: RenderedClip, transform: ClipTransform) => {
    if (clipTransformTimer.current) window.clearTimeout(clipTransformTimer.current);
    clipTransformTimer.current = window.setTimeout(() => {
      const payload = isDefaultTransform(transform) ? null : transformPayload(transform);
      void fetch(`${autoEditBaseUrl}/${clip.task_id}/clips/${clip.clip_id}/transform`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ transform: payload }),
      }).catch(() => { /* 保存失败不阻塞编辑，下次修改会带上最新值重试 */ });
    }, 700);
  };
  const persistRangeTransforms = (next: Record<number, ClipTransform>) => {
    const projectId = timeline?.id;
    if (!projectId?.startsWith("timeline_")) return;
    if (timelineSaveTimer.current) window.clearTimeout(timelineSaveTimer.current);
    timelineSaveTimer.current = window.setTimeout(() => {
      const ranges = (timeline?.timeline.ranges || []).map((range, index) => ({
        ...range,
        transform: next[index] ? transformPayload(next[index]) : null,
      }));
      void fetch(`${transcribeServiceBase}/studio/timeline/${projectId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ranges }),
      })
        .then((response) => (response.ok ? response.json() : null))
        .then((payload: { id: string; timeline: { ranges: TimelineRange[] } } | null) => {
          if (!payload?.timeline?.ranges) return;
          // 后端是事实来源：用响应 EDL（含已保存 transform）回写本地状态。
          setTimeline({ id: payload.id, timeline: payload.timeline, artifacts: { edl: "", srt: "", fcpxml: "" } });
          const restored: Record<number, ClipTransform> = {};
          payload.timeline.ranges.forEach((range, index) => { if (range.transform) restored[index] = parseTransform(range.transform); });
          setRangeTransforms(restored);
        })
        .catch(() => { /* 保存失败保留本地状态，下次修改重试 */ });
    }, 900);
  };
  // 把一组 transform 真正落盘（成片 → sidecar transform；时间线片段 → 项目 EDL）。
  // 全局画布快捷动作沿用此通道；Inspector 使用独立的显式 Apply 流程。
  const commitTransform = (next: ClipTransform) => {
    if (activeRenderedClip) {
      const key = clipTransformKey(activeRenderedClip);
      setClipTransforms((existing) => ({ ...existing, [key]: next }));
      persistClipTransform(activeRenderedClip, next);
      return;
    }
    const sourceRanges = timeline?.timeline.ranges || [];
    if (!sourceRanges.length) {
      // 尚未转写（还没有 EDL 时间线）：先按整段单一片段保存在内存；
      // 转写生成时间线后的下一次编辑会随 PUT /studio/timeline 一起落盘。
      setRangeTransforms((existing) => ({ ...existing, [0]: next }));
      return;
    }
    const hit = sourceRanges.findIndex((range) => currentTime >= range.source_start && currentTime < range.source_end);
    const index = hit >= 0 ? hit : 0;
    const merged = { ...rangeTransforms, [index]: next };
    setRangeTransforms(merged);
    persistRangeTransforms(merged);
  };
  const updateActiveTransform = (next: ClipTransform) => {
    // 裁切/重构图模式内：只改本地草稿，预览实时；持久化推迟到点「应用」。
    if (transformMode) {
      setTransformDraft(next);
      return;
    }
    commitTransform(next);
  };
  // 统一导出入口也读取服务端效果/音频，避免只有效果改动时漏过重渲。
  // 服务端对全默认配置直接返回原成片，画面变换仍使用同一套数学。
  const transformedExportUrl = (clip: RenderedClip) => {
    const transform = clipTransforms[clipTransformKey(clip)];
    return workflowClipExportUrl(autoEditBaseUrl, clip.task_id, clip.clip_id,
      transform ? transformPayload(transform) : undefined, transformBackground);
  };
  /**
   * 统一导出入口（2026-10-21）：在工作台窗口的内嵌预览（iframe）里，浏览器会静默丢弃
   * anchor 下载（沙箱没有 allow-downloads）：后端日志 200、下载栏却什么都没有。
   * 这种场景改成顶层新标签页打开导出链接，并用消息把可复制的绝对地址告诉用户。
   */
  const triggerArtifactDownload = (url: string) => {
    const outcome = downloadWorkflowArtifact(url);
    if (outcome === "downloaded") return;
    const absolute = /^https?:/i.test(url) ? url : `${window.location.origin}${url}`;
    setMessage(outcome === "opened_new_tab"
      ? `当前在工作台内嵌预览里，浏览器不允许预览页直接下载，已在新标签页打开导出链接：${absolute}（若新标签页仍被拦，请把这个地址复制到浏览器地址栏打开）`
      : `当前在工作台内嵌预览里，浏览器不允许预览页下载、也拦住了新标签页。请复制这个地址到浏览器地址栏打开：${absolute}`);
  };
  const downloadRenderedClip = () => {
    if (!activeRenderedClip) return;
    const url = clipExportUrl(activeClip ?? activeRenderedClip);
    triggerArtifactDownload(url);
  };
  // 服务端统一读取变换、效果与音频：全默认时直接返回原片，存在改动时才重渲。
  const clipExportUrl = (clip: RenderedClip) => (
    transformedExportUrl(clip)
  );
  // 导出封面：下载的必须是卡片 cover.path 指向的那个资产（重新生成/撤销后随之变化）。
  const clipCoverExportUrl = (clip: RenderedClip, card: ProjectClipCard | null | undefined) => {
    const path = coverPathOf(clip, card);
    return path ? artifactUrl(clip.task_id, path, true) : null;
  };
  const handleVideoMetadata = (event: SyntheticEvent<HTMLVideoElement>) => {
    setDuration(event.currentTarget.duration || 0);
    setVideoDimensions({
      width: event.currentTarget.videoWidth || 1080,
      height: event.currentTarget.videoHeight || 1920,
    });
  };
  const handleVideoLoadedData = (event: SyntheticEvent<HTMLVideoElement>) => {
    const video = event.currentTarget;
    // Some phone videos begin with a black decode frame. Move a fraction
    // forward so importing always gives the user a visible frame to inspect.
    if (video.currentTime < 0.04 && video.duration > 0.12) video.currentTime = 0.12;
    // 源成功解码出首帧 → 清除加载失败占位，并复位「同一 src 只提示一次」的记录：
    // 之后该 src 若再坏（被清理/中断）仍能重新提示。
    videoErrorSrcRef.current = null;
    setPreviewError(null);
  };

  // video 元素加载失败（error 事件）友好降级：成片源 404/JSON/损坏时 <video> 只会黑屏，
  // 这里把失败转成占位与可读提示（占位 UI 见 previewError 渲染处）。要点：
  //  * 不清空选中态、不回滚 previewUrl——改 src 只会再次触发 error 形成循环；
  //  * 同一 src 只提示一次（videoErrorSrcRef + shouldSurfaceVideoError，防重复触发）；
  //  * TransformStage 的 <video> 与本页共享 videoRef（组件不透传 onError），用原生监听接入。
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return undefined;
    const handleVideoError = () => {
      if (!shouldSurfaceVideoError(videoErrorSrcRef.current, previewUrl)) return;
      videoErrorSrcRef.current = previewUrl;
      setPreviewError(VIDEO_LOAD_FAILED_HINT);
      setMessage(VIDEO_LOAD_FAILED_HINT);
    };
    video.addEventListener("error", handleVideoError);
    return () => video.removeEventListener("error", handleVideoError);
  }, [previewUrl]);
  // 开始自动剪辑 = 提交一个**独立**的 auto-edit job（与转写 job 拆分）：
  // 请求体只带转写凭据（object_key/project_id/transcript_id/transcript_version），
  // 金句/时间线/成片由后端从转写产物自取。copilot 的 start_auto_edit 动作复用本函数。
  const startAutoEdit = async (): Promise<boolean> => {
    if (activeRenderedClip) {
      setMessage("请先在左侧选择原始视频，再开始新的自动剪辑任务。");
      return false;
    }
    if (!transcriptRef || !transcriptRef.object_key) {
      setMessage("请先完成转写与纠错。");
      return false;
    }
    const generation = workflowGenerationRef.current;
    setAutoEditing(true);
    setMessage("正在提交自动剪辑任务（选段、时间线、成片渲染在本机流水线执行）…");
    try {
      const jobResponse = await fetch(`${transcribeServiceBase}/api/jobs/auto-edit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          object_key: transcriptRef.object_key,
          project_id: transcriptRef.project_id,
          transcript_id: transcriptRef.transcript_id,
          transcript_version: transcriptRef.transcript_version,
        }),
      });
      const jobText = await jobResponse.text();
      const jobPayload = jobText ? JSON.parse(jobText) as { job_id?: string; detail?: string } : {};
      if (!jobResponse.ok || !jobPayload.job_id) throw new Error(jobPayload.detail || `自动剪辑任务提交失败（${jobResponse.status}）`);
      if (generation !== workflowGenerationRef.current) return false;
      setWorkflowJob(normalizeWorkflowJob({ status: "queued" }, jobPayload.job_id, "auto_edit"));
      setMessage("自动剪辑任务已提交，等待本机执行器处理…");
      const startedAt = Date.now();
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, 2500));
        const pollResponse = await fetch(`${transcribeServiceBase}/api/jobs/${jobPayload.job_id}`, { cache: "no-store" });
        const pollText = await pollResponse.text();
        const poll = pollText ? JSON.parse(pollText) as {
          status?: string; stage?: string; progress?: number; error?: string;
          waiting_for_executor?: boolean;
          artifacts?: { kind: string; clip_id?: string | null; url: string }[];
          detail?: string;
        } : {};
        if (generation !== workflowGenerationRef.current) return false;
        if (!pollResponse.ok) throw new Error(readableWorkflowError(poll.detail, "无法读取自动剪辑任务进度"));
        await observeWorkflowJob(poll, jobPayload.job_id, "auto_edit");
        if (generation !== workflowGenerationRef.current) return false;
        const status = poll.status || "queued";
        if (status === "queued" && poll.waiting_for_executor) {
          setMessage(`等待本机执行器（已等待 ${Math.round((Date.now() - startedAt) / 1000)} 秒）…`);
          continue;
        }
        setMessage(poll.stage || status);
        if (status === "failed" || status === "cancelled") throw new Error(poll.error || (status === "cancelled" ? "自动剪辑任务已取消" : "自动剪辑任务失败"));
        if (status === "analysis_ready") {
          // 第三步：AUTO_EDIT 先做模型内容分析并落盘即停（本阶段不渲染）。
          setAutoEditing(false);
          setTranscriptionState("idle");
          setMessage("内容分析完成：关键词、金句、主题块与候选区间已保存（本阶段未渲染成片，渲染将在后续阶段接入）。");
          return true;
        }
        if (status === "plan_ready") {
          // 第四步：内容分析 + 完整切片规划完成，clip_plan.json 已保存（本阶段不渲染）。
          setAutoEditing(false);
          setTranscriptionState("idle");
          setMessage("切片规划完成：clip_plan.json 已保存（本阶段未渲染成片，渲染将在后续阶段接入）。");
          return true;
        }
        if (status === "technical_failed") {
          setAutoEditing(false);
          throw new Error(poll.error || "全部切片均因技术原因渲染失败，请查看任务详情后重试。");
        }
        if (status === "completed" || status === "completed_with_warnings" || status === "partial_succeeded" || status === "partial_failed") {
          // 第五步 staged 链：从 clip_final + clip_plan artifacts 构建成片卡片；
          // 部分失败不隐藏成功切片。
          const stagedClips = await fetchStagedClips(poll, String(jobPayload.job_id ?? ""));
          if (generation !== workflowGenerationRef.current) return false;
          if (stagedClips.length) {
            setRenderedClips(stagedClips);
            setTranscriptionState("idle");
            setAutoEditing(false);
            const warned = stagedClips.filter((item) => (item.warnings?.length ?? 0) > 0).length;
            setMessage(`渲染完成：${stagedClips.length} 条成片已加入素材区`
              + ((status === "partial_succeeded" || status === "partial_failed") ? "（部分切片因技术故障未完成，已保留全部成功成片）" : "")
              + (warned ? `，${warned} 条带质量警告（仅提示，不影响播放与导出）` : "")
              + "。");
            return true;
          }
          const artifact = (poll.artifacts || []).find((item) => item.kind === "transcript_json");
          let segments: Segment[] = []; let jobInsights: StudioInsights | null = null;
          let jobTimeline: TimelineProject | null = null; let jobAutoEdit: AutoEditTask | null = null;
          if (artifact) {
            const transcriptResponse = await fetch(artifact.url.startsWith("/") ? `${transcribeServiceBase}${artifact.url}` : artifact.url, { cache: "no-store" });
            const transcript = await transcriptResponse.json() as { asr_segments?: Segment[]; insights?: StudioInsights; timeline?: TimelineProject; auto_edit_task?: AutoEditTask };
            segments = transcript.asr_segments || []; jobInsights = transcript.insights || null;
            jobTimeline = transcript.timeline || null; jobAutoEdit = transcript.auto_edit_task || null;
          }
          if (!jobAutoEdit) throw new Error("自动剪辑任务完成但缺少成片结果，请重试或联系管理员。");
          setSegments(segments); setInsights(jobInsights); setTranscriptionState("idle");
          setTimeline(jobTimeline); setAutoEditTask(jobAutoEdit);
          setAutoEditing(false);
          setMessage(`自动剪辑完成：${segments.length} 段时间戳已载入。${jobAutoEdit.clips?.length ? "成片已加入项目素材，可选择预览和导出。" : ""}`);
          return true;
        }
      }
    } catch (error) {
      if (generation !== workflowGenerationRef.current) return false;
      setAutoEditing(false);
      setMessage(error instanceof Error ? error.message : "自动剪辑任务失败，请重试。");
      return false;
    }
  };
  // 重新剪辑并替换旧切片（第五步扩展）：确认一次后全自动，无中间审核。
  const startReedit = async (): Promise<boolean> => {
    const projectId = workspaceProjectRef.current, epoch = workspaceEpoch.current;
    const credential = [reeditDialogState.credential, transcriptRef, projectTranscriptRef].find((item) => item?.project_id === projectId && item.object_key);
    if (reeditBusy) return false;
    if (!projectId || !credential?.object_key) {
      setReeditDialogState({ status: "missing" }); setReeditConfirmOpen(true);
      setMessage("当前项目缺少匹配的转写凭据，无法重新剪辑。"); return false;
    }
    const generation = workflowGenerationRef.current;
    const current = () => generation === workflowGenerationRef.current && epoch === workspaceEpoch.current && projectId === workspaceProjectRef.current;
    setReeditBusy(true);
    setCleanupRetryRun(null);
    setMessage("已提交重新剪辑任务：复用源视频与转写，新切片成功后替换旧切片…");
    try {
      const jobResponse = await fetch(`${transcribeServiceBase}/api/jobs/re-edit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          object_key: credential.object_key,
          project_id: projectId,
          transcript_id: credential.transcript_id,
          transcript_version: credential.transcript_version,
          reuse_transcript: true,
          force_new_run: true,
          replacement_mode: "delete_old_after_success",
        }),
      });
      const jobText = await jobResponse.text();
      const jobPayload = jobText ? JSON.parse(jobText) as { job_id?: string; detail?: string } : {};
      if (!jobResponse.ok || !jobPayload.job_id) throw new Error(readableWorkflowError(jobPayload.detail, `重新剪辑任务提交失败（${jobResponse.status}）`));
      if (!current()) return false;
      setWorkflowJob(normalizeWorkflowJob({ status: "queued" }, jobPayload.job_id, "reedit"));
      const startedAt = Date.now();
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, 2500));
        const pollResponse = await fetch(`${transcribeServiceBase}/api/jobs/${jobPayload.job_id}`, { cache: "no-store" });
        const pollText = await pollResponse.text();
        const poll = pollText ? JSON.parse(pollText) as {
          status?: string; stage?: string; progress?: number; error?: string;
          artifacts?: { kind: string; clip_id?: string | null; url: string }[];
          detail?: string;
        } : {};
        if (!current()) return false;
        if (!pollResponse.ok) throw new Error(readableWorkflowError(poll.detail, "无法读取重新剪辑任务进度"));
        await observeWorkflowJob(poll, jobPayload.job_id, "reedit");
        if (!current()) return false;
        const status = poll.status || "queued";
        setMessage(poll.stage || status);
        if (status === "failed" || status === "technical_failed" || status === "cancelled") {
          setReeditBusy(false);
          throw new Error(poll.error || (status === "cancelled" ? "重新剪辑任务已取消" : "重新剪辑任务失败；请查看服务端恢复操作。"));
        }
        if (status === "completed_with_cleanup_warning") {
          const stagedClips = await fetchStagedClips(poll, jobPayload.job_id);
          if (!current()) return false;
          if (stagedClips.length) setRenderedClips(stagedClips);
          if (credential.project_id) await loadProjectCards(credential.project_id);
          if (!current()) return false;
          setTranscriptionState("idle");
          setReeditBusy(false);
          setCleanupRetryRun(jobPayload.job_id);
          setMessage("新切片已生效；旧切片清理未完成，可重试清理（不会重新规划或渲染）。");
          return true;
        }
        if (status === "completed" || status === "completed_with_warnings" || status === "partial_succeeded" || status === "partial_failed") {
          const stagedClips = await fetchStagedClips(poll, jobPayload.job_id);
          if (!current()) return false;
          if (stagedClips.length) setRenderedClips(stagedClips);
          if (credential.project_id) await loadProjectCards(credential.project_id);
          if (!current()) return false;
          setTranscriptionState("idle");
          setReeditBusy(false);
          setMessage(`重新剪辑完成：素材区已只显示新切片`
            + ((status === "partial_succeeded" || status === "partial_failed") ? "（部分新切片未完成，已保留成功部分）" : "") + "。");
          return true;
        }
        if (Date.now() - startedAt > 3600_000) throw new Error("重新剪辑超时，请稍后重试。");
      }
    } catch (error) {
      if (!current()) return false;
      setReeditBusy(false);
      setMessage(error instanceof Error ? error.message : "重新剪辑任务失败，请重试。");
      return false;
    }
  };
  // 清理未完成的只重试清理：不重新规划、不重新渲染。
  const retryReeditCleanup = async (): Promise<void> => {
    const projectId = transcriptRef?.project_id || activeProjectId;
    if (!cleanupRetryRun || !projectId) return;
    setReeditBusy(true);
    try {
      const response = await fetch(`${transcribeServiceBase}/studio/projects/${encodeURIComponent(projectId)}/reedit/${encodeURIComponent(cleanupRetryRun)}/retry-cleanup`, { method: "POST" });
      const payload = await response.json().catch(() => null) as
        { clips_deleted?: number; bytes_freed?: number; detail?: { message?: string } | string } | null;
      if (!response.ok) {
        const detail = payload?.detail;
        throw new Error(typeof detail === "string" ? detail : detail?.message || "清理重试失败");
      }
      if (projectId) await loadProjectCards(projectId);
      setCleanupRetryRun(null);
      setMessage(`旧切片清理完成：删除 ${payload?.clips_deleted ?? 0} 条，释放 ${((payload?.bytes_freed ?? 0) / 1024 / 1024).toFixed(1)} MB。`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "清理重试失败");
    } finally {
      setReeditBusy(false);
    }
  };
  const rerenderAutoEdit = async () => {
    if (!autoEditTask || autoEditing) return;
    setAutoEditing(true);
    setMessage("正在按最新固化规则创建复渲染任务（整项目固化流水线，耗时数分钟）…");
    try {
      const response = await fetch(`${autoEditBaseUrl}/${autoEditTask.task_id}/rerender`, { method: "POST" });
      const text = await response.text();
      const payload = text ? JSON.parse(text) as AutoEditTask & { detail?: string } : null;
      if (!response.ok || !payload?.task_id) throw new Error(payload?.detail || "无法创建复渲染任务");
      setActiveRenderedClip(null);
      setAutoEditTask(payload);
      setMessage("已创建复渲染任务：整项目按固化流水线重跑，旧成片保留；只想改封面/标题/字幕/时间线请改用卡片菜单的局部任务。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "无法创建复渲染任务");
    } finally {
      setAutoEditing(false);
    }
  };
  // 单切片复渲染：只重做指定成片（整片后备链路；轻量封面通道见 replaceCover）。
  // style_id 透传：钉定该次复渲染任务的封面风格（后端写入任务 JSON，执行器传给
  // video-brain 的 TaskCreateRequest.style_id）。
  const rerenderClip = async (clip: RenderedClip, kind: "make_cover" | "render_clip", styleId?: CoverStyleId) => {
    setAutoEditing(true);
    setMessage(kind === "make_cover" ? `正在为${clip.name}创建封面制作任务…` : `正在为${clip.name}创建单切片渲染任务…`);
    try {
      const response = await fetch(`${autoEditBaseUrl}/${clip.task_id}/rerender`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clip_ids: [clip.clip_id], ...(styleId ? { style_id: styleId } : {}) }),
      });
      const text = await response.text();
      const payload = text ? JSON.parse(text) as AutoEditTask & { detail?: string } : null;
      if (!response.ok || !payload?.task_id) throw new Error(payload?.detail || "无法创建单切片渲染任务");
      setAutoEditTask(payload);
      setMessage(kind === "make_cover"
        ? "已创建封面制作任务：旧成片与封面保留，新封面随该切片重新渲染生成。"
        : "已创建单切片渲染任务：旧成片保留，新成片完成后加入项目素材。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "无法创建单切片渲染任务");
      throw error;
    } finally {
      setAutoEditing(false);
    }
  };
  // ===== 轻量封面通道（秒级，不整片重渲）=====
  // 「按风格重新生成封面」：只换封面图 + 0.8s 片头；失败时读 error.detail 给可读原因
  // （例如 cutout_v3 的 E_CUTOUT_WEIGHTS_MISSING 安装指引），绝不静默降级成别的风格。
  const replaceCover = async (clip: RenderedClip, styleId: CoverStyleId, replaceIntro = true) => {
    setCoverBusy(true);
    const styleName = COVER_STYLES.find((item) => item.id === styleId)?.name || styleId;
    setMessage(`正在按「${styleName}」重制${clip.name}的封面…`);
    try {
      const response = await fetch(`${autoEditBaseUrl}/${clip.task_id}/clips/${encodeURIComponent(clip.clip_id)}/cover`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ style_id: styleId, replace_intro: replaceIntro }),
      });
      const text = await response.text();
      const payload = text ? JSON.parse(text) as CoverMutationResult : null;
      if (!response.ok || !payload) {
        const detail = payload?.detail || `HTTP ${response.status}`;
        // 5xx / 服务不可达属于「轻量通道暂时不可用」→ 自动退回整片重渲（后备链路）。
        // 4xx 是明确的可读原因（非法风格、抠像权重缺失、成片不存在），保持失败不掩盖。
        if (response.status >= 500) {
          setMessage(`轻量封面通道不可用（${detail}），改走整片重渲后备链路…`);
          await rerenderClip(clip, "make_cover", styleId);
          return;
        }
        throw new Error(detail);
      }
      setCoverVersion((value) => value + 1);
      setCoverState(payload);
      setShowCoverFrame(true);
      setRenderedClips((existing) => existing.map((item) => (
        item.task_id === clip.task_id && item.clip_id === clip.clip_id
          ? { ...item, cover_path: payload.cover_path ?? item.cover_path }
          : item
      )));
      setActiveRenderedClip((current) => (current && current.task_id === clip.task_id && current.clip_id === clip.clip_id
        ? { ...current, cover_path: payload.cover_path ?? current.cover_path }
        : current));
      setMessage(payload.idempotent
        ? `${clip.name}的封面已是「${styleName}」，无需重复生成（产物未变）。`
        : `已按「${styleName}」重制${clip.name}的封面：封面图与 0.8s 片头都已替换，QC ${payload.qc?.passed ? "通过" : "见详情"}，可一键回滚。`);
      // 封面通道也走台账：卡片缩略图/版本徽标原地刷新（卡片数不变）。
      const projectId = activeProjectId;
      if (projectId?.startsWith("timeline_")) void loadProjectCards(projectId);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "封面重制失败");
      throw error;
    } finally {
      setCoverBusy(false);
    }
  };
  // 「沿用已生成的封面」：复用来源成片现成的 cover.jpg，不调用生成链路；默认不动片头。
  const adoptCover = async (clip: RenderedClip, sourceClipId: string, replaceIntro = false) => {
    setCoverBusy(true);
    const source = projectCovers.find((item) => item.clip_id === sourceClipId);
    const sourceName = source?.name || sourceClipId;
    setMessage(`正在把「${sourceName}」已生成的封面套用到${clip.name}…`);
    try {
      const response = await fetch(`${autoEditBaseUrl}/${clip.task_id}/clips/${encodeURIComponent(clip.clip_id)}/cover/adopt`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_task_id: clip.task_id, source_clip_id: sourceClipId, replace_intro: replaceIntro }),
      });
      const text = await response.text();
      const payload = text ? JSON.parse(text) as CoverMutationResult : null;
      if (!response.ok || !payload) throw new Error(payload?.detail || `HTTP ${response.status}`);
      setCoverVersion((value) => value + 1);
      setCoverState(payload);
      setShowCoverFrame(true);
      setRenderedClips((existing) => existing.map((item) => (
        item.task_id === clip.task_id && item.clip_id === clip.clip_id
          ? { ...item, cover_path: payload.cover_path ?? item.cover_path }
          : item
      )));
      setActiveRenderedClip((current) => (current && current.task_id === clip.task_id && current.clip_id === clip.clip_id
        ? { ...current, cover_path: payload.cover_path ?? current.cover_path }
        : current));
      setMessage(payload.idempotent
        ? `${clip.name}当前封面已经沿用自「${sourceName}」，无需重复套用。`
        : `已把「${sourceName}」已生成的封面套用到${clip.name}${replaceIntro ? "（同时替换了 0.8s 片头）" : "（未改片头，成片画面保持原样）"}，可一键回滚。`);
      const projectId = activeProjectId;
      if (projectId?.startsWith("timeline_")) void loadProjectCards(projectId);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "沿用封面失败");
      throw error;
    } finally {
      setCoverBusy(false);
    }
  };
  // 刷新封面台账（当前来源 / 可否回滚）与项目内可选来源列表。
  const refreshCoverState = async (clip: RenderedClip) => {
    try {
      const [stateResponse, listResponse] = await Promise.all([
        fetch(`${autoEditBaseUrl}/${clip.task_id}/clips/${encodeURIComponent(clip.clip_id)}/cover`, { cache: "no-store" }),
        fetch(`${autoEditBaseUrl}/${clip.task_id}/covers`, { cache: "no-store" }),
      ]);
      if (stateResponse.ok) setCoverState(await stateResponse.json() as ClipCoverState);
      if (listResponse.ok) {
        const payload = await listResponse.json() as { clips?: ProjectCoverEntry[] };
        setProjectCovers((payload.clips || []).filter((item) => item.has_cover));
      }
    } catch {
      /* 台账读取失败不影响播放与导出：画布封面视图按「无封面」回退。 */
    }
  };
  // ===== 左侧成片卡片的操作（播放/导出/局部任务/历史/移除）=====
  // 播放：选中该卡片并开始播放（选中会把画布切到该成片，若已在播则不打断）。
  const playRenderedClip = async (clip: RenderedClip) => {
    if (activeRenderedClip?.clip_id !== clip.clip_id) selectRenderedClip(clip);
    const video = videoRef.current;
    if (!video) return;
    try {
      await video.play();
    } catch {
      /* 浏览器可能拒绝自动播放；素材已经载入，用户可手动点播放。 */
    }
  };
  // 「重新制作封面」：类型化局部任务 REGENERATE_COVER（当前选中风格），只换封面与 0.8s 片头。
  const regenerateCoverForClip = async (clip: RenderedClip, styleId: CoverStyleId, instruction?: string) => {
    const projectId = activeProjectId;
    if (!projectId?.startsWith("timeline_")) throw new Error("该项目没有台账（project_id 必须是 timeline_*），无法创建局部封面任务");
    const styleName = COVER_STYLES.find((item) => item.id === styleId)?.name || styleId;
    setMessage(`正在按「${styleName}」重新制作${clip.name}的封面（局部任务，不重剪正片）…`);
    const status = await runLocalClipTask(projectId, clip, {
      task_type: "REGENERATE_COVER",
      style_id: styleId,
      replace_intro: true,
      ...(instruction ? { instruction } : {}),
    });
    if (status.status === "failed") throw new Error(status.error || "封面重制任务失败");
    const result = status.result;
    const revision = result?.cover_revision;
    const copyReason = result?.copy_report?.reason ? ` 封面文案：${result.copy_report.reason}` : "";
    setMessage(`封面已更新 · v${revision ?? "?"}（风格 ${styleName}，正片画面未变）${copyReason}`);
    setCoverVersion((value) => value + 1);
    void refreshCoverHistory(clip);
    return result;
  };
  // 封面版本管理（2026-10-13）：Inspector 封面页的「重新制作封面 + 修改要求」。
  const [coverTaskFailure, setCoverTaskFailure] = useState<{ message: string; instruction: string } | null>(null);
  const regenerateCoverWithInstruction = async (instruction: string) => {
    const clip = activeRenderedClip;
    if (!clip) throw new Error("先在左侧选中一条成片，再重制封面");
    try {
      await regenerateCoverForClip(clip, coverStyle, instruction.trim() || undefined);
      if (mountedRef.current) setCoverTaskFailure(null);
    } catch (error) {
      if (mountedRef.current) {
        setCoverTaskFailure({ message: error instanceof Error ? error.message : "封面重制失败", instruction });
      }
    }
  };
  const retryCoverTask = () => {
    const failure = coverTaskFailure;
    if (failure) void regenerateCoverWithInstruction(failure.instruction);
  };
  // 「设为当前封面」：历史里任意一版原地生效（POST …/cover/activate），不新增卡片。
  const activateCoverRevision = async (revision: number) => {
    const clip = activeRenderedClip;
    const projectId = activeProjectId;
    if (!clip || !projectId?.startsWith("timeline_")) return;
    setLocalTaskBusy(true);
    try {
      const response = await fetch(
        `${studioBase}/studio/projects/${encodeURIComponent(projectId)}/clips/${encodeURIComponent(clip.clip_id)}/cover/activate`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ revision }) },
      );
      const text = await response.text();
      const payload = text ? JSON.parse(text) as { card?: ProjectClipCard; detail?: string } | null : null;
      if (!response.ok || !payload) throw new Error(payload?.detail || `设为当前封面失败（HTTP ${response.status}）`);
      if (payload.card && mountedRef.current) applyProjectCard(payload.card);
      setCoverVersion((value) => value + 1);
      setMessage(`${clip.name}的封面已切换到 v${revision}（原位生效，不新增卡片）`);
      void refreshCoverHistory(clip);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "设为当前封面失败");
    } finally {
      if (mountedRef.current) setLocalTaskBusy(false);
    }
  };
  // 「删除版本」：仅删除未被当前使用的历史封面（v1 基线后端也会拒绝）。
  const deleteCoverRevision = async (revision: number) => {
    const clip = activeRenderedClip;
    const projectId = activeProjectId;
    if (!clip || !projectId?.startsWith("timeline_")) return;
    if (typeof window !== "undefined" && !window.confirm(`删除「${clip.name}」的封面 v${revision}？该版本的历史记录与快照文件会一并移除，且不可恢复。`)) return;
    try {
      const response = await fetch(
        `${studioBase}/studio/projects/${encodeURIComponent(projectId)}/clips/${encodeURIComponent(clip.clip_id)}/cover/revisions/${revision}`,
        { method: "DELETE" },
      );
      const text = await response.text();
      const payload = text ? JSON.parse(text) as { detail?: string } | null : null;
      if (!response.ok || !payload) throw new Error(payload?.detail || `删除封面版本失败（HTTP ${response.status}）`);
      setMessage(`${clip.name}的封面 v${revision} 已删除。`);
      void refreshCoverHistory(clip);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "删除封面版本失败");
    }
  };
  // 「编辑标题」：先 prompt 新标题，再创建 UPDATE_TITLE 局部任务（不重剪视频）。
  const promptClipTitle = (clip: RenderedClip): string | null => {
    if (typeof window === "undefined") return null;
    const card = projectCardsRef.current.find((item) => item.clip_id === clip.clip_id);
    const current = card?.title || card?.topic || clip.topic || "";
    const answer = window.prompt(`「${clip.name}」的新标题（留空则按固化规则自动重排）：`, current);
    if (answer === null) return null;
    return answer.trim();
  };
  const updateClipTitle = async (clip: RenderedClip, title: string | null) => {
    const projectId = activeProjectId;
    if (!projectId?.startsWith("timeline_")) throw new Error("该项目没有台账（project_id 必须是 timeline_*），无法创建局部标题任务");
    setMessage(`正在更新${clip.name}的标题（局部任务，不重剪视频）…`);
    const status = await runLocalClipTask(projectId, clip, {
      task_type: "UPDATE_TITLE",
      ...(title ? { title } : {}),
    });
    return status.result;
  };
  // 封面历史：选中成片变化即加载（版本管理页常显），也供「查看历史」手动刷新。
  const refreshCoverHistory = async (clip: RenderedClip) => {
    const projectId = activeProjectId;
    if (!projectId?.startsWith("timeline_")) { setCoverHistory([]); return; }
    setCoverHistoryClipId(clip.clip_id);
    setCoverHistoryLoading(true);
    try {
      const response = await fetch(
        `${studioBase}/studio/projects/${encodeURIComponent(projectId)}/clips/${encodeURIComponent(clip.clip_id)}/cover-history`,
        { cache: "no-store" },
      );
      const text = await response.text();
      const payload = text ? JSON.parse(text) as ClipCoverHistoryPayload : null;
      if (!response.ok || !payload) throw new Error(payload?.detail || `无法读取封面历史（HTTP ${response.status}）`);
      if (mountedRef.current) setCoverHistory(payload.revisions || []);
    } catch (error) {
      if (mountedRef.current) {
        setCoverHistory([]);
        setMessage(error instanceof Error ? error.message : "无法读取封面历史");
      }
    } finally {
      if (mountedRef.current) setCoverHistoryLoading(false);
    }
  };
  useEffect(() => {
    if (activeRenderedClip) void refreshCoverHistory(activeRenderedClip);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRenderedClip?.clip_id, activeProjectId]);
  // 「查看历史」：端点 6 的封面版本列表（内联面板展示 v{n} · kind · created_at）。
  const toggleCoverHistory = async (clip: RenderedClip) => {
    if (coverHistoryClipId === clip.clip_id) { setCoverHistoryClipId(null); return; }
    setCoverHistoryClipId(clip.clip_id);
    setCoverHistory([]);
    const projectId = activeProjectId;
    if (!projectId?.startsWith("timeline_")) {
      setCoverHistory([]);
      return;
    }
    setCoverHistoryLoading(true);
    try {
      const response = await fetch(
        `${studioBase}/studio/projects/${encodeURIComponent(projectId)}/clips/${encodeURIComponent(clip.clip_id)}/cover-history`,
        { cache: "no-store" },
      );
      const text = await response.text();
      const payload = text ? JSON.parse(text) as ClipCoverHistoryPayload : null;
      if (!response.ok || !payload) throw new Error(payload?.detail || `无法读取封面历史（HTTP ${response.status}）`);
      if (mountedRef.current) setCoverHistory(payload.revisions || []);
    } catch (error) {
      if (mountedRef.current) {
        setCoverHistory([]);
        setMessage(error instanceof Error ? error.message : "无法读取封面历史");
      }
    } finally {
      if (mountedRef.current) setCoverHistoryLoading(false);
    }
  };
  // 「撤销到 v1」：端点 5，原地生效；只刷新这一张卡片。
  const undoClipCover = async (clip: RenderedClip) => {
    // 项目级操作：必须用记住的项目 id（timeline 选中成片后指向成片局部时间线）。
    const projectId = activeProjectId;
    if (!projectId?.startsWith("timeline_")) {
      setMessage("该项目没有台账（project_id 必须是 timeline_*），无法撤销封面。");
      return;
    }
    const card = projectCardsRef.current.find((item) => item.clip_id === clip.clip_id);
    const revision = card?.cover?.undo_revision ?? 1;
    setLocalTaskBusy(true);
    setMessage(`正在把${clip.name}的封面撤销到 v${revision}…`);
    try {
      const response = await fetch(
        `${studioBase}/studio/projects/${encodeURIComponent(projectId)}/clips/${encodeURIComponent(clip.clip_id)}/cover/undo`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ revision }) },
      );
      const text = await response.text();
      const payload = text ? JSON.parse(text) as { card?: ProjectClipCard; detail?: string; cover_revision?: number } : null;
      if (!response.ok || !payload) throw new Error(payload?.detail || `撤销失败（HTTP ${response.status}）`);
      if (payload.card && mountedRef.current) applyProjectCard(payload.card);
      setMessage(`${clip.name}的封面已撤销到 v${revision}，成片画面与封面文字同步回滚。`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "撤销封面失败");
      throw error;
    } finally {
      if (mountedRef.current) setLocalTaskBusy(false);
    }
  };
  // 「删除」：没有删除 API，只从本地视图移除，并在确认文案里写清楚。
  const removeCardFromView = (clip: RenderedClip) => {
    if (typeof window !== "undefined" && !window.confirm(`从当前视图移除「${clip.name}」？（仅从视图移除，不会删除服务器上的成片与封面文件；刷新或重开项目后会重新出现。）`)) return;
    setProjectCards((existing) => existing.filter((item) => item.clip_id !== clip.clip_id));
    setRenderedClips((existing) => existing.filter((item) => item.clip_id !== clip.clip_id));
    if (activeRenderedClip?.clip_id === clip.clip_id) setActiveRenderedClip(null);
    if (coverHistoryClipId === clip.clip_id) setCoverHistoryClipId(null);
    setMessage(`已从当前视图移除${clip.name}（仅从视图移除，服务器文件未删除）。`);
  };
  useEffect(() => {
    const taskId = autoEditTask?.task_id;
    if (!taskId) return undefined;
    if (autoEditTask.status === "completed" || autoEditTask.status === "failed") {
      setAutoEditing(false);
      return undefined;
    }
    let active = true;
    const poll = async () => {
      try {
        const response = await fetch(`${autoEditBaseUrl}/${taskId}`, { cache: "no-store" });
        const text = await response.text();
        const payload = text ? JSON.parse(text) as AutoEditTask & { detail?: string } : null;
        if (!response.ok || !payload?.task_id) throw new Error(payload?.detail || "无法读取自动剪辑进度");
        if (!active) return;
        setAutoEditTask(payload);
        setAutoEditing(payload.status === "queued" || payload.status === "running");
        if (payload.status === "completed") setMessage("自动剪辑已完成，成片、字幕和封面已回传。");
        if (payload.status === "failed") setMessage(payload.error || "自动剪辑失败，请查看任务状态。");
      } catch (error) {
        if (active) setMessage(error instanceof Error ? error.message : "无法读取自动剪辑进度。");
      }
    };
    void poll();
    const timer = window.setInterval(() => { void poll(); }, 2500);
    return () => { active = false; window.clearInterval(timer); };
  }, [autoEditBaseUrl, autoEditTask?.status, autoEditTask?.task_id]);
  useEffect(() => {
    if (!autoEditTask?.clips?.length) return;
    setRenderedClips((existing) => {
      const next = new Map(existing.map((clip) => [`${clip.task_id}:${clip.clip_id}`, clip]));
      autoEditTask.clips?.forEach((clip) => next.set(`${clip.task_id}:${clip.clip_id}`, clip));
      return [...next.values()];
    });
    // sidecar/任务 JSON 里已保存的成片变换随轮询并入本地状态（刷新恢复）。
    setClipTransforms((existing) => {
      const next = { ...existing };
      autoEditTask.clips?.forEach((clip) => {
        const key = clipTransformKey(clip);
        if (clip.transform && !next[key]) next[key] = parseTransform(clip.transform);
      });
      return next;
    });
  }, [autoEditTask?.clips]);
  // ===== 项目台账卡片：一条 clip 一张卡（局部任务只原地刷新，绝不新增卡片）=====
  // 卡片数可核对：端点上报 card_count，前端按键去重后得到 projectClipList.length。
  const cardCount = projectCardCount || projectCards.length;
  const projectClipList = useMemo(() => {
    const previous = new Map(renderedClips.map((clip) => [clip.clip_id, clip]));
    return dedupeByClipId(projectCards.map((card) => renderedClipFromCard(card, previous.get(card.clip_id))));
  }, [projectCards, renderedClips]);
  // 去重合并：台账已加载时以台账为事实源，再并入台账尚未登记的旧任务成片（按 clip_id 去重）。
  const renderedClipList = useMemo(
    () => (projectCardsLoaded ? dedupeByClipId([...projectClipList, ...renderedClips]) : renderedClips),
    [projectCardsLoaded, projectClipList, renderedClips],
  );
  const activeCard = useMemo(
    () => projectCards.find((card) => card.clip_id === activeRenderedClip?.clip_id) ?? null,
    [projectCards, activeRenderedClip?.clip_id],
  );
  // 「导出记录 / 任务历史」用的台账卡片（规则信息替代位置 #3）：
  // 优先后看到的成片（点「查看本次规则」时 workflowSnapshotClipId 就是它），
  // 没选成片时退回当前选中卡片的台账。都不存在时显示「台账未提供」，不编造。
  const ruleHistoryCard = useMemo(() => {
    const bySnapshot = workflowSnapshotClipId
      ? projectCards.find((card) => card.clip_id === workflowSnapshotClipId)
      : null;
    return bySnapshot ?? activeCard ?? null;
  }, [workflowSnapshotClipId, projectCards, activeCard]);
  // 播放器/封面视图的素材项：卡片刷新后立即带动画面与封面版本号。
  const activeClip = useMemo(() => {
    if (!activeRenderedClip) return null;
    const upgraded = renderedClipList.find((clip) => clip.clip_id === activeRenderedClip.clip_id);
    return upgraded && upgraded !== activeRenderedClip ? upgraded : activeRenderedClip;
  }, [activeRenderedClip, renderedClipList]);
  // 应用封面/重渲后成片文件被原地替换：同一 URL 会被浏览器缓存成旧画面。
  // 版本号变化 → 用带新 v 的 URL 重新指向同一 artifact，强制播放器加载新成片（并从头播放片头）。
  useEffect(() => {
    if (!activeRenderedClip) return;
    const clip = activeClip ?? activeRenderedClip;
    if (!clip?.video_path || clip.video_path.startsWith("http") || clip.video_path.startsWith("/")) return;
    setPreviewUrl(clipFileUrl(clip));
    setCurrentTime(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clipVideoVersion]);
  const autoEditCompleted = autoEditTask?.status === "completed";
  // ===== Inspector 接线派生值 =====
  // 底部时间线字幕轨的数据源：Inspector 编辑过（或已拉到字幕版本）就用那一份，
  // 否则保持既有行为（直接用 segments）——不新增第二条重复字幕轨。
  const inspectorCues = useMemo(
    () => inspectorSubtitles?.cues?.length ? inspectorSubtitles.cues : null,
    [inspectorSubtitles],
  );
  // 字幕叠加层：只有确实有 cue 且已载入素材时渲染（否则不占位、不遮挡）。
  const subtitleOverlayEnabled = Boolean(previewUrl && inspectorCues?.length && inspectorSubtitles?.config);
  const captionsHidden = inspectorTab !== "transcript";
  // Inspector 绑定（契约 §1）：选中成片 → clip_id；未选成片但已载入素材 → 哨兵 "source"。
  const inspectorClipId = activeRenderedClip ? activeRenderedClip.clip_id : (file || remoteSource?.projectId === activeProjectId ? "source" : null);
  // 播放器预览：Inspector「音频」标签的改动立刻作用到 <video>（观看侧实时），
  // 但不上传成片（配置保存由面板负责，导出渲染属 Phase 2）。
  useEffect(() => {
    if (!inspectorVoiceConfig) return;
    setPreviewVolume(Math.max(0, Math.min(1, inspectorVoiceConfig.voice_volume)));
    setPreviewMuted(Boolean(inspectorVoiceConfig.muted) || inspectorVoiceConfig.voice_volume <= 0);
  }, [inspectorVoiceConfig]);

  /**
   * 把一张台账卡片原地写回本地状态：只替换同 clip_id 的那一条，卡片总数不变。
   * 返回是否命中已有卡片（未命中说明是新登记的 clip，需要拉一次完整台账）。
   */
  const applyProjectCard = (card: ProjectClipCard): boolean => {
    let hit = false;
    setProjectCards((existing) => {
      hit = existing.some((item) => item.clip_id === card.clip_id);
      const merged = existing.map((item) => (item.clip_id === card.clip_id ? card : item));
      return dedupeByClipId(hit ? merged : [...merged, card]);
    });
    setProjectCardsLoaded(true);
    // 回退列表里的同 clip 条目也原地刷新（转写分段与画面变换保留本地值）。
    setRenderedClips((existing) => {
      const previous = existing.find((item) => item.clip_id === card.clip_id);
      if (!previous) return existing;
      const updated = renderedClipFromCard(card, previous);
      return dedupeByClipId(existing.map((item) => (item.clip_id === card.clip_id
        ? { ...item, ...updated, segments: item.segments, transform: item.transform }
        : item)));
    });
    setCoverVersion((value) => value + 1);
    if (activeClipRef.current?.clip_id === card.clip_id) {
      setCoverState(clipCoverStateFromCard(card));
      setShowCoverFrame(Boolean(card.cover?.path));
    }
    return hit;
  };
  // ===== 封面版型库（2026-10-21 阶段）=====
  /**
   * 拉取版本化注册表（GET /studio/cover-templates），挂载即取一次；载荷形状非法
   * → 置 error（版型卡绝不拿半份数据渲染）。
   */
  const loadCoverTemplates = useCallback(async (): Promise<boolean> => {
    setCoverTemplatesState({ loading: true, error: null });
    try {
      const response = await fetch(`${studioBase}/studio/cover-templates`, { cache: "no-store" });
      const text = await response.text();
      const payload = text ? JSON.parse(text) : null;
      if (!response.ok) throw new Error(readableWorkflowError(payload?.detail, `版型库读取失败（${response.status}）`));
      const library = normalizeCoverTemplateLibrary(payload);
      if (!library) throw new Error("版型库载荷形状非法");
      setCoverTemplateLibrary(library);
      setCoverTemplatesState({ loading: false, error: null });
      return true;
    } catch (error) {
      setCoverTemplatesState({ loading: false, error: error instanceof Error ? error.message : "版型库读取失败" });
      return false;
    }
  }, [studioBase]);
  useEffect(() => { void loadCoverTemplates(); }, [loadCoverTemplates]);
  /** 台账卡片里 cover_template 三元组的本地乐观改写（只动目标 clip 一条）。 */
  const patchCardCoverTemplate = (clipId: string, selection: ReturnType<typeof coverTemplateSelection> | null) => {
    setProjectCards((existing) => existing.map((card) => (
      card.clip_id === clipId ? { ...card, cover_template: selection } : card)));
  };
  /**
   * 选择封面版型：事实源是台账卡片（cover_template 三元组）——乐观改写 + PUT 持久化，
   * 失败回滚保留原选择。选择随卡片走：切换切片自动跟随、刷新自动恢复（不串片）。
   */
  const selectCoverTemplate = (templateId: string) => {
    const pid = activeProjectId;
    const clip = activeRenderedClip;
    if (!pid?.startsWith("timeline_") || !clip) return;
    if (!findCoverTemplate(coverTemplateLibrary, templateId)) return; // 注册表没有的版型绝不进 UI 状态
    const previous = activeCard?.cover_template ?? null;
    const registryVersion = coverTemplateLibrary?.registry_version || "";
    const seq = ++coverTemplateSelectSeq.current;
    patchCardCoverTemplate(clip.clip_id, coverTemplateSelection(templateId, registryVersion));
    void (async () => {
      try {
        const response = await fetch(`${studioBase}/studio/projects/${encodeURIComponent(pid)}/clips/${encodeURIComponent(clip.clip_id)}/cover-template`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ template_id: templateId }),
        });
        if (!response.ok) {
          const text = await response.text();
          let detail: unknown = text;
          try { detail = text ? JSON.parse(text) : null; } catch { /* 原样作为文本报错 */ }
          throw new Error(readableWorkflowError(detail, `版型选择保存失败（HTTP ${response.status}）`));
        }
      } catch (error) {
        if (!mountedRef.current || seq !== coverTemplateSelectSeq.current) return;
        if (workspaceProjectRef.current !== pid) return;
        patchCardCoverTemplate(clip.clip_id, previous ?? null);
        setMessage(error instanceof Error ? `${error.message}，已保留原版型选择。` : "版型选择保存失败，已保留原版型选择。");
      }
    })();
  };
  /**
   * 「使用××版制作封面」——真实渲染（路线 A）：封面轻量通道
   * POST /studio/auto-edit/{task}/clips/{clip}/cover，body 带 template_id；
   * 后端按版本化注册表的版面契约在**切片真实帧**上排版（PIL，无模型调用），
   * 产物走既有备份/回滚/revision 链。结果按点击时的 clip_id 持有，
   * 展示按当前切片过滤：切换切片不串片。
   */
  const generateCoverTemplate = async () => {
    const clip = activeRenderedClip;
    const templateId = selectedCoverTemplateId(activeCard);
    const template = findCoverTemplate(coverTemplateLibrary, templateId);
    const pid = activeProjectId;
    if (!clip || !template || !pid?.startsWith("timeline_")) {
      // 不选切片（或未选版型）不能生成——按钮在 UI 层已挡住，这里兜底。
      setMessage("先选中切片并挑选一个封面版型，再制作封面。");
      return;
    }
    setCoverTemplateGenerateBusy(template.template_id);
    try {
      const response = await fetch(`${autoEditBaseUrl}/${clip.task_id}/clips/${encodeURIComponent(clip.clip_id)}/cover`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ template_id: template.template_id, replace_intro: false }),
      });
      const text = await response.text();
      const payload = text ? JSON.parse(text) as CoverMutationResult : null;
      if (!response.ok || !payload) throw new Error(payload?.detail || `版型封面生成失败（HTTP ${response.status}）`);
      setCoverState(payload);
      setShowCoverFrame(true);
      setCoverVersion((value) => value + 1);
      setRenderedClips((existing) => existing.map((item) => (
        item.clip_id === clip.clip_id ? { ...item, cover_path: payload.cover_path ?? item.cover_path } : item)));
      const revision = payload.cover_revision;
      setMessage(payload.idempotent
        ? `${clip.name} 的封面已是「${template.name}」版型，无需重复生成。`
        : `已按「${template.name}」版型生成封面${revision ? ` v${revision}` : ""}（待应用：点击上方封面可应用或删除）。`);
      if (pid.startsWith("timeline_")) void loadProjectCards(pid);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "版型封面生成失败");
    } finally {
      setCoverTemplateGenerateBusy(null);
    }
  };
  /**
   * 应用：为当前切片创建**真实的渲染任务**（APPLY_COVER），把待应用封面烧成片头、
   * 生成更新后的成片。后端按 project/clip 归属校验 + 幂等（重复点击复用同一任务），
   * 前端轮询进度；失败保留原成片并展示原因（绝不假报成功）。
   */
  const applyCoverTemplate = async () => {
    const clip = activeRenderedClip;
    const pid = activeProjectId;
    if (!clip || !pid?.startsWith("timeline_")) return;
    setCoverTemplateActionBusy(true);
    setCoverApplyProgress({ stage: "正在创建应用任务…", percent: 0 });
    try {
      const status = await runLocalClipTask(pid, clip, {
        task_type: "APPLY_COVER",
        timeoutMs: APPLY_COVER_POLL_TIMEOUT_MS,
        onProgress: (payload) => setCoverApplyProgress({
          stage: String(payload.stage || "正在渲染中"),
          percent: Math.round(Number(payload.progress ?? 0) * 100),
        }),
      });
      const result = status.result;
      if (!result?.applied) throw new Error(status.error || "应用任务未返回已应用状态");
      setCoverVersion((value) => value + 1);
      if (!result.idempotent) setClipVideoVersion((value) => value + 1);
      // 应用后要看的是**新成片**（开头 0.8s 就是这张封面）：
      // 生成封面时留下的「封面首帧预览层」必须退掉，否则播放时画面一直停在封面。
      setShowCoverFrame(false);
      setMessage(result.idempotent
        ? `封面早已应用到${clip.name}，本次未重复渲染成片。`
        : `封面已应用到${clip.name}：${result.intro_duration_s ?? 0.8}s 片头已加入成片，正片音频与字幕从片头之后开始。`);
      void loadProjectCards(pid);
      void refreshCoverState(clip);
    } catch (error) {
      // 失败：原成片保持不变（后端渲染用原子替换），这里只把真实原因透出。
      setMessage(`${error instanceof Error ? error.message : "应用封面失败"}（原成片保持不变）`);
      void refreshCoverState(clip);
    } finally {
      setCoverTemplateActionBusy(false);
      setCoverApplyProgress(null);
    }
  };
  /** 删除：只清这份未应用的版型封面素材及其后端文件（切片/成片/其它数据不动）。 */
  const deleteCoverTemplate = async () => {
    const clip = activeRenderedClip;
    const pid = activeProjectId;
    if (!clip || !pid?.startsWith("timeline_")) return;
    setCoverTemplateActionBusy(true);
    try {
      const response = await fetch(`${autoEditBaseUrl}/${clip.task_id}/clips/${encodeURIComponent(clip.clip_id)}/cover-template/delete`, { method: "POST" });
      const text = await response.text();
      const payload = text ? JSON.parse(text) as { deleted_revision?: number; detail?: string } : null;
      if (!response.ok || payload?.deleted_revision === undefined) throw new Error(payload?.detail || `删除失败（HTTP ${response.status}）`);
      setCoverVersion((value) => value + 1);
      setMessage(`已删除未应用的版型封面草稿 v${payload.deleted_revision}（切片与成片不受影响）。`);
      void loadProjectCards(pid);
      void refreshCoverState(clip);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "删除封面草稿失败。");
    } finally {
      setCoverTemplateActionBusy(false);
    }
  };
  /** 上方图片区的封面 URL：当前 cover.jpg（含缓存版本号）。 */
  const coverImageUrlValue = useMemo(() => (
    activeClip && coverPathOf(activeClip, activeCard) ? `${artifactUrl(activeClip.task_id, coverPathOf(activeClip, activeCard) as string)}&v=${coverVersion}` : null
  ), [activeClip, activeCard, coverVersion, artifactUrl]);
  /**
   * 拉取项目台账（端点 1）。projectId 必须是 timeline_* 才调用；失败则不置 loaded，
   * 左侧列表回退到既有 renderedClips 行为。
   */
  const loadProjectCards = useCallback(async (projectId: string): Promise<boolean> => {
    if (!projectId.startsWith("timeline_")) return false;
    const epoch = workspaceEpoch.current;
    try {
      const response = await fetch(`${studioBase}/studio/projects/${encodeURIComponent(projectId)}/clips`, { cache: "no-store" });
      const text = await response.text();
      const payload = text ? JSON.parse(text) as ProjectClipsPayload : null;
      if (!response.ok || !payload?.cards || (payload.project_id && payload.project_id !== projectId) || epoch !== workspaceEpoch.current || workspaceProjectRef.current !== projectId || !mountedRef.current) return false;
      const cards = payload.cards;
      const byClipId = new Map<string, RenderedClip>();
      // 台账卡片 → 素材项（继承本地已有的转写分段与画面变换），再补上台账没登记的旧成片。
      cards.forEach((card) => {
        const previous = renderedClipsRef.current.find((clip) => clip.clip_id === card.clip_id) || null;
        byClipId.set(card.clip_id, renderedClipFromCard(card, previous));
      });
      renderedClipsRef.current.forEach((clip) => {
        if (!byClipId.has(clip.clip_id)) byClipId.set(clip.clip_id, clip);
      });
      setRenderedClips([...byClipId.values()]);
      setProjectCards(dedupeByClipId(cards));
      setProjectCardCount(typeof payload.card_count === "number" ? payload.card_count : cards.length);
      // 切片选择（2026-10-21 阶段）：记住服务端记录的唯一 active_clip_id，恢复 effect 据此工作。
      setServerActiveClip({ project_id: projectId,
        clip_id: typeof payload.active_clip_id === "string" && payload.active_clip_id ? payload.active_clip_id : null });
      setProjectCardsLoaded(true);
      return true;
    } catch {
      // 端点不可达或响应非法：保持既有卡片（回退链路），不打断播放与导出。
      return false;
    }
  }, [studioBase]);
  // 2026-10-16：显式打开历史项目时拉取项目时间线（替代已删除的会话恢复链）。
  // 只在用户主动打开（「打开历史视频」/ ?source_id=）时调用；失败静默，卡片仍可播。
  const loadProjectTimeline = useCallback(async (projectId: string): Promise<boolean> => {
    const request = sourceReturnSeq.current;
    try {
      const response = await fetch(`${transcribeServiceBase}/studio/timeline/${encodeURIComponent(projectId)}`, { cache: "no-store" });
      const payload = response.ok
        ? await response.json() as { id: string; timeline: { ranges: TimelineRange[] }; transcript?: Segment[] } : null;
      if (request !== sourceReturnSeq.current || !payload?.timeline?.ranges?.length || payload.id !== projectId || workspaceProjectRef.current !== projectId || !mountedRef.current || workspaceBindingRef.current !== `${projectId}:source`) return false;
      setTimeline({ id: payload.id, timeline: payload.timeline, artifacts: { edl: "", srt: "", fcpxml: "" } });
      if (String(payload.id).startsWith("timeline_")) setActiveProjectId(String(payload.id));
      if (payload.transcript?.length) setSegments(payload.transcript as Segment[]);
      const transforms: Record<number, ClipTransform> = {};
      payload.timeline.ranges.forEach((range, index) => { if (range.transform) transforms[index] = parseTransform(range.transform); });
      setRangeTransforms(transforms);
      return true;
    } catch {
      return false;
    }
  }, [transcribeServiceBase]);
  // 显式打开一个历史项目：绑定项目（台账卡片由既有的 activeProjectId 效应拉取）
  // + 载入项目时间线；转写凭据由 projectTranscriptRef 效应按需找回。
  const openHistoryProject = useCallback((projectId: string) => {
    // 只失效客户端请求代际与视图；reset 不调用后端 cancel/delete。
    resetWorkflowSession();
    setAutoEditing(false); setReeditBusy(false); setAutoEditTask(null);
    setReeditConfirmOpen(false); setReeditDialogState({ status: "missing" }); setCleanupRetryRun(null);
    workspaceEpoch.current += 1; workspaceProjectRef.current = projectId; workspaceBindingRef.current = `${projectId}:source`;
    sourceWorkspaceRef.current = null; setRemoteSource(null); setInspectorPreview(null); setInspectorBackgroundPreview(null);
    renderedClipsRef.current = []; projectCardsRef.current = [];
    setRenderedClips([]); setProjectCards([]); setProjectCardsLoaded(false); setProjectCardCount(0); setActiveRenderedClip(null);
    // 切片选择（2026-10-21 阶段）：换项目 = 重置恢复通道与缩略图失败标记，新台账重新恢复。
    activeClipRestoreProjectRef.current = null; setServerActiveClip(null); setFirstFrameFailures({});
    // 封面版型库（2026-10-21 阶段）：生成结果按项目工作区清空；注册表是全局的，保留。
    setCoverTemplateGenerateBusy(null); setCoverTemplateActionBusy(false);
    setFile(null); uploadBindingRef.current = null; setProjectIdRef(projectId);
    setClipTransforms({}); setRangeTransforms({}); setSourceReturnError(null);
    setPreviewUrl(null); setSegments([]); setTimeline(null); setTranscriptRef(null); setProjectTranscriptRef(null);
    setSourceReviewDocument(null); setSourceReviewDraft({}); setSourceReviewOpen(false); setSourceReviewVersion("");
    setActiveProjectId(projectId);
    void loadProjectTimeline(projectId);
    setMessage(`已打开历史项目 ${projectId}：素材区只显示该项目名下的切片。`);
  }, [loadProjectTimeline]);
  // 唯一的自动加载通道：URL 参数 ?source_id=<project_id>（兼容 ?project_id=，挂载时读一次）。
  // 没有参数时工作台保持全空白——不请求最近项目/切片/转写。
  const sourceParamApplied = useRef(false);
  useEffect(() => {
    if (sourceParamApplied.current) return;
    sourceParamApplied.current = true;
    const params = new URLSearchParams(window.location.search);
    const requested = params.get("source_id") || params.get("project_id");
    if (requested?.startsWith("timeline_")) openHistoryProject(requested);
  }, [openHistoryProject]);
  // legacy 实时会话（未刷新）：有成片但没有项目绑定时，按成片任务反查项目，
  // 使「重新剪辑/清空」按钮对该项目可用（loadProjectCards 声明在其后，故置于此处）。
  useEffect(() => {
    if (activeProjectId?.startsWith("timeline_") || !renderedClips.length) return undefined;
    const taskRef = renderedClips.find((clip) => clip.task_id)?.task_id;
    if (!taskRef) return undefined;
    let cancelled = false;
    fetch(`${transcribeServiceBase}/studio/tasks/${encodeURIComponent(taskRef)}/project`, { cache: "no-store" })
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        if (cancelled || !data?.project_id) return;
        setActiveProjectId(String(data.project_id));
        void loadProjectCards(String(data.project_id));
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [activeProjectId, renderedClips, loadProjectCards]);
  // 项目卡片自动加载：进入/接管项目后拉台账——cardCount 驱动顶栏按钮可用性。
  useEffect(() => {
    if (!activeProjectId?.startsWith("timeline_") || projectCardsLoaded) return undefined;
    void loadProjectCards(activeProjectId);
    return undefined;
  }, [activeProjectId, projectCardsLoaded, loadProjectCards]);
  // 2026-10-16：「无项目绑定时自动接管最近项目」已彻底移除（此前由
  // ENABLE_SESSION_RESTORE 门控）。现在没有 URL 参数/显式打开动作时，页面
  // 不发任何 GET /studio/projects 请求；历史项目只能从「打开历史视频」进入。
  /**
   * 清空**当前项目**的任务数据：弹二次确认 → 调后端显式端点 → 清空本地卡片状态。
   *
   * 刻意不做「刷新即删」：台账与任务记录都是落盘持久数据，删除不可撤销，刷新触发
   * 会把一次误按 F5 升级成数据丢失。后端还会用 expected_card_count 挡住
   * 「页面看到 2 张卡、确认时其实已出了第 3 张」的情况（409 → 刷新后重试）。
   */
  const requestClearProjectData = useCallback(async (projectId: string) => {
    try {
      const response = await fetch(`${studioBase}/studio/projects/${encodeURIComponent(projectId)}/data`, { cache: "no-store" });
      const text = await response.text();
      const payload = text ? JSON.parse(text) as {
        card_count?: number; applied_task_count?: number; reclaimable_bytes?: number;
        clip_ids?: string[]; active_task_ids?: string[]; detail?: string;
      } : null;
      if (!response.ok || !payload) {
        setMessage(payload?.detail || `无法读取项目数据（HTTP ${response.status}）`);
        return;
      }
      if (payload.active_task_ids?.length) {
        setMessage(`还有 ${payload.active_task_ids.length} 个未完成的任务，请等它们结束再清空。`);
        return;
      }
      const clipCount = payload.card_count ?? 0;
      if (!clipCount) {
        setMessage("当前项目没有任务卡片，无需清空。");
        return;
      }
      setClearDialog({
        cardCount: clipCount,
        clipCount,
        taskCount: payload.applied_task_count ?? 0,
        reclaimableBytes: payload.reclaimable_bytes ?? 0,
      });
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "无法读取项目数据，请稍后重试。");
    }
  }, [studioBase]);

  /** 用户在确认卡上点「确认清空」：真正执行（不可撤销）。 */
  const confirmClearProjectData = useCallback(async () => {
    const projectId = activeProjectId;
    const dialog = clearDialog;
    if (!projectId || !projectId.startsWith("timeline_") || !dialog) return;
    setClearBusy(true);
    try {
      const response = await fetch(
        `${studioBase}/studio/projects/${encodeURIComponent(projectId)}/data/clear`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          cache: "no-store",
          body: JSON.stringify({ confirm: true, expected_card_count: dialog.cardCount }),
        },
      );
      const text = await response.text();
      const payload = text ? JSON.parse(text) as {
        cleared_card_count?: number; cleared_task_count?: number; freed_bytes?: number;
        kept_source_video?: string | null; note?: string; detail?: unknown;
      } : null;
      if (!response.ok) {
        const detail = payload?.detail;
        const reason = typeof detail === "string" ? detail : JSON.stringify(detail ?? `HTTP ${response.status}`);
        // 409（卡片数变了 / 还有活动任务）不算失败操作，只是要求刷新后重看一遍。
        setMessage(`清空未执行：${reason}`);
        setClearDialog(null);
        return;
      }
      // 清空成功：把本地卡片状态一起清掉，否则 renderedClips 里的旧卡会继续显示。
      setRenderedClips([]);
      renderedClipsRef.current = [];
      setProjectCards([]);
      setProjectCardCount(0);
      setProjectCardsLoaded(true);
      setActiveRenderedClip(null);
      setAutoEditTask(null);
      setCoverHistoryClipId(null);
      setCoverHistory([]);
      setClearDialog(null);
      const freed = payload?.freed_bytes ? `释放 ${(payload.freed_bytes / 1024 / 1024).toFixed(1)} MB` : "已释放产物";
      const kept = payload?.kept_source_video ? "；源片与转写保留" : "";
      setMessage(`已清空 ${payload?.cleared_card_count ?? dialog.cardCount} 张卡片、`
        + `${payload?.cleared_task_count ?? 0} 个任务（${freed}）${kept}。`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "清空失败，请重试。");
    } finally {
      setClearBusy(false);
    }
  }, [activeProjectId, clearDialog, studioBase]);

  /**
   * 轮询一个局部任务（端点 3）直到 completed/failed，或 120s 超时。
   * 后端的 error/detail 原样抛出 —— 例如 cutout_v3 缺权重时的
   * E_CUTOUT_WEIGHTS_MISSING 与安装指引，不得被吞掉或改写成别的风格。
   */
  const pollLocalTask = useCallback(async (taskId: string, onProgress?: (status: LocalTaskStatus) => void, timeoutMs = LOCAL_TASK_POLL_TIMEOUT_MS): Promise<LocalTaskStatus> => {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      if (!mountedRef.current) throw new Error("页面已关闭，任务仍在后台执行");
      const response = await fetch(`${studioBase}/studio/tasks/${encodeURIComponent(taskId)}`, { cache: "no-store" });
      const text = await response.text();
      const payload = text ? JSON.parse(text) as LocalTaskStatus : null;
      if (!response.ok || !payload) throw new Error(payload?.detail || `无法读取任务进度（HTTP ${response.status}）`);
      onProgress?.(payload);
      if (payload.status === "completed") return payload;
      if (payload.status === "failed") throw new Error(payload.error || payload.detail || "局部任务失败");
      if (Date.now() >= deadline) throw new Error(`任务 ${taskId} 超时未完成（已等待 ${Math.round(timeoutMs / 1000)} 秒），请稍后在任务表查看`);
      await new Promise((resolve) => { window.setTimeout(resolve, LOCAL_TASK_POLL_INTERVAL_MS); });
    }
  }, [studioBase]);
  /**
   * 创建一个「类型化局部任务」（端点 2）并轮询到结束：
   * 成功 → 用 result.card 原地刷新该 clip（拿不到就重拉端点 1）；失败 → 原样抛出后端 detail。
   */
  const runLocalClipTask = useCallback(async (
    projectId: string,
    clip: RenderedClip,
    spec: LocalTaskSpec,
  ): Promise<LocalTaskStatus> => {
    setLocalTaskBusy(true);
    try {
      const body: Record<string, unknown> = { task_type: spec.task_type, instruction: spec.instruction };
      if (spec.payload) body.payload = spec.payload;
      if (spec.style_id) body.style_id = spec.style_id;
      if (spec.replace_intro !== undefined) body.replace_intro = spec.replace_intro;
      if (spec.cover_copy) body.cover_copy = spec.cover_copy;
      if (spec.title !== undefined) body.title = spec.title;
      const response = await fetch(
        `${studioBase}/studio/projects/${encodeURIComponent(projectId)}/clips/${encodeURIComponent(clip.clip_id)}/tasks`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
      );
      const createdText = await response.text();
      const created = createdText ? JSON.parse(createdText) as { task_id?: string; detail?: string } : null;
      if (!response.ok || !created?.task_id) throw new Error(created?.detail || `无法创建局部任务（HTTP ${response.status}）`);
      const status = await pollLocalTask(created.task_id, spec.onProgress, spec.timeoutMs);
      const card = status.result?.card;
      if (card && mountedRef.current) {
        const hit = applyProjectCard(card);
        if (!hit) void loadProjectCards(projectId);
      } else if (mountedRef.current) {
        void loadProjectCards(projectId);
      }
      return status;
    } finally {
      if (mountedRef.current) setLocalTaskBusy(false);
    }
  }, [studioBase, pollLocalTask, loadProjectCards]);
  // ===== 规则中心（Phase B）：实时读清单 → 版本可见性 / 启停 skill / 查看成片规则 / 重新生成 =====
  /**
   * 拉取规则中心数据（端点 GET /studio/workflow）。
   * 后端**每次实时读清单**，所以这里不做任何缓存：打开面板或点「刷新」就能看到
   * 「外部改了 skill 文件」或「刚启停过 skill」之后的新版本与新校验状态，无需重启浏览器。
   */
  const loadWorkflow = useCallback(async (): Promise<WorkflowPayload | null> => {
    try {
      const response = await fetch(`${studioBase}/studio/workflow`, { cache: "no-store" });
      const text = await response.text();
      const payload = text ? JSON.parse(text) as WorkflowPayload : null;
      if (!response.ok || !payload?.banner) {
        if (mountedRef.current) setWorkflowMessage(payload?.detail || `无法读取规则中心（HTTP ${response.status}）`);
        return null;
      }
      if (mountedRef.current) {
        setWorkflowBanner(payload.banner);
        setWorkflowSummary(payload.summary ?? null);
      }
      return payload;
    } catch {
      // 注册表不可达：保留既有 banner（不显示一个假的「默认版本」），只记录提示。
      if (mountedRef.current) setWorkflowMessage("规则中心不可达（后端未启动或清单缺失），未显示任何默认规则。");
      return null;
    }
  }, [studioBase]);
  /** 启停某个 skill（后端：备份 → 写回 → 立刻校验 → 非法回滚并 4xx）。 */
  const toggleWorkflowSkill = useCallback(async (skillId: string, enabled: boolean) => {
    setWorkflowBusy(true);
    try {
      const response = await fetch(
        `${studioBase}/studio/workflow/skills/${encodeURIComponent(skillId)}/toggle`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) },
      );
      const text = await response.text();
      const payload = text ? JSON.parse(text) as {
        banner?: WorkflowBanner; summary?: WorkflowSummary; detail?: string;
        workflow_version?: string; backup_path?: string;
      } : null;
      if (!response.ok) throw new Error(payload?.detail || `启停失败（HTTP ${response.status}）`);
      if (!mountedRef.current) return;
      setWorkflowBanner(payload?.banner ?? null);
      setWorkflowSummary(payload?.summary ?? null);
      setWorkflowMessage(`${skillId} 已${enabled ? "启用" : "停用"}：规则版本 v${payload?.workflow_version ?? "?"}（清单已备份，下一次自动剪辑将使用该版本）`);
    } catch (error) {
      if (mountedRef.current) setWorkflowMessage(error instanceof Error ? error.message : "启停规则失败");
    } finally {
      if (mountedRef.current) setWorkflowBusy(false);
    }
  }, [studioBase]);
  /** 「查看本次成片实际使用了哪些规则」：读该成片渲染任务的规则快照。 */
  const viewClipRules = useCallback(async (clip: RenderedClip) => {
    setWorkflowBusy(true);
    setWorkflowPanelOpen(true);
    setWorkflowSnapshotClipId(clip.clip_id);
    try {
      const response = await fetch(
        `${studioBase}/studio/workflow/${encodeURIComponent(clip.task_id)}/snapshot`, { cache: "no-store" });
      const text = await response.text();
      const payload = text ? JSON.parse(text) as WorkflowSnapshotPayload : null;
      if (!response.ok || !payload) throw new Error(payload?.detail || `无法读取规则快照（HTTP ${response.status}）`);
      if (mountedRef.current) setWorkflowSnapshot(payload);
    } catch (error) {
      if (mountedRef.current) {
        setWorkflowSnapshot(null);
        setWorkflowMessage(error instanceof Error ? error.message : "读取规则快照失败");
      }
    } finally {
      if (mountedRef.current) setWorkflowBusy(false);
    }
  }, [studioBase]);
  // 打开页面即取一次规则清单：顶栏不再显示规则版本，这里的数据供非顶栏位置使用
  // （剪辑助手执行任务后的「已按当前规则生成，规则版本：vX」与规则快照面板）。
  useEffect(() => { void loadWorkflow(); }, [loadWorkflow]);
  // 让局部任务回调始终读到最新素材/卡片（不把状态数组塞进依赖）。
  useEffect(() => {
    renderedClipsRef.current = renderedClips;
    projectCardsRef.current = projectCards;
    activeClipRef.current = activeRenderedClip ?? activeClip;
  }, [renderedClips, projectCards, activeRenderedClip, activeClip]);
  // projectId（timeline_*）可用时载入台账；不可用则保持回退链路，不报错。
  useEffect(() => {
    const projectId = activeProjectId;
    if (!projectId?.startsWith("timeline_")) return;
    void loadProjectCards(projectId);
  }, [activeProjectId, loadProjectCards]);
  // 刷新恢复（2026-10-21 阶段）：台账首次就绪后，把服务端记录的唯一 active_clip_id
  // 恢复为当前选中切片（播放器/转写/切片状态经 selectRenderedClip 一次切换到位，
  // skipPersist：服务端已是该值）。每个项目工作区只尝试一次；用户本会话已有选中不抢；
  // 台账已不含该切片（清空/重剪替换）时不恢复也不回写。
  useEffect(() => {
    const pid = activeProjectId;
    if (!pid?.startsWith("timeline_") || !projectCardsLoaded) return;
    if (activeClipRestoreProjectRef.current === pid) return;
    activeClipRestoreProjectRef.current = pid;
    if (activeRenderedClip) return;
    if (!serverActiveClip || serverActiveClip.project_id !== pid || !serverActiveClip.clip_id) return;
    const target = renderedClipList.find((clip) => clip.clip_id === serverActiveClip.clip_id);
    if (!target) return;
    selectRenderedClip(target, { skipPersist: true });
    setMessage(`已恢复上次选中的${target.name}。`);
  }, [activeProjectId, projectCardsLoaded, serverActiveClip, renderedClipList, activeRenderedClip, selectRenderedClip]);
  // 自动剪辑完成后立刻重拉台账：新成片按 clip_id 登记，不按任务追加。
  useEffect(() => {
    const projectId = activeProjectId;
    if (!projectId?.startsWith("timeline_") || autoEditTask?.status !== "completed") return;
    void loadProjectCards(projectId);
  }, [activeProjectId, autoEditTask?.status, autoEditTask?.task_id, loadProjectCards]);
  // 局部任务结束后（成功/失败）都重拉一次台账：失败也要看到真实卡片状态。
  const wasLocalTaskBusy = useRef(false);
  useEffect(() => {
    const projectId = activeProjectId;
    if (localTaskBusy) { wasLocalTaskBusy.current = true; return; }
    if (!wasLocalTaskBusy.current) return;
    wasLocalTaskBusy.current = false;
    if (!projectId?.startsWith("timeline_")) return;
    void loadProjectCards(projectId);
  }, [localTaskBusy, activeProjectId, loadProjectCards]);
  // 卡片刷新后播放器指向最新封面资产（版本号变化时重新拉取封面首帧）。
  useEffect(() => {
    if (!activeClip?.cover_asset_id) return;
    setCoverVersion((value) => value + 1);
  }, [activeClip?.cover_asset_id]);
  // ===== 剪辑助手（Agent）：上下文采集 → POST /studio/copilot → 白名单执行器 =====
  const copilotBaseUrl = autoEditBaseUrl.replace(/\/studio\/auto-edit$/, "/studio/copilot");
  const reportCopilotResult = (projectKey: string, action: CopilotAction, status: "executed" | "failed" | "dismissed", detail: string) => {
    void fetch(`${copilotBaseUrl}/action-result`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: projectKey, action, status, detail }),
    }).catch(() => { /* 审计回写失败不影响工作台 */ });
  };
  const runCopilotAction = async (projectKey: string, action: CopilotAction): Promise<string> => {
    // 目标 clip 定位：来自助手动作的 task_id+clip_id，找不到时退回当前选中的成片。
    const clipForAction = () => renderedClipList.find((item) => (
      action.clip_id && item.clip_id === action.clip_id
      && (!action.task_id || !item.task_id || item.task_id === action.task_id)
    )) || renderedClipList.find((item) => item.clip_id === action.clip_id)
      || (activeRenderedClip && (!action.clip_id || activeRenderedClip.clip_id === action.clip_id) ? activeRenderedClip : null);
    const projectIdForTasks = () => {
      // activeProjectId 才是项目台账的真实 id；timeline 在选中成片后指向成片局部时间线。
      const candidates = [activeProjectId, projectKey, timeline?.id];
      return candidates.find((item) => item?.startsWith("timeline_")) ?? null;
    };
    const styleLabel = (styleId: string | null | undefined) => (
      COVER_STYLES.find((item) => item.id === styleId)?.name || styleId || ""
    );
    const coverResultText = (result: LocalTaskStatus["result"]) => {
      const revision = result?.cover_revision;
      const style = styleLabel(result?.style_id);
      const pictureNote = result?.clip_final_unchanged === false ? "正片画面已更新" : "正片画面未变";
      const undoHint = result?.can_undo ? `，可撤销到 v${result.undo_revision ?? 1}` : "";
      const copyReason = result?.copy_report?.reason ? `；封面文案：${result.copy_report.reason}` : "";
      return `封面已更新 · v${revision ?? "?"}（风格 ${style || "默认"}，${pictureNote}）${undoHint}${copyReason}`;
    };
    // 类型化局部任务（封面/标题/字幕/时间线）：找不到台账时显式失败，不静默降级。
    const runTypedLocalTask = async (clip: RenderedClip, spec: LocalTaskSpec) => {
      const projectId = projectIdForTasks();
      if (!projectId) {
        throw new Error("该项目没有台账（project_id 必须是 timeline_*）：无法创建局部任务，请使用封面工具条或先完成一次自动剪辑");
      }
      return runLocalClipTask(projectId, clip, spec);
    };
    switch (action.type) {
      case "jump_to_time": {
        const target = Math.max(0, Math.min(action.start ?? 0, duration || (action.start ?? 0)));
        seek(target);
        return `已跳到 ${formatTime(target)}`;
      }
      case "play": {
        const video = videoRef.current;
        if (!video) throw new Error("还没有可播放的视频");
        await playGuarded(video);
        return "已开始播放";
      }
      case "pause": {
        videoRef.current?.pause();
        return "已暂停";
      }
      case "select_source_media": {
        if (!file) throw new Error("当前没有原始视频");
        selectSourceMedia();
        return "已切换到原始视频";
      }
      case "select_rendered_clip": {
        const clip = renderedClipList.find((item) => item.task_id === action.task_id && item.clip_id === action.clip_id)
          || renderedClipList.find((item) => item.clip_id === action.clip_id);
        if (!clip) throw new Error("该成片不在当前项目素材列表中");
        selectRenderedClip(clip);
        return `已打开${clip.name}`;
      }
      case "select_transcript_segment": {
        const index = action.index ?? 0;
        const segment = segments[index];
        if (!segment) throw new Error("该转写片段序号超出范围");
        setSelectedSegment(index);
        seek(segment.start);
        return `已选中第 ${index + 1} 句`;
      }
      case "highlight_time_range": {
        const start = action.start ?? 0;
        const end = action.end ?? Math.min(duration, start + 10);
        setCopilotHighlight({ start, end });
        seek(start);
        return `已高亮 ${formatTime(start)} – ${formatTime(end)}`;
      }
      case "extract_insights": {
        if (!segments.length) throw new Error("请先完成转写");
        await extractInsights();
        return "金句与关键词已更新";
      }
      case "start_auto_edit": {
        const ok = await startAutoEdit();
        if (!ok) throw new Error("自动剪辑未能启动（需原始视频 + 已完成转写）");
        return "已提交到本机固定剪辑流程，进度见任务面板";
      }
      case "rerender_auto_edit": {
        if (!autoEditTask) throw new Error("还没有可复渲染的任务");
        await rerenderAutoEdit();
        return "已创建复渲染任务";
      }
      case "make_cover": {
        // 兼容旧动作名：优先走类型化局部任务（REGENERATE_COVER，只换封面图 + 0.8s 片头，
        // 绝不重跑自动剪辑、绝不新增卡片）；没有项目台账时退回既有轻量封面通道
        // （replaceCover 在通道 5xx/不可达时还会自动退回整片重渲后备链路）。
        const clip = clipForAction();
        if (!clip) throw new Error("该成片不在当前项目素材列表中");
        if (coverBusy || localTaskBusy) throw new Error("已有封面任务进行中，请等它完成再提交");
        const styleId = (action.style_id === "cutout_v3" ? "cutout_v3" : "realframe_v2") as CoverStyleId;
        setCoverStyle(styleId);
        const projectId = projectIdForTasks();
        if (projectId && renderedClipList.length > 0) {
          const status = await runTypedLocalTask(clip, {
            task_type: "REGENERATE_COVER",
            style_id: styleId,
            replace_intro: action.replace_intro !== false,
            instruction: action.instruction || undefined,
          });
          return coverResultText(status.result);
        }
        await replaceCover(clip, styleId, action.replace_intro !== false);
        const styleName = COVER_STYLES.find((item) => item.id === styleId)?.name || styleId;
        return `已按「${styleName}」重制${clip.name}的封面（封面图 + 0.8s 片头，已备份可回滚）`;
      }
      case "regenerate_cover": {
        const clip = clipForAction();
        if (!clip) throw new Error("该成片不在当前项目素材列表中");
        if (coverBusy || localTaskBusy) throw new Error("已有封面任务进行中，请等它完成再提交");
        const styleId = (action.style_id === "cutout_v3" ? "cutout_v3" : "realframe_v2") as CoverStyleId;
        setCoverStyle(styleId);
        const status = await runTypedLocalTask(clip, {
          task_type: "REGENERATE_COVER",
          style_id: styleId,
          replace_intro: action.replace_intro !== false,
          instruction: action.instruction || undefined,
        });
        return coverResultText(status.result);
      }
      case "update_title": {
        const clip = clipForAction();
        if (!clip) throw new Error("该成片不在当前项目素材列表中");
        const title = (action.title || "").trim();
        const status = await runTypedLocalTask(clip, {
          task_type: "UPDATE_TITLE",
          ...(title ? { title } : {}),
          instruction: action.instruction || undefined,
        });
        const applied = status.result?.title || title;
        const copyReason = status.result?.copy_report?.reason ? `；封面文案：${status.result.copy_report.reason}` : "";
        return `${clip.name}的标题已更新为「${applied}」（正片画面未重剪）${copyReason}`;
      }
      case "update_subtitle_style": {
        const clip = clipForAction();
        if (!clip) throw new Error("该成片不在当前项目素材列表中");
        const fontScale = action.font_scale ?? 1.15;
        await runTypedLocalTask(clip, {
          task_type: "UPDATE_SUBTITLE_STYLE",
          payload: { font_scale: fontScale },
          instruction: action.instruction || undefined,
        });
        return `${clip.name}的字幕样式已更新（字号 ×${fontScale}）`;
      }
      case "update_timeline": {
        const clip = clipForAction();
        if (!clip) throw new Error("该成片不在当前项目素材列表中");
        const removeRanges = action.remove_ranges ?? null;
        const status = await runTypedLocalTask(clip, {
          task_type: "UPDATE_TIMELINE",
          ...(removeRanges ? { payload: { remove: removeRanges } } : {}),
          instruction: action.instruction || undefined,
        });
        const rangesCount = (status.result as { ranges_count?: number } | null | undefined)?.ranges_count;
        return `${clip.name}的时间线已更新${rangesCount ? `（${rangesCount} 段）` : ""}，需要重渲时才走整片流程`;
      }
      case "update_bgm": {
        // 「换 BGM / 调 BGM」走既有 UPDATE_AUDIO 局部任务：前端基于当前音轨状态
        // 计算补丁（只动 kind=bgm 的轨，原声轨不动），混音在导出侧生效。
        const clip = clipForAction();
        if (!clip) throw new Error("该成片不在当前项目素材列表中");
        const currentTracks = (inspectorVoiceConfig?.audio_tracks ?? []) as Array<{
          kind?: string; asset_id?: string; id?: string;
          volume?: number; fade_in_s?: number; fade_out_s?: number;
        }>;
        const bgmTracks = currentTracks.filter((track) => (track.kind || "bgm") === "bgm");
        if (action.bgm_remove !== true && bgmTracks.length === 0) {
          throw new Error("该成片还没有上传 BGM：请在 Inspector 效果页上传后，再让助手调整");
        }
        const nextTracks = action.bgm_remove === true
          ? currentTracks.filter((track) => (track.kind || "bgm") !== "bgm")
          : currentTracks.map((track) => {
              if ((track.kind || "bgm") !== "bgm") return track;
              return {
                ...track,
                ...(action.bgm_volume !== null && action.bgm_volume !== undefined ? { volume: action.bgm_volume } : {}),
                ...(action.bgm_fade_in_s !== null && action.bgm_fade_in_s !== undefined ? { fade_in_s: action.bgm_fade_in_s } : {}),
                ...(action.bgm_fade_out_s !== null && action.bgm_fade_out_s !== undefined ? { fade_out_s: action.bgm_fade_out_s } : {}),
              };
            });
        await runTypedLocalTask(clip, {
          task_type: "UPDATE_AUDIO",
          payload: { audio_tracks: nextTracks },
          instruction: action.instruction || undefined,
        });
        if (action.bgm_remove === true) return `${clip.name}的 BGM 已移除（混音在导出侧生效）`;
        const parts: string[] = [];
        if (action.bgm_volume !== null && action.bgm_volume !== undefined) parts.push(`音量 ${action.bgm_volume}`);
        if (action.bgm_fade_in_s !== null && action.bgm_fade_in_s !== undefined) parts.push(`淡入 ${action.bgm_fade_in_s}s`);
        if (action.bgm_fade_out_s !== null && action.bgm_fade_out_s !== undefined) parts.push(`淡出 ${action.bgm_fade_out_s}s`);
        return `${clip.name}的 BGM 已更新（${parts.join("、") || "无参数变化"}，混音在导出侧生效）`;
      }
      case "adopt_cover": {
        // 「沿用已生成的封面」：复用现成 cover.jpg，不调用生成链路；默认不改片头。
        const clip = renderedClipList.find((item) => item.task_id === action.task_id && item.clip_id === action.clip_id)
          || renderedClipList.find((item) => item.clip_id === action.clip_id);
        if (!clip) throw new Error("该成片不在当前项目素材列表中");
        if (coverBusy) throw new Error("已有封面任务进行中，请等它完成再提交");
        const sourceClipId = action.cover_source_clip_id || clip.clip_id;
        await adoptCover(clip, sourceClipId, action.replace_intro === true);
        const source = renderedClipList.find((item) => item.clip_id === sourceClipId);
        return `已沿用「${source?.name || sourceClipId}」已生成的封面${action.replace_intro === true ? "（含片头）" : "（未改片头）"}`;
      }
      case "regenerate_with_latest_rules": {
        // 规则中心 / 卡片菜单的统一入口：**按最新规则**重新生成这一条成片。
        // 走 POST /studio/workflow/regenerate/{project}/{clip}：新增 revision、旧成果保留、
        // 不新增卡片（后端只作用在台账里已有的 clip 上）。
        // action.task_type 承载模式：render = 真重渲，其它 = 只重做封面（默认）。
        const clip = clipForAction();
        if (!clip) throw new Error("该成片不在当前项目素材列表中");
        const projectId = projectIdForTasks();
        if (!projectId) throw new Error("该项目没有台账（project_id 必须是 timeline_*），无法按最新规则重新生成");
        const mode = action.task_type === "render" ? "render" : "cover";
        if (localTaskBusy || coverBusy) throw new Error("已有局部任务进行中，请等它完成再提交");
        setLocalTaskBusy(true);
        try {
          const response = await fetch(
            `${studioBase}/studio/workflow/regenerate/${encodeURIComponent(projectId)}/${encodeURIComponent(clip.clip_id)}`,
            {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ mode, instruction: action.instruction || undefined }),
            },
          );
          const text = await response.text();
          const created = text ? JSON.parse(text) as {
            task_id?: string; workflow_version?: string; cards_before?: number;
            cover_revision_before?: number; render_revision_before?: number; detail?: string;
          } : null;
          if (!response.ok || !created?.task_id) {
            throw new Error(created?.detail || `无法创建重新生成任务（HTTP ${response.status}）`);
          }
          setWorkflowMessage(`已按最新规则（workflow v${created.workflow_version ?? "?"}）重新生成 ${clip.name}：卡片数保持 ${created.cards_before ?? "?"}，正在生成新 revision…`);
          const status = await pollLocalTask(created.task_id);
          const card = status.result?.card;
          if (card && mountedRef.current) {
            const hit = applyProjectCard(card);
            if (!hit) void loadProjectCards(projectId);
          }
          const revision = status.result?.cover_revision ?? status.result?.render_revision;
          return `${clip.name}已按最新规则（workflow v${created.workflow_version ?? "?"}）重新生成：新 revision ${revision ?? "?"}，卡片数不变（${created.cards_before ?? "?"}）`;
        } finally {
          if (mountedRef.current) setLocalTaskBusy(false);
        }
      }
      case "render_clip": {
        const clip = renderedClipList.find((item) => item.task_id === action.task_id && item.clip_id === action.clip_id)
          || renderedClipList.find((item) => item.clip_id === action.clip_id);
        if (!clip) throw new Error("该成片不在当前项目素材列表中");
        if (autoEditing) throw new Error("已有本机任务进行中，请等它完成再提交");
        await rerenderClip(clip, "render_clip", coverStyle);
        return `已为${clip.name}提交单切片渲染任务，进度见任务面板`;
      }
      case "set_speaker": {
        const index = action.index ?? selectedSegment;
        if (index === null || index === undefined || !segments[index]) throw new Error("请先选中一句转写");
        updateSpeaker(index, (action.speaker || "未标注") as SpeakerRole);
        return `已把第 ${index + 1} 句标记为「${action.speaker}」`;
      }
      case "export_active_clip": {
        if (!activeRenderedClip) throw new Error("请先打开一个成片再导出");
        downloadRenderedClip();
        return "已触发导出下载";
      }
      default:
        throw new Error(`未知动作类型: ${action.type}`);
    }
  };
  /**
   * UI 按钮（卡片菜单 / 规则中心）的统一入口：复用既有白名单执行器 runCopilotAction，
   * 不另造一条请求链路；返回值文案与助手执行时完全一致。
   */
  const regenerateClipByLatestRules = (clip: RenderedClip, mode: "cover" | "render" = "cover") => {
    const projectKey = activeProjectId || (timeline?.id?.startsWith("timeline_") ? timeline.id : null) || "timeline_unsaved";
    return runCopilotAction(projectKey, {
      id: `latest-rules-${clip.clip_id}-${Date.now()}`,
      type: "regenerate_with_latest_rules",
      label: mode === "render" ? "按最新规则重渲画面" : "按最新规则重新生成封面",
      execution: "run",
      clip_id: clip.clip_id,
      task_id: clip.task_id,
      task_type: mode,
    });
  };
  const patchCopilotAction = (actionId: string, status: CopilotAction["status"], result?: string) => {
    setCopilotMessages((items) => items.map((message) => ({
      ...message,
      actions: message.actions?.map((action) => action.id === actionId ? { ...action, status, result } : action),
    })));
  };
  /**
   * 剪辑助手执行任务后的规则版本行（规则信息替代位置 #2，非顶栏）。
   * 版本来自后端 GET /studio/workflow（每次实时读清单，见 loadWorkflow）；
   * 读不到就明确写「未知」，不编造版本号，也不改后端契约。
   */
  const copilotRuleVersionLine = () => {
    const version = workflowSummary?.workflow_version || workflowBanner?.current_version || null;
    return version ? `已按当前规则生成，规则版本：v${version}` : "已按当前规则生成，规则版本：未知（未读取到规则清单）";
  };
  /** 会产出成片/资产的动作：执行结果里附上规则版本行；纯导航类动作（跳转/播放/选中）不附。 */
  const RULE_VERSION_ACTION_TYPES = new Set([
    "start_auto_edit", "rerender_auto_edit", "make_cover", "regenerate_cover", "adopt_cover",
    "update_title", "update_subtitle_style", "update_timeline", "update_bgm", "regenerate_with_latest_rules", "render_clip",
  ]);
  const executeCopilotAction = async (projectKey: string, action: CopilotAction) => {
    patchCopilotAction(action.id, "run");
    try {
      const result = await runCopilotAction(projectKey, action);
      const withRuleVersion = RULE_VERSION_ACTION_TYPES.has(action.type) ? `${result} · ${copilotRuleVersionLine()}` : result;
      patchCopilotAction(action.id, "executed", withRuleVersion);
      reportCopilotResult(projectKey, action, "executed", withRuleVersion);
    } catch (error) {
      const detail = error instanceof Error ? error.message : "执行失败";
      patchCopilotAction(action.id, "failed", detail);
      reportCopilotResult(projectKey, action, "failed", detail);
    }
  };
  const sendCopilot = async (text: string) => {
    const trimmed = text.trim();
    if (!trimmed || copilotBusy) return;
    const projectKey = activeProjectId || (timeline?.id?.startsWith("timeline_") ? timeline.id : null) || "timeline_unsaved";
    setCopilotMessages((items) => [...items, { id: `${Date.now()}-u`, role: "user", text: trimmed }]);
    setCopilotBusy(true);
    try {
      const response = await fetch(copilotBaseUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: trimmed,
          project_id: projectKey,
          current_time: currentTime,
          duration,
          selected_segment_index: selectedSegment,
          asr_segments: segments,
          rendered_clips: renderedClipList,
          timeline: timeline?.timeline ?? {},
          insights: insights ?? {},
          task: autoEditTask ? { task_id: autoEditTask.task_id, status: autoEditTask.status, stage: autoEditTask.stage, progress: autoEditTask.progress } : {},
          viewing: activeRenderedClip ? "clip" : "source",
          // 让助手绑定到当前选中的 clip：局部任务（封面/标题/字幕/时间线）靠这两个 id 定点。
          active_clip_id: activeRenderedClip?.clip_id ?? null,
          active_task_id: activeRenderedClip?.task_id ?? null,
          has_rendered_clips: projectCards.length > 0 || renderedClips.length > 0,
        }),
      });
      const payloadText = await response.text();
      const payload = payloadText ? JSON.parse(payloadText) as { copilot?: { reply?: string; engine?: "llm" | "rules"; notice?: string | null; actions?: CopilotAction[] }; detail?: string } : {};
      if (!response.ok || !payload.copilot) throw new Error(payload.detail || `剪辑助手服务不可用（${response.status}）`);
      const copilot = payload.copilot;
      const actions = (copilot.actions || []).map((action) => ({ ...action, status: action.execution === "confirm" ? "confirm" as const : "run" as const }));
      const assistantId = `${Date.now()}-a`;
      setCopilotMessages((items) => [...items, { id: assistantId, role: "assistant", text: copilot.reply || "已处理。", engine: copilot.engine, notice: copilot.notice, actions }]);
      for (const action of actions) {
        if (action.status === "run") await executeCopilotAction(projectKey, { ...action, id: action.id || `${assistantId}-${action.type}` });
      }
    } catch (error) {
      setCopilotMessages((items) => [...items, { id: `${Date.now()}-e`, role: "assistant", text: error instanceof Error ? error.message : "剪辑助手暂时不可用。", engine: "rules", notice: "当前为本机规则助手模式。" }]);
    } finally {
      setCopilotBusy(false);
    }
  };
  const handleCopilotConfirm = (action: CopilotAction) => {
    const projectKey = activeProjectId || (timeline?.id?.startsWith("timeline_") ? timeline.id : null) || "timeline_unsaved";
    void executeCopilotAction(projectKey, action);
  };
  const handleCopilotDismiss = (action: CopilotAction) => {
    patchCopilotAction(action.id, "dismissed");
    reportCopilotResult(activeProjectId || "timeline_unsaved", action, "dismissed", "用户选择不执行");
  };
  // play() 的 Promise 在换源/重新载入时会被浏览器以 AbortError 拒绝（React 文档 goo.gl/LdLk22），
  // 属良性中断：只吞 AbortError，真正的自动播放策略拒绝（NotAllowedError 等）照常上抛。
  const playGuarded = (video: HTMLVideoElement) => video.play().catch((error) => {
    if ((error as DOMException)?.name !== "AbortError") throw error;
  });
  const togglePlayback = async () => { const video = videoRef.current; if (!video) return; if (video.paused) await playGuarded(video); else video.pause(); };
  const seek = (seconds: number) => { if (videoRef.current) videoRef.current.currentTime = seconds; setCurrentTime(seconds); };
  const seekFromTimelineEvent = (event: TimelinePointer) => {
    const trackGrid = event.currentTarget.querySelector<HTMLDivElement>(".track-grid") || trackGridRef.current;
    if (!trackGrid || !duration) return;
    const trackRect = trackGrid.getBoundingClientRect();
    const labelWidth = 154;
    const trackWidth = Math.max(1, trackRect.width - labelWidth);
    const position = Math.max(0, Math.min(trackWidth, event.clientX - trackRect.left - labelWidth));
    seek(position / trackWidth * duration);
  };
  const seekFromTimelinePointer = (event: ReactPointerEvent<HTMLDivElement>) => seekFromTimelineEvent(event);
  // ===== 底部时间线接线（Phase B）：历史 / 撤销重做 / ＋ 菜单 / 波形 / 视图持久化 =====
  // 「项目原始时间线视图」才显示/回写 revision 数据：成片视图的 duration 是成片长度，
  // 混入项目级 ranges/音轨会错位。
  const timelineProjectId = activeProjectId?.startsWith("timeline_") ? activeProjectId : null;
  const projectTimelineActive = Boolean(
    timelineProjectId && timeline?.id === timelineProjectId && !activeRenderedClip,
  );
  const applyHistoryPayload = useCallback((payload: TimelineHistoryPayload) => {
    setTimelineCanUndo(Boolean(payload.can_undo));
    setTimelineCanRedo(Boolean(payload.can_redo));
    setRevisionAudioTracks(payload.audio_tracks ?? []);
  }, []);
  // revision.ranges → 页面 TimelineRange：undo/redo/加轨后回写页面时间线状态，
  // 保持「字幕轨/视频轨」的既有显示与 EDL 视图和 revision 一致。
  const writeBackRevisionRanges = useCallback((payload: TimelineHistoryPayload) => {
    const raw = payload.current_revision?.ranges ?? [];
    if (!raw.length) return;
    const mapped: TimelineRange[] = [];
    raw.forEach((item, index) => {
      const record = item as { id?: unknown; source_start?: unknown; source_end?: unknown; transform?: Record<string, unknown> | null };
      const start = Number(record.source_start);
      const end = Number(record.source_end);
      if (!Number.isFinite(start) || !Number.isFinite(end)) return;
      mapped.push({
        id: typeof record.id === "string" ? record.id : `range-${index}`,
        source_start: start,
        source_end: end,
        transform: record.transform ?? null,
      });
    });
    if (!mapped.length) return;
    setTimeline((previous) => (
      previous && previous.id === timelineProjectId
        ? { ...previous, timeline: { ...previous.timeline, ranges: mapped } }
        : previous
    ));
  }, [timelineProjectId]);
  // 对有 path 的轨拉真实波形（后端 ffmpeg 峰值，buckets=800）；同一 (track_id:path) 只拉一次。
  const loadPeaksForTracks = useCallback(async (tracks: TimelineAudioTrack[]) => {
    if (!timelineProjectId) return;
    await Promise.all(tracks.filter((track) => track.path).map(async (track) => {
      const path = track.path as string;
      const cacheKey = `${track.track_id}:${path}`;
      if (timelinePeaksFetchedRef.current.has(cacheKey)) return;
      timelinePeaksFetchedRef.current.add(cacheKey);
      try {
        const payload = await fetchTimelinePeaks(transcribeServiceBase, timelineProjectId, {
          path, trackId: track.track_id, kind: track.kind, buckets: 800,
        });
        if (!mountedRef.current) return;
        setPeaksByTrack((existing) => ({ ...existing, [track.track_id]: payload.peaks }));
        setPeaksDurationByTrack((existing) => ({ ...existing, [track.track_id]: payload.duration_s }));
      } catch {
        timelinePeaksFetchedRef.current.delete(cacheKey); // 失败允许下次重试
      }
    }));
  }, [timelineProjectId]);
  // GET /history：can_undo/can_redo + 当前 revision（音轨/ranges）+ 波形。
  const refreshTimelineHistory = useCallback(async () => {
    if (!timelineProjectId) return;
    try {
      const payload = await fetchTimelineHistory(transcribeServiceBase, timelineProjectId);
      if (!mountedRef.current) return;
      applyHistoryPayload(payload);
      writeBackRevisionRanges(payload);
      void loadPeaksForTracks(payload.audio_tracks ?? []);
    } catch { /* 无历史/端点不可用时保持既有显示，不阻塞编辑 */ }
  }, [timelineProjectId, applyHistoryPayload, writeBackRevisionRanges, loadPeaksForTracks]);
  // undo/redo：用返回的 revision 回写页面时间线状态并刷新轨道。
  const stepTimeline = useCallback(async (step: "undo" | "redo") => {
    if (!timelineProjectId || timelineBusy) return;
    setTimelineBusy(true);
    try {
      const payload = await stepTimelineHistory(transcribeServiceBase, timelineProjectId, step);
      if (!mountedRef.current) return;
      applyHistoryPayload(payload);
      writeBackRevisionRanges(payload);
      void loadPeaksForTracks(payload.audio_tracks ?? []);
      setMessage(step === "undo" ? `已撤销：时间线回到 revision ${payload.revision ?? "?"}` : `已重做：时间线回到 revision ${payload.revision ?? "?"}`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : step === "undo" ? "撤销失败" : "重做失败");
    } finally {
      setTimelineBusy(false);
    }
  }, [timelineProjectId, timelineBusy, applyHistoryPayload, writeBackRevisionRanges, loadPeaksForTracks]);
  // ＋ 菜单落点：bgm/sfx 先选文件上传，subtitle/visual 直接声明轨道；
  // 409（原声已自动进入等）与其它失败把后端 detail 原样提示。
  const submitTimelineTrack = useCallback(async (kind: TimelineAddKind, audioFile: File | null) => {
    if (!timelineProjectId || timelineBusy) return;
    setTimelineBusy(true);
    try {
      let body: Record<string, unknown>;
      if (kind === "bgm" || kind === "sfx") {
        if (!audioFile) return;
        setMessage(kind === "bgm" ? "正在上传背景音乐…" : "正在上传音效…");
        const uploaded = await uploadTimelineAudio(transcribeServiceBase, timelineProjectId, audioFile);
        body = audioTrackRequest({ kind, path: uploaded.path, label: audioFile.name, recordStartS: 0 });
      } else {
        body = { kind };
      }
      const payload = await postTimelineTrack(transcribeServiceBase, timelineProjectId, body);
      if (!mountedRef.current) return;
      applyHistoryPayload(payload);
      writeBackRevisionRanges(payload);
      void loadPeaksForTracks(payload.audio_tracks ?? []);
      setMessage(kind === "bgm" || kind === "sfx"
        ? `「${audioFile?.name || "音频"}」已加入新的音轨（双击音轨行可删除，Ctrl/Cmd+Z 可撤销）`
        : kind === "subtitle" ? "字幕轨已加入时间线" : "视觉轨已加入时间线");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "添加轨道失败");
    } finally {
      setTimelineBusy(false);
    }
  }, [timelineProjectId, timelineBusy, applyHistoryPayload, writeBackRevisionRanges, loadPeaksForTracks]);
  const handleTimelineAddTrack = useCallback((kind: TimelineAddKind) => {
    if (!timelineProjectId || timelineBusy) return;
    if (kind === "bgm" || kind === "sfx") {
      timelinePendingAddRef.current = kind;
      timelineAudioInputRef.current?.click();
      return;
    }
    void submitTimelineTrack(kind, null);
  }, [timelineProjectId, timelineBusy, submitTimelineTrack]);
  const handleTimelineAudioFile = (event: ChangeEvent<HTMLInputElement>) => {
    const picked = event.target.files?.[0] || null;
    const kind = timelinePendingAddRef.current;
    event.target.value = "";
    timelinePendingAddRef.current = null;
    if (!picked || !kind) return;
    void submitTimelineTrack(kind, picked);
  };
  // 双击音轨行删除该轨（可撤销）：原声轨由导入自动进入，不允许删除。
  const handleTimelineLaneDoubleClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    const lane = (event.target as HTMLElement).closest?.('[data-testid^="timeline-audio-lane-"]');
    if (!(lane instanceof HTMLElement)) return;
    const trackId = lane.dataset.trackId;
    const track = revisionAudioTracks.find((item) => item.track_id === trackId);
    if (!track || !timelineProjectId || timelineBusy) return;
    if (track.origin === "source") {
      setMessage("原声轨由导入自动进入时间线，不能删除");
      return;
    }
    void (async () => {
      setTimelineBusy(true);
      try {
        const payload = await deleteTimelineTrack(transcribeServiceBase, timelineProjectId, track.track_id);
        if (!mountedRef.current) return;
        applyHistoryPayload(payload);
        writeBackRevisionRanges(payload);
        setMessage(`已删除音轨「${track.label || track.track_id}」（Ctrl/Cmd+Z 可撤销）`);
      } catch (error) {
        setMessage(error instanceof Error ? error.message : "删除轨道失败");
      } finally {
        setTimelineBusy(false);
      }
    })();
  };
  // 缩放一律以播放头为锚点重算 scroll_left：播放头时间在缩放前后不跳变。
  const handleTimelineZoomIn = useCallback(() => {
    setTimelineView((view) => applyZoomAnchored(view, nextZoomStep(view.zoom_px_per_second, { zoomIn: true }), currentTime));
  }, [currentTime]);
  const handleTimelineZoomOut = useCallback(() => {
    setTimelineView((view) => applyZoomAnchored(view, nextZoomStep(view.zoom_px_per_second, { zoomIn: false }), currentTime));
  }, [currentTime]);
  const handleTimelineZoomChange = useCallback((pxPerSecond: number) => {
    setTimelineView((view) => applyZoomAnchored(view, clampZoom(pxPerSecond), currentTime));
  }, [currentTime]);
  const handleTimelineFitWindow = useCallback(() => {
    setTimelineView((view) => (
      applyZoomAnchored(view, fitToWindowPxPerSecond(duration, timelineViewportWidth), currentTime)
    ));
  }, [currentTime, duration, timelineViewportWidth]);
  const handleTimelineTrackHeightChange = useCallback((px: number) => {
    setTimelineView((view) => ({ ...view, audio_track_height: clampTrackHeight(px) }));
  }, []);
  const handleTimelineWaveformRatioChange = useCallback((ratio: WaveformRatio) => {
    setTimelineView((view) => ({ ...view, waveform_ratio: ratio }));
  }, []);
  // 原生滚动写回视图状态（防抖 PUT 由视图持久化 effect 统一承担）。
  const handleTimelineScroll = useCallback((event: SyntheticEvent<HTMLDivElement>) => {
    const scrollLeft = event.currentTarget.scrollLeft;
    setTimelineView((view) => (view.scroll_left === scrollLeft ? view : { ...view, scroll_left: scrollLeft }));
  }, []);
  const handleTimelineSeek = (seconds: number) => seek(seconds);
  // 快捷键：Ctrl/Cmd+Z 撤销、Ctrl/Cmd+Shift+Z / Ctrl/Cmd+Y 重做。
  // 仅当对应方向可用且焦点不在输入框/选择器/可编辑元素时拦截。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable)) return;
      const key = event.key.toLowerCase();
      if (key === "z" && !event.shiftKey) {
        if (!timelineCanUndo || timelineBusy) return;
        event.preventDefault();
        void stepTimeline("undo");
      } else if ((key === "z" && event.shiftKey) || key === "y") {
        if (!timelineCanRedo || timelineBusy) return;
        event.preventDefault();
        void stepTimeline("redo");
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [timelineCanUndo, timelineCanRedo, timelineBusy, stepTimeline]);
  // 项目就绪后拉一次历史；项目为空时清空时间线编辑状态。
  useEffect(() => {
    if (!timelineProjectId) {
      setTimelineCanUndo(false);
      setTimelineCanRedo(false);
      setRevisionAudioTracks([]);
      return undefined;
    }
    void refreshTimelineHistory();
    return undefined;
  }, [timelineProjectId, refreshTimelineHistory]);
  // 项目载入后 GET view 恢复视图设置（缩放/轨高/波形占比/滚动）。
  useEffect(() => {
    if (!timelineProjectId) {
      timelineViewHydratedRef.current = null;
      return undefined;
    }
    let cancelled = false;
    void (async () => {
      try {
        const view = await loadTimelineView(transcribeServiceBase, timelineProjectId);
        if (!cancelled && mountedRef.current) {
          setTimelineView(view);
          // 记录「已从服务端恢复」的序列化视图：避免恢复本身触发一次回写 PUT。
          timelineViewSavedRef.current = JSON.stringify(view);
        }
      } catch { /* 读取失败按默认视图显示 */ }
      if (!cancelled) timelineViewHydratedRef.current = timelineProjectId;
    })();
    return () => { cancelled = true; };
  }, [timelineProjectId]);
  // 视图变更防抖 300ms PUT（纯显示设置：不写历史、不触发渲染）。
  useEffect(() => {
    if (!timelineProjectId) return undefined;
    if (timelineViewHydratedRef.current !== timelineProjectId) return undefined;
    const serialized = JSON.stringify({
      zoom_px_per_second: timelineView.zoom_px_per_second,
      audio_track_height: timelineView.audio_track_height,
      waveform_ratio: timelineView.waveform_ratio,
      scroll_left: timelineView.scroll_left,
    });
    if (timelineViewSavedRef.current === serialized) return undefined;
    if (timelineViewSaveTimer.current) window.clearTimeout(timelineViewSaveTimer.current);
    timelineViewSaveTimer.current = window.setTimeout(() => {
      timelineViewSavedRef.current = serialized;
      void saveTimelineViewPatch(transcribeServiceBase, timelineProjectId, {
        zoom_px_per_second: timelineView.zoom_px_per_second,
        audio_track_height: timelineView.audio_track_height,
        waveform_ratio: timelineView.waveform_ratio,
        scroll_left: timelineView.scroll_left,
      }).catch(() => { /* 保存失败静默：下一次改动会带最新值重试 */ });
    }, 300);
    return () => {
      if (timelineViewSaveTimer.current) window.clearTimeout(timelineViewSaveTimer.current);
    };
  }, [timelineProjectId, timelineView]);
  // ===== 项目名称内联编辑（2026-10-20）=====
  const refreshProjectName = useCallback(async (projectId: string) => {
    try {
      const response = await fetch(`${transcribeServiceBase}/studio/projects/${encodeURIComponent(projectId)}/name`, { cache: "no-store" });
      if (!response.ok) return;
      const meta = await response.json() as { display_name?: string | null; fallback_name?: string | null; updated_at?: string | null };
      setProjectDisplayName(meta.display_name || meta.fallback_name || projectId);
      setProjectNameUpdatedAt(meta.updated_at || null);
    } catch { /* 名称加载失败不阻塞 */ }
  }, [transcribeServiceBase]);

  const saveProjectName = async () => {
    if (!activeProjectId || projectNameSaving) return;
    const name = projectNameDraft.trim();
    if (!name) { setProjectNameError("名称不能为空"); return; }  // 空名称禁止保存：保持编辑态
    if (name === projectDisplayName) { setProjectNameEditing(false); return; }  // 未改动：仅退出编辑
    setProjectNameSaving(true); setProjectNameError(null);
    try {
      const response = await fetch(`${transcribeServiceBase}/studio/projects/${encodeURIComponent(activeProjectId)}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: name, expected_updated_at: projectNameUpdatedAt }),
      });
      const payload = await response.json().catch(() => ({} as { detail?: unknown }));
      if (response.status === 409) {
        setProjectNameError("项目已在其它页面更新，请刷新页面后重试");
        return;  // 失败恢复旧名称：不提交本地改动
      }
      if (!response.ok) {
        const detail = payload.detail as { message?: string } | string | undefined;
        setProjectNameError(typeof detail === "string" ? detail
          : (detail as { message?: string })?.message || `保存失败（HTTP ${response.status}）`);
        return;
      }
      setProjectDisplayName(name);
      setProjectNameUpdatedAt((payload as { updated_at?: string | null }).updated_at || null);
      setProjectNameSavedTick(true);  // 顶栏短暂显示「已保存」
      if (projectNameTickTimer.current) window.clearTimeout(projectNameTickTimer.current);
      projectNameTickTimer.current = window.setTimeout(() => setProjectNameSavedTick(false), 2000);
      setProjectNameEditing(false);
    } catch {
      setProjectNameError("保存失败：网络错误");
    } finally {
      setProjectNameSaving(false);
    }
  };

  const cancelProjectNameEdit = () => {
    setProjectNameEditing(false);
    setProjectNameDraft(projectDisplayName ?? "");
    setProjectNameError(null);
  };

  // 项目切换/导入后刷新名称（display_name → 源片名回退）；同时复位编辑态。
  useEffect(() => {
    setProjectNameEditing(false);
    setProjectNameError(null);
    setProjectNameSavedTick(false);
    if (activeProjectId?.startsWith("timeline_")) void refreshProjectName(activeProjectId);
    else setProjectDisplayName(null);
  }, [activeProjectId, refreshProjectName]);

  // ===== 项目管理弹层（2026-10-19）：切换/新建前的保存通道 =====
  // 「先保存当前编辑」的唯一落盘点：时间线视图有 300ms 防抖 PUT，切换前立即 flush；
  // 变换/核听修订按既有契约只在显式 Apply/CAS 保存，此处没有待落盘写。存在执行中任务
  // （自动剪辑/转写）时拒绝切换——openHistoryProject 会重置工作区，不能让任务失去归属。
  // 返回 false：弹层保持打开并提示，当前项目与编辑保持不动。
  const saveBeforeProjectSwitch = async (): Promise<boolean> => {
    if (autoEditing || transcriptionState === "processing") {
      setMessage("当前有任务执行中：请等待任务完成或取消后再切换项目。");
      return false;
    }
    if (!timelineProjectId || timelineViewHydratedRef.current !== timelineProjectId) return true;
    const viewPayload = {
      zoom_px_per_second: timelineView.zoom_px_per_second,
      audio_track_height: timelineView.audio_track_height,
      waveform_ratio: timelineView.waveform_ratio,
      scroll_left: timelineView.scroll_left,
    };
    const serialized = JSON.stringify(viewPayload);
    if (timelineViewSavedRef.current === serialized) return true;
    if (timelineViewSaveTimer.current) { window.clearTimeout(timelineViewSaveTimer.current); timelineViewSaveTimer.current = null; }
    timelineViewSavedRef.current = serialized;
    try {
      await saveTimelineViewPatch(transcribeServiceBase, timelineProjectId, viewPayload);
      return true;
    } catch {
      setMessage("保存当前项目视图失败：已保留当前项目，请稍后重试。");
      return false;
    }
  };
  // 打开已有项目（项目管理弹层，2026-10-19）：先只读预检（GET /studio/timeline/{id}，
  // 不触发上传/ASR/纠错/渲染），失败 → 返回 false（弹层保持打开并提示，当前项目不动）；
  // 通过后 openHistoryProject 绑定+时间线，再复用「返回原片」链路（selectSourceMedia）
  // 载入源视频素材、纠错转写凭据/稿与画面尺寸——全部纯 GET 读取。成片卡由
  // activeProjectId 既有效应拉取台账（loadProjectCards），当前有效成片即在素材区可播。
  const openProjectFromManager = async (projectId: string, meta?: { hasSource?: boolean }): Promise<boolean> => {
    try {
      const response = await fetch(`${transcribeServiceBase}/studio/timeline/${encodeURIComponent(projectId)}`, { cache: "no-store" });
      if (!response.ok) { setMessage(`打开项目失败（HTTP ${response.status}）：已保留当前项目。`); return false; }
      const payload = await response.json() as { id?: string; source?: { name?: string } };
      if (payload?.id !== projectId || !payload?.source?.name) {
        setMessage("打开项目失败：项目或源片信息不匹配，已保留当前项目。");
        return false;
      }
    } catch {
      setMessage("打开项目失败：网络错误，已保留当前项目。");
      return false;
    }
    openHistoryProject(projectId);
    await selectSourceMedia(projectId);
    // 三态之三（工程在、源片缺失）：允许打开，但明确提示重新关联原视频；成片仍可播放/下载。
    if (meta?.hasSource === false) {
      setMessage("已打开项目：原视频文件缺失，无法播放原片。请重新关联源视频；已有成片仍可播放与下载。");
    }
    return true;
  };
  // 新建项目：与 removeFile 同一套纯客户端清空（不调用后端 cancel/delete）；真正的项目
  // 在导入素材时诞生（chooseFile 生成新 project id）。
  const newProjectFromManager = () => {
    resetWorkflowSession();
    if (previewUrl && !activeRenderedClip) URL.revokeObjectURL(previewUrl);
    setFile(null); setPreviewUrl(null); setDuration(0); setCurrentTime(0); setSegments([]); setTimeline(null); setInsights(null); setAutoEditTask(null); setAutoEditing(false); setRenderedClips([]); setActiveRenderedClip(null); sourceWorkspaceRef.current = null; setSelectedSegment(null); setTranscriptionState("idle"); setMessage(""); setPreviewRotation(0);
    setClipTransforms({}); setRangeTransforms({}); setTransformMode(false); setVideoDimensions({ width: 1080, height: 1920 });
    setProjectIdRef(generateProjectId()); setTranscriptRef(null);
    setMessage("已切换到新项目：导入素材后自动创建项目。");
  };
  // 时长就绪后内容宽度才变得可滚动：把恢复的 scroll_left 再落一次 DOM。
  // （组件只负责「视口宽度观测 + 按属性写回」；媒体元数据晚于视图恢复到达时，
  //  首次写回会被 clamp 成 0，这里在 duration 到达后补一次。）
  useEffect(() => {
    const element = timelineScrollRef.current;
    if (!element || !duration) return;
    const target = timelineView.scroll_left ?? 0;
    if (Math.abs(element.scrollLeft - target) > 0.5) element.scrollLeft = target;
  }, [duration, timelineView.scroll_left]);
  const beginTimelineScrub = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!duration) return;
    // 音轨行内不启动容器级擦洗：行内点击/双击（删除音轨）交给行自身的原生事件，
    // 避免 setPointerCapture 把后续 pointer/dblclick 重定向到容器。
    if ((event.target as HTMLElement).closest?.('[data-testid^="timeline-audio-lane-"]')) return;
    scrubbingRef.current = true;
    setScrubbing(true);
    event.currentTarget.setPointerCapture(event.pointerId);
    seekFromTimelinePointer(event);
  };
  const moveTimelineScrub = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (scrubbingRef.current) seekFromTimelinePointer(event);
  };
  const endTimelineScrub = (event: ReactPointerEvent<HTMLDivElement>) => {
    scrubbingRef.current = false;
    setScrubbing(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };
  // 转写文字只读化：原 updateText（逐句改写转写文本的编辑入口）已随 textarea 一并移除；
  // 说话人标签属于元数据，set_speaker / 下拉切换保留。
  const updateSpeaker = (index: number, speaker: SpeakerRole) => setSegments((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, speaker } : item));
  const ranges = useMemo(
    () => timeline?.timeline.ranges || (duration ? [{ id: "source", source_start: 0, source_end: duration }] : []),
    [timeline, duration],
  );
  // 当前生效的片段变换：成片视图按 task:clip 键取；原始视频视图取播放头所在的
  // 时间线片段（越界回退第一段）。查看缩放 zoom 不参与这里。
  const activeClipKey = activeRenderedClip ? clipTransformKey(activeRenderedClip) : null;
  const activeRangeIndex = useMemo(() => {
    if (activeRenderedClip) return -1;
    const hit = ranges.findIndex((range) => currentTime >= range.source_start && currentTime < range.source_end);
    return hit >= 0 ? hit : 0;
  }, [ranges, currentTime, activeRenderedClip]);
  const inspectorTarget = `${workspaceBindingRef.current}:${activeRenderedClip ? "clip" : `${ranges[activeRangeIndex]?.id || "source"}:${activeRangeIndex}`}`;
  inspectorSelectionRef.current = inspectorTarget;
  const appliedTransform = useMemo(() => activeClipKey
    ? clipTransforms[activeClipKey] ?? defaultTransform()
    : rangeTransforms[activeRangeIndex] ?? defaultTransform(), [activeClipKey, clipTransforms, activeRangeIndex, rangeTransforms]);
  const activeTransform = inspectorPreview?.binding === inspectorTarget ? inspectorPreview.transform
    : transformMode && transformDraft ? transformDraft : appliedTransform;
  const restoreInspectorTransform = (next: ClipTransform) => {
    if (!activeClipKey) return;
    setClipTransforms((old) => JSON.stringify(old[activeClipKey]) === JSON.stringify(next) ? old : { ...old, [activeClipKey]: next });
  };
  const previewInspectorTransform = (next: ClipTransform) => {
    if (inspectorApplyInFlight.current.has(inspectorTarget)) return;
    setInspectorPreview({ binding: inspectorTarget, transform: next });
  };
  const activeBackground = inspectorBackgroundPreview?.binding === inspectorTarget ? inspectorBackgroundPreview.background : transformBackground;
  const previewInspectorBackground = (background: BackgroundMode) => { if (!inspectorApplyInFlight.current.has(inspectorTarget)) setInspectorBackgroundPreview({ binding: inspectorTarget, background }); };
  const cancelInspectorTransform = () => { if (inspectorApplyInFlight.current.has(inspectorTarget)) return; setInspectorPreview(null); setInspectorBackgroundPreview(null); };
  // 草稿是否与进入模式时的快照不同（决定「应用」是否可点、是否提示未应用）。
  const transformDirty = transformMode && transformDraft != null && transformSnapshotRef.current != null
    && JSON.stringify(transformPayload(transformDraft)) !== JSON.stringify(transformPayload(transformSnapshotRef.current));
  const transformLocked = previewRotation !== 0;
  const isGoldenSegment = (segment: Segment) => insights?.golden_sentences.some((sentence) => segment.end > sentence.start && sentence.start < sentence.end) || false;
  const currentRemoteSource = remoteSource?.projectId === activeProjectId ? remoteSource : null;
  const activeMediaName = activeRenderedClip?.name || file?.name || currentRemoteSource?.name || "等待导入视频";
  // ===== TimelineTracks 的派生入参 =====
  // 音轨只在「项目原始时间线视图」显示 revision 的音轨（origin=source 的原声轨
  // 由导入自动进入，＋ 菜单里只给置灰说明项）。
  const timelineAudioTracks = projectTimelineActive ? revisionAudioTracks : [];
  const hasSourceTrack = timelineAudioTracks.some((track) => track.origin === "source");
  const timelineSegmentCues = useMemo(
    () => segments.map((segment, index) => ({ segment_id: `seg-${index}`, start: segment.start, end: segment.end, text: segment.text })),
    [segments],
  );
  const timelineCues: TimelineSubtitleCue[] = inspectorCues ?? timelineSegmentCues;
  const timelineGoldenSegments = useMemo(
    () => segments.reduce<number[]>((ids, segment, index) => (isGoldenSegment(segment) ? [...ids, index] : ids), []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [segments, insights],
  );
  // ===== 时间线图片素材（真正的时间线拖拽）=====
  // 事实源 = 台账 card_view.timeline_image_clips（本页从不本地合并实例；拖拽 PUT/删除
  // 成功后统一 loadProjectCards 刷新，避免乐观实例与台账漂移）。
  const timelineImageClips = useMemo(
    () => (activeCard?.timeline_image_clips ?? []).map((item) => ({
      id: String(item.id),
      role: String(item.role || "image"),
      start_s: Number(item.start_s) || 0,
      duration_s: Number(item.duration_s) || 1.5,
    })),
    [activeCard],
  );
  // 播放预览与图片轨一致：播放头落进实例窗口 [start, start+duration) → 画面显示封面
  // 静帧、音频静音（导出语义的预览近似；见 timeline-image-preview.tsx 注释）。
  const activeTimelineImage = useMemo(
    () => findActiveTimelineImage(timelineImageClips, currentTime),
    [timelineImageClips, currentTime],
  );
  const handleTimelineDropCoverImage = useCallback(async (
    startSeconds: number,
    payload: { clip_id?: string | null; instance_id?: string | null },
  ) => {
    const projectId = activeProjectId;
    const clip = activeRenderedClip;
    if (!projectId || !clip) { setMessage("先选中一条剪辑成片，再把封面拖进时间线。"); return; }
    if (payload.clip_id && payload.clip_id !== clip.clip_id) {
      setMessage("拖入的封面属于另一条成片；请先在左侧选中那条成片再拖。");
      return;
    }
    const start = Math.max(0, Math.round(startSeconds * 100) / 100);
    const moving = payload.instance_id
      ? timelineImageClips.find((item) => item.id === payload.instance_id) ?? null : null;
    const durationS = moving ? moving.duration_s : 1.5;
    try {
      const response = await fetch(`${studioBase}/studio/projects/${encodeURIComponent(projectId)}/clips/${encodeURIComponent(clip.clip_id)}/timeline-image`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ duration_s: durationS, start_s: start, instance_id: payload.instance_id ?? null }),
      });
      const text = await response.text();
      const payloadBody = text ? JSON.parse(text) as { detail?: { message?: string } | string } : null;
      if (!response.ok) {
        const detail = payloadBody?.detail;
        throw new Error(typeof detail === "object" && detail?.message ? detail.message : "封面进时间线失败");
      }
      await loadProjectCards(projectId);
      setMessage(payload.instance_id
        ? `时间线封面已移动到 ${formatTime(start)}`
        : start <= 0.001
          ? "封面已放到片头（导出时前置并替换内嵌片头）"
          : `封面静帧已插入 ${formatTime(start)}（导出时正片在此让位，段内静音）`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "封面进时间线失败");
    }
  }, [activeProjectId, activeRenderedClip, timelineImageClips, loadProjectCards]);
  const handleTimelineRemoveImageClip = useCallback(async (instanceId: string) => {
    const projectId = activeProjectId;
    const clip = activeRenderedClip;
    if (!projectId || !clip) return;
    try {
      const response = await fetch(`${studioBase}/studio/projects/${encodeURIComponent(projectId)}/clips/${encodeURIComponent(clip.clip_id)}/timeline-image?instance_id=${encodeURIComponent(instanceId)}`, { method: "DELETE" });
      if (!response.ok) throw new Error("时间线封面删除失败");
      await loadProjectCards(projectId);
      setMessage("已从时间线移除封面静帧（封面素材文件不受影响）");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "时间线封面删除失败");
    }
  }, [activeProjectId, activeRenderedClip, loadProjectCards]);
  // 顶栏门禁（任务拆分后）：开始自动剪辑只在「转写 job 已完成（有 transcriptRef 凭据）+
  // 有分段 + 项目还没有成片卡片 + 没有完成过的自动剪辑任务」时可用；一旦产出成片就
  // 永久禁用，后续修改一律走卡片的局部任务。timeline 不再参与门禁——它要等自动剪辑
  // job 完成才会出现。
  const hasCompletedEdit = autoEditCompleted || renderedClipList.length > 0;
  const autoEditDisabled = !transcriptRef || !segments.length || autoEditing || !!activeRenderedClip || hasCompletedEdit;
  // 重新剪辑凭据：会话 transcriptRef 优先，否则按项目从转写存储找回的最新版。
  const reeditCredential = transcriptRef ?? projectTranscriptRef;
  const autoEditDisabledReason = hasCompletedEdit
    ? "已生成成片；修改请用卡片菜单的局部任务（封面/标题/字幕/时间线），不要重跑自动剪辑"
    : !transcriptRef || !segments.length
      ? "请先完成转写与纠错。"
      : activeRenderedClip
        ? "请先在左侧选择原始视频，避免在成片视图里启动新的自动剪辑"
        : "开始自动剪辑";

  // ===== 播放器外壳交互（只影响观看，不改变持久化与导出契约）=====
  // 音量/静音只作用于本地预览试听，不写入片段、不进入导出参数。
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    video.volume = previewVolume;
    // 时间线封面静帧窗口内强制静音（导出时该段本就无声）；离开窗口恢复用户设置。
    video.muted = previewMuted || Boolean(activeTimelineImage);
  }, [previewVolume, previewMuted, previewUrl, transformMode, activeTimelineImage]);
  // Esc = 取消：回滚到进入裁切/重构图时的快照，不产生任何写请求。
  useEffect(() => {
    if (!transformMode) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setTransformMode(false);
      setTransformDraft(null);
      transformSnapshotRef.current = null;
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [transformMode]);
  // 退出裁切/重构图后草稿与快照一并清空：只有 onApply 会在清空前把草稿落盘，
  // 因此「取消 / Esc / 再点一次按钮」都不写后端。
  useEffect(() => {
    if (transformMode) return;
    setTransformDraft(null);
    transformSnapshotRef.current = null;
  }, [transformMode]);
  // 浮层高度实测（只用于画布预留观看空间，不参与任何变换计算）。
  useEffect(() => {
    if (!transformMode) return undefined;
    const el = transformFloatRef.current;
    if (!el) return undefined;
    const observer = new ResizeObserver((entries) => {
      const height = entries[0]?.contentRect.height;
      if (height) setTransformFloatHeight(Math.round(height));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [transformMode]);
  // 进入裁切/重构图：先对当前 transform 做快照，草稿从此快照开始。
  // 退出（按钮 / Esc / 取消）一律回滚快照；只有 applyTransform 会落盘。
  const enterOrExitTransformMode = () => {
    if (transformLocked || !previewUrl) return;
    if (transformMode) { cancelTransform(); return; }
    // 进入时自动选中播放头所在的时间线片段（activeRangeIndex 已按播放头推导）。
    transformSnapshotRef.current = activeTransform;
    setTransformDraft(activeTransform);
    setTransformMode(true);
  };
  // 应用：把草稿交给既有持久化（成片 → PUT transform；时间线片段 → PUT /studio/timeline）。
  const applyTransform = () => {
    const draft = transformDraft;
    setTransformMode(false);
    setTransformDraft(null);
    transformSnapshotRef.current = null;
    if (draft) commitTransform(draft);
  };
  // 取消：回滚到快照，不写后端。
  const cancelTransform = () => {
    setTransformMode(false);
    setTransformDraft(null);
    transformSnapshotRef.current = null;
  };
  const fitViewerZoom = () => setViewerZoom(FIT_VIEWER_ZOOM);
  // ===== Inspector「封面与画面」的应用/取消（契约 §5 PUT /studio/clips/{p}/{c}/config）=====
  // 语义与既有实现一致：面板里的改动只写本地草稿（预览实时），
  // 「应用」才同时走两处持久化 —— 既有 sidecar/EDL 通道 + 合同 config 端点
  // （非默认变换才有可观测的 PUT body）；「取消」不写任何后端。
  const applyInspectorTransform = async (next: ClipTransform, background: BackgroundMode = transformBackground) => {
    const projectId = activeProjectId, clipId = inspectorClipId, clip = activeRenderedClip;
    const generation = workflowGenerationRef.current;
    const binding = inspectorTarget, rangeIndex = activeRangeIndex, sourceTimeline = timeline;
    if (!projectId?.startsWith("timeline_") || !clipId) throw new Error("当前素材尚未绑定可保存的项目。");
    if (!clip && (sourceTimeline?.id !== projectId || !sourceTimeline.timeline.ranges[rangeIndex])) throw new Error("当前原片时间线尚未就绪，请稍后重试。");
    if (inspectorApplyInFlight.current.has(binding)) throw new Error("画面设置正在保存，请等待完成。");
    inspectorApplyInFlight.current.add(binding);
    try {
    const payload = isDefaultTransform(next) ? null : transformPayload(next);
    await putClipConfig(transcribeServiceBase, projectId, clipId, { transform: payload });
    // 两个既有PUT不是事务；第二步失败保留草稿并明示部分保存，不盲回滚配置。
    const syncRender = async (url: string, body: unknown, label: string) => {
      try {
        const response = await fetch(url, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
      } catch (error) {
        throw new Error(`部分保存：片段配置已写入，${label}未完成，请重试应用。${error instanceof Error ? error.message : "同步失败"}`);
      }
    };
    if (clip) {
      await syncRender(`${autoEditBaseUrl}/${clip.task_id}/clips/${clip.clip_id}/transform`, { transform: payload }, "渲染变换");
      if (mountedRef.current && generation === workflowGenerationRef.current && workspaceProjectRef.current === projectId) setClipTransforms((old) => ({ ...old, [clipTransformKey(clip)]: next }));
    } else if (sourceTimeline?.id === projectId) {
      const ranges = sourceTimeline.timeline.ranges.map((range, index) => index === rangeIndex ? { ...range, transform: payload } : range);
      await syncRender(`${transcribeServiceBase}/studio/timeline/${encodeURIComponent(projectId)}`, { ranges }, "原片时间线");
      if (mountedRef.current && generation === workflowGenerationRef.current && inspectorSelectionRef.current === binding) {
        setRangeTransforms((old) => ({ ...old, [rangeIndex]: next }));
        setTimeline({ ...sourceTimeline, timeline: { ranges } });
      }
    }
    if (mountedRef.current && generation === workflowGenerationRef.current && inspectorSelectionRef.current === binding) { setTransformBackground(background); setInspectorPreview(null); setInspectorBackgroundPreview(null); }
    } finally { inspectorApplyInFlight.current.delete(binding); }
  };
  // 原始比例：完整显示素材原始画面（解除裁剪 + contain）。属于片段设置，影响导出。
  const applyOriginalRatio = () => updateActiveTransform({ ...defaultTransform(), fitMode: "contain" });
  // 铺满画布：保留当前裁剪，片段缩放归 100% 且位置归零。属于片段设置，影响导出。
  const fillCanvasWithClip = () => updateActiveTransform({ ...activeTransform, fitMode: "cover", scale: 1, positionX: 0, positionY: 0 });
  // 重置变换：清除当前片段的裁剪、缩放与位置。属于片段设置，影响导出。
  const resetActiveTransform = () => updateActiveTransform(defaultTransform());
  // 逐帧微调：只移动播放头，不写任何片段设置。
  const seekFrame = (direction: -1 | 1) => seek(Math.max(0, Math.min(duration || 0, currentTime + direction / 30)));
  const togglePreviewFullscreen = async () => {
    const card = playerCardRef.current;
    if (!card) return;
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await card.requestFullscreen();
    } catch {
      /* 浏览器拒绝全屏（权限/嵌入式预览）时保持原状，不影响播放。 */
    }
  };
  // 面板上的片段标签：显示自动选中的那一段，让「选中片段」可见。
  const activeClipLabel = activeRenderedClip
    ? `${activeRenderedClip.name}`
    : ranges.length
      ? `片段 ${activeRangeIndex + 1}/${ranges.length} · ${formatTime(ranges[activeRangeIndex]?.source_start ?? 0)}—${formatTime(ranges[activeRangeIndex]?.source_end ?? 0)}`
      : "未选中片段";

  // 原始素材卡状态：只依据现有状态变量推导（transcriptionState / segments），不编造上传字段。
  // 导入后视频仅在浏览器本地（上传发生在「开始转写」内），因此未转写时是「待转写」而不是「已上传」。
  const sourceStatus = transcriptionState === "processing"
    ? { label: "转写中", tone: "busy", hint: "正在上传并转写这条原始素材" }
    : transcriptionState === "error"
      ? { label: "转写失败", tone: "error", hint: message || "转写没有完成，可在右侧重新发起转写" }
      : segments.length > 0
        ? { label: "转写完成", tone: "done", hint: "已生成带时间戳的转写，可在右侧逐句校对" }
        : { label: "待转写", tone: "idle", hint: "素材已在本地导入，点击右侧「开始转写」生成时间戳" };

  return <main className="editor-shell">
    <header className="editor-topbar">
      {/* 精简顶栏（v2）：左侧只有菜单图标 + 标题，右侧只有「已保存 / 开始自动剪辑 / 导出成片 / 设置」。
          文件·编辑·视图·工具·帮助菜单、规则中心入口、规则版本横幅与「按修订规则重新生成」一律移出顶栏；
          规则版本信息改由成片卡片、剪辑助手执行结果与规则快照面板呈现（顶栏不再承载规则提示）。 */}
      <button className="top-icon" aria-label="项目管理" title="项目管理" onClick={() => setProjectManagerOpen(true)}><House size={22} /></button><strong>AI 视频剪辑工作台</strong>
      {activeProjectId?.startsWith("timeline_") && projectDisplayName && (projectNameEditing
        ? <span className="project-name-edit">
            <input value={projectNameDraft} maxLength={60} autoFocus aria-label="项目名称" disabled={projectNameSaving}
              onChange={(e) => { setProjectNameDraft(e.target.value); setProjectNameError(null); }}
              onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void saveProjectName(); } else if (e.key === "Escape") { e.preventDefault(); cancelProjectNameEdit(); } }} />
            <button className="top-icon" aria-label="保存名称" title="保存（Enter）" disabled={projectNameSaving} onClick={() => void saveProjectName()}>{projectNameSaving ? <LoaderCircle size={15} /> : <Check size={15} />}</button>
            <button className="top-icon" aria-label="取消编辑" title="取消（Esc）" disabled={projectNameSaving} onClick={cancelProjectNameEdit}><X size={15} /></button>
            {projectNameError && <span className="project-name-error" role="alert">{projectNameError}</span>}
          </span>
        : <button className="project-name-label" title="点击重命名项目" onClick={() => { setProjectNameDraft(projectDisplayName); setProjectNameError(null); setProjectNameEditing(true); }}>
            {projectNameSavedTick ? <><Check size={13} />已保存</> : projectDisplayName}
          </button>)}
      <div className="top-actions"><span className="save-state"><Check size={15} />{transcriptionState === "processing" ? "正在处理" : "已保存"}</span><button className="auto-edit-button" onClick={startAutoEdit} disabled={autoEditDisabled} aria-label={autoEditDisabledReason} title={autoEditDisabledReason}>{autoEditing ? <LoaderCircle size={16} /> : <Scissors size={16} />}{autoEditing ? "自动剪辑中" : "开始自动剪辑"}</button><button className="export-button" onClick={downloadRenderedClip} disabled={!activeRenderedClip} aria-label="导出成片" title={activeRenderedClip ? "导出当前成片" : "请先选择一个成片"}><Upload size={16} />导出成片</button>{activeRenderedClip?.subtitle_url && <a className="export-button" href={workflowArtifactUrl(transcribeServiceBase, activeRenderedClip.subtitle_url)} download target="_blank" rel="noreferrer" aria-label="下载SRT字幕"><Download size={16} />下载SRT</a>}{cleanupRetryRun && <button className="top-icon danger" aria-label="重试清理旧切片" title="清理未完成，可重试（不会重新规划或渲染）" onClick={() => void retryReeditCleanup()} disabled={reeditBusy}><Trash2 size={18} /></button>}{(activeProjectId?.startsWith("timeline_") || renderedClipList.length > 0) && (cardCount > 0 || renderedClipList.length > 0) && <button className="top-icon" aria-label="重新剪辑并替换旧切片" title={reeditCredential ? "复用源视频和转写重新剪辑；新切片成功后旧切片永久删除" : "该项目还没有已纠错转写：请先完成一次转写与纠错"} disabled={reeditBusy || autoEditing} onClick={() => { setReeditConfirmOpen(true); setReeditDialogState({ status: "loading" }); void resolveReeditCredential(); }}><Scissors size={18} /></button>}{activeProjectId?.startsWith("timeline_") && cardCount > 0 && <button className="top-icon danger" onClick={() => void requestClearProjectData(activeProjectId)} disabled={clearBusy} aria-label="清空任务数据" title="清空当前项目的任务卡片与产物（保留源片与转写），需二次确认"><Trash2 size={18} /></button>}<button className="top-icon" aria-label="设置" title="设置"><Settings2 size={18} /></button></div>
    </header>
    {/* 全局状态提示（2026-10-21）：此前 setMessage 只写进转写占位，封面应用/导出失败在
        界面上完全看不到（用户只看到「点了没反应」）。这里统一把操作结果与失败原因显示出来，
        错误不再静默；可手动关闭，新消息会覆盖旧消息。 */}
    {message && (
      <div className="studio-message" role="status" aria-live="polite" data-testid="studio-message">
        <span>{message}</span>
        <button type="button" aria-label="关闭提示" title="关闭提示" onClick={() => setMessage("")}><X size={12} /></button>
      </div>
    )}
    {/* 成片卡缩略图大图预览：真实比例 contain，Esc/背景点击关闭。 */}
    {coverBigPreview && <CoverLightbox url={coverBigPreview.url} name={coverBigPreview.name}
      onClose={() => setCoverBigPreview(null)} />}
    {/* 清空任务数据：二次确认卡。刻意不做「刷新即删」——落盘台账的删除不可撤销。 */}
    {reeditConfirmOpen && <div className="clear-confirm-backdrop" role="dialog" aria-modal="true" aria-label="确认重新剪辑并替换旧切片">
      <section className="clear-confirm-card">
        <h3>重新剪辑并替换旧切片</h3>
        <p>将<b>复用当前源视频和已完成的转写</b>重新执行内容分析、切片规划与渲染，不重新上传、不重新转写、不重新纠错。</p>
        <p>新切片成功后，<b>旧切片及其封面、字幕、缩略图等衍生产物将被永久删除</b>，素材区只保留新切片。</p>
        <p className="clear-confirm-warning"><b>源视频和转写数据不会删除</b>，始终保留。确认后将自动执行到底，不再出现中间确认。</p>
        {reeditDialogState.status === "missing" && <p className="clear-confirm-warning">未找到该项目的已纠错转写：请先对该素材完成一次「开始转写与纠错」，再回来重新剪辑。</p>}
        <div className="clear-confirm-actions">
          <button type="button" onClick={() => setReeditConfirmOpen(false)} disabled={reeditBusy}>关闭</button>
          <button type="button" onClick={() => { setReeditConfirmOpen(false); void startReedit(); }} disabled={reeditBusy || reeditDialogState.status !== "ready"}>{reeditBusy ? "重新剪辑中…" : "确认重新剪辑"}</button>
        </div>
      </section>
    </div>}
    {clearDialog && <div className="clear-confirm-backdrop" role="dialog" aria-modal="true" aria-label="确认清空任务数据">
      <section className="clear-confirm-card">
        <header><Trash2 size={16} /><strong>清空当前项目的任务数据</strong></header>
        <p>将删除 <b>{clearDialog.cardCount}</b> 张任务卡片、<b>{clearDialog.taskCount}</b> 个任务记录与产物{clearDialog.reclaimableBytes > 0 ? <>（约 <b>{(clearDialog.reclaimableBytes / 1024 / 1024).toFixed(1)} MB</b>）</> : null}；<b>源片与已完成的转写会保留</b>，可重新剪辑。</p>
        <p className="clear-confirm-warning">删除不可撤销，项目历史卡片与成片文件将一并移除。</p>
        <div className="clear-confirm-actions">
          <button type="button" onClick={() => setClearDialog(null)} disabled={clearBusy}>取消</button>
          <button type="button" className="danger" onClick={() => void confirmClearProjectData()} disabled={clearBusy}>{clearBusy ? <LoaderCircle size={15} /> : <Trash2 size={15} />}{clearBusy ? "正在清空" : "确认清空"}</button>
        </div>
      </section>
    </div>}
    <section className={copilotCollapsed ? "editor-main copilot-collapsed" : "editor-main"}>
      <StudioCopilot
        messages={copilotMessages}
        busy={copilotBusy}
        collapsed={copilotCollapsed}
        onToggle={() => setCopilotCollapsed((value) => !value)}
        onSend={(text) => void sendCopilot(text)}
        onRunAction={(action) => handleCopilotConfirm(action)}
        onDismissAction={handleCopilotDismiss}
      />
      <aside className="media-browser">
        <div className="pane-title"><strong>项目素材</strong><button aria-label="切换视图"><ListVideo size={17} /></button></div>
        <button className="import-button" onClick={() => inputRef.current?.click()}><Plus size={19} />上传新视频</button>
        <input ref={inputRef} type="file" accept="video/*" hidden onChange={(event: ChangeEvent<HTMLInputElement>) => chooseFile(event.target.files?.[0])} />
        <label className="asset-search"><Search size={16} /><input placeholder="搜索素材…" /></label>
        <div className="asset-body"><div className="asset-list">{/* 分区标题取代了原来的竖向分类导航（全部/视频/音频/图片/字幕）；空分类不再渲染。 */}
          <section className="asset-section">
            <p className="asset-section-label">原始素材（{file || currentRemoteSource ? 1 : 0}）</p>
            {/* 本地导入或显式返回成功的当前项目源片可见；不伪造上传 File。 */}
            {!file && !currentRemoteSource && <div className="asset-empty"><FolderOpen size={25} /><p>导入视频后在这里管理素材</p></div>}
            {(file || currentRemoteSource) && <>
            <article className={`asset-card source-card ${!activeRenderedClip ? "active" : ""}`} onClick={() => void selectSourceMedia()} role="button" tabIndex={0} onKeyDown={(event) => { if (event.key === "Enter") selectSourceMedia(); }}>
              <video src={sourceWorkspaceRef.current?.previewUrl || previewUrl || undefined} muted />
              <div className="asset-card-body">
                <strong title={file?.name || currentRemoteSource?.name}>{file?.name || currentRemoteSource?.name}</strong>
                <span className="asset-card-sub"><span className="asset-card-duration">{sourceWorkspaceRef.current?.duration || duration ? formatTime(sourceWorkspaceRef.current?.duration || duration) : "读取媒体中…"}</span><em className={`asset-status ${sourceStatus.tone}`} title={sourceStatus.hint}>{sourceStatus.label}</em></span>
              </div>
              <button onClick={(event) => { event.stopPropagation(); removeFile(); }} aria-label="移除素材" title="移除素材"><X size={14} /></button>
            </article>
            </>}
          </section>
          <section className="asset-section">
            <p className="asset-section-label rendered">剪辑成片（{cardCount || renderedClipList.length}）</p>
            {renderedClipList.length === 0 && <p className="asset-subsection-empty">自动剪辑完成后，成片会显示在这里。</p>}
            {renderedClipList.length > 0 && <>
            {/* 卡片按**稳定 clip_id** 渲染（dedupeByClipId）：封面/标题/字幕/时间线局部任务
                只原地刷新同一张卡，绝不新增卡片 —— 不再按任务聚合。 */}
            {renderedClipList.map((clip) => {
              const card = projectCards.find((item) => item.clip_id === clip.clip_id) || null;
              const versionLabel = coverVersionLabel(clip);
              const coverPath = coverPathOf(clip, card);
              const coverExport = clipCoverExportUrl(clip, card);
              const canUndoCover = cardCoverCanUndo(clip);
              const menuOpen = openCardMenu === clip.clip_id;
              // 素材卡缩略图（2026-10-21 阶段）= 切片视频真实第一帧（后端从 clip_final.mp4
              // 抽第 0 帧）：不再用生成封面冒充。项目非 timeline_* 或提取失败 → 占位图标。
              const firstFrameSrc = clipFirstFrameUrl(activeProjectId, clip.clip_id);
              const firstFrameBroken = Boolean(firstFrameFailures[clip.clip_id]);
              return <article className={`asset-card rendered-card ${activeRenderedClip?.clip_id === clip.clip_id ? "active" : ""}`} key={clip.clip_id} onClick={() => selectRenderedClip(clip)} role="button" tabIndex={0} onKeyDown={(event) => { if (event.key === "Enter") selectRenderedClip(clip); }}>
                {/* 紧凑横向卡（2026-10-20 二次统一）：左侧固定宽 88px 9:16 缩略图（contain 防拉伸、
                    深色背景填充），点击缩略图才打开封面大图；右侧信息区随栏宽伸缩。 */}
                <div className="rendered-cover" role="button" tabIndex={-1}
                  title={coverPath ? "点击查看封面大图；拖到下方时间线「图片」轨可插入封面静帧" : "暂无封面"}
                  aria-label={`${clip.name}封面大图预览`}
                  draggable={Boolean(coverPath)}
                  onDragStart={(event) => {
                    if (!coverPath) return;
                    event.dataTransfer.setData(TIMELINE_COVER_DRAG_TYPE, JSON.stringify({ clip_id: clip.clip_id }));
                    event.dataTransfer.effectAllowed = "copy";
                  }}
                  onClick={(event) => {
                    event.stopPropagation();
                    if (coverPath) setCoverBigPreview({ url: `${artifactUrl(clip.task_id, coverPath)}&v=${coverVersion}`, name: clip.name });
                  }}>
                  {firstFrameSrc && !firstFrameBroken
                    ? <img src={firstFrameSrc} alt={`${clip.name}第一帧`} loading="lazy"
                        onError={() => setFirstFrameFailures((old) => (old[clip.clip_id] ? old : { ...old, [clip.clip_id]: true }))} />
                    : <Film size={24} />}
                  {versionLabel && <em className="rendered-version" title={`封面版本 ${versionLabel}（封面资产 ${card?.cover?.cover_asset_id || "未知"}）`}>{versionLabel}</em>}
                </div>
                <div className="rendered-info">
                <button type="button" className="rendered-menu-toggle" aria-label={`${clip.name}更多操作`} aria-expanded={menuOpen} title="更多操作：播放 / 导出 / 重新制作封面 / 编辑标题 / 查看历史 / 删除" onClick={(event) => { event.stopPropagation(); setOpenCardMenu(menuOpen ? null : clip.clip_id); }}><Menu size={14} /></button>
                <strong title={card?.title || clip.topic}>{clip.name}</strong>
                <span>{formatTime(clip.duration)} · {card?.status || "ready"}{clip.topic ? ` · ${clip.topic}` : ""}{clip.warnings?.length ? ` · ⚠ ${clip.warnings.length} 项警告` : ""}</span>{/* 规则版本可见性（Phase B）：这条成片当时用的 workflow / skill 版本；旧成片显示「未知（无规则快照）」。 */}<small className="rendered-rules" title="本次成片实际使用的规则版本（来自项目台账）。点击查看规则快照：workflow 版本、各 skill 版本与生效规则。" onClick={(event) => { event.stopPropagation(); void viewClipRules(clip); }}>{workflowVersionLine(clip)}</small>
                {menuOpen && <div className="rendered-menu" role="menu" onClick={(event) => event.stopPropagation()}>
                  <button type="button" role="menuitem" onClick={(event) => { event.stopPropagation(); setOpenCardMenu(null); void playRenderedClip(clip); }}>播放</button>
                  <button type="button" role="menuitem" onClick={(event) => { event.stopPropagation(); setOpenCardMenu(null); triggerArtifactDownload(clipExportUrl(clip)); }}>导出</button>
                  {coverExport && <button type="button" role="menuitem" onClick={(event) => { event.stopPropagation(); setOpenCardMenu(null); triggerArtifactDownload(coverExport); }}>导出封面</button>}
                  <button type="button" role="menuitem" disabled={coverBusy || localTaskBusy} title={`按当前封面风格（${COVER_STYLES.find((item) => item.id === coverStyle)?.name || coverStyle}）重新生成封面：只换封面图与 0.8s 片头，不重剪正片、不新增卡片。`} onClick={(event) => { event.stopPropagation(); setOpenCardMenu(null); void regenerateCoverForClip(clip, coverStyle).catch((error) => setMessage(error instanceof Error ? error.message : "封面重制失败")); }}>重新制作封面</button>
                  <button type="button" role="menuitem" onClick={(event) => { event.stopPropagation(); setOpenCardMenu(null); const title = promptClipTitle(clip); if (title === null) return; void updateClipTitle(clip, title || null).catch((error) => setMessage(error instanceof Error ? error.message : "标题更新失败")); }}>编辑标题</button>
                  <button type="button" role="menuitem" onClick={(event) => { event.stopPropagation(); setOpenCardMenu(null); void toggleCoverHistory(clip); }}>查看历史</button><button type="button" role="menuitem" onClick={(event) => { event.stopPropagation(); setOpenCardMenu(null); void viewClipRules(clip); }} title="查看这条成片（渲染任务）当时的规则快照：workflow 版本、各 skill 版本、生效规则；旧成片没有快照时会明确说明。">查看本次规则</button><button type="button" role="menuitem" disabled={localTaskBusy} onClick={(event) => { event.stopPropagation(); setOpenCardMenu(null); void regenerateClipByLatestRules(clip, "cover").then((text) => setWorkflowMessage(text)).catch((error) => setWorkflowMessage(error instanceof Error ? error.message : "按最新规则重新生成失败")); }} title="按当前清单里的最新规则重新生成这条成片：新增一个 revision（封面 + 0.8s 片头），旧成果保留在历史里，不会新增成片卡片。">按最新规则重新生成</button><button type="button" role="menuitem" disabled={localTaskBusy} onClick={(event) => { event.stopPropagation(); setOpenCardMenu(null); void regenerateClipByLatestRules(clip, "render").then((text) => setWorkflowMessage(text)).catch((error) => setWorkflowMessage(error instanceof Error ? error.message : "按最新规则重渲失败")); }} title="按最新规则真正重渲这条成片的画面：提交一次单切片渲染任务（render 规则来自清单快照），同样不新增卡片。">按最新规则重渲画面</button>
                  {canUndoCover && <button type="button" role="menuitem" disabled={localTaskBusy} onClick={(event) => { event.stopPropagation(); setOpenCardMenu(null); void undoClipCover(clip).catch((error) => setMessage(error instanceof Error ? error.message : "撤销封面失败")); }}>撤销到 v{card?.cover?.undo_revision ?? 1}</button>}
                  <button type="button" role="menuitem" className="danger" onClick={(event) => { event.stopPropagation(); setOpenCardMenu(null); removeCardFromView(clip); }}>删除（仅从视图移除）</button>
                </div>}
                {coverHistoryClipId === clip.clip_id && <div className="rendered-history" onClick={(event) => event.stopPropagation()}>
                  <header><span>封面历史 · {clip.name}</span><button type="button" aria-label="关闭封面历史" onClick={(event) => { event.stopPropagation(); setCoverHistoryClipId(null); }}><X size={12} /></button></header>
                  {coverHistoryLoading && <p>正在读取…</p>}
                  {!coverHistoryLoading && coverHistory.length === 0 && <p>暂无封面版本记录。</p>}
                  {!coverHistoryLoading && coverHistory.map((entry) => <p key={`${entry.revision}-${entry.created_at || ""}`}><b>v{entry.revision}</b> · {entry.kind || "未知来源"} · {entry.created_at || "时间未知"}</p>)}
                </div>}
                <a className="rendered-export" href={clipExportUrl(clip)} onClick={(event) => event.stopPropagation()}><Download size={13} />导出</a>
                </div>
              </article>;
            })}
          </>}
          </section>
        </div></div>
      </aside>
      <section className="preview-pane">
        {/* 播放器 = 独立深灰圆角卡片，三层：标题栏（56px） / 画面区（占最大空间、纯黑） / 控制栏（64px）。
            三层在卡片内是普通流式布局，控制栏永不遮挡画面（几何断言见验收脚本）。 */}
        <div className="player-card" ref={playerCardRef} data-empty={previewUrl ? "false" : "true"}>
          <PlayerTitleBar
            guides={showGuides}
            loop={loopPlayback}
            onFitView={fitViewerZoom}
            onToggleGuides={() => setShowGuides((value) => !value)}
            onToggleLoop={() => setLoopPlayback((value) => !value)}
          />
          {/* data-preview-mode 把「封面预览」与「普通视频播放」显式分开（2026-10-21 修）：
              封面是独立叠加层，内部画布恒 9:16；不参与视频画布/预览比例计算。 */}
          <div className="player-stage" data-preview-mode={(showCoverFrame || activeTimelineImage) && coverImageUrlValue ? "cover" : "video"}>
            <div className={previewRotation === 90 || previewRotation === -90 ? "preview-frame rotated-source" : "preview-frame"} onDragOver={(event) => event.preventDefault()} onDrop={(event: DragEvent<HTMLDivElement>) => { event.preventDefault(); chooseFile(event.dataTransfer.files[0]); }}>
              <TransformStage
                videoUrl={previewUrl}
                videoRef={videoRef}
                sourceWidth={videoDimensions.width}
                sourceHeight={videoDimensions.height}
                transform={activeTransform}
                background={activeBackground}
                transformMode={transformMode}
                viewerZoom={viewerZoom}
                viewerAspect={viewerAspect}
                ratioLock={ratioLock}
                showGuides={showGuides}
                loop={loopPlayback}
                bottomReserve={transformMode ? transformFloatHeight + 12 : 0}
                transformLocked={transformLocked}
                onTransformChange={updateActiveTransform}
                onViewerZoomChange={(percent) => setViewerZoom(clampViewerZoom(percent))}
                onLoadedMetadata={handleVideoMetadata}
                onLoadedData={handleVideoLoadedData}
                onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)}
                onPlay={() => { setPlaying(true); setShowCoverFrame(false); }}
                onPause={() => setPlaying(false)}
              />
              {/* 播放器内实时字幕叠加层：与后端 ASS 生成器同一 percent→pixel 公式
                  （left = W*x_pct/100，bottom = H*(100-y_pct)/100，fontSize = px*(W/1080)），
                  所以预览与 Phase 2 导出在同一像素网格上。只读层，不拦截指针事件。 */}
              {subtitleOverlayEnabled && inspectorSubtitles && (
                <SubtitleOverlay
                  cues={inspectorSubtitles.cues}
                  config={inspectorSubtitles.config}
                  currentTime={currentTime}
                  enabled={subtitleOverlayEnabled}
                />
              )}
              {!previewUrl && <button className="drop-placeholder" onClick={() => inputRef.current?.click()}><Upload size={26} /><strong>导入原始视频</strong><span>拖入视频或点击选择文件</span></button>}
              {/* video 加载失败占位（error 事件降级）：源 404/JSON/被清理时 <video> 只会黑屏，
                  这里盖一层可读提示；不清空选中态、不改 src（防错误循环），成功加载后由
                  loadeddata 自动清除。再要求 previewUrl 非空：素材被移除时让位给导入占位，
                  不残留旧错误。样式内联对齐 .cover-frame-view 的覆盖层做法（本守卫
                  不引入新 CSS）；Film 图标复用既有导入。 */}
              {previewError && previewUrl && (
                <div
                  className="video-error-placeholder"
                  role="alert"
                  data-testid="video-error-placeholder"
                  style={{ position: "absolute", inset: 0, zIndex: 2, display: "grid", placeItems: "center", justifyItems: "center", gap: 9, padding: 12, textAlign: "center", color: "#b7c2c2", background: "rgba(5, 8, 9, 0.92)" }}
                >
                  <Film size={26} aria-hidden />
                  <strong>视频加载失败</strong>
                  <span style={{ color: "#748084", fontSize: 12 }}>{previewError}</span>
                </div>
              )}
              {/* 画布封面视图：选中成片时可把画布切到「该成片当前的封面资产」当首帧看。
                  封面路径优先取项目台账卡片的 cover.path（重新生成/撤销后随之变化），
                  回退到成片自身的 cover_path；都不存在时不渲染该层，静默回退到播放视频。
                  2026-10-21 修：封面画布单独成 9:16 容器（按播放器可用高度等比缩放、居中，
                  两侧留黑边），img contain 完整显示——不再因横向播放器把 9:16 封面裁掉上下。
                  该层与普通视频播放互不影响（视频画布/预览比例/时间线不改）。 */}
              {showCoverFrame && activeClip && coverImageUrlValue && (
                <div className="cover-frame-view" role="img" aria-label={`${activeClip.name} 已生成的封面`} data-testid="cover-frame-view">
                  <div className="cover-frame-canvas" data-testid="cover-frame-canvas">
                    <img src={coverImageUrlValue} alt={`${activeClip.name} 封面首帧`} />
                  </div>
                  <span className="cover-frame-badge">封面首帧 · 已生成封面{coverVersionLabel(activeClip) ? ` ${coverVersionLabel(activeClip)}` : ""}</span>
                </div>
              )}
              {/* 时间线封面静帧预览：播放头进入图片轨实例窗口时叠显示封面 + 静音
                  （与导出渲染语义一致；既有封面预览层优先，两者不叠加）。 */}
              {!showCoverFrame && activeTimelineImage && activeClip && coverImageUrlValue && (
                <TimelineImagePreview
                  instance={activeTimelineImage}
                  clipName={activeClip.name}
                  coverUrl={coverImageUrlValue}
                />
              )}
            </div>
            {/* 浮层悬浮在画面之上（绝对定位），不挤压画面、不遮挡控制栏；窄屏可用标题行按钮折叠。 */}
            {transformMode && (
              <div className="transform-float" ref={transformFloatRef}>
                <TransformPanel
                  transform={activeTransform}
                  background={activeBackground}
                  ratioLock={ratioLock}
                  viewerZoom={viewerZoom}
                  clipLabel={activeClipLabel}
                  disabled={!previewUrl}
                  transformLocked={transformLocked}
                  dirty={transformDirty}
                  collapsed={transformPanelCollapsed}
                  onToggleCollapse={() => setTransformPanelCollapsed((value) => !value)}
                  onTransformChange={updateActiveTransform}
                  onBackgroundChange={setTransformBackground}
                  onRatioLockChange={setRatioLock}
                  onViewerZoomChange={(percent) => setViewerZoom(clampViewerZoom(percent))}
                  onFitView={fitViewerZoom}
                  onOriginalRatio={applyOriginalRatio}
                  onFillCanvas={fillCanvasWithClip}
                  onResetTransform={resetActiveTransform}
                  onApply={applyTransform}
                  onCancel={cancelTransform}
                />
              </div>
            )}
            {transformLocked && <p className="transform-lock-note">该手机视频带旋转标记，画面变换暂不可编辑（导出不受影响）。</p>}
          </div>
          <PlayerControlBar
            currentTime={currentTime}
            duration={duration}
            playing={playing}
            hasMedia={!!previewUrl}
            volume={previewVolume}
            muted={previewMuted}
            viewerAspect={viewerAspect}
            transformMode={transformMode}
            transformLocked={transformLocked}
            onTogglePlay={() => void togglePlayback()}
            onSeekBy={(delta) => seek(Math.max(0, Math.min(duration || 0, currentTime + delta)))}
            onSeekFrame={seekFrame}
            onVolumeChange={(next) => { setPreviewVolume(next); if (next > 0) setPreviewMuted(false); }}
            onToggleMute={() => setPreviewMuted((value) => !value)}
            onOriginalRatio={applyOriginalRatio}
            onToggleTransformMode={enterOrExitTransformMode}
            onToggleFullscreen={() => void togglePreviewFullscreen()}
            onViewerAspectChange={setViewerAspect}
          />
        </div>
      </section>
      <aside className="transcript-pane" data-inspector-tab={inspectorTab}>
        {/* Inspector Phase 1：固定 5 标签（转写｜字幕｜封面与画面｜音频｜效果设置）。
            面板绑定当前选中对象（成片 → clip_id；未选成片 → source），自行拉取/保存配置与版本；
            这里只传入页面共享状态与既有 handler（封面通道 / 画面变换 / 播放器 seek）。
            data-inspector-tab：非转写标签下 aside 单行（.inspector-pane 占满整列，
            pane-body 内部滚动）；转写标签保持既有三行（转写宿主排布）。 */}
        {sourceReturnError && <p role="alert" className="inspector-error" data-testid="source-return-error">{sourceReturnError}</p>}
        {currentRemoteSource && !transcriptRef && !activeRenderedClip && <p role="status" className="inspector-hint">原片已恢复，尚无可用转写版本；当前仅可查看原片。</p>}
        <InspectorPane
          projectId={activeProjectId}
          clipId={inspectorClipId}
          baseUrl={transcribeServiceBase}
          activeTab={inspectorTab}
          onTabChange={setInspectorTab}
          coverWorkflow={{
            hasCover: Boolean(activeCard?.cover_workflow?.has_cover),
            pending: Boolean(activeCard?.cover_workflow?.pending),
            applied: Boolean(activeCard?.cover_workflow?.applied),
            busy: coverTemplateActionBusy || coverTemplateGenerateBusy !== null,
            coverImageUrl: coverImageUrlValue,
            applyProgress: coverApplyProgress,
            onApply: () => void applyCoverTemplate(),
            onDelete: () => void deleteCoverTemplate(),
          }}
          coverTemplates={{
            exampleUrlBase: studioBase,
            library: coverTemplateLibrary,
            loading: coverTemplatesState.loading,
            error: coverTemplatesState.error,
            selectedTemplateId: selectedCoverTemplateId(activeCard),
            generateBusyTemplateId: coverTemplateGenerateBusy,
            onSelectTemplate: selectCoverTemplate,
            onGenerate: () => void generateCoverTemplate(),
            onRetryLoad: () => void loadCoverTemplates(),
          }}
          fallbackSegments={segments}
          currentTime={currentTime}
          duration={duration}
          onSeek={seek}
          onSubtitleStateChange={handleInspectorSubtitlesChange}
          onPlayerVolumeChange={(config) => setInspectorVoiceConfig(config)}
          boundToActiveClip={Boolean(activeRenderedClip)}
          clipName={activeClip?.name ?? null}
          coverImageUrl={activeClip && coverPathOf(activeClip, activeCard) ? `${artifactUrl(activeClip.task_id, coverPathOf(activeClip, activeCard) as string)}&v=${coverVersion}` : null}
          coverAssetId={activeClip?.cover_asset_id ?? activeCard?.cover?.cover_asset_id ?? null}
          coverRevision={activeClip?.cover_revision ?? activeCard?.cover?.revision ?? null}
          coverVersionLabel={activeClip ? coverVersionLabel(activeClip) : null}
          coverStatusText={coverState?.active_kind === "adopted" ? "沿用自其他成片已生成的封面" : coverState?.active_kind === "generated" ? `按风格生成（${coverState.active_style_id === "cutout_v3" ? "第三版·人物抠像" : "第二版·真实帧"}）` : coverState?.active_kind === "title" ? "标题改动重出的封面" : "原始成片封面"}
          title={activeCard?.title ?? activeClip?.topic ?? null}
          coverBusy={coverBusy}
          canUndoCover={Boolean(activeClip && cardCoverCanUndo(activeClip))}
          coverUndoRevision={activeCard?.cover?.undo_revision ?? null}
          onRegenerateCover={() => { if (activeRenderedClip) void regenerateCoverForClip(activeRenderedClip, coverStyle).catch((error) => setMessage(error instanceof Error ? error.message : "封面重制失败")); }}
          coverTaskFailure={coverTaskFailure}
          onRegenerateWithInstruction={(instruction) => { void regenerateCoverWithInstruction(instruction); }}
          onRetryCoverTask={retryCoverTask}
          onActivateRevision={(revision) => { void activateCoverRevision(revision); }}
          onDeleteRevision={(revision) => { void deleteCoverRevision(revision); }}
          onPreviewInPlayer={() => { if (activeRenderedClip) void playRenderedClip(activeRenderedClip); }}
          artifactUrlFor={artifactUrl}
          coverCacheVersion={coverVersion}
          onEditTitle={() => { if (!activeRenderedClip) return; const title = promptClipTitle(activeRenderedClip); if (title === null) return; void updateClipTitle(activeRenderedClip, title || null).catch((error) => setMessage(error instanceof Error ? error.message : "标题更新失败")); }}
          onShowCoverHistory={() => { if (activeRenderedClip) void toggleCoverHistory(activeRenderedClip); }}
          onAdoptCover={(sourceClipId) => { if (activeRenderedClip && sourceClipId) void adoptCover(activeRenderedClip, sourceClipId, false).catch((error) => setMessage(error instanceof Error ? error.message : "沿用封面失败")); }}
          onUndoCover={() => { if (activeRenderedClip) void undoClipCover(activeRenderedClip).catch((error) => setMessage(error instanceof Error ? error.message : "撤销封面失败")); }}
          coverHistory={coverHistory}
          coverHistoryVisible={coverHistoryClipId !== null && coverHistoryClipId === activeRenderedClip?.clip_id}
          coverHistoryLoading={coverHistoryLoading}
          adoptOptions={projectCovers.map((item) => ({ clip_id: item.clip_id, name: item.name || item.clip_id }))}
          adoptSource={coverSource || activeRenderedClip?.clip_id || null}
          onAdoptSourceChange={setCoverSource}
          transform={appliedTransform}
          transformSelectionKey={inspectorTarget}
          onAppliedTransformLoaded={restoreInspectorTransform}
          transformLocked={transformLocked}
          transformBackground={transformBackground}
          ratioLock={ratioLock}
          viewerZoom={viewerZoom}
          viewerAspect={viewerAspect}
          onTransformDraftChange={previewInspectorTransform}
          onTransformBackgroundChange={previewInspectorBackground}
          onRatioLockChange={setRatioLock}
          onViewerZoomChange={(percent) => setViewerZoom(clampViewerZoom(percent))}
          onFitView={fitViewerZoom}
          onOriginalRatio={applyOriginalRatio}
          onCanvasMode={fillCanvasWithClip}
          onResetTransform={resetActiveTransform}
          onViewerAspectChange={setViewerAspect}
          onApplyTransform={applyInspectorTransform}
          onCancelTransform={cancelInspectorTransform}
        />
        {/* 转写标签的既有界面（自动剪辑进度、金句、转写行）保持挂载：只切换显示，
            这样搜索词/滚动位置不会因为切标签丢失，也不新增重复字幕轨。 */}
        <div className="inspector-transcript-host" hidden={captionsHidden}>
        <label className="transcript-search"><Search size={16} /><input placeholder="搜索转写内容…" /></label>
        <div className="transcript-status"><span>{transcriptionState === "processing" ? <LoaderCircle size={14} /> : <AudioLines size={14} />}{activeRenderedClip ? "剪辑成片时间线" : transcriptionState === "processing" ? "转写中" : segments.length ? "纠错后转写" : "等待转写"}</span><div>{!activeRenderedClip && segments.length > 0 && <button className="insights-action" onClick={extractInsights} disabled={extractingInsights}>{extractingInsights ? "提取中" : "提取金句"}</button>}{!activeRenderedClip && transcriptRef && <button className="insights-action" onClick={() => { setSourceReviewOpen(true); void loadSourceRevision(false); }}>核听与人工修订</button>}{activeRenderedClip ? <button className="insights-action" onClick={() => void selectSourceMedia()} disabled={sourceReturnBusy}>{sourceReturnBusy ? "正在返回原片…" : "返回原片"}</button> : file && <button className="transcribe-action" onClick={startTranscription} disabled={transcriptionState === "processing"}>{transcriptionState === "processing" ? "正在转写" : transcriptionState === "error" ? "重试当前文件" : "开始转写"}</button>}</div></div>
        <div className="transcript-rows">{workflowJob && <section className="auto-edit-panel" aria-label="当前工作流任务">
  <header><strong>当前任务</strong><span>{workflowJob.status}</span></header>
  <p>{workflowJob.stage || "等待服务端状态"}</p>
  <div className="auto-edit-progress"><i style={{ width: `${Math.max(0, Math.min(100, workflowJob.progress * 100))}%` }} /></div>
  {workflowJob.error && <p role="alert">{workflowJob.error}</p>}
  {([ ["correction", "纠错"], ["analysis", "分析"], ["planning", "规划"] ] as const).map(([key, label]) => {
    const source = workflowSources[key];
    const reported = workflowJob.stage_sources[key];
    return <div key={key}><strong>{label}：{workflowSourceLabel(reported && reported !== "unknown" ? reported : source?.source)}</strong>
      <small> 用量：{source?.usage_status === "known" ? "已取得真实usage" : source?.usage_status === "partial" ? "部分调用未知" : "未知"}</small>
      {source?.error && <p>{source.error}</p>}{source?.warnings.map((warning, index) => <p key={index}>{warning}</p>)}</div>;
  })}
  {[...workflowJob.warnings, ...workflowJob.degradation_reasons].map((warning, index) => <p key={index}>{warning}</p>)}
  <div><button className="insights-action" disabled={workflowActionBusy} onClick={() => void refreshWorkflowStatus()}>刷新当前任务状态</button>{workflowJob.available_actions.map((action) => <button key={action} className="insights-action" disabled={workflowActionBusy}
    onClick={() => void requestWorkflowAction(action)}>{action === "resume_render" ? "继续渲染失败部分" : action === "replan" ? "重新模型规划" : "只重试清理"}</button>)}
    {workflowJob.cancel_supported && <button className="insights-action" disabled={workflowActionBusy} onClick={() => void requestWorkflowAction("cancel")}>取消任务</button>}</div>
  {!workflowJob.recovery_supported && <small>服务端尚未提供恢复操作契约，当前操作能力未知。</small>}
</section>}
{activeRenderedClip && <section className="insight-panel" aria-label="当前成片字幕来源"><strong>成片字幕</strong>
  <p>{subtitleModeLabel(activeRenderedClip.subtitle_mode)}</p>
  <p>{activeRenderedClip.subtitle_transcript_version ? `转写版本 v${activeRenderedClip.subtitle_transcript_version}` : "转写版本未知"} · {activeRenderedClip.segments.length ? `${activeRenderedClip.segments.length} 段已载入字幕时间线` : "字幕时间线尚未载入"}</p>
  {activeRenderedClip.subtitle_notice && <p>{activeRenderedClip.subtitle_notice}</p>}
  {activeRenderedClip.subtitle_url && <a href={workflowArtifactUrl(transcribeServiceBase, activeRenderedClip.subtitle_url)} download target="_blank" rel="noreferrer">下载本成片SRT</a>}
</section>}
{sourceReviewOpen && !activeRenderedClip && <section className="insight-panel" aria-label="源转写核听与人工修订">
  <header><strong>源转写核听与人工修订</strong><button className="insights-action" onClick={() => setSourceReviewOpen(false)}>收起（保留草稿）</button></header>
  <p>原始转写只读。请核听原片后修订文本；时间戳保持程序原值。当前剪辑引用 v{transcriptRef?.transcript_version ?? "未知"}。</p>
  <label>核对版本 <input aria-label="核对转写版本" type="number" min={1} value={sourceReviewVersion} onChange={(event) => setSourceReviewVersion(event.target.value)} /></label>
  <button className="insights-action" disabled={sourceReviewBusy} onClick={() => void loadSourceRevision(false)}>核对指定版本</button>
  <button className="insights-action" disabled={sourceReviewBusy} onClick={() => void loadSourceRevision(true)}>载入最新版本（保留草稿）</button>
  {sourceReviewMessage && <p role="status">{sourceReviewMessage}</p>}
  {revisionWarnings(sourceReviewDocument?.review_warnings).risk_flags.map((warning, index) => <p key={index}>核听提示：{revisionWarningLabel(warning)}</p>)}
  {revisionWarnings(sourceReviewDocument?.review_warnings).validation_errors.map((warning, index) => <p key={`validation-${index}`} role="status">检查范围提示：{revisionWarningLabel(warning)}</p>)}
  {sourceReviewDocument && <p>风险提示仅辅助核听；{revisionWarnings(sourceReviewDocument.review_warnings).comparison_source === "base_version" ? "对比人工修订前版本" : revisionWarnings(sourceReviewDocument.review_warnings).comparison_source === "raw_asr" ? "对比原始转写" : "原文对比状态未知"}。</p>}
  {sourceReviewDocument && <><p>{sourceReviewDocument.manual_revision ? "人工核听版本" : "纠错版本"} v{sourceReviewDocument.transcript_version}</p>
    <label>核听第 <input aria-label="核听段号" type="number" min={1} max={sourceReviewDocument.asr_segments.length} value={(selectedSegment ?? 0) + 1}
      onChange={(event) => focusSourceReviewSegment(Number(event.target.value) - 1)} /> 句，共 {sourceReviewDocument.asr_segments.length} 句</label>
    <button className="insights-action" onClick={() => focusSourceReviewSegment((selectedSegment ?? 0) - 1)}>上一句</button>
    <button className="insights-action" onClick={() => focusSourceReviewSegment((selectedSegment ?? 0) + 1)}>下一句</button>
    {sourceReviewDocument.asr_segments.map((segment, index) => index === Math.min(selectedSegment ?? 0, sourceReviewDocument.asr_segments.length - 1) && <div key={segment.segment_id || index}>
      <button className="insights-action" onClick={() => seek(segment.start_ms !== undefined ? segment.start_ms / 1000 : segment.start)}>核听 {formatTime(segment.start_ms !== undefined ? segment.start_ms / 1000 : segment.start)}</button>
      <details><summary>只读原始转写</summary><p>{sourceReviewDocument.raw_segments?.[index]?.text || segment.raw_text || "旧记录未保留原始转写"}</p></details>
      <p>本版本：{segment.text}{segment.uncertain ? "（含不确定词，请核听）" : ""}</p>
      <textarea aria-label={`修订第${index + 1}段`} rows={2} value={sourceReviewDraft[index] ?? segment.text}
        onChange={(event) => setSourceReviewDraft((old) => ({ ...old, [index]: event.target.value }))} style={{ width: "100%", color: "inherit", background: "transparent" }} />
    </div>)}
    <label>修订说明 <input aria-label="修订说明" value={sourceReviewReason} onChange={(event) => setSourceReviewReason(event.target.value)} /></label>
    <button className="insights-action" disabled={sourceReviewBusy || !revisionEdits(sourceReviewDocument, sourceReviewDraft).length}
      onClick={() => void saveSourceRevision()}>{sourceReviewBusy ? "保存中" : "保存人工核听新版本"}</button>
  </>}
</section>}
{autoEditTask && <section className={`auto-edit-panel ${autoEditTask.status}`} aria-label="本机自动剪辑任务"><header><strong>本机自动剪辑</strong><span>{autoEditTask.status === "queued" ? "排队中" : autoEditTask.status === "running" ? "处理中" : autoEditTask.status === "completed" ? "已完成" : "失败"}</span></header><p>{autoEditTask.stage}</p><div className="auto-edit-progress"><i style={{ width: `${Math.max(3, autoEditTask.progress * 100)}%` }} /></div>{autoEditTask.error && <small>{autoEditTask.error}</small>}{autoEditTask.artifacts.length > 0 && <div className="auto-edit-artifacts">{autoEditTask.artifacts.filter((artifact) => /\.(mp4|srt|jpg)$/i.test(artifact.name)).map((artifact) => <a key={artifact.path} href={`${autoEditBaseUrl}/${autoEditTask.task_id}/artifact?path=${encodeURIComponent(artifact.path)}`} target="_blank" rel="noreferrer">{artifact.name}</a>)}</div>}</section>}{insights && <section className="insight-panel" aria-label="金句与关键词"><header><strong>金句与关键词</strong><span className={insights.engine === "llm" ? "model" : "fallback"}>{insights.engine === "llm" ? "模型提取" : "本地候选"}</span></header><div className="golden-list">{insights.golden_sentences.map((sentence, index) => <button key={`${sentence.start}-${index}`} title={sentence.reason} onClick={() => seek(sentence.start)}><time>{formatTime(sentence.start)}</time><span>{sentence.text}</span></button>)}</div>{insights.keywords.length > 0 && <div className="keyword-list">{insights.keywords.map((keyword) => <span key={keyword}>#{keyword}</span>)}</div>}{insights.notice && <p>{insights.notice}</p>}</section>}{segments.length ? segments.map((segment, index) => <article className={selectedSegment === index ? "selected" : ""} key={`${segment.start}-${index}`} onClick={() => { setSelectedSegment(index); seek(segment.start); }}><time>{formatTime(segment.start)} – {formatTime(segment.end)}</time><div><select value={segment.speaker || "未标注"} onChange={(event) => updateSpeaker(index, event.target.value as SpeakerRole)} onClick={(event) => event.stopPropagation()} className={segment.speaker === "医生" ? "doctor" : segment.speaker === "患者" ? "patient" : ""}><option value="未标注">未标注</option><option value="医生">医生</option><option value="患者">患者</option></select>{/* 转写内容已设为只读：仅可查看、选中与复制；点击整行仍会选中该句并跳转播放 */}
<p className="transcript-text">{segment.text}</p>{segment.raw_text && (segment.raw_text !== segment.text || segment.uncertain || segment.correction_reason) ? <details className="transcript-correction" onClick={(event) => event.stopPropagation()}><summary>已纠错<em className="correction-status">{segment.correction_status || "corrected"}</em>{segment.uncertain ? <em className="correction-uncertain">含不确定词，保留原文</em> : null}</summary><p className="transcript-raw-text">{segment.raw_text}</p>{segment.correction_reason ? <p className="transcript-correction-reason">修改原因：{segment.correction_reason}</p> : null}</details> : null}</div></article>) : <div className="transcript-awaiting"><AudioLines size={28} /><strong>{transcriptionState === "error" ? "转写没有完成" : "等待视频转写"}</strong><p>{message || "上传视频后点击“开始转写”，原文、纠错结果和说话人标签会显示在这里。"}</p></div>}</div>
        </div>
      </aside>
    </section>
    <section className="timeline-pane">
      {/* 时间线 Phase B 接线：工具栏与轨道层来自组件契约（TimelineToolbarProps / TimelineTracksProps）。
          视图状态（缩放/轨高/波形占比/滚动）只影响显示；撤销/重做走项目历史；波形来自后端 peaks。 */}
      <TimelineToolbar
        canUndo={timelineCanUndo}
        canRedo={timelineCanRedo}
        zoomPxPerSecond={timelineView.zoom_px_per_second}
        trackHeight={timelineView.audio_track_height}
        waveformRatio={timelineView.waveform_ratio}
        hasSourceTrack={hasSourceTrack}
        busy={timelineBusy}
        onAddTrack={handleTimelineAddTrack}
        onUndo={() => void stepTimeline("undo")}
        onRedo={() => void stepTimeline("redo")}
        onZoomChange={handleTimelineZoomChange}
        onZoomIn={handleTimelineZoomIn}
        onZoomOut={handleTimelineZoomOut}
        onFitWindow={handleTimelineFitWindow}
        onTrackHeightChange={handleTimelineTrackHeightChange}
        onWaveformRatioChange={handleTimelineWaveformRatioChange}
      />
      {/* 外层滚动容器语义保留：页面持有 .timeline-scroll，负责指针擦洗 / onScroll /
          双击删除音轨；组件只观测宽度并写回 scrollLeft。 */}
      <div
        ref={timelineScrollRef}
        className={scrubbing ? "timeline-scroll is-scrubbing" : "timeline-scroll"}
        onPointerDown={beginTimelineScrub}
        onPointerMove={moveTimelineScrub}
        onPointerUp={endTimelineScrub}
        onPointerCancel={endTimelineScrub}
        onScroll={handleTimelineScroll}
        onDoubleClick={handleTimelineLaneDoubleClick}
      >
        <TimelineTracks
          duration={duration}
          currentTime={currentTime}
          view={timelineView}
          ranges={ranges}
          cues={timelineCues}
          segments={timelineSegmentCues}
          selectedSegment={selectedSegment}
          goldenSegments={timelineGoldenSegments}
          copilotHighlight={copilotHighlight}
          audioTracks={timelineAudioTracks}
          peaksByTrack={peaksByTrack}
          peaksDurationByTrack={peaksDurationByTrack}
          activeMediaName={activeMediaName}
          imageClips={timelineImageClips}
          onDropCoverImage={(startSeconds, payload) => void handleTimelineDropCoverImage(startSeconds, payload)}
          onRemoveImageClip={(instanceId) => void handleTimelineRemoveImageClip(instanceId)}
          scrollElementRef={timelineScrollRef}
          viewportWidthPx={timelineViewportWidth}
          scrollLeft={timelineView.scroll_left ?? 0}
          onSeek={handleTimelineSeek}
          onSelectSegment={setSelectedSegment}
          onViewportWidthChange={setTimelineViewportWidth}
        />
      </div>
      {/* ＋ 菜单 bgm/sfx 的音频选择器：选择后先 POST /assets/audio 再 POST /tracks。 */}
      <input ref={timelineAudioInputRef} type="file" accept="audio/*" hidden onChange={handleTimelineAudioFile} />
      <footer className="timeline-footer"><span>{file || currentRemoteSource || activeRenderedClip ? `${activeMediaName} · ${formatTime(duration)}` : "尚未导入素材"}</span>{timeline && <span><Save size={14} />{activeRenderedClip ? "成片时间线已载入" : "EDL、SRT 与 FCPXML 已生成"}</span>}<button onClick={downloadRenderedClip} disabled={!activeRenderedClip}><Download size={14} />导出成片</button></footer>
    </section>
      {/* 规则中心面板（Phase B）：实时读清单 → 版本可见性 / 启停 skill / 查看成片规则 / 按最新规则重新生成。
          数据源是 GET /studio/workflow，后端每次调用都重新读清单，因此这里不做本地缓存：
          成员改完 skill 文件或启停规则后点「刷新」即可看到新版本与新校验状态，无需重启浏览器或后端。 */}
      {workflowPanelOpen && (
        <div className="workflow-panel" role="dialog" aria-label="剪辑规则中心">
          <header>
            <strong>剪辑规则中心</strong>
            <span className={workflowSummary?.validation_ok === false ? "workflow-version bad" : "workflow-version"}>当前使用：医疗短视频剪辑规则 v{workflowBanner?.current_version || "unknown"}</span>
            <div className="workflow-panel-actions">
              <button type="button" disabled={workflowBusy} onClick={() => void loadWorkflow()} title="重新读取清单（后端每次实时读取，不缓存）">{workflowBusy ? <LoaderCircle size={13} /> : <RotateCcw size={13} />}刷新</button>
              <button type="button" aria-label="关闭规则中心" onClick={() => setWorkflowPanelOpen(false)}><X size={13} /></button>
            </div>
          </header>
          {workflowMessage && <p className="workflow-message">{workflowMessage}</p>}
          {workflowSummary?.warnings?.length ? <p className="workflow-warning">校验告警：{workflowSummary.warnings.join("；")}</p> : null}
          <div className="workflow-skills">
            {(workflowSummary?.skills || []).map((skill) => (
              <article key={skill.skill_id} className={`workflow-skill ${skill.status}`}>
                <div className="workflow-skill-head">
                  <strong>{skill.skill_id}</strong>
                  <em>{skill.version}</em>
                  <i className={skill.enabled ? "on" : "off"}>{skill.enabled ? "已启用" : "已停用"}</i>
                  <i className="state">{skill.status === "ok" ? "校验通过" : skill.status === "stale" ? "校验和不符（有新版本）" : skill.status === "disabled" ? "已停用" : skill.status}</i>
                </div>
                {skill.detail && <small>{skill.detail}</small>}
                <button type="button" disabled={workflowBusy} onClick={() => void toggleWorkflowSkill(skill.skill_id, !skill.enabled)}>{skill.enabled ? "停用" : "启用"}</button>
              </article>
            ))}
          </div>
          <p className="workflow-hint">清单更新时间：{workflowSummary?.updated_at || "未知"}。启停规则会先把清单备份为 .bak.&lt;时间戳&gt;，写回后立刻重新校验（含校验和），非法则自动回滚。</p>
          <section className="workflow-snapshot">
            <header><strong>本次成片实际使用了哪些规则</strong>{workflowSnapshotClipId && <span>{workflowSnapshotClipId}</span>}</header>
            {!workflowSnapshot && <p>在成片卡片的「查看本次规则」或此处按钮读取该成片渲染任务的规则快照。</p>}
            {workflowSnapshot?.found === false && <p>{workflowSnapshot.notice}</p>}
            {workflowSnapshot?.found && <>
              <p className="workflow-version-line">{workflowSnapshot.version_line}</p>
              <ul>{(workflowSnapshot.snapshot?.skills || []).map((item) => <li key={item.skill_id}><b>{item.skill_id}</b> {item.version}{item.checksum ? ` · ${item.checksum}` : ""}</li>)}</ul>
              <p className="workflow-hint">生效规则段：{Object.keys(workflowSnapshot.snapshot?.rules || {}).join("、") || "无"}</p>
            </>}
          </section>
          {/* 规则信息替代位置 #3（非顶栏）：导出记录 / 任务历史。
              展示当前成片台账里的 workflow_version、各 skill 版本、规则快照（workflow_snapshot.json）
              读取入口与局部/渲染任务历史。数据全部来自既有接口
              （GET /studio/projects/{id}/clips 的 card_view、GET /studio/workflow/{task_id}/snapshot），
              不新增后端字段：台账没给的一律如实显示「台账未提供」，不编造规则版本。 */}
          <section className="workflow-history">
            <header><strong>导出记录 / 任务历史</strong>{ruleHistoryCard?.clip_id && <span>{ruleHistoryCard.clip_id}</span>}</header>
            <p className="workflow-version-line">使用规则版本：{ruleHistoryCard?.workflow_version ? `v${ruleHistoryCard.workflow_version}` : "台账未提供（旧成片无规则版本字段）"}</p>
            {ruleHistoryCard?.skill_versions && Object.keys(ruleHistoryCard.skill_versions).length > 0
              ? <ul>{Object.entries(ruleHistoryCard.skill_versions).map(([skillId, version]) => <li key={skillId}><b>{skillId}</b> {version ? `v${version}` : "版本未知"}</li>)}</ul>
              : <p>Skill 版本：台账未提供</p>}
            <p className="workflow-hint">
              规则快照：{ruleHistoryCard?.workflow_snapshot_path ? <code>{ruleHistoryCard.workflow_snapshot_path}</code> : "台账未提供"}
              {ruleHistoryCard && <button type="button" disabled={workflowBusy} onClick={() => { const clip = renderedClipList.find((item) => item.clip_id === ruleHistoryCard.clip_id); if (clip) void viewClipRules(clip); }}>读取快照</button>}
            </p>
            {(ruleHistoryCard?.task_history ?? []).length > 0
              ? <ol>{(ruleHistoryCard?.task_history ?? []).map((entry) => <li key={entry.task_id}><b>{entry.task_type}</b> · {entry.status}{entry.created_at ? ` · ${entry.created_at}` : ""}{entry.summary ? ` · ${entry.summary}` : ""}{entry.error ? ` · ${entry.error}` : ""}{/* 逐次任务的规则溯源：台账 task_history 自带的版本字段（有则显示，无则不显示）。 */}{entry.workflow_version ? ` · 规则 v${entry.workflow_version}` : ""}{entry.workflow_snapshot_path ? ` · 快照 ${entry.workflow_snapshot_path}` : ""}</li>)}</ol>
              : <p>任务历史：暂无记录</p>}
          </section>
          {activeClip && <button type="button" className="workflow-regenerate" disabled={localTaskBusy} onClick={() => void regenerateClipByLatestRules(activeClip, "cover").then((text) => setWorkflowMessage(text)).catch((error) => setWorkflowMessage(error instanceof Error ? error.message : "按最新规则重新生成失败"))}>按最新规则重新生成「{activeClip.name}」（新增 revision，旧成果保留）</button>}
        </div>
      )}
  {/* 项目管理弹层：页内浮层，open=false 时返回 null，工作台保持挂载（编辑状态保留）。 */}
  <ProjectManager
    open={projectManagerOpen}
    base={transcribeServiceBase}
    activeProjectId={activeProjectId}
    onClose={() => setProjectManagerOpen(false)}
    onSaveCurrentEdits={saveBeforeProjectSwitch}
    onOpenProject={openProjectFromManager}
    onNewProject={newProjectFromManager}
    />
  </main>;
}
