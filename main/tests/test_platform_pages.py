import io
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from suo_jian.ui.platform_pages import (
    haokan_info, ixigua_mobile_info, assert_requested_identity, page_state,
)
from suo_jian.ui.platform_video import canonical_url, extract_platform_info, import_video_link, platform_name
from tests.test_segmented_video import PublicResponse


def extractor_with_state(variable, value):
    extractor = Mock()
    extractor.urlopen.return_value = io.BytesIO(
        ('<script>window.' + variable + '=' + json.dumps(value) + ';</script>').encode())
    return extractor


class PlatformPagesTests(unittest.TestCase):
    def test_offline_haokan_does_not_import_recommendation_video(self):
        state = {'openAppInfo': {'pagepd': {'pagepd': 'web_hkoffline'}},
                 'curVideoRelate': [{'id': '222222', 'playurl': 'https://cdn.example/other.mp4'}]}
        with self.assertRaisesRegex(ValueError, '推荐内容'):
            haokan_info('https://haokan.baidu.com/v?vid=111111',
                        extractor_with_state('__PRELOADED_STATE__', state))

    def test_haokan_requires_exact_original_identity(self):
        for state in ({'curVideoRelate': [{'id': '111111', 'playurl': 'https://cdn.example/other.mp4'}]},
                      {'curVideoMeta': {'id': '222222', 'playurl': 'https://cdn.example/other.mp4'}}):
            with self.assertRaisesRegex(ValueError, '编号一致'):
                haokan_info('https://haokan.baidu.com/v?vid=111111',
                            extractor_with_state('__PRELOADED_STATE__', state))

    def test_matching_primary_haokan_media_is_preserved(self):
        state = {'curVideoMeta': {'vid': '111111', 'title': '原片',
                                 'playurl': 'https://cdn.example/original.mp4?signature=keep'}}
        info = haokan_info('https://haokan.baidu.com/v?vid=111111',
                           extractor_with_state('__PRELOADED_STATE__', state))
        self.assertEqual(info['url'], state['curVideoMeta']['playurl'])

    def test_haokan_duplicate_id_or_channel_is_rejected_before_network(self):
        extractor = Mock()
        for url in ('https://haokan.baidu.com/', 'https://haokan.baidu.com/v?vid=111111&vid=222222'):
            with self.assertRaisesRegex(ValueError, '单条视频'):
                haokan_info(url, extractor)
        extractor.urlopen.assert_not_called()

    def test_ixigua_encrypted_value_is_not_guessed_as_a_media_url(self):
        state = {'data': {'storeState': {'detail': {'videoData': {'result': {
            'gid': '7415111334255133193', 'url': 'encrypted-private-value'}}}}}}
        with self.assertRaisesRegex(ValueError, '未提供可直接读取'):
            ixigua_mobile_info('https://m.ixigua.com/video/7415111334255133193',
                               extractor_with_state('_SSR_DATA', state))

    def test_ixigua_mismatch_is_rejected(self):
        state = {'data': {'storeState': {'detail': {'videoData': {'result': {
            'gid': '111111', 'url': 'https://cdn.example/wrong.mp4'}}}}}}
        with self.assertRaisesRegex(ValueError, '编号一致'):
            ixigua_mobile_info('https://m.ixigua.com/video/7415111334255133193',
                               extractor_with_state('_SSR_DATA', state))

    def test_page_media_still_passes_through_public_download_guard(self):
        state = {'data': {'storeState': {'detail': {'videoData': {'result': {
            'gid': '7415111334255133193', 'url': 'http://127.0.0.1/private.mp4'}}}}}}
        info = ixigua_mobile_info('https://m.ixigua.com/video/7415111334255133193',
                                  extractor_with_state('_SSR_DATA', state))
        local = [(socket.AF_INET, socket.SOCK_STREAM, 0, '', ('127.0.0.1', 80))]
        with tempfile.TemporaryDirectory() as folder, \
                patch('suo_jian.ui.platform_video.extract_platform_info', return_value=info), \
                patch('suo_jian.ui.video_link.socket.getaddrinfo', return_value=local), \
                patch('suo_jian.ui.video_link.http.client.HTTPConnection') as connection:
            with self.assertRaisesRegex(ValueError, '公网视频地址'):
                import_video_link('https://m.ixigua.com/video/7415111334255133193', Path(folder)/'out.mp4')
            connection.assert_not_called()

    def test_page_data_is_json_only_and_bounded(self):
        extractor = Mock()
        for body in (b'window.__PRELOADED_STATE__=dangerousFunction()', b'x' * (5*1024*1024 + 1)):
            extractor.urlopen.return_value = io.BytesIO(body)
            with self.assertRaises(ValueError):
                page_state(extractor, 'https://haokan.baidu.com/v?vid=111111', '__PRELOADED_STATE__')

    def test_douyin_redirected_recommendation_is_rejected(self):
        url = canonical_url('https://www.douyin.com/jingxuan?modal_id=7686819974069128483')
        with self.assertRaisesRegex(ValueError, '编号与原链接不一致'):
            assert_requested_identity(url, {'id': '7695138598315691305'})
        assert_requested_identity(url, {'id': '7686819974069128483'})

    def test_rednote_login_redirect_is_distinct_from_unsupported_page(self):
        redirects = [PublicResponse(b'', status=302), PublicResponse(b'login')]
        redirects[0].headers['Location'] = 'https://www.rednote.com/login?redirectPath=original'
        with patch('suo_jian.ui.platform_video.open_public', side_effect=[(Mock(), r) for r in redirects]):
            with self.assertRaisesRegex(ValueError, '跳转到了登录页'):
                extract_platform_info('https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9')

    def test_success_status_challenge_is_still_reported_as_verification(self):
        response = PublicResponse(b'<script>window._jsc_ch_conf={}</script>')
        with patch('suo_jian.ui.platform_video.open_public', return_value=(Mock(), response)):
            with self.assertRaisesRegex(ValueError, '浏览器安全验证'):
                extract_platform_info('https://www.douyin.com/video/7686819974069128483')

    def test_new_domains_are_exact_and_xhs_access_token_is_preserved(self):
        self.assertEqual(platform_name('https://www.rednote.com/login'), '小红书')
        self.assertEqual(platform_name('https://m.ixigua.com/video/7415111334255133193'), '西瓜视频')
        self.assertEqual(platform_name('https://tv.cctv.com/test.shtml'), '央视网')
        self.assertIsNone(platform_name('https://haokan.baidu.com.evil.example/v?vid=111111'))
        url = 'https://xiaohongshu.com/explore/id?xsec_token=abc%2B123&xsec_source=pc_feed'
        self.assertEqual(canonical_url(url), url.replace('://xiaohongshu', '://www.xiaohongshu'))
        self.assertEqual(canonical_url('https://haokan.baidu.com/v?pd=tracking&vid=111111'),
                         'https://haokan.baidu.com/v?vid=111111')


if __name__ == '__main__':
    unittest.main()
