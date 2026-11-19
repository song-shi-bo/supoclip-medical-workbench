# 字幕硬烧录规范（2026-10-29 固化；2026-11-09 烧录源升级精修稿优先；2026-11-18 阶段2 P1 升级）

> 依据用户两轮需求固化：①字幕切分逻辑（6 条规则）②字幕样式（去阴影只留描边）。
> 适用：supoclip-medical-workbench 成片硬字幕（SRT→ASS→ffmpeg subtitles 滤镜烧录链）。
> 不适用：studio 金黄字幕模板（`subtitle_compose`，工作台可配置的独立功能，样式另行管理）。

## 0. 烧录文本来源（2026-11-09 用户拍板：精修稿优先）

成片烧录字幕的文本来源按切片分两级（实现在 `src/clip_render._apply_refined_segments`）：

1. **精修稿优先**：该切片在 `clip_refinement.json`（两步制第二步产物）里
   `source=llm` 时，按 segment_id 把纠错分段文本替换为精修稿 `refined_text`
   （提示词B只允许删语气词/删无价值句，不润色不改写），再走 §1 的 cue 合成层
   烧录；`warnings.json.subtitle_provenance.subtitle_source = clip_refinement`、
   `refined_segment_count` = 实际替换为非空精修文本的分段数（整句删除的分段
   保持空文本，cue 层自然跳过，时间码不压缩）。
2. **如实回退**：精修产物缺失或该切片 fallback 时，仍按纠错转写渲染，
   `subtitle_source = corrected_transcript`，绝不冒充精修。

渲染指纹含精修身份串（`clip_render._refined_identity`）；指纹版本串自
2026-11-18 P2 起为 `subtitle-v3/1`（有词）/`subtitle-v3-base/1`（无词），
且 payload 含 `sub_style`（强调形态 + 可读性开关）——P1（cue 6s/药名/品牌
字体）与 P2（自适应底板/克制强调）变更烧录产物，旧 `words-align/1`、
`purify-concat/1`、`subtitle-v2*` 及更早缓存全部失效重渲。

## 1. 切分规则（按优先级从高到低，实现在 `src/subtitle_cueing.py`；输入为 §0 选定的分段文本）

1. **断句单位**：先按语音停顿（分段间隔 ≥ `SUBTITLE_SENTENCE_PAUSE_MS`）和标点（。？！，；）切句；超长句在语义短语边界（连接词/词边界）二次切分；**禁止按固定字数硬切**。
2. **单行长度**：竖屏每行 ≤ `SUBTITLE_MAX_LINE_CHARS`（默认 **16** 汉字），超长在最近语义边界换行，不在词中间断。
3. **不可拆分**：数字与单位完整（「0.5公分」「0.4的厚度」「0.4个毫米」）；标点不出现在行首；常用词不拆（因为/所以/这样/原则上等 `PROTECTED_WORDS`）；**药名不拆（2026-11-18 阶段2）**——常见药内置 starter 表 + `medical_lexicon` TSV `type=drug` 标准词全部进原子 token（长名优先匹配；别名/错写不入表；词典缺失只降级不阻断）。
4. **口语冗余**（`SUBTITLE_STRIP_FILLERS`，默认开）：去叠词（因为因为→因为、对对对→对；谢谢等例外保留）与填充词（是这样子的/来讲/「他要看XX呢」从句等 `_FILLER_SPANS` 显式清单）；时间码用原区间，不压缩。
5. **时间轴**：每条显示 ≥ `SUBTITLE_MIN_CUE_SECONDS`（默认 **0.8s**）；同一屏最多 `SUBTITLE_MAX_LINES`（默认 **2**）行；相邻条不重叠、无缝隙；**单条最长 `SUBTITLE_MAX_CUE_SECONDS`（2026-11-18 阶段2 起 12s 收紧为默认 6s，env >6 钳回 6）**——切分、装箱合并、整平缝隙延伸、短条合并四处全部受该上限约束（验收项「超过 6 秒的 cue：0」的机制保证）。
6. **标点风格**：`SUBTITLE_PUNCT_STYLE` = `omit`（默认，全省略）或 `keep`（全保留），全局二选一。

## 2. 烧录样式（固化在 `src/subtitle_cueing.py` BURN_* 常量与 ASS 头）

| 项 | 值 |
|---|---|
| 字色 | 白 `&H00FFFFFF` |
| 描边 | 纯黑 `&H00000000`，`Outline=2`（约 2px） |
| 阴影 | `Shadow=0`（**无**，任何偏移投影都不允许） |
| BorderStyle | `1`（描边模式） |
| PlayRes | 1080×1920（与视频 1:1，Outline 即字面像素） |
| 字体 | WenQuanYi Zen Hei（容器唯一 CJK 字体），字号 63px；**品牌字体管道（2026-11-18 阶段2）**：配置 `SUBTITLE_BRAND_FONT`（字体文件名/stem，经 font_registry 查找，只认 FONTS_DIR 子树与系统 CJK 白名单）后按 品牌字体 → MicrosoftYaHeiBold → 文泉驿 逐级回退，ASS 族名取字体内部真实族名（`resolve_burn_font` 同产 fontsdir）；0 字节占位文件自动跳过（现存 `fonts/MicrosoftYaHeiBold.ttc` 即占位）。**未配置时保持文泉驿，产物字节兼容** |
| 亮背景底板（P2） | **逐 cue 自适应**：被 `subtitle_readability` 判定复杂背景（字幕带亮度 ≥170 或边缘密度 ≥0.18）的 Dialogue 用第二样式 `SubBox`——BorderStyle=3 + Outline=4 + 半透明黑底板 `&H60000000`（alpha 0x60≈62% 不透明）；其余 cue 保持描边。样式行只在存在底板 cue 时输出（默认字节兼容）。判定失败/开关关闭 → 全描边 |
| 关键词强调（P2） | 每条 cue **最多 1 处**、单处 ≤6 字、数字/单位永不强调、跨行不强调；形态 `SUBTITLE_EMPHASIS=off|bold|color`（默认 bold=内联 `{\b1}`；color=低饱和暖金 `{\1c&H00D7FF&}`，禁红绿）；术语=medical_lexicon 全类型标准词 + 指南库快照 keywords（**只取关键词字段，条目正文绝不进字幕**）；SRT 副本保持纯净，标签只进 ASS |
| 位置 | 底部居中（Alignment=2，MarginV=360，2026-11-09 用户选定 C 档：下三分之一黄金位，避开平台底部交互 UI；原 160 过贴底）；**安全区校验（P2）**：`subtitle_readability.check_safe_zone` 按平台档案（`SUBTITLE_PLATFORM_PROFILE`，默认 douyin 底部 UI 16%）静态校验 MarginV 留白，结果写 `warnings.json.subtitle_safezone` |

**验收方法**：①纯白背景烧录帧（最严苛投影检测）②真实画面亮部抽帧，字幕区放大目检——白字周围只允许一圈干净纯黑描边，无任何灰色/黑色偏移残留。

## 3. 环境变量（作用于 `build_srt` / cue 合成层）

```
SUBTITLE_MAX_LINE_CHARS=16      # 每行最大汉字数
SUBTITLE_MAX_LINES=2            # 每屏最大行数
SUBTITLE_MIN_CUE_SECONDS=0.8    # 单条最短显示时长
SUBTITLE_MAX_CUE_SECONDS=6      # 单条最长显示时长（2026-11-18 阶段2：12→6；env >6 钳回 6）
SUBTITLE_STRIP_FILLERS=true     # 口语冗余清理开关
SUBTITLE_PUNCT_STYLE=omit       # omit=全省略 | keep=全保留
SUBTITLE_SENTENCE_PAUSE_MS=500  # 语音停顿判定阈值
SUBTITLE_GAP_EXTEND_MS=800      # 长停顿前条最大延伸（延伸后仍受 6s 上限约束）
SUBTITLE_CUE_GAP_SPLIT_S=1.5    # 词间停顿切分阈值
SUBTITLE_BRAND_FONT=            # 品牌字体文件名（空=文泉驿字节兼容默认）
SUBTITLE_READABILITY=on         # 亮背景自适应底板开关（off=全描边历史行为）
SUBTITLE_READABILITY_LUMA=170   # 字幕带亮度阈值（≥ 判复杂背景 → 底板）
SUBTITLE_READABILITY_EDGE=0.18  # 字幕带边缘密度阈值（≥ 判复杂背景 → 底板）
SUBTITLE_READABILITY_MAX_FRAMES=24  # 每切片抽帧上限（超出均匀抽样，未采样 cue 继承相邻判定）
SUBTITLE_EMPHASIS=bold          # 关键词强调形态：off | bold | color
SUBTITLE_PLATFORM_PROFILE=douyin  # 安全区档案：douyin | shipinhao | baijiahao | default
SUBTITLE_HARDBURN=true          # 渲染时直接烧字进成片（false=紧急停机：有cue切片硬失败，见§4）
BURN_FONTSDIR=/usr/share/fonts/truetype/wqy   # 烧录字体目录（容器默认已带文泉驿）
```

## 4. 使用入口与生效方式

- **渲染即烧字（默认，2026-10-29 固化）**：worker 渲染成片时（`clip_render.render_one`），同一遍 ffmpeg 在 scale 之后追加 `subtitles=<固化样式 ASS>`——**成片产出即带字幕**（`subtitle_mode=burned`），并写出 `subtitles.ass` 供审计。工作台预览/下载/抖音投递所见即所得。
  - studio 导出（export-transformed）对 burned 底片自动跳过二次烧录；字幕编辑器人工修订（rev-NNN ASS）只对 sidecar 底片生效，burned 底片以画面为准。
  - 紧急停机开关：`SUBTITLE_HARDBURN=false` 不再产出 sidecar_only 底片——有 cue 的切片直接 `ClipRenderError`（2026-11-04 固化契约：切片预览必须显示字幕）。发布层另有纵深防御：`subtitle_mode=sidecar_only` 的成片禁止登记/发布；旧 sidecar_only 缓存命中时视为过期强制重渲染。
- **运维 CLI**：`cd backend && .venv/bin/python scripts/burn_subtitles.py <子命令>`（`ass` / `burn` / `regen-srt` / `meta` / `auto`）。用于：存量 sidecar 成片补烧、样式调整后重烧、worker 重渲染后的补烧（`auto` 一条龙含元数据同步）。
- **底片要求**：烧录必须以**无字幕的干净渲染**为底；对已烧字视频再烧会二次压字。
- **元数据契约**：烧录后同步 ①`warnings.json`：`subtitle_mode=burned` + `srt_sha256` + `subtitle_hard_burn`（2026-11-18 起含 `fontsdir`）+ **`subtitle_qc`**（阶段2 验收块：sync p95/行宽行数/时长违例/词缺失占比，只报告不拦截）+ `subtitle_provenance`；②`cover_realframe_v2.json` 账本 `intro_clip_final_sha256`（防「应用封面」重复烧 0.8s 片头）。`meta` 子命令自动完成。

## 5. 历史变更

- 2026-11-18（P2）：**可读性与克制强调**——①亮背景自适应底板：新 `subtitle_readability.py` 渲染前逐 cue 采样源片字幕带（与烧录位置同比例坐标）亮度/边缘密度，判复杂背景的 cue 用 SubBox 半透明底板（抽帧 ≤24 帧/切片，失败/关开关全描边）；②关键词克制强调：新 `subtitle_emphasis.py`（每 cue ≤1 处 ≤6 字、数字单位永不强调、bold 默认/color 暖金可选）；③安全区档案静态校验（douyin 等 4 档）写 `subtitle_safezone`；④`subtitle_qc.readability` 接入 pre_mitigation_readability 与底板 cue 数；指纹 bump `subtitle-v3/1`（payload 增 `sub_style`）。**P2 验收口径**：「复杂背景可读率 ≥98%」= pre_mitigation 低读 cue 全部获得底板补偿（post-mitigation 100% 达标），raw 比例如实透出不粉饰。
- 2026-11-18（P1）：**阶段2 P1 字幕升级**（方案《剪辑工作台阶段2优化方案-2026-11-18》）——①cue 上限 12s→6s（验收「>6s cue = 0」，切分/装箱/整平/合并四处护栏）；②药名保护词（内置 starter + medical_lexicon type=drug，原子 token 不拆）；③品牌字体解析链 `SUBTITLE_BRAND_FONT`（未配置保持文泉驿字节兼容；发现现存 `fonts/MicrosoftYaHeiBold.ttc` 为 0 字节占位的历史遗留问题，解析链自动跳过）；④新 `subtitle_qc.py`：warnings.json `subtitle_qc` 验收块（p95≤200ms 目标、行宽/行数/时长违例、words_missing 占比），指纹 bump `subtitle-v2/1`（有词）/`subtitle-v2-base/1`（无词），旧缓存全量失效。2026-10-29 验收 case 期望同步更新（7.8s 双行 cue 按新规合法拆为两条单行）。

- 2026-11-09：**烧录源升级「精修稿优先」**（用户提问「字幕为什么还有语气词」后拍板）——source=llm 的切片烧精修稿（语气词/无价值句已删），缺失/fallback 如实回退纠错转写；指纹版本 refined-burn/1 全量失效旧缓存，重新剪辑后生效。同日右侧切片转写去重：切片视图只保留「切片精修文案」面板（医生/患者标注），删除下方转写分段列表（用户指令「切片转写只保留一个板块」）。
- 2026-10-29 上午：cue 合成层上线（切分 6 规则），6 成片首次按新逻辑烧录。
- 2026-10-29 下午：样式修复——ffmpeg 默认 `Shadow=2`（≈13px 偏移投影）改为显式 ASS `Shadow=0`；描边统一 2px；改前/改后 Style 行与白底、亮部双场景对比帧存档于执行记录 §15.2。
- 2026-11-09：位置校准 MarginV 160→360（用户从 160/260/360/460 四档真实帧预览中选定 C 档 360——下三分之一黄金位，避开平台底部交互 UI；样式其余项不动）。
- 2026-11-04：固化契约升级（用户指令「以后切片预览必须显示字幕」）——`SUBTITLE_HARDBURN=false` 由静默降级改为硬失败；sidecar_only 缓存不再复用；发布层拒绝 sidecar_only 成片。回溯背景：任务 67137543 首跑（10-29 06:59）以 sidecar_only 发布，二跑（13:3x）burned 产物滞留暂存区未上传，导致工台预览无字幕；6 条成片已按 burned 契约原地重渲染修复。
