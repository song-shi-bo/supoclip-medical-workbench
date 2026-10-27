/**
 * 封面版型库纯逻辑（2026-10-21 阶段）回归契约。
 *
 * 锁定：注册表载荷归一化（半份数据绝不渲染）、选择态从台账卡片推导、
 * 生成按钮文案、模拟结果按 clip 隔离。
 */
import { describe, expect, it } from "vitest";
import {
  coverTemplateButtonLabel,
  findCoverTemplate,
  normalizeCoverTemplateLibrary,
  resultForClip,
  selectedCoverTemplateId,
} from "./cover-template-library";

const registryPayload = {
  registry_version: "2026-10-21.1",
  templates: [
    { template_id: "yellow_bold", name: "黄色大字", version: "1", example_path: "/studio/cover-templates/yellow_bold/example.svg", description: "d", sort: 4 },
    { template_id: "cute", name: "可爱版", version: "1", example_path: "/studio/cover-templates/cute/example.svg", description: "d", sort: 1 },
    { template_id: "cool", name: "酷炫版", version: "1", example_path: "/studio/cover-templates/cool/example.svg", description: "d", sort: 2 },
    { template_id: "anime_layout", name: "动漫排版", version: "1", example_path: "/studio/cover-templates/anime_layout/example.svg", description: "d", sort: 3 },
  ],
};

describe("normalizeCoverTemplateLibrary", () => {
  it("正常载荷归一化并按 sort 稳定升序（后端顺序打乱也不影响展示序）", () => {
    const library = normalizeCoverTemplateLibrary(registryPayload);
    expect(library?.registry_version).toBe("2026-10-21.1");
    expect(library?.templates.map((t) => t.template_id)).toEqual(
      ["cute", "cool", "anime_layout", "yellow_bold"]);
  });
  it.each([
    ["null", null],
    ["非对象", "gateway-404-html"],
    ["缺 registry_version", { templates: [] }],
    ["templates 非数组", { registry_version: "v", templates: {} }],
    ["条目缺字段", { registry_version: "v", templates: [{ template_id: "cute" }] }],
  ])("非法载荷(%s)→null：版型卡绝不拿半份数据渲染", (_name, payload) => {
    expect(normalizeCoverTemplateLibrary(payload)).toBeNull();
  });
});

describe("选择态与文案", () => {
  const library = normalizeCoverTemplateLibrary(registryPayload);
  it("findCoverTemplate：命中/未命中/空库", () => {
    expect(findCoverTemplate(library, "cute")?.name).toBe("可爱版");
    expect(findCoverTemplate(library, "kawaii")).toBeNull();
    expect(findCoverTemplate(null, "cute")).toBeNull();
  });
  it("selectedCoverTemplateId：从台账卡片三元组推导，缺省/损坏一律视为未选择", () => {
    expect(selectedCoverTemplateId({ clip_id: "c1", cover_template: { template_id: "cool" } })).toBe("cool");
    expect(selectedCoverTemplateId({ clip_id: "c1", cover_template: { template_id: "" } })).toBeNull();
    expect(selectedCoverTemplateId({ clip_id: "c1", cover_template: null })).toBeNull();
    expect(selectedCoverTemplateId({ clip_id: "c1" })).toBeNull();
    expect(selectedCoverTemplateId(null)).toBeNull();
  });
  it("生成按钮文案与后端 template_name 同源", () => {
    expect(coverTemplateButtonLabel(findCoverTemplate(library, "cute"))).toBe("使用可爱版制作封面");
    expect(coverTemplateButtonLabel(null)).toBe("使用该版制作封面");
  });
});

describe("resultForClip（生成结果按 clip 隔离）", () => {
  const result = { clip_id: "clip-b", template_id: "cool", template_name: "酷炫版", example_path: "/studio/cover-templates/cool/example.svg", result_text: "已按版型重制封面", registry_version: "v" };
  it("当前切片命中则透出", () => {
    expect(resultForClip(result, "clip-b")?.template_id).toBe("cool");
  });
  it("切到别的切片：结果不跟随（不串片）", () => {
    expect(resultForClip(result, "clip-a")).toBeNull();
    expect(resultForClip(result, null)).toBeNull();
    expect(resultForClip(null, "clip-b")).toBeNull();
  });
});
