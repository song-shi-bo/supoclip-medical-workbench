"""方案 B 开关测试：ACCOUNT_AUTO_OPEN_QUOTA（注册自动解冻，默认关闭）。

**隔离边界**：ACL 在 tmp_path；开关经 monkeypatch 控制（生产默认 false，零变化）。
"""

from __future__ import annotations

import pytest

import src.studio_access as studio_access  # noqa: F401
import src.studio_workspace as ws
from src.studio_access import DOMAIN_ID
from src.studio_workspace import ensure_workspace

UID = "77777777-7777-4777-8777-777777777777"
GB = 1024**3


def _principal():
    return {"subject": UID, "domain_id": DOMAIN_ID, "expires_at_ms": 0}


@pytest.fixture()
def acl(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCOUNT_AUTO_OPEN_QUOTA", "false")
    return tmp_path / "auto-acl.sqlite3"


def test_default_frozen_when_switch_off(acl, monkeypatch):
    monkeypatch.setattr(ws, "AUTO_OPEN_QUOTA", False)
    with studio_access.AccessLedger(str(acl)).connect() as db:
        info = ensure_workspace(db, _principal())
        assert info["frozen"] is True and info["auto_open"] is False
        q = db.execute("SELECT bytes_limit, concurrent_limit FROM quotas WHERE subject=?", (UID,)).fetchone()
        assert (q["bytes_limit"], q["concurrent_limit"]) == (0, 0)


def test_auto_open_writes_100gib_2_when_switch_on(acl, monkeypatch):
    monkeypatch.setattr(ws, "AUTO_OPEN_QUOTA", True)
    with studio_access.AccessLedger(str(acl)).connect() as db:
        info = ensure_workspace(db, _principal())
        assert info["frozen"] is False and info["auto_open"] is True
        q = db.execute("SELECT bytes_limit, concurrent_limit FROM quotas WHERE subject=?", (UID,)).fetchone()
        assert (q["bytes_limit"], q["concurrent_limit"]) == (100 * GB, 2)


def test_switch_off_does_not_rewrite_existing_open_quota(acl, monkeypatch):
    monkeypatch.setattr(ws, "AUTO_OPEN_QUOTA", True)
    with studio_access.AccessLedger(str(acl)).connect() as db:
        ensure_workspace(db, _principal())                       # 自动 (100GiB,2)
    monkeypatch.setattr(ws, "AUTO_OPEN_QUOTA", False)            # 开关随后关闭
    with studio_access.AccessLedger(str(acl)).connect() as db:
        ensure_workspace(db, _principal())                       # 再次登录
        q = db.execute("SELECT bytes_limit, concurrent_limit FROM quotas WHERE subject=?", (UID,)).fetchone()
        assert (q["bytes_limit"], q["concurrent_limit"]) == (100 * GB, 2)  # 既有配额不被回写冻结
