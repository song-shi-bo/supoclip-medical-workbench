# 医学指南库（第三库）与切片医学闸门设计

状态：**阶段一已实施（2026-11-10）**——种子条目入库、medical_gate 闸门、规划后硬剔除执法、
指纹接线、单测全绿。
日期：2026-11-10 决策与实施
关联：`docs/two-step-planning-design.md`（两步制规划）、`docs/AUTOEDIT_WORKBENCH.md` §3.2

## 背景与目标

用户痛点：切片规划（提示词A）会把**医生患者聊家常**的片段规划成切片——因为
内容分析的 `topic_units` 没有"医学/非医学"轴，闲聊主题块和医学主题块在规划器
眼里地位平等。

目标（用户 2026-11-10 四项职责，**只用于门控，不生成新诊疗意见**）：
1. 找出视频中与医学主题匹配的片段；
2. 判断片段是否带必要前提、适用范围、风险边界；
3. 决定哪些片段不得入选/不得拼接；
4. 多候选择优时提供医学完整度依据。

## 已确认的三项决策（用户拍板）

| 决策点 | 结论 |
|---|---|
| 执法强度 | **硬剔除**：医学密度不达标的切片规划后由程序移入 discarded（宁缺毋滥） |
| 指南来源 | 从公开权威来源（NCI PDQ 患教/指南摘要等）获取**黑色素瘤**材料整理成结构化条目 |
| 生效策略 | **直接生效 + 事后复核**（条目 `review_status=pending_review`，复核 API 回填） |

## 架构（阶段一）

```
内容分析 content_analysis（不动）
    ↓
【医学闸门·规划前标注】clip_medical_gate.run_gate（纯本地，不调 LLM、不连库）
    输入：纠错分段 + topic_units
    匹配：主题块（标题+钩子/收尾引用+首末分段）→ 向量(bge-small-zh) × 指南条目
          + 全库去重关键词命中（LEXICAL_HITS_NORM 归一）→ score = max(向量, 词法)
    输出：medical_profile（逐主题块 strong/weak/none + 命中条目 + 医学分段集合）
    ↓
切片规划 clip_planning（提示词A 一字不动，防混契约不变）
    拼装层 _analysis_context_lines 透出【医学主题匹配】背景行（只标注标签与匹配度，
    条目正文绝不进提示词——提示词A「信息忠实」约束）
    ↓
【医学闸门·规划后执法】enforce_clips（硬剔除，2026-11-10 用户拍板）
    切片医学分段密度 < DISCARD_BELOW(0.2) 且覆盖主题块最强级别 = none
    → 移入 discarded（type=非医学闲聊，附证据）；缺口由全覆盖契约程序补齐
    ↓
切片精修 clip_refinement（提示词B 不动，只删不改契约无交集）
```

### 领域激活规则（防误伤，重要）

只有全片存在 **strong** 级主题块命中才进入 `enforce` 模式；weak 或零命中走
`annotate_only`（只标注不剔除）。原因：
- bge-small-zh 对**无关**中文短文本余弦常落 0.45~0.55，weak 阈值（0.55）之上的
  weak 命中可能是库外领域的语义邻近（实测：高血压视频主题块 vs 黑色素瘤库 ≈0.50）；
- 库没覆盖该视频领域时没有判断依据，硬剔除会把合法视频全片杀光。
- 主题块级词法得分用**全库去重关键词命中数**（`LEXICAL_HITS_NORM=8` 归一）：
  真实医学对话命中 ≥7 个词很常见（→strong），闲聊命中 0——区分度远高于单条目占比。

## 数据模型

### 文件快照（闸门消费，规划阶段不连库/不联网）

`backend/medical_kb_seed/active_snapshot.json`（由 `scripts/materialize_medical_kb.py`
从种子 JSON 物化；`MEDICAL_KB_SNAPSHOT` 可覆盖路径）：

- 闸门指纹 = 加载时按 **approved 条目实际内容**现算的 sha256（不信任文件内声明值）；
  手改快照 / 换种子只要内容变了，指纹就变 → 旧计划缓存自动失效；
- 快照缺失/损坏 → 闸门禁用（profile 如实标注），规划照常，**绝不阻断**。

### 数据库表 `medical_guidelines`（入库/检索/复核）

`migrations/007_medical_guidelines.sql`：条目级结构化存储，字段 =
`id / topic_path[] / entry_type / statement / prerequisites / applicability /
risk_boundary / audience_note / keywords[] / source_name / source_url /
evidence_level / lang / status / review_status / reviewed_by / reviewed_at /
content_hash(唯一) / embedding_json`。风格与热点双库一致（fastembed 向量可空、
降级不阻塞入库）。种子 44 条已入库（2026-11-10）。

### 条目结构（种子 `2026-11-10-melanoma-nci-seed.json`，44 条）

`statement`（单义表述）+ `prerequisites`（前提）+ `applicability`（适用范围）+
`risk_boundary`（风险边界，如"疑似病灶绝不应刮除/烧灼"）+ `audience_note`
（通俗说法，**只参与匹配打分，绝不写入字幕/文案**）+ `keywords`（词法匹配词表，
含口语说法如 点痣/晒伤/复查）+ 出处（NCI PDQ 患者版/专业版、NCI 痣事实页、MedlinePlus）。
覆盖：定义科普、早期识别（ABCDE/就医信号）、危险因素、确诊与病理（活检红线/
第二病理意见/BRAF 检测）、分期（AJCC 8/Breslow/前哨淋巴结）、治疗（手术首选/
靶向前提/免疫治疗）、随访、预防防晒、误区澄清、特殊类型（黏膜/葡萄膜）。

## API（`api/routes/medical_kb.py`，注册于 main_refactored）

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `POST /api/v1/medical-kb/ingest` | Bearer `MEDICAL_KB_INGEST_TOKEN`（.env.kb） | 条目入库，content_hash 幂等，向量缺失静默降级 |
| `GET /api/v1/medical-kb/search` | Bearer `MEDICAL_KB_READ_TOKEN` | keyword/semantic 检索（仅 approved） |
| `POST /api/v1/medical-kb/entries/{id}/review` | Studio 登录态 | 事后复核 pending_review→reviewed，复核人回填 |

中间件豁免清单已加 ingest/search 两路径（`studio_access.py`）；review 走登录态。

## 指纹与缓存契约

`clip_planning.build_current_plan_provenance.rules` 新增：
- `medical_gate`：闸门身份 = `{enabled, gate_version, snapshot_sha256, entry_count,
  strong/weak/lexical 阈值}`——**库内容或阈值变了，旧计划缓存自动失效**；
- `medical_gate_helper_sha256`：闸门模块源码指纹；
- 未显式传身份时由 `med_gate.current_identity()` 现场取（同一快照 → 同一指纹，
  保持 provenance 纯函数语义，w06 直接构建 == plan_clips 内嵌，逐字节相等）。

provenance 的 `prompt_text.root_windows` 记录**含医学标注**的提示词——直接构建方
需同样先过闸门富化（见 `test_w06_planning_wiring.py` 内注释）。

## 产物与消费

- `clip_plan.json` 新增：
  - `medical_gate`：闸门摘要（mode/snapshot_sha256/阈值/逐切片 decisions/moved_count/notes）；
  - 每条切片 `medical_gate` 标注：`{density, best_unit_level, decision, matched_topics, mode}`；
  - discarded 新类型 `非医学闲聊`（与既有 弃用/未覆盖/寒暄 并列）。
- 规划 warnings 新增 `medical_gate: 硬剔除 N 条非医学切片（闸门模式=…）`。
- 前端（阶段二）：切片面板可读 `clip.medical_gate` 展示医学主题标签与依据。

## 红线（与两步制同源）

1. 提示词A/B 一字不动——闸门只在**拼装层**加背景行、在**规划后**程序执法；
2. 条目正文（statement/prerequisites/risk_boundary）**不进任何提示词**；
   `audience_note` 只参与匹配；本库不生成新诊疗意见、不改写字幕/文案；
3. 闸门任何失败（快照缺失/损坏/向量模型异常）→ 降级（禁用或词法），
   绝不阻断规划（有单测锁死）；
4. 环境变量：`MEDICAL_GATE_STRONG=0.62` / `MEDICAL_GATE_WEAK=0.55` /
   `MEDICAL_GATE_LEXICAL_WEIGHT=0.8` / `MEDICAL_GATE_LEXICAL_HITS_NORM=8` /
   `MEDICAL_GATE_DISCARD_BELOW=0.2` / `MEDICAL_KB_SNAPSHOT`（改阈值即缓存失效）。

## 测试（backend/tests/unit/test_clip_medical_gate.py，12 条）

快照加载与内容指纹变化、快照缺失禁用、医学/闲聊文本匹配分野、enforce 模式与
标注、annotate_only（库外领域不误伤）、硬剔除与证据留痕、context 行不泄露条目
正文、plan_clips 集成（剔除+指纹身份变化）、闸门异常降级不阻断。
另有 w06 缓存身份测试更新（医学标注进 provenance 的契约）。

## 阶段二待办

1. 库→审核→重新物化快照闭环：review 后一键重跑 materialize（或 worker 监听）；
2. 前端切片面板医学标注展示 + 指南条目管理页；
3. 前提句吸附（切片边界自动外扩包含 prerequisites 命中句）与合集排序规则；
4. 向量路径记录 top-k 条目 id（目前词法路径有 id、向量最佳单条有 id）；
5. 扩科指南入库（糖尿病、心血管等），多库域路由（按项目病种选快照）。
