"""Synthetic preflight and ledger coverage for an unstarted tool review change."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.collaboration import BroadMode, RoomEvent, RoomGrant, RoomTrigger, ToolAction
from app.models.teammate import TeammateHandoff
from app.services.action_review import ReviewChanged
from app.services.request_retry import build_retry_preflight, execute_retry, tool_retry_requires_once
from test_request_retry import retry_client  # Reuse its isolated SQLite fixture; no live services.


@pytest.fixture
def tool_source(retry_client):
    client, ids = retry_client
    action_id, review_id, session_id = (str(uuid4()) for _ in range(3))
    with client.app.state.session_factory() as db:
        source = db.get(RoomEvent, ids['failure'])
        observation = {'call_id':'synthetic-call', 'state':'denied', 'text':'Review changed',
                       'screenshot_ref':None, 'error_code':'review_changed'}
        fields = {'action_id':action_id, 'room_id':ids['room'], 'agent_id':ids['target'],
                  'trigger_event_id':ids['request'], 'call_id':'synthetic-call', 'workflow_id':None,
                  'session_id':session_id, 'family':'browser_inspect', 'target':'https://example.invalid/',
                  'scope_target':'https://example.invalid', 'expected_effect':'Inspect page', 'payload_digest':'a'*64}
        proposer = {'owner_id':ids['owner'], 'room_id':ids['room'], 'agent_id':ids['target'],
                    'connection_id':ids['connection'], 'model_id':'exact', 'config_revision':1,
                    'auth_grant_revision':1, 'provider_id':None, 'protocol_id':None, 'owner_epoch':None}
        authority = {'owner_id':ids['owner'], 'room_id':ids['room'], 'agent_id':ids['target'],
                     'session_id':session_id, 'os_user_status':'unsupported', 'euid':None, 'proposer':proposer}
        target = {'room_id':ids['room'], 'agent_id':ids['target'], 'session_id':session_id,
                  'workflow_id':None, 'role':'browser_context', 'status':'absent', 'context_id':None,
                  'profile':'temporary_workflow', 'profile_id':None, 'profile_revision':None, 'profile_label':None}
        snapshot = {'version':1, 'captured_at':'2026-10-05T00:00:00Z', 'action':fields,
                    'authority':authority, 'targets':[target],
                    'external_context':{'account':'unsupported','target_version':'unsupported'},
                    'checks':{'target':'runtime_observed','content_CAS':'unsupported',
                              'rule_version':1,'owner_authentication':'unsupported'}}
        action = ToolAction(**fields, arguments={'url':'https://example.invalid/'}, state='denied',
                            review_id=review_id, review_snapshot=snapshot, observation=observation,
                            started_at=None, completed_at=datetime(2026,10,5), approved_at=datetime(2026,10,5),
                            approval_owner_id=ids['owner'])
        db.add(action)
        source.actor_type='tool'; source.actor_id=None; source.kind='tool_result'
        source.idempotency_key='tool-result:'+action_id
        source.payload={**observation,'agent_id':ids['target'],'action_id':action_id,
                        'trigger_event_id':ids['request'],'review_id':review_id}
        db.add(RoomEvent(event_id=str(uuid4()),room_id=ids['room'],seq=3,actor_type='agent',
                         actor_id=ids['target'],actor_name_snapshot='Target',kind='message',
                         payload={'content':'The action could not start.','trigger_event_id':ids['request'],'round':1},
                         created_at=datetime(2026,10,5)))
        db.commit()
    return client, ids, action_id


def plan(db, ids):
    return build_retry_preflight(db, ids['owner'], ids['room'], ids['failure'])


def test_unstarted_result_creates_new_owner_root_once_and_preserves_old_events(tool_source):
    client, ids, action_id = tool_source
    with client.app.state.session_factory() as db:
        preview = plan(db, ids)
        assert preview['eligible'] is True
        result = execute_retry(db, ids['owner'], ids['room'], ids['failure'], preview['plan_hash'], 'synthetic-key')
        replay = execute_retry(db, ids['owner'], ids['room'], ids['failure'], preview['plan_hash'], 'synthetic-key')
        assert result['retry_event_id'] != ids['request'] and replay['retry_event_id'] == result['retry_event_id']
        assert replay['replayed'] is True
        retry = db.get(RoomEvent, result['retry_event_id'])
        assert (retry.actor_type,retry.actor_id,retry.payload['retry_of_failure_event_id']) == ('user',ids['owner'],ids['failure'])
        assert db.get(RoomTrigger,retry.event_id).selected_agents == [ids['target']]
        assert db.get(ToolAction,action_id).state == 'denied'
        assert db.get(RoomEvent,ids['failure']).kind == 'tool_result'
        assert not db.query(ToolAction).filter_by(trigger_event_id=retry.event_id).count()


@pytest.mark.parametrize('change,reason', [
    ('identity','source_invalid'), ('review','source_invalid'), ('started','source_invalid'),
    ('foreign','source_invalid'), ('delegated','source_invalid'), ('file','context_unavailable'),
    ('attachment','context_unavailable'), ('nontext','context_unavailable'), ('handoff','context_unavailable'),
    ('uncertain','action_uncertain'), ('failed_after_start','action_uncertain'), ('effect','completed_effect'), ('unsettled','action_unsettled'),
    ('busy','room_busy'),
])
def test_source_authority_context_and_effects_fail_closed(tool_source, change, reason):
    client, ids, action_id = tool_source
    with client.app.state.session_factory() as db:
        source, action, request = db.get(RoomEvent,ids['failure']), db.get(ToolAction,action_id), db.get(RoomEvent,ids['request'])
        if change == 'identity': source.payload = {**source.payload,'agent_id':ids['peer']}
        elif change == 'review': source.payload = {**source.payload,'review_id':str(uuid4())}
        elif change == 'started': action.started_at = datetime(2026,10,5)
        elif change == 'foreign': action.approval_owner_id = ids['other']
        elif change == 'delegated': action.execution_binding = {'context_mode':'delegated'}
        elif change == 'file': request.payload = {**request.payload,'file_context':{'fixture':True}}
        elif change == 'attachment': request.payload = {**request.payload,'attachments':[{'fixture':True}]}
        elif change == 'nontext': request.payload = {**request.payload,'output_kind':'image_generation'}
        elif change == 'handoff':
            db.add(TeammateHandoff(handoff_id=str(uuid4()),owner_id=ids['owner'],room_id=ids['room'],
                root_request_event_id=ids['request'],sender_agent_id=ids['target'],recipient_agent_id=ids['peer'],
                sender_name='Target',recipient_name='Peer',content='synthetic task',delivery_key='synthetic',
                state='completed',reply_state='completed',depth=1,request_event_id=ids['request'],
                created_at=datetime(2026,10,5),updated_at=datetime(2026,10,5)))
        elif change in ('uncertain','effect','unsettled','failed_after_start'):
            db.add(ToolAction(action_id=str(uuid4()),room_id=ids['room'],agent_id=ids['target'],
                trigger_event_id=ids['request'],family='write_file',arguments={},
                state={'uncertain':'uncertain','effect':'completed','unsettled':'waiting','failed_after_start':'failed'}[change],
                started_at=datetime(2026,10,5) if change=='failed_after_start' else None))
        elif change == 'busy':
            event=RoomEvent(event_id=str(uuid4()),room_id=ids['room'],seq=4,actor_type='user',actor_id=ids['owner'],
                actor_name_snapshot='Owner',kind='message',payload={'content':'another request'},created_at=datetime(2026,10,5))
            db.add(event); db.flush()
            db.add(RoomTrigger(event_id=event.event_id,room_id=ids['room'],state='pending',selected_agents=[ids['target']],
                               max_rounds=1,max_tool_calls=1,max_executor_seconds=60,current_round=0))
        db.commit()
        assert plan(db, ids)['blocked_reason'] == reason


def test_changed_plan_is_rejected_before_creating_root(tool_source):
    client, ids, _ = tool_source
    with client.app.state.session_factory() as db:
        preview=plan(db,ids)
        request=db.get(RoomEvent,ids['request']); request.payload={**request.payload,'content':'changed synthetic request'}; db.commit()
        with pytest.raises(HTTPException) as error:
            execute_retry(db,ids['owner'],ids['room'],ids['failure'],preview['plan_hash'],'stale-key')
        assert error.value.status_code == 409 and error.value.detail['code'] == 'request_retry_plan_stale'


def test_fresh_review_once_rejects_existing_session_broad_and_old_grant(tool_source, monkeypatch):
    from app.services import grants
    client, ids, old_action_id = tool_source
    with client.app.state.session_factory() as db:
        old=db.get(ToolAction,old_action_id)
        preview=plan(db,ids)
        result=execute_retry(db,ids['owner'],ids['room'],ids['failure'],preview['plan_hash'],'once-key')
        session_id=str(uuid4()); fresh_id=str(uuid4()); fresh_review=str(uuid4())
        fresh=ToolAction(action_id=fresh_id,room_id=ids['room'],agent_id=ids['target'],
            trigger_event_id=result['retry_event_id'],family='browser_inspect',arguments={},
            state='waiting',session_id=session_id,review_id=fresh_review)
        old_session=RoomGrant(room_id=ids['room'],agent_id=ids['target'],decision='approved',
            scope={'kind':'room_session','session_id':session_id,'families':['browser_inspect'],
                   'targets':['https://example.invalid'],'approval_review_id':old.review_id})
        old_once=RoomGrant(room_id=ids['room'],agent_id=ids['target'],decision='approved',
            expires_at=datetime.utcnow()+timedelta(minutes=5),
            scope={'kind':'once','session_id':session_id,'families':['browser_inspect'],
                   'targets':['https://example.invalid'],'approval_review_id':old.review_id})
        db.add_all([fresh,old_session,old_once,BroadMode(owner_id=ids['owner'],enabled=True)]);db.commit()
        broker=SimpleNamespace(session_id=session_id,execution_review=lambda *_: object(),require_review=lambda *_,**__: object())
        monkeypatch.setattr(grants,'matches',lambda *_,**__: True)
        assert tool_retry_requires_once(db,fresh) is True
        assert grants.claim(db,fresh,session_id,broker=broker) is None
        with pytest.raises(HTTPException) as error:
            grants.decide(db,fresh_id,ids['room'],ids['owner'],'approve','room_session',session_id,
                          review_id=fresh_review,broker=broker)
        assert error.value.status_code == 409
        grant=RoomGrant(room_id=ids['room'],agent_id=ids['target'],decision='approved',
            expires_at=datetime.utcnow()+timedelta(minutes=5),
            scope={'kind':'once','session_id':session_id,'families':['browser_inspect'],
                   'targets':['https://example.invalid'],'approval_review_id':fresh_review})
        db.add(grant);db.flush()
        fresh.state='approved';fresh.approved_at=datetime.utcnow();fresh.approval_owner_id=ids['owner']
        fresh.approval_scope='once';fresh.grant_id=grant.id;db.commit()
        ref=grants.claim(db,fresh,session_id,grant_id=grant.id,broker=broker)
        assert ref is not None and ref.scope=='once' and ref.grant_id==grant.id and ref.review_id==fresh_review
        assert grant.id not in (old_session.id,old_once.id)


def test_changed_tool_source_cannot_reuse_retry_provenance(tool_source):
    client, ids, old_action_id=tool_source
    with client.app.state.session_factory() as db:
        preview=plan(db,ids)
        result=execute_retry(db,ids['owner'],ids['room'],ids['failure'],preview['plan_hash'],'proof-key')
        fresh=ToolAction(action_id=str(uuid4()),room_id=ids['room'],agent_id=ids['target'],
            trigger_event_id=result['retry_event_id'],family='browser_inspect',arguments={},state='waiting',
            session_id=str(uuid4()),review_id=str(uuid4()))
        assert tool_retry_requires_once(db,fresh) is True
        db.get(ToolAction,old_action_id).started_at=datetime(2026,10,5);db.commit()
        with pytest.raises(ReviewChanged): tool_retry_requires_once(db,fresh)
