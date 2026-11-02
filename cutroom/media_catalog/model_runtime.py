"""Resolve immutable task model bindings into credential-safe process environments."""

from __future__ import annotations

import os
from typing import Callable, Mapping

from .credentials import decode_provider_credentials, read_provider_secret
from .model_profiles import USAGE_ROLE, get_task_profile_binding_for_usage
from .provider_endpoints import normalize_api_root


class TaskModelConfigurationError(RuntimeError):
    """Raised when a frozen task model binding cannot be executed safely."""


def doubao_tts_v3_endpoint(base_url: str) -> str:
    value = str(base_url or "").strip().rstrip("/")
    legacy_suffix = "/api/v1/tts"
    v3_suffix = "/api/v3/tts/unidirectional/sse"
    if value.endswith(v3_suffix):
        return value
    if value.endswith(legacy_suffix):
        return value[: -len(legacy_suffix)] + v3_suffix
    return value


def provider_access_token(secret: str, provider: str, channel_id) -> str:
    if provider == "doubao_tts":
        value = decode_provider_credentials(secret).get("access_token") or ""
    else:
        value = secret.strip() if isinstance(secret, str) else ""
    if not value:
        raise TaskModelConfigurationError("MODEL_CREDENTIAL_MISSING: 模型凭据不存在")
    if provider != "new_api" or channel_id in (None, ""):
        return value
    try:
        channel = int(channel_id)
    except (TypeError, ValueError) as error:
        raise TaskModelConfigurationError(
            "MODEL_CHANNEL_INVALID: New API 渠道 ID 无效"
        ) from error
    if channel <= 0:
        raise TaskModelConfigurationError(
            "MODEL_CHANNEL_INVALID: New API 渠道 ID 无效"
        )
    suffix = f"-{channel}"
    return value if value.endswith(suffix) else value + suffix


def pinned_gateway_token(secret: str, channel_id) -> str:
    """Compatibility wrapper for callers that explicitly use New API routing."""
    return provider_access_token(secret, "new_api", channel_id)


def task_model_environment(
    db,
    task_id: str,
    capability: str,
    *,
    base_environment: Mapping[str, str] | None = None,
    secret_reader: Callable[[str], str | None] = read_provider_secret,
) -> tuple[dict[str, str], dict]:
    snapshot = get_task_profile_binding_for_usage(db, task_id, capability)
    if snapshot is None:
        raise TaskModelConfigurationError(
            f"MODEL_BINDING_MISSING: 任务未冻结 {capability} 模型配置"
        )
    bound_capability = snapshot.get("capability")
    expected_capabilities = {capability, USAGE_ROLE.get(capability, capability)}
    if bound_capability not in expected_capabilities:
        raise TaskModelConfigurationError("MODEL_BINDING_INVALID: 模型能力快照不匹配")
    settings = snapshot.get("profileSettings")
    settings = settings if isinstance(settings, dict) else {}
    account_settings = snapshot.get("accountSettings")
    account_settings = account_settings if isinstance(account_settings, dict) else {}
    protocol = str(settings.get("protocol") or "").strip().lower()
    supported_protocols = {
        "openai",
        "openai_compatible",
        "openai_chat",
        "openai_speech",
        "doubao_video",
        "volcengine_tts",
    }
    if protocol not in supported_protocols:
        raise TaskModelConfigurationError(
            f"MODEL_PROTOCOL_UNSUPPORTED: 暂不支持协议 {protocol or '未设置'}"
        )
    credential_ref = snapshot.get("credentialRef")
    if not isinstance(credential_ref, str) or not credential_ref:
        raise TaskModelConfigurationError("MODEL_CREDENTIAL_REF_MISSING: 模型凭据引用不存在")
    raw_secret = secret_reader(credential_ref) or ""
    provider = str(snapshot.get("provider") or "")
    provider_credentials = (
        decode_provider_credentials(raw_secret)
        if provider == "doubao_tts"
        else {}
    )
    tts_api_key = str(provider_credentials.get("api_key") or "").strip()
    token = (
        tts_api_key
        if tts_api_key
        else provider_access_token(
            raw_secret,
            provider,
            snapshot.get("channelId"),
        )
    )
    model = str(snapshot.get("model") or "").strip()
    if not model:
        raise TaskModelConfigurationError("MODEL_NAME_MISSING: 模型名称不存在")
    try:
        api_root = normalize_api_root(str(snapshot.get("baseUrl") or ""))
    except ValueError as error:
        raise TaskModelConfigurationError(
            "MODEL_BASE_URL_INVALID: 模型请求地址无效"
        ) from error
    environment = dict(os.environ if base_environment is None else base_environment)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        environment.pop(key, None)

    if capability in {
        "planning",
        "general_text",
        "copywriting",
        "storyboard",
        "packaging",
    }:
        environment.update({
            "ARK_API_KEY": token,
            "ARK_MODEL": model,
            "ARK_BASE_URL": api_root,
        })
    elif capability == "tts":
        voice = str(settings.get("voice") or "").strip()
        if not voice:
            raise TaskModelConfigurationError("TTS_VOICE_MISSING: 配音配置缺少音色")
        if protocol == "volcengine_tts":
            if tts_api_key:
                resource_id = str(
                    settings.get("resource_id") or model
                ).strip()
                if not resource_id:
                    raise TaskModelConfigurationError(
                        "TTS_RESOURCE_MISSING: 豆包配音服务版本不存在"
                    )
                environment.update({
                    "TTS_PROTOCOL": "volcengine_v3",
                    "TTS_ENDPOINT": doubao_tts_v3_endpoint(
                        str(snapshot.get("baseUrl") or "")
                    ),
                    "TTS_API_KEY": tts_api_key,
                    "TTS_RESOURCE_ID": resource_id,
                    "TTS_VOICE_TYPE": voice,
                })
                for key in (
                    "TTS_APPID", "TTS_ACCESS_TOKEN", "TTS_CLUSTER", "TTS_MODEL"
                ):
                    environment.pop(key, None)
                return environment, snapshot
            app_id = str(account_settings.get("app_id") or "").strip()
            if not app_id:
                raise TaskModelConfigurationError(
                    "TTS_APPID_MISSING: 豆包 TTS 直连账号缺少 AppID"
                )
            environment.update({
                "TTS_PROTOCOL": "volcengine",
                "TTS_ENDPOINT": str(snapshot.get("baseUrl") or "").strip().rstrip("/"),
                "TTS_APPID": app_id,
                "TTS_ACCESS_TOKEN": token,
                "TTS_CLUSTER": str(
                    account_settings.get("cluster") or "volcano_tts"
                ).strip(),
                "TTS_VOICE_TYPE": voice,
            })
            environment.pop("TTS_API_KEY", None)
            environment.pop("TTS_RESOURCE_ID", None)
            environment.pop("TTS_MODEL", None)
        else:
            environment.update({
                "TTS_PROTOCOL": "openai",
                "TTS_ENDPOINT": api_root + "/audio/speech",
                "TTS_ACCESS_TOKEN": token,
                "TTS_MODEL": model,
                "TTS_VOICE_TYPE": voice,
            })
            environment.pop("TTS_APPID", None)
            environment.pop("TTS_API_KEY", None)
            environment.pop("TTS_RESOURCE_ID", None)
    elif capability in {"broll", "video_generation"}:
        environment.update({
            "SEEDANCE_API_KEY": token,
            "SEEDANCE_MODEL": model,
            "SEEDANCE_BASE_URL": api_root,
        })
    else:
        raise TaskModelConfigurationError(f"MODEL_CAPABILITY_UNSUPPORTED: {capability}")
    return environment, snapshot
