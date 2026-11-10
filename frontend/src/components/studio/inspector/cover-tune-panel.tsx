"use client";

/**
 * 封面微调面板（2026-11-09 P3）：候选帧「换一帧」+ 候选文案点选。
 *
 * - 候选帧：GET …/cover/frame-candidates（成片时间线，懒建清单），点缩略图 →
 *   POST …/cover {frame_time_rel_s}（revision+1、重烧 0.8s 片头，可回滚）；
 * - 候选文案：GET …/cover/copy-candidates（cover_copy.json 的候选+评分报告），
 *   点选 → POST …/cover {copy_override:{topic,hook,segment_ids}}；
 * - 每次 POST 都是显式重制（后端 revision 机制），成功后回调 onReplaced 让页面
 *   刷新封面版本；本组件不本地合成、不缓存写操作。
 */

import { useCallback, useState } from "react";
import { LoaderCircle, RefreshCw } from "lucide-react";

type FrameCandidate = {
  file: string;
  time_rel_s: number;
  score: number;
  face_count?: number;
  brightness_mean?: number | null;
  selected?: boolean;
  thumb_url: string;
};

type CopyCandidate = {
  topic?: string;
  hook?: string;
  segment_ids?: string[];
  source?: string;
};

const BASE = "/video-api/studio/auto-edit";

export type CoverTunePanelProps = {
  /** 成片所属的渲染任务 id（task_id）；null = 未选中成片，面板不渲染。 */
  taskId: string | null;
  clipId: string | null;
  disabled?: boolean;
  /** 重制成功（revision+1）回调：页面刷新封面版本/历史并提示。 */
  onReplaced?: (revision: number | null, note: string) => void;
};

export function CoverTunePanel({ taskId, clipId, disabled, onReplaced }: CoverTunePanelProps) {
  const [frames, setFrames] = useState<FrameCandidate[] | null>(null);
  const [framesLoading, setFramesLoading] = useState(false);
  const [copies, setCopies] = useState<CopyCandidate[] | null>(null);
  const [currentTopic, setCurrentTopic] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const active = Boolean(taskId && clipId) && !disabled;

  const loadFrames = useCallback(async () => {
    if (!taskId || !clipId) return;
    setFramesLoading(true);
    setError(null);
    try {
      const response = await fetch(`${BASE}/${taskId}/clips/${encodeURIComponent(clipId)}/cover/frame-candidates`, { cache: "no-store" });
      if (!response.ok) {
        const detail = await response.json().catch(() => null) as { detail?: { message?: string } | string } | null;
        const message = typeof detail?.detail === "object" && detail.detail !== null
          ? detail.detail.message
          : (typeof detail?.detail === "string" ? detail.detail : null);
        throw new Error(message || `候选帧读取失败（HTTP ${response.status}）`);
      }
      const payload = await response.json() as { frames?: FrameCandidate[] };
      setFrames(payload.frames || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "候选帧读取失败");
    } finally {
      setFramesLoading(false);
    }
  }, [taskId, clipId]);

  const loadCopies = useCallback(async () => {
    if (!taskId || !clipId) return;
    setError(null);
    try {
      const response = await fetch(`${BASE}/${taskId}/clips/${encodeURIComponent(clipId)}/cover/copy-candidates`, { cache: "no-store" });
      if (!response.ok) throw new Error(`文案候选读取失败（HTTP ${response.status}）`);
      const payload = await response.json() as { final?: { topic?: string }; candidates?: CopyCandidate[] };
      setCopies(payload.candidates || []);
      setCurrentTopic(payload.final?.topic ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "文案候选读取失败");
    }
  }, [taskId, clipId]);

  const replaceCover = useCallback(async (body: Record<string, unknown>, label: string) => {
    if (!taskId || !clipId) return;
    setBusyAction(label);
    setError(null);
    try {
      const response = await fetch(`${BASE}/${taskId}/clips/${encodeURIComponent(clipId)}/cover`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...body, replace_intro: true }),
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => null) as { detail?: { message?: string } | string } | null;
        const message = typeof detail?.detail === "object" && detail.detail !== null
          ? detail.detail.message
          : (typeof detail?.detail === "string" ? detail.detail : null);
        throw new Error(message || `封面重制失败（HTTP ${response.status}）`);
      }
      const payload = await response.json() as { cover_revision?: number };
      onReplaced?.(payload.cover_revision ?? null, label);
      if (frames !== null) void loadFrames();
    } catch (err) {
      setError(err instanceof Error ? err.message : "封面重制失败");
    } finally {
      setBusyAction(null);
    }
  }, [taskId, clipId, frames, loadFrames, onReplaced]);

  if (!taskId || !clipId) return null;

  return <section className="inspector-block" aria-label="封面微调" data-testid="cover-tune-panel">
    <header><strong>封面微调</strong><span className="inspector-phase2" title="换帧/换文案都是重制封面（版本+1），可在历史里回滚">点选即重制，可回滚</span></header>

    <div className="inspector-row" role="group" aria-label="换一帧">
      <span className="inspector-label">换一帧</span>
      {frames === null
        ? <button type="button" className="inspector-chip" disabled={!active || framesLoading}
            data-testid="cover-tune-load-frames"
            title="从这条成片里抽 8 张候选帧（确定性评分），点缩略图即换封面帧"
            onClick={() => void loadFrames()}>
            {framesLoading ? <LoaderCircle size={12} /> : <RefreshCw size={12} />}加载候选帧
          </button>
        : <div className="cover-tune-frames" data-testid="cover-tune-frames">
            {frames.map((frame) => (
              <button key={frame.file} type="button"
                  className={`cover-tune-frame${frame.selected ? " active" : ""}`}
                  disabled={!active || busyAction !== null}
                  data-testid={`cover-tune-frame-${frame.file}`}
                  title={`第 ${frame.time_rel_s.toFixed(1)}s · 评分 ${frame.score} · ${frame.face_count ?? 0} 张脸`}
                  onClick={() => void replaceCover({ frame_time_rel_s: frame.time_rel_s }, `换帧 ${frame.time_rel_s.toFixed(1)}s`)}>
                <img src={frame.thumb_url} alt={`候选帧 ${frame.time_rel_s.toFixed(1)}s`} />
                <small>{frame.time_rel_s.toFixed(1)}s{frame.selected ? " ·当前" : ""}</small>
              </button>
            ))}
            <button type="button" className="inspector-link" disabled={!active || framesLoading}
                data-testid="cover-tune-reload-frames" title="重新抽帧评分"
                onClick={() => void loadFrames()}>
              {framesLoading ? <LoaderCircle size={12} /> : <RefreshCw size={12} />}
            </button>
          </div>}
    </div>

    <div className="inspector-row" role="group" aria-label="换文案">
      <span className="inspector-label">换文案</span>
      {copies === null
        ? <button type="button" className="inspector-chip" disabled={!active}
            data-testid="cover-tune-load-copies"
            title="查看上次封面生成时的全部候选文案与评分，点选即重制"
            onClick={() => void loadCopies()}>
            加载候选文案
          </button>
        : <div className="cover-tune-copies" data-testid="cover-tune-copies">
            {copies.length === 0 && <em className="inspector-hint">没有候选文案（该封面可能来自人工指定）</em>}
            {copies.map((candidate, index) => {
              const label = `${candidate.topic ?? ""}｜${candidate.hook ?? ""}`;
              const isCurrent = candidate.topic && candidate.topic === currentTopic;
              return <button key={`${candidate.topic}-${index}`} type="button"
                  className={`inspector-chip${isCurrent ? " active" : ""}`}
                  disabled={!active || busyAction !== null}
                  data-testid={`cover-tune-copy-${index}`}
                  title={`${label}${candidate.source ? `（来源 ${candidate.source}）` : ""}`}
                  onClick={() => void replaceCover({
                    copy_override: {
                      topic: candidate.topic ?? "",
                      hook: candidate.hook ?? "",
                      segment_ids: candidate.segment_ids ?? [],
                    },
                  }, `换文案 ${label}`)}>
                {label}{isCurrent ? " ·当前" : ""}
              </button>;
            })}
          </div>}
    </div>

    {busyAction && <p className="inspector-hint" role="status" data-testid="cover-tune-busy">
      <LoaderCircle size={12} />正在重制封面（{busyAction}）…
    </p>}
    {error && <p className="inspector-error" role="alert" data-testid="cover-tune-error">{error}</p>}
  </section>;
}
