/**
 * 封面图片区拖拽源测试：封面缩略图可拖、dragstart 写入时间线载荷（clip_id）。
 * 应用/删除等既有行为由 inspector-pane 相关测试覆盖，这里只测拖拽增量。
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CoverWorkspace } from "./cover-workspace";
import { TIMELINE_COVER_DRAG_TYPE } from "@/components/studio/timeline-tracks";

function setup() {
  return render(<CoverWorkspace
    clipId="clip-a1"
    clipName="剪辑成片 01"
    coverImageUrl="http://localhost/cover.jpg"
    workflow={{ hasCover: true, pending: false, applied: true, revision: 2 }}
    busy={false}
    onApply={vi.fn()}
    onDelete={vi.fn()}
  />);
}

describe("封面图片拖拽源", () => {
  it("封面缩略图可拖拽，dragstart 携带 clip_id 载荷", () => {
    setup();
    const image = screen.getByTestId("cover-workspace-image");
    expect(image.getAttribute("draggable")).toBe("true");
    const setData = vi.fn();
    fireEvent.dragStart(image, {
      dataTransfer: {
        setData,
        effectAllowed: "none",
        types: [] as string[],
      },
    });
    expect(setData).toHaveBeenCalledTimes(1);
    const [type, raw] = setData.mock.calls[0];
    expect(type).toBe(TIMELINE_COVER_DRAG_TYPE);
    expect(JSON.parse(raw as string)).toEqual({ clip_id: "clip-a1" });
  });

  it("提示文案包含拖拽入口说明", () => {
    setup();
    const image = screen.getByTestId("cover-workspace-image");
    expect(image.getAttribute("title")).toContain("时间线");
  });
});
