"""Room-owned goals and explicitly configured public-page watches."""
from typing import Annotated,Literal
from uuid import UUID
from fastapi import APIRouter,Depends,HTTPException,Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.models.collaboration import RoomEvent
from app.services.room_service import require_room

router=APIRouter()
Text=Annotated[str,Field(max_length=500)]
class Criterion(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id: str=Field(pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    text: str=Field(min_length=1,max_length=500)
    done:bool=False
    evidence_seq:int|None=Field(default=None,gt=0)
class Goal(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision:int=Field(ge=0)
    title:str=Field(min_length=1,max_length=1000)
    criteria:list[Criterion]=Field(min_length=1,max_length=12)
    complete:bool=False
class Watch(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision:int|None=Field(default=None,ge=1)
    command_id:str=Field(pattern=r'^[0-9a-f]{32}$')
    name:str=Field(min_length=1,max_length=100,pattern=r'^[^\x00-\x1f\x7f]+$')
    url:str=Field(min_length=8,max_length=2048)
    interval_minutes:int=Field(ge=5,le=10080)
    timezone:str=Field(min_length=1,max_length=100)
    mode:Literal['text_change','keyword_appears','keyword_disappears']='text_change'
    keyword:str=Field(default='',max_length=200)
    stop_after_change:bool=False
    enabled:bool=False
class Control(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision:int=Field(ge=1)
    action:Literal['pause','resume','end']

def service(request):
    value=getattr(request.app.state,'room_plans',None)
    if value is None:raise HTTPException(503,{'code':'room_plan_unavailable'})
    return value

def result(data):return JSONResponse(data,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

@router.get('/{room_id}/plan')
def listing(room_id:int,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    require_room(db,room_id,owner.id,active=False);store=service(request)
    rows=db.query(RoomEvent).filter(RoomEvent.room_id==room_id,RoomEvent.kind.in_(['message','tool_result','summary','activity'])).order_by(RoomEvent.seq.desc()).limit(100).all()
    evidence=[{'seq':r.seq,'event_id':r.event_id,'kind':r.kind,'actor':r.actor_name_snapshot,'label':str(r.payload.get('content') or r.payload.get('text') or r.kind)[:160]} for r in rows]
    return result({'goal':store.goal(owner.id,room_id),'watches':store.watches(owner.id,room_id),'evidence':evidence})

@router.put('/{room_id}/plan/goal')
def goal(room_id:int,value:Goal,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    require_room(db,room_id,owner.id);return result(service(request).save_goal(owner.id,room_id,value))

@router.post('/{room_id}/plan/watches')
def add_watch(room_id:int,value:Watch,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    require_room(db,room_id,owner.id);return result(service(request).write_watch(owner.id,room_id,value))

@router.put('/{room_id}/plan/watches/{watch_id}')
def edit_watch(room_id:int,watch_id:str,value:Watch,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    require_room(db,room_id,owner.id);return result(service(request).write_watch(owner.id,room_id,value,watch_id))

@router.post('/{room_id}/plan/watches/{watch_id}/control')
def control(room_id:int,watch_id:str,value:Control,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    require_room(db,room_id,owner.id,active=value.action=='resume');return result(service(request).control(owner.id,room_id,watch_id,value))
