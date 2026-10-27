/**
 * 独立封面板块（cover-assets-panel）组件测试（2026-10-20 紧凑横向卡版）。
 * 锁定：横向结构（固定宽 9:16 缩略图 + 信息区）、就绪态（预览/名称/状态/下载）、
 * 缩略图点击才开大图（信息区点击不开）、失败态不冒充、批次切换、刷新恢复、
 * Lightbox 真实比例标注与 Esc/背景关闭。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import CoverAssetsPanel, { CoverLightbox } from "./cover-assets-panel";
import type { CoverAssetItem } from "@/lib/studio-cover-assets";

const readyItem = (clipId: string, name: string): CoverAssetItem => ({
  project_id: "timeline_p1", source_id: "src01", clip_id: clipId, clip_name: name,
  render_task_id: "task01", revision: 1, status: "ready",
  path: `output/task01/${clipId}/cover.jpg`, bytes: 2048,
});
const failedItem = (clipId: string, name: string, reason: string): CoverAssetItem => ({
  project_id: "timeline_p1", source_id: "src01", clip_id: clipId, clip_name: name,
  render_task_id: "task01", revision: 1, status: "failed", reason,
});

const resolveUrl = (item: CoverAssetItem, download?: boolean) =>
  item.status === "ready" ? `http://x/artifact?path=${item.path}${download ? "&dl=1" : ""}` : null;

describe("CoverAssetsPanel：紧凑横向卡结构", () => {
  it("卡片 = 左固定宽缩略图按钮 + 右信息区（名称/状态/下载）", () => {
    const onSelect = vi.fn();
    render(<CoverAssetsPanel items={[readyItem("clip-1", "切片 1")]} loading={false} error={null}
      resolveUrl={resolveUrl} onSelectClip={onSelect} />);
    const card = document.querySelector(".cover-asset-card") as HTMLElement;
    expect(card.querySelector(".cover-asset-thumb")).not.toBeNull();   // 左：缩略图
    expect(card.querySelector(".cover-asset-info")).not.toBeNull();    // 右：信息区
    const info = card.querySelector(".cover-asset-info") as HTMLElement;
    expect(info.textContent).toContain("切片 1");
    expect(info.textContent).toContain("就绪");
    expect(info.querySelector("a")?.getAttribute("href")).toContain("dl=1");
    // 缩略图与信息区是兄弟节点（grid 两列：80px + minmax(0,1fr)）
    expect(card.children).toContain(card.querySelector(".cover-asset-thumb"));
    expect(card.children).toContain(info);
  });

  it("缩略图点击才开大图；信息区点击只选中、不开大图", () => {
    const onSelect = vi.fn();
    render(<CoverAssetsPanel items={[readyItem("clip-1", "切片 1")]} loading={false} error={null}
      resolveUrl={resolveUrl} onSelectClip={onSelect} />);
    expect(document.querySelector(".cover-lightbox-backdrop")).toBeNull();
    // 点缩略图 → Lightbox，且不触发选中
    fireEvent.click(screen.getByLabelText("预览 切片 1 封面大图"));
    const backdrop = document.querySelector(".cover-lightbox-backdrop") as HTMLElement;
    expect(backdrop).not.toBeNull();
    expect(onSelect).not.toHaveBeenCalled();
    expect(backdrop.querySelector("img")?.getAttribute("src")).toContain("output/task01/clip-1/cover.jpg");
    // 关闭后点信息区 → 选中、无 Lightbox
    fireEvent.click(screen.getByLabelText("关闭大图预览"));
    expect(document.querySelector(".cover-lightbox-backdrop")).toBeNull();
    fireEvent.click(screen.getByText("切片 1"));
    expect(onSelect).toHaveBeenCalledWith("clip-1");
    expect(document.querySelector(".cover-lightbox-backdrop")).toBeNull();
  });

  it("Lightbox：加载后按真实宽高标注比例；背景点击与 Esc 关闭", () => {
    const onClose = vi.fn();
    render(<CoverLightbox url="http://x/c.jpg" name="切片 1" onClose={onClose} />);
    const img = screen.getByAltText("切片 1 封面大图");
    Object.defineProperty(img, "naturalWidth", { value: 1080, configurable: true });
    Object.defineProperty(img, "naturalHeight", { value: 1920, configurable: true });
    fireEvent.load(img);
    expect(screen.getByText("切片 1 · 1080×1920 · 标准 9:16")).toBeTruthy();
    // 非标准历史素材 → contain 完整显示标注
    Object.defineProperty(img, "naturalWidth", { value: 1920, configurable: true });
    Object.defineProperty(img, "naturalHeight", { value: 1080, configurable: true });
    fireEvent.load(img);
    expect(screen.getByText("切片 1 · 1920×1080 · 非标准比例（contain 完整显示）")).toBeTruthy();
    // 背景点击关闭；图片本体点击不关闭
    fireEvent.click(screen.getByAltText("切片 1 封面大图"));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(document.querySelector(".cover-lightbox-backdrop") as HTMLElement);
    expect(onClose).toHaveBeenCalledTimes(1);
    // Esc 关闭
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByLabelText("关闭大图预览"));
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it("失败封面：缩略图按钮禁用（无法打开大图）、失败态与原因可见、无下载", () => {
    const onSelect = vi.fn();
    render(<CoverAssetsPanel
      items={[failedItem("clip-2", "切片 2", "封面文件缺失（登记于台账但不在磁盘）")]}
      loading={false} error={null} resolveUrl={resolveUrl} onSelectClip={onSelect} />);
    const thumb = screen.getByLabelText("预览 切片 2 封面大图") as HTMLButtonElement;
    expect(thumb.disabled).toBe(true);                                   // 无图可预览
    const frame = document.querySelector(".cover-asset-frame") as HTMLElement;
    expect(frame.className).toContain("is-failed");
    expect(frame.querySelector("img")).toBeNull();                       // 不冒充
    expect(frame.textContent).toContain("封面生成失败");
    expect(frame.textContent).toContain("封面文件缺失");
    expect(document.querySelector(".cover-asset-card")?.querySelector("a")).toBeNull();
  });

  it("img 加载失败（404 等）降级为失败态，并阻止继续打开大图", () => {
    render(<CoverAssetsPanel items={[readyItem("clip-1", "切片 1")]} loading={false} error={null}
      resolveUrl={resolveUrl} />);
    const thumb = screen.getByLabelText("预览 切片 1 封面大图");
    fireEvent.click(thumb);
    const img = document.querySelector(".cover-lightbox-backdrop img");
    expect(img).not.toBeNull();
    fireEvent.click(screen.getByLabelText("关闭大图预览"));
    fireEvent.error(screen.getByAltText("切片 1 独立封面"));
    const frame = document.querySelector(".cover-asset-frame") as HTMLElement;
    expect(frame.className).toContain("is-failed");
    expect(frame.textContent).toContain("封面文件无法读取");
    fireEvent.click(thumb);
    expect(document.querySelector(".cover-lightbox-backdrop")).toBeNull(); // 坏图不再可预览
  });

  it("批次切换：旧切片卡片消失；刷新恢复：同数据重挂载渲染一致", () => {
    const items = [readyItem("clip-1", "切片 1"), failedItem("clip-2", "切片 2", "未生成封面")];
    const { rerender } = render(<CoverAssetsPanel items={items} loading={false} error={null}
      resolveUrl={resolveUrl} />);
    rerender(<CoverAssetsPanel items={[readyItem("clip-new", "新切片")]} loading={false} error={null}
      resolveUrl={resolveUrl} />);
    expect(screen.queryByText("切片 1")).toBeNull();
    expect(screen.getByText("新切片")).toBeTruthy();
    // 刷新恢复（重挂载一致）
    const first = render(<CoverAssetsPanel items={items} loading={false} error={null} resolveUrl={resolveUrl} />);
    const html = first.container.innerHTML;
    first.unmount();
    const second = render(<CoverAssetsPanel items={items} loading={false} error={null} resolveUrl={resolveUrl} />);
    expect(second.container.innerHTML).toBe(html);
  });

  it("空态/加载/错误与响应式结构类名齐备", () => {
    const { container, rerender } = render(<CoverAssetsPanel items={[]} loading={false} error={null}
      resolveUrl={resolveUrl} />);
    expect(container.textContent).toContain("自动剪辑完成后");
    rerender(<CoverAssetsPanel items={[]} loading={true} error={null} resolveUrl={resolveUrl} />);
    expect(container.textContent).toContain("正在读取");
    rerender(<CoverAssetsPanel items={[readyItem("clip-1", "切片 1")]} loading={false} error={null}
      resolveUrl={resolveUrl} />);
    // 响应式布局契约类：单列横向列表 + 固定宽缩略图 + 可伸缩信息区
    expect(document.querySelector(".cover-asset-list")).not.toBeNull();
    expect(document.querySelector(".cover-asset-thumb")).not.toBeNull();
    expect(document.querySelector(".cover-asset-info")).not.toBeNull();
  });
});
