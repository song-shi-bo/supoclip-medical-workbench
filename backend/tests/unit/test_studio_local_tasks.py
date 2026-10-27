"""studio_local_tasks 单元测试：任务类型拆分 + 局部任务落盘（不联网、不跑 ffmpeg）。

覆盖本轮改造的核心命题：
1. 任务类型/前缀一一对应，AUTO_EDIT 不能被局部任务入口创建；
2. 局部任务只作用于台账里已登记的 clip（不存在 → 显式 E_ 码）；
3. REGENERATE_COVER：只更新封面资产、cover_revision+1、**clip_final.mp4 逐字节不变**；
4. UPDATE_TITLE：只改文字，不重剪（video_rerendered=False）；
5. UPDATE_SUBTITLE_STYLE：只写样式配置 + 本 clip 的 render_revision；
6. UPDATE_TIMELINE：只改本 clip 的时间线区间，越界/非法区间显式拒绝；
7. 任务记录 / 状态镜像 / jsonl 日志三处都能看到 task_type（「只建局部任务」的证据）。
"""

from __future__ import annotations

import json

import pytest

from src import studio_local_tasks as tasks
from src import studio_project_store as store


@pytest.fixture
def env(tmp_path, monkeypatch):
    """假任务根目录 + 一条已登记的成片（封面/成片都用可校验的固定字节）。"""
    root = tmp_path / "tasks"
    monkeypatch.setattr(tasks, "TASKS_ROOT", root)
    monkeypatch.setattr(store, "TASKS_ROOT", root)
    # cover_replace 定位成片目录走 bridge 的 TASKS_ROOT（APPLY_COVER 用得到）
    from src import studio_auto_edit_bridge as bridge
    monkeypatch.setattr(bridge, "TASKS_ROOT", root)

    project_id = "timeline_abc123def456"
    render_task = "autoedit_0123456789ab"
    clip_id = "clip-741ef5-01"
    clip_dir = root / "output" / render_task / clip_id
    clip_dir.mkdir(parents=True)
    (clip_dir / "clip_final.mp4").write_bytes(b"ORIGINAL-VIDEO" * 100)
    (clip_dir / "cover.jpg").write_bytes(b"ORIGINAL-COVER")
    store.register_clips(project_id, render_task, [{
        "clip_id": clip_id, "name": "剪辑成片 01", "title": "胰腺癌早期信号",
        "topic": "健康科普", "source_start": 0.14, "source_end": 16.3, "duration": 16.96,
        "cover_path": f"output/{render_task}/{clip_id}/cover.jpg",
    }])
    return {
        "root": root, "project_id": project_id, "render_task": render_task,
        "clip_id": clip_id, "clip_dir": clip_dir,
    }


def _wait(task_id: str, timeout: float = 10.0) -> dict:
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = tasks.get_task(task_id)
        if record.get("status") in {"completed", "failed"}:
            return record
        time.sleep(0.02)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内结束")


# ---------------------------------------------------------------------------
# 1. 任务类型与路由
# ---------------------------------------------------------------------------

def test_task_prefixes_map_one_to_one():
    for task_type, prefix in tasks.TASK_PREFIX.items():
        assert tasks.task_type_of(f"{prefix}_0123456789ab") == task_type
    assert tasks.task_type_of("nope_0123456789ab") is None
    assert set(tasks.TASK_PREFIX) == set(tasks.TASK_TYPES)


def test_auto_edit_cannot_be_created_through_local_task_entry(env):
    """「改封面」绝不能落成 auto_edit：局部任务入口直接拒绝 AUTO_EDIT。"""
    with pytest.raises(tasks.LocalTaskError) as excinfo:
        tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                          task_type=tasks.TASK_AUTO_EDIT)
    assert excinfo.value.code == "E_TASK_TYPE_UNKNOWN"
    assert "AUTO_EDIT" in str(excinfo.value)


def test_unknown_task_type_is_rejected(env):
    with pytest.raises(tasks.LocalTaskError) as excinfo:
        tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                          task_type="RERENDER_EVERYTHING")
    assert excinfo.value.code == "E_TASK_TYPE_UNKNOWN"


def test_unregistered_clip_is_rejected(env):
    with pytest.raises(store.ProjectStoreError) as excinfo:
        tasks.create_task(project_id=env["project_id"], clip_id="clip-nope",
                          task_type=tasks.TASK_REGENERATE_COVER)
    assert excinfo.value.code == "E_CLIP_NOT_REGISTERED"


# ---------------------------------------------------------------------------
# 2. REGENERATE_COVER：封面原地更新，正片逐字节不动
# ---------------------------------------------------------------------------

def test_regenerate_cover_updates_cover_only(env, monkeypatch):
    calls: list[dict] = []

    def fake_replace(task_id, clip_id, style_id, *, replace_intro=True, cover_copy_override=None):
        calls.append({"task_id": task_id, "clip_id": clip_id, "style_id": style_id,
                      "replace_intro": replace_intro, "override": cover_copy_override})
        cover = env["clip_dir"] / "cover.jpg"
        cover.write_bytes(b"NEW-COVER-V2")
        return {
            "kind": "generated", "idempotent": False, "cover_abs_path": str(cover),
            "cover_path": f"output/{task_id}/{clip_id}/cover.jpg",
            "cover_revision": 2, "cover_copy": {"headline_line_1": "胰腺癌早期"},
            "copy_report": {"changed": True, "reason": "主钩子超过 12 字，已压缩"},
            "qc": {"passed": True}, "source": {"frame_ts_in_base": 1.5},
        }

    monkeypatch.setattr(tasks, "replace_cover", fake_replace)
    video_before = (env["clip_dir"] / "clip_final.mp4").read_bytes()

    record = tasks.create_task(
        project_id=env["project_id"], clip_id=env["clip_id"],
        task_type=tasks.TASK_REGENERATE_COVER,
        instruction="重新制作封面，主题不超过 12 个字",
        payload={"style_id": "realframe_v2", "replace_intro": True})
    assert record["task_type"] == "REGENERATE_COVER"
    assert record["task_id"].startswith("cover_")

    done = _wait(record["task_id"])
    assert done["status"] == "completed", done.get("error")
    result = done["result"]
    assert result["cover_revision"] == 2
    assert result["clip_final_unchanged"] is True
    assert result["copy_report"]["changed"] is True
    # 只调用了封面通道一次，且带上了正确的 task_id/clip_id/风格
    assert calls == [{"task_id": env["render_task"], "clip_id": env["clip_id"],
                      "style_id": "realframe_v2", "replace_intro": True, "override": None}]
    # 成片逐字节未变、封面确实换了
    assert (env["clip_dir"] / "clip_final.mp4").read_bytes() == video_before
    assert (env["clip_dir"] / "cover.jpg").read_bytes() == b"NEW-COVER-V2"
    # 台账：封面 revision 推进到 2，卡片仍只有一张、render_task_id 不变
    cards = store.list_cards(env["project_id"])
    assert len(cards) == 1
    assert cards[0]["clip_id"] == env["clip_id"]
    assert cards[0]["cover"]["revision"] == 2
    assert cards[0]["task_id"] == env["render_task"]
    assert cards[0]["task_history"][-1]["task_type"] == "REGENERATE_COVER"


def test_regenerate_cover_propagates_style_error_with_code(env, monkeypatch):
    from src.cover_replace import CoverReplaceError, E_COVER_STYLE_UNAVAILABLE

    def boom(*_args, **_kwargs):
        raise CoverReplaceError(E_COVER_STYLE_UNAVAILABLE,
                                "E_CUTOUT_WEIGHTS_MISSING: 缺少 /domain/.u2net/u2net.onnx，"
                                "请按 docs/COVER_STYLES_SKILL.md 安装权重")

    monkeypatch.setattr(tasks, "replace_cover", boom)
    record = tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                               task_type=tasks.TASK_REGENERATE_COVER,
                               payload={"style_id": "cutout_v3"})
    done = _wait(record["task_id"])
    assert done["status"] == "failed"
    # 缺权重必须显式报错并带安装指引，绝不静默换风格
    assert "E_CUTOUT_WEIGHTS_MISSING" in done["error"]
    assert "安装权重" in done["error"]
    assert store.list_cards(env["project_id"])[0]["cover"]["revision"] == 1


def test_regenerate_cover_is_idempotent_across_repeat_calls(env, monkeypatch):
    def fake_replace(task_id, clip_id, style_id, *, replace_intro=True, cover_copy_override=None):
        cover = env["clip_dir"] / "cover.jpg"
        cover.write_bytes(b"NEW-COVER-V2")
        return {"kind": "generated", "idempotent": True, "cover_abs_path": str(cover),
                "cover_revision": 2, "copy_report": None, "qc": None, "source": {}}

    monkeypatch.setattr(tasks, "replace_cover", fake_replace)
    first = _wait(tasks.create_task(
        project_id=env["project_id"], clip_id=env["clip_id"],
        task_type=tasks.TASK_REGENERATE_COVER)["task_id"])
    assert first["status"] == "completed"
    assert first["result"]["cover_revision"] == 2

    second = _wait(tasks.create_task(
        project_id=env["project_id"], clip_id=env["clip_id"],
        task_type=tasks.TASK_REGENERATE_COVER)["task_id"])
    assert second["status"] == "completed"
    # 幂等：产物逐字节一致 → 版本号**不变**（不能显示「已更新 · v3」而图没变）
    assert second["result"]["cover_revision"] == 2
    assert second["result"]["idempotent"] is True
    card = store.list_cards(env["project_id"])[0]
    assert card["cover"]["revision"] == 2
    assert [h["revision"] for h in card["cover"]["history"]] == [1, 2]
    # 台账里仍然只有一条 clip（不新增卡片）
    assert len(store.list_cards(env["project_id"])) == 1


# ---------------------------------------------------------------------------
# 3. UPDATE_TITLE / UPDATE_SUBTITLE_STYLE / UPDATE_TIMELINE
# ---------------------------------------------------------------------------

def test_update_title_writes_text_without_touching_video(env, monkeypatch):
    monkeypatch.setattr(tasks, "replace_cover", lambda *a, **k: (
        (_ for _ in ()).throw(AssertionError("标题任务不应调用封面生成"))) )
    video_before = (env["clip_dir"] / "clip_final.mp4").read_bytes()

    done = _wait(tasks.create_task(
        project_id=env["project_id"], clip_id=env["clip_id"],
        task_type=tasks.TASK_UPDATE_TITLE, instruction="重新生成这一条成片的标题",
        payload={"refresh_cover": False})["task_id"])

    assert done["status"] == "completed", done.get("error")
    assert done["result"]["video_rerendered"] is False
    card = store.list_cards(env["project_id"])[0]
    assert card["title"] and len(card["title"]) <= 12
    assert card["render_revision"] == 1           # 标题不产生画面重渲
    assert (env["clip_dir"] / "clip_final.mp4").read_bytes() == video_before


def test_update_subtitle_style_writes_config_and_bumps_render_revision(env):
    done = _wait(tasks.create_task(
        project_id=env["project_id"], clip_id=env["clip_id"],
        task_type=tasks.TASK_UPDATE_SUBTITLE_STYLE, instruction="字幕放大一点")["task_id"])

    assert done["status"] == "completed", done.get("error")
    assert done["result"]["config"]["font_scale"] == 1.15
    assert done["result"]["video_rerendered"] is False
    sidecar = env["clip_dir"] / "subtitle_style.json"
    assert sidecar.is_file() and json.loads(sidecar.read_text(encoding="utf-8"))["config"]["font_scale"] == 1.15
    # 只有这一条 clip 的 render_revision 被推进
    assert store.list_cards(env["project_id"])[0]["render_revision"] == 2


def test_update_timeline_removes_requested_span(env):
    done = _wait(tasks.create_task(
        project_id=env["project_id"], clip_id=env["clip_id"],
        task_type=tasks.TASK_UPDATE_TIMELINE, instruction="删除第 8 秒到第 10 秒")["task_id"])

    assert done["status"] == "completed", done.get("error")
    ranges = done["result"]["ranges"]
    assert ranges == [{"start": 0.14, "end": 8.0}, {"start": 10.0, "end": 16.3}]
    assert done["result"]["pending_rerender"] is True
    assert done["result"]["video_rerendered"] is False
    assert (env["clip_dir"] / "clip_timeline.json").is_file()


def test_update_timeline_rejects_out_of_clip_range(env):
    done = _wait(tasks.create_task(
        project_id=env["project_id"], clip_id=env["clip_id"],
        task_type=tasks.TASK_UPDATE_TIMELINE, instruction="改时间线",
        payload={"ranges": [{"start": 0.0, "end": 999.0}]})["task_id"])
    assert done["status"] == "failed"
    assert "E_TIMELINE_RANGE_OUT_OF_CLIP" in done["error"]


def test_update_timeline_touches_only_this_clip(env):
    """多一条成片时，UPDATE_TIMELINE 不得影响另一条。"""
    other = "clip-741ef5-02"
    other_dir = env["root"] / "output" / env["render_task"] / other
    other_dir.mkdir(parents=True)
    (other_dir / "clip_final.mp4").write_bytes(b"OTHER" * 100)
    (other_dir / "cover.jpg").write_bytes(b"OTHER-COVER")
    store.register_clips(env["project_id"], env["render_task"], [{
        "clip_id": other, "name": "剪辑成片 02", "source_start": 20.0, "source_end": 30.0,
        "cover_path": f"output/{env['render_task']}/{other}/cover.jpg",
    }])
    before = json.dumps(store.list_cards(env["project_id"])[1], sort_keys=True)

    _wait(tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                            task_type=tasks.TASK_UPDATE_TIMELINE,
                            instruction="删除第 8 秒到第 10 秒")["task_id"])
    after = json.dumps(store.list_cards(env["project_id"])[1], sort_keys=True)
    assert before == after


# ---------------------------------------------------------------------------
# 4. 任务表 / 日志 / 状态镜像（「只建 REGENERATE_COVER，不是 auto_edit」的证据）
# ---------------------------------------------------------------------------

def test_task_record_status_and_log_all_carry_task_type(env, monkeypatch):
    monkeypatch.setattr(tasks, "replace_cover", lambda *a, **k: {
        "cover_abs_path": str(env["clip_dir"] / "cover.jpg"), "cover_revision": 2,
        "copy_report": None, "qc": None, "source": {}, "idempotent": True})
    record = tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                               task_type=tasks.TASK_REGENERATE_COVER,
                               instruction="重新制作封面")
    _wait(record["task_id"])

    # 任务记录
    stored = json.loads((env["root"] / "localtasks" / f"{record['task_id']}.json")
                        .read_text(encoding="utf-8"))
    assert stored["task_type"] == "REGENERATE_COVER"
    # 状态镜像（与 AUTO_EDIT 同一张任务表）
    mirror = json.loads((env["root"] / "status" / f"{record['task_id']}.json")
                        .read_text(encoding="utf-8"))
    assert mirror["task_type"] == "REGENERATE_COVER"
    assert mirror["status"] == "completed"
    # jsonl 日志
    lines = [json.loads(line) for line in
             (env["root"] / "logs" / "local_tasks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {line["task_type"] for line in lines} == {"REGENERATE_COVER"}
    assert not any(line["task_type"] == "AUTO_EDIT" for line in lines)

    listed = tasks.list_tasks(project_id=env["project_id"])
    assert [item["task_id"] for item in listed] == [record["task_id"]]


def test_list_tasks_filters_by_clip(env, monkeypatch):
    monkeypatch.setattr(tasks, "replace_cover", lambda *a, **k: {
        "cover_abs_path": str(env["clip_dir"] / "cover.jpg"), "cover_revision": 2,
        "copy_report": None, "qc": None, "source": {}, "idempotent": True})
    _wait(tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                            task_type=tasks.TASK_REGENERATE_COVER)["task_id"])
    assert len(tasks.list_tasks(project_id=env["project_id"], clip_id=env["clip_id"])) == 1
    assert tasks.list_tasks(project_id=env["project_id"], clip_id="clip-other") == []


# ---------------------------------------------------------------------------
# 5. 纯函数：封面文案规则 / 字幕缩放 / 时间线解析
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected_total", [
    ("胰腺癌早期有三个信号别再当成胃病拖着", 12),
    ("短期", 4),
])
def test_two_lines_respects_budget(text, expected_total):
    head, tail = tasks._two_lines(text)
    assert len(head) >= 2 and len(tail) >= 2
    assert len(head) + len(tail) <= max(expected_total, 4)


@pytest.mark.parametrize("message,expected", [
    ("字幕放大一点", 1.15), ("字幕小一点", 0.9), ("字幕放大到 1.5 倍", 1.5),
    ("字幕 120%", 1.2), ("把字幕调一下", None),
])
def test_parse_font_scale(message, expected):
    assert tasks._parse_font_scale(message) == expected


@pytest.mark.parametrize("message,expected", [
    ("删除第 8 秒到第 10 秒", [[8.0, 10.0]]),
    ("删掉 3.5 秒到 4 秒", [[3.5, 4.0]]),
    ("删除这一段", []),
    ("删除第 10 秒到第 8 秒", []),
])
def test_parse_remove_ranges(message, expected):
    assert tasks._parse_remove_ranges(message) == expected


def test_subtract_ranges_keeps_only_inside_window():
    ranges = tasks._subtract_ranges(0.14, 16.3, [[8.0, 10.0]])
    assert ranges == [{"start": 0.14, "end": 8.0}, {"start": 10.0, "end": 16.3}]
    assert tasks._subtract_ranges(0.14, 16.3, [[-5, 99]]) == []


def test_list_tasks_includes_auto_edit_tasks_without_project_id_in_status(env):
    """旧任务状态文件没写 project_id 时，回退读任务 JSON 的 bridge.project_id。"""
    (env["root"] / "status").mkdir(parents=True, exist_ok=True)
    (env["root"] / "completed").mkdir(parents=True, exist_ok=True)
    (env["root"] / "completed" / "autoedit_ffffffffffff.json").write_text(json.dumps({
        "task_id": "autoedit_ffffffffffff",
        "bridge": {"project_id": env["project_id"]},
    }), encoding="utf-8")
    (env["root"] / "status" / "autoedit_ffffffffffff.json").write_text(json.dumps({
        "task_id": "autoedit_ffffffffffff", "task_type": "AUTO_EDIT",
        "status": "completed", "created_at": "2026-10-12T00:00:00Z",
    }), encoding="utf-8")

    listed = tasks.list_tasks(project_id=env["project_id"])
    autoedit = [item for item in listed if item["task_id"] == "autoedit_ffffffffffff"]
    assert len(autoedit) == 1
    assert autoedit[0]["project_id"] == env["project_id"]
    assert autoedit[0]["task_type"] == "AUTO_EDIT"


# ---------------------------------------------------------------------------
# 6. Inspector Phase 1 新类型：UPDATE_SUBTITLE / UPDATE_TRANSFORM /
#    UPDATE_AUDIO / UPDATE_EFFECTS / RENDER_CLIP（契约 §4）
#
# 每个处理器「一个 happy path + 一个非法输入」；非法输入必须保真 E_ 码
# （bad transform / 坏枚举 / 越界音量 / 空 cue）。RENDER_CLIP 另证「只登记不 bump」。
# ---------------------------------------------------------------------------

from src import studio_auto_edit_bridge as bridge        # noqa: E402
from src import transcript_revisions                     # noqa: E402


def _no_autoedit_files(root) -> list[str]:
    """任务定义/状态/产物层面都不该出现 autoedit 文件（除既有成片目录外）。"""
    return [path.name for path in root.rglob("autoedit*") if path.is_file()]


@pytest.fixture
def p1_env(env, monkeypatch):
    """新类型专用环境：额外打桩桥接层根目录，并准备一份转写基线（两段）。"""
    monkeypatch.setattr(bridge, "TASKS_ROOT", env["root"])
    transcript_revisions.seed_document(env["project_id"], env["clip_id"], raw_segments=[
        {"segment_id": "seg-1", "start": 0.0, "end": 1.5,
         "text": "胰腺癌早期信号", "speaker": "医生"},
        {"segment_id": "seg-2", "start": 1.5, "end": 3.0,
         "text": "别当成胃病", "speaker": "医生"},
    ])
    return env


def _entry(env) -> dict:
    return store.entry_for(env["project_id"], env["clip_id"])


def _run(env, task_type: str, *, instruction: str | None = None,
         payload: dict | None = None) -> dict:
    return _wait(tasks.create_task(
        project_id=env["project_id"], clip_id=env["clip_id"], task_type=task_type,
        instruction=instruction, payload=payload or {})["task_id"])


# ---- UPDATE_SUBTITLE ------------------------------------------------------

def test_update_subtitle_writes_config_and_creates_ass_revision(p1_env):
    done = _run(p1_env, tasks.TASK_UPDATE_SUBTITLE,
                instruction="把第一句字幕改一下",
                payload={"config": {"font_size_px": 72},
                         "cues": [{"segment_id": "seg-1", "start": 0.4, "end": 1.6,
                                   "text": "胰腺癌的早期信号"}]})

    assert done["status"] == "completed", done.get("error")
    result = done["result"]
    assert result["revision"] == 1
    assert result["cue_count"] == 2                       # 两段转写（其中一段被覆盖）
    assert result["config"]["font_size_px"] == 72
    assert result["export_burn_in"] == "phase2"           # 契约：不再返回适配器缺口
    assert "SUBTITLE_ADAPTER_GAP" not in json.dumps(result, ensure_ascii=False)
    assert "phase2" in json.dumps(result, ensure_ascii=False)
    # 新版本真的落在该 clip 的 subtitles/rev-001/ 下
    assert result["ass_rel"] == "subtitles/rev-001/clip-741ef5-01.ass"
    assert result["srt_rel"] == "subtitles/rev-001/clip-741ef5-01.srt"
    from pathlib import Path
    assert Path(result["ass_path"]).is_file() and Path(result["srt_path"]).is_file()
    assert "胰腺癌的早期信号" in Path(result["ass_path"]).read_text(encoding="utf-8")

    entry = _entry(p1_env)
    assert entry["subtitle_config"]["font_size_px"] == 72
    assert entry["subtitle_overrides"][0]["text"] == "胰腺癌的早期信号"
    assert entry["render_revision"] == 2                  # 恰好推进一版
    history = entry["task_history"]
    assert history[-1]["task_type"] == "UPDATE_SUBTITLE"
    assert "字幕" in history[-1]["summary"]


def test_update_subtitle_recomposes_from_transcript_when_no_cues(p1_env):
    done = _run(p1_env, tasks.TASK_UPDATE_SUBTITLE, payload={"config": {"outline_width": 6}})
    assert done["status"] == "completed", done.get("error")
    assert done["result"]["cue_count"] == 2
    assert done["result"]["overrides"] == []


def test_update_subtitle_without_any_cue_fails_and_writes_nothing(env):
    """空 cue + 没有转写基线 → E_SUBTITLE_NO_CUES，且台账不留半成品。"""
    done = _run(env, tasks.TASK_UPDATE_SUBTITLE, payload={"cues": []})

    assert done["status"] == "failed"
    assert "E_SUBTITLE_NO_CUES" in done["error"]
    entry = _entry(env)
    assert "subtitle_config" not in entry
    assert "subtitle_overrides" not in entry
    assert entry["render_revision"] == 1
    assert not (env["clip_dir"] / "subtitles").exists()


def test_update_subtitle_rejects_bad_style_value(env):
    done = _run(env, tasks.TASK_UPDATE_SUBTITLE,
                payload={"config": {"font_size_px": 9999},
                         "cues": [{"segment_id": "seg-1", "text": "一段", "start": 0.0,
                                   "end": 1.0}]})
    assert done["status"] == "failed"
    assert "E_CONFIG_INVALID" in done["error"]


# ---- UPDATE_TRANSFORM -----------------------------------------------------

def test_update_transform_writes_clip_and_sidecar(p1_env):
    done = _run(p1_env, tasks.TASK_UPDATE_TRANSFORM, instruction="画面放大一点",
                payload={"transform": {"scale": 1.25, "positionX": 10.0, "positionY": -6.0,
                                       "fitMode": "cover"}})

    assert done["status"] == "completed", done.get("error")
    result = done["result"]
    assert result["applied"] is True
    assert result["transform"]["scale"] == 1.25
    assert result["mirror"] == "synced"
    assert result["export_transform"] == "phase2"
    # 既有 sidecar 镜像 + 台账条目都写到了
    saved = bridge.read_clip_transform(p1_env["render_task"], p1_env["clip_id"])
    assert saved["scale"] == 1.25 and saved["fitMode"] == "cover"
    entry = _entry(p1_env)
    assert entry["transform"]["scale"] == 1.25
    assert entry["render_revision"] == 2
    assert (p1_env["root"] / "output" / p1_env["render_task"]
            / "clip_transforms.json").is_file()


def test_update_transform_null_clears_the_transform(p1_env):
    _run(p1_env, tasks.TASK_UPDATE_TRANSFORM,
         payload={"transform": {"scale": 1.4, "fitMode": "cover"}})
    done = _run(p1_env, tasks.TASK_UPDATE_TRANSFORM, payload={"transform": None})
    assert done["status"] == "completed", done.get("error")
    assert done["result"]["transform"] is None
    assert _entry(p1_env)["transform"] is None


def test_update_transform_rejects_bad_payload(env):
    done = _run(env, tasks.TASK_UPDATE_TRANSFORM,
                payload={"transform": {"fitMode": "zoom"}})
    assert done["status"] == "failed"
    assert "E_CONFIG_INVALID" in done["error"]
    assert "transform" in done["error"]
    assert "transform" not in _entry(env)
    assert _entry(env)["render_revision"] == 1


def test_update_transform_rejects_non_object_transform(env):
    done = _run(env, tasks.TASK_UPDATE_TRANSFORM, payload={"transform": "zoom-in"})
    assert done["status"] == "failed"
    assert "E_CONFIG_INVALID" in done["error"]


# ---- UPDATE_AUDIO ---------------------------------------------------------

def test_update_audio_writes_volume_mute_and_tracks(env):
    track = {"id": "bgm-abc12345", "asset_id": "bgm-abc12345", "kind": "bgm",
             "path": "audio/bgm-abc12345.mp3", "filename": "bgm.mp3", "volume": 0.3}
    done = _run(env, tasks.TASK_UPDATE_AUDIO, instruction="原声小一点，加背景音乐",
                payload={"voice_volume": 0.5, "muted": False, "audio_tracks": [track]})

    assert done["status"] == "completed", done.get("error")
    result = done["result"]
    assert result["voice_volume"] == 0.5
    assert result["muted"] is False
    assert result["audio_tracks"][0]["asset_id"] == "bgm-abc12345"
    assert result["audio_tracks"][0]["volume"] == 0.3
    assert result["export_mix"] == "phase2"
    entry = _entry(env)
    assert entry["voice_volume"] == 0.5 and entry["muted"] is False
    assert entry["audio_tracks"][0]["asset_id"] == "bgm-abc12345"
    assert entry["render_revision"] == 2


def test_update_audio_rejects_out_of_range_volume(env):
    done = _run(env, tasks.TASK_UPDATE_AUDIO, payload={"voice_volume": 2.5})
    assert done["status"] == "failed"
    assert "E_CONFIG_INVALID" in done["error"]
    assert "voice_volume" not in _entry(env)
    assert _entry(env)["render_revision"] == 1


def test_update_audio_rejects_bad_track_structure(env):
    done = _run(env, tasks.TASK_UPDATE_AUDIO, payload={"audio_tracks": [{"volume": 2.0}]})
    assert done["status"] == "failed"
    assert "E_CONFIG_INVALID" in done["error"]


# ---- UPDATE_EFFECTS -------------------------------------------------------

def test_update_effects_writes_effects(p1_env):
    done = _run(p1_env, tasks.TASK_UPDATE_EFFECTS, instruction="右上角加贴纸，加暖色滤镜",
                payload={"effects": {"sticker": {"enabled": True, "position": "right"},
                                     "filter": {"name": "warm", "strength": 0.5},
                                     "export_resolution": "1080x1080"}})

    assert done["status"] == "completed", done.get("error")
    effects = done["result"]["effects"]
    assert effects["sticker"] == {"enabled": True, "position": "right"}
    assert effects["filter"]["name"] == "warm" and effects["filter"]["strength"] == 0.5
    assert effects["export_resolution"] == "1080x1080"
    assert effects["end_card"]["enabled"] is False           # 未提到的段保持默认
    assert done["result"]["export_rendering"] == "phase2"
    assert _entry(p1_env)["effects"]["export_resolution"] == "1080x1080"
    assert _entry(p1_env)["render_revision"] == 2


def test_update_effects_rejects_bad_enum(env):
    done = _run(env, tasks.TASK_UPDATE_EFFECTS,
                payload={"effects": {"filter": {"name": "sepia"}}})
    assert done["status"] == "failed"
    assert "E_CONFIG_INVALID" in done["error"]
    assert "effects" not in _entry(env)


# ---- RENDER_CLIP ----------------------------------------------------------

def test_render_clip_defers_and_does_not_bump_render_revision(env):
    before = _entry(env)["render_revision"]
    assert before == 1

    done = _run(env, tasks.TASK_RENDER_CLIP, instruction="渲染这一条成片")

    assert done["status"] == "completed", done.get("error")
    result = done["result"]
    assert result["render_deferred"] is True
    assert result["reason"] == "需 Phase 2 渲染流程支持"
    assert result["render_revision_bumped"] is False
    assert result["render_revision"] == before
    assert result["video_rerendered"] is False
    # 摘要：每项生效值 + 来源（供 UI 展示「点渲染会发生什么」）
    summary = result["render_config"]
    assert summary["export"]["deferred"] is True
    assert summary["export"]["resolution"] == "1080x1920"
    assert {item["key"] for item in summary["effective"]}
    assert all(item["source"] in {"user", "project", "skill_default"}
               for item in summary["effective"])

    entry = _entry(env)
    assert entry["render_revision"] == before                  # 没有 bump
    assert entry["render_request"]["reason"] == "需 Phase 2 渲染流程支持"
    assert entry["render_request"]["task_id"] == done["task_id"]
    assert entry["task_history"][-1]["task_type"] == "RENDER_CLIP"
    assert "未推进 render_revision" in entry["task_history"][-1]["summary"]
    # 没建任何 autoedit 任务/产物（成片目录是本条 clip 自己的）
    assert _no_autoedit_files(env["root"]) == []


# ---- 摘要（中文可读）------------------------------------------------------

@pytest.mark.parametrize("task_type,result,fragment", [
    (tasks.TASK_UPDATE_SUBTITLE, {"revision": 3, "cue_count": 12}, "rev-3"),
    (tasks.TASK_UPDATE_TRANSFORM, {"mirror": "synced"}, "画面变换已应用"),
    (tasks.TASK_UPDATE_AUDIO, {"muted": True, "audio_tracks": [{}]}, "已静音"),
    (tasks.TASK_UPDATE_EFFECTS, {"effects": {"export_resolution": "1080x1080"}}, "1080x1080"),
    (tasks.TASK_RENDER_CLIP, {"reason": "需 Phase 2 渲染流程支持", "render_revision": 2},
     "需 Phase 2 渲染流程支持"),
])
def test_summarise_covers_the_new_local_types(task_type, result, fragment):
    text = tasks._summarise({"task_type": task_type, "status": "completed", "result": result})
    assert fragment in text
    assert text


def test_new_local_types_touch_only_this_clip(p1_env, monkeypatch):
    """五个新类型都只改本条 clip：另一条成片的台账/资产逐字节不变。"""
    other = "clip-741ef5-02"
    other_dir = p1_env["root"] / "output" / p1_env["render_task"] / other
    other_dir.mkdir(parents=True)
    (other_dir / "clip_final.mp4").write_bytes(b"OTHER" * 100)
    (other_dir / "cover.jpg").write_bytes(b"OTHER-COVER")
    store.register_clips(p1_env["project_id"], p1_env["render_task"], [{
        "clip_id": other, "name": "剪辑成片 02", "source_start": 20.0, "source_end": 30.0,
        "cover_path": f"output/{p1_env['render_task']}/{other}/cover.jpg",
    }])
    before = json.dumps(store.list_cards(p1_env["project_id"])[1], sort_keys=True)

    _run(p1_env, tasks.TASK_UPDATE_SUBTITLE,
         payload={"cues": [{"segment_id": "seg-1", "start": 0.0, "end": 1.0, "text": "改一句"}]})
    _run(p1_env, tasks.TASK_UPDATE_TRANSFORM,
         payload={"transform": {"scale": 1.3, "fitMode": "cover"}})
    _run(p1_env, tasks.TASK_UPDATE_AUDIO, payload={"voice_volume": 0.4})
    _run(p1_env, tasks.TASK_UPDATE_EFFECTS,
         payload={"effects": {"filter": {"name": "cool", "strength": 0.2}}})
    _run(p1_env, tasks.TASK_RENDER_CLIP)

    assert json.dumps(store.list_cards(p1_env["project_id"])[1], sort_keys=True) == before
    # 只写本条 clip 的资产：另一条的目录里没有 subtitles/，sidecar 里也没有它的变换
    assert not (other_dir / "subtitles").exists()
    assert bridge.read_clip_transform(p1_env["render_task"], other) is None
    assert bridge.read_clip_transform(p1_env["render_task"], p1_env["clip_id"]) is not None


# ---------------------------------------------------------------------------
# APPLY_COVER（2026-10-21）：封面「应用」= 真实渲染任务
# ---------------------------------------------------------------------------

def _write_pending_draft(env, *, applied: bool = False, revision: int = 2,
                         style: str = "template:cute") -> None:
    """在 clip 目录里写一份版型封面草稿状态（cover_replace 的唯一事实源）。"""
    from src import cover_replace as cr
    state = cr._load_state(env["clip_dir"])
    state.setdefault("baseline", {
        "cover_jpg": None, "clip_final": "clip_final.mp4",
        "sha256": {"cover_jpg": None, "clip_final": None},
    })
    state["template_pending"] = {"revision": revision, "style": style,
                                 "applied": applied, "updated_at": "t"}
    cr._atomic_json(cr._state_path(env["clip_dir"]), state)


def test_apply_cover_requires_pending_draft(env):
    done = _wait(tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                                   task_type=tasks.TASK_APPLY_COVER)["task_id"])
    assert done["status"] == "failed"
    assert "E_COVER_DRAFT_MISSING" in str(done.get("error"))


def test_apply_cover_renders_intro_and_marks_applied(env, monkeypatch):
    _write_pending_draft(env)
    calls: list[tuple[str, str]] = []

    seen_previous: list[float] = []

    def fake_apply(task_id: str, clip_id: str, **kwargs) -> dict:
        from src import cover_replace as cr
        seen_previous.append(float(kwargs.get("previous_intro_s") or 0.0))
        calls.append((task_id, clip_id))
        state = cr._load_state(env["clip_dir"])
        state["template_pending"]["applied"] = True
        cr._atomic_json(cr._state_path(env["clip_dir"]), state)
        return {"applied": True, "revision": 2, "qc": {"passed": True}}

    monkeypatch.setattr(tasks, "apply_template_cover", fake_apply)
    done = _wait(tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                                   task_type=tasks.TASK_APPLY_COVER)["task_id"])
    assert done["status"] == "completed", done.get("error")
    assert calls == [(env["render_task"], env["clip_id"])]
    assert seen_previous == [0.0]        # 首次应用没有旧片头
    result = done["result"]
    assert result["applied"] is True and result["idempotent"] is False
    assert result["intro_duration_s"] == pytest.approx(0.8)
    # 台账：这份封面已登记进封面历史，且标记成片已烧片头
    card = result["card"]
    assert card["cover"]["intro_burned"] is True
    assert card["cover_workflow"]["applied"] is True
    assert card["cover_workflow"]["pending"] is False
    # 任务历史里能看到 APPLY_COVER（可审计），且状态镜像一致
    entry = store.entry_for(env["project_id"], env["clip_id"])
    types = [h.get("task_type") for h in entry.get("task_history") or []]
    assert tasks.TASK_APPLY_COVER in types


def test_apply_cover_is_idempotent_when_already_applied(env, monkeypatch):
    _write_pending_draft(env, applied=True)
    monkeypatch.setattr(tasks, "apply_template_cover",
                        lambda *a, **k: pytest.fail("已应用过就不允许再次渲染"))
    done = _wait(tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                                   task_type=tasks.TASK_APPLY_COVER)["task_id"])
    assert done["status"] == "completed", done.get("error")
    assert done["result"]["idempotent"] is True
    assert done["result"]["clip_final_unchanged"] is True


def test_duplicate_apply_clicks_reuse_the_same_task(env):
    _write_pending_draft(env)
    first = tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                              task_type=tasks.TASK_APPLY_COVER, run=False)
    again = tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                              task_type=tasks.TASK_APPLY_COVER, run=False)
    assert again["task_id"] == first["task_id"]          # 排队中的任务直接复用
    assert len([t for t in tasks.list_tasks(project_id=env["project_id"], clip_id=env["clip_id"])
                if t.get("task_type") == tasks.TASK_APPLY_COVER]) == 1
    # run=False 不执行 → 手动释放配额预留（正常路径由 _submit 的 finally 释放）
    from src.studio_access import release_operation
    release_operation(first["task_id"])


def test_apply_cover_failure_keeps_original_clip_and_reports_reason(env, monkeypatch):
    _write_pending_draft(env)
    original = (env["clip_dir"] / "clip_final.mp4").read_bytes()

    def boom(*_a, **_k):
        from src.cover_replace import CoverReplaceError
        raise CoverReplaceError("E_COVER_QC_FAILED", "尾部校验不一致")

    monkeypatch.setattr(tasks, "apply_template_cover", boom)
    done = _wait(tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                                   task_type=tasks.TASK_APPLY_COVER)["task_id"])
    assert done["status"] == "failed"
    assert "E_COVER_QC_FAILED" in str(done.get("error"))
    assert (env["clip_dir"] / "clip_final.mp4").read_bytes() == original
    # 草稿仍是待应用：用户可以重试
    card = store.card_view(store.entry_for(env["project_id"], env["clip_id"]))
    assert card["cover_workflow"]["pending"] is True


def test_apply_cover_rejects_clip_from_other_project(env):
    with pytest.raises(store.ProjectStoreError):
        tasks.create_task(project_id="timeline_other99999", clip_id=env["clip_id"],
                          task_type=tasks.TASK_APPLY_COVER, run=False)
