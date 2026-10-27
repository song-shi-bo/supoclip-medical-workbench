import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from src import studio_transcript_store as store
from src.studio_source_revisions import revise_source, RevisionConflict, review_document

ACTOR={'subject':'8','domain_id':'bdd01c9e-1ace-47b1-8288-bee275fa4ad0'}


@pytest.fixture
def source(tmp_path,monkeypatch):
    monkeypatch.setenv('STUDIO_TASKS_ROOT',str(tmp_path/'tasks'))
    monkeypatch.setattr(store,'_source_still_exists',lambda _:True)
    key='fixture:source'
    ref=store.save_corrected_transcript(key,'timeline_fixture',
        asr_segments=[{'start':0.,'end':2.,'text':'原始纠错文本','uncertain':True},
                      {'start':2.,'end':5.,'text':'第二段'}],
        raw_segments=[{'start':0.,'end':2.,'text':'原始识别文本'}],
        correction_audit=[{'before':'识别文本','after':'纠错文本'}],
        correction_summary={'llm_batches':1},correction_provider='fixture',language='zh',words=[])
    return key,ref


def revise(source,**kwargs):
    key,ref=source
    return revise_source(key,ref['transcript_id'],1,[{'segment_index':0,'text':'核听修订'}],
                         actor=ACTOR,request_id=kwargs.pop('request_id','fixture-request-a'),**kwargs)


def test_preserves_original_timestamps_raw_and_audit(source):
    key,ref=source
    original=store.load_corrected_transcript(key,transcript_version=1)
    before=store._version_path(store.source_id_for(key),1).read_bytes()
    revised=revise(source,reason='核听原声')
    assert revised['transcript_version']==2
    assert revised['asr_segments'][0]['text']=='核听修订'
    for field in ['start','end','uncertain']:
        assert revised['asr_segments'][0][field]==original['asr_segments'][0][field]
    assert revised['raw_segments']==original['raw_segments']
    assert revised['correction_audit']==original['correction_audit']
    assert revised['manual_revision']['actor']==ACTOR
    assert store._version_path(store.source_id_for(key),1).read_bytes()==before


def test_duplicate_request_returns_same_version(source):
    first=revise(source)
    assert revise(source)==first
    key,_=source
    assert len(list((store.transcripts_root()/store.source_id_for(key)).glob('v*.json')))==2


def test_stale_and_request_key_conflict_no_new_version(source):
    revise(source)
    with pytest.raises(RevisionConflict):revise(source,request_id='different-request')
    with pytest.raises(RevisionConflict):revise(source,reason='different')
    assert store._latest_version(store.source_id_for(source[0]))==2


def test_two_writers_cas_one_winner(source):
    def call(i):
        try:return revise(source,request_id=f'concurrent-{i}')['transcript_version']
        except RevisionConflict:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        result=list(pool.map(call,[1,2]))
    assert sorted(map(str,result))==['2','conflict']


@pytest.mark.parametrize('edits',[
    [{'segment_index':0,'text':'changed','start':100}],
    [{'segment_index':True,'text':'changed'}],
    [{'segment_index':0,'text':' '}],
    [{'segment_index':10,'text':'changed'}],
    [{'segment_index':0,'text':'x'},{'segment_index':0,'text':'y'}],
    [{'segment_index':0,'text':'原始纠错文本'}],
])
def test_invalid_edits_do_not_write_new_version(source,edits):
    key,ref=source
    with pytest.raises(ValueError):
        revise_source(key,ref['transcript_id'],1,edits,actor=ACTOR,request_id='invalid-request')
    assert store._latest_version(store.source_id_for(key))==1


def test_untrusted_actor_rejected(source):
    key,ref=source
    with pytest.raises(PermissionError):
        revise_source(key,ref['transcript_id'],1,[{'segment_index':0,'text':'x'}],
                      actor={},request_id='invalid-actor')


def test_downstream_fixed_old_and_new_versions(source):
    revised=revise(source)
    key,ref=source
    assert store.load_corrected_transcript(key,transcript_id=ref['transcript_id'],transcript_version=1)['asr_segments'][0]['text']=='原始纠错文本'
    assert store.load_corrected_transcript(key,transcript_id=ref['transcript_id'],transcript_version=2)['asr_segments'][0]['text']=='核听修订'
    automatic=store.save_corrected_transcript(key,'timeline_fixture',
        asr_segments=revised['asr_segments'],raw_segments=revised['raw_segments'],
        correction_audit=[],correction_summary={},correction_provider='fixture',language='zh',words=[])
    assert automatic['transcript_version']==3


def test_warnings_medication_dose_negation_and_uncertain_are_visible():
    document={'raw_segments':[{'start':0,'end':3,'text':'无药甲10mg'}],
              'asr_segments':[{'start':0,'end':3,'text':'药乙20mg','uncertain':True}]}
    before=json.dumps(document,sort_keys=True)
    revised=review_document(document,medical_terms=['药甲','药乙'])
    assert json.dumps(document,sort_keys=True)==before
    warnings=revised['review_warnings']
    assert {flag['category'] for flag in warnings['risk_flags']}=={
        'medication_term_change','dose_change','negation_change','uncertain'}
    assert all(flag['segment_index']==0 for flag in warnings['risk_flags'])
    assert warnings['warning_only'] is True


def test_unaligned_raw_not_compared_as_if_same_segment():
    document={'raw_segments':[{'start':0,'end':5,'text':'药甲'}],
              'asr_segments':[{'start':0,'end':3,'text':'药乙','uncertain':True}]}
    warnings=review_document(document,medical_terms=['药甲','药乙'])['review_warnings']
    assert {flag['category'] for flag in warnings['risk_flags']}=={'uncertain'}
    assert warnings['validation_errors']==[{'kind':'comparison_alignment_unavailable'}]


@pytest.fixture
def source_api(source,tmp_path,monkeypatch):
    from fastapi import FastAPI, Depends, HTTPException
    from fastapi.testclient import TestClient
    from src import studio_access as access
    from src.api.routes import transcripts
    db=access.AccessLedger(tmp_path/'acl.sqlite3')
    owner={**ACTOR,'slug':'fixture_owner','expires_at_ms':9999999999999}
    other={**owner,'subject':'9','slug':'fixture_other'}
    db.register('project','timeline_fixture',owner)
    db.register('source',source[0],owner,project_id='timeline_fixture')
    async def fixture_query(cookie,method,origin='',**_):
        if cookie=='fixture=owner':return owner
        if cookie=='fixture=other':return other
        raise HTTPException(401,'fixture_auth_rejected')
    monkeypatch.setattr(access,'query_principal',fixture_query)
    app=FastAPI()
    app.state.studio_access_ledger=db
    app.add_middleware(access.StudioAuthenticationMiddleware)
    app.include_router(transcripts.router,dependencies=[Depends(access.enforce_studio_access)])
    with TestClient(app) as client:
        yield client,db,owner,other


def api_body(source):
    key,ref=source
    return {'object_key':key,'transcript_id':ref['transcript_id'],'base_version':1,
            'edits':[{'segment_index':0,'text':'API核听修订'}], 'request_id':'api-fixture-request'}


def test_http_revision_and_conflict_contract(source,source_api):
    client,_,_,_=source_api
    headers={'Cookie':'fixture=owner'}
    body=api_body(source)
    response=client.post('/api/studio/transcripts/revisions',json=body,headers=headers)
    assert response.status_code==200,response.text
    assert response.json()['document']['transcript_version']==2
    assert client.post('/api/studio/transcripts/revisions',json=body,headers=headers).json()==response.json()
    stale={**body,'request_id':'second-fixture-request'}
    assert client.post('/api/studio/transcripts/revisions',json=stale,headers=headers).status_code==409
    old=client.get('/api/studio/transcripts/revisions',params={'object_key':source[0],
        'transcript_id':source[1]['transcript_id'],'transcript_version':1},headers=headers)
    assert old.status_code==200
    assert old.json()['document']['asr_segments'][0]['text']=='原始纠错文本'


def test_http_acl_forgery_and_reader_cannot_mutate(source,source_api):
    client,db,owner,other=source_api
    body=api_body(source)
    path=store._version_path(store.source_id_for(source[0]),1)
    before=path.read_bytes()
    assert client.post('/api/studio/transcripts/revisions',json=body,
        headers={'X-User-Id':'8','X-DSH-User-Id':'8'}).status_code==401
    assert client.post('/api/studio/transcripts/revisions',json=body,
        headers={'Cookie':'fixture=other','X-User-Id':'8'}).status_code==403
    db.grant(owner,'source',source[0],other['subject'],'reader')
    read=client.get('/api/studio/transcripts/revisions',params={
        'object_key':source[0],'transcript_id':source[1]['transcript_id']},
        headers={'Cookie':'fixture=other'})
    assert read.status_code==200
    assert client.post('/api/studio/transcripts/revisions',json=body,
        headers={'Cookie':'fixture=other'}).status_code==403
    assert path.read_bytes()==before
    assert store._latest_version(store.source_id_for(source[0]))==1


def test_http_rejects_actor_and_timestamp_fields(source,source_api):
    client,_,_,_=source_api
    body=api_body(source)
    for invalid in [{**body,'actor':ACTOR},{**body,'edits':[{'segment_index':0,'text':'x','start':50}]}]:
        assert client.post('/api/studio/transcripts/revisions',json=invalid,
            headers={'Cookie':'fixture=owner'}).status_code==422
    assert store._latest_version(store.source_id_for(source[0]))==1
