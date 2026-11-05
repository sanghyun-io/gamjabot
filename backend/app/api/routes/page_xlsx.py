"""Owner-bound local XLSX generation; no filesystem or external effects."""
import re
from fastapi import APIRouter,Depends,HTTPException,Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.page_xlsx import PageXlsxRequest
from app.services import page_xlsx
router=APIRouter()
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}

def identity(request,owner,expected=None):
    provider=getattr(request.app.state,'owner_epoch_provider',None);broker=getattr(request.app.state,'auth_broker_client',None)
    try:
        epoch=provider.current(owner) if provider else None
        core=getattr(broker,'current_generation_fingerprint',None) if broker else None
    except Exception:raise HTTPException(503,{'code':'page_xlsx_owner_unavailable'}) from None
    if not isinstance(epoch,str) or not re.fullmatch(r'[a-f0-9]{64}',epoch) or not isinstance(core,str) or not re.fullmatch(r'[a-f0-9]{64}',core):
        raise HTTPException(503,{'code':'page_xlsx_owner_unavailable'})
    if expected is not None and epoch!=expected:raise HTTPException(409,{'code':'page_xlsx_owner_changed'})
    return epoch,core

def current(request,owner,binding):
    if identity(request,owner,binding[0])!=binding:raise HTTPException(409,{'code':'page_xlsx_owner_changed'})

@router.get('/{room_id}/results/{result_id}/versions/{version}/xlsx/preview')
def preview(room_id:int,result_id:str,version:int,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    binding=identity(request,owner.id)
    metadata,_=page_xlsx.prepare(db,owner.id,room_id,result_id,version,binding[0])
    page_xlsx.require_current(db,owner.id,room_id,result_id,version,metadata['document_sha256']);current(request,owner.id,binding)
    return JSONResponse(metadata,headers=HEADERS)

@router.post('/{room_id}/results/{result_id}/versions/{version}/xlsx')
def export(room_id:int,result_id:str,version:int,value:PageXlsxRequest,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    binding=identity(request,owner.id,value.expected_owner_epoch)
    metadata,data=page_xlsx.prepare(db,owner.id,room_id,result_id,version,binding[0])
    if metadata['document_sha256']!=value.expected_document_sha256 or metadata['sha256']!=value.expected_sha256:
        raise HTTPException(409,{'code':'page_xlsx_changed'})
    page_xlsx.require_current(db,owner.id,room_id,result_id,version,metadata['document_sha256']);current(request,owner.id,binding)
    return JSONResponse({**metadata,'content_base64':page_xlsx.encoded(data)},headers=HEADERS)
