"""Local recipient connector links, preference decisions and exact operation receipts."""
from sqlalchemy import Column,Integer,String,Text,ForeignKey,UniqueConstraint
from app.core.database import Base
class SharedConnectorLink(Base):
 __tablename__='shared_connector_links'
 link_id=Column(String(36),primary_key=True);owner_id=Column(Integer,ForeignKey('users.id'),nullable=False)
 issuer=Column(String,nullable=False);subject=Column(String(36),nullable=False);key_fingerprint=Column(String(64),nullable=False);server_id=Column(String(36),ForeignKey('mcp_servers.server_id'),nullable=False)
 server_revision=Column(Integer,nullable=False);credential_ref_sha256=Column(String(64),nullable=False);revision=Column(Integer,nullable=False);preferences_revision=Column(Integer,nullable=False);state=Column(String,nullable=False)
 __table_args__=(UniqueConstraint('owner_id','server_id'),)
class SharedConnectorPreference(Base):
 __tablename__='shared_connector_preferences'
 consent_id=Column(String(36),primary_key=True);link_id=Column(String(36),ForeignKey('shared_connector_links.link_id'),nullable=False)
 scope=Column(String,nullable=False);team_id=Column(String,nullable=False);publication_id=Column(String,nullable=False);revision=Column(Integer,nullable=False);state=Column(String,nullable=False)
 __table_args__=(UniqueConstraint('link_id','scope','team_id','publication_id'),)
class SharedConnectorRequest(Base):
 __tablename__='shared_connector_requests'
 request_id=Column(String(36),primary_key=True);link_id=Column(String(36),ForeignKey('shared_connector_links.link_id'),nullable=False);authority_id=Column(String(36),nullable=False,unique=True)
 room_id=Column(Integer,ForeignKey('conversations.id'),nullable=False);agent_id=Column(Integer,ForeignKey('agents.id'),nullable=False);root_event_id=Column(String(36),ForeignKey('room_events.event_id'),nullable=False)
 run_generation=Column(Integer,nullable=False);action_id=Column(String(36),nullable=False,unique=True);session_generation=Column(String(36),nullable=False)
 owner_epoch=Column(String(64),nullable=False);publication_revision=Column(Integer,nullable=False);revocation_epoch=Column(Integer,nullable=False);authority_revision=Column(String,nullable=False)
 team_id=Column(String(36),nullable=False);publication_id=Column(String(36),nullable=False);state=Column(String,nullable=False)
 tool_id=Column(String(64),nullable=False);consent_id=Column(String(36));consent_scope=Column(String);consent_revision=Column(Integer,nullable=False,default=0);preference_revision=Column(Integer,nullable=False,default=0)
 __table_args__=(UniqueConstraint('room_id','root_event_id','action_id'),)
class SharedConnectorCommand(Base):
 __tablename__='shared_connector_commands'
 owner_id=Column(Integer,ForeignKey('users.id'),primary_key=True);issuer=Column(String,primary_key=True);subject=Column(String,primary_key=True);key_fingerprint=Column(String(64),nullable=False);operation_id=Column(String(36),primary_key=True)
 request_digest=Column(String(64),nullable=False);session_generation=Column(String(36),nullable=False);team_id=Column(String(36),nullable=False);publication_id=Column(String(36),nullable=False);state=Column(String,nullable=False);response=Column(Text,nullable=False)
