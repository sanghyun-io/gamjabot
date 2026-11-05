"""Primary Bot and personal-chat entry points; no dispatch on reads or resolve."""
from fastapi import APIRouter, Depends, Request, Header
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.bot_profile import BotConversationResolve, BotPrimaryWrite, BotMemoryImport
from app.schemas.primary_checkins import PrimaryCheckinWrite
from app.services import bot_profile
from app.services import room_results
from fastapi import Query
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/{agent_id}/library")
def library(agent_id: int, after: str | None = None, q: str | None = None, limit: int = Query(50, ge=1, le=50),
            db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return JSONResponse(room_results.bot_library(db, owner.id, agent_id, after=after, limit=limit, q=q),
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def _pin_store(request):
    return getattr(getattr(request.app.state, 'local_executor', None), 'library_uploads', None)

@router.get('/{agent_id}/library/pins')
def library_pins(agent_id:int, request:Request, after:str|None=None, limit:int=Query(50,ge=1,le=50),
                 db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    from app.services import library_pins as pins
    provider=request.app.state.owner_epoch_provider; lock=request.app.state.owner_recovery_lock
    with lock:epoch=pins.owner_epoch(provider,owner.id)
    result=pins.listing(db,_pin_store(request),owner.id,agent_id,after,limit)
    with lock:
        if pins.owner_epoch(provider,owner.id)!=epoch:pins.fail('pin_owner_changed',409)
    return JSONResponse(result,headers={'Cache-Control':'no-store'})

@router.get('/{agent_id}/library/pins/context')
def library_pin_context(agent_id:int, request:Request, db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    from app.services import library_pins as pins
    lock=request.app.state.owner_recovery_lock
    with lock:
        epoch=pins.owner_epoch(request.app.state.owner_epoch_provider,owner.id)
        bot_profile.owned_agent(db,owner.id,agent_id)
    return JSONResponse({'owner_id':owner.id,'owner_epoch':epoch},headers={'Cache-Control':'no-store'})

@router.post('/{agent_id}/library/pins/status')
def library_pin_status(agent_id:int, value:dict, request:Request, db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    from app.services import library_pins as pins
    if set(value)!={'source'}:pins.fail('pin_invalid')
    provider=request.app.state.owner_epoch_provider; lock=request.app.state.owner_recovery_lock
    with lock:epoch=pins.owner_epoch(provider,owner.id)
    result=pins.status(db,_pin_store(request),owner.id,agent_id,value['source'])
    with lock:
        if pins.owner_epoch(provider,owner.id)!=epoch:pins.fail('pin_owner_changed',409)
    return JSONResponse(result,headers={'Cache-Control':'no-store'})

@router.post('/{agent_id}/library/pins/operations')
def library_pin_operation(agent_id:int, value:dict, request:Request, db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    from app.services import library_pins as pins
    provider=request.app.state.owner_epoch_provider; lock=request.app.state.owner_recovery_lock
    with lock:
        epoch=pins.owner_epoch(provider,owner.id)
        if value.get('expected_owner_epoch')!=epoch:pins.fail('pin_owner_changed',409)
    return JSONResponse(pins.operate(db,_pin_store(request),owner.id,agent_id,value,provider,lock,epoch),headers={'Cache-Control':'no-store'})

@router.post('/{agent_id}/library/pins/resolve')
def library_pin_resolve(agent_id:int, value:dict, request:Request, db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    from app.services import library_pins as pins
    provider=request.app.state.owner_epoch_provider; lock=request.app.state.owner_recovery_lock
    with lock:epoch=pins.owner_epoch(provider,owner.id)
    return JSONResponse(pins.operate(db,_pin_store(request),owner.id,agent_id,value,provider,lock,epoch,resolve=True),headers={'Cache-Control':'no-store'})


@router.get("/primary")
def get_primary(db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return bot_profile.primary(db, owner.id)


@router.put("/primary")
async def set_primary(value: BotPrimaryWrite, request: Request, db: Session = Depends(get_db),
                owner: User = Depends(get_current_user)):
    result=bot_profile.set_primary(db, owner.id, value)
    request.app.state.scheduler.cancel_checkin(owner.id)
    return result


@router.post("/{agent_id}/conversation")
def resolve_conversation(agent_id: int, value: BotConversationResolve,
                         db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return bot_profile.resolve_conversation(db, owner.id, agent_id, value)


@router.get("/conversations/{room_id}")
def get_conversation_profile(room_id: int, db: Session = Depends(get_db),
                             owner: User = Depends(get_current_user)):
    return bot_profile.conversation_profile(db, owner.id, room_id)


@router.get('/primary/check-ins')
def get_checkins(db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    from app.services.primary_checkins import get
    return get(db,owner.id)

@router.put('/primary/check-ins')
async def put_checkins(value: PrimaryCheckinWrite,
                       request: Request, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    from app.services.primary_checkins import put
    result=put(db,owner.id,value)
    request.app.state.scheduler.cancel_checkin(owner.id)
    return result

@router.get('/primary/check-ins/runs')
def checkin_runs(db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    from app.services.primary_checkins import runs
    return runs(db,owner.id)


@router.post('/primary/memory-import')
async def memory_import(value: BotMemoryImport, request: Request, idempotency_key: str | None = Header(None),
                        db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    result = bot_profile.submit_memory_import(db, owner.id, value, idempotency_key)
    if not result['replayed']:
        request.app.state.event_hub.publish(result['room_id'])
    await request.app.state.scheduler.schedule_user_event(result['room_id'], result['event_id'])
    return result
