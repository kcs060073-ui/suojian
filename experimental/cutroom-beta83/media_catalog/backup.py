"""Verified SQLite backup and recoverable, atomic restore operations."""

from contextlib import closing
from datetime import datetime
import os
from pathlib import Path
import shutil
import sqlite3
import uuid
from urllib.parse import quote

from .path_utils import filesystem_path


def backup_database(source: Path, backup_dir: Path) -> Path:
    source = Path(source)
    if not filesystem_path(source).is_file():
        raise FileNotFoundError(f"找不到要备份的数据库：{source}")
    backup_dir = Path(backup_dir)
    filesystem_path(backup_dir).mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"catalog-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite"
    with closing(sqlite3.connect("file:" + quote(str(filesystem_path(source)), safe="/\\:") + "?mode=ro", uri=True)) as source_connection:
        with closing(sqlite3.connect(filesystem_path(target))) as target_connection:
            source_connection.backup(target_connection)
            if target_connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("新备份数据库完整性检查失败")
    return target


def restore_database(backup: Path, target: Path) -> Path | None:
    backup = Path(backup)
    target = Path(target)
    if not backup.is_file():
        raise FileNotFoundError(f"找不到备份数据库：{backup}")
    if backup.resolve() == target.resolve():
        raise ValueError("备份文件和恢复目标不能是同一个文件")
    check = sqlite3.connect(backup)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("备份数据库完整性检查失败，已停止恢复")
    finally:
        check.close()

    target.parent.mkdir(parents=True, exist_ok=True)
    preserved: Path | None = None
    if target.exists():
        preserved = target.with_name(
            f"{target.stem}.pre-restore-{datetime.now():%Y%m%d-%H%M%S-%f}{target.suffix}"
        )
        shutil.copy2(target, preserved)

    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.restore.tmp")
    try:
        shutil.copy2(backup, temporary)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except Exception:
                # 临时文件清理 best-effort：本机回收站不可用会让安全删除守卫抛异常，
                # 绝不能让它掩盖真正的 restore 结果。
                pass
    return preserved
