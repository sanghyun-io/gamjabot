"""Voice calls store only references to text events, never audio."""
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from app.core.database import Base

class VoiceCall(Base):
    __tablename__ = 'voice_calls'
    id = Column(String, primary_key=True)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    room_id = Column(Integer, ForeignKey('conversations.id'), nullable=False)
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False)
    owner_epoch = Column(String, nullable=False)
    status = Column(String, nullable=False)
    started_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime)
    lease_expires_at = Column(DateTime, nullable=False)

class VoiceCallTurn(Base):
    __tablename__ = 'voice_call_turns'
    call_id = Column(String, ForeignKey('voice_calls.id'), primary_key=True)
    client_turn_id = Column(String, primary_key=True)
    user_event_id = Column(String, ForeignKey('room_events.event_id'), nullable=False, unique=True)
