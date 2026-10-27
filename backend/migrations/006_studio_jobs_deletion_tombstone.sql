-- 006: 项目删除的可审计墓碑（studio_jobs）
-- 已删除项目的 studio_jobs 行不物理删除：打 deleted_at_utc/deleted_via 墓碑，保留完整审计链。
-- claim/重试路径以 deleted_at_utc IS NULL 过滤，墓碑行永不复活、永不回填。

ALTER TABLE studio_jobs ADD COLUMN IF NOT EXISTS deleted_at_utc timestamptz;
ALTER TABLE studio_jobs ADD COLUMN IF NOT EXISTS deleted_via text;

CREATE INDEX IF NOT EXISTS idx_studio_jobs_deletion_tombstone
    ON studio_jobs(project_id) WHERE deleted_at_utc IS NOT NULL;
