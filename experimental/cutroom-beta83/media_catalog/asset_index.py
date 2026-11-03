"""Read-only material index for the colleague workbench."""

from __future__ import annotations

from collections import OrderedDict
import json
from functools import wraps
import math
import os
import sqlite3
import stat
import sys
import threading
from urllib.parse import quote
from pathlib import Path

from .path_utils import filesystem_path


MEDIA_KINDS = {"video", "image", "audio", "other"}
AVAILABILITY_STATES = {"available", "offline", "missing"}
AUTHORIZATION_STATES = {"approved", "review_required", "unknown"}
ANALYSIS_STATES = {"observed", "stale", "unobserved", "not_applicable"}
SHOT_PURPOSES = {"task_specific", "generic"}
MAX_PAGE_SIZE = 200


def _normalized_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _duration_seconds(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        value = payload.get("duration_seconds")
        if value is None and isinstance(payload.get("format"), dict):
            value = payload["format"].get("duration")
        duration = float(value)
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return duration if math.isfinite(duration) and duration >= 0 else None


def with_technical_durations(db, items: list[dict]) -> list[dict]:
    """Attach measured video lengths to task-scoped visual candidate rows."""
    video_ids = [item["asset_id"] for item in items if item.get("media_kind") == "video"]
    if not video_ids:
        return items
    placeholders = ",".join("?" for _ in video_ids)
    rows = db.connection.execute(
        "SELECT asset_id,result_json FROM analysis_runs "
        "WHERE stage='technical_probe' AND status='completed' "
        f"AND asset_id IN ({placeholders})",
        tuple(video_ids),
    ).fetchall()
    durations = {row["asset_id"]: _duration_seconds(row["result_json"]) for row in rows}
    return [
        {**item, "duration_seconds": durations.get(item["asset_id"])}
        if item.get("media_kind") == "video" else item
        for item in items
    ]


def _preview_url(task_id: str | None, asset_id: str, availability: str,
                 media_kind: str) -> str | None:
    if availability != "available" or media_kind not in {"video", "image", "audio"}:
        return None
    if task_id is None:
        return "/api/assets/{}/preview".format(quote(asset_id, safe=""))
    return "/api/tasks/{}/rough-media/{}/preview".format(
        quote(task_id, safe=""),
        quote(asset_id, safe=""),
    )


def _live_availability_reader():
    """Keep one lightweight filesystem observation per location for this query."""
    roots = {}
    parents = {}
    directories = {}
    files = {}

    def read(mount_path, relative_path, state, online, *, scan_directory=True):
        if state == "missing":
            return "missing"
        if state == "offline" or not online:
            return "offline"
        if mount_path not in roots:
            try:
                root = Path(mount_path).resolve()
                native_root = filesystem_path(root)
                roots[mount_path] = (root, native_root) if native_root.is_dir() else None
            except (OSError, ValueError, RuntimeError):
                roots[mount_path] = None
        root_pair = roots[mount_path]
        if root_pair is None:
            return "offline"
        root, native_root = root_pair
        key = (mount_path, relative_path)
        if key not in files:
            try:
                if (relative_path not in {"", ".", ".."}
                        and not any(character in relative_path for character in ("/", "\\", ":"))):
                    # A plain filename cannot escape its already resolved root.
                    # Keep full Path validation for every compound/drive path.
                    leaf_name = relative_path
                    parent_key = (mount_path, ".")
                    parents[parent_key] = root_pair
                else:
                    relative = Path(relative_path)
                    if relative.is_absolute() or relative.drive or ".." in relative.parts:
                        files[key] = "missing"
                        return files[key]
                    leaf_name = relative.name
                    parent_key = (mount_path, str(relative.parent))
                    if parent_key not in parents:
                        if relative.parent == Path("."):
                            parents[parent_key] = root_pair
                        else:
                            parent = (root / relative.parent).resolve()
                            parents[parent_key] = (
                                (parent, filesystem_path(parent))
                                if parent.is_relative_to(root) else None
                            )
                parent_pair = parents[parent_key]
                if parent_pair is None:
                    files[key] = "missing"
                    return files[key]
                parent, native_parent = parent_pair
                if not scan_directory and parent_key not in directories:
                    # A page already has its selected locations. Reading their
                    # metadata directly avoids scanning a large shared folder.
                    # lstat preserves the symlink/reparse containment check below.
                    metadata = (native_parent / leaf_name).lstat()
                else:
                    if parent_key not in directories:
                        # A flat OS directory observation avoids one Windows stat
                        # round trip per matching asset. It never registers entries,
                        # recurses, probes media, or survives this query.
                        entries = {}
                        try:
                            with os.scandir(native_parent) as iterator:
                                for entry in iterator:
                                    try:
                                        entries[os.path.normcase(entry.name)] = entry.stat(follow_symlinks=False)
                                    except OSError as error:
                                        entries[os.path.normcase(entry.name)] = error
                            directories[parent_key] = entries
                        except OSError as error:
                            directories[parent_key] = error
                    entries = directories[parent_key]
                    if isinstance(entries, OSError):
                        raise entries
                    metadata = entries.get(os.path.normcase(leaf_name))
                    if metadata is None:
                        files[key] = "missing"
                        return files[key]
                if isinstance(metadata, OSError):
                    raise metadata
                reparse = getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                if reparse or stat.S_ISLNK(metadata.st_mode):
                    resolved = (parent / leaf_name).resolve()
                    present = resolved.is_relative_to(root) and filesystem_path(resolved).is_file()
                else:
                    present = stat.S_ISREG(metadata.st_mode)
                files[key] = "available" if present else "missing"
            except (FileNotFoundError, NotADirectoryError):
                files[key] = "missing"
            except (OSError, ValueError, RuntimeError):
                files[key] = "offline"
        return files[key]

    return read


def _default_catalog_page_sql(projected_availability_sql, multiple_location_join,
                              authorization_sql, availability_sql):
    """Select and project one default page in a single SQLite read snapshot."""
    return f"""
      WITH ranked AS (
        SELECT asset.asset_id, asset.media_kind, asset.size_bytes,
               asset.quick_fingerprint, asset.full_sha256,
               library.library_id, library.name AS library_name,
               library.online AS library_online,
               location.relative_path, location.location_id, library.mount_path,
               location.state AS location_state,
               {projected_availability_sql} AS availability,
               {authorization_sql} AS authorization_status,
               ROW_NUMBER() OVER (
                 PARTITION BY asset.asset_id
                 ORDER BY CASE {projected_availability_sql}
                   WHEN 'available' THEN 0 WHEN 'offline' THEN 1 ELSE 2 END,
                   location.relative_path, location.location_id
               ) AS asset_rank
        FROM assets asset
        JOIN asset_locations location USING(asset_id)
        JOIN libraries library USING(library_id)
        {multiple_location_join}
        LEFT JOIN self_produced_asset_authorizations own ON own.asset_id=asset.asset_id
      ), selected_page AS MATERIALIZED (
        SELECT * FROM ranked WHERE asset_rank=1
        ORDER BY lower(relative_path), asset_id
        LIMIT ? OFFSET ?
      ), totals AS MATERIALIZED (
        SELECT COUNT(DISTINCT CASE
          WHEN EXISTS(SELECT 1 FROM selected_page) THEN asset.asset_id
          WHEN {availability_sql} IN ('available','offline','missing')
            THEN asset.asset_id END) AS total_count
        FROM assets asset
        JOIN asset_locations location USING(asset_id)
        JOIN libraries library USING(library_id)
      ), obs AS (
        SELECT observation.asset_id, COUNT(*) AS total_count,
               COUNT(DISTINCT CASE
                 WHEN observation.asset_fingerprint=current_asset.quick_fingerprint
                 AND NOT EXISTS (
                   SELECT 1 FROM catalog_segment_marks edit
                   WHERE edit.asset_id=observation.asset_id
                     AND edit.asset_fingerprint=current_asset.quick_fingerprint
                     AND edit.base_start_us=observation.source_start_us
                     AND edit.base_end_us=observation.source_end_us
                 )
                 THEN printf('%d:%d',observation.source_start_us,observation.source_end_us)
               END) AS current_count
        FROM visual_observations observation
        JOIN assets current_asset ON current_asset.asset_id=observation.asset_id
        WHERE observation.asset_id IN (SELECT asset_id FROM selected_page)
        GROUP BY observation.asset_id
      ), marked AS (
        SELECT mark.asset_id, COUNT(*) AS total_count,
               COUNT(CASE WHEN mark.asset_fingerprint=current_asset.quick_fingerprint
                          THEN 1 END) AS current_count
        FROM catalog_segment_marks mark
        JOIN assets current_asset ON current_asset.asset_id=mark.asset_id
        WHERE mark.asset_id IN (SELECT asset_id FROM selected_page)
        GROUP BY mark.asset_id
      )
      SELECT selected_page.*, totals.total_count,
             probe.result_json AS probe_result_json,
             CASE
               WHEN selected_page.media_kind NOT IN ('video','image') THEN 'not_applicable'
               WHEN COALESCE(obs.current_count,0)+COALESCE(marked.current_count,0)>0 THEN 'observed'
               WHEN COALESCE(obs.total_count,0)+COALESCE(marked.total_count,0)>0 THEN 'stale'
               ELSE 'unobserved' END AS analysis_status,
             COALESCE(obs.current_count,0)+COALESCE(marked.current_count,0) AS segment_count
      FROM totals LEFT JOIN selected_page ON 1=1
      LEFT JOIN analysis_runs probe
        ON probe.asset_id=selected_page.asset_id
       AND probe.stage='technical_probe' AND probe.status='completed'
      LEFT JOIN obs ON obs.asset_id=selected_page.asset_id
      LEFT JOIN marked ON marked.asset_id=selected_page.asset_id
      ORDER BY lower(selected_page.relative_path), selected_page.asset_id
    """


def _snapshot_member_projection(raw):
    """Pure canonical-JSON projection; malformed inputs retain SQLite semantics."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(value)

    try:
        document = json.loads(raw, object_pairs_hook=unique_object,
                              parse_constant=reject_constant)
        members = document["assets"]
        if not isinstance(members, list):
            raise ValueError("non-array members")
        grouped = {}
        for member in members:
            if not isinstance(member, dict) or not isinstance(member.get("asset_id"), str):
                raise ValueError("non-string asset ID")
            authorization = member.get("authorization_status")
            if authorization is not None and not isinstance(authorization, str):
                raise ValueError("non-string authorization")
            encoded_id = json.encoder.encode_basestring_ascii(member["asset_id"])
            encoded_authorization = ("null" if authorization is None else
                json.encoder.encode_basestring_ascii(authorization))
            grouped.setdefault(member["asset_id"], []).append(
                '{"asset_id":' + encoded_id + ',"authorization_status":'
                + encoded_authorization + '}'
            )
        # Preserve duplicate member order and the original two-field projection.
        return {key: '{"assets":[' + ','.join(values) + ']}'
                for key, values in grouped.items()}
    except (ValueError, TypeError, KeyError, RecursionError):
        return raw


class _TaskSnapshotProjectionCache:
    """Bounded exact-text memoization, never a scope or availability decision.

    Cached dictionaries are private and read-only by convention; SQL callbacks
    expose only their immutable JSON strings. Parsing stays outside the lock.
    The byte budget bounds retained cache entries, not transient parser objects
    or projections still referenced by concurrent in-flight queries.
    """
    def __init__(self, *, max_entries=4, max_bytes=32 * 1024 * 1024):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.entries = OrderedDict()
        self.lock = threading.Lock()
        # Conservative allowance for the cache, lock and empty OrderedDict.
        self.retained_bytes = 1024

    @staticmethod
    def _entry_bytes(raw, projected):
        # Count the actual Python string/dict allocations, not UTF-8 length.
        # 512 covers the OrderedDict node/hash slot, tuple and size metadata.
        count = sys.getsizeof(raw) + 512
        if projected is not raw:
            count += sys.getsizeof(projected)
            if isinstance(projected, dict):
                count += sum(sys.getsizeof(key) + sys.getsizeof(value)
                             for key, value in projected.items())
        return count

    def get(self, raw):
        if not isinstance(raw, str):
            return _snapshot_member_projection(raw)
        with self.lock:
            cached = self.entries.get(raw)
            if cached is not None:
                self.entries.move_to_end(raw)
                return cached[0]
        projected = _snapshot_member_projection(raw)
        count = self._entry_bytes(raw, projected)
        with self.lock:
            # Another query may have filled the identical text during parsing.
            cached = self.entries.get(raw)
            if cached is not None:
                self.entries.move_to_end(raw)
                return cached[0]
            if self.max_entries < 1 or count + 1024 > self.max_bytes:
                return projected
            while self.entries and (len(self.entries) >= self.max_entries or
                                    self.retained_bytes + count > self.max_bytes):
                _, (_, removed_bytes) = self.entries.popitem(last=False)
                self.retained_bytes -= removed_bytes
            self.entries[raw] = (projected, count)
            self.retained_bytes += count
        return projected


_TASK_SNAPSHOT_PROJECTIONS = _TaskSnapshotProjectionCache()


def _task_snapshot_member_reader(connection):
    """Read current raw in this SQL statement; reuse only its pure projection.

    SQL still chooses the active scope, latest snapshot, path and authorization.
    Row IDs are memoized only within this query and are never cross-request keys.
    Every new reader reads current raw before consulting the bounded text cache.
    """
    snapshots = {}

    def read(snapshot_rowid, asset_id):
        if snapshot_rowid not in snapshots:
            row = connection.execute(
                "SELECT summary_json FROM task_source_snapshots WHERE rowid=?",
                (snapshot_rowid,),
            ).fetchone()
            raw = None if row is None else row[0]
            snapshots[snapshot_rowid] = _TASK_SNAPSHOT_PROJECTIONS.get(raw)
        cached = snapshots[snapshot_rowid]
        return cached.get(asset_id, '{"assets":[]}') if isinstance(cached, dict) else cached

    return read


def _task_snapshot_member_sql() -> tuple[str, str]:
    # Capture metadata once, then validate eligibility without evaluating invalid
    # JSON from a scope which the actual library/path/filters may never visit.
    cte = """WITH task_snapshots AS MATERIALIZED (
      SELECT active.scope_id, snapshot.rowid AS snapshot_rowid,
             snapshot.summary_json
      FROM task_source_scopes active
      JOIN task_source_snapshots snapshot
        ON snapshot.rowid=(
          SELECT latest.rowid FROM task_source_snapshots latest
          WHERE latest.scope_id=active.scope_id
          ORDER BY latest.created_at DESC,latest.rowid DESC LIMIT 1
        )
      WHERE active.task_id=? AND active.active=1
    ), task_snapshot_roots AS MATERIALIZED (
      SELECT scope_id, snapshot_rowid, summary_json,
        CASE WHEN json_valid(summary_json) THEN
          CASE WHEN json_type(summary_json,'$.assets')='array' THEN
            NOT EXISTS (
              SELECT 1 FROM json_each(summary_json,'$.assets') element
              WHERE element.type<>'object'
            )
          ELSE 0 END
        ELSE 0 END AS cache_eligible
      FROM task_snapshots
    ), task_snapshot_members AS MATERIALIZED (
      SELECT root.scope_id, root.snapshot_rowid, member.value,
             CAST(CASE WHEN member.type='object' THEN
               json_extract(member.value,'$.asset_id')
             END AS TEXT) AS asset_id,
             CAST(member.key AS INTEGER) AS member_order
      FROM task_snapshot_roots root
      JOIN json_each(
        CASE WHEN root.cache_eligible THEN root.summary_json ELSE '{}' END,
        '$.assets'
      ) member
      WHERE root.cache_eligible=1
    )
    """
    # Keep the original scope/latest-snapshot join and json_extract ON. Rebuild
    # each matching bag in its original array order; never merge duplicate IDs.
    # Non-array/mixed/invalid sources retain both original input and JSON path.
    member_input = """json_each(
          CASE WHEN (
            SELECT cache_eligible FROM task_snapshot_roots root
            WHERE root.scope_id=scope.scope_id
              AND root.snapshot_rowid=snapshot.rowid
          )=1 THEN (
            SELECT json_group_array(value) FROM (
              SELECT matched.value FROM task_snapshot_members matched
              WHERE matched.scope_id=scope.scope_id
                AND matched.snapshot_rowid=snapshot.rowid
                AND matched.asset_id=asset.asset_id
              ORDER BY matched.member_order
            )
          ) ELSE snapshot.summary_json END,
          CASE WHEN (
            SELECT cache_eligible FROM task_snapshot_roots root
            WHERE root.scope_id=scope.scope_id
              AND root.snapshot_rowid=snapshot.rowid
          )=1 THEN '$' ELSE '$.assets' END
        ) snapshot_asset"""
    return cte, member_input


def _with_read_snapshot(function):
    """Keep a list response coherent without taking ownership of caller writes."""
    @wraps(function)
    def read(db, *args, **kwargs):
        connection = db.connection
        owned = not connection.in_transaction
        if owned:
            connection.execute("BEGIN DEFERRED")
        try:
            return function(db, *args, **kwargs)
        finally:
            if owned:
                # This function only reads. End its snapshot without committing
                # any caller transaction (which we never enter this branch for).
                connection.rollback()
    return read


@_with_read_snapshot
def list_asset_index(
    db,
    *,
    task_id: str | None = None,
    query: str | None = None,
    library_id: str | None = None,
    purpose: str | None = None,
    media_kind: str | None = None,
    availability: str | None = None,
    authorization: str | None = None,
    analysis_status: str | None = None,
    asset_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Return one preferred catalog location per matching asset.

    A task-scoped query is constrained to that task's active source scopes,
    including unavailable locations so colleagues can diagnose missing media.
    """

    task_id = _normalized_optional(task_id)
    query = _normalized_optional(query)
    library_id = _normalized_optional(library_id)
    purpose = _normalized_optional(purpose)
    media_kind = _normalized_optional(media_kind)
    availability = _normalized_optional(availability)
    authorization = _normalized_optional(authorization)
    analysis_status = _normalized_optional(analysis_status)
    asset_id = _normalized_optional(asset_id)
    if media_kind is not None and media_kind not in MEDIA_KINDS:
        raise ValueError("media_kind 过滤无效")
    if purpose is not None and purpose not in SHOT_PURPOSES:
        raise ValueError("purpose 过滤无效")
    if availability is not None and availability not in AVAILABILITY_STATES:
        raise ValueError("availability 过滤无效")
    if authorization is not None and authorization not in AUTHORIZATION_STATES:
        raise ValueError("authorization 过滤无效")
    if analysis_status is not None and analysis_status not in ANALYSIS_STATES:
        raise ValueError("analysis_status 过滤无效")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit 必须在 1 到 {MAX_PAGE_SIZE} 之间")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset 不能小于 0")
    if task_id is not None:
        exists = db.connection.execute(
            "SELECT 1 FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if exists is None:
            raise KeyError(f"任务不存在：{task_id}")

    scope_join = ""
    authorization_sql = (
        "CASE WHEN own.quick_fingerprint=asset.quick_fingerprint "
        "THEN 'approved' ELSE 'unknown' END"
    )
    params: list[object] = []
    task_scope_sql = ""
    if task_id is not None:
        scope_join = """
        JOIN task_source_scopes scope
          ON scope.library_id = location.library_id
         AND scope.task_id = ?
         AND scope.active = 1
         AND (
           trim(replace(COALESCE(scope.relative_path, ''), char(92), '/'), '/') = ''
           OR trim(replace(location.relative_path, char(92), '/'), '/') =
              trim(replace(scope.relative_path, char(92), '/'), '/')
           OR substr(
                trim(replace(location.relative_path, char(92), '/'), '/'),
                1,
                length(trim(replace(scope.relative_path, char(92), '/'), '/')) + 1
              ) = trim(replace(scope.relative_path, char(92), '/'), '/') || '/'
         )
        JOIN task_source_snapshots snapshot
          ON snapshot.rowid=(
            SELECT latest.rowid FROM task_source_snapshots latest
            WHERE latest.scope_id=scope.scope_id
            ORDER BY latest.created_at DESC,latest.rowid DESC LIMIT 1
          )
        JOIN json_each(snapshot.summary_json, '$.assets') snapshot_asset
          ON json_extract(snapshot_asset.value, '$.asset_id')=asset.asset_id
        """
        authorization_sql = (
            "CASE WHEN own.quick_fingerprint=asset.quick_fingerprint "
            "THEN 'approved' ELSE COALESCE("
            "json_extract(snapshot_asset.value, '$.authorization_status'), 'unknown') END"
        )
        params.append(task_id)
        if sqlite3.sqlite_version_info >= (3, 35, 0):
            scope_join = scope_join.replace(
                "json_each(snapshot.summary_json, '$.assets') snapshot_asset",
                "json_each(catalog_task_snapshot_members(snapshot.rowid, asset.asset_id), "
                "'$.assets') snapshot_asset",
            )

    conditions = []
    if query is not None:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        conditions.append(
            """(asset.asset_id LIKE ? OR location.relative_path LIKE ?
               OR library.name LIKE ?
               OR EXISTS (SELECT 1 FROM catalog_asset_annotations annotation
                 WHERE annotation.asset_id=asset.asset_id
                   AND annotation.asset_fingerprint=asset.quick_fingerprint
                   AND (json_extract(annotation.fields_json,'$.description') LIKE ?
                        OR json_extract(annotation.fields_json,'$.tags') LIKE ?))
               OR EXISTS (SELECT 1 FROM catalog_segment_marks mark
                 WHERE mark.asset_id=asset.asset_id
                   AND mark.asset_fingerprint=asset.quick_fingerprint
                   AND (mark.tags LIKE ? OR
                     ((mark.edited_fields_json IS NULL OR EXISTS (
                        SELECT 1 FROM json_each(mark.edited_fields_json)
                        WHERE value='描述'))
                      AND (mark.title LIKE ? OR mark.description LIKE ?))))
               OR EXISTS (SELECT 1 FROM visual_observations observation
                 WHERE observation.asset_id=asset.asset_id
                   AND observation.asset_fingerprint=asset.quick_fingerprint
                   AND (observation.title LIKE ? OR observation.description LIKE ?)
                   AND NOT EXISTS (SELECT 1 FROM visual_observations newer
                     WHERE newer.asset_id=observation.asset_id
                       AND newer.asset_fingerprint=observation.asset_fingerprint
                       AND newer.source_start_us=observation.source_start_us
                       AND newer.source_end_us=observation.source_end_us
                       AND (CASE newer.origin WHEN 'human' THEN 0 ELSE 1 END
                            < CASE observation.origin WHEN 'human' THEN 0 ELSE 1 END
                            OR (newer.origin=observation.origin
                                AND newer.observation_id>observation.observation_id)))
                   AND NOT EXISTS (SELECT 1 FROM catalog_segment_marks mark
                     WHERE mark.asset_id=observation.asset_id
                       AND mark.asset_fingerprint=observation.asset_fingerprint
                       AND mark.base_start_us=observation.source_start_us
                       AND mark.base_end_us=observation.source_end_us
                       AND (mark.edited_fields_json IS NULL OR EXISTS (
                         SELECT 1 FROM json_each(mark.edited_fields_json)
                         WHERE value='描述')))))""".replace(" LIKE ?", " LIKE ? ESCAPE '\\'")
        )
        params.extend((pattern,) * 10)
    if library_id is not None:
        conditions.append("location.library_id=?")
        params.append(library_id)
    if purpose is not None:
        conditions.append("library.purpose=?")
        params.append(purpose)
    if media_kind is not None:
        conditions.append("asset.media_kind=?")
        params.append(media_kind)
    if asset_id is not None:
        conditions.append("asset.asset_id=?")
        params.append(asset_id)

    availability_sql = """catalog_live_availability(
        library.mount_path, location.relative_path, location.state, library.online
    )"""
    # Reusable approval follows the current fingerprint; task-scoped rows may
    # also carry authorization in the frozen source snapshot.
    if availability is not None:
        conditions.append(f"({availability_sql})=?")
        params.append(availability)
    if authorization is not None:
        conditions.append(f"({authorization_sql})=?")
        params.append(authorization)
    analysis_sql = """CASE
      WHEN asset.media_kind NOT IN ('video','image') THEN 'not_applicable'
      WHEN COALESCE(obs.current_count,0)+COALESCE(marked.current_count,0)>0 THEN 'observed'
      WHEN COALESCE(obs.total_count,0)+COALESCE(marked.total_count,0)>0 THEN 'stale'
      ELSE 'unobserved' END"""
    if analysis_status is not None:
        conditions.append(f"({analysis_sql})=?")
        params.append(analysis_status)
    where_sql = "" if not conditions else "WHERE " + " AND ".join(conditions)

    # A single ledger location cannot lose a preferred-copy contest. Without
    # an availability filter its live state does not affect membership, total or
    # ordering, so observe it only when projecting the selected page. Multiple
    # ledger locations conservatively retain live ranking, even if scope/filter
    # happens to leave one candidate. All availability filters keep full checks.
    multiple_location_join = ""
    projected_availability_sql = availability_sql
    if availability is None:
        multiple_location_join = """
          LEFT JOIN (
            SELECT asset_id FROM asset_locations
            GROUP BY asset_id HAVING COUNT(*) > 1
          ) multiple_location ON multiple_location.asset_id=asset.asset_id
        """
        projected_availability_sql = (
            f"CASE WHEN multiple_location.asset_id IS NULL THEN NULL "
            f"ELSE {availability_sql} END"
        )

    # Analysis counts affect membership only when explicitly filtered. Keep
    # their expensive aggregation after LIMIT otherwise, inside the same SQL
    # snapshot as preferred-location ranking and the exact total.
    defer_analysis = analysis_status is None and sqlite3.sqlite_version_info >= (3, 35, 0)
    analysis_projection = f"""{analysis_sql} AS analysis_status,
        COALESCE(obs.current_count,0)+COALESCE(marked.current_count,0) AS segment_count,"""
    analysis_joins = """
      LEFT JOIN (
        SELECT observation.asset_id,
               COUNT(*) AS total_count,
               COUNT(DISTINCT CASE
                 WHEN observation.asset_fingerprint=current_asset.quick_fingerprint
                 AND NOT EXISTS (
                   SELECT 1 FROM catalog_segment_marks edit
                   WHERE edit.asset_id=observation.asset_id
                     AND edit.asset_fingerprint=current_asset.quick_fingerprint
                     AND edit.base_start_us=observation.source_start_us
                     AND edit.base_end_us=observation.source_end_us
                 )
                 THEN printf('%d:%d',observation.source_start_us,observation.source_end_us)
               END) AS current_count
        FROM visual_observations observation
        JOIN assets current_asset ON current_asset.asset_id=observation.asset_id
        GROUP BY observation.asset_id
      ) obs ON obs.asset_id=asset.asset_id
      LEFT JOIN (
        SELECT mark.asset_id, COUNT(*) AS total_count,
               COUNT(CASE WHEN mark.asset_fingerprint=current_asset.quick_fingerprint
                          THEN 1 END) AS current_count
        FROM catalog_segment_marks mark
        JOIN assets current_asset ON current_asset.asset_id=mark.asset_id
        GROUP BY mark.asset_id
      ) marked ON marked.asset_id=asset.asset_id
    """
    # A task join can duplicate candidates even for one ledger location. Keep
    # that path and membership-affecting state filters on the original ranking.
    split_single_locations = (
        task_id is None and availability is None and defer_analysis
    )
    rank_projection = f"""ROW_NUMBER() OVER (
          PARTITION BY asset.asset_id
          ORDER BY
            CASE {projected_availability_sql}
              WHEN 'available' THEN 0 WHEN 'offline' THEN 1 ELSE 2
            END,
            location.relative_path,
            location.location_id
        ) AS asset_rank"""
    if split_single_locations:
        rank_projection = "multiple_location.asset_id AS duplicate_asset_id"
    # Ordinary library/purpose browsing depends only on location membership.
    # Asset rows and their fingerprint-bound authorization are display details:
    # join them for the selected page, not once per candidate. Other filters and
    # task snapshots keep the original joins and their membership semantics.
    defer_browse_details = defer_analysis and all(value is None for value in (
        task_id, query, media_kind, authorization, analysis_status, availability, asset_id,
    ))
    candidate_asset_id = "asset.asset_id"
    candidate_from = "assets asset JOIN asset_locations location USING(asset_id)"
    candidate_authorization = authorization_sql
    candidate_location_join = multiple_location_join
    if defer_browse_details:
        candidate_asset_id = "location.asset_id"
        candidate_from = "asset_locations location"
        candidate_authorization = "NULL"
        candidate_location_join = multiple_location_join.replace(
            "=asset.asset_id", "=location.asset_id",
        )
    base_sql = f"""
      SELECT
        {candidate_asset_id},
        location.relative_path,
        location.location_id,
        {projected_availability_sql} AS availability,
        {candidate_authorization} AS authorization_status,
        {'' if defer_analysis else analysis_projection}
        {rank_projection}
      FROM {candidate_from}
      JOIN libraries library USING(library_id)
      {scope_join}
      {candidate_location_join}
      {'' if defer_browse_details else 'LEFT JOIN self_produced_asset_authorizations own ON own.asset_id=asset.asset_id'}
      {'' if defer_analysis else analysis_joins}
      {where_sql}
    """
    if split_single_locations:
        # Apply every original membership filter before splitting. The duplicate
        # flag still counts all ledger locations, so a filter leaving one copy
        # retains its live availability check and original preferred-copy order.
        candidate_columns = (
            "asset_id, relative_path, location_id, availability, authorization_status"
        )
        base_sql = f"""WITH candidates AS MATERIALIZED ({base_sql})
            SELECT {candidate_columns}, 1 AS asset_rank
            FROM candidates WHERE duplicate_asset_id IS NULL
            UNION ALL
            SELECT {candidate_columns}, ROW_NUMBER() OVER (
                PARTITION BY asset_id
                ORDER BY CASE availability
                    WHEN 'available' THEN 0 WHEN 'offline' THEN 1 ELSE 2 END,
                    relative_path, location_id
            ) AS asset_rank
            FROM candidates WHERE duplicate_asset_id IS NOT NULL
        """
    # Query-local observations keep filtering, duplicate choice and totals aligned.
    # No catalog state is written; the next request observes reconnects afresh.
    db.connection.create_function(
        "catalog_task_snapshot_members", 2,
        _task_snapshot_member_reader(db.connection), deterministic=True,
    )
    live_availability = _live_availability_reader()
    db.connection.create_function("catalog_live_availability", 4,
                                  live_availability, deterministic=True)
    use_default_page = (
        sqlite3.sqlite_version_info >= (3, 35, 0)
        and all(value is None for value in (
            task_id, query, library_id, purpose, media_kind, availability,
            authorization, analysis_status, asset_id,
        ))
    )
    try:
        if use_default_page:
            rows = db.connection.execute(
                _default_catalog_page_sql(
                    projected_availability_sql, multiple_location_join,
                    authorization_sql, availability_sql,
                ),
                (limit, offset),
            ).fetchall()
            # The totals LEFT JOIN preserves one sentinel row for an empty page.
            total = int(rows[0]["total_count"])
            rows = [row for row in rows if row["asset_id"] is not None]
        else:
            page_prefix = task_scope_sql
            page_members = f"""SELECT *, COUNT(*) OVER () AS total_count
                FROM ({base_sql}) ranked WHERE asset_rank=1"""
            if (task_id is not None or defer_browse_details) and sqlite3.sqlite_version_info >= (3, 35, 0):
                # Share one compact, filtered set between count and page. This
                # keeps the same read snapshot and preferred-copy ranking while
                # avoiding a second window that copies every matched row.
                page_prefix = f"""WITH matched AS MATERIALIZED (
                    SELECT * FROM ({base_sql}) ranked WHERE asset_rank=1
                ) """
                page_members = """SELECT *,
                    (SELECT COUNT(*) FROM matched) AS total_count FROM matched"""
            page_source = f"""(
                {page_members}
                ORDER BY lower(relative_path), asset_id
                LIMIT ? OFFSET ?
            ) selected_page"""
            page_analysis = ""
            page_analysis_joins = ""
            if defer_analysis:
                separator = "," if page_prefix else "WITH"
                page_prefix += f"""{separator} selected_page AS MATERIALIZED (
                    {page_members}
                    ORDER BY lower(relative_path), asset_id
                    LIMIT ? OFFSET ?
                ) """
                page_source = "selected_page"
                page_analysis = analysis_projection
                page_analysis_joins = analysis_joins.replace(
                    "GROUP BY observation.asset_id",
                    "WHERE observation.asset_id IN (SELECT asset_id FROM selected_page) "
                    "GROUP BY observation.asset_id",
                ).replace(
                    "GROUP BY mark.asset_id",
                    "WHERE mark.asset_id IN (SELECT asset_id FROM selected_page) "
                    "GROUP BY mark.asset_id",
                )
            page_projection = "selected_page.*"
            page_authorization_join = ""
            if defer_browse_details:
                page_projection = (
                    "selected_page.asset_id, selected_page.relative_path, "
                    "selected_page.location_id, selected_page.availability, "
                    "selected_page.asset_rank, selected_page.total_count, "
                    + authorization_sql + " AS authorization_status"
                )
                page_authorization_join = (
                    "LEFT JOIN self_produced_asset_authorizations own "
                    "ON own.asset_id=asset.asset_id"
                )
            rows = db.connection.execute(
                page_prefix + f"""
            SELECT {page_projection}, {page_analysis} asset.media_kind, asset.size_bytes,
                   asset.full_sha256, library.library_id,
                   library.name AS library_name, library.online AS library_online,
                   library.mount_path, location.state AS location_state,
                   probe.result_json AS probe_result_json FROM {page_source}
            JOIN assets asset ON asset.asset_id=selected_page.asset_id
            JOIN asset_locations location ON location.location_id=selected_page.location_id
            JOIN libraries library ON library.library_id=location.library_id
            {page_analysis_joins}
            {page_authorization_join}
            LEFT JOIN analysis_runs probe ON probe.asset_id=selected_page.asset_id
              AND probe.stage='technical_probe' AND probe.status='completed'
            ORDER BY lower(selected_page.relative_path), selected_page.asset_id
            """,
                (*params, limit, offset),
            ).fetchall()
            if rows:
                total = int(rows[0]["total_count"])
            else:
                # Keep the complete live observation for the empty-page fallback.
                count_base_sql = base_sql.replace(
                    projected_availability_sql, availability_sql,
                )
                count_sql = task_scope_sql + f"SELECT COUNT(*) FROM ({count_base_sql}) ranked WHERE asset_rank=1"
                total = int(db.connection.execute(count_sql, tuple(params)).fetchone()[0])

    finally:
        db.connection.create_function("catalog_task_snapshot_members", 2, None)
        db.connection.create_function("catalog_live_availability", 4, None)

    items = []
    for row in rows:
        item_availability = (
            live_availability(row["mount_path"], row["relative_path"],
                              row["location_state"], row["library_online"],
                              scan_directory=False)
            if row["availability"] is None else str(row["availability"])
        )
        items.append(
            {
                "assetId": row["asset_id"],
                "mediaKind": row["media_kind"],
                "sizeBytes": int(row["size_bytes"]),
                "fingerprintStatus": "full" if row["full_sha256"] else "quick",
                "libraryId": row["library_id"],
                "libraryName": row["library_name"],
                "relativePath": row["relative_path"],
                "availability": item_availability,
                "online": item_availability == "available" and bool(row["library_online"]),
                "previewUrl": _preview_url(task_id, row["asset_id"], item_availability,
                                           row["media_kind"]),
                "durationSeconds": _duration_seconds(row["probe_result_json"]),
                "authorizationStatus": row["authorization_status"],
                "analysisStatus": row["analysis_status"],
                "segmentCount": int(row["segment_count"]),
            }
        )
    from .visual_coverage import annotate_content_coverage
    annotate_content_coverage(db, items)
    if query is not None and items:
        page_ids = [item["assetId"] for item in items]
        matched = _matching_source_windows(db, page_ids, query)
        annotations = _matching_asset_annotations(db, page_ids, query)
        for item in items:
            windows = matched.get(item["assetId"], [])
            item["matchedSegmentCount"] = len(windows)
            item["matchedSegments"] = windows[:3]
            if item["assetId"] in annotations:
                item["matchedAssetAnnotation"] = annotations[item["assetId"]]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _matching_source_windows(db, asset_ids: list[str], query: str) -> dict[str, list[dict]]:
    """Batch-project current, effective source windows for one search page."""
    placeholders = ",".join("?" for _ in asset_ids)
    observations = db.connection.execute(
        f"""SELECT observation.* FROM visual_observations observation
            JOIN assets asset USING(asset_id)
            WHERE observation.asset_id IN ({placeholders})
              AND observation.asset_fingerprint=asset.quick_fingerprint
            ORDER BY observation.asset_id,observation.source_start_us,
                     observation.source_end_us,
                     CASE observation.origin WHEN 'human' THEN 0 ELSE 1 END,
                     observation.observation_id DESC""",
        tuple(asset_ids),
    ).fetchall()
    preferred = {}
    for row in observations:
        key = (row["asset_id"], row["source_start_us"], row["source_end_us"])
        preferred.setdefault(key, row)
    marks = db.connection.execute(
        f"""SELECT mark.* FROM catalog_segment_marks mark
            JOIN assets asset USING(asset_id)
            WHERE mark.asset_id IN ({placeholders})
              AND mark.asset_fingerprint=asset.quick_fingerprint
            ORDER BY mark.asset_id,mark.source_start_us,mark.source_end_us""",
        tuple(asset_ids),
    ).fetchall()
    replaced = set()
    matched: dict[str, list[dict]] = {}

    def add(asset_id, start, end, title, description, origin, usage_status=None, tags=""):
        if not any(query.casefold() in str(text or "").casefold()
                   for text in (title, description, tags)):
            return
        matched.setdefault(asset_id, []).append({
            "sourceStartUs": start, "sourceEndUs": end,
            "title": (title or description or "内容匹配")[:160],
            "origin": origin, "usageStatus": usage_status,
        })

    for mark in marks:
        base = (mark["asset_id"], mark["base_start_us"], mark["base_end_us"])
        replaced.add(base)
        edited = None if mark["edited_fields_json"] is None else \
            set(json.loads(mark["edited_fields_json"]))
        observation = preferred.get(base)
        title = mark["title"]
        description = mark["description"]
        start, end = mark["source_start_us"], mark["source_end_us"]
        if edited is not None and observation is not None:
            if "描述" not in edited:
                title, description = observation["title"], observation["description"]
            if "源开始秒" not in edited:
                start = observation["source_start_us"]
            if "源结束秒" not in edited:
                end = observation["source_end_us"]
        add(mark["asset_id"], start, end, title, description, "human",
            mark["usage_status"], mark["tags"])
    for key, row in preferred.items():
        if key not in replaced:
            add(row["asset_id"], row["source_start_us"], row["source_end_us"],
                row["title"], row["description"], row["origin"])
    for windows in matched.values():
        windows.sort(key=lambda item: (item["sourceStartUs"], item["sourceEndUs"]))
    return matched


def _matching_asset_annotations(db, asset_ids: list[str], query: str) -> dict[str, dict]:
    """Explain asset-level text matches without scanning annotations per row."""
    placeholders = ",".join("?" for _ in asset_ids)
    rows = db.connection.execute(
        f"""SELECT annotation.asset_id,annotation.fields_json
            FROM catalog_asset_annotations annotation
            JOIN assets asset USING(asset_id)
            WHERE annotation.asset_id IN ({placeholders})
              AND annotation.asset_fingerprint=asset.quick_fingerprint""",
        tuple(asset_ids),
    ).fetchall()
    matches = {}
    for row in rows:
        fields = json.loads(row["fields_json"])
        for field in ("description", "tags"):
            value = fields.get(field)
            display = ", ".join(value) if isinstance(value, list) else str(value or "")
            if query.casefold() in display.casefold():
                matches[row["asset_id"]] = {"field": field, "text": display[:160]}
                break
    return matches


def get_asset_detail(db, asset_id: str, *, task_id: str | None = None) -> dict:
    """One ledger row and its current, deduplicated source-window evidence."""
    if not isinstance(asset_id, str) or not asset_id.strip():
        raise ValueError("asset_id 不能为空")
    page = list_asset_index(db, task_id=task_id, asset_id=asset_id, limit=1)
    if not page["items"]:
        raise KeyError(f"素材不在当前范围：{asset_id}")
    item = page["items"][0]
    rows = db.connection.execute(
        """SELECT * FROM (
             SELECT observation.*,
                    ROW_NUMBER() OVER (
                      PARTITION BY source_start_us,source_end_us
                      ORDER BY CASE origin WHEN 'human' THEN 0 ELSE 1 END,
                               observation_id DESC
                    ) AS window_rank
             FROM visual_observations observation
             JOIN assets asset ON asset.asset_id=observation.asset_id
             WHERE observation.asset_id=?
               AND observation.asset_fingerprint=asset.quick_fingerprint
           ) WHERE window_rank=1
           ORDER BY source_start_us,source_end_us""",
        (asset_id,),
    ).fetchall()
    from .segment_marks import _public_mark, current_marks, segment_id_for_window

    marks = current_marks(db, asset_id)
    replaced_windows = {(mark["base_start_us"], mark["base_end_us"]) for mark in marks.values()}
    segments = [{
        "segmentId": segment_id_for_window(asset_id, row["source_start_us"], row["source_end_us"]),
        "revision": 0,
        "observationId": row["observation_id"],
        "sourceStartUs": row["source_start_us"],
        "sourceEndUs": row["source_end_us"],
        "title": row["title"],
        "description": row["description"],
        "cameraMotion": "不适用" if item["mediaKind"] == "image" else row["camera_motion"],
        "motionKind": row["motion_kind"],
        "subjectMotion": row["subject_motion"],
        "origin": row["origin"],
        "usageStatus": "待核验",
        "note": "",
        "tags": "",
        "qualityIssue": "",
        "usableReason": "",
        "duplicateNote": "",
        "uncertaintyReason": row["uncertainty_reason"],
    } for row in rows if (row["source_start_us"], row["source_end_us"]) not in replaced_windows]
    segments.extend(_public_mark(mark) for mark in marks.values())
    from .quality_windows import quality_result, quality_for_window
    from .duplicate_evidence import duplicates_for_window
    quality = quality_result(db, asset_id)
    known = {(segment["sourceStartUs"], segment["sourceEndUs"]) for segment in segments}
    for window in quality.get("windows", []):
        start, end = window["sourceStartUs"], window["sourceEndUs"]
        if (start, end) in known or (start, end) in replaced_windows:
            continue
        measured = quality_for_window(db, asset_id, start, end)
        segments.append({"segmentId": segment_id_for_window(asset_id, start, end), "revision": 0,
            "sourceStartUs": start, "sourceEndUs": end, "title": "", "description": "",
            "origin": "local", "cameraMotion": "不适用" if item["mediaKind"] == "image" else measured["cameraMotion"],
            "motionKind": None, "subjectMotion": measured["subjectMotion"], "usageStatus": "待核验",
            "note": "", "tags": "", "qualityIssue": "；".join(measured["issues"]),
            "usableReason": "", "duplicateNote": "", "uncertaintyReason": measured["uncertaintyReason"]})
    for segment in segments:
        start, end = segment["sourceStartUs"], segment["sourceEndUs"]
        measured = quality_for_window(db, asset_id, start, end)
        duplicates = duplicates_for_window(db, asset_id, start, end)
        segment["qualityEvidence"] = measured
        segment["duplicateEvidence"] = duplicates
        if segment["origin"] != "human":
            segment["qualityIssue"] = "；".join(measured["issues"])
            segment["duplicateNote"] = "；".join(d["reason"] for d in duplicates)
    segments.sort(key=lambda segment: (segment["sourceStartUs"], segment["sourceEndUs"]))
    return {
        **item,
        "fingerprint": db.connection.execute(
            "SELECT quick_fingerprint FROM assets WHERE asset_id=?", (asset_id,)
        ).fetchone()["quick_fingerprint"],
        "segments": segments,
    }
