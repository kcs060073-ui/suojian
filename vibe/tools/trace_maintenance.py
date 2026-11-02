"""Task 01 scoped backups, guarded deployment and rollback. Never writes CUTROOM."""
import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MAIN = Path('C:/Users/Tanikawa/suojian')
DEPLOYED = MAIN/'extensions/park-vibe'
OUT = ROOT/'outputs/optimization-01'
MAPPING = [
    (ROOT/'workbench/production_trace.py', DEPLOYED/'workbench/production_trace.py'),
    (ROOT/'workbench/production.py', DEPLOYED/'workbench/production.py'),
    (ROOT/'workbench/server.py', DEPLOYED/'workbench/server.py'),
    (ROOT/'workbench/production_trace.py', MAIN/'suo_jian/production_trace.py'),
    (OUT/'staged-unified.py', MAIN/'suo_jian/integrations/cutroom/unified.py'),
    (OUT/'staged-unified_api.py', MAIN/'suo_jian/ui/unified_api.py'),
]

def sha(path):
    if not path.is_file(): return None
    with path.open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()

def write(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')

def baseline():
    target=OUT/'baseline.json'
    if target.exists(): raise ValueError('Baseline already exists; never overwrite it')
    data={'roots':{'workspace':str(ROOT),'main':str(MAIN),'deployed':str(DEPLOYED)},'code':[], 'protected':[], 'ports':{},'versions':{}}
    files=[ROOT/'workbench/production.py',ROOT/'workbench/server.py',ROOT/'workbench/production_trace.py']+[dst for _,dst in MAPPING]
    for i,path in enumerate(dict.fromkeys(files)):
        backup=OUT/'backups'/f'{i:02d}-{path.name}'
        if path.is_file(): backup.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,backup)
        data['code'].append({'path':str(path),'sha256':sha(path),'backup':str(backup) if path.is_file() else None})
    protected=[MAIN/'results/a89d53f9/execution.json',MAIN/'results/a89d53f9/execution_plan.json',
               DEPLOYED/'work/production/tasks/V20261023-c24fbb51/task.json']
    protected+=list((DEPLOYED/'work/production/tasks/V20261023-c24fbb51').glob('input-*.json'))
    protected+=list((ROOT/'work/production/tasks').glob('*/task.json'))
    protected+=[Path('C:/Users/Tanikawa/CUTROOM/package.json'),Path('C:/Users/Tanikawa/CUTROOM/runtime/workbench/catalog.sqlite')]
    # Existing drafts are protected by hashes; never edit their contents or index.
    state=json.loads((MAIN/'results/a89d53f9/execution.json').read_text(encoding='utf-8'))
    if state.get('preview_path'): protected.append(Path(state['preview_path']))
    if state.get('draft_path'):
        draft=Path(state['draft_path'])
        protected += [draft/name for name in ('draft_content.json','draft_meta_info.json')]
    task=json.loads((DEPLOYED/'work/production/tasks/V20261023-c24fbb51/task.json').read_text(encoding='utf-8'))
    from urllib.parse import unquote
    for version in task['versions']:
        protected.append(DEPLOYED/unquote(version['url'].lstrip('/')))
    for path in protected: data['protected'].append({'path':str(path),'sha256':sha(path)})
    for root in (ROOT,MAIN,DEPLOYED,Path('C:/Users/Tanikawa/CUTROOM')):
        proc=subprocess.run(['git','-C',str(root),'rev-parse','HEAD'],capture_output=True,text=True)
        data['versions'][str(root)]={'gitHead':proc.stdout.strip() if proc.returncode==0 else None}
    for name in ('Flask','pyJianYingDraft','numpy','opencv-python','yt-dlp'):
        try:data['versions'][name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:data['versions'][name]=None
    spec=importlib.util.spec_from_file_location('audit_install',MAIN/'suo_jian/integrations/cutroom/installation.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    data['cutroom']=[module.installation_info(p) for p in (Path('C:/Users/Tanikawa/CUTROOM'),Path('C:/Users/Tanikawa/Documents/Codex/cutroom-beta83'))]
    for port in (8788,8796,8765,8766):
        with socket.socket() as sock:
            sock.settimeout(.5);data['ports'][str(port)]=sock.connect_ex(('127.0.0.1',port))==0
    write(target,data)
    print(json.dumps({'baseline':str(target),'code_files':len(data['code']),'protected_files':len(data['protected'])}))

def deploy(rollback=False):
    base=json.loads((OUT/'baseline.json').read_text(encoding='utf-8'))
    index={row['path']:row for row in base['code']}
    receipt_path=OUT/'deployment.json'
    previous=json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.exists() else []
    if rollback:
        if not previous: raise ValueError('No deployment receipt')
        allowed={str(dst.resolve()) for _,dst in MAPPING}
        for row in previous:
            if str(Path(row['target']).resolve()) not in allowed:raise ValueError('Rollback target outside exact deployment allowlist')
            if sha(Path(row['target']))!=row['after']:raise ValueError('Later changes detected: '+row['target'])
        for row in reversed(previous):
            dst=Path(row['target']);original=index[str(dst)]
            if original['backup']:shutil.copy2(original['backup'],dst)
            else:dst.unlink()  # Only exact files introduced by this scoped deployment.
        write(OUT/'rollback.json',{'restored':[r['target'] for r in previous]})
        return
    # Validate every target before the first write, including unchanged deployment copies.
    deployed={row['target']:row['after'] for row in previous}
    for src,dst in MAPPING:
        if not src.is_file():raise ValueError('Missing staged file: '+str(src))
        if sha(dst)!=deployed.get(str(dst),index[str(dst)]['sha256']):raise ValueError('Baseline drift: '+str(dst))
    rows=list(previous)
    for src,dst in MAPPING:
        shutil.copy2(src,dst)
        frozen=OUT/'release'/(sha(src)+'-'+src.name)
        if not frozen.exists():frozen.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,frozen)
        rows=[r for r in rows if r['target']!=str(dst)]
        rows.append({'source':str(src),'target':str(dst),'before':index[str(dst)]['sha256'],'after':sha(dst),'release':str(frozen)})
        write(receipt_path,rows)  # Partial deployment remains recoverable if interrupted.
    write(receipt_path,rows)
    write(OUT/'workspace-after.json',[{'path':r['path'],'sha256':sha(Path(r['path']))} for r in base['code'] if Path(r['path']).is_relative_to(ROOT)])
    print(json.dumps({'deployed':len(rows),'receipt':str(receipt_path)}))

def enroll():
    """Back up an additional exact hook target before editing it; no overwrite."""
    path=OUT/'baseline.json';data=json.loads(path.read_text(encoding='utf-8'))
    known={r['path'] for r in data['code']}
    for _,dst in MAPPING:
        if str(dst) in known:continue
        backup=OUT/'backups'/f'{len(data["code"]):02d}-{dst.name}'
        if dst.is_file():shutil.copy2(dst,backup)
        data['code'].append({'path':str(dst),'sha256':sha(dst),'backup':str(backup) if dst.is_file() else None})
    write(path,data)

def rollback_workspace():
    baseline=json.loads((OUT/'baseline.json').read_text(encoding='utf-8'))
    current={r['path']:r['sha256'] for r in json.loads((OUT/'workspace-after.json').read_text(encoding='utf-8'))}
    exact={str(ROOT/'workbench/production.py'),str(ROOT/'workbench/server.py')}
    selected=[r for r in baseline['code'] if r['path'] in exact]
    for row in selected:
        if sha(Path(row['path']))!=current[row['path']]:raise ValueError('Later workspace changes detected')
    for row in selected:shutil.copy2(row['backup'],row['path'])
    write(OUT/'workspace-rollback.json',{'restored':[r['path'] for r in selected],
        'retained':'Inactive trace module, tests, tools and evidence are preserved.'})

def verify():
    data=json.loads((OUT/'baseline.json').read_text(encoding='utf-8'))
    changed=[r['path'] for r in data['protected'] if sha(Path(r['path']))!=r['sha256']]
    write(OUT/'protected-check.json',{'changed':changed,'checked':len(data['protected'])})
    print(json.dumps({'protected_changed':changed,'checked':len(data['protected'])}))
    return int(bool(changed))

if __name__=='__main__':
    mode=argparse.ArgumentParser();mode.add_argument('action',choices=['baseline','enroll','deploy','rollback','rollback-workspace','verify']);a=mode.parse_args().action
    if a=='baseline':baseline()
    elif a=='enroll':enroll()
    elif a=='rollback-workspace':rollback_workspace()
    elif a=='verify':sys.exit(verify())
    else:deploy(a=='rollback')
