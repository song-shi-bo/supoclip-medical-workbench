-- 2026-10-16 回滚：移除 Step 2/3 数据管理（源视频档案与持久化 purge 任务）。
-- 按用户决定整体下线该功能族；studio_jobs.status 列宽放宽（0003）保留，
-- 它属于重新剪辑播放链路的状态契约修复。
DROP TABLE IF EXISTS studio_purge_jobs;
DROP TABLE IF EXISTS studio_sources;
DROP INDEX IF EXISTS idx_studio_jobs_source_id;
DROP INDEX IF EXISTS idx_studio_artifacts_source_id;
ALTER TABLE studio_jobs DROP COLUMN IF EXISTS source_id;
ALTER TABLE studio_artifacts DROP COLUMN IF EXISTS source_id;
