from sqlalchemy import Column,Integer,String,Text,Boolean,DateTime,JSON,ForeignKey,UniqueConstraint
from app.core.database import Base

class MCPServer(Base):
    __tablename__='mcp_servers'
    server_id=Column(String(36),primary_key=True);owner_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    revision=Column(Integer,nullable=False);name=Column(String(120),nullable=False);enabled=Column(Boolean,nullable=False)
    transport=Column(JSON,nullable=False);credential_ref=Column(String(200));created_at=Column(DateTime,nullable=False);updated_at=Column(DateTime,nullable=False)
class MCPTool(Base):
    __tablename__='mcp_tools'
    tool_id=Column(String(64),primary_key=True);server_id=Column(String(36),ForeignKey('mcp_servers.server_id'),nullable=False)
    server_revision=Column(Integer,nullable=False);name=Column(String(200),nullable=False);description=Column(Text,nullable=False)
    input_schema=Column(JSON,nullable=False);schema_canonical=Column(Text,nullable=False);schema_sha256=Column(String(64),nullable=False)
    __table_args__=(UniqueConstraint('server_id','server_revision','name'),)
class MCPBinding(Base):
    __tablename__='mcp_bindings'
    owner_id=Column(Integer,ForeignKey('users.id'),primary_key=True);agent_id=Column(Integer,ForeignKey('agents.id'),primary_key=True)
    server_id=Column(String(36),ForeignKey('mcp_servers.server_id'),primary_key=True);revision=Column(Integer,nullable=False);enabled=Column(Boolean,nullable=False);tool_ids=Column(JSON)
class MCPCommand(Base):
    __tablename__='mcp_commands'
    owner_id=Column(Integer,ForeignKey('users.id'),primary_key=True);command_key=Column(String(100),primary_key=True)
    request_digest=Column(String(64),nullable=False);response=Column(JSON,nullable=False)
class MCPDiscovery(Base):
    __tablename__='mcp_discoveries'
    discovery_id=Column(String(36),primary_key=True);owner_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    server_id=Column(String(36),ForeignKey('mcp_servers.server_id'),nullable=False);server_revision=Column(Integer,nullable=False)
    preview_id=Column(String(36),unique=True,nullable=False);transport=Column(JSON,nullable=False);executable_review=Column(JSON);state=Column(String(20),nullable=False)
    failure_code=Column(String(64));created_at=Column(DateTime,nullable=False);expires_at=Column(DateTime,nullable=False);completed_at=Column(DateTime)

class MCPOwnerDraft(Base):
    __tablename__='mcp_owner_drafts'
    action_id=Column(String(36),ForeignKey('tool_actions.action_id'),primary_key=True)
    owner_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    room_id=Column(Integer,ForeignKey('conversations.id'),nullable=False)
    revision=Column(Integer,nullable=False,default=0)
    base_review_id=Column(String(36),nullable=False)
    edits=Column(JSON,nullable=False)
    pending=Column(Boolean,nullable=False,default=False)
    original_review_id=Column(String(36),nullable=False)
    original_summary=Column(JSON,nullable=False)
    applied_review_id=Column(String(36))
    updated_at=Column(DateTime,nullable=False)
