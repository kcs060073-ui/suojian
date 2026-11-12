import json
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from suo_jian.ui.platform_video import extract_input_url,canonical_url,platform_name,import_video_link,tencent_news_info,extract_platform_info,bilibili_info
from suo_jian.ui.video_link import public_address
from tests.test_segmented_video import PublicResponse


class PlatformVideoTests(unittest.TestCase):
    def test_bilibili_localized_path_keeps_page_and_part_but_removes_tracking(self):
        url='https://www.bilibili.com/zh-Hans/video/BV11bxF6CEvi/?trackid=abc&spm_id_from=xyz&p=2&t=15'
        clean=canonical_url(url)
        self.assertEqual(clean,'https://www.bilibili.com/zh-Hans/video/BV11bxF6CEvi/?p=2&t=15')
        self.assertEqual(canonical_url(clean),clean)

    def test_shared_markdown_escaped_ampersand_is_one_link(self):
        url='https://www.bilibili.com/zh-Hans/video/BV11bxF6CEvi/?trackid=abc&spm_id_from=xyz'
        self.assertEqual(extract_input_url('['+url+']('+url.replace('&','\\&')+')'),url)
        self.assertEqual(extract_input_url(url.replace('&','&amp;')),url)

    def test_localized_bilibili_page_uses_specific_extractor(self):
        with tempfile.TemporaryDirectory() as folder,patch('suo_jian.ui.platform_video.VERIFIED_PAGES',Path(folder)):
            extractor=Mock()
            bilibili_info('https://www.bilibili.com/zh-Hant/video/BV11bxF6CEvi/?p=2',extractor)
            self.assertEqual(extractor.extract_info.call_args.kwargs['ie_key'],'LocalizedBiliBili')
            self.assertIn('/zh-Hant/video/',extractor.extract_info.call_args.args[0])

    def test_fresh_browser_media_is_bound_to_video_and_part(self):
        with tempfile.TemporaryDirectory() as folder,patch('suo_jian.ui.platform_video.VERIFIED_PAGES',Path(folder)):
            hint={'bvid':'BV11bxF6CEvi','part':1,'verified_at':time.time(),
                  'basis':'browser_observed_anonymous_playback','streams':[
                      {'kind':'video','url':'https://upos-test.bilivideo.com/video.m4s'},
                      {'kind':'audio','url':'https://upos-test.akamaized.net/audio.m4s'}]}
            path=Path(folder)/'bilibili-BV11bxF6CEvi-p1.json'
            path.write_text(json.dumps(hint))
            extractor=Mock()
            info=bilibili_info('https://www.bilibili.com/zh-Hans/video/BV11bxF6CEvi/',extractor)
            self.assertEqual(info['source'],'browser_observed')
            self.assertEqual(len(info['requested_formats']),2)
            extractor.extract_info.assert_not_called()
            bilibili_info('https://www.bilibili.com/video/BV11bxF6CEvi/?p=2',extractor)
            extractor.extract_info.assert_called_once()

    def test_expired_or_untrusted_bilibili_media_is_not_reused(self):
        with tempfile.TemporaryDirectory() as folder,patch('suo_jian.ui.platform_video.VERIFIED_PAGES',Path(folder)):
            hint={'bvid':'BV11bxF6CEvi','part':1,'verified_at':time.time(),
                  'basis':'browser_observed_anonymous_playback','streams':[
                      {'kind':'video','url':'https://upos-test.bilivideo.com/video.m4s'},
                      {'kind':'audio','url':'https://upos-test.bilivideo.com/audio.m4s'}]}
            path=Path(folder)/'bilibili-BV11bxF6CEvi-p1.json'
            for age,media in ((1900,'https://upos-test.bilivideo.com/video.m4s'),
                              (0,'http://127.0.0.1/private'),
                              (0,'https://upos-test.bilivideo.com/video.m4s?deadline=1')):
                hint['verified_at']=time.time()-age;hint['streams'][0]['url']=media
                path.write_text(json.dumps(hint))
                extractor=Mock()
                bilibili_info('https://www.bilibili.com/video/BV11bxF6CEvi/',extractor)
                extractor.extract_info.assert_called_once()

    def test_bilibili_access_restriction_is_distinct_from_unsupported_url(self):
        with tempfile.TemporaryDirectory() as folder,patch('suo_jian.ui.platform_video.VERIFIED_PAGES',Path(folder)), \
                patch('suo_jian.ui.platform_video.make_extractor') as factory:
            extractor=factory.return_value.__enter__.return_value
            extractor.network_error=None;extractor.browser_verification=False;extractor.access_status=412
            extractor.extract_info.side_effect=RuntimeError('HTTP Error 412')
            with self.assertRaisesRegex(ValueError,'限制了这次自动读取.*412'):
                extract_platform_info('https://www.bilibili.com/video/BV11bxF6CEvi/')

    def test_tencent_news_resolves_article_id_before_loading_the_original_video(self):
        payload={'ret':0,'title':'参考新闻','video_channel':{'video':{'vid':'e3374iyaumm'}}}
        extractor=Mock()
        extractor.urlopen.return_value=PublicResponse(json.dumps(payload).encode())
        extractor.extract_info.return_value={'id':'e3374iyaumm','url':'https://cdn.example/video.mp4'}
        stage=Mock()
        url='https://news.qq.com/rain/a/20261112V03WGJ00?adChannelId=news'
        info=tencent_news_info(url,extractor,stage)
        self.assertIn('getWebVideo?id=20261112V03WGJ00',extractor.urlopen.call_args.args[0])
        extractor.extract_info.assert_called_once_with('https://v.qq.com/x/page/e3374iyaumm.html',download=False)
        self.assertEqual(info['title'],'参考新闻')
        self.assertEqual(info['webpage_url'],url)
        stage.assert_called_once()

    def test_tencent_news_missing_media_does_not_claim_login_is_required(self):
        for payload in ({'ret':0,'video_channel':None},{'ret':0,'video_channel':{'video':{'vid':'../../bad'}}}):
            extractor=Mock()
            extractor.urlopen.return_value=PublicResponse(json.dumps(payload).encode())
            with self.assertRaisesRegex(ValueError,'未返回单条视频'):
                tencent_news_info('https://view.inews.qq.com/a/20261112V03WGJ00',extractor)
            extractor.extract_info.assert_not_called()

    def test_tencent_channel_or_spoofed_host_cannot_use_article_api(self):
        self.assertEqual(platform_name('https://news.qq.com/rain/a/20261112V03WGJ00'),'腾讯新闻')
        self.assertIsNone(platform_name('https://news.qq.com.evil.example/rain/a/20261112V03WGJ00'))
        extractor=Mock()
        with self.assertRaisesRegex(ValueError,'单条视频详情'):
            tencent_news_info('https://news.qq.com/',extractor)
        extractor.urlopen.assert_not_called()

    def test_unsupported_page_has_a_specific_error_instead_of_guessing_login(self):
        with patch('suo_jian.ui.platform_video.make_extractor') as factory:
            extractor=factory.return_value.__enter__.return_value
            extractor.network_error=None
            extractor.browser_verification=False
            extractor.access_status=None
            extractor.extract_info.side_effect=RuntimeError('Unsupported URL: example')
            with self.assertRaisesRegex(ValueError,'暂未适配'):
                extract_platform_info('https://example.com/article')

    def test_drm_selected_format_is_not_downloaded(self):
        with tempfile.TemporaryDirectory() as folder,patch('suo_jian.ui.platform_video.extract_platform_info',return_value={
                'url':'https://cdn.example/video.mp4','has_drm':True}),patch('suo_jian.ui.platform_video.download_video') as download:
            with self.assertRaisesRegex(ValueError,'受保护'):
                import_video_link('https://news.qq.com/rain/a/20261112V03WGJ00',Path(folder)/'video.mp4')
            download.assert_not_called()

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
