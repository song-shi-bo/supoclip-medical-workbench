# 项目资产模型与局部任务（Project → SourceVideo → Clip）

本轮把工作台的「成片卡片」事实源从**任务**搬到**项目 + 稳定 `clip_id`**，
并把「什么都跑 AUTO_EDIT」拆成有类型的局部任务。

## 1. 为什么改

改造前助手发起「重新制作封面」时走的是单切片复渲染：新建 `autoedit_xxxx` 任务 →
新任务有自己的 `output/` 目录 → 前端**按任务**聚合 clips → 左侧多出一张重复的
「剪辑成片 01」卡片；文案/字幕/时间线类修改也被粗暴地当成「重跑自动剪辑」
（3~4 分钟、磁盘增量 ~20MB）。

现在：一条 clip 在项目台账里只登记一次，封面/标题/字幕/时间线修改都只更新该条目，
**不新建 clip、不重跑 AUTO_EDIT、不新增卡片**。

## 2. 数据模型

```
Project（timeline_<hex>，磁盘目录 temp/studio-projects/<project_id>/）
 └── SourceVideo（project/raw/ 下的单副本原片；台账记 sha256）
      └── Clip（稳定 clip_id，例如 clip-d1bd3f-01）
           ├── render_task_id      首次生成成片的任务（**永不变**）
           ├── render_revision     画面重渲次数（封面更新不计数）
           ├── cover              封面资产：cover_asset_id / revision / history
           ├── subtitle_style     字幕样式配置
           ├── timeline           clip 内时间线片段
           └── task_history       该 clip 的局部任务历史
```

## 3. 落盘路径与 schema

所有路径都在 `STUDIO_TASKS_ROOT`（本机 `/domain/supoclip-medical-workbench/tasks`）之下。

### 3.1 项目台账 `projects/{project_id}/clips.json`

```jsonc
{
  "schema_version": 1,
  "project_id": "timeline_f69c37403877",
  "source_video": { "path": "media/autoedit_.../source.mp4", "sha256": "...",
                    "registered_at": "2026-10-12T08:36:17Z" },
  "updated_at": "...",
  "clip_order": ["clip-823060-01", "clip-d1bd3f-01"],   // 卡片顺序（登记顺序）
  "clips": {
    "clip-d1bd3f-01": {
      "clip_id": "clip-d1bd3f-01",
      "name": "剪辑成片 03",                 // 项目内唯一编号
      "title": "血压管理", "topic": "血压管理", "keywords": ["...", "..."],
      "render_task_id": "autoedit_d5583ca1d0c1",  // 稳定：首次生成成片的任务
      "source_start": 0.0, "source_end": 23.24, "duration": 23.24,
      "render_revision": 1,
      "render_history": [ { "render_revision": 2, "task_id": "edit_...", "kind": "UPDATE_TIMELINE",
                            "instruction": "...", "created_at": "..." } ],
      "cover": {
        "cover_asset_id": "cover-clip-d1bd3f-01-v2",  // 卡片/导出/详情统一引用
        "revision": 2,                                 // 最新版本号
        "active_revision": 2,                          // 当前展示的版本（撤销后更小）
        "style_id": "realframe_v2",
        "path": "output/autoedit_d5583ca1d0c1/clip-d1bd3f-01/cover.jpg",
        "sha256": "8a752f46...", "bytes": 111566,
        "updated_at": "...", "intro_burned": true, "kind": "generated",
        "history": [                                   // 每版完整快照，可撤销
          { "revision": 1, "cover_asset_id": "cover-clip-d1bd3f-01-v1",
            "path": "...", "task_id": "autoedit_d5583ca1d0c1", "style_id": null,
            "kind": "auto_edit", "sha256": "4cea0e21...", "created_at": "..." },
          { "revision": 2, "kind": "generated", "style_id": "realframe_v2",
            "sha256": "8a752f46...", "instruction": "重新制作封面，主题不超过 12 个字",
            "created_at": "..." }
        ]
      },
      "subtitle_style": { "updated_at": "...", "config": { "font_scale": 1.15 }, "task_id": "subtitle_..." },
      "timeline": { "updated_at": "...", "ranges": [ {"start": 0.0, "end": 8.0}, {"start": 10.0, "end": 23.24} ],
                    "task_id": "edit_...", "pending_rerender": true },
      "task_history": [
        { "task_id": "cover_6cda33df34f0", "task_type": "REGENERATE_COVER",
          "status": "completed", "stage": "已完成", "instruction": "...",
          "created_at": "...", "updated_at": "...", "summary": "封面已更新 · v2（realframe_v2）", "error": null }
      ]
    }
  }
}
```

写入一律走「临时文件 + `os.replace`」的原子替换，刷新/崩溃后读到的永远是完整 JSON。

### 3.2 局部任务

| 路径 | 内容 |
| --- | --- |
| `localtasks/{task_id}.json` | 任务全量记录（`task_type` / `payload` / `result` / `error`） |
| `status/{task_id}.json` | 状态镜像（与 AUTO_EDIT 共用同一张「任务表」，含 `task_type`） |
| `logs/local_tasks.jsonl` | 追加式任务日志（`grep REGENERATE_COVER` 即「只建了局部任务」的证据） |
| `in/` `processing/` `completed/` `failed/` | AUTO_EDIT 的原有文件队列（不变） |

### 3.3 成片目录内的封面资产与历史

```
output/{render_task_id}/{clip_id}/
├── clip_final.mp4              成片
├── cover.jpg                   封面资产（卡片/导出/详情指向它）
├── cover_layout.json           本次封面版面（含 style_id）
├── cover_replace.json          封面通道台账（baseline + records + active）
├── cover_backup/               基线备份（首次改动前的 cover.jpg / clip_final.mp4）
├── cover_revisions/rev-NNN/    历史版本快照（revision.json + 该版 cover.jpg[/clip_final.mp4]）
├── subtitle_style.json         UPDATE_SUBTITLE_STYLE 写下的样式配置
└── clip_timeline.json          UPDATE_TIMELINE 写下的时间线片段
```

> 历史版本故意放在 `cover_backup/` **之外**：`cover_backup/` 的语义是「首次改动前的
> 基线备份」，其条目数被既有契约当作「有没有叠加备份」的证据。

## 4. 任务类型与路由

| task_type | task_id 前缀 | 触发条件 | 效果 |
| --- | --- | --- | --- |
| `AUTO_EDIT` | `autoedit_` | 转写完成且**尚未首次生成成片**时点「开始自动剪辑」 | 既有固化链路整片生成；完成后登记 clip 卡片 |
| `REGENERATE_COVER` | `cover_` | 「重新制作封面 / 封面主题太长压缩到 12 字内 / 封面不要人物抠图」 | 只换该 clip 的封面图 + 0.8s 烧录片头；`cover_revision+1` |
| `UPDATE_TITLE` | `title_` | 「改标题 / 重新生成这一条成片的标题」 | 只更新标题/主题/关键词与封面文字（封面文字烧录在片头，故连同封面重出）；不重剪 |
| `UPDATE_SUBTITLE_STYLE` | `subtitle_` | 「字幕放大一点」 | 只写字幕样式配置 + 本 clip 的 `render_revision`；不重剪 |
| `UPDATE_TIMELINE` | `edit_` | 「删除第 8 秒到第 10 秒」 | 只改本 clip 的时间线片段；不影响原素材、其它 clip、转写 |

`AUTO_EDIT` **不能**通过局部任务入口创建：`POST /studio/projects/{p}/clips/{c}/tasks`
传 `task_type=AUTO_EDIT` 会返回 422。

### 4.1 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/studio/projects/{project_id}/clips` | 项目卡片列表（回填既有任务后返回，按 `clip_id` 去重） |
| POST | `/studio/projects/{project_id}/sync` | 显式回填 |
| POST | `/studio/projects/{project_id}/clips/{clip_id}/tasks` | 创建局部任务（202） |
| GET | `/studio/tasks/{task_id}` | 轮询任务进度与结果 |
| GET | `/studio/projects/{project_id}/tasks?clip_id=` | 项目任务表 |
| GET | `/studio/projects/{project_id}/clips/{clip_id}/cover-history` | 封面历史版本 |
| POST | `/studio/projects/{project_id}/clips/{clip_id}/cover/undo` | 撤销到指定版本（默认 v1） |

原有的 `.../clips/{clip_id}/cover`、`.../cover/adopt`、`.../cover/restore`、
`.../covers`、`.../transform`、`.../export-transformed` 全部保留不变。

## 5. 封面规则与片头拼接

- **独立资产**：`cover.jpg` 是 clip 的独立资产，通过 `cover_asset_id` 被卡片、导出面板、
  成片详情统一引用；成功后**原子更新** `cover_asset_id` 并 `cover_revision+1`。
- **不重渲 mp4**：封面只是缩略图/发布封面时只替换 `cover.jpg`。
- **片头烧录**：封面文字被烧录进片头约 0.8s。此时只重渲该 clip 的封面开头片段并与
  原视频其余部分拼接（`cover_replace.splice_intro`）：分辨率/fps/编码/音轨与原片严格
  一致，总时长与总帧数不变，**0.8s 之后逐帧不动**（由 `intro_qc` 与验收的逐帧 md5 双重证明）。
- **幂等**：同一 `(task, clip, style)` 重复执行不叠加备份、版本号不变。
- **可撤销**：`cover_revisions/rev-NNN/` 保留每版快照，UI 提供「撤销到 v1」。
- **文案约束（固化规则）**：主钩子总长 ≤12 个汉字、最多两行；超长时由
  `cover_replace.clamp_cover_copy` 压缩并在任务结果里回报 `copy_report.reason`。
- **风格**：`realframe_v2`（真实帧，无额外依赖）、`cutout_v3`（抠像，缺
  `/domain/.u2net/u2net.onnx` 时**显式报错** `E_CUTOUT_WEIGHTS_MISSING` + 安装指引，
  不静默降级）、`medical_big_text_v3`（legacy 冻结）。

## 6. 测试

```bash
cd backend && .venv/bin/python -m pytest tests/unit -q --no-cov
```

相关用例：`tests/unit/test_studio_project_store.py`、`test_studio_local_tasks.py`、
`test_cover_revisions.py`、`test_studio_copilot_routing.py`、`test_cover_replace.py`。
