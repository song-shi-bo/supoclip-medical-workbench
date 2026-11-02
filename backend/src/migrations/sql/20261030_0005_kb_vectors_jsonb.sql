-- 2026-10-30 双库向量（阶段二，无扩展方案）
-- pgvector 在用户态 zonky PG 不可用（pg_available_extensions 无 vector），
-- 向量以 JSON 数组存 JSONB，检索时 Python(numpy) 内存余弦；
-- 数据量 >5 万行时再评估迁移 pgvector。
ALTER TABLE hot_topics ADD COLUMN IF NOT EXISTS embedding_json JSONB;
ALTER TABLE scripts    ADD COLUMN IF NOT EXISTS embedding_json JSONB;
COMMENT ON COLUMN hot_topics.embedding_json IS 'fastembed bge-small-zh-v1.5 (512维) JSON数组';
COMMENT ON COLUMN scripts.embedding_json    IS 'fastembed bge-small-zh-v1.5 (512维) JSON数组';
