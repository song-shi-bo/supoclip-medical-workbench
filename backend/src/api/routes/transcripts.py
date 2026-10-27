"""源转写核听修订入口；与成片字幕编辑分开，返回明确不可变版本。"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from ... import studio_transcript_store as store
from ...studio_source_revisions import revise_source, RevisionConflict, review_document
from ...studio_access import require_source_access

router = APIRouter(prefix='/api/studio/transcripts', tags=['source-transcript-revisions'])


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
