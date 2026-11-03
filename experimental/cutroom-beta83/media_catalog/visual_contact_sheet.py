"""Explicit bounded frame transport for services unreliable with multiple images."""
import base64
import io
import math
from PIL import Image, ImageDraw

TRANSPORT_VERSION='contact_sheet_v1'


def transport_mode(settings):
    mode=settings.get('frame_transport','native')
    if mode not in {'native',TRANSPORT_VERSION}:
        raise ValueError('FRAME_TRANSPORT_INVALID: 未知视觉帧传输方式')
    return mode


def visual_content(settings,content):
    if transport_mode(settings)=='native':
        return content
    count=sum(part.get('type')=='image_url' for part in content)
    if count<=1:
        return content
    if count>16:
        raise ValueError('FRAME_TRANSPORT_LIMIT: 每张帧网格最多16帧')
    images=[];notes=[];pending=[]
    intro=content[0]['text'] if content and content[0].get('type')=='text' else ''
    body=content[1:] if intro else content
    for part in body:
        if part.get('type')=='text':
            pending.append(str(part['text']))
            continue
        if part.get('type')!='image_url':
            raise ValueError('FRAME_TRANSPORT_INVALID: 仅支持文字和已核验的本地图片')
        url=part.get('image_url',{}).get('url','')
        if not isinstance(url,str) or not url.startswith(('data:image/jpeg;base64,','data:image/png;base64,','data:image/webp;base64,')) or len(url)>1400000:
            raise ValueError('FRAME_TRANSPORT_INVALID: 图片必须是有界内嵌数据')
        try:
            data=base64.b64decode(url.split(',',1)[1],validate=True)
            with Image.open(io.BytesIO(data)) as opened:
                if max(opened.size)>4096 or opened.width*opened.height>16777216:
                    raise ValueError('FRAME_TRANSPORT_LIMIT')
                normalized=opened.convert('RGB')
                normalized.thumbnail((320,320),Image.Resampling.LANCZOS)
                images.append(normalized)
        except Exception as error:
            raise ValueError('FRAME_TRANSPORT_INVALID: 无法解码图片') from error
        notes.append('Panel '+str(len(images))+': '+'\n'.join(pending))
        pending=[]
    columns=min(4,len(images));rows=math.ceil(len(images)/columns)
    sheet=Image.new('RGB',(columns*320,rows*348),(24,24,24));draw=ImageDraw.Draw(sheet)
    for index,im in enumerate(images):
        x=index%columns*320;y=index//columns*348
        draw.text((x+8,y+8),'PANEL '+str(index+1),fill='white')
        sheet.paste(im,(x+(320-im.width)//2,y+28+(320-im.height)//2))
    out=io.BytesIO();sheet.save(out,format='PNG')
    text=(intro+'\nThe following single contact sheet contains '+str(len(images))+' ordered frames, NOT one scene. '
          'Read numbered panels left-to-right then top-to-bottom. Each panel is one original image. '
          'Use only the source timestamp in its mapping, never the panel number as time. Ignore empty padding.\n'
          +'\n'.join(notes+pending))
    return [{'type':'text','text':text},{'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(out.getvalue()).decode()}}]
