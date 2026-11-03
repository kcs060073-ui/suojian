"""Immutable, file-proven originals. No TTS, credentials, DB writes or apply adapter.

An origin label proves captured file/input bindings, never measured alignment,
complete PCM windows or permission to pass this package to VoiceEditing.finish.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import time
import uuid

from .ad_artifact_binding import _ARTIFACT_NAMES
from .task_script import read_task_script
from .workflow import project_gates
from .provenance import canonical_digest, latest_gate
from .tasks import get_task


_FILES = ('final-script.txt', 'adplan.json', 'adtiming.json', 'voice.mp3', 'recommendation.json')
_ARTIFACTS = _ARTIFACT_NAMES
_VERIFICATION = dict(file_provenance='verified', original_tts_input='verified',
                     audio_decode='unverified', timing_alignment='unverified', complete_sample_windows='unverified')


class OriginalBaselineError(ValueError):
    def __init__(self, code, *, current_snapshot=None):
        self.code = code
        # Diagnostic current hashes are explicitly NOT historical evidence.
        self.current_snapshot = current_snapshot
        super().__init__(code)


def _fail(code, **kwargs):
    raise OriginalBaselineError(code, **kwargs)


def _deadline(seconds):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        _fail('BASELINE_TIMEOUT_INVALID')
    result = time.monotonic() + seconds
    _check_time(result)
    return result


def _check_time(deadline):
    if time.monotonic() >= deadline:
        _fail('BASELINE_TIMEOUT')


def _sha_value(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _safe(path):
    # Reject Windows junctions as well as ordinary symlinks, including parents.
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            stat = part.lstat()
            if part.is_symlink() or getattr(stat, 'st_file_attributes', 0) & 0x400:
                _fail('BASELINE_PATH_INVALID')


def _signature(path):
    _safe(path)
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino, stat.st_dev


def _hash(path, deadline):
    before = _signature(path)
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while True:
            _check_time(deadline)
            chunk = source.read(65536)
            _check_time(deadline)
            if not chunk:
                break
            digest.update(chunk)
    if before != _signature(path):
        _fail('BASELINE_SOURCE_CHANGED')
    return dict(sha256=digest.hexdigest(), size_bytes=before[0])


def _read_json(path, deadline, expected_sha256=None):
    before = _signature(path)
    if before[0] > 8 * 1024 * 1024:
        _fail('BASELINE_RECORD_TOO_LARGE')
    chunks = []
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as source:
        while True:
            _check_time(deadline)
            chunk = source.read(65536)
            _check_time(deadline)
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
            size += len(chunk)
            if size > 8 * 1024 * 1024:
                _fail('BASELINE_RECORD_TOO_LARGE')
    if before != _signature(path) or expected_sha256 is not None and digest.hexdigest() != expected_sha256:
        _fail('BASELINE_SOURCE_CHANGED')
    try:
        result = json.loads(b''.join(chunks).decode('utf-8'))
    except (ValueError, UnicodeError):
        _fail('BASELINE_RECORD_INVALID')
    if not isinstance(result, dict):
        _fail('BASELINE_RECORD_INVALID')
    return result


def _historical_proof(db, task_id, snapshot_id, paths, files, deadline):
    row = db.connection.execute('SELECT * FROM render_snapshots WHERE snapshot_id=? AND task_id=?',
                                (snapshot_id, task_id)).fetchone()
    if row is None:
        _fail('HISTORICAL_RENDER_INVALID')
    return _historical_file_proof(dict(row), task_id, snapshot_id, paths, files, deadline, resolve_paths=True)


def _historical_file_proof(row, task_id, snapshot_id, paths, files, deadline, *, resolve_paths=False):
    # Read-back compares saved absolute paths lexically. It must not resolve or
    # inspect original source files, which may have moved since capture.
    try:
        if row['task_id'] != task_id or row['snapshot_id'] != snapshot_id:
            raise ValueError()
        if not isinstance(row['params_json'], str) or len(row['params_json'].encode('utf-8')) > 8 * 1024 * 1024:
            raise ValueError()
        params = json.loads(row['params_json'])
        dependency = params['dependency_snapshot']
        digest = canonical_digest(dependency)
        if (row['stage'] != 'mix' or row['producer'] != 'assemble-ad-v1' or not row['created_at']
                or params['dependency_digest'] != digest or row['input_digest'] != digest
                or dependency['task_id'] != task_id or dependency['route'] != 'ad_mix'):
            raise ValueError()
        entries = dependency['files']
        if not isinstance(entries, list):
            raise ValueError()
        for name in _FILES[:4]:
            expected = files[name]
            if (not _sha_value(expected['sha256']) or type(expected['size_bytes']) is not int
                    or expected['size_bytes'] < 0):
                raise ValueError()
            target = paths[name].resolve() if resolve_paths else paths[name].absolute()
            matches = [entry for entry in entries if isinstance(entry, dict)
                       and isinstance(entry.get('path'), str) and Path(entry['path']).is_absolute()
                       and (Path(entry['path']).resolve() if resolve_paths else Path(entry['path']).absolute()) == target]
            if len(matches) != 1:
                raise ValueError()
            entry = matches[0]
            if (entry.get('sha256') != files[name]['sha256']
                    or type(entry.get('size_bytes')) is not int
                    or entry['size_bytes'] != files[name]['size_bytes']):
                raise ValueError()
    except (KeyError, TypeError, ValueError, OSError):
        _fail('HISTORICAL_RENDER_INVALID')
    _check_time(deadline)
    return dict(kind='historical_render_snapshot', snapshot_id=snapshot_id, source_record=row)


def _historical_metadata_proof(record, task_id, metadata_path, expected, deadline):
    # A current recommendation can be rewritten together with its input digest.
    # Only a previously captured dependency row anchors its original TTS fields.
    try:
        if (record['task_id'] != task_id or record['stage'] != 'mix'
                or record['producer'] != 'assemble-ad-v1' or not record['created_at']
                or len(record['params_json'].encode('utf-8')) > 8 * 1024 * 1024):
            raise ValueError()
        params = json.loads(record['params_json'])
        dependency = params['dependency_snapshot']
        digest = canonical_digest(dependency)
        if (params['dependency_digest'] != digest or record['input_digest'] != digest
                or dependency['task_id'] != task_id or dependency['route'] != 'ad_mix'):
            raise ValueError()
        entries = [item for item in dependency['files'] if isinstance(item, dict)
                   and isinstance(item.get('path'), str)
                   and Path(item['path']).absolute() == metadata_path.absolute()]
        if (len(entries) != 1 or entries[0].get('sha256') != expected['sha256']
                or type(entries[0].get('size_bytes')) is not int
                or entries[0]['size_bytes'] != expected['size_bytes']):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        _fail('HISTORICAL_TTS_PROOF_REQUIRED')
    _check_time(deadline)
    return dict(kind='historical_dependency_metadata_sha256', snapshot_id=record['snapshot_id'],
                metadata_path=str(metadata_path.absolute()), source_record=record)


def _tts_input(task, metadata):
    # Agent planning digests deliberately omit TTS: never impose standalone keys.
    if metadata.get('source') == 'agent_artifact':
        _fail('AGENT_ORIGINAL_TTS_LINK_REQUIRED')
    if metadata.get('source') != 'standalone_model' or task['orchestration_mode'] != 'standalone':
        _fail('ORIGINAL_TTS_BINDING_REQUIRED')
    fields = ('task_id', 'script_sha256', 'source_snapshot_ids', 'ratio', 'general_model', 'voice',
              'general_profile_digest', 'tts_profile_digest')
    if (any(not isinstance(metadata.get(key), str) or not metadata[key].strip()
            for key in ('general_model', 'voice'))
            or any(not _sha_value(metadata.get(key)) for key in ('general_profile_digest', 'tts_profile_digest'))):
        _fail('ORIGINAL_TTS_BINDING_REQUIRED')
    sources = metadata.get('source_snapshot_ids')
    if (not isinstance(sources, list) or any(not isinstance(value, str) or not value for value in sources)
            or metadata.get('ratio') != task['brief'].get('aspect_ratio')):
        _fail('ORIGINAL_TTS_INPUT_CHANGED')
    binding = {key: metadata.get(key) for key in fields}
    if canonical_digest(binding) != metadata.get('input_digest'):
        _fail('ORIGINAL_TTS_INPUT_CHANGED')
    # This is the original recorded input, not the currently selected TTS config.
    return dict(source='standalone_model', input_digest=metadata['input_digest'],
                voice=metadata['voice'], tts_profile_digest=metadata['tts_profile_digest'],
                input_binding=binding)


def _inspect(db, root, task_id, historical_render_id, deadline):
    if not isinstance(task_id, str) or re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]*', task_id) is None:
        _fail('BASELINE_TASK_INVALID')
    task = get_task(db, task_id)
    if task is None or task.get('task_type') != 'ad_mix' or not isinstance(task.get('brief'), dict):
        _fail('LOCKED_AD_VOICE_REQUIRED')
    if not project_gates(db, task_id)['brief_locked']:
        _fail('LOCKED_AD_VOICE_REQUIRED')
    _check_time(deadline)
    task_root = root / 'runtime/workbench/tasks' / task_id
    paths = {name: task_root / 'artifacts/ad' / name for name in _FILES}
    paths['final-script.txt'] = task_root / 'inputs/final-script.txt'
    script_record = task['brief'].get('final_script')
    if not isinstance(script_record, dict) or script_record.get('relative_path') != 'inputs/final-script.txt':
        _fail('LOCKED_AD_VOICE_REQUIRED')
    for path in paths.values():
        _safe(path)
        if not path.is_file() or not path.resolve().is_relative_to(task_root.resolve()):
            _fail('LOCKED_AD_VOICE_REQUIRED')
    files = {name: _hash(path, deadline) for name, path in paths.items()}
    metadata = _read_json(paths['recommendation.json'], deadline, files['recommendation.json']['sha256'])
    _check_time(deadline)
    if files['final-script.txt']['sha256'] != script_record.get('sha256'):
        _fail('LOCKED_AD_VOICE_REQUIRED')
    if metadata.get('task_id') != task_id or metadata.get('script_sha256') != script_record['sha256']:
        _fail('ORIGINAL_TASK_BINDING_INVALID')
    recorded = metadata.get('artifact_sha256')
    if recorded is not None:
        if (not isinstance(recorded, dict) or set(recorded) != set(_ARTIFACTS)
                or any(not _sha_value(recorded[name]) or recorded[name] != files[name]['sha256'] for name in _ARTIFACTS)):
            _fail('RECORDED_ARTIFACT_CHANGED')
        provenance = dict(kind='recorded_artifact_sha256', source_record=metadata)
    elif historical_render_id is not None:
        provenance = _historical_proof(db, task_id, historical_render_id, paths, files, deadline)
    else:
        _fail('HISTORICAL_ARTIFACT_PROOF_REQUIRED', current_snapshot={name: value['sha256'] for name,value in files.items()})
    # Reuse current_ad_voice's authoritative lock/script readers and artifact
    # name contract. Its full-file hash helpers cannot honor this deadline.
    # Our already-bounded hashes replace both duplicate unchecked scans.
    _check_time(deadline)
    script = read_task_script(root, task)  # Existing reader caps copy at 200 KB.
    _check_time(deadline)
    if (script is None or hashlib.sha256(script.encode('utf-8')).hexdigest() != script_record['sha256']
            or files['voice.mp3']['size_bytes'] <= 0):
        _fail('LOCKED_AD_VOICE_REQUIRED')
    tts_input = _tts_input(task, metadata)
    if historical_render_id is None:
        _fail('HISTORICAL_TTS_PROOF_REQUIRED')
    history = (provenance if provenance['kind']=='historical_render_snapshot' else
               _historical_proof(db, task_id, historical_render_id, paths, files, deadline))
    tts_input['historical_proof'] = _historical_metadata_proof(
        history['source_record'], task_id, paths['recommendation.json'], files['recommendation.json'], deadline)
    gate = dict(latest_gate(db, task_id, 'brief'))
    _check_time(deadline)
    identity = dict(schema_version=1, origin_kind='verified_legacy_baseline',
                    task_id=task_id, route='ad_mix', orchestration_mode=task['orchestration_mode'],
                    script_sha256=script_record['sha256'], brief_lock=gate,
                    files=files, tts_input=tts_input, provenance=provenance)
    return identity, paths


def _copy_verified(source, target, expected, deadline):
    _safe(source)
    _safe(target)
    digest = hashlib.sha256()
    size = 0
    with source.open('rb') as reader, target.open('xb') as writer:
        while True:
            _check_time(deadline)
            chunk = reader.read(65536)
            _check_time(deadline)
            if not chunk:
                break
            writer.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    if dict(sha256=digest.hexdigest(), size_bytes=size) != expected or _hash(target, deadline) != expected:
        _fail('BASELINE_SOURCE_CHANGED')


def _read_package(path, baseline_id, deadline, *, expected_task_id, identity=None):
    try:
        _safe(path)
        manifest = _read_json(path / 'manifest.json', deadline)
        keys = ('schema_version', 'origin_kind', 'task_id', 'route', 'orchestration_mode',
                'script_sha256', 'brief_lock', 'files', 'tts_input', 'provenance')
        actual_identity = {key: manifest[key] for key in keys}
        if (not _sha_value(baseline_id) or manifest['baseline_id'] != baseline_id
                or canonical_digest(actual_identity) != baseline_id
                or identity is not None and actual_identity != identity
                or manifest['task_id'] != expected_task_id
                or manifest['origin_kind'] != 'verified_legacy_baseline'
                or manifest['verification'] != _VERIFICATION
                or manifest['ready_for_voice_editing'] is not False
                or 'provider_receipt' in manifest or set(manifest['files']) != set(_FILES)):
            raise ValueError()
        proof = manifest['tts_input']['historical_proof']
        if _historical_metadata_proof(proof['source_record'], expected_task_id, Path(proof['metadata_path']),
                                      manifest['files']['recommendation.json'], deadline) != proof:
            raise ValueError()
        # Frozen metadata_path preserves the original task path contract. Derive
        # the other four saved paths from it; never from live source existence or
        # from a basename-only search of possibly unrelated history entries.
        metadata_path = Path(proof['metadata_path'])
        if (not metadata_path.is_absolute() or '..' in metadata_path.parts
                or metadata_path.parts[-7:] != ('runtime', 'workbench', 'tasks', expected_task_id,
                                                'artifacts', 'ad', 'recommendation.json')):
            raise ValueError()
        source_paths = {name: metadata_path.parent / name for name in _FILES[:4]}
        source_paths['final-script.txt'] = metadata_path.parent.parent.parent / 'inputs/final-script.txt'
        history = _historical_file_proof(proof['source_record'], expected_task_id, proof['snapshot_id'],
                                         source_paths, manifest['files'], deadline)
        metadata = _read_json(path / 'recommendation.json', deadline, manifest['files']['recommendation.json']['sha256'])
        if (metadata.get('task_id') != expected_task_id or metadata.get('script_sha256') != manifest['script_sha256']
                or manifest['script_sha256'] != manifest['files']['final-script.txt']['sha256']):
            raise ValueError()
        recorded = metadata.get('artifact_sha256')
        if recorded is not None and (not isinstance(recorded, dict) or set(recorded) != set(_ARTIFACTS)
                or any(recorded[name] != manifest['files'][name]['sha256'] for name in _ARTIFACTS)):
            raise ValueError()
        expected_provenance = (dict(kind='recorded_artifact_sha256', source_record=metadata)
                               if recorded is not None else history)
        if manifest['provenance'] != expected_provenance:
            raise ValueError()
        frozen_task = dict(orchestration_mode=manifest['orchestration_mode'],
                           brief=dict(aspect_ratio=manifest['tts_input']['input_binding']['ratio']))
        recorded_input = {key: value for key, value in manifest['tts_input'].items() if key != 'historical_proof'}
        if _tts_input(frozen_task, metadata) != recorded_input:
            raise ValueError()
        captured = datetime.fromisoformat(manifest['captured_at'])
        if captured.tzinfo is None:
            raise ValueError()
        for name in _FILES:
            if _hash(path / name, deadline) != manifest['files'][name]:
                raise ValueError()
    except OriginalBaselineError as error:
        if error.code == 'BASELINE_TIMEOUT':
            raise
        _fail('BASELINE_PACKAGE_INVALID')
    except (OSError, KeyError, TypeError, ValueError):
        _fail('BASELINE_PACKAGE_INVALID')
    _check_time(deadline)
    return dict(path=str(path), manifest=manifest)


def read_original_voice_baseline(package_path, *, expected_task_id, expected_baseline_id, timeout_seconds=30):
    """Read a package by ID already frozen by the trusted caller, never by self-claim."""
    return _read_package(Path(package_path).absolute(), expected_baseline_id, _deadline(timeout_seconds),
                         expected_task_id=expected_task_id)


def prepare_original_voice_baseline(db, project_root, task_id, output_root, *, historical_render_id=None, timeout_seconds=30):
    """Capture original files only when historical file and original input bindings hold.

    historical_render_id selects an existing same-task DB row; it is not a caller
    supplied hash dictionary. No account/profile/credential store is consulted.
    Agent records currently require a separate historical TTS-link contract.
    """
    deadline = _deadline(timeout_seconds)
    root, output = Path(project_root).absolute(), Path(output_root).absolute()
    identity, paths = _inspect(db, root, task_id, historical_render_id, deadline)
    _safe(output)
    for path in paths.values():
        if output.resolve().is_relative_to(path.parent.resolve()):
            _fail('BASELINE_OUTPUT_INVALID')
    baseline_id = canonical_digest(identity)
    destination = output / ('baseline-' + baseline_id)
    if destination.exists():
        return _read_package(destination, baseline_id, deadline, expected_task_id=task_id, identity=identity)
    output.mkdir(parents=True, exist_ok=True)
    stage = output / ('.capture-' + uuid.uuid4().hex)
    stage.mkdir()
    try:
        for name, path in paths.items():
            _copy_verified(path, stage / name, identity['files'][name], deadline)
        try:
            after, _ = _inspect(db, root, task_id, historical_render_id, deadline)
        except OriginalBaselineError as error:
            if error.code == 'BASELINE_TIMEOUT':
                raise
            _fail('BASELINE_SOURCE_CHANGED')
        if after != identity:
            _fail('BASELINE_SOURCE_CHANGED')
        manifest = dict(identity, baseline_id=baseline_id,
                        captured_at=datetime.now(timezone.utc).isoformat(),
                        verification=dict(_VERIFICATION), ready_for_voice_editing=False)
        (stage / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding='utf-8')
        _read_package(stage, baseline_id, deadline, expected_task_id=task_id, identity=identity)
        _check_time(deadline)
        try:
            stage.rename(destination)
        except OSError:
            # Another capture can win. Never replace its immutable directory.
            if not destination.is_dir():
                raise
        return _read_package(destination, baseline_id, deadline, expected_task_id=task_id, identity=identity)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
