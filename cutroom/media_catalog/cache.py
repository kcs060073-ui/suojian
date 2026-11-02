"""P1-F 缓存安全清理：预览产物都是可重建的派生文件，设上限防撑爆硬盘。

铁律：只删 preview_cache 表里登记过的、且确实存在于 cache 目录的文件。
原片（asset_locations）、成片（out/）、数据库一律不碰。删除按"最旧优先"，
把占用压到预算以内。删前会报告将要删什么、释放多少，可预览后再执行。
"""

from pathlib import Path
from collections.abc import Callable
import math
import os
import stat

ALL_KINDS = ("thumbnail", "contact_sheet", "proxy", "audio_preview")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def cache_dir(db) -> Path:
    return db.path.parent / "cache"


def _is_reparse_path(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _reject_reparse_components(directory: Path, relative: Path | None = None) -> None:
    root = directory.absolute()
    candidates = [root]
    if relative is not None:
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            candidates.append(cursor)
    for candidate in candidates:
        if _is_reparse_path(candidate):
            raise ValueError("缓存目录或文件不能经过符号链接或重解析点")


def _cache_entry_path(directory: Path, rel_path: str) -> Path:
    relative = Path(rel_path)
    if relative.is_absolute():
        raise ValueError("缓存路径不能是绝对路径")
    _reject_reparse_components(directory, relative)
    root = directory.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("缓存路径超出cache目录") from error
    return candidate


def _recorded_size(row) -> tuple[int, str | None]:
    raw_size = row["size_bytes"] or 0
    try:
        size = math.ceil(raw_size) if isinstance(raw_size, float) else int(raw_size)
    except (OverflowError, TypeError, ValueError) as error:
        return 0, f"preview_cache.size_bytes 无法解析: {error}"
    if size < 0:
        return 0, f"preview_cache.size_bytes 不能为负数: {size}"
    return size, None


def cache_status(db) -> dict:
    rows = db.connection.execute(
        "SELECT asset_id, kind, rel_path, size_bytes, generated_at FROM preview_cache "
        "ORDER BY generated_at"
    ).fetchall()
    directory = cache_dir(db)
    total = 0
    orphan_db_rows = 0
    missing_files = 0
    by_kind: dict[str, int] = {}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        full = directory / r["rel_path"]
        if full.exists():
            total += full.stat().st_size
        else:
            missing_files += 1
    return {
        "cache_dir": str(directory),
        "entries": len(rows),
        "by_kind": by_kind,
        "total_bytes": total,
        "missing_files": missing_files,
        "exists": directory.exists(),
    }


def _candidates(db, keep_kinds: set[str] | None):
    rows = db.connection.execute(
        "SELECT asset_id, kind, rel_path, size_bytes, generated_at FROM preview_cache "
        "ORDER BY generated_at"
    ).fetchall()
    out = []
    for r in rows:
        if keep_kinds is not None and r["kind"] in keep_kinds:
            continue
        out.append(r)
    return out


def clean_cache(
    db,
    max_bytes: int | None = None,
    keep_kinds: set[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """删除最旧的缓存文件，直到总占用 <= max_bytes（或全清如果 max_bytes 为 None）。

    返回被删条目与释放字节数。dry_run=True 只报告不真删。
    """
    directory = cache_dir(db)
    _reject_reparse_components(directory)
    if keep_kinds is not None:
        unknown = set(keep_kinds) - set(ALL_KINDS)
        if unknown:
            raise ValueError(f"不支持的缓存类型：{sorted(unknown)}")
    rows = _candidates(db, None)
    # 现存文件大小重新统计，按最旧优先；缺失文件仍需清理陈旧索引。
    planned = []
    failed: list[dict] = []
    current = 0
    remaining_estimated = False
    for r in rows:
        try:
            full = _cache_entry_path(directory, r["rel_path"])
        except (OSError, ValueError) as error:
            recorded_size, size_error = _recorded_size(r)
            current += recorded_size
            remaining_estimated = True
            failure_error = f"{type(error).__name__}: {error}"
            if size_error:
                failure_error = f"{failure_error}; {size_error}"
            failed.append(
                {
                    "asset_id": r["asset_id"],
                    "kind": r["kind"],
                    "rel_path": r["rel_path"],
                    "size_bytes": recorded_size,
                    "error": failure_error,
                }
            )
            continue
        try:
            size = full.stat().st_size
        except FileNotFoundError:
            size = 0
        except OSError as error:
            recorded_size, size_error = _recorded_size(r)
            current += recorded_size
            remaining_estimated = True
            failure_error = f"{type(error).__name__}: {error}"
            if size_error:
                failure_error = f"{failure_error}; {size_error}"
            failed.append(
                {
                    "asset_id": r["asset_id"],
                    "kind": r["kind"],
                    "rel_path": r["rel_path"],
                    "size_bytes": recorded_size,
                    "error": failure_error,
                }
            )
            continue
        current += size
        if keep_kinds is None or r["kind"] not in keep_kinds:
            planned.append((r, full, size))
    planned.sort(key=lambda pair: pair[0]["generated_at"])

    budget = max_bytes if max_bytes is not None else 0
    deleted: list[dict] = []
    freed = 0
    for row, full, size in planned:
        if max_bytes is not None and current - freed <= budget:
            break
        if dry_run:
            deleted.append(
                {
                    "asset_id": row["asset_id"],
                    "kind": row["kind"],
                    "rel_path": row["rel_path"],
                    "size_bytes": size,
                    "generated_at": row["generated_at"],
                }
            )
            freed += size
            continue
        try:
            if _cache_entry_path(directory, row["rel_path"]) != full:
                raise ValueError("缓存文件解析结果已变化")
            full.unlink(missing_ok=True)
        except Exception as error:
            failed.append(
                {
                    "asset_id": row["asset_id"],
                    "kind": row["kind"],
                    "rel_path": row["rel_path"],
                    "size_bytes": size,
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            continue
        with db.transaction() as connection:
            connection.execute(
                "DELETE FROM preview_cache WHERE asset_id=? AND kind=?",
                (row["asset_id"], row["kind"]),
            )
        deleted.append(
            {
                "asset_id": row["asset_id"],
                "kind": row["kind"],
                "rel_path": row["rel_path"],
                "size_bytes": size,
                "generated_at": row["generated_at"],
            }
        )
        freed += size
    return {
        "deleted": deleted,
        "failed": failed,
        "freed_bytes": freed,
        "remaining_bytes": current - freed,
        "remaining_estimated": remaining_estimated,
        "dry_run": dry_run,
    }


def clean_cache_entries(db, expected_entries: list[dict]) -> dict:
    """Delete exactly one previously previewed allowlist; never enumerate new rows."""
    directory = cache_dir(db)
    _reject_reparse_components(directory)
    deleted: list[dict] = []
    freed = 0
    validated: list[tuple[dict, Path]] = []
    try:
        with db.transaction(immediate=True) as connection:
            # Validate the complete saved allowlist before deleting any file. If one
            # entry drifted after preview, the user must preview the full impact again.
            for expected in expected_entries:
                asset_id = expected.get("asset_id")
                kind = expected.get("kind")
                rel_path = expected.get("rel_path")
                generated_at = expected.get("generated_at")
                expected_size = expected.get("size_bytes")
                if not all(
                    isinstance(value, str) and value
                    for value in (asset_id, kind, rel_path)
                ) or not isinstance(expected_size, int) or expected_size < 0:
                    raise ValueError("预览项身份无效")
                row = connection.execute(
                    "SELECT rel_path,generated_at FROM preview_cache "
                    "WHERE asset_id=? AND kind=?",
                    (asset_id, kind),
                ).fetchone()
                if (
                    row is None
                    or row["rel_path"] != rel_path
                    or row["generated_at"] != generated_at
                ):
                    raise ValueError("缓存登记已变化")
                full = _cache_entry_path(directory, rel_path)
                try:
                    actual_size = full.stat().st_size
                except FileNotFoundError:
                    actual_size = 0
                if actual_size != expected_size:
                    raise ValueError("缓存文件大小已变化")
                validated.append((dict(expected), full))

            for expected, full in validated:
                asset_id = expected["asset_id"]
                kind = expected["kind"]
                rel_path = expected["rel_path"]
                generated_at = expected.get("generated_at")
                expected_size = expected["size_bytes"]
                if _cache_entry_path(directory, rel_path) != full:
                    raise ValueError("缓存文件解析结果已变化")
                full.unlink(missing_ok=True)
                removed = connection.execute(
                    "DELETE FROM preview_cache WHERE asset_id=? AND kind=? "
                    "AND rel_path=? AND generated_at IS ?",
                    (asset_id, kind, rel_path, generated_at),
                ).rowcount
                if removed != 1:
                    raise ValueError("缓存登记已变化")
                deleted.append(expected)
                freed += expected_size
    except Exception as error:
        raise ValueError(
            "CACHE_PREVIEW_STALE: 缓存内容已变化，请重新预览后再确认"
        ) from error
    return {
        "deleted": deleted,
        "failed": [],
        "freed_bytes": freed,
        "remaining_bytes": cache_status(db)["total_bytes"],
        "remaining_estimated": False,
        "dry_run": False,
    }
