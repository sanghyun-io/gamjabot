"""Owner-only Vault metadata. Native input and secrets do not use this API."""
import re
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.vault import CreateLogin, UpdateLogin, DeleteLogin, ImportReservation
from app.services import vault_accounts
from app.services import vault_logins as service

class SafeVaultRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                return JSONResponse({'detail': {'code': 'vault_invalid'}}, status_code=422, headers={'Cache-Control':'no-store'})
        return handler


router = APIRouter(route_class=SafeVaultRoute)
HEADERS = {'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff'}


def response(value):
    return JSONResponse(value, headers=HEADERS)


@router.get('/session')
def session(request: Request, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    provider = getattr(request.app.state, 'owner_epoch_provider', None)
    try:
        epoch = provider.current(owner.id) if provider else None
        if not isinstance(epoch, str) or re.fullmatch(r'[0-9a-f]{64}', epoch) is None:
            epoch = None
        attested = bool(epoch and vault_accounts.attest(request, db, owner.id))
        if epoch is not None and provider.current(owner.id) != epoch:
            epoch, attested = None, False
    except Exception:
        epoch, attested = None, False
    return response({'version':1, 'owner_id':owner.id, 'owner_epoch':epoch, 'identity_attested':attested})


@router.get('/commands/{key}')
def command(key: str, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return response(service.receipt(db, owner.id, key))


@router.post('/import/reservations')
def reserve_import(request: Request, value: ImportReservation, idempotency_key: str | None = Header(None), db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return response(service.reserve_import(request, db, owner.id, value, idempotency_key))


@router.post('/import/reservations/{key}/bind')
def bind_import(request: Request, key: str, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return response(service.bind_import(request, db, owner.id, key))


@router.get('/logins')
def logins(request: Request, after: str | None = Query(None), db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return response(service.list_items(request, db, owner.id, after))


@router.post('/logins')
def create(request: Request, value: CreateLogin, idempotency_key: str | None = Header(None), db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return response(service.write(request, db, owner.id, 'create', None, value, idempotency_key))


@router.get('/logins/{item}')
def login(request: Request, item: str, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return response(service.read(request, db, owner.id, item))


@router.put('/logins/{item}')
def update(request: Request, item: str, value: UpdateLogin, idempotency_key: str | None = Header(None), db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return response(service.write(request, db, owner.id, 'update', item, value, idempotency_key))


@router.delete('/logins/{item}')
def delete(request: Request, item: str, value: DeleteLogin, idempotency_key: str | None = Header(None), db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return response(service.write(request, db, owner.id, 'delete', item, value, idempotency_key))
