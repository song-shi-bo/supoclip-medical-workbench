"""陈旧源片副本回收（GC）：默认 dry-run，``--apply`` 才真正删除。

为什么需要
----------
``source_store`` 让**新**的物化走硬链接，但历史上各调用点已经用
``shutil.copy2`` 留下了大量物理重复：同一份 2.86GB 源片在磁盘上曾有 8 份
副本（约 20GB）。本模块负责把这些历史副本识别出来并回收。

只删「不会丢数据」的东西
------------------------
每条删除判定都必须同时满足下面三组条件，代码里逐条对应到
:func:`decide` 的注释：

**绝对禁止（无论其它条件如何）**

* ``UPLOAD_PROTECTED`` —— ``studio-uploads/`` 是用户的原始上传源，只报告不删。
* ``IN_USE`` —— 该路径属于 ``tasks/in/``、``tasks/processing/`` 中的任务，
  或属于被非终态任务引用的项目。正在被使用的一律不碰。
* ``TOO_FRESH`` —— 修改时间晚于 ``min_age``。物化与写任务 JSON 之间存在一个
  毫秒级窗口，这个 guard 保证不会误删另一个进程刚建好、还没登记引用的目录。
* ``REFERENCED`` —— 仍被任何任务 JSON（``raw_video_path``）或任何项目
  （``bridge.project_id``）引用。**inode 链接数为 1 且仍被引用的文件绝不删除**
  —— 那是这份数据的最后一个副本。

**删除依据（整组按 inode 判定，见 :func:`resolve_group_policy`）**

删单个硬链接不释放任何空间，却会让该目录项的下游读者失效，所以删除是**按物理
副本（inode）整组**决定的：

* ``MULTI_LINK`` —— 某个目录项无引用，且同一 inode 还有别的目录项。这只是
  「这个目录项可以删」的资格；真正回收空间必须整组删除。
* ``ORPHAN_DUPLICATE`` —— 该路径没有任何引用，且磁盘上存在一个**将保留**的
  物理副本，其内容指纹（大小 + 头/中/尾抽样 sha256）与它完全一致。
* 整组删除还要求组内**没有任何目录项被保护**，并且磁盘上另有一个将保留的
  同内容副本。``st_nlink >= 2`` 只说明「删一个链接不丢数据」，删光整组依然
  会丢数据，因此必须靠内容孪生来证明「不属于唯一副本」。

**判定不出来就保留**

* ``ORPHAN_UNVERIFIED`` / ``GROUP_LAST_COPY`` —— 无引用、``st_nlink == 1``
  （或整组删光就是最后一个副本），又找不到内容一致的保留副本。此时无法证明
  它是重复的，按「拿不准就不删」保留，并在报告里列出。
* ``GROUP_SHARED_LINK`` —— 同一 inode 还有被保护的目录项。删除它不释放空间
  却会让下游引用失效，因此保留。

分片（``studio-chunks``）是另一套规则：只删整目录修改时间早于保留窗口（默认
24 小时）且 ``.assembly.lock`` 未被任何进程持有的目录。

用法
----
    cd backend
    .venv/bin/python -m src.source_gc                 # dry-run，只报告
    .venv/bin/python -m src.source_gc --apply         # 真正删除
    .venv/bin/python -m src.source_gc --min-age-minutes 60
    .venv/bin/python -m src.source_gc --json plan.json
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:  # 支持 `python backend/src/source_gc.py`
    sys.path.insert(0, str(BASE_DIR))

# 与 studio_job_worker 一致：先读 backend/.env，再解析 TEMP_DIR / STUDIO_TASKS_ROOT，
# 否则手工执行时可能扫到错误的根目录。
try:  # pragma: no cover - 环境相关
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:  # pragma: no cover
    pass

# 源片候选的扫描范围（相对各自的根）。这些是链路里出现过的所有落盘位置。
JOB_SOURCE_GLOB = "studio-jobs/*/source.mp4"
JOB_PROJECT_GLOB = "studio-jobs/*/studio-projects/*/raw/*"
PROJECT_RAW_GLOB = "studio-projects/*/raw/*"
TASK_MEDIA_GLOB = "media/*/*"
UPLOADS_DIR = "studio-uploads"
CHUNKS_DIR = "studio-chunks"

# 内容指纹的抽样窗口：头 / 中 / 尾各取这么多字节。3GB 文件也只读 24MB。
FINGERPRINT_SAMPLE_BYTES = 8 * 1024 * 1024

# 默认 guard
DEFAULT_MIN_AGE_MINUTES = 30
DEFAULT_CHUNK_RETENTION_HOURS = 24

# --- 路径类型 -----------------------------------------------------------------
KIND_UPLOAD = "upload"
KIND_JOB_SOURCE = "job-source"
KIND_PROJECT = "project"
KIND_JOB_PROJECT = "job-project"
KIND_TASK_MEDIA = "task-media"
KIND_CHUNK = "chunk"

# --- 动作与判定码 -------------------------------------------------------------
ACTION_DELETE = "delete"
ACTION_KEEP = "keep"

CODE_UPLOAD_PROTECTED = "UPLOAD_PROTECTED"
CODE_IN_USE = "IN_USE"
CODE_TOO_FRESH = "TOO_FRESH"
CODE_REFERENCED = "REFERENCED"
CODE_MULTI_LINK = "MULTI_LINK"
CODE_ORPHAN_DUPLICATE = "ORPHAN_DUPLICATE"
CODE_ORPHAN_UNVERIFIED = "ORPHAN_UNVERIFIED"
CODE_GROUP_SHARED_LINK = "GROUP_SHARED_LINK"
CODE_GROUP_LAST_COPY = "GROUP_LAST_COPY"
CODE_CHUNK_STALE = "CHUNK_STALE"
CODE_CHUNK_LOCKED = "CHUNK_LOCKED"
CODE_CHUNK_FRESH = "CHUNK_FRESH"
CODE_CHUNK_EMPTY = "CHUNK_EMPTY"


# ---------------------------------------------------------------------------
# 纯决策所需的输入
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceFact:
    """一个候选文件在**决策那一刻**的事实快照（全部可在测试里手工构造）。"""

    path: str
    kind: str
    size: int
    nlink: int
    dev: int
    ino: int
    mtime: float
    ctime: float = 0.0
    fingerprint: str | None = None
    task_id: str | None = None
    project_id: str | None = None

    @property
    def age_base(self) -> float:
        """判定「新鲜度」用的时间基准，取 mtime 与 ctime 的较大者。

        ``copy2`` 会保留源文件的 mtime，所以刚复制出来的文件 mtime 可能很旧；
        只有 ctime（inode 变更时间）能真实反映「这个目录项是刚建的」。取较大者
        只会让判定更保守。
        """

        return max(self.mtime, self.ctime)


@dataclass(frozen=True)
class ChunkFact:
    """一个 ``studio-chunks/<upload_id>`` 目录的事实快照。"""

    path: str
    size: int
    mtime: float
    file_count: int
    has_receipt: bool
    locked: bool
    ctime: float = 0.0

    @property
    def age_base(self) -> float:
        return max(self.mtime, self.ctime)


@dataclass(frozen=True)
class GcContext:
    """决策所需的引用关系与阈值；纯数据，构造它不需要磁盘。"""

    now: float
    min_age_seconds: float
    # 被任务 JSON 的 raw_video_path 解析出的绝对路径
    referenced_paths: frozenset[str] = frozenset()
    # 被任务 JSON 的 bridge.project_id 引用的项目 id
    referenced_projects: frozenset[str] = frozenset()
    # in/ 、processing/ 或非终态 status 里的任务 id
    active_task_ids: frozenset[str] = frozenset()
    # 被 active 任务引用的项目 id
    active_project_ids: frozenset[str] = frozenset()
    # 「将保留」文件的指纹集合（第二阶段才填好）
    retained_fingerprints: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Decision:
    """一条判定结果。``reclaim_bytes`` 是**该 inode 分组**的净回收量。"""

    path: str
    kind: str
    action: str
    code: str
    reason: str
    size: int
    nlink: int
    ino: int
    reclaim_bytes: int = 0
    fingerprint: str | None = None


# ---------------------------------------------------------------------------
# 纯决策函数（不触碰磁盘，全部可单测）
# ---------------------------------------------------------------------------

def _keep(fact: SourceFact, code: str, reason: str) -> Decision:
    return Decision(
        path=fact.path, kind=fact.kind, action=ACTION_KEEP, code=code,
        reason=reason, size=fact.size, nlink=fact.nlink, ino=fact.ino,
        fingerprint=fact.fingerprint,
    )


def _delete(fact: SourceFact, code: str, reason: str) -> Decision:
    return Decision(
        path=fact.path, kind=fact.kind, action=ACTION_DELETE, code=code,
        reason=reason, size=fact.size, nlink=fact.nlink, ino=fact.ino,
        reclaim_bytes=fact.size, fingerprint=fact.fingerprint,
    )


def decide(fact: SourceFact, ctx: GcContext) -> Decision:
    """对单个候选文件做纯判定。规则顺序即优先级，逐条见模块 docstring。"""

    # (0) 用户上传源：只报告，永不自动删除。
    if fact.kind == KIND_UPLOAD:
        return _keep(fact, CODE_UPLOAD_PROTECTED, "用户上传源，按约定不自动删除")

    # (1) 正在被任务使用：in/ 、processing/ 或非终态 status 里点名的任务。
    if fact.task_id and fact.task_id in ctx.active_task_ids:
        return _keep(fact, CODE_IN_USE, f"任务 {fact.task_id} 处于 in/ 或 processing/，正在使用")
    if fact.project_id and fact.project_id in ctx.active_project_ids:
        return _keep(fact, CODE_IN_USE, f"项目 {fact.project_id} 正被未完成任务引用")

    # (2) 太新：物化完成但任务 JSON 还没写出来的窗口期，绝不参与回收。
    #     基准取 max(mtime, ctime)：copy2 会继承源的旧 mtime，只看 mtime 会把
    #     「几分钟前刚复制出来的文件」误判成「几小时没动过」。
    age_seconds = ctx.now - fact.age_base
    if age_seconds < ctx.min_age_seconds:
        return _keep(fact, CODE_TOO_FRESH, f"仅 {max(0.0, age_seconds) / 60.0:.1f} 分钟未变动，未过保护期")

    # (3) 仍被引用：任务 raw_video_path 直接指向它，或任务用 bridge.project_id
    #     点名的项目。**链接数为 1 且仍被引用 = 最后一个副本，绝不删除。**
    if fact.path in ctx.referenced_paths:
        return _keep(fact, CODE_REFERENCED, "被任务 raw_video_path 引用")
    if fact.project_id and fact.project_id in ctx.referenced_projects:
        return _keep(fact, CODE_REFERENCED, f"被任务 bridge.project_id={fact.project_id} 引用")

    # ---- 以下都是「无任何引用」的候选，才允许进入删除分支 ----

    # (4) 同一 inode 还有别的目录项：删掉这个目录项不会丢数据。
    if fact.nlink >= 2:
        return _delete(
            fact, CODE_MULTI_LINK,
            f"无引用；同一 inode 另有 {fact.nlink - 1} 个硬链接，删除本目录项不丢数据",
        )

    # (5) 链接数为 1：必须有内容完全一致的「保留」副本做孪生证明，否则不删。
    if fact.fingerprint and fact.fingerprint in ctx.retained_fingerprints:
        return _delete(
            fact, CODE_ORPHAN_DUPLICATE,
            "无引用；磁盘上存在内容一致的保留副本，删掉不丢数据",
        )
    return _keep(
        fact, CODE_ORPHAN_UNVERIFIED,
        "无引用但找不到内容一致的保留副本，无法判定，保守保留",
    )


def decide_chunk(fact: ChunkFact, *, now: float, retention_seconds: float) -> Decision:
    """对一个分片目录做纯判定：只删「陈旧 + 未被持有」的整目录。"""

    def _chunk_decision(action: str, code: str, reason: str, reclaim: int = 0) -> Decision:
        return Decision(
            path=fact.path, kind=KIND_CHUNK, action=action, code=code, reason=reason,
            size=fact.size, nlink=1, ino=0, reclaim_bytes=reclaim,
        )

    if fact.file_count == 0:
        return _chunk_decision(ACTION_DELETE, CODE_CHUNK_EMPTY, "空目录", fact.size)
    # 正在拼装的分片由 .assembly.lock 的 flock 标识，绝不删除。
    if fact.locked:
        return _chunk_decision(ACTION_KEEP, CODE_CHUNK_LOCKED, "分片正在被拼装进程持有")
    if now - fact.age_base < retention_seconds:
        age_h = max(0.0, (now - fact.age_base) / 3600.0)
        return _chunk_decision(
            ACTION_KEEP, CODE_CHUNK_FRESH, f"仅 {age_h:.1f} 小时未变动，在保留窗口内"
        )
    detail = "含 receipt，拼装已完成" if fact.has_receipt else "无 receipt，拼装未完成"
    return _chunk_decision(
        ACTION_DELETE, CODE_CHUNK_STALE, f"{detail}且已超过保留窗口", fact.size
    )


def resolve_group_policy(
    decisions: list[Decision], *, retained_fingerprints: frozenset[str]
) -> list[Decision]:
    """把逐文件的「删除资格」收敛成按**物理副本**（inode）整组回收的最终计划。

    为什么必须按组判定：删除单个硬链接**不释放任何空间**，却会让该目录项的
    下游读取者（video-brain、时间线工程、Studio 素材接口）失效。所以：

    1. 一个 inode 只有在「它没有任何目录项被保护」时才允许整组删除。被保护 =
       用户上传源 / 正在被任务使用 / 仍被任务或项目引用 / 太新 / 无法证明重复。
    2. 整组删除前还必须证明**磁盘上另有一个将保留的物理副本内容与它完全一致**
       （``retained_fingerprints``）。这正是「不属于唯一副本」的机器化表达：
       ``st_nlink >= 2`` 只说明「删一个链接不丢数据」，删光整组仍会丢数据。
    3. 组内还有受保护目录项时，只允许对 ``tasks/media/autoedit_*`` 下的**空壳
       目录**做零回收的卫生清理——那些目录没有任何任务 JSON 引用，删掉链接
       不丢数据（同 inode 的其它链接仍在），也不影响用户可见的时间线工程。
       其余目录项（job source、时间线工程 raw）降级为保留，因为删除它们
       只是破坏引用，一个字节也换不回来。
    """

    resolved = list(decisions)
    groups: dict[int, list[int]] = {}
    for index, decision in enumerate(resolved):
        if decision.kind == KIND_CHUNK:
            continue
        groups.setdefault(decision.ino, []).append(index)

    for indices in groups.values():
        protected = [i for i in indices if resolved[i].action == ACTION_KEEP]
        if not protected:
            fingerprint = resolved[indices[0]].fingerprint
            if fingerprint and fingerprint in retained_fingerprints:
                # 整组都是重复副本，且别处仍有同内容副本 → 全删，真正释放容量。
                # 只有「扫描到的链接数 == st_nlink」时才确实回收，否则还有链接
                # 在扫描范围之外，数据仍在。回收量只记在组内一条上，避免把同一块
                # 物理数据按链接数重复计入总量。
                complete = len(indices) == resolved[indices[0]].nlink
                for order, i in enumerate(indices):
                    resolved[i] = replace(
                        resolved[i],
                        reclaim_bytes=resolved[i].size if (complete and order == 0) else 0,
                    )
            else:
                # 删光整组会丢掉这份数据的最后一个副本 → 全部保留。
                for i in indices:
                    resolved[i] = replace(
                        resolved[i], action=ACTION_KEEP, code=CODE_GROUP_LAST_COPY,
                        reclaim_bytes=0,
                        reason="删光该 inode 的全部链接将丢掉最后一个副本，且找不到内容一致的保留副本",
                    )
            continue

        for i in indices:
            decision = resolved[i]
            if decision.action != ACTION_DELETE:
                continue
            if decision.kind == KIND_TASK_MEDIA:
                # 空壳任务目录：纯卫生清理，回收 0 字节（数据由同 inode 的其它链接持有）。
                resolved[i] = replace(decision, reclaim_bytes=0)
            else:
                resolved[i] = replace(
                    decision, action=ACTION_KEEP, code=CODE_GROUP_SHARED_LINK,
                    reclaim_bytes=0,
                    reason=(
                        f"同一 inode 另有 {decision.nlink - 1} 个目录项被保留，"
                        "删除本目录项既不释放空间又会让下游引用失效"
                    ),
                )
    return resolved


# ---------------------------------------------------------------------------
# 扫描（IO）
# ---------------------------------------------------------------------------

def _scope_dir(path: Path, kind: str) -> Path | None:
    """返回「该源片的活跃范围目录」：这个目录里的任何写入都说明它正被使用。

    只盯着源片自身的 mtime/ctime 是不够的：一个时间线工程可能在几分钟前刚被
    改过 EDL/SRT/FCPXML（产物是**就地**写进 ``edit/`` 的，不会更新源片也不一定
    更新父目录 mtime），随后立刻被用来发起自动剪辑——此时删掉它的 ``raw/`` 源片
    就会打断别人正在做的事。因此把「同一工程/任务目录下的最新写入时间」也纳入
    新鲜度判定。
    """

    if kind == KIND_JOB_SOURCE:
        return path.parent                     # studio-jobs/<job>/
    if kind == KIND_JOB_PROJECT:
        return path.parent.parent.parent       # studio-jobs/<job>/studio-projects/<pid>/
    if kind == KIND_PROJECT:
        return path.parent.parent              # studio-projects/<pid>/
    if kind == KIND_TASK_MEDIA:
        return path.parent                     # tasks/media/<task_id>/
    return None


def _newest_activity(directory: Path | None) -> float:
    """递归取目录内所有文件的 max(mtime, ctime)；目录不存在时返回 0。"""

    if directory is None or not directory.is_dir():
        return 0.0
    newest = 0.0
    try:
        for item in directory.rglob("*"):
            try:
                stat = item.stat()
            except OSError:
                continue
            newest = max(newest, stat.st_mtime, stat.st_ctime)
    except OSError:
        return newest
    return newest


def fingerprint(path: Path) -> str:
    """大小 + 头/中/尾抽样 sha256；3GB 文件也只读 24MB。

    用途只是「这两份文件是不是同一段内容」的快速证明，不做逐字节校验。
    """

    size = path.stat().st_size
    digest = hashlib.sha256(str(size).encode())
    sample = FINGERPRINT_SAMPLE_BYTES
    offsets = [0]
    if size > sample:
        offsets.append(max(0, size // 2 - sample // 2))
        offsets.append(max(0, size - sample))
    with path.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            digest.update(handle.read(sample))
    return digest.hexdigest()[:16]


def _classify(path: Path, temp_dir: Path, tasks_root: Path) -> tuple[str, str | None, str | None]:
    """从路径推断 (kind, task_id, project_id)。"""

    parts = path.parts
    if temp_dir in path.parents:
        relative = path.relative_to(temp_dir)
        if relative.parts[0] == UPLOADS_DIR:
            return KIND_UPLOAD, None, None
        if relative.parts[0] == "studio-jobs":
            # studio-jobs/<job>/source.mp4 或 studio-jobs/<job>/studio-projects/<pid>/raw/x
            if len(relative.parts) >= 4 and relative.parts[2] == "studio-projects":
                return KIND_JOB_PROJECT, None, relative.parts[3]
            return KIND_JOB_SOURCE, None, None
        if relative.parts[0] == "studio-projects":
            return KIND_PROJECT, None, relative.parts[1]
    if tasks_root in path.parents:
        relative = path.relative_to(tasks_root)
        if relative.parts[0] == "media":
            return KIND_TASK_MEDIA, relative.parts[1], None
    raise ValueError(f"无法归类路径: {path}")


def scan_sources(temp_dir: Path, tasks_root: Path, *, with_fingerprint: bool) -> list[SourceFact]:
    """扫描全部源片候选位置，返回事实快照。"""

    patterns: list[tuple[Path, str]] = [
        (temp_dir, JOB_SOURCE_GLOB),
        (temp_dir, JOB_PROJECT_GLOB),
        (temp_dir, PROJECT_RAW_GLOB),
        (temp_dir, f"{UPLOADS_DIR}/*.mp4"),
        (tasks_root, TASK_MEDIA_GLOB),
    ]
    facts: list[SourceFact] = []
    seen: set[str] = set()
    for root, pattern in patterns:
        if not root.exists():
            continue
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            try:
                kind, task_id, project_id = _classify(path, temp_dir, tasks_root)
            except ValueError:
                continue
            stat = path.stat()
            # ctime 反映「这个目录项是什么时候建的」（copy2 会继承源文件的旧
            # mtime，只看 mtime 会把刚复制出来的文件误判成陈旧）；此后再叠加
            # 「同工程/同任务目录里最近的写入时间」，避免删掉别人正在用的工程源片。
            activity = _newest_activity(_scope_dir(path, kind))
            facts.append(SourceFact(
                path=key, kind=kind, size=stat.st_size, nlink=stat.st_nlink,
                dev=stat.st_dev, ino=stat.st_ino, mtime=stat.st_mtime,
                ctime=max(stat.st_ctime, activity),
                fingerprint=fingerprint(path) if with_fingerprint else None,
                task_id=task_id, project_id=project_id,
            ))
    return facts


def scan_chunks(temp_dir: Path) -> list[ChunkFact]:
    """扫描 ``studio-chunks``；顺便用非阻塞 flock 探测目录是否正被拼装。"""

    root = temp_dir / CHUNKS_DIR
    if not root.exists():
        return []
    facts: list[ChunkFact] = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        size = 0
        dir_stat = directory.stat()
        newest = dir_stat.st_mtime
        count = 0
        for item in directory.rglob("*"):
            if item.is_file():
                stat = item.stat()
                size += stat.st_size
                count += 1
                newest = max(newest, stat.st_mtime, stat.st_ctime)
        facts.append(ChunkFact(
            path=str(directory), size=size, mtime=newest, file_count=count,
            has_receipt=(directory / "receipt.json").is_file(),
            locked=_lock_is_held(directory / ".assembly.lock"),
            ctime=dir_stat.st_ctime,
        ))
    return facts


def _lock_is_held(lock_path: Path) -> bool:
    """非阻塞尝试取锁：取不到说明有进程正持有它（拼装进行中）。"""

    if not lock_path.exists():
        return False
    try:
        with lock_path.open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
    except OSError:
        # 打不开就当作「可能被持有」，保守处理。
        return True


def collect_references(tasks_root: Path) -> tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]:
    """汇总任务/项目引用关系。

    返回 ``(referenced_paths, referenced_projects, active_task_ids, active_project_ids)``。
    """

    referenced_paths: set[str] = set()
    referenced_projects: set[str] = set()
    active_task_ids: set[str] = set()
    active_project_ids: set[str] = set()

    terminal = {"completed", "failed", "cancelled", "canceled", "error"}

    def _read(path: Path) -> dict:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    for state in ("in", "processing", "completed", "failed"):
        for path in sorted((tasks_root / state).glob("*.json")):
            payload = _read(path)
            task_id = str(payload.get("task_id") or path.stem)
            project_id = str((payload.get("bridge") or {}).get("project_id") or "")
            raw = str(payload.get("raw_video_path") or "")
            if raw:
                candidate = (tasks_root / raw).resolve()
                referenced_paths.add(str(candidate))
            if project_id:
                referenced_projects.add(project_id)
            if state in ("in", "processing"):
                active_task_ids.add(task_id)
                if project_id:
                    active_project_ids.add(project_id)

    # status 里的非终态任务同样算「正在使用」：status 先于 in/*.json 写入。
    for path in sorted((tasks_root / "status").glob("*.json")):
        payload = _read(path)
        task_id = str(payload.get("task_id") or path.stem)
        project_id = str((payload.get("bridge") or {}).get("project_id") or "")
        if str(payload.get("status") or "").strip().lower() in terminal:
            continue
        active_task_ids.add(task_id)
        if project_id:
            active_project_ids.add(project_id)

    # in/processing 里的任务以其 media 目录为准（raw_video_path 可能缺失）。
    for state in ("in", "processing"):
        for path in sorted((tasks_root / state).glob("*.json")):
            task_id = str(_read(path).get("task_id") or path.stem)
            media_dir = tasks_root / "media" / task_id
            if media_dir.is_dir():
                for item in media_dir.iterdir():
                    if item.is_file():
                        referenced_paths.add(str(item.resolve()))

    return (
        frozenset(referenced_paths),
        frozenset(referenced_projects),
        frozenset(active_task_ids),
        frozenset(active_project_ids),
    )


def build_plan(
    temp_dir: Path,
    tasks_root: Path,
    *,
    now: float | None = None,
    min_age_minutes: float = DEFAULT_MIN_AGE_MINUTES,
    chunk_retention_hours: float = DEFAULT_CHUNK_RETENTION_HOURS,
) -> list[Decision]:
    """完整计划：扫描 → 两阶段纯判定 → 回填分组回收量。"""

    now = time.time() if now is None else now
    referenced_paths, referenced_projects, active_tasks, active_projects = collect_references(tasks_root)
    ctx = GcContext(
        now=now,
        min_age_seconds=max(0.0, min_age_minutes) * 60.0,
        referenced_paths=referenced_paths,
        referenced_projects=referenced_projects,
        active_task_ids=active_tasks,
        active_project_ids=active_projects,
    )

    # 第一阶段：不需要指纹就能定下来的判定。
    # 指纹一次算全：保留池的成员、单链接孤儿、以及多链接分组的「整组删除」证明
    # 都要用到它，只给其中一部分算会让分组判定拿不到指纹而误判为「最后一个副本」。
    facts = scan_sources(temp_dir, tasks_root, with_fingerprint=True)
    stage_one = {fact.path: decide(fact, ctx) for fact in facts}

    # 第二阶段：把「保留副本」的指纹集合交给纯函数，完成孪生证明。
    # 注意必须排除 ORPHAN_UNVERIFIED —— 它虽然也是 `keep`，但语义是「拿不准」，
    # 若把它算进孪生池，一个无引用的孤立文件会拿自己的指纹给自己作证。
    retained = frozenset(
        fact.fingerprint for fact in facts
        if fact.fingerprint
        and stage_one[fact.path].action == ACTION_KEEP
        and stage_one[fact.path].code != CODE_ORPHAN_UNVERIFIED
    )
    ctx = replace(ctx, retained_fingerprints=retained)
    decisions = [decide(fact, ctx) for fact in facts]

    retention_seconds = max(0.0, chunk_retention_hours) * 3600.0
    decisions.extend(
        decide_chunk(fact, now=now, retention_seconds=retention_seconds)
        for fact in scan_chunks(temp_dir)
    )
    return resolve_group_policy(decisions, retained_fingerprints=retained)


# ---------------------------------------------------------------------------
# 执行（IO）
# ---------------------------------------------------------------------------

def _prune_empty_parents(path: Path, stops: tuple[Path, ...]) -> None:
    """删除文件后向上清理空目录；到扫描根为止，绝不越界。"""

    parent = path.parent
    while True:
        if any(parent == stop for stop in stops):
            return
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def apply_plan(plan: list[Decision], *, temp_dir: Path, tasks_root: Path, log=print) -> dict[str, int]:
    """执行计划：卸载文件、清理空目录、整目录删除分片。返回统计。"""

    stops = (temp_dir.resolve(), tasks_root.resolve())
    unlinked = 0
    skipped = 0
    for decision in plan:
        if decision.action != ACTION_DELETE:
            continue
        target = Path(decision.path)
        try:
            if decision.kind == KIND_CHUNK:
                shutil.rmtree(target)
            else:
                target.unlink()
                _prune_empty_parents(target, stops)
            unlinked += 1
            log(f"  已删除: {decision.path}")
        except FileNotFoundError:
            skipped += 1
        except OSError as exc:
            skipped += 1
            log(f"  跳过（{type(exc).__name__}: {exc.strerror or errno.errorcode.get(exc.errno, '')}）: {decision.path}")
    return {"deleted": unlinked, "skipped": skipped}


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}TB"


def render_report(plan: list[Decision], *, dry_run: bool) -> str:
    lines: list[str] = []
    deletes = [d for d in plan if d.action == ACTION_DELETE]
    reclaim = sum(d.reclaim_bytes for d in deletes)
    header = "DRY-RUN（未删除任何文件）" if dry_run else "APPLY"
    lines.append("=" * 100)
    lines.append(f"源片副本回收报告 [{header}]")
    lines.append("=" * 100)
    lines.append(f"{'动作':<7}{'判定码':<20}{'inode':>10}{'nlink':>6}{'大小':>11}"
                 f"{'回收':>11}  路径")
    lines.append("-" * 100)
    for decision in sorted(plan, key=lambda d: (d.kind, d.path)):
        lines.append(
            f"{decision.action:<7}{decision.code:<20}{decision.ino:>10}{decision.nlink:>6}"
            f"{_human_bytes(decision.size):>11}{_human_bytes(decision.reclaim_bytes):>11}  {decision.path}"
        )
    lines.append("-" * 100)
    lines.append(f"候选 {len(plan)} 项：删除 {len(deletes)} 项，保留 {len(plan) - len(deletes)} 项")
    lines.append(f"预计回收（真实释放的物理容量）: {_human_bytes(reclaim)} ({reclaim} 字节)")
    lines.append("")
    lines.append("判定码分布：")
    counts: dict[str, int] = {}
    for decision in plan:
        counts[decision.code] = counts.get(decision.code, 0) + 1
    for code, count in sorted(counts.items()):
        lines.append(f"  {code:<22} {count}")
    lines.append("")
    lines.append("删除依据（逐条）：")
    for decision in deletes:
        lines.append(f"  [{decision.code}] {decision.path}")
        lines.append(f"      依据: {decision.reason}")
    return "\n".join(lines)


def plan_to_json(plan: list[Decision]) -> list[dict]:
    return [
        {
            "path": d.path, "kind": d.kind, "action": d.action, "code": d.code,
            "reason": d.reason, "size": d.size, "nlink": d.nlink, "ino": d.ino,
            "reclaim_bytes": d.reclaim_bytes,
        }
        for d in plan
    ]


def _disk_free_line(path: Path) -> str:
    usage = shutil.disk_usage(path)
    return (f"{path}: 总 {_human_bytes(usage.total)} / 已用 {_human_bytes(usage.used)}"
            f" / 可用 {_human_bytes(usage.free)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.source_gc",
        description="回收陈旧源片副本（默认 dry-run，--apply 才真正删除）",
    )
    parser.add_argument("--apply", action="store_true", help="真正执行删除；缺省只报告")
    parser.add_argument("--temp-dir", default=os.getenv("TEMP_DIR", str(BASE_DIR / "temp")))
    parser.add_argument(
        "--tasks-root",
        default=os.getenv("STUDIO_TASKS_ROOT", str(BASE_DIR.parent / "tasks")),
    )
    parser.add_argument("--min-age-minutes", type=float, default=DEFAULT_MIN_AGE_MINUTES,
                        help="修改时间晚于该阈值的文件不参与回收（防并发建任务竞态）")
    parser.add_argument("--chunk-retention-hours", type=float, default=DEFAULT_CHUNK_RETENTION_HOURS,
                        help="studio-chunks 分片保留窗口")
    parser.add_argument("--json", dest="json_path", default=None, help="把计划写成 JSON")
    parser.add_argument("--only", choices=["sources", "chunks"], default=None,
                        help="只处理源片或只处理分片")
    args = parser.parse_args(argv)

    temp_dir = Path(args.temp_dir).resolve()
    tasks_root = Path(args.tasks_root).resolve()

    print("清理前磁盘状态：")
    print("  " + _disk_free_line(Path("/domain") if Path("/domain").exists() else temp_dir))

    plan = build_plan(
        temp_dir, tasks_root,
        min_age_minutes=args.min_age_minutes,
        chunk_retention_hours=args.chunk_retention_hours,
    )
    if args.only == "sources":
        plan = [d for d in plan if d.kind != KIND_CHUNK]
    elif args.only == "chunks":
        plan = [d for d in plan if d.kind == KIND_CHUNK]

    print(render_report(plan, dry_run=not args.apply))
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(plan_to_json(plan), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n计划已写入 {args.json_path}")

    if not args.apply:
        print("\n这是 dry-run，未删除任何文件。确认无误后加 --apply 执行。")
        return 0

    print("\n开始执行删除：")
    stats = apply_plan(plan, temp_dir=temp_dir, tasks_root=tasks_root)
    print(f"\n执行完成：删除 {stats['deleted']} 项，跳过 {stats['skipped']} 项")
    print("清理后磁盘状态：")
    print("  " + _disk_free_line(Path("/domain") if Path("/domain").exists() else temp_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
