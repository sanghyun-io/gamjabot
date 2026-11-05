"""Independent team authority. Factory requires explicit issuer, keys and DB."""
import time
import hashlib
import hmac
import secrets
import json
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4
import bcrypt
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from urllib.parse import urlsplit
import jwt
from jwt import PyJWTError as JWTError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from team_server.config import load, prepare_database
from team_server.store import initialize, Account, Session, Team, Member, Invite, Publication, PublicationMember, Operation

class Closed(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
class Login(Closed):
    username:str=Field(min_length=1,max_length=200)
    password:str=Field(min_length=1,max_length=72)
class TeamCreate(Closed):
    id:str=Field(min_length=36,max_length=36)
    name:str=Field(min_length=1,max_length=200)
class InviteCreate(Closed):
    id:str=Field(min_length=36,max_length=36)
    username:str=Field(min_length=1,max_length=200)
    role:Literal['manager','member']
    expires_at:int=Field(gt=0)
    expected_revision:int=Field(ge=1)
class PendingInvite(Closed):
    id:str=Field(min_length=36,max_length=36)
    account_id:str=Field(min_length=36,max_length=36)
    role:Literal['manager','member']
    expires_at:int=Field(gt=0)
    expected_revision:int=Field(ge=1)
class Activate(Closed):
    team_id:str=Field(min_length=36,max_length=36)
    invite_id:str=Field(min_length=36,max_length=36)
    token:str=Field(min_length=32,max_length=128)
    username:str=Field(min_length=1,max_length=200)
    password:str=Field(min_length=12,max_length=72)
class Revision(Closed):
    expected_revision:int=Field(ge=1)
class Person(Closed):
    account_id:str=Field(min_length=36,max_length=36)
    role:Literal['manager','collaborator']
class PublicationWrite(Closed):
    id:str=Field(min_length=36,max_length=36)
    role_text:str=Field(min_length=1,max_length=16000)
    knowledge_text:str=Field(max_length=64000)
    audience:Literal['team','selected']
    people:list[Person]=Field(max_length=100)
    expected_revision:int=Field(ge=0)

def uid(value):
    try:
        if str(UUID(value))!=value: raise ValueError()
    except (ValueError,TypeError,AttributeError): raise HTTPException(422,'Invalid identity') from None
    return value

def create_app():
    cfg=load()
    prepare_database(cfg)
    engine=create_engine(cfg.database_url,connect_args={'check_same_thread':False},future=True)
    initialize(engine)
    sessions=sessionmaker(bind=engine,expire_on_commit=False)
    app=FastAPI(title='Team authority')
    @app.exception_handler(RequestValidationError)
    async def invalid_request(request:Request,error:RequestValidationError):
        return JSONResponse({'detail':'Invalid request'},status_code=422)
    @app.exception_handler(Exception)
    async def unexpected(request:Request,error:Exception):
        return JSONResponse({'detail':'Service unavailable'},status_code=500)
    @app.middleware('http')
    async def transport_boundary(request:Request, call_next):
        expected=urlsplit(cfg.issuer)
        if request.headers.get('host','').lower()!=expected.netloc.lower():
            return JSONResponse({'detail':'Invalid host'},status_code=400)
        if cfg.mode=='production' and request.url.scheme!='https':
            return JSONResponse({'detail':'HTTPS required'},status_code=400)
        if cfg.mode=='development' and (request.client is None or request.client.host not in ('127.0.0.1','::1')):
            return JSONResponse({'detail':'Loopback required'},status_code=403)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        return response
    def db_session():
        with sessions() as db: yield db
    def bearer(authorization:str=Header(...)):
        if not authorization.startswith('Bearer '): raise HTTPException(401,'Authentication required')
        try:
            claims=jwt.decode(authorization[7:],cfg.public_key,algorithms=['RS256'],issuer=cfg.issuer,audience=cfg.audience,
                options={'require':['exp','iat','sub','jti']})
            if claims.get('purpose')!='team_access': raise ValueError()
            uid(claims['sub']);uid(claims['jti'])
            return claims
        except (JWTError,ValueError,KeyError,TypeError,HTTPException): raise HTTPException(401,'Authentication required') from None
    def auth(db,claims):
        account=db.get(Account,claims['sub']);session=db.get(Session,claims['jti']);now=int(time.time())
        if (not account or account.state!='active' or not session or session.account_id!=account.id or session.revoked
                or session.expires_at<=now or session.account_revision!=account.revision): raise HTTPException(401,'Authentication required')
        return account
    def lock(db): db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    def member(db,team_id,account_id,manager=False):
        team=db.get(Team,uid(team_id));row=db.get(Member,(team_id,account_id));account=db.get(Account,account_id)
        if not team or not row or row.state!='active' or not account or account.state!='active': raise HTTPException(404,'Team not found')
        if manager and row.role not in ('owner','manager'): raise HTTPException(403,'Forbidden')
        return team,row
    def authorize_receipt(db,actor,row):
        manager=row.kind in ('invite.existing','invite.enrollment','invite.revoke','member.revoke')
        member(db,row.team_id,actor.id,manager)
        if row.kind in ('publication.put','publication.revoke'):
            publication=db.get(Publication,row.target_id);acl=db.get(PublicationMember,(row.target_id,actor.id))
            if not publication or publication.team_id!=row.team_id or (publication.owner_id!=actor.id and (publication.state!='active' or not acl or acl.role!='manager')):
                raise HTTPException(404,'Operation not found')
    def operation(db,actor,session_id,operation_id,kind,payload):
        operation_id=uid(operation_id)
        material=json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
        digest=hashlib.sha256(material).hexdigest()
        prior=db.get(Operation,(actor.id,operation_id))
        if prior:
            authorize_receipt(db,actor,prior)
            if prior.kind!=kind or prior.session_id!=session_id or not hmac.compare_digest(prior.request_digest,digest):raise HTTPException(409,'Operation conflict')
            result=json.loads(prior.response);result['replayed']=True
            return digest,result
        return digest,None
    def finish(db,actor,session_id,operation_id,kind,digest,result,*,team_id,target_id=None,sensitive=False):
        saved={k:(None if k=='token' else v) for k,v in result.items()}
        db.add(Operation(account_id=actor.id,id=operation_id,kind=kind,session_id=session_id,team_id=team_id,target_id=target_id,request_digest=digest,
                         response=json.dumps(saved,sort_keys=True,ensure_ascii=False,separators=(',',':'))))
        db.commit()
        return result
    def sign(claims): return jwt.encode(claims,cfg.private_key,algorithm='RS256')
    @app.post('/v1/login')
    def login(value:Login,db=Depends(db_session)):
        try: password=value.password.encode('utf-8')
        except UnicodeError: raise HTTPException(401,'Invalid credentials') from None
        if len(password)>72: raise HTTPException(401,'Invalid credentials')
        lock(db);account=db.query(Account).filter_by(username=value.username).first();now=int(time.time())
        ok=False
        if account and account.state=='active' and account.locked_until<=now:
            try:ok=bcrypt.checkpw(password,account.password_hash.encode('ascii'))
            except (ValueError,UnicodeError):ok=False
        if not ok:
            if account and account.locked_until<=now:
                account.failed_count+=1
                if account.failed_count>=5:account.locked_until=now+900;account.failed_count=0
                db.commit()
            else:db.rollback()
            raise HTTPException(401,'Invalid credentials')
        account.failed_count=0;account.locked_until=0
        db.query(Session).filter(Session.expires_at<=now).delete(synchronize_session=False)
        sid=str(uuid4());expires=now+300
        db.add(Session(id=sid,account_id=account.id,account_revision=account.revision,expires_at=expires,revoked=0));db.commit()
        token=sign({'iss':cfg.issuer,'aud':cfg.audience,'sub':account.id,'jti':sid,'iat':now,'exp':expires,'purpose':'team_access'})
        return {'access_token':token,'token_type':'Bearer','expires_at':expires}
    @app.post('/v1/logout')
    def logout(claims=Depends(bearer),db=Depends(db_session)):
        lock(db);auth(db,claims);row=db.get(Session,claims['jti']);row.revoked=1;db.commit();return {'revoked':True}
    @app.get('/v1/operations/{operation_id}')
    def read_operation(operation_id:str,claims=Depends(bearer),db=Depends(db_session)):
        uid(operation_id);lock(db);actor=auth(db,claims);row=db.get(Operation,(actor.id,operation_id))
        if not row:raise HTTPException(404,'Operation not found')
        authorize_receipt(db,actor,row)
        return {'operation_id':operation_id,'team_id':row.team_id,'state':'committed','kind':row.kind,'result':json.loads(row.response)}
    @app.post('/v1/teams')
    def create_team(value:TeamCreate,operation_id:str=Header(...,alias='Idempotency-Key'),claims=Depends(bearer),db=Depends(db_session)):
        team_id=uid(value.id);lock(db);account=auth(db,claims)
        digest,replay=operation(db,account,claims['jti'],operation_id,'team.create',value.model_dump(mode='json'))
        if replay is not None:return replay
        row=db.get(Team,team_id)
        if row:
            if row.owner_id!=account.id or row.name!=value.name: raise HTTPException(409,'Team exists')
            return finish(db,account,claims['jti'],operation_id,'team.create',digest,{'team_id':team_id,'name':row.name,'revision':row.revision},team_id=team_id)
        if not value.name.strip() or not value.name.isprintable():raise HTTPException(422,'Invalid team name')
        db.add(Team(id=team_id,owner_id=account.id,name=value.name,revision=1,revocation_epoch=0))
        db.add(Member(team_id=team_id,account_id=account.id,role='owner',state='active',revision=1))
        return finish(db,account,claims['jti'],operation_id,'team.create',digest,{'team_id':team_id,'name':value.name,'revision':1},team_id=team_id)
    @app.get('/v1/teams/{team_id}/assertion')
    def assertion(team_id:str,claims=Depends(bearer),db=Depends(db_session)):
        lock(db);account=auth(db,claims);team,row=member(db,team_id,account.id);now=int(time.time())
        expires=min(now+60,claims['exp']);revision=f'{team.revision}:{team.revocation_epoch}:{row.revision}:{account.revision}'
        value={'iss':cfg.issuer,'aud':cfg.audience,'sub':account.id,'jti':claims['jti'],'iat':now,'exp':expires,
               'purpose':'team_assertion','team_subject':team.id,'team_role':'owner' if row.role=='owner' else 'admin' if row.role=='manager' else 'member',
               'authority_revision':revision}
        return {'assertion':sign(value)}
    @app.get('/v1/teams')
    def list_teams(claims=Depends(bearer),db=Depends(db_session)):
        lock(db);account=auth(db,claims)
        rows=db.query(Member,Team).join(Team,Team.id==Member.team_id).filter(Member.account_id==account.id,Member.state=='active').order_by(Team.id).limit(101).all()
        return {'items':[{'team_id':team.id,'name':team.name,'role':m.role,'revision':team.revision} for m,team in rows[:100]],'truncated':len(rows)>100}
    @app.get('/v1/invitations')
    def list_invitations(claims=Depends(bearer),db=Depends(db_session)):
        lock(db);account=auth(db,claims);now=int(time.time())
        rows=db.query(Invite,Team).join(Team,Team.id==Invite.team_id).filter(Invite.account_id==account.id,Invite.state=='pending',Invite.kind=='existing',Invite.expires_at>now).order_by(Invite.id).limit(101).all()
        return {'items':[{'invite_id':row.id,'team_id':row.team_id,'team_name':team.name,'role':row.role,'expires_at':row.expires_at} for row,team in rows[:100]],'truncated':len(rows)>100}
    @app.get('/v1/teams/{team_id}/members')
    def list_members(team_id:str,claims=Depends(bearer),db=Depends(db_session)):
        lock(db);actor=auth(db,claims);team,_=member(db,team_id,actor.id)
        rows=db.query(Member,Account).join(Account,Account.id==Member.account_id).filter(Member.team_id==team_id).order_by(Member.account_id).limit(101).all()
        return {'team_id':team_id,'revision':team.revision,'revocation_epoch':team.revocation_epoch,
                'items':[{'account_id':m.account_id,'username':account.username,'role':m.role,'state':m.state,'revision':m.revision} for m,account in rows[:100]],'truncated':len(rows)>100}
    @app.get('/v1/teams/{team_id}/invites')
    def sent_invitations(team_id:str,claims=Depends(bearer),db=Depends(db_session)):
        lock(db);actor=auth(db,claims);team,_=member(db,team_id,actor.id,True);now=int(time.time())
        rows=db.query(Invite,Account).join(Account,Account.id==Invite.account_id).filter(Invite.team_id==team_id).order_by(Invite.id).limit(101).all()
        return {'team_id':team_id,'team_name':team.name,'team_revision':team.revision,'items':[{'invite_id':row.id,'account_id':account.id,'username':account.username if account.state=='active' else None,'account_state':account.state,'role':row.role,'kind':row.kind,'state':'expired' if row.state=='pending' and row.expires_at<=now else row.state,'expires_at':row.expires_at} for row,account in rows[:100]],'truncated':len(rows)>100}
    @app.get('/v1/teams/{team_id}/invites/{invite_id}')
    def get_invite(team_id:str,invite_id:str,claims=Depends(bearer),db=Depends(db_session)):
        uid(invite_id);lock(db);actor=auth(db,claims);team,_=member(db,team_id,actor.id,True);row=db.get(Invite,invite_id)
        if not row or row.team_id!=team_id:raise HTTPException(404,'Invite not found')
        state='expired' if row.state=='pending' and row.expires_at<=int(time.time()) else row.state
        return {'invite_id':row.id,'account_id':row.account_id,'role':row.role,'state':state,'expires_at':row.expires_at,'team_revision':team.revision,'delivery':'not_sent'}
    @app.post('/v1/teams/{team_id}/invites')
    def invite(team_id:str,value:InviteCreate,operation_id:str=Header(...,alias='Idempotency-Key'),claims=Depends(bearer),db=Depends(db_session)):
        invite_id=uid(value.id);now=int(time.time());lock(db);actor=auth(db,claims);team,_=member(db,team_id,actor.id,True)
        account=db.query(Account).filter_by(username=value.username,state='active').first()
        if not account:raise HTTPException(404,'Recipient unavailable')
        recipient=account.id
        digest,replay=operation(db,actor,claims['jti'],operation_id,'invite.existing',{'team_id':team_id,**value.model_dump(mode='json')})
        if replay is not None:return replay
        prior=db.get(Invite,invite_id)
        if prior:
            if (prior.team_id,prior.account_id,prior.role,prior.expires_at)!=(team_id,recipient,value.role,value.expires_at) or prior.kind!='existing': raise HTTPException(409,'Invite conflict')
            return finish(db,actor,claims['jti'],operation_id,'invite.existing',digest,{'invite_id':invite_id,'account_id':recipient,'state':prior.state,'delivery':'not_sent','team_revision':team.revision},team_id=team_id,target_id=invite_id)
        if team.revision!=value.expected_revision: raise HTTPException(409,'Team changed')
        account=db.get(Account,recipient);existing=db.get(Member,(team_id,recipient))
        if not account or account.state!='active' or recipient==actor.id or existing and existing.state=='active': raise HTTPException(404,'Recipient unavailable')
        if not now<value.expires_at<=now+30*86400: raise HTTPException(422,'Invalid expiry')
        db.add(Invite(id=invite_id,team_id=team_id,account_id=recipient,role=value.role,state='pending',expires_at=value.expires_at,token_hash=None,kind='existing'))
        team.revision+=1;return finish(db,actor,claims['jti'],operation_id,'invite.existing',digest,{'invite_id':invite_id,'account_id':recipient,'state':'pending','delivery':'not_sent','team_revision':team.revision},team_id=team_id,target_id=invite_id)
    @app.post('/v1/teams/{team_id}/pending-invites')
    def pending_invite(team_id:str,value:PendingInvite,operation_id:str=Header(...,alias='Idempotency-Key'),claims=Depends(bearer),db=Depends(db_session)):
        invite_id=uid(value.id);recipient=uid(value.account_id);now=int(time.time());lock(db);actor=auth(db,claims);team,_=member(db,team_id,actor.id,True)
        digest,replay=operation(db,actor,claims['jti'],operation_id,'invite.enrollment',{'team_id':team_id,**value.model_dump(mode='json')})
        if replay is not None:return replay
        prior=db.get(Invite,invite_id)
        if prior:
            if (prior.team_id,prior.account_id,prior.role,prior.expires_at)!=(team_id,recipient,value.role,value.expires_at) or prior.kind!='enrollment':
                raise HTTPException(409,'Invite conflict')
            # A lost initial response is never reconstructed; revoke and issue a new invite.
            return finish(db,actor,claims['jti'],operation_id,'invite.enrollment',digest,{'invite_id':invite_id,'account_id':recipient,'state':prior.state,'delivery':'not_sent','token':None,'team_revision':team.revision},team_id=team_id,target_id=invite_id)
        if team.revision!=value.expected_revision:raise HTTPException(409,'Team changed')
        if db.get(Account,recipient) is not None:raise HTTPException(409,'Account identity exists')
        if not now<value.expires_at<=now+30*86400:raise HTTPException(422,'Invalid expiry')
        token=secrets.token_urlsafe(32);token_digest=hashlib.sha256(token.encode('ascii')).hexdigest()
        db.add(Account(id=recipient,username='pending:'+recipient,password_hash='',state='pending',revision=1,failed_count=0,locked_until=0))
        db.add(Invite(id=invite_id,team_id=team_id,account_id=recipient,role=value.role,state='pending',expires_at=value.expires_at,token_hash=token_digest,kind='enrollment'))
        team.revision+=1
        return finish(db,actor,claims['jti'],operation_id,'invite.enrollment',digest,{'invite_id':invite_id,'account_id':recipient,'state':'pending','delivery':'not_sent','token':token,'team_revision':team.revision},team_id=team_id,target_id=invite_id,sensitive=True)
    @app.post('/v1/activate')
    def activate(value:Activate,db=Depends(db_session)):
        invite_id=uid(value.invite_id);team_id=uid(value.team_id)
        try: raw=value.token.encode('ascii');password=value.password.encode('utf-8')
        except UnicodeError:raise HTTPException(400,'Activation unavailable') from None
        if len(password)>72 or not value.username.strip() or not value.username.isprintable() or value.username.startswith('pending:'):
            raise HTTPException(400,'Activation unavailable')
        digest=hashlib.sha256(raw).hexdigest();lock(db);row=db.get(Invite,invite_id)
        if not row or row.team_id!=team_id or row.state!='pending' or row.expires_at<=int(time.time()) or not row.token_hash or not hmac.compare_digest(row.token_hash,digest):
            raise HTTPException(400,'Activation unavailable')
        account=db.get(Account,row.account_id);team=db.get(Team,row.team_id)
        if not account or account.state!='pending' or not team or db.query(Account.id).filter_by(username=value.username).first():
            raise HTTPException(400,'Activation unavailable')
        if db.get(Member,(row.team_id,account.id)) is not None:raise HTTPException(409,'Activation unavailable')
        password_hash=bcrypt.hashpw(password,bcrypt.gensalt()).decode('ascii')
        # Expiry is checked again after the password hash and before the transaction commits.
        if row.expires_at<=int(time.time()):raise HTTPException(400,'Activation unavailable')
        account.username=value.username;account.password_hash=password_hash;account.state='active';account.revision+=1
        db.add(Member(team_id=row.team_id,account_id=account.id,role=row.role,state='active',revision=1))
        row.state='accepted';row.token_hash=None;team.revision+=1;db.commit()
        return {'account_id':account.id,'team_id':team.id,'state':'active'}
    @app.post('/v1/teams/{team_id}/invites/{invite_id}/accept')
    def accept(team_id:str,invite_id:str,operation_id:str=Header(...,alias='Idempotency-Key'),claims=Depends(bearer),db=Depends(db_session)):
        uid(team_id);uid(invite_id);lock(db);account=auth(db,claims);row=db.get(Invite,invite_id);team=db.get(Team,team_id)
        digest,replay=operation(db,account,claims['jti'],operation_id,'invite.accept',{'team_id':team_id,'invite_id':invite_id})
        if replay is not None:return replay
        if not row or not team or row.team_id!=team_id or row.account_id!=account.id: raise HTTPException(404,'Invite not found')
        prior=db.get(Member,(team_id,account.id))
        if row.state=='accepted' and prior and prior.state=='active':return finish(db,account,claims['jti'],operation_id,'invite.accept',digest,{'state':'accepted'},team_id=team_id,target_id=invite_id)
        if row.state!='pending' or row.expires_at<=int(time.time()): raise HTTPException(409,'Invite unavailable')
        if prior and prior.state=='active': raise HTTPException(409,'Already member')
        if prior:prior.role=row.role;prior.state='active';prior.revision+=1
        else:db.add(Member(team_id=team_id,account_id=account.id,role=row.role,state='active',revision=1))
        row.state='accepted';team.revision+=1;return finish(db,account,claims['jti'],operation_id,'invite.accept',digest,{'state':'accepted','team_revision':team.revision},team_id=team_id,target_id=invite_id)
    @app.post('/v1/teams/{team_id}/invites/{invite_id}/revoke')
    def revoke_invite(team_id:str,invite_id:str,value:Revision,operation_id:str=Header(...,alias='Idempotency-Key'),claims=Depends(bearer),db=Depends(db_session)):
        uid(invite_id);lock(db);account=auth(db,claims);team,_=member(db,team_id,account.id,True);row=db.get(Invite,invite_id)
        digest,replay=operation(db,account,claims['jti'],operation_id,'invite.revoke',{'team_id':team_id,'invite_id':invite_id,**value.model_dump(mode='json')})
        if replay is not None:return replay
        if not row or row.team_id!=team_id:raise HTTPException(404,'Invite not found')
        if team.revision!=value.expected_revision or row.state!='pending':raise HTTPException(409,'Invite changed')
        row.state='revoked';row.token_hash=None;team.revision+=1;team.revocation_epoch+=1;return finish(db,account,claims['jti'],operation_id,'invite.revoke',digest,{'state':'revoked','team_revision':team.revision},team_id=team_id,target_id=invite_id)
    @app.post('/v1/teams/{team_id}/members/{account_id}/revoke')
    def revoke_member(team_id:str,account_id:str,value:Revision,operation_id:str=Header(...,alias='Idempotency-Key'),claims=Depends(bearer),db=Depends(db_session)):
        uid(account_id);lock(db);actor=auth(db,claims);team,_=member(db,team_id,actor.id,True);target=db.get(Member,(team_id,account_id))
        digest,replay=operation(db,actor,claims['jti'],operation_id,'member.revoke',{'team_id':team_id,'account_id':account_id,**value.model_dump(mode='json')})
        if replay is not None:return replay
        if not target or target.state!='active':raise HTTPException(404,'Member not found')
        if team.owner_id==account_id:raise HTTPException(409,'Owner cannot be removed')
        if team.revision!=value.expected_revision:raise HTTPException(409,'Team changed')
        target.state='revoked';target.revision+=1;team.revision+=1;team.revocation_epoch+=1
        for acl in db.query(PublicationMember).join(Publication,Publication.id==PublicationMember.publication_id).filter(Publication.team_id==team_id,PublicationMember.account_id==account_id).all():
            pub=db.get(Publication,acl.publication_id);pub.revision+=1;pub.revocation_epoch+=1;db.delete(acl)
        for pending in db.query(Invite).filter_by(team_id=team_id,account_id=account_id,state='pending').all():pending.state='revoked'
        return finish(db,actor,claims['jti'],operation_id,'member.revoke',digest,{'state':'revoked','team_revision':team.revision},team_id=team_id,target_id=account_id)
    @app.get('/v1/teams/{team_id}/publications')
    def list_publications(team_id:str,claims=Depends(bearer),db=Depends(db_session)):
        lock(db);actor=auth(db,claims);member(db,team_id,actor.id)
        rows=db.query(Publication).filter_by(team_id=team_id,state='active').order_by(Publication.id).limit(501).all()
        items=[]
        for row in rows:
            acl=db.get(PublicationMember,(row.id,actor.id))
            if row.owner_id==actor.id or row.audience=='team' or acl:
                items.append({'publication_id':row.id,'owner_id':row.owner_id,'revision':row.revision,
                              'your_role':'owner' if actor.id==row.owner_id else acl.role if acl else 'collaborator'})
                if len(items)>100:break
        return {'items':items[:100],'truncated':len(items)>100 or len(rows)>500}
    @app.put('/v1/teams/{team_id}/publications/{publication_id}')
    def publication(team_id:str,publication_id:str,value:PublicationWrite,operation_id:str=Header(...,alias='Idempotency-Key'),claims=Depends(bearer),db=Depends(db_session)):
        uid(publication_id)
        if value.id!=publication_id or not value.role_text.strip() or len(value.role_text.encode('utf-8'))>32000 or len(value.knowledge_text.encode('utf-8'))>128000:
            raise HTTPException(422,'Invalid publication')
        people=[(uid(p.account_id),p.role) for p in value.people]
        if len({p[0] for p in people})!=len(people) or value.audience=='team' and any(p[1]=='collaborator' for p in people):raise HTTPException(422,'Invalid audience')
        lock(db);actor=auth(db,claims);team,_=member(db,team_id,actor.id);row=db.get(Publication,publication_id)
        digest,replay=operation(db,actor,claims['jti'],operation_id,'publication.put',{'team_id':team_id,'publication_id':publication_id,**value.model_dump(mode='json')})
        if replay is not None:return replay
        if row:
            if row.team_id!=team_id:raise HTTPException(404,'Publication not found')
            acl=db.get(PublicationMember,(publication_id,actor.id))
            if actor.id!=row.owner_id and (not acl or acl.role!='manager' or row.state=='revoked'):raise HTTPException(403,'Forbidden')
            if row.revision!=value.expected_revision:raise HTTPException(409,'Publication changed')
        elif value.expected_revision!=0:raise HTTPException(409,'Publication changed')
        for account_id,_ in people:
            member(db,team_id,account_id)
        if row:
            row.role_text=value.role_text;row.knowledge_text=value.knowledge_text;row.audience=value.audience
            row.revision+=1;row.revocation_epoch+=1;row.state='active'
            db.query(PublicationMember).filter_by(publication_id=publication_id).delete(synchronize_session=False)
        else:
            row=Publication(id=publication_id,team_id=team_id,owner_id=actor.id,role_text=value.role_text,
                knowledge_text=value.knowledge_text,audience=value.audience,revision=1,revocation_epoch=0,state='active');db.add(row)
        for account_id,role in people:db.add(PublicationMember(publication_id=publication_id,account_id=account_id,role=role))
        return finish(db,actor,claims['jti'],operation_id,'publication.put',digest,{'publication_id':publication_id,'revision':row.revision,'revocation_epoch':row.revocation_epoch},team_id=team_id,target_id=publication_id)
    @app.get('/v1/teams/{team_id}/publications/{publication_id}')
    def read_publication(team_id:str,publication_id:str,claims=Depends(bearer),db=Depends(db_session)):
        uid(publication_id);lock(db);actor=auth(db,claims);member(db,team_id,actor.id);row=db.get(Publication,publication_id)
        if not row or row.team_id!=team_id:raise HTTPException(404,'Publication not found')
        acl=db.get(PublicationMember,(publication_id,actor.id))
        if row.state!='active' and actor.id!=row.owner_id and (acl is None or acl.role!='manager'):raise HTTPException(404,'Publication not found')
        if actor.id!=row.owner_id and row.audience!='team' and acl is None:raise HTTPException(404,'Publication not found')
        result={'publication_id':row.id,'team_id':team_id,'owner_id':row.owner_id,'role_text':row.role_text,
                'knowledge_text':row.knowledge_text,'audience':row.audience,'revision':row.revision,
                'revocation_epoch':row.revocation_epoch,'state':row.state,'your_role':'owner' if actor.id==row.owner_id else acl.role if acl else 'collaborator'}
        if actor.id==row.owner_id or acl and acl.role=='manager':
            result['people']=[{'account_id':p.account_id,'role':p.role} for p in db.query(PublicationMember).filter_by(publication_id=row.id).order_by(PublicationMember.account_id).all()]
        return result
    @app.post('/v1/teams/{team_id}/publications/{publication_id}/revoke')
    def revoke_publication(team_id:str,publication_id:str,value:Revision,operation_id:str=Header(...,alias='Idempotency-Key'),claims=Depends(bearer),db=Depends(db_session)):
        uid(publication_id);lock(db);actor=auth(db,claims);member(db,team_id,actor.id);row=db.get(Publication,publication_id)
        digest,replay=operation(db,actor,claims['jti'],operation_id,'publication.revoke',{'team_id':team_id,'publication_id':publication_id,**value.model_dump(mode='json')})
        if replay is not None:return replay
        if not row or row.team_id!=team_id:raise HTTPException(404,'Publication not found')
        acl=db.get(PublicationMember,(publication_id,actor.id))
        if actor.id!=row.owner_id and (not acl or acl.role!='manager'):raise HTTPException(403,'Forbidden')
        if row.state!='active' or row.revision!=value.expected_revision:raise HTTPException(409,'Publication changed')
        row.state='revoked';row.revision+=1;row.revocation_epoch+=1
        return finish(db,actor,claims['jti'],operation_id,'publication.revoke',digest,{'publication_id':row.id,'state':'revoked','revision':row.revision},team_id=team_id,target_id=publication_id)
    return app
