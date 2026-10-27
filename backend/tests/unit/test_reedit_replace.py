"""「重新剪辑并替换旧切片」（reedit_replace）集成测试。

真实 FFmpeg 渲染（合成素材）+ 临时台账；模型层（分析/规划）全部守卫桩。
覆盖需求 A/B/C/D 四组场景与保护清单（源片/转写绝不删除）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from src import clip_planning as cp
from src import clip_render as cr
from src import content_analysis as ca
from src import r2
from src import reedit_replace as rr
from src import studio_job_worker as worker
from src import studio_project_store as store


def seg(i: int, text: str, s_ms: int, e_ms: int) -> dict:
    return {"segment_id": f"seg_{i:04d}", "start_ms": s_ms, "end_ms": e_ms, "text": text}


@pytest.fixture
def tasks_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """同时改 env 与 store 的 import 期常量，确保台账读写都在临时根。"""
    root = tmp_path / "tasks"
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(root))
    monkeypatch.setattr(store, "TASKS_ROOT", root)
    return root


@pytest.fixture(scope="module")
def source_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("media") / "source.mp4"
    subprocess_run = __import__("subprocess").run
    subprocess_run(
        [cr._resolve(cr.FFMPEG_BIN), "-y",
         "-f", "lavfi", "-i", "testsrc2=duration=8:size=320x240:rate=15",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
         str(path)],
        check=True, capture_output=True)
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _install_fake_object_store(monkeypatch: pytest.MonkeyPatch, tasks_root: Path):
    """把 src.r2 换成「真实落盘」的本地对象存储桩（Step 3 发布契约的前提）。

    发布契约在 upload_file 之后要对本地副本做存在性/大小/ffprobe 验证，桩必须
    真实复制（与生产 local 模式同语义）。映射基于 STUDIO_TASKS_ROOT 临时根：
    ``<tasks_root>/objectstore/<key>``——确定性且随用例隔离（local:/R2 key 通吃）。
    返回 upload_file 桩，供发布失败注入用例再包一层。
    """

    def local_path_for(object_key: str) -> Path:
        raw = (object_key[len(r2.LOCAL_SCHEME):]
               if object_key.startswith(r2.LOCAL_SCHEME) else object_key)
        return tasks_root / "objectstore" / raw.lstrip("/").replace("..", "_")

    def upload_file(path, key):
        target = local_path_for(str(key))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(path), str(target))
        return {"object_key": str(key), "size": Path(path).stat().st_size}

    monkeypatch.setattr("src.r2.local_path_for", local_path_for)
    monkeypatch.setattr("src.r2.upload_file", upload_file)
    return upload_file


def _make_old_project(tasks_root: Path, source_video: Path) -> None:
    """旧项目：2 个旧任务、3 张旧卡片、封面/字幕/缩略图等衍生产物。"""
    store.register_clips("timeline_e2e", "old-task-1", [
        {"clip_id": "clip-old1", "topic": "旧主题一", "source_start": 0, "source_end": 2, "duration": 2},
        {"clip_id": "clip-old2", "topic": "旧主题二", "source_start": 2, "source_end": 4, "duration": 2},
    ])
    store.register_clips("timeline_e2e", "old-task-2", [
        {"clip_id": "clip-old3", "topic": "旧主题三", "source_start": 4, "source_end": 6, "duration": 2},
    ])
    for task_id, clip_ids in (("old-task-1", ["clip-old1", "clip-old2"]), ("old-task-2", ["clip-old3"])):
        task_dir = tasks_root / "output" / task_id
        for clip_id in clip_ids:
            clip_dir = task_dir / "clips" / clip_id
            clip_dir.mkdir(parents=True)
            (clip_dir / "clip_final.mp4").write_bytes(b"old-video-bytes")
            (clip_dir / "cover.jpg").write_bytes(b"old-cover")
            (clip_dir / "subtitles.srt").write_text("旧字幕", encoding="utf-8")
            (clip_dir / "thumb.png").write_bytes(b"old-thumb")
            (clip_dir / "waveform.dat").write_bytes(b"old-wave")
        (tasks_root / "completed" / f"{task_id}.json").parent.mkdir(parents=True, exist_ok=True)
        (tasks_root / "completed" / f"{task_id}.json").write_text("{}", encoding="utf-8")
        (tasks_root / "status").mkdir(parents=True, exist_ok=True)
        (tasks_root / "status" / f"{task_id}.json").write_text("{}", encoding="utf-8")
    (tasks_root / "localtasks").mkdir(parents=True, exist_ok=True)
    (tasks_root / "localtasks" / "old-task-2.json").write_text("{}", encoding="utf-8")
    # 受保护共享资产
    media_dir = tasks_root / "media" / "source-task"
    media_dir.mkdir(parents=True)
    shutil.copy2(source_video, media_dir / "source.mp4")
    transcript_dir = tasks_root / "transcripts" / "859e89b941036cb0"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "v1.json").write_text('{"schema": "studio.corrected_transcript/1"}', encoding="utf-8")


def _transcript() -> dict:
    return {"asr_segments": _segments(), "raw_segments": [], "transcript_id": "tr-x",
            "transcript_version": 3}


def _segments() -> list[dict]:
    return [
        seg(0, "第一块完整内容。", 0, 2000),
        seg(1, "第二块完整内容。", 2500, 4500),
        seg(2, "第三块完整内容。", 5000, 7000),
    ]


def _plan() -> dict:
    return {"plan_source": "heuristic_fallback", "llm_error": None,
            "rule_snapshot_id": "2026-10-13.10",
            "clips": [
                {"clip_id": "clip-new1", "topic": "新主题一", "start_segment_id": "seg_0000",
                 "end_segment_id": "seg_0000", "start_ms": 0, "end_ms": 2000,
                 "duration_ms": 2000, "completeness": "pass", "warnings": []},
                {"clip_id": "clip-new2", "topic": "新主题二", "start_segment_id": "seg_0002",
                 "end_segment_id": "seg_0002", "start_ms": 5000, "end_ms": 7000,
                 "duration_ms": 2000, "completeness": "warning",
                 "warnings": ["short_duration: 时长 2s 低于 30s"]},
            ],
            "discarded": [], "notes": []}


class Recorder:
    def __init__(self):
        self.states: list[str] = []

    def __call__(self, job_id, status, stage, progress, error=None):
        self.states.append(status)


def _wire_worker(monkeypatch: pytest.MonkeyPatch, source_video: Path, recorder: Recorder,
                 analyze_calls: list[int], plan_calls: list[int], tasks_root: Path):
    """桩掉模型层/状态上报，并把 r2 换成真实落盘桩（发布副本验证可通过）。

    返回 upload_file 桩，供需要注入发布失败的用例再包装。
    """
    monkeypatch.setattr(worker, "_state", recorder)
    monkeypatch.setattr(worker, "_materialise_source_for_job", lambda key, jid: source_video)
    monkeypatch.setattr(worker, "_rule_snapshot_id", lambda: "2026-10-13.10")
    monkeypatch.setattr(worker, "_http", lambda *a, **k: {})
    fake_upload = _install_fake_object_store(monkeypatch, tasks_root)

    def fake_analyze(segments, **kwargs):
        analyze_calls.append(1)
        return {"plan_source": "heuristic_fallback", "keywords": ["高血压"], "golden_quotes": [],
                "topic_units": [], "candidate_intervals": []}

    def fake_plan(segments, analysis, **kwargs):
        plan_calls.append(1)
        return _plan()

    monkeypatch.setattr(ca, "analyze_content", fake_analyze)
    monkeypatch.setattr(cp, "plan_clips", fake_plan)
    return fake_upload


def test_a_full_success_purges_old_keeps_source_and_transcript(
        tasks_root: Path, monkeypatch: pytest.MonkeyPatch, source_video: Path):
    _make_old_project(tasks_root, source_video)
    source_hash = _sha(tasks_root / "media" / "source-task" / "source.mp4")
    transcript_hash = _sha(tasks_root / "transcripts" / "859e89b941036cb0" / "v1.json")
    recorder, analyze_calls, plan_calls = Recorder(), [], []
    _wire_worker(monkeypatch, source_video, recorder, analyze_calls, plan_calls, tasks_root)
    deleted_artifacts: list[list[str]] = []
    artifact_posts: list[dict] = []

    async def fake_delete_artifacts(ids):
        # worker 的 _delete_artifacts 用 asyncio.run 调用本函数，必须是 async；
        # 被 await 后 append 才会执行。
        deleted_artifacts.append(list(ids))
        return 0

    def capture_http(method, url, payload=None, timeout=60):
        # 捕获 Step 3 发布契约的 artifact 登记请求
        if method == "POST" and str(url).endswith("/artifacts") and isinstance(payload, dict):
            artifact_posts.append(dict(payload))
        return {}

    monkeypatch.setattr("src.studio_job_store.delete_artifacts_for_jobs", fake_delete_artifacts)
    monkeypatch.setattr(worker, "_http", capture_http)

    job = {"job_id": "autoedit_reedit1", "object_key": "local:/v.mp4",
           "project_id": "timeline_e2e",
           "config": {"job_kind": "auto_edit", "replacement_mode": "delete_old_after_success",
                      "idempotency_key": "reedit-key-1"}}
    worker._run_reedit_replace(job, "autoedit_reedit1", _transcript())

    assert recorder.states[0] == "reedit_requested"
    assert "uploading_reedit_artifacts" in recorder.states
    assert "purging_old_clips" in recorder.states and recorder.states[-1] in {"completed", "completed_with_warnings"}
    # 发布成功后才激活（ACTIVATED 标记只在发布事务完成后落盘）
    assert (tasks_root / "output" / "autoedit_reedit1" / rr.ACTIVATED_MARKER).is_file()
    assert recorder.states.index("uploading_reedit_artifacts") < recorder.states.index("activating_run")
    # 旧任务目录与记录全部消失
    for task_id in ("old-task-1", "old-task-2"):
        assert not (tasks_root / "output" / task_id).exists()
        assert not (tasks_root / "completed" / f"{task_id}.json").exists()
        assert not (tasks_root / "status" / f"{task_id}.json").exists()
    assert not (tasks_root / "localtasks" / "old-task-2.json").exists()
    # 台账只剩新切片（render_task_id = run_id）
    ledger = store.load_manifest("timeline_e2e")
    assert set(ledger["clips"]) == {"clip-new1", "clip-new2"}
    assert all(entry["render_task_id"] == "autoedit_reedit1" for entry in ledger["clips"].values())
    assert not any("old-task" in json.dumps(item) for item in ledger.get("task_history") or [])
    # 新切片产物存在且可解码
    for clip_id in ("clip-new1", "clip-new2"):
        final = tasks_root / "output" / "autoedit_reedit1" / "clips" / clip_id / "clip_final.mp4"
        playable, reason = cr._ffprobe_playable(final)
        assert playable, reason
    # 受保护资产不变
    assert _sha(tasks_root / "media" / "source-task" / "source.mp4") == source_hash
    assert _sha(tasks_root / "transcripts" / "859e89b941036cb0" / "v1.json") == transcript_hash
    # 报告与 DB 关联记录删除
    report = json.loads((tasks_root / "output" / "autoedit_reedit1" / "reedit_report.json").read_text())
    assert report["clips_deleted"] == 3
    assert report["bytes_freed"] > 0 and report["files_deleted"] > 0
    assert report["failures"] == []
    assert deleted_artifacts and set(deleted_artifacts[0]) == {"old-task-1", "old-task-2"}

    # Step 3 发布契约：每个新切片都有 kind="clip" 登记，payload 与真实副本一致
    job_id = "autoedit_reedit1"
    clip_posts = {p.get("clip_id"): p for p in artifact_posts if p.get("kind") == "clip"}
    assert set(clip_posts) == {"clip-new1", "clip-new2"}
    for clip_id, payload in clip_posts.items():
        final = tasks_root / "output" / job_id / "clips" / clip_id / "clip_final.mp4"
        copy = (tasks_root / "objectstore" / "studio-artifacts" / job_id / clip_id
                / "clip_final.mp4")
        assert payload["object_key"].startswith(f"studio-artifacts/{job_id}/")
        assert payload["object_key"].endswith("/clip_final.mp4")
        assert copy.is_file(), f"发布副本未落盘 {clip_id}"
        assert payload["size_bytes"] == final.stat().st_size == copy.stat().st_size
        assert len(payload["sha256"]) == 64
        assert payload["sha256"] == _sha(final) == _sha(copy)
        playable, reason = cr._ffprobe_playable(copy)
        assert playable, f"发布副本不可解码 {clip_id}: {reason}"


def test_b_all_render_failure_keeps_old_and_cleans_staging(
        tasks_root: Path, monkeypatch: pytest.MonkeyPatch, source_video: Path):
    _make_old_project(tasks_root, source_video)
    source_hash = _sha(tasks_root / "media" / "source-task" / "source.mp4")
    transcript_hash = _sha(tasks_root / "transcripts" / "859e89b941036cb0" / "v1.json")
    recorder, analyze_calls, plan_calls = Recorder(), [], []
    _wire_worker(monkeypatch, source_video, recorder, analyze_calls, plan_calls, tasks_root)

    def failing_render(plan, **kwargs):
        return {"job_status": "technical_failed", "entries": [], "counts": {},
                "warnings": []}

    monkeypatch.setattr(cr, "render_plan", failing_render)

    job = {"job_id": "autoedit_reedit2", "object_key": "local:/v.mp4",
           "project_id": "timeline_e2e",
           "config": {"job_kind": "auto_edit", "replacement_mode": "delete_old_after_success"}}
    worker._run_reedit_replace(job, "autoedit_reedit2", _transcript())

    assert recorder.states[-1] == "technical_failed"
    assert "purging_old_clips" not in recorder.states
    # 旧切片原样保留且可"播放"（文件仍在）
    assert (tasks_root / "output" / "old-task-1" / "clips" / "clip-old1" / "clip_final.mp4").is_file()
    ledger = store.load_manifest("timeline_e2e")
    assert set(ledger["clips"]) == {"clip-old1", "clip-old2", "clip-old3"}
    # staging 已清理
    assert not (tasks_root / "output" / "autoedit_reedit2").exists()
    assert _sha(tasks_root / "media" / "source-task" / "source.mp4") == source_hash
    assert _sha(tasks_root / "transcripts" / "859e89b941036cb0" / "v1.json") == transcript_hash


def test_c_partial_cleanup_failure_then_retry_cleanup_only(
        tasks_root: Path, monkeypatch: pytest.MonkeyPatch, source_video: Path):
    _make_old_project(tasks_root, source_video)
    recorder, analyze_calls, plan_calls = Recorder(), [], []
    _wire_worker(monkeypatch, source_video, recorder, analyze_calls, plan_calls, tasks_root)

    real_move = shutil.move
    blocked = tasks_root / "output" / "old-task-2"

    def flaky_move(src, dst):
        if str(src).startswith(str(blocked)):
            raise OSError("模拟文件被占用")
        return real_move(src, dst)

    monkeypatch.setattr(rr.shutil, "move", flaky_move)
    job = {"job_id": "autoedit_reedit3", "object_key": "local:/v.mp4",
           "project_id": "timeline_e2e",
           "config": {"job_kind": "auto_edit", "replacement_mode": "delete_old_after_success"}}
    worker._run_reedit_replace(job, "autoedit_reedit3", _transcript())

    assert recorder.states[-1] == "completed_with_cleanup_warning"
    assert any("old-task-2" in f for f in
               json.loads((tasks_root / "output" / "autoedit_reedit3" / "reedit_report.json").read_text())["failures"])
    ledger = store.load_manifest("timeline_e2e")
    assert set(ledger["clips"]) == {"clip-new1", "clip-new2"}          # 新切片已生效
    assert blocked.exists()                                             # 清理失败项仍在

    # 只重试清理：不重新规划/渲染（计数不增），失败项被清掉
    monkeypatch.setattr(rr.shutil, "move", real_move)   # 解除占用模拟
    before_calls = (len(analyze_calls), len(plan_calls))
    report = rr.retry_cleanup("timeline_e2e", "autoedit_reedit3")
    assert report["failures"] == []
    assert report.get("retry") is True
    assert not blocked.exists()
    assert (len(analyze_calls), len(plan_calls)) == before_calls


def test_d_resume_after_activation_only_continues_purge(
        tasks_root: Path, monkeypatch: pytest.MonkeyPatch, source_video: Path):
    _make_old_project(tasks_root, source_video)
    recorder, analyze_calls, plan_calls = Recorder(), [], []

    def _guard(name):
        def _fail(*args, **kwargs):
            raise AssertionError(f"恢复路径禁止重新调用：{name}")
        return _fail

    monkeypatch.setattr(ca, "analyze_content", _guard("analyze_content"))
    monkeypatch.setattr(cp, "plan_clips", _guard("plan_clips"))
    monkeypatch.setattr(worker, "_materialise_source_for_job", lambda key, jid: source_video)
    monkeypatch.setattr(worker, "_rule_snapshot_id", lambda: "v1")

    async def fake_delete_artifacts(ids):
        return 0

    monkeypatch.setattr("src.studio_job_store.delete_artifacts_for_jobs", fake_delete_artifacts)
    monkeypatch.setattr(worker, "_http", lambda *a, **k: {})
    _install_fake_object_store(monkeypatch, tasks_root)  # 发布副本真实落盘（local_path_for + copy）
    states: list[str] = []
    monkeypatch.setattr(worker, "_state",
                        lambda job_id, status, stage, progress, error=None: states.append(status))

    run_id = "autoedit_reedit4"
    # 预置：分析工件（真实落盘）+ staging 成品（真实可解码） + 已激活标记 + 清单
    ca.save_analysis_record(run_id, {"plan_source": "llm", "llm_error": None, "llm_calls": 1,
                                     "llm_duration_ms": 900, "model": "openai:test-model",
                                     "rule_snapshot_id": "v1", "keywords": [], "golden_quotes": [],
                                     "topic_units": [], "candidate_intervals": [],
                                     "transcript_version": 3})
    plan = _plan()
    cp.save_plan_record(run_id, plan)
    render_result = cr.render_plan(plan, source=source_video, out_root=rr.staging_root(run_id),
                                   corrected_segments=_segments(),
                                   transcript_ref={"transcript_id": "tr-x", "transcript_version": 3})
    assert render_result["counts"]["succeeded"] == 2
    rr.build_old_manifest("timeline_e2e", run_id)
    rr.activate_new_run("timeline_e2e", run_id, plan)
    assert rr.activation_done(run_id)

    job = {"job_id": run_id, "object_key": "local:/v.mp4", "project_id": "timeline_e2e",
           "config": {"job_kind": "auto_edit", "replacement_mode": "delete_old_after_success"}}
    new_final = rr.staging_root(run_id) / "clips" / "clip-new1" / "clip_final.mp4"
    mtime_before = new_final.stat().st_mtime_ns
    worker._run_reedit_replace(job, run_id, _transcript())

    # 已可解码成片直接跳过（文件未被覆盖重渲），阶段只继续激活后的清理
    assert new_final.stat().st_mtime_ns == mtime_before
    assert "purging_old_clips" in states
    assert states[-1] in {"completed", "completed_with_warnings", "completed_with_cleanup_warning"}
    ledger = store.load_manifest("timeline_e2e")
    assert set(ledger["clips"]) == {"clip-new1", "clip-new2"}   # 新切片未误删
    for task_id in ("old-task-1", "old-task-2"):
        assert not (tasks_root / "output" / task_id).exists()
    assert new_final.is_file()


def test_e_publish_failure_aborts_replacement_keeps_old_and_staging(
        tasks_root: Path, monkeypatch: pytest.MonkeyPatch, source_video: Path):
    """Step 3 终止契约：任一新切片发布失败 → technical_failed，绝不激活/清理。"""
    _make_old_project(tasks_root, source_video)
    job_id = "autoedit_reedit5"
    # 旧状态快照：旧切片文件内容 + 旧任务记录 + 台账
    snapshot_before: dict[str, str] = {}
    for task_id in ("old-task-1", "old-task-2"):
        for file in (tasks_root / "output" / task_id).rglob("*"):
            if file.is_file():
                snapshot_before[str(file)] = _sha(file)
    for extra in ("completed/old-task-1.json", "completed/old-task-2.json",
                  "status/old-task-1.json", "status/old-task-2.json",
                  "localtasks/old-task-2.json"):
        snapshot_before[str(tasks_root / extra)] = _sha(tasks_root / extra)
    ledger_before = json.dumps(store.load_manifest("timeline_e2e"), sort_keys=True)

    recorder, analyze_calls, plan_calls = Recorder(), [], []
    fake_upload = _wire_worker(monkeypatch, source_video, recorder, analyze_calls,
                               plan_calls, tasks_root)

    def flaky_upload(path, key):
        if "clip-new2" in str(key):
            raise OSError("模拟对象存储写入失败")
        return fake_upload(path, key)

    monkeypatch.setattr("src.r2.upload_file", flaky_upload)

    job = {"job_id": job_id, "object_key": "local:/v.mp4", "project_id": "timeline_e2e",
           "config": {"job_kind": "auto_edit", "replacement_mode": "delete_old_after_success"}}
    worker._run_reedit_replace(job, job_id, _transcript())

    # 终态：发布失败 → technical_failed；绝不进入激活/清理
    assert recorder.states[-1] == "technical_failed"
    assert "uploading_reedit_artifacts" in recorder.states
    assert "activating_run" not in recorder.states
    assert "purging_old_clips" not in recorder.states
    # 台账完全不变（激活未发生）
    assert json.dumps(store.load_manifest("timeline_e2e"), sort_keys=True) == ledger_before
    assert set(store.load_manifest("timeline_e2e")["clips"]) == {"clip-old1", "clip-old2", "clip-old3"}
    # 旧切片文件与旧任务记录逐字节不变
    for path_str, digest in snapshot_before.items():
        assert _sha(Path(path_str)) == digest, f"旧资产被改动: {path_str}"
    for task_id in ("old-task-1", "old-task-2"):
        assert (tasks_root / "output" / task_id).is_dir()
    # staging 保留（不 discard），无激活标记、无清理报告，可重试
    staging = tasks_root / "output" / job_id
    assert staging.is_dir()
    assert not (staging / rr.ACTIVATED_MARKER).exists()
    assert not (staging / rr.PURGE_REPORT).exists()
    assert (staging / "clips" / "clip-new1" / "clip_final.mp4").is_file()
    assert (staging / "clips" / "clip-new2" / "clip_final.mp4").is_file()


def test_find_project_by_task_and_source_inode(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_video: Path):
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(tmp_path))
    monkeypatch.setattr(store, "TASKS_ROOT", tmp_path)
    store.register_clips("timeline_e2e", "task-x", [
        {"clip_id": "clip-x1", "topic": "t", "source_start": 0, "source_end": 1, "duration": 1}])
    assert store.find_project_by_task("task-x") == "timeline_e2e"
    assert store.find_project_by_task("task-none") is None

    uploads = tmp_path / "temp" / "studio-uploads"
    uploads.mkdir(parents=True)
    upload = uploads / "abc.mp4"
    shutil.copy2(source_video, upload)
    media = tmp_path / "media" / "task-x" / "source.mp4"
    media.parent.mkdir(parents=True)
    os.link(upload, media)
    monkeypatch.setattr(store, "BASE_DIR", tmp_path)   # uploads 根 = <BASE_DIR>/temp/studio-uploads
    assert store.find_object_key_by_source_path(media) == f"local:{upload}"
    assert store.find_object_key_by_source_path(tmp_path / "none.mp4") is None
