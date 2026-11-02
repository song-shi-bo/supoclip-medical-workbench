"""热点库/文案库：豆包本地入库 + 只读检索 + 医生确认（2026-10-30 双库阶段一）。

鉴权模型：
- /api/v1/ingest/* 与 /api/v1/kb/search 为独立 Bearer token（HOTSPOT_INGEST_TOKEN /
  KB_READ_TOKEN，值在 backend/.env.kb），并在 StudioAuthenticationMiddleware 豁免清单中；
- /api/v1/kb/scripts/{id}/confirm 走 Studio 登录态（中间件正常校验），确认人回填 created_by。
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db

load_dotenv(Path(__file__).resolve().parents[3] / ".env.kb")

router = APIRouter(prefix="/api/v1", tags=["kb"])


def _env_token(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise HTTPException(503, f"{name} not configured")
    return value


def _check(request: Request, name: str) -> None:
    got = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    if not got or not secrets.compare_digest(got, _env_token(name)):
        raise HTTPException(401, "bad token")


def _content_hash(title: str) -> str:
    norm = "".join(ch for ch in title.lower() if ch.isalnum())
    return hashlib.sha256(norm.encode()).hexdigest()


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class TopicIn(BaseModel):
    title: str = Field(min_length=2, max_length=300)
    summary: str = ""
    raw_text: str = ""
    source_url: str = ""
    author: str = ""
    published_at: Optional[str] = None
    heat_score: int = 0
    tags: list[str] = Field(default_factory=list, max_length=10)


class HotTopicsIn(BaseModel):
    collected_at: Optional[str] = None
    collector: str = "doubao-local"
    topics: list[TopicIn] = Field(min_length=1, max_length=200)


class ScriptIn(BaseModel):
    title: str = Field(min_length=2, max_length=300)
    hook: str = ""
    body: str = Field(min_length=1)
    cta: str = ""
    shooting_notes: str = ""
    refs: list[str] = Field(default_factory=list)
    generated_by: str = "doubao-local"
    model: str = ""
    topic_ref: str = ""


class ScriptsIn(BaseModel):
    scripts: list[ScriptIn] = Field(min_length=1, max_length=50)


INSERT_TOPIC = text("""
    INSERT INTO hot_topics (id, title, summary, raw_text, source_platform, source_url,
                            author, published_at, heat_score, tags, content_hash)
    VALUES (gen_random_uuid()::text, :title, :summary, :raw_text, 'douyin', :source_url,
            :author, :published_at, :heat_score, CAST(:tags AS text[]), :content_hash)
    ON CONFLICT (content_hash) DO NOTHING
""")

INSERT_SCRIPT = text("""
    INSERT INTO scripts (id, topic_id, title, hook, body, cta, shooting_notes, refs,
                         model, generated_by, content_hash)
    VALUES (gen_random_uuid()::text, :topic_id, :title, :hook, :body, :cta,
            :shooting_notes, CAST(:refs AS text[]), :model, :generated_by, :content_hash)
    ON CONFLICT (content_hash) DO NOTHING
""")

FIND_TOPIC = text("""
    SELECT id FROM hot_topics
    WHERE (:hash <> '' AND content_hash = :hash) OR (:like <> '%%' AND title ILIKE :like)
    ORDER BY (content_hash = :hash) DESC, created_at DESC LIMIT 1
""")


@router.post("/ingest/hot-topics")
async def ingest_hot_topics(payload: HotTopicsIn, request: Request,
                            db: AsyncSession = Depends(get_db)):
    _check(request, "HOTSPOT_INGEST_TOKEN")
    inserted = deduped = 0
    for t in payload.topics:
        row = await db.execute(INSERT_TOPIC, {
            "title": t.title.strip(), "summary": t.summary, "raw_text": t.raw_text,
            "source_url": t.source_url, "author": t.author,
            "published_at": _parse_ts(t.published_at),
            "heat_score": t.heat_score, "tags": t.tags,
            "content_hash": _content_hash(t.title),
        })
        inserted += row.rowcount
        deduped += 1 - row.rowcount
    await db.commit()
    return {"inserted": inserted, "deduped": deduped}


@router.post("/ingest/scripts")
async def ingest_scripts(payload: ScriptsIn, request: Request,
                         db: AsyncSession = Depends(get_db)):
    _check(request, "HOTSPOT_INGEST_TOKEN")
    inserted = deduped = 0
    warnings: list[str] = []
    for s in payload.scripts:
        topic_id = None
        ref = s.topic_ref.strip()
        if ref:
            row = (await db.execute(FIND_TOPIC, {
                "hash": ref if len(ref) == 64 else "",
                "like": f"%{ref}%" if ref else ""})).first()
            topic_id = row.id if row else None
            if topic_id is None:
                warnings.append(f"topic_ref 未命中: {ref}")
        row = await db.execute(INSERT_SCRIPT, {
            "topic_id": topic_id, "title": s.title.strip(), "hook": s.hook,
            "body": s.body, "cta": s.cta, "shooting_notes": s.shooting_notes,
            "refs": s.refs, "model": s.model, "generated_by": s.generated_by,
            "content_hash": _content_hash(s.title),
        })
        inserted += row.rowcount
        deduped += 1 - row.rowcount
    await db.commit()
    return {"inserted": inserted, "deduped": deduped, "warnings": warnings}


@router.get("/kb/search")
async def kb_search(request: Request, q: str = "", tag: str = "", status: str = "",
                    table: str = "both", limit: int = Query(10, ge=1, le=50),
                    db: AsyncSession = Depends(get_db)):
    _check(request, "KB_READ_TOKEN")
    like = f"%{q.strip()}%"
    tag_list = [t.strip() for t in tag.split(",") if t.strip()]
    out: dict[str, Any] = {"q": q, "tag": tag_list}
    if table in ("both", "topics"):
        rows = (await db.execute(text("""
            SELECT id, title, summary, tags, heat_score, status, source_url, created_at
            FROM hot_topics
            WHERE (:q = '' OR title ILIKE :like OR summary ILIKE :like OR raw_text ILIKE :like)
              AND (:status = '' OR status = :status)
              AND (cardinality(CAST(:tags AS text[])) = 0 OR tags @> CAST(:tags AS text[]))
            ORDER BY heat_score DESC, created_at DESC LIMIT :limit
        """), {"q": q.strip(), "like": like, "status": status, "tags": tag_list,
               "limit": limit}))
        out["topics"] = [dict(r._mapping) for r in rows]
    if table in ("both", "scripts"):
        rows = (await db.execute(text("""
            SELECT s.id, s.title, s.hook, s.status, s.topic_id, s.model, s.created_at,
                   t.title AS topic_title
            FROM scripts s LEFT JOIN hot_topics t ON t.id = s.topic_id
            WHERE (:q = '' OR s.title ILIKE :like OR s.hook ILIKE :like OR s.body ILIKE :like)
              AND (:status = '' OR s.status = :status)
            ORDER BY s.created_at DESC LIMIT :limit
        """), {"q": q.strip(), "like": like, "status": status, "limit": limit}))
        out["scripts"] = [dict(r._mapping) for r in rows]
    return out


@router.post("/kb/scripts/{script_id}/confirm")
async def confirm_script(script_id: str, request: Request,
                         db: AsyncSession = Depends(get_db)):
    """医生在工作台确认文稿（draft → approved）。走 Studio 登录态，确认人回填 created_by。"""
    principal = getattr(request.state, "studio_principal", None) or {}
    subject = principal.get("subject") or principal.get("user") or ""
    if not subject:
        raise HTTPException(401, "studio session required")
    row = await db.execute(text(
        "UPDATE scripts SET status='approved', created_by=:uid, updated_at=now() "
        "WHERE id=:id AND status='draft' RETURNING id"),
        {"uid": str(subject), "id": script_id})
    await db.commit()
    if row.first() is None:
        raise HTTPException(404, "not found or not in draft")
    return {"ok": True, "id": script_id, "status": "approved"}
