"""Write a reviewable diff and verify deployed hashes; does not touch production."""
import difflib
import hashlib
import importlib.metadata
import json
from pathlib import Path
import socket
import re

ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'outputs/optimization-01'

def sha(path):
    if not path.is_file():return None
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

base=json.loads((OUT/'baseline.json').read_text(encoding='utf-8'))
deployed=json.loads((OUT/'deployment.json').read_text(encoding='utf-8'))
patch=[];changes=[]
for row in base['code']:
    path=Path(row['path']);before=Path(row['backup']).read_text(encoding='utf-8-sig') if row['backup'] else ''
    after=path.read_text(encoding='utf-8-sig') if path.is_file() else ''
    if sha(path)!=row['sha256']:
        changes.append({'path':str(path),'before':row['sha256'],'after':sha(path)})
        patch.extend(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile='before/'+path.name,tofile=str(path)))
(OUT/'changes.patch').write_text(''.join(patch),encoding='utf-8')
data={'changedCode':changes,'deploymentMatches':all(sha(Path(r['target']))==r['after'] for r in deployed),
      'workspaceMatchesDeployed':all(sha(Path(r['source']))==sha(Path(r['target'])) for r in deployed),
      'installedPythonPackages':{d.metadata['Name']:d.version for d in importlib.metadata.distributions()}}
data['ports']={}
for port in (8788,8796,8765,8766):
    with socket.socket() as sock:
        sock.settimeout(.5);data['ports'][str(port)]=sock.connect_ex(('127.0.0.1',port))==0
(OUT/'change-summary.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
tests={}
for name in ('tests-before','tests-after','feed-tests-before','feed-tests-after','upgrade-tests-before','upgrade-tests-after','trace-tests'):
    text=(OUT/(name+'.log')).read_text(encoding='utf-8',errors='replace')
    found=re.search(r'SUMMARY run=(\d+) fail=(\d+) err=(\d+) skip=(\d+)',text)
    if found:tests[name]=dict(zip(('run','fail','error','skip'),map(int,found.groups())))
    else:
        found=re.search(r'Ran (\d+) tests?',text)
        tests[name]={'run':int(found[1]) if found else None,'passed':bool(re.search(r'^OK\b',text,re.M)),'skipped':0}
(OUT/'test-results.json').write_text(json.dumps(tests,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in data.items() if k!='installedPythonPackages'},ensure_ascii=True,indent=2))
