# DSH 3445 项目启动与故障恢复

适用目录：`/domain/supoclip-medical-workbench`。核对日期：2026-10-10。服务状态会变化，操作前重新检查。

## 先确认已有进程

- 本项目 `frontend/package.json` 的 dev/start 端口是 **3107**。工作台页面为 `/studio`，根路径可能显示上游营销首页。
- `video-brain` 固定剪辑流程独立使用 **8940**。现有桥接执行器文件是 `backend/src/autoedit_runner_video_brain.py`；文件存在或语法正确不代表桥接已完成运行验证。
- 原 SupoClip FastAPI 使用 **8000**。2026-10-09 起本机已安装用户态 PostgreSQL 16（监听 **5432**）与 Redis 7.2（监听 **6379**），这两个端口可能已有实例在跑，复用而不重复启动。8000 与 3107 是否在监听仍以实际检查为准，不能宣称完整剪辑服务已启动。
- 先检查对应端口和已有后台任务。复用现有服务，不重复启动进程，不停止不属于本任务的后台任务。

## 工具与启动命令

成员容器没有 Docker/Compose。根 README、setup 文档中的 Docker 操作用于另一个具备 Docker 的开发环境。此处缺少系统依赖时使用 DSH 标题栏“工具与帮助”，不申请 Docker socket 或反复尝试 sudo/apt。

个人工具装在 `/domain/.tools/bin`（2026-10-09 用户空间安装，无 Docker/sudo）：`pnpm`、`uv`（v0.12.24）。先确认文件与版本；普通 shell 未必包含此目录，可以在当前任务 shell 中使用：

```bash
export PATH="/domain/.tools/bin:$PATH"
cd /domain/supoclip-medical-workbench/frontend
pnpm --version && uv --version
# 仅在确认 3107 没有已有服务、且任务要求启动时运行：
pnpm run dev
```

### PostgreSQL 16.4 / Redis 7.2（用户空间，2026-10-09 安装）

PostgreSQL 为便携版 zonky 二进制，在 `/domain/.tools/pg16/bin/`（只有 initdb/pg_ctl/postgres，无 psql），数据目录 `/domain/.tools/pgdata`，日志 `/domain/.tools/pg.log`。本地开发为 trust 认证，用户 supoclip、库 supoclip，无密码；连接串 `postgresql://supoclip@127.0.0.1:5432/supoclip`，带密码亦可（trust 模式忽略密码）。

```bash
# 先确认 5432 没有已有实例，再查询状态/启动：
/domain/.tools/pg16/bin/pg_ctl -D /domain/.tools/pgdata status
/domain/.tools/pg16/bin/pg_ctl -D /domain/.tools/pgdata -l /domain/.tools/pg.log -o "-p 5432 -c listen_addresses=127.0.0.1" start
# 停止：
/domain/.tools/pg16/bin/pg_ctl -D /domain/.tools/pgdata stop
# 客户端 psql 15.18（Debian deb 解包，需指定 LD_LIBRARY_PATH）：
LD_LIBRARY_PATH=/domain/.tools/pgclient/usr/lib/x86_64-linux-gnu \
  /domain/.tools/pgclient/usr/lib/postgresql/15/bin/psql -h 127.0.0.1 -p 5432 -U supoclip -d supoclip
```

Redis 7.2.5 为源码编译（MALLOC=libc），源码在 `/domain/.tools/redis-src`，二进制在其 `src/` 下（redis-server、redis-cli）：

```bash
# 先确认 6379 没有已有实例再启动（--save "" 关闭 RDB、无 AOF，仅作本机队列/缓存）：
/domain/.tools/redis-src/src/redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no
/domain/.tools/redis-src/src/redis-cli -h 127.0.0.1 ping
```

数据库初始化状态（2026-10-09）：`init.sql` 与 `backend/migrations/001-003` 已应用；前端 Prisma 13 个迁移已用 `prisma migrate resolve --applied` 打基线——init.sql 已含最终 schema，直接 `migrate deploy` 会报 P3005，不要重复执行，用 `prisma migrate status` 确认 "up to date"。`backend/.env` 已创建：`DATABASE_URL=postgresql+asyncpg://supoclip:supoclip_password@127.0.0.1:5432/supoclip`、`REDIS_HOST=127.0.0.1`、`TRANSCRIPTION_PROVIDER=faster_whisper`、`STUDIO_TASKS_ROOT=/domain/supoclip-medical-workbench/tasks`、`TEMP_DIR=/domain/supoclip-medical-workbench/backend/temp`；`BACKEND_AUTH_SECRET` 与 frontend/.env 一致，密钥值见 frontend/.env，不写入本文。

### 启动顺序与内存

容器内存有限，Next.js 编译与 faster-whisper 模型加载不要同时进行（曾发生 OOM SIGKILL exit 137）。推荐顺序：先起 DB/Redis → 后端 → worker → 最后起前端；跑长视频转写时避免同时改动前端代码触发编译。前端→后端寻址用 `process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"`（`frontend/src/app/api/studio/transcribe/route.ts`），本机部署无需额外配置。

后端推荐入口为 `src.main_refactored:app`，与现有 Compose/开发文档一致。安装与启动依赖须先确认当前任务范围、uv、Python 环境和数据库/队列是否就绪：

```bash
cd /domain/supoclip-medical-workbench/backend
# 前置条件（DB/Redis 已启动、backend/.env 就绪）满足后使用，而不是直接复制执行：
uv sync
uv run uvicorn src.main_refactored:app --host 127.0.0.1 --port 8000
uv run arq src.workers.tasks.WorkerSettings  # 需要主链路队列处理时另开终端运行
```

原 SupoClip 后端/队列路径与 video-brain 固定流程桥接是不同运行路径。按实际任务核对需要哪些服务，不把前端能打开当成转写、剪辑、导出已全部打通。

## 浏览器与图像

- 截图使用允许的绝对路径，例如 `/domain/.dsh-browser/output/studio-preview.png` 或本项目 `docs/screenshots/` 下的文件。先确认输出目录存在。
- `/home-landing.png` 位于根目录之外的未授权位置，会被浏览器工具拒绝；修改输出路径即可，不扩大目录权限。
- 模型能调用浏览器，不代表它当前已获得图像输入能力。以模型能力声明和实际附件读取结果为准；失败时可用页面结构快照继续只读检查，并准确报告读图限制。
- 3445 属于 `songshibo`，3446 属于 `admin`。同一 IP 不同端口共享浏览器登录状态；管理员与成员同时使用时应使用独立会话/浏览器配置文件。

## 模型失败后的恢复

- 先查看最后一次成功工具操作，避免重复写文件、重复创建任务或重启服务。
- `content_policy_blocked` / HTTP 422 表示平台内容判定拦截；它与供应商 API Key 鉴权失败不同。重复同一段历史或只改追问措辞可能继续失败，应向管理员报告时间与错误码。
- 项目启动说明中讨论项目自身 AI 模块、工具安装及提取视频 ID 属于正常开发内容，不据此推断成员在索取平台私有信息。
- 不把平台托管的模型密钥写入仓库或聊天。项目业务所需独立配置由相应受控配置路径提供，不读取、复制或输出无关凭据。

## 验证与资源

- 已有后端 pytest、前端 Vitest 与 Playwright 测试；命令以当前 README、pyproject 与 package scripts 为准。数据库集成测试使用独立测试数据库。
- 2026-10-10 本次核对容器内存上限为 6 GiB（6442450944 bytes，实际以 cgroup 为准），进程上限为 512，可能随管理员配置变化。浏览器、Next.js、视频处理同时运行时注意使用量，不无限启动后台任务。
- 报告分别说明页面可见、接口可用、模型请求成功、剪辑流程完成与尚未验证内容。

## 2026-10-10 管理侧修复记录

- 已把 worker 下载/渲染/本地对象保存改为 1MiB 流式原子复制。3GiB 文件测试进程峰值 RSS 14128KiB。
- 单实例锁：Studio worker 与 autoedit runner 各一把；SupoClip/video-brain ASR 共用 `/domain/.studio-resources/asr.lock`。
- Worker 每 20 秒心跳，按 worker_id 续租 PostgreSQL 的 120 秒任务租约；不再仅延长 Redis 键。
- 宿主 `songshibo-video.service` 守护 backend、video-brain、autoedit runner、Studio job worker，独立于聊天轮次。DB/Redis/前端目前仍复用原进程，完整重启恢复仍需验收。
- 上传已有持久化分片查询、原子分片写入、连续编号与总大小校验、固定任务 ID 和合并回执；同一文件重试可继续已完成的分片。
- R2 路径修复了字节单位、ETag 和同名函数覆盖问题；真实 R2 尚未配置和验收。
- 大于 32MiB 或长于 300 秒的视频不再整文件解码浏览器音频波形，播放与剪辑保留。
- 业务模型使用管理侧新建的独立应用凭据；仅允许指定文本模型、并发 1、每天 500 请求及 200000 预留 token，同时受团队原配额约束。凭据已写入受保护 backend/.env，不在文档或聊天展示。
- 实测真实模型三条剪辑助手指令均 engine=llm，start_auto_edit 保留 confirm；运行 API 也已成功验证。模型繁忙/不可用仍会回退规则模式。
- 仍待验收：用户侧受鉴权直传入口、完整长视频剪辑导出、全服务冷启动恢复与真实大文件浏览器恢复。不能把小文件或 Mock 测试当成这些流程完成。


## 2026-10-10 当前运行方式（取代上面的旧手工启动建议）

- 用户入口为 `https://101.201.81.118:3445/studio`，先用 songshibo 登录 DSH。该路径保留原成员隔离、会话过期/停用/强制改密检查；修改请求校验 Origin。3445 根路径仍为 DSH。
- Studio API 使用同源 `/video-api`。宿主 Caddy 经成员持久化目录中的 `api.sock` / `frontend.sock` 转发，不依赖容器 IP，不把 8000/8940 暴露到公网。
- 宿主 songshibo-video.service 已守护 PostgreSQL、Redis、Next.js、frontend socket proxy、API、video-brain、autoedit runner、Studio worker；先检查现有进程和日志，避免另外手工启动同一套服务。服务管理由宿主管理员操作，成员容器无需 Docker socket。
- 宿主服务停止通过 ExecStop 通知容器内 supervisor，后者结束自己管理的子进程；避免 docker exec 客户端结束后留下旧守护进程。已验证前端、Redis、PostgreSQL 退出后重新启动、原任务仍可查询。
- Studio worker 每个任务使用独立子进程；总时限默认3600秒。失去租约或心跳连续80秒失败停止执行，任务结束回收进程；转写后释放不用的 ASR 模型。旧执行器不能在租约过期或任务终态后写进度/产物。相同产物回传幂等。
- 分片上传失败后重新选择同一个文件并点击转写继续；若执行失败，重新点击会复用已上传源文件重试。R2 是可选存储，尚无真实凭据，不能宣称它已验收。
- 原有 3066971786-byte（约2.86GiB）、438.17秒的 HEVC 视频已完成转写、剪辑和质检，输出含音视频的1080×1920成片，下载 Range 请求206。此次未新增 oom_kill（仍为2）。这验证了处理链路，不等于验证了用户重新上传整个原片。
- 真实浏览器助手已显示“模型助手”并执行暂停。模型应用额度500次/日、200000保守预留tokens/日（UTC重置）、并发1；应用只能使用指定模型，供应商凭据不会下发给成员。繁忙或不可用时仍允许规则降级。
- 512MiB 合成素材的浏览器上传中断/续传已验证（刷新前12/128片，重新选择文件后续传，前12片均只成功上传一次）；全流程成片UI验收仍在进行。大文件波形保护提示已显示。
- 用户公网登录正向验收安排稍后进行；不要把本地 SSH 转发测试当成该验收。20分钟输入、真实R2、容器整体重建的验收尚未完成。
- 根因报告须区分证据：当前配额6GiB，不能沿用历史2GiB；exit137本身不等于OOM；上传停滞不能仅凭现象定性家庭带宽；服务消失存在宿主备份停止控制平面的记录，不能一概归因每轮会话清理。

## 2026-10-10 最终补充验收
- 移除前端Google Fonts在线启动依赖，系统字体回退；具体任务错误回传页面；修复贴纸禁入矩形(x,y,w,h)。
- 父worker退出时通过Linux PR_SET_PDEATHSIG停止任务进程组；supervisor不将--job子进程误认作主worker。父SIGKILL、子SIGTERM及初始化竞态测试通过；主worker强杀后自动恢复。
- 512MiB浏览器中断续传完成；原2.86GiB原片完成剪辑与QC。最新合成正向任务a4c9defe1bb14901b97c9f53d8d7fa87完成，页面成片/字幕时间线、实际播放和下载通过。
- 新入口https://101.201.81.118:3445/studio复用songshibo的DSH会话。用户公网登录验收稍后进行；真实R2、20分钟输入与容器整体重建未专门验收。

## 2026-10-10 12:59 UTC 最终验收补充
- 20分钟连续合成语音输入（1200.032秒）任务7b6cb0ee7ee84c4ea9cbefd36d025b9d完成，末段转写到1199.88秒，成片/QC通过，全程oom_kill无新增。
- Studio队列转写现在传给video-brain复用，新增输入时间戳/长度/源时长校验，医学纠错、规划、QC保留。新任务task-b213f323ad转写复用0.0027秒，成片/QC/Range下载通过。
- 修复重生成继承旧video_brain_task_id：新生成使用新执行标识，旧成片保留。
- 公网songshibo登录后的访问与上传，用户回复稍后验证，仍未宣称通过。真实R2与容器整体重建未专项验收；R2为可选替代路径。
