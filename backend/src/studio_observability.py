"""Studio 可观测性（阶段5·点4，2026-11-23）：事件流水 / 分钟聚合 / 心跳注册 / 哈希巡检.

设计约束（与阶段5·点1 同款「可降级」契约）：
- 本模块的一切写入都是 best-effort——PG 不可用只记 debug/warning，绝不抛给调用方；
- 埋点在 studio_job_store 的 update_progress / claim_next_job 成功路径上触发，
  只记录 **状态或阶段变化** 的事件，进度百分比刷新不落表（防流水膨胀）；
- 聚合与巡检由 main_refactored 的 lifespan 启动后台任务驱动（纯 PG + 定时循环，
  不引入 Prometheus/Grafana——单机规模下 PG 聚合表 + 工作台面板够用）。

指标口径（对齐阶段5规划监控清单）：
- 队列等待时长：claimed 事件时间 − 任务 created_at（按 queue 分位）；
- 每阶段耗时：同任务相邻事件的时间差，按 to_status（阶段）分 p50/p95；
- 失败率：窗口内 failed/technical_failed 终态事件 ÷ 全部终态事件；
- LLM fallback 率、ASR/拼接失败率：由 update_progress 的 stage 计数自然覆盖，
  前端/查询侧按 stage 维度过滤即可（不另建指标）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from typing import Any

from sqlalchemy import text

from .database import get_session_maker
from .r2 import local_path_for, storage_mode

logger = logging.getLogger("studio_observability")

#: 聚合循环间隔（秒）；哈希巡检间隔（秒）。
METRICS_INTERVAL_SECONDS = 60
HASH_AUDIT_INTERVAL_SECONDS = 30 * 60
HASH_AUDIT_SAMPLE = 50

#: 失败率分母口径的终态集合（completed_with_warnings 亦视为成功产出）。
TERMINAL_COUNTED_STATUSES = frozenset({
    "completed", "completed_with_warnings", "failed", "technical_failed",
    "partial_succeeded", "transcript_ready"})


def _degradable(coro):
    """fire-and-forget 执行观测写入；任何异常只记 debug（绝不影响主链路）。"""
    async def _runner():
        try:
            await coro
        except Exception as exc:  # noqa: BLE001
            logger.debug("observability write skipped: %s", exc)

    try:
        task = asyncio.get_running_loop().create_task(_runner())
        return task
    except RuntimeError:
        # 无运行中事件循环（同步线程上下文）：直接同步执行一次，异常吞掉
        try:
            asyncio.run(_runner())
        except Exception as exc:  # noqa: BLE001
            logger.debug("observability sync write skipped: %s", exc)
        return None


async def record_job_event(job_id: str, queue: str, from_status: str | None,
                           to_status: str, stage: str = "", progress: float | None = None,
                           worker_id: str = "") -> None:
    if not job_id:
        return
    maker = get_session_maker()
    async with maker() as session:
        await session.execute(text(
            "INSERT INTO studio_job_events(id, job_id, queue, event, from_status, to_status,"
            " stage, progress, worker_id)"
            " VALUES(:id, :job_id, :queue, 'status_change', :from_status, :to_status,"
            " :stage, :progress, :worker_id)"),
            {"id": uuid.uuid4().hex, "job_id": job_id, "queue": queue or "render",
             "from_status": from_status, "to_status": to_status,
             "stage": (stage or "")[:120], "progress": progress, "worker_id": worker_id or ""})
        await session.commit()


def emit_job_event(job_id: str, queue: str, from_status: str | None,
                   to_status: str, stage: str = "", progress: float | None = None,
                   worker_id: str = "") -> None:
    """埋点入口（fire-and-forget，可降级）。"""
    _degradable(record_job_event(job_id, queue or "render", from_status,
                                 to_status, stage, progress, worker_id))


async def mark_worker_seen(worker_id: str, queues: list[str] | None = None,
                           job_id: str | None = None) -> None:
    """worker 心跳落 PG 注册表（Redis 心跳键保留不变，PG 版本可查询、可告警）。"""
    if not worker_id:
        return
    maker = get_session_maker()
    async with maker() as session:
        await session.execute(text(
            "INSERT INTO studio_worker_registry(worker_id, queues, last_job_id, last_heartbeat)"
            " VALUES(:worker_id, :queues, :job_id, CURRENT_TIMESTAMP)"
            " ON CONFLICT (worker_id) DO UPDATE SET"
            " last_heartbeat = CURRENT_TIMESTAMP,"
            " queues = CASE WHEN :queues <> '' THEN :queues ELSE studio_worker_registry.queues END,"
            " last_job_id = COALESCE(:job_id, studio_worker_registry.last_job_id)"),
            {"worker_id": worker_id, "queues": ",".join(queues or []), "job_id": job_id})
        await session.commit()


async def aggregate_job_metrics(window_seconds: int = METRICS_INTERVAL_SECONDS) -> dict[str, Any]:
    """对上一个完整窗口的事件做分钟级聚合（队列等待 / 阶段 p50·p95 / 失败率）。

    Python 内计算：单 worker 场景每分钟事件量很小，取 [window_start, window_end)
    的事件 + 任务 created_at，逐队列算分位后 upsert 进 job_metrics_minute。
    """
    import datetime as _dt

    now = time.time()
    win_start = _dt.datetime.fromtimestamp((int(now) // window_seconds - 1) * window_seconds,
                                           tz=_dt.timezone.utc)
    win_end = win_start + _dt.timedelta(seconds=window_seconds)
    maker = get_session_maker()
    async with maker() as session:
        rows = (await session.execute(text("""
            SELECT e.job_id, e.queue, e.from_status, e.to_status, e.created_at, j.created_at AS job_created_at
            FROM studio_job_events e JOIN studio_jobs j ON j.id = e.job_id
            WHERE e.created_at >= :win_start AND e.created_at < :win_end
            ORDER BY e.created_at ASC
        """), {"win_start": win_start, "win_end": win_end})).mappings().all()
        created = {str(r["job_id"]): r["job_created_at"] for r in rows}

        per_queue: dict[str, dict[str, Any]] = {}
        prev_event: dict[str, dict[str, Any]] = {}
        for r in rows:
            queue = str(r["queue"] or "render")
            bucket = per_queue.setdefault(queue, {"claimed": 0, "queue_waits": [],
                                                  "stage_durs": {}, "failed": 0, "terminal_total": 0})
            job_key = str(r["job_id"])
            if r["to_status"] == "downloading" and (r["from_status"] or "") in ("queued", "reedit_requested"):
                bucket["claimed"] += 1
                created_at = created.get(job_key)
                if created_at is not None:
                    bucket["queue_waits"].append((r["created_at"] - created_at).total_seconds())
            prev = prev_event.get(job_key)
            if prev is not None:
                dur = (r["created_at"] - prev["created_at"]).total_seconds()
                if dur >= 0 and r["to_status"]:
                    bucket["stage_durs"].setdefault(str(r["to_status"]), []).append(dur)
            prev_event[job_key] = {"created_at": r["created_at"]}
            if r["to_status"] in ("failed", "technical_failed"):
                bucket["failed"] += 1
            if r["to_status"] in TERMINAL_COUNTED_STATUSES:
                bucket["terminal_total"] += 1

        inserted = 0
        for queue, bucket in per_queue.items():
            def _pct(values: list[float], q: float) -> float | None:
                if not values:
                    return None
                ordered = sorted(values)
                idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
                return round(ordered[idx], 3)
            waits = bucket["queue_waits"]
            stages = {stage: {"p50_s": _pct(durs, 0.5), "p95_s": _pct(durs, 0.95), "n": len(durs)}
                      for stage, durs in bucket["stage_durs"].items()}
            terminal = bucket["terminal_total"]
            metrics = {
                "queue_wait_p50_s": _pct(waits, 0.5),
                "queue_wait_p95_s": _pct(waits, 0.95),
                "stage_durations": stages,
                "claimed": bucket["claimed"],
                "failed": bucket["failed"],
                "terminal_total": terminal,
                "failure_rate": round(bucket["failed"] / terminal, 4) if terminal else 0.0,
            }
            await session.execute(text(
                "INSERT INTO job_metrics_minute(window_start, queue, metrics, computed_at)"
                " VALUES(:ws, :queue, CAST(:metrics AS JSONB), CURRENT_TIMESTAMP)"
                " ON CONFLICT (window_start, queue) DO UPDATE SET"
                " metrics = EXCLUDED.metrics, computed_at = CURRENT_TIMESTAMP"),
                {"ws": win_start, "queue": queue,
                 "metrics": __import__("json").dumps(metrics, ensure_ascii=False)})
            inserted += 1
        await session.commit()
    return {"window_start": win_start.isoformat(), "queues_upserted": inserted,
            "events_seen": len(rows)}


async def audit_artifact_hashes(sample: int = HASH_AUDIT_SAMPLE) -> dict[str, Any]:
    """抽样重算本地工件的 sha256 并与台账比对（不可变存储的验证手段）。

    只审计 `local:` 键（R2 模式的远端校验走存储侧生命周期，不在本函数范围）；
    不一致即 warning 日志 + 落 studio_artifact_hash_audits 台账（阶段5·点1 的
    「禁止静默覆盖」契约由此闭环验证）。
    """
    maker = get_session_maker()
    details: list[dict[str, Any]] = []
    mismatch = missing = 0
    async with maker() as session:
        rows = (await session.execute(text(
            "SELECT id, object_key, sha256 FROM studio_artifacts"
            " WHERE object_key LIKE 'local:%' AND sha256 IS NOT NULL"
            " ORDER BY random() LIMIT :limit"), {"limit": int(sample)})).mappings().all()
        for row in rows:
            artifact_id = str(row["id"])
            expected = str(row["sha256"] or "")
            try:
                path = local_path_for(str(row["object_key"]))
            except Exception:  # noqa: BLE001
                continue
            if not path.is_file():
                missing += 1
                details.append({"artifact_id": artifact_id, "issue": "missing",
                                "object_key": str(row["object_key"])[:200]})
                continue
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                mismatch += 1
                details.append({"artifact_id": artifact_id, "issue": "mismatch",
                                "object_key": str(row["object_key"])[:200]})
        if mismatch or missing:
            logger.warning("artifact hash audit: %d mismatch / %d missing of %d sampled",
                           mismatch, missing, len(rows))
        else:
            logger.debug("artifact hash audit clean (%d sampled)", len(rows))
        await session.execute(text(
            "INSERT INTO studio_artifact_hash_audits(id, sample_size, mismatch_count,"
            " missing_count, details) VALUES(:id, :sample, :mismatch, :missing, CAST(:details AS JSONB))"),
            {"id": uuid.uuid4().hex, "sample": len(rows), "mismatch": mismatch,
             "missing": missing, "details": __import__("json").dumps(details[:20], ensure_ascii=False)})
        await session.commit()
    return {"sampled": len(rows), "mismatch": mismatch, "missing": missing, "details": details[:20]}


async def observability_summary() -> dict[str, Any]:
    """读侧汇总：最近指标窗口 / worker 注册表 / 最近哈希巡检。"""
    maker = get_session_maker()
    async with maker() as session:
        metrics = [dict(r) for r in (await session.execute(text(
            "SELECT window_start, queue, metrics FROM job_metrics_minute"
            " ORDER BY window_start DESC LIMIT 20"))).mappings().all()]
        workers = [dict(r) for r in (await session.execute(text(
            "SELECT worker_id, queues, first_seen, last_heartbeat, last_job_id"
            " FROM studio_worker_registry ORDER BY last_heartbeat DESC"))).mappings().all()]
        audits = [dict(r) for r in (await session.execute(text(
            "SELECT checked_at, sample_size, mismatch_count, missing_count"
            " FROM studio_artifact_hash_audits ORDER BY checked_at DESC LIMIT 5"))).mappings().all()]
    return {"metrics": metrics, "workers": workers, "hash_audits": audits,
            "worker_online_threshold_s": 120}


async def _metrics_loop() -> None:
    while True:
        await asyncio.sleep(METRICS_INTERVAL_SECONDS)
        try:
            await aggregate_job_metrics()
        except Exception as exc:  # noqa: BLE001
            logger.debug("metrics aggregation skipped: %s", exc)


async def _hash_audit_loop() -> None:
    await asyncio.sleep(30)  # 启动后先让主链路就绪
    while True:
        try:
            await audit_artifact_hashes()
        except Exception as exc:  # noqa: BLE001
            logger.debug("hash audit skipped: %s", exc)
        await asyncio.sleep(HASH_AUDIT_INTERVAL_SECONDS)


def start_background_tasks() -> list[asyncio.Task]:
    """lifespan 启动时调用：返回任务列表（shutdown 时 cancel）。"""
    if storage_mode() != "local":
        logger.debug("hash audit disabled under %s storage mode", storage_mode())
    return [asyncio.create_task(_metrics_loop()), asyncio.create_task(_hash_audit_loop())]
