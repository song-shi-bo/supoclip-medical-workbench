"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Captions, ChevronDown, Download, Eye, Film, FolderOpen, LoaderCircle,
  Pause, Play, Save, Scissors, Volume2,
} from "lucide-react";

export type StudioTranscriptSegment = {
  start: number;
  end: number;
  text: string;
  speaker?: string;
};

export type StudioTimelineRange = {
  id: string;
  source: string;
  source_start: number;
  source_end: number;
  record_start: number;
  track: number;
  beat?: string;
  quote?: string;
  reason?: string;
};

export type StudioTimelineProject = {
  id: string;
  source: { name: string; duration: number; width: number; height: number; fps: number };
  timeline: { name: string; resolution: [number, number]; ranges: StudioTimelineRange[] };
  transcript: StudioTranscriptSegment[];
  artifacts: { edl: string; srt: string; fcpxml: string };
};

const formatTime = (seconds: number) => {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return [hours, minutes, secs].map((unit) => String(unit).padStart(2, "0")).join(":");
};

function Waveform({ file, currentTime, duration }: { file: File; currentTime: number; duration: number }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    let cancelled = false;
    const draw = async () => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const context = canvas.getContext("2d");
      if (!context) return;
      try {
        const audioContext = new AudioContext();
        const decoded = await audioContext.decodeAudioData(await file.arrayBuffer());
        await audioContext.close();
        if (cancelled) return;
        const channel = decoded.getChannelData(0);
        const buckets = Math.min(520, Math.max(64, Math.floor(canvas.clientWidth || 520)));
        const step = Math.max(1, Math.floor(channel.length / buckets));
        const ratio = window.devicePixelRatio || 1;
        canvas.width = Math.floor((canvas.clientWidth || 520) * ratio);
        canvas.height = Math.floor((canvas.clientHeight || 42) * ratio);
        context.scale(ratio, ratio);
        const width = canvas.clientWidth || 520;
        const height = canvas.clientHeight || 42;
        context.clearRect(0, 0, width, height);
        context.strokeStyle = "#19b8c9";
        context.globalAlpha = 0.92;
        context.lineWidth = 1;
        for (let index = 0; index < buckets; index += 1) {
          let peak = 0;
          const from = index * step;
          const to = Math.min(channel.length, from + step);
          for (let sample = from; sample < to; sample += 1) peak = Math.max(peak, Math.abs(channel[sample] || 0));
          const amplitude = Math.max(1, peak * height * 0.48);
          const x = (index / buckets) * width;
          context.beginPath(); context.moveTo(x, height / 2 - amplitude); context.lineTo(x, height / 2 + amplitude); context.stroke();
        }
      } catch {
        // Some uploaded codecs are not decodable by Web Audio. Playback still works.
      }
    };
    void draw();
    return () => { cancelled = true; };
  }, [file]);

  const playhead = duration > 0 ? `${Math.min(100, (currentTime / duration) * 100)}%` : "0%";
  return <div className="timeline-waveform"><canvas ref={canvasRef} /><span style={{ left: playhead }} /></div>;
}

export function StudioTimelineDraft({ file, previewUrl }: { file: File; previewUrl: string }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [duration, setDuration] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const ticks = useMemo(() => {
    const safeDuration = duration || 60;
    return Array.from({ length: 7 }, (_, index) => (safeDuration / 6) * index);
  }, [duration]);

  const togglePlayback = async () => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) await video.play(); else video.pause();
  };

  return <section className="timeline-editor timeline-draft" aria-label="视频时间线">
    <header className="timeline-editor-header">
      <div><span>06</span><h2>可视化时间线</h2><p>视频轨已准备就绪；完成转写后会自动填入字幕轨、时间戳片段与 FCPXML。</p></div>
      <div className="timeline-actions"><button disabled><Captions size={15} />等待转写生成字幕轨</button></div>
    </header>
    <div className="timeline-draft-preview">
      <video ref={videoRef} src={previewUrl} muted onLoadedMetadata={(event) => setDuration(event.currentTarget.duration || 0)} onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)} onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} />
      <div><strong>{file.name}</strong><span>{duration ? formatTime(duration) : "正在读取视频时长…"}</span></div>
      <button onClick={togglePlayback} aria-label={playing ? "暂停预览" : "播放预览"}>{playing ? <Pause size={17} fill="currentColor" /> : <Play size={17} fill="currentColor" />}</button>
    </div>
    <div className="timeline-sequence">
      <div className="sequence-toolbar"><strong>时间线：原视频</strong><span><Scissors size={15} />转写完成后可按时间戳选择片段</span></div>
      <div className="timeline-ruler">{ticks.map((tick) => <span key={tick} style={{ left: `${duration ? (tick / duration) * 100 : 0}%` }}>{formatTime(tick)}</span>)}</div>
      <div className="timeline-lanes timeline-draft-lanes">
        <div className="lane-name"><Captions size={15} />字幕</div><div className="subtitle-lane draft-caption-lane"><span>转写完成后显示逐段字幕</span></div>
        <div className="lane-name"><Film size={15} />视频 1</div><div className="video-lane"><div className="video-range draft-video-range"><b>{file.name}</b><span>{duration ? `00:00:00 — ${formatTime(duration)}` : "正在读取视频轨"}</span></div></div>
        <div className="lane-name"><Volume2 size={15} />音频 1</div><Waveform file={file} currentTime={currentTime} duration={duration || 1} />
      </div>
    </div>
  </section>;
}

export function StudioTimelineEditor({ project, file, previewUrl }: { project: StudioTimelineProject; file: File; previewUrl: string }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState("");
  const [selectedSegment, setSelectedSegment] = useState<number | null>(null);
  const duration = project.source.duration;
  const tickMarks = useMemo(() => {
    const count = Math.min(12, Math.max(5, Math.ceil(duration / 60)));
    return Array.from({ length: count + 1 }, (_, index) => (duration / count) * index);
  }, [duration]);

  const seek = (seconds: number) => {
    if (!videoRef.current) return;
    videoRef.current.currentTime = seconds;
    setCurrentTime(seconds);
  };
  const togglePlayback = async () => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) await video.play(); else video.pause();
  };
  const saveTimeline = async () => {
    setSaving(true); setSaveMessage("");
    try {
      const response = await fetch(`/api/studio/timeline/${project.id}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ranges: project.timeline.ranges }),
      });
      const payload = await response.json() as StudioTimelineProject | { detail?: string };
      if (!response.ok) throw new Error("detail" in payload ? payload.detail : "时间线保存失败");
      setSaveMessage("已保存 EDL，并重新生成 SRT 与 FCPXML。");
    } catch (error) {
      setSaveMessage(error instanceof Error ? error.message : "时间线保存失败");
    } finally { setSaving(false); }
  };
  const downloadEdl = () => {
    const blob = new Blob([JSON.stringify(project.timeline, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a"); link.href = url; link.download = `${project.id}.timeline.json`; link.click(); URL.revokeObjectURL(url);
  };

  return <section className="timeline-editor" aria-label="可视化时间线编辑器">
    <header className="timeline-editor-header">
      <div><span>06</span><h2>可视化时间线</h2><p>当前项目由 EDL 驱动；转写、字幕、预览与 FCPXML 使用同一时间轴。</p></div>
      <div className="timeline-actions"><button onClick={downloadEdl}><Download size={15} />下载 EDL</button><button className="timeline-save" onClick={saveTimeline} disabled={saving}>{saving ? <LoaderCircle size={15} /> : <Save size={15} />}{saving ? "正在保存" : "保存时间线"}</button></div>
    </header>
    <div className="timeline-workspace">
      <aside className="timeline-media">
        <div className="timeline-pane-heading"><FolderOpen size={16} /><strong>项目素材</strong></div>
        <div className="timeline-file active"><Film size={17} /><div><b>{project.source.name}</b><span>{formatTime(duration)} · {project.source.width} × {project.source.height}</span></div></div>
        <div className="timeline-metadata"><span>EDL</span><code>{project.artifacts.edl}</code><span>SRT</span><code>{project.artifacts.srt}</code><span>FCPXML</span><code>{project.artifacts.fcpxml}</code></div>
      </aside>
      <div className="timeline-viewer">
        <video ref={videoRef} src={previewUrl} onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)} onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} />
        <div className="viewer-controls"><span>{formatTime(currentTime)} <i>/</i> {formatTime(duration)}</span><button onClick={togglePlayback} aria-label={playing ? "暂停" : "播放"}>{playing ? <Pause size={18} fill="currentColor" /> : <Play size={18} fill="currentColor" />}</button><Volume2 size={17} /><div className="viewer-progress"><span style={{ width: `${duration ? (currentTime / duration) * 100 : 0}%` }} /></div><Eye size={17} /></div>
      </div>
      <aside className="timeline-transcript">
        <div className="timeline-pane-heading"><Captions size={16} /><strong>转写</strong><button>段落 <ChevronDown size={14} /></button></div>
        <div className="timeline-transcript-list">{project.transcript.map((segment, index) => <button className={selectedSegment === index ? "selected" : ""} key={`${segment.start}-${index}`} onClick={() => { setSelectedSegment(index); seek(segment.start); }}><time>{formatTime(segment.start)}</time><span className={segment.speaker === "医生" ? "speaker doctor" : segment.speaker === "患者" ? "speaker patient" : "speaker"}>{segment.speaker || "未标注"}</span><p>{segment.text}</p></button>)}</div>
      </aside>
    </div>
    <div className="timeline-sequence">
      <div className="sequence-toolbar"><strong>时间线：{project.timeline.name}</strong><span><Scissors size={15} />片段选择与字幕均对齐源视频时间戳</span>{saveMessage && <em>{saveMessage}</em>}</div>
      <div className="timeline-ruler">{tickMarks.map((tick) => <span key={tick} style={{ left: `${duration ? (tick / duration) * 100 : 0}%` }}>{formatTime(tick)}</span>)}</div>
      <div className="timeline-lanes">
        <div className="lane-name"><Captions size={15} />字幕</div><div className="subtitle-lane">{project.transcript.map((segment, index) => <button key={`${segment.start}-${index}`} className={selectedSegment === index ? "selected" : ""} style={{ left: `${(segment.start / duration) * 100}%`, width: `${Math.max(2.5, ((segment.end - segment.start) / duration) * 100)}%` }} onClick={() => { setSelectedSegment(index); seek(segment.start); }}>{segment.text}</button>)}</div>
        <div className="lane-name"><Film size={15} />视频 1</div><div className="video-lane">{project.timeline.ranges.map((range) => <div key={range.id} className="video-range" style={{ left: `${(range.source_start / duration) * 100}%`, width: `${((range.source_end - range.source_start) / duration) * 100}%` }}><b>{project.source.name}</b><span>{formatTime(range.source_start)} — {formatTime(range.source_end)}</span></div>)}</div>
        <div className="lane-name"><Volume2 size={15} />音频 1</div><Waveform file={file} currentTime={currentTime} duration={duration} />
      </div>
    </div>
  </section>;
}
