"""Owner Agent workload overview."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.agent_workload import AgentWorkloadResponse
from app.services.agent_workload import build_agent_workloads


router = APIRouter()


@router.get("", response_model=AgentWorkloadResponse)
def get_agent_workloads(
    agent_id: int | None = Query(None, ge=1),
    limit: int = Query(25, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return build_agent_workloads(db, current_user.id, agent_id=agent_id, limit=limit, cursor=cursor)
