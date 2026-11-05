"""Explicit, in-memory team login bound to the current local Owner epoch."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
import json
import base64
import re
from uuid import UUID
from app.api.routes.auth import get_current_user
from app.models.models import User
from app.services.team_authority_adapter import TeamAuthorityAdapter, TeamAuthorityUnavailable, TeamAuthorityRejected, TeamSessionExpired

router=APIRouter()
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}
class Connect(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    profile_id:str=Field(min_length=1,max_length=64)
    team_id:str|None=Field(default=None,min_length=36,max_length=36)
    username:str=Field(min_length=1,max_length=200)
    password:str=Field(min_length=1,max_length=72,repr=False)

@router.get('/team-connection/profiles')
def profiles(request:Request,owner:User=Depends(get_current_user)):
    with request.app.state.owner_recovery_lock:
        epochs=getattr(request.app.state,'owner_epoch_provider',None)
        if not epochs or not epochs.current(owner.id):raise HTTPException(409,{'code':'team_owner_unavailable'})
        registry=getattr(request.app.state,'team_trust_profiles',{})
        return JSONResponse({'profiles':[p.public() for p in registry.values()],'conditional_disconnect_supported':True},headers=HEADERS)

def shared_text_available(request,adapter,owner_id,epoch):
    provider=getattr(request.app.state,'provider_client',None)
    return bool(adapter and adapter.owner_id==owner_id and adapter.owner_epoch==epoch and adapter.team_id
                and adapter.has_session() and getattr(provider,'available',False) is True)

def shared_apps_available(request,adapter,owner_id,epoch):
    bridge=getattr(request.app.state,'mcp_client',None)
    return bool(shared_text_available(request,adapter,owner_id,epoch) and getattr(request.app.state,'shared_connector_runtime_ready',False) and bridge and bridge.available)

def remote_rejected(error, adapter=None):
    if error.status==401:
        if adapter:adapter.clear()
        raise HTTPException(409,{'code':'team_session_expired'}) from None
    mapping={400:(422,'team_request_invalid'),403:(403,'team_permission_denied'),
             404:(404,'team_resource_not_found'),409:(409,'team_conflict'),422:(422,'team_request_invalid')}
    status,code=mapping.get(error.status,(503,'team_command_rejected'))
    raise HTTPException(status,{'code':code}) from None

@router.post('/team-connection')
def connect(value:Connect,request:Request,owner:User=Depends(get_current_user)):
    with request.app.state.owner_recovery_lock:
        epochs=getattr(request.app.state,'owner_epoch_provider',None);epoch=epochs.current(owner.id) if epochs else None
        if not epoch:raise HTTPException(409,{'code':'team_owner_unavailable'})
        profile=getattr(request.app.state,'team_trust_profiles',{}).get(value.profile_id)
        if profile is None:raise HTTPException(503,{'code':'team_profile_unavailable'})
        adapter=None
        try:
            adapter=TeamAuthorityAdapter(base_url=profile.base_url,issuer=profile.issuer,audience=profile.audience,
                public_key=profile.public_key,mode=profile.mode,owner_id=owner.id,owner_epoch=epoch,team_id=None)
            adapter.connect(value.username,value.password)
            if value.team_id is not None:adapter.select(value.team_id)
            if epochs.current(owner.id)!=epoch:raise TeamAuthorityUnavailable()
        except TeamAuthorityRejected as error:
            if adapter:adapter.close()
            if error.status==401:raise HTTPException(422,{'code':'team_credentials_invalid'}) from None
            remote_rejected(error)
        except Exception:
            if adapter:adapter.close()
            raise HTTPException(503,{'code':'team_connection_unavailable'}) from None
        adapter.profile_id=profile.profile_id;adapter.key_fingerprint=profile.key_fingerprint
        old=getattr(request.app.state,'team_connection',None)
        if old:
            from app.services.shared_connector import revoke_session_blocking
            if not revoke_session_blocking(request.app.state,old):
                adapter.close();raise HTTPException(503,{'code':'shared_connector_revoke_unconfirmed'})
        request.app.state.team_connection=adapter
        if old:old.close()
        try: adapter.authenticated_claims(request,owner.id,epoch);state='team_ready'
        except TeamSessionExpired:
            adapter.close();request.app.state.team_connection=None
            return JSONResponse({'state':'team_session_expired','shared_execution_available':False,'shared_personal_apps_available':False,'shared_private_memory_available':False,'shared_text_execution_available':False},headers=HEADERS)
        except TeamAuthorityRejected as error:
            if error.status==401:
                adapter.close();request.app.state.team_connection=None
                return JSONResponse({'state':'team_session_expired','shared_execution_available':False,'shared_personal_apps_available':False,'shared_private_memory_available':False,'shared_text_execution_available':False},headers=HEADERS)
            state='authenticated'
        except TeamAuthorityUnavailable: state='authenticated'
        return JSONResponse({'state':state,'issuer':adapter.issuer,'team_id':adapter.team_id,
            'account_id':adapter._subject,'profile_id':adapter.profile_id,'session_generation':adapter.generation,'persisted':False,'shared_execution_available':False,
            'shared_personal_apps_available':state=='team_ready' and shared_apps_available(request,adapter,owner.id,epoch),
            'shared_private_memory_available':state=='team_ready' and shared_text_available(request,adapter,owner.id,epoch),
            'shared_text_execution_available':state=='team_ready' and shared_text_available(request,adapter,owner.id,epoch)},headers=HEADERS)

@router.get('/team-connection')
def status(request:Request,owner:User=Depends(get_current_user)):
    with request.app.state.owner_recovery_lock:
        adapter=getattr(request.app.state,'team_connection',None);epochs=getattr(request.app.state,'owner_epoch_provider',None)
        if adapter is None or epochs is None or epochs.current(owner.id)!=adapter.owner_epoch or adapter.owner_id!=owner.id:
            if adapter:adapter.close();request.app.state.team_connection=None
            return JSONResponse({'state':'disconnected','shared_execution_available':False,'shared_personal_apps_available':False,'shared_private_memory_available':False,'shared_text_execution_available':False},headers=HEADERS)
        if not adapter.has_session():
            adapter.close();request.app.state.team_connection=None
            return JSONResponse({'state':'team_session_expired','shared_execution_available':False,'shared_personal_apps_available':False,'shared_private_memory_available':False,'shared_text_execution_available':False},headers=HEADERS)
        try:
            adapter.validate_session()
            claims=adapter.authenticated_claims(request,owner.id,adapter.owner_epoch)
        except TeamSessionExpired:
            adapter.close();request.app.state.team_connection=None
            return JSONResponse({'state':'team_session_expired','shared_execution_available':False,'shared_personal_apps_available':False,'shared_private_memory_available':False,'shared_text_execution_available':False},headers=HEADERS)
        except TeamAuthorityRejected as error:
            if error.status==401:
                adapter.close();request.app.state.team_connection=None
                return JSONResponse({'state':'team_session_expired','shared_execution_available':False,'shared_personal_apps_available':False,'shared_private_memory_available':False,'shared_text_execution_available':False},headers=HEADERS)
            return JSONResponse({'state':'authenticated','issuer':adapter.issuer,'team_id':adapter.team_id,
                'account_id':adapter._subject,'profile_id':adapter.profile_id,'session_generation':adapter.generation,'persisted':False,'shared_execution_available':False,'shared_personal_apps_available':False,'shared_private_memory_available':False,'shared_text_execution_available':False},headers=HEADERS)
        except TeamAuthorityUnavailable:
            return JSONResponse({'state':'authenticated','issuer':adapter.issuer,'team_id':adapter.team_id,
                'account_id':adapter._subject,'profile_id':adapter.profile_id,'session_generation':adapter.generation,'persisted':False,'shared_execution_available':False,'shared_personal_apps_available':False,'shared_private_memory_available':False,'shared_text_execution_available':False},headers=HEADERS)
        return JSONResponse({'state':'team_ready','issuer':claims['issuer'],'team_id':claims['team_subject'],
            'account_id':claims['subject'],'profile_id':adapter.profile_id,'session_generation':adapter.generation,'persisted':False,'shared_execution_available':False,
            'shared_personal_apps_available':shared_apps_available(request,adapter,owner.id,adapter.owner_epoch),
            'shared_private_memory_available':shared_text_available(request,adapter,owner.id,adapter.owner_epoch),
            'shared_text_execution_available':shared_text_available(request,adapter,owner.id,adapter.owner_epoch)},headers=HEADERS)

@router.delete('/team-connection')
def disconnect(request:Request,session_generation:str,owner:User=Depends(get_current_user)):
    with request.app.state.owner_recovery_lock:
        adapter=getattr(request.app.state,'team_connection',None);epochs=getattr(request.app.state,'owner_epoch_provider',None)
        if adapter is None:return JSONResponse({'state':'disconnected'},headers=HEADERS)
        if epochs is None or adapter.owner_id!=owner.id or epochs.current(owner.id)!=adapter.owner_epoch:
            adapter.close();request.app.state.team_connection=None
            raise HTTPException(409,{'code':'team_owner_changed'})
        if session_generation!=adapter.generation:raise HTTPException(409,{'code':'team_session_changed'})
        from app.services.shared_connector import revoke_session_blocking
        shared_cancel_confirmed=revoke_session_blocking(request.app.state,adapter)
        request.app.state.team_connection=None
        revoked=adapter.disconnect();adapter.close()
        return JSONResponse({'state':'disconnected','remote_session_revoked':revoked,'shared_cancel_confirmed':shared_cancel_confirmed},headers=HEADERS)

class Command(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    operation:Literal['create_team','list_teams','list_invitations','list_publications','list_members','list_sent_invitations','get_invite','invite_existing','invite_new','accept_invite',
                      'revoke_invite','revoke_member','get_publication','put_publication','revoke_publication']
    session_generation:str=Field(min_length=36,max_length=36)
    operation_id:str|None=Field(default=None,min_length=36,max_length=36)
    payload:dict

@router.post('/team-connection/command')
def command(value:Command,request:Request,owner:User=Depends(get_current_user)):
    try:
        mutations={'create_team','invite_existing','invite_new','accept_invite','revoke_invite','revoke_member','put_publication','revoke_publication'}
        if value.operation in mutations:
            if not isinstance(value.operation_id,str) or str(UUID(value.operation_id))!=value.operation_id:raise ValueError()
        elif value.operation_id is not None:raise ValueError()
        target={'get_invite':'invite_id','accept_invite':'invite_id','revoke_invite':'invite_id',
                'revoke_member':'account_id','get_publication':'publication_id','put_publication':'publication_id',
                'revoke_publication':'publication_id'}.get(value.operation)
        if target is not None:
            identity=value.payload.get(target)
            if not isinstance(identity,str) or str(UUID(identity))!=identity:raise ValueError()
        if len(json.dumps(value.payload,ensure_ascii=False,allow_nan=False).encode('utf-8'))>192*1024:
            raise ValueError()
    except (ValueError,TypeError,UnicodeError,RecursionError):raise HTTPException(422,{'code':'team_command_invalid'}) from None
    with request.app.state.owner_recovery_lock:
        adapter=getattr(request.app.state,'team_connection',None);epochs=getattr(request.app.state,'owner_epoch_provider',None)
        epoch=epochs.current(owner.id) if epochs else None
        if not adapter or adapter.owner_id!=owner.id or not epoch or adapter.owner_epoch!=epoch:
            if adapter:adapter.close();request.app.state.team_connection=None
            raise HTTPException(409,{'code':'team_connection_unavailable'})
        if value.session_generation!=adapter.generation:
            raise HTTPException(409,{'code':'team_session_changed'})
        try:
            result=adapter.command(value.operation,value.payload,value.operation_id)
            if value.operation=='invite_new':
                token=result.pop('token',None)
                result['invite_code']=make_invite_code(adapter.issuer,adapter.key_fingerprint,adapter.team_id,result['invite_id'],token) if token else None
        except TeamSessionExpired:
            adapter.close();request.app.state.team_connection=None
            raise HTTPException(409,{'code':'team_session_expired'}) from None
        except TeamAuthorityRejected as error:
            remote_rejected(error,adapter)
        except TeamAuthorityUnavailable:
            # A sent mutation may have completed. Never repeat it automatically.
            raise HTTPException(503,{'code':'team_command_unconfirmed'}) from None
        if epochs.current(owner.id)!=epoch or getattr(request.app.state,'team_connection',None) is not adapter:
            adapter.close();request.app.state.team_connection=None
            raise HTTPException(409,{'code':'team_command_unconfirmed'})
        return JSONResponse({'result':result,'session_generation':adapter.generation},headers=HEADERS)

def make_invite_code(issuer,fingerprint,team_id,invite_id,secret):
    from app.services.team_authority_adapter import _uuid
    _uuid(team_id);_uuid(invite_id)
    if not isinstance(secret,str) or not 32<=len(secret)<=128 or not secret.isascii():raise TeamAuthorityUnavailable()
    value={'v':1,'issuer':issuer,'fingerprint':fingerprint,'team_id':team_id,'invite_id':invite_id,'secret':secret}
    data=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii')
    return 'GT1.'+base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')

def parse_invite_code(code):
    from app.services.team_authority_adapter import _uuid
    try:
        if not isinstance(code,str) or len(code)>4096 or not re.fullmatch(r'GT1\.[A-Za-z0-9_-]+',code):raise ValueError()
        raw=code[4:];data=base64.b64decode(raw+'='*(-len(raw)%4),altchars=b'-_',validate=True)
        def unique(pairs):
            result={}
            for key,value in pairs:
                if key in result:raise ValueError()
                result[key]=value
            return result
        value=json.loads(data,object_pairs_hook=unique)
        if set(value)!={'v','issuer','fingerprint','team_id','invite_id','secret'} or value['v']!=1:raise ValueError()
        _uuid(value['team_id']);_uuid(value['invite_id'])
        if not isinstance(value['issuer'],str) or not isinstance(value['fingerprint'],str) or not re.fullmatch(r'[0-9a-f]{64}',value['fingerprint']):raise ValueError()
        if make_invite_code(value['issuer'],value['fingerprint'],value['team_id'],value['invite_id'],value['secret'])!=code:raise ValueError()
        return value
    except Exception:raise ValueError('Invalid invitation') from None

class SelectTeam(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    session_generation:str=Field(min_length=36,max_length=36)
    team_id:str=Field(min_length=36,max_length=36)

@router.post('/team-connection/select')
def select_team(value:SelectTeam,request:Request,owner:User=Depends(get_current_user)):
    with request.app.state.owner_recovery_lock:
        adapter=getattr(request.app.state,'team_connection',None);epochs=getattr(request.app.state,'owner_epoch_provider',None)
        epoch=epochs.current(owner.id) if epochs else None
        if not adapter or adapter.owner_id!=owner.id or not epoch or adapter.owner_epoch!=epoch:raise HTTPException(409,{'code':'team_connection_unavailable'})
        if value.session_generation!=adapter.generation:raise HTTPException(409,{'code':'team_session_changed'})
        from app.services.shared_connector import revoke_session_blocking
        if not revoke_session_blocking(request.app.state,adapter):raise HTTPException(503,{'code':'shared_connector_revoke_unconfirmed'})
        try:claims=adapter.select(value.team_id)
        except TeamAuthorityRejected as error:remote_rejected(error,adapter)
        except TeamSessionExpired:
            adapter.close();request.app.state.team_connection=None
            raise HTTPException(409,{'code':'team_session_expired'}) from None
        except TeamAuthorityUnavailable:raise HTTPException(503,{'code':'team_selection_unavailable'}) from None
        if epochs.current(owner.id)!=epoch:
            adapter.close();request.app.state.team_connection=None
            raise HTTPException(409,{'code':'team_session_changed'})
        return JSONResponse({'state':'team_ready','issuer':adapter.issuer,'team_id':adapter.team_id,'account_id':claims['sub'],
            'profile_id':adapter.profile_id,'session_generation':adapter.generation,'persisted':False,'shared_execution_available':False,
            'shared_personal_apps_available':shared_apps_available(request,adapter,owner.id,epoch),
            'shared_private_memory_available':shared_text_available(request,adapter,owner.id,epoch),
            'shared_text_execution_available':shared_text_available(request,adapter,owner.id,epoch)},headers=HEADERS)

class Activation(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    invite_code:str=Field(min_length=1,max_length=4096,repr=False)
    username:str=Field(min_length=1,max_length=200)
    password:str=Field(min_length=12,max_length=72,repr=False)

@router.post('/team-connection/activate')
def activate(value:Activation,request:Request,owner:User=Depends(get_current_user)):
    from app.services.team_authority_adapter import _uuid
    try: code=parse_invite_code(value.invite_code)
    except ValueError:raise HTTPException(422,{'code':'team_invitation_invalid'}) from None
    with request.app.state.owner_recovery_lock:
        epochs=getattr(request.app.state,'owner_epoch_provider',None);epoch=epochs.current(owner.id) if epochs else None
        if not epoch:raise HTTPException(409,{'code':'team_owner_unavailable'})
        matches=[p for p in getattr(request.app.state,'team_trust_profiles',{}).values() if p.issuer==code['issuer'] and p.key_fingerprint==code['fingerprint']]
        if len(matches)!=1:raise HTTPException(422,{'code':'team_invitation_untrusted'})
        profile=matches[0];adapter=None
        try:
            adapter=TeamAuthorityAdapter(base_url=profile.base_url,issuer=profile.issuer,audience=profile.audience,
                public_key=profile.public_key,mode=profile.mode,owner_id=owner.id,owner_epoch=epoch,team_id=code['team_id'])
            result=adapter._http('POST','/v1/activate',payload={'team_id':code['team_id'],'invite_id':code['invite_id'],'token':code['secret'],
                'username':value.username,'password':value.password})
            if result.get('state')!='active' or result.get('team_id')!=code['team_id'] or epochs.current(owner.id)!=epoch:raise TeamAuthorityUnavailable()
            _uuid(result.get('account_id'))
        except TeamAuthorityRejected as error:remote_rejected(error)
        except Exception:raise HTTPException(503,{'code':'team_activation_unconfirmed'}) from None
        finally:
            if adapter:adapter.close()
        return JSONResponse({'state':'active','account_id':result['account_id'],'team_id':result['team_id'],'profile_id':profile.profile_id,
            'login_required':True,'shared_execution_available':False},headers=HEADERS)

@router.get('/team-connection/operations/{operation_id}')
def operation_receipt(operation_id:str,session_generation:str,request:Request,owner:User=Depends(get_current_user)):
    with request.app.state.owner_recovery_lock:
        adapter=getattr(request.app.state,'team_connection',None);epochs=getattr(request.app.state,'owner_epoch_provider',None)
        epoch=epochs.current(owner.id) if epochs else None
        if not adapter or adapter.owner_id!=owner.id or not epoch or adapter.owner_epoch!=epoch:
            if adapter:adapter.close();request.app.state.team_connection=None
            raise HTTPException(409,{'code':'team_connection_unavailable'})
        if session_generation!=adapter.generation:raise HTTPException(409,{'code':'team_session_changed'})
        try: result=adapter.operation_receipt(operation_id)
        except TeamSessionExpired:
            adapter.close();request.app.state.team_connection=None
            raise HTTPException(409,{'code':'team_session_expired'}) from None
        except TeamAuthorityRejected as error:
            remote_rejected(error,adapter)
        except TeamAuthorityUnavailable:
            raise HTTPException(503,{'code':'team_operation_unavailable'}) from None
        if epochs.current(owner.id)!=epoch or getattr(request.app.state,'team_connection',None) is not adapter:
            adapter.close();request.app.state.team_connection=None
            raise HTTPException(409,{'code':'team_session_changed'})
        return JSONResponse({'receipt':result,'session_generation':adapter.generation},headers=HEADERS)
