"""Local task protection and reversible cache cleanup. No model calls or gate changes."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from contextlib import closing
from types import SimpleNamespace
import threading
import uuid

from .backup import backup_database
from .voice_editing import _digest as _voice_directory_digest
from .cache import cache_dir, clean_cache, _cache_entry_path, _is_reparse_path, ALL_KINDS

LOCK = threading.RLock()
MAX_FILE = 64 * 1024 * 1024
MAX_SNAPSHOT = 512 * 1024 * 1024
CACHE_SUFFIXES = {'thumbnail':{'.jpg','.jpeg','.png','.webp'},'contact_sheet':{'.jpg','.jpeg','.png','.webp'},'proxy':{'.mp4','.webm'},'audio_preview':{'.wav','.mp3','.m4a','.ogg'}}
SUFFIXES = {'.json', '.txt', '.srt', '.ass', '.vtt', '.mp3', '.wav', '.m4a'}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _plain(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if _is_reparse_path(part):
            raise ValueError('STORAGE_PATH_UNSAFE: 文件夹经过链接或重定向，请先核对保存位置。')
    return path


def protection_root(db, root):
    base = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local/share')))
    identity = hashlib.sha256(str(Path(db.path).resolve()).casefold().encode()).hexdigest()[:20]
    return _plain(base / 'CUTROOM' / 'task-protection' / identity)


def _json(path, default=None):
    if not path.exists():
        return {} if default is None else default
    _plain(path)
    if path.stat().st_size > MAX_FILE:
        raise ValueError('STORAGE_RECORD_INVALID: 保护记录过大，请联系维护人员。')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError) as error:
        raise ValueError('STORAGE_RECORD_INVALID: 保护记录无法读取，请保留目录并联系维护人员。') from error


def _write(path, value):
    _plain(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def _digest(path):
    _plain(path)
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def require_database_startup(db_path, root):
    """Do not interpret loss of a previously used database as a fresh install."""
    path = _plain(Path(db_path).resolve())
    if path.is_file() and path.stat().st_size > 0:
        return
    home = protection_root(SimpleNamespace(path=path), root)
    history = (home / 'initialized.json').exists() or (home / 'manifest.json').exists()
    history = history or any((home / 'history').glob('*.json'))
    history = history or any((home / 'database').glob('catalog-*.sqlite'))
    history = history or any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm'))
    if history:
        raise RuntimeError('STORAGE_DATABASE_MISSING: 原任务数据库缺失，已停止启动和备份轮换。请保留保护目录，使用数据库恢复工具恢复后重新启动。')


def record_database_initialized(db_path, root):
    path = Path(db_path).resolve()
    home = protection_root(SimpleNamespace(path=path), root)
    _write(home / 'initialized.json', {'database': str(path), 'initializedAt': _now()})


def _last_nonempty_snapshot(home, manifest, task_count):
    """Keep one verified nonempty snapshot even across legitimate empty backups."""
    anchor = home / 'last-nonempty.json'
    if task_count:
        _write(anchor, manifest)
        return manifest
    if anchor.exists():
        saved = _json(anchor)
        path = _plain(Path(saved['databaseBackup']))
        if path.parent.resolve() != (home / 'database').resolve() or not path.is_file():
            raise ValueError('STORAGE_BACKUP_INVALID: 非空任务备份缺失，已停止轮换，请保留保护目录。')
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as check:
                valid = check.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
                nonempty = check.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] > 0
        except sqlite3.Error as error:
            raise ValueError('STORAGE_BACKUP_INVALID: 非空任务备份损坏，已停止轮换，请保留保护目录。') from error
        if not valid or not nonempty:
            raise ValueError('STORAGE_BACKUP_INVALID: 非空任务备份校验失败，已停止轮换，请保留保护目录。')
        return saved
    # Upgrade existing protection history, created before the anchor was introduced.
    for item in sorted((home / 'history').glob('*.json'), reverse=True):
        saved = _json(item)
        path = _plain(Path(saved['databaseBackup']))
        if path.parent.resolve() != (home / 'database').resolve() or not path.is_file():
            raise ValueError('STORAGE_BACKUP_INVALID: 历史备份不可用，已停止轮换。')
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as check:
            if check.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('STORAGE_BACKUP_INVALID: 历史备份校验失败，已停止轮换。')
            if check.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]:
                _write(anchor, saved)
                return saved
    return {}


def _tasks(db):
    result = {}
    for row in db.connection.execute('SELECT task_id,title,brief_json FROM tasks'):
        try:
            brief = json.loads(row['brief_json'] or '{}')
        except ValueError:
            brief = {}
        result[row['task_id']] = {'title':row['title'], 'brief':brief,
            'binding':hashlib.sha256(json.dumps(brief,sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
    return result


def require_idle(db):
    if db.connection.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','pending') LIMIT 1").fetchone():
        raise ValueError('STORAGE_TASK_BUSY: 任务正在处理或排队，请结束处理后再清理、备份或恢复。')


def _protected_path(root, relative, tasks):
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or ':' in relative or '\\' in relative:
        raise ValueError('STORAGE_PATH_UNSAFE: 保护文件路径无效。')
    parts = rel.parts
    task_id = None
    if len(parts) >= 6 and parts[:3] == ('runtime','workbench','tasks') and parts[4] in {'inputs','artifacts','history'}:
        task_id = parts[3]
    elif len(parts) >= 3 and parts[0] == 'out':
        task_id = parts[1]
    elif len(parts) >= 4 and parts[:2] == ('runtime','voice'):
        task_id = next((key for key in tasks if parts[2] in {key,_voice_directory_digest(key)[:20]}),None)
    if task_id not in tasks or rel.suffix.lower() not in SUFFIXES:
        raise ValueError('STORAGE_PATH_UNSAFE: 此文件不属于可恢复的任务资料。')
    path = _plain(Path(root) / rel)
    if not path.is_relative_to(Path(root).absolute()):
        raise ValueError('STORAGE_PATH_UNSAFE: 文件超出任务保存目录。')
    return path, task_id


def _walk(directory):
    if not directory.is_dir() or _is_reparse_path(directory):
        return
    for parent, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = [name for name in dirs if not _is_reparse_path(Path(parent)/name)]
        for name in files:
            path = Path(parent)/name
            if not _is_reparse_path(path):
                yield path


def create_backup(db, root):
    with LOCK:
        require_idle(db)
        root = _plain(root); home = _plain(protection_root(db,root));home.mkdir(parents=True,exist_ok=True)
        old = _json(home/'manifest.json'); tasks = _tasks(db)
        # Keep references to absent files; a later backup must not erase recovery evidence.
        records = dict(old.get('files', {})); skipped = []; total = 0
        for task_id in tasks:
            if Path(task_id).name != task_id or task_id in {'.','..'}:
                continue
            folders = [root/'runtime/workbench/tasks'/task_id,root/'runtime/voice'/task_id,root/'runtime/voice'/_voice_directory_digest(task_id)[:20],root/'out'/task_id]
            for folder in folders:
                for path in _walk(folder):
                    if path.suffix.lower() not in SUFFIXES:
                        continue
                    relative = path.relative_to(root).as_posix()
                    try:
                        path, owner = _protected_path(root,relative,tasks)
                        before = path.stat()
                        if before.st_size > MAX_FILE or total + before.st_size > MAX_SNAPSHOT:
                            skipped.append(relative);continue
                        total += before.st_size
                        digest = _digest(path); target = _plain(home/'objects'/digest)
                        target.parent.mkdir(parents=True,exist_ok=True)
                        if not target.exists() or _digest(target) != digest:
                            temp = target.with_suffix('.tmp');shutil.copyfile(path,temp)
                            if _digest(temp) != digest:temp.unlink();raise ValueError('文件正在变化')
                            os.replace(temp,target)
                        after = path.stat()
                        if (before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns):
                            skipped.append(relative);continue
                        records[relative] = {'sha256':digest,'bytes':before.st_size,'taskId':owner,'binding':tasks[owner]['binding'],'mtimeNs':before.st_mtime_ns}
                    except (OSError,ValueError):
                        skipped.append(relative)
        db_backup = backup_database(db.path,home/'database')
        with closing(sqlite3.connect(db_backup.resolve().as_uri() + '?mode=ro', uri=True)) as snapshot_db:
            snapshot_tasks = snapshot_db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
        manifest = {'updatedAt':_now(),'projectRoot':str(root),'database':str(Path(db.path).resolve()),
                    'databaseBackup':str(db_backup),'files':records,'skipped':skipped,'backedBytes':total,'taskCount':snapshot_tasks}
        history = home/'history';history.mkdir(exist_ok=True)
        name = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')+'.json'
        _write(history/name,manifest);_write(home/'manifest.json',manifest)
        # Only our generated snapshots are rotated; missing file references survive in the latest manifest.
        histories = sorted(history.glob('*.json'),reverse=True)
        retained = histories[:3]
        snapshots = [_json(h) for h in retained]
        anchor = _last_nonempty_snapshot(home, manifest, snapshot_tasks)
        if anchor:
            snapshots.append(anchor)
        references = {r['sha256'] for snapshot in snapshots for r in snapshot.get('files',{}).values()}
        db_refs = {Path(snapshot['databaseBackup']).name for snapshot in snapshots}
        for h in histories[3:]:_plain(h).unlink()
        for blob in (home/'objects').glob('*'):
            if re.fullmatch('[a-f0-9]{64}',blob.name) and blob.name not in references:_plain(blob).unlink()
        for backup in (home/'database').glob('catalog-*.sqlite'):
            if backup.name not in db_refs:_plain(backup).unlink()
        if (home/'last-error.json').exists():_plain(home/'last-error.json').unlink()
        return {'updatedAt':manifest['updatedAt'],'fileCount':len(records),'skipped':skipped,'directory':str(home)}


def _recoverable(home, record):
    digest = record.get('sha256','')
    if not re.fullmatch('[a-f0-9]{64}',digest):return False
    try:
        blob = _plain(home/'objects'/digest)
        return blob.is_file() and blob.stat().st_size == record['bytes'] and _digest(blob) == digest
    except (OSError,ValueError,KeyError):return False


def _label(path):
    names={'final-script.txt':'已确认文案','transcription.json':'口播识别记录','transcript.words.json':'字幕时间轴','transcript-review.json':'口播校对记录','transcript.srt':'字幕文件'}
    return names.get(path.name, '字幕文件' if path.suffix in {'.srt','.ass','.vtt'} else '任务记录（'+path.name+'）')


def inspect_storage(db, root):
    with LOCK:
        home = _plain(protection_root(db,root));manifest = _json(home/'manifest.json');tasks = _tasks(db);issues=[]
        bound = manifest.get('projectRoot') == str(Path(root).absolute()) and manifest.get('database') == str(Path(db.path).resolve())
        files = manifest.get('files',{}) if bound else {}
        for relative,record in files.items():
            try:path,task_id = _protected_path(root,relative,tasks)
            except ValueError:continue
            if path.exists():continue
            compatible = record.get('binding') == tasks[task_id]['binding']
            recoverable = compatible and _recoverable(home,record)
            issues.append({'taskId':task_id,'title':tasks[task_id]['title'],'path':str(path),'label':_label(path),
                'recoverable':recoverable,'reason':'可从本机备份补回。' if recoverable else '当前版本与备份不一致或备份不可用，请进入任务核对。'})
        seen = {i['path'] for i in issues}
        for task_id,task in tasks.items():
            expected=[]
            if task['brief'].get('final_script'):expected.append((Path(root)/'runtime/workbench/tasks'/task_id/'inputs/final-script.txt','文案文件缺失，请进入任务恢复或重新填写。'))
            source=task['brief'].get('digital_human_source',{}).get('path')
            if source:expected.append((Path(source),'原口播视频找不到，请连接素材硬盘或在任务设置中重新关联。'))
            for path,reason in expected:
                if not path.exists() and str(path) not in seen:
                    issues.append({'taskId':task_id,'title':task['title'],'path':str(path),'label':_label(path),'recoverable':False,'reason':reason});seen.add(str(path))
        batches=[]
        for path in sorted((home/'cache-recovery').glob('*/manifest.json'),reverse=True):
            batch=_json(path)
            entries=batch.get('entries',[])
            if entries:batches.append({'batchId':path.parent.name,'createdAt':batch.get('createdAt'),'files':len(entries),'bytes':sum(x['size_bytes'] for x in entries)})
        cache_report=clean_cache(db,dry_run=True)
        cached=[i for i in cache_report['deleted'] if _cache_entry_path(cache_dir(db),i['rel_path']).is_file()]
        cache_status={'available':len(cached),'missing':len(cache_report['deleted'])-len(cached),'bytes':sum(i['size_bytes'] for i in cached)}
        busy=bool(db.connection.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','pending') LIMIT 1").fetchone())
        return {'directory':str(home),'updatedAt':manifest.get('updatedAt'),'fileCount':len(files),'skipped':manifest.get('skipped',[]),
                'issues':issues,'batches':batches,'busy':busy,'cacheStatus':cache_status,'databaseBackup':manifest.get('databaseBackup'),'backupError':_json(home/'last-error.json'),
                'note':'自动保护文案、字幕时间轴、编辑数据库和托管任务小文件；不备份原视频、成片或整个素材库。单文件超过64MB或单次超过512MB会列为未备份。'}


def restore_missing(db, root):
    with LOCK, db.transaction(immediate=True):
        require_idle(db)
        home = _plain(protection_root(db,root));manifest = _json(home/'manifest.json');tasks = _tasks(db);restored=0
        if manifest.get('projectRoot') != str(Path(root).absolute()) or manifest.get('database') != str(Path(db.path).resolve()):
            raise ValueError('STORAGE_BACKUP_MISMATCH: 备份不属于当前任务目录，不能自动恢复。')
        for relative,record in manifest.get('files',{}).items():
            if record.get('taskId') not in tasks:continue
            path,task_id = _protected_path(root,relative,tasks)
            if path.exists() or record.get('binding') != tasks[task_id]['binding'] or not _recoverable(home,record):continue
            path.parent.mkdir(parents=True,exist_ok=True);_plain(path)
            # Exclusive create never overwrites an edit that arrived after inspection.
            try:
                with path.open('xb') as out, (home/'objects'/record['sha256']).open('rb') as source:shutil.copyfileobj(source,out)
            except FileExistsError:continue
            except OSError:
                path.unlink(missing_ok=True)
                raise
            if record.get('mtimeNs'):os.utime(path,ns=(record['mtimeNs'],record['mtimeNs']))
            restored+=1
        return {'restored':restored}


def preview_cleanup(db, root, plans):
    with LOCK:
        require_idle(db)
        result=clean_cache(db,dry_run=True)
        # The web cleaner admits only known regenerable kinds.
        entries=[];missing=0
        for item in result['deleted']:
            if item['kind'] not in ALL_KINDS or Path(item['rel_path']).suffix.lower() not in CACHE_SUFFIXES[item['kind']]:continue
            path=_cache_entry_path(cache_dir(db),item['rel_path'])
            if not path.is_file():missing+=1;continue
            entries.append({**item,'sha256':_digest(path)})
        return {'deleted':entries,'missingCount':missing,'failed':result['failed'],'freedBytes':0,'recoverableBytes':sum(x['size_bytes'] for x in entries),
                'impactToken':plans.create(entries),'message':'这些缓存会先移入可恢复区，原素材、字幕和编辑方案保留；永久清空后才释放空间。'}


def _asset_signature(db,asset_id):
    row=db.connection.execute('SELECT * FROM assets WHERE asset_id=?',(asset_id,)).fetchone()
    return hashlib.sha256(json.dumps(dict(row) if row else {},sort_keys=True,default=str).encode()).hexdigest()


def _batch(home,batch_id):
    if not isinstance(batch_id,str) or not re.fullmatch('[a-f0-9]{32}',batch_id):raise ValueError('STORAGE_BATCH_INVALID: 清理记录无效，请刷新。')
    return _plain(home/'cache-recovery'/batch_id)


def quarantine_cache(db, root, plans, token):
    with LOCK:
        require_idle(db)
        # Preserve editing data before any cache is moved; backup failure stops cleanup.
        create_backup(db,root)
        expected=plans.consume(token);home=protection_root(db,root);batch_id=uuid.uuid4().hex;folder=_batch(home,batch_id)
        moved=[];entries=[]
        try:
            with db.transaction(immediate=True):
                require_idle(db)
                for item in expected:
                    row=db.connection.execute('SELECT * FROM preview_cache WHERE asset_id=? AND kind=?',(item['asset_id'],item['kind'])).fetchone()
                    path=_cache_entry_path(cache_dir(db),item['rel_path'])
                    size=path.stat().st_size if path.exists() else 0
                    if item['kind'] not in ALL_KINDS or Path(item['rel_path']).suffix.lower() not in CACHE_SUFFIXES[item['kind']] or (_digest(path) if path.is_file() else None)!=item.get('sha256') or row is None or row['rel_path']!=item['rel_path'] or row['generated_at']!=item.get('generated_at') or size!=item['size_bytes']:
                        raise ValueError('CACHE_PREVIEW_STALE: 缓存已变化，请重新查看清理范围。')
                    entries.append({**dict(row),'recordedSizeBytes':row['size_bytes'],'size_bytes':size,'sha256':_digest(path) if path.exists() else None,'assetSignature':_asset_signature(db,item['asset_id']), 'stored':str(len(entries))})
                folder.mkdir(parents=True)
                record={'createdAt':_now(),'cacheRoot':str(cache_dir(db).absolute()),'entries':entries}
                # Write recovery evidence first so a process interruption still leaves a route back.
                _write(folder/'manifest.json',record)
                for entry in entries:
                    source=_cache_entry_path(cache_dir(db),entry['rel_path']);target=_plain(folder/entry['stored'])
                    if source.exists():
                        # copy + fsync supports separate disks; remove only after verified copy.
                        with source.open('rb') as data,target.open('xb') as output:
                            shutil.copyfileobj(data,output);output.flush();os.fsync(output.fileno())
                        if _digest(target)!=entry['sha256'] or _digest(source)!=entry['sha256']:raise ValueError('CACHE_PREVIEW_STALE: 缓存内容变化，已停止。')
                        source.unlink();moved.append((source,target))
                    db.connection.execute('DELETE FROM preview_cache WHERE asset_id=? AND kind=?',(entry['asset_id'],entry['kind']))
        except Exception:
            for source,target in reversed(moved):
                if not source.exists():shutil.copyfile(target,source)
            raise
        return {'batchId':batch_id,'moved':len(entries),'freedBytes':0,'recoverableBytes':sum(x['size_bytes'] for x in entries)}


def restore_cache(db, root, batch_id):
    with LOCK, db.transaction(immediate=True):
        require_idle(db);folder=_batch(protection_root(db,root),batch_id);record=_json(folder/'manifest.json');remaining=[];restored=0
        if record.get('cacheRoot')!=str(cache_dir(db).absolute()):raise ValueError('STORAGE_BACKUP_MISMATCH: 缓存备份不属于当前工作台。')
        for entry in record.get('entries',[]):
            target=_cache_entry_path(cache_dir(db),entry['rel_path'])
            stored=entry.get('stored','')
            if not re.fullmatch(r'\d+',stored):raise ValueError('STORAGE_BATCH_INVALID: 缓存记录无效。')
            source=_plain(folder/stored)
            existing=db.connection.execute('SELECT * FROM preview_cache WHERE asset_id=? AND kind=?',(entry['asset_id'],entry['kind'])).fetchone()
            registration_matches=existing is None or (all(existing[key]==entry[key] for key in ('rel_path','generated_at')) and existing['size_bytes']==entry.get('recordedSizeBytes',entry['size_bytes']))
            if target.exists() or not registration_matches or not source.is_file() or _asset_signature(db,entry['asset_id'])!=entry['assetSignature'] or _digest(source)!=entry['sha256']:
                remaining.append(entry);continue
            target.parent.mkdir(parents=True,exist_ok=True)
            try:
                with target.open('xb') as out,source.open('rb') as data:
                    shutil.copyfileobj(data,out);out.flush();os.fsync(out.fileno())
            except FileExistsError:
                remaining.append(entry);continue
            except OSError:
                target.unlink(missing_ok=True);raise
            if existing is None:
                db.connection.execute('INSERT INTO preview_cache(asset_id,kind,rel_path,size_bytes,generated_at) VALUES(?,?,?,?,?)',tuple(entry[k] for k in ['asset_id','kind','rel_path','size_bytes','generated_at']))
            restored+=1
        # Keep original recovery copies until explicitly purged; mark restored rows for UI.
        record['restoredAt']=_now();_write(folder/'manifest.json',record)
        return {'restored':restored,'skipped':len(remaining)}


def purge_cache(db, root, batch_id, confirmed):
    if confirmed is not True:raise ValueError('STORAGE_CONFIRM_REQUIRED: 请确认永久清空这批缓存。')
    with LOCK, db.transaction(immediate=True):
        require_idle(db);folder=_batch(protection_root(db,root),batch_id);record=_json(folder/'manifest.json');freed=0
        if record.get('cacheRoot')!=str(cache_dir(db).absolute()):raise ValueError('STORAGE_BACKUP_MISMATCH: 缓存备份不属于当前工作台。')
        for entry in record.get('entries',[]):
            stored=entry.get('stored','')
            if not re.fullmatch(r'\d+',stored):raise ValueError('STORAGE_BATCH_INVALID: 缓存记录无效。')
            path=_plain(folder/stored)
            if path.is_file():freed+=path.stat().st_size;path.unlink()
        record['entries']=[];record['purgedAt']=_now();_write(folder/'manifest.json',record)
        return {'freedBytes':freed}


def dispatch_storage(db,root,plans,method,path,body):
    prefix='/api/settings/storage/'
    if not path.startswith(prefix):return None
    action=path[len(prefix):]
    if action=='protection' and method=='GET':return inspect_storage(db,root)
    if method!='POST':return None
    if action=='protection/backup':return create_backup(db,root)
    if action=='protection/restore':return restore_missing(db,root)
    if action=='cache/preview':return preview_cleanup(db,root,plans)
    if action=='cache/clean':return quarantine_cache(db,root,plans,body.get('impactToken',''))
    if action=='cache/restore':return restore_cache(db,root,body.get('batchId'))
    if action=='cache/purge':return purge_cache(db,root,body.get('batchId'),body.get('confirmed'))
    return None


def run_protection(stop_event,db_path,root,on_error):
    from .database import CatalogDatabase
    while not stop_event.is_set():
        try:
            if not Path(db_path).is_file():
                raise ValueError('STORAGE_DATABASE_MISSING: 任务数据库缺失，请保留保护目录并联系维护人员恢复。')
            with CatalogDatabase.open(db_path) as db:
                if not db.connection.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','pending') LIMIT 1").fetchone():create_backup(db,root)
        except Exception as error:
            on_error(error)
            try:
                class Location: path=Path(db_path)
                _write(protection_root(Location(),root)/'last-error.json',{'at':_now(),'message':'自动备份未完成，请检查保护目录的磁盘空间和写入权限，然后点击立即备份。原任务文件未删除。'})
            except (OSError,ValueError):pass
        stop_event.wait(300)
