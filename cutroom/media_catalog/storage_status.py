"""Read-only workbench storage facts and safe preview-cache cleanup."""

from __future__ import annotations

from pathlib import Path
from copy import deepcopy
import secrets
import shutil
import threading
import time

from .cache import cache_status, clean_cache, clean_cache_entries
from .storage_settings import personal_packaging_status


class CacheCleanupPlanStore:
    def __init__(self, *, ttl_seconds: int = 300, max_plans: int = 32) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_plans = max_plans
        self._plans: dict[str, tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()

    def create(self, entries: list[dict]) -> str:
        now = time.monotonic()
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge(now)
            while len(self._plans) >= self.max_plans:
                oldest = min(self._plans, key=lambda key: self._plans[key][0])
                self._plans.pop(oldest, None)
            self._plans[token] = (now + self.ttl_seconds, deepcopy(entries))
        return token

    def consume(self, token: str) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            plan = self._plans.pop(token, None)
        if plan is None:
            raise ValueError("CACHE_PREVIEW_STALE: 清理预览已失效，请重新预览")
        return plan[1]

    def _purge(self, now: float) -> None:
        for token, (expires_at, _) in list(self._plans.items()):
            if expires_at <= now:
                self._plans.pop(token, None)


def workbench_storage_status(db, project_root: Path) -> dict:
    root = Path(project_root).resolve()
    cache = cache_status(db)
    personal_packaging = personal_packaging_status(db, root)
    usage = shutil.disk_usage(root)
    directory_specs = (
        ("project", "项目目录", root),
        ("database", "工作台数据库", Path(db.path).resolve()),
        ("cache", "预览缓存", Path(cache["cache_dir"]).resolve()),
        ("coreAssets", "核心包装素材", root / "assets"),
        ("extensionPacks", "团队扩展素材包", root / "asset-packs"),
        ("personalAssets", "个人本地素材", Path(personal_packaging["path"])),
        ("outputs", "成片输出", root / "out"),
    )
    return {
        "disk": {
            "totalBytes": usage.total,
            "usedBytes": usage.used,
            "freeBytes": usage.free,
        },
        "personalPackaging": personal_packaging,
        "cache": {
            "directory": cache["cache_dir"],
            "entries": cache["entries"],
            "byKind": cache["by_kind"],
            "totalBytes": cache["total_bytes"],
            "missingFiles": cache["missing_files"],
            "exists": cache["exists"],
        },
        "directories": [
            {
                "key": key,
                "label": label,
                "path": str(path),
                "exists": path.exists(),
                "kind": "file" if path.is_file() else "directory",
            }
            for key, label, path in directory_specs
        ],
    }


def _public_cleanup(result: dict) -> dict:
    return {
        "dryRun": result["dry_run"],
        "deleted": result["deleted"],
        "failed": result["failed"],
        "freedBytes": result["freed_bytes"],
        "remainingBytes": result["remaining_bytes"],
        "remainingEstimated": result["remaining_estimated"],
    }


def preview_workbench_cache_cleanup(db, plans: CacheCleanupPlanStore) -> dict:
    result = clean_cache(db, dry_run=True)
    public = _public_cleanup(result)
    public["impactToken"] = plans.create(result["deleted"])
    return public


def clean_workbench_preview_cache(
    db, plans: CacheCleanupPlanStore, *, impact_token: str
) -> dict:
    return _public_cleanup(clean_cache_entries(db, plans.consume(impact_token)))
