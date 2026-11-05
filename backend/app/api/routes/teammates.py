"""Read the same Owner-scoped teammate roster and delivery receipts in GUI and CLI."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.api.routes.auth import get_current_user
from app.models.models import User
from app.services.teammates import list_room

router = APIRouter()


@router.get("/{conversation_id}/teammates")
def list_teammates(conversation_id: int, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    return list_room(db, conversation_id, current_user.id)
