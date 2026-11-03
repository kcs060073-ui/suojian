"""One integrity check for registered packages, their UI, and safe reuse."""
import json
from pathlib import Path
from .snapshots import file_content_signature

PACKAGE_FILES = (
    "video-final.mp4", "cover-01.jpg", "cover-02.jpg", "cover-03.jpg",
    "publish-copy.md", "publish-check.json", "qa-report.md",
)


def safe_name(name):
    return isinstance(name, str) and bool(name) and name not in {".", ".."} and not any(
        char in name for char in "/\\:\0\r\n"
    )


def manifest_files(check):
    raw = check.get("files")
    if not isinstance(raw, list) or not raw or any(not safe_name(n) for n in raw):
        raise RuntimeError("PUBLISH_PACKAGE_CHANGED: 发布包文件清单非法")
    cover = check.get("cover")
    if not isinstance(cover, dict) or not safe_name(cover.get("path")):
        raise RuntimeError("PUBLISH_PACKAGE_CHANGED: 发布包封面清单非法")
    required = set(PACKAGE_FILES) | {cover["path"]}
    if check.get("delivery_type") == "editable":
        required.add("delivery-manifest.json")
    if len(raw) != len(set(raw)) or not required.issubset(raw):
        raise RuntimeError("PUBLISH_PACKAGE_CHANGED: 发布包文件清单缺项")
    return tuple(raw)


def record_manifest(connection, check, signature):
    """Call only in the transaction that registers/adopts this package."""
    if not connection.in_transaction:
        raise RuntimeError("PUBLISH_MANIFEST_TRANSACTION_REQUIRED")
    encoded = json.dumps(check, ensure_ascii=False, sort_keys=True)
    encoded_signature = json.dumps(signature, sort_keys=True)
    connection.execute(
        "INSERT OR IGNORE INTO publish_manifests(record_id,manifest_json,manifest_signature_json) VALUES(?,?,?)",
        (check["record_id"], encoded, encoded_signature),
    )
    row = connection.execute("SELECT manifest_json,manifest_signature_json FROM publish_manifests WHERE record_id=?", (check["record_id"],)).fetchone()
    if tuple(row) != (encoded, encoded_signature):
        raise RuntimeError("PUBLISH_PACKAGE_CHANGED: 交付清单与已登记版本不一致")


def _object(value):
    return value if isinstance(value, dict) else {}


def inspect_package(db, record, folder, *, verify_content=True):
    """Report missing/changed bytes against SQLite receipts; legacy uses strict structure."""
    folder = Path(folder) if folder is not None else None
    disk = {}
    if folder is not None:
        try:
            disk = _object(json.loads((folder / "publish-check.json").read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            pass
    receipt = db.connection.execute(
        "SELECT manifest_json,manifest_signature_json FROM publish_manifests WHERE record_id=?",
        (record["record_id"],),
    ).fetchone()
    expected, manifest_signature = disk, None
    if receipt is not None:
        expected = _object(json.loads(receipt["manifest_json"]))
        manifest_signature = json.loads(receipt["manifest_signature_json"])
    changed = []
    try:
        files = manifest_files(expected)
    except (RuntimeError, TypeError, ValueError):
        # Even a legacy/damaged manifest cannot make mandatory files disappear.
        files = PACKAGE_FILES
        changed.append("publish-check.json")
    signatures = _object(expected.get("file_signatures"))
    if (not signatures or any(expected.get(key) != record[key] for key in ("record_id", "task_id", "candidate_id"))
            or receipt is not None and disk != expected):
        changed.append("publish-check.json")
    legacy_video_signature = None
    if receipt is None:
        rendered = db.connection.execute(
            "SELECT r.params_json FROM candidate_versions c JOIN render_snapshots r "
            "ON r.snapshot_id=c.mix_snapshot_id WHERE c.candidate_id=? AND c.task_id=?",
            (record["candidate_id"], record["task_id"]),
        ).fetchone()
        if rendered is not None:
            try:
                legacy_video_signature = _object(json.loads(rendered[0])).get("output_signature")
            except (TypeError, ValueError):
                pass
    missing = []
    for name in files:
        path = None if folder is None else folder / name
        if path is None or not path.is_file():
            missing.append(name)
            continue
        signature = manifest_signature if name == "publish-check.json" else signatures.get(name)
        if receipt is None and name == "video-final.mp4":
            # Legacy manifests are mutable; bind their video to the immutable render.
            signature = legacy_video_signature
        try:
            # Task-list projection is a quick anomaly check, not an integrity certificate.
            # Detail, export, reuse and open-directory always use full content verification.
            signature_changed = signature is not None and (
                not isinstance(signature, dict) or (
                    file_content_signature(path) != signature if verify_content
                    else path.stat().st_size != signature.get("size_bytes")
                )
            )
            if folder.is_symlink() or path.is_symlink() or signature_changed:
                changed.append(name)
            elif name != "publish-check.json" and not isinstance(signature, dict):
                changed.append(name)
        except OSError:
            changed.append(name)
    return {
        "check": expected,
        "files": list(files),
        "missing_files": missing,
        "changed_files": list(dict.fromkeys(n for n in changed if n not in missing)),
        "manifest_registered": receipt is not None,
    }
