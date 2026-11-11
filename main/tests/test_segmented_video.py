import io
import json
import subprocess
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

from suo_jian.ui.platform_video import import_video_link
from suo_jian.ui.segmented_video import BoundedStream, DownloadBudget


class PublicResponse(io.BytesIO):
    def __init__(self, data, status=200, content_type='application/octet-stream'):
        super().__init__(data)
        self.status = status
        self.headers = Message()
        self.headers['Content-Length'] = str(len(data))
        self.headers['Content-Type'] = content_type

    def getheader(self, key):
        return self.headers.get(key)

    def getheaders(self):
        return list(self.headers.items())


class SegmentedVideoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.run([
            'ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'color=c=blue:s=64x64:r=25:d=0.8', '-f', 'lavfi', '-i',
            'sine=frequency=440:duration=0.8', '-c:v', 'libx264', '-g', '10',
            '-c:a', 'aac', '-f', 'hls', '-hls_time', '0.4', '-hls_list_size', '0',
            str(self.root / 'index.m3u8')], check=True, capture_output=True, timeout=30)
        self.info = {'url': 'https://cdn.example/index.m3u8',
                     'protocol': 'm3u8_native', 'ext': 'mp4', 'vcodec': 'avc1'}

    def open_url(self, url, timeout, **kwargs):
        path = self.root / url.rsplit('/', 1)[-1]
        return Mock(), PublicResponse(path.read_bytes())

    def test_hls_download_reads_segments_and_remuxes_video_and_audio(self):
        target = self.root / 'result.mp4'
        progress = []
        with patch('suo_jian.ui.platform_video.open_public', side_effect=self.open_url):
            import_video_link('https://cdn.example/index.m3u8', target,
                              lambda size, total: progress.append(size))
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                'stream=codec_type', '-of', 'json', str(target)],
                               capture_output=True, text=True, check=True)
        self.assertEqual({s['codec_type'] for s in json.loads(probe.stdout)['streams']},
                         {'video', 'audio'})
        self.assertTrue(progress)
        self.assertEqual(progress, sorted(progress))
        self.assertEqual(list((self.root / '.incoming').iterdir()), [])

    def test_dash_fragments_use_native_reader_and_produce_readable_video(self):
        manifest = self.root / 'index.mpd'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'color=c=blue:s=64x64:r=25:d=0.8', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=0.8', '-c:v', 'libx264', '-c:a', 'aac',
                        '-f', 'dash', str(manifest)],
                       cwd=self.root, check=True, capture_output=True, timeout=30)
        target = self.root / 'dash.mp4'
        with patch('suo_jian.ui.platform_video.open_public', side_effect=self.open_url):
            import_video_link('https://cdn.example/index.mpd', target)
        self.assertTrue(target.is_file())
        self.assertGreater(target.stat().st_size, 0)

    def test_encrypted_hls_never_delegates_network_access_to_ffmpeg(self):
        manifest = self.root / 'index.m3u8'
        manifest.write_text('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXTINF:1,\nindex0.ts\n#EXT-X-ENDLIST\n')
        with patch('suo_jian.ui.platform_video.extract_platform_info', return_value=self.info), \
                patch('suo_jian.ui.platform_video.open_public', side_effect=self.open_url), \
                patch('yt_dlp.downloader.external.FFmpegFD.real_download') as external:
            with self.assertRaises(ValueError):
                import_video_link('https://cdn.example/index.m3u8', self.root / 'encrypted.mp4')
        external.assert_not_called()

    def test_webpage_video_is_extracted_through_normal_import_entry(self):
        source = self.root / 'source.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(self.root / 'index.m3u8'),
                        '-c', 'copy', str(source)], check=True, capture_output=True, timeout=30)
        page = b'<html><head><title>Reference</title></head><body><video src="https://cdn.example/source.mp4" controls></video></body></html>'
        def open_page(url, timeout, **kwargs):
            if url == 'https://example.com/watch':
                return Mock(), PublicResponse(page, content_type='text/html')
            return Mock(), PublicResponse(source.read_bytes(), content_type='video/mp4')
        target = self.root / 'embedded.mp4'
        with patch('suo_jian.ui.video_link.open_public', side_effect=open_page), \
                patch('suo_jian.ui.platform_video.open_public', side_effect=open_page):
            import_video_link('https://example.com/watch', target)
        self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_failed_or_private_segment_preserves_existing_file_and_cleans_staging(self):
        target = self.root / 'result.mp4'
        target.write_bytes(b'previous upload')
        manifest = self.root / 'index.m3u8'
        manifest.write_text('#EXTM3U\n#EXTINF:1,\nhttp://127.0.0.1/private.ts\n#EXT-X-ENDLIST\n')
        def guarded_open(url, timeout, **kwargs):
            if '127.0.0.1' in url:
                raise ValueError('Private address rejected')
            return self.open_url(url, timeout, **kwargs)
        with patch('suo_jian.ui.platform_video.extract_platform_info', return_value=self.info), \
                patch('suo_jian.ui.platform_video.open_public', side_effect=guarded_open):
            with self.assertRaises(ValueError):
                import_video_link('https://cdn.example/index.m3u8', target)
        self.assertEqual(target.read_bytes(), b'previous upload')
        self.assertEqual(list((self.root / '.incoming').iterdir()), [])

    def test_byte_limit_counts_all_segments_and_retries(self):
        budget = DownloadBudget(5)
        first = BoundedStream(Mock(), PublicResponse(b'abc'), budget)
        self.assertEqual(first.read(), b'abc')
        second = BoundedStream(Mock(), PublicResponse(b'def'), budget)
        with self.assertRaises(ValueError):
            second.read()
        with self.assertRaises(ValueError):
            first.read()


if __name__ == '__main__':
    unittest.main()
