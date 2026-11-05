"""Owner-selected primary Bot and canonical one-to-one conversations."""
from sqlalchemy import Column, DateTime, ForeignKey, Integer
from app.core.database import Base


class BotOwnerProfile(Base):
    __tablename__ = "bot_owner_profiles"
    owner_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    primary_agent_id = Column(Integer, ForeignKey("agents.id"))
    revision = Column(Integer, nullable=False, default=0)
    personal_project_id = Column(Integer, ForeignKey("projects.id"))


class BotConversation(Base):
    __tablename__ = "bot_conversations"
    owner_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), primary_key=True)
    room_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, unique=True)
    replaced_room_id = Column(Integer)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
