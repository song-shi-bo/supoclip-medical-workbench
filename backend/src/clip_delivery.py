"""成片投递：工作台「发送到抖音」→ 95 中转暂存 → 本地拉取 → 抖音私密上传。

链路（2026-10-28 与 95 侧对接）：
  工作台点「发送到抖音」→ POST https://push.zcmed.net/drop/api/inbox（Bearer upload_token）
  → 95 暂存（sha256 幂等去重）→ 本地 pull 脚本拉进 inbox → 预检 → 私密上传 → 手机复核。

配置（backend/.env，不含密钥默认值）：
  DROP_INBOX_URL    接收端地址（https://push.zcmed.net/drop/api/inbox）
  DROP_UPLOAD_TOKEN 95 侧 /opt/dropbox/config.json 签发的 upload_token（缺失 → 503 不投递）

安全边界：路由层先做 ACL 归属校验（require project）；本模块只负责解析路径与发送。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import HTTPException


def drop_config() -> tuple[str, str]:
    """读取投递配置；未配置 → 503（不静默降级）。"""
    url = os.environ.get("DROP_INBOX_URL", "").strip()
    token = os.environ.get("DROP_UPLOAD_TOKEN", "").strip()
    if not url or not token:
        raise HTTPException(503, "drop_inbox_not_configured")
    return url, token


def pg_dsn() -> str:
    return (os.environ.get("DATABASE_URL") or "").replace("+asyncpg", "")


def auth_db_dsn() -> str:
    """Better Auth 用户库（前端库）——投递 meta 的可读身份（email/username）来源。"""
    return (os.environ.get("BETTER_AUTH_DATABASE_URL")
            or os.environ.get("DATABASE_URL") or "").replace("+asyncpg", "")


def fetch_user_identity(subject: str) -> dict[str, str]:
    """subject → {email, username}（豆包侧路由绑定的可读标识）。查询失败返回空 dict，不阻塞投递。"""
    import asyncio

    import asyncpg

    dsn = auth_db_dsn()
    if not dsn:
        return {}

    async def _run() -> Optional[asyncpg.Record]:
        conn = await asyncpg.connect(dsn, timeout=5)
        try:
            return await conn.fetchrow(
                'SELECT email, username FROM users WHERE id=$1 LIMIT 1', subject)
        finally:
            await conn.close()

    try:
        row = asyncio.run(_run())
    except Exception:  # noqa: BLE001 — 身份增强字段失败不影响投递主链路
        return {}
    if row is None:
        return {}
    return {"email": str(row["email"] or ""), "username": str(row["username"] or "")}


def _temp_dir() -> Path:
    return Path(os.environ.get("TEMP_DIR") or "/tmp").resolve()


def resolve_clip_final_path(clip_id: str) -> dict[str, Any]:
    """clip_id → 最新 clip_final 产物（object_key → 物理路径）。

    短连接 asyncpg（与 project_lifecycle 同款：线程池内 asyncio.run，不复用池）。
    """
    import asyncio

    import asyncpg

    dsn = pg_dsn()
    if not dsn:
        raise HTTPException(503, "database_unavailable")

    async def _run() -> Optional[asyncpg.Record]:
        conn = await asyncpg.connect(dsn, timeout=10)
        try:
            return await conn.fetchrow(
                "SELECT job_id, object_key, sha256, size_bytes FROM studio_artifacts "
                "WHERE clip_id=$1 AND kind='clip_final' ORDER BY created_at DESC LIMIT 1",
                clip_id,
            )
        finally:
            await conn.close()

    row = asyncio.run(_run())
    if row is None:
        raise HTTPException(404, "clip_final_not_found")
    key = str(row["object_key"] or "")
    if not key.startswith("local:"):
        raise HTTPException(409, "clip_artifact_not_local")
    # 官方解析器 r2.local_path_for：local: 后是相对 backend cwd 的路径（实际根 =
    # backend/studio-artifacts，不在 TEMP_DIR 下——2026-10-28 首投实测修正）。
    candidates: list[Path] = []
    try:
        from . import r2 as r2_storage

        candidates.append(r2_storage.local_path_for(key))
    except Exception:  # noqa: BLE001 — 解析失败走回退根
        pass
    rest = Path(key[len("local:"):])
    if not rest.is_absolute():
        candidates.append(Path(__file__).resolve().parent.parent / rest)
    for cand in candidates:
        if cand.is_file():
            return {
                "path": cand,
                "job_id": row["job_id"],
                "sha256_db": row["sha256"],
                "size_bytes": int(row["size_bytes"] or 0),
            }
    raise HTTPException(404, "clip_file_missing")


def deliver_clip_file(path: Path, meta: dict[str, Any], *, transport: Optional[httpx.BaseTransport] = None) -> dict[str, Any]:
    """POST 成片到接收端（multipart：file + meta JSON）。返回接收端 JSON 响应。

    - sha256 分块流式计算（成片可上百 MB，不全量读内存）；
    - 上游 4xx 透传状态码（401/403 鉴权、413 大小、429 限流），其余归一为 502；
    - transport 参数仅测试用（httpx.MockTransport）。
    """
    url, token = drop_config()
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    payload = {**meta, "sha256": digest.hexdigest(), "filename": path.name, "size": path.stat().st_size}
    with path.open("rb") as fh, httpx.Client(
        timeout=httpx.Timeout(600.0, connect=10.0), transport=transport
    ) as client:
        resp = client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            data={"meta": json.dumps(payload, ensure_ascii=False)},
            files={"file": (path.name, fh, "video/mp4")},
        )
    if resp.status_code >= 400:
        raise HTTPException(
            resp.status_code if resp.status_code in (401, 403, 413, 429) else 502,
            {
                "detail": "drop_receiver_error",
                "upstream_status": resp.status_code,
                "upstream_body": resp.text[:300],
            },
        )
    try:
        return resp.json()
    except ValueError:
        return {"ok": True, "raw": resp.text[:300]}
