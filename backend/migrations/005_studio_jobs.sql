-- Studio Job 管线（R2 直传 + 任务化转写/剪辑）
-- 源视频与产物二进制存 R2，数据库只存元数据与 object_key。

CREATE TABLE IF NOT EXISTS studio_jobs (
    id VARCHAR(36) PRIMARY KEY,
    project_id VARCHAR(64) NOT NULL,
    kind VARCHAR(24) NOT NULL DEFAULT 'transcribe_autoedit',
    status VARCHAR(24) NOT NULL DEFAULT 'queued',   -- queued|downloading|transcribing|correcting|analyzing|editing|rendering|uploading|completed|failed
    stage VARCHAR(120) NOT NULL DEFAULT '',
    progress REAL NOT NULL DEFAULT 0,
    object_key VARCHAR(512) NOT NULL,               -- 源视频存储 key（R2）或本地路径标记
    object_size BIGINT,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT,
    worker_id VARCHAR(64),
    lease_expires_at TIMESTAMPTZ,
    attempts SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_studio_jobs_project ON studio_jobs (project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_studio_jobs_status ON studio_jobs (status);

CREATE TABLE IF NOT EXISTS studio_upload_sessions (
    id VARCHAR(36) PRIMARY KEY,
    project_id VARCHAR(64) NOT NULL,
    object_key VARCHAR(512) NOT NULL UNIQUE,
    filename VARCHAR(255),
    size_bytes BIGINT,
    part_size BIGINT NOT NULL,
    parts_total INT NOT NULL,
    r2_upload_id VARCHAR(256),
    status VARCHAR(16) NOT NULL DEFAULT 'open',     -- open | completed | aborted
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS studio_artifacts (
    id VARCHAR(36) PRIMARY KEY,
    job_id VARCHAR(36) NOT NULL REFERENCES studio_jobs(id),
    kind VARCHAR(32) NOT NULL,                      -- transcript_json|srt|edl|fcpxml|cover|clip
    clip_id VARCHAR(64),
    object_key VARCHAR(512) NOT NULL,
    size_bytes BIGINT,
    sha256 VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_studio_artifacts_job ON studio_artifacts (job_id);
