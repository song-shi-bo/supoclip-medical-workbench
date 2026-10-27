"use client";

/**
 * 更新日志页面（客户端主体）。
 *
 * - 筛选状态全部反映在 URL 查询参数里（?view=dev&type=fix&module=timeline…），
 *   任何筛选结果都是一条可直接分享的链接。
 * - 公开条目随页面数据下发；内部条目仅在 dev 视图通过鉴权接口拉取。
 * - 概览区的「最新变化」始终取公开+已完成条目，与当前视图无关。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { ExternalLink, Rss } from "lucide-react";
import {
  filterUpdateEntries,
  formatUpdateDayHeading,
  groupUpdatesByDay,
  parseUpdateFilter,
  updateFilterToParams,
  updateHeadline,
  type TaggedUpdateEntry,
  type UpdateEntry,
  type UpdateFilter,
} from "@/lib/updates";
import { EntryCard } from "./entry-card";
import { FilterBar } from "./filter-bar";

const GITHUB_URL = "https://github.com/song-shi-bo/supoclip-medical-workbench";

type InternalState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ok"; entries: UpdateEntry[] }
  | { kind: "unauthorized" }
  | { kind: "error" };

export function UpdatesClient({ publicEntries }: { publicEntries: UpdateEntry[] }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const filter = useMemo(() => parseUpdateFilter(searchParams), [searchParams]);

  const [internal, setInternal] = useState<InternalState>({ kind: "idle" });
  // 防重入用 ref：不能把 internal.kind 放进 effect 依赖——effect 内部的
  // setInternal(loading) 会触发 cleanup，把进行中请求的结果整个作废掉。
  const internalRequested = useRef(false);

  // 内部条目只在首次进入 dev 视图时拉取一次；结果缓存在组件状态里，来回切换视图不重拉。
  useEffect(() => {
    if (filter.view !== "dev" || internalRequested.current) return;
    internalRequested.current = true;
    // 显式绝对化：浏览器里等价于相对路径请求；Node/jsdom 测试环境下相对 URL 无法解析。
    fetch(new URL("/api/updates/internal", window.location.origin).toString())
      .then(async (res) => {
        if (res.status === 401) return { kind: "unauthorized" } as InternalState;
        if (!res.ok) throw new Error(`status ${res.status}`);
        const body = (await res.json()) as { entries: UpdateEntry[] };
        return { kind: "ok", entries: body.entries } as InternalState;
      })
      .catch(() => ({ kind: "error" } as InternalState))
      .then((state) => setInternal(state));
  }, [filter.view]);

  const applyFilter = useCallback(
    (patch: Partial<UpdateFilter>) => {
      const next = { ...filter, ...patch };
      if (patch.view && patch.view !== "dev") next.visibility = "all";
      const params = updateFilterToParams(next);
      const qs = new URLSearchParams(params).toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [filter, pathname, router],
  );

  const tagged = useMemo<TaggedUpdateEntry[]>(() => {
    const publicTagged = publicEntries.map((entry) => ({ ...entry, visibility: "public" as const }));
    if (filter.view !== "dev" || internal.kind !== "ok") return publicTagged;
    return [...internal.entries.map((entry) => ({ ...entry, visibility: "internal" as const })), ...publicTagged];
  }, [publicEntries, filter.view, internal]);

  const visible = useMemo(() => filterUpdateEntries(tagged, filter), [tagged, filter]);
  const groups = useMemo(() => groupUpdatesByDay(visible), [visible]);
  const headline = useMemo(
    () => updateHeadline(publicEntries.map((entry) => ({ ...entry, visibility: "public" as const }))),
    [publicEntries],
  );

  return (
    <div className="space-y-8">
      <header className="space-y-4">
        <div className="space-y-2">
          <h1 className="text-2xl font-semibold tracking-tight">更新日志</h1>
          {headline ? (
            <p className="text-sm text-muted-foreground">
              <span className="font-medium text-foreground">最新 · {headline.date}</span>
              {"　"}
              {headline.title}——{headline.summary}
            </p>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <a
            href="/updates/rss.xml"
            className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium hover:bg-muted"
          >
            <Rss className="size-3.5" aria-hidden />
            RSS 订阅
          </a>
          <a
            href={GITHUB_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <ExternalLink className="size-3.5" aria-hidden />
            GitHub
          </a>
        </div>

        <div
          role="tablist"
          aria-label="更新视图"
          className="inline-flex rounded-lg border p-0.5 text-sm"
        >
          <button
            role="tab"
            aria-selected={filter.view === "user"}
            onClick={() => applyFilter({ view: "user" })}
            className={`rounded-md px-3 py-1.5 font-medium transition-colors ${
              filter.view === "user" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"
            }`}
          >
            用户更新
          </button>
          <button
            role="tab"
            aria-selected={filter.view === "dev"}
            onClick={() => applyFilter({ view: "dev" })}
            className={`rounded-md px-3 py-1.5 font-medium transition-colors ${
              filter.view === "dev" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"
            }`}
          >
            开发日志
          </button>
        </div>
      </header>

      <FilterBar filter={filter} onChange={applyFilter} />

      {filter.view === "dev" && internal.kind === "unauthorized" ? (
        <p className="rounded-md border border-dashed px-4 py-3 text-sm text-muted-foreground">
          登录后可查看内部条目，当前仅显示公开条目。
        </p>
      ) : null}
      {filter.view === "dev" && internal.kind === "error" ? (
        <p className="rounded-md border border-dashed px-4 py-3 text-sm text-muted-foreground">
          内部条目加载失败，当前仅显示公开条目。
        </p>
      ) : null}

      {groups.length === 0 ? (
        <p className="rounded-md border border-dashed px-4 py-10 text-center text-sm text-muted-foreground">
          没有符合条件的更新
        </p>
      ) : (
        <div className="space-y-10">
          {groups.map((group) => (
            <section key={group.date} data-date={group.date} className="space-y-4">
              <h2 className="flex items-center gap-2 border-b pb-2 text-sm font-medium text-muted-foreground">
                {formatUpdateDayHeading(group.date)}
                {group.version ? (
                  <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{group.version}</span>
                ) : null}
              </h2>
              <div className="space-y-4">
                {group.entries.map((entry) => (
                  <EntryCard key={`${entry.visibility}-${entry.id}`} entry={entry} devView={filter.view === "dev"} />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}

      <p className="border-t pt-4 text-xs text-muted-foreground">
        用户更新视图只展示已完成的公开变化；开发日志视图包含全部工作记录。筛选结果可直接复制地址栏分享。
      </p>
    </div>
  );
}
