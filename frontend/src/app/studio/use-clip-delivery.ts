/**
 * Studio 投递与术语表 hook（阶段5·点2 拆分，2026-11-23）。
 *
 * 从 page.tsx 原地外移「发送到豆包收件箱」单条/批量投递簇与「项目术语表」读写簇。
 * 外部依赖仅 activeProjectId 与 setMessage；函数保留原 `async function` 声明形式
 * （非 useCallback），行为与外移前逐行等价。
 */

import { useState } from "react";
import type { RenderedClip } from "./studio-page-model";

export function useClipDelivery(options: {
  activeProjectId: string | null;
  setMessage: (message: string) => void;
}) {
  const { activeProjectId, setMessage } = options;
  const [deliveringClipId, setDeliveringClipId] = useState<string | null>(null);
  // 批量投递：卡片勾选 → 一次请求逐条投递（单条失败不中断批次）。
  const [selectedClipIds, setSelectedClipIds] = useState<string[]>([]);
  const [batchDelivering, setBatchDelivering] = useState(false);
  // ===== 项目术语表（转写热词 + 纠错参照，2026-10-28）=====
  const [glossaryOpen, setGlossaryOpen] = useState(false);
  const [glossaryText, setGlossaryText] = useState("");
  const [glossaryBusy, setGlossaryBusy] = useState(false);

  async function deliverClipToDoubao(clip: RenderedClip) {
    if (!activeProjectId) { setMessage("尚未打开项目，无法投递。"); return; }
    if (!window.confirm(`把成片「${clip.name}」发送到豆包收件箱？\n将经过 95 暂存 → 本地拉取，随后出现在豆包侧本地 inbox。`)) return;
    setDeliveringClipId(clip.clip_id);
    setMessage(`正在发送「${clip.name}」到抖音收件箱…`);
    try {
      const resp = await fetch(`/video-api/studio/projects/${activeProjectId}/clips/${clip.clip_id}/deliver`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ title: clip.topic || clip.name }),
      });
      const data = await resp.json().catch(() => ({}) as Record<string, unknown>);
      if (resp.ok) {
        const receipt = JSON.stringify((data as { delivery?: unknown })?.delivery ?? {});
        setMessage(`「${clip.name}」已发送到豆包收件箱 ✓（回执：${receipt.slice(0, 140)}）。等豆包侧拉取后，即可在本地 inbox 见到该成片。`);
      } else {
        const detail = (data as { detail?: unknown }).detail;
        const line = typeof detail === "string" ? detail
          : typeof detail === "object" && detail !== null && "detail" in detail ? String((detail as { detail: unknown }).detail)
          : `HTTP ${resp.status}`;
        setMessage(`「${clip.name}」发送失败：${line}${resp.status === 503 ? "（投递通道未配置 upload_token）" : ""}`);
      }
    } catch {
      setMessage(`「${clip.name}」发送失败：网络错误`);
    } finally {
      setDeliveringClipId(null);
    }
  }

  async function glossaryToggle() {
    if (glossaryOpen) { setGlossaryOpen(false); return; }
    if (!activeProjectId) { setMessage("尚未打开项目，无法编辑术语表。"); return; }
    setGlossaryOpen(true);
    setGlossaryBusy(true);
    try {
      const resp = await fetch(`/video-api/studio/projects/${activeProjectId}/glossary`, { credentials: "include" });
      const data = await resp.json().catch(() => ({}) as { terms?: string[] });
      setGlossaryText(((data as { terms?: string[] }).terms || []).join("\n"));
    } catch { setMessage("术语表读取失败"); }
    finally { setGlossaryBusy(false); }
  }

  async function glossarySave() {
    if (!activeProjectId) return;
    const terms = glossaryText.split("\n").map((x) => x.trim()).filter(Boolean);
    setGlossaryBusy(true);
    try {
      const resp = await fetch(`/video-api/studio/projects/${activeProjectId}/glossary`, {
        method: "PUT", headers: { "content-type": "application/json" }, credentials: "include",
        body: JSON.stringify({ terms }),
      });
      const data = await resp.json().catch(() => ({}) as { count?: number; terms?: string[] });
      if (resp.ok) {
        setGlossaryText(((data as { terms?: string[] }).terms || []).join("\n"));
        setMessage(`术语表已保存（${(data as { count?: number }).count ?? 0} 个词）。下次转写与纠错自动生效。`);
      } else { setMessage(`术语表保存失败：HTTP ${resp.status}`); }
    } catch { setMessage("术语表保存失败：网络错误"); }
    finally { setGlossaryBusy(false); }
  }

  function toggleClipSelect(clipId: string, checked: boolean) {
    setSelectedClipIds((old) => checked ? (old.includes(clipId) ? old : [...old, clipId]) : old.filter((x) => x !== clipId));
  }

  async function deliverSelectedClips() {
    if (!activeProjectId || selectedClipIds.length === 0) return;
    if (!window.confirm(`把已选的 ${selectedClipIds.length} 条成片批量发送到豆包收件箱？\n将经过 95 暂存 → 本地拉取，随后出现在豆包侧本地 inbox。`)) return;
    setBatchDelivering(true);
    setMessage(`正在批量发送 ${selectedClipIds.length} 条成片到抖音收件箱…`);
    try {
      const resp = await fetch(`/video-api/studio/projects/${activeProjectId}/clips/deliver-batch`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ clip_ids: selectedClipIds }),
      });
      const data = await resp.json().catch(() => ({}) as Record<string, unknown>);
      if (resp.ok) {
        const summary = data as { succeeded?: number; failed?: number; results?: Array<{ clip_id: string; ok: boolean; error?: string }> };
        const fails = (summary.results || []).filter((r) => !r.ok).map((r) => `${r.clip_id}: ${r.error || "失败"}`).join("；");
        setMessage(`批量发送完成：成功 ${summary.succeeded ?? 0} 条，失败 ${summary.failed ?? 0} 条。${fails ? `失败明细：${fails}` : "等豆包侧拉取后，即可在本地 inbox 见到该成片。"}`);
        if (!summary.failed) setSelectedClipIds([]);
      } else {
        const detail = (data as { detail?: unknown }).detail;
        setMessage(`批量发送失败：${typeof detail === "string" ? detail : `HTTP ${resp.status}`}`);
      }
    } catch {
      setMessage("批量发送失败：网络错误");
    } finally {
      setBatchDelivering(false);
    }
  }

  return {
    deliveringClipId, selectedClipIds, setSelectedClipIds, batchDelivering,
    glossaryOpen, setGlossaryOpen, glossaryText, setGlossaryText, glossaryBusy,
    deliverClipToDoubao, glossaryToggle, glossarySave, toggleClipSelect, deliverSelectedClips,
  };
}
