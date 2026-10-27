"use client";

// 「效果」标签 —— 成片风格微调（不是素材商城）。绑定当前选中的原片 / 成片。
//
// 五个分区（数值与后端 / 导出同源）：
//  1. 风格预设：医疗专业 / 口播清晰 / 科普重点 / 原始自然 —— 一组画面调整+转场+ducking
//     参数；选中即写入配置并即时预览；面板显示「当前预设 + 已手改项」。
//  2. 画面调整：亮度 / 对比度 / 饱和度 / 锐化 / 色温 / 滤镜（none|warm|cool|bw|vivid）。
//  3. 画幅与构图：9:16 / 16:9 / 1:1（仅影响预览，UI 明确标注）、适合画布、裁剪、缩放、位置
//     —— 复用既有 transform/clip_transform 语义，不另造第二套变换。
//  4. 转场：无 / 淡入淡出（导出为片段首尾 fade）。
//  5. 音频处理：原声音量 / BGM 音量 / 淡入淡出 / BGM 自动压低（ducking）——
//     沿用既有 voice_volume / muted / audio_tracks 字段；无 BGM 时置灰 + tooltip。
//
// 颜色类预览用 CSS filter 近似（数值与 FFmpeg 参数同源，由页面应用到播放器视频元素）；
// 几何类预览与导出严格同源。render_config 摘要卡片显示生效值与来源。

import { LoaderCircle } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ClipTransform } from "@/lib/clip-transform";
import type { ViewerAspect } from "@/components/studio/viewer-aspect";

import { errorText } from "./api";
import { effectsPreviewFilter, handEditedKeys } from "./effects-preview";
import { RenderConfigSummary } from "./render-config-summary";
import { SaveBadge } from "./save-state";
import {
  EFFECT_PRESET_NAMES,
  PICTURE_FILTER_NAMES,
  RATIO_NAMES,
  type AudioTrack,
  type EffectPreset,
  type EffectPresetName,
  type Effects,
  type EffectsPicture,
  type PictureFilterName,
  type RatioName,
  type RenderConfigView,
  type SaveState,
  type TransitionType,
  IDLE_SAVE,
} from "./types";

export type EffectsAudioConfig = {
  voice_volume: number;
  muted: boolean;
  audio_tracks: AudioTrack[];
};

export type EffectsTabProps = {
  effects: Effects;
  onPreview: (next: Effects) => void;
  onSave: (next: Effects) => Promise<unknown>;
  /** 音频段落沿用既有 voice_volume / muted / audio_tracks。 */
  audio: EffectsAudioConfig;
  onPreviewAudio: (next: EffectsAudioConfig) => void;
  onSaveAudio: (next: EffectsAudioConfig) => Promise<unknown>;
  /** 画幅与构图的几何部分复用既有 transform（预览 + 导出同源）。 */
  transform: ClipTransform | null;
  transformLocked: boolean;
  onTransformPreview: (next: ClipTransform) => void;
  onTransformApply: (next: ClipTransform) => Promise<unknown> | void;
  viewerAspect: ViewerAspect;
  onViewerAspectChange: (aspect: ViewerAspect) => void;
  renderConfig: RenderConfigView | null;
  renderConfigLoading?: boolean;
  renderConfigError?: string | null;
  onRefreshRenderConfig?: () => void;
};

const SAVE_DEBOUNCE_MS = 500;
const TRANSFORM_PERSIST_DEBOUNCE_MS = 500;
const CANVAS_WIDTH = 1080;
const CANVAS_HEIGHT = 1920;

const PRESET_LABELS: Record<EffectPresetName, string> = {
  medical: "医疗专业",
  clear: "口播清晰",
  science: "科普重点",
  natural: "原始自然",
};

// UI 兜底预设：render-config 到达前（或读取失败时）用于即时预览。
// 后端 clip_config.EFFECT_PRESETS 是唯一事实源；配置到达后一律以 render-config 为准。
const FALLBACK_PRESETS: Record<EffectPresetName, EffectPreset> = {
  medical: {
    label: "医疗专业",
    picture: { brightness: 0, contrast: 1.08, saturation: 1, sharpen: 0.25, temperature: 0, filter: "none" },
    transition: { enabled: true, type: "fade", duration_s: 0.4 },
    audio: { ducking: true, ducking_db: -12 },
  },
  clear: {
    label: "口播清晰",
    picture: { brightness: 0.03, contrast: 1.12, saturation: 1.06, sharpen: 0.5, temperature: 0.03, filter: "none" },
    transition: { enabled: false, type: "none", duration_s: 0.4 },
    audio: { ducking: true, ducking_db: -12 },
  },
  science: {
    label: "科普重点",
    picture: { brightness: 0.02, contrast: 1.15, saturation: 1.2, sharpen: 0.35, temperature: 0.12, filter: "none" },
    transition: { enabled: true, type: "fade", duration_s: 0.5 },
    audio: { ducking: true, ducking_db: -10 },
  },
  natural: {
    label: "原始自然",
    picture: { brightness: 0, contrast: 1, saturation: 1, sharpen: 0, temperature: 0, filter: "none" },
    transition: { enabled: false, type: "none", duration_s: 0.5 },
    audio: { ducking: false, ducking_db: -12 },
  },
};

const FILTER_LABELS: Record<PictureFilterName, string> = {
  none: "无",
  warm: "暖色",
  cool: "冷色",
  bw: "黑白",
  vivid: "鲜艳",
};

const RATIO_LABELS: Record<RatioName, string> = {
  "9:16": "9:16 · 竖屏",
  "16:9": "16:9 · 横屏",
  "1:1": "1:1 · 方形",
};

function nearestPreset(presets: Record<string, EffectPreset>, name: EffectPresetName): EffectPreset {
  return presets[name] ?? FALLBACK_PRESETS[name];
}

export function EffectsTab({
  effects, onPreview, onSave, audio, onPreviewAudio, onSaveAudio,
  transform, transformLocked, onTransformPreview, onTransformApply,
  viewerAspect, onViewerAspectChange,
  renderConfig, renderConfigLoading, renderConfigError, onRefreshRenderConfig,
}: EffectsTabProps) {
  const [draft, setDraft] = useState<Effects>(effects);
  const [audioDraft, setAudioDraft] = useState<EffectsAudioConfig>(audio);
  const [transformDraft, setTransformDraft] = useState<ClipTransform | null>(transform);
  const [save, setSave] = useState<SaveState>(IDLE_SAVE);
  const mountedRef = useRef(true);
  const timerRef = useRef<number | null>(null);
  const transformTimerRef = useRef<number | null>(null);
  const draftRef = useRef(draft);
  const audioRef = useRef(audioDraft);
  const transformRef = useRef(transformDraft);
  draftRef.current = draft;
  audioRef.current = audioDraft;
  transformRef.current = transformDraft;

  useEffect(() => () => { mountedRef.current = false; }, []);
  useEffect(() => { setDraft(effects); }, [effects]);
  useEffect(() => {
    setAudioDraft({
      voice_volume: audio.voice_volume,
      muted: audio.muted,
      audio_tracks: audio.audio_tracks,
    });
  }, [audio.voice_volume, audio.muted, audio.audio_tracks]);
  useEffect(() => { setTransformDraft(transform); }, [transform]);

  const scheduleEffects = useCallback(() => {
    if (timerRef.current) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => {
      setSave({ status: "saving" });
      Promise.resolve(onSave(draftRef.current))
        .then(() => {
          if (!mountedRef.current) return;
          setSave({ status: "saved", message: "效果配置已保存" });
          onRefreshRenderConfig?.();
        })
        .catch((error: unknown) => { if (mountedRef.current) setSave({ status: "error", message: errorText(error) }); });
    }, SAVE_DEBOUNCE_MS);
  }, [onSave, onRefreshRenderConfig]);

  const scheduleAudio = useCallback(() => {
    if (timerRef.current) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => {
      setSave({ status: "saving" });
      Promise.resolve(onSaveAudio(audioRef.current))
        .then(() => {
          if (!mountedRef.current) return;
          setSave({ status: "saved", message: "音频设置已保存" });
          onRefreshRenderConfig?.();
        })
        .catch((error: unknown) => { if (mountedRef.current) setSave({ status: "error", message: errorText(error) }); });
    }, SAVE_DEBOUNCE_MS);
  }, [onSaveAudio, onRefreshRenderConfig]);

  const scheduleTransformPersist = useCallback(() => {
    if (transformTimerRef.current) window.clearTimeout(transformTimerRef.current);
    transformTimerRef.current = window.setTimeout(() => {
      const next = transformRef.current;
      if (!next) return;
      Promise.resolve(onTransformApply(next))
        .then(() => { if (mountedRef.current) onRefreshRenderConfig?.(); })
        .catch((error: unknown) => { if (mountedRef.current) setSave({ status: "error", message: errorText(error) }); });
    }, TRANSFORM_PERSIST_DEBOUNCE_MS);
  }, [onTransformApply, onRefreshRenderConfig]);

  const patchEffects = (next: Effects, persist = true) => {
    draftRef.current = next;
    setDraft(next);
    onPreview(next);
    if (persist) scheduleEffects();
  };

  const patchPicture = (partial: Partial<EffectsPicture>) => {
    patchEffects({ ...draft, picture: { ...draft.picture, ...partial } });
  };

  const patchAudio = (next: EffectsAudioConfig, persist = true) => {
    audioRef.current = next;
    setAudioDraft(next);
    onPreviewAudio(next);
    if (persist) scheduleAudio();
  };

  const patchTransform = (next: ClipTransform) => {
    transformRef.current = next;
    setTransformDraft(next);
    onTransformPreview(next);
    scheduleTransformPersist();
  };

  const presets = useMemo<Record<string, EffectPreset>>(() => {
    const fromBackend = renderConfig?.presets;
    if (fromBackend && Object.keys(fromBackend).length) return fromBackend;
    return FALLBACK_PRESETS;
  }, [renderConfig?.presets]);

  const activePreset = nearestPreset(presets, draft.preset);
  const editedKeys = useMemo(
    () => handEditedKeys(activePreset, draft),
    [activePreset, draft],
  );

  const applyPreset = (id: EffectPresetName) => {
    const bundle = nearestPreset(presets, id);
    patchEffects({
      ...draft,
      preset: id,
      picture: { ...bundle.picture },
      transition: { ...draft.transition, ...bundle.transition },
      audio: { ...draft.audio, ...bundle.audio },
    });
  };

  const bgmTracks = audioDraft.audio_tracks.filter((track) => (track.kind || "bgm") === "bgm");
  const primaryBgm = bgmTracks[0] ?? null;
  const hasBgm = Boolean(primaryBgm);
  const bgmTooltip = hasBgm
    ? "BGM 音量 / 淡入淡出 / ducking 作用于该成片的 BGM 音轨；导出由 FFmpeg amix + sidechaincompress 生效。"
    : "该成片还没有 BGM 素材：请在「音频」标签上传 BGM 后再调整 BGM 音量、淡入淡出与 ducking。";

  const updateBgm = (partial: Partial<AudioTrack>) => {
    if (!primaryBgm) return;
    const key = primaryBgm.asset_id || primaryBgm.id;
    const nextTracks = audioDraft.audio_tracks.map((track) => (
      (track.asset_id || track.id) === key ? { ...track, ...partial } : track
    ));
    patchAudio({ ...audioDraft, audio_tracks: nextTracks });
  };

  const previewFilter = effectsPreviewFilter(draft.picture);

  return <div className="inspector-effects" data-testid="inspector-effects-tab">
    {/* §1 风格预设 */}
    <section className="inspector-block" aria-label="风格预设">
      <header><strong>风格预设</strong><SaveBadge state={save} /></header>
      <div className="inspector-preset-grid" data-testid="inspector-effect-presets">
        {EFFECT_PRESET_NAMES.map((name) => <button
          key={name}
          type="button"
          className={`inspector-preset${draft.preset === name ? " active" : ""}`}
          aria-pressed={draft.preset === name}
          aria-label={`风格预设 ${PRESET_LABELS[name]}`}
          title={`应用「${PRESET_LABELS[name]}」的一组画面调整 / 转场 / ducking 参数（选中即写入配置并即时预览）。`}
          onClick={() => applyPreset(name)}
        >{PRESET_LABELS[name]}</button>)}
      </div>
      <p className="inspector-hint" data-testid="inspector-active-preset">
        当前预设：{activePreset.label || PRESET_LABELS[draft.preset]}
        {editedKeys.length
          ? ` · 已手改项：${editedKeys.join("、")}`
          : " · 未手改（参数与预设一致）"}
      </p>
      <p className="inspector-hint" data-testid="inspector-effects-approximate-note">
        颜色类预览为近似（CSS filter），导出以 FFmpeg 为准；亮度/对比度/饱和度数值与导出参数同源。
      </p>
    </section>

    {/* §2 画面调整 */}
    <section className="inspector-block" aria-label="画面调整">
      <header><strong>画面调整</strong><SaveBadge state={save} /></header>
      <label className="inspector-field">
        <span>亮度</span>
        <input aria-label="亮度" type="range" min={-1} max={1} step={0.01}
          value={draft.picture.brightness}
          onChange={(event) => patchPicture({ brightness: Number(event.target.value) })} />
        <em>{draft.picture.brightness.toFixed(2)}</em>
      </label>
      <label className="inspector-field">
        <span>对比度</span>
        <input aria-label="对比度" type="range" min={0} max={3} step={0.01}
          value={draft.picture.contrast}
          onChange={(event) => patchPicture({ contrast: Number(event.target.value) })} />
        <em>{draft.picture.contrast.toFixed(2)}</em>
      </label>
      <label className="inspector-field">
        <span>饱和度</span>
        <input aria-label="饱和度" type="range" min={0} max={3} step={0.01}
          value={draft.picture.saturation}
          onChange={(event) => patchPicture({ saturation: Number(event.target.value) })} />
        <em>{draft.picture.saturation.toFixed(2)}</em>
      </label>
      <label className="inspector-field">
        <span>锐化</span>
        <input aria-label="锐化" type="range" min={0} max={1} step={0.01}
          value={draft.picture.sharpen}
          onChange={(event) => patchPicture({ sharpen: Number(event.target.value) })} />
        <em>{Math.round(draft.picture.sharpen * 100)}%</em>
      </label>
      <label className="inspector-field" title="暖色为正、冷色为负；导出由 FFmpeg colortemperature 精确生效。">
        <span>色温</span>
        <input aria-label="色温" type="range" min={-1} max={1} step={0.01}
          value={draft.picture.temperature}
          onChange={(event) => patchPicture({ temperature: Number(event.target.value) })} />
        <em>{draft.picture.temperature.toFixed(2)}</em>
      </label>
      <label className="inspector-field">
        <span>滤镜</span>
        <select aria-label="画面滤镜" value={draft.picture.filter}
          onChange={(event) => patchPicture({ filter: event.target.value as PictureFilterName })}>
          {PICTURE_FILTER_NAMES.map((name) => <option key={name} value={name}>{FILTER_LABELS[name]}</option>)}
        </select>
      </label>
      <p className="inspector-hint" data-testid="inspector-preview-filter">预览近似滤镜：{previewFilter}</p>
    </section>

    {/* §3 画幅与构图 */}
    <section className="inspector-block" aria-label="画幅与构图">
      <header><strong>画幅与构图</strong><SaveBadge state={save} /></header>
      <label className="inspector-field" title="画幅比例当前仅影响预览；导出画布恒为 9:16（1080×1920）。">
        <span>画幅比例</span>
        <select aria-label="画幅比例" value={draft.ratio.name}
          onChange={(event) => {
            const name = event.target.value as RatioName;
            patchEffects({ ...draft, ratio: { ...draft.ratio, name } });
            onViewerAspectChange(name);
          }}>
          {RATIO_NAMES.map((name) => <option key={name} value={name}>{RATIO_LABELS[name]}</option>)}
        </select>
      </label>
      <p className="inspector-hint" data-testid="inspector-ratio-preview-only">
        画幅比例仅影响预览；导出仍为 9:16（1080×1920）。{renderConfig?.effects_export?.ratio_note ? ` ${renderConfig.effects_export.ratio_note}` : ""}
      </p>
      <label className="inspector-field">
        <span>适配</span>
        <select aria-label="适配方式" value={transform?.fitMode ?? "cover"}
          onChange={(event) => transform && patchTransform({ ...transform, fitMode: event.target.value as ClipTransform["fitMode"] })}>
          <option value="cover">铺满画布（cover）</option>
          <option value="contain">完整显示（contain）</option>
        </select>
        <button type="button" className="inspector-mini" disabled={!transform || transformLocked}
          title="适合画布：铺满画布、缩放 100%、位置归零（复用既有 transform，导出同源）。"
          onClick={() => transform && patchTransform({ ...transform, fitMode: "cover", scale: 1, positionX: 0, positionY: 0 })}>
          适合画布
        </button>
      </label>
      <label className="inspector-field">
        <span>缩放</span>
        <input aria-label="片段缩放" type="range" min={0.1} max={3} step={0.01}
          disabled={!transform || transformLocked}
          value={transform?.scale ?? 1}
          onChange={(event) => transform && patchTransform({ ...transform, scale: Number(event.target.value) })} />
        <em>{((transform?.scale ?? 1) * 100).toFixed(0)}%</em>
      </label>
      <label className="inspector-field">
        <span>水平位置</span>
        <input aria-label="水平位置" type="range" min={-CANVAS_WIDTH} max={CANVAS_WIDTH} step={1}
          disabled={!transform || transformLocked}
          value={transform?.positionX ?? 0}
          onChange={(event) => transform && patchTransform({ ...transform, positionX: Number(event.target.value) })} />
        <em>{Math.round(transform?.positionX ?? 0)}</em>
      </label>
      <label className="inspector-field">
        <span>垂直位置</span>
        <input aria-label="垂直位置" type="range" min={-CANVAS_HEIGHT} max={CANVAS_HEIGHT} step={1}
          disabled={!transform || transformLocked}
          value={transform?.positionY ?? 0}
          onChange={(event) => transform && patchTransform({ ...transform, positionY: Number(event.target.value) })} />
        <em>{Math.round(transform?.positionY ?? 0)}</em>
      </label>
      {(["left", "top", "right", "bottom"] as const).map((edge) => <label className="inspector-field" key={edge}>
        <span>{`裁剪 ${{ left: "左", top: "上", right: "右", bottom: "下" }[edge]}（0..1）`}</span>
        <input aria-label={`裁剪 ${{ left: "左", top: "上", right: "右", bottom: "下" }[edge]}`}
          type="number" min={0} max={1} step={0.01}
          disabled={!transform || transformLocked}
          value={transform?.crop?.[edge] ?? (edge === "right" || edge === "bottom" ? 1 : 0)}
          onChange={(event) => transform && patchTransform({
            ...transform,
            crop: { ...transform.crop, [edge]: Number(event.target.value) },
          })} />
      </label>)}
      <p className="inspector-hint">裁剪 / 缩放 / 位置复用既有 transform 语义（预览与导出严格同源）。</p>
    </section>

    {/* §4 转场 */}
    <section className="inspector-block" aria-label="转场">
      <header><strong>转场</strong><SaveBadge state={save} /></header>
      <label className="inspector-field">
        <span>转场</span>
        <select aria-label="转场类型" value={draft.transition.enabled ? draft.transition.type : "none"}
          onChange={(event) => {
            const type = event.target.value as TransitionType;
            patchEffects({ ...draft, transition: { ...draft.transition, enabled: type !== "none", type } });
          }}>
          <option value="none">无</option>
          <option value="fade">淡入淡出</option>
        </select>
      </label>
      <label className="inspector-field">
        <span>转场时长</span>
        <input aria-label="转场时长" type="range" min={0.1} max={2} step={0.05}
          disabled={!draft.transition.enabled}
          value={draft.transition.duration_s}
          onChange={(event) => patchEffects({ ...draft, transition: { ...draft.transition, duration_s: Number(event.target.value) } })} />
        <em>{draft.transition.duration_s.toFixed(2)}s</em>
      </label>
      <p className="inspector-hint">转场导出为片段首尾 `fade=t=in/out`（由 FFmpeg 精确生效）。</p>
    </section>

    {/* §5 音频处理 */}
    <section className="inspector-block" aria-label="音频处理">
      <header><strong>音频处理</strong><SaveBadge state={save} /></header>
      <label className="inspector-field">
        <span>原声音量</span>
        <input aria-label="原声音量" type="range" min={0} max={1} step={0.01}
          value={audioDraft.voice_volume}
          onChange={(event) => patchAudio({ ...audioDraft, voice_volume: Number(event.target.value) })} />
        <em>{Math.round(audioDraft.voice_volume * 100)}%</em>
      </label>
      <label className="inspector-field" title={bgmTooltip}>
        <span>BGM 音量</span>
        <input aria-label="BGM 音量" type="range" min={0} max={1} step={0.01}
          disabled={!hasBgm}
          value={primaryBgm?.volume ?? 0}
          onChange={(event) => updateBgm({ volume: Number(event.target.value) })} />
        <em>{hasBgm ? `${Math.round((primaryBgm?.volume ?? 0) * 100)}%` : "无 BGM"}</em>
      </label>
      <label className="inspector-field" title={bgmTooltip}>
        <span>淡入</span>
        <input aria-label="音频淡入" type="range" min={0} max={5} step={0.1}
          disabled={!hasBgm}
          value={primaryBgm?.fade_in_s ?? 0}
          onChange={(event) => updateBgm({ fade_in_s: Number(event.target.value) })} />
        <em>{hasBgm ? `${(primaryBgm?.fade_in_s ?? 0).toFixed(1)}s` : "—"}</em>
      </label>
      <label className="inspector-field" title={bgmTooltip}>
        <span>淡出</span>
        <input aria-label="音频淡出" type="range" min={0} max={5} step={0.1}
          disabled={!hasBgm}
          value={primaryBgm?.fade_out_s ?? 0}
          onChange={(event) => updateBgm({ fade_out_s: Number(event.target.value) })} />
        <em>{hasBgm ? `${(primaryBgm?.fade_out_s ?? 0).toFixed(1)}s` : "—"}</em>
      </label>
      <label className="inspector-field" title={bgmTooltip}>
        <span>BGM 自动压低</span>
        <button
          type="button"
          role="switch"
          aria-checked={draft.audio.ducking}
          aria-label="BGM 自动压低开关"
          disabled={!hasBgm}
          className={`inspector-switch${draft.audio.ducking ? " on" : ""}`}
          onClick={() => patchEffects({ ...draft, audio: { ...draft.audio, ducking: !draft.audio.ducking } })}
        ><i /></button>
      </label>
      <p className="inspector-hint" data-testid="inspector-bgm-note">
        {hasBgm
          ? "ducking：人声出现时自动压低 BGM（FFmpeg sidechaincompress）。"
          : "无 BGM 素材：BGM 音量 / 淡入淡出 / ducking 已置灰；先到「音频」标签上传 BGM。"}
      </p>
    </section>

    {save.status === "saving" && <p className="inspector-hint"><LoaderCircle size={12} />正在保存效果配置…</p>}

    <RenderConfigSummary view={renderConfig} loading={renderConfigLoading} error={renderConfigError} />
  </div>;
}
