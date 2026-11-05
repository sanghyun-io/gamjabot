"""Durable request/task cancellation lineage and bounded native work."""
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, JSON
from app.core.database import Base


class ExecutionScope(Base):
    __tablename__ = 'execution_scopes'
    scope_id = Column(String(36), primary_key=True)
    root_request_event_id = Column(String(36), ForeignKey('room_events.event_id'), nullable=False)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    root_room_id = Column(Integer, ForeignKey('conversations.id'), nullable=False)
    parent_scope_id = Column(String(36), ForeignKey('execution_scopes.scope_id'))
    kind = Column(String(16), nullable=False)
    execution_room_id = Column(Integer, ForeignKey('conversations.id'))
    agent_id = Column(Integer, ForeignKey('agents.id'))
    job_id = Column(String(36))
    job_kind = Column(String(16))
    epoch = Column(Integer, nullable=False, default=0)
    state = Column(String(16), nullable=False, default='active')
    deadline_at = Column(DateTime, nullable=False)
    revocation_state = Column(String(16))
    native_calls = Column(Integer, nullable=False, default=0)
    max_native_calls = Column(Integer, nullable=False, default=0)
    executor_used_ms = Column(Integer, nullable=False, default=0)
    executor_reserved_ms = Column(Integer, nullable=False, default=0)
    max_executor_ms = Column(Integer, nullable=False, default=0)


class ScopeStopReceipt(Base):
    __tablename__ = 'scope_stop_receipts'
    stop_id = Column(String(36), primary_key=True)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    room_id = Column(Integer, ForeignKey('conversations.id'), nullable=False)
    revocations = Column(JSON, nullable=False)
    receipts = Column(JSON, nullable=False)
    state = Column(String(16), nullable=False)
    created_at = Column(DateTime, nullable=False)
