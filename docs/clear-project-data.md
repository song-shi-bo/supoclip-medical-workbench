# 清空项目任务数据（Clear project data）

> 目的：让工作台能**显式**回到干净起点 —— 删掉当前项目的任务卡片与产物。
> 刻意**不做**「刷新即删」：台账与任务记录都是落盘持久数据，刷新触发会把一次误按
> F5 升级成不可撤销的数据丢失。

## 用户怎么用

1. 顶栏右侧出现垃圾桶图标（**仅当当前项目有任务卡片时显示**，`card_count > 0`）；
2. 点击 → 弹二次确认卡，写清「将删几张卡、几个任务、约释放多少空间」与
   「源片与已完成的转写会保留」；
3. 点「确认清空」才真正执行；点「取消」不发任何删除请求。

## 契约

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/studio/projects/{project_id}/data` | 只读预览：`card_count`、`applied_task_count`、`reclaimable_bytes`、`active_task_ids`、`keep`（保留项） |
| `POST` | `/studio/projects/{project_id}/data/clear` | 执行清空。请求体 `{confirm: bool, expected_card_count: int, purge: bool = true}` |

`expected_card_count` 必填且必须等于**当前**台账卡片数 —— 这是防误删闸门，也是幂等键：
页面看到 2 张卡、确认时其实已经出了第 3 张 → `409`，前端提示刷新后重试。

### 错误码

| 码 | HTTP | 含义 |
|---|---|---|
| `E_PURGE_CONFIRM_REQUIRED` | 422 | 未带 `confirm=true` |
| `E_PURGE_CARD_COUNT_MISMATCH` | 409 | 卡片数已变化（响应含 `current_card_count`） |
| `E_PURGE_ACTIVE_TASKS` | 409 | 还有未完成任务（响应含 `active_task_ids`） |
| `E_PURGE_FAILED` | 422 | 执行失败，**已还原全部改动**、旧台账未动 |
| `E_PURGE_RESTORE_FAILED` | 422 | 失败且部分文件未能还原（响应含 `unrestored` 路径清单，需人工介入） |

## 删什么 / 留什么

| 删除 | 保留 |
|---|---|
| 该项目引用到的任务 JSON（`completed/`、`failed/`、`in/`、`processing/`） | `projects/<project_id>/clips.json`（**台账本体**：项目仍在，只是卡片清空） |
| `status/<task_id>.json` | `transcripts/`（corrected transcript，重跑无需再做 ASR/纠错） |
| `output/<task_id>/`（含 `clip_final.mp4`、封面历史 `cover_revisions/`、`clip_plan.json`、`workflow_snapshot.json`） | `media/<源片任务>/source.mp4`（**源片 = 项目唯一输入**，删了就无法重新剪辑） |
| `localtasks/<task_id>.json` 与 `<task_id>/` 快照目录 | 其他项目的一切 |
| `logs/local_tasks.jsonl` 中属于这些任务的行 | — |

源片保护有两个来源合并判定：台账 `source_video` 与任务 JSON 的 `raw_video_path`
（存量项目常见只写了后者）。

## 为什么必须删任务 JSON 而不是只清台账

`studio_project_store.sync_from_tasks` 会在**每次读取**时扫描
`tasks/{completed,processing,in,failed}/autoedit_*.json` 回填台账。只清
`clips.json` 而不删任务 JSON，下一次加载卡片会**原样复活**。清理因此从任务 JSON
下手，台账是最后一步（有专门的单元测试钉住这一点）。

## 安全设计

1. **只在终态执行**：`in/`、`processing/` 里出现该项目的任务 JSON，或状态文件
   是非终态（`queued`/`running`/`transcribing`/…）一律拒绝 —— 删活动任务会让
   worker 回写失败。
2. **卡片基数校验**：见上文 `expected_card_count`。
3. **两阶段提交**：先把目标 `os.replace` 到 `tasks/.purge-trash/<ts>-<project_id>/`，
   全部搬完才写台账；任何一步失败就**反向搬回**并保持旧台账不动。写台账成功后才
   物理删除回收站（`purge=false` 可保留用于排障）。已存在且被跳过的目标、
   `os.replace` 的静默覆盖，使清理**幂等**：重复执行不报错、不留半删状态。
4. **不可撤销**：默认 `purge=true` 是真删（磁盘会释放）。

## 代码位置

| 文件 | 作用 |
|---|---|
| `backend/src/studio_project_purge.py` | 清理逻辑（`summarize` / `clear_project_tasks` / `source_media_dirs`） |
| `backend/src/studio_project_store.py` | `card_clip_ids` / `card_task_ids`（清理用的只读助手） |
| `backend/src/api/routes/media.py` | 两个端点 + `_purge_error` 错误映射 |
| `frontend/src/app/studio/page.tsx` | 顶栏按钮、确认卡、`requestClearProjectData` / `confirmClearProjectData` |
| `frontend/src/app/studio/studio.css` | `.clear-confirm-*` / `.top-icon.danger` |
| `backend/tests/unit/test_studio_project_purge.py` | 13 个清理用例（含"卡片不复活"与失败还原） |
| `backend/tests/unit/test_studio_project_data_endpoints.py` | 8 个端点/错误映射用例 |
| `frontend/src/app/studio/studio-clear-data.test.tsx` | 4 个交互用例（预览 / 取消 / 确认 / 409） |

## 未做（有意）

* **刷新即删**：见开头，属于误删入口。
* **自动清理回收站**：`purge=true` 已在同一次调用里清掉；没有后台 GC 任务。
* **删除项目本身**：只清卡片与产物，`projects/<id>/` 与源片、转写都在。
