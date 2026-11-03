"""Optional, bounded monotonic measurements. Never changes workflow decisions."""
from __future__ import annotations
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import math
from pathlib import Path
import re
from statistics import median
import threading
import time
import uuid

_ACTIVE=ContextVar('cutroom_performance',default=None)
_REGISTRY={}
_LOCK=threading.RLock()
_METRICS={'cache_hits','candidate_count','input_count','input_duration_seconds'}

def _ref(value):
    return None if value is None else hashlib.sha256(str(value).encode()).hexdigest()[:16]

def _number(value):
    return type(value) in (int,float) and math.isfinite(value) and 0<=value<=1e12

def _label(value):
    return value if isinstance(value,str) and re.fullmatch('[a-z][a-z0-9_]{0,63}',value) else 'unknown'

class Recorder:
    def __init__(self,sink,*,clock=None,max_pending=2048):
        self.sink=sink;self.clock=clock or time.perf_counter_ns
        self.max_pending=max_pending;self.pending=OrderedDict();self.waiting=OrderedDict()
        self.lock=threading.RLock();self.dropped_events=0

    def emit(self,event):
        try:
            if self.sink(event) is False:self.dropped_events+=1
        except Exception:self.dropped_events+=1

    def enqueued(self,job_id):
        with self.lock:
            self.pending.setdefault(str(job_id),self.clock())
            while len(self.pending)>self.max_pending:self.pending.popitem(last=False)

    def queue_seconds(self,job_id):
        with self.lock:start=self.pending.pop(str(job_id),None)
        return None if start is None else max(0,(self.clock()-start)/1e9)

    @contextmanager
    def operation(self,kind,*,task_id=None,job_id=None,queue_seconds=None,input_fingerprint=None,fingerprint_scope='unknown'):
        state={'recorder':self,'kind':_label(kind),'task_ref':_ref(task_id),'job_ref':_ref(job_id),
               'trace_ref':uuid.uuid4().hex[:16],'queue_seconds':queue_seconds,
               'input_fingerprint':input_fingerprint if isinstance(input_fingerprint,str) and re.fullmatch('[a-f0-9]{64}',input_fingerprint) else None,
               'fingerprint_scope':fingerprint_scope if fingerprint_scope in {'job_payload','api_request','render_action'} else 'unknown',
               'metrics':{k:None for k in _METRICS},'spans':0,'response_failed':False}
        token=_ACTIVE.set(state)
        try:
            with self.span('handler'):yield state
        finally:_ACTIVE.reset(token)

    @contextmanager
    def span(self,stage):
        state=_ACTIVE.get()
        if state is None or state['recorder'] is not self:
            yield;return
        start=self.clock();outcome='success'
        try:yield
        except BaseException:
            outcome='failed';raise
        finally:
            state['spans']+=1
            if state['spans']<=128:
                self.emit({'event':'span','kind':state['kind'],'stage':_label(stage),
                           'seconds':max(0,(self.clock()-start)/1e9),'outcome':'failed' if state['response_failed'] else outcome,
                           **{k:state[k] for k in ['task_ref','job_ref','trace_ref','queue_seconds','input_fingerprint','fingerprint_scope']},
                           **state['metrics']})
            else:self.dropped_events+=1

    def observe_workflow(self,task_id,stage,status):
        """Measure only the observed interval; prior wait time remains unknown."""
        key=_ref(task_id);stage=_label(stage)
        category=('approval_wait' if stage=='full_review' else 'user_wait') if status=='awaiting_user' else None
        now=self.clock();event=None
        with self.lock:
            old=self.waiting.get(key)
            if old and (old[0],old[1])!=(stage,category):
                self.waiting.pop(key)
                event={'event':'wait','kind':'workflow','stage':old[0],'category':old[1],
                       'task_ref':key,'seconds':None,'observed_seconds':max(0,(old[3]-old[2])/1e9),
                       'coverage':'observed_lower_bound','outcome':'success'}
            if category:
                if key not in self.waiting:self.waiting[key]=(stage,category,now,now)
                else:
                    prior=self.waiting[key]
                    self.waiting[key]=(stage,category,prior[2],now)
            while len(self.waiting)>self.max_pending:self.waiting.popitem(last=False)
        if event:self.emit(event)

def measured(stage):
    def decorate(fn):
        @wraps(fn)
        def call(*args,**kwargs):
            state=_ACTIVE.get()
            if state is None:return fn(*args,**kwargs)
            with state['recorder'].span(stage):return fn(*args,**kwargs)
        return call
    return decorate

def identity(fingerprint,scope):
    state=_ACTIVE.get()
    if (state is not None and scope in {'job_payload','api_request','render_action'}
            and isinstance(fingerprint,str) and re.fullmatch('[a-f0-9]{64}',fingerprint)):
        state['input_fingerprint']=fingerprint;state['fingerprint_scope']=scope

def metric(name,value):
    state=_ACTIVE.get()
    if state is not None and name in _METRICS and _number(value):state['metrics'][name]=value

def _key(db):return str(Path(getattr(db,'path',db)).resolve()).casefold()

def configure(db,sink):
    with _LOCK:
        recorder=Recorder(sink);_REGISTRY[_key(db)]=recorder
        while len(_REGISTRY)>16:_REGISTRY.pop(next(iter(_REGISTRY)))
        return recorder

def unregister(db,expected=None):
    with _LOCK:
        if expected is None or _REGISTRY.get(_key(db)) is expected:_REGISTRY.pop(_key(db),None)

def recorder_for(db):
    with _LOCK:return _REGISTRY.get(_key(db))

def enqueued(db,job_id):
    try:
        r=recorder_for(db)
        if r:r.enqueued(job_id)
    except Exception:pass

def observed_workflow(db,task_id,stage,status):
    try:
        r=recorder_for(db)
        if r:r.observe_workflow(task_id,stage,status)
    except Exception:pass

def execute_handler(db,job,handler):
    r=recorder_for(db)
    if r is None:return handler(job)
    # Metrics are optional; schema/diagnostic failures cannot prevent the job.
    fingerprint=None;count=None;duration=None
    try:
        row=db.connection.execute('SELECT payload_digest FROM job_payloads WHERE job_id=?',(job.job_id,)).fetchone()
        fingerprint=row[0] if row else None
        if job.task_id and job.job_type=='render_and_qa':
            size=db.connection.execute('SELECT COUNT(*),MAX(narrative_end) FROM scene_cards WHERE task_id=?',(job.task_id,)).fetchone()
            count,duration=size[0],size[1]
    except Exception:pass
    with r.operation(job.job_type,task_id=job.task_id,job_id=job.job_id,queue_seconds=r.queue_seconds(job.job_id),input_fingerprint=fingerprint,fingerprint_scope='job_payload'):
        metric('input_count',count)
        metric('input_duration_seconds',duration)
        result=handler(job)
        values=result.get('result',{}) if isinstance(result,dict) else {}
        if isinstance(values,dict):
            if type(values.get('reused')) is bool:metric('cache_hits',int(values['reused']))
            for key in ['candidate_count','candidateCount','totalWindows']:
                if key in values:metric('candidate_count',values[key]);break
        return result

def summarize(events):
    groups={}
    for event in events:
        if not isinstance(event,dict) or event.get('event')!='span':continue
        key=tuple(event.get(k) for k in ['kind','stage','version','input_fingerprint','fingerprint_scope','cache_hits','input_count','candidate_count','input_duration_seconds'])
        groups.setdefault(key,[]).append(event)
    result=[]
    for key,rows in groups.items():
        values=sorted(e['seconds'] for e in rows if _number(e.get('seconds')))
        result.append({**dict(zip(['kind','stage','version','input_fingerprint','fingerprint_scope','cache_hits','input_count','candidate_count','input_duration_seconds'],key)),
                       'n':len(rows),'unknown_n':len(rows)-len(values),
                       'failures':sum(e.get('outcome')=='failed' for e in rows),
                       'p50_seconds':median(values) if values else None,
                       'p95_seconds':values[math.ceil(.95*len(values))-1] if values else None,
                       'max_seconds':max(values) if values else None})
    return result


def observe_api(fn):
    @wraps(fn)
    def call(self,method,path,*args,**kwargs):
        r=recorder_for(self.db)
        if r is None:return fn(self,method,path,*args,**kwargs)
        import json
        identity=hashlib.sha256(json.dumps([method,path,kwargs.get('query')],sort_keys=True,default=str).encode()).hexdigest()
        parts=path.strip('/').split('/')
        task=parts[2] if len(parts)>2 and parts[:2]==['api','tasks'] else None
        with r.operation('request',task_id=task,input_fingerprint=identity,fingerprint_scope='api_request') as state:
            result=fn(self,method,path,*args,**kwargs)
            status=getattr(result,'status',None)
            state['response_failed']=isinstance(status,int) and status>=400
            payload=getattr(result,'payload',None)
            if isinstance(payload,dict):
                workflow=payload.get('workflow')
                if isinstance(workflow,dict):
                    r.observe_workflow(workflow.get('task_id') or task,workflow.get('current_stage'),workflow.get('run_status'))
            return result
    return call
