"""Offline measured sentence timing from A4's verified complete PCM windows.

Not a generated provider receipt, word aligner or public apply/restore adapter.
"""
from copy import deepcopy
import hashlib
import time

from .voice_audio_assembly import RATE
from .voice_track_segments import ExtractedTrackVerifier, _inputs, _checkpoint, prepare_track_segments


def adapt_track_bundle_timing(bundle, context, base_revision, artifact_binding, timing_evidence,
                              *, timing_kind='measured_sentence', timeout_seconds=30):
    """Trusted caller inputs only; preserve original text and actual 48k windows.

    adtiming.json is a sentence table. Asking for word timings is unsupported;
    character-count interpolation cannot substitute for measured word evidence.
    """
    if timing_kind == 'measured_word':
        raise ValueError('WORD_TIMING_NOT_PROVEN')
    if timing_kind != 'measured_sentence':
        raise ValueError('TRACK_TIMING_KIND_INVALID')
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 120:
        raise ValueError('TRACK_TIMEOUT_INVALID')
    deadline=time.monotonic()+timeout_seconds
    context, binding, evidence, bundle = deepcopy((context, artifact_binding, timing_evidence, bundle))
    source_sha, timing_sha = _inputs(context, base_revision, binding, evidence)
    manifest=bundle['manifest']
    expected=dict(task_id=context.task_id, base_revision=base_revision, context_digest=context.context_digest,
                  script_sha256=context.script_sha256, voice=context.voice, input_digest=binding['input_digest'],
                  source_sha256=source_sha, timing_sha256=timing_sha,
                  timing_evidence={key:evidence[key] for key in ('kind','evidence_id')})
    if any(manifest.get(key)!=value for key,value in expected.items()):
        raise ValueError('TRACK_TIMING_ADAPTER_BINDING_INVALID')
    verifier=ExtractedTrackVerifier(bundle,bundle['audio_root'],deadline=deadline)
    verifier.validate_sources()
    windows=manifest['windows']
    previous=0
    timing=[]
    for window in windows:
        _checkpoint(deadline)
        first,last=window['start_frame'],window['end_frame']
        if (type(first) is not int or type(last) is not int or first!=previous or not 0<=first<last):
            raise ValueError('TRACK_SOURCE_COVERAGE_REQUIRED')
        # Direct sample positions, never ratios, dropped tails or ffprobe padding.
        timing.append(dict(text=window['text'],start=first/RATE,end=last/RATE))
        previous=last
    text=''.join(row['text'] for row in timing)
    if text!=context.script or hashlib.sha256(text.encode('utf-8')).hexdigest()!=context.script_sha256:
        raise ValueError('TRACK_ORIGINAL_TEXT_REQUIRED')
    if previous!=manifest['frame_count']:
        raise ValueError('TRACK_SOURCE_COVERAGE_REQUIRED')
    verifier._check_snapshot()
    _checkpoint(deadline)
    return dict(origin_kind='extracted_track',timing_kind='measured_sentence',timing=timing,
                pcm_windows=deepcopy(windows),sample_rate=RATE,frame_count=manifest['frame_count'],
                duration=manifest['frame_count']/RATE,bundle_id=manifest['bundle_id'],
                source_sha256=source_sha,timing_sha256=timing_sha,
                timing_evidence=deepcopy(evidence),provider_receipt_available=False,
                ready_for_voice_editing=False)


def prepare_ad_voice_timing(project_root, output_root, context, base_revision, artifact_binding,
                           timing_evidence, *, runner=None, timeout_seconds=120,
                           timing_kind='measured_sentence'):
    """Reuse A4 for strict MP3 decode, source proof and exact full-window coverage."""
    if timing_kind=='measured_word':
        raise ValueError('WORD_TIMING_NOT_PROVEN')
    if timing_kind!='measured_sentence':
        raise ValueError('TRACK_TIMING_KIND_INVALID')
    started=time.monotonic()
    bundle=prepare_track_segments(project_root,output_root,context,base_revision,artifact_binding,
                                  timing_evidence,runner=runner,timeout_seconds=timeout_seconds)
    remaining=timeout_seconds-(time.monotonic()-started)
    if remaining<=0:
        raise RuntimeError('PROCESS_TIMEOUT')
    return adapt_track_bundle_timing(bundle,context,base_revision,artifact_binding,timing_evidence,
                                    timeout_seconds=min(120,remaining))
