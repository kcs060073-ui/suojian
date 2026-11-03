"""Local poster drafts. No model calls, export or approval side effects."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import uuid
from PIL import Image
from .candidate_versions import list_candidates


def design_directory(db, root, task_id, candidate_id):
    candidates = list_candidates(db, task_id)
    if not candidates or candidates[0]["candidate_id"] != candidate_id or candidates[0].get("invalidation"):
        raise ValueError("成片版本已变化，请刷新页面后重新设计封面。")
    key = hashlib.sha256((task_id + "|" + candidate_id).encode()).hexdigest()
    return Path(root) / "runtime" / "workbench" / "cover-designs" / key


def save_cover_design(directory, encoded, settings, source=None):
    if not isinstance(encoded, str) or len(encoded) > 900000:
        raise ValueError("封面图片过大，请换用较小的底图。")
    try:
        data = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "JPEG" or image.size not in {(1080,1920),(1920,1080)}:
                raise ValueError("封面需要为高清横版或竖版 JPG。")
            image.load()
    except Exception as error:
        raise ValueError("封面图片无效，请重新生成。") from error
    if not isinstance(settings, dict):
        raise ValueError("封面设置无效。")
    clean = {k: str(settings.get(k, ""))[:80] for k in ("title", "subtitle", "eyebrow", "theme", "layout")}
    if not clean["title"].strip():
        raise ValueError("请填写封面标题。")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    image_path = directory / (digest + ".jpg")
    if not image_path.exists():
        image_path.write_bytes(data)
    record = {"image": image_path.name, "signature": digest, "settings": clean, "source": source or {}}
    temporary = directory / (uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, directory / "current.json")
    return record, image_path


def load_cover_design(directory):
    directory = Path(directory)
    if not (directory / "current.json").is_file():
        return None
    record = json.loads((directory / "current.json").read_text(encoding="utf-8"))
    digest = record.get("signature", "")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("封面记录损坏，请重新制作。")
    path = directory / (digest + ".jpg")
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("已保存封面缺失或变化，请重新制作。")
    return record, path
