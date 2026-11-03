"""Open an idle recovery copy without migration, WAL replay or new sidecars."""
from pathlib import Path
import sqlite3

from media_catalog.database import CatalogDatabase


def open_recovery_database(path: Path) -> CatalogDatabase:
    database = Path(path).resolve()
    if not database.is_file():
        raise FileNotFoundError('维护回看需要已存在的一致恢复数据库。')
    for suffix in ('-wal', '-journal'):
        sidecar = database.with_name(database.name + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError('恢复数据库仍有 WAL 或事务日志，请先停机并完成一致快照。')
    connection = sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=5)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA query_only=ON')
        migrations = Path(__file__).resolve().parents[1] / 'media_catalog/migrations'
        supported = {int(p.stem.split('_', 1)[0]) for p in migrations.glob('0*.sql')}
        applied = {row[0] for row in connection.execute('SELECT version FROM schema_migrations')}
        if applied != supported:
            raise ValueError('恢复数据库与本程序版本不匹配；只读回看不会执行迁移。')
        return CatalogDatabase(database, connection)
    except sqlite3.Error as error:
        connection.close()
        raise ValueError('恢复数据库结构不可读取；只读回看不会修复或重置数据库。') from error
    except BaseException:
        connection.close()
        raise
