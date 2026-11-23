-- 阶段5·点4 可观测性（2026-11-23）
-- 四张表：任务事件流水（状态/阶段变更埋点）、分钟级指标聚合、worker 心跳注册、
-- artifact 哈希巡检台账。全部由 studio_observability.py 读写；埋点 best-effort
-- 可降级，观测失败绝不阻断主链路。
-- 执行约定：本文件由 database.init_db 按「分号」拆句逐条执行——包括注释在内的
-- 任何位置都不允许出现分号字符（2026-11-22 首版教训）。

CREATE TABLE IF NOT EXISTS studio_job_events (
    id VARCHAR(36) PRIMARY KEY,
    job_id VARCHAR(36) NOT NULL,
    queue VARCHAR(16) NOT NULL DEFAULT 'render',
    event VARCHAR(24) NOT NULL DEFAULT 'status_change',
    from_status VARCHAR(24),
    to_status VARCHAR(24) NOT NULL,
    stage VARCHAR(120) NOT NULL DEFAULT '',
    progress REAL,
    worker_id VARCHAR(64) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_studio_job_events_job
    ON studio_job_events (job_id, created_at);

CREATE INDEX IF NOT EXISTS idx_studio_job_events_time
    ON studio_job_events (created_at);

CREATE TABLE IF NOT EXISTS job_metrics_minute (
    window_start TIMESTAMPTZ NOT NULL,
    queue VARCHAR(16) NOT NULL DEFAULT 'render',
    metrics JSONB NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (window_start, queue)
);

CREATE TABLE IF NOT EXISTS studio_worker_registry (
    worker_id VARCHAR(64) PRIMARY KEY,
    queues VARCHAR(120) NOT NULL DEFAULT '',
    first_seen TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_job_id VARCHAR(36)
);

CREATE TABLE IF NOT EXISTS studio_artifact_hash_audits (
    id VARCHAR(36) PRIMARY KEY,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sample_size INT NOT NULL,
    mismatch_count INT NOT NULL,
    missing_count INT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);