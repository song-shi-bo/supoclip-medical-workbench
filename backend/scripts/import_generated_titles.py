"""把项目台账里的成片标题批量导入标题库（手动执行，不挂自动剪辑链）。

标签1（钩子类型）：按六类原型自动判类（本地嵌入 + 词法回退）；
标签2（病种）：病种词表别名命中/相似度自动匹配；
人工纠错写在 backend/seed/title_library_generated.json 的 overrides，优先于自动结果。

用法（backend/ 下）：.venv/bin/python scripts/import_generated_titles.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.database import AsyncSessionLocal  # noqa: E402
from src.title_library import import_generated_titles  # noqa: E402


async def main() -> None:
    async with AsyncSessionLocal() as db:
        result = await import_generated_titles(db)
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
