"use client";

// Inspector 外壳（契约 §6）：固定 5 个标签 转写｜字幕｜封面与画面｜音频｜效果设置。
//
// 面板绑定到**当前选中的对象**（契约 §1）：
//   * 选中成片 → clip_id（配置读项目台账）；
//   * 未选成片但已载入素材 → 哨兵 clip_id = "source"（配置读 clips.json 顶层 source_config）；
//   * 都没有 → 不发任何请求，只显示「无可编辑对象」。
// 切换选中对象（binding 变化）时，本地草稿与已拉取数据整体重置，再按新绑定重新拉取。
//
// 这一层只管编排：fetch + 草稿状态 + 回调上抛（字幕配置给播放器叠加层与时间线字幕轨）。

import { useEffect, useMemo, useRef, useState } from "react";
import { Info, LoaderCircle } from "lucide-react";

import { defaultTransform, type BackgroundMode, type ClipTransform } from "@/lib/clip-transform";
import type { ViewerAspect } from "@/components/studio/viewer-aspect";

import { errorText, getClipConfig, getRenderConfig, getSubtitles, putClipConfig } from "./api";
import { CoverTemplateLibraryPanel } from "./cover-template-library";
import { CoverWorkspace } from "./cover-workspace";
import type { CoverTemplateLibrary } from "@/lib/cover-template-library";
import type { CoverAssetItem } from "@/lib/studio-cover-assets";
import { CoverPictureTab } from "./cover-picture-tab";
import { EffectsTab } from "./effects-tab";
import { TranscriptTab } from "./transcript-tab";
import {
  DEFAULT_EFFECTS,
  DEFAULT_SUBTITLE_CONFIG,
  type AudioTrack,
  type ClipConfigView,
  type ClipCoverHistoryEntry,
  type Effects,
  type RenderConfigView,
  type SubtitleConfig,
  type SubtitleCue,

  type TranscriptSegment,
} from "./types";

export type InspectorTabId = "transcript" | "subtitle" | "cover" | "audio" | "effects";

export const INSPECTOR_TABS: { id: InspectorTabId; label: string; title: string }[] = [
  { id: "transcript", label: "转写", title: "转写" },
  // 标签栏单行平均分宽：短文案「封面」节省空间，完整名称放 tooltip。
  { id: "cover", label: "封面", title: "封面与画面" },
  { id: "effects", label: "效果", title: "效果设置" },
];

export type InspectorSubtitleState = {
  cues: SubtitleCue[];
  config: SubtitleConfig;
};

export type AdoptOption = { clip_id: string; name: string };

export type InspectorPaneProps = {
  projectId: string | null;
  /** clip_id 或哨兵 "source"；null = 无可编辑对象。 */
  clipId: string | null;
  baseUrl: string;
  activeTab: InspectorTabId;
  onTabChange: (tab: InspectorTabId) => void;
  /** 页面已加载的分段（转写端点不可用时的只读兜底）。 */
  fallbackSegments: TranscriptSegment[];
  currentTime: number;
  duration: number;
  onSeek: (seconds: number) => void;
  onSubtitleStateChange: (state: InspectorSubtitleState | null) => void;
  onPlayerVolumeChange?: (config: { voice_volume: number; muted: boolean; audio_tracks: AudioTrack[] }) => void;
  /** 效果草稿上报（页面据此做颜色类实时预览与导出发送判定）。 */
  onEffectsChange?: (effects: Effects) => void;

  // ===== 封面（页面既有 handler）=====
  boundToActiveClip: boolean;
  clipName: string | null;
  coverImageUrl: string | null;
  coverAssetId: string | null;
  coverRevision: number | null;
  coverVersionLabel: string | null;
  coverStatusText: string;
  title: string | null;
  coverBusy: boolean;
  canUndoCover: boolean;
  coverUndoRevision: number | null;
  onRegenerateCover: () => void;
  onEditTitle: () => void;
  onShowCoverHistory: () => void;
  onAdoptCover: (sourceClipId: string) => void;
  onUndoCover: () => void;
  coverHistory: ClipCoverHistoryEntry[];
  /** 封面版本管理（2026-10-13）：透传给 CoverPictureTab。 */
  coverTaskFailure?: { message: string; instruction: string } | null;
  onRegenerateWithInstruction?: (instruction: string) => void;
  onRetryCoverTask?: () => void;
  onActivateRevision?: (revision: number) => void;
  onDeleteRevision?: (revision: number) => void;
  onPreviewInPlayer?: () => void;
  artifactUrlFor?: (taskId: string, path: string, download?: boolean) => string;
  coverCacheVersion?: number;
  /** 封面图片区（2026-10-21 精简版）：上方封面 + 点击展开应用/删除。 */
  coverWorkflow?: {
    hasCover: boolean | null;
    pending: boolean;
    applied: boolean;
    busy: boolean;
    coverImageUrl: string | null;
    /** 应用渲染任务进度（轮询实时刷新）；null = 未在应用。 */
    applyProgress?: { stage: string; percent: number } | null;
    onApply: () => void;
    onDelete: () => void;
  };
  /** 封面版型库：版本化注册表 + 单选 + 真实渲染（不调用模型）。 */
  coverTemplates?: {
    exampleUrlBase?: string;
    library: CoverTemplateLibrary | null;
    loading: boolean;
    error: string | null;
    selectedTemplateId: string | null;
    generateBusyTemplateId: string | null;
    onSelectTemplate: (templateId: string) => void;
    onGenerate: () => void;
    onRetryLoad?: () => void;
  };
  coverHistoryVisible: boolean;
  coverHistoryLoading: boolean;
  adoptOptions: AdoptOption[];
  adoptSource: string | null;
  onAdoptSourceChange: (clipId: string) => void;

  // ===== 画面（页面既有变换机制）=====
  transform: ClipTransform | null;
  /** 与配置绑定分离：同一 source 的不同时间线片段也必须隔离草稿。 */
  transformSelectionKey?: string;
  /** 读取已保存配方时同步播放器；不经过预览草稿或持久化回调。 */
  onAppliedTransformLoaded?: (next: ClipTransform) => void;
  transformLocked: boolean;
  transformBackground: BackgroundMode;
  ratioLock: boolean;
  viewerZoom: number;
  viewerAspect: ViewerAspect;
  onTransformDraftChange: (next: ClipTransform) => void;
  onTransformBackgroundChange: (next: BackgroundMode) => void;
  onRatioLockChange: (next: boolean) => void;
  onViewerZoomChange: (percent: number) => void;
  onFitView: () => void;
  onOriginalRatio: () => void;
  onCanvasMode: () => void;
  onResetTransform: () => void;
  onViewerAspectChange: (aspect: ViewerAspect) => void;
  /** 应用：写 PUT /studio/clips/{p}/{c}/config 的 transform（取消不调用）。 */
  onApplyTransform: (next: ClipTransform, background?: BackgroundMode) => Promise<unknown> | void;
  onCancelTransform: () => void;
};

function transformsEqual(a: ClipTransform | null, b: ClipTransform | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return JSON.stringify(a) === JSON.stringify(b);
}

function payloadToTransform(payload: Record<string, unknown> | null | undefined): ClipTransform | null {
  if (!payload) return null;
  return {
    scale: Number(payload.scale ?? 1),
    positionX: Number(payload.positionX ?? 0),
    positionY: Number(payload.positionY ?? 0),
    crop: (payload.crop as ClipTransform["crop"]) ?? { left: 0, top: 0, right: 1, bottom: 1 },
    fitMode: (payload.fitMode as ClipTransform["fitMode"]) ?? "cover",
    // 契约里 ClipTransform.rotation 恒为 0（lib/clip-transform.ts 的解析器会拒绝非 0）。
    rotation: 0,
  };
}

export function InspectorPane(props: InspectorPaneProps) {
  const {
    projectId, clipId, baseUrl, activeTab, onTabChange, fallbackSegments, currentTime, duration,
    onSeek, onSubtitleStateChange, onPlayerVolumeChange, onEffectsChange,
    boundToActiveClip, clipName, coverImageUrl, coverAssetId, coverRevision, coverVersionLabel,
    coverStatusText, title, coverBusy, canUndoCover, coverUndoRevision,
    onRegenerateCover, onEditTitle, onShowCoverHistory, onAdoptCover, onUndoCover,
    coverHistory, coverHistoryVisible, coverHistoryLoading, adoptOptions, adoptSource, onAdoptSourceChange,
    coverTaskFailure, onRegenerateWithInstruction, onRetryCoverTask,
    onActivateRevision, onDeleteRevision, onPreviewInPlayer, artifactUrlFor, coverCacheVersion,
    coverWorkflow, coverTemplates,
    transform, transformSelectionKey, onAppliedTransformLoaded, transformLocked, transformBackground, ratioLock, viewerZoom, viewerAspect,
    onTransformDraftChange, onTransformBackgroundChange, onRatioLockChange, onViewerZoomChange,
    onFitView, onViewerAspectChange,
    onApplyTransform, onCancelTransform,
  } = props;

  const bindingKey = projectId && clipId ? `${projectId}::${clipId}` : "";
  const hasBinding = Boolean(projectId && clipId);

  const [configView, setConfigView] = useState<ClipConfigView | null>(null);
  const [configLoading, setConfigLoading] = useState(false);
  const [configError, setConfigError] = useState<string | null>(null);
  // 字幕视图的加载态随「字幕」人工编辑入口一起移除；cues/config 的拉取与上抛保留
  // （播放器叠加层与底部时间线字幕轨的唯一数据来源仍是这里）。
  const [renderConfig, setRenderConfig] = useState<RenderConfigView | null>(null);
  const [renderConfigLoading, setRenderConfigLoading] = useState(false);
  const [renderConfigError, setRenderConfigError] = useState<string | null>(null);
  const [renderConfigVersion, setRenderConfigVersion] = useState(0);
  const [transcriptVersion, setTranscriptVersion] = useState(0);

  // 字幕草稿（叠加层与底部时间线字幕轨都用这一份）。
  const [subtitleConfig, setSubtitleConfig] = useState<SubtitleConfig>(DEFAULT_SUBTITLE_CONFIG);
  const [subtitleCues, setSubtitleCues] = useState<SubtitleCue[]>([]);
  const [subtitleError, setSubtitleError] = useState<string | null>(null);
  // 效果草稿（保存后摘要要刷新）。
  const [effects, setEffects] = useState<Effects>(DEFAULT_EFFECTS);
  const [audioConfig, setAudioConfig] = useState<{ voice_volume: number; muted: boolean; audio_tracks: AudioTrack[] }>({
    voice_volume: 1,
    muted: false,
    audio_tracks: [],
  });
  const [transformNotice, setTransformNotice] = useState<{ status: "idle" | "saving" | "saved" | "error"; message?: string }>({ status: "idle" });
  const [transformDraft, setTransformDraft] = useState<ClipTransform | null>(transform);
  const [transformSnapshot, setTransformSnapshot] = useState<ClipTransform | null>(transform);

  const [backgroundDraft, setBackgroundDraft] = useState(transformBackground);
  const [backgroundSnapshot, setBackgroundSnapshot] = useState(transformBackground);
  const mountedRef = useRef(true);
  const draftBinding = transformSelectionKey || bindingKey;
  const draftBindingRef = useRef(draftBinding);
  const transformEditedRef = useRef(false);
  const transformApplySeq = useRef(0);
  const transformPendingRef = useRef<{ binding: string; request: number } | null>(null);
  const appliedTransformReportRef = useRef(onAppliedTransformLoaded);
  appliedTransformReportRef.current = onAppliedTransformLoaded;
  if (draftBindingRef.current !== draftBinding) {
    draftBindingRef.current = draftBinding; transformEditedRef.current = false; transformApplySeq.current += 1;
    setTransformDraft(transform); setTransformSnapshot(transform); setTransformNotice({ status: "idle" });
    setBackgroundDraft(transformBackground); setBackgroundSnapshot(transformBackground);
  }
  const lastBindingRef = useRef(bindingKey);
  const subtitleReportRef = useRef(onSubtitleStateChange);
  subtitleReportRef.current = onSubtitleStateChange;
  const effectsReportRef = useRef(onEffectsChange);
  effectsReportRef.current = onEffectsChange;
  const viewerAspectRef = useRef(onViewerAspectChange);
  viewerAspectRef.current = onViewerAspectChange;

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // 绑定变化 = 换了一个可编辑对象：本地草稿全部重置（页面状态由页面自己清），
  // 避免上一个对象的数据短暂显示在新对象上。
  if (lastBindingRef.current !== bindingKey) {
    lastBindingRef.current = bindingKey;
    setConfigView(null);
    setRenderConfig(null);
    setSubtitleConfig(DEFAULT_SUBTITLE_CONFIG);
    setSubtitleCues([]);
    setEffects(DEFAULT_EFFECTS);
    effectsReportRef.current?.(DEFAULT_EFFECTS);
    setAudioConfig({ voice_volume: 1, muted: false, audio_tracks: [] });
    setTransformNotice({ status: "idle" });
    setTranscriptVersion((value) => value + 1);
  }

  const clipPayload = configView?.config ?? null;

  // 配置投影：GET …/config 是配方的唯一事实源（subtitle_config / effects / audio / transform）。
  useEffect(() => {
    if (!hasBinding || !projectId || !clipId) {
      setConfigView(null);
      setConfigError(null);
      setConfigLoading(false);
      return undefined;
    }
    let cancelled = false;
    const requestedSelection = draftBinding;
    setSubtitleError(null);
    setConfigLoading(true);
    void (async () => {
      try {
        const view = await getClipConfig(baseUrl, projectId, clipId);
        if (cancelled || !mountedRef.current) return;
        setConfigView(view);
        setConfigError(null);
        if (view?.config) {
          setSubtitleConfig(view.config.subtitle_config || DEFAULT_SUBTITLE_CONFIG);
          const loadedEffects = view.config.effects || DEFAULT_EFFECTS;
          setEffects(loadedEffects);
          effectsReportRef.current?.(loadedEffects);
          // 画幅比例落盘后恢复预览形状（仅预览，导出恒为 9:16）。
          const ratioName = loadedEffects.ratio?.name;
          if (ratioName) viewerAspectRef.current?.(ratioName as ViewerAspect);
          setAudioConfig({
            voice_volume: view.config.voice_volume ?? 1,
            muted: Boolean(view.config.muted),
            audio_tracks: view.config.audio_tracks || [],
          });
          const persisted = payloadToTransform(view.config.transform) || defaultTransform();
          if (requestedSelection === draftBindingRef.current && !transformEditedRef.current && (clipId !== "source" || !transformSelectionKey)) {
            setTransformDraft(persisted); setTransformSnapshot(persisted);
            appliedTransformReportRef.current?.(persisted);
          }
          onPlayerVolumeChange?.({
            voice_volume: view.config.voice_volume ?? 1,
            muted: Boolean(view.config.muted),
            audio_tracks: view.config.audio_tracks || [],
          });
        }
      } catch (error) {
        if (cancelled || !mountedRef.current) return;
        setConfigView(null);
        setConfigError(errorText(error));
      } finally {
        if (!cancelled && mountedRef.current) setConfigLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // onPlayerVolumeChange 只在配置到达时上报一次，不进依赖以免每次渲染都重拉。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseUrl, projectId, clipId, hasBinding, transcriptVersion]);

  // 字幕视图：只取 cues + config 上抛（播放器叠加层与时间线字幕轨的数据源）。
  // 「字幕」人工编辑入口已移除：人工调整改走 AI 剪辑助手自然语言指令。
  useEffect(() => {
    if (!hasBinding || !projectId || !clipId) {
      setSubtitleCues([]);
      setSubtitleConfig(DEFAULT_SUBTITLE_CONFIG);
      setSubtitleError(null);
      return undefined;
    }
    let cancelled = false;
    setSubtitleError(null);
    void (async () => {
      try {
        const view = await getSubtitles(baseUrl, projectId, clipId);
        if (cancelled || !mountedRef.current) return;
        setSubtitleCues(view.cues || []);
        setSubtitleConfig(view.config || DEFAULT_SUBTITLE_CONFIG);
        setSubtitleError(null);
      } catch (error) {
        // 拉取失败不造假数据：叠加层与字幕轨回退到转写分段（页面已有兜底）。
        if (cancelled || !mountedRef.current) return;
        setSubtitleCues([]);
        setSubtitleError(errorText(error));
      }
    })();
    return () => { cancelled = true; };
  }, [baseUrl, projectId, clipId, hasBinding, transcriptVersion]);

  // 导出摘要：每次保存效果/字幕/画面后 bump renderConfigVersion 重新读取。
  useEffect(() => {
    if (!hasBinding || !projectId || !clipId) {
      setRenderConfig(null);
      setRenderConfigError(null);
      return undefined;
    }
    let cancelled = false;
    setRenderConfigLoading(true);
    void (async () => {
      try {
        const view = await getRenderConfig(baseUrl, projectId, clipId);
        if (cancelled || !mountedRef.current) return;
        setRenderConfig(view);
        setRenderConfigError(null);
      } catch (error) {
        if (cancelled || !mountedRef.current) return;
        setRenderConfig(null);
        setRenderConfigError(errorText(error));
      } finally {
        if (!cancelled && mountedRef.current) setRenderConfigLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [baseUrl, projectId, clipId, hasBinding, renderConfigVersion]);

  // 上报字幕状态：播放器叠加层与时间线字幕轨的唯一数据来源。
  useEffect(() => {
    subtitleReportRef.current(hasBinding ? { cues: subtitleCues, config: subtitleConfig } : null);
  }, [hasBinding, subtitleCues, subtitleConfig]);

  // 页面把 transform 恢复/切换后同步快照（未编辑时草稿跟随）。
  const transformSnapshotRef = useRef<ClipTransform | null>(transform);
  useEffect(() => {
    // 只在页面真的换了一组变换时才重置草稿，避免每次渲染都覆盖正在编辑的草稿。
    if (transformsEqual(transformSnapshotRef.current, transform)) return;
    transformSnapshotRef.current = transform;
    setTransformDraft(transform);
    setTransformSnapshot(transform);
    transformEditedRef.current = false;
    // transformDraft 有意不进依赖：它只用于「是否已同步」的判断。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transform]);

  useEffect(() => { setBackgroundDraft(transformBackground); setBackgroundSnapshot(transformBackground); }, [transformBackground]);
  const dirty = !transformsEqual(transformDraft, transformSnapshot) || backgroundDraft !== backgroundSnapshot;
  const handleBackgroundPreview = (background: BackgroundMode) => {
    setBackgroundDraft(background); setTransformNotice({ status: "idle" }); onTransformBackgroundChange(background);
  };

  const refreshRenderConfig = () => setRenderConfigVersion((value) => value + 1);

  const handleTransformPreview = (next: ClipTransform) => {
    transformEditedRef.current = true; setTransformNotice({ status: "idle" });
    setTransformDraft(next);
    onTransformDraftChange(next);
  };

  const handleApply = async () => {
    if (!transformDraft || transformNotice.status === "saving") return;
    const requestedBinding = draftBinding, request = ++transformApplySeq.current;
    const current = () => mountedRef.current && draftBindingRef.current === requestedBinding && transformApplySeq.current === request;
    const pending = { binding: requestedBinding, request }; transformPendingRef.current = pending;
    setTransformNotice({ status: "saving" });
    try {
      await onApplyTransform(transformDraft, backgroundDraft);
      if (current()) {
        transformEditedRef.current = false;
        setTransformSnapshot(transformDraft); setBackgroundSnapshot(backgroundDraft);
        setTransformNotice({ status: "saved", message: "画面设置已保存" });
        refreshRenderConfig();
      }
    } catch (error) {
      if (current()) setTransformNotice({ status: "error", message: errorText(error) });
    } finally { if (transformPendingRef.current === pending) transformPendingRef.current = null; }
  };

  const handleCancel = () => {
    if (transformPendingRef.current?.binding === draftBinding) return;
    transformApplySeq.current += 1; transformEditedRef.current = false;
    setTransformDraft(transformSnapshot); setBackgroundDraft(backgroundSnapshot);
    setTransformNotice({ status: "idle" });
    onCancelTransform();
  };

  useEffect(() => {
    if (!dirty || activeTab !== "effects" || transformNotice.status === "saving") return;
    const cancel = (event: KeyboardEvent) => { if (event.key === "Escape") handleCancel(); };
    window.addEventListener("keydown", cancel);
    return () => window.removeEventListener("keydown", cancel);
  }, [dirty, activeTab, transformSnapshot, backgroundSnapshot, onCancelTransform, transformNotice.status]);

  const handleSaveAudio = async (next: { voice_volume: number; muted: boolean; audio_tracks: AudioTrack[] }) => {
    if (!projectId || !clipId) return;
    await putClipConfig(baseUrl, projectId, clipId, {
      voice_volume: next.voice_volume,
      muted: next.muted,
      audio_tracks: next.audio_tracks,
    });
    refreshRenderConfig();
  };

  const handleSaveEffects = async (next: Effects) => {
    if (!projectId || !clipId) return;
    const result = await putClipConfig(baseUrl, projectId, clipId, { effects: next });
    // 后端是预设展开/校验的事实源：以其返回的 config.effects 为准同步本地草稿。
    const saved = result?.config?.effects;
    if (saved) {
      setEffects(saved);
      effectsReportRef.current?.(saved);
    }
    refreshRenderConfig();
  };

  // 传给「效果」标签的音频配置必须有稳定引用：该标签用它同步本地草稿，
  // 每次渲染都新建对象会让草稿被反复重置。
  const audioPatch = useMemo(
    () => ({
      voice_volume: audioConfig.voice_volume,
      muted: audioConfig.muted,
      audio_tracks: audioConfig.audio_tracks,
    }),
    [audioConfig.voice_volume, audioConfig.muted, audioConfig.audio_tracks],
  );

  return <div className="inspector-pane" data-testid="inspector-pane" data-binding={bindingKey || "none"}>
    <div className="inspector-tabs" role="tablist" aria-label="Inspector 标签">
      {INSPECTOR_TABS.map((tab) => <button
        key={tab.id}
        type="button"
        role="tab"
        aria-selected={activeTab === tab.id}
        aria-label={tab.title}
        title={tab.title}
        className={activeTab === tab.id ? "active" : ""}
        data-testid={`inspector-tab-${tab.id}`}
        onClick={() => onTabChange(tab.id)}
      >{tab.label}</button>)}
    </div>
    <div className="inspector-pane-body">
      {!hasBinding && <p className="inspector-hint" data-testid="inspector-no-binding">
        <Info size={13} />没有可编辑的对象：请选择一条成片，或先导入素材（未选成片时会绑定到项目/源片级配置）。
      </p>}
      {hasBinding && configLoading && !configView && <p className="inspector-hint"><LoaderCircle size={13} />正在读取 clip 配置…</p>}
      {hasBinding && configError && <p className="inspector-error" role="alert" data-testid="inspector-config-error">读取配置失败：{configError}</p>}
      {hasBinding && subtitleError && <p className="inspector-error" role="alert" data-testid="inspector-subtitles-error">读取字幕失败：{subtitleError}</p>}
      {hasBinding && clipId === "source" && <p className="inspector-hint" data-testid="inspector-source-binding">
        当前绑定：项目/源片级（clip_id = source），配置读写落在 clips.json 顶层 source_config。
      </p>}

      {hasBinding && activeTab === "transcript" && projectId && clipId && <TranscriptTab
        key={bindingKey}
        projectId={projectId}
        clipId={clipId}
        baseUrl={baseUrl}
        fallbackSegments={fallbackSegments}
        onSeek={onSeek}
        onChanged={() => setTranscriptVersion((value) => value + 1)}
      />}

      {/* 独立封面素材总览（2026-10-20）：全部切片的封面网格，不依赖选中态；

      {/* 封面版型库（2026-10-21 阶段）：版本化注册表 + 单选 + 模拟生成；
          未选切片只出提示；选择事实源=台账卡片（切换切片自动跟随、刷新自动恢复）。 */}
      {/* 封面图片区（2026-10-21 精简版）：已生成封面/占位提示，点击展开应用/删除。 */}
      {activeTab === "cover" && <CoverWorkspace
        clipId={clipId && clipId !== "source" ? clipId : null}
        clipName={clipName}
        coverImageUrl={coverWorkflow?.coverImageUrl ?? null}
        workflow={{ hasCover: coverWorkflow?.hasCover ?? null,
                    pending: coverWorkflow?.pending ?? false,
                    applied: coverWorkflow?.applied ?? false }}
        busy={coverWorkflow?.busy ?? false}
        applyProgress={coverWorkflow?.applyProgress ?? null}
        onApply={() => coverWorkflow?.onApply()}
        onDelete={() => coverWorkflow?.onDelete()} />}

      {/* 封面版型库：版本化注册表 + 单选 + 真实渲染；
          未选切片只出提示；选择事实源=台账卡片（切换切片自动跟随、刷新自动恢复）。 */}
      {activeTab === "cover" && <CoverTemplateLibraryPanel
        clipId={clipId && clipId !== "source" ? clipId : null}
        exampleUrlBase={coverTemplates?.exampleUrlBase ?? ""}
        library={coverTemplates?.library ?? null}
        loading={coverTemplates?.loading ?? false}
        error={coverTemplates?.error ?? null}
        selectedTemplateId={coverTemplates?.selectedTemplateId ?? null}
        generateBusyTemplateId={coverTemplates?.generateBusyTemplateId ?? null}
        onSelectTemplate={(templateId) => coverTemplates?.onSelectTemplate(templateId)}
        onGenerate={() => coverTemplates?.onGenerate()}
        onRetryLoad={() => coverTemplates?.onRetryLoad?.()} />}

      {/* 画面/裁切重构图（2026-10-21 用户决定）：封面页只保留封面图片与版型卡片，
          画面设置整体移到「效果」页 —— 接线与 PUT config 契约保持不变。 */}
      {hasBinding && activeTab === "effects" && <fieldset disabled={transformNotice.status === "saving"} style={{ border: 0, padding: 0, minWidth: 0 }} aria-label="画面设置面板"><CoverPictureTab
        projectId={projectId}
        clipId={clipId}
        boundToActiveClip={boundToActiveClip}
        clipName={clipName}
        coverImageUrl={coverImageUrl}
        coverAssetId={coverAssetId}
        coverRevision={coverRevision}
        coverVersionLabel={coverVersionLabel}
        coverStatusText={coverStatusText}
        title={title}
        busy={coverBusy}
        canUndoCover={canUndoCover}
        undoRevision={coverUndoRevision}
        onRegenerateCover={onRegenerateCover}
        coverTaskFailure={coverTaskFailure ?? null}
        onRegenerateWithInstruction={(instruction) => onRegenerateWithInstruction?.(instruction)}
        onRetryCoverTask={() => onRetryCoverTask?.()}
        onActivateRevision={(revision) => onActivateRevision?.(revision)}
        onDeleteRevision={(revision) => onDeleteRevision?.(revision)}
        onPreviewInPlayer={() => onPreviewInPlayer?.()}
        artifactUrlFor={artifactUrlFor ?? (() => "")}
        coverCacheVersion={coverCacheVersion ?? 0}
        onEditTitle={onEditTitle}
        onShowCoverHistory={onShowCoverHistory}
        onAdoptCover={onAdoptCover}
        onUndoCover={onUndoCover}
        coverHistory={coverHistory}
        coverHistoryVisible={coverHistoryVisible}
        coverHistoryLoading={coverHistoryLoading}
        adoptOptions={adoptOptions}
        adoptSource={adoptSource}
        onAdoptSourceChange={onAdoptSourceChange}
        transform={transformDraft}
        transformLocked={transformLocked || configLoading || transformNotice.status === "saving"}
        background={backgroundDraft}
        ratioLock={ratioLock}
        viewerZoom={viewerZoom}
        viewerAspect={viewerAspect}
        dirty={dirty}
        onTransformChange={handleTransformPreview}
        onBackgroundChange={handleBackgroundPreview}
        onRatioLockChange={onRatioLockChange}
        onViewerZoomChange={onViewerZoomChange}
        onFitView={onFitView}
        onOriginalRatio={() => handleTransformPreview({ ...defaultTransform(), fitMode: "contain" })}
        onCanvasMode={() => handleTransformPreview({ ...(transformDraft || defaultTransform()), fitMode: "cover", scale: 1, positionX: 0, positionY: 0 })}
        onResetTransform={() => handleTransformPreview(defaultTransform())}
        onViewerAspectChange={onViewerAspectChange}
        onApply={() => void handleApply()}
        onCancel={handleCancel}
      /></fieldset>}

      {hasBinding && activeTab === "effects" && <EffectsTab
        effects={effects}
        onPreview={(next) => { setEffects(next); effectsReportRef.current?.(next); }}
        onSave={handleSaveEffects}
        audio={audioPatch}
        onPreviewAudio={(next) => { setAudioConfig(next); onPlayerVolumeChange?.(next); }}
        onSaveAudio={handleSaveAudio}
        transform={transformDraft}
        transformLocked={transformLocked}
        onTransformPreview={handleTransformPreview}
        onTransformApply={onApplyTransform}
        viewerAspect={viewerAspect}
        onViewerAspectChange={onViewerAspectChange}
        renderConfig={renderConfig}
        renderConfigLoading={renderConfigLoading}
        renderConfigError={renderConfigError}
        onRefreshRenderConfig={refreshRenderConfig}
      />}

      {hasBinding && activeTab === "effects" && <p className="inspector-hint" data-testid="inspector-background-scope">背景应用于本次工作区导出，刷新后恢复默认。</p>}
      {hasBinding && transformNotice.status !== "idle" && transformNotice.status !== "saved" && activeTab === "effects" && (
        <p className={transformNotice.status === "error" ? "inspector-error" : "inspector-hint"} role="status" data-testid="inspector-transform-state">
          {transformNotice.status === "saving" ? "正在保存画面设置，提交尚未完成，请等待…" : `保存画面设置失败：${transformNotice.message || "未知原因"}`}
        </p>
      )}
      {hasBinding && transformNotice.status === "saved" && activeTab === "effects" && (
        <p className="inspector-hint" role="status" data-testid="inspector-transform-state">已保存：画面变换已写入 clip 配置。背景应用于本次工作区导出，刷新后恢复默认。</p>
      )}
    </div>
  </div>;
}
