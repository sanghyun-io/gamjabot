"""Authenticated Owner preferences for Bot directory display only."""
from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.bot_directory import DirectoryWrite
from app.services import bot_directory as service

router=APIRouter()
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}


@router.get('')
def directory(db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    return JSONResponse(service.read(db,owner.id),headers=HEADERS)


@router.put('')
def save_directory(value:DirectoryWrite, idempotency_key:str|None=Header(None),
                   db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    return JSONResponse(service.write(db,owner.id,value,idempotency_key),headers=HEADERS)


@router.get('/commands/{key}')
def command(key:str, db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    return JSONResponse(service.receipt(db,owner.id,key),headers=HEADERS)
