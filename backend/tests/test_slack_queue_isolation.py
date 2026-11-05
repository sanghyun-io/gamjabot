"""Synthetic SQLite checks for Slack thread scheduling; no Slack calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException

from test_room_state import room_client, headers
from app.models.collaboration import ProviderConnection, RoomAgentRun
from app.models.models import Agent
from app.models.slack import SlackConfig, SlackInbox, SlackRun, SlackOutbox
from app.services import slack
from app.services.events import stage_event, ActorRef

EPOCH = 'a' * 64


def seed_scope(client, owner, project, agent_id):
    with client.app.state.session_factory() as db:
        conn = ProviderConnection(user_id=owner, kind='compatible', name='Synthetic',
            base_url='http://127.0.0.1:8765/v1', default_model_id='model', auth_required=False, enabled=True)
        db.add(conn); db.flush()
        agent = db.get(Agent, agent_id); agent.connection_id = conn.id; agent.model_id = 'model'
        db.add(SlackConfig(owner_id=owner, revision=1, operation_id='1'*36, state='configured',
            account_id='2'*36, credential_revision=1, enabled=True, workspace_id='T1', app_id='A1',
            channel_id='C1', sender_ids=['U1'], agent_id=agent_id, project_id=project,
            connection_id=conn.id, model_id='model', updated_at=datetime(2026, 1, 1)))
        db.commit()


def add(db, owner, event_id, thread, offset):
    db.add(SlackInbox(owner_id=owner, workspace_id='T1', event_id=event_id, digest='a'*64,
        app_id='A1', owner_epoch=EPOCH, config_revision=1, credential_revision=1, channel_id='C1',
        user_id='U1', event_ts=f'{offset+1}.000001', thread_ts=thread, text='synthetic',
        state='queued', created_at=datetime(2026, 1, 1) + timedelta(seconds=offset)))
    db.commit()


def test_busy_thread_does_not_block_other_threads_and_preserves_fifo(room_client):
    client, (owner, _, project, agent, _) = room_client
    seed_scope(client, owner, project, agent)
    with client.app.state.session_factory() as db:
        add(db, owner, 'first', '1.000001', 0)
        first = slack.claim(db, owner, EPOCH)
        add(db, owner, 'second', '1.000001', 1)
        add(db, owner, 'third', '1.000001', 2)
        add(db, owner, 'other', '2.000001', 3)
        other = slack.claim(db, owner, EPOCH)
        assert db.get(SlackRun, other).event_id == 'other'
        assert slack.claim(db, owner, EPOCH) is None
        first_run = db.get(SlackRun, first)
        member = db.get(RoomAgentRun, (first_run.room_id, agent))
        member.state = 'idle'; member.active_tool = {}; member.pending_task = {}
        first_run.state = 'completed';db.add(SlackOutbox(run_id=first,attempt_id='00000000-0000-4000-8000-000000000001',state='sent',updated_at=datetime(2026, 1, 1)));db.commit()
        second = slack.claim(db, owner, EPOCH)
        assert db.get(SlackRun, second).event_id == 'second'
        assert db.get(SlackInbox, (owner, 'T1', 'third')).state == 'queued'


def test_simultaneous_claims_do_not_start_same_event_twice(room_client):
    client, (owner, _, project, agent, _) = room_client
    seed_scope(client, owner, project, agent)
    with client.app.state.session_factory() as db:
        add(db, owner, 'only', '1.000001', 0)
    barrier = Barrier(2)
    def claim():
        with client.app.state.session_factory() as db:
            barrier.wait(timeout=5)
            return slack.claim(db, owner, EPOCH)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(result is not None for result in results) == 1
    with client.app.state.session_factory() as db:
        assert db.query(SlackRun).filter_by(owner_id=owner, event_id='only').count() == 1


def test_bounded_pages_reach_later_thread_without_overtaking(room_client):
    client, (owner, _, project, agent, _) = room_client
    seed_scope(client, owner, project, agent)
    cursors = {}
    with client.app.state.session_factory() as db:
        add(db, owner, 'active', '1.000001', 0)
        active = slack.claim(db, owner, EPOCH, cursors)
        for i in range(40):
            add(db, owner, f'waiting-{i:02}', '1.000001', i+1)
        add(db, owner, 'independent', '2.000001', 50)
        assert slack.claim(db, owner, EPOCH, cursors) is None
        independent = slack.claim(db, owner, EPOCH, cursors)
        assert db.get(SlackRun, independent).event_id == 'independent'
        first = db.get(SlackRun, active)
        member = db.get(RoomAgentRun, (first.room_id, agent))
        member.state = 'idle'; member.active_tool = {}; member.pending_task = {}
        first.state = 'completed';db.add(SlackOutbox(run_id=active,attempt_id='00000000-0000-4000-8000-000000000002',state='sent',updated_at=datetime(2026, 1, 1)));db.commit()
        # Cursor wraps; a later same-thread item must not pass the oldest.
        assert slack.claim(db, owner, EPOCH, cursors) is None
        next_run = slack.claim(db, owner, EPOCH, cursors)
        assert db.get(SlackRun, next_run).event_id == 'waiting-00'


def test_unknown_does_not_unblock_until_sent_observation_and_scope_cas(room_client):
    client, (owner, other, project, agent, _) = room_client
    seed_scope(client, owner, project, agent)
    with client.app.state.session_factory() as db:
        add(db, owner, 'first', '1.000001', 0)
        first = slack.claim(db, owner, EPOCH)
        add(db, owner, 'waiting', '1.000001', 1)
        run = db.get(SlackRun, first); run.state='completed'
        result=stage_event(db,run.room_id,ActorRef('agent',agent),'message',{'content':'Synthetic expected reply','trigger_event_id':run.root_event_id},'synthetic-result')
        run.result_event_id=result.event_id
        member=db.get(RoomAgentRun,(run.room_id,agent));member.state='idle';member.active_tool={};member.pending_task={}
        attempt=str(uuid4());db.add(SlackOutbox(run_id=first,attempt_id=attempt,state='unknown',updated_at=datetime(2026,1,1)))
        config=db.get(SlackConfig,owner);config.revision=2;config.enabled=False;db.commit()
        assert slack.unresolved_replies(db,owner)['items'][0]['run_id']==first
        value=SimpleNamespace(operation_id=str(uuid4()),attempt_id=attempt,decision='owner_confirmed_not_sent',expected_state='unknown',expected_config_revision=2,expected_run_revision=1,expected_core_epoch='b'*64)
        assert slack.resolve_reply(db,owner,EPOCH,first,value)['decision']=='owner_confirmed_not_sent'
        # Configuration can change or be disabled; observing absence never unblocks an unknown dispatch.
        config=db.get(SlackConfig,owner);config.revision=1;config.enabled=True;db.commit()
        assert slack.claim(db,owner,EPOCH) is None
        with pytest.raises(HTTPException):slack.resolve_reply(db,other,EPOCH,first,value)
        with pytest.raises(HTTPException):slack.resolve_reply(db,owner,EPOCH,first,SimpleNamespace(**{**vars(value),'operation_id':str(uuid4()),'expected_config_revision':2,'decision':'owner_confirmed_sent'}))
        sent=SimpleNamespace(**{**vars(value),'operation_id':str(uuid4()),'expected_config_revision':1,'decision':'owner_confirmed_sent'})
        assert slack.resolve_reply(db,owner,EPOCH,first,sent)['decision']=='owner_confirmed_sent'
        assert slack.resolve_reply(db,owner,EPOCH,first,sent)['operation_id']==sent.operation_id
        for field,value in [('expected_run_revision',2),('expected_state','blocked')]:
            with pytest.raises(HTTPException):slack.resolve_reply(db,owner,EPOCH,first,SimpleNamespace(**{**vars(sent),field:value}))
        assert db.get(SlackRun,slack.claim(db,owner,EPOCH)).event_id=='waiting'


def test_failed_run_without_outbox_can_be_seen_and_skipped(room_client):
    client, (owner, _, project, agent, _) = room_client
    seed_scope(client, owner, project, agent)
    with client.app.state.session_factory() as db:
        add(db,owner,'first','1.000001',0);first=slack.claim(db,owner,EPOCH)
        add(db,owner,'waiting','1.000001',1)
        run=db.get(SlackRun,first);run.state='failed';member=db.get(RoomAgentRun,(run.room_id,agent));member.state='idle';member.active_tool={};member.pending_task={};db.commit()
        row=slack.unresolved_replies(db,owner)['items'][0];assert row['attempt_id'] is None and row['state']=='failed'
        assert slack.claim(db,owner,EPOCH) is None
        value=SimpleNamespace(operation_id=str(uuid4()),attempt_id=None,decision='owner_skip_no_reply',expected_state='failed',expected_config_revision=1,expected_run_revision=1,expected_core_epoch='b'*64)
        slack.resolve_reply(db,owner,EPOCH,first,value)
        assert db.get(SlackRun,slack.claim(db,owner,EPOCH)).event_id=='waiting'


def test_unknown_tool_outcome_can_be_held_but_never_skipped(room_client):
    client, (owner, _, project, agent, _) = room_client
    seed_scope(client,owner,project,agent)
    with client.app.state.session_factory() as db:
        add(db,owner,'first','1.000001',0);first=slack.claim(db,owner,EPOCH)
        add(db,owner,'waiting','1.000001',1)
        run=db.get(SlackRun,first);run.state='unknown';member=db.get(RoomAgentRun,(run.room_id,agent));member.state='idle';member.active_tool={};member.pending_task={};db.commit()
        assert slack.unresolved_replies(db,owner)['items'][0]['attempt_id'] is None
        hold=SimpleNamespace(operation_id=str(uuid4()),attempt_id=None,decision='owner_hold_unknown',expected_state='unknown',expected_config_revision=1,expected_run_revision=1,expected_core_epoch='b'*64)
        slack.resolve_reply(db,owner,EPOCH,first,hold)
        assert slack.claim(db,owner,EPOCH) is None
        with pytest.raises(HTTPException):slack.resolve_reply(db,owner,EPOCH,first,SimpleNamespace(**{**vars(hold),'operation_id':str(uuid4()),'decision':'owner_skip_no_reply'}))


def test_unresolved_keyset_survives_removal_and_refresh(room_client):
    client, (owner, _, project, agent, _) = room_client
    seed_scope(client,owner,project,agent)
    with client.app.state.session_factory() as db:
        add(db,owner,'seed','1.000001',0);first=slack.claim(db,owner,EPOCH)
        run=db.get(SlackRun,first);run.state='failed';db.commit()
        for i in range(55):
            event=f'event-{i:03}';add(db,owner,event,f'{i+2}.000001',i+1)
            db.add(SlackRun(run_id=str(uuid4()),owner_id=owner,workspace_id='T1',event_id=event,
                config_revision=1,credential_revision=1,owner_epoch=EPOCH,room_id=run.room_id,agent_id=agent,
                generation=1,profile_fingerprint=run.profile_fingerprint,max_tool_calls=8,max_executor_seconds=60,
                connection_id=run.connection_id,model_id='model',state='failed',root_event_id=run.root_event_id,created_at=run.created_at));db.commit()
        page1=slack.unresolved_replies(db,owner);assert len(page1['items'])==50 and page1['next_cursor']
        first_row=page1['items'][0]
        value=SimpleNamespace(operation_id=str(uuid4()),attempt_id=None,decision='owner_skip_no_reply',expected_state='failed',expected_config_revision=1,expected_run_revision=1,expected_core_epoch='b'*64)
        slack.resolve_reply(db,owner,EPOCH,first_row['run_id'],value)
        page2=slack.unresolved_replies(db,owner,page1['next_cursor'])
        assert len(page2['items'])==6 and not page2['next_cursor']
        assert not ({x['run_id'] for x in page1['items']} & {x['run_id'] for x in page2['items']})
        assert first_row['run_id'] not in {x['run_id'] for x in slack.unresolved_replies(db,owner)['items']}
        with pytest.raises(HTTPException):slack.unresolved_replies(db,owner,'bad-cursor')


def test_owner_resolution_http_receipt_and_replay_conflict(room_client,monkeypatch):
    from app.api.routes import slack as route
    client,(owner,other,project,agent,_)=room_client
    seed_scope(client,owner,project,agent)
    with client.app.state.session_factory() as db:
        add(db,owner,'first','1.000001',0);first=slack.claim(db,owner,EPOCH)
        run=db.get(SlackRun,first);run.state='failed';db.commit()
    fake=object();binding={'version':1,'owner_id':owner,'owner_epoch':EPOCH,'core_epoch':'b'*64}
    monkeypatch.setattr(route,'identity',lambda request,owner_id:(fake,binding))
    monkeypatch.setattr(route,'current',lambda *args:None)
    operation=str(uuid4());payload={'operation_id':operation,'attempt_id':None,'expected_state':'failed',
        'decision':'owner_skip_no_reply','expected_config_revision':1,'expected_run_revision':1,
        'expected_owner_epoch':EPOCH,'expected_core_epoch':'b'*64}
    path=f'/api/v1/slack/replies/{first}/resolution'
    response=client.post(path,headers=headers(client,owner),json=payload)
    assert response.status_code==200,response.text
    assert client.get(path,headers=headers(client,owner),params={'operation_id':operation}).json()['decision']=='owner_skip_no_reply'
    assert client.post(path,headers=headers(client,owner),json={**payload,'expected_run_revision':2}).status_code==409
    assert client.get(path,headers=headers(client,other),params={'operation_id':operation}).status_code==404
