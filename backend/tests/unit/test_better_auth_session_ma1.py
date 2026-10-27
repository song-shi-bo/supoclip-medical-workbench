"""Better Auth 会话验证模块隔离测试（阶段一 · 隔离测试环境专用）。

**隔离边界**：
* 仅连接独立 PG 集群（127.0.0.1:5433/supoclip_test，MA1_ISO_PG_DSN 可覆盖），
  连不上即 skip——绝不触达生产 5432；
* cookie 样本含 2026-10-23 spike 捕获的**真实 better-auth 1.6.9 输出**；
* 全程不读生产 frontend/.env 密钥（secret 为隔离测试专用值）。

覆盖：有效/过期/登出撤销/伪造签名/伪造token/缺cookie/多cookie解析/删号残留会话/签名算法对照。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from src.better_auth_session import (
    extract_session_cookie,
    make_signed_value,
    principal_from_cookie,
    split_signed_value,
    verify_signature,
)

DSN = os.environ.get("MA1_ISO_PG_DSN", "postgresql://ma1admin@127.0.0.1:5433/supoclip_test")
SECRET = "ma1-isolated-test-secret-0f3a"
DOMAIN_ID = "ma1-test-domain"
USER_ID = "9GtphgF9GS2SNzAUBany7yCsKN7vTFbX"  # spike 注册的真实用户

# asyncpg 连接在 session 级 loop 的 fixture 中创建，测试必须同 loop（防跨 loop 协议损坏）
pytestmark = pytest.mark.asyncio(loop_scope="session")

# 2026-10-23 spike 真实捕获（better-auth 1.6.9 sign-in 输出，隔离库中该会话仍有效）
REAL_COOKIE_HEADER = (
    "better-auth.session_token=UFJUSRRMjXx5x5wUIrOb51y3Y5POzlP9."
    "MKKXsMXPlMX%2BbolXymkqMso6i7ESotkgtXOKXgKcR1M%3D"
)
REAL_TOKEN = "UFJUSRRMjXx5x5wUIrOb51y3Y5POzlP9"


async def _connect():
    return await asyncpg.connect(DSN)


@pytest.fixture()
async def iso_db():
    try:
        conn = await asyncpg.connect(timeout=5, **{
            "host": "127.0.0.1", "port": 5433, "user": "ma1admin", "database": "supoclip_test",
        })
    except (OSError, asyncpg.PostgresError):
        pytest.skip("隔离 PG(5433/supoclip_test) 未运行——跳过，不触达任何生产资源")
    yield conn
    await conn.close()


async def _insert_session(conn, token: str, user_id: str, *, expired: bool = False) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # 列为 timestamp without time zone
    expires = now + timedelta(days=7) if not expired else now - timedelta(minutes=1)
    await conn.execute(
        'INSERT INTO "session" (id, token, "userId", "expiresAt", "createdAt", "updatedAt")'
        " VALUES ($1, $2, $3, $4, $5, $5) ON CONFLICT (id) DO NOTHING",
        f"ma1-{token[:20]}", token, user_id, expires, now,
    )


async def _cleanup_session(conn, token: str) -> None:
    await conn.execute('DELETE FROM "session" WHERE token = $1', token)


# ---------------------------------------------------------------------------
# 纯函数：解析 / 签名
# ---------------------------------------------------------------------------


def test_extract_session_cookie_parses_multi_cookie_header():
    header = f"other=1; {REAL_COOKIE_HEADER}; x=2"
    value = extract_session_cookie(header)
    assert value is not None and value.startswith(REAL_TOKEN + ".")


def test_extract_session_cookie_missing_or_empty():
    assert extract_session_cookie(None) is None
    assert extract_session_cookie("") is None
    assert extract_session_cookie("other=1; x=2") is None


def test_extract_session_cookie_secure_prefix_name():
    # 生产 https baseURL：Better Auth secure cookie 名带 __Secure- 前缀（阶段 5 试点实测）
    token, sig = "tok456", "sigABC"
    header = f"other=1; __Secure-better-auth.session_token={token}.{sig}"
    assert extract_session_cookie(header) == f"{token}.{sig}"
    # 候选顺序：无前缀名同样可识别（隔离环境兼容）
    assert extract_session_cookie(f"better-auth.session_token={token}.{sig}") == f"{token}.{sig}"
    # 显式指定具体名时仅识别该名（既有调用行为不变）
    assert extract_session_cookie(header, "better-auth.session_token") is None
    assert extract_session_cookie(header, "__Secure-better-auth.session_token") == f"{token}.{sig}"


def test_split_signed_value_shape():
    parts = split_signed_value(make_signed_value("tok123", SECRET))
    assert parts is not None and parts[0] == "tok123"
    assert split_signed_value("no-dot-value") is None


def test_signature_algorithm_matches_real_better_auth_output():
    """真实 spike 捕获签名必须能被我们的算法以同 secret 复现。"""
    token, sig = split_signed_value(extract_session_cookie(REAL_COOKIE_HEADER))
    assert verify_signature(token, sig, SECRET) is True
    assert verify_signature(token, sig, "wrong-secret") is False
    assert verify_signature(token[:-1] + ("0" if token[-1] != "0" else "1"), sig, SECRET) is False


# ---------------------------------------------------------------------------
# 端到端：cookie → principal（隔离 PG）
# ---------------------------------------------------------------------------


async def test_real_better_auth_cookie_resolves_principal(iso_db):
    principal = await principal_from_cookie(REAL_COOKIE_HEADER, SECRET, iso_db, DOMAIN_ID)
    assert principal is not None
    assert principal["subject"] == USER_ID
    assert principal["domain_id"] == DOMAIN_ID
    assert principal["expires_at_ms"] > 0


async def test_expired_session_rejected(iso_db):
    token = "ma1-expired-token-aaaaaaaaaaaa"
    await _insert_session(iso_db, token, USER_ID, expired=True)
    cookie = make_signed_value(token, SECRET)
    assert await principal_from_cookie(f"better-auth.session_token={cookie}", SECRET, iso_db, DOMAIN_ID) is None


async def test_logout_revoked_session_rejected(iso_db):
    token = "ma1-revoked-token-aaaaaaaaaaaa"
    await _insert_session(iso_db, token, USER_ID)
    cookie = f"better-auth.session_token={make_signed_value(token, SECRET)}"
    assert await principal_from_cookie(cookie, SECRET, iso_db, DOMAIN_ID) is not None
    await _cleanup_session(iso_db, token)  # 登出撤销 = session 行删除（spike 实证语义）
    assert await principal_from_cookie(cookie, SECRET, iso_db, DOMAIN_ID) is None


async def test_forged_signature_and_token_rejected(iso_db):
    good = make_signed_value(REAL_TOKEN, SECRET)
    token, sig = good.split(".")
    tampered_sig = sig[:-4] + "AAAA"
    assert await principal_from_cookie(
        f"better-auth.session_token={token}.{tampered_sig}", SECRET, iso_db, DOMAIN_ID) is None
    assert await principal_from_cookie(
        f"better-auth.session_token=badtoken0000000000000000000000.{sig}", SECRET, iso_db, DOMAIN_ID) is None


async def test_missing_or_malformed_cookie_rejected(iso_db):
    assert await principal_from_cookie(None, SECRET, iso_db, DOMAIN_ID) is None
    assert await principal_from_cookie("", SECRET, iso_db, DOMAIN_ID) is None
    assert await principal_from_cookie("better-auth.session_token=garbage", SECRET, iso_db, DOMAIN_ID) is None


async def test_deleted_user_residual_session_rejected(iso_db):
    """删号残留会话：绕 FK 插入孤儿会话（replication_role），用户不存在必须拒绝。"""
    ghost = "ma1-ghost-user-token-aaaaaaaaa"
    await iso_db.execute("SET session_replication_role = replica")
    try:
        naive_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        await iso_db.execute(
            'INSERT INTO "session" (id, token, "userId", "expiresAt", "createdAt", "updatedAt")'
            ' VALUES ($1, $2, $3, $4, $5, $5)',
            "ma1-ghost-session", ghost, "ghost-user-id-nonexistent",
            naive_utc + timedelta(days=1), naive_utc,
        )
    finally:
        await iso_db.execute("SET session_replication_role = origin")
    cookie = f"better-auth.session_token={make_signed_value(ghost, SECRET)}"
    assert await principal_from_cookie(cookie, SECRET, iso_db, DOMAIN_ID) is None
    await _cleanup_session(iso_db, ghost)


async def test_production_secret_isolation(iso_db):
    """密钥域隔离：同一 token 用 secret-A 签名、secret-B 验证必须失败。"""
    token = "ma1-cross-secret-token-aaaaaaa"
    await _insert_session(iso_db, token, USER_ID)
    signed_with_a = make_signed_value(token, "secret-A-aaa")
    assert await principal_from_cookie(
        f"better-auth.session_token={signed_with_a}", "secret-B-bbb", iso_db, DOMAIN_ID
    ) is None
    assert await principal_from_cookie(
        f"better-auth.session_token={signed_with_a}", "secret-A-aaa", iso_db, DOMAIN_ID
    ) is not None  # 同 secret 对照组通过
