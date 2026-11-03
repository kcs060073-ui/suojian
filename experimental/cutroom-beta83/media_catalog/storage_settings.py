"""Persistent settings for the user-owned personal packaging asset root."""

from __future__ import annotations

from pathlib import Path


PERSONAL_PACKAGING_SETTING_KEY = "personal_packaging_root"
PERSONAL_PACKAGING_CATEGORIES = (
    ("subtitle", "字幕预设", "subtitle-presets/inbox"),
    ("voice", "音色", "voice-profiles/inbox"),
    ("bgm", "BGM", "bgm/inbox"),
    ("sfx", "音效", "sfx/inbox"),
    ("title_card", "文字卡", "visual-cards/inbox"),
    ("sticker", "贴纸", "stickers/inbox"),
    ("fancy_text", "花字", "fancy-text-presets/inbox"),
    ("brand", "Logo / 水印", "brand/inbox"),
)


def default_personal_packaging_root(project_root: Path) -> Path:
    return (Path(project_root).resolve() / "assets-local").resolve()


def personal_packaging_root(db, project_root: Path) -> Path:
    row = db.connection.execute(
        "SELECT path FROM storage_settings WHERE setting_key = ?",
        (PERSONAL_PACKAGING_SETTING_KEY,),
    ).fetchone()
    if row is None:
        return default_personal_packaging_root(project_root)
    return Path(str(row["path"])).resolve(strict=False)


def personal_packaging_status(db, project_root: Path) -> dict:
    root = personal_packaging_root(db, project_root)
    default_root = default_personal_packaging_root(project_root)
    return {
        "path": str(root),
        "isDefault": root == default_root,
        "exists": root.is_dir(),
        "categories": [
            {
                "key": key,
                "label": label,
                "path": str(root / Path(relative_path)),
                "exists": (root / Path(relative_path)).is_dir(),
            }
            for key, label, relative_path in PERSONAL_PACKAGING_CATEGORIES
        ],
    }


def configure_personal_packaging_root(db, project_root: Path, raw_path: str) -> dict:
    candidate = Path(str(raw_path).strip())
    if not candidate.is_absolute():
        raise ValueError("personal packaging root must be absolute")
    root = candidate.resolve(strict=False)
    if not root.is_dir():
        raise ValueError("personal packaging root must be an existing directory")

    for _, _, relative_path in PERSONAL_PACKAGING_CATEGORIES:
        (root / Path(relative_path)).mkdir(parents=True, exist_ok=True)

    with db.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO storage_settings(setting_key, path, updated_at)
            VALUES(?, ?, datetime('now'))
            ON CONFLICT(setting_key) DO UPDATE SET
              path = excluded.path,
              updated_at = excluded.updated_at
            """,
            (PERSONAL_PACKAGING_SETTING_KEY, str(root)),
        )
    return personal_packaging_status(db, project_root)