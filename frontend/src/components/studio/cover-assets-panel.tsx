"use client";

/**
 * 独立封面素材（紧凑横向卡片，2026-10-20 二次统一）。
 *
 * 需求锚点：
 *  - 紧凑横向卡：左侧固定宽 80px 的 9:16 缩略图（aspect-ratio: 9/16 + object-fit:
 *    contain，深色背景填充空白，绝不拉伸）；右侧信息区（名称/状态/下载）；
 *  - 点击**缩略图**才打开大图预览（Lightbox），信息区点击不触发；
 *  - 素材栏宽度变化时只缩放信息区域（minmax(0,1fr)），缩略图固定宽不放大；
 *  - 历史非 9:16 图片同样 contain 完整显示；Lightbox 里真实宽高标注比例；
 *  - 缺失/失败明确显示失败态与原因，绝不用其它切片的封面冒充。
 */

import { useEffect, useState } from "react";
import { AlertTriangle, Download, ImageOff, X } from "lucide-react";
import { coverFitClass, type CoverAssetItem } from "@/lib/studio-cover-assets";

export type CoverAssetsPanelProps = {
  items: CoverAssetItem[];
  loading: boolean;
  error: string | null;
  /** 组装封面预览/下载 URL（页面注入 artifactUrl，组件不耦合请求层）。 */
  resolveUrl: (item: CoverAssetItem, download?: boolean) => string | null;
  activeClipId?: string | null;
  onSelectClip?: (clipId: string) => void;
};

/** 大图预览（Lightbox）：真实比例 contain 显示，Esc/背景/关闭按钮退出。 */
export function CoverLightbox({ url, name, onClose }: {
  url: string; name: string; onClose: () => void;
}) {
  const [ratioLabel, setRatioLabel] = useState("读取尺寸中…");
  useEffect(() => {
    const handler = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);
  return (
    <div className="cover-lightbox-backdrop" role="dialog" aria-modal="true"
      aria-label={`${name} 封面大图`} onClick={onClose}>
      <figure className="cover-lightbox-body" onClick={(event) => event.stopPropagation()}>
        <img src={url} alt={`${name} 封面大图`}
          onLoad={(event) => {
            const img = event.currentTarget;
            setRatioLabel(`${img.naturalWidth}×${img.naturalHeight} · ` +
              (coverFitClass(img.naturalWidth, img.naturalHeight) === "fit-cover"
                ? "标准 9:16" : "非标准比例（contain 完整显示）"));
          }} />
        <figcaption>{name} · {ratioLabel}</figcaption>
        <button type="button" className="cover-lightbox-close" aria-label="关闭大图预览"
          onClick={onClose}><X size={16} /></button>
      </figure>
    </div>
  );
}

function CoverAssetCard({ item, resolveUrl, active, onSelect }: {
  item: CoverAssetItem;
  resolveUrl: CoverAssetsPanelProps["resolveUrl"];
  active: boolean;
  onSelect?: (clipId: string) => void;
}) {
  const url = item.status === "ready" ? resolveUrl(item) : null;
  const downloadUrl = item.status === "ready" ? resolveUrl(item, true) : null;
  const [broken, setBroken] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const failed = item.status !== "ready" || broken || !url;
  const binding = `项目 ${item.project_id}\n源片 ${item.source_id ?? "未知"}\n切片 ${item.clip_id}\n渲染任务 ${item.render_task_id ?? "未知"}\n封面批次 v${item.revision}`;
  return (
    <article className={`cover-asset-card${active ? " active" : ""}`} title={binding}
      onClick={() => onSelect?.(item.clip_id)}>
      {/* 缩略图（固定 80px 9:16，contain 防拉伸）：只有点这里才开大图预览。 */}
      <button type="button" className={`cover-asset-thumb${failed ? " is-failed" : ""}`}
        aria-label={`预览 ${item.clip_name} 封面大图`} title="点击查看封面大图"
        disabled={failed}
        onClick={(event) => { event.stopPropagation(); if (!failed && url) setPreviewing(true); }}>
        <span className={`cover-asset-frame${failed ? " is-failed" : ""}`}>
          {failed
            ? <span className="cover-asset-failed"><ImageOff size={20} />
                <b>{item.status !== "ready" ? "封面生成失败" : "封面加载失败"}</b>
                <small>{broken ? "封面文件无法读取" : item.reason || "未知原因"}</small>
              </span>
            : <img src={url ?? undefined} alt={`${item.clip_name} 独立封面`} loading="lazy"
                onError={() => setBroken(true)} />}
          <em className="cover-asset-revision" title={`封面批次 v${item.revision}`}>v{item.revision}</em>
        </span>
      </button>
      {/* 信息区：随素材栏宽度伸缩（minmax(0,1fr)），缩略图不放大。 */}
      <div className="cover-asset-info">
        <strong title={item.clip_name}>{item.clip_name}</strong>
        <span className="cover-asset-sub">
          <em className={`cover-asset-status ${failed ? "failed" : "ready"}`}>
            {failed ? <><AlertTriangle size={11} />失败</> : "就绪"}
          </em>
          {downloadUrl && <a href={downloadUrl} onClick={(event) => event.stopPropagation()}
            aria-label={`下载 ${item.clip_name} 封面`} title="下载封面"><Download size={12} />下载</a>}
        </span>
      </div>
      {previewing && url && <CoverLightbox url={url} name={item.clip_name}
        onClose={() => setPreviewing(false)} />}
    </article>
  );
}

export default function CoverAssetsPanel({
  items, loading, error, resolveUrl, activeClipId, onSelectClip,
}: CoverAssetsPanelProps) {
  return (
    <section className="asset-section cover-assets-section" aria-label="独立封面素材">
      <p className="asset-section-label covers">独立封面（{items.length}）</p>
      {loading && items.length === 0 && <p className="asset-subsection-empty">正在读取封面素材…</p>}
      {error && <p className="asset-subsection-empty cover-assets-error" role="alert">{error}</p>}
      {!loading && !error && items.length === 0
        && <p className="asset-subsection-empty">自动剪辑完成后，每条切片的独立封面会显示在这里。</p>}
      {items.length > 0 && <div className="cover-asset-list">
        {items.map((item) => (
          <CoverAssetCard key={`${item.clip_id}-v${item.revision}`} item={item}
            resolveUrl={resolveUrl} active={activeClipId === item.clip_id}
            onSelect={onSelectClip} />
        ))}
      </div>}
    </section>
  );
}
