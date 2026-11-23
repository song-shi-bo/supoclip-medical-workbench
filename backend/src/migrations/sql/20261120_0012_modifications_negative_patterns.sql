-- 修改自动沉淀（阶段3 第6条正规化）：
-- modifications = 医生审稿/审批反馈的病历本（9类归因 + 路由去向 + 处理状态）
-- negative_patterns = 校验器负例模式入库（从代码常量升级为可后台增删的负例库）
-- 约定：topic_pipeline.py 内置 4 条基线红线保持不变；医生反馈沉淀的模式全部入本表，
--       校验时由路由层加载 enabled 行传入，命中不入库的模式不影响基线行为。

CREATE TABLE IF NOT EXISTS modifications (
    id VARCHAR(36) PRIMARY KEY,
    topic_id VARCHAR(36),
    script_id VARCHAR(36),
    reason_class VARCHAR(30) NOT NULL,          -- 9类：asr_error/medical_term/premise_missing/conclusion_rigor/privacy/subtitle_style/slice_boundary/stitching/hook_issue
    note TEXT NOT NULL DEFAULT '',              -- 反馈原文/备注
    payload JSONB NOT NULL DEFAULT '{}'::jsonb, -- 分类专属结构化数据（如 correct/wrong 词对、pattern 正则）
    created_by VARCHAR(64) NOT NULL DEFAULT '',
    routing_target VARCHAR(40) NOT NULL DEFAULT '',   -- medical.tsv / medical_guidelines / negative_patterns / none
    routing_status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- routed / recorded / failed / pending
    routing_detail TEXT NOT NULL DEFAULT '',
    resolved_by VARCHAR(64),
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_modifications_class ON modifications (reason_class);
CREATE INDEX IF NOT EXISTS idx_modifications_created ON modifications (created_at DESC);

CREATE TABLE IF NOT EXISTS negative_patterns (
    id VARCHAR(36) PRIMARY KEY,
    pattern TEXT NOT NULL,                      -- Python 正则
    label TEXT NOT NULL,                        -- 命中提示文案
    severity VARCHAR(10) NOT NULL DEFAULT 'soft',  -- hard=拒绝并重写 / soft=警告提示软化
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    source VARCHAR(20) NOT NULL DEFAULT 'doctor_feedback',
    created_by VARCHAR(64) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_negative_patterns_enabled ON negative_patterns (enabled);

-- 种子：2026-11-20 医生审稿反馈沉淀的 6 条模式（胃癌卡审稿负例，hard=绝对化强度超纲 / soft=承诺化句式）
INSERT INTO negative_patterns (id, pattern, label, severity, source, created_by) VALUES
('np-seed-abs-block',  '完全(阻断|稳住|避免|消除|治愈|防止|杜绝)', '绝对化表述（完全…）：依据只能支撑''降低风险''类口径', 'hard', 'doctor_feedback', 'seed'),
('np-seed-abs-guar',   '(保证|肯定|一定)(能|可以)?(阻断|稳住|避免|治愈|不得)', '绝对化承诺（保证/肯定/一定…）', 'hard', 'doctor_feedback', 'seed'),
('np-seed-abs-thorough', '彻底(治愈|根治|消除|解决)', '绝对化表述（彻底…）', 'hard', 'doctor_feedback', 'seed'),
('np-seed-soft-jiuneng', '就能(控制|阻断|稳住|解决|避免|预防)', '承诺化句式（就能…）：建议改''有机会控制/大多能稳住''', 'soft', 'doctor_feedback', 'seed'),
('np-seed-soft-yeneng',  '也能(阻断|控制|稳住|避免)', '承诺化句式（也能…）：建议改''也有机会/同样有助于''', 'soft', 'doctor_feedback', 'seed'),
('np-seed-soft-block',   '阻断癌变(进程|风险|链)', '''阻断癌变''需核对依据原文口径（指南多写''降低发生风险''）', 'soft', 'doctor_feedback', 'seed')
ON CONFLICT (id) DO NOTHING;
