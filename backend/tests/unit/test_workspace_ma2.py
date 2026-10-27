"""多账号阶段二隔离测试：workspace 幂等创建、越权拒绝、冻结/解冻、配额互不影响。

**隔离边界**：
* ACL 实例化在 pytest tmp_path 下的独立 SQLite 文件——绝不触碰生产 ACL；
* Better Auth 集成用例仅连接隔离 PG（127.0.0.1:5433/supoclip_test），连不上即 skip；
* 授权主体 = Better Auth user.id（principal.subject），不使用账号名/邮箱。

覆盖（阶段二验收 6 项）：
1. ensure_workspace 幂等创建（重复调用同 workspace、无重复行、冻结配额 (0,0)）
2. 账号间越权拒绝（读/写/删除/可见性/授权管理）
3. 冻结态：读/台账写可用，重操作（存储/并发）429
4. 显式解冻 (100GiB,2) 后重操作恢复，上限真实生效
5. 不同账号配额互不影响
6. Better Auth user.id → principal → ensure_workspace 集成（subject 稳定性）
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import secrets
from fastapi import HTTPException

from src.studio_access import DOMAIN_ID, AccessLedger
from src.studio_workspace import (
    FROZEN_QUOTA,
    OPEN_QUOTA,
    ensure_workspace,
    set_workspace_quota,
    unfreeze_workspace,
)

GB = 1024**3


def _principal(subject: str) -> dict:
    return {"subject": subject, "slug": "", "domain_id": DOMAIN_ID, "expires_at_ms": 0}


USER_A = "11111111-1111-4111-8111-111111111111"  # Better Auth user.id 形态
USER_B = "22222222-2222-4222-8222-222222222222"


@pytest.fixture()
def acl(tmp_path):
    return AccessLedger(tmp_path / "ma2-acl.sqlite3")


# ---------------------------------------------------------------------------
# 1. ensure_workspace 幂等创建
# ---------------------------------------------------------------------------

def test_ensure_workspace_is_idempotent(acl):
    with acl.connect() as db:
        first = ensure_workspace(db, _principal(USER_A))
        second = ensure_workspace(db, _principal(USER_A))  # 第二次登录/请求
        assert first["workspace_id"] == second["workspace_id"]
        assert first["workspace_id"].startswith("ws_") and len(first["workspace_id"]) == 15

        assert db.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM workspace_members").fetchone()[0] == 1
        member = db.execute(
            "SELECT role FROM workspace_members WHERE subject=?", (USER_A,)
        ).fetchone()
        assert member["role"] == "owner"

        quota = db.execute(
            "SELECT bytes_limit, concurrent_limit FROM quotas WHERE subject=?", (USER_A,)
        ).fetchone()
        assert (quota["bytes_limit"], quota["concurrent_limit"]) == FROZEN_QUOTA  # D14 冻结

        # 不同账号 → 不同 workspace（UNIQUE(domain, owner_subject)）
        ensure_workspace(db, _principal(USER_B))
        ws = db.execute("SELECT owner_subject, workspace_id FROM workspaces").fetchall()
        assert {r["owner_subject"] for r in ws} == {USER_A, USER_B}
        assert len({r["workspace_id"] for r in ws}) == 2


def test_ensure_workspace_does_not_overwrite_unfrozen_quota(acl):
    """解冻后再登录（重复 ensure）不得把配额覆盖回冻结值。"""
    with acl.connect() as db:
        ensure_workspace(db, _principal(USER_A))
        unfreeze_workspace(db, DOMAIN_ID, USER_A)
        ensure_workspace(db, _principal(USER_A))  # 再次登录
        quota = db.execute(
            "SELECT bytes_limit, concurrent_limit FROM quotas WHERE subject=?", (USER_A,)
        ).fetchone()
        assert (quota["bytes_limit"], quota["concurrent_limit"]) == OPEN_QUOTA


# ---------------------------------------------------------------------------
# 2. 账号间越权拒绝（服务端 ACL 校验，不信任客户端身份）
# ---------------------------------------------------------------------------

def test_cross_account_project_access_denied(acl):
    with acl.connect() as db:
        ensure_workspace(db, _principal(USER_A))
        ensure_workspace(db, _principal(USER_B))
    acl.register("project", "proj-a", _principal(USER_A), size_bytes=1024)

    # B 对 A 的项目：读/写/删除（台账级 light 写）全部 403
    with pytest.raises(HTTPException) as exc:
        acl.require(_principal(USER_B), "project", "proj-a")
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        acl.require(_principal(USER_B), "project", "proj-a", write=True)

    # B 的可见项目列表不含 A 的项目；B 自己的项目对 B 可见
    acl.register("project", "proj-b", _principal(USER_B), size_bytes=1024)
    visible_b = acl.filter_visible(_principal(USER_B), "project",
                                   [{"project_id": "proj-a"}, {"project_id": "proj-b"}])
    assert [r["project_id"] for r in visible_b] == ["proj-b"]
    visible_a = acl.filter_visible(_principal(USER_A), "project",
                                   [{"project_id": "proj-a"}, {"project_id": "proj-b"}])
    assert [r["project_id"] for r in visible_a] == ["proj-a"]

    # A 的项目下的 source（成片/素材），B 同样越权
    acl.register("source", "local:clip-a.mp4", _principal(USER_A), project_id="proj-a", size_bytes=4096)
    with pytest.raises(HTTPException):
        acl.require(_principal(USER_B), "source", "local:clip-a.mp4", write=True)


def test_client_supplied_identity_cannot_take_over(acl):
    """即使客户端传入他人 user_id 作为对象参数，授权主体仍来自服务端 principal。"""
    with acl.connect() as db:
        ensure_workspace(db, _principal(USER_A))
        ensure_workspace(db, _principal(USER_B))
    acl.register("project", "proj-a", _principal(USER_A), size_bytes=1024)
    # B 伪装请求（principal 仍是 B）：试图以 A 的 workspace/项目名义 require → 403
    with pytest.raises(HTTPException):
        acl.require(_principal(USER_B), "project", "proj-a", write=True)
    # B 无法把 A 的项目授权给第三方（非 owner）
    with pytest.raises(HTTPException) as exc:
        acl.grant(_principal(USER_B), "project", "proj-a", USER_B, "editor")
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# 3/4. 冻结 (0,0)：light 可用、重操作拒绝；显式解冻恢复且上限真实生效
# ---------------------------------------------------------------------------

def test_frozen_from_ensure_workspace_allows_light_blocks_heavy(acl):
    with acl.connect() as db:
        ensure_workspace(db, _principal(USER_A))  # 冻结配额来自 ensure_workspace，非手工写入
    acl.register("project", "proj-a", _principal(USER_A), size_bytes=1024)
    acl.register("source", "local:src.mp4", _principal(USER_A), project_id="proj-a", size_bytes=4096)

    # light：读 / 归属校验 / 台账级写（删除、改名类）全部可用
    assert acl.get("project", "proj-a") is not None
    acl.require(_principal(USER_A), "project", "proj-a")
    acl.require(_principal(USER_A), "project", "proj-a", write=True)

    # 重操作：任何正 size reserve → 429 存储（含零 size——已有用量>0 时存储先触发）
    with pytest.raises(HTTPException) as exc:
        acl.reserve(_principal(USER_A), "op-upload", size_bytes=2048)
    assert (exc.value.status_code, exc.value.detail) == (429, "studio_user_storage_quota")

    # 零用量冻结账号：零 size 重操作 → 并发 0 上限先触发 429
    with acl.connect() as db:
        ensure_workspace(db, _principal(USER_B))
    with pytest.raises(HTTPException) as exc:
        acl.reserve(_principal(USER_B), "op-render", size_bytes=0)
    assert (exc.value.status_code, exc.value.detail) == (429, "studio_user_concurrency_quota")


def test_unfreeze_restores_heavy_and_cap_enforced(acl):
    with acl.connect() as db:
        ensure_workspace(db, _principal(USER_A))
    acl.register("project", "proj-a", _principal(USER_A), size_bytes=100 * GB - 512 * 1024 * 1024)

    with pytest.raises(HTTPException):
        acl.reserve(_principal(USER_A), "op-x", size_bytes=1024)  # 冻结时拒绝
    with acl.connect() as db:
        unfreeze_workspace(db, DOMAIN_ID, USER_A)  # 显式解冻 (100GiB, 2)

    acl.reserve(_principal(USER_A), "op-1", size_bytes=1024)               # 恢复
    acl.reserve(_principal(USER_A), "op-2", size_bytes=1024)               # 第二并发
    with pytest.raises(HTTPException) as exc:
        acl.reserve(_principal(USER_A), "op-3", size_bytes=1024)           # 第三并发 → 429
    assert exc.value.detail == "studio_user_concurrency_quota"
    with pytest.raises(HTTPException) as exc:
        acl.reserve(_principal(USER_A), "op-big", size_bytes=600 * 1024 * 1024)  # 超上限 → 429
    assert exc.value.detail == "studio_user_storage_quota"


# ---------------------------------------------------------------------------
# 5. 不同账号配额互不影响
# ---------------------------------------------------------------------------

def test_quota_independence_between_accounts(acl):
    with acl.connect() as db:
        ensure_workspace(db, _principal(USER_A))  # 冻结
        ensure_workspace(db, _principal(USER_B))
        unfreeze_workspace(db, DOMAIN_ID, USER_A)  # 仅 A 解冻

    acl.register("project", "pa", _principal(USER_A), size_bytes=1024)
    acl.register("project", "pb", _principal(USER_B), size_bytes=1024)

    acl.reserve(_principal(USER_A), "op-a1", size_bytes=800 * 1024 * 1024)  # A 大额预留 OK
    with pytest.raises(HTTPException) as exc:
        acl.reserve(_principal(USER_B), "op-b1", size_bytes=1024)           # B 仍冻结 → 429
    assert (exc.value.status_code, exc.value.detail) == (429, "studio_user_storage_quota")
    # A 的用量不计入 B；B 的拒绝不影响 A
    acl.reserve(_principal(USER_A), "op-a2", size_bytes=1024)


# ---------------------------------------------------------------------------
# 6. Better Auth user.id → principal → ensure_workspace 集成（隔离 PG 5433）
# ---------------------------------------------------------------------------

DSN = os.environ.get("MA1_ISO_PG_DSN", "postgresql://ma1admin@127.0.0.1:5433/supoclip_test")
SECRET = "ma1-isolated-test-secret-0f3a"
pytestmark_integration = pytest.mark.asyncio(loop_scope="session")


async def _make_user_and_cookie(conn, email: str, user_id: str) -> str:
    from src.better_auth_session import make_signed_value

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await conn.execute(
        'INSERT INTO "users" (id, email, name, "emailVerified", "createdAt", "updatedAt")'
        " VALUES ($1, $2, $3, false, $4, $4) ON CONFLICT (id) DO NOTHING",
        user_id, email, "MA2", now,
    )
    token = f"ma2-{user_id[:8]}-{secrets.token_urlsafe(8)}"
    await conn.execute(
        'INSERT INTO "session" (id, token, "userId", "expiresAt", "createdAt", "updatedAt")'
        " VALUES ($1, $2, $3, $4, $5, $5) ON CONFLICT (id) DO NOTHING",
        f"ma2-{token[:20]}", token, user_id, now + timedelta(days=7), now,
    )
    return make_signed_value(token, SECRET)


class TestBetterAuthWorkspaceIntegration:
    """真实隔离库（5433）：cookie → principal(subject=user.id) → ensure_workspace。"""

    pytestmark = pytest.mark.asyncio(loop_scope="session")

    async def _connect(self):
        try:
            return await asyncpg.connect(timeout=5, host="127.0.0.1", port=5433,
                                         user="ma1admin", database="supoclip_test")
        except (OSError, asyncpg.PostgresError):
            pytest.skip("隔离 PG(5433/supoclip_test) 未运行——跳过，不触达任何生产资源")

    async def test_cookie_principal_creates_workspace_owned_by_user_id(self, tmp_path):
        import secrets as _s

        conn = await self._connect()
        try:
            acl = AccessLedger(tmp_path / "ma2-int-acl.sqlite3")
            user_id = f"ma2{secrets.token_hex(10)}"
            cookie = await _make_user_and_cookie(conn, f"ma2-{secrets.token_hex(4)}@example.com", user_id)

            from src.better_auth_session import principal_from_cookie
            principal = await principal_from_cookie(
                f"better-auth.session_token={cookie}", SECRET, conn, DOMAIN_ID
            )
            assert principal is not None
            assert principal["subject"] == user_id  # 稳定 user.id，非邮箱/账号名

            with acl.connect() as db:
                info = ensure_workspace(db, principal)
                assert info["owner_subject"] == user_id
                again = ensure_workspace(db, principal)
                assert again["workspace_id"] == info["workspace_id"]
                with acl.connect() as db2:
                    owner = db2.execute(
                        "SELECT owner_subject FROM workspaces WHERE workspace_id=?",
                        (info["workspace_id"],),
                    ).fetchone()
                    assert owner["owner_subject"] == user_id
        finally:
            await conn.close()
