"""Read-only vendor catalog and isolated native tool adapters. No installation or service start."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import datetime
from urllib.parse import quote, urlsplit

# 外部依赖路径统一来自 workbench/paths.py（可用 SUOJIAN_LEGACY_MODULES 覆盖）
from workbench.paths import LEGACY_MODULES
LOCAL_MODULES = Path(__file__).resolve().parents[1]/'work/runtime/node_modules'
SHARED = LOCAL_MODULES if all((LOCAL_MODULES/p/'package.json').exists() for p in ('remotion','@remotion/bundler','@remotion/renderer','vite','@remotion/motion-blur')) else LEGACY_MODULES
BROWSER = SHARED/'.remotion/chrome-headless-shell/win64/chrome-headless-shell-win64/chrome-headless-shell.exe'
REPOS = ('video-shotcraft-main', 'vibe-knowledge-film-main', 'anything2explainer-main')

def uri(path: str | Path) -> str:
    return '/vendor/' + quote(str(path).replace('\\', '/'), safe='/')

def resolve_file(vendor, value):
    """Resolve vendor-relative request, reject absolute paths and path traversal."""
    root = Path(vendor).resolve()
    requested = Path(value)
    if requested.is_absolute() or ':' in str(value):
        raise ValueError('只允许素材库相对路径')
    result = (root / requested).resolve()
    if root not in result.parents or not result.is_file():
        raise ValueError('素材不存在或超出素材库范围')
    return result

def _packages(base):
    result = {}
    for p in ('react', 'react-dom', 'remotion', '@remotion/renderer', '@remotion/cli',
              '@remotion/media', '@remotion/player', 'vite', '@vitejs/plugin-react',
              'zustand', '@react-three/fiber', '@remotion/three', 'three',
              '@remotion/motion-blur', '@remotion/google-fonts', '@remotion/zod-types', 'zod'):
        f = Path(base) / p / 'package.json'
        result[p] = json.loads(f.read_text(encoding='utf-8')).get('version') if f.exists() else None
    return result

def _imports(files):
    imports = set()
    for f in files:
        text = f.read_text(encoding='utf-8', errors='replace')
        for match in re.findall(r'^\s*(?:import|export)\s+(?:[\w*{},\s]+?\s+from\s+)?[\'\"]([^\'\"]+)', text, re.M):
            if not match.startswith(('.', '/', '@demos', '@proj')):
                imports.add('/'.join(match.split('/')[:2]) if match.startswith('@') else match.split('/')[0])
    return sorted(imports)

def discover(vendor) -> dict:
    vendor = Path(vendor).resolve()
    repo = vendor / REPOS[0]
    library = json.loads((repo / 'gallery/api/library.json').read_text(encoding='utf-8-sig'))
    packages = _packages(SHARED)
    recipes = []
    for card in library['cards']:
        reference = repo / card['source']
        card_text = reference.read_text(encoding='utf-8') if reference.exists() else ''
        match = re.search(r'(demos/[A-Za-z0-9_./-]+)', card_text.split('参考实现')[-1])
        demo_dir = repo / match.group(1).rstrip('/') if match else repo / 'demos' / card.get('category', '') / card['name']
        demo_files = sorted(demo_dir.rglob('*.tsx')) if demo_dir.is_dir() else ([demo_dir] if demo_dir.is_file() else [])
        dependencies = _imports(demo_files)
        missing = [name for name in dependencies if not (SHARED / name / 'package.json').exists()]
        demos = [uri(f.relative_to(vendor)) for f in demo_files]
        styles = []
        for style in card.get('styles', []):
            media = style.get('media', {})
            media_path = repo / 'gallery' / urlsplit(media.get('url', '')).path.lstrip('./')
            poster = repo / 'gallery/media/poster' / (style['key']+'.jpg')
            styles.append({'id': style['key'], 'name': style.get('label', style['key']),
                           'description': style.get('description', ''),
                           'preview': uri(media_path.relative_to(vendor)) if media_path.is_file() else None,
                           'remotePreview': 'https://vincentwei1021.github.io/video-shotcraft/media/'+quote(style['key'])+'.mp4',
                           'poster':uri(poster.relative_to(vendor)) if poster.is_file() else None,
                           'previewStatus':'local' if media_path.is_file() else 'local_missing_remote_unverified',
                           'available': media_path.is_file(), 'mediaType': media.get('type', 'mp4')})
        recipes.append({'id': card['name'], 'name': card['name'], 'category': card.get('category', 'other'),
                        'description': card.get('summary', ''), 'use': card.get('use', ''),
                        'duration': card.get('duration', ''), 'energy': card.get('energy', ''),
                        'intention': card.get('intention', ''), 'tags': card.get('tags', []),
                        'reference': uri(reference.relative_to(vendor)), 'demo': demos,
                        'preview': next((s['preview'] for s in styles if s['preview']), None),
                        'styles': styles, 'sourceAvailable': bool(demo_files),
                        'poster':next((s['poster'] for s in styles if s['poster']),None),
                        'available': bool(demo_files) and not missing, 'missingDependencies': missing,
                        'renderStatus': 'not_rendered',
                        'applyMode': 'native-source', 'sourceText': card_text,
                        'note': '应用会保留准确demo源码；额外依赖缺失的镜头需在原生工作台补齐后渲染。'})
    audio_root = repo / 'assets/audio'
    attribution = (audio_root / 'ATTRIBUTION.md').read_text(encoding='utf-8')
    legacy_sfx = audio_root / 'ATTRIBUTION-legacy-sfx.md'
    legacy_attribution = legacy_sfx.read_text(encoding='utf-8') if legacy_sfx.is_file() else None
    audio = []
    for f in sorted(audio_root.rglob('*')):
        if f.suffix.lower() not in ('.mp3', '.wav', '.ogg', '.m4a', '.flac'): continue
        rel = f.relative_to(audio_root)
        source = legacy_sfx if rel.parts[0] == 'sfx' and legacy_attribution is not None else audio_root / 'ATTRIBUTION.md'
        text = legacy_attribution if source == legacy_sfx else attribution
        lines = [line for line in text.splitlines() if f.name in line]
        evidence = '\n'.join(lines)
        uncertain = not lines or any(x in evidence for x in ('无法反查', '待考', '须确认'))
        audio.append({'id': rel.as_posix(), 'name': f.stem, 'kind': 'bgm' if rel.parts[0] == 'bgm' else 'sfx',
                      'category': rel.parts[1] if rel.parts[0] == 'sfx' and len(rel.parts)>2 else 'bgm',
                      'preview': uri(f.relative_to(vendor)), 'bytes': f.stat().st_size,
                      'available': True, 'licenseStatus': 'needs_review' if uncertain else 'recorded',
                      'attribution': evidence, 'source': uri(source.relative_to(vendor))})
    licenses = []
    for repo_name in REPOS:
        for f in sorted((vendor/repo_name).rglob('*')):
            if f.is_file() and (f.name.upper().startswith(('LICENSE', 'OFL', 'COPYING')) or f.name == 'ATTRIBUTION.md'):
                licenses.append({'repo': repo_name, 'name': f.name, 'path': str(f.relative_to(vendor)),
                                 'url': uri(f.relative_to(vendor)), 'text': f.read_text(encoding='utf-8', errors='replace')})
    refs = []
    for repo_name in REPOS:
        for folder in ('references', 'reference'):
            for f in sorted((vendor/repo_name/folder).rglob('*.md')):
                if '/shots/' not in f.as_posix():
                    refs.append({'source':repo_name,'name':f.stem,'url':uri(f.relative_to(vendor))})
    return {'recipes': recipes, 'audio': audio, 'licenses': licenses, 'references': refs,
            'categories': library.get('categories', {}), 'packages': packages,
            'stats': {'cards':len(recipes),'styles':sum(len(r['styles']) for r in recipes),
                      'previews':sum(s['available'] for r in recipes for s in r['styles']),
                      'bgm':sum(a['kind']=='bgm' for a in audio),'sfx':sum(a['kind']=='sfx' for a in audio)},
            'policy': {'bilingualSubtitles':False,'presenterFollowsSelectedVersion':True,'fixedPresenterHead':False,'fixedMouth':False,'fixedNose':False,
                       'nativeAnimationInJianying':False}}

def _copy(source, target):
    if target.exists(): return
    shutil.copytree(source, target, ignore=shutil.ignore_patterns('node_modules','.git','exports','.vite','dist'), symlinks=False)

def _junction(target, source):
    if target.exists():
        if target.resolve()==source.resolve(): return
        if os.name!='nt' or target.name!='node_modules': return
        # Only replace a verified junction inside our isolated native copies, never a real directory.
        expected_native=Path(__file__).resolve().parents[1]/'work/native'
        if expected_native not in target.absolute().parents: raise ValueError('不能重接工作区以外的依赖')
        q = lambda p: "'" + str(p).replace("'", "''") + "'"
        command='$e=Get-Item -LiteralPath '+q(target)+' -Force; if($e.LinkType -ne \'Junction\'){throw \'拒绝替换真实目录\'}; Remove-Item -LiteralPath '+q(target)+' -Force'
        shell=shutil.which('pwsh') or 'powershell'
        if shell=='powershell': command=command.replace('Remove-Item -LiteralPath '+q(target)+' -Force','[System.IO.Directory]::Delete('+q(target)+')')
        subprocess.run([shell,'-NoProfile','-NonInteractive','-Command',command],check=True,capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        # Literal single-quoted PowerShell arguments; no cmd or shell command composition.
        q = lambda p: "'" + str(p).replace("'", "''") + "'"
        subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',
                        'New-Item -ItemType Junction -Path '+q(target)+' -Target '+q(source)+' | Out-Null'], check=True,
                       capture_output=True, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    else: target.symlink_to(source, target_is_directory=True)

def native_tools(vendor, root) -> dict:
    """Prepare copies lazily; return commands, never execute them or install packages."""
    vendor, root = Path(vendor).resolve(), Path(root).resolve()
    native = root / 'work/native'
    native.mkdir(parents=True, exist_ok=True)
    canvas = native/'canvas'
    _copy(vendor/REPOS[1], canvas)
    for index in list((canvas/'examples').glob('*/index.html')):
        _junction(index.parent/'engine',canvas/'engine')
    explainer = native/'explainer'
    _copy(vendor/REPOS[2]/'template', explainer)
    if SHARED.exists(): _junction(explainer/'node_modules', SHARED)
    version_file = vendor/REPOS[0]/'.suojian-upstream.json'
    version = json.loads(version_file.read_text(encoding='utf-8')).get('commit', '') if version_file.is_file() else ''
    shot = native/('shotcraft-'+version[:12] if re.fullmatch(r'[0-9a-f]{40}', version) else 'shotcraft')
    _copy(vendor/REPOS[0], shot)
    wb = shot/'workbench'
    if SHARED.exists(): _junction(wb/'node_modules', SHARED)
    # GitHub ZIP represents this upstream symlink as a small text file.
    demo_link=wb/'demosrc'
    if demo_link.is_file() and demo_link.read_text(encoding='utf-8').strip()=='../demos': demo_link.unlink()
    _junction(wb/'demosrc', shot/'demos')
    _junction(shot/'template/node_modules', SHARED)
    # Upstream generator assumes POSIX path separators and directory symlink privileges.
    # Adapt only the isolated copy for Windows junctions and webpack import paths.
    generator=wb/'scripts/gen-index.mjs'
    generated=generator.read_text(encoding='utf-8')
    generated=generated.replace('symlinkSync(target, linkPath);',"symlinkSync(process.platform === 'win32' ? abs : target, linkPath, process.platform === 'win32' ? 'junction' : 'dir');")
    generated=generated.replace('relative(demosDir, p).replace(/\\.tsx$/, "")','relative(demosDir, p).split(String.fromCharCode(92)).join("/").replace(/\\.tsx$/, "")')
    generator.write_text(generated,encoding='utf-8')
    packages = _packages(SHARED)
    desired = json.loads((wb/'package.json').read_text(encoding='utf-8'))
    required = list(desired.get('dependencies',{})) + list(desired.get('devDependencies',{}))
    missing = [p for p in required if not (SHARED/p/'package.json').exists()]
    cli = SHARED/'@remotion/cli/remotion-cli.js'
    if not cli.exists(): cli = SHARED/'@remotion/cli/dist/index.js'
    tools = [
        {'id':'canvas','name':'Vibe Canvas 原生引擎与五套示例','status':'available', 'available':True,
         'cwd':str(canvas), 'url':'/native/canvas/examples/kit_gallery/index.html?play',
         'entry':str(canvas/'engine/vk.js'), 'renderScript':str(canvas/'engine/render.py'),
         'renderAvailable': importlib.util.find_spec('playwright') is not None and shutil.which('ffmpeg') is not None,
         'examples':[{'name':p.parent.name,'url':'/native/canvas/'+quote(str(p.relative_to(canvas)).replace('\\','/'),safe='/')+'?play'}
                     for p in sorted((canvas/'examples').glob('*/index.html'))],
         'notes':['Canvas预览不需要npm；render.py额外需要Python Playwright和Chrome、FFmpeg。','原始示例忠实保留；导入当前项目时仅使用单语字幕。']},
        {'id':'shotcraft','name':'Shotcraft 原生多轨工作台','status':'dependencies_missing' if missing else 'ready',
         'available':not missing,'cwd':str(wb),'url':'http://127.0.0.1:18992/',
         'prepareArgv':['node',str(wb/'scripts/gen-index.mjs')],
         'argv':['node',str(SHARED/'vite/bin/vite.js'),'--host','127.0.0.1','--port','18992','--strictPort'],
         'missingDependencies':missing,'notes':['独立副本，不使用原open.mjs，不安装或升级依赖、不占用5198。',
                    '124张卡与150个本地封面已接入；归档不含150个MP4，动态预览需按卡生成或访问原站。']},
        {'id':'explainer','name':'Anything2Explainer 原生 Remotion Studio','status':'ready' if cli.exists() else 'dependencies_missing',
         'available':cli.exists(),'cwd':str(explainer),'url':'http://127.0.0.1:18993/',
         'argv':['node',str(cli),'studio','src/index.ts','--port=18993','--no-open'],
         'notes':['共享已有Remotion依赖，声明版本与本机版本不同，须以编译及渲染检查为准。',
                  '配音与批量生成只通过工具台明确操作触发，不在准备阶段运行。']}
    ]
    tools.append({'id':'inkpress','name':'Ink Press 完整宣传片模板','status':'ready','available':True,'cwd':str(shot/'template'),'url':'http://127.0.0.1:18994/','argv':['node',str(cli),'studio','src/index.ts','--port=18994','--no-open'],'notes':['完整36.2秒模板，含字幕/音效/字卡/原生可调镜头；独立示例，不替换已选园区影片。']})
    scripts=[]
    for f in sorted((explainer/'scripts').iterdir()):
        if f.is_file():
            scripts.append({'id':f.stem,'name':f.name,'path':str(f),'suffix':f.suffix,
                            'status':'ready' if f.suffix=='.py' else 'requires_bash_or_adapter',
                            'argv':[sys.executable,str(f)] if f.suffix=='.py' else None})
    result={'tools':tools,'scripts':scripts,'packages':packages,'root':str(native),
            'vendorReadOnly':True,'dependencyInstallPerformed':False,'sharedServicesTouched':False}
    (native/'adapter-manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result

def run_demo(card_id, vendor, root, output=None, *, style_id=None, mode='video') -> dict:
    """Render the exact upstream demo in isolation. Known IDs only; never falls back to a generic animation."""
    if mode not in ('video','still'): raise ValueError('demo模式必须是video或still')
    vendor, root = Path(vendor).resolve(), Path(root).resolve()
    catalog = discover(vendor)
    card = next((c for c in catalog['recipes'] if c['id']==card_id),None)
    if not card: raise ValueError('未知镜头卡')
    if not card['available']: raise ValueError('准确原生实现依赖缺失：'+', '.join(card['missingDependencies']))
    style = style_id or card['styles'][0]['id']
    if style not in [s['id'] for s in card['styles']]: raise ValueError('未知镜头变体')
    job = root/'work/demo'/style
    job.mkdir(parents=True,exist_ok=True)
    if output is None: output = root/'outputs/demo'/(style+('.mp4' if mode=='video' else '.png'))
    output = Path(output).resolve()
    if root not in output.parents: raise ValueError('输出必须位于当前工作区')
    config={'vendor':str(vendor),'root':str(root),'card':card,'style':style,'mode':mode,'output':str(output),'modules':str(SHARED),'browser':str(BROWSER)}
    config_file=job/'request.json'
    config_file.write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')
    adapter=Path(__file__).with_name('demo.cjs')
    result=subprocess.run(['node',str(adapter),str(config_file)],cwd=root,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=600,
                          creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode: raise RuntimeError(result.stderr[-5000:] or result.stdout[-5000:])
    receipt=json.loads((Path(str(output)+'.json')).read_text(encoding='utf-8'))
    receipt['preview']='/outputs/'+quote(output.relative_to(root/'outputs').as_posix(),safe='/') if root/'outputs' in output.parents else None
    return receipt

def run_native(tool_id, action, payload, vendor, root, output=None) -> dict:
    """Run a known operation in a copied native project. Saves, renders and QA are explicit actions."""
    if tool_id not in ('canvas','explainer','inkpress'): raise ValueError('未知原生工具')
    vendor,root=Path(vendor).resolve(),Path(root).resolve()
    payload=dict(payload or {})
    slug=str(payload.get('project','default'))
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',slug): raise ValueError('工程名仅支持字母数字、下划线和短横线')
    native_tools(vendor,root)
    native=root/'work/native'
    project=native/'projects'/tool_id/slug
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    out=Path(output).resolve() if output else root/'outputs/native'/tool_id/slug/stamp
    if root not in out.parents: raise ValueError('输出目录超出当前工作区')
    out.mkdir(parents=True,exist_ok=True)
    def response(status='done',**extra):
        return {'status':status,'tool':tool_id,'action':action,'project':str(project),'artifacts':[],**extra}
    def artifact(p):
        p=Path(p);return {'path':str(p),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
            'url':'/outputs/'+quote(p.relative_to(root/'outputs').as_posix(),safe='/') if root/'outputs' in p.parents else None}
    if action in ('new_project','create'):
        if project.exists(): raise ValueError('工程已存在，请用新的工程名；已有原生工程保留')
        project.parent.mkdir(parents=True,exist_ok=True)
        if tool_id in ('explainer','inkpress'):
            shutil.copytree(vendor/(REPOS[2] if tool_id=='explainer' else REPOS[0])/'template',project)
            _junction(project/'node_modules',SHARED)
        else:
            project.mkdir()
            shutil.copy2(native/'canvas/engine/index.html',project/'index.html')
            _junction(project/'engine',native/'canvas/engine')
            default="""(function(){const {W}=VK;const K=VKit;VK.film({dur:8,theme:'gilt',meta:{title:'园区知识短片'},draw(fr,t){fr.text('从一个问题开始',W/2,420,{font:'serif',size:86,color:'#F1D694'});K.subs(fr,t,[{a:0,b:8,zh:'用画面演出原因，再让结论出现。'}],'gilt');}});})();\n"""
            default=default.replace("theme:'gilt'","theme:'flat'" if payload.get('world')=='B' else "theme:'night'").replace("],'gilt')","],'flat')" if payload.get('world')=='B' else "],'night')")
            (project/'film.js').write_text(default,encoding='utf-8')
        (project/'vibe-policy.json').write_text(json.dumps({'singleLanguage':True,'presenterPolicy':'latest-user-selected-version'},ensure_ascii=False),encoding='utf-8')
        return response(message='独立原生工程已创建；单语字幕，保留最新用户选定版本策略。')
    if not project.exists(): return response('needs_project',message='请先创建独立原生工程，已有融合成片未被修改。')
    if action in ('read_film','project_info'):
        return response(content=(project/'film.js').read_text(encoding='utf-8') if tool_id=='canvas' else (project/'src/config.ts').read_text(encoding='utf-8'),
                        files=[str(p.relative_to(project)) for p in project.glob('*') if p.is_file()])
    if action=='save_film':
        if tool_id!='canvas': raise ValueError('film.js仅适用于Canvas工程')
        text=str(payload.get('content',''))
        if not text or len(text)>2_000_000: raise ValueError('film.js内容为空或超过2MB')
        target=project/'film.js'
        if target.exists(): shutil.copy2(target,project/('film.backup-'+stamp+'.js'))
        target.write_text(text,encoding='utf-8');return response(message='film.js已保存，并保留修改前副本。')
    if action=='set_language':
        if payload.get('language','zh') not in ('zh','en'): raise ValueError('选择单一语言zh或en')
        language=payload.get('language','zh')
        if tool_id=='explainer':
            target=project/'src/config.ts';text=target.read_text(encoding='utf-8');text=re.sub(r"lang:\s*'(?:zh|en)'",f"lang: '{language}'",text)
            target.write_text(text,encoding='utf-8')
        else: (project/'language.json').write_text(json.dumps({'language':language,'bilingualSubtitles':False}),encoding='utf-8')
        return response(message='已选择单一语言；不启用双语字幕。')
    allowed_canvas={'stills','sheet','motion','clip','events','determinism','render','sound'}
    allowed_explainer={'still','preview','render','selfcheck','frame_metrics','motion_check','storyboard'}
    if action not in (allowed_canvas if tool_id=='canvas' else allowed_explainer): raise ValueError('该工具不支持此动作')
    env=os.environ.copy()
    env['PYTHONIOENCODING']='utf-8'
    env['PYTHONPATH']=str(root/'work/runtime/python')+os.pathsep+env.get('PYTHONPATH','')
    # 原先此处有一个 bundled 路径（codex 运行时 python），从未被使用，已删除；
    # 需要指定解释器时改用环境变量 SUOJIAN_RUNTIME_PYTHON（见 workbench/paths.py）。
    # The isolated SciPy/Playwright target is installed for the system Python 3.11 ABI.
    python=sys.executable
    if action in ('selfcheck','frame_metrics','motion_check','storyboard'):
        requirements = {'selfcheck':['分镜表.md','script/timeline.json','src/shots'], 'frame_metrics':['分镜表.md','fin_frames'], 'motion_check':['fin_frames'], 'storyboard':['script/storyboard_src.md']}
        missing = [f for f in requirements[action] if not (project/f).exists()]
        if missing:
            return response('needs_input', message='先完成原生工程所需输入：'+', '.join(missing), warnings=['这些原生量化规则面向1280×720横屏；竖屏融合使用工具台的通用成片质检。'])
    if action in ('selfcheck','frame_metrics','motion_check','storyboard','sound'):
        if action=='sound':
            event_file=project/'audio/events.json'
            if not event_file.exists():
                generated=run_native('canvas','events',payload,vendor,root)
                if generated['status']!='done': return generated
            command=[python,str(native/'canvas/engine/audio.py'),str(project)]
            if payload.get('wavOnly',False): command.append('--wav-only')
            if payload.get('bgm'):
                audio=Path(payload['bgm']).resolve()
                if root not in audio.parents and vendor not in audio.parents: raise ValueError('BGM须为已导入的本机素材')
                command+=['--bgm',str(audio),str(float(payload.get('bgmDb',-8)))]
        else:
            script={'selfcheck':'selfcheck.py','frame_metrics':'frame_metrics.py','motion_check':'motion_check.py','storyboard':'render_storyboard.py'}[action]
            command=[python,str(project/'scripts'/script)]
            if action=='selfcheck': command+=['--root',str(project)]
            elif action=='motion_check': command+=['--root',str(project),'--frames',str(project/'fin_frames'),'--shots','index']
            elif action=='frame_metrics': command+=['--frames',str(project/'fin_frames'),'--storyboard',str(project/'分镜表.md'),'--out',str(out/'frame_metrics.md')]
        result=subprocess.run(command,cwd=project,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=1200,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        log=out/(action+'.txt');log.write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
        artifacts=[artifact(log)]
        if action=='sound':
            for source in (project/'audio/score.wav',project/'renders/final.mp4'):
                if source.exists(): target=out/source.name;shutil.copy2(source,target);artifacts.append(artifact(target))
        if action=='storyboard' and (project/'分镜表.md').exists(): target=out/'分镜表.md';shutil.copy2(project/'分镜表.md',target);artifacts.append(artifact(target))
        status='done' if result.returncode==0 else 'dependencies_missing' if 'ModuleNotFoundError' in result.stderr else 'failed'
        if action=='selfcheck' and re.search(r'problems\s*:\s*[1-9]',result.stdout): status='needs_review'
        return response(status,artifacts=artifacts,log=result.stdout[-5000:]+result.stderr[-2000:],exitCode=result.returncode)
    config={'tool':tool_id,'action':action,'payload':payload,'project':str(project),'root':str(root),'output':str(out),'modules':str(SHARED),'browser':str(BROWSER),'ffmpeg':shutil.which('ffmpeg') or 'ffmpeg'}
    request=out/'request.json';request.write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')
    result=subprocess.run(['node',str(Path(__file__).with_name('native.cjs')),str(request)],cwd=project,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=3600,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    log=out/'log.txt';log.write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
    if result.returncode: return response('failed',artifacts=[artifact(log)],log=result.stderr[-5000:],exitCode=result.returncode)
    receipt=json.loads((out/'receipt.json').read_text(encoding='utf-8'))
    for a in receipt.get('artifacts',[]): a.update(artifact(a['path']))
    if tool_id=='canvas' and action=='render':
        (project/'renders').mkdir(exist_ok=True);shutil.copy2(out/'render.mp4',project/'renders/raw.mp4')
    if tool_id=='explainer' and action=='render' and payload.get('extractFrames',True):
        frames=project/'fin_frames';frames.mkdir(exist_ok=True)
        subprocess.run([config['ffmpeg'],'-v','error','-y','-i',str(out/'render.mp4'),'-q:v','4',str(frames/'f_%04d.jpg')],capture_output=True,timeout=300,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),check=True)
    return response(**receipt)
