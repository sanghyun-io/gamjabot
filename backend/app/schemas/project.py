"""
Project schemas
"""
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, List, Literal
from datetime import datetime
from app.schemas.agent import AgentResponse


class ProjectCreate(BaseModel):
    """Schema for creating a project"""
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=1000)


class ProjectUpdate(BaseModel):
    """Schema for updating a project"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=1000)


class ProjectResponse(BaseModel):
    """Schema for project response"""
    id: int
    user_id: int
    name: str
    description: Optional[str]
    is_active: bool
    archived_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime]
    
    class Config:
        from_attributes = True


class ProjectRoomSummary(BaseModel):
    """All owned rooms, including closed; timestamps contain no event content."""
    total_rooms: int = Field(ge=0)
    active_rooms: int = Field(ge=0)
    latest_activity_at: Optional[datetime] = None


class ProjectWithAgents(ProjectResponse):
    """Schema for project response with agents"""
    agents: List[AgentResponse] = []
    room_summary: ProjectRoomSummary | None = None


class ProjectArchiveApply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProjectArchiveCounts(BaseModel):
    active_rooms: int
    pending_triggers: int
    running_triggers: int
    running_runs: int
    waiting_runs: int
    waiting_approvals: int
    started_actions_uncertain: int
    open_actions_cancelled: int
    grants_revoked: int


class ProjectArchiveRoom(BaseModel):
    id: int
    title: str
    last_event_seq: int
    active_work_count: int


class ProjectArchivePreview(BaseModel):
    project_id: int
    plan_hash: str
    counts: ProjectArchiveCounts
    rooms: List[ProjectArchiveRoom]
    rooms_truncated: bool


class ProjectArchiveResult(BaseModel):
    project_id: int
    is_active: Literal[False]
    archived_at: datetime
    plan_hash: str
    counts: ProjectArchiveCounts
    cleanup_unconfirmed: List[int]


class ProjectRestoreResult(BaseModel):
    project_id: int
    is_active: Literal[True]
    closed_rooms: int
