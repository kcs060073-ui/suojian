"""Read-only progress for material batches whose item receipts precede job completion."""
import json


def material_progress_for_job(db, job):
    kind=job['job_type']
    status=job.get('public_status') or job['status']
    if kind not in {'scene_detection','visual_analysis'} or status not in {'queued','running','paused'}:
        return None
    row=db.connection.execute('SELECT payload_json FROM job_payloads WHERE job_id=?',
                              (job['job_id'],)).fetchone()
    if row is None: return None
    try:
        payload=json.loads(row['payload_json'])
        items=payload.get('items') if isinstance(payload,dict) else None
        if not isinstance(items,list) or not 1 <= len(items) <= 2000: return None
    except (TypeError,ValueError):
        return None
    total=len(items)
    if kind=='visual_analysis':
        rows=db.connection.execute(
            "SELECT item_index,status FROM visual_batch_items WHERE job_id=? AND item_index>=0 AND item_index<?",
            (job['job_id'],total)).fetchall()
        done={r['item_index'] for r in rows if r['status'] in {'succeeded','reused','failed'}}
        failed={r['item_index'] for r in rows if r['status']=='failed'}
        label,unit='素材分析','个区间'
    else:
        rows=db.connection.execute(
            "SELECT item_index,status,quality_status FROM scene_detection_items WHERE job_id=? AND item_index>=0 AND item_index<?",
            (job['job_id'],total)).fetchall()
        done={r['item_index'] for r in rows if r['status']=='failed' or
              (r['status']=='succeeded' and r['quality_status'] in {'measured','failed'})}
        failed={r['item_index'] for r in rows if r['status']=='failed' or r['quality_status']=='failed'}
        label,unit='本机素材检测','项素材'
    current=len(done)
    if status=='queued': phase=f'{label}排队中 · {total} {unit}'
    elif status=='paused': phase=f'{label}已暂停 · 已处理 {current}/{total}'
    elif kind=='scene_detection' and current==total:
        phase=f'正在比对重复画面 · 素材检测 {current}/{total}'
    elif current==total: phase=f'正在整理分析结果 · 已处理 {current}/{total}'
    else: phase=f'{label}中 · 已处理 {current}/{total}'
    if failed: phase+=f' · {len(failed)} 项失败'
    return {'progress':{'current':current,'total':total},'phase':phase}
