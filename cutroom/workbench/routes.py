"""Minimal Task 9 routes; business APIs are added separately in Task 10."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, unquote, urlsplit

from media_catalog.database import CatalogDatabase
from media_catalog.candidate_versions import resolve_candidate_media
from media_catalog.packaging_plan import resolve_manifest_asset_path
from media_catalog.packaging_preview import (
    get_preview_artifact,
    resolve_packaging_preview_media,
)
from media_catalog.rough_media import (
    RoughMediaError,
    resolve_rough_media,
    rough_media_content_type,
)
from media_catalog.snapshots import file_content_signature, latest_review_candidate

from .api import WorkbenchAPI, translate_exception
from .security import (
    HttpError,
    SESSION_COOKIE,
    read_json,
    resolve_static_file,
    validate_picker_result,
)

_CLIENT_DISCONNECT_ERRORS = (
    BrokenPipeError,
    ConnectionAbortedError,
    ConnectionResetError,
)
_REJECTED_BODY_MAX_BYTES = 16 * 1024
_REJECTED_BODY_DRAIN_SECONDS = 0.15


class WorkbenchRouter:
    def __init__(self, runtime) -> None:
        self.runtime = runtime

    def dispatch(self, handler) -> None:
        parsed = urlsplit(handler.path)
        method = handler.command.upper()
        write_method = method in {"POST", "PUT", "PATCH", "DELETE"}
        body_read_started = False
        try:
            if method == "GET" and parsed.path == "/auth/bootstrap":
                self._bootstrap(handler, parse_qs(parsed.query))
                return
            if write_method:
                length = self.runtime.security.validate_write(
                    handler.headers,
                    expected_origin=self.runtime.origin,
                    max_json_bytes=self.runtime.max_json_bytes,
                )
                body_read_started = True
                handler.workbench_json = read_json(handler.rfile, length)
            if method == "GET" and parsed.path == "/api/service/status":
                self.runtime.security.require_session(handler.headers)
                self._service_status(handler)
                return
            rough_media = self._rough_media_parts(parsed.path)
            if method in {"GET", "HEAD"} and rough_media is not None:
                self.runtime.security.require_session(handler.headers)
                self._rough_media(
                    handler,
                    *rough_media,
                    head_only=method == "HEAD",
                )
                return
            if method in {"GET", "HEAD"} and self._is_review_media_path(parsed.path):
                self.runtime.security.require_session(handler.headers)
                review_parts = parsed.path.strip("/").split("/")
                self._review_media(
                    handler,
                    unquote(review_parts[2]),
                    candidate_id=(
                        unquote(review_parts[4]) if len(review_parts) == 5 else None
                    ),
                    head_only=method == "HEAD",
                )
                return
            generated_preview = self._packaging_preview_media_parts(parsed.path)
            if method in {"GET", "HEAD"} and generated_preview is not None:
                self.runtime.security.require_session(handler.headers)
                self._packaging_preview_media(
                    handler,
                    *generated_preview,
                    head_only=method == "HEAD",
                )
                return
            if method in {"GET", "HEAD"} and self._is_packaging_asset_preview_path(parsed.path):
                self.runtime.security.require_session(handler.headers)
                parts = parsed.path.split("/")
                self._packaging_preview(
                    handler,
                    unquote(parts[3]),
                    unquote(parts[4]),
                    head_only=method == "HEAD",
                )
                return
            if method in {"GET", "HEAD"} and self._is_voice_preview_path(parsed.path):
                self.runtime.security.require_session(handler.headers)
                parts = parsed.path.split("/")
                self._voice_preview(
                    handler,
                    parts[3],
                    parts[4][:-4],
                    head_only=method == "HEAD",
                )
                return
            if method == "GET" and parsed.path.startswith("/api/diagnostics/"):
                self.runtime.security.require_session(handler.headers)
                diagnostic_id = parsed.path.rsplit("/", 1)[-1]
                if not self.runtime.has_diagnostic(diagnostic_id):
                    raise HttpError(404, "NOT_FOUND", "诊断编号不存在。")
                self.send_json(handler, 200, {"diagnosticId": diagnostic_id})
                return
            if method == "POST" and parsed.path in {
                "/api/picker/video", "/api/picker/directory", "/api/picker/image"
            }:
                self._picker(handler, parsed.path.rsplit("/", 1)[-1])
                return
            if parsed.path.startswith("/api/"):
                self.runtime.security.require_session(handler.headers)
                with CatalogDatabase.open(self.runtime.db_path) as db:
                    result = WorkbenchAPI(db, self.runtime.project_root).dispatch(
                        method,
                        parsed.path,
                        getattr(handler, "workbench_json", None),
                        cache_cleanup_plans=self.runtime.cache_cleanup_plans,
                        query=parse_qs(parsed.query),
                    )
                self.send_json(handler, result.status, result.payload)
                return
            if method not in {"GET", "HEAD"}:
                raise HttpError(405, "METHOD_NOT_ALLOWED", "该资源不支持此操作。")
            self._static(handler, parsed.path, head_only=method == "HEAD")
        except _CLIENT_DISCONNECT_ERRORS:
            return
        except HttpError as error:
            try:
                if write_method and not body_read_started:
                    self._discard_rejected_small_body(handler)
                self.send_error(
                    handler,
                    error.status,
                    error.code,
                    error.public_message,
                    head_only=method == "HEAD",
                )
            except _CLIENT_DISCONNECT_ERRORS:
                return
        except Exception as error:
            problem = translate_exception(error)
            payload = {"code": problem.code, "message": problem.message}
            if problem.details:
                if "diagnosticId" in problem.details:
                    diagnostic_id = problem.details["diagnosticId"]
                    self.runtime.record_diagnostic(diagnostic_id)
                    payload["diagnosticId"] = diagnostic_id
                else:
                    payload["details"] = problem.details
            try:
                self.send_json(
                    handler,
                    problem.status,
                    {"error": payload},
                    head_only=method == "HEAD",
                )
            except _CLIENT_DISCONNECT_ERRORS:
                return

    def _discard_rejected_small_body(self, handler) -> None:
        """Bound a small rejected upload before close, so its bytes cannot reset the reply."""
        handler.close_connection = True
        if "Transfer-Encoding" in handler.headers:
            return
        lengths = handler.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            return
        try:
            remaining = int(lengths[0])
        except (TypeError, ValueError):
            return
        if not 0 < remaining <= min(_REJECTED_BODY_MAX_BYTES, self.runtime.max_json_bytes):
            return
        deadline = time.monotonic() + _REJECTED_BODY_DRAIN_SECONDS
        previous_timeout = handler.connection.gettimeout()
        try:
            while remaining:
                seconds_left = deadline - time.monotonic()
                if seconds_left <= 0:
                    break
                handler.connection.settimeout(seconds_left)
                chunk = handler.rfile.read1(min(remaining, 4096))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, TimeoutError):
            pass
        finally:
            handler.connection.settimeout(previous_timeout)

    def _bootstrap(self, handler, query: dict[str, list[str]]) -> None:
        key = query.get("key", [None])[0]
        session = self.runtime.security.exchange(key)
        handler.send_response(303)
        handler.send_header("Location", "/")
        handler.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={session}; Max-Age={self.runtime.security.session_ttl_seconds}; "
            "Path=/; HttpOnly; SameSite=Strict",
        )
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    def _service_status(self, handler) -> None:
        with CatalogDatabase.open(self.runtime.db_path) as db:
            task_count = db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            queued_count = db.connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')"
            ).fetchone()[0]
        self.send_json(
            handler,
            200,
            {"ready": True, "taskCount": task_count, "activeJobCount": queued_count},
        )

    @staticmethod
    def _rough_media_parts(path: str) -> tuple[str, str, str] | None:
        parts = path.split("/")
        if (
            len(parts) != 7
            or parts[:3] != ["", "api", "tasks"]
            or not parts[3]
            or parts[4] != "rough-media"
            or not parts[5]
            or parts[6] not in {"thumbnail", "preview"}
        ):
            return None
        return tuple(unquote(value) for value in (parts[3], parts[5], parts[6]))

    def _rough_media(
        self,
        handler,
        task_id: str,
        asset_id: str,
        purpose: str,
        *,
        head_only: bool,
    ) -> None:
        try:
            with CatalogDatabase.open(self.runtime.db_path) as db:
                media = resolve_rough_media(db, task_id, asset_id, purpose)
        except RoughMediaError as error:
            raise HttpError(
                error.status,
                error.code,
                error.public_message,
            ) from error
        try:
            rough_media_content_type(media)
        except RoughMediaError as error:
            raise HttpError(error.status, error.code, error.public_message) from error
        self._send_media_file(handler, media.path, head_only=head_only)

    @staticmethod
    def _is_review_media_path(path: str) -> bool:
        parts = path.split("/")
        return (
            len(parts) == 6
            and parts[:3] == ["", "api", "tasks"]
            and bool(parts[3])
            and parts[4:] == ["review-media", ""]
        ) or (
            len(parts) == 5
            and parts[:3] == ["", "api", "tasks"]
            and bool(parts[3])
            and parts[4] == "review-media"
        ) or (
            len(parts) == 6
            and parts[:3] == ["", "api", "tasks"]
            and bool(parts[3])
            and parts[4] == "review-media"
            and bool(parts[5])
        )

    def _review_media(
        self,
        handler,
        task_id: str,
        *,
        candidate_id: str | None = None,
        head_only: bool,
    ) -> None:
        with CatalogDatabase.open(self.runtime.db_path) as db:
            candidate = latest_review_candidate(db, task_id)
            if candidate_id is not None:
                try:
                    media_path = resolve_candidate_media(db, task_id, candidate_id)
                except KeyError as error:
                    raise HttpError(
                        404, "REVIEW_MEDIA_NOT_FOUND", "候选版本不存在。"
                    ) from error
                except RuntimeError as error:
                    raise HttpError(
                        409, "CANDIDATE_VIDEO_CHANGED", "候选成片内容已变化。"
                    ) from error
            else:
                media_path = None
        if candidate is None and media_path is None:
            raise HttpError(404, "REVIEW_MEDIA_NOT_FOUND", "当前任务没有可审候选成片。")
        if media_path is None:
            media_path = Path(candidate["output_path"]).resolve()
        if not media_path.is_file():
            raise HttpError(404, "REVIEW_MEDIA_NOT_FOUND", "当前候选成片文件不可用。")
        if candidate_id is None:
            expected_signature = candidate.get("qa_output_signature")
            if (
                not isinstance(expected_signature, dict)
                or file_content_signature(media_path) != expected_signature
            ):
                raise HttpError(
                    409,
                    "CANDIDATE_VIDEO_CHANGED",
                    "候选成片内容已变化，请重新渲染和质检。",
                )

        self._send_media_file(handler, media_path, head_only=head_only)

    @staticmethod
    def _packaging_preview_media_parts(path: str) -> tuple[str, str] | None:
        parts = path.split("/")
        if (
            len(parts) != 7
            or parts[:3] != ["", "api", "tasks"]
            or not parts[3]
            or parts[4] != "packaging-previews"
            or not parts[5]
            or parts[6] != "media"
        ):
            return None
        return unquote(parts[3]), unquote(parts[5])

    def _packaging_preview_media(
        self,
        handler,
        task_id: str,
        preview_id: str,
        *,
        head_only: bool,
    ) -> None:
        with CatalogDatabase.open(self.runtime.db_path) as db:
            preview = get_preview_artifact(db, preview_id)
            if preview is None or preview["task_id"] != task_id:
                raise HttpError(404, "PACKAGING_PREVIEW_NOT_FOUND", "包装预览不存在。")
            media_path = resolve_packaging_preview_media(
                db, preview_id, self.runtime.project_root
            )
        if media_path.suffix.lower() != ".mp4":
            raise HttpError(404, "PACKAGING_PREVIEW_NOT_FOUND", "包装预览不存在。")
        self._send_media_file(handler, media_path, head_only=head_only)

    @staticmethod
    def _is_packaging_asset_preview_path(path: str) -> bool:
        parts = path.split("/")
        return (
            len(parts) == 6
            and parts[:3] == ["", "api", "packaging-assets"]
            and bool(parts[3])
            and bool(parts[4])
            and parts[5] == "preview"
        )

    def _packaging_preview(
        self, handler, category: str, asset_ref: str, *, head_only: bool
    ) -> None:
        manifest_category = "title_card" if category == "brand" else category
        media_path = resolve_manifest_asset_path(manifest_category, asset_ref)
        allowed_suffixes = {
            ".mp3", ".wav", ".ogg", ".m4a", ".aac",
            ".png", ".jpg", ".jpeg", ".webp", ".gif",
            ".mp4", ".mov", ".webm",
        }
        if (
            media_path is None
            or media_path.suffix.lower() not in allowed_suffixes
            or not media_path.is_file()
        ):
            raise HttpError(404, "PACKAGING_PREVIEW_NOT_FOUND", "该包装素材暂无可用预览。")
        self._send_media_file(handler, media_path, head_only=head_only)

    @staticmethod
    def _is_voice_preview_path(path: str) -> bool:
        parts = path.split("/")
        return (
            len(parts) == 5
            and parts[:3] == ["", "api", "voice-previews"]
            and re.fullmatch(r"[0-9a-f]{64}", parts[3]) is not None
            and re.fullmatch(r"[0-9a-f]{64}\.mp3", parts[4]) is not None
        )

    def _voice_preview(
        self,
        handler,
        profile_hash: str,
        voice_hash: str,
        *,
        head_only: bool,
    ) -> None:
        root = (
            Path(self.runtime.project_root)
            / "runtime"
            / "workbench"
            / "voice-previews"
        ).resolve()
        media_path = (root / profile_hash / f"{voice_hash}.mp3").resolve()
        if (
            root not in media_path.parents
            or not media_path.is_file()
        ):
            raise HttpError(
                404,
                "TTS_VOICE_PREVIEW_NOT_FOUND",
                "该音色试听文件不存在，请重新试听。",
            )
        self._send_media_file(handler, media_path, head_only=head_only)

    def _send_media_file(self, handler, media_path: Path, *, head_only: bool) -> None:
        size = media_path.stat().st_size
        try:
            start, end, partial = self._byte_range(handler.headers.get("Range"), size)
        except HttpError as error:
            handler.send_response(error.status)
            handler.send_header("Content-Range", f"bytes */{size}")
            handler.send_header("Content-Length", "0")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.end_headers()
            return
        length = 0 if size == 0 else end - start + 1
        handler.send_response(206 if partial else 200)
        handler.send_header(
            "Content-Type",
            mimetypes.guess_type(media_path.name)[0] or "application/octet-stream",
        )
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Content-Length", str(length))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Content-Disposition", "inline")
        if partial:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        handler.end_headers()
        if head_only or length == 0:
            return
        with media_path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = source.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)

    @staticmethod
    def _byte_range(raw_range: str | None, size: int) -> tuple[int, int, bool]:
        if raw_range is None:
            return 0, max(0, size - 1), False
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", raw_range.strip())
        if match is None or size <= 0:
            raise HttpError(416, "RANGE_NOT_SATISFIABLE", "视频播放范围无效。")
        raw_start, raw_end = match.groups()
        if not raw_start and not raw_end:
            raise HttpError(416, "RANGE_NOT_SATISFIABLE", "视频播放范围无效。")
        if raw_start:
            start = int(raw_start)
            end = int(raw_end) if raw_end else size - 1
        else:
            suffix = int(raw_end)
            if suffix <= 0:
                raise HttpError(416, "RANGE_NOT_SATISFIABLE", "视频播放范围无效。")
            start = max(0, size - suffix)
            end = size - 1
        if start >= size or end < start:
            raise HttpError(416, "RANGE_NOT_SATISFIABLE", "视频播放范围无效。")
        return start, min(end, size - 1), True

    def _picker(self, handler, kind: str) -> None:
        pickers = {
            "video": self.runtime.picker.pick_video,
            "directory": self.runtime.picker.pick_directory,
            "image": self.runtime.picker.pick_image,
        }
        value = pickers[kind]()
        safe_value = validate_picker_result(value, kind=kind)
        self.send_json(handler, 200, {"path": safe_value})

    def _static(self, handler, request_path: str, *, head_only: bool) -> None:
        candidate = resolve_static_file(self.runtime.static_root, request_path)
        if not candidate.is_file():
            raise HttpError(404, "NOT_FOUND", "未找到该资源。")
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        size = candidate.stat().st_size
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(size))
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.end_headers()
        if not head_only:
            with candidate.open("rb") as source:
                while chunk := source.read(64 * 1024):
                    handler.wfile.write(chunk)

    @staticmethod
    def send_json(handler, status: int, payload: dict, *, head_only: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        if not head_only:
            handler.wfile.write(body)

    @classmethod
    def send_error(
        cls,
        handler,
        status: int,
        code: str,
        message: str,
        *,
        head_only: bool = False,
    ) -> None:
        cls.send_json(
            handler,
            status,
            {"error": {"code": code, "message": message}},
            head_only=head_only,
        )
