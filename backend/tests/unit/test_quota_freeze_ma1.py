"""(0,0) 配额冻结语义 fixture 测试（阶段一 · 隔离测试环境专用）。

**隔离边界**：AccessLedger 实例化在 pytest tmp_path 下的独立 SQLite 文件，
绝不触碰生产 ACL（/domain/.studio-resources/access/access.sqlite3）；
不使用模块级 `ledger()`/`register_created_object()`（那会落在默认生产路径）。

验证 M1 §2 冻结/解冻设计：
* quotas 行 (0,0)：重操作 reserve → 429（存储/并发两条路径）；
  light 操作（get/require）不经 reserve，完全不受影响；
* 解冻 UPDATE → (100GiB, 2)：重操作恢复，配额与并发上限真实生效；
* 再冻结：双向可逆。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.studio_access import DOMAIN_ID, AccessLedger

SUBJECT = "ma1-new-user-subject"
OTHER = "ma1-other-subject"
GB = 1024**3
FROZEN = (0, 0)
OPEN = (100 * GB, 2)


def _principal(subject: str = SUBJECT) -> dict:
    return {"subject": subject, "slug": "", "domain_id": DOMAIN_ID, "expires_at_ms": 0}


@pytest.fixture()
def ledger(tmp_path):
    return AccessLedger(tmp_path / "ma1-acl.sqlite3")


def _set_quota(ledger, subject: str, limits: tuple[int, int]) -> None:
    with ledger.connect() as db:
        db.execute(
            "INSERT INTO quotas(domain,subject,bytes_limit,concurrent_limit) VALUES(?,?,?,?)"
            " ON CONFLICT(domain,subject) DO UPDATE SET bytes_limit=excluded.bytes_limit,"
            " concurrent_limit=excluded.concurrent_limit",
            (DOMAIN_ID, subject, limits[0], limits[1]),
        )


def test_quota_table_starts_empty(ledger):
    with ledger.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM quotas").fetchone()[0] == 0


def test_frozen_000_blocks_heavy_operation_with_429(ledger):
    ledger.register("project", "p1", _principal(), size_bytes=1024)
    _set_quota(ledger, SUBJECT, FROZEN)
    with pytest.raises(HTTPException) as exc:
        ledger.reserve(_principal(), "op-upload-1", size_bytes=2048)
    assert exc.value.status_code == 429
    assert exc.value.detail == "studio_user_storage_quota"


def test_frozen_000_leaves_light_operations_unaffected(ledger):
    """查看/下载/删除等 light 操作不经 reserve——注册/读取/归属校验全部正常。"""
    ledger.register("project", "p1", _principal(), size_bytes=1024)
    ledger.register("source", "local:src.mp4", _principal(), project_id="p1", size_bytes=4096)
    _set_quota(ledger, SUBJECT, FROZEN)
    assert ledger.get("project", "p1") is not None          # 读
    assert ledger.get("source", "local:src.mp4") is not None
    ledger.require(_principal(), "project", "p1")            # light 归属校验（读语义）
    ledger.require(_principal(), "source", "local:src.mp4")  # 同上
    ledger.require(_principal(), "project", "p1", write=True)  # 台账级 light 写（删除/改名类）
    row = ledger.get("project", "p1")
    assert row["owner"] == SUBJECT


def test_unfreeze_to_100gib_2_restores_heavy_operations(ledger):
    ledger.register("project", "p1", _principal(), size_bytes=1024)
    _set_quota(ledger, SUBJECT, FROZEN)
    with pytest.raises(HTTPException):
        ledger.reserve(_principal(), "op-1", size_bytes=2048)
    _set_quota(ledger, SUBJECT, OPEN)  # 解冻：D10 终版 100GiB / 2
    ledger.reserve(_principal(), "op-1", size_bytes=2048)          # 恢复
    ledger.reserve(_principal(), "op-2", size_bytes=1024)          # 第二个并发允许
    with pytest.raises(HTTPException) as exc:
        ledger.reserve(_principal(), "op-3", size_bytes=1024)      # 第三个 → 并发 429
    assert exc.value.detail == "studio_user_concurrency_quota"
    ledger.release("op-1")
    ledger.reserve(_principal(), "op-3", size_bytes=1024)          # 释放后恢复


def test_unfrozen_storage_cap_still_enforced(ledger):
    ledger.register("project", "p1", _principal(), size_bytes=100 * GB - 512 * 1024 * 1024)
    _set_quota(ledger, SUBJECT, OPEN)
    with pytest.raises(HTTPException) as exc:
        ledger.reserve(_principal(), "op-big", size_bytes=600 * 1024 * 1024)
    assert exc.value.detail == "studio_user_storage_quota"          # 逼近 100GiB 上限仍拒绝
    ledger.reserve(_principal(), "op-ok", size_bytes=256 * 1024 * 1024)  # 限额内正常


def test_per_subject_isolation_frozen_vs_open(ledger):
    """冻结只影响对应 subject；其他账号（已开放）不受牵连。"""
    ledger.register("project", "pa", _principal(), size_bytes=1024)
    ledger.register("project", "pb", _principal(OTHER), size_bytes=1024)
    _set_quota(ledger, SUBJECT, FROZEN)
    _set_quota(ledger, OTHER, OPEN)
    with pytest.raises(HTTPException):
        ledger.reserve(_principal(), "op-a", size_bytes=1024)       # 冻结账号拒绝
    ledger.reserve(_principal(OTHER), "op-b", size_bytes=1024)      # 开放账号正常


def test_refreeze_is_reversible(ledger):
    ledger.register("project", "p1", _principal(), size_bytes=1024)
    _set_quota(ledger, SUBJECT, OPEN)
    ledger.reserve(_principal(), "op-1", size_bytes=1024)
    _set_quota(ledger, SUBJECT, FROZEN)                             # 再冻结（应急收紧）
    with pytest.raises(HTTPException):
        ledger.reserve(_principal(), "op-2", size_bytes=1024)
    _set_quota(ledger, SUBJECT, OPEN)
    ledger.reserve(_principal(), "op-2", size_bytes=1024)           # 双向可逆
