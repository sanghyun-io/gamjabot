"""Local records for an externally verified team; execution stays unavailable.

The adapter is deliberately not configured by this module. Its authenticated
claims must come from a separately trusted team authority, never request JSON.
"""
from datetime import datetime, timezone, timedelta
from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import text
from app.models.models import Agent, Conversation, Project, User, Message
from app.models.collaboration import ProviderConnection, RoomMembership, RoomEvent, RoomDraft, RoomAgentRun


def fail(code, status=409): raise HTTPException(status, {'code':'shared_bot_'+code})

def stamp(): return datetime.now(timezone.utc).isoformat()

def ident(value):
    try:
        if not isinstance(value, str) or not value or len(value.encode('utf-8')) > 512 or not value.isprintable():
            fail('identity_invalid', 422)
    except UnicodeError: fail('identity_invalid',422)
    return value

def uid(value):
    try:
        if not isinstance(value,str) or str(UUID(value)) != value: raise ValueError()
    except (ValueError,TypeError): fail('id_invalid',422)
    return value

def principal(request, owner):
    epochs = getattr(request.app.state, 'owner_epoch_provider', None)
    epoch = epochs.current(owner.id) if epochs else None
    adapter = getattr(request.app.state, 'team_authority', None)
    if (not epoch or adapter is None or not callable(getattr(adapter, 'authenticated_claims', None))
            or not callable(getattr(adapter, 'is_current', None))):
        fail('authority_unavailable',503)
    # The configured adapter validates issuer, signature, team membership,
    # expiry, and the local Core Owner/epoch binding. There is no fallback.
    try: claims = adapter.authenticated_claims(request, owner.id, epoch)
    except Exception: fail('authority_unavailable',503)
    if (not isinstance(claims,dict) or set(claims) != {'verified','owner_id','owner_epoch','issuer','subject','team_subject','team_role','expires_at','authority_revision'}
            or claims['verified'] is not True or type(claims['owner_id']) is not int or claims['owner_id'] != owner.id
            or claims['owner_epoch'] != epoch or claims['team_role'] not in ('owner','admin','member')):
        fail('identity_unverified',403)
    claims = dict(claims)
    for key in ('issuer','subject','team_subject','authority_revision'): ident(claims[key])
    try: expires=datetime.fromisoformat(claims['expires_at'].replace('Z','+00:00'))
    except (ValueError,TypeError,AttributeError): fail('identity_unverified',403)
    if expires.tzinfo is None: fail('identity_unverified',403)
    def current():
        if expires <= datetime.now(timezone.utc) or epochs.current(owner.id) != epoch: fail('owner_changed',409)
        try: valid=adapter.is_current(request,owner.id,epoch,claims)
        except Exception: fail('authority_unavailable',503)
        if valid is not True: fail('identity_unverified',403)
    current()
    return {**claims,'_guard':current}

def guard(claims):
    check=claims.get('_guard')
    if not callable(check): fail('identity_unverified',403)
    check()

def begin(db):
    db.rollback(); db.execute(text('BEGIN IMMEDIATE'))

def realm(db, team_id, claims, *, manager=False):
    row=db.execute(text('SELECT * FROM team_realms WHERE id=:id'),{'id':uid(team_id)}).mappings().first()
    if not row or (row['issuer'],row['team_subject']) != (claims['issuer'],claims['team_subject']): fail('team_not_found',404)
    member=db.execute(text('SELECT * FROM team_people WHERE team_id=:team AND issuer=:issuer AND subject=:subject'),
        {'team':team_id,'issuer':claims['issuer'],'subject':claims['subject']}).mappings().first()
    if not member or member['state']!='active': fail('team_not_found',404)
    if manager and (member['role'] not in ('owner','manager') or claims['team_role'] not in ('owner','admin')):
        fail('team_forbidden',403)
    return row,member

def create_team(db, owner, claims, value):
    if claims['team_role']!='owner': fail('team_forbidden',403)
    team=uid(value.team_id);begin(db)
    try:
        prior=db.execute(text('SELECT * FROM team_realms WHERE id=:id OR (issuer=:i AND team_subject=:s)'),
                      {'id':team,'i':claims['issuer'],'s':claims['team_subject']}).mappings().first()
        if prior:
            if (prior['id'],prior['issuer'],prior['team_subject'],prior['owner_id'],prior['owner_subject']) != (team,claims['issuer'],claims['team_subject'],owner.id,claims['subject']): fail('team_exists')
            db.rollback();return {'team_id':team,'revision':prior['revision'],'replayed':True,'execution_available':False,'reason':'team_transport_unavailable'}
        db.execute(text('INSERT INTO team_realms VALUES (:id,:i,:t,:o,:s,1,0,:at)'),
                   {'id':team,'i':claims['issuer'],'t':claims['team_subject'],'o':owner.id,'s':claims['subject'],'at':stamp()})
        db.execute(text("INSERT INTO team_people VALUES (:id,:i,:s,'owner','active',1)"),
                   {'id':team,'i':claims['issuer'],'s':claims['subject']})
        guard(claims);db.commit();return {'team_id':team,'revision':1,'execution_available':False,'reason':'team_transport_unavailable'}
    except BaseException: db.rollback();raise

def invite(db, claims, team_id, value):
    invite_id=uid(value.invite_id);issuer=ident(value.subject.issuer);subject=ident(value.subject.subject)
    try: expiry=datetime.fromisoformat(value.expires_at.replace('Z','+00:00'))
    except ValueError: fail('expiry_invalid',422)
    now=datetime.now(timezone.utc)
    if expiry.tzinfo is None: fail('expiry_invalid',422)
    begin(db)
    try:
        team,_=realm(db,team_id,claims,manager=True)
        if issuer!=team['issuer'] or subject==claims['subject']: fail('invite_invalid',422)
        existing=db.execute(text('SELECT * FROM team_invites WHERE id=:id'),{'id':invite_id}).mappings().first()
        if existing:
            if (existing['team_id'],existing['issuer'],existing['subject'],existing['role'],existing['expires_at']) != (team_id,issuer,subject,value.role,expiry.astimezone(timezone.utc).isoformat()): fail('invite_exists')
            state='expired' if existing['state']=='pending' and datetime.fromisoformat(existing['expires_at'])<=now else existing['state']
            db.rollback();return {'invite_id':invite_id,'state':state,'delivery':'not_sent','team_revision':team['revision'],'replayed':True}
        if not now < expiry <= now+timedelta(days=30): fail('expiry_invalid',422)
        if team['revision']!=value.expected_revision: fail('team_changed')
        person=db.execute(text("SELECT 1 FROM team_people WHERE team_id=:t AND issuer=:i AND subject=:s AND state='active'"),{'t':team_id,'i':issuer,'s':subject}).first()
        if person: fail('already_member')
        db.execute(text('INSERT INTO team_invites VALUES (:id,:t,:i,:s,:r,\'pending\',:exp,:at)'),
            {'id':invite_id,'t':team_id,'i':issuer,'s':subject,'r':value.role,'exp':expiry.astimezone(timezone.utc).isoformat(),'at':stamp()})
        db.execute(text('UPDATE team_realms SET revision=revision+1 WHERE id=:id'),{'id':team_id});guard(claims);db.commit()
        return {'invite_id':invite_id,'state':'pending','delivery':'not_sent','team_revision':value.expected_revision+1}
    except BaseException: db.rollback();raise

def accept(db, claims, team_id, invite_id):
    uid(team_id);uid(invite_id);begin(db)
    try:
        team=db.execute(text('SELECT * FROM team_realms WHERE id=:id'),{'id':team_id}).mappings().first()
        row=db.execute(text('SELECT * FROM team_invites WHERE id=:id AND team_id=:t'),{'id':invite_id,'t':team_id}).mappings().first()
        if (not team or not row or (team['issuer'],team['team_subject']) != (claims['issuer'],claims['team_subject'])
                or (row['issuer'],row['subject']) != (claims['issuer'],claims['subject'])): fail('invite_not_found',404)
        if row['state']=='accepted':
            member=db.execute(text("SELECT 1 FROM team_people WHERE team_id=:t AND issuer=:i AND subject=:s AND state='active'"),
                {'t':team_id,'i':claims['issuer'],'s':claims['subject']}).first()
            if not member: fail('invite_unavailable')
            db.rollback();return {'invite_id':invite_id,'state':'accepted','execution_available':False}
        if row['state']!='pending' or datetime.fromisoformat(row['expires_at'])<=datetime.now(timezone.utc): fail('invite_unavailable')
        existing=db.execute(text('SELECT state FROM team_people WHERE team_id=:t AND issuer=:i AND subject=:s'),
            {'t':team_id,'i':claims['issuer'],'s':claims['subject']}).first()
        if existing and existing[0]=='active': fail('already_member')
        if existing:
            db.execute(text("UPDATE team_people SET state='active',role=:r,revision=revision+1 WHERE team_id=:t AND issuer=:i AND subject=:s AND state='revoked'"),
                {'t':team_id,'i':claims['issuer'],'s':claims['subject'],'r':row['role']})
        else:
            db.execute(text("INSERT INTO team_people VALUES (:t,:i,:s,:r,'active',1)"),
                {'t':team_id,'i':claims['issuer'],'s':claims['subject'],'r':row['role']})
        db.execute(text("UPDATE team_invites SET state='accepted' WHERE id=:id AND state='pending'"),{'id':invite_id})
        db.execute(text('UPDATE team_realms SET revision=revision+1 WHERE id=:id'),{'id':team_id})
        if datetime.fromisoformat(row['expires_at'])<=datetime.now(timezone.utc): fail('invite_unavailable')
        guard(claims);db.commit()
        return {'invite_id':invite_id,'state':'accepted','execution_available':False}
    except BaseException: db.rollback();raise

def revoke_invite(db, claims, team_id, invite_id, expected_revision):
    uid(invite_id);begin(db)
    try:
        team,_=realm(db,team_id,claims,manager=True)
        if team['revision']!=expected_revision: fail('team_changed')
        count=db.execute(text("UPDATE team_invites SET state='revoked' WHERE id=:id AND team_id=:team AND state='pending'"),{'id':invite_id,'team':team_id}).rowcount
        if count!=1: fail('invite_unavailable')
        db.execute(text('UPDATE team_realms SET revision=revision+1,revocation_epoch=revocation_epoch+1 WHERE id=:id'),{'id':team_id});guard(claims);db.commit()
        return {'invite_id':invite_id,'state':'revoked','team_revision':expected_revision+1}
    except BaseException: db.rollback();raise

def revoke_member(db, claims, team_id, issuer, subject, expected_revision):
    issuer=ident(issuer);subject=ident(subject);begin(db)
    try:
        team,_=realm(db,team_id,claims,manager=True)
        if team['revision']!=expected_revision: fail('team_changed')
        if (issuer,subject)==(team['issuer'],team['owner_subject']): fail('owner_cannot_be_removed')
        count=db.execute(text("UPDATE team_people SET state='revoked',revision=revision+1 WHERE team_id=:t AND issuer=:i AND subject=:s AND state='active'"),
            {'t':team_id,'i':issuer,'s':subject}).rowcount
        if count!=1: fail('member_not_found',404)
        # Re-admission never restores an old selected-Bot grant silently.
        db.execute(text("UPDATE shared_bot_publications SET revision=revision+1,revocation_epoch=revocation_epoch+1,updated_at=:at WHERE team_id=:t AND id IN (SELECT share_id FROM shared_bot_people WHERE issuer=:i AND subject=:s)"),
            {'at':stamp(),'t':team_id,'i':issuer,'s':subject})
        db.execute(text('DELETE FROM shared_bot_people WHERE issuer=:i AND subject=:s AND share_id IN (SELECT id FROM shared_bot_publications WHERE team_id=:t)'),{'t':team_id,'i':issuer,'s':subject})
        db.execute(text("UPDATE team_invites SET state='revoked' WHERE team_id=:t AND issuer=:i AND subject=:s AND state='pending'"),{'t':team_id,'i':issuer,'s':subject})
        db.execute(text('UPDATE team_realms SET revision=revision+1,revocation_epoch=revocation_epoch+1 WHERE id=:id'),{'id':team_id});guard(claims);db.commit()
        return {'state':'revoked','team_revision':expected_revision+1,'execution_available':False}
    except BaseException: db.rollback();raise

def publication(db, owner, claims, team_id, value):
    share_id=uid(value.share_id)
    if len(value.role_text.encode('utf-8'))>32000 or len(value.knowledge_text.encode('utf-8'))>128000 or not value.role_text.strip(): fail('content_invalid',422)
    people=[(ident(p.issuer),ident(p.subject),p.role) for p in value.people]
    if len({(i,s) for i,s,_ in people})!=len(people): fail('audience_invalid',422)
    if value.audience=='team' and any(role=='collaborator' for _,_,role in people): fail('audience_invalid',422)
    begin(db)
    try:
        team,_=realm(db,team_id,claims)
        prior=db.execute(text('SELECT * FROM shared_bot_publications WHERE id=:id'),{'id':share_id}).mappings().first()
        if prior:
            if prior['team_id']!=team_id or prior['agent_id']!=value.agent_id: fail('share_id_conflict')
            is_owner=(prior['owner_id']==owner.id and (prior['owner_issuer'],prior['owner_subject'])==(claims['issuer'],claims['subject']))
            manager=db.execute(text("SELECT 1 FROM shared_bot_people WHERE share_id=:id AND issuer=:i AND subject=:s AND role='manager'"),
                {'id':share_id,'i':claims['issuer'],'s':claims['subject']}).first()
            if not is_owner and not manager: fail('share_forbidden',403)
            if prior['state']=='revoked' and not is_owner: fail('owner_required',403)
            agent=db.get(Agent,prior['agent_id'])
            if not agent or not agent.is_active or agent.user_id!=prior['owner_id']: fail('bot_not_found',404)
            if prior['revision']!=value.expected_revision:
                prior_people=db.execute(text('SELECT issuer,subject,role FROM shared_bot_people WHERE share_id=:id'),{'id':share_id}).all()
                replay=(prior['state']=='prepared' and prior['revision']==value.expected_revision+1
                    and prior['role_text']==value.role_text and prior['knowledge_text']==value.knowledge_text
                    and prior['audience']==value.audience and set(prior_people)==set(people))
                if not replay: fail('share_changed')
                db.rollback();return {'share_id':share_id,'revision':prior['revision'],'revocation_epoch':prior['revocation_epoch'],
                    'state':'prepared','replayed':True,'execution_available':False,'reason':'team_transport_unavailable'}
        else:
            agent=db.get(Agent,value.agent_id)
            if not agent or agent.user_id!=owner.id or not agent.is_active: fail('bot_not_found',404)
            if value.expected_revision!=0: fail('share_changed')
            if db.execute(text('SELECT 1 FROM shared_bot_publications WHERE team_id=:t AND agent_id=:a'),{'t':team_id,'a':value.agent_id}).first(): fail('share_exists')
        for issuer,subject,_ in people:
            person=db.execute(text("SELECT 1 FROM team_people WHERE team_id=:t AND issuer=:i AND subject=:s AND state='active'"),{'t':team_id,'i':issuer,'s':subject}).first()
            if not person: fail('recipient_unavailable')
        if prior:
            revision=prior['revision']+1;epoch=prior['revocation_epoch']+1
            db.execute(text("UPDATE shared_bot_publications SET role_text=:r,knowledge_text=:k,audience=:a,revision=:rev,revocation_epoch=:e,state='prepared',updated_at=:at WHERE id=:id"),
                {'r':value.role_text,'k':value.knowledge_text,'a':value.audience,'rev':revision,'e':epoch,'at':stamp(),'id':share_id})
            db.execute(text('DELETE FROM shared_bot_people WHERE share_id=:id'),{'id':share_id})
        else:
            revision=1;epoch=0
            db.execute(text("INSERT INTO shared_bot_publications VALUES (:id,:t,:o,:oi,:os,:agent,:r,:k,:a,1,0,'prepared',:at)"),
                {'id':share_id,'t':team_id,'o':owner.id,'oi':claims['issuer'],'os':claims['subject'],'agent':value.agent_id,'r':value.role_text,'k':value.knowledge_text,'a':value.audience,'at':stamp()})
        for issuer,subject,role in people:
            db.execute(text('INSERT INTO shared_bot_people VALUES (:id,:i,:s,:r)'),{'id':share_id,'i':issuer,'s':subject,'r':role})
        guard(claims);db.commit();return {'share_id':share_id,'revision':revision,'revocation_epoch':epoch,'state':'prepared',
            'execution_available':False,'reason':'team_transport_unavailable'}
    except BaseException: db.rollback();raise

def revoke_share(db, owner, claims, team_id, share_id, expected_revision):
    uid(share_id);begin(db)
    try:
        team,_=realm(db,team_id,claims)
        row=db.execute(text('SELECT * FROM shared_bot_publications WHERE id=:id AND team_id=:t'),{'id':share_id,'t':team_id}).mappings().first()
        if not row: fail('share_not_found',404)
        is_owner=(row['owner_id']==owner.id and (row['owner_issuer'],row['owner_subject'])==(claims['issuer'],claims['subject']))
        manager=db.execute(text("SELECT 1 FROM shared_bot_people WHERE share_id=:id AND issuer=:i AND subject=:s AND role='manager'"),{'id':share_id,'i':claims['issuer'],'s':claims['subject']}).first()
        if not is_owner and not manager: fail('share_forbidden',403)
        if row['state']=='revoked' and row['revision']==expected_revision+1:
            db.rollback();return {'share_id':share_id,'state':'revoked','revision':row['revision'],'replayed':True,'execution_available':False}
        if row['revision']!=expected_revision: fail('share_changed')
        if row['state']!='prepared': fail('share_unavailable')
        db.execute(text("UPDATE shared_bot_publications SET state='revoked',revision=revision+1,revocation_epoch=revocation_epoch+1,updated_at=:at WHERE id=:id"),{'id':share_id,'at':stamp()})
        guard(claims);db.commit();return {'share_id':share_id,'state':'revoked','revision':expected_revision+1,'execution_available':False}
    except BaseException: db.rollback();raise

def prepare_room(db, owner, claims, team_id, share_id, value):
    uid(share_id);begin(db)
    try:
        team,member=realm(db,team_id,claims)
        share=db.execute(text('SELECT * FROM shared_bot_publications WHERE id=:id AND team_id=:t'),{'id':share_id,'t':team_id}).mappings().first()
        if not share or share['state']!='prepared': fail('share_not_found',404)
        allowed=(share['audience']=='team' or (share['owner_id']==owner.id and (share['owner_issuer'],share['owner_subject'])==(claims['issuer'],claims['subject'])) or
            db.execute(text('SELECT 1 FROM shared_bot_people WHERE share_id=:id AND issuer=:i AND subject=:s'),{'id':share_id,'i':claims['issuer'],'s':claims['subject']}).first())
        if not allowed: fail('share_not_found',404)
        if share['revision']!=value.expected_share_revision or share['revocation_epoch']!=value.expected_revocation_epoch: fail('share_changed')
        room=db.get(Conversation,value.room_id);project=db.get(Project,room.project_id) if room else None
        conn=db.get(ProviderConnection,value.connection_id)
        if (not room or room.user_id!=owner.id or not room.is_active or not project or project.user_id!=owner.id or not project.is_active
                or not conn or conn.user_id!=owner.id or not conn.enabled): fail('recipient_context_unavailable',404)
        existing=db.execute(text('SELECT * FROM shared_bot_rooms WHERE share_id=:id AND issuer=:i AND subject=:s'),{'id':share_id,'i':claims['issuer'],'s':claims['subject']}).mappings().first()
        if existing:
            same=(existing['recipient_id']==owner.id and existing['room_id']==room.id and existing['connection_id']==conn.id
                and existing['model_id']==value.model_id and existing['share_revision']==share['revision']
                and existing['revocation_epoch']==share['revocation_epoch'] and existing['team_revocation_epoch']==team['revocation_epoch'])
            if not same: fail('room_binding_exists')
            db.rollback();return {'share_id':share_id,'room_id':room.id,'state':'prepared','replayed':True,'execution_available':False,'reason':'team_transport_unavailable'}
        # A prepared binding cannot adopt or disclose an existing private chat.
        if (db.query(RoomMembership).filter_by(room_id=room.id).first()
                or db.query(RoomEvent).filter_by(room_id=room.id).first()
                or db.query(RoomAgentRun).filter_by(room_id=room.id).first()
                or db.query(RoomDraft).filter_by(room_id=room.id).first()
                or db.query(Message).filter_by(conversation_id=room.id).first()):
            fail('recipient_room_not_empty')
        if db.execute(text('SELECT 1 FROM shared_bot_rooms WHERE room_id=:r'),{'r':room.id}).first(): fail('room_binding_exists')
        db.execute(text('''INSERT INTO shared_bot_rooms VALUES (:id,:i,:s,:owner,:room,:conn,:model,:rev,:epoch,:team_epoch,:at)'''),
            {'id':share_id,'i':claims['issuer'],'s':claims['subject'],'owner':owner.id,'room':room.id,'conn':conn.id,
             'model':value.model_id,'rev':share['revision'],'epoch':share['revocation_epoch'],'team_epoch':team['revocation_epoch'],'at':stamp()})
        guard(claims);db.commit();return {'share_id':share_id,'room_id':room.id,'state':'prepared','execution_available':False,'reason':'team_transport_unavailable'}
    except BaseException: db.rollback();raise

def read_team(db, claims, team_id):
    team,member=realm(db,team_id,claims)
    rows=db.execute(text('SELECT issuer,subject,role,state,revision FROM team_people WHERE team_id=:id ORDER BY issuer,subject LIMIT 101'),{'id':team_id}).mappings().all()
    return {'team_id':team_id,'revision':team['revision'],'revocation_epoch':team['revocation_epoch'],
            'your_role':member['role'],'members':[dict(row) for row in rows[:100]],'truncated':len(rows)>100,
            'execution_available':False,'reason':'team_transport_unavailable'}

def read_publication(db, owner, claims, team_id, share_id):
    uid(share_id);_,member=realm(db,team_id,claims)
    share=db.execute(text('SELECT * FROM shared_bot_publications WHERE id=:id AND team_id=:t'),{'id':share_id,'t':team_id}).mappings().first()
    if not share or share['state']!='prepared': fail('share_not_found',404)
    is_owner=(share['owner_id']==owner.id and (share['owner_issuer'],share['owner_subject'])==(claims['issuer'],claims['subject']))
    access=db.execute(text('SELECT role FROM shared_bot_people WHERE share_id=:id AND issuer=:i AND subject=:s'),
        {'id':share_id,'i':claims['issuer'],'s':claims['subject']}).first()
    if not (is_owner or access or share['audience']=='team'): fail('share_not_found',404)
    return {'share_id':share_id,'team_id':team_id,'agent_id':share['agent_id'],'role_text':share['role_text'],
        'knowledge_text':share['knowledge_text'],'audience':share['audience'],'revision':share['revision'],
        'revocation_epoch':share['revocation_epoch'],'your_role':'owner' if is_owner else access[0] if access else 'collaborator',
        'state':share['state'],'execution_available':False,'reason':'team_transport_unavailable'}

def inspect_room(db, owner, claims, team_id, share_id):
    uid(share_id);team,_=realm(db,team_id,claims)
    row=db.execute(text('SELECT * FROM shared_bot_rooms WHERE share_id=:id AND issuer=:i AND subject=:s AND recipient_id=:o'),
        {'id':share_id,'i':claims['issuer'],'s':claims['subject'],'o':owner.id}).mappings().first()
    if not row: fail('room_binding_not_found',404)
    share=db.execute(text('SELECT * FROM shared_bot_publications WHERE id=:id AND team_id=:t'),{'id':share_id,'t':team_id}).mappings().first()
    if (not share or share['state']!='prepared' or share['revision']!=row['share_revision']
            or share['revocation_epoch']!=row['revocation_epoch'] or team['revocation_epoch']!=row['team_revocation_epoch']):
        fail('room_binding_stale')
    access=db.execute(text('SELECT 1 FROM shared_bot_people WHERE share_id=:id AND issuer=:i AND subject=:s'),
        {'id':share_id,'i':claims['issuer'],'s':claims['subject']}).first()
    is_owner=(share['owner_id']==owner.id and (share['owner_issuer'],share['owner_subject'])==(claims['issuer'],claims['subject']))
    if not (is_owner or share['audience']=='team' or access): fail('room_binding_stale')
    room=db.get(Conversation,row['room_id']);project=db.get(Project,room.project_id) if room else None
    conn=db.get(ProviderConnection,row['connection_id'])
    if (not room or room.user_id!=owner.id or not room.is_active or not project or project.user_id!=owner.id or not project.is_active
            or not conn or conn.user_id!=owner.id or not conn.enabled): fail('recipient_context_unavailable')
    return {'share_id':share_id,'room_id':room.id,'connection_id':conn.id,'model_id':row['model_id'],
        'share_revision':row['share_revision'],'revocation_epoch':row['revocation_epoch'],
        'team_revocation_epoch':row['team_revocation_epoch'],'state':'prepared',
        'execution_available':False,'reason':'team_transport_unavailable'}

def release_room(db, owner, claims, team_id, share_id):
    uid(share_id);begin(db)
    try:
        realm(db,team_id,claims)
        count=db.execute(text('DELETE FROM shared_bot_rooms WHERE share_id=:id AND issuer=:i AND subject=:s AND recipient_id=:o AND EXISTS (SELECT 1 FROM shared_bot_publications WHERE id=:id AND team_id=:t)'),
            {'id':share_id,'i':claims['issuer'],'s':claims['subject'],'o':owner.id,'t':team_id}).rowcount
        if count!=1: fail('room_binding_not_found',404)
        guard(claims);db.commit();return {'share_id':share_id,'state':'released','execution_available':False}
    except BaseException: db.rollback();raise
