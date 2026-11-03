"""Frame-supported content intervals, separate from broad scene summaries."""
from .visual_schema import observation_schema
from .visual_observations import SINGLE_FRAME_UNCERTAINTY
from types import SimpleNamespace

CONTENT_SAMPLING_VERSION = "visual-content-1s-v2"

def content_frame_times(start, end):
    if type(start) is not int or type(end) is not int or not 0 <= start < end or end-start > 15000000:
        raise ValueError("SOURCE_WINDOW_INVALID: 精细分析区间须在15秒内")
    # Sample near both ends; never ask ffmpeg for the nonexistent EOF frame.
    last = max(start, end-min(100000, max(1,(end-start)//10)))
    times = sorted(set([start, *range(start+1000000,end,1000000), last]))
    if len(times) < 2:
        raise ValueError("SOURCE_WINDOW_INVALID: 区间过短，无法核验内容")
    return times

def content_observation_schema():
    segment = observation_schema()
    segment['properties'].update(sourceStartUs={'type':'integer'},sourceEndUs={'type':'integer'})
    segment['required'] += ['sourceStartUs','sourceEndUs']
    return {'type':'object','properties':{'segments':{'type':'array','minItems':1,'maxItems':16,'items':segment}},
            'required':['segments'],'additionalProperties':False}

def validate_content_segments(sample, result):
    from .visual_batch_execution import _validate_result
    from jsonschema import Draft202012Validator
    if not Draft202012Validator(content_observation_schema()).is_valid(result):
        raise ValueError("VISUAL_CONTENT_SCHEMA_INVALID: 缺少带源时间的内容片段")
    start,end=sample.source_window_us
    times={f.timestamp_us for f in sample.frames}
    cursor=start;rows=[]
    for raw in result['segments']:
        left,right=raw['sourceStartUs'],raw['sourceEndUs']
        if type(left) is not int or type(right) is not int or left != cursor or not left < right <= end:
            raise ValueError("VISUAL_CONTENT_RANGE_INVALID: 内容片段有遗漏、重叠或越界")
        if left not in times or right != end and right not in times:
            raise ValueError("VISUAL_CONTENT_BOUNDARY_INVALID: 内容边界须来自实际取样时码")
        evidence=raw['evidenceUs']
        if any(type(t) is not int or not left <= t < right for t in evidence):
            raise ValueError("VISUAL_CONTENT_EVIDENCE_INVALID: 片段证据不能借用其他区间")
        segment_frames=[frame for frame in sample.frames if left <= frame.timestamp_us < right]
        segment_sample=SimpleNamespace(media_kind=sample.media_kind,frames=segment_frames)
        single=len(segment_frames)==1
        fields=_validate_result(segment_sample,{k:v for k,v in raw.items() if k not in ('sourceStartUs','sourceEndUs')},
                                single_frame_content=single)
        if single:fields['sample_frame_times_us']=sorted(times)
        rows.append((left,right,fields));cursor=right
    if cursor != end:
        raise ValueError("VISUAL_CONTENT_COVERAGE_INCOMPLETE: 分析未覆盖完整源区间")
    return rows


def indexed_content_schema(frame_count):
    """Provider chooses numbered frames; only the server converts them to time."""
    if type(frame_count) is not int or not 2 <= frame_count <= 16:
        raise ValueError('VISUAL_CONTENT_FRAME_COUNT_INVALID')
    schema=content_observation_schema()
    item=schema['properties']['segments']['items']
    for key in ('sourceStartUs','sourceEndUs','evidenceUs'):
        item['properties'].pop(key)
        item['required'].remove(key)
    number={'type':'integer','minimum':1,'maximum':frame_count}
    item['properties'].update(firstFrame=dict(number),lastFrame=dict(number),
        evidenceFrames={'type':'array','minItems':1,'maxItems':16,'uniqueItems':True,'items':dict(number)})
    item['required'] += ['firstFrame','lastFrame','evidenceFrames']
    return schema


def _single_frame_unknown(fields):
    # Called only after the whole window's indices, coverage and evidence pass.
    # Original model fields remain intact in the private HTTP response artifact.
    uncertainty = fields['uncertaintyReason']
    if SINGLE_FRAME_UNCERTAINTY not in uncertainty:
        uncertainty = (uncertainty + '；' if uncertainty else '') + SINGLE_FRAME_UNCERTAINTY
    return {**fields, 'cameraMotion': '未知', 'motionKind': '无法区分',
            'subjectMotion': '未知', 'uncertaintyReason': uncertainty}


def decode_indexed_segments(sample,result):
    from .visual_schema import validate_visual_schema
    from .visual_protocol import DEFAULT_PROTOCOL_MODE, VisualProtocolError
    times=[f.timestamp_us for f in sample.frames]
    try:
        validate_visual_schema(result,indexed_content_schema(len(times)),DEFAULT_PROTOCOL_MODE)
    except VisualProtocolError as error:
        raise VisualProtocolError('VISUAL_CONTENT_INDEX_INVALID',
            '返回的帧编号格式或证据数量无效', error.diagnostics) from None
    cursor=1
    for row in result['segments']:
        first,last=row['firstFrame'],row['lastFrame']
        evidence=row['evidenceFrames']
        if (type(first) is not int or type(last) is not int or first!=cursor or last<first
                or any(type(i) is not int or not first<=i<=last for i in evidence)):
            raise ValueError('VISUAL_CONTENT_INDEX_INVALID: 帧分段有遗漏、重叠或跨段证据')
        if first!=last and len(set(evidence))<2:
            raise ValueError('VISUAL_CONTENT_INDEX_INVALID: 多帧段仍须至少两条段内真实证据')
        cursor=last+1
    if cursor!=len(times)+1:
        raise ValueError('VISUAL_CONTENT_INDEX_INVALID: 帧分段未完整覆盖')
    out=[]
    for row in result['segments']:
        first,last=row['firstFrame'],row['lastFrame']
        fields={k:v for k,v in row.items() if k not in ('firstFrame','lastFrame','evidenceFrames')}
        if first==last:
            fields=_single_frame_unknown(fields)
        out.append({**fields,'sourceStartUs':times[first-1],
            'sourceEndUs':times[last] if last<len(times) else sample.source_window_us[1],
            'evidenceUs':[times[i-1] for i in row['evidenceFrames']]})
    return {'segments':out}
