"""Independent planning consumes registered audio; never falls back to old TTS."""
import json
import shutil
import uuid
from pathlib import Path
from contextlib import nullcontext


class _JoinedTransaction:
    """Pass existing domain writers one owned transaction without nested commits."""
    def __init__(self, db): self._db = db
    def __getattr__(self, name): return getattr(self._db, name)
    def transaction(self, **kwargs):
        if not self._db.connection.in_transaction:
            raise RuntimeError('STORYBOARD_TRANSACTION_REQUIRED')
        return nullcontext(self._db.connection)


def recommend_registered_storyboard(db, job, root, runner):
    from . import ad_actions as ad
    from .voice_bundle_service import _service, read_registered_voice, preview_voice_timing
    from .voice_task_context import resolve_voice_task_context

    task_root, task, brief, script_path, script_sha = ad._task_context(db, job, root)
    task_id = task['task_id']
    ad.require_source_readiness(db, task_id)
    context = resolve_voice_task_context(db, root, task_id)
    head = _service(db, root, task_id).current(task_id)
    checked = read_registered_voice(db, root, task_id, head['audio'], head['config'], context.context_digest)
    cards_before = ad.list_scene_cards(db, task_id)
    projection = preview_voice_timing(db, root, task_id, head['audio'], head['config'], context.context_digest)
    timing = projection['timing']
    texts = [row['text'] for row in timing['segments']]
    if ''.join(texts) != script_path.read_text(encoding='utf-8'):
        raise RuntimeError('VOICE_PLAN_TIMING_MISMATCH: 实测分句未覆盖锁定原文')
    # Missing material is a normal storyboard outcome; never invent voice anchors.
    allowed_gaps = {'SCENE_ASSET_REQUIRED', 'SOURCE_WINDOW_INVALID', 'SOURCE_WINDOW_TOO_SHORT', 'SHOT_COVERAGE_MISSING'}
    if any(row['code'] not in allowed_gaps for row in projection['gaps']):
        raise RuntimeError('VOICE_TIMING_REVIEW_REQUIRED: 当前镜头与实测配音需重新核对')
    def sources():
        return [dict(row) for row in db.connection.execute(
            'SELECT * FROM task_source_snapshots WHERE task_id=? ORDER BY rowid', (task_id,))]
    source_rows = sources()
    by_id = {row['card_id']: row for row in cards_before}
    for update in projection.get('scene_updates', []):
        current = by_id.get(update['card_id'])
        if current is None or any(current.get(key) != update[key] for key in ('narrative_start','narrative_end')):
            raise RuntimeError('VOICE_TIMING_REVIEW_REQUIRED: 分镜时间尚未与已确认声音同步')
    capability = 'storyboard'
    model = ad.get_task_profile_binding_for_usage(db, task_id, capability)
    if model is None:
        capability = 'general_text'
        model = ad.get_task_profile_binding_for_usage(db, task_id, capability)
    if model is None:
        raise RuntimeError('GENERAL_TEXT_NOT_CONFIGURED: 请配置本片分镜模型')
    voice_binding = dict(revision=head['revision'], context_digest=context.context_digest,
        bundle_id=checked['bundle_id'], audio_sha256=head['audio']['sha256'],
        timeline_digest=ad._canonical_digest(checked['timeline']))
    binding = dict(task_id=task_id, script_sha256=script_sha, source_snapshot_ids=[row['snapshot_id'] for row in source_rows],
        ratio=brief['aspect_ratio'], registered_voice=voice_binding, general_profile_digest=model.get('configDigest'))
    digest = ad._canonical_digest(binding)
    directory = task_root/'artifacts/ad'
    directory.mkdir(parents=True, exist_ok=True)
    plan_path, metadata_path = directory/'adplan.json', directory/'recommendation.json'
    prior = None
    if metadata_path.exists():
        prior = json.loads(metadata_path.read_text(encoding='utf-8'))
        if prior.get('source_snapshot_ids') != binding['source_snapshot_ids']:
            raise RuntimeError('STORYBOARD_STALE: 原规划素材范围已变化，请先核对素材')
        if prior.get('script_sha256') != script_sha or not plan_path.is_file() or (prior.get('artifact_sha256') or {}).get('adplan.json') != ad._file_sha256(plan_path):
            raise RuntimeError('STORYBOARD_STALE: 原规划文件或文案已变化')
    nonce = uuid.uuid4().hex
    units_path = directory/('registered-units-'+nonce+'.json')
    output_path = directory/('registered-plan-'+nonce+'.json')
    # This is a generated result, not a completed storyboard. Keep it outside
    # the commit rollback so a local failure never forces another paid request.
    cache_binding = dict(binding, source_digest=ad._canonical_digest(source_rows),
        model_digest=ad._canonical_digest(model), texts=texts)
    cache_path = directory/'generated-plans'/(ad._canonical_digest(cache_binding)+'.json')
    cache_reused = False
    written = []
    def write_bound(path, value):
        before = path.read_bytes() if path.exists() else None
        ad._write_json_atomically(path, value)
        written.append((path, before, path.read_bytes()))
    try:
        if plan_path.exists():
            plan = json.loads(plan_path.read_text(encoding='utf-8'))
        elif cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding='utf-8'))
                plan = cached['plan']
                if (cached.get('binding') != cache_binding
                        or cached.get('plan_sha256') != ad._canonical_digest(plan)):
                    raise ValueError('binding or digest mismatch')
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise RuntimeError('STORYBOARD_CACHE_INVALID: 已生成规划缓存损坏，请检查后再重试，未再次调用模型') from error
            cache_reused = True
        else:
            ad._write_json_atomically(units_path, texts)
            python = root/'.venv/Scripts/python.exe'
            if not python.is_file(): python = root/'.venv/bin/python'
            node = root/'node.exe'
            if not node.is_file(): node = Path(shutil.which('node') or node)
            command = [str(python), '-X', 'utf8', str(root/'scripts/ad_pipeline_runner.py'),
                '--project-root', str(root), '--node', str(node), '--ts-node', str(root/'node_modules/ts-node/dist/bin.js'),
                '--catalog-db', str(Path(db.path).resolve()), '--task-id', task_id, '--text-capability', capability,
                '--script', str(script_path), '--ratio', brief['aspect_ratio'], '--plan-output', str(output_path),
                '--timing-output', str(directory/'adtiming.json'), '--voice-output', str(directory/'voice.mp3'),
                '--segments-dir', str(directory/'segments'), '--plan-only', '--fixed-segments', str(units_path)]
            ad._run_checked(runner, command, cwd=root, timeout_seconds=3600)
            plan = json.loads(output_path.read_text(encoding='utf-8'))
        ad._validate_ad_plan(context.script, brief['aspect_ratio'], plan)
        if [row['text'] for row in plan['segments']] != texts:
            raise RuntimeError('VOICE_PLAN_TIMING_MISMATCH: 分镜必须沿用已确认配音的实测分句')
        if not plan_path.exists() and not cache_reused:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            ad._write_json_atomically(cache_path, dict(binding=cache_binding,
                plan=plan, plan_sha256=ad._canonical_digest(plan), status='generated'))
        with db.transaction(immediate=True):
            try:
                ad.require_source_readiness(db, task_id)
                if (_service(db, root, task_id).current(task_id) != head
                    or resolve_voice_task_context(db, root, task_id).context_digest != context.context_digest
                    or sources() != source_rows or ad.list_scene_cards(db, task_id) != cards_before
                    or ad.get_task_profile_binding_for_usage(db, task_id, capability) != model):
                    raise RuntimeError('VOICE_STORYBOARD_INPUT_CHANGED: 配音或素材输入已变化，请重新规划')
                read_registered_voice(db, root, task_id, head['audio'], head['config'], context.context_digest)
                if plan_path.exists() and json.loads(plan_path.read_text(encoding='utf-8')) != plan:
                    raise RuntimeError('STORYBOARD_EXISTS: 现有规划已变化，不可覆盖')
                writer = _JoinedTransaction(db)
                cards = cards_before or ad._persist_ad_cards(writer, task_id, plan, timing, script_sha)
                if not plan_path.exists(): write_bound(plan_path, plan)
                metadata = dict(binding, input_digest=digest, source='standalone_model', voice_source='registered',
                    scene_card_count=len(cards), missing_count=sum(not row.get('asset_id') for row in cards),
                    artifact_sha256={'adplan.json': ad._file_sha256(plan_path)})
                write_bound(metadata_path, metadata)
                ad._complete_storyboard_generation(writer, task_id, input_digest=digest,
                    details=dict(source='standalone_model', voice_source='registered',
                        scene_card_count=len(cards), missing_count=metadata['missing_count']))
            except BaseException:
                for path, before, after in reversed(written):
                    if path.is_file() and path.read_bytes() == after:
                        if before is None: path.unlink()
                        else: path.write_bytes(before)
                written.clear()
                raise
        return ad._action_result('recommend_storyboard:complete', '分镜已生成，沿用已确认配音',
            dict(reused=prior is not None or cache_reused, voice_source='registered', voice_revision=head['revision'],
                scene_card_count=len(cards), missing_count=metadata['missing_count'], plan_path=str(plan_path),
                timing_path=str(checked['path'].parent/'timeline.json'), voice_path=str(checked['path'])))
    except BaseException:
        for path, before, after in reversed(written):
            if path.is_file() and path.read_bytes() == after:
                if before is None: path.unlink()
                else: path.write_bytes(before)
        raise
    finally:
        units_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
