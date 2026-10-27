# Studio Job 管线（R2 直传 + 任务化转写/剪辑）

> 替代方案：公网隧道场景下的「大文件上传 + 长转写」双瓶颈根治方案。
> 视频二进制**不经 Cloudflare 隧道**，浏览器直传对象存储；隧道只承载信令 JSON。

## 架构

```
浏览器                    DSH 后端 (:8000)                本机执行器 (studio_job_worker)
  │ POST /api/uploads ────▶ 建 R2 Multipart 会话
  │ ◀── 分片 PUT 预签名 URL（600s）
  │ PUT ×N ──▶ R2（CF 内网直达，8-16MiB/片，3-4 并发，单片重试）
  │ POST /api/uploads/{id}/complete ──▶ 合并 + 校验 → object_key
  │ POST /api/jobs/transcribe ──▶ studio_jobs 表 queued（秒回 job_id）
  │                                          ┌─ Redis 队列/锁/心跳
  │ GET /api/jobs/{id}（2s 轮询）◀───────────┤ 本机执行器认领（WORKER_SHARED_SECRET）
  │ ◀── 状态机九态进度                        │ downloading: signed GET 下载源视频
  │                                          │ transcribing: faster-whisper small/CPU/int8/VAD/zh/词级
  │                                          │ correcting: 医学同音纠错 → analyzing: 金句关键词
  │                                          │ editing: EDL/SRT/FCPXML → rendering: video-brain 固定流程
  │ ◀── completed + 产物列表 ────────────────┘ uploading: 产物 signed PUT 回 R2 → completed
```

## 状态机

`queued → downloading → transcribing → correcting → analyzing → editing → rendering → uploading → completed | failed`

执行器离线时任务停留 queued，前端显示「等待本机执行器（已等待 N 秒）」，不丢任务、不 500。

## 环境变量（值不入库不入 Git）

| 变量 | 说明 |
|---|---|
| `R2_ACCOUNT_ID` / `R2_BUCKET` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | R2 凭据（留空 = 本地回退模式，自动可用） |
| `R2_PRESIGN_PUT_TTL` / `R2_PRESIGN_GET_TTL` / `R2_PART_SIZE_MB` | 签名时效与分片大小 |
| `WORKER_SHARED_SECRET` | 执行器↔后端内部端点鉴权（必填，否则执行器退出） |
| `BACKEND_INTERNAL_URL` | 执行器访问后端内部端点（默认 `http://127.0.0.1:8000/api`） |
| `WORKER_POLL_SECONDS` / `WORKER_JOB_TIMEOUT_SECONDS` | 轮询与超时 |

前端无新增变量（所有 URL 由 API 签发）。`/api/uploads/capabilities` 探测返回
`storage: "r2" | "local"`——local 模式自动回退到本机分片上传（`/studio/transcribe/chunk|assemble`），
行为与已验证的本地流程一致。

## 数据库

- `backend/migrations/005_studio_jobs.sql`（已应用，已并入 init.sql）：
  `studio_jobs`（任务）、`studio_upload_sessions`（上传会话）、`studio_artifacts`（产物）。
- 视频二进制只在 R2/本地对象目录；DB 仅元数据与 key。

## R2 桶 CORS（创建桶后一次性配置）

```json
[{
  "AllowedOrigins": ["http://localhost:3107", "http://127.0.0.1:3107", "https://<前端隧道域名>"],
  "AllowedMethods": ["PUT", "GET", "HEAD"],
  "AllowedHeaders": ["content-type"],
  "ExposeHeaders": ["ETag"],
  "MaxAgeSeconds": 3600
}]
```

隧道域名变更后需更新（`scripts/update-r2-cors.sh` 或控制台手改）；或改用 `"AllowedOrigins": ["*"]`
（预签名 URL 本身即能力凭证，风险可控）。

## 验证记录（2026-10-10）

- 本地模式（R2 未配置）全链路：浏览器分片上传 → assemble 建 studio_job → 执行器认领 →
  转写（9 段，词级时间戳）→ 纠错 → 金句 → 渲染 2 条成片 → 产物入库 → `completed`（约 200s）。
- 产物接口：transcript_json 内容完整；clip_final.mp4 HTTP 206 断点可下载。
- 执行器心跳/认领/租约续期/崩溃重置（attempts 上限 3）逻辑实测。
- 待办：配置真实 R2 凭据后重复同套验证（代码路径相同，仅存储驱动不同）。
