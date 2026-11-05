from sqlalchemy import Column, Integer, String, Text, Boolean, JSON, UniqueConstraint
from app.core.database import Base

class BrowserDemonstration(Base):
    __tablename__ = 'browser_demonstrations'
    demo_id = Column(String(32), primary_key=True)
    owner_id = Column(Integer, nullable=False)
    room_id = Column(Integer, nullable=False)
    agent_id = Column(Integer, nullable=False)
    profile_id = Column(String(32), nullable=False)
    profile_revision = Column(Integer, nullable=False)
    state = Column(String(20), nullable=False)
    goal = Column(Text, nullable=False)
    steps = Column(JSON, nullable=False)
    include_target_labels = Column(Boolean, nullable=False)
    review = Column(JSON, nullable=False)
    review_revision = Column(Integer, nullable=False)
    command_key = Column(String(100), nullable=False)
    command_digest = Column(String(64), nullable=False)
    instance_id = Column(String(32), nullable=False)
    __table_args__ = (UniqueConstraint('owner_id', 'command_key'),)

class BrowserDemonstrationReviewCommand(Base):
    __tablename__ = 'browser_demonstration_review_commands'
    owner_id = Column(Integer, primary_key=True)
    command_key = Column(String(100), primary_key=True)
    demo_id = Column(String(32), nullable=False)
    digest = Column(String(64), nullable=False)
    response = Column(JSON, nullable=False)
