"""Public share-page extraction; no browser cookies or account credentials."""
import gzip
import io
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
import zlib
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit,urljoin,parse_qs,parse_qsl,urlencode

from .video_link import validate_url,open_public,download_video,MAX_BYTES,VideoPageLink
from .segmented_video import BoundedStream, DownloadBudget, download_segments
from .platform_pages import haokan_info, ixigua_mobile_info, assert_requested_identity

PLATFORMS={"douyin.com":"抖音","iesdouyin.com":"抖音","xiaohongshu.com":"小红书","xhslink.com":"小红书","bilibili.com":"哔哩哔哩","b23.tv":"哔哩哔哩","xinpianchang.com":"新片场",
           "news.qq.com":"腾讯新闻","inews.qq.com":"腾讯新闻","v.qq.com":"腾讯视频",
           "rednote.com":"小红书","ixigua.com":"西瓜视频","haokan.baidu.com":"好看视频",
           "cctv.com":"央视网","cctv.cn":"央视网","cntv.cn":"央视网","cntv.com":"央视网"}
META_LIMIT=5*1024*1024
VERIFIED_PAGES=Path(__file__).resolve().parents[2]/'work'/'verified-video-pages'
BILIBILI_VIDEO_PATH=re.compile(r'/(?:(?P<locale>zh-Hans|zh-Hant|en)/)?video/(?P<video_id>BV[A-Za-z0-9]{10}|av[0-9]{1,20})/?')


def bilibili_info(url,extractor,stage=None):
    """Preserve localized pages and reuse only fresh, explicitly observed media."""
    url=canonical_url(url)
    parsed=urlsplit(url)
    match=BILIBILI_VIDEO_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError('请使用 B 站单条视频的 BV／AV 链接，不支持主页、合集或直播。')
    video_id=match['video_id']
    values=parse_qs(parsed.query).get('p',['1'])
    if len(values)!=1 or not re.fullmatch(r'[1-9][0-9]{0,3}',values[0]):
        raise ValueError('B 站分 P 编号无效，请使用具体视频页的链接。')
    part=int(values[0])
    cache=VERIFIED_PAGES/f'bilibili-{video_id}-p{part}.json'
    if cache.is_file() and cache.stat().st_size<=65536:
        try:
            hint=json.loads(cache.read_text(encoding='utf-8'))
            streams=hint['streams']
            age=time.time()-float(hint['verified_at'])
            if (hint['bvid']==video_id and hint['part']==part and 0<=age<1800
                    and hint.get('basis')=='browser_observed_anonymous_playback'
                    and isinstance(streams,list) and len(streams)==2
                    and {s['kind'] for s in streams}=={'video','audio'}):
                formats=[]
                for stream in streams:
                    media_url=validate_url(stream['url'])
                    host=urlsplit(media_url).hostname or ''
                    if not re.fullmatch(r'upos[-a-z0-9]*\.(?:bilivideo\.com|akamaized\.net)',host):
                        raise ValueError('播放地址不属于 B 站公开媒体域名')
                    deadline=parse_qs(urlsplit(media_url).query).get('deadline',[None])[0]
                    if deadline and (not deadline.isdigit() or int(deadline)<=time.time()):
                        raise ValueError('浏览器播放地址已过期')
                    formats.append({'url':media_url,'protocol':'https','ext':'mp4' if stream['kind']=='video' else 'm4a',
                                    'vcodec':'unknown' if stream['kind']=='video' else 'none',
                                    'http_headers':{'Referer':url}})
                if stage:stage('已取得浏览器播放地址，正在下载音视频')
                return {'id':video_id,'title':hint.get('title') or video_id,'duration':hint.get('duration'),
                        'webpage_url':url,'requested_formats':formats,'source':'browser_observed'}
        except (OSError,ValueError,TypeError,KeyError):
            pass
    if match['locale']:
        from yt_dlp.extractor.bilibili import BiliBiliIE
        class LocalizedBiliBiliIE(BiliBiliIE):
            _VALID_URL=r'https?://(?:www\.)?bilibili\.com/(?:zh-Hans|zh-Hant|en)/video/(?P<prefix>[aAbB][vV])(?P<id>[^/?#&]+)'
        extractor.add_info_extractor(LocalizedBiliBiliIE(extractor))
        return extractor.extract_info(url,download=False,ie_key=LocalizedBiliBiliIE.ie_key())
    return extractor.extract_info(url,download=False)


def tencent_news_info(url, extractor, stage=None):
    """Resolve the public news article metadata before extracting its video."""
    match=re.fullmatch(r'/(?:rain/)?a/([0-9]{8}[A-Za-z0-9]{6,16})(?:\.html)?/?',urlsplit(url).path)
    if not match:
        raise ValueError('请使用腾讯新闻的单条视频详情链接，不支持频道或新闻列表。')
    from urllib.parse import urlencode
    endpoint='https://i.news.qq.com/getWebVideo?'+urlencode({'id':match[1],'appver':'29_android_7.6.10'})
    with extractor.urlopen(endpoint) as response:
        raw=response.read(META_LIMIT+1)
    if len(raw)>META_LIMIT:
        raise ValueError('腾讯新闻视频信息过大，无法读取。')
    try:payload=json.loads(raw)
    except (ValueError,UnicodeError) as error:
        raise ValueError('腾讯新闻视频信息暂时无法读取，请稍后重试。') from error
    if not isinstance(payload,dict) or payload.get('ret') not in (0,'0'):
        raise ValueError('腾讯新闻未返回该条内容的视频信息，内容可能已下线或暂不可访问。')
    channel=payload.get('video_channel')
    video=channel.get('video') if isinstance(channel,dict) else None
    vid=video.get('vid') if isinstance(video,dict) else None
    if not isinstance(vid,str) or not re.fullmatch(r'[A-Za-z0-9]{11}',vid):
        raise ValueError('该腾讯新闻页面未返回单条视频，请确认分享的是视频详情页。')
    if stage:stage('已找到原片，正在读取播放地址')
    info=extractor.extract_info('https://v.qq.com/x/page/'+vid+'.html',download=False)
    if isinstance(info,dict):
        info['title']=payload.get('title') or info.get('title')
        info['webpage_url']=url
    return info


def verified_xpc_info(url,extractor):
    """Use public media identifiers recorded after an interactive page verification."""
    match=re.fullmatch(r'/a(\d+)/?',urlsplit(url).path)
    if not match:return None
    path=VERIFIED_PAGES/('xpc-'+match[1]+'.json')
    if not path.is_file():return None
    try:
        hint=json.loads(path.read_text(encoding='utf-8'))
        if hint['article_id']!=match[1] or time.time()-hint['verified_at']>86400:return None
        if not re.fullmatch(r'[A-Za-z0-9]{8,64}',hint['media_id']):return None
        if not re.fullmatch(r'[A-Za-z0-9]{8,64}',hint['app_key']):return None
    except (ValueError,KeyError,TypeError):return None
    endpoint='https://mod-api.xinpianchang.com/mod/api/v2/media/'+hint['media_id']+'?appKey='+hint['app_key']
    with extractor.urlopen(endpoint) as response:data=json.loads(response.read())['data']
    formats=[{'url':f['url'],'ext':'mp4','width':f.get('width'),'height':f.get('height'),
              'filesize':f.get('filesize'),'http_headers':{'Range':'bytes=0-','Referer':url}}
             for f in data.get('resource',{}).get('progressive',[]) if f.get('url')]
    if not formats:return None
    return extractor.process_ie_result({'id':match[1],'title':data.get('title') or hint.get('title') or match[1],
                                         'formats':formats},download=False)


def extract_input_url(text):
    if not isinstance(text,str) or len(text)>16384:
        raise ValueError("请粘贴一个视频链接，或包含链接的分享文案。")
    text=text.replace('\\&','&').replace('&amp;','&')
    urls=[m.rstrip('。，，！!？?；;）)】]”\"\'') for m in re.findall(r'https?://[^\s<>\[\]()]+',text)]
    urls=list(dict.fromkeys(urls))
    if len(urls)!=1:
        raise ValueError("请每次粘贴一个视频分享链接。")
    return validate_url(urls[0])


def platform_name(url):
    host=(urlsplit(url).hostname or "").lower()
    return next((name for domain,name in PLATFORMS.items() if host==domain or host.endswith('.'+domain)),None)


def canonical_url(url):
    parsed=urlsplit(url);host=parsed.hostname
    if host and (host.endswith('.douyin.com') or host=='douyin.com'):
        modal=parse_qs(parsed.query).get('modal_id',[None])[0]
        if modal and re.fullmatch(r'\d{10,24}',modal):
            parsed=parsed._replace(netloc='www.douyin.com',path='/video/'+modal,query='')
    if host and (host.endswith('.xiaohongshu.com') or host=='xiaohongshu.com'):
        parsed=parsed._replace(netloc='www.xiaohongshu.com')
    elif host and (host.endswith('.bilibili.com') or host=='bilibili.com'):
        parsed=parsed._replace(netloc='www.bilibili.com')
        if BILIBILI_VIDEO_PATH.fullmatch(parsed.path):
            tracking={'trackid','spm_id_from','vd_source','share_source','share_medium','share_plat','share_session_id'}
            parsed=parsed._replace(query=urlencode([(k,v) for k,v in parse_qsl(parsed.query,keep_blank_values=True) if k not in tracking]))
    elif host and (host.endswith('.iesdouyin.com') or host=='iesdouyin.com'):
        match=re.search(r'/share/video/(\d+)',parsed.path)
        if match:parsed=parsed._replace(netloc='www.douyin.com',path='/video/'+match[1])
    elif host=='haokan.baidu.com' and parsed.path=='/v':
        parsed=parsed._replace(query=urlencode([(k,v) for k,v in parse_qsl(parsed.query) if k=='vid']))
    return urlunsplit(parsed)


class QuietLogger:
    def debug(self,*args,**kwargs):pass
    def info(self,*args,**kwargs):pass
    def warning(self,*args,**kwargs):pass
    def error(self,*args,**kwargs):pass


def make_extractor(budget=None):
    try:
        import yt_dlp
        from yt_dlp.networking import Request,Response
        from yt_dlp.networking.exceptions import HTTPError,TransportError
    except ImportError as error:
        raise ValueError("分享页解析组件未安装，请先使用本地视频上传。") from error

    class PublicYoutubeDL(yt_dlp.YoutubeDL):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs);self.deadline=time.monotonic()+120;self.network_error=None
            self.access_status=None;self.browser_verification=False;self.login_required=False
            if budget:self.deadline=budget.deadline

        def urlopen(self,request):
            if isinstance(request,str):request=Request(request)
            if isinstance(request,urllib.request.Request):
                request=Request(request.full_url,data=request.data,headers=dict(request.header_items()),method=request.get_method())
            url=validate_url(request.url)
            method=request.method or ('POST' if request.data else 'GET')
            body=request.data
            headers={**self.params.get('http_headers',{}),**dict(request.headers)}
            for _ in range(6):
                parsed=urlsplit(url)
                if platform_name(url)=='小红书' and re.match(r'^/(?:login|signin)(?:/|$)',parsed.path):
                    self.login_required=True
                    raise TransportError('Public page redirected to login')
                if budget:budget.check()
                if time.monotonic()>self.deadline:raise TransportError('Share-page extraction timed out')
                cookie_request=urllib.request.Request(url,headers=headers,method=method,data=body)
                self.cookiejar.add_cookie_header(cookie_request)
                try:connection,response=open_public(url,15,method=method,body=body,headers=dict(cookie_request.header_items()))
                except (OSError,ValueError) as error:
                    self.network_error=str(error)
                    raise TransportError('Public video website unavailable') from error
                try:
                    if budget and response.status in {200,206}:
                        wrapped=Response(BoundedStream(connection,response,budget),url,
                                         dict(response.getheaders()),status=response.status)
                        self.cookiejar.extract_cookies(wrapped,cookie_request)
                        connection=None
                        return wrapped
                    raw=response.read(META_LIMIT+1)
                    if len(raw)>META_LIMIT:raise TransportError('Share-page response too large')
                    response_headers=dict(response.getheaders())
                    wrapped=Response(io.BytesIO(raw),url,response_headers,status=response.status)
                    wrapped.headers=response.headers
                    self.cookiejar.extract_cookies(wrapped,cookie_request)
                    if response.status in {301,302,303,307,308}:
                        location=response.getheader('Location')
                        if not location:raise HTTPError(wrapped)
                        next_url=validate_url(urljoin(url,location))
                        if urlsplit(next_url).netloc!=urlsplit(url).netloc:
                            headers={k:v for k,v in headers.items() if k.lower() not in {'cookie','authorization','host'}}
                        if response.status==303 or response.status in {301,302} and method=='POST':method='GET';body=None
                        url=next_url
                        continue
                    encoding=(response.getheader('Content-Encoding') or '').lower()
                    if encoding=='gzip':
                        raw=gzip.GzipFile(fileobj=io.BytesIO(raw)).read(META_LIMIT+1)
                    elif encoding=='deflate':
                        raw=zlib.decompressobj().decompress(raw,META_LIMIT+1)
                    if len(raw)>META_LIMIT:raise TransportError('Share-page response too large')
                    if b'_jsc_ch_conf' in raw.lower() or b'challenge-platform' in raw.lower():
                        self.browser_verification=True
                        raise TransportError('Public page requires browser verification')
                    response_headers={k:v for k,v in response_headers.items() if k.lower() not in {'content-encoding','content-length'}}
                    wrapped=Response(io.BytesIO(raw),url,response_headers,status=response.status)
                    if response.status>=400:
                        self.access_status=response.status
                        self.browser_verification=any(marker in raw.lower() for marker in
                            (b'_jsc_ch_conf',b'captcha',b'challenge-platform'))
                        raise HTTPError(wrapped)
                    return wrapped
                finally:
                    if connection:connection.close()
            raise TransportError('Too many share-page redirects')

    return PublicYoutubeDL({'quiet':True,'no_warnings':True,'logger':QuietLogger(),'noplaylist':True,'playlist_items':'1',
        'format':'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best','skip_download':True,
        'socket_timeout':15,'retries':1,'fragment_retries':1,'skip_unavailable_fragments':False,
        'concurrent_fragment_downloads':1,'continuedl':False,'hls_prefer_native':True,
        'extractor_retries':1,'cachedir':False,'proxy':'','js_runtimes':{},'remote_components':[]})


def extract_platform_info(url,stage=None):
    name=platform_name(url) or '视频页面'
    try:
        with make_extractor() as extractor:
            if name=='新片场':
                verified=verified_xpc_info(url,extractor)
                if verified:return verified
            host=urlsplit(url).hostname
            if host in {'v.douyin.com','b23.tv','xhslink.com'} or (host and host.endswith('.xhslink.com')):
                response=extractor.urlopen(url)
                url=response.url;response.close()
            if name!='视频页面' and platform_name(url)!=name:raise ValueError("分享链接没有跳转到对应的视频平台。")
            if name=='腾讯新闻':info=tencent_news_info(url,extractor,stage)
            elif name=='哔哩哔哩':info=bilibili_info(url,extractor,stage)
            elif name=='好看视频':info=haokan_info(canonical_url(url),extractor)
            elif name=='西瓜视频' and host=='m.ixigua.com':info=ixigua_mobile_info(url,extractor)
            else:info=extractor.extract_info(canonical_url(url),download=False)
            assert_requested_identity(canonical_url(url),info)
            # Generic HLS URLs are often marked live before the media playlist is read.
            if isinstance(info,dict) and info.get('is_live') and info.get('extractor_key')=='Generic':
                selected=info.get('requested_formats') or [info]
                if all(f.get('protocol') in {'m3u8','m3u8_native'} for f in selected):
                    for fmt in selected:
                        with extractor.urlopen(fmt['url']) as playlist:
                            if b'#EXT-X-ENDLIST' not in playlist.read(META_LIMIT+1):
                                break
                    else:
                        info['is_live']=False
                        for fmt in selected:fmt['is_live']=False
    except ValueError:
        raise
    except Exception as error:
        if 'extractor' in locals() and getattr(extractor,'login_required',False) is True:
            raise ValueError(name+'将该链接跳转到了登录页，匿名读取无法取得原片。请使用可公开播放的分享链接，或上传本地视频。') from error
        if 'extractor' in locals() and extractor.network_error:
            raise ValueError("本机网络未能访问"+name+"："+extractor.network_error) from error
        if 'extractor' in locals() and extractor.browser_verification:
            raise ValueError(name+'返回了浏览器安全验证页。请在浏览器打开原链接完成验证；也可将网站下载的视频拖入。') from error
        if 'extractor' in locals() and extractor.access_status in {401,403}:
            raise ValueError(name+'拒绝了自动读取请求（HTTP '+str(extractor.access_status)+'）。网页可下载时，可先下载视频后拖入。') from error
        if 'extractor' in locals() and extractor.access_status in {412,429}:
            raise ValueError(name+'限制了这次自动读取（HTTP '+str(extractor.access_status)+'）。需要正常浏览器播放后读取，或上传已下载的视频。') from error
        if 'fresh cookies' in str(error).lower():
            raise ValueError(name+"要求有效的浏览器会话，当前匿名导入未成功。本工具暂不接入浏览器登录状态；请将视频下载到本机后拖入。") from error
        if 'unsupported url' in str(error).lower():
            raise ValueError('暂未适配此网页的视频读取，请使用原视频平台的单条作品链接，或上传视频文件。') from error
        raise ValueError(name+"未能解析出可下载的视频地址。请使用原平台的单条视频链接，或上传视频文件。") from error
    if not isinstance(info,dict) or info.get('entries') is not None or info.get('is_live') or info.get('_type') in {'playlist','multi_video'}:
        raise ValueError("目前只支持单个非直播视频，请粘贴具体视频的分享链接。")
    return info


def import_video_link(url,destination,progress=None,stage=None):
    url=extract_input_url(url)
    name=platform_name(url)
    if not name and urlsplit(url).path.lower().endswith(('.mp4','.mov','.mkv','.webm')):
        return download_video(url,destination,progress)
    if not name and not urlsplit(url).path.lower().endswith(('.m3u8','.mpd')):
        try:return download_video(url,destination,progress)
        except VideoPageLink:
            pass
    if stage:stage("正在读取"+(name or '视频链接'))
    info=extract_platform_info(url,stage=stage)
    formats=info.get('requested_formats') or [info]
    if not formats or len(formats)>2 or any(not x.get('url') for x in formats):
        raise ValueError("分享内容没有可下载的视频，请确认不是图文笔记、直播或合集。")
    if any(x.get('has_drm') for x in formats):
        raise ValueError('该视频使用受保护的播放格式，无法直接导入。')
    if any(x.get('protocol') not in {None,'http','https','m3u8','m3u8_native','http_dash_segments'} for x in formats):
        raise ValueError("此视频播放格式暂时无法读取，请上传本地视频。")
    destination=Path(destination)
    if stage:stage("链接已读取，正在下载视频")
    segmented=any(x.get('protocol') in {'m3u8','m3u8_native','http_dash_segments'} for x in formats)
    if len(formats)==1 and not segmented:
        return download_video(formats[0]['url'],destination,progress,headers=formats[0].get('http_headers') or info.get('http_headers'))
    incoming=destination.parent/'.incoming'
    incoming.mkdir(parents=True,exist_ok=True)
    temporary=tempfile.TemporaryDirectory(prefix=destination.stem+'-',dir=incoming)
    work=Path(temporary.name)
    files=[];combined=work/'combined.mp4';spent=0
    try:
        for index,fmt in enumerate(formats):
            kind='audio' if fmt.get('vcodec')=='none' else 'video'
            path=work/(f'track-{index}'+('.m4a' if kind=='audio' else '.mp4'))
            def track_progress(size,total):
                if progress:progress(spent+size,None)
            headers=fmt.get('http_headers') or info.get('http_headers')
            if fmt.get('protocol') in {'m3u8','m3u8_native','http_dash_segments'}:
                if stage:stage('正在下载视频分段（'+str(index+1)+' / '+str(len(formats))+' 轨）')
                budget=DownloadBudget(MAX_BYTES-spent,track_progress)
                try:
                    with make_extractor(budget) as extractor:
                        extractor.params['http_headers']={**extractor.params.get('http_headers',{}),**(headers or {})}
                        download_segments(extractor,fmt,path,headers)
                except Exception as error:
                    raise ValueError(budget.error or '视频分段读取失败，请检查链接是否过期或需要登录。') from error
                # Include manifest bytes and retry traffic in the shared download limit.
                downloaded_size=max(budget.size,path.stat().st_size)
            else:
                download_video(fmt['url'],path,track_progress,max_bytes=MAX_BYTES-spent,
                               headers=headers,media_kind=kind)
                downloaded_size=path.stat().st_size
            files.append((path,kind));spent+=downloaded_size
        video=next((p for p,k in files if k=='video'),None);audio=next((p for p,k in files if k=='audio'),None)
        if not video or (len(formats)>1 and not audio):raise ValueError("该视频的音画轨道不完整，请改为上传本地视频。")
        if stage:stage("视频已下载，正在合并音画轨道")
        inputs=['-protocol_whitelist','file,pipe','-i',str(video)]
        mapping=['-map','0:v:0','-map','0:a?']
        if audio:
            inputs+=['-protocol_whitelist','file,pipe','-i',str(audio)]
            mapping=['-map','0:v:0','-map','1:a:0']
        subprocess.run(['ffmpeg','-v','error','-y',*inputs,*mapping,'-c','copy','-shortest','-movflags','+faststart',str(combined)],capture_output=True,check=True,timeout=120)
        if combined.stat().st_size>MAX_BYTES:raise ValueError("合并后视频超过 1 GB，请上传较小的视频。")
        probe=subprocess.run(['ffprobe','-v','error','-protocol_whitelist','file,pipe','-show_entries','stream=codec_type,width,height','-of','json',str(combined)],capture_output=True,text=True,encoding='utf-8',check=True,timeout=45)
        streams=json.loads(probe.stdout).get('streams',[])
        if not any(s.get('codec_type')=='video' and s.get('width',0)>0 and s.get('height',0)>0 for s in streams):
            raise ValueError('下载结果没有可读取的视频画面。')
        combined.replace(destination)
        return destination
    except (subprocess.CalledProcessError,subprocess.TimeoutExpired,OSError) as error:
        raise ValueError("分享视频音画合并失败，请改为上传本地视频。") from error
    finally:
        temporary.cleanup()
