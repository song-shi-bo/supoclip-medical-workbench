// 「效果」标签页测试（成片风格微调，契约 §5 效果设置 + 导出生效）：
// 五分区（风格预设/画面调整/画幅与构图/转场/音频处理）真实可改；
// 颜色类预览是 CSS 近似并如实标注；几何类复用既有 transform；导出侧由 FFmpeg 精确生效。

import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { server } from "@/test/setup";
import type { ClipTransform } from "@/lib/clip-transform";

import { EffectsTab, type EffectsTabProps } from "./effects-tab";
import { DEFAULT_EFFECTS } from "./types";
import { bgmTrack, inspectorHandlers, renderConfigView } from "./test-utils";

// 用 setup.ts 的全局 msw server（见 timeline/asset 测试里的说明：不要二次 listen）。
beforeEach(() => server.use(...inspectorHandlers()));

const TRANSFORM: ClipTransform = {
  scale: 1,
  positionX: 0,
  positionY: 0,
  crop: { left: 0, top: 0, right: 1, bottom: 1 },
  fitMode: "cover" as const,
  rotation: 0 as const,
};

function renderTab(overrides: Partial<EffectsTabProps> = {}) {
  const handlers = {
    onPreview: vi.fn(),
    onSave: vi.fn().mockResolvedValue({ changed: ["effects"] }),
    onPreviewAudio: vi.fn(),
    onSaveAudio: vi.fn().mockResolvedValue({ changed: ["audio_tracks", "voice_volume"] }),
    onTransformPreview: vi.fn(),
    onTransformApply: vi.fn().mockResolvedValue(undefined),
    onViewerAspectChange: vi.fn(),
    onRefreshRenderConfig: vi.fn(),
  };
  const props: EffectsTabProps = {
    effects: DEFAULT_EFFECTS,
    audio: { voice_volume: 1, muted: false, audio_tracks: [] },
    transform: TRANSFORM,
    transformLocked: false,
    viewerAspect: "9:16",
    renderConfig: renderConfigView(),
    renderConfigLoading: false,
    renderConfigError: null,
    ...handlers,
    ...overrides,
  };
  const view = render(<EffectsTab {...props} />);
  return { handlers, ...view };
}

describe("EffectsTab（成片风格微调）", () => {
  it("五个分区齐全，并如实标注「颜色类预览为近似」", () => {
    const { container } = renderTab();
    const headers = [...container.querySelectorAll(".inspector-block > header strong")].map((n) => n.textContent);
    expect(headers).toEqual(["风格预设", "画面调整", "画幅与构图", "转场", "音频处理"]);
    expect(screen.getByTestId("inspector-effects-approximate-note").textContent)
      .toContain("近似");
  });

  it("风格预设：点击「医疗专业」→ 预览 + 去抖保存，且显示当前预设", async () => {
    vi.useFakeTimers();
    const { handlers } = renderTab();
    fireEvent.click(screen.getByRole("button", { name: "风格预设 医疗专业" }));
    expect(handlers.onPreview).toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(600);
    expect(handlers.onSave).toHaveBeenLastCalledWith(expect.objectContaining({ preset: "medical" }));
    vi.useRealTimers();
    expect(screen.getByTestId("inspector-active-preset").textContent).toContain("医疗专业");
  });

  it("画面调整：亮度滑杆 → 近似滤镜文本更新 + 去抖保存 picture.brightness", async () => {
    vi.useFakeTimers();
    const { handlers } = renderTab();
    fireEvent.change(screen.getByLabelText("亮度"), { target: { value: "0.25" } });
    expect(handlers.onPreview).toHaveBeenLastCalledWith(expect.objectContaining({
      picture: expect.objectContaining({ brightness: 0.25 }),
    }));
    expect(screen.getByTestId("inspector-preview-filter").textContent).toContain("brightness(1.25)");
    await vi.advanceTimersByTimeAsync(600);
    expect(handlers.onSave).toHaveBeenLastCalledWith(expect.objectContaining({
      picture: expect.objectContaining({ brightness: 0.25 }),
    }));
    vi.useRealTimers();
  });

  it("画幅与构图：比例写进 effects 并标注仅预览；缩放走既有 transform（预览→应用）", async () => {
    vi.useFakeTimers();
    const { handlers } = renderTab();
    expect(screen.getByTestId("inspector-ratio-preview-only").textContent).toContain("仅影响预览");
    fireEvent.change(screen.getByLabelText("画幅比例"), { target: { value: "16:9" } });
    fireEvent.change(screen.getByLabelText("片段缩放"), { target: { value: "1.25" } });
    expect(handlers.onTransformPreview).toHaveBeenLastCalledWith(expect.objectContaining({ scale: 1.25 }));
    await vi.advanceTimersByTimeAsync(600);
    expect(handlers.onSave).toHaveBeenLastCalledWith(expect.objectContaining({
      ratio: expect.objectContaining({ name: "16:9" }),
    }));
    expect(handlers.onTransformApply).toHaveBeenCalled();
    vi.useRealTimers();
  });

  it("转场：fade + 时长 0.8s 保存到 effects.transition", async () => {
    vi.useFakeTimers();
    const { handlers } = renderTab();
    fireEvent.change(screen.getByLabelText("转场类型"), { target: { value: "fade" } });
    fireEvent.change(screen.getByLabelText("转场时长"), { target: { value: "0.8" } });
    await vi.advanceTimersByTimeAsync(600);
    expect(handlers.onSave).toHaveBeenLastCalledWith(expect.objectContaining({
      transition: { enabled: true, type: "fade", duration_s: 0.8 },
    }));
    vi.useRealTimers();
  });

  it("音频处理：原声音量 → onPreviewAudio/onSaveAudio；无 BGM 时控件置灰并提示先上传", async () => {
    vi.useFakeTimers();
    const { handlers } = renderTab();
    const bgmVolume = screen.getByLabelText("BGM 音量") as HTMLInputElement;
    expect(bgmVolume).toBeDisabled();
    expect(screen.getByTestId("inspector-bgm-note").textContent).toContain("无 BGM 素材");
    fireEvent.change(screen.getByLabelText("原声音量"), { target: { value: "0.6" } });
    expect(handlers.onPreviewAudio).toHaveBeenLastCalledWith(expect.objectContaining({ voice_volume: 0.6 }));
    await vi.advanceTimersByTimeAsync(600);
    expect(handlers.onSaveAudio).toHaveBeenLastCalledWith(expect.objectContaining({ voice_volume: 0.6 }));
    vi.useRealTimers();
  });

  it("有 BGM 时：音量/ducking 可用，ducking 开关保存并说明 sidechaincompress", async () => {
    vi.useFakeTimers();
    const { handlers } = renderTab({
      audio: { voice_volume: 1, muted: false, audio_tracks: [bgmTrack()] },
    });
    expect(screen.getByLabelText("BGM 音量")).toBeEnabled();
    fireEvent.click(screen.getByRole("switch", { name: "BGM 自动压低开关" }));
    await vi.advanceTimersByTimeAsync(600);
    expect(handlers.onSave).toHaveBeenLastCalledWith(expect.objectContaining({
      audio: expect.objectContaining({ ducking: true }),
    }));
    vi.useRealTimers();
    expect(screen.getByTestId("inspector-bgm-note").textContent).toContain("sidechaincompress");
  });

  it("render_config 摘要：逐项生效值 + 来源标签 + 导出备注（FFmpeg 精确生效、画幅仅预览）", () => {
    renderTab({ renderConfig: renderConfigView({ picture: { ...DEFAULT_EFFECTS.picture, filter: "warm" } }) });
    const summary = screen.getByTestId("inspector-render-config");
    expect(summary.textContent).toContain("导出前 render_config 摘要");
    expect(screen.getByTestId("inspector-source-subtitle_config.font_size_px").textContent).toBe("用户修改");
    expect(screen.getByTestId("inspector-source-effects.picture.filter").textContent).toBe("skill 默认");
    expect(screen.getByTestId("inspector-render-config-export").textContent).toContain("1080x1920");
    // 分辨率仍是唯一 Phase 2 项；画面/转场/音频已由 FFmpeg 在导出通道生效。
    expect(screen.getByTestId("inspector-phase2-resolution").textContent).toContain("分辨率 Phase 2");
    const note = screen.getByTestId("inspector-render-config-note").textContent;
    expect(note).toContain("FFmpeg");
    expect(note).toContain("画幅比例当前仅影响预览");
  });

  it("摘要读取失败时显示可读原因，不显示假数据", () => {
    renderTab({ renderConfig: null, renderConfigError: "读取配置失败：HTTP 500" });
    expect(screen.getByTestId("inspector-render-config").textContent).toContain("HTTP 500");
    expect(screen.queryByTestId("inspector-render-config-export")).not.toBeInTheDocument();
  });

  it("保存失败 → SaveBadge 显示失败原因", async () => {
    vi.useFakeTimers();
    const onSave = vi.fn().mockRejectedValue(new Error("E_CONFIG_UNKNOWN_SECTION: 未知配置段"));
    renderTab({ onSave });
    fireEvent.change(screen.getByLabelText("转场类型"), { target: { value: "fade" } });
    await vi.advanceTimersByTimeAsync(600);
    await vi.waitFor(() => {
      const badge = screen.getAllByTestId("inspector-save").find((node) => node.getAttribute("data-status") === "error");
      expect(badge?.textContent).toContain("未知配置段");
    });
    vi.useRealTimers();
  });
});
