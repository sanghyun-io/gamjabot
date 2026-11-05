"""Owner-requested public template retrieval without proxy, redirects, or credentials."""
import http.client
import ipaddress
import json
import queue
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID, RFC_4122
from fastapi import HTTPException
from pydantic import ValidationError
from app.schemas.bot_template_portable import portable_bytes, TemplateTooLarge

MAX_RESPONSE=512*1024
TIMEOUT=15


def fail(code):
    raise HTTPException(422,{'code':'bot_template_link_'+code}) from None


def _global(value):
    try:
        address=ipaddress.ip_address(value)
        return bool(address.is_global and not address.is_multicast and not address.is_reserved
                    and not (getattr(address,'ipv4_mapped',None) and not address.ipv4_mapped.is_global))
    except ValueError:
        return False


def _url(value):
    try:
        if (not isinstance(value,str) or not 1<=len(value)<=2048 or not value.isprintable()
                or any(c.isspace() for c in value) or '?' in value or '#' in value or '\\' in value):
            raise ValueError
        parsed=urlsplit(value)
        if (parsed.scheme!='https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None,443)):
            raise ValueError
        host=parsed.hostname.encode('idna').decode('ascii').lower()
        if (host.endswith('.') or '%' in host or host=='localhost'
                or host.endswith(('.localhost','.local','.internal'))): raise ValueError
        try:
            address=ipaddress.ip_address(host)
            if not _global(host): raise ValueError
            authority='['+host+']' if address.version==6 else host
        except ValueError:
            # An IP literal that fails the public-address check must never become a DNS name.
            if ':' in host or not re.fullmatch(r'[a-z0-9.-]+',host): raise
            labels=host.split('.')
            if (len(host)>253 or len(labels)<2 or any(not label or len(label)>63
                    or label[0]=='-' or label[-1]=='-' for label in labels)):
                raise ValueError
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError
            authority=host
        match=re.fullmatch(r'/t/([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})(?:/snapshot)?',parsed.path)
        if not match: raise ValueError
        identifier=UUID(match.group(1))
        if identifier.version!=4 or identifier.variant!=RFC_4122: raise ValueError
        path='/t/'+str(identifier)
        return host,'https://'+authority+path,path+'/snapshot'
    except (ValueError,TypeError,UnicodeError,AttributeError):
        fail('invalid')


def _resolve(host,deadline):
    result=queue.Queue(maxsize=1)
    def worker():
        try: result.put(socket.getaddrinfo(host,443,type=socket.SOCK_STREAM))
        except Exception: result.put(None)
    threading.Thread(target=worker,daemon=True).start()
    try: records=result.get(timeout=max(0.01,deadline-time.monotonic()))
    except queue.Empty: fail('unavailable')
    if not records: fail('unavailable')
    addresses=[]
    for family,kind,proto,_,address in records:
        if family not in (socket.AF_INET,socket.AF_INET6) or not _global(address[0]): fail('invalid')
        addresses.append((family,kind,proto,address))
    return addresses


def _pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('duplicate')
        result[key]=value
    return result


def _bad_constant(_): raise ValueError('invalid number')


def retrieve(url):
    host,source,path=_url(url)
    deadline=time.monotonic()+TIMEOUT
    addresses=_resolve(host,deadline)
    family,kind,proto,address=addresses[0]
    class Pinned(http.client.HTTPSConnection):
        def connect(self):
            raw=socket.socket(family,kind,proto)
            self.sock=raw
            self.transport=raw
            raw.settimeout(max(.01,deadline-time.monotonic()))
            raw.connect(address)
            remaining=deadline-time.monotonic()
            if remaining<=0: fail('unavailable')
            wrapped=ssl.create_default_context().wrap_socket(raw,server_hostname=host,do_handshake_on_connect=False)
            self.sock=wrapped
            self.transport=wrapped
            remaining=deadline-time.monotonic()
            if remaining<=0: fail('unavailable')
            wrapped.settimeout(remaining)
            wrapped.do_handshake()
    connection=Pinned(host,443,timeout=TIMEOUT)
    def expire():
        sock=getattr(connection,'transport',None)
        if sock:
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            try: sock.close()
            except OSError: pass
    timer=threading.Timer(max(.01,deadline-time.monotonic()),expire);timer.daemon=True;timer.start()
    try:
        connection.request('GET',path,headers={'Accept':'application/json','Accept-Encoding':'identity',
            'User-Agent':'GamjaBot-TemplatePreview/1','Connection':'close'})
        response=connection.getresponse()
        if time.monotonic()>=deadline: fail('unavailable')
        if response.status!=200: fail('unavailable')
        if response.getheader('Content-Encoding','identity').strip().lower() not in ('','identity'): fail('invalid_template')
        if response.getheader('Content-Type','').split(';',1)[0].strip().lower()!='application/json': fail('invalid_template')
        length=response.getheader('Content-Length')
        if length is not None and (not length.isascii() or not length.isdecimal()): fail('invalid_template')
        if length is not None and int(length)>MAX_RESPONSE: fail('too_large')
        chunks=[];total=0
        while True:
            if time.monotonic()>=deadline: fail('unavailable')
            part=response.read1(min(32768,MAX_RESPONSE+1-total))
            if not part:break
            total+=len(part)
            if total>MAX_RESPONSE: fail('too_large')
            chunks.append(part)
        if length is not None and total!=int(length): fail('invalid_template')
        try:
            value=json.loads(b''.join(chunks).decode('utf-8'),object_pairs_hook=_pairs,parse_constant=_bad_constant)
            template,_,sha=portable_bytes(value)
        except TemplateTooLarge: fail('too_large')
        except (ValidationError,ValueError,TypeError,UnicodeError,RecursionError): fail('invalid_template')
        return {'version':1,'source_url':source,'template':template,'template_sha256':sha}
    except HTTPException: raise
    except Exception: fail('unavailable')
    finally:
        timer.cancel();connection.close()
