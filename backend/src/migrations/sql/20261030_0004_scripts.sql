-- 2026-10-30 文案库（阶段一，无向量列；阶段二迁移补 embedding）
CREATE TABLE IF NOT EXISTS scripts (
    id             VARCHAR(36) PRIMARY KEY,
    topic_id       VARCHAR(36) REFERENCES hot_topics(id) ON DELETE SET NULL,
    version        INTEGER NOT NULL DEFAULT 1,
    title          VARCHAR(300) NOT NULL,
    hook           TEXT NOT NULL DEFAULT '',
    body           TEXT NOT NULL DEFAULT '',
    cta            TEXT NOT NULL DEFAULT '',
    shooting_notes TEXT NOT NULL DEFAULT '',
    refs           TEXT[] NOT NULL DEFAULT '{}',
    model          VARCHAR(100) NOT NULL DEFAULT '',
    generated_by   VARCHAR(30) NOT NULL DEFAULT 'doubao-local',
    content_hash   VARCHAR(64) NOT NULL UNIQUE,
    status         VARCHAR(20) NOT NULL DEFAULT 'draft',
    task_id        VARCHAR(64),
    created_by     VARCHAR(36),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_scripts_status ON scripts (status);
CREATE INDEX IF NOT EXISTS idx_scripts_topic ON scripts (topic_id);
CREATE INDEX IF NOT EXISTS idx_scripts_task ON scripts (task_id);
CREATE OR REPLACE TRIGGER update_scripts_updated_at BEFORE UPDATE ON scripts
  FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
