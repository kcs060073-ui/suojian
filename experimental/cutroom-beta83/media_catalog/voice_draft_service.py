"""Server-owned voice draft facade. Saving never activates audio or gates.

A revision-zero editor head is bookkeeping, not a current-voice ownership claim.
No provider, worker, apply or audio receipt is exposed by this facade.
Context recovery rebinds an unused ledger, or a strictly proven formal script rework.
Neither path activates audio or issues generation requests.
"""
from copy import deepcopy
import json
import re
from pathlib import Path
from .provenance import canonical_digest
from .voice_editing import VoiceEditing, compile_voice_input
from .voice_task_context import resolve_voice_task_context, compile_task_voice_request

class VoiceDraftError(ValueError):
    def __init__(self,code,message,status=409):
        self.code,self.message,self.status=code,message,status
        super().__init__(code)

def _service(db,root,task_id):
    return VoiceEditing(db,Path(root)/'runtime/workbench/tasks'/task_id/'artifacts/voice-editing')

def _head(db,task_id,service):
    row=db.connection.execute('SELECT 1 FROM voice_edit_heads WHERE task_id=?',(task_id,)).fetchone()
    return service.current(task_id) if row else None

def _owned_draft(db,task_id,draft_id,service):
    row=db.connection.execute('SELECT task_id FROM voice_edit_drafts WHERE draft_id=?',(draft_id,)).fetchone()
    if row is None or row['task_id']!=task_id:
        raise VoiceDraftError('VOICE_DRAFT_NOT_FOUND','本任务没有这份配音草稿。',404)
    return service.draft(draft_id)

def _project(draft,head,context,db,root):
    current=(draft['status']=='draft' and head is not None and draft['base_revision']==head['revision']
             and draft['context_digest']==head['context_digest']==context.context_digest)
    from .voice_bundle_service import _segments,_generation_plan
    maximum=None;reason=None
    # Saved draft identity must remain available even if generation cannot be planned.
    try:
        if current:
            request=db.connection.execute("SELECT q.request_id FROM voice_edit_requests q WHERE q.draft_id=? AND q.status IN ('prepared','running','unknown') AND COALESCE(q.error_code,'')!='VOICE_REQUEST_RELEASED' ORDER BY rowid DESC LIMIT 1",(draft['draft_id'],)).fetchone()
            _,plan,_=_generation_plan(db,root,draft['task_id'],draft,context,head,current_request_id=request['request_id'] if request else None)
            maximum=plan['maximum_requests']
        else:
            maximum=len(_segments(draft['config'],[dict(r) for r in db.connection.execute('SELECT * FROM script_segments WHERE task_id=? AND script_revision=? ORDER BY idx',(draft['task_id'],context.script_sha256))]))
    except (ValueError,OSError) as error:reason=getattr(error,'code','VOICE_BUNDLE_UNAVAILABLE');maximum=None
    return dict(draftId=draft['draft_id'],taskId=draft['task_id'],baseRevision=draft['base_revision'],
                contextDigest=draft['context_digest'],status=draft['status'],voiceId=draft['config']['voice']['voice'],
                inputFingerprint=draft['config']['fingerprint'],
                hasChanges=head is None or draft['config']['fingerprint']!=head['config']['fingerprint'],
                isCurrent=current,canApply=False,canGenerate=current and reason is None and maximum>0,maximumRequests=maximum,generationBlockedReason=reason)

def _context_recovery_block(db,task_id,head,context):
    # Check ALL requests, including cancelled drafts and previous contexts.
    if db.connection.execute("""SELECT 1 FROM voice_edit_requests q JOIN voice_edit_drafts d
            ON d.draft_id=q.draft_id WHERE d.task_id=? AND q.status IN ('prepared','running','unknown') AND COALESCE(q.error_code,'')!='VOICE_REQUEST_RELEASED' LIMIT 1""",(task_id,)).fetchone():
        return 'VOICE_GENERATION_UNRESOLVED'
    if head is None or head['audio'] is not None or head['config']['rules'] or head['config']['voice']!=context.voice:
        return 'CONTEXT_CHANGED'
    from .voice_ownership import read_voice_ownership
    if read_voice_ownership(db,task_id).branch!='legacy':return 'CONTEXT_CHANGED'
    if db.connection.execute('SELECT 1 FROM voice_edit_versions WHERE task_id=? AND audio_json IS NOT NULL LIMIT 1',(task_id,)).fetchone():
        return 'CONTEXT_CHANGED'
    baseline=compile_voice_input(head['config']['script'],[],context.voice,{'substitutions':{}})
    if baseline['fingerprint']!=head['config']['fingerprint']:return 'CONTEXT_CHANGED'
    return None


def recover_voice_context(db,root,task_id,body):
    """Explicit, idempotent bookkeeping repair. Never touches formal task state.

    Both source and target identities are required, then rechecked under the
    writer lock. Old versions/drafts/requests remain immutable and stale.
    """
    required={'expectedRevision','previousContextDigest','contextDigest'}
    if (not isinstance(body,dict) or set(body)!=required
            or type(body['expectedRevision']) is not int or body['expectedRevision']<0
            or any(not isinstance(body[k],str) or re.fullmatch(r'[0-9a-f]{64}',body[k]) is None
                   for k in ('previousContextDigest','contextDigest'))):
        raise VoiceDraftError('VOICE_REQUEST_INVALID','配音上下文恢复请求无效。',400)
    context=resolve_voice_task_context(db,root,task_id)
    service=_service(db,root,task_id)
    # A confirmed script rework has its own narrow proof; ordinary recovery
    # retains the original no-audio/no-history rules below.
    existing_head=_head(db,task_id,service)
    if existing_head is not None and _context_recovery_block(db,task_id,existing_head,context)=='CONTEXT_CHANGED':
        from .voice_script_rework import recover_confirmed_script_voice
        from .task_script import ScriptDraftError
        try:
            if recover_confirmed_script_voice(db,root,task_id,body):
                return voice_draft_session(db,root,task_id)
        except ScriptDraftError as error:
            raise VoiceDraftError(error.code,error.message,error.status) from error
    baseline=compile_voice_input(context.script,[],context.voice,{'substitutions':{}})
    def guard():
        latest=resolve_voice_task_context(db,root,task_id)
        if latest.context_digest!=body['contextDigest'] or latest.context_digest!=context.context_digest:
            raise VoiceDraftError('CONTEXT_CHANGED','原稿或配音服务再次变化，请重新加载本片。')
        code=_context_recovery_block(db,task_id,_head(db,task_id,service),latest)
        if code:raise VoiceDraftError(code,'旧配音记录尚不能安全恢复，请先核对原请求或声音版本。')
    _domain(lambda:service.rebind_upstream(task_id,body['expectedRevision'],body['previousContextDigest'],
        body['contextDigest'],baseline,route='ad_mix',apply_effects=lambda *args:None,pre_write_check=guard))
    return voice_draft_session(db,root,task_id)


def voice_draft_session(db,root,task_id):
    context=resolve_voice_task_context(db,root,task_id,include_voice_catalog=True)
    service=_service(db,root,task_id);head=_head(db,task_id,service)
    public_head=dict(revision=head['revision'] if head else 0,initialized=head is not None,
                     voiceId=head['config']['voice']['voice'] if head else context.voice['voice'],
                     contextMatches=head is None or head['context_digest']==context.context_digest,
                     readingRules=[dict(rule,start=len(context.script[:rule['start']].encode('utf-16-le'))//2,
                        end=len(context.script[:rule['end']].encode('utf-16-le'))//2) for rule in head['config']['rules']] if head and head['context_digest']==context.context_digest else [])
    public_context=context.public_payload()
    from .voice_bundle_service import _segments
    baseline=compile_voice_input(context.script,[],context.voice,{'substitutions':{}})
    public_context['readingSupport']=dict(status='literal_text_substitution',pronunciation='unverified',numericReading='blocked')
    public_context['maximumRequests']=len(_segments(baseline,[dict(r) for r in db.connection.execute('SELECT * FROM script_segments WHERE task_id=? AND script_revision=? ORDER BY idx',(task_id,context.script_sha256))]))
    recovery=[]
    if head:
        records=db.connection.execute("""SELECT d.draft_id,d.task_id,d.base_revision,d.context_digest,d.config_json,q.request_id,q.status
          FROM voice_edit_drafts d JOIN voice_edit_requests q ON q.draft_id=d.draft_id
          WHERE d.task_id=? AND d.status IN ('draft','cancelled')
           AND COALESCE(q.error_code,'')!='VOICE_REQUEST_RELEASED'
           AND ((d.base_revision=? AND d.context_digest=? AND q.status IN ('prepared','running','unknown','ready')
                 AND (d.status='draft' OR q.status!='ready'))
             OR (q.status IN ('running','unknown') AND EXISTS(SELECT 1 FROM voice_generation_runs g
                  JOIN jobs j ON j.job_id=g.job_id WHERE g.request_id=q.request_id AND j.status IN ('interrupted','failed','cancelled'))))
           AND q.rowid=(SELECT MAX(latest.rowid) FROM voice_edit_requests latest WHERE latest.draft_id=d.draft_id)
           ORDER BY CASE WHEN q.status IN ('running','unknown') THEN 0 ELSE 1 END,q.rowid DESC LIMIT 10""",
           (task_id,head['revision'],context.context_digest))
        for row in records:
            config=json.loads(row['config_json'])
            stale=row['base_revision']!=head['revision'] or row['context_digest']!=context.context_digest or head['context_digest']!=context.context_digest
            recovery.append(dict(draftId=row['draft_id'],taskId=task_id,baseRevision=row['base_revision'],
                contextDigest=row['context_digest'],status=row['status'],requestId=row['request_id'],voiceId=config['voice']['voice'],
                releaseOnly=stale,
                readingRules=[] if stale else [dict(rule,start=len(context.script[:rule['start']].encode('utf-16-le'))//2,
                    end=len(context.script[:rule['end']].encode('utf-16-le'))//2) for rule in config['rules']]))
    blocked=_context_recovery_block(db,task_id,head,context) if head and not public_head['contextMatches'] else None
    if blocked=='CONTEXT_CHANGED':
        from .voice_script_rework import confirmed_script_voice_plan
        from .task_script import ScriptDraftError
        try:
            plan=confirmed_script_voice_plan(db,root,task_id)
            if (plan is not None and plan['head']['revision']==head['revision']
                    and plan['head']['context_digest']==head['context_digest']
                    and plan['context'].context_digest==context.context_digest):
                blocked=None
        except ScriptDraftError:
            pass
    return dict(context=public_context,head=public_head,recoveryDrafts=recovery,
                contextRecovery=dict(canRecover=head is not None and not public_head['contextMatches'] and blocked is None,
                    previousContextDigest=head['context_digest'] if head else None,blockedReason=blocked))

def _request(body):
    required={'requestId','baseRevision','contextDigest','voiceId','rules'}
    if not isinstance(body,dict) or not required<=set(body) or set(body)-required-{'editSpan'}:
        raise VoiceDraftError('VOICE_REQUEST_INVALID','配音草稿请求字段无效。',400)
    if (not isinstance(body['requestId'],str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}',body['requestId']) is None
        or type(body['baseRevision']) is not int or body['baseRevision']<0
        or not isinstance(body['contextDigest'],str) or re.fullmatch(r'[0-9a-f]{64}',body['contextDigest']) is None
        or not isinstance(body['voiceId'],str) or not body['voiceId'].strip() or len(body['voiceId'])>256
        or not isinstance(body['rules'],list)):
        raise VoiceDraftError('VOICE_REQUEST_INVALID','配音草稿的操作编号、版本或选区无效。',400)
    return deepcopy(body)

def _domain(action):
    try:return action()
    except ValueError as error:
        code=str(error)
        allowed={'VERSION_CONFLICT','CONTEXT_CHANGED','VOICE_ALREADY_INITIALIZED','DRAFT_REQUEST_CONFLICT','REQUEST_ID_CONFLICT',
                 'DRAFT_ID_INVALID','VOICE_RECORD_NOT_FOUND','ALREADY_APPLIED','DRAFT_CLOSED',
                 'SCRIPT_CHANGE_REQUIRES_SCRIPT_WORKFLOW','VOICE_DRAFT_CONFLICT','VOICE_DRAFT_ID_INVALID','UPSTREAM_CONTEXT_NOT_CHANGED'}
        if code not in allowed:raise
        raise VoiceDraftError(code,'配音草稿与当前版本不一致，请重新加载本片。') from error

def _compile_draft_request(db,root,task_id,body):
    request=dict(context_digest=body['contextDigest'],voice_id=body['voiceId'],rules=body['rules'])
    compiled=compile_task_voice_request(db,root,task_id,request,reading_strategy='literal_text_substitution')
    if 'editSpan' not in body:return compiled  # Legacy callers submit their full explicit rule set.
    context=resolve_voice_task_context(db,root,task_id)
    span=body['editSpan']
    if span is not None:
        from .voice_task_context import utf16_span_to_unicode
        if not isinstance(span,dict) or set(span)!={'start','end','original'}:raise VoiceDraftError('VOICE_RULE_INVALID','读法选区无效。',400)
        start,end=utf16_span_to_unicode(context.script,span['start'],span['end'])
        if context.script[start:end]!=span['original']:raise VoiceDraftError('VOICE_RULE_INVALID','读法选区已变化。')
        if any(not start<=r['start']<r['end']<=end for r in compiled['config']['rules']):raise VoiceDraftError('VOICE_RULE_INVALID','规则必须位于本次编辑选区内。',400)
    elif body['rules']:raise VoiceDraftError('VOICE_RULE_INVALID','无选区时只能修改音色。',400)
    service=_service(db,root,task_id);head=_head(db,task_id,service)
    previous=[]
    if head is not None:
        # Replay merges against the originally requested revision, not a later head.
        row=db.connection.execute('SELECT context_digest,config_json FROM voice_edit_versions WHERE task_id=? AND revision=?',(task_id,body['baseRevision'])).fetchone()
        if row is None:raise VoiceDraftError('VERSION_CONFLICT','声音版本已变化，请重新加载。')
        if row['context_digest']!=context.context_digest:raise VoiceDraftError('CONTEXT_CHANGED','原稿或配音服务已变化。')
        import json
        base=json.loads(row['config_json'])
        if base['script']!=context.script:raise VoiceDraftError('CONTEXT_CHANGED','原稿已变化。')
        previous=base['rules']
    retained=[];inside=[]
    for rule in previous:
        overlap=span is not None and rule['start']<end and start<rule['end']
        if overlap and not start<=rule['start']<rule['end']<=end:raise VoiceDraftError('VOICE_EDIT_SPAN_PARTIAL_OVERLAP','请选择已有读法的完整原文区间。')
        if not overlap:retained.append(rule)
        else:inside.append(rule)
    unchanged={(r['start'],r['end'],r['original'],r['reading']):r for r in previous}
    edited=[unchanged.get((r['start'],r['end'],r['original'],r['reading']),r) for r in compiled['config']['rules']]
    if span is not None and inside and len(edited)==1 and edited[0]['start']==start and edited[0]['end']==end:
        cursor=start;pieces=[]
        for rule in inside:
            pieces.extend([context.script[cursor:rule['start']],rule['reading']]);cursor=rule['end']
        pieces.append(context.script[cursor:end])
        if edited[0]['reading']==''.join(pieces):edited=inside
    rules=sorted(retained+edited,key=lambda r:r['start'])
    substitutions={}
    for rule in rules:substitutions.setdefault(rule['original'],[]).append(rule['reading'])
    compiled['config']=compile_voice_input(context.script,rules,compiled['config']['voice'],{'substitutions':substitutions})
    return compiled


def save_voice_draft(db,root,task_id,body):
    body=_request(body)
    compiled=_compile_draft_request(db,root,task_id,body)
    context=resolve_voice_task_context(db,root,task_id)
    if context.context_digest!=body['contextDigest']:
        raise VoiceDraftError('CONTEXT_CHANGED','原稿或配音服务已变化，请重新加载。')
    service=_service(db,root,task_id)
    draft_id=canonical_digest(dict(task_id=task_id,operation_id=body['requestId'],operation='voice_draft_v1'))
    def guard():
        latest=_compile_draft_request(db,root,task_id,body)
        if latest['context_digest']!=compiled['context_digest'] or latest['config']['fingerprint']!=compiled['config']['fingerprint']:
            raise VoiceDraftError('CONTEXT_CHANGED','原稿或配音服务已变化，请重新加载。')
    baseline=compile_voice_input(context.script,[],context.voice,{'substitutions':{}})
    draft=_domain(lambda:service.create_draft(task_id,body['baseRevision'],compiled['context_digest'],compiled['config'],
                                            draft_id=draft_id,pre_write_check=guard,initial_config=baseline,route='ad_mix'))
    context=resolve_voice_task_context(db,root,task_id)
    return _project(draft,_head(db,task_id,service),context,db,root)

def get_voice_draft(db,root,task_id,draft_id):
    service=_service(db,root,task_id)
    draft=_owned_draft(db,task_id,draft_id,service)
    context=resolve_voice_task_context(db,root,task_id)
    return _project(draft,_head(db,task_id,service),context,db,root)

def cancel_voice_draft(db,root,task_id,draft_id,body):
    if not isinstance(body,dict) or body:
        raise VoiceDraftError('VOICE_REQUEST_INVALID','取消草稿不接受声音或配置回执。',400)
    service=_service(db,root,task_id)
    _owned_draft(db,task_id,draft_id,service)
    # Cancellation is safe cleanup even if the authoritative context changed.
    draft=_domain(lambda:service.cancel(draft_id))
    return dict(draftId=draft['draft_id'],taskId=task_id,status=draft['status'],canApply=False,canGenerate=False)
