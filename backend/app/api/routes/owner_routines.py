from fastapi import APIRouter,Depends,Header,Request,HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.owner_routines import RoutineWrite,RoutineEdit,RoutineTest,RoutineDelete,RoutineWebhookKey
from app.services import owner_routines as service
router=APIRouter()
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}
def out(value):return JSONResponse(value,headers=HEADERS)
@router.get('/bots/{agent_id}/routines')
def listing(agent_id:int,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.listing(db,owner.id,agent_id))
@router.post('/bots/{agent_id}/routines')
def create(agent_id:int,value:RoutineWrite,key:str=Header(alias='Idempotency-Key'),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.create(db,owner.id,agent_id,value,key))
@router.get('/bots/{agent_id}/routines/commands/{key}')
def command(agent_id:int,key:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.command(db,owner.id,agent_id,key))
@router.get('/bots/{agent_id}/routines/{routine_id}')
def get(agent_id:int,routine_id:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.dto(db,service.owned(db,owner.id,agent_id,routine_id)))
@router.put('/bots/{agent_id}/routines/{routine_id}')
def update(agent_id:int,routine_id:str,value:RoutineEdit,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    result=service.update(db,owner.id,agent_id,routine_id,value);request.app.state.scheduler.cancel_routine(owner.id,routine_id=routine_id)
    return out(result)
@router.get('/bots/{agent_id}/routines/{routine_id}/runs')
def runs(agent_id:int,routine_id:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.runs(db,owner.id,agent_id,routine_id))
@router.get('/bots/{agent_id}/routines/{routine_id}/runs/{run_id}')
def read_run(agent_id:int,routine_id:str,run_id:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.read_run(db,owner.id,agent_id,routine_id,run_id))
@router.post('/bots/{agent_id}/routines/{routine_id}/test')
async def test(agent_id:int,routine_id:str,value:RoutineTest,request:Request,key:str=Header(alias='Idempotency-Key'),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    if len(request.app.state.scheduler._scheduled_runs)>=4:
        from app.models.owner_routines import OwnerRoutineCommand
        if db.get(OwnerRoutineCommand,(owner.id,key)) is None:
            raise HTTPException(409,{'code':'routine_capacity_busy'})
    result,created=service.test_claim(db,owner.id,agent_id,routine_id,value,key)
    if created:request.app.state.scheduler.start_routine(result['run']['run_id'])
    return out(result)
@router.get('/bots/{agent_id}/routine-persistence/{receipt_id}')
def receipt(agent_id:int,receipt_id:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.receipt(db,owner.id,agent_id,receipt_id))

@router.delete('/bots/{agent_id}/routines/{routine_id}')
def delete(agent_id:int,routine_id:str,value:RoutineDelete,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    result=service.delete(db,owner.id,agent_id,routine_id,value);request.app.state.scheduler.cancel_routine(owner.id,routine_id=routine_id)
    return out(result)

@router.post('/bots/{agent_id}/routines/{routine_id}/webhook-key')
def issue_webhook_key(agent_id:int,routine_id:str,value:RoutineWebhookKey,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    result=service.webhook_key_change(db,owner.id,agent_id,routine_id,value,True)
    request.app.state.scheduler.cancel_routine(owner.id,routine_id=routine_id)
    return out(result)

@router.delete('/bots/{agent_id}/routines/{routine_id}/webhook-key')
def revoke_webhook_key(agent_id:int,routine_id:str,value:RoutineWebhookKey,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    result=service.webhook_key_change(db,owner.id,agent_id,routine_id,value,False)
    request.app.state.scheduler.cancel_routine(owner.id,routine_id=routine_id)
    return out(result)

@router.get('/bots/{agent_id}/routines/{routine_id}/webhook-events/{event_key}')
def webhook_receipt(agent_id:int,routine_id:str,event_key:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.webhook_receipt(db,owner.id,agent_id,routine_id,event_key))

@router.post('/routine-webhooks/{routine_id}')
async def webhook(routine_id:str,request:Request,db:Session=Depends(get_db)):
    import json
    authorization=request.headers.get('authorization','')
    token=authorization[7:] if authorization.startswith('Bearer ') else ''
    service.webhook_auth(db,routine_id,token)
    event_key=request.headers.get('idempotency-key','');service.key_valid(event_key)
    chunks=[];size=0
    async for chunk in request.stream():
        size+=len(chunk)
        if size>16384:raise HTTPException(413,{'code':'routine_webhook_body_too_large'})
        chunks.append(chunk)
    try:
        raw=b''.join(chunks)
        body=json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(ValueError())) if raw else None
        json.dumps(body,ensure_ascii=False,allow_nan=False).encode('utf-8')
    except (ValueError,UnicodeError,RecursionError,OverflowError):raise HTTPException(422,{'code':'routine_webhook_invalid_body'})
    scheduler=request.app.state.scheduler
    result,created=service.webhook_claim(db,routine_id,token,event_key,body,len(scheduler._scheduled_runs)<4)
    if created:scheduler.start_routine(result['run_id'])
    return out(result)
