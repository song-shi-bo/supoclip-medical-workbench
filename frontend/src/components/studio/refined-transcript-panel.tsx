"use client";

/**
 * 右侧「切片精修文案」面板（两步制第二步消费侧，2026-11-05）。
 *
 * 需求锚点（用户 2026-11-05 拍板，29 条示例只是模板、绝不能硬编码）：
 *  - 表头固定四列：# / 时间码 / 角色 / 上屏字幕；时间为**切片相对时间**
 *    （后端已减去计划 start_ms），口径 00:00–00:02；
 *  - 数据源 GET /studio/auto-edit/{task_id}/clips/{clip_id}/refined-transcript
 *    （结构化 JSON）；文案下载用同源的 refined-copy.md；
 *  - 诚实展示：加载/404/错误分态；fallback 行如实标注「原始纠错转写」，
 *    没有时间码显示 —（绝不编造）；空角色显示「未标注」；
 *  - 防串片：任务或切片变化即 abort 旧请求，并对迟到响应做 clip_id 校验。
 */

import { useEffect, useState } from "react";

export type RefinedTranscriptRow = {
  index: number;
  /** 切片相对毫秒；fallback / 计划缺失时为 null（显示 —，不编造）。 */
  start_ms: number | null;
  end_ms: number | null;
  speaker: string;
  text: string;
};

export type RefinedTranscriptPayload = {
  task_id: string;
  clip_id: string;
  theme: string;
  source: "llm" | "fallback";
  fallback_reason?: string | null;
  prompt_version: string;
  model?: string | null;
  rows: RefinedTranscriptRow[];
  warnings: string[];
  checklist_pending: string[];
  checklist_compliance: string[];
  ending_cards: string[];
};

export type RefinedTranscriptPanelProps = {
  taskId: string;
  clipId: string;
  /** 后端 API 基址（页面注入，如 /video-api）；组件不耦合代理配置。 */
  apiBase: string;
};

/** 相对毫秒 → mm:ss（模板口径 00:00）。 */
export function formatRelativeTimecode(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

type Phase = "loading" | "ready" | "not_found" | "error";

export default function RefinedTranscriptPanel({ taskId, clipId, apiBase }: RefinedTranscriptPanelProps) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [payload, setPayload] = useState<RefinedTranscriptPayload | null>(null);
  const [errorText, setErrorText] = useState("");
  const [reloadNonce, setReloadNonce] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    // 防串片主保险：任务或切片变化时本 effect 被清理，superseded 置位后
    // 迟到响应一律不再 setState（不依赖 AbortController 是否真正生效）。
    let superseded = false;
    setPhase("loading");
    setPayload(null);
    setErrorText("");
    (async () => {
      try {
        const url = `${apiBase}/studio/auto-edit/${encodeURIComponent(taskId)}/clips/${encodeURIComponent(clipId)}/refined-transcript`;
        const response = await fetch(url, { cache: "no-store", signal: controller.signal });
        if (superseded) return;
        if (response.status === 404) {
          let detail = "";
          try { detail = String((await response.json())?.detail || ""); } catch { /* 保持空 */ }
          if (superseded) return;
          setPhase("not_found");
          setErrorText(detail);
          return;
        }
        if (!response.ok) {
          if (superseded) return;
          setPhase("error");
          setErrorText(`精修文案请求失败（HTTP ${response.status}）`);
          return;
        }
        const data = (await response.json()) as RefinedTranscriptPayload;
        if (superseded) return;
        // 防串片双保险：当前请求返回的载荷与请求切片不一致（服务端异常）→ 如实报错。
        if (String(data.task_id) !== taskId || String(data.clip_id) !== clipId) {
          setPhase("error");
          setErrorText("精修文案响应与当前切片不一致，已丢弃；请重试。");
          return;
        }
        setPayload(data);
        setPhase("ready");
      } catch (error) {
        if (superseded || (error as Error)?.name === "AbortError") return;
        setPhase("error");
        setErrorText(error instanceof Error ? error.message : "精修文案加载失败");
      }
    })();
    return () => {
      superseded = true;
      controller.abort();
    };
  }, [taskId, clipId, apiBase, reloadNonce]);

  const copyUrl = `${apiBase}/studio/auto-edit/${encodeURIComponent(taskId)}/clips/${encodeURIComponent(clipId)}/refined-copy.md`;

  return (
    <section className="insight-panel refined-transcript-panel" aria-label="切片精修文案面板">
      <header>
        <strong>切片精修文案</strong>
        {phase === "ready" && payload && (
          <span className={`refined-source ${payload.source}`}>
            {payload.source === "llm" ? "模型精修" : "原始纠错转写"}
          </span>
        )}
      </header>

      {phase === "loading" && <p>正在加载精修文案…</p>}

      {phase === "not_found" && (
        <p>{errorText || "精修产物不存在（该任务可能早于两步制运行，重新规划后可用）。"} 成片字幕不受影响，仍为原始纠错转写。</p>
      )}

      {phase === "error" && <p role="alert">{errorText || "精修文案加载失败。"} <button className="insights-action" onClick={() => setReloadNonce((n) => n + 1)}>重试</button></p>}

      {phase === "ready" && payload && (
        <>
          <p className="refined-meta">
            {payload.theme || "未命名主题"}
            {payload.source === "fallback" && payload.fallback_reason
              ? ` · 精修未生成（${payload.fallback_reason}），已回退原始纠错转写`
              : ""}
            {payload.prompt_version ? ` · ${payload.prompt_version}` : ""}
          </p>
          <div className="refined-table-wrap">
            <table className="refined-table">
              <thead>
                <tr><th>#</th><th>时间码</th><th>角色</th><th>上屏字幕</th></tr>
              </thead>
              <tbody>
                {payload.rows.length === 0 && (
                  <tr><td className="refined-empty" colSpan={4}>该切片没有可展示的精修行</td></tr>
                )}
                {payload.rows.map((row) => (
                  <tr key={`${row.index}-${row.text}`}>
                    <td>{row.index}</td>
                    <td className="refined-time">
                      {row.start_ms === null || row.end_ms === null
                        ? "—"
                        : `${formatRelativeTimecode(row.start_ms)}–${formatRelativeTimecode(row.end_ms)}`}
                    </td>
                    <td className={`refined-speaker ${row.speaker === "医生" ? "doctor" : row.speaker === "患者" || row.speaker === "家属" ? "patient" : ""}`}>
                      {row.speaker || "未标注"}
                    </td>
                    <td className="refined-text">{row.text}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <a className="insights-action refined-download" href={copyUrl} download target="_blank" rel="noreferrer">下载精修文案（markdown）</a>
          {(payload.checklist_pending.length > 0 || payload.checklist_compliance.length > 0
            || payload.ending_cards.length > 0 || payload.warnings.length > 0) && (
            <div className="refined-fold">
              {payload.checklist_pending.length > 0 && (
                <details><summary>待核对清单（{payload.checklist_pending.length}）</summary>
                  <ul>{payload.checklist_pending.map((row, i) => <li key={i}>{row}</li>)}</ul></details>
              )}
              {payload.checklist_compliance.length > 0 && (
                <details><summary>合规处理清单（{payload.checklist_compliance.length}）</summary>
                  <ul>{payload.checklist_compliance.map((row, i) => <li key={i}>{row}</li>)}</ul></details>
              )}
              {payload.ending_cards.length > 0 && (
                <details><summary>结尾文字卡建议（{payload.ending_cards.length}）</summary>
                  <ul>{payload.ending_cards.map((row, i) => <li key={i}>{row}</li>)}</ul></details>
              )}
              {payload.warnings.length > 0 && (
                <details className="refined-warnings"><summary>程序留痕（{payload.warnings.length}）</summary>
                  <ul>{payload.warnings.map((row, i) => <li key={i}>{row}</li>)}</ul></details>
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
}
