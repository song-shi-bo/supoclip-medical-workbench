"""当前输入expected接线与发布复用，纯合成文件/模型桩/HTTP桩。"""
import copy
import hashlib
import json
from pathlib import Path
import pytest
from src import content_analysis as ca,clip_planning as cp,studio_job_worker as worker,r2


@pytest.fixture
def isolated(tmp_path,monkeypatch):
    monkeypatch.setenv("STUDIO_TASKS_ROOT",str(tmp_path/"tasks"))
    monkeypatch.setattr(ca,"_model_ready",lambda:(False,"synthetic-model","synthetic-disabled"))
    monkeypatch.setattr(cp,"_model_ready",lambda:(False,"synthetic-model","synthetic-disabled"))
    monkeypatch.setattr(worker,"_CURRENT_JOB",{"id":"a"*32,"lease_token":"synthetic-fence","run_id":"a"*32,"metadata":{}})
    rule={"manifest":{"workflow_version":"v1","clip_planning_rules":{"value":1}},"rules":{"value":1}}
    monkeypatch.setattr(worker,"_frozen_rules",lambda job:copy.deepcopy(rule))
    segments=[{"segment_id":"s1","start_ms":0,"end_ms":1000,"text":"合成测试完整句。"}]
    transcript={"asr_segments":segments,"transcript_id":"synthetic-tr","transcript_version":1}
    job={"job_id":"a"*32,"project_id":"timeline_test","object_key":"synthetic",
         "config":{"job_kind":"auto_edit","_lifecycle":{"render_generation":0}}}
    return job,transcript,rule


def save_analysis(job,transcript,rule,source="llm",generation=0):
    provenance=ca.build_current_analysis_provenance(transcript["asr_segments"],transcript_version=transcript["transcript_version"],rules_snapshot=rule)
    record={"plan_source":source,"keywords":[],"golden_quotes":[],"topic_units":[],"candidate_intervals":[],"plan_generation":generation}
    ca.save_analysis_record(job["job_id"],record,provenance=provenance)
    return ca.load_analysis_record(job["job_id"],expected_provenance=provenance,require_verified=True)


def test_analysis_expected_rebuilt_from_current_rules_and_revision(isolated):
    job,transcript,rule=isolated
    save_analysis(job,transcript,rule)
    assert worker._analysis_cache(job,job["job_id"],transcript)["plan_source"]=="llm"
    rule["rules"]["value"]=2
    assert worker._analysis_cache(job,job["job_id"],transcript) is None
    rule["rules"]["value"]=1;transcript["transcript_version"]=2
    assert worker._analysis_cache(job,job["job_id"],transcript) is None


def test_resume_requires_verified_cache_and_replan_generations_bypass_old_rules_cache(isolated):
    job,transcript,rule=isolated
    save_analysis(job,transcript,rule,source="heuristic_fallback")
    state=job["config"]["_lifecycle"];state["retry_mode"]="resume_render"
    assert worker._analysis_cache(job,job["job_id"],transcript)["plan_source"]=="heuristic_fallback"
    rule["rules"]["value"]=3
    with pytest.raises(RuntimeError,match="E_CACHE_CONTEXT_CHANGED"):
        worker._analysis_cache(job,job["job_id"],transcript)
    rule["rules"]["value"]=1;state.update(retry_mode="replan",render_generation=1)
    assert worker._analysis_cache(job,job["job_id"],transcript) is None
    save_analysis(job,transcript,rule,source="llm",generation=1)
    assert worker._analysis_cache(job,job["job_id"],transcript)["plan_source"]=="llm"


def test_plan_expected_current_analysis_rules_not_cached_provenance(isolated):
    job,transcript,rule=isolated
    analysis=save_analysis(job,transcript,rule)
    expected=cp.build_current_plan_provenance(transcript["asr_segments"],analysis,
        transcript_version=1,rule_snapshot_id="v1",rules_snapshot=rule)
    record={"clips":[],"plan_source":"mixed","plan_generation":0,"degradations":["synthetic-window-fallback"]}
    cp.save_plan_record(job["job_id"],record,provenance=expected)
    cached=worker._plan_cache(job,job["job_id"],transcript,analysis)
    assert cached["plan_source"]=="mixed" and worker._CURRENT_JOB["metadata"]["stage_sources"]["planning"]=="mixed"
    changed=copy.deepcopy(analysis);changed["keywords"]=["new-context"]
    assert worker._plan_cache(job,job["job_id"],transcript,changed) is None


def test_unverified_old_analysis_is_not_reusable(isolated):
    job,transcript,rule=isolated
    path=ca.analysis_root()/f"{job['job_id']}.json";path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"plan_source":"llm","keywords":[],"golden_quotes":[]}))
    assert worker._analysis_cache(job,job["job_id"],transcript) is None


def test_complete_actual_rules_snapshot_frozen_once(tmp_path,monkeypatch):
    from src import skill_registry
    rule=tmp_path/"rules.md";rule.write_text("synthetic rules")
    manifest={"workflow_version":"actual-version","clip_planning_rules":{"budget":7},
              "highlight_selection_rules":{"soft":True},"skills":[{"source_path":str(rule)}]}
    monkeypatch.setattr(skill_registry,"load_manifest",lambda:manifest)
    monkeypatch.setattr(skill_registry,"enabled_skills",lambda m:m["skills"])
    job={};snapshot=worker._frozen_rules(job)
    assert snapshot["manifest"]["clip_planning_rules"]=={"budget":7}
    assert snapshot["enabled_source_sha256"][str(rule)]==hashlib.sha256(rule.read_bytes()).hexdigest()
    manifest["clip_planning_rules"]["budget"]=99
    assert worker._frozen_rules(job)["manifest"]["clip_planning_rules"]["budget"]==7


def test_reedit_existing_video_and_srt_not_copied_or_registered_again(tmp_path,monkeypatch):
    source=tmp_path/"clips"/"stable"/"clip_final.mp4";source.parent.mkdir(parents=True);source.write_bytes(b"synthetic-video")
    srt=source.parent/"subtitles.srt";srt.write_text("synthetic-srt")
    objects=tmp_path/"objects";objects.mkdir()
    artifacts=[]
    for kind,path in (("clip",source),("srt",srt)):
        dest=objects/path.name;dest.write_bytes(path.read_bytes())
        artifacts.append({"kind":kind,"clip_id":"stable","object_key":str(dest),"sha256":hashlib.sha256(path.read_bytes()).hexdigest()})
    posts=[]
    monkeypatch.setattr(worker,"_http",lambda method,url,*a,**k:{"artifacts":artifacts} if method=="GET" else posts.append(a))
    monkeypatch.setattr(r2,"local_path_for",lambda key:Path(key))
    monkeypatch.setattr(r2,"upload_file",lambda *a,**k:pytest.fail("successful publication copied twice"))
    from src import clip_render
    monkeypatch.setattr(clip_render,"_ffprobe_playable",lambda path:(True,""))
    result=worker._upload_reedit_clip_artifacts("a"*32,{"entries":[{"clip_id":"stable","status":"skipped_playable","path":str(source)}]})
    assert result[0]["reused"] and posts==[]


def test_reused_video_repairs_only_missing_srt(tmp_path,monkeypatch):
    source=tmp_path/"stable"/"clip_final.mp4";source.parent.mkdir();source.write_bytes(b"synthetic-video")
    (source.parent/"subtitles.srt").write_text("synthetic-srt")
    dest=tmp_path/"published.mp4";dest.write_bytes(source.read_bytes())
    artifact={"kind":"clip","clip_id":"stable","object_key":str(dest),"sha256":hashlib.sha256(source.read_bytes()).hexdigest()}
    calls=[];posts=[]
    monkeypatch.setattr(worker,"_http",lambda method,url,*a,**k:{"artifacts":[artifact]} if method=="GET" else posts.append(a) or {})
    monkeypatch.setattr(r2,"local_path_for",lambda key:Path(key) if str(key).startswith("/") else tmp_path/"objects"/key)
    def upload(path,key):
        calls.append(Path(path).name);target=r2.local_path_for(key);target.parent.mkdir(parents=True);target.write_bytes(Path(path).read_bytes())
        return {"object_key":str(target),"size":target.stat().st_size}
    monkeypatch.setattr(r2,"upload_file",upload)
    from src import capacity_guard,clip_render
    monkeypatch.setattr(capacity_guard,"free_bytes",lambda _:10*1024**3)
    guard=tmp_path/"guard";guard.mkdir();monkeypatch.setattr(capacity_guard,"_reservation_paths",lambda device:(guard/"guard.lock",guard/"guard.json"))
    monkeypatch.setattr(clip_render,"_ffprobe_playable",lambda path:(True,""))
    worker._upload_reedit_clip_artifacts("a"*32,{"entries":[{"clip_id":"stable","status":"rendered","path":str(source)}]})
    assert calls==["subtitles.srt"] and len(posts)==1


def test_actual_activation_always_requires_server_acceptance(monkeypatch):
    monkeypatch.setattr(worker,"_http",lambda *a,**k:{"accepted":False})
    with pytest.raises(RuntimeError,match="E_LEASE_LOST"):
        worker._fenced_activation({"project_id":"timeline_test"},"a"*32,{},[])


def test_transcript_upload_failure_resumes_successful_asr_and_correction(tmp_path,monkeypatch):
    from src import studio_transcript_store as transcripts
    monkeypatch.setenv("STUDIO_TASKS_ROOT",str(tmp_path/"tasks"))
    source=tmp_path/"source.mp4";source.write_bytes(b"synthetic")
    job={"job_id":"a"*32,"project_id":"timeline_test","object_key":"synthetic","config":{"job_kind":"transcribe_only"}}
    calls={"asr":0,"correct":0,"save":0,"upload":0}
    monkeypatch.setattr(worker,"_state",lambda *a,**k:None)
    monkeypatch.setattr(worker,"_materialise_source_for_job",lambda *a:source)
    monkeypatch.setattr(worker,"_assert_audio_stream",lambda *a:None)
    segments=[{"text":"synthetic","start":0,"end":1}]
    def asr(*a): calls["asr"]+=1;return segments,{"language":"zh"}
    def correct(*a): calls["correct"]+=1;return {"asr_segments":segments,"correction_summary":{"engine":"dictionary"}}
    def save(*a,**k): calls["save"]+=1;return {"transcript_id":"synthetic","transcript_version":1}
    def upload(*a):
        calls["upload"]+=1
        if calls["upload"]==1: raise OSError("synthetic publication failure")
    monkeypatch.setattr(worker,"stage_transcribing",asr);monkeypatch.setattr(worker,"stage_correcting",correct)
    monkeypatch.setattr(transcripts,"save_corrected_transcript",save)
    monkeypatch.setattr(worker,"_upload_transcript_artifact",upload)
    with pytest.raises(OSError): worker.run_transcribe_only(job)
    worker.run_transcribe_only(job)
    assert calls=={"asr":1,"correct":1,"save":1,"upload":2}


def test_invalid_published_video_is_not_reused(tmp_path,monkeypatch):
    from src import clip_render
    source=tmp_path/"source.mp4";source.write_bytes(b"synthetic")
    copied=tmp_path/"copied.mp4";copied.write_bytes(source.read_bytes())
    monkeypatch.setattr(worker,"_http",lambda *a,**k:{"artifacts":[{"kind":"clip","clip_id":"stable","object_key":str(copied),"sha256":hashlib.sha256(source.read_bytes()).hexdigest()}]})
    monkeypatch.setattr(r2,"local_path_for",lambda key:Path(key))
    monkeypatch.setattr(clip_render,"_ffprobe_playable",lambda p:(False,"synthetic-unplayable"))
    assert worker._existing_published("a"*32,"clip","stable",source) is None


def test_correction_sources_and_unknown_usage_are_explicit(monkeypatch):
    monkeypatch.setattr(worker,"_CURRENT_JOB",{"metadata":{}})
    worker._stage_metadata("correction",{"engine":"local-conservative-rules","llm_batches":0,"fallback_batches":2})
    metadata=worker._CURRENT_JOB["metadata"]
    assert metadata["stage_sources"]["correction"]=="rules" and metadata["llm_usage"]["total_tokens"] is None
    worker._stage_metadata("correction",{"engine":"dsh-llm:synthetic+local-fallback","llm_batches":2,"fallback_batches":1})
    assert metadata["stage_sources"]["correction"]=="mixed"
    assert metadata["degradation_reasons"]


def test_stage_usage_accumulates_without_relabeling_unknown(monkeypatch):
    monkeypatch.setattr(worker,"_CURRENT_JOB",{"metadata":{}})
    worker._stage_metadata("analysis",{"plan_source":"llm","llm_usage":{"status":"known","total_tokens":10}})
    worker._stage_metadata("planning",{"plan_source":"llm","llm_usage":{"status":"known","total_tokens":20}})
    usage=worker._CURRENT_JOB["metadata"]["llm_usage"]
    assert usage["status"]=="known" and usage["total_tokens"]==30 and usage["by_stage"]["planning"]["total_tokens"]==20
    worker._stage_metadata("analysis",{"plan_source":"llm","llm_usage":{"status":"unknown","total_tokens":None}})
    assert worker._CURRENT_JOB["metadata"]["llm_usage"]["total_tokens"] is None


def test_analysis_result_cannot_relabel_mismatched_actual_provenance(isolated,monkeypatch):
    job,transcript,rule=isolated
    monkeypatch.setattr(worker,"_state",lambda *a,**k:None)
    monkeypatch.setattr(ca,"analyze_content",lambda *a,**k:{"plan_source":"llm","source_provenance":{"foreign":True}})
    with pytest.raises(RuntimeError,match="E_CACHE_CONTEXT_CHANGED"):
        worker._run_content_analysis_stage(job,job["job_id"],transcript)
    assert not (ca.analysis_root()/f"{job['job_id']}.json").exists()
