"""Render a source-bound illustrative storyboard preview for a confirmed shot.

The result is a silent animatic, never real footage or an accepted final clip.
It does not call generative models, touch Cutroom, or assemble a final film.
"""
from __future__ import annotations
import argparse, hashlib, json, math, subprocess, uuid
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
from workbench.production import Store
from workbench.vibe_storyboard import Storyboards,digest_file

def render(root,task_id,shot_id):
 root=Path(root).resolve();store=Store(root);task=store.get(task_id);api=Storyboards(root)
 api.require_plans(task_id);state=api.get(task_id)
 row=next((s for s in state['shots'] if s['id']==shot_id),None)
 if not row:raise ValueError('镜头不存在')
 board=next((b for b in row['boards'] if b['version']==row['selectedBoard']),None)
 plan=next((p for p in row['plans'] if p['version']==row['selectedPlan']),None)
 if not board or board['status']!='confirmed' or not plan or plan['status']!='confirmed' or digest_file(board['file'])!=board['sha256']:
  raise ValueError('必须使用已确认且哈希一致的分镜和动效计划')
 if any(e['action_type']!='illustrative_animatic' or e['execution_method']!='programmatic' for e in plan['elements']):
  raise ValueError('该执行器只支持明确确认的线稿动态预演计划')
 seconds=row['end']-row['start'];frames=round(seconds*24)
 if seconds<=0 or abs(frames/24-seconds)>.001:raise ValueError('镜头时长需精确对应24fps')
 out=root/'outputs'/'storyboard-animatics'/task_id/('shot-'+shot_id+'-'+uuid.uuid4().hex[:8]);out.mkdir(parents=True,exist_ok=False)
 output=out/'animatic.mp4';img=Image.open(board['file']).convert('RGB').resize((720,1280),Image.Resampling.LANCZOS)
 font='C:/Windows/Fonts/msyhbd.ttc';headline=ImageFont.truetype(font,35);small=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',22)
 title=str(row['description'].get('onscreenText',''))
 cmd=['ffmpeg','-v','error','-n','-f','rawvideo','-pix_fmt','rgb24','-s','720x1280','-r','24','-i','-','-an','-c:v','libx264','-crf','18','-pix_fmt','yuv420p','-movflags','+faststart',str(output)]
 process=subprocess.Popen(cmd,stdin=subprocess.PIPE)
 try:
  for n in range(frames):
   t=n/24
   if shot_id=='03':
    scale=1+.018*t/seconds;w=round(720*scale);h=round(1280*scale)
    im=img.resize((w,h),Image.Resampling.LANCZOS);x=(w-720)//2;y=(h-1280)//2;frame=im.crop((x,y,x+720,y+1280)).convert('RGBA')
   else:frame=img.convert('RGBA')
   if shot_id=='02':
    # Three panels are already present in the supplied illustration. A translucent
    # veil focuses each one in turn; it is not evidence of real footage.
    ranges=[(188,470),(470,730),(730,925)];active=min(int(t/2),2)
    veil=Image.new('RGBA',frame.size);draw=ImageDraw.Draw(veil)
    for j,(y0,y1) in enumerate(ranges):
     if j!=active:draw.rectangle((0,y0,719,y1),fill=(30,35,37,48))
    frame.alpha_composite(veil)
   d=ImageDraw.Draw(frame);d.text((28,30),'分镜动态预演 · 画面为示意',font=small,fill=(40,46,46))
   text=title;box=d.textbbox((0,0),text,font=headline)
   if box[2]-box[0]>610:raise ValueError('标题过长，请先调整')
   d.text(((720-(box[2]-box[0]))/2,970),text,font=headline,fill=(30,32,32))
   if n in (0,frames//2,frames-1):frame.convert('RGB').save(out/f'frame-{n:03}.png')
   process.stdin.write(frame.convert('RGB').tobytes())
  process.stdin.close()
  if process.wait()!=0:raise RuntimeError('FFmpeg预演失败')
 except Exception:
  process.kill();process.wait();raise
 probe=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_type,width,height','-of','json',str(output)],capture_output=True,text=True,check=True)
 metadata=json.loads(probe.stdout)
 if abs(float(metadata['format']['duration'])-seconds)>.05:raise RuntimeError('输出时长不符')
 subprocess.run(['ffmpeg','-v','error','-i',str(output),'-f','null','-'],check=True)
 with output.open('rb') as f:sha=hashlib.file_digest(f,'sha256').hexdigest()
 receipt={'taskId':task_id,'shotId':shot_id,'revision':task['revision'],'inputHash':task['inputHash'],
  'boardVersion':board['version'],'boardSha256':board['sha256'],'planSha256':plan['sha256'],'executionMethod':'programmatic',
  'actual':'静态分镜图程序化突出显示或轻微推近；不含人声、不含真实园区录像','command':cmd,
  'output':str(output),'sha256':sha,'duration':seconds,'visualAcceptance':'unverified'}
 with (out/'receipt.json').open('x',encoding='utf-8') as f:json.dump(receipt,f,ensure_ascii=False,indent=2)
 return receipt

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--task',required=True);parser.add_argument('--shot',required=True);parser.add_argument('--root',default=str(Path(__file__).resolve().parents[1]));args=parser.parse_args()
 print(json.dumps(render(args.root,args.task,args.shot),ensure_ascii=False))
