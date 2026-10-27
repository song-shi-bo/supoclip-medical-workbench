"use client";

/**
 * 封面版型库（2026-10-21 阶段）：右侧「封面」页的版型卡网格 + 选择 + 模拟生成。
 *
 * 契约：
 * - 版型集合来自 props（页面从版本化注册表 GET /studio/cover-templates 拉取），
 *   本组件**绝不硬编码**版型——注册表加版型/改序，这里零改动；
 * - 单选：radiogroup + radio 语义（aria-checked），一次只能选中一个版型；
 * - 未选中切片：只显示提示，不出卡片、不出「制作封面」按钮（不选切片不能生成）；
 * - 「使用××版制作封面」在选中版型后出现，接**模拟结果**（本阶段不调用模型、
 *   不生成真实封面）：点击 → onMockGenerate，结果由页面按 clip 隔离后经
 *   mockResult 传入展示；
 * - 选中态事实源是台账卡片（cover_template 三元组），由页面透传 selectedTemplateId：
 *   切换切片自动跟随、刷新自动恢复，本组件不持有选择状态。
 */

import { LoaderCircle, Sparkles } from "lucide-react";

import {
  coverTemplateButtonLabel,
  findCoverTemplate,
  type CoverTemplate,
  type CoverTemplateLibrary,
} from "@/lib/cover-template-library";

export type CoverTemplateLibraryProps = {
  /** null = 未选中切片（或绑定在原片视图）：显示提示，不出卡片与生成按钮。 */
  clipId: string | null;
  /** 示例图 URL 前缀（页面的 API base；注册表 example_path 为 API 相对路径）。 */
  exampleUrlBase?: string;
  library: CoverTemplateLibrary | null;
  loading: boolean;
  error: string | null;
  /** 当前切片已保存的选择（台账事实源，页面从 activeCard 推导）。 */
  selectedTemplateId: string | null;
  /** 生成进行中的 template_id（null = 空闲）。 */
  generateBusyTemplateId: string | null;
  onSelectTemplate: (templateId: string) => void;
  onGenerate: () => void;
  onRetryLoad?: () => void;
};

export function CoverTemplateLibraryPanel(props: CoverTemplateLibraryProps) {
  const {
    clipId, library, loading, error, selectedTemplateId,
    generateBusyTemplateId, onSelectTemplate, onGenerate, onRetryLoad,
  } = props;
  const exampleUrl = (relative: string | null | undefined) => `${props.exampleUrlBase ?? ""}${relative ?? ""}`;

  return <section className="inspector-block" aria-label="封面版型" data-testid="cover-template-library">
    <header>
      <strong>封面版型</strong>
      {library?.registry_version && <span className="inspector-metadata">版型库 v{library.registry_version}</span>}
    </header>

    {/* 未选中切片：提示；不出卡片、不出生成按钮（不选切片不能生成）。 */}
    {!clipId && <p className="inspector-hint" data-testid="cover-templates-no-clip">
      先在左侧选中一条剪辑成片，再挑选封面版型。
    </p>}

    {clipId && loading && <p className="inspector-hint" data-testid="cover-templates-loading"><LoaderCircle size={12} /> 正在读取版型库…</p>}
    {clipId && !loading && error && <p className="inspector-error" role="alert" data-testid="cover-templates-error">
      版型库读取失败：{error}
      {onRetryLoad && <button type="button" className="inspector-link" onClick={onRetryLoad} data-testid="cover-templates-retry">重试</button>}
    </p>}
    {clipId && !loading && !error && library && library.templates.length === 0 && <p className="inspector-hint" data-testid="cover-templates-none">注册表暂无可用版型。</p>}

    {clipId && !loading && !error && library && library.templates.length > 0 && <div
      className="cover-template-grid"
      role="radiogroup"
      aria-label="封面版型（单选）"
      data-testid="cover-template-grid">
      {library.templates.map((template: CoverTemplate) => {
        const selected = selectedTemplateId === template.template_id;
        const busy = generateBusyTemplateId === template.template_id;
        return <button type="button" key={template.template_id}
          role="radio"
          aria-checked={selected}
          data-testid={`cover-template-card-${template.template_id}`}
          className={`cover-template-card${selected ? " selected" : ""}`}
          title={`${template.name}（v${template.version}）：${template.description}`}
          onClick={() => onSelectTemplate(template.template_id)}>
          <span className="cover-template-frame">
            <img src={exampleUrl(template.example_path)} alt={`${template.name}示例图`} />
          </span>
          <span className="cover-template-meta">
            <b>{template.name}</b>
            <small>v{template.version}</small>
          </span>
          {selected && <em className="cover-template-selected-badge" data-testid={`cover-template-selected-${template.template_id}`}>已选</em>}
          {busy && <LoaderCircle size={12} className="cover-template-busy" aria-label="正在生成模拟结果" />}
        </button>;
      })}
    </div>}

    {/* 选中版型后出现「使用××版制作封面」（真实渲染：版面契约来自版本化注册表）。 */}
    {clipId && !loading && !error && selectedTemplateId && (() => {
      const template = findCoverTemplate(library, selectedTemplateId);
      if (!template) return null;
      const busy = generateBusyTemplateId === template.template_id;
      return <div className="inspector-row inspector-row-actions" data-testid="cover-template-generate-row">
        <button type="button" className="inspector-primary" disabled={busy}
          data-testid="cover-template-generate"
          title={`按「${template.name}」版型重制封面：切片真实帧 + 版面契约排版，不调用模型。`}
          onClick={onGenerate}>
          {busy ? <LoaderCircle size={12} /> : <Sparkles size={12} />}{coverTemplateButtonLabel(template)}
        </button>
      </div>;
    })()}

  </section>;
}
