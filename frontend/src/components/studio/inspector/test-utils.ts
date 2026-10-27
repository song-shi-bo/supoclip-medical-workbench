// Inspector 测试共用桩：msw 默认 handler（按合同路径/形状）+ 测试数据工厂。
//
// 这里只 mock 契约 §5 的端点，不 mock 任何组件内部实现：
// 用例断言的是「真的发了哪个请求、body 里是什么」。

import { http, HttpResponse } from "msw";

import {
  DEFAULT_EFFECTS,
  DEFAULT_SUBTITLE_CONFIG,
  type AudioTrack,
  type ClipConfigView,
  type Effects,
  type SubtitleConfig,
  type SubtitleCue,
  type SubtitleView,
  type TranscriptDocument,
} from "@/components/studio/inspector/types";

export const PROJECT_ID = "timeline_p1";
export const CLIP_ID = "clip-aaaa-01";

export function clipUrl(suffix: string, clipId: string = CLIP_ID, projectId: string = PROJECT_ID): string {
  return `*/studio/clips/${projectId}/${clipId}/${suffix}`;
}

export function configView(overrides: Partial<ClipConfigView> = {}): ClipConfigView {
  const config = {
    clip_id: CLIP_ID,
    title: "高血压用药三件事",
    transcript_revision: 1,
    subtitle_config: DEFAULT_SUBTITLE_CONFIG,
    subtitle_overrides: [],
    cover_asset_id: "cover-asset-7",
    cover_revision: 3,
    // 与 lib/clip-transform.ts 的 transformPayload() 同形（后端 clip 记录的 transform）。
    transform: {
      scale: 1, positionX: 0, positionY: 0,
      crop: { left: 0, top: 0, right: 1, bottom: 1 },
      fitMode: "cover", rotation: 0,
    },
    audio_tracks: [] as AudioTrack[],
    voice_volume: 1,
    muted: false,
    effects: DEFAULT_EFFECTS,
    render_revision: 4,
    task_history: [],
  };
  return {
    project_id: PROJECT_ID,
    clip_id: CLIP_ID,
    binding: "clip",
    config: { ...config, ...(overrides.config || {}) },
    revisions: { transcript_revision: 1, render_revision: 4 },
    sources: { subtitle_config: "skill_default", effects: "user" },
    ...overrides,
  };
}

export function transcriptDoc(overrides: Partial<TranscriptDocument> = {}): TranscriptDocument {
  return {
    project_id: PROJECT_ID,
    clip_id: CLIP_ID,
    transcript_revision: 1,
    approved_revision: 1,
    raw_text: "血压高要吃三种药。",
    raw_segments: [
      { segment_id: "seg-1", start: 0, end: 2, text: "血压高要吃三种药。", speaker: "医生" },
      { segment_id: "seg-2", start: 2, end: 4, text: "血压高要注意饮食。", speaker: "未标注" },
    ],
    corrected_segments: [
      { segment_id: "seg-1", start: 0, end: 2, text: "血压高要吃三种药。", speaker: "医生" },
      { segment_id: "seg-2", start: 2, end: 4, text: "血压高要注意饮食。", speaker: "未标注" },
    ],
    effective_segments: [
      { segment_id: "seg-1", start: 0, end: 2, text: "血压高要吃三种药。", speaker: "医生" },
      { segment_id: "seg-2", start: 2, end: 4, text: "血压高要注意饮食。", speaker: "未标注" },
    ],
    revisions: [{
      revision: 1, author: "editor", created_at: "2026-01-01T00:00:00Z", reason: "人工审校",
      changes: [{ segment_id: "seg-1", before: "血压高要吃三种药", after: "血压高要吃三种药。" }],
    }],
    ...overrides,
  };
}

export function subtitleCues(): SubtitleCue[] {
  return [
    { segment_id: "seg-1", start: 0, end: 2, text: "血压高要吃三种药。", speaker: "医生" },
    { segment_id: "seg-2", start: 2, end: 4, text: "血压高要注意饮食。", speaker: "未标注" },
  ];
}

export function subtitleView(overrides: Partial<SubtitleView> = {}): SubtitleView {
  return {
    project_id: PROJECT_ID,
    clip_id: CLIP_ID,
    config: { ...DEFAULT_SUBTITLE_CONFIG, ...(overrides.config || {}) },
    cues: subtitleCues(),
    overrides: [],
    revisions: [
      { revision: 1, cue_count: 2, created_at: "2026-01-01T00:00:00Z", ass_path: "/tmp/rev-1/clip.ass", srt_path: "/tmp/rev-1/clip.srt" },
    ],
    latest_revision: 1,
    export_burn_in: "phase2",
    note: "字幕版本用于预览与版本追溯；烧录进导出成片属 Phase 2",
    ...overrides,
  };
}

export function bgmTrack(overrides: Partial<AudioTrack> = {}): AudioTrack {
  return {
    id: "bgm-1",
    kind: "bgm",
    asset_id: "bgm-deadbeef",
    path: "/tmp/audio/bgm-deadbeef.mp3",
    filename: "bgm.mp3",
    volume: 0.8,
    fade_in_s: 1,
    fade_out_s: 2,
    duration_s: 30,
    peaks: [0, 0.2, 0.8, 0.4, 0.1],
    sha256: "deadbeef",
    bytes: 2048,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

export function renderConfigView(effects: Partial<Effects> = {}, subtitle: Partial<SubtitleConfig> = {}) {
  const mergedEffects: Effects = { ...DEFAULT_EFFECTS, ...effects };
  const mergedSubtitle: SubtitleConfig = { ...DEFAULT_SUBTITLE_CONFIG, ...subtitle };
  return {
    project_id: PROJECT_ID,
    clip_id: CLIP_ID,
    binding: "clip" as const,
    effective: [
      { key: "subtitle_config.font_size_px", value: mergedSubtitle.font_size_px, source: "user" as const },
      { key: "effects.picture.filter", value: mergedEffects.picture.filter, source: "skill_default" as const },
      { key: "effects.export_resolution", value: mergedEffects.export_resolution, source: "project" as const },
    ],
    sources: { subtitle_config: "user" as const, effects: "skill_default" as const },
    export: {
      resolution: mergedEffects.export_resolution,
      deferred: true,
      // 与后端 render-config 摘要同文：画面/转场/音频已接入「按变换重渲」导出通道。
      note: "画面调整 / 转场 / 音频在「导出成片（按变换重渲）」通道由 FFmpeg 精确生效；颜色类预览为 CSS filter 近似，几何类预览与导出严格同源；画幅比例当前仅影响预览",
    },
    config: configView().config,
  };
}

/** 契约 §5 的每个路由各自一个 handler 工厂（组合用，避免同一路由重复注册）。 */
export const inspectorRoutes = {
  configGet: () => http.get(clipUrl("config"), () => HttpResponse.json(configView())),
  configPut: () => http.put(clipUrl("config"), async ({ request }) => {
    const patch = JSON.parse(await request.text()) as Record<string, unknown>;
    return HttpResponse.json({
      changed: Object.keys(patch),
      config: { ...configView().config, ...patch },
      revisions: { transcript_revision: 1, render_revision: 5 },
      task: { task_id: "msub_0123456789ab", task_type: "UPDATE_SUBTITLE" },
      render_deferred: true,
      render_note: "配置与版本已保存；按配置真渲染属 Phase 2",
    });
  }),
  transcriptGet: () => http.get(clipUrl("transcript/revisions"), () => HttpResponse.json(transcriptDoc())),
  transcriptPost: () => http.post(clipUrl("transcript/revisions"), async () => HttpResponse.json(transcriptDoc({
    transcript_revision: 2,
    approved_revision: 2,
    revisions: [
      ...transcriptDoc().revisions,
      { revision: 2, author: "editor", created_at: "2026-01-02T00:00:00Z", reason: "手动纠错", changes: [] },
    ],
  }))),
  transcriptRollback: () => http.post(clipUrl("transcript/rollback"), async () => HttpResponse.json(transcriptDoc({ approved_revision: 1 }))),
  subtitlesGet: () => http.get(clipUrl("subtitles"), () => HttpResponse.json(subtitleView())),
  subtitlesPost: () => http.post(clipUrl("subtitles"), async ({ request }) => {
    const body = JSON.parse(await request.text()) as Record<string, unknown>;
    return HttpResponse.json({
      revision: 2,
      cue_count: Array.isArray(body.cues) ? (body.cues as unknown[]).length : 2,
      ass_path: "/tmp/rev-2/clip.ass",
      srt_path: "/tmp/rev-2/clip.srt",
      export_burn_in: "phase2",
      note: "字幕版本用于预览与版本追溯；烧录进导出成片属 Phase 2",
      latest_revision: 2,
    });
  }),
  bgmPost: () => http.post(clipUrl("audio/bgm"), () => HttpResponse.json({
    clip_id: CLIP_ID, track: bgmTrack(), audio_tracks: [bgmTrack()], peaks: bgmTrack().peaks,
  })),
  bgmDelete: () => http.delete(clipUrl("audio/bgm/:assetId"), ({ params }) => HttpResponse.json({ removed: true, asset_id: String(params.assetId) })),
  renderConfigGet: () => http.get(clipUrl("render-config"), () => HttpResponse.json(renderConfigView())),
} as const;

export type InspectorRouteName = keyof typeof inspectorRoutes;

/**
 * 默认 handlers：所有 GET 成功、所有写方法返回可用的最小响应。
 * 需要断言请求体或制造失败的用例用 ``server.use(自定义handler)`` 覆盖（prepend，
 * 只有自定义那一个会跑）。
 */
export function inspectorHandlers() {
  return [
    inspectorRoutes.configGet(),
    inspectorRoutes.configPut(),
    inspectorRoutes.transcriptGet(),
    inspectorRoutes.transcriptPost(),
    inspectorRoutes.transcriptRollback(),
    inspectorRoutes.subtitlesGet(),
    inspectorRoutes.subtitlesPost(),
    inspectorRoutes.bgmPost(),
    inspectorRoutes.bgmDelete(),
    inspectorRoutes.renderConfigGet(),
  ];
}
