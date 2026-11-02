"""Install a new, self-contained local draft without overwriting existing projects."""
import json
import shutil
import uuid
import os
import subprocess
from pathlib import Path
from .library import save

def install_draft(exported,draft_root):
    from suo_jian.jianying.execution_export import register_native_draft
    source=Path(exported.get('export_folder') or exported['folder']).resolve(strict=True);root=Path(draft_root).resolve(strict=True)
    if not root.is_dir() or root.parent==root:raise ValueError('剪映草稿根目录无效')
    name=source.name
    if not name.startswith('溯剪-真人复刻-'):raise ValueError('只能安装本功能生成的草稿')
    destination=root/name
    if destination.exists():
        # A repeated click may re-register the same installed project, but must
        # never replace materials or editor changes in the existing directory.
        if Path(exported['folder']).resolve()!=destination or not exported.get('registration'):
            raise FileExistsError('同名剪映草稿已存在，不会覆盖可能已编辑的工程')
        registration=register_native_draft(destination)
        return {**exported,'registration':registration}
    if os.name=='nt':
        check=subprocess.run(['powershell.exe','-NoProfile','-Command',
            "if (@(Get-Process -Name JianyingPro -ErrorAction SilentlyContinue).Count -gt 0) { Write-Output 'RUNNING' }"],capture_output=True,text=True,timeout=15)
        if check.returncode:raise ValueError('无法确认剪映运行状态，尚未登记草稿')
        if 'RUNNING' in check.stdout:raise ValueError('请先正常退出剪映，再点击“保存到剪映草稿列表”，避免与自动保存冲突')
    content=json.loads((source/'draft_content.json').read_text(encoding='utf-8'))
    for material in content.get('materials',{}).get('videos',[]):
        path=Path(material['path']).resolve(strict=True)
        if not path.is_relative_to(source):raise ValueError('草稿素材必须完整保存在当前导出目录内')
    shutil.copytree(source,destination)
    # Independently identify every installed project, including older exports
    # created before the fixed template UUID was discovered.
    project_id=str(uuid.uuid4()).upper()
    old=str(source).replace('\\','/');new=str(destination).replace('\\','/')
    def relocate(value):
        if isinstance(value,dict):return {k:relocate(v) for k,v in value.items()}
        if isinstance(value,list):return [relocate(v) for v in value]
        if isinstance(value,str):
            candidate=value.replace('\\','/')
            if candidate==old or candidate.startswith(old+'/'):return new+candidate[len(old):]
        return value
    for filename in ('draft_content.json','draft_meta_info.json'):
        path=destination/filename
        value=relocate(json.loads(path.read_text(encoding='utf-8-sig')))
        if filename=='draft_content.json':value['id']=project_id
        else:value.update(draft_id=project_id,draft_name=exported.get('display_name') or name)
        save(path,value)
    rewritten=json.loads((destination/'draft_content.json').read_text(encoding='utf-8'))
    if not all(Path(m['path']).is_file() for m in rewritten['materials']['videos']):
        raise ValueError('保存到剪映后的素材路径核验失败')
    registration=register_native_draft(destination)
    text_count=sum(len(t.get('segments',[])) for t in rewritten.get('tracks',[]) if t.get('type')=='text')
    return {**exported,'folder':str(destination),'export_folder':str(source),'registration':registration,
        'note':f'已保存到本机剪映草稿列表。视频与本人原声在主轨，{text_count}条字幕在独立文字轨；尚需在剪映内审看字体与效果。'}
