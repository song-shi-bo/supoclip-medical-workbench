-- 标题库扩展（2026-11-16）：支持导入工作台剪辑生成的成片标题。
-- source 区分来源：hot_topic=热点库同步，generated=剪辑成片导入（手动批量）；
-- project_id 记录成片所属项目，便于溯源。旧数据按默认值归为 hot_topic。
ALTER TABLE video_titles ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'hot_topic';
ALTER TABLE video_titles ADD COLUMN IF NOT EXISTS project_id VARCHAR(64) NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_video_titles_source ON video_titles (source);
