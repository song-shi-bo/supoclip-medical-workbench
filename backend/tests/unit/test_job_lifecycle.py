"""隔离事务模拟验证生命周期调用；不声明真实PostgreSQL/Redis证据。"""
import asyncio
import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
import pytest
from src import studio_job_store as store


JOB="a"*32


class Result:
    def __init__(self,rows=(),count=0): self.rows=list(rows);self.rowcount=count
    def mappings(self): return self
    def first(self): return self.rows[0] if self.rows else None
    def all(self): return self.rows


class TransactionDB:
    def __init__(self,job):
        self.jobs={JOB:copy.deepcopy(job)};self.artifacts=[];self.uploads={};self.lock=asyncio.Lock()
        self.events=[];self.fail_insert=False
    def session(self): return Session(self)


class Session:
    def __init__(self,db): self.db=db
    async def __aenter__(self):
        await self.db.lock.acquire();self.snapshot=(copy.deepcopy(self.db.jobs),copy.deepcopy(self.db.artifacts),copy.deepcopy(self.db.uploads));self.committed=False
        return self
    async def __aexit__(self,kind,exc,tb):
        if exc or not self.committed: self.db.jobs,self.db.artifacts,self.db.uploads=self.snapshot
        self.db.lock.release()
    async def commit(self): self.committed=True;self.db.events.append("commit")
    async def execute(self,statement,params=None):
        q=" ".join(str(statement).split());p=params or {};self.db.events.append(q)
        if q.startswith("SELECT pg_advisory"): return Result()
        if "studio_upload_sessions" in q:
            if q.startswith("INSERT"):
                if self.db.fail_insert: raise RuntimeError("synthetic database failure")
                self.db.uploads[p["id"]]={**p,"status":"open"};return Result(count=1)
            if q.startswith("SELECT"):
                row=self.db.uploads.get(p["id"]);return Result([copy.deepcopy(row)] if row else [])
            if q.startswith("UPDATE"):
                self.db.uploads[p["id"]]["status"]="completed";return Result(count=1)
        if q.startswith("SELECT") and "FROM studio_jobs" in q:
            if "ORDER BY created_at" in q:
                rows=[j for j in self.db.jobs.values() if j["status"] in {"queued","reedit_requested"} or
                      j["status"] not in store.TERMINAL_STATUSES and not j.get("lease_alive",False)]
                return Result(rows[:1])
            row=self.db.jobs.get(p.get("id"))
            if row and "worker_id=:worker_id" in q and not self.valid(row,p): row=None
            return Result([copy.deepcopy(row)] if row else [])
        if q.startswith("UPDATE studio_jobs"):
            row=self.db.jobs.get(p["id"])
            if row is None or "worker_id=:worker_id" in q and "WHERE id=:id AND worker_id" in q and not self.valid(row,p): return Result(count=0)
            if "config" in p: row["config"]=json.loads(p["config"])
            for key in ("status","stage","progress","error"):
                if key in p: row[key]=p[key]
            if "attempts=attempts+1" in q:
                row["attempts"]+=1;row["worker_id"]=p["worker_id"];row["lease_alive"]=True
                row["status"]="downloading"
            if "worker_id=NULL" in q or p.get("terminal") is True:
                row["worker_id"]=None;row["lease_alive"]=False
            if "status='failed'" in q: row["status"]="failed"
            return Result(count=1)
        if q.startswith("INSERT INTO studio_jobs"):
            if self.db.fail_insert: raise RuntimeError("synthetic database failure")
            self.db.jobs[p["id"]]={**p,"config":json.loads(p["config"]),"attempts":0,"status":"queued","worker_id":None,"lease_alive":False}
            return Result(count=1)
        if q.startswith("SELECT") and "FROM studio_artifacts" in q:
            rows=[a for a in self.db.artifacts if a["job_id"]==p["job_id"]]
            if "kind=:kind" in q:
                rows=[a for a in rows if a["kind"]==p["kind"] and a["clip_id"]==p["clip_id"] and a["object_key"]==p["object_key"]]
                return Result([(a["id"],) for a in rows])
            return Result(rows)
        if q.startswith("INSERT INTO studio_artifacts"):
            self.db.artifacts.append(copy.deepcopy(p));return Result(count=1)
        raise AssertionError("Unhandled synthetic SQL: "+q)
    def valid(self,row,p):
        return (row.get("worker_id")==p.get("worker_id") and row.get("lease_alive",False)
            and str(store.lifecycle(row).get("lease_token") or "")==str(p.get("lease_token") or "")
            and row["status"] not in store.TERMINAL_STATUSES and row["status"] not in {"queued","reedit_requested"})


class Access:
    DOMAIN_ID="synthetic-domain"
    def __init__(self,source): self.source=source;self.reserved=set();self.renewed=[];self.registered=[]
    def AccessLedger(self): return self
    def ledger(self): return self
    def current_principal(self): return {"subject":"owner","domain_id":self.DOMAIN_ID}
    def require(self,*args,**kwargs): return {"owner":"owner"}
    def get(self,kind,identifier):
        return {"owner":"owner","project_id":"timeline_test","bytes":self.source.stat().st_size}
    def reserve(self,principal,key,size,**kwargs): self.reserved.add(key)
    def renew(self,key): self.renewed.append(key)
    def reserve_job_operation(self,key,project,source): self.reserved.add(key)
    def release_operation(self,key): self.reserved.discard(key)
    def renew_operation(self,key): self.renewed.append(key)
    def register_created_object(self,*args,**kwargs): self.registered.append(args)


@pytest.fixture
def backend(tmp_path,monkeypatch):
    source=tmp_path/"source";source.write_bytes(b"synthetic-source")
    job={"id":JOB,"project_id":"timeline_test","object_key":"local:"+str(source),"object_size":16,
         "config":{"job_kind":"auto_edit","_lifecycle":{"generation":1,"lease_token":"old-token"}},
         "status":"technical_failed","stage":"rendering","progress":1,"error":"synthetic","attempts":2,
         "worker_id":None,"lease_alive":False}
    from src import r2
    monkeypatch.setattr(r2,"is_r2_enabled",lambda:False)
    monkeypatch.setattr(r2,"local_path_for",lambda key:Path(key.removeprefix("local:")))
    db=TransactionDB(job);access=Access(source);enqueued=[];events=[]
    monkeypatch.setattr(store,"get_session_maker",lambda:db.session)
    monkeypatch.setattr(store,"_access",lambda:access)
    monkeypatch.setattr(store,"_is_activated",lambda job:bool(store.lifecycle(job).get("activated_run_id")))
    async def enqueue(jid): enqueued.append(jid)
    async def publish(*args): events.append(args)
    monkeypatch.setattr(store,"_enqueue",enqueue);monkeypatch.setattr(store,"publish_event",publish)
    return db,access,enqueued,events,source


def test_duplicate_retry_and_different_body_conflict(backend):
    db,access,queue,events,source=backend
    async def run():
        key=uuid.uuid4()
        one,two=await asyncio.gather(store.request_job_operation(JOB,"resume_render",key),store.request_job_operation(JOB,"resume_render",key))
        assert one["accepted"] and two["accepted"] and sum(bool(x.get("reused")) for x in (one,two))==1
        before=copy.deepcopy(db.jobs)
        with pytest.raises(store.JobLifecycleError) as caught: await store.request_job_operation(JOB,"replan",key)
        assert caught.value.code=="E_IDEMPOTENCY_CONFLICT" and db.jobs==before
    asyncio.run(run())
    assert queue==[JOB] and db.jobs[JOB]["attempts"]==2 and source.read_bytes()==b"synthetic-source"


def test_partial_failure_claim_and_old_fence_rejected(backend):
    db,access,queue,events,source=backend
    db.jobs[JOB]["status"]="partial_succeeded"
    async def run():
        await store.request_job_operation(JOB,"resume_render",uuid.uuid4())
        claimed=await store.claim_next_job("same-worker")
        new=store.lifecycle(claimed)["lease_token"]
        assert new!="old-token" and claimed["attempts"]==3
        before=copy.deepcopy(db.jobs);prior=len(access.renewed)
        assert not await store.renew_lease(JOB,"same-worker","old-token")
        assert len(access.renewed)==prior and db.jobs==before
        assert await store.renew_lease(JOB,"same-worker",new)
        assert await store.update_progress(JOB,"rendering","synthetic",.5,worker_id="same-worker",lease_token=new)
    asyncio.run(run())
    assert queue==[JOB] and access.registered[-1][0]=="task"


def test_cancel_fences_late_progress_artifact_activation_and_renew(backend,monkeypatch):
    db,access,queue,events,source=backend
    job=db.jobs[JOB];job.update(status="rendering",worker_id="worker",lease_alive=True)
    access.reserved.add(JOB)
    from src import reedit_replace
    calls=[];monkeypatch.setattr(reedit_replace,"activate_new_run",lambda *a,**k:calls.append(a))
    async def run():
        result=await store.request_job_operation(JOB,"cancel",uuid.uuid4())
        assert result["status"]=="cancelled" and JOB not in access.reserved
        before=copy.deepcopy(db.jobs)
        assert not await store.update_progress(JOB,"completed","late",1,worker_id="worker",lease_token="old-token")
        assert await store.save_artifact(JOB,"clip","synthetic",1,worker_id="worker",lease_token="old-token") is None
        assert not await store.renew_lease(JOB,"worker","old-token")
        with pytest.raises(store.JobLifecycleError): await store.activate_job_run(JOB,"worker","old-token",{},[])
        assert db.jobs==before and db.artifacts==[] and calls==[]
    asyncio.run(run())


def test_cleanup_only_activation_cannot_be_cancelled_or_replanned(backend):
    db,access,queue,events,source=backend
    row=db.jobs[JOB];row["status"]="completed_with_cleanup_warning"
    row["config"]["_lifecycle"]["activated_run_id"]=JOB
    source.unlink()  # 合成源片缺失不阻止已激活清理。
    async def run():
        assert store.job_capabilities(row)["available_actions"]==["cleanup_only"]
        with pytest.raises(store.JobLifecycleError): await store.request_job_operation(JOB,"replan",uuid.uuid4())
        result=await store.request_job_operation(JOB,"cleanup_only",uuid.uuid4())
        assert result["accepted"] and store.lifecycle(db.jobs[JOB])["retry_mode"]=="cleanup_only"
    asyncio.run(run())


def test_replan_new_generation_uses_locator_compatible_uuid(backend):
    db,*_=backend
    async def run(): await store.request_job_operation(JOB,"replan",uuid.uuid4())
    asyncio.run(run())
    run_id=store.render_run_id(db.jobs[JOB])
    assert run_id!=JOB and len(run_id)==32 and int(run_id,16)>0
    assert db.jobs[JOB]["attempts"]==2


def test_source_missing_rejection_has_no_business_changes(backend):
    db,access,queue,events,source=backend;source.unlink();before=copy.deepcopy(db.jobs)
    async def run():
        with pytest.raises(store.JobLifecycleError) as caught: await store.request_job_operation(JOB,"resume_render",uuid.uuid4())
        assert caught.value.status_code==410
    asyncio.run(run())
    assert db.jobs==before and queue==events==[] and access.reserved==set()


def test_create_database_failure_releases_reserved_quota(backend):
    db,access,queue,events,source=backend;db.fail_insert=True
    async def run():
        with pytest.raises(RuntimeError,match="synthetic database failure"):
            await store.create_job("timeline_test","local:"+str(source),16,{"job_kind":"auto_edit"},job_id="b"*32)
    asyncio.run(run())
    assert access.reserved==set() and "b"*32 not in db.jobs and queue==[]


def test_terminal_releases_quota_but_unknown_usage_not_zero(backend):
    db,access,queue,events,source=backend
    db.jobs[JOB].update(status="rendering",worker_id="worker",lease_alive=True);access.reserved.add(JOB)
    async def run():
        assert await store.update_progress(JOB,"partial_succeeded","partial",1,worker_id="worker",lease_token="old-token",
            metadata={"stage_sources":{"planning":"mixed"},"degradation_reasons":["one-window-fallback"],"llm_usage":{"status":"unknown","total_tokens":None}})
    asyncio.run(run())
    fields=store.job_capabilities(db.jobs[JOB])
    assert JOB not in access.reserved and fields["stage_sources"]["planning"]=="mixed"
    assert fields["llm_usage"]["total_tokens"] is None and fields["available_actions"]==["resume_render","replan"]


def test_post_activation_cancel_rejected_under_same_row_lock(backend,monkeypatch):
    db,access,queue,events,source=backend;db.jobs[JOB].update(status="activating_run",worker_id="worker",lease_alive=True)
    db.jobs[JOB]["config"]["replacement_mode"]="delete_old_after_success"
    from src import reedit_replace
    monkeypatch.setattr(reedit_replace,"activate_new_run",lambda *a,**k:{"clips":{"stable":{}}})
    async def run():
        result=await store.activate_job_run(JOB,"worker","old-token",{},[])
        assert result["accepted"]
        with pytest.raises(store.JobLifecycleError): await store.request_job_operation(JOB,"cancel",uuid.uuid4())
        assert db.jobs[JOB]["status"]=="activating_run"
    asyncio.run(run())


def test_http_retry_cancel_contract_and_internal_fence(backend,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.api.routes import jobs
    db,*_=backend
    app=FastAPI();app.include_router(jobs.router)
    monkeypatch.setattr(jobs,"_WORKER_SECRET","synthetic-secret")
    async def offline(): return False
    async def artifacts(job_id): return []
    monkeypatch.setattr(jobs,"worker_online",offline)
    monkeypatch.setattr(jobs,"list_artifacts",artifacts)
    key=str(uuid.uuid4())
    with TestClient(app) as client:
        state=client.get(f"/api/jobs/{JOB}")
        assert state.status_code==200 and state.json()["available_actions"]==["resume_render","replan"]
        assert state.json()["stage_sources"]["planning"]=="unknown"
        assert client.post(f"/api/jobs/{JOB}/retry",json={}).status_code==422
        response=client.post(f"/api/jobs/{JOB}/retry",json={"mode":"resume_render","idempotency_key":key})
        assert response.status_code==200 and response.json()["accepted"]
        assert client.post(f"/api/jobs/{JOB}/retry",json={"mode":"resume_render","idempotency_key":key}).json()["reused"]
        assert client.post(f"/api/jobs/{JOB}/retry",json={"mode":"replan","idempotency_key":key}).status_code==409
        assert client.get(f"/api/internal/jobs/{JOB}/artifacts").status_code==401
        headers={"X-Worker-Secret":"synthetic-secret","X-Worker-Id":"worker","X-Worker-Lease":"old-token"}
        assert client.get(f"/api/internal/jobs/{JOB}/artifacts",headers=headers).status_code==409
        assert client.post(f"/api/jobs/{JOB}/cancel",json={"idempotency_key":str(uuid.uuid4())}).status_code==200
        assert client.post(f"/api/internal/jobs/{JOB}/activate",json={"plan_record":{},"published":[]},headers=headers).status_code==409


def test_real_isolated_access_ledger_owner_quota_and_old_lease(backend,tmp_path,monkeypatch):
    from src import studio_access as access
    from fastapi import HTTPException
    db,_,queue,events,source=backend
    ledger=access.AccessLedger(tmp_path/"acl.sqlite3")
    owner={"subject":"synthetic-owner","domain_id":access.DOMAIN_ID,"expires_at_ms":9999999999999}
    editor={"subject":"synthetic-editor","domain_id":access.DOMAIN_ID}
    principal_token=access._principal.set(editor);ledger_token=access._access.set(ledger)
    monkeypatch.setattr(store,"_access",lambda:access)
    try:
        ledger.register("project","timeline_test",owner)
        ledger.grant(owner,"project","timeline_test",editor["subject"],"editor")
        ledger.register("source",db.jobs[JOB]["object_key"],owner,project_id="timeline_test",size_bytes=source.stat().st_size)
        ledger.register("job",JOB,editor,project_id="timeline_test")
        async def run():
            await store.request_job_operation(JOB,"resume_render",uuid.uuid4())
            row=await store.claim_next_job("worker")
            fence=store.lifecycle(row)["lease_token"]
            with ledger.connect() as conn:
                reservation=dict(conn.execute("SELECT * FROM reservations WHERE operation_id=?",(JOB,)).fetchone())
            assert reservation["subject"]==owner["subject"] and reservation["active"]==1
            before=reservation["expires_ms"]
            assert not await store.renew_lease(JOB,"worker","old-token")
            with ledger.connect() as conn:
                assert conn.execute("SELECT expires_ms FROM reservations WHERE operation_id=?",(JOB,)).fetchone()[0]==before
            assert await store.update_progress(JOB,"partial_succeeded","synthetic",1,worker_id="worker",lease_token=fence)
            with ledger.connect() as conn:
                assert conn.execute("SELECT active FROM reservations WHERE operation_id=?",(JOB,)).fetchone()[0]==0
        asyncio.run(run())
        ledger.grant(owner,"project","timeline_test",editor["subject"],"editor",revoke=True)
        with pytest.raises(HTTPException): ledger.require(editor,"job",JOB,write=True)
        assert ledger.get("job",JOB)["owner"]==owner["subject"]
    finally:
        access._principal.reset(principal_token);access._access.reset(ledger_token)


def test_worker_rejects_mismatched_trusted_parent(backend,monkeypatch):
    db,access,*_=backend
    original=access.get
    def get(kind,key):
        row=original(kind,key)
        if kind=="job": row["project_id"]="different-project"
        return row
    monkeypatch.setattr(access,"get",get)
    with pytest.raises(store.JobLifecycleError,match="可信项目"):
        store._worker_quota(db.jobs[JOB])
    assert access.reserved==set()


def test_upload_sql_failure_releases_operation(backend):
    db,access,*_=backend;db.fail_insert=True
    async def run():
        with pytest.raises(RuntimeError,match="synthetic database failure"):
            await store.create_upload_session("timeline_test","synthetic-source","s.mp4",16,8,2,"synthetic-upload",session_id="b"*32)
    asyncio.run(run())
    assert access.reserved==set() and db.uploads=={}


def test_r2_upload_hooks_real_ledger_verified_bytes_and_failure_release(backend,tmp_path,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src import studio_access as access
    from src.api.routes import jobs
    db,*_=backend
    ledger=access.AccessLedger(tmp_path/"uploads.sqlite3")
    owner={"subject":"synthetic-owner","domain_id":access.DOMAIN_ID,"expires_at_ms":9999999999999}
    principal_token=access._principal.set(owner);ledger_token=access._access.set(ledger)
    monkeypatch.setattr(store,"_access",lambda:access)
    ledger.register("project","timeline_test",owner)
    calls=[]
    monkeypatch.setattr(jobs.r2,"is_r2_enabled",lambda:True)
    monkeypatch.setattr(jobs.r2,"part_size_bytes",lambda:8)
    def create(key): calls.append(key);return "synthetic-upload"
    monkeypatch.setattr(jobs.r2,"r2_create_multipart",create)
    monkeypatch.setattr(jobs.r2,"r2_presign_part",lambda *a:"https://synthetic.invalid/part")
    monkeypatch.setattr(jobs.r2,"r2_complete_multipart",lambda *a:None)
    monkeypatch.setattr(jobs.r2,"r2_head",lambda *a:{"size":16})
    app=FastAPI();app.include_router(jobs.router)
    try:
        with TestClient(app) as client:
            assert client.post("/api/uploads",json={"project_id":"unknown","filename":"s.mp4","size_bytes":16}).status_code==403
            assert calls==[] and db.uploads=={}
            reply=client.post("/api/uploads",json={"project_id":"timeline_test","filename":"s.mp4","size_bytes":16})
            assert reply.status_code==200
            data=reply.json();sid=data["upload_id"];key=data["object_key"]
            assert ledger.get("upload",sid)["owner"]==owner["subject"] and ledger.get("source",key) is None
            complete=client.post(f"/api/uploads/{sid}/complete",json={"parts":[{"index":0,"etag":"one"},{"index":1,"etag":"two"}]})
            assert complete.status_code==200 and db.uploads[sid]["status"]=="completed"
            registered=ledger.get("source",key)
            assert registered["bytes"]==16 and registered["project_id"]=="timeline_test"
            with ledger.connect() as conn:
                assert conn.execute("SELECT active FROM reservations WHERE operation_id=?",("upload:"+sid,)).fetchone()[0]==0
    finally:
        access._principal.reset(principal_token);access._access.reset(ledger_token)
