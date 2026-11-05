"""Owner-uploaded Bot library items. No model ingestion or source path access."""
import hashlib
import re
from urllib.parse import quote, unquote_to_bytes
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.services.bot_profile import owned_agent
from app.services.library_uploads import LibraryUploadUnavailable, LibraryUploadGone, LibraryUploadConflict, MAX_BYTES

router=APIRouter()
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}


def _id(value):
    try:
        if not isinstance(value,str) or str(UUID(value))!=value: raise ValueError()
    except (ValueError,TypeError,AttributeError): raise HTTPException(422,'Invalid upload ID') from None
    return value


def _filename(value):
    if not isinstance(value,str) or not value or len(value)>1800 or re.search(r'%(?![0-9A-Fa-f]{2})',value):
        raise HTTPException(422,'Invalid filename')
    try: name=unquote_to_bytes(value).decode('utf-8','strict')
    except (UnicodeError,ValueError): raise HTTPException(422,'Invalid filename') from None
    if (not name or len(name)>200 or name in ('.','..') or '/' in name or '\\' in name
            or not name.isprintable() or any(ord(c)<32 or ord(c)==127 for c in name)):
        raise HTTPException(422,'Invalid filename')
    return name


def _store(request):
    value=getattr(request.app.state.local_executor,'library_uploads',None)
    if value is None:raise HTTPException(503,'Upload storage unavailable')
    return value


def _epoch(request,owner):
    provider=getattr(request.app.state,'owner_epoch_provider',None)
    value=provider.current(owner) if provider else None
    if not value: raise HTTPException(503,'Owner authority unavailable')
    return value


def _error(error):
    if isinstance(error,LibraryUploadGone): raise HTTPException(410,'Upload removed') from None
    if isinstance(error,LibraryUploadConflict): raise HTTPException(409,'Upload conflict') from None
    raise HTTPException(404,'Upload unavailable') from None


@router.put('/{bot_id}/library/uploads/{upload_id}')
async def put_upload(bot_id:int,upload_id:str,request:Request,
                     filename:str|None=Header(None,alias='X-Upload-Filename'),
                     digest:str|None=Header(None,alias='X-Upload-SHA256'),
                     db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    owned_agent(db,owner.id,bot_id);upload_id=_id(upload_id);name=_filename(filename)
    if not isinstance(digest,str) or not re.fullmatch(r'[a-f0-9]{64}',digest): raise HTTPException(422,'Invalid digest')
    if request.headers.get('content-type','').lower().strip()!='application/octet-stream': raise HTTPException(422,'Invalid content type')
    length=request.headers.get('content-length')
    if length is not None:
        if not length.isascii() or not length.isdigit():raise HTTPException(422,'Invalid content length')
        if int(length)>MAX_BYTES:raise HTTPException(413,'Upload too large')
    epoch=_epoch(request,owner.id);db.rollback();data=bytearray();hasher=hashlib.sha256()
    async for chunk in request.stream():
        if len(data)+len(chunk)>MAX_BYTES:raise HTTPException(413,'Upload too large')
        data.extend(chunk);hasher.update(chunk)
    if hasher.hexdigest()!=digest:raise HTTPException(422,'Digest mismatch')
    db.rollback();db.expire_all();owned_agent(db,owner.id,bot_id)
    user=db.get(User,owner.id)
    if user is None or not user.is_active:raise HTTPException(404,'Bot unavailable')
    if _epoch(request,owner.id)!=epoch:raise HTTPException(409,'Owner authority changed')
    try: result,created=_store(request).put(owner.id,bot_id,upload_id,name,bytes(data),digest)
    except (LibraryUploadUnavailable,LibraryUploadConflict) as error: _error(error)
    return JSONResponse(result,status_code=201 if created else 200,headers=HEADERS)


@router.get('/{bot_id}/library/uploads')
def list_uploads(bot_id:int,request:Request,after:str|None=None,limit:int=Query(50,ge=1,le=50),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    owned_agent(db,owner.id,bot_id)
    if after is not None:_id(after)
    try: result=_store(request).list(owner.id,bot_id,after=after,limit=limit)
    except (LibraryUploadUnavailable,LibraryUploadConflict) as error: _error(error)
    return JSONResponse({'bot_id':bot_id,**result},headers=HEADERS)


@router.get('/{bot_id}/library/uploads/{upload_id}')
def upload_detail(bot_id:int,upload_id:str,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    owned_agent(db,owner.id,bot_id);upload_id=_id(upload_id)
    try: result=_store(request).detail(owner.id,bot_id,upload_id)
    except (LibraryUploadUnavailable,LibraryUploadConflict) as error: _error(error)
    return JSONResponse(result,headers=HEADERS)


@router.get('/{bot_id}/library/uploads/{upload_id}/file')
def read_upload(bot_id:int,upload_id:str,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    owned_agent(db,owner.id,bot_id);upload_id=_id(upload_id)
    try:
        store=_store(request);meta=store.detail(owner.id,bot_id,upload_id);content=store.read(owner.id,bot_id,upload_id)
    except (LibraryUploadUnavailable,LibraryUploadConflict) as error: _error(error)
    return Response(content,media_type='application/octet-stream',headers={**HEADERS,
        'Content-Disposition':"attachment; filename*=UTF-8''"+quote(meta['filename'],safe='')})


@router.delete('/{bot_id}/library/uploads/{upload_id}')
def delete_upload(bot_id:int,upload_id:str,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    owned_agent(db,owner.id,bot_id);upload_id=_id(upload_id)
    try: result=_store(request).delete(owner.id,bot_id,upload_id)
    except (LibraryUploadUnavailable,LibraryUploadConflict) as error: _error(error)
    return JSONResponse(result,headers=HEADERS)
