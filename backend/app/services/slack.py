"""Secret-free Slack scope and durable execution records."""
import re
from datetime import datetime, timezone
from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import text
from app.models.models import Agent, Project, ProjectAgent, User
from app.models.collaboration import ProviderConnection
from app.models.shared_bot import SharedBotPublication
from app.models.slack import SlackConfig, SlackInbox, SlackRun, SlackOutbox, SlackCommand, SlackReplyResolution

ID = re.compile(r'^[A-Z0-9]{1,64}$')
def now(): return datetime.now(timezone.utc).replace(tzinfo=None)
def fail(status, code): raise HTTPException(status, {'code':code})
def uuid_valid(value):
    try: return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError): return False

def validate_target(db, owner, agent_id, project_id, connection_id, model_id):
    user = db.get(User, owner); agent = db.get(Agent, agent_id); project = db.get(Project, project_id)
    connection = db.get(ProviderConnection,connection_id)
    if (not user or not user.is_active or not agent or not agent.is_active or agent.user_id != owner
        or not project or not project.is_active or project.archived_at or project.user_id != owner
        or not db.query(ProjectAgent).filter_by(project_id=project_id, agent_id=agent_id).first()
        or agent.connection_id != connection_id or agent.model_id != model_id
        or not connection or connection.user_id!=owner or not connection.enabled
        or db.query(SharedBotPublication).filter_by(owner_id=owner, agent_id=agent_id).first()):
        fail(409, 'slack_target_unavailable')
    return agent

def targets(db, owner):
    agents = db.query(Agent).filter_by(user_id=owner, is_active=True).order_by(Agent.id).all()
    result = []
    for agent in agents:
        connection=db.get(ProviderConnection,agent.connection_id) if agent.connection_id else None
        if not connection or connection.user_id!=owner or not connection.enabled or not agent.model_id or db.query(SharedBotPublication).filter_by(owner_id=owner, agent_id=agent.id).first(): continue
        projects = db.query(Project).join(ProjectAgent, ProjectAgent.project_id == Project.id).filter(
            ProjectAgent.agent_id == agent.id, Project.user_id == owner, Project.is_active.is_(True), Project.archived_at.is_(None)).order_by(Project.id).all()
        for project in projects:
            result.append({'agent_id':agent.id, 'agent_name':agent.name, 'project_id':project.id,
                'project_name':project.name, 'connection_id':agent.connection_id, 'connection_name':connection.name, 'model_id':agent.model_id})
    return {'version':1,'items':result}

def config_dto(row):
    if not row: return {'version':1,'revision':0,'state':'unconfigured'}
    return {'version':1,'revision':row.revision,'operation_id':row.operation_id,'state':row.state,
        'account_id':row.account_id,'credential_revision':row.credential_revision,'enabled':row.enabled,
        'workspace_id':row.workspace_id,'app_id':row.app_id,'channel_id':row.channel_id,'sender_ids':row.sender_ids,
        'agent_id':row.agent_id,'project_id':row.project_id,'connection_id':row.connection_id,'model_id':row.model_id}

def reserve(db, owner, value):
    if not uuid_valid(value.operation_id) or not uuid_valid(value.account_id): fail(422, 'slack_config_invalid')
    if (not re.fullmatch(r'T[A-Z0-9]{1,63}',value.workspace_id) or not re.fullmatch(r'A[A-Z0-9]{1,63}',value.app_id)
        or not re.fullmatch(r'[CG][A-Z0-9]{1,63}',value.channel_id)
        or not all(re.fullmatch(r'[UW][A-Z0-9]{1,63}',v) for v in value.sender_ids)
        or len(value.sender_ids)>64 or len(set(value.sender_ids))!=len(value.sender_ids)): fail(422,'slack_config_invalid')
    import hashlib,json
    signature=hashlib.sha256(json.dumps(value.model_dump(),sort_keys=True,separators=(',',':')).encode()).hexdigest()
    db.rollback(); db.execute(text('BEGIN IMMEDIATE'))
    try:
        if db.get(SlackCommand,(owner,value.operation_id)): fail(409,'slack_operation_exists')
        row = db.get(SlackConfig, owner)
        if row and row.state == 'pending': fail(409,'slack_config_pending')
        if (row.revision if row else 0) != value.expected_revision: fail(409,'slack_revision_conflict')
        if not value.enabled and row is not None:
            for field in ('account_id','credential_revision','workspace_id','app_id','channel_id','sender_ids','agent_id','project_id','connection_id','model_id'):
                if getattr(row,field)!=getattr(value,field):fail(409,'slack_disable_scope_changed')
        else:validate_target(db,owner,value.agent_id,value.project_id,value.connection_id,value.model_id)
        values = value.model_dump(exclude={'expected_revision','expected_owner_epoch','expected_core_epoch'})
        if row is None:
            row=SlackConfig(owner_id=owner,revision=1,state='pending',updated_at=now(),**values);db.add(row)
        else:
            for key,val in values.items(): setattr(row,key,val)
            row.revision += 1;row.state='pending';row.updated_at=now()
        db.add(SlackCommand(owner_id=owner,operation_id=value.operation_id,request_digest=signature,revision=row.revision,state='pending',created_at=now()))
        db.commit();return config_dto(row)
    except BaseException: db.rollback();raise

def finish(db, owner, operation_id, revision):
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        row=db.get(SlackConfig,owner)
        if not row or row.operation_id!=operation_id or row.revision!=revision or row.state!='pending': fail(409,'slack_config_changed')
        command=db.get(SlackCommand,(owner,operation_id))
        if not command or command.revision!=revision or command.state!='pending': fail(409,'slack_config_changed')
        command.state='configured';row.state='configured';row.updated_at=now();db.commit();return config_dto(row)
    except BaseException:db.rollback();raise

def events(db,owner):
    items=[]
    for row in db.query(SlackInbox).filter_by(owner_id=owner).order_by(SlackInbox.created_at.desc()).limit(50):
        run=db.get(SlackRun,row.run_id) if row.run_id else None
        out=db.get(SlackOutbox,row.run_id) if row.run_id else None
        from app.models.collaboration import ToolAction
        waiting=bool(run and run.state=='running' and db.query(ToolAction).filter(ToolAction.trigger_event_id==run.root_event_id,ToolAction.state.in_(('pending','waiting','approved'))).first())
        items.append({'event_id':row.event_id,'workspace_id':row.workspace_id,'channel_id':row.channel_id,
            'sender_id':row.user_id,'thread_ts':row.thread_ts,'state':'waiting_approval' if waiting else run.state if run else row.state,
            'room_id':run.room_id if run else None,'root_event_id':run.root_event_id if run else None,
            'result_event_id':run.result_event_id if run else None,'reply_state':out.state if out else None})
    return {'version':1,'items':items}

def unresolved_replies(db, owner, cursor=None):
    from sqlalchemy import and_,or_
    import base64,json
    query=(db.query(SlackRun,SlackInbox,SlackOutbox)
        .join(SlackInbox,(SlackInbox.owner_id==SlackRun.owner_id)&(SlackInbox.workspace_id==SlackRun.workspace_id)&(SlackInbox.event_id==SlackRun.event_id))
        .outerjoin(SlackOutbox,SlackOutbox.run_id==SlackRun.run_id)
        .outerjoin(SlackReplyResolution,and_(SlackReplyResolution.run_id==SlackRun.run_id,SlackReplyResolution.decision.in_(('owner_confirmed_sent','owner_skip_no_reply'))))
        .filter(SlackRun.owner_id==owner,or_(SlackOutbox.state.in_(('unknown','blocked','failed_before_dispatch')),
            and_(SlackOutbox.run_id.is_(None),SlackRun.state.in_(('failed','unknown','cancelled','interrupted')))),SlackReplyResolution.run_id.is_(None)))
    if cursor:
        try:
            if not isinstance(cursor,str) or len(cursor)>512:raise ValueError()
            data=json.loads(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)))
            if not isinstance(data,list) or len(data)!=3 or not all(isinstance(x,str) and 0<len(x)<=100 for x in data):raise ValueError()
            created=datetime.fromisoformat(data[0]);workspace,event_id=data[1:]
            if created.tzinfo is not None:raise ValueError()
        except Exception:fail(422,'slack_cursor_invalid')
        query=query.filter(or_(SlackInbox.created_at>created,and_(SlackInbox.created_at==created,SlackInbox.workspace_id>workspace),
            and_(SlackInbox.created_at==created,SlackInbox.workspace_id==workspace,SlackInbox.event_id>event_id)))
    rows=query.order_by(SlackInbox.created_at,SlackInbox.workspace_id,SlackInbox.event_id).limit(51).all()
    from app.models.collaboration import RoomEvent
    items=[]
    for run,inbox,out in rows[:50]:
        result=db.get(RoomEvent,run.result_event_id) if run.result_event_id else None
        content=(result.payload or {}).get('content') if result and result.room_id==run.room_id and result.actor_id==run.agent_id and result.kind=='message' and (result.payload or {}).get('trigger_event_id')==run.root_event_id else None
        items.append({'run_id':run.run_id,'event_id':inbox.event_id,'workspace_id':inbox.workspace_id,
            'channel_id':inbox.channel_id,'thread_ts':inbox.thread_ts,'sender_id':inbox.user_id,
            'state':out.state if out else run.state,'attempt_id':out.attempt_id if out else None,
            'config_revision':run.config_revision,'result_text':content if isinstance(content,str) else None})
    next_cursor=None
    if len(rows)>50:
        inbox=rows[49][1]
        next_cursor=base64.urlsafe_b64encode(json.dumps([inbox.created_at.isoformat(),inbox.workspace_id,inbox.event_id],separators=(',',':')).encode()).decode().rstrip('=')
    return {'version':1,'next_cursor':next_cursor,'items':items}

def resolve_reply(db,owner,epoch,run_id,value):
    if not uuid_valid(run_id) or not uuid_valid(value.operation_id) or (value.attempt_id is not None and not uuid_valid(value.attempt_id)):fail(422,'slack_resolution_invalid')
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        existing=db.query(SlackReplyResolution).filter_by(owner_id=owner,operation_id=value.operation_id).first()
        if existing:
            if (existing.run_id!=run_id or existing.attempt_id!=value.attempt_id or existing.decision!=value.decision
                or existing.config_revision!=value.expected_config_revision or existing.run_revision!=value.expected_run_revision
                or existing.expected_state!=value.expected_state or existing.owner_epoch!=epoch or existing.core_epoch!=value.expected_core_epoch):fail(409,'slack_resolution_conflict')
            db.commit();return {'version':1,'run_id':run_id,'decision':existing.decision,'operation_id':existing.operation_id}
        run=db.get(SlackRun,run_id);out=db.get(SlackOutbox,run_id);cfg=db.get(SlackConfig,owner)
        if not run or run.owner_id!=owner:fail(404,'slack_reply_not_found')
        inbox=db.get(SlackInbox,(owner,run.workspace_id,run.event_id))
        actual_state=out.state if out else run.state
        if ((cfg.revision if cfg else 0)!=value.expected_config_revision or run.config_revision!=value.expected_run_revision
            or not inbox or actual_state!=value.expected_state or (out.attempt_id if out else None)!=value.attempt_id
            or db.query(SlackReplyResolution).filter_by(run_id=run_id).filter(SlackReplyResolution.decision.in_(('owner_confirmed_sent','owner_skip_no_reply'))).first()):fail(409,'slack_reply_changed')
        allowed=(value.decision=='owner_confirmed_sent' and out is not None and out.state=='unknown'
            or value.decision=='owner_confirmed_not_sent' and out is not None and out.state=='unknown'
            or value.decision=='owner_hold_unknown' and out is None and run.state=='unknown'
            or value.decision=='owner_skip_no_reply' and (out is not None and out.state in ('blocked','failed_before_dispatch')
                or out is None and run.state in ('failed','cancelled','interrupted')))
        if not allowed:fail(409,'slack_resolution_not_allowed')
        if value.decision=='owner_confirmed_sent':
            from app.models.collaboration import RoomEvent
            result=db.get(RoomEvent,run.result_event_id) if run.result_event_id else None
            if (not result or result.room_id!=run.room_id or result.actor_type!='agent' or result.actor_id!=run.agent_id
                or result.kind!='message' or (result.payload or {}).get('trigger_event_id')!=run.root_event_id
                or not isinstance((result.payload or {}).get('content'),str) or not result.payload['content'].strip()):
                fail(409,'slack_result_unavailable')
        db.add(SlackReplyResolution(run_id=run_id,owner_id=owner,operation_id=value.operation_id,
            attempt_id=out.attempt_id if out else None,decision=value.decision,config_revision=value.expected_config_revision,
            run_revision=value.expected_run_revision,expected_state=value.expected_state,owner_epoch=epoch,core_epoch=value.expected_core_epoch,created_at=now()))
        db.commit();return {'version':1,'run_id':run_id,'decision':value.decision,'operation_id':value.operation_id}
    except BaseException:db.rollback();raise

def envelope_digest(event):
    import hashlib, json
    stable={k:event.get(k) for k in ('event_id','workspace_id','app_id','channel_id','user_id','event_ts','thread_ts','text','config_revision','credential_revision')}
    return hashlib.sha256(json.dumps(stable,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def accept(db,owner,epoch,event):
    """Commit the canonical inbound event before the caller acknowledges Core."""
    import re
    if not isinstance(event,dict): fail(422,'slack_event_invalid')
    fields={'delivery_id','event_id','workspace_id','app_id','channel_id','user_id','event_ts','thread_ts','text','config_revision','credential_revision'}
    if set(event)!=fields: fail(422,'slack_event_invalid')
    for name in ('delivery_id','event_id','workspace_id','app_id','channel_id','user_id','event_ts'):
        if not isinstance(event[name],str) or not 1<=len(event[name])<=100 or not event[name].isprintable(): fail(422,'slack_event_invalid')
    if event['thread_ts'] is not None and (not isinstance(event['thread_ts'],str) or not re.fullmatch(r'\d{1,20}\.\d{1,12}',event['thread_ts'])): fail(422,'slack_event_invalid')
    if not re.fullmatch(r'\d{1,20}\.\d{1,12}',event['event_ts']): fail(422,'slack_event_invalid')
    if not isinstance(event['text'],str) or not 1<=len(event['text'].encode('utf-8'))<=4000: fail(422,'slack_event_invalid')
    digest=envelope_digest(event)
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        cfg=db.get(SlackConfig,owner)
        if (not cfg or cfg.state!='configured' or not cfg.enabled or cfg.revision!=event['config_revision']
            or cfg.credential_revision!=event['credential_revision'] or cfg.workspace_id!=event['workspace_id']
            or cfg.app_id!=event['app_id'] or cfg.channel_id!=event['channel_id'] or event['user_id'] not in cfg.sender_ids): fail(409,'slack_scope_changed')
        validate_target(db,owner,cfg.agent_id,cfg.project_id,cfg.connection_id,cfg.model_id)
        prior=db.get(SlackInbox,(owner,event['workspace_id'],event['event_id']))
        if prior:
            if prior.digest!=digest: fail(409,'slack_event_conflict')
            db.commit();return False
        db.add(SlackInbox(owner_id=owner,workspace_id=event['workspace_id'],event_id=event['event_id'],digest=digest,app_id=event['app_id'],owner_epoch=epoch,
            config_revision=cfg.revision,credential_revision=cfg.credential_revision,channel_id=event['channel_id'],
            user_id=event['user_id'],event_ts=event['event_ts'],thread_ts=event['thread_ts'] or event['event_ts'],
            text=event['text'],state='queued',created_at=now()))
        db.commit();return True
    except BaseException:db.rollback();raise

def claim(db,owner,epoch,cursors=None):
    from uuid import uuid4
    from app.models.slack import SlackThread
    from app.models.collaboration import RoomAgentRun,RoomEvent,RoomTrigger
    from app.services import room_service
    from app.services.events import stage_event,ActorRef
    from app.services.bot_configuration_binding import fingerprint
    from app.services.owner_routines import room_limits
    from sqlalchemy import and_,or_
    # Read only a bounded page of identifiers before taking SQLite's write lock.
    # A per-owner cursor lets later threads progress even behind a large backlog.
    if cursors is None:cursors={}
    db.rollback()
    query=db.query(SlackInbox.created_at,SlackInbox.workspace_id,SlackInbox.event_id).filter_by(owner_id=owner,state='queued')
    cursor=cursors.get(owner)
    if cursor:
        created,workspace,event_id=cursor
        query=query.filter(or_(SlackInbox.created_at>created,
            and_(SlackInbox.created_at==created,SlackInbox.workspace_id>workspace),
            and_(SlackInbox.created_at==created,SlackInbox.workspace_id==workspace,SlackInbox.event_id>event_id)))
    page=query.order_by(SlackInbox.created_at,SlackInbox.workspace_id,SlackInbox.event_id).limit(32).all()
    db.rollback()
    if not page:
        cursors.pop(owner,None);return None
    db.execute(text('BEGIN IMMEDIATE'))
    try:
        cfg=db.get(SlackConfig,owner)
        if not cfg or cfg.state!='configured' or not cfg.enabled: db.commit();return None
        validate_target(db,owner,cfg.agent_id,cfg.project_id,cfg.connection_id,cfg.model_id)
        busy_threads=set()
        last=None
        for created,workspace,event_id in page:
            last=(created,workspace,event_id)
            row=db.get(SlackInbox,(owner,workspace,event_id))
            if not row or row.state!='queued' or row.created_at!=created:continue
            if row.owner_epoch!=epoch or row.config_revision!=cfg.revision or row.credential_revision!=cfg.credential_revision:
                row.state='blocked';continue
            key=(owner,row.workspace_id,row.channel_id,row.thread_ts)
            if key in busy_threads:continue
            # The cursor may begin midway through a thread: never overtake an
            # earlier queued event for that same thread, including across pages.
            earlier=db.query(SlackInbox.event_id).filter(
                SlackInbox.owner_id==owner,SlackInbox.workspace_id==row.workspace_id,
                SlackInbox.channel_id==row.channel_id,SlackInbox.thread_ts==row.thread_ts,
                SlackInbox.state=='queued',or_(SlackInbox.created_at<row.created_at,
                    and_(SlackInbox.created_at==row.created_at,SlackInbox.event_id<row.event_id))).first()
            if earlier:busy_threads.add(key);continue
            # A later request must not run ahead of an earlier unresolved reply.
            previous=(db.query(SlackRun,SlackOutbox,SlackReplyResolution)
                .join(SlackInbox, (SlackInbox.owner_id==SlackRun.owner_id)&(SlackInbox.workspace_id==SlackRun.workspace_id)&(SlackInbox.event_id==SlackRun.event_id))
                .outerjoin(SlackOutbox,SlackOutbox.run_id==SlackRun.run_id)
                .outerjoin(SlackReplyResolution,and_(SlackReplyResolution.run_id==SlackRun.run_id,SlackReplyResolution.decision.in_(('owner_confirmed_sent','owner_skip_no_reply'))))
                .filter(SlackInbox.owner_id==owner,SlackInbox.workspace_id==row.workspace_id,
                    SlackInbox.channel_id==row.channel_id,SlackInbox.thread_ts==row.thread_ts,
                    or_(SlackInbox.created_at<row.created_at,and_(SlackInbox.created_at==row.created_at,SlackInbox.event_id<row.event_id)),
                    or_(SlackOutbox.run_id.is_(None),SlackOutbox.state!='sent'),SlackReplyResolution.run_id.is_(None)).first())
            if previous:busy_threads.add(key);continue
            thread=db.get(SlackThread,key)
            if thread and (thread.agent_id!=cfg.agent_id or thread.project_id!=cfg.project_id):
                row.state='blocked';continue
            if not thread:
                room=room_service.stage_room_from_setup(db,owner,cfg.project_id,'Slack thread',[cfg.agent_id],
                    {'max_rounds':2,'max_tool_calls':8,'max_executor_seconds':60})
                thread=SlackThread(owner_id=owner,workspace_id=row.workspace_id,channel_id=row.channel_id,
                    thread_ts=row.thread_ts,room_id=room.id,agent_id=cfg.agent_id,project_id=cfg.project_id,created_at=now())
                db.add(thread);db.flush()
            member=db.get(RoomAgentRun,(thread.room_id,cfg.agent_id))
            if (not member or member.state!='idle' or member.paused or member.active_tool
                or db.query(RoomTrigger).filter(RoomTrigger.room_id==thread.room_id,RoomTrigger.state.in_(('pending','running','paused'))).first()
                or db.query(RoomEvent.event_id).outerjoin(RoomTrigger,RoomTrigger.event_id==RoomEvent.event_id).filter(
                    RoomEvent.room_id==thread.room_id,RoomEvent.actor_type=='user',RoomEvent.kind=='message',RoomTrigger.event_id.is_(None)).first()):
                busy_threads.add(key);continue
            break
        else:
            db.commit();cursors[owner]=last;return None
        max_calls,max_seconds=room_limits(db,thread.room_id);run_id=str(uuid4())
        payload={'category':'slack_external','content':row.text,'slack':{'version':1,'run_id':run_id,
            'event_id':row.event_id,'workspace_id':row.workspace_id,'channel_id':row.channel_id,
            'user_id':row.user_id,'thread_ts':row.thread_ts,'config_revision':cfg.revision}}
        root=stage_event(db,thread.room_id,ActorRef('system',None),'activity',payload,'slack:'+row.event_id)
        member.state='running';member.generation+=1;member.trigger_event_id=root.event_id
        member.active_tool={};member.pending_task={**dict(member.pending_task or {}),'slack_run':run_id}
        agent=db.get(Agent,cfg.agent_id)
        run=SlackRun(run_id=run_id,owner_id=owner,workspace_id=row.workspace_id,event_id=row.event_id,
            config_revision=cfg.revision,credential_revision=cfg.credential_revision,owner_epoch=epoch,
            room_id=thread.room_id,agent_id=cfg.agent_id,generation=member.generation,
            profile_fingerprint=fingerprint(agent),max_tool_calls=max_calls,max_executor_seconds=max_seconds,
            connection_id=cfg.connection_id,model_id=cfg.model_id,state='running',root_event_id=root.event_id,created_at=now())
        db.add(run);row.run_id=run_id;row.state='running';db.commit();cursors[owner]=last;return run_id
    except BaseException:db.rollback();raise

def current(db,run,epoch):
    from app.models.slack import SlackThread
    from app.models.collaboration import RoomAgentRun,RoomEvent
    from app.models.models import Conversation
    from app.services.bot_configuration_binding import fingerprint
    from app.services.owner_routines import room_limits
    if not run or run.state!='running' or not epoch or run.owner_epoch!=epoch:return None
    cfg=db.get(SlackConfig,run.owner_id,populate_existing=True)
    inbox=db.get(SlackInbox,(run.owner_id,run.workspace_id,run.event_id),populate_existing=True)
    root=db.get(RoomEvent,run.root_event_id,populate_existing=True)
    member=db.get(RoomAgentRun,(run.room_id,run.agent_id),populate_existing=True)
    if (not cfg or cfg.state!='configured' or not cfg.enabled or cfg.revision!=run.config_revision
        or cfg.credential_revision!=run.credential_revision or cfg.workspace_id!=run.workspace_id or cfg.agent_id!=run.agent_id
        or cfg.connection_id!=run.connection_id or cfg.model_id!=run.model_id
        or not inbox or inbox.app_id!=cfg.app_id or inbox.owner_epoch!=run.owner_epoch or inbox.run_id!=run.run_id or inbox.state!='running' or inbox.channel_id!=cfg.channel_id or inbox.user_id not in cfg.sender_ids or inbox.config_revision!=cfg.revision or inbox.credential_revision!=cfg.credential_revision
        or not root or root.room_id!=run.room_id or root.actor_type!='system' or root.kind!='activity'
        or root.payload.get('category')!='slack_external' or root.payload.get('content')!=inbox.text
        or root.payload.get('slack')!={'version':1,'run_id':run.run_id,'event_id':inbox.event_id,'workspace_id':inbox.workspace_id,'channel_id':inbox.channel_id,'user_id':inbox.user_id,'thread_ts':inbox.thread_ts,'config_revision':cfg.revision}
        or not member or member.state!='running' or member.paused or member.generation!=run.generation
        or member.trigger_event_id!=run.root_event_id or (member.pending_task or {}).get('slack_run')!=run.run_id
        or room_limits(db,run.room_id)!=(run.max_tool_calls,run.max_executor_seconds)):return None
    try: agent=validate_target(db,run.owner_id,run.agent_id,cfg.project_id,run.connection_id,run.model_id)
    except HTTPException:return None
    thread=db.get(SlackThread,(run.owner_id,inbox.workspace_id,inbox.channel_id,inbox.thread_ts))
    room=db.get(Conversation,run.room_id,populate_existing=True)
    if (not thread or thread.room_id!=run.room_id or thread.agent_id!=run.agent_id or thread.project_id!=cfg.project_id
        or not room or not room.is_active or room.user_id!=run.owner_id or room.project_id!=cfg.project_id
        or fingerprint(agent)!=run.profile_fingerprint
        or db.query(RoomEvent).filter(RoomEvent.room_id==run.room_id,RoomEvent.seq>root.seq,RoomEvent.actor_type=='user',RoomEvent.kind=='message').first()):return None
    return cfg

def reconcile_startup(db):
    from app.models.collaboration import RoomAgentRun
    for out in db.query(SlackOutbox).filter_by(state='sending').all():out.state='unknown';out.updated_at=now()
    for run in db.query(SlackRun).filter_by(state='running').all():
        run.state='interrupted';run.failure_code='interrupted';run.completed_at=now()
        inbox=db.get(SlackInbox,(run.owner_id,run.workspace_id,run.event_id))
        if inbox:inbox.state='interrupted'
        member=db.get(RoomAgentRun,(run.room_id,run.agent_id))
        if member and member.generation==run.generation and member.trigger_event_id==run.root_event_id:
            member.state='idle';member.generation+=1;member.active_tool={};pending=dict(member.pending_task or {});pending.pop('slack_run',None);member.pending_task=pending
    db.commit()

def mark_ack(db,owner,event):
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        row=db.get(SlackInbox,(owner,event['workspace_id'],event['event_id']))
        if not row or row.digest!=envelope_digest(event):fail(409,'slack_event_conflict')
        if row.state=='received':row.state='queued'
        db.commit()
    except BaseException:db.rollback();raise

def prepare_reply(db,owner,epoch):
    from uuid import uuid4
    from app.models.collaboration import RoomEvent
    from app.models.slack import SlackThread
    from app.models.models import Conversation
    from app.services.bot_configuration_binding import fingerprint
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        cfg=db.get(SlackConfig,owner)
        if not cfg or cfg.state!='configured' or not cfg.enabled:db.commit();return None
        runs=db.query(SlackRun).filter_by(owner_id=owner,state='completed').order_by(SlackRun.created_at).all()
        for run in runs:
            if db.get(SlackOutbox,run.run_id):continue
            inbox=db.get(SlackInbox,(owner,run.workspace_id,run.event_id));result=db.get(RoomEvent,run.result_event_id) if run.result_event_id else None
            if (not inbox or not result or result.kind!='message' or result.actor_type!='agent' or result.actor_id!=run.agent_id
                or result.room_id!=run.room_id or result.payload.get('trigger_event_id')!=run.root_event_id
                or not isinstance(result.payload.get('content'),str) or not result.payload['content'].strip()):continue
            attempt=str(uuid4());valid=(run.owner_epoch==epoch and cfg.revision==run.config_revision
                and cfg.credential_revision==run.credential_revision and cfg.workspace_id==inbox.workspace_id and cfg.app_id==inbox.app_id
                and cfg.channel_id==inbox.channel_id and cfg.agent_id==run.agent_id
                and cfg.connection_id==run.connection_id and cfg.model_id==run.model_id)
            try:
                agent=validate_target(db,owner,run.agent_id,cfg.project_id,run.connection_id,run.model_id)
                thread=db.get(SlackThread,(owner,inbox.workspace_id,inbox.channel_id,inbox.thread_ts))
                room=db.get(Conversation,run.room_id)
                valid=bool(valid and inbox.owner_epoch==epoch and inbox.user_id in cfg.sender_ids
                    and thread and thread.room_id==run.room_id and thread.agent_id==run.agent_id and thread.project_id==cfg.project_id
                    and room and room.is_active and room.user_id==owner and room.project_id==cfg.project_id
                    and fingerprint(agent)==run.profile_fingerprint)
            except HTTPException:valid=False
            too_large=len(result.payload['content'].encode('utf-8'))>32768
            out=SlackOutbox(run_id=run.run_id,attempt_id=attempt,state=('blocked' if not valid else 'failed_before_dispatch' if too_large else 'sending'),updated_at=now());db.add(out)
            if not valid or too_large:db.commit();return None
            data={'event_id':inbox.event_id,'attempt_id':attempt,'workspace_id':inbox.workspace_id,'app_id':inbox.app_id,'user_id':inbox.user_id,'channel_id':inbox.channel_id,'thread_ts':inbox.thread_ts,
                'text':result.payload['content'],'config_revision':cfg.revision,'credential_revision':cfg.credential_revision}
            db.commit();return run.run_id,data
        db.commit();return None
    except BaseException:db.rollback();raise

def finish_reply(db,run_id,attempt,state,remote_ts=None):
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        row=db.get(SlackOutbox,run_id)
        if row and row.attempt_id==attempt and row.state=='sending':
            row.state=state if state in ('sent','failed_before_dispatch','unknown') else 'unknown'
            row.remote_ts=remote_ts if row.state=='sent' and isinstance(remote_ts,str) and len(remote_ts)<=64 else None
            row.updated_at=now()
        db.commit()
    except BaseException:db.rollback();raise

async def loop(app):
    import asyncio
    scheduler=app.state.scheduler
    cursors={}
    while True:
        try:
            bridge=getattr(app.state,'mcp_client',None);epochs=getattr(app.state,'owner_epoch_provider',None)
            if bridge is not None and bridge.available and epochs is not None:
                with scheduler.sessions() as db:
                    owners=[r.owner_id for r in db.query(SlackConfig).filter_by(state='configured',enabled=True).all()]
                for owner in owners:
                    async with app.state.slack_locks.setdefault(owner,asyncio.Lock()):
                        epoch=epochs.current(owner)
                        if not isinstance(epoch,str) or not re.fullmatch(r'[a-f0-9]{64}',epoch):continue
                        with scheduler.sessions() as db:
                            cfg=db.get(SlackConfig,owner)
                            if not cfg or cfg.state!='configured' or not cfg.enabled:continue
                            binding={'version':1,'owner_id':owner,'owner_epoch':epoch,'core_epoch':bridge.core_epoch,
                                'config_revision':cfg.revision,'credential_revision':cfg.credential_revision}
                        response=await asyncio.wait_for(bridge.slack_poll(binding),5)
                        if not isinstance(response,dict) or response.get('state')!='completed' or not isinstance(response.get('result'),dict):continue
                        if epochs.current(owner)!=epoch:continue
                        event=response['result'].get('event')
                        if event is not None:
                            with scheduler.sessions() as db:accept(db,owner,epoch,event)
                            ack=await asyncio.wait_for(bridge.slack_ack({**binding,'delivery_id':event['delivery_id'],'event_id':event['event_id']}),5)
                            if isinstance(ack,dict) and ack.get('state')=='completed' and epochs.current(owner)==epoch:
                                with scheduler.sessions() as db:mark_ack(db,owner,event)
                        with scheduler.sessions() as db:run_id=claim(db,owner,epoch,cursors)
                        if run_id:
                            from app.services.owner_routines import execute
                            task=asyncio.create_task(execute(scheduler,run_id,slack=True));scheduler._scheduled_runs.add(task);task.add_done_callback(scheduler._scheduled_runs.discard)
                        with scheduler.sessions() as db:prepared=prepare_reply(db,owner,epoch)
                        if prepared:
                            run_id,data=prepared
                            reply_binding={**binding,**data}
                            state='unknown';remote=None
                            try:
                                response=await asyncio.wait_for(bridge.slack_reply(reply_binding),45)
                                result=response.get('result') if isinstance(response,dict) and response.get('state')=='completed' else None
                                if isinstance(result,dict):state=result.get('state','unknown');remote=result.get('message_ts')
                            except Exception:pass
                            finally:
                                with scheduler.sessions() as db:finish_reply(db,run_id,data['attempt_id'],state,remote)
        except asyncio.CancelledError:raise
        except Exception:pass
        await asyncio.sleep(1)
