"""Explicit, read-only frozen render inputs and historical review relationships.

The caller owns file observation and unresolved accounting. No migrations,
renderers, current catalog revalidation, approvals, or signature writes occur.
Unknown producer/structure/version stays unresolved; this adapter never grants
snapshot or installation switching permission.
"""
import hashlib
import json
import math
from pathlib import Path
import re

from .path_utils import filesystem_path


class RenderReferenceError(ValueError):
    """Stable refusal code; ROOT may translate it to its RecoveryPlanError."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(code + ': ' + message)


def _fail(code, message):
    raise RenderReferenceError(code, message)


def _object(value, code='FROZEN_DEPENDENCY_INVALID'):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            _fail(code, '冻结记录不是有效对象')
    if not isinstance(value, dict):
        _fail(code, '冻结记录不是对象')
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _signature(value):
    if (not isinstance(value, dict) or type(value.get('size_bytes')) is not int
            or value['size_bytes'] < 0 or not isinstance(value.get('sha256'), str)
            or re.fullmatch('[0-9a-f]{64}', value['sha256']) is None):
        _fail('FROZEN_DEPENDENCY_INVALID', '缺少已冻结的大小和内容摘要')
    # Writers also retain mtime/path/kind; bind accepts only content identity.
    return {key: value[key] for key in ('size_bytes', 'sha256')}


_PACKAGING_FIELDS = ('bgm', 'sfx', 'titles', 'stickers', 'fancyTexts', 'watermark')
_DIGITAL_KEYS = {
    'task_id', 'picture_lock', 'selection_state', 'shot_authorization',
    'material_authorization_attestation', 'gate_events', 'assets', 'scene_cards',
    'packaging', 'ass', 'plan', 'files', 'environment', 'plan_file', 'subtitle',
}
_AD_KEYS = {
    'route', 'task_id', 'voice_identity', 'action_input_digest', 'runtime_files',
    'files', 'scene_cards', 'packaging', 'packaging_compiled', 'recipe_digest',
    'shot_authorization', 'material_authorization_attestation', 'subtitle_evidence',
}
_SUBTITLE_KEYS = {
    'preset_id', 'alignment_coverage', 'script', 'words', 'semantics', 'ass',
    'metadata', 'font', 'alignment_review_digest', 'alignment_words',
}
_RECIPE_KEYS = {
    'recipe_version', 'audio_policy', 'task_snapshot', 'duration_us', 'output',
    'shots', 'main_audio', 'subtitle_cues', 'subtitle_style', 'subtitle_highlight',
    'packaging_instances', 'transitions', 'asset_manifest', 'dependency_digest', 'subtitle_appearance',
}
_CORE_MANIFEST_FIELDS = {
    'id', 'version', 'name', 'kind', 'type', 'file', 'font', 'fontname_guess',
    'license', 'note', 'default_for', 'sha256', 'source_url', 'source_license',
    'downloaded_at', 'attribution', 'attribution_required', 'default_gain', 'mood',
    'effect_kind', 'duration_s', 'sticker_kind', 'label', 'card_kind', 'accent_hex',
    'background_hex', 'entrance', 'fill', 'glow', 'shadow', 'stroke_hex', 'bg_hex',
    'alignment', 'ass_style', 'back_hex', 'bold', 'border_style', 'margin_v',
    'primary_hex', 'size', 'outline', 'outline_hex', 'shadow_hex', 'back_alpha_hex',
    'fade_in_ms', 'fade_out_ms', 'highlight_hex', 'max_chars_per_line', 'max_lines',
    'punch_size', 'punch_outline', 'punch_shadow', 'punch_spacing', 'spacing',
    'tail_pad_seconds', 'engine', 'language', 'voice_type_ref',
}


def inspect_render_references(db, source_root, tables, bind, pending) -> dict:
    """Inspect explicit writer shapes; bind keeps original addresses and hashes.

    bind(table, identity, field, absolute_path, content_signature) -> Path must
    reject lexical path redirection and content changes. Its exceptions propagate.
    pending(table, identity, code) receives every unsupported/legacy boundary.
    Reads only the supplied SQLite connection and already-bound render plan JSON.
    No returned object contains script, subtitle, notes, or raw recipe text.
    """
    source_root = Path(source_root)  # Old logical manifest base, never a new root.
    unresolved, snapshot_results, seen_pending = [], [], set()
    def wait(table, identity, code):
        key = (table, identity, code)
        if key not in seen_pending:
            seen_pending.add(key)
            pending(*key)
            unresolved.append({'table': table, 'rowId': identity, 'code': code})

    def rows(table):
        return [dict(r) for r in db.connection.execute('SELECT * FROM '+table)] if table in tables else []
    snapshots = {r['snapshot_id']: r for r in rows('render_snapshots')}
    candidates = rows('candidate_versions')
    reports = {r['report_id']: r for r in rows('qa_reports')}

    for identity, row in snapshots.items():
        if row.get('stage') != 'mix':
            if row.get('stage') != 'qa':
                wait('render_snapshots', identity, 'SNAPSHOT_PRODUCER_UNSUPPORTED')
            continue
        if row.get('producer') not in {'assemble-v1', 'assemble-ad-v1'}:
            wait('render_snapshots', identity, 'SNAPSHOT_PRODUCER_UNSUPPORTED')
            continue
        params = _object(row.get('params_json'))
        dep = params.get('dependency_snapshot')
        if not isinstance(dep, dict):
            wait('render_snapshots', identity, 'NESTED_RENDER_STRUCTURE_UNSUPPORTED')
            continue
        if (params.get('dependency_digest') != row.get('input_digest')
                or _digest(dep) != row.get('input_digest')):
            _fail('FROZEN_DEPENDENCY_INVALID', '嵌套输入与原快照摘要不一致')
        digital = row['producer'] == 'assemble-v1'
        required = {'task_id', 'files', 'assets', 'plan', 'packaging'} if digital else {
            'task_id', 'files', 'runtime_files', 'packaging_compiled', 'subtitle_evidence', 'recipe_digest', 'route'}
        if not required.issubset(dep):
            wait('render_snapshots', identity, 'NESTED_RENDER_STRUCTURE_UNSUPPORTED')
            continue
        if dep['task_id'] != row['task_id'] or not digital and dep['route'] != 'ad_mix':
            _fail('FROZEN_DEPENDENCY_INVALID', '输入不是原任务或原生产路线')
        if 'runtime_fonts' in dep:
            # Current inspected writers put signed font entries in files/runtime_files.
            # There is no runtime_fonts envelope version to safely interpret.
            wait('render_snapshots', identity, 'NESTED_RENDER_STRUCTURE_UNSUPPORTED')
        if set(dep) - (_DIGITAL_KEYS if digital else _AD_KEYS) - {'runtime_fonts'}:
            wait('render_snapshots', identity, 'NESTED_RENDER_FIELDS_UNSUPPORTED')
        ledger, fields = {}, []
        def signed(field, entry):
            entry = _object(entry)
            path = entry.get('path')
            if not {'size_bytes', 'sha256'}.issubset(entry):
                wait('render_snapshots', identity, 'RENDER_SOURCE_SIGNATURE_UNAVAILABLE')
                return None
            signature = _signature(entry)
            if not isinstance(path, str) or not path:
                _fail('FROZEN_DEPENDENCY_INVALID', '冻结输入缺少原地址')
            bound = bind('render_snapshots', identity, field, path, signature)
            key = str(Path(path))
            if key in ledger and ledger[key] != signature:
                _fail('FROZEN_DEPENDENCY_INVALID', '同址嵌套输入签名冲突')
            ledger[key] = signature
            fields.append(field)
            return bound
        def collection(key):
            entries = dep.get(key)
            if not isinstance(entries, list):
                wait('render_snapshots', identity, 'NESTED_RENDER_STRUCTURE_UNSUPPORTED')
                return
            for index, entry in enumerate(entries):
                signed(f'dependency_snapshot/{key}/{index}/path', entry)
        collection('files')
        if digital:
            collection('assets')
        else:
            collection('runtime_files')
        for key in ('ass', 'plan_file', 'subtitle_evidence'):
            if dep.get(key) is not None:
                signed(f'dependency_snapshot/{key}/path', dep[key])
        subtitle = dep.get('subtitle')
        if subtitle is not None:
            subtitle = _object(subtitle)
            if set(subtitle) - _SUBTITLE_KEYS:
                wait('render_snapshots', identity, 'NESTED_RENDER_FIELDS_UNSUPPORTED')
            for key in ('script', 'words', 'semantics', 'ass', 'metadata', 'font', 'alignment_words'):
                if subtitle.get(key) is not None:
                    signed(f'dependency_snapshot/subtitle/{key}/path', subtitle[key])

        def source(field, raw, required=False):
            if raw is None:
                if required:
                    wait('render_snapshots', identity, 'NESTED_RENDER_STRUCTURE_UNSUPPORTED')
                return
            if not isinstance(raw, str) or not raw:
                _fail('FROZEN_DEPENDENCY_INVALID', '已知渲染源地址格式错误')
            signature = ledger.get(str(Path(raw)))
            if signature is None:
                wait('render_snapshots', identity, 'RENDER_SOURCE_SIGNATURE_UNAVAILABLE')
                return
            bind('render_snapshots', identity, field, raw, signature)
            fields.append(field)

        def packaging(value, field):
            value = _object(value)
            for name in ('items', 'instances'):
                entries = value.get(name, [])
                if not isinstance(entries, list):
                    wait('render_snapshots', identity, 'NESTED_RENDER_STRUCTURE_UNSUPPORTED')
                    continue
                for i, entry in enumerate(entries):
                    entry = _object(entry)
                    raw = entry.get('file')
                    if raw:
                        sig = entry.get('file_signature') or ledger.get(str(Path(raw)))
                        if sig is None:
                            wait('render_snapshots', identity, 'RENDER_SOURCE_SIGNATURE_UNAVAILABLE')
                        else:
                            signed(f'{field}/{name}/{i}/file', {'path': raw, **sig})
                    if entry.get('render_value') is not None:
                        source(f'{field}/{name}/{i}/render_value/src', _object(entry['render_value']).get('src'))
                    if name == 'items' and entry.get('manifest') is not None:
                        frozen_manifest(entry, f'{field}/{name}/{i}/manifest')

        def frozen_manifest(entry, field):
            manifest = entry['manifest']
            if not isinstance(manifest, dict):
                wait('render_snapshots', identity, 'PACKAGING_MANIFEST_STRUCTURE_UNSUPPORTED')
                return
            if ('_local_root' in manifest or '_local_path' in manifest
                    or str(manifest.get('id', '')).startswith('local-bgm-')):
                wait('render_snapshots', identity, 'PACKAGING_LOCAL_REFERENCE_PENDING')
                return
            # Known core item shapes from packaging_plan/packaging_layers and the
            # shipped legacy/2026-09 manifests. Do not load the current manifests,
            # compare mutable asset versions, or reinterpret future fields.
            if (set(manifest) - _CORE_MANIFEST_FIELDS
                    or not isinstance(manifest.get('id'), str) or not manifest['id']
                    or manifest.get('version') not in (None, '2026-09')):
                wait('render_snapshots', identity, 'PACKAGING_MANIFEST_STRUCTURE_UNSUPPORTED')
                return
            if entry.get('asset_ref') is not None and entry['asset_ref'] != manifest['id']:
                _fail('FROZEN_DEPENDENCY_INVALID', '包装逻辑编号与冻结清单不一致')
            for key in ('file', 'font'):
                reference = manifest.get(key)
                if reference is None:
                    continue
                if not isinstance(reference, str) or not reference:
                    wait('render_snapshots', identity, 'PACKAGING_MANIFEST_REFERENCE_PENDING')
                    continue
                path = Path(reference)
                if '..' in path.parts or '\0' in reference:
                    wait('render_snapshots', identity, 'PACKAGING_MANIFEST_REFERENCE_PENDING')
                    continue
                if not path.is_absolute():
                    if key == 'file' and (not path.parts or path.parts[0] not in {'assets', 'public', 'asset-packs'}):
                        wait('render_snapshots', identity, 'PACKAGING_MANIFEST_REFERENCE_PENDING')
                        continue
                    # A task created after an upgrade belongs to its own frozen
                    # installation, not necessarily the oldest recovery root.
                    # Resolve only against already signed original addresses.
                    relative = Path('assets/fonts') / path if key == 'font' else path
                    original_file = entry.get('file')
                    if key == 'file' or manifest.get('file') is None:
                        candidates = [Path(original_file)] if isinstance(original_file, str) and original_file else []
                    else:
                        candidates = [Path(raw) for raw in ledger]
                    candidates = [candidate for candidate in candidates
                                  if candidate.is_absolute()
                                  and tuple(candidate.parts[-len(relative.parts):]) == tuple(relative.parts)]
                    if len(candidates) != 1:
                        if key == 'font' and manifest.get('file') is not None:
                            wait('render_snapshots', identity, 'PACKAGING_MANIFEST_REFERENCE_PENDING')
                            continue
                        _fail('FROZEN_DEPENDENCY_INVALID', '包装清单与冻结原文件地址冲突')
                    path = candidates[0]
                if key == 'file' or manifest.get('file') is None:
                    original_file = entry.get('file')
                    if not isinstance(original_file, str) or not original_file:
                        wait('render_snapshots', identity, 'PACKAGING_MANIFEST_REFERENCE_PENDING')
                        continue
                    if str(Path(original_file)) != str(path):
                        _fail('FROZEN_DEPENDENCY_INVALID', '包装清单与冻结原文件地址冲突')
                expected = ledger.get(str(path))
                # This is a reference identity check, not a fresh lookup or hash.
                # A separately used font must already have its own frozen signature.
                if expected is None:
                    wait('render_snapshots', identity, 'PACKAGING_MANIFEST_REFERENCE_PENDING')
                    continue
                if key == 'file' and manifest.get('sha256') is not None and manifest['sha256'] != expected['sha256']:
                    _fail('FROZEN_DEPENDENCY_INVALID', '包装清单与冻结原文件摘要冲突')
                source(field+'/'+key, str(path))
        packaging(dep['packaging'] if digital else dep['packaging_compiled'], 'dependency_snapshot/packaging' if digital else 'dependency_snapshot/packaging_compiled')
        if params.get('packaging') is not None:
            packaging(params['packaging'], 'params/packaging')

        def plan_sources(plan, prefix):
            for key in _PACKAGING_FIELDS:
                value = plan.get(key)
                if value is None:
                    continue
                for i, item in enumerate(value if isinstance(value, list) else [value]):
                    source(f'{prefix}/{key}/{i}/src', _object(item).get('src'))
            entries = plan.get('inserts', []) if digital else plan.get('segments', [])
            if not isinstance(entries, list):
                wait('render_snapshots', identity, 'NESTED_RENDER_STRUCTURE_UNSUPPORTED')
                entries = []
            for i, item in enumerate(entries):
                source(f'{prefix}/'+('inserts' if digital else 'segments')+f'/{i}/src', _object(item).get('src'))
            if not digital:
                voice = plan.get('voice')
                source(prefix+'/voice', voice.get('src') if isinstance(voice, dict) else voice)
        if digital:
            plan_sources(_object(dep['plan']), 'dependency_snapshot/plan')

        plan_path = params.get('plan_path')
        if plan_path:
            expected = ledger.get(str(Path(plan_path)))
            if expected is None:
                wait('render_snapshots', identity, 'RENDER_PLAN_SIGNATURE_UNAVAILABLE')
            else:
                resolved = bind('render_snapshots', identity, 'params/plan_path', plan_path, expected)
                # Read after bind's lexical guard; compare exact frozen bytes again.
                data = filesystem_path(resolved).read_bytes()
                if {'size_bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()} != expected:
                    _fail('SOURCE_CHANGED', '计划在绑定与读取之间变化')
                bind('render_snapshots', identity, 'params/plan_path', plan_path, expected)
                try:
                    plan = _object(data.decode('utf-8-sig'))
                except UnicodeError:
                    _fail('FROZEN_DEPENDENCY_INVALID', '计划不是UTF-8对象')
                recipe_text = plan.get('renderRecipeJson')
                if not isinstance(recipe_text, str):
                    wait('render_snapshots', identity, 'RENDER_RECIPE_UNAVAILABLE')
                else:
                    recipe = _object(recipe_text)
                    if type(recipe.get('recipe_version')) is not int or recipe['recipe_version'] != 1:
                        wait('render_snapshots', identity, 'RENDER_RECIPE_VERSION_UNSUPPORTED')
                    else:
                        task = _object(recipe.get('task_snapshot'))
                        if task.get('task_id') != row['task_id'] or task.get('task_type') != ('digital_human' if digital else 'ad_mix'):
                            _fail('FROZEN_DEPENDENCY_INVALID', '配方任务或路线不一致')
                        if plan.get('recipe_digest') != _digest(recipe) or not digital and dep.get('recipe_digest') != _digest(recipe):
                            _fail('FROZEN_DEPENDENCY_INVALID', '配方与已冻结摘要不一致')
                        if set(recipe) - _RECIPE_KEYS:
                            wait('render_snapshots', identity, 'NESTED_RENDER_FIELDS_UNSUPPORTED')
                        manifest = recipe.get('asset_manifest')
                        if not isinstance(manifest, list):
                            _fail('FROZEN_DEPENDENCY_INVALID', '配方没有素材签名清单')
                        for i, entry in enumerate(manifest):
                            signed(f'plan/recipe/asset_manifest/{i}/path', entry)
                        if 'subtitle_appearance' in recipe:
                            # Frozen rendering values contain fontFamily, not a
                            # live preset ID. Never resolve today's font catalog.
                            appearance = _object(recipe['subtitle_appearance'])
                            from .packaging_appearance import validate_appearance
                            values = {k: v for k, v in appearance.items() if k != 'fontFamily'}
                            try:
                                if 'fontPresetId' in values:
                                    raise ValueError('unresolved current font preset')
                                validate_appearance(values)
                            except ValueError:
                                wait('render_snapshots', identity, 'SUBTITLE_APPEARANCE_UNSUPPORTED')
                            if 'fontFamily' in appearance:
                                family = appearance['fontFamily']
                                if not isinstance(family, str) or not family.strip() or '\0' in family:
                                    _fail('FROZEN_DEPENDENCY_INVALID', '冻结字幕字体名称无效')
                                fonts = [e for e in manifest if e.get('kind') == 'subtitle_appearance_font']
                                if len(fonts) != 1:
                                    wait('render_snapshots', identity, 'SUBTITLE_APPEARANCE_FONT_UNAVAILABLE')
                        for i, shot in enumerate(recipe.get('shots', [])):
                            shot = _object(shot)
                            source(f'plan/recipe/shots/{i}/src', shot.get('src'), required=True)
                            if shot.get('render_value') is not None:
                                source(f'plan/recipe/shots/{i}/render_value/src', _object(shot['render_value']).get('src'))
                        source('plan/recipe/main_audio/src', _object(recipe.get('main_audio')).get('src'), required=True)
                        for i, item in enumerate(recipe.get('packaging_instances', [])):
                            source(f'plan/recipe/packaging_instances/{i}/render_value/src', _object(_object(item).get('render_value')).get('src'))
                        style = _object(recipe.get('subtitle_style', {}))
                        if style.get('ass_path'):
                            source('plan/recipe/subtitle_style/ass_path', style['ass_path'])
                        plan_sources(plan, 'plan')
        elif dep.get('plan_file'):
            wait('render_snapshots', identity, 'RENDER_PLAN_ADDRESS_UNAVAILABLE')
        else:
            wait('render_snapshots', identity, 'RENDER_RECIPE_UNAVAILABLE')
        if params.get('ass_path'):
            source('params/ass_path', params['ass_path'])
        snapshot_results.append({'snapshotId': identity, 'producer': row['producer'],
                                 'status': 'unresolved' if any(u['table']=='render_snapshots' and u['rowId']==identity for u in unresolved) else 'verified',
                                 'fields': fields})

    # Historical QA-only rechecks may replace candidate.qa_snapshot_id. Resolve
    # old sessions through their immutable QA.parent_snapshot -> candidate.mix.
    by_mix = {c['mix_snapshot_id']:c for c in candidates}
    review_code = 'FROZEN_REVIEW_BINDING_INVALID'
    def review_candidate(table, identity, item):
        qa = snapshots.get(item.get('snapshot_id'))
        if qa is None:
            wait(table, identity, 'REVIEW_QA_UNAVAILABLE');return None
        if qa.get('producer') not in {'assemble-v1','assemble-ad-v1'}:
            wait(table, identity, 'SNAPSHOT_PRODUCER_UNSUPPORTED');return None
        candidate = by_mix.get(qa.get('parent_snapshot'))
        if candidate is None:
            wait(table, identity, 'REVIEW_CANDIDATE_UNAVAILABLE');return None
        params = _object(qa.get('params_json'), review_code)
        mix = snapshots.get(candidate['mix_snapshot_id'])
        if (qa.get('stage')!='qa' or item.get('task_id')!=candidate['task_id']
                or qa['task_id']!=candidate['task_id'] or mix is None
                or mix['task_id']!=candidate['task_id'] or mix.get('stage')!='mix'
                or mix.get('producer')!=qa.get('producer')
                or mix.get('input_digest')!=candidate.get('input_digest')
                or Path(mix['output_path'])!=Path(candidate['output_path'])
                or qa.get('input_digest')!=candidate.get('input_digest')
                or params.get('dependency_digest')!=candidate.get('input_digest')
                or params.get('qa_report_id')!=item.get('qa_report_id')
                or Path(qa['output_path'])!=Path(candidate['output_path'])):
            _fail(review_code, '审片记录串用其他任务、QA或候选版本')
        signature = _signature(_object(candidate.get('output_signature_json'),review_code))
        if (params.get('output_signature') != signature
                or _object(mix.get('params_json'),review_code).get('output_signature')!=signature):
            _fail(review_code, 'QA与原候选内容身份不同')
        report = reports.get(item.get('qa_report_id'))
        if report is None:
            wait(table, identity, 'REVIEW_REPORT_UNAVAILABLE');return None
        report_json = _object(report.get('report_json'),review_code)
        if (report.get('task_id')!=candidate['task_id'] or report.get('video_path')!=candidate['output_path']
                or report_json.get('video_path')!=candidate['output_path']):
            _fail(review_code, '审片报告不是原候选视频')
        bind(table, identity, 'candidate/output_path', candidate['output_path'], signature)
        return candidate, signature, report

    watches, watch_rows = [], {w['session_id']:w for w in rows('review_watch_sessions')}
    for identity, item in watch_rows.items():
        resolved = review_candidate('review_watch_sessions',identity,item)
        if resolved is None:
            continue
        candidate, signature, report = resolved
        if _object(item.get('output_signature'),review_code) != signature:
            _fail(review_code,'观看内容摘要不是原候选版本')
        if item.get('status') not in {'active','completed'}:
            wait('review_watch_sessions',identity,'WATCH_STATUS_UNSUPPORTED');continue
        try:
            duration = float(_object(report.get('report_json'),review_code)['video_spec']['duration'])
            same_duration = math.isfinite(duration) and duration>0 and duration==float(item['duration_seconds'])
        except (KeyError,TypeError,ValueError,OverflowError):
            wait('review_watch_sessions',identity,'WATCH_DURATION_EVIDENCE_UNAVAILABLE')
            continue
        if not same_duration:
            _fail(review_code,'观看时长不是原QA视频时长')
        if item['status']=='completed' and not _completed(item):
            _fail(review_code,'已完成观看的覆盖或片尾状态不成立')
        watches.append({'sessionId':identity,'taskId':item['task_id'],'candidateId':candidate['candidate_id'],
                        'snapshotId':item['snapshot_id'],'reportId':item['qa_report_id'],'status':item['status']})

    approvals = []
    verified_watch_ids = {w['sessionId'] for w in watches}
    for item in rows('review_approvals'):
        identity = item['approval_id']
        resolved = review_candidate('review_approvals',identity,item)
        if resolved is None:
            continue
        candidate, signature, report = resolved
        decision = item.get('decision')
        if decision not in {'approved','rejected'}:
            wait('review_approvals',identity,'REVIEW_DECISION_UNSUPPORTED');continue
        watch_id = item.get('watch_session_id')
        if decision=='approved' and not watch_id:
            wait('review_approvals',identity,'APPROVAL_WATCH_UNAVAILABLE');continue
        if watch_id:
            watch = watch_rows.get(watch_id)
            if watch is None:
                wait('review_approvals',identity,'APPROVAL_WATCH_UNAVAILABLE');continue
            if any(watch.get(k)!=item.get(k) for k in ('task_id','snapshot_id','qa_report_id')) or _object(watch.get('output_signature'),review_code)!=signature:
                _fail(review_code,'决策借用了其他任务或版本的观看')
            if watch_id not in verified_watch_ids:
                wait('review_approvals',identity,'APPROVAL_WATCH_UNVERIFIED');continue
            if decision=='approved' and (not _completed(watch) or report.get('status')!='passed'):
                _fail(review_code,'批准没有原QA通过及完整观看关系')
        approvals.append({'approvalId':identity,'taskId':item['task_id'],'candidateId':candidate['candidate_id'],
                          'snapshotId':item['snapshot_id'],'reportId':item['qa_report_id'],
                          'watchSessionId':watch_id,'decision':decision})
    return {'phase':'render-input-and-review-inspection','auditComplete':False,
            'supported':['assemble-v1 explicit dependency inputs','assemble-ad-v1 explicit dependency inputs',
                         'RenderRecipe v1 digital/ad sources','historical QA parent candidate/watch/approval edges'],
            'snapshots':snapshot_results,'reviews':{'watches':watches,'approvals':approvals},
            'unresolved':unresolved,'remainingAdapters':['unknown producer/shape/version','local packaging references and unknown core manifest shapes',
                                                       'library and local music lookup','job result envelopes']}


def _completed(item):
    try:
        duration, covered = float(item['duration_seconds']), float(item['covered_seconds'])
        return (math.isfinite(duration) and math.isfinite(covered) and duration>0 and covered>=0
                and item.get('status')=='completed' and item.get('reached_end')==1
                and min(covered,duration)/duration>=0.98)
    except (KeyError,TypeError,ValueError,OverflowError):
        return False
