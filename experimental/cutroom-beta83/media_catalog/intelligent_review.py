"""Product entry for candidate-bound final-film visual and mixed-audio review."""
import json
import re
from .model_profiles import list_model_profiles, list_provider_accounts
from .candidate_versions import list_candidates
from .content_review import get_content_review, begin_content_review, fail_content_review
from .content_review_jobs import queue_content_review, _profile_snapshot
from .audio_review_jobs import _account_snapshot


def review_options(db):
    profiles=[p for p in list_model_profiles(db, capability='visual_analysis')
              if p['enabled'] and p['availabilityStatus']=='available']
    profiles.sort(key=lambda p:not p['isDefault'])
    accounts=[a for a in list_provider_accounts(db)
              if a['baseUrl'].rstrip('/')=='https://open.bigmodel.cn/api/paas/v4']
    if not profiles:
        raise ValueError('INTELLIGENT_REVIEW_SETUP: 请先检测并启用视觉模型')
    if not accounts:
        raise ValueError('INTELLIGENT_REVIEW_SETUP: 请先配置智谱声音检查账号')
    profile=profiles[0]
    account=next((a for a in accounts if a['accountId']==profile['accountId']),accounts[0])
    visual=_profile_snapshot(db,profile['profileId'])
    audio=_account_snapshot(db,account['accountId'])
    return {'profile_id':profile['profileId'],'audio_account_id':account['accountId'],
            'profile_digest':visual['digest'],'audio_account_digest':audio['digest']}


_REVIEW_SETUP_ERRORS = frozenset({
    'VISUAL_PROFILE_UNVERIFIED', 'VISUAL_PROFILE_INVALID',
    'INTELLIGENT_REVIEW_SETUP', 'AUDIO_REVIEW_ACCOUNT_INVALID',
})


def render_review_options(db):
    """Preserve the review request without making local rendering depend on AI setup."""
    try:
        return review_options(db)
    except ValueError as error:
        if str(error).partition(':')[0] not in _REVIEW_SETUP_ERRORS:
            raise
        # Keep the exact reason; do not refresh verification timestamps or silently
        # select a different model later. A separate explicit review can retry it.
        return {'setup_error': str(error)}


def queue_intelligent_review(db, root, task_id, candidate_id, options, *, request_id=None):
    # A render retry or page refresh reuses its exact candidate request.
    if options.get('profile_digest') and _profile_snapshot(db,options['profile_id'])['digest']!=options['profile_digest']:
        raise RuntimeError('INTELLIGENT_REVIEW_CONFIG_CHANGED: 视觉配置已变化，请重新开始检查')
    if options.get('audio_account_digest') and _account_snapshot(db,options['audio_account_id'])['digest']!=options['audio_account_digest']:
        raise RuntimeError('INTELLIGENT_REVIEW_CONFIG_CHANGED: 声音配置已变化，请重新开始检查')
    return queue_content_review(db,root,task_id,candidate_id,
        profile_id=options['profile_id'],audio_account_id=options['audio_account_id'],
        request_id=request_id or 'intelligent-'+candidate_id,confirm_paid_call=True,
        confirm_audio_call=True,review_mode='audiovisual',supplemental=True)


def after_render(db, root, job, result):
    options=job.payload.get('intelligent_review')
    if not options or result.get('cursor')!='render_and_qa:complete':return result
    candidate=None
    try:
        candidates=list_candidates(db,job.task_id)
        if not candidates:raise RuntimeError('INTELLIGENT_REVIEW_CANDIDATE_MISSING: 未找到本版成片')
        candidate=candidates[0]
        expected=result.get('result',{}).get('snapshots',{}).get('qa_snapshot_id')
        if expected and candidate['qa_snapshot_id']!=expected:
            candidate=None
            raise RuntimeError('INTELLIGENT_REVIEW_CANDIDATE_CHANGED: 成片版本已变化，请检查当前版本')
        if options.get('setup_error'):
            raise ValueError(options['setup_error'])
        queued=queue_intelligent_review(db,root,job.task_id,candidate['candidate_id'],options)
        status={'status':'queued',**queued}
    except (ValueError,RuntimeError,KeyError) as error:
        # The rendered film remains usable; the check can be submitted separately.
        status={'status':'not_started','reason':str(error)}
        if candidate and not get_content_review(db,job.task_id,candidate['candidate_id']):
            run=begin_content_review(db,job.task_id,candidate['candidate_id'],executor='vision_service',
                prompt_version='intelligent-review-dispatch-v1',model_version='not-dispatched',supplemental=True)
            fail_content_review(db,run['run_id'],reason=str(error))
    return {**result,'result':{**result.get('result',{}),'intelligentReview':status}}


def intelligent_review_state(db, task_id, candidate_id):
    if not candidate_id:return {'status':'awaiting_film'}
    row=db.connection.execute("""SELECT j.job_id,j.status,j.progress_current,j.progress_total,
        j.phase_label,j.error_summary,j.cursor,p.outcome_status,p.payload_json FROM jobs j
        JOIN job_payloads p USING(job_id) WHERE j.task_id=? AND j.job_type='content_review'
        AND json_extract(p.payload_json,'$.candidate_id')=?
        ORDER BY j.rowid DESC LIMIT 1""",(task_id,candidate_id)).fetchone()
    review=get_content_review(db,task_id,candidate_id)
    if row is None:return {'status':review['status'] if review else 'not_started','candidateId':candidate_id,
        'reason':review.get('error_reason') if review else None}
    payload=json.loads(row['payload_json'])
    status=row['status']
    if status=='completed' and review and row['cursor']=='content_review:'+review['run_id']:
        status=review['status']
    return {'status':status,'candidateId':candidate_id,'jobId':row['job_id'],
        'mode':payload.get('review_mode','sparse'),'audioIncluded':bool(payload.get('audio_account_id')),
        'progressCurrent':row['progress_current'],'progressTotal':row['progress_total'],
        'phase':row['phase_label'],'reason':row['error_summary'],
        'outcomeUnknown':row['outcome_status']=='unknown',
        # This is only permission to attempt local receipt verification. The POST
        # still proves the exact candidate/config/receipt chain before enqueueing;
        # GET must neither reclassify the job nor imply a safe provider retry.
        'canVerifyLocalRecovery':status=='interrupted' and row['outcome_status']=='unknown'
            and payload.get('review_mode') in {'timeline','audiovisual'}
            and bool(re.match(r'^(?:(?:ValueError|RuntimeError):\s*)?VISUAL_SAMPLE_(?:FAILED|INVALID):',row['error_summary'] or '')),
        'issueCount':len(review['issues']) if review else 0,
        'audioInsufficient':bool(review and any('听审证据不足' in x for x in review['uncovered']))}
