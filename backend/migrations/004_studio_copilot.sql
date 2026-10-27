-- Studio Copilot（执行型 AI 剪辑助手）持久化
-- 对话记录与动作日志按 project_id（timeline_<hex>）隔离；
-- 只存文本与元数据，视频/封面等二进制仍走文件系统 + artifact 端点。

CREATE TABLE IF NOT EXISTS studio_copilot_messages (
    id VARCHAR(36) PRIMARY KEY,
    project_id VARCHAR(64) NOT NULL,
    role VARCHAR(16) NOT NULL,           -- user | assistant
    content TEXT NOT NULL,
    engine VARCHAR(16),                  -- llm | rules
    actions JSONB,                       -- 助手返回的动作数组（含 execution 标记）
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_copilot_messages_project
    ON studio_copilot_messages (project_id, created_at);

CREATE TABLE IF NOT EXISTS studio_copilot_actions (
    id VARCHAR(36) PRIMARY KEY,
    project_id VARCHAR(64) NOT NULL,
    message_id VARCHAR(36),
    action_type VARCHAR(48) NOT NULL,    -- 白名单动作类型
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(16) NOT NULL,         -- run | confirm | executed | failed | dismissed
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_copilot_actions_project
    ON studio_copilot_actions (project_id, created_at);
