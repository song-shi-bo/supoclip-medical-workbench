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
        _save_alignment_record(job_id, record)
        return record
    except Exception as exc:  # noqa: BLE001 —— 匹配失败绝不阻断主链
        record["warnings"].append(f"匹配阶段异常：{exc}；按门诊切片流程继续")
        try:
            _save_alignment_record(job_id, record)
        except Exception:  # noqa: BLE001 —— 落盘失败也不阻断
            pass
        return record
