import unittest
from unittest.mock import patch

from workbench.script_import import extract_html, _validate_url, fetch_script


class ScriptImportTests(unittest.TestCase):
    def test_html_preserves_script_and_skips_active_content(self):
        text = extract_html('<h1>剧本</h1><script>evil()</script><p>第一镜</p><p>口播内容</p>')
        self.assertIn('剧本', text)
        self.assertIn('第一镜', text)
        self.assertNotIn('evil()', text)

    def test_rejects_local_and_private_hosts(self):
        for url in ('http://127.0.0.1:8788/', 'file:///etc/passwd', 'http://user:pass@example.com'):
            with self.assertRaises(ValueError):
                _validate_url(url)
        with patch('workbench.script_import.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('192.168.1.1', 80))]):
            with self.assertRaises(ValueError):
                fetch_script('http://example.com')

    def test_rejects_empty_and_oversize(self):
        with self.assertRaises(ValueError):
            extract_html('<script>only()</script>')
        with self.assertRaises(ValueError):
            extract_html('<p>' + '字' * 30001 + '</p>')
