"""源片「单副本化」物化层：同盘硬链接优先，跨设备才复制。

背景
----
一份数 GB 的源片在整条链路上会被多个组件「放到自己的目录里」：

    上传分片拼装 (studio_runtime)  →  studio-jobs/{job}/source.mp4
    studio_job_worker              →  studio-projects/timeline_*/raw/source.mp4
    studio_auto_edit_bridge        →  tasks/media/{task_id}/source.mp4
    timeline_copilot               →  {project}/raw/source.mp4

历史上每个调用点都各写一遍 ``shutil.copy2``，于是同一份 2.86GB 源片在磁盘上
留下了 8 份物理副本（约 20GB），把数据盘写到只剩不到 200MB。本模块把
「放一份源片」收敛成唯一实现：

* **同设备** → ``os.link`` 硬链接，零额外空间占用；
* **跨设备 / 文件系统不支持硬链接** → ``shutil.copy2`` 到 ``.part`` 再
  ``os.replace`` 原子改名，复制前后都校验目标盘剩余空间。

只读契约（必须遵守）
--------------------
**源片在整条链路上只读，任何组件都不得就地改写（in-place write）已物化的
源片文件。** 硬链接只是同一个 inode 的第二个目录项——对任一链接做原地写入
（``open(path, "r+b")``、``ffmpeg -i src -y src``、``truncate`` 等）会同时
改写所有链接指向的同一份数据，等于串改原片与其它任务正在读取的输入。

允许且唯一的写路径是「在本目录内新建**另一个**文件」（例如渲染产物、
``transcript.json``），再用 ``os.replace`` 覆盖目录项；即便这样也必须先
解除硬链接（写进新 inode），不能续写已有源片。

空间守卫
--------
硬链接不消耗空间，因此不做空间检查；只有真正要复制时才校验：
复制**前** ``free >= size + min_free_mb``，复制**后**再复核一次目标盘
（防止并发写入在复制期间挤爆磁盘）。不足时抛 :class:`SourceStoreError`，
其 ``code`` 为机器可读错误码（``E_DISK_SPACE``）。``SourceStoreError`` 继承
``ValueError``：既有的 Studio 路由层就是把 ``ValueError`` 映射成 400 并透出
可读原因，而不是 500。
"""

from __future__ import annotations

import errno
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "SourceStoreError",
    "DiskSpaceError",
    "materialise_source",
    "is_same_physical",
    "link_count",
    "physical_id",
    "file_size",
    "DEFAULT_MIN_FREE_MB",
    "CODE_DISK_SPACE",
    "CODE_SOURCE_MISSING",
    "CODE_COPY_FAILED",
]

# 复制时默认预留的安全余量（MiB）。硬链接路径不消耗额外空间，不参与此判断。
DEFAULT_MIN_FREE_MB = 512

# 机器可读错误码：路由层据此返回 4xx 而不是 500，让成员看到可读原因。
CODE_DISK_SPACE = "E_DISK_SPACE"
CODE_SOURCE_MISSING = "E_SOURCE_MISSING"
CODE_COPY_FAILED = "E_COPY_FAILED"


class SourceStoreError(ValueError):
    """源片物化失败；``code`` 是机器可读错误码，``detail`` 为人类可读说明。

    继承 ``ValueError`` 是刻意的：Studio 路由层既有的映射就是
    ``ValueError`` → 400（带可读 detail），``OSError`` → 500。
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class DiskSpaceError(SourceStoreError, OSError):
    """空间不足（含复制中途 ENOSPC）。

    同时是 ``OSError``：既有的复制失败用例与调用方按 ``OSError`` 捕获的
    语义保持兼容，同时带上 ``E_DISK_SPACE`` 机器码供路由层透出。
    """


@dataclass(frozen=True)
class physical_id:
    """一个物理数据块的稳定标识（设备号 + inode 号）。

    同一 inode 的多个硬链接共享同一个 ``physical_id``，因此这是判断
    「两份路径是不是同一份物理数据」的唯一正确依据——文件大小、mtime、
    内容抽样都不足以区分两个碰巧相同的独立副本。
    """

    dev: int
    ino: int

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"{self.dev}:{self.ino}"


def file_size(path: Path | str) -> int:
    """返回文件字节数；路径不存在时返回 0（GC 统计用，不抛异常）。"""

    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def link_count(path: Path | str) -> int:
    """返回该路径的硬链接数（``st_nlink``）。

    ``1`` 表示磁盘上只有这一个目录项指向这份数据——删掉就真的没了。
    GC 用它判断「删除是否会丢数据」。
    """

    return Path(path).stat().st_nlink


def is_same_physical(a: Path | str, b: Path | str) -> bool:
    """两个路径是否指向同一份物理数据（比较 ``st_dev`` + ``st_ino``）。

    任一路径不存在时返回 ``False``（而不是抛异常），便于 GC 在扫描过程中
    对已被删除的候选做安全判断。
    """

    try:
        stat_a = os.stat(a)
        stat_b = os.stat(b)
    except OSError:
        return False
    return (stat_a.st_dev, stat_a.st_ino) == (stat_b.st_dev, stat_b.st_ino)


def _free_bytes(path: Path) -> int:
    """返回 ``path`` 所在挂载点的可用字节数。"""

    return shutil.disk_usage(path).free


def _require_space(destination: Path, needed: int, min_free_mb: int, *, stage: str) -> None:
    """校验目标盘空间；不足则抛 ``E_DISK_SPACE``。"""

    margin = max(0, int(min_free_mb)) * 1024 * 1024
    free = _free_bytes(destination)
    if free < needed + margin:
        raise SourceStoreError(
            CODE_DISK_SPACE,
            f"{stage}磁盘剩余 {free // (1024 * 1024)}MB，不足以安全复制源视频"
            f"（需 {needed // (1024 * 1024)}MB + {min_free_mb}MB 余量），请先清理磁盘",
        )


def materialise_source(
    canonical: Path,
    dest: Path,
    *,
    min_free_mb: int = DEFAULT_MIN_FREE_MB,
) -> Path:
    """把源片 ``canonical`` 物化到 ``dest``：优先硬链接，必要时安全复制。

    参数
    ----
    canonical:
        源片路径。**按只读契约，本函数永不改写它。**
    dest:
        目标路径。父目录会自动创建。
    min_free_mb:
        仅在实际需要复制时校验的额外余量（MiB）。

    返回
    ----
    ``dest``（便于链式调用）。

    行为
    ----
    * ``dest`` 已存在：直接返回，**不覆盖**（重复物化幂等，也避免把正在被
      下游读取的文件换掉）。
    * 同设备：``os.link`` 成功即返回，零占用。
    * 跨设备（``EXDEV``）或文件系统不支持硬链接（``EPERM`` / ``EOPNOTSUPP``
      / ``EMLINK`` 等）：回退 ``shutil.copy2`` 到同目录 ``<name>.<uuid>.part``
      再 ``os.replace`` 原子改名；任何失败都清理临时文件，**绝不留下一个
      看似有效的截断 source.mp4**。

    异常
    ----
    :class:`SourceStoreError` / :class:`DiskSpaceError`

        * ``E_SOURCE_MISSING`` —— 源片不存在；
        * ``E_DISK_SPACE`` —— 复制前后空间守卫不通过，或复制中途 ENOSPC；
        * 其它 ``OSError`` 原样抛出（保持既有失败语义不变）。
    """

    canonical, dest = Path(canonical), Path(dest)

    if not canonical.is_file():
        raise SourceStoreError(CODE_SOURCE_MISSING, f"源片不存在: {canonical}")

    # 幂等：目标已就位就不再动它（重复物化不应把下游正在读的文件换掉）。
    if dest.exists():
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)

    # 已经是同一份物理数据（例如 canonical 本身就在 dest 上）→ 无需任何操作。
    if is_same_physical(canonical, dest):
        return dest

    try:
        os.link(canonical, dest)
        return dest
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            # 并发竞争：另一个进程刚刚建好了目标，视为成功。
            return dest
        # EXDEV（跨设备）/ EPERM / EOPNOTSUPP / EMLINK 等：退回到受保护的复制。
        # 其它 errno 也一并回退——复制即便同样失败，报出的原因也更具体。

    needed = canonical.stat().st_size
    _require_space(dest.parent, needed, min_free_mb, stage="复制前")

    # 先写临时文件再原子改名：复制被中断也不会留下看似有效的截断 source.mp4。
    temporary = dest.with_name(f"{dest.name}.{uuid.uuid4().hex}.part")
    try:
        shutil.copy2(canonical, temporary)
        # 复制后再复核一次：并发写入可能在复制期间挤占了磁盘。
        _require_space(dest.parent, 0, min_free_mb, stage="复制后")
        os.replace(temporary, dest)
    except DiskSpaceError:
        temporary.unlink(missing_ok=True)
        raise
    except SourceStoreError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        if exc.errno == errno.ENOSPC:
            # 保留原始 errno 文案（既有调用方/用例按 OSError 读它），同时附上机器码。
            raise DiskSpaceError(
                CODE_DISK_SPACE, f"复制源视频失败: {exc}"
            ) from exc
        # 其它 OSError 原样透出，保持既有失败语义不变。
        raise
    except BaseException:
        # KeyboardInterrupt / 子进程被杀等：同样不能留下半成品。
        temporary.unlink(missing_ok=True)
        raise

    return dest
