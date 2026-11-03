"""Explicit one-window audio-only review. No automatic retry or movie approval."""
from pathlib import Path
import base64
import hashlib
import io
import json
import math
import os
import re
import wave
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from .packaging_audio_evidence import _run_bounded
from .tools import find_ffmpeg, default_ffprobe_search_roots
from .audio_review_protocol import AUDIO_REVIEW_SCHEMA, MAX_RESPONSE_BYTES, AudioReviewProtocolError, parse_audio_response
from .content_review import _current_candidate
from .candidate_versions import resolve_candidate_media
from .content_review_jobs import _inputs
from .credentials import read_provider_secret
from .model_profiles import get_provider_account
from .model_runtime import provider_access_token
from .workbench_worker import enqueue_job, ProviderOutcomeUnknown, ProviderOutcomeConfirmedFailure, ProviderRequestNotSent, JobInterrupted

MODEL = 'glm-4-voice'
BASE_URL = 'https://open.bigmodel.cn/api/paas/v4'
PROMPT_VERSION = 'audio-review-natural-v1'
MAX_TOKENS = 1024
PROMPT = '请实际听这段音频，简短回答：是否有人声；是否有明显停顿，若有给出大约起止时间；一句话描述说话速度和句间衔接。听不清或不能判断就说未知。不要推断说话者身份或心理状态，不要开场白，不必逐字转写。'
_TERMINAL = {'structured','unstructured','ambiguous'}


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _base_inputs(db, root, task_id, candidate_id):
    inputs,_ = _inputs(db,Path(root),task_id,candidate_id)
    candidate = _current_candidate(db,task_id,candidate_id)
    duration = float((candidate.get('qa_report') or {}).get('video_spec',{}).get('duration',0))
    if not math.isfinite(duration) or duration<=0:
        raise RuntimeError('AUDIO_REVIEW_DURATION_REQUIRED: 缺少有效成片时长')
    return {**inputs,'duration_seconds':duration}


def _account_snapshot(db, account_id):
    account=get_provider_account(db,account_id,include_credential=True)
    if not account or account['baseUrl'].rstrip('/')!=BASE_URL or not account.get('credentialRef'):
        raise ValueError('AUDIO_REVIEW_ACCOUNT_INVALID: 仅支持已配置的智谱官方账号')
    identity={k:account[k] for k in ('accountId','provider','baseUrl','credentialRef','settings')}
    return {'accountId':account_id,'displayName':account['name'],'model':MODEL,
            'baseUrl':BASE_URL,'digest':_digest(identity)}


def _window(inputs,start,end):
    duration=inputs['duration_seconds']
    if end is None:
        end=min(duration,start+30) if type(start) in (int,float) else None
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in (start,end)) or not 0<=start<end<=duration or end-start>30:
        raise ValueError('AUDIO_REVIEW_WINDOW_INVALID: 请指定成片内不超过30秒的有效区间')
    return float(start),float(end)


def _binding(inputs,start,end):
    return {**inputs,'start_seconds':start,'end_seconds':end,'prompt_version':PROMPT_VERSION,
            'prompt_sha256':_digest([PROMPT,AUDIO_REVIEW_SCHEMA]),'model':MODEL,
            'request_parameters':{'max_tokens':MAX_TOKENS,'stream':False}}


def _public(row, *, stale=False):
    result={'runId':row['run_id'],'jobId':row['job_id'],'candidateId':row['candidate_id'],
            'inputsDigest':row['inputs_digest'],'status':row['status'],
            'coverage':{'startSeconds':row['start_seconds'],'endSeconds':row['end_seconds']},
            'stale':bool(stale or row['stale_at_completion'])}
    if row['parsed_json']:
        parsed=json.loads(row['parsed_json'])
        result['parsed']={k:v for k,v in parsed.items() if k in ('protocolVersion','status','review','generatedAudioIsSourceEvidence')}
        result['parsed']['choices']=[{k:v for k,v in choice.items() if k in ('index','finishReason','status','textSources','review')} for choice in parsed.get('choices',[])]
    if row['error_reason']:
        result['error']=row['error_reason']
    return result


def prepare_audio_review(db,project_root,task_id,candidate_id,*,start_seconds=0,end_seconds=None):
    inputs=_base_inputs(db,project_root,task_id,candidate_id)
    start,end=_window(inputs,start_seconds,end_seconds)
    binding=_binding(inputs,start,end);digest=_digest(binding)
    accounts=[]
    for row in db.connection.execute('SELECT account_id FROM model_provider_accounts ORDER BY account_id'):
        try:
            account=_account_snapshot(db,row['account_id'])
        except ValueError:
            continue
        accounts.append({k:v for k,v in account.items() if k!='digest'})
    latest=None
    job=db.connection.execute("""SELECT j.*,p.payload_json,p.outcome_status,p.provider_state FROM jobs j JOIN job_payloads p USING(job_id)
        WHERE j.task_id=? AND j.job_type='audio_review' AND json_extract(p.payload_json,'$.candidate_id')=?
        ORDER BY j.rowid DESC LIMIT 1""",(task_id,candidate_id)).fetchone()
    if job:
        payload=json.loads(job['payload_json'])
        row=db.connection.execute('SELECT * FROM audio_review_runs WHERE job_id=?',(job['job_id'],)).fetchone()
        stale=payload.get('inputs_digest')!=digest
        try:
            stale=stale or _account_snapshot(db,payload['account_id'])['digest']!=payload['account_digest']
        except (ValueError,KeyError):
            stale=True
        if row:
            latest=_public(row,stale=stale)
        else:
            latest={'runId':job['job_id'],'jobId':job['job_id'],'status':job['status'],
                    'inputsDigest':payload['inputs_digest'],'stale':stale,
                    'coverage':{'startSeconds':payload['start_seconds'],'endSeconds':payload['end_seconds']}}
    if latest is not None:
        latest['outcomeStatus']=job['outcome_status'];latest['providerState']=job['provider_state']
        latest['usable']=not latest['stale'] and latest['status'] in _TERMINAL and job['outcome_status']!='unknown'
    return {'candidateId':candidate_id,'snapshotId':inputs['qa_snapshot_id'],'qaReportId':inputs['qa_report_id'],
            'durationSeconds':inputs['duration_seconds'],'startSeconds':start,'endSeconds':end,
            'inputsDigest':digest,'accounts':accounts,'latest':latest,
            'limitations':['仅发送所选成片音频及声音检查指令，不发送文案或画面；实际效果待验证。',
                           '最多30秒是本次受控单窗边界；未覆盖区间、完整人审和批准仍须另行完成。']}


def _approved(connection,task_id,payload):
    if connection.execute("SELECT 1 FROM review_approvals WHERE task_id=? AND snapshot_id=? AND qa_report_id=? AND decision='approved'",
                          (task_id,payload['qa_snapshot_id'],payload['qa_report_id'])).fetchone():
        raise RuntimeError('AUDIO_REVIEW_ALREADY_APPROVED: 已批准影片不能提交新检查')


def queue_audio_review(db,project_root,task_id,candidate_id,*,account_id,request_id,inputs_digest,
                       confirm_paid_call,start_seconds=0,end_seconds=None):
    if confirm_paid_call is not True:
        raise ValueError('PAID_CONFIRMATION_REQUIRED: 请确认发送本段音频并可能产生费用')
    if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',request_id):
        raise ValueError('AUDIO_REVIEW_REQUEST_INVALID: 无效请求编号')
    inputs=_base_inputs(db,project_root,task_id,candidate_id)
    start,end=_window(inputs,start_seconds,end_seconds);binding=_binding(inputs,start,end)
    if _digest(binding)!=inputs_digest:
        raise RuntimeError('AUDIO_REVIEW_INPUT_CHANGED: 当前成片或区间已变化，请刷新')
    account=_account_snapshot(db,account_id)
    provider_id=f'audio-review:{task_id}:{request_id}';job_id='audio-review-job-'+_digest(provider_id)[:32]
    payload={**binding,'task_id':task_id,'job_id':job_id,'request_id':request_id,'inputs_digest':inputs_digest,
             'account_id':account_id,'account_digest':account['digest']}
    def check(connection):
        if _binding(_base_inputs(db,project_root,task_id,candidate_id),start,end)!=binding or _account_snapshot(db,account_id)['digest']!=account['digest']:
            raise RuntimeError('AUDIO_REVIEW_INPUT_CHANGED: 排队时输入变化')
        _approved(connection,task_id,payload)
        rows=connection.execute("""SELECT j.status,p.outcome_status,r.status AS review_status FROM jobs j
            JOIN job_payloads p USING(job_id) LEFT JOIN audio_review_runs r USING(job_id)
            WHERE j.task_id=? AND j.job_type='audio_review' AND json_extract(p.payload_json,'$.candidate_id')=?""",(task_id,candidate_id)).fetchall()
        if any(r['outcome_status']=='unknown' or r['review_status'] in ('dispatched','unknown','cancelled') for r in rows):
            raise RuntimeError('AUDIO_REVIEW_OUTCOME_UNKNOWN: 原请求结果未知或已取消待核对，不允许重新付费提交')
        if any(r['status'] in ('queued','running') for r in rows):
            raise RuntimeError('AUDIO_REVIEW_BUSY: 当前成片已有排队或运行的声音检查')
    enqueue_job(db,job_id=job_id,job_type='audio_review',task_id=task_id,payload=payload,
                provider_request_id=provider_id,cost_status='approved',retry_policy='never',pre_enqueue_check=check)
    return {'jobId':job_id,'requestId':request_id}


def _audio_bytes(db,root,task_id,payload):
    path=resolve_candidate_media(db,task_id,payload['candidate_id'])
    duration=payload['end_seconds']-payload['start_seconds']
    ffmpeg=find_ffmpeg(os.environ,search_roots=default_ffprobe_search_roots())
    pcm=_run_bounded([str(ffmpeg),'-nostdin','-v','error','-xerror','-ss',str(payload['start_seconds']),
        '-i',str(path),'-map','0:a:0','-t',str(duration),'-vn','-sn','-dn','-ac','1','-ar','16000',
        '-c:a','pcm_s16le','-f','s16le','pipe:1'],timeout=90,max_bytes=960002)
    actual=len(pcm)/32000
    if len(pcm)%2 or not 0<actual<=30 or abs(actual-duration)>.1:
        raise ValueError('AUDIO_REVIEW_SAMPLE_INVALID: 音频时长与选定区间不一致')
    stream=io.BytesIO()
    with wave.open(stream,'wb') as sample:
        sample.setnchannels(1);sample.setsampwidth(2);sample.setframerate(16000);sample.writeframes(pcm)
    return stream.getvalue()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


class AudioRequestNotSent(ProviderRequestNotSent, ValueError):
    pass


def request_audio(db,account_id,audio_bytes,*,expected_account_digest,review_prompt=None,binding_guard=None):
    """One official-endpoint request; caller binds any contextual prompt and approval."""
    prompt=PROMPT if review_prompt is None else review_prompt
    if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>24000:
        raise AudioRequestNotSent('AUDIO_REVIEW_PROMPT_INVALID: 听审上下文超出范围')
    account=get_provider_account(db,account_id,include_credential=True)
    if not account or account['baseUrl'].rstrip('/')!=BASE_URL or not account.get('credentialRef'):
        raise AudioRequestNotSent('AUDIO_REVIEW_ACCOUNT_INVALID: 账号不可用，请刷新')
    identity={k:account[k] for k in ('accountId','provider','baseUrl','credentialRef','settings')}
    if _digest(identity)!=expected_account_digest:
        raise AudioRequestNotSent('AUDIO_REVIEW_INPUT_CHANGED: 发送前账号配置已变化')
    token=provider_access_token(read_provider_secret(account['credentialRef']) or '',account['provider'],account.get('channelId'))
    if not token:
        raise AudioRequestNotSent('AUDIO_REVIEW_CREDENTIAL_MISSING: 账号凭据不可用')
    payload={'model':MODEL,'stream':False,'max_tokens':MAX_TOKENS,'messages':[{'role':'user','content':[
        {'type':'text','text':prompt},
        {'type':'input_audio','input_audio':{'data':base64.b64encode(audio_bytes).decode('ascii'),'format':'wav'}}]}]}
    request=Request(BASE_URL+'/chat/completions',data=_json(payload).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:
        if binding_guard is not None:
            binding_guard()
    except Exception as error:
        raise AudioRequestNotSent(str(error)) from error
    try:
        with build_opener(ProxyHandler({}),_NoRedirect()).open(request,timeout=120) as response:
            raw=response.read(MAX_RESPONSE_BYTES+1)
            if len(raw)>MAX_RESPONSE_BYTES:
                raise ProviderOutcomeConfirmedFailure('AUDIO_RESPONSE_TOO_LARGE: 响应超出本地保存上限')
            return raw
    except HTTPError as error:
        if error.code==408 or error.code>=500:
            raise ProviderOutcomeUnknown('AUDIO_REVIEW_HTTP_UNKNOWN: HTTP '+str(error.code)) from error
        raise ProviderOutcomeConfirmedFailure('AUDIO_REVIEW_HTTP_REJECTED: HTTP '+str(error.code),provider_state='http_rejected',cost_status='unknown') from error
    except (TimeoutError,URLError,ConnectionError,OSError) as error:
        raise ProviderOutcomeUnknown('AUDIO_REVIEW_NETWORK_UNKNOWN: 请求结果尚未确认') from error


def saved_audio_review_result(connection,task_id,payload):
    state=connection.execute('SELECT cancel_requested,status FROM jobs WHERE job_id=? AND task_id=?',(payload.get('job_id'),task_id)).fetchone()
    if state is None or state['cancel_requested'] or state['status']=='cancelled':
        return None
    row=connection.execute('SELECT * FROM audio_review_runs WHERE job_id=? AND task_id=?',(payload.get('job_id'),task_id)).fetchone()
    if (row is None or row['status'] not in _TERMINAL or row['stale_at_completion'] or not row['parsed_json']
            or payload.get('task_id')!=task_id or row['candidate_id']!=payload.get('candidate_id')
            or row['inputs_digest']!=payload.get('inputs_digest') or row['account_digest']!=payload.get('account_digest')):
        return None
    return {'cursor':'audio_review:'+row['run_id'],'progress':{'current':1,'total':1},
            'phase_label':'声音检查返回文字待复核','result':_public(row)}


def _current_binding(db,root,task_id,payload):
    current=_binding(_base_inputs(db,root,task_id,payload['candidate_id']),payload['start_seconds'],payload['end_seconds'])
    if _digest(current)!=payload['inputs_digest'] or _account_snapshot(db,payload['account_id'])['digest']!=payload['account_digest']:
        raise RuntimeError('AUDIO_REVIEW_INPUT_CHANGED: 成片、质检、文案或账号配置已变化')
    return current


def _finish_saved_response(db,job,root,run_id,raw):
    try:
        parsed=parse_audio_response(raw)
    except AudioReviewProtocolError as error:
        raise ProviderOutcomeConfirmedFailure(str(error),diagnostics=error.diagnostics) from error
    try:
        cancelled=False
        with db.transaction(immediate=True) as connection:
            state=connection.execute('SELECT status,cancel_requested FROM jobs WHERE job_id=?',(job.job_id,)).fetchone()
            cancelled=bool(state['cancel_requested'] or state['status']=='cancelled')
            _current_binding(db,root,job.task_id,job.payload)
            connection.execute("UPDATE audio_review_runs SET status=?,parsed_json=?,error_reason=NULL,updated_at=datetime('now') WHERE run_id=?",
                               ('cancelled' if cancelled else parsed['status'],_json(parsed),run_id))
            if cancelled:
                connection.execute('UPDATE audio_review_runs SET stale_at_completion=1 WHERE run_id=?',(run_id,))
        if cancelled:
            raise JobInterrupted('声音检查在返回前已取消；回执仅作历史保留')
    except JobInterrupted:
        raise
    except (RuntimeError,ValueError,KeyError) as error:
        with db.transaction(immediate=True) as connection:
            connection.execute("UPDATE audio_review_runs SET stale_at_completion=1,parsed_json=?,status='failed',error_reason='AUDIO_REVIEW_INPUT_CHANGED' WHERE run_id=?",(_json(parsed),run_id))
        raise ProviderOutcomeConfirmedFailure('AUDIO_REVIEW_INPUT_CHANGED: 返回结果已成为历史，不能套用当前成片') from error
    return saved_audio_review_result(db.connection,job.task_id,job.payload)


def _private_folder(root,task_id,run_id):
    base=(Path(root)/'runtime/workbench/tasks').resolve()
    folder=(base/task_id/'artifacts/audio-review'/run_id).resolve()
    if not folder.is_relative_to(base) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',task_id):
        raise ValueError('AUDIO_REVIEW_PATH_INVALID')
    return folder


def recover_saved_audio_review(db,project_root,task_id,job_id):
    """Explicit LOCAL recovery of a complete persisted response; never dispatch."""
    from types import SimpleNamespace
    row=db.connection.execute("SELECT p.payload_json FROM jobs j JOIN job_payloads p USING(job_id) WHERE j.job_id=? AND j.task_id=? AND j.job_type='audio_review'",(job_id,task_id)).fetchone()
    receipt=db.connection.execute('SELECT response_path,response_sha256 FROM audio_review_runs WHERE job_id=? AND task_id=?',(job_id,task_id)).fetchone()
    if row is None or receipt is None or not receipt['response_path'] or not receipt['response_sha256']:
        raise RuntimeError('AUDIO_REVIEW_RECEIPT_REQUIRED: 没有完整已保存响应，禁止重发')
    return run_audio_review(db,SimpleNamespace(job_id=job_id,task_id=task_id,payload=json.loads(row['payload_json'])),project_root)


def run_audio_review(db,job,project_root):
    payload,task_id=job.payload,job.task_id
    if not task_id or payload.get('task_id')!=task_id or payload.get('job_id')!=job.job_id:
        raise ValueError('AUDIO_REVIEW_TASK_MISMATCH: 作业归属不匹配')
    stored=db.connection.execute('SELECT j.task_id,j.job_type,p.payload_json FROM jobs j JOIN job_payloads p USING(job_id) WHERE job_id=?',(job.job_id,)).fetchone()
    if stored is None or stored['task_id']!=task_id or stored['job_type']!='audio_review' or json.loads(stored['payload_json'])!=payload:
        raise ValueError('AUDIO_REVIEW_PAYLOAD_MISMATCH: 执行输入不等于持久请求')
    saved=saved_audio_review_result(db.connection,task_id,payload)
    if saved is not None:
        return saved
    row=db.connection.execute('SELECT * FROM audio_review_runs WHERE job_id=?',(job.job_id,)).fetchone()
    if row:
        if row['status'] in ('dispatched','unknown') and row['response_path'] and row['response_sha256']:
            path=_private_folder(project_root,task_id,row['run_id'])/'response.json'
            if path.resolve()!=Path(row['response_path']).resolve() or path.stat().st_size>MAX_RESPONSE_BYTES:
                raise RuntimeError('AUDIO_REVIEW_RECEIPT_CHANGED: 回执路径或大小不匹配')
            raw=path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=row['response_sha256']:
                raise RuntimeError('AUDIO_REVIEW_RECEIPT_CHANGED: 已保存原件变化')
            try:
                return _finish_saved_response(db,job,project_root,row['run_id'],raw)
            except ProviderOutcomeConfirmedFailure as error:
                with db.transaction(immediate=True) as connection:
                    connection.execute("UPDATE audio_review_runs SET status='failed',error_reason=?,updated_at=datetime('now') WHERE run_id=?",(str(error)[:300],row['run_id']))
                raise
        raise RuntimeError('AUDIO_REVIEW_ALREADY_DISPATCHED: 原请求已有执行记录，禁止重发')
    binding=_current_binding(db,project_root,task_id,payload)
    run_id='audio-review-'+_digest(job.job_id)[:32]
    # Persist local preparation before the expensive extraction, without declaring dispatch.
    with db.transaction(immediate=True) as connection:
        state=connection.execute('SELECT * FROM jobs WHERE job_id=?',(job.job_id,)).fetchone()
        if not state or state['task_id']!=task_id or state['job_type']!='audio_review':
            raise ValueError('AUDIO_REVIEW_TASK_MISMATCH: 作业不存在或类型不匹配')
        if state['cancel_requested'] or state['status'] not in ('queued','running'):
            raise JobInterrupted('声音检查已取消或中断')
        _approved(connection,task_id,payload)
        connection.execute('''INSERT INTO audio_review_runs(run_id,job_id,task_id,candidate_id,inputs_digest,binding_json,
          account_digest,start_seconds,end_seconds,status) VALUES(?,?,?,?,?,?,?,?,?,'preparing')''',
          (run_id,job.job_id,task_id,payload['candidate_id'],payload['inputs_digest'],_json(binding),payload['account_digest'],payload['start_seconds'],payload['end_seconds']))
        connection.execute('UPDATE jobs SET cursor=? WHERE job_id=?',('audio_review:'+run_id,job.job_id))
    dispatched=False
    try:
        folder=_private_folder(project_root,task_id,run_id)
        folder.mkdir(parents=True,exist_ok=True)
        with (folder/'request.json').open('x',encoding='utf8') as manifest:
            manifest.write(_json({'jobId':job.job_id,'inputsDigest':payload['inputs_digest'],'accountDigest':payload['account_digest']}));manifest.flush();os.fsync(manifest.fileno())
        audio=_audio_bytes(db,project_root,task_id,payload)
        def permission_guard():
            from .model_media_access import require_candidate_access
            require_candidate_access(db,task_id,payload['candidate_id'])
            _current_binding(db,project_root,task_id,payload)
        permission_guard()
        with db.transaction(immediate=True) as connection:
            state=connection.execute('SELECT status,cancel_requested FROM jobs WHERE job_id=?',(job.job_id,)).fetchone()
            if state['cancel_requested'] or state['status'] not in ('queued','running'):
                raise JobInterrupted('声音检查发送前已取消或中断')
            _approved(connection,task_id,payload)
            _current_binding(db,project_root,task_id,payload)
            connection.execute("UPDATE audio_review_runs SET status='dispatched',audio_sha256=? WHERE run_id=?",(hashlib.sha256(audio).hexdigest(),run_id))
            connection.execute("UPDATE job_payloads SET provider_state='audio_review_dispatched',updated_at=datetime('now') WHERE job_id=?",(job.job_id,))
        dispatched=True
        raw=request_audio(db,payload['account_id'],audio,expected_account_digest=payload['account_digest'],binding_guard=permission_guard)
        if not isinstance(raw,bytes) or len(raw)>MAX_RESPONSE_BYTES:
            raise ProviderOutcomeConfirmedFailure('AUDIO_RESPONSE_INVALID: 响应原件类型或大小无效')
        path=folder/'response.json'
        with path.open('xb') as output:
            output.write(raw);output.flush();os.fsync(output.fileno())
        with db.transaction(immediate=True) as connection:
            connection.execute('UPDATE audio_review_runs SET response_path=?,response_sha256=?,updated_at=datetime(\'now\') WHERE run_id=?',
                               (str(path),hashlib.sha256(raw).hexdigest(),run_id))
        return _finish_saved_response(db,job,project_root,run_id,raw)
    except BaseException as error:
        # Local persistence failure after dispatch cannot establish a safely replayable receipt.
        cancelled=isinstance(error,JobInterrupted)
        unknown=dispatched and not isinstance(error,(ProviderOutcomeConfirmedFailure,AudioRequestNotSent)) and not cancelled
        # Before durable dispatch, interruption is a local failure, not a paid-cancellation lock.
        status='cancelled' if cancelled and dispatched else 'unknown' if unknown else 'failed'
        reason=('AUDIO_REVIEW_OUTCOME_UNKNOWN: 原请求不可自动重发' if unknown else
                str(error)[:300] if isinstance(error,(ProviderOutcomeConfirmedFailure,RuntimeError,ValueError,JobInterrupted)) else 'AUDIO_REVIEW_LOCAL_FAILURE')
        with db.transaction(immediate=True) as connection:
            connection.execute('UPDATE audio_review_runs SET status=?,error_reason=?,updated_at=datetime(\'now\') WHERE run_id=?',(status,reason,run_id))
        if unknown and not isinstance(error,ProviderOutcomeUnknown):
            raise ProviderOutcomeUnknown('AUDIO_REVIEW_OUTCOME_UNKNOWN: 发送后的本地处理未完成，请核对原回执') from error
        raise