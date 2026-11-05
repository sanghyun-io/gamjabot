"""Explicit team setup; unavailable until a trusted authority is installed."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.shared_bot import CreateTeam, Invite, Publish, RecipientRoom, ExpectedRevision, RevokeMember
from app.services import shared_bot_foundation as service

router=APIRouter()
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}

def invoke(request, user, function, *args):
    # Account bind/switch already uses this lock. The future authority adapter
    # must additionally attest the same Core epoch and verified team identity.
    with request.app.state.owner_recovery_lock:
        claims=service.principal(request,user)
        result=function(*args,claims)
        if request.method == 'GET': service.guard(claims)
        return JSONResponse(result,headers=HEADERS)

@router.post('/teams')
def create_team(value:CreateTeam,request:Request,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    return invoke(request,user,lambda claims:service.create_team(db,user,claims,value))

@router.get('/teams/{team_id}')
def team(team_id:str,request:Request,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    return invoke(request,user,lambda claims:service.read_team(db,claims,team_id))

@router.post('/teams/{team_id}/invites')
def invite(team_id:str,value:Invite,request:Request,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    return invoke(request,user,lambda claims:service.invite(db,claims,team_id,value))

@router.post('/teams/{team_id}/invites/{invite_id}/accept')
def accept(team_id:str,invite_id:str,request:Request,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    return invoke(request,user,lambda claims:service.accept(db,claims,team_id,invite_id))

@router.post('/teams/{team_id}/invites/{invite_id}/revoke')
def revoke_invite(team_id:str,invite_id:str,value:ExpectedRevision,request:Request,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    return invoke(request,user,lambda claims:service.revoke_invite(db,claims,team_id,invite_id,value.expected_revision))

@router.post('/teams/{team_id}/members/revoke')
def revoke_member(team_id:str,value:RevokeMember,request:Request,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    return invoke(request,user,lambda claims:service.revoke_member(db,claims,team_id,value.identity.issuer,value.identity.subject,value.expected_revision))

@router.put('/teams/{team_id}/bots/{share_id}')
def publication(team_id:str,share_id:str,value:Publish,request:Request,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    if value.share_id!=share_id: service.fail('id_invalid',422)
    return invoke(request,user,lambda claims:service.publication(db,user,claims,team_id,value))

@router.get('/teams/{team_id}/bots/{share_id}')
def read_publication(team_id:str,share_id:str,request:Request,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    return invoke(request,user,lambda claims:service.read_publication(db,user,claims,team_id,share_id))

@router.post('/teams/{team_id}/bots/{share_id}/revoke')
def revoke_share(team_id:str,share_id:str,value:ExpectedRevision,request:Request,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    return invoke(request,user,lambda claims:service.revoke_share(db,user,claims,team_id,share_id,value.expected_revision))

@router.post('/teams/{team_id}/bots/{share_id}/recipient-room')
def prepare_room(team_id:str,share_id:str,value:RecipientRoom,request:Request,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    return invoke(request,user,lambda claims:service.prepare_room(db,user,claims,team_id,share_id,value))

@router.get('/teams/{team_id}/bots/{share_id}/recipient-room')
def inspect_room(team_id:str,share_id:str,request:Request,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    return invoke(request,user,lambda claims:service.inspect_room(db,user,claims,team_id,share_id))

@router.delete('/teams/{team_id}/bots/{share_id}/recipient-room')
def release_room(team_id:str,share_id:str,request:Request,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    return invoke(request,user,lambda claims:service.release_room(db,user,claims,team_id,share_id))
