"""Bind one task to an explicit, history-preserving material-library scope."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import uuid

from . import workflow
from .fingerprint import quick_fingerprint
from .path_utils import filesystem_path
from .scanner import LibraryOffline, discover_files, scan_library


SOURCE_MODES = ("primary", "selected_generic", "default_generic")


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _stable_digest(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_task(db, task_id: str) -> None:
    if db.connection.execute(
        "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone() is None:
        raise KeyError(f"任务不存在：{task_id}")


def _normalized_relative(path: str) -> str:
    raw = str(path or "")
    if "\x00" in raw:
        raise ValueError("relative_path 不能包含 NUL")
    normalized = raw.replace("\\", "/")
    if not normalized:
        return ""
    if normalized.startswith("/") or (
        len(normalized) >= 2 and normalized[1] == ":"
    ):
        raise ValueError("relative_path 必须是素材库内的相对路径")
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("relative_path 必须是规范的相对路径")
    return "/".join(parts)


def _normalized_location_relative(path: str) -> str:
    raw = str(path or "")
    if "\x00" in raw:
        raise ValueError("素材位置 relative_path 不能包含 NUL")
    normalized = raw.replace("\\", "/").strip("/")
    if not normalized:
        return ""
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("素材位置 relative_path 不是规范相对路径")
    return "/".join(parts)


def _is_within_scope(path: str, scope_path: str) -> bool:
    candidate = _normalized_location_relative(path)
    prefix = _normalized_relative(scope_path)
    return not prefix or candidate == prefix or candidate.startswith(prefix + "/")


def _scope_assets(db, library_id: str, relative_path: str) -> list[dict]:
    rows = db.connection.execute(
        """
        SELECT
          al.asset_id,
          al.relative_path,
          a.size_bytes,
          a.quick_fingerprint
        FROM asset_locations al
        JOIN assets a USING(asset_id)
        WHERE al.library_id=? AND al.state='available'
        ORDER BY al.relative_path, al.location_id
        """,
        (library_id,),
    ).fetchall()
    assets = {}
    for row in rows:
        if _is_within_scope(row["relative_path"], relative_path):
            assets.setdefault(
                row["asset_id"],
                {
                    "asset_id": row["asset_id"],
                    "relative_path": row["relative_path"],
                    "size_bytes": row["size_bytes"],
                    "quick_fingerprint": row["quick_fingerprint"],
                },
            )
    return sorted(assets.values(), key=lambda item: item["asset_id"])


def _scope_root(mount_path: str, relative_path: str) -> Path:
    root = Path(mount_path).resolve()
    normalized = _normalized_relative(relative_path)
    candidate = (
        root if not normalized else root.joinpath(*normalized.split("/")).resolve()
    )
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("relative_path 解析后超出素材库挂载目录") from error
    return candidate


def _active_scope_rows(db, task_id: str):
    return db.connection.execute(
        """
        SELECT
          s.scope_id,
          s.library_id,
          s.relative_path,
          s.source_mode,
          s.priority,
          l.name,
          l.mount_path,
          l.online,
          l.purpose,
          l.is_default_generic
        FROM task_source_scopes s
        JOIN libraries l USING(library_id)
        WHERE s.task_id=? AND s.active=1
        ORDER BY s.priority, s.scope_id
        """,
        (task_id,),
    ).fetchall()


def _source_record(db, row) -> dict:
    assets = _scope_assets(db, row["library_id"], row["relative_path"])
    last_sync = db.connection.execute(
        """
        SELECT created_at FROM task_source_snapshots
        WHERE scope_id=?
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (row["scope_id"],),
    ).fetchone()
    online = bool(row["online"]) and _scope_root(
        row["mount_path"], row["relative_path"]
    ).is_dir()
    return {
        "scope_id": row["scope_id"],
        "library_id": row["library_id"],
        "name": row["name"],
        "mount_path": row["mount_path"],
        "relative_path": _normalized_relative(row["relative_path"]),
        "online": online,
        "asset_count": len(assets),
        "last_synced_at": None if last_sync is None else last_sync["created_at"],
    }


def get_task_sources(db, task_id: str) -> dict:
    _require_task(db, task_id)
    setting = db.connection.execute(
        """
        SELECT include_default_generic FROM task_source_settings
        WHERE task_id=?
        """,
        (task_id,),
    ).fetchone()
    result = {
        "primary": None,
        "selected_generic": [],
        "default_generic": [],
        "include_default_generic": bool(
            setting is not None and setting["include_default_generic"]
        ),
    }
    for row in _active_scope_rows(db, task_id):
        record = _source_record(db, row)
        if row["source_mode"] == "primary":
            result["primary"] = record
        else:
            result[row["source_mode"]].append(record)
    return result


def _library_map(db, library_ids: list[str]) -> dict:
    if not library_ids:
        return {}
    placeholders = ",".join("?" for _ in library_ids)
    rows = db.connection.execute(
        f"""
        SELECT library_id, purpose, is_default_generic
        FROM libraries WHERE library_id IN ({placeholders})
        """,
        tuple(library_ids),
    ).fetchall()
    found = {row["library_id"]: row for row in rows}
    missing = [library_id for library_id in library_ids if library_id not in found]
    if missing:
        raise KeyError(f"找不到素材库：{', '.join(missing)}")
    return found


def _deduplicated(values) -> list[str]:
    result = []
    seen = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _desired_sources(
    db,
    primary_library_id: str,
    selected_generic_ids,
    include_default_generic: bool,
) -> list[tuple[str, str, int]]:
    if not primary_library_id or not primary_library_id.strip():
        raise ValueError("主素材目录不能为空")
    selected = _deduplicated(list(selected_generic_ids or []))
    requested = _deduplicated([primary_library_id, *selected])
    libraries = _library_map(db, requested)
    if libraries[primary_library_id]["purpose"] != "task_specific":
        raise ValueError("主素材目录必须是任务专属镜头素材库")
    for library_id in selected:
        purpose = libraries[library_id]["purpose"]
        if purpose == "packaging":
            raise ValueError("包装素材库不能绑定进镜头范围")
        if purpose != "generic":
            raise ValueError("指定补充目录必须是通用镜头素材库")

    desired = [("primary", primary_library_id, 0)]
    desired.extend(
        ("selected_generic", library_id, 1000 + index)
        for index, library_id in enumerate(selected)
    )
    if include_default_generic:
        selected_set = set(selected)
        defaults = db.connection.execute(
            """
            SELECT library_id FROM libraries
            WHERE purpose='generic' AND is_default_generic=1
            ORDER BY library_id
            """
        ).fetchall()
        desired.extend(
            ("default_generic", row["library_id"], 2000 + index)
            for index, row in enumerate(defaults)
            if row["library_id"] not in selected_set
        )
    return desired


def _source_configuration(rows) -> list[tuple[str, str]]:
    configuration = []
    seen = set()
    for row in rows:
        key = (row["source_mode"], row["library_id"])
        if key not in seen:
            seen.add(key)
            configuration.append(key)
    return configuration


def _inherited_relative_paths(rows, source_mode: str, library_id: str) -> list[str]:
    exact = [
        row
        for row in rows
        if row["library_id"] == library_id and row["source_mode"] == source_mode
    ]
    compatible = exact
    if not compatible and source_mode in ("selected_generic", "default_generic"):
        compatible = [
            row
            for row in rows
            if row["library_id"] == library_id
            and row["source_mode"] in ("selected_generic", "default_generic")
        ]
    paths = []
    seen = set()
    for row in compatible:
        path = _normalized_relative(row["relative_path"])
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths or [""]


def _expanded_desired_sources(rows, desired) -> list[tuple[str, str, int, str]]:
    priorities = {
        "primary": 0,
        "selected_generic": 1000,
        "default_generic": 2000,
    }
    expanded = []
    for source_mode, library_id, _ in desired:
        for relative_path in _inherited_relative_paths(
            rows, source_mode, library_id
        ):
            expanded.append(
                (
                    source_mode,
                    library_id,
                    priorities[source_mode],
                    relative_path,
                )
            )
            priorities[source_mode] += 1
    return expanded


def _asset_in_proposed_sources(db, asset_id: str, proposed_sources) -> bool:
    locations = db.connection.execute(
        """
        SELECT library_id, relative_path
        FROM asset_locations
        WHERE asset_id=? AND state='available'
        ORDER BY location_id
        """,
        (asset_id,),
    ).fetchall()
    for location in locations:
        for _, library_id, _, relative_path in proposed_sources:
            if (
                location["library_id"] == library_id
                and _is_within_scope(location["relative_path"], relative_path)
            ):
                return True
    return False


def _validate_existing_references(db, task_id: str, proposed_sources) -> None:
    task = db.connection.execute(
        "SELECT task_type, brief_json FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    locked_digital_asset = None
    if task is not None and task["task_type"] == "digital_human":
        try:
            brief = json.loads(task["brief_json"] or "{}")
        except json.JSONDecodeError:
            brief = {}
        source = brief.get("digital_human_source")
        if isinstance(source, dict):
            locked_digital_asset = source.get("asset_id")
    invalid_task_assets = [
        row["asset_id"]
        for row in db.connection.execute(
            """
            SELECT asset_id, role FROM task_assets
            WHERE task_id=? ORDER BY asset_id
            """,
            (task_id,),
        ).fetchall()
        if not (
            row["role"] == "digital_human"
            and row["asset_id"] == locked_digital_asset
        )
        if not _asset_in_proposed_sources(db, row["asset_id"], proposed_sources)
    ]
    invalid_scene_cards = [
        (row["card_id"], row["asset_id"])
        for row in db.connection.execute(
            """
            SELECT card_id, asset_id FROM scene_cards
            WHERE task_id=? AND asset_id IS NOT NULL
            ORDER BY idx, card_id
            """,
            (task_id,),
        ).fetchall()
        if row["asset_id"] != locked_digital_asset
        if not _asset_in_proposed_sources(db, row["asset_id"], proposed_sources)
    ]
    if invalid_task_assets or invalid_scene_cards:
        details = []
        if invalid_task_assets:
            details.append("task_assets=" + ",".join(invalid_task_assets))
        if invalid_scene_cards:
            details.append(
                "scene_cards="
                + ",".join(
                    f"{card_id}:{asset_id}"
                    for card_id, asset_id in invalid_scene_cards
                )
            )
        raise ValueError(
            "任务现有素材引用不在拟议素材范围内，拒绝更改来源配置："
            + "; ".join(details)
        )


def _picture_lock_exists(db, task_id: str) -> bool:
    return db.connection.execute(
        "SELECT 1 FROM picture_lock WHERE task_id=?", (task_id,)
    ).fetchone() is not None


def set_task_sources(
    db,
    task_id: str,
    *,
    primary_library_id: str,
    selected_generic_ids,
    include_default_generic: bool,
) -> dict:
    _require_task(db, task_id)
    if not isinstance(include_default_generic, bool):
        raise ValueError("include_default_generic 必须是布尔值")
    desired = _desired_sources(
        db,
        primary_library_id,
        selected_generic_ids,
        include_default_generic,
    )
    setting = db.connection.execute(
        """
        SELECT include_default_generic FROM task_source_settings
        WHERE task_id=?
        """,
        (task_id,),
    ).fetchone()
    active_rows = list(_active_scope_rows(db, task_id))
    expanded = _expanded_desired_sources(active_rows, desired)
    _validate_existing_references(db, task_id, expanded)
    unchanged = (
        setting is not None
        and bool(setting["include_default_generic"]) == include_default_generic
        and _source_configuration(active_rows)
        == [(source_mode, library_id) for source_mode, library_id, _ in desired]
    )
    if unchanged:
        return get_task_sources(db, task_id)

    locked = _picture_lock_exists(db, task_id)
    now = db.connection.execute("SELECT datetime('now')").fetchone()[0]
    with db.transaction() as connection:
        with db.task_source_write_authorization(task_id):
            connection.execute(
                """
                UPDATE task_source_scopes
                SET active=0, retired_at=?, updated_at=?
                WHERE task_id=? AND active=1
                """,
                (now, now, task_id),
            )
            if setting is not None:
                connection.execute(
                    """
                    UPDATE task_source_settings
                    SET include_default_generic=?, revision=revision + 1, updated_at=?
                    WHERE task_id=?
                    """,
                    (int(include_default_generic), now, task_id),
                )
            for source_mode, library_id, priority, relative_path in expanded:
                scope_id = "scope_" + uuid.uuid4().hex
                connection.execute(
                    """
                    INSERT INTO task_source_scopes(
                      scope_id, task_id, library_id, role, relative_path,
                      source_mode, active, priority, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        scope_id,
                        task_id,
                        library_id,
                        "primary"
                        if source_mode == "primary"
                        else "supplementary",
                        relative_path,
                        source_mode,
                        priority,
                        now,
                        now,
                    ),
                )
                summary_json, asset_count, total_size = _snapshot_summary(
                    db,
                    {
                        "library_id": library_id,
                        "relative_path": relative_path,
                        "source_mode": source_mode,
                    },
                )
                connection.execute(
                    """
                    INSERT INTO task_source_snapshots(
                      snapshot_id, task_id, scope_id, summary_json,
                      asset_count, total_size_bytes, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "source_snapshot_" + uuid.uuid4().hex,
                        task_id,
                        scope_id,
                        summary_json,
                        asset_count,
                        total_size,
                        now,
                    ),
                )
            if setting is None:
                connection.execute(
                    """
                    INSERT INTO task_source_settings(
                      task_id, include_default_generic, revision,
                      created_at, updated_at
                    ) VALUES(?, ?, 1, ?, ?)
                    """,
                    (task_id, int(include_default_generic), now, now),
                )
            if locked:
                connection.execute(
                    """
                    UPDATE picture_lock SET changed_since_lock=1
                    WHERE task_id=?
                    """,
                    (task_id,),
                )
                workflow.invalidate_from_connection(
                    connection,
                    task_id,
                    "material_sync",
                    reason="task source scope changed",
                )
    return get_task_sources(db, task_id)


def source_health(db, task_id: str) -> dict:
    sources = get_task_sources(db, task_id)
    blocking = []
    warnings = []
    if sources["primary"] is None:
        blocking.append(
            {
                "code": "PRIMARY_SOURCE_MISSING",
                "library_id": None,
                "message": "任务没有主素材目录",
            }
        )
    elif not sources["primary"]["online"]:
        blocking.append(
            {
                "code": "PRIMARY_LIBRARY_OFFLINE",
                "library_id": sources["primary"]["library_id"],
                "message": "主素材目录当前不可访问",
            }
        )
    for mode in ("selected_generic", "default_generic"):
        for source in sources[mode]:
            if not source["online"]:
                warnings.append(
                    {
                        "code": "GENERIC_LIBRARY_OFFLINE",
                        "library_id": source["library_id"],
                        "source_mode": mode,
                        "message": "补充素材目录当前不可访问",
                    }
                )
    status = "blocking" if blocking else ("warning" if warnings else "healthy")
    return {"status": status, "blocking": blocking, "warnings": warnings}


def _snapshot_summary(db, row) -> tuple[str, int, int]:
    relative_path = _normalized_relative(row["relative_path"])
    assets = _scope_assets(db, row["library_id"], relative_path)
    summary = {
        "library_id": row["library_id"],
        "relative_path": relative_path,
        "source_mode": row["source_mode"],
        "assets": assets,
    }
    return (
        _canonical_json(summary),
        len(assets),
        sum(item["size_bytes"] for item in assets),
    )


def _catalog_scope_state(db, row) -> dict:
    summary_json, asset_count, total_size = _snapshot_summary(db, row)
    return {
        "summary_json": summary_json,
        "asset_count": asset_count,
        "total_size_bytes": total_size,
        "digest": hashlib.sha256(summary_json.encode("utf-8")).hexdigest(),
    }


def _latest_scope_snapshot(db, scope_id: str) -> dict | None:
    row = db.connection.execute(
        """
        SELECT snapshot_id, summary_json, asset_count, total_size_bytes
        FROM task_source_snapshots
        WHERE scope_id=?
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (scope_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        summary = json.loads(row["summary_json"])
    except (TypeError, json.JSONDecodeError):
        summary = {"assets": []}
    return {
        "snapshot_id": row["snapshot_id"],
        "summary_json": row["summary_json"],
        "summary": summary,
        "asset_count": row["asset_count"],
        "total_size_bytes": row["total_size_bytes"],
    }


def _snapshot_state(snapshot: dict | None) -> dict | None:
    if snapshot is None:
        return None
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "summary_json": snapshot["summary_json"],
        "asset_count": snapshot["asset_count"],
        "total_size_bytes": snapshot["total_size_bytes"],
        "digest": hashlib.sha256(
            snapshot["summary_json"].encode("utf-8")
        ).hexdigest(),
    }


def _preview_scope_rows(db, task_id: str) -> list[dict]:
    rows = [
        dict(row)
        | {
            "pending_configuration": False,
            "pending_removal": False,
        }
        for row in _active_scope_rows(db, task_id)
    ]
    setting = db.connection.execute(
        """
        SELECT include_default_generic FROM task_source_settings
        WHERE task_id=?
        """,
        (task_id,),
    ).fetchone()
    if setting is None or not setting["include_default_generic"]:
        return rows

    selected_library_ids = {
        row["library_id"]
        for row in rows
        if row["source_mode"] == "selected_generic"
    }
    defaults = db.connection.execute(
        """
        SELECT library_id, name, mount_path, online, purpose, is_default_generic
        FROM libraries
        WHERE purpose='generic' AND is_default_generic=1
        ORDER BY library_id
        """
    ).fetchall()
    desired_default_ids = {
        row["library_id"]
        for row in defaults
        if row["library_id"] not in selected_library_ids
    }
    for row in rows:
        if (
            row["source_mode"] == "default_generic"
            and row["library_id"] not in desired_default_ids
        ):
            row["pending_removal"] = True

    active_library_ids = {
        row["library_id"] for row in rows if not row["pending_removal"]
    }
    priority = max(
        [
            row["priority"]
            for row in rows
            if row["source_mode"] == "default_generic"
            and not row["pending_removal"]
        ]
        or [1999]
    )
    for library in defaults:
        if library["library_id"] in active_library_ids:
            continue
        priority += 1
        rows.append(
            {
                "scope_id": f"pending_default:{library['library_id']}",
                "library_id": library["library_id"],
                "relative_path": "",
                "source_mode": "default_generic",
                "priority": priority,
                "name": library["name"],
                "mount_path": library["mount_path"],
                "online": library["online"],
                "purpose": library["purpose"],
                "is_default_generic": library["is_default_generic"],
                "pending_configuration": True,
                "pending_removal": False,
            }
        )
    return rows


def _filesystem_scope_manifest(row, *, on_progress=None) -> tuple[bool, list[dict], str | None]:
    scope_root = filesystem_path(
        _scope_root(row["mount_path"], row["relative_path"])
    )
    if not scope_root.is_dir():
        return False, [], None
    prefix = _normalized_relative(row["relative_path"])
    manifest = []
    try:
        paths = discover_files(scope_root)
        if on_progress is not None:
            paths = list(paths)
            on_progress(0, len(paths))
        for path in paths:
            stat = path.stat()
            within_scope = str(path.relative_to(scope_root)).replace("\\", "/")
            relative_path = "/".join(
                part for part in (prefix, within_scope) if part
            )
            manifest.append(
                {
                    "relative_path": relative_path,
                    "size_bytes": stat.st_size,
                    "quick_fingerprint": quick_fingerprint(path),
                    "mtime_ns": stat.st_mtime_ns,
                    "file_identity": (
                        f"{stat.st_dev}:{stat.st_ino}" if stat.st_ino else ""
                    ),
                }
            )
            if on_progress is not None:
                on_progress(len(manifest), len(paths))
    except OSError as error:
        return False, [], f"{type(error).__name__}: {error}"[-2000:]
    manifest.sort(key=lambda item: item["relative_path"])
    return True, manifest, None


def _filesystem_scope_state(row, *, on_progress=None) -> dict:
    online, manifest, error = (_filesystem_scope_manifest(row) if on_progress is None
                               else _filesystem_scope_manifest(row, on_progress=on_progress))
    return {
        "online": online,
        "error": error,
        "manifest": manifest,
        "state_digest": _stable_digest(
            {"online": online, "error": error, "assets": manifest}
        ),
    }


def _baseline_assets(snapshot: dict | None) -> dict[str, dict]:
    if snapshot is None:
        return {}
    result = {}
    for item in snapshot["summary"].get("assets", []):
        relative_path = _normalized_location_relative(
            item.get("relative_path", "")
        )
        if relative_path:
            result[relative_path] = item
    return result


def _change_summary(paths) -> dict:
    ordered = sorted(paths)
    return {"count": len(ordered), "paths": ordered}


def _scope_sync_preview(db, row, *, on_progress=None) -> dict:
    snapshot = None
    if not row["pending_configuration"]:
        snapshot = _latest_scope_snapshot(db, row["scope_id"])
    catalog_online = bool(row["online"])
    baseline_state = _snapshot_state(snapshot)
    if row["pending_removal"]:
        return {
            "scope_id": row["scope_id"],
            "library_id": row["library_id"],
            "source_mode": row["source_mode"],
            "relative_path": _normalized_relative(row["relative_path"]),
            "priority": row["priority"],
            "online": catalog_online,
            "catalog_online": catalog_online,
            "health_changed": False,
            "error": None,
            "pending_configuration": False,
            "pending_removal": True,
            "baseline_missing": snapshot is None,
            "baseline_snapshot_id": (
                None if snapshot is None else snapshot["snapshot_id"]
            ),
            "baseline_digest": (
                None
                if snapshot is None
                else hashlib.sha256(
                    snapshot["summary_json"].encode("utf-8")
                ).hexdigest()
            ),
            "change_status": "removed_scope",
            "added": _change_summary(()),
            "changed": _change_summary(()),
            "missing": _change_summary(()),
            "proposed": {
                "asset_count": 0,
                "total_size_bytes": 0,
                "state_digest": _stable_digest(
                    {
                        "removed_scope": True,
                        "scope_id": row["scope_id"],
                        "library_id": row["library_id"],
                    }
                ),
            },
            "changed_any": True,
            "_source": {
                "mount_path": row["mount_path"],
                "purpose": row["purpose"],
                "is_default_generic": bool(row["is_default_generic"]),
            },
            "_baseline": baseline_state,
            "_filesystem": {
                "online": catalog_online,
                "error": None,
                "manifest": [],
                "state_digest": _stable_digest(
                    {
                        "removed_scope": True,
                        "scope_id": row["scope_id"],
                        "library_id": row["library_id"],
                    }
                ),
            },
        }
    filesystem = (_filesystem_scope_state(row) if on_progress is None
                  else _filesystem_scope_state(row, on_progress=on_progress))
    online = filesystem["online"]
    manifest = filesystem["manifest"]
    error = filesystem["error"]
    baseline = _baseline_assets(snapshot)
    proposed = {item["relative_path"]: item for item in manifest}
    if online:
        added = proposed.keys() - baseline.keys()
        missing = baseline.keys() - proposed.keys()
        modified = {
            path
            for path in proposed.keys() & baseline.keys()
            if (
                proposed[path]["size_bytes"] != baseline[path].get("size_bytes")
                or proposed[path]["quick_fingerprint"]
                != baseline[path].get("quick_fingerprint")
            )
        }
    else:
        added = missing = modified = set()

    baseline_missing = snapshot is None and not row["pending_configuration"]
    changed = bool(
        row["pending_configuration"]
        or baseline_missing
        or added
        or modified
        or missing
    )
    if not online:
        change_status = "offline"
    elif row["pending_configuration"]:
        change_status = "new_scope"
    elif changed:
        change_status = "changed"
    else:
        change_status = "unchanged"
    return {
        "scope_id": row["scope_id"],
        "library_id": row["library_id"],
        "source_mode": row["source_mode"],
        "relative_path": _normalized_relative(row["relative_path"]),
        "priority": row["priority"],
        "online": online,
        "catalog_online": catalog_online,
        "health_changed": online != catalog_online,
        "error": error,
        "pending_configuration": bool(row["pending_configuration"]),
        "pending_removal": False,
        "baseline_missing": baseline_missing,
        "baseline_snapshot_id": None if snapshot is None else snapshot["snapshot_id"],
        "baseline_digest": None
        if snapshot is None
        else hashlib.sha256(snapshot["summary_json"].encode("utf-8")).hexdigest(),
        "change_status": change_status,
        "added": _change_summary(added),
        "changed": _change_summary(modified),
        "missing": _change_summary(missing),
        "proposed": {
            "asset_count": len(manifest),
            "total_size_bytes": sum(item["size_bytes"] for item in manifest),
            "state_digest": filesystem["state_digest"],
        },
        "changed_any": changed,
        "_source": {
            "mount_path": row["mount_path"],
            "purpose": row["purpose"],
            "is_default_generic": bool(row["is_default_generic"]),
        },
        "_baseline": baseline_state,
        "_filesystem": filesystem,
    }


def _preview_health(scopes: list[dict]) -> dict:
    blocking = []
    warnings = []
    primary = next(
        (scope for scope in scopes if scope["source_mode"] == "primary"),
        None,
    )
    if primary is None:
        blocking.append(
            {
                "code": "PRIMARY_SOURCE_MISSING",
                "library_id": None,
                "message": "任务没有主素材目录",
            }
        )
    elif not primary["online"]:
        blocking.append(
            {
                "code": "PRIMARY_LIBRARY_OFFLINE",
                "library_id": primary["library_id"],
                "message": "主素材目录当前不可访问",
            }
        )
    for scope in scopes:
        if (
            scope["pending_removal"]
            or scope["source_mode"] == "primary"
            or scope["online"]
        ):
            continue
        warnings.append(
            {
                "code": "GENERIC_LIBRARY_OFFLINE",
                "library_id": scope["library_id"],
                "source_mode": scope["source_mode"],
                "message": "补充素材目录当前不可访问",
            }
        )
    status = "blocking" if blocking else ("warning" if warnings else "healthy")
    return {"status": status, "blocking": blocking, "warnings": warnings}


def _configuration_preview(scopes: list[dict]) -> dict:
    fields = (
        "scope_id",
        "library_id",
        "source_mode",
        "relative_path",
        "priority",
    )
    return {
        "additions": [
            {field: scope[field] for field in fields}
            for scope in scopes
            if scope["pending_configuration"]
        ],
        "removals": [
            {field: scope[field] for field in fields}
            for scope in scopes
            if scope["pending_removal"]
        ],
    }


def _default_generic_library_ids(db) -> list[str]:
    return [
        row["library_id"]
        for row in db.connection.execute(
            """
            SELECT library_id FROM libraries
            WHERE purpose='generic' AND is_default_generic=1
            ORDER BY library_id
            """
        ).fetchall()
    ]


def _scope_proposal_identity(scopes) -> list[dict]:
    fields = (
        "scope_id",
        "library_id",
        "source_mode",
        "relative_path",
        "priority",
        "pending_configuration",
        "pending_removal",
    )
    identities = []
    for scope in scopes:
        identity = {field: scope[field] for field in fields}
        identity["relative_path"] = _normalized_relative(
            identity["relative_path"]
        )
        identities.append(identity)
    return sorted(identities, key=_canonical_json)


def _verified_media_state(db, scopes) -> list[dict]:
    states = []
    for scope in scopes:
        catalog = None
        if scope["online"] and not scope["pending_removal"]:
            catalog = _catalog_scope_state(db, scope)
        states.append(
            {
                "scope": _scope_proposal_identity([scope])[0],
                "source": dict(scope["_source"]),
                "health": {
                    "online": scope["online"],
                    "catalog_online": scope["catalog_online"],
                    "health_changed": scope["health_changed"],
                    "error": scope["error"],
                },
                "baseline": (
                    None
                    if scope["_baseline"] is None
                    else dict(scope["_baseline"])
                ),
                "filesystem": {
                    "online": scope["_filesystem"]["online"],
                    "error": scope["_filesystem"]["error"],
                    "state_digest": scope["_filesystem"]["state_digest"],
                    "manifest": [
                        dict(item) for item in scope["_filesystem"]["manifest"]
                    ],
                },
                "catalog": catalog,
            }
        )
    return sorted(states, key=lambda item: _canonical_json(item["scope"]))


def _source_commit_guard(
    task_id: str,
    *,
    source_revision: int | None,
    include_default_generic: bool,
    default_generic_library_ids: list[str],
    scopes,
) -> dict:
    proposal = _scope_proposal_identity(scopes)
    return {
        "task_id": task_id,
        "source_revision": source_revision,
        "include_default_generic": include_default_generic,
        "default_generic_library_ids": list(default_generic_library_ids),
        "active_scopes": [
            scope for scope in proposal if not scope["pending_configuration"]
        ],
        "proposal": proposal,
    }


def _current_source_commit_guard(db, task_id: str, *, scopes=None) -> dict:
    setting = db.connection.execute(
        """
        SELECT revision, include_default_generic
        FROM task_source_settings WHERE task_id=?
        """,
        (task_id,),
    ).fetchone()
    return _source_commit_guard(
        task_id,
        source_revision=None if setting is None else setting["revision"],
        include_default_generic=bool(
            setting is not None and setting["include_default_generic"]
        ),
        default_generic_library_ids=_default_generic_library_ids(db),
        scopes=_preview_scope_rows(db, task_id) if scopes is None else scopes,
    )


def _health_changes(scopes: list[dict]) -> list[dict]:
    changes = []
    seen = set()
    for scope in scopes:
        library_id = scope["library_id"]
        if (
            scope["pending_removal"]
            or not scope["health_changed"]
            or library_id in seen
        ):
            continue
        seen.add(library_id)
        changes.append(
            {
                "library_id": library_id,
                "source_mode": scope["source_mode"],
                "from_online": scope["catalog_online"],
                "to_online": scope["online"],
            }
        )
    return changes


def _preview_source_sync(
    db,
    task_id: str,
    *,
    workers: int = 4,
    include_commit_guard: bool = False,
    on_progress=None,
) -> dict:
    """Read the bound folders and describe sync impact without durable writes."""
    if workers < 1:
        raise ValueError("workers 必须大于 0")
    _require_task(db, task_id)
    setting = db.connection.execute(
        """
        SELECT revision, include_default_generic
        FROM task_source_settings WHERE task_id=?
        """,
        (task_id,),
    ).fetchone()
    scope_previews = [
        (_scope_sync_preview(db, row) if on_progress is None else
         _scope_sync_preview(db, row, on_progress=on_progress))
        for row in _preview_scope_rows(db, task_id)
    ]
    health = _preview_health(scope_previews)
    configuration = _configuration_preview(scope_previews)
    health_changes = _health_changes(scope_previews)
    default_generic_library_ids = _default_generic_library_ids(db)
    changed = any(scope["changed_any"] for scope in scope_previews)
    health_changed = bool(health_changes)
    locked = _picture_lock_exists(db, task_id)
    token_payload = {
        "version": 2,
        "task_id": task_id,
        "source_revision": None if setting is None else setting["revision"],
        "include_default_generic": bool(
            setting is not None and setting["include_default_generic"]
        ),
        "default_generic_library_ids": default_generic_library_ids,
        "configuration": configuration,
        "scopes": [
            {
                "scope_id": scope["scope_id"],
                "library_id": scope["library_id"],
                "source_mode": scope["source_mode"],
                "relative_path": scope["relative_path"],
                "priority": scope["priority"],
                "pending_configuration": scope["pending_configuration"],
                "pending_removal": scope["pending_removal"],
                "online": scope["online"],
                "baseline_snapshot_id": scope["baseline_snapshot_id"],
                "baseline_digest": scope["baseline_digest"],
                "proposed": scope["proposed"],
            }
            for scope in scope_previews
        ],
    }
    public_scopes = []
    for scope in scope_previews:
        public_scope = dict(scope)
        for field in tuple(public_scope):
            if field == "changed_any" or field.startswith("_"):
                public_scope.pop(field)
        public_scopes.append(public_scope)
    result = {
        "task_id": task_id,
        "changed": changed,
        "health_changed": health_changed,
        "no_op": not changed and not health_changed,
        "scopes": public_scopes,
        "configuration": configuration,
        "health_changes": health_changes,
        "health": health,
        "invalidation": {
            "will_invalidate_picture_lock": bool(locked and changed),
            "from_stage": "material_sync" if locked and changed else None,
        },
        "impact_token": _stable_digest(token_payload) if changed else None,
    }
    if include_commit_guard:
        result["_commit_guard"] = _source_commit_guard(
            task_id,
            source_revision=None if setting is None else setting["revision"],
            include_default_generic=bool(
                setting is not None and setting["include_default_generic"]
            ),
            default_generic_library_ids=default_generic_library_ids,
            scopes=scope_previews,
        )
        result["_verified_media_state"] = _verified_media_state(
            db,
            scope_previews,
        )
    return result


def list_eligible_assets(db, task_id: str) -> list[dict]:
    """Return deduplicated visual candidates inside this task's active source scopes."""
    _require_task(db, task_id)
    assets: dict[str, dict] = {}
    library_rows: dict[str, list[dict]] = {}
    mount_paths: dict[str, str] = {}
    for row in _active_scope_rows(db, task_id):
        if not bool(row["online"]) or not _scope_root(
            row["mount_path"], row["relative_path"]
        ).is_dir():
            continue
        mount_paths[row["library_id"]] = row["mount_path"]
        if row["library_id"] not in library_rows:
            library_rows[row["library_id"]] = [
                dict(item) for item in db.connection.execute(
                    """
                    SELECT
                      al.asset_id,
                      al.relative_path,
                      a.size_bytes,
                      a.quick_fingerprint,
                      a.media_kind,
                      al.file_identity,
                      ta.role AS task_role
                    FROM asset_locations AS al
                    JOIN assets AS a USING(asset_id)
                    LEFT JOIN task_assets AS ta
                      ON ta.task_id=? AND ta.asset_id=al.asset_id
                    WHERE al.library_id=? AND al.state='available'
                    ORDER BY al.relative_path, al.location_id
                    """,
                    (task_id, row["library_id"]),
                ).fetchall()
            ]
        for asset in library_rows[row["library_id"]]:
            if not _is_within_scope(asset["relative_path"], row["relative_path"]):
                continue
            item = assets.setdefault(
                asset["asset_id"],
                {
                    **asset,
                    "library_id": row["library_id"],
                    "library_name": row["name"],
                    "source_mode": row["source_mode"],
                },
            )
            if row["source_mode"] == "primary":
                item["library_id"] = row["library_id"]
                item["library_name"] = row["name"]
                item["source_mode"] = "primary"
    ordered = sorted(
        assets.values(),
        key=lambda item: (
            item["source_mode"] != "primary",
            item["library_name"],
            item["relative_path"],
        ),
    )
    unique = []
    seen_paths = set()
    seen_identities = set()
    for item in ordered:
        path = os.path.normcase(os.path.abspath(
            os.path.join(mount_paths[item["library_id"]], item["relative_path"])
        ))
        identity = item.get("file_identity")
        if path in seen_paths or (identity and identity in seen_identities):
            continue
        seen_paths.add(path)
        if identity:
            seen_identities.add(identity)
        unique.append({key: value for key, value in item.items() if key != "file_identity"})
    return unique


def preview_source_sync(db, task_id: str, *, workers: int = 4, on_progress=None) -> dict:
    if on_progress is None:
        return _preview_source_sync(db, task_id, workers=workers)
    return _preview_source_sync(db, task_id, workers=workers, on_progress=on_progress)


def source_readiness(db, task_id: str, *, workers: int = 4, on_progress=None) -> dict:
    """Read-only source gate shared by the storyboard UI and execution paths."""
    preview = (preview_source_sync(db, task_id, workers=workers) if on_progress is None else
               preview_source_sync(db, task_id, workers=workers, on_progress=on_progress))
    if preview["health"]["status"] == "blocking":
        reason = "primary_offline"
    elif preview["changed"]:
        reason = "source_changed"
    elif preview["health_changed"]:
        reason = "source_health_changed"
    else:
        reason = "ready"
    return {**preview, "ready": reason == "ready", "reason": reason}


def initial_source_check_pending(db, task_id: str) -> bool:
    """The creation scan must finish before later work consumes its scope."""
    row = db.connection.execute(
        "SELECT j.status FROM task_source_checks c JOIN jobs j ON j.job_id=c.job_id WHERE c.task_id=?", (task_id,)
    ).fetchone()
    return row is not None and row["status"] in {"queued", "running", "pending"}


def require_source_readiness(db, task_id: str) -> dict:
    """Reject stale or disconnected scopes before queued planning consumes them."""
    if initial_source_check_pending(db, task_id):
        raise RuntimeError(
            "SOURCE_CHECK_PENDING: 创建后的素材检查尚未完成，请稍后重试"
        )
    readiness = source_readiness(db, task_id)
    if not readiness["ready"]:
        raise RuntimeError(
            "SOURCE_SYNC_REQUIRED: 素材检查尚未通过，请先处理变化或连接异常"
        )
    return readiness


def require_bound_source_readiness(db, task_id: str) -> dict | None:
    """Guard current scoped tasks while preserving legacy tasks with no bound scope."""
    if not task_has_source_configuration(db, task_id):
        return None
    return require_source_readiness(db, task_id)


def _scan_source_rows(db, rows: list[dict], preview: dict) -> None:
    scope_previews = {
        scope["scope_id"]: scope
        for scope in preview["scopes"]
        if not scope["pending_removal"]
    }
    primary = next(
        (
            scope
            for scope in scope_previews.values()
            if scope["source_mode"] == "primary"
        ),
        None,
    )
    if primary is None:
        raise LibraryOffline("主素材目录离线，任务已阻断：任务没有主素材目录")
    if not primary["online"]:
        try:
            scan_library(db, primary["library_id"])
        except LibraryOffline as error:
            raise LibraryOffline(
                f"主素材目录离线，任务已阻断：{error}"
            ) from error

    if preview["changed"]:
        scan_library_ids = {
            row["library_id"] for row in rows if not row["pending_removal"]
        }
    else:
        scan_library_ids = {
            item["library_id"] for item in preview["health_changes"]
        }

    scanned = set()
    for row in rows:
        if row["pending_removal"]:
            continue
        library_id = row["library_id"]
        if library_id not in scan_library_ids or library_id in scanned:
            continue
        try:
            scan_library(db, library_id)
        except LibraryOffline as error:
            if row["source_mode"] == "primary":
                raise LibraryOffline(
                    f"主素材目录离线，任务已阻断：{error}"
                ) from error
        scanned.add(library_id)


def _verified_post_scan_preview(
    db,
    task_id: str,
    before: dict,
    impact_token: str | None,
    *,
    workers: int,
) -> dict:
    after = _preview_source_sync(
        db,
        task_id,
        workers=workers,
        include_commit_guard=True,
    )
    if before["changed"]:
        if (
            after["impact_token"] is None
            or not hmac.compare_digest(impact_token or "", after["impact_token"])
        ):
            raise ValueError(
                "扫描期间素材或默认目录集合发生变化，请重新预检并确认"
            )
    elif after["changed"]:
        raise ValueError("扫描期间检测到素材变化，请重新预检并确认")
    return after


def _pending_task_snapshots(
    db,
    rows: list[dict],
    scope_previews: dict,
    verified_media_state: dict,
):
    additions = []
    snapshots = []
    for row in rows:
        if row["pending_removal"]:
            continue
        scope_preview = scope_previews[row["scope_id"]]
        if row["pending_configuration"]:
            scope_id = "scope_" + uuid.uuid4().hex
            additions.append((row, scope_id))
        else:
            scope_id = row["scope_id"]
        if not scope_preview["online"]:
            continue
        frozen_catalog = verified_media_state[row["scope_id"]]["catalog"]
        if frozen_catalog is None:
            raise RuntimeError(
                "confirmed source proposal is missing its frozen catalog state"
            )
        summary_json = frozen_catalog["summary_json"]
        asset_count = frozen_catalog["asset_count"]
        total_size = frozen_catalog["total_size_bytes"]
        previous = (
            None
            if row["pending_configuration"]
            else _latest_scope_snapshot(db, scope_id)
        )
        if previous is not None and previous["summary_json"] == summary_json:
            continue
        snapshots.append(
            (
                "source_snapshot_" + uuid.uuid4().hex,
                scope_id,
                summary_json,
                asset_count,
                total_size,
            )
        )
    return additions, snapshots


def _assert_verified_media_state(db, rows, verified_states) -> None:
    current_by_scope = {
        item["scope_id"]: item
        for item in rows
    }
    verified_by_scope = {
        item["scope"]["scope_id"]: item
        for item in verified_states
    }
    if set(current_by_scope) != set(verified_by_scope):
        raise ValueError(
            "impact_token 已过期：素材范围在提交前发生变化，请重新预检并确认"
        )

    for scope_id, verified in verified_by_scope.items():
        current = current_by_scope[scope_id]
        if (
            _scope_proposal_identity([current])[0] != verified["scope"]
            or {
                "mount_path": current["mount_path"],
                "purpose": current["purpose"],
                "is_default_generic": bool(current["is_default_generic"]),
            }
            != verified["source"]
        ):
            raise ValueError(
                "impact_token 已过期：素材范围在提交前发生变化，请重新预检并确认"
            )
        if not verified["scope"]["pending_configuration"]:
            current_baseline = _snapshot_state(
                _latest_scope_snapshot(db, scope_id)
            )
            if current_baseline != verified["baseline"]:
                raise ValueError(
                    "impact_token 已过期：素材同步基线在提交前发生变化，请重新预检并确认"
                )
        if verified["scope"]["pending_removal"]:
            continue

        current_filesystem = _filesystem_scope_state(current)
        if (
            not hmac.compare_digest(
                current_filesystem["state_digest"],
                verified["filesystem"]["state_digest"],
            )
            or current_filesystem != verified["filesystem"]
        ):
            raise ValueError(
                "impact_token 已过期：素材文件在提交前发生变化，请重新预检并确认"
            )

        frozen_catalog = verified["catalog"]
        if frozen_catalog is None:
            if current_filesystem["online"]:
                raise ValueError(
                    "impact_token 已过期：素材目录状态在提交前发生变化，请重新预检并确认"
                )
            continue
        if not current_filesystem["online"]:
            raise ValueError(
                "impact_token 已过期：素材目录状态在提交前发生变化，请重新预检并确认"
            )
        current_catalog = _catalog_scope_state(db, current)
        if (
            not hmac.compare_digest(
                current_catalog["digest"],
                frozen_catalog["digest"],
            )
            or current_catalog != frozen_catalog
        ):
            raise ValueError(
                "impact_token 已过期：素材主账本在提交前发生变化，请重新预检并确认"
            )


def _commit_confirmed_source_sync(
    db,
    task_id: str,
    post_scan_preview: dict,
) -> tuple[bool, bool, list[str]]:
    rows = [dict(scope) for scope in post_scan_preview["scopes"]]
    scope_previews = {
        scope["scope_id"]: scope for scope in post_scan_preview["scopes"]
    }
    verified_guard = post_scan_preview.get("_commit_guard")
    if verified_guard is None:
        raise RuntimeError("confirmed source proposal is missing its commit guard")
    verified_states = post_scan_preview.get("_verified_media_state")
    if verified_states is None:
        raise RuntimeError("confirmed source proposal is missing verified media state")
    verified_by_scope = {
        item["scope"]["scope_id"]: item
        for item in verified_states
    }
    with db.transaction(immediate=True) as connection:
        current_rows = _preview_scope_rows(db, task_id)
        current_guard = _current_source_commit_guard(
            db,
            task_id,
            scopes=current_rows,
        )
        if not hmac.compare_digest(
            _stable_digest(verified_guard),
            _stable_digest(current_guard),
        ):
            raise ValueError(
                "impact_token 已过期：素材来源配置在提交前发生变化，请重新预检并确认"
            )
        _assert_verified_media_state(
            db,
            current_rows,
            verified_states,
        )

        removals = [row for row in rows if row["pending_removal"]]
        proposed = [
            (
                row["source_mode"],
                row["library_id"],
                row["priority"],
                _normalized_relative(row["relative_path"]),
            )
            for row in rows
            if not row["pending_removal"]
        ]
        _validate_existing_references(db, task_id, proposed)
        additions, snapshots = _pending_task_snapshots(
            db,
            rows,
            scope_previews,
            verified_by_scope,
        )
        configuration_changed = bool(removals or additions)
        changed = bool(configuration_changed or snapshots)
        snapshot_ids = [snapshot[0] for snapshot in snapshots]
        if not changed:
            return False, False, []

        locked = _picture_lock_exists(db, task_id)
        now = connection.execute("SELECT datetime('now')").fetchone()[0]
        with db.task_source_write_authorization(task_id):
            for row in removals:
                connection.execute(
                    """
                    UPDATE task_source_scopes
                    SET active=0, retired_at=?, updated_at=?
                    WHERE scope_id=? AND task_id=? AND active=1
                    """,
                    (now, now, row["scope_id"], task_id),
                )
            for row, scope_id in additions:
                connection.execute(
                    """
                    INSERT INTO task_source_scopes(
                      scope_id, task_id, library_id, role, relative_path,
                      source_mode, active, priority, created_at, updated_at
                    ) VALUES(?, ?, ?, 'supplementary', ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        scope_id,
                        task_id,
                        row["library_id"],
                        _normalized_relative(row["relative_path"]),
                        row["source_mode"],
                        row["priority"],
                        now,
                        now,
                    ),
                )
            if configuration_changed:
                connection.execute(
                    """
                    UPDATE task_source_settings
                    SET revision=revision + 1, updated_at=?
                    WHERE task_id=?
                    """,
                    (now, task_id),
                )
            for (
                snapshot_id,
                scope_id,
                summary_json,
                asset_count,
                total_size,
            ) in snapshots:
                connection.execute(
                    """
                    INSERT INTO task_source_snapshots(
                      snapshot_id, task_id, scope_id, summary_json,
                      asset_count, total_size_bytes, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        task_id,
                        scope_id,
                        summary_json,
                        asset_count,
                        total_size,
                        now,
                    ),
                )
            if locked:
                connection.execute(
                    """
                    UPDATE picture_lock SET changed_since_lock=1
                    WHERE task_id=?
                    """,
                    (task_id,),
                )
                workflow.invalidate_from_connection(
                    connection,
                    task_id,
                    "material_sync",
                    reason="task source summary changed",
                )
    return changed, configuration_changed, snapshot_ids


def sync_task_sources(
    db,
    task_id: str,
    *,
    workers: int = 4,
    impact_token: str | None = None,
) -> dict:
    if workers < 1:
        raise ValueError("workers 必须大于 0")
    preview = preview_source_sync(db, task_id, workers=workers)
    rows = _preview_scope_rows(db, task_id)
    primary = next(
        (
            scope
            for scope in preview["scopes"]
            if scope["source_mode"] == "primary"
            and not scope["pending_removal"]
        ),
        None,
    )
    if primary is None or not primary["online"]:
        _scan_source_rows(db, rows, preview)
        detail = preview["health"]["blocking"][0]["message"]
        raise LibraryOffline(f"主素材目录离线，任务已阻断：{detail}")
    if preview["changed"]:
        if impact_token is None:
            raise ValueError("检测到素材变化，请先预检并确认影响")
        if not isinstance(impact_token, str) or not hmac.compare_digest(
            impact_token, preview["impact_token"]
        ):
            raise ValueError("impact_token 已过期或不匹配，请重新预检")
    elif not preview["health_changed"]:
        return {
            "changed": False,
            "configuration_changed": False,
            "health_changed": False,
            "health_changes": [],
            "snapshot_ids": [],
            "sources": get_task_sources(db, task_id),
            "health": preview["health"],
        }
    health_changes = preview["health_changes"]
    _scan_source_rows(db, rows, preview)
    post_scan_preview = _verified_post_scan_preview(
        db,
        task_id,
        preview,
        impact_token,
        workers=workers,
    )
    changed, configuration_changed, snapshot_ids = _commit_confirmed_source_sync(
        db,
        task_id,
        post_scan_preview,
    )
    return {
        "changed": changed,
        "configuration_changed": configuration_changed,
        "health_changed": bool(health_changes),
        "health_changes": health_changes,
        "snapshot_ids": snapshot_ids,
        "sources": get_task_sources(db, task_id),
        "health": source_health(db, task_id),
    }


def task_has_source_configuration(db, task_id: str) -> bool:
    return db.connection.execute(
        "SELECT 1 FROM task_source_settings WHERE task_id=?", (task_id,)
    ).fetchone() is not None


def scoped_asset_location(db, task_id: str, asset_id: str) -> dict | None:
    rows = db.connection.execute(
        """
        SELECT
          al.relative_path,
          al.library_id,
          s.relative_path AS scope_relative_path,
          s.source_mode,
          s.priority
        FROM task_source_scopes s
        JOIN asset_locations al USING(library_id)
        WHERE s.task_id=? AND s.active=1
          AND al.asset_id=? AND al.state='available'
        ORDER BY s.priority, al.location_id
        """,
        (task_id, asset_id),
    ).fetchall()
    for row in rows:
        if _is_within_scope(row["relative_path"], row["scope_relative_path"]):
            return {
                "relative_path": row["relative_path"],
                "library_id": row["library_id"],
                "source_mode": row["source_mode"],
            }
    return None
