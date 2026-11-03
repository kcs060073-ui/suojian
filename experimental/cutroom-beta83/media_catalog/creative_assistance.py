"""Local, evidence-bound gap diagnosis. Reports are advice, never task edits."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from .candidate_ranking import observed_text_match
from .ad_matching import load_observed_ad_windows, match_ad_shots, _content_score, TRANSITION_HANDLE_SECONDS
from .script_segments import storyboard_digest, project_script_shots
from .task_sources import _active_scope_rows, _is_within_scope, _filesystem_scope_manifest
from .rough_cut import _required_ad_ratio, _source_window, _snapshot_state
from .visual_uniqueness import validate_scene_card_uniqueness
from .scanner import classify_media
METHOD_VERSION = 'local-gap-v1'
REASONS = {'observed_unmatched': ('已有有效画面观察，但未匹配本条需求，请逐项核对', '对照需求与现有观察逐项核对；未匹配不代表素材不存在，也不自动解除缺口'), 'missing_content': ('当前授权范围没有匹配的已观察画面', '补充素材或考虑补拍，再分析重检'), 'unanalyzed': ('素材尚未完成有效分析', '先完成技术探测/画面分析或人工标记，再重检'), 'offline': ('授权素材库或素材当前离线', '先重连素材目录并同步，再重检'), 'orientation': ('内容匹配，但素材方向不符', '补充与任务方向一致的素材，不自动转换横竖'), 'duration': ('内容匹配，但连续可用时长不足', '补充足够长的片段或调整镜头分配后重检'), 'source_scope': ('任务尚无可用授权范围', '先确认本任务素材范围，再重检'), 'unusable': ('现有素材受文件或人工可用区间限制', '检查文件、人工标记或区间，修复后重检'), 'allocation': ('候选与本片已选画面冲突', '调整已有镜头分配或补充其他画面，再重检')}

def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def _context(db, task_id, expected_revision=None):
    task = db.connection.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if task is None:
        raise KeyError('RESHOOT_TASK_NOT_FOUND: 任务不存在')
    if task['task_type'] not in {'digital_human', 'ad_mix'}:
        raise ValueError('RESHOOT_ROUTE: 不支持的任务路线')
    revision = storyboard_digest(db, task_id)
    if not revision:
        raise RuntimeError('RESHOOT_TIMING_REQUIRED: 需要当前确认文案及实测声音时间轴，不推定时长')
    if expected_revision is not None and (not isinstance(expected_revision, str) or expected_revision != revision):
        raise RuntimeError('RESHOOT_REVISION_CHANGED: 任务已修改，请刷新并重新核验缺口')
    cards = [dict(r) for r in db.connection.execute('SELECT * FROM scene_cards WHERE task_id=? ORDER BY idx,card_id', (task_id,))]
    projection = project_script_shots(db, task_id, cards)
    direction = '9:16' if task['task_type'] == 'digital_human' else _required_ad_ratio(db.connection, task_id)
    return (dict(task), revision, cards, projection, direction)

def _catalog(db, task_id):
    """Reuse source synchronization inventory; never create another catalogue."""
    scopes = [dict(r) for r in _active_scope_rows(db, task_id)]
    assets = {}
    state = []
    offline_scope = False
    unregistered = False
    for scope in scopes:
        online, manifest, error = _filesystem_scope_manifest(scope) if scope['online'] else (False, [], None)
        offline_scope = offline_scope or not online
        inventory = {m['relative_path']: m for m in manifest}
        state.append({'scope': {k: scope[k] for k in ('scope_id', 'library_id', 'relative_path', 'source_mode')}, 'online': online, 'manifest': manifest, 'readError': bool(error)})
        rows = db.connection.execute('SELECT a.*,al.relative_path,al.state,ta.role AS task_role FROM asset_locations al JOIN assets a USING(asset_id) LEFT JOIN task_assets ta ON ta.task_id=? AND ta.asset_id=a.asset_id WHERE al.library_id=? ORDER BY al.location_id', (task_id, scope['library_id']))
        registered = set()
        for row in rows:
            a = dict(row)
            if not _is_within_scope(a['relative_path'], scope['relative_path']):
                continue
            registered.add(a['relative_path'].replace('\\', '/'))
            if a['media_kind'] not in {'video', 'image'} or a['task_role'] == 'digital_human':
                continue
            file = inventory.get(a['relative_path'].replace('\\', '/'))
            available = online and a['state'] == 'available' and (file is not None)
            changed = available and file['quick_fingerprint'] != a['quick_fingerprint']
            a.update(available=available and (not changed), changed=changed, offline=not online or a['state'] == 'offline', source_mode=scope['source_mode'])
            if a['asset_id'] not in assets or a['available']:
                assets[a['asset_id']] = a
            state.append({'asset': a})
        unregistered = unregistered or any((p not in registered and classify_media(Path(p)).value in {'video', 'image'} for p in inventory))
    ids = sorted(assets)
    evidence = []
    for table in ('analysis_runs', 'visual_observations', 'catalog_segment_marks', 'quality_evidence_runs'):
        for offset in range(0, len(ids), 400):
            batch = ids[offset:offset + 400]
            marks = ','.join(('?' for _ in batch))
            evidence.extend(([table, dict(r)] for r in db.connection.execute(f'SELECT * FROM {table} WHERE asset_id IN ({marks}) ORDER BY rowid', batch)))
    return (list(assets.values()), {'scopes': state, 'evidence': evidence, 'configured': bool(scopes), 'offline_scope': offline_scope, 'unregistered': unregistered})

def register_reshoot_need(db, task_id, script_revision, script_segment_id, origin_key, query, media_kind, start_us, end_us, card_id=None, timing_basis='measured_timeline'):
    if not query or not isinstance(query, str) or media_kind not in {'video', 'image'} or (type(start_us) is not int) or (type(end_us) is not int) or (not 0 <= start_us < end_us):
        raise ValueError('RESHOOT_NEED_INVALID: 画面需求或实测时间无效')
    seg = db.connection.execute('SELECT * FROM script_segments WHERE segment_id=? AND task_id=? AND script_revision=?', (script_segment_id, task_id, script_revision)).fetchone()
    if seg is None or start_us < seg['output_start_us'] or end_us > seg['output_end_us']:
        raise ValueError('RESHOOT_SEGMENT_INVALID: 需求未绑定当前文案段')
    gap = 'gap-' + _digest([task_id, script_revision, origin_key])[:24]
    existing = db.connection.execute(
        'SELECT gap_id FROM reshoot_requirements WHERE task_id=? AND script_revision=? AND origin_key=?',
        (task_id, script_revision, origin_key)).fetchone()
    if existing is not None:
        gap = existing['gap_id']
    elif db.connection.execute('SELECT 1 FROM reshoot_requirements WHERE gap_id=?', (gap,)).fetchone():
        # A former incarnation of the same copy remains immutable history.
        gap = 'gap-' + _digest([task_id, script_revision, origin_key, script_segment_id])[:24]

    if db.connection.execute('SELECT 1 FROM reshoot_requirement_revisions WHERE task_id=? AND old_gap_id=?', (task_id, gap)).fetchone():
        raise RuntimeError('RESHOOT_REQUIREMENT_REVISED: 这条原建议需求已修订，请使用当前缺口清单中的需求；不会重新启用旧需求')
    db.connection.execute('INSERT INTO reshoot_requirements(gap_id,task_id,script_revision,script_segment_id,origin_key,card_id,query,media_kind,start_us,end_us,timing_basis) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id,script_revision,origin_key) DO UPDATE SET start_us=excluded.start_us,end_us=excluded.end_us,timing_basis=excluded.timing_basis,active=1', (gap, task_id, script_revision, script_segment_id, origin_key, card_id, query, media_kind, start_us, end_us, timing_basis))
    return gap

def register_digital_missing(db, task_id, script_revision, missing):
    """Persist unmet insert requests even when the presenter fills their screen time."""
    segments = [dict(r) for r in db.connection.execute('SELECT * FROM script_segments WHERE task_id=? AND script_revision=? ORDER BY idx', (task_id, script_revision))]
    for item in missing:
        start, end = (item['outputStartUs'], item['outputEndUs'])
        identity = _digest([item['anchorText'], item['query'], item['kind']])
        db.connection.execute('UPDATE reshoot_requirements SET active=0 WHERE task_id=? AND script_revision=? AND origin_key LIKE ?', (task_id, script_revision, 'digital:'+identity+':%'))
        for seg in segments:
            left, right = (max(start, seg['output_start_us']), min(end, seg['output_end_us']))
            if left < right:
                register_reshoot_need(db, task_id, script_revision, seg['segment_id'], 'digital:' + identity + ':' + seg['segment_id'], item['query'], 'image' if item['kind'] == 'image' else 'video', left, right, timing_basis=item.get('timingBasis', 'measured_timeline'))

def _recover_digital_missing(db, task_id, project_root):
    task = db.connection.execute('SELECT task_type FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if task['task_type'] != 'digital_human' or project_root is None:
        return
    if any((x in task_id for x in ('/', '\\'))) or task_id in {'.', '..'}:
        raise ValueError('RESHOOT_TASK_INVALID: 任务编号无效')
    root = (Path(project_root) / 'runtime' / 'workbench' / 'tasks').resolve()
    task_root = (root / task_id).resolve()
    if not task_root.is_relative_to(root):
        raise ValueError('RESHOOT_TASK_INVALID: 任务目录越界')
    meta_path = task_root / 'artifacts' / 'storyboard' / 'recommendation.json'
    if not meta_path.is_file():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        raise RuntimeError('RESHOOT_LEGACY_INPUTS_REQUIRED: 旧分镜记录不可读，需恢复后重检') from error
    missing = meta.get('missing', [])
    if not missing:
        return
    from .planning_finalize import _task_planning_inputs, _anchor_time
    from .timeline_contract import PlaybackRate, seconds_to_us
    _, script_path, _, script_sha, words = _task_planning_inputs(db, task_id, Path(project_root).resolve())
    if meta.get('script_sha256') != script_sha:
        raise RuntimeError('RESHOOT_LEGACY_INPUTS_REQUIRED: 旧缺口不属于当前文案，不沿用')
    script = script_path.read_text(encoding='utf-8-sig')
    rate = PlaybackRate(5, 4)
    restored = []
    for item in missing:
        if 'outputStartUs' in item and 'outputEndUs' in item:
            restored.append(item)
            continue
        try:
            start, end = _anchor_time(script, words, item['anchorText'])
        except RuntimeError as error:
            if not str(error).startswith('STORYBOARD_TIMING_MISSING:'):
                raise
            segments = [dict(r) for r in db.connection.execute('SELECT * FROM script_segments WHERE task_id=? AND script_revision=? ORDER BY idx', (task_id, script_sha)) if item['anchorText'] in r['text']]
            if len(segments) != 1:
                raise RuntimeError('RESHOOT_LEGACY_INPUTS_REQUIRED: 旧缺口无法唯一归属已确认文案段，需人工核对') from error
            seg = segments[0]
            restored.append({**item, 'outputStartUs': seg['output_start_us'], 'outputEndUs': seg['output_end_us'], 'timingBasis': 'confirmed_script_segment'})
            continue
        restored.append({**item, 'outputStartUs': rate.output_us(seconds_to_us(start)), 'outputEndUs': rate.output_us(seconds_to_us(end))})
    register_digital_missing(db, task_id, script_sha, restored)

def _recover_ad_missing(db, task_id, project_root):
    if project_root is None:
        return
    task = db.connection.execute('SELECT task_type FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if task['task_type'] != 'ad_mix':
        return
    if any((x in task_id for x in ('/', '\\'))) or task_id in {'.', '..'}:
        raise ValueError('RESHOOT_TASK_INVALID: 任务编号无效')
    root = Path(project_root).resolve()
    ad_dir = root / 'runtime' / 'workbench' / 'tasks' / task_id / 'artifacts' / 'ad'
    metadata_path = ad_dir / 'recommendation.json'
    if not metadata_path.is_file():
        return
    from types import SimpleNamespace
    from .ad_actions import _task_context, _validate_ad_outputs
    _, _, brief, script_path, script_sha = _task_context(db, SimpleNamespace(task_id=task_id, payload={}), root)
    metadata = json.loads(metadata_path.read_text(encoding='utf-8-sig'))
    if metadata.get('script_sha256') != script_sha:
        raise RuntimeError('RESHOOT_LEGACY_INPUTS_REQUIRED: 旧广告需求不属于当前文案')
    plan, _ = _validate_ad_outputs(script_path.read_text(encoding='utf-8-sig'), brief['aspect_ratio'], ad_dir / 'adplan.json', ad_dir / 'adtiming.json', ad_dir / 'voice.mp3')
    for card in db.connection.execute('SELECT * FROM scene_cards WHERE task_id=? AND asset_id IS NULL ORDER BY idx', (task_id,)):
        seg = db.connection.execute('SELECT * FROM script_segments WHERE segment_id=? AND script_revision=?', (card['script_segment_id'], script_sha)).fetchone()
        if seg is None or not 0 <= seg['idx'] < len(plan['segments']):
            raise RuntimeError('RESHOOT_LEGACY_INPUTS_REQUIRED: 旧广告需求缺少可核对的文案段')
        original = plan['segments'][seg['idx']]
        if original['text'] != seg['text']:
            raise RuntimeError('RESHOOT_LEGACY_INPUTS_REQUIRED: 旧广告需求与当前文案段不一致')
        register_reshoot_need(db, task_id, script_sha, seg['segment_id'], 'card:' + card['card_id'], original['query'], original['type'], round(card['narrative_start'] * 1000000.0), round(card['narrative_end'] * 1000000.0), card['card_id'])

def _requirements(db, task_id, projection, cards):
    script_revision = projection['scriptRevision']
    for card in cards:
        if card['asset_id'] or not card['script_segment_id'] or card['mix_form'] == '数字人全屏':
            continue
        match = re.search('画面需求[：:]([^；\\n]+)', card['system_suggestion'] or '')
        query = match.group(1).strip() if match else card['narrative_text']
        register_reshoot_need(db, task_id, script_revision, card['script_segment_id'], 'card:' + card['card_id'], query, 'image' if card['mix_form'] == '信息卡' else 'video', round(card['narrative_start'] * 1000000.0), round(card['narrative_end'] * 1000000.0), card['card_id'])
    current = {c['card_id']: c for c in cards}
    needs = []
    for row in db.connection.execute('SELECT r.*,s.text FROM reshoot_requirements r JOIN script_segments s ON s.segment_id=r.script_segment_id WHERE r.task_id=? AND r.script_revision=? AND r.active=1 ORDER BY r.start_us,r.gap_id', (task_id, script_revision)):
        need = dict(row)
        if need['card_id']:
            card = current.get(need['card_id'])
            if card is None:
                continue
            need.update(start_us=round(card['narrative_start'] * 1000000.0), end_us=round(card['narrative_end'] * 1000000.0))
        needs.append(need)
    return needs

def _item(db, need, assets, windows, task, cards, direction, configured, offline_scope, unregistered):
    from .reshoot_revision import revision_fields
    duration = need['end_us'] - need['start_us']
    handle = round(TRANSITION_HANDLE_SECONDS * 1000000.0) if task['task_type'] == 'ad_mix' else 0
    required = duration + handle
    good = []
    reasons = set()
    evidence = []
    if unregistered:
        reasons.add('unanalyzed')
    matching = [w for w in windows if _content_score(need['query'], w) > 0 and w['media_kind'] == need['media_kind']]
    byid = {a['asset_id']: a for a in assets}
    observed_ids = {w['asset_id'] for w in windows}
    current_observed = [w for w in windows if w['media_kind'] == need['media_kind']
                        and byid[w['asset_id']]['available']]
    unobserved_ids = set()
    omitted_observations = 0
    # These are diagnostic observations, never matching options or closure evidence.
    # Keep other unanalyzed assets visible without describing this window as unanalyzed.
    if not matching and current_observed:
        reasons.add('observed_unmatched')
        omitted_observations = max(0, len(current_observed) - 20)
        for w in current_observed[:20]:
            evidence.append({'assetId': w['asset_id'], 'reason': 'observed_unmatched',
                             'sourceStart': w['source_start'], 'sourceEnd': w['source_end'],
                             'orientation': w['direction'],
                             'matchEvidence': observed_text_match(need['query'],
                                 usage_status=w.get('usage_status'),
                                 **{key: w.get(key) or '' for key in ('title', 'description', 'tags')})})
    for a in assets:
        if a['offline']:
            reasons.add('offline')
        elif a.get('changed'):
            reasons.add('unanalyzed')
        elif not a['available']:
            reasons.add('unusable')
        elif a['asset_id'] not in observed_ids:
            marks = db.connection.execute("SELECT 1 FROM catalog_segment_marks WHERE asset_id=? AND asset_fingerprint=? AND usage_status='不建议使用' LIMIT 1", (a['asset_id'], a['quick_fingerprint'])).fetchone()
            reasons.add('unusable' if marks else 'unanalyzed')
            if not marks and a['media_kind'] == need['media_kind']:
                unobserved_ids.add(a['asset_id'])
    for w in matching:
        a = byid[w['asset_id']]
        why = None
        if not a['available']:
            why = 'offline' if a['offline'] else 'unanalyzed' if a.get('changed') else 'unusable'
        elif w['direction'] != direction:
            why = 'orientation'
        elif w['media_kind'] == 'video' and round((w['source_end'] - w['source_start']) * 1000000.0) < required:
            why = 'duration'
        else:
            proposal = {'card_id': need['gap_id'], 'asset_id': w['asset_id'], 'mix_form': 'broll全屏', 'narrative_start': need['start_us'] / 1000000.0, 'narrative_end': need['end_us'] / 1000000.0, 'enter_tc': w['source_start'], 'exit_tc': None if w['media_kind'] == 'image' else w['source_start'] + required / 1000000.0, 'segment_id': w.get('segment_id')}
            others = [c for c in cards if c['asset_id'] and c['card_id'] != need['card_id'] and (not (task['task_type'] == 'digital_human' and c['mix_form'] == '数字人全屏'))]
            try:
                validate_scene_card_uniqueness(db.connection, others + [proposal])
            except ValueError:
                why = 'allocation'
            if why is None:
                good.append(w)
        if why:
            reasons.add(why)
        evidence.append({'assetId': w['asset_id'], 'reason': why, 'sourceStart': w['source_start'], 'sourceEnd': w['source_end'], 'orientation': w['direction']})
    if offline_scope:
        reasons.add('offline')
    fulfilled = None
    for card in cards:
        related = card['card_id'] == need['card_id'] or (need['card_id'] is None and card['script_segment_id'] == need['script_segment_id'] and (round(card['narrative_start'] * 1000000.0) <= need['start_us']) and (round(card['narrative_end'] * 1000000.0) >= need['end_us']))
        if not related or card['mix_form'] == '数字人全屏' or (not card['asset_id']):
            continue
        asset = byid.get(card['asset_id'])
        if not asset or not asset['available'] or asset['media_kind'] != need['media_kind']:
            continue
        try:
            source, selected_end = _source_window(db.connection, _snapshot_state(db.connection, task['task_id']), card)
        except ValueError:
            continue
        source = source + (need['start_us'] / 1000000.0 - card['narrative_start'])
        if need['media_kind'] == 'video' and selected_end < source + duration / 1000000.0 - 1e-06:
            continue
        if any((w['asset_id'] == card['asset_id'] and w['direction'] == direction and (w['media_kind'] == 'image' or (w['source_start'] <= source and w['source_end'] >= source + required / 1000000.0 - 1e-06)) for w in matching)):
            fulfilled = card['asset_id']
            break
    reason = None if good else next((r for r in ('source_scope', 'offline', 'observed_unmatched', 'unanalyzed', 'orientation', 'duration', 'unusable', 'allocation', 'missing_content') if r in reasons), 'missing_content')
    if not configured:
        reason = 'source_scope'
    status = 'fulfilled' if fulfilled else 'candidate_available' if good else 'open'
    if fulfilled:
        reason = None
    reshoot = status == 'open' and reason in {'missing_content', 'orientation', 'duration'}
    suggestion = f"如现场具备所需内容，可拍摄“{need['query']}”的全景及细节；先保持稳定，再按现场条件缓慢移动。使用{('竖屏' if direction == '9:16' else '横屏')}，需覆盖{duration / 1000000.0:g}秒成片；连续素材至少{required / 1000000.0:g}秒，建议前后各保留2秒稳定余量。拍摄余量为建议，现场存在与可拍性需核实。" if reshoot else None
    return {'_options': good, **revision_fields(db, need, cards), 'gapId': need['gap_id'], 'scriptSegmentId': need['script_segment_id'], 'scriptRevision': need['script_revision'], 'cardId': need['card_id'], 'text': need['text'], 'visualIntent': need['query'], 'startUs': need['start_us'], 'endUs': need['end_us'], 'durationUs': duration, 'timingBasis': need['timing_basis'], 'timingNote': '精确锚点时间待补；当前按已确认文案整段的实际时间核验' if need['timing_basis'] == 'confirmed_script_segment' else '按当前镜头或有效锚点的实测时间核验', 'requiredSourceDurationUs': required, 'orientation': direction, 'status': status, 'reason': reason, 'reasons': sorted(reasons) if status == 'open' else [], 'reasonText': REASONS[reason][0] if reason else '对应镜头已取用合格素材' if fulfilled else '已有候选可供选择；原镜头保留', 'nextAction': REASONS[reason][1] if reason else '当前镜头已满足该需求；修改后重新核验' if fulfilled else '审看候选后手动选择；不自动替换旧镜头', 'candidateAssetIds': [fulfilled] if fulfilled else sorted({w['asset_id'] for w in good}), 'evidence': evidence, 'sourceAnalysis': {'observedAssetCount': len({w['asset_id'] for w in current_observed}), 'unobservedAssetCount': len(unobserved_ids)}, 'omittedObservationEvidence': omitted_observations, 'reshootRecommended': reshoot, 'shootingSuggestion': suggestion, 'fallback': '可保留原人物画面' if task['task_type'] == 'digital_human' else '信息流保留实际缺口，不使用无关占位画面', 'closureCondition': '授权范围内有方向及连续时长合格、可用且内容匹配的画面，并经审看取用'}

def _evaluate(db, task_id, expected_revision):
    task, revision, cards, projection, direction = _context(db, task_id, expected_revision)
    needs = _requirements(db, task_id, projection, cards)
    assets, catalog = _catalog(db, task_id)
    windows = load_observed_ad_windows(db, assets)
    items = [_item(db, n, assets, windows, task, cards, direction, catalog['configured'], catalog['offline_scope'], catalog['unregistered']) for n in needs]
    pool = {}
    for item in items:
        if item['timingBasis'] == 'confirmed_script_segment':
            continue  # Context-only needs remain available for manual review, not allocation.
        for w in item['_options'] if item['status'] == 'candidate_available' else []:
            pool[w['asset_id'], w['source_start'], w['source_end']] = w
    ready = [(n, i) for n, i in zip(needs, items) if i['status'] == 'candidate_available' and i['timingBasis'] != 'confirmed_script_segment']
    matches = match_ad_shots([{'query': n['query'], 'type': n['media_kind']} for n, i in ready], [{'dur': (n['end_us'] - n['start_us']) / 1000000.0, 'start': n['start_us'] / 1000000.0, 'end': n['end_us'] / 1000000.0} for n, i in ready], list(pool.values()), direction, transition_handle_seconds=TRANSITION_HANDLE_SECONDS if task['task_type'] == 'ad_mix' else 0) if ready else []
    for (_, item), selected in zip(ready, matches):
        allowed = item['_options']
        if item['status'] == 'candidate_available' and (selected is None or not any((w['asset_id'] == selected['asset_id'] and (w['media_kind'] == 'image' or (w['source_start'] <= selected['source_start'] and selected['source_end'] <= w['source_end'] + 1e-06)) for w in allowed))):
            item.update(status='open', reason='allocation', reasons=['allocation'], candidateAssetIds=[], reasonText=REASONS['allocation'][0], nextAction=REASONS['allocation'][1], reshootRecommended=False, shootingSuggestion=None)
        elif selected is not None and item['status'] == 'candidate_available':
            item['suggestedCandidate'] = {'assetId': selected['asset_id'], 'sourceStart': selected['source_start'], 'sourceEnd': selected['source_end']}
    for item in items:
        item.pop('_options', None)
    digest = _digest({'method': METHOD_VERSION, 'needs': needs, 'catalog': catalog, 'items': items, 'direction': direction})
    return (revision, digest, items)

def build_reshoot_list(db, task_id: str, expected_revision: str, *, project_root=None) -> dict:
    if not isinstance(expected_revision, str) or not expected_revision:
        raise ValueError('RESHOOT_REVISION_REQUIRED: 缺少任务修订摘要')
    with db.transaction(immediate=True):
        return _build_reshoot_list_locked(db, task_id, expected_revision, project_root=project_root)


def _build_reshoot_list_locked(db, task_id, expected_revision, *, project_root=None):
    """Shared report builder; caller owns the single atomic writer transaction."""
    if not db.connection.in_transaction:
        raise RuntimeError("RESHOOT_TRANSACTION_REQUIRED: 缺少报告写入事务")
    _context(db, task_id, expected_revision)
    _recover_digital_missing(db, task_id, project_root)
    _recover_ad_missing(db, task_id, project_root)
    revision, digest, items = _evaluate(db, task_id, expected_revision)
    report_id = 'reshoot-' + _digest([task_id, revision, digest])[:24]
    old = db.connection.execute('SELECT result_json FROM reshoot_reports WHERE report_id=?', (report_id,)).fetchone()
    if old:
        db.connection.execute('INSERT INTO task_reshoot_current(task_id,report_id) VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET report_id=excluded.report_id', (task_id, report_id))
        return json.loads(old[0])
    result = {'reportId': report_id, 'taskId': task_id, 'taskRevision': revision, 'inputsDigest': digest, 'method': METHOD_VERSION, 'status': 'draft', 'items': items, 'openCount': sum((i['status'] == 'open' for i in items)), 'limitations': ['仅基于授权范围当前登记和有效观察文字进行本地核验，不能宣称完整AI语义匹配。', '没有发起模型、素材生成、渲染、扫描或对外消息。', '候选可用与拍摄建议不代表用户已选择、已拍摄或业务批准。']}
    db.connection.execute('INSERT INTO reshoot_reports(report_id,task_id,task_revision,inputs_digest,result_json) VALUES(?,?,?,?,?)', (report_id, task_id, revision, digest, json.dumps(result, ensure_ascii=False, sort_keys=True)))
    db.connection.execute('INSERT INTO task_reshoot_current(task_id,report_id) VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET report_id=excluded.report_id', (task_id, report_id))
    return result

def get_reshoot_list(db, task_id, report_id=None):
    if report_id is None:
        row = db.connection.execute('SELECT r.result_json FROM reshoot_reports r JOIN task_reshoot_current c USING(report_id) WHERE c.task_id=?', (task_id,)).fetchone()
    else:
        row = db.connection.execute('SELECT result_json FROM reshoot_reports WHERE task_id=? AND report_id=?', (task_id, report_id)).fetchone()
    if row is None:
        raise KeyError('RESHOOT_REPORT_NOT_FOUND: 当前任务没有这份清单')
    return json.loads(row[0])

def export_reshoot_list(db, task_id, report_id, expected_revision, *, format='text'):
    if format not in {'text', 'json'}:
        raise ValueError('RESHOOT_EXPORT_FORMAT: 只支持文本或JSON')
    with db.transaction(immediate=True):
        report = get_reshoot_list(db, task_id, report_id)
        revision, digest, _ = _evaluate(db, task_id, expected_revision)
        if revision != report['taskRevision']:
            raise RuntimeError('RESHOOT_REVISION_CHANGED: 清单不属于当前任务版本')
        if digest != report['inputsDigest']:
            raise RuntimeError('RESHOOT_INPUTS_CHANGED: 素材或分析已变化，请先重检缺口')
        if format == 'json':
            return json.dumps(report, ensure_ascii=False, indent=2)
        lines = ['CUTROOM 本地补拍与缺口处理清单（草稿）', f'任务：{task_id}', f'修订：{revision}', f'报告：{report_id}', '']
        for i in report['items']:
            lines.extend([f"{i['gapId']} · {i['status']}", f"文案：{i['text']}", f"画面需求：{i['visualIntent']}", f"方向：{i['orientation']}；实测成片覆盖：{i['durationUs'] / 1000000.0:g}秒", f"时间依据：{i['timingNote']}", f"原因：{i['reasonText']}", f"先做：{i['nextAction']}", f"解除条件：{i['closureCondition']}"])
            if i['shootingSuggestion']:
                lines.append('补拍建议：' + i['shootingSuggestion'])
            lines.append('')
        lines.extend(report['limitations'])
        return '\n'.join(lines) + '\n'
