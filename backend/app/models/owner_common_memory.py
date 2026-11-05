from sqlalchemy import Column,Integer,String,Text,DateTime,JSON,ForeignKey,Boolean,Index,text
from app.core.database import Base

class BotMemoryPolicy(Base):
    __tablename__='bot_memory_policies'
    owner_id=Column(Integer,ForeignKey('users.id'),primary_key=True)
    agent_id=Column(Integer,ForeignKey('agents.id'),primary_key=True)
    revision=Column(Integer,nullable=False)
    mode=Column(String(20),nullable=False)

class OwnerCommonState(Base):
    __tablename__='owner_common_memory_state'
    owner_id=Column(Integer,ForeignKey('users.id'),primary_key=True)
    revision=Column(Integer,nullable=False)

class OwnerCommonEntry(Base):
    __tablename__='owner_common_memory_entries'
    fact_id=Column(String(36),primary_key=True)
    owner_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    kind=Column(String(12),nullable=False)
    content=Column(Text,nullable=False)
    origin_agent_id=Column(Integer,ForeignKey('agents.id'),nullable=False)
    origin_kind=Column(String(8),nullable=False)
    origin_room_id=Column(Integer,ForeignKey('conversations.id'))
    origin_event_id=Column(String(36),ForeignKey('room_events.event_id'))
    origin_seq=Column(Integer)
    revision=Column(Integer,nullable=False)
    created_at=Column(DateTime,nullable=False)
    updated_at=Column(DateTime,nullable=False)
    deleted_at=Column(DateTime)
    __table_args__=(Index('ux_owner_common_active_kind','owner_id','kind',unique=True,sqlite_where=text('deleted_at IS NULL')),)

class OwnerCommonCommand(Base):
    __tablename__='owner_common_memory_commands'
    owner_id=Column(Integer,ForeignKey('users.id'),primary_key=True)
    idempotency_key=Column(String(200),primary_key=True)
    request_digest=Column(String(64),nullable=False)
    response=Column(JSON,nullable=False)
    created_at=Column(DateTime,nullable=False)
