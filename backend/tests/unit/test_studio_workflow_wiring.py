"""Phase B 接线测试：规则注册表 + Skill 适配器 → 自动剪辑 / 局部任务 / 台账 / 规则中心端点。

覆盖任务书的五个验收点（纯逻辑，不联网、不跑 ffmpeg、不调 video-brain）：
1. 任务级规则快照落到**任务自己的目录**，版本字段写进任务 JSON / 状态镜像 / 台账；
2. AUTO_EDIT 提交时把快照摘要挂进提交给 video-brain 的载荷（执行器透传）；
3. 局部任务（封面/标题/字幕/时间线）的规则来源是**冻结快照**，且经 Skill 层解析；
4. 规则中心端点**每次实时读清单**；启停 skill 有备份 + 校验 + 回滚；
5. 「按最新规则重新生成」只新增 revision、不新增卡片。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src import skill_registry as registry
from src import skills, studio_auto_edit_bridge as bridge, studio_local_tasks as tasks
from src import studio_project_store as store
from src import studio_workflow
from src.api.routes import media


def _sha16(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _manifest(tmp_path: Path, *, cover_style: str = "realframe_v2",
              forbid: list[str] | None = None, single_skill: bool = False) -> Path:
    """构造一份自洽的清单（真实文件 + 真实校验和），并让 build_workflow_snapshot 用它。"""
    cover_file = tmp_path / "cover_styles.json"
    cover_file.write_text('{"default_style_id": "%s"}' % cover_style, encoding="utf-8")
    plan_file = tmp_path / "planning.py"
    plan_file.write_text("# planning prompt\n", encoding="utf-8")
    entries = [
        {
            "skill_id": "cover", "version": "v1", "source_path": str(cover_file),
            "purpose": "test", "priority": 20, "enabled": True, "checksum": _sha16(cover_file),
            "input_schema": {"type": "object", "required": ["clip_id"]},
            "output_schema": {"type": "object", "required": ["cover_jpg"]},
        },
        {
            "skill_id": "clip_planning", "version": "v2", "source_path": str(plan_file),
            "purpose": "test", "priority": 10, "enabled": True, "checksum": _sha16(plan_file),
            "input_schema": {"type": "object", "required": ["segments"]},
            "output_schema": {"type": "object", "required": ["clips"]},
        },
        {
            "skill_id": "render", "version": "v1", "source_path": str(plan_file),
            "purpose": "test", "priority": 30, "enabled": True, "checksum": _sha16(plan_file),
            "input_schema": {"type": "object"}, "output_schema": {"type": "object"},
        },
    ]
    if single_skill:
        entries = entries[:1]
    document = {
        "workflow_version": "test-1",
        "enabled": True,
        "updated_at": "2026-10-12T00:00:00Z",
        "skills": entries,
        "cover_rules": {"skill_id": "cover", "default_style_id": cover_style,
                        "intro_duration_s": 0.8},
        "title_rules": {"skill_id": "clip_planning", "must_be_grounded_in_transcript": True,
                        "forbidden": forbid if forbid is not None else ["疗效承诺", "绝对化用语"]},
        "subtitle_rules": {"skill_id": None, "band": "bottom", "burn_in": True},
        "clip_planning_rules": {"duration_range_default": [25, 45], "llm_enabled": True},
        "render_rules": {"resolution": "1080x1920", "platform": "douyin"},
        "qa_rules": {"fail_cation": "reject_clip"},
    }
    path = tmp_path / "editing-workflow.manifest.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    """临时任务根 + 临时清单 + 一条已登记成片 + 一个可提交 AUTO_EDIT 的项目。"""
    root = tmp_path / "tasks"
    manifest = _manifest(tmp_path)
    monkeypatch.setenv("EDITING_WORKFLOW_MANIFEST", str(manifest))
    # **必须**同时改桥接模块的任务根：否则 submit_auto_edit_task 会把测试任务写进真实队列，
    # 被真机执行器领取（这正是本轮接线测试要避免的副作用）。
    for module in (studio_workflow, tasks, store, media, bridge):
        monkeypatch.setattr(module, "TASKS_ROOT", root, raising=False)

    project_id = "timeline_abc123def456"
    # W07资源准入也必须隔离；后台线程从ContextVar默认值取得同一合成账本。
    from contextvars import ContextVar
    from src import studio_access
    acl=studio_access.AccessLedger(tmp_path/"access.sqlite3")
    principal={"subject":"workflow-test-owner","domain_id":studio_access.DOMAIN_ID,"expires_at_ms":9999999999999}
    acl.register("project",project_id,principal)
    monkeypatch.setattr(studio_access,"_access",ContextVar("workflow_test_access",default=acl))
    monkeypatch.setattr(studio_access,"_principal",ContextVar("workflow_test_principal",default=principal))
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

    # 可提交 AUTO_EDIT 的项目（project.json + raw/ 源视频 + 转写）
    uploads = tmp_path / "uploads"
    project_dir = uploads / "studio-projects" / project_id
    (project_dir / "raw").mkdir(parents=True)
    (project_dir / "project.json").write_text("{}", encoding="utf-8")
    (project_dir / "raw" / "source.mp4").write_bytes(b"SOURCE-BYTES")
    return {
        "root": root, "manifest": manifest, "uploads": uploads, "project_id": project_id,
        "render_task": render_task, "clip_id": clip_id, "clip_dir": clip_dir, "tmp": tmp_path,
    }


def _wait(task_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = tasks.get_task(task_id)
        if record.get("status") in {"completed", "failed"}:
            return record
        time.sleep(0.02)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内结束")


def _wire_render_task(env: dict, *, text: str = "胰腺癌早期信号要重视") -> None:
    """把 fixture 里那条成片的 AUTO_EDIT 产物补全（status + 任务 JSON + clip_plan）。

    ``read_auto_edit_task`` 需要 status 文件与 clip_plan.json 才能还原该 clip 的转写分段，
    局部任务（重新选段 / 重渲）据此取转写窗口。
    """
    root, render_task, clip_id = env["root"], env["render_task"], env["clip_id"]
    (root / "status").mkdir(parents=True, exist_ok=True)
    (root / "completed").mkdir(parents=True, exist_ok=True)
    (root / "status" / f"{render_task}.json").write_text(json.dumps({
        "task_id": render_task, "task_type": "AUTO_EDIT", "status": "completed",
        "project_id": env["project_id"], "artifacts": [],
    }), encoding="utf-8")
    (root / "completed" / f"{render_task}.json").write_text(json.dumps({
        "task_id": render_task, "raw_video_path": "media/source.mp4",
        "asr_segments": [{"start": 0.0, "end": 16.3, "text": text}],
        "bridge": {"project_id": env["project_id"]},
    }), encoding="utf-8")
    (root / "output" / render_task / "clip_plan.json").write_text(json.dumps({
        "engine": "medical-video-production",
        "video_brain_task_id": "task-test-1",
        "highlights": [{"clip_id": clip_id, "start": 0.14, "end": 16.3, "topic": "健康科普"}],
    }), encoding="utf-8")
    (root / "media").mkdir(parents=True, exist_ok=True)
    (root / "media" / "source.mp4").write_bytes(b"SOURCE-BYTES")


# ---------------------------------------------------------------------------
# 1. AUTO_EDIT：任务 JSON / 状态镜像 / 载荷都带规则版本
# ---------------------------------------------------------------------------

def test_runner_import_mode_works():
    """回归锁：执行器以**顶层模块**方式导入（``backend/src`` 在 sys.path 上）必须成功。

    真实故障：``studio_project_store`` / ``studio_workflow`` 一旦使用包相对导入，
    执行器侧的 ``from studio_project_store import register_clips`` 会 ImportError，
    于是「规则版本写台账」整段静默失败 —— 成片照样产出，但台账里没有版本。
    """
    src = Path(__file__).resolve().parents[2] / "src"
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(src)!r})\n"
        "import studio_project_store as store\n"
        "import studio_workflow as workflow\n"
        "assert callable(store.register_clips)\n"
        "assert callable(workflow.versions_for_root)\n"
        "assert workflow.versions_for_root('/nonexistent', 'autoedit_aaaaaaaaaaaa') == {}\n"
        "print('runner-mode-ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "runner-mode-ok" in result.stdout


def test_runner_status_update_preserves_version_fields(tmp_path, monkeypatch):
    """执行器刷新状态时**不得**抹掉提交阶段写入的规则版本（否则任务详情 API 读不到版本）。"""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "src"))
    from src import autoedit_runner_video_brain as runner

    monkeypatch.setattr(runner, "TASKS_ROOT", tmp_path)
    task_id = "autoedit_abcdef012345"
    status_path = tmp_path / "status" / f"{task_id}.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps({
        "task_id": task_id, "task_type": "AUTO_EDIT", "status": "queued", "stage": "排队",
        "workflow_version": "test-1", "skill_versions": {"cover": "v1"},
        "workflow_snapshot_path": "/x/workflow_snapshot.json",
    }), encoding="utf-8")

    runner._update_status(task_id, status="processing", stage="处理中", progress=0.5,
                          created_at="2026-10-12T00:00:00Z", project_id="timeline_x")
    document = json.loads(status_path.read_text(encoding="utf-8"))
    assert document["status"] == "processing" and document["progress"] == 0.5
    assert document["workflow_version"] == "test-1"
    assert document["skill_versions"] == {"cover": "v1"}
    assert document["workflow_snapshot_path"] == "/x/workflow_snapshot.json"


def test_auto_edit_submit_freezes_versions_into_task_and_status(env):
    view = bridge.submit_auto_edit_task(
        project_id=env["project_id"], uploads_root=env["uploads"],
        asr_segments=[{"start": 0.0, "end": 3.0, "text": "这是一句转写"}],
    )
    task_id = view["task_id"]
    task_json = json.loads((env["root"] / "in" / f"{task_id}.json").read_text(encoding="utf-8"))
    status = json.loads((env["root"] / "status" / f"{task_id}.json").read_text(encoding="utf-8"))

    for document in (task_json, status):
        assert document["workflow_version"] == "test-1"
        assert document["skill_versions"] == {"clip_planning": "v2", "cover": "v1", "render": "v1"}
        assert document["workflow_snapshot_path"].endswith("workflow_snapshot.json")
    # 快照落在**该任务自己的目录**（output/{task_id}）
    assert Path(task_json["workflow_snapshot_path"]) == (
        env["root"] / "output" / task_id / registry.SNAPSHOT_FILENAME)
    # 提交给 video-brain 的载荷摘要（执行器透传；video-brain 忽略未知字段）
    assert task_json["workflow_snapshot"]["workflow_version"] == "test-1"
    assert task_json["workflow_snapshot"]["rules"]["cover_rules"]["default_style_id"] == "realframe_v2"
    assert view["workflow_version"] == "test-1"


def test_auto_edit_snapshot_is_not_listed_as_artifact(env):
    view = bridge.submit_auto_edit_task(
        project_id=env["project_id"], uploads_root=env["uploads"],
        asr_segments=[{"start": 0.0, "end": 3.0, "text": "这是一句转写"}],
    )
    names = [item["name"] for item in view["artifacts"]]
    assert registry.SNAPSHOT_FILENAME not in names  # 产物清单与接线前一致


# ---------------------------------------------------------------------------
# 2. 台账：版本字段 + AUTO_EDIT 任务历史
# ---------------------------------------------------------------------------

def test_register_clips_records_versions_and_auto_edit_history(env):
    task_id = env["render_task"]
    studio_workflow.build_task_snapshot(
        task_id, snapshot_dir=studio_workflow.snapshot_dir(env["root"], task_id))
    manifest = store.register_clips(env["project_id"], task_id, [{
        "clip_id": env["clip_id"], "title": "标题", "source_start": 1.0, "source_end": 5.0,
        "cover_path": f"output/{task_id}/{env['clip_id']}/cover.jpg",
    }])
    entry = manifest["clips"][env["clip_id"]]
    assert entry["workflow_version"] == "test-1"
    assert entry["skill_versions"]["cover"] == "v1"
    assert entry["workflow_snapshot_path"].endswith("workflow_snapshot.json")
    history = entry["task_history"]
    assert [item["task_type"] for item in history] == ["AUTO_EDIT"]
    assert history[0]["workflow_version"] == "test-1"
    # 幂等：重复登记不重复追加历史
    store.register_clips(env["project_id"], task_id, [{"clip_id": env["clip_id"]}])
    assert len(store.load_manifest(env["project_id"])["clips"][env["clip_id"]]["task_history"]) == 1
    # 卡片原样透出版本字段
    card = store.list_cards(env["project_id"])[0]
    assert card["workflow_version"] == "test-1"
    assert card["skill_versions"]["render"] == "v1"
    assert card["workflow_snapshot_path"] == entry["workflow_snapshot_path"]


def test_register_clips_without_snapshot_keeps_legacy_shape(env):
    """Phase B 之前的旧任务没有快照 → 不写版本字段、也不凭空造历史。"""
    manifest = store.register_clips(env["project_id"], "autoedit_ffffffffffff", [{
        "clip_id": "clip-legacy", "source_start": 0.0, "source_end": 3.0,
    }])
    entry = manifest["clips"]["clip-legacy"]
    assert "workflow_version" not in entry
    assert entry["task_history"] == []
    card = store.card_view(entry)
    assert card["workflow_version"] is None and card["skill_versions"] == {}


# ---------------------------------------------------------------------------
# 3. 局部任务：快照 + 规则来源 + 版本落库
# ---------------------------------------------------------------------------

def test_local_task_freezes_snapshot_and_versions_reach_ledger(env, monkeypatch):
    monkeypatch.setattr(tasks, "replace_cover", lambda *a, **k: {
        "cover_abs_path": str(env["clip_dir"] / "cover.jpg"), "cover_revision": 2,
        "copy_report": None, "qc": None, "source": {}, "idempotent": True})
    record = tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                               task_type=tasks.TASK_REGENERATE_COVER)
    assert record["workflow_version"] == "test-1"
    assert Path(record["workflow_snapshot_path"]).is_file()
    assert Path(record["workflow_snapshot_path"]).parent == (
        env["root"] / "localtasks" / record["task_id"])
    done = _wait(record["task_id"])
    assert done["status"] == "completed", done.get("error")

    # 状态镜像与任务记录都带版本
    status = json.loads((env["root"] / "status" / f"{record['task_id']}.json")
                        .read_text(encoding="utf-8"))
    assert status["workflow_version"] == "test-1"
    assert status["skill_versions"]["cover"] == "v1"
    # 台账 task_history 也带版本（规则溯源）。
    # 注意：任务状态先落盘、台账历史随后写入（既有约定），因此这里轮询等待，避免竞态。
    history: list[dict] = []
    deadline = time.time() + 5
    while time.time() < deadline:
        history = store.list_cards(env["project_id"])[0]["task_history"]
        if history:
            break
        time.sleep(0.02)
    assert history[-1]["task_type"] == "REGENERATE_COVER"
    assert history[-1]["workflow_version"] == "test-1"
    assert history[-1]["skill_versions"]["cover"] == "v1"
    assert history[-1]["workflow_snapshot_path"] == record["workflow_snapshot_path"]
    # clip 条目本身也带上版本 → 卡片/成片详情显示「使用规则版本」。
    card = store.list_cards(env["project_id"])[0]
    assert card["workflow_version"] == "test-1"
    assert card["skill_versions"]["cover"] == "v1"


def test_cover_style_comes_from_snapshot_and_user_instruction_wins(env, monkeypatch):
    calls: list[dict] = []

    def fake_replace(task_id, clip_id, style_id, *, replace_intro=True, cover_copy_override=None):
        calls.append({"style_id": style_id})
        return {"cover_abs_path": str(env["clip_dir"] / "cover.jpg"), "cover_revision": 2,
                "copy_report": None, "qc": None, "source": {}, "idempotent": False}

    monkeypatch.setattr(tasks, "replace_cover", fake_replace)
    record = tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                               task_type=tasks.TASK_REGENERATE_COVER, run=False)
    result = tasks._HANDLERS[tasks.TASK_REGENERATE_COVER](record)
    assert calls[-1]["style_id"] == "realframe_v2"          # 来自清单 cover_rules
    assert result["rules_source"]["cover_rules.default_style_id"] == registry.SOURCE_SKILL
    assert result["skills"]["cover"] == "v1"

    override = tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                                 task_type=tasks.TASK_REGENERATE_COVER,
                                 payload={"style_id": "cutout_v3"}, run=False)
    overridden = tasks._HANDLERS[tasks.TASK_REGENERATE_COVER](override)
    assert calls[-1]["style_id"] == "cutout_v3"             # 用户指令优先
    assert overridden["rules_source"]["cover_rules.default_style_id"] == (
        registry.SOURCE_USER_INSTRUCTION)


def test_cover_style_follows_manifest_change_without_code_change(env, monkeypatch):
    """改清单（外部编辑，不重启进程）→ 下一个任务的封面风格随之变化。"""
    calls: list[str] = []
    monkeypatch.setattr(tasks, "replace_cover", lambda t, c, style, **k: (
        calls.append(style) or {"cover_abs_path": str(env["clip_dir"] / "cover.jpg"),
                                "cover_revision": 2, "copy_report": None, "qc": None,
                                "source": {}, "idempotent": False}))
    _manifest(env["tmp"], cover_style="cutout_v3")
    record = tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                               task_type=tasks.TASK_REGENERATE_COVER, run=False)
    tasks._HANDLERS[tasks.TASK_REGENERATE_COVER](record)
    assert calls == ["cutout_v3"]


def test_title_rules_come_from_snapshot_and_forbidden_terms_are_rejected(env, monkeypatch):
    # 用一份「禁用词表里有字面词」的清单，验证禁用词表确实生效
    _manifest(env["tmp"], forbid=["治愈", "包治"])
    _wire_render_task(env)
    monkeypatch.setattr(tasks, "replace_cover", lambda *a, **k: {
        "cover_abs_path": str(env["clip_dir"] / "cover.jpg"), "cover_revision": 2,
        "copy_report": None, "qc": None, "source": {}, "idempotent": False})
    bad = tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                            task_type=tasks.TASK_UPDATE_TITLE,
                            payload={"title": "三天治愈胰腺癌", "refresh_cover": False},
                            run=False)
    with pytest.raises(tasks.LocalTaskError) as excinfo:
        tasks._HANDLERS[tasks.TASK_UPDATE_TITLE](bad)
    assert excinfo.value.code == "E_TITLE_FORBIDDEN_TERM"
    assert "治愈" in str(excinfo.value)

    good = tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                             task_type=tasks.TASK_UPDATE_TITLE,
                             payload={"title": "胰腺癌早期信号", "refresh_cover": False},
                             run=False)
    result = tasks._HANDLERS[tasks.TASK_UPDATE_TITLE](good)
    assert result["title"] == "胰腺癌早期信号"
    assert result["rules_source"]["title_rules"] == registry.SOURCE_SKILL
    assert result["title_forbidden_checked"] == ["治愈", "包治"]
    assert result["title_grounded_in_transcript"] is True     # 标题出自该片转写


def test_subtitle_style_records_the_unimplemented_adapter_gap(env):
    record = tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                               task_type=tasks.TASK_UPDATE_SUBTITLE_STYLE, run=False)
    result = tasks._HANDLERS[tasks.TASK_UPDATE_SUBTITLE_STYLE](record)
    # 字幕规则来自快照；字幕合成适配器仍未实现 —— 如实上报，不伪实现
    assert result["rules_source"]["subtitle_rules"] == registry.SOURCE_SKILL
    assert result["subtitle_adapter"] == "E_SKILL_NOT_IMPLEMENTED"
    assert result["config"]["band"] == "bottom" and result["config"]["burn_in"] is True
    with pytest.raises(skills.SkillExecutionError) as excinfo:
        skills.run_subtitle_composition_skill(skills.SkillContext(task_id="x"))
    assert excinfo.value.code == "E_SKILL_NOT_IMPLEMENTED"


def test_timeline_without_rerender_keeps_pending_flag(env):
    record = tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                               task_type=tasks.TASK_UPDATE_TIMELINE,
                               instruction="删除第 8 秒到第 10 秒", run=False)
    result = tasks._HANDLERS[tasks.TASK_UPDATE_TIMELINE](record)
    assert result["pending_rerender"] is True
    assert result["render_skill"] is None
    assert result["rules_source"]["render_rules"] == registry.SOURCE_SKILL


def test_timeline_rerender_goes_through_render_skill(env, monkeypatch):
    """「真正重渲该 clip」现在真的会提交渲染（render Skill 适配器），不再是只挂 pending 标记。"""
    captured: dict = {}

    def fake_call(ctx, endpoint, payload=None, **kwargs):
        captured["endpoint"] = endpoint
        captured["payload"] = payload or {}
        return {"task_id": "vb-edit-1", "status": "queued"}

    monkeypatch.setattr("src.skills.render.call_video_brain", fake_call)
    _wire_render_task(env)

    record = tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                               task_type=tasks.TASK_UPDATE_TIMELINE,
                               payload={"ranges": [{"start": 1.0, "end": 6.0}], "rerender": True},
                               run=False)
    result = tasks._HANDLERS[tasks.TASK_UPDATE_TIMELINE](record)
    assert captured["endpoint"] == "/v1/tasks"
    assert captured["payload"]["resolution"] == "1080x1920"     # 来自快照 render_rules
    assert captured["payload"]["platform"] == "douyin"
    assert captured["payload"]["idempotency_key"] == f"studio-{record['task_id']}"
    assert result["pending_rerender"] is False
    assert result["video_brain_task_id"] == "vb-edit-1"
    assert result["render_skill"]["ok"] is True
    assert result["skills"]["render"] == "v1"
    # 台账时间线字段记录了提交状态与 video-brain 任务号
    timeline = store.entry_for(env["project_id"], env["clip_id"])["timeline"]
    assert timeline["pending_rerender"] is False
    assert timeline["video_brain_task_id"] == "vb-edit-1"


def test_timeline_reselect_uses_clip_planning_skill(env, monkeypatch):
    captured: dict = {}

    def fake_call(ctx, endpoint, payload=None, **kwargs):
        captured["endpoint"] = endpoint
        captured["payload"] = payload or {}
        return {"clips": [{"clip_id": "clip-01", "start": 2.0, "end": 7.0}], "validated": True}

    monkeypatch.setattr("src.skills.planning.call_video_brain", fake_call)
    _wire_render_task(env)

    record = tasks.create_task(project_id=env["project_id"], clip_id=env["clip_id"],
                               task_type=tasks.TASK_UPDATE_TIMELINE,
                               payload={"reselect": True}, run=False)
    result = tasks._HANDLERS[tasks.TASK_UPDATE_TIMELINE](record)
    assert captured["endpoint"] == "/v1/tools/clip.plan_highlights"
    assert captured["payload"]["duration_range"] == [25, 45]     # 来自快照 clip_planning_rules
    assert result["planning_skill"]["ok"] is True
    assert result["ranges"] == [{"start": 2.0, "end": 7.0}]


# ---------------------------------------------------------------------------
# 4. 规则中心：实时读清单 / 启停 skill（备份 + 校验 + 回滚）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_workflow_endpoint_reads_manifest_live(env):
    first = await media.studio_workflow_registry()
    assert first["banner"]["current_version"] == "test-1"
    assert first["banner"]["label"] == "当前使用：医疗短视频剪辑规则 vtest-1"
    assert first["banner"]["update_hint"] == ""
    names = [item["skill_id"] for item in first["summary"]["skills"]]
    assert names == ["cover", "clip_planning", "render"]

    # 外部改清单（等价于成员更新规则文件/版本），**不重启任何东西** → 再读即新版本
    document = json.loads(env["manifest"].read_text(encoding="utf-8"))
    document["workflow_version"] = "test-2"
    document["updated_at"] = "2026-10-12T10:00:00Z"
    env["manifest"].write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    second = await media.studio_workflow_registry()
    assert second["banner"]["current_version"] == "test-2"
    assert second["summary"]["workflow_version"] == "test-2"


@pytest.mark.asyncio
async def test_workflow_endpoint_reports_stale_skill_as_new_rule_hint(env):
    """外部改 skill 文件（校验和不再匹配）→ 规则中心立刻显示「检测到新规则版本」。"""
    cover_file = env["tmp"] / "cover_styles.json"
    cover_file.write_text('{"default_style_id": "cutout_v3"}', encoding="utf-8")
    payload = await media.studio_workflow_registry()
    assert "cover" in payload["banner"]["stale_skills"]
    assert "检测到新规则版本" in payload["banner"]["update_hint"]
    stale = [item for item in payload["summary"]["skills"] if item["skill_id"] == "cover"]
    assert stale[0]["status"] == "stale"


@pytest.mark.asyncio
async def test_snapshot_endpoint_found_and_legacy_notice(env):
    wired = "autoedit_aaaabbbbcccc"
    studio_workflow.build_task_snapshot(
        wired, snapshot_dir=studio_workflow.snapshot_dir(env["root"], wired))
    payload = await media.studio_workflow_snapshot(wired)
    assert payload["found"] is True
    assert payload["version_line"].startswith("使用规则版本：workflow vtest-1")
    assert payload["snapshot"]["rules"]["cover_rules"]["default_style_id"] == "realframe_v2"

    # 旧成片（Phase B 之前，例如台账里的 autoedit_064d806d3218）→ 如实说明没有快照
    legacy = await media.studio_workflow_snapshot("autoedit_064d806d3218")
    assert legacy["found"] is False
    assert "没有规则快照" in legacy["notice"]

    with pytest.raises(Exception):
        await media.studio_workflow_snapshot("../../etc/passwd")   # 目录穿越被挡


@pytest.mark.asyncio
async def test_toggle_endpoint_backs_up_and_bumps_version(env):
    result = await media.studio_workflow_toggle_skill(
        "cover", media.WorkflowSkillToggleRequest(enabled=False))
    assert result["enabled"] is False
    assert result["previous_enabled"] is True
    assert result["previous_workflow_version"] == "test-1"
    assert result["workflow_version"] == "test-2"          # 启停即新版本
    backup = Path(result["backup_path"])
    assert backup.is_file() and ".bak." in backup.name
    assert json.loads(backup.read_text(encoding="utf-8"))["workflow_version"] == "test-1"
    on_disk = json.loads(env["manifest"].read_text(encoding="utf-8"))
    assert next(item for item in on_disk["skills"]
                if item["skill_id"] == "cover")["enabled"] is False
    assert on_disk["workflow_version"] == "test-2"

    # 同一秒内再启停一次：版本继续推进，且**不会覆盖**上一份备份
    again = await media.studio_workflow_toggle_skill(
        "cover", media.WorkflowSkillToggleRequest(enabled=True))
    assert again["workflow_version"] == "test-3"
    assert again["backup_path"] != result["backup_path"]
    backups = sorted(p.name for p in env["manifest"].parent.glob(
        f"{env['manifest'].name}.bak.*"))
    assert len(backups) == 2
    # 第一份备份仍是「最初」的状态（version test-1），没被第二次启停覆盖
    assert json.loads(Path(result["backup_path"]).read_text(encoding="utf-8"))[
        "workflow_version"] == "test-1"


def test_toggle_rolls_back_when_manifest_becomes_invalid(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path, single_skill=True)
    monkeypatch.setenv("EDITING_WORKFLOW_MANIFEST", str(manifest))
    original = manifest.read_text(encoding="utf-8")
    with pytest.raises(studio_workflow.WorkflowError) as excinfo:
        studio_workflow.set_skill_enabled("cover", False)   # 关掉唯一启用的 skill → 非法
    assert excinfo.value.code == studio_workflow.E_MANIFEST_ROLLED_BACK
    assert manifest.read_text(encoding="utf-8") == original   # 逐字节回滚
    assert registry.validate_manifest().ok


# ---------------------------------------------------------------------------
# 5. 「按最新规则重新生成」：新 revision、旧成果保留、卡片数不变
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_regenerate_endpoint_adds_revision_without_new_card(env, monkeypatch):
    monkeypatch.setattr(tasks, "replace_cover", lambda t, c, style, **k: {
        "cover_abs_path": str(env["clip_dir"] / "cover.jpg"), "cover_revision": 2,
        "copy_report": None, "qc": None, "source": {}, "idempotent": False})
    # 让封面产物真的变化，revision 才会推进
    (env["clip_dir"] / "cover.jpg").write_bytes(b"NEW-COVER-V2")

    response = await media.studio_workflow_regenerate(
        env["project_id"], env["clip_id"], media.WorkflowRegenerateRequest(mode="cover"))
    assert response["mode"] == "cover"
    assert response["cards_before"] == 1
    assert response["cover_revision_before"] == 1
    assert response["workflow_version"] == "test-1"
    assert response["task_type"] == tasks.TASK_REGENERATE_COVER

    done = _wait(response["task_id"])
    assert done["status"] == "completed", done.get("error")
    cards = store.list_cards(env["project_id"])
    assert len(cards) == 1                                  # 不新增无关卡片
    assert cards[0]["cover"]["revision"] == 2               # 新 revision
    assert [item["revision"] for item in cards[0]["cover"]["history"]] == [1, 2]  # 旧成果保留
    assert cards[0]["render_task_id"] == env["render_task"]  # 首渲任务不变


# ---------------------------------------------------------------------------
# Phase 1：新增局部任务前缀必须与 studio_local_tasks 的白名单一致
# ---------------------------------------------------------------------------

def test_phase1_local_task_prefixes_are_valid_snapshot_ids(tmp_path):
    """msub/xfm/aud/fx/rnd 必须被 validate_task_id 接受，且快照落在 localtasks/。

    回归：这三个模块各有一份任务 id 白名单（studio_local_tasks /
    studio_auto_edit_bridge / studio_workflow）。Phase 1 只更新了前两份，
    studio_workflow 仍只认 autoedit|cover|title|subtitle|edit —— 于是
    create_task 建规则快照时抛 E_TASK_ID_INVALID（全部新类型都无法创建），
    GET /studio/workflow/{task_id}/snapshot 也会把合法任务判为非法 id。
    """
    for prefix in ("msub", "xfm", "aud", "fx", "rnd"):
        task_id = f"{prefix}_0123456789ab"
        assert studio_workflow.validate_task_id(task_id) == task_id
        assert studio_workflow.snapshot_dir(tmp_path, task_id) == tmp_path / "localtasks" / task_id
    # 既有前缀与 AUTO_EDIT 行为不变
    assert studio_workflow.snapshot_dir(tmp_path, "autoedit_0123456789ab") == \
        tmp_path / "output" / "autoedit_0123456789ab"
    # 仍然挡掉穿越 / 未知前缀
    for bad in ("../etc", "evil_0123456789ab", "msub_0123456789AB"):
        with pytest.raises(studio_workflow.WorkflowError):
            studio_workflow.validate_task_id(bad)
