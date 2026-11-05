"""Owner API for the same core used by the public CLI."""
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.room_memory import MemoryWrite,MemoryEdit,MemoryDelete,MemoryLegacy
from app.services import room_memory as service
router=APIRouter()
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}

def command(request,db,owner,room,op,value,key,mid=None,agent=None):
    result=service.mutate(db,owner.id,room,op,value,key,mid,agent)
    if not result['replayed']:request.app.state.event_hub.publish(room)
    return JSONResponse(result,headers=HEADERS)

@router.get('/{room_id}/memories')
def listing(room_id:int, db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return JSONResponse(service.listing(db,owner.id,room_id),headers=HEADERS)

@router.get('/{room_id}/memories/{memory_id}')
def detail(room_id:int,memory_id:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return JSONResponse(service.detail(db,owner.id,room_id,memory_id),headers=HEADERS)

@router.post('/{room_id}/memories')
def add(room_id:int,value:MemoryWrite,request:Request,idempotency_key:str|None=Header(None),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return command(request,db,owner,room_id,'add',value,idempotency_key)

@router.patch('/{room_id}/memories/{memory_id}')
def edit(room_id:int,memory_id:str,value:MemoryEdit,request:Request,idempotency_key:str|None=Header(None),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return command(request,db,owner,room_id,'edit',value,idempotency_key,memory_id)

@router.delete('/{room_id}/memories/{memory_id}')
def delete(room_id:int,memory_id:str,value:MemoryDelete,request:Request,idempotency_key:str|None=Header(None),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return command(request,db,owner,room_id,'delete',value,idempotency_key,memory_id)

@router.post('/{room_id}/memories/legacy/{agent_id}')
def legacy(room_id:int,agent_id:int,value:MemoryLegacy,request:Request,idempotency_key:str|None=Header(None),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return command(request,db,owner,room_id,'legacy',value,idempotency_key,agent=agent_id)
