"""One atomic fan-out and one durable join of actual Bot outcomes."""
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from app.core.database import Base


class BotBatch(Base):
    __tablename__='bot_batches'
    batch_id=Column(String(36),primary_key=True)
    owner_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    root_room_id=Column(Integer,ForeignKey('conversations.id'),nullable=False)
    root_request_event_id=Column(String(36),ForeignKey('room_events.event_id'),nullable=False)
    sender_room_id=Column(Integer,ForeignKey('conversations.id'),nullable=False)
    sender_agent_id=Column(Integer,ForeignKey('agents.id'),nullable=False)
    sender_name=Column(String,nullable=False)
    parent_job_id=Column(String(36))
    parent_kind=Column(String(16))
    source_scope_id=Column(String(36),ForeignKey('execution_scopes.scope_id'),nullable=False)
    content=Column(Text,nullable=False)
    delivery_key=Column(String(64),nullable=False)
    state=Column(String(16),nullable=False)
    reply_state=Column(String(16),nullable=False)
    failure_code=Column(String(64))
    request_event_id=Column(String(36),ForeignKey('room_events.event_id'),nullable=False)
    return_delivery_event_id=Column(String(36),ForeignKey('room_events.event_id'))
    return_event_id=Column(String(36),ForeignKey('room_events.event_id'))
    deadline_at=Column(DateTime,nullable=False)
    created_at=Column(DateTime,nullable=False)
    updated_at=Column(DateTime,nullable=False)
    __table_args__=(UniqueConstraint('root_request_event_id','delivery_key'),)
