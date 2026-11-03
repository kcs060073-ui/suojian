"""Single-call adapter for server-frozen visual payloads, never an approval source.

Uses urllib and existing v3/schema/evidence rules. 45s socket timeout plus a
cooperative monotonic 45s deadline is NOT a hard wall limit: DNS, one blocked
read, validation, or an injected opener may finish after that deadline.
"""
import base64
from copy import deepcopy
import hashlib
import json
import math
import re
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from .visual_batch_execution import _validate_result
from .visual_request_policy import visual_request_options
from .visual_protocol import safe_visual_diagnostics
from .visual_protocol import (_loads, protocol_contract, parse_visual_response,
                              response_diagnostics, VisualProtocolError)
from .visual_schema import observation_schema, response_format, validate_visual_schema, _Validator

SOCKET_TIMEOUT_SECONDS = 45.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
_CONTEXT = {'endpoint','provider','model','protocol','payloadSha256','frames','sourceStartUs','sourceEndUs'}
_CURRENT = {'endpoint','provider','model','protocol'}
_SHA = re.compile(r'[0-9a-f]{64}')
_CODES = {'MODEL_AUTH_FAILED','MODEL_PROVIDER_HTTP_ERROR','MODEL_VISUAL_MISMATCH',
          'MODEL_VISUAL_PROTOCOL_INVALID','MODEL_VISUAL_SCHEMA_INVALID',
          'MODEL_VISUAL_RESPONSE_REFUSED','MODEL_VISUAL_RESPONSE_INCOMPLETE',
          'MODEL_VISUAL_FINAL_RESULT_MISSING','MODEL_PROBE_OUTCOME_UNKNOWN'}


def _safe_diagnostics(value):
    if not isinstance(value,dict):return {}
    strings={
        'protocolVersion':{'visual-chat-final-json-v3'},
        'protocolMode':{'server_json_schema','json_object_local_schema'},
        'schemaValidation':{'server','local'},'schemaVersion':{'visual-result-schema-v1'},
        'envelopeKind':{'invalid','object'},'contentKind':{'text','parts','missing','other'},
        'finishReason':{'stop','length','content_filter','tool_calls','function_call','missing','other'},
        'schemaFailureKind':{'required','additional_properties','type','enum','other'},
    }
    facts={k:v for k,v in value.items() if k in strings and isinstance(v,str) and v in strings[k]}
    for key in ('deadlineExceeded','responseTooLarge','hasReasoning','hasRefusal','hasToolCalls','finalTextPresent'):
        if type(value.get(key)) is bool:facts[key]=value[key]
    for key,low,high in (('httpStatus',100,599),('responseBytes',0,MAX_RESPONSE_BYTES+1),('choiceCount',0,1000000)):
        if type(value.get(key)) is int and low<=value[key]<=high:facts[key]=value[key]
    facts.update(safe_visual_diagnostics(value))
    return facts


class ApprovedVisualTransportError(RuntimeError):
    def __init__(self, code, diagnostics=None):
        self.code = code if isinstance(code,str) and code in _CODES else 'MODEL_PROBE_OUTCOME_UNKNOWN'
        self.diagnostics = _safe_diagnostics(diagnostics)
        super().__init__(self.code)


def _fail(code='MODEL_VISUAL_MISMATCH', diagnostics=None):
    raise ApprovedVisualTransportError(code, diagnostics) from None


def _canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _endpoint(value, allow_loopback):
    if not isinstance(value,str) or len(value)>4096 or any(ord(c)<=32 or ord(c)>126 for c in value):_fail()
    try:
        url=urlsplit(value)
        valid=(url.hostname and not url.username and not url.password and not url.query and not url.fragment
               and url.path.endswith('/chat/completions') and (url.port is None or 0<url.port<65536))
    except (ValueError,TypeError):_fail()
    if not valid:_fail()
    if url.scheme!='https' and not (allow_loopback is True and url.scheme=='http' and url.hostname in ('127.0.0.1','::1')):_fail()


def _prepare(context, payload, *, allow_loopback=False):
    """Validate exact identity and bytes without changing any outbound field."""
    context=deepcopy(context)
    if not isinstance(context,dict) or set(context)!=_CONTEXT:_fail()
    _endpoint(context['endpoint'],allow_loopback)
    if (context['provider']!='openai_compatible' or not isinstance(context['model'],str)
            or not context['model'] or len(context['model'])>256 or not isinstance(context['protocol'],dict)):_fail()
    try:
        mode=context['protocol'].get('protocolMode')
        if context['protocol']!=protocol_contract(mode):_fail()
    except (ValueError,TypeError):_fail()
    if (not isinstance(payload,bytes) or not 0<len(payload)<=MAX_REQUEST_BYTES
            or not isinstance(context['payloadSha256'],str) or not _SHA.fullmatch(context['payloadSha256'])
            or hashlib.sha256(payload).hexdigest()!=context['payloadSha256']):_fail()
    try:
        body=_loads(payload)
        if _canonical(body)!=payload:_fail()
        options=visual_request_options({'provider':context['provider'],'model':context['model'],'base_url':context['endpoint']})
        if (set(body)!={'model','messages','max_tokens','response_format'} | set(options) or any(body.get(k)!=v for k,v in options.items()) or body['model']!=context['model']
                or type(body['max_tokens']) is not int or body['max_tokens']!=350
                or body['response_format']!=response_format(mode,'cutroom_visual_observation',observation_schema())):_fail()
        messages=body['messages']
        if (not isinstance(messages,list) or len(messages)!=1 or set(messages[0])!={'role','content'}
                or messages[0]['role']!='user'):_fail()
        parts=messages[0]['content'];refs=context['frames']
        start,end=context['sourceStartUs'],context['sourceEndUs']
        if (type(start) is not int or type(end) is not int or not 0<=start<end
                or not isinstance(refs,list) or len(refs)!=3 or not isinstance(parts,list) or len(parts)!=7
                or set(parts[0])!={'type','text'} or parts[0]['type']!='text'
                or not isinstance(parts[0]['text'],str) or not parts[0]['text'].strip()):_fail()
        timestamps=[]
        for index,ref in enumerate(refs):
            if (not isinstance(ref,dict) or set(ref)!={'atUs','sha256'} or type(ref['atUs']) is not int
                    or not start<=ref['atUs']<=end or not isinstance(ref['sha256'],str) or not _SHA.fullmatch(ref['sha256'])):_fail()
            text,image=parts[1+2*index:3+2*index]
            if text!={'type':'text','text':f"源时码 {ref['atUs']} 微秒"}:_fail()
            if (set(image)!={'type','image_url'} or image['type']!='image_url'
                    or set(image['image_url'])!={'url'}):_fail()
            url=image['image_url']['url']
            if not isinstance(url,str) or re.fullmatch(r'data:image/(?:jpeg|png|webp);base64,[A-Za-z0-9+/=]+',url) is None:_fail()
            raw=base64.b64decode(url.split(',',1)[1],validate=True)
            if not raw or hashlib.sha256(raw).hexdigest()!=ref['sha256']:_fail()
            timestamps.append(ref['atUs'])
        if timestamps!=sorted(set(timestamps)):_fail()
        sample=SimpleNamespace(media_kind='video',frames=[SimpleNamespace(timestamp_us=t) for t in timestamps])
        return context,mode,sample
    except ApprovedVisualTransportError:raise
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError,OverflowError):_fail()


def _failure_response_diagnostics(raw,mode):
    """First-choice final TEXT presence, not valid JSON, completion or truth.

    Invalid strict envelopes yield False: text cannot be established from their
    shape. No reasoning fallback, content copies, provider IDs or exception text.
    """
    facts={**response_diagnostics(raw,protocol_mode=mode),
           'responseBytes':len(raw),'finalTextPresent':False}
    try:
        envelope=_loads(raw)
        if not isinstance(envelope,dict):return _safe_diagnostics(facts)
        choices=envelope.get('choices')
        if not isinstance(choices,list) or not choices or not isinstance(choices[0],dict):
            return _safe_diagnostics(facts)
        message=choices[0].get('message')
        if not isinstance(message,dict):return _safe_diagnostics(facts)
        content=message.get('content')
        if isinstance(content,str):facts['finalTextPresent']=bool(content.strip())
        elif isinstance(content,list) and all(isinstance(part,dict) and part.get('type')=='text'
                and isinstance(part.get('text'),str) for part in content):
            facts['finalTextPresent']=any(part['text'].strip() for part in content)
    except (ValueError,TypeError,RecursionError):pass
    return _safe_diagnostics(facts)


def _schema_failure_kind(answer):
    """Reuse the existing schema engine; retain only a finite rule category.

    Never copy error message, instance, schema path or provider field names.
    When several rules fail, report the first engine rule, not all failures.
    """
    try:
        error=next(_Validator(observation_schema()).iter_errors(answer),None)
        keyword=error.validator if error is not None else None
        return {'required':'required','additionalProperties':'additional_properties',
                'type':'type','enum':'enum'}.get(keyword,'other')
    except (ValueError,TypeError,RecursionError,OverflowError):return 'other'


def _validated(answer,mode,sample,diagnostics=None):
    try:
        # Full schema applies in BOTH modes, followed by original sample rules.
        validate_visual_schema(answer,observation_schema(),mode,diagnostics=diagnostics)
        return _validate_result(sample,answer,protocol_mode=mode)
    except VisualProtocolError as error:
        facts={**(diagnostics or {}),**error.diagnostics}
        if error.code=='VISUAL_SCHEMA_INVALID':facts['schemaFailureKind']=_schema_failure_kind(answer)
        _fail('MODEL_'+error.code,facts)
    except (ValueError,TypeError,KeyError,AttributeError):_fail(diagnostics=diagnostics)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        return None


def _open_once(request, *, timeout):
    # No environment proxy replacement, redirect, auth challenge handler or retry.
    return build_opener(ProxyHandler({}),_NoRedirect()).open(request,timeout=timeout)


def _time(clock):
    try:value=clock()
    except Exception:
        raise ApprovedVisualTransportError('MODEL_PROBE_OUTCOME_UNKNOWN') from None
    if type(value) not in (int,float) or not math.isfinite(value) or value<0:
        _fail('MODEL_PROBE_OUTCOME_UNKNOWN')
    return value


def _deadline(clock,end):
    if _time(clock)>=end:_fail('MODEL_PROBE_OUTCOME_UNKNOWN',{'deadlineExceeded':True})


def _http_error(status):
    safe={'httpStatus':status} if type(status) is int and 100<=status<=599 else {}
    _fail('MODEL_AUTH_FAILED' if status==401 else 'MODEL_PROVIDER_HTTP_ERROR',safe)


def _bounded_read(response,clock,end):
    chunks=[];count=0
    reader=getattr(response,'read1',None) or response.read
    while True:
        _deadline(clock,end)
        piece=reader(min(65536,MAX_RESPONSE_BYTES+1-count))
        _deadline(clock,end)
        if not isinstance(piece,bytes):_fail('MODEL_VISUAL_PROTOCOL_INVALID')
        if not piece:return b''.join(chunks)
        count+=len(piece)
        if count>MAX_RESPONSE_BYTES:_fail('MODEL_VISUAL_PROTOCOL_INVALID',{'responseTooLarge':True})
        chunks.append(piece)


def send_approved_visual(context,current_context,payload,*,token,timeout_seconds=SOCKET_TIMEOUT_SECONDS,
                         open_once=None,monotonic=None,allow_loopback=False):
    """One POST of the exact bytes. No state, approval, credentials I/O or retries.

    Injected open_once must be a trusted one-call transport honoring timeout;
    arbitrary callbacks cannot be forcibly stopped here. The deadline checks
    before/after I/O and validation, and rejects late success without replay.
    """
    opener=_open_once if open_once is None else open_once
    if not callable(opener):_fail()
    clock=time.monotonic if monotonic is None else monotonic
    if not callable(clock):_fail()
    end=_time(clock)+SOCKET_TIMEOUT_SECONDS
    context,mode,sample=_prepare(context,payload,allow_loopback=allow_loopback)
    if (not isinstance(current_context,dict) or set(current_context)!=_CURRENT
            or current_context!={k:context[k] for k in _CURRENT}
            or type(timeout_seconds) not in (int,float) or timeout_seconds!=SOCKET_TIMEOUT_SECONDS):_fail()
    if not isinstance(token,str) or not token or len(token)>8192 or any(ord(c)<=32 or ord(c)>126 for c in token):_fail('MODEL_AUTH_FAILED')
    request=Request(context['endpoint'],data=payload,method='POST',headers={
        'Authorization':'Bearer '+token,'Accept':'application/json','Content-Type':'application/json'})
    response=None
    try:
        _deadline(clock,end)
        response=opener(request,timeout=SOCKET_TIMEOUT_SECONDS)
        _deadline(clock,end)
        status=getattr(response,'status',None)
        if type(status) is not int or not 200<=status<300:_http_error(status)
        raw=_bounded_read(response,clock,end)
        try:answer=parse_visual_response(raw,protocol_mode=mode)
        except VisualProtocolError as error:
            _fail('MODEL_'+error.code,_failure_response_diagnostics(raw,mode))
        diagnostics=response_diagnostics(raw,protocol_mode=mode)
        observation=_validated(answer,mode,sample,{**diagnostics,'responseBytes':len(raw),'finalTextPresent':True})
        _deadline(clock,end)
        return dict(answer=deepcopy(answer),observation=observation,payloadSha256=context['payloadSha256'],
                    contextSha256=_digest(context),diagnostics={**diagnostics,'responseBytes':len(raw)})
    except ApprovedVisualTransportError:raise
    except HTTPError as error:
        status=error.code
        try:error.close()
        except Exception:pass
        _http_error(status)
    except Exception:
        raise ApprovedVisualTransportError('MODEL_PROBE_OUTCOME_UNKNOWN') from None
    finally:
        if response is not None:
            try:response.close()
            except Exception:pass


def validate_approved_transport_result(result,context,payload,*,allow_loopback=False):
    """Pure full-schema/sample/identity recheck for the executor callback.

    This checks internal data consistency, not provider provenance or semantics;
    it must never become an untrusted client receipt/approval endpoint.
    """
    context,mode,sample=_prepare(context,payload,allow_loopback=allow_loopback)
    if (not isinstance(result,dict) or set(result)!={'answer','observation','payloadSha256','contextSha256','diagnostics'}
            or result['payloadSha256']!=context['payloadSha256'] or result['contextSha256']!=_digest(context)):_fail()
    facts=result['diagnostics'];expected=protocol_contract(mode)
    safe_keys=set(expected)|{'envelopeKind','choiceCount','finishReason','contentKind','hasReasoning','hasRefusal','hasToolCalls','responseBytes','contentChars','usage'}
    if (not isinstance(facts,dict) or set(facts)!=safe_keys or any(facts[k]!=v for k,v in expected.items())
            or _safe_diagnostics(facts)!=facts
            or facts['envelopeKind']!='object' or type(facts['choiceCount']) is not int or facts['choiceCount']!=1
            or facts['finishReason']!='stop' or facts['contentKind'] not in ('text','parts')
            or type(facts['hasReasoning']) is not bool or facts['hasRefusal'] is not False or facts['hasToolCalls'] is not False
            or type(facts['responseBytes']) is not int or not 0<facts['responseBytes']<=MAX_RESPONSE_BYTES):_fail()
    if result['observation']!=_validated(result['answer'],mode,sample):_fail()
    return True
