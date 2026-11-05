"""Owner-wide derived intervention inbox."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.attention import AttentionResponse
from app.services.attention import build_attention


router = APIRouter()


@router.get("", response_model=AttentionResponse)
def get_attention(
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, max_length=512),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return build_attention(db, current_user.id, limit=limit, cursor=cursor)
