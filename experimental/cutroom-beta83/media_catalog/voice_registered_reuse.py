"""Read-only reuse plans from server-registered complete voice bundles.

The coordinator supplies the real read_registered_voice callback, current
context and frozen inputs. This module never creates a job, TTS receipt, file,
registration or applied version. Mixed sources require the real full reader
and its server-only segment_sources contract; this module does not certify ancestry.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import time

from .voice_audio_assembly import ASSEMBLER_VERSION, FORMAT, _existing, _check_boundaries, _us
from .voice_editing import VoiceEditing, compile_voice_input, _check_config, _digest
from .voice_regeneration import plan_segment_regeneration
from .voice_track_segments import _checkpoint, _pcm, _read_json, _safe, _sha

VERSION = 'registered-segment-reuse-v1'
LIMIT = 48000 * 3600 * 2 + 4096


def _fail(code='VOICE_REUSE_SOURCE_INVALID'):
    raise ValueError(code)


def _json(value):
    if not isinstance(value, str) or len(value.encode('utf-8')) > 2 * 1024 * 1024:
        _fail()
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        _fail()


def _config(config):
    try:
        _check_config(config)
        rules = config['rules']
        allowed = {}
        for rule in rules:
            allowed.setdefault(rule['original'], []).append(rule['reading'])
        canonical = compile_voice_input(config['script'], rules, config['voice'], {'substitutions': allowed})
        if canonical != config:
            _fail('VOICE_REUSE_CONFIG_INVALID')
    except (ValueError, KeyError, TypeError, UnicodeError):
        _fail('VOICE_REUSE_CONFIG_INVALID')
    return config


def _parts(config, parts):
    _config(config)
    if not isinstance(parts, list) or not 0 < len(parts) <= 100:
        _fail('VOICE_REUSE_SEGMENTS_INVALID')
    cursor, seen = 0, set()
    for part in parts:
        if not isinstance(part, dict) or set(part) != {'segment_id', 'original_start', 'original_end', 'config'}:
            _fail('VOICE_REUSE_SEGMENTS_INVALID')
        sid, end = part['segment_id'], part['original_end']
        if (not isinstance(sid, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', sid) is None
                or sid in seen or type(part['original_start']) is not int or type(end) is not int
                or part['original_start'] != cursor or not cursor < end <= len(config['script'])):
            _fail('VOICE_REUSE_SEGMENTS_INVALID')
        seen.add(sid)
        if any(rule['start'] < end < rule['end'] for rule in config['rules']):
            _fail('VOICE_REUSE_SEGMENTS_INVALID')
        local = [dict(rule, start=rule['start']-cursor, end=rule['end']-cursor)
                 for rule in config['rules'] if cursor <= rule['start'] < rule['end'] <= end]
        allowed = {}
        for rule in local:
            allowed.setdefault(rule['original'], []).append(rule['reading'])
        expected = compile_voice_input(config['script'][cursor:end], local, config['voice'], {'substitutions': allowed})
        if part['config'] != expected:
            _fail('VOICE_REUSE_SEGMENTS_INVALID')
        cursor = end
    if cursor != len(config['script']) or ''.join(p['config']['tts_text'] for p in parts) != config['tts_text']:
        _fail('VOICE_REUSE_SEGMENTS_INVALID')
    return parts


def _snapshot(db, task_id, bundle_id):
    head = db.connection.execute('SELECT * FROM voice_edit_heads WHERE task_id=?', (task_id,)).fetchone()
    pending = [dict(r) for r in db.connection.execute(
        "SELECT q.request_id,q.status,q.fingerprint,d.draft_id,d.context_digest,d.base_revision,d.config_json,r.status AS run_status,r.input_json FROM voice_edit_requests q JOIN voice_edit_drafts d ON d.draft_id=q.draft_id LEFT JOIN voice_generation_runs r ON r.request_id=q.request_id "
        "WHERE d.task_id=? AND q.status IN ('prepared','running','unknown') AND COALESCE(q.error_code,'')!='VOICE_REQUEST_RELEASED' ORDER BY q.request_id", (task_id,))]
    bundle = None
    if bundle_id is not None:
        registered = db.connection.execute('SELECT 1 FROM voice_registered_bundles WHERE bundle_id=? AND task_id=?',
                                           (bundle_id, task_id)).fetchone()
        row = db.connection.execute(
            'SELECT b.*,r.job_id,r.input_json,r.parameters_json,r.execution_json,r.status AS run_status,'
            'r.bundle_id AS run_bundle_id,q.status AS request_status,q.fingerprint AS request_fingerprint,'
            'q.audio_json AS request_audio_json,d.task_id AS draft_task_id,d.context_digest AS draft_context_digest,'
            'd.config_json AS draft_config_json,d.base_revision AS draft_base_revision,d.status AS draft_status '
            'FROM voice_registered_bundles b JOIN voice_generation_runs r ON r.request_id=b.request_id '
            'JOIN voice_edit_requests q ON q.request_id=r.request_id AND q.draft_id=b.draft_id '
            'JOIN voice_edit_drafts d ON d.draft_id=b.draft_id WHERE b.bundle_id=? AND b.task_id=?',
            (bundle_id, task_id)).fetchone()
        if registered is not None and row is None:
            _fail()
        bundle = dict(row) if row is not None else None
    return dict(head=dict(head) if head is not None else None, pending=pending, bundle=bundle)


def _path(root, relative):
    if not isinstance(relative, str):
        _fail()
    raw = Path(relative)
    if raw.is_absolute() or raw.drive or '..' in raw.parts or not raw.parts:
        _fail('VOICE_REUSE_PATH_INVALID')
    return _safe(root/raw, root)


def _files(root, files, deadline):
    if not isinstance(files, list) or not 1 <= len(files) <= 405:
        _fail()
    expected = {}
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {'path', 'sha256'}:
            _fail()
        path = _path(root, entry['path'])
        sha = entry['sha256']
        if (not isinstance(sha, str) or re.fullmatch('[0-9a-f]{64}', sha) is None or path in expected
                or not path.is_file() or not 0 < path.stat().st_size <= LIMIT
                or _sha(path, deadline=deadline) != sha):
            _fail('VOICE_REUSE_SOURCE_CHANGED')
        expected[path] = sha
    return expected


def _mixed_registered_segments(root, record, private, config, parts, checked, receipt, execution, result,
                               inventory, directory, generation, timeline, parameters, deadline):
    """Consume root-verified mixed provenance, never accept self-declared package origins."""
    entries = checked.get('segment_sources')
    selected = private.get('generation_segment_ids')
    plan_digest = private.get('reuse_plan_digest')
    ids = [p['segment_id'] for p in parts]
    if (not isinstance(entries, list) or len(entries) != len(parts) or not isinstance(selected, list)
            or not selected or any(not isinstance(sid, str) for sid in selected)
            or selected != [sid for sid in ids if sid in selected]
            or not isinstance(plan_digest, str) or re.fullmatch('[0-9a-f]{64}', plan_digest) is None):
        _fail('VOICE_REUSE_MIXED_SOURCES_REQUIRED')
    binding = dict(schema_version=2, executor_version='voice-editing-tts-v1', input_sha256=_digest(private),
                   **{key: private[key] for key in ('task_id', 'draft_id', 'request_id', 'context_digest',
                                                  'base_revision', 'profile_config_digest')},
                   compiled_config_fingerprint=config['fingerprint'], script_sha256=config['script_sha256'],
                   generation_segment_ids=selected, reuse_plan_digest=plan_digest)
    rows, events = result.get('segments'), execution.get('events')
    if (any(doc.get(key) != value for doc in (result, execution) for key, value in binding.items())
            or not isinstance(result.get('run_id'), str) or not result['run_id']
            or result['run_id'] != execution.get('run_id') or result.get('status') != 'succeeded'
            or not isinstance(rows, list) or len(rows) != len(selected)
            or not isinstance(events, list) or len(events) != 2*len(selected)
            or receipt.get('assembler_version') != ASSEMBLER_VERSION or receipt.get('full_decode_verified') is not True
            or receipt.get('assembly_id') != record['bundle_id'] or receipt.get('status') != 'assembled_draft'
            or receipt.get('task_id') != private['task_id'] or receipt.get('base_revision') != private['base_revision']
            or receipt.get('format') != FORMAT or len(receipt.get('sources', [])) != len(parts)
            or len(receipt.get('segments', [])) != len(parts)
            or receipt.get('manifest_digest') != _digest({k:v for k,v in receipt.items() if k != 'manifest_digest'})
            or receipt.get('timeline_digest') != _digest(timeline)
            or _read_json(directory/'timeline.json', deadline, limit=2*1024*1024) != timeline):
        _fail('VOICE_REUSE_BINDING_INVALID')
    origin_keys = {'segment_id', 'source_bundle_id', 'source_request_id', 'source_execution_digest',
                   'source_input_digest', 'source_config_fingerprint', 'source_path', 'source_sha256',
                   'source_raw_sha256', 'source_pcm_sha256', 'source_start_frame', 'source_end_frame',
                   'sample_rate', 'new_tts_execution'}
    hex_keys = {'source_bundle_id', 'source_execution_digest', 'source_input_digest', 'source_config_fingerprint',
                'source_sha256', 'source_raw_sha256', 'source_pcm_sha256'}
    results, origins, cursor, generated_cursor, index, uuids = {}, [], 0, 0, 0, set()
    for part, entry, source, window in zip(parts, entries, receipt['sources'], receipt['segments']):
        sid, cfg = part['segment_id'], part['config']
        if (not isinstance(entry, dict) or set(entry) != {'segment_id', 'config', 'audio', 'origin'}
                or entry['segment_id'] != sid or entry['config'] != cfg
                or not isinstance(source, dict) or source.get('segment_id') != sid
                or source.get('config_fingerprint') != cfg['fingerprint'] or entry['audio'] != source.get('audio')):
            _fail('VOICE_REUSE_MIXED_SOURCE_INVALID')
        audio, origin = entry['audio'], entry['origin']
        if (not isinstance(audio, dict) or audio.get('fingerprint') != cfg['fingerprint']
                or not isinstance(origin, dict) or set(origin) != origin_keys
                or origin['segment_id'] != sid or origin['source_config_fingerprint'] != cfg['fingerprint']
                or origin['new_tts_execution'] is not False or type(origin['sample_rate']) is not int or origin['sample_rate'] != 48000
                or any(not isinstance(origin[k], str) or re.fullmatch('[0-9a-f]{64}', origin[k]) is None for k in hex_keys)
                or not isinstance(origin['source_request_id'], str)
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', origin['source_request_id']) is None
                or origin['source_path'] != audio.get('path') or origin['source_sha256'] != audio.get('sha256')
                or type(origin['source_start_frame']) is not int or type(origin['source_end_frame']) is not int
                or not 0 <= origin['source_start_frame'] < origin['source_end_frame'] <= 48000*3600):
            _fail('VOICE_REUSE_MIXED_ORIGIN_INVALID')
        prefix = 'controlled:' + origin['source_request_id'] + ':'
        proof = audio.get('provider_receipt')
        if not isinstance(proof, str) or not proof.startswith(prefix) or not proof[len(prefix):].strip():
            _fail('VOICE_REUSE_MIXED_ORIGIN_INVALID')
        path = _path(root, audio.get('path'))
        count, pcm_sha = _pcm(path, deadline=deadline)
        mode = 'generate' if sid in selected else 'reuse'
        if (path not in inventory or inventory[path] != audio.get('sha256')
                or origin['source_pcm_sha256'] != pcm_sha or origin['source_end_frame']-origin['source_start_frame'] != count
                or source.get('mode') != mode or window.get('mode') != mode or window.get('segment_id') != sid
                or window.get('start_frame') != cursor or window.get('end_frame') != cursor+count
                or window.get('source_sha256') != audio['sha256'] or window.get('normalized_pcm_sha256') != pcm_sha):
            _fail('VOICE_REUSE_BOUNDARY_INVALID')
        if mode == 'generate':
            row, intent, finished = rows[index], events[2*index], events[2*index+1]
            actual = _digest(dict(parameters=parameters, tts_text_sha256=hashlib.sha256(cfg['tts_text'].encode()).hexdigest()))
            uuid = row.get('client_request_uuid')
            if (row.get('segment_id') != sid or row.get('compiled_config_fingerprint') != cfg['fingerprint']
                    or row.get('original_start') != part['original_start'] or row.get('original_end') != part['original_end']
                    or row.get('original_text') != cfg['script'] or row.get('actual_input_digest') != actual
                    or row.get('decoded', {}).get('frame_count') != count
                    or row.get('decoded', {}).get('sample_rate') != 48000 or row.get('decoded', {}).get('channels') != 1
                    or row.get('start_frame') != generated_cursor or row.get('end_frame') != generated_cursor+count
                    or not isinstance(uuid, str) or not uuid or uuid in uuids
                    or intent.get('phase') != 'intent' or finished.get('phase') != 'result' or finished.get('status') != 'succeeded'
                    or any(e.get('segment_id') != sid or e.get('actual_input_digest') != actual
                           or e.get('client_request_uuid') != uuid for e in (intent, finished))
                    or origin['source_request_id'] != record['request_id'] or origin['source_bundle_id'] != record['bundle_id']
                    or origin['source_execution_digest'] != record['execution_digest'] or origin['source_input_digest'] != actual
                    or origin['source_start_frame'] != cursor or origin['source_end_frame'] != cursor+count
                    or proof != prefix+uuid):
                _fail('VOICE_REUSE_EXECUTION_BINDING_INVALID')
            raw = _path(generation, row.get('raw_path'))
            if (raw not in inventory or inventory[raw] != row.get('raw_sha256')
                    or origin['source_raw_sha256'] != row.get('raw_sha256') or raw.stat().st_size != row.get('raw_size_bytes')):
                _fail('VOICE_REUSE_EXECUTION_BINDING_INVALID')
            uuids.add(uuid);generated_cursor += count;index += 1
        elif origin['source_request_id'] == record['request_id'] or origin['source_bundle_id'] == record['bundle_id']:
            _fail('VOICE_REUSE_MIXED_ORIGIN_INVALID')
        results[sid] = deepcopy(audio);origins.append(deepcopy(origin));cursor += count
    if (generated_cursor != result.get('total_frame_count') or result.get('sample_rate') != 48000 or result.get('channels') != 1
            or cursor != receipt.get('frame_count') or receipt.get('duration_us') != _us(cursor)):
        _fail('VOICE_REUSE_BOUNDARY_INVALID')
    count, pcm_sha = _pcm(directory/'voice.wav', deadline=deadline)
    if (count != cursor or pcm_sha != receipt.get('pcm_sha256')
            or receipt.get('sha256') != inventory[directory/'voice.wav'] or receipt['sha256'] != checked['audio']['sha256']):
        _fail('VOICE_REUSE_BOUNDARY_INVALID')
    # Root already rechecked full A2 identity + registered ancestry; recheck actual concatenated PCM here.
    _check_boundaries(directory/'voice.wav', receipt['segments'])
    _files(root, _json(record['files_json']), deadline)
    return config, parts, results, origins


def _registered_segments(root, record, task_id, context_digest, reader, deadline):
    if not callable(reader):
        _fail('VOICE_REUSE_REGISTERED_READER_REQUIRED')
    private, parameters = _json(record['input_json']), _json(record['parameters_json'])
    config, audio, timeline = _json(record['draft_config_json']), _json(record['audio_json']), _json(record['timeline_json'])
    _config(config)
    if (record['run_status'] != 'ready' or record['request_status'] != 'ready'
            or record['draft_status'] not in ('draft', 'applied')
            or record['run_bundle_id'] != record['bundle_id'] or record['draft_task_id'] != task_id
            or record['context_digest'] != context_digest or record['draft_context_digest'] != context_digest
            or record['config_fingerprint'] != config['fingerprint'] or record['request_fingerprint'] != config['fingerprint']
            or _json(record['request_audio_json']) != audio or private.get('config') != config
            or any(private.get(k) != v for k, v in dict(task_id=task_id, draft_id=record['draft_id'],
                 request_id=record['request_id'], context_digest=context_digest, base_revision=record['draft_base_revision'],
                 profile_config_digest=config['voice']['revision']).items())
            or audio.get('bundle_id') != record['bundle_id'] or audio.get('request_id') != record['request_id']
            or audio.get('fingerprint') != config['fingerprint']):
        _fail('VOICE_REUSE_BINDING_INVALID')
    parts = _parts(config, private.get('segments'))
    # Mandatory real full-reader boundary. A cache stamp cannot satisfy this shape.
    checked = reader(task_id, deepcopy(audio), deepcopy(config), context_digest)
    if (not isinstance(checked, dict) or checked.get('bundle_id') != record['bundle_id']
            or checked.get('audio') != audio or checked.get('timeline') != timeline
            or Path(checked.get('path', '')) != _path(root, audio['path'])):
        _fail('VOICE_REUSE_FULL_READER_REQUIRED')
    _checkpoint(deadline)
    inventory = _files(root, _json(record['files_json']), deadline)
    directory = _path(root, record['package_path'])
    if directory.name != record['bundle_id'] or _path(root, audio['path']) != directory/'voice.wav':
        _fail('VOICE_REUSE_BINDING_INVALID')
    generation = _safe(root/'gen'/_digest(record['request_id'])[:32], root)
    needed = [directory/name for name in ('voice.wav', 'receipt.json', 'timeline.json')]
    needed += [generation/name for name in ('execution.json', 'result.json')]
    if any(path not in inventory for path in needed):
        _fail('VOICE_REUSE_SOURCE_UNREGISTERED')
    receipt = _read_json(directory/'receipt.json', deadline, limit=2*1024*1024)
    execution = _read_json(generation/'execution.json', deadline, limit=2*1024*1024)
    result = _read_json(generation/'result.json', deadline, limit=2*1024*1024)
    if execution != _json(record['execution_json']) or _digest(execution) != record['execution_digest']:
        _fail('VOICE_REUSE_BINDING_INVALID')
    if private.get('schema_version') == 2:
        return _mixed_registered_segments(root, record, private, config, parts, checked, receipt, execution, result,
                                          inventory, directory, generation, timeline, parameters, deadline)
    if private.get('schema_version') != 1:
        _fail('VOICE_REUSE_SOURCE_FORMAT_UNSUPPORTED')
    rows, events = result.get('segments'), execution.get('events')
    if (result.get('status') != 'succeeded' or not isinstance(rows, list) or len(rows) != len(parts)
            or not isinstance(events, list) or len(events) != 2*len(parts)
            or receipt.get('assembler_version') != ASSEMBLER_VERSION or receipt.get('full_decode_verified') is not True
            or receipt.get('task_id') != task_id or receipt.get('base_revision') != private['base_revision']
            or receipt.get('format') != FORMAT or len(receipt.get('sources', [])) != len(parts)
            or len(receipt.get('segments', [])) != len(parts)):
        _fail('VOICE_REUSE_SOURCE_INVALID')
    old = [dict(segment_id=p['segment_id'], config=p['config']) for p in parts]
    plan = plan_segment_regeneration(task_id, private['base_revision'], old, [p['config'] for p in parts], route='ad_mix')
    sources, results, paths, origins, seen, cursor = [], {}, [], [], set(), 0
    for part, planned, row, source, window in zip(parts, plan['segments'], rows, receipt['sources'], receipt['segments']):
        sid, cfg = part['segment_id'], part['config']
        frames = row.get('decoded', {}).get('frame_count')
        if type(frames) is not int or not 0 < frames <= 48000*3600:
            _fail('VOICE_REUSE_BOUNDARY_INVALID')
        input_digest = _digest(dict(parameters=parameters, tts_text_sha256=hashlib.sha256(cfg['tts_text'].encode()).hexdigest()))
        intent, finished = events[2*len(sources):2*len(sources)+2]
        uuid = row.get('client_request_uuid')
        if (not isinstance(uuid, str) or not uuid or uuid in seen or row.get('segment_id') != sid
                or row.get('compiled_config_fingerprint') != cfg['fingerprint'] or row.get('actual_input_digest') != input_digest
                or row.get('start_frame') != cursor or row.get('end_frame') != cursor+frames
                or row['decoded'].get('sample_rate') != 48000 or row['decoded'].get('channels') != 1
                or intent.get('phase') != 'intent' or finished.get('phase') != 'result' or finished.get('status') != 'succeeded'
                or any(e.get('segment_id') != sid or e.get('actual_input_digest') != input_digest
                       or e.get('client_request_uuid') != uuid for e in (intent, finished))):
            _fail('VOICE_REUSE_EXECUTION_BINDING_INVALID')
        seen.add(uuid)
        raw = _path(generation, row.get('raw_path'))
        source_audio = source.get('audio', {})
        path = _path(root, source_audio.get('path'))
        count, pcm_sha = _pcm(path, deadline=deadline)
        if (raw not in inventory or inventory[raw] != row.get('raw_sha256') or raw.stat().st_size != row.get('raw_size_bytes')
                or path not in inventory or inventory[path] != source_audio.get('sha256')
                or source.get('segment_id') != sid or source.get('mode') != 'generate'
                or source.get('config_fingerprint') != cfg['fingerprint'] or source_audio.get('fingerprint') != cfg['fingerprint']
                or source_audio.get('request_id') != planned['request_id']
                or source_audio.get('provider_receipt') != f'controlled:{record["request_id"]}:{uuid}'
                or count != frames or window.get('segment_id') != sid or window.get('mode') != 'generate'
                or window.get('start_frame') != cursor or window.get('end_frame') != cursor+frames
                or window.get('normalized_pcm_sha256') != pcm_sha or window.get('source_sha256') != source_audio['sha256']):
            _fail('VOICE_REUSE_BOUNDARY_INVALID')
        sources.append(source);results[sid] = source_audio;paths.append(path)
        origins.append(dict(segment_id=sid, source_bundle_id=record['bundle_id'], source_request_id=record['request_id'],
                            source_execution_digest=record['execution_digest'], source_input_digest=input_digest,
                            source_config_fingerprint=cfg['fingerprint'], source_path=source_audio['path'],
                            source_sha256=source_audio['sha256'], source_raw_sha256=row['raw_sha256'],
                            source_pcm_sha256=pcm_sha, source_start_frame=cursor, source_end_frame=cursor+frames,
                            sample_rate=48000, new_tts_execution=False))
        cursor += frames
    if result.get('total_frame_count') != cursor or result.get('sample_rate') != 48000 or result.get('channels') != 1:
        _fail('VOICE_REUSE_BOUNDARY_INVALID')
    expected_id = _digest(dict(version=ASSEMBLER_VERSION, format=FORMAT, plan_digest=plan['digest'], sources=sources, shots=[]))
    if expected_id != record['bundle_id']:
        _fail('VOICE_REUSE_SOURCE_FORMAT_UNSUPPORTED')
    verifier = VoiceEditing(None, root, duration_reader=lambda path: _pcm(path, deadline=deadline)[0]/48000)
    existing = _existing(directory, expected_id, sources, plan, results, verifier, [], paths, deadline)
    if existing['timeline'] != timeline or existing['receipt'] != receipt or receipt['sha256'] != audio['sha256']:
        _fail('VOICE_REUSE_BINDING_INVALID')
    _files(root, _json(record['files_json']), deadline)
    return config, parts, results, origins


def plan_registered_voice_reuse(db, audio_root, *, task_id, context_digest, base_revision, config, segments,
                               source_bundle_id=None, registered_reader=None, current_request_id=None, timeout_seconds=120):
    """Freeze a private A4 plan; only its generated subset may need a new call.

    All expected identities are server inputs. Call again and compare digest at
    the worker boundary; the root coordinator owns actual context resolution,
    authorization, queue claiming, paid consent, mixed registration and CAS.
    """
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
        _fail('VOICE_REUSE_TIMEOUT_INVALID')
    if (not isinstance(task_id, str) or not task_id or type(base_revision) is not int or base_revision < 0
            or not isinstance(context_digest, str) or re.fullmatch('[0-9a-f]{64}', context_digest) is None
            or source_bundle_id is not None and (not isinstance(source_bundle_id, str)
                                               or re.fullmatch('[0-9a-f]{64}', source_bundle_id) is None)):
        _fail('VOICE_REUSE_BINDING_INVALID')
    if current_request_id is not None and (not isinstance(current_request_id, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', current_request_id) is None):
        _fail('VOICE_REUSE_CURRENT_REQUEST_INVALID')
    deadline = time.monotonic()+timeout_seconds
    root = _safe(audio_root)
    config, segments = deepcopy((config, segments))
    _parts(config, segments)
    snapshot = _snapshot(db, task_id, source_bundle_id)
    head = snapshot['head']
    if head is None or head['revision'] != base_revision or head['context_digest'] != context_digest:
        _fail('VOICE_REUSE_CURRENT_CONTEXT_REQUIRED')
    found_current = False
    for pending in snapshot['pending']:
        if pending['request_id'] != current_request_id or pending['status'] == 'unknown':
            _fail('VOICE_REUSE_GENERATION_UNRESOLVED')
        frozen = _json(pending['input_json'])
        if (pending['status'], pending['run_status']) not in (('prepared', 'queued'), ('running', 'running')):
            _fail('VOICE_REUSE_CURRENT_REQUEST_INVALID')
        if (pending['fingerprint'] != config['fingerprint'] or _json(pending['config_json']) != config
                or pending['context_digest'] != context_digest or pending['base_revision'] != base_revision
                or any(frozen.get(k) != v for k, v in dict(task_id=task_id, draft_id=pending['draft_id'],
                    request_id=current_request_id, context_digest=context_digest, base_revision=base_revision,
                    config=config, segments=segments, profile_config_digest=config['voice']['revision']).items())):
            _fail('VOICE_REUSE_CURRENT_REQUEST_INVALID')
        found_current = True
    if current_request_id is not None and not found_current:
        _fail('VOICE_REUSE_CURRENT_REQUEST_INVALID')
    if _config(_json(head['config_json']))['script_sha256'] != config['script_sha256']:
        _fail('SCRIPT_CHANGE_REQUIRES_SCRIPT_WORKFLOW')
    old_config, previous, audios, origins = None, [], {}, []
    if snapshot['bundle'] is not None:
        old_config, previous, audios, origins = _registered_segments(root, snapshot['bundle'], task_id,
                                                                  context_digest, registered_reader, deadline)
        if old_config['script_sha256'] != config['script_sha256']:
            _fail('SCRIPT_CHANGE_REQUIRES_SCRIPT_WORKFLOW')
    by_window = {(p['segment_id'], p['original_start'], p['original_end']): p for p in previous}
    origin_map = {r['segment_id']: r for r in origins}
    old, reused = [], []
    for part in segments:
        sid = part['segment_id']
        before = by_window.get((sid, part['original_start'], part['original_end']))
        item = dict(segment_id=sid, config=part['config'])
        if before is not None and old_config['voice'] == config['voice'] and before['config'] == part['config']:
            item['audio'] = deepcopy(audios[sid]);reused.append(deepcopy(origin_map[sid]))
        old.append(item)
    plan = plan_segment_regeneration(task_id, base_revision, old, [p['config'] for p in segments], route='ad_mix')
    generated_ids = {p['segment_id'] for p in plan['segments'] if p['mode'] == 'generate'}
    if _snapshot(db, task_id, source_bundle_id) != snapshot:
        _fail('VOICE_REUSE_SNAPSHOT_CHANGED')
    _checkpoint(deadline)
    output = dict(schema_version=1, planner_version=VERSION, status='planned', task_id=task_id,
                  context_digest=context_digest, base_revision=base_revision, config_fingerprint=config['fingerprint'],
                  plan=plan, generation_segments=[p for p in segments if p['segment_id'] in generated_ids],
                  reuse_sources=reused, maximum_requests=len(generated_ids), ready_for_application=False,
                  provider_dispatched=False)
    output['digest'] = _digest(output)
    return output
