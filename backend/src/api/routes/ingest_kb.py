"""热点库/文案库：豆包本地入库 + 只读检索 + 医生确认（2026-10-30 双库阶段一）。

鉴权模型：
- /api/v1/ingest/* 与 /api/v1/kb/search 为独立 Bearer token（HOTSPOT_INGEST_TOKEN /
  KB_READ_TOKEN，值在 backend/.env.kb），并在 StudioAuthenticationMiddleware 豁免清单中；
- /api/v1/kb/scripts/{id}/confirm 走 Studio 登录态（中间件正常校验），确认人回填 created_by。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets

import numpy as np

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ... import kb_embedding
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


def _cosine_top_k(query_vec: list[float], rows: list[Any], limit: int):
    """内存余弦排序（量级 <5 万行无需 pgvector）。rows 需含 embedding_json。"""
    query = np.asarray(query_vec, dtype=float)
    scored = []
    for r in rows:
        emb = r.embedding_json
        if isinstance(emb, str):
            try:
                emb = json.loads(emb)
            except ValueError:
                continue
        if not emb:
            continue
        vec = np.asarray(emb, dtype=float)
        denom = float(np.linalg.norm(vec) * np.linalg.norm(query))
        sim = float(vec @ query / denom) if denom else 0.0
        scored.append((sim, r))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[:limit]


async def _embed_missing_topics(db: AsyncSession, hashes: list[str]) -> int:
    """入库后为新增行补向量；失败静默降级（向量缺失不影响入库结果）。"""
    if not hashes:
        return 0
    try:
        rows = (await db.execute(text(
            "SELECT id, title, summary, raw_text FROM hot_topics "
            "WHERE embedding_json IS NULL AND content_hash = ANY(CAST(:hs AS text[]))"),
            {"hs": hashes})).fetchall()
        if not rows:
            return 0
        vectors = kb_embedding.embed_texts(
            [f"{r.title}\n{r.summary}\n{r.raw_text}" for r in rows])
        if not vectors:
            return 0
        for r, vec in zip(rows, vectors):
            await db.execute(text(
                "UPDATE hot_topics SET embedding_json = CAST(:vec AS jsonb) WHERE id = :id"),
                {"vec": json.dumps(vec), "id": r.id})
        await db.commit()
        return len(rows)
    except Exception:
        await db.rollback()
        return 0


async def _embed_missing_scripts(db: AsyncSession, hashes: list[str]) -> int:
    if not hashes:
        return 0
    try:
        rows = (await db.execute(text(
            "SELECT id, title, hook, body FROM scripts "
            "WHERE embedding_json IS NULL AND content_hash = ANY(CAST(:hs AS text[]))"),
            {"hs": hashes})).fetchall()
        if not rows:
            return 0
        vectors = kb_embedding.embed_texts(
            [f"{r.title}\n{r.hook}\n{r.body}" for r in rows])
        if not vectors:
            return 0
        for r, vec in zip(rows, vectors):
            await db.execute(text(
                "UPDATE scripts SET embedding_json = CAST(:vec AS jsonb) WHERE id = :id"),
                {"vec": json.dumps(vec), "id": r.id})
        await db.commit()
        return len(rows)
    except Exception:
        await db.rollback()
        return 0


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
    hashes: list[str] = []
    for t in payload.topics:
        content_hash = _content_hash(t.title)
        row = await db.execute(INSERT_TOPIC, {
            "title": t.title.strip(), "summary": t.summary, "raw_text": t.raw_text,
            "source_url": t.source_url, "author": t.author,
            "published_at": _parse_ts(t.published_at),
            "heat_score": t.heat_score, "tags": t.tags,
            "content_hash": content_hash,
        })
        hashes.append(content_hash)
        inserted += row.rowcount
        deduped += 1 - row.rowcount
    await db.commit()
    embedded = await _embed_missing_topics(db, hashes)
    return {"inserted": inserted, "deduped": deduped, "embedded": embedded}


@router.post("/ingest/scripts")
async def ingest_scripts(payload: ScriptsIn, request: Request,
                         db: AsyncSession = Depends(get_db)):
    _check(request, "HOTSPOT_INGEST_TOKEN")
    inserted = deduped = 0
    warnings: list[str] = []
    hashes: list[str] = []
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
        content_hash = _content_hash(s.title)
        row = await db.execute(INSERT_SCRIPT, {
            "topic_id": topic_id, "title": s.title.strip(), "hook": s.hook,
            "body": s.body, "cta": s.cta, "shooting_notes": s.shooting_notes,
            "refs": s.refs, "model": s.model, "generated_by": s.generated_by,
            "content_hash": content_hash,
        })
        hashes.append(content_hash)
        inserted += row.rowcount
        deduped += 1 - row.rowcount
    await db.commit()
    embedded = await _embed_missing_scripts(db, hashes)
    return {"inserted": inserted, "deduped": deduped, "embedded": embedded,
            "warnings": warnings}


@router.get("/kb/search")
async def kb_search(request: Request, q: str = "", tag: str = "", status: str = "",
                    table: str = "both",
                    mode: str = Query("keyword", pattern="^(keyword|semantic)$"),
                    full: bool = Query(False, description="true=返回文稿全部内容（正文/CTA/拍摄提示/依据）"),
                    limit: int = Query(10, ge=1, le=50),
                    db: AsyncSession = Depends(get_db)):
    _check(request, "KB_READ_TOKEN")
    like = f"%{q.strip()}%"
    tag_list = [t.strip() for t in tag.split(",") if t.strip()]

    # 语义模式：查询文本向量化；模型不可用/查询为空 → 自动降级关键词
    use_semantic = mode == "semantic" and bool(q.strip())
    query_vec = None
    if use_semantic:
        vectors = kb_embedding.embed_texts([q.strip()])
        query_vec = vectors[0] if vectors and vectors[0] else None
        if query_vec is None:
            use_semantic = False
    extra_cols = ", s.body, s.cta, s.shooting_notes, s.refs" if full else ""
    out: dict[str, Any] = {"q": q, "tag": tag_list,
                           "mode": "semantic" if use_semantic else "keyword",
                           "full": full}

    if table in ("both", "topics"):
        if use_semantic:
            rows = (await db.execute(text("""
                SELECT id, title, summary, tags, heat_score, status, source_url,
                       created_at, embedding_json
                FROM hot_topics
                WHERE embedding_json IS NOT NULL
                  AND (:status = '' OR status = :status)
                  AND (cardinality(CAST(:tags AS text[])) = 0 OR tags @> CAST(:tags AS text[]))
                LIMIT 500
            """), {"status": status, "tags": tag_list})).fetchall()
            scored = _cosine_top_k(query_vec, rows, limit)
            out["topics"] = [{**{k: v for k, v in dict(r._mapping).items()
                                 if k != "embedding_json"},
                              "similarity": round(sim, 4)} for sim, r in scored]
        else:
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
        if use_semantic:
            rows = (await db.execute(text(f"""
                SELECT s.id, s.title, s.hook, s.status, s.topic_id, s.model, s.created_at,
                       t.title AS topic_title, s.embedding_json{extra_cols}
                FROM scripts s LEFT JOIN hot_topics t ON t.id = s.topic_id
                WHERE s.embedding_json IS NOT NULL
                  AND (:status = '' OR s.status = :status)
                LIMIT 500
            """), {"status": status})).fetchall()
            scored = _cosine_top_k(query_vec, rows, limit)
            out["scripts"] = [{**{k: v for k, v in dict(r._mapping).items()
                                  if k != "embedding_json"},
                               "similarity": round(sim, 4)} for sim, r in scored]
        else:
            rows = (await db.execute(text(f"""
                SELECT s.id, s.title, s.hook, s.status, s.topic_id, s.model, s.created_at,
                       t.title AS topic_title{extra_cols}
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
