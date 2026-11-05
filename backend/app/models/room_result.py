"""Room-scoped immutable result versions and append-only peer-source opinions."""

from sqlalchemy import (CheckConstraint, Column, DateTime, ForeignKey,
                        ForeignKeyConstraint, Integer, JSON, String, Text,
                        UniqueConstraint, text)

from app.core.database import Base


class RoomResult(Base):
    __tablename__ = "room_results"
    result_id = Column(String(36), primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    room_id = Column(Integer, nullable=False)
    current_version = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    __table_args__ = (
        UniqueConstraint("result_id", "room_id"),
        ForeignKeyConstraint(("room_id", "owner_id"), ("conversations.id", "conversations.user_id")),
        CheckConstraint("current_version >= 1"),
    )


class RoomResultVersion(Base):
    __tablename__ = "room_result_versions"
    result_id = Column(String(36), primary_key=True)
    version = Column(Integer, primary_key=True)
    room_id = Column(Integer, nullable=False)
    kind = Column(String(8), nullable=False)
    title = Column(String(200), nullable=False)
    source_event_id = Column(String(36), nullable=False)
    source_seq = Column(Integer, nullable=False)
    selector = Column(String(2048), nullable=False)
    author_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    author_name_snapshot = Column(String(200), nullable=False)
    execution = Column(JSON, nullable=False)
    content = Column(Text, nullable=False)
    file_ref = Column(JSON)
    evidence = Column(JSON, nullable=False)
    state = Column(String(12), nullable=False, server_default=text("'draft'"))
    state_revision = Column(Integer, nullable=False, server_default=text("1"))
    link_report = Column(String(12), nullable=False, server_default=text("'unchecked'"))
    state_owner_id = Column(Integer, ForeignKey("users.id"))
    state_changed_at = Column(DateTime)
    link_report_owner_id = Column(Integer, ForeignKey("users.id"))
    link_reported_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False)
    __table_args__ = (
        UniqueConstraint("result_id", "version", "room_id"),
        UniqueConstraint("result_id", "version", "room_id", "source_event_id"),
        UniqueConstraint("room_id", "source_event_id", "kind", "selector"),
        ForeignKeyConstraint(("result_id", "room_id"), ("room_results.result_id", "room_results.room_id")),
        ForeignKeyConstraint(("source_event_id", "room_id"), ("room_events.event_id", "room_events.room_id")),
        CheckConstraint("version >= 1 AND source_seq >= 1 AND state_revision >= 1"),
        CheckConstraint("kind IN ('text','link','file','page')"),
        CheckConstraint("state IN ('draft','reviewing','final')"),
        CheckConstraint("link_report IN ('unchecked','reported_bad')"),
    )


class RoomResultReview(Base):
    __tablename__ = "room_result_reviews"
    review_id = Column(String(36), primary_key=True)
    result_id = Column(String(36), nullable=False)
    version = Column(Integer, nullable=False)
    room_id = Column(Integer, nullable=False)
    source_event_id = Column(String(36), nullable=False)
    review_event_id = Column(String(36), nullable=False)
    review_seq = Column(Integer, nullable=False)
    reviewer_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    reviewer_name_snapshot = Column(String(200), nullable=False)
    linked_by_owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False)
    __table_args__ = (
        UniqueConstraint("result_id", "version", "review_event_id"),
        ForeignKeyConstraint(("result_id", "version", "room_id", "source_event_id"),
                             ("room_result_versions.result_id", "room_result_versions.version", "room_result_versions.room_id", "room_result_versions.source_event_id")),
        ForeignKeyConstraint(("review_event_id", "room_id"), ("room_events.event_id", "room_events.room_id")),
        ForeignKeyConstraint(("source_event_id", "room_id"), ("room_events.event_id", "room_events.room_id")),
        CheckConstraint("version >= 1 AND review_seq >= 1"),
    )


class RoomResultCommand(Base):
    __tablename__ = "room_result_commands"
    owner_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    idempotency_key = Column(String(200), primary_key=True)
    room_id = Column(Integer, nullable=False)
    request_digest = Column(String(64), nullable=False)
    response = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(("room_id", "owner_id"), ("conversations.id", "conversations.user_id")),
        CheckConstraint("length(idempotency_key) BETWEEN 1 AND 200"),
        CheckConstraint("length(request_digest) = 64"),
    )
