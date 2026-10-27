"""阶段五前置：双通道认证模式（STUDIO_AUTH_BACKEND=dual）隔离测试。

**覆盖**：
1. 请求携带有效 Better Auth 会话 cookie → user.id principal（平台 socket 不被调用）；
2. cookie 存在但签名伪造 → 401 fail-closed（**不降级到平台**——防身份降级）；
3. 无 Better Auth cookie → 平台认证 socket（假 socket 返回平台 principal，存量用户零影响）；
4. 双通道下 ensure_workspace 按 user.id 触发（幂等性由 studio_workspace 单元测试保证）。

**隔离边界**：Better Auth 校验用假 asyncpg 池；平台回退用临时 unix socket 假认证服务；
ACL 为 tmp 实例。
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest
from starlette.requests import Request

import src.studio_access as studio_access
from src.studio_access import DOMAIN_ID, StudioAuthenticationMiddleware

SECRET = "ma1-isolated-test-secret-0f3a"
USER_ID = "66666666-6666-4666-8666-666666666666"
PLATFORM_SUBJECT = "8"
PLATFORM_SLUG = "songshibo"


def _sign(token: str, secret: str = SECRET) -> str:
    import base64
    import hashlib
    import hmac

    sig = base64.b64encode(hmac.new(secret.encode(), token.encode(), hashlib.sha256).digest()).decode()
    return urllib.parse.quote(sig, safe="")


def _cookie(token: str = "dual-tok", secret: str = SECRET) -> str:
    return f"better-auth.session_token={token}.{_sign(token, secret)}"


class _StubState:
    studio_access_ledger = None


class _StubApp:
    def __init__(self, acl=None):
        self.state = type("S", (), {"studio_access_ledger": acl})()


class _FakeASGIApp:
    def __init__(self):
        self.seen_principal = None

    async def __call__(self, scope, receive, send):
        self.seen_principal = scope.get("state", {}).get("studio_principal")


def _make_scope(cookie: str | None, method: str = "GET", path: str = "/studio/projects",
                app=None):
    headers = []
    if cookie is not None:
        headers.append((b"cookie", cookie.encode()))
    scope = {"type": "http", "method": method, "path": path, "headers": headers}
    if app is not None:
        scope["app"] = app
    return scope


async def _receive():
    return {"type": "http.request", "body": b""}


async def _send(message):
    pass


async def start_fake_platform_socket(path: str, principal: dict, calls: list):
    async def handle(reader, writer):
        calls.append(1)
        try:
            await reader.readuntil(b"\r\n\r\n")
            body = json.dumps(principal).encode()
            header = ("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                      f"Content-Length: {len(body)}\r\n\r\n").encode()
            writer.write(header + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    return await asyncio.start_unix_server(handle, path=path)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def dual_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_AUTH_BACKEND", "dual")
    monkeypatch.setenv("BETTER_AUTH_SECRET", SECRET)
    sock = str(tmp_path / "platform.sock")
    monkeypatch.setenv("STUDIO_AUTH_SOCKET", sock)
    instance = studio_access.AccessLedger(tmp_path / "dual-acl.sqlite3")
    monkeypatch.setattr(studio_access, "AccessLedger", lambda path=None: instance)
    return {"sock": sock, "acl": instance}


@pytest.fixture()
def fake_ba_pool(monkeypatch):
    class _FakeConn:
        async def fetchrow(self, query, *args):
            from datetime import datetime, timedelta, timezone

            class R(dict):
                pass
            if '"session"' in query:
                naive = datetime.now(timezone.utc).replace(tzinfo=None)
                return R(userId=USER_ID, expiresAt=naive + timedelta(days=1))
            if '"users"' in query:
                return R(id=USER_ID)
            return None

        async def fetchval(self, query, *args):
            if '"users"' in query:
                return USER_ID
            return None

    class _FakePool:
        def acquire(self):
            class C:
                async def __aenter__(self):
                    return _FakeConn()

                async def __aexit__(self, *a):
                    return False
            return C()

    pool = _FakePool()
    monkeypatch.setattr(studio_access, "_ba_pool", pool)
    return pool


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dual_valid_ba_cookie_uses_user_id_principal(dual_env, fake_ba_pool):
    """有效 Better Auth cookie → user.id principal；平台 socket 不被调用。"""
    calls = []
    platform_calls = calls
    server = await start_fake_platform_socket(dual_env["sock"],
                                              principal={"subject": PLATFORM_SUBJECT}, calls=platform_calls)
    async with server:
        target = _FakeASGIApp()
        mw = StudioAuthenticationMiddleware(target)
        from src.studio_access import _better_auth_principal  # noqa: F401  (确保模块加载)
        await mw(_make_scope(_cookie("dual-tok"), app=_StubApp(dual_env["acl"])), _receive, _send)
        assert platform_calls == []
    assert target.seen_principal["subject"] == USER_ID


@pytest.mark.asyncio
async def test_dual_platform_socket_not_called_with_ba_cookie(dual_env, fake_ba_pool):
    """携带 Better Auth cookie 时平台 socket 计数为 0（双通道优先级）。"""
    calls = []
    server = await start_fake_platform_socket(dual_env["sock"], principal={}, calls=calls)
    async with server:
        app = _FakeASGIApp()
        mw = StudioAuthenticationMiddleware(_FakeASGIApp())
        await mw(_make_scope(_cookie("dual-tok"), app=_StubApp(dual_env["acl"])), _receive, _send)
    assert calls == []


@pytest.mark.asyncio
async def test_dual_forged_cookie_fails_closed_401(dual_env, fake_ba_pool):
    """cookie 签名伪造 → 401，且不降级平台（防身份降级）。"""
    calls = []
    server = await start_fake_platform_socket(dual_env["sock"], principal={}, calls=calls)
    async with server:
        app = _FakeASGIApp()
        mw = StudioAuthenticationMiddleware(_FakeASGIApp())
        sent = {}

        async def send(message):
            if message["type"] == "http.response.start":
                sent["status"] = message["status"]

        await mw(_make_scope(_cookie("dual-tok", secret="attacker-secret"), app=_StubApp(dual_env["acl"])), _receive, send)
    assert sent.get("status") == 401
    assert calls == []


@pytest.mark.asyncio
async def test_dual_without_ba_cookie_falls_back_to_platform(dual_env):
    """无 Better Auth cookie（仅平台 cookie）→ 平台 socket 回退（存量平台用户零影响）。"""
    calls = []
    platform_principal = {"subject": PLATFORM_SUBJECT, "slug": PLATFORM_SLUG,
                          "domain_id": DOMAIN_ID,
                          "expires_at_ms": int(time.time() * 1000) + 86400000}
    server = await start_fake_platform_socket(dual_env["sock"], principal=platform_principal,
                                              calls=calls)
    async with server:
        target = _FakeASGIApp()
        mw = StudioAuthenticationMiddleware(target)
        # 平台 cookie（非 Better Auth 名）→ dual 分支判定无 BA 会话 → 走平台认证
        await mw(_make_scope("dsh_session=platform-cookie", app=_StubApp(dual_env["acl"])), _receive, _send)
    assert target.seen_principal["subject"] == PLATFORM_SUBJECT


# ---------------------------------------------------------------------------
# ensure_workspace 触发（双通道 user.id 主体）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dual_ensure_workspace_triggered_by_user_id(dual_env, fake_ba_pool, monkeypatch):
    """Better Auth principal 到来时 ensure_workspace 以 user.id 触发（幂等性由其单元测试保证）。"""
    import src.studio_workspace as ws

    seen = []
    monkeypatch.setattr(ws, "ensure_workspace",
                        lambda db, principal: seen.append(principal["subject"]))
    cookie = _cookie("dual-tok")
    app = _FakeASGIApp()
    mw = StudioAuthenticationMiddleware(_FakeASGIApp())
    await mw(_make_scope(cookie, app=_StubApp(dual_env["acl"])), _receive, _send)
    assert seen == [USER_ID]
