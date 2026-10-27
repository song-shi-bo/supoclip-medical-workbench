-- 2026-10-16 Step 4 状态契约补齐：uploading_reedit_artifacts（27 字符）超出
-- studio_jobs.status VARCHAR(24) 上限，导致发布阶段状态更新 500。
-- 放宽到 48，容纳全部 ALLOWED_JOB_STATUSES 成员（最长 completed_with_cleanup_warning = 32）。
ALTER TABLE studio_jobs ALTER COLUMN status TYPE VARCHAR(48);
