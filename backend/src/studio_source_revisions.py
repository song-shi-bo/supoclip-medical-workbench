"""源转写人工核听修订：固定基版、只改文本、保留原文、追加不可变版本。"""
from __future__ import annotations
import copy
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any
from . import studio_transcript_store as store
from .studio_runtime import process_lock


class RevisionConflict(ValueError):
    pass


def review_document(document: dict[str, Any], *, comparison: dict[str, Any] | None = None,
                    medical_terms: list[str] | None = None) -> dict[str, Any]:
    """可见提示由真实对齐文本计算；不修改版本文件，不裁定医学正确性。"""
    from .sensitive_change import detect_sensitive_changes
    if medical_terms is None:
        from .medical_lexicon import load_lexicon
        medical_terms = sorted({term for entry in load_lexicon().get('entries',[])
            if entry.get('type')=='drug'
            for term in [entry.get('standard'),*(entry.get('aliases') or [])] if term})
    after=document.get('asr_segments') or []
    before=(comparison.get('asr_segments') if comparison else document.get('raw_segments')) or []
    valid_alignment=len(before)==len(after) and all(
        a.get('start')==b.get('start') and a.get('end')==b.get('end')
        for a,b in zip(before,after))
    # 未对齐时只保留 uncertainty 提示，不把不同段落冒认成词汇变化。
    aligned_before=before if valid_alignment else after
    ids=[str(item.get('segment_id') or f'source_{index:06d}') for index,item in enumerate(after)]
    uncertain={ids[i] for i,item in enumerate(after) if item.get('uncertain') or
        'uncertain' in (item.get('flags') or [])}
    for entry in document.get('correction_audit') or []:
        if not isinstance(entry,dict) or not (entry.get('uncertain') or entry.get('status')=='uncertain'):
            continue
        index=entry.get('segment_index',entry.get('index'))
        if type(index) is int and 0<=index<len(ids):uncertain.add(ids[index])
        if str(entry.get('segment_id') or '') in ids:uncertain.add(str(entry['segment_id']))
    result=detect_sensitive_changes(
        [{'segment_id':ids[i],'text':item.get('text','')} for i,item in enumerate(aligned_before)],
        [{'segment_id':ids[i],'text':item.get('text','')} for i,item in enumerate(after)],
        medical_terms=medical_terms,uncertain_segment_ids=sorted(uncertain))
    for flag in result['risk_flags']:
        if flag['segment_id'] in ids:flag['segment_index']=ids.index(flag['segment_id'])
    if not valid_alignment:
        result['validation_errors'].append({'kind':'comparison_alignment_unavailable'})
    result['warning_only']=True
    result['comparison_source']='base_version' if comparison else 'raw_asr'
    revised=copy.deepcopy(document)
    revised['review_warnings']=result
    return revised


def revise_source(object_key: str, transcript_id: str, base_version: int,
                  edits: list[dict[str, Any]], *, actor: dict[str, Any],
                  request_id: str, reason: str = '') -> dict[str, Any]:
    """调用方须先完成对象 ACL；actor 仅接受已验证主体，不从正文推断。"""
    if not actor.get('subject') or not actor.get('domain_id'):
        raise PermissionError('缺少可信修订主体')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', request_id or ''):
        raise ValueError('修订 request_id 无效')
    if type(base_version) is not int or base_version < 1:
        raise ValueError('基版无效')
    if not isinstance(edits, list) or not edits or len(edits) > 10000:
        raise ValueError('请选择至少一个需要修订的段落')
    if not isinstance(reason, str) or len(reason) > 2000:
        raise ValueError('修订说明过长')
    normalized, seen = [], set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {'segment_index', 'text'}:
            raise ValueError('修订仅允许段落序号和文本，不能修改时间戳')
        index, text = edit['segment_index'], edit['text']
        if type(index) is not int or index < 0 or index in seen:
            raise ValueError('段落序号无效或重复')
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise ValueError('修订文本不能为空或过长')
        normalized.append({'segment_index':index, 'text':text.strip()})
        seen.add(index)
    normalized.sort(key=lambda item:item['segment_index'])
    subject = {'subject':str(actor['subject']), 'domain_id':str(actor['domain_id'])}
    fingerprint = hashlib.sha256(json.dumps({'transcript_id':transcript_id,
        'base_version':base_version,'edits':normalized,'actor':subject,'reason':reason},
        ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    original = store.load_corrected_transcript(object_key,transcript_id=transcript_id,
                                              transcript_version=base_version)
    if original is None:
        raise FileNotFoundError('源片或指定转写版本不存在')
    directory = store.transcripts_root() / store.source_id_for(object_key)
    with process_lock(directory / '.version.lock'):
        # 网络重复提交复用同一不可变版本；相同键不同内容明确冲突。
        for path in directory.glob('v*.json'):
            try:
                saved = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            receipt = saved.get('manual_revision') or {}
            if receipt.get('request_id') == request_id:
                if receipt.get('fingerprint') != fingerprint:
                    raise RevisionConflict('同一修订请求键的内容不同')
                return saved
        latest = store._latest_version(store.source_id_for(object_key))
        if latest != base_version:
            raise RevisionConflict(f'转写已更新为 v{latest}，请重新读取后修订')
        segments = copy.deepcopy(original['asr_segments'])
        audit = []
        for edit in normalized:
            index = edit['segment_index']
            if index >= len(segments):
                raise ValueError('段落序号超出源转写范围')
            before = str(segments[index].get('text') or '')
            after = edit['text']
            if before == after:
                continue
            segments[index]['text'] = after
            audit.append({'segment_index':index,'before':before,'after':after,
                          'start':segments[index].get('start'), 'end':segments[index].get('end')})
        if not audit:
            raise ValueError('没有文本变化，无需创建新版本')
        revised = copy.deepcopy(original)
        revised.update({'transcript_version':base_version+1,
                        'created_at_utc':datetime.now(UTC).isoformat(),
                        'asr_segments':segments,
                        'word_alignment_status':'original_alignment_preserved_manual_text_changed'})
        revised['manual_revision'] = {'request_id':request_id,'fingerprint':fingerprint,
            'base_version':base_version,'actor':subject,'reason':reason,
            'changes':audit,'review_status':'human_revised',
            'created_at_utc':revised['created_at_utc']}
        revised=review_document(revised,comparison=original)
        target=store._version_path(store.source_id_for(object_key),base_version+1)
        if target.exists():
            raise RevisionConflict('目标转写版本已存在')
        temporary=target.with_name(target.name+'.'+uuid.uuid4().hex+'.part')
        try:
            temporary.write_text(json.dumps(revised,ensure_ascii=False,indent=2),encoding='utf-8')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return revised
