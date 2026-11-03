"""Link-free voice paths with no repeated resolution for ordinary descendants."""
import os
from pathlib import Path, PureWindowsPath
import stat


def _ordinary_spelling(path):
    # Only ordinary local names may skip the OS resolver. Device/UNC namespaces,
    # ADS, short-name aliases and Win32 normalization keep the original path.
    if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
        return False
    if os.name != 'nt':
        return True
    if len(path.drive) != 2 or path.drive[1] != ':' or not path.drive[0].isalpha():
        return False
    return all(
        name not in ('.', '..') and not name.endswith((' ', '.'))
        and not PureWindowsPath(name).is_reserved()
        and not any(char in name for char in '~:<>"|?*')
        and not any(ord(char) < 32 for char in name)
        for name in path.parts[1:]
    )


def safe_voice_path(path, root, error_code):
    path = Path(path).absolute()
    try:
        # Recheck on every call; do not cache mutable directory metadata.
        # Inspect the original components before normalizing '..', so a link
        # followed by '..' cannot disappear from the security check.
        for component in (path, *path.parents):
            try:
                metadata = component.lstat()
            except FileNotFoundError:
                continue  # Assembly output files/directories may not exist yet.
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & 0x400:
                raise ValueError(error_code)
        if root is not None:
            root = Path(root)
            ordinary_child = _ordinary_spelling(path) and _ordinary_spelling(root) and path.is_relative_to(root)
            if not ordinary_child and not path.resolve().is_relative_to(root):
                # Keep resolution for alternate path spellings (e.g. Windows
                # short names), and preserve rejection of actual escapes.
                raise ValueError(error_code)
    except OSError as exc:
        raise ValueError(error_code) from exc
    return path
