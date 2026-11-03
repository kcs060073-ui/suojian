"""Bounded response normalization, without requests, approval or audio evidence inference.

The caller privately saves the complete response BEFORE parsing. Generated audio
is provider output, never proof that the submitted media was heard correctly.
"""
import hashlib
import json

PROTOCOL_VERSION = 'audio-review-response-v1'
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_AUDIO_CHARS = 8 * 1024 * 1024
MAX_TEXT_CHARS = 64 * 1024
MAX_CHOICES = 8
_DIMENSIONS = ('asr', 'pronunciation', 'fluency', 'prosody')
_DIMENSION_SCHEMA = {'type':'object','additionalProperties':False,'required':['status','evidence'],
    'properties':{'status':{'type':'string','enum':['clear','issues','unknown']},
                  'evidence':{'type':'array','maxItems':32,'items':{'type':'string','minLength':1,'maxLength':2000}}}}
AUDIO_REVIEW_SCHEMA = {'type':'object','additionalProperties':False,
    'required':['schemaVersion','coverage','dimensions'],
    'properties':{'schemaVersion':{'const':'audio-review-v1'},
        'coverage':{'type':'string','minLength':1,'maxLength':2000},
        'dimensions':{'type':'object','additionalProperties':False,'required':list(_DIMENSIONS),
                      'properties':{key:_DIMENSION_SCHEMA for key in _DIMENSIONS}}}}


class AudioReviewProtocolError(ValueError):
    outcome = 'confirmed_protocol_failure'

    def __init__(self, code, diagnostics):
        # No provider text or generated audio enters exception/log messages.
        super().__init__(code + ': 已收到响应，但未得到完整有效的听审结果')
        self.code = code
        self.diagnostics = diagnostics


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _constant(_value):
    raise ValueError('nonfinite number')


def _loads(raw):
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)


def _bounded_metadata(value):
    """Copy small JSON metadata, preserving its actual fields without defaults."""
    count = 0
    chars = 0
    def walk(item, depth=0):
        nonlocal count, chars
        count += 1
        if count > 2048 or depth > 12:
            raise ValueError('metadata bound')
        if item is None or type(item) in (bool,int):
            return item
        if isinstance(item, str):
            chars += len(item)
            if chars > MAX_TEXT_CHARS:
                raise ValueError('metadata text bound')
            return item
        if isinstance(item, float):
            if not (-float('inf') < item < float('inf')):
                raise ValueError('nonfinite')
            return item
        if isinstance(item, list):
            return [walk(v,depth+1) for v in item]
        if isinstance(item, dict):
            if not all(isinstance(k,str) and len(k)<=256 for k in item):
                raise ValueError('invalid key')
            return {k:walk(v,depth+1) for k,v in item.items()}
        raise ValueError('not JSON metadata')
    return walk(value)


def _review(text):
    stripped = text.strip()
    if not stripped.startswith(('{','[')):
        return None
    value = _loads(stripped)
    if (not isinstance(value,dict) or set(value) != {'schemaVersion','coverage','dimensions'}
            or value['schemaVersion'] != 'audio-review-v1'
            or not isinstance(value['coverage'],str) or not 1<=len(value['coverage'].strip())<=2000):
        raise ValueError('review schema')
    dimensions=value['dimensions']
    if not isinstance(dimensions,dict) or set(dimensions)!=set(_DIMENSIONS):
        raise ValueError('dimensions schema')
    for dimension in dimensions.values():
        if (not isinstance(dimension,dict) or set(dimension)!={'status','evidence'}
                or dimension['status'] not in ('clear','issues','unknown')
                or not isinstance(dimension['evidence'],list) or len(dimension['evidence'])>32
                or any(not isinstance(e,str) or not 1<=len(e.strip())<=2000 for e in dimension['evidence'])):
            raise ValueError('dimension schema')
        if dimension['status']=='issues' and not dimension['evidence']:
            raise ValueError('issue without evidence')
    return value


def parse_audio_response(raw) -> dict:
    """Normalize str/UTF-8 bytes/dict; protocol errors are confirmed responses.

    Only one unambiguous structured choice exposes top-level review. Plain text
    remains readable unstructured data. No approval flag is produced. coverage
    is an unverified model statement; callers bind real input duration/window.
    """
    diagnostics={'protocolVersion':PROTOCOL_VERSION,'choices':[]}
    def reject(code='AUDIO_PROTOCOL_INVALID'):
        raise AudioReviewProtocolError(code,diagnostics)
    try:
        if isinstance(raw,(str,bytes)):
            if (len(raw)>MAX_RESPONSE_BYTES or
                    isinstance(raw,str) and len(raw.encode('utf8'))>MAX_RESPONSE_BYTES):
                reject('AUDIO_RESPONSE_TOO_LARGE')
            if isinstance(raw,bytes):
                raw=raw.decode('utf8')
            envelope=_loads(raw)
        else:
            envelope=raw
        if not isinstance(envelope,dict):
            reject()
        result={'protocolVersion':PROTOCOL_VERSION,'generatedAudioIsSourceEvidence':False,'choices':[]}
        if 'id' in envelope:
            if not isinstance(envelope['id'],str) or len(envelope['id'])>512:
                reject()
            result['responseId']=envelope['id'];diagnostics['responseId']=envelope['id']
        if 'usage' in envelope:
            if envelope['usage'] is not None and not isinstance(envelope['usage'],dict):
                reject()
            result['usage']=_bounded_metadata(envelope['usage'])
            diagnostics['usage']={k:v for k,v in (result['usage'] or {}).items()
                                  if k in ('prompt_tokens','completion_tokens','total_tokens')
                                  and type(v) is int and 0<=v<=1_000_000_000}
        choices=envelope.get('choices')
        if not isinstance(choices,list) or not 1<=len(choices)<=MAX_CHOICES:
            reject()
        total_text=0
        total_audio=0
        for choice in choices:
            if not isinstance(choice,dict) or not isinstance(choice.get('message'),dict):
                reject()
            finish=choice.get('finish_reason')
            facts={'finishReason':finish if isinstance(finish,str) and len(finish)<=64 else None}
            diagnostics['choices'].append(facts)
            message=choice['message']
            normalized={'finishReason':finish,'textSources':[]}
            if 'index' in choice:
                if type(choice['index']) is not int or choice['index']<0:
                    reject()
                normalized['index']=choice['index']
            def add(source,text):
                nonlocal total_text
                if not isinstance(text,str):
                    reject()
                total_text+=len(text)
                if total_text>MAX_TEXT_CHARS:
                    reject('AUDIO_RESPONSE_TOO_LARGE')
                normalized['textSources'].append({'source':source,'text':text})
            content=message.get('content')
            candidates=[]
            if isinstance(content,str):
                add('content',content);candidates.append(content)
            elif isinstance(content,list):
                if len(content)>64:
                    reject('AUDIO_RESPONSE_TOO_LARGE')
                parts=[]
                for i,part in enumerate(content):
                    if not isinstance(part,dict) or part.get('type') not in ('text','output_text'):
                        reject()
                    add(f'content[{i}].text',part.get('text'));parts.append(part['text'])
                candidates.append(''.join(parts))
            elif content is not None:
                reject()
            if 'audio' in message:
                audio=message['audio']
                if audio is None:
                    normalized['audio']=None
                elif isinstance(audio,dict):
                    metadata={k:v for k,v in audio.items() if k!='data'}
                    normalized['audio']=_bounded_metadata(metadata)
                    if 'data' in audio:
                        data=audio['data']
                        if not isinstance(data,str):
                            reject()
                        total_audio+=len(data)
                        if total_audio>MAX_AUDIO_CHARS:
                            reject('AUDIO_RESPONSE_TOO_LARGE')
                        normalized['audio']['dataChars']=len(data)
                        normalized['audio']['dataSha256']=hashlib.sha256(data.encode('utf8')).hexdigest()
                    if audio.get('transcript') is not None:
                        add('audio.transcript',audio['transcript']);candidates.append(audio['transcript'])
                else:
                    reject()
            facts['textChars']=sum(len(s['text']) for s in normalized['textSources'])
            facts['hasAudio']=isinstance(message.get('audio'),dict)
            if message.get('refusal') or finish=='content_filter':
                reject('AUDIO_RESPONSE_REFUSED')
            if finish!='stop':
                reject('AUDIO_RESPONSE_INCOMPLETE')
            if message.get('tool_calls') or message.get('function_call'):
                reject()
            candidates=[text for text in candidates if text.strip()]
            if not candidates:
                reject('AUDIO_FINAL_RESULT_MISSING')
            reviews=[_review(text) for text in candidates]
            if all(r is not None and r==reviews[0] for r in reviews):
                normalized.update(status='structured',review=reviews[0])
            elif any(r is not None for r in reviews):
                normalized['status']='ambiguous'
            else:
                normalized['status']='unstructured'
            result['choices'].append(normalized)
        result['status']=result['choices'][0]['status'] if len(choices)==1 else 'ambiguous'
        if result['status']=='structured':
            result['review']=result['choices'][0]['review']
        return result
    except AudioReviewProtocolError:
        raise
    except (ValueError,TypeError,RecursionError,OverflowError):
        reject()