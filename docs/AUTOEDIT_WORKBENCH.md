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
   内容分析 → 切片规划(提示词A) → 切片精修(提示词B) → 本地 FFmpeg 渲染
   │  产物落 tasks/output/<job>/clips/<clip_id>/（clip_final.mp4 + ASS/SRT + warnings）
   │  封面：STUDIO_AUTO_COVER 开关（2026-11-09 起默认关闭=不制作封面/不拼片头；
   │  手动制作链——重制封面/版型库/封面微调——不受开关影响）
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
| 精修转写面板 | 切片精修（提示词B 产物）的只读消费视图（四列：#/时间码/角色/上屏字幕，医生/患者/家属标注）+ refined-copy.md；**2026-11-09 起切片视图只保留此面板**（下方重复的转写分段列表已按用户指令删除） | `refined-transcript-panel.tsx`、`GET .../refined-transcript` |
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
| 1 | 内容分析 extracting_highlights | 模型全篇分析（主题/关键词/金句候选）；主题簇带 **mechanism/hook_quote/ending_quote/patient_question/doctor_core_segment_ids**（机制限钩子四机制词表；引用句与患者提问逐字校验违者清除留痕；医生核心段必须为该块 segment_ids 子集、非法 id 清理留痕——问答化重组 P2，2026-11-11）+ **info_seconds**（程序按分段时长求和，模型无权产生）；heuristic 回退各字段留空 | `content_analysis.py`（analysis/v4 + analysis-prompt/2026-11-11） + LLM 网关 | `tasks/analysis/<job>.json` |
| 2 | 切片规划 planning（提示词A） | 连续时间区间切片、动态条数、硬性校验（时间不越界/文本忠于转写/禁词表）；拼装层背景透出主题簇五字段（含患者提问钩子候选，提示词A 原文不动，slice-a3）；**医学闸门（2026-11-10 第三库）**：规划前 `clip_medical_gate.run_gate` 对主题块做向量+词法匹配（ melanoma 指南快照，纯本地不调 LLM），标注随拼装层透出【医学主题匹配】行（只标注标签与匹配度，条目正文不进提示词）；规划后 `enforce_clips` **硬剔除**：医学分段密度 <0.2 且主题块级别 none 的切片移入 discarded（type=非医学闲聊，附证据；2026-11-10 用户拍板宁缺毋滥）；**领域激活防误伤**：仅全片存在 strong 级命中才执法，否则只标注不剔除；闸门任何失败降级不阻断；闸门身份（快照 sha256+阈值）进 plan 指纹，库变了旧缓存自动失效。设计与条目见 `docs/medical-gate-design.md`；**净化重组（2026-11-11 问答化 P1，P2 扩展）**：规划后 `clip_purify.purify_clips` 删除切片内应答碎片与患者插话，产出有序子区间 `sub_ranges`——**删除授权阶梯**：纯应答段（无需标注）< 患者话语（必须有说话人标注，qa 风格保护患者提问钩子段、mono 风格全删，`MEDICAL_PURIFY_STYLE=qa|mono`）< 医生核心段（analysis `doctor_core_segment_ids` 命中永不删）；开头吸附、净删占比超 50%（`MEDICAL_PURIFY_MIN_KEEP_RATIO`）或异常→整片回退原区间；说话人标注 overlay（`speaker_labels.json`，worker 解析并连同指纹显式传入 expected/plan_clips 两边，identity 一致性契约）可用时报告医生话语纯度（低于 `MEDICAL_PURIFY_MIN_DOCTOR_RATIO` 只 warning 不淘汰）；拼接合理性证据（命中指南主题）随 decision 透出；`sub_ranges`/`purify` 进 clip_plan.json，purify 身份（版本+风格+标注指纹+阈值）进 plan 指纹。设计与决策见 `docs/qa-style-restructure-design.md`；**钩子模式库（2026-11-12）**：拼装层注入【钩子候选】块——候选全部为本片转写逐字原句（主题块钩子句/患者提问/金句），经 8 类开头模式（案例警示/认知反差/数字预告/提问直击/亲历叙事/人群点名/比喻定调/实拍原声，从文稿库 16 条参考转写开头人工提炼）结构打分排序；模式示例文本不进提示词（加载器不读取示例字段）；规划后**逐字执法** hook_first_line（去标点归一必须命中转写，否则替换为切片内打分最高候选+notes 留痕）；模式库身份 sha256 进 plan 指纹；文稿入库自动触发模式库刷新（计数累积+LLM 新结构提议合并，版本+1 存档可回滚，`HOOK_PATTERNS_PATH/MAX/PROPOSAL_THRESHOLD` 可调） | `clip_planning.py`、`clip_medical_gate.py`、`clip_purify.py`、`hook_patterns.py`、`hook_pattern_refresh.py`、`medical_kb.py` + LLM | `tasks/plans/<job>/clip_plan.json`（含 `medical_gate`/`purify`/`hook_patterns` 摘要、逐切片标注与 `sub_ranges`） |
| 3 | 切片精修 refining（提示词B b2） | 逐切片精修字幕文本 + **逐段说话人（医生/患者/家属）**，失败粒度回退不阻断；**只删不改执法（2026-11-08）**：精修句必须是语气词清理后转写的顺序保持子串，违规段整段回退原纠错转写（`reverted_segments` 留痕；全违规→整片 fallback `delete_only_violation`）；精修窗口（2026-11-11 问答化 P1）：切片带 `sub_ranges` 时窗口取子区间并集，被净化的应答碎片不进提示词B | `clip_refinement.py`（clip-refine-prompt/2026-11-08-b2） + LLM | `tasks/plans/<job>/clip_refinement.json`（`clips[clip_id].segments`：`segment_id/start_ms/end_ms/refined_text/speaker`） |
| 4 | 封面 cover | realframe_v2：真实视频帧+文字叠加（纯本地，无 AI 生图）；**默认版型 yellow_bold_v5（2026-11-09 P2）**：两行同级黄字（theme 88/hook 110，#FFDD00 纯黑描边 6/8px）锚定胸腹区（top_ratio 0.56）+ 暗底面板 + 角标层（`cover_badge_text`），`build_cover(template=None)` 自动套用（此前 worker 路径无版型走白字默认排版）；均衡断行（优先空格分隔，杜绝单字孤行）；**选帧**：切片内每 2s 抽候选（首帧自 +1.0s 起，上限 40）→ 确定性评分＝人脸数（1 张最佳）+ 面积 + 清晰度 + **亮度项**（灰度 [90,200] 满分）+ 单脸居中项，运动模糊 ×0.6；**文案主题对齐（2026-11-09 P1）**：主题块锚点＝content_analysis `topic_units` 按切片区间重叠最大（并列优先标题与切片规划主题相关者），底料**精修稿优先**；LLM 一次出 3 组候选 → 主题校验（title 覆盖率/钩子原话命中）→ 不合格带反馈**受控重生成一次**（每切片封面文案 LLM ≤2 次）→ 仍不合格走 hint 规则组/主题词直组句兜底（有主题块时 hint 不进初选池）；hook 复述 topic（≥3 字全包含）评分 −2；**人工换帧（P3）**：`build_cover(frame_time_rel_s=)` 指定成片时间线秒，`selection_source=manual` 留痕；可读线校验（≥96px@1080，不过→纯色兜底）；`clip_final` 前 0.8s 替换为封面片头（时长不变契约）；失败只 warning。审计：`cover_copy.json` 的 `topic_unit_used/validation_report/regeneration_count/prompt_version(2026-11-09.2)/transcript_source/frame.selection_source`。规范全文见 `docs/cover-generation-convention.md` | `cover_realframe_v2.py`、`cover_templates.py`、`cover_replace.splice_intro`、ffmpeg | 切片目录四产物 + `cover_revision`（`cover_frames/` 侧目录：候选帧缩略图，仅手动链写）。**自动封面开关 STUDIO_AUTO_COVER（2026-11-09 用户拍板，默认关闭）**：关闭时渲染链不生成封面四产物、不拼接 0.8s 片头；手动制作链（重制封面/版型库/封面微调）不受影响 |
| 5 | 渲染 rendering | clip_plan 的 `start_ms/end_ms` + 指定版本纠错转写 → **本地 FFmpeg 逐条渲染**：横转竖居中裁切（608×1080→1080×1920）、H.264+AAC、SRT 时间平移、**字幕同遍烧录**（2026-10-29 固化）；**烧录源=精修稿优先（2026-11-09 用户拍板）**：`source=llm` 切片按 segment_id 把烧录文本替换为 `refined_text`（语气词/无价值句已删，`clip_render._apply_refined_segments`），精修缺失/fallback 如实回退纠错转写，`warnings.json.subtitle_provenance.subtitle_source`（`clip_refinement`/`corrected_transcript`）+ `refined_segment_count` 留痕；渲染指纹含精修身份、版本 `purify-concat/1`（2026-11-11 净化重组升级：旧 `refined-burn/1` 缓存全量失效）；**多区间渲染（2026-11-11 问答化 P1）**：切片带合法 `sub_ranges` 时走 filter_complex 逐段 trim+concat（输入侧 `-ss 首段起 -t 末段止` 粗定位，分段/字幕平移到虚拟时间轴，`provenance.purify.ranges` 留痕），sub_ranges 缺失/非法回退单区间老路径；源片本地硬链接不重传；**字幕词级对齐 + 短 cue 纪律（2026-11-12 字幕优化，同日单位/行宽修复后定稿）**：提供词级时间（faster-whisper words，秒→内部 ms）时 cue 起止取真实词首尾——尾部静音不再挂字幕；词间停顿 >`SUBTITLE_CUE_GAP_SPLIT_S`（默认 1.5 秒）作为 cue 内切分边界；cue 间空隙 ≤`SUBTITLE_GAP_EXTEND_MS`（默认 0.8 秒）闭合防闪烁、超过则前条最多延伸 0.8 秒后留白（长停顿字幕消失，说完就撤）；单条显示超 `SUBTITLE_MAX_CUE_SECONDS`（**默认 12 秒**，此前文档误写 5 秒）按词时间/容量二次切分，且装箱合并同受时长上限约束（防切开后按文字容量重新并回超长挂屏 cue）；短条合并/折行全程守住每行 ≤16 字（修复 `_join_texts` 兜底盲拼与 `_arrange_lines` 行预算不足仍折行两处超宽行缺陷）。**2026-11-12 单位修复**：token 层毫秒与配置秒阈值直接比较曾致真实词时间下整句逐词碎裂（验收 case 回归），已统一换算；**words 已接入生产渲染（2026-11-12 人工确认 A/B 后接线）**：`_run_clip_render_stage` 与重新剪辑 `_run_reedit_replace` 两处渲染均传 v2 转写词级时间（秒），`_render_one_reserved` 将其与分段同映射平移到净化 sub_ranges 虚拟时间轴后交给 `build_srt`；渲染指纹版本 words-align/1（无词仍 purify-concat/1）——旧无词缓存自动失效重渲；`warnings.json.subtitle_provenance.words`（used/count/virtual_timeline）留痕；words 缺失/空回退段内线性插值（旧行为）。离线对比脚本 `backend/scripts/subtitle_ab_demo.py`（复刻生产链，words 平移引用同一生产实现） | `clip_render.py`、`subtitle_cueing.py`、ffmpeg（`CONTENT_RENDER_FFMPEG`） | `tasks/output/<job>/clips/<clip_id>/clip_final.mp4` + cover.jpg + ASS/SRT + warnings.json |
| 6 | 回传/登记 | 渲染同一遍产出的字幕/封面四产物登记 artifact；卡片/台账构建 | `store.register_clips`（generation 锁，幂等） | 项目 `tasks/projects/<id>/clips.json` |

**隔离与恢复**：单切片技术故障记 `technical_error` 继续其余；已存在且可解码的 `clip_final.mp4` 直接跳过（worker 重启恢复）；终态 `completed / completed_with_warnings / partial_succeeded / technical_failed`。**全程无质量拦截**（只有 warning）。

### 3.3 重新剪辑（re-edit 两阶段替换）

`POST /api/jobs/re-edit` → 复用源片+已纠错转写 → 独立 staging 渲染 → **≥1 条可解码才激活** → 按激活前清单清除旧切片（两阶段回收站，`reedit_replace.py`）→ 素材区只剩新切片。re-edit 链同样**先精修再渲染**（2026-11-05 起，`clip_refinement.json` 随新 run 落盘），渲染消费精修稿契约与 §3.2 第 5 步一致（2026-11-09 起烧录源=精修稿优先）。旧切片转写 seed 走纠错审计窗口裁剪兜底（见 §4.2 第 3 级）。

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
- **烧录文本来源（2026-11-09 固化）**：成片硬字幕=精修稿优先（§3.2 第 5 步）——`source=llm` 切片烧精修稿（无语气词），缺失/fallback 如实回退纠错转写；`warnings.json.subtitle_provenance.subtitle_source` 可审计。字幕编辑器人工修订（rev-NNN ASS）只对 sidecar 底片生效，burned 底片以画面为准；要换字幕文本需重新剪辑（渲染指纹 `refined-burn/1` 识别精修/转写变化）。
- **words 接线与存量成片重烧（2026-11-12）**：words 进入渲染后指纹升至 `words-align/1`，此后所有新渲染自动带词级对齐；存量成片换新字幕必须重编码（字幕烧录在画面里，无法只换文件）。run `c6cd115b` 的 5 条成片已执行**就地字幕重烧维护**（`backend/scripts/subtitle_reburn_inplace_once.py`，用户授权）：同 plan/sub_ranges/源片/编码参数原位重渲 → 抽帧对比确认长停顿留白生效、发布副本原子覆盖、`clips.json.published` 哈希刷新为真实新值；旧副本备份于 `tasks/output/<run>/.pre-subtitle-reburn-*`。**注意两条系统约束**：(1) `studio_artifacts` 行按 (job,kind,clip,object_key) 去重且不更新已有行——同路径重发布时行内 sha/size 保持首次登记值，文件为权威内容；(2) 规划 provenance 冻结含实现文件哈希与闸门/overlay 身份，**任何 skill 实现变更后 reedit/`_plan_cache` 缓存必然失效**（protected resume 路径会硬失败 E_CACHE_CONTEXT_CHANGED）——跨实现变更的「只重渲」只能走直连 render + 原位发布，不能走 reedit（会重新 LLM 规划）。
- `POST /studio/clips/{p}/{c}/subtitles`：cues→overrides、`clip_config.update_config`（真变化才 bump `render_revision`，`task_history` 记 `UPDATE_SUBTITLE`），生成 `subtitles/rev-00N/{clip}.ass|.srt`。
- `GET` 返回 cues+config+overrides+版本列表 + **`subtitles_burned_in`**（读成片 `warnings.json`：`subtitle_mode=burned`）——前端据此**关闭播放器实时叠加层防重影**（2026-11-08 修复）；叠加层只服务未烧录（Phase 2 导出）场景。时间线字幕轨不受影响。
- 字幕合成公式与后端 ASS 生成器一致（`subtitle_compose.py`、`caption_templates.py`、`emoji_captions.py`；默认 44px 金黄 #FFD700、y_pct 78.125）。

### 4.4 封面体系
- realframe_v2 固化封面（渲染时自动）；手动替换/模板应用/多版本历史/回收站/撤销激活：`cover_templates.py`、`cover_replace.py`、`bump_cover/set_cover_pointer/prune_cover_history`（`studio_project_store.py`，原子 rev+1）。
- **文案主题对齐与选帧升级（2026-11-09 P1）**：见 §3.2 第 4 步——封面文字来源可用 `cover_copy.json` 审计（`final.source`: llm/rules/topic_fallback/user_override + `validation_report`）；封面/文案不合格时经「重新制作封面」（revision+1）生效，重制走生产同款取数（主题块+精修稿）。
- **封面微调（2026-11-09 P3）**：Inspector「效果」页新增候选帧条（`GET .../cover/frame-candidates`，成片时间线、清单缺失时从成片懒建并避开 0.8s 片头区）+ 候选文案点选（`GET .../cover/copy-candidates`，读 `cover_copy.json` 候选+评分）；点选 → `POST .../cover` 带 `frame_time_rel_s` / `copy_override`（revision+1、重烧片头、可回滚）。前端 `cover-tune-panel.tsx`；缩略图文件 `GET .../cover/frame-candidates/{frame-NN.jpg}`（白名单文件名）。

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
| 医学指南库（第三库） | `medical_kb.py`（快照检索）、`clip_medical_gate.py`（闸门）、`api/routes/medical_kb.py`（ingest/search/review，独立 token）、`medical_kb_seed/`（种子与 active_snapshot）、`migrations/007_medical_guidelines.sql`；改种子后跑 `scripts/materialize_medical_kb.py`（`--db` 同时入库） | 切片医学主题匹配、前提/风险边界标注、非医学切片硬剔除（2026-11-10） |
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
- `docs/medical-gate-design.md` — 医学指南库（第三库）与切片医学闸门设计（2026-11-10）
- `docs/qa-style-restructure-design.md` — 问答化重组：对话切片剪出口播感（净化重组/多点拼接/多区间渲染，2026-11-11）
- `docs/two-step-planning-design.md` — 两步制切片规划（提示词A/B）设计
- `docs/subtitle-burn-convention.md` — 烧录字幕契约
- `docs/copilot.md` — AI 助手
- `docs/clear-project-data.md` — 项目数据清理
- `CLAUDE.md` / `AGENTS.md` — 仓库级开发约定（注意：其「video-brain 是唯一管线」的描述已被 staged 本地链取代，以本文与代码为准）
- `DSH_STARTUP.md` — 本容器端口/工具/截图目录等运行事实

---

## 7. 本文档的维护方式（自动更新）

- **事实快照自动生成**：文末「附录 · 代码事实快照」由 `docs/generate_workbench_doc.py` 扫描代码生成（端点、worker 阶段、服务、Inspector 标签、git 版本），手写内容永不改动。
- **推送即刷新**：本仓库启用了 `core.hooksPath=.githooks`，`git push` 前 pre-push 钩子自动重新生成快照，有变化就先提交再推——远端文档永远与代码一致。
- **漂移检测**：`backend/tests/unit/test_doc_workbench_snapshot.py` 在跑后端测试时校验快照是否过期，过期即红，提示执行 `python3 docs/generate_workbench_doc.py`。
- **CI 服务端校验（2026-11-08 起）**：`.github/workflows/doc-snapshot-check.yml` 在每次 push(main)/PR 时跑 `--check`，过期即红；GitHub Actions 页手动触发可勾选 autofix 自动修正并提交回分支。配合分支保护规则可作合并硬门禁。
- **跨窗口维护约定（2026-11-08 起）**：本快照记录「代码版本」；新窗口/新会话执行 `git log <该版本>..HEAD --oneline` 即可列出未覆盖的改动，据此更新叙述章节（维护规则已写入 AGENTS.md / CLAUDE.md）。
- **手动更新**：改完代码跑一次 `python3 docs/generate_workbench_doc.py` 即可。
- 叙述性内容（流程讲解/原因）仍建议在改动较大时让 DSH 助手按 diff 重写对应章节。

<!-- BEGIN:AUTO-SNAPSHOT（本区块由 docs/generate_workbench_doc.py 生成，勿手改） -->

## 附录 · 代码事实快照（自动生成）

- 生成时间：2026-11-13 02:48 UTC · 代码版本：`41308a1` · 生成器：`docs/generate_workbench_doc.py`（`--check` 可做漂移检测）

### HTTP 端点（backend/src/api/routes）

- `admin.py`（2）：`GET /health`、`GET /runtime-settings`
- `api_keys.py`（3）：`GET /`、`POST /`、`DELETE /{key_id}`
- `billing.py`（1）：`POST /subscription-email`
- `feedback.py`（1）：`POST /feedback`
- `ingest_kb.py`（4）：`POST /ingest/hot-topics`、`POST /ingest/scripts`、`GET /kb/search`、`POST /kb/scripts/{script_id}/confirm`
- `jobs.py`（16）：`GET /uploads/capabilities`、`POST /uploads`、`POST /uploads/{session_id}/complete`、`POST /jobs/transcribe`、`POST /jobs/auto-edit`、`POST /jobs/re-edit`、`GET /jobs/{job_id}`、`POST /jobs/{job_id}/retry`、`POST /jobs/{job_id}/cancel`、`GET /jobs/{job_id}/artifacts/{artifact_id}/file`、`POST /internal/heartbeat`、`POST /internal/jobs/claim`、`POST /internal/jobs/{job_id}/state`、`POST /internal/jobs/{job_id}/artifacts`、`GET /internal/jobs/{job_id}/artifacts`、`POST /internal/jobs/{job_id}/activate`
- `media.py`（102）：`GET /fonts`、`GET /fonts/{font_name}`、`POST /fonts/upload`、`DELETE /fonts/{font_name}`、`GET /transitions`、`GET /caption-templates`、`GET /broll/status`、`POST /studio/transcribe`、`POST /studio/transcribe/start`、`GET /studio/transcribe/chunks/{upload_id}`、`POST /studio/transcribe/chunk`、`POST /studio/transcribe/assemble`、`GET /studio/transcribe/jobs/{job_id}`、`POST /studio/insights`、`POST /studio/auto-edit`、`GET /studio/auto-edit/{task_id}`、`POST /studio/auto-edit/{task_id}/rerender`、`GET /studio/auto-edit/{task_id}/artifact`、`POST /studio/auto-edit/{task_id}/clips/{clip_id}/cover`、`POST /studio/auto-edit/{task_id}/clips/{clip_id}/cover-template/apply`、`POST /studio/auto-edit/{task_id}/clips/{clip_id}/cover-template/delete`、`POST /studio/auto-edit/{task_id}/clips/{clip_id}/cover/adopt`、`POST /studio/auto-edit/{task_id}/clips/{clip_id}/cover/restore`、`GET /studio/auto-edit/{task_id}/clips/{clip_id}/cover`、`GET /studio/auto-edit/{task_id}/clips/{clip_id}/cover/frame-candidates`、`GET /studio/auto-edit/{task_id}/clips/{clip_id}/cover/frame-candidates/{filename}`、`GET /studio/auto-edit/{task_id}/clips/{clip_id}/cover/copy-candidates`、`GET /studio/auto-edit/{task_id}/covers`、`PUT /studio/auto-edit/{task_id}/clips/{clip_id}/transform`、`GET /studio/auto-edit/{task_id}/clips/{clip_id}/refined-transcript`、`GET /studio/auto-edit/{task_id}/clips/{clip_id}/refined-copy.md`、`GET /studio/auto-edit/{task_id}/clips/{clip_id}/export-transformed`、`GET /studio/timeline/{project_id}`、`PUT /studio/timeline/{project_id}`、`GET /studio/timeline/{project_id}/preview`、`POST /studio/timeline/{project_id}/preview`、`GET /studio/timeline/{project_id}/source`、`GET /studio/timeline/{project_id}/view`、`PUT /studio/timeline/{project_id}/view`、`GET /studio/timeline/{project_id}/history`、`POST /studio/timeline/{project_id}/undo`、`POST /studio/timeline/{project_id}/redo`、`POST /studio/timeline/{project_id}/revision`、`POST /studio/timeline/{project_id}/tracks`、`DELETE /studio/timeline/{project_id}/tracks/{track_id}`、`GET /studio/timeline/{project_id}/assets/peaks`、`POST /studio/timeline/{project_id}/assets/audio`、`POST /studio/copilot`、`GET /studio/copilot/history`、`POST /studio/copilot/action-result`、`GET /studio/projects/{project_id}/clips`、`POST /studio/projects/{project_id}/sync`、`GET /studio/projects/{project_id}/data`、`POST /studio/projects/{project_id}/data/clear`、`POST /studio/projects/{project_id}/clips/{clip_id}/tasks`、`GET /studio/tasks/{task_id}`、`GET /studio/projects/{project_id}/tasks`、`GET /studio/projects/{project_id}/clips/{clip_id}/cover-history`、`POST /studio/projects/{project_id}/clips/{clip_id}/cover/undo`、`POST /studio/projects/{project_id}/clips/{clip_id}/cover/activate`、`DELETE /studio/projects/{project_id}/clips/{clip_id}/cover/revisions/{revision}`、`GET /studio/workflow`、`GET /studio/workflow/{task_id}/snapshot`、`POST /studio/workflow/skills/{skill_id}/toggle`、`POST /studio/workflow/regenerate/{project_id}/{clip_id}`、`GET /studio/clips/{project_id}/{clip_id}/config`、`PUT /studio/clips/{project_id}/{clip_id}/config`、`GET /studio/clips/{project_id}/{clip_id}/transcript/revisions`、`POST /studio/clips/{project_id}/{clip_id}/transcript/revisions`、`POST /studio/clips/{project_id}/{clip_id}/transcript/rollback`、`GET /studio/clips/{project_id}/{clip_id}/subtitles`、`POST /studio/clips/{project_id}/{clip_id}/subtitles`、`POST /studio/clips/{project_id}/{clip_id}/audio/bgm`、`DELETE /studio/clips/{project_id}/{clip_id}/audio/bgm/{asset_id}`、`GET /studio/clips/{project_id}/{clip_id}/render-config`、`POST /studio/projects/{project_id}/reedit/{run_id}/retry-cleanup`、`GET /studio/projects/{project_id}/transcript-ref`、`PUT /studio/projects/{project_id}/clips/{clip_id}/timeline-image`、`DELETE /studio/projects/{project_id}/clips/{clip_id}/timeline-image`、`GET /studio/tasks/{task_id}/project`、`GET /studio/projects`、`GET /studio/projects/list`、`GET /studio/projects/{project_id}/thumbnail.jpg`、`GET /studio/projects/{project_id}/clips/{clip_id}/first-frame.jpg`、`POST /studio/projects/{project_id}/archive`、`POST /studio/projects/{project_id}/restore`、`GET /studio/projects/{project_id}/delete/preview`、`POST /studio/projects/{project_id}/delete`、`GET /studio/projects/{project_id}/name`、`PUT /studio/projects/{project_id}/active-clip`、`GET /studio/cover-templates`、`GET /studio/cover-templates/{template_id}/example.svg`、`PUT /studio/projects/{project_id}/clips/{clip_id}/cover-template`、`POST /studio/cover-templates/{template_id}/mock-result`、`GET /studio/projects/{project_id}/cover-assets`、`GET /studio/projects/{project_id}/delete/status`、`POST /studio/projects/{project_id}/delete/retry`、`GET /studio/projects/{project_id}/exports`、`POST /studio/projects/{project_id}/clips/{clip_id}/deliver`、`POST /studio/projects/{project_id}/clips/deliver-batch`、`GET /studio/projects/{project_id}/glossary`、`PUT /studio/projects/{project_id}/glossary`
- `medical_kb.py`（3）：`POST /ingest`、`GET /search`、`POST /entries/{entry_id}/review`

### worker 任务阶段（studio_job_worker.py）

- `run_transcribe_only`——TRANSCRIBE_ONLY：提取音轨 → 转写 → 纠错 → transcript_ready（终态）
- `_run_id`——
- `_run_content_analysis_stage`——第三步（2026-10-14）：AUTO_EDIT 的模型内容分析阶段
- `_run_clip_planning_stage`——第四步（2026-10-14）：完整内容切片规划——产出 clip_plan.json
- `_run_clip_refinement_stage`——第四步半（2026-11-05 两步制）：切片精修——clip_refinement.json
- `_run_clip_cover_stage`——每条切片发布前的固化封面步骤（realframe_v2：真实帧+文字叠加，纯本地）
- `_run_clip_render_stage`——第五步（2026-10-14）：clip_plan → 本地 FFmpeg 渲染，生成多个视频成品
- `_run_reedit_replace`——重新剪辑并替换旧切片（2026-10-15，replacement_mode=delete_old_after_succ
- `run_auto_edit_only`——AUTO_EDIT：读取既有纠错转写 → 分析 → 规划 → 渲染（第五步正式链）
- `run_legacy_full`——legacy 全链（config 无 job_kind 的存量/重试任务）：行为与拆分前完全一致
- `run_isolated_job`——

### supervisor 托管服务

`postgres`、`redis`、`frontend`、`backend`、`autoedit`、`worker`

### Inspector 标签

转写(transcript)、封面(cover)、效果(effects)

<!-- END:AUTO-SNAPSHOT -->
