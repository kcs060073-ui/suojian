"""Bounded, local, source-timed measurements. Sampling never approves a shot."""
from __future__ import annotations
import base64
import json
import math
import os
import tempfile
import subprocess
from pathlib import Path
import numpy as np
from PIL import Image
from .dedup import _sample_frames, FrameSamplingCancelled
from .visual_media_samples import _source, _image_frame

METHOD_VERSION = "local-quality-motion-v1"
MAX_FRAMES = 120


def _gray(image, size=96):
    return np.asarray(image.convert("L").resize((size, size), Image.Resampling.BILINEAR), dtype=float)


def measure_motion(left, right):
    """Translation supported by multiple textured tiles; no object recognition."""
    a, b = _gray(left), _gray(right)
    vectors = []
    changes = []
    for y in (8, 36, 64):
        for x in (8, 36, 64):
            patch = a[y:y+20, x:x+20]
            if patch.std() < 8:
                continue
            baseline = float(np.abs(patch-b[y:y+20,x:x+20]).mean())
            best = (baseline, 0, 0)
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    error = float(np.abs(patch-b[y+dy:y+dy+20,x+dx:x+dx+20]).mean())
                    if error < best[0]: best = (error, dx, dy)
            if best[0] <= 12 and (best[1:] == (0,0) or baseline > best[0]*1.8+3):
                vectors.append(best[1:])
            changes.append(baseline > 12)
    result = {"cameraMotion": "未知", "subjectMotion": "未确定", "supportTiles": 0,
              "testedTiles": len(changes), "method": "textured-tile-translation",
              "uncertaintyReason": "纹理或全局一致运动不足；不识别推进、环绕或物体身份"}
    if len(changes) < 6: return result
    counts = {v: vectors.count(v) for v in set(vectors)}
    if not counts: return result
    (dx,dy), support = max(counts.items(), key=lambda p:p[1])
    result["supportTiles"] = support
    if support < 6 or support/len(changes) < 0.66: return result
    result["uncertaintyReason"] = "局部平移测量；方向仍需回看片段核对"
    if dx == dy == 0:
        result["cameraMotion"] = "静止"
        result["subjectMotion"] = "局部变化，疑似主体运动" if any(changes) else "未见明显变化"
    else:
        # Center tiles may all belong to one large moving object. Require
        # textured evidence at the image rim before calling this camera motion.
        rim_support = 0
        rims = [(1,x,3,20) for x in (8,36,64)] + [(92,x,3,20) for x in (8,36,64)] + \
               [(y,1,20,3) for y in (8,36,64)] + [(y,92,20,3) for y in (8,36,64)]
        for y,x,h,w in rims:
            if not (0 <= x+dx and x+dx+w <= 96 and 0 <= y+dy and y+dy+h <= 96):
                continue
            patch = a[y:y+h,x:x+w]
            if patch.std() < 8: continue
            shifted = float(np.abs(patch-b[y+dy:y+dy+h,x+dx:x+dx+w]).mean())
            unshifted = float(np.abs(patch-b[y:y+h,x:x+w]).mean())
            if shifted <= 12 and unshifted > shifted*1.8+3: rim_support += 1
        result["rimSupportTiles"] = rim_support
        if rim_support < 6:
            result["uncertaintyReason"] = "全局纹理变化但边缘背景依据不足，不能区分大主体运动与相机运动"
            return result
    if dx == dy == 0:
        return result
    if abs(dx) > abs(dy):
        # Content moves right when the camera pans left.
        result["cameraMotion"] = "向左" if dx > 0 else "向右"
    elif abs(dy) > abs(dx):
        result["cameraMotion"] = "向上" if dy > 0 else "向下"
    return result


def measure_frames(frames, duration_us, *, cancel_check=None):
    """Each sampled bin is measured; suspicious bins never merge across clear ones."""
    if type(duration_us) is not int or duration_us < 0 or not frames or len(frames)>MAX_FRAMES:
        raise ValueError("QUALITY_RESULT_INVALID: 时长或抽帧数量无效")
    times = [t for t,_ in frames]
    if any(type(t) is not int or t<0 or t>duration_us for t in times) or times!=sorted(set(times)):
        raise ValueError("QUALITY_RESULT_INVALID: 抽样时码无效")
    if duration_us == 0 and times != [0]: raise ValueError("QUALITY_RESULT_INVALID: 图片时间码无效")
    boundaries = [0]+[(a+b)//2 for a,b in zip(times,times[1:])]+[duration_us]
    measured, windows = [], []
    for i,(at,image) in enumerate(frames):
        if cancel_check and cancel_check(): raise FrameSamplingCancelled()
        gray = _gray(image)
        mean, std = float(gray.mean()),float(gray.std())
        lap = gray[1:-1,:-2]+gray[1:-1,2:]+gray[:-2,1:-1]+gray[2:,1:-1]-4*gray[1:-1,1:-1]
        sharp = float(lap.var())
        black, white = float((gray<8).mean()),float((gray>247).mean())
        issues = []
        if black>0.98 and std<3: issues.append("疑似黑屏或遮挡")
        elif mean<35: issues.append("偏暗，可能为有意低光")
        if white>0.75: issues.append("大面积高亮，可能过曝或有意白场")
        if sharp<15 and std>=3: issues.append("低细节或疑似模糊，需回看")
        motion = measure_motion(frames[i-1][1],image) if i and at-times[i-1]<=1000000 else {
            "cameraMotion":"未知","subjectMotion":"未确定","supportTiles":0,
            "uncertaintyReason":"首帧或抽样间隔过大"}
        descriptor = base64.b64encode(gray.astype('uint8').tobytes()).decode('ascii')
        measured.append({"timestampUs":at,"meanLuma":round(mean,3),"lumaStd":round(std,3),
            "laplacianVariance":round(sharp,3),"blackFraction":round(black,4),
            "whiteFraction":round(white,4),"issues":issues,"motion":motion,
            "gray96":descriptor})
        start,end=boundaries[i:i+2]
        if windows and windows[-1]['issues']==issues and end-windows[-1]['sourceStartUs']<=15000000:
            windows[-1]['sourceEndUs']=end; windows[-1]['evidenceUs'].append(at)
        else: windows.append({"sourceStartUs":start,"sourceEndUs":end,"issues":issues,
                              "usageStatus":"待核验","evidenceUs":[at]})
    dense = duration_us==0 or max([times[0]*2,(duration_us-times[-1])*2]+
                                  [b-a for a,b in zip(times,times[1:])])<=1100000
    return {"methodVersion":METHOD_VERSION,"durationUs":duration_us,"windows":windows,
            "frames":measured,"coverage":"sampled" if dense else "sparse",
            "automaticRejection":False,
            "uncertaintyReason":"抽样测量不保证未取帧处无问题；偏暗、低细节及遮挡均需人工判断"}


def _current(db, asset_id):
    asset=db.connection.execute("SELECT quick_fingerprint FROM assets WHERE asset_id=?",(asset_id,)).fetchone()
    if asset is None: raise KeyError("ASSET_NOT_FOUND: 素材不存在")
    return db.connection.execute("SELECT * FROM quality_evidence_runs WHERE asset_id=? AND asset_fingerprint=? AND method_version=? "
        "ORDER BY evidence_id DESC LIMIT 1",(asset_id,asset[0],METHOD_VERSION)).fetchone()


def evidence_source_status(db, asset_id):
    """A cached measurement is current only while its actual source is current."""
    locations=db.connection.execute("SELECT al.library_id,al.relative_path FROM asset_locations al JOIN libraries l USING(library_id) "
        "WHERE al.asset_id=? AND al.state='available' AND l.online=1 ORDER BY al.location_id", (asset_id,)).fetchall()
    stale=False
    for location in locations:
        try:
            _source(db,asset_id,location['library_id'],location['relative_path'])
            return 'current'
        except (ValueError,OSError) as error:
            stale = stale or str(error).startswith('ASSET_STALE:')
    return 'stale' if stale else 'offline'


def store_quality_result(db, asset_id, expected_fingerprint, method_version, result):
    if method_version!=METHOD_VERSION or result.get('methodVersion')!=method_version:
        raise ValueError("QUALITY_METHOD_INVALID: 检测方法版本无效")
    duration=result.get('durationUs');frames=result.get('frames');windows=result.get('windows')
    if type(duration) is not int or duration<0 or not isinstance(frames,list) or not 1<=len(frames)<=MAX_FRAMES:
        raise ValueError("QUALITY_RESULT_INVALID: 检测结果无效")
    if not isinstance(windows,list) or not windows or len(windows)>MAX_FRAMES:
        raise ValueError("QUALITY_RESULT_INVALID: 检测区间无效")
    cursor=0
    for w in windows:
        if (type(w.get('sourceStartUs')) is not int or type(w.get('sourceEndUs')) is not int
            or w['sourceStartUs']!=cursor or w['sourceEndUs']<cursor or w['sourceEndUs']>duration
            or not isinstance(w.get('issues'),list) or not w.get('evidenceUs')
            or any(type(t) is not int or not w['sourceStartUs']<=t<=w['sourceEndUs'] for t in w['evidenceUs'])):
            raise ValueError("QUALITY_RESULT_INVALID: 检测区间与证据不一致")
        cursor=w['sourceEndUs']
    if cursor!=duration: raise ValueError("QUALITY_RESULT_INVALID: 区间不完整")
    with db.transaction(immediate=True) as c:
        asset=c.execute("SELECT quick_fingerprint FROM assets WHERE asset_id=?",(asset_id,)).fetchone()
        if asset is None or asset[0]!=expected_fingerprint: raise ValueError("ASSET_STALE: 检测期间素材已变化")
        c.execute("INSERT INTO quality_evidence_runs(asset_id,asset_fingerprint,method_version,result_json) VALUES(?,?,?,?) "
                  "ON CONFLICT(asset_id,asset_fingerprint,method_version) DO UPDATE SET result_json=excluded.result_json",
                  (asset_id,expected_fingerprint,method_version,json.dumps(result,ensure_ascii=False)))
    return quality_result(db,asset_id)


def quality_result(db, asset_id, *, request_evidence=None):
    row=request_evidence.quality_row(asset_id) if request_evidence is not None else _current(db,asset_id)
    if row is None:
        stale=request_evidence.has_quality(asset_id) if request_evidence is not None else db.connection.execute("SELECT 1 FROM quality_evidence_runs WHERE asset_id=? LIMIT 1",(asset_id,)).fetchone()
        return {"status":"stale" if stale else "unknown","windows":[],"frames":[]}
    availability=request_evidence.source_status(asset_id) if request_evidence is not None else evidence_source_status(db,asset_id)
    if availability!='current': return {"status":availability,"windows":[],"frames":[]}
    result=json.loads(row['result_json'])
    if result.get('methodVersion')!=METHOD_VERSION: return {"status":"stale","windows":[],"frames":[]}
    return {**result,"status":"measured","evidenceId":row['evidence_id'],
            "assetFingerprint":row['asset_fingerprint'],"assetId":asset_id}


def quality_for_window(db, asset_id, start_us, end_us, *, request_evidence=None):
    result=request_evidence.quality(asset_id) if request_evidence is not None else quality_result(db,asset_id)
    if result['status']!='measured': return {"status":result['status'],"summary":"尚无当前画质测量证据","issues":[],"penalty":0}
    matched=[w for w in result['windows'] if end_us==start_us==0 or
             w['sourceStartUs']<end_us and w['sourceEndUs']>start_us]
    issues=list(dict.fromkeys(issue for w in matched for issue in w['issues']))
    frames=[f for f in result['frames'] if start_us<=f['timestampUs']<=end_us]
    motions=[f['motion'] for f in frames if f['motion']['cameraMotion']!='未知']
    directions={m['cameraMotion'] for m in motions}
    camera=next(iter(directions)) if len(directions)==1 else '未知'
    subject='局部变化，疑似主体运动' if any(m['subjectMotion']=='局部变化，疑似主体运动' for m in motions) else '未确定'
    from .segment_marks import current_marks
    human_usable=any(m['usage_status']=='可用' and m['source_start_us']<=start_us and m['source_end_us']>=end_us
                     for m in (request_evidence.marks(asset_id) if request_evidence is not None else current_marks(db,asset_id)).values())
    return {"status":"measured","evidenceId":result['evidenceId'],"methodVersion":result['methodVersion'],
        "coverage":result['coverage'],"sourceStartUs":start_us,"sourceEndUs":end_us,
        "evidenceUs":[f['timestampUs'] for f in frames],"issues":issues,
        "cameraMotion":camera,"subjectMotion":subject,"humanOverride":human_usable,"penalty":0 if human_usable else min(30,len(issues)*10),
        "summary":('；'.join(issues) if issues else '抽样帧未见上述指标异常')+'；需回看片段核对',
        "uncertaintyReason":result['uncertaintyReason']}


def quality_source_ranges(db, asset_id, start_us, end_us, *, request_evidence=None):
    result=request_evidence.quality(asset_id) if request_evidence is not None else quality_result(db,asset_id)
    from .segment_marks import current_marks
    if any(m['usage_status']=='可用' and m['source_start_us']<=start_us and m['source_end_us']>=end_us
           for m in (request_evidence.marks(asset_id) if request_evidence is not None else current_marks(db,asset_id)).values()):
        return [(start_us,end_us)]
    points={start_us,end_us}
    for w in result['windows']:
        # Low light / low detail are preferences, never duration constraints.
        if '疑似黑屏或遮挡' in w['issues']:
            for t in (w['sourceStartUs'],w['sourceEndUs']):
                if start_us<t<end_us: points.add(t)
    points=sorted(points)
    return list(zip(points,points[1:]))


def decoded_evidence_frames(result, start_us=0, end_us=None):
    end_us=result.get('durationUs',0) if end_us is None else end_us
    return [(f['timestampUs'],Image.fromarray(np.frombuffer(base64.b64decode(f['gray96']),dtype='uint8').reshape(96,96)))
            for f in result.get('frames',[]) if start_us<=f['timestampUs']<=end_us]


def analyze_quality_windows(db, asset_id, expected_fingerprint, method_version=METHOD_VERSION, *, ffmpeg=None,
                            library_id=None,relative_path=None,cancel_check=None,frame_runner=subprocess.run):
    if method_version!=METHOD_VERSION: raise ValueError("QUALITY_METHOD_INVALID: 未知检测方法")
    if library_id is None:
        loc=db.connection.execute("SELECT al.library_id,al.relative_path FROM asset_locations al JOIN libraries l USING(library_id) "
            "WHERE al.asset_id=? AND al.state='available' AND l.online=1 ORDER BY al.location_id LIMIT 1",(asset_id,)).fetchone()
        if loc is None: raise ValueError("ASSET_OFFLINE: 没有在线素材位置")
        library_id,relative_path=loc
    source,kind,fp=_source(db,asset_id,library_id,relative_path)
    if fp!=expected_fingerprint: raise ValueError("ASSET_STALE: 素材指纹不匹配")
    cached=quality_result(db,asset_id)
    if cached['status']=='measured' and cached['methodVersion']==method_version: return cached
    if kind=='image':
        from io import BytesIO
        frame=_image_frame(source);result=measure_frames([(0,Image.open(BytesIO(frame.jpeg_bytes)))],0)
    elif kind=='video':
        from .dedup import _duration
        from .tools import find_ffmpeg
        ffmpeg=Path(ffmpeg or find_ffmpeg(os.environ))
        duration=_duration(source);n=max(1,min(MAX_FRAMES,math.ceil(duration*2)))
        with tempfile.TemporaryDirectory(prefix='cutroom-quality-') as tmp:
            measured_duration,samples=_sample_frames(source,Path(tmp),n,ffmpeg,
                cancel_check=cancel_check,timeout_seconds=120,batch_size=20,runner=frame_runner)
            frames=[]
            for path,at in samples:
                with Image.open(path) as image: frames.append((round(at*1000000),image.copy()))
            # Some frame rates decode adjacent seeks to the same PTS.
            frames=list({t:(t,im) for t,im in frames}.values())
            result=measure_frames(sorted(frames,key=lambda f:f[0]),round(measured_duration*1000000),cancel_check=cancel_check)
    else: raise ValueError("MEDIA_KIND_INVALID: 只检测图片和视频")
    if cancel_check and cancel_check(): raise FrameSamplingCancelled()
    _path,_kind,current=_source(db,asset_id,library_id,relative_path)
    if current!=expected_fingerprint: raise ValueError("ASSET_STALE: 检测期间源素材变化")
    return store_quality_result(db,asset_id,expected_fingerprint,method_version,result)
