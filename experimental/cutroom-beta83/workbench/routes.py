"""Minimal Task 9 routes; business APIs are added separately in Task 10."""

from __future__ import annotations

import json
import hashlib
import mimetypes
import logging
import os
import uuid
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, unquote, urlsplit

from media_catalog.path_utils import filesystem_path
from media_catalog.catalog_exchange import export_catalog_workbook, preview_catalog_import
from media_catalog.candidate_versions import resolve_candidate_media
from media_catalog.ad_voice_preview import current_ad_voice
from media_catalog.packaging_plan import resolve_manifest_asset_path
from media_catalog.packaging_preview import (
    get_preview_artifact,
    resolve_packaging_preview_media,
)
from media_catalog.publish import registered_delivery_cover, registered_delivery_directory
from media_catalog.rough_media import (
    RoughMediaError,
    resolve_catalog_media,
    resolve_rough_media,
    rough_media_content_type,
)
from media_catalog.snapshots import file_content_signature, latest_review_candidate
from media_catalog.tasks import get_task

from .native_picker import picker_request_id
from .api import WorkbenchAPI, translate_exception
from .security import (
    HttpError,
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
            if method == "GET" and parsed.path == "/ui-version.json":
                from .ui_version import ui_version
                self.send_json(handler, 200, {"version": ui_version(self.runtime.static_root)})
                return
            if method == "GET" and parsed.path == "/auth/local-ready":
                self._local_ready(handler, parse_qs(parsed.query, keep_blank_values=True))
                return
            if method == "GET" and parsed.path == "/auth/bootstrap":
                self._bootstrap(handler, parse_qs(parsed.query))
                return
            if write_method and self.runtime.maintenance_read_only:
                self.runtime.security.require_session(handler.headers)
                raise HttpError(423, 'MAINTENANCE_READ_ONLY', '当前为维护只读回看，不能保存、批准或运行作业。')
            if method == "POST" and parsed.path == "/api/catalog-exchange/import-preview":
                length = self.runtime.security.validate_xlsx_write(
                    handler.headers, expected_origin=self.runtime.origin,
                )
                body_read_started = True
                workbook_bytes = handler.rfile.read(length)
                if len(workbook_bytes) != length:
                    raise HttpError(400, "BODY_INCOMPLETE", "Excel 文件上传不完整。")
                with self.runtime.open_database() as db:
                    result = preview_catalog_import(db, workbook_bytes)
                self.send_json(handler, 200, result)
                return
            if write_method:
                length = self.runtime.security.validate_write(
                    handler.headers,
                    expected_origin=self.runtime.origin,
                    max_json_bytes=self.runtime.max_json_bytes,
                )
                body_read_started = True
                handler.workbench_json = read_json(handler.rfile, length)
            if method == "GET" and parsed.path == "/api/catalog-exchange/export":
                self.runtime.security.require_session(handler.headers)
                query = parse_qs(parsed.query)
                for values in query.values():
                    if len(values) != 1:
                        raise HttpError(400, "ASSET_INDEX_QUERY_INVALID", "台账筛选条件无效。")
                filter_names = {"query": "query", "libraryId": "library_id",
                                "purpose": "purpose",
                                "mediaKind": "media_kind", "availability": "availability",
                                "authorization": "authorization", "analysisStatus": "analysis_status"}
                filters = {target: query[source][0] for source, target in filter_names.items()
                           if source in query and query[source][0]}
                with self.runtime.open_database() as db:
                    data = export_catalog_workbook(db, task_id=query.get("taskId", [None])[0],
                                                   filters=filters)
                handler.send_response(200)
                handler.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                handler.send_header("Content-Disposition", "attachment; filename=\"CUTROOM-catalog.xlsx\"")
                handler.send_header("Content-Length", str(len(data)))
                handler.send_header("Cache-Control", "no-store")
                handler.send_header("X-Content-Type-Options", "nosniff")
                handler.end_headers()
                handler.wfile.write(data)
                return
            if method == "GET" and parsed.path == "/api/service/status":
                self.runtime.security.require_session(handler.headers)
                self._service_status(handler)
                return
            catalog_media = self._catalog_media_asset_id(parsed.path)
            if method in {"GET", "HEAD"} and catalog_media is not None:
                self.runtime.security.require_session(handler.headers)
                self._catalog_media(handler, catalog_media, head_only=method == "HEAD")
                return
            digital_source = self._digital_source_task_id(parsed.path)
            if method in {"GET", "HEAD"} and digital_source is not None:
                self.runtime.security.require_session(handler.headers)
                self._digital_source_media(
                    handler, digital_source, head_only=method == "HEAD"
                )
                return
            voice_parts=parsed.path.strip('/').split('/')
            if (method in {'GET','HEAD'} and len(voice_parts)==7 and voice_parts[:2]==['api','tasks']
                    and voice_parts[3:5]==['voice-editing','previews'] and voice_parts[6]=='media'):
                self.runtime.security.require_session(handler.headers)
                from media_catalog.voice_bundle_service import resolve_voice_preview_media,VoiceDraftError
                try:
                    with self.runtime.open_database() as db:
                        voice=resolve_voice_preview_media(db,self.runtime.project_root,unquote(voice_parts[2]),unquote(voice_parts[5]))
                except (VoiceDraftError,ValueError,OSError) as error:
                    raise HttpError(404,'VOICE_PREVIEW_NOT_FOUND','本任务的新版声音暂不可播放。') from error
                self._send_media_file(handler,voice,head_only=method=='HEAD')
                return
            script_voice = self._script_voice_task_id(parsed.path)
            if method in {"GET", "HEAD"} and script_voice is not None:
                self.runtime.security.require_session(handler.headers)
                self._script_voice_media(handler, script_voice, head_only=method == "HEAD")
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
            if method == "GET" and parsed.path == "/api/diagnostics/export":
                self.runtime.security.require_session(handler.headers)
                data = self.runtime.diagnostics.export_zip()
                handler.send_response(200)
                handler.send_header("Content-Type", "application/zip")
                handler.send_header("Content-Disposition", 'attachment; filename="剪辑工作台-errors.zip"')
                handler.send_header("Content-Length", str(len(data)))
                handler.send_header("Cache-Control", "no-store")
                handler.send_header("X-Content-Type-Options", "nosniff")
                handler.end_headers()
                handler.wfile.write(data)
                return
            if method == "POST" and parsed.path == "/api/diagnostics/browser":
                ident = self.runtime.diagnostics.record_browser(handler.workbench_json)
                self.send_json(handler, 200, {"recorded": ident is not None, "diagnosticId": ident})
                return
            if method == "GET" and parsed.path.startswith("/api/diagnostics/"):
                self.runtime.security.require_session(handler.headers)
                diagnostic_id = parsed.path.rsplit("/", 1)[-1]
                if not self.runtime.has_diagnostic(diagnostic_id):
                    raise HttpError(404, "NOT_FOUND", "诊断编号不存在。")
                self.send_json(handler, 200, {"diagnosticId": diagnostic_id})
                return
            if method == "POST" and parsed.path in {
                "/api/picker/video", "/api/picker/directory", "/api/picker/image", "/api/picker/packaging"
            }:
                self._picker(handler, parsed.path.rsplit("/", 1)[-1])
                return
            picked_image = re.fullmatch(r"/api/picker/image-preview/([a-f0-9]{32})", parsed.path)
            if method in {"GET", "HEAD"} and picked_image is not None:
                self.runtime.security.require_session(handler.headers)
                try:
                    image_path = self.runtime.resolve_picked_image(picked_image.group(1))
                except KeyError as error:
                    raise HttpError(404, "PICKED_IMAGE_NOT_FOUND", "图片预览已过期，请重新选择。") from error
                except RuntimeError as error:
                    raise HttpError(409, "PICKED_IMAGE_CHANGED", str(error)) from error
                self._send_media_file(handler, image_path, head_only=method == "HEAD")
                return
            cover_design = re.fullmatch(r"/api/tasks/([^/]+)/cover-design", parsed.path)
            if method in {"GET", "POST"} and cover_design is not None:
                self.runtime.security.require_session(handler.headers)
                from media_catalog.cover_design import design_directory, save_cover_design, load_cover_design
                task_id = unquote(cover_design.group(1))
                body = handler.workbench_json if method == "POST" else {}
                candidate_id = body.get("candidateId") if method == "POST" else parse_qs(parsed.query).get("candidateId", [""])[0]
                try:
                    with self.runtime.open_database() as db:
                        directory = design_directory(db, self.runtime.project_root, task_id, candidate_id)
                    if method == "POST":
                        source = {"kind": "frame", "time": max(0, min(float(body.get("frameTime", 0)), 86400))}
                        token = body.get("sourceToken")
                        if token:
                            source_path = self.runtime.resolve_picked_image(token)
                            from media_catalog.publish import validate_external_cover
                            _, signature = validate_external_cover(source_path)
                            source = {"kind": "image", "path": str(source_path), "signature": signature["sha256"]}
                        result = save_cover_design(directory, body.get("imageBase64"), body.get("settings"), source)
                    else:
                        result = load_cover_design(directory)
                    if result is None:
                        self.send_json(handler, 200, {"saved": False})
                        return
                    record, image_path = result
                    preview_token, signature = self.runtime.register_picked_image(str(image_path))
                    payload = {"saved": True, "path": str(image_path), "signature": signature,
                               "previewUrl": f"/api/picker/image-preview/{preview_token}",
                               "settings": record["settings"], "source": {"kind": record["source"].get("kind"), "time": record["source"].get("time", 0)}}
                    if record["source"].get("kind") == "image":
                        try:
                            source_token, source_sig = self.runtime.register_picked_image(record["source"]["path"])
                            if source_sig == record["source"].get("signature"):
                                payload["source"].update({"token": source_token, "previewUrl": f"/api/picker/image-preview/{source_token}"})
                        except (ValueError, OSError):
                            pass
                    self.send_json(handler, 200, payload)
                except (ValueError, KeyError, RuntimeError) as error:
                    raise HttpError(409, "COVER_DESIGN_UNAVAILABLE", str(error)) from error
                return
            package_cover = re.fullmatch(
                r"/api/tasks/([^/]+)/publish/([^/]+)/cover-preview", parsed.path,
            )
            if method in {"GET", "HEAD"} and package_cover is not None:
                self.runtime.security.require_session(handler.headers)
                task_id, record_id = (unquote(part) for part in package_cover.groups())
                with self.runtime.open_database() as db:
                    try:
                        cover = registered_delivery_cover(db, task_id, record_id)
                    except KeyError as error:
                        raise HttpError(404, "DELIVERY_PACKAGE_NOT_FOUND", "未找到本任务交付包。") from error
                    except RuntimeError as error:
                        code, _, message = str(error).partition(":")
                        raise HttpError(409, code, message.strip()) from error
                self._send_media_file(handler, cover, head_only=method == "HEAD")
                return
            open_package = re.fullmatch(
                r"/api/tasks/([^/]+)/publish/([^/]+)/open-directory", parsed.path,
            )
            if method == "POST" and open_package is not None:
                task_id, record_id = (unquote(part) for part in open_package.groups())
                with self.runtime.open_database() as db:
                    try:
                        directory = registered_delivery_directory(db, task_id, record_id)
                    except KeyError as error:
                        raise HttpError(404, "DELIVERY_PACKAGE_NOT_FOUND", "未找到本任务交付包。") from error
                    except RuntimeError as error:
                        code, _, message = str(error).partition(":")
                        raise HttpError(409, code, message.strip()) from error
                try:
                    os.startfile(str(directory))
                except OSError as error:
                    raise HttpError(409, "DELIVERY_DIRECTORY_UNAVAILABLE", "交付目录当前无法打开。") from error
                self.send_json(handler, 200, {"requested": True})
                return
            if parsed.path.startswith("/api/settings/storage/"):
                self.runtime.security.require_session(handler.headers)
                from media_catalog.storage_safety import dispatch_storage
                try:
                    with self.runtime.open_database() as db:
                        payload = dispatch_storage(db, self.runtime.project_root,
                            self.runtime.cache_cleanup_plans, method, parsed.path,
                            getattr(handler, "workbench_json", None) or {})
                    if payload is not None:
                        self.send_json(handler, 200, payload)
                        return
                except (ValueError, OSError) as error:
                    code, _, message = str(error).partition(":")
                    if not code.startswith(("STORAGE_", "CACHE_")):
                        code, message = "STORAGE_OPERATION_FAILED", "文件无法读取或写入，操作已停止。请保留原文件，检查磁盘空间和目录权限后重试。"
                    raise HttpError(409, code, message.strip()) from error
            if parsed.path.startswith("/api/"):
                self.runtime.security.require_session(handler.headers)
                with self.runtime.open_database() as db:
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
            diagnostic_id = self.runtime.diagnostics.record("api", error=error, code=error.code,
                method=method, path=parsed.path, status=error.status)
            try:
                if write_method and not body_read_started:
                    self._discard_rejected_small_body(handler)
                self.send_error(
                    handler,
                    error.status,
                    error.code,
                    error.public_message,
                    diagnostic_id=diagnostic_id,
                    head_only=method == "HEAD",
                )
            except _CLIENT_DISCONNECT_ERRORS:
                return
        except Exception as error:
            problem = translate_exception(error)
            payload = {"code": problem.code, "message": problem.message}
            diagnostic_id = self.runtime.diagnostics.record("api", error=error, code=problem.code,
                method=method, path=parsed.path, status=problem.status,
                diagnostic_id=(problem.details or {}).get("diagnosticId"))
            if diagnostic_id:
                payload["diagnosticId"] = diagnostic_id
            if problem.details and "diagnosticId" not in problem.details:
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

    def _local_ready(self, handler, query: dict[str, list[str]]) -> None:
        if (handler.headers.get("Host") != self.runtime.origin.removeprefix("http://")
                or handler.headers.get("Origin") not in (None, self.runtime.origin)):
            raise HttpError(403, "ORIGIN_REJECTED", "请求来源不是当前本机工作台。")
        values = query.get("nonce", [])
        if (set(query) != {"nonce"} or len(values) != 1
                or re.fullmatch(r"[0-9a-f]{64}", values[0]) is None):
            raise HttpError(400, "LOCAL_READY_INVALID", "本机启动检查无效。")
        from .local_entrypoint import startup_reference
        port = int(self.runtime.origin.rsplit(":", 1)[1])
        reference = startup_reference(self.runtime.project_root, self.runtime.db_path, port,
                                      maintenance_read_only=self.runtime.maintenance_read_only)
        self.send_json(handler, 200, {"proof": self.runtime.security.local_launch_proof(reference, values[0])})

    def _bootstrap(self, handler, query: dict[str, list[str]]) -> None:
        key = query.get("key", [None])[0]
        session = self.runtime.security.exchange(key)
        handler.send_response(303)
        handler.send_header("Location", "/")
        handler.send_header(
            "Set-Cookie",
            f"{self.runtime.security.cookie_name}={session}; Max-Age={self.runtime.security.session_ttl_seconds}; "
            "Path=/; HttpOnly; SameSite=Strict",
        )
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    def _service_status(self, handler) -> None:
        with self.runtime.open_database() as db:
            task_count = db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            queued_count = db.connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')"
            ).fetchone()[0]
        self.send_json(
            handler,
            200,
            {"ready": True, "taskCount": task_count, "activeJobCount": queued_count,
             "maintenanceReadOnly": self.runtime.maintenance_read_only},
        )

    @staticmethod
    def _catalog_media_asset_id(path: str) -> str | None:
        parts = path.split("/")
        if len(parts) != 5 or parts[:3] != ["", "api", "assets"] or not parts[3] or parts[4] != "preview":
            return None
        return unquote(parts[3])

    def _catalog_media(self, handler, asset_id: str, *, head_only: bool) -> None:
        try:
            with self.runtime.open_database() as db:
                media = resolve_catalog_media(db, asset_id)
            rough_media_content_type(media)
        except RoughMediaError as error:
            raise HttpError(error.status, error.code, error.public_message) from error
        self._send_media_file(handler, media.path, head_only=head_only)

    @staticmethod
    def _digital_source_task_id(path: str) -> str | None:
        parts = path.split("/")
        if (len(parts) == 7 and parts[:3] == ["", "api", "tasks"]
                and parts[3] and parts[4:] == ["digital-source", "media", ""]):
            return unquote(parts[3])
        if (len(parts) == 6 and parts[:3] == ["", "api", "tasks"]
                and parts[3] and parts[4:] == ["digital-source", "media"]):
            return unquote(parts[3])
        return None

    def _digital_source_media(self, handler, task_id: str, *, head_only: bool) -> None:
        with self.runtime.open_database() as db:
            task = get_task(db, task_id)
        if task is None or task.get("task_type") != "digital_human":
            raise HttpError(404, "DIGITAL_SOURCE_NOT_FOUND", "数字人底片不存在。")
        source_record = (task.get("brief") or {}).get("digital_human_source") or {}
        source = source_record.get("path")
        if not isinstance(source, str) or not source:
            raise HttpError(404, "DIGITAL_SOURCE_NOT_FOUND", "数字人底片不存在。")
        media_path = Path(source).resolve()
        if media_path.suffix.lower() not in {".mp4", ".mov", ".m4v"} or not media_path.is_file():
            raise HttpError(404, "DIGITAL_SOURCE_NOT_FOUND", "数字人底片不存在。")
        self._send_media_file(handler, media_path, head_only=head_only)

    @staticmethod
    def _script_voice_task_id(path: str) -> str | None:
        parts = path.split("/")
        if (len(parts) == 6 and parts[:3] == ["", "api", "tasks"]
                and parts[3] and parts[4:] == ["script-voice", "media"]):
            return unquote(parts[3])
        return None

    def _script_voice_media(self, handler, task_id: str, *, head_only: bool) -> None:
        with self.runtime.open_database() as db:
            task = get_task(db, task_id)
            voice = None if task is None else current_ad_voice(db, self.runtime.project_root, task)
        if voice is None:
            raise HttpError(404, "SCRIPT_VOICE_NOT_FOUND", "当前文案没有可试听的完整配音。")
        self._send_media_file(handler, voice, head_only=head_only)

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
            with self.runtime.open_database() as db:
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
        with self.runtime.open_database() as db:
            candidate = latest_review_candidate(db, task_id)
            if candidate_id is not None:
                try:
                    media_path = resolve_candidate_media(db, task_id, candidate_id)
                except KeyError as error:
                    raise HttpError(
                        404, "REVIEW_MEDIA_NOT_FOUND", "候选版本不存在。"
                    ) from error
                except RuntimeError as error:
                    if str(error).startswith("CANDIDATE_MEDIA_MISSING:"):
                        raise HttpError(
                            404, "REVIEW_MEDIA_NOT_FOUND", "本版成片文件不存在或无法读取。"
                        ) from error
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
        with self.runtime.open_database() as db:
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
        manifest_category = "title_card" if category in {"brand", "info_card"} else category
        with self.runtime.open_database() as db:
            try:
                media_path = resolve_manifest_asset_path(manifest_category, asset_ref, db)
            except RuntimeError:
                media_path = None
        allowed_suffixes = {
            ".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".opus",
            ".png", ".jpg", ".jpeg", ".webp", ".gif",
            ".mp4", ".mov", ".webm", ".ttf", ".otf", ".woff", ".woff2",
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
        preview_url = f"/api/voice-previews/{profile_hash}/{voice_hash}.mp3"
        with self.runtime.open_database() as db:
            receipt = db.connection.execute(
                "SELECT output_sha256 FROM voice_preview_requests WHERE preview_url=? AND state='succeeded'",
                (preview_url,),
            ).fetchone()
        if receipt is None:
            raise HttpError(404, "TTS_VOICE_PREVIEW_NOT_FOUND", "该试听已被替代或没有有效回执，请重新试听。")
        if hashlib.sha256(media_path.read_bytes()).hexdigest() != receipt["output_sha256"]:
            raise HttpError(409, "VOICE_PREVIEW_CACHE_CHANGED", "试听文件已变化，请回到试听入口核对并恢复。")
        self._send_media_file(handler, media_path, head_only=head_only)

    def _send_media_file(self, handler, media_path: Path, *, head_only: bool) -> None:
        read_path = filesystem_path(media_path)
        size = read_path.stat().st_size
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
        with read_path.open("rb") as source:
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
            "packaging": self.runtime.picker.pick_packaging,
        }
        body = getattr(handler, "workbench_json", None) or {}
        request_id = body.get("requestId", uuid.uuid4().hex)
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id):
            raise HttpError(400, "PICKER_REQUEST_INVALID", "文件选择请求编号无效，请重新选择。")
        started = time.monotonic()
        token = picker_request_id.set(request_id)
        log = logging.getLogger(__name__)
        log.info("picker request=%s kind=%s event=request_received", request_id, kind)
        try:
            value = pickers[kind]()
            safe_value = validate_picker_result(value, kind=kind)
            payload = {"path": safe_value}
            if kind == "image" and safe_value:
                try:
                    preview_token, image_signature = self.runtime.register_picked_image(safe_value)
                except ValueError as error:
                    raise HttpError(400, "EXTERNAL_COVER_INVALID",
                                    "请选择内容有效的 JPG、PNG 或 WebP 图片。") from error
                payload["previewUrl"] = f"/api/picker/image-preview/{preview_token}"
                payload["signature"] = image_signature
            if "requestId" in body:
                payload["requestId"] = request_id
            self.send_json(handler, 200, payload)
        finally:
            picker_request_id.reset(token)
            log.info("picker request=%s kind=%s event=request_finished elapsed_ms=%.3f",
                     request_id, kind, (time.monotonic() - started) * 1000)

    def _static(self, handler, request_path: str, *, head_only: bool) -> None:
        candidate = resolve_static_file(self.runtime.static_root, request_path)
        if not candidate.is_file():
            raise HttpError(404, "NOT_FOUND", "未找到该资源。")
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        body = None
        if candidate.name == "index.html":
            from .ui_version import ui_version
            body = candidate.read_bytes().replace(b"__CUTROOM_UI_VERSION__", ui_version(self.runtime.static_root).encode())
        size = len(body) if body is not None else candidate.stat().st_size
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(size))
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.end_headers()
        if not head_only:
            if body is not None:
                handler.wfile.write(body)
                return
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
        diagnostic_id: str | None = None,
        head_only: bool = False,
    ) -> None:
        cls.send_json(
            handler,
            status,
            {"error": {"code": code, "message": message, **({"diagnosticId": diagnostic_id} if diagnostic_id else {})}},
            head_only=head_only,
        )
