"""Loopback-only persistent workbench service."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import signal
import threading
import time
import uuid
import webbrowser

from media_catalog.database import CatalogDatabase
from media_catalog.publish import validate_external_cover
from media_catalog.storage_status import CacheCleanupPlanStore
from media_catalog.workbench_actions import build_action_handlers
from media_catalog.workbench_worker import WorkbenchWorker, recover_interrupted_jobs

from .native_picker import NativePicker
from .routes import WorkbenchRouter
from .security import (
    DEFAULT_MAX_JSON_BYTES,
    DEFAULT_SESSION_TTL_SECONDS,
    SessionSecurity,
    load_or_create_session_secret,
)


LOOPBACK_HOST = "127.0.0.1"


class WorkbenchRuntime:
    def __init__(
        self,
        *,
        db_path: Path,
        static_root: Path,
        project_root: Path,
        picker,
        startup_key: str | None,
        session_secret: bytes,
        session_ttl_seconds: int,
        max_json_bytes: int,
        worker_handlers: dict | None,
        maintenance_read_only: bool = False,
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.static_root = Path(static_root).resolve()
        self.project_root = Path(project_root).resolve()
        self.picker = picker
        self.maintenance_read_only = maintenance_read_only
        import os
        self.security = SessionSecurity(
            startup_key,
            session_secret=session_secret,
            session_ttl_seconds=session_ttl_seconds,
            cookie_scope=os.path.normcase(str(self.db_path)),
        )
        self.max_json_bytes = max_json_bytes
        self._default_worker_handlers = worker_handlers is None
        self.worker_handlers = (
            None if worker_handlers is None else dict(worker_handlers)
        )
        self.cache_cleanup_plans = CacheCleanupPlanStore()
        from .diagnostics import DiagnosticLog
        self.diagnostics = DiagnosticLog(self.project_root, enabled=not maintenance_read_only)
        from .performance import PerformanceLog
        from media_catalog.performance import configure
        self.performance_log = PerformanceLog(self.project_root, enabled=not maintenance_read_only)
        self.performance_recorder = (None if maintenance_read_only else
                                     configure(self.db_path, self.performance_log.record))
        self._diagnostics: set[str] = set()
        self._diagnostics_lock = threading.Lock()
        self._picked_image_previews: dict[str, tuple[Path, dict, float]] = {}
        self._picked_image_lock = threading.Lock()
        self.origin = ""
        self.stop_event = threading.Event()
        self.worker_ready = threading.Event()
        self.worker_thread = threading.Thread(
            target=self._worker_main,
            name="cutroom-persistent-worker",
            daemon=True,
        )

    def prepare(self) -> int:
        if self.maintenance_read_only:
            with self.open_database():
                return 0
        from media_catalog.storage_safety import require_database_startup, record_database_initialized
        require_database_startup(self.db_path, self.project_root)
        with CatalogDatabase.open(self.db_path) as db:
            db.migrate()
            record_database_initialized(self.db_path, self.project_root)
            from media_catalog.task_create_idempotency import recover_task_creations
            creation_recovery = recover_task_creations(db, self.project_root)
            for route, result in creation_recovery.items():
                if result.get("failed"):
                    self.diagnostics.record("startup", error=RuntimeError(str(result["failed"])), code="TASK_CREATION_RECOVERY_FAILED")
            return recover_interrupted_jobs(db)

    def open_database(self) -> CatalogDatabase:
        if self.maintenance_read_only:
            from .maintenance_readonly import open_recovery_database
            return open_recovery_database(self.db_path)
        return CatalogDatabase.open(self.db_path)

    def start_worker(self) -> None:
        if self.maintenance_read_only:
            self.worker_ready.set()
            return
        self.worker_thread.start()
        if self._default_worker_handlers:
            from media_catalog.storage_safety import run_protection
            self.protection_thread = threading.Thread(
                target=run_protection,
                args=(self.stop_event, self.db_path, self.project_root,
                      lambda error: self.diagnostics.record("storage", error=error, code="TASK_BACKUP_FAILED")),
                name="cutroom-task-protection", daemon=True)
            self.protection_thread.start()
        if not self.worker_ready.wait(timeout=5):
            raise RuntimeError("工作台后台执行器启动超时")

    def _worker_main(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._run_worker()
                return
            except Exception as error:
                self.diagnostics.record("worker", error=error, code="WORKER_STOPPED")
                # The previous handler has unwound. Reopen the connection and let
                # normal lease recovery mark expired work interrupted. Never replay it.
                if self.stop_event.wait(5):
                    return

    def _run_worker(self) -> None:
        if self.worker_handlers == {}:
            self.worker_ready.set()
            self.stop_event.wait()
            return
        with CatalogDatabase.open(self.db_path) as db:
            if self._default_worker_handlers:
                self.worker_handlers = build_action_handlers(db, self.project_root)
            worker = WorkbenchWorker(
                db,
                worker_id=f"server-{uuid.uuid4().hex}",
                handlers=self.worker_handlers,
                on_error=lambda error, job: self.diagnostics.record("worker", error=error, code="JOB_FAILED", task_id=job.task_id, job_id=job.job_id),
            )
            self.worker_ready.set()
            worker.run_forever(self.stop_event)

    def close(self) -> None:
        self.stop_event.set()
        protection = getattr(self, "protection_thread", None)
        if protection is not None and protection.is_alive():
            protection.join(timeout=10)
        if self.worker_thread.is_alive():
            self.worker_thread.join(timeout=10)

        from media_catalog.performance import unregister
        if self.performance_recorder is not None:
            unregister(self.db_path, expected=self.performance_recorder)

    def record_diagnostic(self, diagnostic_id: str) -> None:
        self.diagnostics.record("api", diagnostic_id=diagnostic_id, code="INTERNAL_ERROR")
        with self._diagnostics_lock:
            if len(self._diagnostics) >= 200:
                self._diagnostics.pop()
            self._diagnostics.add(diagnostic_id)

    def has_diagnostic(self, diagnostic_id: str) -> bool:
        with self._diagnostics_lock:
            return diagnostic_id in self._diagnostics or self.diagnostics.has(diagnostic_id)

    def register_picked_image(self, value: str) -> tuple[str, str]:
        path, signature = validate_external_cover(value)
        token = uuid.uuid4().hex
        now = time.monotonic()
        with self._picked_image_lock:
            self._picked_image_previews = {
                key: item for key, item in self._picked_image_previews.items()
                if item[2] > now
            }
            if len(self._picked_image_previews) >= 32:
                oldest = min(self._picked_image_previews, key=lambda key: self._picked_image_previews[key][2])
                del self._picked_image_previews[oldest]
            self._picked_image_previews[token] = (path, signature, now + 600)
        return token, signature["sha256"]

    def resolve_picked_image(self, token: str) -> Path:
        with self._picked_image_lock:
            item = self._picked_image_previews.get(token)
            if item is None or item[2] <= time.monotonic():
                raise KeyError(token)
            path, signature, _ = item
        try:
            current_path, current_signature = validate_external_cover(path)
        except (OSError, ValueError) as error:
            raise RuntimeError("所选图片已变化，请重新选择。") from error
        if current_path != path or current_signature != signature:
            raise RuntimeError("所选图片已变化，请重新选择。")
        return path


class _RequestHandler(BaseHTTPRequestHandler):
    server_version = "CutroomWorkbench"
    sys_version = ""

    def do_GET(self) -> None:
        self.server.router.dispatch(self)

    def do_HEAD(self) -> None:
        self.server.router.dispatch(self)

    def do_POST(self) -> None:
        self.server.router.dispatch(self)

    def do_PUT(self) -> None:
        self.server.router.dispatch(self)

    def do_PATCH(self) -> None:
        self.server.router.dispatch(self)

    def do_DELETE(self) -> None:
        self.server.router.dispatch(self)

    def log_message(self, _format: str, *_args) -> None:
        return


class WorkbenchHTTPServer(ThreadingHTTPServer):
    # Allow module-load connection bursts while the accept thread is scheduled.
    request_queue_size = 32
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, runtime: WorkbenchRuntime):
        self.runtime = runtime
        self.router = WorkbenchRouter(runtime)
        super().__init__(address, _RequestHandler)

    def server_close(self) -> None:
        self.runtime.close()
        super().server_close()


def create_server(
    *,
    db_path: Path,
    port: int = 8765,
    static_root: Path | None = None,
    project_root: Path | None = None,
    picker=None,
    startup_key: str | None = None,
    session_secret_path: Path | None = None,
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
    max_json_bytes: int = DEFAULT_MAX_JSON_BYTES,
    worker_handlers: dict | None = None,
    maintenance_read_only: bool = False,
) -> WorkbenchHTTPServer:
    root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
    if (root / "runtime/software-updates/pending.json").exists():
        raise RuntimeError("存在未完成的升级，请运行产品更新升级.bat恢复后再打开。")
    static = Path(static_root or root / "web-ui" / "public").resolve()
    resolved_db_path = Path(db_path).resolve()
    if not static.is_dir():
        raise ValueError(f"静态资源目录不存在：{static}")
    if not isinstance(max_json_bytes, int) or max_json_bytes <= 0:
        raise ValueError("max_json_bytes 必须是正整数")
    secret_path = Path(
        session_secret_path or resolved_db_path.parent / "session-signing.key"
    ).resolve()
    if maintenance_read_only:
        if session_secret_path is None or secret_path.is_relative_to(root):
            raise ValueError('维护只读模式需要安装目录之外的显式会话状态目录。')
        if not resolved_db_path.is_relative_to(root):
            raise ValueError('维护只读数据库必须位于当前恢复安装目录内。')
        from .maintenance_readonly import open_recovery_database
        with open_recovery_database(resolved_db_path):
            pass
    runtime = WorkbenchRuntime(
        db_path=resolved_db_path,
        static_root=static,
        project_root=root,
        picker=picker or NativePicker(),
        startup_key=startup_key or secrets.token_urlsafe(32),
        session_secret=load_or_create_session_secret(secret_path),
        session_ttl_seconds=session_ttl_seconds,
        max_json_bytes=max_json_bytes,
        worker_handlers=worker_handlers,
        maintenance_read_only=maintenance_read_only,
    )
    runtime.session_secret_path = secret_path
    runtime.prepare()
    server = WorkbenchHTTPServer((LOOPBACK_HOST, int(port)), runtime)
    runtime.origin = f"http://{LOOPBACK_HOST}:{server.server_address[1]}"
    runtime.start_worker()
    return server


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="启动本机视频制作工作台")
    parser.add_argument(
        "--db",
        type=Path,
        default=root / "runtime" / "workbench" / "catalog.sqlite",
        help="工作台 SQLite 数据库路径",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument('--maintenance-read-only', action='store_true', help='只读回看停机后的恢复副本，不迁移或执行作业')
    parser.add_argument('--session-state-dir', type=Path, help='维护登录状态目录，须位于安装目录之外')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from media_catalog.database import UnsupportedSchemaVersionError
    try:
        server = create_server(db_path=args.db, port=args.port,
                               maintenance_read_only=args.maintenance_read_only,
                               session_secret_path=(args.session_state_dir / 'session-signing.key'
                                                    if args.session_state_dir else None))
    except Exception as error:
        from .diagnostics import DiagnosticLog
        DiagnosticLog(Path(__file__).resolve().parents[1], enabled=not args.maintenance_read_only).record("startup", error=error, code="SERVER_START_FAILED")
        print(f"剪辑工作台 未启动：{error}")
        return 1
    # The shipped Windows launcher reopens the existing service without a plaintext key file.
    import os
    if os.name == "nt":
        from .local_entrypoint import register_startup
        try:
            register_startup(server.runtime)
        except (OSError, ValueError, RuntimeError) as error:
            server.runtime.diagnostics.record("startup", error=error, code="STARTUP_CREDENTIAL_FAILED")
            server.server_close()
            print("无法登记本机启动凭据，请联系维护同事。")
            return 1
    key = server.runtime.security.startup_key
    url = f"{server.runtime.origin}/auth/bootstrap?key={key}"
    print("视频制作工作台已启动")
    if not args.no_open:
        print(f"请打开：{url}")
    print(f"数据保存：{server.runtime.db_path}")
    if not args.no_open:
        webbrowser.open(url)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, lambda *_: threading.Thread(
            target=server.shutdown, name='cutroom-stop', daemon=True).start())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        server.runtime.diagnostics.record("startup", error=error, code="SERVER_STOPPED")
        return 1
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
