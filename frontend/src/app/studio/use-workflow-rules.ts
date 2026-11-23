/**
 * Studio 规则中心 hook（阶段5·点2 拆分，2026-11-23）。
 *
 * 从 page.tsx 原地外移「规则中心」状态与执行簇：规则清单读取（loadWorkflow）、
 * skill 启停（toggleWorkflowSkill）、成片规则快照（viewClipRules）与挂载即取一次
 * 清单的 effect。状态全部归 hook 所有并原样返回，页面解构后 JSX 与其它调用点
 * 行为逐行等价。
 */

import { useCallback, useEffect, useState } from "react";
import type {
  RenderedClip,
  WorkflowBanner,
  WorkflowPayload,
  WorkflowSnapshotPayload,
  WorkflowSummary,
} from "./studio-page-model";

export function useWorkflowRules(options: {
  mountedRef: React.RefObject<boolean>;
  studioBase: string;
}) {
  const { mountedRef, studioBase } = options;
  const [workflowBanner, setWorkflowBanner] = useState<WorkflowBanner | null>(null);
  const [workflowSummary, setWorkflowSummary] = useState<WorkflowSummary | null>(null);
  const [workflowPanelOpen, setWorkflowPanelOpen] = useState(false);
  const [workflowBusy, setWorkflowBusy] = useState(false);
  const [workflowMessage, setWorkflowMessage] = useState<string | null>(null);
  const [workflowSnapshot, setWorkflowSnapshot] = useState<WorkflowSnapshotPayload | null>(null);
  const [workflowSnapshotClipId, setWorkflowSnapshotClipId] = useState<string | null>(null);

  /**
   * 拉取规则中心数据（端点 GET /studio/workflow）。
   * 后端**每次实时读清单**，所以这里不做任何缓存：打开面板或点「刷新」就能看到
   * 「外部改了 skill 文件」或「刚启停过 skill」之后的新版本与新校验状态，无需重启浏览器。
   */
  const loadWorkflow = useCallback(async (): Promise<WorkflowPayload | null> => {
    try {
      const response = await fetch(`${studioBase}/studio/workflow`, { cache: "no-store" });
      const text = await response.text();
      const payload = text ? JSON.parse(text) as WorkflowPayload : null;
      if (!response.ok || !payload?.banner) {
        if (mountedRef.current) setWorkflowMessage(payload?.detail || `无法读取规则中心（HTTP ${response.status}）`);
        return null;
      }
      if (mountedRef.current) {
        setWorkflowBanner(payload.banner);
        setWorkflowSummary(payload.summary ?? null);
      }
      return payload;
    } catch {
      // 注册表不可达：保留既有 banner（不显示一个假的「默认版本」），只记录提示。
      if (mountedRef.current) setWorkflowMessage("规则中心不可达（后端未启动或清单缺失），未显示任何默认规则。");
      return null;
    }
  }, [studioBase]);

  /** 启停某个 skill（后端：备份 → 写回 → 立刻校验 → 非法回滚并 4xx）。 */
  const toggleWorkflowSkill = useCallback(async (skillId: string, enabled: boolean) => {
    setWorkflowBusy(true);
    try {
      const response = await fetch(
        `${studioBase}/studio/workflow/skills/${encodeURIComponent(skillId)}/toggle`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) },
      );
      const text = await response.text();
      const payload = text ? JSON.parse(text) as {
        banner?: WorkflowBanner; summary?: WorkflowSummary; detail?: string;
        workflow_version?: string; backup_path?: string;
      } : null;
      if (!response.ok) throw new Error(payload?.detail || `启停失败（HTTP ${response.status}）`);
      if (!mountedRef.current) return;
      setWorkflowBanner(payload?.banner ?? null);
      setWorkflowSummary(payload?.summary ?? null);
      setWorkflowMessage(`${skillId} 已${enabled ? "启用" : "停用"}：规则版本 v${payload?.workflow_version ?? "?"}（清单已备份，下一次自动剪辑将使用该版本）`);
    } catch (error) {
      if (mountedRef.current) setWorkflowMessage(error instanceof Error ? error.message : "启停规则失败");
    } finally {
      if (mountedRef.current) setWorkflowBusy(false);
    }
  }, [studioBase]);

  /** 「查看本次成片实际使用了哪些规则」：读该成片渲染任务的规则快照。 */
  const viewClipRules = useCallback(async (clip: RenderedClip) => {
    setWorkflowBusy(true);
    setWorkflowPanelOpen(true);
    setWorkflowSnapshotClipId(clip.clip_id);
    try {
      const response = await fetch(
        `${studioBase}/studio/workflow/${encodeURIComponent(clip.task_id)}/snapshot`, { cache: "no-store" });
      const text = await response.text();
      const payload = text ? JSON.parse(text) as WorkflowSnapshotPayload : null;
      if (!response.ok || !payload) throw new Error(payload?.detail || `无法读取规则快照（HTTP ${response.status}）`);
      if (mountedRef.current) setWorkflowSnapshot(payload);
    } catch (error) {
      if (mountedRef.current) {
        setWorkflowSnapshot(null);
        setWorkflowMessage(error instanceof Error ? error.message : "读取规则快照失败");
      }
    } finally {
      if (mountedRef.current) setWorkflowBusy(false);
    }
  }, [studioBase]);

  // 打开页面即取一次规则清单：顶栏不再显示规则版本，这里的数据供非顶栏位置使用
  // （剪辑助手执行任务后的「已按当前规则生成，规则版本：vX」与规则快照面板）。
  useEffect(() => { void loadWorkflow(); }, [loadWorkflow]);

  return {
    workflowBanner, workflowSummary, workflowPanelOpen, setWorkflowPanelOpen,
    workflowBusy, workflowMessage, setWorkflowMessage, workflowSnapshot,
    setWorkflowSnapshot, workflowSnapshotClipId,
    loadWorkflow, toggleWorkflowSkill, viewClipRules,
  };
}
