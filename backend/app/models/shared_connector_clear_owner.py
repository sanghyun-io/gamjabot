"""Instance fence for an unfinished account-wide clear."""
from sqlalchemy import Column,Integer,String,ForeignKey
from app.core.database import Base
class SharedConnectorClearOwner(Base):
 __tablename__='shared_connector_clear_owners'
 owner_id=Column(Integer,ForeignKey('users.id'),primary_key=True)
 issuer=Column(String,primary_key=True);subject=Column(String,primary_key=True);key_fingerprint=Column(String,primary_key=True)
 operation_id=Column(String,nullable=False);instance_id=Column(String,nullable=False)
 recovery_id=Column(String);recovery_instance_id=Column(String)
