-- 三库上下文运行时依赖：统一补齐热点检索字段与医学指南表。
ALTER TABLE hot_topics ADD COLUMN IF NOT EXISTS search_keyword VARCHAR(100) NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS medical_guidelines (
    id              VARCHAR(64) PRIMARY KEY,
    topic_path      TEXT[] NOT NULL DEFAULT '{}',
    entry_type      VARCHAR(32) NOT NULL,
    statement       TEXT NOT NULL,
    prerequisites   TEXT NOT NULL DEFAULT '',
    applicability   TEXT NOT NULL DEFAULT '',
    risk_boundary   TEXT NOT NULL DEFAULT '',
    audience_note   TEXT NOT NULL DEFAULT '',
    keywords        TEXT[] NOT NULL DEFAULT '{}',
    source_name     VARCHAR(200) NOT NULL DEFAULT '',
    source_url      TEXT NOT NULL DEFAULT '',
    evidence_level  VARCHAR(60) NOT NULL DEFAULT '',
    lang            VARCHAR(8) NOT NULL DEFAULT 'zh',
    status          VARCHAR(16) NOT NULL DEFAULT 'approved',
    review_status   VARCHAR(24) NOT NULL DEFAULT 'pending_review',
    reviewed_by     VARCHAR(36),
    reviewed_at     TIMESTAMPTZ,
    content_hash    VARCHAR(64) NOT NULL UNIQUE,
    embedding_json  JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_medical_guidelines_status
    ON medical_guidelines (status, review_status);
CREATE INDEX IF NOT EXISTS idx_medical_guidelines_entry_type
    ON medical_guidelines (entry_type);
