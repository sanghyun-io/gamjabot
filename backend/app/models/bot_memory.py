"""Explicit Owner memories for one Bot's canonical personal conversation."""
from sqlalchemy import (Column, Integer, String, Text, DateTime, JSON, Boolean, text,
                        ForeignKey, ForeignKeyConstraint, CheckConstraint, Index)
from app.core.database import Base


class BotMemoryEntry(Base):
    __tablename__ = "bot_memory_entries"
    memory_id = Column(String(36), primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    group_allowed = Column(Boolean, nullable=False, server_default=text("1"))
    category = Column(String(16), nullable=False)
    content = Column(Text, nullable=False)
    source_kind = Column(String(8), nullable=False)
    source_room_id = Column(Integer)
    source_event_id = Column(String(36))
    source_seq = Column(Integer)
    source_url = Column(String(2048))
    source_snapshot = Column(JSON, nullable=False)
    revision = Column(Integer, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    deleted_at = Column(DateTime)
    __table_args__ = (
        ForeignKeyConstraint(("source_room_id", "owner_id"),
                             ("conversations.id", "conversations.user_id")),
        ForeignKeyConstraint(("source_event_id", "source_room_id"),
                             ("room_events.event_id", "room_events.room_id")),
        CheckConstraint("category IN ('fact','preference','role')"),
        CheckConstraint("length(content) BETWEEN 1 AND 2000 AND revision >= 1"),
        CheckConstraint("created_by=owner_id AND updated_by=owner_id"),
        CheckConstraint("(source_kind='manual' AND source_room_id IS NULL AND source_event_id IS NULL AND source_seq IS NULL AND source_url IS NULL) OR (source_kind='event' AND source_room_id IS NOT NULL AND source_event_id IS NOT NULL AND source_seq IS NOT NULL AND source_seq >= 1 AND source_url IS NULL) OR (source_kind='link' AND source_room_id IS NULL AND source_event_id IS NULL AND source_seq IS NULL AND source_url IS NOT NULL)"),
        Index("ix_bot_memory_active", "owner_id", "agent_id", "deleted_at", "memory_id"),
    )


class BotMemoryState(Base):
    __tablename__ = "bot_memory_state"
    owner_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), primary_key=True)
    revision = Column(Integer, nullable=False, default=0)
    __table_args__ = (CheckConstraint("revision >= 0"),)


class BotMemoryCommand(Base):
    __tablename__ = "bot_memory_commands"
    owner_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    idempotency_key = Column(String(200), primary_key=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    request_digest = Column(String(64), nullable=False)
    response = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False)
    __table_args__ = (CheckConstraint("length(idempotency_key) BETWEEN 1 AND 200 AND length(request_digest)=64"),)
