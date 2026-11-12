-- 2026-11-12 热点库增加检索关键词列（记录该视频在抖音检索时使用的搜索词条）
ALTER TABLE hot_topics ADD COLUMN IF NOT EXISTS search_keyword VARCHAR(100) NOT NULL DEFAULT '';
