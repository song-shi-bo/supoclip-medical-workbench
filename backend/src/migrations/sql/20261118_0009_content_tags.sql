-- 通用多维度内容标签表（2026-11-18）：热点库/文稿库/标题库统一挂标签。
-- dimension 维度词表：intent(内容意图-患者决策链) / topic(病种) / audience(受众) /
-- format(形式) / style(钩子风格) / mechanism(钩子机制)；tag 取各维度受控词表。
-- tagged_by 区分来源：rule(规则自动) / model(模型) / manual(人工，规则重跑不覆盖)。
CREATE TABLE IF NOT EXISTS content_tags (
    id            BIGSERIAL PRIMARY KEY,
    object_type   VARCHAR(24) NOT NULL,
    object_id     VARCHAR(64) NOT NULL,
    dimension     VARCHAR(24) NOT NULL,
    tag           VARCHAR(64) NOT NULL,
    tagged_by     VARCHAR(16) NOT NULL DEFAULT 'rule',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (object_type, object_id, dimension, tag)
);

CREATE INDEX IF NOT EXISTS idx_content_tags_dim_tag ON content_tags (dimension, tag);
CREATE INDEX IF NOT EXISTS idx_content_tags_object ON content_tags (object_type, object_id);
