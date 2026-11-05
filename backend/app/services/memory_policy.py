"""Owner-controlled Bot memory visibility; membership is not a permission grant."""
from sqlalchemy import text,func
from fastapi import HTTPException
from app.models.owner_common_memory import BotMemoryPolicy
from app.models.models import Agent,Conversation,Project,User
from app.models.collaboration import RoomMembership,RoomEvent
from app.services import bot_memory,bot_profile


def get(db,owner_id,agent_id):
    bot_memory._agent(db,owner_id,agent_id)
    row=db.get(BotMemoryPolicy,(owner_id,agent_id))
    return {'version':1,'agent_id':agent_id,'revision':row.revision if row else 0,'mode':row.mode if row else 'canonical_only'}


def eligible(db,owner_id,agent_id,room_id):
    owner=db.get(User,owner_id);agent=db.get(Agent,agent_id);room=db.get(Conversation,room_id)
    project=db.get(Project,room.project_id) if room else None;member=db.get(RoomMembership,(room_id,agent_id))
    if not (owner and owner.is_active and agent and agent.is_active and agent.user_id==owner_id and room and room.is_active
            and room.user_id==owner_id and project and project.is_active and project.user_id==owner_id and member and member.is_active):
        return False,False
    if bot_profile.is_personal_conversation(db,owner_id,agent_id,room_id):return True,False
    policy=db.get(BotMemoryPolicy,(owner_id,agent_id))
    if not policy or policy.mode!='owner_rooms':return False,False
    members=db.query(RoomMembership).filter_by(room_id=room_id,is_active=True).all()
    if any(not (a:=db.get(Agent,m.agent_id)) or not a.is_active or a.user_id!=owner_id for m in members):return False,False
    # Noncanonical rooms are presented as group-capable even with one active
    # Bot; a later member can read earlier replies. Never classify them as
    # private solely from a momentary member count.
    return True,True


def advance_floors(db,owner_id,agent_ids,*,exclude_room=None):
    """Invalidate each affected Bot's prior room history without touching others."""
    from app.services.room_memory import local_for
    rows=db.query(RoomMembership).join(Conversation,Conversation.id==RoomMembership.room_id).filter(
        Conversation.user_id==owner_id,RoomMembership.agent_id.in_(list(agent_ids))).all()
    for member in rows:
        if exclude_room is not None and member.room_id==exclude_room:continue
        seq=db.query(func.max(RoomEvent.seq)).filter_by(room_id=member.room_id).scalar() or 0
        local=local_for(db,member.room_id,member.agent_id)
        local.revision+=1;local.context_after_seq=max(local.context_after_seq,seq)


def set_policy(db,owner_id,agent_id,value):
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        bot_memory._agent(db,owner_id,agent_id,active=True)
        row=db.get(BotMemoryPolicy,(owner_id,agent_id));revision=row.revision if row else 0
        if revision!=value.expected_revision:raise HTTPException(409,{'code':'bot_memory_policy_changed'})
        if row is None:row=BotMemoryPolicy(owner_id=owner_id,agent_id=agent_id,revision=0,mode='canonical_only');db.add(row)
        if row.mode=='owner_rooms' and value.mode=='canonical_only':
            canonical=bot_profile.canonical_conversation(db,owner_id,agent_id)
            advance_floors(db,owner_id,[agent_id],exclude_room=canonical.id if canonical else None)
        row.mode=value.mode;row.revision+=1;db.flush()
        if value.mode=='owner_rooms':bot_memory._budget(db,owner_id,agent_id,None)
        db.commit();return get(db,owner_id,agent_id)
    except BaseException:db.rollback();raise


def direct_eligible(db,room_id,root_id,agent_id,generation):
    """Current direct Owner root only; delegated or peer jobs do not gain memory authority."""
    from datetime import timedelta
    from app.services import teammates,bot_messages,execution_scopes
    from app.services.action_review import ReviewChanged
    from app.services.llm_service import LLMFailure
    try:
        root,room,_=teammates._root(db,room_id,root_id)
        _,run=teammates._member(db,room,agent_id)
        if (run.state!='running' or run.generation!=generation or run.trigger_event_id!=root_id
                or execution_scopes.aware(root.created_at)+timedelta(seconds=300)<=execution_scopes.now()):return False
        allowed,_=eligible(db,room.user_id,agent_id,room_id)
        if not allowed:return False
        job=(run.pending_task or {}).get('execution_job')
        if job:
            origin=bot_messages.resolve_execution_origin(db,job.get('message_id'),job.get('phase'),room_id,agent_id,generation)
            if (origin['context_mode']!='user_room' or origin['root_room_id']!=room_id
                    or origin['owner_id']!=room.user_id or origin['root_request_event_id']!=root_id):return False
        return True
    except (LLMFailure,ReviewChanged,HTTPException):return False
