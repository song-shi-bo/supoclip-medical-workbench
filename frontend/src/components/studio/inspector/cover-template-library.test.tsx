/**
 * 封面版型库组件（2026-10-21 阶段）契约测试。
 *
 * 锁定验收四条：四张模板卡（示例图/名称/选中态）、只能选一个、
 * 未选切片只出提示（不能生成）、模拟按钮与结果展示。
 */
import { describe, expect, it, vi } from "vitest";
import { render, fireEvent, screen } from "@testing-library/react";
import { CoverTemplateLibraryPanel } from "./cover-template-library";
import { normalizeCoverTemplateLibrary } from "@/lib/cover-template-library";

const library = normalizeCoverTemplateLibrary({
  registry_version: "2026-10-21.1",
  templates: [
    { template_id: "cute", name: "可爱版", version: "1", example_path: "/studio/cover-templates/cute/example.svg", description: "柔和", sort: 1 },
    { template_id: "cool", name: "酷炫版", version: "1", example_path: "/studio/cover-templates/cool/example.svg", description: "霓虹", sort: 2 },
    { template_id: "anime_layout", name: "动漫排版", version: "1", example_path: "/studio/cover-templates/anime_layout/example.svg", description: "分格", sort: 3 },
    { template_id: "yellow_bold", name: "黄色大字", version: "1", example_path: "/studio/cover-templates/yellow_bold/example.svg", description: "压屏", sort: 4 },
  ],
});

const renderPanel = (overrides: Partial<Parameters<typeof CoverTemplateLibraryPanel>[0]> = {}) => render(
  <CoverTemplateLibraryPanel
    clipId="clip-a"
    library={library}
    loading={false}
    error={null}
    selectedTemplateId={null}
    generateBusyTemplateId={null}
    onSelectTemplate={vi.fn()}
    onGenerate={vi.fn()}
    {...overrides}
  />,
);

describe("四张模板卡显示正常", () => {
  it("按注册表渲染 4 张卡：示例图、名称、版本、注册表版本徽标", () => {
    renderPanel();
    expect(screen.getByTestId("cover-template-grid").children).toHaveLength(4);
    expect(screen.getByAltText("可爱版示例图")).toHaveAttribute("src", "/studio/cover-templates/cute/example.svg");
    expect(screen.getByAltText("黄色大字示例图")).toHaveAttribute("src", "/studio/cover-templates/yellow_bold/example.svg");
    expect(screen.getByText("可爱版")).toBeInTheDocument();
    expect(screen.getByText("动漫排版")).toBeInTheDocument();
    expect(screen.getByText("版型库 v2026-10-21.1")).toBeInTheDocument();
    expect(screen.getByRole("radiogroup", { name: "封面版型（单选）" })).toBeInTheDocument();
  });
  it("注册表载荷为空/加载中/失败各自有明确状态，失败可重试", () => {
    const first = renderPanel({ library: null, loading: true });
    expect(screen.getByTestId("cover-templates-loading")).toBeInTheDocument();
    first.unmount();
    const second = renderPanel({ library: null, error: "HTTP 500", onRetryLoad: vi.fn() });
    expect(screen.getByTestId("cover-templates-error")).toHaveTextContent("版型库读取失败：HTTP 500");
    expect(screen.getByTestId("cover-templates-retry")).toBeInTheDocument();
    second.unmount();
    renderPanel({ library: { registry_version: "v", templates: [] } });
    expect(screen.getByTestId("cover-templates-none")).toBeInTheDocument();
  });
});

describe("只能选择一个", () => {
  it("点击卡 A 选中 A；再点卡 B 后只有 B 处于选中态（radio 语义）", () => {
    const onSelect = vi.fn();
    const view = renderPanel({ onSelectTemplate: onSelect });
    const cards = (id: string) => screen.getByTestId(`cover-template-card-${id}`);
    expect(cards("cute").getAttribute("aria-checked")).toBe("false");
    fireEvent.click(cards("cute"));
    expect(onSelect).toHaveBeenLastCalledWith("cute");
    // 页面把选择写回台账后经 props 传回：仅一个 radio 为 checked
    view.rerender(<CoverTemplateLibraryPanel
      clipId="clip-a" library={library} loading={false} error={null}
      selectedTemplateId="cute" generateBusyTemplateId={null}
      onSelectTemplate={onSelect} onGenerate={vi.fn()} />);
    expect(cards("cute").getAttribute("aria-checked")).toBe("true");
    expect(screen.getByTestId("cover-template-selected-cute")).toBeInTheDocument();
    fireEvent.click(cards("cool"));
    expect(onSelect).toHaveBeenLastCalledWith("cool");
    view.rerender(<CoverTemplateLibraryPanel
      clipId="clip-a" library={library} loading={false} error={null}
      selectedTemplateId="cool" generateBusyTemplateId={null}
      onSelectTemplate={onSelect} onGenerate={vi.fn()} />);
    expect(cards("cool").getAttribute("aria-checked")).toBe("true");
    expect(screen.queryByTestId("cover-template-selected-cute")).toBeNull();
    expect(screen.getAllByRole("radio", { checked: true })).toHaveLength(1);
  });
});

describe("未选中切片不能生成", () => {
  it("clipId=null：只有提示，无卡片、无 radiogroup、无生成按钮", () => {
    renderPanel({ clipId: null });
    expect(screen.getByTestId("cover-templates-no-clip")).toHaveTextContent("先在左侧选中一条剪辑成片");
    expect(screen.queryByTestId("cover-template-grid")).toBeNull();
    expect(screen.queryByRole("radiogroup")).toBeNull();
    expect(screen.queryByTestId("cover-template-generate")).toBeNull();
  });
});

describe("选中后出现模拟生成按钮", () => {
  it("按钮文案=使用可爱版制作封面；点击触发 onGenerate；进行中置灰", () => {
    const onGenerate = vi.fn();
    const view = renderPanel({ selectedTemplateId: "cute", onGenerate });
    const button = screen.getByTestId("cover-template-generate");
    expect(button).toHaveTextContent("使用可爱版制作封面");
    fireEvent.click(button);
    expect(onGenerate).toHaveBeenCalledTimes(1);
    view.rerender(<CoverTemplateLibraryPanel
      clipId="clip-a" library={library} loading={false} error={null}
      selectedTemplateId="cute" generateBusyTemplateId="cute"
      onSelectTemplate={vi.fn()} onGenerate={onGenerate} />);
    expect(screen.getByTestId("cover-template-generate")).toBeDisabled();
  });
});