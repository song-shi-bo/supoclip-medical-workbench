"""Better Auth 会话 cookie 的 FastAPI 侧验证（M1 §1.2 规格 · 隔离环境实测版）。

## 实测格式（better-auth 1.6.9 · 2026-10-23 隔离环境 spike，真实 sign-up/sign-in）

    Cookie: better-auth.session_token=<token>.<urlencode(base64(HMAC-SHA256(secret, token)))>
    * DB ``session.token`` 存**裸 token**（32 位 URL 安全随机串）
    * 签名 = base64 标准（含 ``=`` padding），cookie 内再做 URL 编码（%2F %2B %3D）
    * 算法已用 Python 独立复现验证（hmac.compare_digest 口径）
    * Max-Age=604800（7 天）；HttpOnly; SameSite=Lax; Path=/
    * 登出撤销 = ``session`` 行删除（spike 实证 3→2）

## 验证步骤（对应 M1 冻结 §1.2）

    1) 从 Cookie 头解析该名值
    2) 从右侧最后一个 ``.`` 分离 token / 签名；签名 URL 解码 + base64 解码
    3) ``hmac.compare_digest`` 常数时间比对
    4) ``session`` 表按 token 查：无行 / 过期 → 认证失败
    5) ``users`` 存在性回查（防删号残留会话）
    6) 构造 principal：``{subject: userId, slug: "", domain_id: <DOMAIN_ID>, expires_at_ms}``

## 接线边界（本阶段约束）

本模块**未接入** StudioAuthenticationMiddleware（接线属 M3，需部署窗口）；
生产服务当前不 import 本模块，行为零变化。PG 不可达的异常由调用方映射 503；
本模块对认证失败一律返回 ``None``，不抛业务异常。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from datetime import timezone
from typing import Any, Optional

DEFAULT_COOKIE_NAME = "better-auth.session_token"
# 生产 https baseURL 下 Better Auth 启用 secure cookie（名字带 __Secure- 前缀）；
# 隔离环境 http 无前缀。校验时按候选顺序识别（阶段 5 生产试点实测发现）。
COOKIE_NAME_CANDIDATES = ("__Secure-" + DEFAULT_COOKIE_NAME, DEFAULT_COOKIE_NAME)


def extract_session_cookie(cookie_header: Optional[str], name: Optional[str] = None) -> Optional[str]:
    """从 Cookie 头解析指定 cookie 值；无/空/不含该名 → None。

    容忍 ``", "`` 组合头与 ``"; "`` 分隔（ASGI 单头形态）。
    ``name`` 缺省时按 ``COOKIE_NAME_CANDIDATES`` 顺序识别（secure 前缀优先）；
    显式传入 name 时仅识别该名（既有测试行为不变）。
    """
    if not cookie_header:
        return None
    names = (name,) if name is not None else COOKIE_NAME_CANDIDATES
    for part in cookie_header.replace(", ", "; ").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        if key.strip() in names:
            return value.strip() or None
    return None


def split_signed_value(value: str) -> Optional[tuple[str, str]]:
    """``token.signature`` → (token, urlencoded_base64_sig)；格式不符 → None。"""
    if not value or "." not in value:
        return None
    token, _, sig = value.rpartition(".")
    if not token or not sig:
        return None
    return token, sig


def verify_signature(token: str, urlencoded_sig: str, secret: str | bytes) -> bool:
    """常数时间校验 ``base64(HMAC-SHA256(secret, token))`` 与 cookie 签名段。"""
    try:
        key = secret.encode() if isinstance(secret, str) else secret
        expected_digest = hmac.new(key, token.encode(), hashlib.sha256).digest()
        provided_digest = base64.b64decode(urllib.parse.unquote(urlencoded_sig), validate=True)
    except (ValueError, TypeError, UnicodeError):
        return False
    return hmac.compare_digest(expected_digest, provided_digest)


def make_signed_value(token: str, secret: str | bytes) -> str:
    """按实测算法构造合法 cookie 值（测试/规格对照用；与 better-auth 输出等价）。"""
    key = secret.encode() if isinstance(secret, str) else secret
    sig = base64.b64encode(hmac.new(key, token.encode(), hashlib.sha256).digest()).decode()
    return f"{token}.{urllib.parse.quote(sig, safe='')}"


async def principal_from_cookie(
    cookie_header: Optional[str],
    secret: str | bytes,
    conn: Any,
    domain_id: str,
    now_ms: Optional[int] = None,
    cookie_name: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """cookie 头 → principal（M1 冻结 §1.2）；任何认证失败返回 None。

    ``conn``：asyncpg 连接/池（测试注入隔离库；生产接线时注入应用连接池）。
    PG 异常向上抛（调用方映射 503）；认证失败一律 ``None``（调用方映射 401）。
    """
    value = extract_session_cookie(cookie_header, cookie_name)
    if not value:
        return None
    parts = split_signed_value(value)
    if parts is None:
        return None
    token, sig = parts
    if not verify_signature(token, sig, secret):
        return None

    row = await conn.fetchrow(
        'SELECT "userId", "expiresAt" FROM "session" WHERE token = $1 LIMIT 1', token
    )
    if row is None:
        return None
    user_id = row["userId"]
    expires = row["expiresAt"]
    if expires.tzinfo is None:  # 列为 timestamp without time zone 时按 UTC 解释
        expires = expires.replace(tzinfo=timezone.utc)
    expires_ms = int(expires.timestamp() * 1000)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    if expires_ms <= now:
        return None
    user = await conn.fetchval('SELECT id FROM "users" WHERE id = $1 LIMIT 1', user_id)
    if user is None:
        return None
    return {
        "subject": user_id,
        "slug": "",
        "domain_id": domain_id,
        "expires_at_ms": expires_ms,
    }
