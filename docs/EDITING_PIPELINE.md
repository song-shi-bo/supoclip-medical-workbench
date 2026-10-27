# 剪辑流程（现状快照）

> 截至 2026-10-15。规则清单 `workflow_version: 2026-10-13.10`。**定位：14 项规则用于「剪辑遵循」，QC 只测量留痕、永不拦截**。规划校验关闭（off）。
> **2026-10-14 起「开始转写」与「开始自动剪辑」是两类独立任务**：TRANSCRIBE_ONLY 只做转写+纠错（终态 `transcript_ready`），AUTO_EDIT 从 `retrieving_rules` 起步、强制复用已纠错转写、绝不重跑转写/纠错。
> **2026-10-14/15 起 AUTO_EDIT 正式链升级为全自动 staged 流水线**（§1.5–§1.10）：内容分析（模型）→ 切片规划（模型）→ 本地 FFmpeg 逐条渲染多成品，全程无质量拦截（只有 warning）、无人工确认；**不再使用 video-brain 规划/渲染/QC**。§2–§4 的 `planner.plan_v2`/渲染/QC 描述的是 video-brain legacy 链（仅存量任务）。
> **2026-10-15 模型 token 优化**：纠错改 patch 协议（整篇紧凑输入/只返回修改段/摘要+重叠分批），分析与规划显式 `max_tokens` 4096（超限提档 6144）；新增当日额度台账（§1.10）。**额度预算闸门已按用户拍板删除**：额度不足不排队，直接走各阶段自动回退。
> 本文是「工作台 + video-brain 当前实际执行」的剪辑全流程，与代码同步维护；文档与代码不一致时以代码为准。
> 覆盖：转写任务、自动剪辑主链、纠错门禁、切片规划、渲染、QC 拦截、成片台账、局部修改、封面版本管理、导出。

---

## 0. 一图总览

```
【任务一：开始转写 = TRANSCRIBE_ONLY（独立任务，到 transcript_ready 结束）】
 上传(分片) ─► extracting_audio(音轨校验) ─► transcribing(whisper+热词) ─► correcting(模型纠错)
     │ 通过全文门禁
     ▼
 transcript_ready：纠错转写落版本化存储 tasks/transcripts/<source_id>/v{n}.json
 （终态。明确禁止：金句/关键词、读取剪辑规则、切片规划、裁切、字幕、封面、渲染、卡片）

           │ 用户点击顶部「开始自动剪辑」（凭据：object_key+transcript_id+transcript_version）
           ▼
【任务二：开始自动剪辑 = AUTO_EDIT staged（前置=有效纠错转写，否则 422）】
  ─► retrieving_rules(记 rule_snapshot_id) ─► extracting_highlights(第三步：模型内容分析
      → tasks/analysis/<job>.json) ─► planning(第四步：模型切片规划，连续区间/柔性时长，
      只记 warning → tasks/plans/<job>/clip_plan.json) ─► rendering(第五步：本地 FFmpeg
      逐条渲染 H.264+AAC + SRT 时间平移 → tasks/output/<job>/clips/<clip_id>/…)
      ─► completed / completed_with_warnings / partial_succeeded / technical_failed
 诊断停点：stop_after=analysis → analysis_ready；stop_after=plan → plan_ready（终态）
 全程无质量拦截、无人工确认；单条切片技术故障隔离，已可解码成片跳过（重启恢复）；
 前端成品卡片从 clip_final + clip_plan artifacts 构建（staged 链不写项目台账）。
 【重新剪辑并替换旧切片】POST /api/jobs/re-edit：复用源片+转写 → 独立 staging 渲染
     → ≥1 条可解码才激活 + 按激活前清单清除旧切片（两阶段回收站）→ 素材区只剩新切片。

  【legacy 全链（仅存量任务）】retrieving_rules ─► 时间线工程 ─► video-brain
      （转写复用/plan_v2/9:16 重构/封面/QC）──§2–§4 描述这条链──► 台账登记

 【缓存与版本】同一 source 且原视频未变化、转写版本有效 ⇒ 自动剪辑必须复用该版本；
 自动剪辑可重复点击（每次都从 retrieving_rules 开始，绝不重复转写/纠错）；
 仅原视频被替换 / 用户点「重新转写」/ 版本失效时才重新转写。
```

## 1. 转写与纠错（TRANSCRIBE_ONLY 任务）

1. 前端分片上传（`/studio/transcribe/chunk|assemble` 或 R2 直传）→ 源片单副本落 `studio-uploads`（硬链接全链只读）；
2. 前端提交 `POST /api/jobs/transcribe`（`mode=transcribe_only`）→ `studio_jobs`（config.`job_kind=transcribe_only`）→ `studio_job_worker --job` 隔离子进程；
3. worker 状态机：`extracting_audio`（ffprobe 音轨校验，无音轨显式 `E_NO_AUDIO_STREAM` 失败）→ `transcribing`（faster-whisper + 医学热词注入，词表 mtime 热更新）→ `correcting`（**2026-10-14 起 = DSH 模型全文纠错** `transcript_correction_llm.py`，patch 协议见 §1.2；LLM 未配置/单批失败 → 该批回退同音词典保守规整，`engine`/`fallback_reasons` 留痕）→ **`transcript_ready`（终态）**；
4. 产物双落盘：① 纠错转写**版本化存储** `tasks/transcripts/<source_id>/v{n}.json`（`schema=studio.corrected_transcript/1`，含 asr_segments+raw_text+audit，`studio_transcript_store.py` 原子写、版本递增；分段含 `segment_id/start_ms/end_ms/raw_text/corrected_text/uncertain/correction_reason`，时间戳程序复制）；② `transcript_json` artifact 回传（insights/timeline/auto_edit_task 恒为 null）；
5. **明确禁止**（前端转写完成不出现卡片、不出金句面板）：金句/关键词提取、读取剪辑规则、切片规划、自动裁切、字幕、封面、渲染成片、台账写入。

红线（不可协商）：不改诊断结论、药名、剂量数值、病情描述与否定词；无把握保持原文标 `uncertain=true` 进人工审核。

### 1.2 纠错实现（`transcript_correction_llm.py`，2026-10-14；10-15 patch 化）

- DSH 模型按「完整上下文」做普通中文校对：只修错别字/同音字/漏字/重复字/标点/断句；**禁止**润色、扩写、改写句式、按医学术语表强制替换（恢复的是「说话人原本说出的词」）；
- **patch 协议（2026-10-15 额度优化）**：模型只返回「发生修改的分段」（`{index, corrected_text, reason, uncertain}`），未修改段不再逐段回显；不确定 → 不修改、输出该段并 `uncertain=true`、corrected_text 留空；
- **输入不携带时间戳**：紧凑行格式「全局序号|文本」；时间戳（start/end/start_ms/end_ms）全部由程序从原始分段复制合并，模型无权改动；输出分段数守恒（patch 越界/重复序号忽略并计数）；
- **整篇单次优先**：≤800 段（`CONTENT_CORRECTION_SINGLE_CALL_LIMIT`）且估算 prompt ≤24000 tokens 时一次请求全篇；确需分批 →「全篇开头摘要（默认 300 字）+ 当前批 + 上一批少量重叠段（默认 4 段）」，**不再每批重复 4000 字全文开头**；整篇输出超上限（token limit）自动降级分批重跑；
- **显式 max_tokens** 4096（`CONTENT_CORRECTION_MAX_TOKENS`），token limit 提档 6144 重试一次；
- 单批失败只该批回退 `transcript_correction` 保守规整；engine=`dsh-llm:<model>` / `dsh-llm:<model>+local-fallback` / `local-conservative-rules`，`fallback_reasons` 逐批留痕——**fallback 绝不冒充 dsh-llm 结果**；
- summary 记录 `mode/llm_batches/invalid_patches/llm_reserved_tokens_estimate/llm_prompt_tokens/llm_completion_tokens/llm_reasoning_tokens`；每次成功（与 token-limit 失败）调用记入当日台账（§1.10）。

## 1.5 AUTO_EDIT staged 流水线（2026-10-14/15 正式链）

- 入口：`POST /api/jobs/auto-edit`，请求体只带 `object_key / project_id / transcript_id / transcript_version`；存储中无有效转写 → **422「请先完成转写与纠错。」**；正式任务**不带 `stop_after`**（`stop_after=analysis|plan` 仅测试/开发诊断；旧 `analysis_only=true` 自动映射为 `stop_after=analysis`）；
- 正式状态机（全自动、无人工确认）：
  `queued → retrieving_rules（记 rule_snapshot_id=清单 workflow_version）→ extracting_highlights（§1.6 内容分析）→ planning（§1.7 切片规划）→ rendering（§1.8 本地渲染）→ completed / completed_with_warnings / partial_succeeded / technical_failed`；
  诊断停点：`stop_after=analysis → analysis_ready`、`stop_after=plan → plan_ready`（两者都是终态，已加入 claim 排除集合，不会被重复认领）；
- **全程不出现 transcribing/correcting**：输入只有已保存的指定版本纠错转写（`studio_transcript_store.load_corrected_transcript`）+ 规则清单版本；
- **无质量拦截**：规划/时长/字幕一般问题全部只记 warning；只有真实技术故障（FFmpeg 失败/产物不可解码）才 `technical_error`；
- **恢复语义**：`tasks/analysis/<job>.json`、`tasks/plans/<job>/clip_plan.json` 已存在则直接复用（不重新调用模型、不重新规划）；已可解码的 `clip_final.mp4` 渲染时自动跳过 → worker 重启可恢复；
- **不写项目台账**：前端成品卡片从 `clip_final` + `clip_plan` artifacts 构建（`page.tsx`）；封面/局部修改/Inspector 那套台账链当前服务 legacy 产物与「重新剪辑替换」新批次；
- **不用 video-brain**：bridge/runner/`planner.plan_v2`/QC 是 legacy 链（§2–§4，仅存量任务；bridge 的 `transcript_pre_corrected` 复用机制随 legacy 保留）；
- 兼容：config 无 `job_kind` 的存量任务走 legacy 全链（行为与拆分前一致）。

## 1.6 第三步：内容分析（`content_analysis.py`）

- 一次结构化模型调用提取四类：`keywords`（3–10 个，原文用词）/ `golden_quotes`（≤12 条，**逐字摘自原话**——代码侧空白归一后逐字校验，改写/拼接/发明的丢弃并留痕）/ `topic_units`（≤10 个，标题由模型归纳，**不用固定词表**）/ `candidate_intervals`（≤12 个）；
- **时间戳由程序重算**（被引用分段的原始数据），模型无权产生；模型只读「纠错文本+时间戳+段 id」，不读视频、不联网；
- **显式 `max_tokens` 4096**（2026-10-15 从 16384 降档），token limit 提档 6144 重试一次，仍失败自动本地回退（`plan_source=heuristic_fallback`，回退按停顿边界切分、位置命名），**不等人工确认、任务不失败**；
- 思考关闭（`thinking.type=disabled`，最低推理强度；网关 4xx 拒参自动降级并记住）；
- 落盘 `tasks/analysis/<job>.json`（原子写）+ `content_analysis` artifact 回传；记录含 `plan_source / llm_error / llm_calls / llm_duration_ms / model / rule_snapshot_id / llm_reserved_tokens_estimate / llm_prompt_tokens / llm_completion_tokens / llm_reasoning_tokens`；
- >600 段才分批（`BATCH_SIZE=400` 合并）；479 段实测单次完成。

## 1.7 第四步：切片规划（`clip_planning.py`）

- 输入只有：指定版本纠错转写分段 + 第三步四类分析产物（内存传递）；**每条切片是一个连续源区间**（start..end 之间全部分段归属该切片，程序按分段重算 `start_ms/end_ms/duration_ms`），禁止拼接不连续讲话；
- 条数不固定、宁缺毋滥：允许舍弃寒暄/重复/停顿/跑题/无法独立成立/高度重复（写进 `discarded{segment_ids,type,reason}`）；`CONTENT_PLAN_MAX_CLIPS`(默认 12) 是**资源保护上限**（超限按综合得分保留最优并记计划级 warning），不是凑数目标；
- 时长是柔性目标：优选 120–180s、接受 90–210s，内容完整可自然偏离；**结构性时长下限已废除**——<30s 只记 `short_duration` warning 并降时长得分，不淘汰（§2 的 60s 硬下限属于 legacy 规划器）；
- 综合评分权重：语义完整度 35% / 主题集中度 20% / 开头上下文 15% / 结尾自然度 15% / 金句证据 10% / 时长匹配 5%；每条切片带 `completeness`（一切质量问题=warning，无 reject）与 `cut_in_reason/cut_out_reason`；
- **显式 `max_tokens` 4096**（`CONTENT_PLAN_MAX_TOKENS`），token limit 提档 6144（`CONTENT_PLAN_MAX_TOKENS_RETRY`）重试一次；模型规划失败自动重试一次（`PLAN_MAX_RETRIES=1`），仍失败 → 本地回退（`plan_source=heuristic_fallback`）保存当前最优；>1200 段直接本地回退（规划需全局视野，不分批）；思考关闭复用 content_analysis 探测结论；
- 落盘 `tasks/plans/<job>/clip_plan.json` + `clip_plan` artifact 回传；记录含 `plan_source/llm_error/llm_calls/llm_duration_ms/model/transcript_version/rule_snapshot_id/warnings` 与 token 预留/真实 usage 字段。

## 1.8 第五步：本地渲染（`clip_render.py`，零模型依赖）

- clip_plan → 逐条 FFmpeg 渲染：连续区间裁切（`-ss/-t` 精确到计划毫秒）、H.264（veryfast/crf23）+ AAC 128k、保留自然音频、+faststart、偶数像素（**保持源分辨率，不做 9:16 重构图**）；
- 字幕：复用指定版本纠错转写做**时间平移**（`subtitle_ms = transcript_ms - clip.start_ms`），裁剪到切片区间；只用本切片范围内分段，绝不用其他切片文字、绝不让模型重写；`warnings.json` 记录 **`subtitle_provenance`**（transcript_id/version/segment_ids/时间平移公式）与 `rule_snapshot_id`；
- 产物：`tasks/output/<job_id>/clips/<clip_id>/{clip_final.mp4, subtitles.srt, warnings.json, clip_plan.json}`；**不生成 cover.jpg（缺封面不是错误）**；
- **没有任何质量拦截**：时长/completeness/评分/字幕一般问题全部只进 warnings；只有 FFmpeg 失败或产物不可解码（ffprobe 校验）算 `technical_error`；
- 隔离与幂等：单条失败继续其余（`ClipRenderError` 捕获）；已存在且可解码的 `clip_final.mp4` 直接跳过（worker 重启恢复）；clip_id 稳定 → 重试不重复出片；
- 终态聚合：全部成功无警告 `completed`；全部成功有警告 `completed_with_warnings`；存在技术故障 `partial_succeeded`（成功文件保留）；全部技术故障 `technical_failed`。每条成片/警告登记 artifact（`clip_final`/`clip_warnings`）。

## 1.9 重新剪辑并替换旧切片（`reedit_replace.py`，2026-10-15）

- 入口：`POST /api/jobs/re-edit`（`replacement_mode=delete_old_after_success`，`reuse_transcript`/`force_new_run` 强制 true；新 `run_id` + `idempotency_key=reedit-<project>-v<n>-<rand>`）；前端顶栏按钮**一次确认**（复用源片与转写：不重传/不 ASR/不纠错）；
- 状态机：`reedit_requested → staging_run → extracting_highlights → planning → rendering → verifying_outputs → activating_run → purging_old_clips → completed / completed_with_warnings / completed_with_cleanup_warning`；verify 失败 → 清 staging → `technical_failed`，**旧切片全部保留**；
- 新批次写独立 staging `tasks/output/<run_id>/`，绝不触碰旧切片目录；分析/计划工件与已可解码新成片自动复用（重启恢复）；已激活未清理（`ACTIVATED` 标记）只继续清理；
- **激活前精确清单** `purge_manifest.json`（schema `reedit.purge_manifest/1`）：旧 clip_id/旧 render_task_id/逐个目标路径；清理目标必须在白名单根 `tasks/{output,completed,failed,status,localtasks,logs}` 下，**禁止**通配/递归删项目根；`tasks/media`（源片）、`tasks/transcripts`（转写）、新 run 目录永不入清单；引用计数：旧 render_task_id 若被任何非旧切片引用则整个剔除（`skipped_referenced_task_ids`）；
- ≥1 条新切片可解码才激活：`register_clips` 幂等登记台账 + `ACTIVATED` 标记 → 两阶段回收站 `tasks/.purge-trash/<run_id>/`（先整体 `os.replace` 搬入，台账切换成功才物理删除；任一移动失败反向搬回、旧台账不动）；
- 报告 `reedit_report.json`：删除切片数/文件数/删除前字节/实际释放字节/失败项；清理有失败 → `completed_with_cleanup_warning`，`POST /studio/projects/{pid}/reedit/{run}/retry-cleanup` 只重试清理；成功后前端素材区只剩新切片。

## 1.10 模型额度：旋钮与当日台账（2026-10-15）

网关按「`prompt_tokens + max_tokens`」做**每请求预留**（未设置 max_tokens 时按 provider 默认——会把小请求也顶到 429）；额度残余少时大请求先死。**所有模型调用显式 max_tokens**；glm 思考会吃掉输出上限，故分析/规划/纠错均请求**关闭思考**（最低推理强度，网关拒参自动降级）。

| 环节 | 旋钮（env，默认） |
|---|---|
| 纠错 | `CONTENT_CORRECTION_MAX_TOKENS`(4096) / `CONTENT_CORRECTION_MAX_TOKENS_RETRY`(6144) / `CONTENT_CORRECTION_BATCH_SIZE`(100，最小 10) / `CONTENT_CORRECTION_SINGLE_CALL_LIMIT`(800) / `CONTENT_CORRECTION_SINGLE_PROMPT_TOKEN_LIMIT`(24000) / `CONTENT_CORRECTION_CONTEXT_OVERLAP`(4) / `CONTENT_CORRECTION_HEAD_CHARS`(300，全篇摘要字符) / `CORRECTION_LLM_TIMEOUT_SECONDS`(180) |
| 内容分析 | `CONTENT_ANALYSIS_MAX_TOKENS`(4096) / `CONTENT_ANALYSIS_MAX_TOKENS_RETRY`(6144) / `CONTENT_ANALYSIS_THINKING`(disabled) / `CONTENT_ANALYSIS_LLM_TIMEOUT_SECONDS`(240) |
| 切片规划 | `CONTENT_PLAN_MAX_TOKENS`(4096) / `CONTENT_PLAN_MAX_TOKENS_RETRY`(6144) / `CONTENT_PLAN_MAX_CLIPS`(12) |

**额度不足的行为（2026-10-15 用户拍板：预算闸门已删除）**：不再估算拦截、不再排队等待重置；429 时由各阶段自动回退兜底——纠错该批回退保守规整、分析/规划本地回退，任务照常推进并在 `engine`/`plan_source`/`fallback_reasons` 如实留痕。`llm_budget.py` 只保留**观测**：`estimate_tokens/estimate_reserve`（预留估算，随调用留痕）。

**当日台账** `tasks/llm_usage/<YYYY-MM-DD>.json`：每次成功调用（以及 token-limit 失败——同样消耗预留）记录 `kind/job_id/est_reserved/usage(prompt,completion,reasoning)/model`，并累计 `reserved_total`；UTC 日期切换即新账。**诚实边界**：台账只覆盖管线三环（纠错/内容分析/切片规划）；Copilot 同享应用额度但不在台账内。429 两种消息：`Application daily quota exhausted` / `Application is busy; retry later`；**禁止周期性真实请求探测额度**。

## 2. 切片规划（`planner.plan_v2`）—— legacy video-brain 链（仅存量任务）

> ⚠ 2026-10-14 起正式 AUTO_EDIT 用 §1.7 的 `clip_planning.py`（工作台本地），不再调用 video-brain 规划。本节描述 video-brain 内置规划器，仅服务 legacy 全链与历史任务。

```
输入：纠错转写 + 规则快照(冻结) + n_clips("auto"|固定) + dur_range + pinned
① 定条数 decide_auto_k（auto 模式）
   k_time = 源时长÷25s（目标下限），封顶：源片>600s 最多 6 / 否则 3
   k_topic：从 k_time 逐级下降做可行性测试（能选出 k 条互不重叠+主题互异的完整组）
   → 宁缺毋滥：保底 1 条
② 产出 attempt()：LLM 优先（成员裁定版提示词）/ 失败或未启用 → 启发式（显式留痕降级）
   启发式：枚举 25–90s 发现窗全部候选（479 段素材≈13451 个，2026-10-13 起毫秒级）
   → 主题纯度贪心成组（互不重叠+主题互异，泛主题降权）→ 超 45s 完整表达优先拆分，
     拆不动记 duration_exception（必须写依据，禁止悄悄截断）
③ 硬校验 validate_plan_v2：条数精确/时间戳合法/时长≥下限/主题互异+证据独立/
   医疗红线/证据可回溯(source_segment_ids)/首尾语义完整(completeness_of)
   ── 校验：**已彻底关闭**（清单 clip_planning_rules.validation_mode=off，
        validate_plan_v2 不调用、零开销零留痕），规划完成直接进渲染。
        可选档位：off（现）/ advisory（只留痕）/ enforce（replan≤2 → 人工）
④ 质量闸门落盘 plan_meta：plan_source(llm|heuristic)/llm_error 真实原因/
   validated/needs_review+原因/gates ── 全部落 clip_plan.json（或 plan_rejected.json）
```

- LLM 路径当前**降级**（总闸 `llm_enabled=False` 且 LLM 未配置）：`plan_source=heuristic` 显式标注。是否开总闸待拍板（行为剧变+每日配额，见 HANDOFF 待办 7）。
- **性能（2026-10-13 修复）**：鸽笼快速通道（不同主题数 < n_clips → O(n) 判定无解走兜底）+ 词对记忆化；17 分钟/479 段素材 plan_v2 从 >600s → **1.13s**。残余：主题丰富的长素材仍 O(n²)，约分钟级，后续可做候选去重/向量化。
- **时长硬下限（2026-10-13 用户拍板）**：`clip_planning_rules.duration_min_s=60`——候选枚举只生成 ≥60s 的窗口、条数估算与校验下限同步；单切片复渲染钉窗豁免。当前软目标 25–45s 与之并存，60s 以上切片记 duration_exception 留痕（如需对齐目标区间改 [60,90] 待拍板）。
- **语义现状**：`_topic_of` 词表窄，单一主题讲座会全落「健康科普」桶 → 只出 1 条（宁缺毋滥的设计结果）。扩词表/切换 `_topic_of_v2` 待产品拍板（HANDOFF 待办 10）。

## 3. 渲染—— legacy video-brain 链（仅存量任务）

> 正式 AUTO_EDIT 的渲染见 §1.8（工作台本地 FFmpeg：无 9:16 重构图、无封面、无 QC；保持源分辨率）。本节描述 video-brain 渲染链。

- `rendering_reframed_source`：9:16 重构图（人脸跟踪裁剪，MediaPipe→OpenCV DNN→Haar 兜底）；
- `rendering_cover`：封面渲染——首次自动剪辑 `realframe_v2`（清单 `cover_rules.style_for_auto_edit`）；重制 `cutout_v3`（缺 u2net 权重显式报 `E_CUTOUT_WEIGHTS_MISSING`，绝不静默换风格）；
- `rendering_clip_N`：逐切片渲染字幕（ASS MarginV≥380、字号≤64、底部安全带）与成片；**单切片失败不影响其余**，失败切片可 `POST /v1/clips/{id}/retry`；
- 约束：1080×1920、H.264+AAC、偶数像素、封面 0.8s 片头（文字窗不与正片字幕重叠）。

## 4. QC 拦截（`qc.validate_clip`，`qc@3`）—— legacy video-brain 链（仅存量任务）

> 正式 AUTO_EDIT staged 链没有 QC 环节（§1.8：无质量拦截，只有 warnings 与技术故障隔离）。

每条成片渲染后过 **19 项检查**，按四类门槛汇总，`reject_clip` 任一不过该条不输出：

| 门槛 | 检查项 |
|---|---|
| 完整性 | （以下 19 项检查在 `enforcement=off` 下**不运行**，仅保留代码供恢复）playable / codec_h264_aac / resolution_1080x1920 / duration_target / cover_duration / subtitle_provenance / head_tail_boundary |
| 主题独立性 | two_topic_distinct（主题互异、重叠≤5s、证据重合<60%） |
| 医疗安全 | no_unresolved_review / medical_red_lines / cover_hook_evidence |
| 视觉 QA | subtitle_safe_area / sticker_window / sticker_no_collision / crop_cuts_person / cover_text_window / cover_theme_limits（2–6 字）/ cover_hook_limits（≤12 字两行） |

**QC = 合规测量，不是审核闸门（清单 `qa_rules`，2026-10-13 用户最终拍板）**：`enforcement=record` + `enabled_categories=[completeness, visual_qa]`——渲染管线在**构建时**即按 14 项规范产出（1080×1920 H.264+AAC、字幕安全区、封面文字窗与限额、60s 时长下限等是结构化保证），QC 照常测量这 14 项并落 `qc_report.json` 供查看，但**永不 reject_clip、永不 qc_failed、永不卡住成片生成**；主题独立性与医疗安全 3 项不运行、不留痕。检查 16 cover_text_window 已做时基修正（qc@4：字幕烧录在主片段内、与封面片头前后相邻，物理不相交，旧实现误杀首句贴 0s 的素材）。转写级医疗风险扫描维持下线（2026-10-13 用户拍板），医疗拦截靠纠错红线门禁 + 封面文案红线。`retry_clip` 已修复重渲记录持久化（旧 bug：rejected 标记残留导致回传 copied=0）。

## 5. 成片台账（卡片唯一事实源）

- `tasks/projects/{project_id}/clips.json`：稳定 `clip_id`（`render_task_id` 首渲后永不改）、一 clip 一卡；
- 封面是独立资产：`cover_asset_id=cover-{clip_id}-v{n}`，`cover_revisions/rev-NNN/` 快照 + v1 基线备份；
- **不变量**：局部修改绝不新增卡片、绝不重跑 AUTO_EDIT、源片全链路只读；
- **staged 链（2026-10-14 起）不写台账**：正式 AUTO_EDIT 成品卡片由前端从 artifacts 构建；台账当前服务 legacy 产物 + 「重新剪辑替换」激活的新批次（§1.9）。

## 6. 局部修改（全部走类型化局部任务，不复制成片）

| 用户说法 | 任务类型 | 行为 |
|---|---|---|
| 重新制作封面（页面按钮 / AI 助手 / 带修改要求） | `REGENERATE_COVER` | 只重做封面图+0.8s 片头，新 revision 原位替换，旧版可撤销；失败保留旧封面并显示原因+重试 |
| 标题/主题 | `UPDATE_TITLE` | 按标题规则（忠于转写+禁用词字面校验），封面文字连同片头重出 |
| 字幕样式 | `UPDATE_SUBTITLE_STYLE` | 只落样式配置+render_revision（字幕重渲 adapter 未实现，显式标注） |
| 删几秒/重剪 | `UPDATE_TIMELINE` | 改时间线区间；重渲为显式开关 |
| 换 BGM 音量/淡入淡出/移除（AI 助手 `update_bgm`） | `UPDATE_AUDIO` | 只动 kind=bgm 轨；混音在导出侧生效；新 BGM 文件走效果页上传 |
| 封面版本管理（Inspector 封面页） | `cover/activate`·`cover/revisions/{n}` DELETE | 任意历史版本「设为当前」原位切换（QA 未通过拒绝 `E_COVER_QA_FAILED`）；删除仅限非当前且非 v1 基线 |
| 重新按规则剪辑 | `AUTO_EDIT`（仅显式入口） | 唯一允许全链重跑的路径 |

规则优先级：**用户本次指令 > 项目设置 > 已启用 skill > legacy 默认**；任务创建即冻结规则快照，运行中不读实时清单。

## 7. 导出（预览 = 导出）

- `viewerZoom` 只影响观看；`clip transform`（scale/position/crop）影响导出，前后端数学由 10 组对照向量逐值守护；
- 默认变换 → 直下 `clip_final.mp4`；非默认 → `export-transformed`（FFmpeg 按 transform 哈希缓存重渲，含效果页五分区微调：eq/unsharp/色温 + 音频 amix/ducking/afade/BGM）；
- 下载：视频 / 封面 / 字幕均走 artifact 端点；批量导出为后续项。

## 8. 当前已知边界（诚实清单）

1. **LLM 受控凭据已配置（2026-10-14 起）**，纠错/分析/规划走模型链；额度 429 或输出不合法时自动降级（纠错→保守规整、分析/规划→本地回退，`plan_source`/`engine`/`fallback_reasons` 显式留痕，**fallback 绝不冒充 dsh-llm 结果**）。额度机制与旋钮见 §1.10；额度不足不排队（预算闸门已删除），直接回退照常出片。
2. **字幕重渲 adapter 未实现**（video-brain 无跨服务字幕端点）：字幕样式改动只落配置，不真实重渲（显式标注，非静默）。
3. **转写质量依赖素材**：whisper small CPU + 无 LLM 纠错时，方言/音质差的素材会产出碎段错字（实测案例：「张小张达们」），规划/QC 会按规则拦截，但源头在转写。
4. **单一主题素材只出 1 条**（宁缺毋滥 × `_topic_of` 词表窄）：扩词表/切换 `_topic_of_v2` 待产品拍板。
5. 主题丰富的长素材规划仍 O(n²) 常数级开销（约分钟级），候选去重/向量化留后续。
6. **短素材行为两条链不同**：legacy 规划器 `duration_min_s=60`（<60s 素材 0 条）；staged `clip_planning` 已废除结构性时长下限（<30s 只记 warning 不淘汰）。60s 以下素材的产品口径仍待拍板。
7. **staged 链产物无封面、保持源分辨率**（无 9:16 重构图/人脸跟踪/封面文字窗）：封面资产化与局部修改（REGENERATE_COVER 等）当前只服务台账产物；staged 成品的封面/变换补齐待产品拍板。
8. **额度台账未覆盖 Copilot**：台账只记录管线三环（纠错/内容分析/切片规划）；Copilot 同享应用额度（§1.10 诚实边界）。

## 9. 运维速查

- 重启 video-brain / studio_server / studio_job_worker / **autoedit_runner**：确认无 processing/queued → kill 对应进程 → supervisor ~14s 自动拉起；改 `backend/**` 或 `video-brain/app/**` 必须重启（**runner 是常驻循环，改代码后必须单独重启**，否则旧代码继续消费新任务）；前端 dev 热更新即生效；
- 测试基线（2026-10-15）：video-brain **126** / 工作台后端 **1091** / 前端 **38 文件 326 例** / tsc 0 error / `npm run lint`（**必须 npm run lint**，裸 npx eslint 假警报）；不得回退；
- 转写存储：`tasks/transcripts/<source_id>/v{n}.json`（`studio_transcript_store.py`；`source_id=sha256(object_key)[:16]`）；诊断转写复用问题先看这里；
- staged 产物：分析 `tasks/analysis/<job_id>.json`、计划 `tasks/plans/<job_id>/clip_plan.json`、成品 `tasks/output/<job_id>/clips/<clip_id>/`、替换 `tasks/output/<run_id>/{purge_manifest.json,ACTIVATED,reedit_report.json}`、模型台账 `tasks/llm_usage/<YYYY-MM-DD>.json`；
- 额度诊断：429 → 各阶段自动回退（不排队），看当日台账与各记录的 `fallback_reasons`/`llm_error`。
- 日志：`backend/logs/services/{backend,autoedit,video-brain,frontend}.log`；video-brain `data/access.log`；
- 规则：改 `video-brain/config/editing-workflow.manifest.json` 下次任务生效；热词改 `config/medical_terms.txt` 即生效。
