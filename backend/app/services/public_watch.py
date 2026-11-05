"""Bounded unauthenticated public text retrieval. No browser, cookies or environment proxy."""
import http.client
import ipaddress
import re
import socket
import ssl
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit,urljoin,urlunsplit
from email.utils import parsedate_to_datetime
from datetime import datetime,timezone

LIMIT=1024*1024
class WatchFailure(Exception):
    def __init__(self,code,retry_after=None):self.code=code;self.retry_after=retry_after


def public_url(value):
    try:
        p=urlsplit(value.strip());port=p.port
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or len(value)>2048:raise ValueError
        if port not in (None,80 if p.scheme=='http' else 443):raise ValueError
        host=p.hostname.encode('idna').decode('ascii').lower()
        if host=='localhost' or host.endswith(('.localhost','.local','.internal')):raise ValueError
        try:
            if not ipaddress.ip_address(host).is_global:raise ValueError
        except ValueError:
            if ':' in host or host.replace('.','').isdigit():raise
        # Query text can be a credential; this first version accepts plain public page paths only.
        if p.query or re.search(r'[\x00-\x20\x7f]',value):raise ValueError
        authority='['+host+']' if ':' in host else host
        return urlunsplit((p.scheme,authority,p.path or '/','',''))
    except (ValueError,UnicodeError,AttributeError):raise WatchFailure('invalid_public_url') from None


class Text(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.parts=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript','template'):self.hidden+=1
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript','template'):self.hidden=max(0,self.hidden-1)
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)


def retrieve(url):
    """Each redirect is separately resolved, public checked and pinned to one checked IP."""
    deadline=time.monotonic()+22
    current=public_url(url)
    for redirect in range(4):
        p=urlsplit(current);port=443 if p.scheme=='https' else 80
        try:
            addresses=socket.getaddrinfo(p.hostname,port,type=socket.SOCK_STREAM)
            ips=list(dict.fromkeys(a[4][0] for a in addresses))
            if not ips or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast or ipaddress.ip_address(ip).is_reserved for ip in ips):raise WatchFailure('private_address')
            ip=ips[0]
            class Pinned(http.client.HTTPConnection):
                def connect(self):
                    self.sock=socket.create_connection((ip,port),timeout=max(.1,min(6,deadline-time.monotonic())))
                    if p.scheme=='https':self.sock=ssl.create_default_context().wrap_socket(self.sock,server_hostname=p.hostname)
            connection=Pinned(p.hostname,port,timeout=6)
            try:
                if time.monotonic()>=deadline:raise WatchFailure('timeout')
                connection.request('GET',p.path or '/',headers={'User-Agent':'GamjaBot-PublicWatch/1','Accept':'text/html,text/plain;q=0.9','Accept-Encoding':'identity','Connection':'close'})
                response=connection.getresponse()
                if response.status==429:
                    retry=response.getheader('Retry-After');delay=None
                    if retry:
                        try:delay=float(retry)
                        except ValueError:
                            try:delay=(parsedate_to_datetime(retry)-datetime.now(timezone.utc)).total_seconds()
                            except (ValueError,TypeError):pass
                    raise WatchFailure('http_429',max(300,min(delay,86400)) if delay is not None else None)
                if response.status in (301,302,303,307,308):
                    location=response.getheader('Location')
                    if redirect==3 or not location:raise WatchFailure('redirect_limit')
                    target=public_url(urljoin(current,location))
                    if p.scheme=='https' and urlsplit(target).scheme!='https':raise WatchFailure('insecure_redirect')
                    current=target;continue
                if response.status!=200:raise WatchFailure('http_'+str(response.status))
                if response.getheader('Content-Encoding','identity').lower() not in ('identity',''):raise WatchFailure('compressed_response')
                media=response.getheader('Content-Type','').split(';')[0].lower().strip()
                if media not in ('text/html','text/plain','application/xhtml+xml'):raise WatchFailure('unsupported_content')
                length=response.getheader('Content-Length')
                if length and int(length)>LIMIT:raise WatchFailure('page_too_large')
                chunks=[];size=0
                while True:
                    if time.monotonic()>=deadline:raise WatchFailure('timeout')
                    part=response.read(min(32768,LIMIT+1-size))
                    if not part:break
                    chunks.append(part);size+=len(part)
                    if size>LIMIT:raise WatchFailure('page_too_large')
                charset=response.headers.get_content_charset() or 'utf-8'
                try:text=b''.join(chunks).decode(charset,errors='replace')
                except LookupError:text=b''.join(chunks).decode('utf-8',errors='replace')
                if media!='text/plain':parser=Text();parser.feed(text);text=' '.join(parser.parts)
                return re.sub(r'\s+',' ',text).strip()
            finally:connection.close()
        except WatchFailure:raise
        except Exception:raise WatchFailure('request_failed') from None
    raise WatchFailure('redirect_limit')
