"""Cross-room Bot messages, bound to an original Owner-authored request."""
from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from app.core.database import Base


class BotMessage(Base):
    __tablename__ = "bot_messages"
    message_id = Column(String(36), primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    root_room_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    root_request_event_id = Column(String(36), ForeignKey("room_events.event_id"), nullable=False)
    sender_room_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    recipient_room_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    sender_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    recipient_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    sender_name = Column(String, nullable=False)
    recipient_name = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    delivery_key = Column(String(64), nullable=False)
    batch_id = Column(String(36), ForeignKey('bot_batches.batch_id'))
    batch_index = Column(Integer)
    parent_job_id = Column(String(36))
    parent_kind = Column(String(16))
    depth = Column(Integer, nullable=False)
    state = Column(String(16), nullable=False)
    reply_state = Column(String(16), nullable=False)
    failure_code = Column(String(48))
    request_event_id = Column(String(36), ForeignKey("room_events.event_id"), nullable=False)
    delivery_event_id = Column(String(36), ForeignKey("room_events.event_id"), nullable=False)
    result_event_id = Column(String(36), ForeignKey("room_events.event_id"))
    return_delivery_event_id = Column(String(36), ForeignKey("room_events.event_id"))
    return_event_id = Column(String(36), ForeignKey("room_events.event_id"))
    deadline_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    __table_args__ = (UniqueConstraint("root_request_event_id", "delivery_key"),)


class BotMessageRetry(Base):
    __tablename__ = "bot_message_retries"
    owner_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    idempotency_key = Column(String(200), primary_key=True)
    message_id = Column(String(36), ForeignKey("bot_messages.message_id"), nullable=False)
    request_digest = Column(String(64), nullable=False)
    response = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False)
