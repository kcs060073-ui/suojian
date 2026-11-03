"""Bind a finalized ad plan, timing table, and complete voice to exact file bytes."""

from __future__ import annotations

import hashlib
from pathlib import Path


_ARTIFACT_NAMES = ("adplan.json", "adtiming.json", "voice.mp3")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_ad_artifact_hashes(ad_dir: Path) -> dict[str, str]:
    return {name: _sha256(ad_dir / name) for name in _ARTIFACT_NAMES}


def ad_artifact_hashes_match(ad_dir: Path, metadata: dict) -> bool:
    recorded = metadata.get("artifact_sha256")
    if recorded is None:
        return True  # Older finalized tasks had only the script input binding.
    if not isinstance(recorded, dict) or set(recorded) != set(_ARTIFACT_NAMES):
        return False
    try:
        return all(
            isinstance(recorded[name], str)
            and len(recorded[name]) == 64
            and recorded[name] == _sha256(ad_dir / name)
            for name in _ARTIFACT_NAMES
        )
    except OSError:
        return False
