-- 2026-10-30 热点库（阶段一，无向量列；阶段二迁移补 embedding）
CREATE TABLE IF NOT EXISTS hot_topics (
    id              VARCHAR(36) PRIMARY KEY,
    title           VARCHAR(300) NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    raw_text        TEXT NOT NULL DEFAULT '',
    source_platform VARCHAR(30) NOT NULL DEFAULT 'douyin',
    source_url      VARCHAR(1000) NOT NULL DEFAULT '',
    author          VARCHAR(200) NOT NULL DEFAULT '',
    published_at    TIMESTAMPTZ,
    heat_score      INTEGER NOT NULL DEFAULT 0,
    tags            TEXT[] NOT NULL DEFAULT '{}',
    content_hash    VARCHAR(64) NOT NULL UNIQUE,
    status          VARCHAR(20) NOT NULL DEFAULT 'new',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_topics_tags ON hot_topics USING gin (tags);
CREATE INDEX IF NOT EXISTS idx_topics_status ON hot_topics (status);
CREATE INDEX IF NOT EXISTS idx_topics_created ON hot_topics (created_at DESC);
CREATE OR REPLACE TRIGGER update_hot_topics_updated_at BEFORE UPDATE ON hot_topics
  FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
