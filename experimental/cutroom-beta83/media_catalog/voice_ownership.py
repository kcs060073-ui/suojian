"""Select voice ownership from server-owned state; never verify or activate audio.

``managed`` requires the caller to verify the immutable bundle, actual SHA,
measured timing, upstream context and downstream gates. ``legacy`` retains its
existing checks. This module reads no media, credentials, requests or drafts.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
import re
import sqlite3
from typing import Literal


@dataclass(frozen=True)
class OwnershipDecision:
    branch: Literal['legacy', 'managed', 'blocked']
    reason: str
    revision: int | None = None

    @property
    def requires_bundle_verification(self) -> bool:
        return self.branch == 'managed'


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('nonfinite JSON')


def _payload(row, name):
    missing = object()
    value = row.get(name, missing)
    serialized = row.get(name + '_json', missing)
    if serialized is not missing:
        if serialized is not None:
            if not isinstance(serialized, str):
                raise ValueError('JSON type')
            serialized = json.loads(serialized, object_pairs_hook=_object_pairs, parse_constant=_invalid_constant)
        if value is not missing and _canonical(value) != _canonical(serialized):
            raise ValueError('ambiguous payload')
        value = serialized
    if value is missing or (value is not None and not isinstance(value, Mapping)):
        raise ValueError('missing or invalid payload')
    _canonical(value)
    return value


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _normalize(row, task_id):
    if not isinstance(row, Mapping) or row.get('task_id') != task_id:
        raise ValueError('task mismatch')
    revision = row.get('revision')
    if type(revision) is not int or revision < 0 or not _nonempty(row.get('context_digest')):
        raise ValueError('invalid version')
    config = _payload(row, 'config')
    if config is None or not _nonempty(config.get('script')):
        raise ValueError('invalid config')
    unsigned = {k: v for k, v in config.items() if k != 'fingerprint'}
    if (not _sha(config.get('fingerprint'))
            or hashlib.sha256(_canonical(unsigned).encode()).hexdigest() != config['fingerprint']
            or hashlib.sha256(config['script'].encode()).hexdigest() != config.get('script_sha256')):
        raise ValueError('invalid config identity')
    return dict(task_id=task_id, revision=revision, context_digest=row['context_digest'],
                config=config, audio=_payload(row, 'audio'), reason=row.get('reason'))


def _audio_identity_valid(row):
    audio = row['audio']
    if not isinstance(audio, Mapping) or not _sha(audio.get('sha256')):
        return False
    path = audio.get('path')
    if not _nonempty(path) or path in ('.', '..'):
        return False
    windows, posix = PureWindowsPath(path), PurePosixPath(path)
    return (bool(windows.parts) and bool(posix.parts)
            and not windows.drive and not windows.root and not posix.root
            and '..' not in windows.parts and '..' not in posix.parts
            and audio.get('fingerprint') == row['config']['fingerprint'])


def _binding_matches(binding, task_id, head):
    required = {'revision', 'fingerprint', 'audio_sha256'}
    allowed = required | {'timeline_digest', 'context_digest', 'task_id'}
    if (not isinstance(binding, Mapping) or not required.issubset(binding)
            or not set(binding).issubset(allowed) or type(binding['revision']) is not int
            or not _sha(binding['fingerprint']) or not _sha(binding['audio_sha256'])):
        return False
    if (binding['revision'] != head['revision']
            or binding['fingerprint'] != head['config']['fingerprint']
            or binding['audio_sha256'] != head['audio']['sha256']):
        return False
    if 'task_id' in binding and binding['task_id'] != task_id:
        return False
    if 'context_digest' in binding and binding['context_digest'] != head['context_digest']:
        return False
    if 'timeline_digest' in binding:
        if not _sha(binding['timeline_digest']) or binding['timeline_digest'] != head['audio'].get('timeline_digest'):
            return False
    return True


def select_voice_ownership(task_id, head, versions, *, context_digest=None, trusted_binding=None):
    """Choose a branch using complete server-owned history, never browser flags.

    Rows may contain decoded config/audio, raw *_json, or agreeing copies of
    both. A supplied binding means a CURRENT trusted managed downstream binding,
    not an old invalidated gate. No binding is synthesized from browser input.
    """
    blocked = OwnershipDecision('blocked', 'LEDGER_INVALID')
    try:
        if not _nonempty(task_id) or not isinstance(versions, Sequence) or isinstance(versions, (str, bytes)):
            return blocked
        if head is None:
            if versions or trusted_binding is not None:
                return blocked
            return OwnershipDecision('legacy', 'NO_EDIT')
        current = _normalize(head, task_id)
        history = sorted((_normalize(v, task_id) for v in versions), key=lambda v: v['revision'])
        if len(history) != current['revision'] + 1:
            return blocked
        if any(v['revision'] != index for index, v in enumerate(history)):
            return blocked
        if not history or history[0]['reason'] != 'initial':
            return blocked
        activated = False
        for index, version in enumerate(history[1:], 1):
            reason, previous = version['reason'], history[index - 1]
            if reason == 'upstream_rebind':
                if version['context_digest'] == previous['context_digest']:
                    return blocked
                if version['config']['fingerprint'] != previous['config']['fingerprint'] and version['audio'] is not None:
                    return blocked
            elif reason == 'voice_apply' or (isinstance(reason, str) and re.fullmatch(r'restore:[0-9]+', reason)):
                activated = True
                if (version['context_digest'] != previous['context_digest']
                        or version['config']['script_sha256'] != previous['config']['script_sha256']):
                    return blocked
                if not _audio_identity_valid(version):
                    return blocked
                if reason != 'voice_apply':
                    target = int(reason.split(':')[1])
                    if (target >= index or history[target]['context_digest'] != version['context_digest']
                            or _canonical(history[target]['config']) != _canonical(version['config'])
                            or not _audio_identity_valid(history[target])
                            or _canonical({k: v for k, v in history[target]['audio'].items() if k != 'duration'})
                            != _canonical({k: v for k, v in version['audio'].items() if k != 'duration'})):
                        return blocked
            else:
                return blocked
        latest = history[-1]
        if any(_canonical(current[k]) != _canonical(latest[k]) for k in ('context_digest', 'config', 'audio')):
            return blocked
        if not activated:
            if trusted_binding is not None:
                return OwnershipDecision('blocked', 'BINDING_CONFLICT', current['revision'])
            return OwnershipDecision('legacy', 'EDIT_ONLY', current['revision'])
        if not _nonempty(context_digest) or context_digest != current['context_digest']:
            return OwnershipDecision('blocked', 'CURRENT_CONTEXT_INVALID', current['revision'])
        if not _audio_identity_valid(current):
            return OwnershipDecision('blocked', 'CURRENT_AUDIO_INVALID', current['revision'])
        if trusted_binding is not None and not _binding_matches(trusted_binding, task_id, current):
            return OwnershipDecision('blocked', 'BINDING_CONFLICT', current['revision'])
        return OwnershipDecision('managed', 'FORMAL_APPLICATION', current['revision'])
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return blocked


def read_voice_ownership(db_or_connection, task_id, *, context_digest=None, trusted_binding=None):
    """Read only caller-provided SQLite state. Never open, migrate or commit a DB.

    Schema absence is unknown ownership, not proof of legacy. One compound
    SELECT reads head and versions in the same SQLite statement snapshot, and
    leaves any caller-owned transaction and row factory untouched.
    """
    try:
        connection = getattr(db_or_connection, 'connection', db_or_connection)
        required = {'task_id', 'revision', 'context_digest', 'config_json', 'audio_json'}
        for table, extra in (('voice_edit_heads', set()), ('voice_edit_versions', {'reason'})):
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info({table})').fetchall()}
            if not (required | extra).issubset(columns):
                return OwnershipDecision('blocked', 'SCHEMA_UNAVAILABLE')
        cursor = connection.execute(
            "SELECT 'head' AS row_kind,task_id,revision,context_digest,config_json,audio_json,NULL AS reason "
            "FROM voice_edit_heads WHERE task_id=? UNION ALL "
            "SELECT 'version',task_id,revision,context_digest,config_json,audio_json,reason "
            "FROM voice_edit_versions WHERE task_id=?", (task_id, task_id))
        fields = [column[0] for column in cursor.description]
        rows = [dict(zip(fields, row)) for row in cursor.fetchall()]
        heads = [row for row in rows if row['row_kind'] == 'head']
        if len(heads) > 1:
            return OwnershipDecision('blocked', 'LEDGER_INVALID')
        return select_voice_ownership(task_id, heads[0] if heads else None,
                                     [row for row in rows if row['row_kind'] == 'version'],
                                     context_digest=context_digest, trusted_binding=trusted_binding)
    except (sqlite3.DatabaseError, AttributeError, TypeError):
        return OwnershipDecision('blocked', 'DATABASE_UNAVAILABLE')
