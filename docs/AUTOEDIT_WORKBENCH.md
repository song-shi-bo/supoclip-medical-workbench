# 自动化剪辑工作台 · 功能与流程全景

> 截至 2026-11-08（main @ `92743b6`）。**文档与代码不一致时以代码为准。**
> 用途：新成员上手、排障定位、二次开发的单一入口。本文是「现在真实在跑」的那条链；
> 历史细节见 `docs/EDITING_PIPELINE.md`（按日期分节），video-brain 桥接见 `docs/autoedit-runner.md`。

---

## 1. 总览与运行形态

```
浏览器 /studio（Next.js 15，dev:3107，supervisor 托管）
   │  同源 /video-api → Caddy → api.sock（studio_server: FastAPI main_refactored:app，:8000）
   ▼
Studio API ──任务写 Postgres(studio_jobs 租约)──▶ studio_job_worker
   │  GET /studio/timeline|clips|subtitles|transcripts…（enforce_studio_access 全局门禁）
   ▼
worker（每任务独立子进程，3600s 上限）—— staged 全自动流水线：
   内容分析 → 切片规划(提示词A) → 切片精修(提示词B) → 封面 → 本地 FFmpeg 渲染
   │  产物落 tasks/output/<job>/clips/<clip_id>/（clip_final.mp4 + cover.jpg + ASS/SRT + warnings）
   ▼
成片卡片 / 项目台账(clips.json, generation 锁) / 时间线工程(edit/edl.json)
   ── 后期编辑：转写三态+修订、说话人标注、字幕版本、封面版本、效果/BGM、AI 助手 ──▶ 投递(抖音)
```

### 服务清单（`backend/src/studio_supervisor.py` 托管，全部本机端口）

| 服务 | 启动命令（cwd） | 端口 | 说明 |
|---|---|---|---|
| postgres | `.tools/pg16/bin/postgres -D $PGDATA -p 5432` | 5432 | studio_jobs 租约/心跳、计费、API keys |
| redis | `.tools/redis-src/src/redis-server --port 6379` | 6379 | 队列/缓存池 |
| frontend | `pnpm dev --port 3107`（frontend/） | 3107 | Next.js dev + Turbopack |
| frontend-proxy | `python -m src.studio_frontend_proxy`（backend/） | — | unix socket 转发 |
| backend | `python -m src.studio_server`（backend/） | 8000 | FastAPI 主 API（`src.main_refactored:app`） |
| video-brain | `uvicorn app.main:app --port 8940`（/domain/video-brain） | 8940 | 医疗切片流水线（**legacy 桥**，仅存量任务） |
| autoedit | `python3 backend/src/autoedit_runner_video_brain.py` | — | 旧文件队列执行器（legacy） |
| worker | `python -m src.studio_job_worker`（backend/） | — | **现役 staged 流水线执行器** |

日志：`<repo>/logs/<服务名>.log`。容器内存上限 ~6GiB：**Next.js 编译与视频渲染/ASR 不要同时跑**（曾 OOM 137）。

---

## 2. 工作台（/studio）现有功能

前端入口 `frontend/src/app/studio/page.tsx` + `frontend/src/components/studio/*`。

| 功能 | 说明 | 主要代码 |
|---|---|---|
| 项目管理 | 项目列表/切换/清理（清数据文档 `docs/clear-project-data.md`） | `project-manager.tsx`、`POST /studio/projects/{id}/data/clear` |
| 素材导入 | 分片上传（本地/R2 可选、断点续传）→ 转写入口 | `POST /api/uploads`、`/api/uploads/{id}/complete`（jobs.py） |
| 成片卡片墙 | 台账卡片（封面/时长/说话人状态/revision），缺失工程有只读成果清单 | `GET /studio/projects/{id}/clips`、`GET /studio/projects/{id}/exports` |
| 播放器 | 9:16 预览、封面帧、播放控制条、实时字幕叠加层（**成片已烧录字幕时自动关闭防重影**） | `page.tsx`、`player-control-bar.tsx`、`inspector/subtitle-overlay.tsx` |
| 时间线编辑 | 多轨 EDL、轨道增删、undo/redo/revision 历史、音频资产+波形 peaks、画面变换（transform） | `studio-timeline-editor.tsx`、`timeline-tracks.tsx`、`transform-panel.tsx`；`/studio/timeline/{id}/*` 路由组 |
| Inspector 三标签 | **转写**（三态稿+修订+说话人标注）、**封面**（版本/模板/历史/回收站）、**效果**（音量/BGM/静音/字幕样式预览） | `inspector/inspector-pane.tsx`（`INSPECTOR_TABS`） |
| AI 剪辑助手 | 自然语言下达剪辑指令（copilot），动作结果回执 | `POST /studio/copilot`、`timeline_copilot.py`、`studio_copilot.py` |
| 精修转写面板 | 切片精修（提示词B 产物）的只读消费视图 + refined-copy.md | `refined-transcript-panel.tsx`、`GET .../refined-transcript` |
| 成片投递 | 发送到抖音收件箱（95 中转 → 本地拉取 → 私密上传 → 手机复核） | media.py「成片投递」路由组（2026-10-28） |
| 管理面 | admin/feedback/计费/API Keys/知识库入库 | `admin.py`、`billing.py`、`api_keys.py`、`feedback.py`、`ingest_kb.py` |

---

## 3. 自动剪辑端到端流程

两类独立任务（`docs/EDITING_PIPELINE.md` 契约）：**TRANSCRIBE_ONLY**（只转写+纠错，终态 `transcript_ready`）与 **AUTO_EDIT**（从有效纠错转写起步，绝不重跑转写/纠错）。

### 3.1 TRANSCRIBE_ONLY（转写+医学纠错）

| # | 阶段 | 实现 | 工具 | 产物 |
|---|---|---|---|---|
| 1 | 音轨校验 extracting_audio | 抽音轨/校验可解码 | ffmpeg | 中间文件 |
| 2 | 转写 transcribing | `transcribe_with_faster_whisper`（热词注入、VAD 过滤） | faster-whisper（`FASTER_WHISPER_MODEL`，默认 small/cpu） | 带 `segment_id` 的原始段 |
| 3 | 医学纠错 correcting | 同音词库 + LLM patch 协议（整篇紧凑输入/只返回修改段）+ 相似度安全门 | `transcript_correction*.py`、`medical_lexicon.py`、`project_glossary.py`、统一 LLM 网关（`ai.py`，额度记 `llm_budget`） | `tasks/transcripts/<source_id>/v{n}.json` + `correction_audit`（原始↔纠错逐段对照，含 `segment_id/raw_text/corrected_text`） |

终态 `transcript_ready`。版本化存储支持多版本（v1/v2…），后续所有链路凭 `object_key+transcript_id+transcript_version` 引用。

### 3.2 AUTO_EDIT staged（主链，`studio_job_worker.py::run_auto_edit_only`）

| # | 阶段 | 实现 | 工具 | 产物 |
|---|---|---|---|---|
| 0 | retrieving_rules | 记 `rule_snapshot_id`（剪辑规则只读留痕，QC 永不拦截） | — | rule snapshot |
| 1 | 内容分析 extracting_highlights | 模型全篇分析（主题/关键词/金句候选） | LLM 网关 | `tasks/analysis/<job>.json` |
| 2 | 切片规划 planning（提示词A） | 连续时间区间切片、动态条数、硬性校验（时间不越界/文本忠于转写/禁词表） | `clip_planning.py` + LLM | `tasks/plans/<job>/clip_plan.json` |
| 3 | 切片精修 refining（提示词B） | 逐切片精修字幕文本 + **逐段说话人（医生/患者/家属）**，失败粒度回退不阻断 | `clip_refinement.py` + LLM | `tasks/plans/<job>/clip_refinement.json`（`clips[clip_id].segments`：`segment_id/start_ms/end_ms/refined_text/speaker`） |
| 4 | 封面 cover | realframe_v2：真实视频帧+文字叠加（纯本地，无 AI 生图）；`clip_final` 前 0.8s 替换为封面片头（时长不变契约）；失败只 warning | `cover_realframe_v2.py`、`cover_replace.splice_intro`、ffmpeg | 切片目录四产物 + `cover_revision` |
| 5 | 渲染 rendering | clip_plan 的 `start_ms/end_ms` + 指定版本纠错转写 → **本地 FFmpeg 逐条渲染**：横转竖居中裁切（608×1080→1080×1920）、H.264+AAC、SRT 时间平移、**字幕同遍烧录**（2026-10-29 固化）；源片本地硬链接不重传 | `clip_render.py`、ffmpeg（`CONTENT_RENDER_FFMPEG`） | `tasks/output/<job>/clips/<clip_id>/clip_final.mp4` + cover.jpg + ASS/SRT + warnings.json |
| 6 | 回传/登记 | 渲染同一遍产出的字幕/封面四产物登记 artifact；卡片/台账构建 | `store.register_clips`（generation 锁，幂等） | 项目 `tasks/projects/<id>/clips.json` |

**隔离与恢复**：单切片技术故障记 `technical_error` 继续其余；已存在且可解码的 `clip_final.mp4` 直接跳过（worker 重启恢复）；终态 `completed / completed_with_warnings / partial_succeeded / technical_failed`。**全程无质量拦截**（只有 warning）。

### 3.3 重新剪辑（re-edit 两阶段替换）

`POST /api/jobs/re-edit` → 复用源片+已纠错转写 → 独立 staging 渲染 → **≥1 条可解码才激活** → 按激活前清单清除旧切片（两阶段回收站，`reedit_replace.py`）→ 素材区只剩新切片。注意：re-edit 渲染**没有自己的 clip_refinement**，其切片转写 seed 走纠错审计窗口裁剪兜底（见 §4.2）。

### 3.4 legacy video-brain 桥（仅存量任务）

`autoedit_runner_video_brain.py` 消费旧文件队列 `tasks/in/autoedit_*.json` → video-brain :8940 固定流程（预检→转写→纠错→规则→规划→渲染→QC）→ 回写 `tasks/status/<task>.json`。新任务不再走这条链。

---

## 4. 成片之后：后期编辑与产出链路

### 4.1 项目台账与时间线工程
- 台账 `tasks/projects/<project_id>/clips.json`：卡片/封面/修订历史/`generation` 乐观锁；`POST /studio/projects/{id}/sync` 从任务产物同步。
- 时间线 EDL `edit/edl.json` + 轨道/视图/历史：`/studio/timeline/{id}` 路由组（tracks/undo/redo/revision/peaks/audio）；画面变换 `PUT .../transform`（裁切/缩放/旋转，`clip_transform.py`）。

### 4.2 转写三态、修订与说话人标注（2026-11-06/08 新近固化）
- **三态稿**：`raw`（审计原始 ASR）↔ `corrected`（纠错稿）↔ `effective`（当前有效），逐段修订可撤销（`transcript_revisions.py`；seed 幂等，已建文档绝不覆盖）。
- **seed 链路（惰性建基线）** `media._clip_seed_baseline`，三级回退：
  1. 旧 `autoedit_*` 任务 → `read_auto_edit_task` 的 clips[].segments；
  2. 新管线 32 位 job id → `clip_refinement.json` 逐切片分段（**自带医生/患者/家属**，按 `source_start` 平移到成片基）；
  3. 以上皆缺（如 re-edit 重切）→ 纠错审计按当前窗口裁剪 `corrected_text`（真实文本，不伪造）。
- **说话人标注三层**：
  1. 精修自带（主来源，LLM 逐段标）；
  2. 源级 overlay `tasks/transcripts/<source_id>/speaker_labels.json`（窗口化 LLM，`speaker_labeling.py`；时间线按 `segment_id` 合并显示）；
  3. worker 精修降级自动补跑 overlay（`_spawn_speaker_labeling_backfill`，断点续跑+额度记账）。
  seed 时 `annotate_clip_segments` 按时间窗把 overlay 回填进未标注空位——**新项目零操作即得医患标注**（2026-11-08 起）。

### 4.3 字幕版本与烧录
- `POST /studio/clips/{p}/{c}/subtitles`：cues→overrides、`clip_config.update_config`（真变化才 bump `render_revision`，`task_history` 记 `UPDATE_SUBTITLE`），生成 `subtitles/rev-00N/{clip}.ass|.srt`。
- `GET` 返回 cues+config+overrides+版本列表 + **`subtitles_burned_in`**（读成片 `warnings.json`：`subtitle_mode=burned`）——前端据此**关闭播放器实时叠加层防重影**（2026-11-08 修复）；叠加层只服务未烧录（Phase 2 导出）场景。时间线字幕轨不受影响。
- 字幕合成公式与后端 ASS 生成器一致（`subtitle_compose.py`、`caption_templates.py`、`emoji_captions.py`；默认 44px 金黄 #FFD700、y_pct 78.125）。

### 4.4 封面体系
- realframe_v2 固化封面（渲染时自动）；手动替换/模板应用/多版本历史/回收站/撤销激活：`cover_templates.py`、`cover_replace.py`、`bump_cover/set_cover_pointer/prune_cover_history`（`studio_project_store.py`，原子 rev+1）。

### 4.5 效果与音频
- 音量/静音/BGM 音轨：Inspector「效果」标签，预览即时生效；配置保存 bump `render_revision`；BGM 上传/管理走 media.py BGM 路由（波形 peaks）。

### 4.6 AI 剪辑助手
- `POST /studio/copilot`：自然语言指令 → `timeline_copilot`/`studio_copilot`（LLM）→ EDL/字幕/封面动作 + `action-result` 回执；字幕人工微调走自然语言指令（独立字幕编辑入口已移除）。

### 4.7 投递与成果清单
- 投递：成片 → 抖音收件箱（95 中转 → 本地拉取 → 私密上传 → 手机复核），ACL 校验项目归属防跨项目投递。
- `GET /studio/projects/{id}/exports`：工程缺失时的只读成果清单（MP4/SRT 下载定位 + 源区间），复用 artifact 下载鉴权。

---

## 5. 工具与基础设施清单

| 工具/设施 | 位置/入口 | 用途 |
|---|---|---|
| ffmpeg / ffprobe | 容器内 `ffmpeg`（DSH 固化版 `/domain/dsh-home/bin/`，7.0.2 static） | 渲染、封面、音频、抽帧、探针 |
| faster-whisper | `FASTER_WHISPER_MODEL`（默认 small），CPU+VAD | 转写 |
| LLM 网关 | `ai.py`（Pydantic AI）+ `llm_budget` 当日台账 | 纠错/分析/规划/精修/说话人标注/copilot/封面文案 |
| 医学词库 | `medical_lexicon.py`、`project_glossary.py`（video-brain 侧：`medical_terms.py`、`doctor_profiles.py`、`kb_context.py`） | 纠错与领域上下文 |
| 字幕体系 | `subtitle_compose.py`、`caption_templates.py`、`emoji_captions.py`、`font_registry.py`（`backend/fonts/` 自动发现） | ASS/SRT 生成与烧录 |
| B-roll | `broll.py`（Pexels API，`PEXELS_API_KEY`） | 备用素材（可选） |
| 存储 | 本地 `tasks/`（in/processing/completed/failed/output/plans/analysis/status/transcripts/projects/logs）；R2 可选（`r2.py`，无凭据时降级本地） | 任务与产物事实源 |
| 数据库/队列 | PostgreSQL 16（`.tools/pg16`）、Redis 7.2（`.tools/redis-src`） | studio_jobs 租约、台账、计费、缓存 |
| 访问控制 | `studio_access.py`（`AccessLedger`，默认 `/domain/.studio-resources/access/access.sqlite3`）+ 全局 `enforce_studio_access` | 对象级 ACL（task/project/clip），未登记 → 403 `studio_object_unregistered` |
| API Keys | `api_keys.py` + `auth_headers.resolve_authenticated_user_id`（只存 SHA-256） | 程序化访问 /fonts、/studio/transcribe |
| 额度台账 | `llm_budget.py`（按日、按 kind） | 所有 LLM 调用记账；超限走各阶段自动回退 |
| ASR 锁 | `/domain/.studio-resources/asr.lock` | 同时刻一个 ASR 模型（内存保护） |

---

## 6. 文档索引

- `docs/EDITING_PIPELINE.md` — 剪辑流程按日期的权威演进记录（规则清单/纠错门禁/两步制来龙去脉）
- `docs/autoedit-runner.md` — video-brain 桥（legacy）文件队列契约
- `docs/ARCHITECTURE.md` / `docs/architecture.md` — 系统架构
- `docs/app-guide.md` / `docs/api-reference.md` / `docs/configuration.md` / `docs/development.md`
- `docs/inspector-phase1-contract.md` — Inspector 接线契约
- `docs/copilot.md` — AI 助手
- `docs/clear-project-data.md` — 项目数据清理
- `CLAUDE.md` / `AGENTS.md` — 仓库级开发约定（注意：其「video-brain 是唯一管线」的描述已被 staged 本地链取代，以本文与代码为准）
- `DSH_STARTUP.md` — 本容器端口/工具/截图目录等运行事实
