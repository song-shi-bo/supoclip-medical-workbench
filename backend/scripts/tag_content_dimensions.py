"""对热点库/文稿库/标题库存量做多维度规则打标（D2 意图 / D4 受众 / D5 形式）。

用法（backend/ 下）：.venv/bin/python scripts/tag_content_dimensions.py
人工标签（tagged_by='manual'）不会被覆盖；重跑只刷新规则标签。
同时清理标题库 topic_tags 里的维度混装（就医指南 → intent 维度）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.content_tags import (  # noqa: E402
    normalize_video_title_topic_tags,
    tagged_intents_summary,
    tag_audience_format_all_libraries,
    tag_intent_all_libraries,
)
from src.database import AsyncSessionLocal  # noqa: E402


async def main() -> None:
    async with AsyncSessionLocal() as db:
        intent = await tag_intent_all_libraries(db)
        audience_format = await tag_audience_format_all_libraries(db)
        normalize = await normalize_video_title_topic_tags(db)
        summary = await tagged_intents_summary(db)
    print(json.dumps({"intent": intent, "audience_format": audience_format,
                      "normalize": normalize, "by_library_intent": summary},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
