"""resolve_auto_edit_artifact 的任务标识与穿越防护回归测试。

回归 2026-10-16：该函数此前复用 _task_paths 的 TASK_ID_PATTERN（只认
autoedit_*），新渲染管线/reedit 卡片的 render_task_id 是 32 位 hex job id，
一律 400「Invalid auto-edit task id」，导致新成片无法经
/studio/auto-edit/{id}/artifact 播放（无画面/播放不了）。
"""

from __future__ import annotations

import pytest

from src import studio_auto_edit_bridge as bridge

JOB_ID = "192a7b28a47c41059fb147651593d480"


def _make_staged_clip(root, job_id: str = JOB_ID, clip_id: str = "clip-x1") -> None:
    staged = root / "tasks" / "output" / job_id / "clips" / clip_id / "clip_final.mp4"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"0")


def test_accepts_job_id_and_staged_layout(tmp_path, monkeypatch):
    """32 位 job id + clips/ 子层布局必须可解析（reedit 新卡片的主播放路径）。"""
    monkeypatch.setattr(bridge, "TASKS_ROOT", tmp_path / "tasks")
    _make_staged_clip(tmp_path)
    resolved = bridge.resolve_auto_edit_artifact(
        JOB_ID, f"output/{JOB_ID}/clips/clip-x1/clip_final.mp4")
    assert resolved == (tmp_path / "tasks" / "output" / JOB_ID / "clips"
                        / "clip-x1" / "clip_final.mp4").resolve()


def test_accepts_legacy_task_id_and_layout(tmp_path, monkeypatch):
    """旧 autoedit_* 任务 + 平铺布局行为不变。"""
    monkeypatch.setattr(bridge, "TASKS_ROOT", tmp_path / "tasks")
    legacy = tmp_path / "tasks" / "output" / "autoedit_aaaaaaaaaaaa" / "clip-x1" / "clip_final.mp4"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"0")
    resolved = bridge.resolve_auto_edit_artifact(
        "autoedit_aaaaaaaaaaaa", "output/autoedit_aaaaaaaaaaaa/clip-x1/clip_final.mp4")
    assert resolved == legacy.resolve()


def test_rejects_malformed_and_traversal(tmp_path, monkeypatch):
    """非法任务标识报 ValueError（路由映射 400）；穿越与缺失报 FileNotFoundError。"""
    monkeypatch.setattr(bridge, "TASKS_ROOT", tmp_path / "tasks")
    _make_staged_clip(tmp_path)
    with pytest.raises(ValueError):
        bridge.resolve_auto_edit_artifact("../evil", "output/x/clip_final.mp4")
    with pytest.raises(ValueError):
        bridge.resolve_auto_edit_artifact("short", "output/short/clip_final.mp4")
    with pytest.raises(FileNotFoundError):
        bridge.resolve_auto_edit_artifact(
            JOB_ID, "output/" + JOB_ID + "/clips/clip-x1/../../secret.mp4")
    with pytest.raises(FileNotFoundError):
        bridge.resolve_auto_edit_artifact(
            JOB_ID, "output/" + JOB_ID + "/clips/clip-x1/missing.mp4")
