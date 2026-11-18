"""对热点库/文稿库/标题库存量做 D2 内容意图（患者决策链）规则打标。

用法（backend/ 下）：.venv/bin/python scripts/tag_content_dimensions.py
人工标签（tagged_by='manual'）不会被覆盖；重跑只刷新规则标签。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.content_tags import tagged_intents_summary, tag_intent_all_libraries  # noqa: E402
from src.database import AsyncSessionLocal  # noqa: E402


async def main() -> None:
    async with AsyncSessionLocal() as db:
        result = await tag_intent_all_libraries(db)
        summary = await tagged_intents_summary(db)
    print(json.dumps({"result": result, "by_library": summary}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
