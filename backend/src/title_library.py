"""标题库（video_titles）：热点标题双标签（钩子类型 + 病种主题）同步与检索支撑。

- 数据来源：``hot_topics`` 中带钩子类型标签（热点型/流量型/信任型/品牌型/收藏型/问答型）的条目；
- 标签1（hook_tag）取自热点条目 ``tags[0]``；
- 标签2（topic_tags，病种）来自 ``backend/seed/title_library_topic_tags.json`` 人工标注，
  未标注的新标题先以空数组同步，标注后重跑同步即可补齐；
- ``id`` 直接复用 ``hot_topics.id``，同步幂等（ON CONFLICT DO UPDATE）；
- 检索走 ``GET /api/v1/titles/search``（ingest_kb.py）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SEED_PATH = Path(__file__).resolve().parent.parent / "seed" / "title_library_topic_tags.json"

HOOK_TAGS = ("热点型", "流量型", "信任型", "品牌型", "收藏型", "问答型")

UPSERT = text("""
    INSERT INTO video_titles (id, title, hook_tag, topic_tags, heat_score, content_hash)
    VALUES (:id, :title, :hook_tag, CAST(:topic_tags AS text[]), :heat_score, :content_hash)
    ON CONFLICT (id) DO UPDATE SET
        title = EXCLUDED.title,
        hook_tag = EXCLUDED.hook_tag,
        topic_tags = EXCLUDED.topic_tags,
        heat_score = EXCLUDED.heat_score,
        content_hash = EXCLUDED.content_hash,
        updated_at = now()
    RETURNING (xmax = 0) AS inserted
""")


def load_topic_tags(path: Path | None = None) -> dict[str, list[str]]:
    payload = json.loads((path or SEED_PATH).read_text(encoding="utf-8"))
    return {str(key): [str(tag) for tag in value]
            for key, value in payload.get("topic_tags", {}).items()}


def title_hash(title: str, hook_tag: str) -> str:
    norm = "".join(ch for ch in f"{title}|{hook_tag}".lower() if ch.isalnum())
    return hashlib.sha256(norm.encode()).hexdigest()


def build_rows(rows: Iterable[Any], mapping: dict[str, list[str]]) -> list[dict[str, Any]]:
    """hot_topics 行 → video_titles 行；缺标注的标题保持空病种标签。"""
    out: list[dict[str, Any]] = []
    for row in rows:
        data = row._mapping if hasattr(row, "_mapping") else row
        title = str(data.get("title") or "").strip()
        if not title:
            continue
        tags = [str(tag) for tag in (data.get("tags") or [])]
        hook_tag = tags[0] if tags and tags[0] in HOOK_TAGS else ""
        entry_id = str(data.get("id") or "")
        out.append({
            "id": entry_id,
            "title": title,
            "hook_tag": hook_tag,
            "topic_tags": list(mapping.get(entry_id, [])),
            "heat_score": int(data.get("heat_score") or 0),
            "content_hash": title_hash(title, hook_tag),
        })
    return out


async def sync_title_library(db: AsyncSession, *, mapping_path: Path | None = None) -> dict[str, int]:
    """全量幂等同步钩子类型热点标题到标题库；返回插入/更新计数。"""
    mapping = load_topic_tags(mapping_path)
    rows = (await db.execute(text(
        "SELECT id, title, tags, heat_score FROM hot_topics "
        "WHERE status <> 'disabled' AND tags && CAST(:tags AS text[])"),
        {"tags": list(HOOK_TAGS)})).fetchall()
    payloads = build_rows(rows, mapping)
    inserted = updated = 0
    for values in payloads:
        result = await db.execute(UPSERT, values)
        row = result.first()
        if row is not None and row.inserted:
            inserted += 1
        else:
            updated += 1
    await db.commit()
    return {"synced": len(payloads), "inserted": inserted, "updated": updated}
