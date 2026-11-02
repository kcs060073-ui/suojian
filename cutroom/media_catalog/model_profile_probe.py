"""Real, bounded availability probes for task-selectable model profiles."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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
from .tts_voice_catalog import mark_voice_test_result


class ModelProfileProbeError(RuntimeError):
    def __init__(self, message: str, code: str = "MODEL_PROBE_FAILED") -> None:
        super().__init__(message)
        self.code = code


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
                    "X-Api-Request-Id": str(uuid.uuid4()),
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
                    "reqid": "cutroom-model-probe",
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


def create_tts_voice_preview(
    db,
    profile_id: str,
    voice_id: str,
    project_root: Path | str,
    *,
    secret_reader=read_provider_secret,
    timeout: float = 15.0,
) -> dict:
    row = _profile_row(db, profile_id)
    if row is None or row["capability"] != "tts":
        raise ModelProfileProbeError("配音配置不存在")
    voice = voice_id.strip() if isinstance(voice_id, str) else ""
    if not voice:
        raise ModelProfileProbeError("请选择要试听的音色")
    profile_settings = _load(row["settings_json"])
    account_settings = _load(row["account_settings_json"])
    resource_id = str(
        profile_settings.get("resource_id") or row["model"] or ""
    ).strip()
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
        audio = _request_tts_audio(
            row,
            profile_settings,
            account_settings,
            token,
            voice,
            timeout=timeout,
            tts_api_key=tts_api_key,
        )
    except (
        HTTPError,
        URLError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
        ValueError,
        TaskModelConfigurationError,
        ModelProfileProbeError,
    ) as error:
        if isinstance(error, ModelProfileProbeError):
            error_code, safe_message = error.code, str(error)
        else:
            error_code, safe_message = _failure_details(error)
        mark_voice_test_result(
            db,
            row["account_id"],
            resource_id,
            voice,
            available=False,
            error_code=error_code,
            error_message=safe_message,
        )
        raise ModelProfileProbeError(safe_message, error_code) from error

    mark_voice_test_result(
        db,
        row["account_id"],
        resource_id,
        voice,
        available=True,
    )
    profile_hash = hashlib.sha256(profile_id.encode("utf-8")).hexdigest()
    voice_hash = hashlib.sha256(voice.encode("utf-8")).hexdigest()
    directory = (
        Path(project_root)
        / "runtime"
        / "workbench"
        / "voice-previews"
        / profile_hash
    )
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{voice_hash}.mp3"
    # Keep the atomic staging name shorter than the final hashed filename.
    # Repeating the 64-character voice hash plus a UUID can push otherwise
    # valid Windows installations past MAX_PATH before os.replace runs.
    temporary = directory / f".{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_bytes(audio)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "voiceId": voice,
        "previewUrl": f"/api/voice-previews/{profile_hash}/{voice_hash}.mp3",
    }


def probe_model_profile(
    db,
    profile_id: str,
    *,
    secret_reader=read_provider_secret,
    timeout: float = 15.0,
) -> dict:
    row = _profile_row(db, profile_id)
    if row is None:
        raise ModelProfileProbeError("模型配置不存在")
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
