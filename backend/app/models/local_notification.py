"""Owner-scoped local notification preferences and at-most-once deliveries."""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from app.core.database import Base

class LocalNotificationSetting(Base):
    __tablename__='local_notification_settings'
    owner_id=Column(Integer,ForeignKey('users.id'),primary_key=True)
    agent_id=Column(Integer,ForeignKey('agents.id'),primary_key=True)
    revision=Column(Integer,nullable=False)
    completion=Column(Boolean,nullable=False)
    failure=Column(Boolean,nullable=False)
    approval=Column(Boolean,nullable=False)
    updated_at=Column(DateTime,nullable=False)

class LocalNotificationCommand(Base):
    __tablename__='local_notification_commands'
    owner_id=Column(Integer,ForeignKey('users.id'),primary_key=True)
    operation_id=Column(String(36),primary_key=True)
    request_digest=Column(String(64),nullable=False)
    agent_id=Column(Integer,ForeignKey('agents.id'),nullable=False)
    revision=Column(Integer,nullable=False)
    created_at=Column(DateTime,nullable=False)

class LocalNotificationSession(Base):
    __tablename__='local_notification_sessions'
    session_id=Column(String(36),primary_key=True)
    owner_id=Column(Integer,ForeignKey('users.id'),nullable=False,unique=True)
    owner_epoch=Column(String(64),nullable=False)
    started_at=Column(DateTime,nullable=False)

class LocalNotificationDelivery(Base):
    __tablename__='local_notification_deliveries'
    delivery_id=Column(String(36),primary_key=True)
    owner_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    owner_epoch=Column(String(64),nullable=False)
    session_id=Column(String(36),nullable=False)
    agent_id=Column(Integer,ForeignKey('agents.id'),nullable=False)
    kind=Column(String(16),nullable=False)
    source_id=Column(String(36),nullable=False)
    room_id=Column(Integer,ForeignKey('conversations.id'),nullable=False)
    state=Column(String(16),nullable=False)
    created_at=Column(DateTime,nullable=False)
    updated_at=Column(DateTime,nullable=False)
    __table_args__=(UniqueConstraint('owner_id','kind','source_id'),)
