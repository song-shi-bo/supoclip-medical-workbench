"""仅合成主体、独立SQLite和真实测试UDS；无生产会话。"""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from src import studio_access as sa


def principal(subject="fictional-a"):
    return {"subject": subject, "slug": subject, "domain_id": sa.DOMAIN_ID, "expires_at_ms": int(time.time()*1000)+60000}


@pytest.fixture
def acl(tmp_path):
    return sa.AccessLedger(tmp_path / "acl.sqlite")


def assert_status(status, callback):
    with pytest.raises(HTTPException) as caught:
        callback()
    assert caught.value.status_code == status


def test_unknown_is_not_claimed(acl):
    assert_status(403, lambda: acl.require(principal(), "source", "local:unknown"))
    assert acl.get("source", "local:unknown") is None


def test_two_users_cross_object_denied(acl):
    a, b = principal(), principal("fictional-b")
    acl.register("project", "p-a", a)
    acl.register("project", "p-b", b)
    for kind in ("job", "source", "task", "upload", "artifact"):
        acl.register(kind, kind + "-a", a, project_id="p-a")
        assert_status(403, lambda: acl.require(b, kind, kind + "-a", write=True))
    assert_status(403, lambda: acl.register("project", "p-a", b))


def test_roles_revocation_and_child_ownership(acl):
    a, b = principal(), principal("fictional-b")
    acl.register("project", "p", a)
    acl.grant(a, "project", "p", b["subject"], "reader")
    acl.require(b, "project", "p")
    assert_status(403, lambda: acl.require(b, "project", "p", write=True))
    acl.grant(a, "project", "p", b["subject"], "editor")
    acl.register("task", "task-created-by-editor", b, project_id="p")
    assert acl.get("task", "task-created-by-editor")["owner"] == a["subject"]
    assert_status(403, lambda: acl.grant(b, "project", "p", "third"))
    acl.grant(a, "project", "p", b["subject"], "editor", revoke=True)
    assert_status(403, lambda: acl.require(b, "task", "task-created-by-editor"))


def test_persistent_filter_no_unknown_leak(acl):
    acl.register("project", "p", principal())
    rows = [{"project_id": "p"}, {"project_id": "unknown"}]
    assert sa.AccessLedger(acl.path).filter_visible(principal(), "project", rows) == rows[:1]
    assert acl.filter_visible(principal("fictional-b"), "project", rows) == []


def test_quota_idempotency_release_and_conflict(acl):
    p = principal()
    with acl.connect() as db:
        db.execute("INSERT INTO quotas VALUES(?,?,?,?)", (sa.DOMAIN_ID, p["subject"], 100, 1))
    acl.reserve(p, "operation", 60)
    acl.reserve(p, "operation", 60)
    assert_status(409, lambda: acl.reserve(p, "operation", 61))
    assert_status(429, lambda: acl.reserve(p, "second", 60))
    assert_status(403, lambda: acl.reserve(principal("fictional-b"), "operation", 60))
    acl.release("operation")
    acl.reserve(p, "second", 100)
    with acl.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM reservations WHERE active=1").fetchone()[0] == 1
    assert_status(429, lambda: acl.reserve(p, "operation", 60))
    acl.release("second")
    acl.reserve(p, "operation", 60)
    with acl.connect() as db:
        assert db.execute("SELECT active,bytes FROM reservations WHERE operation_id='operation'").fetchone()[:] == (1, 60)


def test_expired_reservation_rebudgeted(acl):
    p = principal()
    acl.reserve(p, "expired", 100, ttl_ms=-1)
    acl.reserve(p, "live", 100)
    with acl.connect() as db:
        assert db.execute("SELECT active FROM reservations WHERE operation_id='expired'").fetchone()[0] == 0


def test_editor_creation_uses_project_owner_quota_and_context_ledger(acl):
    a, b = principal(), principal("fictional-b")
    acl.register("project", "p", a)
    acl.register("source", "local:synthetic", a, project_id="p", size_bytes=25)
    acl.grant(a, "project", "p", b["subject"], "editor")
    actor_token=sa._principal.set(b)
    ledger_token=sa._access.set(acl)
    try:
        sa.reserve_job_operation("job", "p", "local:synthetic")
        sa.register_created_object("job", "job", "p")
        with acl.connect() as db:
            row=db.execute("SELECT subject,bytes FROM reservations WHERE operation_id='job'").fetchone()
            assert row[:] == (a["subject"], 50)
        assert acl.get("job", "job")["owner"] == a["subject"]
        sa.release_operation("job")
    finally:
        sa._access.reset(ledger_token)
        sa._principal.reset(actor_token)


def test_concurrent_duplicate_part_does_not_share_reservation(acl):
    acl.reserve(principal(), "part", 8, counts_concurrent=False, allow_active=False)
    assert_status(409, lambda: acl.reserve(principal(), "part", 8, counts_concurrent=False, allow_active=False))
    acl.release("part")
    acl.reserve(principal(), "part", 8, counts_concurrent=False, allow_active=False)


def test_explicit_creation_rejects_existing_unowned_directory(acl, tmp_path):
    app = FastAPI()
    app.state.studio_access_ledger = acl
    request = Request({"type": "http", "method": "POST", "path": "/create", "headers": [], "app": app,
                       "state": {"studio_principal": principal()}, "query_string": b""})
    directory = tmp_path / "protected"
    directory.mkdir()
    unknown_id = "a" * 32
    assert_status(403, lambda: sa.reserve_new_upload(request, unknown_id, directory))
    assert acl.get("upload", unknown_id) is None
    sa.reserve_new_upload(request, unknown_id, tmp_path / "absent")
    assert acl.get("upload", unknown_id)["owner"] == principal()["subject"]
    assert_status(403, lambda: sa.reserve_new_project(request, "legacy", [directory]))
    sa.reserve_new_project(request, "new", [tmp_path / "absent"])
    assert acl.get("project", "new")["owner"] == principal()["subject"]


def test_upload_part_replacement_accounting(acl):
    p = principal()
    acl.register("upload", "u", p)
    acl.record_upload_part(p, "u", 0, 5)
    acl.record_upload_part(p, "u", 1, 7)
    acl.record_upload_part(p, "u", 0, 3)
    assert acl.get("upload", "u")["bytes"] == 10
    assert_status(403, lambda: acl.record_upload_part(principal("fictional-b"), "u", 0, 99))
    assert acl.get("upload", "u")["bytes"] == 10


def test_staged_task_alias_uses_verified_db_parent(acl, monkeypatch):
    import src.studio_job_store as store
    async def get_job(job_id):
        return {"id": job_id, "project_id": "p"}
    monkeypatch.setattr(store, "get_job", get_job)
    acl.register("project", "p", principal())
    app = FastAPI()
    app.state.studio_access_ledger = acl
    def request_for(who):
        return Request({"type": "http", "method": "GET", "path": "/studio/auto-edit/" + "b"*32,
                        "headers": [], "app": app, "state": {"studio_principal": who}, "query_string": b""})
    async def run():
        with pytest.raises(HTTPException):
            await sa.require_reference(request_for(principal("fictional-b")), "task", "b"*32, False)
        assert acl.get("task", "b"*32) is None
        await sa.require_reference(request_for(principal()), "task", "b"*32, False)
        assert acl.get("task", "b"*32)["project_id"] == "p"
    asyncio.run(run())


def test_domain_fencing(acl):
    p = principal()
    acl.register("project", "p", p)
    p["domain_id"] = "another-domain"
    assert_status(403, lambda: acl.require(p, "project", "p"))
    assert_status(403, lambda: acl.register("project", "new", p))


def test_query_principal_actual_unix_transport(tmp_path):
    async def run():
        observed = []
        async def serve(reader, writer):
            observed.append(await reader.readuntil(b"\r\n\r\n"))
            payload = json.dumps(principal()).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload)
            await writer.drain()
            writer.close()
        with tempfile.TemporaryDirectory(prefix="w07-uds-") as short_root:
            path = str(Path(short_root) / "auth.sock")
            server = await asyncio.start_unix_server(serve, path)
            async with server:
                p = await sa.query_principal("synthetic_session=fictional", "POST", "https://example.invalid", socket_path=path)
        assert p["subject"] == "fictional-a"
        assert b"X-Forwarded-Method: POST" in observed[0]
        assert b"X-Video-Origin: https://example.invalid" in observed[0]
        assert b"X-User-Id" not in observed[0]
    asyncio.run(run())


def test_missing_expired_and_unavailable_auth(tmp_path):
    async def run():
        for cookie, expected in [("", 401), ("synthetic=x", 503), ("x\r\nBad: 1", 400)]:
            with pytest.raises(HTTPException) as caught:
                await sa.query_principal(cookie, "GET", socket_path=str(tmp_path / "absent"))
            assert caught.value.status_code == expected
    asyncio.run(run())


def test_real_asgi_acl_identity_header_and_path_body_shadowing(acl, monkeypatch):
    async def auth(cookie, method, origin):
        if cookie != "synthetic=a":
            raise HTTPException(401, "synthetic_denied")
        return principal()
    monkeypatch.setattr(sa, "query_principal", auth)
    acl.register("project", "mine", principal())
    acl.register("project", "theirs", principal("fictional-b"))
    app = FastAPI()
    app.state.studio_access_ledger = acl
    app.add_middleware(sa.StudioAuthenticationMiddleware)
    writes = []
    @app.post("/studio/projects/{project_id}", dependencies=[Depends(sa.enforce_studio_access)])
    async def write(project_id: str, request: Request):
        writes.append(project_id)
        return {"subject": request.state.studio_principal["subject"], "forged_header": request.headers.get("x-user-id")}
    @app.get("/studio/projects")
    async def projects():
        return [{"project_id": "mine"}, {"project_id": "theirs"}, {"project_id": "unknown"}]
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            denied = await client.post("/studio/projects/mine", json={}, headers={"x-user-id": "fictional-a"})
            assert denied.status_code == 401 and writes == []
            allowed = await client.post("/studio/projects/mine", json={}, headers={"cookie": "synthetic=a", "x-user-id": "fictional-b"})
            assert allowed.json() == {"subject": "fictional-a", "forged_header": None}
            before = list(writes)
            denied = await client.post("/studio/projects/theirs", json={"project_id": "mine"}, headers={"cookie": "synthetic=a"})
            assert denied.status_code == 403 and writes == before
            result = await client.get("/studio/projects", headers={"cookie": "synthetic=a"})
            assert result.json() == [{"project_id": "mine"}]
    asyncio.run(run())


def test_chunk_guard_does_not_read_body(acl):
    acl.register("upload", "cccccccccccccccccccccccccccccccc", principal())
    async def receive():
        raise AssertionError("chunk body must not be buffered by ACL")
    app = FastAPI()
    app.state.studio_access_ledger = acl
    request = Request({"type": "http", "method": "POST", "path": "/studio/transcribe/chunk", "query_string": b"upload_id=cccccccccccccccccccccccccccccccc&index=0",
                       "headers": [(b"content-type", b"application/octet-stream")], "app": app,
                       "state": {"studio_principal": principal()}, "path_params": {}}, receive=receive)
    asyncio.run(sa.enforce_studio_access(request))


def test_internal_exception_is_narrow():
    assert sa._is_worker_request("POST", "/api/internal/jobs/id/artifacts")
    assert sa._is_worker_request("GET", "/api/internal/jobs/id/artifacts")
    assert sa._is_worker_request("POST", "/api/internal/jobs/id/activate")
    assert not sa._is_worker_request("POST", "/api/jobs/id/retry")
    assert not sa._is_worker_request("POST", "/api/internal/arbitrary")


def test_actual_main_media_paths_and_new_source_route(acl, monkeypatch):
    from src.main_refactored import create_app
    from src import studio_project_store
    async def auth(cookie, method, origin):
        if cookie != "synthetic=a":
            raise HTTPException(401, "no_synthetic_session")
        return principal()
    monkeypatch.setattr(sa, "query_principal", auth)
    monkeypatch.setattr(studio_project_store, "list_projects", lambda: [{"project_id": "mine"}, {"project_id": "theirs"}])
    acl.register("project", "mine", principal())
    acl.register("project", "theirs", principal("fictional-b"))
    app = create_app()
    app.state.studio_access_ledger = acl
    paths = {route.path for route in app.routes}
    assert "/studio/projects" in paths and "/studio/transcribe/chunk" in paths
    assert "/api/studio/transcripts/revisions" in paths and "/api/studio/access" in paths
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            missing = await client.get("/studio/projects", headers={"x-user-id": "fictional-a"})
            assert missing.status_code == 401
            response = await client.get("/studio/projects", headers={"cookie": "synthetic=a"})
            assert response.status_code == 200 and response.json() == [{"project_id": "mine"}]
            forbidden = await client.get("/studio/projects/theirs/clips", headers={"cookie": "synthetic=a"})
            assert forbidden.status_code == 403
    asyncio.run(run())


def test_actual_chunk_assemble_owner_quota_and_idempotency(acl, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from src.main_refactored import create_app
    from src.api.routes import media
    from src import config, studio_job_store, studio_project_store
    cfg = SimpleNamespace(temp_dir=str(tmp_path / "media"), max_video_upload_bytes=1024*1024)
    monkeypatch.setattr(media, "get_config", lambda: cfg)
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    monkeypatch.setattr(studio_project_store, "TASKS_ROOT", tmp_path / "tasks")
    enqueued = []
    async def auth(cookie, method, origin):
        if cookie not in {"synthetic=a", "synthetic=b"}:
            raise HTTPException(401, "missing_synthetic_session")
        return principal("fictional-a" if cookie.endswith("a") else "fictional-b")
    async def create_job(project_id, object_key, size, job_config, *, job_id):
        sa.register_created_object("job", job_id, project_id)
        sa.register_created_object("task", job_id, project_id)
        enqueued.append(job_id)
        return job_id
    monkeypatch.setattr(sa, "query_principal", auth)
    monkeypatch.setattr(studio_job_store, "create_job", create_job)
    app = create_app()
    app.state.studio_access_ledger = acl
    upload_id = "d"*32
    data = b"synthetic-media-only"
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            a, b = {"cookie": "synthetic=a"}, {"cookie": "synthetic=b"}
            response = await client.post(f"/studio/transcribe/chunk?upload_id={upload_id}&index=0", content=data, headers=a)
            assert response.status_code == 200, response.text
            part = Path(cfg.temp_dir) / "studio-chunks" / upload_id / "000000.part"
            before = part.read_bytes()
            denied = await client.post(f"/studio/transcribe/chunk?upload_id={upload_id}&index=0", content=b"forged", headers=b)
            assert denied.status_code == 403 and part.read_bytes() == before
            payload = {"upload_id": upload_id, "filename": "synthetic.mp4", "total": 1, "size_bytes": len(data), "project_id": "timeline_synthetica"}
            response = await client.post("/studio/transcribe/assemble", json=payload, headers=a)
            assert response.status_code == 200, response.text
            source = response.json()["object_key"]
            assert acl.require(principal(), "source", source)["bytes"] == len(data)
            assert acl.get("upload", upload_id)["bytes"] == 0
            response2 = await client.post("/studio/transcribe/assemble", json=payload, headers=a)
            assert response2.json() == response.json() and enqueued == [upload_id]
            denied = await client.post("/studio/transcribe/assemble", json=payload, headers=b)
            assert denied.status_code == 403 and enqueued == [upload_id]
            payload["project_id"] = "timeline_other"
            conflict = await client.post("/studio/transcribe/assemble", json=payload, headers=a)
            assert conflict.status_code == 409 and acl.get("project", "timeline_other") is None
    asyncio.run(run())


def test_actual_legacy_upload_quota_rejection_has_no_source_write(acl, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from src.main_refactored import create_app
    from src.api.routes import media
    cfg=SimpleNamespace(temp_dir=str(tmp_path/'media'), max_video_upload_bytes=1024*1024)
    monkeypatch.setattr(media,'get_config',lambda:cfg)
    async def auth(cookie,method,origin):
        return principal()
    monkeypatch.setattr(sa,'query_principal',auth)
    with acl.connect() as db:
        db.execute('INSERT INTO quotas VALUES(?,?,?,?)',(sa.DOMAIN_ID,principal()['subject'],10,1))
    app=create_app();app.state.studio_access_ledger=acl
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            for path in ('/studio/transcribe','/studio/transcribe/start'):
                response=await client.post(path,files={'video':('synthetic.mp4',b'synthetic-media-only','video/mp4')})
                assert response.status_code==429,response.text
                assert not (Path(cfg.temp_dir)/'studio-uploads').exists()
    asyncio.run(run())


@pytest.mark.parametrize('content_type',['text/plain','multipart/form-data','', 'Application/JSON'])
def test_actual_assemble_content_type_cannot_bypass_owner(acl,monkeypatch,tmp_path,content_type):
    from types import SimpleNamespace
    from src.main_refactored import create_app
    from src.api.routes import media
    from src import config,studio_project_store
    cfg=SimpleNamespace(temp_dir=str(tmp_path/'media'),max_video_upload_bytes=1024*1024)
    monkeypatch.setattr(media,'get_config',lambda:cfg);monkeypatch.setattr(config,'get_config',lambda:cfg)
    monkeypatch.setattr(studio_project_store,'TASKS_ROOT',tmp_path/'tasks')
    foreign=principal('fictional-b');upload_id='e'*32
    acl.register('project','timeline_mine',principal());acl.register('upload',upload_id,foreign)
    directory=Path(cfg.temp_dir)/'studio-chunks'/upload_id;directory.mkdir(parents=True)
    (directory/'000000.part').write_bytes(b'foreign-synthetic-source')
    async def auth(*args):return principal()
    monkeypatch.setattr(sa,'query_principal',auth)
    app=create_app();app.state.studio_access_ledger=acl
    before={str(p):p.read_bytes() for p in directory.rglob('*') if p.is_file()}
    with acl.connect() as db:before_rows=[tuple(row) for row in db.execute('SELECT * FROM objects ORDER BY kind,object_id')]
    payload={'upload_id':upload_id,'project_id':'timeline_mine','filename':'x.mp4','total':1,'size_bytes':len(b'foreign-synthetic-source')}
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            headers={'content-type':content_type} if content_type else {}
            response=await client.post('/studio/transcribe/assemble',content=json.dumps(payload),headers=headers)
            assert response.status_code==(403 if content_type=='Application/JSON' else 415),response.text
    asyncio.run(run())
    assert before=={str(p):p.read_bytes() for p in directory.rglob('*') if p.is_file()}
    assert not (Path(cfg.temp_dir)/'studio-uploads').exists()
    with acl.connect() as db:assert before_rows==[tuple(row) for row in db.execute('SELECT * FROM objects ORDER BY kind,object_id')]


def test_global_timeline_material_requires_existing_source_acl(acl,monkeypatch,tmp_path):
    from src.api.routes import media
    project=tmp_path/'studio-projects'/'timeline_mine';project.mkdir(parents=True)
    uploads=tmp_path/'studio-uploads';uploads.mkdir();foreign=uploads/'foreign.wav';foreign.write_bytes(b'synthetic-foreign-audio')
    monkeypatch.setattr(media,'_timeline_material_roots',lambda:[uploads.resolve()])
    p=sa._principal.set(principal());a=sa._access.set(acl)
    try:
        acl.register('project','timeline_mine',principal())
        acl.register('source','local:'+str(foreign.resolve()),principal('fictional-b'))
        with pytest.raises(HTTPException) as denied:media._timeline_asset_path(project,str(foreign))
        assert denied.value.status_code==403 and foreign.read_bytes()==b'synthetic-foreign-audio'
        owned=project/'own.wav';owned.write_bytes(b'synthetic-own')
        assert media._timeline_asset_path(project,str(owned))==owned
        unknown=uploads/'unknown.wav';unknown.write_bytes(b'unknown')
        with pytest.raises(HTTPException):media._timeline_asset_path(project,str(unknown))
        assert acl.get('source','local:'+str(unknown.resolve())) is None
    finally:
        sa._access.reset(a);sa._principal.reset(p)


def test_actual_timeline_foreign_source_rejected_before_decode_or_history(acl,monkeypatch,tmp_path):
    from types import SimpleNamespace
    from src.api.routes import media
    from src.main_refactored import create_app
    project=tmp_path/'studio-projects'/'timeline_mine';project.mkdir(parents=True)
    (project/'project.json').write_text(json.dumps({'project_id':'timeline_mine','source':{}}))
    uploads=tmp_path/'studio-uploads';uploads.mkdir();foreign=uploads/'foreign.wav';foreign.write_bytes(b'foreign-synthetic-audio')
    cfg=SimpleNamespace(temp_dir=str(tmp_path))
    monkeypatch.setattr(media,'get_config',lambda:cfg)
    monkeypatch.setattr(media,'_timeline_material_roots',lambda:[uploads.resolve()])
    acl.register('project','timeline_mine',principal());acl.register('source','local:'+str(foreign.resolve()),principal('fictional-b'))
    async def auth(*args):return principal()
    monkeypatch.setattr(sa,'query_principal',auth)
    def no_decode(*args,**kwargs):raise AssertionError('foreign audio must not be decoded')
    monkeypatch.setattr(media.timeline_audio,'peaks_for_asset',no_decode)
    app=create_app();app.state.studio_access_ledger=acl
    before={str(p):p.read_bytes() for p in project.rglob('*') if p.is_file()}
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            result=await client.get('/studio/timeline/timeline_mine/assets/peaks',params={'path':str(foreign)})
            assert result.status_code==403,result.text
            result=await client.post('/studio/timeline/timeline_mine/tracks',json={'kind':'bgm','path':str(foreign)})
            assert result.status_code==403,result.text
    asyncio.run(run())
    assert before=={str(p):p.read_bytes() for p in project.rglob('*') if p.is_file()}
    assert foreign.read_bytes()==b'foreign-synthetic-audio'


def test_media_clip_entry_uses_authoritative_staged_locator(monkeypatch,tmp_path):
    from src.api.routes import media
    task_id='f'*32;clip_id='clip'
    monkeypatch.setattr(media,'_tasks_root',lambda:tmp_path)
    monkeypatch.setattr(media,'project_entry_for',lambda *args:{'render_task_id':task_id})
    staged=tmp_path/'output'/task_id/'clips'/clip_id;staged.mkdir(parents=True)
    (staged/'clip_final.mp4').write_bytes(b'synthetic-staged')
    legacy=tmp_path/'output'/task_id/clip_id;legacy.mkdir()
    (legacy/'clip_final.mp4').write_bytes(b'synthetic-legacy')
    assert media._clip_entry_and_dir('timeline_test',clip_id)[1]==staged
    (staged/'clip_final.mp4').unlink()
    with pytest.raises(FileNotFoundError):media._clip_entry_and_dir('timeline_test',clip_id)
