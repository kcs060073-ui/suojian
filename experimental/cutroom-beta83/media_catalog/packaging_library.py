"""User-created packaging resources; append-only identities preserve existing tasks."""
from pathlib import Path
import hashlib
import json
import math
import re
import shutil
import subprocess
import uuid

FILE_TYPES = {
    'bgm': {'.mp3','.wav','.ogg','.m4a','.aac','.flac','.opus'},
    'sfx': {'.mp3','.wav','.ogg','.m4a','.aac','.flac','.opus'},
    'sticker': {'.png','.jpg','.jpeg','.webp','.gif'},
    'brand': {'.png','.jpg','.jpeg','.webp'},
}
PRESETS = {'subtitle','title_card','info_card','fancy_text'}

def manifest_items(db, category):
    conn = getattr(db, 'connection', db)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='personal_packaging_resources'").fetchone():
        return []
    categories = ['title_card','info_card','brand'] if category in {'title_card','info_card','brand'} else [category]
    rows = conn.execute('SELECT item_json FROM personal_packaging_resources WHERE category IN ('+','.join('?' for _ in categories)+') ORDER BY created_at,asset_ref',categories)
    result=[]
    for row in rows:
        item=json.loads(row[0])
        if item.get('_local_path'):
            path=Path(item['_local_path'])
            item['local_available']=path.is_file()
        result.append(item)
    return result

def create_resource(db, project_root, body):
    from .packaging_plan import find_item
    from .storage_settings import personal_packaging_root
    category=body.get('category')
    if category not in FILE_TYPES and category not in PRESETS:
        raise ValueError('请选择有效的包装分类；音色请从配音服务配置。')
    name=str(body.get('name') or '').strip()
    if not name or len(name)>100: raise ValueError('请填写1—100字的素材名称。')
    asset_ref='personal-pack-'+uuid.uuid4().hex
    copied=None
    if category in PRESETS:
        base=find_item('title_card' if category=='info_card' else category,str(body.get('baseRef') or ''),db)
        if base is None or base.get('file') or base.get('kind')=='watermark':
            raise ValueError('请选择可用的同类样式作为基础。')
        item={k:v for k,v in base.items() if not k.startswith('_')}
        for key in ['primary_hex','highlight_hex','outline_hex','background_hex','accent_hex']:
            if key in body:
                color=str(body[key])
                if not re.fullmatch(r'#[0-9a-fA-F]{6}',color): raise ValueError('颜色格式无效。')
                item[key]=color
        if category=='fancy_text':
            if 'accent_hex' in body:item['fill']=[body['accent_hex']]
            if 'background_hex' in body:item['bg_hex']=body['background_hex']
        if category=='subtitle':item.pop('ass_style',None)
        if 'size' in body:
            size=float(body['size'])
            if not math.isfinite(size) or not 12<=size<=200: raise ValueError('字号应在12—200之间。')
            item['size']=size
    else:
        path=Path(str(body.get('path') or ''))
        if not path.is_absolute() or not path.is_file() or path.suffix.lower() not in FILE_TYPES[category]:
            raise ValueError('请选择当前分类支持的本地素材文件。')
        path=path.resolve()
        if path.stat().st_size>200*1024*1024: raise ValueError('单个包装文件请控制在200MB以内。')
        try: probe=subprocess.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],capture_output=True,text=True,encoding='utf-8',timeout=20)
        except subprocess.TimeoutExpired as error:raise ValueError('文件检查超时，请重试或选择较小的文件。') from error
        try: info=json.loads(probe.stdout)
        except ValueError: info={}
        required='audio' if category in {'bgm','sfx'} else 'video'
        if probe.returncode or not any(x.get('codec_type')==required for x in info.get('streams',[])):
            raise ValueError('文件无法读取，或内容与所选分类不符。')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        for existing in manifest_items(db,category):
            if existing.get('_category')==category and existing.get('sha256')==digest and existing.get('local_available'):
                return {'assetRef':existing['id'],'reused':True}
        root=personal_packaging_root(db,Path(project_root)).resolve()
        folder=root/category/'registered';folder.mkdir(parents=True,exist_ok=True)
        copied=folder/(asset_ref+path.suffix.lower())
        shutil.copyfile(path,copied)
        if hashlib.sha256(copied.read_bytes()).hexdigest()!=digest:
            copied.unlink();raise ValueError('源文件在导入时发生变化，请重新选择。')
        duration=float(info.get('format',{}).get('duration') or 0)
        item={'file':str(copied),'_local_path':str(copied),'_local_root':str(root),'local_available':True,'sha256':digest,'source_sha256':digest}
        if category in {'bgm','sfx'}:
            if not math.isfinite(duration) or duration<=0:
                copied.unlink();raise ValueError('音频时长无效。')
            item.update(duration_s=duration,default_gain=0.18)
        if category=='brand':item['kind']='watermark'
    item.update(id=asset_ref,name=name,license='commercial-approved',_category=category,_personal=True)
    try:
        with db.transaction() as conn:
            conn.execute('INSERT INTO personal_packaging_resources(asset_ref,category,item_json) VALUES(?,?,?)',(asset_ref,category,json.dumps(item,ensure_ascii=False)))
    except Exception:
        if copied and copied.is_file():copied.unlink()
        raise
    return {'assetRef':asset_ref,'reused':False}

def rename_resource(db, body):
    name=str(body.get('name') or '').strip()
    if not name or len(name)>100:raise ValueError('请填写1—100字的素材名称。')
    with db.transaction() as conn:
        row=conn.execute('SELECT item_json FROM personal_packaging_resources WHERE asset_ref=? AND category=?',(body.get('assetRef'),body.get('category'))).fetchone()
        if row is None:raise ValueError('仅能重命名当前分类中的个人素材。')
        item=json.loads(row[0]);item['name']=name
        conn.execute('UPDATE personal_packaging_resources SET item_json=? WHERE asset_ref=?',(json.dumps(item,ensure_ascii=False),body['assetRef']))
    return {'assetRef':body['assetRef']}
