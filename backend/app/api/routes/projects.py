"""
Project API routes
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session
from typing import List, Literal

from app.core.database import get_db
from app.api.routes.auth import get_current_user
from app.models.models import User, Project, Agent, ProjectAgent, Conversation
from app.models.collaboration import RoomEvent
from app.schemas.project import (ProjectArchiveApply, ProjectArchivePreview, ProjectArchiveResult,
                                 ProjectCreate, ProjectRestoreResult, ProjectUpdate,
                                 ProjectResponse, ProjectWithAgents)
from app.schemas.conversation import ConversationResponse
from app.schemas.room_template import RoomTemplateInstantiate, RoomTemplateResponse, RoomTemplateWrite
from app.services import room_template_service
from app.services import project_archive

router = APIRouter()


@router.post("/{project_id}/archive/preview", response_model=ProjectArchivePreview)
def preview_project_archive(project_id: int, db: Session = Depends(get_db),
                            current_user: User = Depends(get_current_user)):
    return project_archive.preview(db, project_id, current_user.id)


@router.post("/{project_id}/archive", response_model=ProjectArchiveResult)
async def archive_project(project_id: int, value: ProjectArchiveApply, request: Request,
                          db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    durable = project_archive.apply(db, project_id, current_user.id, value.plan_hash)
    room_ids = list(durable.pop("room_ids", ()))
    unconfirmed = []
    for room_id in room_ids:
        try:
            request.app.state.scheduler.cancel(room_id)
            await request.app.state.local_executor.stop_room(room_id)
            request.app.state.event_hub.publish(room_id)
        except Exception:
            unconfirmed.append(room_id)
    return {**durable, "cleanup_unconfirmed": unconfirmed}


@router.post("/{project_id}/restore", response_model=ProjectRestoreResult)
def restore_project(project_id: int, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    return project_archive.restore(db, project_id, current_user.id)


@router.get("/{project_id}/room-templates", response_model=List[RoomTemplateResponse])
def list_room_templates(project_id: int, db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    return room_template_service.list_templates(db, project_id, current_user.id)


@router.post("/{project_id}/room-templates", response_model=RoomTemplateResponse,
             status_code=status.HTTP_201_CREATED)
def create_room_template(project_id: int, value: RoomTemplateWrite,
                         db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return room_template_service.create_template(db, project_id, current_user.id, value)


@router.get("/{project_id}/room-templates/{template_id}", response_model=RoomTemplateResponse)
def get_room_template(project_id: int, template_id: int, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    return room_template_service.get_template(db, project_id, template_id, current_user.id)


@router.put("/{project_id}/room-templates/{template_id}", response_model=RoomTemplateResponse)
def update_room_template(project_id: int, template_id: int, value: RoomTemplateWrite,
                         db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return room_template_service.update_template(db, project_id, template_id, current_user.id, value)


@router.delete("/{project_id}/room-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_room_template(project_id: int, template_id: int, db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    room_template_service.delete_template(db, project_id, template_id, current_user.id)


@router.post("/{project_id}/room-templates/{template_id}/rooms",
             response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
def instantiate_room_template(project_id: int, template_id: int, _body: RoomTemplateInstantiate,
                              db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return room_template_service.instantiate_template(db, project_id, template_id, current_user.id)


@router.post("/", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    project_data: ProjectCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new project"""
    new_project = Project(
        user_id=current_user.id,
        name=project_data.name,
        description=project_data.description
    )
    
    db.add(new_project)
    db.commit()
    db.refresh(new_project)
    
    return new_project


def _project_room_summaries(db: Session, owner_id: int, project_ids: list[int]):
    """Read-only all-room counts and latest canonical activity (closed rooms included).

    Match Dashboard's latest-sequence-per-room rule. A room with no events uses
    its creation time, never Project/Conversation.updated_at or a truncated feed.
    Both project and room ownership are required even for inconsistent legacy rows.
    """
    if not project_ids:
        return {}
    latest_seq = db.query(
        RoomEvent.room_id.label("room_id"), func.max(RoomEvent.seq).label("seq"),
    ).join(Conversation, Conversation.id == RoomEvent.room_id).join(
        Project, Project.id == Conversation.project_id,
    ).filter(Conversation.user_id == owner_id, Project.user_id == owner_id,
             Project.id.in_(project_ids)).group_by(RoomEvent.room_id).subquery()
    rows = db.query(
        Conversation.project_id,
        func.count(Conversation.id),
        func.sum(case((Conversation.is_active.is_(True), 1), else_=0)),
        func.max(func.coalesce(RoomEvent.created_at, Conversation.created_at)),
    ).join(Project, Project.id == Conversation.project_id).outerjoin(
        latest_seq, latest_seq.c.room_id == Conversation.id,
    ).outerjoin(RoomEvent, and_(RoomEvent.room_id == latest_seq.c.room_id,
                              RoomEvent.seq == latest_seq.c.seq)).filter(
        Conversation.user_id == owner_id, Project.user_id == owner_id,
        Project.id.in_(project_ids),
    ).group_by(Conversation.project_id).all()
    return {project_id: {"total_rooms": total, "active_rooms": active,
                         "latest_activity_at": latest}
            for project_id, total, active, latest in rows}


@router.get("/", response_model=List[ProjectWithAgents])
async def list_projects(
    status_filter: Literal["active", "archived", "all"] = Query("active", alias="status"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all projects for current user"""
    query = db.query(Project).filter(Project.user_id == current_user.id)
    if status_filter != "all":
        query = query.filter(Project.is_active.is_(status_filter == "active"))
    projects = query.order_by(Project.id).all()
    summaries = _project_room_summaries(db, current_user.id, [p.id for p in projects])
    
    result = []
    for project in projects:
        # Get agents for this project
        agents = db.query(Agent).join(ProjectAgent).filter(
            ProjectAgent.project_id == project.id,
            Agent.is_active == True
        ).all()
        
        project_dict = {
            "id": project.id,
            "user_id": project.user_id,
            "name": project.name,
            "description": project.description,
            "is_active": project.is_active,
            "archived_at": project.archived_at,
            "created_at": project.created_at,
            "updated_at": project.updated_at,
            "agents": agents,
            "room_summary": summaries.get(project.id, {
                "total_rooms": 0, "active_rooms": 0, "latest_activity_at": None}),
        }
        result.append(project_dict)
    
    return result


@router.get("/{project_id}", response_model=ProjectWithAgents)
async def get_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get project by ID"""
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.user_id == current_user.id,
    ).first()
    
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    # Get agents for this project
    agents = db.query(Agent).join(ProjectAgent).filter(
        ProjectAgent.project_id == project.id,
        Agent.is_active == True
    ).all()
    
    return {
        "id": project.id,
        "user_id": project.user_id,
        "name": project.name,
        "description": project.description,
        "is_active": project.is_active,
        "archived_at": project.archived_at,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "agents": agents
    }


@router.put("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: int,
    project_data: ProjectUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update project"""
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.user_id == current_user.id,
        Project.is_active.is_(True),
    ).first()
    
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    # Update fields
    update_data = project_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(project, field, value)
    
    db.commit()
    db.refresh(project)
    
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete project (soft delete)"""
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.user_id == current_user.id,
        Project.is_active.is_(True),
    ).first()
    
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    plan = project_archive.preview(db, project_id, current_user.id)
    if plan["counts"]["active_rooms"]:
        raise HTTPException(status_code=409, detail={"code": "project_archive_preview_required"})
    project_archive.apply(db, project_id, current_user.id, plan["plan_hash"])
    
    return None


@router.post("/{project_id}/agents/{agent_id}", status_code=status.HTTP_201_CREATED)
async def add_agent_to_project(
    project_id: int,
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Add agent to project"""
    # Check project exists
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.user_id == current_user.id,
        Project.is_active.is_(True),
    ).first()
    
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    # Check agent exists
    agent = db.query(Agent).filter(
        Agent.id == agent_id,
        Agent.user_id == current_user.id,
        Agent.is_active == True
    ).first()
    
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    
    # Check if already assigned
    existing = db.query(ProjectAgent).filter(
        ProjectAgent.project_id == project_id,
        ProjectAgent.agent_id == agent_id
    ).first()
    
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Agent already assigned to project"
        )
    
    # Add agent to project
    project_agent = ProjectAgent(
        project_id=project_id,
        agent_id=agent_id
    )
    
    db.add(project_agent)
    db.commit()
    
    return {"message": "Agent added to project"}


@router.delete("/{project_id}/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_agent_from_project(
    project_id: int,
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Remove agent from project"""
    # Check project exists
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.user_id == current_user.id,
        Project.is_active.is_(True),
    ).first()
    
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    # Check assignment exists
    project_agent = db.query(ProjectAgent).filter(
        ProjectAgent.project_id == project_id,
        ProjectAgent.agent_id == agent_id
    ).first()
    
    if not project_agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not assigned to project"
        )
    
    db.delete(project_agent)
    db.commit()
    
    return None
