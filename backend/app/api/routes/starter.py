from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.starter import StarterApply, StarterCommitted, StarterIntent, StarterPreview, StarterResult
from app.services import starter


router = APIRouter()


@router.post("/preview", response_model=StarterPreview)
def preview(value: StarterIntent, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return starter.preview(db, user.id, value)


@router.post("/apply", response_model=StarterResult)
def apply(value: StarterApply, idempotency_key: str = Header(alias="Idempotency-Key"),
          db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return starter.apply(db, user.id, value, idempotency_key)


@router.get("/last-result", response_model=StarterCommitted | None)
def last_result(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return starter.last_result(db, user.id)
