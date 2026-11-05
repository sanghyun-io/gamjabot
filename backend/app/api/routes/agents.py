"""
Agent API routes
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import cast, String
from typing import List
from datetime import timezone

from app.core.database import get_db
from app.services.agent_mutations import require_owned_connection, stage_create, stage_update
from app.api.routes.auth import get_current_user
from app.models.models import User, Agent
from app.models.shared_text_runtime import SharedTextRoom
from app.models.collaboration import ProviderConnection, RoomAgentRun, RoomGrant, ToolAction
from app.services.tool_broker import record_action_result
from app.schemas.agent import (AgentCreate, AgentUpdate, AgentResponse, InactiveAgentConnectionResponse, InactiveAgentConnectionPage, InactiveAgentDetach)

router = APIRouter()



@router.post("/", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(
    agent_data: AgentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new agent"""
    new_agent = stage_create(db, current_user.id, agent_data)
    db.commit()
    db.refresh(new_agent)
    
    return new_agent


@router.get("/", response_model=List[AgentResponse])
async def list_agents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all agents for current user"""
    agents = db.query(Agent).filter(
        Agent.user_id == current_user.id,
        Agent.is_active == True,
        ~Agent.id.in_(db.query(SharedTextRoom.agent_id))
    ).all()
    
    return agents


_inactive_reference_columns = (Agent.id, Agent.name, Agent.connection_id, Agent.model_id, Agent.model, Agent.is_active, Agent.updated_at)


@router.get('/avatars')
async def agent_avatars(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    # Include soft-inactive Agents so historical messages keep their saved face.
    return {str(row.id): row.avatar for row in db.query(Agent.id, Agent.avatar).filter(Agent.user_id == current_user.id).all()}


@router.get("/inactive-connections", response_model=InactiveAgentConnectionPage)
async def inactive_agent_connections(
    connection_id: int = Query(..., gt=0),
    after_id: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_owned_connection(db, connection_id, current_user.id)
    rows = db.query(*_inactive_reference_columns).filter(
        Agent.user_id == current_user.id, Agent.is_active == False,
        Agent.connection_id == connection_id, Agent.id > after_id,
    ).order_by(Agent.id).limit(limit + 1).all()
    return InactiveAgentConnectionPage(
        items=[InactiveAgentConnectionResponse.model_validate(row) for row in rows[:limit]],
        next_after_id=rows[limit - 1].id if len(rows) > limit else None,
    )


@router.put("/{agent_id}/inactive-connection", response_model=InactiveAgentConnectionResponse)
async def detach_inactive_agent_connection(
    agent_id: int, body: InactiveAgentDetach,
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    # Conditional SQL touches only the connection reference. Concurrent changes
    # cannot turn this into activation, deletion, or a stale full-Agent update.
    expected_at = body.expected_updated_at
    if expected_at is not None and expected_at.tzinfo is not None:
        expected_at = expected_at.astimezone(timezone.utc)
    timestamp_match = Agent.updated_at == expected_at
    if expected_at is not None and db.get_bind().dialect.name == 'sqlite':
        # SQLite server func.now() may omit fractional zeros while ORM values
        # include six digits. Match only exact equivalent textual precision,
        # never rounded milliseconds or a client-side read/update window.
        stamp = expected_at.strftime('%Y-%m-%d %H:%M:%S.%f')
        forms = {stamp}
        while stamp.endswith('0'):
            stamp = stamp[:-1]
            forms.add(stamp)
        if stamp.endswith('.'):
            forms.add(stamp[:-1])
        timestamp_match = cast(Agent.updated_at, String).in_(forms)
    count = db.query(Agent).filter(
        Agent.id == agent_id, Agent.user_id == current_user.id,
        Agent.is_active == False, Agent.connection_id == body.expected_connection_id,
        Agent.name == body.expected_name, Agent.model_id == body.expected_model_id,
        Agent.model == body.expected_model, timestamp_match,
    ).update({Agent.connection_id: None}, synchronize_session=False)
    if count != 1:
        db.rollback()
        raise HTTPException(409, detail={"code": "inactive_agent_reference_changed"})
    db.commit()
    db.expire_all()
    row = db.query(*_inactive_reference_columns).filter(Agent.id == agent_id, Agent.user_id == current_user.id).one()
    return InactiveAgentConnectionResponse.model_validate(row)


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get agent by ID"""
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
    
    return agent


@router.put("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: int,
    agent_data: AgentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update agent"""
    agent = stage_update(db, current_user.id, agent_id, agent_data)
    
    db.commit()
    db.refresh(agent)
    
    return agent


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete agent (soft delete)"""
    agent = db.query(Agent).filter(
        Agent.id == agent_id,
        Agent.user_id == current_user.id
    ).first()
    
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )
    
    # Invalidate every in-flight turn before actor authorization is withdrawn.
    rooms = [run.room_id for run in db.query(RoomAgentRun).filter_by(agent_id=agent_id).all()]
    for room_id in rooms:
        request.app.state.scheduler.cancel(room_id, agent_id)
    db.query(RoomAgentRun).filter_by(agent_id=agent_id).update({
        "state": "stopped", "active_tool": {}, "pending_task": {},
        "generation": RoomAgentRun.generation + 1},
        synchronize_session=False)
    db.query(RoomGrant).filter_by(agent_id=agent_id).update({"decision": "revoked"})
    for action in db.query(ToolAction).filter(ToolAction.agent_id == agent_id,
            ToolAction.state.in_(("pending", "waiting", "approved", "started"))).all():
        state = "uncertain" if action.state == "started" else "cancelled"
        record_action_result(db, action.action_id, state,
                             "Agent removed; effect unknown." if state == "uncertain" else "Agent removed.")
    from app.services.owner_common_memory import remove_origin
    remove_origin(db,current_user.id,agent_id)
    agent.is_active = False
    db.commit()
    executor = getattr(request.app.state.scheduler.broker, "executor", None)
    if hasattr(executor, "stop_room"):
        for room_id in rooms:
            await executor.stop_room(room_id, agent_id)
    
    return None
