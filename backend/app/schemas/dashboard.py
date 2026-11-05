"""Owner dashboard aggregate response schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class DashboardSetup(BaseModel):
    next_action: Literal["connection", "agent", "project", "room"] | None


class DashboardCounts(BaseModel):
    enabled_connections: int
    ready_agents: int
    ready_projects: int
    active_rooms: int
    unread_authored: int
    waiting_approvals: int
    active_failures: int


class DashboardRoom(BaseModel):
    room_id: int
    room_title: str
    personal_agent_id: int | None = None
    project_id: int
    project_name: str
    last_event_seq: int
    last_activity_at: datetime
    last_authored_seq: int | None
    last_authored_at: datetime | None
    last_authored_preview: str | None
    unread_authored: int
    waiting_approvals: int
    active_failures: int
    work_status: Literal["idle", "running", "waiting", "paused"]


class DashboardResponse(BaseModel):
    setup: DashboardSetup
    counts: DashboardCounts
    rooms: list[DashboardRoom]
