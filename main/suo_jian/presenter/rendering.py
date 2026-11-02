"""Local synchronized presenter cuts and a real, editable draft."""
import json
import math
import re
import shutil
import subprocess
import uuid
from pathlib import Path
import numpy as np
from .library import Faces,frame,fingerprint,probe,save

def run(command,timeout=900):
    result=subprocess.run(command,capture_output=True,timeout=timeout)
    if result.returncode:raise RuntimeError(result.stderr.decode('utf-8',errors='replace')[-2500:])

def checked_cuts(plan,library,phase=lambda *a,**k:None):
    assets={a['id']:a for a in library['assets']}
    group=next(g for g in library['groups'] if g['id']==plan['person_group'])
    detector=Faces();report=[]
    for i,cut in enumerate(plan['selected']):
        phase('正在复核所选片段中的出镜人物',completed=i,total=len(plan['selected']))
        asset=assets[cut['asset_id']]
        if asset.get('person_group')!=plan['person_group'] or fingerprint(asset['path'])!=asset['id']:
            raise ValueError('素材人物组或文件发生变化，请重新分析')
        scores=[]
        for fraction in (.2,.5,.8):
            t=cut['source_start']+(cut['source_end']-cut['source_start'])*fraction
            observation=detector.observe(frame(asset['path'],t,asset))
            if observation and not observation.get('ambiguous'):
                scores.append(float(np.dot(observation['feature'],group['anchor'])))
        if len(scores)<2 or min(scores)<.50:
            raise ValueError(f"{asset['name']} 的第 {i+1} 个切片人物不够确定，已停止自动生成，请核对素材")
        report.append({'index':i,'scores':[round(s,4) for s in scores]})
    return report

def subtitle_style(reference):
    # Fall back to a neutral legible style, rather than the older yellow default.
    styles=reference.get('styles',{})
    if isinstance(styles,list):styles={s.get('id',str(i)):s for i,s in enumerate(styles)}
    style=styles.get('subtitle_style_01') or next(iter(styles.values()),{})
    return {'font_size':float(style.get('font_size',54)), 'font_weight':900,
        'fill':style.get('fill','#FFFFFF'),'stroke':style.get('stroke',{'color':'#111111','width':4}),
        'shadow':{'enabled':True},'position':style.get('position',{'x':.5,'y':.72})}

def ass_color(value):
    h=str(value).lstrip('#');h=h if len(h)==6 else 'FFFFFF'
    return '&H00'+h[4:6]+h[2:4]+h[0:2]

def ass_time(t):
    c=round(t*100);return f'{c//360000}:{c//6000%60:02}:{c//100%60:02}.{c%100:02}'

def create_ass(path,cuts,style,width,height):
    size=style['font_size']
    x=width*style['position'].get('x',.5);y=height*style['position'].get('y',.72)
    header=f'''[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei,{size},{ass_color(style['fill'])},&H00FFFFFF,{ass_color(style['stroke'].get('color','#111111'))},&H80000000,-1,0,0,0,100,100,0,0,1,{style['stroke'].get('width',4)},1,5,20,20,20,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    lines=[]
    for cut in cuts:
        text=cut['text'].replace('\\','').replace('{','').replace('}','').replace('\n',' ')
        appearance=caption_style(style,cut,width)
        tags=f"\\pos({x:.1f},{y:.1f})\\fs{appearance['font_size']:.1f}\\1c{ass_color(appearance['fill'])}"
        lines.append(f"Dialogue: 0,{ass_time(cut['target_start'])},{ass_time(cut['target_start']+cut['duration'])},Default,,0,0,0,,{{{tags}}}{text}")
    path.write_text(header+'\n'.join(lines),encoding='utf-8-sig');return size

def caption_style(style,caption,width):
    result={**style}
    if caption.get('emphasis'):
        result['fill']='#FFE77A';result['font_size']=float(style['font_size'])*1.25
    result['font_size']=min(result['font_size'],width*.84/max(1,len(caption['text'])))
    return result

def caption_timeline(cuts):
    captions=[]
    for cut in cuts:
        if not cut.get('captions'):
            captions.append({k:cut[k] for k in ('text','target_start','duration')});continue
        for caption in cut['captions']:
            a=max(0,float(caption['local_start']));b=min(cut['duration'],float(caption['local_end']))
            if b<=a:raise ValueError('字幕时间范围无效')
            captions.append({'text':caption['text'],'target_start':round(cut['target_start']+a,6),
                'duration':round(b-a,6),'emphasis':bool(caption.get('emphasis'))})
    return captions

def voice_gain(path,start=0,duration=None):
    command=['ffmpeg','-hide_banner','-ss',str(start)]
    if duration is not None:command+=['-t',str(duration)]
    result=subprocess.run(command+['-i',str(path),'-vn','-af','volumedetect','-f','null','-'],capture_output=True,timeout=120)
    if result.returncode:raise ValueError('无法读取选中口播的音量')
    output=result.stderr.decode('utf-8',errors='replace')
    mean=re.search(r'mean_volume: ([-.0-9]+) dB',output);peak=re.search(r'max_volume: ([-.0-9]+) dB',output)
    if not mean or not peak:raise ValueError('选中片段没有可测量的口播声音')
    average=float(mean[1]);maximum=float(peak[1])
    # Gain only: no voice replacement, time stretching or dynamic voice effects.
    gain=min(12.,max(-12.,-24.-average),-2.-maximum)
    return {'source_mean_db':average,'source_peak_db':maximum,'gain_db':round(gain,2)}

def compile_preview(output,cursor):
    result=subprocess.run(['ffmpeg','-v','error','-y','-f','concat','-safe','1','-i','concat.txt',
        '-map','0:v:0','-map','0:a:0','-map_metadata','-1','-map_chapters','-1',
        '-vf','setpts=PTS-STARTPTS,ass=captions.ass',
        '-af',f'aresample=async=1:first_pts=0,atrim=duration={cursor}',
        '-t',f'{cursor:.6f}','-c:v','libx264','-preset','veryfast','-crf','22','-threads','2',
        '-c:a','aac','-b:a','192k','-movflags','+faststart','preview.mp4'],cwd=output,capture_output=True,timeout=1200)
    if result.returncode:raise RuntimeError(result.stderr.decode('utf-8',errors='replace')[-2500:])
    metadata=probe(output/'preview.mp4')
    if not metadata['has_audio'] or abs(metadata['duration']-cursor)>.05:
        raise ValueError(f"成片音轨或时长核验失败：实际 {metadata['duration']:.3f}s，预期 {cursor:.3f}s")

def balance_existing_render(manifest,work):
    """Retain the previous preview, publish a separate revision with levelled native audio."""
    value=json.loads(json.dumps(manifest));old=Path(work)/manifest['id']
    output=Path(work)/('render-'+uuid.uuid4().hex[:10]);output.mkdir()
    for i,cut in enumerate(value['cuts']):
        level=voice_gain(cut['media']);target=output/f'clip-{i:03}.mkv'
        run(['ffmpeg','-v','error','-y','-i',cut['media'],'-map','0:v:0','-map','0:a:0','-c:v','copy',
            '-af',f"volume={level['gain_db']}dB",'-c:a','pcm_s16le',str(target)])
        cut['media']=str(target);cut['audio_level']=level
    for name in ('concat.txt','captions.ass'):shutil.copy2(old/name,output/name)
    compile_preview(output,sum(c['duration'] for c in value['cuts']))
    value.update(id=output.name,previous_render=manifest['id'],audio_balanced=True)
    save(output/'manifest.json',value);save(Path(work)/'last-render.json',value)
    return value

def render(plan,library,reference,work,partial=False,phase=lambda *a,**k:None):
    if plan['missing'] and not partial:raise ValueError('脚本有缺句，不能生成为完整复刻；可以明确选择制作已匹配片段的小样')
    if not plan['selected']:raise ValueError('没有可用片段')
    identity=checked_cuts(plan,library,phase)
    output=Path(work)/('render-'+uuid.uuid4().hex[:10]);output.mkdir()
    assets={a['id']:a for a in library['assets']};width=1080;height=1920;fps=30;cursor=0;cuts=[]
    for i,selection in enumerate(plan['selected']):
        phase('正在保留新人物原声组装画面',completed=i,total=len(plan['selected']))
        asset=assets[selection['asset_id']];duration=selection['source_end']-selection['source_start']
        frames=max(1,math.ceil(duration*fps-1e-7));length=frames/fps
        level=voice_gain(asset['path'],selection['source_start'],duration)
        filters=['setpts=PTS-STARTPTS']
        if asset.get('transfer') in {'smpte2084','arib-std-b67'}:
            filters.append('zscale=t=linear:npl=100,format=gbrpf32le,tonemap=hable,zscale=p=bt709:t=bt709:m=bt709,format=yuv420p')
        filters.extend([f'scale={width}:{height}:force_original_aspect_ratio=increase',f'crop={width}:{height}',
                        f'fps={fps}','setsar=1','tpad=stop_mode=clone:stop_duration=0.1'])
        # PCM intermediates avoid adding a new AAC priming interval at every cut.
        target=output/f'clip-{i:03}.mkv'
        run(['ffmpeg','-v','error','-y','-threads','2','-ss',str(selection['source_start']),'-t',str(duration),
            '-i',asset['path'],'-map','0:v:0','-map','0:a:0','-vf',','.join(filters),
            '-af',f"asetpts=PTS-STARTPTS,aresample=48000,volume={level['gain_db']}dB,apad,atrim=duration={length},afade=t=in:d=0.015,afade=t=out:st={max(0,length-.015)}:d=0.015",
            '-frames:v',str(frames),'-c:v','libx264','-preset','veryfast','-crf','22','-threads','2',
            '-pix_fmt','yuv420p','-c:a','pcm_s16le','-ac','2',str(target)])
        cut={**selection,'target_start':round(cursor,6),'duration':round(length,6),'media':str(target),'audio_level':level}
        cuts.append(cut);cursor+=length
    phase('正在生成单行字幕和可播放预览',completed=len(cuts),total=len(cuts))
    (output/'concat.txt').write_text('\n'.join(f"file 'clip-{i:03}.mkv'\nduration {cut['duration']:.6f}" for i,cut in enumerate(cuts)),encoding='utf-8')
    style={**subtitle_style(reference),**plan.get('subtitle_style_override',{})}
    captions=caption_timeline(cuts)
    create_ass(output/'captions.ass',captions,style,width,height)
    # A relative filter path avoids escaping drive letters and arbitrary user file names.
    compile_preview(output,cursor)
    manifest={'id':output.name,'partial':bool(plan['missing']),'missing':plan['missing'],'person_group':plan['person_group'],
        'duration':round(cursor,3),'cuts':cuts,'captions':captions,'identity_checks':identity,'style':style,'canvas':[width,height],
        'adapted':bool(plan.get('adapted')),'reference_gaps':plan.get('reference_gaps',[]),'changes':plan.get('changes',[]),
        'audio_balanced':True,
        'continuity_policy':plan.get('continuity_policy',{}),
        'note':'保留新人物口播原声；人脸抽样与语音切点均需审看。普通字幕可编辑，未复刻参考片全部花字和图形。'}
    save(output/'manifest.json',manifest);save(Path(work)/'last-render.json',manifest)
    return manifest

def export_draft(manifest,work):
    import pyJianYingDraft as d
    from suo_jian.jianying.style_mapper import resolve_style
    from suo_jian.jianying.text_geometry import native_font_size,native_border_width
    folder=Path(work)/manifest['id'];name='溯剪-真人复刻-'+manifest['id'][7:]
    root=folder/'drafts';root.mkdir(exist_ok=True);out=root/name
    if out.exists():raise FileExistsError('本次小样的草稿已生成，可直接下载')
    width,height=manifest['canvas']
    script=d.DraftFolder(str(root)).create_draft(name,width,height,30)
    # The library's content template contains a fixed project UUID.
    script.content['id']=str(uuid.uuid4()).upper()
    media=out/'media';media.mkdir()
    video=script.append_track(d.TrackSpec(d.TrackType.video,'同一人物口播与原声'))
    for i,cut in enumerate(manifest['cuts']):
        path=media/f'clip-{i:03}.mp4'
        # Editable source clips contain no burned-in subtitles.
        run(['ffmpeg','-v','error','-y','-i',cut['media'],'-c:v','copy','-c:a','aac','-b:a','192k','-movflags','+faststart',str(path)])
        begin_us,end_us=draft_video_bounds(manifest,i)
        span=d.trange(begin_us,end_us-begin_us)
        material=d.VideoMaterial(str(path))
        # Matroska's millisecond timebase can round a frame endpoint down by <1ms.
        # Keep the target frame duration; never request beyond the material endpoint.
        available=min(material.duration,end_us-begin_us)
        if (end_us-begin_us)-available>5000:raise ValueError('导出片段时长不足，不能缩短口播来凑草稿')
        script.add_segment(d.VideoSegment(material,span,source_timerange=d.trange(0,available),volume=1),video)
    text_track=script.append_track(d.TrackSpec(d.TrackType.text,'实际口播 · 单行可编辑字幕'))
    all_captions=manifest.get('captions',caption_timeline(manifest['cuts']))
    for index,caption in enumerate(all_captions):
        appearance=caption_style(manifest['style'],caption,width)
        style=resolve_style({'presenter':appearance},'presenter')
        begin_us=round(caption['target_start']*1e6);end_us=round((caption['target_start']+caption['duration'])*1e6)
        if index+1<len(all_captions):end_us=min(end_us,round(all_captions[index+1]['target_start']*1e6))
        text=d.TextSegment(caption['text'],d.trange(begin_us,end_us-begin_us),
            style=d.TextStyle(size=native_font_size(style['font_size'],width,height),bold=True,color=style['fill'],
                align=1,auto_wrapping=False,max_line_width=.88),
            border=d.TextBorder(color=style['stroke'],width=native_border_width(style['stroke_width'],style['font_size'])),
            shadow=d.TextShadow(color=(0.,0.,0.),alpha=.65,diffuse=8.,distance=3.),
            clip_settings=d.ClipSettings(transform_x=(style['x']-.5)*2,transform_y=(.5-style['y'])*2))
        script.add_segment(text,text_track)
    script.save()
    meta_path=out/'draft_meta_info.json'
    meta=json.loads(meta_path.read_text(encoding='utf-8-sig'))
    meta.update(draft_id=script.content['id'],draft_name=name)
    save(meta_path,meta)
    shutil.copy2(folder/'preview.mp4',out/'review-preview.mp4')
    save(out/'suojian_presenter_manifest.json',manifest)
    (out/'本机草稿说明.txt').write_text('这是本机可编辑草稿备份，视频原声在主轨，字幕在独立文字轨。\n'
        '素材使用当前电脑的绝对路径。移动到其他目录或电脑时，需要在剪映中重新定位素材。\n'
        '请以工作台显示的本机草稿目录为准，最终效果仍需在当前剪映版本中审看。\n',encoding='utf-8-sig')
    archive=shutil.make_archive(str(folder/'jianying-draft'),'zip',root_dir=root,base_dir=name)
    result={'folder':str(out),'zip':archive,'name':name,'note':'视频和原声在主轨，字幕在文字轨；已生成草稿文件，尚未在剪映界面验收。'}
    save(folder/'draft.json',result);return result

def draft_video_bounds(manifest,index):
    """Round shared endpoints once; independently rounded durations can overlap by 1us."""
    cut=manifest['cuts'][index]
    begin=round(cut['target_start']*1e6)
    end=round((manifest['cuts'][index+1]['target_start'] if index+1<len(manifest['cuts']) else manifest['duration'])*1e6)
    if end<=begin:raise ValueError('草稿片段时间范围无效')
    return begin,end
