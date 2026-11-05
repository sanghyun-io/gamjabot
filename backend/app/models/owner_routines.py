from sqlalchemy import Column,Integer,String,Text,Boolean,DateTime,JSON,ForeignKey,UniqueConstraint
from app.core.database import Base

class OwnerRoutine(Base):
    __tablename__='owner_routines'
    routine_id=Column(String(36),primary_key=True);owner_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    agent_id=Column(Integer,ForeignKey('agents.id'),nullable=False);room_id=Column(Integer,ForeignKey('conversations.id'),nullable=False)
    revision=Column(Integer,nullable=False);name=Column(String(120),nullable=False);instruction=Column(Text,nullable=False)
    schedule=Column(JSON,nullable=False);enabled=Column(Boolean,nullable=False);profile_fingerprint=Column(String(64),nullable=False)
    next_run_at=Column(DateTime);created_at=Column(DateTime,nullable=False);updated_at=Column(DateTime,nullable=False);deleted_at=Column(DateTime)
class OwnerRoutineRun(Base):
    __tablename__='owner_routine_runs'
    run_id=Column(String(36),primary_key=True);routine_id=Column(String(36),ForeignKey('owner_routines.routine_id'),nullable=False)
    owner_id=Column(Integer,ForeignKey('users.id'),nullable=False);agent_id=Column(Integer,ForeignKey('agents.id'),nullable=False)
    room_id=Column(Integer,ForeignKey('conversations.id'),nullable=False);config_revision=Column(Integer,nullable=False);generation=Column(Integer,nullable=False)
    origin=Column(String(12),nullable=False);scheduled_at=Column(DateTime,nullable=False);started_at=Column(DateTime,nullable=False);completed_at=Column(DateTime)
    state=Column(String(16),nullable=False);root_event_id=Column(String(36),ForeignKey('room_events.event_id'),nullable=False)
    result_event_id=Column(String(36),ForeignKey('room_events.event_id'));failure_code=Column(String(64));connection_id=Column(Integer);model_id=Column(String(200))
    max_tool_calls=Column(Integer,nullable=False);max_executor_seconds=Column(Integer,nullable=False)
    __table_args__=(UniqueConstraint('routine_id','origin','scheduled_at'),)
class OwnerRoutineCommand(Base):
    __tablename__='owner_routine_commands'
    owner_id=Column(Integer,ForeignKey('users.id'),primary_key=True);command_key=Column(String(100),primary_key=True)
    request_digest=Column(String(64),nullable=False);response=Column(JSON,nullable=False)

class OwnerRoutinePendingTest(Base):
    __tablename__='owner_routine_pending_tests'
    request_id=Column(String(64),primary_key=True);owner_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    agent_id=Column(Integer,ForeignKey('agents.id'),nullable=False);routine_id=Column(String(36),ForeignKey('owner_routines.routine_id'),nullable=False)
    config_revision=Column(Integer,nullable=False);room_id=Column(Integer,ForeignKey('conversations.id'),nullable=False)
    root_request_event_id=Column(String(36),ForeignKey('room_events.event_id'),nullable=False);source_generation=Column(Integer,nullable=False)
    activity_event_id=Column(String(36),ForeignKey('room_events.event_id'),nullable=False);state=Column(String(16),nullable=False)
    run_id=Column(String(36),ForeignKey('owner_routine_runs.run_id'));failure_code=Column(String(64));created_at=Column(DateTime,nullable=False)

class OwnerRoutineWebhookKey(Base):
    __tablename__='owner_routine_webhook_keys'
    routine_id=Column(String(36),ForeignKey('owner_routines.routine_id'),primary_key=True)
    owner_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    generation=Column(Integer,nullable=False);routine_revision=Column(Integer,nullable=False)
    key_hash=Column(String(64));updated_at=Column(DateTime,nullable=False)

class OwnerRoutineWebhookEvent(Base):
    __tablename__='owner_routine_webhook_events'
    routine_id=Column(String(36),ForeignKey('owner_routines.routine_id'),primary_key=True)
    event_key=Column(String(100),primary_key=True);body_digest=Column(String(64),nullable=False)
    key_generation=Column(Integer,nullable=False);routine_revision=Column(Integer,nullable=False)
    run_id=Column(String(36),ForeignKey('owner_routine_runs.run_id'),nullable=False,unique=True)
    created_at=Column(DateTime,nullable=False)
