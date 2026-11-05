"""Private memories owned by one canonical-recipient shared room binding."""
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, CheckConstraint, Index
from app.core.database import Base

class SharedPrivateMemoryState(Base):
    __tablename__='shared_private_memory_state'
    room_id=Column(Integer,ForeignKey('shared_text_rooms.room_id'),primary_key=True)
    revision=Column(Integer,nullable=False)
    context_after_seq=Column(Integer,nullable=False)
    __table_args__=(CheckConstraint('revision>=0'),CheckConstraint('context_after_seq>=0'))

class SharedPrivateMemoryEntry(Base):
    __tablename__='shared_private_memory_entries'
    memory_id=Column(String(36),primary_key=True)
    room_id=Column(Integer,ForeignKey('shared_text_rooms.room_id'),nullable=False)
    category=Column(String(16),nullable=False)
    content=Column(Text,nullable=False)
    revision=Column(Integer,nullable=False)
    source_event_id=Column(String(36),ForeignKey('room_events.event_id'),nullable=False)
    source_seq=Column(Integer,nullable=False)
    created_at=Column(DateTime,nullable=False)
    updated_at=Column(DateTime,nullable=False)
    deleted_at=Column(DateTime)
    __table_args__=(CheckConstraint("category IN ('fact','preference')"),
        CheckConstraint('length(content)<=2000 AND revision>0 AND source_seq>0'),
        CheckConstraint("(deleted_at IS NULL AND length(content)>0) OR (deleted_at IS NOT NULL AND content='')"),
        Index('ix_shared_private_memory_active','room_id','deleted_at','memory_id'))

class SharedPrivateMemoryCommand(Base):
    __tablename__='shared_private_memory_commands'
    room_id=Column(Integer,ForeignKey('shared_text_rooms.room_id'),primary_key=True)
    root_event_id=Column(String(36),ForeignKey('room_events.event_id'),primary_key=True)
    agent_id=Column(Integer,ForeignKey('agents.id'),primary_key=True)
    call_id=Column(String(200),primary_key=True)
    request_digest=Column(String(64),nullable=False)
    response=Column(Text,nullable=False)
    created_at=Column(DateTime,nullable=False)
    __table_args__=(CheckConstraint('length(call_id) BETWEEN 1 AND 200'),CheckConstraint('length(request_digest)=64'))
