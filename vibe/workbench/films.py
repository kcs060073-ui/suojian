"""Discover completed deliveries and register future workbench renders.

The completed-cut library is distinct from the editable working configuration.
It never pretends that a finished MP4 is a newly reconstructed editable timeline.
"""
from __future__ import annotations
import hashlib
import copy
import json
from pathlib import Path
import threading
import time
import uuid
from urllib.parse import quote

LOCK=threading.RLock()

def config_signature(config):
    value={k:v for k,v in config.items() if k!='editFilmId'}
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def latest_completion(root):
    return read_json(Path(root)/'work/films/latest-completion.json')

def can_auto_open(root,identifier,current):
    root=Path(root).resolve();registry=read_json(root/'work/films/registry.json',{})
    if not isinstance(registry,dict):return False
    for relative,meta in registry.items():
        video=(root/relative).resolve()
        if not video.is_relative_to(root/'outputs') or not isinstance(meta,dict):continue
        if film_id(root,video)==identifier and meta.get('sourceConfigHash')==config_signature(current):return True
    return False

def film_id(root,video):
    return 'film-'+hashlib.sha256(Path(video).resolve().relative_to(Path(root).resolve()).as_posix().encode()).hexdigest()[:16]

def editor_path(root,identifier):
    import re
    if not re.fullmatch(r'film-[0-9a-f]{16}',str(identifier)):raise ValueError('影片编号无效')
    return Path(root)/'work/films/editors'/f'{identifier}.json'

def save_editor(root,config):
    identifier=config.get('editFilmId')
    if not identifier:return
    path=editor_path(root,identifier)
    with LOCK:
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(path)

def load_editor(root,identifier):
    root=Path(root).resolve()
    film=next((f for f in catalog(root) if f['id']==identifier),None)
    if not film:raise ValueError('影片不存在')
    saved=read_json(editor_path(root,identifier))
    if isinstance(saved,dict) and saved.get('shots'):return copy.deepcopy(saved)
    if '嘉定项目-这栋楼只有一个业主' in film['video']['name']:
        data=read_json(root/'work/park-fusion/src/script07-data.json')
        if not isinstance(data,dict):raise ValueError('嘉定影片编辑源文件缺失')
        shots=[]
        for s in data['scenes']:
            shots.append({'id':f'jiading-{s["index"]+1}','from':s['from'],'duration':s['duration'],'label':s['label'],
                'title':'\n'.join(s['title']),'recipe':f'jiading-{s["index"]}','source':'/media/'+s['footage'],'sourceType':'video',
                'sourceOffset':s['from'],'footageOffset':0,'speed':1,'enabled':True,'asset':'嘉定项目实拍 · '+s['label']})
        return {'version':1,'editFilmId':identifier,'scenePack':'jiading-script07','name':'嘉定项目 · 这栋楼，只有一个业主',
            'format':'portrait','style':'fusion','background':'footage','language':'zh','showSelectedVideo':False,
            'audioProfile':'jiading-script07','narrationMode':'existing','narrationFile':'/media/'+data['voice'],
            'musicGain':1,'voiceGain':1,'sfxEnabled':True,'camera':'static','typography':'minimal','seed':17,
            'presenter':{'enabled':True,'placement':'side','fixedHead':False,'fixedMouth':False,'grounded':False},
            'shots':shots,'subtitles':copy.deepcopy(data['subtitles']),'subtitleMode':'single','subtitlesFollowShots':True,
            'subtitleStyle':{'fontSize':44,'color':'#f3f1e9','background':True},'chapters':[{'name':s['label'],'from':s['from']} for s in data['scenes']],
            'scriptDraft':{'title':film['title'],'narration':'\n'.join(s['narration'] for s in data['scenes'])},
            'assets':[{'url':'/media/'+s['footage'],'name':'嘉定实拍 '+s['label']} for s in data['scenes']],
            'simulation':{'enabled':False,'equipmentHeight':3.6,'entranceHeight':3.2}}
    if '最终选定' in film['video']['name']:
        base=read_json(root/'engine/default-config.json')
        if base:
            return {**base,'editFilmId':identifier,'name':'项目 · 写实设备与半身讲解员','showSelectedVideo':False}
    raise ValueError('此成片尚无可编辑工程，请下载制作工程')

def read_json(path,default=None):
    try:return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):return default

def artifact(root,path,label=None):
    root=Path(root).resolve();path=Path(path).resolve()
    if not path.is_relative_to(root/'outputs') or not path.is_file():return None
    return {'path':str(path),'url':'/outputs/'+quote(path.relative_to(root/'outputs').as_posix(),safe='/'),
            'name':path.name,'label':label or path.name,'bytes':path.stat().st_size}

def register_film(root,video,config=None):
    root=Path(root).resolve();video=Path(video).resolve()
    if video.suffix.lower()!='.mp4' or not artifact(root,video):raise ValueError('只登记本任务已完成的MP4')
    config=config or {};path=root/'work/films/registry.json'
    with LOCK:
        registry=read_json(path,{})
        if not isinstance(registry,dict):registry={}
        key=video.relative_to(root).as_posix()
        registry[key]={'title':config.get('name') or video.stem,
            'durationSeconds':sum(s.get('duration',0) for s in config.get('shots',[]) if s.get('enabled') is not False)/30,
            'width':1920 if config.get('format')=='landscape' else 1080,
            'height':1080 if config.get('format')=='landscape' else 1920,
            'sourceConfigHash':config_signature(config)}
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(registry,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(path)
        identifier=film_id(root,video)
        if config.get('shots'):
            save_editor(root,{**config,'editFilmId':identifier})
            event=path.with_name('latest-completion.json');temporary=event.with_suffix('.tmp')
            temporary.write_text(json.dumps({'id':uuid.uuid4().hex,'filmId':identifier,'created':time.time()},ensure_ascii=False),encoding='utf-8');temporary.replace(event)
        return identifier

def catalog(root):
    root=Path(root).resolve();out=root/'outputs'
    with LOCK:registry=read_json(root/'work/films/registry.json',{})
    if not isinstance(registry,dict):registry={}
    videos={p.resolve() for p in out.glob('*.mp4') if artifact(root,p)}
    for relative in registry:
        path=(root/relative).resolve()
        if path.is_relative_to(out) and path.is_file() and path.suffix.lower()=='.mp4':videos.add(path)
    records=[]
    for video in videos:
        relative=video.relative_to(root).as_posix();meta=registry.get(relative,{})
        receipt=video.with_name(video.stem+'-制作回执.json');report=read_json(receipt,{})
        if not isinstance(report,dict):report={}
        metadata={**meta,**{k:v for k,v in report.items() if k in ('title','durationSeconds','fps','frames','resolution','mode','sourceScript','documentPolicy')}}
        poster=video.with_name(video.stem+'-封面.jpg')
        if not poster.exists() and '最终选定' in video.stem:poster=out/'最终选定版封面.jpg'
        assets=[artifact(root,video,'下载 MP4')]
        for suffix,label in [('-制作工程.zip','制作工程'),('.srt','单语字幕'),('-制作回执.json','制作回执'),('-说明.md','制作说明')]:
            a=artifact(root,video.with_name(video.stem+suffix),label)
            if a:assets.append(a)
        identifier=film_id(root,video)
        editable=editor_path(root,identifier).exists() or ('嘉定项目-这栋楼只有一个业主' in video.name and (root/'work/park-fusion/src/script07-data.json').exists()) or ('最终选定' in video.name and (root/'engine/default-config.json').exists())
        records.append({'id':identifier,'title':metadata.get('title') or video.stem,'created':video.stat().st_mtime,
            'durationSeconds':metadata.get('durationSeconds'),'fps':metadata.get('fps',30),
            'resolution':metadata.get('resolution') or ([meta['width'],meta['height']] if meta.get('width') else None),
            'mode':metadata.get('mode','已完成影片'),'video':assets[0],'poster':artifact(root,poster),
            'artifacts':assets,'sourceScript':metadata.get('sourceScript'),'documentPolicy':metadata.get('documentPolicy'),
            'state':'completed','editingMode':'source-project' if editable else 'finished-cut','editorAvailable':editable})
    return sorted(records,key=lambda r:r['created'],reverse=True)

def as_jobs(root):
    return [{'id':f['id'],'action':'render','title':f['title'],'status':'completed','created':f['created'],'finished':f['created'],
        'phase':'已入库成片 · '+f['title'],'filmId':f['id'],
        'result':{'status':'completed','artifacts':f['artifacts'],'warnings':[],'durationSeconds':f['durationSeconds']}}
        for f in catalog(root)]
