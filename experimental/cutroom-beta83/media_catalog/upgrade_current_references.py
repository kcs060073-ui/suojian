"""Current editable references: backup observations never replace frozen signatures."""
from pathlib import Path

from .upgrade_recovery import _fail, _plain_path
from .upgrade_packaging_references import _object, _address
from .upgrade_render_references import _CORE_MANIFEST_FIELDS
from .packaging_plan import SLOT_MANIFEST
from .path_utils import filesystem_path


def complete_current_references(db, root, tables, catalog, packaging, unresolved, observe):
    root=Path(root)
    rows=lambda table: [dict(r) for r in db.connection.execute(f'SELECT * FROM "{table}"')] if table in tables else []
    resolved=set()
    def done(table,identity,code):
        resolved.add((table,identity,code))
    for location in catalog['locations']:
        if location['status']=='fingerprint_verified' and location['backupObservationRequired']:
            observe('asset_locations',location['locationId'],'mount_path/relative_path',location['originalPath'])
    for cache in catalog['cache']:
        if cache['status']=='present_unverified' and cache['backupObservationRequired']:
            observe('preview_cache',cache['rowId'],'rel_path',cache['originalPath'])
    bgm_gaps={item['rowId'] for item in catalog['unresolved'] if item['table']=='packaging_plan_items'}
    bgm_ids={item['itemId'] for item in catalog['localBgmItems'] if item['status']=='catalog_reference' and item['locationIds'] and item['itemId'] not in bgm_gaps}
    for identity in bgm_ids:
        done('packaging_plan_items',identity,'LOCAL_BGM_ADAPTER_PENDING')
    local_frozen={item['snapshotId'] for item in catalog['frozenLocalBgm'] if item['status']=='verified'}
    unresolved_bgm={item['rowId'] for item in catalog['unresolved'] if item['table']=='render_snapshots'}
    for identity in local_frozen-unresolved_bgm:
        done('render_snapshots',identity,'PACKAGING_LOCAL_REFERENCE_PENDING')
    def current_item(table,identity,item):
        ref=item.get('asset_ref')
        if str(ref or '').startswith('local-bgm-'):
            asset_id=ref[len('local-bgm-'):]
            if item.get('asset_file') not in (None,'catalog:'+asset_id):
                _fail('PACKAGING_CURRENT_REFERENCE_CHANGED','模板本地音乐引用与登记编号不一致。')
            return any(location['assetId']==asset_id and location['status']=='verified'
                       for location in catalog['locations']) and db.connection.execute(
                "SELECT 1 FROM packaging_assets p JOIN assets a USING(asset_id) WHERE p.asset_id=? AND p.kind='bgm' AND a.media_kind='audio'",(asset_id,)).fetchone() is not None
        slot=item.get('slot')
        if slot not in SLOT_MANIFEST or not isinstance(ref,str) or not ref:
            return False
        manifest=_plain_path(root/'assets/manifests'/SLOT_MANIFEST[slot])
        if not filesystem_path(manifest).is_file():
            return False
        # This reads the OLD installation's manifest, never module-global/new code assets.
        observe(table,identity,'manifest',str(manifest))
        payload=_object(filesystem_path(manifest).read_text(encoding='utf8'))
        values=payload.get('items')
        if not isinstance(values,list) or any(not isinstance(v,dict) for v in values):
            return False
        matches=[v for v in values if v.get('id')==ref]
        if len(matches)!=1:
            return False
        value=matches[0]
        if set(value)-_CORE_MANIFEST_FIELDS or value.get('version') not in (None,'2026-09'):
            return False
        if item.get('asset_version') not in (None,value.get('version')):
            _fail('PACKAGING_CURRENT_VERSION_CHANGED','当前包装版本与旧安装清单不一致。')
        raw=item.get('asset_file') or value.get('file')
        if item.get('asset_file') and value.get('file') and Path(item['asset_file']).as_posix()!=Path(value['file']).as_posix():
            _fail('PACKAGING_CURRENT_REFERENCE_CHANGED','当前包装文件与旧安装清单不一致。')
        allowed=tuple(root/name for name in ('public','assets','asset-packs'))
        if raw:
            path=_address(raw,root,allowed)
            observe(table,identity,'asset_file',str(path))
        if value.get('font'):
            font=_address(value['font'],root/'assets/fonts',(root/'assets/fonts',))
            observe(table,identity,'font',str(font))
        done(table,identity,'PACKAGING_ITEM_SIGNATURE_UNAVAILABLE')
        done(table,identity,'PACKAGING_MANIFEST_RESOLUTION_PENDING')
        return True
    for item in rows('packaging_plan_items'):
        if not str(item.get('asset_ref') or '').startswith('local-bgm-'):
            current_item('packaging_plan_items',item['item_id'],item)
    for item in rows('packaging_plan_instances'):
        if not current_item('packaging_plan_instances',item['instance_id'],{**item,'slot':item['role']}):
            unresolved.append({'table':'packaging_plan_instances','rowId':item['instance_id'],'code':'CURRENT_INSTANCE_REFERENCE_UNRESOLVED'})
    from .provenance import canonical_digest
    item_fields={'slot','asset_ref','asset_file','asset_version','is_backup','rank','notes','enabled'}
    rule_fields={'key','role','asset_ref','placement','start_scene_ratio','start_offset','end_scene_ratio','end_offset',
                 'follow_key','follow_event','source_length','x','y','scale','gain','source_offset','fade_in','fade_out'}
    for template in rows('packaging_templates'):
        identity=template['template_id'];snapshot=_object(template['snapshot_json'])
        if canonical_digest(snapshot)!=template['snapshot_digest']:
            _fail('PACKAGING_TEMPLATE_INVALID','个人模板与保存的原摘要不一致。')
        if (snapshot.get('schema',1) not in (1,2) or set(snapshot)-{'schema','route','items','instances','settings'}
                or snapshot.get('route')!=template['route'] or not isinstance(snapshot.get('items'),list)
                or not isinstance(snapshot.get('instances',[]),list)):
            unresolved.append({'table':'packaging_templates','rowId':identity,'code':'PERSONAL_TEMPLATE_VERSION_UNSUPPORTED'})
            continue
        for index,item in enumerate(snapshot['items']):
            if (not isinstance(item,dict) or set(item)-item_fields
                    or not current_item('packaging_templates',identity+':item:'+str(index),item)):
                unresolved.append({'table':'packaging_templates','rowId':identity,'code':'PERSONAL_TEMPLATE_REFERENCE_UNRESOLVED'})
        for index,item in enumerate(snapshot.get('instances',[])):
            if (not isinstance(item,dict) or set(item)-rule_fields
                    or not current_item('packaging_templates',identity+':instance:'+str(index),{**item,'slot':item.get('role')})):
                unresolved.append({'table':'packaging_templates','rowId':identity,'code':'PERSONAL_TEMPLATE_REFERENCE_UNRESOLVED'})
    # output_root is a directory instruction, not a signed file. A completed
    # export must be tied to its already-verified registered package folder.
    jobs={j['job_id']:j for j in rows('jobs')}
    for row in rows('job_payloads'):
        job=jobs.get(row['job_id'],{})
        if job.get('job_type')!='build_publish_package':
            continue
        payload=_object(row['payload_json'])
        if not payload.get('output_root'):
            continue
        output=_address(payload['output_root'])
        records=[record for record in rows('publish_records') if record['task_id']==job.get('task_id') and record['candidate_id']==payload.get('candidate_id')]
        if job.get('status') in {'failed','interrupted','cancelled'} or any(_address(record['cover_path']).parent.is_relative_to(output) for record in records):
            done('job_payloads',row['job_id'],'JOB_OUTPUT_DIRECTORY_BINDING_UNRESOLVED')
    return [item for item in unresolved if (item['table'],item['rowId'],item['code']) not in resolved]
