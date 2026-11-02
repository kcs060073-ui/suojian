"""Task-bound storyboard review, preserving every imported image and revision.

No model call, render or automatic artistic acceptance is performed here.
"""
from __future__ import annotations
import copy, hashlib, json, re, shutil, subprocess, uuid
from pathlib import Path
from workbench.production import Store, LOCK, Conflict, read, save, signature
from workbench.paths import FFPROBE

SHOT_ID=re.compile(r'^[A-Za-z0-9_-]{1,48}$')
MAX_IMAGE=30*1024*1024
METHODS={'seedance','external','programmatic','footage','hybrid'}
FIELDS=('element_id','action_type','start_time','duration','motion_description',
        'start_state','end_state','execution_method','prompt','required_assets',
        'overlay_requirements','validation_rules')

def image_prompt(art, shot):
    """Build a copyable prompt from human-authored facts, without claiming AI analysis."""
    desc=shot.get('description', {})
    def pick(*keys):
        return next((str(desc[k]).strip() for k in keys if desc.get(k)), '待补充')
    return '\n'.join([
        '统一美术规范：'+art,
        '镜头 '+str(shot['id'])+'；'+str(shot['start'])+'–'+str(shot['end'])+'秒。',
        '叙事目的：'+pick('why','purpose','role'),
        '画面类型：'+pick('visualMode','type','ratio'),
        '画面主体、位置与焦点：'+pick('subject','visual')+'；'+pick('focus'),
        '环境与前中背景：'+pick('environment')+'；前景：'+pick('foreground')+'；中景：'+pick('midground')+'；背景：'+pick('background'),
        '空间关系与构图：'+pick('composition','space'),
        '真实参考的使用方式：'+pick('referenceRole','reference'),
        '实景与图示职责：'+pick('ratio','visualMode'),
        '画面关系及后续动作：'+pick('motion'),
        '动画预留空间：'+pick('motionSpace'),
        '负面约束：'+pick('negative'),
        '口播（仅供理解，不要让模型凭空生成字幕）：'+pick('voiceover','narration','text'),
        '计划标题（复杂中文、数字和界面请留白，后续程序化叠加）：'+pick('onscreenText','title'),
        '前后镜衔接：'+pick('transition'),
        '待核实与业务约束：'+pick('pending','facts'),
        '保持竖屏安全区和主体完整，保留动画所需空间；不要编造企业、数据、合作关系或真实配套；避免无关装饰。',
        '若主体、位置或业务事实标为待补充，请先由创作者补齐，不要自行猜测。'])

def digest_file(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def picture(path):
    path=Path(path)
    if not path.is_file() or path.stat().st_size>MAX_IMAGE or path.stat().st_size<12:
        raise ValueError('图片不存在或超过30MB')
    with path.open('rb') as f:magic=f.read(16)
    suffix=path.suffix.lower()
    if not ((suffix=='.png' and magic.startswith(b'\x89PNG\r\n\x1a\n')) or
            (suffix in ('.jpg','.jpeg') and magic.startswith(b'\xff\xd8\xff')) or
            (suffix=='.webp' and magic[:4]==b'RIFF' and magic[8:12]==b'WEBP')):
        raise ValueError('图片格式与内容不一致')
    return path

class Storyboards:
    def __init__(self,root):self.store=Store(root);self.root=self.store.root
    def path(self,task):return self.store.path(task['id'])/'storyboards'/('r%03d'%task['revision'])
    def load(self,task):
        state=read(self.path(task)/'state.json')
        if not state:return None
        if state['inputHash']!=task['inputHash']:raise Conflict('分镜与任务输入不匹配')
        return state
    def _task(self,identifier,payload):
        task=self.store.get(identifier);self.store.expected(task,payload)
        if payload.get('inputHash')!=task['inputHash']:raise Conflict('需要当前输入哈希')
        return task
    def get(self,identifier):
        with LOCK:
            task=self.store.get(identifier);return self.load(task)
    def create(self,identifier,payload):
        with LOCK:
            task=self._task(identifier,payload)
            if self.load(task):raise Conflict('本次输入已有分镜，不覆盖原有内容')
            shots=payload.get('shots'); art=payload.get('artDirection')
            if not isinstance(art,str) or not art.strip() or len(art)>15000:raise ValueError('需要全片美术说明')
            if not isinstance(shots,list) or not 1<=len(shots)<=30:raise ValueError('镜头数量无效')
            seen=set();rows=[];last=0.0
            for s in shots:
                ident=s.get('id','');start=float(s.get('start',-1));end=float(s.get('end',-1))
                if not SHOT_ID.fullmatch(ident) or ident in seen or start<last or end<=start or end>600:raise ValueError('镜头编号或时间无效')
                seen.add(ident);last=end
                rows.append({'id':ident,'start':start,'end':end,'description':copy.deepcopy(s),
                             'boards':[],'selectedBoard':None,'plans':[],'selectedPlan':None,'clips':[],'selectedClip':None})
            state={'schema':'suojian.vibe-storyboard/1','taskId':identifier,'revision':task['revision'],
                   'inputHash':task['inputHash'],'serial':1,'artDirection':art,'shots':rows,'events':[]}
            save(self.path(task)/'state.json',state);return state
    def mutate(self,identifier,operation,payload):
        with LOCK:
            task=self._task(identifier,payload);state=self.load(task)
            if not state:raise ValueError('请先生成并保存镜头清单')
            if payload.get('expectedSerial')!=state['serial']:raise Conflict('分镜已更新，请刷新后重试')
            row=next((s for s in state['shots'] if s['id']==payload.get('shotId')),None)
            if operation!='confirm_all' and row is None:raise ValueError('镜头不存在')
            if row and operation in ('board','request_changes','select_board','plan','edit_description'):
                for clip in row.get('clips',[]):
                    if clip['version']==row.get('selectedClip'):clip['status']='stale'
                row['selectedClip']=None
            if operation=='edit_description':
                fields=('why','visualMode','subject','environment','foreground','midground','background','composition','focus','referenceRole','motion','motionSpace','onscreenText','negative','pending')
                changes=payload.get('description')
                if not isinstance(changes,dict) or any(k not in fields or not isinstance(v,str) or len(v)>3000 for k,v in changes.items()):
                    raise ValueError('分镜描述无效')
                row.setdefault('descriptionHistory',[]).append(copy.deepcopy(row['description']))
                row['description'].update(changes)
                board=next((b for b in row['boards'] if b['version']==row['selectedBoard']),None)
                if board: board['status']='pending'
                plan=next((p for p in row['plans'] if p['version']==row['selectedPlan']),None)
                if plan: plan['status']='stale'
                row['selectedPlan']=None
            elif operation=='board':
                source=picture(Path(str(payload.get('path',''))).resolve(strict=True))
                roots=[(self.root/'outputs').resolve(),(self.root/'work').resolve(),(self.root/'work/park-fusion/public/uploads').resolve()]
                if not any(source.is_relative_to(p) for p in roots) or any(x.lower() in ('secrets','personal') for x in source.parts):raise ValueError('图片不在允许的任务素材目录')
                folder=self.path(task)/'images'/row['id'];folder.mkdir(parents=True,exist_ok=True)
                target=folder/(uuid.uuid4().hex+source.suffix.lower())
                with source.open('rb') as src,target.open('xb') as dst:shutil.copyfileobj(src,dst)
                display_name=Path(str(payload.get('originalName') or source.name)).name[:200]
                value={'version':len(row['boards'])+1,'file':str(target),'name':display_name,'sha256':digest_file(target),'status':'pending'}
                for prior in row['plans']:
                    if prior['version']==row['selectedPlan']:prior['status']='stale'
                row['boards'].append(value);row['selectedBoard']=value['version'];row['selectedPlan']=None
            elif operation=='request_changes':
                board=next((b for b in row['boards'] if b['version']==row['selectedBoard']),None)
                note=payload.get('note')
                if not board or not isinstance(note,str) or not note.strip() or len(note)>2000:raise ValueError('请填写当前镜头的修改意见')
                board['status']='changes_requested';board['feedback']=note.strip()
                for prior in row['plans']:
                    if prior['version']==row['selectedPlan']:prior['status']='stale'
                row['selectedPlan']=None
            elif operation=='select_board':
                board=next((b for b in row['boards'] if b['version']==payload.get('version')),None)
                if not board or digest_file(board['file'])!=board['sha256']:raise ValueError('历史分镜不存在或内容已变化')
                for prior in row['plans']:
                    if prior['version']==row['selectedPlan']:prior['status']='stale'
                board['status']='pending';row['selectedBoard']=board['version'];row['selectedPlan']=None
            elif operation=='confirm_board':
                board=next((b for b in row['boards'] if b['version']==row['selectedBoard']),None)
                if not board or board['status']=='changes_requested' or payload.get('boardSha256')!=board['sha256'] or digest_file(board['file'])!=board['sha256']:
                    raise Conflict('分镜图片或确认版本已变化，请重新审查')
                board['status']='confirmed'
            elif operation=='confirm_all':
                for item in state['shots']:
                    board=next((b for b in item['boards'] if b['version']==item['selectedBoard']),None)
                    if not board or board['status']=='changes_requested' or digest_file(board['file'])!=board['sha256']:raise ValueError('每个镜头须有待审或已确认且未变更的分镜图片')
                for item in state['shots']:
                    next(b for b in item['boards'] if b['version']==item['selectedBoard'])['status']='confirmed'
            elif operation=='plan':
                board=next((b for b in row['boards'] if b['version']==row['selectedBoard']),None)
                if not all(any(b['version']==s['selectedBoard'] and b['status']=='confirmed' for b in s['boards']) for s in state['shots']):raise ValueError('须先确认全部分镜')
                if digest_file(board['file'])!=board['sha256']:raise ValueError('分镜图片已变化')
                elements=payload.get('elements')
                if not isinstance(elements,list) or not elements:raise ValueError('动效计划不能为空')
                for e in elements:
                    if not isinstance(e,dict) or any(k not in e for k in FIELDS) or e['execution_method'] not in METHODS:raise ValueError('动效计划字段不完整')
                    start=float(e['start_time']);duration=float(e['duration'])
                    if start<0 or duration<=0 or start+duration>row['end']-row['start']+1e-6:raise ValueError('动效超出镜头时长')
                plan={'version':len(row['plans'])+1,'boardVersion':board['version'],'boardSha256':board['sha256'],
                      'elements':copy.deepcopy(elements),'status':'pending'}
                plan['sha256']=signature({k:v for k,v in plan.items() if k!='status'})
                row['plans'].append(plan);row['selectedPlan']=plan['version']
            elif operation=='confirm_plan':
                plan=next((p for p in row['plans'] if p['version']==row['selectedPlan']),None)
                board=next((b for b in row['boards'] if b['version']==row['selectedBoard']),None)
                if not plan or not board or payload.get('planSha256')!=plan['sha256'] or board['status']!='confirmed' or board['sha256']!=plan['boardSha256'] or digest_file(board['file'])!=board['sha256']:raise Conflict('计划或确认分镜已变，请重新检查')
                if signature({k:v for k,v in plan.items() if k not in ('status','sha256')})!=plan['sha256']:raise Conflict('计划内容已变化')
                plan['status']='confirmed'
            elif operation=='register_clip':
                board=next((b for b in row['boards'] if b['version']==row['selectedBoard']),None)
                plan=next((p for p in row['plans'] if p['version']==row['selectedPlan']),None)
                if not board or board['status']!='confirmed' or digest_file(board['file'])!=board['sha256'] or not plan or plan['status']!='confirmed' or plan['boardSha256']!=board['sha256'] or signature({k:v for k,v in plan.items() if k not in ('status','sha256')})!=plan['sha256']:raise ValueError('需要当前已确认的分镜与动效计划')
                source=Path(str(payload.get('path',''))).resolve(strict=True)
                if source.suffix.lower()!='.mp4' or not source.is_file() or not any(source.is_relative_to((self.root/p).resolve()) for p in ('outputs','work')) or any(x.lower() in ('secrets','personal') or x.startswith('.env') for x in source.parts):raise ValueError('片段必须是任务素材目录里的 MP4')
                method=payload.get('executionMethod')
                if method not in METHODS:raise ValueError('片段执行方式无效')
                actual={e['execution_method'] for e in plan['elements']}
                if method not in actual and not (method=='hybrid' and len(actual)>1):raise ValueError('片段执行方式与已确认计划不一致')
                proc=subprocess.run([FFPROBE,'-v','error','-show_entries','format=duration:stream=codec_type,width,height','-of','json',str(source)],capture_output=True,text=True,encoding='utf-8',timeout=20,check=True)
                meta=json.loads(proc.stdout);video=next((x for x in meta.get('streams',[]) if x.get('codec_type')=='video'),None);duration=float(meta.get('format',{}).get('duration',0))
                if not video or abs(duration-(row['end']-row['start']))>.25:raise ValueError('片段视频或时长不符合镜头计划')
                if (video['width']>video['height']) != (task['input']['format']=='landscape'):raise ValueError('片段画幅不符合任务')
                folder=self.path(task)/'clips'/row['id'];folder.mkdir(parents=True,exist_ok=True);target=folder/(uuid.uuid4().hex+'.mp4')
                with source.open('rb') as src,target.open('xb') as dst:shutil.copyfileobj(src,dst)
                for prior in row.get('clips',[]):
                    if prior['version']==row.get('selectedClip'):prior['status']='stale'
                clip={'version':len(row.get('clips',[]))+1,'file':str(target),'name':source.name,'sha256':digest_file(target),'duration':duration,'width':video['width'],'height':video['height'],'boardVersion':board['version'],'boardSha256':board['sha256'],'planVersion':plan['version'],'planSha256':plan['sha256'],'executionMethod':method,'externalTaskId':str(payload.get('externalTaskId',''))[:200],'status':'pending'}
                row.setdefault('clips',[]).append(clip);row['selectedClip']=clip['version']
            elif operation in ('accept_clip','request_clip_changes'):
                clip=next((c for c in row.get('clips',[]) if c['version']==row.get('selectedClip')),None)
                plan=next((p for p in row['plans'] if p['version']==row['selectedPlan']),None)
                board=next((b for b in row['boards'] if b['version']==row['selectedBoard']),None)
                if not clip or not plan or not board or board['status']!='confirmed' or digest_file(board['file'])!=board['sha256'] or plan['status']!='confirmed' or plan['boardSha256']!=board['sha256'] or clip['planSha256']!=plan['sha256'] or payload.get('clipSha256')!=clip['sha256'] or digest_file(clip['file'])!=clip['sha256']:raise Conflict('片段、分镜或动效计划已变化')
                if operation=='request_clip_changes':
                    note=payload.get('note')
                    if not isinstance(note,str) or not note.strip() or len(note)>2000:raise ValueError('请填写修改意见')
                    clip['feedback']=note.strip();clip['status']='changes_requested'
                else:clip['status']='accepted'
            else:raise ValueError('未知分镜操作')
            state['serial']+=1;state['events'].append({'operation':operation,'shotId':payload.get('shotId'),'serial':state['serial']})
            save(self.path(task)/'state.json',state);return state

    def require_plans(self,identifier):
        task=self.store.get(identifier);state=self.load(task)
        if state is None:
            if task['input'].get('workflowMode')=='stick-narrative' or task['input'].get('creativeRoute')=='storyboard':raise ValueError('分镜叙事须先保存并确认分镜与动效计划')
            return
        for row in state['shots']:
            board=next((b for b in row['boards'] if b['version']==row['selectedBoard']),None)
            plan=next((p for p in row['plans'] if p['version']==row['selectedPlan']),None)
            if not board or board['status']!='confirmed' or digest_file(board['file'])!=board['sha256'] or not plan or plan['status']!='confirmed' or plan['boardSha256']!=board['sha256'] or signature({k:v for k,v in plan.items() if k not in ('status','sha256')})!=plan['sha256']:
                raise ValueError('当前分镜或动效计划尚未全部确认，不能启动全片制作')

    def require_clips(self,identifier):
        self.require_plans(identifier)
        task=self.store.get(identifier);state=self.load(task)
        if not state:return
        for row in state['shots']:
            clip=next((c for c in row.get('clips',[]) if c['version']==row.get('selectedClip')),None)
            plan=next((p for p in row['plans'] if p['version']==row['selectedPlan']),None)
            if not clip or clip['status']!='accepted' or clip['planSha256']!=plan['sha256'] or digest_file(clip['file'])!=clip['sha256']:raise ValueError('所有镜头须先逐一通过人工验收，才能登记成片')
