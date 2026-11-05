"""Owner-epoch-bound explicit team login; credentials remain in process memory."""
from datetime import datetime, timezone
from threading import RLock
from urllib.parse import urlsplit
from uuid import UUID, uuid4
import httpx
import json
import jwt
from jwt import ExpiredSignatureError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

class TeamAuthorityUnavailable(Exception): pass
class TeamSessionExpired(TeamAuthorityUnavailable): pass
class TeamAuthorityRejected(TeamAuthorityUnavailable):
    def __init__(self,status): self.status=status

def _uuid(value):
    try:
        if not isinstance(value,str) or str(UUID(value))!=value:raise ValueError()
    except (ValueError,TypeError):raise TeamAuthorityUnavailable() from None
    return value

def _url(value, mode):
    if not isinstance(value,str) or not value or len(value)>1024:raise TeamAuthorityUnavailable()
    parts=urlsplit(value)
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('','/') or not parts.hostname:raise TeamAuthorityUnavailable()
    if mode=='production' and parts.scheme!='https':raise TeamAuthorityUnavailable()
    if mode=='development' and (parts.scheme!='http' or parts.hostname not in ('127.0.0.1','::1')):raise TeamAuthorityUnavailable()
    if mode not in ('production','development'):raise TeamAuthorityUnavailable()
    return value.rstrip('/')

class TeamAuthorityAdapter:
    def __init__(self, *, base_url, issuer, audience, public_key, mode, owner_id, owner_epoch, team_id):
        self.base_url=_url(base_url,mode);self.issuer=_url(issuer,mode)
        if self.issuer!=self.base_url or not isinstance(audience,str) or not 1<=len(audience)<=200:raise TeamAuthorityUnavailable()
        try:key=serialization.load_pem_public_key(public_key.encode('ascii'))
        except (ValueError,TypeError,UnicodeError):raise TeamAuthorityUnavailable() from None
        if not isinstance(key,rsa.RSAPublicKey) or key.key_size<2048:raise TeamAuthorityUnavailable()
        if type(owner_id) is not int or owner_id<1 or not isinstance(owner_epoch,str) or len(owner_epoch)!=64:raise TeamAuthorityUnavailable()
        self.audience=audience;self.public_key=public_key;self.owner_id=owner_id;self.owner_epoch=owner_epoch
        self.team_id=_uuid(team_id) if team_id is not None else None;self.generation=str(uuid4());self._token=None;self._subject=None;self._lock=RLock()
        self._client=httpx.Client(timeout=5,follow_redirects=False,trust_env=False)
    def _decode(self,token,purpose):
        try:
            claims=jwt.decode(token,self.public_key,algorithms=['RS256'],issuer=self.issuer,audience=self.audience,
                options={'require':['exp','iat','sub','jti']})
            if claims.get('purpose')!=purpose:raise ValueError()
            _uuid(claims['sub']);_uuid(claims['jti'])
            return claims
        except ExpiredSignatureError:raise TeamSessionExpired() from None
        except Exception:raise TeamAuthorityUnavailable() from None
    def _http(self,method,path,*,payload=None,token=None,operation_id=None):
        headers={'Authorization':'Bearer '+token} if token else {}
        if operation_id is not None:headers['Idempotency-Key']=_uuid(operation_id)
        try:
            with self._client.stream(method,self.base_url+path,json=payload,headers=headers) as response:
                if 400 <= response.status_code < 500:raise TeamAuthorityRejected(response.status_code)
                response.raise_for_status();data=bytearray()
                for chunk in response.iter_bytes():
                    if len(data)+len(chunk)>256*1024:raise TeamAuthorityUnavailable()
                    data.extend(chunk)
            value=json.loads(data)
            if not isinstance(value,dict):raise TeamAuthorityUnavailable()
            return value
        except TeamAuthorityRejected:raise
        except Exception:raise TeamAuthorityUnavailable() from None
    def connect(self,username,password):
        if not isinstance(username,str) or not isinstance(password,str):raise TeamAuthorityUnavailable()
        try:
            obj=self._http('POST','/v1/login',payload={'username':username,'password':password});token=obj['access_token']
            claims=self._decode(token,'team_access')
            with self._lock:self._token=token;self._subject=claims['sub']
        except TeamAuthorityRejected:
            self.clear();raise
        except Exception:
            self.clear();raise TeamAuthorityUnavailable() from None
    def _assertion(self, team_id=None):
        team_id=_uuid(team_id or self.team_id)
        with self._lock:
            token=self._token;subject=self._subject
        if not token or not subject:raise TeamAuthorityUnavailable()
        access=self._decode(token,'team_access')
        try:
            response=self._http('GET','/v1/teams/'+team_id+'/assertion',token=token)
            claims=self._decode(response['assertion'],'team_assertion')
        except (TeamAuthorityRejected,TeamSessionExpired):raise
        except Exception:raise TeamAuthorityUnavailable() from None
        if (claims['sub']!=subject or access['sub']!=subject or claims['jti']!=access['jti']
                or claims.get('team_subject')!=team_id or claims.get('team_role') not in ('owner','admin','member')
                or not isinstance(claims.get('authority_revision'),str) or not claims['authority_revision']):
            raise TeamAuthorityUnavailable()
        return claims
    def select(self,team_id):
        team_id=_uuid(team_id)
        claims=self._assertion(team_id)
        with self._lock:
            self.team_id=team_id;self.generation=str(uuid4())
        return claims
    def authenticated_claims(self,request,owner_id,owner_epoch):
        if (owner_id,owner_epoch)!=(self.owner_id,self.owner_epoch):self.clear();raise TeamAuthorityUnavailable()
        claims=self._assertion()
        return {'verified':True,'owner_id':owner_id,'owner_epoch':owner_epoch,'issuer':self.issuer,'subject':claims['sub'],
                'team_subject':self.team_id,'team_role':claims['team_role'],
                'expires_at':datetime.fromtimestamp(claims['exp'],timezone.utc).isoformat(),'authority_revision':claims['authority_revision']}
    def is_current(self,request,owner_id,owner_epoch,claims):
        if (owner_id,owner_epoch)!=(self.owner_id,self.owner_epoch):self.clear();return False
        current=self._assertion()
        return (current['sub']==claims['subject'] and current['team_subject']==claims['team_subject']
                and current['team_role']==claims['team_role'] and current['authority_revision']==claims['authority_revision'])
    def command(self,operation,payload,operation_id=None):
        """Only fixed authority operations; no caller-controlled URL or headers."""
        with self._lock: token=self._token
        if not token:raise TeamAuthorityUnavailable()
        self._decode(token,'team_access')
        if not isinstance(payload,dict):raise TeamAuthorityUnavailable()
        mutations={'create_team','invite_existing','invite_new','accept_invite','revoke_invite','revoke_member','put_publication','revoke_publication'}
        if operation in mutations:_uuid(operation_id)
        elif operation_id is not None:raise TeamAuthorityUnavailable()
        if operation=='create_team':
            if set(payload)!={'id','name'}:raise TeamAuthorityUnavailable()
            _uuid(payload['id'])
            return self._http('POST','/v1/teams',payload=payload,token=token,operation_id=operation_id)
        if operation in ('list_teams','list_invitations'):
            if payload:raise TeamAuthorityUnavailable()
            return self._http('GET','/v1/teams' if operation=='list_teams' else '/v1/invitations',token=token)
        team_id=self.team_id
        if operation=='accept_invite':
            team_id=_uuid(payload.get('team_id'))
        if team_id is None:raise TeamAuthorityUnavailable()
        root='/v1/teams/'+team_id
        methods={
            'list_teams':('GET','/v1/teams'),
            'list_invitations':('GET','/v1/invitations'),
            'list_publications':('GET',root+'/publications'),
            'list_members':('GET',root+'/members'),
            'list_sent_invitations':('GET',root+'/invites'),
            'invite_existing':('POST',root+'/invites'),
            'invite_new':('POST',root+'/pending-invites'),
        }
        if operation in methods:
            method,path=methods[operation]
            if method=='GET' and payload:raise TeamAuthorityUnavailable()
            return self._http(method,path,payload=payload if method!='GET' else None,token=token,operation_id=operation_id)
        field={'get_invite':'invite_id','accept_invite':'invite_id','revoke_invite':'invite_id','revoke_member':'account_id',
               'get_publication':'publication_id','put_publication':'publication_id','revoke_publication':'publication_id'}.get(operation)
        if field is None:raise TeamAuthorityUnavailable()
        value=_uuid(payload.get(field));body={k:v for k,v in payload.items() if k!=field and not (operation=='accept_invite' and k=='team_id')}
        if operation=='get_invite':method,path='GET',root+'/invites/'+value
        elif operation=='accept_invite':method,path='POST',root+'/invites/'+value+'/accept'
        elif operation=='revoke_invite':method,path='POST',root+'/invites/'+value+'/revoke'
        elif operation=='revoke_member':method,path='POST',root+'/members/'+value+'/revoke'
        elif operation=='get_publication':method,path='GET',root+'/publications/'+value
        elif operation=='put_publication':method,path='PUT',root+'/publications/'+value;body['id']=value
        else:method,path='POST',root+'/publications/'+value+'/revoke'
        if method=='GET' and body:raise TeamAuthorityUnavailable()
        return self._http(method,path,payload=body if method!='GET' else None,token=token,operation_id=operation_id)
    def operation_receipt(self,operation_id):
        with self._lock:token=self._token
        if not token:raise TeamAuthorityUnavailable()
        self._decode(token,'team_access')
        result=self._http('GET','/v1/operations/'+_uuid(operation_id),token=token)
        if (result.get('operation_id')!=operation_id or
                (self.team_id is None and result.get('kind') not in ('team.create','invite.accept')) or
                (self.team_id is not None and result.get('team_id')!=self.team_id and result.get('kind') not in ('team.create','invite.accept'))):
            raise TeamAuthorityUnavailable()
        return result
    def validate_session(self):
        with self._lock: token=self._token
        if not token:raise TeamSessionExpired()
        self._decode(token,'team_access')
    def has_session(self):
        with self._lock:return bool(self._token)
    def disconnect(self):
        with self._lock: token=self._token
        self.clear()
        if not token:return True
        try:
            response=self._http('POST','/v1/logout',token=token)
            return response.get('revoked') is True
        except Exception:return False
    def clear(self):
        with self._lock:self._token=None;self._subject=None
    def close(self):
        self.clear();self._client.close()
