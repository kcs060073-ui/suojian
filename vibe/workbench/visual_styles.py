"""Shared visual style definitions, independent of any business preset."""

DEFAULT_STYLE = {
    'style_id': 'handdrawn-realistic-v1', 'name': '手绘写实分镜', 'version': 1,
    'description': '浅灰白纸张，黑灰细线手绘，低饱和灰蓝建筑与浅绿植被；人物比例自然，空间透视清晰，商业分镜质感。',
    'line': '黑灰色细线轮廓', 'color': '灰白、灰蓝、浅绿；少量黄或红用于表达',
    'texture': '浅灰白纸张', 'typography': '中文与精确数字后期独立叠加',
    'contentTypes': ['商业解释', '企业介绍', '知识科普', '园区招商'],
    'composition': '9:16 优先，主体清晰，保留字幕和平台安全区，减少无效留白',
    'motion': '依照镜头内容选择克制运动，保持主体结构稳定',
    'avoid': '夸张漫画特效、无意义装饰、未经核实的业务事实',
    'reference': 'work/production/tasks/V20261028-3cac8b0d/storyboards/r001/images/01/f9a2c7c569b84c5cb8c22303a608a8ee.png',
    'referenceNote': '已确认六镜分镜的第一镜裁片，仅作美术参考，不规定新镜头内容。',
    'sample': 'outputs/vibe-workflow-v11/factory-programmatic-six-60aca0c8/shot-01/preview.mp4',
    'sampleStatus': '程序化动态预览，待视觉验收', 'acceptance': '风格参考已确认；动态样片未验收'
}

def default_style(root):
    from copy import deepcopy
    from hashlib import sha256
    from pathlib import Path
    style = deepcopy(DEFAULT_STYLE)
    for key in ('reference', 'sample'):
        source = Path(root) / style[key]
        if not source.is_file():
            style[key] = None
        else:
            with source.open('rb') as stream:
                style[key + 'Sha256'] = sha256(stream.read()).hexdigest()
    return style

class StyleLibrary:
    """Local, versioned reference library; images are copied, never moved."""
    def __init__(self, root):
        from pathlib import Path
        self.root = Path(root).resolve()
        self.base = self.root/'work/production/visual-styles'

    def get(self, style_id, version=None):
        import re
        from workbench.production import read
        if not re.fullmatch(r'style-[0-9a-f]{12}', style_id):
            raise ValueError('风格编号无效')
        folder=self.base/style_id
        meta=read(folder/'index.json')
        if not meta: raise ValueError('风格模板不存在')
        number=version or meta['latest']
        style=read(folder/f'v{number:03d}'/'style.json')
        if not style: raise ValueError('风格版本不存在')
        return style

    def list(self, archived=False):
        from workbench.production import read
        rows=[]
        if self.base.exists():
            for folder in self.base.glob('style-*'):
                meta=read(folder/'index.json')
                if meta and bool(meta.get('archived')) == archived:
                    rows.append(self.get(folder.name))
        return sorted(rows,key=lambda x:x['createdAt'],reverse=True)

    def builtin_archived(self):
        from workbench.production import read
        return bool(read(self.base/'builtin.json',{}).get('archived'))

    def set_archived(self, style_id, archived, expected_version):
        from workbench.production import LOCK, read, save
        with LOCK:
            if style_id == 'handdrawn-realistic-v1':
                if expected_version != 1: raise ValueError('模板版本已变化，请刷新')
                save(self.base/'builtin.json',{'archived':archived})
                return default_style(self.root)
            style=self.get(style_id)
            if style['version'] != expected_version: raise ValueError('模板已有新版本，请刷新')
            index=self.base/style_id/'index.json'
            meta=read(index);meta['archived']=archived;save(index,meta)
            return style

    def create(self, payload):
        import uuid
        from workbench.production import LOCK
        with LOCK:
            style_id='style-'+uuid.uuid4().hex[:12]
            return self._write(style_id,1,payload)

    def revise(self, style_id, payload):
        from workbench.production import LOCK
        with LOCK:
            old=self.get(style_id)
            from workbench.production import read
            if read(self.base/style_id/'index.json',{}).get('archived'):
                raise ValueError('请先恢复模板再修改')
            if payload.get('expectedVersion') != old['version']:
                raise ValueError('模板已有新版本，请刷新后重试')
            merged={'name':payload.get('name',old['name']), 'description':payload.get('description',old['description']),
                    'images':payload.get('images',[r['file'] for r in old['references']]),
                    'source':old.get('source'), 'sourceShots':old.get('sourceShots',[])}
            return self._write(style_id,old['version']+1,merged)

    def _write(self, style_id, version, payload):
        import hashlib, shutil, time, uuid
        from workbench.production import save, text
        from workbench.vibe_storyboard import picture
        name=text(payload.get('name',''),'模板名称',80,True)
        description=text(payload.get('description',''),'风格说明',5000)
        images=payload.get('images',[])
        if not isinstance(images,list) or not 1<=len(images)<=30: raise ValueError('请添加 1–30 张参考图片')
        sources=[]
        for item in images:
            from pathlib import Path
            source_path=Path(item)
            source=picture(source_path if source_path.is_absolute() else self.root/source_path).resolve()
            if not any(source.is_relative_to((self.root/p).resolve()) for p in ('work','outputs')):
                raise ValueError('参考图片必须先导入工作台')
            sources.append(source)
        folder=self.base/style_id/f'v{version:03d}'
        folder.mkdir(parents=True,exist_ok=False)
        references=[]
        for source in sources:
            target=folder/(uuid.uuid4().hex+source.suffix.lower())
            with source.open('rb') as src,target.open('xb') as dst: shutil.copyfileobj(src,dst)
            with target.open('rb') as f: digest=hashlib.file_digest(f,'sha256').hexdigest()
            references.append({'file':str(target),'sha256':digest,'name':source.name,
                               'url':'/files/'+target.relative_to(self.root).as_posix()})
        style={'schema':'suojian.visual-style/1','style_id':style_id,'name':name,'version':version,
               'description':description,'references':references,'source':payload.get('source'),
               'sourceShots':payload.get('sourceShots',[]),'createdAt':time.time(),
               'acceptance':'用户收藏，视觉效果待具体任务审看'}
        save(folder/'style.json',style)
        save(self.base/style_id/'index.json',{'latest':version})
        return style

    def from_storyboard(self, task_id, payload):
        from workbench.vibe_storyboard import Storyboards, digest_file
        store=Storyboards(self.root);task=store.store.get(task_id);state=store.load(task)
        if not state: raise ValueError('该任务尚无分镜')
        ids=payload.get('shotIds') or [s['id'] for s in state['shots']]
        if not isinstance(ids,list) or not ids: raise ValueError('请选择分镜')
        shots=[];images=[]
        for shot_id in ids:
            row=next((s for s in state['shots'] if s['id']==shot_id),None)
            board=next((b for b in (row or {}).get('boards',[]) if b['version']==row['selectedBoard']),None) if row else None
            if not board or digest_file(board['file'])!=board['sha256']: raise ValueError('所选分镜缺少图片或图片已变化')
            images.append(board['file'])
            shots.append({'id':shot_id,'start':row['start'],'end':row['end'],'description':row['description'],
                          'boardSha256':board['sha256'],'reviewStatus':board['status']})
        return self.create({'name':payload.get('name',''), 'description':payload.get('description') or state['artDirection'],
                            'images':images, 'source':{'taskId':task_id,'revision':task['revision'],
                            'inputHash':task['inputHash'],'script':task['input']['script']}, 'sourceShots':shots})
