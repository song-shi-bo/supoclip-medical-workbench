"use client";

// 选题板（阶段3 M4）：热点 → 选题卡 → 医生原创稿 的看板与操作台。
// 数据全部来自后端 /video-api/api/v1/topics（登录态由同源 cookie 承载）。
// 权限模型：任何人可看板/拆解/保存/生稿；两级审批仅 topic_reviewers 白名单医生；
// 白名单管理仅管理员（探测 403 后自动隐藏入口）。

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  ArrowLeft,
  ClipboardList,
  FileText,
  LoaderCircle,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  UserCheck,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";
import {
  createTopicsApi,
  groupTopicsByStatus,
  SCRIPT_ORIGIN_LABELS,
  SCRIPT_STATUS_LABELS,
  TOPIC_STATUSES,
  TOPIC_STATUS_LABELS,
  TOPIC_STATUS_LABELS as STATUS_LABELS,
  type BriefDraftResult,
  type GenerateScriptResult,
  type ReviewerItem,
  type ScriptItem,
  type TopicItem,
  type TopicsApiError,
} from "@/lib/topics-api";

const API_BASE = "/video-api";
const api = createTopicsApi(API_BASE);

interface BriefFormState {
  userQuestion: string;
  duration: string;
  targetAudience: string;
  contentType: string;
  hookType: string;
  structure: string;
  cta: string;
  businessGoal: string;
  coreConclusion: string;
  riskBoundary: string;
  prerequisites: string;
  forbiddenExpr: string;
}

const EMPTY_FORM: BriefFormState = {
  userQuestion: "",
  duration: "60",
  targetAudience: "",
  contentType: "60秒口播科普",
  hookType: "",
  structure: "",
  cta: "",
  businessGoal: "",
  coreConclusion: "",
  riskBoundary: "",
  prerequisites: "",
  forbiddenExpr: "",
};

function formFromTopic(topic: TopicItem): BriefFormState {
  const brief = topic.brief ?? {};
  return {
    userQuestion: String(brief.user_question ?? ""),
    duration: String(brief.suggested_duration_seconds ?? 60),
    targetAudience: String(brief.target_audience ?? ""),
    contentType: String(brief.content_type ?? ""),
    hookType: String(brief.suggested_hook_type ?? ""),
    structure: String(brief.suggested_structure ?? ""),
    cta: String(brief.suggested_cta ?? ""),
    businessGoal: String(brief.business_goal ?? ""),
    coreConclusion: topic.core_conclusion ?? "",
    riskBoundary: topic.risk_boundary ?? "",
    prerequisites: (topic.prerequisites ?? []).join("\n"),
    forbiddenExpr: (topic.forbidden_expr ?? []).join("\n"),
  };
}

function formToPayload(form: BriefFormState) {
  return {
    brief: {
      user_question: form.userQuestion.trim(),
      suggested_duration_seconds: Number(form.duration) || 60,
      target_audience: form.targetAudience.trim(),
      content_type: form.contentType.trim(),
      suggested_hook_type: form.hookType.trim(),
      suggested_structure: form.structure.trim(),
      suggested_cta: form.cta.trim(),
      business_goal: form.businessGoal.trim(),
    },
    core_conclusion: form.coreConclusion.trim(),
    risk_boundary: form.riskBoundary.trim(),
    prerequisites: form.prerequisites.split("\n").map((s) => s.trim()).filter(Boolean),
    forbidden_expr: form.forbiddenExpr.split("\n").map((s) => s.trim()).filter(Boolean),
  };
}

function errorMessage(error: unknown): string {
  const topicsError = error as TopicsApiError;
  if (topicsError && topicsError.name === "TopicsApiError" && topicsError.message) return topicsError.message;
  if (error instanceof Error) return error.message;
  return "未知错误";
}

function timeLabel(iso: string | null): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}

export default function TopicsPage() {
  const [topics, setTopics] = useState<TopicItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selected, setSelected] = useState<TopicItem | null>(null);
  const [form, setForm] = useState<BriefFormState>(EMPTY_FORM);
  const [busy, setBusy] = useState<string>("");
  const [lastDraft, setLastDraft] = useState<BriefDraftResult | null>(null);
  const [lastScript, setLastScript] = useState<GenerateScriptResult | null>(null);
  const [doctorNotes, setDoctorNotes] = useState("");
  // 白名单管理（管理员）：探测 403 → 自动隐藏入口
  const [canManageReviewers, setCanManageReviewers] = useState(false);
  const [reviewersOpen, setReviewersOpen] = useState(false);
  const [reviewers, setReviewers] = useState<ReviewerItem[]>([]);
  const [newReviewerId, setNewReviewerId] = useState("");

  const refreshTopics = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.list("", "", 300);
      setTopics(data.items);
    } catch (error) {
      setMessage(`加载选题失败：${errorMessage(error)}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshTopics();
    // 探测管理员能力：403 → 隐藏白名单入口
    api.listReviewers().then(() => setCanManageReviewers(true)).catch(() => setCanManageReviewers(false));
  }, [refreshTopics]);

  // 打开抽屉：拉详情并填充表单
  const openTopic = useCallback(async (topicId: string) => {
    setSelectedId(topicId);
    setLastDraft(null);
    setLastScript(null);
    setMessage("");
    try {
      const detail = await api.get(topicId);
      setSelected(detail);
      setForm(formFromTopic(detail));
    } catch (error) {
      setMessage(`加载选题详情失败：${errorMessage(error)}`);
    }
  }, []);

  const refreshSelected = useCallback(async () => {
    if (!selectedId) return;
    try {
      const detail = await api.get(selectedId);
      setSelected(detail);
      // 不覆盖正在编辑的表单，只刷新状态徽标与文稿列表
      setTopics((prev) => prev.map((t) => (t.id === detail.id ? { ...t, status: detail.status, scripts: detail.scripts } : t)));
    } catch {
      // 详情刷新失败不打断编辑
    }
  }, [selectedId]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return topics;
    return topics.filter((t) =>
      `${t.title} ${t.summary} ${(t.brief?.target_audience ?? "")}`.toLowerCase().includes(q),
    );
  }, [topics, search]);

  const grouped = useMemo(() => groupTopicsByStatus(filtered), [filtered]);

  // ---- 操作 ----
  const withBusy = useCallback(async (key: string, action: () => Promise<void>) => {
    setBusy(key);
    setMessage("");
    try {
      await action();
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setBusy("");
    }
  }, []);

  const runDraftBrief = () =>
    withBusy("draft", async () => {
      if (!selectedId) return;
      const result = await api.draftBrief(selectedId);
      setLastDraft(result);
      setForm((prev) => ({
        ...prev,
        userQuestion: result.draft.user_question || prev.userQuestion,
        duration: String(result.draft.suggested_duration_seconds || prev.duration),
        targetAudience: result.draft.target_audience || prev.targetAudience,
        contentType: result.draft.content_type || prev.contentType,
        hookType: result.draft.suggested_hook_type || prev.hookType,
        structure: result.draft.suggested_structure || prev.structure,
        cta: result.draft.suggested_cta || prev.cta,
        coreConclusion: result.draft.core_conclusion || prev.coreConclusion,
        prerequisites: result.draft.prerequisites?.length ? result.draft.prerequisites.join("\n") : prev.prerequisites,
      }));
      setMessage("拆解完成：草稿已填入表单（不落库）。请补风险边界与禁止表达后保存，并让医生确认核心结论。");
    });

  const runSaveBrief = () =>
    withBusy("save", async () => {
      if (!selectedId) return;
      const saved = await api.saveBrief(selectedId, formToPayload(form));
      setSelected(saved);
      setTopics((prev) => prev.map((t) => (t.id === saved.id ? { ...t, status: saved.status, missing_required: saved.missing_required } : t)));
      setMessage(
        saved.reapproval_required
          ? "已保存。选题卡在审批通过后被修改过，已重置为「待医生审批」，医学门必须重新过。"
          : `已保存（${STATUS_LABELS[saved.status as keyof typeof STATUS_LABELS] ?? saved.status}）。${saved.missing_required.length ? `还缺：${saved.missing_required.join("、")}` : ""}`,
      );
    });

  const runApproveTopic = () =>
    withBusy("approve", async () => {
      if (!selectedId) return;
      const result = await api.approveTopic(selectedId);
      setMessage(`审批通过（${result.approved_by}），选题卡进入「已批准」。`);
      await refreshSelected();
    });

  const runGenerateScript = () =>
    withBusy("generate", async () => {
      if (!selectedId) return;
      const result = await api.generateScript(selectedId, doctorNotes);
      setLastScript(result);
      setMessage(
        `生稿完成（v${result.version}，状态草稿待医生审批）。${result.warnings.length ? `软警告：${result.warnings.join("；")}` : "硬校验全部通过。"}`,
      );
      await refreshSelected();
    });

  const runApproveScript = (scriptId: string) =>
    withBusy(`approve-script-${scriptId}`, async () => {
      await api.approveScript(scriptId);
      setMessage("文稿审批通过，可进入拍摄。");
      await refreshSelected();
    });

  const runAddReviewer = () =>
    withBusy("add-reviewer", async () => {
      if (!newReviewerId.trim()) return;
      await api.addReviewer(newReviewerId.trim());
      setReviewers((await api.listReviewers()).items);
      setNewReviewerId("");
      setMessage("已加入医生审批白名单。");
    });

  const runRemoveReviewer = (userId: string) =>
    withBusy(`remove-reviewer-${userId}`, async () => {
      await api.removeReviewer(userId);
      setReviewers((await api.listReviewers()).items);
    });

  const openReviewers = async () => {
    setReviewersOpen(true);
    try {
      setReviewers((await api.listReviewers()).items);
    } catch (error) {
      setMessage(errorMessage(error));
    }
  };

  return (
    <div className="flex min-h-screen flex-col bg-stone-50 text-stone-900">
      {/* 顶栏 */}
      <header className="flex items-center gap-3 border-b bg-white px-4 py-3">
        <Link href="/studio" className="flex items-center gap-1 text-sm text-stone-500 hover:text-stone-900">
          <ArrowLeft size={16} /> 返回工作台
        </Link>
        <h1 className="flex items-center gap-2 text-lg font-semibold">
          <ClipboardList size={20} /> 选题板
        </h1>
        <span className="text-xs text-stone-400">热点 → 选题卡 → 医生审批 → 原创稿 → 拍摄</span>
        <div className="ml-auto flex items-center gap-2">
          <div className="relative">
            <Search size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-stone-400" />
            <Input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="搜索选题/人群"
              className="h-8 w-48 pl-7"
            />
          </div>
          {canManageReviewers ? (
            <Button variant="outline" size="sm" onClick={() => void openReviewers()}>
              <UserCheck size={14} className="mr-1" /> 审批白名单
            </Button>
          ) : null}
          <Button variant="outline" size="sm" onClick={() => void refreshTopics()} disabled={loading}>
            <RefreshCw size={14} className={loading ? "mr-1 animate-spin" : "mr-1"} /> 刷新
          </Button>
        </div>
      </header>

      {message ? (
        <div className="border-b bg-amber-50 px-4 py-2 text-sm text-amber-900">{message}</div>
      ) : null}

      {/* 看板 */}
      <main className="flex flex-1 gap-3 overflow-x-auto p-3">
        {TOPIC_STATUSES.map((status) => {
          const items = grouped[status];
          return (
            <section key={status} className="flex w-72 shrink-0 flex-col gap-2 rounded-lg bg-stone-100/70 p-2">
              <div className="flex items-center justify-between px-1">
                <h2 className="text-sm font-medium text-stone-600">{TOPIC_STATUS_LABELS[status]}</h2>
                <Badge variant="secondary" className="text-xs">{items.length}</Badge>
              </div>
              {loading && items.length === 0 && status === "new" ? (
                <div className="flex items-center gap-2 px-2 py-6 text-xs text-stone-400">
                  <LoaderCircle size={14} className="animate-spin" /> 加载中…
                </div>
              ) : null}
              {!loading && items.length === 0 ? (
                <div className="px-2 py-6 text-center text-xs text-stone-400">暂无</div>
              ) : null}
              {items.map((topic) => (
                <button
                  key={topic.id}
                  type="button"
                  onClick={() => void openTopic(topic.id)}
                  className="flex flex-col gap-1 rounded-md border bg-white p-2.5 text-left shadow-sm transition hover:border-stone-400 hover:shadow"
                >
                  <div className="flex items-start justify-between gap-2">
                    <span className="line-clamp-2 text-sm font-medium">{topic.title || "(无标题)"}</span>
                    {topic.heat_score != null ? (
                      <span className="shrink-0 rounded bg-orange-100 px-1 text-xs text-orange-700" title="热度分">
                        {topic.heat_score}
                      </span>
                    ) : null}
                  </div>
                  {topic.brief?.target_audience ? (
                    <span className="text-xs text-stone-500">人群：{String(topic.brief.target_audience)}</span>
                  ) : null}
                  <div className="flex flex-wrap items-center gap-1">
                    {topic.missing_required?.length ? (
                      <Badge variant="destructive" className="text-[10px]">缺{topic.missing_required.length}项</Badge>
                    ) : (
                      <Badge variant="secondary" className="text-[10px]">必填齐</Badge>
                    )}
                    {(topic.scripts ?? []).some((s) => s.origin === "shooting_script") ? (
                      <Badge variant="outline" className="text-[10px]"><FileText size={10} className="mr-0.5" />有原创稿</Badge>
                    ) : null}
                    <span className="ml-auto text-[10px] text-stone-400">{timeLabel(topic.updated_at)}</span>
                  </div>
                </button>
              ))}
            </section>
          );
        })}
      </main>

      {/* 详情抽屉 */}
      <Sheet open={selectedId !== null} onOpenChange={(open) => { if (!open) setSelectedId(null); }}>
        <SheetContent side="right" className="w-full overflow-y-auto p-4 sm:max-w-xl">
          <SheetHeader className="px-0">
            <SheetTitle className="text-left">{selected?.title || "选题详情"}</SheetTitle>
          </SheetHeader>
          {selected ? (
            <div className="mt-2 flex flex-col gap-4">
              <div className="flex flex-wrap items-center gap-2 text-xs text-stone-500">
                <Badge>{STATUS_LABELS[selected.status as keyof typeof STATUS_LABELS] ?? selected.status}</Badge>
                {selected.source_platform ? <span>来源：{selected.source_platform}</span> : null}
                {selected.source_url ? (
                  <a href={selected.source_url} target="_blank" rel="noreferrer" className="text-blue-600 underline">原视频</a>
                ) : null}
                {selected.missing_required?.length ? (
                  <span className="text-red-600">缺必填：{selected.missing_required.join("、")}</span>
                ) : null}
                {selected.approved_by ? <span>审批人：{selected.approved_by}</span> : null}
              </div>

              {/* 表单 */}
              <div className="grid grid-cols-2 gap-3">
                <div className="col-span-2">
                  <Label className="text-xs">用户问题（必填）</Label>
                  <Input className="mt-1" value={form.userQuestion}
                    onChange={(e) => setForm({ ...form, userQuestion: e.target.value })} />
                </div>
                <div>
                  <Label className="text-xs">建议时长（秒，必填）</Label>
                  <Input className="mt-1" type="number" min={15} max={300} value={form.duration}
                    onChange={(e) => setForm({ ...form, duration: e.target.value })} />
                </div>
                <div>
                  <Label className="text-xs">目标人群</Label>
                  <Input className="mt-1" value={form.targetAudience}
                    onChange={(e) => setForm({ ...form, targetAudience: e.target.value })} />
                </div>
                <div>
                  <Label className="text-xs">内容类型</Label>
                  <Input className="mt-1" value={form.contentType}
                    onChange={(e) => setForm({ ...form, contentType: e.target.value })} />
                </div>
                <div>
                  <Label className="text-xs">建议钩子类型</Label>
                  <Input className="mt-1" placeholder="患者问题/医生结论/反常识" value={form.hookType}
                    onChange={(e) => setForm({ ...form, hookType: e.target.value })} />
                </div>
                <div className="col-span-2">
                  <Label className="text-xs">核心结论（必填，医生确认后生效）</Label>
                  <Textarea className="mt-1 min-h-16" value={form.coreConclusion}
                    onChange={(e) => setForm({ ...form, coreConclusion: e.target.value })} />
                </div>
                <div className="col-span-2">
                  <Label className="text-xs">风险边界（必填，人工填写）</Label>
                  <Textarea className="mt-1 min-h-16" placeholder="不推荐…、不评价…、提示遵医嘱" value={form.riskBoundary}
                    onChange={(e) => setForm({ ...form, riskBoundary: e.target.value })} />
                </div>
                <div className="col-span-2">
                  <Label className="text-xs">禁止表达（必填，一行一条；生稿硬校验红线）</Label>
                  <Textarea className="mt-1 min-h-16" placeholder={"自行停药没问题\n可以自己减半"} value={form.forbiddenExpr}
                    onChange={(e) => setForm({ ...form, forbiddenExpr: e.target.value })} />
                </div>
                <div className="col-span-2">
                  <Label className="text-xs">必要前提（一行一条）</Label>
                  <Textarea className="mt-1 min-h-12" placeholder={"患者已确诊\n已开始服药"} value={form.prerequisites}
                    onChange={(e) => setForm({ ...form, prerequisites: e.target.value })} />
                </div>
                <div>
                  <Label className="text-xs">建议结构</Label>
                  <Input className="mt-1" placeholder="钩子-前提-结论-CTA" value={form.structure}
                    onChange={(e) => setForm({ ...form, structure: e.target.value })} />
                </div>
                <div>
                  <Label className="text-xs">建议 CTA</Label>
                  <Input className="mt-1" value={form.cta}
                    onChange={(e) => setForm({ ...form, cta: e.target.value })} />
                </div>
              </div>

              {/* 操作区 */}
              <div className="flex flex-wrap items-center gap-2 border-t pt-3">
                <Button variant="outline" size="sm" onClick={() => void runDraftBrief()} disabled={busy !== ""}>
                  {busy === "draft" ? <LoaderCircle size={14} className="mr-1 animate-spin" /> : <Sparkles size={14} className="mr-1" />}
                  拆解草稿
                </Button>
                <Button size="sm" onClick={() => void runSaveBrief()} disabled={busy !== ""}>
                  {busy === "save" ? <LoaderCircle size={14} className="mr-1 animate-spin" /> : null}
                  保存
                </Button>
                <Button variant="outline" size="sm" onClick={() => void runApproveTopic()} disabled={busy !== ""}>
                  {busy === "approve" ? <LoaderCircle size={14} className="mr-1 animate-spin" /> : <ShieldCheck size={14} className="mr-1" />}
                  医学审批
                </Button>
                <Button variant="secondary" size="sm" onClick={() => void runGenerateScript()} disabled={busy !== ""}>
                  {busy === "generate" ? <LoaderCircle size={14} className="mr-1 animate-spin" /> : <FileText size={14} className="mr-1" />}
                  生 成 稿
                </Button>
              </div>
              {/* 抽屉内也要能看到操作反馈（页面顶部的 message 会被 Sheet 遮罩挡住） */}
              {message ? (
                <p className={`text-xs ${/失败|不允许|缺失|重复/.test(message) ? "text-red-600" : "text-emerald-700"}`}>{message}</p>
              ) : null}
              <div>
                <Label className="text-xs">医生补充说明（生稿输入：口播习惯/称呼/强调点，可空）</Label>
                <Input className="mt-1" value={doctorNotes} onChange={(e) => setDoctorNotes(e.target.value)} />
              </div>

              {/* 生稿结果 */}
              {lastScript ? (
                <div className="rounded-md border bg-stone-50 p-3 text-sm">
                  <div className="mb-1 flex items-center gap-2 font-medium">
                    <FileText size={14} /> 生稿结果 v{lastScript.version}
                    <Badge variant="secondary" className="text-[10px]">草稿待医生审批</Badge>
                  </div>
                  <p className="whitespace-pre-wrap text-xs text-stone-700"><strong>钩子：</strong>{lastScript.draft.hook}</p>
                  <p className="mt-1 whitespace-pre-wrap text-xs text-stone-700"><strong>正文：</strong>{lastScript.draft.body}</p>
                  <p className="mt-1 whitespace-pre-wrap text-xs text-stone-700"><strong>CTA：</strong>{lastScript.draft.cta}</p>
                  {lastScript.draft.refs?.length ? (
                    <p className="mt-1 text-xs text-stone-500">依据：{lastScript.draft.refs.join("；")}</p>
                  ) : null}
                  {lastScript.warnings?.length ? (
                    <p className="mt-1 text-xs text-amber-700">软警告：{lastScript.warnings.join("；")}</p>
                  ) : null}
                </div>
              ) : null}

              {/* 文稿列表 */}
              <div className="border-t pt-3">
                <h3 className="mb-2 text-sm font-medium">关联文稿</h3>
                {(selected.scripts ?? []).length === 0 ? (
                  <p className="text-xs text-stone-400">暂无。生稿或豆包推送转写稿后会显示在这里。</p>
                ) : (
                  <ul className="flex flex-col gap-1.5">
                    {(selected.scripts ?? []).map((script: ScriptItem) => (
                      <li key={script.id} className="flex items-center gap-2 rounded border px-2 py-1.5 text-xs">
                        {script.origin ? (
                          <Badge variant="outline" className="text-[10px]">
                            {SCRIPT_ORIGIN_LABELS[script.origin] ?? script.origin}
                          </Badge>
                        ) : null}
                        <span className="truncate">{script.title || "(无标题)"}</span>
                        <span className="text-stone-400">v{script.version}</span>
                        <Badge variant="secondary" className="text-[10px]">{SCRIPT_STATUS_LABELS[script.status] ?? script.status}</Badge>
                        {script.task_id ? <span className="text-stone-400">任务 {script.task_id.slice(0, 8)}…</span> : null}
                        <span className="ml-auto" />
                        {script.status === "draft" && script.origin === "shooting_script" ? (
                          <Button variant="outline" size="sm" className="h-6 px-2 text-xs"
                            onClick={() => void runApproveScript(script.id)}
                            disabled={busy !== ""}>
                            {busy === `approve-script-${script.id}` ? <LoaderCircle size={12} className="animate-spin" /> : "审批"}
                          </Button>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-2 text-sm text-stone-400">
              <LoaderCircle size={14} className="animate-spin" /> 加载中…
            </div>
          )}
        </SheetContent>
      </Sheet>

      {/* 白名单管理（管理员） */}
      <Dialog open={reviewersOpen} onOpenChange={setReviewersOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>医生审批白名单</DialogTitle>
          </DialogHeader>
          <ul className="flex max-h-60 flex-col gap-1 overflow-y-auto">
            {reviewers.map((reviewer) => (
              <li key={reviewer.user_id} className="flex items-center gap-2 rounded border px-2 py-1.5 text-sm">
                <span className="truncate">{reviewer.name || reviewer.user_id}</span>
                <span className="truncate text-xs text-stone-400">{reviewer.email}</span>
                <span className="ml-auto" />
                <Button variant="ghost" size="sm" className="h-6 px-2 text-xs text-red-600"
                  onClick={() => void runRemoveReviewer(reviewer.user_id)}
                  disabled={busy !== ""}>
                  移除
                </Button>
              </li>
            ))}
            {reviewers.length === 0 ? <li className="py-3 text-center text-xs text-stone-400">暂无（两级审批需要至少一名医生）</li> : null}
          </ul>
          <div className="flex items-center gap-2">
            <Input value={newReviewerId} onChange={(e) => setNewReviewerId(e.target.value)}
              placeholder="输入用户 ID（users.id）" />
            <Button size="sm" onClick={() => void runAddReviewer()} disabled={busy !== "" || !newReviewerId.trim()}>
              {busy === "add-reviewer" ? <LoaderCircle size={14} className="animate-spin" /> : "添加"}
            </Button>
          </div>
          <p className="text-xs text-stone-400">用户 ID 见「设置 → API Keys」页同源账号体系；仅管理员可管理本白名单。</p>
        </DialogContent>
      </Dialog>
    </div>
  );
}
