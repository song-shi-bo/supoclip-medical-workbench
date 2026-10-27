# Inspector Phase 1 — 冻结契约（后端模块 / HTTP / 前端）

> 本文件是「右侧 Inspector 真实编辑能力 Phase 1」的**唯一接口契约**。实现者按本文件的
> 名称、签名、路径、字段实现；不得改 `/domain/video-brain/**`、不得改
> `frontend/src/lib/clip-transform.ts`(+test)、不得让任何局部操作触发 AUTO_EDIT。
> Phase 2（FFmpeg 按配置真渲染）**不做**，但配置/版本/UI 标注必须落到位。

## 0. 硬边界

- 只用工作台侧代码：`backend/src/**`（除 `video-brain`）、`frontend/src/**`。
- 新增任务类型一律**局部通道**，`FULL_PIPELINE_TYPES` 仍只含 `AUTO_EDIT`。
- 任何写盘都用原子写（`_atomic_json` 风格：临时文件 + `os.replace`）。
- 缺字段向后兼容：老 `clips.json` 读出来必须能补齐默认值，`card_view` 形状不破坏。

## 1. 路径约定（全部在 `STUDIO_TASKS_ROOT` 下）

| 用途 | 路径 |
|---|---|
| 项目台账 | `projects/{project_id}/clips.json` |
| 源片级（未选成片）配置 | `clips.json` 顶层 `source_config` |
| 转写修订层 | `projects/{project_id}/transcripts/{clip_id}.json` |
| 成片资产目录 | `output/{render_task_id}/{clip_id}/` |
| 字幕版本 | `output/{render_task_id}/{clip_id}/subtitles/rev-{NNN}/{clip_id}.ass` + `.srt` + `meta.json` |
| BGM 资产 | `output/{render_task_id}/{clip_id}/audio/bgm-{sha8}.{ext}` |
| 局部任务 | `localtasks/{task_id}.json` / `status/{task_id}.json` |

`clip_id == "source"` 是**保留哨兵**，表示「绑定当前项目/源片」，配置读写落在
`clips.json` 顶层 `source_config`（不要求有已登记 clip）。其余 clip_id 走既有校验。

## 2. clip 记录扩展（`projects/{project_id}/clips.json`）

在既有条目上**追加**下列键（缺省按默认值读，写入时补齐）：

```jsonc
{
  "clip_id": "clip-xxxx-01",
  "title": "…",                        // 既有
  "transcript_revision": 0,            // 有效转写修订号（由 transcript_revisions 维护）
  "subtitle_config": {
    "font_size_px": 62,                // 1080x1920 坐标系
    "font_family": "",                 // "" = video-brain 默认字体（FONT_NAME）
    "text_color": "#FFFFFF",
    "outline_color": "#101010",
    "outline_width": 4,                // 0..12
    "background": {"enabled": false, "color": "#000000", "opacity": 0.5},
    "position": {"x_pct": 50.0, "y_pct": 78.125},   // 底部居中基准；y_pct=78.125 → MarginV=420
    "word_highlight": false,
    "max_chars_per_line": 18
  },
  "subtitle_overrides": [],            // [{segment_id, start?, end?, text, updated_at}]
  "cover_asset_id": null,              // **投影**：读自 entry["cover"].cover_asset_id（不落盘副本）
  "cover_revision": 1,                 // **投影**：读自 entry["cover"].revision
  "transform": null,                   // clip_transform.ClipTransform.to_payload() 或 null
  "audio_tracks": [],                  // 见 §4
  "voice_volume": 1.0,                 // 0..1
  "muted": false,
  "effects": {
    "sticker": {"enabled": false, "position": "left"},          // left|right|bottom-left|bottom-right
    "end_card": {"enabled": false, "text": "", "duration_s": 3.0},
    "filter": {"name": "none", "strength": 0.0},                // none|warm|cool|bw, 0..1
    "transition": {"enabled": false, "type": "none"},           // none|fade
    "export_resolution": "1080x1920"                            // 1080x1920|1080x1080|1920x1080
  },
  "render_revision": 1,                // 既有
  "task_history": []                   // 既有
}
```

默认常量放在 `clip_config.py`，`studio_project_store` 通过 `clip_config.with_clip_defaults(entry)`
投影（**避免循环导入**：`clip_config.py` 不得 import `studio_project_store`；
`studio_project_store.py` 可以在函数内**延迟 import** `clip_config`）。

## 3. 模块 API

### 3.1 `backend/src/clip_config.py`（新增）

```python
class ClipConfigError(ValueError):
    code: str          # E_CONFIG_INVALID / E_CONFIG_UNKNOWN_SECTION / E_CONFIG_UNKNOWN_CLIP
    message: str

DEFAULT_SUBTITLE_CONFIG: dict
DEFAULT_EFFECTS: dict
SUBTITLE_POSITIONS = ("left", "right", "bottom-left", "bottom-right")   # sticker 用
FILTER_NAMES = ("none", "warm", "cool", "bw")
TRANSITION_TYPES = ("none", "fade")
EXPORT_RESOLUTIONS = ("1080x1920", "1080x1080", "1920x1080")
PATCH_SECTIONS = ("title", "subtitle_config", "subtitle_overrides", "transform",
                  "audio_tracks", "voice_volume", "muted", "effects")

def normalize_subtitle_config(raw: Any) -> dict          # 合并默认 + 校验
def normalize_effects(raw: Any) -> dict
def normalize_audio_tracks(raw: Any) -> list[dict]       # 仅结构/范围校验，不碰文件
def normalize_patch(patch: Any) -> dict                  # 校验后返回规范化 patch；未知键/类型错 → ClipConfigError
def apply_patch(current: dict, patch: dict) -> tuple[dict, list[str]]
    # 返回 (新配置, 真正发生变化的 section 列表)；值相等则不算变化
def needs_render_bump(changed_sections: list[str]) -> bool
    # subtitle_config/subtitle_overrides/transform/audio_tracks/voice_volume/muted/effects → True
def config_view(project_id: str, clip_id: str) -> dict
    # {"project_id","clip_id","binding":"clip"|"source","config":{...},
    #  "revisions":{"transcript_revision":int,"render_revision":int},
    #  "sources":{section: "user"|"project"|"skill_default"}}
def update_config(project_id: str, clip_id: str, patch: dict) -> dict
    # 部分更新：校验 → 应用 → 只在真有变化时 bump render_revision →
    # **同步 mirror**（见下）→ 追加 task_history（见 §5 映射）→ 返回
    # {"changed":[...], "config":{...}, "revisions":{...}, "task":{...},
    #  "render_deferred": True, "render_note": "配置与版本已保存；按配置真渲染属 Phase 2"}
def render_config_summary(project_id: str, clip_id: str) -> dict
    # {"project_id","clip_id","binding","effective":[{"key","value","source"}],
    #  "sources":{...},"export":{"resolution","deferred":True,
    #  "note":"导出渲染（外部 ASS 烧录 / BGM 混音 / 滤镜 / 转场 / 非 9:16）属 Phase 2"},
    #  "config":{...}}
def set_transcript_revision(project_id: str, clip_id: str, revision: int) -> dict
```

`update_config` 的 **mirror** 规则（保持既有语义不变）：
- `transform` 变化时同时调用
  `studio_auto_edit_bridge.save_clip_transform(render_task_id, clip_id, transform)`（`None` = 清除）。
- `subtitle_config`/`subtitle_overrides` 变化时**不**自动生成 ASS/SRT（字幕版由 §3.3 或局部任务生成）。

非 clip 绑定（`clip_id == "source"`）时 `update_config` 只写 `source_config`，`revisions` 用
`{"transcript_revision":0,"render_revision":0}`，不写 task_history。

### 3.2 `backend/src/transcript_revisions.py`（新增）

原始 ASR 与纠错稿**不可变**；所有修改以修订（revision）叠加。

```python
class TranscriptRevisionError(ValueError):
    code: str   # E_TRANSCRIPT_NOT_SEEDED / E_TRANSCRIPT_REVISION_UNKNOWN / E_TRANSCRIPT_CHANGES_INVALID

def document_path(project_id: str, clip_id: str) -> Path
def load_document(project_id: str, clip_id: str) -> dict | None
def seed_document(project_id: str, clip_id: str, *,
                  raw_segments: list[dict], corrected_segments: list[dict] | None = None,
                  raw_text: str | None = None, corrected_text: str | None = None,
                  author: str = "system") -> dict
    # 幂等：已存在且 raw_segments 非空则原样返回（绝不覆盖 raw/corrected）
def ensure_document(project_id: str, clip_id: str, *, segments: list[dict]) -> dict
    # 便捷入口：以成片 segments 作为 raw 基线的种子（若未 seed）
def effective_segments(doc: dict, variant: str = "effective") -> list[dict]
    # 纯函数。variant ∈ raw|corrected|effective。
    # effective = (corrected_segments or raw_segments) 叠加 approved_revision 的 changes。
    # 返回 [{segment_id,start,end,text,speaker}]，按 start 排序。
def add_revision(project_id: str, clip_id: str, *, changes: list[dict],
                 reason: str | None = None, author: str = "editor") -> dict
    # changes: [{segment_id, before?, after?, speaker_before?, speaker_after?}]
    # 追加 revision=N+1，写 approved_revision=N+1；同步 store 的 transcript_revision
def rollback(project_id: str, clip_id: str, revision: int) -> dict   # 设 approved_revision
def list_revisions(project_id: str, clip_id: str) -> list[dict]
def document_view(project_id: str, clip_id: str, *, seed_segments: list[dict] | None = None) -> dict
    # {"project_id","clip_id","transcript_revision","approved_revision",
    #  "raw_text","corrected_text","effective_text","revisions":[...],
    #  "raw_segments":[...],"corrected_segments":[...],"effective_segments":[...]}
```

落盘 schema：

```jsonc
{"schema_version":1,"project_id":"…","clip_id":"…",
 "raw_text":"…","corrected_text":"…",
 "raw_segments":[{"segment_id":"seg-1","start":0.0,"end":2.0,"text":"…","speaker":"未标注"}],
 "corrected_segments":[…],
 "revisions":[{"revision":1,"author":"editor","created_at":"…","reason":"…",
   "changes":[{"segment_id":"seg-1","before":"旧","after":"新",
               "speaker_before":"未标注","speaker_after":"医生"}]}],
 "approved_revision":1,"updated_at":"…"}
```

变体语义（三态切换）：
- `raw` = `raw_segments`（ASR 原文，永不改）
- `corrected` = `corrected_segments`（纠错稿，永不改）
- `effective` = corrected（无则 raw）叠加 approved revision 的逐段 `after`/`speaker_after`

### 3.3 `backend/src/subtitle_compose.py`（新增）

**不 import clip_config**（配置以 plain dict 传入）。

```python
DEFAULT_WIDTH, DEFAULT_HEIGHT = 1080, 1920
DEFAULT_Y_PCT = 78.125          # → MarginV 420，与 video-brain app/subtitles.py 一致
DEFAULT_FONT_SIZE = 62
DEFAULT_FONT_NAME = "Noto Sans CJK SC"   # 仅当 config.font_family 为空时用于 ASS 头
SUBTITLE_DIRNAME = "subtitles"
META_FILENAME = "meta.json"

class SubtitleComposeError(ValueError):
    code: str   # E_SUBTITLE_CONFIG_INVALID / E_SUBTITLE_NO_CUES

def percent_to_pixels(position: dict, width: int = DEFAULT_WIDTH,
                      height: int = DEFAULT_HEIGHT) -> dict
    # {"x": round(width*x_pct/100), "bottom": round(height*(100-y_pct)/100)}
    # 与前端预览共用同一换算（前端 TS 侧实现同一公式）
def build_cues(segments: list[dict], *, overrides: list[dict] | None = None) -> list[dict]
    # segments: [{segment_id,start,end,text,speaker}] → cues
    # [{index,segment_id,start,end,text,speaker}]，按 start 排序，空文本跳过
    # overrides 按 segment_id 覆盖 text/start/end；带 start/end 且无匹配 segment_id 的
    # override 作为额外 cue 追加
def ass_content(cues: list[dict], config: dict, width: int = DEFAULT_WIDTH,
                height: int = DEFAULT_HEIGHT) -> str
def srt_content(cues: list[dict]) -> str
def revision_dir(clip_dir: Path, revision: int) -> Path      # clip_dir/subtitles/rev-{NNN}
def list_revisions(clip_dir: Path) -> list[dict]             # 读 meta.json，按 revision 升序
def next_revision(clip_dir: Path) -> int
def compose(clip_dir: Path, *, clip_id: str, cues: list[dict], config: dict,
            revision: int | None = None, width: int = DEFAULT_WIDTH,
            height: int = DEFAULT_HEIGHT, task_id: str | None = None) -> dict
    # 原子写 rev-{NNN}/{clip}.ass + .srt + meta.json；
    # 返回 {"revision","dir","ass_path","srt_path","cue_count","config_hash",
    #       "font_size_px","margin_v","style_id":"workbench_v1"}
```

ASS 生成约定（必须与 `video-brain/app/subtitles.py` 兼容，保证 Phase 2 可直接喂渲染）：
- `ScriptType: v4.00+`，`PlayResX/PlayResY` = 1080/1920，`WrapStyle: 0`
- 单一样式名 `Speak`，`Alignment 2`（底部居中），`BorderStyle 1`，`Outline=outline_width`，`Shadow 1`
- `MarginV = percent_to_pixels(position, width, height)["bottom"]`（默认 420）
- `MarginL/MarginR = round(width * x_pct/100)` 之外的对称留白：默认 70
- 时间格式 `H:MM:SS.cc`（两位小数），文本换行转空格
- `word_highlight=false` → 纯 `Dialogue: 0,start,end,Speak,,0,0,0,,text`
- `word_highlight=true` → 逐词平均分配时长的 `{\k<cs>}` 卡拉OK 标签（`SecondaryColour` 生效），
  并在 ASS 里保留 `\an2`；文档注释说明「无词级时间戳时按 cue 时长均分」
- `background.enabled=true` → `BorderStyle=3` + `BackColour` 带透明度（`&HAABBGGRR`）
- 颜色 `#RRGGBB` → ASS `&H00BBGGRR`

### 3.4 `backend/src/clip_audio.py`（新增）

```python
AUDIO_DIRNAME = "audio"
BGM_KINDS = ("bgm",)
MAX_PEAKS = 800
SUPPORTED_BGM_EXTENSIONS = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac")

class ClipAudioError(ValueError):
    code: str   # E_AUDIO_TRACK_INVALID / E_AUDIO_FILE_TOO_LARGE /
                # E_AUDIO_UNSUPPORTED_FORMAT / E_AUDIO_TRACK_NOT_FOUND / E_AUDIO_PEAKS_FAILED

def audio_dir(clip_dir: Path) -> Path
def normalize_track(raw: Any) -> dict
    # {id,kind,asset_id,path,filename,volume(0..1),fade_in_s>=0,fade_out_s>=0,
    #  duration_s>=0,peaks[]<=800,sha256,bytes,created_at}
def validate_tracks(raw: Any) -> list[dict]
def extract_peaks(media_path: Path, points: int = MAX_PEAKS) -> list[float]
    # ffmpeg -i X -ac 1 -ar 8000 -f s16le - → 每段取绝对值峰值 → 线性重采样到 points 个 [0,1]
    # ffmpeg 不可用/失败 → ClipAudioError(E_AUDIO_PEAKS_FAILED, 可读原因)
def probe_duration(media_path: Path) -> float | None      # ffprobe，失败返回 None
def save_bgm(clip_dir: Path, *, clip_id: str, filename: str, source_path: Path,
             max_bytes: int) -> dict
    # 校验扩展名/体积 → 复制到 audio/bgm-{sha8}{ext} → 算 sha256/bytes/duration/peaks → 返回 track
def delete_bgm(clip_dir: Path, asset_id: str) -> dict      # 删文件 + 返回 {"removed":bool,"asset_id":...}
def peaks_payload(track: dict) -> dict                     # 前端画波形用（peaks 可能被截断到 800）
```

## 4. 局部任务（`backend/src/studio_local_tasks.py`）

新增常量与路由（**必须**加进 `TASK_TYPES` / `TASK_PREFIX` / `PREFIX_TASK` /
`TASK_ID_PATTERN` / `_HANDLERS`）：

| task_type | 前缀 | 语义 |
|---|---|---|
| `UPDATE_SUBTITLE` | `msub` | 字幕文本/时间/样式 → 写 config+overrides 并生成 ASS/SRT rev |
| `UPDATE_TRANSFORM` | `xfm` | 画面变换「应用」→ 写 clip `transform` + 既有 sidecar |
| `UPDATE_AUDIO` | `aud` | BGM 音量/淡入出/原声音量/静音 |
| `UPDATE_EFFECTS` | `fx` | 贴纸/片尾/滤镜/转场/分辨率 |
| `RENDER_CLIP` | `rnd` | **本阶段只登记待渲染并返回摘要**：`render_deferred=True, reason="需 Phase 2 渲染流程支持"`，不 bump render_revision |

`TASK_ID_PATTERN = r"^(autoedit|cover|title|subtitle|edit|msub|xfm|aud|fx|rnd)_[a-f0-9]{12}$"`
（既有 `UPDATE_SUBTITLE_STYLE`/`subtitle` 前缀保持不变，避免老任务无法识别。）

- `_summarise` 为每个新类型加中文摘要。
- 每个 handler 都只改本 clip 的配置/资产；**禁止**调用
  `studio_auto_edit_bridge.submit_auto_edit_task` / `rerender_auto_edit_task` /
  任何 `autoedit_` 前缀的新任务。
- `_handle_render_clip` 返回体里必须带 `render_config`（`clip_config.render_config_summary`）。
- `UPDATE_SUBTITLE` 处理器**不再**返回 `SUBTITLE_ADAPTER_GAP`（工作台侧合成已实现），
  但必须带 `"export_burn_in": "phase2"` 标注「尚未进入导出渲染」。

## 5. HTTP 端点（`backend/src/api/routes/media.py`，风格与既有路由一致）

错误映射：`ClipConfigError/TranscriptRevisionError/SubtitleComposeError/ClipAudioError/
ProjectStoreError/LocalTaskError` → 带 `E_` 码的 422（`detail` = `"CODE: 可读原因"`）；
`FileNotFoundError`/未登记 clip → 404；其它 → 400/500。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/studio/clips/{project_id}/{clip_id}/config` | 绑定的完整配置（`clip_id="source"` → 源片级） |
| PUT | `/studio/clips/{project_id}/{clip_id}/config` | 部分更新（422 带原因），返回 config/revisions/changed/task |
| GET | `/studio/clips/{project_id}/{clip_id}/transcript/revisions` | 三态稿 + 修订列表（`view=raw\|corrected\|effective` 可选） |
| POST | `/studio/clips/{project_id}/{clip_id}/transcript/revisions` | 新增修订 `{author,reason,changes[]}` |
| POST | `/studio/clips/{project_id}/{clip_id}/transcript/rollback` | `{revision}` 回滚到某已批准修订 |
| GET | `/studio/clips/{project_id}/{clip_id}/subtitles` | cues + config + 版本列表 |
| POST | `/studio/clips/{project_id}/{clip_id}/subtitles` | 保存 config+overrides（+可选 cues）并生成新版 ASS/SRT |
| POST | `/studio/clips/{project_id}/{clip_id}/audio/bgm` | multipart 上传 BGM（单文件上限 = `config.max_video_upload_bytes`） |
| DELETE | `/studio/clips/{project_id}/{clip_id}/audio/bgm/{asset_id}` | 删除 BGM 资产 |
| GET | `/studio/clips/{project_id}/{clip_id}/render-config` | 导出摘要（每项生效值 + 来源） |

`GET .../subtitles` 返回：
```jsonc
{"project_id","clip_id","config":{...},"cues":[...],"overrides":[...],
 "revisions":[{"revision":1,"ass_path":"…","srt_path":"…","cue_count":12,
               "created_at":"…","config_hash":"…"}],
 "latest_revision":1,"export_burn_in":"phase2",
 "note":"字幕版本用于预览与版本追溯；烧录进导出成片属 Phase 2"}
```

`POST .../subtitles` body：
```jsonc
{"config": {...},            // 可选，部分覆盖
 "overrides": [...],          // 可选，整体替换
 "cues": [{"segment_id","start","end","text"}],  // 可选：直接给出最终 cue（会转成 overrides）
 "reason": "手动调整",
 "task_id": null}
```

`POST .../audio/bgm` form：`file`（必填）、`volume`、`fade_in_s`、`fade_out_s`；
返回 `{"clip_id","track":{...},"audio_tracks":[...],"peaks":[...]}`。

`GET .../render-config` 返回：
```jsonc
{"project_id","clip_id","binding",
 "effective":[{"key":"subtitle_config.font_size_px","value":62,"source":"skill_default"}],
 "sources":{...},
 "export":{"resolution":"1080x1920","deferred":true,
           "note":"导出侧（外部 ASS 烧录 / BGM 混音 / 滤镜 / 转场 / 片尾卡 / 非 9:16）属 Phase 2"},
 "config":{...}}
```
`source` 取值：`user`（clip 记录已显式写过）、`project`（源片级 `source_config`）、`skill_default`。

## 6. 前端契约（`frontend/src/components/studio/inspector/`）

新增组件目录（唯一新 UI 归属），`frontend/src/app/studio/page.tsx` 只做**接线**：

- `types.ts`：与 §2/§5 对应的 TS 类型
- `api.ts`：typed fetch（`getClipConfig/putClipConfig/getTranscript/addTranscriptRevision/rollbackTranscript/getSubtitles/saveSubtitles/uploadBgm/deleteBgm/getRenderConfig`）
- `inspector-pane.tsx`：外壳，固定 5 标签 `转写｜字幕｜封面与画面｜音频｜效果设置`（`.inspector-tabs`）
- `transcript-tab.tsx` / `subtitle-tab.tsx` / `cover-picture-tab.tsx` / `audio-tab.tsx` / `effects-tab.tsx`
- `subtitle-overlay.tsx`：播放器内实时字幕叠加层（与 ASS 同一 `percent_to_pixels` 公式：
  `left = W*x_pct/100`，`bottom = H*(100-y_pct)/100`，`fontSize = font_size_px * (W/1080)`）
- `audio-waveform.tsx`：canvas 画 peaks（不引重型库）
- 每个标签一个 `*.test.tsx`（RTL），加 `inspector-pane.test.tsx`

保存状态统一显示：`保存中 / 已保存 / 失败原因`。

Phase 2 标注必须可见（UI 文案）：
- 字幕标签：「字幕版本已保存（预览用）；烧录进导出成片属 Phase 2」
- 画面比例菜单 tooltip：「导出仍按流水线 9:16（Phase 2 支持其他比例）」
- 效果标签分辨率下拉：「Phase 2 生效」
- 封面/画面、音频同处标注导出侧未生效

---

# 附：第二轮追加（A 标签栏布局 / B「效果」成片风格微调 + 导出生效）

> 追加于 Phase 1 主体完成之后。**导出必须真实生效**（用工作台既有的
> `clip_transform.build_filter_chain` + `GET .../export-transformed` 链路扩展），
> 仍然**禁止改 `/domain/video-brain/**`**，也**不得改 `frontend/src/lib/clip-transform.ts`(+test)**
> （既有几何对照向量必须继续全绿；几何语义不变，只是这条链上**追加**颜色/音频阶段）。

## B1. `effects` schema（向后兼容扩展，缺字段按默认值读）

保留既有键，新增以下键（`clip_config.normalize_effects` 负责校验与默认值）：

```jsonc
"effects": {
  "preset": "custom",              // custom|medical_pro|clear_speech|science_focus|natural
  "picture": {
    "brightness": 0.0,             // -1..1   → ffmpeg eq=brightness
    "contrast": 1.0,               // 0..3    → ffmpeg eq=contrast
    "saturation": 1.0,             // 0..3    → ffmpeg eq=saturation
    "sharpness": 0.0,              // 0..2    → ffmpeg unsharp=5:5:<amount>
    "temperature": 0.0             // -1..1   → ffmpeg colortemperature（<0 冷 / >0 暖）
  },
  "filter": {"name": "none", "strength": 0.0},   // 既有；name 追加 "vivid"
  "transition": {"enabled": false, "type": "none", "duration_s": 0.5},  // 追加 duration_s（0..3）
  "audio": {"ducking": {"enabled": false, "amount": 0.3}},              // 追加（0..1）
  "sticker": {...},              // 既有
  "end_card": {...},             // 既有
  "export_resolution": "1080x1920"   // 既有
}
```

**风格预设**（选中即写入 2–5 分区的取值组合；用户手改后 `preset` 变 `custom`）：

| preset | picture | filter | audio | transition |
|---|---|---|---|---|
| `medical_pro` | brightness 0.03 / contrast 1.08 / saturation 1.02 / sharpness 0.25 / temperature -0.05 | none | ducking 0.35 | fade 0.4 |
| `clear_speech` | 0 / 1.12 / 1.05 / 0.45 / 0 | none | ducking 0.30 | fade 0.3 |
| `science_focus` | -0.02 / 1.15 / 0.98 / 0.35 / -0.1 | cool | ducking 0.40 | fade 0.5 |
| `natural` | 0 / 1 / 1 / 0 / 0 | none | ducking off | none |

## B2. 导出链路（`backend/src/studio_transform_export.py` + `backend/src/clip_transform.py`）

在既有几何链（crop → scale → overlay，含 blur/color 背景）**之后**追加：

1. 颜色：`eq=brightness=B:contrast=C:saturation=S`（非默认才加）→ `unsharp=5:5:<sharpness>` → 色温
   （`temperature != 0` 时用 `colortemperature=temperature=<开尔文>`，映射 `K = 6500 - t*3500`，钳到 2000..12000）
   → `filter` 预设：`warm`=`colorbalance=rs=.08:gs=.02:bs=-.06`、`cool`=`colorbalance=rs=-.06:bs=.08`、
   `bw`=`hue=s=0`、`vivid`=`eq=saturation=1.25:contrast=1.06`；`strength` 线性缩放该组参数（0 = 不生效）。
2. 转场：`transition.enabled && type=="fade"` → `fade=t=in:st=0:d=<duration_s>,fade=t=out:st=<duration-duration_s>:d=<duration_s>`。
3. 音频（仅当存在音频轨）：原声 `volume=<voice_volume>`（`muted` → `volume=0`）；
   有 BGM 时 `[1:a]volume=<bgm.volume>,afade=t=in:st=0:d=<fade_in_s>,afade=t=out:st=<duration-fade_out_s>:d=<fade_out_s>[bgm]`，
   ducking 开启则 `[bgm][voice]sidechaincompress=threshold=<...>:ratio=<...>[bgmd]` 再 `amix=inputs=2:duration=first:dropize...`；
   无 BGM 时只输出原声 `-c:a aac`。所有 `-i` 输入顺序、`-map` 必须显式，缺音频时保持“无音轨”不报错。
4. **预览 vs 导出**：颜色类预览用 CSS filter 近似（数值同源：`brightness(x)`/`contrast(x)`/`saturate(x)`/
   色温用 `sepia`+`hue-rotate` 近似），UI 必须标注「颜色类预览为近似，导出以 FFmpeg 为准」；
   几何类严格一致（既有对照向量守护）。

`GET .../export-transformed` 增加可选 `project_id` 查询参数：给了就从台账读该 clip 的
`transform` + `effects` + `audio_tracks`/`voice_volume`/`muted`（不传则维持既有语义，仅几何变换）。

## B3. UI（`转写｜字幕｜封面｜音频｜效果`）

标签栏：`display:flex;flex-wrap:nowrap;gap:0`、无左右留白、每个标签 `flex:1 1 0` + 居中 +
`white-space:nowrap` + `overflow:hidden;text-overflow:ellipsis`，`overflow-x:auto`（极窄可横滚，
**任何宽度都不换行/不竖排**），激活项用既有青绿下边框/底色，相邻标签用极细分隔线区分。
文案 `转写｜字幕｜封面｜音频｜效果`，完整名放 `title`（封面→"封面与画面"、效果→"效果设置"），
每个标签都有 `aria-label`。

「效果」标签 5 个分区：1 风格预设 / 2 画面调整 / 3 画幅与构图 / 4 转场 / 5 音频处理（含 ducking；
无 BGM 时置灰 + tooltip）。全部绑定当前选中 clip，改动即时预览，走既有 `clip_config` + 局部任务
（`UPDATE_EFFECTS`/`UPDATE_TRANSFORM`/`UPDATE_AUDIO`），**绝不触发转写/自动剪辑**。
