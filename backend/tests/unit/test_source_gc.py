"""``src.source_gc`` 的单元测试。

覆盖点：
* 纯决策函数：不删唯一副本、不删被任务/项目引用者、不删正在使用或被保护者、
  删多链接副本、删有内容孪生的孤儿；
* 按 inode 整组回收的语义：只删一部分链接不释放空间，因此必须整组删且回收量
  只计一次；
* dry-run（``build_plan``）绝不产生任何删除副作用，只有 ``apply_plan`` 才落地；
* 分片目录：陈旧才删、被 flock 持有不删、保留窗口内不删。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from src import source_gc
from src.source_gc import (
    ACTION_DELETE,
    ACTION_KEEP,
    CODE_CHUNK_FRESH,
    CODE_CHUNK_LOCKED,
    CODE_CHUNK_STALE,
    CODE_GROUP_LAST_COPY,
    CODE_GROUP_SHARED_LINK,
    CODE_IN_USE,
    CODE_MULTI_LINK,
    CODE_ORPHAN_DUPLICATE,
    CODE_ORPHAN_UNVERIFIED,
    CODE_REFERENCED,
    CODE_TOO_FRESH,
    CODE_UPLOAD_PROTECTED,
    ChunkFact,
    GcContext,
    SourceFact,
    apply_plan,
    build_plan,
    decide,
    decide_chunk,
    resolve_group_policy,
)

NOW = 1_800_000_000.0  # 固定"现在"，让所有判定可复现
OLD = NOW - 10 * 3600        # 10 小时前：早于默认 30 分钟保护期
FRESH = NOW - 60             # 1 分钟前：处于保护期内

FP_A = "aaaaaaaaaaaaaaaa"
FP_B = "bbbbbbbbbbbbbbbb"


def _fact(
    *,
    ino: int = 100,
    nlink: int = 1,
    kind: str = source_gc.KIND_PROJECT,
    size: int = 1024,
    mtime: float = OLD,
    ctime: float | None = None,
    fingerprint: str | None = FP_A,
    task_id: str | None = None,
    project_id: str | None = None,
    dev: int = 1,
) -> SourceFact:
    return SourceFact(
        path=f"/tmp/scan/{kind}-{ino}-{nlink}-{project_id or task_id or 'x'}",
        kind=kind, size=size, nlink=nlink, dev=dev, ino=ino, mtime=mtime,
        ctime=mtime if ctime is None else ctime,
        fingerprint=fingerprint, task_id=task_id, project_id=project_id,
    )


def _ctx(**overrides) -> GcContext:
    base = dict(now=NOW, min_age_seconds=30 * 60)
    base.update(overrides)
    return GcContext(**base)


# ---------------------------------------------------------------------------
# 1. 绝对不删的情况
# ---------------------------------------------------------------------------

def test_decide_keeps_user_upload_even_if_duplicated():
    """studio-uploads 是用户上传源：只报告不删，哪怕它有无数副本。"""
    decision = decide(_fact(kind=source_gc.KIND_UPLOAD, ino=1, nlink=9, size=3_000_000_000), _ctx())
    assert decision.action == ACTION_KEEP
    assert decision.code == CODE_UPLOAD_PROTECTED


def test_decide_keeps_file_used_by_active_task():
    """tasks/in/ 或 tasks/processing/ 里的任务正在用这个文件，绝不删除。"""
    decision = decide(
        _fact(kind=source_gc.KIND_TASK_MEDIA, task_id="autoedit_active", ino=2, nlink=5),
        _ctx(active_task_ids=frozenset({"autoedit_active"})),
    )
    assert decision.action == ACTION_KEEP
    assert decision.code == CODE_IN_USE


def test_decide_keeps_file_of_active_project():
    """项目正被未完成任务引用时不删。"""
    decision = decide(
        _fact(kind=source_gc.KIND_JOB_PROJECT, project_id="timeline_busy", ino=3, nlink=2),
        _ctx(active_project_ids=frozenset({"timeline_busy"})),
    )
    assert decision.action == ACTION_KEEP
    assert decision.code == CODE_IN_USE


def test_decide_keeps_unique_referenced_copy():
    """inode 链接数为 1 且仍被引用 = 最后一个副本，绝对不删。"""
    fact = _fact(ino=4, nlink=1, project_id="timeline_used")
    decision = decide(
        fact,
        _ctx(referenced_projects=frozenset({"timeline_used"}), retained_fingerprints=frozenset({FP_A})),
    )
    assert decision.action == ACTION_KEEP
    assert decision.code == CODE_REFERENCED


def test_decide_keeps_path_referenced_by_task_raw_video_path():
    """被任务 raw_video_path 直接点名的路径不删。"""
    fact = _fact(kind=source_gc.KIND_TASK_MEDIA, task_id="autoedit_done", ino=5, nlink=1)
    decision = decide(
        fact,
        _ctx(
            referenced_paths=frozenset({fact.path}),
            active_task_ids=frozenset(),
            retained_fingerprints=frozenset({FP_A}),
        ),
    )
    assert decision.action == ACTION_KEEP
    assert decision.code == CODE_REFERENCED


def test_decide_keeps_too_fresh_candidate():
    """新建立的目录可能还在写任务 JSON，保护期内不参与回收。"""
    decision = decide(_fact(ino=6, nlink=3, mtime=FRESH), _ctx())
    assert decision.action == ACTION_KEEP
    assert decision.code == CODE_TOO_FRESH


def test_decide_uses_ctime_when_copy_preserves_old_mtime():
    """copy2 会继承源的旧 mtime；刚复制出来的文件必须靠 ctime 判为「太新」。

    否则「几分钟前刚物化、任务 JSON 还没写出来」的目录会被当成陈旧副本删掉。
    """
    copied_just_now = _fact(ino=61, nlink=1, mtime=OLD, ctime=FRESH)
    decision = decide(copied_just_now, _ctx(retained_fingerprints=frozenset({FP_A})))
    assert copied_just_now.age_base == FRESH
    assert decision.action == ACTION_KEEP
    assert decision.code == CODE_TOO_FRESH

    # 同样内容若确实陈旧（ctime 也旧），才允许作为重复副本回收
    truly_old = _fact(ino=62, nlink=1, mtime=OLD, ctime=OLD)
    assert decide(truly_old, _ctx(retained_fingerprints=frozenset({FP_A}))).action == ACTION_DELETE


def test_decide_keeps_orphan_without_content_twin():
    """无引用、链接数为 1、又找不到内容一致的副本 → 拿不准就不删。"""
    decision = decide(
        _fact(ino=7, nlink=1, fingerprint=FP_B), _ctx(retained_fingerprints=frozenset({FP_A}))
    )
    assert decision.action == ACTION_KEEP
    assert decision.code == CODE_ORPHAN_UNVERIFIED


# ---------------------------------------------------------------------------
# 2. 允许删除的情况
# ---------------------------------------------------------------------------

def test_decide_deletes_unreferenced_multi_link_copy():
    """无引用且同一 inode 另有链接：删除该目录项不会丢数据。"""
    decision = decide(_fact(ino=8, nlink=3), _ctx())
    assert decision.action == ACTION_DELETE
    assert decision.code == CODE_MULTI_LINK


def test_decide_deletes_orphan_with_identical_retained_twin():
    """无引用但磁盘上存在内容完全一致的保留副本 → 删掉不丢数据。"""
    decision = decide(
        _fact(ino=9, nlink=1, fingerprint=FP_A),
        _ctx(retained_fingerprints=frozenset({FP_A})),
    )
    assert decision.action == ACTION_DELETE
    assert decision.code == CODE_ORPHAN_DUPLICATE


# ---------------------------------------------------------------------------
# 3. 按 inode 整组回收
# ---------------------------------------------------------------------------

def _decisions(*facts: SourceFact, retained=frozenset({FP_A})) -> list:
    ctx = _ctx(retained_fingerprints=retained)
    return resolve_group_policy([decide(f, ctx) for f in facts], retained_fingerprints=retained)


def test_group_with_protected_member_keeps_other_links():
    """组内还有被引用的目录项时，其余链接降级保留：删它们不省空间只会破坏引用。"""
    shared = _fact(ino=20, nlink=2, kind=source_gc.KIND_PROJECT)
    referenced = _fact(ino=20, nlink=2, kind=source_gc.KIND_JOB_PROJECT, project_id="timeline_used")
    retained = frozenset({FP_A})
    ctx = _ctx(referenced_projects=frozenset({"timeline_used"}), retained_fingerprints=retained)
    resolved = resolve_group_policy(
        [decide(shared, ctx), decide(referenced, ctx)], retained_fingerprints=retained
    )
    by_kind = {d.kind: d for d in resolved}
    assert by_kind[source_gc.KIND_JOB_PROJECT].action == ACTION_KEEP
    assert by_kind[source_gc.KIND_PROJECT].action == ACTION_KEEP
    assert by_kind[source_gc.KIND_PROJECT].code == CODE_GROUP_SHARED_LINK


def test_group_without_content_twin_is_kept_as_last_copy():
    """整组都无引用，但别处没有同内容副本 → 删光就是丢数据，必须保留。"""
    first = _fact(ino=21, nlink=2, kind=source_gc.KIND_JOB_SOURCE, project_id=None)
    second = _fact(ino=21, nlink=2, kind=source_gc.KIND_PROJECT)
    resolved = _decisions(first, second, retained=frozenset({FP_B}))  # 孪生池里没有 FP_A
    assert all(d.action == ACTION_KEEP for d in resolved)
    assert all(d.code == CODE_GROUP_LAST_COPY for d in resolved)


def test_group_with_content_twin_is_deleted_whole_and_reclaim_counted_once():
    """整组无引用且存在同内容保留副本 → 整组删除，回收量只计一次。"""
    first = _fact(ino=22, nlink=2, kind=source_gc.KIND_JOB_SOURCE, size=3_000_000_000)
    second = _fact(ino=22, nlink=2, kind=source_gc.KIND_PROJECT, size=3_000_000_000)
    resolved = _decisions(first, second, retained=frozenset({FP_A}))
    assert all(d.action == ACTION_DELETE for d in resolved)
    assert sum(d.reclaim_bytes for d in resolved) == 3_000_000_000, "同一块物理数据只应计一次"


def test_group_reclaim_is_zero_when_a_link_is_outside_scan_scope():
    """st_nlink 大于扫描到的链接数：还有链接在扫描范围外，数据仍在，回收记 0。"""
    only = _fact(ino=23, nlink=4, kind=source_gc.KIND_JOB_SOURCE, size=500)
    resolved = _decisions(only, retained=frozenset({FP_A}))
    assert resolved[0].action == ACTION_DELETE
    assert resolved[0].reclaim_bytes == 0


def test_task_media_shell_can_be_cleaned_without_reclaiming_space():
    """tasks/media 空壳目录允许零回收清理，但同 inode 的被引用链接必须留下。"""
    referenced = _fact(ino=24, nlink=6, kind=source_gc.KIND_TASK_MEDIA, task_id="autoedit_done")
    shell = _fact(ino=24, nlink=6, kind=source_gc.KIND_TASK_MEDIA, task_id="autoedit_shell")
    ctx = _ctx(referenced_paths=frozenset({referenced.path}))
    resolved = resolve_group_policy(
        [decide(referenced, ctx), decide(shell, ctx)], retained_fingerprints=frozenset({FP_A})
    )
    by_task = {d.path: d for d in resolved}
    assert by_task[referenced.path].action == ACTION_KEEP
    assert by_task[referenced.path].code == CODE_REFERENCED
    assert by_task[shell.path].action == ACTION_DELETE
    assert by_task[shell.path].reclaim_bytes == 0


# ---------------------------------------------------------------------------
# 4. 分片目录
# ---------------------------------------------------------------------------

def test_chunk_stale_is_deleted():
    fact = ChunkFact(path="/tmp/chunks/x", size=1000, mtime=NOW - 48 * 3600,
                     file_count=3, has_receipt=False, locked=False)
    decision = decide_chunk(fact, now=NOW, retention_seconds=24 * 3600)
    assert decision.action == ACTION_DELETE
    assert decision.code == CODE_CHUNK_STALE
    assert decision.reclaim_bytes == 1000


def test_chunk_inside_retention_window_is_kept():
    fact = ChunkFact(path="/tmp/chunks/y", size=1000, mtime=NOW - 3600,
                     file_count=3, has_receipt=True, locked=False)
    decision = decide_chunk(fact, now=NOW, retention_seconds=24 * 3600)
    assert decision.action == ACTION_KEEP
    assert decision.code == CODE_CHUNK_FRESH


def test_chunk_held_by_assembling_process_is_kept():
    """整块陈旧但 .assembly.lock 仍被持有 → 拼装进行中，绝不能删。"""
    fact = ChunkFact(path="/tmp/chunks/z", size=1000, mtime=NOW - 72 * 3600,
                     file_count=5, has_receipt=False, locked=True)
    decision = decide_chunk(fact, now=NOW, retention_seconds=24 * 3600)
    assert decision.action == ACTION_KEEP
    assert decision.code == CODE_CHUNK_LOCKED


# ---------------------------------------------------------------------------
# 5. dry-run 不产生副作用，apply 才删除
# ---------------------------------------------------------------------------

def _build_fixture_tree(root: Path) -> dict[str, Path]:
    """搭一棵最小但结构完整的假工作区：temp/ 与 tasks/ 两个根。"""

    temp = root / "temp"
    tasks = root / "tasks"
    for state in ("in", "processing", "completed", "failed", "status"):
        (tasks / state).mkdir(parents=True, exist_ok=True)

    payload = b"shared-source-bytes" * 512

    # 用户上传源（受保护），同时也是下面两份副本的内容孪生
    upload = temp / "studio-uploads" / "upload-1.mp4"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_bytes(payload)

    # 一个已完成的自动剪辑任务，它的 media 源片是「被引用」的，必须保留
    referenced_media = tasks / "media" / "autoedit_referenced" / "source.mp4"
    referenced_media.parent.mkdir(parents=True, exist_ok=True)
    referenced_media.write_bytes(payload)
    (tasks / "completed" / "autoedit_referenced.json").write_text(
        '{"task_id": "autoedit_referenced", "raw_video_path": "media/autoedit_referenced/source.mp4",'
        ' "bridge": {"project_id": "timeline_keep"}}',
        encoding="utf-8",
    )

    # 无引用的时间线工程 raw 源片：内容与上传一致 → 可回收
    orphan_project = temp / "studio-projects" / "timeline_orphan" / "raw" / "source.mp4"
    orphan_project.parent.mkdir(parents=True, exist_ok=True)
    orphan_project.write_bytes(payload)

    # 陈旧分片：整目录应当被回收
    stale_chunk = temp / "studio-chunks" / "stale-upload"
    stale_chunk.mkdir(parents=True, exist_ok=True)
    (stale_chunk / "000000.part").write_bytes(b"z" * 4096)

    for path in (upload, referenced_media, orphan_project):
        os.utime(path, (OLD, OLD))
    old_dir = NOW - 72 * 3600
    os.utime(stale_chunk, (old_dir, old_dir))
    os.utime(stale_chunk / "000000.part", (old_dir, old_dir))

    return {
        "temp": temp, "tasks": tasks, "upload": upload,
        "referenced_media": referenced_media, "orphan_project": orphan_project,
        "stale_chunk": stale_chunk,
    }


def test_build_plan_is_dry_run_and_deletes_nothing(tmp_path):
    """build_plan 只产出计划，任何文件都不会消失。"""
    tree = _build_fixture_tree(tmp_path)

    plan = build_plan(tree["temp"], tree["tasks"], now=NOW, min_age_minutes=30)

    for key in ("upload", "referenced_media", "orphan_project", "stale_chunk"):
        assert tree[key].exists(), f"dry-run 不得删除 {key}"
    by_path = {d.path: d for d in plan}
    assert by_path[str(tree["upload"])].action == ACTION_KEEP
    assert by_path[str(tree["referenced_media"])].action == ACTION_KEEP
    assert by_path[str(tree["orphan_project"])].action == ACTION_DELETE
    assert by_path[str(tree["orphan_project"])].code == CODE_ORPHAN_DUPLICATE
    assert by_path[str(tree["stale_chunk"])].action == ACTION_DELETE


def test_apply_plan_deletes_exactly_the_planned_paths(tmp_path):
    """apply_plan 只删计划内的路径，被保护的源片与任务输入原样保留。"""
    tree = _build_fixture_tree(tmp_path)
    plan = build_plan(tree["temp"], tree["tasks"], now=NOW, min_age_minutes=30)

    stats = apply_plan(plan, temp_dir=tree["temp"], tasks_root=tree["tasks"], log=lambda *_a: None)

    assert stats["deleted"] >= 2
    assert tree["upload"].exists(), "上传源绝不删除"
    assert tree["referenced_media"].exists(), "被任务引用的源片绝不删除"
    assert not tree["orphan_project"].exists()
    assert not tree["stale_chunk"].exists()
    # 空目录被顺手清理，但任务 JSON 仍在
    assert (tree["tasks"] / "completed" / "autoedit_referenced.json").is_file()


def test_scan_sources_ignores_files_outside_known_roots(tmp_path):
    """扫描只覆盖约定的链路位置，不会误伤无关大文件。"""
    tree = _build_fixture_tree(tmp_path)
    unrelated = tree["temp"] / "clips" / "final.mp4"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_bytes(b"not-a-source")

    facts = source_gc.scan_sources(tree["temp"], tree["tasks"], with_fingerprint=False)

    assert str(unrelated) not in {f.path for f in facts}


def test_project_source_is_protected_while_project_is_still_being_edited(tmp_path):
    """工程里的产物刚被写过 → 它的 raw 源片判为「太新」，不得回收。

    真实场景：另一个代理正在改某个时间线工程的 EDL/SRT（就地写入 ``edit/``，
    既不更新源片也不一定更新父目录 mtime），紧接着就会用它发起自动剪辑。
    只看源片自身的 ctime 会误删这份工程源片。
    """
    from src import source_gc as gc

    temp = tmp_path / "temp"
    tasks = tmp_path / "tasks"
    (tasks / "in").mkdir(parents=True, exist_ok=True)
    (tasks / "status").mkdir(parents=True, exist_ok=True)

    # 内容孪生：上传源仍然在，所以「内容重复」这件事本身成立
    upload = temp / "studio-uploads" / "up.mp4"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_bytes(b"shared" * 1024)

    project = temp / "studio-projects" / "timeline_hot"
    raw = project / "raw" / "source.mp4"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(upload.read_bytes())
    os.utime(raw, (OLD, OLD))

    # 刚写完的工程产物
    edl = project / "edit" / "edl.json"
    edl.parent.mkdir(parents=True, exist_ok=True)
    edl.write_text("{}", encoding="utf-8")
    os.utime(edl, (FRESH, FRESH))

    facts = {f.path: f for f in gc.scan_sources(temp, tasks, with_fingerprint=True)}
    fact = facts[str(raw)]
    decision = gc.decide(fact, _ctx(retained_fingerprints=frozenset({gc.fingerprint(upload)})))

    assert fact.age_base > OLD, "工程内的新写入必须体现在新鲜度上"
    assert decision.action == ACTION_KEEP
    assert decision.code == CODE_TOO_FRESH


def test_fingerprint_distinguishes_different_content(tmp_path):
    """指纹必须区分内容：同大小不同内容不能算孪生，同内容复制才算。"""
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    first.write_bytes(b"a" * 4096)
    second.write_bytes(b"b" * 4096)

    assert source_gc.fingerprint(first) != source_gc.fingerprint(second)

    clone = tmp_path / "a-copy.mp4"
    clone.write_bytes(first.read_bytes())
    assert source_gc.fingerprint(first) == source_gc.fingerprint(clone)
    assert source_gc.fingerprint(first).startswith(source_gc.fingerprint(first))
