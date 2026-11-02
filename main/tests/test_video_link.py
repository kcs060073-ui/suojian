import io
import json
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

from suo_jian.ui.video_link import validate_url,public_address,download_video,open_public


class Response(io.BytesIO):
    def __init__(self,data=b'video',status=200,headers=None):
        super().__init__(data);self.status=status;self.headers=headers or {}
    def getheader(self,key):return self.headers.get(key)


class VideoLinkTests(unittest.TestCase):
    def test_only_public_web_links_are_allowed(self):
        for url in ('file:///c:/a.mp4','ftp://host/a.mp4','http://user:pass@host/a.mp4','https://host:8788/a.mp4'):
            with self.assertRaises(ValueError):validate_url(url)
        self.assertEqual(validate_url(' https://example.com/a.mp4#x '),'https://example.com/a.mp4')
        for address in ['127.0.0.1','192.168.1.2','169.254.169.254','::1','224.0.0.1']:
            with patch('suo_jian.ui.video_link.socket.getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,0,'',(address,80))]):
                with self.assertRaises(ValueError):public_address('http://example.com/a.mp4')

    def test_connection_pins_checked_ip_and_tls_hostname(self):
        connection=Mock();sock=Mock();tls=Mock();context=Mock();context.wrap_socket.return_value=tls
        with patch('suo_jian.ui.video_link.public_address',return_value=('example.com',443,'93.184.216.34')),patch('suo_jian.ui.video_link.socket.create_connection',return_value=sock) as connect,patch('suo_jian.ui.video_link.ssl.create_default_context',return_value=context),patch('suo_jian.ui.video_link.http.client.HTTPConnection',return_value=connection):
            open_public('https://example.com/a.mp4',20)
        connect.assert_called_once_with(('93.184.216.34',443),timeout=20)
        context.wrap_socket.assert_called_once_with(sock,server_hostname='example.com')
        self.assertIs(connection.sock,tls)
        self.assertEqual(connection.auto_open,0)

    def test_webpage_oversized_stream_and_private_redirect_leave_no_upload(self):
        cases=[Response(headers={'Content-Type':'text/html'}),Response(b'x'*20),Response(status=302,headers={'Location':'http://127.0.0.1/private'})]
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'video.mp4'
            for response in cases:
                calls=0
                def open_url(url,timeout):
                    nonlocal calls
                    calls+=1
                    if calls>1:raise ValueError('private destination')
                    return Mock(),response
                with patch('suo_jian.ui.video_link.open_public',side_effect=open_url):
                    with self.assertRaises(ValueError):download_video('https://example.com/video',target,max_bytes=10)
                self.assertFalse(target.exists())
                self.assertEqual(list((target.parent/'.incoming').glob('*.part')),[])

    def test_real_video_download_is_probed_before_becoming_an_upload(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source.mp4';target=root/'download.mp4'
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=black:s=64x64:r=25:d=0.2','-c:v','libx264',str(source)],check=True,capture_output=True,timeout=30)
            data=source.read_bytes();progress=[]
            with patch('suo_jian.ui.video_link.open_public',return_value=(Mock(),Response(data,headers={'Content-Length':str(len(data)),'Content-Type':'video/mp4'}))):
                download_video('https://example.com/video',target,lambda a,b:progress.append((a,b)))
            self.assertEqual(target.read_bytes(),data)
            self.assertEqual(progress[-1],(len(data),len(data)))

    def test_bad_video_and_truncated_content_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'video.mp4'
            for response in [Response(b'not-video'),Response(b'short',headers={'Content-Length':'99'})]:
                with patch('suo_jian.ui.video_link.open_public',return_value=(Mock(),response)):
                    with self.assertRaises(ValueError):download_video('https://example.com/video',target)
                self.assertFalse(target.exists())

    def test_link_route_starts_same_analysis_worker_and_rejects_cross_site(self):
        from suo_jian.ui import app as module
        client=module.app.test_client()
        with patch.object(module,'_start_analysis') as start:
            result=client.post('/api/analyze/link',json={'url':'https://example.com/video.mp4'})
            self.assertEqual(result.status_code,200)
            self.assertEqual(start.call_args.kwargs['download_url'],'https://example.com/video.mp4')
            blocked=client.post('/api/analyze/link',json={'url':'https://example.com/video.mp4'},headers={'Origin':'https://other.example'})
            self.assertEqual(blocked.status_code,403)
            self.assertEqual(start.call_count,1)

if __name__=='__main__':unittest.main()
