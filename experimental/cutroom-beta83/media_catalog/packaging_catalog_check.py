"""Read-only packaging asset discovery and change reporting."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

from . import packaging_plan
from .provenance import canonical_digest
from .storage_settings import personal_packaging_root


ALLOWED_SUFFIXES = {
    "audio": {".mp3", ".wav", ".ogg", ".m4a", ".aac"},
    "image": {".png", ".jpg", ".jpeg", ".webp", ".gif"},
    "video": {".mp4", ".mov", ".webm"},
    "font": {".otf", ".ttf"},
}
ALL_ALLOWED_SUFFIXES = frozenset().union(*ALLOWED_SUFFIXES.values())
PERSONAL_DIRECTORY_CATEGORIES = {
    "subtitle-presets": "subtitle",
    "voice-profiles": "voice",
    "bgm": "bgm",
    "sfx": "sfx",
    "visual-cards": "title_card",
    "stickers": "sticker",
    "fancy-text-presets": "fancy_text",
    "brand": "brand",
}


def relative_entry(layer_root: Path, candidate: Path) -> str:
    resolved_root = Path(layer_root).resolve()
    resolved = Path(candidate).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError("PATH_OUTSIDE_PACKAGING_ROOT")
    return resolved.relative_to(resolved_root).as_posix()


def _quick_signature(path: Path) -> dict:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        head = source.read(64 * 1024)
        digest.update(head)
        if stat.st_size > len(head):
            source.seek(max(0, stat.st_size - 64 * 1024))
            digest.update(source.read(64 * 1024))
    return {
        "sizeBytes": stat.st_size,
        "mtimeNs": stat.st_mtime_ns,
        "quickFingerprint": digest.hexdigest(),
    }


def _media_kind(path: Path) -> str | None:
    suffix = path.suffix.lower()
    for kind, suffixes in ALLOWED_SUFFIXES.items():
        if suffix in suffixes:
            return kind
    return None


def _decodable(path: Path, kind: str | None) -> bool:
    if kind is None or not path.is_file() or path.stat().st_size <= 0:
        return False
    if kind == "font":
        return True
    if kind == "image":
        try:
            from PIL import Image

            with Image.open(path) as image:
                image.verify()
            return True
        except (ImportError, OSError, ValueError):
            return False
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return False
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        timeout=15,
        check=False,
    )
    return result.returncode == 0


def _manifest_entries(project_root: Path) -> list[dict]:
    entries = []
    allowed_roots = tuple(
        (project_root / name).resolve()
        for name in ("assets", "public", "asset-packs")
    )
    for category, manifest in packaging_plan.load_all_manifests().items():
        for item in manifest.get("items", []):
            file_value = item.get("file")
            if not isinstance(file_value, str) or not file_value:
                continue
            path = Path(file_value)
            if not path.is_absolute():
                path = project_root / path
            resolved = path.resolve(strict=False)
            base = {
                "category": category,
                "assetRef": item.get("id"),
                "manifest": item,
            }
            if not any(
                resolved == root or root in resolved.parents
                for root in allowed_roots
            ):
                entries.append({
                    **base,
                    "path": None,
                    "relativePath": Path(file_value).name,
                    "reasonCode": "PATH_OUTSIDE_PACKAGING_ROOT",
                })
                continue
            entries.append({**base, "path": resolved})
    return entries


def _registered_key(item: dict) -> tuple:
    return (
        item.get("layer"),
        item.get("category"),
        item.get("assetRef"),
        item.get("relativePath"),
    )


def _registered_result(entry: dict, project_root: Path) -> dict:
    path = entry["path"]
    manifest = entry["manifest"]
    if entry.get("reasonCode") == "PATH_OUTSIDE_PACKAGING_ROOT":
        return {
            "layer": "registered",
            "category": entry["category"],
            "assetRef": entry["assetRef"],
            "relativePath": entry["relativePath"],
            "status": "invalid",
            "reasonCode": "PATH_OUTSIDE_PACKAGING_ROOT",
            "selectable": False,
        }
    try:
        relative = path.relative_to(project_root).as_posix()
    except ValueError:
        relative = Path(str(manifest.get("file") or path.name)).as_posix()
    base = {
        "layer": "registered",
        "category": entry["category"],
        "assetRef": entry["assetRef"],
        "relativePath": relative,
        "selectable": False,
    }
    if not path.is_file():
        return {**base, "status": "missing", "reasonCode": "PACKAGING_FILE_MISSING"}
    kind = _media_kind(path)
    if not _decodable(path, kind):
        return {**base, "status": "invalid", "reasonCode": "PACKAGING_FILE_UNREADABLE"}
    signature = _quick_signature(path)
    expected_size = manifest.get("size_bytes")
    expected_fingerprint = manifest.get("quick_fingerprint")
    changed = (
        expected_size is not None and int(expected_size) != signature["sizeBytes"]
    ) or (
        expected_fingerprint is not None
        and str(expected_fingerprint) != signature["quickFingerprint"]
    )
    license_value = manifest.get("license", "unknown")
    selectable = license_value == "commercial-approved"
    return {
        **base,
        **signature,
        "status": "updated" if changed else "unchanged",
        "reasonCode": "REGISTERED_CONTENT_CHANGED" if changed else None,
        "selectable": selectable,
    }


def _discovered_entries(layer: str, root: Path, registered: set[Path]) -> list[dict]:
    if not root.is_dir():
        return []
    result = []
    for directory, _subdirs, filenames in os.walk(root, followlinks=False):
        for filename in sorted(filenames):
            candidate = Path(directory) / filename
            unresolved_relative = candidate.relative_to(root).as_posix()
            try:
                relative = relative_entry(root, candidate)
            except ValueError:
                result.append({
                    "layer": layer,
                    "category": None,
                    "assetRef": None,
                    "relativePath": unresolved_relative,
                    "status": "invalid",
                    "reasonCode": "PATH_OUTSIDE_PACKAGING_ROOT",
                    "selectable": False,
                })
                continue
            resolved = candidate.resolve()
            if resolved in registered:
                continue
            category = PERSONAL_DIRECTORY_CATEGORIES.get(Path(relative).parts[0])
            kind = _media_kind(candidate)
            if not _decodable(candidate, kind):
                result.append({
                    "layer": layer,
                    "category": category,
                    "assetRef": None,
                    "relativePath": relative,
                    "status": "invalid",
                    "reasonCode": "PACKAGING_FILE_UNREADABLE",
                    "selectable": False,
                })
                continue
            result.append({
                "layer": layer,
                "category": category,
                "assetRef": None,
                "relativePath": relative,
                "status": "added",
                "reasonCode": "PACKAGING_LICENSE_UNKNOWN",
                "selectable": False,
                **_quick_signature(candidate),
            })
    return result


def _scope(project_root: Path, personal_root: Path) -> dict:
    manifests = []
    if packaging_plan.MANIFEST_DIR.is_dir():
        for path in sorted(packaging_plan.MANIFEST_DIR.glob("*.json")):
            manifests.append({"name": path.name, **_quick_signature(path)})
    return {
        "projectRoot": str(project_root),
        "personalRoot": str(personal_root),
        "manifests": manifests,
    }


def _projection(row) -> dict | None:
    if row is None:
        return None
    result = json.loads(row["result_json"] or "{}")
    return {
        "checkId": row["check_id"],
        "scopeDigest": row["scope_digest"],
        "status": row["status"],
        "counts": result.get("counts", {}),
        "items": result.get("items", []),
        "errorCode": row["error_code"],
        "createdAt": row["created_at"],
        "completedAt": row["completed_at"],
    }


def latest_packaging_catalog_check(db, scope_digest: str | None = None) -> dict | None:
    if scope_digest is None:
        row = db.connection.execute(
            "SELECT * FROM packaging_catalog_checks ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    else:
        row = db.connection.execute(
            "SELECT * FROM packaging_catalog_checks WHERE scope_digest=? "
            "ORDER BY rowid DESC LIMIT 1",
            (scope_digest,),
        ).fetchone()
    return _projection(row)


def run_packaging_catalog_check(
    db, project_root: Path, *, check_id: str | None = None
) -> dict:
    root = Path(project_root).resolve()
    personal = personal_packaging_root(db, root).resolve(strict=False)
    digest = canonical_digest(_scope(root, personal))
    if check_id is None:
        check_id = f"pcc-{uuid.uuid4().hex[:16]}"
        with db.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO packaging_catalog_checks(check_id,scope_digest,status) "
                "VALUES(?,?,'running')",
                (check_id, digest),
            )
    else:
        with db.transaction(immediate=True) as connection:
            changed = connection.execute(
                "UPDATE packaging_catalog_checks SET status='running',error_code=NULL "
                "WHERE check_id=? AND status='queued'",
                (check_id,),
            ).rowcount
        if changed != 1:
            raise RuntimeError("PACKAGING_CATALOG_CHECK_STATE_INVALID: 检查任务状态无效")

    try:
        previous_row = db.connection.execute(
            "SELECT prior.result_json FROM packaging_catalog_checks current "
            "JOIN packaging_catalog_checks prior ON prior.rowid < current.rowid "
            "WHERE current.check_id=? AND prior.status='completed' "
            "AND prior.scope_digest=current.scope_digest "
            "ORDER BY prior.rowid DESC LIMIT 1",
            (check_id,),
        ).fetchone()
        previous_items = (
            json.loads(previous_row["result_json"] or "{}").get("items", [])
            if previous_row is not None else []
        )
        previous_by_key = {_registered_key(item): item for item in previous_items}
        registered_entries = _manifest_entries(root)
        registered_paths = {
            entry["path"] for entry in registered_entries if entry["path"] is not None
        }
        items = []
        for entry in registered_entries:
            current = _registered_result(entry, root)
            previous = previous_by_key.get(_registered_key(current))
            manifest = entry["manifest"]
            if (
                current["status"] == "unchanged"
                and previous is not None
                and manifest.get("size_bytes") is None
                and manifest.get("quick_fingerprint") is None
                and any(
                    previous.get(key) != current.get(key)
                    for key in ("sizeBytes", "mtimeNs", "quickFingerprint")
                )
            ):
                current = {
                    **current,
                    "status": "updated",
                    "reasonCode": "REGISTERED_CONTENT_CHANGED",
                }
            items.append(current)
        items.extend(
            _discovered_entries("extension", root / "asset-packs", registered_paths)
        )
        items.extend(_discovered_entries("personal", personal, registered_paths))
        items.sort(key=lambda item: (item["status"], item["relativePath"]))
        counts = {
            status: sum(item["status"] == status for item in items)
            for status in ("added", "updated", "missing", "invalid", "unchanged")
        }
        payload = {"counts": counts, "items": items}
        with db.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE packaging_catalog_checks SET status='completed',result_json=?,"
                "completed_at=datetime('now'),error_code=NULL WHERE check_id=?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), check_id),
            )
    except Exception as error:
        message = str(error)
        prefix = message.partition(":")[0]
        error_code = (
            prefix
            if prefix and prefix.replace("_", "").isalnum() and prefix.upper() == prefix
            else "PACKAGING_CATALOG_CHECK_FAILED"
        )
        with db.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE packaging_catalog_checks SET status='failed',error_code=?,"
                "completed_at=datetime('now') WHERE check_id=?",
                (error_code, check_id),
            )
        raise
    result = latest_packaging_catalog_check(db, digest)
    if result is None:  # pragma: no cover
        raise RuntimeError("PACKAGING_CATALOG_CHECK_MISSING: 检查结果未保存")
    return result


def _active_job(db, check_id: str) -> dict | None:
    from .workbench_worker import get_job

    rows = db.connection.execute(
        "SELECT j.job_id,p.payload_json FROM jobs j JOIN job_payloads p USING(job_id) "
        "WHERE j.job_type='packaging_catalog_check' AND j.status IN('queued','running')"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("check_id") == check_id:
            return get_job(db, row["job_id"])
    return None


def _next_catalog_job_id(db, check_id: str) -> str:
    base = f"packaging-catalog-check-{check_id}"
    existing = {
        row["job_id"]
        for row in db.connection.execute(
            "SELECT job_id FROM jobs WHERE job_id=? OR job_id LIKE ?",
            (base, f"{base}-r%"),
        ).fetchall()
    }
    if base not in existing:
        return base
    for retry in range(1, 100):
        candidate = f"{base}-r{retry}"
        if candidate not in existing:
            return candidate
    raise RuntimeError(
        "PACKAGING_CATALOG_CHECK_RETRY_LIMIT: 素材检查重试次数过多"
    )


def request_packaging_catalog_check(db, project_root: Path) -> dict:
    from .workbench_worker import enqueue_job, get_job

    root = Path(project_root).resolve()
    personal = personal_packaging_root(db, root).resolve(strict=False)
    digest = canonical_digest(_scope(root, personal))
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT * FROM packaging_catalog_checks WHERE scope_digest=? "
            "AND status IN('queued','running') ORDER BY rowid DESC LIMIT 1",
            (digest,),
        ).fetchone()
        if row is None:
            check_id = f"pcc-{uuid.uuid4().hex[:16]}"
            connection.execute(
                "INSERT INTO packaging_catalog_checks(check_id,scope_digest,status) "
                "VALUES(?,?,'queued')",
                (check_id, digest),
            )
            row = connection.execute(
                "SELECT * FROM packaging_catalog_checks WHERE check_id=?",
                (check_id,),
            ).fetchone()
    check = _projection(row)
    active = _active_job(db, check["checkId"])
    if active is None and check["status"] == "running":
        with db.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE packaging_catalog_checks SET status='failed',"
                "error_code='PACKAGING_CATALOG_CHECK_ORPHANED',"
                "completed_at=datetime('now') WHERE check_id=? AND status='running'",
                (check["checkId"],),
            )
        return request_packaging_catalog_check(db, root)
    if active is None:
        job_id = _next_catalog_job_id(db, check["checkId"])
        try:
            enqueue_job(
                db,
                job_id=job_id,
                job_type="packaging_catalog_check",
                payload={"check_id": check["checkId"]},
                retry_policy="explicit",
            )
        except ValueError:
            active = _active_job(db, check["checkId"])
            if active is None:
                raise
        else:
            active = get_job(db, job_id)
    return {**check, "jobId": active["job_id"], "job": active}
