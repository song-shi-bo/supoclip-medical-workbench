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
| 管理面 | admin/feedback/计费/API Keys/知识库入库；标题库双标签检索（`video_titles`：标签1=钩子类型，标签2=病种，`GET /api/v1/titles/search`，2026-11-16） | `admin.py`、`billing.py`、`api_keys.py`、`feedback.py`、`ingest_kb.py`、`title_library.py` |

### 2.1 选题卡与原创文稿链路（阶段3 M1/M2/M3，2026-11-20）

「热点 → 选题卡 → 医生原创稿 → 拍摄 → 剪辑」的最上游生产层，落库于既有双库表（不建新选题表）：

- **数据模型（迁移 `20261119_0010_topics_brief.sql`）**：`hot_topics` 升格为选题卡载体（`brief` JSONB + `core_conclusion`/`risk_boundary`/`prerequisites`/`forbidden_expr` 独立列 + `approved_by/at`）；`scripts.origin` 身份列（`transcript_reference`=豆包转写参考稿 / `shooting_script`=医生原创稿，**写入通道决定身份**：`ingest_kb.py` 恒为参考稿，工作台生稿恒为原创稿）；`topic_reviewers` 医生审批白名单表。状态机：`new → briefed → doctor_approved → scheduled → used → archived`（沿用原 status 列）。
- **转写稿自动关联（2026-11-20）**：`ingest_scripts` 在 `topic_ref` 为空时兜底——剥掉【原始转写】前缀后按归一化标题哈希精确匹配 `hot_topics.content_hash` 自动挂链，未命中则带 warning 入库不关联；存量 107 条孤儿转写稿已按同一算法回填 101 条（余 6 条热点不在库内，待人工处理）。
- **路由（`api/routes/topics.py`，Studio 登录态）**：`GET /api/v1/topics`（列表/筛选）、`GET /scripts`（文稿列表，供上传「关联文稿」选择器；**2026-11-25 修复**：JOIN hot_topics 后 WHERE 的 status 不带表前缀 → AmbiguousColumnError 500，曾被前端静默吞成空列表；改为 `s.status IN :statuses` expanding IN，见 `test_list_scripts_returns_approved_shooting_only` 回归测试）、`GET /{topic_id}`（详情+关联文稿）、`POST /{topic_id}/brief`（保存；**审批后改卡自动清空审批、回 briefed，医学门必须重过**）、`POST /{topic_id}/brief/draft`（转写稿→选题卡草稿，只返回不落库）、`POST /{topic_id}/approve`（选题卡医学审批，仅白名单）、`POST /{topic_id}/generate-script`（生稿：硬校验 hard 不过自动带违规反馈重试一次，仍不过 422 不落库）、`POST /scripts/{id}/approve`（文稿审批 draft→approved，参考稿 409 无审批语义）、`POST /scripts/{id}/link-job`（拍摄上传挂链：`scripts.task_id` 回填 + `studio_jobs.config` 挂 `script_id`/`topic_id` + 选题→used）、`GET/POST/DELETE /api/v1/topics/reviewers`（白名单管理，仅 `users.is_admin`）。
- **前端（2026-11-20，M4/M5）**：`/topics` 选题板页（看板列=状态机、详情抽屉含 brief 表单与拆解/保存/两级审批/生稿操作、管理员白名单管理弹窗；`app/topics/page.tsx` + `lib/topics-api.ts`，顶栏新增「选题板」入口）；上传流程嵌入「关联文稿」选择器；2026-11-25 **改版为两选项模式开关**（常驻「项目素材」面板、「上传新视频」按钮下方，仅上传前可见）：**不关联文稿=门诊切片工作流 / 关联文稿=对稿成片工作流**（转写任务不再选具体文稿、不再提交时 link-job——`config.script_mode=true` 随自动剪辑提交，具体稿件由剪辑链对文稿库句级对齐自动锁定并补挂链，见 §1.5 对稿成片匹配）。
- **生成管线（`topic_pipeline.py`）**：拆解与生稿两个 Pydantic AI Agent（复用 `ai.py` 模型装配，未配置 LLM 显式 503 不静默降级）；拆解**不自动生成医疗红线字段**（风险边界人工填）；生稿提示词明确"参考稿只学立意/结构，禁止复用原句"；硬校验器两级：hard（禁止表达命中/refs 缺失/红线模式）与 soft（字数越界/前提覆盖启发式），字数按 3.5 字/秒折算。 医生审稿反馈沉淀（2026-11-20）：绝对化强度超纲（完全阻断/稳住类）=hard、承诺化句式（就能/也能）=soft 软化提示均已入校验器并配回归测试；refs 对照核准库（medical_guidelines.source_name 白名单进提示词+事后核对），未核准仅警告。
- **测试**：`tests/unit/test_topic_pipeline.py`（校验器/prompt）、`tests/unit/test_topics_api.py`（直接 await 端点协程闭环：白名单→brief→两级审批→生稿 503→文稿审批→挂链→改卡重审）。
- **修改自动沉淀（2026-11-23，迁移 `20261120_0012`）**：`modifications` 表（9类归因+note+payload+路由去向）与 `negative_patterns` 表（负例模式 DB 化，含 6 条医生反馈种子）。路由：`POST /api/v1/topics/modifications`（记录+按类路由：ASR错误/术语→追加 `medical.tsv` 热词行（`MEDICAL_LEXICON_DIR` 可覆盖）；前提缺失/结论不严谨→`medical_guidelines` 待复核条目（entry_type=doctor_feedback）；隐私/字幕/钩子→负例模式入库；切片/拼接→仅记录）、`GET /modifications`、`POST /modifications/{id}/resolve`；两级审批接口支持可选 `reason_class/feedback` 顺带沉淀；`generate-script` 校验时加载 `negative_patterns` 启用行（hard 拒绝重写 / soft 警告）。前端抽屉加「沉淀修改」表单（分类下拉+备注，随审批或单独记录）。校验器内置 4 条基线红线保持代码化，医生反馈沉淀模式全部走 DB。

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
| -1 | 对稿成片匹配（2026-11-25 P0，仅 `config.script_mode=true` 时执行） | 上传页「关联文稿」开关置位后自动进入：①加载文稿库候选（`origin=shooting_script` 且 `status=approved`，≤50 篇）→ ②纠错转写 vs 候选句级对齐（`script_align.match_script_library`，NFKC/去标点/去语气字归一化，单调贪心+1~3 分段窗口拼接，阈值 0.55）→ ③覆盖率 ≥0.5 判按稿拍摄并**自动挂链**（`scripts.task_id` 回填 + 状态 uploaded + 选题→used，替代人工 link-job）→ ④报告原子落盘。**任何失败只降级 warnings 绝不阻断主链**（库空/无分段/异常 → 按门诊切片流程继续）。**P0 仅匹配+报告+自动挂链；对稿精剪决策与单成片渲染（P1/P2）尚未实现——对稿模式下剪辑仍走既有分析→规划→渲染链** | `script_conform.py`（`run_script_match_stage`）、`script_align.py`；pipeline/runner 三处 AUTO_EDIT 入口接线 | `tasks/alignment/<job_id>.json`（version=script-conform/v1，含逐句 matched/missing、extra 段、覆盖率、candidate_summaries、script_linked） |
| 0 | retrieving_rules | 记 `rule_snapshot_id`（剪辑规则只读留痕，QC 永不拦截） | — | rule snapshot |
| 1 | 内容分析 `extracting_highlights` | **处理链：**①按 `object_key + transcript_id + transcript_version` 读取纠错转写 → ②根据全片均匀抽样文本，向 PostgreSQL `hot_topics`、`scripts`、`medical_guidelines` 分别做本地 BGE 向量+字词回退的 Top-K 检索，并把结果冻结到当前 generation → ③将三库参考块与纠错转写一起交给运行时 `Config.llm`，提取关键词、金句、主题块、患者问题、医生核心段、**钩子候选（hooks，2026-11-16 新增，逐字原话+机制分类）**和候选区间 → ④程序执行转写忠实校验：关键词/金句/钩子等必须逐字来自转写、不得从库中复制，非法结果清理留痕 → ⑤继续用确定性 `active_snapshot.json` 生成 strong/weak/none 医学闸门标签 → ⑥原子保存、上传并登记。<br>**数据库/存储：**直接读取三张业务表；使用 `tasks/knowledge_context/<job>/g<generation>.json` 冻结 Top-K；读版本化转写和本地医学闸门快照；写分析 JSON、R2/本地对象存储和 `studio_artifacts`。三库故障降级为空上下文，不阻断转写主链。<br>**模型证据：**实际生成模型看分析 JSON 的 `model`；`knowledge_context_summary` 记录三库条数、ID、模式和上下文指纹。 | `knowledge_context.retrieve` → `content_analysis.analyze_content` → Pydantic AI `_AnalysisOut` → `clip_medical_gate.analyze_topics` → `save_analysis_record` | `tasks/analysis/<job>.json`（analysis/v7；含 `knowledge_context/hooks/model/plan_source/llm_error/medical_analysis`） |
| 2 | 切片规划 `planning`（提示词 A） | **处理链：**①读取同版本权威分段、分析结果和同一份已冻结三库 Top-K → ②以 Prompt A 为 `system_prompt`，将热点、文稿、医学指南参考块、钩子候选以及全片分段、主题、金句、候选区间和医学标签组成用户提示 → ③运行时 `Config.llm` 一次全局规划，参考三库判断主题价值、Hook/金句潜力与句子取舍，但最终 Hook、引用和保留内容必须来自转写 → ④程序吸附切点、重算时间、去重评分 → ⑤执行医学闸门、Purify、**钩子前置护栏（钩子句必须落在切片前 8 秒内，只把钩子放到开头、不改切片起点，HOOK_FRONT_SECONDS 可调）**和全片覆盖 → ⑥保存、上传并登记计划。<br>**数据库/存储：**规划不再次查询数据库，直接复用分析工件中冻结的 `knowledge_context`，保证分析与规划看到完全相同的三库条目并可复现；计划指纹绑定上下文内容。<br>**模型证据：**分析与规划共用运行时 `Config.llm`；实际模型看 `clip_plan.json.model`，`knowledge_context_summary` 记录规划使用的三库身份，`hook_front_window_ms` 留痕前置窗口；失败重试一次后走 `_fallback_plan`。 | `clip_planning.plan_clips` → Prompt A/Pydantic AI `_SlicePlanOut` → `planning_profile_from_analysis/enforce_clips` → `clip_purify.purify_clips` → `save_plan_record` | `tasks/plans/<job>/clip_plan.json`（clip-plan/v5；含 `knowledge_context_summary/hook_front_window_ms/model/plan_source/medical_gate/purify/clips/discarded`） |
| 3 | 切片精修 refining（提示词B b2） | 逐切片精修字幕文本 + **逐段说话人（医生/患者/家属）**，失败粒度回退不阻断；**只删不改执法（2026-11-08）**：精修句必须是语气词清理后转写的顺序保持子串，违规段整段回退原纠错转写（`reverted_segments` 留痕；全违规→整片 fallback `delete_only_violation`）；精修窗口（2026-11-11 问答化 P1）：切片带 `sub_ranges` 时窗口取子区间并集，被净化的应答碎片不进提示词B | `clip_refinement.py`（clip-refine-prompt/2026-11-08-b2） + LLM | `tasks/plans/<job>/clip_refinement.json`（`clips[clip_id].segments`：`segment_id/start_ms/end_ms/refined_text/speaker`） |
| 4 | 封面 cover | realframe_v2：真实视频帧+文字叠加（纯本地，无 AI 生图）；**默认版型 yellow_bold_v5（2026-11-09 P2）**：两行同级黄字（theme 88/hook 110，#FFDD00 纯黑描边 6/8px）锚定胸腹区（top_ratio 0.56）+ 暗底面板 + 角标层（`cover_badge_text`），`build_cover(template=None)` 自动套用（此前 worker 路径无版型走白字默认排版）；均衡断行（优先空格分隔，杜绝单字孤行）；**选帧**：切片内每 2s 抽候选（首帧自 +1.0s 起，上限 40）→ 确定性评分＝人脸数（1 张最佳）+ 面积 + 清晰度 + **亮度项**（灰度 [90,200] 满分）+ 单脸居中项，运动模糊 ×0.6；**文案主题对齐（2026-11-09 P1）**：主题块锚点＝content_analysis `topic_units` 按切片区间重叠最大（并列优先标题与切片规划主题相关者），底料**精修稿优先**；LLM 一次出 3 组候选 → 主题校验（title 覆盖率/钩子原话命中）→ 不合格带反馈**受控重生成一次**（每切片封面文案 LLM ≤2 次）→ 仍不合格走 hint 规则组/主题词直组句兜底（有主题块时 hint 不进初选池）；hook 复述 topic（≥3 字全包含）评分 −2；**人工换帧（P3）**：`build_cover(frame_time_rel_s=)` 指定成片时间线秒，`selection_source=manual` 留痕；可读线校验（≥96px@1080，不过→纯色兜底）；**封面与片头解耦（阶段2 P3 2026-11-18）**：封面资产（四产物含 cover_intro.mp4）默认必产，`clip_final` 前 0.8s 静态片头**默认不再烧入**——由 `COVER_INTRO_MODE`（默认 off）单独控制，on 时旧契约拼接（时长不变、台账防重拼），成片第 0 帧即自然画面；封面生成失败 → **发布软标记**（用户拍板 2026-11-18：降级为软处理）：切片照常发布，条目带 `cover_missing` + warning 留痕，工作台可见后人工重制封面补齐再投递；`intro_burned` 按实际烧入状态入卡片绑定。审计：`cover_copy.json` 的 `topic_unit_used/validation_report/regeneration_count/prompt_version(2026-11-09.2)/transcript_source/frame.selection_source`。规范全文见 `docs/cover-generation-convention.md` | `cover_realframe_v2.py`、`cover_templates.py`、`cover_replace.splice_intro`、ffmpeg | 切片目录四产物 + `cover_revision`（`cover_frames/` 侧目录：候选帧缩略图，仅手动链写）。**自动封面开关 STUDIO_AUTO_COVER（阶段2 P3 语义收窄）**：控制**封面资产生产**（默认必产——阶段2 契约「每条审核通过成片必须有独立封面资产」，supervisor 不再强制 '0'；0=紧急停机，此时不做封面缺失标记）；**0.8s 片头烧入由 `COVER_INTRO_MODE`（默认 off）单独控制**，与本开关解耦；手动制作链（重制封面/版型库/封面微调）与时间线 image-clip（用户显式放置）不受片头开关影响 |
| 5 | 渲染 rendering | clip_plan 的 `start_ms/end_ms` + 指定版本纠错转写 → **本地 FFmpeg 逐条渲染**：横转竖居中裁切（608×1080→1080×1920）、H.264+AAC、SRT 时间平移、**字幕同遍烧录**（2026-10-29 固化）；**烧录源=精修稿优先（2026-11-09 用户拍板）**：`source=llm` 切片按 segment_id 把烧录文本替换为 `refined_text`（语气词/无价值句已删，`clip_render._apply_refined_segments`），精修缺失/fallback 如实回退纠错转写，`warnings.json.subtitle_provenance.subtitle_source`（`clip_refinement`/`corrected_transcript`）+ `refined_segment_count` 留痕；渲染指纹含精修身份、版本 `purify-concat/1`（2026-11-11 净化重组升级：旧 `refined-burn/1` 缓存全量失效）；**多区间渲染（2026-11-11 问答化 P1）**：切片带合法 `sub_ranges` 时走 filter_complex 逐段 trim+concat（输入侧 `-ss 首段起 -t 末段止` 粗定位，分段/字幕平移到虚拟时间轴，`provenance.purify.ranges` 留痕），sub_ranges 缺失/非法回退单区间老路径；源片本地硬链接不重传；**字幕词级对齐 + 短 cue 纪律（2026-11-12 字幕优化，同日单位/行宽修复后定稿）**：提供词级时间（faster-whisper words，秒→内部 ms）时 cue 起止取真实词首尾——尾部静音不再挂字幕；词间停顿 >`SUBTITLE_CUE_GAP_SPLIT_S`（默认 1.5 秒）作为 cue 内切分边界；cue 间空隙 ≤`SUBTITLE_GAP_EXTEND_MS`（默认 0.8 秒）闭合防闪烁、超过则前条最多延伸 0.8 秒后留白（长停顿字幕消失，说完就撤）；单条显示超 `SUBTITLE_MAX_CUE_SECONDS`（**默认 6 秒**，2026-11-18 阶段2 从 12 秒收紧，env >6 钳回 6；验收「>6s cue = 0」由切分/装箱/整平/合并四处护栏机制保证）按词时间/容量二次切分，且装箱合并同受时长上限约束（防切开后按文字容量重新并回超长挂屏 cue）；短条合并/折行全程守住每行 ≤16 字（修复 `_join_texts` 兜底盲拼与 `_arrange_lines` 行预算不足仍折行两处超宽行缺陷）。**2026-11-12 单位修复**：token 层毫秒与配置秒阈值直接比较曾致真实词时间下整句逐词碎裂（验收 case 回归），已统一换算；**阶段2 P1 字幕升级（2026-11-18，方案《剪辑工作台阶段2优化方案》）**：药名保护词（内置 starter 表 + `medical_lexicon` TSV `type=drug` 标准词）进原子 token 不可拆；品牌字体解析链 `SUBTITLE_BRAND_FONT`（→雅黑→文泉驿逐级回退，未配置保持文泉驿字节兼容，0 字节占位字体自动跳过）；新 `subtitle_qc.py` 在 warnings.json 写 `subtitle_qc` 验收块（cue 首尾 vs 词首尾偏差 p95≤200ms 目标、行宽/行数/时长违例、words_missing 占比——**只报告不拦截**）；渲染指纹版本 **`subtitle-v2/1`**（有词）/`subtitle-v2-base/1`（无词），旧 `words-align/1`/`purify-concat/1` 缓存全量失效。**阶段2 P2 可读性与克制强调（2026-11-18 同日）**：新 `subtitle_readability.py` 渲染前逐 cue 采样源片字幕带亮度/边缘密度（≤24 帧/切片，净化 sub_ranges 虚拟时间轴反解采样点），判复杂背景（亮度≥170 或边缘≥0.18）的 cue 烧录用第二样式 SubBox 半透明黑底板（BorderStyle=3/alpha 0x60），其余保持描边，分析失败/`SUBTITLE_READABILITY=off` 全描边绝不阻断；新 `subtitle_emphasis.py` 关键词克制强调（每 cue ≤1 处 ≤6 字、数字单位永不强调、`SUBTITLE_EMPHASIS=off|bold|color` 默认 bold，术语=词典标准词+指南库 keywords 只取关键词字段）；安全区档案静态校验（`SUBTITLE_PLATFORM_PROFILE`，默认 douyin）写 `subtitle_safezone`；`subtitle_qc.readability` 透出 pre_mitigation_readability 与底板 cue 数；指纹 bump **`subtitle-v3/1`**（payload 增 `sub_style`：强调形态+可读性开关，改 env 自动失效缓存）。**阶段2 P4 人脸重构图 MVP（2026-11-18）**：新 `reframe.py`，`REFRAME_MODE=auto` 时渲染前分析源片切片窗口（3fps@480w 单遍解码，MediaPipe 单脸主脸跟踪 + Pose 手腕手势保护；**默认 off 零开销零指纹变化**）——横宽源（宽于 9:16）x 向 SmoothDamp 平滑跟踪（复用 `video_utils.build_crop_trajectory`：死区 5%/中值滤波/缺口填充，近距手腕并入目标），竖高源静态纵向定位（上半身安全区：脸顶留 ~10% 头顶），9:16 源直通；降级链：第二脸面积 ≥60% 持续 >2s → 多人保守禁用动态跟踪定帧主脸开头；检出率 <20% → 静态定帧/居中；依赖缺失/抽帧失败 → 静态居中——**绝不阻断渲染**；pan_y 脸部落入成片字幕带记避让提示；审计落盘切片目录 `reframe.json` + `warnings.json.reframe` 摘要；auto 模式指纹加 `reframe` 键（切开关自动失效重渲）。**words 已接入生产渲染（2026-11-12 人工确认 A/B 后接线）**：`_run_clip_render_stage` 与重新剪辑 `_run_reedit_replace` 两处渲染均传 v2 转写词级时间（秒），`_render_one_reserved` 将其与分段同映射平移到净化 sub_ranges 虚拟时间轴后交给 `build_srt`；渲染指纹版本 words-align/1（无词仍 purify-concat/1）——旧无词缓存自动失效重渲；`warnings.json.subtitle_provenance.words`（used/count/virtual_timeline）留痕；words 缺失/空回退段内线性插值（旧行为）。离线对比脚本 `backend/scripts/subtitle_ab_demo.py`（复刻生产链，words 平移引用同一生产实现） | `clip_render.py`、`subtitle_cueing.py`、ffmpeg（`CONTENT_RENDER_FFMPEG`） | `tasks/output/<job>/clips/<clip_id>/clip_final.mp4` + cover.jpg + ASS/SRT + warnings.json |
| 6 | 回传/登记 | 渲染同一遍产出的字幕/封面四产物登记 artifact；卡片/台账构建 | `store.register_clips`（generation 锁，幂等） | 项目 `tasks/projects/<id>/clips.json` |


#### 3.2.1 四层边界

| 层级 | 回答的问题 | 本层负责 | 本层不负责 |
|---|---|---|---|
| 知识与策略配置 | 系统用什么标准理解内容？ | 医学知识卡、词表、禁词、阈值、规则版本、快照身份 | 判断某条视频具体剪哪一段 |
| 内容分析 | 原视频里有什么？ | 主题簇、原话候选、患者问题、医生核心段和医学相关性标签 | 淘汰候选、决定发布内容 |
| 切片规划 | 这次要剪什么、怎么组合？ | 选择主题和区间、选择 Hook、**四点拼接（钩子→接题→叙述→收口锚点重排）**、医学过滤、生成计划 | 清理语气词、净化音视频、渲染 |
| 切片精修/后处理 | 已经决定剪的内容怎样呈现？ | Purify、Prompt B、`sub_ranges`、字幕与渲染 | 重新理解整条视频或重新选择主题 |

数据流：

```text
纠错转写 → 三库语义检索 → generation 冻结上下文
                         ↓（同一份 Top-K）
知识与策略配置 ───────→ 内容分析（事实与标签）
                         ↓
                    切片规划（本次取舍决定）
    ↓
切片精修与渲染（执行呈现）
```

##### 3.2.1.1 三个业务知识库分别用在哪里

主链不会把整库送入模型。它先从纠错转写均匀抽取最多 80 段、6000 字作为查询，分别检索三库 Top-K，并把有限结果冻结为当前 job/generation 的 `knowledge-context/v1`。分析与规划始终复用同一份冻结上下文。

| 库 | 合格数据与默认 Top-K | 内容分析用途 | 切片规划用途 |
|---|---|---|---|
| 热点库 `hot_topics` | `status <> disabled`，Top-5 | 辅助识别当前话题热度、受众关注点和主题价值 | 辅助主题排序与切片取舍；不得把热点标题/摘要当视频原话 |
| 文稿库 `scripts` | `status <> disabled`，Top-5（含 draft） | **只参考**既有钩子形态、内容拼接结构和潜在表达机制 | 辅助 Hook 潜力和语句取舍；不得复制历史 Hook、正文、CTA 或具体表达 |
| 医学指南库 `medical_guidelines` | `status=approved`，Top-8 | 辅助理解医学主题、边界和关键词 | 辅助判断信息价值与风险；不得引入转写外医学结论 |
| 医学闸门快照 `active_snapshot.json` | 独立的确定性 strong/weak/none 标签源 | 对主题与分段贴医学标签 | 规划消费已保存标签；不替代三库 Top-K |

检索使用本地 `BAAI/bge-small-zh-v1.5` 与字词分数的确定性混合排序；嵌入不可用或异常时退为词法。每库失败只清空该库并留 `errors`。Top-K、查询哈希、完整转写哈希、条目 ID、内容哈希和检索模式写入冻结快照；同一 generation 恢复不得查询当前数据库，显式 replan 才刷新。

**标题库 `video_titles` 与多维度标签体系**（迁移 `20261116_0007/0008`）：热点标题与剪辑成片标题统一入库，带双标签——标签1=钩子类型（热点型/流量型/信任型/品牌型/收藏型/问答型），标签2=病种主题（`backend/seed/title_library_topic_tags.json` 人工标注；新批次回退用热点自带标签，批次标记如「门诊素材」不计入）。同步用 `scripts/seed_title_library.py`（按 hot_topics.id 幂等 upsert，2026-11-18 起同步全部非 disabled 热点）；成片标题用 `scripts/import_generated_titles.py` 手动批量导入（`source='generated'`，六类自动判类 + 病种自动匹配，人工纠错写 `seed/title_library_generated.json`）。

**通用多维度标签 `content_tags`（迁移 `20261118_0009`，2026-11-18 落地）**：热点/文稿/标题三库统一挂维度标签，词表统一由 `backend/seed/tag_dimensions.json` 维护（全库唯一来源）。已落地维度：
- **intent 内容意图（患者决策链）**：症状识别 / 就医决策 / 方案解释 / 误区辟谣 / 就医指南 / 临床研究 / 预防生活方式——规则自动打标（`scripts/tag_content_dimensions.py`），三库 294 条已覆盖；
- **audience 受众人群**（同日落地）：新确诊患者 / 治疗中患者 / 随访复查患者 / 家属 / 高危人群 / 中老年 / 女性 / 儿童家长——关键词规则打标，单条最多 3 个；
- **format 内容形式**（同日落地）：问诊实录 / 医生口播 / 图文科普 等——成片素材固定问诊实录，对标标题词法判类；
- 待落维度：mechanism（钩子机制四类，分析已产出）、D6 合规维度（用户明确暂不做）。

人工标签 `tagged_by='manual'` 永不被规则重跑覆盖；标题库 `topic_tags` 里的意图类标签已迁移到 intent 维度（维度不混装）。检索：`GET /api/v1/titles/search` 支持 `hook_tag/topic_tag/intent/audience/source/q` 组合过滤（如 `intent=方案解释&audience=新确诊患者`）；`GET /api/v1/tags/browse?dimension=&tag=&object_type=` 跨热点/文稿/标题三库按维度+标签列出命中对象。两接口均走 KB_READ_TOKEN。标题库当前只做创作参考检索，不参与自动剪辑检索。

#### 3.2.2 内容分析：对视频做了什么

内容分析实际分析的是**指定版本的纠错转写**，不是原始视频画面或音频。进入本阶段前，视频已经完成 ASR 和医学纠错；本阶段不重新上传、不重新转写、不逐帧看画面，也不联网检索资料。

**逐步流程**

| 步骤 | 实际处理 | 程序与结果 |
|---|---|---|
| 1. 固定输入版本 | 按 `object_key + transcript_id + transcript_version` 取得纠错后的权威分段 | 输入来自 `tasks/transcripts/<source_id>/vN.json` |
| 2. 检索并冻结三库 | 对全片均匀抽样，三库分别做全体合格条目的 Top-K；结果写 `tasks/knowledge_context/<job>/g<generation>.json` | 快照绑定查询哈希、完整转写哈希、generation 和内容 identity；已有快照损坏时拒绝静默刷新 |
| 3. 冻结分析身份 | provenance 同时绑定转写、模型/提示词、规则、医学闸门快照以及三库上下文 identity 和渲染实现 | 同一输入才允许复用缓存；分析内嵌上下文与冻结快照不一致时重新分析 |
| 4. 全片结构化分析 | 把冻结三库参考块和纠错转写交给模型；超过 600 段才按每批 400 段拆批并合并 | 输出 `keywords`、`golden_quotes`、`topic_units`、`hooks`（钩子候选：逐字原话+机制）、`candidate_intervals` |
| 5. 转写忠实与防复制校验 | 金句/主题引用逐字回查；关键词必须是转写原词；三库独有的长文本若复制到主题标题、理由或候选说明则替换/清空；时间和 ID 回到权威分段重算 | 清理动作写入 `notes`，库内容不能成为视频原话或医学结论 |
| 6. 医学主题匹配 | 继续使用确定性闸门快照生成 strong/weak/none 标签，不淘汰内容 | 写入 `medical_analysis` / `medical_annotations` |
| 7. 原子保存与登记 | 保存完整冻结上下文、摘要和分析记录，上传并登记 artifact | `tasks/analysis/<job>.json`（analysis/v7） |

**分析阶段使用的模型**

| 用途 | 模型与调用方式 | 失败处理 |
|---|---|---|
| 主题/金句/候选区间抽取 | Pydantic AI 结构化调用，实际生成模型读取运行时 `Config.llm`；不是在分析代码中写死一个专用模型。温度 `0.2`，默认请求关闭思考，输出预算先 4096、token 上限不足时升到 6144 | 模型未配置、超时、配额不足或结构化输出非法时，改走本地 heuristic；`plan_source=heuristic_fallback` |
| 医学语义匹配 | 本地嵌入模型 `BAAI/bge-small-zh-v1.5`（fastembed）叠加关键词匹配；它不是生成式规划模型 | 向量模型不可用时退化为词法匹配；快照不可用时医学标注禁用，但分析任务继续 |

某次任务究竟用了哪个生成模型，以该任务 `tasks/analysis/<job>.json` 中的 `model` 为准；同时查看 `plan_source`、`llm_calls`、`llm_error` 和 `llm_usage` 可判断是否真正走了模型。分析与规划默认读取同一份运行时 `LLM` 配置，不是两个固定型号。

**内容分析连接了哪些数据库或存储**

| 数据库/存储 | 本阶段如何使用 |
|---|---|
| PostgreSQL `studio_jobs` / `studio_artifacts` | 保存任务租约、阶段和分析 artifact 元数据 |
| PostgreSQL 三库 | 内部租约保护端点查询非 disabled 热点/文稿（文稿只作结构参考）和 approved 医学指南；只返回有限业务字段，不返回向量或连接信息 |
| 三库冻结文件 | `tasks/knowledge_context/<job>/g<generation>.json` 是分析与规划共同事实源；恢复优先读它，不再次查询数据库 |
| 版本化转写文件库 | 读取纠错转写；其完整内容哈希写入三库快照身份，避免抽样查询相同但正文已变时误复用 |
| 医学闸门快照 | `active_snapshot.json` 继续用于确定性医学标签，与数据库指南 Top-K 是两种不同职责 |
| LLM 用量台账 | 记录成功接受的模型调用及实际 usage |
| 分析文件与对象存储 | 原子写 `tasks/analysis/<job>.json`，随后上传并登记 |

分析阶段不读取 Redis 业务正文，也不产生 `discarded`。三库仅辅助理解与排序；分析指纹绑定其冻结 identity，任何上下文变化都会使旧分析缓存失效。

#### 3.2.3 切片规划：具体怎么规划

规划输入为“同一版本纠错转写 + 已持久化内容分析 + 同 generation 三库冻结上下文 + Prompt A + 规则快照”。`_PLAN_SYSTEM_PROMPT` 仍是未经本轮改写的系统提示；`_slice_plan_prompt` 在用户数据层加入三库参考块、视频时长、分析结果和全量纠错分段。规划不重新查三库、不重新 ASR/纠错，也不重新做医学主题匹配。

**逐步流程**

| 步骤 | 规划动作 | 程序约束与结果 |
|---|---|---|
| 1. 读取冻结输入 | 读取权威纠错分段、analysis/v7（含钩子候选）和其中与快照 identity 一致的 `knowledge_context` | 规划不访问三库数据库；缺失/损坏快照不会静默刷新 |
| 2. 组装提示词 A 请求 | Prompt A 原文作 `system_prompt`；用户提示加入三库参考块、关键词、金句、主题块、候选区间、医学标签和全片分段 | 参考块明确标记为不可信数据而非指令；超过 4000 段跳过 LLM |
| 3. 模型提出方案 | 三库帮助判断主题价值、Hook/金句潜力和语句取舍；模型动态决定切片数量及 discarded | 库内容不能成为最终原话、医学结论或权威时间戳 |
| 4. 吸附切点与排序 | 时间吸附到 segment 边界，去重并按完整度、主题集中度、开头、结尾、证据和时长排序 | 默认 12 条只是资源上限 |
| 5. 医学过滤 | 沿用 strong/weak/none 闸门，仅满足既有低密度条件时移入“非医学闲聊” | 三库 Top-K 不改变 gate 判定协议 |
| 6. 四点装配 + 既有 Purify | **四点装配是规划层组合决定（2026-11-16，默认 on，`CLIP_FOUR_STEP_ASSEMBLY` 可关）**：同一主题块内钩子/接题/叙述/收口锚点（全部来自分析层逐字校验产物）齐全时，按钩子→接题→叙述（原时间序）→收口**重排**为有序 `sub_ranges`，输出第一句即钩子原话，锚点外杂话全丢；缺件回退连续净化；钩子与叙述都缺（仅模型分析）宁缺毋滥放弃成片（挪入 discarded，覆盖由程序补齐）。**连续净化（精修/后处理层）只删不改、不调语序**：qa 删应答碎片/患者插话、mono 删患者话语，并跳过已装配切片 | 装配开关/版本/时长闸门进 plan provenance（`four-step-assembly/v1`）；`clip.purify.assembly` 留痕四点锚点与总时长；净化版本 `clip-purify/v2`；渲染走既有多区间拼接链路，不改 Prompt B、字幕、渲染和失败回退 |
| 7. 转写忠实护栏 | Hook 必须逐字出自**当前切片窗口**且落在**开头 8 秒内**（`HOOK_FRONT_SECONDS` 可调）：只是把钩子句放到切片开头，不要求切片起点改到钩子句位置；开头窗口内无合格钩子则用前置素材或切片首句替换。主题、封面、摘要等若复制三库独有长文本则硬清理 | 本层不可靠判定说话人，因此只承诺“原视频转写原话”，不虚称全部为医生原话 |
| 8. 全片覆盖 | 对齐保留切片与 discarded，补入未归属转写段 | 覆盖的是转写段，不是无语音墙钟空白 |
| 9. 保存与登记 | 原子写 clip-plan/v5，保存上下文摘要、前置窗口、workflow snapshot 并登记 artifact | `tasks/plans/<job>/clip_plan.json` |

**提示词 A 和哪个模型在规划**

调用关系是：`plan_clips` → `_slice_plan_prompt` 组装用户数据 → `_run_plan_agent` → `Agent(system_prompt=_PLAN_SYSTEM_PROMPT, output_type=_SlicePlanOut)`。因此 Prompt A 不是文档里写了但运行时没用，而是每次 LLM 规划调用的系统提示；Prompt B 只在之后的 `clip_refinement.py` 使用，两者不混用。

规划没有另写死一个模型，它与内容分析共用运行时 `Config.llm`，通过 Pydantic AI 按 `_SlicePlanOut` schema 调用。模型设置为温度 `0.2`、默认关闭思考、初始输出预算 4096、token 上限不足时升到 6144；全局请求失败会再重试一次，仍失败则使用本地 `_fallback_plan`，不会悄悄切换到另一个模型或供应通道。某次任务的实际模型与执行结果必须看 `tasks/plans/<job>/clip_plan.json`：

- `model`：本次解析到的生成模型；
- `plan_source=llm`：模型规划成功；
- `plan_source=heuristic_fallback`：本地规则规划；
- `llm_error/actual_calls/llm_usage`：失败原因、调用参数和用量。

**切片规划连接了哪些数据库或存储**

| 数据库/存储 | 本阶段如何使用 |
|---|---|
| PostgreSQL `studio_jobs` / `studio_artifacts` | 保存 planning 状态、租约和计划 artifact；规划算法不执行三库 SQL |
| 三库冻结文件 | 从 analysis 内嵌上下文及 `tasks/knowledge_context/<job>/gN.json` 校验 identity 后复用；这是三库事实输入 |
| 版本化转写与说话人 overlay | 提供权威分段、Hook 窗口校验和 Purify 输入 |
| 内容分析文件 | 提供关键词、金句、主题、候选区间、医学标签和同一冻结上下文 |
| 医学闸门快照 | 标签已在分析阶段持久化；可选旧背景注入开关仍独立存在 |
| LLM 用量台账 | 记录成功规划调用；`actual_calls/llm_error` 留失败与参数证据 |
| 计划文件与对象存储 | 写 `tasks/plans/<job>/clip_plan.json`，上传并登记 artifact |

> 详细契约：`docs/content-pipeline-layering-optimization.md`、`docs/medical-gate-design.md`、`docs/two-step-planning-design.md`。D 层 Prompt B、字幕和渲染的行为未在本轮文档整理中改变。

**隔离与恢复**：单切片技术故障记 `technical_error` 继续其余；已存在且可解码的 `clip_final.mp4` 直接跳过（worker 重启恢复）；终态 `completed / completed_with_warnings / partial_succeeded / technical_failed`。**全程无质量拦截**（只有 warning）。

### 3.3 重新剪辑（re-edit 两阶段替换）

`POST /api/jobs/re-edit` → 复用源片+已纠错转写 → 独立 staging 渲染 → **≥1 条可解码才激活** → 按激活前清单清除旧切片（两阶段回收站，`reedit_replace.py`）→ 素材区只剩新切片。re-edit 链同样**先精修再渲染**（2026-11-05 起，`clip_refinement.json` 随新 run 落盘），渲染消费精修稿契约与 §3.2 第 5 步一致（2026-11-09 起烧录源=精修稿优先）。旧切片转写 seed 走纠错审计窗口裁剪兜底（见 §4.2 第 3 级）。

### 3.4 legacy video-brain 桥（仅存量任务）

`autoedit_runner_video_brain.py` 消费旧文件队列 `tasks/in/autoedit_*.json` → video-brain :8940 固定流程（预检→转写→纠错→规则→规划→渲染→QC）→ 回写 `tasks/status/<task>.json`。新任务不再走这条链。

---

## 4. 成片之后：后期编辑与产出链路

### 4.1 项目台账与时间线工程
- 台账 `tasks/projects/<project_id>/clips.json`：卡片/封面/修订历史/`generation` 乐观锁；`POST /studio/projects/{id}/sync` 从任务产物同步。
- **统一事实源（阶段5·点1，2026-11-22）**：PG `project_manifests` 表是台账的**权威镜像**（迁移 `20261122_0001_unified_sources.sql`）。`save_manifest` 磁盘原子写成功后由常驻后台 loop 写穿镜像（best-effort 可降级：PG 不可用只记 debug，绝不阻断本地链路）；`load_manifest` 磁盘优先（健康路径零额外查询），磁盘缺失/损坏时从镜像**自动修复回盘**并记 warning。clips.json 由唯一事实源降级为本地可重建视图；读写签名与 `generation` 并发契约不变。镜像走专用 loop + 专用小 engine（与主 API pool 隔离，避免 asyncpg 连接跨事件循环复用）。
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
- **阶段2 P1 验收指标（2026-11-18）**：每次渲染 warnings.json 新增 `subtitle_qc` 块——sync p95（cue 首尾 vs 词首尾，目标 ≤200ms，无词记 `words_missing` 占比不计失败）、行宽/行数违例（≤16 字/行、≤2 行/条）、超时长 cue 清单（>6s 应为 0）。**只报告不拦截**（沿用字幕问题不阻断渲染契约）；P1 验收=对真实项目重渲后读该块断言全部达标。
- **阶段2 P2 可读性/强调/安全区（2026-11-18）**：warnings.json 另有 `subtitle_readability`（mode=analyzed/off/fallback、逐 cue 采样与底板判定、`pre_mitigation_readability` 如实透出）、`subtitle_safezone`（平台档案校验，当前 MarginV=360 过 douyin 16% 档）、`subtitle_emphasis_mode` 留痕。「复杂背景可读率≥98%」验收口径=pre_mitigation 低读 cue **全部**获得底板补偿（post-mitigation 100%），raw 比例不粉饰；真 OCR（tesseract）未装，当前判定为亮度+边缘密度像素代理，装齐后可升级采样器不动链路。
- **烧录文本来源（2026-11-09 固化）**：成片硬字幕=精修稿优先（§3.2 第 5 步）——`source=llm` 切片烧精修稿（无语气词），缺失/fallback 如实回退纠错转写；`warnings.json.subtitle_provenance.subtitle_source` 可审计。字幕编辑器人工修订（rev-NNN ASS）只对 sidecar 底片生效，burned 底片以画面为准；要换字幕文本需重新剪辑（渲染指纹识别精修/转写/字幕规范变化，当前 `subtitle-v3/1`）。
- **words 接线与存量成片重烧（2026-11-12）**：words 进入渲染后指纹升至 `words-align/1`，此后所有新渲染自动带词级对齐；存量成片换新字幕必须重编码（字幕烧录在画面里，无法只换文件）。run `c6cd115b` 的 5 条成片已执行**就地字幕重烧维护**（`backend/scripts/subtitle_reburn_inplace_once.py`，用户授权）：同 plan/sub_ranges/源片/编码参数原位重渲 → 抽帧对比确认长停顿留白生效、发布副本原子覆盖、`clips.json.published` 哈希刷新为真实新值；旧副本备份于 `tasks/output/<run>/.pre-subtitle-reburn-*`。**注意两条系统约束**：(1) `studio_artifacts` 台账（阶段5·点1 2026-11-22 起收紧）：同 (job,kind,clip,object_key) 且内容一致（sha/size 相同）仍幂等返回原行；**同 key 内容变化视为同址覆盖**——显式刷新行内 size/sha 并 `revision+1` + `overwritten_at` 留痕（warning 日志），不再静默；同 (job,kind,clip) 下的**新 object_key** 插入新行且 `revision` 单调递增（`20261122_0001_unified_sources.sql` 加列）。(2) 规划 provenance 冻结含实现文件哈希与闸门/overlay 身份，**任何 skill 实现变更后 reedit/`_plan_cache` 缓存必然失效**（protected resume 路径会硬失败 E_CACHE_CONTEXT_CHANGED）——跨实现变更的「只重渲」只能走直连 render + 原位发布，不能走 reedit（会重新 LLM 规划）。
- `POST /studio/clips/{p}/{c}/subtitles`：cues→overrides、`clip_config.update_config`（真变化才 bump `render_revision`，`task_history` 记 `UPDATE_SUBTITLE`），生成 `subtitles/rev-00N/{clip}.ass|.srt`。
- `GET` 返回 cues+config+overrides+版本列表 + **`subtitles_burned_in`**（读成片 `warnings.json`：`subtitle_mode=burned`）——前端据此**关闭播放器实时叠加层防重影**（2026-11-08 修复）；叠加层只服务未烧录（Phase 2 导出）场景。时间线字幕轨不受影响。
- 字幕合成公式与后端 ASS 生成器一致（`subtitle_compose.py`、`caption_templates.py`、`emoji_captions.py`；默认 44px 金黄 #FFD700、y_pct 78.125）。

### 4.4 封面体系
- realframe_v2 固化封面（渲染时自动）；手动替换/模板应用/多版本历史/回收站/撤销激活：`cover_templates.py`、`cover_replace.py`、`bump_cover/set_cover_pointer/prune_cover_history`（`studio_project_store.py`，原子 rev+1）。
- **封面与片头解耦（阶段2 P3 2026-11-18）**：封面资产与 0.8s 片头分家——资产（含 cover_intro.mp4）必产，片头烧入由 `COVER_INTRO_MODE` 控制（默认 off，实验分配入口）；`STUDIO_AUTO_COVER` 收窄为资产生产开关；发布门禁拦封面缺失 → **已按用户拍板（2026-11-18）降级为发布软标记**：封面缺失照常发布，条目带 `cover_missing` + warning 留痕；三资产定位=平台封面（cover.jpg 1080×1920）/自然首帧（片头 off 后成片第 0 帧）/开场钩子（规划层 hook_first_line）。详见 `docs/cover-generation-convention.md` §七。
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
| 字幕体系（阶段2 扩展 2026-11-18） | 烧录链 `subtitle_cueing.py`（cue 合成+固化样式+品牌字体链）、`subtitle_qc.py`（验收指标）、`subtitle_readability.py`（亮背景自适应+安全区）、`subtitle_emphasis.py`（关键词克制强调）；studio 金黄模板 `subtitle_compose.py`、`caption_templates.py`、`emoji_captions.py`、`font_registry.py`（`backend/fonts/` 自动发现） | ASS/SRT 生成与烧录、五项验收数据源 |
| 人脸重构图（阶段2 P4） | `reframe.py`（`REFRAME_MODE=auto`，默认 off；`video_utils.build_crop_trajectory`/`build_smooth_pan_expression` 平滑资产接线） | 横宽源单脸跟踪横转竖、上半身定位、手势保护、多人保守回退；审计 `reframe.json` |
| B-roll | `broll.py`（Pexels API，`PEXELS_API_KEY`） | 备用素材（可选） |
| 存储 | 本地 `tasks/`（in/processing/completed/failed/output/plans/analysis/status/transcripts/projects/logs）；R2 可选（`r2.py`，无凭据时降级本地） | 任务与产物事实源 |
| 数据库/队列 | PostgreSQL 16（`.tools/pg16`）、Redis 7.2（`.tools/redis-src`） | studio_jobs 租约、台账权威镜像 `project_manifests`（阶段5·点1）、artifact revision、资源队列 `queue` 列（阶段5·点3）、观测四表（阶段5·点4）、计费、缓存 |
| 访问控制 | `studio_access.py`（`AccessLedger`，默认 `/domain/.studio-resources/access/access.sqlite3`）+ 全局 `enforce_studio_access` | 对象级 ACL（task/project/clip），未登记 → 403 `studio_object_unregistered` |
| API Keys | `api_keys.py` + `auth_headers.resolve_authenticated_user_id`（只存 SHA-256） | 程序化访问 /fonts、/studio/transcribe |
| 额度台账 | `llm_budget.py`（按日、按 kind） | 所有 LLM 调用记账；超限走各阶段自动回退 |
| ASR 锁 | `/domain/.studio-resources/asr.lock` | 同时刻一个 ASR 模型（内存保护） |
| 可观测性（阶段5·点4） | `studio_observability.py` + 迁移 `20261123_0003_observability.sql`；读端点 `GET /internal/observability/summary`（worker 鉴权）；`STUDIO_OBSERVABILITY_TASKS=0` 可关闭后台任务 | 任务事件流水 `studio_job_events`（状态/阶段变化埋点，update_progress 与认领成功路径触发）、分钟聚合 `job_metrics_minute`（队列等待 p50/p95、阶段耗时 p50/p95、失败率；lifespan 每 60s 聚合上一完整窗口）、worker 心跳注册 `studio_worker_registry`（claim/heartbeat 时 upsert，Redis 键保留）、artifact 哈希抽样巡检 `studio_artifact_hash_audits`（每 30min，local 键重算 sha256，不一致 warning+落台账）。全部 best-effort 可降级 |

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
- `docs/subtitle-burn-convention.md` — 烧录字幕契约（阶段2 P1/P2 全量更新：6s 上限/药名/品牌字体/自适应底板/克制强调/安全区）
- `docs/cover-generation-convention.md` — 封面生成规范（阶段2 P3 更新：封面与片头解耦、发布软标记）
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

- 生成时间：2026-11-23 04:49 UTC · 代码版本：`0a935ff` · 生成器：`docs/generate_workbench_doc.py`（`--check` 可做漂移检测）

### HTTP 端点（backend/src/api/routes）

- `admin.py`（2）：`GET /health`、`GET /runtime-settings`
- `api_keys.py`（3）：`GET /`、`POST /`、`DELETE /{key_id}`
- `billing.py`（1）：`POST /subscription-email`
- `feedback.py`（1）：`POST /feedback`
- `ingest_kb.py`（6）：`POST /ingest/hot-topics`、`POST /ingest/scripts`、`GET /kb/search`、`GET /titles/search`、`GET /tags/browse`、`POST /kb/scripts/{script_id}/confirm`
- `jobs.py`（18）：`GET /uploads/capabilities`、`POST /uploads`、`POST /uploads/{session_id}/complete`、`POST /jobs/transcribe`、`POST /jobs/auto-edit`、`POST /jobs/re-edit`、`GET /jobs/{job_id}`、`POST /jobs/{job_id}/retry`、`POST /jobs/{job_id}/cancel`、`GET /jobs/{job_id}/artifacts/{artifact_id}/file`、`POST /internal/heartbeat`、`POST /internal/jobs/claim`、`POST /internal/jobs/{job_id}/knowledge-context`、`POST /internal/jobs/{job_id}/state`、`POST /internal/jobs/{job_id}/artifacts`、`GET /internal/jobs/{job_id}/artifacts`、`POST /internal/jobs/{job_id}/activate`、`GET /internal/observability/summary`
- `media.py`（102）：`GET /fonts`、`GET /fonts/{font_name}`、`POST /fonts/upload`、`DELETE /fonts/{font_name}`、`GET /transitions`、`GET /caption-templates`、`GET /broll/status`、`POST /studio/transcribe`、`POST /studio/transcribe/start`、`GET /studio/transcribe/chunks/{upload_id}`、`POST /studio/transcribe/chunk`、`POST /studio/transcribe/assemble`、`GET /studio/transcribe/jobs/{job_id}`、`POST /studio/insights`、`POST /studio/auto-edit`、`GET /studio/auto-edit/{task_id}`、`POST /studio/auto-edit/{task_id}/rerender`、`GET /studio/auto-edit/{task_id}/artifact`、`POST /studio/auto-edit/{task_id}/clips/{clip_id}/cover`、`POST /studio/auto-edit/{task_id}/clips/{clip_id}/cover-template/apply`、`POST /studio/auto-edit/{task_id}/clips/{clip_id}/cover-template/delete`、`POST /studio/auto-edit/{task_id}/clips/{clip_id}/cover/adopt`、`POST /studio/auto-edit/{task_id}/clips/{clip_id}/cover/restore`、`GET /studio/auto-edit/{task_id}/clips/{clip_id}/cover`、`GET /studio/auto-edit/{task_id}/clips/{clip_id}/cover/frame-candidates`、`GET /studio/auto-edit/{task_id}/clips/{clip_id}/cover/frame-candidates/{filename}`、`GET /studio/auto-edit/{task_id}/clips/{clip_id}/cover/copy-candidates`、`GET /studio/auto-edit/{task_id}/covers`、`PUT /studio/auto-edit/{task_id}/clips/{clip_id}/transform`、`GET /studio/auto-edit/{task_id}/clips/{clip_id}/refined-transcript`、`GET /studio/auto-edit/{task_id}/clips/{clip_id}/refined-copy.md`、`GET /studio/auto-edit/{task_id}/clips/{clip_id}/export-transformed`、`GET /studio/timeline/{project_id}`、`PUT /studio/timeline/{project_id}`、`GET /studio/timeline/{project_id}/preview`、`POST /studio/timeline/{project_id}/preview`、`GET /studio/timeline/{project_id}/source`、`GET /studio/timeline/{project_id}/view`、`PUT /studio/timeline/{project_id}/view`、`GET /studio/timeline/{project_id}/history`、`POST /studio/timeline/{project_id}/undo`、`POST /studio/timeline/{project_id}/redo`、`POST /studio/timeline/{project_id}/revision`、`POST /studio/timeline/{project_id}/tracks`、`DELETE /studio/timeline/{project_id}/tracks/{track_id}`、`GET /studio/timeline/{project_id}/assets/peaks`、`POST /studio/timeline/{project_id}/assets/audio`、`POST /studio/copilot`、`GET /studio/copilot/history`、`POST /studio/copilot/action-result`、`GET /studio/projects/{project_id}/clips`、`POST /studio/projects/{project_id}/sync`、`GET /studio/projects/{project_id}/data`、`POST /studio/projects/{project_id}/data/clear`、`POST /studio/projects/{project_id}/clips/{clip_id}/tasks`、`GET /studio/tasks/{task_id}`、`GET /studio/projects/{project_id}/tasks`、`GET /studio/projects/{project_id}/clips/{clip_id}/cover-history`、`POST /studio/projects/{project_id}/clips/{clip_id}/cover/undo`、`POST /studio/projects/{project_id}/clips/{clip_id}/cover/activate`、`DELETE /studio/projects/{project_id}/clips/{clip_id}/cover/revisions/{revision}`、`GET /studio/workflow`、`GET /studio/workflow/{task_id}/snapshot`、`POST /studio/workflow/skills/{skill_id}/toggle`、`POST /studio/workflow/regenerate/{project_id}/{clip_id}`、`GET /studio/clips/{project_id}/{clip_id}/config`、`PUT /studio/clips/{project_id}/{clip_id}/config`、`GET /studio/clips/{project_id}/{clip_id}/transcript/revisions`、`POST /studio/clips/{project_id}/{clip_id}/transcript/revisions`、`POST /studio/clips/{project_id}/{clip_id}/transcript/rollback`、`GET /studio/clips/{project_id}/{clip_id}/subtitles`、`POST /studio/clips/{project_id}/{clip_id}/subtitles`、`POST /studio/clips/{project_id}/{clip_id}/audio/bgm`、`DELETE /studio/clips/{project_id}/{clip_id}/audio/bgm/{asset_id}`、`GET /studio/clips/{project_id}/{clip_id}/render-config`、`POST /studio/projects/{project_id}/reedit/{run_id}/retry-cleanup`、`GET /studio/projects/{project_id}/transcript-ref`、`PUT /studio/projects/{project_id}/clips/{clip_id}/timeline-image`、`DELETE /studio/projects/{project_id}/clips/{clip_id}/timeline-image`、`GET /studio/tasks/{task_id}/project`、`GET /studio/projects`、`GET /studio/projects/list`、`GET /studio/projects/{project_id}/thumbnail.jpg`、`GET /studio/projects/{project_id}/clips/{clip_id}/first-frame.jpg`、`POST /studio/projects/{project_id}/archive`、`POST /studio/projects/{project_id}/restore`、`GET /studio/projects/{project_id}/delete/preview`、`POST /studio/projects/{project_id}/delete`、`GET /studio/projects/{project_id}/name`、`PUT /studio/projects/{project_id}/active-clip`、`GET /studio/cover-templates`、`GET /studio/cover-templates/{template_id}/example.svg`、`PUT /studio/projects/{project_id}/clips/{clip_id}/cover-template`、`POST /studio/cover-templates/{template_id}/mock-result`、`GET /studio/projects/{project_id}/cover-assets`、`GET /studio/projects/{project_id}/delete/status`、`POST /studio/projects/{project_id}/delete/retry`、`GET /studio/projects/{project_id}/exports`、`POST /studio/projects/{project_id}/clips/{clip_id}/deliver`、`POST /studio/projects/{project_id}/clips/deliver-batch`、`GET /studio/projects/{project_id}/glossary`、`PUT /studio/projects/{project_id}/glossary`
- `medical_kb.py`（3）：`POST /ingest`、`GET /search`、`POST /entries/{entry_id}/review`
- `topics.py`（15）：`GET /reviewers`、`GET /scripts`、`GET /scripts/{script_id}`、`POST /modifications`、`GET /modifications`、`POST /modifications/{mod_id}/resolve`、`GET /{topic_id}`、`POST /{topic_id}/brief`、`POST /{topic_id}/brief/draft`、`POST /{topic_id}/approve`、`POST /scripts/{script_id}/approve`、`POST /{topic_id}/generate-script`、`POST /scripts/{script_id}/link-job`、`POST /reviewers`、`DELETE /reviewers/{user_id}`

### worker 任务阶段（studio_job_worker 包）

- `_run_id`——
- `_run_content_analysis_stage`——第三步（2026-10-14）：AUTO_EDIT 的模型内容分析阶段
- `_run_clip_planning_stage`——第四步（2026-10-14）：完整内容切片规划——产出 clip_plan.json
- `_run_clip_refinement_stage`——第四步半（2026-11-05 两步制）：切片精修——clip_refinement.json
- `_run_clip_cover_stage`——每条切片发布前的固化封面步骤（realframe_v2：真实帧+文字叠加，纯本地）
- `_run_clip_render_stage`——第五步（2026-10-14）：clip_plan → 本地 FFmpeg 渲染，生成多个视频成品
- `_run_reedit_replace`——重新剪辑并替换旧切片（2026-10-15，replacement_mode=delete_old_after_succ
- `run_transcribe_only`——TRANSCRIBE_ONLY：提取音轨 → 转写 → 纠错 → transcript_ready（终态）
- `run_auto_edit_only`——AUTO_EDIT：读取既有纠错转写 → 分析 → 规划 → 渲染（第五步正式链）
- `run_legacy_full`——legacy 全链（config 无 job_kind 的存量/重试任务）：行为与拆分前完全一致
- `run_isolated_job`——

### supervisor 托管服务

`postgres`、`redis`、`frontend`、`backend`、`autoedit`、`worker`

### Inspector 标签

转写(transcript)、封面(cover)、效果(effects)

<!-- END:AUTO-SNAPSHOT -->
