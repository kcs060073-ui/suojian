"""Explicit user submission only; prepare, dry-run, send once, then status-only."""
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import sys

PLATFORMS={'tiktok','instagram','youtube','linkedin','facebook','x','threads','pinterest','bluesky'}

def publish(payload, context):
    root=Path(context['root']); out=Path(context['output']); out.mkdir(parents=True,exist_ok=True)
    script=Path(context['vendor'])/'video-shotcraft-main/publish/upload_post.py'
    if not os.environ.get('UPLOAD_POST_API_KEY') or not os.environ.get('UPLOAD_POST_USER'):
        return {'status':'unavailable','artifacts':[],'warnings':['请先在本机配置 Upload-Post 账号连接、UPLOAD_POST_API_KEY 与 UPLOAD_POST_USER；未发起发布。']}
    def invoke(argv, timeout=660):
        p=subprocess.run([str(context.get('python') or sys.executable),str(script),*map(str,argv),'--json-out'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
        try: data=json.loads(p.stdout)
        except ValueError:
            try: data=json.loads(p.stdout.strip().splitlines()[-1])
            except (ValueError,IndexError): data={'status':'unknown','doNotRerun':True,'error':'无法确认服务端受理结果；不得重发，请查询请求状态。'}
        return p.returncode,data
    if payload.get('operation')=='status':
        identity=str(payload.get('requestId',''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{5,150}',identity): raise ValueError('需要先前发布回执中的 requestId / jobId')
        _,data=invoke(['--status',identity],90)
    else:
        if payload.get('confirmed') is not True: raise ValueError('先确认具体成片、文案、平台、时间与隐私设置')
        video=Path(payload.get('video','')).resolve()
        if not video.is_file(): raise ValueError('先选择实际成片')
        platforms=payload.get('platforms') or []
        if not platforms or set(platforms)-PLATFORMS: raise ValueError('平台清单无效')
        title=str(payload.get('caption') or payload.get('title') or '').strip()
        if not title: raise ValueError('请填写发布文案')
        plan={'sha256':hashlib.sha256(video.read_bytes()).hexdigest(),'platforms':platforms,'title':title,'description':payload.get('description',''),'schedule':payload.get('schedule'),'privacy':payload.get('privacy','private'),'aiGenerated':bool(payload.get('aiGenerated'))}
        fingerprint=hashlib.sha256(json.dumps(plan,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        marker=root/'work/publish-attempts'/(fingerprint+'.json'); marker.parent.mkdir(parents=True,exist_ok=True)
        if marker.exists():
            return {'status':'needs_review','artifacts':[str(marker)],'warnings':['这份成片与发布方案已有提交记录。请只查询回执状态，防止重复发布。']}
        argv=['--video',video,'--title',title,'--description',plan['description'],'--platforms',','.join(platforms),'--youtube-privacy',plan['privacy']]
        if payload.get('schedule'): argv+=['--schedule',payload['schedule'],'--timezone',payload.get('timezone','Asia/Shanghai')]
        for key,flag in [('tags','--tags'),('firstComment','--first-comment'),('thumbnail','--thumbnail'),('pinterestBoard','--pinterest-board'),('facebookPage','--facebook-page-id'),('linkedinPage','--linkedin-page-id'),('tiktokPrivacy','--tiktok-privacy')]:
            if payload.get(key): argv += [flag,str(payload[key])]
        for key,flag in [('aiGenerated','--ai-generated'),('tiktokDraft','--tiktok-draft'),('instagramStory','--instagram-story')]:
            if payload.get(key): argv.append(flag)
        code,dry=invoke([*argv,'--dry-run'],90)
        if code or not dry.get('authOk') or dry.get('missingPlatforms'):
            return {'status':'needs_review','artifacts':[],'warnings':['账号或平台 dry run 未通过；未提交视频。'],'validation':{k:dry.get(k) for k in ('authOk','missingPlatforms','status','errorType')}}
        marker.write_text(json.dumps({'plan':plan,'status':'submitting','doNotRerun':True},ensure_ascii=False,indent=2),encoding='utf-8')
        try:
            _,data=invoke(argv)
        except subprocess.TimeoutExpired:
            data={'status':'unknown','doNotRerun':True,'error':'发布过程超时；可能已受理，请查询状态，不要重发。'}
        marker.write_text(json.dumps({'plan':plan,'result':data,'doNotRerun':True},ensure_ascii=False,indent=2),encoding='utf-8')
    file=out/'publish-receipt.json';file.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'status':data.get('status','needs_review'),'artifacts':[str(file)],'warnings':['实际结果以发布回执为准；unknown 只能查询，不要重复提交。'],'result':data}
