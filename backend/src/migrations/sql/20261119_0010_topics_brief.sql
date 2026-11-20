-- 选题卡（阶段3 M1）：hot_topics 升格为选题卡载体，scripts 增加 origin 身份列
-- 约定：scripts.topic_id 外键已指向 hot_topics，选题卡不建新表，直接在本表升格
-- brief 存展示型字段（目标人群/内容类型/建议钩子/建议结构/建议CTA/业务目标等）
-- core_conclusion / risk_boundary / prerequisites / forbidden_expr 单列，供生成管线与硬校验精确取用
-- 状态机扩展（status 列沿用）：
--   new（入库默认） → briefed（拆解完成，待医生审批） → doctor_approved（医学审批通过）
--   → scheduled（已生成拍摄文稿） → used（素材已上传/已有成片） → archived（弃用）

ALTER TABLE hot_topics ADD COLUMN IF NOT EXISTS brief JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE hot_topics ADD COLUMN IF NOT EXISTS core_conclusion TEXT NOT NULL DEFAULT '';
ALTER TABLE hot_topics ADD COLUMN IF NOT EXISTS risk_boundary TEXT NOT NULL DEFAULT '';
ALTER TABLE hot_topics ADD COLUMN IF NOT EXISTS prerequisites TEXT[] NOT NULL DEFAULT '{}'::text[];
ALTER TABLE hot_topics ADD COLUMN IF NOT EXISTS forbidden_expr TEXT[] NOT NULL DEFAULT '{}'::text[];
ALTER TABLE hot_topics ADD COLUMN IF NOT EXISTS approved_by VARCHAR(64);
ALTER TABLE hot_topics ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;

-- scripts.origin：文稿身份列。transcript_reference = 热点视频转写参考稿（豆包 ingest 通道）；
-- shooting_script = 按选题卡生成的医生原创拍摄稿（工作台内部通道）。写入通道决定身份，不由内容声明
ALTER TABLE scripts ADD COLUMN IF NOT EXISTS origin VARCHAR(30) NOT NULL DEFAULT 'transcript_reference';

-- 医生审批白名单：审批端点校验 user_id 必须在表内（users 表无角色字段，白名单即医生身份）
CREATE TABLE IF NOT EXISTS topic_reviewers (
    user_id VARCHAR(64) PRIMARY KEY,
    created_by VARCHAR(64) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scripts_origin ON scripts (origin);
