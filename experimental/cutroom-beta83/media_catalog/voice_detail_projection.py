"""Bounded display-only memo of verified voice provenance.

Every read hashes every registered dependency and rereads every ancestor record.
A metadata stamp alone is never a hit. Playback, apply, render and export keep
using the full reader; this module only projects the task-detail playback link.
"""
from collections import OrderedDict
import json
from pathlib import Path
import re
from threading import Lock
from .voice_editing import _digest
from .voice_registered_reuse import _snapshot
from .voice_track_segments import _safe, _sha

_CACHE = OrderedDict()
_LOCK = Lock()
_LIMIT = 16


def _records(db, task_id, bundle_id):
    chain=[]; seen=set()
    while bundle_id:
        if bundle_id in seen or len(seen)>=16:
            raise ValueError('VOICE_REUSE_ANCESTRY_LIMIT')
        seen.add(bundle_id)
        record=_snapshot(db,task_id,bundle_id)['bundle']
        if record is None: raise ValueError('VOICE_BUNDLE_UNREGISTERED')
        chain.append(record)
        private=json.loads(record['input_json'])
        bundle_id=private.get('source_bundle_id') if private.get('schema_version')==2 else None
    return chain


def _proof(db, root, task_id, audio, config, context_digest, *, verify_files=False):
    from .voice_bundle_service import _service
    if not isinstance(audio,dict) or not audio.get('bundle_id'):
        raise ValueError('VOICE_BUNDLE_UNREGISTERED')
    audio_root=_service(db,root,task_id).audio_root
    chain=_records(db,task_id,audio['bundle_id']); inventory={}
    for record in chain:
        files=json.loads(record['files_json'])
        if not isinstance(files,list) or not 1<=len(files)<=405:
            raise ValueError('VOICE_BUNDLE_CORRUPT')
        for entry in files:
            raw=Path(entry['path']); sha=entry['sha256']
            if (raw.is_absolute() or raw.drive or '..' in raw.parts or not raw.parts
                    or not isinstance(sha,str) or re.fullmatch('[0-9a-f]{64}',sha) is None):
                raise ValueError('VOICE_BUNDLE_CORRUPT')
            if raw in inventory and inventory[raw]!=sha:raise ValueError('VOICE_BUNDLE_CORRUPT')
            inventory[raw]=sha
    if verify_files:
        for raw,expected in inventory.items():
            if _sha(_safe(audio_root/raw,audio_root))!=expected:
                raise ValueError('VOICE_BUNDLE_CORRUPT')
    if chain!=_records(db,task_id,audio['bundle_id']):
        raise ValueError('VOICE_BUNDLE_BINDING_INVALID')
    return _digest(dict(root=str(audio_root),task_id=task_id,audio=audio,config=config,
                        context_digest=context_digest,records=chain))


def read_voice_projection(db, root, task_id, audio, config, context_digest):
    # Cache only successful full verification. Exceptions never become entries.
    key=_proof(db,root,task_id,audio,config,context_digest)
    with _LOCK:
        path=_CACHE.get(key)
        if path is not None:_CACHE.move_to_end(key)
    if path is not None:
        if _proof(db,root,task_id,audio,config,context_digest,verify_files=True)!=key:
            raise ValueError('VOICE_BUNDLE_BINDING_INVALID')
        return {'path':path}
    from .voice_bundle_service import read_registered_voice
    checked=read_registered_voice(db,root,task_id,audio,config,context_digest)
    if _proof(db,root,task_id,audio,config,context_digest)!=key:
        raise ValueError('VOICE_BUNDLE_BINDING_INVALID')
    with _LOCK:
        _CACHE[key]=checked['path'];_CACHE.move_to_end(key)
        while len(_CACHE)>_LIMIT:_CACHE.popitem(last=False)
    return {'path':checked['path']}
