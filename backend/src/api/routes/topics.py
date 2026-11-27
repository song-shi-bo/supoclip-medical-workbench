"""选题卡 API（阶段3 M2）。

路由前缀 /api/v1/topics，挂载时带 enforce_studio_access（登录态）。
身份模型：
- 策划/运营：任何登录用户可拆解/保存选题卡、触发生稿、关联上传任务；
- 医生：仅 topic_reviewers 白名单内的用户可执行两级审批（选题卡审批 / 文稿审批）；
- 管理员：users.is_admin 管理审批白名单。

写入通道即身份：本路由生成的文稿 origin='shooting_script'；
豆包 ingest 通道（ingest_kb.py）写入的永远是 transcript_reference。
"""

import hashlib
import json
import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from urllib.parse import quote as _url_quote
from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ... import topic_pipeline as tp
from ...database import get_db
from ...medical_lexicon import lexicon_dir

router = APIRouter(prefix="/api/v1/topics", tags=["topics"])

# 修改沉淀 9 类归因（阶段3 第6条）：类名 → 中文标签 → 路由目标
REASON_CLASSES: dict[str, tuple[str, str]] = {
    "asr_error":         ("ASR错误",       "medical.tsv"),
    "medical_term":      ("医学术语",       "medical.tsv"),
    "premise_missing":   ("前提缺失",       "medical_guidelines"),
    "conclusion_rigor":  ("医学结论不严谨",  "medical_guidelines"),
    "privacy":           ("隐私",          "negative_patterns"),
    "subtitle_style":    ("字幕表达",       "negative_patterns"),
    "slice_boundary":    ("切片边界",       "none"),
    "stitching":         ("拼接问题",       "none"),
    "hook_issue":        ("钩子问题",       "negative_patterns"),
}

# 审批必填最小集（写入 hot_topics 独立列或 brief JSONB）
_REQUIRED_COLUMN_FIELDS = ("core_conclusion", "risk_boundary")
_FORBIDDEN_MIN_ITEMS = 1
_TOPIC_LIST_COLUMNS = (
    "id, title, summary, status, brief, core_conclusion, risk_boundary, "
    "prerequisites, forbidden_expr, approved_by, approved_at, "
    "heat_score, source_platform, source_url, created_at, updated_at"
)


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------


class BriefIn(BaseModel):
    brief: dict[str, Any] = Field(default_factory=dict, description="展示型字段：目标人群/内容类型/钩子/结构/CTA/业务目标等")
    core_conclusion: str = ""
    risk_boundary: str = ""
    prerequisites: list[str] = Field(default_factory=list)
    forbidden_expr: list[str] = Field(default_factory=list)


class ScriptApproveIn(BaseModel):
    note: str = ""
    reason_class: str = ""   # 可选：审批时顺带沉淀修改分类（9类之一）
    feedback: str = ""       # 可选：反馈原文


class TopicApproveIn(BaseModel):
    reason_class: str = ""
    feedback: str = ""


class GenerateScriptIn(BaseModel):
    doctor_notes: str = Field(default="", description="医生口播习惯/称呼/强调点等补充说明")
    duration_seconds: Optional[int] = Field(default=None, description="覆盖建议时长（秒）")


class JobLinkIn(BaseModel):
    job_id: str = Field(description="studio_jobs.id，拍摄素材上传后回填")


class ReviewerIn(BaseModel):
    user_id: str


class ModificationIn(BaseModel):
    topic_id: str = ""
    script_id: str = ""
    reason_class: str = Field(description="9类归因之一")
    note: str = Field(default="", max_length=2000, description="反馈原文/备注")
    payload: dict[str, Any] = Field(default_factory=dict, description="分类专属：asr_error→{correct,wrong}；句式类→{pattern,severity}")


class ModificationResolveIn(BaseModel):
    note: str = ""


# ---------------------------------------------------------------------------
# 鉴权辅助
# ---------------------------------------------------------------------------


def _subject(request: Request) -> str:
    principal = getattr(request.state, "studio_principal", None) or {}
    subject = principal.get("subject") or principal.get("user") or ""
    if not subject:
        raise HTTPException(401, "studio session required")
    return str(subject)


async def _require_admin(request: Request, db: AsyncSession) -> str:
    subject = _subject(request)
    row = (
        await db.execute(text("SELECT is_admin FROM users WHERE id = :uid"), {"uid": subject})
    ).first()
    if row is None or not row[0]:
        raise HTTPException(403, "admin required")
    return subject


async def _require_reviewer(request: Request, db: AsyncSession) -> str:
    subject = _subject(request)
    row = (
        await db.execute(
            text("SELECT 1 FROM topic_reviewers WHERE user_id = :uid"), {"uid": subject}
        )
    ).first()
    if row is None:
        raise HTTPException(403, "doctor reviewer required")
    return subject


# ---------------------------------------------------------------------------
# 数据辅助
# ---------------------------------------------------------------------------


def _brief_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)) and raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _as_str_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    return []


async def _load_topic(db: AsyncSession, topic_id: str):
    row = (
        await db.execute(
            text(f"SELECT {_TOPIC_LIST_COLUMNS} FROM hot_topics WHERE id = :id"), {"id": topic_id}
        )
    ).first()
    if row is None:
        raise HTTPException(404, "topic not found")
    return row


def _missing_required(row) -> list[str]:
    """返回审批前仍缺失的必填字段名（中文标签）。"""
    missing: list[str] = []
    if not (row.core_conclusion or "").strip():
        missing.append("核心结论")
    if not (row.risk_boundary or "").strip():
        missing.append("风险边界")
    if len(_as_str_list(row.forbidden_expr)) < _FORBIDDEN_MIN_ITEMS:
        missing.append("禁止表达")
    brief = _brief_dict(row.brief)
    if not (brief.get("user_question") or "").strip():
        missing.append("用户问题")
    if not brief.get("suggested_duration_seconds"):
        missing.append("建议时长")
    return missing


def _topic_out(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "summary": row.summary,
        "status": row.status,
        "brief": _brief_dict(row.brief),
        "core_conclusion": row.core_conclusion,
        "risk_boundary": row.risk_boundary,
        "prerequisites": _as_str_list(row.prerequisites),
        "forbidden_expr": _as_str_list(row.forbidden_expr),
        "approved_by": row.approved_by,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "heat_score": row.heat_score,
        "source_platform": row.source_platform,
        "source_url": row.source_url,
        "missing_required": _missing_required(row),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _reference_transcript(db: AsyncSession, topic_id: str) -> str:
    """参考稿：优先取该选题下最新的转写参考稿正文，回退热点摘要原文。"""
    row = (
        await db.execute(
            text(
                "SELECT body FROM scripts WHERE topic_id = :tid AND origin = :origin "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"tid": topic_id, "origin": tp.SCRIPT_ORIGIN_TRANSCRIPT},
        )
    ).first()
    if row and (row[0] or "").strip():
        return row[0]
    row2 = (
        await db.execute(
            text("SELECT raw_text FROM hot_topics WHERE id = :tid"), {"tid": topic_id}
        )
    ).first()
    return (row2[0] or "") if row2 else ""


def _append_medical_tsv(correct: str, wrong: str, actor: str) -> str:
    """向热词文件追加一行（标准词｜类型｜别名错写｜权重｜备注）；标准词已存在则跳过。"""
    tsv = lexicon_dir() / "medical.tsv"
    tsv.parent.mkdir(parents=True, exist_ok=True)
    if tsv.exists():
        for line in tsv.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{correct}\t"):
                return "标准词已存在于词典，未重复添加"
    else:
        tsv.write_text(
            "# 医疗词典（修改沉淀自动追加）\n# 标准词｜类型｜别名/错写｜权重｜备注\n",
            encoding="utf-8",
        )
    with tsv.open("a", encoding="utf-8") as f:
        f.write(f"{correct}\tterm\t{wrong}\t7\t医生反馈沉淀（{actor}）\n")
    return "已追加热词行（转写指纹将在下次加载时刷新）"


async def _record_and_route(
    db: AsyncSession, *,
    actor: str,
    reason_class: str,
    note: str,
    payload: dict[str, Any],
    topic_id: str = "",
    script_id: str = "",
) -> dict[str, Any]:
    """记录一条修改归因并按类路由；返回路由摘要。"""
    if reason_class not in REASON_CLASSES:
        raise HTTPException(422, f"reason_class 必须是 9 类之一：{'/'.join(REASON_CLASSES)}")
    label, default_target = REASON_CLASSES[reason_class]
    detail = ""
    target = default_target
    status = "routed"
    try:
        if reason_class in ("asr_error", "medical_term"):
            correct = str(payload.get("correct", "")).strip()
            wrong = str(payload.get("wrong", "")).strip()
            if not correct:
                target, status = "none", "recorded"
                detail = "缺少 payload.correct（标准词），仅记录未路由"
            else:
                detail = _append_medical_tsv(correct, wrong, actor)
        elif reason_class in ("premise_missing", "conclusion_rigor"):
            statement = (note or str(payload.get("statement", "")) or "（无备注）").strip()
            topic_title = ""
            if topic_id:
                trow = (await db.execute(text("SELECT title FROM hot_topics WHERE id = :id"),
                                         {"id": topic_id})).first()
                topic_title = (trow.title or "")[:40] if trow else ""
            statement = f"【审稿反馈·{label}】{statement}" + (f"（选题：{topic_title}）" if topic_title else "")
            hash_src = json.dumps(
                {"statement": statement, "source_url": "", "entry_type": "doctor_feedback",
                 "risk_boundary": "", "prerequisites": "", "applicability": ""},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            ghash = hashlib.sha256(hash_src.encode()).hexdigest()
            await db.execute(text(
                "INSERT INTO medical_guidelines (id, topic_path, entry_type, statement, prerequisites, "
                "applicability, risk_boundary, audience_note, keywords, source_name, source_url, "
                "evidence_level, lang, status, review_status, content_hash) "
                "VALUES (gen_random_uuid()::text, CAST(:tp AS text[]), 'doctor_feedback', :st, '', '', '', '', "
                "CAST(:kw AS text[]), '工作台审稿反馈', '', '医生审稿', 'zh', 'approved', 'pending_review', :ch) "
                "ON CONFLICT (content_hash) DO NOTHING"
            ), {"tp": ["工作台反馈"], "st": statement, "kw": [topic_title] if topic_title else [],
                "ch": ghash})
            detail = "已写入医学指南库（pending_review，待医生复核）"
        elif reason_class in ("privacy", "subtitle_style", "hook_issue"):
            pattern = str(payload.get("pattern", "")).strip()
            if not pattern:
                target, status = "none", "recorded"
                detail = "未提供 payload.pattern 正则，仅记录未路由"
            else:
                severity = str(payload.get("severity", "soft"))
                severity = severity if severity in ("hard", "soft") else "soft"
                await db.execute(text(
                    "INSERT INTO negative_patterns (id, pattern, label, severity, source, created_by) "
                    "VALUES (gen_random_uuid()::text, :p, :l, :sev, 'doctor_feedback', :by)"
                ), {"p": pattern, "l": f"{label}：{note[:80]}" if note else label,
                    "sev": severity, "by": actor})
                detail = f"已加入负例模式（{severity}），下次生稿即生效"
        else:  # slice_boundary / stitching：记录暂不路由
            target, status = "none", "recorded"
            detail = "该分类暂只记录，不路由（剪辑侧治理已有规则快照）"
    except HTTPException:
        raise
    except Exception as exc:  # 路由失败不能挡记录本身
        target, status = target, "failed"
        detail = f"路由失败：{exc}"
    row = (
        await db.execute(
            text(
                "INSERT INTO modifications (id, topic_id, script_id, reason_class, note, payload, "
                "created_by, routing_target, routing_status, routing_detail) "
                "VALUES (gen_random_uuid()::text, :tid, :sid, :rc, :note, CAST(:pl AS jsonb), "
                ":by, :target, :status, :detail) RETURNING id, created_at"
            ),
            {"tid": topic_id or None, "sid": script_id or None, "rc": reason_class,
             "note": note, "pl": json.dumps(payload, ensure_ascii=False), "by": actor,
             "target": target, "status": status, "detail": detail},
        )
    ).first()
    await db.commit()
    return {"id": row.id, "reason_class": reason_class, "label": label,
            "routing_target": target, "routing_status": status, "routing_detail": detail}


# ---------------------------------------------------------------------------
# 选题卡：列表 / 详情 / 保存 brief / 拆解草稿
# ---------------------------------------------------------------------------


@router.get("")
async def list_topics(
    status: str = Query(default="", description="按状态过滤，逗号分隔可多值"),
    q: str = Query(default="", max_length=100, description="标题/摘要关键词"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    statuses = [s.strip() for s in status.split(",") if s.strip()]
    where_parts: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if statuses:
        where_parts.append("status = ANY(:statuses)")
        params["statuses"] = statuses
    if q.strip():
        where_parts.append("(title ILIKE :q OR summary ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    rows = (
        await db.execute(
            text(
                f"SELECT {_TOPIC_LIST_COLUMNS} FROM hot_topics{where_sql} "
                "ORDER BY updated_at DESC LIMIT :limit OFFSET :offset"
            ),
            params,
        )
    ).fetchall()
    total = (
        await db.execute(
            text(f"SELECT count(*) FROM hot_topics{where_sql}"),
            {k: v for k, v in params.items() if k not in ("limit", "offset")},
        )
    ).scalar() or 0
    return {"total": total, "items": [_topic_out(r) for r in rows]}


@router.get("/reviewers")
async def list_reviewers(request: Request, db: AsyncSession = Depends(get_db)):
    """静态路由必须先于 GET /{topic_id} 注册，否则被动态段抢匹配。"""
    await _require_admin(request, db)
    rows = (
        await db.execute(
            text(
                "SELECT r.user_id, u.name, u.email, r.created_at "
                "FROM topic_reviewers r LEFT JOIN users u ON u.id = r.user_id "
                "ORDER BY r.created_at"
            )
        )
    ).fetchall()
    return {
        "items": [
            {
                "user_id": r.user_id,
                "name": r.name,
                "email": r.email,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/scripts")
async def list_scripts(
    status: str = Query(default="approved", description="按状态过滤，逗号分隔可多值"),
    origin: str = Query(default="shooting_script", description="文稿身份过滤"),
    topic_id: str = Query(default="", description="限定选题"),
    limit: int = Query(default=100, le=300),
    db: AsyncSession = Depends(get_db),
):
    """文稿列表（供上传「关联文稿」选择器与一般检索）。静态路由，先于 /{topic_id} 注册。"""
    statuses = [s.strip() for s in status.split(",") if s.strip()]
    # 2026-11-25 修复：JOIN hot_topics 后两表都有 status 列，WHERE 不带前缀会
    # AmbiguousColumnError（500）。此前被前端静默吞成空列表，选择器看似"无稿可挂"。
    # 状态过滤用 expanding IN（SQLAlchemy 标准展开），不用 ANY(:list) 数组绑定——
    # 后者在 asyncpg + NullPool 组合下会把连接留在未完成状态。
    where = ["s.origin = :origin"]
    params: dict[str, Any] = {"origin": origin, "limit": limit}
    if statuses:
        where.append("s.status IN :statuses")
        params["statuses"] = statuses
    if topic_id.strip():
        where.append("s.topic_id = :tid")
        params["tid"] = topic_id.strip()
    rows = (
        await db.execute(
            text(
                "SELECT s.id, s.topic_id, t.title AS topic_title, s.version, s.title, s.status, "
                "s.task_id, s.created_by, s.created_at "
                "FROM scripts s LEFT JOIN hot_topics t ON t.id = s.topic_id "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY s.created_at DESC LIMIT :limit"
            ).bindparams(bindparam("statuses", expanding=True)),
            params,
        )
    ).fetchall()
    return {
        "items": [
            {
                "id": r.id,
                "topic_id": r.topic_id,
                "topic_title": r.topic_title,
                "version": r.version,
                "title": r.title,
                "status": r.status,
                "task_id": r.task_id,
                "created_by": r.created_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.get("/scripts/{script_id}")
async def get_script_detail(script_id: str, db: AsyncSession = Depends(get_db)):
    """文稿全文（正文/依据/拍摄备注），供选题板查看与审批前通读。"""
    row = (
        await db.execute(
            text(
                "SELECT s.id, s.topic_id, s.origin, s.version, s.title, s.hook, s.body, s.cta, "
                "s.shooting_notes, s.refs, s.status, s.task_id, s.created_by, s.created_at, "
                "t.title AS topic_title "
                "FROM scripts s LEFT JOIN hot_topics t ON t.id = s.topic_id WHERE s.id = :id"
            ),
            {"id": script_id},
        )
    ).first()
    if row is None:
        raise HTTPException(404, "script not found")
    return {
        "id": row.id,
        "topic_id": row.topic_id,
        "topic_title": row.topic_title,
        "origin": row.origin,
        "version": row.version,
        "title": row.title,
        "hook": row.hook,
        "body": row.body,
        "cta": row.cta,
        "shooting_notes": row.shooting_notes,
        "refs": _as_str_list(row.refs),
        "status": row.status,
        "task_id": row.task_id,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.post("/modifications")
async def record_modification(payload: ModificationIn, request: Request,
                              db: AsyncSession = Depends(get_db)):
    """记录一条修改归因并按类路由（热词文件 / 指南库待复核 / 负例模式）。"""
    subject = _subject(request)
    result = await _record_and_route(
        db, actor=subject, reason_class=payload.reason_class, note=payload.note,
        payload=payload.payload, topic_id=payload.topic_id, script_id=payload.script_id,
    )
    return {"ok": True, **result}


@router.get("/modifications")
async def list_modifications(
    reason_class: str = Query(default=""),
    limit: int = Query(default=50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """修改沉淀列表（默认倒序）。静态路由，先于 /{topic_id} 注册。"""
    params: dict[str, Any] = {"limit": limit}
    where = ""
    if reason_class.strip():
        where = " WHERE reason_class = :rc"
        params["rc"] = reason_class.strip()
    rows = (
        await db.execute(
            text(
                "SELECT id, topic_id, script_id, reason_class, note, payload, created_by, "
                "routing_target, routing_status, routing_detail, resolved_by, resolved_at, created_at "
                f"FROM modifications{where} ORDER BY created_at DESC LIMIT :limit"
            ),
            params,
        )
    ).fetchall()
    return {
        "items": [
            {
                "id": r.id,
                "topic_id": r.topic_id,
                "script_id": r.script_id,
                "reason_class": r.reason_class,
                "label": REASON_CLASSES.get(r.reason_class, ("未知", ""))[0],
                "note": r.note,
                "payload": r.payload if isinstance(r.payload, dict) else {},
                "created_by": r.created_by,
                "routing_target": r.routing_target,
                "routing_status": r.routing_status,
                "routing_detail": r.routing_detail,
                "resolved": r.resolved_at is not None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.post("/modifications/{mod_id}/resolve")
async def resolve_modification(mod_id: str, payload: ModificationResolveIn,
                               request: Request, db: AsyncSession = Depends(get_db)):
    subject = _subject(request)
    row = (
        await db.execute(
            text(
                "UPDATE modifications SET resolved_by = :uid, resolved_at = now() "
                "WHERE id = :id AND resolved_at IS NULL RETURNING id"
            ),
            {"uid": subject, "id": mod_id},
        )
    ).first()
    if row is None:
        raise HTTPException(404, "modification not found or already resolved")
    await db.commit()
    return {"ok": True, "id": mod_id, "resolved_by": subject}


@router.get("/{topic_id}")
async def get_topic(topic_id: str, db: AsyncSession = Depends(get_db)):
    row = await _load_topic(db, topic_id)
    scripts = (
        await db.execute(
            text(
                "SELECT id, origin, version, title, status, task_id, created_by, created_at "
                "FROM scripts WHERE topic_id = :tid ORDER BY created_at DESC"
            ),
            {"tid": topic_id},
        )
    ).fetchall()
    item = _topic_out(row)
    item["scripts"] = [
        {
            "id": s.id,
            "origin": s.origin,
            "version": s.version,
            "title": s.title,
            "status": s.status,
            "task_id": s.task_id,
            "created_by": s.created_by,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in scripts
    ]
    return item


@router.post("/{topic_id}/brief")
async def save_brief(topic_id: str, payload: BriefIn, request: Request,
                     db: AsyncSession = Depends(get_db)):
    """保存选题卡。审批通过后再次保存会清空审批（内容变更须重新过医学门）。"""
    subject = _subject(request)
    row = await _load_topic(db, topic_id)
    if row.status not in tp.TOPIC_STATUS_ALLOW_BRIEF_SAVE and row.status != tp.TOPIC_STATUS_DOCTOR_APPROVED:
        raise HTTPException(409, f"status {row.status} 不允许编辑选题卡")

    was_approved = row.status == tp.TOPIC_STATUS_DOCTOR_APPROVED
    await db.execute(
        text(
            "UPDATE hot_topics SET brief = CAST(:brief AS jsonb), core_conclusion = :cc, "
            "risk_boundary = :rb, prerequisites = CAST(:prereq AS text[]), "
            "forbidden_expr = CAST(:fexpr AS text[]), "
            "status = CASE WHEN :was_approved THEN :briefed ELSE status END, "
            "approved_by = CASE WHEN :was_approved THEN NULL ELSE approved_by END, "
            "approved_at = CASE WHEN :was_approved THEN NULL ELSE approved_at END, "
            "updated_at = now() WHERE id = :id"
        ),
        {
            "brief": json.dumps(payload.brief, ensure_ascii=False),
            "cc": payload.core_conclusion.strip(),
            "rb": payload.risk_boundary.strip(),
            "prereq": payload.prerequisites,
            "fexpr": [e.strip() for e in payload.forbidden_expr if e.strip()],
            "was_approved": was_approved,
            "briefed": tp.TOPIC_STATUS_BRIEFED,
            "id": topic_id,
        },
    )
    # 若从 new 保存且必填已齐 → briefed；未齐则保持 new
    updated = await _load_topic(db, topic_id)
    missing = _missing_required(updated)
    if updated.status == tp.TOPIC_STATUS_NEW and not missing:
        await db.execute(
            text("UPDATE hot_topics SET status = :st, updated_at = now() WHERE id = :id"),
            {"st": tp.TOPIC_STATUS_BRIEFED, "id": topic_id},
        )
        updated = await _load_topic(db, topic_id)
    await db.commit()
    out = _topic_out(updated)
    out["reapproval_required"] = was_approved
    out["missing_required"] = missing
    return out


@router.post("/{topic_id}/brief/draft")
async def draft_brief(topic_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """拆解：热点转写稿 → 选题卡草稿（只返回草稿，不落库）。"""
    _subject(request)
    await _load_topic(db, topic_id)  # 404 兜底
    source = await _reference_transcript(db, topic_id)
    if not (source or "").strip():
        raise HTTPException(422, "该选题没有可拆解的转写稿（无参考稿正文，raw_text 为空）")
    try:
        draft = await tp.run_brief_draft(source)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {
        "topic_id": topic_id,
        "draft": draft.model_dump(),
        "note": "草稿不落库；core_conclusion 须经医生确认，风险边界请人工填写",
    }


# ---------------------------------------------------------------------------
# 两级审批
# ---------------------------------------------------------------------------


@router.post("/{topic_id}/approve")
async def approve_topic(topic_id: str, request: Request,
                        payload: Optional[TopicApproveIn] = None,
                        db: AsyncSession = Depends(get_db)):
    """选题卡医学审批：briefed/new → doctor_approved。仅审批白名单用户。"""
    subject = await _require_reviewer(request, db)
    row = await _load_topic(db, topic_id)
    if row.status not in tp.TOPIC_STATUS_ALLOW_APPROVE:
        raise HTTPException(409, f"status {row.status} 不允许审批")
    missing = _missing_required(row)
    if missing:
        raise HTTPException(422, f"必填字段缺失：{'、'.join(missing)}")
    await db.execute(
        text(
            "UPDATE hot_topics SET status = :st, approved_by = :uid, approved_at = now(), "
            "updated_at = now() WHERE id = :id"
        ),
        {"st": tp.TOPIC_STATUS_DOCTOR_APPROVED, "uid": subject, "id": topic_id},
    )
    await db.commit()
    routed = None
    if payload and payload.reason_class:
        routed = await _record_and_route(
            db, actor=subject, reason_class=payload.reason_class, note=payload.feedback,
            payload={}, topic_id=topic_id,
        )
    return {"ok": True, "id": topic_id, "status": tp.TOPIC_STATUS_DOCTOR_APPROVED,
            "approved_by": subject, "modification": routed}


@router.get("/scripts/{script_id}/export")
async def export_script(script_id: str, request: Request,
                        format: str = Query(default="txt"),
                        db: AsyncSession = Depends(get_db)):
    """文稿导出到本地（2026-11-25）：txt=提词器友好纯口播稿（hook+body+cta 纯文本）；
    md=结构化全稿（含标题/拍摄提示/依据）。仅原创拍摄稿可导出（参考稿 409）；
    草稿也可导出（供医生通读）。下载即用：手机提词器直接打开 txt 拍摄。"""
    _subject(request)
    fmt = (format or "txt").strip().lower()
    if fmt not in ("txt", "md"):
        raise HTTPException(status_code=422, detail="format 仅支持 txt / md")
    row = (await db.execute(text(
        "SELECT origin, version, title, hook, body, cta, shooting_notes, refs "
        "FROM scripts WHERE id = :id"), {"id": script_id})).first()
    if row is None:
        raise HTTPException(status_code=404, detail="文稿不存在")
    if row.origin != tp.SCRIPT_ORIGIN_SHOOTING:
        raise HTTPException(status_code=409, detail="参考稿不支持导出（仅原创拍摄稿可导出）")
    title = (row.title or f"文稿-{script_id[:8]}").strip()
    safe_title = re.sub(r'[\\/:*?"<>|\s]+', "_", title)[:40] or "script"
    paragraphs = [str(part or "").strip()
                  for part in (row.hook, row.body, row.cta)]
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs:
        raise HTTPException(status_code=409, detail="文稿口播内容为空（hook/body/cta 均空），无可导出内容")
    if fmt == "txt":
        content = "\n\n".join(paragraphs) + "\n"
        filename = f"{safe_title}-口播稿.txt"
        media_type = "text/plain; charset=utf-8"
    else:
        refs_list = [str(r) for r in (row.refs or [])] if isinstance(row.refs, list) else []
        parts = [f"# {title}", ""]
        for heading, body_text in (("开场钩子", row.hook), ("正文", row.body), ("结尾引导", row.cta)):
            body_text = str(body_text or "").strip()
            if body_text:
                parts += [f"## {heading}", body_text, ""]
        notes = str(row.shooting_notes or "").strip()
        if notes:
            parts += ["## 拍摄提示", notes, ""]
        if refs_list:
            parts += ["## 依据（医学指南/文献）"] + [f"- {r}" for r in refs_list] + [""]
        content = "\n".join(parts)
        filename = f"{safe_title}-文稿.md"
        media_type = "text/markdown; charset=utf-8"
    headers = {"Content-Disposition":
               f"attachment; filename=\"export_{script_id[:8]}.{fmt}\"; "
               f"filename*=UTF-8''{_url_quote(filename)}"}
    return Response(content=content, media_type=media_type, headers=headers)


@router.post("/scripts/{script_id}/approve")
async def approve_script(script_id: str, request: Request, payload: ScriptApproveIn,
                         db: AsyncSession = Depends(get_db)):
    """文稿审批：draft → approved（仅 shooting_script，参考稿无审批语义）。"""
    subject = await _require_reviewer(request, db)
    row = (
        await db.execute(
            text("SELECT origin, status, topic_id FROM scripts WHERE id = :id"), {"id": script_id}
        )
    ).first()
    if row is None:
        raise HTTPException(404, "script not found")
    if row.origin != tp.SCRIPT_ORIGIN_SHOOTING:
        raise HTTPException(409, "转写参考稿无审批语义（origin=transcript_reference）")
    if row.status != "draft":
        raise HTTPException(409, f"status {row.status} 不允许审批")
    await db.execute(
        text(
            "UPDATE scripts SET status = 'approved', created_by = :uid, updated_at = now() "
            "WHERE id = :id"
        ),
        {"uid": subject, "id": script_id},
    )
    await db.commit()
    routed = None
    if payload.reason_class:
        routed = await _record_and_route(
            db, actor=subject, reason_class=payload.reason_class, note=payload.feedback,
            payload={}, topic_id=row.topic_id or "", script_id=script_id,
        )
    return {"ok": True, "id": script_id, "status": "approved", "approved_by": subject,
            "modification": routed}


# ---------------------------------------------------------------------------
# 生稿 / 挂链
# ---------------------------------------------------------------------------


@router.post("/{topic_id}/generate-script")
async def generate_script(topic_id: str, payload: GenerateScriptIn, request: Request,
                          db: AsyncSession = Depends(get_db)):
    """选题卡 → 医生原创拍摄稿。须 doctor_approved；硬校验 hard 不过不落库。"""
    _subject(request)
    row = await _load_topic(db, topic_id)
    if row.status not in tp.TOPIC_STATUS_ALLOW_GENERATE:
        raise HTTPException(409, f"status {row.status} 不允许生稿（须先过医生审批）")
    missing = _missing_required(row)
    if missing:
        raise HTTPException(422, f"必填字段缺失：{'、'.join(missing)}")

    brief = _brief_dict(row.brief)
    duration = payload.duration_seconds or int(brief.get("suggested_duration_seconds")
                                               or tp.DEFAULT_DURATION_SECONDS)
    forbidden = _as_str_list(row.forbidden_expr)
    prerequisites = _as_str_list(row.prerequisites)
    reference = await _reference_transcript(db, topic_id)
    # 引用核准白名单：核准库（medical_guidelines.source_name，approved）进提示词 + 事后核对
    registry_rows = (
        await db.execute(text(
            "SELECT DISTINCT source_name FROM medical_guidelines "
            "WHERE status = 'approved' AND source_name <> '' ORDER BY source_name"
        ))
    ).fetchall()
    approved_refs = [r.source_name.strip() for r in registry_rows if r.source_name and r.source_name.strip()]
    # 负例库（DB 化的医生反馈沉淀模式）：加载启用行，随硬校验一起执行
    pattern_rows = (
        await db.execute(text(
            "SELECT pattern, label, severity FROM negative_patterns WHERE enabled = TRUE"
        ))
    ).fetchall()
    negative_patterns = [(r.pattern, r.label, r.severity) for r in pattern_rows]

    try:
        draft = await tp.run_script_generation(
            brief=brief,
            core_conclusion=row.core_conclusion,
            risk_boundary=row.risk_boundary,
            forbidden_expr=forbidden,
            prerequisites=prerequisites,
            duration_seconds=duration,
            reference_transcript=reference,
            doctor_notes=payload.doctor_notes,
            approved_refs=approved_refs,
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

    # 硬校验：hard 不过可自动重试一次（把违规反馈回提示词），再不过则拒绝落库
    hard, soft = tp.validate_script_draft(
        draft, forbidden_expr=forbidden, prerequisites=prerequisites, duration_seconds=duration,
        extra_patterns=negative_patterns,
    )
    if hard:
        repair_note = "上一稿违反以下约束，必须全部修正后重新输出完整文稿：\n- " + "\n- ".join(hard)
        retry_prompt = tp.build_script_prompt(
            brief=brief,
            core_conclusion=row.core_conclusion,
            risk_boundary=row.risk_boundary,
            forbidden_expr=forbidden,
            prerequisites=prerequisites,
            duration_seconds=duration,
            reference_transcript=reference,
            doctor_notes=(payload.doctor_notes + "\n\n" + repair_note).strip(),
            approved_refs=approved_refs,
        )
        _, script_agent = tp._get_agents()
        result = await script_agent.run(retry_prompt)
        draft = result.output
        hard, soft = tp.validate_script_draft(
            draft, forbidden_expr=forbidden, prerequisites=prerequisites, duration_seconds=duration,
            extra_patterns=negative_patterns,
        )
        if hard:
            raise HTTPException(422, {"message": "生稿未通过硬校验（已重试一次）",
                                      "violations": hard})

    # 引用核准（2026-11-20 医生反馈沉淀）：引用名须能对上核准库，对不上只警告不拦截
    if approved_refs:
        registry_lower = [n.lower() for n in approved_refs]
        for ref in (draft.refs or []):
            rl = ref.strip().lower()
            if rl and not any(name and (name in rl or rl in name) for name in registry_lower):
                soft.append(f"引用未在核准指南库中找到，发布前请人工核准名称与出处：《{ref.strip()}》")

    version_row = (
        await db.execute(
            text("SELECT COALESCE(max(version), 0) FROM scripts WHERE topic_id = :tid"),
            {"tid": topic_id},
        )
    ).scalar() or 0
    runtime_config = tp.get_config()
    title = (draft.title or row.title or "").strip()[:300]
    # content_hash：按标题+正文归一化计算（双库去重约定；正文入哈希使不同版本可共存）
    _norm = "".join(ch for ch in f"{title}{draft.body}".lower() if ch.isalnum())
    content_hash = hashlib.sha256(_norm.encode("utf-8")).hexdigest()
    try:
        inserted = (
            await db.execute(
                text(
                    "INSERT INTO scripts (id, topic_id, version, title, hook, body, cta, "
                    "shooting_notes, refs, model, generated_by, origin, status, content_hash) "
                    "VALUES (gen_random_uuid()::text, :tid, :ver, :title, :hook, :body, :cta, "
                    ":notes, CAST(:refs AS text[]), :model, 'workbench', :origin, 'draft', :content_hash) "
                    "RETURNING id, version"
                ),
                {
                    "tid": topic_id,
                    "ver": int(version_row) + 1,
                    "title": title,
                    "hook": draft.hook,
                    "body": draft.body,
                    "cta": draft.cta,
                    "notes": draft.shooting_notes,
                    "refs": [r.strip() for r in (draft.refs or []) if r.strip()],
                    "model": runtime_config.llm,
                    "origin": tp.SCRIPT_ORIGIN_SHOOTING,
                    "content_hash": content_hash,
                },
            )
        ).first()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "生稿内容与已有文稿完全重复（content_hash 冲突）。"
                                 "如需重新生成，请微调选题卡的核心结论或标题后再试。")
    # 首次生稿后进入 scheduled
    if row.status == tp.TOPIC_STATUS_DOCTOR_APPROVED:
        await db.execute(
            text("UPDATE hot_topics SET status = :st, updated_at = now() WHERE id = :id"),
            {"st": tp.TOPIC_STATUS_SCHEDULED, "id": topic_id},
        )
    await db.commit()
    return {
        "ok": True,
        "script_id": inserted.id,
        "version": inserted.version,
        "topic_status": tp.TOPIC_STATUS_SCHEDULED if row.status == tp.TOPIC_STATUS_DOCTOR_APPROVED else row.status,
        "draft": draft.model_dump(),
        "warnings": soft,
    }


@router.post("/scripts/{script_id}/link-job")
async def link_script_job(script_id: str, payload: JobLinkIn, request: Request,
                          db: AsyncSession = Depends(get_db)):
    """拍摄上传后挂链：scripts.task_id 回填 + studio_jobs.config 挂 script_id/topic_id。"""
    _subject(request)
    row = (
        await db.execute(
            text("SELECT origin, status, topic_id FROM scripts WHERE id = :id"), {"id": script_id}
        )
    ).first()
    if row is None:
        raise HTTPException(404, "script not found")
    if row.origin != tp.SCRIPT_ORIGIN_SHOOTING:
        raise HTTPException(409, "仅原创拍摄稿可关联拍摄任务")
    if row.status not in ("approved", "shooting"):
        raise HTTPException(409, f"status {row.status} 不允许挂链（须先过文稿审批）")
    job = (
        await db.execute(
            text("SELECT id FROM studio_jobs WHERE id = :jid"), {"jid": payload.job_id}
        )
    ).first()
    if job is None:
        raise HTTPException(404, "studio job not found")

    await db.execute(
        text(
            "UPDATE scripts SET task_id = :jid, status = 'uploaded', updated_at = now() "
            "WHERE id = :id"
        ),
        {"jid": payload.job_id, "id": script_id},
    )
    patch = json.dumps({"script_id": script_id, "topic_id": row.topic_id}, ensure_ascii=False)
    await db.execute(
        text(
            "UPDATE studio_jobs SET config = COALESCE(config, '{}'::jsonb) || CAST(:patch AS jsonb) "
            "WHERE id = :jid"
        ),
        {"patch": patch, "jid": payload.job_id},
    )
    if row.topic_id:
        await db.execute(
            text("UPDATE hot_topics SET status = :st, updated_at = now() WHERE id = :id"),
            {"st": tp.TOPIC_STATUS_USED, "id": row.topic_id},
        )
    await db.commit()
    return {"ok": True, "script_id": script_id, "job_id": payload.job_id,
            "script_status": "uploaded"}


# ---------------------------------------------------------------------------
# 审批白名单管理（管理员）——增删接口
# ---------------------------------------------------------------------------


@router.post("/reviewers")
async def add_reviewer(payload: ReviewerIn, request: Request, db: AsyncSession = Depends(get_db)):
    admin = await _require_admin(request, db)
    user = (
        await db.execute(text("SELECT id FROM users WHERE id = :uid"), {"uid": payload.user_id})
    ).first()
    if user is None:
        raise HTTPException(404, "user not found")
    await db.execute(
        text(
            "INSERT INTO topic_reviewers (user_id, created_by) VALUES (:uid, :by) "
            "ON CONFLICT (user_id) DO NOTHING"
        ),
        {"uid": payload.user_id, "by": admin},
    )
    await db.commit()
    return {"ok": True, "user_id": payload.user_id}


@router.delete("/reviewers/{user_id}")
async def remove_reviewer(user_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    await _require_admin(request, db)
    await db.execute(text("DELETE FROM topic_reviewers WHERE user_id = :uid"), {"uid": user_id})
    await db.commit()
    return {"ok": True, "user_id": user_id}
