"""Private, bounded PCM facts for the current production ad voice. No audio upload."""
from collections import OrderedDict
import hashlib
import json
import math
from pathlib import Path
import threading

import numpy as np

from .packaging_audio_evidence import (
    _decode_audio, _db, SAMPLE_RATE, MAX_ANALYSIS_SECONDS, file_sha256,
)
from .voice_bundle_service import resolve_ad_render_inputs

METHOD_VERSION = 'creative-current-pcm-v1'
_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
_MAX_CACHE_ENTRIES = 4
_MAX_RESULT_BYTES = 32768


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _read_json(path):
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('VOICE_EVIDENCE_TOO_LARGE')
    return json.loads(path.read_text(encoding='utf-8'))


def _bound(db, root, task, projection):
    task_id = task['task_id']
    if not task_id or Path(task_id).name != task_id or task_id in {'.', '..'}:
        raise ValueError('VOICE_TASK_INVALID')
    trusted = (Path(root).resolve() / 'runtime/workbench/tasks').resolve()
    task_root = (trusted / task_id).resolve()
    if not task_root.is_relative_to(trusted) or task_root == trusted:
        raise ValueError('VOICE_TASK_INVALID')
    # Read live task state again, including after analysis; never trust a caller's
    # stale brief or a PCM cache entry as task/voice authorization.
    current = db.connection.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if current is None or current['task_type'] != 'ad_mix' or current['brief_json'] != task['brief_json']:
        raise ValueError('VOICE_TASK_CHANGED')
    brief = json.loads(current['brief_json'])
    record = brief['final_script']
    relative = Path(record['relative_path'])
    script_path = (task_root / relative).resolve()
    if relative.is_absolute() or '..' in relative.parts or not script_path.is_relative_to(task_root):
        raise ValueError('VOICE_SCRIPT_INVALID')
    if script_path.stat().st_size > 200000:
        raise ValueError('VOICE_SCRIPT_INVALID')
    script_bytes = script_path.read_bytes()
    sha = hashlib.sha256(script_bytes).hexdigest()
    if sha != record['sha256'] or sha != projection['scriptRevision']:
        raise ValueError('VOICE_SCRIPT_CHANGED')
    ad_dir = task_root / 'artifacts/ad'
    if not ad_dir.resolve().is_relative_to(task_root):
        raise ValueError('VOICE_ARTIFACT_INVALID')
    meta_path = ad_dir / 'recommendation.json'
    meta = _read_json(meta_path)
    if meta.get('task_id') != task_id or meta.get('script_sha256') != sha:
        raise ValueError('VOICE_METADATA_CHANGED')
    hashes = meta.get('artifact_sha256')
    if not isinstance(hashes, dict) or not hashes.get('adplan.json'):
        raise ValueError('VOICE_METADATA_INVALID')
    if file_sha256(ad_dir / 'adplan.json') != hashes['adplan.json']:
        raise ValueError('VOICE_PLAN_CHANGED')
    inputs = resolve_ad_render_inputs(db, root, task_id, script_bytes.decode('utf-8'), brief['aspect_ratio'])
    ownership = inputs['identity']['source']
    if ownership == 'legacy':
        if any(not hashes.get(name) or file_sha256(ad_dir / name) != hashes[name]
               for name in ('adtiming.json', 'voice.mp3')):
            raise ValueError('VOICE_ARTIFACT_CHANGED')
    elif ownership != 'registered':
        raise ValueError('VOICE_OWNER_INVALID')
    segments = inputs['timing']['segments']
    groups = projection['groups']
    if len(segments) != len(groups) or not segments:
        raise ValueError('VOICE_TIMELINE_CHANGED')
    for segment, group in zip(segments, groups):
        a, b = segment['start'], segment['end']
        if (type(a) not in (int, float) or type(b) not in (int, float)
                or not math.isfinite(a) or not math.isfinite(b)
                or (segment['text'], round(a * 1000000), round(b * 1000000))
                != (group['text'], group['output_start_us'], group['output_end_us'])):
            raise ValueError('VOICE_TIMELINE_CHANGED')
    total = inputs['timing']['total']
    if type(total) not in (int, float) or not math.isfinite(total) or not 0 < total <= MAX_ANALYSIS_SECONDS:
        raise ValueError('VOICE_DURATION_UNCOVERED')
    if round(total * 1000000) != groups[-1]['output_end_us']:
        raise ValueError('VOICE_TIMELINE_CHANGED')
    path = Path(inputs['voice_path'])
    audio_sha = file_sha256(path)
    # Re-read all timeline groups so an in-flight scene edit cannot turn an old
    # projection into a new audio-backed context.
    from .script_segments import project_script_shots
    cards = [dict(r) for r in db.connection.execute('SELECT * FROM scene_cards WHERE task_id=? ORDER BY idx,card_id', (task_id,))]
    live = project_script_shots(db, task_id, cards)
    if live != projection:
        raise ValueError('VOICE_TIMELINE_CHANGED')
    signature = _digest({'task': task_id, 'script': sha, 'metadata': meta,
        'identity': inputs['identity'], 'audio': audio_sha, 'groups': groups})
    return path, audio_sha, ownership, signature


def _measure(path, audio_sha, groups):
    windows = [(g['segment_id'], g['output_start_us'], g['output_end_us']) for g in groups]
    key = (METHOD_VERSION, audio_sha, _digest(windows))
    with _CACHE_LOCK:
        encoded = _CACHE.get(key)
        if encoded is not None:
            _CACHE.move_to_end(key)
            return json.loads(encoded)
    samples, _, channels = _decode_audio(path)
    duration = len(samples) / SAMPLE_RATE
    expected = groups[-1]['output_end_us'] / 1000000
    if not 0 < duration <= MAX_ANALYSIS_SECONDS or abs(duration - expected) > .03:
        raise ValueError('VOICE_DECODE_DURATION_MISMATCH')
    segments = []
    hop = SAMPLE_RATE // 50
    for group in groups[:40]:
        start, end = group['output_start_us'], group['output_end_us']
        block = samples[round(start * SAMPLE_RATE / 1000000):round(end * SAMPLE_RATE / 1000000)]
        if not len(block):
            raise ValueError('VOICE_EMPTY_SEGMENT')
        levels = [float(np.sqrt(np.mean(np.square(block[i:i+hop], dtype=np.float64))))
                  for i in range(0, len(block), hop)]
        quiet = np.array(levels) <= 10 ** (-45 / 20)
        intervals = []; run = None
        for i in range(len(quiet) + 1):
            is_quiet = i < len(quiet) and bool(quiet[i])
            if is_quiet and run is None: run = i
            if not is_quiet and run is not None:
                a = run * hop; b = min(i * hop, len(block))
                intervals.append((a, b)); run = None
        pauses = [(a, b) for a, b in intervals if (b-a) / SAMPLE_RATE >= .1]
        segments.append({'segmentId': group['segment_id'], 'startUs': start, 'endUs': end,
            'rmsDbfs': _db(np.sqrt(np.mean(np.square(block, dtype=np.float64)))),
            'samplePeakDbfs': _db(np.max(np.abs(block))),
            'quietDurationUs': round(sum(b-a for a,b in intervals) * 1000000 / SAMPLE_RATE),
            'longestQuietUs': round(max((b-a for a,b in intervals), default=0) * 1000000 / SAMPLE_RATE),
            'quietIntervals': [{'startUs': start+round(a*1000000/SAMPLE_RATE), 'endUs': start+round(b*1000000/SAMPLE_RATE)} for a,b in pauses[:12]],
            'omittedQuietIntervals': max(0, len(pauses)-12)})
    result = {'methodVersion': METHOD_VERSION, 'decodedDurationUs': round(duration * 1000000),
        'sampleRateHz': SAMPLE_RATE, 'sourceChannels': channels,
        'rmsDbfs': _db(np.sqrt(np.mean(np.square(samples, dtype=np.float64)))),
        'samplePeakDbfs': _db(np.max(np.abs(samples))),
        'quietMethod': '20ms RMS <= -45dBFS; intervals >= 100ms are low-energy evidence, not semantic pauses',
        'segments': segments, 'omittedSegments': max(0, len(groups)-40),
        'semanticListening': 'unverified', 'prosody': 'unknown', 'musicBeat': 'unknown',
        'limitations': ['本地PCM能量与低能量区间，不代表听懂语调、情绪或音乐节拍。',
            '采样峰为16kHz解码PCM峰值，不是true peak；RMS不是LUFS。']}
    if file_sha256(path) != audio_sha:
        raise ValueError('VOICE_CHANGED_DURING_ANALYSIS')
    encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    if len(encoded.encode('utf-8')) <= _MAX_RESULT_BYTES:
        with _CACHE_LOCK:
            _CACHE[key] = encoded
            _CACHE.move_to_end(key)
            while len(_CACHE) > _MAX_CACHE_ENTRIES: _CACHE.popitem(last=False)
    return result


def current_voice_evidence(db, project_root, task, projection):
    if project_root is None or task['task_type'] != 'ad_mix':
        return None
    try:
        bound = _bound(db, Path(project_root), task, projection)
        path, sha, ownership, signature = bound
        measurement = _measure(path, sha, projection['groups'])
        if _bound(db, Path(project_root), task, projection) != bound:
            return None
        return {'status': 'measured_current_audio', 'source': 'current_ad_render_inputs',
            'ownership': ownership, 'audioInput': False, 'audioBytesRechecked': True,
            'audioSha256': sha, 'bindingDigest': signature,
            'durationUs': projection['groups'][-1]['output_end_us'], 'measurement': measurement}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, UnicodeError):
        return None