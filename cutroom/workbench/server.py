"""Loopback-only persistent workbench service."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import threading
import uuid
import webbrowser

from media_catalog.database import CatalogDatabase
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
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.static_root = Path(static_root).resolve()
        self.project_root = Path(project_root).resolve()
        self.picker = picker
        self.security = SessionSecurity(
            startup_key,
            session_secret=session_secret,
            session_ttl_seconds=session_ttl_seconds,
        )
        self.max_json_bytes = max_json_bytes
        self.worker_handlers = (
            None if worker_handlers is None else dict(worker_handlers)
        )
        self.cache_cleanup_plans = CacheCleanupPlanStore()
        self._diagnostics: set[str] = set()
        self._diagnostics_lock = threading.Lock()
        self.origin = ""
        self.stop_event = threading.Event()
        self.worker_ready = threading.Event()
        self.worker_thread = threading.Thread(
            target=self._worker_main,
            name="cutroom-persistent-worker",
            daemon=True,
        )

    def prepare(self) -> int:
        with CatalogDatabase.open(self.db_path) as db:
            db.migrate()
            return recover_interrupted_jobs(db)

    def start_worker(self) -> None:
        self.worker_thread.start()
        if not self.worker_ready.wait(timeout=5):
            raise RuntimeError("工作台后台执行器启动超时")

    def _worker_main(self) -> None:
        if self.worker_handlers == {}:
            self.worker_ready.set()
            self.stop_event.wait()
            return
        with CatalogDatabase.open(self.db_path) as db:
            if self.worker_handlers is None:
                self.worker_handlers = build_action_handlers(db, self.project_root)
            worker = WorkbenchWorker(
                db,
                worker_id=f"server-{uuid.uuid4().hex}",
                handlers=self.worker_handlers,
            )
            self.worker_ready.set()
            worker.run_forever(self.stop_event)

    def close(self) -> None:
        self.stop_event.set()
        if self.worker_thread.is_alive():
            self.worker_thread.join(timeout=10)

    def record_diagnostic(self, diagnostic_id: str) -> None:
        with self._diagnostics_lock:
            if len(self._diagnostics) >= 200:
                self._diagnostics.pop()
            self._diagnostics.add(diagnostic_id)

    def has_diagnostic(self, diagnostic_id: str) -> bool:
        with self._diagnostics_lock:
            return diagnostic_id in self._diagnostics


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
) -> WorkbenchHTTPServer:
    root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
    static = Path(static_root or root / "web-ui" / "public").resolve()
    resolved_db_path = Path(db_path).resolve()
    if not static.is_dir():
        raise ValueError(f"静态资源目录不存在：{static}")
    if not isinstance(max_json_bytes, int) or max_json_bytes <= 0:
        raise ValueError("max_json_bytes 必须是正整数")
    secret_path = Path(
        session_secret_path or resolved_db_path.parent / "session-signing.key"
    ).resolve()
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
    )
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = create_server(db_path=args.db, port=args.port)
    key = server.runtime.security.startup_key
    url = f"{server.runtime.origin}/auth/bootstrap?key={key}"
    print("视频制作工作台已启动")
    print(f"请打开：{url}")
    print(f"数据保存：{server.runtime.db_path}")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
