"""Inspect explicit shared-catalog and local-BGM references without mutations.

The caller owns a stable, read-only SQLite transaction and the binding ledger.
No resolver fallback, scan, reconnection, hashing-to-register, or cache rebuild
is performed. A catalog location is not an immutable task-version address.
"""
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import stat

from .path_utils import filesystem_path
from .upgrade_recovery import _fail, _plain_path


def _object(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        _fail("CATALOG_RECORD_INVALID", "素材引用记录不是有效 JSON 对象。")
    if not isinstance(value, dict):
        _fail("CATALOG_RECORD_INVALID", "素材引用记录不是有效对象。")
    return value


def _identity(value):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        _fail("CATALOG_BINDING_INVALID", "素材或库的原逻辑 ID 为空或无效。")
    return value


def _has_unknown_fields(value, allowed):
    return any(key not in allowed and item is not None and item != "" and item != [] and item != {}
               for key, item in value.items())


def _sha(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        _fail("CATALOG_SIGNATURE_INVALID", "素材原内容摘要缺失或无效。")
    return value


def _size(value):
    if type(value) is not int or value < 0:
        _fail("CATALOG_SIGNATURE_INVALID", "素材原字节数无效。")
    return value


def _path(raw):
    if (not isinstance(raw, str) or not raw or "\0" in raw
            or not Path(raw).is_absolute() or ".." in Path(raw).parts
            or any(part.lower() in {".env", "session-signing.key"} for part in Path(raw).parts)
            or any(":" in part for part in Path(raw).parts[1:])):
        _fail("CATALOG_PATH_INVALID", "素材必须保留明确、安全的原绝对地址。")
    return _plain_path(raw)


def _relative(raw, allow_empty=False):
    if not isinstance(raw, str) or "\0" in raw or ":" in raw or PureWindowsPath(raw).drive:
        _fail("CATALOG_PATH_INVALID", "素材相对地址无效。")
    if raw == "" and allow_empty:
        return Path()
    parts = raw.replace("\\", "/").split("/")
    if any(part.lower() in {"", ".", "..", ".env", "session-signing.key"} for part in parts):
        _fail("CATALOG_PATH_INVALID", "素材相对地址不能越界或引用敏感文件。")
    return Path(*parts)


def _observe(path, full=True):
    """One open, original quick-fingerprint algorithm, bounded-memory full SHA."""
    path = _plain_path(path)
    native = filesystem_path(path)
    before = native.lstat()
    if not stat.S_ISREG(before.st_mode):
        _fail("CATALOG_FILE_INVALID", "素材不是普通文件。")
    with native.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
        if stamp(opened) != stamp(before):
            _fail("SOURCE_CHANGED", "素材在打开时发生变化。")
        quick = hashlib.sha256(str(before.st_size).encode("ascii"))
        quick.update(stream.read(1024 * 1024))
        if before.st_size > 1024 * 1024:
            stream.seek(max(0, before.st_size - 1024 * 1024))
            quick.update(stream.read(1024 * 1024))
        digest = None
        if full:
            stream.seek(0)
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        closed_stamp = stamp(os.fstat(stream.fileno()))
    _plain_path(path)
    after = native.lstat()
    if stamp(before) != closed_stamp or stamp(before) != stamp(after):
        _fail("SOURCE_CHANGED", "素材在盘点期间发生变化。")
    return {"size_bytes": before.st_size, "sha256": None if digest is None else digest.hexdigest(),
            "quick": quick.hexdigest(), "mtime_ns": before.st_mtime_ns,
            "identity": f"{before.st_dev}:{before.st_ino}"}


def inspect_catalog_references(db, source_root, tables, bind, pending) -> dict:
    """Read all registered locations, retain original addresses and pending facts.

    bind(table, identity, field, original_absolute_path, {size_bytes, sha256})
    must validate using the recorded signature. It must never retarget a path.
    pending(table, identity, code) keeps a blocking unresolved item in the caller.
    The function does not own/alter transactions or query_only configuration.
    """
    if not db.connection.in_transaction or db.connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        _fail("CATALOG_READONLY_REQUIRED", "素材恢复盘点需要只读稳定事务。")
    source_root = _path(str(source_root))
    result = {"phase": "catalog-reference-inspection", "libraries": [], "locations": [],
              "localBgmItems": [], "frozenLocalBgm": [], "cache": [], "taskReferences": [], "taskScopes": [],
              "taskSourceSnapshots": [], "information": [], "unresolved": [], "auditComplete": False,
              "remainingAdapters": ["historical_scope_mounts", "cache_backup_observations",
                                    "non_local_packaging_inputs", "local_bgm_job_envelopes_and_recipe_variants"]}

    def unresolved(table, identity, code):
        result["unresolved"].append({"table": table, "rowId": identity, "code": code})
        pending(table, identity, code)

    def information(table, identity, code):
        result["information"].append({"table": table, "rowId": identity, "code": code})

    def rows(table):
        # Only the fixed table literals below ever reach this query.
        return [dict(row) for row in db.connection.execute(f'SELECT * FROM "{table}"')] if table in tables else []

    if not {"libraries", "assets", "asset_locations"} <= set(tables):
        unresolved("catalog", None, "CATALOG_TABLES_UNAVAILABLE")
        return result
    libraries = {r["library_id"]: r for r in rows("libraries")}
    assets = {r["asset_id"]: r for r in rows("assets")}
    locations = rows("asset_locations")
    music_libraries = {r["library_id"]: r for r in rows("local_bgm_libraries")}
    packaging = {r["asset_id"]: r for r in rows("packaging_assets")}
    roots, library_status, music_notes = {}, {}, {}
    for identity, row in libraries.items():
        _identity(identity)
        item = {"libraryId": identity, "recordedMountPath": row["mount_path"], "kind": row["kind"]}
        if row["kind"] != "local":
            status = "unsupported"
            unresolved("libraries", identity, "CATALOG_LIBRARY_KIND_UNSUPPORTED")
        else:
            root = _path(row["mount_path"])
            roots[identity] = root
            status = "online" if row["online"] == 1 and filesystem_path(root).is_dir() else "offline"
            if status == "offline":
                unresolved("libraries", identity, "LIBRARY_OFFLINE")
        item["status"] = library_status[identity] = status
        result["libraries"].append(item)
    for identity, row in music_libraries.items():
        if identity not in libraries or libraries[identity]["kind"] != "local":
            _fail("CATALOG_BINDING_INVALID", "本地音乐引用了不存在或非本地素材库。")
        if row["enabled"] != 1:
            unresolved("local_bgm_libraries", identity, "BGM_LIBRARY_DISABLED")
    for identity, asset in assets.items():
        _identity(identity)
        _size(asset["size_bytes"])
        _sha(asset["quick_fingerprint"])
        if asset.get("full_sha256") is not None:
            _sha(asset["full_sha256"])
        p = packaging.get(identity)
        if p and p.get("source_path"):
            unresolved("packaging_assets", identity, "PACKAGING_SOURCE_PATH_UNSUPPORTED")
        if p and p.get("kind") == "bgm" and any(l["asset_id"] == identity and l["library_id"] in music_libraries for l in locations):
            notes = _object(p.get("notes"))
            if _has_unknown_fields(notes, {"sha256", "stamp", "duration_s", "license", "mood"}):
                unresolved("packaging_assets", identity, "BGM_NOTES_FIELD_UNSUPPORTED")
            sha, stamp = notes.get("sha256"), notes.get("stamp")
            if (not sha or not isinstance(stamp, list) or len(stamp) != 2
                    or type(stamp[0]) is not int or type(stamp[1]) is not int
                    or stamp[0] != asset["size_bytes"] or stamp[1] < 0
                    or type(notes.get("duration_s")) not in (int, float)
                    or not math.isfinite(notes["duration_s"]) or notes["duration_s"] <= 0):
                unresolved("packaging_assets", identity, "BGM_ORIGINAL_SIGNATURE_UNAVAILABLE")
            else:
                _sha(sha)
                if asset.get("full_sha256") is not None and sha != asset["full_sha256"]:
                    _fail("BGM_SIGNATURE_CONFLICT", "音乐原登记摘要与素材账本冲突，不能换用当前摘要。")
                music_notes[identity] = notes
        if not any(l["asset_id"] == identity for l in locations):
            unresolved("assets", identity, "CATALOG_LOCATION_UNAVAILABLE")

    verified = {}
    for row in locations:
        identity = row["location_id"]
        asset_id, library_id = row["asset_id"], row["library_id"]
        if asset_id not in assets or library_id not in libraries:
            _fail("CATALOG_BINDING_INVALID", "素材位置的库 ID 或素材 ID 无法对应原账本。")
        relative = _relative(row["relative_path"])
        asset, library = assets[asset_id], libraries[library_id]
        original_sha = asset.get("full_sha256") or music_notes.get(asset_id, {}).get("sha256")
        item = {"locationId": identity, "assetId": asset_id, "libraryId": library_id,
                "recordedMountPath": library["mount_path"], "relativePath": row["relative_path"],
                "recordedQuickFingerprint": asset["quick_fingerprint"], "recordedSizeBytes": asset["size_bytes"],
                "recordedSha256": original_sha, "originalPath": None,
                "integrityLevel": "original_full_sha" if original_sha is not None else "quick_fingerprint_only",
                "backupObservationRequired": original_sha is None, "frozenBusinessReuseVerified": False}
        if library_status[library_id] == "unsupported":
            status = "unsupported"
            unresolved("asset_locations", identity, "CATALOG_LIBRARY_KIND_UNSUPPORTED")
        else:
            path = _plain_path(roots[library_id] / relative)
            item["originalPath"] = str(path)
            status = "offline" if library_status[library_id] == "offline" else row["state"]
            if status in {"offline", "missing"}:
                unresolved("asset_locations", identity, "CATALOG_LOCATION_" + status.upper())
            elif status != "available":
                _fail("CATALOG_BINDING_INVALID", "素材原位置状态无效。")
            else:
                try:
                    actual = _observe(path, full=original_sha is not None)
                except FileNotFoundError:
                    status = "missing"
                    unresolved("asset_locations", identity, "CATALOG_FILE_MISSING")
                except OSError:
                    status = "unreadable"
                    unresolved("asset_locations", identity, "CATALOG_FILE_UNREADABLE")
                else:
                    if (actual["size_bytes"] != asset["size_bytes"] or actual["quick"] != asset["quick_fingerprint"]
                            or original_sha is not None and actual["sha256"] != original_sha):
                        _fail("CATALOG_CONTENT_CHANGED", "素材内容与原指纹、字节数或摘要不同，请保留原记录。")
                    if actual["mtime_ns"] != row["mtime_ns"] or row.get("file_identity") and row["file_identity"] != actual["identity"]:
                        status = "metadata_changed"
                        unresolved("asset_locations", identity, "CATALOG_LOCATION_METADATA_CHANGED")
                    elif original_sha is None:
                        status = "fingerprint_verified"
                        information("asset_locations", identity, "CATALOG_FULL_SHA_UNAVAILABLE")
                    else:
                        expected = {"size_bytes": asset["size_bytes"], "sha256": original_sha}
                        bind("asset_locations", identity, "mount_path/relative_path", str(path), expected)
                        status = "verified"
                        verified[identity] = expected
        item["status"] = status
        result["locations"].append(item)

    for row in rows("packaging_plan_items"):
        ref = row.get("asset_ref")
        if not isinstance(ref, str) or not ref.startswith("local-bgm-"):
            continue
        identity, asset_id = row["item_id"], ref[len("local-bgm-"):]
        if (row["slot"] != "bgm" or asset_id not in assets or assets[asset_id]["media_kind"] != "audio"
                or packaging.get(asset_id, {}).get("kind") != "bgm"
                or row.get("asset_file") not in (None, "", "catalog:" + asset_id)):
            _fail("BGM_REFERENCE_INVALID", "本地音乐必须使用原素材逻辑标识，不能把标识当文件路径。")
        matches = [l for l in locations if l["asset_id"] == asset_id and l["library_id"] in music_libraries]
        if not matches:
            unresolved("packaging_plan_items", identity, "BGM_CATALOG_LOCATION_UNAVAILABLE")
        for location in matches:
            lid = location["library_id"]
            if (music_libraries[lid]["enabled"] == 1 and location["location_id"] in verified
                    and asset_id in music_notes and music_notes[asset_id]["stamp"][1] == location["mtime_ns"]):
                # Catalog relationships are editable, not frozen plan addresses.
                information("packaging_plan_items", identity, "CURRENT_CATALOG_RELATIONSHIP_VERIFIED")
            else:
                unresolved("packaging_plan_items", identity, "BGM_CATALOG_LOCATION_UNVERIFIED")
        # The row stores catalog:<id>. Keep its editable identity, not a fake gap.
        information("packaging_plan_items", identity, "EDITABLE_CATALOG_REFERENCE")
        result["localBgmItems"].append({"itemId": identity, "planId": row["plan_id"], "assetRef": ref,
                                       "assetFile": row.get("asset_file"), "assetVersion": row.get("asset_version"),
                                       "locationIds": [l["location_id"] for l in matches], "status": "catalog_reference",
                                       "referenceKind": "editable_catalog_id", "frozenBusinessReuseVerified": False})

    for row in rows("render_snapshots"):
        params = _object(row["params_json"])
        containers = [("packaging", params.get("packaging"))]
        dependency = params.get("dependency_snapshot")
        if isinstance(dependency, dict):
            containers.append(("dependency_snapshot/packaging", dependency.get("packaging")))
            containers.append(("dependency_snapshot/packaging_compiled", dependency.get("packaging_compiled")))
        for field, container in containers:
            if not isinstance(container, dict):
                continue
            for index, item in enumerate(container.get("items", [])):
                if not isinstance(item, dict):
                    _fail("CATALOG_RECORD_INVALID", "冻结包装条目形状无效。")
                ref, manifest = item.get("asset_ref"), item.get("manifest")
                if not isinstance(ref, str) or not ref.startswith("local-bgm-"):
                    continue
                identity, asset_id = row["snapshot_id"], ref[len("local-bgm-"):]
                if (not isinstance(manifest, dict) or item.get("slot") != "bgm" or manifest.get("id") != ref
                        or manifest.get("file") != "catalog:" + asset_id or asset_id not in assets
                        or assets[asset_id]["media_kind"] != "audio"):
                    _fail("BGM_REFERENCE_INVALID", "冻结音乐标识与原 manifest 不一致。")
                if _has_unknown_fields(item, {"slot", "asset_ref", "asset_version", "is_backup", "rank", "license",
                                              "file", "manifest", "file_signature"}):
                    unresolved("render_snapshots", identity, "BGM_ITEM_FIELD_UNSUPPORTED")
                if _has_unknown_fields(manifest, {"id", "name", "mood", "description", "license", "default_for",
                                                  "file", "default_gain", "duration_s", "source_sha256",
                                                  "local_available", "_local_root", "_local_path"}):
                    unresolved("render_snapshots", identity, "BGM_MANIFEST_FIELD_UNSUPPORTED")
                root, path = _path(manifest.get("_local_root")), _path(manifest.get("_local_path"))
                if not path.is_relative_to(root) or _path(item.get("file")) != path:
                    _fail("BGM_REFERENCE_INVALID", "冻结音乐原根目录与原文件地址不一致。")
                signature = item.get("file_signature")
                if not isinstance(signature, dict) or set(signature) != {"size_bytes", "sha256"}:
                    unresolved("render_snapshots", identity, "BGM_ORIGINAL_SIGNATURE_UNAVAILABLE")
                    continue
                expected = {"size_bytes": _size(signature["size_bytes"]), "sha256": _sha(signature["sha256"])}
                if manifest.get("source_sha256") != expected["sha256"]:
                    _fail("BGM_SIGNATURE_CONFLICT", "冻结音乐的原 manifest 与原文件摘要冲突。")
                matching = [l for l in locations if l["asset_id"] == asset_id and l["library_id"] in music_libraries
                            and roots[l["library_id"]] == root and root / _relative(l["relative_path"]) == path]
                status = "verified"
                if not matching:
                    status = "unresolved"
                    unresolved("render_snapshots", identity, "BGM_ORIGINAL_LOCATION_UNAVAILABLE")
                elif not any(l["location_id"] in verified and music_libraries[l["library_id"]]["enabled"] == 1 for l in matching):
                    status = "unresolved"
                    unresolved("render_snapshots", identity, "BGM_CATALOG_LOCATION_UNVERIFIED")
                else:
                    bind("render_snapshots", identity, f"{field}/items/{index}/manifest/_local_path", str(path), expected)
                result["frozenLocalBgm"].append({"snapshotId": identity, "assetRef": ref,
                                               "recordedRoot": str(root), "recordedPath": str(path),
                                               "recordedSignature": expected, "status": status})

    cache_root = _path(str(Path(db.path).parent / "cache"))
    if not cache_root.is_relative_to(source_root):
        _fail("CATALOG_PATH_INVALID", "缓存的登记账本位置不在原安装范围。")
    for row in rows("preview_cache"):
        identity = row["asset_id"] + ":" + row["kind"]
        if row["asset_id"] not in assets:
            _fail("CATALOG_BINDING_INVALID", "预览缓存引用了不存在的素材。")
        path = _plain_path(cache_root / _relative(row["rel_path"]))
        try:
            info = filesystem_path(path).lstat()
            if not stat.S_ISREG(info.st_mode):
                _fail("CACHE_FILE_INVALID", "预览缓存不是普通文件。")
            size = info.st_size
        except FileNotFoundError:
            status, code = "missing", "CACHE_FILE_MISSING"
        except OSError:
            status, code = "unreadable", "CACHE_FILE_UNREADABLE"
        else:
            if row.get("size_bytes") is not None and size != _size(row["size_bytes"]):
                _fail("CACHE_CONTENT_CHANGED", "缓存字节数与原登记不同。")
            status, code = "present_unverified", "CACHE_CONTENT_SIGNATURE_UNAVAILABLE"
        if status == "present_unverified":
            information("preview_cache", identity, code)
        else:
            unresolved("preview_cache", identity, code)
        result["cache"].append({"rowId": identity, "recordedRelativePath": row["rel_path"],
                                "originalPath": str(path), "status": status, "rebuildPerformed": False,
                                "backupObservationRequired": status == "present_unverified", "businessReuseVerified": False})

    for row in rows("task_assets"):
        identity = row["task_id"] + ":" + row["asset_id"]
        if row["asset_id"] not in assets:
            _fail("CATALOG_BINDING_INVALID", "任务引用了不存在的素材 ID。")
        result["taskReferences"].append({"table": "task_assets", "rowId": identity, "assetId": row["asset_id"],
                                         "taskId": row["task_id"], "role": row.get("role"),
                                         "referenceKind": "editable_catalog_id", "frozenBusinessReuseVerified": False})
        information("task_assets", identity, "EDITABLE_CATALOG_REFERENCE")
    for row in rows("scene_cards"):
        if row.get("asset_id") is not None:
            if row["asset_id"] not in assets:
                _fail("CATALOG_BINDING_INVALID", "镜头引用了不存在的素材 ID。")
            result["taskReferences"].append({"table": "scene_cards", "rowId": row["card_id"],
                                             "taskId": row["task_id"], "assetId": row["asset_id"],
                                             "referenceKind": "editable_catalog_id", "frozenBusinessReuseVerified": False})
            information("scene_cards", row["card_id"], "EDITABLE_CATALOG_REFERENCE")
    scopes = {r["scope_id"]: r for r in rows("task_source_scopes")}
    for identity, row in scopes.items():
        if row["library_id"] not in libraries:
            _fail("CATALOG_BINDING_INVALID", "任务范围引用了不存在的库 ID。")
        _relative(row["relative_path"], allow_empty=True)
        result["taskScopes"].append({"scopeId": identity, "taskId": row["task_id"],
                                     "libraryId": row["library_id"], "relativePath": row["relative_path"],
                                     "active": row.get("active"), "referenceKind": "editable_library_scope",
                                     "frozenBusinessReuseVerified": False})
        information("task_source_scopes", identity, "EDITABLE_LIBRARY_SCOPE")
    for row in rows("task_source_snapshots"):
        identity, summary = row["snapshot_id"], _object(row["summary_json"])
        scope = scopes.get(row["scope_id"])
        if (scope is None or scope["task_id"] != row["task_id"]
                or summary.get("library_id") != scope["library_id"]):
            _fail("CATALOG_BINDING_INVALID", "任务素材快照与原任务、范围或库 ID 不一致。")
        _relative(summary.get("relative_path"), allow_empty=True)
        entries = summary.get("assets")
        if not isinstance(entries, list):
            _fail("CATALOG_RECORD_INVALID", "任务素材快照缺少原素材清单。")
        missing_version = False
        present_ids = {location["locationId"] for location in result["locations"]
                       if location["status"] in {"verified", "fingerprint_verified"}}
        for entry in entries:
            if not isinstance(entry, dict):
                _fail("CATALOG_RECORD_INVALID", "任务素材快照条目无效。")
            _relative(entry.get("relative_path"));_size(entry.get("size_bytes"));_sha(entry.get("quick_fingerprint"))
            asset = assets.get(entry.get("asset_id"))
            if (asset is None or asset["size_bytes"] != entry["size_bytes"]
                    or asset["quick_fingerprint"] != entry["quick_fingerprint"]
                    or not any(l["asset_id"] == entry["asset_id"] and l["library_id"] == summary["library_id"]
                               and _relative(l["relative_path"]) == _relative(entry["relative_path"])
                               and l["location_id"] in present_ids for l in locations)):
                missing_version = True
                unresolved("task_source_snapshots", identity, "TASK_SNAPSHOT_VERSION_UNAVAILABLE")
        if missing_version:
            unresolved("task_source_snapshots", identity, "TASK_SNAPSHOT_ORIGINAL_MOUNT_UNFROZEN")
        else:
            information("task_source_snapshots", identity, "HISTORICAL_SCOPE_CATALOG_VERSION_MATCHED")
        result["taskSourceSnapshots"].append({"snapshotId": identity, "taskId": row["task_id"],
                                               "scopeId": row["scope_id"], "libraryId": summary["library_id"],
                                               "relativePath": summary["relative_path"], "assets": entries,
                                               "status": "unresolved_version" if missing_version else "matched_current_catalog",
                                               "integrityLevel": "quick_fingerprint_snapshot", "originalMountRecorded": False,
                                               "frozenBusinessReuseVerified": False})
    return result
