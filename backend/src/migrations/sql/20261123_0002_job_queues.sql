-- 阶段5·点3 Worker 资源型调度（2026-11-23）
-- studio_jobs 增加 queue 列：按资源分类的轻量队列标签（PG 租约/fencing 机制不变）。
-- queue 取值：asr（转写，火山 ASR 有账号级 QPS）、render（自动剪辑/重剪，CPU 密集）、
-- llm / cover / analytics 为规划保留位（cover 走进程内局部任务，不经 studio_jobs）。
-- 存量回填：transcribe_only → asr，其余 → render。
-- 执行约定：本文件由 database.init_db 按「分号」拆句逐条执行——包括注释在内的
-- 任何位置都不允许出现分号字符。

ALTER TABLE studio_jobs
    ADD COLUMN IF NOT EXISTS queue VARCHAR(16) NOT NULL DEFAULT 'render';

UPDATE studio_jobs
    SET queue = 'asr'
    WHERE config#>>'{job_kind}' = 'transcribe_only' AND queue = 'render';

CREATE INDEX IF NOT EXISTS idx_studio_jobs_queue_status
    ON studio_jobs (queue, status, created_at);
