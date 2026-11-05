"""Typed names for staged shared-Bot authorization records."""
from sqlalchemy import Column, ForeignKey, Integer, String, Text, UniqueConstraint
from app.core.database import Base

class TeamRealm(Base):
    __tablename__ = 'team_realms'
    id = Column(String, primary_key=True)
    issuer = Column(Text, nullable=False)
    team_subject = Column(Text, nullable=False)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    owner_subject = Column(Text, nullable=False)
    revision = Column(Integer, nullable=False)
    revocation_epoch = Column(Integer, nullable=False)
    created_at = Column(Text, nullable=False)
    __table_args__ = (UniqueConstraint('issuer','team_subject'),)

class TeamPerson(Base):
    __tablename__ = 'team_people'
    team_id = Column(String, ForeignKey('team_realms.id'), primary_key=True)
    issuer = Column(Text, primary_key=True)
    subject = Column(Text, primary_key=True)
    role = Column(String, nullable=False)
    state = Column(String, nullable=False)
    revision = Column(Integer, nullable=False)

class SharedBotPublication(Base):
    __tablename__ = 'shared_bot_publications'
    id = Column(String, primary_key=True)
    team_id = Column(String, ForeignKey('team_realms.id'), nullable=False)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    owner_issuer = Column(Text, nullable=False)
    owner_subject = Column(Text, nullable=False)
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False)
    role_text = Column(Text, nullable=False)
    knowledge_text = Column(Text, nullable=False)
    audience = Column(String, nullable=False)
    revision = Column(Integer, nullable=False)
    revocation_epoch = Column(Integer, nullable=False)
    state = Column(String, nullable=False)
    updated_at = Column(Text, nullable=False)
    __table_args__ = (UniqueConstraint('team_id','agent_id'),)

class TeamInvite(Base):
    __tablename__ = 'team_invites'
    id = Column(String, primary_key=True)
    team_id = Column(String, ForeignKey('team_realms.id'), nullable=False)
    issuer = Column(Text, nullable=False)
    subject = Column(Text, nullable=False)
    role = Column(String, nullable=False)
    state = Column(String, nullable=False)
    expires_at = Column(Text, nullable=False)
    created_at = Column(Text, nullable=False)

class SharedBotPerson(Base):
    __tablename__ = 'shared_bot_people'
    share_id = Column(String, ForeignKey('shared_bot_publications.id'), primary_key=True)
    issuer = Column(Text, primary_key=True)
    subject = Column(Text, primary_key=True)
    role = Column(String, nullable=False)

class SharedBotRoom(Base):
    __tablename__ = 'shared_bot_rooms'
    share_id = Column(String, ForeignKey('shared_bot_publications.id'), primary_key=True)
    issuer = Column(Text, primary_key=True)
    subject = Column(Text, primary_key=True)
    recipient_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    room_id = Column(Integer, ForeignKey('conversations.id'), nullable=False, unique=True)
    connection_id = Column(Integer, ForeignKey('provider_connections.id'), nullable=False)
    model_id = Column(Text, nullable=False)
    share_revision = Column(Integer, nullable=False)
    revocation_epoch = Column(Integer, nullable=False)
    team_revocation_epoch = Column(Integer, nullable=False)
    created_at = Column(Text, nullable=False)
