type ChunkStatus = { job_id?: string; object_key?: string; parts?: { index: number; bytes: number }[] };
// 转写/自动剪辑拆分后，自动剪辑 job 需要 object_key：本地分片路径从 assemble 响应里
// 带回（复用已上传任务的路径可能拿不到 → null，由调用方再从任务轮询响应兜底）。
export type UploadedStudioVideo = { job_id: string; object_key: string | null };

export type StudioUploadBinding = { file: File; projectId: string; uploadId: string; uploaded?: UploadedStudioVideo };
export function studioUploadBinding(file: File, projectId: string, previous: StudioUploadBinding | null): StudioUploadBinding {
  return previous?.file === file && previous.projectId === projectId ? previous :
    { file, projectId, uploadId: crypto.randomUUID().replace(/-/g, "") };
}

export async function uploadStudioVideo(file: File, base: string, projectId: string,
  progress: (message: string) => void, options: { uploadId?: string } = {}): Promise<UploadedStudioVideo> {
  // 每次导入创建新会话；当前可见File的显式重试由调用方传入内存uploadId。
  // 不读取localStorage，不跨刷新或新导入接上历史任务。
  const uploadId = options.uploadId || crypto.randomUUID().replace(/-/g, "");
  if (!/^[a-f0-9]{32}$/i.test(uploadId)) throw new Error("上传会话标识无效，请重新导入视频。");
  const chunkSize = 4 * 1024 * 1024;
  const total = Math.ceil(file.size / chunkSize);
  // 当前会话GET确认已传分片；显式重试只补传未完成分片。
  // 不可达/404 都按「从 0 开始」处理，不阻塞上传。
  let done = new Set<number>();
  try {
    const response = await fetch(`${base}/studio/transcribe/chunks/${uploadId}`, { cache: "no-store", signal: AbortSignal.timeout(15000) });
    if (response.ok) {
      const status = await response.json() as ChunkStatus;
      if (status.job_id) return { job_id: status.job_id, object_key: status.object_key ?? null };
      done = new Set((status.parts || []).filter((part) => part.index >= 0 && part.index < total &&
        part.bytes === Math.min(chunkSize, file.size - part.index * chunkSize)).map((part) => part.index));
    }
  } catch { /* 全新会话：从 0 开始上传 */ }
  let stopped = false;
  const controller = new AbortController();
  const send = async (index: number) => {
    if (done.has(index)) return;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      if (stopped) return;
      try {
        const uploaded = await fetch(`${base}/studio/transcribe/chunk?upload_id=${uploadId}&index=${index}`, {
          method: "POST", headers: { "Content-Type": "application/octet-stream" },
          body: file.slice(index * chunkSize, Math.min(file.size, (index + 1) * chunkSize)),
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(45000)]),
        });
        if (!uploaded.ok) throw new Error(`HTTP ${uploaded.status}`);
        done.add(index);
        progress(`正在上传视频（${done.size}/${total} 片）…`);
        return;
      } catch (error) {
        if (stopped) return;
        if (attempt === 2) {
          stopped = true; controller.abort();
          throw new Error(`第 ${index + 1}/${total} 片上传失败：${error instanceof Error ? error.message : "连接中断"}。当前文件保留上传进度；显式重试将补传未完成分片。`);
        }
        await new Promise((resolve) => setTimeout(resolve, 1200 * (attempt + 1)));
      }
    }
  };
  progress(`正在上传视频（共 ${total} 片）…`);
  try {
    await Promise.all(Array.from({ length: Math.min(3, total) }, async (_, worker) => {
      for (let index = worker; index < total && !stopped; index += 3) await send(index);
    }));
  } finally {
    controller.abort();
  }
  const assembled = await fetch(`${base}/studio/transcribe/assemble`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ upload_id: uploadId, filename: file.name, total, size_bytes: file.size, project_id: projectId }),
    signal: AbortSignal.timeout(120000),
  });
  const result = await assembled.json() as { job_id?: string; object_key?: string; detail?: string };
  if (!assembled.ok || !result.job_id) throw new Error(result.detail || `视频合并失败（${assembled.status}）`);
  return { job_id: result.job_id, object_key: result.object_key ?? null };
}
