"""``src.source_store`` 的单元测试。

覆盖点：
* 同盘物化必须产生硬链接（同 inode、链接数 >= 2，零额外占用）；
* 只读契约：下游必须用「新文件 + 原子改名」发布，不得就地改写源片
  （就地写会顺着硬链接串改原片，这是本模块存在的根本前提）；
* 跨设备（EXDEV）回退到 ``.part`` + ``os.replace`` 的原子复制；
* 空间守卫必须在复制前就拒绝，并带上 ``E_DISK_SPACE`` 机器码；
* 失败路径绝不留下看似有效的截断目标文件。
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from src import source_store
from src.source_store import (
    CODE_DISK_SPACE,
    CODE_SOURCE_MISSING,
    DiskSpaceError,
    SourceStoreError,
    is_same_physical,
    link_count,
    materialise_source,
    physical_id,
)


def _write_source(path: Path, payload: bytes = b"medical-source-video" * 64) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


# ---------------------------------------------------------------------------
# 1. 同盘硬链接：这是「一份物理数据」的全部依据
# ---------------------------------------------------------------------------

def test_materialise_source_hardlinks_on_same_device(tmp_path):
    """同盘物化必须走 os.link：目标与源同 inode，且链接数 >= 2。"""
    source = _write_source(tmp_path / "canonical.mp4")
    destination = tmp_path / "job" / "source.mp4"

    returned = materialise_source(source, destination)

    assert returned == destination
    assert destination.is_file()
    assert destination.stat().st_ino == source.stat().st_ino, "同盘物化应产生硬链接"
    assert link_count(source) >= 2
    assert link_count(destination) >= 2
    assert is_same_physical(source, destination)
    # 零额外占用：同一 inode 的第二、第三个目录项不改变磁盘占用
    assert destination.stat().st_blocks == source.stat().st_blocks


def test_materialise_source_is_idempotent_and_keeps_existing_destination(tmp_path):
    """重复物化是幂等的，且不会把下游正在读的目标文件换掉。"""
    source = _write_source(tmp_path / "canonical.mp4")
    destination = tmp_path / "media" / "source.mp4"
    materialise_source(source, destination)
    first_ino = destination.stat().st_ino

    materialise_source(source, destination)

    assert destination.stat().st_ino == first_ino


def test_materialise_source_skips_when_already_same_physical(tmp_path):
    """源与目标已经是同一份物理数据时不做任何多余动作。"""
    source = _write_source(tmp_path / "canonical.mp4")
    assert materialise_source(source, source) == source
    assert link_count(source) == 1


# ---------------------------------------------------------------------------
# 2. 只读契约
# ---------------------------------------------------------------------------

def test_readonly_contract_compliant_publish_does_not_touch_source(tmp_path):
    """合规的发布方式（新文件 + os.replace）不会串改源片。

    硬链接意味着 ``source`` 与 ``destination`` 是同一份数据。任何组件要更新
    ``destination``，只能另建文件再原子改名（写进新 inode）；如果就地改写，
    就会连带改掉原片——这正是契约禁止的行为。
    """
    payload = b"original-source-bytes" * 128
    source = _write_source(tmp_path / "canonical.mp4", payload)
    destination = tmp_path / "media" / "source.mp4"
    materialise_source(source, destination)
    assert is_same_physical(source, destination)

    # 合规做法：先写临时文件，再 os.replace 覆盖目录项（不续写已有 inode）。
    staged = destination.with_name("source.new.mp4")
    staged.write_bytes(b"derived-artifact")
    os.replace(staged, destination)

    assert source.read_bytes() == payload, "源片必须逐字节保持不变"
    assert not is_same_physical(source, destination), "发布后目标应是新 inode"
    assert link_count(source) == 1
    assert destination.read_bytes() == b"derived-artifact"


def test_readonly_contract_inplace_write_would_corrupt_source(tmp_path):
    """反证：就地改写目标会顺着硬链接改掉源片，所以契约必须被遵守。

    这个用例固化「为什么不能就地写」这一事实，避免后人把它当成可优化项。
    """
    payload = b"payload-before" * 64
    source = _write_source(tmp_path / "canonical.mp4", payload)
    destination = tmp_path / "media" / "source.mp4"
    materialise_source(source, destination)

    with destination.open("r+b") as handle:  # 被契约禁止的写法
        handle.seek(0)
        handle.write(b"X" * 8)
        handle.truncate(8)

    assert source.read_bytes() != payload, "就地写必然串改源片（因此被禁止）"
    assert source.read_bytes() == b"X" * 8


# ---------------------------------------------------------------------------
# 3. 跨设备回退：受保护的复制
# ---------------------------------------------------------------------------

def test_materialise_source_falls_back_to_atomic_copy_on_exdev(tmp_path, monkeypatch):
    """os.link 抛 EXDEV（跨设备）时回退复制，并通过 .part + 原子改名落地。"""
    payload = b"cross-device-payload" * 96
    source = _write_source(tmp_path / "canonical.mp4", payload)
    destination = tmp_path / "other-device" / "source.mp4"

    def _exdev(*_args, **_kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(source_store.os, "link", _exdev)

    # 测试机的 /tmp 只有约 100MB，显式关闭 512MB 余量以聚焦回退分支本身。
    materialise_source(source, destination, min_free_mb=0)

    assert destination.read_bytes() == payload
    assert not is_same_physical(source, destination), "复制不应共享 inode"
    assert link_count(source) == 1
    # 临时文件必须已被原子改名消费掉
    leftovers = [p.name for p in destination.parent.iterdir() if p.name.endswith(".part")]
    assert leftovers == []


def test_materialise_source_copy_is_removed_when_interrupted(tmp_path, monkeypatch):
    """复制中途失败只清理 .part，绝不留下看似有效的截断目标文件。"""
    source = _write_source(tmp_path / "canonical.mp4")
    destination = tmp_path / "media" / "source.mp4"

    monkeypatch.setattr(
        source_store.os, "link",
        lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")),
    )

    def _boom(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(source_store.shutil, "copy2", _boom)

    with pytest.raises(OSError, match="No space left"):
        materialise_source(source, destination, min_free_mb=0)

    assert not destination.exists()
    assert list(destination.parent.glob("*.part")) == []


# ---------------------------------------------------------------------------
# 4. 空间守卫：复制前就要拒绝，并带上机器码
# ---------------------------------------------------------------------------

def test_materialise_source_space_guard_raises_e_disk_space(tmp_path, monkeypatch):
    """空间不足时抛带 E_DISK_SPACE 的 SourceStoreError，且不产生任何文件。"""
    source = _write_source(tmp_path / "canonical.mp4", b"x" * 4096)
    destination = tmp_path / "media" / "source.mp4"

    monkeypatch.setattr(
        source_store.os, "link",
        lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")),
    )

    class _Usage:
        free = 1024 * 1024  # 1MB，远小于 4096B + 512MB 余量

    monkeypatch.setattr(source_store.shutil, "disk_usage", lambda *_a, **_k: _Usage())

    with pytest.raises(SourceStoreError) as excinfo:
        materialise_source(source, destination)

    assert excinfo.value.code == CODE_DISK_SPACE
    # 路由层按 ValueError → 400 透出可读原因，因此必须同时是 ValueError
    assert isinstance(excinfo.value, ValueError)
    assert "不足以安全复制源视频" in str(excinfo.value)
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


def test_materialise_source_post_copy_space_guard_cleans_up(tmp_path, monkeypatch):
    """复制完成后复核发现空间被挤爆：回滚临时文件，不留半成品。"""
    source = _write_source(tmp_path / "canonical.mp4", b"y" * 2048)
    destination = tmp_path / "media" / "source.mp4"

    monkeypatch.setattr(
        source_store.os, "link",
        lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")),
    )

    calls = {"count": 0}

    class _Usage:
        def __init__(self, free: int) -> None:
            self.free = free

    def _usage(*_args, **_kwargs):
        calls["count"] += 1
        # 第一次（复制前）充足，第二次（复制后）不足
        return _Usage(1 << 30) if calls["count"] == 1 else _Usage(1024)

    monkeypatch.setattr(source_store.shutil, "disk_usage", _usage)

    with pytest.raises(SourceStoreError) as excinfo:
        materialise_source(source, destination)

    assert excinfo.value.code == CODE_DISK_SPACE
    assert not destination.exists()
    assert list(destination.parent.glob("*.part")) == []


def test_disk_space_error_is_both_valueerror_and_oserror():
    """DiskSpaceError 双继承：既走 ValueError→400 的路由映射，又兼容 OSError 捕获。"""
    error = DiskSpaceError(CODE_DISK_SPACE, "复制源视频失败: [Errno 28] No space left on device")
    assert isinstance(error, ValueError)
    assert isinstance(error, OSError)
    assert error.code == CODE_DISK_SPACE


def test_materialise_source_missing_source_is_coded_error(tmp_path):
    """源片不存在时给机器码，而不是让底层 stat 甩出裸 FileNotFoundError。"""
    with pytest.raises(SourceStoreError) as excinfo:
        materialise_source(tmp_path / "missing.mp4", tmp_path / "out" / "source.mp4")
    assert excinfo.value.code == CODE_SOURCE_MISSING


# ---------------------------------------------------------------------------
# 5. 小工具
# ---------------------------------------------------------------------------

def test_is_same_physical_and_link_count(tmp_path):
    source = _write_source(tmp_path / "canonical.mp4")
    destination = tmp_path / "media" / "source.mp4"
    materialise_source(source, destination)

    assert is_same_physical(source, destination)
    assert is_same_physical(str(source), str(destination)), "应同时接受 str 路径"
    assert not is_same_physical(source, tmp_path / "nope.mp4")
    assert link_count(source) == 2

    stat = source.stat()
    assert physical_id(stat.st_dev, stat.st_ino) == physical_id(stat.st_dev, stat.st_ino)
    assert str(physical_id(stat.st_dev, stat.st_ino)) == f"{stat.st_dev}:{stat.st_ino}"
