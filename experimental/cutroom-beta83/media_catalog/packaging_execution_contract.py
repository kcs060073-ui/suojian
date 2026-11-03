"""Executable P09 rules shared by request evidence and frozen model instructions."""
import hashlib
import json

VERSION = "exec-v4-refined"
RULES = """
P09 当前产品可执行合同（优先于上文概括性描述）：
先读取 C1 的本次能力和 L1 的真实 phrase 时码，按可用能力选合适元素，不要求全部出现。最小合法例只是格式例，不是要求永远只返回字幕。
1. 整套建议恰好一个 subtitle。若 C1.requested_roles 非空，本次是单类追加草稿，只返回指定类别的新实例；已有字幕和人工实例由程序复制保留，不返回它们。单类追加的 sfx 可跟随 C1.existing_opening 指定的现有标题，不得为它新建 opening。除这两条例外外遵守下列规则。它只选字幕样式，content 必须 null，position=none，start_anchor={kind:script,target_ref:S1,edge:start}，end_anchor 同 S1、edge:end。重点词开关由程序处理；不得为重点词新增 subtitle 实例，不输出或改写字幕文本。
2. bgm 最多一个，与 subtitle 相同的全片锚点和 content:null。品牌也最多一个。sticker、sfx、brand 的 content 必须 null，绝不能输出空 text 对象。不能假称听过声音。
3. opening 只可从 C1.opening.texts 选择已核源候选，绑定 C1.opening.anchor_ref，kind=event，start→end。候选来自确认文案，允许跨开场镜头展示；不能改字。最多一个开场，已有人工元素占用数量预算。
4. info / fancy_text 只能逐字选择 C1.excerpt_candidates 某条 text，两端绑定该候选 source_ref，kind=word，start→end。不能自由提炼，不得将问句变承诺。信息卡最多两个，服从 C1.element_budget 扣除人工元素后的数量；同文同时间不可重复强调。选不到安全文字则省略并说明。
5. sticker 两端同一个真实 shot:*，start→end。sfx 只可在同一结果有 opening 时选一个真实音效，两端 event:opening，kind=event，start edge=start，end edge=end；不允许 start→start，实际音效长度由程序裁定。
6. 只使用 C1 可用类别及实际包装资产支持的 slots；保留关闭类别和人工选择。每条理由说明内容/画面依据，引用真实 frame ref，不编造未采样画面。信息卡与花字的事实引用必须含 S1。
7. C1.assets_by_kind 是每个 kind 允许的素材 ID 硬白名单：opening 只能选其中 opening 列表，sub-title-card 是字幕资产，不能因为名称带 title 就用于 opening。不要自行跨类别复用资产。
8. 顶层只有 template_id,status,result,evidence_refs,unknowns,clarifications。result 只有 instances,omitted_reasons,issues，不要 issues_note 或其他扩展字段。严格遵守输出 Schema，任何文字对象的 text 非空；无文字用 null。返回完整 JSON。
"""

def prompt_body(base):
    return base + RULES

def system_prompt_sha256(body, schema=None):
    from .packaging_agent_requests import response_schema
    # Same exact system-message suffix used by request_visual_json.
    message=body+"\n完整JSON输出协议：\n"+json.dumps(response_schema() if schema is None else schema,ensure_ascii=False)
    return hashlib.sha256(message.encode()).hexdigest()

def prompt_sha256(base):
    return system_prompt_sha256(prompt_body(base))

def spoken_text(value):
    # Preserve numbers, units and internal list punctuation; trim only sentence endings.
    return value.strip().rstrip("，,。！？!?；;：:\r\n").strip()

def opening_texts(scenes,phrases):
    if not scenes:return []
    first=scenes[0]
    return [p['text'] for p in phrases.values() if p['text'] and p['text'] in (first.get('narrative_text') or '')
        and abs(float(p['start'])-float(first['narrative_start']))<.001
        and float(p['end'])<=float(first['narrative_end'])+.001]

def build_capabilities(scenes,phrases,assets,allowed,choices,visual,*,excerpt_candidates=None):
    excerpt_candidates = excerpt_candidates or {}
    titles=list(dict.fromkeys(c["text"] for values in excerpt_candidates.values() for c in values))
    selected=choices.get('selected_assets',{}).get('subtitle',[])
    candidates=[a['ref'] for a in assets.values() if 'subtitle' in a['slots']]
    subtitle=next((a for a in selected if a in candidates),candidates[0] if candidates else None)
    frames=[f['ref'] for f in visual['frames']]
    example={'template_id':'P09','status':'partial','result':{'instances':[],
        'omitted_reasons':['这是格式最小例；正式建议应结合当前合法短句和画面决定是否增加元素'],'issues':[]},
        'evidence_refs':['S1',*frames[:1]],'unknowns':['未听审音乐；未检测遮挡安全区'],'clarifications':[]}
    if subtitle:example['result']['instances'].append({'kind':'subtitle','asset_ref':subtitle,'content':None,
        'start_anchor':{'kind':'script','target_ref':'S1','edge':'start'},
        'end_anchor':{'kind':'script','target_ref':'S1','edge':'end'},'position':'none','reason':'保留本片字幕样式，文字由确认原文生成'})
    slots={'subtitle':'subtitle','bgm':'bgm','opening':'title_card','info':'info_card','sfx':'sfx','sticker':'sticker','fancy_text':'fancy_text','brand':'brand'}
    permitted={kind:[a['ref'] for a in assets.values() if slot in a['slots']] for kind,slot in slots.items() if slot in allowed}
    preferred = {"opening": "card-refined-opening-v1", "info": "card-refined-info-v1", "subtitle": "sub-refined-v1", "fancy_text": "ft-refined-v1"}
    for kind, ref in preferred.items():
        if ref in permitted.get(kind, []) and not choices.get("selected_assets", {}).get(slots[kind]):
            permitted[kind] = [ref]
    from .packaging_excerpt_candidates import budget
    limits = budget(choices)
    for kind, limit in limits.items():
        if limit == 0: permitted.pop(kind, None)
    if not titles:permitted.pop('opening',None)
    if (not titles or 'title_card' not in allowed) and not choices.get('existing_opening'):permitted.pop('sfx',None)
    if not excerpt_candidates:permitted.pop('info',None);permitted.pop('fancy_text',None)
    return {'version':VERSION,'assets_by_kind':permitted,'allowed_categories':sorted(allowed),'opening':{'texts':titles,
        'anchor_ref':'event:opening','available':bool(titles) and 'title_card' in allowed},
        'excerpt_candidates':excerpt_candidates,'element_budget':limits,'phrase_refs':list(phrases),'phrase_rule':'完整文本与区间见 L1.measured_phrases；info/fancy只能同phrase的start到end',
        'requested_roles':choices.get('requested_roles'), 'existing_opening':choices.get('existing_opening'),
        'sfx_requires_opening':not bool(choices.get('existing_opening')),'subtitle_count':0 if choices.get('requested_roles') else 1,'subtitle_and_bgm_content':None,
        'minimum_valid_example':example}


def constrain_schema(schema,prepared):
    """Provider instructions and local validation use the same per-request asset enum."""
    import copy
    schema=copy.deepcopy(schema)
    cap=prepared['execution_capabilities']
    # Issue targets are references too; expose the same registry used by validation.
    schema['$defs']['issue']['properties']['target_ref']['enum']=sorted(prepared['context'].records)
    schema['$defs']['text']['properties']['fact_refs']['items']['enum']=sorted(prepared['context'].approved_fact_refs)
    variants=[]
    for kind,refs in cap['assets_by_kind'].items():
        if not refs:continue
        props={'kind':{'const':kind},'asset_ref':{'enum':refs}}
        if kind in {'subtitle','bgm','sticker','sfx','brand'}:props['content']={'type':'null'}
        def anchor(kind,ref,edge):return {'properties':{'kind':{'const':kind},'target_ref':{'const':ref},'edge':{'const':edge}}}
        if kind in {'subtitle','bgm'}:
            props.update(start_anchor=anchor('script','S1','start'),end_anchor=anchor('script','S1','end'),position={'const':'none'})
        if kind=='opening':
            props.update(content={'type':'object','properties':{'text':{'enum':cap['opening']['texts']}}},start_anchor=anchor('event',cap['opening']['anchor_ref'],'start'),end_anchor=anchor('event',cap['opening']['anchor_ref'],'end'))
        if kind=='sfx':props.update(start_anchor=anchor('event','event:opening','start'),end_anchor=anchor('event','event:opening','end'))
        if kind in {'info','fancy_text'}:props['content']={'type':'object'}
        variant={'properties':props}
        if kind in {'info','fancy_text'}:
            variant['oneOf']=[{'properties':{'content':{'properties':{'text':{'const':p['text']}}},
                'start_anchor':anchor('word',ref,'start'),'end_anchor':anchor('word',ref,'end')}} for ref,values in prepared.get('excerpt_candidates',{}).items() for p in values]
        if kind=='sticker':
            variant['oneOf']=[{'properties':{
                'start_anchor':anchor('shot','shot:'+scene['card_id'],'start'),
                'end_anchor':anchor('shot','shot:'+scene['card_id'],'end')}} for scene in prepared['scenes']
                if float(scene['narrative_end'] or 0)-float(scene['narrative_start'] or 0)>=.5]
        if kind=='brand':props.update(start_anchor=anchor('script','S1','start'),end_anchor=anchor('script','S1','end'))
        variants.append(variant)
    array=schema['$defs']['P09Output']['properties']['result']['anyOf'][0]['properties']['instances']
    if variants:array['items']['oneOf']=variants
    else:array['maxItems']=0
    array['allOf']=[{'contains':{'properties':{'kind':{'const':kind}}},'minContains':cap['subtitle_count'] if kind=='subtitle' else 0,'maxContains':1} for kind in ('subtitle','bgm','brand','sfx')]
    array['allOf'].extend({'contains':{'properties':{'kind':{'const':kind}}},'minContains':0,'maxContains':limit} for kind,limit in cap['element_budget'].items())
    if cap['sfx_requires_opening']:array['allOf'].append({'if':{'contains':{'properties':{'kind':{'const':'sfx'}}}},'then':{'contains':{'properties':{'kind':{'const':'opening'}}}}})
    return schema
