// 封面与画面标签页测试（契约 §6）：
// 封面按钮各自触发正确的页面回调（重新制作封面只走 REGENERATE_COVER 通道）、
// cover_asset_id / 版本显示、画面比例菜单只改预览、变换「应用」触发回调（取消不触发）。

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { defaultTransform } from "@/lib/clip-transform";

import { CoverPictureTab } from "./cover-picture-tab";

function renderTab(overrides: Partial<Parameters<typeof CoverPictureTab>[0]> = {}) {
  const handlers = {
    onRegenerateCover: vi.fn(),
    onRegenerateWithInstruction: vi.fn(),
    onRetryCoverTask: vi.fn(),
    onActivateRevision: vi.fn(),
    onDeleteRevision: vi.fn(),
    onPreviewInPlayer: vi.fn(),
    onEditTitle: vi.fn(),
    onShowCoverHistory: vi.fn(),
    onAdoptCover: vi.fn(),
    onUndoCover: vi.fn(),
    onAdoptSourceChange: vi.fn(),
    onTransformChange: vi.fn(),
    onBackgroundChange: vi.fn(),
    onRatioLockChange: vi.fn(),
    onViewerZoomChange: vi.fn(),
    onFitView: vi.fn(),
    onOriginalRatio: vi.fn(),
    onCanvasMode: vi.fn(),
    onResetTransform: vi.fn(),
    onViewerAspectChange: vi.fn(),
    onApply: vi.fn(),
    onCancel: vi.fn(),
  };
  const props: Parameters<typeof CoverPictureTab>[0] = {
    projectId: "timeline_p1",
    clipId: "clip-aaaa-01",
    boundToActiveClip: true,
    clipName: "剪辑成片 01",
    coverImageUrl: "/video-api/studio/auto-edit/t1/artifact?path=cover.jpg&v=3",
    coverAssetId: "cover-asset-7",
    coverRevision: 3,
    coverVersionLabel: "v3",
    coverStatusText: "按风格生成（第二版·真实帧）",
    title: "高血压用药三件事",
    busy: false,
    canUndoCover: true,
    undoRevision: 2,
    coverHistory: [{ revision: 3, created_at: "2026-01-01T00:00:00Z", kind: "generated" }],
    coverHistoryVisible: false,
    coverHistoryLoading: false,
    adoptOptions: [{ clip_id: "clip-aaaa-01", name: "剪辑成片 01" }],
    adoptSource: "clip-aaaa-01",
    transform: defaultTransform(),
    transformLocked: false,
    background: "blur",
    ratioLock: false,
    viewerZoom: 100,
    viewerAspect: "9:16",
    dirty: false,
    ...handlers,
    ...overrides,
  };
  const rendered = render(<CoverPictureTab {...props} />);
  return { handlers, props, ...rendered };
}

describe("CoverPictureTab", () => {
  it("显示当前封面预览、版本与 cover_asset_id", () => {
    renderTab();
    const cover = screen.getByTestId("inspector-cover-active");
    expect(cover.querySelector("img")?.getAttribute("src")).toContain("cover.jpg");
    expect(screen.getByTestId("inspector-cover-active-badge").textContent).toContain("当前使用中");
    expect(screen.getByTestId("inspector-cover-asset").textContent).toContain("cover-asset-7");
    expect(screen.getByTestId("inspector-cover-revision").textContent).toContain("v3");
  });

  it("「重新制作封面」只触发页面的 REGENERATE_COVER 通道回调（不触发自动剪辑/重渲）", () => {
    const { handlers } = renderTab();
    const input = screen.getByTestId("inspector-cover-instruction");
    fireEvent.change(input, { target: { value: "主题更短" } });
    fireEvent.click(screen.getByTestId("inspector-cover-regenerate"));
    expect(handlers.onRegenerateWithInstruction).toHaveBeenCalledTimes(1);
    expect(handlers.onRegenerateWithInstruction).toHaveBeenCalledWith("主题更短");
    expect(handlers.onRegenerateCover).not.toHaveBeenCalled();
    // 面板只暴露一个「重新制作封面」按钮，不存在 AUTO_EDIT / 重渲入口。
    expect(screen.queryByRole("button", { name: /自动剪辑/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /重新生成全部成片/ })).not.toBeInTheDocument();
  });

  it("「编辑标题」「查看历史版本」「设为当前封面」「撤销」各自触发对应回调", () => {
    const { handlers } = renderTab();
    fireEvent.click(screen.getByTestId("inspector-cover-edit-title"));
    fireEvent.click(screen.getByTestId("inspector-cover-history"));
    fireEvent.click(screen.getByTestId("inspector-cover-adopt"));
    fireEvent.click(screen.getByTestId("inspector-cover-undo"));
    expect(handlers.onEditTitle).toHaveBeenCalledTimes(1);
    expect(handlers.onShowCoverHistory).toHaveBeenCalledTimes(1);
    expect(handlers.onAdoptCover).toHaveBeenCalledWith("clip-aaaa-01");
    expect(handlers.onUndoCover).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("inspector-cover-undo").textContent).toContain("v2");
  });

  it("未绑定到当前成片时封面按钮全部禁用（不误操作别的 clip）", () => {
    renderTab({ boundToActiveClip: false });
    expect(screen.getByTestId("inspector-cover-regenerate")).toBeDisabled();
    expect(screen.getByTestId("inspector-cover-adopt")).toBeDisabled();
  });

  it("比例菜单只改预览：1:1 / 16:9 只回调 viewerAspect，不改 transform；原始比例才动 fitMode", () => {
    const { handlers } = renderTab();
    fireEvent.click(screen.getByTestId("inspector-aspect-1-1"));
    expect(handlers.onViewerAspectChange).toHaveBeenCalledWith("1:1");
    expect(handlers.onTransformChange).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("inspector-aspect-16-9"));
    expect(handlers.onViewerAspectChange).toHaveBeenLastCalledWith("16:9");
    expect(handlers.onTransformChange).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("inspector-aspect-source"));
    expect(handlers.onOriginalRatio).toHaveBeenCalledTimes(1);
    expect(handlers.onViewerAspectChange).toHaveBeenLastCalledWith("source");

    fireEvent.click(screen.getByTestId("inspector-aspect-canvas"));
    expect(handlers.onCanvasMode).toHaveBeenCalledTimes(1);
  });

  it("比例菜单的 tooltip 明确写着「导出仍按流水线 9:16（Phase 2 支持其他比例）」", () => {
    renderTab();
    expect(screen.getByTestId("inspector-aspect-tooltip").getAttribute("title")).toBe("导出仍按流水线 9:16（Phase 2 支持其他比例）");
    expect(screen.getByTestId("inspector-aspect-1-1").getAttribute("title")).toBe("导出仍按流水线 9:16（Phase 2 支持其他比例）");
  });

  it("变换面板：「应用」触发 onApply（并由页面写 PUT config），「取消」只回调 onCancel", () => {
    const { handlers } = renderTab({ dirty: true });
    fireEvent.click(screen.getByRole("button", { name: "应用" }));
    expect(handlers.onApply).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(handlers.onCancel).toHaveBeenCalledTimes(1);
  });

  it("旋转在本版本是只读的 0°（ClipTransform.rotation 契约恒为 0）", () => {
    renderTab();
    expect(screen.getByTestId("inspector-rotation")).toHaveValue("0°");
  });

  it("封面历史可见时列出 v{n} / kind / 时间", () => {
    renderTab({ coverHistoryVisible: true });
    const list = screen.getByTestId("inspector-cover-versions");
    expect(list.textContent).toContain("v3");
    expect(list.textContent).toContain("generated");
  });
});
