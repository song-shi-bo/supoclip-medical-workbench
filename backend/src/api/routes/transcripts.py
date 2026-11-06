"""源转写核听修订入口；与成片字幕编辑分开，返回明确不可变版本。"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from ... import studio_transcript_store as store
from ...studio_source_revisions import revise_source, RevisionConflict, review_document
from ...studio_access import require_source_access

router = APIRouter(prefix='/api/studio/transcripts', tags=['source-transcript-revisions'])


class SpeakerLabelRequest(BaseModel):
    object_key: str = Field(min_length=1,max_length=4096)
    transcript_id: str = Field(default='',max_length=128)
    transcript_version: int | None = Field(default=None,ge=1)

    model_config = {'extra':'forbid'}


@router.post('/speaker-labels')
async def generate_speaker_labels(body: SpeakerLabelRequest, request: Request):
    """时间线整条转写的说话人自动标注（医生/患者/家属/未知）。

    只写 overlay（tasks/transcripts/<source_id>/speaker_labels.json），
    不改写任何纠错转写版本；时间线载入时按指纹非破坏合并。
    """
    await require_source_access(request,body.object_key)
    document = await run_in_threadpool(store.load_corrected_transcript, body.object_key,
        transcript_id=body.transcript_id or None,
        transcript_version=body.transcript_version)
    if document is None or not document.get('asr_segments'):
        raise HTTPException(404,'源片或指定转写版本不存在')
    from ... import speaker_labeling
    from ...studio_transcript_store import transcripts_root, source_id_for

    def _run() -> dict:
        source_id = source_id_for(body.object_key)
        resume = speaker_labeling.load_labels(transcripts_root(), source_id)
        record = speaker_labeling.label_transcript(
            document['asr_segments'],
            transcript_id=str(document.get('transcript_id') or ''),
            transcript_version=document.get('transcript_version'),
            note='api:/api/studio/transcripts/speaker-labels',
            resume_from=resume)
        path = speaker_labeling.save_labels(transcripts_root(), source_id, record)
        return {'record': record, 'rel_path': str(path.relative_to(transcripts_root().parent))}

    result = await run_in_threadpool(_run)
    record = result['record']
    return {'summary': {k: record.get(k) for k in
                        ('schema','prompt_version','transcript_id','transcript_version',
                         'segment_count','stats','llm_error','model','windows')},
            'labels': record['labels'], 'path': result['rel_path']}


@router.get('/speaker-labels')
async def read_speaker_labels(request: Request, object_key: str):
    """读取已生成的说话人标注 overlay（没有则 404，前端据此隐藏入口）。"""
    await require_source_access(request,object_key)
    from ... import speaker_labeling
    from ...studio_transcript_store import transcripts_root, source_id_for

    record = await run_in_threadpool(
        speaker_labeling.load_labels, transcripts_root(), source_id_for(object_key))
    if record is None:
        raise HTTPException(404,'尚未生成说话人标注')
    return {'record': record}


class SegmentEdit(BaseModel):
    segment_index: int = Field(ge=0)
    text: str = Field(min_length=1,max_length=20000)

    model_config = {'extra':'forbid'}


class RevisionRequest(BaseModel):
    object_key: str = Field(min_length=1,max_length=4096)
    transcript_id: str = Field(min_length=1,max_length=128)
    base_version: int = Field(ge=1)
    edits: list[SegmentEdit] = Field(min_length=1,max_length=10000)
    request_id: str = Field(min_length=8,max_length=128)
    reason: str = Field(default='',max_length=2000)

    model_config = {'extra':'forbid'}


@router.get('/revisions')
async def read_revision(request: Request, object_key: str, transcript_id: str,
                        transcript_version: int | None = None):
    await require_source_access(request,object_key)
    if transcript_version is not None and transcript_version<1:
        raise HTTPException(422,'转写版本无效')
    document=await run_in_threadpool(store.load_corrected_transcript,object_key,
        transcript_id=transcript_id,transcript_version=transcript_version)
    if document is None:
        raise HTTPException(404,'源片或指定转写版本不存在')
    if 'review_warnings' not in document:
        document=await run_in_threadpool(review_document,document)
    return {'document':document}


@router.post('/revisions')
async def create_revision(body: RevisionRequest, request: Request):
    await require_source_access(request,body.object_key)
    actor=getattr(request.state,'studio_principal',None)
    if not isinstance(actor,dict) or not actor.get('subject') or not actor.get('domain_id'):
        raise HTTPException(401,'缺少可信修订主体')
    try:
        document=await run_in_threadpool(revise_source,body.object_key,body.transcript_id,
            body.base_version,[item.model_dump() for item in body.edits],actor=actor,
            request_id=body.request_id,reason=body.reason)
    except RevisionConflict as exc:
        raise HTTPException(409,str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404,str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(401,str(exc)) from exc
    return {'document':document,'transcript_id':document['transcript_id'],
            'transcript_version':document['transcript_version']}
