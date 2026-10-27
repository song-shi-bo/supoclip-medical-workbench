"use client";

/**
 * 筛选栏：类型 / 模块 / 时间范围 /（dev 视图）可见性 + 关键词搜索。
 * 纯展示组件，筛选状态由父组件写入 URL。工作台模块排首位并有快捷入口样式。
 */
import { Search, X } from "lucide-react";
import {
  UPDATE_MODULES,
  UPDATE_RANGES,
  UPDATE_TYPES,
  type UpdateFilter,
} from "@/lib/updates";

interface FilterBarProps {
  filter: UpdateFilter;
  onChange: (patch: Partial<UpdateFilter>) => void;
}

function Chip({
  active,
  onClick,
  children,
  title,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      title={title}
      onClick={onClick}
      className={`rounded-full border px-2.5 py-1 text-xs transition-colors ${
        active
          ? "border-primary bg-primary text-primary-foreground"
          : "text-muted-foreground hover:border-foreground/30 hover:text-foreground"
      }`}
    >
      {children}
    </button>
  );
}

export function FilterBar({ filter, onChange }: FilterBarProps) {
  const hasQuery = filter.q.length > 0;

  return (
    <div className="space-y-3 rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="mr-1 text-xs font-medium text-muted-foreground">类型</span>
        <Chip active={filter.type === "all"} onClick={() => onChange({ type: "all" })}>
          全部
        </Chip>
        {UPDATE_TYPES.map((type) => (
          <Chip
            key={type.id}
            active={filter.type === type.id}
            onClick={() => onChange({ type: type.id })}
          >
            {type.label}
          </Chip>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        <span className="mr-1 text-xs font-medium text-muted-foreground">模块</span>
        {UPDATE_MODULES.map((module, index) => (
          <Chip
            key={module.id}
            active={filter.module === module.id}
            onClick={() => onChange({ module: filter.module === module.id ? "all" : module.id })}
            title={index === 0 ? "工作台核心操作台的变化" : undefined}
          >
            {index === 0 ? `⚡ ${module.label}` : module.label}
          </Chip>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        <span className="mr-1 text-xs font-medium text-muted-foreground">时间</span>
        {UPDATE_RANGES.map((range) => (
          <Chip
            key={range.id}
            active={filter.range === range.id}
            onClick={() => onChange({ range: range.id })}
          >
            {range.label}
          </Chip>
        ))}

        {filter.view === "dev" ? (
          <>
            <span className="mx-1 text-xs text-muted-foreground">|</span>
            <span className="mr-1 text-xs font-medium text-muted-foreground">可见性</span>
            {(
              [
                { id: "all", label: "全部" },
                { id: "public", label: "公开" },
                { id: "internal", label: "内部" },
              ] as const
            ).map((visibility) => (
              <Chip
                key={visibility.id}
                active={filter.visibility === visibility.id}
                onClick={() => onChange({ visibility: visibility.id })}
              >
                {visibility.label}
              </Chip>
            ))}
          </>
        ) : null}
      </div>

      <div className="relative">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" aria-hidden />
        <input
          type="search"
          value={filter.q}
          onChange={(event) => onChange({ q: event.target.value })}
          placeholder="搜索标题、摘要、模块…"
          aria-label="搜索更新"
          className="w-full rounded-md border bg-transparent py-1.5 pl-8 pr-8 text-sm outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring"
        />
        {hasQuery ? (
          <button
            type="button"
            onClick={() => onChange({ q: "" })}
            aria-label="清除搜索"
            className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
          >
            <X className="size-3.5" aria-hidden />
          </button>
        ) : null}
      </div>
    </div>
  );
}
