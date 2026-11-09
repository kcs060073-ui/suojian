"""Export the selected storyboard images without adding overlays."""
from io import BytesIO
from pathlib import Path
import zipfile

from PIL import Image, ImageOps

from workbench.vibe_storyboard import Storyboards, digest_file


def storyboard_image(path):
    with Image.open(path) as source:
        return ImageOps.exif_transpose(source).convert('RGB')


def selected_boards(root, task_id, *, require_confirmed=True):
    store=Storyboards(root);task=store.store.get(task_id);state=store.load(task)
    if not state: raise ValueError('没有可导出的分镜')
    result=[]
    for shot in state['shots']:
        board=next((b for b in shot['boards'] if b['version']==shot['selectedBoard']),None)
        if not board or (require_confirmed and board['status']!='confirmed'):
            raise ValueError('请先确认全部分镜再导出')
        path=Path(board['file']).resolve();allowed=store.path(task).resolve()/'images'
        if not path.is_relative_to(allowed) or not path.is_file() or digest_file(path)!=board['sha256']:
            raise ValueError('分镜图片已变化，请重新审查')
        result.append((shot,board,path))
    return result


def export_storyboard(root,task_id,kind):
    items=selected_boards(root,task_id)
    output=BytesIO()
    if kind=='zip':
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
            for shot,board,path in items:
                archive.write(path,shot['id']+path.suffix.lower())
        return output.getvalue(),'application/zip','storyboard-images.zip'
    if kind!='overview': raise ValueError('未知导出方式')
    rendered=[storyboard_image(path) for _,_,path in items]
    cell_width=400
    scaled=[im.resize((cell_width,round(im.height*cell_width/im.width)),Image.Resampling.LANCZOS) for im in rendered]
    cols=min(4,len(scaled)); rows=(len(scaled)+cols-1)//cols;gap=12
    cell_height=max(im.height for im in scaled)
    overview=Image.new('RGB',(gap+(cell_width+gap)*cols,gap+(cell_height+gap)*rows),'#f2f1ed')
    for i,im in enumerate(scaled):overview.paste(im,(gap+(i%cols)*(cell_width+gap),gap+(i//cols)*(cell_height+gap)))
    overview.save(output,format='PNG')
    return output.getvalue(),'image/png','storyboard-overview.png'
