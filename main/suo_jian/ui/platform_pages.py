"""Read bounded public page data without evaluating website JavaScript."""
import json
import re
from urllib.parse import parse_qs, urlsplit

from .video_link import validate_url

PAGE_LIMIT = 5 * 1024 * 1024


def page_state(extractor, url, variable):
    with extractor.urlopen(url) as response:
        raw = response.read(PAGE_LIMIT + 1)
    if len(raw) > PAGE_LIMIT:
        raise ValueError('视频页面信息过大，无法读取。')
    text = raw.decode('utf-8', errors='replace')
    match = re.search(r'window\.' + re.escape(variable) + r'\s*=\s*', text)
    try:
        state = json.JSONDecoder().raw_decode(text[match.end():])[0] if match else None
    except ValueError:
        state = None
    if not isinstance(state, dict):
        raise ValueError('视频页面结构已变化，暂时无法读取，请上传本地视频。')
    return state


def haokan_info(url, extractor):
    parsed = urlsplit(url)
    ids = parse_qs(parsed.query).get('vid', [])
    if parsed.path != '/v' or len(ids) != 1 or not re.fullmatch(r'\d{5,24}', ids[0]):
        raise ValueError('请使用好看视频的单条视频链接（包含 vid），不支持列表页。')
    state = page_state(extractor, url, '__PRELOADED_STATE__')
    app = state.get('openAppInfo')
    page = app.get('pagepd') if isinstance(app, dict) else None
    if isinstance(page, dict) and page.get('pagepd') == 'web_hkoffline':
        raise ValueError('这条好看视频已不可访问，页面返回的是推荐内容，请换一条原片链接。')
    # Recommendations must never be used as the requested original video.
    video = state.get('curVideoMeta')
    if not isinstance(video, dict) or str(video.get('vid') or video.get('id')) != ids[0]:
        raise ValueError('好看视频未返回与链接编号一致的原片，请上传本地视频。')
    media = video.get('playurl') or video.get('html5_playurl')
    if not isinstance(media, str) or not media.startswith(('https://', 'http://')):
        raise ValueError('好看视频未提供公开播放地址，请上传本地视频。')
    return {'id': ids[0], 'title': video.get('title') or ids[0],
            'url': validate_url(media), 'protocol': urlsplit(media).scheme,
            'webpage_url': url, 'http_headers': {'Referer': url}}


def ixigua_mobile_info(url, extractor):
    match = re.fullmatch(r'/(?:video/)?(\d{5,24})/?', urlsplit(url).path)
    if not match:
        raise ValueError('请使用西瓜视频的单条视频链接，不支持频道或列表。')
    state = page_state(extractor, url, '_SSR_DATA')
    try:
        video = state['data']['storeState']['detail']['videoData']['result']
    except (KeyError, TypeError):
        video = None
    if not isinstance(video, dict) or str(video.get('gid')) != match[1]:
        raise ValueError('西瓜视频未返回与链接编号一致的原片，请上传本地视频。')
    media = video.get('url')
    if not isinstance(media, str) or not media.startswith(('https://', 'http://')):
        raise ValueError('西瓜视频页面未提供可直接读取的公开播放地址，请在原站确认可播放完整视频，或上传本地文件。')
    return {'id': match[1], 'title': video.get('title') or match[1],
            'duration': video.get('duration'), 'url': validate_url(media),
            'protocol': urlsplit(media).scheme, 'webpage_url': url,
            'http_headers': {'Referer': url}}


def assert_requested_identity(url, info):
    host = urlsplit(url).hostname or ''
    match = re.fullmatch(r'/video/(\d+)/?', urlsplit(url).path)
    if (host == 'www.douyin.com' and match and isinstance(info, dict)
            and str(info.get('id')) != match[1]):
        raise ValueError('抖音返回的视频编号与原链接不一致，已停止读取，请换一条原片链接。')
