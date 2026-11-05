"""Private local execution handle; canonical publication remains remote."""
from sqlalchemy import Column, Integer, String, ForeignKey, UniqueConstraint
from app.models.models import Base

class SharedTextRoom(Base):
    __tablename__ = 'shared_text_rooms'
    owner_id = Column(Integer, ForeignKey('users.id'), primary_key=True)
    issuer = Column(String, primary_key=True)
    subject = Column(String, primary_key=True)
    team_id = Column(String, primary_key=True)
    publication_id = Column(String, primary_key=True)
    room_id = Column(Integer, ForeignKey('conversations.id'), nullable=False, unique=True)
    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, unique=True)
    connection_id = Column(Integer, ForeignKey('provider_connections.id'), nullable=False)
    model_id = Column(String, nullable=False)
