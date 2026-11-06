// 封面与画面标签页测试（契约 §6）：
// 2026-10-21 精简版：封面图片区/版本管理已迁移出本组件（页面级 CoverWorkspace 承担），
// 这里钉住「只剩画面子区」的现状 + 画面比例菜单只改预览、变换「应用」触发回调（取消不触发）。

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
  it("封面管理 UI 已按精简版移除：只剩「画面」子区，不残留重做/标题/历史/沿用/撤销入口", () => {
    renderTab();
    // 旧的封面版本管理（预览/重做指令/编辑标题/历史/沿用/撤销/版本列表）迁出本组件，
    // 由页面级 CoverWorkspace 承担（见 cover-picture-tab.tsx 头注释）。
    expect(screen.queryByTestId("inspector-cover-active")).not.toBeInTheDocument();
    expect(screen.queryByTestId("inspector-cover-instruction")).not.toBeInTheDocument();
    expect(screen.queryByTestId("inspector-cover-regenerate")).not.toBeInTheDocument();
    expect(screen.queryByTestId("inspector-cover-edit-title")).not.toBeInTheDocument();
    expect(screen.queryByTestId("inspector-cover-history")).not.toBeInTheDocument();
    expect(screen.queryByTestId("inspector-cover-adopt")).not.toBeInTheDocument();
    expect(screen.queryByTestId("inspector-cover-undo")).not.toBeInTheDocument();
    expect(screen.queryByTestId("inspector-cover-versions")).not.toBeInTheDocument();
    // 画面子区仍在。
    expect(screen.getByTestId("inspector-cover-picture-tab").textContent).toContain("画面");
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
});
