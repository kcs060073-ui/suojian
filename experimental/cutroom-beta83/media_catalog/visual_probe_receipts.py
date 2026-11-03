"""Persist server-owned visual protocol contracts and safe shapes, never bodies.

Reading a receipt never re-labels old protocol history, calls a provider, or
provides permission to repeat a request. Pending/unknown is not a billing claim.
"""
import json
import re
import uuid
from datetime import datetime, timezone

from .model_profiles import _profile_row
from .visual_capabilities import VISUAL_CHECK_KINDS, visual_profile_digest
from .visual_profile_probe import STOP_POLICY, PROBE_MAX_TOKENS, visual_probe_request_binding, _request_hash
from .visual_protocol import (
    PROTOCOL_VERSION, protocol_contract, visual_protocol_mode,
)

_SAFE_CODES = {
    'MODEL_AUTH_FAILED','MODEL_CONFIGURATION_INVALID','MODEL_NETWORK_UNREACHABLE',
    'MODEL_OR_ENDPOINT_NOT_FOUND','MODEL_PROBE_FAILED','MODEL_PROTOCOL_MISMATCH',
    'MODEL_PROVIDER_HTTP_ERROR','MODEL_PROVIDER_REJECTED','MODEL_RESOURCE_NOT_GRANTED',
    'MODEL_VISUAL_MISMATCH','MODEL_VISUAL_PROTOCOL_INVALID',
    'MODEL_VISUAL_FINAL_RESULT_MISSING','MODEL_VISUAL_RESPONSE_INCOMPLETE',
    'MODEL_VISUAL_RESPONSE_REFUSED','MODEL_PROBE_OUTCOME_UNKNOWN',
    'MODEL_VISUAL_SCHEMA_INVALID','VISUAL_SCHEMA_INVALID','VISUAL_PROTOCOL_MODE_INVALID',
    'VISUAL_PROBE_RECEIPT_MISMATCH','VISUAL_PROBE_RECEIPT_INCOMPLETE','VISUAL_PROFILE_STALE','VISUAL_PROFILE_INVALID',
    'VISUAL_PROTOCOL_UNSUPPORTED','VISUAL_PROBE_BINDING_INVALID','VISUAL_PROBE_POLICY_INVALID',
}
_CONTRACT_KEYS = ('protocolVersion','protocolMode','schemaValidation','schemaVersion')


def _code(value):
    return value if isinstance(value,str) and value in _SAFE_CODES else None


def _same_contract(value,expected):
    return isinstance(value,dict) and all(value.get(k)==expected[k] for k in _CONTRACT_KEYS)


def _frozen_contract(value):
    if not isinstance(value,dict) or value.get('protocolVersion')!=PROTOCOL_VERSION:
        raise ValueError('PROBE_OPERATION_CONTRACT_INVALID')
    expected=protocol_contract(value.get('protocolMode'))
    if not _same_contract(value,expected):raise ValueError('PROBE_OPERATION_CONTRACT_INVALID')
    return expected


def _execution_metadata(value):
    # Only server-frozen metadata is eligible; provider/client additions are not.
    if not isinstance(value, dict) or not any(key in value for key in ('executionPolicy','requestBindingDigest')):
        return {}
    digest = value.get('requestBindingDigest')
    if value.get('executionPolicy') != STOP_POLICY or not isinstance(digest,str) or re.fullmatch(r'[0-9a-f]{64}',digest) is None:
        raise ValueError('PROBE_OPERATION_CONTRACT_INVALID')
    return {'executionPolicy':STOP_POLICY,'requestBindingDigest':digest}


def _shape(value,contract,request=False):
    if not _same_contract(value,contract):return None
    out=dict(contract)
    enums=({'responseFormat':{'none','json_schema','json_object'}} if request else {
        'envelopeKind':{'invalid','object'},'finishReason':{'stop','length','content_filter','tool_calls','function_call','missing','other'},
        'contentKind':{'text','parts','missing','other'}})
    for key,allowed in enums.items():
        raw=value.get(key)
        if isinstance(raw,str) and raw in allowed:out[key]=raw
    for key in (('imageCount','textPartCount','maxTokens') if request else ('choiceCount',)):
        raw=value.get(key)
        if type(raw) is int and 0<=raw<=131072:out[key]=raw
    if request:
        if value.get("reasoningEffort")=="low" and value.get("thinkingType")=="enabled":
            out.update(reasoningEffort="low", thinkingType="enabled")
    if not request:
        from .visual_protocol import safe_visual_diagnostics
        out.update(safe_visual_diagnostics(value))
        for key in ('hasReasoning','hasRefusal','hasToolCalls'):
            if type(value.get(key)) is bool:out[key]=value[key]
    return out


def sanitize_visual_receipt(value,*,expected_contract=None):
    contract=_frozen_contract(expected_contract) if expected_contract is not None else protocol_contract()
    execution=_execution_metadata(expected_contract)
    out={**contract,**execution,'checks':[]}
    if value is None:return out
    if execution and (not isinstance(value,dict) or any(value.get(k)!=v for k,v in execution.items())):
        out['diagnosticStatus']='contract_mismatch';return out
    if not _same_contract(value,contract):
        out['diagnosticStatus']='contract_mismatch';return out
    checks=value.get('checks')
    if not isinstance(checks,list):
        out['diagnosticStatus']='contract_mismatch';return out
    if len(checks)>3 or value.get('diagnosticStatus')=='check_count_invalid':
        out['diagnosticStatus']='check_count_invalid'
    seen=set()
    for entry in checks[:3]:
        if not isinstance(entry,dict) or entry.get('kind') not in VISUAL_CHECK_KINDS or entry['kind'] in seen:continue
        seen.add(entry['kind']);row={'kind':entry['kind']}
        status=entry.get('status')
        if isinstance(status,str) and status in {'available','unavailable'}:row['status']=status
        code=_code(entry.get('errorCode'))
        if code:row['errorCode']=code
        for key in ('request','response'):
            raw=entry.get(key)
            row[key]=_shape(raw,contract,key=='request')
            if isinstance(raw,dict) and row[key] is None:out['diagnosticStatus']='contract_mismatch'
        if row['request'] is not None:
            expected_format = ('json_object' if contract['schemaValidation']=='local' else 'json_schema') if row['kind']=='structured_output' else 'none'
            if row['request'].get('responseFormat') != expected_format:
                row['request']=None
                out['diagnosticStatus']='contract_mismatch'
        out['checks'].append(row)
    return out


def _complete_success(value, row=None):
    from .visual_contact_sheet import transport_mode
    mode=transport_mode(json.loads(row["settings_json"] or "{}")) if row is not None else "native"
    if value.get('diagnosticStatus'):return False
    checks=value['checks']
    if len(checks)!=3 or {entry['kind'] for entry in checks}!=set(VISUAL_CHECK_KINDS):return False
    for entry in checks:
        request,response=entry.get('request'),entry.get('response')
        if entry.get('status')!='available' or entry.get('errorCode') or request is None or response is None:return False
        images=1 if entry['kind']=='image' or mode=='contact_sheet_v1' else 2
        if request.get('imageCount')!=images or request.get('maxTokens')!=PROBE_MAX_TOKENS or type(request.get('textPartCount')) is not int or request['textPartCount']<1:return False
        if (response.get('envelopeKind')!='object' or response.get('choiceCount')!=1 or response.get('finishReason')!='stop'
                or response.get('contentKind') not in {'text','parts'} or type(response.get('hasReasoning')) is not bool
                or response.get('hasRefusal') is not False or response.get('hasToolCalls') is not False):return False
    return True


def begin_visual_probe(db,profile_id,*,request_binding=None):
    """Freeze a server-reviewed request identity; preparation grants no approval.

    Public callers do not accept a raw binding from a browser. Approval and
    one-time dispatch consumption must be enforced by the eventual executor.
    """
    operation_id=uuid.uuid4().hex
    with db.transaction(immediate=True) as conn:
        row=_profile_row(db,profile_id)
        if row is None or row['capability']!='visual_analysis':return None
        contract=protocol_contract(visual_protocol_mode(json.loads(row['settings_json'] or '{}')))
        execution={}
        if request_binding is not None:
            expected=visual_probe_request_binding(row)
            if not isinstance(request_binding,dict) or request_binding!=expected:
                raise ValueError('VISUAL_PROBE_BINDING_INVALID')
            execution={'executionPolicy':STOP_POLICY,'requestBindingDigest':_request_hash(expected)}
        conn.execute("INSERT INTO visual_profile_probe_runs(operation_id,profile_id,config_digest,protocol_version,status,diagnostic_json) VALUES(?,?,?,?,'pending',?)",
                     (operation_id,profile_id,visual_profile_digest(row),contract['protocolVersion'],
                      json.dumps({**contract,**execution,'checks':[]},sort_keys=True)))
    return operation_id


def get_visual_probe(db,operation_id):
    row=db.connection.execute('SELECT * FROM visual_profile_probe_runs WHERE operation_id=?',(operation_id,)).fetchone()
    if row is None:return None
    # History is immutable evidence. Do not normalize a stored v2 result to v3.
    return {'operationId':row['operation_id'],'profileId':row['profile_id'],'configDigest':row['config_digest'],
            'protocolVersion':row['protocol_version'],'status':row['status'],'startedAt':row['started_at'],
            'finishedAt':row['finished_at'],'errorCode':row['error_code'],
            'diagnostic':json.loads(row['diagnostic_json']) if row['diagnostic_json'] else None,'canRetry':False}


def finish_visual_probe(db,operation_id,*,status,receipt=None,error_code=None):
    if status not in {'succeeded','failed','unknown'}:raise ValueError('PROBE_STATUS_INVALID')
    with db.transaction(immediate=True) as conn:
        old=get_visual_probe(db,operation_id)
        if old is None or old['status']!='pending':raise ValueError('PROBE_OPERATION_CLOSED')
        contract={**_frozen_contract(old['diagnostic']),**_execution_metadata(old['diagnostic'])}
        row=_profile_row(db,old['profileId'])
        try:
            current_digest=visual_profile_digest(row) if row is not None else None
            binding_current = (not contract.get('executionPolicy') or
                (row is not None and _request_hash(visual_probe_request_binding(row))==contract['requestBindingDigest']))
        except (ValueError,TypeError,KeyError):
            current_digest=None;binding_current=False
        if current_digest!=old['configDigest'] or not binding_current or error_code=='VISUAL_PROFILE_STALE':
            status='stale';error_code='VISUAL_PROFILE_STALE'
        elif error_code=='MODEL_PROBE_OUTCOME_UNKNOWN':
            status='unknown'
        safe=sanitize_visual_receipt(receipt,expected_contract=contract)
        if status=='succeeded' and (receipt is None or safe.get('diagnosticStatus')=='contract_mismatch'):
            status='failed';error_code='VISUAL_PROBE_RECEIPT_MISMATCH'
        elif status=='succeeded' and (len(receipt.get('checks',[]))!=3 or not _complete_success(safe,row) or not _policy_receipt_matches(row,safe)):
            status='failed';error_code='VISUAL_PROBE_RECEIPT_INCOMPLETE'
        if status=='failed' and error_code in {'VISUAL_PROBE_RECEIPT_MISMATCH','VISUAL_PROBE_RECEIPT_INCOMPLETE'}:
            # Same writer lock and matching digest: malformed receipts cannot
            # leave this exact configuration newly available. Never touch a
            # configuration changed while the operation was running.
            conn.execute("UPDATE model_profiles SET availability_status='unavailable',last_checked_at=datetime('now'),last_error_code=?,last_error_message=? WHERE profile_id=?",
                         (error_code,'未取得完整且与本次协议一致的检测回执。',old['profileId']))
        conn.execute("UPDATE visual_profile_probe_runs SET status=?,finished_at=datetime('now'),error_code=?,diagnostic_json=? WHERE operation_id=?",
                     (status,_code(error_code),json.dumps(safe,ensure_ascii=False,sort_keys=True),operation_id))
    return get_visual_probe(db,operation_id)


def latest_visual_probe(db,profile_id):
    row=db.connection.execute('SELECT operation_id FROM visual_profile_probe_runs WHERE profile_id=? ORDER BY rowid DESC LIMIT 1',(profile_id,)).fetchone()
    return get_visual_probe(db,row['operation_id']) if row else None


def reconcile_visual_probe_receipt(db, operation_id, *, expected_receipt_sha256, reviewed_by, reason):
    """Explicit local revalidation of a stored budget-validator failure, no dispatch.

    The failed operation remains immutable. The new record identifies local
    reconciliation and cannot extend the age of the original visual evidence.
    """
    from .model_profiles import PROFILE_CHECK_MAX_AGE, _parse_iso
    if not all(isinstance(v,str) and v.strip() for v in (reviewed_by,reason)):
        raise ValueError('VISUAL_PROBE_RECOVERY_REVIEW_REQUIRED')
    if len(reviewed_by)>200 or len(reason)>2000:
        raise ValueError('VISUAL_PROBE_RECOVERY_REVIEW_REQUIRED')
    now=datetime.now(timezone.utc)
    def recent(value):
        stamp=_parse_iso(value)
        if stamp is None or not 0 <= (now-stamp).total_seconds() <= PROFILE_CHECK_MAX_AGE.total_seconds():
            raise ValueError('VISUAL_PROBE_RECOVERY_STALE')
        return stamp
    with db.transaction(immediate=True) as conn:
        old=get_visual_probe(db,operation_id)
        if old is None or old['status']!='failed' or old['errorCode']!='VISUAL_PROBE_RECEIPT_INCOMPLETE':
            raise ValueError('VISUAL_PROBE_RECOVERY_INELIGIBLE')
        diagnostic=old['diagnostic']
        if _request_hash(diagnostic)!=expected_receipt_sha256:
            raise ValueError('VISUAL_PROBE_RECOVERY_EVIDENCE_CHANGED')
        latest=latest_visual_probe(db,old['profileId'])
        recovery=(latest.get('diagnostic') or {}).get('localRecovery',{})
        idempotent=(latest['status']=='succeeded' and recovery.get('sourceOperationId')==operation_id
                    and recovery.get('sourceReceiptSha256')==expected_receipt_sha256)
        if latest['operationId']!=operation_id and not idempotent:
            raise ValueError('VISUAL_PROBE_RECOVERY_NOT_LATEST')
        row=_profile_row(db,old['profileId'])
        if (row is None or not row['enabled'] or row['capability']!='visual_analysis'
                or old['configDigest']!=visual_profile_digest(row)
                or diagnostic.get('executionPolicy')!=STOP_POLICY
                or diagnostic.get('requestBindingDigest')!=_request_hash(visual_probe_request_binding(row))):
            raise ValueError('VISUAL_PROBE_RECOVERY_STALE')
        safe=sanitize_visual_receipt(diagnostic,expected_contract=diagnostic)
        if safe.get('diagnosticStatus') or not _complete_success(safe,row) or not _policy_receipt_matches(row,safe):
            raise ValueError('VISUAL_PROBE_RECOVERY_INELIGIBLE')
        original_time=recent(old['finishedAt'])
        checks=conn.execute('SELECT check_kind,status,config_digest,checked_at FROM visual_profile_checks WHERE profile_id=?',(old['profileId'],)).fetchall()
        if len(checks)!=3 or {c['check_kind'] for c in checks}!=set(VISUAL_CHECK_KINDS):
            raise ValueError('VISUAL_PROBE_RECOVERY_STALE')
        for check in checks:
            if check['status']!='available' or check['config_digest']!=old['configDigest']:
                raise ValueError('VISUAL_PROBE_RECOVERY_STALE')
            original_time=min(original_time,recent(check['checked_at']))
        if idempotent:return latest
        recovered_id=uuid.uuid4().hex
        safe['localRecovery']={'kind':'local_receipt_reconciliation','sourceOperationId':operation_id,
            'sourceReceiptSha256':expected_receipt_sha256,'reviewedBy':reviewed_by.strip(),
            'reason':reason.strip(),'reviewedAt':now.isoformat(),'originalCheckedAt':original_time.isoformat()}
        # Use this connection directly: nested helpers would commit mid-operation.
        conn.execute("INSERT INTO visual_profile_probe_runs(operation_id,profile_id,config_digest,protocol_version,status,diagnostic_json,started_at,finished_at) VALUES(?,?,?,?,'succeeded',?,?,?)",
            (recovered_id,old['profileId'],old['configDigest'],old['protocolVersion'],json.dumps(safe,ensure_ascii=False,sort_keys=True),now.isoformat(),now.isoformat()))
        conn.execute("UPDATE model_profiles SET availability_status='available',last_checked_at=?,last_error_code=NULL,last_error_message=NULL,updated_at=datetime('now') WHERE profile_id=?",
            (original_time.isoformat(),old['profileId']))
    return get_visual_probe(db,recovered_id)


def _policy_receipt_matches(row, receipt):
    from .visual_request_policy import visual_request_options
    options = visual_request_options(row)
    expected = {'reasoningEffort':'low','thinkingType':'enabled'} if options else {}
    return all({k:v for k,v in check.get('request',{}).items() if k in {'reasoningEffort','thinkingType'}} == expected
               for check in receipt.get('checks',[]))
