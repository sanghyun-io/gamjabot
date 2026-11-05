"""Personal mobile gateway. A separate loopback listener; no desktop capability leaves it.

Tailscale Serve terminates private HTTPS. Devices pair through a short-lived code
created by the logged-in desktop Owner. Only named conversation/once-consent/Stop
routes are forwarded in-process to the unchanged Core API, with its guards intact.
AI provider credentials and app passwords are not mobile credentials.
"""
import asyncio
from contextlib import contextmanager
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import threading
import time
from urllib.parse import urlsplit, quote
from uuid import UUID

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from app.models.models import User

COOKIE='__Host-gamja-mobile'
PORT=8432
DAY=86400

def digest(value):return hashlib.sha256(value.encode()).hexdigest()
def origin_value(value):
    try:
        u=urlsplit(value)
        if (u.scheme!='https' or not u.hostname or not u.hostname.endswith('.ts.net')
                or not re.fullmatch(r'[a-z0-9.-]+',u.hostname) or u.port not in (None,443,8443)
                or u.username or u.password or u.path not in ('','/') or u.query or u.fragment):raise ValueError()
        return 'https://'+u.netloc.lower()
    except (ValueError,TypeError):raise HTTPException(422,'Use the HTTPS Tailscale device name, optionally port8443.') from None

class MobileDownload(Response):
    def __init__(self,content,semaphore,disposition,check):
        super().__init__(content,media_type='application/octet-stream',headers={'Content-Disposition':disposition})
        self.semaphore=semaphore;self.check=check
    async def __call__(self,scope,receive,send):
        try:
            try:self.check()
            except HTTPException as error:
                return await JSONResponse({'detail':error.detail},status_code=error.status_code)(scope,receive,send)
            await super().__call__(scope,receive,send)
        finally:self.semaphore.release()

class GatewayServer(uvicorn.Server):
    def install_signal_handlers(self):pass
    @contextmanager
    def capture_signals(self):yield

class MobileBinding:
    def __init__(self,manager,device_id,epoch):self.manager,self.device_id,self.epoch=manager,device_id,epoch

class MobileAccess:
    def __init__(self,root,app,*,port=PORT):
        self.root=Path(root);self.app=app;self.port=port;self.lock=threading.RLock();self.server=None;self.task=None;self.sock=None;self.pending=None;self.library_download=asyncio.Semaphore(1);self.library_scope_uncertain=False
        self.root.mkdir(mode=0o700,exist_ok=True);self._private(self.root,True)
        self.path=self.root/'state.json';self.state={'version':1,'enabled':False,'owner':None,'origin':'','devices':{}}
        if self.path.exists():
            self._private(self.path)
            if self.path.stat().st_size>1000000:raise ValueError('mobile_state_size')
            self.state=json.loads(self.path.read_text())
            if self.state.get('version')!=1 or not isinstance(self.state.get('devices'),dict):raise ValueError('mobile_state')
            if self.state.get('origin'):origin_value(self.state['origin'])
        self.web=self._web()
    def _private(self,p,directory=False):
        s=p.lstat()
        if p.is_symlink() or s.st_uid!=os.geteuid() or s.st_mode&0o077 or not (stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode)):raise ValueError('mobile_permissions')
    def _save(self,*,durable=False):
        self._private(self.root,True)
        if self.path.exists():self._private(self.path)
        temp=self.root/('.write-'+secrets.token_hex(16))
        try:
            fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w') as f:json.dump(self.state,f,separators=(',',':'));f.flush();os.fsync(f.fileno())
            os.replace(temp,self.path)
            if durable:
                directory=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                try:os.fsync(directory)
                finally:os.close(directory)
        finally:temp.unlink(missing_ok=True)
    def _epoch(self,owner):
        provider=getattr(self.app.state,'owner_epoch_provider',None);current=provider.snapshot() if provider else None
        if current is None or current.owner_id!=owner:raise HTTPException(401,'Mac에서 감자봇에 로그인하세요.')
        return current.owner_epoch
    def status(self,owner):
        with self.lock:
            owned=self.state['owner']==owner
            return {'enabled':bool(owned and self.state['enabled']),'listening':bool(owned and self.task and not self.task.done()),'origin':self.state['origin'] if owned else '',
                    'loopback_port':self.port,'devices':[{'id':key,'name':d['name'],'expires_at':d['expires'],'last_seen_at':d.get('last_seen'),'library_read':d.get('library_read') is True,'library_revision':d.get('library_revision',0)} for key,d in self.state['devices'].items() if d['owner']==owner and d['expires']>time.time()],
                    'owner_epoch':self._epoch(owner),'recommended':'Tailscale Serve (private tailnet HTTPS); never Funnel','app_login':'local Owner/device pairing','ai_provider_auth':'separate and never shared with mobile'}
    async def configure(self,owner,enabled,origin):
        self._epoch(owner)
        value=origin_value(origin) if enabled else self.state['origin']
        await self.close()
        with self.lock:
            previous=copy.deepcopy(self.state)
            if self.state['owner']!=owner or self.state['origin']!=value:self.state['devices']={}
            self.pending=None;self.state.update(owner=owner,enabled=enabled,origin=value);self._save()
        try:
            if enabled:await self.start()
            self._epoch(owner)
        except BaseException:
            await self.close()
            with self.lock:self.state=previous;self.state['enabled']=False;self._save()
            raise
        return self.status(owner)
    def pairing(self,owner):
        epoch=self._epoch(owner)
        with self.lock:
            if not self.state['enabled'] or self.state['owner']!=owner or not self.task or self.task.done():raise HTTPException(409,'먼저 모바일 연결을 켜세요.')
            code=''.join(secrets.choice('0123456789') for _ in range(8));expires=time.time()+300
            self.pending={'hash':digest(code),'owner':owner,'epoch':epoch,'expires':expires,'attempts':0}
            return {'code':code,'expires_at':expires,'url':self.state['origin']+'/','instructions':'같은 Tailscale 네트워크의 휴대폰에서 주소를 열고 코드를 입력하세요. 이 코드는 앱 비밀번호나 AI 제공업체 인증이 아닙니다.'}
    def revoke(self,owner,device_id):
        with self.lock:
            d=self.state['devices'].get(device_id)
            if not d or d['owner']!=owner:raise HTTPException(404,'Device not found')
            del self.state['devices'][device_id];self._save()
    def library_scope_command(self,owner,device_id,body,*,resolve=False):
        if (not isinstance(body,dict) or set(body)!={'operation_id','enabled','expected_revision','expected_owner_epoch'}
                or type(body['enabled']) is not bool or type(body['expected_revision']) is not int
                or not 0<=body['expected_revision']<9223372036854775807
                or not isinstance(body['expected_owner_epoch'],str) or not re.fullmatch('[a-f0-9]{64}',body['expected_owner_epoch'])):
            raise HTTPException(422,{'code':'mobile_library_invalid'})
        try:
            if not isinstance(body['operation_id'],str) or str(UUID(body['operation_id']))!=body['operation_id']:raise ValueError()
        except (ValueError,TypeError):raise HTTPException(422,{'code':'mobile_library_invalid'}) from None
        request_digest=digest(json.dumps(body,sort_keys=True,separators=(',',':')))
        with self.app.state.owner_recovery_lock:
            current=self._epoch(owner)
            if not resolve and current!=body['expected_owner_epoch']:raise HTTPException(409,{'code':'mobile_library_owner_changed'})
            with self.lock:
                if self.library_scope_uncertain:raise HTTPException(503,{'code':'mobile_library_unavailable'})
                d=self.state['devices'].get(device_id)
                if not d or d['owner']!=owner or d['expires']<=time.time() or self.state['owner']!=owner:raise HTTPException(404,{'code':'mobile_device_unavailable'})
                previous=copy.deepcopy(self.state);ops=d.setdefault('library_ops',{})
                if not isinstance(ops,dict):raise HTTPException(503,{'code':'mobile_library_unavailable'})
                prior=ops.get(body['operation_id'])
                if prior:
                    if prior.get('digest')!=request_digest:raise HTTPException(409,{'code':'mobile_library_operation_conflict'})
                    result=prior['result']
                else:
                    if len(ops)>=256:raise HTTPException(503,{'code':'mobile_library_capacity'})
                    revision=d.get('library_revision',0);enabled=d.get('library_read') is True
                    if type(revision) is not int or revision<0:raise HTTPException(503,{'code':'mobile_library_unavailable'})
                    if resolve:state='fenced';code='mobile_library_not_applied'
                    elif body['expected_revision']!=revision:state='rejected';code='mobile_library_revision_changed'
                    else:
                        state='applied';code=None
                        if enabled!=body['enabled']:
                            revision+=1;enabled=body['enabled'];d['library_revision']=revision;d['library_read']=enabled
                    result={'state':state,'code':code,'library_read':enabled,'library_revision':revision,'device_id':device_id,**body}
                    ops[body['operation_id']]={'digest':request_digest,'result':result}
                    try:
                        if self._epoch(owner)!=current:raise HTTPException(409,{'code':'mobile_library_owner_changed'})
                        if len(json.dumps(self.state,separators=(',',':')).encode())>900000:raise HTTPException(503,{'code':'mobile_library_capacity'})
                    except BaseException:self.state=previous;raise
                    try:
                        self._save(durable=True)
                    except BaseException:
                        # replace may already have happened; do not invent a rollback.
                        self.library_scope_uncertain=True
                        raise
                return result
    def library_check(self,key,owner,epoch,revision=None):
        with self.lock:
            if self.library_scope_uncertain:raise HTTPException(503,'Library access unavailable')
            d=self.state['devices'].get(key)
            if (not self.state['enabled'] or not d or d['owner']!=owner or self.state['owner']!=owner
                    or d['expires']<=time.time() or self._epoch(owner)!=epoch):raise HTTPException(401,'Mobile session changed')
            if d.get('library_read') is not True:raise HTTPException(403,'Library access disabled')
            actual=d.get('library_revision',0)
            if type(actual) is not int or actual<1 or (revision is not None and actual!=revision):raise HTTPException(409,'Library access changed')
            return actual
    def pair(self,code,name):
        with self.lock:
            p=self.pending
            if not self.state['enabled'] or not p or p['expires']<=time.time() or p['attempts']>=5:raise HTTPException(401,'Mac에서 새 연결 코드를 만드세요.')
            p['attempts']+=1
            if not hmac.compare_digest(p['hash'],digest(code)):raise HTTPException(401,'연결 코드가 맞지 않습니다.')
            if self._epoch(p['owner'])!=p['epoch']:self.pending=None;raise HTTPException(401,'Mac 로그인 상태가 바뀌었습니다.')
            self.state['devices']={k:d for k,d in self.state['devices'].items() if d['expires']>time.time()}
            if len(self.state['devices'])>=32:raise HTTPException(409,'Mac에서 사용하지 않는 기기를 해제하세요.')
            token=secrets.token_urlsafe(32);key=secrets.token_hex(16)
            self.state['devices'][key]={'owner':p['owner'],'name':name,'hash':digest(token),'expires':time.time()+30*DAY,'last_seen':time.time(),'library_read':False,'library_revision':0,'library_ops':{}}
            self._save();self.pending=None
            return token
    def device(self,token):
        with self.lock:
            if not self.state['enabled'] or not token or len(token)>128:raise HTTPException(401,'휴대폰 연결이 필요합니다.')
            hashed=digest(token)
            for key,d in self.state['devices'].items():
                if d['expires']>time.time() and d['owner']==self.state['owner'] and hmac.compare_digest(d['hash'],hashed):
                    epoch=self._epoch(d['owner'])
                    if time.time()-d.get('last_seen',0)>60:d['last_seen']=time.time();self._save()
                    return key,d['owner'],epoch
        raise HTTPException(401,'기기 연결이 만료되었거나 해제됐습니다.')
    def authorize(self,binding,db):
        if not isinstance(binding,MobileBinding) or binding.manager is not self:raise HTTPException(401,'Mobile session required')
        with self.lock:
            d=self.state['devices'].get(binding.device_id)
            if (not self.state['enabled'] or not d or d['expires']<=time.time() or d['owner']!=self.state['owner'] or self._epoch(d['owner'])!=binding.epoch):raise HTTPException(401,'모바일 연결 상태가 바뀌었습니다.')
            user=db.get(User,d['owner'])
            if not user or not user.is_active:raise HTTPException(401,'Owner unavailable')
            return user
    async def start(self):
        if not self.state['enabled']:return
        if self.task and not self.task.done():return
        sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind(('127.0.0.1',self.port));sock.listen(32);sock.setblocking(False)
            self.server=GatewayServer(uvicorn.Config(self.web,host='127.0.0.1',port=self.port,access_log=False,log_level='warning',lifespan='off',limit_concurrency=24,timeout_keep_alive=5,proxy_headers=False))
            self.sock=sock;self.task=asyncio.create_task(self.server.serve(sockets=[sock]))
            for _ in range(100):
                if self.task.done():await self.task;raise RuntimeError('mobile listener stopped')
                if self.server.started:return
                await asyncio.sleep(.01)
            raise RuntimeError('mobile listener did not start')
        except BaseException:
            sock.close();await self.close();raise
    async def close(self):
        if self.server:self.server.should_exit=True
        if self.task:
            try:await asyncio.wait_for(asyncio.shield(self.task),10)
            except (TimeoutError,asyncio.CancelledError):self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        if self.sock:self.sock.close()
        self.server=self.task=self.sock=None
    async def _body(self,request):
        if request.headers.get('content-type','').split(';')[0]!='application/json':raise HTTPException(415,'JSON required')
        raw=bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw)>65536:raise HTTPException(413,'Request too large')
        try:
            data=json.loads(raw)
            if not isinstance(data,dict):raise ValueError()
            return data
        except ValueError:raise HTTPException(422,'Invalid JSON') from None
    async def forward(self,request,key,epoch,path,*,body=None,query=None):
        binding=MobileBinding(self,key,epoch)
        async def trusted(scope,receive,send):
            scope['state']={**scope.get('state',{}),'_gamja_mobile_binding':binding}
            await self.app(scope,receive,send)
        headers={'X-Gamja-Session':self.app.state.local_session_token,'Authorization':'Bearer mobile-bridge'}
        if request.method=='POST' and path.endswith('/events'):
            command=request.headers.get('idempotency-key','')
            if not re.fullmatch(r'[a-f0-9-]{32,36}',command):raise HTTPException(422,'Idempotency key required')
            headers['Idempotency-Key']=command
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=trusted),base_url='http://127.0.0.1',timeout=45) as client:
            result=await client.request(request.method,'/api/v1'+path,json=body,params=query,headers=headers)
        return Response(result.content,status_code=result.status_code,media_type=result.headers.get('content-type','application/json'))
    async def library_forward(self,request,key,owner,epoch,revision,path,query=None,download=False):
        binding=MobileBinding(self,key,epoch)
        async def trusted(scope,receive,send):
            scope['state']={**scope.get('state',{}),'_gamja_mobile_binding':binding}
            await self.app(scope,receive,send)
        async def fetch():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=trusted),base_url='http://127.0.0.1',timeout=45) as client:
                return await client.get('/api/v1'+path,params=query,headers={'X-Gamja-Session':self.app.state.local_session_token,'Authorization':'Bearer mobile-bridge'})
        result=await fetch()
        self.library_check(key,owner,epoch,revision)
        if len(result.content)>20*1024*1024:raise HTTPException(413,'File too large')
        return result

    def library_bot_access(self,key,owner,epoch,revision,bot):
        from app.models.models import Agent
        from app.models.shared_text_runtime import SharedTextRoom
        with self.app.state.session_factory() as db:
            self.authorize(MobileBinding(self,key,epoch),db)
            row=(db.query(Agent.id).filter(Agent.id==bot,Agent.user_id==owner,Agent.is_active.is_(True),
                ~Agent.id.in_(db.query(SharedTextRoom.agent_id))).first())
            if row is None:raise HTTPException(404,'Bot unavailable')
        self.library_check(key,owner,epoch,revision)

    def library_personal_room(self,key,owner,epoch,revision,room):
        from sqlalchemy import text
        with self.app.state.session_factory() as db:
            self.authorize(MobileBinding(self,key,epoch),db)
            shared=db.execute(text('SELECT 1 FROM shared_text_rooms WHERE room_id=:room UNION ALL SELECT 1 FROM shared_bot_rooms WHERE room_id=:room LIMIT 1'),{'room':room}).first()
        self.library_check(key,owner,epoch,revision)
        return shared is None

    def library_result_access(self,key,owner,epoch,revision,bot,room,result_id,version):
        from app.models.models import Agent,Conversation,Project
        from app.models.room_result import RoomResult,RoomResultVersion
        with self.app.state.session_factory() as db:
            self.authorize(MobileBinding(self,key,epoch),db)
            row=(db.query(RoomResultVersion).join(RoomResult,RoomResult.result_id==RoomResultVersion.result_id)
                .join(Conversation,Conversation.id==RoomResult.room_id).join(Project,Project.id==Conversation.project_id)
                .join(Agent,Agent.id==RoomResultVersion.author_agent_id)
                .filter(RoomResult.owner_id==owner,RoomResult.room_id==room,RoomResult.result_id==result_id,
                    RoomResultVersion.room_id==room,RoomResultVersion.version==version,RoomResultVersion.author_agent_id==bot,
                    Agent.user_id==owner,Agent.is_active.is_(True),Conversation.user_id==owner,Conversation.is_active.is_(True),
                    Project.user_id==owner,Project.is_active.is_(True)).first())
            if row is None:raise HTTPException(404,'Result unavailable')
        if not self.library_personal_room(key,owner,epoch,revision,room):raise HTTPException(404,'Result unavailable')
        self.library_check(key,owner,epoch,revision)

    async def library_read(self,request,key,owner,epoch,route):
        revision=self.library_check(key,owner,epoch)
        after=request.query_params.get('after');query={'limit':50}
        if after:
            if len(after)>200:raise HTTPException(422,'Invalid cursor')
            query['after']=after
        if route=='library/bots':
            from app.models.models import Agent
            from app.models.shared_text_runtime import SharedTextRoom
            try:cursor=int(after or 0);assert 0<=cursor<=9223372036854775807
            except (ValueError,AssertionError):raise HTTPException(422,'Invalid cursor') from None
            with self.app.state.session_factory() as db:
                self.authorize(MobileBinding(self,key,epoch),db)
                rows=(db.query(Agent.id,Agent.name).filter(Agent.user_id==owner,Agent.is_active.is_(True),Agent.id>cursor,
                    ~Agent.id.in_(db.query(SharedTextRoom.agent_id))).order_by(Agent.id).limit(51).all())
                items=[{'id':r.id,'name':r.name} for r in rows[:50]]
            self.library_check(key,owner,epoch,revision)
            return JSONResponse({'items':items,'next_cursor':rows[49].id if len(rows)>50 else None})
        match=re.fullmatch(r'library/bots/([1-9][0-9]{0,17})/(results|uploads|pins)',route)
        if match:
            bot,kind=match.groups();self.library_bot_access(key,owner,epoch,revision,int(bot));path=f'/bots/{bot}/library'+('' if kind=='results' else '/'+kind)
            r=await self.library_forward(request,key,owner,epoch,revision,path,query)
            if r.status_code!=200:return Response(r.content,status_code=r.status_code,media_type='application/json')
            try:
                value=r.json();items=value['items'];assert isinstance(items,list) and len(items)<=50
                if kind=='results':items=[{k:item[k] for k in ('room_id','result_id','version','kind','title','author_agent_id')} for item in items if self.library_personal_room(key,owner,epoch,revision,item['room_id'])]
                elif kind=='uploads':items=[{k:item[k] for k in ('upload_id','bot_id','filename','size_bytes','sha256','removed')} for item in items]
                else:items=[{k:item[k] for k in ('source','revision','pinned','availability')} for item in items if item['source'].get('kind')!='result' or self.library_personal_room(key,owner,epoch,revision,item['source']['room_id'])]
                self.library_bot_access(key,owner,epoch,revision,int(bot))
                return JSONResponse({'items':items,'next_cursor':value.get('next_cursor')})
            except (ValueError,KeyError,TypeError,AssertionError):raise HTTPException(503,'Library unavailable') from None
        upload=re.fullmatch(r'library/bots/([1-9][0-9]{0,17})/uploads/([a-f0-9-]{36})(/file)?',route)
        result=re.fullmatch(r'library/bots/([1-9][0-9]{0,17})/results/([1-9][0-9]{0,17})/([a-f0-9-]{36})/([1-9][0-9]{0,17})(/file)?',route)
        if upload:
            bot,upload_id,file=upload.groups();self.library_bot_access(key,owner,epoch,revision,int(bot));path=f'/bots/{bot}/library/uploads/{upload_id}'+('/file' if file else '')
        elif result:
            bot,room,result_id,version,file=result.groups();self.library_bot_access(key,owner,epoch,revision,int(bot));self.library_result_access(key,owner,epoch,revision,int(bot),int(room),result_id,int(version))
            path=f'/conversations/{room}/results/{result_id}/versions/{version}'+('/file' if file else '')
        else:raise HTTPException(404,'Mobile route unavailable')
        if file:
            if self.library_download.locked():raise HTTPException(429,'Download busy')
            await self.library_download.acquire()
            try:
                r=await self.library_forward(request,key,owner,epoch,revision,path,download=True)
                self.library_bot_access(key,owner,epoch,revision,int(bot))
                if result:self.library_result_access(key,owner,epoch,revision,int(bot),int(room),result_id,int(version))
                if r.status_code!=200:
                    self.library_download.release()
                    return Response(r.content,status_code=r.status_code,media_type='application/json')
                disposition=r.headers.get('content-disposition','')
                if not re.fullmatch(r"attachment; filename\*=UTF-8''[A-Za-z0-9%._~-]{1,1800}",disposition):disposition="attachment; filename*=UTF-8''gamja-file"
                def check_send():
                    self.library_bot_access(key,owner,epoch,revision,int(bot))
                    if result:self.library_result_access(key,owner,epoch,revision,int(bot),int(room),result_id,int(version))
                return MobileDownload(r.content,self.library_download,disposition,check_send)
            except BaseException:
                self.library_download.release();raise
        r=await self.library_forward(request,key,owner,epoch,revision,path)
        self.library_bot_access(key,owner,epoch,revision,int(bot))
        if result:self.library_result_access(key,owner,epoch,revision,int(bot),int(room),result_id,int(version))
        if r.status_code!=200:return Response(r.content,status_code=r.status_code,media_type='application/json')
        try:
            value=r.json()
            fields=('upload_id','bot_id','filename','size_bytes','sha256','removed') if upload else ('room_id','result_id','version','kind','title','author_agent_id','content','file_status')
            return JSONResponse({k:value[k] for k in fields})
        except (ValueError,KeyError,TypeError):raise HTTPException(503,'Library unavailable') from None
    def _web(self):
        web=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
        @web.middleware('http')
        async def boundary(request,call_next):
            # Header trust never identifies a user; only the paired cookie does.
            if not self.state['enabled'] or request.headers.get('host','').lower()!=urlsplit(self.state['origin']).netloc:
                return JSONResponse({'detail':'Mobile gateway unavailable'},status_code=403)
            if request.method not in ('GET','POST'):return Response(status_code=405)
            if request.method=='POST' and (request.headers.get('origin')!=self.state['origin'] or request.headers.get('x-gamja-mobile')!='1'):
                return JSONResponse({'detail':'Origin denied'},status_code=403)
            response=await call_next(request)
            response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"})
            return response
        @web.get('/')
        async def page():
            from app.services.mobile_page import PAGE
            return HTMLResponse(PAGE)
        @web.get('/mobile.js')
        async def script():
            from app.services.mobile_page import JS
            return Response(JS,media_type='application/javascript')
        @web.post('/pair')
        async def pair(request:Request):
            data=await self._body(request);code=data.get('code');name=data.get('name')
            if set(data)!={'code','name'} or not isinstance(code,str) or not re.fullmatch(r'\d{8}',code) or not isinstance(name,str) or not 1<=len(name.strip())<=80 or any(ord(c)<32 for c in name):raise HTTPException(422,'기기 이름과 8자리 코드를 입력하세요.')
            token=self.pair(code,name.strip());response=JSONResponse({'paired':True});response.set_cookie(COOKIE,token,max_age=30*DAY,secure=True,httponly=True,samesite='strict',path='/');return response
        @web.get('/session')
        async def session(request:Request):
            key,owner,epoch=self.device(request.cookies.get(COOKIE));return {'paired':True,'device_name':self.state['devices'][key]['name'],'library_read':self.state['devices'][key].get('library_read') is True}
        @web.post('/disconnect')
        async def disconnect(request:Request):
            key,owner,epoch=self.device(request.cookies.get(COOKIE));self.revoke(owner,key);response=JSONResponse({'paired':False});response.delete_cookie(COOKIE,secure=True,httponly=True,samesite='strict',path='/');return response
        @web.api_route('/api/{route:path}',methods=['GET','POST'])
        async def api(route:str,request:Request):
            key,owner,epoch=self.device(request.cookies.get(COOKIE));method=request.method;query={};body=None
            if route.startswith('library/'):
                if method!='GET':raise HTTPException(405,'Mobile operation unavailable')
                return await self.library_read(request,key,owner,epoch,route)
            if route=='rooms' and method=='GET':
                path='/rooms/index';query={'status':'active','limit':50}
                cursor=request.query_params.get('cursor');search=request.query_params.get('q')
                if cursor:query['cursor']=cursor[:512]
                if search:query['q']=search[:200]
            else:
                match=re.fullmatch(r'rooms/([1-9][0-9]{0,17})/(events|messages|activity|stop|actions/([a-f0-9-]{36})/(preview|decision))',route)
                if not match:raise HTTPException(404,'Mobile route unavailable')
                room,operation,action,kind=match.groups()
                if method=='GET' and operation=='events':
                    path=f'/conversations/{room}/events'
                    try:after=int(request.query_params.get('after_seq','0'));assert after>=0
                    except (ValueError,AssertionError):raise HTTPException(422,'Invalid cursor') from None
                    query={'after_seq':after,'limit':100}
                elif method=='GET' and operation=='activity':path=f'/tools/rooms/{room}/activity'
                elif method=='GET' and kind=='preview':path=f'/tools/rooms/{room}/actions/{action}/preview'
                elif method=='POST' and operation=='messages':
                    body=await self._body(request)
                    if set(body)!={'content'} or not isinstance(body['content'],str) or not 1<=len(body['content'].strip())<=8000:raise HTTPException(422,'메시지는 1~8000자입니다.')
                    path=f'/conversations/{room}/events'
                elif method=='POST' and operation=='stop':
                    body=await self._body(request)
                    if body:raise HTTPException(422,'Empty body required')
                    path=f'/conversations/{room}/stop'
                elif method=='POST' and kind=='decision':
                    body=await self._body(request)
                    if set(body)-{'decision','review_id','acknowledge_unverified_context'} or body.get('decision') not in ('approve','deny') or not isinstance(body.get('review_id'),str):raise HTTPException(422,'Review required')
                    body={**body,'scope':'once'};path=f'/tools/rooms/{room}/actions/{action}/decision'
                else:raise HTTPException(405,'Mobile operation unavailable')
            return await self.forward(request,key,epoch,path,body=body,query=query)
        return web
