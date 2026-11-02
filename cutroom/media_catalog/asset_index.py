"""Read-only material index for the colleague workbench."""

from __future__ import annotations

import json
from urllib.parse import quote


MEDIA_KINDS = {"video", "image", "audio", "other"}
AVAILABILITY_STATES = {"available", "offline", "missing"}
AUTHORIZATION_STATES = {"approved", "review_required", "unknown"}
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
    return duration if duration >= 0 else None


def _preview_url(task_id: str | None, asset_id: str, availability: str) -> str | None:
    if task_id is None or availability != "available":
        return None
    return "/api/tasks/{}/rough-media/{}/preview".format(
        quote(task_id, safe=""),
        quote(asset_id, safe=""),
    )


def list_asset_index(
    db,
    *,
    task_id: str | None = None,
    query: str | None = None,
    library_id: str | None = None,
    media_kind: str | None = None,
    availability: str | None = None,
    authorization: str | None = None,
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
    media_kind = _normalized_optional(media_kind)
    availability = _normalized_optional(availability)
    authorization = _normalized_optional(authorization)
    if media_kind is not None and media_kind not in MEDIA_KINDS:
        raise ValueError("media_kind 过滤无效")
    if availability is not None and availability not in AVAILABILITY_STATES:
        raise ValueError("availability 过滤无效")
    if authorization is not None and authorization not in AUTHORIZATION_STATES:
        raise ValueError("authorization 过滤无效")
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

    conditions = []
    if query is not None:
        pattern = f"%{query}%"
        conditions.append(
            "(asset.asset_id LIKE ? OR location.relative_path LIKE ? OR library.name LIKE ?)"
        )
        params.extend((pattern, pattern, pattern))
    if library_id is not None:
        conditions.append("location.library_id=?")
        params.append(library_id)
    if media_kind is not None:
        conditions.append("asset.media_kind=?")
        params.append(media_kind)

    availability_sql = """
      CASE
        WHEN location.state='missing' THEN 'missing'
        WHEN location.state='offline' OR library.online=0 THEN 'offline'
        ELSE 'available'
      END
    """
    # Reusable approval follows the current fingerprint; task-scoped rows may
    # also carry authorization in the frozen source snapshot.
    if availability is not None:
        conditions.append(f"({availability_sql})=?")
        params.append(availability)
    if authorization is not None:
        conditions.append(f"({authorization_sql})=?")
        params.append(authorization)
    where_sql = "" if not conditions else "WHERE " + " AND ".join(conditions)

    base_sql = f"""
      SELECT
        asset.asset_id,
        asset.media_kind,
        asset.size_bytes,
        asset.quick_fingerprint,
        asset.full_sha256,
        library.library_id,
        library.name AS library_name,
        library.online AS library_online,
        location.relative_path,
        location.location_id,
        {availability_sql} AS availability,
        {authorization_sql} AS authorization_status,
        probe.result_json AS probe_result_json,
        ROW_NUMBER() OVER (
          PARTITION BY asset.asset_id
          ORDER BY
            CASE {availability_sql}
              WHEN 'available' THEN 0 WHEN 'offline' THEN 1 ELSE 2
            END,
            location.relative_path,
            location.location_id
        ) AS asset_rank
      FROM assets asset
      JOIN asset_locations location USING(asset_id)
      JOIN libraries library USING(library_id)
      {scope_join}
      LEFT JOIN self_produced_asset_authorizations own ON own.asset_id=asset.asset_id
      LEFT JOIN analysis_runs probe
        ON probe.asset_id=asset.asset_id
       AND probe.stage='technical_probe'
       AND probe.status='completed'
      {where_sql}
    """
    count_sql = f"SELECT COUNT(*) FROM ({base_sql}) ranked WHERE asset_rank=1"
    total = int(db.connection.execute(count_sql, tuple(params)).fetchone()[0])
    rows = db.connection.execute(
        f"""
        SELECT * FROM ({base_sql}) ranked
        WHERE asset_rank=1
        ORDER BY lower(relative_path), asset_id
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()

    items = []
    for row in rows:
        item_availability = str(row["availability"])
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
                "previewUrl": _preview_url(task_id, row["asset_id"], item_availability),
                "durationSeconds": _duration_seconds(row["probe_result_json"]),
                "authorizationStatus": row["authorization_status"],
            }
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}
