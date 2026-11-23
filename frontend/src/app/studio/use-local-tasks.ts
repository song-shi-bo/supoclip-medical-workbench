/**
 * Studio 局部任务 hook（阶段5·点2 拆分，2026-11-23）。
 *
 * 从 page.tsx 原地外移「创建类型化局部任务 + 轮询到终态」的执行簇：
 * pollLocalTask / runLocalClipTask。状态（localTaskBusy）仍归页面所有，
 * hook 只收 setter；applyProjectCard / loadProjectCards 由页面注入，
 * 行为与外移前逐行等价。
 */

import { useCallback } from "react";
import {
  LOCAL_TASK_POLL_INTERVAL_MS,
  LOCAL_TASK_POLL_TIMEOUT_MS,
  type LocalTaskSpec,
  type LocalTaskStatus,
  type ProjectClipCard,
  type RenderedClip,
} from "./studio-page-model";

export function useStudioLocalTasks(options: {
  mountedRef: React.RefObject<boolean>;
  studioBase: string;
  setLocalTaskBusy: (busy: boolean) => void;
  applyProjectCard: (card: ProjectClipCard) => boolean;
  loadProjectCards: (projectId: string) => Promise<unknown> | unknown;
}) {
  const { mountedRef, studioBase, setLocalTaskBusy, applyProjectCard, loadProjectCards } = options;

  /**
   * 轮询一个局部任务（端点 3）直到 completed/failed，或 120s 超时。
   * 后端的 error/detail 原样抛出 —— 例如 cutout_v3 缺权重时的
   * E_CUTOUT_WEIGHTS_MISSING 与安装指引，不得被吞掉或改写成别的风格。
   */
  const pollLocalTask = useCallback(async (taskId: string, onProgress?: (status: LocalTaskStatus) => void, timeoutMs = LOCAL_TASK_POLL_TIMEOUT_MS): Promise<LocalTaskStatus> => {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      if (!mountedRef.current) throw new Error("页面已关闭，任务仍在后台执行");
      const response = await fetch(`${studioBase}/studio/tasks/${encodeURIComponent(taskId)}`, { cache: "no-store" });
      const text = await response.text();
      const payload = text ? JSON.parse(text) as LocalTaskStatus : null;
      if (!response.ok || !payload) throw new Error(payload?.detail || `无法读取任务进度（HTTP ${response.status}）`);
      onProgress?.(payload);
      if (payload.status === "completed") return payload;
      if (payload.status === "failed") throw new Error(payload.error || payload.detail || "局部任务失败");
      if (Date.now() >= deadline) throw new Error(`任务 ${taskId} 超时未完成（已等待 ${Math.round(timeoutMs / 1000)} 秒），请稍后在任务表查看`);
      await new Promise((resolve) => { window.setTimeout(resolve, LOCAL_TASK_POLL_INTERVAL_MS); });
    }
  }, [studioBase, mountedRef]);

  /**
   * 创建一个「类型化局部任务」（端点 2）并轮询到结束：
   * 成功 → 用 result.card 原地刷新该 clip（拿不到就重拉端点 1）；失败 → 原样抛出后端 detail。
   */
  const runLocalClipTask = useCallback(async (
    projectId: string,
    clip: RenderedClip,
    spec: LocalTaskSpec,
  ): Promise<LocalTaskStatus> => {
    setLocalTaskBusy(true);
    try {
      const body: Record<string, unknown> = { task_type: spec.task_type, instruction: spec.instruction };
      if (spec.payload) body.payload = spec.payload;
      if (spec.style_id) body.style_id = spec.style_id;
      if (spec.replace_intro !== undefined) body.replace_intro = spec.replace_intro;
      if (spec.cover_copy) body.cover_copy = spec.cover_copy;
      if (spec.title !== undefined) body.title = spec.title;
      const response = await fetch(
        `${studioBase}/studio/projects/${encodeURIComponent(projectId)}/clips/${encodeURIComponent(clip.clip_id)}/tasks`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
      );
      const createdText = await response.text();
      const created = createdText ? JSON.parse(createdText) as { task_id?: string; detail?: string } : null;
      if (!response.ok || !created?.task_id) throw new Error(created?.detail || `无法创建局部任务（HTTP ${response.status}）`);
      const status = await pollLocalTask(created.task_id, spec.onProgress, spec.timeoutMs);
      const card = status.result?.card;
      if (card && mountedRef.current) {
        const hit = applyProjectCard(card);
        if (!hit) void loadProjectCards(projectId);
      } else if (mountedRef.current) {
        void loadProjectCards(projectId);
      }
      return status;
    } finally {
      if (mountedRef.current) setLocalTaskBusy(false);
    }
  }, [studioBase, pollLocalTask, loadProjectCards, setLocalTaskBusy, applyProjectCard, mountedRef]);

  return { pollLocalTask, runLocalClipTask };
}
