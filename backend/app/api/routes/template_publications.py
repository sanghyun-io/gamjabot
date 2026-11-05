"""Owner-authenticated publication operations. No token is returned or logged."""
from fastapi import APIRouter,Depends,Header,HTTPException,Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.api.routes.bot_templates import body,HEADERS
from app.core.database import get_db
from app.models.models import User
from app.schemas.template_publications import PublisherConfig,PublicationPreview,PublicationCommand,RemoteAdopt
from app.services import template_publications as service

router=APIRouter()

def _store(request):return getattr(request.app.state,'template_publisher_secret_store',None)
def _call(function,*args):
    try:return function(*args)
    except HTTPException:raise
    except Exception:raise HTTPException(503,{'code':'template_publication_unavailable'}) from None

@router.get('/config')
def config(db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return JSONResponse(_call(service.read_config,db,owner.id),headers=HEADERS)

@router.post('/config')
async def save_config(request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    value=await body(request,PublisherConfig)
    result=await run_in_threadpool(_call,service.set_config,db,owner.id,value,_store(request))
    return JSONResponse(result,headers=HEADERS)

@router.get('/{bot_id}')
def publication(bot_id:int,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return JSONResponse(_call(service.read_publication,db,owner.id,bot_id),headers=HEADERS)

@router.post('/{bot_id}/preview')
async def preview(bot_id:int,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    value=await body(request,PublicationPreview)
    result=await run_in_threadpool(_call,service.preview,db,owner.id,bot_id,value)
    return JSONResponse(result,headers=HEADERS)

@router.post('/{bot_id}/commands')
async def command(bot_id:int,request:Request,idempotency_key:str|None=Header(None),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    value=await body(request,PublicationCommand)
    result=await run_in_threadpool(_call,service.command,db,owner.id,bot_id,value,idempotency_key,_store(request))
    return JSONResponse(result,headers=HEADERS)

@router.get('/{bot_id}/commands/{key}')
def receipt(bot_id:int,key:str,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return JSONResponse(_call(service.receipt,db,owner.id,bot_id,key,_store(request)),headers=HEADERS)

@router.get('/{bot_id}/remote')
def remote(bot_id:int,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return JSONResponse(_call(service.remote_publication,db,owner.id,bot_id,_store(request)),headers=HEADERS)


@router.post('/{bot_id}/remote/adopt')
async def adopt(bot_id:int,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    value=await body(request,RemoteAdopt)
    result=await run_in_threadpool(_call,service.adopt_remote,db,owner.id,bot_id,value,_store(request))
    return JSONResponse(result,headers=HEADERS)
