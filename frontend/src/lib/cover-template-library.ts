/**
 * 封面版型库（2026-10-21 阶段）纯逻辑：注册表载荷归一化 + 选择态推导。
 *
 * 事实源约定：
 * - **版型集合**只来自后端版本化注册表（GET /studio/cover-templates，
 *   见 backend/src/cover_templates.py）——前端绝不硬编码四种版型；
 * - **每条成片的选择**存台账 clips.{clip_id}.cover_template 三元组，随卡片载荷
 *   透出（card_view.cover_template）——切换切片自动跟随、刷新自动恢复；
 * - 模拟生成（本阶段不调用模型）走 POST /studio/cover-templates/{id}/mock-result。
 *
 * 本模块不依赖 React；回归测试见 cover-template-library.test.ts。
 */

export type CoverTemplate = {
  template_id: string;
  name: string;
  version: string;
  example_path: string;
  description: string;
  sort: number;
};

export type CoverTemplateLibrary = {
  registry_version: string;
  templates: CoverTemplate[];
};

/** 台账卡片里本模块需要的字段（page.tsx 的 ProjectClipCard 结构兼容）。 */
export type CoverTemplateCardLike = {
  clip_id: string;
  cover_template?: { template_id?: string | null; registry_version?: string | null; updated_at?: string | null } | null;
};

/** GET /studio/cover-templates 载荷形状（后端 cover_templates.list_templates）。 */
export type CoverTemplatesPayload = {
  registry_version?: string;
  templates?: unknown;
  detail?: unknown;
};

const isTemplate = (value: unknown): value is CoverTemplate => {
  if (!value || typeof value !== "object") return false;
  const t = value as Record<string, unknown>;
  return typeof t.template_id === "string" && t.template_id.length > 0
    && typeof t.name === "string" && t.name.length > 0
    && typeof t.example_path === "string" && t.example_path.length > 0
    && typeof t.version === "string"
    && typeof t.description === "string"
    && typeof t.sort === "number" && Number.isFinite(t.sort);
};

/**
 * 注册表载荷归一化：形状非法（网关 404 HTML、字段缺失）→ null（调用方显示
 * 「版型库不可用」，绝不拿半份数据渲染卡片）；正常则按 sort 稳定升序。
 */
export function normalizeCoverTemplateLibrary(payload: unknown): CoverTemplateLibrary | null {
  if (!payload || typeof payload !== "object") return null;
  const raw = payload as { registry_version?: unknown; templates?: unknown };
  if (typeof raw.registry_version !== "string" || !raw.registry_version) return null;
  if (!Array.isArray(raw.templates) || !raw.templates.every(isTemplate)) return null;
  const templates = [...raw.templates].sort((a, b) => a.sort - b.sort);
  return { registry_version: raw.registry_version, templates };
}

export function findCoverTemplate(
  library: CoverTemplateLibrary | null | undefined, templateId: string | null | undefined,
): CoverTemplate | null {
  const id = String(templateId ?? "").trim();
  if (!id || !library) return null;
  return library.templates.find((t) => t.template_id === id) ?? null;
}

/** 「使用××版制作封面」按钮文案（注册表 name 本身以「版」结尾，如「可爱版」）。 */
export function coverTemplateButtonLabel(template: CoverTemplate | null | undefined): string {
  return `使用${template?.name || "该版"}制作封面`;
}

/** 卡片 → 选中版型 id（cover_template 缺失/形状损坏一律视为未选择）。 */
export function selectedCoverTemplateId(card: CoverTemplateCardLike | null | undefined): string | null {
  const value = card?.cover_template;
  if (!value || typeof value !== "object") return null;
  const id = String(value.template_id ?? "").trim();
  return id || null;
}

/** 台账 cover_template 三元组的本地乐观更新形状。 */
export function coverTemplateSelection(templateId: string, registryVersion: string) {
  return { template_id: templateId, registry_version: registryVersion, updated_at: new Date().toISOString() };
}

/**
 * 「使用××版制作封面」结果的会话内持有形状：**键在 clip_id**——展示方必须只渲染
 * `result.clip_id === 当前clip` 的结果，切换切片即隔离（不串片）。
 *
 * 2026-10-21 路线 A 起为**真实渲染**结果（REGENERATE 风格封面局部通道，
 * 版面契约来自版本化注册表；无模型参与）：title/result_text 由页面按响应组装。
 */
export type CoverTemplateResult = {
  clip_id: string;
  template_id: string;
  template_name: string;
  example_path: string;
  /** 结果框标题（如「封面已更新」；缺省回落「生成结果」）。 */
  title?: string;
  result_text: string;
  registry_version: string;
  /** 真实新封面的可访问 URL（有则优先于 example_path 展示）。 */
  cover_url?: string;
  /** 新封面版本号（真实通道带回）。 */
  cover_revision?: number;
};

export function resultForClip(
  result: CoverTemplateResult | null | undefined, clipId: string | null | undefined,
): CoverTemplateResult | null {
  if (!result || !clipId || result.clip_id !== clipId) return null;
  return result;
}
