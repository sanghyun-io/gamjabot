"""Owner-edited, room-local memory. No automatic extraction or global Agent state."""
from sqlalchemy import (Column, Integer, String, Text, DateTime, JSON, Boolean,
                        ForeignKey, ForeignKeyConstraint, CheckConstraint)
from app.core.database import Base

class RoomMemoryEntry(Base):
    __tablename__ = 'room_memory_entries'
    memory_id = Column(String(36), primary_key=True)
    room_id = Column(Integer, ForeignKey('conversations.id'), nullable=False)
    category = Column(String(16), nullable=False)
    content = Column(Text, nullable=False)
    scope = Column(String(8), nullable=False)
    target_agent_id = Column(Integer)
    source_kind = Column(String(8), nullable=False)
    source_event_id = Column(String(36))
    source_seq = Column(Integer)
    source_url = Column(String(2048))
    source_snapshot = Column(JSON, nullable=False)
    revision = Column(Integer, nullable=False)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    updated_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    deleted_at = Column(DateTime)
    __table_args__ = (
        ForeignKeyConstraint(('room_id','target_agent_id'), ('room_memberships.room_id','room_memberships.agent_id')),
        ForeignKeyConstraint(('source_event_id','room_id'), ('room_events.event_id','room_events.room_id')),
        CheckConstraint("category IN ('fact','decision','requirement')"),
        CheckConstraint("length(content) BETWEEN 1 AND 2000 AND revision >= 1"),
        CheckConstraint("(scope='room' AND target_agent_id IS NULL) OR (scope='agent' AND target_agent_id IS NOT NULL)"),
        CheckConstraint("(source_kind='manual' AND source_event_id IS NULL AND source_seq IS NULL AND source_url IS NULL) OR (source_kind='event' AND source_event_id IS NOT NULL AND source_seq IS NOT NULL AND source_seq >= 1 AND source_url IS NULL) OR (source_kind='link' AND source_url IS NOT NULL AND source_event_id IS NULL AND source_seq IS NULL)"),
    )

class RoomMemoryState(Base):
    __tablename__ = 'room_memory_state'
    room_id = Column(Integer, ForeignKey('conversations.id'), primary_key=True)
    revision = Column(Integer, nullable=False, default=0)
    common_context_after_seq = Column(Integer, nullable=False, default=0)
    __table_args__ = (CheckConstraint('revision >= 0 AND common_context_after_seq >= 0'),)

class RoomMemoryAgentState(Base):
    __tablename__ = 'room_memory_agent_state'
    room_id = Column(Integer, primary_key=True)
    agent_id = Column(Integer, primary_key=True)
    revision = Column(Integer, nullable=False, default=0)
    context_after_seq = Column(Integer, nullable=False, default=0)
    legacy_memory_disabled = Column(Boolean, nullable=False, default=False)
    __table_args__ = (
        ForeignKeyConstraint(('room_id','agent_id'), ('room_memberships.room_id','room_memberships.agent_id')),
        CheckConstraint('revision >= 0 AND context_after_seq >= 0'),
    )

class RoomMemoryCommand(Base):
    __tablename__ = 'room_memory_commands'
    owner_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    idempotency_key = Column(String(200), primary_key=True)
    room_id = Column(Integer, nullable=False)
    request_digest = Column(String(64), nullable=False)
    response = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(('room_id','owner_id'), ('conversations.id','conversations.user_id')),
        CheckConstraint('length(idempotency_key) BETWEEN 1 AND 200 AND length(request_digest)=64'),
    )
