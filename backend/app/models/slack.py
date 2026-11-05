"""Owner-personal Slack configuration and durable event records (no credentials)."""
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, JSON, ForeignKey, UniqueConstraint
from app.core.database import Base

class SlackConfig(Base):
    __tablename__ = 'slack_configs'
    owner_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    revision = Column(Integer, nullable=False)
    operation_id = Column(String(36), nullable=False)
    state = Column(String(20), nullable=False)
    account_id = Column(String(36), nullable=False)
    credential_revision = Column(Integer, nullable=False)
    enabled = Column(Boolean, nullable=False)
    workspace_id = Column(String(64), nullable=False)
    app_id = Column(String(64), nullable=False)
    channel_id = Column(String(64), nullable=False)
    sender_ids = Column(JSON, nullable=False)
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False)
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=False)
    connection_id = Column(Integer, ForeignKey('provider_connections.id'), nullable=False)
    model_id = Column(String(200), nullable=False)
    updated_at = Column(DateTime, nullable=False)

class SlackThread(Base):
    __tablename__ = 'slack_threads'
    owner_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    workspace_id = Column(String(64), primary_key=True)
    channel_id = Column(String(64), primary_key=True)
    thread_ts = Column(String(64), primary_key=True)
    room_id = Column(Integer, ForeignKey('conversations.id'), nullable=False, unique=True)
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False)
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=False)
    created_at = Column(DateTime, nullable=False)

class SlackInbox(Base):
    __tablename__ = 'slack_inbox'
    owner_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    workspace_id = Column(String(64), primary_key=True)
    event_id = Column(String(100), primary_key=True)
    digest = Column(String(64), nullable=False)
    app_id = Column(String(64), nullable=False)
    owner_epoch = Column(String(64), nullable=False)
    config_revision = Column(Integer, nullable=False)
    credential_revision = Column(Integer, nullable=False)
    channel_id = Column(String(64), nullable=False)
    user_id = Column(String(64), nullable=False)
    event_ts = Column(String(64), nullable=False)
    thread_ts = Column(String(64), nullable=False)
    text = Column(Text, nullable=False)
    state = Column(String(24), nullable=False)
    run_id = Column(String(36), unique=True)
    created_at = Column(DateTime, nullable=False)

class SlackRun(Base):
    __tablename__ = 'slack_runs'
    run_id = Column(String(36), primary_key=True)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    workspace_id = Column(String(64), nullable=False)
    event_id = Column(String(100), nullable=False)
    config_revision = Column(Integer, nullable=False)
    credential_revision = Column(Integer, nullable=False)
    owner_epoch = Column(String(64), nullable=False)
    room_id = Column(Integer, ForeignKey('conversations.id'), nullable=False)
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False)
    generation = Column(Integer, nullable=False)
    profile_fingerprint = Column(String(64), nullable=False)
    max_tool_calls = Column(Integer, nullable=False)
    max_executor_seconds = Column(Integer, nullable=False)
    connection_id = Column(Integer, ForeignKey('provider_connections.id'), nullable=False)
    model_id = Column(String(200), nullable=False)
    state = Column(String(24), nullable=False)
    root_event_id = Column(String(36), ForeignKey('room_events.event_id'), nullable=False)
    result_event_id = Column(String(36), ForeignKey('room_events.event_id'))
    failure_code = Column(String(64))
    created_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime)
    __table_args__ = (UniqueConstraint('owner_id','workspace_id','event_id'),)

class SlackOutbox(Base):
    __tablename__ = 'slack_outbox'
    run_id = Column(String(36), ForeignKey('slack_runs.run_id'), primary_key=True)
    attempt_id = Column(String(36), nullable=False, unique=True)
    state = Column(String(24), nullable=False)
    remote_ts = Column(String(64))
    updated_at = Column(DateTime, nullable=False)

class SlackCommand(Base):
    __tablename__ = 'slack_commands'
    owner_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    operation_id = Column(String(36), primary_key=True)
    request_digest = Column(String(64), nullable=False)
    revision = Column(Integer, nullable=False)
    state = Column(String(20), nullable=False)
    created_at = Column(DateTime, nullable=False)

class SlackReplyResolution(Base):
    __tablename__ = 'slack_reply_resolutions'
    run_id = Column(String(36), ForeignKey('slack_runs.run_id'), nullable=False)
    owner_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    operation_id = Column(String(36), primary_key=True)
    attempt_id = Column(String(36))
    decision = Column(String(32), nullable=False)
    config_revision = Column(Integer, nullable=False)
    run_revision = Column(Integer, nullable=False)
    expected_state = Column(String(32), nullable=False)
    owner_epoch = Column(String(64), nullable=False)
    core_epoch = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False)
