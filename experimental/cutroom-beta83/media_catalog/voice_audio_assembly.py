"""Immutable 48 kHz mono PCM voice assemblies and measured timeline drafts.

No TTS calls, database/gate changes, padding, looping, speed changes or fades.
The coordinator still owns current-version checks, RenderRecipe, QA and apply.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import time
import uuid
import wave

from .digital_workflow import _default_process_runner
from .tools import default_ffprobe_search_roots, find_ffmpeg, find_ffprobe
from .voice_editing import VoiceEditing, _digest, _json
from .voice_regeneration import bind_measured_timeline
from .workbench_actions import _run_checked

RATE = 48000
FORMAT = dict(sample_rate=RATE, channels=1, sample_width=2, codec='pcm_s16le')
ASSEMBLER_VERSION = 'voice-pcm-assembly-v1'


def _assembly_process_runner(command, **options):
    """Reuse ATR, then the existing bounded single-process FFmpeg decoder.

    The workbench's generic fallback buffers diagnostics without a byte cap.
    These direct FFmpeg/ffprobe commands do not spawn child processes; the audio
    evidence runner bounds stdout, discards stderr and kills/reaps on timeout.
    """
    started = time.monotonic()
    result = _default_process_runner(command, **options)
    if result.get('started') is not True and result.get('error_code') == 'runtime.internal_error':
        from .packaging_audio_evidence import _run_bounded, AudioEvidenceError
        try:
            remaining = options['timeout_seconds'] - (time.monotonic() - started)
            if remaining <= 0:
                raise RuntimeError('PROCESS_TIMEOUT')
            output = _run_bounded(command, timeout=remaining, max_bytes=options['max_output_bytes'])
        except AudioEvidenceError as exc:
            code = 'PROCESS_TIMEOUT' if str(exc) == 'AUDIO_PROCESS_TIMEOUT' else (
                'PROCESS_OUTPUT_LIMIT' if str(exc) == 'AUDIO_OUTPUT_LIMIT' else 'PROCESS_FAILED')
            raise RuntimeError(code) from exc
        return dict(started=True, returncode=0, stdout=output.decode('utf-8'), stderr='')
    return result


def _sha(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def _us(frame):
    return (frame * 1_000_000 + RATE // 2) // RATE


def _safe(path, root=None):
    from .voice_path_safety import safe_voice_path
    return safe_voice_path(path, root, "ASSEMBLY_PATH_INVALID")


def _pcm(path):
    digest = hashlib.sha256()
    with wave.open(str(path), 'rb') as source:
        if (source.getframerate(), source.getnchannels(), source.getsampwidth(), source.getcomptype()) != (RATE, 1, 2, 'NONE'):
            raise ValueError('ASSEMBLY_PCM_INVALID')
        frames = source.getnframes()
        if not 0 < frames <= RATE * 3600:
            raise ValueError('ASSEMBLY_DURATION_INVALID')
        count = 0
        for chunk in iter(lambda: source.readframes(32768), b''):
            count += len(chunk)
            digest.update(chunk)
        if count != frames * 2:
            raise ValueError('ASSEMBLY_PCM_TRUNCATED')
    return frames, digest.hexdigest()


def _coverage(timeline):
    cursor = 0
    for shot in sorted(timeline['shots'], key=lambda item: (item['start_us'], item['end_us'])):
        start, end = shot['start_us'], shot['end_us']
        if start > cursor:
            timeline['gaps'].append(dict(code='SHOT_COVERAGE_MISSING', start_us=cursor, end_us=start))
        elif start < cursor:
            timeline['gaps'].append(dict(code='SHOT_OVERLAP', shot_id=shot['shot_id'], start_us=start,
                                         end_us=min(cursor, end)))
        cursor = max(cursor, end)
    if cursor < timeline['duration_us']:
        timeline['gaps'].append(dict(code='SHOT_COVERAGE_MISSING', start_us=cursor, end_us=timeline['duration_us']))


def _unchanged(paths, sources, root):
    for path, metadata in zip(paths, sources):
        try:
            if _safe(path, root) != path or _sha(path) != metadata['audio']['sha256']:
                raise ValueError('SOURCE_CHANGED')
        except (OSError, ValueError) as exc:
            raise ValueError('SOURCE_CHANGED') from exc


def _timeline(plan, results, sources, records, shots):
    durations = {part['segment_id']: (part['end_frame'] - part['start_frame']) / RATE for part in records}
    class NormalizedDurations:
        def __init__(self):
            self.cursor = 0
        def verify_audio(self, config, receipt):
            source = sources[self.cursor]
            self.cursor += 1
            checked = source['audio']
            # Change only duration to measured normalized frames. Local word/sentence
            # timings stay exactly as supplied; an out-of-range end fails closed.
            return dict(checked, duration=durations[source['segment_id']])
    timeline = bind_measured_timeline(plan, results, NormalizedDurations(), shots=shots)
    boundaries = {r['segment_id']: (_us(r['start_frame']), _us(r['end_frame'])) for r in records}
    old_ranges = {row['segment_id']: (row['start_us'], row['end_us']) for row in timeline['segments']}
    def absolute(sid, value):
        old_start, old_end = old_ranges[sid]
        start, end = boundaries[sid]
        # A measured full-segment edge belongs to the cumulative PCM boundary.
        # Interior measured anchors retain their supplied local time.
        if value == old_start: return start
        if value == old_end: return end
        return start + value - old_start
    for row in timeline['subtitles']:
        sid = row['segment_id']
        row['start_us'], row['end_us'] = absolute(sid, row['start_us']), absolute(sid, row['end_us'])
    selected = {shot['shot_id']: shot for shot in shots}
    kept = []
    for row in timeline['shots']:
        sid = row['segment_id']
        row['start_us'], row['end_us'] = absolute(sid, row['start_us']), absolute(sid, row['end_us'])
        source_end = row['source_start_us'] + row['end_us'] - row['start_us']
        if source_end > selected[row['shot_id']]['source_end_us']:
            timeline['gaps'].append(dict(code='SOURCE_WINDOW_TOO_SHORT',shot_id=row['shot_id'],segment_id=sid,
                shortfall_us=source_end-selected[row['shot_id']]['source_end_us']))
            continue
        row['source_end_us'] = source_end
        kept.append(row)
    timeline['shots'] = kept
    for gap in timeline['gaps']:
        if 'start_us' in gap:
            sid = gap['segment_id']
            gap['start_us'], gap['end_us'] = absolute(sid, gap['start_us']), absolute(sid, gap['end_us'])
    for row in timeline['segments'] + timeline['audio']:
        row['start_us'], row['end_us'] = boundaries[row['segment_id']]
    timeline['duration_us'] = _us(records[-1]['end_frame'])
    _coverage(timeline)
    timeline['assembler_version'] = ASSEMBLER_VERSION
    return timeline


def _existing(directory, identity, sources, plan, results, verifier, shots, source_paths, deadline):
    try:
        for name in ('voice.wav', 'receipt.json', 'timeline.json'):
            _safe(directory / name, directory)
        receipt = json.loads((directory / 'receipt.json').read_text('utf-8'))
        timeline = json.loads((directory / 'timeline.json').read_text('utf-8'))
        body = {k: v for k, v in receipt.items() if k != 'manifest_digest'}
        if receipt['assembly_id'] != identity or receipt['manifest_digest'] != _digest(body):
            raise ValueError('receipt digest')
        if receipt['sources'] != sources or receipt['format'] != FORMAT or receipt['status'] != 'assembled_draft':
            raise ValueError('receipt input')
        path = directory / 'voice.wav'
        frames, pcm_sha = _pcm(path)
        if (receipt['sha256'] != _sha(path) or receipt['frame_count'] != frames
                or receipt['pcm_sha256'] != pcm_sha or receipt['duration_us'] != _us(frames)
                or receipt['timeline_digest'] != _digest(timeline)
                or timeline != _timeline(plan, results, sources, receipt['segments'], shots)):
            raise ValueError('artifact digest')
        _check_boundaries(path, receipt['segments'])
    except (OSError, ValueError, KeyError, TypeError, wave.Error, StopIteration) as exc:
        raise ValueError('ASSEMBLY_VERSION_CORRUPT') from exc
    _unchanged(source_paths, sources, verifier.audio_root)
    _validate_origins(verifier)
    if time.monotonic() > deadline:
        raise RuntimeError('PROCESS_TIMEOUT')
    return dict(assembly_id=identity, directory=str(directory), audio_path=str(path),
                receipt=receipt, timeline=timeline, reused=True)


def _validate_origins(verifier):
    check = getattr(verifier, 'validate_sources', None)
    if callable(check):
        check()


def _check_boundaries(path, records):
    with wave.open(str(path), 'rb') as source:
        cursor = 0
        for row in records:
            if row['start_frame'] != cursor or row['end_frame'] <= cursor:
                raise ValueError('ASSEMBLY_BOUNDARY_INVALID')
            remaining = row['end_frame'] - cursor
            digest = hashlib.sha256()
            while remaining:
                chunk = source.readframes(min(remaining, 32768))
                if not chunk or len(chunk) % 2:
                    raise ValueError('ASSEMBLY_BOUNDARY_INVALID')
                remaining -= len(chunk) // 2
                digest.update(chunk)
            if digest.hexdigest() != row['normalized_pcm_sha256']:
                raise ValueError('ASSEMBLY_BOUNDARY_INVALID')
            cursor = row['end_frame']
        if cursor != source.getnframes() or source.readframes(1):
            raise ValueError('ASSEMBLY_BOUNDARY_INVALID')


def assemble_voice_audio(plan, results, verifier, output_root, *, shots=(), runner=None, timeout_seconds=120):
    """Return an assembled draft bundle, atomically published into a digest directory.

    ``verifier`` is the existing VoiceEditing audio verifier (root + fingerprint,
    SHA, ffprobe, original-text timing and upstream receipt). ``results`` contains
    only the generated segments. Reused inputs remain exactly their verified SHA.
    Mono is the explicit narration output contract; stereo sources are downmixed
    by FFmpeg, never stretched. A deadline covers normalization/probe/full decode.
    A replay validates inputs and the existing bundle and does not normalize or
    decode again. Input ffprobe verification still runs under the same deadline.
    """
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
        raise ValueError('ASSEMBLY_TIMEOUT_INVALID')
    deadline = time.monotonic() + timeout_seconds
    plan, results, shots = deepcopy(plan), deepcopy(results), deepcopy(list(shots))
    root = _safe(output_root)
    ffmpeg = find_ffmpeg(os.environ, search_roots=default_ffprobe_search_roots())
    ffprobe = find_ffprobe(os.environ, search_roots=default_ffprobe_search_roots())
    def run(command, cwd):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('PROCESS_TIMEOUT')
        outcome = _run_checked(runner or _assembly_process_runner, [str(v) for v in command], cwd=cwd,
                               timeout_seconds=max(1, math.ceil(remaining)), max_output_bytes=65536)
        if outcome.get('stdout_truncated') or outcome.get('stderr_truncated'):
            raise RuntimeError('PROCESS_OUTPUT_LIMIT')
        if time.monotonic() > deadline:
            raise RuntimeError('PROCESS_TIMEOUT')
        return outcome
    def duration_reader(path):
        probe = run([ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                     '-of', 'default=noprint_wrappers=1:nokey=1', path], Path(path).parent)
        try:
            seconds = float(probe['stdout'].strip())
            return seconds if math.isfinite(seconds) and seconds > 0 else None
        except (ValueError, KeyError):
            return None
    # Trusted offline extraction adapters preserve distinct source provenance.
    # They must retain this deadline-bound duration reader for every probe.
    bounded_verifier = (verifier.with_duration_reader(duration_reader,deadline=deadline)
                        if callable(getattr(verifier, 'with_duration_reader', None))
                        else VoiceEditing(None, verifier.audio_root, duration_reader=duration_reader))
    _validate_origins(bounded_verifier)
    verified = []
    class Capture:
        def verify_audio(self, config, receipt):
            checked = bounded_verifier.verify_audio(config, receipt)
            verified.append(checked)
            return checked
    original = bind_measured_timeline(plan, results, Capture(), shots=shots)
    if original['duration_us'] > 3_600_000_000:
        raise ValueError('ASSEMBLY_DURATION_INVALID')
    sources = []
    source_paths = []
    for segment, checked in zip(plan['segments'], verified):
        path = _safe(verifier.audio_root / checked['path'], verifier.audio_root)
        sources.append(dict(segment_id=segment['segment_id'], mode=segment['mode'],
                            config_fingerprint=segment['config']['fingerprint'], audio=checked))
        source_paths.append(path)
    identity = _digest(dict(version=ASSEMBLER_VERSION, format=FORMAT, plan_digest=plan['digest'], sources=sources, shots=shots))
    root.mkdir(parents=True, exist_ok=True)
    directory = _safe(root / identity, root)
    if directory.exists():
        return _existing(directory,identity,sources,plan,results,bounded_verifier,shots,source_paths,deadline)
    stage = root / ('.assembly-' + uuid.uuid4().hex)
    stage.mkdir()
    try:
        normalized, records, cursor = [], [], 0
        for index, (source, metadata) in enumerate(zip(source_paths, sources)):
            snapshot = stage / ('source-' + str(index) + source.suffix)
            shutil.copyfile(source, snapshot)
            if _sha(snapshot) != metadata['audio']['sha256']:
                raise ValueError('SOURCE_CHANGED')
            target = stage / ('normalized-' + str(index) + '.wav')
            run([ffmpeg, '-v', 'error', '-nostdin', '-n', '-i', snapshot, '-map', '0:a:0', '-vn',
                 '-ac', 1, '-ar', RATE, '-c:a', 'pcm_s16le', target], stage)
            frames, pcm_sha = _pcm(target)
            boundary_error_us = _us(frames) - round(metadata['audio']['duration'] * 1_000_000)
            # Resampling/container duration rounding may differ by one output frame.
            # Larger differences require new alignment evidence, never proportional correction.
            if abs(boundary_error_us) > math.ceil(1_000_000 / RATE):
                raise ValueError('NORMALIZED_DURATION_MISMATCH')
            records.append(dict(segment_id=metadata['segment_id'], mode=metadata['mode'], start_frame=cursor,
                                end_frame=cursor + frames, source_sha256=metadata['audio']['sha256'],
                                normalized_pcm_sha256=pcm_sha, boundary_error_us=boundary_error_us))
            normalized.append(target)
            cursor += frames
            if cursor > RATE * 3600:
                raise ValueError('ASSEMBLY_DURATION_INVALID')
        _unchanged(source_paths, sources, verifier.audio_root)
        _validate_origins(bounded_verifier)
        timeline = _timeline(plan, results, sources, records, shots)
        audio_path = stage / 'voice.wav'
        with wave.open(str(audio_path), 'wb') as output:
            output.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
            for path in normalized:
                with wave.open(str(path), 'rb') as source:
                    for chunk in iter(lambda: source.readframes(32768), b''):
                        output.writeframesraw(chunk)
        frames, pcm_sha = _pcm(audio_path)
        _check_boundaries(audio_path, records)
        probe = run([ffprobe, '-v', 'error', '-select_streams', 'a:0', '-show_entries',
                     'stream=codec_name,sample_rate,channels:format=duration', '-of', 'json', audio_path], stage)
        data = json.loads(probe['stdout'])
        stream = data['streams'][0]
        probe_duration = float(data['format']['duration'])
        if (stream['codec_name'] != 'pcm_s16le' or int(stream['sample_rate']) != RATE
                or int(stream['channels']) != 1 or not math.isfinite(probe_duration)
                or abs(probe_duration * RATE - frames) > 1):
            raise ValueError('ASSEMBLY_PROBE_INVALID')
        run([ffmpeg, '-v', 'error', '-nostdin', '-xerror', '-err_detect', 'explode', '-i', audio_path,
             '-map', '0:a:0', '-f', 'null', '-'], stage)
        _unchanged(source_paths, sources, verifier.audio_root)
        _validate_origins(bounded_verifier)
        if time.monotonic() > deadline:
            raise RuntimeError('PROCESS_TIMEOUT')
        receipt = dict(assembly_id=identity, assembler_version=ASSEMBLER_VERSION, status='assembled_draft',
                       task_id=plan['task_id'], base_revision=plan['base_revision'], plan_digest=plan['digest'],
                       format=dict(FORMAT), frame_count=frames, duration_us=_us(frames), sha256=_sha(audio_path),
                       pcm_sha256=pcm_sha, sources=sources, segments=records, timeline_digest=_digest(timeline),
                       full_decode_verified=True, ffprobe=dict(codec=stream['codec_name'], sample_rate=RATE,
                                                               channels=1, duration=probe_duration))
        receipt['manifest_digest'] = _digest(receipt)
        (stage / 'receipt.json').write_text(_json(receipt), encoding='utf-8')
        (stage / 'timeline.json').write_text(_json(timeline), encoding='utf-8')
        # Publish only the complete evidence bundle; snapshots/intermediates never escape.
        for path in stage.iterdir():
            if path.name not in ('voice.wav', 'receipt.json', 'timeline.json'):
                path.unlink()
        try:
            stage.rename(directory)
        except OSError:
            if directory.exists():
                return _existing(directory,identity,sources,plan,results,bounded_verifier,shots,source_paths,deadline)
            raise
        return dict(assembly_id=identity, directory=str(directory), audio_path=str(directory / 'voice.wav'),
                    receipt=receipt, timeline=timeline, reused=False)
    finally:
        if stage.exists():
            _safe(stage, root)
            shutil.rmtree(stage)
