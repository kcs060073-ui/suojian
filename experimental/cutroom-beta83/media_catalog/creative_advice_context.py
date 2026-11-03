"""Bounded read-only context and reuse of already verified creative evidence."""
from __future__ import annotations
import hashlib, json, math
from pathlib import Path
from .content_review import _current_candidate, _public
from .candidate_versions import list_candidates
from .quality_windows import quality_for_window
from .packaging_plan import get_plan, active_instances
from .snapshots import file_content_signature

VERSION='creative-context-v1'
RHYTHM_CATEGORIES={'rhythm','pacing','节奏','画面安排'}
def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def text(value,limit=240):return value[:limit] if isinstance(value,str) else ''
def finite(value):return type(value) in (float,int) and math.isfinite(value)
def _candidate(db,task_id):
    candidates=list_candidates(db,task_id)
    if not candidates:return None
    try:return _current_candidate(db,task_id,candidates[0]['candidate_id'])
    except (RuntimeError,ValueError,KeyError,OSError):return None

def current_review(db,task_id):
    candidate=_candidate(db,task_id)
    if candidate is None:return None
    row=db.connection.execute('SELECT * FROM content_review_runs WHERE task_id=? AND candidate_id=? ORDER BY rowid DESC LIMIT 1',(task_id,candidate['candidate_id'])).fetchone()
    if row is None or row['status'] not in {'completed','partial'}:return None
    if (row['qa_snapshot_id']!=candidate['qa_snapshot_id'] or row['qa_report_id']!=candidate['qa_report_id'] or row['video_sha256']!=candidate['output_signature']['sha256']):return None
    value=_public(row)
    # Only allowlisted scalar facts leave the task; bodies/paths are not context.
    coverage=[text(v,256) for v in value['coverage_refs'][:20] if isinstance(v,str)]
    issues=[]
    for issue in value['issues'][:12]:
        refs=issue.get('evidence_refs',[])
        if not isinstance(refs,list) or not set(refs).issubset(value['coverage_refs']):continue
        at=issue.get('position_seconds')
        if not finite(at) or not 0<=at<=row['duration_seconds']:continue
        issues.append({k:text(issue.get(k),400) for k in ('category','severity','description','recommendation','target_ref')} | {'evidence_refs':[text(v,256) for v in refs[:8]],'position_seconds':at})
    return {'runId':row['run_id'],'candidateId':row['candidate_id'],'qaSnapshotId':row['qa_snapshot_id'],'qaReportId':row['qa_report_id'],'videoSha256':row['video_sha256'],'status':row['status'],'modelVersion':text(row['model_version']),'promptVersion':text(row['prompt_version']),'conclusion':row['conclusion'],'coverageRefs':coverage,'uncovered':[text(v) for v in value['uncovered'][:8]],'unknowns':[text(v) for v in value['unknowns'][:8]],'issues':issues}

def presenter_mapping(db,task,cards):
    if task['task_type']!='digital_human':return {},None
    candidate=_candidate(db,task['task_id'])
    if candidate is None:return {},None
    try:
        row=db.connection.execute("SELECT params_json FROM render_snapshots WHERE snapshot_id=? AND task_id=? AND stage='mix'",(candidate['mix_snapshot_id'],task['task_id'])).fetchone()
        params=json.loads(row['params_json']);dependency=params['dependency_snapshot']
        plan_path=Path(params['plan_path']);signature=dependency['plan_file']
        if (Path(signature['path']).resolve()!=plan_path.resolve() or any(signature.get(k)!=v for k,v in file_content_signature(plan_path).items())):return {},None
        plan=json.loads(plan_path.read_text(encoding='utf-8'));recipe=json.loads(plan['renderRecipeJson'])
        if digest(recipe)!=plan['recipe_digest'] or recipe['task_snapshot']['task_id']!=task['task_id']:return {},None
        from .digital_render_recipe import digital_plan_from_recipe
        # Use the production recipe validator and stored exact source windows.
        digital_plan_from_recipe(recipe)
        frozen={c['card_id']:c for c in dependency['scene_cards']}
        keys=('idx','asset_id','segment_id','mix_form','narrative_start','narrative_end','enter_tc','exit_tc','audio_policy_draft','confirm_status')
        if any(c['card_id'] not in frozen or any(c.get(k)!=frozen[c['card_id']].get(k) for k in keys) for c in cards):return {},None
        brief=json.loads(task['brief_json']);source=brief['digital_human_source'];aid=source['asset_id']
        from .rough_media import _scoped_location,_available_source
        loc=_scoped_location(db,task['task_id'],aid)
        path,_=_available_source(db,aid,loc['library_id'],loc['relative_path'])
        main=recipe['main_audio'];num,den=main['playback_rate_num'],main['playback_rate_den']
        if type(num) is not int or type(den) is not int or num<=0 or den<=0 or Path(main['src']).resolve()!=path.resolve():return {},None
        manifest=next(e for e in recipe['asset_manifest'] if e['kind']=='main_audio')
        if file_content_signature(path)['sha256']!=manifest['sha256']:return {},None
        mapped={}
        for shot in recipe['shots']:
            if shot['media_kind']!='presenter':continue
            if Path(shot['src']).resolve()!=path.resolve():return {},None
            a,b=shot['source_start_us'],shot['source_end_us'];x,y=shot['output_start_us'],shot['output_end_us']
            if not all(type(v) is int for v in (a,b,x,y)) or not 0<=a<b or abs((b-a)*den-(y-x)*num)>den:return {},None
            mapped[shot['shot_id']]={'assetId':aid,'sourceStart':a/1e6,'sourceEnd':b/1e6,'kind':'video'}
        voice={'status':'verified_render_recipe','audioInput':False,'audioBytesRechecked':True,'source':'current_digital_render_recipe','durationUs':recipe['duration_us'],'playbackRate':{'numerator':num,'denominator':den},'sourceAudioSha256':manifest['sha256'],'candidateId':candidate['candidate_id']}
        return mapped,voice
    except (KeyError,TypeError,ValueError,RuntimeError,OSError,StopIteration):return {},None

def _voice(db,task,projection,project_root=None):
    groups=projection['groups']
    base={'audioInput':False,'audioBytesRechecked':False,'status':'unverified_audio','source':'current_script_segment_timeline','durationUs':max(g['output_end_us'] for g in groups),'segments':[{'segmentId':g['segment_id'],'startUs':g['output_start_us'],'endUs':g['output_end_us'],'text':text(g['text'],600)} for g in groups[:40]],'omittedSegments':max(0,len(groups)-40)}
    from .creative_voice_evidence import current_voice_evidence
    measured=current_voice_evidence(db,project_root,task,projection)
    if measured:base.update(measured)
    return base

def _packaging(db,task_id):
    row=db.connection.execute('SELECT plan_id FROM packaging_plans WHERE task_id=? ORDER BY rowid DESC LIMIT 1',(task_id,)).fetchone()
    if row is None:return {'status':'not_configured','instances':[],'omittedInstances':0}
    plan=get_plan(db,row['plan_id']);instances=active_instances(plan)
    safe=[]
    for item in instances[:16]:
        safe.append({k:(text(item.get(k),240) if k in ('instance_id','role','content_text','anchor_kind','anchor_ref') else item.get(k)) for k in ('instance_id','role','content_text','anchor_kind','anchor_ref','start_seconds','end_seconds','x','y','scale')})
    return {'status':plan['status'],'planId':row['plan_id'],'instances':safe,'omittedInstances':max(0,len(instances)-16),'limitation':'配置实例信息，不代表已看过包装后的效果'}

def enrich(db,task,cards,projection,direction,context,voice=None,project_root=None):
    quality=[]
    for shot in context['shots']:
        start=round((shot.get('sourceStart') or 0)*1e6);end=round((shot.get('sourceEnd') or 0)*1e6)
        q=quality_for_window(db,shot['assetId'],start,end)
        safe={'cardId':shot['cardId'],'status':q['status']}
        for key in ('evidenceId','methodVersion','sourceStartUs','sourceEndUs','cameraMotion','subjectMotion','humanOverride','summary','uncertaintyReason'):
            if key in q:safe[key]=text(q[key]) if isinstance(q[key],str) else q[key]
        safe['issues']=[text(v) for v in q.get('issues',[])[:8]];safe['evidenceUs']=q.get('evidenceUs',[])[:16]
        quality.append(safe)
    result={**context,'contextVersion':VERSION,'route':task['task_type'],'orientation':direction,'voice':voice or _voice(db,task,projection,project_root),'packaging':_packaging(db,task['task_id']),'quality':quality,'contentReview':current_review(db,task['task_id']),'audioLimitation':'未外发或试听音频。当前本地PCM事实仅代表时长、能量和低能量区间，不代表语调、情绪或语义听审；缺少可靠绑定时保留未知。'}
    result['evidenceDigest']=digest({k:v for k,v in result.items() if k not in ('shots','taskRevision','inputsDigest')})
    if len(json.dumps(result,ensure_ascii=False))>28000:raise ValueError('ADVICE_CONTEXT_TOO_LARGE: 当前上下文超出安全范围，请缩小确认文案或镜头范围')
    return result

def same_core(left,right):
    return isinstance(left,dict) and isinstance(right,dict) and all(isinstance(left.get(k),str) and left.get(k) and left[k]==right.get(k) for k in ('taskRevision','inputsDigest'))

def cached_report(context):
    review=context.get('contentReview')
    if not isinstance(review,dict):return None
    items=[]
    for issue in review.get('issues',[]):
        if issue.get('category') not in RHYTHM_CATEGORIES or issue.get('severity')!='suggestion':continue
        refs=issue.get('evidence_refs',[]);at=issue.get('position_seconds')
        if not finite(at) or not refs or not any(r.startswith('frame:') for r in refs) or not set(refs).issubset(review['coverageRefs']):continue
        shot=next((s for s in context['shots'] if s['startUs']<=at*1e6<s['endUs']),None)
        if shot is None:continue
        items.append({'cardId':shot['cardId'],'kind':'rhythm','observation':text(issue['description'],400),'suggestion':text(issue['recommendation'],400),'frameRefs':refs,'startUs':shot['startUs'],'endUs':shot['endUs'],'text':shot['text']})
        if len(items)==3:break
    if not items:return None
    ref='content-review:'+review['runId']+':'+digest(context)
    result={'summary':'复用本版内容检查中的既有节奏建议；本次未调用模型。','source':'content_review_cache','sourceReviewId':review['runId'],'providerCalls':0,'items':items,'taskRevision':context['taskRevision'],'inputsDigest':context['inputsDigest'],'modelVersion':review['modelVersion'],'promptVersion':review['promptVersion'],'unknowns':review['unknowns'],'limitations':['仅复用原成片稀疏覆盖中的原观察与建议，不是新的源片或全片节奏检查。','未输入或试听音频；未覆盖区间继续未知。'],'coverage':{'sampledCards':len({i['cardId'] for i in items}),'totalCards':context['totalCards'],'frames':[],'coverageRefs':review['coverageRefs'],'uncovered':review['uncovered'],'unobservedCardIds':[s['cardId'] for s in context['shots'] if s['cardId'] not in {i['cardId'] for i in items}]+context['unobservedCardIds']}}
    return {'jobId':None,'evidenceRef':ref,'source':'content_review_cache','status':'saved','stale':False,'problem':None,'result':result,'canRetry':False,'retryOfJobId':None}
