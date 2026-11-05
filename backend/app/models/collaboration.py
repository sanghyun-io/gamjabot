"""Mapped collaboration tables. Schema changes belong in numbered migrations."""

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint, text
from datetime import datetime, timezone

from app.core.database import Base


class RoomEvent(Base):
    __tablename__ = "room_events"
    event_id = Column(String(36), primary_key=True)
    device_id = Column(String(36))
    room_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    seq = Column(Integer, nullable=False)
    actor_type = Column(String, nullable=False)
    actor_id = Column(Integer)
    actor_name_snapshot = Column(String, nullable=False)
    kind = Column(String, nullable=False)
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False)
    idempotency_key = Column(String)
    __table_args__ = (
        UniqueConstraint("room_id", "seq"),
        UniqueConstraint("room_id", "idempotency_key"),
        CheckConstraint("actor_type IN ('user','agent','tool','system')"),
        CheckConstraint("kind IN ('message','tool_result','failure','summary','activity')"),
    )


class RoomMembership(Base):
    __tablename__ = "room_memberships"
    room_id = Column(Integer, ForeignKey("conversations.id"), primary_key=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), primary_key=True)
    is_active = Column(Boolean, nullable=False, server_default=text("1"))
    joined_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class RoomAgentRun(Base):
    __tablename__ = "room_agent_runs"
    room_id = Column(Integer, primary_key=True)
    agent_id = Column(Integer, primary_key=True)
    state = Column(String, nullable=False, server_default=text("'idle'"))
    watermark_seq = Column(Integer)
    round = Column(Integer)
    checkpoint = Column(JSON, nullable=False, default=dict)
    memory = Column(JSON, nullable=False, default=dict)
    pending_task = Column(JSON, nullable=False, default=dict)
    active_tool = Column(JSON, nullable=False, default=dict)
    paused = Column(Boolean, nullable=False, server_default=text("0"))
    generation = Column(Integer, nullable=False, server_default=text("0"))
    trigger_event_id = Column(String(36))
    updated_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"),
                        onupdate=lambda: datetime.now(timezone.utc))
    __table_args__ = (ForeignKeyConstraint(("room_id", "agent_id"), ("room_memberships.room_id", "room_memberships.agent_id")),)


class RoomTrigger(Base):
    __tablename__ = "room_triggers"
    event_id = Column(String(36), ForeignKey("room_events.event_id"), primary_key=True)
    room_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    state = Column(String, nullable=False, server_default=text("'pending'"))
    selected_agents = Column(JSON, nullable=False, default=list)
    max_rounds = Column(Integer, nullable=False, server_default=text("2"))
    max_tool_calls = Column(Integer, nullable=False, server_default=text("8"))
    max_executor_seconds = Column(Integer, nullable=False, server_default=text("60"))
    current_round = Column(Integer, nullable=False, server_default=text("0"))
    created_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class RoomScheduleSettings(Base):
    __tablename__ = "room_schedule_settings"
    room_id = Column(Integer, ForeignKey("conversations.id"), primary_key=True)
    max_rounds = Column(Integer, nullable=False, server_default=text("2"))
    max_tool_calls = Column(Integer, nullable=False, server_default=text("8"))
    max_executor_seconds = Column(Integer, nullable=False, server_default=text("60"))


class RoomLaunchRequest(Base):
    __tablename__ = "room_launch_requests"
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    idempotency_key = Column(String(200), primary_key=True)
    payload_digest = Column(String(64), nullable=False)
    room_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = (
        ForeignKeyConstraint(("room_id", "owner_id"), ("conversations.id", "conversations.user_id"), ondelete="CASCADE"),
        CheckConstraint("length(idempotency_key) BETWEEN 1 AND 200"),
        CheckConstraint("length(payload_digest) = 64"),
    )


class RequestRetryRecord(Base):
    __tablename__ = "request_retry_records"
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    idempotency_key = Column(String(200), primary_key=True)
    request_digest = Column(String(64), nullable=False)
    plan_hash = Column(String(64), nullable=False)
    room_id = Column(Integer, nullable=False)
    source_failure_event_id = Column(String(36), ForeignKey("room_events.event_id"), nullable=False)
    source_trigger_event_id = Column(String(36), ForeignKey("room_events.event_id"), nullable=False)
    source_request_event_id = Column(String(36), ForeignKey("room_events.event_id"), nullable=False)
    retry_event_id = Column(String(36), ForeignKey("room_events.event_id"), nullable=False, unique=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    retry_seq = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = (
        ForeignKeyConstraint(("room_id", "owner_id"), ("conversations.id", "conversations.user_id"), ondelete="CASCADE"),
        CheckConstraint("length(idempotency_key) BETWEEN 1 AND 200"),
        CheckConstraint("length(request_digest) = 64"),
        CheckConstraint("length(plan_hash) = 64"),
        CheckConstraint("retry_seq > 0"),
    )


class RequestFinalizeRecord(Base):
    __tablename__ = "request_finalize_records"
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    idempotency_key = Column(String(200), primary_key=True)
    request_digest = Column(String(64), nullable=False)
    plan_hash = Column(String(64), nullable=False)
    room_id = Column(Integer, nullable=False)
    source_request_event_id = Column(String(36), ForeignKey("room_events.event_id"), nullable=False)
    source_outcome_event_ids = Column(JSON, nullable=False)
    finalize_event_id = Column(String(36), ForeignKey("room_events.event_id"), nullable=False, unique=True)
    finalize_seq = Column(Integer, nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    execution_binding_digest = Column(String(64), nullable=False)
    projection_fingerprint = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = (
        ForeignKeyConstraint(("room_id", "owner_id"), ("conversations.id", "conversations.user_id"), ondelete="CASCADE"),
        CheckConstraint("length(idempotency_key) BETWEEN 1 AND 200"),
        CheckConstraint("length(request_digest) = 64"),
        CheckConstraint("length(plan_hash) = 64"),
        CheckConstraint("length(execution_binding_digest) = 64"),
        CheckConstraint("length(projection_fingerprint) = 64"),
        CheckConstraint("finalize_seq > 0"),
    )


class ProjectRoomTemplate(Base):
    __tablename__ = "project_room_templates"
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    project_id = Column(Integer, nullable=False)
    name = Column(String(100), nullable=False)
    room_title = Column(String(200), nullable=False)
    max_rounds = Column(Integer, nullable=False)
    max_tool_calls = Column(Integer, nullable=False)
    max_executor_seconds = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = (
        ForeignKeyConstraint(("project_id", "owner_id"), ("projects.id", "projects.user_id"), ondelete="CASCADE"),
        UniqueConstraint("owner_id", "project_id", "name"),
        CheckConstraint("length(trim(name)) BETWEEN 1 AND 100"),
        CheckConstraint("length(trim(room_title)) BETWEEN 1 AND 200"),
        CheckConstraint("max_rounds BETWEEN 1 AND 4"),
        CheckConstraint("max_tool_calls BETWEEN 0 AND 64"),
        CheckConstraint("max_executor_seconds BETWEEN 1 AND 3600"),
    )


class ProjectRoomTemplateMember(Base):
    __tablename__ = "project_room_template_members"
    template_id = Column(Integer, ForeignKey("project_room_templates.id", ondelete="CASCADE"), primary_key=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), primary_key=True)


class RoomDraft(Base):
    __tablename__ = "room_drafts"
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    room_id = Column(Integer, primary_key=True)
    content = Column(Text, nullable=False)
    reply_to_event_id = Column(String(36))
    reply_to_seq = Column(Integer)
    version = Column(Integer, nullable=False)
    idempotency_key = Column(String(36))
    updated_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    @property
    def reply_to(self):
        if self.reply_to_event_id is None and self.reply_to_seq is None:return None
        return {'event_id':self.reply_to_event_id,'seq':self.reply_to_seq}
    __table_args__ = (
        ForeignKeyConstraint(("room_id", "owner_id"), ("conversations.id", "conversations.user_id"), ondelete="CASCADE"),
        CheckConstraint("version >= 1"),
        CheckConstraint("length(content) <= 65536"),
        CheckConstraint("(content = '' AND idempotency_key IS NULL) OR (content <> '' AND idempotency_key IS NOT NULL)"),
        CheckConstraint("(reply_to_event_id IS NULL AND reply_to_seq IS NULL) OR (reply_to_event_id IS NOT NULL AND reply_to_seq IS NOT NULL AND reply_to_seq > 0)"),
    )


class RoomReadState(Base):
    __tablename__ = "room_read_state"
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    room_id = Column(Integer, primary_key=True)
    last_read_seq = Column(Integer, nullable=False)
    updated_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = (
        ForeignKeyConstraint(("room_id", "owner_id"), ("conversations.id", "conversations.user_id"), ondelete="CASCADE"),
        CheckConstraint("last_read_seq >= 0"),
    )


class RoomPin(Base):
    __tablename__ = "room_pins"
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    room_id = Column(Integer, primary_key=True)
    pinned_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = (
        ForeignKeyConstraint(("room_id", "owner_id"), ("conversations.id", "conversations.user_id"), ondelete="CASCADE"),
    )


class ProviderConnection(Base):
    __tablename__ = "provider_connections"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    kind = Column(String, nullable=False)
    name = Column(String, nullable=False)
    base_url = Column(String)
    secret_ref = Column(String)
    default_model_id = Column(String, nullable=False, default="")
    enabled = Column(Boolean, nullable=False, default=True)
    auth_required = Column(Boolean, nullable=False, default=True)
    capabilities = Column(JSON, nullable=False, default=dict)
    readiness_revision = Column(Integer, nullable=False, default=1, server_default=text("1"))
    provider_id = Column(String(64))
    protocol_id = Column(String(64))
    auth_method_id = Column(String(64))
    public_config_version = Column(Integer)
    public_config = Column(JSON)
    auth_grant_revision = Column(Integer)
    credential_state = Column(String(32))
    created_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime)
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="ux_provider_connections_id_user_id"),
        CheckConstraint("public_config_version IS NULL OR public_config_version = 1"),
        CheckConstraint("auth_grant_revision IS NULL OR auth_grant_revision >= 0"),
        CheckConstraint("credential_state IS NULL OR credential_state IN "
                        "('missing','authorizing','present','refresh_required','rejected',"
                        "'locked','unavailable','not_required')"),
    )


class ConnectionModelCache(Base):
    __tablename__ = "connection_model_cache"
    owner_id = Column(Integer, primary_key=True)
    connection_id = Column(Integer, primary_key=True)
    config_revision = Column(Integer, nullable=False)
    auth_grant_revision = Column(Integer, nullable=False)
    config_fingerprint = Column(String(64), nullable=False)
    observed_at = Column(DateTime)
    truncated = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    models = Column(JSON, nullable=False, default=list)
    updated_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = (
        ForeignKeyConstraint(
            ("connection_id", "owner_id"),
            ("provider_connections.id", "provider_connections.user_id"),
            ondelete="CASCADE",
        ),
        CheckConstraint("config_revision >= 1"),
        CheckConstraint("auth_grant_revision >= 0"),
        CheckConstraint("length(config_fingerprint) = 64"),
    )


class ConnectionTestHistory(Base):
    __tablename__ = "connection_test_history"
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    connection_id = Column(Integer, nullable=False)
    config_revision = Column(Integer, nullable=False)
    config_fingerprint = Column(String(64), nullable=False)
    model_id = Column(String(200))
    ok = Column(Boolean, nullable=False)
    outcome_code = Column(String(40), nullable=False)
    credential_observation = Column(String(20), nullable=False)
    verified_capabilities = Column(JSON)
    duration_ms = Column(Integer, nullable=False)
    profile = Column(String(10))
    actual_request_count = Column(Integer)
    tested_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = (
        ForeignKeyConstraint(("connection_id", "owner_id"),
                             ("provider_connections.id", "provider_connections.user_id"), ondelete="CASCADE"),
        CheckConstraint("duration_ms >= 0"),
        CheckConstraint("profile IS NULL OR profile IN ('text','full')"),
        CheckConstraint("actual_request_count IS NULL OR actual_request_count >= 0"),
    )


class StarterRequest(Base):
    __tablename__ = "starter_requests"
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    idempotency_key = Column(String(200), nullable=False)
    request_digest = Column(String(64), nullable=False)
    plan_hash = Column(String(64), nullable=False)
    project_id = Column(Integer, nullable=False)
    room_id = Column(Integer, nullable=False)
    proposer_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    reviewer_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key"),
        ForeignKeyConstraint(("project_id", "owner_id"), ("projects.id", "projects.user_id"), ondelete="CASCADE"),
        ForeignKeyConstraint(("room_id", "owner_id"), ("conversations.id", "conversations.user_id"), ondelete="CASCADE"),
        CheckConstraint("length(idempotency_key) BETWEEN 1 AND 200"),
        CheckConstraint("length(request_digest) = 64"),
        CheckConstraint("length(plan_hash) = 64"),
        CheckConstraint("proposer_agent_id <> reviewer_agent_id"),
    )


class RoomGrant(Base):
    __tablename__ = "room_grants"
    id = Column(Integer, primary_key=True)
    room_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id"))
    scope = Column(JSON, nullable=False)
    decision = Column(String, nullable=False)
    expires_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class BroadMode(Base):
    __tablename__ = "broad_modes"
    owner_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    enabled = Column(Boolean, nullable=False, server_default=text("0"))
    session_id = Column(String(36))
    expires_at = Column(DateTime)
    updated_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class ToolAction(Base):
    __tablename__ = "tool_actions"
    action_id = Column(String(36), primary_key=True)
    room_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id"))
    trigger_event_id = Column(String(36))
    call_id = Column(String)
    grant_id = Column(Integer, ForeignKey("room_grants.id"))
    family = Column(String, nullable=False)
    arguments = Column(JSON, nullable=False)
    state = Column(String, nullable=False)
    workflow_id = Column(String)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    observation = Column(JSON)
    target = Column(String)
    expected_effect = Column(String)
    session_id = Column(String(36))
    requested_at = Column(DateTime)
    approval_scope = Column(String)
    scope_target = Column(String)
    payload_digest = Column(String(64))
    execution_binding = Column(JSON)
    mcp_binding = Column(JSON)
    executor_reservation_ms = Column(Integer)
    # NULL means no trusted review, including every pre-26 action. Decisions
    # must not manufacture a review from legacy arguments or approval state.
    review_id = Column(String(36))
    review_snapshot = Column(JSON)
    send_image_to_model = Column(Boolean, nullable=True, default=False)
    approved_at = Column(DateTime)
    approval_owner_id = Column(Integer, ForeignKey("users.id"))
    __table_args__ = (Index("ux_tool_actions_review_id", "review_id", unique=True),)


class DesktopLease(Base):
    __tablename__ = "desktop_lease"
    id = Column(Integer, primary_key=True)
    room_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    workflow_id = Column(String, nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id"))
    session_id = Column(String(36))
    lease_id = Column(String(32))
    acquired_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    __table_args__ = (CheckConstraint("id=1"),)


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    version = Column(Integer, primary_key=True)
    applied_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
