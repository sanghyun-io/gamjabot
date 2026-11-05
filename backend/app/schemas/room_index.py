"""Safe response contracts for owner-wide room navigation."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class RoomIndexItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room_id: int
    room_title: str
    project_id: int
    project_name: str
    room_active: bool
    project_active: bool
    effective_active: bool
    read_only: bool
    pinned: bool
    pinned_at: datetime | None
    last_event_seq: int
    last_activity_at: datetime
    unread_authored: int
    waiting_approvals: int
    active_failures: int
    attention_count: int
    work_status: Literal["idle", "running", "waiting", "paused"]


class RoomIndexResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RoomIndexItem]
    next_cursor: str | None


class RoomPinResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room_id: int
    pinned: Literal[True]
    pinned_at: datetime


class StoredDraftItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room_id: int
    room_title: str
    project_id: int
    project_name: str
    read_only: bool
    version: int
    character_count: int
    preview: str
    updated_at: datetime


class StoredDraftResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[StoredDraftItem]
    next_cursor: str | None
