# 工作台内容分析与内容规划分层优化方案

> 状态：A～C 层第一阶段已实施（2026-11-16）；D 层明确不在本轮范围  
> 日期：2026-11-16  
> 适用范围：Studio AUTO_EDIT 主链  
> 关联文档：`AUTOEDIT_WORKBENCH.md`、`two-step-planning-design.md`、`medical-gate-design.md`、`qa-style-restructure-design.md`

## 1. 背景

当前 AUTO_EDIT 已具备内容分析、医学主题匹配、切片规划、医学候选淘汰、问答化净化、切片精修和渲染能力，但部分功能的阶段归属与实际接线不一致：

1. 医学主题匹配语义上属于“理解视频”，实际却在 planning stage 中临时执行；
2. `clip_medical_gate` 同时承担分析标注与规划淘汰，两个职责没有清晰分界；
3. `clip_purify` 在 `clip_planning.plan_clips()` 内执行，导致 `clip_plan.json` 同时承载规划决定和后处理结果；
4. 规划缓存同时绑定知识库、医学闸门、说话人 overlay 和 Purify 身份，后处理规则变化也可能使规划缓存失效；
5. 分析、规划、后处理的告警与淘汰理由混在一个计划产物中，难以解释“内容是没被选中，还是选中后被净化删除”。

本方案不以推翻现有算法为目标，而是重新建立阶段边界、数据契约、缓存身份和恢复语义，使系统能够明确回答：

- 原视频里有什么；
- 本次决定剪什么；
- 已决定使用的内容如何干净呈现；
- 系统依据什么知识和规则理解、筛选内容。

### 1.1 本轮实施边界（2026-11-16 用户确认）

本轮只优化 A～C 层：知识与策略配置、内容分析、内容规划。工作台现有“切片精修”整体视为 D 层后处理，因此：

- 不拆分或迁移 `clip_purify`；
- 不修改 qa/mono、`sub_ranges`、Prompt B、字幕精修、渲染和导出行为；
- 不改变现有成片结果与 D 层缓存语义；
- 本文第 6 节保留为职责说明，不作为本轮实施清单；
- 原 Phase 2“Purify 从规划中拆出”延期，只有用户以后单独确认优化 D 层时才实施。

本轮实际改造目标只有两项：

1. 医学主题匹配从 planning stage 前移并正式写入内容分析工件；
2. 规划阶段只消费已持久化的分析标注，再负责领域激活、医学密度判断和候选淘汰，禁止现场重算医学匹配。

---

## 2. 总体原则

### 2.1 四层定义

```text
A. 知识与策略配置层：系统用什么标准理解和约束内容？
                         ↓
B. 内容分析层：原视频里有什么？
                         ↓
C. 内容规划层：这次要从中剪什么、怎么组合？
                         ↓
D. 后处理层：已经决定剪的内容，怎样干净地呈现？
```

推荐内部命名：

```text
knowledge_policy      知识与策略配置
content_analysis      视频内容画像
clip_planning         本次剪辑决策
clip_postprocess      净化、精修与渲染
```

### 2.2 最小判断法

新增功能进入哪个阶段，只问四个问题：

1. 它是否是长期固定的知识、规则、模式？是 → A 层；
2. 它是否只是在理解、标记原视频？是 → B 层；
3. 它是否决定本次选什么、丢什么、如何组合？是 → C 层；
4. 它是否在已选内容内部处理画面、声音、字幕或表达？是 → D 层。

### 2.3 不可混淆的动作

| 对象 | 分析动作 | 规划动作 | 后处理动作 |
|---|---|---|---|
| 患者问题 | 发现并逐字校验问题原话 | 决定是否作为本条开头 | 实际保留对应音视频区间 |
| 医生核心段 | 标记核心表达所在 segment | 决定选哪些核心段 | 删除其间插话并生成 `sub_ranges` |
| 医学知识 | 给主题块匹配标签和证据 | 决定是否启用过滤、是否淘汰候选 | 不生成新诊疗意见，不改写原话 |
| Hook | 发现候选原话 | 排序并选择本条 Hook | 保证 Hook 对应素材在最终成片中存在 |
| 非医学闲聊 | 标注相关性低 | 根据本次目标决定是否丢弃 | 不负责判断是否值得成片 |

---

## 3. A 层：知识与策略配置

### 3.1 职责

这一层是与具体视频无关的“字典和规则书”，只定义系统理解和约束内容的依据。

应归入本层：

- 61 条 MEL 黑色素瘤知识卡；
- NCI、国家卫健委、NCCN/CACA/ESMO 等来源文件及授权信息；
- 知识卡的公众问题、必要前提、适用范围、风险边界、来源锚点和版本；
- 医学主题词、同义词、口语说法、否定词守卫；
- 医学匹配 strong/weak 阈值和规划淘汰阈值的配置值；
- 禁词表、绝对化用语、隐私与个体化诊疗规则；
- Hook 模式定义；
- 规则版本、快照 SHA、实现版本和缓存失效逻辑。

### 3.2 不属于本层的内容

本层不回答：

- 某条视频包含哪些主题；
- 某个主题块匹配度是多少；
- 这次应该剪哪 30 秒；
- 某条候选是否应淘汰；
- 最终成片应删除哪些应答碎片。

### 3.3 配置与决策分离

例如：

```text
MEDICAL_GATE_DISCARD_BELOW=0.2
```

- 阈值定义属于 A 层；
- 某条候选密度为 0.13 属于 B/C 层可消费事实；
- 因低于 0.2 而被本次任务淘汰属于 C 层决策。

### 3.4 推荐保留的现有模块

- `medical_kb.py`
- `medical_kb_seed/`
- 医学词表、同义词和否定守卫模块
- 规则 manifest、快照物化脚本和 SHA 计算

这些模块可继续被分析和规划调用，但不能直接代表某条视频的分析产物或剪辑决定。

---

## 4. B 层：内容分析

### 4.1 职责

内容分析只负责忠实描述原视频，不决定最终成片，不丢弃素材，不做发布选择。

核心原则：

```text
分析可以发现、标记、校验、警告；
分析不能决定最终删什么、拼什么、发布什么。
```

### 4.2 输入

```text
权威原始分段目录：segment_id / start_ms / end_ms
原始转写与纠错转写
说话人标注：医生 / 患者 / 家属 / 未知
视频基础事实：时长、轨道、分辨率等（不等同于多模态视觉理解）
知识与策略快照
```

说明：第一阶段优化仍以转写语义分析为主，不默认引入视觉多模态模型。若未来需要识别人物、场景、屏幕文字或镜头质量，应作为独立的视觉分析子模块接入本层。

### 4.3 输出

建议将正式产物定义为“视频内容画像”：

```text
tasks/analysis/<job_id>/content_profile.json
```

建议结构：

```json
{
  "schema": "studio.content-profile/v1",
  "keywords": [],
  "golden_quotes": [],
  "topic_units": [
    {
      "topic_unit_id": "topic-001",
      "title": "可疑皮损的就医判断",
      "segment_ids": [],
      "hook_quote_candidates": [],
      "ending_quote_candidates": [],
      "patient_questions": [],
      "doctor_core_segment_ids": [],
      "info_seconds": 0,
      "medical_annotation": {
        "level": "strong",
        "score": 0.82,
        "matched_entry_ids": ["MEL-055"],
        "matched_topics": ["黑色素瘤检查", "分期相关"]
      }
    }
  ],
  "speaker_labels": {},
  "candidate_intervals": [],
  "validation": {
    "verbatim_quotes": {},
    "invalid_segment_ids_removed": [],
    "warnings": []
  },
  "provenance": {
    "transcript_identity": {},
    "analysis_prompt": {},
    "knowledge_snapshot": {}
  }
}
```

### 4.4 本层应包含的现有能力

- 全篇主题、关键词和金句候选；
- 主题簇及其 `segment_ids`；
- `hook_quote`、`ending_quote` 候选；
- `patient_question`；
- `doctor_core_segment_ids`；
- `info_seconds`；
- 说话人标签；
- 原话逐字校验；
- 非法 segment ID 清理；
- 主题块医学相关度、命中卡片和匹配证据；
- 分析级告警，例如知识库未覆盖、匹配证据不足、说话人未知。

### 4.5 医学闸门拆分

当前 `clip_medical_gate.run_gate()` 与 `enforce_clips()` 应按职责拆开：

```text
分析阶段：medical_topic_annotator.annotate(...)
规划阶段：medical_selection_policy.evaluate(...)
```

允许继续共用底层匹配函数，但正式产物必须区分：

- `medical_annotation`：分析事实；
- `medical_decision`：规划决定。

### 4.6 明确禁止

内容画像中不得出现：

```json
{
  "decision": "discarded_non_medical"
}
```

可以出现：

```json
{
  "level": "none",
  "score": 0.08,
  "matched_entry_ids": []
}
```

---

## 5. C 层：内容规划

### 5.1 职责

规划阶段根据内容画像、权威分段和本次用户目标，决定本次成片怎么剪。

它回答：

- 本次输出几个主题；
- 选择哪些主题；
- 每条选择哪些连续区间；
- 哪一句作为开头；
- 是否组合多个候选区间；
- 哪些候选应淘汰；
- 素材不足时是否不生成。

### 5.2 输入

```text
content_profile
+ content_profile 引用的权威原始分段
+ 本次用户目标 planning_goal
+ 本次启用的规划策略 policy_snapshot
```

规划不重新读取或理解视频文件，但仍需访问权威分段目录，以完成切点吸附、逐字校验、密度计算和时间重算。

建议新增结构化目标：

```json
{
  "planning_goal": {
    "content_type": "medical_education",
    "target_audience": "public",
    "style": "qa",
    "target_duration_s": [40, 180],
    "medical_filter": "strict",
    "notes": ""
  }
}
```

### 5.3 应归入规划的能力

- 动态切片数量；
- 主题选择；
- 连续时间区间选择；
- Prompt A；
- 主题簇和医学标签背景注入；
- 患者提问 Hook 候选排序；
- Hook 模式评分与最终选择；
- `hook_first_line` 合法性和逐字护栏；
- 医学密度计算和候选淘汰；
- `enforce_clips`；
- 领域激活逻辑；
- `MEDICAL_BG_INJECT` 是否启用；
- 个体化诊疗内容拦截；
- “素材不足，不生成”；
- 规划结果与分析、知识库及策略版本绑定。

### 5.4 输出

继续使用：

```text
tasks/plans/<job_id>/clip_plan.json
```

但将其收敛为纯规划决策：

```json
{
  "schema": "studio.clip-plan/v4",
  "planning_goal": {},
  "clips": [
    {
      "clip_id": "clip-001",
      "theme": "什么样的痣需要专业评估",
      "selected_ranges": [
        {
          "start_segment_id": "seg-101",
          "end_segment_id": "seg-128"
        }
      ],
      "hook": {
        "segment_id": "seg-101",
        "text": "我这个痣是不是一定有问题？",
        "source": "patient_question"
      },
      "ending": {
        "segment_id": "seg-126"
      },
      "medical_decision": {
        "mode": "enforce",
        "density": 0.73,
        "decision": "kept",
        "reason": "医学主题明确且适合公众科普"
      },
      "decision_reason": "主题完整、开头明确、结尾自然"
    }
  ],
  "discarded": [],
  "provenance": {}
}
```

### 5.5 规划层不再包含

- `sub_ranges`；
- 删除了哪些“嗯、对、是的”；
- qa/mono 的实际删段结果；
- 医生纯度执行结果；
- Purify 回退原因；
- 最终字幕 cue；
- 渲染参数和输出文件。

---

## 6. D 层：后处理

### 6.1 职责

后处理的前提是：系统已经决定这条切片要使用。它只负责在已选内容内部进行净化、文本整理和渲染。

建议主链：

```text
planning
→ purifying
→ refining
→ rendering
→ completed
```

### 6.2 Purify

应归入本阶段：

- 删除“嗯、对、是的”等应答碎片；
- qa 模式保留患者提问；
- mono 模式删除患者声音；
- 保留医生核心段；
- 医生纯度计算；
- 删除比例超过阈值时回退；
- 拼接合法性校验；
- 生成最终 `sub_ranges`；
- 记录实际删除的 segment IDs；
- 确保规划所选 Hook 对应区间仍被保留。

建议产物：

```text
tasks/plans/<job_id>/clip_purification.json
```

建议结构：

```json
{
  "schema": "studio.clip-purification/v1",
  "plan_fingerprint": "...",
  "style": "qa",
  "clips": {
    "clip-001": {
      "sub_ranges": [],
      "dropped_segment_ids": [],
      "doctor_ratio": 0.81,
      "question_kept": true,
      "fallback_reason": null
    }
  },
  "provenance": {}
}
```

### 6.3 Refinement

Prompt B 继续属于后处理：

- 只允许删除无用完整句；
- 只允许删除语气词；
- 禁止改写、润色和新增医学信息；
- 针对 Purify 后 `sub_ranges` 的并集处理；
- 产出角色、上屏字幕和核对清单；
- 违规段回退纠错转写。

建议继续使用：

```text
tasks/plans/<job_id>/clip_refinement.json
```

但其指纹应依赖 purification，而不是直接把 Purify 身份塞进 planning fingerprint。

### 6.4 Rendering

渲染消费：

```text
clip_plan
+ clip_purification
+ clip_refinement
+ render settings
```

渲染前应校验：

- 所有 `sub_ranges` 均位于规划选择范围内；
- Hook 对应素材未被 Purify 删除；
- 最终文本逐字来自权威转写，仅发生允许的删除；
- 时间轴无逆序、重叠和越界；
- 字幕与多区间虚拟时间轴一致；
- 产物可解码。

---

## 7. 目标接线

```text
knowledge_policy
  ├─ medical_kb snapshot
  ├─ vocabulary / negation guards
  ├─ prohibited expressions
  ├─ hook patterns
  └─ policy versions and thresholds
            ↓
content_analysis
  ├─ transcript semantic analysis
  ├─ speaker annotation
  ├─ verbatim and segment-id validation
  └─ medical topic annotation
            ↓
content_profile.json
            ↓
clip_planning
  ├─ Prompt A
  ├─ topic and interval selection
  ├─ Hook selection
  ├─ medical/individual-treatment filtering
  └─ insufficient-material decision
            ↓
clip_plan.json
            ↓
clip_postprocess
  ├─ clip_purify → clip_purification.json
  ├─ Prompt B → clip_refinement.json
  └─ render → clip_final.mp4 / subtitles / warnings
```

---

## 8. 缓存与指纹重构

### 8.1 分级指纹

建议建立四级身份：

#### Analysis fingerprint

依赖：

- 转写版本和分段内容；
- 说话人标注身份；
- 内容分析提示词和实现；
- 医学知识快照及匹配实现。

不依赖：

- qa/mono；
- Purify 应答词表；
- 渲染设置。

#### Planning fingerprint

依赖：

- analysis fingerprint；
- planning goal；
- Prompt A；
- 医学淘汰策略和阈值；
- Hook 选择策略；
- 规划实现版本。

不依赖：

- Purify 版本；
- 字幕样式；
- FFmpeg 参数。

#### Purification fingerprint

依赖：

- planning fingerprint；
- qa/mono 风格；
- 说话人标注；
- 应答词表；
- 医生纯度和最小保留比例；
- Purify 实现版本。

#### Render fingerprint

依赖：

- purification fingerprint；
- refinement fingerprint；
- 字幕、封面、画面和音频设置；
- 渲染器版本。

### 8.2 期望重跑语义

| 变化 | 重跑范围 |
|---|---|
| 知识卡或医学匹配算法变化 | 分析 → 规划 → 后处理 |
| 用户改变成片目标 | 规划 → 后处理 |
| 医学淘汰阈值变化 | 规划 → 后处理 |
| Hook 选择策略变化 | 规划 → 后处理 |
| qa 改 mono | Purify → 精修 → 渲染 |
| 应答词表变化 | Purify → 精修 → 渲染 |
| 字幕样式变化 | 渲染 |
| FFmpeg 参数变化 | 渲染 |

---

## 9. 状态机与恢复语义

### 9.1 推荐状态机

```text
queued
→ retrieving_rules
→ extracting_highlights
→ annotating_content
→ planning
→ purifying
→ refining
→ rendering
→ completed / completed_with_warnings / partial_succeeded / technical_failed
```

前端可将 `extracting_highlights + annotating_content` 合并显示为“内容分析中”，将 `purifying + refining` 合并显示为“内容净化中”，避免状态过多。

### 9.2 诊断停点

建议：

- `stop_after=analysis`：完成内容语义分析、说话人和医学主题标注，产出 `content_profile.json`；
- `stop_after=plan`：只完成纯规划，产出 `clip_plan.json`，不执行 Purify；
- 可新增 `stop_after=purify`：验证 `sub_ranges`，不调用 Prompt B、不渲染。

### 9.3 恢复规则

- analysis fingerprint 一致：复用 `content_profile.json`；
- planning fingerprint 一致：复用 `clip_plan.json`；
- purification fingerprint 一致：复用 `clip_purification.json`；
- refinement fingerprint 一致：复用 `clip_refinement.json`；
- render fingerprint 一致且成片可解码：跳过渲染。

---

## 10. 兼容策略

### 10.1 旧计划读取

旧 `clip_plan.json` 可能内含：

- `sub_ranges`；
- `purify`；
- 每条切片的净化字段。

兼容期读取逻辑：

1. 若存在独立 `clip_purification.json`，以新产物为准；
2. 若不存在但旧计划含 `sub_ranges/purify`，转换为内存中的 legacy purification；
3. 不回写旧计划，不破坏历史工件；
4. 新任务只写新契约。

### 10.2 Schema 版本

建议升版：

```text
content analysis → studio.content-profile/v1
clip plan       → studio.clip-plan/v4
purification    → studio.clip-purification/v1
refinement      → 保留现有 schema，增加 purification_fingerprint
```

### 10.3 API 与前端

第一阶段不要求大改 UI，只需：

- 现有切片卡继续读取最终有效区间；
- 任务详情可区分“规划淘汰”和“净化删除”；
- 旧任务仍可播放、导出和重新渲染。

后续可新增：

- 内容分析主题块查看器；
- 规划决策解释；
- Purify 删除区间预览；
- 医学标签和知识卡依据展示。

---

## 11. 分阶段实施计划

### Phase 0：契约冻结与测试基线

目标：先锁定当前行为，防止重构改变成片。

- 为现有分析、规划、Purify 产物建立黄金样例；
- 记录同一素材当前 clips、discarded、sub_ranges 和字幕结果；
- 增加“最终所选 segment 集合不变”的对比测试；
- 明确旧任务兼容读取规则。

验收：未开始改接线前，黄金样例可稳定重放。

### Phase 1：医学标注归入分析

目标：分离“医学匹配事实”与“规划淘汰决定”。

- 将 `run_gate` 的主题匹配提前到分析阶段；
- 分析产物保存 `medical_annotations` 和知识快照身份；
- 规划阶段只消费分析标注；
- `enforce_clips` 继续留在规划阶段；
- 禁止规划阶段重新计算另一份医学匹配结果。

验收：同一分析产物可被不同 planning goal 重复使用；分析产物中没有 discarded 决策。

### Phase 2：Purify 从规划中拆出（本轮不实施）

该项属于 D 层“切片精修/后处理”。2026-11-16 用户明确要求本轮不优化 D 层，因此保留现有 `clip_purify → refinement → render` 接线与产物，不新增 `purifying` 状态，不迁移 `sub_ranges`，也不改变 Prompt B 和渲染消费方式。

本节仅保留为未来可能的独立议题；必须再次取得用户确认后才能实施。

### Phase 3：缓存身份分层

目标：实现最小必要重跑。

- 拆分 analysis/planning/purification/refinement/render fingerprint；
- 清除 planning provenance 中的 Purify 身份；
- 为每级工件增加上游 fingerprint；
- 补齐恢复、实现变化和上下文变化测试。

验收：任一层规则变化只使本层及下游缓存失效。

### Phase 4：规划能力补齐

目标：在清晰边界上增加真正的内容规划能力。

- 引入 `planning_goal`；
- Hook 候选排序和模式评分；
- 个体化诊疗内容拦截；
- “素材不足，不生成”的结构化结果；
- 多用途规划：医学科普、人物故事、访谈花絮等。

验收：同一 `content_profile` 使用不同目标可得到不同计划，且不重复分析原视频。

### Phase 5：可观测性与前端

- 展示分析主题块和医学标签；
- 区分规划淘汰与后处理删除；
- 展示 Hook 来源和最终保留情况；
- 支持查看规划区间与最终 `sub_ranges` 的差异。

---

## 12. 测试策略

### 12.1 分层单测

#### 分析层

- 引用句必须来自对应主题块原文；
- 非法 segment ID 被清除；
- 医学标注不产生 discard 决策；
- 知识库不可用时只降级标注，不阻断分析；
- 说话人未知时不伪造身份。

#### 规划层

- Prompt A 只消费分析产物与权威分段；
- 医学密度淘汰只发生在规划层；
- Hook 必须来自候选或切片窗口原话；
- 不同 planning goal 可产生不同选择；
- 素材不足时返回明确的 no-output 原因；
- 计划中不存在 `sub_ranges` 和 Purify 执行字段。

#### 后处理层

- Purify 不得选择规划范围外的 segment；
- qa 模式保护选中的患者问题；
- mono 模式按规则删除患者声音；
- 删除比例过高时回退；
- Hook 对应区间被删除时必须失败或回退；
- Prompt B 继续满足只删不改；
- 多区间渲染时长和字幕虚拟时间轴正确。

### 12.2 跨层契约测试

- 分析标注变化使规划缓存失效；
- planning goal 变化不使分析缓存失效；
- Purify 配置变化不使规划缓存失效；
- 渲染参数变化只使渲染缓存失效；
- 旧计划内嵌 `sub_ranges` 可正常兼容；
- 新计划与新 purification 合并后的有效执行区间正确。

### 12.3 黄金样例验收

至少准备：

1. 黑色素瘤医学对话；
2. 含大量闲聊的问诊；
3. 库外医学领域素材；
4. 医生独白；
5. 多患者或跨会诊对象素材；
6. 无足够安全成片素材。

对每个样例记录：

```text
分析主题 → 规划保留/淘汰 → Purify 删除 → 最终成片 segment 集合
```

---

## 13. 验收标准

完成本次分层优化后，系统必须满足：

1. 内容分析产物完整描述视频，但不包含最终淘汰和发布决定；
2. 医学主题匹配在分析阶段产生，`medical_analysis` 与 `medical_annotations` 正式落盘；
3. 医学候选淘汰只在规划阶段发生；
4. 规划阶段不重新加载知识库、不重新匹配医学主题；
5. 知识快照或匹配实现变化使分析缓存失效；
6. 医学淘汰阈值变化使规划缓存失效；
7. 旧分析工件缺少医学标注时明确禁用过滤并留痕，不静默现场重算；
8. D 层 `clip_purify`、Prompt B、`sub_ranges`、字幕和渲染行为保持不变；
9. 旧任务可继续播放、导出和重新渲染；
10. 分析标签与规划淘汰决定可分别审计。

---

## 14. 工程量评估

### 14.1 本轮 A～C 接线整理

范围：Phase 0、Phase 1 和 A～C 指纹整理；Phase 2（D 层）不实施，也不新增 Hook 评分、用户目标或个体化诊疗新算法。

本轮主要是后端接线、分析/规划数据契约、缓存身份、兼容降级、测试与文档同步，不涉及前端和渲染链改造。

### 14.2 落实完整规划能力

包含 Phase 4～5：结构化用户目标、Hook 模式评分、个体化诊疗拦截、多用途规划和前端解释视图。

预计约 2～4 周，取决于旧任务兼容深度和前端展示范围。

### 14.3 不纳入本次默认范围

以下能力会显著扩大工程量，应单独立项：

- 模型直接观看视频画面；
- OCR、人物识别和场景切换分析；
- 表情、镜头质量和视觉可用性评分；
- 新增独立多模态模型服务。

---

## 15. 推荐决策

本轮按以下顺序实施：

1. 冻结现有输出和 D 层行为；
2. 将医学主题匹配正式归入内容分析并写入分析工件；
3. 让规划只消费持久化分析标注，领域激活与候选淘汰继续留在规划层；
4. 将知识快照和匹配实现纳入分析指纹，将淘汰阈值纳入规划指纹；
5. 补齐旧分析工件降级、缓存失效和“不在规划阶段重算”的测试。

`clip_purify`、Prompt B、切片精修、字幕和渲染属于 D 层，本轮保持现状。未来若要拆分 D 层，另开方案并单独确认。

---

## 16. 示例

原视频：

```text
患者：我这个痣是不是一定有问题？
医生：不能只靠肉眼判断。
医生：如果近期明显变化，建议专业评估。
医生：具体要不要活检，要结合检查和个人情况。
```

### A 层：知识与策略

```text
MEL 知识卡：可疑皮损、专业评估、活检前提和个体化诊疗边界
个体化诊疗规则：具体是否活检不能泛化为公众结论
医学过滤阈值及 Hook 规则
```

### B 层：内容分析

```text
主题簇：可疑皮损的就医判断
患者问题候选：我这个痣是不是一定有问题？
医生核心段：
- 不能只靠肉眼判断
- 出现变化建议专业评估
- 是否活检需结合个人情况
医学标签：黑色素瘤相关，strong
匹配依据：MEL-055
```

分析阶段不删除“是否活检”这句话，只标明其内容和个体化属性。

### C 层：内容规划

```text
目标：45 秒“什么样的痣需要专业评估”
开头：保留患者问题
主体：保留“不能只靠肉眼判断”
结尾：保留“建议专业评估”
不使用：“要不要活检”段
原因：更偏个体化诊疗决策，不适合作为本次公众科普结论
```

### D 层：后处理

```text
qa 模式保留患者问题
删除患者“嗯、明白了”等应答
删除医生无意义停顿
生成最终 sub_ranges
精修字幕只删无用句和语气词
渲染并导出成片
```

该示例体现了完整边界：

```text
分析发现内容
→ 规划决定取舍
→ 后处理执行删除和呈现
```
