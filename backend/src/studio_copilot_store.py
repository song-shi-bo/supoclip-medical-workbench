"""Studio Copilot 持久化：PostgreSQL 存对话与操作日志，Redis 做最近会话缓存.

约定：
- 只存元数据与文本，绝不把视频二进制放进数据库（成片仍走文件系统 + artifact 端点）。
- 按 project_id（timeline_<hex>）隔离，一个时间线项目一份助手历史。
- 任何持久化故障都不能影响剪辑主流程：全部 best-effort，失败仅记录。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import text

from .database import get_session_maker

logger = logging.getLogger("studio_copilot_store")

_RECENT_KEY = "studio:copilot:recent:{project_id}"
_RECENT_LIMIT = 40
_RECENT_TTL_SECONDS = 7 * 24 * 3600


async def save_message(
    project_id: str,
    role: str,
    content: str,
    engine: str | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> str | None:
    """写一条对话消息，返回 message_id；失败返回 None（不影响主流程）。"""
    message_id = uuid.uuid4().hex
    try:
        maker = get_session_maker()
        async with maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO studio_copilot_messages (id, project_id, role, content, engine, actions)
                    VALUES (:id, :project_id, :role, :content, :engine, CAST(:actions AS JSONB))
                    """
                ),
                {
                    "id": message_id,
                    "project_id": project_id,
                    "role": role,
                    "content": content,
                    "engine": engine,
                    "actions": json.dumps(actions or [], ensure_ascii=False),
                },
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("copilot message persist failed: %s", exc)
        return None
    await _cache_recent_turn(project_id, {"id": message_id, "role": role, "content": content, "engine": engine, "actions": actions or []})
    return message_id


async def save_action_log(
    project_id: str,
    action: dict[str, Any],
    status: str,
    detail: str | None = None,
    message_id: str | None = None,
) -> None:
    """记录一次动作的产生/执行结果（run|confirm|executed|failed|dismissed）。"""
    try:
        maker = get_session_maker()
        async with maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO studio_copilot_actions (id, project_id, message_id, action_type, payload, status, detail)
                    VALUES (:id, :project_id, :message_id, :action_type, CAST(:payload AS JSONB), :status, :detail)
                    """
                ),
                {
                    "id": uuid.uuid4().hex,
                    "project_id": project_id,
                    "message_id": message_id,
                    "action_type": str(action.get("type", "unknown")),
                    "payload": json.dumps(action, ensure_ascii=False),
                    "status": status,
                    "detail": detail,
                },
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("copilot action log failed: %s", exc)


async def load_history(project_id: str, limit: int = 40) -> list[dict[str, Any]]:
    """优先 Redis 最近缓存，回退 PostgreSQL 全量历史（按时间正序）。"""
    recent = await _read_recent(project_id)
    if recent:
        return recent[-limit:]
    try:
        maker = get_session_maker()
        async with maker() as session:
            result = await session.execute(
                text(
                    """
                    SELECT id, role, content, engine, actions
                    FROM studio_copilot_messages
                    WHERE project_id = :project_id
                    ORDER BY created_at ASC
                    LIMIT :limit
                    """
                ),
                {"project_id": project_id, "limit": limit},
            )
            rows = result.mappings().all()
        return [
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "engine": row["engine"],
                "actions": row["actions"] if isinstance(row["actions"], list) else [],
            }
            for row in rows
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("copilot history read failed: %s", exc)
        return []


async def _cache_recent_turn(project_id: str, item: dict[str, Any]) -> None:
    try:
        from .workers.job_queue import JobQueue

        pool = await JobQueue.get_pool()
        key = _RECENT_KEY.format(project_id=project_id)
        await pool.rpush(key, json.dumps(item, ensure_ascii=False))
        await pool.ltrim(key, -_RECENT_LIMIT, -1)
        await pool.expire(key, _RECENT_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("copilot redis cache skipped: %s", exc)


async def _read_recent(project_id: str) -> list[dict[str, Any]]:
    try:
        from .workers.job_queue import JobQueue

        pool = await JobQueue.get_pool()
        key = _RECENT_KEY.format(project_id=project_id)
        raw_items = await pool.lrange(key, 0, -1)
        return [json.loads(item) for item in raw_items if item]
    except Exception as exc:  # noqa: BLE001
        logger.debug("copilot redis read skipped: %s", exc)
        return []
