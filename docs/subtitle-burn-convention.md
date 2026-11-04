# 字幕硬烧录规范（2026-10-29 固化）

> 依据用户两轮需求固化：①字幕切分逻辑（6 条规则）②字幕样式（去阴影只留描边）。
> 适用：supoclip-medical-workbench 成片硬字幕（SRT→ASS→ffmpeg subtitles 滤镜烧录链）。
> 不适用：studio 金黄字幕模板（`subtitle_compose`，工作台可配置的独立功能，样式另行管理）。

## 1. 切分规则（按优先级从高到低，实现在 `src/subtitle_cueing.py`）

1. **断句单位**：先按语音停顿（分段间隔 ≥ `SUBTITLE_SENTENCE_PAUSE_MS`）和标点（。？！，；）切句；超长句在语义短语边界（连接词/词边界）二次切分；**禁止按固定字数硬切**。
2. **单行长度**：竖屏每行 ≤ `SUBTITLE_MAX_LINE_CHARS`（默认 **16** 汉字），超长在最近语义边界换行，不在词中间断。
3. **不可拆分**：数字与单位完整（「0.5公分」「0.4的厚度」「0.4个毫米」）；标点不出现在行首；常用词不拆（因为/所以/这样/原则上等 `PROTECTED_WORDS`）。
4. **口语冗余**（`SUBTITLE_STRIP_FILLERS`，默认开）：去叠词（因为因为→因为、对对对→对；谢谢等例外保留）与填充词（是这样子的/来讲/「他要看XX呢」从句等 `_FILLER_SPANS` 显式清单）；时间码用原区间，不压缩。
5. **时间轴**：每条显示 ≥ `SUBTITLE_MIN_CUE_SECONDS`（默认 **0.8s**）；同一屏最多 `SUBTITLE_MAX_LINES`（默认 **2**）行；相邻条不重叠、无缝隙。
6. **标点风格**：`SUBTITLE_PUNCT_STYLE` = `omit`（默认，全省略）或 `keep`（全保留），全局二选一。

## 2. 烧录样式（固化在 `src/subtitle_cueing.py` BURN_* 常量与 ASS 头）

| 项 | 值 |
|---|---|
| 字色 | 白 `&H00FFFFFF` |
| 描边 | 纯黑 `&H00000000`，`Outline=2`（约 2px） |
| 阴影 | `Shadow=0`（**无**，任何偏移投影都不允许） |
| BorderStyle | `1`（描边模式） |
| PlayRes | 1080×1920（与视频 1:1，Outline 即字面像素） |
| 字体 | WenQuanYi Zen Hei（容器唯一 CJK 字体），字号 63px |
| 位置 | 底部居中（Alignment=2，MarginV=160） |

**验收方法**：①纯白背景烧录帧（最严苛投影检测）②真实画面亮部抽帧，字幕区放大目检——白字周围只允许一圈干净纯黑描边，无任何灰色/黑色偏移残留。

## 3. 环境变量（作用于 `build_srt` / cue 合成层）

```
SUBTITLE_MAX_LINE_CHARS=16      # 每行最大汉字数
SUBTITLE_MAX_LINES=2            # 每屏最大行数
SUBTITLE_MIN_CUE_SECONDS=0.8    # 单条最短显示时长
SUBTITLE_STRIP_FILLERS=true     # 口语冗余清理开关
SUBTITLE_PUNCT_STYLE=omit       # omit=全省略 | keep=全保留
SUBTITLE_SENTENCE_PAUSE_MS=500  # 语音停顿判定阈值
SUBTITLE_HARDBURN=true          # 渲染时直接烧字进成片（false=紧急停机：有cue切片硬失败，见§4）
BURN_FONTSDIR=/usr/share/fonts/truetype/wqy   # 烧录字体目录（容器默认已带文泉驿）
```

## 4. 使用入口与生效方式

- **渲染即烧字（默认，2026-10-29 固化）**：worker 渲染成片时（`clip_render.render_one`），同一遍 ffmpeg 在 scale 之后追加 `subtitles=<固化样式 ASS>`——**成片产出即带字幕**（`subtitle_mode=burned`），并写出 `subtitles.ass` 供审计。工作台预览/下载/抖音投递所见即所得。
  - studio 导出（export-transformed）对 burned 底片自动跳过二次烧录；字幕编辑器人工修订（rev-NNN ASS）只对 sidecar 底片生效，burned 底片以画面为准。
  - 紧急停机开关：`SUBTITLE_HARDBURN=false` 不再产出 sidecar_only 底片——有 cue 的切片直接 `ClipRenderError`（2026-11-04 固化契约：切片预览必须显示字幕）。发布层另有纵深防御：`subtitle_mode=sidecar_only` 的成片禁止登记/发布；旧 sidecar_only 缓存命中时视为过期强制重渲染。
- **运维 CLI**：`cd backend && .venv/bin/python scripts/burn_subtitles.py <子命令>`（`ass` / `burn` / `regen-srt` / `meta` / `auto`）。用于：存量 sidecar 成片补烧、样式调整后重烧、worker 重渲染后的补烧（`auto` 一条龙含元数据同步）。
- **底片要求**：烧录必须以**无字幕的干净渲染**为底；对已烧字视频再烧会二次压字。
- **元数据契约**：烧录后同步 ①`warnings.json`：`subtitle_mode=burned` + `srt_sha256` + `subtitle_hard_burn`；②`cover_realframe_v2.json` 账本 `intro_clip_final_sha256`（防「应用封面」重复烧 0.8s 片头）。`meta` 子命令自动完成。

## 5. 历史变更

- 2026-10-29 上午：cue 合成层上线（切分 6 规则），6 成片首次按新逻辑烧录。
- 2026-10-29 下午：样式修复——ffmpeg 默认 `Shadow=2`（≈13px 偏移投影）改为显式 ASS `Shadow=0`；描边统一 2px；改前/改后 Style 行与白底、亮部双场景对比帧存档于执行记录 §15.2。
- 2026-11-04：固化契约升级（用户指令「以后切片预览必须显示字幕」）——`SUBTITLE_HARDBURN=false` 由静默降级改为硬失败；sidecar_only 缓存不再复用；发布层拒绝 sidecar_only 成片。回溯背景：任务 67137543 首跑（10-29 06:59）以 sidecar_only 发布，二跑（13:3x）burned 产物滞留暂存区未上传，导致工台预览无字幕；6 条成片已按 burned 契约原地重渲染修复。
