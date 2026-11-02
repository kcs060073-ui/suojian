"""Create an isolated SQLite copy for workbench acceptance testing."""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = (
    PROJECT_ROOT / "runtime" / "media-catalog" / "p2-v2-acceptance.sqlite"
)
DEFAULT_DESTINATION = (
    PROJECT_ROOT
    / "runtime"
    / "workbench"
    / "acceptance"
    / "p2-xinjiang-v2.sqlite"
)


@dataclass(frozen=True)
class BackupResult:
    source: Path
    destination: Path
    previous_backup: Path | None
    source_sha256: str
    destination_sha256: str
    integrity_check: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _readonly_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _sqlite_backup(source: Path, destination: Path) -> None:
    with closing(_readonly_connection(source)) as source_connection:
        with closing(sqlite3.connect(destination)) as destination_connection:
            source_connection.backup(destination_connection)
            destination_connection.commit()


def _previous_backup_path(destination: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = destination.with_name(
        f"{destination.stem}.before-{stamp}{destination.suffix}"
    )
    if candidate.exists():
        candidate = destination.with_name(
            f"{destination.stem}.before-{stamp}-{uuid.uuid4().hex[:8]}"
            f"{destination.suffix}"
        )
    return candidate


def backup_acceptance_database(
    source: Path,
    destination: Path,
    *,
    replace: bool = False,
) -> BackupResult:
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"验收源库不存在：{source}")
    if source == destination:
        raise ValueError("验收源库和副本不能是同一个文件")
    if destination.exists() and not replace:
        raise FileExistsError(
            f"验收副本已存在：{destination}；如需更新，请显式传入 --replace"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    source_hash_before = _sha256(source)
    previous_backup = None
    if destination.exists():
        previous_backup = _previous_backup_path(destination)
        _sqlite_backup(destination, previous_backup)

    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.preparing"
    )
    try:
        _sqlite_backup(source, temporary)
        with closing(_readonly_connection(temporary)) as connection:
            integrity_check = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity_check != "ok":
            raise RuntimeError(f"验收副本完整性检查失败：{integrity_check}")
        source_hash_after = _sha256(source)
        if source_hash_after != source_hash_before:
            raise RuntimeError("备份期间验收源库发生变化，未安装副本")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    return BackupResult(
        source=source,
        destination=destination,
        previous_backup=previous_backup,
        source_sha256=source_hash_before,
        destination_sha256=_sha256(destination),
        integrity_check=integrity_check,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从只读真实验收库创建 CUTROOM 工作台验收副本"
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="先备份已有验收副本，再安装新的 SQLite 在线备份",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = backup_acceptance_database(
        args.source,
        args.destination,
        replace=args.replace,
    )
    payload = asdict(result)
    for key in ("source", "destination", "previous_backup"):
        if payload[key] is not None:
            payload[key] = str(payload[key])
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
