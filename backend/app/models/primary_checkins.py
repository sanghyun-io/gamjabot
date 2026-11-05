from sqlalchemy import Column,Integer,String,Text,Boolean,DateTime,ForeignKey,UniqueConstraint
from app.core.database import Base

class PrimaryCheckin(Base):
    __tablename__='primary_checkins'
    owner_id=Column(Integer,ForeignKey('users.id'),primary_key=True)
    revision=Column(Integer,nullable=False)
    primary_revision=Column(Integer,nullable=False)
    agent_id=Column(Integer,ForeignKey('agents.id'))
    room_id=Column(Integer,ForeignKey('conversations.id'))
    enabled=Column(Boolean,nullable=False)
    profile_fingerprint=Column(String(64))
    prompt=Column(Text,nullable=False)
    interval_minutes=Column(Integer,nullable=False)
    next_run_at=Column(DateTime)
    last_run_id=Column(String(36))

class PrimaryCheckinRun(Base):
    __tablename__='primary_checkin_runs'
    run_id=Column(String(36),primary_key=True)
    owner_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    config_revision=Column(Integer,nullable=False)
    primary_revision=Column(Integer,nullable=False)
    agent_id=Column(Integer,ForeignKey('agents.id'),nullable=False)
    room_id=Column(Integer,ForeignKey('conversations.id'),nullable=False)
    generation=Column(Integer,nullable=False)
    scheduled_at=Column(DateTime,nullable=False)
    started_at=Column(DateTime,nullable=False)
    completed_at=Column(DateTime)
    state=Column(String(16),nullable=False)
    root_event_id=Column(String(36),ForeignKey('room_events.event_id'),nullable=False)
    result_event_id=Column(String(36),ForeignKey('room_events.event_id'))
    failure_code=Column(String(64))
    connection_id=Column(Integer)
    model_id=Column(String(200))
    __table_args__=(UniqueConstraint('owner_id','config_revision','scheduled_at'),)
