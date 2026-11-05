"""Synthetic file references; no provider, native or browser calls."""
import pytest
from test_room_state import room_client, create_room
from test_scheduler import ready_agents
from app.models.models import User
from app.services.context_preview import build_context_preview
from app.services.context_projection import project_turn_context, ProjectionError
from app.services.events import ActorRef, append_event


def test_preview_selects_exact_bot_and_includes_reference(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, 'file context')
    ada, ben = ready_agents(client, owner, room, ('Ada', 'Ben'))
    with client.app.state.session_factory() as db:
        result = build_context_preview(db, db.get(User, owner), room, '@Ben question', [],
            requires_verified_tools=False, file_projection={'agent_id':ada,'root_event_id':None,'content':'question\nREFERENCE_MARKER'})
    assert result['selected_agent_ids'] == [ada]
    assert any('REFERENCE_MARKER' in msg['content'] for msg in result['agents'][0]['messages'])
    assert ben not in result['selected_agent_ids']


def test_only_verified_file_projection_expands_active_limit():
    active = {'event_id':'a','room_id':1,'seq':1,'actor_type':'user','actor_id':1,
        'actor_name_snapshot':'Owner','kind':'message','payload':{'content':'x'*13000}}
    args = dict(events=[active],extractive_checkpoints=[],watermark_seq=1,active_event_id='a',agent_id=1,system='',memory={},tools=[])
    with pytest.raises(ProjectionError): project_turn_context(**args)
    projected = project_turn_context(**args, active_file_limit=True)
    assert any('x'*13000 in msg['content'] for msg in projected.messages)
    assert not projected.truncations


def test_current_root_only_and_pretransport_recheck(room_client, monkeypatch):
    import asyncio
    from app.services import library_file_questions
    from app.services.providers import ProviderResult
    from app.services.providers.base import attributed_messages
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, 'file runtime')
    ada, ben = ready_agents(client, owner, room, ('Ada', 'Ben'))
    async def current(*args):
        return {'excerpt':'EXACT_FILE_MARKER','extraction':{'truncated':False,'format':'utf8','page_count':None}}
    monkeypatch.setattr(library_file_questions, 'current_for_root', current)
    monkeypatch.setattr('app.services.model_features.permitted', lambda *_: True)
    calls=[]
    async def fake_provider(request):
        calls.append((request.agent_id, str(attributed_messages(request))))
        return ProviderResult(text='done')
    client.app.state.scheduler.provider = fake_provider
    with client.app.state.session_factory() as db:
        event = append_event(db, room, ActorRef('user',owner),'message',
            {'content':'@Ben question','file_context':{'bot_id':ada}})
        root=event.event_id
    client.portal.call(lambda: client.app.state.scheduler.schedule_user_event(room,root,wait=True))
    with client.app.state.session_factory() as db:
        from app.models.collaboration import RoomEvent
        failures=[e.payload for e in db.query(RoomEvent).filter_by(room_id=room,kind='failure').all()]
    assert calls and all(agent==ada and 'EXACT_FILE_MARKER' in text for agent,text in calls), failures
    calls.clear()
    with client.app.state.session_factory() as db:
        next_event=append_event(db,room,ActorRef('user',owner),'message',{'content':'@Ada next'})
        next_id=next_event.event_id
    client.portal.call(lambda: client.app.state.scheduler.schedule_user_event(room,next_id,wait=True))
    assert calls and all('EXACT_FILE_MARKER' not in text for _,text in calls)


def test_revoked_at_final_check_prevents_transport(room_client, monkeypatch):
    from fastapi import HTTPException
    from app.services import library_file_questions
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, 'revoked file')
    (ada,) = ready_agents(client, owner, room, ('Ada',))
    checks=[]; calls=[]
    async def current(*args):
        checks.append(1)
        if len(checks) == 3: raise HTTPException(409, 'changed')
        return {'excerpt':'private fixture','extraction':{'truncated':False,'format':'utf8','page_count':None}}
    async def fake_provider(request): calls.append(request); raise AssertionError('transport must not run')
    monkeypatch.setattr(library_file_questions,'current_for_root',current)
    monkeypatch.setattr('app.services.model_features.permitted',lambda *_:True)
    client.app.state.scheduler.provider=fake_provider
    with client.app.state.session_factory() as db:
        root=append_event(db,room,ActorRef('user',owner),'message',{'content':'question','file_context':{'bot_id':ada}}).event_id
    client.portal.call(lambda: client.app.state.scheduler.schedule_user_event(room,root,wait=True))
    assert len(checks)==3 and not calls
