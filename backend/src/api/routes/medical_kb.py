"""医学指南库（第三库）：条目入库 + 检索 + 医生事后复核（2026-11-10 阶段一）。

鉴权模型（与热点双库 ingest_kb.py 同款）：
- ``/api/v1/medical-kb/ingest`` 与 ``/api/v1/medical-kb/search`` 为独立 Bearer token
  （MEDICAL_KB_INGEST_TOKEN / MEDICAL_KB_READ_TOKEN，值放 backend/.env.kb），
  需加入 StudioAuthenticationMiddleware 豁免清单；
- ``/api/v1/medical-kb/entries/{id}/review`` 走 Studio 登录态（中间件正常校验），
  复核人回填 reviewed_by（2026-11-10 用户拍板：直接生效 + 事后复核）。

与闸门的关系：切片医学闸门（clip_medical_gate）阶段一消费**文件快照**
（medical_kb_seed/active_snapshot.json，规划阶段不连库）；本路由维护数据库条目，
供检索 API / 复核流使用；阶段二「库→审核→重新物化快照」闭环后闸门改为消费审核后的快照。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any

import numpy as np

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ... import kb_embedding
from ...database import get_db

load_dotenv(Path(__file__).resolve().parents[3] / ".env.kb")

router = APIRouter(prefix="/api/v1/medical-kb", tags=["medical-kb"])


def _env_token(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise HTTPException(503, f"{name} not configured")
    return value


def _check(request: Request, name: str) -> None:
    got = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    if not got or not secrets.compare_digest(got, _env_token(name)):
        raise HTTPException(401, "bad token")


def _content_hash(entry: dict[str, Any]) -> str:
    norm = json.dumps(
        {k: entry.get(k) for k in ("statement", "source_url", "entry_type", "risk_boundary",
                                   "prerequisites", "applicability")},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(norm.encode()).hexdigest()


def _cosine_top_k(query_vec: list[float], rows: list[Any], limit: int):
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


class GuidelineIn(BaseModel):
    id: str = Field(min_length=3, max_length=64)
    topic_path: list[str] = Field(default_factory=list, max_length=8)
    entry_type: str = Field(min_length=2, max_length=32)
    statement: str = Field(min_length=4)
    prerequisites: str = ""
    applicability: str = ""
    risk_boundary: str = ""
    audience_note: str = ""
    keywords: list[str] = Field(default_factory=list, max_length=40)
    source_name: str = Field(min_length=2, max_length=200)
    source_url: str = ""
    evidence_level: str = Field(default="", max_length=60)
    lang: str = "zh"
    status: str = Field(default="approved", pattern="^(approved|disabled)$")
    review_status: str = Field(default="pending_review",
                               pattern="^(pending_review|reviewed)$")


class GuidelinesIn(BaseModel):
    entries: list[GuidelineIn] = Field(min_length=1, max_length=200)


INSERT_GUIDELINE = text("""
    INSERT INTO medical_guidelines (id, topic_path, entry_type, statement, prerequisites,
                                    applicability, risk_boundary, audience_note, keywords,
                                    source_name, source_url, evidence_level, lang,
                                    status, review_status, content_hash)
    VALUES (:id, CAST(:topic_path AS text[]), :entry_type, :statement, :prerequisites,
            :applicability, :risk_boundary, :audience_note, CAST(:keywords AS text[]),
            :source_name, :source_url, :evidence_level, :lang,
            :status, :review_status, :content_hash)
    ON CONFLICT (content_hash) DO NOTHING
""")


async def _embed_missing(db: AsyncSession, ids: list[str]) -> int:
    if not ids:
        return 0
    try:
        rows = (await db.execute(text(
            "SELECT id, statement, audience_note, keywords FROM medical_guidelines "
            "WHERE embedding_json IS NULL AND id = ANY(:ids)"), {"ids": ids})).fetchall()
        if not rows:
            return 0
        vectors = kb_embedding.embed_texts([
            f"{r.statement}\n{r.audience_note}\n{' '.join(r.keywords or [])}"[:2000]
            for r in rows])
        if not vectors:
            return 0
        for r, vec in zip(rows, vectors):
            await db.execute(text(
                "UPDATE medical_guidelines SET embedding_json = CAST(:vec AS jsonb) "
                "WHERE id = :id"), {"vec": json.dumps(vec), "id": r.id})
        await db.commit()
        return len(rows)
    except Exception:
        await db.rollback()
        return 0


@router.post("/ingest")
async def ingest_guidelines(payload: GuidelinesIn, request: Request,
                            db: AsyncSession = Depends(get_db)):
    _check(request, "MEDICAL_KB_INGEST_TOKEN")
    inserted = deduped = 0
    ids: list[str] = []
    for entry in payload.entries:
        row = await db.execute(INSERT_GUIDELINE, {
            "id": entry.id, "topic_path": entry.topic_path, "entry_type": entry.entry_type,
            "statement": entry.statement, "prerequisites": entry.prerequisites,
            "applicability": entry.applicability, "risk_boundary": entry.risk_boundary,
            "audience_note": entry.audience_note, "keywords": entry.keywords,
            "source_name": entry.source_name, "source_url": entry.source_url,
            "evidence_level": entry.evidence_level, "lang": entry.lang,
            "status": entry.status, "review_status": entry.review_status,
            "content_hash": _content_hash(entry.model_dump()),
        })
        inserted += row.rowcount
        deduped += 1 - row.rowcount
        ids.append(entry.id)
    await db.commit()
    embedded = await _embed_missing(db, ids)
    return {"inserted": inserted, "deduped": deduped, "embedded": embedded}


@router.get("/search")
async def search_guidelines(request: Request, q: str = "",
                            entry_type: str = "", topic: str = "",
                            mode: str = Query("keyword", pattern="^(keyword|semantic)$"),
                            full: bool = Query(False, description="true=返回条目全部结构化字段"),
                            limit: int = Query(10, ge=1, le=50),
                            db: AsyncSession = Depends(get_db)):
    _check(request, "MEDICAL_KB_READ_TOKEN")
    like = f"%{q.strip()}%"
    use_semantic = mode == "semantic" and bool(q.strip())
    query_vec = None
    if use_semantic:
        vectors = kb_embedding.embed_texts([q.strip()])
        query_vec = vectors[0] if vectors and vectors[0] else None
        if query_vec is None:
            use_semantic = False
    extra_cols = (", prerequisites, applicability, risk_boundary, audience_note, keywords,"
                  if full else "")
    out: dict[str, Any] = {"q": q, "entry_type": entry_type, "topic": topic,
                           "mode": "semantic" if use_semantic else "keyword", "full": full}
    if use_semantic:
        rows = (await db.execute(text(f"""
            SELECT id, topic_path, entry_type, statement, source_name, source_url,
                   status, review_status, embedding_json{extra_cols}
            FROM medical_guidelines
            WHERE status = 'approved' AND embedding_json IS NOT NULL
            LIMIT 500
        """))).fetchall()
        scored = _cosine_top_k(query_vec, rows, limit)
        out["entries"] = [{**{k: v for k, v in dict(r._mapping).items()
                              if k != "embedding_json"},
                           "similarity": round(sim, 4)} for sim, r in scored]
    else:
        rows = (await db.execute(text(f"""
            SELECT id, topic_path, entry_type, statement, source_name, source_url,
                   status, review_status{extra_cols}
            FROM medical_guidelines
            WHERE status = 'approved'
              AND (:q = '' OR statement ILIKE :like OR audience_note ILIKE :like
                   OR source_name ILIKE :like OR array_to_string(keywords, ' ') ILIKE :like)
              AND (:entry_type = '' OR entry_type = :entry_type)
              AND (:topic = '' OR :topic = ANY(topic_path))
            ORDER BY entry_type, id LIMIT :limit
        """), {"q": q.strip(), "like": like, "entry_type": entry_type, "topic": topic,
               "limit": limit})).fetchall()
        out["entries"] = [dict(r._mapping) for r in rows]
    return out


@router.post("/entries/{entry_id}/review")
async def review_guideline(entry_id: str, request: Request,
                           db: AsyncSession = Depends(get_db)):
    """医生事后复核（pending_review → reviewed）；走 Studio 登录态，复核人回填。"""
    principal = getattr(request.state, "studio_principal", None) or {}
    subject = principal.get("subject") or principal.get("user") or ""
    if not subject:
        raise HTTPException(401, "studio session required")
    row = await db.execute(text(
        "UPDATE medical_guidelines SET review_status='reviewed', reviewed_by=:uid, "
        "reviewed_at=now(), updated_at=now() "
        "WHERE id=:id AND review_status='pending_review' RETURNING id"),
        {"uid": str(subject), "id": entry_id})
    await db.commit()
    if row.first() is None:
        raise HTTPException(404, "not found or already reviewed")
    return {"ok": True, "id": entry_id, "review_status": "reviewed"}
