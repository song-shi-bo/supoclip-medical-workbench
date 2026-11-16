-- 标题库（2026-11-16）：热点视频标题 + 双标签（标签1=钩子类型，标签2=病种主题）。
-- id 直接复用 hot_topics.id（1:1 同步、幂等 upsert）；病种标签由人工/模型标注后
-- 经 scripts/seed_title_library.py 写入，检索走 /api/v1/titles/search。
CREATE TABLE IF NOT EXISTS video_titles (
    id             VARCHAR(36) PRIMARY KEY,
    title          TEXT NOT NULL,
    hook_tag       VARCHAR(32) NOT NULL DEFAULT '',
    topic_tags     TEXT[] NOT NULL DEFAULT '{}',
    heat_score     INTEGER NOT NULL DEFAULT 0,
    status         VARCHAR(16) NOT NULL DEFAULT 'active',
    content_hash   VARCHAR(64) NOT NULL UNIQUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_video_titles_hook_tag ON video_titles (hook_tag);
CREATE INDEX IF NOT EXISTS idx_video_titles_topic_tags ON video_titles USING gin (topic_tags);
CREATE OR REPLACE TRIGGER update_video_titles_updated_at BEFORE UPDATE ON video_titles
  FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
