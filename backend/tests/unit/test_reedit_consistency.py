"""纯合成对象，禁止真实模型、渲染、数据库和资料删除。"""
import json
import os
import subprocess
import sys
import multiprocessing
from pathlib import Path
import pytest
from src import reedit_replace as rr, studio_project_store as store, clip_render as cr


@pytest.fixture(autouse=True)
def isolated_capacity_for_synthetic_controls(tmp_path_factory, monkeypatch):
    from src import capacity_guard as cg
    root = tmp_path_factory.mktemp("reedit-capacity-ledger")
    monkeypatch.setattr(cg, "free_bytes", lambda _: 10 * 1024**3)
    monkeypatch.setattr(cg, "_reservation_paths", lambda device: (root/f"{device}.lock",root/f"{device}.json"))


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "tasks"
    monkeypatch.setenv("STUDIO_TASKS_ROOT", str(root))
    monkeypatch.setattr(store, "TASKS_ROOT", root)
    monkeypatch.setattr(rr, "_ffprobe_playable", lambda p: (p.is_file(), ""))
    monkeypatch.setattr(rr.purge, "_prune_task_log", lambda ids: None)
    store.register_clips("timeline_test", "old", [{"clip_id": "clip-stable", "topic": "old"}])
    old = root / "output" / "old" / "clips" / "clip-stable" / "clip_final.mp4"
    old.parent.mkdir(parents=True); old.write_bytes(b"old-video")
    return root


def prepare(run_id, clips=("clip-stable",)):
    old = rr.build_old_manifest("timeline_test", run_id)
    plan = {"clips": [{"clip_id": cid, "topic": "new", "start_ms": 0,
                        "end_ms": 1000, "duration_ms": 1000} for cid in clips]}
    published = []
    for cid in clips:
        video = rr.staging_root(run_id) / "clips" / cid / "clip_final.mp4"
        video.parent.mkdir(parents=True); video.write_bytes(run_id.encode())
        published.append({"clip_id": cid, "path": str(video)})
    return old, plan, published


def test_two_identical_boundaries_switch_instance_and_cleanup(sandbox):
    for run_id in ("1"*32, "2"*32):
        old, plan, published = prepare(run_id)
        rr.activate_new_run("timeline_test", run_id, plan, published=published)
        result = rr.purge_old_clips(old, delete_artifacts_fn=lambda ids: None)
        entry = store.entry_for("timeline_test", "clip-stable")
        assert entry["render_task_id"] == run_id
        assert Path(store.TASKS_ROOT / store.card_view(entry)["video_path"]).read_bytes() == run_id.encode()
        assert result["failures"] == []
        assert len(store.list_cards("timeline_test")) == 1
    assert not (sandbox / "output" / "old").exists()
    assert not (sandbox / "output" / ("1"*32)).exists()


def test_partial_only_verified_clip_and_total_failure_preserves_ledger(sandbox):
    old, plan, published = prepare("partial", ("clip-stable", "clip-failed"))
    before = store.manifest_path("timeline_test").read_bytes()
    with pytest.raises(rr.ReeditError) as caught:
        rr.activate_new_run("timeline_test", "partial", plan, published=[])
    assert caught.value.code == "E_REEDIT_NOT_PUBLISHED"
    assert store.manifest_path("timeline_test").read_bytes() == before
    rr.activate_new_run("timeline_test", "partial", plan, published=published[:1])
    assert set(store.load_manifest("timeline_test")["clips"]) == {"clip-stable"}


def test_two_prepared_runs_cas_rejects_loser(sandbox):
    old1, plan1, pubs1 = prepare("winner")
    old2, plan2, pubs2 = prepare("loser")
    rr.activate_new_run("timeline_test", "winner", plan1, published=pubs1)
    before = store.manifest_path("timeline_test").read_bytes()
    with pytest.raises(rr.ReeditError) as caught:
        rr.activate_new_run("timeline_test", "loser", plan2, published=pubs2)
    assert caught.value.code == "E_REEDIT_CONFLICT"
    assert store.manifest_path("timeline_test").read_bytes() == before
    with pytest.raises(rr.ReeditError):
        rr.purge_old_clips(old2, delete_artifacts_fn=lambda ids: None)


def _activate_competing(run, plan, pubs, barrier, results):
    barrier.wait(timeout=3)
    try:
        rr.activate_new_run("timeline_test", run, plan, published=pubs)
        results.put("activated")
    except rr.ReeditError as exc:
        results.put(exc.code)


def test_real_process_activation_race_has_one_winner(sandbox):
    ctx = multiprocessing.get_context("fork")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    children = []
    for run in ("race1", "race2"):
        old, plan, pubs = prepare(run)
        children.append(ctx.Process(target=_activate_competing, args=(run, plan, pubs, barrier, results)))
    for child in children: child.start()
    for child in children:
        child.join(5)
        assert child.exitcode == 0
    assert sorted([results.get(timeout=1), results.get(timeout=1)]) == ["E_REEDIT_CONFLICT", "activated"]
    assert store.entry_for("timeline_test", "clip-stable")["render_task_id"] in {"race1", "race2"}


def test_cleanup_retries_known_trash_without_double_count(sandbox, monkeypatch):
    old, plan, pubs = prepare("recover")
    rr.activate_new_run("timeline_test", "recover", plan, published=pubs)
    original = Path.unlink
    def blocked(path, *args, **kwargs):
        if ".purge-trash" in str(path):
            raise OSError("synthetic occupied")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", blocked)
    first = rr.purge_old_clips(old, delete_artifacts_fn=lambda ids: None)
    assert first["failures"] and first["bytes_freed"] == 0
    assert not (sandbox / "output" / "old").exists()
    state = json.loads((rr.staging_root("recover") / "cleanup_state.json").read_text())
    assert any(Path(v["trash"]).exists() for v in state["targets"].values())
    monkeypatch.setattr(Path, "unlink", original)
    second = rr.retry_cleanup("timeline_test", "recover", delete_artifacts_fn=lambda ids: None)
    third = rr.retry_cleanup("timeline_test", "recover", delete_artifacts_fn=lambda ids: None)
    assert second["failures"] == third["failures"] == []
    assert second["bytes_freed"] == third["bytes_freed"] == len(b"old-video")
    assert second["clips_deleted"] == third["clips_deleted"] == 1
    assert store.entry_for("timeline_test", "clip-stable")["render_task_id"] == "recover"


def test_shared_run_and_source_never_removed(sandbox):
    store.register_clips("timeline_other", "old", [{"clip_id": "other"}])
    old, plan, pubs = prepare("shared")
    source = sandbox / "media" / "source.mp4"; source.parent.mkdir(); source.write_bytes(b"source")
    old["targets"].append(str(source))
    # 持久清单也故意含越界源片：白名单仍必须拒绝。
    store._atomic_json(rr.staging_root("shared") / rr.REEDIT_MANIFEST, old)
    rr.activate_new_run("timeline_test", "shared", plan, published=pubs)
    deleted = []
    result = rr.purge_old_clips(old, delete_artifacts_fn=lambda ids: deleted.extend(ids))
    assert result["failures"]
    assert source.read_bytes() == b"source" and (sandbox / "output" / "old").exists()
    assert deleted == []


def test_activated_recovery_uses_ledger_even_without_marker(sandbox, monkeypatch):
    from src import studio_job_worker as worker
    old, plan, pubs = prepare("resume")
    rr.activate_new_run("timeline_test", "resume", plan, published=pubs)
    (rr.staging_root("resume") / rr.ACTIVATED_MARKER).unlink()
    assert rr.activation_done("resume")
    for name in ("_run_content_analysis_stage", "_run_clip_planning_stage", "_materialise_source_for_job"):
        monkeypatch.setattr(worker, name, lambda *a, **k: pytest.fail("recovery called model/render"))
    monkeypatch.setattr(cr, "render_plan", lambda *a, **k: pytest.fail("recovery rendered"))
    states = []
    monkeypatch.setattr(worker, "_state", lambda jid, status, *a: states.append(status))
    async def delete(ids): return 0
    monkeypatch.setattr("src.studio_job_store.delete_artifacts_for_jobs", delete)
    worker._run_reedit_replace({"project_id": "timeline_test"}, "resume", {})
    assert states == ["purging_old_clips", "completed"]


def test_worker_actual_entry_cleanup_does_not_read_transcript(sandbox, monkeypatch):
    from src import studio_job_worker as worker, studio_transcript_store
    old, plan, pubs = prepare("entryresume")
    rr.activate_new_run("timeline_test", "entryresume", plan, published=pubs)
    monkeypatch.setattr(studio_transcript_store, "load_corrected_transcript",
                        lambda *a, **k: pytest.fail("cleanup entry read transcript"))
    observed = []
    monkeypatch.setattr(worker, "_run_reedit_replace", lambda job, jid, transcript: observed.append((jid, transcript)))
    worker.run_auto_edit_only({"job_id": "entryresume", "project_id": "timeline_test",
        "config": {"replacement_mode": "delete_old_after_success"}})
    assert observed == [("entryresume", {})]


def test_hardlinks_count_only_actual_last_unlink_and_keep_external_source(sandbox):
    store.register_clips("timeline_test", "old2", [{"clip_id": "second"}])
    first = sandbox / "output" / "old" / "clips" / "clip-stable" / "clip_final.mp4"
    second = sandbox / "output" / "old2" / "other"; second.parent.mkdir(parents=True)
    os.link(first, second)
    protected = sandbox / "media" / "source"; protected.parent.mkdir(); protected.write_bytes(b"protected")
    external = first.parent / "source-link"; os.link(protected, external)
    old, plan, pubs = prepare("inodes")
    rr.activate_new_run("timeline_test", "inodes", plan, published=pubs)
    result = rr.purge_old_clips(old, delete_artifacts_fn=lambda ids: None)
    assert result["bytes_freed"] == len(b"old-video")
    assert result["files_deleted"] == 2 and result["paths_unlinked"] == 3
    assert protected.read_bytes() == b"protected"
    again = rr.retry_cleanup("timeline_test", "inodes", delete_artifacts_fn=lambda ids: None)
    assert again["bytes_freed"] == result["bytes_freed"]


def test_ffprobe_and_ffmpeg_timeout_are_isolated(tmp_path, monkeypatch):
    def timeout(*a, **k): raise subprocess.TimeoutExpired("synthetic", .01)
    monkeypatch.setattr(cr.subprocess, "run", timeout)
    assert cr._ffprobe_playable(tmp_path / "test")[0] is False
    with pytest.raises(RuntimeError, match="TimeoutExpired"):
        cr._run_ffmpeg([])
    source = tmp_path / "source"; source.write_bytes(b"synthetic")
    result = cr.render_plan({"clips": [{"clip_id": "a", "end_ms": 1000}, {"clip_id": "b", "end_ms": 1000}]},
        source=source, out_root=tmp_path / "out", corrected_segments=[], transcript_ref={})
    assert result["job_status"] == "technical_failed" and len(result["entries"]) == 2


def test_subprocess_timeout_kills_and_reaps_child(tmp_path, monkeypatch):
    actual = subprocess.run
    pidfile = tmp_path / "pid"
    def short_run(*a, **k):
        return actual([sys.executable, "-c",
            "import os,time,pathlib;pathlib.Path(" + repr(str(pidfile)) + ").write_text(str(os.getpid()));time.sleep(10)"],
            capture_output=True, text=True, timeout=.15)
    monkeypatch.setattr(cr.subprocess, "run", short_run)
    with pytest.raises(RuntimeError, match="TimeoutExpired"):
        cr._run_ffmpeg([])
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError): os.kill(pid, 0)


def test_force_render_timeout_preserves_preexisting_final(tmp_path, monkeypatch):
    final = tmp_path / "clip_final.mp4"; final.write_bytes(b"verified-existing")
    monkeypatch.setattr(cr, "_ffprobe_playable", lambda p: (False, "synthetic timeout"))
    monkeypatch.setattr(cr, "_run_ffmpeg", lambda *a: (_ for _ in ()).throw(RuntimeError("timeout")))
    with pytest.raises(cr.ClipRenderError, match="timeout"):
        cr.render_one({"clip_id": "stable", "end_ms": 1000}, source=tmp_path/"src", out_dir=tmp_path,
                      corrected_segments=[], transcript_ref={}, rule_snapshot_id=None, force=True)
    assert final.read_bytes() == b"verified-existing"


def test_activation_rejects_other_clips_published_file(sandbox):
    old, plan, pubs = prepare("own", ("clip-stable", "clip-other"))
    bad = {"clip_id": "clip-stable", "path": pubs[1]["path"]}
    before = store.manifest_path("timeline_test").read_bytes()
    with pytest.raises(rr.ReeditError):
        rr.activate_new_run("timeline_test", "own", plan, published=[bad])
    assert store.manifest_path("timeline_test").read_bytes() == before


@pytest.mark.parametrize("publish_fails", [False, True])
def test_worker_partial_and_all_publish_failure_are_consistent(sandbox, monkeypatch, publish_fails):
    from src import studio_job_worker as worker, content_analysis as ca, clip_planning as cp, r2
    import shutil
    run = "workerpartial" if not publish_fails else "workerpubfail"
    before = store.manifest_path("timeline_test").read_bytes()
    states, posts = [], []
    monkeypatch.setattr(worker, "_state", lambda jid, status, *args: states.append(status))
    monkeypatch.setattr(worker, "_http", lambda *args, **kwargs: {"artifacts":[]} if args[0]=="GET" else posts.append(args) or {})
    monkeypatch.setattr(worker, "_fenced_activation",lambda job,jid,plan,pubs:rr.activate_new_run(job["project_id"],jid,plan,published=pubs))
    monkeypatch.setattr(worker, "_materialise_source_for_job", lambda *args: sandbox/"synthetic-source")
    monkeypatch.setattr(worker, "_rule_snapshot_id", lambda: "synthetic")
    monkeypatch.setattr(ca, "load_analysis_record", lambda jid, **kwargs: None)
    monkeypatch.setattr(cp, "load_plan_record", lambda jid, **kwargs: None)
    monkeypatch.setattr(worker, "_run_content_analysis_stage", lambda *a: {})
    plan = {"clips": [{"clip_id": "clip-stable", "end_ms": 1000}, {"clip_id": "failed", "end_ms": 1000}]}
    monkeypatch.setattr(worker, "_run_clip_planning_stage", lambda *a: plan)
    def render(*a, **k):
        video = rr.staging_root(run)/"clips"/"clip-stable"/"clip_final.mp4"
        video.parent.mkdir(parents=True); video.write_bytes(b"synthetic-new")
        return {"job_status": "partial_succeeded", "entries": [
            {"clip_id": "clip-stable", "status": "rendered", "path": str(video)},
            {"clip_id": "failed", "status": "technical_error", "technical_error": "synthetic timeout"}],
            "counts": {"succeeded": 1, "technical_error": 1}, "warnings": []}
    monkeypatch.setattr(cr, "render_plan", render)
    monkeypatch.setattr(cr, "_ffprobe_playable", lambda p: (Path(p).is_file(), ""))
    monkeypatch.setattr(r2, "local_path_for", lambda key: sandbox/"objectstore"/str(key).removeprefix("local:"))
    def upload(path, key):
        if publish_fails: raise OSError("synthetic publish failure")
        target = r2.local_path_for(key); target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,target)
        return {"object_key": key,"size":Path(path).stat().st_size}
    monkeypatch.setattr(r2, "upload_file", upload)
    async def delete(ids): return 0
    monkeypatch.setattr("src.studio_job_store.delete_artifacts_for_jobs", delete)
    worker._run_reedit_replace({"project_id":"timeline_test","object_key":"synthetic"},run,{})
    ledger = store.load_manifest("timeline_test")
    if publish_fails:
        assert states[-1] == "technical_failed" and "activating_run" not in states
        assert store.manifest_path("timeline_test").read_bytes() == before
        assert (sandbox/"output"/"old").exists() and not rr.activation_done(run)
    else:
        assert set(ledger["clips"]) == {"clip-stable"}
        assert ledger["clips"]["clip-stable"]["render_task_id"] == run
        assert states[-1] == "completed_with_warnings"
        assert not (sandbox/"output"/"old").exists()
        assert len(posts) == 1
