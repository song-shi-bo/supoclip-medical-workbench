-- 阶段5·点1 统一事实源（2026-11-22）
-- 1) project_manifests：clips.json 的 PG 权威镜像。
--    save_manifest 磁盘原子写成功后写穿镜像（best-effort 可降级）；
--    磁盘缺失/损坏时 load_manifest 从本表修复回盘，clips.json 降级为可重建视图。
-- 2) studio_artifacts：revision 修订号 + overwritten_at 覆盖留痕。
--    同 (job, kind, clip) 内新 object_key 单调 revision；同 key 内容变化视为
--    同址覆盖，显式刷新 size/sha 并留痕，不再对台账静默。
-- 执行约定：本文件由 database.init_db 按「分号」拆句逐条执行——包括注释在内的
-- 任何位置都不允许出现分号字符（2026-11-22 首版即在注释里误用导致拆句错位）。

CREATE TABLE IF NOT EXISTS project_manifests (
    project_id VARCHAR(64) PRIMARY KEY,
    manifest JSONB NOT NULL,
    generation BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_project_manifests_updated
    ON project_manifests (updated_at);

ALTER TABLE studio_artifacts
    ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 1;

ALTER TABLE studio_artifacts
    ADD COLUMN IF NOT EXISTS overwritten_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_studio_artifacts_clip_rev
    ON studio_artifacts (job_id, kind, clip_id);
