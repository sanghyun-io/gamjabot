"""Shared Agent validation and staged mutations; the caller owns its transaction."""
from fastapi import HTTPException
from app.core.agent_avatar import random_avatar
from app.models.models import Agent
from app.models.owner_common_memory import BotMemoryPolicy
from app.models.collaboration import ProviderConnection
from app.schemas.agent import AgentCreate, AgentUpdate


def require_owned_connection(db, connection_id, owner_id):
    if db.query(ProviderConnection.id).filter_by(id=connection_id,user_id=owner_id).first() is None:
        raise HTTPException(404,"Connection not found")


def stage_create(db, owner_id, value: AgentCreate):
    value=AgentCreate.model_validate(value)
    require_owned_connection(db,value.connection_id,owner_id)
    row=Agent(user_id=owner_id,name=value.name,description=value.description,system_prompt=value.system_prompt,
        model=value.model_id,connection_id=value.connection_id,model_id=value.model_id,
        temperature=value.temperature,max_tokens=value.max_tokens,
        avatar=value.avatar.model_dump() if value.avatar else random_avatar())
    db.add(row);db.flush()
    # New Agents start with Owner-room use enabled. Existing Agents without a
    # policy retain the legacy canonical-only default; resolving a DM never changes it.
    db.add(BotMemoryPolicy(owner_id=owner_id,agent_id=row.id,revision=0,mode='owner_rooms'))
    db.flush()
    return row


def stage_update(db, owner_id, agent_id, value: AgentUpdate):
    value=AgentUpdate.model_validate(value)
    row=db.query(Agent).filter_by(id=agent_id,user_id=owner_id).first()
    if row is None:raise HTTPException(404,"Agent not found")
    changes=value.model_dump(exclude_unset=True)
    if 'connection_id' in changes:
        if changes['connection_id'] is None:raise HTTPException(422,"Select a saved connection")
        require_owned_connection(db,changes['connection_id'],owner_id)
    if 'model_id' in changes:
        if changes['model_id'] is None:raise HTTPException(422,"Set an exact model ID")
        changes['model']=changes['model_id']
    for key,field in changes.items():setattr(row,key,field)
    db.flush()
    return row
