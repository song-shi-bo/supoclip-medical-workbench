"""对稿成片：匹配阶段（P0 接线）。

AUTO_EDIT 开始时若 ``config.script_mode`` 为真：
- 加载文稿库候选（``origin=shooting_script`` 且 ``status=approved``，上限 50 篇）；
- 用 ``script_align.match_script_library`` 与纠错转写做句级对齐；
- 报告原子落盘 ``tasks/alignment/<job_id>.json``（目录约定同 analysis）；
- 覆盖率达标 → 自动挂链：``scripts.task_id`` 回填 + 状态 uploaded + 选题→used
  （替代人工 link-job；config.script_id 由调用方按需合并进内存 job）；
- 任何失败只返回带 warnings 的记录，绝不阻断主链（沿用三库降级原则）。

VERSION: script-conform/v1
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from . import script_align
from .database import get_database_url

SCRIPT_CONFORM_VERSION = "script-conform/v1"

# 匹配候选上限：已批准待拍的原创稿天然只有少量在池，50 为资源保护上限
_CANDIDATE_LIMIT = 50


def _fresh_engine():
    """独立短生命周期引擎（NullPool）。

    不用全局 get_engine()：全局引擎有事件循环亲和性，worker 线程/嵌套异步
    场景下跨循环复用会污染连接池（Future attached to a different loop）。
    匹配阶段每次调用独立建连、用完 dispose，彻底循环隔离。
    """
    return create_async_engine(get_database_url(), poolclass=NullPool)


def alignment_root() -> Path:
    """对齐报告目录：``<tasks 根>/alignment``。

    直接复用 ``content_analysis.analysis_root`` 的父目录（保证与 analysis 落盘
    根完全一致，不各自维护相对层级——否则 backend/src 与 backend/src/domains
    两个深度会算出不同根）。
    """
    from .domains.analysis.content_analysis import analysis_root
    return analysis_root().parent / "alignment"


def conform_root() -> Path:
    """精剪 EDL 目录：``<tasks 根>/conform``（与 alignment/analysis 同根约定）。"""
    return alignment_root().parent / "conform"


def _save_conform_edl(job_id: str, edl: dict[str, Any]) -> str:
    directory = conform_root()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{job_id}.json"
    temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.part")
    try:
        temporary.write_text(json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return str(target)


_BUILTIN_DRUG_NAMES: tuple[str, ...] = (
    "紫杉醇", "顺铂", "卡铂", "多西他赛", "环磷酰胺", "伊马替尼",
    "曲美替尼", "达拉非尼", "维莫非尼", "帕博利珠单抗", "纳武利尤单抗",
)


def _drug_terms() -> tuple[str, ...]:
    """红线药名词表：内置 starter + medical_lexicon type=drug 标准词（缺失仅内置生效）。"""
    names = set(_BUILTIN_DRUG_NAMES)
    try:
        from src.domains.medical_review import medical_lexicon

        for entry in medical_lexicon.load_lexicon().get("entries") or []:
            if isinstance(entry, dict) and str(entry.get("type") or "") == "drug":
                word = str(entry.get("standard") or entry.get("word") or "").strip()
                if len(word) >= 2:
                    names.add(word)
    except Exception:
        pass  # 词典缺失/损坏：仅内置表生效（红线判定绝不因词典阻断）
    return tuple(sorted((w for w in names if len(w) >= 2), key=len, reverse=True))


def _save_alignment_record(job_id: str, record: dict[str, Any]) -> str:
    """对齐报告原子落盘，落盘前把 artifact 路径写进记录本体（同 analysis 约定）。"""
    directory = alignment_root()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{job_id}.json"
    record["alignment_artifact_path"] = str(target)
    temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.part")
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return str(target)


def _run_async(coro):
    """在同步阶段里执行协程。

    无运行中的事件循环 → 直接 asyncio.run（worker 线程的正常路径）；
    已处于某个事件循环内（嵌套调用场景）→ 丢到独立线程执行——
    嵌套 asyncio.run 会抛 RuntimeError，这里绝不让它炸出主链。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _load_candidates() -> list[dict[str, Any]]:
    """文稿库候选：已批准原创稿（参考稿永不入池）。"""

    def _query() -> list[dict[str, Any]]:
        return _run_async(_query_coro())

    async def _query_coro() -> list[dict[str, Any]]:
        engine = _fresh_engine()
        try:
            async with engine.connect() as conn:
                rows = (await conn.execute(text(
                    "SELECT s.id, s.topic_id, s.version, s.title, s.hook, s.body, s.cta "
                    "FROM scripts s "
                    "WHERE s.origin = 'shooting_script' AND s.status = 'approved' "
                    "ORDER BY s.created_at DESC LIMIT :limit"
                ), {"limit": _CANDIDATE_LIMIT})).mappings().all()
            return [dict(r) for r in rows]
        finally:
            await engine.dispose()

    return _query()


def _auto_link(script_id: str, topic_id: str | None, job_id: str) -> bool:
    """匹配达标后自动挂链（等价 link-job 的库侧动作）。script 不在可挂状态时返回 False。"""

    def _write() -> bool:
        return _run_async(_write_coro())

    async def _write_coro() -> bool:
        engine = _fresh_engine()
        try:
            async with engine.begin() as conn:
                row = (await conn.execute(text(
                    "SELECT origin, status, topic_id FROM scripts WHERE id = :id"
                ), {"id": script_id})).first()
                if row is None or row.origin != "shooting_script" or row.status not in ("approved", "shooting"):
                    return False
                await conn.execute(text(
                    "UPDATE scripts SET task_id = :jid, status = 'uploaded', updated_at = now() "
                    "WHERE id = :id"
                ), {"jid": job_id, "id": script_id})
                if topic_id:
                    await conn.execute(text(
                        "UPDATE hot_topics SET status = 'used', updated_at = now() WHERE id = :id"
                    ), {"id": topic_id})
                return True
        finally:
            await engine.dispose()

    return _write()


def run_script_match_stage(job: dict, job_id: str, transcript: dict) -> dict[str, Any] | None:
    """对稿模式匹配阶段。非对稿模式返回 None；任何异常降级为 warnings 记录不阻断。"""
    config = job.get("config") or {}
    if not config.get("script_mode"):
        return None
    # 恢复语义（2026-11-25 P2b）：确认后任务重新排队重跑时，对齐/EDL/分级工件
    # 已存在 → 直接复用（不重新匹配、不重复调 LLM），与 analysis/plan 恢复同约定。
    existing = alignment_root() / f"{job_id}.json"
    if existing.is_file():
        try:
            record = json.loads(existing.read_text(encoding="utf-8"))
            record["_resumed"] = True
            if record.get("conform_edl_path") and Path(record["conform_edl_path"]).is_file():
                return record
            record["warnings"].append("对齐工件存在但 EDL 缺失，重算 EDL")
        except Exception:  # noqa: BLE001 —— 工件损坏按无工件处理
            pass
    segments = list(transcript.get("asr_segments") or [])
    record: dict[str, Any] = {
        "version": SCRIPT_CONFORM_VERSION,
        "align_version": script_align.SCRIPT_ALIGN_VERSION,
        "job_id": job_id,
        "script_mode": True,
        "warnings": [],
    }
    try:
        if not segments:
            record["warnings"].append("转写无分段，无法匹配；按门诊切片流程继续")
            _save_alignment_record(job_id, record)
            return record
        candidates = _load_candidates()
        if not candidates:
            record["warnings"].append("文稿库无已批准原创稿；按门诊切片流程继续")
            _save_alignment_record(job_id, record)
            return record

        result = script_align.match_script_library(segments, candidates)
        best = result["best"]
        chosen = next((c for c in candidates if c.get("id") == best["script"].get("id")), None)
        linked = False
        if best.get("is_script_shot") and chosen is not None:
            linked = _auto_link(str(chosen["id"]), chosen.get("topic_id"), job_id)
            if not linked:
                record["warnings"].append("匹配文稿不在可挂链状态（已批准/拍摄中才可挂）")
        elif not best.get("is_script_shot"):
            record["warnings"].append(
                f"最高覆盖率 {best['coverage_ratio']:.0%} 未达阈值，按门诊切片流程继续")

        record.update({
            "matched_script": best["script"],
            "coverage_ratio": best["coverage_ratio"],
            "is_script_shot": best.get("is_script_shot", False),
            "script_sentences": best["script_sentences"],
            "sentences": best["sentences"],
            "extra_segments": best["extra_segments"],
            "script_linked": linked,
            "candidate_summaries": result["candidates"],
        })
        # P2a：LLM 分级 extra 段（一次调用；失败回退 local-rule，不影响匹配报告）
        try:
            from .conform_review import review_extra_segments

            review = review_extra_segments(
                {"extra_segments": best["extra_segments"],
                 "sentences": best["sentences"]}, job_id=job_id)
            record["review"] = {"engine": review.get("engine"),
                                "verdicts": review.get("verdicts", []),
                                "warnings": list(review.get("warnings") or [])}
        except Exception as exc:  # noqa: BLE001 —— 分级阶段异常不影响匹配报告
            record["warnings"].append(f"分级阶段异常：{exc}")
        # P1a：匹配达标 → 构建精剪 EDL（独立 artifact；失败只警告不影响匹配报告）
        if chosen is not None:
            try:
                from .conform_edl import build_conform_edl

                edl = build_conform_edl(
                    transcript,
                    {"sentences": best["sentences"], "extra_segments": best["extra_segments"]},
                    script=chosen, drug_terms=_drug_terms(), job_id=job_id,
                    review=record.get("review"))
                edl_path = _save_conform_edl(job_id, edl)
                edl["conform_edl_path"] = edl_path
                _save_conform_edl(job_id, edl)
                record["conform_edl_path"] = edl_path
                record["edl_summary"] = {
                    "output_duration_ms": edl["output_duration_ms"],
                    "cut_ratio": edl["cut_ratio"],
                    "stats": edl["stats"],
                }
            except Exception as exc:  # noqa: BLE001 —— EDL 失败不影响匹配报告
                record["warnings"].append(f"EDL 构建失败：{exc}")
        _save_alignment_record(job_id, record)
        return record
    except Exception as exc:  # noqa: BLE001 —— 匹配失败绝不阻断主链
        record["warnings"].append(f"匹配阶段异常：{exc}；按门诊切片流程继续")
        try:
            _save_alignment_record(job_id, record)
        except Exception:  # noqa: BLE001 —— 落盘失败也不阻断
            pass
        return record
