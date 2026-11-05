"""Owner-scoped declared readiness. Listing never resolves credentials or probes providers."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.services import readiness


router = APIRouter()


@router.get('')
async def readiness_overview(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return readiness.overview(db, user.id)
