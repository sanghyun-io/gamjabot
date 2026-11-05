"""Reactions acknowledge messages and never enqueue an Agent turn."""
from fastapi import APIRouter,Depends,Header,Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.message_reactions import MessageReactionWrite
from app.services import message_reactions as service

router=APIRouter()
def out(value):return JSONResponse(value,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

@router.get('/{room_id}/messages/{event_id}/reaction')
def get_reaction(room_id:int,event_id:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.read(db,owner.id,room_id,event_id))

@router.get('/{room_id}/messages/{event_id}/reaction/commands/{key}')
def get_command(room_id:int,event_id:str,key:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.receipt(db,owner.id,room_id,event_id,key))

@router.put('/{room_id}/messages/{event_id}/reaction')
def put_reaction(room_id:int,event_id:str,value:MessageReactionWrite,request:Request,
                 key:str=Header(alias='Idempotency-Key'),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    result,created=service.write(db,owner.id,room_id,event_id,value,key)
    if created:request.app.state.event_hub.publish(room_id)
    return out(result)
