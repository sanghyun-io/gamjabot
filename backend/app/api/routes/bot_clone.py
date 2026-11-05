"""Explicit Owner-reviewed Bot copies; preview is read-only."""
from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.bot_clone import ClonePreview, CloneCommit
from app.services import bot_clone

router=APIRouter()
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}


@router.post('/{source_id}/clone/preview')
def preview(source_id:int, value:ClonePreview, db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    return JSONResponse(bot_clone.preview(db,owner.id,source_id,value.name),headers=HEADERS)


@router.post('/{source_id}/clone')
def commit(source_id:int, value:CloneCommit, idempotency_key:str|None=Header(None),
           db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    result=bot_clone.commit(db,owner.id,source_id,value.name,value.plan_hash,idempotency_key)
    return JSONResponse(result,status_code=200 if result['replayed'] else 201,headers=HEADERS)


@router.get('/{source_id}/clone/commands/{key}')
def receipt(source_id:int,key:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return JSONResponse(bot_clone.receipt(db,owner.id,source_id,key),headers=HEADERS)
