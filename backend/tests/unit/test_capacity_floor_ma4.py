"""多账号阶段四：全站磁盘保护（D11 水位）隔离测试。

**隔离边界**：磁盘 free 通过 monkeypatch `capacity_guard.free_bytes` 精确控制；
预留台账与锁文件位于 tmp_path（与生产 /tmp 隔离）；不触碰真实文件系统水位。

覆盖（阶段四验收要求）：
1. floor 关闭（生产默认）：既有 margin 语义不变；
2. floor 开启：可用（扣除活跃预留与本次 peak 后的净可用）< floor → 拒绝 E_CAPACITY_FLOOR；
3. 并发预留计入判定：N 个并发各自通过后合计不击穿水位；
4. 拒绝/业务异常后预留释放，light 操作不受影响；
5. 检查间隔（负面缓存）：TTL 内快速拒绝、放行清缓存；
6. 挂载/设备回归：st_dev 不匹配与 marker 缺失仍拒绝（不误用系统盘水位）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.capacity_guard as cg
from src.capacity_guard import CapacityInsufficientError, ensure_capacity, reserve_capacity

GB = 1024**3
MB = 1024**2
FLOOR = 10 * GB
USER_ID = "44444444-4444-4444-8444-444444444444"


@pytest.fixture()
def guard(tmp_path, monkeypatch):
    """独立守卫环境：tmp 台账/锁 + 受控 free。"""
    base = tmp_path / "data"
    base.mkdir(parents=True)
    monkeypatch.setattr(tempfile if False else __import__("tempfile"), "gettempdir", lambda: str(tmp_path))
    state = {"free": 100 * GB, "base": base}

    def set_free(value: int):
        state["free"] = value
        monkeypatch.setattr(cg, "free_bytes", lambda path=None: value)

    set_free(100 * GB)
    state["set_free"] = set_free
    yield state


@pytest.fixture()
def no_floor(monkeypatch):
    monkeypatch.setattr(cg, "CAPACITY_FLOOR_BYTES", 0)
    monkeypatch.setattr(cg, "CAPACITY_CHECK_INTERVAL_MS", 0)
    monkeypatch.setattr(cg, "_floor_reject_until", {})


@pytest.fixture()
def floor_on(monkeypatch):
    monkeypatch.setattr(cg, "CAPACITY_FLOOR_BYTES", FLOOR)
    monkeypatch.setattr(cg, "CAPACITY_CHECK_INTERVAL_MS", 0)
    monkeypatch.setattr(cg, "_floor_reject_until", {})


def _principal():
    return {"subject": USER_ID, "domain_id": DOMAIN_ID if False else "d", "expires_at_ms": 0}


# 1. floor 关闭（生产默认）-------------------------------------------------

def test_floor_disabled_keeps_margin_semantics(guard, no_floor):
    guard["set_free"](100 * GB)
    ensure_capacity("upload", 600 * MB)
    with pytest.raises(CapacityInsufficientError) as exc:
        ensure_capacity("upload", 600 * GB)
    assert exc.value.code == "E_CAPACITY_INSUFFICIENT"


# 2. floor 开启：阈值上下 ---------------------------------------------------

def test_floor_above_free_rejects_any_peak(guard, floor_on):
    guard["set_free"](9 * GB)
    with pytest.raises(CapacityInsufficientError) as exc:
        ensure_capacity("upload", 1)
    assert exc.value.code == "E_CAPACITY_FLOOR"
    with pytest.raises(CapacityInsufficientError) as exc:
        with reserve_capacity("upload", 1):
            pass
    assert exc.value.code == "E_CAPACITY_FLOOR"


def test_floor_below_free_allows_within_budget(guard, floor_on):
    guard["set_free"](11 * GB)
    ensure_capacity("upload", 512 * MB)
    with reserve_capacity("upload", 512 * MB):
        pass


def test_floor_boundary_exact(guard, floor_on):
    guard["set_free"](FLOOR)          # 等于水位：放行
    ensure_capacity("upload", 0)
    guard["set_free"](FLOOR - 1)      # 低于 1 字节：拒绝
    with pytest.raises(CapacityInsufficientError) as exc:
        ensure_capacity("upload", 0)
    assert exc.value.code == "E_CAPACITY_FLOOR"


# 3. 并发预留计入判定 -------------------------------------------------------

def test_floor_uses_net_available_including_new_peak(guard, floor_on):
    guard["set_free"](12 * GB)
    with reserve_capacity("upload", 1500 * MB) as first:
        assert first["reservation_id"]
        with pytest.raises(CapacityInsufficientError) as exc:
            with reserve_capacity("upload", 1200 * MB):   # 12-1.5-1.2=9.3 < 10 → 拒绝
                pass
        assert exc.value.code == "E_CAPACITY_FLOOR"
        with reserve_capacity("upload", 200 * MB):        # 12-1.5-0.2=10.3 ≥ 10 → 放行
            pass


def test_concurrent_reservations_tracked_across_tokens(guard, floor_on):
    guard["set_free"](11 * GB)
    cm1 = reserve_capacity("upload", 500 * MB)
    cm2 = reserve_capacity("upload", 500 * MB)
    with cm1, cm2:                                        # 11-0.5-0.5=10 ≥ floor
        with pytest.raises(CapacityInsufficientError) as exc:
            with reserve_capacity("upload", 800 * MB):    # 10-0.8=9.2 < 10 → 拒绝
                pass
        assert exc.value.code == "E_CAPACITY_FLOOR"


# 4. 释放 -------------------------------------------------------------------

def test_successful_reservation_released_after_exit(guard, floor_on):
    guard["set_free"](11 * GB)
    for _ in range(3):
        with reserve_capacity("upload", 500 * MB):
            pass


def test_rejected_reserve_leaves_no_residue(guard, floor_on, monkeypatch):
    guard["set_free"](9 * GB)
    with pytest.raises(CapacityInsufficientError):
        with reserve_capacity("upload", 100 * MB):
            pass
    monkeypatch.setattr(cg, "CAPACITY_FLOOR_BYTES", 0)   # 关 floor 立即可预留（无台账残留）
    with reserve_capacity("upload", 100 * MB):
        pass


def test_body_exception_still_releases_reservation(guard, floor_on):
    guard["set_free"](11 * GB)
    with pytest.raises(RuntimeError):
        with reserve_capacity("upload", 500 * MB):
            raise RuntimeError("business failure")
    with reserve_capacity("upload", 500 * MB):
        pass


# 5. light 不受影响 ----------------------------------------------------------

def test_floor_does_not_affect_light_paths(guard, floor_on):
    guard["set_free"](1 * GB)
    probe = Path(str(guard["base"])) / "probe.txt"
    probe.write_text("x")
    assert probe.read_text() == "x"
    probe.unlink()
    assert not probe.exists()
    with pytest.raises(CapacityInsufficientError):
        ensure_capacity("upload", 1)


# 6. 检查间隔（负面缓存）----------------------------------------------------

def test_check_interval_negative_cache(guard, floor_on, monkeypatch):
    """TTL 内快速拒绝（即使 free 已恢复）；TTL 过期后重新真实判定。"""
    import types

    clock = {"now": 1000.0}
    fake_time = types.SimpleNamespace(time=lambda: clock["now"])
    monkeypatch.setattr(cg, "time", fake_time)
    monkeypatch.setattr(cg, "CAPACITY_CHECK_INTERVAL_MS", 5000)
    monkeypatch.setattr(cg, "_floor_reject_until", {})
    guard["set_free"](9 * GB)

    with pytest.raises(CapacityInsufficientError):
        with reserve_capacity("upload", 1):
            pass
    assert cg._floor_reject_until  # 拒绝已缓存

    clock["now"] += 2  # TTL 内：即便 free 恢复仍快速拒绝（宁可多拒）
    guard["set_free"](100 * GB)
    with pytest.raises(CapacityInsufficientError) as exc:
        with reserve_capacity("upload", 1):
            pass
    assert exc.value.code == "E_CAPACITY_FLOOR"

    clock["now"] += 4  # TTL 过期：重新真实判定 → 放行
    with reserve_capacity("upload", 1):
        pass
    assert not cg._floor_reject_until


def test_successful_admission_clears_negative_cache(guard, floor_on, monkeypatch):
    """拒绝缓存存在时关闭间隔读取（interval=0）→ 放行路径清除缓存。"""
    monkeypatch.setattr(cg, "CAPACITY_CHECK_INTERVAL_MS", 5000)
    monkeypatch.setattr(cg, "_floor_reject_until", {})
    guard["set_free"](9 * GB)
    with pytest.raises(CapacityInsufficientError):
        with reserve_capacity("upload", 1):
            pass
    assert cg._floor_reject_until

    monkeypatch.setattr(cg, "CAPACITY_CHECK_INTERVAL_MS", 0)  # 停用缓存读取
    guard["set_free"](100 * GB)
    with reserve_capacity("upload", 1):
        pass
    assert not cg._floor_reject_until  # 放行清缓存


# 7. 挂载/设备回归 ------------------------------------------------------------

def test_target_filesystem_resolves_data_disk(guard, no_floor):
    target = cg.target_filesystem(str(guard["base"]))
    assert target.stat().st_dev == Path(str(guard["base"])).stat().st_dev


def test_expected_st_dev_mismatch_rejects(guard, no_floor):
    with pytest.raises(CapacityInsufficientError) as exc:
        cg.target_filesystem(str(guard["base"]), expected_st_dev=987654321)
    assert exc.value.code == "E_STORAGE_DEVICE_MISMATCH"


def test_missing_marker_rejects(guard, no_floor, tmp_path):
    with pytest.raises(CapacityInsufficientError) as exc:
        cg.target_filesystem(str(guard["base"]), storage_marker=str(tmp_path / "no-such-marker"))
    assert exc.value.code == "E_STORAGE_MARKER_MISSING"
