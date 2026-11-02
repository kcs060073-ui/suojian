"""Backfill two known productions without rendering, providers or task mutation."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from workbench.production_trace import trace_delivery,trace_feed,write_trace,file_record

MAIN=Path('C:/Users/Tanikawa/suojian')
DEPLOYED=MAIN/'extensions/park-vibe'

def read(path):return json.loads(path.read_text(encoding='utf-8-sig'))

def run(write_runtime=False,label=''):
    if label and not re.fullmatch('[a-z0-9-]{1,32}',label):raise ValueError('Invalid run label')
    suffix='-'+label if label else ''
    out=ROOT/('outputs/optimization-01/samples'+suffix);out.mkdir(parents=True,exist_ok=True)
    identifier='V20261023-c24fbb51'
    folder=DEPLOYED/'work/production/tasks'/identifier
    task=read(folder/'task.json');version=task['versions'][-1]
    task['input']=read(folder/f'input-{version["revision"]:03d}.json')
    vibe=trace_delivery(DEPLOYED,task,version)
    state=read(MAIN/'results/a89d53f9/execution.json')
    plan=read(MAIN/'results/a89d53f9/execution_plan.json')
    feed=trace_feed(MAIN,'a89d53f9',state,plan)
    summary=[]
    for name,root,trace in [('vibe',DEPLOYED,vibe),('feed',MAIN,feed)]:
        path=out/(name+'-trace.json')
        if path.exists():raise ValueError('Sample already exists; preserve previous backfill')
        path.write_text(json.dumps(trace,ensure_ascii=False,indent=2),encoding='utf-8')
        counts=Counter(c['capabilities']['final_referenced']['status'] for s in trace['shots'] for c in s['components'])
        item={'mode':name,'taskId':trace['taskId'],'revision':trace['revision'],'shots':len(trace['shots']),
              'finalReference':dict(counts),'delivery':trace['delivery'],'sample':file_record(path)}
        if write_runtime:item['runtimeTrace']=write_trace(root,trace)
        summary.append(item)
    target=out.parent/('acceptance'+suffix+'.json')
    if target.exists():raise ValueError('Existing acceptance record must not be overwritten')
    target.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=True,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--write-runtime',action='store_true');p.add_argument('--label',default='');a=p.parse_args();run(a.write_runtime,a.label)
