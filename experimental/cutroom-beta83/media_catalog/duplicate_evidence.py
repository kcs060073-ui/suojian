"""Direct, fingerprint-bound source-pair similarity; no transitive exclusion."""
from __future__ import annotations
import hashlib
import json
import numpy as np
from PIL import Image
from .dedup import ahash, dhash, hamming

METHOD_VERSION = 'timed-pair-crop-v1'


def _similarity(a,b):
    if np.asarray(a.convert('L')).std()<8 or np.asarray(b.convert('L')).std()<8:
        return {'matched':False,'correlation':None,'hashDistance':None,'cropFraction':None}
    def patches(image):
        for fraction in (0,.02,.03,.05,.08):
            w,h=image.size;x,y=round(w*fraction),round(h*fraction)
            yield fraction,image.crop((x,y,w-x,h-y)).resize((32,32),Image.Resampling.BILINEAR)
    best={'matched':False,'correlation':0.0,'hashDistance':64,'cropFraction':0}
    # Crop either side against the untrimmed opposite side. Both-sided crops
    # would admit unrelated central patches and hide large composition changes.
    for swap in (False,True):
        left,right=(b,a) if swap else (a,b)
        plain=right.convert('L').resize((32,32),Image.Resampling.BILINEAR)
        for fraction,patch in patches(left):
            x,y=np.asarray(patch.convert('L'),dtype=float),np.asarray(plain,dtype=float)
            if min(x.std(),y.std())<3: continue
            corr=float(np.corrcoef(x.flatten(),y.flatten())[0,1])
            distance=max(hamming(ahash(patch),ahash(plain)),hamming(dhash(patch),dhash(plain)))
            matched=corr>=.93 and distance<=10
            if corr>best['correlation']:
                best={'matched':matched,'correlation':round(corr,5),'hashDistance':distance,
                      'cropFraction':fraction,'croppedSide':'right' if swap else 'left'}
    return best


def compare_frames(left_frames,right_frames,left_window,right_window):
    if not left_frames or not right_frames:
        return {'methodVersion':METHOD_VERSION,'verdict':'unknown','pairs':[],
                'matchedPairs':0,'hardConflict':False,'reason':'没有有效抽样帧'}
    for frames,window in ((left_frames,left_window),(right_frames,right_window)):
        start,end=window;times=[t for t,_ in frames]
        if (type(start) is not int or type(end) is not int or start<0 or end<start
            or times!=sorted(set(times)) or any(type(t) is not int or not start<=t<=end for t in times)):
            raise ValueError('SOURCE_WINDOW_INVALID: 重复比对时码无效')
    count=min(len(left_frames),len(right_frames),120)
    pairs=[]
    for i in range(count):
        li=round(i*(len(left_frames)-1)/max(1,count-1));ri=round(i*(len(right_frames)-1)/max(1,count-1))
        lt,a=left_frames[li];rt,b=right_frames[ri]
        pairs.append({'leftUs':lt,'rightUs':rt,**_similarity(a,b)})
    matches=sum(p['matched'] for p in pairs)
    textured=sum(p['correlation'] is not None for p in pairs)
    def coverage(frames,window):
        span=window[1]-window[0]
        return 1 if span==0 else (frames[-1][0]-frames[0][0])/span
    still = left_window == right_window == (0, 0)
    enough=(count>=3 or still and count==1) and min(coverage(left_frames,left_window),coverage(right_frames,right_window))>=.6
    verdict='suspected' if enough and matches/count>=.8 else 'unknown' if textured<3 else 'not_established'
    return {'methodVersion':METHOD_VERSION,'verdict':verdict,'matchedPairs':matches,
            'pairCount':count,'pairs':pairs,'hardConflict':False,
            'reason':f'{matches}/{count} 对抽样帧相似；仅为近重复线索，需比较实际区间；不作自动禁用'}


def _validate_ref(db,ref):
    from .segment_marks import _duration_us
    if not isinstance(ref,dict): raise ValueError('SOURCE_REFERENCE_INVALID: 缺少素材引用')
    asset= db.connection.execute('SELECT quick_fingerprint,media_kind FROM assets WHERE asset_id=?',(ref.get('assetId'),)).fetchone()
    if asset is None or asset[0]!=ref.get('assetFingerprint'): raise ValueError('ASSET_STALE: 重复证据素材已变化')
    start,end=ref.get('sourceStartUs'),ref.get('sourceEndUs')
    if type(start) is not int or type(end) is not int or start<0 or end<start:
        raise ValueError('SOURCE_WINDOW_INVALID: 重复证据区间无效')
    if asset[1]=='image':
        if (start,end)!=(0,0): raise ValueError('SOURCE_WINDOW_INVALID: 图片无时码')
    elif asset[1]=='video':
        duration=_duration_us(db,ref['assetId'])
        if end<=start or duration is None or end>duration: raise ValueError('SOURCE_WINDOW_INVALID: 重复证据超出源时长')
    else: raise ValueError('MEDIA_KIND_INVALID: 不是画面素材')


def _pair_identity(left, right):
    refs = sorted((left, right), key=lambda ref: json.dumps(ref, sort_keys=True))
    key = hashlib.sha256(json.dumps(refs, sort_keys=True).encode()).hexdigest()
    return key, refs[0], refs[1]


def record_duplicate_evidence(db,left,right,evidence,expected_revision):
    if type(expected_revision) is not int or expected_revision<0: raise ValueError('REVISION_INVALID: 证据版本无效')
    if (evidence.get('methodVersion')!=METHOD_VERSION or evidence.get('verdict') not in {'unknown','suspected','not_established'}
        or evidence.get('hardConflict') is not False or not isinstance(evidence.get('pairs'),list)
        or len(evidence['pairs'])>120): raise ValueError('DUPLICATE_EVIDENCE_INVALID: 重复测量结果无效')
    for p in evidence['pairs']:
        if any(type(p.get(k)) is not int or not ref['sourceStartUs']<=p[k]<=ref['sourceEndUs']
               for k,ref in (('leftUs',left),('rightUs',right))):
            raise ValueError('DUPLICATE_EVIDENCE_INVALID: 帧时码不在引用区间内')
    # One unordered direct pair; reversing it cannot bypass a human correction.
    key, ordered_left, ordered_right = _pair_identity(left, right)
    if ordered_left != left:
        evidence = {**evidence, 'pairs': [{**p, 'leftUs': p['rightUs'], 'rightUs': p['leftUs'],
                                         **({'croppedSide':'right' if p['croppedSide']=='left' else 'left'} if 'croppedSide' in p else {})}
                                         for p in evidence['pairs']]}
    left, right = ordered_left, ordered_right
    with db.transaction(immediate=True) as c:
        _validate_ref(db,left);_validate_ref(db,right)
        old=c.execute('SELECT * FROM duplicate_evidence WHERE pair_key=?',(key,)).fetchone()
        if expected_revision!=(old['revision'] if old else 0): raise ValueError('REVISION_CONFLICT: 重复证据已更新')
        c.execute("INSERT INTO duplicate_evidence(pair_key,left_asset_id,right_asset_id,left_fingerprint,right_fingerprint,"
                  "left_start_us,left_end_us,right_start_us,right_end_us,result_json,revision) VALUES(?,?,?,?,?,?,?,?,?,?,1) "
                  "ON CONFLICT(pair_key) DO UPDATE SET result_json=excluded.result_json,revision=duplicate_evidence.revision+1,updated_at=datetime('now')",
                  (key,left['assetId'],right['assetId'],left['assetFingerprint'],right['assetFingerprint'],left['sourceStartUs'],left['sourceEndUs'],
                   right['sourceStartUs'],right['sourceEndUs'],json.dumps(evidence,ensure_ascii=False)))
        row=c.execute('SELECT * FROM duplicate_evidence WHERE pair_key=?',(key,)).fetchone()
    return {'evidenceId':row['evidence_id'],'revision':row['revision'],'decision':row['decision']}


def correct_duplicate_evidence(db,evidence_id,decision,*,expected_revision,reason):
    if decision not in {'same','different','unknown'} or type(expected_revision) is not int or expected_revision<1:
        raise ValueError('DUPLICATE_DECISION_INVALID: 重复修订无效')
    if not isinstance(reason,str) or not reason.strip() or len(reason)>4000:
        raise ValueError('DUPLICATE_DECISION_INVALID: 需要修订依据')
    with db.transaction(immediate=True) as c:
        row=c.execute('SELECT * FROM duplicate_evidence WHERE evidence_id=?',(evidence_id,)).fetchone()
        if row is None: raise KeyError('DUPLICATE_EVIDENCE_NOT_FOUND')
        for side in ('left','right'):
            _validate_ref(db,dict(assetId=row[side+'_asset_id'],assetFingerprint=row[side+'_fingerprint'],
                                 sourceStartUs=row[side+'_start_us'],sourceEndUs=row[side+'_end_us']))
        if row['revision']!=expected_revision: raise ValueError('REVISION_CONFLICT: 重复证据已更新')
        c.execute("UPDATE duplicate_evidence SET decision=?,reason=?,revision=revision+1,updated_at=datetime('now') WHERE evidence_id=?",
                  (decision,reason,evidence_id))
    return {'evidenceId':evidence_id,'revision':expected_revision+1,'decision':decision}


def duplicates_for_window(db,asset_id,start_us,end_us, *, request_evidence=None):
    rows=request_evidence.duplicate_rows(asset_id) if request_evidence is not None else db.connection.execute('SELECT e.* FROM duplicate_evidence e JOIN assets a ON a.asset_id=e.left_asset_id '
        'JOIN assets b ON b.asset_id=e.right_asset_id WHERE (e.left_asset_id=? OR e.right_asset_id=?) '
        "AND a.quick_fingerprint=e.left_fingerprint AND b.quick_fingerprint=e.right_fingerprint AND e.decision<>'different'",
        (asset_id,asset_id)).fetchall()
    from .quality_windows import evidence_source_status
    result=[]
    for row in rows:
        if any((request_evidence.source_status(row[side+'_asset_id']) if request_evidence is not None else evidence_source_status(db,row[side+'_asset_id']))!='current' for side in ('left','right')):
            continue
        e=json.loads(row['result_json'])
        if e.get('methodVersion')!=METHOD_VERSION: continue
        if e['verdict']!='suspected' and row['decision']!='same': continue
        # A direct relation may compare two different windows in the same file.
        # Return each applicable direction so either displayed span can use it.
        for side in ('left', 'right'):
            if row[side+'_asset_id'] != asset_id: continue
            other='right' if side=='left' else 'left'
            if end_us!=start_us or row[side+'_end_us']!=row[side+'_start_us']:
                if row[side+'_start_us']>=end_us or row[side+'_end_us']<=start_us: continue
            other_location=request_evidence.other_location(row[other+'_asset_id']) if request_evidence is not None else db.connection.execute('SELECT relative_path FROM asset_locations WHERE asset_id=? ORDER BY location_id LIMIT 1',
                (row[other+'_asset_id'],)).fetchone()
            result.append({'otherLabel': other_location[0] if other_location else row[other+'_asset_id'], 'evidenceId':row['evidence_id'],'revision':row['revision'],
                'otherAssetId':row[other+'_asset_id'],'otherStartUs':row[other+'_start_us'],'otherEndUs':row[other+'_end_us'],
                'sourceStartUs':row[side+'_start_us'],'sourceEndUs':row[side+'_end_us'],
                'decision':row['decision'],'reason':row['reason'] or e['reason'],
                'methodVersion':e['methodVersion'],'hardConflict':False})
    return result


def pending_quality_pairs(db, refs):
    """Count unmeasured direct pairs without decoding or changing evidence."""
    eligible=[]
    for ref in refs:
        try: _validate_ref(db,ref)
        except (ValueError,KeyError): continue
        eligible.append(ref)
    missing=0
    for index,left in enumerate(eligible):
        for right in eligible[index+1:]:
            key,_,_=_pair_identity(left,right)
            old=db.connection.execute('SELECT result_json FROM duplicate_evidence WHERE pair_key=?',(key,)).fetchone()
            if old is None or json.loads(old[0]).get('methodVersion')!=METHOD_VERSION:
                missing+=1
    return missing


def compare_quality_results(db,left_id,right_id,*,left_window=None,right_window=None,allow_new=True):
    from .quality_windows import quality_result,decoded_evidence_frames
    left,right=quality_result(db,left_id),quality_result(db,right_id)
    statuses={left['status'],right['status']}
    if 'offline' in statuses: raise ValueError('ASSET_OFFLINE: 相似比对来源当前离线')
    if 'stale' in statuses: raise ValueError('ASSET_STALE: 相似比对来源或方法已变化')
    if statuses!={'measured'}: raise ValueError('QUALITY_EVIDENCE_REQUIRED: 请先分析两段素材')
    lw=left_window or (0,left['durationUs']);rw=right_window or (0,right['durationUs'])
    lref=dict(assetId=left_id,assetFingerprint=left['assetFingerprint'],sourceStartUs=lw[0],sourceEndUs=lw[1])
    rref=dict(assetId=right_id,assetFingerprint=right['assetFingerprint'],sourceStartUs=rw[0],sourceEndUs=rw[1])
    _validate_ref(db,lref);_validate_ref(db,rref)
    key, ordered_left, _right = _pair_identity(lref, rref)
    old=db.connection.execute('SELECT * FROM duplicate_evidence WHERE pair_key=?',(key,)).fetchone()
    manual_reason = {'reason': old['reason']} if old is not None and old['reason'] else {}
    if old:
        cached=json.loads(old['result_json'])
        if cached.get('methodVersion')==METHOD_VERSION:
            if ordered_left!=lref:
                cached={**cached,'pairs':[{**p,'leftUs':p['rightUs'],'rightUs':p['leftUs'],
                    **({'croppedSide':'right' if p['croppedSide']=='left' else 'left'} if 'croppedSide' in p else {})}
                    for p in cached['pairs']]}
            return {**cached,**manual_reason,'evidenceId':old['evidence_id'],'revision':old['revision'],
                    'decision':old['decision'],'left':lref,'right':rref,'reused':True}
    if not allow_new: return None
    evidence=compare_frames(decoded_evidence_frames(left,*lw),decoded_evidence_frames(right,*rw),lw,rw)
    saved=record_duplicate_evidence(db,lref,rref,evidence,old['revision'] if old else 0)
    return {**evidence,**saved,**manual_reason,'left':lref,'right':rref,'reused':False}


def confirmed_window_duplicate(left, right):
    """Return a direct human proof for two displayed uses, never infer transitivity.

    duplicate_evidence must come from duplicates_for_window in this request.
    Equal reference lengths establish a 1:1 time offset; unproven retiming and
    partly unreviewed displayed windows remain advisory.
    """
    if left['kind'] != right['kind']:
        return None
    for a, b in ((left, right), (right, left)):
        for relation in a.get('duplicate_evidence', ()):
            if (relation.get('decision') != 'same'
                    or relation.get('methodVersion') != METHOD_VERSION
                    or relation.get('otherAssetId') != b['asset_id']):
                continue
            bounds = [relation.get(k) for k in
                      ('sourceStartUs', 'sourceEndUs', 'otherStartUs', 'otherEndUs')]
            if any(type(value) is not int or value < 0 for value in bounds):
                continue
            start, end, other_start, other_end = bounds
            if end < start or end-start != other_end-other_start:
                continue
            if a['kind'] == 'image':
                if bounds == [0, 0, 0, 0]:
                    return relation
                continue
            if a['kind'] != 'video' or end == start:
                continue
            a_start, a_end = round(a['start']*1e6), round(a['end']*1e6)
            b_start, b_end = round(b['start']*1e6), round(b['end']*1e6)
            if not (start <= a_start < a_end <= end
                    and other_start <= b_start < b_end <= other_end):
                continue
            mapped_start = b_start-other_start+start
            mapped_end = b_end-other_start+start
            overlap = min(a_end, mapped_end)-max(a_start, mapped_start)
            shorter = min(a_end-a_start, b_end-b_start)
            if overlap >= 500_000 and overlap/shorter >= .75:
                return relation
    return None
