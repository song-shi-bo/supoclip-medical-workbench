# 医疗科普剪辑平台 · 架构说明

> 适用范围：`/domain/supoclip-medical-workbench`（工作台）+ `/domain/video-brain`（固定剪辑流程服务）+ DSH 容器运行时。
> 状态：截至 2026-10-15（含 10-14/10-15 落地的转写模型纠错、AUTO_EDIT staged 全自动流水线「内容分析→切片规划→本地渲染」与「重新剪辑并替换旧切片」）。
> 维护：组件/端口/契约变更时同步更新本文件；文档与代码不一致时以代码为准。

---

## 1. 总览

平台是**双服务 + 一前端**的结构：工作台负责「人机交互、资产与规则治理」，video-brain 负责「确定性剪辑执行」，两者通过 HTTP 与共享任务目录协作。

```
┌────────────────────────────── 公网入口（宿主 Caddy） ──────────────────────────────┐
│  https://<host>:3445/studio   →  成员持久化目录中的 frontend.sock / api.sock        │
└───────────────┬───────────────────────────────────────────────┬────────────────────┘
                │ frontend.sock                                  │ api.sock
        ┌───────▼────────┐   HTTP    ┌───────────────────┐   HTTP   ┌──────────────────┐
        │ 前端 Next.js    │ ────────▶ │ 工作台后端 FastAPI │ ───────▶ │ video-brain       │
        │ :3107 (dev)     │           │ src.studio_server  │          │ app.main:app :8940│
        │ 播放器/时间线/   │           │ :8000              │          │ 固定剪辑流程       │
        │ 卡片/助手/规则中心│          └─────────┬─────────┘           └────────┬─────────┘
        └────────────────┘                     │                              │
                                     ┌─────────▼─────────┐          ┌─────────▼─────────┐
                                     │ PostgreSQL :5432  │          │ tasks/ 共享任务目录 │
                                     │ Redis      :6379  │          │ data/tasks/<id>/   │
                                     └───────────────────┘          └───────────────────┘
```

**一句话职责划分**
| 层 | 负责 | 不负责 |
|---|---|---|
| 前端 | 交互、预览、资产卡片、规则中心 | 任何渲染与真值校验 |
| 工作台后端 | 任务编排/台账/规则/封面资产/变换/导出分发、转写上传与 Copilot | 视频编码本身 |
| video-brain | 转写/纠错/规划/封面/字幕/渲染/QC 的**确定性执行** | 人机交互与项目台账 |

---

## 2. 运行时拓扑

### 2.1 进程与端口（容器内）
| 进程 | 端口/形态 | 说明 |
|---|---|---|
| `src.studio_supervisor` (PID 31) | — | 容器内监督者：托管下列子进程，子进程退出即以新代码拉起 |
| `redis-server` | 6379 | 队列/进度/锁 |
| `pnpm dev`（Next.js，Turbopack） | 3107 | 前端 dev 服务，**热更新即时生效** |
| `src.studio_frontend_proxy` | unix `frontend.sock` | 把宿主 Caddy 的请求转发到 3107 |
| `src.studio_server` | 8000 + unix `api.sock` | 工作台 API（FastAPI，含 `/studio/*`、`/tasks/*`） |
| `src.studio_job_worker` | — | Studio 队列 worker（每任务隔离子进程） |
| `autoedit_runner_video_brain.py` | — | 自动剪辑执行器：认领 `tasks/in/*.json` → 调 video-brain → 回传产物 |
| `uvicorn app.main:app`（video-brain） | 8940 | 固定剪辑流程 API |
| 用户态 PostgreSQL 16 | 5432 | 业务库（`supoclip`） |

### 2.2 公网入口
- 工作台：`https://<宿主>:3445/studio`（先用成员账号登录 DSH，会话复用）；根路径 `/` 为 DSH 本体。
- 8000 / 8940 **不对公网暴露**，仅经宿主 Caddy → 成员持久化目录中的 `api.sock` / `frontend.sock` 转发。
- 凭据类配置（数据库连接串、鉴权密钥、模型凭据）只存在于受保护配置文件，**不进入仓库、不进日志、不在文档展示**。

### 2.3 对外依赖
| 依赖 | 用途 | 缺失时行为 |
|---|---|---|
| 本机 ffmpeg / ffprobe | 全部渲染与探测 | 相关技能显式失败（不静默） |
| LLM（受控应用凭据，限流：并发 1、每日 500 请求 + token 预留配额，UTC 00:00 重置） | 转写纠错、内容分析、切片规划、Copilot（新调用必须显式 max_tokens——网关按 prompt+max_tokens 做每请求预留） | 自动降级（纠错→保守规整；分析/规划→本地回退，`plan_source`/`fallback_reasons` 留痕） |
| faster-whisper 模型 + 医学热词表 | 转写 | 模型不可用 → `/health` 暴露原因，转写失败 |
| rembg + u2net 权重（`/domain/.u2net/u2net.onnx`） | 封面第三版抠像 | **权重缺失 → `E_CUTOUT_WEIGHTS_MISSING` + 安装指引**（第二版真实帧风格不受影响） |

---

## 3. 代码结构

### 3.1 前端 `frontend/src`
| 位置 | 职责 |
|---|---|
| `app/studio/page.tsx` | 工作台主页：画布/播放器、变换与裁剪、时间线、转写面板（纠错文+原文折叠+uncertain 标记+原因）、staged 成片卡片（`clip_final`+`clip_plan` artifacts 构建）、「重新剪辑并替换」按钮、Copilot、规则中心 |
| `app/studio/studio-copilot.tsx` | 助手面板（对话、确认卡片、动作结果） |
| `components/studio/transform-stage.tsx` | 9:16 舞台：8 手柄裁剪、拖动/滚轮、模糊背景预览 |
| `components/studio/player-control-bar.tsx` / `transform-panel.tsx` | 两层控制栏 + 变换浮层（`viewerZoom` 与 clip transform 分离） |
| `lib/clip-transform.ts` | **后端变换数学的 TS 镜像**（与 Python 逐值一致，10 组对照向量） |
| `lib/studio-clip-cards.ts` | 台账卡片 → 前端 clip 适配（版本行、`segments` 兜底） |
| `app/settings/*`、`app/tasks/*` | 设置/API Key、历史任务页（legacy 链） |

### 3.2 工作台后端 `backend/src`
**入口/设施**：`main_refactored.py`（FastAPI 应用与中间件、鉴权、日志）、`studio_server.py`（socket 模式启动）、`studio_frontend_proxy.py`（前端反代）、`studio_supervisor.py`（进程托管）、`studio_runtime.py`（上传分片拼装/本地对象路径）、`config.py`。

**路由**（`api/routes/`）：`media.py`（Studio 主路由：上传/转写/自动剪辑/封面/变换/工作流）、`tasks.py`（legacy 任务与导出）、`jobs.py`、`api_keys.py`、`billing.py`、`admin.py`、`feedback.py`。

**领域模块**：
| 模块 | 职责 |
|---|---|
| `skill_registry.py` | 规则清单读取/校验、**任务级规则快照**、优先级解析、规则中心只读摘要 |
| `skills/`（9 个 adapter） | 把固化技能变成可调用工具（转写/纠错/规划/封面/渲染/QA 已可用；说话人/金句/字幕未实现，显式报错） |
| `studio_workflow.py` | Phase B 接线：快照落库、启停 skill（备份→写回→校验→回滚） |
| `studio_project_store.py` | **项目台账**（`clips.json`）：稳定 clip、封面资产与修订、渲染修订、任务历史 |
| `studio_local_tasks.py` | **类型化局部任务**：REGENERATE_COVER / UPDATE_TITLE / UPDATE_SUBTITLE_STYLE / UPDATE_TIMELINE |
| `studio_auto_edit_bridge.py` | 自动剪辑提交与读取（任务 JSON、产物清单、快照挂载、`transcript_pre_corrected` 透传） |
| `autoedit_runner_video_brain.py` | 执行器：认领任务 → 提交 video-brain → 轮询 → 回传产物与状态（**常驻循环，改代码后必须单独重启**） |
| `studio_transcript_store.py` | **已纠错转写版本化存储**（`tasks/transcripts/<source_id>/v{n}.json`）：TRANSCRIBE_ONLY 落盘、AUTO_EDIT 复用；纯文件、原子写 |
| `transcript_correction_llm.py` | **DSH 模型全文纠错**（2026-10-14，10-15 patch 化）：只修同音/漏字/重复/标点/断句，不润色不强制术语表；uncertain 保留原文；**输入不带时间戳**、时间戳程序复制；**模型只返回修改段 patch**，整篇紧凑输入（超限降级「摘要+少量重叠」分批）；输出超上限提档重试；单批失败仅该批回退保守规整，engine/fallback_reasons 留痕 |
| `content_analysis.py` | **AUTO_EDIT 第三步内容分析**：一次结构化调用提取 keywords/golden_quotes/topic_units/candidate_intervals；金句逐字校验、时间戳由分段重算（模型无权产生）；失败自动本地回退；`tasks/analysis/<job>.json` |
| `clip_planning.py` | **AUTO_EDIT 第四步切片规划**：连续区间（禁拼接）、柔性时长（优选 120–180s，只 warning 不淘汰）、权重 35/20/15/15/10/5、舍弃分类、`CONTENT_PLAN_MAX_CLIPS` 资源上限；失败重试一次→本地回退；`tasks/plans/<job>/clip_plan.json` |
| `clip_render.py` | **AUTO_EDIT 第五步本地渲染**：clip_plan → FFmpeg H.264+AAC 逐条裁切（零模型依赖、保持源分辨率偶数像素）、SRT 时间平移、`warnings.json`（subtitle_provenance/rule_snapshot_id）；不生成封面、无质量拦截；单条技术故障隔离、可解码成片幂等跳过 |
| `reedit_replace.py` | **重新剪辑并替换旧切片**：独立 staging、激活前精确清单 `purge_manifest.json`、两阶段回收站（引用计数 + 白名单根）、`reedit_report.json`；`POST /studio/projects/{pid}/reedit/{run}/retry-cleanup` 只重试清理 |
| `llm_budget.py` | **模型 token 台账**（2026-10-15）：预留制估算（prompt 估算+max_tokens+固定开销）与当日台账（`tasks/llm_usage/YYYY-MM-DD.json`，真实 usage+预留值）；**仅观测不拦截**（预算闸门已按用户拍板删除，额度不足由各阶段自动回退兜底） |
| `cover_replace.py` | 封面资产化：备份、修订、撤销、**片头局部替换**、幂等 |
| `clip_transform.py` | 非破坏变换：归一化/钳制、画布矩形契约、FFmpeg 滤镜链生成 |
| `source_store.py` / `source_gc.py` | 源片**单副本化**（硬链接）与陈旧副本回收 |
| `timeline_copilot.py`、`studio_insights.py`、`studio_job_worker.py`、`studio_job_store.py` | 时间线工程与 EDL、金句/关键词（legacy 链）、Studio 队列 worker（**三路分派：transcribe_only / auto_edit / legacy_full**；AUTO_EDIT 为 staged 流水线，见 §5.2）与任务存储（claim 终态排除集合含全部新终态） |
| `video_utils.py`、`clip_editor.py` | legacy 剪辑/导出链（仅历史任务页使用） |

### 3.3 video-brain `/domain/video-brain/app`
| 模块 | 职责 |
|---|---|
| `pipeline.py` | 任务状态机与步骤编排（transcribing → correcting → planning → rendering → QC） |
| `asr.py` + `medical_terms.py` | 转写与**热词注入**（词表 mtime 热更新，220 token 预算内安全截断） |
| `correct.py` | 纠错：术语表权威 + **四条医疗红线** + `uncertain` → 审核闭环 |
| `planning_prompt.py` + `planner.py` | **成员裁定版规划提示词**（逐字固化）+ 硬校验（时长/重叠/证据/条数） |
| `cover.py` / `cover_layout.py` | 封面两版风格渲染与版面引擎（`cover_layout.json`） |
| `media.py` / `render.py` / `subtitles.py` / `qc.py` / `reframe.py` | 切片渲染、封面片头、字幕、质检、重构图 |
| `schemas.py` / `store.py` / `tasks.py` / `preflight.py` | 请求契约、任务快照与工件、任务视图、**建任务环境门**（如可用磁盘 ≥500MB） |
| `knowledge.py` / `doctor_profiles.py` / `llm.py` | 知识检索、医生档案、LLM 封装 |

`config/`：`editing-workflow.manifest.json`（规则清单）、`cover_styles.json`（封面风格参数）、`medical_terms.txt`（热词/术语表）、legacy 规则（最低优先级默认层）。

---

## 4. 数据模型与落盘

### 4.1 项目台账（**卡片的唯一事实源**）
`tasks/projects/{project_id}/clips.json`（`schema_version=1`）
```
source_video:{path,sha256,bytes,mtime,registered_at}
clips.{clip_id}: clip_id,name,title,topic,keywords,source_start/end,duration,
                 render_task_id(首渲任务，永不改), render_revision, render_history[],
                 cover:{cover_asset_id, revision, active_revision, style_id, path, sha256, bytes,
                        intro_burned, kind, history[]},
                 subtitle_style{}, timeline{updated_at,ranges,task_id,pending_rerender},
                 workflow_version, skill_versions{}, workflow_snapshot_path
task_history[]: {task_id, task_type, status, stage, instruction, summary, error,
                 workflow_version, skill_versions}
```
**不变量**：一条 clip 一张卡；卡片按 `clip_id` 渲染；封面更新**原地替换**、不新增 clip。

### 4.2 任务目录与状态
```
tasks/in|processing|completed|failed/<task_id>.json   任务输入/中间态
tasks/status/<task_id>.json                            状态镜像（含 task_type、版本字段）
tasks/media/<task_id>/source.mp4                       该任务的源片**硬链接**
tasks/output/<task_id>/<clip_id>/{clip_final.mp4,cover.jpg,cover_layout.json,
                                 cover_revisions/rev-NNN/,cover_backup/,cover_replace.json,
                                 subtitle_style.json,clip_timeline.json}
tasks/localtasks/<task_id>.json                        局部任务记录
tasks/logs/local_tasks.jsonl                           局部任务 JSONL 日志
tasks/projects/<project_id>/clips.json                 项目台账
tasks/analysis/<job_id>.json                           AUTO_EDIT 内容分析记录（第三步）
tasks/plans/<job_id>/clip_plan.json                    AUTO_EDIT 切片计划（第四步）
tasks/output/<job_id>/clips/<clip_id>/{clip_final.mp4,subtitles.srt,warnings.json,clip_plan.json}
                                                       AUTO_EDIT staged 成品（第五步，无 cover.jpg）
tasks/output/<run_id>/{purge_manifest.json,ACTIVATED,reedit_report.json}
                                                       重新剪辑替换：精确清单/激活标记/报告
tasks/llm_usage/<YYYY-MM-DD>.json                      模型 token 台账（预留值+真实 usage）
```
任务类型前缀：`autoedit_`（AUTO_EDIT）/ `cover_` / `title_` / `subtitle_` / `edit_`（四类局部任务）。

### 4.3 源片单副本化
```
studio-uploads/<id>.mp4                      ← 唯一物理数据（canonical）
 └ studio-jobs/<job>/source.mp4                         （硬链接）
    └ studio-projects/timeline_*/raw/source.mp4         （硬链接）
       └ tasks/media/<task_id>/source.mp4               （硬链接）
```
- 同盘优先 `os.link`（零额外占用）；跨设备回退「512MB 空间守卫 + `.part` 原子改名复制」；
- **契约：源片全链路只读**，任何组件不得就地改写（否则顺硬链接串改原片）；
- `python -m src.source_gc`（默认 dry-run）回收陈旧副本，保护「唯一副本/被引用/使用中/保护期」四类。

### 4.4 规则与快照
- 清单：`video-brain/config/editing-workflow.manifest.json`（`workflow_version` + skills[id/version/source_path/checksum/schema/priority/enabled] + 10 类规则段）；
- **任务级快照**：`tasks/output/<task_id>/workflow_snapshot.json`（版本 + 各 skill checksum + 生效规则 + `rules_source`）；
- 优先级：**用户本次指令 > 项目设置 > 已启用 Skill > 系统默认（legacy）**；
- 启停 skill：备份 `<清单>.bak.<UTC>` → 写回（版本号 +1）→ 立刻校验 → 非法逐字节回滚。

### 4.5 数据库（PostgreSQL）
业务与 Studio 双轨：`tasks/sources/generated_clips`（legacy 剪辑链）、`studio_jobs/studio_upload_sessions/studio_artifacts`（Studio 队列与工件）、`studio_copilot_messages/studio_copilot_actions`（助手对话与动作留痕）、`api_keys`、`app_settings`、Better Auth 表（`users/session/account/verification`）。

### 4.6 临时目录（`backend/temp/`）
`studio-uploads`（上传源，受保护不删）、`studio-jobs`（任务工作区）、`studio-projects`（时间线工程与 EDL）、`studio-chunks`（分片残留，可 GC）、`objectstore`（本地对象模式）、`studio-transform-exports`（变换导出缓存，按 transform 哈希）。

---

## 5. 关键流程

### 5.1 上传 → 转写 → 纠错（TRANSCRIBE_ONLY 任务，2026-10-14 拆分）
1. 前端分片上传（`/studio/transcribe/chunk|assemble` 或 R2 直传）→ 源片落 `studio-uploads`；
2. `POST /api/jobs/transcribe`（`mode=transcribe_only`）→ `studio_jobs`（config.`job_kind=transcribe_only`）→ worker 隔离子进程；
3. worker：`extracting_audio`（ffprobe 音轨校验）→ whisper 本地转写（注入医学热词）→ **DSH 模型全文纠错**（`transcript_correction_llm.py`：patch 协议只返回修改段；同音/漏字/重复/标点；uncertain 保留原文；时间戳程序复制；输出超上限提档重试、整篇超限降级分批；失败回退保守规整并留痕）→ **版本化落盘** `tasks/transcripts/<source_id>/v{n}.json`（`studio_transcript_store.py`；分段含 `segment_id/start_ms/end_ms/raw_text/corrected_text/uncertain/correction_reason`）+ `transcript_json` artifact 回传 → 终态 **`transcript_ready`**（认领终态集合含它，不会被重复认领）；
4. **明确禁止**：金句/规则/规划/渲染/卡片——转写完成左侧不出现成片；
5. 前端只读展示「纠错后转写」（时间码 + 纠错文 + 原文折叠 + uncertain 标记 + 纠错原因 + 点击跳转；**不可编辑**；错字通过热词表与纠错提示词改进）。

### 5.2 AUTO_EDIT staged 流水线（2026-10-14/15 起，前置=有效纠错转写）

```
POST /api/jobs/auto-edit（object_key/project_id/transcript_id/transcript_version）
  → 存储校验：load_corrected_transcript 无效 → 422「请先完成转写与纠错。」
  → studio_jobs（config.job_kind=auto_edit；正式任务不带 stop_after）
  → retrieving_rules（记 rule_snapshot_id=清单 workflow_version）
  → extracting_highlights（第三步：模型内容分析 → tasks/analysis/<job>.json + artifact）
  → planning（第四步：模型切片规划 → tasks/plans/<job>/clip_plan.json + artifact）
  → rendering（第五步：本地 FFmpeg 逐条渲染 → tasks/output/<job>/clips/<clip_id>/…）
  → completed / completed_with_warnings / partial_succeeded / technical_failed
诊断停点：stop_after=analysis → analysis_ready；stop_after=plan → plan_ready（终态，不渲染）
```

- 全程**不调用** `stage_transcribing`/`stage_correcting`；输入只有已保存的指定版本纠错转写 + 规则清单版本；
- **无质量拦截**：规划/时长/字幕一般问题全部只记 warning；只有真实技术故障（FFmpeg 失败/产物不可解码）才 `technical_error`；
- **恢复语义**：分析/计划工件已落盘则直接复用（不重复调用模型）；已可解码的 `clip_final.mp4` 渲染时跳过（worker 重启恢复）；
- **前端卡片**：staged 链不写项目台账，成品卡片由前端从 `clip_final` + `clip_plan` artifacts 构建；
- **不用 video-brain**（bridge/runner/`planner.plan_v2`/QC 那条是 legacy 链，仅存量任务）；
- `config` 无 `job_kind` 的存量任务走 legacy 全链（行为不变）。

### 5.2.1 重新剪辑并替换旧切片（2026-10-15，replacement_mode=delete_old_after_success）

- 提交：`POST /api/jobs/re-edit`（复用源片 + 指定版本转写，`reuse_transcript`/`force_new_run` 强制 true；新 `run_id` + `idempotency_key`），前端顶栏按钮**一次确认**（不重传/不 ASR/不纠错）；
- 状态机：`reedit_requested → staging_run → extracting_highlights → planning → rendering → verifying_outputs → activating_run → purging_old_clips → completed / completed_with_warnings / completed_with_cleanup_warning`（verify 失败 → 清 staging → `technical_failed`，旧切片全部保留）；
- 新批次写独立 staging `tasks/output/<run_id>/`；激活前落 `purge_manifest.json`（精确删除目标，白名单根 `tasks/{output,completed,failed,status,localtasks,logs}`，引用计数剔除被共享任务，`media/transcripts` 与新 run 永不入清单）；
- ≥1 条新切片可解码才 `activate_new_run`（`register_clips` 幂等登记台账 + `ACTIVATED` 标记）→ 两阶段回收站 `tasks/.purge-trash/`（先整体搬入、台账切换成功才物理删除，失败反向搬回）→ `reedit_report.json`（删除数/文件数/释放字节/失败项）；
- 清理失败 → `completed_with_cleanup_warning`，`POST /studio/projects/{pid}/reedit/{run}/retry-cleanup` 只重试清理；成功后前端素材区只剩新切片。

### 5.3 局部修改（**不重跑 AUTO_EDIT、不新增卡片**）
| 用户说法 | 任务类型 | 实际动作 |
|---|---|---|
| 重新制作封面 / 主题压缩到 12 字 | `REGENERATE_COVER` | 快照取风格与字数规则 → `cover_replace.replace_cover`（video-brain `cover.render`）→ 新 revision，**只重拼 0.8s 片头** |
| 封面不要人物抠图 | `REGENERATE_COVER` + 风格覆盖 | 风格切 `realframe_v2`（`cutout_v3` 缺权重则显式失败） |
| 重新生成标题 | `UPDATE_TITLE` | 规则取 `title_rules`（忠于转写 + 禁用词字面校验） |
| 字幕放大一点 | `UPDATE_SUBTITLE_STYLE` | 只落字幕样式配置 + `render_revision`（字幕合成 adapter 未实现，显式标注） |
| 删除第 8–10 秒 / 重剪短一点 | `UPDATE_TIMELINE` | 需要时 `run_clip_planning_skill` 重选区间；`rerender` 为显式开关时走 `run_render_skill` |
| BGM 小声一点 / 淡入 2 秒 / 去掉背景音乐 | `UPDATE_AUDIO`（助手动作 `update_bgm`，2026-10-13 补） | 前端按当前音轨算补丁只动 `kind=bgm` 轨；换新 BGM 文件引导去 Inspector 效果页上传；混音在导出侧生效 |

### 5.4 封面资产化与片头替换
- 封面是 clip 的**独立资产**：`cover_asset_id=cover-{clip_id}-v{n}`，历史版本可撤销；
- 片头替换：关键帧切点三段拼接（新片头 + 原片尾），**正片逐帧不变**（实测 581 帧 / 23.2762s 不变，仅第 1–19 帧变化）；
- 幂等：同风格重跑哈希一致则不叠加版本、不重复拼片头。

### 5.5 画面变换与导出（预览 = 导出）
- 两条轴严格分离：`viewerZoom`（只看，不影响导出）vs `clip transform`（`scale/positionX/positionY/crop`，影响导出）；
- 数学单一来源：后端 `clip_transform.transform_to_canvas_rect` 与前端 `lib/clip-transform.ts` 由 10 组对照向量逐值守护；
- 导出分发：默认变换 → 直下原 `clip_final.mp4`；非默认 → `GET .../export-transformed`（FFmpeg 按 transform 重渲，按哈希缓存）。

### 5.6 规则优先级与可复现
任务创建时冻结快照 → 后续步骤只读快照 → 规则更新只影响新任务；旧成片可经 `GET /studio/workflow/{task_id}/snapshot` 查看历史规则版本（无快照的旧任务显式 `found:false`，不伪造）。

---

## 6. 关键不变量（改动前务必确认）

1. **稳定 `clip_id`**：`render_task_id` 首渲后永不改；局部修改只 bump `cover_revision` / `render_revision`；
2. **一卡一片**：卡片来自项目台账，禁止按任务聚合（否则重制封面会新增重复卡片）；
3. **源片只读 + 单副本**：新增任何写源片路径的实现都会破坏硬链接语义；
4. **预览与导出同源数学**：前端 CSS 变换不得成为第二套实现；
5. **规则快照冻结**：运行中的任务不得读取实时清单；
6. **错误必须显式**：未实现/依赖缺失/空间不足一律带机器码抛错（`E_*`），禁止静默降级；
7. **合规红线**：不改诊断/药名/剂量/否定词；导出加 AI 标识；免责声明；审核留痕（详见 `docs/project-assets.md` 与合规清单）。

---

## 7. 运维

| 操作 | 命令/位置 |
|---|---|
| 重启后端 | 确认无运行中任务（`tasks/status/*.json` 无 processing/queued、无 `studio_job_worker --job` 子进程）→ `kill <studio_server_pid>`，supervisor 自动拉起 |
| 重启 video-brain | 同上 → `kill <uvicorn_pid>`，supervisor 自动拉起 |
| 前端 | dev 模式热更新，无需重启；生产需 `pnpm build && pnpm start` |
| 健康检查 | 8000 `/tasks/`→401 属正常（鉴权）；8940 `/health`；前端 3107 |
| 日志 | `backend/logs/services/{backend,autoedit,video-brain,frontend}.log`；视频脑 `data/access.log` |
| 磁盘 | 建任务环境门：video-brain 要求数据盘 ≥500MB；改动前用 `df -h /domain`；`python -m src.source_gc [--apply]` 回收 |
| 规则 | 改 `config/editing-workflow.manifest.json`（或经规则中心启停）→ 下次任务生效；词表直接改 `medical_terms.txt` 即生效 |
| 测试 | 后端 `cd backend && .venv/bin/python -m pytest tests/unit -q --no-cov`（**1092 例**）；video-brain `cd /domain/video-brain && .venv/bin/python -m pytest tests -q`（**126 例**）；前端 `npx tsc --noEmit`（0 error）、`npx vitest run`（38 文件 **326 例**）、`npm run lint`（**必须用 npm run lint**，裸 npx eslint 假警报；截至 2026-10-15，不得回退） |

---

## 8. 已知限制与技术债

1. **字幕合成 adapter 未实现**（video-brain 无可跨服务调用的字幕端点）；说话人分离、金句提取同样为显式 `E_SKILL_NOT_IMPLEMENTED`；
2. `UPDATE_TIMELINE` 的重渲是**显式开关**，产物回传仍走既有执行器导出通道；
3. `cover_rules.intro_duration_s` 只入库，**片头时长权威在 video-brain 的 `cover_styles.json`**（两处若不一致以 video-brain 为准）；
4. 时间线为单轨（无多轨/关键帧）；「删除」卡片目前仅视图移除；
5. legacy 剪辑链（`video_utils.py`/`clip_editor.py` + `/tasks/*` 路由）仍在仓库中，仅服务历史任务页；
6. `backend/temp` 长期增长（`studio-jobs` 曾达 12G），需定期 GC；
7. 规则「变更历史（谁改了哪条）」与「从 `.bak` 一键回滚」尚未做 UI；
8. **staged 链不做 9:16 重构图、不生成封面**：`clip_render` 保持源分辨率（仅偶数像素）直接裁切；封面资产化/重构图仍属台账局部修改链（legacy 产物），是否补齐待产品拍板；
9. **staged 链不写项目台账**：成品卡片由前端从 artifacts 构建；只有「重新剪辑替换」经 `register_clips` 写台账（激活新批次 + 清除旧切片）；
10. **模型额度台账只覆盖管线三环**（纠错/分析/规划）；Copilot 同享应用额度但不在台账内（不虚报全覆盖）。额度不足不排队（预算闸门已删除），由各阶段自动回退兜底。
