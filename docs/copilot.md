# 剪辑助手（Studio Copilot）— 执行型 AI Agent

工作台最左侧的「剪辑助手」是一个可执行工作区操作的对话式 Agent：理解自然语言指令 →
后端产出**白名单结构化动作 JSON** → 前端执行器落地 → 每个动作在对话里显示
已执行 / 等待确认 / 执行失败。

## 布局

四栏横向编辑器：**剪辑助手（可收起）｜项目素材｜播放器｜转写·字幕·效果**，底部时间线保留。
点击时间线片段、转写行、金句或 AI 推荐都会让播放器跳到对应时间。

## 接口

| 端点 | 说明 |
|---|---|
| `POST /studio/copilot` | 主入口。请求：`{message, project_id, current_time, duration, selected_segment_index, asr_segments, rendered_clips, timeline, insights, task, viewing}`；响应：`{copilot: {reply, engine, notice?, actions[]}}` |
| `GET /studio/copilot/history?project_id=` | 最近对话（Redis 缓存优先，回退 PostgreSQL） |
| `POST /studio/copilot/action-result` | 前端执行器回写动作结果（操作审计） |

## 白名单动作（前端执行器映射）

| 类型 | execution | 前端行为 |
|---|---|---|
| `jump_to_time` | run | `seek(start)` |
| `play` / `pause` | run | 播放 / 暂停 |
| `select_source_media` | run | 切回原始素材 |
| `select_rendered_clip` | run | 按 task_id+clip_id 切换成片（播放器+时间线） |
| `select_transcript_segment` | run | 选中转写行并跳转 |
| `highlight_time_range` | run | 时间线高亮区间并定位 |
| `extract_insights` | run | 走既有金句/关键词提取 |
| `start_auto_edit` | run | 走既有本机固定剪辑流程（队列 → 执行器 → video-brain） |
| `rerender_auto_edit` | run | 走既有复渲染 |
| `set_speaker` | **confirm** | 需用户点击「确认执行」后才改标签 |
| `export_active_clip` | **confirm** | 需用户点击「确认执行」后才导出 |

安全规则：动作类型白名单之外一律丢弃；时间/序号/成片 ID 在真实上下文中二次校验；
模型不能改写医学内容，不能凭声音推断说话人身份。

## 模型接入

与项目既有 LLM 体系一致（pydantic-ai）：`LLM=provider:model`，支持
`openai:*` / `google-gla:*` / `anthropic:*` / `ollama:*`，对应
`OPENAI_API_KEY` / `GOOGLE_API_KEY` / `ANTHROPIC_API_KEY` / `OLLAMA_BASE_URL`+`OLLAMA_API_KEY`。
**未配置或调用失败时自动降级为本机规则助手（engine=rules）并在界面标注，绝不 500。**
规则助手覆盖：跳转/播放/暂停、切换原片成片、选中转写、金句提取、片段推荐、
说话人标注（确认）、自动剪辑、复渲染、导出（确认）、主题/进度问答。

### DSH 模型接入（2026-10-10）

**结论**：当前 DSH 工作区没有向成员进程暴露可直接调用的模型端点或内嵌凭据
（`dsh-tool search` 无模型服务目录；shell 环境无任何 `*_API_KEY`/`*BASE_URL` 注入；
平台内置模型走受管路由，不属于可复用凭据）。因此模型层做成**任意 OpenAI 兼容端点
可插拔**：接入方式 = 在 `backend/.env` 配置以下变量（名称如下，值自行填写，勿入库）：

| 变量 | 说明 |
|---|---|
| `LLM` | `openai:<model>`（推荐，兼容一切 OpenAI 风格网关）/ `google-gla:*` / `anthropic:*` / `ollama:*` |
| `OPENAI_API_KEY` | 端点凭据 |
| `OPENAI_BASE_URL` | 非官方端点时填写（DSH 注入代理 / 内网网关 / Ollama 的 /v1） |
| `LLM_TIMEOUT_SECONDS` | 调用超时（默认 45s），超时/失败自动降级规则模式 |

一旦 DSH 平台注入标准 `OPENAI_API_KEY`/`OPENAI_BASE_URL`，后端进程会原生继承，
无需改代码即可直连。

### LLM 调用协议与加固

- 模型面向协议即需求示例形态：`{reply, actions:[{tool, arguments, execution, label}], requires_confirmation}`；
  后端归一化为前端现有协议（`type` + 扁平参数）后返回，**对外 API 与前端执行器无任何变更**。
- 加固：`asyncio.wait_for` 超时（`LLM_TIMEOUT_SECONDS`）、`output_retries=2`、
  错误脱敏（sk-/Bearer/key= 模式替换为「已脱敏」）、pydantic 严格校验 + 二次校验。
- 确认门控：模型产出的 `start_auto_edit` / `export_active_clip` / `set_speaker`
  无论模型如何声明一律 `execution=confirm`；未知工具与越界参数被丢弃并在 notice 说明。
- 上下文限长：只发送播放位置就近 ±6 段（最多 12 段、字段截断）、金句 ≤5、成片 ≤10，
  总上下文 ≤4000 字符，绝不全量发送转写。
- 降级：模型不可用/超时/格式错误 → HTTP 200 + `engine=rules` +
  notice「模型暂不可用，已切换为规则模式（原因已脱敏）」。

### 接入链路验证（2026-10-10，Mock 端点实测）

用本地 OpenAI 兼容 Mock（`backend/tests/mock_llm_server.py`，返回与真实模型同构响应）
实测 `engine=llm` 全链路：协议归一化、确认门控（start_auto_edit 强制 confirm）、
未知工具丢弃（shell_exec 被拒）、越界时间钳制、死端口降级（200 + rules）全部通过。
真实模型接入后请重复三条验收命令确认。

## 持久化

- PostgreSQL（`backend/migrations/004_studio_copilot.sql`，已并入 init.sql）：
  `studio_copilot_messages`（对话）、`studio_copilot_actions`（动作审计），按 `project_id` 隔离。
- Redis：每项目最近 40 条对话缓存（`studio:copilot:recent:{project_id}`，TTL 7 天）。
- 视频二进制不入库：成片仍走文件系统 + artifact 端点。

## 转写通道说明（公网隧道场景）

Cloudflare 免费隧道有 100s 代理超时，同步转写会被截断。已新增异步通道：

- `POST /studio/transcribe/start`：上传后立即返回 `{job_id}`，后台线程执行完整转写。
- `GET /studio/transcribe/jobs/{job_id}`：`{status, elapsed_seconds, result?, error?}`。

前端 `开始转写` 已默认走异步通道；原同步 `POST /studio/transcribe` 保留（本机直连场景无回归）。

**分片上传（2026-10-10 增补）**：实测发现经公网隧道上传 29MB 单请求仍会中途失败
（浏览器→CF 边缘上传慢且可能被边缘掐断，后端收不到任何字节）。已改为**分片上传**：

- `POST /studio/transcribe/chunk?upload_id=<32hex>&index=<n>`：单片原始字节 ≤8MB。
- `POST /studio/transcribe/assemble`：`{upload_id, filename, total}` 合并并启动转写 job。
- 前端切片 4MB/片、逐片重试（最多 3 次）、进度实时显示「正在上传视频（n/total 片）」。
- 实测：7 片 29MB 全部成功，合并后异步转写 160s 完成（同步模式此场景必然失败）。

## 验证记录（2026-10-10，DSH 容器内实测）

- 四栏布局经公网隧道页面渲染确认；助手栏可收起/展开。
- 「跳到 10 秒并播放」→ 播放器 `currentTime=10` 起播、`paused=false`（规则引擎）。
- 「暂停」→ `paused=true`。
- 「开始自动剪辑」→ 任务 `autoedit_f3a4377c6b26` 80 秒完成，2 条成片（血压管理 22.93s / 停药风险 16.96s）。
- 「打开第 1 个成片」→ 播放器切到 artifact 成片、导出按钮启用。
- 「当前成片的主题是什么」→「当前成片主题：血压管理、停药风险」。
- 未配置模型时全程 engine=rules + 界面提示，无 500；控制台 0 错误。
- PostgreSQL `studio_copilot_messages/actions` 与 Redis 会话缓存均已落数据。
