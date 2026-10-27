# 工作台移交文档（HANDOFF）

> 写于 2026-10-15（UTC 凌晨）。本文档取代之前所有版本的移交记录。
> **新会话必读顺序**：本文档 → `DSH_STARTUP.md`（环境事实）→ `docs/ARCHITECTURE.md` → `docs/EDITING_PIPELINE.md`（⚠这两份截至 10-12/10-14 早，**未同步步骤 2–5**，差异清单见待办 0）→ `docs/clear-project-data.md`。
> 流程口径冲突时：**代码为准**；剪辑主链新口径见本文 §1.5。

## 0. 一句话现状

「开始转写」与「开始自动剪辑」已彻底拆分，且 **AUTO_EDIT 正式链已升级为全自动 staged 流水线**：分析（模型）→ 切片规划（模型）→ **本地 FFmpeg 逐条渲染多成品**，全程无质量拦截（只有 warning）、无人工确认；另有「重新剪辑并替换旧切片」（新批次成功后永久删除旧切片，两阶段回收站）。9 段转写已完成模型纠错 **v2**（山自停腰→擅自停药、拱柱→汞柱等 8 处，时间戳不变，v1 保留）；17 分钟素材的 v2+规划验收由后台任务在配额重置后自动完成（见待办 1）。
**基线（2026-10-15 01:30，不得回退）**：后端 pytest **1076** / 前端 vitest 38 文件 **326** + tsc 0 error + eslint **14w0e（必须 `npm run lint`）** / video-brain **126**（本轮未动）。

## 1. 环境事实（本容器，实例 3445 / songshibo）

- 端口：前端 dev **3107**（Turbopack 热更）、工作台后端 **8000**（`src.studio_server`）、video-brain **8940**、PostgreSQL **5432**、Redis **6379**。公网入口 `https://101.201.81.118:3445/studio`（401=先登录 DSH）。
- **重启后端**：确认 `tasks/in|processing` 空且无 `--job` 子进程 → kill `src.studio_server` 与 `src.studio_job_worker` → supervisor ~14s 拉起。改 `autoedit_runner_video_brain.py` 后单独 kill runner。服务当前运行的就是最新代码（10-15 01:29 重启）。
- PostgreSQL 不自愈：`rm -f /domain/.tools/pgdata/postmaster.pid && /domain/.tools/pg16/bin/pg_ctl -D /domain/.tools/pgdata -l /domain/.tools/pg.log -o "-p 5432" start`。容器可能被平台重启；重启后先拉 PG 再等 supervisor 重试。
- **模型配额（2026-10-15 调查结论，重要）**：工作台模型走管理侧独立应用凭据的 OpenAI 兼容网关（地址/账号标识属受保护配置，键名 `LLM`/`OPENAI_API_KEY`/`OPENAI_BASE_URL`，值不读不展示；`GET /models` 确认仅授权 `glm-5.3-flash`）。**每日额度按 token 预留**：每请求预留 = `prompt_tokens + max_tokens`（未设置 max_tokens 时按 provider 默认——会把小请求也顶到 429；prompt 含 ~220 固定开销；completion 含 reasoning_tokens），另有 500 请求/日与团队配额；**UTC 00:00 重置；无查询接口、无限额响应头**（billing/quota 端点全 404 `Endpoint not allowed`）。额度残余少时 **大请求先死**（~2-3k tokens 被 429 而 ~250 tokens 能过）。缓解旋钮（都已实现）：纠错 `CONTENT_CORRECTION_MAX_TOKENS`(4096)/`BATCH_SIZE`(100)/`CONTEXT_OVERLAP`(50)/`HEAD_CHARS`(4000)，分析 `CONTENT_ANALYSIS_MAX_TOKENS`(16384)/`CONTENT_ANALYSIS_THINKING`(disabled，网关拒参自动降级)。**禁止周期性真实请求探测额度**——等 UTC 00:00 后单次重试。429 两种消息：`Application daily quota exhausted` / `Application is busy; retry later`。
- 工作台通道与 DSH 聊天**相互独立**（应用额度耗尽期间 DSH 聊天正常）。工作台内部 Copilot/纠错/分析/规划共享同一应用额度；若要规划独享需管理员建第二凭据 + 代码加 `CONTENT_ANALYSIS_LLM` env 覆盖（未实现，待拍板）。
- 规则清单改动即时生效（mtime 缓存）但**测试跑真实清单**；热词 `medical_terms.txt` 改文件即生效。
- 不要动：`/profile`、`.env` 值、密钥；**不 commit**（video-brain 无 git 历史，改前 ast.parse、改后跑全量）。
- 子代理可能假死：派发任务书要窄；接手前先 `interrupt_agent`。

## 1.5 本次会话交付（2026-10-14 晚 → 10-15，已全部测试）

| 步骤 | 内容 | 关键位置 | 测试 |
|---|---|---|---|
| 2 转写纠错升级 | faster-whisper 转写不变；纠错改为 **DSH 模型全文纠错**（上下文同音/漏字/重复/标点；不润色不强制医学术语表；uncertain 保留原文；时间戳程序复制；失败回退词典）；分段新增 `segment_id/start_ms/end_ms/raw_text/corrected_text/uncertain/correction_reason`；前端转写行=时间码+纠错文+原文折叠+不确定标记+原因 | `transcript_correction_llm.py`、`studio_job_worker.py::stage_correcting`、`page.tsx` 转写行 | 10 例 |
| 3 内容分析 | 「开始自动剪辑」→ 内容分析（一次结构化调用提取 keywords/golden_quotes/topic_units/candidate_intervals；金句逐字校验、时间戳重算、topic 无词表、失败自动回退），落盘 `tasks/analysis/<job>.json` + artifact；诊断停点 `analysis_ready` | `content_analysis.py`、`jobs.py`、`page.tsx` | 12 例 |
| 4 切片规划 | 完整规划：连续区间（禁拼接）、柔性时长（优选 120–180s/接受 90–210s/只 warning）、权重 35/20/15/15/10/5、舍弃寒暄/重复/跑题、`max_clips` 资源保护上限（env 可调）、模型失败重试一次→本地回退；`tasks/plans/<job>/clip_plan.json`；恢复=复用分析/计划工件不重调模型 | `clip_planning.py`、`studio_job_worker.py`、`studio_job_store.py`（claim 终态）、`jobs.py`、`page.tsx` | 12 例 |
| 5 本地渲染 | clip_plan → **本地 FFmpeg 逐条渲染**（连续区间裁切 H.264+AAC、自然音频、SRT 时间平移、`warnings.json` 含 subtitle_provenance/rule_snapshot_id、**不生成封面且不算错误**）；单条技术故障隔离；已可解码成片跳过（重启恢复）；终态 `completed/completed_with_warnings/partial_succeeded/technical_failed`。**不用 video-brain 规划**（legacy 全链仅存量任务） | `clip_render.py`（零模型依赖）、`studio_job_worker.py::_run_clip_render_stage`、`page.tsx`（成品卡片从 clip_final+clip_plan artifacts 构建） | 7 例（真实 FFmpeg） |
| 扩展 重新剪辑替换 | 「重新剪辑并替换旧切片」：复用 source+transcript_version（不重传/不 ASR/不纠错），新 run_id+idempotency_key，新批次写独立 staging；≥1 条可解码才激活+按**激活前精确清单**清除旧切片（两阶段回收站、引用计数、白名单根、报告含释放字节）；失败保留旧切片清 staging；清理失败→`completed_with_cleanup_warning` + 只重试清理端点；前端一次确认（文案三点）+素材区只剩新切片 | `reedit_replace.py`、`POST /api/jobs/re-edit`、`POST /studio/projects/{pid}/reedit/{run}/retry-cleanup`、`studio_job_store.py`、`page.tsx` 顶栏按钮 | 4 例 |
| 纠错 v2 | 9 段转写 **v2 已生成**（配额重置后第 48 次探测命中）：物区→误区×4、山自停腰→擅自停药、夹中→加重、拱柱→汞柱、间一把→建议把、长气→长期、高位→高危、将到→降到；**时间戳逐段一致、v1 原样保留、逐段依据入 audit、uncertain=0**；engine=`dsh-llm:openai:glm-5.3-flash` | `tasks/transcripts/eef9e151e19c77bd/v2.json`、`/domain/.recorrect/v2_result.log` | 真实调用 |

AUTO_EDIT 最终状态序列（正式，无 `stop_after`）：`queued → retrieving_rules → extracting_highlights → planning → rendering → completed / completed_with_warnings / partial_succeeded / technical_failed`。诊断停点：`stop_after=analysis`→`analysis_ready`、`stop_after=plan`→`plan_ready`。替换模式另见 §1.5 扩展行。所有新终态均已加入 claim 排除集合（不会被重复认领）。

## 2. 历史交付（摘要）

10-13 全天与 10-14 白天交付（播放器/卡片/Inspector/封面版本管理、规划器鸽笼快速通道、QC 只测量、纠错固化、retry_clip 修复、update_bgm、清空项目数据等）已合入代码并有各自测试；细节见 `docs/EDITING_PIPELINE.md` 与 `docs/clear-project-data.md`（注意后者 test 计数是准确的）。

## 3. 待办（按优先级）

0. **文档同步**：`ARCHITECTURE.md`/`EDITING_PIPELINE.md` 未记录步骤 2–5。需补：新模块 5 个（`transcript_correction_llm`/`content_analysis`/`clip_planning`/`clip_render`/`reedit_replace`）、AUTO_EDIT staged 状态机与全部新状态、`POST /api/jobs/re-edit` 与 retry-cleanup、额度旋钮、字幕 provenance、§1.5 各表。
1. **17 分钟素材验收（自动）**：后台任务 `bash-33` 睡到 UTC 00:01 后单次执行「479 段重纠错 v2（小批次 30/12/1200 + max_tokens 4096）→ stop_after=plan 全链规划」，结果写 `/domain/.recorrect/step4_result.log`（探测日志 `step4_probe.log`）。若容器重启任务丢失，手动：`cd backend && CONTENT_CORRECTION_BATCH_SIZE=30 CONTENT_CORRECTION_CONTEXT_OVERLAP=12 CONTENT_CORRECTION_HEAD_CHARS=1200 PYTHONPATH=. .venv/bin/python /tmp/step4_acceptance.py`（注意：脚本会先重纠错生成 v2 再规划，不落盘非模型结果）。
2. **正式 AUTO_EDIT 真机端到端**：staged 全链（分析→规划→渲染多成品）从未用真实模型+真实素材完整跑过（配额限制，本轮测试全部用已保存工件/桩/合成素材）。配额充足后上传一条真实素材点「开始自动剪辑」验收：状态序列、clips/ 产物、ffprobe、前端卡片、导出。
3. **重新剪辑替换真机验收**：按钮→确认→自动替换→素材区只剩新切片→旧产物释放；`completed_with_cleanup_warning` 分支与「重试清理」真机走一遍。
4. LLM 配置仍为零成本最大杠杆（`/health` 自报 not-configured 指的是 video-brain；工作台通道配额受管）。若配额持续紧张：管理员建第二凭据 → 代码加 `CONTENT_ANALYSIS_LLM` env 覆盖实现规划独享。
5. 原待办延续：`_topic_of` 词表窄（单主题只出 1 条）、混排长素材 O(n²)、效果页导出对比实测、短素材 <60s 出 0 条的产品决策、cutout_v3 真实素材验证、字幕重渲 adapter 未实现、规则代理 `ea0fd2c1` 最终报告未收。
6. 验证数据清理（用户要空态时）：`timeline_e2esplit02`/`timeline_790a6b0c9d87`/`timeline_abc123def456`/`timeline_afe4ba0a831f` 台账与 `tasks/output/autoedit_*`、`tasks/analysis|plans` 验收记录、DB `studio_jobs` 里终态验收任务行。**保留**：`.u2net/`、代码、文档、截图、9 段与 479 段转写存储。
7. `docs/clear-project-data.md` 的「删除项目本身」「回收站自动 GC」为有意不做，勿当缺陷。

## 4. 重要的坑（本轮新增 + 沿用）

- **额度 429 是 token 预留制**：见 §1。新写模型调用**必须显式 max_tokens**（glm 思考会吃掉 provider 默认输出上限，报 "Model token limit (provider default) exceeded"）。思考关闭用 `extra_body={"thinking":{"type":"disabled"}}`，网关 4xx 拒参自动降级（content_analysis 已封装，clip_planning 复用其探测结论）。
- **测试中 `studio_project_store.TASKS_ROOT` 是 import 期常量**：monkeypatch env 不够，必须 `setattr(store, "TASKS_ROOT", tmp)`（`test_reedit_replace.py` 有现成 fixture）。否则台账读写落到模块默认根、测试互相串台。
- **验收脚本多次 `asyncio.run`** 会撞 "attached to a different loop"：同一脚本只用一个 event loop（`/tmp/step4_plan_test.py` 有正确写法）。
- **`worker._http` 校验任务租约**：绕过 API 直调 stage 函数的脚本要么先 `claim_next_job(worker.WORKER_ID)`，要么 stub `_http`。
- **eslint 必须用 `npm run lint`**（next lint）：裸 `npx eslint .` 会扫出 858 errors 假警报。
- `pkill -f` 会自杀——用 `ps -eo pid,args | grep X | grep -v grep | awk '{print $1}' | xargs -r kill`。
- 改 `backend/**` 必须重启 server+worker（改 `runner` 单独重启；前端热更即生效）。重启前确认无 processing/queued 且无 `--job` 子进程。
- 沿用：容器可能被平台重启（PG 手动拉起）；新增检查先核对时间基与持久化；错误必须显式 `E_*`；源片全链只读+单副本；预览=导出同源数学；规则快照冻结。
- 后台长任务（如等配额）在容器重启后会丢，脚本都在 `/tmp` 与 `/domain/.recorrect/`，丢了按待办 1 手动跑。

## 5. 数据与路径速查（增补）

- 项目台账：`tasks/projects/<timeline_id>/clips.json`；分析记录 `tasks/analysis/<job_id>.json`；切片计划 `tasks/plans/<job_id>/clip_plan.json`；staged 成品 `tasks/output/<job_id>/clips/<clip_id>/{clip_final.mp4, subtitles.srt, warnings.json, clip_plan.json}`；替换清单/报告 `tasks/output/<run_id>/{purge_manifest.json, ACTIVATED, reedit_report.json}`。
- 转写存储：`tasks/transcripts/<source_id>/v{n}.json`——`eef9e151e19c77bd`（9 段）已到 **v2**（模型纠错）；`859e89b941036cb0`（17 分钟/479 段）仍 v1，v2 由待办 1 自动生成。
- 规则：`/domain/video-brain/config/editing-workflow.manifest.json`（`workflow_version 2026-10-13.10`）；热词 `config/medical_terms.txt`。
- 日志：`backend/logs/services/{backend,autoedit,video-brain,frontend}.log`。
- 额度调查脚本与日志：`/domain/.recorrect/`（step4_oneshot.sh、step4_probe.log、step4_result.log、v2_result.log）；一次性验收脚本 `/tmp/step4_acceptance.py`、`/tmp/recorrect_v2.py`、`/tmp/step5_real_render.py`、`/tmp/step3_acceptance.py`。
- 测试命令：后端 `cd backend && .venv/bin/python -m pytest tests/unit -q --no-cov`（**1076**）；前端 `npm run lint`（14w0e）/ `npx tsc --noEmit`（0）/ `npx vitest run`（38 文件 **326**）；video-brain `cd /domain/video-brain && .venv/bin/python -m pytest tests -q`（**126**）。
- 真实渲染验证样例（第五步）：`tasks/output/b5fae4fe7fb04166a1f449d1a4338069-r/clips/clip-422137c371/clip_final.mp4`（17MB，ffprobe 可解码）。

## 6. 后台任务与挂起事项

- `bash-33`（挂起，sleep 至 UTC 00:01）：17 分钟素材 v2+规划验收单次执行。完成后看 `/domain/.recorrect/step4_result.log`；若容器重启丢失按待办 1 手动跑。
- 无其他挂起后台任务；当前无在跑任务，服务全部健康（10-15 01:29 起新代码）。
