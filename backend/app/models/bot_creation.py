"""Immutable committed conversational creation receipt; not a new authority grant."""
from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from app.core.database import Base


class BotCreation(Base):
    __tablename__='bot_creations'
    creation_id=Column(String(36),primary_key=True)
    owner_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    root_room_id=Column(Integer,ForeignKey('conversations.id'),nullable=False)
    root_request_event_id=Column(String(36),ForeignKey('room_events.event_id'),nullable=False)
    source_agent_id=Column(Integer,ForeignKey('agents.id'),nullable=False)
    source_generation=Column(Integer,nullable=False)
    call_id=Column(String(200),nullable=False)
    payload_digest=Column(String(64),nullable=False)
    source_profile_fingerprint=Column(String(64),nullable=False)
    created_agent_id=Column(Integer,ForeignKey('agents.id'),nullable=False,unique=True)
    created_room_id=Column(Integer,ForeignKey('conversations.id'),nullable=False,unique=True)
    request_event_id=Column(String(36),ForeignKey('room_events.event_id'),nullable=False)
    result_event_id=Column(String(36),ForeignKey('room_events.event_id'),nullable=False)
    response=Column(JSON,nullable=False)
    created_at=Column(DateTime,nullable=False)
    __table_args__=(UniqueConstraint('root_request_event_id','source_agent_id','call_id'),)
