"""Canonical endpoint resolution for OpenAI-compatible providers."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


_TERMINAL_ENDPOINTS = (
    "/chat/completions",
    "/models",
    "/audio/speech",
)
_VERSION_RE = re.compile(r"/v\d+$", re.IGNORECASE)


def normalize_api_root(base_url: str) -> str:
    """Return an API root from either a root URL or a full request URL."""
    value = base_url.strip().rstrip("/") if isinstance(base_url, str) else ""
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("模型请求地址必须是有效的 HTTP(S) URL")
    if parts.query or parts.fragment:
        raise ValueError("模型请求地址不能包含查询参数或片段")
    path = parts.path.rstrip("/")
    lower_path = path.lower()
    for suffix in _TERMINAL_ENDPOINTS:
        if lower_path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    if not _VERSION_RE.search(path):
        path += "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def provider_endpoint(base_url: str, endpoint: str) -> str:
    """Resolve one provider endpoint below a normalized API root."""
    suffix = endpoint.strip("/") if isinstance(endpoint, str) else ""
    if not suffix:
        raise ValueError("模型接口路径不能为空")
    return normalize_api_root(base_url) + "/" + suffix
