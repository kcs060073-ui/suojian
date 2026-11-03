"""Immutable file-proven original track with separate recorded speech and tail.

No original TTS claim, generated receipt, apply/restore adapter or shared changes.
Reads require caller-frozen task/ID and recheck strict decoded PCM against MP3.
"""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import time
import uuid
import wave

from .provenance import canonical_digest,latest_gate
from .tasks import get_task
from .workflow import project_gates
from .tools import find_ffmpeg,find_ffprobe,default_ffprobe_search_roots
from .voice_audio_assembly import RATE,FORMAT,_assembly_process_runner
from .workbench_actions import _run_checked
from .voice_original_baseline import (_deadline,_check_time,_safe,_hash,_read_json,
    _historical_proof,_historical_file_proof,_copy_verified,OriginalBaselineError)
from .voice_track_segments import _pcm,_windows
from .voice_timing_original_map import map_original_sentence_text,describe_original_track_tail

_SOURCE_FILES=('final-script.txt','adplan.json','adtiming.json','voice.mp3')
_FILES=(*_SOURCE_FILES,'decoded.wav')
_VERIFICATION=dict(file_provenance='verified',complete_pcm='verified',sentence_table='verified',
                   original_text='verified',original_tts_input='unverified',timing_alignment='unverified')
_TTS=dict(status='unverified',reason='historical_tts_input_not_proven')
IDENTITY_KEYS=('schema_version','origin_kind','task_id','route','source_root','script_sha256',
               'brief_lock','files','provenance','pcm','mapping','sentence_windows','unlabeled_tail',
               'timing_kind','verification','original_tts_input','ready_for_voice_editing')


def _paths(source_root):
    paths={name:source_root/'artifacts/ad'/name for name in _SOURCE_FILES}
    paths['final-script.txt']=source_root/'inputs/final-script.txt'
    return paths


def _script(path,expected_sha,deadline):
    _check_time(deadline)
    if path.stat().st_size>200_000:raise ValueError('ORIGINAL_TRACK_SCRIPT_INVALID')
    with path.open('rb') as source:data=source.read(200_001)
    _check_time(deadline)
    if len(data)>200_000 or hashlib.sha256(data).hexdigest()!=expected_sha:
        raise ValueError('ORIGINAL_TRACK_SCRIPT_INVALID')
    return data.decode('utf8')


def _inspect(db,root,task_id,snapshot_id,deadline):
    if not isinstance(task_id,str) or re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]*',task_id) is None:
        raise ValueError('ORIGINAL_TRACK_TASK_INVALID')
    task=get_task(db,task_id)
    if (task is None or task.get('task_type')!='ad_mix' or not isinstance(task.get('brief'),dict)
            or not project_gates(db,task_id)['brief_locked']):
        raise ValueError('ORIGINAL_TRACK_LOCK_REQUIRED')
    record=task['brief'].get('final_script')
    if not isinstance(record,dict) or record.get('relative_path')!='inputs/final-script.txt':
        raise ValueError('ORIGINAL_TRACK_LOCK_REQUIRED')
    source_root=root/'runtime/workbench/tasks'/task_id
    paths=_paths(source_root)
    for path in paths.values():
        _safe(path)
        if not path.is_file():raise ValueError('ORIGINAL_TRACK_SOURCE_MISSING')
    files={name:_hash(path,deadline) for name,path in paths.items()}
    if files['final-script.txt']['sha256']!=record.get('sha256'):
        raise ValueError('ORIGINAL_TRACK_LOCK_REQUIRED')
    proof=_historical_proof(db,task_id,snapshot_id,paths,files,deadline)
    return dict(task_id=task_id,route='ad_mix',source_root=str(source_root.absolute()),
                script_sha256=record['sha256'],brief_lock=dict(latest_gate(db,task_id,'brief')),
                files=files,provenance=proof),paths


def _run(command,cwd,deadline,runner):
    _check_time(deadline)
    result=_run_checked(runner or _assembly_process_runner,[str(v) for v in command],cwd=cwd,
                        timeout_seconds=max(1,math.ceil(deadline-time.monotonic())),max_output_bytes=65536)
    if result.get('stdout_truncated') or result.get('stderr_truncated'):
        raise RuntimeError('PROCESS_OUTPUT_LIMIT')
    _check_time(deadline)
    return result


def _strict_pcm_hash(mp3,cwd,deadline,runner):
    ffmpeg=find_ffmpeg(os.environ,search_roots=default_ffprobe_search_roots())
    result=_run([ffmpeg,'-v','error','-nostdin','-xerror','-err_detect','explode','-i',mp3,
                 '-map','0:a:0','-vn','-ac',1,'-ar',RATE,'-c:a','pcm_s16le','-f','hash',
                 '-hash','sha256','pipe:1'],cwd,deadline,runner)
    output=result['stdout'].strip()
    if re.fullmatch('SHA256=[0-9a-f]{64}',output) is None:
        raise ValueError('ORIGINAL_TRACK_DECODE_INVALID')
    return output[7:]


def _timing(path,script,sha,deadline):
    if path.stat().st_size>2_000_000:raise ValueError('TRACK_TIMING_INVALID')
    data=_read_json(path,deadline,sha)
    mapping=map_original_sentence_text(script,data.get('segments'))
    mapped=deepcopy(data);mapped['segments']=mapping['segments']
    return mapping,_windows(mapped,script)


def _tail(path,start,frames,deadline):
    if start>frames:raise ValueError('ORIGINAL_TRACK_TIMING_EXCEEDS_SOURCE')
    if start==frames:return None
    tail=describe_original_track_tail(start,frames)
    digest=hashlib.sha256();remaining=frames-start
    with wave.open(str(path),'rb') as source:
        source.setpos(start)
        while remaining:
            _check_time(deadline)
            count=min(remaining,32768);chunk=source.readframes(count)
            _check_time(deadline)
            if len(chunk)!=count*2:raise ValueError('ORIGINAL_TRACK_PCM_TRUNCATED')
            digest.update(chunk);remaining-=count
    tail.update(pcm_sha256=digest.hexdigest(),count_source='strict_full_decode_48k')
    return tail


def _pcm_record(frames,sha):
    return dict(FORMAT,frame_count=frames,pcm_sha256=sha,decode_kind='strict_full_decode',path='decoded.wav')


def _read(path,task_id,bundle_id,deadline,runner,expected=None):
    try:
        _safe(path)
        manifest=_read_json(path/'manifest.json',deadline)
        identity={k:manifest[k] for k in IDENTITY_KEYS}
        if (set(manifest)!=set(IDENTITY_KEYS)|{'bundle_id','captured_at'}
                or not isinstance(bundle_id,str) or re.fullmatch('[0-9a-f]{64}',bundle_id) is None
                or manifest['bundle_id']!=bundle_id or canonical_digest(identity)!=bundle_id
                or expected is not None and identity!=expected
                or manifest['task_id']!=task_id or manifest['route']!='ad_mix'
                or manifest['schema_version']!=1 or manifest['origin_kind']!='original_track'
                or manifest['verification']!=_VERIFICATION or manifest['original_tts_input']!=_TTS
                or manifest['ready_for_voice_editing'] is not False
                or manifest['timing_kind']!='recorded_sentence_table' or set(manifest['files'])!=set(_FILES)):
            raise ValueError('identity')
        if datetime.fromisoformat(manifest['captured_at']).tzinfo is None:raise ValueError('capture time')
        source_root=Path(manifest['source_root'])
        if (not source_root.is_absolute() or '..' in source_root.parts
                or source_root.parts[-4:]!=('runtime','workbench','tasks',task_id)):
            raise ValueError('source paths')
        proof=manifest['provenance']
        checked=_historical_file_proof(proof['source_record'],task_id,proof['snapshot_id'],
                                       _paths(source_root),manifest['files'],deadline)
        if checked!=proof:raise ValueError('provenance')
        for name in _FILES:
            if _hash(path/name,deadline)!=manifest['files'][name]:raise ValueError('file')
        if manifest['script_sha256']!=manifest['files']['final-script.txt']['sha256']:
            raise ValueError('script')
        script=_script(path/'final-script.txt',manifest['script_sha256'],deadline)
        mapping,windows=_timing(path/'adtiming.json',script,manifest['files']['adtiming.json']['sha256'],deadline)
        frames,pcm_sha=_pcm(path/'decoded.wav',deadline=deadline)
        if (_pcm_record(frames,pcm_sha)!=manifest['pcm'] or mapping!=manifest['mapping']
                or windows!=manifest['sentence_windows']
                or _tail(path/'decoded.wav',windows[-1]['end_frame'],frames,deadline)!=manifest['unlabeled_tail']):
            raise ValueError('complete windows')
        # A newly self-consistent PCM digest cannot certify replacement audio.
        if _strict_pcm_hash(path/'voice.mp3',path,deadline,runner)!=pcm_sha:
            raise ValueError('decoded source')
        for name in _FILES:
            if _hash(path/name,deadline)!=manifest['files'][name]:raise ValueError('file changed')
    except OriginalBaselineError as error:
        if error.code=='BASELINE_TIMEOUT':raise
        raise ValueError('ORIGINAL_TRACK_PACKAGE_INVALID') from error
    except (OSError,ValueError,KeyError,TypeError,wave.Error) as error:
        raise ValueError('ORIGINAL_TRACK_PACKAGE_INVALID') from error
    _check_time(deadline)
    return dict(path=str(path),manifest=manifest)


def read_original_track_bundle(package_path,*,expected_task_id,expected_bundle_id,runner=None,timeout_seconds=120):
    """Read-only package verification, including bounded full-source decode hash."""
    return _read(Path(package_path).absolute(),expected_task_id,expected_bundle_id,_deadline(timeout_seconds),runner)


def prepare_original_track_bundle(db,project_root,task_id,output_root,*,historical_render_id,
                                  runner=None,timeout_seconds=120):
    """Capture original four-file history and complete PCM, never original TTS.

    Sources and DB are read-only; only output_root/private stages are written.
    A terminal unlabeled tail is preserved; interior gaps and overflows reject.
    Caller owns current-source type, permission, ID registration and restore.
    """
    deadline=_deadline(timeout_seconds)
    root,output=Path(project_root).absolute(),Path(output_root).absolute()
    source,paths=_inspect(db,root,task_id,historical_render_id,deadline)
    _safe(output)
    if output.resolve().is_relative_to(Path(source['source_root']).resolve()):
        raise ValueError('ORIGINAL_TRACK_OUTPUT_INVALID')
    # Text/gap errors fail before the decoder or any output is created.
    script=_script(paths['final-script.txt'],source['script_sha256'],deadline)
    _timing(paths['adtiming.json'],script,source['files']['adtiming.json']['sha256'],deadline)
    output.mkdir(parents=True,exist_ok=True)
    stage=output/('.original-track-'+uuid.uuid4().hex);stage.mkdir()
    try:
        for name,path in paths.items():_copy_verified(path,stage/name,source['files'][name],deadline)
        mapping,windows=_timing(stage/'adtiming.json',script,source['files']['adtiming.json']['sha256'],deadline)
        ffprobe=find_ffprobe(os.environ,search_roots=default_ffprobe_search_roots())
        probe=_run([ffprobe,'-v','error','-show_entries','format=duration','-of','json',stage/'voice.mp3'],stage,deadline,runner)
        import json
        duration=float(json.loads(probe['stdout'])['format']['duration'])
        if not math.isfinite(duration) or not 0<duration<=3600:raise ValueError('ORIGINAL_TRACK_DURATION_INVALID')
        ffmpeg=find_ffmpeg(os.environ,search_roots=default_ffprobe_search_roots())
        _run([ffmpeg,'-v','error','-nostdin','-xerror','-err_detect','explode','-n','-i',stage/'voice.mp3',
              '-map','0:a:0','-vn','-ac',1,'-ar',RATE,'-c:a','pcm_s16le',stage/'decoded.wav'],stage,deadline,runner)
        frames,pcm_sha=_pcm(stage/'decoded.wav',deadline=deadline)
        tail=_tail(stage/'decoded.wav',windows[-1]['end_frame'],frames,deadline)
        files=dict(source['files'],**{'decoded.wav':_hash(stage/'decoded.wav',deadline)})
        identity=dict(schema_version=1,origin_kind='original_track',**source)
        identity.update(files=files,pcm=_pcm_record(frames,pcm_sha),mapping=mapping,sentence_windows=windows,
                        unlabeled_tail=tail,timing_kind='recorded_sentence_table',verification=dict(_VERIFICATION),
                        original_tts_input=dict(_TTS),ready_for_voice_editing=False)
        bundle_id=canonical_digest(identity)
        manifest=dict(identity,bundle_id=bundle_id,captured_at=datetime.now(timezone.utc).isoformat())
        (stage/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,sort_keys=True),encoding='utf8')
        _read(stage,task_id,bundle_id,deadline,runner,expected=identity)
        after,_=_inspect(db,root,task_id,historical_render_id,deadline)
        if after!=source:raise ValueError('ORIGINAL_TRACK_SOURCE_CHANGED')
        destination=output/('original-track-'+bundle_id)
        _check_time(deadline)
        try:stage.rename(destination)
        except OSError:
            if not destination.is_dir():raise
        return _read(destination,task_id,bundle_id,deadline,runner,expected=identity)
    finally:
        if stage.exists():shutil.rmtree(stage)
