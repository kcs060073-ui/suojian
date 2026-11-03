"""Current fine-analysis coverage; a receipt is not a claim of visual quality."""

def covered_microseconds(windows, duration):
    cursor=0; total=0
    for start,end in sorted(windows):
        start=max(0,start);end=min(duration,end)
        if end>max(cursor,start):total+=end-max(cursor,start)
        cursor=max(cursor,end)
    return total

def annotate_content_coverage(db, items):
    if not items:return
    ids=[x['assetId'] for x in items]
    placeholders=','.join('?' for _ in ids)
    rows=db.connection.execute(
        f"SELECT o.asset_id,o.source_start_us,o.source_end_us FROM visual_observations o "
        f"JOIN assets a USING(asset_id) WHERE o.asset_id IN ({placeholders}) "
        "AND o.asset_fingerprint=a.quick_fingerprint AND o.method_version LIKE 'visual-content-v2-%'",ids).fetchall()
    grouped={}
    for aid,start,end in rows:grouped.setdefault(aid,[]).append((start,end))
    for item in items:
        windows=grouped.get(item['assetId'],[])
        duration=round((item.get('durationSeconds') or 0)*1000000)
        image=item['mediaKind']=='image'
        covered=covered_microseconds(windows,duration) if not image else 0
        complete=bool(windows) if image else duration>0 and covered>=duration-1000
        status='complete' if complete else 'partial' if windows else 'legacy' if item['analysisStatus']=='observed' else 'none'
        item['contentAnalysis']={'status':status,'coveredSeconds':covered/1000000,
            'totalSeconds':None if image else duration/1000000,
            'percent':100 if complete else round(covered/duration*100) if duration else 0}
