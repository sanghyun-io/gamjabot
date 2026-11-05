"""Owner/connection-scoped Sign in with ChatGPT public-client flow.

Credentials stay in private atomic files, never in the conversation DB or API.
Only OpenAI's documented authorization, token, JWKS and Responses origins are used.
"""
import asyncio
import base64
from datetime import datetime, timezone, timedelta
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from urllib.parse import urlencode, urlsplit, parse_qs
import uuid

import httpx
import jwt
from fastapi import HTTPException
from sqlalchemy import update
from app.models.collaboration import ProviderConnection
from app.models.models import User
from app.services.connections import ConnectionFailure

AUTH = 'https://auth.openai.com'
API = 'https://api.openai.com/v1'
SCOPES = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
_manager = None


def failure(code='invalid_credential'):
    return ConnectionFailure(code, 'ChatGPT authorization is unavailable. Sign in again in Connections; no API-key fallback was used.')


def install(root, sessions, owner_epochs):
    global _manager
    _manager = None
    _manager = Manager(root, sessions, owner_epochs)
    return _manager


def manager():
    if _manager is None: raise HTTPException(503, detail={'code': 'owner_epoch_unavailable'})
    return _manager


class Manager:
    def __init__(self, root, sessions, owner_epochs):
        self.root = Path(root).absolute(); self.sessions = sessions; self.epochs = owner_epochs
        self.lock = threading.RLock(); self.operations = {}; self.refresh_locks = {}
        if self.root.resolve() != self.root: raise ValueError('oauth_path')
        self.root.mkdir(mode=0o700, exist_ok=True); self._private(self.root, directory=True)
        host = self.root/'host.json'
        if not host.exists(): self._write(host, {'id': 'urn:uuid:'+str(uuid.uuid4())})
        self.host = self._read(host)['id']
        if not re.fullmatch(r'urn:uuid:[a-f0-9-]{36}', self.host): raise ValueError('oauth_host')

    def _private(self, path, directory=False):
        s = path.lstat()
        if (path.is_symlink() or s.st_uid != os.geteuid() or s.st_mode & 0o077
                or not (stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode))):
            raise ValueError('oauth_permissions')
        if not directory and s.st_size > 131072: raise ValueError('oauth_size')

    def _read(self, path):
        self._private(self.root, True); self._private(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream: return json.load(stream)

    def _write(self, path, data):
        self._private(self.root, True)
        if path.exists(): self._private(path)
        raw = json.dumps(data, separators=(',', ':')).encode()
        if len(raw) > 131072: raise ValueError('oauth_size')
        temp = self.root/('.write-'+secrets.token_hex(16))
        try:
            fd = os.open(temp, os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as out: out.write(raw); out.flush(); os.fsync(out.fileno())
            os.replace(temp, path)
        finally:
            if temp.exists(): temp.unlink()

    def _path(self, ref):
        if not isinstance(ref, str) or not re.fullmatch(r'chatgpt:[0-9a-f]{64}', ref): raise failure()
        return self.root/(ref.split(':')[1]+'.json')

    def _epoch(self, owner):
        epoch = self.epochs.current(owner) if self.epochs is not None else None
        if not epoch: raise HTTPException(503, detail={'code': 'owner_epoch_unavailable'})
        return epoch

    def _row(self, db, owner, connection):
        row = db.query(ProviderConnection).filter_by(id=connection, user_id=owner, provider_id='chatgpt').first()
        user = db.query(User).filter_by(id=owner, is_active=True).first()
        if row is None or user is None: raise HTTPException(404, detail={'code': 'operation_not_found'})
        return row

    def start(self, row, config_revision, grant_revision, key):
        with self.lock:
            self._expire()
            if not row.enabled or row.auth_method_id != 'oauth_browser': raise HTTPException(422, detail={'code':'auth_method_unavailable'})
            binding = (row.user_id, row.id, int(row.readiness_revision), int(row.auth_grant_revision), self._epoch(row.user_id))
            if binding[2:4] != (config_revision, grant_revision): raise HTTPException(409, detail={'code':'configuration_changed'})
            if not isinstance(key,str) or not 1 <= len(key) <= 256: raise HTTPException(422, detail={'code':'invalid_request'})
            keyhash = hashlib.sha256(key.encode()).hexdigest()
            for op in self.operations.values():
                if op['binding'][:2] == binding[:2]:
                    if op['key'] == keyhash:
                        if op['binding'] != binding: raise HTTPException(409, detail={'code':'idempotency_conflict'})
                        return self._public(op)
                    if op['state'] == 'awaiting_user': raise HTTPException(409, detail={'code':'auth_operation_busy'})
            if len(self.operations) >= 32: raise HTTPException(409, detail={'code':'auth_operation_busy'})
            registration=self.root/f'registration-{row.user_id}-{row.id}.json'
            previous = self._read(self._path(row.secret_ref)) if row.secret_ref else (self._read(registration) if registration.exists() else None)
            if previous and (previous['owner'],previous['connection']) != binding[:2]: raise failure()
            op = {'id':secrets.token_hex(32), 'key':keyhash, 'binding':binding, 'state':'awaiting_user',
                  'expires':time.time()+1800, 'state_nonce':secrets.token_urlsafe(32), 'nonce':secrets.token_urlsafe(32),
                  'verifier':secrets.token_urlsafe(48), 'previous':previous, 'old_ref':row.secret_ref}
            outer = self
            class Callback(BaseHTTPRequestHandler):
                def log_message(self,*args): pass
                def do_GET(self):
                    if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}': self.send_error(400); return
                    parsed = urlsplit(self.path)
                    if parsed.path == '/done':
                        body=b'ChatGPT sign-in finished. Return to Gamja Bot to check the result. You may close this tab.'
                        self.send_response(200); self.send_header('Content-Type','text/plain; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Referrer-Policy','no-referrer'); self.end_headers(); self.wfile.write(body); return
                    if parsed.path != '/auth/callback' or len(self.path)>16384: self.send_error(400); return
                    try:
                        query=parse_qs(parsed.query,strict_parsing=True)
                        if any(len(v)!=1 for v in query.values()): raise ValueError()
                        if not secrets.compare_digest(query.get('state',[''])[0],op['state_nonce']): raise ValueError()
                    except Exception: self.send_error(400); return
                    outer._complete(op,query)
                    self.send_response(303); self.send_header('Location','/done'); self.send_header('Cache-Control','no-store'); self.send_header('Referrer-Policy','no-referrer'); self.end_headers()
                    timer=threading.Timer(10,outer._stop_listener,args=(op,)); timer.daemon=True; timer.start()
            server=ThreadingHTTPServer(('127.0.0.1',0),Callback);server.daemon_threads=True
            op['server']=server;op['redirect']=f'http://127.0.0.1:{server.server_port}/auth/callback'
            params={'client_id':previous['client_id'] if previous else 'dynamic_agent_client','response_type':'code',
                    'ext_agent_host_id':self.host,'redirect_uri':op['redirect'],'scope':SCOPES,'resource':API,
                    'state':op['state_nonce'],'nonce':op['nonce'],'code_challenge_method':'S256',
                    'code_challenge':base64.urlsafe_b64encode(hashlib.sha256(op['verifier'].encode()).digest()).decode().rstrip('=')}
            # Keep ID tokens out of the public instruction URL. Reauthorization
            # may show an account chooser; the returned subject must still match.
            if not previous: params['agent_name_hint']='Gamja Bot'
            op['url']=AUTH+'/api/accounts/authorize?'+urlencode(params)
            self.operations[op['id']]=op
            thread=threading.Thread(target=server.serve_forever,daemon=True,name='gamja-chatgpt-callback');thread.start()
            timer=threading.Timer(1800,self._timeout,args=(op,));timer.daemon=True;timer.start();op['timer']=timer
            return self._public(op)

    def _json(self, client, method, url, **kwargs):
        response=client.request(method,url,**kwargs)
        if response.status_code>=300 or len(response.content)>1000000: raise ValueError('oauth_http')
        value=response.json()
        if not isinstance(value,dict): raise ValueError('oauth_payload')
        return value

    def _identity(self, client, token, client_id, nonce=None):
        keys=self._json(client,'GET',AUTH+'/.well-known/jwks.json')
        header=jwt.get_unverified_header(token)
        kid=header.get('kid'); algorithm=header.get('alg')
        if not isinstance(kid,str) or not kid or algorithm not in ('RS256','ES256'):
            raise ValueError('oauth_signing_key')
        entries=keys.get('keys')
        if not isinstance(entries,list): raise ValueError('oauth_signing_key')
        matches=[key for key in entries if isinstance(key,dict) and key.get('kid')==kid]
        if len(matches)!=1: raise ValueError('oauth_signing_key')
        selected=matches[0]
        if selected.get('alg',algorithm)!=algorithm or selected.get('use','sig')!='sig':
            raise ValueError('oauth_signing_key')
        key=jwt.PyJWK.from_dict(selected,algorithm=algorithm)
        claims=jwt.decode(token,key,algorithms=['RS256','ES256'],audience=client_id,issuer=AUTH,
                          options={'require':['exp','aud','iss']})
        if not isinstance(claims.get('sub'),str) or not claims['sub']: raise ValueError('oauth_subject')
        if nonce is not None and not secrets.compare_digest(claims.get('nonce',''),nonce): raise ValueError('oauth_nonce')
        return claims

    def _tokens(self, client, response, client_id, previous=None, nonce=None):
        scopes=response.get('scope',' '.join(previous.get('scopes',[])) if previous and nonce is None else '').split()
        if 'chatgpt.tokens.use.direct' not in scopes or 'resource.invoke' not in scopes: raise ValueError('oauth_permission')
        access=response.get('access_token'); refresh=response.get('refresh_token') or (previous or {}).get('refresh_token')
        if (response.get('token_type','').lower()!='bearer' or not isinstance(access,str) or not access
                or not isinstance(refresh,str) or not refresh or not isinstance(response.get('expires_in'),(int,float))
                or not 0 < response['expires_in'] <= 86400): raise ValueError('oauth_tokens')
        id_token=response.get('id_token')
        if id_token:
            claims=self._identity(client,id_token,client_id,nonce)
            if previous and claims['sub'] != previous['subject']: raise ValueError('oauth_account_changed')
        elif previous and nonce is None:
            claims={'sub':previous['subject']};id_token=previous['id_token']
        else: raise ValueError('oauth_id_token')
        return {'access_token':access,'refresh_token':refresh,'id_token':id_token,'subject':claims['sub'],
                'client_id':client_id,'scopes':scopes,'expires_at':time.time()+response['expires_in'],
                'label':claims.get('email') if isinstance(claims.get('email'),str) and re.fullmatch(r'[^@\s]{1,128}@[^@\s]{1,128}',claims['email']) else (previous or {}).get('label','ChatGPT account')}

    def _complete(self, op, query):
        with self.lock:
            if op['state']!='awaiting_user' or time.time()>op['expires']: return
            op['state']='exchanging';new_ref=None
            try:
                owner,connection,revision,grant,epoch=op['binding']
                if self._epoch(owner)!=epoch: raise ValueError('owner_changed')
                if 'error' in query: raise ValueError('oauth_declined')
                client_id=query.get('client_id',[(op['previous'] or {}).get('client_id','')])[0]
                if not re.fullmatch(r'oaiapp_[A-Za-z0-9_-]{1,240}',client_id): raise ValueError('oauth_client')
                if op['previous'] and client_id!=op['previous']['client_id']: raise ValueError('oauth_client_changed')
                with httpx.Client(trust_env=False,follow_redirects=False,timeout=20) as client:
                    response=self._json(client,'POST',AUTH+'/api/accounts/oauth/token',data={'grant_type':'authorization_code','client_id':client_id,'code':query.get('code',[''])[0],'code_verifier':op['verifier'],'redirect_uri':op['redirect'],'resource':API})
                    record=self._tokens(client,response,client_id,op['previous'],op['nonce'])
                record.update(owner=owner,connection=connection,host=self.host)
                new_ref='chatgpt:'+secrets.token_hex(32);self._write(self._path(new_ref),record)
                with self.sessions() as db:
                    row = self._row(db,owner,connection)
                    old_caps = row.capabilities
                    declared = old_caps.get('declared', {}) if isinstance(old_caps, dict) else {}
                    declared = {k: v for k, v in declared.items()
                                if k in {'tools', 'vision', 'temperature', 'max_tokens'} and type(v) is bool} if isinstance(declared, dict) else {}
                    if self._epoch(owner)!=epoch: raise ValueError('owner_changed')
                    changed=db.execute(update(ProviderConnection).where(ProviderConnection.id==connection,ProviderConnection.user_id==owner,ProviderConnection.provider_id=='chatgpt',ProviderConnection.enabled==True,ProviderConnection.readiness_revision==revision,ProviderConnection.auth_grant_revision==grant,ProviderConnection.secret_ref==op['old_ref'],ProviderConnection.capabilities==old_caps).values(secret_ref=new_ref,credential_state='present',auth_grant_revision=grant+1,readiness_revision=revision+1,capabilities={'declared':declared,'verified':{}})).rowcount
                    if changed!=1: raise ValueError('configuration_changed')
                    db.commit()
                op['state']='succeeded'
                self._write(self.root/f'registration-{owner}-{connection}.json', {k:record[k] for k in ('owner','connection','host','client_id','subject','label')})
                if op['old_ref']:
                    try:self._path(op['old_ref']).unlink(missing_ok=True)
                    except OSError:op['cleanup_pending']=True
            except Exception:
                op['state']='failed';op['error']='provider_authorization_denied'
                if new_ref:
                    with self.sessions() as db:
                        row=db.query(ProviderConnection).filter_by(id=op['binding'][1],user_id=op['binding'][0]).first()
                        if row is None or row.secret_ref!=new_ref:self._path(new_ref).unlink(missing_ok=True)
            finally:
                op['verifier']='';op['nonce']='';op['state_nonce']='';op['previous']=None;op['url']=None

    def _public(self,op):
        terminal=op['state'] in {'succeeded','failed','cancelled','expired'}
        result={'version':1,'operation_id':op['id'],'state':op['state']}
        if not terminal:
            result.update(expires_at=datetime.fromtimestamp(op['expires'],timezone.utc).isoformat(),browser_url=op['url'])
        elif op['state']=='succeeded':result['result']={'credential_state':'present'}
        else:result['error_code']=op.get('error') or ('operation_cancelled' if op['state']=='cancelled' else 'operation_expired')
        return result

    def status(self,owner,connection,operation,cancel=False):
        with self.lock:
            self._expire();op=self.operations.get(operation)
            if op is None or op['binding'][:2]!=(owner,connection):raise HTTPException(404,detail={'code':'operation_not_found'})
            if op['state']=='awaiting_user' and (cancel or self._epoch(owner)!=op['binding'][4]):
                op['state']='cancelled';op['previous']=None;op['verifier']='';op['nonce']='';op['state_nonce']='';op['url']=None;self._stop_listener(op)
            return self._public(op)

    def _stop_listener(self,op):
        server=op.pop('server',None)
        if server:server.shutdown();server.server_close()
        timer=op.pop('timer',None)
        if timer:timer.cancel()

    def _timeout(self,op):
        with self.lock:
            if op['state']=='awaiting_user':op['state']='expired';op['previous']=None;op['verifier']='';op['url']=None;op['nonce']='';op['state_nonce']=''
        self._stop_listener(op)

    def _expire(self):
        for op in list(self.operations.values()):
            if op['state']=='awaiting_user' and time.time()>op['expires']:self._timeout(op)
            if time.time()>op['expires']+600:self._stop_listener(op);self.operations.pop(op['id'],None)

    def token(self,config):
        owner,connection=config.owner_id,config.connection_id
        with self.lock:lock=self.refresh_locks.setdefault((owner,connection),threading.RLock())
        with lock:
            with self.sessions() as db:
                row=self._row(db,owner,connection)
                if not row.enabled or row.auth_method_id!='oauth_browser' or not row.secret_ref or row.readiness_revision!=config.config_revision or row.auth_grant_revision!=config.auth_grant_revision:raise failure()
                ref=row.secret_ref
            try:
                record=self._read(self._path(ref))
                if (record['owner'],record['connection'],record['host'])!=(owner,connection,self.host):raise failure()
                if record['expires_at']<time.time()+60:
                    with httpx.Client(trust_env=False,follow_redirects=False,timeout=20) as client:
                        data=self._json(client,'POST',AUTH+'/api/accounts/oauth/token',data={'grant_type':'refresh_token','client_id':record['client_id'],'refresh_token':record['refresh_token'],'resource':API})
                        fresh=self._tokens(client,data,record['client_id'],record)
                    record.update(fresh)
                    with self.sessions() as db:
                        row=self._row(db,owner,connection)
                        if not row.enabled or row.secret_ref!=ref or row.auth_grant_revision!=config.auth_grant_revision:raise failure()
                        self._write(self._path(ref),record)
                if 'chatgpt.tokens.use.direct' not in record['scopes']:raise failure()
                return record['access_token']
            except Exception:raise failure() from None

    def disconnect(self,db,row,revoke=False):
        with self.lock:
            for op in self.operations.values():
                if op['binding'][:2]==(row.user_id,row.id) and op['state']=='awaiting_user':self.status(row.user_id,row.id,op['id'],True)
            ref=row.secret_ref; revoke_state='skipped'
            if ref:
                record=self._read(self._path(ref))
                self._write(self.root/f'registration-{row.user_id}-{row.id}.json',{k:record[k] for k in ('owner','connection','host','client_id','subject','label')})
                if revoke:
                    revoke_state='remote_revoke_failed'
                    try:
                        with httpx.Client(trust_env=False,follow_redirects=False,timeout=15) as client:
                            metadata=self._json(client,'GET',AUTH+'/.well-known/openid-configuration')
                            endpoint=metadata.get('revocation_endpoint','');parsed=urlsplit(endpoint)
                            if (parsed.scheme!='https' or parsed.hostname!='auth.openai.com' or parsed.port is not None
                                    or parsed.username or parsed.password or parsed.query or parsed.fragment):raise ValueError('revocation_origin')
                            response=client.post(endpoint,data={'token':record['refresh_token'],'token_type_hint':'refresh_token','client_id':record['client_id']})
                            if response.status_code==200:revoke_state='revoked'
                    except Exception:pass
            row.secret_ref=None;row.enabled=False;row.credential_state='missing';row.auth_grant_revision+=1;row.readiness_revision+=1;row.capabilities={'declared':{},'verified':{}};db.commit()
            if ref:self._path(ref).unlink(missing_ok=True)
            return {'credential_state':'missing','revoke_state':revoke_state}

    def account(self,row):
        path=self.root/f'registration-{row.user_id}-{row.id}.json'
        if not path.exists():return None
        try:
            record=self._read(path)
            if (record['owner'],record['connection'],record['host'])!=(row.user_id,row.id,self.host):return None
            return {'label':record['label']}
        except Exception:return None

    def close(self):
        with self.lock:
            for op in list(self.operations.values()):self._stop_listener(op)
            self.operations.clear()
