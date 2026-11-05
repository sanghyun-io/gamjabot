"""Authenticated Owner contact reference data only."""
from fastapi import APIRouter,Depends,Header,Query,Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.vault_contacts import CreateContact,UpdateContact,DeleteContact
from app.services import vault_contacts as service

class SafeContactRoute(APIRoute):
    def get_route_handler(self):
        route=super().get_route_handler()
        async def handler(request):
            try:return await route(request)
            except RequestValidationError:return JSONResponse({'detail':{'code':'vault_contact_invalid'}},status_code=422,headers={'Cache-Control':'no-store'})
        return handler
router=APIRouter(route_class=SafeContactRoute)
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}
def response(value):return JSONResponse(value,headers=HEADERS)
def provider(request):return getattr(request.app.state,'owner_epoch_provider',None)

@router.get('/contact-commands/{key}')
def receipt(key:str,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return response(service.receipt(db,provider(request),owner.id,key))
@router.get('/contacts')
def list_contacts(request:Request,after:str|None=Query(None),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return response(service.list_items(db,provider(request),owner.id,after))
@router.get('/contacts/{item_id}')
def contact(item_id:str,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return response(service.read(db,provider(request),owner.id,item_id))
@router.post('/contacts')
def create(request:Request,value:CreateContact,idempotency_key:str|None=Header(None),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return response(service.write(db,provider(request),owner.id,'create',None,value,idempotency_key))
@router.put('/contacts/{item_id}')
def update(item_id:str,request:Request,value:UpdateContact,idempotency_key:str|None=Header(None),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return response(service.write(db,provider(request),owner.id,'update',item_id,value,idempotency_key))
@router.delete('/contacts/{item_id}')
def delete(item_id:str,request:Request,value:DeleteContact,idempotency_key:str|None=Header(None),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return response(service.write(db,provider(request),owner.id,'delete',item_id,value,idempotency_key))
