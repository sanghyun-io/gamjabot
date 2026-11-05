"""Owner-only card metadata; no PAN or security code is accepted."""
from fastapi import APIRouter,Depends,Header,Query,Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.vault_cards import CreateCard,UpdateCard,DeleteCard,ConfirmCard
from app.services import vault_cards as service
class SafeCardRoute(APIRoute):
    def get_route_handler(self):
        route=super().get_route_handler()
        async def handler(request):
            try:return await route(request)
            except RequestValidationError:return JSONResponse({'detail':{'code':'vault_card_invalid'}},status_code=422,headers={'Cache-Control':'no-store'})
        return handler
router=APIRouter(route_class=SafeCardRoute)
def response(value):return JSONResponse(value,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
def provider(request):return getattr(request.app.state,'owner_epoch_provider',None)
@router.get('/card-commands/{key}')
def receipt(key:str,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):return response(service.receipt(db,provider(request),owner.id,key))
@router.get('/cards')
def listing(request:Request,after:str|None=Query(None),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):return response(service.list_items(db,provider(request),owner.id,after))
@router.get('/cards/{item}')
def read(item:str,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):return response(service.read(db,provider(request),owner.id,item))
@router.post('/cards')
def create(request:Request,value:CreateCard,idempotency_key:str|None=Header(None),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):return response(service.write(db,provider(request),owner.id,'create',None,value,idempotency_key))
@router.put('/cards/{item}')
def update(item:str,request:Request,value:UpdateCard,idempotency_key:str|None=Header(None),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):return response(service.write(db,provider(request),owner.id,'update',item,value,idempotency_key))
@router.post('/cards/{item}/confirm')
def confirm(item:str,request:Request,value:ConfirmCard,idempotency_key:str|None=Header(None),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):return response(service.write(db,provider(request),owner.id,'confirm',item,value,idempotency_key))
@router.delete('/cards/{item}')
def delete(item:str,request:Request,value:DeleteCard,idempotency_key:str|None=Header(None),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):return response(service.write(db,provider(request),owner.id,'delete',item,value,idempotency_key))
