"use client";
/**
 * 项目管理弹层（2026-10-19）：左上角小房子按钮打开的近全屏浮层。
 *
 * 契约：
 * - 不新开标签页、不跳转路由、不刷新页面：本组件由工作台页内条件渲染，``open=false``
 *   时返回 null；工作台始终挂载在弹层之下，关闭弹层不触碰任何工作台状态，编辑状态天然保留。
 * - Esc / 关闭按钮只关弹层。
 * - 数据源：``GET {base}/studio/projects/list``（后端按当前主体可见性过滤 + 服务端
 *   搜索/排序/分页，返回 {items,total,page,page_size}）；缩略图走
 *   ``GET {base}/studio/projects/{id}/thumbnail.jpg``（源视频抽帧，404 → 占位图标）。
 *   搜索输入 300ms 防抖后触发服务端查询并回到第 1 页。
 * - 「打开项目 / 新建项目」先经 ``onSaveCurrentEdits`` 保存当前编辑；随后 ``onOpenProject``
 *   做只读预检并加载项目。onOpenProject resolve false = 打开失败（弹层保持打开并提示，
 *   当前项目不动）；成功后由页面在同一工作台内切换，本组件关闭弹层。
 * - 配色沿用 studio.css 的既有变量，不引入新色板。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Clock, Ellipsis, FolderOpen, LayoutGrid, List, LoaderCircle, Plus, Search, X,
} from "lucide-react";
import { formatProjectTime, projectStatusChip, type ProjectSortKey } from "./project-status";

export type ProjectListItem = {
  project_id: string;
  name: string | null;
  card_count: number;
  updated_at: string;
  status: string | null;
  stage: string | null;
  /** timeline 工程（project.json）存在才可打开；false = 孤儿台账（仅存档），点击不切换。 */
  openable?: boolean;
  /** 源片文件在 → 才请求缩略图（无源片不打必然 404 的请求）。 */
  has_source?: boolean;
  /** 项目生命周期：active/archived/deleting/delete_failed/deleted（缺省 active）。 */
  lifecycle?: string;
};

export type DeletePreviewItem = { kind: string; path: string; bytes: number; shared: boolean; nlink: number | null };
export type DeletePreviewPayload = {
  project_id: string;
  items: DeletePreviewItem[];
  reclaim_bytes: number;
  shared_bytes: number;
  note?: string;
};
export type DeleteStatusPayload = {
  project_id: string;
  project_status: string;
  manifest_status: string | null;
  items_total: number;
  items_done: number;
  items_failed: number;
  items: { kind: string | null; path: string | null; state: string | null; error: string | null }[];
  errors?: string[];
};

export type ProjectListPayload = { items: ProjectListItem[]; total: number; page: number; page_size: number };

const SORT_OPTIONS: { key: ProjectSortKey; label: string }[] = [
  { key: "updated_desc", label: "最近更新优先" },
  { key: "updated_asc", label: "最早更新优先" },
  { key: "name", label: "按名称 A→Z" },
  { key: "cards", label: "按成片数量" },
];

export type ProjectExportsItem = {
  clip_id: string;
  name: string | null;
  duration: number | null;
  source_start: number | null;
  source_end: number | null;
  cover_path: string | null;
  mp4_url: string | null;
  srt_url: string | null;
};

export type ProjectExportsPayload = { project_id: string; items: ProjectExportsItem[] };

export type ProjectManagerProps = {
  open: boolean;
  /** 同源 API 前缀（工作台为 "/video-api"）。 */
  base: string;
  activeProjectId: string | null;
  onClose: () => void;
  /** 保存当前编辑；resolve false = 保存失败（弹层保持打开并提示，当前项目不动）。 */
  onSaveCurrentEdits: () => Promise<boolean>;
  /** 保存成功后打开项目：页面做只读预检并加载，resolve false = 失败（保留当前项目）。
   *  meta.hasSource=false 时页面会提示「重新关联原视频」（工程在、源片缺失态）。 */
  onOpenProject: (projectId: string, meta?: { hasSource?: boolean }) => Promise<boolean> | boolean | void;
  /** 保存成功后新建项目（页面清空为待导入工作区）。 */
  onNewProject: () => void;
  /** 归档/恢复/删除完成后刷新当前列表。 */
  onRefreshList?: () => void;
};

const PAGE_SIZE = 12;

export function ProjectManager(props: ProjectManagerProps) {
  const { open, base, activeProjectId, onClose, onSaveCurrentEdits, onOpenProject, onNewProject } = props;
  const [items, setItems] = useState<ProjectListItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [viewMode, setViewMode] = useState<"grid" | "list">("grid");
  const [order, setOrder] = useState<ProjectSortKey>("updated_desc");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [switching, setSwitching] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  // 删除流程：preview（待确认）→ running（轮询进度）→ done/failed
  const [deleteFor, setDeleteFor] = useState<ProjectListItem | null>(null);
  const [deletePreview, setDeletePreview] = useState<DeletePreviewPayload | null>(null);
  const [deletePhase, setDeletePhase] = useState<"preview" | "running" | "done" | "failed" | null>(null);
  const [deleteStatus, setDeleteStatus] = useState<DeleteStatusPayload | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [confirmChecked, setConfirmChecked] = useState(false);
  // 打开代际：关闭/重开后，慢响应不得写回新一轮状态（StrictMode 双挂载同样安全）。
  const loadSeq = useRef(0);
  const loadAbort = useRef<AbortController | null>(null);

  // 搜索防抖：输入停 300ms 才真正触发服务端查询，并回到第 1 页。
  useEffect(() => {
    if (!open) return undefined;
    const timer = window.setTimeout(() => {
      setQuery((previous) => {
        const next = queryInput.trim();
        if (previous !== next) setPage(1);
        return next;
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [queryInput, open]);

  const loadProjects = useCallback(async (signal: AbortController, effectiveQuery: string,
    effectivePage: number, effectiveOrder: ProjectSortKey, effectiveShowArchived: boolean) => {
    const seq = ++loadSeq.current;
    loadAbort.current?.abort();
    loadAbort.current = signal;
    setLoading(true); setLoadError(null);
    const params = new URLSearchParams({
      q: effectiveQuery, page: String(effectivePage), page_size: String(PAGE_SIZE),
      order: effectiveOrder, include_archived: effectiveShowArchived ? "1" : "0",
    });
    try {
      const response = await fetch(`${base}/studio/projects/list?${params.toString()}`,
        { cache: "no-store", signal: signal.signal });
      if (!response.ok) throw new Error(`无法读取项目列表（HTTP ${response.status}）`);
      const payload = await response.json() as ProjectListPayload;
      if (seq !== loadSeq.current) return;
      setItems(Array.isArray(payload.items) ? payload.items : []);
      setTotal(Number(payload.total) || 0);
      setPage(Number(payload.page) || 1);
    } catch (error) {
      if (signal.signal.aborted || seq !== loadSeq.current) return;
      setItems([]);
      setTotal(0);
      setLoadError(error instanceof Error ? error.message : "无法读取项目列表");
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }, [base]);

  useEffect(() => {
    if (!open) return undefined;
    const controller = new AbortController();
    void loadProjects(controller, query, page, order, showArchived);
    return () => { controller.abort(); };
  }, [open, query, page, order, showArchived, loadProjects]);

  // Esc 只关弹层：工作台不动（capture 拦截，避免同时触发工作台内部的 Esc 语义）。
  useEffect(() => {
    if (!open) return undefined;
    const handler = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      onClose();
    };
    document.addEventListener("keydown", handler, true);
    return () => document.removeEventListener("keydown", handler, true);
  }, [open, onClose]);

  // ===== 三态之二：成果查看/下载（工程缺失、成片存在）=====
  const [exportsFor, setExportsFor] = useState<ProjectListItem | null>(null);
  const [deleteConfirmChecked, setDeleteConfirmChecked] = useState(false);
  const [exportsItems, setExportsItems] = useState<ProjectExportsItem[] | null>(null);
  const [exportsError, setExportsError] = useState<string | null>(null);
  const [exportsLoading, setExportsLoading] = useState(false);
  const exportsSeq = useRef(0);

  if (!open) return null;

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const runSwitch = async (busyKey: string, action: () => Promise<boolean> | boolean | void) => {
    if (switching) return;
    setActionError(null); setMenuFor(null);
    setSwitching(busyKey);
    let saved = false;
    try { saved = await onSaveCurrentEdits(); } catch { saved = false; }
    if (!saved) {
      setSwitching(null);
      setActionError("保存当前项目失败：已保留当前项目与编辑，请稍后重试。");
      return;
    }
    let opened: boolean | void = true;
    try { opened = await action(); } catch { opened = false; }
    if (opened === false) {
      setSwitching(null);
      setActionError("打开项目失败：已保留当前项目与编辑。");
      return;
    }
    setSwitching(null);
    onClose();
  };

  const copyProjectId = (projectId: string) => {
    setMenuFor(null);
    void navigator.clipboard?.writeText(projectId).then(() => {
      setCopiedId(projectId);
      window.setTimeout(() => setCopiedId((current) => (current === projectId ? null : current)), 1500);
    }).catch(() => setCopiedId(null));
  };

  const lifecycleArchived = (project: ProjectListItem) => (project.lifecycle || "active") === "archived";
  const lifecycleBusy = (project: ProjectListItem) =>
    ["deleting", "delete_failed", "deleted"].includes(project.lifecycle || "active");

  const openProject = (project: ProjectListItem) => {
    setMenuFor(null);
    if (project.project_id === activeProjectId) { onClose(); return; }  // 已是当前项目：只收起弹层，不动编辑状态
    if (lifecycleBusy(project)) {
      // 删除中/删除失败/已删除：打开删除进度面板，禁止其它操作。
      void openDeleteProgress(project);
      return;
    }
    if (project.openable === false) {
      if ((project.card_count || 0) > 0) {
        // 三态之二（工程缺失、成片存在）：可查看/下载成果，不冒充可编辑工程。
        void openExports(project);
        return;
      }
      // 三态之外（无工程也无成片）：纯存档残留，无法提供任何成果。
      setActionError("该项目没有工作台工程档案，也没有可下载的成片。");
      return;
    }
    const hasSource = project.has_source !== false;
    void runSwitch(project.project_id, () => onOpenProject(project.project_id, { hasSource }));
  };

  const openExports = async (project: ProjectListItem) => {
    setActionError(null); setMenuFor(null);
    setExportsFor(project); setExportsItems(null); setExportsError(null); setExportsLoading(true);
    const seq = ++exportsSeq.current;
    try {
      const response = await fetch(`${base}/studio/projects/${encodeURIComponent(project.project_id)}/exports`,
        { cache: "no-store" });
      if (!response.ok) throw new Error(`无法读取成果清单（HTTP ${response.status}）`);
      const payload = await response.json() as ProjectExportsPayload;
      if (seq !== exportsSeq.current) return;
      setExportsItems(Array.isArray(payload.items) ? payload.items : []);
    } catch (error) {
      if (seq !== exportsSeq.current) return;
      setExportsError(error instanceof Error ? error.message : "无法读取成果清单");
    } finally {
      if (seq === exportsSeq.current) setExportsLoading(false);
    }
  };

  const closeExports = () => { setExportsFor(null); setExportsItems(null); setExportsError(null); };

  // ===== 归档 / 恢复 / 删除（生命周期，2026-10-20）=====

  const archiveProject = async (project: ProjectListItem) => {
    setMenuFor(null); setActionError(null);
    try {
      const response = await fetch(`${base}/studio/projects/${encodeURIComponent(project.project_id)}/archive`,
        { method: "POST", cache: "no-store" });
      if (!response.ok) throw new Error(`归档失败（HTTP ${response.status}）`);
      await loadProjects(new AbortController(), query, page, order, showArchived);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "归档失败");
    }
  };

  const restoreArchived = async (project: ProjectListItem) => {
    setMenuFor(null); setActionError(null);
    try {
      const response = await fetch(`${base}/studio/projects/${encodeURIComponent(project.project_id)}/restore`,
        { method: "POST", cache: "no-store" });
      if (!response.ok) throw new Error(`恢复失败（HTTP ${response.status}）`);
      await loadProjects(new AbortController(), query, page, order, showArchived);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "恢复失败");
    }
  };

  const openDeleteFlow = async (project: ProjectListItem) => {
    setMenuFor(null); setExportsFor(null); setActionError(null);
    setDeleteFor(project); setDeletePreview(null); setDeleteError(null);
    setDeletePhase("preview"); setDeleteConfirmChecked(false);
    try {
      const response = await fetch(`${base}/studio/projects/${encodeURIComponent(project.project_id)}/delete/preview`,
        { cache: "no-store" });
      if (!response.ok) throw new Error(`无法读取删除预览（HTTP ${response.status}）`);
      setDeletePreview(await response.json() as DeletePreviewPayload);
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : "无法读取删除预览");
    }
  };

  const openDeleteProgress = async (project: ProjectListItem) => {
    setMenuFor(null); setExportsFor(null); setActionError(null);
    setDeleteFor(project); setDeletePreview(null); setDeleteError(null);
    setDeletePhase("running"); setDeleteConfirmChecked(false);
    const outcome = await pollDeleteStatus(project.project_id);
    if (outcome === "done") {
      setDeletePhase("done");
      await loadProjects(new AbortController(), query, page, order, showArchived);
    } else if (outcome === "failed") {
      setDeletePhase("failed");
    }
  };

  const closeDeleteFlow = () => {
    setDeleteFor(null); setDeletePreview(null); setDeletePhase(null); setDeleteError(null); setDeleteConfirmChecked(false);
  };

  const pollDeleteStatus = async (projectId: string) => {
    // 轮询删除进度：deleted → done；delete_failed → failed；上限 ~100×250ms
    for (let attempt = 0; attempt < 100; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      try {
        const response = await fetch(`${base}/studio/projects/${encodeURIComponent(projectId)}/delete/status`,
          { cache: "no-store" });
        if (!response.ok) continue;
        const status = await response.json() as DeleteStatusPayload;
        setDeleteStatus(status);
        if (status.project_status === "deleted") return "done";
        if (status.project_status === "delete_failed") return "failed";
      } catch { /* 单次轮询失败继续 */ }
    }
    return "timeout";
  };

  const confirmDelete = async () => {
    if (!deleteFor || deletePhase !== "preview") return;
    setDeletePhase("running"); setDeleteStatus(null);
    try {
      const response = await fetch(`${base}/studio/projects/${encodeURIComponent(deleteFor.project_id)}/delete`,
        { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirmed: true }), cache: "no-store" });
      if (!response.ok) throw new Error(`删除启动失败（HTTP ${response.status}）`);
    } catch (error) {
      setDeletePhase("failed");
      setDeleteError(error instanceof Error ? error.message : "删除启动失败");
      return;
    }
    const outcome = await pollDeleteStatus(deleteFor.project_id);
    if (outcome === "done") {
      setDeletePhase("done");
      await loadProjects(new AbortController(), query, page, order, showArchived);
    } else {
      setDeletePhase("failed");
      setDeleteError(outcome === "timeout" ? "删除超时：请用「查看进度」继续观察。" : "删除失败：可重试清理。");
    }
  };

  const retryDelete = async () => {
    if (!deleteFor) return;
    setDeleteError(null);
    try {
      const response = await fetch(`${base}/studio/projects/${encodeURIComponent(deleteFor.project_id)}/delete/retry`,
        { method: "POST", cache: "no-store" });
      if (!response.ok) throw new Error(`重试失败（HTTP ${response.status}）`);
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : "重试失败");
      return;
    }
    const outcome = await pollDeleteStatus(deleteFor.project_id);
    if (outcome === "done") {
      setDeletePhase("done");
      await loadProjects(new AbortController(), query, page, order, showArchived);
    } else {
      setDeletePhase("failed");
      setDeleteError("重试后仍未完成：请稍后再试或联系管理员。");
    }
  };

  const renderThumb = (project: ProjectListItem, className: string) => (
    <div className={`pm-thumb ${className}`}>
      {project.has_source !== false && (
        <img
          src={`${base}/studio/projects/${encodeURIComponent(project.project_id)}/thumbnail.jpg`}
          alt=""
          loading="lazy"
          onError={(event) => { event.currentTarget.style.display = "none"; }}
          onLoad={(event) => { event.currentTarget.style.display = ""; }}
        />
      )}
      <FolderOpen size={26} aria-hidden className="pm-thumb-fallback" />
    </div>
  );

  const renderMeta = (project: ProjectListItem) => {
    const label = (project.name || "").trim() || project.project_id;
    const chip = projectStatusChip(project.status);
    const clipsOnly = project.openable === false && (project.card_count || 0) > 0;
    const lc = project.lifecycle || "active";
    const lcLabel = { archived: "已归档", deleting: "删除中", delete_failed: "删除失败", deleted: "已删除" }[lc];
    return (
      <div className="pm-meta">
        <strong className="pm-name" title={label}>{label}</strong>
        <span className="pm-sub">
          {lcLabel && (
            <span className={`pm-status tone-${lc === "delete_failed" ? "bad" : lc === "deleting" ? "running" : "idle"}`}>
              {lcLabel}
            </span>
          )}
          {lcLabel && <span aria-hidden>·</span>}
          {project.openable === false && (
            <span className={`pm-status ${clipsOnly ? "tone-warn" : "tone-idle"}`}>
              {clipsOnly ? "无法恢复原编辑工程" : "无工程档案"}
            </span>
          )}
          {project.openable === false && <span aria-hidden>·</span>}
          <span className={`pm-status tone-${chip.tone}`}>{chip.label}</span>
          <span aria-hidden>·</span>{project.card_count} 个成片
          <span aria-hidden>·</span><Clock size={11} aria-hidden /> {formatProjectTime(project.updated_at)}
        </span>
        {project.stage && <span className="pm-stage" title={project.stage}>{project.stage}</span>}
      </div>
    );
  };

  const renderMore = (project: ProjectListItem) => (
    <div className="pm-more" onClick={(event) => event.stopPropagation()}>
      <button
        type="button"
        className="pm-more-toggle"
        aria-label={`${(project.name || "").trim() || project.project_id}更多操作`}
        aria-expanded={menuFor === project.project_id}
        title="更多操作"
        onClick={() => setMenuFor(menuFor === project.project_id ? null : project.project_id)}
      ><Ellipsis size={16} /></button>
      {menuFor === project.project_id && (
        <div className="pm-menu" role="menu">
          {!lifecycleBusy(project) && (
            <button type="button" role="menuitem" onClick={() => openProject(project)}>打开项目</button>
          )}
          {(project.lifecycle || "active") === "active" && (
            <button type="button" role="menuitem"
              onClick={() => { setMenuFor(null); void archiveProject(project); }}>归档</button>
          )}
          {(project.lifecycle || "active") === "archived" && (
            <button type="button" role="menuitem" onClick={() => { setMenuFor(null); void restoreArchived(project); }}>恢复</button>
          )}
          <button type="button" role="menuitem" onClick={() => copyProjectId(project.project_id)}>复制项目 ID</button>
          {(project.lifecycle || "active") !== "deleted" && (
            <button type="button" role="menuitem" className="pm-menu-danger"
              onClick={() => { setMenuFor(null); void openDeleteFlow(project); }}>
              {(project.card_count || 0) > 0 || project.openable ? "删除…" : "删除（无成果，仅清记录）…"}
            </button>
          )}
          {(project.lifecycle || "active") === "deleting" && (
            <button type="button" role="menuitem" onClick={() => { setMenuFor(null); void openDeleteProgress(project); }}>查看进度</button>
          )}
          {(project.lifecycle || "active") === "delete_failed" && (
            <button type="button" role="menuitem" onClick={() => { setMenuFor(null); void retryDelete(); }}>重试清理</button>
          )}
        </div>
      )}
    </div>
  );

  const renderCard = (project: ProjectListItem) => {
    const isCurrent = project.project_id === activeProjectId;
    const clipsOnly = project.openable === false && (project.card_count || 0) > 0;
    const archived = (project.lifecycle || "active") === "archived"
      || (project.openable === false && !clipsOnly);
    // key 必须直接写在 JSX 上，不能混进展开对象（React 19 禁止 {...props} 携带 key）。
    return (
      <div
        key={project.project_id}
        role="button"
        tabIndex={0}
        className={`pm-card${isCurrent ? " current" : ""}${archived ? " archived" : ""}`}
        onClick={() => openProject(project)}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openProject(project); }
        }}
        title={isCurrent ? "当前项目：点击返回工作台"
          : clipsOnly ? "工程缺失：点击查看/下载已成片（无法恢复原编辑工程）"
          : archived ? "无工作台工程档案，也没有可下载的成片"
          : "打开该项目"}
      >
        {isCurrent && <span className="pm-badge">当前</span>}
        {renderThumb(project, "")}
        {renderMeta(project)}
        {renderMore(project)}
      </div>
    );
  };

  const renderRow = (project: ProjectListItem) => {
    const isCurrent = project.project_id === activeProjectId;
    const clipsOnly = project.openable === false && (project.card_count || 0) > 0;
    const archived = (project.lifecycle || "active") === "archived"
      || (project.openable === false && !clipsOnly);
    return (
      <div
        key={project.project_id}
        role="button"
        tabIndex={0}
        className={`pm-row${isCurrent ? " current" : ""}${archived ? " archived" : ""}`}
        onClick={() => openProject(project)}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openProject(project); }
        }}
        title={isCurrent ? "当前项目：点击返回工作台"
          : clipsOnly ? "工程缺失：点击查看/下载已成片（无法恢复原编辑工程）"
          : archived ? "无工作台工程档案，也没有可下载的成片"
          : "打开该项目"}
      >
        {isCurrent && <span className="pm-badge">当前</span>}
        {renderThumb(project, "pm-thumb-row")}
        {renderMeta(project)}
        {renderMore(project)}
      </div>
    );
  };

  return (
    <div className="pm-overlay" onClick={() => setMenuFor(null)}>
      <section className="pm-panel" role="dialog" aria-modal="true" aria-label="项目管理">
        <header className="pm-header">
          <h2><FolderOpen size={17} aria-hidden />项目管理</h2>
          <div className="pm-search">
            <Search size={15} aria-hidden />
            <input
              value={queryInput}
              onChange={(event) => setQueryInput(event.target.value)}
              placeholder="搜索项目名称或 ID…"
              aria-label="搜索项目"
            />
          </div>
          <div className="pm-tools">
            <button
              type="button"
              className={`pm-arch-toggle${showArchived ? " on" : ""}`}
              aria-pressed={showArchived}
              title="显示/隐藏已归档项目"
              onClick={() => { setShowArchived((v) => !v); setPage(1); }}
            >已归档</button>
            <div className="pm-seg" role="group" aria-label="视图切换">
              <button
                type="button"
                className={viewMode === "grid" ? "on" : ""}
                aria-label="网格视图"
                aria-pressed={viewMode === "grid"}
                title="网格视图"
                onClick={() => setViewMode("grid")}
              ><LayoutGrid size={15} /></button>
              <button
                type="button"
                className={viewMode === "list" ? "on" : ""}
                aria-label="列表视图"
                aria-pressed={viewMode === "list"}
                title="列表视图"
                onClick={() => setViewMode("list")}
              ><List size={15} /></button>
            </div>
            <select
              className="pm-sort"
              value={order}
              onChange={(event) => { setOrder(event.target.value as ProjectSortKey); setPage(1); }}
              aria-label="排序方式"
            >
              {SORT_OPTIONS.map((option) => (
                <option key={option.key} value={option.key}>{option.label}</option>
              ))}
            </select>
            <button
              type="button"
              className="pm-close"
              aria-label="关闭项目管理"
              title="关闭（Esc）"
              onClick={onClose}
            ><X size={18} /></button>
          </div>
        </header>
        <div className="pm-body">
          {deleteFor && (
            <section className="pm-delete" aria-label={`删除项目：${(deleteFor.name || "").trim() || deleteFor.project_id}`}>
              <header className="pm-exports-head">
                <strong>删除「{(deleteFor.name || "").trim() || deleteFor.project_id}」</strong>
                <button type="button" className="pm-close" aria-label="关闭删除流程" title="关闭"
                  onClick={closeDeleteFlow}><X size={16} /></button>
              </header>
              {deletePhase === "preview" && (
                deleteError ? <p className="pm-error">{deleteError}</p> : (
                  deletePreview ? (
                    <>
                      <p className="pm-delete-note">
                        将永久删除以下数据（{deletePreview.reclaim_bytes > 1048576
                          ? `约 ${(deletePreview.reclaim_bytes / 1048576).toFixed(1)} MB`
                          : `${deletePreview.reclaim_bytes} 字节`}）。共享硬链接仅摘除本项目引用
                        {deletePreview.shared_bytes > 0 && <>（共享 {deletePreview.shared_bytes} 字节不计入释放量）</>}。</p>
                      <ul className="pm-delete-items">
                        {deletePreview.items.map((item) => (
                          <li key={item.path} className="pm-delete-item">
                            <span className="pm-delete-kind">{item.kind}</span>
                            <span className="pm-delete-path" title={item.path}>{item.path}</span>
                            <span className="pm-delete-bytes">
                              {item.bytes > 1048576 ? `${(item.bytes / 1048576).toFixed(1)} MB` : `${item.bytes} B`}
                              {item.shared ? " · 共享" : ""}
                            </span>
                          </li>
                        ))}
                      </ul>
                      <label className="pm-delete-confirm">
                        <input type="checkbox" checked={deleteConfirmChecked}
                          onChange={(event) => setDeleteConfirmChecked(event.target.checked)} />
                        我确认永久删除该项目全部数据（含源片引用、成片、字幕、封面与缓存）
                      </label>
                      <button type="button" className="pm-delete-btn" disabled={!deleteConfirmChecked}
                        onClick={() => void confirmDelete()}>确认删除</button>
                    </>
                  ) : <div className="pm-state"><LoaderCircle size={18} className="pm-spin" aria-label="正在读取删除预览" /></div>
                )
              )}
              {deletePhase === "running" && (
                <div className="pm-delete-progress" aria-live="polite">
                  <LoaderCircle size={16} className="pm-spin" aria-hidden />
                  正在删除… {deleteStatus ? `${deleteStatus.items_done}/${deleteStatus.items_total} 项完成` : ""}
                  {deleteStatus && deleteStatus.items_failed > 0 && ` · ${deleteStatus.items_failed} 项失败`}
                </div>
              )}
              {deletePhase === "done" && (
                <div className="pm-delete-progress" role="status">项目已删除。列表已刷新。</div>
              )}
              {deletePhase === "failed" && (
                <div className="pm-delete-progress" role="alert">
                  删除未完成{deleteStatus && deleteStatus.items_failed > 0 ? `（${deleteStatus.items_failed} 项失败）` : ""}
                  ：项目已进入「删除失败」状态，可重试清理。
                  <button type="button" className="pm-retry" onClick={() => void retryDelete()}>重试清理</button>
                </div>
              )}
            </section>
          )}
          {exportsFor && (
            <section className="pm-exports" aria-label={`成果清单：${(exportsFor.name || "").trim() || exportsFor.project_id}`}>
              <header className="pm-exports-head">
                <strong>{(exportsFor.name || "").trim() || exportsFor.project_id} · 成果清单</strong>
                <span className="pm-exports-note">无法恢复原编辑工程：以下为已发布成片，可预览/下载；如需重新剪辑，请重新导入源视频。</span>
                <button type="button" className="pm-close" aria-label="关闭成果清单" title="关闭" onClick={closeExports}><X size={16} /></button>
              </header>
              {exportsLoading && <div className="pm-state"><LoaderCircle size={18} className="pm-spin" aria-label="正在读取成果清单" /></div>}
              {exportsError && <p className="pm-error">{exportsError}</p>}
              {exportsItems && exportsItems.length === 0 && <div className="pm-state">该台账没有成片卡片。</div>}
              {exportsItems && exportsItems.length > 0 && (
                <ul className="pm-exports-list">
                  {exportsItems.map((item) => (
                    <li key={item.clip_id} className="pm-exports-row">
                      <span className="pm-exports-name">{item.name || item.clip_id}</span>
                      <span className="pm-exports-meta">
                        {item.duration != null && <>{Math.round(item.duration / 60)}分{Math.round(item.duration % 60)}秒 · </>}
                        {item.source_start != null && item.source_end != null &&
                          <>源区间 {Math.round(item.source_start)}s–{Math.round(item.source_end)}s</>}
                      </span>
                      <span className="pm-exports-actions">
                        {item.mp4_url && <a className="pm-exports-btn" href={item.mp4_url} target="_blank" rel="noreferrer">预览</a>}
                        {item.mp4_url && <a className="pm-exports-btn" href={item.mp4_url} download>{item.mp4_url ? "下载 MP4" : ""}</a>}
                        {item.srt_url && <a className="pm-exports-btn" href={item.srt_url} download>下载 SRT</a>}
                        {!item.mp4_url && <span className="pm-exports-none">成片文件缺失</span>}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          )}
          {loadError && (
            <div className="pm-state">
              <p className="pm-error">{loadError}</p>
              <button
                type="button"
                className="pm-retry"
                onClick={() => void loadProjects(new AbortController(), query, page, order, showArchived)}
              >重试</button>
            </div>
          )}
          {!loadError && items === null && (
            <div className="pm-state"><LoaderCircle size={20} className="pm-spin" aria-label="正在加载项目列表" /></div>
          )}
          {!loadError && items !== null && (
            <>
              {loading && <div className="pm-loading" aria-label="正在刷新"><LoaderCircle size={16} className="pm-spin" aria-hidden /> 正在刷新…</div>}
              {viewMode === "grid"
                ? <div className="pm-grid">
                    <button
                      type="button"
                      className="pm-card pm-new"
                      onClick={() => void runSwitch("__new__", onNewProject)}
                      disabled={switching !== null}
                      title="保存当前项目后，切换到新的空白工作区"
                    >
                      <Plus size={30} aria-hidden />
                      <span className="pm-new-label">新建项目</span>
                      <span className="pm-new-hint">导入素材后自动创建</span>
                    </button>
                    {items.map(renderCard)}
                  </div>
                : <div className="pm-list">
                    <button
                      type="button"
                      className="pm-row pm-new"
                      onClick={() => void runSwitch("__new__", onNewProject)}
                      disabled={switching !== null}
                      title="保存当前项目后，切换到新的空白工作区"
                    >
                      <Plus size={20} aria-hidden />
                      <span className="pm-new-label">新建项目</span>
                      <span className="pm-new-hint">导入素材后自动创建</span>
                    </button>
                    {items.map(renderRow)}
                  </div>}
              {items.length === 0 && !loading && (
                <div className="pm-state">{query ? `没有匹配「${query}」的项目。` : "还没有项目：导入素材后自动创建。"}</div>
              )}
              {total > 0 && (
                <footer className="pm-pager">
                  <span className="pm-pager-info">共 {total} 个项目 · 第 {page}/{totalPages} 页</span>
                  <button
                    type="button"
                    onClick={() => setPage((current) => Math.max(1, current - 1))}
                    disabled={page <= 1 || loading}
                    aria-label="上一页"
                  >上一页</button>
                  <button
                    type="button"
                    onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
                    disabled={page >= totalPages || loading}
                    aria-label="下一页"
                  >下一页</button>
                </footer>
              )}
            </>
          )}
          {actionError && <div className="pm-action-error" role="alert">{actionError}</div>}
          {copiedId && <div className="pm-copied" role="status">已复制 {copiedId}</div>}
          {switching && (
            <div className="pm-switching" aria-live="polite">
              <LoaderCircle size={18} className="pm-spin" aria-hidden />
              {switching === "__new__" ? "正在保存并新建项目…" : "正在保存并切换项目…"}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
