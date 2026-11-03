"""Offline, manifest-checked program updates. Never migrates user data or kills processes."""
from __future__ import annotations
import argparse, contextlib, datetime, hashlib, json, os, re, stat, subprocess, sys, uuid, zipfile
from pathlib import Path

class UpdateError(Exception):
    def __init__(self, code, message):
        super().__init__(message); self.code = code

def fail(code, message): raise UpdateError(code, message)
def digest(data): return hashlib.sha256(data).hexdigest()
def read_json(path): return json.loads(path.read_text(encoding='utf-8-sig'))

def safe_path(root, name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name or name.startswith('/'):
        fail('PATH', '安装包文件路径无效。')
    parts = name.split('/')
    if any(p in ('', '.', '..') or p.endswith((' ', '.')) or re.match(r'^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)', p, re.I) for p in parts):
        fail('PATH', '安装包包含不安全的文件路径。')
    target = root.joinpath(*parts)
    for p in (target, *target.parents):
        if p.exists() or p.is_symlink():
            st = p.lstat()
            if p.is_symlink() or getattr(st, 'st_file_attributes', 0) & 0x400 or (p.is_file() and st.st_nlink > 1):
                fail('LINK', '安装目录含链接或重定向文件，请交维护人员处理。')
    return target

ALLOWED_DIRS = {'media_catalog','cutroom_mcp','scripts','src','web-ui','workbench','assets','asset-packs','docs','release'}
ALLOWED_FILES = {'.env.example','package.json','package-lock.json','tsconfig.json','requirements.txt','requirements-mcp.txt','README.md','THIRD_PARTY_NOTICES.md','广告出片指南.md','安装CUTROOM.bat','启动CUTROOM.bat','导出报错日志.bat','产品更新升级.bat'}
DATA_ROOTS = {'runtime','out','public','assets-local'}

def data_placeholder(name, entry):
    return name.split('/')[0] in DATA_ROOTS and name.endswith('/.gitkeep') and entry.get('bytes')==0 and entry.get('sha256')==digest(b'')

def manifest_files(value):
    if not isinstance(value, dict) or not isinstance(value.get('files'), list) or not 1 <= len(value['files']) <= 10000:
        fail('MANIFEST', '不是完整的 CUTROOM 发行包，请下载 Windows ZIP，勿选 Source code。')
    result = {}; aliases = set()
    for e in value['files']:
        if not isinstance(e, dict): fail('MANIFEST','发行清单无效。')
        n=e.get('path');safe_path(Path.cwd(), n)
        if n not in ALLOWED_FILES and n.split('/')[0] not in ALLOWED_DIRS and not data_placeholder(n,e):
            fail('DATA_PATH', '发行包试图写入用户数据目录，已停止。')
        if n.casefold() in aliases or not isinstance(e.get('bytes'),int) or e['bytes'] < 0 or not re.fullmatch('[0-9a-f]{64}', str(e.get('sha256',''))):
            fail('MANIFEST','发行清单重复或校验字段无效。')
        aliases.add(n.casefold()); result[n]=e
    return result

def version_key(v):
    m = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+))?', str(v))
    if not m: fail('VERSION','此安装版本不支持快捷升级，请交维护人员处理。')
    return (*map(int,m.group(1,2,3)), 0 if m[4] else 1, int(m[4] or 0))

def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp=path.with_name('.cutroom-'+uuid.uuid4().hex)
    try:
        with temp.open('xb') as f: f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if temp.exists(): temp.unlink()

def update_dir(root): return safe_path(root,'runtime/software-updates')
def pending_path(root): return safe_path(root,'runtime/software-updates/pending.json')

@contextlib.contextmanager
def update_lock(root):
    folder=update_dir(root);folder.mkdir(parents=True,exist_ok=True)
    path=safe_path(root,'runtime/software-updates/update.lock')
    with path.open('a+b') as f:
        f.seek(0);f.write(b'0');f.flush();f.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError: fail('BUSY','另一个升级窗口正在运行，请先关闭或等待完成。')
        try: yield
        finally:
            f.seek(0)
            if os.name=='nt': msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(f.fileno(),fcntl.LOCK_UN)

def dependency_contract(payload):
    package=json.loads(payload['package.json'])
    if package.get('name') != 'digital-human-mix': fail('PRODUCT','安装包不属于 CUTROOM。')
    lock=json.loads(payload['package-lock.json'])
    lock.pop('version',None)
    if isinstance(lock.get('packages',{}).get(''),dict):lock['packages'][''].pop('version',None)
    return (payload['requirements.txt'].replace(b'\r\n',b'\n'),payload['requirements-mcp.txt'].replace(b'\r\n',b'\n'),lock,
            {k:v for k,v in package.items() if k in ('dependencies','devDependencies','engines','optionalDependencies')},
            {n:(b.replace(b'\r\n',b'\n') if Path(n).suffix in ('.py','.sql','.json','.ts') else b) for n,b in payload.items() if 'migration' in n.lower() or 'schema' in n.lower()})

def compatibility_digest(payload):
    requirements,mcp,lock,package,schema=dependency_contract(payload)
    return digest(json.dumps({'requirements':digest(requirements),'mcp':digest(mcp),'lock':lock,'package':package,
                              'schema':{n:digest(b) for n,b in schema.items()}},sort_keys=True,separators=(',',':')).encode())

def prepare_update(root, archive):
    root=Path(root).absolute();archive=Path(archive).absolute()
    if pending_path(root).exists(): fail('INTERRUPTED','发现未完成的升级，请先选择“恢复上次升级”再重试。')
    manifest_path=safe_path(root,'release-manifest.json')
    if not manifest_path.is_file(): fail('INSTALL','请选择已安装 CUTROOM 的文件夹，不要选择源码或新解压目录。')
    old=read_json(manifest_path);old_files=manifest_files(old)
    with zipfile.ZipFile(archive) as z:
        infos=z.infolist()
        if len(infos)>10001 or sum(i.file_size for i in infos)>1024*1024*1024:
            fail('SIZE','安装包大小或文件数超过快捷升级范围。')
        names=[i.filename for i in infos]
        if len(set(n.casefold() for n in names))!=len(names):fail('ZIP','安装包含重复路径。')
        for i in infos:
            safe_path(Path.cwd(),i.filename)
            if i.is_dir() or stat.S_ISLNK(i.external_attr>>16):fail('ZIP','安装包格式不符合发行约定。')
        manifests=[n for n in names if n.count('/')==1 and n.endswith('/release-manifest.json')]
        if len(manifests)!=1:fail('ZIP','请选择官方 Windows 发行 ZIP。')
        prefix=manifests[0].split('/')[0]+'/'
        manifest_bytes=z.read(manifests[0]);new=json.loads(manifest_bytes)
        new_files=manifest_files(new)
        if prefix != 'CUTROOM-v'+new['version']+'/':fail('VERSION','安装包名称与版本不一致。')
        if version_key(new['version'])<=version_key(old['version']):fail('NO_UPDATE','所选版本不比当前版本新，无需升级；不支持降级覆盖。')
        expected={prefix+n for n in new_files}|{prefix+'release-manifest.json'}
        if set(names)!=expected:fail('ZIP','安装包文件与发行清单不一致。')
        payload={}
        for n,e in new_files.items():
            b=z.read(prefix+n)
            if len(b)!=e['bytes'] or digest(b)!=e['sha256']:fail('CHECKSUM','安装包已损坏，请重新下载。')
            if not data_placeholder(n,e): payload[n]=b
    if json.loads(payload['package.json']).get('version')!=new['version']:fail('VERSION','安装包版本信息不一致。')
    previous={}
    for n,e in old_files.items():
        if data_placeholder(n,e):continue
        p=safe_path(root,n)
        if not p.is_file():fail('LOCAL_CHANGE','原程序文件缺失，请导出报错日志并联系维护人员。')
        b=p.read_bytes()
        if digest(b)!=e['sha256']:fail('LOCAL_CHANGE','原程序有本地修改，已保留；请交维护人员合并后升级。')
        previous[n]=b
    if dependency_contract(previous)[:4]!=dependency_contract(payload)[:4]:
        fail('MAINTENANCE','本次涉及运行依赖变化，请交维护人员升级；原安装未改动。')
    old_contract=old.get('update_compatibility') or compatibility_digest(previous)
    new_contract=new.get('update_compatibility') or compatibility_digest(payload)
    if (not re.fullmatch('[0-9a-f]{64}',str(old_contract)) or not re.fullmatch('[0-9a-f]{64}',str(new_contract)) or old_contract!=new_contract):
        fail('MAINTENANCE','本次涉及运行环境或数据库结构变化，请交维护人员升级；原安装未改动。')
    changes={n:b for n,b in payload.items() if previous.get(n)!=b}
    for n in changes:
        p=safe_path(root,n)
        if n not in old_files and p.exists():fail('COLLISION','新程序文件与本地文件重名，已停止以保留原文件。')
    changes['release-manifest.json']=manifest_bytes
    previous['release-manifest.json']=manifest_path.read_bytes()
    return {'root':root,'from':old['version'],'to':new['version'],'changes':changes,
            'previous':previous,'removed':sorted(n for n in set(old_files)-set(new_files) if not data_placeholder(n,old_files[n]))}

def process_arguments(command):
    if not command:return []
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        argc=ctypes.c_int()
        parse=ctypes.windll.shell32.CommandLineToArgvW
        parse.argtypes=[wintypes.LPCWSTR,ctypes.POINTER(ctypes.c_int)]
        parse.restype=ctypes.POINTER(wintypes.LPWSTR)
        ptr=parse(command,ctypes.byref(argc))
        if not ptr:return []
        try:return [ptr[i] for i in range(argc.value)]
        finally:
            free=ctypes.windll.kernel32.LocalFree;free.argtypes=[ctypes.c_void_p];free.restype=ctypes.c_void_p
            free(ptr)
    import shlex
    return [s.strip('"') for s in shlex.split(command,posix=False)]

def known_process_root(process, processes):
    seen=set()
    for _ in range(12):
        pid=process.get('ProcessId')
        if pid in seen:return None
        seen.add(pid)
        for arg in process_arguments(process.get('CommandLine')):
            path=Path(arg)
            normalized=arg.replace('\\','/').casefold()
            if path.is_absolute() and normalized.endswith(('/scripts/cutroom_mcp.py','/scripts/cutroom_mcp.pyc','/workbench/start-local.ps1')):
                return path.parent.parent.resolve(strict=False)
        parent=processes.get(process.get('ParentProcessId'))
        if not parent:return None
        # Ignore a reused PID that was created later than its purported child.
        if parent.get('CreationDate') and process.get('CreationDate') and parent['CreationDate']>process['CreationDate']:return None
        process=parent
    return None

def check_process_snapshot(root, processes, current_pid=None):
    if isinstance(processes,dict):processes=[processes]
    if not isinstance(processes,list):fail('PROCESS_CHECK','无法核实后台进程，未更新程序。')
    by_pid={p.get('ProcessId'):p for p in processes}
    for p in processes:
        if p.get('ProcessId')==(current_pid or os.getpid()):continue
        args=process_arguments(p.get('CommandLine'));normalized=[a.replace('\\','/').casefold() for a in args]
        service=any(a in ('workbench.server','cutroom_mcp.server') for a in normalized) or any(a.rsplit('/',1)[-1] in ('cutroom_mcp.py','cutroom_mcp.pyc','start-local.ps1') for a in normalized)
        renderer=any(a.startswith('scripts/') and a.endswith(('.ts','.js')) for a in normalized) or Path(str(p.get('ExecutablePath') or '')).name.casefold() in ('node.exe','ffmpeg.exe','ffprobe.exe')
        if not service and not renderer:continue
        owner=known_process_root(p,by_pid)
        if owner is not None:
            if os.path.normcase(str(owner.resolve(strict=False)))!=os.path.normcase(str(Path(root).resolve(strict=False))):continue
        elif not service:
            # Only claim a detached renderer when its absolute input is inside this install.
            if not any(Path(a).is_absolute() and Path(a).resolve(strict=False).is_relative_to(Path(root).resolve(strict=False)) for a in args):continue
        fail('RUNNING','CUTROOM 后台或 MCP 仍在运行。保存工作、等待任务完成后重启电脑；先运行升级入口，不要先打开 CUTROOM 或 WorkBuddy。关闭网页不会停止后台。')

def assert_idle(root):
    if os.name!='nt':fail('PLATFORM','快捷升级仅支持 Windows。')
    script="[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CreationDate,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
    try:
        r=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],capture_output=True,timeout=25,creationflags=0x08000000)
        if r.returncode:raise ValueError()
        processes=json.loads(r.stdout.decode('utf-8-sig',errors='strict'))
    except (OSError,ValueError,subprocess.TimeoutExpired):fail('PROCESS_CHECK','无法核实 CUTROOM 是否停止，请重试或联系维护人员；未更新程序。')
    check_process_snapshot(root,processes)

def validate_current(plan):
    root=plan['root']
    for n,b in plan['previous'].items():
        p=safe_path(root,n)
        if not p.is_file() or p.read_bytes()!=b:fail('CHANGED','预检后原文件发生变化，未开始升级，请重新检查。')
    for n in plan['changes']:
        if n not in plan['previous'] and safe_path(root,n).exists():fail('CHANGED','预检后出现同名文件，未开始升级。')

def restore_journal(root, journal):
    ident=journal.get('id','')
    if not re.fullmatch('[0-9a-f]{32}',ident):fail('JOURNAL','升级记录无效，请联系维护人员。')
    backup=safe_path(root,'runtime/software-updates/'+ident)
    entries=journal['entries']
    # Validate all files before restoring any. Never overwrite unrelated later changes.
    for n,e in entries.items():
        if n!='release-manifest.json':manifest_files({'files':[{'path':n,'bytes':0,'sha256':'0'*64}]})
        p=safe_path(root,n);current=digest(p.read_bytes()) if p.exists() else None
        if current not in (e['before'],e['after']):fail('RECOVERY_CONFLICT','升级后文件又发生变化，请联系维护人员，暂不覆盖。')
        if e['before'] is not None:
            b=safe_path(backup,'files/'+n).read_bytes()
            if digest(b)!=e['before']:fail('BACKUP','升级备份校验失败，请联系维护人员。')
    for n,e in entries.items():
        p=safe_path(root,n)
        if e['before'] is None:
            if p.exists():p.unlink()
        else:atomic_write(p,safe_path(backup,'files/'+n).read_bytes())
    journal['status']='restored'
    atomic_write(backup/'receipt.json',json.dumps(journal,ensure_ascii=False,indent=2).encode('utf-8'))
    pending_path(root).unlink()

def recover_update(root, *, check_idle=None):
    root=Path(root).absolute()
    with update_lock(root):
        (check_idle or (lambda:assert_idle(root)))()
        p=pending_path(root)
        if not p.exists():return {'status':'nothing_to_recover'}
        restore_journal(root,read_json(p))
        return {'status':'restored'}

def apply_update(plan, *, check_idle=None):
    root=plan['root'];check_idle=check_idle or (lambda:assert_idle(root))
    with update_lock(root):
        if pending_path(root).exists():fail('INTERRUPTED','请先恢复上次未完成的升级。')
        check_idle();validate_current(plan)
        ident=uuid.uuid4().hex;backup=safe_path(root,'runtime/software-updates/'+ident)
        backup.mkdir()
        entries={}
        for n in list(plan['changes'])+plan['removed']:
            old=plan['previous'].get(n);new=plan['changes'].get(n)
            entries[n]={'before':digest(old) if old is not None else None,'after':digest(new) if new is not None else None}
            if old is not None:atomic_write(safe_path(backup,'files/'+n),old)
        journal={'id':ident,'status':'applying','from':plan['from'],'to':plan['to'],'entries':entries}
        recovery={}
        engine=Path(__file__).absolute()
        compiled=engine.with_suffix('.pyc')
        for source in [compiled if compiled.is_file() else engine]:
            if source.is_file():
                data=source.read_bytes();name='product_update.pyc' if source.suffix=='.pyc' else 'product_update.recovery'
                relative='recovery/'+name
                atomic_write(safe_path(backup,relative),data);recovery[name]=digest(data)
        journal['recovery']=recovery
        check_idle();validate_current(plan)
        atomic_write(pending_path(root),json.dumps(journal,ensure_ascii=False).encode('utf-8'))
        try:
            check_idle()
            guards=['启动CUTROOM.bat','workbench/start-local.ps1','scripts/cutroom_mcp.py']
            ordered=sorted(plan['changes'],key=lambda n:(0 if n.endswith(('.pyc','.js')) else 1 if n in guards else 2,n))
            for n in ordered:
                b=plan['changes'][n]
                if n!='release-manifest.json':atomic_write(safe_path(root,n),b)
            for n in plan['removed']:safe_path(root,n).unlink()
            for n,b in plan['changes'].items():
                if n!='release-manifest.json' and safe_path(root,n).read_bytes()!=b:fail('VERIFY','更新后文件校验失败。')
            atomic_write(safe_path(root,'release-manifest.json'),plan['changes']['release-manifest.json'])
            journal['status']='completed'
            atomic_write(backup/'receipt.json',json.dumps(journal,ensure_ascii=False,indent=2).encode('utf-8'))
            pending_path(root).unlink()
        except Exception as error:
            restore_journal(root,journal)
            raise UpdateError('ROLLED_BACK','升级未完成，已恢复原程序；请导出报错日志并联系维护人员。') from error
    return {'status':'completed','version':plan['to'],'backup':str(backup)}

def main(argv=None):
    p=argparse.ArgumentParser(description='CUTROOM 产品更新升级')
    p.add_argument('--root',type=Path,required=True);p.add_argument('--zip',type=Path)
    p.add_argument('--check',action='store_true');p.add_argument('--recover',action='store_true')
    p.add_argument('--online',action='store_true')
    args=p.parse_args(argv)
    try:
        if args.recover:
            print(json.dumps(recover_update(args.root),ensure_ascii=False));return 0
        if args.online:
            from public_updates import download_update
            args.zip=download_update(args.root,read_json(safe_path(args.root,'release-manifest.json'))['version'])
            if args.zip is None:print('当前已是公开通道最新可用版本。');return 0
        if not args.zip:fail('ZIP','请先选择新版 Windows ZIP。')
        plan=prepare_update(args.root,args.zip)
        print('当前版本：'+plan['from']+' → 新版本：'+plan['to'],flush=True)
        if args.check:print('安装包检查通过；尚未更新。');return 0
        assert_idle(plan['root'])
        if input('任务和配置保留，程序会原位更新。输入 Y 开始，其他键取消：').strip().lower()!='y':
            print('已取消，原程序未改动。');return 0
        result=apply_update(plan)
        print('升级完成：'+result['version']+'。请从原来的启动入口打开，并在 WorkBuddy 重连一次 MCP。')
        print('程序备份：'+result['backup']);return 0
    except (UpdateError,OSError,ValueError,KeyError,zipfile.BadZipFile) as e:
        code=getattr(e,'code','UPDATE_FAILED')
        print('升级未完成 ['+code+']：'+(str(e) if isinstance(e,UpdateError) else '文件不可读或安装包格式有误，请导出报错日志并联系维护人员。'))
        return 1
if __name__=='__main__':raise SystemExit(main())
