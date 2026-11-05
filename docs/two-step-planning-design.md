# 两步切片规划设计（范围规划 + 切片精修）

状态：**已实施（2026-11-05）——第一步/第二步代码落地，worker 接线完成，防混单测通过。**
日期：2026-11-04 决策会话；2026-11-05 实施

## 背景

现有切片规划是单步（`clip_planning.py::_PLAN_SYSTEM_PROMPT`）：一次 LLM 调用同时完成
区间选择。用户决定拆成两步，两步都调用大模型，各自使用专属提示词，**绝不能混用**。

## 已确认的四个决策（用户拍板）

| 决策点 | 结论 |
|---|---|
| 精修产物用途 | **右侧展示 + 文案导出**；成片烧录字幕暂不动（仍用原纠错转写） |
| 精修执行时机 | **规划完成后立即逐条精修**，产物随规划一起落盘，一次任务内完成 |
| 精修失败兜底 | **回退原始纠错转写 + 记 warning**，不阻断任务（与现有规划哲学一致） |
| 第一步定位 | **完全替换现有 `_PLAN_SYSTEM_PROMPT`**，旧提示词退役 |

## 架构

```
内容分析 extracting_highlights                ← 不动
    ↓
【第一步·范围规划】clip_planning（换用用户提示词A）
    输入：纠错分段 + 分析四产物（不变）
    输出：clip_plan.json（schema 不变：连续区间/segment 边界/程序重算时间）
    ↓
【第二步·切片精修】新模块 src/clip_refinement.py（用户提示词B）
    逐条切片：该切片纠错分段 + 提示词B → LLM
    输出：每段精修文本按 segment_id 键控对齐
      （要求示例：去语气词；每条标注说话人 医生/患者；说话人集合以用户提示词为准）
    落盘：tasks/plans/<job_id>/clip_refinement.json
      {clip_id: {segments:[{segment_id, refined_text, speaker?}],
                 source: llm|fallback, warnings[], prompt_version, model}}
    ↓
plan_ready（诊断停点后移到两步都完成之后）
    ↓
渲染 clip_render                              ← 不动（烧录字幕仍用原纠错转写）
```

消费侧：
- 工作台切片预览右侧面板显示精修转写（前端改造，读新 artifact 的 API）
- 文案导出（如 切片01_文案.md）出精修版

## 防混机制（硬要求）

1. 两份提示词为独立常量（提示词A/B），各自独立版本号与 sha256，进 provenance；
   改任何一个字 → 缓存身份变化。
2. 两个阶段各建独立 Agent 构建函数，绑定关系写死在代码，无运行时选择路径。
3. 单测互斥断言：规划阶段发出的 system prompt 逐字等于提示词A；
   精修阶段逐字等于提示词B；任何交叉直接测试失败。
4. 输出 schema 分开校验：A 只准输出区间 JSON；B 只准输出按段精修 JSON。

## 技术硬约束

- **segment_id 对齐**：第二步输出必须挂在原分段 ID 上，否则字幕/文案时间轴断裂。
  若用户提示词B的输出是自由文本，实现时加对齐适配层。
- 说话人标注由 LLM 按内容推断（无 diarization）。注意项目实际对话含**家属**
  （如切片01"我是他爱人"），提示词B的标签集合需覆盖或明确归类规则——以用户
  提供的提示词为准，实现前复核一次。
- 精修逐条独立调用，失败按切片粒度回退（source=fallback 如实标注），绝不冒充。

## 提示词已归档（2026-11-04 用户提供，逐字使用）

- 提示词A：`docs/prompts/step1-slice-planning.md`
- 提示词B：`docs/prompts/step2-slice-refinement.md`

## 复核结论

- **B 输出不是 JSON**：四段文档结构（`## 1 精修 SRT` / `## 2 待核对清单` /
  `## 3 合规处理清单` / `## 4 结尾文字卡建议`）→ 实现解析器按标题切块，
  SRT 部分按标准语法解析后与 segment 时间轴对齐。
- **说话人标签**：提示词B定为（医生）/（家属）两类；纠错转写无 diarization，
  由 LLM 按上下文判断（提示词B已含此指令）。
- **语气词清单**：提示词B自带（啊、呃、嗯、那个、就是、然后呢 等）。

## 与现有实现的三个契约冲突及处置

1. **A 的 time_range vs 现有"模型无权时间戳"**：A 原文不改；拼装层要求
   time_range 取自分段边界时间，程序解析后**吸附到最近分段边界**（带容差
   校验），权威毫秒永远由分段重算——模型时间仅是定位线索。
2. **A 要求全覆盖时间轴 vs 现有"不要求覆盖全片"**：按提示词A执行，改为
   全覆盖契约；程序校验无重叠无缺口，偏差记 warning 并程序侧补齐。
3. **A 的时长/数量规则（60~180s、5~10条、最低40s）vs 现有柔性目标**
   （120~180优选、上限12、min 30s）：按提示词A执行，常量同步更新。

## 另注意

- ~~A 原文写死"约 29 分钟"~~ **已解决（2026-11-05 用户确认"29分钟"是示例）**：
  原文泛化为「视频实际时长以输入说明为准」（拼装层在用户提示给出实际时长），
  归档件与代码常量同步更新，PLAN_PROMPT_VERSION → slice-a2，防混单测 43 条全过。

## 实施清单

1. [x] clip_planning：换提示词A + 新输出 schema（video_summary/slices/discarded_segments）+ time_range 解析吸附 + 全覆盖校验 + 常量更新（2026-11-05：`_PLAN_SYSTEM_PROMPT`=提示词A逐字；分层窗口机制退役，全局单次规划 + 失败重试一次；`_SlicePlanOut` 新 schema；吸附容差 SLICE_SNAP_TOLERANCE_MS=2.5s；60~180s/5~10条/最低40s 常量化；plan_source/lineage 逐切片标注；PLAN_SCHEMA_VERSION→clip-plan/v3）
2. [x] 新建 src/clip_refinement.py：逐切片原始SRT → 提示词B → 四段解析 → cue↔segment 对齐 → 逐条失败回退原转写 + warning（2026-11-05：四段标题切块解析；cue 按最长重叠挂段；切片相对时间码程序平移；>15字只留痕不改写；artifact=plans/<job_id>/clip_refinement.json；plan_fingerprint 供恢复复用）
3. [x] worker：规划后插入精修阶段（refining 状态，jobs.py 白名单已加），artifact 落盘并登记 kind=clip_refinement，plan_ready 后移到两步完成（stop_after=plan、正式链、reedit 链三处接线；同指纹同 generation 直接复用不重调模型）
4. [x] 防混单测：A/B 提示词逐字节比对归档原文 + 两阶段 system prompt 互斥捕获断言 + schema 分离 + 吸附/覆盖/回退测试（tests/unit/test_clip_refinement.py 19 条；test_clip_planning.py / test_w06_planning_wiring.py 已更新为全局规划接线）
5. [x] 文案导出精修版：GET /api/studio/auto-edit/{task_id}/clips/{clip_id}/refined-copy.md（精修缺失/fallback 时如实回退原转写并标注）
6. [ ] 前端右侧面板显示精修转写（单独一轮）
7. [x] 成本提示：第二步新增 ≈ 切片数次调用（每片约2–4k token）；配额记账走 llm_budget stage=clip_refinement，任务详情 llm_usage 可见

### 实施注意（给后续窗口）

- ~~A 原文「约 29 分钟」未泛化~~ 已泛化（2026-11-05 用户确认是示例）：归档件与 `_PLAN_SYSTEM_PROMPT` 同步改为「视频实际时长以输入说明为准」；拼装层用户提示继续给出实际时长（`视频实际时长 HH:MM:SS（约 N 分钟）`）。
- 第二步**不参与**烧录字幕：render 仍用原纠错转写（subtitle-burn-convention 不受影响）；精修产物仅供右侧面板（待实施）与文案导出消费。
- 弃用类型约定：提示词A弃用段程序记录 type=弃用；与已选切片重叠的矛盾弃用被剔除；缺口补齐 type=未覆盖（全覆盖契约）。
- 旧分层窗口机制（hierarchical_planning 拆窗、mixed 来源、_PlanOut schema）已随第一步退役；hierarchical_planning.py 模块文件保留未动（hp.HELPER_VERSION 仍进 provenance）。

## 关联文档

- docs/subtitle-burn-convention.md（烧录契约——本方案不动烧录字幕）
