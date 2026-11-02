"""Register media libraries independently from their current mount paths."""

from pathlib import Path


VALID_LIBRARY_KINDS = {"local", "nas"}
VALID_LIBRARY_PURPOSES = {"task_specific", "generic", "packaging"}


def list_libraries(db, purpose: str | None = None) -> list[dict]:
    if purpose is not None and purpose not in VALID_LIBRARY_PURPOSES:
        raise ValueError("素材库用途过滤无效")
    sql = (
        "SELECT l.library_id,l.name,l.kind,l.mount_path,l.online,l.purpose,"
        "l.is_default_generic,l.created_at,l.updated_at,"
        "(SELECT COUNT(DISTINCT al.asset_id) FROM asset_locations al "
        " WHERE al.library_id=l.library_id AND al.state='available') AS asset_count,"
        "(SELECT MAX(j.updated_at) FROM jobs j WHERE j.library_id=l.library_id "
        " AND j.status='completed' AND j.job_type IN ('scan','probe')) AS last_sync_at "
        "FROM libraries l"
    )
    params: tuple = ()
    if purpose is not None:
        sql += " WHERE l.purpose=?"
        params = (purpose,)
    sql += " ORDER BY l.purpose,l.name,l.library_id"
    return [dict(row) for row in db.connection.execute(sql, params).fetchall()]


def register_library(
    db,
    library_id: str,
    name: str,
    kind: str,
    mount_path: str,
    *,
    purpose: str | None = None,
    is_default_generic: bool | None = None,
) -> None:
    if not library_id.strip():
        raise ValueError("素材库 ID 不能为空")
    if not name.strip():
        raise ValueError("素材库名称不能为空")
    if kind not in VALID_LIBRARY_KINDS:
        raise ValueError("素材库类型只能是 local 或 nas")
    existing = db.connection.execute(
        """
        SELECT purpose, is_default_generic FROM libraries WHERE library_id=?
        """,
        (library_id,),
    ).fetchone()
    purpose_value = (
        purpose
        if purpose is not None
        else (existing["purpose"] if existing is not None else "task_specific")
    )
    if purpose_value not in VALID_LIBRARY_PURPOSES:
        raise ValueError(
            "素材库用途只能是 task_specific、generic 或 packaging"
        )
    if is_default_generic is None:
        default_value = (
            bool(existing["is_default_generic"])
            if existing is not None and purpose_value == "generic"
            else False
        )
    else:
        default_value = bool(is_default_generic)
    if default_value and purpose_value != "generic":
        raise ValueError("只有通用镜头素材库可设为默认通用")
    normalized_mount = str(Path(mount_path))
    now = db.connection.execute("SELECT datetime('now')").fetchone()[0]
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO libraries(
              library_id, name, kind, mount_path, online,
              purpose, is_default_generic, created_at, updated_at
            ) VALUES(?, ?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(library_id) DO UPDATE SET
              name=excluded.name,
              kind=excluded.kind,
              mount_path=excluded.mount_path,
              purpose=excluded.purpose,
              is_default_generic=excluded.is_default_generic,
              updated_at=excluded.updated_at
            """,
            (
                library_id,
                name,
                kind,
                normalized_mount,
                purpose_value,
                int(default_value),
                now,
                now,
            ),
        )


def update_mount(db, library_id: str, mount_path: str) -> None:
    with db.transaction() as connection:
        changed = connection.execute(
            """
            UPDATE libraries
            SET mount_path=?, updated_at=datetime('now')
            WHERE library_id=?
            """,
            (str(Path(mount_path)), library_id),
        ).rowcount
    if changed != 1:
        raise KeyError(f"找不到素材库：{library_id}")


def resolve_location(db, asset_id: str) -> Path:
    row = db.connection.execute(
        """
        SELECT l.mount_path, al.relative_path
        FROM asset_locations al
        JOIN libraries l USING(library_id)
        WHERE al.asset_id=? AND al.state='available'
        ORDER BY
          l.online DESC,
          CASE l.kind WHEN 'local' THEN 0 ELSE 1 END,
          al.location_id
        LIMIT 1
        """,
        (asset_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"找不到可用素材位置：{asset_id}")
    return Path(row["mount_path"]) / Path(row["relative_path"])
