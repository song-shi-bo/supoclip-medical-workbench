"use client";

// 转写标签页（契约 §3.2 / §5 / §6）—— 真实编辑能力，不是静态标签：
//  * 关键词搜索（命中高亮 + 计数 n/m + 上一个/下一个）；
//  * 点分段 → 播放器 seek 到该段起点；
//  * 改文本 / 改说话人 → POST transcript/revisions（显示 修订 vN）；
//  * 修订历史 + 回滚（POST transcript/rollback）；
//  * 三态切换 原始稿 / 纠错稿 / 有效稿（显示与搜索都切到该变体）；
//  * 明确提示：**原始 ASR 不会被覆盖**（原始/纠错不可变，改动以修订叠加）。
//
// 端点未种子化（404 / 未登记）时只读显示页面已加载的成片字幕，不伪造文档/分段ID；
// 成功读到真实修订文档后才开放编辑。

import { AlertTriangle, ChevronLeft, ChevronRight, History, LoaderCircle, RotateCcw, Search, Save, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { addTranscriptRevision, errorText, getTranscript, InspectorApiError, rollbackTranscript } from "./api";
import { SaveBadge } from "./save-state";
import {
  IDLE_SAVE,
  SPEAKER_LABELS,
  type SaveState,
  type SpeakerLabel,
  type TranscriptDocument,
  type TranscriptFallback,
  type TranscriptSegment,
  type TranscriptVariant,
} from "./types";

export type TranscriptTabProps = {
  projectId: string;
  clipId: string;
  baseUrl: string;
  /** 页面已加载的分段（成片 segments）：端点 404 时的只读兜底。 */
  fallbackSegments: TranscriptSegment[];
  /** 页面 seek（点分段跳转播放头）。 */
  onSeek: (seconds: number) => void;
  /** 修订成功后通知外壳：字幕标签要重新读有效稿。 */
  onChanged?: (info: { revision: number }) => void;
  /** 有效稿的分段也回报给页面（时间线字幕轨/叠加层数据来源）。 */
  onEffectiveSegments?: (segments: TranscriptSegment[]) => void;
};

const VARIANTS: { id: TranscriptVariant; label: string; hint: string }[] = [
  { id: "raw", label: "原始稿", hint: "原始 ASR 稿：只读，永不被覆盖。" },
  { id: "corrected", label: "纠错稿", hint: "纠错稿：只读基线（第一次保存修订时冻结当时的有效稿）。" },
  { id: "effective", label: "有效稿", hint: "有效稿 = 纠错稿（无则原始稿）叠加已批准修订；编辑与字幕预览都用它。" },
];

/** 变体 → 实际分段（端点带 view 参数时优先用它返回的 segments）。 */
export function segmentsForVariant(doc: TranscriptDocument | null, variant: TranscriptVariant): TranscriptSegment[] {
  if (!doc) return [];
  if (variant === "raw") return doc.raw_segments || [];
  if (variant === "corrected") return doc.corrected_segments || [];
  return doc.effective_segments || doc.segments || [];
}

export function countHits(segments: TranscriptSegment[], keyword: string): number {
  const needle = keyword.trim();
  if (!needle) return 0;
  let total = 0;
  segments.forEach((segment) => {
    const haystack = (segment.text || "").toLowerCase();
    const lower = needle.toLowerCase();
    let index = haystack.indexOf(lower);
    while (index !== -1) {
      total += 1;
      index = haystack.indexOf(lower, index + lower.length);
    }
  });
  return total;
}

/** 高亮片段：把命中的子串切成 <mark>，active 标记当前命中。 */
function highlight(text: string, keyword: string, activeOrdinal: number, startOrdinal: number) {
  const needle = keyword.trim();
  if (!needle) return text;
  const lowerText = text.toLowerCase();
  const lower = needle.toLowerCase();
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  let ordinal = startOrdinal;
  let index = lowerText.indexOf(lower);
  let key = 0;
  while (index !== -1) {
    if (index > cursor) parts.push(text.slice(cursor, index));
    parts.push(<mark key={`hit-${key}`} className={ordinal === activeOrdinal ? "active" : ""} data-hit={ordinal}>{text.slice(index, index + needle.length)}</mark>);
    key += 1;
    ordinal += 1;
    cursor = index + needle.length;
    index = lowerText.indexOf(lower, cursor);
  }
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts;
}

export function TranscriptTab({
  projectId, clipId, baseUrl, fallbackSegments, onSeek, onChanged, onEffectiveSegments,
}: TranscriptTabProps) {
  const [doc, setDoc] = useState<TranscriptDocument | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [fallback, setFallback] = useState<TranscriptFallback | null>(null);
  const [variant, setVariant] = useState<TranscriptVariant>("effective");
  const [keyword, setKeyword] = useState("");
  const [hitIndex, setHitIndex] = useState(0);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftText, setDraftText] = useState("");
  const [save, setSave] = useState<SaveState>(IDLE_SAVE);
  const [speakerSave, setSpeakerSave] = useState<SaveState>(IDLE_SAVE);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [rollbackError, setRollbackError] = useState<string | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const fallbackSegmentsRef = useRef(fallbackSegments);
  fallbackSegmentsRef.current = fallbackSegments;

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      setLoadError(null);
      setDoc(null);
      setFallback(null);
      try {
        const next = await getTranscript(baseUrl, projectId, clipId, variant);
        if (cancelled || !mountedRef.current) return;
        setDoc(next);
        setLoadError(null);
        setFallback(null);
      } catch (error) {
        if (cancelled || !mountedRef.current) return;
        // 只有缺基线允许只读字幕展示；403与其它422仍显示真实错误。
        setDoc(null);
        setLoadError(errorText(error));
        const missingBaseline = error instanceof InspectorApiError && (
          error.status === 404 || (error.status === 422 && error.code === "E_TRANSCRIPT_NOT_SEEDED")
        );
        setFallback(missingBaseline ? { segments: fallbackSegmentsRef.current, reason: errorText(error) } : null);
      } finally {
        if (!cancelled && mountedRef.current) setLoading(false);
      }
    };
    void load();
    return () => { cancelled = true; };
  }, [baseUrl, projectId, clipId, variant]);

  // SRT没有修订分段ID，仍可只读显示；编辑只消费真实文档里的segment_id。
  const visibleFallback = fallback ? fallbackSegments : null;
  const segments: TranscriptSegment[] = useMemo(
    () => doc
      ? segmentsForVariant(doc, variant).filter((segment) => Boolean(segment.segment_id))
      : (visibleFallback || []),
    [doc, variant, visibleFallback],
  );
  const readOnly = variant !== "effective" || !doc;
  const correctedMissing = variant === "corrected" && doc !== null && !(doc.corrected_segments || []).length;

  const effectiveSegments = useMemo(
    () => (doc ? segmentsForVariant(doc, "effective") : (visibleFallback || [])).filter((segment) => Boolean(segment.segment_id)),
    [doc, visibleFallback],
  );

  // 回调放进 ref：外层用内联 lambda 时不应因为身份变化而重复上报。
  const effectiveReportRef = useRef(onEffectiveSegments);
  effectiveReportRef.current = onEffectiveSegments;
  useEffect(() => {
    effectiveReportRef.current?.(effectiveSegments);
  }, [effectiveSegments]);

  const totalHits = useMemo(() => countHits(segments, keyword), [segments, keyword]);

  const hitsPerSegment = useMemo(() => {
    let running = 0;
    return segments.map((segment) => {
      const count = countHits([segment], keyword);
      const start = running;
      running += count;
      return { start, count };
    });
  }, [segments, keyword]);

  useEffect(() => { setHitIndex(0); }, [keyword, variant]);

  const activeOrdinal = totalHits ? hitIndex % totalHits : 0;

  const jump = (direction: -1 | 1) => {
    if (!totalHits) return;
    const next = (hitIndex + direction + totalHits) % totalHits;
    setHitIndex(next);
    const target = segments.find((segment, index) => {
      const bucket = hitsPerSegment[index];
      return bucket.count > 0 && next >= bucket.start && next < bucket.start + bucket.count;
    });
    if (target) onSeek(target.start);
  };

  const saveRevision = async (changes: Parameters<typeof addTranscriptRevision>[3]["changes"], reason: string): Promise<TranscriptDocument | null> => {
    setSave({ status: "saving" });
    try {
      const next = await addTranscriptRevision(baseUrl, projectId, clipId, { changes, author: "editor", reason });
      if (mountedRef.current) {
        setDoc(next);
        setFallback(null);
        setLoadError(null);
        setSave({ status: "saved", message: `修订 v${next.approved_revision ?? next.transcript_revision}`, revision: next.approved_revision ?? next.transcript_revision });
        setVariant((current) => (current === "raw" ? "effective" : current));
        onChanged?.({ revision: next.approved_revision ?? next.transcript_revision });
      }
      return mountedRef.current ? next : null;
    } catch (error) {
      if (mountedRef.current) setSave({ status: "error", message: errorText(error) });
      return null;
    }
  };

  const commitText = async (segment: TranscriptSegment & { segment_id: string }) => {
    const after = draftText.trim();
    const before = segment.text;
    if (!after || after === before) {
      setEditingId(null);
      return;
    }
    const next = await saveRevision(
      [{ segment_id: segment.segment_id, before, after }],
      "手动纠错",
    );
    if (next) setEditingId(null);
  };

  const commitSpeaker = async (segment: TranscriptSegment & { segment_id: string }, speaker: SpeakerLabel) => {
    const before = (segment.speaker || "未标注") as string;
    if (before === speaker) return;
    setSpeakerSave({ status: "saving" });
    try {
      const next = await addTranscriptRevision(baseUrl, projectId, clipId, {
        author: "editor",
        reason: "说话人标注",
        changes: [{ segment_id: segment.segment_id, speaker_before: before, speaker_after: speaker }],
      });
      if (mountedRef.current) {
        setDoc(next);
        setFallback(null);
        setSpeakerSave({ status: "saved", message: `修订 v${next.approved_revision ?? next.transcript_revision}`, revision: next.approved_revision ?? next.transcript_revision });
        setSave({ status: "saved", message: `修订 v${next.approved_revision ?? next.transcript_revision}`, revision: next.approved_revision ?? next.transcript_revision });
        onChanged?.({ revision: next.approved_revision ?? next.transcript_revision });
      }
    } catch (error) {
      if (mountedRef.current) setSpeakerSave({ status: "error", message: errorText(error) });
    }
  };

  const doRollback = async (revision: number) => {
    setRollbackError(null);
    setSave({ status: "saving" });
    try {
      const next = await rollbackTranscript(baseUrl, projectId, clipId, revision);
      if (mountedRef.current) {
        setDoc(next);
        setSave({ status: "saved", message: `已回滚到 v${revision}`, revision });
        onChanged?.({ revision });
      }
    } catch (error) {
      if (mountedRef.current) {
        setSave({ status: "error", message: errorText(error) });
        setRollbackError(errorText(error));
      }
    }
  };

  return <div className="inspector-transcript" data-testid="inspector-transcript-tab">
    <p className="inspector-notice" data-testid="inspector-asr-notice">
      <AlertTriangle size={13} />原始 ASR 不会被覆盖：原始稿与纠错稿只读，所有修改以修订（revision）叠加，可随时回滚。
    </p>

    <div className="inspector-row" role="group" aria-label="转写稿件变体">
      {VARIANTS.map((item) => <button
        key={item.id}
        type="button"
        className={`inspector-chip${variant === item.id ? " active" : ""}`}
        aria-pressed={variant === item.id}
        data-testid={`inspector-variant-${item.id}`}
        title={item.hint}
        onClick={() => setVariant(item.id)}
      >{item.label}</button>)}
      <span className="inspector-metadata" data-testid="inspector-transcript-revision">
        修订 v{doc?.approved_revision ?? doc?.transcript_revision ?? 0}
      </span>
    </div>

    <label className="inspector-search" title="在当前稿件变体里搜索（命中会高亮，计数为 当前/总数）">
      <Search size={14} />
      <input
        aria-label="搜索转写内容"
        placeholder="搜索转写内容…"
        value={keyword}
        onChange={(event) => setKeyword(event.target.value)}
      />
      <span className="inspector-hit-count" data-testid="inspector-hit-count">
        {totalHits ? `${Math.min(hitIndex + 1, totalHits)}/${totalHits}` : keyword.trim() ? "0/0" : ""}
      </span>
      <button type="button" aria-label="上一个命中" disabled={!totalHits} onClick={() => jump(-1)}><ChevronLeft size={13} /></button>
      <button type="button" aria-label="下一个命中" disabled={!totalHits} onClick={() => jump(1)}><ChevronRight size={13} /></button>
    </label>

    <div className="inspector-row inspector-row-actions">
      <SaveBadge state={save} />
      <SaveBadge state={speakerSave} />
      <button
        type="button"
        className="inspector-link"
        aria-expanded={historyOpen}
        data-testid="inspector-toggle-history"
        onClick={() => setHistoryOpen((value) => !value)}
      ><History size={13} />修订历史（{(doc?.revisions || []).length}）</button>
    </div>

    {historyOpen && <section className="inspector-history" aria-label="修订历史" data-testid="inspector-transcript-history">
      {!(doc?.revisions || []).length && <p className="inspector-hint">暂无修订（改动会以 v1 起逐条追加，原始稿不变）。</p>}
      <ul>
        {(doc?.revisions || []).map((revision) => <li key={revision.revision} data-testid={`inspector-revision-${revision.revision}`}>
          <header>
            <b>v{revision.revision}</b>
            <span>{revision.author || "editor"} · {revision.reason || "手动调整"}</span>
            <button
              type="button"
              className="inspector-link"
              aria-label={`回滚到 v${revision.revision}`}
              onClick={() => void doRollback(revision.revision)}
            ><RotateCcw size={12} />回滚</button>
          </header>
          {revision.changes?.map((change, index) => <p key={`${revision.revision}-${change.segment_id}-${index}`}>
            <em>{change.segment_id}</em>
            {change.before !== undefined && <span className="before">{change.before}</span>}
            {change.after !== undefined && <span className="after">{change.after}</span>}
            {change.speaker_after !== undefined && <span className="after">说话人 → {change.speaker_after}</span>}
          </p>)}
        </li>)}
      </ul>
    </section>}

    {rollbackError && <p className="inspector-error" role="alert">{rollbackError}</p>}

    {loading && <p className="inspector-hint"><LoaderCircle size={13} />正在读取转写修订…</p>}

    {!loading && loadError && <p className="inspector-error" role="alert" data-testid="inspector-transcript-fallback">
      {fallback
        ? "当前显示成片字幕；本成片尚未建立转写文档，尚无可编辑的成片修订记录；请在源片的核听与人工修订入口编辑。"
        : `读取转写失败：${loadError}`}
    </p>}

    {!loading && correctedMissing && <p className="inspector-hint" data-testid="inspector-corrected-missing">
      该成片还没有独立纠错稿：纠错稿会在第一次保存修订时以当时的有效稿冻结为只读基线。
    </p>}

    <div className="inspector-segments">
      {!segments.length && !loading && <p className="inspector-hint">没有可显示的分段（该变体为空或尚未转写）。</p>}
      {segments.map((segment, index) => {
        const bucket = hitsPerSegment[index];
        const activeStart = totalHits ? activeOrdinal : -1;
        const editableSegment = doc && segment.segment_id ? { ...segment, segment_id: segment.segment_id } : null;
        const editing = !readOnly && editableSegment !== null && editingId === editableSegment.segment_id;
        return <article
          key={segment.segment_id || `${segment.start}-${index}`}
          className="inspector-segment"
          data-testid={segment.segment_id ? `inspector-segment-${segment.segment_id}` : `inspector-subtitle-${index}`}
          onClick={() => onSeek(segment.start)}
        >
          <header>
            <time>{formatClock(segment.start)}</time>
            <select
              aria-label={`说话人 ${segment.segment_id || `字幕${index + 1}`}`}
              value={segment.speaker || "未标注"}
              disabled={readOnly || !editableSegment}
              onClick={(event) => event.stopPropagation()}
              onChange={(event) => { if (editableSegment) void commitSpeaker(editableSegment, event.target.value as SpeakerLabel); }}
            >
              {SPEAKER_LABELS.map((label) => <option key={label} value={label}>{label}</option>)}
            </select>
            {!readOnly && editableSegment && !editing && <button
              type="button"
              className="inspector-link"
              aria-label={`编辑分段 ${segment.segment_id}`}
              onClick={(event) => { event.stopPropagation(); setEditingId(editableSegment.segment_id); setDraftText(editableSegment.text); }}
            >编辑</button>}
          </header>
          {editing ? <div className="inspector-segment-edit" onClick={(event) => event.stopPropagation()}>
            <textarea
              aria-label={`分段文本 ${segment.segment_id}`}
              value={draftText}
              onChange={(event) => setDraftText(event.target.value)}
            />
            <div className="inspector-row">
              <button type="button" className="inspector-primary" disabled={save.status === "saving"} onClick={() => { if (editableSegment) void commitText(editableSegment); }}>
                {save.status === "saving" ? <LoaderCircle size={12} /> : <Save size={12} />}保存修订</button>
              <button type="button" onClick={() => setEditingId(null)}><X size={12} />取消</button>
            </div>
          </div> : <p className="inspector-segment-text">
            {highlight(segment.text, keyword, activeStart, bucket.start)}
          </p>}
        </article>;
      })}
    </div>
  </div>;
}

export function formatClock(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds || 0));
  return [Math.floor(total / 3600), Math.floor((total % 3600) / 60), total % 60].map((value) => String(value).padStart(2, "0")).join(":");
}
