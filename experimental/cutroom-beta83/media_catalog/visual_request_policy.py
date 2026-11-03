"""Bound future visual requests and distinguish safe unknown-outcome diagnostics."""
from urllib.error import HTTPError, URLError

VISUAL_REQUEST_TIMEOUT_SECONDS = 120.0


def unknown_visual_failure(error):
    from .workbench_worker import ProviderOutcomeUnknown
    if isinstance(error, HTTPError):
        state = 'visual_http_408_unknown' if error.code == 408 else 'visual_http_5xx_unknown' if error.code >= 500 else 'visual_http_unknown'
        return ProviderOutcomeUnknown(
            f'VISUAL_HTTP_UNKNOWN: 视觉服务返回 HTTP {error.code}，结果尚未确认；请核对原请求，不自动重发', provider_state=state)
    if isinstance(error, TimeoutError) or isinstance(error, URLError) and isinstance(error.reason, TimeoutError):
        return ProviderOutcomeUnknown(
            'VISUAL_TIMEOUT_UNKNOWN: 视觉请求等待超时，可能已被服务接收；请核对原请求，不自动重发',
            provider_state='visual_timeout_unknown')
    return ProviderOutcomeUnknown(
        'VISUAL_NETWORK_UNKNOWN: 视觉请求网络中断，结果尚未确认；请核对原请求，不自动重发',
        provider_state='visual_network_unknown')


# Official GLM 5.3 Flash is forced-thinking; only its standard endpoint is opted in.
GLM_REASONING_POLICY_VERSION = 'bigmodel-glm-5.3-flash-low-v1'


def visual_request_options(row):
    from urllib.parse import urlsplit
    from .provider_endpoints import provider_endpoint
    try:
        if row['provider'] != 'openai_compatible' or row['model'] != 'glm-5.3-flash':
            return {}
        raw = row['base_url']
        url = urlsplit(raw)
        if (url.scheme != 'https' or url.netloc != 'open.bigmodel.cn' or url.query or url.fragment
                or url.path.rstrip('/') not in {'/api/paas/v4', '/api/paas/v4/chat/completions'}):
            return {}
        if provider_endpoint(raw, 'chat/completions') != 'https://open.bigmodel.cn/api/paas/v4/chat/completions':
            return {}
    except (KeyError, TypeError, ValueError):
        return {}
    return {'thinking': {'type': 'enabled'}, 'reasoning_effort': 'low'}


def visual_request_contract(row):
    options = visual_request_options(row)
    return {'version': GLM_REASONING_POLICY_VERSION, 'options': options} if options else {}
