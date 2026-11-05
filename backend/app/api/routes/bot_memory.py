"""Owner API; no Agent-facing memory-write or automatic extraction endpoint."""
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.bot_memory import BotMemoryWrite, BotMemoryEdit, BotMemoryDelete
from app.schemas.owner_common_memory import MemoryPolicyWrite,MemoryVisibilityWrite,OwnerCommonAdd,OwnerCommonEdit,OwnerCommonDelete
from app.services import bot_memory as service

router = APIRouter()
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def command(request, db, owner, agent_id, operation, value, key, memory_id=None):
    result = service.mutate(db, owner.id, agent_id, operation, value, key, memory_id)
    if not result["replayed"] and result.get("personal_room_id") is not None:
        request.app.state.event_hub.publish(result["personal_room_id"])
    return JSONResponse(result, headers=HEADERS)


@router.get("/{agent_id}/memories")
def listing(agent_id: int, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return JSONResponse(service.listing(db, owner.id, agent_id), headers=HEADERS)


@router.get("/{agent_id}/memories/{memory_id}")
def detail(agent_id: int, memory_id: str, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return JSONResponse(service.detail(db, owner.id, agent_id, memory_id), headers=HEADERS)


@router.post("/{agent_id}/memories")
def add(agent_id: int, value: BotMemoryWrite, request: Request,
        idempotency_key: str | None = Header(None), db: Session = Depends(get_db),
        owner: User = Depends(get_current_user)):
    return command(request, db, owner, agent_id, "add", value, idempotency_key)


@router.patch("/{agent_id}/memories/{memory_id}")
def edit(agent_id: int, memory_id: str, value: BotMemoryEdit, request: Request,
         idempotency_key: str | None = Header(None), db: Session = Depends(get_db),
         owner: User = Depends(get_current_user)):
    return command(request, db, owner, agent_id, "edit", value, idempotency_key, memory_id)


@router.delete("/{agent_id}/memories/{memory_id}")
def delete(agent_id: int, memory_id: str, value: BotMemoryDelete, request: Request,
           idempotency_key: str | None = Header(None), db: Session = Depends(get_db),
           owner: User = Depends(get_current_user)):
    return command(request, db, owner, agent_id, "delete", value, idempotency_key, memory_id)


@router.get("/{agent_id}/persistence/{receipt_id}")
def persistence_receipt(agent_id: int, receipt_id: str, db: Session = Depends(get_db),
                        owner: User = Depends(get_current_user)):
    from app.services.bot_persistence import get
    return JSONResponse(get(db, owner.id, agent_id, receipt_id), headers=HEADERS)


@router.get('/{agent_id}/memory-policy')
def policy_get(agent_id:int,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    from app.services.memory_policy import get
    return JSONResponse(get(db,owner.id,agent_id),headers=HEADERS)

@router.put('/{agent_id}/memory-policy')
def policy_put(agent_id:int,value:MemoryPolicyWrite,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    from app.services.memory_policy import set_policy
    return JSONResponse(set_policy(db,owner.id,agent_id,value),headers=HEADERS)

@router.patch('/{agent_id}/memories/{memory_id}/visibility')
def visibility(agent_id:int,memory_id:str,value:MemoryVisibilityWrite,idempotency_key:str|None=Header(None),
               db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return JSONResponse(service.visibility(db,owner.id,agent_id,memory_id,value,idempotency_key),headers=HEADERS)

@router.get('/memory/common')
def common_list(db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    from app.services.owner_common_memory import listing
    return JSONResponse(listing(db,owner.id),headers=HEADERS)

@router.post('/memory/common')
def common_add(value:OwnerCommonAdd,idempotency_key:str|None=Header(None),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    from app.services.owner_common_memory import mutate
    return JSONResponse(mutate(db,owner.id,'add',value,idempotency_key),headers=HEADERS)

@router.patch('/memory/common/{fact_id}')
def common_edit(fact_id:str,value:OwnerCommonEdit,idempotency_key:str|None=Header(None),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    from app.services.owner_common_memory import mutate
    return JSONResponse(mutate(db,owner.id,'edit',value,idempotency_key,fact_id),headers=HEADERS)

@router.delete('/memory/common/{fact_id}')
def common_delete(fact_id:str,value:OwnerCommonDelete,idempotency_key:str|None=Header(None),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    from app.services.owner_common_memory import mutate
    return JSONResponse(mutate(db,owner.id,'delete',value,idempotency_key,fact_id),headers=HEADERS)
