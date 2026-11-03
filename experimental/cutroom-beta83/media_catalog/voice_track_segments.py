"""Offline sentence extraction from an already bound complete narration track.

Trusted server inputs only. No TTS, database writes or generated-provider receipt.
All timings must have source evidence and cover the actual decoded PCM exactly.
"""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
import uuid
import wave

from .tools import default_ffprobe_search_roots, find_ffmpeg, find_ffprobe
from .voice_audio_assembly import RATE, FORMAT, _assembly_process_runner, _us
from .voice_editing import VoiceEditing, compile_voice_input, _check_config, _digest, _json
from .workbench_actions import _run_checked

VERSION = 'bound-track-extraction-v1'
IDENTITY_KEYS = ('version','task_id','base_revision','context_digest','script_sha256','voice','input_digest',
                 'source_sha256','timing_sha256','timing_evidence','windows')


def _checkpoint(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise RuntimeError('PROCESS_TIMEOUT')


def _sha(path, *, deadline=None):
    _checkpoint(deadline)
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        while True:
            _checkpoint(deadline)
            chunk = source.read(65536)
            _checkpoint(deadline)
            if not chunk: break
            digest.update(chunk)
    return digest.hexdigest()


def _pcm(path, *, deadline=None):
    _checkpoint(deadline)
    digest, count = hashlib.sha256(), 0
    with wave.open(str(path),'rb') as source:
        if (source.getframerate(),source.getnchannels(),source.getsampwidth(),source.getcomptype()) != (RATE,1,2,'NONE'):
            raise ValueError('ASSEMBLY_PCM_INVALID')
        frames = source.getnframes()
        if not 0 < frames <= RATE*3600: raise ValueError('ASSEMBLY_DURATION_INVALID')
        while True:
            _checkpoint(deadline)
            chunk = source.readframes(32768)
            _checkpoint(deadline)
            if not chunk: break
            digest.update(chunk); count += len(chunk)
        if count != frames*2: raise ValueError('ASSEMBLY_PCM_TRUNCATED')
    return frames,digest.hexdigest()


def _copy(source, target, deadline):
    with Path(source).open('rb') as reader,Path(target).open('xb') as writer:
        while True:
            _checkpoint(deadline)
            chunk = reader.read(65536)
            _checkpoint(deadline)
            if not chunk: break
            writer.write(chunk)
            _checkpoint(deadline)


def _read_json(path, deadline, *, limit=8_000_000):
    _checkpoint(deadline)
    if path.stat().st_size > limit: raise ValueError('TRACK_EXTRACTION_CORRUPT')
    data = bytearray()
    with path.open('rb') as source:
        while True:
            _checkpoint(deadline)
            chunk = source.read(65536)
            _checkpoint(deadline)
            if not chunk: break
            data.extend(chunk)
            if len(data)>limit: raise ValueError('TRACK_EXTRACTION_CORRUPT')
    return json.loads(data.decode('utf-8'))


def _safe(path, root=None):
    from .voice_path_safety import safe_voice_path
    return safe_voice_path(path, root, "TRACK_PATH_INVALID")


def _match_files(paths, hashes, *, deadline=None):
    try:
        for path, expected in zip(paths,hashes):
            _checkpoint(deadline)
            if _sha(_safe(path),deadline=deadline) != expected:
                raise ValueError('TRACK_SOURCE_CHANGED')
    except OSError as exc:
        raise ValueError('TRACK_SOURCE_CHANGED') from exc


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('TRACK_TIMING_INVALID')
    return Decimal(str(value))


def _frame(value):
    return int((value * RATE).to_integral_value(rounding=ROUND_HALF_UP))


def _windows(data, script):
    rows = data.get('segments') if isinstance(data, dict) else None
    if not isinstance(rows, list) or not 0 < len(rows) <= 500:
        raise ValueError('TRACK_TIMING_INVALID')
    windows, previous, text = [], Decimal(0), ''
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get('text'), str) or not row['text']:
            raise ValueError('TRACK_TIMING_INVALID')
        if 'i' in row and (type(row['i']) is not int or row['i'] != index):
            raise ValueError('TRACK_TIMING_INVALID')
        start, end, duration = (_number(row.get(key)) for key in ('start', 'end', 'dur'))
        if start != previous or not 0 <= start < end or abs(duration - (end-start)) > Decimal('.001'):
            raise ValueError('TRACK_TIMING_INVALID')
        first, last = _frame(start), _frame(end)
        if last <= first:
            raise ValueError('TRACK_TIMING_INVALID')
        windows.append(dict(index=index, text=row['text'], start_frame=first, end_frame=last,
                            start_seconds=str(start), end_seconds=str(end)))
        previous = end
        text += row['text']
    if text != script:
        raise ValueError('TRACK_ORIGINAL_TEXT_REQUIRED')
    if _number(data.get('total')) != previous or previous > 3600:
        raise ValueError('TRACK_TIMING_INVALID')
    return windows


def _inputs(context, revision, binding, evidence):
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', context.task_id)
            or type(revision) is not int or revision < 0):
        raise ValueError('TRACK_BINDING_REQUIRED')
    hashes = binding.get('artifact_sha256') if isinstance(binding, dict) else None
    if (not isinstance(hashes, dict) or any(not isinstance(hashes.get(key), str)
            or not re.fullmatch('[0-9a-f]{64}', hashes[key]) for key in ('voice.mp3','adtiming.json'))
            or binding.get('task_id') != context.task_id or binding.get('script_sha256') != context.script_sha256
            or not isinstance(binding.get('input_digest'), str) or not binding['input_digest']):
        raise ValueError('TRACK_BINDING_REQUIRED')
    if (not isinstance(evidence, dict) or evidence.get('kind') not in ('existing_sentence_tts','measured_alignment')
            or not isinstance(evidence.get('evidence_id'), str) or not evidence['evidence_id'].strip()
            or evidence.get('context_digest') != context.context_digest
            or evidence.get('input_digest') != binding['input_digest']
            or evidence.get('source_sha256') != hashes['voice.mp3']
            or evidence.get('timing_sha256') != hashes['adtiming.json']):
        raise ValueError('TRACK_TIMING_EVIDENCE_REQUIRED')
    return [hashes['voice.mp3'], hashes['adtiming.json']]


def _read_bundle(directory, root, expected_identity, *, deadline=None):
    deadline = time.monotonic()+120 if deadline is None else deadline
    _checkpoint(deadline)
    try:
        manifest = _read_json(_safe(directory/'manifest.json',root),deadline)
        if manifest['bundle_id'] != expected_identity or manifest['manifest_digest'] != _digest(
                {k:v for k,v in manifest.items() if k != 'manifest_digest'}):
            raise ValueError('manifest')
        bundle = dict(directory=str(directory), audio_root=str(root), manifest=manifest,
                      segments=manifest['segments'], reused=True)
        verifier = ExtractedTrackVerifier(bundle,root,deadline=deadline)
        verifier.validate_sources()
        for segment in bundle['segments']:
            _checkpoint(deadline)
            verifier._verify_extracted(segment['config'],segment['audio'],probe=False,sources_validated=True)
        verifier.validate_sources()
        _checkpoint(deadline)
        return bundle
    except (OSError, ValueError, KeyError, TypeError, wave.Error) as exc:
        if isinstance(exc, ValueError) and str(exc) == 'TRACK_SOURCE_CHANGED':
            raise
        raise ValueError('TRACK_EXTRACTION_CORRUPT') from exc


def prepare_track_segments(project_root, output_root, context, base_revision, artifact_binding,
                           timing_evidence, *, runner=None, timeout_seconds=120):
    """Publish an immutable extraction draft from trusted same-version evidence.

    Caller rechecks current task context/version before any eventual apply.
    Old metadata without exact artifact hashes is intentionally unsupported.
    """
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
        raise ValueError('TRACK_TIMEOUT_INVALID')
    deadline = time.monotonic() + timeout_seconds
    context, artifact_binding, timing_evidence = deepcopy(context), deepcopy(artifact_binding), deepcopy(timing_evidence)
    hashes = _inputs(context, base_revision, artifact_binding, timing_evidence)
    root = _safe(Path(project_root).resolve())
    ad = _safe(root / 'runtime/workbench/tasks' / context.task_id / 'artifacts/ad', root)
    paths = [_safe(ad / name, root) for name in ('voice.mp3','adtiming.json')]
    _match_files(paths,hashes,deadline=deadline)
    if paths[1].stat().st_size > 2_000_000:
        raise ValueError('TRACK_TIMING_INVALID')
    try:
        windows = _windows(_read_json(paths[1],deadline,limit=2_000_000),context.script)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError('TRACK_TIMING_INVALID') from exc
    target = _safe(output_root, root)
    if target == ad or target.is_relative_to(ad):
        raise ValueError('TRACK_PATH_INVALID')
    identity_inputs = dict(version=VERSION, task_id=context.task_id, base_revision=base_revision,
                           context_digest=context.context_digest, script_sha256=context.script_sha256,
                           voice=context.voice, input_digest=artifact_binding['input_digest'],
                           source_sha256=hashes[0], timing_sha256=hashes[1],
                           timing_evidence={key:timing_evidence[key] for key in ('kind','evidence_id')}, windows=windows)
    identity = _digest(identity_inputs)
    target.mkdir(parents=True, exist_ok=True)
    directory = _safe(target / identity, root)
    if directory.exists():
        result = _read_bundle(directory,root,identity,deadline=deadline)
        _match_files(paths,hashes,deadline=deadline)
        _checkpoint(deadline)
        return result
    stage = target / ('.track-' + uuid.uuid4().hex)
    stage.mkdir()
    def run(command):
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise RuntimeError('PROCESS_TIMEOUT')
        result = _run_checked(runner or _assembly_process_runner, [str(v) for v in command], cwd=stage,
                              timeout_seconds=max(1,math.ceil(remaining)), max_output_bytes=65536)
        if result.get('stdout_truncated') or result.get('stderr_truncated'): raise RuntimeError('PROCESS_OUTPUT_LIMIT')
        if time.monotonic() > deadline: raise RuntimeError('PROCESS_TIMEOUT')
        return result
    try:
        snapshot = stage / 'source.mp3'
        _copy(paths[0],snapshot,deadline)
        if _sha(snapshot,deadline=deadline) != hashes[0]: raise ValueError('TRACK_SOURCE_CHANGED')
        ffprobe = find_ffprobe(os.environ, search_roots=default_ffprobe_search_roots())
        ffmpeg = find_ffmpeg(os.environ, search_roots=default_ffprobe_search_roots())
        result = run([ffprobe,'-v','error','-show_entries','format=duration','-of','json',snapshot])
        probe_duration = float(json.loads(result['stdout'])['format']['duration'])
        if not math.isfinite(probe_duration) or not 0 < probe_duration <= 3600:
            raise ValueError('TRACK_SOURCE_DURATION_INVALID')
        full = stage / 'source.wav'
        run([ffmpeg,'-v','error','-nostdin','-xerror','-err_detect','explode','-n','-i',snapshot,
             '-map','0:a:0','-vn','-ac',1,'-ar',RATE,'-c:a','pcm_s16le',full])
        _match_files(paths,hashes,deadline=deadline)
        frames, pcm_sha = _pcm(full,deadline=deadline)
        if windows[-1]['end_frame'] > frames: raise ValueError('TRACK_TIMING_EXCEEDS_SOURCE')
        if windows[-1]['end_frame'] < frames: raise ValueError('TRACK_SOURCE_COVERAGE_REQUIRED')
        segments = []
        with wave.open(str(full),'rb') as source:
            for row in windows:
                _checkpoint(deadline)
                segment_id = 'track-' + _digest([identity,row['index'],row['start_frame'],row['end_frame']])
                relative = (directory / (str(row['index']) + '.wav')).relative_to(root).as_posix()
                segment_path = stage / (str(row['index']) + '.wav')
                source.setpos(row['start_frame'])
                remaining = row['end_frame']-row['start_frame']
                with wave.open(str(segment_path),'wb') as part:
                    part.setparams((1,2,RATE,0,'NONE','not compressed'))
                    while remaining:
                        _checkpoint(deadline)
                        count = min(remaining,32768); chunk = source.readframes(count)
                        _checkpoint(deadline)
                        if len(chunk) != count*2: raise ValueError('TRACK_EXTRACTION_CORRUPT')
                        part.writeframesraw(chunk); remaining -= count
                count, segment_pcm = _pcm(segment_path,deadline=deadline)
                config = compile_voice_input(row['text'], [], context.voice, {})
                receipt = dict(origin_kind='extracted_track',path=relative,sha256=_sha(segment_path,deadline=deadline),
                               fingerprint=config['fingerprint'], timing_kind='measured_sentence',
                               timing=[dict(text=row['text'],start=0,end=count/RATE)],
                               extraction=dict(bundle_id=identity,segment_id=segment_id,start_frame=row['start_frame'],
                                               end_frame=row['end_frame'],pcm_sha256=segment_pcm,
                                               source_sha256=hashes[0],timing_sha256=hashes[1]))
                segments.append(dict(segment_id=segment_id,config=config,audio=receipt))
        manifest = dict(identity_inputs, bundle_id=identity, status='extracted_draft', format=dict(FORMAT),
                        source_path=paths[0].relative_to(root).as_posix(), timing_path=paths[1].relative_to(root).as_posix(),
                        decoded_path=(directory/'source.wav').relative_to(root).as_posix(),
                        frame_count=frames,pcm_sha256=pcm_sha,decoded_sha256=_sha(full,deadline=deadline),
                        duration_us=_us(frames),ffprobe_duration=probe_duration,full_decode_verified=True,segments=segments)
        manifest['manifest_digest'] = _digest(manifest)
        (stage/'manifest.json').write_text(_json(manifest),encoding='utf-8')
        snapshot.unlink()
        _match_files(paths,hashes,deadline=deadline)
        _checkpoint(deadline)
        try: stage.rename(directory)
        except OSError:
            if directory.exists():
                result = _read_bundle(directory,root,identity,deadline=deadline)
                _checkpoint(deadline)
                return result
            raise
        return dict(directory=str(directory),audio_root=str(root),manifest=manifest,segments=segments,reused=False)
    finally:
        if stage.exists(): shutil.rmtree(_safe(stage,target))


class ExtractedTrackVerifier:
    """Trusted offline reuse verifier; never a VoiceEditing.finish adapter."""
    def __init__(self,bundle,audio_root,*,duration_reader=None,deadline=None,_batch=False):
        self.bundle = deepcopy(bundle)
        self.audio_root = _safe(Path(audio_root).resolve())
        self.deadline = time.monotonic()+120 if deadline is None else deadline
        self._batch, self._source_snapshot = _batch,None
        _checkpoint(self.deadline)
        if (Path(bundle['audio_root']).resolve() != self.audio_root
                or bundle['segments'] != bundle['manifest']['segments']):
            raise ValueError('TRACK_EXTRACTION_BINDING_INVALID')
        self.generated = VoiceEditing(None,self.audio_root,**({'duration_reader':duration_reader} if duration_reader else {}))

    def with_duration_reader(self,reader,*,deadline=None):
        return ExtractedTrackVerifier(self.bundle,self.audio_root,duration_reader=reader,
                                      deadline=self.deadline if deadline is None else deadline,_batch=True)

    def _snapshot(self):
        manifest=self.bundle['manifest']
        paths=[self.audio_root/manifest[key] for key in ('source_path','timing_path','decoded_path')]
        paths.append(Path(self.bundle['directory'])/'manifest.json')
        result=[]
        for index,path in enumerate(paths):
            _checkpoint(self.deadline)
            try: info=_safe(path,self.audio_root).stat()
            except OSError as exc:
                raise ValueError('TRACK_SOURCE_CHANGED' if index<2 else 'TRACK_EXTRACTION_CORRUPT') from exc
            result.append((str(path),info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_ino,info.st_dev))
        return result

    def _check_snapshot(self):
        _checkpoint(self.deadline)
        try:
            current=self._snapshot()
            if self._source_snapshot is None or current!=self._source_snapshot:
                code=('TRACK_SOURCE_CHANGED' if self._source_snapshot is not None
                      and current[:2]!=self._source_snapshot[:2] else 'TRACK_EXTRACTION_CORRUPT')
                raise ValueError(code)
        except OSError as exc:
            raise ValueError('TRACK_SOURCE_CHANGED') from exc

    def validate_sources(self):
        _checkpoint(self.deadline)
        manifest = self.bundle['manifest']
        try:
            before=self._snapshot()
            _match_files([_safe(self.audio_root/manifest[key],self.audio_root) for key in ('source_path','timing_path')],
                         [manifest['source_sha256'],manifest['timing_sha256']],deadline=self.deadline)
            recorded=_read_json(_safe(Path(self.bundle['directory'])/'manifest.json',self.audio_root),self.deadline)
            if recorded != manifest or manifest['manifest_digest'] != _digest({k:v for k,v in manifest.items() if k!='manifest_digest'}):
                raise ValueError('manifest')
            if _digest({key:manifest[key] for key in IDENTITY_KEYS}) != manifest['bundle_id']:
                raise ValueError('identity')
            if self.bundle['segments'] != manifest['segments'] or len(manifest['segments']) != len(manifest['windows']):
                raise ValueError('segments')
            if (manifest['format']!=FORMAT or manifest['status']!='extracted_draft' or manifest['full_decode_verified'] is not True
                    or manifest['version']!=VERSION or type(manifest['frame_count']) is not int):
                raise ValueError('format')
            original_text=''.join(window['text'] for window in manifest['windows'])
            if (hashlib.sha256(original_text.encode('utf-8')).hexdigest()!=manifest['script_sha256']
                    or _windows(_read_json(self.audio_root/manifest['timing_path'],self.deadline,limit=2_000_000),original_text)!=manifest['windows']):
                raise ValueError('timing windows')
            for segment, window in zip(manifest['segments'],manifest['windows']):
                _checkpoint(self.deadline)
                expected_id = 'track-' + _digest([manifest['bundle_id'],window['index'],window['start_frame'],window['end_frame']])
                if (segment['config'] != compile_voice_input(window['text'],[],manifest['voice'],{})
                        or segment['segment_id'] != expected_id):
                    raise ValueError('configuration')
                audio, extraction = segment['audio'], segment['audio']['extraction']
                expected_path = (Path(self.bundle['directory'])/(str(window['index'])+'.wav')).relative_to(self.audio_root).as_posix()
                expected_extraction = dict(bundle_id=manifest['bundle_id'],segment_id=expected_id,
                                           start_frame=window['start_frame'],end_frame=window['end_frame'],
                                           source_sha256=manifest['source_sha256'],timing_sha256=manifest['timing_sha256'],
                                           pcm_sha256=extraction['pcm_sha256'])
                if (extraction != expected_extraction or audio.get('path') != expected_path
                        or audio.get('origin_kind') != 'extracted_track' or 'provider_receipt' in audio
                        or audio.get('fingerprint') != segment['config']['fingerprint']
                        or audio.get('timing_kind') != 'measured_sentence'
                        or audio.get('timing') != [dict(text=window['text'],start=0,
                                                      end=(window['end_frame']-window['start_frame'])/RATE)]):
                    raise ValueError('frozen extraction window')
            full = _safe(self.audio_root/manifest['decoded_path'],self.audio_root)
            expected_full=(Path(self.bundle['directory'])/'source.wav').relative_to(self.audio_root).as_posix()
            if (manifest['decoded_path']!=expected_full or manifest['windows'][-1]['end_frame']!=manifest['frame_count']
                    or _sha(full,deadline=self.deadline)!=manifest['decoded_sha256']
                    or _pcm(full,deadline=self.deadline)!=(manifest['frame_count'],manifest['pcm_sha256'])):
                raise ValueError('PCM')
            self._source_snapshot=before
            self._check_snapshot()
        except (OSError,ValueError,KeyError,TypeError,IndexError,wave.Error) as exc:
            if isinstance(exc,ValueError) and str(exc)=='TRACK_SOURCE_CHANGED': raise
            raise ValueError('TRACK_EXTRACTION_CORRUPT') from exc

    def _verify_extracted(self,config,audio,*,probe=True,sources_validated=False):
        _checkpoint(self.deadline)
        _check_config(config)
        if sources_validated: self._check_snapshot()
        else: self.validate_sources()
        matches = [s for s in self.bundle['manifest']['segments'] if s['audio'] == audio and s['config'] == config]
        if len(matches) != 1: raise ValueError('TRACK_EXTRACTION_BINDING_INVALID')
        try:
            path = _safe(self.audio_root/audio['path'],self.audio_root)
            count, pcm_sha = _pcm(path,deadline=self.deadline)
            extraction = audio['extraction']
            if (_sha(path,deadline=self.deadline) != audio['sha256'] or pcm_sha != extraction['pcm_sha256']
                    or count != extraction['end_frame']-extraction['start_frame']):
                raise ValueError('PCM')
            with wave.open(str(self.audio_root/self.bundle['manifest']['decoded_path']),'rb') as original:
                original.setpos(extraction['start_frame'])
                with wave.open(str(path),'rb') as extracted:
                    remaining=count
                    while remaining:
                        _checkpoint(self.deadline)
                        n=min(remaining,32768)
                        if original.readframes(n) != extracted.readframes(n): raise ValueError('PCM window')
                        _checkpoint(self.deadline)
                        remaining-=n
            if probe:
                _checkpoint(self.deadline)
                duration = self.generated.duration_reader(path)
                _checkpoint(self.deadline)
                if duration is None or abs(duration*RATE-count)>1: raise ValueError('duration')
            if sources_validated: self._check_snapshot()
            else: self.validate_sources()
            # Duration probing (or another reader) may race a segment overwrite.
            # Recheck this segment, never rescan the complete track per segment.
            if _sha(_safe(path,self.audio_root),deadline=self.deadline)!=audio['sha256']:
                raise ValueError('segment changed during verification')
            return dict(audio,duration=count/RATE)
        except (OSError,ValueError,KeyError,wave.Error) as exc:
            if isinstance(exc,ValueError) and str(exc)=='TRACK_SOURCE_CHANGED': raise
            raise ValueError('TRACK_EXTRACTION_CORRUPT') from exc

    def verify_audio(self, config, audio):
        _checkpoint(self.deadline)
        if audio.get('origin_kind') == 'extracted_track':
            return self._verify_extracted(config,audio,sources_validated=self._batch)
        result=self.generated.verify_audio(config,audio)
        _checkpoint(self.deadline)
        return result
