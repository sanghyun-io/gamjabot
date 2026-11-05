"""Owner-only consent, activity, and room screenshot API for the desktop UI."""

import os
import stat
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.collaboration import DesktopLease, RoomGrant, ToolAction
from app.models.models import Conversation, User
from app.schemas.tool import BroadModeRequest, DecisionRequest
from app.services import room_service
from app.services.events import ActorRef, append_event
from app.services.grants import broad_state, decide, revoke, set_broad


router = APIRouter()

PNG_LIMIT = 10_000_000


def _screenshot_record(db: Session, room_id: int, action_id: UUID, owner_id: int):
    room_service.require_room(db, room_id, owner_id, active=False)
    action = db.get(ToolAction, str(action_id))
    expected = f"rooms/{room_id}/screenshots/{action_id}.png"
    observation = dict(action.observation or {}) if action is not None else {}
    if (action is None or action.room_id != room_id or action.family not in {"screenshot", "browser_screenshot"}
            or action.state != "completed" or observation.get("screenshot_ref") != expected):
        raise HTTPException(404, "Screenshot not found")
    return action, observation


class _ArtifactAbsent(Exception):
    pass


def _private_directory(info) -> bool:
    owner = os.getuid() if hasattr(os, "getuid") else info.st_uid
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == owner
            and stat.S_IMODE(info.st_mode) & 0o077 == 0)


def _private_artifact(info) -> bool:
    owner = os.getuid() if hasattr(os, "getuid") else info.st_uid
    return (stat.S_ISREG(info.st_mode) and info.st_uid == owner
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
            and 24 <= info.st_size <= PNG_LIMIT)


@contextmanager
def _screenshot_directory(request: Request, room_id: int):
    root = Path(request.app.state.local_executor.desktop.app_data)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    bindings: list[tuple[int, str, int, int]] = []
    try:
        root_fd = os.open(root, flags)
        descriptors.append(root_fd)
        if not _private_directory(os.fstat(root_fd)):
            raise HTTPException(404, "Screenshot not found")
        parent = root_fd
        for name in ("rooms", str(room_id), "screenshots"):
            entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not _private_directory(entry):
                raise HTTPException(404, "Screenshot not found")
            child = os.open(name, flags, dir_fd=parent)
            descriptors.append(child)
            opened = os.fstat(child)
            if (not _private_directory(opened)
                    or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)):
                raise HTTPException(404, "Screenshot not found")
            bindings.append((parent, name, opened.st_dev, opened.st_ino))
            parent = child
        for parent_fd, name, device, inode in bindings:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (device, inode) or not _private_directory(current):
                raise HTTPException(404, "Screenshot not found")
        yield parent
    except FileNotFoundError as error:
        raise _ArtifactAbsent from error
    except HTTPException:
        raise
    except OSError as error:
        raise HTTPException(404, "Screenshot not found") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_screenshot(request: Request, room_id: int, action_id: UUID) -> bytes:
    try:
        with _screenshot_directory(request, room_id) as directory_fd:
            name = f"{action_id}.png"
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
            try:
                info = os.fstat(descriptor)
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                identity = (info.st_dev, info.st_ino)
                if (not _private_artifact(info) or identity != (entry.st_dev, entry.st_ino)
                        or identity != (current.st_dev, current.st_ino)):
                    raise HTTPException(404, "Screenshot not found")
                chunks, remaining = [], PNG_LIMIT + 1
                while remaining:
                    chunk = os.read(descriptor, min(1_048_576, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                image = b"".join(chunks)
                if len(image) != info.st_size or image[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
                    raise HTTPException(404, "Screenshot not found")
                return image
            finally:
                os.close(descriptor)
    except _ArtifactAbsent as error:
        raise HTTPException(404, "Screenshot not found") from error
    except OSError as error:
        raise HTTPException(404, "Screenshot not found") from error


def _unlink_screenshot(request: Request, room_id: int, action_id: UUID, *, missing_ok: bool) -> None:
    try:
        with _screenshot_directory(request, room_id) as directory_fd:
            name = f"{action_id}.png"
            try:
                entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                if missing_ok:
                    return
                raise HTTPException(404, "Screenshot not found")
            descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
            try:
                opened = os.fstat(descriptor)
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                identity = (opened.st_dev, opened.st_ino)
                if (not _private_artifact(opened) or identity != (entry.st_dev, entry.st_ino)
                        or identity != (current.st_dev, current.st_ino)):
                    raise HTTPException(404, "Screenshot not found")
                os.unlink(name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            finally:
                os.close(descriptor)
    except _ArtifactAbsent as error:
        if missing_ok:
            return
        raise HTTPException(404, "Screenshot not found") from error
    except HTTPException:
        raise
    except OSError as error:
        raise HTTPException(500, "Screenshot deletion could not be confirmed") from error


@router.get("/rooms/{room_id}/actions/{action_id}/screenshot")
def screenshot(room_id: int, action_id: UUID, request: Request, db: Session = Depends(get_db),
               owner: User = Depends(get_current_user)):
    _, observation = _screenshot_record(db, room_id, action_id, owner.id)
    if observation.get("artifact_state") == "deleted":
        raise HTTPException(410, "Image removed")
    image = _read_screenshot(request, room_id, action_id)
    return Response(image, media_type="image/png", headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="screenshot-{action_id}.png"',
    })


@router.delete("/rooms/{room_id}/actions/{action_id}/screenshot", status_code=204)
def delete_screenshot(room_id: int, action_id: UUID, request: Request,
                      db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    action, observation = _screenshot_record(db, room_id, action_id, owner.id)
    deleted = observation.get("artifact_state") == "deleted"
    if not deleted:
        _read_screenshot(request, room_id, action_id)
        action.observation = {**observation, "artifact_state": "deleted",
                              "artifact_deleted_at": datetime.now(timezone.utc).isoformat()}
        db.commit()
    _unlink_screenshot(request, room_id, action_id, missing_ok=deleted)
    return Response(status_code=204)


@router.get("/desktop/lease")
def desktop_lease(request: Request, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    """Current host owner, with room identity visible only to that room's owner."""
    current = request.app.state.local_executor.desktop.lease.current_owner()
    row = db.get(DesktopLease, 1)
    room = db.get(Conversation, row.room_id) if row else None
    visible = bool(current and row and room and room.user_id == owner.id
                   and (row.room_id, row.agent_id, row.workflow_id) ==
                   (current["room_id"], current["agent_id"], current["workflow_id"]))
    return {"active": bool(current), "owner": current if visible else None,
            "another_profile_or_room": bool(current and not visible)}


@router.get("/rooms/{room_id}/actions/{action_id}/preview")
def preview_action(room_id: int, action_id: str, request: Request,
                   db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    room_service.require_room(db, room_id, owner.id)
    action = db.get(ToolAction, action_id)
    if action is None or action.room_id != room_id:
        raise HTTPException(404, "Action not found")
    from app.services.action_review import ReviewChanged
    try:
        with request.app.state.owner_recovery_lock:
            preview = request.app.state.scheduler.broker.preview(action, owner_id=owner.id)
    except (ReviewChanged, ValueError):
        raise HTTPException(409, "Action review changed; inspect the current request again") from None
    if preview is None:
        raise HTTPException(404, "Action preview unavailable")
    return JSONResponse(preview, headers={"Cache-Control": "no-store"})


@router.get("/rooms/{room_id}/activity")
def activity(room_id: int, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    from app.services.execution_scopes import public_binding
    from app.services.owner_routines import public_action_binding
    from app.models.mcp import MCPOwnerDraft
    room_service.require_room(db, room_id, owner.id, active=False)
    actions = db.query(ToolAction).filter_by(room_id=room_id).order_by(ToolAction.requested_at, ToolAction.action_id).all()
    drafts={row.action_id:row for row in db.query(MCPOwnerDraft).filter_by(owner_id=owner.id,room_id=room_id).all()
            if type(row.revision) is int and row.revision>0}
    return [{"action_id": row.action_id, "room_id": row.room_id, "agent_id": row.agent_id,
             "trigger_event_id": row.trigger_event_id,
             "actor_type": "agent", "actor_id": row.agent_id,
             "family": row.family, "target": row.target, "expected_effect": row.expected_effect,
             "mcp_binding": row.mcp_binding,
             "arguments": row.arguments, "state": row.state, "scope": row.approval_scope,
             "grant_id": row.grant_id, "requested_at": row.requested_at, "started_at": row.started_at,
             "completed_at": row.completed_at, "review_id": row.review_id,
             "review_status": "stored" if row.review_id and row.review_snapshot else "unavailable",
             "approved_at": row.approved_at, "approval_owner_id": row.approval_owner_id, "send_image_to_model": row.send_image_to_model,
             "owner_draft": ({"revision":drafts[row.action_id].revision,"pending":bool(drafts[row.action_id].pending),
                              "applied":bool(drafts[row.action_id].applied_review_id)}
                             if row.family=='mcp_tool_call' and row.action_id in drafts else None),
             **public_binding(db, row), **public_action_binding(db,row)} for row in actions]


@router.get("/rooms/{room_id}/grants")
def grants(room_id: int, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    room_service.require_room(db, room_id, owner.id, active=False)
    rows = db.query(RoomGrant).filter_by(room_id=room_id).order_by(RoomGrant.id).all()
    return [{"id": row.id, "room_id": row.room_id, "agent_id": row.agent_id,
             "scope": row.scope.get("kind"), "families": row.scope.get("families", []),
             "targets": row.scope.get("targets", []), "decision": row.decision,
             "expires_at": row.expires_at} for row in rows]


@router.post("/rooms/{room_id}/actions/{action_id}/decision")
def decide_action(room_id: int, action_id: str, data: DecisionRequest, request: Request,
                  db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    broker = request.app.state.scheduler.broker
    with request.app.state.owner_recovery_lock:
        result = decide(db, action_id, room_id, owner.id, data.decision, data.scope, broker.session_id,
            review_id=data.review_id, acknowledge_unverified_context=data.acknowledge_unverified_context, send_image_to_model=data.send_image_to_model, broker=broker)
    request.app.state.event_hub.publish(room_id)
    action = db.get(ToolAction, action_id)
    if action and action.execution_binding:
        request.app.state.event_hub.publish(action.execution_binding['root_room_id'])
    return result


@router.post("/rooms/{room_id}/grants/{grant_id}/revoke", status_code=204)
async def revoke_grant(room_id: int, grant_id: int, request: Request, db: Session = Depends(get_db),
                 owner: User = Depends(get_current_user)):
    revoke(db, grant_id, room_id, owner.id)
    await request.app.state.local_executor.stop_grant(grant_id)
    append_event(db, room_id, ActorRef("user", owner.id), "activity",
                 {"grant_id": grant_id, "status": "revoked"})
    request.app.state.event_hub.publish(room_id)


@router.get("/broad-mode")
def get_broad_mode(request: Request, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return broad_state(db, owner.id, request.app.state.scheduler.broker.session_id)


@router.put("/broad-mode")
def put_broad_mode(data: BroadModeRequest, request: Request, db: Session = Depends(get_db),
                   owner: User = Depends(get_current_user)):
    result = set_broad(db, owner.id, request.app.state.scheduler.broker.session_id,
                       data.enabled, data.expires_minutes, data.always)
    # A visible owner setting is separate from grants minted for individual actions.
    return result


# Saved browser profiles are Owner-managed state, separate from Agent tool consent.
from pydantic import BaseModel, ConfigDict, Field
from typing import Annotated, Literal


class BrowserProfileWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1,max_length=100,pattern=r'^[^\x00-\x1f\x7f]+$')
    account_label: str = Field(default='',max_length=100,pattern=r'^[^\x00-\x1f\x7f]*$')
    agent_ids: list[Annotated[int,Field(gt=0)]] = Field(default_factory=list,max_length=16)
    origins: list[Annotated[str,Field(max_length=2048)]] = Field(min_length=1,max_length=8)
    revision: int | None = Field(default=None,ge=1)


class BrowserProfileControl(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['takeover','return','close','reset']
    revision: int = Field(ge=1)


class BrowserProfileRoomBinding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    room_id: int = Field(gt=0)
    agent_ids: list[Annotated[int,Field(gt=0)]] = Field(min_length=1,max_length=16)


class BrowserProfileSharing(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    bindings: list[BrowserProfileRoomBinding] = Field(max_length=8)
    allow_delegated: bool = Field(default=False,strict=True)


class VaultHandoffReturn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    handoff_id: str = Field(pattern=r'^[0-9a-f]{32}$', strict=True)
    revision: int = Field(ge=1, strict=True)


class VaultHandoffRecover(VaultHandoffReturn):
    confirm_return: Literal[True]


def _profiles(request):
    executor=getattr(request.app.state,'local_executor',None)
    browser=getattr(executor,'browser',None)
    if browser is None:raise HTTPException(503,{'code':'browser_profile_unavailable'})
    return browser.profiles


def _profile_epoch(store,owner_id):
    epochs=store.browser.owner_epochs
    return epochs.current(owner_id) if epochs else None


def _same_profile_epoch(store,owner_id,expected):
    if store.browser.owner_epochs and (not expected or _profile_epoch(store,owner_id)!=expected):
        raise HTTPException(409,{'code':'browser_profile_authority_unavailable'})


@router.get('/rooms/{room_id}/browser-profiles')
def browser_profiles(room_id:int,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    room_service.require_room(db,room_id,owner.id,active=False)
    store=_profiles(request)
    return {'items':[store.status(m,room_id) for m in store.list(owner.id,room_id)]}


@router.get('/rooms/{room_id}/browser-profiles/{profile_id}/vault-handoff')
async def vault_handoff_get(room_id:int,profile_id:str,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    from app.services.vault_profile_handoff import view
    room_service.require_room(db,room_id,owner.id,active=False)
    return await view(_profiles(request),owner.id,room_id,profile_id)


@router.post('/rooms/{room_id}/browser-profiles/{profile_id}/vault-handoff/return')
async def vault_handoff_return(room_id:int,profile_id:str,value:VaultHandoffReturn,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    from app.services.vault_profile_handoff import finish
    room_service.require_room(db,room_id,owner.id,active=False);db.rollback()
    return await finish(_profiles(request),owner.id,room_id,profile_id,value.handoff_id,value.revision)

@router.post('/rooms/{room_id}/browser-profiles/{profile_id}/vault-handoff/seal')
async def vault_handoff_seal(room_id:int,profile_id:str,value:VaultHandoffReturn,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    from app.services.vault_profile_handoff import custody_seal
    room_service.require_room(db,room_id,owner.id)
    return await custody_seal(_profiles(request),owner.id,room_id,profile_id,value.handoff_id,value.revision)

@router.get('/rooms/{room_id}/browser-profiles/{profile_id}/vault-handoff/custody')
async def vault_handoff_custody(room_id:int,profile_id:str,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    from app.services.vault_profile_handoff import custody_status
    room_service.require_room(db,room_id,owner.id,active=False)
    return await custody_status(_profiles(request),owner.id,room_id,profile_id)


@router.post('/rooms/{room_id}/browser-profiles/{profile_id}/vault-handoff/recover')
async def vault_handoff_recover(room_id:int,profile_id:str,value:VaultHandoffRecover,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    from app.services.vault_profile_handoff import finish
    room_service.require_room(db,room_id,owner.id,active=False);db.rollback()
    return await finish(_profiles(request),owner.id,room_id,profile_id,value.handoff_id,value.revision,recover=True)


@router.get('/rooms/{room_id}/browser-profiles/{profile_id}/sharing')
def browser_profile_sharing(room_id:int,profile_id:str,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    from app.models.models import Agent,Project
    from app.models.collaboration import RoomMembership
    from app.services.tools.browser_profiles import assigned_agents
    room_service.require_room(db,room_id,owner.id,active=False);store=_profiles(request)
    metadata=store.read(profile_id,owner.id,room_id)
    if metadata['room_id']!=room_id:raise HTTPException(409,{'code':'browser_profile_home_room_required'})
    rooms=[]
    owned=db.query(Conversation).join(Project,Project.id==Conversation.project_id).filter(
        Conversation.user_id==owner.id,Conversation.is_active==True,Project.user_id==owner.id,
        Project.is_active==True,Conversation.id!=room_id).order_by(Conversation.id).all()
    for room in owned:
        profiles=store.list(owner.id,room.id)
        bound={aid:m['id'] for m in profiles for aid in assigned_agents(m,room.id)}
        others=[m for m in profiles if m['id']!=profile_id]
        room_available=len(others)<8 and all(m['name'].casefold()!=metadata['name'].casefold() for m in others)
        agents=db.query(Agent).join(RoomMembership,RoomMembership.agent_id==Agent.id).filter(
            RoomMembership.room_id==room.id,RoomMembership.is_active==True,
            Agent.user_id==owner.id,Agent.is_active==True).order_by(Agent.id).all()
        rooms.append({'room_id':room.id,'title':room.title,'agents':[{'id':a.id,'name':a.name,
            'available':room_available and bound.get(a.id) in (None,profile_id)} for a in agents]})
    return {'profile_id':profile_id,'revision':metadata['revision'],'home_room_id':room_id,'allow_delegated':metadata.get('allow_delegated') is True,
        'bindings':[{'room_id':int(r),'agent_ids':list(ids)} for r,ids in sorted(metadata.get('shared_rooms',{}).items(),key=lambda pair:int(pair[0]))],
        'rooms':rooms}


@router.post('/rooms/{room_id}/browser-profiles/{profile_id}/sharing')
async def update_browser_profile_sharing(room_id:int,profile_id:str,value:BrowserProfileSharing,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    room_service.require_room(db,room_id,owner.id);store=_profiles(request);epoch=_profile_epoch(store,owner.id)
    async with store.operations:
        _same_profile_epoch(store,owner.id,epoch)
        revision=value.revision
        if profile_id in store.browser.parked:
            current=store.read(profile_id,owner.id,room_id)
            if current['room_id']!=room_id:raise HTTPException(403,{'code':'browser_profile_home_room_required'})
            if current['revision']!=revision:raise HTTPException(409,{'code':'browser_profile_changed'})
            await store.close_profile(current)
            _same_profile_epoch(store,owner.id,epoch)
            fresh=store.read(profile_id,owner.id,room_id)
            if fresh['revision']!=revision+1 or any(fresh.get(k)!=current.get(k) for k in set(fresh)|set(current) if k not in ('revision','last_used_at')):raise HTTPException(409,{'code':'browser_profile_changed'})
            revision=fresh['revision']
        return store.share(db,owner.id,room_id,profile_id,revision,[item.model_dump() for item in value.bindings],allow_delegated=value.allow_delegated)


@router.post('/rooms/{room_id}/browser-profiles')
async def create_browser_profile(room_id:int,value:BrowserProfileWrite,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    store=_profiles(request);epoch=_profile_epoch(store,owner.id)
    async with store.operations:
        _same_profile_epoch(store,owner.id,epoch)
        return store.put(db,owner.id,room_id,value)


@router.patch('/rooms/{room_id}/browser-profiles/{profile_id}')
async def edit_browser_profile(room_id:int,profile_id:str,value:BrowserProfileWrite,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    store=_profiles(request);epoch=_profile_epoch(store,owner.id)
    async with store.operations:
        _same_profile_epoch(store,owner.id,epoch)
        if profile_id in store.browser.parked:
            current=store.read(profile_id,owner.id,room_id)
            if current['room_id']!=room_id:raise HTTPException(403,{'code':'browser_profile_home_room_required'})
            if current['revision']!=value.revision:raise HTTPException(409,{'code':'browser_profile_changed'})
            await store.close_profile(current)
            _same_profile_epoch(store,owner.id,epoch)
            fresh=store.read(profile_id,owner.id,room_id)
            if fresh['revision']!=value.revision+1 or any(fresh.get(k)!=current.get(k) for k in set(fresh)|set(current) if k not in ('revision','last_used_at')):raise HTTPException(409,{'code':'browser_profile_changed'})
            value=value.model_copy(update={'revision':fresh['revision']})
        return store.put(db,owner.id,room_id,value,profile_id)


@router.post('/rooms/{room_id}/browser-profiles/{profile_id}/control')
async def browser_profile_control(room_id:int,profile_id:str,value:BrowserProfileControl,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    room_service.require_room(db,room_id,owner.id,active=value.action=='takeover');store=_profiles(request);epoch=_profile_epoch(store,owner.id)
    async with store.operations:
        _same_profile_epoch(store,owner.id,epoch)
        m=store.read(profile_id,owner.id,room_id)
        if value.revision!=m['revision']:raise HTTPException(409,{'code':'browser_profile_changed'})
        if value.action=='reset' and m['room_id']!=room_id:raise HTTPException(409,{'code':'browser_profile_home_room_required'})
        db.rollback()
        try:
            if value.action=='takeover':return await store.owner_open(m,expected_epoch=epoch,room_id=room_id)
            if value.action=='reset':return await store.delete(m,reset=True,expected_epoch=epoch,room_id=room_id)
            if value.action=='return':await store.return_profile(m)
            else:await store.close_profile(m)
            return store.status(store.read(profile_id,owner.id,room_id),room_id)
        except HTTPException:raise
        except Exception:raise HTTPException(409,{'code':'browser_profile_operation_failed'}) from None


@router.delete('/rooms/{room_id}/browser-profiles/{profile_id}')
async def delete_browser_profile(room_id:int,profile_id:str,revision:int,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    room_service.require_room(db,room_id,owner.id,active=False);store=_profiles(request);epoch=_profile_epoch(store,owner.id)
    async with store.operations:
        _same_profile_epoch(store,owner.id,epoch)
        m=store.read(profile_id,owner.id,room_id)
        if revision!=m['revision']:raise HTTPException(409,{'code':'browser_profile_changed'})
        if m['room_id']!=room_id:raise HTTPException(409,{'code':'browser_profile_home_room_required'})
        db.rollback();return await store.delete(m,expected_epoch=epoch,room_id=room_id)


@router.get('/rooms/{room_id}/browser-profiles/{profile_id}/preview')
async def browser_profile_preview(room_id:int,profile_id:str,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user),revision:int|None=None):
    room_service.require_room(db,room_id,owner.id,active=False);store=_profiles(request)
    metadata=store.read(profile_id,owner.id,room_id);epoch=_profile_epoch(store,owner.id);pair=store.session(profile_id);db.rollback()
    if not epoch:raise HTTPException(409,{'code':'browser_profile_authority_unavailable'})
    if revision is not None and revision!=metadata['revision']:raise HTTPException(409,{'code':'browser_profile_changed'})
    if pair is None:raise HTTPException(409,{'code':'browser_profile_disconnected'})
    session=pair[1];page=session.page;context_id=session.context_id;controller=session.controller
    def current():
        _same_profile_epoch(store,owner.id,epoch)
        latest=store.read(profile_id,owner.id,room_id);live=store.session(profile_id)
        if latest['revision']!=metadata['revision']:raise HTTPException(409,{'code':'browser_profile_changed'})
        if (live is None or live[1] is not session or session.page is not page or session.context_id!=context_id
                or session.controller!=controller or session.closing or page.is_closed() or not session.authority.active()):
            raise HTTPException(409,{'code':'browser_profile_disconnected'})
    async with session.lock:
        current()
        try:image=await page.screenshot(type='jpeg',quality=60,full_page=False,timeout=5000)
        except Exception:raise HTTPException(409,{'code':'browser_profile_preview_failed'}) from None
        current()
    return Response(content=image,media_type='image/jpeg',headers={'Cache-Control':'no-store','X-Captured-At':datetime.now(timezone.utc).isoformat(),
        'X-Browser-Profile-Id':profile_id,'X-Browser-Profile-Revision':str(metadata['revision']),'X-Browser-Context-Id':context_id})


# Durable demonstrations never automatically resume their collector after restart.
def _demonstrations(request):
    from app.services.browser_demonstration import Demonstrations
    profiles = _profiles(request)
    if not hasattr(profiles, 'demonstrations'):
        profiles.demonstrations = Demonstrations(profiles)
    return profiles.demonstrations

from app.schemas.browser_demonstration import StartDemonstration, SaveDemonstration

@router.post('/rooms/{room_id}/browser-demonstrations')
async def demonstration_start(room_id:int, value:StartDemonstration, request:Request, key:str=Header(alias='Idempotency-Key'), db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    return await _demonstrations(request).start(db, owner.id, room_id, value, key)

@router.get('/rooms/{room_id}/browser-demonstrations/commands/{key}')
def demonstration_command(room_id:int, key:str, request:Request, db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    room_service.require_room(db, room_id, owner.id, active=False)
    return _demonstrations(request).command(owner.id, room_id, key)

@router.get('/rooms/{room_id}/browser-demonstrations')
def demonstration_list(room_id:int, request:Request, after_demo_id:str|None=None, db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    room_service.require_room(db,room_id,owner.id,active=False)
    return _demonstrations(request).list(owner.id,room_id,after_demo_id)

@router.get('/rooms/{room_id}/browser-demonstrations/{demo_id}')
def demonstration_get(room_id:int, demo_id:str, request:Request, db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    room_service.require_room(db, room_id, owner.id, active=False)
    service = _demonstrations(request)
    return service.view(service.owned(demo_id, owner.id, room_id))

@router.post('/rooms/{room_id}/browser-demonstrations/{demo_id}/stop')
async def demonstration_stop(room_id:int, demo_id:str, request:Request, db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    room_service.require_room(db, room_id, owner.id)
    service = _demonstrations(request)
    return await service.stop(service.owned(demo_id, owner.id, room_id))

@router.post('/rooms/{room_id}/browser-demonstrations/{demo_id}/cancel')
async def demonstration_cancel(room_id:int, demo_id:str, request:Request, db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    room_service.require_room(db, room_id, owner.id, active=False)
    service = _demonstrations(request)
    async with service.lock:
        item = service.owned(demo_id, owner.id, room_id)
        if item['state'] != 'recording': raise HTTPException(409, {'code':'browser_demonstration_not_recording'})
        service.interrupt(item)
        if item.get('watcher'): item['watcher'].cancel()
        await service.cleanup(item)
        if item['state'] != 'interrupted': raise HTTPException(409, {'code':'browser_demonstration_not_recording'})
        item['finished_at'] = time.monotonic()
        return service.view(item)

@router.post('/rooms/{room_id}/browser-demonstrations/{demo_id}/save')
async def demonstration_save(room_id:int, demo_id:str, value:SaveDemonstration, request:Request, key:str=Header(alias='Idempotency-Key'), db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    room_service.require_room(db, room_id, owner.id)
    service = _demonstrations(request)
    return await service.save(db, service.owned(demo_id, owner.id, room_id), value, key)

from app.schemas.browser_demonstration import DraftPreflight, DraftSubmission

@router.post('/rooms/{room_id}/browser-demonstrations/{demo_id}/draft/preflight')
def demonstration_draft_preflight(room_id:int, demo_id:str, value:DraftPreflight, request:Request, db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    from app.services import browser_demo_draft
    return browser_demo_draft.preflight(db, owner.id, room_id, demo_id, value, _demonstrations(request), getattr(request.app.state, 'owner_epoch_provider', None))

@router.post('/rooms/{room_id}/browser-demonstrations/{demo_id}/draft')
async def demonstration_draft_submit(room_id:int, demo_id:str, value:DraftSubmission, request:Request, key:str=Header(alias='Idempotency-Key'), db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    from app.services import browser_demo_draft
    result = browser_demo_draft.submit(db, owner.id, room_id, demo_id, value, key, _demonstrations(request), getattr(request.app.state, 'owner_epoch_provider', None))
    state = browser_demo_draft.read(db, owner.id, room_id, demo_id, key) if result.get('replayed') else None
    if state is None or state.get('status') == 'submitted':
        await request.app.state.scheduler.schedule_user_event(room_id, result['event_id'])
    return result

@router.get('/rooms/{room_id}/browser-demonstrations/{demo_id}/draft')
def demonstration_draft_read(room_id:int, demo_id:str, key:str=Header(alias='Idempotency-Key'), db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    from app.services import browser_demo_draft
    return browser_demo_draft.read(db, owner.id, room_id, demo_id, key)

@router.post('/rooms/{room_id}/browser-demonstrations/{demo_id}/draft/stop')
def demonstration_draft_stop(room_id:int, demo_id:str, request:Request, key:str=Header(alias='Idempotency-Key'), db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    from app.services import browser_demo_draft
    return browser_demo_draft.stop(db, owner.id, room_id, demo_id, key, request.app.state.scheduler)


from app.schemas.browser_demonstration import DemonstrationReview

@router.put('/rooms/{room_id}/browser-demonstrations/{demo_id}/review')
def demonstration_review(room_id:int, demo_id:str, value:DemonstrationReview, request:Request, key:str=Header(alias='Idempotency-Key'), db:Session=Depends(get_db), owner:User=Depends(get_current_user)):
    room_service.require_room(db,room_id,owner.id)
    service=_demonstrations(request)
    return service.update_review(db,service.owned(demo_id,owner.id,room_id),value,key)


# Draft values are Owner-submitted private content. No endpoint starts an action.
from pydantic import BaseModel,ConfigDict,StrictInt,StrictStr
class MCPDraftSave(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_revision:StrictInt
    expected_review_id:StrictStr
    edits:dict
class MCPDraftApply(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_revision:StrictInt
    expected_review_id:StrictStr

def _draft_broker(request):
    broker=getattr(request.app.state.scheduler,'broker',None)
    if broker is None or not hasattr(broker,'_draft_applied'):
        raise HTTPException(503,{'code':'mcp_draft_unavailable'})
    return broker

@router.get('/rooms/{room_id}/actions/{action_id}/draft')
def get_mcp_draft(room_id:int,action_id:UUID,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    from app.services import mcp_drafts
    return JSONResponse(mcp_drafts.read(db,getattr(request.app.state.scheduler,'broker',None),owner.id,room_id,str(action_id)),headers={'Cache-Control':'no-store'})

@router.put('/rooms/{room_id}/actions/{action_id}/draft')
def save_mcp_draft(room_id:int,action_id:UUID,value:MCPDraftSave,request:Request,key:str=Header(alias='Idempotency-Key'),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    from app.services import mcp_drafts
    return JSONResponse(mcp_drafts.save(db,_draft_broker(request),owner.id,room_id,str(action_id),value.expected_revision,value.expected_review_id,value.edits,key),headers={'Cache-Control':'no-store'})

@router.post('/rooms/{room_id}/actions/{action_id}/draft/apply')
def apply_mcp_draft(room_id:int,action_id:UUID,value:MCPDraftApply,request:Request,key:str=Header(alias='Idempotency-Key'),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    from app.services import mcp_drafts
    return JSONResponse(mcp_drafts.apply(db,_draft_broker(request),owner.id,room_id,str(action_id),value.expected_revision,value.expected_review_id,key),headers={'Cache-Control':'no-store'})

@router.get('/rooms/{room_id}/actions/{action_id}/draft/commands/{key}')
def mcp_draft_command(room_id:int,action_id:UUID,key:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    from app.services import mcp_drafts
    return JSONResponse(mcp_drafts.command(db,owner.id,room_id,str(action_id),key),headers={'Cache-Control':'no-store'})
