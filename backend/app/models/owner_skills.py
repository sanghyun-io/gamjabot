from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, JSON, ForeignKey
from app.core.database import Base

class OwnerSkill(Base):
    __tablename__='owner_skills'
    skill_id=Column(String(36),primary_key=True);owner_id=Column(Integer,ForeignKey('users.id'),nullable=False)
    revision=Column(Integer,nullable=False);name=Column(String(120),nullable=False);description=Column(String(500),nullable=False)
    content=Column(Text,nullable=False);active=Column(Boolean,nullable=False);source=Column(JSON,nullable=True);package_provenance=Column(JSON,nullable=True)
    created_at=Column(DateTime,nullable=False);updated_at=Column(DateTime,nullable=False)

class OwnerSkillVersion(Base):
    __tablename__='owner_skill_versions'
    skill_id=Column(String(36),ForeignKey('owner_skills.skill_id'),primary_key=True)
    revision=Column(Integer,primary_key=True);snapshot=Column(JSON,nullable=False)

class OwnerSkillBinding(Base):
    __tablename__='owner_skill_bindings'
    owner_id=Column(Integer,ForeignKey('users.id'),primary_key=True);agent_id=Column(Integer,ForeignKey('agents.id'),primary_key=True)
    skill_id=Column(String(36),ForeignKey('owner_skills.skill_id'),primary_key=True)
    revision=Column(Integer,nullable=False);enabled=Column(Boolean,nullable=False)

class OwnerSkillCommand(Base):
    __tablename__='owner_skill_commands'
    owner_id=Column(Integer,ForeignKey('users.id'),primary_key=True);command_key=Column(String(100),primary_key=True)
    request_digest=Column(String(64),nullable=False);response=Column(JSON,nullable=False)
