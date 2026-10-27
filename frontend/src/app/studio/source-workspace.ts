import { readableWorkflowError, type RevisionDocument } from "@/lib/studio-workflow";

export type SourceCredential = { object_key: string; transcript_id: string; transcript_version: number; project_id: string };
export type SourceProject = {
  id: string;
  source: { name: string; duration: number; width?: number; height?: number };
  timeline: { ranges: { id: string; source_start: number; source_end: number; transform?: Record<string, unknown> | null }[] };
  artifacts: { edl: string; srt: string; fcpxml: string };
};

async function readJson(response: Response): Promise<Record<string, unknown>> {
  const payload = await response.json() as Record<string, unknown>;
  if (!response.ok) throw new Error(readableWorkflowError(payload.detail, `返回原片失败（${response.status}）`));
  return payload;
}

/** 显式操作专用：版本来自该项目凭据，绝不以 timeline 内嵌旧稿替代。
 *  播放优先预览代理（/preview：原生编码直通；HEVC/4K 自动转 H.264 720p 代理）；
 *  代理未就绪（202 building / failed / 触发失败）时回退原片并置 fallbackNotice，
 *  由页面提示「高码率原片可能卡顿」。 */
export async function loadSourceWorkspace(base: string, projectId: string, signal?: AbortSignal) {
  const project = await readJson(await fetch(`${base}/studio/timeline/${encodeURIComponent(projectId)}`, { cache: "no-store", signal })) as unknown as SourceProject;
  if (project.id !== projectId || !Array.isArray(project.timeline?.ranges) || !project.source?.name || !(project.source.duration > 0)) {
    throw new Error("返回原片失败：项目或源片信息不匹配。");
  }
  let fallbackNotice = false;
  let previewUrl = `${base}/studio/timeline/${encodeURIComponent(projectId)}/source`;
  const response = await fetch(`${base}/studio/projects/${encodeURIComponent(projectId)}/transcript-ref`, { cache: "no-store", signal });
  let credential: SourceCredential | null = null;
  let document: RevisionDocument | null = null;
  if (response.status !== 404) {
    credential = await readJson(response) as unknown as SourceCredential;
    if (credential.project_id !== projectId || !credential.object_key || !credential.transcript_id || !Number.isInteger(credential.transcript_version) || credential.transcript_version < 1) {
      throw new Error("返回原片失败：转写凭据不属于当前项目或版本无效。");
    }
    const query = new URLSearchParams({ object_key: credential.object_key, transcript_id: credential.transcript_id, transcript_version: String(credential.transcript_version) });
    const revision = await fetch(`${base}/api/studio/transcripts/revisions?${query}`, { cache: "no-store", signal });
    if (revision.status === 404) credential = null;
    else {
      document = (await readJson(revision)).document as RevisionDocument;
      if (!document || document.transcript_id !== credential.transcript_id || document.transcript_version !== credential.transcript_version || !Array.isArray(document.asr_segments)) {
        throw new Error("返回原片失败：指定转写版本不匹配。");
      }
      // 后端允许物理同源跨项目复用；source_project_id 不是这里的拒绝条件。
    }
  }
  // 播放优先预览代理：幂等触发（ready → /preview；native → 原片即可播；building/failed → 回退原片）。
  // 放在凭据处理之后：凭据拒绝路径不触发构建，也避免打乱调用方的 fetch once 队列。
  try {
    const trigger = await fetch(`${base}/studio/timeline/${encodeURIComponent(projectId)}/preview`,
      { method: "POST", cache: "no-store", signal });
    if (trigger.ok) {
      const status = await trigger.json() as { state?: string };
      if (status?.state === "ready") {
        previewUrl = `${base}/studio/timeline/${encodeURIComponent(projectId)}/preview`;
      } else if (status?.state !== "native") {
        fallbackNotice = true;  // building/failed：回退原片（高码率原片可能卡顿）
      }
    } else {
      fallbackNotice = true;
    }
  } catch {
    fallbackNotice = true;      // 触发失败不打断打开：回退原片
  }
  return { project, credential, document, previewUrl, fallbackNotice };
}
