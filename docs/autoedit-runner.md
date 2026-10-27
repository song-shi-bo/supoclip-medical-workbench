# DSH 固化剪辑流程执行器（video-brain 桥接）

`backend/src/autoedit_runner_video_brain.py` 是「medical-video-production」固定剪辑
流程在 DSH 环境的执行器实现：消费 Studio 的自动剪辑文件队列，调用本机
**video-brain**（`/domain/video-brain`，医疗科普切片流水线）完成切片、纠错、
封面、字幕与渲染，再把成片回写到队列供 Studio 时间线使用。

```
Studio (/studio 页面)
   │  POST /studio/auto-edit
   ▼
studio_auto_edit_bridge.py ──写──▶ tasks/in/autoedit_xxx.json        （文件队列契约）
                                        │
                                        ▼
                    autoedit_runner_video_brain.py（本执行器）
                        │ POST /v1/tasks（:8940）
                        ▼
                    video-brain 固定流程（预检→转写→纠错→规划→渲染→质检）
                        │
                        ▼
        tasks/output/{task_id}/{clip}/clip_final.mp4 + cover.jpg
        tasks/output/{task_id}/clip_plan.json
        tasks/status/{task_id}.json  ◀──进度/终态回写，前端轮询
```

## 契约对齐（已验证）

- video-brain 每条切片输出 `clip_final.mp4`（1080×1920 · H.264/AAC）+ `cover.jpg`，
  与桥接 `read_auto_edit_task` 期望的文件名一致。
- video-brain 封面片头为 **0.8s 静帧**，与桥接里字幕 +0.8s 时移的硬编码一致，
  无需改动。
- `clip_plan.json` 的 `highlights[]`（clip_id/start/end/topic/cover_title）由执行器
  从 video-brain 的 `ClipRecord` 合成，桥接据此还原 Studio 时间线切片。
- `partial_failed`：有可用成片时按 `completed` 回写，stage 注明成功/失败条数。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `STUDIO_TASKS_ROOT` | `/app/tasks` | **必须与后端一致**；DSH 上建议 `/domain/supoclip-medical-workbench/tasks` |
| `VIDEO_BRAIN_URL` | `http://127.0.0.1:8940` | video-brain 服务地址 |
| `VIDEO_BRAIN_HOME` | `/domain/video-brain` | 用于直接拷贝切片产物（同机部署） |
| `AUTOEDIT_POLL_SECONDS` | `2` | 轮询 video-brain 间隔 |
| `AUTOEDIT_IDLE_SECONDS` | `3` | 队列空闲扫描间隔 |
| `AUTOEDIT_TASK_TIMEOUT_SECONDS` | `3600` | 单任务超时（超时自动取消并置 failed） |

执行器崩溃/重启后会自动恢复 `processing/` 里的遗留任务（依据任务 JSON 里的
`bridge.video_brain_task_id` 续轮询，不重复建任务）。

## 启动顺序（DSH，无 Docker）

**前置条件（数据库初始化状态，2026-10-09）**：`init.sql` 与
`backend/migrations/001-003` 已应用；前端 Prisma 13 个迁移已用
`prisma migrate resolve --applied` 打基线——init.sql 已含最终 schema，直接
`migrate deploy` 会报 P3005，勿重复执行，用 `prisma migrate status` 确认
"up to date"。`backend/.env` 已创建：DATABASE_URL 指向 127.0.0.1:5432 的
supoclip 库（trust 认证）、`REDIS_HOST=127.0.0.1`、
`TRANSCRIPTION_PROVIDER=faster_whisper`、
`STUDIO_TASKS_ROOT=/domain/supoclip-medical-workbench/tasks`、
`TEMP_DIR=/domain/supoclip-medical-workbench/backend/temp`；
`BACKEND_AUTH_SECRET` 与 frontend/.env 一致（密钥值见 frontend/.env，不写入文档）。
PostgreSQL/Redis 为用户空间安装，详见 `DSH_STARTUP.md`「工具与启动命令」。

容器内存有限，按下列顺序启动，不要让 Next.js 编译与 faster-whisper 模型加载
同时进行（曾发生 OOM SIGKILL exit 137）；跑长视频转写时避免同时改动前端代码
触发编译：

```bash
# 1. PostgreSQL 16（:5432，trust 认证，用户/库 supoclip）
/domain/.tools/pg16/bin/pg_ctl -D /domain/.tools/pgdata -l /domain/.tools/pg.log \
  -o "-p 5432 -c listen_addresses=127.0.0.1" start

# 2. Redis 7.2（:6379）
/domain/.tools/redis-src/src/redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no

# 3. SupoClip 后端（:8000）
cd /domain/supoclip-medical-workbench/backend
/domain/.tools/bin/uv run uvicorn src.main_refactored:app --host 127.0.0.1 --port 8000

# 4. video-brain 固定流程服务（:8940）+ 固化剪辑执行器（与后端共享同一 STUDIO_TASKS_ROOT）
cd /domain/video-brain && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8940
cd /domain/supoclip-medical-workbench
STUDIO_TASKS_ROOT=$PWD/tasks VIDEO_BRAIN_URL=http://127.0.0.1:8940 \
  python3 backend/src/autoedit_runner_video_brain.py

# 5. SupoClip 前端（:3107，页面 /studio，最后启动）
cd frontend && /domain/.tools/bin/pnpm dev
```

前端→后端寻址：`frontend/src/app/api/studio/transcribe/route.ts` 使用
`process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"`，本机部署无需额外配置。

## 端到端验证记录（2026-10-09）

用桥接自身的 `submit_auto_edit_task` 提交 `timeline_e2etest`（45s 高血压口播样片，
真实 ASR 10 段）→ 执行器消费 → **75s 完成**，回传 2 条切片：

- `clip-a61e7e-01`「血压管理」源 0–22.13s → 成片 22.98s（含 0.8s 封面）
- `clip-a61e7e-02`「停药风险」源 22.13–38.29s → 成片 16.96s
- `read_auto_edit_task` 读取：artifacts/clips/字幕 0.8s 时移全部正确。

## 注意事项

- **内存**：容器内存有限时，避免 Next.js 编译与 faster-whisper 模型加载同时进行
  （曾触发 OOM SIGKILL）。跑长视频前先确认另一侧空闲。
- Studio 已带纠错转写，但当前链路由 video-brain 重新 ASR（其任务契约不含注入
  转写字段）；45s 样片约 20s，长视频按 ~0.5x 实时估算。
- `tasks/`、`.studio-uploads/` 已在 `.gitignore`，含源媒体，不入库。
