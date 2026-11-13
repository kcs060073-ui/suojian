"""Official Volcengine ListSpeakers adapter with safe normalized output."""

from __future__ import annotations

from collections.abc import Mapping
import json


class VoiceCatalogError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


def _required(value, label: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise VoiceCatalogError(
            "VOICE_CATALOG_CREDENTIALS_MISSING",
            f"{label}未配置。",
        )
    return text


def _default_client_factory(access_key_id: str, secret_access_key: str):
    try:
        import volcenginesdkcore
        import volcenginesdkspeechsaasprod
    except ImportError as error:
        raise VoiceCatalogError(
            "VOICE_CATALOG_SDK_MISSING",
            "音色目录组件未安装，请重新运行安装程序。",
        ) from error
    configuration = volcenginesdkcore.Configuration()
    configuration.ak = access_key_id
    configuration.sk = secret_access_key
    configuration.region = "cn-beijing"
    configuration.debug = False
    api_client = volcenginesdkcore.ApiClient(configuration)
    return (
        volcenginesdkspeechsaasprod.SPEECHSAASPRODApi(api_client),
        volcenginesdkspeechsaasprod.ListSpeakersRequest,
    )


def _request(request_type, *, page: int, resource_id: str):
    values = {
        "page": page,
        "resource_ids": [resource_id],
    }
    try:
        return request_type(**values)
    except TypeError:
        request = request_type()
        for key, value in values.items():
            setattr(request, key, value)
        return request


_MISSING = object()


def _field(value, *names, default=None):
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        converted = converter()
        if isinstance(converted, Mapping):
            return _field(converted, *names, default=default)
    return default


def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _texts(value, *nested_fields: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    items = value if isinstance(value, (list, tuple)) else [value]
    result = []
    for item in items:
        if isinstance(item, str):
            if item.strip():
                result.append(item.strip())
            continue
        for field in nested_fields:
            nested = _field(item, field, default=_MISSING)
            if nested is _MISSING or nested is None:
                continue
            values = _texts(nested)
            if values:
                result.extend(values)
                break
    return result


def _unique(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _normalize_speaker(speaker) -> dict:
    voice_id = _text(_field(speaker, "VoiceType", "voice_type"))
    display_name = _text(_field(speaker, "Name", "name"))
    resource_id = _text(_field(speaker, "ResourceID", "resource_id"))
    if not voice_id or not display_name or not resource_id:
        raise VoiceCatalogError(
            "VOICE_CATALOG_INVALID_RESPONSE",
            "音色目录缺少音色名称、标识或服务版本。",
        )
    categories = _texts(
        _field(speaker, "Categories", "categories"),
        "categories",
    )
    normal_labels = _texts(
        _field(speaker, "NormalLabels", "normal_labels")
    )
    special_labels = _texts(
        _field(speaker, "SpecialLabels", "special_labels")
    )
    return {
        "voiceId": voice_id,
        "displayName": display_name,
        "resourceId": resource_id,
        "gender": _text(_field(speaker, "Gender", "gender")),
        "languages": _texts(
            _field(speaker, "Languages", "languages"),
            "text",
            "language",
        ),
        "emotions": _texts(
            _field(speaker, "Emotions", "emotions"),
            "label",
            "value",
        ),
        "styles": _unique(categories + normal_labels + special_labels),
        "previewUrl": _text(
            _field(speaker, "TrialURL", "trial_url")
        ),
        "shortPreviewUrl": _text(
            _field(speaker, "ShortTrialURL", "short_trial_url")
        ),
        "metadata": {
            "age": _text(_field(speaker, "Age", "age")),
            "avatar": _text(_field(speaker, "Avatar", "avatar")),
            "description": _text(
                _field(speaker, "Description", "description")
            ),
            "categories": categories,
            "normalLabels": normal_labels,
            "specialLabels": special_labels,
        },
    }


def _safe_upstream_error(error: BaseException) -> VoiceCatalogError:
    status = getattr(error, "status", None) or getattr(error, "status_code", None)
    name = type(error).__name__.lower()
    body = getattr(error, "body", None)
    try:
        payload = json.loads(body) if isinstance(body, str) else body
    except (TypeError, json.JSONDecodeError):
        payload = None

    def error_code(value) -> str | None:
        if isinstance(value, Mapping):
            upstream_error = value.get("Error") or value.get("error")
            if isinstance(upstream_error, Mapping):
                code = upstream_error.get("Code") or upstream_error.get("code")
                return code if isinstance(code, str) else None
            for nested in value.values():
                code = error_code(nested)
                if code:
                    return code
        elif isinstance(value, (list, tuple)):
            for nested in value:
                code = error_code(nested)
                if code:
                    return code
        return None

    if error_code(payload) == "InvalidParameter":
        return VoiceCatalogError(
            "VOICE_CATALOG_INVALID_PARAMETER",
            "音色目录请求参数不兼容，请更新 剪辑工作台 后重试。",
        )
    if status in {401, 403} or "unauthorized" in name or "forbidden" in name:
        return VoiceCatalogError(
            "VOICE_CATALOG_AUTH_FAILED",
            "无权读取音色目录，请检查 AK/SK、IAM 权限与服务开通状态。",
        )
    if isinstance(error, TimeoutError) or "timeout" in name:
        return VoiceCatalogError(
            "VOICE_CATALOG_TIMEOUT",
            "读取音色目录超时，请检查网络后重试。",
        )
    return VoiceCatalogError(
        "VOICE_CATALOG_UNAVAILABLE",
        "暂时无法读取音色目录，请稍后重试。",
    )


def fetch_volcengine_voices(
    access_key_id: str,
    secret_access_key: str,
    resource_id: str,
    *,
    timeout: float = 15.0,
    client_factory=None,
) -> list[dict]:
    access_key_id = _required(access_key_id, "Access Key ID")
    secret_access_key = _required(secret_access_key, "Secret Access Key")
    resource_id = _required(resource_id, "配音服务版本")
    factory = client_factory or _default_client_factory
    try:
        client, request_type = factory(access_key_id, secret_access_key)
        result = []
        seen_voice_ids = set()
        fetched_count = 0
        page = 1
        while page <= 100:
            response = client.list_speakers(
                _request(request_type, page=page, resource_id=resource_id),
                _request_timeout=timeout,
            )
            raw_speakers = _field(
                response,
                "Speakers",
                "speakers",
                default=_MISSING,
            )
            if raw_speakers is _MISSING or not isinstance(
                raw_speakers, (list, tuple)
            ):
                raise VoiceCatalogError(
                    "VOICE_CATALOG_INVALID_RESPONSE",
                    "音色目录返回了无法识别的数据。",
                )
            for item in raw_speakers:
                normalized = _normalize_speaker(item)
                if normalized["voiceId"] in seen_voice_ids:
                    continue
                seen_voice_ids.add(normalized["voiceId"])
                result.append(normalized)
            fetched_count += len(raw_speakers)
            raw_total = _field(response, "Total", "total", default=len(result))
            try:
                total = int(raw_total)
            except (TypeError, ValueError) as error:
                raise VoiceCatalogError(
                    "VOICE_CATALOG_INVALID_RESPONSE",
                    "音色目录返回了无效的总数。",
                ) from error
            if fetched_count >= total or not raw_speakers:
                return result
            page += 1
        raise VoiceCatalogError(
            "VOICE_CATALOG_INVALID_RESPONSE",
            "音色目录分页数量异常，请稍后重试。",
        )
    except VoiceCatalogError:
        raise
    except BaseException as error:
        raise _safe_upstream_error(error) from error
