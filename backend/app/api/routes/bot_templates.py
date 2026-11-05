"""Owner-only portable templates; these endpoints do not publish files or links."""
import json
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from pydantic import ValidationError
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.bot_templates import Export, ImportPreview, ImportCommit, PreviewLink
from app.services import bot_templates, bot_template_links

router=APIRouter()
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}

def _object(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('duplicate')
        result[key]=value
    return result

def _constant(_): raise ValueError('invalid number')

async def body(request,cls):
    data=bytearray()
    try:
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data)>512*1024: bot_templates.fail('too_large')
        value=json.loads(data,object_pairs_hook=_object,parse_constant=_constant)
        return cls.model_validate(value)
    except (ValidationError,ValueError,UnicodeError,RecursionError,TypeError):
        bot_templates.fail('invalid')

@router.post('/preview-export')
async def export(request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    value=await body(request,Export)
    return JSONResponse(bot_templates.export(db,owner.id,value.source_id),headers=HEADERS)

@router.post('/preview-link')
async def preview_link(request:Request,owner:User=Depends(get_current_user)):
    value=await body(request,PreviewLink)
    result=await run_in_threadpool(bot_template_links.retrieve,value.url)
    return JSONResponse(result,headers=HEADERS)

@router.post('/preview-import')
async def preview(request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    value=await body(request,ImportPreview)
    return JSONResponse(bot_templates.preview(db,owner.id,value),headers=HEADERS)

@router.post('/import')
async def commit(request:Request,idempotency_key:str|None=Header(None),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    value=await body(request,ImportCommit)
    result=bot_templates.commit(db,owner.id,value,idempotency_key)
    return JSONResponse(result,status_code=200 if result['replayed'] else 201,headers=HEADERS)

@router.get('/import/commands/{key}')
def receipt(key:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return JSONResponse(bot_templates.receipt(db,owner.id,key),headers=HEADERS)
