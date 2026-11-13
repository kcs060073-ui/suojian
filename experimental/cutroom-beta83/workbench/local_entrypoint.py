"""Authenticate the running local installation before opening its normal bootstrap."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener
import webbrowser

from media_catalog.credentials import WindowsCredentialBackend
from .security import SessionSecurity, SESSION_SECRET_BYTES


class LocalLaunchError(Exception):
    """Safe, user-facing launch failure without credentials or private paths."""


def startup_reference(project_root: Path, db_path: Path, port: int, *, maintenance_read_only=False) -> str:
    identity = [os.path.normcase(str(Path(project_root).resolve())),
                os.path.normcase(str(Path(db_path).resolve())), int(port)]
    if maintenance_read_only:
        identity.append('maintenance-read-only')
    scope = json.dumps(identity,
                       ensure_ascii=True, separators=(",", ":"))
    return "cutroom/local-startup/" + hashlib.sha256(scope.encode("ascii")).hexdigest()


def register_startup(runtime, *, backend=None) -> None:
    storage = backend if backend is not None else WindowsCredentialBackend()
    port = int(runtime.origin.rsplit(":", 1)[1])
    storage.write(startup_reference(runtime.project_root, runtime.db_path, port,
                                    maintenance_read_only=runtime.maintenance_read_only),
                  runtime.security.startup_key)


def verify_local(project_root: Path, db_path: Path, port: int, *, backend=None,
                 maintenance_read_only=False, session_secret_path=None) -> str:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise LocalLaunchError("剪辑工作台 启动端口无效。")
    reference = startup_reference(project_root, db_path, port,
                                  maintenance_read_only=maintenance_read_only)
    try:
        storage = backend if backend is not None else WindowsCredentialBackend()
        key = storage.read(reference)
        if not isinstance(key, str) or not 16 <= len(key) <= 256 or not key.isascii():
            raise ValueError("startup credential unavailable")
        secret_path = Path(session_secret_path or Path(db_path).resolve().parent / 'session-signing.key')
        if maintenance_read_only and (session_secret_path is None
                                     or secret_path.resolve().is_relative_to(Path(project_root).resolve())):
            raise ValueError('maintenance authentication state must be separate')
        secret = secret_path.read_bytes()
        if len(secret) != SESSION_SECRET_BYTES:
            raise ValueError("session secret invalid")
    except (OSError, ValueError, RuntimeError) as error:
        raise LocalLaunchError("剪辑工作台 尚未准备好，请稍后重试启动；仍失败时请联系维护同事。") from error
    nonce = secrets.token_hex(32)
    request = Request(f"http://127.0.0.1:{port}/auth/local-ready?nonce={nonce}",
                      headers={"Accept": "application/json"})
    # Only a random nonce leaves this process. Never send credentials to an unverified listener.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=1) as response:
            body = response.read(2049)
            if len(body) > 2048 or response.status != 200:
                raise ValueError("invalid readiness reply")
            value = json.loads(body)
        expected = SessionSecurity(key, session_secret=secret).local_launch_proof(reference, nonce)
        if (not isinstance(value, dict) or set(value) != {"proof"}
                or not isinstance(value["proof"], str)
                or not hmac.compare_digest(value["proof"], expected)):
            raise ValueError("installation mismatch")
    except (HTTPError, URLError, OSError, ValueError, TypeError) as error:
        raise LocalLaunchError("剪辑工作台 服务未准备好，或端口被其他程序占用；未打开其他程序，请联系维护同事。") from error
    return f"http://127.0.0.1:{port}/auth/bootstrap?key={quote(key, safe='')}"


from urllib.request import HTTPRedirectHandler


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_local(project_root: Path, db_path: Path, port: int, *, backend=None,
               browser_open=webbrowser.open, maintenance_read_only=False,
               session_secret_path=None) -> None:
    url = verify_local(project_root, db_path, port, backend=backend,
                       maintenance_read_only=maintenance_read_only,
                       session_secret_path=session_secret_path)
    if browser_open(url) is False:
        raise LocalLaunchError("无法打开默认浏览器，请设置默认浏览器后重新启动 剪辑工作台。")


def main(argv=None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="安全打开本机 剪辑工作台")
    parser.add_argument("--db", type=Path, default=root / "runtime/workbench/catalog.sqlite")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--wait", type=int, default=0)
    parser.add_argument('--maintenance-read-only', action='store_true')
    parser.add_argument('--session-state-dir', type=Path)
    args = parser.parse_args(argv)
    if not 0 <= args.wait <= 30:
        parser.error("--wait must be between 0 and 30 seconds")
    deadline = time.monotonic() + args.wait
    while True:
        try:
            options = dict(maintenance_read_only=args.maintenance_read_only,
                           session_secret_path=(args.session_state_dir / 'session-signing.key'
                                                if args.session_state_dir else None))
            if args.open:
                open_local(root, args.db, args.port, **options)
            else:
                verify_local(root, args.db, args.port, **options)
            return 0
        except LocalLaunchError as error:
            if time.monotonic() >= deadline:
                from .diagnostics import DiagnosticLog
                DiagnosticLog(root, enabled=not args.maintenance_read_only).record("startup", error=error, code="LOCAL_OPEN_FAILED")
                print(str(error))
                return 1
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    raise SystemExit(main())
