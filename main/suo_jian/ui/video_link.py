"""Bounded public video download; redirects and DNS addresses stay validated."""
import http.client
import ipaddress
import json
import re
import socket
import ssl
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit, urljoin, urlunsplit, urlencode

MAX_BYTES = 1024*1024*1024


class VideoPageLink(ValueError):
    """The URL is a webpage or manifest and needs extraction before downloading."""


_DNS_CACHE = {}


def real_platform_addresses(host):
    """Resolve proxy Fake-IP names to public IPs without relaxing private-IP checks."""
    cached=_DNS_CACHE.get(host)
    if cached and cached[0]>time.monotonic():return cached[1]
    for address,resolver,path in [('223.5.5.5','dns.alidns.com','/resolve'),('1.1.1.1','cloudflare-dns.com','/dns-query')]:
        connection=None;sock=None
        try:
            sock=socket.create_connection((address,443),timeout=8)
            sock=ssl.create_default_context().wrap_socket(sock,server_hostname=resolver)
            connection=http.client.HTTPConnection(resolver,443,timeout=8)
            connection.sock=sock;connection.auto_open=0
            connection.request('GET',path+'?'+urlencode({'name':host,'type':'A'}),headers={'Accept':'application/dns-json'})
            response=connection.getresponse();raw=response.read(65537)
            if response.status!=200 or len(raw)>65536:continue
            payload=json.loads(raw)
            if payload.get('Status')!=0:continue
            records=[row for row in payload.get('Answer',[]) if row.get('type')==1]
            ips=[str(ipaddress.ip_address(row['data'])) for row in records]
            if not ips or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast or ipaddress.ip_address(ip).is_reserved for ip in ips):continue
            ttl=min(300,max(1,min(int(row.get('TTL',30)) for row in records)))
            if len(_DNS_CACHE)>256:_DNS_CACHE.clear()
            _DNS_CACHE[host]=(time.monotonic()+ttl,ips)
            return ips
        except (OSError,ValueError,http.client.HTTPException):
            continue
        finally:
            if connection:connection.close()
            elif sock:sock.close()
    raise ValueError('本机代理的域名解析暂时不可用，请重试或上传本地视频。')


def validate_url(url):
    if not isinstance(url,str) or not url.strip() or len(url)>8192:
        raise ValueError("请粘贴完整的视频下载链接。")
    url=url.strip()
    try:
        parsed=urlsplit(url)
        port=parsed.port
    except ValueError as error:
        raise ValueError("视频链接格式不正确。") from error
    if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("请使用不含账号密码的 HTTP 或 HTTPS 视频链接。")
    if port not in {None,80,443}:
        raise ValueError("目前支持普通 HTTP／HTTPS 视频地址。")
    if any(ord(c)<32 for c in url) or "\\" in url:
        raise ValueError("视频链接含有无效字符。")
    return urlunsplit((parsed.scheme,parsed.netloc,parsed.path or "/",parsed.query,""))


def public_address(url):
    parsed=urlsplit(validate_url(url))
    host=parsed.hostname.encode("idna").decode("ascii")
    port=parsed.port or (443 if parsed.scheme=="https" else 80)
    try:
        addresses=socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)
    except OSError as error:
        raise ValueError("无法找到链接对应的网站，请检查链接。") from error
    if not addresses:
        raise ValueError("链接的网站地址不可用。")
    fake_network=ipaddress.ip_network('198.18.0.0/15')
    if all(ipaddress.ip_address(entry[4][0]) in fake_network for entry in addresses):
        ips=real_platform_addresses(host)
        addresses=[(socket.AF_INET,socket.SOCK_STREAM,0,'',(ip,port)) for ip in ips]
    for entry in addresses:
        ip=ipaddress.ip_address(entry[4][0])
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise ValueError("链接导入目前只支持公网视频地址，请将本地或内网视频拖入上传。")
    addresses.sort(key=lambda row:row[0]!=socket.AF_INET)
    return host,port,addresses[0][4][0]


def open_public(url,timeout,*,method="GET",body=None,headers=None):
    host,port,address=public_address(url)
    parsed=urlsplit(url)
    connection=http.client.HTTPConnection(host,port,timeout=timeout)
    sock=socket.create_connection((address,port),timeout=timeout)
    try:
        if parsed.scheme=="https":
            sock=ssl.create_default_context().wrap_socket(sock,server_hostname=host)
        connection.sock=sock
        # Disable implicit reconnect (which would resolve DNS a second time).
        connection.auto_open=0
        target=parsed.path or "/"
        if parsed.query:target+="?"+parsed.query
        connection.request(method,target,body=body,headers={"User-Agent":"SuoJian/1.0","Accept":"video/*,application/octet-stream",**(headers or {}),"Accept-Encoding":"identity"})
        return connection,connection.getresponse()
    except BaseException:
        sock.close();connection.close();raise


def download_video(url,destination,progress=None,*,max_bytes=MAX_BYTES,headers=None,media_kind="video"):
    url=validate_url(url)
    destination=Path(destination)
    staging=destination.parent/".incoming"/(destination.name+".part")
    staging.parent.mkdir(parents=True,exist_ok=True)
    deadline=time.monotonic()+600
    connection=None
    try:
        for _ in range(6):
            connection,response=open_public(url,20,**({"headers":headers} if headers else {}))
            if response.status in {301,302,303,307,308}:
                location=response.getheader("Location")
                connection.close();connection=None
                if not location:raise ValueError("视频链接跳转无效。")
                next_url=validate_url(urljoin(url,location))
                if urlsplit(next_url).netloc!=urlsplit(url).netloc and headers:
                    headers={k:v for k,v in headers.items() if k.lower() not in {'cookie','authorization','host'}}
                url=next_url
                continue
            break
        else:raise ValueError("链接跳转次数过多，请使用视频文件的下载地址。")
        if response.status in {401,403}:
            raise ValueError(f"网站拒绝了这次视频读取（HTTP {response.status}），可能需要浏览器验证或下载授权。")
        if response.status not in {200,206}:
            raise ValueError(f"视频下载未成功（网站返回 {response.status}），请检查链接是否过期。")
        if response.status==206:
            content_range=response.getheader('Content-Range') or ''
            match=re.fullmatch(r'bytes 0-(\d+)/(\d+)',content_range)
            if not match or int(match[1])+1!=int(match[2]):
                raise ValueError('网站只返回了部分视频，请使用完整下载地址。')
        media_type=(response.getheader("Content-Type") or "").split(";")[0].strip().lower()
        if media_type in {"text/html","application/xhtml+xml","application/vnd.apple.mpegurl","application/x-mpegurl"}:
            raise VideoPageLink("这是视频网页或播放列表，需要先读取视频地址。")
        length=response.getheader("Content-Length")
        try:total=int(length) if length is not None else None
        except ValueError:raise ValueError("网站返回了无效的视频文件大小。")
        if total is not None and (total<=0 or total>max_bytes):
            raise ValueError("视频大小超出限制，当前最多支持 1 GB。")
        size=0
        with staging.open("wb") as target:
            while True:
                if time.monotonic()>deadline:raise ValueError("视频下载超时，请重试或上传本地视频。")
                chunk=response.read(min(1024*1024,max_bytes-size+1))
                if not chunk:break
                size+=len(chunk)
                if size>max_bytes:raise ValueError("视频大小超出限制，当前最多支持 1 GB。")
                target.write(chunk)
                if progress:progress(size,total)
        if size==0 or (total is not None and size!=total):
            raise ValueError("视频下载不完整，请重新下载。")
        check=subprocess.run(["ffprobe","-v","error","-show_entries","stream=codec_type,width,height","-of","json",str(staging)],capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=45)
        if check.returncode!=0:
            raise ValueError("链接内容不是可读取的视频文件，请检查下载地址。")
        try:streams=json.loads(check.stdout).get("streams",[])
        except (ValueError,AttributeError):raise ValueError("无法识别该链接的视频内容。")
        if not any(row.get("codec_type")==media_kind and (media_kind=="audio" or (row.get("width",0)>0 and row.get("height",0)>0)) for row in streams):
            raise ValueError("该链接没有有效的视频画面，请使用视频文件地址。")
        staging.replace(destination)
        return destination
    except (OSError,http.client.HTTPException,subprocess.TimeoutExpired) as error:
        raise ValueError("视频下载或读取失败，请检查网络、链接有效期，或改为上传本地视频。") from error
    finally:
        if connection:connection.close()
        if staging.exists():staging.unlink()
