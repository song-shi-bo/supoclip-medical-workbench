"""选题卡 API（阶段3 M2）接口级定向测试：直接 await 端点协程 + stub 认证。

风格沿用 test_active_clip_selection：不经过 Studio 鉴权中间件，Request 用
SimpleNamespace 携带 studio_principal；DB 走 conftest 的 db_session fixture。

覆盖一条最小闭环：
* 管理员把医生加入 topic_reviewers 白名单（非管理员 403）；
* 保存 brief（必填齐 → new→briefed）；
* 审批：白名单内通过（briefed→doctor_approved），白名单外 403；
* 生稿在 LLM 未配置时显式 503（不静默降级，不消耗真实额度）；
* 直接落库一条 shooting_script 后：文稿审批（draft→approved；参考稿 409）；
* link-job：task_id 回填 + studio_jobs.config 挂 script_id/topic_id + 选题→used；
* 结束清理全部 topic-test- 前缀数据。
"""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src import studio_access as sa
from src import topic_pipeline as tp
from src.api.routes import topics as T
from src.database import get_engine

PREFIX = "topic-test-"


def _principal(subject: str) -> dict:
    return {"subject": subject, "slug": subject, "domain_id": sa.DOMAIN_ID,
            "expires_at_ms": int(time.time() * 1000) + 60000}


def _req(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(studio_principal=_principal(user_id)))


@pytest.fixture()
async def seeded(db_session):
    admin_id, doctor_id, plain_id = f"{PREFIX}admin", f"{PREFIX}doctor", f"{PREFIX}plain"
    topic_id, job_id, script_id = f"{PREFIX}topic", f"{PREFIX}job", f"{PREFIX}script"
    for uid, admin in ((admin_id, True), (doctor_id, False), (plain_id, False)):
        await db_session.execute(text(
            "INSERT INTO users (id, name, email, is_admin) "
            "VALUES (:id, :name, :email, :admin) ON CONFLICT (id) DO NOTHING"
        ), {"id": uid, "name": uid, "email": f"{uid}@test.local", "admin": admin})
    await db_session.execute(text(
        "INSERT INTO hot_topics (id, title, summary, raw_text, source_platform, status, content_hash) "
        "VALUES (:id, '测试选题：血压正常能停药吗', '摘要', '转写稿正文若干', 'test', 'new', :hash) "
        "ON CONFLICT (id) DO NOTHING"
    ), {"id": topic_id, "hash": uuid.uuid4().hex})
    await db_session.execute(text(
        "INSERT INTO scripts (id, topic_id, title, body, origin, generated_by, content_hash) "
        "VALUES (:id, :tid, '参考稿', '参考稿正文', 'transcript_reference', 'doubao-local', :hash) "
        "ON CONFLICT (id) DO NOTHING"
    ), {"id": f"{PREFIX}ref", "tid": topic_id, "hash": uuid.uuid4().hex})
    await db_session.commit()
    yield {"admin": admin_id, "doctor": doctor_id, "plain": plain_id,
           "topic": topic_id, "job": job_id, "script": script_id}
    await db_session.execute(text("DELETE FROM studio_jobs WHERE id = :j"), {"j": job_id})
    await db_session.execute(text("DELETE FROM scripts WHERE topic_id = :t"), {"t": topic_id})
    await db_session.execute(text("DELETE FROM hot_topics WHERE id = :t"), {"t": topic_id})
    await db_session.execute(text("DELETE FROM topic_reviewers WHERE user_id LIKE :p"), {"p": PREFIX + "%"})
    await db_session.execute(text("DELETE FROM users WHERE id LIKE :p"), {"p": PREFIX + "%"})
    # 本文件测试的沉淀副产物（真实表，但只删测试自己产生的行）
    await db_session.execute(text("DELETE FROM modifications WHERE created_by LIKE :p"), {"p": PREFIX + "%"})
    await db_session.execute(text("DELETE FROM negative_patterns WHERE created_by LIKE :p"), {"p": PREFIX + "%"})
    # 指南库反馈条目按测试备注文本精确清理（不碰生产上的真实反馈）
    await db_session.execute(text(
        "DELETE FROM medical_guidelines WHERE source_name = '工作台审稿反馈' "
        "AND statement LIKE '%明显降低癌变风险%'"))
    await db_session.commit()


async def test_topic_card_flow(db_session: AsyncSession, seeded, monkeypatch):
    # 强制"LLM 未配置"路径：生稿必须显式报错，不静默降级、不消耗真实模型额度
    monkeypatch.setattr(
        tp, "get_config",
        lambda: SimpleNamespace(llm="google-gla:unavailable-for-test", google_api_key=None),
    )
    admin_req, doctor_req, plain_req = _req(seeded["admin"]), _req(seeded["doctor"]), _req(seeded["plain"])
    tid = seeded["topic"]

    # 1) 白名单管理：非管理员 403；管理员添加医生成功并可列出
    with pytest.raises(HTTPException) as exc:
        await T.add_reviewer(T.ReviewerIn(user_id=seeded["doctor"]), request=plain_req, db=db_session)
    assert exc.value.status_code == 403
    add_result = await T.add_reviewer(T.ReviewerIn(user_id=seeded["doctor"]), request=admin_req, db=db_session)
    assert add_result["ok"]
    listed = await T.list_reviewers(request=admin_req, db=db_session)
    assert any(item["user_id"] == seeded["doctor"] for item in listed["items"])

    # 2) 保存 brief：必填齐全 → new → briefed
    saved = await T.save_brief(
        tid,
        T.BriefIn(
            brief={"user_question": "血压正常能停药吗", "suggested_duration_seconds": 60,
                   "target_audience": "新确诊高血压患者"},
            core_conclusion="血压正常是药物控制的结果，擅自停药会反弹",
            risk_boundary="不推荐具体剂量调整方案",
            prerequisites=["患者已确诊并已开始服药"],
            forbidden_expr=["自行停药没问题", "可以自己减半"],
        ),
        request=plain_req, db=db_session,
    )
    assert saved["status"] == "briefed"
    assert saved["missing_required"] == []

    # 3) 审批门：白名单外 403；白名单内通过 → doctor_approved
    with pytest.raises(HTTPException) as exc:
        await T.approve_topic(tid, request=plain_req, db=db_session)
    assert exc.value.status_code == 403
    approved = await T.approve_topic(tid, request=doctor_req, db=db_session)
    assert approved["status"] == "doctor_approved" and approved["approved_by"] == seeded["doctor"]

    # 4) 生稿：LLM 未配置 → 显式 503
    with pytest.raises(HTTPException) as exc:
        await T.generate_script(tid, T.GenerateScriptIn(doctor_notes=""),
                                request=plain_req, db=db_session)
    assert exc.value.status_code == 503

    # 5) 文稿审批：落库一条 shooting_script；白名单外 403、参考稿 409、通过 → approved
    maker = async_sessionmaker(get_engine(), class_=AsyncSession, expire_on_commit=False)
    async with maker() as db:
        await db.execute(text(
            "INSERT INTO scripts (id, topic_id, version, title, hook, body, refs, origin, status, content_hash) "
            "VALUES (:id, :tid, 1, '原创稿v1', '钩子', '正文', "
            "CAST(ARRAY['指南X'] AS text[]), 'shooting_script', 'draft', :hash) "
            "ON CONFLICT (id) DO NOTHING"
        ), {"id": seeded["script"], "tid": tid, "hash": uuid.uuid4().hex})
        await db.commit()
    with pytest.raises(HTTPException) as exc:
        await T.approve_script(seeded["script"], payload=T.ScriptApproveIn(),
                               request=plain_req, db=db_session)
    assert exc.value.status_code == 403
    approved_script = await T.approve_script(
        seeded["script"], payload=T.ScriptApproveIn(), request=doctor_req, db=db_session
    )
    assert approved_script["status"] == "approved"
    with pytest.raises(HTTPException) as exc:
        await T.approve_script(f"{PREFIX}ref", payload=T.ScriptApproveIn(),
                               request=doctor_req, db=db_session)
    assert exc.value.status_code == 409

    # 6) link-job：task_id 回填 + studio_jobs.config 挂 script_id/topic_id + 选题 → used
    async with maker() as db:
        await db.execute(text(
            "INSERT INTO studio_jobs (id, project_id, object_key, kind, status, config) "
            "VALUES (:id, 'proj-test', 'tasks/test.mp4', 'transcribe_autoedit', 'queued', "
            "CAST('{}' AS jsonb)) ON CONFLICT (id) DO NOTHING"
        ), {"id": seeded["job"]})
        await db.commit()
    linked = await T.link_script_job(
        seeded["script"], T.JobLinkIn(job_id=seeded["job"]), request=plain_req, db=db_session
    )
    assert linked["script_status"] == "uploaded"
    async with maker() as db:
        script = (await db.execute(text(
            "SELECT status, task_id FROM scripts WHERE id = :id"), {"id": seeded["script"]})).first()
        cfg = (await db.execute(text(
            "SELECT config FROM studio_jobs WHERE id = :id"), {"id": seeded["job"]})).scalar()
        topic_status = (await db.execute(text(
            "SELECT status FROM hot_topics WHERE id = :id"), {"id": tid})).scalar()
    assert script.status == "uploaded" and script.task_id == seeded["job"]
    assert cfg["script_id"] == seeded["script"] and cfg["topic_id"] == tid
    assert topic_status == "used"

    # 7) M5 追溯查询（与工作台文档中的验收 SQL 逐字一致）：挂链后必须能查到完整链
    async with maker() as db:
        traced = (await db.execute(text(
            "SELECT t.title AS topic_title, s.version, s.status, s.task_id, j.id AS job_id "
            "FROM hot_topics t JOIN scripts s ON s.topic_id = t.id "
            "LEFT JOIN studio_jobs j ON j.config->>'script_id' = s.id "
            "WHERE s.origin = 'shooting_script' AND t.id = :tid"
        ), {"tid": tid})).first()
    assert traced is not None
    assert traced.task_id == seeded["job"] and traced.job_id == seeded["job"]
    assert traced.status == "uploaded"


async def test_brief_edit_after_approval_requires_reapproval(db_session: AsyncSession, seeded):
    """审批后改动选题卡 → 状态回 briefed、清空审批人（医学门必须重新过）。"""
    doctor_req, plain_req = _req(seeded["doctor"]), _req(seeded["plain"])
    tid = seeded["topic"]
    await T.add_reviewer(T.ReviewerIn(user_id=seeded["doctor"]), request=_req(seeded["admin"]), db=db_session)
    await T.save_brief(tid, T.BriefIn(
        brief={"user_question": "血压正常能停药吗", "suggested_duration_seconds": 60},
        core_conclusion="结论A", risk_boundary="边界A", forbidden_expr=["禁止A"],
    ), request=plain_req, db=db_session)
    await T.approve_topic(tid, request=doctor_req, db=db_session)
    saved2 = await T.save_brief(tid, T.BriefIn(
        brief={"user_question": "血压正常能停药吗", "suggested_duration_seconds": 60},
        core_conclusion="结论B（改过了）", risk_boundary="边界A", forbidden_expr=["禁止A"],
    ), request=plain_req, db=db_session)
    assert saved2["status"] == "briefed"
    assert saved2["reapproval_required"] is True
    assert saved2["approved_by"] is None


async def test_modification_routing(db_session: AsyncSession, seeded, monkeypatch, tmp_path):
    """三类路由：ASR错误→热词文件追加；结论不严谨→指南库待复核；钩子句式→负例模式生效。"""
    doctor_req, plain_req = _req(seeded["doctor"]), _req(seeded["plain"])
    tid = seeded["topic"]

    # 热词文件路由：MEDICAL_LEXICON_DIR 指向 tmp，验证追加而不碰真实词典
    monkeypatch.setenv("MEDICAL_LEXICON_DIR", str(tmp_path))
    r1 = await T.record_modification(T.ModificationIn(
        topic_id=tid, reason_class="asr_error", note="转写把前表性胃炎写成浅表型胃炎",
        payload={"correct": "浅表性胃炎", "wrong": "前表型胃炎"},
    ), request=plain_req, db=db_session)
    assert r1["routing_target"] == "medical.tsv" and r1["routing_status"] == "routed"
    tsv = (tmp_path / "medical.tsv").read_text(encoding="utf-8")
    assert "浅表性胃炎\tterm\t前表型胃炎" in tsv

    # 指南库路由：结论不严谨 → 待复核条目
    r2 = await T.record_modification(T.ModificationIn(
        topic_id=tid, reason_class="conclusion_rigor",
        note="原文'三次完全可以阻断癌变'超出指南口径，应改为'明显降低癌变风险'",
    ), request=doctor_req, db=db_session)
    assert r2["routing_target"] == "medical_guidelines" and r2["routing_status"] == "routed"
    from sqlalchemy import text
    g = await db_session.execute(text(
        "SELECT statement, review_status FROM medical_guidelines "
        "WHERE source_name='工作台审稿反馈' AND statement LIKE '%明显降低癌变风险%' ORDER BY created_at DESC LIMIT 1"
    ))
    grow = g.first()
    assert grow is not None and grow.review_status == "pending_review"

    # 负例模式路由：钩子句式带 pattern → 负例表新增且校验器可用
    r3 = await T.record_modification(T.ModificationIn(
        topic_id=tid, reason_class="hook_issue", note="连续反问结尾需人工复核",
        payload={"pattern": r"(了解|考虑|重视)过吗？", "severity": "soft"},
    ), request=plain_req, db=db_session)
    assert r3["routing_target"] == "negative_patterns"
    from src.database import get_engine
    from sqlalchemy.ext.asyncio import async_sessionmaker
    maker = async_sessionmaker(get_engine(), class_=AsyncSession, expire_on_commit=False)
    async with maker() as db:
        np_row = (await db.execute(text(
            "SELECT pattern, label, severity FROM negative_patterns WHERE label LIKE '%连续反问结尾%'"
            "ORDER BY created_at DESC LIMIT 1"
        ))).first()
    assert np_row is not None and np_row.severity == "soft"
    hard, soft = tp.validate_script_draft(
        tp.ShootingScriptDraft(title="t", hook="h", body="你真的了解吗？你考虑过吗？你重视过吗？", cta="c", refs=["依据"]),
        duration_seconds=60, extra_patterns=[(np_row.pattern, np_row.label, np_row.severity)])
    assert any("连续反问结尾" in v for v in soft)

    # 无 pattern 的句式类只记录不路由
    r4 = await T.record_modification(T.ModificationIn(
        topic_id=tid, reason_class="privacy", note="画面里出现了他人病历"),
        request=plain_req, db=db_session)
    assert r4["routing_status"] == "recorded"

    # resolve：标记已处理
    resolved = await T.resolve_modification(r1["id"], T.ModificationResolveIn(note="ok"),
                                            request=doctor_req, db=db_session)
    assert resolved["resolved_by"] == seeded["doctor"]

    # 非法分类 422
    with pytest.raises(HTTPException) as exc:
        await T.record_modification(T.ModificationIn(reason_class="not_a_class"),
                                    request=plain_req, db=db_session)
    assert exc.value.status_code == 422


async def test_list_scripts_returns_approved_shooting_only(db_session: AsyncSession, seeded):
    """回归（2026-11-25）：GET /scripts 的 WHERE 必须带表前缀——JOIN hot_topics 后两表都有
    status 列，不带前缀 = AmbiguousColumnError 500；该错误曾被前端静默吞成空列表，
    上传「关联文稿」选择器看起来像"无稿可挂"。本测试用真实 DB 执行该 SQL。"""
    # 落一条已批准原创稿（挂 seeded 选题，随现有 teardown 清理）
    await db_session.execute(text(
        "INSERT INTO scripts (id, topic_id, title, body, origin, generated_by, content_hash, status) "
        "VALUES (:id, :tid, '已批准原创稿', '正文', 'shooting_script', 'workbench', :hash, 'approved') "
        "ON CONFLICT (id) DO NOTHING"
    ), {"id": f"{PREFIX}shot", "tid": seeded["topic"], "hash": uuid.uuid4().hex})
    await db_session.commit()

    resp = await T.list_scripts(status="approved", origin="shooting_script",
                                topic_id="", limit=100, db=db_session)
    await db_session.commit()  # 纯读事务显式关闭，否则 asyncpg+NullPool 下 teardown 卡死
    ids = {item["id"] for item in resp["items"]}
    assert f"{PREFIX}shot" in ids          # 已批准原创稿出现
    assert f"{PREFIX}ref" not in ids       # 参考稿永不出现（origin=shooting_script 过滤）

    # 多状态过滤走同一条 SQL（ANY 列表参数）
    resp2 = await T.list_scripts(status="approved,shooting", origin="shooting_script",
                                 topic_id="", limit=100, db=db_session)
    await db_session.commit()
    assert any(item["id"] == f"{PREFIX}shot" for item in resp2["items"])
