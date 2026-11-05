"""Bind server-side Bot creation to the Agent profile that produced a request.

This hash is confinement metadata, never model input or a new permission. The
creation service separately checks the original Owner turn and existing live
connection/auth authority under its write transaction.
"""
from datetime import datetime,timezone
from hashlib import sha256
import json
import re

from app.models.models import Agent,User
from app.services.llm_service import LLMFailure


PROFILE_FIELDS=('id','user_id','name','description','system_prompt','model','connection_id',
    'model_id','avatar','temperature','max_tokens','is_active','created_at','updated_at')


def _changed():
    raise LLMFailure('bot_configuration_changed','The source Bot configuration changed; send a new request.')


def fingerprint(agent):
    """Read only the Agent profile, excluding connections/credentials/history."""
    try:
        values={name:getattr(agent,name) for name in PROFILE_FIELDS}
        for name in ('created_at','updated_at'):
            value=values[name]
            if isinstance(value,datetime):
                value=value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
                values[name]=value.isoformat(timespec='microseconds')
        payload=json.dumps({'version':1,'agent':values},ensure_ascii=False,sort_keys=True,
            separators=(',',':'),allow_nan=False).encode()
    except (AttributeError,TypeError,ValueError):
        _changed()
    return sha256(payload).hexdigest()


def require_current(db,owner_id,agent_id,expected):
    """Fresh-read comparison; never end the caller's BEGIN IMMEDIATE lock."""
    if (type(owner_id) is not int or type(agent_id) is not int or not isinstance(expected,str)
            or re.fullmatch(r'[a-f0-9]{64}',expected) is None):
        _changed()
    owner=db.get(User,owner_id,populate_existing=True)
    agent=db.get(Agent,agent_id,populate_existing=True)
    if (owner is None or not owner.is_active or agent is None or not agent.is_active
            or agent.user_id!=owner_id or fingerprint(agent)!=expected):
        _changed()
    return agent
