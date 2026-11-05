"""The GUI and Owner CLI share these room result commands."""

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from urllib.parse import quote
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.room_result import (ResultSave, ResultVersionSave, ResultRevisionRequest, ResultStateWrite,
                                    ResultReviewWrite, ResultLinkReportWrite, PageRename)
from app.services import room_results as service

router = APIRouter()
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def write(request, db, owner, room, operation, value, key, result_id=None, version=None):
    response = service.mutate(db, owner.id, room, operation, value, key, result_id=result_id, version=version)
    if not response["replayed"]:
        request.app.state.event_hub.publish(room)
    return JSONResponse(response, status_code=200 if response["replayed"] or operation in {"state", "link-report"} else 201, headers=HEADERS)


@router.get("/{room_id}/results")
def results(room_id: int, after: str | None = None, limit: int = Query(50, ge=1, le=50),
            db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return JSONResponse(service.list_results(db, owner.id, room_id, after=after, limit=limit), headers=HEADERS)


@router.post("/{room_id}/results")
def save(room_id: int, value: ResultSave, request: Request, idempotency_key: str | None = Header(None),
         db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return write(request, db, owner, room_id, "save", value, idempotency_key)


@router.get("/{room_id}/results/{result_id}/current")
def current_result(room_id: int, result_id: str, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    service.require_room(db, room_id, owner.id, active=False)
    root=service.result_row(db,owner.id,room_id,result_id)
    return JSONResponse(service.detail(db,owner.id,room_id,result_id,root.current_version),headers=HEADERS)


@router.post("/{room_id}/results/{result_id}/rename")
def rename_page(room_id: int, result_id: str, value: PageRename, request: Request,
                idempotency_key: str | None = Header(None), db: Session = Depends(get_db),
                owner: User = Depends(get_current_user)):
    receipt = service.rename_page(db, owner.id, room_id, result_id, value, idempotency_key)
    if not receipt['replayed']:
        request.app.state.event_hub.publish(room_id)
    return JSONResponse(receipt, headers=HEADERS)


@router.get("/{room_id}/results/{result_id}/rename/receipt")
def rename_receipt(room_id: int, result_id: str, idempotency_key: str | None = Header(None),
                   db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return JSONResponse(service.page_rename_receipt(db, owner.id, room_id, result_id, idempotency_key), headers=HEADERS)


@router.get("/{room_id}/results/{result_id}/conversation")
def page_conversation(room_id: int, result_id: str, after_seq: int = Query(0,ge=0),
                      upper_seq: int | None = Query(None,ge=0), limit: int = Query(50,ge=1,le=50),
                      db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return JSONResponse(service.page_conversation(db,owner.id,room_id,result_id,after_seq=after_seq,
                                                  upper_seq=upper_seq,limit=limit),headers=HEADERS)


@router.get("/{room_id}/results/{result_id}/versions")
def versions(room_id: int, result_id: str, before: int | None = Query(None, ge=1),
             limit: int = Query(50, ge=1, le=50), db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return JSONResponse(service.list_versions(db, owner.id, room_id, result_id, before=before, limit=limit), headers=HEADERS)


@router.post("/{room_id}/results/{result_id}/versions")
def add_version(room_id: int, result_id: str, value: ResultVersionSave, request: Request,
                idempotency_key: str | None = Header(None), db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return write(request, db, owner, room_id, "version", value, idempotency_key, result_id)


@router.post("/{room_id}/results/{result_id}/revisions")
async def start_revision(room_id: int, result_id: str, value: ResultRevisionRequest, request: Request,
                         idempotency_key: str | None = Header(None), db: Session = Depends(get_db),
                         owner: User = Depends(get_current_user)):
    result = service.start_revision(db, owner.id, room_id, result_id, value, idempotency_key)
    if not result['replayed']:
        request.app.state.event_hub.publish(room_id)
        await request.app.state.scheduler.schedule_user_event(room_id, result['root_ref']['event_id'])
    return JSONResponse(result, status_code=200 if result['replayed'] else 201, headers=HEADERS)


@router.get("/{room_id}/results/{result_id}/revisions/receipt")
def revision_receipt(room_id: int, result_id: str, idempotency_key: str | None = Header(None),
                     db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return JSONResponse(service.revision_receipt(db, owner.id, room_id, result_id, idempotency_key), headers=HEADERS)


@router.get("/{room_id}/results/{result_id}/revisions/active")
def active_revision(room_id: int, result_id: str, db: Session = Depends(get_db),
                    owner: User = Depends(get_current_user)):
    return JSONResponse(service.revision_active(db, owner.id, room_id, result_id), headers=HEADERS)


@router.get("/{room_id}/results/{result_id}/revisions/{root_id}")
def revision_status(room_id: int, result_id: str, root_id: str,
                    db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return JSONResponse(service.revision_status(db, owner.id, room_id, result_id, root_id), headers=HEADERS)


@router.post("/{room_id}/results/{result_id}/revisions/{root_id}/finalize")
def finalize_revision(room_id: int, result_id: str, root_id: str, request: Request,
                      db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    result = service.finalize_revision(db, owner.id, room_id, result_id, root_id)
    if result['state'] == 'completed': request.app.state.event_hub.publish(room_id)
    return JSONResponse(result, headers=HEADERS)


@router.get("/{room_id}/results/{result_id}/versions/{version}")
def show(room_id: int, result_id: str, version: int, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return JSONResponse(service.detail(db, owner.id, room_id, result_id, version), headers=HEADERS)


@router.post("/{room_id}/results/{result_id}/versions/{version}/state")
def state(room_id: int, result_id: str, version: int, value: ResultStateWrite, request: Request,
          idempotency_key: str | None = Header(None), db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return write(request, db, owner, room_id, "state", value, idempotency_key, result_id, version)


@router.get("/{room_id}/results/{result_id}/versions/{version}/reviews")
def reviews(room_id: int, result_id: str, version: int, after: str | None = None, limit: int = Query(50, ge=1, le=50),
            db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return JSONResponse(service.list_reviews(db, owner.id, room_id, result_id, version, after=after, limit=limit), headers=HEADERS)


@router.post("/{room_id}/results/{result_id}/versions/{version}/reviews")
def link_review(room_id: int, result_id: str, version: int, value: ResultReviewWrite, request: Request,
                idempotency_key: str | None = Header(None), db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return write(request, db, owner, room_id, "review", value, idempotency_key, result_id, version)


@router.post("/{room_id}/results/{result_id}/versions/{version}/link-report")
def link_report(room_id: int, result_id: str, version: int, value: ResultLinkReportWrite, request: Request,
                idempotency_key: str | None = Header(None), db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return write(request, db, owner, room_id, "link-report", value, idempotency_key, result_id, version)


def _generated_store(request):
    from app.services.generated_files import GeneratedFileUnavailable
    store=getattr(request.app.state.local_executor,'generated_files',None)
    if store is None: raise GeneratedFileUnavailable()
    return store

@router.get('/{room_id}/results/{result_id}/versions/{version}/file')
def read_generated_file(room_id:int, result_id:str, version:int, request:Request, db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    from app.services.generated_files import GeneratedFileUnavailable, GeneratedFileGone
    action, public=service.generated_file_version(db,owner.id,room_id,result_id,version)
    if action.observation.get('generated_file_status')=='deleted': raise HTTPException(410,'File removed')
    try: data=_generated_store(request).read(owner.id,room_id,action.action_id,public)
    except GeneratedFileGone: raise HTTPException(410,'File removed') from None
    except GeneratedFileUnavailable: raise HTTPException(404,'File unavailable') from None
    return Response(data, media_type='text/plain', headers={**HEADERS,
        'Content-Disposition':"attachment; filename*=UTF-8''"+quote(public['filename'],safe='')})

@router.delete('/{room_id}/results/{result_id}/versions/{version}/file')
def delete_generated_file(room_id:int, result_id:str, version:int, request:Request, db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    from app.services.generated_files import GeneratedFileUnavailable, GeneratedFileGone
    # Validate owner and reference before acquiring a write lock; recheck under it.
    service.generated_file_version(db,owner.id,room_id,result_id,version)
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        action,public=service.generated_file_version(db,owner.id,room_id,result_id,version)
        if action.observation.get('generated_file_status')!='deleted':
            try: _generated_store(request).delete(owner.id,room_id,action.action_id,public)
            except GeneratedFileGone: pass  # A prior deletion may have committed only its file tombstone.
            except GeneratedFileUnavailable: raise HTTPException(404,'File unavailable') from None
            action.observation={**action.observation,'generated_file_status':'deleted'}
        db.commit()
    except BaseException:
        db.rollback();raise
    return JSONResponse({'removed':True},headers=HEADERS)


def _voice_memo_source(db, owner_id, room_id, action_id):
    from app.models.collaboration import RoomEvent, ToolAction
    from app.models.models import Agent
    service.require_room(db, room_id, owner_id, active=False)
    action = db.get(ToolAction, action_id)
    if action is None or action.room_id != room_id:
        raise HTTPException(404, 'Voice memo unavailable')
    event = db.query(RoomEvent).filter_by(room_id=room_id, idempotency_key='tool-result:' + action_id).first()
    if event is None:
        raise HTTPException(404, 'Voice memo unavailable')
    service.file_action(db, room_id, event)
    public = event.payload.get('voice_memo')
    agent = db.get(Agent, action.agent_id)
    if (action.family != 'synthesize_voice_memo' or not isinstance(public, dict)
            or public.get('owner_id') != owner_id or agent is None or agent.user_id != owner_id):
        raise HTTPException(404, 'Voice memo unavailable')
    return action, public


def _voice_memo_read(request, owner_id, room_id, action, public):
    from app.services.voice_memo_artifacts import VoiceMemoUnavailable, VoiceMemoGone
    if action.observation.get('voice_memo_status') == 'deleted': raise HTTPException(410, 'Voice memo removed')
    store = getattr(request.app.state.local_executor, 'voice_memos', None)
    if store is None: raise HTTPException(404, 'Voice memo unavailable')
    try: meta, data = store.read(owner_id, room_id, action.action_id)
    except VoiceMemoGone: raise HTTPException(410, 'Voice memo removed') from None
    except VoiceMemoUnavailable: raise HTTPException(404, 'Voice memo unavailable') from None
    if meta != public: raise HTTPException(404, 'Voice memo unavailable')
    return Response(data, media_type='audio/wav', headers={**HEADERS, 'Content-Disposition':'inline'})


@router.get('/{room_id}/voice-memos/{action_id}/file')
def read_voice_memo(room_id: int, action_id: str, request: Request,
                    db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    if not service.canonical_id(action_id): raise HTTPException(404, 'Voice memo unavailable')
    action, public = _voice_memo_source(db, owner.id, room_id, action_id)
    return _voice_memo_read(request, owner.id, room_id, action, public)


@router.get('/{room_id}/results/{result_id}/versions/{version}/voice-memo')
def read_voice_memo_result(room_id: int, result_id: str, version: int, request: Request,
                           db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    action, public = service.voice_memo_version(db, owner.id, room_id, result_id, version)
    return _voice_memo_read(request, owner.id, room_id, action, public)


@router.delete('/{room_id}/voice-memos/{action_id}/file')
def delete_voice_memo(room_id: int, action_id: str, request: Request,
                      db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    from app.services.voice_memo_artifacts import VoiceMemoUnavailable
    if not service.canonical_id(action_id): raise HTTPException(404, 'Voice memo unavailable')
    _voice_memo_source(db, owner.id, room_id, action_id)
    db.rollback(); db.execute(text('BEGIN IMMEDIATE'))
    try:
        action, public = _voice_memo_source(db, owner.id, room_id, action_id)
        if action.observation.get('voice_memo_status') != 'deleted':
            store = getattr(request.app.state.local_executor, 'voice_memos', None)
            if store is None: raise HTTPException(404, 'Voice memo unavailable')
            try:
                meta = store.delete(owner.id, room_id, action_id, public)
            except VoiceMemoUnavailable: raise HTTPException(404, 'Voice memo unavailable') from None
            if {**meta, 'deleted':False} != public: raise HTTPException(404, 'Voice memo unavailable')
            action.observation = {**action.observation, 'voice_memo_status':'deleted'}
        db.commit()
    except BaseException: db.rollback(); raise
    return JSONResponse({'removed':True}, headers=HEADERS)
