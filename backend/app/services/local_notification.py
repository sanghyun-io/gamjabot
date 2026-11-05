"""At-most-once local notification claims over existing Owner events."""
import hashlib,json
from datetime import datetime,timezone
from uuid import UUID,uuid4
from fastapi import HTTPException
from sqlalchemy import exists, text
from app.models.models import Agent,Conversation,Project,User
from app.models.collaboration import RoomEvent,RoomTrigger,ToolAction
from app.models.owner_routines import OwnerRoutineRun
from app.models.slack import SlackRun
from app.models.shared_text_runtime import SharedTextRoom
from app.models.local_notification import LocalNotificationSetting,LocalNotificationCommand,LocalNotificationSession,LocalNotificationDelivery

def now():return datetime.now(timezone.utc).replace(tzinfo=None)
def fail(status,code):raise HTTPException(status,{'code':code})
def valid_uuid(value):
    try:return str(UUID(value))==value
    except (ValueError,TypeError,AttributeError):return False

def owned_agent(db,owner,agent_id):
    user=db.get(User,owner);agent=db.get(Agent,agent_id)
    if not user or not user.is_active or not agent or not agent.is_active or agent.user_id!=owner:fail(409,'notification_agent_unavailable')
    return agent

def setting_dto(agent_id,row):
    return {'agent_id':agent_id,'revision':row.revision if row else 0,
        'completion':bool(row and row.completion),'failure':bool(row and row.failure),'approval':bool(row and row.approval)}

def settings(db,owner):
    agents=db.query(Agent).filter_by(user_id=owner,is_active=True).order_by(Agent.id).all()
    return {'version':1,'items':[setting_dto(agent.id,db.get(LocalNotificationSetting,(owner,agent.id))) for agent in agents]}

def write_setting(db,owner,agent_id,value,current):
    if not valid_uuid(value.operation_id):fail(422,'notification_operation_invalid')
    digest=hashlib.sha256(json.dumps(value.model_dump(),sort_keys=True,separators=(',',':')).encode()).hexdigest()
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        if db.get(LocalNotificationCommand,(owner,value.operation_id)):fail(409,'notification_operation_exists')
        owned_agent(db,owner,agent_id);row=db.get(LocalNotificationSetting,(owner,agent_id))
        if (row.revision if row else 0)!=value.expected_revision:fail(409,'notification_revision_conflict')
        if not row:
            row=LocalNotificationSetting(owner_id=owner,agent_id=agent_id,revision=1,completion=value.completion,failure=value.failure,approval=value.approval,updated_at=now());db.add(row)
        else:
            row.revision+=1;row.completion=value.completion;row.failure=value.failure;row.approval=value.approval;row.updated_at=now()
        current()
        db.add(LocalNotificationCommand(owner_id=owner,operation_id=value.operation_id,request_digest=digest,agent_id=agent_id,revision=row.revision,created_at=now()))
        result={'version':1,'operation_id':value.operation_id,'state':'saved','setting':setting_dto(agent_id,row)}
        db.commit();return result
    except BaseException:db.rollback();raise

def command(db,owner,operation_id):
    if not valid_uuid(operation_id):fail(422,'notification_operation_invalid')
    row=db.get(LocalNotificationCommand,(owner,operation_id))
    if not row:fail(404,'notification_command_not_found')
    return {'version':1,'operation_id':operation_id,'state':'saved','agent_id':row.agent_id,'revision':row.revision}

def resolve_setting_command(db,owner,agent_id,value,current):
    """Inspect exact request identity and revision under the same write lock as save."""
    if not valid_uuid(value.operation_id):fail(422,'notification_operation_invalid')
    digest=hashlib.sha256(json.dumps(value.model_dump(),sort_keys=True,separators=(',',':')).encode()).hexdigest()
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        current()
        command_row=db.get(LocalNotificationCommand,(owner,value.operation_id))
        if command_row:
            if (command_row.agent_id!=agent_id or command_row.request_digest!=digest
                    or command_row.revision!=value.expected_revision+1):fail(409,'notification_operation_mismatch')
            state='saved'
        else:
            owned_agent(db,owner,agent_id)
            setting=db.get(LocalNotificationSetting,(owner,agent_id))
            revision=setting.revision if setting else 0
            state='superseded' if revision>value.expected_revision else 'pending' if revision==value.expected_revision else 'unknown'
        current();db.commit()
        return {'version':1,'operation_id':value.operation_id,'agent_id':agent_id,'expected_revision':value.expected_revision,'state':state}
    except BaseException:db.rollback();raise

def open_session(db,owner,epoch,current):
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        if not db.get(User,owner) or not db.get(User,owner).is_active:fail(409,'notification_owner_changed')
        for previous in db.query(LocalNotificationDelivery).filter_by(owner_id=owner,state='attempted').all():
            previous.state='unknown';previous.updated_at=now()
        db.query(LocalNotificationSession).filter_by(owner_id=owner).delete(synchronize_session=False)
        row=LocalNotificationSession(session_id=str(uuid4()),owner_id=owner,owner_epoch=epoch,started_at=now());db.add(row)
        current();db.commit();return {'version':1,'session_id':row.session_id}
    except BaseException:db.rollback();raise

def room_ok(db,owner,agent,room_id):
    room=db.get(Conversation,room_id);project=db.get(Project,room.project_id) if room else None
    return bool(room and room.user_id==owner and room.is_active and project and project.user_id==owner and project.is_active
        and not project.archived_at and not db.query(SharedTextRoom).filter_by(room_id=room_id).first()
        and agent and agent.user_id==owner and agent.is_active)

def completion_valid(db,event):
    payload=event.payload if isinstance(event.payload,dict) else {}
    root=payload.get('trigger_event_id')
    if not isinstance(root,str):return False
    trigger=db.get(RoomTrigger,root)
    done=bool(trigger and trigger.room_id==event.room_id and trigger.state=='done')
    if not done:
        done=bool(db.query(OwnerRoutineRun).filter_by(root_event_id=root,result_event_id=event.event_id,state='completed').first()
            or db.query(SlackRun).filter_by(root_event_id=root,result_event_id=event.event_id,state='completed').first())
    if not done:return False
    later=db.query(RoomEvent.event_id).filter(RoomEvent.room_id==event.room_id,RoomEvent.actor_type=='agent',RoomEvent.actor_id==event.actor_id,
        RoomEvent.kind.in_(('message','failure')),RoomEvent.seq>event.seq,RoomEvent.payload['trigger_event_id'].as_string()==root).first()
    return later is None

def source_valid(db,owner,kind,source_id,room_id,agent_id):
    if kind not in ('approval','failure','completion'):return None
    agent=db.get(Agent,agent_id)
    if not room_ok(db,owner,agent,room_id):return None
    if kind=='approval':
        action=db.get(ToolAction,source_id)
        if not action or action.room_id!=room_id or action.agent_id!=agent_id or action.state!='waiting':return None
        root=db.get(RoomEvent,action.trigger_event_id) if action.trigger_event_id else None
        return {'action_id':source_id,'event_id':root.event_id if root and root.room_id==room_id else None,'seq':root.seq if root and root.room_id==room_id else None}
    event=db.get(RoomEvent,source_id)
    if not event or event.room_id!=room_id or event.actor_type!='agent' or event.actor_id!=agent_id:return None
    if kind=='failure' and event.kind!='failure':return None
    if kind=='completion' and (event.kind!='message' or not completion_valid(db,event)):return None
    return {'action_id':None,'event_id':event.event_id,'seq':event.seq}

def next_item(db,owner,epoch,session_id,current):
    if not valid_uuid(session_id):fail(422,'notification_session_invalid')
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        session=db.get(LocalNotificationSession,session_id)
        if not session or session.owner_id!=owner or session.owner_epoch!=epoch:fail(409,'notification_session_changed')
        candidates=[]
        settings_rows=db.query(LocalNotificationSetting).filter_by(owner_id=owner).all()
        for setting in settings_rows:
            agent=db.get(Agent,setting.agent_id)
            if not agent or not agent.is_active or agent.user_id!=owner:continue
            since=max(session.started_at,setting.updated_at)
            if setting.approval:
                used=exists().where(LocalNotificationDelivery.owner_id==owner,LocalNotificationDelivery.kind=='approval',LocalNotificationDelivery.source_id==ToolAction.action_id)
                for action in db.query(ToolAction).filter(ToolAction.agent_id==agent.id,ToolAction.state=='waiting',ToolAction.requested_at>=since,~used).order_by(ToolAction.requested_at,ToolAction.action_id):
                    candidates.append((action.requested_at,'approval',action.action_id,action.room_id,agent.id))
            kinds=[]
            if setting.completion:kinds.append('message')
            if setting.failure:kinds.append('failure')
            if kinds:
                used=exists().where(LocalNotificationDelivery.owner_id==owner,LocalNotificationDelivery.source_id==RoomEvent.event_id,LocalNotificationDelivery.kind.in_(('completion','failure')))
                for event in db.query(RoomEvent).filter(RoomEvent.actor_type=='agent',RoomEvent.actor_id==agent.id,RoomEvent.kind.in_(kinds),RoomEvent.created_at>=since,~used).order_by(RoomEvent.created_at,RoomEvent.event_id):
                    candidates.append((event.created_at,'completion' if event.kind=='message' else 'failure',event.event_id,event.room_id,agent.id))
        for _,kind,source,room,agent in sorted(candidates,key=lambda item:(item[0],item[1],item[2])):
            locator=source_valid(db,owner,kind,source,room,agent)
            if locator is None:continue
            t=now();row=LocalNotificationDelivery(delivery_id=str(uuid4()),owner_id=owner,owner_epoch=epoch,
                session_id=session_id,agent_id=agent,kind=kind,source_id=source,room_id=room,state='attempted',created_at=t,updated_at=t)
            current();db.add(row);db.commit()
            return {'version':1,'item':{'delivery_id':row.delivery_id,'kind':kind,'room_id':room,'agent_id':agent,**locator}}
        db.commit();return {'version':1,'item':None}
    except BaseException:db.rollback();raise

def finish(db,owner,epoch,delivery_id,state,current):
    if not valid_uuid(delivery_id):fail(422,'notification_delivery_invalid')
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        row=db.get(LocalNotificationDelivery,delivery_id)
        if not row or row.owner_id!=owner or row.owner_epoch!=epoch:fail(404,'notification_delivery_unavailable')
        if row.state=='attempted':row.state=state;row.updated_at=now()
        elif row.state!=state:fail(409,'notification_result_conflict')
        current();result={'version':1,'delivery_id':delivery_id,'state':row.state};db.commit();return result
    except BaseException:db.rollback();raise

def resolve(db,owner,epoch,delivery_id):
    if not valid_uuid(delivery_id):fail(422,'notification_delivery_invalid')
    row=db.get(LocalNotificationDelivery,delivery_id)
    if not row or row.owner_id!=owner or row.owner_epoch!=epoch:fail(404,'notification_delivery_unavailable')
    setting=db.get(LocalNotificationSetting,(owner,row.agent_id))
    if not setting or not getattr(setting,row.kind,False):fail(404,'notification_delivery_unavailable')
    locator=source_valid(db,owner,row.kind,row.source_id,row.room_id,row.agent_id)
    if locator is None:fail(404,'notification_delivery_unavailable')
    return {'version':1,'delivery_id':delivery_id,'kind':row.kind,'room_id':row.room_id,'agent_id':row.agent_id,**locator}

def ready(db,owner,epoch,delivery_id):
    if not valid_uuid(delivery_id):fail(422,'notification_delivery_invalid')
    row=db.get(LocalNotificationDelivery,delivery_id)
    if not row or row.owner_id!=owner or row.owner_epoch!=epoch or row.state!='attempted':fail(404,'notification_delivery_unavailable')
    session=db.get(LocalNotificationSession,row.session_id)
    if not session or session.owner_id!=owner or session.owner_epoch!=epoch:fail(409,'notification_session_changed')
    setting=db.get(LocalNotificationSetting,(owner,row.agent_id))
    if not setting or not getattr(setting,row.kind,False) or source_valid(db,owner,row.kind,row.source_id,row.room_id,row.agent_id) is None:
        fail(404,'notification_delivery_unavailable')
    return {'version':1,'delivery_id':delivery_id,'session_id':session.session_id,'ready':True}
