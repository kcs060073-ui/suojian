"""Real, bounded availability probes for task-selectable model profiles."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from .credentials import decode_provider_credentials, read_provider_secret
from .model_profiles import (
    _load,
    _profile_row,
    get_provider_account,
    get_model_profile,
    mark_profile_check,
)
from .model_runtime import (
    TaskModelConfigurationError,
    doubao_tts_v3_endpoint,
    provider_access_token,
)
from .provider_endpoints import provider_endpoint
from .provider_presets import provider_preset
from .tts_voice_catalog import mark_voice_test_result, get_provider_voice


class ModelProfileProbeError(RuntimeError):
    def __init__(self, message: str, code: str = "MODEL_PROBE_FAILED", *, receipt: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.receipt = receipt


def _protocol_error() -> ModelProfileProbeError:
    return ModelProfileProbeError("供应商响应格式不兼容", "MODEL_PROTOCOL_MISMATCH")


def _response_object(data) -> dict:
    decoded = json.loads(data)
    if not isinstance(decoded, dict):
        raise _protocol_error()
    return decoded


def _tts_rejection(message: str) -> tuple[str, str]:
    lowered = message.lower()
    # Account/resource failures take precedence, even when a voice is mentioned.
    if "resource not granted" in lowered:
        return "MODEL_RESOURCE_NOT_GRANTED", "当前凭据未开通所选配音服务版本，请在火山控制台开通后重试"
    if any(word in lowered for word in ("api key", "unauthorized", "authentication", "access token", "permission denied")):
        return "MODEL_AUTH_FAILED", "API Key 无效或没有访问权限"
    if re.search(r"(?:voice|speaker|voice_type|音色).{0,80}(?:not supported|unsupported|not found|invalid|not exist|不存在|不支持|无效)", lowered) or re.search(
        r"(?:unsupported|invalid|unknown).{0,20}(?:voice|speaker|音色)", lowered
    ):
        return "TTS_VOICE_TEST_FAILED", "供应商拒绝了所选音色，请重新选择音色或刷新目录"
    return "MODEL_PROVIDER_REJECTED", "配音请求被供应商拒绝，请检查服务状态后重试"


def _tts_status(decoded: dict, success_codes: set[int]) -> None:
    code = decoded.get("code")
    if type(code) is not int:
        raise _protocol_error()
    if code not in success_codes:
        message = decoded.get("message", "")
        if not isinstance(message, str):
            raise _protocol_error()
        error_code, safe_message = _tts_rejection(message)
        raise ModelProfileProbeError(safe_message, error_code)


def _provider_limit_details(detail: str, *, provider_host: str | None = None) -> tuple[str, str]:
    """Classify explicit provider evidence; never echo body text or assume 429 means debt."""
    try:
        decoded = json.loads(detail)
    except (ValueError, TypeError):
        decoded = None
    markers = set()
    provider_code = None
    message = detail
    if isinstance(decoded, dict):
        error = decoded.get("error", decoded)
        if isinstance(error, dict):
            provider_code = str(error.get("code", ""))
            markers = {str(error.get(key, "")).lower() for key in ("code", "type")}
            message = str(error.get("message", "")).lower()
        else:
            message = error.lower() if isinstance(error, str) else ""
    # BigModel documents 1113 as account debt. Numeric codes are provider-specific:
    # https://docs.bigmodel.cn/cn/api/api-code
    if provider_host == "open.bigmodel.cn" and provider_code == "1113":
        return "MODEL_QUOTA_EXHAUSTED", "智谱账户已欠费（HTTP 429 / 1113）；请恢复账户余额或可用额度后再手动检测"
    if markers & {"insufficient_quota", "insufficient_balance", "credit_balance_too_low", "billing_hard_limit_reached"} or any(
        value in message for value in ("insufficient quota", "insufficient balance", "credit balance is too low", "余额不足", "额度不足", "额度已用尽", "账户欠费", "已欠费")
    ):
        return "MODEL_QUOTA_EXHAUSTED", "供应商提示余额或可用额度不足（HTTP 429）；请检查账号余额、令牌额度或渠道额度后再手动检测"
    if markers & {"concurrency_limit_exceeded", "concurrent_requests_limit_exceeded"} or any(
        value in message for value in ("concurrency limit", "too many concurrent", "并发限制", "并发超限", "并发数超出")
    ):
        return "MODEL_CONCURRENCY_LIMITED", "供应商提示并发请求超过限制（HTTP 429）；请等待正在执行的请求结束或检查渠道并发额度后再手动检测"
    if markers & {"rate_limit_exceeded", "rate_limit_error", "rpm_limit_exceeded", "tpm_limit_exceeded"} or any(
        value in message for value in ("rate limit", "requests per minute", "tokens per minute", "请求频率", "速率限制")
    ):
        return "MODEL_RATE_LIMITED", "供应商提示请求频率或令牌速率超过限制（HTTP 429）；请等待限制窗口恢复或检查渠道速率额度后再手动检测"
    return "MODEL_PROVIDER_LIMITED", "供应商限制了本次请求（HTTP 429），原因未明确；请核对服务端的余额/额度、速率与并发限制，确认后再手动检测"


def _failure_details(error: BaseException) -> tuple[str, str]:
    if isinstance(error, HTTPError):
        try:
            provider_detail = error.read(64 * 1024).decode(
                "utf-8", errors="replace"
            ).lower()
        except OSError:
            provider_detail = ""
        if "resource not granted" in provider_detail:
            return (
                "MODEL_RESOURCE_NOT_GRANTED",
                "当前凭据未开通所选配音服务版本，请在火山控制台开通后重试",
            )
        if error.code in {401, 403}:
            return "MODEL_AUTH_FAILED", "API Key 无效或没有访问权限"
        if error.code == 400:
            code, message = _tts_rejection(provider_detail)
            if code != "MODEL_PROVIDER_REJECTED":
                return code, message
        if error.code == 429:
            try:
                provider_host = urlsplit(error.geturl()).hostname
            except (TypeError, ValueError):
                provider_host = None
            return _provider_limit_details(provider_detail, provider_host=provider_host)
        if error.code == 404:
            return "MODEL_OR_ENDPOINT_NOT_FOUND", "请求地址或模型不存在"
        return "MODEL_PROVIDER_HTTP_ERROR", f"供应商返回 HTTP {error.code}"
    if isinstance(error, (URLError, TimeoutError)):
        return "MODEL_NETWORK_UNREACHABLE", "无法连接供应商接口"
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        return "MODEL_PROTOCOL_MISMATCH", "供应商响应格式不兼容"
    if isinstance(error, TaskModelConfigurationError):
        return "MODEL_CONFIGURATION_INVALID", str(error)
    return "MODEL_PROBE_FAILED", type(error).__name__


def _request_json(
    url: str,
    token: str,
    *,
    payload: dict | None,
    timeout: float,
    authorization: str | None = None,
):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="GET" if payload is None else "POST",
        headers={
            "Authorization": authorization or ("Bearer " + token),
            "Accept": "application/json",
            **({} if body is None else {"Content-Type": "application/json"}),
        },
    )
    with urlopen(request, timeout=timeout) as response:
        data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ModelProfileProbeError("检测响应过大")
        return response.headers.get_content_type(), data


def list_provider_models(
    db,
    account_id: str,
    *,
    secret_reader=read_provider_secret,
    timeout: float = 15.0,
) -> list[str]:
    account = get_provider_account(db, account_id, include_credential=True)
    if account is None:
        raise ModelProfileProbeError("供应商账号不存在")
    try:
        preset = provider_preset(account["preset"])
    except ValueError as error:
        raise ModelProfileProbeError("供应商预设不支持模型目录") from error
    if not preset["supports_model_catalog"]:
        raise ModelProfileProbeError("该供应商不支持获取模型目录，请手工填写模型 ID")
    secret = secret_reader(account["credentialRef"])
    if not secret:
        raise ModelProfileProbeError("供应商访问凭据未配置")
    try:
        token = provider_access_token(
            secret,
            account["provider"],
            account["channelId"],
        )
        _content_type, data = _request_json(
            provider_endpoint(account["baseUrl"], "models"),
            token,
            payload=None,
            timeout=timeout,
        )
        decoded = json.loads(data)
    except (
        HTTPError,
        URLError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
        ValueError,
        TaskModelConfigurationError,
    ) as error:
        raise ModelProfileProbeError("获取模型目录失败") from error
    if not isinstance(decoded, dict) or not isinstance(decoded.get("data"), list):
        raise ModelProfileProbeError("模型目录响应格式无效")
    return sorted({
        item["id"].strip()
        for item in decoded["data"]
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item["id"].strip()
    })


def _request_tts_audio(
    row,
    profile_settings: dict,
    account_settings: dict,
    token: str,
    voice: str,
    *,
    timeout: float,
    tts_api_key: str | None = None,
    request_id: str | None = None,
) -> bytes:
    protocol = str(profile_settings.get("protocol") or "").strip().lower()
    if protocol == "volcengine_tts":
        if tts_api_key:
            resource_id = str(
                profile_settings.get("resource_id")
                or account_settings.get("tts_resource_id")
                or row["model"]
                or ""
            ).strip()
            if not resource_id:
                raise ModelProfileProbeError(
                    "豆包配音服务版本不存在",
                    "TTS_RESOURCE_MISSING",
                )
            request = Request(
                doubao_tts_v3_endpoint(row["base_url"]),
                data=json.dumps({
                    "user": {"uid": "cutroom-model-probe"},
                    "req_params": {
                        "text": "连接测试",
                        "speaker": voice,
                        "sample_rate": 24000,
                        "audio_params": {
                            "format": "mp3",
                            "speech_rate": 0,
                            "loudness_rate": 0,
                            "bit_rate": 64000,
                        },
                        "additions": json.dumps({
                            "post_process": {"pitch": 0},
                            "disable_markdown_filter": True,
                            "enable_latex_tn": False,
                            "latex_parser": "v2",
                        }),
                    },
                }).encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    "X-Api-Key": tts_api_key,
                    "X-Api-Resource-Id": resource_id,
                    "X-Api-Request-Id": request_id or str(uuid.uuid4()),
                },
            )
            with urlopen(request, timeout=timeout) as response:
                data = response.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                raise ModelProfileProbeError("TTS 试听响应过大")
            chunks = []
            for line in data.decode("utf-8").splitlines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = _response_object(line[5:].strip())
                except json.JSONDecodeError as error:
                    raise ModelProfileProbeError(
                        "豆包 V3 响应格式不兼容",
                        "MODEL_PROTOCOL_MISMATCH",
                    ) from error
                _tts_status(event, {0, 20000000})
                audio_value = event.get("data")
                if audio_value is not None and not isinstance(audio_value, str):
                    raise _protocol_error()
                if isinstance(audio_value, str) and audio_value:
                    try:
                        chunks.append(base64.b64decode(audio_value, validate=True))
                    except ValueError as error:
                        raise ModelProfileProbeError(
                            "豆包 V3 返回了无效音频",
                            "MODEL_PROTOCOL_MISMATCH",
                        ) from error
            audio = b"".join(chunks)
            if not audio:
                raise ModelProfileProbeError("TTS 没有返回有效音频")
            return audio
        app_id = str(account_settings.get("app_id") or "").strip()
        if not app_id:
            raise ModelProfileProbeError("豆包 TTS 直连账号缺少 AppID")
        _content_type, data = _request_json(
            row["base_url"].rstrip("/"),
            token,
            payload={
                "app": {
                    "appid": app_id,
                    "token": token,
                    "cluster": str(
                        account_settings.get("cluster") or "volcano_tts"
                    ).strip(),
                },
                "user": {"uid": "cutroom-model-probe"},
                "audio": {
                    "voice_type": voice,
                    "encoding": "mp3",
                    "speed_ratio": 1.0,
                },
                "request": {
                    "reqid": request_id or str(uuid.uuid4()),
                    "text": "连接测试",
                    "operation": "query",
                },
            },
            timeout=timeout,
            authorization="Bearer;" + token,
        )
        decoded = _response_object(data)
        _tts_status(decoded, {3000})
        audio_value = decoded.get("data")
        if not isinstance(audio_value, str):
            raise _protocol_error()
        audio = base64.b64decode(audio_value, validate=True)
        if not audio:
            raise ModelProfileProbeError("TTS 没有返回有效音频")
        return audio
    content_type, data = _request_json(
        provider_endpoint(row["base_url"], "audio/speech"),
        token,
        payload={
            "model": row["model"],
            "voice": voice,
            "input": "连接测试",
            "response_format": "mp3",
        },
        timeout=timeout,
    )
    if not data or (
        content_type != "application/octet-stream"
        and not content_type.startswith("audio/")
    ):
        raise ModelProfileProbeError("TTS 没有返回有效音频")
    return data


def _voice_preview_context(db, profile_id, voice_id, project_root, secret_reader):
    row = _profile_row(db, profile_id)
    if row is None or row["capability"] != "tts" or not row["enabled"]:
        raise ModelProfileProbeError("配音配置不存在或已停用")
    voice = voice_id.strip() if isinstance(voice_id, str) else ""
    if not voice:
        raise ModelProfileProbeError("请选择要试听的音色")
    profile_settings = _load(row["settings_json"])
    account_settings = _load(row["account_settings_json"])
    resource_id = str(profile_settings.get("resource_id") or row["model"] or "").strip()
    selected = get_provider_voice(db, row["account_id"], resource_id, voice)
    if not selected or not selected.get("selectable"):
        raise ModelProfileProbeError("音色不在当前配置目录中", "VOICE_NOT_SELECTABLE")
    raw_secret = secret_reader(row["credential_ref"])
    if not raw_secret:
        raise ModelProfileProbeError("供应商访问凭据未配置")
    provider = str(row["provider"] or "")
    credentials = decode_provider_credentials(raw_secret) if provider == "doubao_tts" else {}
    tts_api_key = str(credentials.get("api_key") or "").strip() or None
    token = tts_api_key or provider_access_token(raw_secret, provider, account_settings.get("channel_id"))
    # Only the final digest is stored. Neither credentials nor their separate hashes
    # are persisted. Availability checks and display-name edits do not change audio.
    identity = {"version": 1, "text": "连接测试", "voice": voice,
                "profile": profile_id, "account": row["account_id"], "provider": provider,
                "base_url": row["base_url"], "model": row["model"],
                "settings": profile_settings, "account_settings": account_settings,
                "credential": hashlib.sha256(raw_secret.encode("utf-8")).hexdigest()}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    profile_hash = hashlib.sha256(profile_id.encode("utf-8")).hexdigest()
    directory = Path(project_root) / "runtime/workbench/voice-previews" / profile_hash
    return row, voice, profile_settings, account_settings, resource_id, token, tts_api_key, digest, directory


def _preview_receipt(old, directory):
    if old is None:
        return {"state": "not_requested", "requestId": None, "canRetry": False}
    state = old["state"]
    if state == "pending" and old["lease_expires_at"] <= time.time():
        state = "unknown"
    if state == "succeeded":
        filename = (old["preview_url"] or "").rsplit("/", 1)[-1]
        target = directory / filename
        try:
            valid = re.fullmatch(r"[0-9a-f]{64}\.mp3", filename) and target.is_file() and (
                hashlib.sha256(target.read_bytes()).hexdigest() == old["output_sha256"])
        except OSError:
            valid = False
        if not valid:
            state = "cache_changed"
    result = {"state": state, "requestId": old["request_id"], "voiceId": old["voice_id"],
              "canRetry": state in {"unknown", "cache_changed"},
              "errorCode": old["error_code"], "updatedAt": old["updated_at"]}
    if state == "succeeded":
        result["previewUrl"] = old["preview_url"]
    return result


def voice_preview_status(db, profile_id, voice_id, project_root, *, secret_reader=read_provider_secret):
    """Inspect local receipt/cache only; never retry or query a paid provider."""
    *_, digest, directory = _voice_preview_context(db, profile_id, voice_id, project_root, secret_reader)
    old = db.connection.execute("SELECT * FROM voice_preview_requests WHERE input_digest=?", (digest,)).fetchone()
    return _preview_receipt(old, directory)


def _preview_blocked(receipt):
    code, message = {
        "pending": ("VOICE_PREVIEW_IN_PROGRESS", "试听仍在处理中，请稍后刷新状态。"),
        "unknown": ("VOICE_PREVIEW_RESULT_UNKNOWN", "未取得可复用结果，请先核对供应商记录；不会自动重发。"),
        "cache_changed": ("VOICE_PREVIEW_CACHE_CHANGED", "试听缓存缺失或已变化，不会自动重复付费生成。"),
    }[receipt["state"]]
    return ModelProfileProbeError(message + f"（请求 {receipt['requestId']}）", code, receipt=receipt)


def _preflight_voice_preview_storage(directory: Path, target: Path) -> None:
    """Check the actual attempt's destination before recording or sending it."""
    temporary = directory / f".{uuid.uuid4().hex}.tmp"
    owns_target = False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError("preview attempt destination already exists")
        temporary.write_bytes(b"\0")
        os.replace(temporary, target)
        owns_target = True
        target.unlink()
    except OSError as error:
        raise ModelProfileProbeError(
            "本机试听缓存无法写入、替换或清理，请检查存储路径和权限；尚未调用配音服务。",
            "VOICE_PREVIEW_STORAGE_UNAVAILABLE",
        ) from error
    finally:
        # Cleanup must never hide the original filesystem failure or remove a
        # pre-existing cache. These paths belong only to this new attempt.
        for path in ([temporary, target] if owns_target else [temporary]):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def create_tts_voice_preview(
    db, profile_id: str, voice_id: str, project_root: Path | str, *,
    secret_reader=read_provider_secret, timeout: float = 15.0,
    retry_request_id: str | None = None, provider_checked: bool = False,
    confirm_paid_retry: bool = False,
) -> dict:
    (row, voice, profile_settings, account_settings, resource_id, token, tts_api_key,
     digest, directory) = _voice_preview_context(db, profile_id, voice_id, project_root, secret_reader)
    if retry_request_id is not None and (not isinstance(retry_request_id, str) or not retry_request_id
            or provider_checked is not True or confirm_paid_retry is not True):
        raise ModelProfileProbeError("请核对原请求记录，并明确确认重新生成可能再次收费。",
                                     "VOICE_PREVIEW_RECOVERY_CONFIRM_REQUIRED")
    with db.transaction(immediate=True) as connection:
        old = connection.execute("SELECT * FROM voice_preview_requests WHERE input_digest=?", (digest,)).fetchone()
        receipt = _preview_receipt(old, directory)
        if retry_request_id is not None and (old is None or old["request_id"] != retry_request_id):
            # Repeated delivery of the same confirmation may retrieve its result,
            # but can never authorize another generation after a second failure.
            previous = connection.execute("SELECT replaced_by FROM voice_preview_attempt_history WHERE request_id=? AND input_digest=?",
                                          (retry_request_id, digest)).fetchone()
            if old is None or previous is None or previous[0] != old["request_id"] or receipt["state"] != "succeeded":
                raise ModelProfileProbeError("请求或配置已变化，请刷新状态后重新核对。",
                                             "VOICE_PREVIEW_RECOVERY_STALE", receipt=receipt)
        if receipt["state"] == "succeeded":
            return {"voiceId": voice, "previewUrl": receipt["previewUrl"],
                    "requestId": receipt["requestId"], "reused": True}
        if receipt["state"] == "pending" or (receipt["canRetry"] and retry_request_id is None):
            raise _preview_blocked(receipt)
        request_id = str(uuid.uuid4())
        # Probe the full destination name: a shorter temporary path can work
        # while the final rename fails (for example, Windows path limits).
        filename = hashlib.sha256(f"{digest}:{request_id}".encode()).hexdigest() + ".mp3"
        target = directory / filename
        _preflight_voice_preview_storage(directory, target)
        if old is not None:
            connection.execute("""INSERT INTO voice_preview_attempt_history
                (request_id,input_digest,profile_id,voice_id,state,preview_url,output_sha256,error_code,
                 created_at,updated_at,replaced_by,reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (old["request_id"], digest, profile_id, voice, receipt["state"], old["preview_url"],
                 old["output_sha256"], old["error_code"], old["created_at"], old["updated_at"], request_id,
                 "user_confirmed_retry" if retry_request_id else "definite_rejection_retry"))
        connection.execute("""INSERT INTO voice_preview_requests
            (input_digest,request_id,profile_id,voice_id,state,lease_expires_at) VALUES(?,?,?,?,'pending',?)
            ON CONFLICT(input_digest) DO UPDATE SET request_id=excluded.request_id,
            state='pending',error_code=NULL,preview_url=NULL,output_sha256=NULL,
            lease_expires_at=excluded.lease_expires_at,created_at=datetime('now'),updated_at=datetime('now')""",
            (digest, request_id, profile_id, voice, time.time()+max(120, timeout*3+30)))
    # Each attempt owns a distinct URL: a late result or browser cache cannot
    # substitute audio belonging to a superseded receipt.
    preview_url = f"/api/voice-previews/{directory.name}/{filename}"
    try:
        audio = _request_tts_audio(row, profile_settings, account_settings, token, voice,
                                   timeout=timeout, tts_api_key=tts_api_key, request_id=request_id)
        with db.transaction(immediate=True) as connection:
            current = connection.execute("SELECT * FROM voice_preview_requests WHERE input_digest=?", (digest,)).fetchone()
            if current["request_id"] != request_id:
                raise ModelProfileProbeError("原请求已被后续恢复替代，迟到结果没有覆盖当前试听。",
                                             "VOICE_PREVIEW_SUPERSEDED", receipt=_preview_receipt(current, directory))
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / f".{uuid.uuid4().hex}.tmp"
            try:
                temporary.write_bytes(audio)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            mark_voice_test_result(db, row["account_id"], resource_id, voice, available=True)
            connection.execute("""UPDATE voice_preview_requests SET state='succeeded',
                preview_url=?,output_sha256=?,updated_at=datetime('now')
                WHERE input_digest=? AND request_id=? AND state='pending'""",
                (preview_url, hashlib.sha256(audio).hexdigest(), digest, request_id))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError,
            TaskModelConfigurationError, ModelProfileProbeError) as error:
        if isinstance(error, ModelProfileProbeError) and error.code == "VOICE_PREVIEW_SUPERSEDED":
            raise
        code, message = ((error.code, str(error)) if isinstance(error, ModelProfileProbeError)
                         else _failure_details(error))
        rejected = isinstance(error, HTTPError) and error.code in {400, 401, 403, 404, 422, 429}
        with db.transaction(immediate=True) as connection:
            changed = connection.execute("""UPDATE voice_preview_requests SET state=?,error_code=?,
                updated_at=datetime('now') WHERE input_digest=? AND request_id=?""",
                ('rejected' if rejected else 'unknown', code, digest, request_id)).rowcount
            if changed:
                mark_voice_test_result(db, row["account_id"], resource_id, voice, available=False,
                                       error_code=code, error_message=message)
            current = connection.execute("SELECT * FROM voice_preview_requests WHERE input_digest=?", (digest,)).fetchone()
        raise ModelProfileProbeError(message + f"（请求 {request_id}）", code,
                                     receipt=_preview_receipt(current, directory)) from error
    return {"voiceId": voice, "previewUrl": preview_url, "requestId": request_id, "reused": False}


def probe_model_profile(
    db,
    profile_id: str,
    *,
    secret_reader=read_provider_secret,
    timeout: float | None = None,
) -> dict:
    row = _profile_row(db, profile_id)
    if row is None:
        raise ModelProfileProbeError("模型配置不存在")
    if row["capability"] == "visual_analysis":
        from .visual_profile_probe import (
            PROBE_TIMEOUT_SECONDS, probe_visual_profile, visual_probe_request_binding,
        )

        return probe_visual_profile(
            db, profile_id, secret_reader=secret_reader,
            timeout=PROBE_TIMEOUT_SECONDS if timeout is None else timeout,
            stop_on_failure=True, approved_request=visual_probe_request_binding(row),
        )
    timeout = 15.0 if timeout is None else timeout
    profile_settings = _load(row["settings_json"])
    account_settings = _load(row["account_settings_json"])
    verification_level = "request"
    try:
        raw_secret = secret_reader(row["credential_ref"])
        if not raw_secret:
            raise ModelProfileProbeError("供应商访问凭据未配置")
        provider = str(row["provider"] or "")
        credentials = (
            decode_provider_credentials(raw_secret)
            if provider == "doubao_tts"
            else {}
        )
        tts_api_key = str(credentials.get("api_key") or "").strip() or None
        token = (
            tts_api_key
            if tts_api_key
            else provider_access_token(
                raw_secret,
                provider,
                account_settings.get("channel_id"),
            )
        )
        capability = row["capability"]
        if capability in {
            "planning",
            "general_text",
            "copywriting",
            "storyboard",
            "packaging",
        }:
            _content_type, data = _request_json(
                provider_endpoint(row["base_url"], "chat/completions"),
                token,
                payload={
                    "model": row["model"],
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 8,
                },
                timeout=timeout,
            )
            decoded = _response_object(data)
            if not isinstance(decoded.get("choices"), list) or not decoded["choices"]:
                raise _protocol_error()
            for choice in decoded["choices"]:
                if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                    raise _protocol_error()
                message = choice["message"]
                if not isinstance(message.get("content"), str):
                    raise _protocol_error()
        elif capability == "tts":
            voice = profile_settings.get("voice") or profile_settings.get(
                "default_voice_id"
            )
            if not isinstance(voice, str) or not voice.strip():
                raise ModelProfileProbeError("TTS 配置缺少音色")
            _request_tts_audio(
                row,
                profile_settings,
                account_settings,
                token,
                voice.strip(),
                timeout=timeout,
                tts_api_key=tts_api_key,
            )
        elif capability in {"broll", "video_generation"}:
            verification_level = "catalog"
            _content_type, data = _request_json(
                provider_endpoint(row["base_url"], "models"),
                token,
                payload=None,
                timeout=timeout,
            )
            decoded = _response_object(data)
            entries = decoded.get("data")
            if not isinstance(entries, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not item["id"].strip() for item in entries
            ):
                raise _protocol_error()
            model_ids = {item["id"] for item in entries}
            if row["model"] not in model_ids:
                raise ModelProfileProbeError("视频模型未出现在当前账号可用目录中")
        else:
            raise ModelProfileProbeError("该能力不需要远程模型检测")
    except (
        HTTPError,
        URLError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
        ValueError,
        TaskModelConfigurationError,
    ) as error:
        error_code, safe_message = _failure_details(error)
        mark_profile_check(
            db,
            profile_id,
            available=False,
            error_code=error_code,
            error_message=safe_message,
        )
        raise ModelProfileProbeError(safe_message, error_code) from error
    except ModelProfileProbeError as error:
        mark_profile_check(
            db,
            profile_id,
            available=False,
            error_code=error.code,
            error_message=str(error),
        )
        raise

    result = mark_profile_check(db, profile_id, available=True)
    result["verificationLevel"] = verification_level
    return result
