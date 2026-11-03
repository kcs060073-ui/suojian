"""Read-only authoritative inputs for task-local voice editing.

No HTTP authorization, TTS calls, draft/job writes, migrations or gate changes.
The public adapter must use public_payload(), never serialize internal context.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

from .model_profiles import get_task_profile_binding, resolve_task_tts_voice
from .provenance import canonical_digest, latest_gate
from .task_script import read_task_script
from .tasks import get_task
from .tts_voice_catalog import list_provider_voices
from .voice_editing import compile_voice_input
from .workflow import project_brief_locked


class VoiceTaskContextError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(f'{code}: {message}')


def _fail(code, message):
    raise VoiceTaskContextError(code, message)


@dataclass(frozen=True)
class VoiceTaskContext:
    task_id: str
    script: str
    script_sha256: str
    context_digest: str
    voice: dict
    voice_label: str
    voices: tuple

    def public_payload(self):
        return dict(taskId=self.task_id, route='ad_mix', contextDigest=self.context_digest,
                    script=self.script, scriptSha256=self.script_sha256, voiceId=self.voice['voice'],
                    voiceLabel=self.voice_label, voices=deepcopy(list(self.voices)),
                    readingSupport=dict(status='unverified', code='PROVIDER_READING_UNSUPPORTED'))


def utf16_span_to_unicode(text, start, end):
    """Convert only complete Unicode scalar boundaries, never guess a selection."""
    if not isinstance(text, str) or any(0xD800 <= ord(char) <= 0xDFFF for char in text):
        _fail('SCRIPT_UNICODE_INVALID', '原文不是有效Unicode文本')
    if any(type(value) is not int for value in (start, end)) or not 0 <= start < end:
        _fail('UTF16_SPAN_INVALID', '请选择完整且非空的原文区间')
    boundaries, offset = {0: 0}, 0
    for index, char in enumerate(text):
        offset += 2 if ord(char) > 0xFFFF else 1
        boundaries[offset] = index + 1
    if start not in boundaries or end not in boundaries:
        _fail('UTF16_SPAN_INVALID', '选区越界或切在Unicode字符内部')
    return boundaries[start], boundaries[end]


def _redirected(path):
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)
    except OSError:
        return False


def _script(db, root, task_id):
    task = get_task(db, task_id)
    if task is None:
        _fail('TASK_NOT_FOUND', '当前任务不存在')
    if task.get('task_type') != 'ad_mix':
        _fail('DIGITAL_ORIGINAL_VOICE_PRESERVED', '当前路线保留原声，不支持重生成配音')
    brief = task.get('brief')
    if not isinstance(brief, dict) or brief.get('route') != 'ad_mix':
        _fail('VOICE_TASK_INVALID', '广告任务的路线记录无效')
    if brief.get('voice_mode') != 'ai_tts':
        _fail('VOICE_MODE_UNSUPPORTED', '当前任务未冻结AI配音方式')
    if not project_brief_locked(db, task_id):
        _fail('BRIEF_NOT_LOCKED', '请先确认当前版本的最终文案')
    gate = latest_gate(db, task_id, 'brief')
    record = brief.get('final_script')
    if not isinstance(record, dict) or record.get('relative_path') != 'inputs/final-script.txt':
        _fail('LOCKED_SCRIPT_PATH_INVALID', '原稿不在本任务固定输入位置')
    raw_path = root / 'runtime/workbench/tasks' / task_id / 'inputs/final-script.txt'
    for path in (raw_path, *raw_path.parents):
        if path == root:
            break
        if _redirected(path):
            _fail('LOCKED_SCRIPT_PATH_INVALID', '任务输入不接受链接或重定向目录')
    if not raw_path.resolve().is_relative_to(root):
        _fail('LOCKED_SCRIPT_PATH_INVALID', '原稿不属于本项目任务目录')
    text = read_task_script(root, task)
    if text is None or not text.strip():
        _fail('LOCKED_SCRIPT_UNAVAILABLE', '锁定原稿无法读取或为空')
    sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
    if sha != record.get('sha256'):
        _fail('LOCKED_SCRIPT_CHANGED', '锁定原稿内容已变化，请重新核对')
    return task, text, sha, dict(gate)


def _binding(db, task_id):
    try:
        snapshot = get_task_profile_binding(db, task_id, 'tts')
    except ValueError:
        _fail('TTS_BINDING_INVALID', '本任务冻结的配音服务校验失败')
    if snapshot is None:
        _fail('TTS_BINDING_REQUIRED', '本任务没有冻结的配音服务')
    if snapshot.get('capability') != 'tts' or any(not isinstance(snapshot.get(key), str) or not snapshot[key].strip()
                                               for key in ('provider', 'accountId', 'configDigest')):
        _fail('TTS_BINDING_INVALID', '本任务冻结的配音服务字段不完整')
    settings = snapshot.get('profileSettings')
    if not isinstance(settings, dict):
        _fail('TTS_BINDING_INVALID', '本任务冻结的配音设置无效')
    return snapshot


def _voice(db, snapshot, voice_id):
    try:
        selected = resolve_task_tts_voice(db, snapshot, voice_id)
    except ValueError:
        _fail('VOICE_NOT_SELECTABLE', '请选择本任务冻结服务中的可用音色')
    if any(not isinstance(selected.get(key), str) or not selected[key].strip() for key in ('voiceId', 'resourceId', 'displayName')):
        _fail('TTS_BINDING_INVALID', '本任务冻结音色或资源标识无效')
    return selected


def resolve_voice_task_context(db, project_root, task_id, *, include_voice_catalog=False):
    """Reconstruct trusted copy + frozen TTS without consulting current defaults.

    Ownership here means a file belongs to this task, not multi-user permission.
    The API must authorize task access before calling, and application must
    recompute this context inside its current-version transaction.
    """
    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', task_id):
        _fail('TASK_ID_INVALID', '任务编号不能映射到安全目录')
    if type(include_voice_catalog) is not bool:
        _fail('VOICE_REQUEST_INVALID', '音色目录开关无效')
    root = Path(project_root).resolve()
    task, text, sha, gate = _script(db, root, task_id)
    snapshot = _binding(db, task_id)
    settings = snapshot['profileSettings']
    frozen_id = settings.get('voice') or settings.get('default_voice_id')
    if not isinstance(frozen_id, str) or not frozen_id.strip():
        _fail('TTS_VOICE_REQUIRED', '本任务未冻结默认音色')
    declared_id = task['brief'].get('tts_voice_id')
    if declared_id is not None and declared_id != frozen_id:
        _fail('TTS_BINDING_INVALID', '任务音色与冻结服务记录不一致')
    selected = _voice(db, snapshot, frozen_id)
    voice = dict(provider=snapshot['provider'], resource=selected['resourceId'],
                 voice=selected['voiceId'], revision=snapshot['configDigest'])
    choices = [dict(id=selected['voiceId'], label=selected['displayName'])]
    if include_voice_catalog and snapshot['provider'] == 'doubao_tts':
        choices = [dict(id=v['voiceId'], label=v['displayName']) for v in
                   list_provider_voices(db, snapshot['accountId'], selected['resourceId'], selectable_only=True)]
    digest = canonical_digest(dict(task_id=task_id, route='ad_mix', script_sha256=sha,
                                   gate_event_id=gate['event_id'], gate_digest=gate['gate_digest'],
                                   tts_binding_digest=snapshot['configDigest'], frozen_voice=voice))
    # Optimistic end check catches inputs changed while resolving a voice/catalog.
    task_after, text_after, sha_after, gate_after = _script(db, root, task_id)
    binding_after = _binding(db, task_id)
    if (canonical_digest(task_after['brief']) != canonical_digest(task['brief']) or text_after != text
            or sha_after != sha or gate_after['event_id'] != gate['event_id']
            or binding_after['configDigest'] != snapshot['configDigest']):
        _fail('CONTEXT_CHANGED', '原稿或配音服务已变化，请重新加载')
    return VoiceTaskContext(task_id, text, sha, digest, voice, selected['displayName'], tuple(choices))


def _reading_capabilities(context, voice_id, rules, evidence, strategy=None):
    if not any(rule['original'] != rule['reading'] for rule in rules):
        return {'substitutions': {}}
    if strategy == 'literal_text_substitution':
        substitutions = {}
        for rule in rules:
            if not isinstance(rule.get('reading'),str) or not rule['reading'].strip():
                _fail('VOICE_RULE_INVALID','读法文字必须非空')
            substitutions.setdefault(rule['original'],[]).append(rule['reading'])
        return {'substitutions': substitutions}
    if strategy is not None:
        _fail('PROVIDER_READING_UNSUPPORTED','未启用该读法策略')
    if (not isinstance(evidence, dict) or evidence.get('binding_digest') != context.voice['revision']
            or evidence.get('voice_id') != voice_id or not isinstance(evidence.get('evidence_id'), str)
            or not evidence['evidence_id'].strip() or not isinstance(evidence.get('substitutions'), dict)):
        _fail('PROVIDER_READING_UNSUPPORTED', '当前服务和音色尚无该读法的已验证支持')
    substitutions = evidence['substitutions']
    if any(not isinstance(word, str) or not isinstance(readings, list)
           or any(not isinstance(reading, str) for reading in readings) for word, readings in substitutions.items()):
        _fail('PROVIDER_READING_UNSUPPORTED', '读法验证证据不完整')
    return {'substitutions': deepcopy(substitutions)}


def compile_task_voice_request(db, project_root, task_id, request, *, reading_capabilities=None, reading_strategy=None):
    """Compile a browser request; trusted reading evidence is a separate argument.

    No generation request or draft is written. The caller supplies base revision
    to VoiceEditing.create_draft only after it rechecks task authorization.
    """
    allowed = {'context_digest', 'voice_id', 'rules'}
    if (not isinstance(request, dict) or set(request) != allowed or not isinstance(request.get('rules'), list)
            or len(request['rules']) > 500):
        _fail('VOICE_REQUEST_INVALID', '配音请求字段无效')
    context = resolve_voice_task_context(db, project_root, task_id)
    if request['context_digest'] != context.context_digest:
        _fail('CONTEXT_CHANGED', '原稿或配音服务已变化，请重新加载')
    snapshot = _binding(db, task_id)
    if snapshot['configDigest'] != context.voice['revision']:
        _fail('CONTEXT_CHANGED', '配音服务已变化，请重新加载')
    selected = _voice(db, snapshot, request['voice_id'])
    rules = []
    for rule in request['rules']:
        if (not isinstance(rule, dict) or set(rule) != {'start', 'end', 'original', 'reading', 'version', 'source'}
                or rule['source'] != 'user'):
            _fail('VOICE_RULE_INVALID', '读法规则必须来自本次明确选区编辑')
        start, end = utf16_span_to_unicode(context.script, rule['start'], rule['end'])
        rules.append(dict(rule, start=start, end=end))
    capabilities = _reading_capabilities(context, selected['voiceId'], rules, reading_capabilities, reading_strategy)
    voice = dict(context.voice, voice=selected['voiceId'], resource=selected['resourceId'])
    config = compile_voice_input(context.script, rules, voice, capabilities)
    final = resolve_voice_task_context(db, project_root, task_id)
    final_binding = _binding(db, task_id)
    _voice(db, final_binding, selected['voiceId'])
    if (final.context_digest != context.context_digest
            or final_binding['configDigest'] != context.voice['revision']):
        _fail('CONTEXT_CHANGED', '原稿或配音服务已变化，请重新加载')
    return dict(context_digest=context.context_digest, config=config,
                selected_voice=dict(id=selected['voiceId'], label=selected['displayName']))
