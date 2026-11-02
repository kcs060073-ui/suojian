"""Run under an explicitly selected, already installed pyJianYingDraft Python."""
import json
from pathlib import Path
import sys
import shutil
import uuid
import pyJianYingDraft as d

manifest=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
target=Path(sys.argv[2]); fps=manifest['fps']
def us(frames): return round(float(frames)*1000000/fps)
def timer(item):
    if 'from' in item:
        start=us(item['from']);return start,us(item['to']+1)-start
    start=float(item.get('start',0)); end=float(item.get('end',manifest['frames']/fps))
    return round(start*1000000),round(end*1000000)-round(start*1000000)

folder=d.DraftFolder(str(target.parent))
script=folder.create_draft(target.name,manifest['width'],manifest['height'],fps=round(fps),allow_replace=False)
resources=target/'Resources'; resources.mkdir()
for source in Path(manifest['plate']).parent.glob('*'):
    if source.is_file(): shutil.copy2(source,resources/source.name)
manifest['plate']=str(resources/Path(manifest['plate']).name)
video_track=script.append_track(d.TrackSpec(d.TrackType.video,name='画面底片'))
for shot in manifest['shots']:
    start,duration=timer(shot)
    if duration<=0: raise ValueError('invalid shot duration')
    script.add_segment(d.VideoSegment(manifest['plate'],d.Timerange(start,duration),source_timerange=d.Timerange(start,duration),volume=0),track=video_track)
if manifest['subtitles']:
    text_track=script.append_track(d.TrackSpec(d.TrackType.text,name='单语字幕'))
    for item in manifest['subtitles']:
        start,duration=timer(item)
        if duration<=0: raise ValueError('invalid subtitle duration')
        kwargs={'style':d.TextStyle(size=float(item.get('fontSize',8)),color=(1,1,1),align=1)}
        if hasattr(d,'ClipSettings'): kwargs['clip_settings']=d.ClipSettings(transform_y=-.72)
        script.add_segment(d.TextSegment(item['text'],d.Timerange(start,duration),**kwargs),track=text_track)
for track,items in manifest['tracks'].items():
    name={'voice':'人声','bgm':'背景音乐','sfx':'音效','reference':'选定原片完整混音'}[track]
    audio_track=script.append_track(d.TrackSpec(d.TrackType.audio,name=name))
    expanded=[]
    for item in items:
        if not item.get('loop'):
            expanded.append(item); continue
        mat=d.AudioMaterial(str(resources/Path(item['path']).name))
        start=round(float(item.get('start',0))*1000000)
        left=min(us(manifest['frames'])-start,round(float(item.get('duration',manifest['frames']/fps))*1000000))
        offset=us(item['sourceFrom']) if 'sourceFrom' in item else round(float(item.get('sourceStart',0))*1000000)
        speed=float(item.get('speed',1))
        while left>0:
            count=min(left,round((mat.duration-offset)/speed))
            if count<=0: raise ValueError('循环音频没有可用源时长')
            part={**item,'start':start/1000000,'duration':count/1000000,'sourceStart':offset/1000000,'loop':False}
            for key in ('from','to','sourceFrom','sourceOffset','sourceDuration','sourceDurationFrames'): part.pop(key,None)
            expanded.append(part); left-=count; start+=count; offset=0
    items=expanded
    for index,item in enumerate(items):
        if track=='sfx' and index: audio_track=script.append_track(d.TrackSpec(d.TrackType.audio,name=f'{name}-{index+1}'))
        mat=d.AudioMaterial(str(resources/Path(item['path']).name)); start=us(item['from']) if 'from' in item else round(float(item.get('start',0))*1000000)
        source=us(item['sourceFrom']) if 'sourceFrom' in item else us(item['sourceOffset']) if 'sourceOffset' in item else round(float(item.get('sourceStart',0))*1000000)
        speed=float(item.get('speed',1))
        if speed<=0: raise ValueError('audio speed must be positive')
        available=min(round((mat.duration-source)/speed),us(manifest['frames'])-start)
        duration=min(available,timer(item)[1] if 'from' in item else round(float(item.get('duration',available/1000000))*1000000))
        if duration<=0: continue
        source_duration=us(item['sourceDurationFrames']) if 'sourceDurationFrames' in item else round(float(item['sourceDuration'])*1000000) if 'sourceDuration' in item else round(duration*speed)
        source_duration=min(source_duration,mat.duration-source)
        # Passing source and target ranges lets pyJianYingDraft derive speed, while
        # retaining the exact output frame -> microsecond target interval.
        seg=d.AudioSegment(mat,d.Timerange(start,duration),source_timerange=d.Timerange(source,source_duration),volume=float(item.get('volume',1)))
        if item.get('fadeIn') or item.get('fadeOut'):
            seg.add_fade(round(float(item.get('fadeIn',0))*1000000),round(float(item.get('fadeOut',0))*1000000))
        envelope=[]
        for point in item.get('envelope',[]):
            time=us(point['frame']) if 'frame' in point else round(float(point.get('time',0))*1000000)
            absolute=time if point.get('absolute',True) else time+start
            envelope.append((absolute,float(point.get('volume',point.get('value',1)))))
        envelope.sort()
        def volume_at(time):
            if time<=envelope[0][0]: return envelope[0][1]
            for left,right in zip(envelope,envelope[1:]):
                if time<=right[0]: return left[1]+(right[1]-left[1])*(time-left[0])/max(1,right[0]-left[0])
            return envelope[-1][1]
        if envelope:
            seg.add_keyframe(0,volume_at(start))
            for time,value in envelope:
                if start<time<start+duration: seg.add_keyframe(time-start,value)
            seg.add_keyframe(duration,volume_at(start+duration))
        script.add_segment(seg,track=audio_track)
script.save()
identity=str(uuid.uuid4()).upper()
for filename in ('draft_content.json','draft_meta_info.json'):
    file=target/filename
    if file.exists():
        data=json.loads(file.read_text(encoding='utf-8'));data['id' if filename=='draft_content.json' else 'draft_id']=identity;file.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
(target/'本机草稿说明.txt').write_text('这是独立导出的本机剪映草稿，尚未安装到剪映草稿列表。素材使用当前工作区绝对路径；ZIP作为本机备份，迁移机器或目录需重新生成或重定位素材。请保留Resources和原输出目录。复杂动画已烘焙，单语字幕和声音可编辑。',encoding='utf-8')
print(json.dumps({'status':'completed','draft':str(target/'draft_content.json')}))
