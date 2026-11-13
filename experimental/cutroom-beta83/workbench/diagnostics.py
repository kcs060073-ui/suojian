"""Bounded local support records. No exception messages, request bodies or credentials."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import stat
import threading
import time
import traceback
import uuid
import zipfile

_LOCK=threading.RLock()
_SOURCES={'api','browser','worker','startup','installer','mcp'}
_PAGES={'tasks','library','packlib','settings','script','storyboard','roughcut','packaging','review'}
_KINDS={'javascript','promise','request','resource'}
_SEGMENTS=set('api auth bootstrap local-ready tasks jobs service status settings accounts providers profiles model-profiles capabilities probe libraries materials catalog assets packaging planning script storyboard roughcut review voice-editing actions run start cancel resume publish diagnostics export browser'.split())
_VERSION=re.compile(r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\Z')

def _version(root):
    try:
        value=json.loads((root/'package.json').read_text(encoding='utf-8-sig'))['version']
        return value if isinstance(value,str) and _VERSION.fullmatch(value) else 'unknown'
    except (OSError,ValueError,KeyError,TypeError):return 'unknown'

def safe_route(path):
    path=str(path).split('?',1)[0].split('#',1)[0]
    return '/'+ '/'.join(p if p in _SEGMENTS else ':id' for p in path.strip('/').split('/')[:12])

def _safe_event(value):
    """Reapply the allowlist on export; edited/corrupt local files aren't trusted."""
    if not isinstance(value,dict):return None
    ident=value.get('diagnosticId','')
    stamp=value.get('timestamp','')
    if not isinstance(ident,str) or not re.fullmatch(r'diag-[a-f0-9]{16}',ident):return None
    if not isinstance(stamp,str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z',stamp):return None
    result={'diagnosticId':ident,'timestamp':stamp,'source':value.get('source') if value.get('source') in _SOURCES else 'api'}
    for key,pattern,limit in [('code',r'[A-Z][A-Z0-9_]{0,79}',80),('errorType',r'[A-Za-z][A-Za-z0-9_]{0,79}',80),('version',r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?',80),('taskRef',r'[a-f0-9]{12}',12),('jobRef',r'[a-f0-9]{12}',12)]:
        v=value.get(key)
        if isinstance(v,str) and len(v)<=limit and re.fullmatch(pattern,v):result[key]=v
    for key in ('status','line','column','errno'):
        v=value.get(key)
        if type(v) is int and 0<=v<=10000000:result[key]=v
    if value.get('method') in {'GET','HEAD','POST','PUT','PATCH','DELETE'}:result['method']=value['method']
    if isinstance(value.get('route'),str):result['route']=safe_route(value['route'])
    if value.get('page') in _PAGES:result['page']=value['page']
    if value.get('kind') in _KINDS:result['kind']=value['kind']
    occurred=value.get('occurredAt')
    if isinstance(occurred,str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z',occurred):result['occurredAt']=occurred
    frames=[]
    for frame in (value.get('frames') or [])[:12]:
        if not isinstance(frame,dict):continue
        file=frame.get('file','')
        if isinstance(file,str) and re.fullmatch(r'(?:workbench|media_catalog|cutroom_mcp|scripts)/[A-Za-z0-9_/-]+\.py',file) and '..' not in file:
            frames.append({'file':file,'line':frame['line'] if type(frame.get('line')) is int and 0<frame['line']<1000000 else 0})
    if frames:result['frames']=frames
    return result

class DiagnosticLog:
    def __init__(self,root:Path,*,max_bytes=256*1024,directory=None,enabled=True):
        self.enabled=enabled
        self.root=Path(root).resolve()
        self.directory=Path(directory) if directory else self.root/'runtime'/'diagnostics'
        self.path=self.directory/'errors.jsonl'
        self.max_bytes=max_bytes
        self.version=_version(self.root)

    def _safe_path(self,path):
        for parent in [path,*path.parents]:
            if parent.exists() or parent.is_symlink():
                info=parent.lstat()
                if parent.is_symlink() or getattr(info,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',1024):
                    raise OSError('diagnostics path is redirected')
            if parent==self.directory.parent:break

    @contextmanager
    def _locked(self):
        with _LOCK:
            self._safe_path(self.directory)
            self.directory.mkdir(parents=True,exist_ok=True)
            lock_path=self.directory/'.write.lock';self._safe_path(lock_path)
            with lock_path.open('a+b') as handle:
                handle.seek(0,2)
                if handle.tell()==0:handle.write(b'0');handle.flush()
                handle.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                try:yield
                finally:
                    handle.seek(0)
                    if os.name=='nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                    else:fcntl.flock(handle,fcntl.LOCK_UN)

    def _append(self,event):
        data=(json.dumps(event,ensure_ascii=True,separators=(',',':'))+'\n').encode('utf-8')
        with self._locked():
            for i in range(4):self._safe_path(self.directory/('errors.jsonl' if not i else f'errors.{i}.jsonl'))
            if self.path.exists() and self.path.stat().st_size+len(data)>self.max_bytes:
                (self.directory/'errors.3.jsonl').unlink(missing_ok=True)
                for i in (2,1):
                    old=self.directory/f'errors.{i}.jsonl'
                    if old.exists():old.replace(self.directory/f'errors.{i+1}.jsonl')
                self.path.replace(self.directory/'errors.1.jsonl')
            with self.path.open('ab') as handle:handle.write(data)

    def record(self,source,*,error=None,code='ERROR',method=None,path=None,status=None,diagnostic_id=None,task_id=None,job_id=None,**extra):
        if not self.enabled:return None
        try:
            value={'diagnosticId':diagnostic_id or 'diag-'+uuid.uuid4().hex[:16],'timestamp':datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z'),'source':source,'code':code,'version':self.version,**extra}
            value.update(method=method,route=safe_route(path) if path else None,status=status)
            for field,ident in [('taskRef',task_id),('jobRef',job_id)]:
                if ident is not None:value[field]=hashlib.sha256(str(ident).encode()).hexdigest()[:12]
            if isinstance(error,BaseException):
                value['errorType']=type(error).__name__;value['errno']=getattr(error,'errno',None)
                frames=[]
                for frame in traceback.extract_tb(error.__traceback__)[-12:]:
                    try:relative=Path(frame.filename).resolve().relative_to(self.root).as_posix()
                    except ValueError:continue
                    frames.append({'file':relative,'line':frame.lineno})
                value['frames']=frames
            event=_safe_event(value)
            if event is None:return None
            self._append(event)
            return event['diagnosticId']
        except Exception:return None  # logging must never change business outcomes

    def record_browser(self,value):
        if not isinstance(value,dict):value={}
        name=value.get('name')
        if name not in {'Error','TypeError','ReferenceError','RangeError','SyntaxError','URIError','EvalError','NetworkError','AbortError'}:name='Error'
        return self.record('browser',code='BROWSER_ERROR',errorType=name,kind=value.get('kind'),page=value.get('page'),line=value.get('line'),column=value.get('column'),occurredAt=value.get('time'))

    def events(self):
        records=[]
        # Bounded files only; never collect arbitrary logs, env files or the database.
        for name in ['errors.3.jsonl','errors.2.jsonl','errors.1.jsonl','errors.jsonl','lifecycle.jsonl']:
            path=self.directory/name
            try:
                self._safe_path(path)
                with path.open('rb') as f:raw=f.read(512*1024)
            except OSError:continue
            for line in raw.splitlines():
                try:event=_safe_event(json.loads(line))
                except (ValueError,TypeError,UnicodeError):continue
                if event:records.append(event)
        return records[-4000:]

    def has(self,ident):return any(e['diagnosticId']==ident for e in self.events())

    def export_zip(self):
        events=self.events()
        meta={'product':'CUTROOM','version':self.version,'exportedAt':datetime.now(timezone.utc).isoformat(timespec='seconds'),'system':platform.system(),'architecture':platform.machine(),'python':platform.python_version(),'eventCount':len(events),'contentPolicy':'Error categories and code locations only; no raw messages, request/response bodies, credentials, task text, media, or database.'}
        result=io.BytesIO()
        with zipfile.ZipFile(result,'w',zipfile.ZIP_DEFLATED) as z:
            z.writestr('environment.json',json.dumps(meta,ensure_ascii=False,indent=2))
            z.writestr('errors.jsonl',''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in events))
            z.writestr('说明.txt','将此 ZIP 发给维护人员，并补充：出错时间、所在页面、刚才点了什么。\n日志只记录错误类别和代码位置，不含密钥、文案、素材或数据库。\n无记录不代表没有错误：断电、磁盘不可写、旧版程序或浏览器尚未加载时可能无法记录。\n')
        return result.getvalue()

def main():
    parser=argparse.ArgumentParser(description='导出 剪辑工作台 报错日志（无业务内容）')
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--export',type=Path)
    args=parser.parse_args();log=DiagnosticLog(args.root)
    dest=args.export or args.root/'out'/('CUTROOM-errors-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]+'.zip')
    dest.parent.mkdir(parents=True,exist_ok=True)
    with dest.open('xb') as f:f.write(log.export_zip())
    print(dest)
    return 0

if __name__=='__main__':raise SystemExit(main())
