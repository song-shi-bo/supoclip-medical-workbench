"""封面固化规则 + 版本快照/撤销 的单元测试（纯文件系统，不跑 ffmpeg、不联网）。

覆盖任务书 §二/§三 的硬约束：
1. 主钩子总长 ≤12 个汉字、最多两行；已在预算内时**一字不改**（幂等）；
2. 证据字段（evidence_segment_ids 等）在压缩后原样保留；
3. 文案过短无法满足两行各 ≥2 字时不猜、不截断，显式说明；
4. 版本快照 rev-NNN 幂等（重复执行不叠加）；
5. install_revision 逐字节校验，v1 走基线回滚；
6. 撤回到不存在的版本 → E_COVER_BACKUP_MISSING；撤回到当前版本 → E_COVER_BAD_REQUEST。
"""

from __future__ import annotations

import json

import pytest

from src import cover_replace as cr
from src import studio_auto_edit_bridge as bridge

EVIDENCE = {
    "evidence_segment_ids": ["seg-1"],
    "top_theme_evidence_segment_ids": ["seg-1"],
    "headline_line_1_evidence_segment_ids": ["seg-1"],
    "headline_line_2_evidence_segment_ids": ["seg-2"],
}


def _copy(line_1: str, line_2: str, theme: str = "警惕") -> dict:
    return {"top_theme": theme, "headline_line_1": line_1,
            "headline_line_2": line_2, **EVIDENCE}


# ---------------------------------------------------------------------------
# 1. 文案规则
# ---------------------------------------------------------------------------

def test_short_hook_is_untouched():
    copy_, report = cr.clamp_cover_copy(_copy("别再拖", "当胃病"))
    assert report["changed"] is False
    assert copy_["headline_line_1"] == "别再拖"
    assert copy_["headline_line_2"] == "当胃病"
    assert report["after"]["total_chars"] == 6


def test_long_hook_is_clamped_to_12_chars_two_lines():
    copy_, report = cr.clamp_cover_copy(_copy("胰腺癌早期有三个信号", "别再当成胃病拖着"))
    assert report["changed"] is True
    assert report["after"]["total_chars"] <= cr.HOOK_MAX_TOTAL_CHARS
    assert len(copy_["headline_line_1"]) >= 2 and len(copy_["headline_line_2"]) >= 2
    assert len(copy_["headline_line_1"]) + len(copy_["headline_line_2"]) <= 12
    assert "12" in report["reason"]


def test_evidence_fields_are_preserved():
    """压缩只动钩子两行；每个字段的证据段 id 必须逐字保留（医疗可回溯红线）。"""
    copy_, _ = cr.clamp_cover_copy(_copy("胰腺癌早期有三个信号", "别再当成胃病拖着"))
    for key, value in EVIDENCE.items():
        assert copy_[key] == value
    assert copy_["top_theme"] == "警惕"


def test_very_short_copy_passes_through_unchanged():
    """本来就短（预算内）的文案不动它；压缩只在超预算时发生。"""
    copy_, report = cr.clamp_cover_copy(_copy("癌", "症"))
    assert report["changed"] is False
    assert report["reason"] is None
    assert copy_["headline_line_1"] == "癌"
    assert copy_["headline_line_2"] == "症"


def test_clamp_is_idempotent():
    once, _ = cr.clamp_cover_copy(_copy("胰腺癌早期有三个信号", "别再当成胃病拖着"))
    twice, report = cr.clamp_cover_copy(once)
    assert report["changed"] is False
    assert once == twice


def test_punctuation_does_not_consume_the_budget():
    """标点不占 12 字额度（按汉字计数），但输出必须仍是 ≤12 个汉字。"""
    copy_, report = cr.clamp_cover_copy(_copy("胰腺癌，早期信号", "别再拖"))
    assert report["after"]["total_chars"] <= 12


# ---------------------------------------------------------------------------
# 2. 版本快照与撤销
# ---------------------------------------------------------------------------

@pytest.fixture
def clip_env(tmp_path, monkeypatch):
    root = tmp_path / "tasks"
    clip_dir = root / "output" / "autoedit_0123456789ab" / "clip-01"
    clip_dir.mkdir(parents=True)
    (clip_dir / "clip_final.mp4").write_bytes(b"ORIGINAL-VIDEO" * 100)
    (clip_dir / "cover.jpg").write_bytes(b"ORIGINAL-COVER")
    monkeypatch.setattr(bridge, "TASKS_ROOT", root)
    # 不跑真实 ffprobe：快照只需要文件本身
    monkeypatch.setattr(cr, "probe_streams", lambda _path: {
        "frames": 427, "duration_s": 17.1, "fps": 25.0, "has_audio": True,
        "width": 1080, "height": 1920,
    })
    return clip_dir


def test_snapshot_revision_is_idempotent(clip_env):
    first = cr.snapshot_revision(clip_env, 2, {"kind": "generated", "cover_sha256": "x"})
    again = cr.snapshot_revision(clip_env, 2, {"kind": "adopted"})
    assert first == again                       # 已存在 → 原样复用，不叠加
    assert first["kind"] == "generated"
    assert (cr.revision_dir(clip_env, 2) / cr.COVER_REVISION_META).is_file()


def test_current_revision_counts_snapshots(clip_env):
    assert cr.current_revision(clip_env) == 0
    cr._ensure_baseline(clip_env, cr._load_state(clip_env))
    assert cr.current_revision(clip_env) == 1
    cr.snapshot_revision(clip_env, 2, {})
    cr.snapshot_revision(clip_env, 3, {})
    assert cr.current_revision(clip_env) == 3


def test_list_revisions_includes_baseline_as_v1(clip_env):
    state = cr._load_state(clip_env)
    cr._ensure_baseline(clip_env, state)
    cr.snapshot_revision(clip_env, 2, {"kind": "generated", "cover_sha256": "abc"})
    revisions = cr.list_revisions(clip_env)
    assert [item["revision"] for item in revisions] == [1, 2]
    assert revisions[0]["from_baseline"] is True


def test_install_revision_v1_restores_baseline_bytes(clip_env):
    state = cr._load_state(clip_env)
    cr._ensure_baseline(clip_env, state)
    (clip_env / "cover.jpg").write_bytes(b"NEW-COVER")
    (clip_env / "clip_final.mp4").write_bytes(b"NEW-VIDEO")

    result = cr.install_revision(clip_env, 1)
    assert result["restored"] is True
    assert (clip_env / "cover.jpg").read_bytes() == b"ORIGINAL-COVER"
    assert (clip_env / "clip_final.mp4").read_bytes() == b"ORIGINAL-VIDEO" * 100


def test_install_revision_v2_restores_that_snapshot(clip_env):
    state = cr._load_state(clip_env)
    cr._ensure_baseline(clip_env, state)
    (clip_env / "cover.jpg").write_bytes(b"V2-COVER")
    cr.snapshot_revision(clip_env, 2, {"replace_intro": False})
    (clip_env / "cover.jpg").write_bytes(b"V3-COVER")

    result = cr.install_revision(clip_env, 2)
    assert result["restored"] is True
    assert result["restored_video"] is False        # 未换片头 → 不白占磁盘存一份成片
    assert (clip_env / "cover.jpg").read_bytes() == b"V2-COVER"


def test_undo_cover_revision_rejects_unknown_and_current(clip_env):
    state = cr._load_state(clip_env)
    cr._ensure_baseline(clip_env, state)
    # 撤销到「未来」（比当前还新）没有意义
    with pytest.raises(cr.CoverReplaceError) as excinfo:
        cr.undo_cover_revision("autoedit_0123456789ab", "clip-01", 5)
    assert excinfo.value.code == cr.E_COVER_BAD_REQUEST

    # 历史里缺了中间那一版（v2/v4 有快照但 v3 没有）→ 显式报备份缺失
    cr.snapshot_revision(clip_env, 2, {})
    cr.snapshot_revision(clip_env, 4, {})
    with pytest.raises(cr.CoverReplaceError) as excinfo:
        cr.undo_cover_revision("autoedit_0123456789ab", "clip-01", 3)
    assert excinfo.value.code == cr.E_COVER_BACKUP_MISSING


def test_undo_cover_revision_round_trip(clip_env):
    state = cr._load_state(clip_env)
    cr._ensure_baseline(clip_env, state)
    (clip_env / "cover.jpg").write_bytes(b"V2-COVER")
    cr.snapshot_revision(clip_env, 2, {})
    (clip_env / "cover.jpg").write_bytes(b"V3-COVER")
    cr.snapshot_revision(clip_env, 3, {})

    result = cr.undo_cover_revision("autoedit_0123456789ab", "clip-01", 1)
    assert result["kind"] == "undo"
    assert result["cover_revision"] == 1
    assert (clip_env / "cover.jpg").read_bytes() == b"ORIGINAL-COVER"


def test_read_cover_state_exposes_revision_and_asset_id(clip_env):
    state = cr._load_state(clip_env)
    cr._ensure_baseline(clip_env, state)
    (clip_env / "cover.jpg").write_bytes(b"V2-COVER")
    cr.snapshot_revision(clip_env, 2, {"kind": "generated"})

    view = cr.read_cover_state("autoedit_0123456789ab", "clip-01")
    assert view["cover_revision"] == 2
    assert view["cover_asset_id"] == "cover-clip-01-v2"
    assert view["can_undo"] is True and view["undo_revision"] == 1
    assert [item["revision"] for item in view["revisions"]] == [1, 2]
    assert view["cover_sha256"] == cr._sha256(clip_env / "cover.jpg")


def test_cover_backup_dir_is_not_polluted_by_revisions(clip_env):
    """既有契约：cover_backup/ 只放基线备份，历史版本必须落在别处。"""
    state = cr._load_state(clip_env)
    cr._ensure_baseline(clip_env, state)
    cr.snapshot_revision(clip_env, 2, {})
    backup_entries = sorted(p.name for p in (clip_env / cr.COVER_BACKUP_DIRNAME).iterdir())
    assert len(backup_entries) == 2
    assert (clip_env / cr.COVER_REVISIONS_DIRNAME).is_dir()


# ---------------------------------------------------------------------------
# 封面版本管理（2026-10-13）：activate 任意切换 / delete 版本删除
# ---------------------------------------------------------------------------

def test_activate_cover_revision_switches_any_direction(clip_env):
    state = cr._load_state(clip_env)
    cr._ensure_baseline(clip_env, state)
    (clip_env / "cover.jpg").write_bytes(b"V2-COVER")
    cr.snapshot_revision(clip_env, 2, {})
    (clip_env / "cover.jpg").write_bytes(b"V3-COVER")
    cr.snapshot_revision(clip_env, 3, {})

    # v3 当前 → 切到 v2（物理字节级替换）
    cr.activate_cover_revision("autoedit_0123456789ab", "clip-01", 2)
    assert (clip_env / "cover.jpg").read_bytes() == b"V2-COVER"
    assert cr._load_state(clip_env)["active_revision"] == 2

    # v2 当前 → 正向切回 v3（undo 做不到的方向）
    cr.activate_cover_revision("autoedit_0123456789ab", "clip-01", 3)
    assert (clip_env / "cover.jpg").read_bytes() == b"V3-COVER"

    # 切到当前版本 → 空操作，显式拒绝
    with pytest.raises(cr.CoverReplaceError) as excinfo:
        cr.activate_cover_revision("autoedit_0123456789ab", "clip-01", 3)
    assert excinfo.value.code == cr.E_COVER_BAD_REQUEST


def test_delete_cover_revision_rules(clip_env):
    state = cr._load_state(clip_env)
    cr._ensure_baseline(clip_env, state)
    (clip_env / "cover.jpg").write_bytes(b"V2-COVER")
    cr.snapshot_revision(clip_env, 2, {})
    (clip_env / "cover.jpg").write_bytes(b"V3-COVER")
    cr.snapshot_revision(clip_env, 3, {})

    # v1 基线是回滚锚点，不可删
    with pytest.raises(cr.CoverReplaceError) as excinfo:
        cr.delete_cover_revision("autoedit_0123456789ab", "clip-01", 1)
    assert excinfo.value.code == cr.E_COVER_BAD_REQUEST

    # 当前使用中的 v3 不可删
    with pytest.raises(cr.CoverReplaceError) as excinfo:
        cr.delete_cover_revision("autoedit_0123456789ab", "clip-01", 3)
    assert excinfo.value.code == cr.E_COVER_BAD_REQUEST

    # 删 v2：目录移除、状态记录修剪、当前版本不变
    result = cr.delete_cover_revision("autoedit_0123456789ab", "clip-01", 2)
    assert result["deleted_revision"] == 2
    assert not cr.revision_dir(clip_env, 2).exists()
    assert cr.current_revision(clip_env) == 3
    assert all(int(r.get("revision") or 0) != 2 for r in cr._load_state(clip_env)["records"])

    # 重复删除 → 显式快照缺失
    with pytest.raises(cr.CoverReplaceError) as excinfo:
        cr.delete_cover_revision("autoedit_0123456789ab", "clip-01", 2)
    assert excinfo.value.code == cr.E_COVER_BACKUP_MISSING


def test_activate_cover_rejects_failed_qa_before_touching_files(monkeypatch):
    """qa_status != passed 的版本不得设为当前：先拒绝，绝不触达物理层。"""
    from src import studio_local_tasks as slt

    entry = {"render_task_id": "autoedit_0123456789ab",
             "cover": {"revision": 3, "active_revision": 3,
                       "history": [{"revision": 2, "qa_status": "failed"}]}}
    monkeypatch.setattr(slt.store, "entry_for", lambda pid, cid: entry)

    def _boom(*_args, **_kwargs):
        raise AssertionError("QA 未通过时不得触达物理安装层")

    monkeypatch.setattr(slt, "activate_cover_revision", _boom)
    with pytest.raises(slt.LocalTaskError) as excinfo:
        slt.activate_cover("timeline_x", "clip-01", 2)
    assert excinfo.value.code == "E_COVER_QA_FAILED"


def test_prune_cover_history_rules(tmp_path, monkeypatch):
    """台账侧删除：当前使用中拒绝；非当前版本修剪成功。"""
    from src import studio_project_store as store

    manifest = {"clips": {"clip-01": {
        "cover": {"revision": 3, "active_revision": 3, "history": [
            {"revision": 1}, {"revision": 2}, {"revision": 3}]}}}}
    monkeypatch.setattr(store, "load_manifest", lambda pid: manifest)
    monkeypatch.setattr(store, "save_manifest", lambda pid, m: m)
    monkeypatch.setattr(store, "TASKS_ROOT", tmp_path)

    with pytest.raises(store.ProjectStoreError) as excinfo:
        store.prune_cover_history("timeline_x", "clip-01", revision=3)
    assert excinfo.value.code == "E_COVER_REVISION_ACTIVE"

    with pytest.raises(store.ProjectStoreError) as excinfo:
        store.prune_cover_history("timeline_x", "clip-01", revision=9)
    assert excinfo.value.code == "E_COVER_REVISION_UNKNOWN"

    store.prune_cover_history("timeline_x", "clip-01", revision=2)
    revisions = [h["revision"] for h in manifest["clips"]["clip-01"]["cover"]["history"]]
    assert revisions == [1, 3]
