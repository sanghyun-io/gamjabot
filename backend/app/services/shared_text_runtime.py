"""Fail-closed canonical authority for recipient-owned shared text rooms."""
from dataclasses import dataclass
from app.models.shared_text_runtime import SharedTextRoom
from app.models.models import Agent, Conversation
from app.models.collaboration import RoomMembership, RoomEvent
from app.services.llm_service import LLMFailure
from app.services.team_authority_adapter import TeamAuthorityUnavailable

@dataclass(frozen=True)
class SharedTurn:
    owner_id: int
    owner_epoch: str
    session_generation: str
    issuer: str
    subject: str
    team_id: str
    publication_id: str
    revision: int
    revocation_epoch: int
    authority_revision: str
    room_id: int
    agent_id: int
    root_id: str
    generation: int
    system: str


def binding_for(db, room_id, agent_id):
    by_room=db.query(SharedTextRoom).filter_by(room_id=room_id).first()
    by_agent=db.query(SharedTextRoom).filter_by(agent_id=agent_id).first()
    if not by_room and not by_agent:return None
    if not by_room or not by_agent or by_room.room_id!=by_agent.room_id or by_room.agent_id!=agent_id:
        raise LLMFailure('shared_room_scope_changed','Shared Bot room membership changed.')
    return by_room


def verify(app_state, db, room_id, agent_id, root_id, generation, expected=None):
    binding=binding_for(db,room_id,agent_id)
    if binding is None:
        if expected:raise LLMFailure('shared_authority_changed','Shared Bot binding changed.')
        return None
    room=db.get(Conversation,room_id);agent=db.get(Agent,agent_id);root=db.get(RoomEvent,root_id)
    members=db.query(RoomMembership).filter_by(room_id=room_id,is_active=True).all()
    if (not room or not agent or room.user_id!=binding.owner_id or agent.user_id!=binding.owner_id
            or agent.connection_id!=binding.connection_id or agent.model_id!=binding.model_id
            or len(members)!=1 or members[0].agent_id!=agent_id or not root or root.room_id!=room_id
            or root.actor_type!='user' or root.actor_id!=binding.owner_id or root.kind!='message'
            or not isinstance(root.payload,dict) or set(root.payload)!={'content','legacy_message_id'}
            or type(root.payload.get('legacy_message_id')) is not int or not isinstance(root.payload['content'],str)):
        raise LLMFailure('shared_room_scope_changed','Shared Bot supports text-only requests in its private room.')
    with app_state.owner_recovery_lock:
        adapter=getattr(app_state,'team_connection',None);epochs=getattr(app_state,'owner_epoch_provider',None)
        epoch=epochs.current(binding.owner_id) if epochs else None
        if (not adapter or not epoch or adapter.owner_id!=binding.owner_id or adapter.owner_epoch!=epoch
                or adapter.issuer!=binding.issuer or adapter._subject!=binding.subject or adapter.team_id!=binding.team_id):
            raise LLMFailure('shared_session_unavailable','Connect the same team account and select this team.')
        session_generation=adapter.generation
        try:
            claims=adapter._assertion()
            publication=adapter.command('get_publication',{'publication_id':binding.publication_id})
        except Exception:
            raise LLMFailure('shared_authority_unavailable','Shared Bot access could not be confirmed.') from None
        if (getattr(app_state,'team_connection',None) is not adapter or adapter.generation!=session_generation
                or epochs.current(binding.owner_id)!=epoch or publication.get('state')!='active'
                or publication.get('publication_id')!=binding.publication_id or publication.get('team_id')!=binding.team_id
                or claims.get('sub')!=binding.subject or type(publication.get('revision')) is not int
                or type(publication.get('revocation_epoch')) is not int
                or not isinstance(publication.get('role_text'),str) or not isinstance(publication.get('knowledge_text'),str)):
            raise LLMFailure('shared_authority_changed','Shared Bot access changed.')
        turn=SharedTurn(binding.owner_id,epoch,session_generation,binding.issuer,binding.subject,binding.team_id,
            binding.publication_id,publication['revision'],publication['revocation_epoch'],claims['authority_revision'],
            room_id,agent_id,root_id,generation,publication['role_text']+'\n\nShared knowledge:\n'+publication['knowledge_text'])
        if expected is not None and turn!=expected:
            raise LLMFailure('shared_authority_changed','Shared Bot access or configuration changed.')
        return turn


def require_read(db, binding, owner_id):
    from fastapi import HTTPException
    state=db.info.get('app_state')
    if state is None or binding.owner_id!=owner_id:raise HTTPException(404,'Room not found')
    with state.owner_recovery_lock:
        adapter=getattr(state,'team_connection',None);epochs=getattr(state,'owner_epoch_provider',None)
        epoch=epochs.current(owner_id) if epochs else None
        if (not adapter or not epoch or adapter.owner_id!=owner_id or adapter.owner_epoch!=epoch
                or adapter.issuer!=binding.issuer or adapter._subject!=binding.subject or adapter.team_id!=binding.team_id):
            raise HTTPException(404,'Room not found')
        generation=adapter.generation
        try:
            claims=adapter._assertion()
            publication=adapter.command('get_publication',{'publication_id':binding.publication_id})
            if (claims['sub']!=binding.subject or publication.get('state')!='active'
                    or publication.get('publication_id')!=binding.publication_id or publication.get('team_id')!=binding.team_id):raise ValueError()
        except Exception:raise HTTPException(404,'Room not found') from None
        if epochs.current(owner_id)!=epoch or getattr(state,'team_connection',None) is not adapter or adapter.generation!=generation:
            raise HTTPException(404,'Room not found')
