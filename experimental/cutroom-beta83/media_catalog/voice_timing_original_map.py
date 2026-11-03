"""Pure original-text slicing and tail facts; no audio/timing acceptance changes."""
from copy import deepcopy
import hashlib
import math

# Unicode White_Space, not Python's broader isspace() control-character set.
_WHITESPACE = frozenset('\t\n\v\f\r \u0085\u00a0\u1680\u2028\u2029\u202f\u205f\u3000') | frozenset(chr(n) for n in range(0x2000,0x200b))


def _content(text):
    return ''.join(c for c in text if c not in _WHITESPACE)


def map_original_sentence_text(original_script, sentence_rows):
    """Match the complete non-whitespace stream, then partition original slices.

    Offsets are Python Unicode character indices, never UTF-8 byte offsets.
    Leading whitespace belongs to the first row; inter-row/trailing whitespace
    belongs to the preceding row. No substring search or repeat-word guessing.
    Whitespace remains formatting inside the slice, not additional speech.
    All input fields (including measured start/end) are copied without changes
    except text; original_* fields explicitly describe this text-only mapping.
    """
    if not isinstance(original_script,str) or not isinstance(sentence_rows,list) or not sentence_rows:
        raise ValueError('ORIGINAL_TEXT_MAPPING_INVALID')
    rows=deepcopy(sentence_rows)
    contents=[]
    for row in rows:
        if (not isinstance(row,dict) or not isinstance(row.get('text'),str) or not _content(row['text'])
                or any(type(row.get(key)) not in (int,float) or not math.isfinite(row[key]) for key in ('start','end'))
                or not 0<=row['start']<row['end']):
            raise ValueError('ORIGINAL_TEXT_MAPPING_INVALID')
        contents.append(_content(row['text']))
    positions=[i for i,c in enumerate(original_script) if c not in _WHITESPACE]
    if _content(original_script)!=''.join(contents):
        raise ValueError('ORIGINAL_TEXT_MAPPING_MISMATCH')
    consumed=0
    first=0
    for row,content in zip(rows,contents):
        consumed+=len(content)
        # The next content character gives a unique boundary in the global
        # ordered stream, even when whole sentences or words repeat.
        last=positions[consumed] if consumed<len(positions) else len(original_script)
        row.update(original_timing_text=row['text'],text=original_script[first:last],
                   original_start=first,original_end=last)
        first=last
    assert ''.join(row['text'] for row in rows)==original_script
    return dict(segments=rows,script_sha256=hashlib.sha256(original_script.encode('utf-8')).hexdigest(),
                mapping_kind='unicode_whitespace_original_slices',
                whitespace_is_speech=False,measured_alignment_proven=False,ready_for_voice_editing=False,
                verification=dict(original_text='verified',original_tts_input='unverified',
                                  timing_alignment='unverified',complete_sample_windows='unverified'))


def describe_original_track_tail(last_timed_end_frame, decoded_frame_count, *, sample_rate=48000):
    """Describe supplied decoded frame counts; neither decode nor modify audio."""
    if (type(last_timed_end_frame) is not int or type(decoded_frame_count) is not int
            or type(sample_rate) is not int or sample_rate!=48000
            or not 0<=last_timed_end_frame<=decoded_frame_count):
        raise ValueError('ORIGINAL_TRACK_TAIL_INVALID')
    count=decoded_frame_count-last_timed_end_frame
    return dict(start_frame=last_timed_end_frame,end_frame=decoded_frame_count,frame_count=count,
                sample_rate=sample_rate,duration_seconds=count/sample_rate,
                timing_kind='unlabeled_original_track_tail',has_tail=count>0,
                extends_measured_sentence=False,proposed_audio_action='preserve_original_samples',
                ready_for_voice_editing=False,counts_are_caller_supplied=True)
