"""Inspector Phase 1 任务路由证据：六类局部任务都**不得**生成 AUTO_EDIT。

为什么单独一个文件：契约 §4 的硬约束是「所有成片级编辑只走局部通道」。
本文件对六个局部类型（``UPDATE_SUBTITLE`` / ``UPDATE_TRANSFORM`` / ``UPDATE_AUDIO`` /
``UPDATE_EFFECTS`` / ``RENDER_CLIP`` / ``REGENERATE_COVER``）逐个创建任务并同步跑完
``_run_task``，再从**四个独立角度**取证 AUTO_EDIT 从未出现：

1. 任务记录里的 ``task_type`` 恰好是提交的那一个（前缀一一对应）；
2. 项目任务表（``list_tasks``）里没有 AUTO_EDIT / 没有 ``autoedit_*`` 任务 id；
3. clip 台账的 ``task_history`` 里没有 AUTO_EDIT；
4. 任务根下没有**新增**任何 ``autoedit*`` 路径（任务 JSON / 输出目录 / 状态镜像）。

另外两条边界证据：
* 六个类型全部跑完后台账里仍只有六条局部任务（``TASK_AUTO_EDIT not in [...]``）；
* ``create_task(task_type="AUTO_EDIT")`` 仍然被拒绝（AUTO_EDIT 只能由「开始自动剪辑」入口提交）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import studio_auto_edit_bridge as bridge
from src import studio_local_tasks as tasks
from src import studio_project_store as store

PROJECT_ID = "timeline_abc123def456"
RENDER_TASK = "autoedit_0123456789ab"
CLIP_ID = "clip-741ef5-01"

#: 契约 §4 的六个局部类型（五个新类型 + 既有的封面通道）。
LOCAL_TYPES = (
    tasks.TASK_UPDATE_SUBTITLE,
    tasks.TASK_UPDATE_TRANSFORM,
    tasks.TASK_UPDATE_AUDIO,
    tasks.TASK_UPDATE_EFFECTS,
    tasks.TASK_RENDER_CLIP,
    tasks.TASK_REGENERATE_COVER,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """假任务根 + 一条已登记的成片（含真实的成片/封面文件与桥接层根目录）。"""
    root = tmp_path / "tasks"
    monkeypatch.setattr(tasks, "TASKS_ROOT", root)
    monkeypatch.setattr(store, "TASKS_ROOT", root)
    monkeypatch.setattr(bridge, "TASKS_ROOT", root)

    clip_dir = root / "output" / RENDER_TASK / CLIP_ID
    clip_dir.mkdir(parents=True)
    (clip_dir / "clip_final.mp4").write_bytes(b"ORIGINAL-VIDEO" * 100)
    (clip_dir / "cover.jpg").write_bytes(b"ORIGINAL-COVER")
    store.register_clips(PROJECT_ID, RENDER_TASK, [{
        "clip_id": CLIP_ID, "name": "剪辑成片 01", "title": "胰腺癌早期信号",
        "topic": "健康科普", "source_start": 0.0, "source_end": 16.3, "duration": 17.1,
        "cover_path": f"output/{RENDER_TASK}/{CLIP_ID}/cover.jpg",
    }])
    return {"root": root, "clip_dir": clip_dir, "project_id": PROJECT_ID,
            "render_task": RENDER_TASK, "clip_id": CLIP_ID}


def _payload_for(task_type: str) -> dict:
    """每个类型的最小合法输入（都不需要联网/不需要 video-brain）。"""
    if task_type == tasks.TASK_UPDATE_SUBTITLE:
        return {"config": {"font_size_px": 70},
                "cues": [{"segment_id": "seg-1", "start": 0.5, "end": 2.0, "text": "术后复查"}]}
    if task_type == tasks.TASK_UPDATE_TRANSFORM:
        return {"transform": {"scale": 1.2, "positionX": 12.0, "positionY": -8.0,
                              "fitMode": "cover"}}
    if task_type == tasks.TASK_UPDATE_AUDIO:
        return {"voice_volume": 0.6, "muted": True}
    if task_type == tasks.TASK_UPDATE_EFFECTS:
        return {"effects": {"filter": {"name": "warm", "strength": 0.4},
                            "export_resolution": "1080x1080"}}
    if task_type == tasks.TASK_RENDER_CLIP:
        return {"note": "登记一次待渲染"}
    return {}   # REGENERATE_COVER 走默认风格


def _fake_replace_cover(env):
    def fake(task_id, clip_id, style_id, *, replace_intro=True, cover_copy_override=None):
        cover = env["clip_dir"] / "cover.jpg"
        cover.write_bytes(b"NEW-COVER-V2")
        return {"kind": "generated", "idempotent": False, "cover_abs_path": str(cover),
                "cover_revision": 2, "cover_copy": None, "copy_report": None, "qc": None,
                "source": {"frame_ts_in_base": 1.2}}
    return fake


def _autoedit_artifacts(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("autoedit*")}


def _autoedit_task_files(root: Path) -> list[str]:
    """任务定义/状态层面的 autoedit 文件（``in|processing|completed|failed|status``）。"""
    found: list[str] = []
    for directory in ("in", "processing", "completed", "failed", "status"):
        base = root / directory
        if base.is_dir():
            found.extend(path.name for path in base.glob("autoedit*"))
    return found


def _run_local_task(env, task_type: str) -> dict:
    """create_task(run=False) + 同步 _run_task：确定性地跑完一条局部任务。"""
    record = tasks.create_task(
        project_id=env["project_id"], clip_id=env["clip_id"], task_type=task_type,
        instruction=f"局部通道：{task_type}", payload=_payload_for(task_type), run=False)
    tasks._run_task(record)
    # run=False 不经过 _submit 的 finally，这里手动释放并发配额预留，避免脏预留跨用例累积
    # （并发上限=2：不释放会让第 3 个用例起全部 429 studio_user_concurrency_quota）。
    from src.studio_access import release_operation
    release_operation(record["task_id"])
    return tasks.get_task(record["task_id"])


def _assert_no_auto_edit(env, task_type: str, task_id: str, before: set[str]) -> None:
    """四个角度的「没有 AUTO_EDIT」取证（单条任务与批量场景共用）。"""
    # 1) 任务记录本身
    record = tasks.get_task(task_id)
    assert record["task_type"] == task_type
    assert tasks.task_type_of(task_id) == task_type
    assert not task_id.startswith("autoedit_")

    # 2) 项目任务表
    listed = tasks.list_tasks(project_id=env["project_id"])
    assert tasks.TASK_AUTO_EDIT not in [item.get("task_type") for item in listed]
    assert not any(str(item.get("task_id") or "").startswith("autoedit_") for item in listed)
    assert task_id in [item["task_id"] for item in listed]

    # 3) clip 台账 task_history
    history = store.list_cards(env["project_id"])[0]["task_history"]
    assert tasks.TASK_AUTO_EDIT not in [item.get("task_type") for item in history]
    assert not any(str(item.get("task_id") or "").startswith("autoedit_") for item in history)
    assert [item["task_id"] for item in history] == [task_id]

    # 4) 任务根：没有新增 autoedit 产物，也没有 autoedit 任务/状态文件
    assert _autoedit_artifacts(env["root"]) == before
    assert _autoedit_task_files(env["root"]) == []

    # 追加式日志里也只有这一个类型（人可读的「只建局部任务」证据）
    log_lines = [json.loads(line) for line in
                 (env["root"] / "logs" / "local_tasks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log_lines and {line["task_type"] for line in log_lines} == {task_type}


@pytest.mark.parametrize("task_type", LOCAL_TYPES)
def test_each_local_type_completes_without_creating_auto_edit(env, monkeypatch, task_type):
    monkeypatch.setattr(tasks, "replace_cover", _fake_replace_cover(env))
    before = _autoedit_artifacts(env["root"])

    done = _run_local_task(env, task_type)

    assert done["status"] == "completed", done.get("error")
    _assert_no_auto_edit(env, task_type, done["task_id"], before)


def test_all_six_local_types_never_touch_the_auto_edit_path(env, monkeypatch):
    """六个类型依次跑完：只有六条局部任务，前缀各不相同，AUTO_EDIT 一次都没出现。"""
    monkeypatch.setattr(tasks, "replace_cover", _fake_replace_cover(env))
    before = _autoedit_artifacts(env["root"])

    task_ids: list[str] = []
    for task_type in LOCAL_TYPES:
        done = _run_local_task(env, task_type)
        assert done["status"] == "completed", (task_type, done.get("error"))
        assert done["task_type"] == task_type
        task_ids.append(done["task_id"])

    assert len(set(task_ids)) == len(LOCAL_TYPES)
    listed = tasks.list_tasks(project_id=env["project_id"])
    assert sorted(item["task_type"] for item in listed) == sorted(LOCAL_TYPES)
    assert tasks.TASK_AUTO_EDIT not in [item.get("task_type") for item in listed]
    assert not any(str(item.get("task_id") or "").startswith("autoedit_") for item in listed)

    history = store.list_cards(env["project_id"])[0]["task_history"]
    assert sorted(item["task_type"] for item in history) == sorted(LOCAL_TYPES)
    assert tasks.TASK_AUTO_EDIT not in [item.get("task_type") for item in history]

    # 任务根下 autoedit 相关路径与开跑之前逐项一致（没有新任务目录/状态文件）
    assert _autoedit_artifacts(env["root"]) == before
    assert _autoedit_task_files(env["root"]) == []
    # 台账仍只有这一条 clip（局部通道绝不新增卡片）
    assert len(store.list_cards(env["project_id"])) == 1


def test_prefix_map_stays_one_to_one_with_the_new_types():
    assert set(tasks.TASK_PREFIX) == set(tasks.TASK_TYPES)
    expected = {"UPDATE_SUBTITLE": "msub", "UPDATE_TRANSFORM": "xfm",
                "UPDATE_AUDIO": "aud", "UPDATE_EFFECTS": "fx", "RENDER_CLIP": "rnd"}
    for task_type, prefix in expected.items():
        assert tasks.TASK_PREFIX[task_type] == prefix
        assert tasks.task_type_of(f"{prefix}_0123456789ab") == task_type
    # 既有前缀（含老任务的 subtitle）不变
    assert tasks.task_type_of("subtitle_0123456789ab") == tasks.TASK_UPDATE_SUBTITLE_STYLE
    assert tasks.task_type_of("autoedit_0123456789ab") == tasks.TASK_AUTO_EDIT
    assert tasks.FULL_PIPELINE_TYPES == {tasks.TASK_AUTO_EDIT}


def test_auto_edit_is_still_refused_through_the_local_entry(env):
    """负向证据：局部任务入口永远建不出 AUTO_EDIT（只能走「开始自动剪辑」）。"""
    before = _autoedit_artifacts(env["root"])
    with pytest.raises(tasks.LocalTaskError) as excinfo:
        tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                          task_type=tasks.TASK_AUTO_EDIT, run=False)
    assert excinfo.value.code == "E_TASK_TYPE_UNKNOWN"
    assert "AUTO_EDIT" in str(excinfo.value)
    # 被拒绝的提交不留痕：没有任务记录、没有 autoedit 产物
    assert tasks.list_tasks(project_id=env["project_id"]) == []
    assert _autoedit_artifacts(env["root"]) == before
