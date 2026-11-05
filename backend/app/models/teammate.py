"""Durable same-room teammate delivery; never a substitute user identity."""
from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from app.core.database import Base


class TeammateHandoff(Base):
    __tablename__ = "teammate_handoffs"
    handoff_id = Column(String(36), primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    room_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    root_request_event_id = Column(String(36), ForeignKey("room_events.event_id"), nullable=False)
    parent_handoff_id = Column(String(36), ForeignKey("teammate_handoffs.handoff_id"))
    sender_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    recipient_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    sender_name = Column(String, nullable=False)
    recipient_name = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    delivery_key = Column(String(64), nullable=False)
    state = Column(String(16), nullable=False)
    reply_state = Column(String(16), nullable=False)
    depth = Column(Integer, nullable=False)
    request_event_id = Column(String(36), ForeignKey("room_events.event_id"), nullable=False)
    result_event_id = Column(String(36), ForeignKey("room_events.event_id"))
    return_event_id = Column(String(36), ForeignKey("room_events.event_id"))
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    __table_args__ = (UniqueConstraint("root_request_event_id", "delivery_key"),)


class TeammateBudget(Base):
    __tablename__ = "teammate_budgets"
    root_request_event_id = Column(String(36), ForeignKey("room_events.event_id"), primary_key=True)
    provider_calls = Column(Integer, nullable=False)


class TeammateDeliveryReceipt(Base):
    """Provider-call provenance. Acknowledgement means a response, not model comprehension."""
    __tablename__ = "teammate_delivery_receipts"
    receipt_id = Column(String(36), primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    root_room_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    root_request_event_id = Column(String(36), ForeignKey("room_events.event_id"), nullable=False)
    execution_room_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    generation = Column(Integer, nullable=False)
    job_id = Column(String(36), nullable=False)
    job_kind = Column(String(16), nullable=False)
    phase = Column(String(16), nullable=False)
    invocation_ordinal = Column(Integer, nullable=False)
    state = Column(String(16), nullable=False)
    sources_json = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    acknowledged_at = Column(DateTime)
    __table_args__ = (
        CheckConstraint("generation >= 0"),
        CheckConstraint("invocation_ordinal >= 1"),
        CheckConstraint("job_kind IN ('teammate','bot_message','bot_batch','retry')"),
        CheckConstraint("phase IN ('response','return')"),
        CheckConstraint("(state = 'attempted' AND acknowledged_at IS NULL) OR (state = 'acknowledged' AND acknowledged_at IS NOT NULL)"),
        Index("uq_teammate_delivery_invocation", "root_request_event_id", "job_kind", "job_id", "phase", "generation", "invocation_ordinal", unique=True),
        Index("ix_teammate_delivery_root", "owner_id", "root_room_id", "root_request_event_id"),
        Index("ix_teammate_delivery_job", "owner_id", "job_kind", "job_id", "phase"),
    )
