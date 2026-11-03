"""Security boundaries for the loopback-only workbench HTTP service."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from typing import Callable, Mapping
from urllib.parse import unquote


SESSION_COOKIE = "cutroom_session"
DEFAULT_MAX_JSON_BYTES = 1024 * 1024
MAX_XLSX_BYTES = 20 * 1024 * 1024
DEFAULT_SESSION_TTL_SECONDS = 365 * 24 * 60 * 60
SESSION_SECRET_BYTES = 32


@dataclass(frozen=True)
class HttpError(Exception):
    status: int
    code: str
    public_message: str

    def __str__(self) -> str:
        return self.public_message


def load_or_create_session_secret(path: Path) -> bytes:
    secret_path = Path(path)
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    created = secrets.token_bytes(SESSION_SECRET_BYTES)
    try:
        with secret_path.open("xb") as stream:
            stream.write(created)
            stream.flush()
            os.fsync(stream.fileno())
        secret = created
    except FileExistsError:
        secret = secret_path.read_bytes()
    if len(secret) != SESSION_SECRET_BYTES:
        raise ValueError("本机会话密钥文件已损坏，请联系项目维护同事处理。")
    if os.name != "nt":
        try:
            secret_path.chmod(0o600)
        except OSError:
            pass
    return secret


class SessionSecurity:
    def __init__(
        self,
        startup_key: str | None = None,
        *,
        session_secret: bytes | None = None,
        session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        cookie_scope: str = "",
    ) -> None:
        if (
            not isinstance(session_ttl_seconds, int)
            or isinstance(session_ttl_seconds, bool)
            or session_ttl_seconds <= 0
        ):
            raise ValueError("session_ttl_seconds 必须是正整数")
        secret = session_secret or secrets.token_bytes(SESSION_SECRET_BYTES)
        if not isinstance(secret, bytes) or len(secret) < SESSION_SECRET_BYTES:
            raise ValueError("session_secret 至少需要 32 字节")
        self.startup_key = startup_key or secrets.token_urlsafe(32)
        self.session_ttl_seconds = session_ttl_seconds
        self._session_secret = secret
        # Cookies are scoped to a host, not a port. Independent local databases
        # have independent persisted secrets; keep their browser cookies apart.
        namespace_payload = json.dumps(["cutroom-session-cookie-v1", cookie_scope], ensure_ascii=True).encode("ascii")
        namespace = hmac.new(secret, namespace_payload, hashlib.sha256).hexdigest()[:20]
        self.cookie_name = f"{SESSION_COOKIE}_{namespace}"
        self._clock = clock

    def exchange(self, candidate: str | None) -> str:
        if not candidate or not secrets.compare_digest(candidate, self.startup_key):
            raise HttpError(403, "AUTH_INVALID", "启动凭据无效，请从启动窗口重新打开。")
        expires_at = int(self._clock()) + self.session_ttl_seconds
        payload = f"v1.{expires_at}.{secrets.token_urlsafe(24)}"
        return f"{payload}.{self._signature(payload)}"

    def require_session(self, headers: Mapping[str, str]) -> str:
        cookies = _parse_cookie(headers.get("Cookie", ""))
        # Existing pre-namespace sessions stay valid under the same signature.
        # A present but invalid scoped token must never fall back to a legacy one.
        session = cookies.get(self.cookie_name, cookies.get(SESSION_COOKIE))
        if session is None:
            raise HttpError(403, "AUTH_REQUIRED", "当前页面未获得本机工作台授权。")
        if not session.isascii():
            raise HttpError(403, "AUTH_REQUIRED", "当前页面未获得本机工作台授权。")
        try:
            version, raw_expiry, nonce, signature = session.split(".")
            expires_at = int(raw_expiry)
        except (TypeError, ValueError):
            raise HttpError(
                403, "AUTH_REQUIRED", "当前页面未获得本机工作台授权。"
            ) from None
        payload = f"{version}.{raw_expiry}.{nonce}"
        if (
            version != "v1"
            or not secrets.compare_digest(signature, self._signature(payload))
            or expires_at <= int(self._clock())
        ):
            raise HttpError(403, "AUTH_REQUIRED", "当前页面未获得本机工作台授权。")
        return session

    def local_launch_proof(self, reference: str, nonce: str) -> str:
        """Bind a public random challenge to this exact installation and boot key."""
        payload = json.dumps(["cutroom-local-launch-v1", reference, nonce, self.startup_key],
                             ensure_ascii=True, separators=(",", ":"))
        return hmac.new(self._session_secret, payload.encode("ascii"), hashlib.sha256).hexdigest()

    def _signature(self, payload: str) -> str:
        digest = hmac.new(
            self._session_secret,
            payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def validate_write(
        self,
        headers: Mapping[str, str],
        *,
        expected_origin: str,
        max_json_bytes: int = DEFAULT_MAX_JSON_BYTES,
    ) -> int:
        self.require_session(headers)
        if headers.get("Origin") != expected_origin:
            raise HttpError(403, "ORIGIN_REJECTED", "请求来源不是当前本机工作台。")
        content_type = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise HttpError(415, "JSON_REQUIRED", "该操作只接受 JSON 请求。")
        if headers.get("Transfer-Encoding"):
            raise HttpError(400, "BODY_LENGTH_REQUIRED", "请求体必须提供明确长度。")
        raw_length = headers.get("Content-Length")
        if raw_length is None:
            raise HttpError(411, "BODY_LENGTH_REQUIRED", "请求体必须提供明确长度。")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise HttpError(400, "BODY_LENGTH_INVALID", "请求体长度无效。") from error
        if length < 0:
            raise HttpError(400, "BODY_LENGTH_INVALID", "请求体长度无效。")
        if length > max_json_bytes:
            raise HttpError(413, "BODY_TOO_LARGE", "请求内容超过本机工作台限制。")
        return length

    def validate_xlsx_write(self, headers: Mapping[str, str], *, expected_origin: str) -> int:
        self.require_session(headers)
        if headers.get("Origin") != expected_origin:
            raise HttpError(403, "ORIGIN_REJECTED", "请求来源不是当前本机工作台。")
        content_type = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            raise HttpError(415, "XLSX_REQUIRED", "只接受 XLSX 台账文件。")
        if headers.get("Transfer-Encoding"):
            raise HttpError(400, "BODY_LENGTH_REQUIRED", "请求体必须提供明确长度。")
        lengths = headers.get_all("Content-Length", []) if hasattr(headers, "get_all") else [headers.get("Content-Length")]
        if len(lengths) != 1:
            raise HttpError(400, "BODY_LENGTH_REQUIRED", "请求体必须提供唯一长度。")
        try:
            length = int(lengths[0])
        except (TypeError, ValueError) as error:
            raise HttpError(400, "BODY_LENGTH_INVALID", "请求体长度无效。") from error
        if length <= 0 or length > MAX_XLSX_BYTES:
            raise HttpError(413, "WORKBOOK_SIZE_INVALID", "Excel 文件为空或超过 20 MB。")
        return length


def read_json(stream, length: int) -> dict:
    raw = stream.read(length)
    if len(raw) != length:
        raise HttpError(400, "BODY_INCOMPLETE", "请求内容不完整。")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HttpError(400, "JSON_INVALID", "请求内容不是有效 JSON。") from error
    if not isinstance(value, dict):
        raise HttpError(400, "JSON_OBJECT_REQUIRED", "请求内容必须是 JSON 对象。")
    return value


def resolve_static_file(static_root: Path, request_path: str) -> Path:
    """Resolve a URL path inside static_root, following links fail-closed."""
    decoded = unquote(request_path)
    if "\x00" in decoded or "\\" in decoded:
        raise HttpError(403, "PATH_REJECTED", "资源路径不安全。")
    relative = decoded.lstrip("/") or "index.html"
    if any(part in {"", ".", ".."} for part in relative.split("/")):
        raise HttpError(403, "PATH_REJECTED", "资源路径不安全。")
    root = Path(static_root).resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise HttpError(403, "PATH_REJECTED", "资源路径不安全。") from error
    return candidate


def validate_picker_result(value: str | None, *, kind: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        candidate = Path(value).expanduser().resolve()
    except (OSError, TypeError, ValueError) as error:
        raise HttpError(
            400,
            "PICKER_RESULT_INVALID",
            "选择结果已失效或类型不正确，请重新选择。",
        ) from error
    if kind == "directory":
        valid = candidate.is_dir()
    elif kind == "video":
        valid = candidate.is_file() and candidate.suffix.lower() in {
            ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"
        }
    elif kind == "packaging":
        valid = candidate.is_file() and candidate.suffix.lower() in {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".opus", ".png", ".jpg", ".jpeg", ".webp", ".gif"}
    elif kind == "image":
        valid = candidate.is_file() and candidate.suffix.lower() in {
            ".jpg", ".jpeg", ".png", ".webp"
        }
    else:
        raise ValueError(f"unknown picker kind: {kind}")
    if not valid:
        raise HttpError(400, "PICKER_RESULT_INVALID", "选择结果已失效或类型不正确，请重新选择。")
    return str(candidate)


def _parse_cookie(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in raw.split(";"):
        name, separator, value = item.strip().partition("=")
        if separator and name:
            result[name] = value
    return result
