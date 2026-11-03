"""Read public script pages and local HTML uploads as text."""
import ipaddress
import re
import socket
import http.client
import ssl
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'nav', 'footer', 'header', 'noscript', 'svg'}:
            self.hidden += 1
        elif not self.hidden and tag in {'p', 'div', 'section', 'article', 'br', 'li', 'tr', 'h1', 'h2', 'h3'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'nav', 'footer', 'header', 'noscript', 'svg'} and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in {'p', 'div', 'section', 'article', 'li', 'tr', 'h1', 'h2', 'h3'}:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def extract_html(value):
    parser = TextExtractor()
    parser.feed(value)
    text = re.sub(r'\n{3,}', '\n\n', re.sub(r'[ \t]+', ' ', ''.join(parser.parts))).strip()
    if not text:
        raise ValueError('没有提取到正文，请检查链接权限或文件内容')
    if len(text) > 30000:
        raise ValueError('正文超过 30000 字，请精简脚本后再导入')
    return text


def _validate_url(url):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('请使用公开的 HTTP 或 HTTPS 链接')
    addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('只支持公网链接；本机文件请使用 HTML 文件导入')
    return url


def _public_socket(host, port, timeout):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('只支持公网链接')
    last = None
    for family, kind, proto, _, address in addresses:
        sock = socket.socket(family, kind, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(address)
            return sock
        except OSError as error:
            last = error
            sock.close()
    raise last or OSError('无法连接网页')


class PublicHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = _public_socket(self.host, self.port, self.timeout)


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        raw = _public_socket(self.host, self.port, self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(PublicHTTPConnection, req)


class PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(PublicHTTPSConnection, req, context=ssl.create_default_context())


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, _validate_url(urljoin(req.full_url, newurl)))


def fetch_script(url):
    url = _validate_url(url.strip())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), SafeRedirect(), PublicHTTPHandler(), PublicHTTPSHandler())
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 SuoJian/1.0'})
    with opener.open(request, timeout=15) as response:
        content_type = response.headers.get_content_type()
        if content_type not in ('text/html', 'text/plain', 'application/xhtml+xml'):
            raise ValueError('链接不是 HTML 网页或纯文本文件')
        raw = response.read(2*1024*1024 + 1)
        if len(raw) > 2*1024*1024:
            raise ValueError('网页超过 2 MB，请使用文件或粘贴正文')
        charset = response.headers.get_content_charset() or 'utf-8'
        content = raw.decode(charset, errors='replace')
    result = extract_html(content) if content_type != 'text/plain' else content.strip()
    if not result or len(result) > 30000:
        raise ValueError('网页正文为空或超过 30000 字')
    return {'script': result, 'source': response.geturl()}
