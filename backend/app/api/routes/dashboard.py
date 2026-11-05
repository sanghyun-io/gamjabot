"""Owner dashboard aggregate API."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.dashboard import DashboardResponse
from app.services.dashboard import build_dashboard


router = APIRouter()


@router.get("", response_model=DashboardResponse)
def get_dashboard(
    limit: int = Query(6, ge=1, le=12),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return build_dashboard(db, current_user.id, limit)
