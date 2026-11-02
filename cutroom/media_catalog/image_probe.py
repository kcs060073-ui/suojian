"""Fast, read-only image metadata probing without spawning ffprobe."""

from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .path_utils import filesystem_path


def probe_image(path: Path) -> dict[str, object]:
    source = filesystem_path(path)
    try:
        with Image.open(source) as image:
            image.verify()
        with Image.open(source) as image:
            orientation = image.getexif().get(274)
            return {
                "kind": "image",
                "format": image.format or path.suffix.lstrip(".").upper(),
                "width": image.width,
                "height": image.height,
                "mode": image.mode,
                "orientation": orientation,
                "has_alpha": "A" in image.getbands(),
            }
    except UnidentifiedImageError as error:
        raise OSError(f"无法识别图片格式：{path.name}") from error
