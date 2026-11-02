"""SQLite lifecycle and migration support for the media catalog."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import sqlite3


class CatalogDatabase:
    def __init__(self, path: Path, connection: sqlite3.Connection):
        self.path = path
        self.connection = connection
        self._transaction_context_depth = 0
        self._task_source_write_authorizations: dict[str, int] = {}
        self.connection.create_function(
            "task_source_write_authorized",
            1,
            self._task_source_write_authorized,
        )

    def _task_source_write_authorized(self, task_id: str) -> int:
        return int(self._task_source_write_authorizations.get(str(task_id), 0) > 0)

    @contextmanager
    def task_source_write_authorization(
        self, task_id: str
    ) -> Iterator[sqlite3.Connection]:
        if self._transaction_context_depth <= 0:
            raise RuntimeError(
                "task source write authorization requires db.transaction()"
            )
        key = str(task_id)
        self._task_source_write_authorizations[key] = (
            self._task_source_write_authorizations.get(key, 0) + 1
        )
        try:
            yield self.connection
        finally:
            remaining = self._task_source_write_authorizations[key] - 1
            if remaining:
                self._task_source_write_authorizations[key] = remaining
            else:
                del self._task_source_write_authorizations[key]

    @classmethod
    def open(cls, path: Path) -> "CatalogDatabase":
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, timeout=30)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.row_factory = sqlite3.Row
        return cls(database_path, connection)

    def migrate(self, migration_dir: Path | None = None) -> None:
        migration_dir = migration_dir or Path(__file__).parent / "migrations"
        files = sorted(migration_dir.glob("0*.sql"))
        with self.connection:
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations("
                "version INTEGER PRIMARY KEY, applied_at TEXT)"
            )
        applied = {
            row["version"]
            for row in self.connection.execute(
                "SELECT version FROM schema_migrations"
            )
        }
        for migration_path in files:
            version = int(migration_path.stem.split("_", 1)[0])
            if version in applied:
                continue
            sql = migration_path.read_text(encoding="utf-8")
            transaction_sql = (
                "BEGIN IMMEDIATE;\n"
                f"{sql.rstrip()}\n"
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                f"VALUES({version}, datetime('now'));\n"
                "COMMIT;"
            )
            try:
                self.connection.executescript(transaction_sql)
            except sqlite3.Error:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise
            else:
                applied.add(version)
        from .tts_voice_catalog import import_legacy_tts_voices
        from .candidate_versions import recover_legacy_candidate_versions

        import_legacy_tts_voices(self)
        recover_legacy_candidate_versions(self)

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        self._transaction_context_depth += 1
        try:
            if immediate:
                if self.connection.in_transaction:
                    raise RuntimeError(
                        "immediate transaction cannot start inside an active transaction"
                    )
                self.connection.execute("BEGIN IMMEDIATE")
            with self.connection:
                yield self.connection
        finally:
            self._transaction_context_depth -= 1

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "CatalogDatabase":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
