"use client";

/**
 * 单条更新卡片。
 *
 * 字段渲染顺序 = 扫读优先级：类型/模块标签 → 标题 → 摘要 → 效果图（含前后对比）→
 * 折叠的详细说明 / 技术细节（技术细节仅 dev 视图渲染）。
 * highlight 条目（工作台核心变化）带左侧高亮边框与图标。
 */
import { MonitorPlay, MapPin } from "lucide-react";
import {
  STATUS_GLYPHS,
  STATUS_LABELS,
  moduleLabel,
  typeLabel,
  type TaggedUpdateEntry,
  type UpdateType,
} from "@/lib/updates";

const TYPE_BADGE_CLASS: Record<UpdateType, string> = {
  feature: "bg-primary text-primary-foreground",
  improvement: "bg-muted text-foreground",
  fix: "border text-foreground",
  experiment: "border border-dashed text-muted-foreground",
};

export function EntryCard({ entry, devView }: { entry: TaggedUpdateEntry; devView: boolean }) {
  return (
    <article
      data-update-id={entry.id}
      data-highlight={entry.highlight ? "true" : undefined}
      className={`rounded-lg border bg-card p-4 ${
        entry.highlight ? "border-l-4 border-l-primary" : ""
      }`}
    >
      <div className="mb-2 flex flex-wrap items-center gap-1.5 text-xs">
        {devView ? (
          <span title={STATUS_LABELS[entry.status]} aria-label={STATUS_LABELS[entry.status]}>
            {STATUS_GLYPHS[entry.status]}
          </span>
        ) : null}
        <span className={`rounded px-1.5 py-0.5 font-medium ${TYPE_BADGE_CLASS[entry.type]}`}>
          {typeLabel(entry.type)}
        </span>
        {entry.modules
          // highlight 条目已用 ⚡工作台 chip 标注工作台模块，这里去重避免重复徽章
          .filter((module) => !(entry.highlight && module === "workbench"))
          .map((module) => (
            <span key={module} className="rounded bg-secondary px-1.5 py-0.5 text-secondary-foreground">
              {moduleLabel(module)}
            </span>
          ))}
        {entry.highlight ? (
          <span className="inline-flex items-center gap-1 rounded bg-primary/10 px-1.5 py-0.5 font-medium text-primary">
            <MonitorPlay className="size-3" aria-hidden />
            工作台
          </span>
        ) : null}
        {entry.visibility === "internal" ? (
          <span className="rounded border border-amber-500/60 px-1.5 py-0.5 text-amber-600 dark:text-amber-400">
            内部
          </span>
        ) : null}
        {entry.version ? (
          <span className="rounded bg-muted px-1.5 py-0.5 font-mono">{entry.version}</span>
        ) : null}
      </div>

      <h3 className="font-medium leading-snug">{entry.title}</h3>
      <p className="mt-1 text-sm text-muted-foreground">{entry.summary}</p>

      {entry.compare ? (
        <div className="mt-3 grid grid-cols-2 gap-2">
          {(
            [
              { image: entry.compare.before, label: "旧版" },
              { image: entry.compare.after, label: "新版" },
            ] as const
          ).map(({ image, label }) => (
            <figure key={label} className="overflow-hidden rounded-md border">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={image.src} alt={image.alt} className="w-full object-cover" loading="lazy" />
              <figcaption className="bg-muted px-2 py-1 text-center text-xs text-muted-foreground">
                {label}
              </figcaption>
            </figure>
          ))}
        </div>
      ) : entry.image ? (
        <figure className="mt-3 overflow-hidden rounded-md border">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={entry.image.src} alt={entry.image.alt} className="w-full object-cover" loading="lazy" />
        </figure>
      ) : null}

      {entry.details ? (
        <details className="mt-3 text-sm">
          <summary className="cursor-pointer text-muted-foreground hover:text-foreground">详细说明</summary>
          <p className="mt-2 leading-relaxed">{entry.details}</p>
        </details>
      ) : null}

      {entry.entry ? (
        <p className="mt-3 inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <MapPin className="size-3 shrink-0" aria-hidden />
          {entry.entry}
        </p>
      ) : null}

      {entry.links && entry.links.length > 0 ? (
        <p className="mt-2 flex flex-wrap gap-3 text-xs">
          {entry.links.map((link) => (
            <a
              key={link.href}
              href={link.href}
              target="_blank"
              rel="noopener noreferrer"
              className="text-primary underline-offset-2 hover:underline"
            >
              {link.label}
            </a>
          ))}
        </p>
      ) : null}

      {devView && entry.techDetails ? (
        <details className="mt-3 text-sm">
          <summary className="cursor-pointer text-muted-foreground hover:text-foreground">技术细节</summary>
          <p className="mt-2 leading-relaxed text-muted-foreground">{entry.techDetails}</p>
        </details>
      ) : null}
    </article>
  );
}
