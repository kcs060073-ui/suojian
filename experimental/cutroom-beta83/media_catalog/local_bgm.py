"""User BGM folders backed by the shared asset identity/location catalog."""
from pathlib import Path
import json
import uuid

PREFIX = "local-bgm-"

def connection(db):
    return getattr(db, "connection", db)

def folders(db):
    conn = connection(db)
    return [dict(row) for row in conn.execute(
        "SELECT l.library_id,l.name,l.mount_path,l.online,b.enabled,"
        "(SELECT COUNT(*) FROM asset_locations al JOIN assets a USING(asset_id) "
        "WHERE al.library_id=l.library_id AND a.media_kind='audio' AND al.state='available') AS track_count "
        "FROM local_bgm_libraries b JOIN libraries l USING(library_id) ORDER BY l.name,l.library_id")]

def register_folder(db, raw_path, library_id=None):
    from .libraries import register_library, update_mount
    path = Path(raw_path)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("BGM_FOLDER_INVALID: 请选择可访问的本地音乐文件夹")
    path = path.resolve()
    known = folders(db)
    if library_id and db.connection.execute("SELECT 1 FROM jobs WHERE library_id=? AND status IN ('queued','running')", (library_id,)).fetchone():
        raise RuntimeError("BGM_SCAN_BUSY: 目录正在读取，请完成后再重新连接")
    if library_id:
        if library_id not in {row["library_id"] for row in known}:
            raise ValueError("BGM_FOLDER_UNKNOWN: 音乐目录不存在")
        update_mount(db, library_id, str(path))
    else:
        old = next((row for row in known if Path(row["mount_path"]).resolve() == path), None)
        library_id = old["library_id"] if old else "bgm-lib-" + uuid.uuid4().hex[:16]
        if not old:
            register_library(db, library_id, path.name or "本地音乐", "local", str(path), purpose="packaging")
    with db.transaction(immediate=True) as conn:
        conn.execute("INSERT INTO local_bgm_libraries(library_id) VALUES(?) ON CONFLICT(library_id) DO UPDATE SET enabled=1", (library_id,))
    return request_scan(db, library_id)

def set_enabled(db, library_id, enabled):
    if not isinstance(enabled, bool):
        raise ValueError("BGM_SETTING_INVALID: 启用状态无效")
    with db.transaction(immediate=True) as conn:
        if conn.execute("UPDATE local_bgm_libraries SET enabled=? WHERE library_id=?", (int(enabled), library_id)).rowcount != 1:
            raise ValueError("BGM_FOLDER_UNKNOWN: 音乐目录不存在")
    return {"libraryId": library_id, "enabled": enabled}

def request_scan(db, library_id):
    from .workbench_worker import enqueue_job
    if library_id not in {row["library_id"] for row in folders(db)}:
        raise ValueError("BGM_FOLDER_UNKNOWN: 音乐目录不存在")
    active = db.connection.execute("SELECT job_id FROM jobs WHERE library_id=? AND job_type='scan' AND status IN ('queued','running') ORDER BY rowid DESC LIMIT 1", (library_id,)).fetchone()
    if active:
        return {"libraryId": library_id, "jobId": active["job_id"]}
    job_id = "bgm-scan-" + uuid.uuid4().hex
    enqueue_job(db, job_id=job_id, job_type="scan", library_id=library_id,
                payload={"action": "scan", "library_id": library_id, "catalog_only": True})
    return {"libraryId": library_id, "jobId": job_id}

def _notes(value):
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}

def index_scanned_folder(db, library_id):
    """Enrich only audio from an explicitly registered BGM folder, after shared scan."""
    if library_id not in {row["library_id"] for row in folders(db)}:
        return
    from .packaging_plan import audio_duration_seconds
    from .packaging_audio_evidence import file_sha256
    rows = db.connection.execute("SELECT a.asset_id,l.mount_path,al.relative_path, p.notes,p.name,p.kind FROM assets a "
        "JOIN asset_locations al USING(asset_id) JOIN libraries l USING(library_id) "
        "LEFT JOIN packaging_assets p USING(asset_id) WHERE al.library_id=? AND al.state='available' AND a.media_kind='audio'", (library_id,)).fetchall()
    for row in rows:
        root = Path(row["mount_path"]).resolve()
        path = (root / row["relative_path"]).resolve()
        if not path.is_relative_to(root) or row["kind"] not in (None, "bgm"):
            continue
        previous = _notes(row["notes"])
        try:
            stat = path.stat()
            stamp = [stat.st_size, stat.st_mtime_ns]
            if previous.get("stamp") == stamp and previous.get("sha256") and previous.get("duration_s"):
                continue
            sha = file_sha256(path)
            duration = audio_duration_seconds(path)
            after = path.stat()
            if [after.st_size, after.st_mtime_ns] != stamp:
                continue
        except OSError:
            continue
        unchanged = previous.get("sha256") == sha
        metadata = {**previous, "sha256": sha, "stamp": stamp, "duration_s": duration,
                    "license": previous.get("license", "unknown") if unchanged else "unknown",
                    "mood": previous.get("mood", "") if unchanged else ""}
        with db.transaction() as conn:
            conn.execute("INSERT INTO packaging_assets(asset_id,kind,name,notes) VALUES(?,'bgm',?,?) "
                "ON CONFLICT(asset_id) DO UPDATE SET notes=excluded.notes", (row["asset_id"], path.stem, json.dumps(metadata, ensure_ascii=False)))

def manifest_items(db):
    if db is None:
        return []
    conn = connection(db)
    rows = conn.execute("SELECT p.asset_id,p.name,p.notes,l.mount_path,l.online,al.relative_path,al.state,b.enabled "
        "FROM packaging_assets p JOIN asset_locations al USING(asset_id) JOIN libraries l USING(library_id) "
        "JOIN local_bgm_libraries b USING(library_id) WHERE p.kind='bgm' "
        "ORDER BY b.enabled DESC,l.online DESC,al.location_id").fetchall()
    result, seen = [], set()
    for row in rows:
        ref = PREFIX + row["asset_id"]
        if ref in seen:
            continue
        root = Path(row["mount_path"]).resolve()
        path = (root / row["relative_path"]).resolve()
        if not path.is_relative_to(root):
            continue
        notes = _notes(row["notes"])
        available = bool(row["enabled"] and row["online"] and row["state"] == "available" and path.is_file())
        try:
            stat = path.stat()
            current = notes.get("stamp") == [stat.st_size, stat.st_mtime_ns]
        except OSError:
            current = False
        if not current or not notes.get("duration_s"):
            available = False
        result.append({"id": ref, "name": row["name"] or path.stem, "mood": notes.get("mood", ""),
            "description": "用户本地曲库；风格为用户填写，未作听感识别",
            "license": notes.get("license", "unknown") if current else "unknown", "default_for": [],
            "file": "catalog:" + row["asset_id"], "default_gain": .18,
            "duration_s": notes.get("duration_s"), "source_sha256": notes.get("sha256"),
            "local_available": available, "_local_root": str(root), "_local_path": str(path)})
        # Prefer any usable copy; retain one unavailable entry if all copies are offline.
        if available:
            seen.add(ref)
    unique = {}
    for item in result:
        if item["id"] not in unique or item["local_available"]:
            unique[item["id"]] = item
    return list(unique.values())

def update_track(db, asset_ref, body):
    from .packaging_audio_evidence import file_sha256
    item = next((x for x in manifest_items(db) if x["id"] == asset_ref), None)
    if not item or not item["local_available"]:
        raise ValueError("BGM_TRACK_UNAVAILABLE: 音乐不可用，请重新扫描或连接目录")
    if body.get("expectedSha256") != item["source_sha256"] or file_sha256(Path(item["_local_path"])) != item["source_sha256"]:
        raise RuntimeError("BGM_TRACK_CHANGED: 文件已改变，请重新扫描后再保存")
    name, mood = body.get("name"), body.get("mood", "")
    rights = body.get("rightsConfirmed")
    if not isinstance(name, str) or not name.strip() or len(name)>200 or not isinstance(mood,str) or len(mood)>500 or not isinstance(rights,bool):
        raise ValueError("BGM_METADATA_INVALID: 请填写曲名、风格与使用权状态")
    asset_id = asset_ref[len(PREFIX):]
    with db.transaction(immediate=True) as conn:
        row = conn.execute("SELECT notes FROM packaging_assets WHERE asset_id=?", (asset_id,)).fetchone()
        data = _notes(row["notes"])
        if data.get("sha256") != item["source_sha256"]:
            raise RuntimeError("BGM_TRACK_CHANGED: 音乐信息已更新，请刷新")
        data.update(mood=mood.strip(), license="commercial-approved" if rights else "unknown")
        conn.execute("UPDATE packaging_assets SET name=?,notes=? WHERE asset_id=?", (name.strip(),json.dumps(data,ensure_ascii=False),asset_id))
    return {"assetRef": asset_ref}

def catalog(db):
    library_rows = folders(db)
    for folder in library_rows:
        job = connection(db).execute("SELECT job_id,status FROM jobs WHERE library_id=? AND job_type='scan' ORDER BY rowid DESC LIMIT 1", (folder["library_id"],)).fetchone()
        folder["jobStatus"] = job["status"] if job else None
        folder["jobId"] = job["job_id"] if job else None
    return {"folders": library_rows, "tracks": [{"assetRef": x["id"], "name": x["name"], "mood": x["mood"],
        "available": x["local_available"], "rightsConfirmed": x["license"] == "commercial-approved",
        "sourceSha256": x["source_sha256"], "durationSeconds": x["duration_s"],
        "previewUrl": "/api/packaging-assets/bgm/"+x["id"]+"/preview" if x["local_available"] else None}
        for x in manifest_items(db)]}
