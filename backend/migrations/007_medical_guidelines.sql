-- 007_medical_guidelines.sql — 医学指南库（第三库）· 切片医学闸门（2026-11-10）
-- 条目级结构化指南/患教知识；闸门（clip_medical_gate）阶段一消费文件快照，
-- 本表承载条目入库、检索 API 与医生事后复核（review_status）。
-- 与 hot_topics/scripts 双库同风格：embedding_json 可空（fastembed 降级不影响入库）。

CREATE TABLE IF NOT EXISTS medical_guidelines (
    id              VARCHAR(64) PRIMARY KEY,
    topic_path      text[]       NOT NULL DEFAULT '{}',
    entry_type      VARCHAR(32)  NOT NULL,
    statement       TEXT         NOT NULL,
    prerequisites   TEXT         NOT NULL DEFAULT '',
    applicability   TEXT         NOT NULL DEFAULT '',
    risk_boundary   TEXT         NOT NULL DEFAULT '',
    audience_note   TEXT         NOT NULL DEFAULT '',
    keywords        text[]       NOT NULL DEFAULT '{}',
    source_name     VARCHAR(200) NOT NULL DEFAULT '',
    source_url      TEXT         NOT NULL DEFAULT '',
    evidence_level  VARCHAR(60)  NOT NULL DEFAULT '',
    lang            VARCHAR(8)   NOT NULL DEFAULT 'zh',
    -- approved=参与闸门匹配；disabled=只留档。用户 2026-11-10 拍板：直接生效 + 事后复核。
    status          VARCHAR(16)  NOT NULL DEFAULT 'approved',
    review_status   VARCHAR(24)  NOT NULL DEFAULT 'pending_review',
    reviewed_by     VARCHAR(36),
    reviewed_at     timestamptz,
    content_hash    VARCHAR(64)  NOT NULL UNIQUE,
    embedding_json  jsonb,
    created_at      timestamptz  NOT NULL DEFAULT now(),
    updated_at      timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_medical_guidelines_status
    ON medical_guidelines (status, review_status);
CREATE INDEX IF NOT EXISTS idx_medical_guidelines_entry_type
    ON medical_guidelines (entry_type);
