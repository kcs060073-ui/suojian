import json
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from suo_jian.ui.platform_video import extract_input_url,canonical_url,platform_name,import_video_link
from suo_jian.ui.video_link import public_address
from tests.test_segmented_video import PublicResponse


class PlatformVideoTests(unittest.TestCase):
    def test_xinpianchang_verification_is_reported_without_claiming_login_required(self):
        with tempfile.TemporaryDirectory() as folder,patch('suo_jian.ui.platform_video.VERIFIED_PAGES',Path(folder)),patch('suo_jian.ui.platform_video.open_public',return_value=(Mock(),PublicResponse(b'<script>window._jsc_ch_conf={}</script>',status=403))):
            with self.assertRaisesRegex(ValueError,'浏览器安全验证'):
                import_video_link('https://www.xinpianchang.com/a123456',Path(folder)/'video.mp4')

    def test_verified_page_identifiers_resolve_only_returned_public_media_urls(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'xpc-123456.json').write_text(json.dumps({'article_id':'123456','media_id':'abcdefgh1234','app_key':'0123456789abcdef','verified_at':time.time()}))
            payload={'data':{'title':'public reference','resource':{'progressive':[{'url':'','height':2160},{'url':'https://cdn.example/public.mp4','width':1920,'height':1080}]}}}
            response=PublicResponse(json.dumps(payload).encode())
            with patch('suo_jian.ui.platform_video.VERIFIED_PAGES',root),patch('suo_jian.ui.platform_video.open_public',return_value=(Mock(),response)),patch('suo_jian.ui.platform_video.download_video') as download:
                import_video_link('https://www.xinpianchang.com/a123456',root/'result.mp4')
            self.assertEqual(download.call_args.args[0],'https://cdn.example/public.mp4')
            self.assertEqual(download.call_args.kwargs['headers']['Range'],'bytes=0-')

    def test_share_text_markdown_and_modal_id_normalize_to_one_video(self):
        url='https://www.douyin.com/jingxuan?modal_id=7686432847778982833'
        self.assertEqual(extract_input_url('['+url+']('+url+')'),url)
        self.assertEqual(canonical_url(url),'https://www.douyin.com/video/7686432847778982833')
        self.assertEqual(extract_input_url('来看看这条视频 https://v.douyin.com/ABC/ 复制链接打开抖音'),'https://v.douyin.com/ABC/')
        with self.assertRaises(ValueError):extract_input_url('https://a.example/1 https://b.example/2')

    def test_platform_domain_boundary_and_legacy_page_mapping(self):
        self.assertEqual(platform_name('https://b23.tv/a'),'哔哩哔哩')
        self.assertIsNone(platform_name('https://www.douyin.com.evil.example/a'))
        self.assertEqual(canonical_url('https://www.iesdouyin.com/share/video/12345'),'https://www.douyin.com/video/12345')

    def test_proxy_dns_uses_validated_public_address_without_allowing_local_ip(self):
        fake=[(socket.AF_INET,socket.SOCK_STREAM,0,'',('198.18.0.191',443))]
        with patch('suo_jian.ui.video_link.socket.getaddrinfo',return_value=fake),patch('suo_jian.ui.video_link.real_platform_addresses',return_value=['93.184.216.34']):
            self.assertEqual(public_address('https://www.douyin.com/')[2],'93.184.216.34')
            self.assertEqual(public_address('https://cdn.example/video.mp4')[2],'93.184.216.34')
        private=[(socket.AF_INET,socket.SOCK_STREAM,0,'',('127.0.0.1',443))]
        with patch('suo_jian.ui.video_link.socket.getaddrinfo',return_value=private):
            with self.assertRaises(ValueError):public_address('https://www.douyin.com/')

    def test_direct_video_and_platform_progressive_media_use_bounded_downloader(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'video.mp4'
            with patch('suo_jian.ui.platform_video.download_video',return_value=target) as download:
                import_video_link('https://example.com/file.mp4',target)
                self.assertEqual(download.call_args.args[0],'https://example.com/file.mp4')
            with patch('suo_jian.ui.platform_video.extract_platform_info',return_value={'url':'https://cdn.example/video.mp4','protocol':'https'}),patch('suo_jian.ui.platform_video.download_video',return_value=target) as download:
                import_video_link('https://www.douyin.com/video/12345',target)
                self.assertEqual(download.call_args.args[0],'https://cdn.example/video.mp4')

    def test_two_tracks_merge_to_real_video_and_audio_streams(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);video=root/'source.mp4';audio=root/'audio.m4a';target=root/'merged.mp4'
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=black:s=64x64:r=25:d=0.4','-c:v','libx264',str(video)],check=True,capture_output=True,timeout=30)
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=0.4','-c:a','aac',str(audio)],check=True,capture_output=True,timeout=30)
            info={'requested_formats':[{'url':'https://cdn.example/video','protocol':'https','vcodec':'avc1'}, {'url':'https://cdn.example/audio','protocol':'https','vcodec':'none'}]}
            def download(url,path,*args,**kwargs):path.write_bytes((audio if kwargs.get('media_kind')=='audio' else video).read_bytes());return path
            with patch('suo_jian.ui.platform_video.extract_platform_info',return_value=info),patch('suo_jian.ui.platform_video.download_video',side_effect=download):import_video_link('https://www.bilibili.com/video/BV1test',target)
            probe=subprocess.run(['ffprobe','-v','error','-show_entries','stream=codec_type','-of','json',str(target)],capture_output=True,text=True,check=True)
            self.assertEqual({x['codec_type'] for x in json.loads(probe.stdout)['streams']},{'video','audio'})

if __name__=='__main__':unittest.main()
