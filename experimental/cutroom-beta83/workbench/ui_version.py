"""Fingerprint served UI files so open pages can detect local upgrades."""
import hashlib
from pathlib import Path
from threading import Lock
_cache = {}
_lock = Lock()
def ui_version(root):
    root = Path(root)
    paths = sorted(p for p in root.iterdir() if p.is_file() and p.suffix in {".mjs", ".js", ".css", ".html"})
    stamp = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
    key = str(root.resolve())
    with _lock:
        cached = _cache.get(key)
        if cached and cached[0] == stamp: return cached[1]
        digest = hashlib.sha256()
        for p in paths:
            digest.update(p.name.encode()); digest.update(b"\0"); digest.update(p.read_bytes())
        value = digest.hexdigest()
        _cache[key] = (stamp, value)
        return value
