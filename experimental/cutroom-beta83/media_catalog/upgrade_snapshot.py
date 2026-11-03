"""Maintenance snapshots with exclusive writes; never migrate or start services.

Completion requires the real read-only reference inspector to agree before and
after copying. Restore preserves recorded absolute addresses and old root; it
does not grant business approvals, launch jobs, or authorize switching service.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import sqlite3
from urllib.parse import quote

from .path_utils import filesystem_path
from .upgrade_recovery import (RecoveryPlanError, _fail, _logical_digest,
                               _plain_path, build_recovery_plan)
from .upgrade_frozen_references import _observe, _signature


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf8')


def _relative(value):
    if (not isinstance(value, str) or not value or '\\' in value or '\0' in value
            or ':' in value or PureWindowsPath(value).drive
            or PurePosixPath(value).is_absolute()
            or any(p in ('', '.', '..') for p in value.split('/'))
            or any(p.lower() in {'.env', 'session-signing.key'} for p in value.split('/'))):
        _fail('SNAPSHOT_PATH_INVALID', '快照内地址不是安全的相对文件地址。')
    return Path(*value.split('/'))


def _independent(left, right):
    if left == right or left.is_relative_to(right) or right.is_relative_to(left):
        _fail('DESTINATION_OVERLAP', '源安装、快照和恢复目录必须独立，不能互相嵌套。')


def _ro(path):
    path = _plain_path(path)
    connection = sqlite3.connect('file:' + quote(str(filesystem_path(path)), safe='/\\:') + '?mode=ro', uri=True, timeout=5)
    connection.execute('PRAGMA query_only=ON')
    return connection


def _write_exclusive(path, data):
    path = _plain_path(path)
    filesystem_path(path.parent).mkdir(parents=True, exist_ok=True)
    _plain_path(path)
    with filesystem_path(path).open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _plain_path(path)


def _copy_exclusive(source, target, expected):
    expected = _signature(expected)
    source, target = _plain_path(source), _plain_path(target)
    if _observe(source) != expected:
        _fail('SOURCE_CHANGED', '原文件在盘点后变化，不能写完整快照。')
    filesystem_path(target.parent).mkdir(parents=True, exist_ok=True)
    _plain_path(target)
    digest = hashlib.sha256()
    count = 0
    with filesystem_path(source).open('rb') as before, filesystem_path(target).open('xb') as after:
        for chunk in iter(lambda: before.read(1024 * 1024), b''):
            digest.update(chunk)
            count += len(chunk)
            after.write(chunk)
        after.flush()
        os.fsync(after.fileno())
    _plain_path(source)
    _plain_path(target)
    if ({'size_bytes': count, 'sha256': digest.hexdigest()} != expected
            or _observe(source) != expected or _observe(target) != expected):
        _fail('SOURCE_CHANGED', '文件在复制窗口变化，未生成完成标记。')


def _complete_inputs(plan):
    if (not isinstance(plan, dict) or plan.get('formatVersion') != 1
            or plan.get('externalReferencesAuditComplete') is not True
            or not isinstance(plan.get('frozenReferences'), dict)
            or plan['frozenReferences'].get('auditComplete') is not True
            or plan.get('frozenReferences', {}).get('unresolved')):
        _fail('REFERENCE_AUDIT_INCOMPLETE', '引用盘点尚有未支持或缺失项，不能执行完整快照。')


def _file_rows(plan):
    root = _plain_path(plan['sourceRoot'])
    rows = {}
    installed = {item['path']:item for item in (plan['frozenReferences'].get('installedRelease') or {}).get('files',[])}
    for item in plan['managedFiles']:
        relative = _relative(item['relativePath'])
        path = _plain_path(root / relative)
        if not path.is_relative_to(root):
            _fail('SNAPSHOT_PATH_INVALID', '托管文件逃出原安装目录。')
        rows[str(path)] = {'originalPath': str(path), 'restoreRelativePath': relative.as_posix(),
                           'signature': _signature({'size_bytes': item['bytes'], 'sha256': item['sha256']})}
    for item in plan['frozenReferences']['files']:
        path = _plain_path(item['path'])
        signature = _signature({'size_bytes': item['bytes'], 'sha256': item['sha256']})
        if str(path) in installed and signature != installed[str(path)]['signature']:
            _fail('SNAPSHOT_CHANGED', '安装文件分类与原始字节签名不一致。')
        row = {'originalPath': str(path), 'restoreRelativePath': path.relative_to(root).as_posix() if path.is_relative_to(root) and str(path) not in installed else None,
               'signature': signature}
        if str(path) in rows and rows[str(path)]['signature'] != signature:
            _fail('SOURCE_CHANGED', '同一原地址的托管与冻结签名不一致。')
        rows[str(path)] = row
    return [dict(rows[k], snapshotRelativePath='files/%08d.bin' % i)
            for i, k in enumerate(sorted(rows))]


def _directory_rows(plan):
    """Only signed transcript-recovery storyboard directories are restorable."""
    root = _plain_path(plan['sourceRoot'])
    workflow = plan['frozenReferences'].get('adapters', {}).get('workflow', {})
    references = workflow.get('managedDirectoryReferences', [])
    if not isinstance(references, list):
        _fail('SNAPSHOT_PATH_INVALID', '恢复目录清单无效。')
    task_ids = {row['taskId'] for row in plan.get('tasks', [])}
    rows = {}
    for ref in references:
        if (not isinstance(ref, dict)
                or set(ref) != {'table', 'rowId', 'field', 'path', 'evidenceGrade'}
                or ref['table'] != 'job_payloads'
                or ref['evidenceGrade'] != 'historical_recovery_bytes'
                or not isinstance(ref['rowId'], str) or not ref['rowId']
                or not isinstance(ref['path'], str) or not Path(ref['path']).is_absolute()):
            _fail('SNAPSHOT_PATH_INVALID', '恢复目录缺少已核验的任务归属。')
        path = _plain_path(ref['path'])
        if not path.is_relative_to(root) or str(path) != ref['path']:
            _fail('SNAPSHOT_PATH_INVALID', '恢复目录越过原安装范围。')
        relative = _relative(path.relative_to(root).as_posix())
        parts = relative.parts
        token = hashlib.sha256(ref['rowId'].encode('utf8')).hexdigest()[:24]
        if (len(parts) < 6 or parts[:3] != ('runtime', 'workbench', 'tasks')
                or parts[3] not in task_ids):
            _fail('SNAPSHOT_PATH_INVALID', '恢复目录不属于已登记任务。')
        scoped = parts[4:]
        # Rework UUIDs are accepted only from the freshly rebuilt workflow
        # inventory, which verifies their applied request and signed old inputs.
        rework_storyboard = (len(scoped) >= 5 and scoped[0] == 'history'
                            and scoped[1] in {'script-revisions', 'source-revisions'}
                            and len(scoped[2]) == 32
                            and all(c in '0123456789abcdef' for c in scoped[2])
                            and scoped[3:5] == ('artifacts', 'storyboard'))
        allowed = (rework_storyboard or scoped[:2] == ('artifacts', 'storyboard')
                   or scoped[:4] == ('history', 'transcript-recoveries', token, 'storyboard')
                   or scoped[:5] == ('history', 'transcript-recoveries', token, 'storyboard-extension', 'storyboard'))
        if (not allowed or ref['field'] != 'transcript_recovery/' + '/'.join(scoped)):
            _fail('SNAPSHOT_PATH_INVALID', '只能恢复已核验的转写分镜历史目录。')
        rows[str(path)] = {'originalPath': str(path), 'restoreRelativePath': relative.as_posix()}
    return [rows[key] for key in sorted(rows)]


def _verify_directories(rows):
    for row in rows:
        path = _plain_path(row['originalPath'])
        if not filesystem_path(path).is_dir():
            _fail('ORIGINAL_REFERENCE_CHANGED', '登记的恢复目录已缺失或变为文件。')


def create_upgrade_snapshot(plan, snapshot_root, *, source_stopped=False, _after_file=None):
    """Operator must stop the identified source service; no process is stopped here.

    `_after_file` is a local fault-injection seam, never exposed by the CLI.
    Incomplete data is retained for diagnosis without a completion marker.
    """
    if source_stopped is not True:
        _fail('SOURCE_STOP_REQUIRED', '需维护人员先停止已核明归属的源服务，再声明停机窗口。')
    _complete_inputs(plan)
    source = _plain_path(plan['sourceRoot'])
    destination = _plain_path(plan['destinationRoot'])
    snapshot = _plain_path(snapshot_root)
    _independent(source, destination)
    _independent(source, snapshot)
    _independent(destination, snapshot)
    if filesystem_path(snapshot).exists():
        _fail('SNAPSHOT_EXISTS', '快照目录已存在，已保留；请指定新的独立目录。')
    database = source / _relative(plan['databaseRelativePath'])
    current = build_recovery_plan(source, database, destination)
    _complete_inputs(current)
    if _json(current) != _json(plan):
        _fail('SOURCE_CHANGED', '源账本、文件或引用在计划后变化，请重新盘点。')
    files = _file_rows(current)
    directories = _directory_rows(current)
    _verify_directories(directories)
    try:
        filesystem_path(snapshot).mkdir(parents=True, exist_ok=False)
        _plain_path(snapshot)
        # Reserve a unique empty destination before sqlite creates a backup.
        backup = snapshot / 'catalog.sqlite'
        _write_exclusive(backup, b'')
        with closing(_ro(database)) as src, closing(sqlite3.connect(filesystem_path(backup))) as dst:
            src.execute('BEGIN')
            if _logical_digest(src) != current['databaseLogicalSha256']:
                _fail('SOURCE_CHANGED', '账本在快照复制前变化。')
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or dst.execute('PRAGMA foreign_key_check').fetchone() is not None:
                _fail('SNAPSHOT_DATABASE_INVALID', '账本副本完整性检查失败。')
            if _logical_digest(dst) != current['databaseLogicalSha256']:
                _fail('SOURCE_CHANGED', '账本副本与盘点窗口不一致。')
        for row in files:
            _copy_exclusive(Path(row['originalPath']), snapshot / row['snapshotRelativePath'], row['signature'])
            if _after_file:
                _after_file(row)
        # Fresh connection/transaction is essential: an old WAL read snapshot
        # cannot detect a concurrent commit hidden from its own view.
        with closing(_ro(database)) as connection:
            if _logical_digest(connection) != current['databaseLogicalSha256']:
                _fail('SOURCE_CHANGED', '原账本在复制期间变化，未生成完成标记。')
        after = build_recovery_plan(source, database, destination)
        if _json(after) != _json(current):
            _fail('SOURCE_CHANGED', '源文件树或引用在复制期间变化，未生成完成标记。')
        for row in files:
            if _observe(Path(row['originalPath'])) != row['signature']:
                _fail('SOURCE_CHANGED', '原文件在完成快照前变化。')
        _verify_directories(directories)
        manifest = {'schema': 'cutroom.upgrade-snapshot/v1', 'sourceRoot': str(source),
                    'databaseRelativePath': current['databaseRelativePath'],
                    'databaseLogicalSha256': current['databaseLogicalSha256'],
                    'schemaVersion': current['schemaVersion'], 'databaseSignature': _observe(backup),
                    'files': files, 'directories': directories, 'plan': current, 'oldRootMustBePreserved': True}
        manifest_path = snapshot / 'snapshot-manifest.json'
        _write_exclusive(manifest_path, _json(manifest))
        receipt = {'schema': 'cutroom.upgrade-snapshot-complete/v1', 'manifestSignature': _observe(manifest_path)}
        _write_exclusive(snapshot / 'snapshot-complete.json', _json(receipt))
        return {'snapshotComplete': True, 'snapshotRoot': str(snapshot), 'fileCount': len(files),
                'databaseLogicalSha256': current['databaseLogicalSha256'],
                'oldRootMustBePreserved': True, 'canSwitchInstallation': False}
    except RecoveryPlanError:
        raise
    except (OSError, sqlite3.Error):
        _fail('SNAPSHOT_WRITE_FAILED', '快照写入未完成；原安装保留，残留目录仅供诊断，不可用于恢复。')


def _read_snapshot(snapshot):
    receipt_path, manifest_path = snapshot / 'snapshot-complete.json', snapshot / 'snapshot-manifest.json'
    try:
        for path in (receipt_path, manifest_path):
            _plain_path(path)
        receipt = json.loads(filesystem_path(receipt_path).read_text(encoding='utf8'))
        if not isinstance(receipt, dict) or receipt.get('schema') != 'cutroom.upgrade-snapshot-complete/v1':
            _fail('SNAPSHOT_INCOMPLETE', '快照完成回执版本不受支持。')
        if _observe(manifest_path) != _signature(receipt.get('manifestSignature')):
            _fail('SNAPSHOT_CHANGED', '快照清单已变化，不能恢复。')
        manifest = json.loads(filesystem_path(manifest_path).read_text(encoding='utf8'))
        if (not isinstance(manifest, dict) or manifest.get('schema') != 'cutroom.upgrade-snapshot/v1'
                or manifest.get('oldRootMustBePreserved') is not True):
            _fail('SNAPSHOT_INCOMPLETE', '快照版本或原地址保留约束不受支持。')
        _complete_inputs(manifest.get('plan'))
        plan = manifest['plan']
        if (manifest.get('sourceRoot') != plan.get('sourceRoot')
                or manifest.get('databaseRelativePath') != plan.get('databaseRelativePath')
                or manifest.get('databaseLogicalSha256') != plan.get('databaseLogicalSha256')
                or manifest.get('schemaVersion') != plan.get('schemaVersion')
                or manifest.get('files') != _file_rows(plan)
                or manifest.get('directories', []) != _directory_rows(plan)):
            _fail('SNAPSHOT_CHANGED', '快照文件清单与原核验计划不一致。')
        if _observe(snapshot / 'catalog.sqlite') != _signature(manifest['databaseSignature']):
            _fail('SNAPSHOT_CHANGED', '快照账本已变化。')
        with closing(_ro(snapshot / 'catalog.sqlite')) as connection:
            supported = {int(p.stem.split('_', 1)[0]) for p in (Path(__file__).parent / 'migrations').glob('0*.sql')}
            versions = {r[0] for r in connection.execute('SELECT version FROM schema_migrations')}
            if (not versions or versions - supported
                    or versions != {v for v in supported if v <= max(versions)}
                    or max(versions) != manifest['schemaVersion']):
                _fail('SCHEMA_UNSUPPORTED', '快照账本版本未知或不完整，未写入恢复目录。')
            if (connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok'
                    or connection.execute('PRAGMA foreign_key_check').fetchone() is not None
                    or _logical_digest(connection) != manifest['databaseLogicalSha256']):
                _fail('SNAPSHOT_CHANGED', '快照账本与原核验依据不一致。')
        for row in manifest['files']:
            path = snapshot / _relative(row['snapshotRelativePath'])
            if _observe(path) != row['signature']:
                _fail('SNAPSHOT_CHANGED', '快照文件缺失或内容变化。')
        return manifest
    except RecoveryPlanError:
        raise
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        _fail('SNAPSHOT_INCOMPLETE', '快照清单或完成标记缺失/无效，不能恢复。')


def restore_upgrade_snapshot(snapshot_root, destination_root):
    """Copy only absent data files to an independent installed/staging root.

    Validate all conflicts before writing. Historical original absolute paths
    must remain readable; no external files are overwritten or silently moved.
    A successful data readback still does not migrate or switch installation.
    """
    snapshot, destination = _plain_path(snapshot_root), _plain_path(destination_root)
    _independent(snapshot, destination)
    manifest = _read_snapshot(snapshot)
    source = _plain_path(manifest['sourceRoot'])
    _independent(source, destination)
    _independent(source, snapshot)
    plan = manifest['plan']
    destination_release = None
    if plan['frozenReferences'].get('installedRelease'):
        from .upgrade_installed_references import verify_installed_release
        destination_release = verify_installed_release(destination)
    # Old frozen addresses remain contractual dependencies even if a backup
    # copy was made. Restoring elsewhere must not imply original address repair.
    for row in plan['frozenReferences']['files']:
        if _observe(Path(row['path'])) != {'size_bytes': row['bytes'], 'sha256': row['sha256']}:
            _fail('ORIGINAL_REFERENCE_CHANGED', '历史原地址不可用或内容变化，先恢复原依据；未写入新目录。')
    directories = manifest.get('directories', [])
    _verify_directories(directories)
    directory_targets = [destination / _relative(row['restoreRelativePath']) for row in directories]
    database = destination / _relative(manifest['databaseRelativePath'])
    copies = [(snapshot / 'catalog.sqlite', database, manifest['databaseSignature'])]
    for row in manifest['files']:
        if row['restoreRelativePath'] is not None:
            copies.append((snapshot / _relative(row['snapshotRelativePath']),
                           destination / _relative(row['restoreRelativePath']), row['signature']))
    complete = destination / 'runtime/upgrade-recovery-complete.json'
    # A main DB absent next to another installation's WAL/journal is not an
    # empty destination. Opening our restored DB there could apply foreign rows.
    companions = [Path(str(database) + suffix) for suffix in ('-wal', '-shm', '-journal')]
    paths = [target for _, target, _ in copies] + directory_targets + [complete] + companions
    if len({str(p).casefold() for p in paths}) != len(paths):
        _fail('RESTORE_CONFLICT', '快照含重复或冲突恢复地址。')
    for path in paths:
        _plain_path(path)
        if filesystem_path(path).exists():
            _fail('RESTORE_CONFLICT', '恢复目标已有文件，全部保留；请使用独立空数据目录。')
    try:
        # Explicit directories may be empty, so copying their files cannot create them.
        # Parent-first exclusive mkdir also rejects a concurrent directory replacement.
        for target in sorted(directory_targets, key=lambda path: (len(path.parts), str(path))):
            _plain_path(target)
            filesystem_path(target).mkdir(parents=True, exist_ok=False)
            _plain_path(target)
        for before, after, signature in copies:
            _copy_exclusive(before, after, signature)
        # Refuse companion files created during copying before SQLite can read them.
        for path in companions:
            _plain_path(path)
            if filesystem_path(path).exists():
                _fail('RESTORE_CONFLICT', '复制期间出现其他 SQLite 事务文件；保留现场且不写完成标记。')
        # Re-enter actual readers against copied data, not a database-only test.
        checked = build_recovery_plan(destination, database, destination.parent / (destination.name + '-readback-probe'),
                                      _original_reference_root=plan.get('originalReferenceRoot', manifest['sourceRoot']))
        _complete_inputs(checked)
        if checked['databaseLogicalSha256'] != manifest['databaseLogicalSha256']:
            _fail('RESTORE_READBACK_FAILED', '恢复账本未保留原逻辑内容。')
        for row in manifest['files']:
            if row['restoreRelativePath'] is not None and _observe(destination / row['restoreRelativePath']) != row['signature']:
                _fail('RESTORE_READBACK_FAILED', '恢复文件未保留原内容。')
        if _observe(snapshot / 'snapshot-manifest.json') != _signature(json.loads(filesystem_path(snapshot / 'snapshot-complete.json').read_text(encoding='utf8'))['manifestSignature']):
            _fail('SNAPSHOT_CHANGED', '快照在恢复期间变化。')
        if destination_release is not None and verify_installed_release(destination) != destination_release:
            _fail('SOURCE_CHANGED', '目标安装在恢复期间发生变化。')
        result = {'formatVersion': 1, 'restoreComplete': True, 'destinationRoot': str(destination),
                  'originalReferenceRoot': plan.get('originalReferenceRoot', manifest['sourceRoot']),
                  'restoredFileCount': len(copies), 'externalReferencesRetained': sum(r['restoreRelativePath'] is None for r in manifest['files']),
                  'oldRootMustBePreserved': True, 'canSwitchInstallation': False,
                  'schemaVersion': checked['schemaVersion'], 'databaseLogicalSha256': checked['databaseLogicalSha256'],
                  'remainingChecks': ['维护人员单独迁移恢复副本并验证新任务', '确认服务归属和切换/回退窗口', '普通同事安装和首片验收']}
        _write_exclusive(complete, _json(result))
        return result
    except FileExistsError:
        _fail('RESTORE_CONFLICT', '恢复时目标被其他进程创建，已拒绝覆盖；未写完成标记。')
    except RecoveryPlanError:
        raise
    except (OSError, sqlite3.Error):
        _fail('RESTORE_WRITE_FAILED', '恢复未完成；原安装和已有文件保留，残留新数据仅供维护诊断。')
