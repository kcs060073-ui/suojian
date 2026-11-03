"""Anonymous reader for the one public CUTROOM release repository."""
import hashlib,json,re,time,uuid
from pathlib import Path
from urllib.parse import urlsplit,urljoin
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.error import URLError,HTTPError
from product_update import UpdateError,version_key,safe_path,fail

REPOSITORY='qnwhageqlavs-jpg/cutroom-releases'
API='https://api.github.com/repos/'+REPOSITORY+'/releases?per_page=30'
BASE='https://github.com/'+REPOSITORY+'/releases/download/'
MAX_PACKAGE_BYTES=1024*1024*1024

def allowed_url(url,*,api=False):
    try:
        p=urlsplit(url);host=p.hostname or ''
        hosts=host=='api.github.com' if api else host=='github.com' or host.endswith('.githubusercontent.com')
        valid=p.scheme=='https' and hosts and not p.username and not p.password and p.port in (None,443) and not p.fragment
    except ValueError:valid=False
    if not valid:fail('DOWNLOAD_URL','更新地址未通过验证，已停止下载。')
    return url

class Redirects(HTTPRedirectHandler):
    max_redirections=5;max_repeats=1
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        allowed_url(newurl,api=req.full_url.startswith('https://api.github.com/'))
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def open_public(url,*,api=False):
    allowed_url(url,api=api)
    req=Request(url,headers={'User-Agent':'CUTROOM-Updater','Accept':'application/vnd.github+json' if api else 'application/octet-stream'})
    # No credential, cookie jar, GitHub login or publishing token is consulted.
    try:return build_opener(Redirects()).open(req,timeout=30)
    except (HTTPError,URLError,OSError) as e:
        if isinstance(e,HTTPError) and e.code==404:fail('NOT_PUBLISHED','公开更新尚未就绪，请稍后重试或选择本地安装包。')
        fail('NETWORK','暂时无法获取更新，请检查网络后重试，或选择已下载的本地安装包。')

def read_public_json(url,*,api=False):
    with open_public(url,api=api) as response:
        b=response.read(2*1024*1024+1)
    if len(b)>2*1024*1024:fail('METADATA','更新信息过大，已停止。')
    try:return json.loads(b)
    except ValueError:fail('METADATA','更新信息格式不正确，请稍后重试。')

def choose_release(releases,current):
    if not isinstance(releases,list):fail('METADATA','公开版本列表无效。')
    result=[];current_key=version_key(current)
    for r in releases:
        if not isinstance(r,dict) or r.get('draft') is not False:continue
        tag=r.get('tag_name','')
        if not isinstance(tag,str) or not re.fullmatch(r'v\d+\.\d+\.\d+(?:-beta\.\d+)?',tag):continue
        if r.get('prerelease') is True and '-beta.' not in tag:continue
        if version_key(tag[1:])<=current_key:continue
        assets=r.get('assets',[])
        matches=[a for a in assets if isinstance(a,dict) and a.get('name')=='cutroom-update.json'] if isinstance(assets,list) else []
        if len(matches)!=1:continue
        if matches[0].get('browser_download_url')!=BASE+tag+'/cutroom-update.json':continue
        result.append(r)
    return max(result,key=lambda r:version_key(r['tag_name'][1:])) if result else None

def validate_update_manifest(m,version):
    if not isinstance(m,dict) or m.get('format')!=1 or m.get('product')!='CUTROOM' or m.get('platform')!='windows-x64' or m.get('version')!=version:
        fail('METADATA','更新清单与产品或版本不一致。')
    p=m.get('package');name='CUTROOM-v'+version+'-windows-x64.zip'
    if (not isinstance(p,dict) or p.get('name')!=name or p.get('url')!=BASE+'v'+version+'/'+name
        or not re.fullmatch('[0-9a-f]{64}',str(p.get('sha256',''))) or type(p.get('bytes'))!=int or not 0<p['bytes']<=MAX_PACKAGE_BYTES):
        fail('METADATA','安装包地址、大小或校验信息无效。')
    return m

def download_update(root,current):
    release=choose_release(read_public_json(API,api=True),current)
    if release is None:return None
    version=release['tag_name'][1:]
    m=validate_update_manifest(read_public_json(BASE+'v'+version+'/cutroom-update.json'),version)
    package=m['package'];folder=safe_path(Path(root),'runtime/software-updates/downloads');folder.mkdir(parents=True,exist_ok=True)
    target=folder/(uuid.uuid4().hex+'-'+package['name'])
    count=0;sha=hashlib.sha256();start=time.monotonic()
    print('发现新版 '+version+'，正在下载并校验…',flush=True)
    try:
        with open_public(package['url']) as r,target.open('xb') as f:
            while True:
                b=r.read(1024*1024)
                if not b:break
                count+=len(b)
                if count>package['bytes'] or time.monotonic()-start>600:fail('DOWNLOAD','下载超时或大小不符，请重试。')
                sha.update(b);f.write(b)
        if count!=package['bytes'] or sha.hexdigest()!=package['sha256']:fail('CHECKSUM','下载校验失败，请重试；原程序未改动。')
    except BaseException:
        target.unlink(missing_ok=True);raise
    return target
