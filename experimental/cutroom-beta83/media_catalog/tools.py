"""Discovery of external media tools without machine-specific hard coding."""

from collections.abc import Callable, Iterable, Mapping
import os
from pathlib import Path
import shutil


class ToolNotFound(RuntimeError):
    """Raised with a user-actionable message when a required tool is absent."""


def default_ffprobe_search_roots(
    env: Mapping[str, str] | None = None,
) -> list[Path]:
    environment = os.environ if env is None else env
    roots: list[Path] = []
    local_app_data = environment.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(Path(local_app_data) / "Microsoft" / "WinGet" / "Packages")
    for name in ("ProgramFiles", "ProgramFiles(x86)"):
        value = environment.get(name)
        if value:
            roots.append(Path(value) / "ffmpeg")
    roots.extend((Path(r"C:\ffmpeg"), Path(r"C:\tools\ffmpeg")))
    return roots


def find_ffprobe(
    env: Mapping[str, str],
    which: Callable[[str], str | None] = shutil.which,
    search_roots: Iterable[Path] = (),
    *,
    exists: Callable[[Path], bool] = lambda path: path.exists(),
) -> Path:
    explicit = env.get("FFPROBE_PATH")
    if explicit:
        path = Path(explicit)
        if exists(path):
            return path
        raise ToolNotFound(
            f"FFPROBE_PATH 指向的文件不存在：{path}。请重新选择 ffprobe.exe。"
        )

    discovered = which("ffprobe")
    if discovered:
        return Path(discovered)

    for root_value in search_roots:
        root = Path(root_value)
        if not root.exists():
            continue
        for candidate in root.rglob("ffprobe.exe"):
            if exists(candidate):
                return candidate

    raise ToolNotFound(
        "找不到 ffprobe。请安装 FFmpeg，或把 ffprobe.exe 的完整路径写入 "
        "FFPROBE_PATH 后重试。"
    )


def find_ffmpeg(
    env: Mapping[str, str],
    which: Callable[[str], str | None] = shutil.which,
    search_roots: Iterable[Path] = (),
    *,
    exists: Callable[[Path], bool] = lambda path: path.exists(),
) -> Path:
    explicit = env.get("FFMPEG_PATH")
    if explicit:
        path = Path(explicit)
        if exists(path):
            return path
        raise ToolNotFound(
            f"FFMPEG_PATH 指向的文件不存在：{path}。请重新选择 ffmpeg.exe。"
        )

    discovered = which("ffmpeg")
    if discovered:
        return Path(discovered)

    for root_value in search_roots:
        root = Path(root_value)
        if not root.exists():
            continue
        for candidate in root.rglob("ffmpeg.exe"):
            if exists(candidate):
                return candidate

    raise ToolNotFound(
        "找不到 ffmpeg。请安装 FFmpeg，或把 ffmpeg.exe 的完整路径写入 "
        "FFMPEG_PATH 后重试。"
    )
