"""Script-to-speech matching. Identity and numeric agreement are hard constraints."""
import re
from pathlib import Path
import json
from rapidfuzz.fuzz import partial_ratio_alignment, ratio

CN='零〇一二两三四五六七八九十百千万点'
DIGITS=dict(zip('零〇一二两三四五六七八九',[0,0,1,2,2,3,4,5,6,7,8,9]))
NUMBER_PATTERN=r'[0-9零〇一二两三四五六七八九十百千万]+(?:[点.][0-9零〇一二两三四五六七八九]+)?'

def number(value):
    if '点' in value:
        a,b=value.split('点',1);return number(a)+'.'+''.join(str(DIGITS.get(c,c)) for c in b)
    if not any(c in value for c in '十百千万'):return ''.join(str(DIGITS.get(c,c)) for c in value)
    total=section=current=0
    for c in value:
        if c in DIGITS or c.isdecimal():current=DIGITS[c] if c in DIGITS else int(c)
        elif c=='万':total+=(section+current)*10000;section=current=0
        else:section+=(current or 1)*{'十':10,'百':100,'千':1000}[c];current=0
    return str(total+section+current)

def normalized(text):
    text=re.sub(NUMBER_PATTERN,lambda m:number(m[0]),text.lower())
    return ''.join(c for c in text if c.isalnum())

def stream(words):
    raw='';mapping=[]
    for i,w in enumerate(words):
        raw+=w['text'];mapping.extend([i]*len(w['text']))
    text='';indices=[];pos=0
    for m in re.finditer(NUMBER_PATTERN+r'|[\s\S]',raw):
        part=number(m[0]) if re.fullmatch(NUMBER_PATTERN,m[0]) else m[0].lower()
        for c in part:
            if c.isalnum():text+=c;indices.append((mapping[m.start()],mapping[m.end()-1]))
        pos=m.end()
    return text,indices

def numeric_facts(text):
    text=re.sub(NUMBER_PATTERN,lambda m:number(m[0]),text)
    return re.findall(r'\d+(?:\.\d+)?',text)

def clauses(text):
    """Punctuation is a boundary; long clauses split at speech-safe phrase hints."""
    result=[]
    for part in re.split(r'[，,。！？!?；;：:\n、]+',text):
        part=part.strip()
        if not part:continue
        if len(normalized(part))>24:
            pieces=re.split(r'(?=带有|还有|可以|您的|重要的是|请点击)',part)
            result.extend(p.strip() for p in pieces if normalized(p))
        else:result.append(part)
    return result

def candidates(text,assets,transcripts):
    needle=normalized(text);out=[]
    if not needle:return out
    for asset in assets:
        speech=transcripts.get(asset['id'])
        if not speech:continue
        words=speech['words'];haystack,index=stream(words)
        spans=[];start=haystack.find(needle)
        while start>=0:
            spans.append((start,start+len(needle)));start=haystack.find(needle,start+1)
        if not spans:
            alignment=partial_ratio_alignment(needle,haystack)
            if alignment:spans=[(alignment.dest_start,alignment.dest_end)]
        for first,last in spans:
            if first>=last:continue
            matched=haystack[first:last];score=ratio(needle,matched)/100
            if score<(.96 if len(needle)<8 else .90):continue
            a=index[first][0];b=index[last-1][1]
            actual=''.join(w['text'] for w in words[a:b+1])
            if numeric_facts(text)!=numeric_facts(actual):continue
            # Do not silently turn a positive statement into its negation.
            if [c for c in normalized(text) if c in '不没无'] != [c for c in normalized(actual) if c in '不没无']:continue
            begin=max(0,words[a]['start']-.08);end=min(asset['duration'],words[b]['end']+.12)
            # Handles never borrow a neighbouring spoken word.
            if a>0:begin=max(begin,words[a-1]['end'])
            if b+1<len(words):end=min(end,words[b+1]['start'])
            if end-begin<.18:continue
            out.append({'asset_id':asset['id'],'name':asset['name'],'person_group':asset['person_group'],
                'source_start':round(begin,3),'source_end':round(end,3),'score':round(score,4),
                'spoken_text':actual,'word_first':a,'word_last':b,'text':text})
    return sorted(out,key=lambda c:-c['score'])

CONCEPTS={
    'location':r'乌鲁木齐','rent':r'租金|毛钱','utilities':r'水电|供暖',
    'dorm':r'宿舍','food':r'食堂','office':r'办公','parking':r'停车|车位',
    'minimum':r'起租|小面积分割','floor':r'整层|这层','environment':r'环评|环屏|环瓶',
    'sewage':r'污水','building':r'整栋','expansion':r'扩租','highway':r'高速',
    'service':r'配套|服务齐全','procedure':r'手续|办理','click':r'点击|链接',
}

def concepts(text):
    return {key for key,pattern in CONCEPTS.items() if re.search(pattern,text)}

def suggest(text,assets,transcripts):
    """Suggestions are review-only; they never become selected cuts automatically."""
    needed=concepts(text)
    if not needed:return []
    options=[]
    for asset in assets:
        speech=transcripts.get(asset['id'])
        if not speech:continue
        chunk=[]
        for word in speech['words']:
            if re.search(r'[，,。！？!?；;、]',word['text']):
                actual=''.join(w['text'] for w in chunk).strip()
                if 3<=len(actual)<=65 and needed<=concepts(actual) and numeric_facts(text)==numeric_facts(actual):
                    options.append({'text':actual,'asset_id':asset['id'],'name':asset['name'],
                        'source_start':chunk[0]['start'],'source_end':chunk[-1]['end'],
                        'note':'相同信息点的候选措辞，需要人工确认，不是原句匹配'})
                chunk=[]
            else:chunk.append(word)
    unique={}
    for option in sorted(options,key=lambda o:len(o['text'])):
        unique.setdefault(normalized(option['text']),option)
    return list(unique.values())[:3]

def match(script,library,work,group_id):
    from .continuity import primary_profile,continuity_bonus,merge_uninterrupted
    group=next((g for g in library['groups'] if g['id']==group_id),None)
    if not group:raise ValueError('请先选择已识别的人物组')
    assets=[a for a in library['assets'] if a.get('person_group')==group_id]
    transcripts={}
    for asset in assets:
        path=Path(work)/'asr'/(asset['id']+'-timed-v1.json')
        if path.exists():transcripts[asset['id']]=json.loads(path.read_text(encoding='utf-8'))
    profile_path=Path(work)/'scene-profiles.json'
    profiles=json.loads(profile_path.read_text(encoding='utf-8')).get('assets',{}) if profile_path.exists() else {}
    texts=clauses(script);all_options=[candidates(t,assets,transcripts) for t in texts]
    primary=primary_profile(all_options,profiles)
    selected=[];missing=[];rows=[];clock=0
    for i,text in enumerate(texts):
        options=all_options[i]
        options=[c for c in options if not any(c['asset_id']==p['asset_id'] and
            min(c['source_end'],p['source_end'])-max(c['source_start'],p['source_start'])>.025 for p in selected)]
        def rank(c):
            return c['score']+continuity_bonus(c,selected[-1] if selected else None,primary,profiles)
        options.sort(key=rank,reverse=True)
        row={'index':i,'text':text,'alternatives':options[:4]}
        if options:
            chosen=dict(options[0]);chosen['index']=i;chosen['target_start']=round(clock,3)
            clock+=chosen['source_end']-chosen['source_start'];chosen['duration']=round(clock-chosen['target_start'],3)
            selected.append(chosen);row['selected']=chosen
        else:
            missing.append(text);row['suggestions']=suggest(text,assets,transcripts)
        rows.append(row)
    matched_count=len(selected)
    selected=merge_uninterrupted(selected,transcripts)
    warnings=['语音文字和切点是模型估计；请审看人脸、数字、句头句尾。原音中的环境噪声会一并保留。']
    if not profiles:warnings.append('当前素材缺少已核对的场景标注，仅能优先保留同一源片的连续口播。')
    return {'script':script,'person_group':group_id,'rows':rows,'selected':selected,'missing':missing,'matched_clause_count':matched_count,
        'continuity_policy':{'mode':'same_scene_and_outfit','primary':primary,'reviewed_scene_profiles':bool(profiles)},
        'coverage':round(matched_count/len(rows),3) if rows else 0,'duration':round(sum(c['duration'] for c in selected),3),
        'status':'ready_for_review' if rows and not missing else 'missing_speech',
        'warnings':warnings}
