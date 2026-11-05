"""Owner-wide room navigation and pin routes."""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.room_index import RoomIndexResponse, RoomPinResponse, StoredDraftResponse
from app.services import room_index


router = APIRouter()


@router.get("/index", response_model=RoomIndexResponse)
def list_owner_rooms(
    room_status: Literal["active", "closed", "all"] = Query("active", alias="status"),
    project_id: int | None = Query(None, gt=0),
    q: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, max_length=512),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return room_index.build_room_index(
        db, current_user.id, status=room_status, project_id=project_id,
        query=q, limit=limit, cursor=cursor,
    )


@router.put("/{room_id}/pin", response_model=RoomPinResponse)
def put_room_pin(
    room_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pinned = room_index.pin_room(db, current_user.id, room_id)
    return {"room_id": pinned.room_id, "pinned": True, "pinned_at": pinned.pinned_at}


@router.delete("/{room_id}/pin", status_code=status.HTTP_204_NO_CONTENT)
def delete_room_pin(
    room_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room_index.unpin_room(db, current_user.id, room_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/drafts", response_model=StoredDraftResponse)
def list_stored_room_drafts(
    room_status: Literal["active", "closed", "all"] = Query("all", alias="status"),
    q: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return room_index.build_stored_draft_index(
        db, current_user.id, status=room_status, query=q, limit=limit, cursor=cursor,
    )
