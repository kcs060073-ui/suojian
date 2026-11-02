"""Same-presenter assembly, independent of synthesized-voice/B-roll generation."""
import functools
import json
import re
import threading
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse
from flask import jsonify,render_template,request,send_file

from suo_jian.presenter.library import scan,save
from suo_jian.presenter.matching import match

LOCK=threading.Lock()

def read(path,default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default

def register_presenter_routes(app,workspace,reference_for):
    def context(job):
        if re.fullmatch(r'p-[0-9a-f]{12}', job):
            work=Path(workspace)/'work'/'presenter-workspaces'/job
            if not work.is_dir():raise FileNotFoundError('找不到该口播任务')
            return {},None,work
        reference,video,result=reference_for(job)
        work=result/'presenter';work.mkdir(exist_ok=True)
        return reference,video,work
    def guarded(fn):
        @functools.wraps(fn)
        def wrapper(*args,**kwargs):
            try:
                if request.host.split(':')[0] not in {'127.0.0.1','localhost'}:raise PermissionError('仅限本机工作台')
                if request.method=='POST':
                    origin=request.headers.get('Origin')
                    if (origin and urlparse(origin).netloc!=request.host) or request.headers.get('Sec-Fetch-Site')=='cross-site' or not request.is_json:
                        raise PermissionError('请从本机溯剪页面操作')
                return fn(*args,**kwargs)
            except PermissionError as e:return jsonify(error=str(e)),403
            except (ValueError,FileNotFoundError,KeyError,FileExistsError) as e:return jsonify(error=str(e)),400
            except Exception as e:return jsonify(error=str(e)),500
        return wrapper
    def public_library(library):
        if not library:return None
        groups=[{k:v for k,v in g.items() if k!='anchor'} for g in library['groups']]
        assets=[{k:v for k,v in a.items() if k not in {'faces','path'}} for a in library['assets']]
        return {**library,'groups':groups,'assets':assets}
    def start(work,action):
        if not LOCK.acquire(blocking=False):raise ValueError('另一个人物分析或出片任务正在运行，请稍候')
        def phase(text,**extra):save(work/'state.json',{'status':'running','phase':text,**extra})
        phase('正在准备')
        def worker():
            try:
                action(phase)
                save(work/'state.json',{'status':'completed','phase':'处理完成'})
            except Exception as e:save(work/'state.json',{'status':'failed','phase':'需要处理','error':str(e)[:1500]})
            finally:LOCK.release()
        threading.Thread(target=worker,daemon=True).start()

    @app.get('/presenter')
    @guarded
    def presenter_page():
        return render_template('presenter.html')

    @app.post('/api/presenter/new')
    @guarded
    def presenter_new():
        job='p-'+uuid.uuid4().hex[:12]
        (Path(workspace)/'work'/'presenter-workspaces'/job).mkdir(parents=True,exist_ok=False)
        return jsonify(job=job),201

    @app.get('/api/presenter/<job>/state')
    @guarded
    def presenter_state(job):
        _,_,work=context(job)
        library=read(work/'library.json')
        reference=read(work/'reference.json',{})
        draft=None;last=read(work/'last-render.json')
        if last:draft=read(work/last['id']/'draft.json')
        state=read(work/'state.json',{'status':'idle','phase':'选择素材文件夹，按原片脚本定位口播'})
        if state.get('status')=='running' and not LOCK.locked():state={**state,'status':'interrupted','phase':'上次任务已中断，可以使用缓存重新开始'}
        return jsonify(state=state,library=public_library(library),reference=reference,plan=read(work/'plan.json'),render=last,draft=draft,speech_errors=read(work/'speech-errors.json',[]))

    @app.post('/api/presenter/<job>/scan')
    @guarded
    def presenter_scan(job):
        _,video,work=context(job);data=request.get_json();folder=data.get('folder','')
        if not isinstance(folder,str) or not folder.strip():raise ValueError('请输入本机文件夹完整路径')
        def action(phase):
            from suo_jian.presenter.timing import TimedASR
            library=scan(folder,work,phase)
            phase('正在载入本机语音时间定位模型')
            model=TimedASR()
            if video:
                reference=model.transcribe(video,work/'asr')
                save(work/'reference.json',reference)
            errors=[]
            for i,asset in enumerate(library['assets']):
                phase('正在定位素材口播的逐词时间',completed=i,total=len(library['assets']))
                try:
                    if not asset['has_audio']:raise ValueError('没有口播音轨')
                    model.transcribe(asset['path'],work/'asr')
                except Exception as e:errors.append({'id':asset['id'],'name':asset['name'],'error':str(e)[:300]})
            save(work/'speech-errors.json',errors)
            # Changing a library invalidates all previous selections and previews.
            for name in ('plan.json','last-render.json'):(work/name).unlink(missing_ok=True)
        start(work,action);return jsonify(started=True),202

    @app.get('/api/presenter/<job>/sentences')
    @guarded
    def presenter_sentences(job):
        from suo_jian.presenter.sentences import split_sentences
        _,_,work=context(job);library=read(work/'library.json')
        if not library:raise ValueError('请先扫描素材文件夹')
        rows=[]
        for asset in library['assets']:
            transcript=read(work/'asr'/(asset['id']+'-timed-v1.json'))
            if transcript:
                rows.append({'asset_id':asset['id'],'name':asset['name'],'person_group':asset.get('person_group'),
                             'sentences':split_sentences(transcript)})
        return jsonify(assets=rows)

    @app.post('/api/presenter/<job>/sentence-plan')
    @guarded
    def presenter_sentence_plan(job):
        from suo_jian.presenter.assembly import reviewed_plan
        from suo_jian.presenter.continuity import merge_uninterrupted
        from suo_jian.presenter.sentences import caption_ranges,split_sentences
        _,_,work=context(job);data=request.get_json();library=read(work/'library.json')
        if LOCK.locked():raise ValueError('请等待当前任务结束')
        if not library:raise ValueError('请先扫描素材文件夹')
        group=data.get('person_group');selections=data.get('selections')
        if not any(g['id']==group for g in library['groups']):raise ValueError('请先选择人物')
        if not isinstance(selections,list) or not selections:raise ValueError('请选择至少一句口播')
        assets={a['id']:a for a in library['assets']};requests=[];transcripts={}
        for selection in selections:
            asset_id=selection['asset_id'];asset=assets[asset_id]
            if asset.get('person_group')!=group:raise ValueError('请选择同一人物的口播')
            transcript=transcripts.setdefault(asset_id,read(work/'asr'/(asset_id+'-timed-v1.json')))
            first,last=selection['first'],selection['last']
            if not any(s['first']==first and s['last']==last for s in split_sentences(transcript)):
                raise ValueError('句子已变化，请刷新并重新选择')
            requests.append({'asset_id':asset_id,'first':first,'last':last,
                             'captions':caption_ranges(transcript['words'],first,last)})
        plan=reviewed_plan(library,work,group,requests,'')
        plan['selected']=merge_uninterrupted(plan['selected'],transcripts)
        plan.update(adapted=False,selection_mode='sentences',adaptation_policy='选择实际口播句子，保留原声',
                    warnings=['语音识别和断句可能有误，请试听并核对切点、人物与场景。'])
        history=work/'selection-history'
        for name in ('plan.json','last-render.json'):
            previous=read(work/name)
            if previous is not None:save(history/(uuid.uuid4().hex+'-'+name),previous)
        save(work/'plan.json',plan);(work/'last-render.json').unlink(missing_ok=True)
        return jsonify(plan=plan)

    @app.get('/api/presenter/<job>/source/<asset_id>')
    @guarded
    def presenter_source(job,asset_id):
        _,_,work=context(job)
        library=read(work/'library.json') or {}
        asset=next((a for a in library.get('assets',[]) if a['id']==asset_id),None)
        if not asset:raise FileNotFoundError('找不到素材')
        return send_file(asset['path'],conditional=True)

    @app.post('/api/presenter/<job>/match')
    @guarded
    def presenter_match(job):
        _,_,work=context(job);data=request.get_json()
        if LOCK.locked():raise ValueError('请等待当前任务结束')
        script=data.get('script','')
        if not isinstance(script,str) or not 1<=len(script)<=5000:raise ValueError('文案应为 1–5000 字')
        library=read(work/'library.json')
        if not library:raise ValueError('请先分析素材文件夹')
        plan=match(script,library,work,data.get('person_group'))
        save(work/'plan.json',plan);(work/'last-render.json').unlink(missing_ok=True)
        return jsonify(plan=plan)

    @app.post('/api/presenter/<job>/render')
    @guarded
    def presenter_render(job):
        reference,_,work=context(job);data=request.get_json();plan=read(work/'plan.json')
        if not plan:raise ValueError('请先按脚本匹配')
        if plan['missing'] and not data.get('partial'):raise ValueError('存在缺句，请补充素材或明确选择已匹配片段小样')
        def action(phase):
            from suo_jian.presenter.rendering import render
            render(plan,read(work/'library.json'),reference,work,partial=bool(data.get('partial')),phase=phase)
        start(work,action);return jsonify(started=True),202

    @app.post('/api/presenter/<job>/draft')
    @guarded
    def presenter_draft(job):
        _,_,work=context(job);manifest=read(work/'last-render.json')
        if not manifest:raise ValueError('请先生成并审看预览')
        def action(phase):
            from suo_jian.presenter.rendering import export_draft
            from suo_jian.presenter.delivery import install_draft
            record=work/manifest['id']/'draft.json'
            exported=read(record)
            if not exported:
                phase('正在保存独立视频片段、原声和可编辑字幕')
                exported=export_draft(manifest,work)
            phase('正在保存到剪映本机草稿列表')
            root=Path(os.environ.get('LOCALAPPDATA',''))/'JianyingPro/User Data/Projects/com.lveditor.draft'
            installed=install_draft(exported,root)
            save(record,installed)
        start(work,action);return jsonify(started=True),202

    @app.get('/api/presenter/<job>/thumb/<asset_id>')
    @guarded
    def presenter_thumb(job,asset_id):
        _,_,work=context(job)
        if not re.fullmatch('[0-9a-f]{20}',asset_id):raise ValueError('素材编号无效')
        return send_file(work/(asset_id+'.jpg'),mimetype='image/jpeg')

    @app.get('/api/presenter/<job>/media/<render_id>/<kind>')
    @guarded
    def presenter_media(job,render_id,kind):
        _,_,work=context(job)
        if not re.fullmatch(r'render-[0-9a-f]{10}',render_id):raise ValueError('预览编号无效')
        names={'preview':'preview.mp4','draft':'jianying-draft.zip','manifest':'manifest.json'}
        if kind not in names:raise ValueError('文件类型无效')
        return send_file(work/render_id/names[kind],conditional=True,as_attachment=kind=='draft')
