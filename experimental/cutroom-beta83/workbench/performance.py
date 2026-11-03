"""Local bounded performance evidence, using the support log's path/lock guards."""
import json
from pathlib import Path
import re
from .diagnostics import DiagnosticLog
from media_catalog.performance import _number, summarize

def safe_event(event):
    if not isinstance(event,dict) or event.get('event') not in {'span','wait'}:return None
    result={'event':event['event']}
    scope=event.get('fingerprint_scope')
    result['fingerprint_scope']=scope if scope in {'job_payload','api_request','render_action'} else 'unknown'
    for key in ['kind','stage','category','coverage','outcome']:
        value=event.get(key)
        if isinstance(value,str) and re.fullmatch('[a-z][a-z0-9_]{0,63}',value):result[key]=value
    for key in ['task_ref','job_ref','trace_ref','input_fingerprint']:
        value=event.get(key)
        if isinstance(value,str) and re.fullmatch('[a-f0-9]{16}|[a-f0-9]{64}',value):result[key]=value
    for key in ['seconds','observed_seconds','queue_seconds','cache_hits','candidate_count','input_count','input_duration_seconds']:
        value=event.get(key);result[key]=value if _number(value) else None
    version=event.get('version')
    if isinstance(version,str) and re.fullmatch(r'(?:unknown|\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)',version):result['version']=version
    return result

class PerformanceLog(DiagnosticLog):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.path=self.directory/'performance.jsonl'

    def record(self,event):
        if not self.enabled:return False
        try:
            value=safe_event({**event,'version':self.version})
            if value is None:return False
            data=(json.dumps(value,separators=(',',':'))+'\n').encode('utf-8')
            if len(data)>self.max_bytes:return False
            with self._locked():
                paths=[self.directory/('performance.jsonl' if i==0 else f'performance.{i}.jsonl') for i in range(4)]
                for p in paths:self._safe_path(p)
                if self.path.exists() and self.path.stat().st_size+len(data)>self.max_bytes:
                    paths[3].unlink(missing_ok=True)
                    for i in [2,1]:
                        if paths[i].exists():paths[i].replace(paths[i+1])
                    self.path.replace(paths[1])
                with self.path.open('ab') as f:f.write(data)
            return True
        except Exception:return False

    def events(self):
        events=[]
        for name in ['performance.3.jsonl','performance.2.jsonl','performance.1.jsonl','performance.jsonl']:
            try:
                path=self.directory/name;self._safe_path(path)
                with path.open('rb') as f:raw=f.read(min(self.max_bytes,256*1024))
            except OSError:continue
            for line in raw.splitlines():
                try:value=safe_event(json.loads(line))
                except (ValueError,TypeError,UnicodeError):continue
                if value:events.append(value)
        return events[-4000:]

    def report(self):
        events=self.events()
        return {'schema':'cutroom-performance/v1','version':self.version,'event_count':len(events),
                'samples':summarize(events),'spans':[e for e in events if e['event']=='span'],'waits':[e for e in events if e['event']=='wait'],
                'limits':['Rolling bounded sample, not all historical jobs. Fingerprints identify request payloads or render action state, not all input file bytes; compare content manifests separately.','Wait lower bounds run from first to last observation still waiting; unobserved time is unknown.','Queue is unknown after process restart or unobserved enqueue.','Percentiles describe samples only; do not promise fixed time per film. Handler spans exclude worker result persistence; nested spans are inclusive and must not be added together.']}

def main():
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    with a.output.open('x',encoding='utf-8') as f:json.dump(PerformanceLog(a.root).report(),f,ensure_ascii=False,indent=2)
    return 0

if __name__=='__main__':raise SystemExit(main())
