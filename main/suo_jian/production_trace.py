"""Versioned, additive production provenance. Standard library only; never renders.

Canonical source is this file. Deployment copies are hash-pinned in Task 01's
deployment receipt, including the host's suo_jian.production_trace module.
Evidence levels are independent: existence never implies execution or approval.
"""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import unquote
import uuid

SCHEMA = 'suojian.production-trace/1'
STAGES = ('source_exists', 'dependencies_available', 'execution_succeeded', 'final_referenced', 'quality_accepted')
STATUSES = ('verified', 'unverified', 'failed', 'not_executed', 'degraded')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def file_record(value):
    if not value: return {'path': None, 'exists': False, 'sha256': None}
    path=Path(value).resolve()
    if any(part.lower() in ('secrets','personal','motiongit-personal') or part.lower().startswith('.env') for part in path.parts):
        return {'path': '[private location omitted]', 'exists': None, 'sha256': None, 'reason': 'private input not inspected'}
    row={'path':str(path),'exists':path.is_file(),'sha256':None}
    if row['exists']:
        try:
            with path.open('rb') as f:row['sha256']=hashlib.file_digest(f,'sha256').hexdigest()
            row['bytes']=path.stat().st_size
        except OSError:row.update(exists=None,reason='file unavailable during evidence capture')
    return row


def read_json(path):
    if not path:return {}
    record=file_record(path)
    if not record.get('exists'):return {}
    try:return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):return {}


def capability(status='unverified', reason='No evidence recorded', evidence=None):
    if status not in STATUSES:raise ValueError('Unknown evidence status')
    return {'status':status,'reason':reason,'evidence':evidence or []}


def component(identifier, executor, implementation, *, source=None, output=None, planned=None):
    src=file_record(source);out=file_record(output)
    stages={stage:capability() for stage in STAGES}
    if src.get('exists'):stages['source_exists']=capability('verified','Source captured now; not proof of historical execution',[src])
    return {'id':identifier,'planned':planned or {'skills':[],'template':None,'card':None},
            'actual':{'executor':executor,'component':identifier,'implementation':implementation,
                      'executionStatus':'unverified','version':None},
            'source':src,'output':out,'capabilities':stages,'failureReason':None,
            'limitations':['Source snapshot is current, not necessarily the version used in a historical render.']}


def base_trace(task_id, revision, user_input, reference, video, *, input_hash=None, kind='vibe', version_id=None):
    return {'schema':SCHEMA,'writer':file_record(__file__),'traceId':uuid.uuid4().hex,'createdAt':time.time(),'mode':kind,
        'taskId':str(task_id),'revision':revision,'versionId':version_id,
        'input':{'sha256':input_hash,'snapshot':user_input,'reference':reference},
        'video':file_record(video),'shots':[], 'invocations':[],
        'delivery':{'render':'unverified','review':'unverified','visualAcceptance':'unverified',
                    'draftGeneration':'unverified','editability':{'level':'unverified','verifiedInEditor':False}},
        'warnings':[]}


def shot(identifier, start, end, components, trace, *, timebase='seconds', planned=None):
    return {'taskId':trace['taskId'],'revision':trace['revision'],'finalStoryboardId':str(identifier),
            'interval':{'start':start,'end':end,'timebase':timebase,'endExclusive':True},
            'planned':planned or {},'components':components,'visualAcceptance':capability()}


def write_trace(root, trace):
    validate(trace)
    task=re.sub(r'[^a-zA-Z0-9_.-]', '_',trace['taskId'])[:100]
    if task in ('','.','..') or not re.fullmatch('[0-9a-f]{32}',trace['traceId']):raise ValueError('Invalid trace identity')
    path=Path(root)/'work/production-traces'/task/(trace['traceId']+'.json')
    path.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive create: old traces, task files and media are never rewritten.
    with path.open('x',encoding='utf-8') as f:json.dump(trace,f,ensure_ascii=False,indent=2)
    return {'schema':SCHEMA,'traceId':trace['traceId'],'path':str(path.resolve()),'sha256':file_record(path)['sha256']}


def validate(trace):
    if trace.get('schema')!=SCHEMA:raise ValueError('Trace schema mismatch')
    for row in trace['shots']:
        if row['interval']['end']<=row['interval']['start']:raise ValueError('Empty shot interval')
        for comp in row['components']:
            for name in STAGES:
                value=comp['capabilities'][name]
                if value['status'] not in STATUSES:raise ValueError('Invalid capability status')
                if value['status']=='verified' and not value['evidence']:raise ValueError('Verified status requires evidence')
    if trace['delivery']['visualAcceptance']=='passed':
        raise ValueError('Automatic production tracing cannot grant visual acceptance')


def input_snapshot(task):
    src=task.get('input',{})
    return {k:copy.deepcopy(src.get(k)) for k in ('title','script','materials','format','styleIntent','reference','requirements','voice') if k in src}


def version_video(root, version):
    """Resolve migrated outputs only through this module's output URL boundary."""
    recorded=Path(version.get('video') or '__missing__')
    if recorded.is_file():return recorded
    url=version.get('url','')
    if not url.startswith('/outputs/'):return recorded
    candidate=(Path(root)/unquote(url.lstrip('/'))).resolve()
    if not candidate.is_relative_to((Path(root)/'outputs').resolve()):raise ValueError('Output URL escapes module')
    return candidate


def trace_delivery(root, task, version):
    """Legacy and dialogue deliveries: metadata is a claim, never an execution receipt."""
    trace=base_trace(task['id'],version.get('revision'),input_snapshot(task),task.get('input',{}).get('reference'),
                     version_video(root,version),input_hash=version.get('inputHash'),version_id=version.get('id'))
    trace['input']['binding']=capability('verified','Full input revision hash matches delivery',[{'sha256':version['inputHash'],'revision':version.get('revision')}]) if version.get('inputHash') and digest(task.get('input',{}))==version['inputHash'] else capability(reason='Full input revision hash not supplied or not matching')
    trace['delivery'].update(review='pending',editability={'level':'unverified','declaredMode':version.get('editingMode'),
                                                        'verifiedInEditor':False})
    trace['warnings'].append('Delivery metadata alone cannot prove rendering, individual component execution or visual acceptance.')
    project_path=version.get('sourceProject')
    project=read_json(project_path) if project_path and Path(project_path).suffix=='.json' else {}
    # Only the explicit task-authored assembly schema is recognized; no private template packages are opened.
    assembly_path=project.get('assembly') if isinstance(project,dict) else None
    if assembly_path and (Path(assembly_path).name!='assembly.json' or Path(assembly_path).resolve().parent!=Path(project_path).resolve().parent):
        assembly_path=None
    assembly=read_json(assembly_path)
    if not isinstance(assembly,dict) or assembly.get('schema')!='suojian.park-vibe-assembly/1':
        trace['warnings'].append('No supported, bound final shot manifest; final shot count and generator calls remain unverified.')
        return trace
    bound=(assembly.get('task')==task['id'] and assembly.get('revision')==version.get('revision')
           and bool(trace['video'].get('sha256')) and trace['video']['sha256']==version.get('sha256')==assembly.get('sha256'))
    assembly_record=file_record(assembly_path)
    trace['finalPlan']=assembly_record
    trace['delivery']['render']='succeeded' if bound else 'unverified'
    if not bound:trace['warnings'].append('Assembly/task/revision/video digest mismatch; no final-reference promotion.')
    for row in sorted(assembly.get('storyVideoScenes',[]),key=lambda r:r['start']):
        comp=component('semantic-scene:'+row['name'],'task-authored Python animation','regenerated',
                       source=project.get('storyRenderer'),output=row.get('video'),
                       planned={'skills':[], 'template':{k:task.get('input',{}).get('template',{}).get(k) for k in ('key','version','sha256')} if task.get('input',{}).get('template') else None,'card':None})
        output_bound=comp['output'].get('sha256')==row.get('sha256') and bool(row.get('sha256'))
        command=assembly.get('command',[])
        referenced=str(row.get('video')) in [command[i+1] for i,x in enumerate(command[:-1]) if x=='-i']
        # Hash-bound assembly proves use of the scene file, not the upstream generator's invocation.
        if bound and output_bound and referenced:
            comp['capabilities']['final_referenced']=capability('verified','Hash-bound final assembly explicitly references this scene',[assembly_record,comp['output'],trace['video']])
        comp['limitations'].append('Historical scene generation command, dependencies and original source-code version have no structured receipt.')
        comp['sourceClips']=[{**file_record(c.get('path')),'sourceStart':c.get('sourceStart'),'duration':c.get('duration'),'binding':'declared-not-verified-inside-scene'} for c in row.get('sourceClips',[])]
        trace['shots'].append(shot(row['name'],row['start'],row['end'],[comp],trace))
    for i,(start,end) in enumerate(assembly.get('templateNativeMotionRanges',[])):
        comp=component('template-native-range:'+str(i+1),'pre-rendered MotionGit template','native',output=assembly.get('templateBase'),
                       planned={'skills':['MotionGit'],'template':{'version':assembly.get('templateVersion')},'card':None})
        comp['actual']['version']=assembly.get('templateVersion')
        comp['limitations'].append('Range is declared by assembly; pre-rendered template hash at original invocation is absent. Native refers to template implementation, not native Jianying effects.')
        trace['shots'].append(shot('template-'+str(i+1),start,end,[comp],trace))
    trace['shots'].sort(key=lambda r:r['interval']['start'])
    return trace


def observe_renderer_return(root, out, action, config, output):
    """Called by worker immediately after run_engine returns; not accepted from UI."""
    try:
        receipt={'schema':'suojian.renderer-observation/1','observedAt':time.time(),
                 'observer':'workbench.server.tool_worker/run_engine returned', 'action':action,
                 'inputHash':digest(config),'output':file_record(output),
                 'executor':'Remotion ParkVibeWorkbench', 'source':file_record(Path(root)/'engine/bridge.cjs')}
        path=Path(out)/('trace-stage-'+uuid.uuid4().hex+'.json')
        with path.open('x',encoding='utf-8') as f:json.dump(receipt,f,ensure_ascii=False,indent=2)
        return file_record(path)
    except Exception as error:return {'status':'failed','reason':type(error).__name__}


def trace_vibe_run(root, job_id, action, config, result=None, error=None, *, task_id=None, revision=None, scope=None, observation=None, tool=None):
    """Called only by the real worker after execution, never from submitted trace JSON."""
    result=result or {};root=Path(root)
    out=root/'outputs/runs'/job_id
    config_record=file_record(out/'config.json')
    trace=base_trace(task_id or 'run-'+job_id,revision,{'config':config},config.get('reference'),None,kind='vibe-worker',input_hash=digest(config))
    observed_success=not error and result.get('status') in ('completed','done')
    observed_failure=bool(error) or result.get('status')=='failed'
    failure_reason=str(error)[:500] if error else result.get('reason') or result.get('error')
    trace['invocations']=[{'action':action,'jobId':job_id,'status':'failed' if observed_failure else 'succeeded' if observed_success else 'unverified',
                          'executor':'Remotion ParkVibeWorkbench' if action in ('render','pilot','still','export_draft') else 'workbench.library.run_demo' if action=='demo' else 'workbench.library.run_native' if action=='native' else 'workbench.tasks.'+action,
                          'component':tool or {},
                          'config':config_record,'failureReason':failure_reason}]
    if action in ('render','pilot','still','export_draft'):
        video=out/('plate.mp4' if action=='export_draft' else 'still.png' if action=='still' else 'preview.mp4')
        trace['video']=file_record(video)
        receipt=read_json((observation or {}).get('path'))
        receipt_intact=bool((observation or {}).get('sha256')) and file_record((observation or {}).get('path')).get('sha256')==observation['sha256']
        expected_config={**config,'plate':True} if action=='export_draft' else config
        render_succeeded=(receipt_intact and receipt.get('schema')=='suojian.renderer-observation/1' and receipt.get('inputHash')==digest(expected_config)
            and bool(trace['video'].get('sha256')) and receipt.get('output',{}).get('sha256')==trace['video']['sha256'])
        trace['delivery']['render']='succeeded' if render_succeeded else 'failed' if error else 'unverified'
        trace['delivery']['review']='pending' if trace['delivery']['render']=='succeeded' else 'unverified'
        trace['delivery']['draftGeneration']='succeeded' if action=='export_draft' and observed_success else 'failed' if action=='export_draft' and observed_failure else 'unverified'
        trace['delivery']['editability']={'level':'baked-scenes-native-caption-audio' if action=='export_draft' and observed_success else 'unverified','verifiedInEditor':False}
        trace['finalPlan']=file_record(out/'timeline-manifest.json')
        trace['scope']={'kind':action,**(scope or {})}
        # Rendering consumes the saved config. Trace only rendered enabled shots and partial ranges.
        fps=30;cursor=0
        for index,row in enumerate(config.get('shots',[])):
            start=row.get('from',cursor);end=start+row.get('duration',0);cursor=end
            if row.get('enabled') is False or end<=start:continue
            if action=='pilot':
                start=max(start,int((scope or {}).get('from',0)))
                end=min(end,int((scope or {}).get('to',149))+1)
                if end<=start:continue
            if action=='still' and not start<=int((scope or {}).get('frame',100))<end:continue
            renderer=component('ParkVibeWorkbench','Remotion','adapted',source=root/'engine/bridge.cjs')
            renderer['output']=copy.deepcopy(trace['video'])
            renderer['actual']['executionStatus']='succeeded' if render_succeeded else 'failed' if error else 'unverified'
            renderer['actual']['component']='ParkVibeWorkbench'
            renderer['actual']['version']=read_json(root/'work/park-fusion/package.json').get('dependencies',{}).get('remotion')
            renderer['limitations']=[]
            if render_succeeded:
                evidence=[observation,trace['video'],{'observer':'workbench.server.tool_worker','jobId':job_id,'action':action}]
                renderer['capabilities']['execution_succeeded']=capability('verified','Observed worker invocation returned successfully',evidence)
                renderer['capabilities']['dependencies_available']=capability('verified','Renderer runtime completed this invocation; other Skill dependencies remain unknown',evidence)
                if action=='render':
                    renderer['capabilities']['final_referenced']=capability('verified','This renderer produced the captured full render; not a per-card visibility claim',evidence)
                else:
                    renderer['capabilities']['final_referenced']=capability(reason='Still, pilot or clean plate output is not proof of inclusion in a final film')
            elif error:
                renderer['failureReason']=str(error)[:500]
                renderer['capabilities']['execution_succeeded']=capability('failed','Observed worker failure; no success inferred')
            components=[renderer]
            names=[row.get('recipe','unidentified')]
            if row.get('cardEffect') not in (None,'none'):names.append(row['cardEffect'])
            for name in names:
                comp=component(name,'Remotion ParkVibeWorkbench (planned branch)','adapted',
                    planned={'skills':row.get('skills',[]),'template':config.get('template'),
                                          'card':row.get('cardEffect') or row.get('recipe')})
                comp['actual']['executionStatus']='failed' if error else 'unverified'
                comp['failureReason']=str(error)[:500] if error else None
                # Whole renderer success does NOT establish per-recipe branch execution.
                comp['capabilities']['execution_succeeded']=capability('failed' if error else 'unverified','Whole renderer invocation recorded; no per-component branch telemetry')
                comp['limitations'].append('Configured recipe is planned; component-level execution/reference requires renderer branch evidence.')
                components.append(comp)
            trace['shots'].append(shot(row.get('id',f'shot-{index+1}'),start,end,components,trace,timebase=f'frames/{fps}'))
        trace['warnings'].append('Whole-render success is recorded separately; configured cards are not automatically verified as executed or visible.')
    else:
        trace['toolArtifacts']=[file_record(a.get('path') if isinstance(a,dict) else a) for a in result.get('artifacts',[]) if isinstance(a,(str,dict))]
        trace['warnings'].append('This tool invocation is not a final-film render; final reference remains unverified.')
    return trace


def trace_feed(root, job_id, state, plan, *, draft_observation=None):
    """Read a real normalized execution plan; no catalog/MCP/provider calls."""
    root=Path(root);cfg=state.get('config',{})
    trace=base_trace(job_id,cfg.get('revision'),{'script':cfg.get('script'),'template_id':plan.get('template_id')},
        (cfg.get('reference_policy') or {}).get('reference_source'),plan.get('preview_path') or state.get('preview_path'),kind='feed')
    trace['backendTaskId']=plan.get('task_id')
    trace['renderVersion']=Path(trace['video']['path']).parent.name if trace['video'].get('path') else None
    trace['finalPlan']=file_record(root/'results'/job_id/'execution_plan.json')
    signature=plan.get('preview_signature') or {}
    bound=bool(signature.get('sha256')) and signature['sha256']==trace['video'].get('sha256')
    trace['bindingBasis']='existing preview signature and normalized execution plan; historical invocation not observed'
    trace['delivery'].update(render='succeeded' if bound else 'unverified',review='pending',
                            editability={'level':'unverified','verifiedInEditor':False})
    draft=Path(state.get('draft_path') or '__missing__')
    if (draft/'draft_content.json').is_file():
        trace['draft']=file_record(draft/'draft_content.json')
        trace['delivery']['draftGeneration']='unverified'
        trace['delivery']['editability']['draftArtifactPresent']=True
        if draft_observation and draft_observation.get('observer')=='unified_export/build_execution_draft returned':
            trace['delivery']['draftGeneration']='succeeded'
            trace['delivery']['editability'].update(level='native-clips-text-audio-with-approximate-overlays',
                                                   report=draft_observation.get('report'),verifiedInEditor=False)
    for row in plan.get('clips',[]):
        comp=component('source-clip','剪辑工作台 ad_mix','native',output=row.get('source'),
            planned={'skills':['Cutroom'],'template':{'id':plan.get('template_id')},'card':None})
        current=comp['output'];expected=plan.get('source_signatures',{}).get(row.get('source'),{})
        if bound and current.get('sha256') and current['sha256']==expected.get('sha256'):
            comp['capabilities']['final_referenced']=capability('verified','Source digest and hash-bound final render plan agree',[current,trace['finalPlan'],trace['video']])
        comp['sourceWindow']={'start':row.get('source_start'),'end':row.get('source_end')}
        comp['limitations'].append('Historical renderer invocation and individual visual processing not re-executed; native denotes source footage, not a Jianying effect.')
        components=[comp]
        start=row['start'];end=start+row['duration']
        for index,overlay in enumerate(plan.get('overlays',[])):
            if overlay['start']>=end or overlay['start']+overlay['duration']<=start:continue
            layer=component('overlay-'+str(index+1),'剪辑工作台 packaging overlay','adapted',output=overlay.get('source'))
            wanted=plan.get('source_signatures',{}).get(overlay.get('source'),{}).get('sha256')
            if bound and wanted and layer['output'].get('sha256')==wanted:
                layer['capabilities']['final_referenced']=capability('verified','Overlay window and source hash in final render plan',[layer['output'],trace['finalPlan']])
            components.append(layer)
        trace['shots'].append(shot(row['id'],start,end,components,trace))
    trace['warnings'].append('Legacy plan has no task revision field; revision stays null. Source/plan binding is not proof of Skill invocation or visual acceptance.')
    if not bound:trace['warnings'].append('Final preview hash absent or changed; final-reference claims remain unverified.')
    return trace


def safe_record(root, builder, *args, **kwargs):
    """Trace failure never invalidates an already generated film; caller surfaces error."""
    try:return write_trace(root,builder(root,*args,**kwargs))
    except Exception as error:
        return {'schema':SCHEMA,'status':'failed','reason':type(error).__name__+': '+str(error)[:300]}
