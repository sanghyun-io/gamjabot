"""Owner-scoped non-secret workspace blueprint routes."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.workspace import WorkspaceApplyRequest, WorkspaceBlueprint
from app.services import workspace_blueprint


router = APIRouter()


@router.get("/blueprint")
def export_blueprint(db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    return workspace_blueprint.export_blueprint(db, current_user.id)


@router.post("/blueprint/preview")
def preview_blueprint(blueprint: WorkspaceBlueprint, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    return workspace_blueprint.preview(db, current_user.id, blueprint)


@router.post("/blueprint/apply", status_code=status.HTTP_201_CREATED)
def apply_blueprint(value: WorkspaceApplyRequest, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    return workspace_blueprint.apply(db, current_user.id, value.blueprint, value.plan_hash)

