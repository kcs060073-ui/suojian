"""Local, explicit tool adapters. No package installs or shared-service changes."""
from __future__ import annotations
import array
import datetime as dt
import hashlib
import html
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid
import zipfile
from .capabilities import build_catalog

def _hash(path):
    h = hashlib.sha256()
    with open(path,'rb') as stream:
        for block in iter(lambda:stream.read(1024*1024), b''): h.update(block)
    return h.hexdigest()

def _write(path, data):
    path = Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    return str(path)

def _run(args, timeout=180, binary=False, **kwargs):
    return subprocess.run([str(x) for x in args],capture_output=True,text=not binary,
                          encoding=None if binary else 'utf-8',errors=None if binary else 'replace',timeout=timeout,**kwargs)

def _exe(context, key):
    value = context.get(key) or shutil.which(key)
    if not value: raise FileNotFoundError(f'{key} unavailable')
    return str(value)

def _probe(path, context):
    ffmpeg = _exe(context,'ffmpeg')
    probe = context.get('ffprobe') or str(Path(ffmpeg).with_name('ffprobe.exe' if os.name=='nt' else 'ffprobe'))
    if not Path(probe).is_file(): probe = shutil.which('ffprobe') or probe
    result = _run([probe,'-v','error','-show_streams','-show_format','-of','json',path],60)
    if result.returncode: raise ValueError(result.stderr[-2000:])
    return json.loads(result.stdout)

def _path(value, context):
    if not value: raise ValueError('需要输入文件路径')
    p = Path(value).expanduser()
    if not p.is_absolute(): p = Path(context.get('project') or context['root']) / p
    p = p.resolve()
    if not p.is_file(): raise FileNotFoundError(str(p))
    return p

def _result(status='completed', artifacts=None,warnings=None,**data):
    return {'status':status,'artifacts':artifacts or [],'warnings':warnings or [],**data}

def _artifact(path,kind=None):
    path=Path(path)
    return {'path':str(path),'name':path.name,'kind':kind or path.suffix.lstrip('.'),'bytes':path.stat().st_size,'sha256':_hash(path)}

def _workspace(context, action):
    root=Path(context.get('root') or Path.cwd()).resolve()
    output=Path(context.get('output') or root/'outputs').resolve()
    # Do not permit API callers to use these tools as arbitrary filesystem writers.
    if not output.is_relative_to(root): raise ValueError('output must be within this workbench root')
    path=output / (action+'-'+dt.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
    path.mkdir(parents=True,exist_ok=False)
    return path

def _research(payload,context,out):
    sources=payload.get('sources') or payload.get('urls') or []
    if isinstance(sources,str): sources=[s.strip() for s in sources.splitlines() if s.strip()]
    if payload.get('text'): sources=[{'text':payload['text'],'label':payload.get('title','用户提供资料')}]+sources
    if not sources: raise ValueError('提供 sources、urls 或 text')
    records=[]; facts=[]; warnings=[]
    for index,item in enumerate(sources[:30]):
        source=item if isinstance(item,dict) else {'url':item} if re.match(r'https?://',item) else {'path':item}
        try:
            if source.get('url'):
                url=source['url']; parsed=urllib.parse.urlsplit(url)
                if parsed.scheme not in ('http','https') or parsed.username or parsed.password: raise ValueError('仅支持无账号信息 HTTP(S) URL')
                request=urllib.request.Request(url,headers={'User-Agent':'SuoJian-Research/1.0'})
                with urllib.request.urlopen(request,timeout=25) as response:
                    raw=response.read(8*1024*1024+1)
                    if len(raw)>8*1024*1024: raise ValueError('来源超过 8 MiB，请导入本地文档')
                    mime=response.headers.get('Content-Type',''); charset=response.headers.get_content_charset() or 'utf-8'
                text=raw.decode(charset,errors='replace'); origin=url
            elif source.get('path'):
                file=_path(source['path'],context); raw=file.read_bytes(); mime=file.suffix; origin=str(file)
                if file.suffix.lower()=='.docx':
                    with zipfile.ZipFile(file) as z: text=z.read('word/document.xml').decode('utf-8')
                elif file.suffix.lower()=='.pdf':
                    cmd=shutil.which('pdftotext')
                    if not cmd: raise ValueError('PDF 需要本机 pdftotext；可先导入 TXT/DOCX')
                    p=_run([cmd,str(file),'-']); text=p.stdout
                else: text=raw.decode('utf-8-sig',errors='replace')
            else: text=str(source.get('text','')); raw=text.encode(); mime='text/plain'; origin=source.get('label','user')
            snapshot=out/f'source-{index+1}.bin'; snapshot.write_bytes(raw)
            if 'html' in mime or '<html' in text[:500].lower() or '<w:' in text[:500]:
                text=re.sub(r'<(?:script|style)\b[^>]*>.*?</(?:script|style)>','',text,flags=re.S|re.I)
                text=html.unescape(re.sub(r'<[^>]+>','\n',text))
            text=re.sub(r'[ \t]+',' ',text)
            (out/f'source-{index+1}.txt').write_text(text,encoding='utf-8')
            record={'id':f'S{index+1}','source':origin,'mime':mime,'sha256':hashlib.sha256(raw).hexdigest(),'snapshot':str(snapshot),'fetchedAt':dt.datetime.now(dt.timezone.utc).isoformat(),'verification':'unverified'}
            records.append(record)
            sentences=re.split(r'(?<=[。！？.!?])\s*|\n+',text)
            for sentence in sentences:
                sentence=sentence.strip()
                if 6<=len(sentence)<=600:
                    facts.append({'id':f'F{len(facts)+1}','claim':sentence,'quote':sentence,'sourceId':record['id'],'source':origin,'status':'needs_review','numericTerms':re.findall(r'\d+(?:\.\d+)?%?|[A-Za-z][A-Za-z0-9_-]{2,}',sentence)})
                    if len(facts)>=300: break
        except Exception as error: warnings.append(f'来源 {index+1} 获取失败：{error}')
    result={'sources':records,'facts':facts,'note':'原文候选事实已留出处与哈希；未执行外部事实验证，需审核后上屏。'}
    path=_write(out/'research-facts.json',result)
    return _result('completed' if records else 'failed',[_artifact(path)],warnings,**result)

def _storyboard(payload,context,out):
    project=Path(context.get('project') or context['root']); fps=float(payload.get('fps',30)); facts=payload.get('facts',[])
    custom_content=bool(payload.get('paragraphs') or payload.get('text'))
    paragraphs=payload.get('paragraphs')
    if not paragraphs:
        text=payload.get('text')
        if text: paragraphs=[{'label':f'段落 {i+1}','text':t.strip()} for i,t in enumerate(re.split(r'\n\s*\n',text)) if t.strip()]
        else: paragraphs=json.loads((project/'script/full-narration.json').read_text(encoding='utf-8'))
    if isinstance(paragraphs,str): paragraphs=json.loads(paragraphs)
    captions=payload.get('subtitles')
    if captions is None and not custom_content and (project/'src/full-narration-subs.json').exists(): captions=json.loads((project/'src/full-narration-subs.json').read_text(encoding='utf-8'))
    captions=captions or []; cursor=0; shots=[]
    for i,para in enumerate(paragraphs):
        if isinstance(para,str): para={'text':para}
        text=para.get('text',''); chunks=para.get('chunks') or [s for s in re.split(r'[|]',text) if s]
        matching=[s for s in captions if int(s.get('shot',0))==i+1]
        start=int(para.get('from',min((s['from'] for s in matching),default=cursor)))
        estimated_seconds=len(re.findall(r'\S+',text))/2.1 if payload.get('language')=='en' else len(text)/4.3
        end=int(para.get('to',max((s['to'] for s in matching),default=start+max(120,round(estimated_seconds*fps)))))
        if end<start: raise ValueError('分镜结束帧必须大于开始帧')
        cursor=end+1
        shots.append({'id':f'SC{i+1:02d}','label':para.get('label',f'段落 {i+1}'),'text':text,'chunks':chunks,'from':start,'to':end,
                      'chapter':para.get('chapter',1),'visual':para.get('visual','待指定：用实拍/图解证明本段动词'),'subject':para.get('subject',''),'action':para.get('action',''),
                      'recipe':para.get('recipe','current'),'camera':para.get('camera','continuous'),'holdFrames':30,'light':'single-subject',
                      'facts':[f.get('id') for f in facts if f.get('claim') and f['claim'] in text], 'status':'editable-draft'})
    report={'fps':fps,'language':payload.get('language','zh'),'shots':shots,'subtitles':captions,'facts':facts,
            'constraints':{'singleLanguage':True,'presenterPolicy':'selected-version','chapterHandoff':True,'minimumHoldFrames':30,'glitchWhitelist':[],'sweepWhitelist':[]},
            'timing':'source-frame-exact' if captions else 'estimated-needs-audio-alignment'}
    path=_write(out/'storyboard.json',report)
    md='# 可编辑分镜\n\n|镜头|帧|口播|主体 / 动作 / 运镜|\n|---|---|---|---|\n'+''.join(f"|{s['id']}|{s['from']}–{s['to']}|{s['text'].replace('|','／')}|{s['visual']} / {s['camera']}|\n" for s in shots)
    (out/'storyboard.md').write_text(md,encoding='utf-8')
    return _result(artifacts=[_artifact(path),_artifact(out/'storyboard.md')],warnings=['语义画面建议为可编辑草案；新增事实尚待核对。'],**report)

def _pcm(path,context,rate=8000,seconds=600):
    p=_run([_exe(context,'ffmpeg'),'-v','error','-i',path,'-t',str(seconds),'-vn','-ac','1','-ar',str(rate),'-f','s16le','pipe:1'],180,True)
    if p.returncode: raise ValueError(p.stderr.decode(errors='replace')[-2000:])
    values=array.array('h'); values.frombytes(p.stdout)
    if sys.byteorder!='little': values.byteswap()
    return values

def _beats(samples,rate=8000):
    hop=160; rms=[]
    for i in range(0,len(samples)-hop,hop): rms.append(math.sqrt(sum(v*v for v in samples[i:i+hop])/hop)/32768)
    flux=[max(0,rms[i]-rms[i-1]) for i in range(1,len(rms))]
    if not flux: return {'bpm':None,'beats':[],'hits':[],'rms':[],'candidates':[]}
    threshold=statistics.mean(flux)+statistics.pstdev(flux)*1.5; hits=[]
    for i in range(1,len(flux)-1):
        if flux[i]>threshold and flux[i]>=flux[i-1] and flux[i]>=flux[i+1] and (not hits or i*.02-hits[-1]['t']>.12): hits.append({'t':(i+1)*.02,'s':flux[i],'k':'broadband-onset'})
    scores=[]
    for bpm in range(60,181):
        lag=round(60/bpm/.02)
        score=sum(flux[i]*flux[i-lag] for i in range(lag,len(flux)))
        scores.append((score,bpm))
    best=max(scores)[1]; candidates=[]
    for bpm in (best/2,best,best*2):
        period=60/bpm; phase=max(hits,key=lambda x:x['s'])['t']%period if hits else 0
        grid=[phase+i*period for i in range(int(len(samples)/rate/period)+1)]
        errors=[min(abs(t-h['t']) for h in hits) for t in grid] if hits else []
        match=sum(e<=.033 for e in errors)/len(errors) if errors else 0
        candidates.append({'bpm':bpm,'t0':phase,'T':period,'match':match,'mean_abs_ms':statistics.mean(errors)*1000 if errors else None})
    winner=max(candidates,key=lambda c:c['match'])
    return {**winner,'beats':[winner['t0']+i*winner['T'] for i in range(int(len(samples)/rate/winner['T'])+1)],'hits':hits,'rms':[{'t':round(i*.02,3),'v':v} for i,v in enumerate(rms)],'candidates':candidates,'method':'stdlib-RMS-onset/autocorrelation','verified':False}

def _audio(payload,context,out):
    path=_path(payload.get('path') or payload.get('audio') or payload.get('video'),context)
    ffmpeg=_exe(context,'ffmpeg')
    p=_run([ffmpeg,'-hide_banner','-i',path,'-vn','-af','loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json','-f','null','-'],180)
    if p.returncode: raise ValueError(p.stderr[-2000:])
    blocks=re.findall(r'\{[^{}]*"input_i"[^{}]*\}',p.stderr,re.S); loud=json.loads(blocks[-1]) if blocks else {}
    samples=_pcm(path,context); beat=_beats(samples)
    advancedWarnings=[]
    python=context.get('python') or sys.executable
    check=_run([python,'-c',"import importlib.util;print(all(importlib.util.find_spec(x) for x in ['numpy','scipy']))"],30)
    if check.stdout.strip()=='True':
        try:
            advanced=_run([python,Path(__file__).parent/'adapters/beat_analysis.py',path,out/'beat-data.json',ffmpeg],90)
            if advanced.returncode==0 and (out/'beat-data.json').exists(): beat=json.loads((out/'beat-data.json').read_text(encoding='utf-8'))
            else: advancedWarnings.append('FFT/HPSS 分析失败，已回退 RMS 候选：'+advanced.stderr[-600:])
        except subprocess.TimeoutExpired: advancedWarnings.append('高级节拍分析超时，已保留实测响度与 RMS 候选；未声明通过网格验收。')
    cut_errors=[]
    for cut in payload.get('cuts',[]):
        t=float(cut)/float(payload.get('fps',30))
        delta=min((abs(t-h['t']) for h in beat['hits']),default=None)
        cut_errors.append({'frame':cut,'nearestOnsetErrorFrames':None if delta is None else delta*float(payload.get('fps',30))})
    result={'source':str(path),'sourceSha256':_hash(path),'probe':_probe(path,context),'loudness':loud,'beat':beat,'cutErrors':cut_errors}
    if payload.get('compareTo'):
        other=_path(payload['compareTo'],context); second=_pcm(other,context); length=min(len(samples),len(second))
        diff=math.sqrt(sum((samples[i]-second[i])**2 for i in range(length))/length)/32768 if length else 0
        result['encodedAudioComparison']={'other':str(other),'otherSha256':_hash(other),'rmsDifference':diff,'differentAudio':diff>.0001,'comparedSeconds':length/8000,'note':'实际解码音轨差异可证明输出音频变化；不能单凭差异断言 BGM 分离音量。'}
    artifact=_write(out/'audio-analysis.json',result)
    return _result(artifacts=[_artifact(artifact)],warnings=advancedWarnings+['节拍/鼓点为算法候选；网格门槛与变速分段需复核，不等于人工验收。','不从混合音轨推断 BGM 独立音量；需传入原 BGM 与人声分别比较。'],**result)

def _tts(payload,context,out):
    engine=payload.get('engine','existing'); project=Path(context.get('project') or context['root'])
    if engine in ('existing','external'):
        source=_path(payload.get('path') or (project/'public/audio/full-narration.wav'),context)
        target=out/source.name; shutil.copy2(source,target)
        return _result(artifacts=[_artifact(target)],engine=engine,probe=_probe(target,context),alignment='existing-preserved')
    text=payload.get('text','').strip()
    if not text: raise ValueError('生成新配音需要 text')
    # A single explicitly requested synthesis, never batch synthesis or auto downloads.
    python=context.get('tts_python') or context.get('python') or sys.executable
    runtime=Path(context['root'])/'work/runtime/python'
    tts_env=dict(os.environ)
    if runtime.is_dir(): tts_env['PYTHONPATH']=str(runtime)+(os.pathsep+tts_env['PYTHONPATH'] if tts_env.get('PYTHONPATH') else '')
    dependencies={'edge':'edge_tts','kokoro':'kokoro','kokoro_onnx':'kokoro_onnx','piper':'piper'}
    if engine not in dependencies: raise ValueError('未知配音引擎')
    check=_run([python,'-c',f"import importlib.util; print(bool(importlib.util.find_spec('{dependencies[engine]}')))"],30,env=tts_env)
    if check.stdout.strip()!='True': return _result('unavailable',warnings=[f'{engine} 未安装在所选 Python；未安装依赖或调用任何配音服务。'])
    fingerprint={key:payload.get(key) for key in ('voice','rate','language','model','voices','repo','pronounce')}
    for key in ('model','voices'):
        value=fingerprint.get(key)
        if value and Path(value).is_file(): fingerprint[key]={'path':str(Path(value).resolve()),'sha256':_hash(value)}
    key=hashlib.sha256(json.dumps({'text':text,'engine':engine,**fingerprint},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    cache=Path(context['root'])/'work/tts-cache'/key; cache.mkdir(parents=True,exist_ok=True)
    suffix='.mp3' if engine=='edge' else '.wav'; target=cache/('voice'+suffix)
    if target.exists():
        shutil.copy2(target,out/target.name)
        return _result(artifacts=[_artifact(out/target.name)],engine=engine,cacheHit=True)
    text_file=cache/'text.txt'; text_file.write_text(text,encoding='utf-8')
    if engine=='edge':
        voice=payload.get('voice') or 'zh-CN-XiaoxiaoNeural'
        args=[python,'-m','edge_tts','--voice',voice,'--rate='+payload.get('rate','+0%'),'--file',text_file,'--write-media',target,'--write-subtitles',cache/'voice.srt']
        result=_run(args,180,env=tts_env)
    elif engine=='piper':
        model=_path(payload.get('model'),context)
        script="import sys,wave;from piper import PiperVoice;p=PiperVoice.load(sys.argv[1]);w=wave.open(sys.argv[3],'wb');p.synthesize_wav(open(sys.argv[2],encoding='utf-8').read(),w);w.close()"
        result=_run([python,'-c',script,model,text_file,target],180,env=tts_env)
    else:
        if engine=='kokoro_onnx':
            model=_path(payload.get('model'),context)
            voices=_path(payload.get('voices'),context)
            script="import sys,soundfile as sf;from kokoro_onnx import Kokoro;k=Kokoro(sys.argv[1],sys.argv[2]);a,s=k.create(open(sys.argv[3],encoding='utf-8').read(),voice=sys.argv[4],speed=1,lang=sys.argv[5]);sf.write(sys.argv[6],a,s)"
            args=[python,'-c',script,model,voices,text_file,payload.get('voice','am_michael'),payload.get('language','en-us'),target]
        else:
            spoken=text
            for word,phonetic in payload.get('pronounce',{}).items(): spoken=spoken.replace(word,phonetic)
            text_file.write_text(spoken,encoding='utf-8')
            script="import sys,soundfile as sf,numpy as np;from kokoro import KPipeline;p=KPipeline(lang_code=sys.argv[1],repo_id=sys.argv[2]);a=[x[2].detach().cpu().numpy() if hasattr(x[2],'detach') else np.asarray(x[2]) for x in p(open(sys.argv[3],encoding='utf-8').read(),voice=sys.argv[4])];sf.write(sys.argv[5],np.concatenate(a),24000)"
            args=[python,'-c',script,payload.get('language','a'),payload.get('repo','hexgrad/Kokoro-82M'),text_file,payload.get('voice','am_liam'),target]
        env={**tts_env,'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'}
        result=_run(args,180,env=env)
    if result.returncode or not target.exists(): return _result('failed',warnings=[result.stderr[-2000:] or '未产生音频'])
    shutil.copy2(target,out/target.name)
    artifacts=[_artifact(out/target.name)]
    if (cache/'voice.srt').exists(): shutil.copy2(cache/'voice.srt',out/'voice.srt'); artifacts.append(_artifact(out/'voice.srt'))
    return _result(artifacts=artifacts,engine=engine,cacheHit=False,warnings=['新音频需要重新对齐字幕与分镜；既有工程音频未改变。'])

def _qc(payload,context,out):
    path=_path(payload.get('path') or payload.get('video'),context); probe=_probe(path,context); ffmpeg=_exe(context,'ffmpeg')
    decode=_run([ffmpeg,'-v','error','-i',path,'-f','null','-'],180)
    streams=probe.get('streams',[]); video=next((s for s in streams if s.get('codec_type')=='video'),{})
    peak=_run([ffmpeg,'-hide_banner','-i',path,'-vn','-af','volumedetect','-f','null','-'],180)
    volume=dict(re.findall(r'(mean_volume|max_volume):\s*([-\w.]+) dB',peak.stderr))
    duration=float(probe.get('format',{}).get('duration',0)); sample_fps=float(payload.get('sampleFps',2)); sample_fps=max(.2,min(10,sample_fps))
    frame=_run([ffmpeg,'-v','error','-i',path,'-t',str(min(duration,600)),'-vf',f'fps={sample_fps},scale=96:96,format=gray','-f','rawvideo','pipe:1'],180,True)
    pixels=frame.stdout; n=96*96; previous=None; metrics=[]; run=0; longest=0; threshold=float(payload.get('motionThreshold',.8))
    for i in range(0,len(pixels)-n+1,n):
        data=pixels[i:i+n]; mean=statistics.mean(data); difference=sum(abs(a-b) for a,b in zip(data,previous))/n if previous else None
        if difference is not None and difference<threshold: run+=1
        else: run=0
        longest=max(longest,run)
        metrics.append({'second':len(metrics)/sample_fps,'meanLuma':round(mean,3),'darkPixelRatio':sum(v<12 for v in data)/n,'frameDifference':round(difference,4) if difference is not None else None})
        previous=data
    warnings=[]
    if not video: warnings.append('没有视频流')
    if decode.returncode or decode.stderr.strip(): warnings.append('解码错误：'+decode.stderr[-1500:])
    if volume.get('max_volume') and float(volume['max_volume'])>=0: warnings.append('检测到 0 dBFS 音峰，需检查削波')
    if longest/sample_fps>float(payload.get('maxStaticSeconds',3)): warnings.append('低分辨率采样出现长静止段；可能是设计停留，需逐镜审查')
    result={'source':str(path),'probe':probe,'decodePassed':decode.returncode==0 and not decode.stderr.strip(),'volumeDb':volume,'frameMetrics':metrics,'longestStaticSampleSeconds':longest/sample_fps,'thresholds':{'sampleFps':sample_fps,'motionThreshold':threshold},'humanReview':['主角识别与尺寸','重点光效归属','字幕安全区遮挡','人物与选定版本一致','画面事实与原文一致','最终 BGM 听感与人声避让']}
    p=_write(out/'qc-report.json',result)
    sheet=out/'contact-sheet.jpg'
    images=_run([ffmpeg,'-v','error','-i',path,'-vf',f'fps=1/{max(duration/12,.1)},scale=180:-1,tile=4x3','-frames:v','1',sheet],90)
    artifacts=[_artifact(p)]
    if images.returncode==0 and sheet.exists(): artifacts.append(_artifact(sheet))
    return _result('completed' if result['decodePassed'] else 'failed',artifacts,warnings,**result)

def _capture(payload,context,out):
    if payload.get('path'):
        path=_path(payload['path'],context); target=out/path.name; shutil.copy2(path,target)
        return _result(artifacts=[_artifact(target)],source=str(path))
    url=payload.get('url','')
    if urllib.parse.urlsplit(url).scheme not in ('http','https'): raise ValueError('需要网页 url 或本地素材 path')
    script=Path(__file__).parent/'capture.cjs'
    if not script.exists(): return _result('unavailable',warnings=['尚无本地 capture.cjs 适配器；可先导入本地素材。'])
    args=[_exe(context,'node'),script,'--url',url,'--output',out/'page.png','--width',payload.get('width',1080),'--height',payload.get('height',1920)]
    if payload.get('selector'): args+=['--selector',payload['selector']]
    if payload.get('waitSelector'): args+=['--wait-selector',payload['waitSelector']]
    if payload.get('seek') is not None: args+=['--seek',payload['seek']]
    p=_run(args,90)
    if p.returncode: return _result('unavailable',warnings=['当前截图适配器未完成：'+p.stderr[-1000:],'可用“导入本地素材”加入截图/录屏。'])
    return _result(artifacts=[_artifact(out/'page.png'),_artifact(out/'page.png.json')])

def _publish(payload,context,out):
    video=_path(payload.get('video') or payload.get('path'),context)
    platforms=payload.get('platforms') or ['youtube']
    if isinstance(platforms,str): platforms=[p.strip() for p in platforms.split(',')]
    allowed={'tiktok','instagram','youtube','linkedin','facebook','x','threads','pinterest','bluesky'}
    if set(platforms)-allowed: raise ValueError('未知发布平台')
    report={'video':str(video),'sha256':_hash(video),'title':payload.get('title') or payload.get('caption',''),'description':payload.get('description',''),'platforms':platforms,'schedule':payload.get('schedule'),'timezone':payload.get('timezone','Asia/Shanghai'),'privacy':payload.get('privacy','private'),'aiGenerated':bool(payload.get('aiGenerated')),'published':False,'dryRunOnly':True}
    artifacts=[_artifact(_write(out/'publish-plan.json',report))]; warnings=['本工具仅准备与 dry run，未发布。']
    if payload.get('validateAccount'):
        if not os.getenv('UPLOAD_POST_API_KEY') or not os.getenv('UPLOAD_POST_USER'):
            return _result('unavailable',artifacts,warnings+['尚未配置发布服务账号；账号信息未写入产物。'],**report)
        script=Path(context['vendor'])/'video-shotcraft-main/publish/upload_post.py'
        args=[context.get('python') or sys.executable,script,'--video',video,'--title',report['title'],'--description',report['description'],'--platforms',','.join(platforms),'--dry-run','--json-out']
        p=_run(args,90)
        # Vendor output can include profile identifiers; retain only explicit boolean/enum keys.
        try: status=json.loads(p.stdout.strip().splitlines()[-1])
        except Exception: status={}
        report['accountValidation']={k:status[k] for k in ('authOk','missingPlatforms','status','errorType') if k in status}
        if p.returncode: warnings.append('账号 dry run 未通过；具体账号信息不落日志。')
        artifacts.append(_artifact(_write(out/'publish-validation.json',report['accountValidation'])))
    return _result(artifacts=artifacts,warnings=warnings,**report)

def _package(payload,context,out):
    project=Path(context.get('project') or context['root']).resolve(); root=Path(context['root']).resolve(); files=[]
    destination=out/'project-package.zip'
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED,compresslevel=1) as archive:
        excluded={'node_modules','.git','.venv','venv','outputs','tts-cache','__pycache__','secrets'}
        if not payload.get('includeRenders',False): excluded|={'out','renders'}
        for folder,dirs,names in os.walk(project,followlinks=False):
            dirs[:]=[name for name in dirs if name not in excluded and not (Path(folder)/name).is_relative_to(out)]
            for name in sorted(names):
                path=Path(folder)/name
                if path.is_symlink() or path.is_relative_to(out) or name.startswith('.env') or path.suffix.lower() in {'.pem','.key'}: continue
                rel=path.relative_to(project)
                archive.write(path,('work/park-fusion/' if project==root/'work/park-fusion' else 'project/')+rel.as_posix()); files.append({'path':rel.as_posix(),'bytes':path.stat().st_size,'sha256':_hash(path)})
        for directory in ('engine','workbench','tests','work/vendor','work/native'):
            for folder,dirs,names in os.walk(root/directory,followlinks=False):
                native_excluded={'exports','dist','.vite','fin_frames','projects','demosrc'} if directory=='work/native' else set()
                dirs[:]=[name for name in dirs if name not in excluded|native_excluded]
                for name in sorted(names):
                    path=Path(folder)/name
                    if path.is_symlink() or name.startswith('.env') or path.suffix.lower() in {'.pem','.key'}: continue
                    if directory=='work/native' and (name.startswith('service-') or path.suffix.lower() in {'.log','.tsbuildinfo'}): continue
                    relative=path.relative_to(root).as_posix()
                    archive.write(path,relative); files.append({'path':relative,'bytes':path.stat().st_size,'sha256':_hash(path)})
        for path in list((root/'outputs').glob('*.md'))+[root/'work/workbench-config.json',root/'work/selected-baseline.json',root/'work/vendor-receipt.json',root/'work/runtime/package.json',root/'work/runtime/package-lock.json',root/'outputs/园区Vibe-最终选定-写实设备与半身讲解员.mp4',root/'outputs/最终选定版封面.jpg',root/'outputs/打开园区Vibe.ps1']:
            if path.is_file():
                relative=path.relative_to(root).as_posix(); archive.write(path,relative)
                files.append({'path':relative,'bytes':path.stat().st_size,'sha256':_hash(path)})
        vendor=Path(context.get('vendor','nonexistent'))
        for repo in vendor.glob('*'):
            for path in repo.rglob('*'):
                if not path.is_file() or 'node_modules' in path.parts: continue
                if path.name.upper().startswith(('LICENSE','ATTRIBUTION','COPYING','NOTICE')):
                    relative='licenses/'+path.relative_to(vendor).as_posix(); archive.write(path,relative)
                    files.append({'path':relative,'bytes':path.stat().st_size,'sha256':_hash(path)})
        for folder in (root/'work/backend-verification',root/'outputs/reports',root/'work/qa'):
            for path in folder.rglob('*.json'):
                if not path.is_file() or path.stat().st_size>2*1024*1024: continue
                if any(part in ('runtime','node_modules','draft-media','Resources') for part in path.parts): continue
                relative=path.relative_to(root).as_posix(); archive.write(path,relative)
                files.append({'path':relative,'bytes':path.stat().st_size,'sha256':_hash(path)})
        runtime={'python':str(context.get('python') or sys.executable),'node':context.get('node') or shutil.which('node'),'ffmpeg':context.get('ffmpeg') or shutil.which('ffmpeg'),
                 'vendorRoot':str(vendor),'projectOriginalPath':str(project),'runtimePath':str(root/'work/runtime/python'),
                 'localPackages':[p.name for p in sorted((root/'work/runtime/python').glob('*.dist-info'))],
                 'dependencyPolicy':'源码与素材已打包；vendor源码已收录；node_modules与Python依赖/模型不打入归档。移动到其他机器需恢复这些路径或使用原锁定版本。',
                 'secretsIncluded':False,'published':False}
        archive.writestr('runtime-dependencies.json',json.dumps(runtime,ensure_ascii=False,indent=2))
        archive.writestr('workbench-config.json',json.dumps(payload.get('config',{}),ensure_ascii=False,indent=2))
        manifest={'files':files,'project':project.name,'createdAt':dt.datetime.now(dt.timezone.utc).isoformat(),'excluded':['node_modules','.git','.venv','.env','private keys','renders unless requested','giant runtime/model binaries','outputs/runs media'],'runtime':runtime}
        archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    path=_write(out/'package-manifest.json',manifest)
    return _result(artifacts=[_artifact(destination),_artifact(path)],fileCount=len(files))

def _export(payload,context,out):
    plate=_path(payload.get('clean_plate'),context)  # Never fall back to a caption-burned final video.
    probe=_probe(plate,context); stream=next(s for s in probe['streams'] if s['codec_type']=='video')
    fps=float(payload.get('fps',30)); duration=float(probe['format']['duration']); frames=round(duration*fps)
    subtitles=payload.get('subtitles',[]); shots=payload.get('shots') or [{'from':0,'to':frames-1}]
    manifest={'plate':str(plate),'fps':fps,'width':int(payload.get('width',stream['width'])),'height':int(payload.get('height',stream['height'])),'frames':frames,'subtitles':subtitles,'shots':shots,'tracks':{},'limitations':['复杂动画已烘焙底片；单语字幕与音轨原生可编辑','Windows 剪映实际打开需验证；本工具不会安装或覆盖剪映草稿']}
    resources=out/'draft-media'; resources.mkdir(parents=True)
    def copy(source):
        p=_path(source,context); target=resources/(hashlib.sha256(str(p).encode()).hexdigest()[:8]+'-'+p.name); shutil.copy2(p,target); return str(target)
    manifest['plate']=copy(plate)
    for key in ('voice','bgm','sfx','reference'):
        source=payload.get(key)
        if not source: continue
        items=source if isinstance(source,list) else [source]
        normalized=[]
        for item in items:
            entry=dict(item) if isinstance(item,dict) else {'path':item}
            entry['path']=copy(entry.get('path') or entry.get('file'))
            gain=payload.get('gains',{}).get(key,payload.get(key+'Gain',1 if key=='voice' else .25 if key=='bgm' else .65))
            entry.setdefault('volume',gain)
            if 'from' in entry:
                entry.setdefault('start',float(entry['from'])/fps)
                if 'to' in entry: entry.setdefault('duration',(float(entry['to'])+1-float(entry['from']))/fps)
            if 'v' in entry: entry['volume']=entry['v']
            if key=='bgm' and not entry.get('envelope'):
                envelope=payload.get('bgmEnvelope') or payload.get('musicEnvelope')
                if envelope: entry['envelope']=envelope
                else: entry['volumeUnverified']=True
            if isinstance(entry.get('envelope'),dict):
                envelope=entry['envelope']
                if envelope.get('type')=='frame-values':
                    values=envelope.get('values',[])
                    start_frame=round(float(entry.get('start',0))*fps)
                    entry['envelope']=[{'frame':start_frame+i,'volume':float(entry['volume'])*float(v)} for i,v in enumerate(values)]
                    normalized.append(entry)
                    continue
                if envelope.get('type')!='min-linear-fades': raise ValueError('未知音乐包络类型，拒绝无提示丢失混音')
                start_frame=round(float(entry.get('start',0))*fps)
                count=round(float(entry.get('duration',(frames-start_frame)/fps))*fps)
                fade_in=float(envelope.get('fadeInFrames',0)); fade_out=float(envelope.get('fadeOutFrames',0))
                base=float(entry['volume'])
                entry['envelope']=[{'frame':start_frame+f,'volume':base*min(min(1,f/fade_in) if fade_in else 1,min(1,(count-f)/fade_out) if fade_out else 1)} for f in range(count)]
            normalized.append(entry)
        manifest['tracks'][key]=normalized
    config=out/'draft-manifest.json'; _write(config,manifest)
    python=context.get('python') or sys.executable
    check=_run([python,'-c',"import importlib.util;print(bool(importlib.util.find_spec('pyJianYingDraft')))"],30)
    if check.stdout.strip()!='True': return _result('unavailable',[_artifact(config)],['所选 Python 未安装 pyJianYingDraft；已生成可审计分轨清单，尚未生成真实剪映草稿。','未安装依赖或接触任何已有剪映编辑。'])
    adapter=Path(__file__).parent/'adapters/jianying.py'
    p=_run([python,adapter,config,out/'jianying-draft'],180)
    if p.returncode: return _result('failed',[_artifact(config)],['剪映适配生成失败：'+p.stderr[-2000:]])
    draft=out/'jianying-draft/draft_content.json'
    if not draft.exists(): return _result('failed',warnings=['适配器未产生 draft_content.json'])
    archive=out/'jianying-draft.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=1) as z:
        for f in (out/'jianying-draft').rglob('*'):
            if f.is_file(): z.write(f,'jianying-draft/'+f.relative_to(out/'jianying-draft').as_posix())
    warnings=list(manifest['limitations'])
    if manifest['tracks'].get('reference'): warnings.append('当前默认使用选定原片完整混音；独立人声/BGM/SFX轨为静音备选。修改独立轨时先关闭原片混音轨，避免双重叠加；分轨重建不声称与历史混合音轨逐样本相同。')
    if any(e.get('volumeUnverified') for e in manifest['tracks'].get('bgm',[])): warnings.append('没有提供音乐包络，仅应用显式增益；未声明与原混音一致。')
    return _result(artifacts=[_artifact(draft),_artifact(archive),_artifact(config)],warnings=warnings,nativeTextCount=len(subtitles),nativeAudioTracks=list(manifest['tracks']))

def _vendor(payload,context,out):
    vendor=Path(context['vendor']); project=Path(context.get('project') or context['root']); python=str(context.get('python') or sys.executable)
    commands=[{'name':'Shotcraft 多轨工作台（按需启动独立端口）','argv':[_exe(context,'node'),str(vendor/'video-shotcraft-main/workbench/scripts/open.mjs'),str(project)],'note':'命令仅供显式执行；此工具不启动服务。'},
              {'name':'Vibe Canvas 静帧/转场/片段/全片','argv':[python,str(vendor/'vibe-knowledge-film-main/engine/render.py'),'<Canvas工程>','--help']},
              {'name':'Vibe 事件音合成','argv':[python,str(vendor/'vibe-knowledge-film-main/engine/audio.py'),'<Canvas工程>']},
              {'name':'讲解片静态检查','argv':[python,str(vendor/'anything2explainer-main/template/scripts/selfcheck.py')]},
              {'name':'讲解片画面指标','argv':[python,str(vendor/'anything2explainer-main/template/scripts/frame_metrics.py')]},
              {'name':'讲解片运动检查','argv':[python,str(vendor/'anything2explainer-main/template/scripts/motion_check.py'),'--frames','<抽帧目录>']}]
    data={'commands':commands,'catalog':build_catalog(vendor),'dependencies':{'python':python,'node':_exe(context,'node'),'ffmpeg':_exe(context,'ffmpeg')},'startedServices':False}
    return _result(artifacts=[_artifact(_write(out/'vendor-tools.json',data))],**data)

HANDLERS={'research':_research,'storyboard':_storyboard,'audio_analyze':_audio,'tts':_tts,'qc':_qc,'capture':_capture,'publish_prepare':_publish,'package':_package,'export_draft':_export,'vendor_workbench':_vendor}

def run_tool(action,payload=None,context=None):
    payload=payload or {}; context=context or {}; out=None
    try:
        if action=='catalog': return _result(**build_catalog(context['vendor']))
        if action not in HANDLERS: return _result('unavailable',warnings=[f'未知工具：{action}'])
        out=_workspace(context,action)
        result=HANDLERS[action](payload,context,out); result['output']=str(out)
        return result
    except (FileNotFoundError,ImportError) as error: return _result('unavailable',warnings=[str(error)],output=str(out) if out else None)
    except Exception as error: return _result('failed',warnings=[str(error)],output=str(out) if out else None)
