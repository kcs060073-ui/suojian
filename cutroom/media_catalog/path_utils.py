"""Windows extended-length paths for local read-only media operations."""

import os
from pathlib import Path


def filesystem_path(path: Path) -> Path:
    """Return an absolute extended path on Windows, otherwise the input path."""
    value = Path(path)
    if os.name != "nt":
        return value
    absolute = str(value.resolve())
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute.lstrip("\\"))
    return Path("\\\\?\\" + absolute)
