"""Native segment downloads through the same validated HTTP client as share pages."""
import io
import time
from pathlib import Path

from .video_link import MAX_BYTES


class DownloadBudget:
    def __init__(self, max_bytes=MAX_BYTES, progress=None):
        self.max_bytes = max_bytes
        self.progress = progress
        self.size = 0
        self.deadline = time.monotonic() + 600
        self.error = None

    def check(self):
        if self.error:
            raise ValueError(self.error)
        if time.monotonic() > self.deadline:
            self.error = '分段视频下载超时，请重试。'
            raise ValueError(self.error)


class BoundedStream(io.RawIOBase):
    """Keep the pinned connection alive, bounding bytes across segments and retries."""
    def __init__(self, connection, response, budget):
        self.connection = connection
        self.response = response
        self.budget = budget

    def readable(self):
        return True

    def read(self, size=-1):
        self.budget.check()
        remaining = self.budget.max_bytes - self.budget.size
        limit = remaining + 1 if size is None or size < 0 else min(size, remaining + 1)
        data = self.response.read(limit)
        self.budget.size += len(data)
        if self.budget.size > self.budget.max_bytes:
            self.budget.error = '视频大小超出限制，当前最多支持 1 GB。'
            raise ValueError(self.budget.error)
        if self.budget.progress:
            self.budget.progress(self.budget.size, None)
        return data

    def close(self):
        self.connection.close()
        super().close()


def download_segments(extractor, fmt, path, headers):
    from yt_dlp.downloader.hls import HlsFD
    from yt_dlp.downloader.dash import DashSegmentsFD

    class ManagedFragments:
        def _prepare_frag_download(self, context):
            contexts.append(context)
            return super()._prepare_frag_download(context)

    class ManagedHls(ManagedFragments, HlsFD):
        pass

    class ManagedDash(ManagedFragments, DashSegmentsFD):
        pass

    contexts = []

    info = {**fmt, 'id': 'reference', 'ext': fmt.get('ext') or 'mp4',
            'http_headers': headers or {}}
    protocol = fmt.get('protocol')
    if info.get('is_live') or info.get('has_drm'):
        raise ValueError('请使用可下载的非直播视频链接。')
    if protocol in {'m3u8', 'm3u8_native'}:
        # Preflight prevents HlsFD delegating network access to an external ffmpeg.
        with extractor.urlopen(info['url']) as response:
            manifest = response.read(5 * 1024 * 1024 + 1)
            info['url'] = response.url
        if len(manifest) > 5 * 1024 * 1024:
            raise ValueError('视频播放列表过大。')
        manifest = manifest.decode('utf-8-sig')
        if not manifest.lstrip().startswith('#EXTM3U') or '#EXT-X-ENDLIST' not in manifest:
            raise ValueError('该播放列表尚未结束，请使用完整视频链接。')
        if not HlsFD.can_download(manifest, info) or '#EXT-X-KEY:' in manifest:
            raise ValueError('此视频包含加密或特殊播放格式，请上传可读取的视频文件。')
        info['hls_media_playlist_data'] = manifest
        downloader = ManagedHls(extractor, extractor.params)
    elif protocol == 'http_dash_segments':
        if not isinstance(info.get('fragments'), list) or not info['fragments']:
            raise ValueError('视频缺少可读取的分段清单。')
        downloader = ManagedDash(extractor, extractor.params)
    else:
        raise ValueError('此视频播放格式暂时无法读取，请上传本地视频。')
    try:
        if not downloader.download(str(path), info)[0] or not Path(path).is_file():
            raise ValueError('视频分段下载未完成，请重试。')
    finally:
        # yt-dlp can raise before FragmentFD closes the output on a failed request.
        for context in contexts:
            stream = context.get('dest_stream')
            if stream:
                stream.close()
    return Path(path)
