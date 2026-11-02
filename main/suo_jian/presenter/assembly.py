"""Auditable assembly from reviewed, word-indexed takes of one presenter."""
import json
import re
from pathlib import Path
from .matching import normalized, numeric_facts

PUNCTUATION=re.compile(r'^[\s，,。！？!?；;：:、]+$')

def reviewed_plan(library,work,group_id,selections,reference_script,reference_gaps=(),changes=()):
    assets={a['id']:a for a in library['assets']}
    selected=[];rows=[];clock=0
    for i,request in enumerate(selections):
        asset=assets[request['asset_id']]
        if asset.get('person_group')!=group_id:raise ValueError('同义方案不能混用其他人物')
        path=Path(work)/'asr'/(asset['id']+'-timed-v1.json')
        transcript=json.loads(path.read_text(encoding='utf-8'));words=transcript['words']
        first,last=int(request['first']),int(request['last'])
        if not 0<=first<=last<len(words):raise ValueError('口播词序号超出范围')
        begin=max(0,words[first]['start']-.15)
        tail=.10 if PUNCTUATION.fullmatch(words[last]['text']) else .25
        end=min(asset['duration'],words[last]['end']+tail)
        if first:begin=max(begin,(words[first-1]['end']+words[first]['start'])/2)
        if last+1<len(words):end=min(end,(words[last]['end']+words[last+1]['start'])/2)
        if end<=begin:raise ValueError('口播切片时长无效')
        if any(c['asset_id']==asset['id'] and min(end,c['source_end'])-max(begin,c['source_start'])>.025 for c in selected):
            raise ValueError('同一段口播不能重复使用')
        corrections=request.get('corrections',{})
        corrected=[]
        for j in range(first,last+1):
            replacement=corrections.get(str(j),words[j]['text'])
            if numeric_facts(replacement)!=numeric_facts(words[j]['text']):raise ValueError('不能以字幕校正替换数字事实')
            corrected.append({**words[j],'text':replacement,'word_index':j})
        raw=''.join(w['text'] for w in words[first:last+1]);actual=''.join(w['text'] for w in corrected)
        captions=[];covered=[]
        for spec in request['captions']:
            a,b=spec['first'],spec['last']
            if not first<=a<=b<=last:raise ValueError('字幕范围必须位于当前口播片段内')
            subset=corrected[a-first:b-first+1]
            text=''.join(w['text'] for w in subset if not PUNCTUATION.fullmatch(w['text']))
            if not text or len(text)>16:raise ValueError('单行字幕应在 1–16 字之间，请按语义拆分')
            if covered and a<=covered[-1]:raise ValueError('字幕词范围不能重复或倒序')
            covered.extend(range(a,b+1))
            captions.append({'text':text,'local_start':max(0,subset[0]['start']-begin-.08),
                'local_end':min(end-begin,subset[-1]['end']-begin+.15),
                'emphasis':bool(spec.get('emphasis'))})
        visible_indices={j for j in range(first,last+1) if not PUNCTUATION.fullmatch(words[j]['text'])}
        if not visible_indices<=set(covered):raise ValueError('字幕漏掉了片段中的实际口播词')
        for j,caption in enumerate(captions):
            caption['local_start']=0 if j==0 else caption['local_start']
            caption['local_end']=captions[j+1]['local_start'] if j+1<len(captions) else end-begin
        cut={'index':i,'asset_id':asset['id'],'name':asset['name'],'person_group':group_id,
            'source_start':round(begin,3),'source_end':round(end,3),'word_first':first,'word_last':last,
            'target_start':round(clock,3),'duration':round(end-begin,3),'score':1.,
            'spoken_text':raw,'text':actual.strip('，,。！？!?；;、'),'captions':captions,
            'reference_point':request.get('reference_point',''),'corrections':corrections}
        clock+=end-begin;selected.append(cut)
        rows.append({'index':i,'text':cut['text'],'selected':cut,'alternatives':[],'reference_point':cut['reference_point']})
    return {'script':'\n'.join(c['text'] for c in selected),'reference_script':reference_script,
        'person_group':group_id,'adapted':True,'adaptation_policy':'同义重组，保留新人物实际原声；缺失事实单独提示',
        'reference_gaps':list(reference_gaps),'changes':list(changes),'rows':rows,'selected':selected,
        'missing':[],'coverage':1.,'duration':round(clock,3),'status':'ready_for_review',
        'subtitle_style_override':{'fill':'#FFFFFF','font_size':72,'position':{'x':.5,'y':.66},
            'stroke':{'color':'#111111','width':5}},
        'warnings':['本片是经审定措辞的同义重组；与原片数字或事实不一致的内容没有自动替换。',
            '人脸抽样和语音切点均是模型估计，仍需审看。']}
