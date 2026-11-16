"""标题库同步 CLI：把热点标题（含钩子类型 + 病种双标签）幂等同步到 video_titles。

用法（backend/ 下）：
    .venv/bin/python scripts/seed_title_library.py
标注新病种标签后重跑即可更新；表结构与迁移 20261116_0007_title_library.sql。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.database import AsyncSessionLocal  # noqa: E402
from src.title_library import sync_title_library  # noqa: E402


async def main() -> None:
    async with AsyncSessionLocal() as db:
        result = await sync_title_library(db)
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
