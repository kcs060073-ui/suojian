"""Bounded local language -> preview -> explicit domain edit -> guarded undo.

No provider, code execution, arbitrary SQL, rendering or business approval.
All writes are receipts or calls to existing manual editing domains.
"""
from __future__ import annotations
from contextlib import contextmanager
import copy
import json
import re
import time
import uuid
from .rhythm_advice import _inputs, _digest
from .rough_cut import (_snapshot_state, _planned_state, preview_edit_impact,
    apply_edit as apply_rough_edit, undo_last_edit, _lock_state)
from .scene_cards import picture_view_digest
from . import packaging_plan
from .snapshots import file_content_signature

METHOD = 'local-whitelist-v1'
TTL = 600
NUM = r'(?:[1-9][0-9]*|[一二三四五六七八九两]|十[一二三四五六七八九]?|[二三四五六七八九]十[一二三四五六七八九]?)'

class _AtomicDomain:
    """Keep existing domains' nested transactions inside the proposal transaction."""
    def __init__(self, db): self.db = db
    def __getattr__(self, name): return getattr(self.db, name)
    @contextmanager
    def transaction(self, *, immediate=False):
        if not self.connection.in_transaction:
            with self.db.transaction(immediate=immediate) as c: yield c
            return
        name = 'proposal_' + uuid.uuid4().hex
        self.connection.execute('SAVEPOINT '+name)
        try:
            yield self.connection
        except BaseException:
            self.connection.execute('ROLLBACK TO '+name)
            raise
        finally:
            self.connection.execute('RELEASE '+name)

def _required(value, label, limit=500):
    if not isinstance(value,str) or not value.strip() or len(value)>limit:
        raise ValueError('PROPOSAL_INPUT_INVALID: '+label+'不能为空或过长')
    return value.strip()

def _number(text):
    if text.isdigit(): return int(text)
    digits={'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    if '十' in text:
        a,b=text.split('十',1)
        return (digits.get(a,1)*10)+digits.get(b,0)
    return digits.get(text,0)

def _plan(db, task_id, plan_id):
    plan=packaging_plan.get_plan(db,plan_id)
    if plan is None or plan['task_id']!=task_id:
        raise KeyError('PROPOSAL_PLAN_NOT_FOUND: 当前任务没有这份方案')
    packaging_plan._require_editable(db.connection,plan_id)
    if plan['schema_version']<2: raise RuntimeError('PACKAGING_LEGACY_PLAN: 先复制旧方案再修改')
    return plan

def _context(db, task_id, expected_revision=None, plan_id=None):
    try: inputs=_inputs(db,task_id,expected_revision)
    except (KeyError,ValueError,RuntimeError) as error:
        raise type(error)(str(error).replace('RHYTHM_','PROPOSAL_')) from error
    resources=[]
    if plan_id:
        plan=_plan(db,task_id,plan_id)
        for item in plan['items']:
            category='title_card' if item['slot'] in {'brand','info_card'} else item['slot']
            path=packaging_plan.resolve_manifest_asset_path(category,item['asset_ref'],db)
            resources.append([item['item_id'],file_content_signature(path) if path and path.is_file() else None])
    history=[tuple(r) for r in db.connection.execute('SELECT edit_id,status,impact_digest FROM rough_cut_edits WHERE task_id=? ORDER BY rowid',(task_id,))]
    digest=_digest([inputs[5],picture_view_digest(db,task_id),_lock_state(db.connection,task_id),history,resources])
    return inputs,digest

def _parse(db,task_id,instruction,inputs,plan_id):
    text=re.sub(r'[\s，,。！!]+','',instruction)
    cards=inputs[2]
    by_position=lambda n: next((c for c in cards if c['idx']==n-1),None)
    def one_card(number):
        card=by_position(_number(number))
        if card is None: raise ValueError('所指镜头不存在，请核对镜头编号')
        return card
    match=re.fullmatch(r'(?:删除|去掉)第('+NUM+r')(?:个)?镜头',text)
    if match: return [{'domain':'rough','edit':{'action':'delete','card_id':one_card(match[1])['card_id']}}],None
    match=re.fullmatch(r'第('+NUM+r')段(?:少切一次|减少一次切镜)',text)
    if match:
        group=next((g for g in inputs[3]['groups'] if g['idx']==_number(match[1])-1),None)
        if not group: return [],'所指文案段不存在，请核对段号。'
        state=_snapshot_state(db.connection,task_id)
        for card in sorted(group['shots'],key=lambda c:c['narrative_end']-c['narrative_start']):
            if card['mix_form']=='数字人全屏': continue
            edit={'action':'delete','card_id':card['card_id']}
            try: _planned_state(db,task_id,state,edit)
            except (ValueError,RuntimeError,KeyError): continue
            return [{'domain':'rough','edit':edit}],None
        return [],'本段没有可安全减少的补画面；请指定镜头或保留当前安排。原人物表达不会自动删除。'
    match=re.fullmatch(r'第('+NUM+r')(?:个)?镜头(左移|右移)',text)
    if match:
        card=one_card(match[1]);order=[c['card_id'] for c in cards];i=order.index(card['card_id']);j=i+(-1 if match[2]=='左移' else 1)
        if not 0<=j<len(order): return [],'该镜头已在边界，不能继续移动。'
        order[i],order[j]=order[j],order[i]
        return [{'domain':'rough','edit':{'action':'reorder','card_ids':order}}],None
    match=re.fullmatch(r'第('+NUM+r')(?:个)?镜头取用([0-9]+(?:\.[0-9]+)?)(?:到|至)([0-9]+(?:\.[0-9]+)?)秒',text)
    if match: return [{'domain':'rough','edit':{'action':'trim','card_id':one_card(match[1])['card_id'],'enter_tc':float(match[2]),'exit_tc':float(match[3])}}],None
    if text in {'保留信息卡去掉贴纸','去掉贴纸','关闭贴纸'}:
        if not plan_id: return [],'请在包装页选中要修改的方案。'
        plan=_plan(db,task_id,plan_id);items=[i for i in plan['items'] if i['slot']=='sticker']
        if len(items)!=1 or items[0]['is_backup'] or not items[0]['enabled']:
            return [],'选中方案没有唯一已开启的贴纸类别；请在现有包装编辑中核对。'
        return [{'domain':'packaging','action':'disable_slot','plan_id':plan_id,'slot':'sticker'}],None
    match=re.fullmatch(r'(?:音乐|BGM|bgm)(?:音量)?(?:改为|设为|设置为)([0-9]+(?:\.[0-9]+)?)%',text)
    if match:
        if not plan_id: return [],'请在包装页选中要修改的方案。'
        return [{'domain':'packaging','action':'settings','plan_id':plan_id,'changes':{'bgm_gain':float(match[1])/100}}],None
    if text in {'关闭重点词','开启重点词','打开重点词'}:
        if not plan_id: return [],'请在包装页选中要修改的方案。'
        return [{'domain':'packaging','action':'settings','plan_id':plan_id,'changes':{'subtitle_highlight':text!='关闭重点词'}}],None
    return [],'请明确镜头、段号或包装参数，例如“第2段少切一次”“删除第3个镜头”“音乐音量改为20%”。暂不修改原稿、主声、倍速或执行代码。'

def _packaging_dispatch(db,operation):
    pid=operation['plan_id']
    if operation['action']=='disable_slot':
        return packaging_plan.disable_optional_slot(db,pid,operation['slot'],connection=db.connection)
    if operation['action']=='settings':
        plan=packaging_plan.get_plan(db,pid)
        return packaging_plan.update_plan_settings(db,pid,expected_revision=plan['settings_revision'],**operation['changes'])
    raise ValueError('PROPOSAL_OPERATION_INVALID: 不支持的操作')

def _preview(db,task_id,operations):
    operation=operations[0]
    if operation['domain']=='rough':
        before=_snapshot_state(db.connection,task_id);after,affected=_planned_state(db,task_id,before,operation['edit'])
        impact=preview_edit_impact(db,task_id,operation['edit'],expected_digest=picture_view_digest(db,task_id))
        return before['cards'],after['cards'],impact['invalidated_stages'],{'impactToken':impact['impact_token']},impact['expires_at']
    before=_plan(db,task_id,operation['plan_id'])
    db.connection.execute('SAVEPOINT proposal_preview')
    try: after=_packaging_dispatch(_AtomicDomain(db),operation)
    finally:
        db.connection.execute('ROLLBACK TO proposal_preview');db.connection.execute('RELEASE proposal_preview')
    private={'beforePlan':before}
    return before,after,['packaging_preview','packaging_confirm','render','review','publish'],private,int(time.time())+TTL

def _row(db,task_id,proposal_id):
    row=db.connection.execute('SELECT * FROM edit_proposals WHERE task_id=? AND proposal_id=?',(task_id,proposal_id)).fetchone()
    if row is None: raise KeyError('PROPOSAL_NOT_FOUND: 当前任务没有这份草稿')
    return row

def _public(db,row):
    result=json.loads(row['result_json']);result.pop('_private',None);result['status']=row['status'];result['stale']=False
    if row['status']=='draft':
        try: result['stale']=_context(db,row['task_id'],plan_id=result.get('planId'))[1]!=row['inputs_digest'] or time.time()>row['expires_at']
        except (KeyError,ValueError,RuntimeError,OSError): result['stale']=True
        if not result['stale'] and result.get('intentJobId'):
            from .edit_intent import verify_intent_binding
            try: verify_intent_binding(db,row['task_id'],result['intentJobId'])
            except (KeyError,ValueError,RuntimeError,OSError): result['stale']=True
    if row['receipt_json']:result['receipt']=json.loads(row['receipt_json'])
    return result

def get_edit_proposal(db,task_id,proposal_id): return _public(db,_row(db,task_id,proposal_id))

def list_edit_proposals(db,task_id):
    return [_public(db,r) for r in db.connection.execute('SELECT * FROM edit_proposals WHERE task_id=? AND request_id NOT LIKE \"intent:%\" ORDER BY rowid DESC LIMIT 20',(task_id,))]

def propose_edit(db,task_id,request_id,expected_revision,instruction,*,plan_id=None):
    request_id=_required(request_id,'请求编号',128);instruction=_required(instruction,'修改要求');expected_revision=_required(expected_revision,'当前版本',128)
    if plan_id is not None:plan_id=_required(plan_id,'方案编号',128)
    signature=_digest([instruction,expected_revision,plan_id])
    with db.transaction(immediate=True):
        row=db.connection.execute('SELECT * FROM edit_proposals WHERE task_id=? AND request_id=?',(task_id,request_id)).fetchone()
        if row:
            if row['request_digest']!=signature:raise RuntimeError('PROPOSAL_REQUEST_CONFLICT: 同一请求编号不能改写内容')
            return _public(db,row)
        inputs,digest=_context(db,task_id,expected_revision,plan_id)
        if inputs[6]:raise RuntimeError('PROPOSAL_SOURCE_UNAVAILABLE: 选定素材当前不可用，请先恢复')
        operations,message=_parse(db,task_id,instruction,inputs,plan_id)
        before=after=[];stages=[];private={};expiry=int(time.time())+TTL
        status='needs_clarification' if not operations else 'draft'
        if operations:
            try: before,after,stages,private,expiry=_preview(db,task_id,operations)
            except (ValueError,RuntimeError,KeyError) as error:
                status='blocked';message=str(error);operations=[]
        pid='proposal-'+uuid.uuid4().hex
        result={'proposalId':pid,'taskId':task_id,'taskRevision':inputs[1],'inputsDigest':digest,'planId':plan_id,
            'instruction':instruction,'method':METHOD,'aiEvaluated':False,'status':status,'operations':operations,
            'before':before,'after':after,'invalidatedStages':stages,'expiresAt':expiry,'message':message,
            'mainAudioPreserved':True,'durationPreserved':True,'_private':private}
        db.connection.execute('INSERT INTO edit_proposals(proposal_id,task_id,request_id,request_digest,task_revision,inputs_digest,status,result_json,expires_at) VALUES(?,?,?,?,?,?,?,?,?)',
            (pid,task_id,request_id,signature,inputs[1],digest,status,json.dumps(result,ensure_ascii=False),expiry))
        return _public(db,_row(db,task_id,pid))

def _replay(db,task_id,proposal_id,request_id,action,expected_revision):
    signature=_digest([proposal_id,action,expected_revision])
    row=db.connection.execute('SELECT * FROM edit_proposal_receipts WHERE task_id=? AND request_id=?',(task_id,request_id)).fetchone()
    if row:
        if row['request_digest']!=signature:raise RuntimeError('PROPOSAL_REQUEST_CONFLICT: 该请求编号已用于其他操作')
        result=json.loads(row['result_json']);result['status']=_row(db,task_id,proposal_id)['status'];return result,signature
    return None,signature

def _save_receipt(db,task_id,proposal_id,request_id,action,signature,result):
    db.connection.execute('INSERT INTO edit_proposal_receipts VALUES(?,?,?,?,?,?)',(task_id,request_id,proposal_id,action,signature,json.dumps(result,ensure_ascii=False)))
    return result

def _guard(db,row,expected_revision,after=False):
    result=json.loads(row['result_json'])
    if not after and time.time()>row['expires_at']:raise RuntimeError('PROPOSAL_EXPIRED: 草稿已过期，请重新生成')
    inputs,digest=_context(db,row['task_id'],plan_id=result.get('planId'))
    if inputs[1]!=expected_revision or (not after and inputs[1]!=row['task_revision']):
        raise RuntimeError('PROPOSAL_REVISION_CHANGED: 当前镜头版本已变化，请重新生成')
    if digest!=(row['after_inputs_digest'] if after else row['inputs_digest']):
        raise RuntimeError('PROPOSAL_INPUTS_CHANGED: 修改依据已变化，请重新核对')
    if not after and result.get("intentJobId"):
        from .edit_intent import verify_intent_binding
        verify_intent_binding(db,row["task_id"],result["intentJobId"])
    return result

def apply_edit(db,task_id,proposal_id,request_id,expected_revision):
    request_id=_required(request_id,'请求编号',128);expected_revision=_required(expected_revision,'当前版本',128)
    with db.transaction(immediate=True):
        row=_row(db,task_id,proposal_id);replay,signature=_replay(db,task_id,proposal_id,request_id,'apply',expected_revision)
        if replay:return replay
        if row['status'] in {'applied','reverted'}:
            result=json.loads(row['receipt_json']);result['status']=row['status']
            return _save_receipt(db,task_id,proposal_id,request_id,'apply',signature,result)
        if row['status']!='draft':raise RuntimeError('PROPOSAL_NOT_DRAFT: 草稿不能应用，请重新生成')
        proposal=_guard(db,row,expected_revision);operation=proposal['operations'][0];domain=_AtomicDomain(db)
        if operation['domain']=='rough':
            applied=apply_rough_edit(domain,task_id,operation['edit'],impact_token=proposal['_private'].get('impactToken'),expected_digest=picture_view_digest(db,task_id))
        else:applied=_packaging_dispatch(domain,operation)
        digest=_context(db,task_id,plan_id=proposal.get('planId'))[1]
        receipt={'proposalId':proposal_id,'status':'applied','result':applied}
        db.connection.execute("UPDATE edit_proposals SET status='applied',receipt_json=?,after_inputs_digest=?,updated_at=datetime('now') WHERE proposal_id=?",(json.dumps(receipt,ensure_ascii=False),digest,proposal_id))
        return _save_receipt(db,task_id,proposal_id,request_id,'apply',signature,receipt)

def cancel_edit(db,task_id,proposal_id,request_id):
    request_id=_required(request_id,'请求编号',128)
    with db.transaction(immediate=True):
        row=_row(db,task_id,proposal_id);replay,signature=_replay(db,task_id,proposal_id,request_id,'cancel',None)
        if replay:return replay
        if row['status'] in {'applied','reverted'}:raise RuntimeError('PROPOSAL_NOT_DRAFT: 已保存的修改请使用撤销')
        db.connection.execute("UPDATE edit_proposals SET status='cancelled',updated_at=datetime('now') WHERE proposal_id=?",(proposal_id,))
        return _save_receipt(db,task_id,proposal_id,request_id,'cancel',signature,{'proposalId':proposal_id,'status':'cancelled'})

def undo_edit(db,task_id,proposal_id,request_id,expected_revision):
    request_id=_required(request_id,'请求编号',128);expected_revision=_required(expected_revision,'当前版本',128)
    with db.transaction(immediate=True):
        row=_row(db,task_id,proposal_id);replay,signature=_replay(db,task_id,proposal_id,request_id,'undo',expected_revision)
        if replay:return replay
        if row['status']=='reverted':return _save_receipt(db,task_id,proposal_id,request_id,'undo',signature,{'proposalId':proposal_id,'status':'reverted','result':None})
        if row['status']!='applied':raise RuntimeError('PROPOSAL_NOT_APPLIED: 本草稿尚未保存')
        proposal=_guard(db,row,expected_revision,after=True);operation=proposal['operations'][0];domain=_AtomicDomain(db)
        if operation['domain']=='rough':
            expected_edit=json.loads(row['receipt_json'])['result']['edit_id']
            latest=db.connection.execute("SELECT edit_id FROM rough_cut_edits WHERE task_id=? AND status='applied' ORDER BY rowid DESC LIMIT 1",(task_id,)).fetchone()
            if latest is None or latest['edit_id']!=expected_edit:raise RuntimeError('PROPOSAL_INPUTS_CHANGED: 最新操作已变化，不能撤销其他修改')
            undone=undo_last_edit(domain,task_id,expected_digest=picture_view_digest(db,task_id))
        elif operation['action']=='settings':
            old=proposal['_private']['beforePlan'];changes={k:bool(old[k]) if k=='subtitle_highlight' else old[k] for k in operation['changes']}
            undone=_packaging_dispatch(domain,{**operation,'changes':changes})
        else:
            old=next(i for i in proposal['_private']['beforePlan']['items'] if i['slot']==operation['slot'])
            undone=packaging_plan.replace_item(domain,operation['plan_id'],operation['slot'],old['asset_ref'],old['asset_file'],old['asset_version'],old['notes'],connection=db.connection)
        receipt={'proposalId':proposal_id,'status':'reverted','result':undone}
        db.connection.execute("UPDATE edit_proposals SET status='reverted',receipt_json=?,updated_at=datetime('now') WHERE proposal_id=?",(json.dumps(receipt,ensure_ascii=False),proposal_id))
        return _save_receipt(db,task_id,proposal_id,request_id,'undo',signature,receipt)


def _create_intent_proposal(db,task_id,request_id,expected_revision,instruction,option,job_id,*,plan_id=None):
    """Internal server-owned intent bridge; HTTP never supplies arbitrary operations."""
    signature=_digest(['intent',job_id,option['optionId'],expected_revision])
    row=db.connection.execute('SELECT * FROM edit_proposals WHERE task_id=? AND request_id=?',(task_id,request_id)).fetchone()
    if row:
        if row['request_digest']!=signature:raise RuntimeError('PROPOSAL_REQUEST_CONFLICT: 请求编号已用于另一修改。')
        return _public(db,row)
    inputs,digest=_context(db,task_id,expected_revision,plan_id)
    operations=[{'domain':'rough','edit':option['edit']}]
    before,after,stages,private,expiry=_preview(db,task_id,operations)
    pid='proposal-'+uuid.uuid4().hex
    result={'proposalId':pid,'taskId':task_id,'taskRevision':inputs[1],'inputsDigest':digest,'planId':plan_id,
        'instruction':instruction,'method':'semantic-p13-options-v1','aiEvaluated':True,'status':'draft',
        'operations':operations,'before':before,'after':after,'invalidatedStages':stages,'expiresAt':expiry,
        'message':option['label'],'evidence':option['evidence'],'intentJobId':job_id,
        'mainAudioPreserved':True,'durationPreserved':True,'_private':private}
    db.connection.execute("INSERT INTO edit_proposals(proposal_id,task_id,request_id,request_digest,task_revision,inputs_digest,status,result_json,expires_at) VALUES(?,?,?,?,?,?,'draft',?,?)",
        (pid,task_id,request_id,signature,inputs[1],digest,json.dumps(result,ensure_ascii=False),expiry))
    return _public(db,_row(db,task_id,pid))