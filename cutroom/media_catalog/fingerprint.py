"""Bounded-memory fingerprints for large local media files."""

import hashlib
from pathlib import Path


def quick_fingerprint(path: Path, block_size: int = 1024 * 1024) -> str:
    if block_size <= 0:
        raise ValueError("block_size 必须大于 0")
    size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as handle:
        digest.update(handle.read(block_size))
        if size > block_size:
            handle.seek(max(0, size - block_size))
            digest.update(handle.read(block_size))
    return digest.hexdigest()


def full_sha256(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
