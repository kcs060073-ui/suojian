"""Private evidence reader owned by one candidate-list request; never a write gate."""
from collections import defaultdict


class RequestEvidence:
    def __init__(self, db, assets):
        self.db = db
        self.assets = {a['asset_id']: a for a in assets}
        self._marks, self._quality, self._status = {}, {}, {}
        self._effective_visual = {}
        self._fingerprints = {aid:a['quick_fingerprint'] for aid,a in self.assets.items()}
        self._has_quality = set()
        self._rows = {table: defaultdict(list) for table in
                      ('segments', 'visual_observations', 'catalog_segment_marks', 'quality_evidence_runs')}
        self._duplicates = defaultdict(list)
        self._locations = {}
        ids = list(self.assets)
        # Parameter count and transient result size are bounded per fetch; all
        # eligible windows still participate in the unchanged global ranking.
        pairs = {}
        for begin in range(0, len(ids), 400):
            batch = ids[begin:begin + 400]
            placeholders = ','.join('?' for _ in batch)
            for table, groups in self._rows.items():
                if table == 'quality_evidence_runs':
                    from .quality_windows import METHOD_VERSION
                    # Historical runs affect only the stale/unknown distinction.
                    # Do not materialize their potentially large frame payloads.
                    rows = db.connection.execute(
                        'SELECT q.evidence_id,q.asset_id,q.asset_fingerprint,q.method_version,'
                        'CASE WHEN q.asset_fingerprint=a.quick_fingerprint AND q.method_version=? '
                        'THEN q.result_json ELSE NULL END AS result_json '
                        'FROM quality_evidence_runs q JOIN assets a USING(asset_id) '
                        f'WHERE q.asset_id IN ({placeholders}) ORDER BY q.rowid', [METHOD_VERSION,*batch])
                    for row in rows:
                        self._has_quality.add(row['asset_id'])
                        if row['result_json'] is not None:
                            groups[row['asset_id']].append(dict(row))
                else:
                    for row in db.connection.execute(
                        f'SELECT * FROM {table} WHERE asset_id IN ({placeholders}) ORDER BY rowid', batch):
                        groups[row['asset_id']].append(dict(row))
            for row in db.connection.execute(
                'SELECT e.* FROM duplicate_evidence e JOIN assets a ON a.asset_id=e.left_asset_id '
                'JOIN assets b ON b.asset_id=e.right_asset_id '
                f'WHERE (e.left_asset_id IN ({placeholders}) OR e.right_asset_id IN ({placeholders})) '
                "AND a.quick_fingerprint=e.left_fingerprint AND b.quick_fingerprint=e.right_fingerprint AND e.decision<>'different'",
                batch + batch):
                pairs[row['evidence_id']] = dict(row)
        peers = set()
        for row in sorted(pairs.values(), key=lambda r:r['evidence_id']):
            for side in ('left','right'):
                self._fingerprints.setdefault(row[side+'_asset_id'],row[side+'_fingerprint'])
            for aid in dict.fromkeys((row['left_asset_id'], row['right_asset_id'])):
                self._duplicates[aid].append(row)
                peers.add(aid)
        peers = list(peers)
        for begin in range(0,len(peers),400):
            batch=peers[begin:begin+400];placeholders=','.join('?' for _ in batch)
            for row in db.connection.execute(f'SELECT asset_id,relative_path FROM asset_locations WHERE asset_id IN ({placeholders}) ORDER BY location_id',batch):
                self._locations.setdefault(row['asset_id'], (row['relative_path'],))

    def segments(self, aid):
        return sorted(self._rows['segments'][aid],key=lambda r:(r['start_sec'],r['segment_id']))

    def raw_marks(self, aid):
        fp=self.assets[aid]['quick_fingerprint']
        return [r for r in self._rows['catalog_segment_marks'][aid] if r['asset_fingerprint']==fp]

    def mark_observation(self, aid, mark):
        rows=[r for r in self._rows['visual_observations'][aid]
              if r['asset_fingerprint']==self.assets[aid]['quick_fingerprint']
              and (r['source_start_us'],r['source_end_us'])==(mark['base_start_us'],mark['base_end_us'])]
        return min(rows,key=lambda r:(0 if r['origin']=='human' else 1,-r['observation_id'])) if rows else None

    def marks(self, aid):
        from .segment_marks import current_marks
        if aid not in self._marks:
            self._marks[aid]=current_marks(self.db,aid,request_evidence=self)
        return self._marks[aid]

    def effective_visual_rows(self, aid):
        from .visual_observations import effective_visual_observations
        if aid not in self._effective_visual:
            replaced = {(mark['base_start_us'], mark['base_end_us'])
                        for mark in self.marks(aid).values()}
            self._effective_visual[aid] = effective_visual_observations(
                self._rows['visual_observations'][aid], replaced_windows=replaced)
        return self._effective_visual[aid]

    def visual_rows(self, aid, start, end):
        rows=[r for r in self.effective_visual_rows(aid) if r['source_start_us']<=start and r['source_end_us']>=end]
        return sorted(rows,key=lambda r:(0 if r['origin']=='human' else 1,r['source_end_us']-r['source_start_us'],-r['observation_id']))

    def observation_windows(self, aid):
        windows={(r['source_start_us'],r['source_end_us']) for r in self.effective_visual_rows(aid)
                 if r['asset_fingerprint']==self.assets[aid]['quick_fingerprint'] and r['source_end_us']>r['source_start_us']}
        return [dict(source_start_us=a,source_end_us=b) for a,b in sorted(windows)]

    def quality_row(self, aid):
        from .quality_windows import METHOD_VERSION
        rows=[r for r in self._rows['quality_evidence_runs'][aid]
              if r['asset_fingerprint']==self.assets[aid]['quick_fingerprint'] and r['method_version']==METHOD_VERSION]
        return max(rows,key=lambda r:r['evidence_id']) if rows else None

    def has_quality(self, aid):
        return aid in self._has_quality

    def quality(self, aid):
        from .quality_windows import quality_result
        if aid not in self._quality:
            self._quality[aid]=quality_result(self.db,aid,request_evidence=self)
        return self._quality[aid]

    def source_status(self, aid):
        from .quality_windows import evidence_source_status
        if aid not in self._status:
            def matches_frozen():
                row=self.db.connection.execute('SELECT quick_fingerprint FROM assets WHERE asset_id=?',(aid,)).fetchone()
                return row is not None and row[0] == self._fingerprints.get(aid)
            if not matches_frozen():
                self._status[aid]='stale'
            else:
                status=evidence_source_status(self.db,aid)
                self._status[aid]=status if matches_frozen() else 'stale'
        return self._status[aid]

    def duplicate_rows(self, aid):
        return self._duplicates[aid]

    def other_location(self, aid):
        return self._locations.get(aid)
