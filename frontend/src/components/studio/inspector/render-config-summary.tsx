"use client";

// 「导出前 render_config 摘要」卡片（契约 §5 / §6）。
//
// 数据源是 GET /studio/clips/{p}/{c}/render-config：每一项都带**来源**
// （user → 用户修改 / project → 项目设置 / skill_default → skill 默认），
// 并且明确标注导出侧（ASS 烧录 / BGM 混音 / 滤镜 / 转场 / 片尾卡 / 非 9:16）属 Phase 2。

import type { ConfigSource, RenderConfigView } from "./types";

export function sourceLabel(source: ConfigSource | string | undefined): string {
  if (source === "user") return "用户修改";
  if (source === "project") return "项目设置";
  if (source === "skill_default") return "skill 默认";
  return "未知来源";
}

export function formatConfigValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "开" : "关";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.length ? `共 ${value.length} 项` : "无";
  return JSON.stringify(value);
}

export function RenderConfigSummary({ view, loading, error }: { view: RenderConfigView | null; loading?: boolean; error?: string | null }) {
  return <section className="inspector-render-config" aria-label="导出前 render_config 摘要" data-testid="inspector-render-config">
    <header>
      <strong>导出前 render_config 摘要</strong>
      {/* 该徽标只指「导出分辨率」：非 9:16 仍待流水线支持；画面/转场/音频已由 FFmpeg 生效。 */}
      <span className="inspector-phase2" data-testid="inspector-phase2-resolution" title="导出分辨率切换仍待流水线支持；画面调整 / 转场 / 音频已由 FFmpeg 在导出通道生效。">分辨率 Phase 2</span>
    </header>
    {loading && <p className="inspector-hint">正在读取 render_config…</p>}
    {error && <p className="inspector-error" role="alert">{error}</p>}
    {!loading && !error && !view && <p className="inspector-hint">尚无导出摘要（后端 /render-config 未返回数据）。</p>}
    {view && <>
      <ul className="inspector-render-config-list">
        {(view.effective || []).map((entry) => <li key={entry.key}>
          <code>{entry.key}</code>
          <b>{formatConfigValue(entry.value)}</b>
          <em className={entry.source} data-testid={`inspector-source-${entry.key}`}>{sourceLabel(entry.source)}</em>
        </li>)}
        {!(view.effective || []).length && <li className="empty">后端未返回任何生效项。</li>}
      </ul>
      <p className="inspector-hint" data-testid="inspector-render-config-export">
        导出分辨率：{view.export?.resolution || "未知"}
        {view.export?.deferred ? " · 分辨率切换待流水线支持" : ""}
      </p>
      {view.export?.note && <p className="inspector-hint phase2" data-testid="inspector-render-config-note">{view.export.note}</p>}
    </>}
  </section>;
}
