# 医学背景注入设计稿（小闭环第二步：让规划模型真正"读"到知识点）

状态：**已实施（默认 off）** · 日期：2026-11-13 · 范围：仅规划阶段（提示词A）

## 实施记录与设计偏差（2026-11-13）

- 已实现：`medical_kb.build_background_block/background_config`（白名单字段+预算截断+块首指令）、
  `clip_planning._analysis_context_lines` 注入、`_scan_background_leak`/`_max_common_run`
  泄漏扫描、配置 `MEDICAL_BG_INJECT/MAX_ENTRIES/PER_UNIT/BUDGET/LEAK_THRESHOLD`、
  测试 `tests/unit/test_medical_background_injection.py`（8 项）。
- **偏差 1（简化）**：闸门无需新增 `background_candidates`——unit_annotations 的
  `matched[].id` 本身即候选（每块已按闸门排序取前 N）。
- **偏差 2（简化）**：未新增 provenance 字段——注入块在 `_analysis_context_lines`
  内构建，其源码哈希已在 provenance `program_sha256`、块文本经
  `_slice_plan_prompt` 进入 `prompt_sha256`：注入变化自动改变指纹，expected/actual
  两边对称（确定性纯函数）。A/B 实测 prompt_sha256 off≠on ✓。
- **实测（真实转写）**：注入块 +980 字符（24480→25460，+4%），每个主题块命中
  相关卡片（扩切主题→MEL-022 边界卡、淋巴结答疑→MEL-055 活检规范卡）；对比文件
  `tasks/projects/<id>/medical_bg_prompt_diff.txt`。
- **真模型 A/B 受限**：模型网关凭据仅存在于服务进程环境（不落 .env），临时
  shell 调规划返回 403（Model not allowed for this application）——真模型对比
  需经服务环境内任务（或运维在 .env 追加 `MEDICAL_BG_INJECT=on` 后由真实任务
  触发）。默认 off 下工作台行为零变化。

## 1. 目标与不变式

**目标**：把闸门命中的知识卡片正文（医学结论/适用前提/边界）以受控格式注入规划提示词，
让模型带着医学背景做选片与拼接——即用户定位的"对医学语句有敏感度"。

**不变式（三条铁律，注入后仍然成立）**：
1. 输出（主题标题/钩子/切片边界）仍只能来自转写原文——注入的是"理解力"，不是"素材"；
2. 卡片正文绝不进入字幕/标题/文案输出（字幕只来自精修稿只删不改；钩子已逐字执法兜底）；
3. 系统提示词A五个规则不动，特别是规则5「信息忠实」。

## 2. 与现状的关系

| 现状 | 本设计 |
|---|---|
| 闸门只透出【医学主题匹配】标注行（主题/级别/匹配度） | 标注行保留，另增背景块 |
| `medical_kb.py:11`、`clip_planning.py:684` 显式不注入条目正文 | **受控放开**：按白名单字段、预算、防泄漏框架注入（本设计核心） |
| 系统提示词规则5「不得引入文本外的医学结论」 | 不改系统提示词；在注入块内自带使用说明（见 §3） |

## 3. 注入格式（拼装层第三背景块）

位置：`clip_planning.py` 背景段构建处（:653 起，与【钩子候选】块 :688-703 并列），
提示词A原文不动。格式：

```
【医学背景（仅供理解内容主题与医学逻辑，禁止复制其文字到任何输出；
  主题/钩子/摘要仍只能来自转写原文）】
· 主题「淋巴结肿大患者复查与手部变形情况答疑」相关背景：
  - (MEL-055/国家卫健委2018规范) 活检：常规推荐切除活检（切缘0.3～0.5cm）…避免直接扩大切除。
    边界提示：活检方式由医生按病灶部位选择。
  - (MEL-060/同上) 黑色素瘤治疗须重视多学科诊疗团队（MDT）模式…
```

字段白名单：`statement`（≤120字截断）、`risk_boundary`（≤60字截断）、`entry_id`、
`source_name`（短名）。**不注入**：keywords/audience_note/public_question（省预算）。

## 4. 注入量控制

- 每个主题块取闸门 `matched` 前 **3** 条（按闸门排序）；全片去重后总条数上限 **9**；
- 全块字符预算 **1200**（约 600-800 token），超限按分数从低往高丢条目；
- 闸门未启用/无命中 → 不注入（行为与现状完全一致）。

## 5. 防泄漏护栏（三层）

1. **块内指令**：注入块首行明示"禁止复制其文字到任何输出"（见 §3 格式）；
2. **输出校验（新增，warn 级）**：规划完成后扫描主题标题/钩子/摘要——
   若某输出片段与注入 statement 存在 ≥12 字连续相同子串，记
   `medical_background_leak` warning（不阻断，供人工核查）；
3. **既有兜底**：钩子逐字执法（钩子必须来自转写）；字幕链路只删不改（与规划隔离）。

## 6. 指纹与缓存语义

`build_current_plan_provenance` 新增字段：
`medical_background = {template_version, entry_ids(排序后)}`。
闸门快照 sha256 已在指纹内；注入条目集变化（匹配不同）→ 指纹变 → 旧缓存自动失效
（与现行语义一致，无额外迁移）。

## 7. 配置与降级

| 配置 | 默认 | 说明 |
|---|---|---|
| `MEDICAL_BG_INJECT` | `off` | `on` 注入 / `off` 纯标注（现状）/ 任何非法值按 `off` |
| `MEDICAL_BG_MAX_ENTRIES` | 9 | 全片注入条目上限 |
| `MEDICAL_BG_PER_UNIT` | 3 | 每主题块条目上限 |

失败降级：快照读取失败、字段缺失、预算异常 → 记 warning、退回纯标注模式，
**绝不阻断规划**（与闸门同策略）。`PLAN_PROMPT_VERSION` 不 bump（提示词A未动）；
模板版本单独记录 `medical-bg-tpl/1`。

## 8. 改动点清单（文件级）

| 文件 | 改动 |
|---|---|
| `src/clip_medical_gate.py` | profile 增加 `background_candidates`（每主题块 top3 的 entry id + 分数，不取正文） |
| `src/medical_kb.py` | 新增 `build_background_block(entries, budget)`（白名单字段+截断+首行指令） |
| `src/clip_planning.py` | 背景段构建处接入注入块（§3）；输出泄漏扫描（§5-2）；provenance 新字段（§6） |
| `scripts/` | A/B 对比脚本（§9） |
| 测试 | 单测：on/off、预算截断、防泄漏扫描、降级路径、指纹字段；不用真实 LLM |

## 9. 验证方案（melanoma 项目实测）

1. `MEDICAL_BG_INJECT=off` 跑一次规划（基线，应有缓存可复用或以现有 plan 为基线）；
2. `=on` 跑一次（新 job_id 或清缓存）；
3. 自动对比：主题块标题/切片数/边界差异；泄漏扫描零命中；
4. 人工看两份 `clip_plan.json` 的逐切片标注与 `sub_ranges` 差异，由您判定是否更有医学逻辑；
5. 认可后把默认值切 `on`（或保持 off 由任务配置开启）。

## 10. 明确不做（本设计边界）

- 不动系统提示词A、不动精修（提示词B）/字幕/标题生成链路；
- 不做卡片正文注入到提示词B（精修只删不改，无需医学背景）；
- 不引入新的 LLM 调用（背景注入是纯本地拼装，零额外成本）。
