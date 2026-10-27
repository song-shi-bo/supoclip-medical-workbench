"""仅合成项目/独立ACL，真实工厂与线程生命周期；无模型或FFmpeg。"""
from pathlib import Path
import json
import sys
import threading
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import pytest
from fastapi import HTTPException
from src import studio_access as sa,studio_local_tasks as local,studio_auto_edit_bridge as bridge

PROJECT='timeline_quota'
OWNER={'subject':'fictional-a','domain_id':sa.DOMAIN_ID,'slug':'fixture','expires_at_ms':9999999999999}

@pytest.fixture
def context(tmp_path,monkeypatch):
    access=sa.AccessLedger(tmp_path/'acl.sqlite')
    access.register('project',PROJECT,OWNER)
    p=sa._principal.set(OWNER);a=sa._access.set(access)
    monkeypatch.setattr(local,'TASKS_ROOT',tmp_path/'local')
    monkeypatch.setattr(bridge,'TASKS_ROOT',tmp_path/'bridge')
    monkeypatch.setattr(local.store,'entry_for',lambda *args:{'clip_id':'clip'})
    monkeypatch.setattr(local.store,'record_task',lambda *args:None)
    monkeypatch.setattr(local.studio_workflow,'build_task_snapshot',lambda *args,**kwargs:{})
    monkeypatch.setattr(local.skill_registry,'task_versions',lambda snapshot:{})
    saved=[]
    monkeypatch.setattr(local,'_save',lambda record:saved.append(dict(record)))
    monkeypatch.setattr(local,'_log_line',lambda record:None)
    source=tmp_path/'synthetic.mp4';source.write_bytes(b'fixture-only')
    monkeypatch.setattr(bridge,'_project_dir',lambda *args:tmp_path)
    monkeypatch.setattr(bridge,'_source_for_project',lambda *args:source)
    monkeypatch.setattr(bridge,'attach_workflow_snapshot',lambda document,**kwargs:document)
    actual_watch=sa.watch_legacy_operation
    monkeypatch.setattr(bridge,'watch_legacy_operation',lambda task,path,*,access:actual_watch(task,path,access=access,poll_seconds=.01))
    yield access,saved,tmp_path,source
    sa._access.reset(a);sa._principal.reset(p)

def slot(access,task_id):
    with access.connect() as db:
        row=db.execute('SELECT active,expires_ms FROM reservations WHERE operation_id=?',(task_id,)).fetchone()
        return tuple(row) if row else None

def wait_release(access,task_id):
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        if slot(access,task_id)[0]==0:return
        time.sleep(.01)
    raise AssertionError('reservation was not released')

def deny_all(access):
    with access.connect() as db:
        db.execute('INSERT INTO quotas VALUES(?,?,?,?)',(sa.DOMAIN_ID,OWNER['subject'],1000000,0))

def test_local_creation_quota_rejected_before_snapshot_or_save(context,monkeypatch):
    access,saved,root,source=context;deny_all(access)
    calls=[]
    monkeypatch.setattr(local.studio_workflow,'build_task_snapshot',lambda *args,**kwargs:calls.append(args))
    with pytest.raises(HTTPException) as exc:
        local.create_task(project_id=PROJECT,clip_id='clip',task_type=local.TASK_UPDATE_TITLE,run=False)
    assert exc.value.status_code==429 and saved==[] and calls==[]
    assert not (root/'local').exists()

def test_snapshot_failure_releases(context,monkeypatch):
    access,saved,root,source=context
    monkeypatch.setattr(local,'new_task_id',lambda *args:'title_snapshot')
    def fail(*args,**kwargs):raise RuntimeError('synthetic snapshot failure')
    monkeypatch.setattr(local.studio_workflow,'build_task_snapshot',fail)
    with pytest.raises(RuntimeError):
        local.create_task(project_id=PROJECT,clip_id='clip',task_type=local.TASK_UPDATE_TITLE,run=False)
    assert slot(access,'title_snapshot')[0]==0 and saved==[]

@pytest.mark.parametrize('fail',[False,True])
def test_local_runner_finally_releases_success_and_failure(context,monkeypatch,fail):
    access,saved,root,source=context
    record=local.create_task(project_id=PROJECT,clip_id='clip',task_type=local.TASK_UPDATE_TITLE,run=False)
    def handle(record):
        if fail:raise RuntimeError('synthetic handler failure')
        return {'title':'fixture'}
    monkeypatch.setitem(local._HANDLERS,local.TASK_UPDATE_TITLE,handle)
    local._submit(record,quota_renew_seconds=.01)
    wait_release(access,record['task_id'])
    assert record['status']==('failed' if fail else 'completed')

def test_local_runner_renews_while_handler_active(context,monkeypatch):
    access,saved,root,source=context
    record=local.create_task(project_id=PROJECT,clip_id='clip',task_type=local.TASK_UPDATE_TITLE,run=False)
    finish=threading.Event();entered=threading.Event();renews=[]
    original=access.renew
    def renew(task_id):renews.append(task_id);original(task_id)
    monkeypatch.setattr(access,'renew',renew)
    def handle(record):entered.set();finish.wait(2);return {'title':'fixture'}
    monkeypatch.setitem(local._HANDLERS,local.TASK_UPDATE_TITLE,handle)
    local._submit(record,quota_renew_seconds=.01)
    assert entered.wait(1)
    deadline=time.monotonic()+1
    while not renews and time.monotonic()<deadline:time.sleep(.01)
    assert renews and slot(access,record['task_id'])[0]==1
    finish.set();wait_release(access,record['task_id'])

def test_editor_task_owner_and_revocation(context):
    access,saved,root,source=context
    other={**OWNER,'subject':'fictional-b'}
    access.grant(OWNER,'project',PROJECT,other['subject'],'editor')
    token=sa._principal.set(other)
    try:
        record=local.create_task(project_id=PROJECT,clip_id='clip',task_type=local.TASK_UPDATE_TITLE,run=False)
        assert access.get('task',record['task_id'])['owner']==OWNER['subject']
        assert slot(access,record['task_id'])[0]==1
        access.grant(OWNER,'project',PROJECT,other['subject'],'editor',revoke=True)
        with pytest.raises(HTTPException):
            local.create_task(project_id=PROJECT,clip_id='clip',task_type=local.TASK_UPDATE_TITLE,run=False)
    finally:
        access.release(record['task_id']);sa._principal.reset(token)

def test_legacy_enqueue_quota_rejected_without_queue_side_effects(context):
    access,saved,root,source=context;deny_all(access)
    with pytest.raises(HTTPException) as exc:
        bridge.submit_auto_edit_task(project_id=PROJECT,uploads_root=root,asr_segments=[{'start':0,'end':1,'text':'fixture'}])
    assert exc.value.status_code==429 and not (root/'bridge').exists()

def test_legacy_queued_task_renews_and_terminal_releases(context):
    access,saved,root,source=context
    record=bridge.submit_auto_edit_task(project_id=PROJECT,uploads_root=root,asr_segments=[{'start':0,'end':1,'text':'fixture'}])
    task_id=record['task_id'];status=root/'bridge/status'/(task_id+'.json')
    assert slot(access,task_id)[0]==1
    payload=json.loads(status.read_text());payload['status']='completed'
    bridge._atomic_json(status,payload)
    wait_release(access,task_id)
    assert access.get('task',task_id)['project_id']==PROJECT

def test_legacy_enqueue_failure_releases(context,monkeypatch):
    access,saved,root,source=context
    task_ids=[];original=sa.prepare_project_operation
    def prepare(task_id,project_id,**kwargs):task_ids.append(task_id);return original(task_id,project_id,**kwargs)
    monkeypatch.setattr(bridge,'prepare_project_operation',prepare)
    def fail(*args,**kwargs):raise RuntimeError('synthetic write failure')
    monkeypatch.setattr(bridge,'_atomic_json',fail)
    with pytest.raises(RuntimeError):
        bridge.submit_auto_edit_task(project_id=PROJECT,uploads_root=root,asr_segments=[{'start':0,'end':1,'text':'fixture'}])
    assert slot(access,task_ids[0])[0]==0

def test_legacy_recovery_observes_existing_reservation_without_replay(context,monkeypatch):
    access,saved,root,source=context
    task_id='autoedit_'+'a'*12
    sa.prepare_project_operation(task_id,PROJECT)
    called=[]
    monkeypatch.setattr(sa,'watch_legacy_operation',lambda task,path,*,access:called.append((task,path)))
    sa.resume_legacy_quota_watchers(root/'bridge')
    assert called==[(task_id,root/'bridge/status'/(task_id+'.json'))]
    assert not (root/'bridge').exists()
    access.release(task_id)


@pytest.mark.parametrize('failure',[False,True])
def test_preview_background_slot_survives_response_and_releases(context,monkeypatch,failure):
    from src import preview_proxy as preview
    access,saved,root,source=context;project=root/PROJECT;project.mkdir()
    monkeypatch.setattr(preview,'status',lambda project:preview.PreviewStatus('failed',None,'hevc'))
    monkeypatch.setattr(preview,'source_video',lambda project:source)
    finish=threading.Event();entered=threading.Event()
    def build(project,src):
        entered.set();finish.wait(2)
        if failure:raise RuntimeError('synthetic preview failure')
        target=preview.proxy_path(project);target.parent.mkdir(parents=True);target.write_bytes(b'synthetic-preview')
    monkeypatch.setattr(preview,'_build',build)
    result=preview.start_build(project);assert result.state=='building' and entered.wait(1)
    with access.connect() as db:
        rows=db.execute("SELECT operation_id FROM reservations WHERE active=1").fetchall()
        assert len(rows)==1;task_id=rows[0][0]
    assert preview.start_build(project).state=='building' and slot(access,task_id)[0]==1
    finish.set();wait_release(access,task_id)
    assert str(project) not in preview._BUILDING


def test_preview_readonly_native_does_not_reserve(context,monkeypatch):
    from src import preview_proxy as preview
    access,saved,root,source=context;deny_all(access)
    monkeypatch.setattr(preview,'status',lambda project:preview.PreviewStatus('native',source,'h264'))
    assert preview.start_build(root/PROJECT).state=='native'
    with access.connect() as db:assert db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0]==0


def test_peaks_cache_hit_does_not_consume_quota_and_miss_does(context,monkeypatch):
    from src import timeline_audio
    access,saved,root,source=context;project=root/PROJECT;project.mkdir()
    calls=[]
    monkeypatch.setattr(timeline_audio.clip_audio,'extract_peaks',lambda *args:(calls.append(args) or [.1,.2]))
    monkeypatch.setattr(timeline_audio.clip_audio,'probe_duration',lambda *args:1.0)
    first=timeline_audio.peaks_for_asset(project,source,buckets=2)
    assert not first['cached'] and len(calls)==1
    deny_all(access)
    cached=timeline_audio.peaks_for_asset(project,source,buckets=2)
    assert cached['cached'] and len(calls)==1
    with pytest.raises(HTTPException) as rejected:timeline_audio.peaks_for_asset(project,source,buckets=3)
    assert rejected.value.status_code==429 and len(calls)==1


def test_transform_cache_hit_download_is_free_and_new_render_requires_slot(context,monkeypatch):
    from types import SimpleNamespace
    from src import studio_transform_export as export
    access,saved,root,source=context;task_id='autoedit_'+'b'*12
    access.register('task',task_id,OWNER,project_id=PROJECT)
    monkeypatch.setattr(export,'clip_video_path',lambda *args:source)
    monkeypatch.setattr(export,'read_clip_render_settings',lambda *args:None)
    monkeypatch.setattr(export,'_probe_media',lambda *args:{'width':320,'height':240,'duration':1.0,'has_audio':False})
    calls=[]
    def run(command,**kwargs):
        calls.append(command);Path(command[-1]).write_bytes(b'synthetic-render');return SimpleNamespace(returncode=0,stdout='',stderr='')
    monkeypatch.setattr(export.subprocess,'run',run)
    rendered=export.export_transformed_clip(task_id=task_id,clip_id='clip',transform={'scale':1.1},cache_root=root/'cache')
    assert rendered.read_bytes()==b'synthetic-render' and len(calls)==1
    deny_all(access)
    assert export.export_transformed_clip(task_id=task_id,clip_id='clip',transform={'scale':1.1},cache_root=root/'cache')==rendered
    assert len(calls)==1
    with pytest.raises(HTTPException) as rejected:
        export.export_transformed_clip(task_id=task_id,clip_id='clip',transform={'scale':1.2},cache_root=root/'cache')
    assert rejected.value.status_code==429 and len(calls)==1
