"use client";

import { ChangeEvent, CSSProperties, DragEvent, PointerEvent as ReactPointerEvent, useEffect, useMemo, useRef, useState } from "react";
import { AudioLines, Captions, Check, ChevronDown, Download, Eye, Film, FolderOpen, ImageIcon, Layers3, ListVideo, LoaderCircle, LockKeyhole, Menu, Music2, Pause, Play, Plus, Redo2, Save, Scissors, Search, Settings2, SlidersHorizontal, Trash2, Type, Undo2, Upload, Volume2, X, ZoomIn, ZoomOut } from "lucide-react";
import "./studio.css";

type SpeakerRole = "未标注" | "医生" | "患者";
type Segment = { start: number; end: number; text: string; speaker?: SpeakerRole };
type TimelineRange = { id: string; source_start: number; source_end: number };
type TimelineProject = { id: string; timeline: { ranges: TimelineRange[] }; artifacts: { edl: string; srt: string; fcpxml: string } };
type GoldenSentence = { start: number; end: number; text: string; reason: string };
type StudioInsights = { golden_sentences: GoldenSentence[]; keywords: string[]; engine: "llm" | "local-fallback"; notice?: string | null };
type RenderedClip = {
  task_id: string;
  clip_id: string;
  name: string;
  topic: string;
  source_start: number;
  source_end: number;
  duration: number;
  video_path: string;
  cover_path?: string | null;
  segments: Segment[];
};
type AutoEditTask = {
  task_id: string;
  status: "queued" | "running" | "completed" | "failed";
  stage: string;
  progress: number;
  error?: string;
  artifacts: { name: string; path: string; size_bytes: number }[];
  clips?: RenderedClip[];
};
type SourceWorkspace = { previewUrl: string; duration: number; segments: Segment[]; timeline: TimelineProject | null; insights: StudioInsights | null; previewRotation: 0 | 90 | -90 | 180 };
type TimelinePointer = { clientX: number; currentTarget: HTMLDivElement };
type TimelineLanePointer = { clientX: number; currentTarget: HTMLElement };

/**
 * Phone recordings often store a landscape-sized pixel buffer together with a
 * 90° display matrix. Browsers honour that matrix while laying the video out
 * from the raw dimensions, which can crop the top and bottom in a wide editor
 * preview. Read the small MP4 header/tail only — never the whole source file —
 * so the preview box can use the displayed orientation.
 */
async function readMp4Rotation(file: File): Promise<0 | 90 | -90 | 180> {
  // Walk the top-level MP4 atoms first. `moov` can live after a multi-gigabyte
  // `mdat`, so probing just the file head/tail is not reliable.
  let atomOffset = 0;
  while (atomOffset + 8 <= file.size) {
    const header = new DataView(await file.slice(atomOffset, atomOffset + 16).arrayBuffer());
    const declaredSize = header.getUint32(0);
    const atomType = String.fromCharCode(header.getUint8(4), header.getUint8(5), header.getUint8(6), header.getUint8(7));
    const atomSize = declaredSize === 1 && header.byteLength >= 16
      ? Number(header.getBigUint64(8))
      : declaredSize === 0 ? file.size - atomOffset : declaredSize;
    if (!Number.isFinite(atomSize) || atomSize < 8 || atomOffset + atomSize > file.size) break;
    if (atomType === "moov") {
      // A movie header is tiny compared with the source media. Keep a strict
      // ceiling so merely importing a large video never reads the video payload.
      if (atomSize > 16 * 1024 * 1024) return 0;
      const bytes = new Uint8Array(await file.slice(atomOffset, atomOffset + atomSize).arrayBuffer());
      for (let offset = 0; offset <= bytes.length - 48; offset += 1) {
      if (bytes[offset] !== 0x74 || bytes[offset + 1] !== 0x6b || bytes[offset + 2] !== 0x68 || bytes[offset + 3] !== 0x64) continue;
      const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      const dataStart = offset + 4;
      const version = view.getUint8(dataStart);
      const matrixStart = dataStart + (version === 1 ? 52 : 40);
      if (matrixStart + 20 > bytes.length) continue;
      const a = view.getInt32(matrixStart);
      const b = view.getInt32(matrixStart + 4);
      const c = view.getInt32(matrixStart + 12);
      const d = view.getInt32(matrixStart + 16);
      const unit = 65536;
      if (Math.abs(a) < unit / 8 && Math.abs(d) < unit / 8 && b > unit / 2 && c < -unit / 2) return 90;
      if (Math.abs(a) < unit / 8 && Math.abs(d) < unit / 8 && b < -unit / 2 && c > unit / 2) return -90;
      if (a < -unit / 2 && d < -unit / 2) return 180;
      }
      return 0;
    }
    atomOffset += atomSize;
  }
  return 0;
}

const formatTime = (seconds: number) => {
  const total = Math.max(0, Math.floor(seconds || 0));
  return [Math.floor(total / 3600), Math.floor((total % 3600) / 60), total % 60].map((value) => String(value).padStart(2, "0")).join(":");
};

function AudioWaveform({ file, currentTime, duration, onSeek }: { file: File | null; currentTime: number; duration: number; onSeek: (event: TimelineLanePointer) => void }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    let cancelled = false;
    const paint = async () => {
      const canvas = canvasRef.current;
      if (!canvas || !file) return;
      const context = canvas.getContext("2d");
      if (!context) return;
      try {
        const audio = new AudioContext();
        const decoded = await audio.decodeAudioData(await file.arrayBuffer());
        await audio.close();
        if (cancelled) return;
        const width = canvas.clientWidth || 900, height = canvas.clientHeight || 46, ratio = window.devicePixelRatio || 1;
        canvas.width = width * ratio; canvas.height = height * ratio; context.scale(ratio, ratio); context.clearRect(0, 0, width, height);
        const samples = decoded.getChannelData(0), bars = Math.max(80, Math.floor(width / 2)), step = Math.max(1, Math.floor(samples.length / bars));
        context.strokeStyle = "#20ced0"; context.globalAlpha = .9; context.lineWidth = 1;
        for (let bar = 0; bar < bars; bar += 1) {
          let peak = 0;
          for (let sample = bar * step; sample < Math.min(samples.length, (bar + 1) * step); sample += 1) peak = Math.max(peak, Math.abs(samples[sample] || 0));
          const amplitude = Math.max(1, peak * height * .45), x = (bar / bars) * width;
          context.beginPath(); context.moveTo(x, height / 2 - amplitude); context.lineTo(x, height / 2 + amplitude); context.stroke();
        }
      } catch { /* Some browser codecs cannot be decoded to a waveform; playback still works. */ }
    };
    void paint();
    return () => { cancelled = true; };
  }, [file]);
  return <div className="waveform-track" onClick={onSeek} onMouseDown={onSeek}><canvas ref={canvasRef} /><i style={{ left: `${duration ? Math.min(100, currentTime / duration * 100) : 0}%` }} /></div>;
}

function RenderedWaveform({ currentTime, duration, onSeek }: { currentTime: number; duration: number; onSeek: (event: TimelineLanePointer) => void }) {
  return <div className="rendered-waveform" onClick={onSeek} onMouseDown={onSeek}>
    <span /><i style={{ left: `${duration ? Math.min(100, currentTime / duration * 100) : 0}%` }} />
  </div>;
}

export default function StudioPage() {
  const inputRef = useRef<HTMLInputElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const trackGridRef = useRef<HTMLDivElement>(null);
  const scrubbingRef = useRef(false);
  const [file, setFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [duration, setDuration] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [timeline, setTimeline] = useState<TimelineProject | null>(null);
  const [selectedSegment, setSelectedSegment] = useState<number | null>(null);
  const [transcriptionState, setTranscriptionState] = useState<"idle" | "processing" | "error">("idle");
  const [message, setMessage] = useState("");
  const [zoom, setZoom] = useState(100);
  const [scrubbing, setScrubbing] = useState(false);
  const [previewRotation, setPreviewRotation] = useState<0 | 90 | -90 | 180>(0);
  const [insights, setInsights] = useState<StudioInsights | null>(null);
  const [extractingInsights, setExtractingInsights] = useState(false);
  const [autoEditTask, setAutoEditTask] = useState<AutoEditTask | null>(null);
  const [autoEditing, setAutoEditing] = useState(false);
  const [renderedClips, setRenderedClips] = useState<RenderedClip[]>([]);
  const [activeRenderedClip, setActiveRenderedClip] = useState<RenderedClip | null>(null);
  const sourceWorkspaceRef = useRef<SourceWorkspace | null>(null);

  const chooseFile = (candidate?: File) => {
    if (!candidate || (!candidate.type.startsWith("video/") && !candidate.name.toLowerCase().endsWith(".mp4"))) return;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    setFile(candidate); setPreviewUrl(URL.createObjectURL(candidate)); setDuration(0); setCurrentTime(0); setSegments([]); setTimeline(null); setInsights(null); setAutoEditTask(null); setAutoEditing(false); setRenderedClips([]); setActiveRenderedClip(null); sourceWorkspaceRef.current = null; setSelectedSegment(null); setTranscriptionState("idle"); setMessage(""); setPreviewRotation(0);
  };
  const removeFile = () => { if (previewUrl && !activeRenderedClip) URL.revokeObjectURL(previewUrl); setFile(null); setPreviewUrl(null); setDuration(0); setCurrentTime(0); setSegments([]); setTimeline(null); setInsights(null); setAutoEditTask(null); setAutoEditing(false); setRenderedClips([]); setActiveRenderedClip(null); sourceWorkspaceRef.current = null; setMessage(""); setPreviewRotation(0); };
  useEffect(() => {
    let cancelled = false;
    // A generated vertical clip has its own pixels and must never inherit the
    // display matrix of the imported phone recording.
    if (!file || activeRenderedClip) return undefined;
    void readMp4Rotation(file).then((rotation) => { if (!cancelled) setPreviewRotation(rotation); }).catch(() => { if (!cancelled) setPreviewRotation(0); });
    return () => { cancelled = true; };
  }, [file, activeRenderedClip]);
  const startTranscription = async () => {
    if (!file) return;
    setTranscriptionState("processing"); setMessage("正在用本机 faster-whisper（small · CPU · int8 · VAD）转写并纠错…");
    try {
      const body = new FormData(); body.append("video", file);
      // Large source videos must bypass Next.js' request-body parser. The local
      // FastAPI service accepts up to 12 GB and explicitly allows this origin.
      const response = await fetch(
        process.env.NEXT_PUBLIC_LOCAL_TRANSCRIPTION_URL || "http://localhost:8000/studio/transcribe",
        { method: "POST", body },
      );
      const text = await response.text();
      const payload = text ? JSON.parse(text) as { asr_segments?: Segment[]; insights?: StudioInsights; timeline?: TimelineProject; detail?: string; error?: string } : {};
      if (!response.ok) throw new Error(payload.detail || payload.error || `转写服务不可用（${response.status}）`);
      if (!payload.asr_segments?.length) throw new Error("未识别到可用人声，请检查视频音轨。");
      setSegments(payload.asr_segments); setInsights(payload.insights || null); setTimeline(payload.timeline || null); setTranscriptionState("idle"); setMessage(`转写完成：${payload.asr_segments.length} 段已按时间戳载入时间线。`);
    } catch (error) { setTranscriptionState("error"); setMessage(error instanceof Error ? error.message : "转写失败，请检查本机服务。"); }
  };
  const extractInsights = async () => {
    if (!segments.length) return;
    setExtractingInsights(true);
    setMessage("正在从当前转写文稿提取金句与关键词…");
    try {
      const response = await fetch(
        process.env.NEXT_PUBLIC_LOCAL_TRANSCRIPTION_URL?.replace(/\/studio\/transcribe$/, "/studio/insights") || "http://localhost:8000/studio/insights",
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ asr_segments: segments }) },
      );
      const text = await response.text();
      const payload = text ? JSON.parse(text) as { insights?: StudioInsights; detail?: string; error?: string } : {};
      if (!response.ok || !payload.insights) throw new Error(payload.detail || payload.error || "金句提取服务暂不可用。");
      setInsights(payload.insights);
      setMessage(payload.insights.engine === "llm" ? "模型已提取金句与关键词。" : "当前显示本地候选；配置模型后会自动升级为模型提取。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "金句提取失败，请重试。"); }
    finally { setExtractingInsights(false); }
  };
  const autoEditBaseUrl = process.env.NEXT_PUBLIC_LOCAL_TRANSCRIPTION_URL?.replace(/\/studio\/transcribe$/, "/studio/auto-edit") || "http://localhost:8000/studio/auto-edit";
  const artifactUrl = (taskId: string, artifactPath: string, download = false) => `${autoEditBaseUrl}/${taskId}/artifact?path=${encodeURIComponent(artifactPath)}${download ? "&download=true" : ""}`;
  const selectRenderedClip = (clip: RenderedClip) => {
    if (!activeRenderedClip && previewUrl) {
      sourceWorkspaceRef.current = { previewUrl, duration, segments, timeline, insights, previewRotation };
    }
    setActiveRenderedClip(clip);
    setPreviewUrl(artifactUrl(clip.task_id, clip.video_path));
    setDuration(clip.duration);
    setCurrentTime(0);
    setSegments(clip.segments);
    setTimeline({ id: `rendered_${clip.task_id}_${clip.clip_id}`, timeline: { ranges: [{ id: clip.clip_id, source_start: 0, source_end: clip.duration }] }, artifacts: { edl: "", srt: "", fcpxml: "" } });
    setInsights(null);
    setSelectedSegment(null);
    setPreviewRotation(0);
    setMessage(`正在查看${clip.name}：${clip.topic}`);
  };
  const selectSourceMedia = () => {
    const source = sourceWorkspaceRef.current;
    if (!source) return;
    setActiveRenderedClip(null);
    setPreviewUrl(source.previewUrl);
    setDuration(source.duration);
    setCurrentTime(0);
    setSegments(source.segments);
    setTimeline(source.timeline);
    setInsights(source.insights);
    setPreviewRotation(source.previewRotation);
    setSelectedSegment(null);
    setMessage("已切回原始视频与原始时间线。");
  };
  const downloadRenderedClip = () => {
    if (!activeRenderedClip) return;
    window.open(artifactUrl(activeRenderedClip.task_id, activeRenderedClip.video_path, true), "_blank", "noopener,noreferrer");
  };
  const startAutoEdit = async () => {
    if (activeRenderedClip) {
      setMessage("请先在左侧选择原始视频，再开始新的自动剪辑任务。");
      return;
    }
    if (!timeline || !segments.length) {
      setMessage("请先完成转写；自动剪辑会使用当前时间轴和已编辑的转写文本。");
      return;
    }
    setAutoEditing(true);
    setMessage("正在把视频、时间轴、金句与关键词交给本机固化剪辑流程…");
    try {
      const response = await fetch(autoEditBaseUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_id: timeline.id,
          asr_segments: segments,
          golden_sentences: insights?.golden_sentences || [],
          keywords: insights?.keywords || [],
        }),
      });
      const text = await response.text();
      const payload = text ? JSON.parse(text) as AutoEditTask & { detail?: string } : null;
      if (!response.ok || !payload?.task_id) throw new Error(payload?.detail || `本机任务桥接失败（${response.status}）`);
      setAutoEditTask(payload);
      setMessage("自动剪辑任务已进入本机队列，将按固化流程完成选段、固定画框、字幕、封面、成片与 QC。");
    } catch (error) {
      setAutoEditing(false);
      setMessage(error instanceof Error ? error.message : "自动剪辑任务创建失败。");
    }
  };
  const rerenderAutoEdit = async () => {
    if (!autoEditTask || autoEditing) return;
    setAutoEditing(true);
    setMessage("正在按最新固化规则创建复渲染任务…");
    try {
      const response = await fetch(`${autoEditBaseUrl}/${autoEditTask.task_id}/rerender`, { method: "POST" });
      const text = await response.text();
      const payload = text ? JSON.parse(text) as AutoEditTask & { detail?: string } : null;
      if (!response.ok || !payload?.task_id) throw new Error(payload?.detail || "无法创建复渲染任务");
      setActiveRenderedClip(null);
      setAutoEditTask(payload);
      setMessage("已创建复渲染任务：旧成片保留，新成片将按短主题与安全区规则生成。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "无法创建复渲染任务");
    } finally {
      setAutoEditing(false);
    }
  };
  useEffect(() => {
    const taskId = autoEditTask?.task_id;
    if (!taskId) return undefined;
    if (autoEditTask.status === "completed" || autoEditTask.status === "failed") {
      setAutoEditing(false);
      return undefined;
    }
    let active = true;
    const poll = async () => {
      try {
        const response = await fetch(`${autoEditBaseUrl}/${taskId}`, { cache: "no-store" });
        const text = await response.text();
        const payload = text ? JSON.parse(text) as AutoEditTask & { detail?: string } : null;
        if (!response.ok || !payload?.task_id) throw new Error(payload?.detail || "无法读取自动剪辑进度");
        if (!active) return;
        setAutoEditTask(payload);
        setAutoEditing(payload.status === "queued" || payload.status === "running");
        if (payload.status === "completed") setMessage("自动剪辑已完成，成片、字幕和封面已回传。");
        if (payload.status === "failed") setMessage(payload.error || "自动剪辑失败，请查看任务状态。");
      } catch (error) {
        if (active) setMessage(error instanceof Error ? error.message : "无法读取自动剪辑进度。");
      }
    };
    void poll();
    const timer = window.setInterval(() => { void poll(); }, 2500);
    return () => { active = false; window.clearInterval(timer); };
  }, [autoEditBaseUrl, autoEditTask?.status, autoEditTask?.task_id]);
  useEffect(() => {
    if (!autoEditTask?.clips?.length) return;
    setRenderedClips((existing) => {
      const next = new Map(existing.map((clip) => [`${clip.task_id}:${clip.clip_id}`, clip]));
      autoEditTask.clips?.forEach((clip) => next.set(`${clip.task_id}:${clip.clip_id}`, clip));
      return [...next.values()];
    });
  }, [autoEditTask?.clips]);
  const togglePlayback = async () => { const video = videoRef.current; if (!video) return; if (video.paused) await video.play(); else video.pause(); };
  const seek = (seconds: number) => { if (videoRef.current) videoRef.current.currentTime = seconds; setCurrentTime(seconds); };
  const seekFromTimelineEvent = (event: TimelinePointer) => {
    const trackGrid = event.currentTarget.querySelector<HTMLDivElement>(".track-grid") || trackGridRef.current;
    if (!trackGrid || !duration) return;
    const trackRect = trackGrid.getBoundingClientRect();
    const labelWidth = 154;
    const trackWidth = Math.max(1, trackRect.width - labelWidth);
    const position = Math.max(0, Math.min(trackWidth, event.clientX - trackRect.left - labelWidth));
    seek(position / trackWidth * duration);
  };
  const seekFromTimelinePointer = (event: ReactPointerEvent<HTMLDivElement>) => seekFromTimelineEvent(event);
  const seekWithinTimelineLane = (event: TimelineLanePointer) => {
    if (!duration) return;
    const lane = event.currentTarget.getBoundingClientRect();
    const position = Math.max(0, Math.min(lane.width, event.clientX - lane.left));
    seek(position / Math.max(1, lane.width) * duration);
  };
  const beginTimelineScrub = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!duration) return;
    scrubbingRef.current = true;
    setScrubbing(true);
    event.currentTarget.setPointerCapture(event.pointerId);
    seekFromTimelinePointer(event);
  };
  const moveTimelineScrub = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (scrubbingRef.current) seekFromTimelinePointer(event);
  };
  const endTimelineScrub = (event: ReactPointerEvent<HTMLDivElement>) => {
    scrubbingRef.current = false;
    setScrubbing(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };
  const updateText = (index: number, text: string) => setSegments((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, text } : item));
  const updateSpeaker = (index: number, speaker: SpeakerRole) => setSegments((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, speaker } : item));
  const ranges = timeline?.timeline.ranges || (duration ? [{ id: "source", source_start: 0, source_end: duration }] : []);
  const ruler = useMemo(() => Array.from({ length: 9 }, (_, index) => duration / 8 * index), [duration]);
  const playheadStyle = { "--playhead-position": `${duration ? Math.min(100, currentTime / duration * 100) : 0}%` } as CSSProperties;
  const isGoldenSegment = (segment: Segment) => insights?.golden_sentences.some((sentence) => segment.end > sentence.start && segment.start < sentence.end) || false;
  const activeMediaName = activeRenderedClip?.name || file?.name || "等待导入视频";

  return <main className="editor-shell">
    <header className="editor-topbar">
      <button className="top-icon" aria-label="打开菜单"><Menu size={22} /></button><strong>AI 视频剪辑工作台</strong>
      <nav><button>文件</button><button>编辑</button><button>视图</button><button>工具</button><button>帮助</button></nav>
      <div className="top-actions"><span><Check size={15} />{transcriptionState === "processing" ? "正在处理" : "已保存"}</span><i />{autoEditTask?.status === "completed" && <button className="rerender-button" onClick={rerenderAutoEdit} disabled={autoEditing}>按修订规则重生成</button>}<button className="auto-edit-button" onClick={startAutoEdit} disabled={!timeline || !segments.length || autoEditing || !!activeRenderedClip}>{autoEditing ? <LoaderCircle size={16} /> : <Scissors size={16} />}{autoEditing ? "自动剪辑中" : "开始自动剪辑"}</button><button className="export-button" onClick={downloadRenderedClip} disabled={!activeRenderedClip}><Upload size={16} />导出成片</button><button className="top-icon" aria-label="设置"><Settings2 size={18} /></button></div>
    </header>
    <section className="editor-main">
      <aside className="media-browser">
        <div className="pane-title"><strong>项目素材</strong><button aria-label="切换视图"><ListVideo size={17} /></button></div>
        <button className="import-button" onClick={() => inputRef.current?.click()}><Plus size={19} />导入素材</button><input ref={inputRef} type="file" accept="video/*" hidden onChange={(event: ChangeEvent<HTMLInputElement>) => chooseFile(event.target.files?.[0])} />
        <label className="asset-search"><Search size={16} /><input placeholder="搜索素材…" /></label>
        <div className="asset-body"><div className="asset-types"><button className="active"><Layers3 size={17} />全部 <em>{(file ? 1 : 0) + renderedClips.length}</em></button><button><Film size={17} />视频 <em>{(file ? 1 : 0) + renderedClips.length}</em></button><button><Music2 size={17} />音频 <em>0</em></button><button><ImageIcon size={17} />图片 <em>0</em></button><button><Captions size={17} />字幕 <em>{segments.length ? 1 : 0}</em></button></div><div className="asset-list">{file ? <>
          <p className="asset-section-label">原始素材</p>
          <article className={`asset-card ${!activeRenderedClip ? "active" : ""}`} onClick={selectSourceMedia} role="button" tabIndex={0} onKeyDown={(event) => { if (event.key === "Enter") selectSourceMedia(); }}><video src={sourceWorkspaceRef.current?.previewUrl || previewUrl || undefined} muted /><strong>{file.name}</strong><span>{sourceWorkspaceRef.current?.duration || duration ? formatTime(sourceWorkspaceRef.current?.duration || duration) : "读取媒体中…"}</span><button onClick={(event) => { event.stopPropagation(); removeFile(); }} aria-label="移除素材"><X size={14} /></button></article>
          {renderedClips.length > 0 && <><p className="asset-section-label rendered">剪辑成片</p>{renderedClips.map((clip) => <article className={`asset-card rendered-card ${activeRenderedClip?.task_id === clip.task_id && activeRenderedClip.clip_id === clip.clip_id ? "active" : ""}`} key={`${clip.task_id}-${clip.clip_id}`} onClick={() => selectRenderedClip(clip)} role="button" tabIndex={0} onKeyDown={(event) => { if (event.key === "Enter") selectRenderedClip(clip); }}><div className="rendered-cover">{clip.cover_path ? <img src={artifactUrl(clip.task_id, clip.cover_path)} alt={`${clip.name}封面`} /> : <Film size={24} />}</div><strong>{clip.name}</strong><span>{formatTime(clip.duration)} · {clip.topic}</span><a href={artifactUrl(clip.task_id, clip.video_path, true)} onClick={(event) => event.stopPropagation()}><Download size={13} />导出</a></article>)}</>}
        </> : <div className="asset-empty"><FolderOpen size={25} /><p>导入视频后在这里管理素材</p></div>}</div></div>
      </aside>
      <section className="preview-pane">
        <div className={previewRotation === 90 || previewRotation === -90 ? "preview-frame rotated-source" : "preview-frame"} onDragOver={(event) => event.preventDefault()} onDrop={(event: DragEvent<HTMLDivElement>) => { event.preventDefault(); chooseFile(event.dataTransfer.files[0]); }}>
          {previewUrl ? <video key={previewUrl} ref={videoRef} src={previewUrl} playsInline preload="auto" onLoadedMetadata={(event) => setDuration(event.currentTarget.duration || 0)} onLoadedData={(event) => {
            const video = event.currentTarget;
            // Some phone videos begin with a black decode frame. Move a fraction
            // forward so importing always gives the user a visible frame to inspect.
            if (video.currentTime < 0.04 && video.duration > 0.12) video.currentTime = 0.12;
          }} onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)} onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} /> : <button className="drop-placeholder" onClick={() => inputRef.current?.click()}><Upload size={26} /><strong>导入原始视频</strong><span>拖入视频或点击选择文件</span></button>}
        </div>
        <div className="preview-controls"><span>{formatTime(currentTime)} <i>/</i> {formatTime(duration)}</span><button onClick={() => seek(Math.max(0, currentTime - 5))} aria-label="后退五秒"><Undo2 size={17} /></button><button className="play-button" onClick={togglePlayback} disabled={!file} aria-label={playing ? "暂停" : "播放"}>{playing ? <Pause size={21} fill="currentColor" /> : <Play size={21} fill="currentColor" />}</button><button onClick={() => seek(Math.min(duration, currentTime + 5))} aria-label="前进五秒"><Redo2 size={17} /></button><Volume2 size={17} /><div className="preview-progress"><span style={{ width: `${duration ? currentTime / duration * 100 : 0}%` }} /></div><button aria-label="显示选项"><Eye size={17} /></button></div>
      </section>
      <aside className="transcript-pane">
        <div className="inspector-tabs"><button className="active">转写</button><button>字幕</button><button>效果设置</button></div><label className="transcript-search"><Search size={16} /><input placeholder="搜索转写内容…" /><SlidersHorizontal size={16} /></label>
        <div className="transcript-status"><span>{transcriptionState === "processing" ? <LoaderCircle size={14} /> : <AudioLines size={14} />}{activeRenderedClip ? "剪辑成片时间线" : transcriptionState === "processing" ? "转写中" : segments.length ? "已加载时间戳" : "等待转写"}</span><div>{!activeRenderedClip && segments.length > 0 && <button className="insights-action" onClick={extractInsights} disabled={extractingInsights}>{extractingInsights ? "提取中" : "提取金句"}</button>}{activeRenderedClip ? <button className="insights-action" onClick={selectSourceMedia}>返回原片</button> : file && <button className="transcribe-action" onClick={startTranscription} disabled={transcriptionState === "processing"}>{transcriptionState === "processing" ? "正在转写" : "开始转写"}</button>}</div></div>
        <div className="transcript-rows">{autoEditTask && <section className={`auto-edit-panel ${autoEditTask.status}`} aria-label="本机自动剪辑任务"><header><strong>本机自动剪辑</strong><span>{autoEditTask.status === "queued" ? "排队中" : autoEditTask.status === "running" ? "处理中" : autoEditTask.status === "completed" ? "已完成" : "失败"}</span></header><p>{autoEditTask.stage}</p><div className="auto-edit-progress"><i style={{ width: `${Math.max(3, autoEditTask.progress * 100)}%` }} /></div>{autoEditTask.error && <small>{autoEditTask.error}</small>}{autoEditTask.artifacts.length > 0 && <div className="auto-edit-artifacts">{autoEditTask.artifacts.filter((artifact) => /\.(mp4|srt|jpg)$/i.test(artifact.name)).map((artifact) => <a key={artifact.path} href={`${autoEditBaseUrl}/${autoEditTask.task_id}/artifact?path=${encodeURIComponent(artifact.path)}`} target="_blank" rel="noreferrer">{artifact.name}</a>)}</div>}</section>}{insights && <section className="insight-panel" aria-label="金句与关键词"><header><strong>金句与关键词</strong><span className={insights.engine === "llm" ? "model" : "fallback"}>{insights.engine === "llm" ? "模型提取" : "本地候选"}</span></header><div className="golden-list">{insights.golden_sentences.map((sentence, index) => <button key={`${sentence.start}-${index}`} title={sentence.reason} onClick={() => seek(sentence.start)}><time>{formatTime(sentence.start)}</time><span>{sentence.text}</span></button>)}</div>{insights.keywords.length > 0 && <div className="keyword-list">{insights.keywords.map((keyword) => <span key={keyword}>#{keyword}</span>)}</div>}{insights.notice && <p>{insights.notice}</p>}</section>}{segments.length ? segments.map((segment, index) => <article className={selectedSegment === index ? "selected" : ""} key={`${segment.start}-${index}`} onClick={() => { setSelectedSegment(index); seek(segment.start); }}><time>{formatTime(segment.start)}</time><div><select value={segment.speaker || "未标注"} onChange={(event) => updateSpeaker(index, event.target.value as SpeakerRole)} onClick={(event) => event.stopPropagation()} className={segment.speaker === "医生" ? "doctor" : segment.speaker === "患者" ? "patient" : ""}><option value="未标注">未标注</option><option value="医生">医生</option><option value="患者">患者</option></select><textarea value={segment.text} rows={2} onClick={(event) => event.stopPropagation()} onChange={(event) => updateText(index, event.target.value)} /></div></article>) : <div className="transcript-awaiting"><AudioLines size={28} /><strong>{transcriptionState === "error" ? "转写没有完成" : "等待视频转写"}</strong><p>{message || "上传视频后点击“开始转写”，原文、纠错结果和说话人标签会显示在这里。"}</p></div>}</div>
      </aside>
    </section>
    <section className="timeline-pane">
      <div className="timeline-toolbar"><strong>时间线</strong><div><button aria-label="选择"><Menu size={16} /></button><button aria-label="剪刀"><Scissors size={16} /></button><button aria-label="删除"><Trash2 size={16} /></button><button aria-label="撤销"><Undo2 size={16} /></button><button aria-label="重做"><Redo2 size={16} /></button></div><div className="timeline-right"><button onClick={() => setZoom((value) => Math.max(75, value - 10))}><ZoomOut size={16} /></button><input aria-label="时间线缩放" type="range" min="75" max="150" value={zoom} onChange={(event) => setZoom(Number(event.target.value))} /><button onClick={() => setZoom((value) => Math.min(150, value + 10))}><ZoomIn size={16} /></button><button className="fit-button">适合窗口 <ChevronDown size={14} /></button><button aria-label="时间线设置"><Settings2 size={16} /></button></div></div>
      <div className={scrubbing ? "timeline-scroll is-scrubbing" : "timeline-scroll"} onPointerDown={beginTimelineScrub} onPointerMove={moveTimelineScrub} onPointerUp={endTimelineScrub} onPointerCancel={endTimelineScrub}><div className="time-ruler" style={{ minWidth: 860 * zoom / 100 }} onClick={seekWithinTimelineLane} onMouseDown={seekWithinTimelineLane}><span className="track-gutter" />{ruler.map((mark, index) => <span key={`${mark}-${index}`} style={{ left: `${duration ? mark / duration * 100 : 0}%` }}>{formatTime(mark)}</span>)}</div><div ref={trackGridRef} className="track-grid" style={{ minWidth: 154 + 860 * zoom / 100 }}><div className="track-label"><Type size={16} /><span>字幕</span><Eye size={14} /><LockKeyhole size={13} /></div><div className="caption-track" style={playheadStyle} onClick={seekWithinTimelineLane} onMouseDown={seekWithinTimelineLane}>{segments.map((segment, index) => <button key={`${segment.start}-${index}`} className={`${selectedSegment === index ? "active " : ""}${isGoldenSegment(segment) ? "golden" : ""}`.trim()} style={{ left: `${duration ? segment.start / duration * 100 : 0}%`, width: `${duration ? Math.max(5, (segment.end - segment.start) / duration * 100) : 0}%` }} onPointerDown={(event) => event.stopPropagation()} onClick={(event) => { event.stopPropagation(); setSelectedSegment(index); seek(segment.start); }}>{segment.text}</button>)}<i className="track-playhead" aria-hidden="true" /></div><div className="track-label"><Film size={16} /><span>视频 1</span><Eye size={14} /><LockKeyhole size={13} /></div><div className="video-track" style={playheadStyle} onClick={seekWithinTimelineLane} onMouseDown={seekWithinTimelineLane}>{ranges.map((range) => <div className="video-clip" key={range.id} style={{ left: `${duration ? range.source_start / duration * 100 : 0}%`, width: `${duration ? (range.source_end - range.source_start) / duration * 100 : 0}%` }}><b>{activeMediaName}</b><span>{formatTime(range.source_start)} — {formatTime(range.source_end)}</span></div>)}<i className="track-playhead" aria-hidden="true" /></div><div className="track-label"><Volume2 size={16} /><span>音频 1</span><Eye size={14} /><LockKeyhole size={13} /></div>{activeRenderedClip ? <RenderedWaveform currentTime={currentTime} duration={duration} onSeek={seekWithinTimelineLane} /> : <AudioWaveform file={file} currentTime={currentTime} duration={duration} onSeek={seekWithinTimelineLane} />}</div></div>
      <footer className="timeline-footer"><span>{file || activeRenderedClip ? `${activeMediaName} · ${formatTime(duration)}` : "尚未导入素材"}</span>{timeline && <span><Save size={14} />{activeRenderedClip ? "成片时间线已载入" : "EDL、SRT 与 FCPXML 已生成"}</span>}<button onClick={downloadRenderedClip} disabled={!activeRenderedClip}><Download size={14} />导出成片</button></footer>
    </section>
  </main>;
}
