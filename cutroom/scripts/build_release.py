from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
import zipfile


CONTRACT_PATH = Path("release/release-files.json")
FORBIDDEN_PARTS = {
    ".git",
    ".worktrees",
    ".venv",
    "node_modules",
    "runtime",
    "out",
    "assets-local",
    "__pycache__",
}
FORBIDDEN_FILES = {".env"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log"}
EMPTY_RUNTIME_ROOTS = {"runtime", "out", "assets-local"}
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class BuildResult:
    zip_path: Path
    checksum_path: Path
    manifest_path: Path


def _normalise(raw: str) -> PurePosixPath:
    path = PurePosixPath(str(raw).replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe release path: {raw!r}")
    return path


def _is_forbidden(path: PurePosixPath) -> bool:
    return path.name in FORBIDDEN_FILES or any(part in FORBIDDEN_PARTS for part in path.parts)


def validate_contract(contract: dict) -> None:
    for key in ("files", "directories"):
        values = contract.get(key)
        if not isinstance(values, list):
            raise ValueError(f"release contract field {key!r} must be a list")
        for raw in values:
            path = _normalise(raw)
            if _is_forbidden(path):
                raise ValueError(f"forbidden release path: {raw}")

    create_directories = contract.get("create_directories", [])
    if not isinstance(create_directories, list):
        raise ValueError("release contract field 'create_directories' must be a list")
    for raw in create_directories:
        path = _normalise(raw)
        if _is_forbidden(path) and path.parts[0] not in EMPTY_RUNTIME_ROOTS:
            raise ValueError(f"forbidden release path: {raw}")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _copy_file(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise ValueError(f"symbolic links are not allowed in a release: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _should_skip(relative: PurePosixPath) -> bool:
    return _is_forbidden(relative) or relative.suffix.lower() in SKIP_SUFFIXES


def _git_commit(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _write_deterministic_zip(source_root: Path, zip_path: Path, archive_root: str) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
            relative = source.relative_to(source_root).as_posix()
            info = zipfile.ZipInfo(f"{archive_root}/{relative}", FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            with source.open("rb") as input_stream, archive.open(info, "w") as output_stream:
                shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)


def build_release(
    root: Path,
    output: Path,
    version: str,
    commit: str | None = None,
) -> BuildResult:
    root = root.resolve()
    output = output.resolve()
    contract_file = root / CONTRACT_PATH
    contract = json.loads(contract_file.read_text(encoding="utf-8"))
    validate_contract(contract)

    package_name = f"CUTROOM-v{version}"
    zip_path = output / f"{package_name}-windows-x64.zip"
    checksum_path = zip_path.with_suffix(zip_path.suffix + ".sha256")
    legacy_checksum_path = output / f"{package_name}-SHA256SUMS.txt"
    output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="cutroom-release-") as temporary:
        staging = Path(temporary) / package_name
        staging.mkdir(parents=True)

        for raw in contract["files"]:
            relative = _normalise(raw)
            source = root.joinpath(*relative.parts)
            if not source.is_file():
                raise FileNotFoundError(f"required release file is missing: {relative.as_posix()}")
            _copy_file(source, staging.joinpath(*relative.parts))

        for raw in contract["directories"]:
            relative = _normalise(raw)
            source_dir = root.joinpath(*relative.parts)
            if not source_dir.is_dir():
                raise FileNotFoundError(f"required release directory is missing: {relative.as_posix()}")
            for source in sorted(path for path in source_dir.rglob("*") if path.is_file()):
                child = PurePosixPath(source.relative_to(root).as_posix())
                if _should_skip(child):
                    continue
                _copy_file(source, staging.joinpath(*child.parts))

        for raw in contract.get("create_directories", []):
            relative = _normalise(raw)
            directory = staging.joinpath(*relative.parts)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / ".gitkeep").write_text("", encoding="utf-8")

        file_records = []
        for path in sorted(item for item in staging.rglob("*") if item.is_file()):
            file_records.append(
                {
                    "path": path.relative_to(staging).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": _hash_file(path),
                }
            )
        manifest = {
            "product": "CUTROOM",
            "version": version,
            "commit": commit or _git_commit(root),
            "platform": "windows-x64",
            "files": file_records,
        }
        manifest_path = staging / "release-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        if zip_path.exists():
            zip_path.unlink()
        _write_deterministic_zip(staging, zip_path, package_name)

    legacy_checksum_path.unlink(missing_ok=True)
    _write_text_atomic(checksum_path, f"{_hash_file(zip_path)}  {zip_path.name}\n")
    return BuildResult(zip_path, checksum_path, Path(f"{package_name}/release-manifest.json"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the CUTROOM Windows Beta release package.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, default=Path("release/dist"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = build_release(args.root, args.output, args.version)
    print(result.zip_path)
    print(result.checksum_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
