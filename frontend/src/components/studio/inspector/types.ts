// Inspector Phase 1 —— 与冻结契约 docs/inspector-phase1-contract.md §2 / §5 对应的 TS 类型。
//
// 这里的字段名 = 后端 JSON 的字段名（snake_case，不做本地重命名），
// 只为了在写请求体时不会漏字段、在读响应时不会猜形状。
// Phase 2（按配置真渲染）未实现：render_deferred / export_burn_in 等字段只用于 UI 标注。

/** 三态稿：原始 ASR / 纠错稿 / 有效稿（契约 §3.2）。 */
export type TranscriptVariant = "raw" | "corrected" | "effective";

/** 说话人标签（与页面既有 SpeakerRole 一致，外加「其他」）。 */
export type SpeakerLabel = "未标注" | "医生" | "患者" | "其他";

export const SPEAKER_LABELS: SpeakerLabel[] = ["未标注", "医生", "患者", "其他"];

/** 字幕位置（1080x1920 坐标系里的百分比，与后端 percent_to_pixels 同一公式）。 */
export type SubtitlePosition = { x_pct: number; y_pct: number };

export type SubtitleBackground = {
  enabled: boolean;
  color: string;
  opacity: number;
};

export type SubtitleConfig = {
  font_size_px: number;
  /** "" = video-brain 默认字体（FONT_NAME）。 */
  font_family: string;
  text_color: string;
  outline_color: string;
  /** 0..12 */
  outline_width: number;
  background: SubtitleBackground;
  position: SubtitlePosition;
  word_highlight: boolean;
  max_chars_per_line: number;
};

export const DEFAULT_SUBTITLE_CONFIG: SubtitleConfig = {
  // 与 backend subtitle_compose.DEFAULT_* 保持同步：54px=5% 画宽、14 字/行、3px 描边。
  font_size_px: 54,
  font_family: "",
  text_color: "#FFFFFF",
  outline_color: "#101010",
  outline_width: 3,
  background: { enabled: false, color: "#000000", opacity: 0.5 },
  // 底部居中基准：y_pct=78.125 → MarginV=420（与 ASS 生成器 / video-brain 一致）。
  position: { x_pct: 50, y_pct: 78.125 },
  word_highlight: false,
  max_chars_per_line: 14,
};

/** 字幕覆盖条目：按 segment_id 覆盖文本与时间（契约 §2 subtitle_overrides）。 */
export type SubtitleOverride = {
  segment_id: string;
  start?: number;
  end?: number;
  text: string;
  updated_at?: string;
};

/** 渲染用 cue（后端 build_cues 的产物 / POST …/subtitles 的 cues 入参）。 */
export type SubtitleCue = {
  index?: number;
  segment_id: string;
  start: number;
  end: number;
  text: string;
  speaker?: string;
};

export type StickerPosition = "left" | "right" | "bottom-left" | "bottom-right";
export type FilterName = "none" | "warm" | "cool" | "bw";
export type TransitionType = "none" | "fade";
export type ExportResolution = "1080x1920" | "1080x1080" | "1920x1080";
// ===== 成片风格微调（与后端 clip_config 的 PICTURE_FILTER_NAMES / RATIO_NAMES /
// EFFECT_PRESET_NAMES 一一对应；预设参数组从 GET …/render-config 的 presets 读取，
// 后端是唯一事实源）=====
export type PictureFilterName = "none" | "warm" | "cool" | "bw" | "vivid";
export type RatioName = "9:16" | "16:9" | "1:1";
export type EffectPresetName = "medical" | "clear" | "science" | "natural";

export const STICKER_POSITIONS: StickerPosition[] = ["left", "right", "bottom-left", "bottom-right"];
export const FILTER_NAMES: FilterName[] = ["none", "warm", "cool", "bw"];
export const TRANSITION_TYPES: TransitionType[] = ["none", "fade"];
export const EXPORT_RESOLUTIONS: ExportResolution[] = ["1080x1920", "1080x1080", "1920x1080"];
export const PICTURE_FILTER_NAMES: PictureFilterName[] = ["none", "warm", "cool", "bw", "vivid"];
export const RATIO_NAMES: RatioName[] = ["9:16", "16:9", "1:1"];
export const EFFECT_PRESET_NAMES: EffectPresetName[] = ["medical", "clear", "science", "natural"];

/** 画面调整（数值与 FFmpeg eq/unsharp/colortemperature 同源）。 */
export type EffectsPicture = {
  /** -1..1（FFmpeg eq brightness；CSS filter brightness(1+b) 同参数）。 */
  brightness: number;
  /** 0..3（FFmpeg eq contrast；CSS filter contrast(c) 同参数）。 */
  contrast: number;
  /** 0..3（FFmpeg eq saturation；CSS filter saturate(s) 同参数）。 */
  saturation: number;
  /** 0..1（FFmpeg unsharp；预览不体现，UI 已标注）。 */
  sharpen: number;
  /** -1..1（负=冷 / 正=暖；FFmpeg colortemperature）。 */
  temperature: number;
  filter: PictureFilterName;
};

/** 画幅比例：当前仅影响预览（导出画布恒为 9:16），UI 明确标注。 */
export type EffectsRatio = { name: RatioName; fit_canvas: boolean };

/** BGM 自动压低（ducking）。 */
export type EffectsAudio = { ducking: boolean; ducking_db: number };

/** 一个风格预设的参数组（后端 render-config 返回）。 */
export type EffectPreset = {
  label: string;
  picture: EffectsPicture;
  transition: { enabled: boolean; type: TransitionType; duration_s: number };
  audio: EffectsAudio;
};

export type Effects = {
  // 旧契约段（保留）
  sticker: { enabled: boolean; position: StickerPosition };
  end_card: { enabled: boolean; text: string; duration_s: number };
  filter: { name: FilterName; strength: number };
  transition: { enabled: boolean; type: TransitionType; duration_s: number };
  export_resolution: ExportResolution;
  // 成片风格微调
  preset: EffectPresetName;
  picture: EffectsPicture;
  ratio: EffectsRatio;
  audio: EffectsAudio;
};

export const DEFAULT_EFFECTS: Effects = {
  sticker: { enabled: false, position: "left" },
  end_card: { enabled: false, text: "", duration_s: 3 },
  filter: { name: "none", strength: 0 },
  transition: { enabled: false, type: "none", duration_s: 0.5 },
  export_resolution: "1080x1920",
  preset: "natural",
  picture: { brightness: 0, contrast: 1, saturation: 1, sharpen: 0, temperature: 0, filter: "none" },
  ratio: { name: "9:16", fit_canvas: true },
  audio: { ducking: false, ducking_db: -12 },
};

/** BGM / 原始声轨（契约 §3.4 / §4）。 */
export type AudioTrack = {
  id: string;
  kind: "bgm" | string;
  asset_id: string;
  path: string;
  filename: string;
  volume: number;
  fade_in_s: number;
  fade_out_s: number;
  duration_s: number;
  /** 可能被后端截断到 800 点，每个值 0..1。 */
  peaks: number[];
  sha256: string;
  bytes: number;
  created_at: string;
};

/** 序列化后的画面变换（lib/clip-transform.ts 的 transformPayload wire 格式）。 */
export type TransformPayload = {
  scale: number;
  positionX: number;
  positionY: number;
  crop: { left: number; top: number; right: number; bottom: number };
  fitMode: "cover" | "contain";
  rotation: number;
};

/** 页面侧 transform 的既有形状（parseTransform/transformPayload 的实际出入参）。 */
export type TransformLike = Parameters<typeof import("@/lib/clip-transform").transformPayload>[0] | null;

/** GET /studio/clips/{p}/{c}/config 的 config 段（clip 记录投影，契约 §2）。 */
export type ClipConfig = {
  clip_id: string;
  title?: string | null;
  transcript_revision: number;
  subtitle_config: SubtitleConfig;
  subtitle_overrides: SubtitleOverride[];
  cover_asset_id: string | null;
  cover_revision: number;
  transform: Record<string, unknown> | null;
  audio_tracks: AudioTrack[];
  voice_volume: number;
  muted: boolean;
  effects: Effects;
  render_revision: number;
  task_history: unknown[];
};

/** 每个有效的 render 配置项的来源（契约 §5）。 */
export type ConfigSource = "user" | "project" | "skill_default";

export type ClipConfigView = {
  project_id: string;
  clip_id: string;
  binding: "clip" | "source";
  config: ClipConfig;
  revisions: { transcript_revision: number; render_revision: number };
  sources: Record<string, ConfigSource>;
};

export type ClipConfigUpdateResult = {
  changed: string[];
  config: ClipConfig;
  revisions: { transcript_revision: number; render_revision: number };
  task: { task_id?: string; task_type?: string } | null;
  /** 本阶段恒为 true：配置与版本已保存，按配置真渲染属 Phase 2。 */
  render_deferred: boolean;
  render_note: string;
};

// ===== 转写（契约 §3.2 / §5）=====

export type TranscriptSegment = {
  /** 修订/覆盖的键。页面已有的成片分段可能没有这个字段（只作只读展示）。 */
  segment_id?: string;
  start: number;
  end: number;
  text: string;
  speaker?: string;
};

export type TranscriptRevisionChange = {
  segment_id: string;
  before?: string;
  after?: string;
  speaker_before?: string;
  speaker_after?: string;
};

export type TranscriptRevision = {
  revision: number;
  author: string;
  created_at: string;
  reason?: string | null;
  changes: TranscriptRevisionChange[];
};

export type TranscriptDocument = {
  project_id: string;
  clip_id: string;
  transcript_revision: number;
  approved_revision: number;
  raw_text?: string;
  corrected_text?: string;
  effective_text?: string;
  revisions: TranscriptRevision[];
  raw_segments: TranscriptSegment[];
  corrected_segments: TranscriptSegment[];
  effective_segments: TranscriptSegment[];
  /** 端点可选返回（`view=` 查询参数），缺省时前端按变体字段自行取。 */
  variant?: TranscriptVariant;
  segments?: TranscriptSegment[];
};

/** 转写端点不可用/未种子化时页面状态转成的本地兜底（只读提示，不伪造修订）。 */
export type TranscriptFallback = {
  segments: TranscriptSegment[];
  reason: string;
};

// ===== 字幕（契约 §3.3 / §5）=====

export type SubtitleRevisionEntry = {
  revision: number;
  ass_path?: string;
  srt_path?: string;
  cue_count?: number;
  created_at?: string;
  config_hash?: string;
};

export type SubtitleView = {
  project_id: string;
  clip_id: string;
  config: SubtitleConfig;
  cues: SubtitleCue[];
  overrides: SubtitleOverride[];
  revisions: SubtitleRevisionEntry[];
  latest_revision: number;
  /** 本阶段恒为 "phase2"：字幕版本已保存（预览用），烧录进导出成片属 Phase 2。 */
  export_burn_in: string;
  note: string;
};

export type SubtitleSaveResult = {
  revision: number;
  cue_count: number;
  ass_path?: string;
  srt_path?: string;
  config?: SubtitleConfig;
  export_burn_in?: string;
  note?: string;
  revisions?: SubtitleRevisionEntry[];
  latest_revision?: number;
};

// ===== 音频（契约 §3.4 / §5）=====

export type BgmUploadResult = {
  clip_id: string;
  track: AudioTrack;
  audio_tracks: AudioTrack[];
  peaks: number[];
};

// ===== 导出摘要（契约 §5）=====

export type RenderConfigEntry = {
  key: string;
  value: unknown;
  source: ConfigSource;
};

/** render-config 的「预览近似 / 导出精确」说明（后端唯一事实源）。 */
export type EffectsExportView = {
  channel: string;
  endpoint: string;
  preset: EffectPresetName;
  preset_label: string;
  hand_edited: string[];
  picture: EffectsPicture;
  ratio: EffectsRatio;
  ratio_preview_only: boolean;
  transition: { enabled: boolean; type: TransitionType; duration_s: number };
  audio: EffectsAudio;
  voice_volume: number;
  muted: boolean;
  bgm_track_count: number;
  preview_approximate: string;
  export_exact: string;
  geometry_note: string;
  ratio_note: string;
  audio_note: string;
  approximate_keys: string[];
  exact_keys: string[];
};

export type RenderConfigView = {
  project_id: string;
  clip_id: string;
  binding: "clip" | "source";
  effective: RenderConfigEntry[];
  sources: Record<string, ConfigSource>;
  export: { resolution: ExportResolution | string; deferred: boolean; note: string };
  /** 四个风格预设的参数组（后端唯一事实源）。 */
  presets?: Record<string, EffectPreset>;
  effects_export?: EffectsExportView;
  config: ClipConfig;
};

// ===== UI 侧 =====

/** 每一次写后端的控件统一显示的保存状态：保存中 / 已保存 / 失败原因。 */
export type SaveState = {
  status: "idle" | "saving" | "saved" | "error";
  /** 失败原因或「已保存 rev-N」等可读文案。 */
  message?: string;
  /** 保存成功后的版本号（字幕 rev-N / 转写 vN）。 */
  revision?: number;
};

export const IDLE_SAVE: SaveState = { status: "idle" };

/** 面板绑定：选中的成片 → clip_id；未选成片但已载入源片 → 保留哨兵 "source"。 */
export type InspectorBinding = {
  projectId: string;
  /** clip_id 或哨兵 "source"。 */
  clipId: string;
  /** 用于界面提示：是否绑定到具体成片。 */
  isSource: boolean;
};

/**
 * 绑定推导（纯函数，可单测）：与契约 §1 一致。
 *  * 选中成片 → 该成片的 clip_id（配置读项目台账）；
 *  * 无成片但有项目 + 素材 → 哨兵 "source"（配置落在 clips.json 顶层 source_config）；
 *  * 都没有 → null（面板显示「无可编辑对象」，不发任何请求）。
 */
export function resolveInspectorBinding(input: {
  projectId?: string | null;
  clipId?: string | null;
  hasMedia?: boolean;
}): InspectorBinding | null {
  const projectId = input.projectId?.trim();
  const clipId = input.clipId?.trim();
  if (clipId && projectId) return { projectId, clipId, isSource: false };
  if (projectId && input.hasMedia) return { projectId, clipId: "source", isSource: true };
  return null;
}

/** 两处绑定是否指向同一个对象（决定面板要不要重新拉数据）。 */
export function sameBinding(a: InspectorBinding | null, b: InspectorBinding | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return a.projectId === b.projectId && a.clipId === b.clipId;
}

/** 封面历史版本条目（GET …/cover-history 的 enriched revisions[]，2026-10-13 版本管理页）。 */
export type ClipCoverHistoryEntry = {
  revision: number;
  created_at: string | null;
  kind: string | null;
  style_id?: string | null;
  cover_asset_id?: string | null;
  sha256?: string | null;
  instruction?: string | null;
  /** auto = 自动剪辑首版；assistant_regenerate = 重制封面（卡片按钮与 AI 助手共用同一任务链）。 */
  source?: "auto" | "assistant_regenerate" | string | null;
  title?: string | null;
  hook?: string | null;
  qa_status?: string | null;
  path?: string | null;
  task_id?: string | null;
  active?: boolean;
  deletable?: boolean;
};

/** 契约 §4 的导出侧标注文案（UI 必须可见）。 */
export const PHASE2_LABELS = {
  subtitle:
    "字幕版本已保存（预览用）；烧录进导出成片属 Phase 2",
  aspect:
    "导出仍按流水线 9:16（Phase 2 支持其他比例）",
  resolution: "Phase 2 生效",
  audio: "BGM 混音、淡入淡出只在 Phase 2 的导出成片里生效；本阶段保存的是可追溯的配置与版本。",
  effects: "滤镜、转场、片尾卡只在 Phase 2 的导出成片里生效；本阶段保存的是可追溯的配置与版本。",
  transform: "画面设置已写入片段（预览与刷新恢复同源）；按配置真渲染属 Phase 2。",
} as const;
