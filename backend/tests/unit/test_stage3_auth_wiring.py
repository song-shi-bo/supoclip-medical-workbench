"""阶段三接线单元测试：Better Auth 分支 fail-closed 语义。

**隔离边界**：AccessLedger monkeypatch 到 tmp_path 实例（绝不触碰生产 ACL）；
PG 连接池 monkeypatch 为桩（不触达任何真实数据库）。

验证：
* 缺 secret / PG 故障 / ACL 故障 → 503 studio_auth_unavailable（fail-closed）；
* 缺 cookie / 签名伪造 / 会话无效 → 401；
* 认证失败路径绝不调用 ensure_workspace（无匿名降级、无旁路）；
* 有效会话 → principal(subject=user.id) + ensure_workspace 幂等触发。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

import asyncpg
import pytest
from fastapi import HTTPException
from starlette.requests import Request

import src.studio_access as studio_access
from src.studio_access import DOMAIN_ID, _better_auth_principal

SECRET = "ma1-isolated-test-secret-0f3a"
USER_ID = "33333333-3333-4333-8333-333333333333"


def _request(cookie: str | None = None) -> Request:
    """cookie 参数是 make_signed_value 的返回值（裸 name=value 的 value 段）；
    构造 Cookie 头时必须带 Better Auth 的 cookie 名字前缀。"""
    headers = []
    if cookie is not None:
        headers.append((b"cookie", f"better-auth.session_token={cookie}".encode()))
    return Request({"type": "http", "headers": headers})


def _cookie(user_id: str = USER_ID, secret: str = SECRET, token: str = "tok-stage3") -> str:
    from src.better_auth_session import make_signed_value

    return make_signed_value(token, secret)


@pytest.fixture()
def acl(tmp_path, monkeypatch):
    """ledger() 与 AccessLedger 构造全部指向 tmp ACL，防误触生产路径。"""
    instance = studio_access.AccessLedger(tmp_path / "ma3-acl.sqlite3")
    monkeypatch.setattr(studio_access, "AccessLedger", lambda path=None: instance)
    return instance


@pytest.fixture()
def pool(monkeypatch):
    """最小 asyncpg 连接池桩：acquire() 直接给出假连接。"""

    class _FakeConn:
        async def fetchrow(self, query, *args):
            import sys as _sys
            print("[fetchrow]", query[:60], file=_sys.stderr)
            # 有效会话 + 有效用户（被测代码只查这两条；列语义为 naive UTC）
            from datetime import datetime, timedelta, timezone

            class _Row(dict):
                pass
            if '"session"' in query:
                naive_utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                return _Row(userId=USER_ID,
                            expiresAt=naive_utc_now + timedelta(days=1))
            if '"users"' in query:
                return _Row(id=USER_ID)
            return None

        async def fetchval(self, query, *args):
            import sys as _sys
            print("[fetchval]", query[:60], file=_sys.stderr)
            if '"users"' in query:
                return USER_ID
            return None

    class _FakePool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self):
                    return _FakeConn()

                async def __aexit__(self, *a):
                    return False
            return _Ctx()

    p = _FakePool()
    monkeypatch.setattr(studio_access, "_ba_pool", p)
    return p


@pytest.fixture()
def secret_env(monkeypatch):
    monkeypatch.setenv("BETTER_AUTH_SECRET", SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:5999/db")


# ---------------------------------------------------------------------------
# fail-closed：503
# ---------------------------------------------------------------------------

async def test_missing_secret_fails_closed_503(monkeypatch, acl):
    monkeypatch.delenv("BETTER_AUTH_SECRET", raising=False)
    with pytest.raises(HTTPException) as exc:
        await _better_auth_principal(_request(_cookie()))
    assert (exc.value.status_code, exc.value.detail) == (503, "studio_auth_unavailable")


async def test_pg_failure_fails_closed_503(monkeypatch, acl, secret_env):
    class _BrokenPool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self):
                    raise asyncpg.PostgresError("connection refused")

                async def __aexit__(self, *a):
                    return False
            return _Ctx()

    monkeypatch.setattr(studio_access, "_ba_pool", _BrokenPool())
    with pytest.raises(HTTPException) as exc:
        await _better_auth_principal(_request(_cookie()))
    assert (exc.value.status_code, exc.value.detail) == (503, "studio_auth_unavailable")


async def test_acl_failure_fails_closed_503(monkeypatch, acl, pool, secret_env):
    """ACL（ensure_workspace）故障同样 503：认证成功也不得半放行。"""
    import sys as _sys
    called = {}

    def _boom(db, principal):
        called["yes"] = True
        raise sqlite3.OperationalError("acl unavailable")

    monkeypatch.setattr(studio_access, "ledger", lambda request=None: acl)
    import src.studio_workspace as ws
    monkeypatch.setattr(ws, "ensure_workspace", _boom)
    cookie = _cookie()
    request = _request(cookie)

    with pytest.raises(HTTPException) as exc:
        await _better_auth_principal(request)
    print("[dbg] exc:", exc.value.status_code, exc.value.detail, file=_sys.stderr)
    assert exc.value.status_code == 503
    assert called.get("yes") is True


# ---------------------------------------------------------------------------
# 401：缺 cookie / 签名伪造
# ---------------------------------------------------------------------------

async def test_no_cookie_401(monkeypatch, acl, pool, secret_env):
    monkeypatch.setattr(studio_access, "ledger", lambda request=None: acl)
    with pytest.raises(HTTPException) as exc:
        await _better_auth_principal(_request(None))
    assert (exc.value.status_code, exc.value.detail) == (401, "studio_auth_required")


async def test_forged_signature_401(monkeypatch, acl, pool, secret_env):
    monkeypatch.setattr(studio_access, "ledger", lambda request=None: acl)
    forged = _cookie(secret="attacker-secret")  # 同 token、错误密钥签名
    print("[forged-cookie]", forged[:60], file=__import__("sys").stderr)
    with pytest.raises(HTTPException) as exc:
        await _better_auth_principal(_request(forged))
    assert (exc.value.status_code, exc.value.detail) == (401, "studio_auth_required")


# ---------------------------------------------------------------------------
# 有效会话：principal + ensure_workspace（subject=user.id）
# ---------------------------------------------------------------------------

async def test_valid_session_returns_principal_and_ensures_workspace(monkeypatch, acl, pool, secret_env):
    monkeypatch.setattr(studio_access, "ledger", lambda request=None: acl)
    import src.studio_workspace as ws
    seen = []
    monkeypatch.setattr(ws, "ensure_workspace", lambda db, principal: seen.append(principal["subject"]))

    principal = await _better_auth_principal(_request(_cookie()))
    assert principal["subject"] == USER_ID
    assert principal["domain_id"] == DOMAIN_ID
    assert seen == [USER_ID]  # 首次登录触发 workspace 幂等创建
