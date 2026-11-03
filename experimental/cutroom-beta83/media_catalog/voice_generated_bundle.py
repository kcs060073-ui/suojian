"""Read existing single-segment generated assemblies without certifying TTS.

All expected bindings come from trusted server records, never the browser or
package itself. No storage, jobs, providers, migrations, head/apply writes.
"""
from copy import deepcopy
import math
import os
from pathlib import Path
import re
import time

from .tools import find_ffprobe, default_ffprobe_search_roots
from .voice_audio_assembly import ASSEMBLER_VERSION, FORMAT, _existing
from .voice_editing import VoiceEditing, _check_config, _digest
from .voice_original_baseline import _safe, _hash, _read_json
from .voice_original_track_bundle import _run, _strict_pcm_hash


def _path(value):
    path = Path(value).absolute()
    if '..' in path.parts:
        raise ValueError('GENERATED_PATH_INVALID')
    _safe(path)
    return path


def _request_snapshot(db, request_id):
    if db is None:
        return None
    tables = {row[0] for row in db.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('voice_edit_requests','voice_edit_drafts')")}
    if tables != {'voice_edit_requests', 'voice_edit_drafts'}:
        return None
    sizes = db.connection.execute(
        'SELECT length(r.audio_json),length(d.config_json) FROM voice_edit_requests r '
        'LEFT JOIN voice_edit_drafts d ON d.draft_id=r.draft_id WHERE r.request_id=?', (request_id,)).fetchone()
    if sizes is None:
        return None
    if any(size is not None and size > 2 * 1024 * 1024 for size in sizes):
        raise ValueError('GENERATED_REQUEST_TOO_LARGE')
    service = VoiceEditing(db, Path('.'))
    request = service.request(request_id)
    return dict(request=request, draft=service.draft(request['draft_id']))


def read_generated_voice_bundle(db, audio_root, *, task_id, context_digest, config,
                                bundle_id, package_path, plan, results, shots=(),
                                runner=None, timeout_seconds=120):
    """Verify the existing A2 three-file assembly and return an internal draft.

    First contract: one complete new ``generate`` segment, no original/reused
    audio. A local ready request binds task/context/input but does not prove a
    real provider response: VoiceEditing.finish currently trusts any nonempty
    provider_receipt. Consequently every result stays pending and cannot apply.
    Missing request tables/rows also remain explicitly unverified.

    The coordinator owns authorization, frozen bundle registration, current
    context/head checks and public projection. This function never discovers
    an expected ID from the artifact or infers a voice from current defaults.
    """
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
        raise ValueError('GENERATED_TIMEOUT_INVALID')
    deadline = time.monotonic() + timeout_seconds
    if (not isinstance(task_id, str) or not task_id or not isinstance(context_digest, str)
            or not context_digest or not isinstance(bundle_id, str)
            or re.fullmatch('[0-9a-f]{64}', bundle_id) is None):
        raise ValueError('GENERATED_BINDING_REQUIRED')
    config, plan, results, shots = deepcopy((config, plan, results, list(shots)))
    _check_config(config)
    if (not isinstance(plan, dict) or set(plan) != {'task_id','base_revision','segments','jobs','digest'}
            or plan['task_id'] != task_id or type(plan['base_revision']) is not int
            or plan['base_revision'] < 0 or not isinstance(plan['segments'], list)
            or len(plan['segments']) != 1):
        raise ValueError('GENERATED_PLAN_BINDING_INVALID')
    if plan['digest'] != _digest({k:v for k,v in plan.items() if k != 'digest'}):
        raise ValueError('REGENERATION_PLAN_CHANGED')
    segment = plan['segments'][0]
    if (set(segment) != {'segment_id','config','mode','request_id'} or segment['mode'] != 'generate'
            or not isinstance(segment['segment_id'], str) or not segment['segment_id']
            or segment['config'] != config):
        raise ValueError('GENERATED_CONFIG_BINDING_INVALID')
    request_id = 'segment-' + _digest([task_id,plan['base_revision'],segment['segment_id'],config['fingerprint']])
    if (segment['request_id'] != request_id or plan['jobs'] != [dict(segment_id=segment['segment_id'],
            request_id=request_id,fingerprint=config['fingerprint'])]
            or not isinstance(results, dict) or set(results) != {segment['segment_id']}
            or results[segment['segment_id']].get('request_id') != request_id):
        raise ValueError('GENERATED_REQUEST_BINDING_INVALID')
    root, directory = _path(audio_root), _path(package_path)
    if directory.name != bundle_id or not directory.is_dir():
        raise ValueError('GENERATED_BUNDLE_ID_INVALID')
    paths = {name:_path(directory/name) for name in ('voice.wav','receipt.json','timeline.json')}
    for name,path in paths.items():
        limit = 48000 * 3600 * 2 + 4096 if name == 'voice.wav' else 2 * 1024 * 1024
        if not path.is_file() or path.stat().st_size > limit:
            raise ValueError('GENERATED_PACKAGE_INVALID')
    before = {name:_hash(path,deadline) for name,path in paths.items()}
    receipt = _read_json(paths['receipt.json'],deadline,before['receipt.json']['sha256'])
    if (receipt.get('task_id') != task_id or receipt.get('base_revision') != plan['base_revision']
            or receipt.get('plan_digest') != plan['digest'] or receipt.get('assembly_id') != bundle_id
            or receipt.get('assembler_version') != ASSEMBLER_VERSION
            or receipt.get('full_decode_verified') is not True):
        raise ValueError('GENERATED_RECEIPT_BINDING_INVALID')
    audio = results[segment['segment_id']]
    raw_path = Path(audio.get('path', ''))
    if raw_path.is_absolute() or '..' in raw_path.parts or not raw_path.parts:
        raise ValueError('AUDIO_PATH_INVALID')
    source = _path(root / raw_path)
    if not source.is_relative_to(root) or not source.is_file() or source.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('GENERATED_SOURCE_INVALID')
    source_before = _hash(source,deadline)
    snapshot = _request_snapshot(db,request_id)
    ffprobe = find_ffprobe(os.environ,search_roots=default_ffprobe_search_roots())
    def duration_reader(path):
        outcome = _run([ffprobe,'-v','error','-show_entries','format=duration',
                        '-of','default=noprint_wrappers=1:nokey=1',path],path.parent,deadline,runner)
        duration = float(outcome['stdout'].strip())
        return duration if math.isfinite(duration) and 0 < duration <= 3600 else None
    verifier = VoiceEditing(None,root,duration_reader=duration_reader)
    checked = verifier.verify_audio(config,audio)
    if source_before['sha256'] != checked['sha256']:
        raise ValueError('SOURCE_CHANGED')
    if snapshot is not None:
        req,draft = snapshot['request'],snapshot['draft']
        if (req['status'] != 'ready' or req['fingerprint'] != config['fingerprint']
                or req['audio'] != checked or draft['task_id'] != task_id
                or draft['context_digest'] != context_digest or draft['config'] != config
                or draft['base_revision'] != plan['base_revision']):
            raise ValueError('GENERATED_REQUEST_RECORD_MISMATCH')
    sources = [dict(segment_id=segment['segment_id'],mode='generate',
                    config_fingerprint=config['fingerprint'],audio=checked)]
    identity = _digest(dict(version=ASSEMBLER_VERSION,format=FORMAT,
                           plan_digest=plan['digest'],sources=sources,shots=shots))
    if identity != bundle_id:
        raise ValueError('GENERATED_BUNDLE_ID_INVALID')
    try:
        bundle = _existing(directory,bundle_id,sources,plan,results,verifier,shots,[source],deadline)
    except EOFError as exc:
        raise ValueError('ASSEMBLY_VERSION_CORRUPT') from exc
    if bundle['receipt'] != receipt:
        raise ValueError('GENERATED_PACKAGE_CHANGED')
    # A2's replay checks local PCM consistency; additionally bind that PCM to
    # a strict full normalization of the still-known source. Manifest self-hash
    # and a fixed assembly ID alone cannot establish this acoustic relation.
    if _strict_pcm_hash(source,source.parent,deadline,runner) != receipt['pcm_sha256']:
        raise ValueError('GENERATED_SOURCE_PCM_MISMATCH')
    if (_hash(source,deadline) != source_before
            or {name:_hash(path,deadline) for name,path in paths.items()} != before
            or _request_snapshot(db,request_id) != snapshot):
        raise ValueError('GENERATED_PACKAGE_CHANGED')
    if time.monotonic() >= deadline:
        raise RuntimeError('PROCESS_TIMEOUT')
    return dict(status='pending',code='GENERATED_PROVIDER_PROVENANCE_UNVERIFIED',
        task_id=task_id,context_digest=context_digest,config_fingerprint=config['fingerprint'],
        bundle_id=bundle_id,audio_path=bundle['audio_path'],sha256=receipt['sha256'],
        pcm_sha256=receipt['pcm_sha256'],frame_count=receipt['frame_count'],sample_rate=48000,
        duration_us=receipt['duration_us'],timing_kind=checked['timing_kind'],
        timing=[dict(text=row['text'],start=row['start_us']/1_000_000,end=row['end_us']/1_000_000)
                for row in bundle['timeline']['subtitles']],
        verification=dict(bundle_integrity='verified',complete_pcm='verified',timing_table='verified',
            task_config_binding='verified',request_binding='verified' if snapshot else 'unverified',
            provider_provenance='unverified',timing_alignment='unverified'),
        gaps=['PROVIDER_SOURCE_EVIDENCE_REQUIRED'] + ([] if snapshot else ['GENERATION_REQUEST_RECORD_MISSING']),
        ready_for_voice_editing=False)
