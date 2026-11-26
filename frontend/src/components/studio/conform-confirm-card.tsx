/**
 * 对稿成片人工确认卡（P2b）：任务停在 conform_ready 时渲染逐句 diff +
 * 精剪决策摘要，医生/策划确认后触发渲染。
 *
 * 数据契约：GET /api/jobs/conform/{job_id}/report（alignment + edl + 审计）；
 * 确认：POST /api/jobs/conform/{job_id}/approve（conform_ready → queued 续跑）。
 * 确认后组件自带轻量轮询，成片完成即展示结果，不阻塞页面其他区域。
 */
import { useCallback, useEffect, useRef, useState } from "react";

type Sentence = {
  index: number;
  status: "matched" | "missing";
  text: string;
  start_ms: number | null;
  end_ms: number | null;
};

type Decision = {
  type: string;
  start_ms: number;
  end_ms: number;
  action: "cut" | "kept";
  reason: string;
  text?: string;
};

type ConformReport = {
  job_id: string;
  job_status: string;
  conform_approved: boolean;
  alignment: {
    coverage_ratio: number;
    script_sentences: { total: number; matched: number; missing: number[] };
    sentences: Sentence[];
    extra_segments: Array<Record<string, unknown>>;
    warnings: string[];
    review?: { engine: string; verdicts: Array<Record<string, unknown>>; warnings: string[] };
  };
  edl: {
    output_duration_ms: number;
    cut_ratio: number;
    redline: { protected_segments: number };
    stats: Record<string, number>;
    warnings: string[];
    review_engine: string | null;
  } | null;
};

type Phase = "loading" | "ready" | "approving" | "rendering" | "done" | "error";

const STATUS_TEXT: Record<string, string> = {
  queued: "已确认，排队中",
  downloading: "渲染中（准备源片）",
  rendering: "渲染中",
  conform_ready: "等待确认",
  completed: "成片已完成",
  completed_with_warnings: "成片已完成（有警告）",
  technical_failed: "渲染失败",
  failed: "渲染失败",
};

export default function ConformConfirmCard({ jobId, onApproved }: {
  jobId: string;
  onApproved?: () => void;
}) {
  const [report, setReport] = useState<ConformReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [phase, setPhase] = useState<Phase>("loading");
  const [jobStatus, setJobStatus] = useState<string>("");
  const approvedRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    fetch(`/video-api/api/jobs/conform/${encodeURIComponent(jobId)}/report`, { cache: "no-store" })
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`);
        return r.json() as Promise<ConformReport>;
      })
      .then((data) => {
        if (!cancelled) {
          setReport(data);
          setPhase("ready");
        }
      })
      .catch((err: Error) => {
        if (!cancelled) {
          setError(err.message);
          setPhase("error");
        }
      });
    return () => { cancelled = true; };
  }, [jobId]);

  const approve = useCallback(async () => {
    if (approvedRef.current) return;
    approvedRef.current = true;
    setPhase("approving");
    try {
      const r = await fetch(`/video-api/api/jobs/conform/${encodeURIComponent(jobId)}/approve`,
        { method: "POST" });
      if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`);
      setPhase("rendering");
      setJobStatus("queued");
      onApproved?.();
      // 轻量轮询：确认后 worker 渲染单条成片，完成即止（最多 ~3 分钟）
      for (let i = 0; i < 45; i += 1) {
        await new Promise((resolve) => setTimeout(resolve, 4000));
        const poll = await fetch(`/video-api/api/jobs/${encodeURIComponent(jobId)}`,
          { cache: "no-store" });
        if (!poll.ok) continue;
        const doc = await poll.json() as { status?: string };
        const status = doc.status || "";
        setJobStatus(status);
        if (status === "completed" || status === "completed_with_warnings") {
          setPhase("done");
          return;
        }
        if (status === "failed" || status === "technical_failed") {
          setPhase("error");
          setError("渲染失败，请查看任务详情");
          return;
        }
      }
      setPhase("rendering"); // 超时仍在渲染：保留状态展示
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
      setPhase("error");
      approvedRef.current = false;
    }
  }, [jobId, onApproved]);

  if (phase === "loading") {
    return <section className="auto-edit-panel" aria-label="对稿成片确认"><p>正在加载对稿报告…</p></section>;
  }
  if (phase === "error" && !report) {
    return <section className="auto-edit-panel" aria-label="对稿成片确认">
      <p role="alert">对稿报告加载失败：{error}</p>
    </section>;
  }
  const alignment = report?.alignment;
  const edl = report?.edl;
  const coveragePct = alignment ? Math.round((alignment.coverage_ratio || 0) * 100) : 0;
  const missing = alignment?.script_sentences?.missing || [];

  return (
    <section className="auto-edit-panel" aria-label="对稿成片确认">
      <header><strong>对稿成片 · 待确认</strong>
        <span>{edl ? `预计成片 ${Math.round((edl.output_duration_ms || 0) / 1000)}s` : ""}</span></header>
      {alignment && (
        <p>文稿覆盖率 <strong>{coveragePct}%</strong>（{alignment.script_sentences.matched}/{alignment.script_sentences.total} 句）
          · 剪除率 <strong>{Math.round((edl?.cut_ratio || 0) * 100)}%</strong>
          · 红线保护段 <strong>{edl?.redline?.protected_segments ?? 0}</strong>
          · 分级 {edl?.review_engine || "local-rule"}</p>
      )}
      {phase === "rendering" && <p>已确认，正在渲染单条精剪成片…（{STATUS_TEXT[jobStatus] || jobStatus}）</p>}
      {phase === "done" && <p>成片已完成，可在播放器中预览。</p>}
      {phase === "error" && error && <p role="alert">{error}</p>}
      {alignment && (
        <ol>
          {alignment.sentences.map((s) => (
            <li key={s.index}
              style={{ color: s.status === "matched" ? "#9fd6a0" : "#c9c3b8" }}>
              {s.status === "matched" ? "✓" : "✗ 缺拍（建议补拍）"}：{s.text}
            </li>
          ))}
        </ol>
      )}
      {edl && (
        <details>
          <summary>精剪决策账（{edl.stats ? Object.entries(edl.stats).filter(([, v]) => v > 0).map(([k, v]) => `${k} ${v}`).join("，") || "无剪除" : ""}）</summary>
          {(edl.decisions || []).map((d: Decision, i: number) => (
            <p key={i} style={{ color: d.action === "cut" ? "#e0b34d" : "#9fd6a0" }}>
              [{d.action === "cut" ? "剪" : "留"}] {d.type} {d.start_ms}–{d.end_ms}ms：{d.reason}
            </p>
          ))}
          {missing.length > 0 && <p>缺句清单：第 {missing.join("、")} 句（可补拍后重跑）</p>}
          {(edl.warnings || []).map((w, i) => <p key={`w${i}`}>{w}</p>)}
        </details>
      )}
      {phase === "ready" && (
        <button className="insights-action" onClick={() => void approve()}>确认渲染</button>
      )}
    </section>
  );
}
