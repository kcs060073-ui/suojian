"""Non-destructive, streaming discovery and identity-safe catalog scans."""

from collections.abc import Callable, Iterator
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .fingerprint import full_sha256, quick_fingerprint
from .libraries import resolve_location
from .models import MediaKind, ScanSummary
from .path_utils import filesystem_path


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS


class LibraryOffline(RuntimeError):
    """Raised when a configured library mount is currently unavailable."""


def classify_media(path: Path) -> MediaKind:
    suffix = path.suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return MediaKind.VIDEO
    if suffix in IMAGE_EXTENSIONS:
        return MediaKind.IMAGE
    if suffix in AUDIO_EXTENSIONS:
        return MediaKind.AUDIO
    return MediaKind.OTHER


def discover_files(root: Path) -> Iterator[Path]:
    def raise_walk_error(error: OSError) -> None:
        raise error

    for directory, child_dirs, filenames in os.walk(root, onerror=raise_walk_error):
        child_dirs.sort(key=str.casefold)
        filenames.sort(key=str.casefold)
        directory_path = Path(directory)
        for filename in filenames:
            path = directory_path / filename
            if path.suffix.lower() in MEDIA_EXTENSIONS:
                yield path


def file_identity(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_dev}:{stat.st_ino}" if stat.st_ino else ""


def scan_library(
    db,
    library_id: str,
    fingerprint_fn: Callable[[Path], str] = quick_fingerprint,
    identity_fn: Callable[[Path], str] = file_identity,
    *,
    start_after: str | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
    on_error: Callable[[str, str], None] | None = None,
) -> ScanSummary:
    library = db.connection.execute(
        "SELECT mount_path FROM libraries WHERE library_id=?",
        (library_id,),
    ).fetchone()
    if library is None:
        raise KeyError(f"找不到素材库：{library_id}")

    root = Path(library["mount_path"])
    scan_root = filesystem_path(root)
    if not scan_root.is_dir():
        _mark_library_offline(db, library_id)
        raise LibraryOffline(
            f"素材目录当前不可访问：{root}。请连接本机硬盘或移动硬盘后重试。"
        )
    connection = db.connection
    connection.execute(
        "CREATE TEMP TABLE IF NOT EXISTS scan_seen(relative_path TEXT PRIMARY KEY)"
    )
    connection.execute("DELETE FROM scan_seen")
    discovered = completed = failed = 0
    resume_pending = bool(
        start_after
        and (scan_root / start_after).is_file()
        and (scan_root / start_after).suffix.lower() in MEDIA_EXTENSIONS
    )

    try:
        for path in discover_files(scan_root):
            relative_path = str(path.relative_to(scan_root))
            connection.execute(
                "INSERT OR IGNORE INTO scan_seen(relative_path) VALUES(?)",
                (relative_path,),
            )
            if resume_pending:
                if relative_path == start_after:
                    resume_pending = False
                continue
            discovered += 1
            try:
                _catalog_path(
                    db,
                    library_id,
                    root,
                    path,
                    relative_path,
                    fingerprint_fn,
                    identity_fn,
                )
                completed += 1
            except (OSError, sqlite3.Error) as error:
                failed += 1
                if on_error is not None:
                    on_error(
                        relative_path,
                        f"{type(error).__name__}: {error}"[-2000:],
                    )
            if on_progress is not None:
                on_progress(relative_path, completed, failed)
    except OSError:
        connection.execute("DELETE FROM scan_seen")
        connection.commit()
        raise

    with db.transaction() as transaction:
        transaction.execute(
            """
            UPDATE asset_locations
            SET state='missing'
            WHERE library_id=?
              AND relative_path NOT IN (SELECT relative_path FROM scan_seen)
            """,
            (library_id,),
        )
        transaction.execute(
            """
            UPDATE libraries
            SET online=1, updated_at=datetime('now')
            WHERE library_id=?
            """,
            (library_id,),
        )
    connection.execute("DELETE FROM scan_seen")
    connection.commit()
    return ScanSummary(discovered=discovered, completed=completed, failed=failed)


def confirm_duplicate_candidate(db, left_asset_id: str, right_asset_id: str) -> str:
    left_hash = _asset_full_hash(db, left_asset_id)
    right_hash = _asset_full_hash(db, right_asset_id)
    status = "confirmed" if left_hash == right_hash else "different"
    left, right = sorted((left_asset_id, right_asset_id))
    if status == "confirmed":
        _merge_assets(db, left, right, left_hash)
        return status
    with db.transaction() as connection:
        changed = connection.execute(
            """
            UPDATE duplicate_candidates
            SET status=?, checked_sha256_at=datetime('now')
            WHERE left_asset_id=? AND right_asset_id=?
            """,
            (status, left, right),
        ).rowcount
    if changed != 1:
        raise KeyError(f"找不到疑似重复记录：{left} / {right}")
    return status


def _merge_assets(
    db,
    canonical_asset_id: str,
    duplicate_asset_id: str,
    full_hash: str,
) -> None:
    with db.transaction() as connection:
        # Never choose between contradictory human decisions implicitly. Check
        # before any mutation so a failed merge preserves both complete records.
        unique_tables = {
            'selection_decisions': ('decision', 'reason', 'decided_by'),
            'packaging_assets': ('kind', 'name', 'source_path', 'notes'),
            'color_params': ('params_json',),
            'task_assets': ('role',),
        }
        for table, fields in unique_tables.items():
            join = 'AND a.task_id=b.task_id' if table == 'task_assets' else ''
            differences = ' OR '.join(f'a.{field} IS NOT b.{field}' for field in fields)
            if connection.execute(
                f'SELECT 1 FROM {table} a JOIN {table} b ON a.asset_id=? '
                f'AND b.asset_id=? {join} WHERE {differences}',
                (canonical_asset_id, duplicate_asset_id),
            ).fetchone():
                raise ValueError(f'ASSET_MERGE_CONFLICT: {table} 存在不同的人工记录，请先统一决定')
        if connection.execute(
            'SELECT 1 FROM privacy_flags a JOIN privacy_flags b '
            'ON a.asset_id=? AND b.asset_id=? AND a.flag_type=b.flag_type '
            'AND a.start_sec IS b.start_sec AND a.end_sec IS b.end_sec AND a.notes IS b.notes '
            'WHERE a.needs_human_confirm=0 AND b.needs_human_confirm=0 AND a.confirmed<>b.confirmed',
            (canonical_asset_id, duplicate_asset_id),
        ).fetchone():
            raise ValueError('ASSET_MERGE_CONFLICT: 同一隐私命中存在相反的人工决定，请先统一决定')

        affected = _affected_tasks(connection, canonical_asset_id) | _affected_tasks(connection, duplicate_asset_id)
        # Membership triggers must see the new locations and presenter binding
        # before task/card references are moved.
        connection.execute('UPDATE asset_locations SET asset_id=? WHERE asset_id=?',
                           (canonical_asset_id, duplicate_asset_id))
        for task in connection.execute('SELECT task_id,brief_json FROM tasks WHERE brief_json IS NOT NULL').fetchall():
            try:
                brief = json.loads(task['brief_json'])
            except (ValueError, TypeError):
                continue
            source = brief.get('digital_human_source') if isinstance(brief, dict) else None
            if isinstance(source, dict) and source.get('asset_id') == duplicate_asset_id:
                from .provenance import canonical_digest, record_gate
                previous_digest = canonical_digest(brief)
                previous_gate = connection.execute(
                    "SELECT gate_digest FROM task_gate_events WHERE task_id=? AND gate_type='brief' ORDER BY rowid DESC LIMIT 1",
                    (task['task_id'],),
                ).fetchone()
                source['asset_id'] = canonical_asset_id
                connection.execute('UPDATE tasks SET brief_json=?,updated_at=datetime(\'now\') WHERE task_id=?',
                                   (json.dumps(brief, ensure_ascii=False), task['task_id']))
                # This is solely an identity migration of full-SHA-equal bytes,
                # not a new human approval. Preserve the original audit event
                # and continue it only when it matched the complete prior brief.
                if previous_gate and previous_gate['gate_digest'] == previous_digest:
                    record_gate(connection, task['task_id'], 'brief', canonical_digest(brief), brief)
        for table in unique_tables:
            extra = f'AND target.task_id={table}.task_id' if table == 'task_assets' else ''
            connection.execute(
                f'DELETE FROM {table} WHERE asset_id=? AND EXISTS '
                f'(SELECT 1 FROM {table} target WHERE target.asset_id=? {extra})',
                (duplicate_asset_id, canonical_asset_id),
            )
            connection.execute(f'UPDATE {table} SET asset_id=? WHERE asset_id=?',
                               (canonical_asset_id, duplicate_asset_id))
        for table in ('segments', 'scene_cards', 'privacy_flags'):
            connection.execute(f'UPDATE {table} SET asset_id=? WHERE asset_id=?',
                               (canonical_asset_id, duplicate_asset_id))
        # Derived caches are interchangeable for identical bytes; keep the
        # canonical kind if both exist, while preserving all non-overlapping kinds.
        connection.execute('DELETE FROM preview_cache WHERE asset_id=? AND kind IN '
                           '(SELECT kind FROM preview_cache WHERE asset_id=?)',
                           (duplicate_asset_id, canonical_asset_id))
        connection.execute('UPDATE preview_cache SET asset_id=? WHERE asset_id=?',
                           (canonical_asset_id, duplicate_asset_id))
        duplicate_runs = connection.execute(
            """
            SELECT run_id, stage, status, tool_version, result_json,
                   error_summary, updated_at
            FROM analysis_runs
            WHERE asset_id=?
            """,
            (duplicate_asset_id,),
        ).fetchall()
        for duplicate_run in duplicate_runs:
            canonical_run = connection.execute(
                """
                SELECT run_id, status
                FROM analysis_runs
                WHERE asset_id=? AND stage=?
                """,
                (canonical_asset_id, duplicate_run["stage"]),
            ).fetchone()
            if canonical_run is None:
                connection.execute(
                    "UPDATE analysis_runs SET asset_id=? WHERE run_id=?",
                    (canonical_asset_id, duplicate_run["run_id"]),
                )
                continue
            if (
                canonical_run["status"] != "completed"
                and duplicate_run["status"] == "completed"
            ):
                connection.execute(
                    """
                    UPDATE analysis_runs
                    SET status=?, tool_version=?, result_json=?,
                        error_summary=?, updated_at=?
                    WHERE run_id=?
                    """,
                    (
                        duplicate_run["status"],
                        duplicate_run["tool_version"],
                        duplicate_run["result_json"],
                        duplicate_run["error_summary"],
                        duplicate_run["updated_at"],
                        canonical_run["run_id"],
                    ),
                )
            connection.execute(
                "DELETE FROM analysis_runs WHERE run_id=?",
                (duplicate_run["run_id"],),
            )
        connection.execute(
            "UPDATE asset_locations SET asset_id=? WHERE asset_id=?",
            (canonical_asset_id, duplicate_asset_id),
        )
        candidates = connection.execute('SELECT * FROM duplicate_candidates WHERE left_asset_id=? OR right_asset_id=?',
                                        (duplicate_asset_id, duplicate_asset_id)).fetchall()
        connection.execute('DELETE FROM duplicate_candidates WHERE left_asset_id=? OR right_asset_id=?',
                           (duplicate_asset_id, duplicate_asset_id))
        for candidate in candidates:
            other = candidate['right_asset_id'] if candidate['left_asset_id'] == duplicate_asset_id else candidate['left_asset_id']
            if other != canonical_asset_id:
                left, right = sorted((canonical_asset_id, other))
                connection.execute('INSERT OR IGNORE INTO duplicate_candidates(left_asset_id,right_asset_id,reason,status,checked_sha256_at) VALUES(?,?,?,?,?)',
                                   (left, right, candidate['reason'], candidate['status'], candidate['checked_sha256_at']))
        connection.execute(
            "UPDATE assets SET full_sha256=? WHERE asset_id=?",
            (full_hash, canonical_asset_id),
        )
        connection.execute(
            "DELETE FROM assets WHERE asset_id=?",
            (duplicate_asset_id,),
        )
        _invalidate_tasks(connection, affected, '素材身份归并，需重新核验选材及下游产物')


def _affected_tasks(connection, asset_id: str) -> set[str]:
    tasks = {r[0] for r in connection.execute(
        'SELECT task_id FROM task_assets WHERE asset_id=? UNION SELECT task_id FROM scene_cards WHERE asset_id=?',
        (asset_id, asset_id))}
    for row in connection.execute('SELECT task_id,brief_json FROM tasks WHERE brief_json IS NOT NULL'):
        try:
            brief = json.loads(row['brief_json'])
        except (ValueError, TypeError):
            continue
        source = brief.get('digital_human_source') if isinstance(brief, dict) else None
        if isinstance(source, dict) and source.get('asset_id') == asset_id:
            tasks.add(row['task_id'])
    return tasks


def _invalidate_tasks(connection, task_ids: set[str], reason: str) -> None:
    from .workflow import invalidate_from_connection
    for task_id in task_ids:
        connection.execute('UPDATE picture_lock SET changed_since_lock=1 WHERE task_id=?', (task_id,))
        connection.execute('UPDATE scene_cards SET confirm_status=0 WHERE task_id=?', (task_id,))
        invalidate_from_connection(connection, task_id, 'brief_lock', reason=reason)


def _invalidate_asset_content(connection, asset_id: str) -> None:
    connection.execute("UPDATE analysis_runs SET status='stale',updated_at=datetime('now') WHERE asset_id=?", (asset_id,))
    connection.execute('DELETE FROM preview_cache WHERE asset_id=?', (asset_id,))
    connection.execute("UPDATE selection_decisions SET decision='candidate' WHERE asset_id=?", (asset_id,))
    connection.execute('UPDATE privacy_flags SET needs_human_confirm=1,confirmed=0 WHERE asset_id=?', (asset_id,))
    connection.execute('UPDATE scene_cards SET segment_id=NULL WHERE asset_id=?', (asset_id,))
    connection.execute('DELETE FROM segments WHERE asset_id=?', (asset_id,))
    _invalidate_tasks(connection, _affected_tasks(connection, asset_id), '源素材发生变化，原分析与人工确认需要重做')


def _catalog_path(
    db,
    library_id: str,
    root: Path,
    path: Path,
    relative_path: str,
    fingerprint_fn: Callable[[Path], str],
    identity_fn: Callable[[Path], str],
) -> None:
    stat = path.stat()
    identity = identity_fn(path)
    same_path = db.connection.execute(
        """
        SELECT
          al.location_id,
          al.asset_id,
          al.mtime_ns,
          al.file_identity,
          a.size_bytes,
          a.quick_fingerprint,
          a.full_sha256
        FROM asset_locations al
        JOIN assets a USING(asset_id)
        WHERE al.library_id=? AND al.relative_path=?
        """,
        (library_id, relative_path),
    ).fetchone()

    identity_changed = bool(
        same_path
        and same_path["file_identity"]
        and identity
        and same_path["file_identity"] != identity
    )
    if same_path and not identity_changed:
        metadata_unchanged = (
            same_path["mtime_ns"] == stat.st_mtime_ns
            and same_path["size_bytes"] == stat.st_size
        )
        if metadata_unchanged:
            _mark_location_available(
                db,
                same_path["location_id"],
                stat.st_mtime_ns,
                identity,
            )
            return
        fingerprint = fingerprint_fn(path)
        _refresh_existing_asset(db, same_path, stat, identity, fingerprint, path)
        return

    if identity:
        existing_identity = db.connection.execute(
            """
            SELECT location_id, asset_id
            FROM asset_locations
            WHERE library_id=? AND file_identity=?
            """,
            (library_id, identity),
        ).fetchone()
        if existing_identity:
            with db.transaction() as connection:
                connection.execute(
                    """
                    UPDATE asset_locations
                    SET relative_path=?, mtime_ns=?, state='available'
                    WHERE location_id=?
                    """,
                    (relative_path, stat.st_mtime_ns, existing_identity["location_id"]),
                )
            return

    fingerprint = fingerprint_fn(path)
    asset_id = _create_asset(db, path, stat.st_size, fingerprint)
    with db.transaction() as connection:
        if same_path and identity_changed:
            _invalidate_tasks(connection, _affected_tasks(connection, same_path['asset_id']), '源文件被替换，需重新选材')
            connection.execute(
                """
                UPDATE asset_locations
                SET asset_id=?, mtime_ns=?, file_identity=?, state='available'
                WHERE location_id=?
                """,
                (asset_id, stat.st_mtime_ns, identity, same_path["location_id"]),
            )
        else:
            connection.execute(
                """
                INSERT INTO asset_locations(
                  asset_id, library_id, relative_path, mtime_ns, file_identity, state
                ) VALUES(?, ?, ?, ?, ?, 'available')
                """,
                (asset_id, library_id, relative_path, stat.st_mtime_ns, identity),
            )


def _mark_location_available(
    db,
    location_id: int,
    mtime_ns: int,
    identity: str,
) -> None:
    with db.transaction() as connection:
        connection.execute(
            """
            UPDATE asset_locations
            SET mtime_ns=?, file_identity=?, state='available'
            WHERE location_id=?
            """,
            (mtime_ns, identity, location_id),
        )


def _refresh_existing_asset(db, row, stat, identity: str, fingerprint: str, path: Path) -> None:
    changed = (
        row["mtime_ns"] != stat.st_mtime_ns
        or row["size_bytes"] != stat.st_size
        or row["quick_fingerprint"] != fingerprint
    )
    if (changed and row['size_bytes'] == stat.st_size
            and row['quick_fingerprint'] == fingerprint and row['full_sha256']):
        changed = full_sha256(path) != row['full_sha256']
    with db.transaction() as connection:
        asset_id = row['asset_id']
        if changed and connection.execute('SELECT COUNT(*) FROM asset_locations WHERE asset_id=?', (asset_id,)).fetchone()[0] > 1:
            # A location now denotes different bytes. Existing selections continue
            # to refer to the unchanged copies, never to this new generation.
            new_id = 'ast_' + uuid.uuid4().hex
            connection.execute('INSERT INTO assets(asset_id,media_kind,size_bytes,quick_fingerprint,created_at) '
                               'SELECT ?,media_kind,?,?,datetime(\'now\') FROM assets WHERE asset_id=?',
                               (new_id, stat.st_size, fingerprint, asset_id))
            connection.execute('UPDATE asset_locations SET asset_id=? WHERE location_id=?', (new_id, row['location_id']))
            _invalidate_tasks(connection, _affected_tasks(connection, asset_id), '素材副本内容分叉，需核验任务范围')
            asset_id = new_id
        connection.execute(
            """
            UPDATE asset_locations
            SET mtime_ns=?, file_identity=?, state='available'
            WHERE location_id=?
            """,
            (stat.st_mtime_ns, identity, row["location_id"]),
        )
        if changed:
            connection.execute(
                """
                UPDATE assets
                SET size_bytes=?, quick_fingerprint=?, full_sha256=NULL
                WHERE asset_id=?
                """,
                (stat.st_size, fingerprint, asset_id),
            )
            _invalidate_asset_content(connection, asset_id)


def _create_asset(db, path: Path, size: int, fingerprint: str) -> str:
    asset_id = "ast_" + uuid.uuid4().hex
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO assets(
              asset_id, media_kind, size_bytes, quick_fingerprint, created_at
            ) VALUES(?, ?, ?, ?, datetime('now'))
            """,
            (asset_id, classify_media(path).value, size, fingerprint),
        )
        matches = connection.execute(
            """
            SELECT asset_id
            FROM assets
            WHERE size_bytes=? AND quick_fingerprint=? AND asset_id<>?
            """,
            (size, fingerprint, asset_id),
        ).fetchall()
        for match in matches:
            left, right = sorted((match["asset_id"], asset_id))
            connection.execute(
                """
                INSERT OR IGNORE INTO duplicate_candidates(
                  left_asset_id, right_asset_id, reason
                ) VALUES(?, ?, 'quick_fingerprint')
                """,
                (left, right),
            )
    return asset_id


def _asset_full_hash(db, asset_id: str) -> str:
    row = db.connection.execute(
        "SELECT full_sha256 FROM assets WHERE asset_id=?",
        (asset_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"找不到素材：{asset_id}")
    if row["full_sha256"]:
        return row["full_sha256"]
    digest = full_sha256(resolve_location(db, asset_id))
    with db.transaction() as connection:
        connection.execute(
            "UPDATE assets SET full_sha256=? WHERE asset_id=?",
            (digest, asset_id),
        )
    return digest


def _mark_library_offline(db, library_id: str) -> None:
    with db.transaction() as connection:
        connection.execute(
            """
            UPDATE libraries
            SET online=0, updated_at=datetime('now')
            WHERE library_id=?
            """,
            (library_id,),
        )
        connection.execute(
            "UPDATE asset_locations SET state='offline' WHERE library_id=?",
            (library_id,),
        )
