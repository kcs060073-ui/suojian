"""Server approval consumption only; no approval writer, network or credentials.

Existing job leases/paid deduplication remain authoritative. The proposed ledger
must be installed by the coordinator, and real rows require original approval
evidence. A frozen package by itself never grants permission.
"""
import base64
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid

from .visual_protocol import protocol_contract

POLICY = 'approved-visual-execution-v1'
_SHA = re.compile('[0-9a-f]{64}')
_CONFIG = {'profileId','accountId','enabled','configDigest','credentialRefSha256',
           'endpointSha256','provider','model','protocol'}
_FAILURES = {'MODEL_AUTH_FAILED','MODEL_PROVIDER_HTTP_ERROR','MODEL_PROVIDER_REJECTED',
             'MODEL_VISUAL_SCHEMA_INVALID','MODEL_VISUAL_RESPONSE_REFUSED',
             'MODEL_VISUAL_RESPONSE_INCOMPLETE','MODEL_VISUAL_FINAL_RESULT_MISSING',
             'MODEL_VISUAL_PROTOCOL_INVALID','MODEL_VISUAL_MISMATCH'}

class ApprovedVisualError(ValueError):
    def __init__(self, code): self.code=code; super().__init__(code)

def _fail(code): raise ApprovedVisualError(code)
def _json(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',', ':'),allow_nan=False)
def _digest(value): return hashlib.sha256(_json(value).encode()).hexdigest()
def _sha(value): return isinstance(value,str) and _SHA.fullmatch(value) is not None
def _id(value): return isinstance(value,str) and re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:-]{0,159}',value) is not None
def _now(clock):
    value=(clock or time.time)()
    if type(value) not in (int,float) or not math.isfinite(value) or value<0:
        _fail('VISUAL_EXECUTION_CLOCK_INVALID')
    return value

def freeze_visual_execution(config, requests):
    """Prepare exact review material, not approval. Only the original two/6 scope."""
    config,requests=deepcopy(config),deepcopy(requests)
    if (not isinstance(config,dict) or set(config)!=_CONFIG or config['enabled'] is not True
        or any(not _sha(config[k]) for k in ('configDigest','credentialRefSha256','endpointSha256'))
        or any(not isinstance(config[k],str) or not config[k].strip() or len(config[k])>256
               for k in ('profileId','accountId','provider','model'))):
        _fail('VISUAL_EXECUTION_INPUT_INVALID')
    contract=config['protocol']
    if not isinstance(contract,dict) or contract!=protocol_contract(contract.get('protocolMode')):
        _fail('VISUAL_EXECUTION_INPUT_INVALID')
    if not isinstance(requests,list) or len(requests)!=2:
        _fail('VISUAL_EXECUTION_SCOPE_INVALID')
    frozen=[];ids=set();materials=set();total_frames=0
    for request in requests:
        if (not isinstance(request,dict) or set(request)!={'requestId','materialId','materialFingerprint',
                'sourceStartUs','sourceEndUs','frames','payload','timeoutSeconds'}):
            _fail('VISUAL_EXECUTION_INPUT_INVALID')
        rid=request['requestId'];mid=request['materialId']
        start,end=request['sourceStartUs'],request['sourceEndUs']
        if (not _id(rid) or rid in ids or not _id(mid) or mid in materials
            or not isinstance(request['materialFingerprint'],str) or not request['materialFingerprint']
            or type(start) is not int or type(end) is not int or not 0<=start<end
            or type(request['timeoutSeconds']) not in (int,float)
            or not math.isfinite(request['timeoutSeconds']) or not 0<request['timeoutSeconds']<=60):
            _fail('VISUAL_EXECUTION_INPUT_INVALID')
        ids.add(rid);materials.add(mid)
        payload=request['payload'];frames=request['frames']
        if (not isinstance(payload,dict) or payload.get('model')!=config['model']
            or type(payload.get('max_tokens')) is not int or not 1<=payload['max_tokens']<=350
            or not isinstance(frames,list) or not 1<=len(frames)<=6):
            _fail('VISUAL_EXECUTION_INPUT_INVALID')
        response_format=payload.get('response_format')
        expected='json_object' if contract['schemaValidation']=='local' else 'json_schema'
        if not isinstance(response_format,dict) or response_format.get('type')!=expected:
            _fail('VISUAL_EXECUTION_INPUT_INVALID')
        messages=payload.get('messages');urls=[];texts=[]
        if not isinstance(messages,list) or not messages:_fail('VISUAL_EXECUTION_INPUT_INVALID')
        for message in messages:
            if not isinstance(message,dict) or message.get('role') not in ('system','user'):
                _fail('VISUAL_EXECUTION_INPUT_INVALID')
            content=message.get('content')
            if isinstance(content,str):texts.append([message['role'],content]);continue
            if not isinstance(content,list):_fail('VISUAL_EXECUTION_INPUT_INVALID')
            for part in content:
                if not isinstance(part,dict):_fail('VISUAL_EXECUTION_INPUT_INVALID')
                if part.get('type')=='text' and isinstance(part.get('text'),str):
                    texts.append([message['role'],part['text']])
                elif part.get('type')=='image_url' and isinstance(part.get('image_url'),dict):
                    urls.append(part['image_url'].get('url'))
                else:_fail('VISUAL_EXECUTION_INPUT_INVALID')
        if not texts or len(urls)!=len(frames):_fail('VISUAL_EXECUTION_INPUT_INVALID')
        refs=[];at=set()
        for frame,url in zip(frames,urls):
            if (not isinstance(frame,dict) or set(frame)!={'atUs','dataUrl'} or frame['dataUrl']!=url
                or type(frame['atUs']) is not int or not start<=frame['atUs']<=end or frame['atUs'] in at
                or not isinstance(url,str) or len(url)>14_000_000
                or re.fullmatch(r'data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+',url) is None):
                _fail('VISUAL_EXECUTION_INPUT_INVALID')
            try:raw=base64.b64decode(url.split(',',1)[1],validate=True)
            except ValueError:_fail('VISUAL_EXECUTION_INPUT_INVALID')
            if not raw or len(raw)>10_000_000:_fail('VISUAL_EXECUTION_INPUT_INVALID')
            at.add(frame['atUs']);refs.append(dict(atUs=frame['atUs'],sha256=hashlib.sha256(raw).hexdigest()))
        total_frames+=len(refs)
        frozen.append(dict(requestId=rid,materialId=mid,materialFingerprint=request['materialFingerprint'],
            sourceStartUs=start,sourceEndUs=end,frames=refs,requestSha256=_digest(payload),
            promptSha256=_digest(texts),responseFormatSha256=_digest(response_format),
            maxTokens=payload['max_tokens'],timeoutSeconds=request['timeoutSeconds']))
    if total_frames!=6:_fail('VISUAL_EXECUTION_SCOPE_INVALID')
    return dict(executionPolicy=POLICY,scopeKind='original_remaining_business',config=config,requests=frozen)

@contextmanager
def _write(db):
    try:
        with db.transaction(immediate=True) as conn:yield conn
    except sqlite3.OperationalError as error:
        _fail('VISUAL_EXECUTION_BUSY' if 'locked' in str(error) else 'VISUAL_APPROVAL_LEDGER_UNAVAILABLE')

def _approval(conn, approval_id, expected_digest, now):
    row=conn.execute('SELECT * FROM visual_execution_approvals WHERE approval_id=?',(approval_id,)).fetchone()
    if row is None:_fail('VISUAL_APPROVAL_REQUIRED')
    try:
        valid=(isinstance(row['approval_ref'],str) and bool(row['approval_ref'].strip())
            and _sha(row['evidence_sha256']) and _sha(row['prior_use_evidence_sha256'])
            and row['scope_kind']=='original_remaining_business' and row['total_limit']==5 and row['prior_used']==3
            and _digest(json.loads(row['package_json']))==row['package_sha256'])
    except (TypeError,ValueError):valid=False
    if not valid:_fail('VISUAL_APPROVAL_INVALID')
    if row['status']=='cancelled':_fail('VISUAL_EXECUTION_CANCELLED')
    if row['status'] not in ('approved','reserved'):_fail('VISUAL_APPROVAL_INVALID')
    if type(row['expires_at']) not in (int,float) or not math.isfinite(row['expires_at']):_fail('VISUAL_APPROVAL_INVALID')
    if now>=row['expires_at']:_fail('VISUAL_APPROVAL_EXPIRED')
    if row['package_sha256']!=expected_digest:_fail('VISUAL_EXECUTION_STALE')
    return row

def _job(conn,job_id,worker_id,operation_id,approval_id,package_digest,now):
    row=conn.execute('''SELECT j.*,p.payload_json,p.payload_digest,p.cost_status,p.requested_cost_status,
        p.is_paid,p.provider_request_id,c.state AS control_state FROM jobs j JOIN job_payloads p USING(job_id)
        LEFT JOIN job_controls c USING(job_id) WHERE j.job_id=?''',(job_id,)).fetchone()
    if row is None:_fail('VISUAL_EXECUTION_WORKER_INVALID')
    if row['cancel_requested'] or row['control_state'] not in (None,'active'):_fail('VISUAL_EXECUTION_CANCELLED')
    try:lease=datetime.fromisoformat(row['lease_expires_at'].replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError,AttributeError):lease=0
    if (row['status']!='running' or row['worker_id']!=worker_id or lease<=now
        or row['retry_policy']!='never' or row['job_type']!='visual_analysis'):
        _fail('VISUAL_EXECUTION_WORKER_INVALID')
    try:
        p=json.loads(row['payload_json'])
        valid=(_digest(p)==row['payload_digest'] and p.get('action')=='approved_visual_execution_v1'
            and p.get('operationId')==operation_id and p.get('approvalId')==approval_id
            and p.get('packageSha256')==package_digest and row['requested_cost_status']=='approved'
            and row['cost_status'] in ('approved','incurred') and row['is_paid']==1
            and row['provider_request_id']=='visual-approved:'+operation_id)
    except (ValueError,TypeError,AttributeError):valid=False
    if not valid:_fail('VISUAL_EXECUTION_JOB_INVALID')

def claim_visual_execution(db,*,approval_id,operation_id,job_id,worker_id,current_package,clock=None):
    if any(not _id(value) for value in (approval_id,operation_id,job_id,worker_id)):
        _fail('VISUAL_EXECUTION_INPUT_INVALID')
    package=deepcopy(current_package);package_digest=_digest(package);now=_now(clock)
    token=uuid.uuid4().hex
    with _write(db) as conn:
        if conn.execute('SELECT 1 FROM visual_execution_operations WHERE operation_id=?',(operation_id,)).fetchone():
            _fail('VISUAL_EXECUTION_ALREADY_CLAIMED')
        approval=_approval(conn,approval_id,package_digest,now)
        _job(conn,job_id,worker_id,operation_id,approval_id,package_digest,now)
        if approval['prior_used']+approval['reserved_requests']+2>approval['total_limit']:
            _fail('VISUAL_APPROVAL_QUOTA_EXHAUSTED')
        if (package.get('executionPolicy')!=POLICY or package.get('scopeKind')!='original_remaining_business'
            or len(package.get('requests',[]))!=2 or sum(len(r['frames']) for r in package['requests'])!=6):
            _fail('VISUAL_APPROVAL_INVALID')
        conn.execute("UPDATE visual_execution_approvals SET reserved_requests=reserved_requests+2,status='reserved' WHERE approval_id=?",(approval_id,))
        conn.execute("INSERT INTO visual_execution_operations VALUES(?,?,?,?,?,?,'claimed',2,NULL)",
            (operation_id,approval_id,job_id,worker_id,hashlib.sha256(token.encode()).hexdigest(),package_digest))
    return dict(operationId=operation_id,claimToken=token,reservedRequests=2,canRetry=False)

def execution_status(db,operation_id):
    row=db.connection.execute('SELECT * FROM visual_execution_operations WHERE operation_id=?',(operation_id,)).fetchone()
    if row is None:_fail('VISUAL_EXECUTION_NOT_FOUND')
    attempts=[dict(index=r['request_index'],requestSha256=r['request_sha256'],status=r['status'],errorCode=r['error_code'])
        for r in db.connection.execute('SELECT * FROM visual_execution_attempts WHERE operation_id=? ORDER BY request_index',(operation_id,))]
    return dict(operationId=operation_id,status=row['status'],packageSha256=row['package_sha256'],
        reservedRequests=row['reserved_requests'],dispatchIntents=len(attempts),attempts=attempts,
        errorCode=row['error_code'],canRetry=False)

def _check(conn,operation,package_digest,now):
    current=conn.execute('SELECT * FROM visual_execution_operations WHERE operation_id=?',(operation['operation_id'],)).fetchone()
    if (current is None or current['status']!='running' or any(current[k]!=operation[k]
            for k in ('approval_id','job_id','worker_id','claim_token_sha256','package_sha256'))):
        _fail('VISUAL_EXECUTION_CLOSED')
    _approval(conn,operation['approval_id'],package_digest,now)
    if package_digest!=operation['package_sha256']:_fail('VISUAL_EXECUTION_STALE')
    _job(conn,operation['job_id'],operation['worker_id'],operation['operation_id'],operation['approval_id'],package_digest,now)

def _stop(db,operation_id,status,code,index=None):
    with _write(db) as conn:
        changed=conn.execute("UPDATE visual_execution_operations SET status=?,error_code=? WHERE operation_id=? AND status='running'",(status,code,operation_id)).rowcount
        if changed and index is not None:
            conn.execute("UPDATE visual_execution_attempts SET status=?,error_code=? WHERE operation_id=? AND request_index=? AND status='dispatch_intent'",(status,code,operation_id,index))
    return execution_status(db,operation_id)

def execute_claimed_visual(db,operation_id,claim_token,*,worker_id,current_package,transport,validate_response,clock=None):
    """Only trusted server callbacks. Transport must send once, with no retries.

    current_package returns (config, requests) as fresh server-owned input. The
    whole batch is reserved before any dispatch; an intent persists before each
    transport, so process loss never permits replay. Intents are not billing proof.
    """
    if not all(callable(fn) for fn in (current_package,transport,validate_response)):_fail('VISUAL_EXECUTION_INPUT_INVALID')
    with _write(db) as conn:
        operation=conn.execute('SELECT * FROM visual_execution_operations WHERE operation_id=?',(operation_id,)).fetchone()
        if (operation is None or operation['worker_id']!=worker_id or not isinstance(claim_token,str)
            or hashlib.sha256(claim_token.encode()).hexdigest()!=operation['claim_token_sha256']):
            _fail('VISUAL_EXECUTION_CLAIM_INVALID')
        if operation['status']!='claimed':_fail('VISUAL_EXECUTION_ALREADY_STARTED')
        conn.execute("UPDATE visual_execution_operations SET status='running' WHERE operation_id=?",(operation_id,))
    for index in range(2):
        intent=False
        try:
            config,requests=current_package()
            config,requests=deepcopy(config),deepcopy(requests)
            package=freeze_visual_execution(config,requests)
            with _write(db) as conn:
                _check(conn,operation,_digest(package),_now(clock))
                conn.execute("INSERT INTO visual_execution_attempts VALUES(?,?,?,'dispatch_intent',NULL)",
                    (operation_id,index,package['requests'][index]['requestSha256']))
            intent=True
            # Recheck cooperative cancellation/config after intent commit, before I/O.
            config_now,requests_now=current_package()
            with _write(db) as conn:
                _check(conn,operation,_digest(freeze_visual_execution(config_now,requests_now)),_now(clock))
            try:response=transport(deepcopy(requests[index]),timeout_seconds=requests[index]['timeoutSeconds'])
            except Exception as error:
                code=getattr(error,'code',None)
                return _stop(db,operation_id,'failed' if code in _FAILURES else 'unknown',
                    code if code in _FAILURES else 'MODEL_PROBE_OUTCOME_UNKNOWN',index)
            try:valid=validate_response(response,deepcopy(requests[index])) is True
            except Exception:valid=False
            if not valid:return _stop(db,operation_id,'failed','VISUAL_EXECUTION_VALIDATION_FAILED',index)
            config_now,requests_now=current_package()
            with _write(db) as conn:
                _check(conn,operation,_digest(freeze_visual_execution(config_now,requests_now)),_now(clock))
                conn.execute("UPDATE visual_execution_attempts SET status='succeeded' WHERE operation_id=? AND request_index=?",(operation_id,index))
                if index==1:conn.execute("UPDATE visual_execution_operations SET status='succeeded' WHERE operation_id=? AND status='running'",(operation_id,))
        except ApprovedVisualError as error:
            state='cancelled' if error.code=='VISUAL_EXECUTION_CANCELLED' else 'stale'
            return _stop(db,operation_id,state,error.code,index if intent else None)
        except Exception:
            return _stop(db,operation_id,'unknown','MODEL_PROBE_OUTCOME_UNKNOWN',index if intent else None)
    return execution_status(db,operation_id)


def recover_visual_execution(db,operation_id,*,clock=None):
    """Expired/lost existing worker only: mark unknown, never release or replay."""
    with _write(db) as conn:
        operation=conn.execute('SELECT * FROM visual_execution_operations WHERE operation_id=?',(operation_id,)).fetchone()
        if operation is None:_fail('VISUAL_EXECUTION_NOT_FOUND')
        if operation['status']=='unknown':return execution_status(db,operation_id)
        if operation['status'] not in ('claimed','running'):_fail('VISUAL_EXECUTION_CLOSED')
        job=conn.execute('SELECT status,lease_expires_at FROM jobs WHERE job_id=?',(operation['job_id'],)).fetchone()
        try:lease=datetime.fromisoformat(job['lease_expires_at'].replace('Z','+00:00')).timestamp()
        except (TypeError,ValueError,AttributeError):lease=0
        if job is None or (job['status']!='interrupted' and lease>_now(clock)):
            _fail('VISUAL_EXECUTION_WORKER_STILL_ACTIVE')
        conn.execute("UPDATE visual_execution_operations SET status='unknown',error_code='MODEL_PROBE_OUTCOME_UNKNOWN' WHERE operation_id=?",(operation_id,))
        conn.execute("UPDATE visual_execution_attempts SET status='unknown',error_code='MODEL_PROBE_OUTCOME_UNKNOWN' WHERE operation_id=? AND status='dispatch_intent'",(operation_id,))
    return execution_status(db,operation_id)
