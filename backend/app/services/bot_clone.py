"""Owner-reviewed Bot copy; no memory, credentials, actions or history are copied."""
import json
from hashlib import sha256
from uuid import uuid4
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text
from app.models.models import Agent, Project, User
from app.models.bot_profile import BotOwnerProfile
from app.models.collaboration import ProviderConnection
from app.models.owner_skills import OwnerSkillBinding
from app.models.owner_routines import OwnerRoutine
from app.models.mcp import MCPBinding
from app.core.agent_avatar import PART_COUNTS
from app.schemas.agent import AgentCreate
from app.schemas.bot_profile import BotConversationResolve
from app.schemas.owner_routines import RoutineWrite
from app.services import agent_mutations, bot_profile, owner_routines, owner_skills
from app.services.bot_configuration_binding import fingerprint


def fail(code, status=422):
    raise HTTPException(status, {'code':'bot_clone_'+code})


def _key(key):
    if not isinstance(key,str) or not 1<=len(key)<=200 or not key.isprintable(): fail('key_invalid')
    return key


def _name(name):
    if not name.strip() or name!=name.strip() or not name.isprintable(): fail('name_invalid')
    return name


def _hash(value):
    return sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()


def _visible_avatar(value, agent_id):
    # The UI renders a deterministic source-ID placeholder for absent/invalid
    # recipes; persist that exact visible recipe on the copy, never a new random face.
    if (type(value) is dict and type(value.get('version')) in (int,float) and value['version']==1
            and all(type(value.get(part)) in (int,float) and 0<=value[part]<count and int(value[part])==value[part] for part,count in PART_COUNTS.items())):
        return {'version':1,**{part:int(value[part]) for part in PART_COUNTS}}
    seed=agent_id if type(agent_id) is int and abs(agent_id)<=9007199254740991 else 0
    seed &= 0xffffffff
    result={'version':1}
    for part,count in PART_COUNTS.items():
        seed=(seed*1664525+1013904223)&0xffffffff
        result[part]=seed%count
    return result


def _snapshot(db, owner, source_id, name):
    source=bot_profile.owned_agent(db,owner,source_id)
    user=db.get(User,owner)
    if not user or not user.is_active: fail('source_unavailable',404)
    connection=(db.query(ProviderConnection.id,ProviderConnection.user_id,ProviderConnection.enabled,
        ProviderConnection.readiness_revision,ProviderConnection.auth_grant_revision,
        ProviderConnection.provider_id,ProviderConnection.protocol_id)
        .filter(ProviderConnection.id==source.connection_id,ProviderConnection.user_id==owner).first())
    if not connection or not source.model_id: fail('source_unavailable',409)
    profile={k:getattr(source,k) for k in ('description','system_prompt','connection_id','model_id','temperature','max_tokens')}
    profile['avatar']=_visible_avatar(source.avatar,source.id)
    try:
        AgentCreate.model_validate({'name':name,**profile})
    except ValidationError:
        fail('source_unavailable',409)
    skills=owner_skills.projected(db,owner,source_id)
    routines=(db.query(OwnerRoutine).filter_by(owner_id=owner,agent_id=source_id,deleted_at=None)
              .order_by(OwnerRoutine.routine_id).all())
    if len(routines)>50: fail('routine_limit',409)
    source_fp=fingerprint(source)
    for row in routines:
        if not bot_profile.is_personal_conversation(db,owner,source_id,row.room_id):
            fail('source_routine_unavailable',409)
        try: RoutineWrite.model_validate({'name':row.name,'instruction':row.instruction,'schedule':row.schedule,'enabled':row.enabled})
        except ValidationError: fail('source_routine_unavailable',409)
    routine_values=[{'routine_id':r.routine_id,'revision':r.revision,'room_id':r.room_id,'name':r.name,
                     'instruction':r.instruction,'schedule':r.schedule,'enabled':r.enabled,
                     'profile_fingerprint':r.profile_fingerprint} for r in routines]
    bindings=(db.query(MCPBinding).filter_by(owner_id=owner,agent_id=source_id)
              .order_by(MCPBinding.server_id).all())
    mcp=[{'server_id':x.server_id,'revision':x.revision,'enabled':x.enabled,'tool_ids':x.tool_ids} for x in bindings]
    authority={'id':connection.id,'owner_id':connection.user_id,'enabled':connection.enabled,
               'readiness_revision':connection.readiness_revision,'auth_grant_revision':connection.auth_grant_revision,
               'provider_id':connection.provider_id,'protocol_id':connection.protocol_id}
    owner_profile=db.get(BotOwnerProfile,owner)
    project=db.get(Project,owner_profile.personal_project_id) if owner_profile and owner_profile.personal_project_id else None
    owner_scope={'revision':owner_profile.revision if owner_profile else None,
                 'personal_project_id':owner_profile.personal_project_id if owner_profile else None,
                 'project_owner':project.user_id if project else None,'project_active':bool(project.is_active) if project else None}
    snapshot={'version':1,'owner_id':owner,'source_agent_id':source_id,'name':name,'source_fingerprint':source_fp,
              'profile':profile,'connection':authority,'owner_scope':owner_scope,'skills':skills,'routines':routine_values,'mcp':mcp}
    return snapshot,_hash(snapshot)


def _preview(snapshot, plan_hash):
    at=owner_routines.now()
    routines=[{'routine_id':r['routine_id'],'revision':r['revision'],'name':r['name'],
               'instruction':r['instruction'],'schedule':r['schedule'],'enabled':r['enabled'],
               'next_run_at':owner_routines.stamp(owner_routines.next_time(r['schedule'],at)) if r['enabled'] else None,
               'webhook_setup_required':r['schedule']['kind']=='webhook'} for r in snapshot['routines']]
    return {'version':1,'source_agent_id':snapshot['source_agent_id'],'name':snapshot['name'],'plan_hash':plan_hash,
            'profile':snapshot['profile'],
            'skills':[{'skill_id':s['skill_id'],'revision':s['revision'],'binding_revision':s['binding_revision'],'name':s['name']} for s in snapshot['skills']],
            'routines':routines,'mcp_reconnect_required':any(b['enabled'] for b in snapshot['mcp'])}


def preview(db, owner, source_id, name):
    name=_name(name);db.rollback()
    try:
        db.execute(text('BEGIN'))
        snapshot,plan_hash=_snapshot(db,owner,source_id,name)
        result=_preview(snapshot,plan_hash);db.rollback();return result
    except BaseException:
        db.rollback();raise


def commit(db,owner,source_id,name,plan_hash,key):
    key=_key(key);name=_name(name)
    signature=_hash({'source_agent_id':source_id,'name':name,'plan_hash':plan_hash})
    db.rollback()
    try:
        db.execute(text('BEGIN IMMEDIATE'))
        prior=db.execute(text('SELECT source_agent_id,request_digest,response FROM bot_clone_commands WHERE owner_id=:owner AND idempotency_key=:key'),{'owner':owner,'key':key}).first()
        if prior:
            if prior.source_agent_id!=source_id or prior.request_digest!=signature: fail('idempotency_conflict',409)
            response=json.loads(prior.response);db.rollback();return {**response,'replayed':True}
        snapshot,current_hash=_snapshot(db,owner,source_id,name)
        if current_hash!=plan_hash: fail('changed',409)
        created=agent_mutations.stage_create(db,owner,AgentCreate.model_validate({'name':name,**snapshot['profile']}))
        room=bot_profile.stage_resolve_conversation(db,owner,created.id,BotConversationResolve())
        for skill in snapshot['skills']:
            db.add(OwnerSkillBinding(owner_id=owner,agent_id=created.id,skill_id=skill['skill_id'],revision=1,enabled=True))
        db.flush()
        # Reuse the existing total skill/context budget before anything commits.
        owner_skills.projected(db,owner,created.id)
        if snapshot['skills']:
            owner_skills._require_room_budgets(db,owner,created.id)
        at=owner_routines.now();new_profile=fingerprint(created)
        for routine in snapshot['routines']:
            schedule=routine['schedule']
            db.add(OwnerRoutine(routine_id=str(uuid4()),owner_id=owner,agent_id=created.id,room_id=room['room_id'],
                revision=1,name=routine['name'],instruction=routine['instruction'],schedule=schedule,enabled=routine['enabled'],
                profile_fingerprint=new_profile,next_run_at=owner_routines.next_time(schedule,at) if routine['enabled'] else None,
                created_at=at,updated_at=at))
        db.flush()
        result={'version':1,'source_agent_id':source_id,'name':name,'plan_hash':plan_hash,
                'agent_id':created.id,'room_id':room['room_id'],'project_id':room['project_id'],'href':room['href'],
                'skill_count':len(snapshot['skills']),'routine_count':len(snapshot['routines']),
                'webhook_setup_required':any(r['schedule']['kind']=='webhook' for r in snapshot['routines']),
                'mcp_reconnect_required':any(b['enabled'] for b in snapshot['mcp']),'replayed':False}
        db.execute(text('INSERT INTO bot_clone_commands(owner_id,idempotency_key,source_agent_id,request_digest,response) VALUES(:owner,:key,:source,:digest,:response)'),
                   {'owner':owner,'key':key,'source':source_id,'digest':signature,'response':json.dumps(result,ensure_ascii=False,separators=(',',':'))})
        db.commit();return result
    except BaseException:
        db.rollback();raise


def receipt(db,owner,source_id,key):
    key=_key(key)
    row=db.execute(text('SELECT response FROM bot_clone_commands WHERE owner_id=:owner AND idempotency_key=:key AND source_agent_id=:source'),
                   {'owner':owner,'key':key,'source':source_id}).first()
    if not row: fail('command_not_found',404)
    return {**json.loads(row.response),'replayed':True}
