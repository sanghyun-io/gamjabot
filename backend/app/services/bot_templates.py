"""Owner-reviewed portable Bot export and atomic import; never publishes."""
import hmac
import json
from hashlib import sha256
from uuid import uuid4
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text
from app.models.models import Project, User
from app.models.bot_profile import BotOwnerProfile
from app.models.collaboration import ProviderConnection
from app.models.owner_skills import OwnerSkill, OwnerSkillVersion, OwnerSkillBinding
from app.models.owner_routines import OwnerRoutine
from app.schemas.agent import AgentCreate
from app.schemas.bot_profile import BotConversationResolve
from app.schemas.bot_template_portable import portable_bytes, TemplateTooLarge
from app.services import agent_mutations, bot_profile, owner_routines, owner_skills
from app.services.bot_clone import _visible_avatar
from app.services.bot_configuration_binding import fingerprint

MAX_BYTES=128*1024

def fail(code,status=422):
    raise HTTPException(status,{'code':'bot_template_'+code})

def canonical(value):
    try:
        raw=json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
    except (TypeError,ValueError,UnicodeError,RecursionError): fail('invalid')
    if len(raw)>MAX_BYTES: fail('too_large')
    return raw

def digest(value): return sha256(canonical(value)).hexdigest()

def checked(template):
    try:
        value,_,_=portable_bytes(template)
        return value
    except ValidationError: fail('invalid')
    except TemplateTooLarge: fail('too_large')
    except (ValueError,AttributeError,TypeError,UnicodeError,RecursionError): fail('invalid')

def key_checked(key):
    if not isinstance(key,str) or not 1<=len(key)<=200 or not key.isprintable(): fail('key_invalid')
    return key

def export(db,owner,source_id):
    source=bot_profile.owned_agent(db,owner,source_id)
    skills=owner_skills.projected(db,owner,source_id)
    rows=(db.query(OwnerRoutine).filter_by(owner_id=owner,agent_id=source_id,deleted_at=None)
          .order_by(OwnerRoutine.routine_id).all())
    if len(rows)>50: fail('routine_limit',409)
    value={'version':1,'name':source.name,'description':source.description,'system_prompt':source.system_prompt,
        'avatar':_visible_avatar(source.avatar,source.id),
        'settings':{'temperature':source.temperature,'max_tokens':source.max_tokens},
        'skills':[{k:s[k] for k in ('name','description','content')} for s in skills],
        'routines':[{'name':r.name,'instruction':r.instruction,'schedule':r.schedule,'enabled':r.enabled} for r in rows]}
    value=checked(value)
    return {'version':1,'template':value,'template_sha256':digest(value)}

def authority(db,owner,connection_id):
    user=db.get(User,owner)
    if not user or not user.is_active: fail('owner_unavailable',404)
    conn=(db.query(ProviderConnection.id,ProviderConnection.user_id,ProviderConnection.enabled,
        ProviderConnection.readiness_revision,ProviderConnection.auth_grant_revision,
        ProviderConnection.provider_id,ProviderConnection.protocol_id)
        .filter_by(id=connection_id,user_id=owner).first())
    if not conn: fail('connection_unavailable',404)
    profile=db.get(BotOwnerProfile,owner)
    project=db.get(Project,profile.personal_project_id) if profile and profile.personal_project_id else None
    return {'owner':owner,'connection':list(conn),'profile_revision':profile.revision if profile else None,
        'project_id':profile.personal_project_id if profile else None,
        'skill_library_count':db.query(OwnerSkill).filter_by(owner_id=owner).count(),
        'project_owner':project.user_id if project else None,'project_active':bool(project.is_active) if project else None}

def plan(db,owner,value):
    template=checked(value.template.model_dump(mode='json'))
    template_hash=digest(template)
    spec={'template_sha256':template_hash,'name':value.name,'connection_id':value.connection_id,'model_id':value.model_id}
    scope=authority(db,owner,value.connection_id)
    plan_hash=digest({'spec':spec,'authority':scope})
    when=owner_routines.now()
    routines=[{'name':r['name'],'schedule':r['schedule'],'enabled':r['enabled'],
        'next_run_at':owner_routines.stamp(owner_routines.next_time(r['schedule'],when)) if r['enabled'] and r['schedule']['kind']!='webhook' else None,
        'webhook_setup_required':r['schedule']['kind']=='webhook',
        'availability':('paused' if not r['enabled'] else 'webhook_setup_required' if r['schedule']['kind']=='webhook'
            else 'connection_disabled' if not scope['connection'][2] else 'requires_runtime_checks')} for r in template['routines']]
    return template,{'version':1,**spec,'plan_hash':plan_hash,'skills_count':len(template['skills']),
        'routines':routines,'connection_enabled':bool(scope['connection'][2]),'app_bindings_copied':False}

def preview(db,owner,value):
    db.rollback()
    try:
        db.execute(text('BEGIN'))
        _,result=plan(db,owner,value)
        db.rollback();return result
    except BaseException:
        db.rollback();raise

def commit(db,owner,value,key):
    key_checked(key)
    template=checked(value.template.model_dump(mode='json'))
    signature=digest({'template_sha256':digest(template),'name':value.name,'connection_id':value.connection_id,
        'model_id':value.model_id,'plan_hash':value.plan_hash})
    db.rollback()
    try:
        db.execute(text('BEGIN IMMEDIATE'))
        prior=db.execute(text('SELECT request_digest,response FROM bot_template_import_commands WHERE owner_id=:owner AND idempotency_key=:key'),{'owner':owner,'key':key}).first()
        if prior:
            if not hmac.compare_digest(prior.request_digest,signature): fail('idempotency_conflict',409)
            result=json.loads(prior.response);db.rollback();return {**result,'replayed':True}
        template,review=plan(db,owner,value)
        if not hmac.compare_digest(review['plan_hash'],value.plan_hash): fail('changed',409)
        if db.query(OwnerSkill).filter_by(owner_id=owner).count()+len(template['skills'])>200: fail('skill_library_limit',409)
        created=agent_mutations.stage_create(db,owner,AgentCreate.model_validate({'name':value.name,
            'description':template['description'],'system_prompt':template['system_prompt'],'avatar':template['avatar'],
            'connection_id':value.connection_id,'model_id':value.model_id,**template['settings']}))
        room=bot_profile.stage_resolve_conversation(db,owner,created.id,BotConversationResolve())
        at=owner_routines.now()
        for skill in template['skills']:
            row=OwnerSkill(skill_id=str(uuid4()),owner_id=owner,revision=1,active=True,source=None,
                package_provenance=None,created_at=at,updated_at=at,**skill)
            db.add(row);db.flush()
            db.add(OwnerSkillVersion(skill_id=row.skill_id,revision=1,snapshot=owner_skills.dto(row)))
            db.add(OwnerSkillBinding(owner_id=owner,agent_id=created.id,skill_id=row.skill_id,revision=1,enabled=True))
        db.flush();owner_skills.projected(db,owner,created.id)
        if template['skills']: owner_skills._require_room_budgets(db,owner,created.id)
        for routine in template['routines']:
            schedule=routine['schedule']
            db.add(OwnerRoutine(routine_id=str(uuid4()),owner_id=owner,agent_id=created.id,room_id=room['room_id'],
                revision=1,profile_fingerprint=fingerprint(created),next_run_at=owner_routines.next_time(schedule,at) if routine['enabled'] else None,
                created_at=at,updated_at=at,**routine))
        db.flush()
        result={'version':1,'name':value.name,'connection_id':value.connection_id,'model_id':value.model_id,
            'template_sha256':review['template_sha256'],'plan_hash':value.plan_hash,'agent_id':created.id,
            'room_id':room['room_id'],'project_id':room['project_id'],'href':room['href'],
            'skill_count':len(template['skills']),'routine_count':len(template['routines']),
            'webhook_setup_required':any(r['schedule']['kind']=='webhook' for r in template['routines']),
            'app_bindings_copied':False,'replayed':False}
        db.execute(text('INSERT INTO bot_template_import_commands(owner_id,idempotency_key,request_digest,response) VALUES(:owner,:key,:digest,:response)'),
            {'owner':owner,'key':key,'digest':signature,'response':json.dumps(result,ensure_ascii=False,separators=(',',':'))})
        db.commit();return result
    except BaseException:
        db.rollback();raise

def receipt(db,owner,key):
    key_checked(key)
    row=db.execute(text('SELECT response FROM bot_template_import_commands WHERE owner_id=:owner AND idempotency_key=:key'),{'owner':owner,'key':key}).first()
    if not row: fail('command_not_found',404)
    return {**json.loads(row.response),'replayed':True}
