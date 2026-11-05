"""Persistent fail-closed barrier, scoped to one pinned canonical account."""
from sqlalchemy import Column,Integer,String,ForeignKey
from app.core.database import Base
class SharedConnectorClearBarrier(Base):
 __tablename__='shared_connector_clear_barriers'
 owner_id=Column(Integer,ForeignKey('users.id'),primary_key=True)
 issuer=Column(String,primary_key=True);subject=Column(String,primary_key=True);key_fingerprint=Column(String,primary_key=True)
 operation_id=Column(String(36),nullable=False);state=Column(String,nullable=False)
