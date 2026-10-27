"use client";

// 保存状态统一显示（契约 §6）：保存中 / 已保存 / 失败原因。
// 每个会写后端的控件都必须带上它，禁止「点了按钮什么反馈都没有」。

import { AlertTriangle, Check, LoaderCircle } from "lucide-react";

import type { SaveState } from "./types";

export function saveStateLabel(state: SaveState): string {
  if (state.status === "saving") return "保存中…";
  if (state.status === "saved") {
    return state.revision !== undefined ? `已保存 rev-${state.revision}` : "已保存";
  }
  if (state.status === "error") return `失败：${state.message || "未知原因"}`;
  return state.message || "";
}

export function SaveBadge({ state, className = "" }: { state: SaveState; className?: string }) {
  if (state.status === "idle" && !state.message) return null;
  const label = saveStateLabel(state);
  return <span
    className={`inspector-save ${state.status} ${className}`.trim()}
    data-testid="inspector-save"
    data-status={state.status}
    role="status"
    title={state.status === "error" ? state.message || "" : label}
  >
    {state.status === "saving" && <LoaderCircle size={12} />}
    {state.status === "saved" && <Check size={12} />}
    {state.status === "error" && <AlertTriangle size={12} />}
    {label}
  </span>;
}
