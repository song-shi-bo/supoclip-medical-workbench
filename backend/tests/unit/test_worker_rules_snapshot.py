"""冻结计划来源→workflow snapshot→卡片版本，合成文件/模型桩。"""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from src import studio_job_worker as worker,studio_workflow as flow,skill_registry as registry,studio_project_store as store


RUN="a"*32


@pytest.fixture
def context(tmp_path,monkeypatch):
    root=tmp_path/"tasks"
    monkeypatch.setenv("STUDIO_TASKS_ROOT",str(root))
    monkeypatch.setattr(store,"TASKS_ROOT",root)
    manifest={"workflow_version":"frozen-1","clip_planning_rules":{"budget":7},
        "subtitle_rules":{"font":"synthetic-font"},"skills":[{"skill_id":"synthetic","enabled":True,
        "version":"1","checksum":"synthetic-sha","priority":1,"source_path":"/synthetic/rules.md"}]}
    monkeypatch.setattr(registry,"validate_manifest",lambda *a,**k:SimpleNamespace(ok=True,warnings=[],stale_skills=[]))
    latest=copy.deepcopy(manifest)
    reads=[]
    def load(*a,**k): reads.append(1);return copy.deepcopy(latest)
    monkeypatch.setattr(registry,"load_manifest",load)
    job={"job_id":RUN,"project_id":"timeline_snapshot","config":{"job_kind":"auto_edit"}}
    frozen=worker._frozen_rules(job)
    plan={"clips":[{"clip_id":"clip-1","start_ms":0,"end_ms":1000}],
          "source_provenance":{"rules_snapshot":{"external_snapshot":copy.deepcopy(frozen)}}}
    return root,job,plan,manifest,latest,reads


@pytest.mark.parametrize("task_id,layout",[(RUN,"output"),("autoedit_"+"b"*12,"output"),("cover_"+"c"*12,"localtasks")])
def test_uuid_and_legacy_layouts(tmp_path,task_id,layout):
    assert flow.validate_task_id(task_id)==task_id
    assert flow.TASK_ID_PATTERN.fullmatch(task_id).group(1)==(task_id.split("_")[0] if "_" in task_id else None)
    assert flow.snapshot_dir(tmp_path,task_id)==tmp_path/layout/task_id


@pytest.mark.parametrize("task_id",["../"+RUN,RUN+"/../other","A"*32,"a"*31,"a"*33,"autoedit_invalid"])
def test_invalid_ids_have_no_filesystem_effects(tmp_path,task_id):
    with pytest.raises(flow.WorkflowError): flow.snapshot_dir(tmp_path,task_id)
    assert list(tmp_path.iterdir())==[]


def test_plan_frozen_manifest_drives_snapshot_and_registered_card(context):
    root,job,plan,manifest,latest,reads=context
    latest["workflow_version"]="latest-99";latest["clip_planning_rules"]["budget"]=999
    # 模拟当前job内存已经重新计算过expected；历史展示仍消费计划实际来源。
    job["_rules_snapshot"]["manifest"]=copy.deepcopy(latest)
    before=json.dumps(plan,sort_keys=True)
    snapshot=worker._persist_workflow_snapshot(job,plan)
    assert snapshot["workflow_version"]==manifest["workflow_version"]
    assert snapshot["rules"]["clip_planning_rules"]["budget"]==7
    assert flow.read_snapshot(root,RUN)["workflow_version"]==manifest["workflow_version"]
    assert reads==[1] and json.dumps(plan,sort_keys=True)==before
    store.register_clips(job["project_id"],RUN,[{"clip_id":"clip-1"}])
    card=store.list_cards(job["project_id"])[0]
    assert card["workflow_version"]==manifest["workflow_version"]
    assert card["skill_versions"]=={"synthetic":"1"}
    assert Path(card["workflow_snapshot_path"])==root/"output"/RUN/registry.SNAPSHOT_FILENAME


def test_snapshot_reuse_does_not_rewrite_and_conflict_preserves_old_bytes(context):
    root,job,plan,*_=context
    worker._persist_workflow_snapshot(job,plan)
    path=root/"output"/RUN/registry.SNAPSHOT_FILENAME
    original=path.read_bytes();mtime=path.stat().st_mtime_ns
    worker._persist_workflow_snapshot(job,plan)
    assert path.read_bytes()==original and path.stat().st_mtime_ns==mtime
    changed=copy.deepcopy(plan)
    changed["source_provenance"]["rules_snapshot"]["external_snapshot"]["manifest"]["clip_planning_rules"]["budget"]=8
    with pytest.raises(RuntimeError,match="E_RULE_SNAPSHOT_CONFLICT"): worker._persist_workflow_snapshot(job,changed)
    assert path.read_bytes()==original and path.stat().st_mtime_ns==mtime


def test_unverified_plan_does_not_get_latest_snapshot(context):
    root,job,plan,manifest,latest,reads=context
    assert worker._persist_workflow_snapshot(job,{"clips":[]}) is None
    assert not (root/"output").exists() and reads==[1]


def test_replan_snapshot_uses_current_generation_uuid(context):
    root,job,plan,*_=context
    job["config"]["_lifecycle"]={"render_generation":1}
    run_id=worker._job_run_id(job)
    assert run_id!=RUN and len(run_id)==32
    worker._persist_workflow_snapshot(job,plan)
    assert flow.read_snapshot(root,run_id)["task_id"]==run_id
    assert not (root/"output"/RUN).exists()


def test_plan_stage_persists_before_artifact_and_leaves_cache_fingerprint_unchanged(context,monkeypatch):
    from src import clip_planning as cp,content_analysis as ca
    root,job,plan,manifest,latest,reads=context
    transcript={"asr_segments":[{"segment_id":"s1","start_ms":0,"end_ms":1000,"text":"synthetic"}],"transcript_version":1}
    analysis={"plan_source":"llm","keywords":[],"golden_quotes":[],"topic_units":[],"candidate_intervals":[]}
    monkeypatch.setattr(cp,"_model_ready",lambda:(False,"synthetic-model","disabled"))
    monkeypatch.setattr(cp,"plan_clips",lambda *a,**k:{"plan_source":"llm","clips":[]})
    monkeypatch.setattr(worker,"_state",lambda *a,**k:None)
    monkeypatch.setattr(worker,"_http",lambda *a,**k:{})
    def publish(path,key):
        assert flow.read_snapshot(root,RUN)["workflow_version"]==manifest["workflow_version"]
        return {"object_key":key,"size":Path(path).stat().st_size}
    monkeypatch.setattr(worker,"_guarded_upload_file",publish)
    expected=cp.build_current_plan_provenance(transcript["asr_segments"],analysis,transcript_version=1,
        rule_snapshot_id=manifest["workflow_version"],rules_snapshot=job["_rules_snapshot"])
    record=worker._run_clip_planning_stage(job,RUN,transcript,analysis)
    assert record["source_provenance"]==expected
    assert cp.load_plan_record(RUN,expected_provenance=expected,require_verified=True) is not None
