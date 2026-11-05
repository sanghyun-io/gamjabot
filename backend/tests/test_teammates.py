"""Disposable source integration checks; real-provider evidence is recorded separately."""
import json
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from test_room_state import room_client, headers, create_room
from app.models.models import Agent
from app.models.collaboration import ProviderConnection, RoomEvent, RoomScheduleSettings
from app.services.providers import ProviderResult, ToolCall


def team(client, owner, room, names=("Ada", "Ben")):
    base = "http://127.0.0.1:8765/v1"
    caps = {"model_features": {"exact": {
        "model_id": "exact", "base_url": base, "config_revision": 1,
        "reported": {"text_generation": True, "tool_calling": True},
        "tested": {"text_generation": True, "tool_calling": True},
    }}}
    with client.app.state.session_factory() as db:
        connection = ProviderConnection(user_id=owner, kind="compatible", name="Synthetic only",
            base_url=base, default_model_id="exact", auth_required=False, enabled=True,
            capabilities=caps)
        db.add(connection); db.flush()
        agents = []
        for name in names:
            row = Agent(user_id=owner, name=name, description=name, system_prompt=name,
                connection_id=connection.id, model_id="exact", temperature="auto", max_tokens=0)
            db.add(row); db.flush(); agents.append(row.id)
        settings = db.get(RoomScheduleSettings, room)
        if settings is None:
            db.add(RoomScheduleSettings(room_id=room, max_rounds=1, max_tool_calls=8, max_executor_seconds=60))
        else:
            settings.max_rounds = 1
        db.commit()
    for agent in agents:
        assert client.post(f"/api/v1/conversations/{room}/members/{agent}", headers=headers(client, owner)).status_code == 201
    client.app.state.scheduler.broker = SimpleNamespace(tool_schemas=[], requires_proposer_binding=False,
        executor=None, session_id="00000000-0000-4000-8000-000000000001")
    return agents


def send_call(target, content, call_id="delegate"):
    return ProviderResult(tool_calls=[ToolCall(call_id, "message_teammate", {"agent_id": target, "content": content})],
        continuation={"call_ids": [call_id]})


def test_selected_sender_delegates_to_unselected_peer_and_resumes_with_real_result(room_client):
    client, (owner, other, project, _, _) = room_client
    room = create_room(client, owner, project, "Teammates")
    ada, ben = team(client, owner, room)
    seen = []

    async def provider(request):
        seen.append((request.agent_id, request.system))
        assert {"list_teammates", "message_teammate"} <= {t["name"] for t in request.tools}
        if request.agent_id == ben:
            assert "Check the launch criteria" in request.system or "Check the launch criteria" in json.dumps(request.events)
            return ProviderResult(text="BEN_ACTUAL_RESULT: missing rollback instructions")
        if "You have resumed" in request.system:
            assert "BEN_ACTUAL_RESULT" in json.dumps(request.events)
            return ProviderResult(text="ADA_FINAL: added rollback instructions from BEN_ACTUAL_RESULT")
        if request.observations:
            return ProviderResult(text="Sent a task; waiting for the actual response.")
        return send_call(ben, "Check the launch criteria")

    client.app.state.scheduler.provider = provider
    response = client.post(f"/api/v1/conversations/{room}/messages", headers=headers(client, owner), json={"content": "@Ada finish with Ben's review"})
    assert response.status_code == 201, response.text
    listing = client.get(f"/api/v1/conversations/{room}/teammates", headers=headers(client, owner))
    assert listing.status_code == 200, listing.text
    handoff, = listing.json()["handoffs"]
    assert handoff["state"] == "completed" and handoff["reply_state"] == "delivered", handoff
    assert handoff["sender_agent_id"] == ada and handoff["recipient_agent_id"] == ben
    assert handoff["root_request_seq"] < handoff["request_seq"] < handoff["result_seq"] < handoff["return_seq"]
    assert client.get(f"/api/v1/conversations/{room}/teammates", headers=headers(client, other)).status_code == 404
    inspector = client.get(f"/api/v1/conversations/{room}/requests/{handoff['root_request_event_id']}", headers=headers(client, owner))
    assert inspector.status_code == 200, inspector.text
    assert inspector.json()["completion"]["outcome_status"] == "succeeded", inspector.json()
    assert [who for who, _ in seen] == [ada, ada, ben, ada]
    assert any("ADA_FINAL" in row["content"] for row in response.json())


def test_nested_handoff_preserves_parent_sources_and_carries_child_result(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "Nested")
    ada, ben, cy = team(client, owner, room, ("Ada", "Ben", "Cy"))
    client.app.state.owner_epoch_provider = SimpleNamespace(snapshot=lambda: object(), current=lambda _owner: 'synthetic-epoch')

    async def provider(request):
        if request.agent_id == cy:
            return ProviderResult(text="CY_ACTUAL_RESULT_47")
        if "You have resumed" in request.system:
            assert "CY_ACTUAL_RESULT_47" in json.dumps(request.events) + request.system
            return ProviderResult(text="BEN_RESOLVED_47" if request.agent_id == ben else "ADA_FINAL_47")
        if request.observations:
            return ProviderResult(text="Delegated; awaiting result.")
        return send_call(ben if request.agent_id == ada else cy, "Inspect delegated work", str(request.agent_id))

    client.app.state.scheduler.provider = provider
    response = client.post(f"/api/v1/conversations/{room}/messages", headers=headers(client, owner), json={"content": "@Ada finish a nested handoff"})
    assert response.status_code == 201, response.text
    rows = client.get(f"/api/v1/conversations/{room}/teammates", headers=headers(client, owner)).json()["handoffs"]
    assert len(rows) == 2 and all(r["state"] == "completed" and r["reply_state"] == "delivered" for r in rows), rows
    with client.app.state.session_factory() as db:
        for row in rows:
            event = db.get(RoomEvent, row["result_event_id"])
            assert event.payload["teammate_handoff"]["handoff_id"] == row["handoff_id"]
            assert event.payload["teammate_handoff"]["phase"] == "response"
    assert any("ADA_FINAL_47" in row["content"] for row in response.json())


def test_failed_colleague_is_not_reported_as_whole_request_success(room_client):
    from app.services.providers.base import ProviderFailure
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "Failure")
    ada, ben = team(client, owner, room)
    async def provider(request):
        if request.agent_id == ben:
            raise ProviderFailure("provider_unavailable", "Synthetic unavailable colleague")
        if "You have resumed" in request.system:
            return ProviderResult(text="The colleague failed; this is incomplete.")
        if request.observations:
            return ProviderResult(text="Requested work.")
        return send_call(ben, "Check the task")
    client.app.state.scheduler.provider = provider
    client.post(f"/api/v1/conversations/{room}/messages", headers=headers(client, owner), json={"content": "@Ada ask Ben"})
    row, = client.get(f"/api/v1/conversations/{room}/teammates", headers=headers(client, owner)).json()["handoffs"]
    assert row["state"] == "failed" and row["reply_state"] == "delivered"
    result = client.get(f"/api/v1/conversations/{room}/requests/{row['root_request_event_id']}", headers=headers(client, owner))
    assert result.status_code == 200 and result.json()["completion"]["outcome_status"] == "partial", result.text


def test_stop_cancels_waiting_colleague_and_prevents_sender_followup(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "Stop")
    ada, ben = team(client, owner, room)
    entered = threading.Event()
    resumed = []
    async def provider(request):
        if request.agent_id == ben:
            entered.set()
            await asyncio.Event().wait()
        if "You have resumed" in request.system:
            resumed.append(True)
            return ProviderResult(text="Should never run")
        if request.observations:
            return ProviderResult(text="Requested work.")
        return send_call(ben, "Wait for a stop")
    client.app.state.scheduler.provider = provider
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages", headers=headers(client, owner), json={"content": "@Ada ask Ben"})
        assert entered.wait(5), "colleague never started"
        stopped = client.post(f"/api/v1/conversations/{room}/stop", headers=headers(client, owner))
        assert stopped.status_code == 204, stopped.text
        pending.result(timeout=5)
    row, = client.get(f"/api/v1/conversations/{room}/teammates", headers=headers(client, owner)).json()["handoffs"]
    assert row["state"] == "cancelled" and row["reply_state"] == "cancelled"
    assert resumed == []


@pytest.mark.asyncio
async def test_chatgpt_offers_collaboration_without_exposing_unverified_os_tools(monkeypatch):
    from app.services.connections import ConnectionConfig
    from app.services.providers.base import ProviderRequest, wire_tool_name
    from app.services.providers.chatgpt import ChatGPTAdapter
    from app.services import connections, teammates
    base = "https://api.openai.com/v1"
    caps = {"verified": {"model_id": "oauth-exact", "base_url": base, "tools": True},
        "model_features": {"oauth-exact": {"model_id": "oauth-exact", "base_url": base,
            "config_revision": 1, "tested": {"text_generation": True, "tool_calling": True}, "tool_families": {"read_file": True}}}}
    config = ConnectionConfig(1, "chatgpt", "oauth-exact", base, "synthetic-no-network-token", caps, provider_id="chatgpt", protocol_id="openai_responses", config_revision=1)
    async def discover(_config, transport=None): return [{"id": "oauth-exact"}], False, None
    monkeypatch.setattr(connections, "discover_models", discover)
    captured = {}
    adapter = ChatGPTAdapter()
    async def post(request, path, hdrs, body):
        captured.update(body)
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ready"}]}]}
    monkeypatch.setattr(adapter, "_post", post)
    os_tools = [{"name": name, "parameters": {"type": "object", "properties": {}}} for name in ("read_file", "write_file", "start_process")]
    result = await adapter.generate(ProviderRequest(connection=config, model_id="oauth-exact", room_id=1, tools=[*os_tools, *teammates.TOOL_SCHEMAS]))
    names = {t["name"] for namespace in captured["tools"] for t in namespace["tools"]}
    assert names == {wire_tool_name(x) for x in ("read_file", "list_teammates", "message_teammate")}
    assert "collaboration" in captured["instructions"].lower()
    assert result.text == "ready"


def assert_incomplete_retry_blocked(client, owner, room, root_id, failure_id):
    url = f'/api/v1/conversations/{room}/failures/{failure_id}/retry'
    preview = client.get(url, headers=headers(client, owner))
    assert preview.status_code == 200, preview.text
    assert preview.json()['eligible'] is False and preview.json()['blocked_reason'] == 'teammate_result_incomplete'
    bypass = client.post(url, headers={**headers(client, owner), 'Idempotency-Key':'synthetic-bypass'},
                         json={'plan_hash':'a'*64})
    assert bypass.status_code == 409 and bypass.json()['detail']['reason'] == 'teammate_result_incomplete'
    from app.services.teammates import retry_content
    from app.services.llm_service import LLMFailure
    with client.app.state.session_factory() as db:
        with pytest.raises(LLMFailure) as error:
            retry_content(db, db.get(RoomEvent, failure_id), db.get(RoomEvent, root_id))
        assert error.value.code == 'teammate_result_incomplete'
    inspected = client.get(f'/api/v1/conversations/{room}/requests/{root_id}', headers=headers(client, owner))
    assert inspected.status_code == 200, inspected.text
    assert any(item['event_id'] == failure_id and item['summary_code'] == 'failure_teammate_result_incomplete'
               for item in inspected.json()['outcomes'])


@pytest.mark.parametrize(('case', 'redelegate'), [('success', False), ('success', True), ('success', 'long'),
                                                   ('source_changed', False), ('source_midread', False),
                                                   ('owner_changed', False), ('child_owner_changed', True), ('stop', False),
                                                   ('mixed_read_effect', False), ('read_only_receipt', False),
                                                   ('missing_read_receipt', False), ('effect_before_post', False),
                                                   ('effect_after_post', False), ('source_before_room', False),
                                                   ('approval_source_changed', False), ('restart_pending', False),
                                                   ('restart_running', False), ('admission_model', False),
                                                   ('admission_connection', False), ('admission_memory', False),
                                                   ('memory_after_dispatch', True), ('admission_between', False)])
def test_long_teammate_retry_snapshot_can_be_reviewed(room_client, case, redelegate, monkeypatch):
    client, (owner, other, project, _, _) = room_client
    room = create_room(client, owner, project, 'Synthetic retry context')
    ada, ben, cy = team(client, owner, room, ('Ada', 'Ben', 'Cy'))
    epoch = {'value': 'synthetic-epoch'}
    client.app.state.owner_epoch_provider = SimpleNamespace(snapshot=lambda: object(), current=lambda _owner: epoch['value'])
    async def provider(request):
        if request.agent_id == ben:
            return ProviderResult(text='A' * 4100 + 'TAIL')
        if 'You have resumed' in request.system:
            return ProviderResult(text='Premature answer')
        if request.observations:
            return ProviderResult(text='Waiting')
        return send_call(ben, 'Review')
    client.app.state.scheduler.provider = provider
    sent = client.post(f'/api/v1/conversations/{room}/messages', headers=headers(client, owner),
                       json={'content': '@Ada ask Ben'})
    assert sent.status_code == 502, sent.text
    handoff, = client.get(f'/api/v1/conversations/{room}/teammates', headers=headers(client, owner)).json()['handoffs']
    url = f"/api/v1/conversations/{room}/failures/{handoff['return_event_id']}/retry"
    def add_prior_action(state, family, with_receipt=False):
        from uuid import uuid4
        from app.models.collaboration import ToolAction
        from app.services.events import ActorRef, stage_event
        with client.app.state.session_factory() as db:
            action_id = str(uuid4())
            db.add(ToolAction(action_id=action_id, room_id=room, agent_id=ada,
                trigger_event_id=handoff['root_request_event_id'], family=family, arguments={}, state=state))
            db.flush()
            if with_receipt:
                stage_event(db, room, ActorRef('tool', None), 'tool_result',
                    {'trigger_event_id': handoff['root_request_event_id'], 'agent_id': ada,
                     'action_id': action_id, 'state': state}, 'tool-result:' + action_id)
            db.commit()
    if case in ('read_only_receipt', 'missing_read_receipt'):
        add_prior_action('completed', 'read_file', with_receipt=case == 'read_only_receipt')
    preview = client.get(url, headers=headers(client, owner))
    assert preview.status_code == 200, preview.text
    assert client.get(url, headers=headers(client, other)).status_code == 404
    if case == 'missing_read_receipt':
        assert not preview.json()['eligible'] and preview.json()['blocked_reason'] == 'completed_effect'
        return
    assert preview.json()['eligible'], preview.json()
    assert preview.json()['teammate_recovery'] == {'version': 1, 'direct_count': 1, 'nested_count': 0,
                                                   'total_chars': 4104, 'single_attempt': True}
    original_schedule = client.app.state.scheduler.schedule_user_event
    async def parked(*_args, **_kwargs):
        return None
    client.app.state.scheduler.schedule_user_event = parked
    if case == 'effect_before_post':
        add_prior_action('started', 'write_file')
    posted = client.post(url, headers={**headers(client, owner), 'Idempotency-Key': 'synthetic-retry'},
                         json={'plan_hash': preview.json()['plan_hash']})
    if case == 'effect_before_post':
        assert posted.status_code == 409
        return
    assert posted.status_code == 201, posted.text
    retry_id = posted.json()['retry_event_id']
    replay = client.post(url, headers={**headers(client, owner), 'Idempotency-Key': 'synthetic-retry'},
                         json={'plan_hash': preview.json()['plan_hash']})
    assert replay.status_code == 201 and replay.json()['replayed'] is True
    blocked_again = client.get(url, headers=headers(client, owner))
    assert blocked_again.json()['eligible'] is False and blocked_again.json()['blocked_reason'] == 'failure_superseded'
    duplicate = client.post(url, headers={**headers(client, owner), 'Idempotency-Key': 'different-key'},
                            json={'plan_hash': preview.json()['plan_hash']})
    assert duplicate.status_code == 409
    if case == 'source_changed':
        with client.app.state.session_factory() as db:
            event = db.get(RoomEvent, handoff['result_event_id'])
            event.payload = {**event.payload, 'content': 'changed original'}
            db.commit()
    if case == 'effect_after_post':
        add_prior_action('started', 'write_file')
    if case.startswith('admission_'):
        from app.models.models import Agent
        from app.models.collaboration import ProviderConnection, RoomAgentRun
        with client.app.state.session_factory() as db:
            agent = db.get(Agent, ada)
            if case == 'admission_model':
                agent.model_id = 'changed-synthetic-model'
            elif case == 'admission_connection':
                connection = db.get(ProviderConnection, agent.connection_id)
                connection.readiness_revision += 1
            elif case == 'admission_memory':
                db.get(RoomAgentRun, (room, ada)).memory = {'synthetic': 'changed'}
            db.commit()
    if case == 'admission_between':
        from app.services import request_retry
        original_digest = request_retry.retry_admission_digest
        seen_admission = []
        def mutate_after_dispatch_check(db, owner_id, room_id, agent_id):
            value = original_digest(db, owner_id, room_id, agent_id)
            if not seen_admission:
                seen_admission.append(True)
                db.get(Agent, agent_id).model_id = 'synthetic-between-checks'
            return value
        monkeypatch.setattr(request_retry, 'retry_admission_digest', mutate_after_dispatch_check)
    if case == 'restart_running':
        from app.models.collaboration import RoomAgentRun, RoomTrigger
        with client.app.state.session_factory() as db:
            db.get(RoomTrigger, retry_id).state = 'running'
            run = db.get(RoomAgentRun, (room, ada))
            run.state = 'running'; run.generation += 1; run.trigger_event_id = retry_id
            db.commit()
    parts = {False: [], True: []}
    child_parts = []
    trace = []
    async def resumed_provider(request):
        trace.append((request.agent_id, 'You have resumed' in request.system, len(request.observations), 'explicit user retry' in request.system))
        if request.agent_id == cy:
            if case == 'memory_after_dispatch':
                from app.models.collaboration import RoomAgentRun
                with client.app.state.session_factory() as db:
                    db.get(RoomAgentRun, (room, ada)).memory = {'synthetic': 'legitimate later write'}
                    db.commit()
            if case == 'child_owner_changed':
                epoch['value'] = 'changed-epoch'
            return ProviderResult(text=('C' * 4000 if redelegate == 'long' else '') + 'CY_FOLLOWUP')
        is_return = 'You have resumed' in request.system
        assert 'explicit user retry' in request.system
        assert any(item['name'] == 'read_teammate_result' for item in request.tools)
        if is_return and redelegate == 'long' and not child_parts and not request.observations:
            import re
            match = re.search(r'with digest ([0-9a-f]{64}) and offset (\d+)', request.system)
            assert match
            return ProviderResult(tool_calls=[ToolCall('read-child', 'read_teammate_result',
                {'digest': match.group(1), 'offset': int(match.group(2))})], continuation={'call_ids': ['read-child']})
        if request.observations:
            part = json.loads(request.observations[-1].text)
            if 'next_offset' not in part:
                return ProviderResult(text='Waiting for child')
            if case == 'owner_changed' and not is_return:
                epoch['value'] = 'changed-epoch'
            if case == 'source_midread' and not is_return:
                with client.app.state.session_factory() as db:
                    event = db.get(RoomEvent, handoff['result_event_id'])
                    event.payload = {**event.payload, 'content': 'changed during read'}
                    db.commit()
            if case == 'stop' and not is_return:
                from app.models.execution_scope import ExecutionScope
                from app.services.execution_scopes import identity
                with client.app.state.session_factory() as db:
                    scope = db.get(ExecutionScope, identity(retry_id, 'root'))
                    scope.state = 'revoked'; scope.epoch += 1; db.commit()
            if 'slot' not in part:
                child_parts.append(part['content'])
                assert part['next_offset'] is None and ''.join(child_parts).endswith('CY_FOLLOWUP')
                sources = json.JSONDecoder().raw_decode(request.system.split('Sources: ', 1)[1])[0]
                slot, digest, offset = sources[0]['slot'], sources[0]['digest'], 0
                return ProviderResult(tool_calls=[ToolCall('read-'+str(offset), 'read_teammate_result',
                    {'slot': slot, 'digest': digest, 'offset': offset})], continuation={'call_ids': ['read-'+str(offset)]})
            parts[is_return].append(part['content'])
            if part['next_offset'] is None:
                assert ''.join(parts[is_return]).endswith('TAIL')
                if case == 'approval_source_changed':
                    from uuid import uuid4
                    from app.services import execution_scopes
                    from app.services.action_review import ReviewChanged
                    from app.models.collaboration import RoomAgentRun, ToolAction
                    action_id = str(uuid4())
                    with client.app.state.session_factory() as db:
                        run = db.get(RoomAgentRun, (room, ada))
                        proof = execution_scopes.bind(db, room, ada, retry_id, run.generation)
                        action = ToolAction(action_id=action_id, room_id=room, agent_id=ada,
                            trigger_event_id=retry_id, family='write_file', arguments={}, state='approved', execution_binding=proof)
                        execution_scopes.claim_proposal(db, action)
                        db.add(action); db.commit()
                        event = db.get(RoomEvent, handoff['result_event_id'])
                        event.payload = {**event.payload, 'content': 'changed after approval'}
                        db.commit()
                    with client.app.state.session_factory() as db:
                        with pytest.raises(ReviewChanged):
                            execution_scopes.reserve_executor(db, db.get(ToolAction, action_id))
                if case == 'source_before_room':
                    from app.services import bot_profile, execution_scopes
                    from app.schemas.bot_profile import BotConversationResolve
                    from app.services.action_review import ReviewChanged
                    from app.models.collaboration import RoomAgentRun
                    with client.app.state.session_factory() as db:
                        run = db.get(RoomAgentRun, (room, ada))
                        proof = execution_scopes.bind(db, room, ada, retry_id, run.generation)
                        assert bot_profile.canonical_conversation(db, owner, cy, active_only=True) is None
                        event = db.get(RoomEvent, handoff['result_event_id'])
                        event.payload = {**event.payload, 'content': 'changed before room creation'}
                        db.commit()
                    with client.app.state.session_factory() as db:
                        with pytest.raises(ReviewChanged):
                            bot_profile.resolve_conversation(db, owner, cy, BotConversationResolve(), retry_binding=proof)
                        assert bot_profile.canonical_conversation(db, owner, cy, active_only=True) is None
                if redelegate and not is_return:
                    return send_call(cy, 'Check the recovered result', 'retry-followup')
                if is_return:
                    assert child_parts or 'CY_FOLLOWUP' in request.system or 'CY_FOLLOWUP' in json.dumps(request.events)
                return ProviderResult(text='RECOVERED_TAIL CY_FOLLOWUP' if is_return else 'RECOVERED_TAIL')
            slot, digest, offset = part['slot'], part['digest'], part['next_offset']
        else:
            sources = json.JSONDecoder().raw_decode(request.system.split('Sources: ', 1)[1])[0]
            slot, digest, offset = sources[0]['slot'], sources[0]['digest'], 0
        if case == 'mixed_read_effect' and offset == 3200:
            return ProviderResult(tool_calls=[ToolCall('read-last', 'read_teammate_result',
                {'slot': slot, 'digest': digest, 'offset': offset}),
                ToolCall('premature-handoff', 'message_teammate', {'agent_id': cy, 'content': 'MUST_NOT_RUN'})],
                continuation={'call_ids': ['read-last', 'premature-handoff']})
        return ProviderResult(tool_calls=[ToolCall('read-'+str(offset), 'read_teammate_result',
            {'slot': slot, 'digest': digest, 'offset': offset})], continuation={'call_ids': ['read-'+str(offset)]})
    async def debug_provider(request):
        try:
            return await resumed_provider(request)
        except Exception as error:
            trace.append(('synthetic_callback_error', repr(error)))
            raise
    client.app.state.scheduler.provider = debug_provider
    client.app.state.scheduler.schedule_user_event = original_schedule
    from functools import partial
    if case in ('restart_pending', 'restart_running'):
        from app.services.scheduler import Scheduler
        async def simulate_restart():
            old = client.app.state.scheduler
            fresh = Scheduler(client.app.state.session_factory, old.hub, client.app.state)
            fresh.provider, fresh.broker = debug_provider, old.broker
            await fresh.start()
            try:
                if case == 'restart_pending':
                    await fresh.schedule_user_event(room, retry_id, wait=True)
            finally:
                await fresh.close()
        client.portal.call(simulate_restart)
    else:
        client.portal.call(partial(original_schedule, room, retry_id, wait=True))
    with client.app.state.session_factory() as db:
        events = db.query(RoomEvent).filter_by(room_id=room, kind='message').all()
        answers = [str((event.payload or {}).get('content', '')) for event in events]
        failures = db.query(RoomEvent).filter_by(room_id=room, kind='failure').all()
        if not any('RECOVERED_TAIL' in value for value in answers):
            print('SYNTHETIC_TRACE', trace)
        if case == 'restart_running':
            from app.models.collaboration import RoomTrigger
            assert db.get(RoomTrigger, retry_id).state == 'interrupted' and not trace
            return
        if case not in ('success', 'read_only_receipt', 'restart_pending', 'memory_after_dispatch'):
            assert not any('RECOVERED_TAIL' in value for value in answers)
            assert failures
            if case.startswith('admission_'):
                assert not trace
            if case == 'mixed_read_effect':
                from app.models.teammate import TeammateHandoff
                assert db.query(TeammateHandoff).filter_by(root_request_event_id=retry_id).count() == 0
            return
        assert any('RECOVERED_TAIL' in value for value in answers), (trace, [str((event.payload or {}).get('code')) for event in failures])
        if redelegate:
            assert any('RECOVERED_TAIL' in value and 'CY_FOLLOWUP' in value for value in answers)

@pytest.mark.parametrize('complete_read', [True, False])
def test_long_teammate_result_requires_exact_chunk_read_before_final(room_client, complete_read):
    import re
    from app.services.providers.base import observation_text
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, 'Long teammate result')
    ada, ben = team(client, owner, room)
    client.app.state.owner_epoch_provider = SimpleNamespace(snapshot=lambda: object(), current=lambda _owner: 'synthetic-epoch')
    result = ('A' * 4000) + ('\x01' * 1200) + 'TAIL_FINAL_FACT'
    parts = []
    async def provider(request):
        if request.agent_id == ben:
            return ProviderResult(text=result)
        if 'You have resumed' in request.system:
            assert 'INCOMPLETE' in request.system
            assert any(t['name'] == 'read_teammate_result' for t in request.tools)
            if not complete_read:
                return ProviderResult(text='Premature answer must not be saved')
            if request.observations:
                raw = request.observations[-1]
                wire = observation_text(raw)
                assert len(wire) < 16000
                part = json.loads(raw.text)
                parts.append(part['content'])
                if part['next_offset'] is None:
                    assert ''.join(parts) == result[4000:]
                    return ProviderResult(text='FINAL: TAIL_FINAL_FACT')
                offset, digest = part['next_offset'], part['digest']
            else:
                match = re.search(r'with digest ([0-9a-f]{64}) and offset (\d+)', request.system)
                assert match
                digest, offset = match.group(1), int(match.group(2))
            return ProviderResult(tool_calls=[ToolCall('read-'+str(offset), 'read_teammate_result',
                {'offset': offset, 'digest': digest})], continuation={'call_ids': ['read-'+str(offset)]})
        if request.observations:
            return ProviderResult(text='Delegated; awaiting teammate')
        return send_call(ben, 'Return the long exact result')
    client.app.state.scheduler.provider = provider
    sent = client.post(f'/api/v1/conversations/{room}/messages', headers=headers(client, owner),
                       json={'content': '@Ada ask Ben for the complete result'})
    assert sent.status_code == (201 if complete_read else 502), sent.text
    row, = client.get(f'/api/v1/conversations/{room}/teammates', headers=headers(client, owner)).json()['handoffs']
    if complete_read:
        assert row['reply_state'] == 'delivered'
        assert any('FINAL: TAIL_FINAL_FACT' in e['content'] for e in sent.json())
    else:
        assert row['reply_state'] == 'failed'
        assert sent.json()['detail']['code'] == 'teammate_result_incomplete'
        retry_url = f"/api/v1/conversations/{room}/failures/{row['return_event_id']}/retry"
        preview = client.get(retry_url, headers=headers(client, owner))
        assert preview.status_code == 200 and preview.json()['eligible'] is True
        # A forged failure code cannot borrow the original diagnostic.
        with client.app.state.session_factory() as db:
            event = db.get(RoomEvent, row['return_event_id'])
            event.payload = {**event.payload, 'code':'tool_budget_exhausted'}; db.commit()
        forged = client.get(retry_url, headers=headers(client, owner))
        assert forged.status_code == 200 and forged.json()['eligible'] is False


def test_nested_long_result_is_read_before_final(room_client):
    import re
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, 'Nested long result')
    ada, ben, cy = team(client, owner, room, ('Ada', 'Ben', 'Cy'))
    client.app.state.owner_epoch_provider = SimpleNamespace(snapshot=lambda: object(), current=lambda _owner: 'synthetic-epoch')
    tail = 'LONG_NESTED_TAIL'
    async def provider(request):
        if request.agent_id == cy:
            return ProviderResult(text='CY_SHORT')
        if 'You have resumed' in request.system:
            if request.agent_id == ben:
                return ProviderResult(text=('B' * 4001) + tail)
            if request.observations:
                part = json.loads(request.observations[-1].text)
                assert tail in part['content']
                return ProviderResult(text='ADA_FINAL_NESTED_TAIL')
            match = re.search(r'slot ([0-9a-f-]+), digest ([0-9a-f]{64}), offset (\d+)', request.system)
            assert match and any(t['name'] == 'read_teammate_result' for t in request.tools)
            return ProviderResult(tool_calls=[ToolCall('read-nested', 'read_teammate_result',
                {'slot': match.group(1), 'digest': match.group(2), 'offset': int(match.group(3))})],
                continuation={'call_ids':['read-nested']})
        if request.observations:
            return ProviderResult(text='Requested work')
        return send_call(ben if request.agent_id == ada else cy, 'Inspect task', str(request.agent_id))
    client.app.state.scheduler.provider = provider
    sent = client.post(f'/api/v1/conversations/{room}/messages', headers=headers(client, owner),
                       json={'content': '@Ada finish nested task'})
    assert sent.status_code == 201, sent.text
    assert any('ADA_FINAL_NESTED_TAIL' in e['content'] for e in sent.json())


def test_long_result_mutation_before_chunk_fails_closed(room_client):
    import re
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, 'Changed teammate result')
    ada, ben = team(client, owner, room)
    client.app.state.owner_epoch_provider = SimpleNamespace(snapshot=lambda: object(), current=lambda _owner: 'synthetic-epoch')
    async def provider(request):
        if request.agent_id == ben:
            return ProviderResult(text='A' * 4100)
        if 'You have resumed' in request.system:
            match = re.search(r'with digest ([0-9a-f]{64}) and offset (\d+)', request.system)
            assert match
            with client.app.state.session_factory() as db:
                from app.models.teammate import TeammateHandoff
                row = db.query(TeammateHandoff).filter_by(room_id=room).one()
                event = db.get(RoomEvent, row.result_event_id)
                event.payload = {**event.payload, 'content': 'B' * 4100}
                db.commit()
            return ProviderResult(tool_calls=[ToolCall('read-changed', 'read_teammate_result',
                {'offset': int(match.group(2)), 'digest': match.group(1)})], continuation={'call_ids':['read-changed']})
        if request.observations:
            return ProviderResult(text='Requested work')
        return send_call(ben, 'Return long result')
    client.app.state.scheduler.provider = provider
    sent = client.post(f'/api/v1/conversations/{room}/messages', headers=headers(client, owner),
                       json={'content':'@Ada ask Ben'})
    assert sent.status_code == 502, sent.text
    assert sent.json()['detail']['code'] == 'teammate_result_changed'


@pytest.mark.parametrize('provider', ['opencode-go', 'chatgpt'])
def test_read_teammate_result_provider_wire_roundtrip(monkeypatch, provider):
    from app.services import teammates, connections, model_features, provider_catalog
    from app.services.connections import ConnectionConfig
    from app.services.providers.base import ProviderRequest, wire_tool_name
    from app.services.providers.opencode_go import OpenCodeGoMessagesAdapter
    from app.services.providers.chatgpt import ChatGPTAdapter
    model = 'qwen3.7-plus' if provider == 'opencode-go' else 'oauth-exact'
    base = provider_catalog.OPENCODE_GO_BASE_URL if provider == 'opencode-go' else 'https://api.openai.com/v1'
    protocol = 'anthropic_messages' if provider == 'opencode-go' else 'openai_responses'
    caps = {'verified': {'model_id':model,'base_url':base,'tools':True},
            'model_features': {model:{'model_id':model,'base_url':base,'config_revision':1,
                'tested':{'text_generation':True,'tool_calling':True}}}}
    cfg = ConnectionConfig(1,provider,model,base,'synthetic',caps,provider_id=provider,
                           protocol_id=protocol,config_revision=1,owner_id=1)
    req = ProviderRequest(connection=cfg,model_id=model,room_id=0,system='Synthetic',tools=[teammates.RESULT_TOOL_SCHEMA])
    async def enforce(_request): return None
    async def discover(_config, transport=None): return [{'id':model}],False,None
    monkeypatch.setattr(model_features,'enforce',enforce)
    monkeypatch.setattr(connections,'discover_models',discover)
    adapter = OpenCodeGoMessagesAdapter() if provider == 'opencode-go' else ChatGPTAdapter()
    captured={}
    async def post(request,path,headers,body):
        captured.clear(); captured.update(body)
        if request.observations:
            if provider == "opencode-go":
                return {"content":[{"type":"text","text":"TAIL_FINAL_FACT"}]}
            return {"output":[{"type":"message","content":[{"type":"output_text","text":"TAIL_FINAL_FACT"}]}]}
        args=json.dumps({'slot':'synthetic-slot','offset':4000,'digest':'a'*64})
        if provider == 'opencode-go':
            return {'content':[{'type':'tool_use','id':'read1','name':wire_tool_name('read_teammate_result'),'input':json.loads(args)}]}
        return {'output':[{'type':'function_call','call_id':'read1','name':wire_tool_name('read_teammate_result'),'arguments':args}]}
    monkeypatch.setattr(adapter,'_post',post)
    response=asyncio.run(adapter.generate(req))
    names=({entry['name'] for entry in captured['tools']} if provider=='opencode-go' else
           {entry['name'] for namespace in captured['tools'] for entry in namespace['tools']})
    assert wire_tool_name('read_teammate_result') in names
    assert len(response.tool_calls)==1 and response.tool_calls[0].family=='read_teammate_result'
    assert response.tool_calls[0].arguments['slot'] == 'synthetic-slot'
    assert response.tool_calls[0].arguments=={'slot':'synthetic-slot','offset':4000,'digest':'a'*64}
    from dataclasses import replace
    from app.services.providers.base import ToolObservation
    result = json.dumps({'offset':4000,'next_offset':None,'content':'TAIL_FINAL_FACT'})
    continuation = replace(req,continuation=response.continuation,observations=[ToolObservation('read1','completed',result)])
    final = asyncio.run(adapter.generate(continuation))
    assert 'TAIL_FINAL_FACT' in json.dumps(captured) and final.text == 'TAIL_FINAL_FACT'


@pytest.mark.parametrize('changed', ['owner_epoch', 'scope_deadline'])
def test_long_result_owner_or_deadline_change_stops_read(room_client, changed):
    import re
    from datetime import datetime, timedelta
    from app.models.execution_scope import ExecutionScope
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, 'Changed handoff access')
    ada, ben = team(client, owner, room)
    epoch = ['owner-a']
    client.app.state.owner_epoch_provider = SimpleNamespace(snapshot=lambda: object(), current=lambda _owner: epoch[0])
    async def provider(request):
        if request.agent_id == ben:
            return ProviderResult(text='A' * 4100)
        if 'You have resumed' in request.system:
            match = re.search(r'with digest ([0-9a-f]{64}) and offset (\d+)', request.system)
            assert match
            if changed == 'owner_epoch':
                epoch[0] = 'owner-b'
            else:
                with client.app.state.session_factory() as db:
                    scopes = db.query(ExecutionScope).filter_by(root_room_id=room).all()
                    assert scopes
                    for scope in scopes:
                        scope.deadline_at = datetime.utcnow() - timedelta(seconds=1)
                    db.commit()
            return ProviderResult(tool_calls=[ToolCall('read-stale', 'read_teammate_result',
                {'offset':int(match.group(2)), 'digest':match.group(1)})],continuation={'call_ids':['read-stale']})
        if request.observations:
            return ProviderResult(text='Requested work')
        return send_call(ben, 'Return long result')
    client.app.state.scheduler.provider = provider
    sent=client.post(f'/api/v1/conversations/{room}/messages',headers=headers(client, owner),json={'content':'@Ada ask Ben'})
    assert sent.status_code == 502, sent.text
    assert sent.json()['detail']['code'] == ('teammate_result_changed' if changed == 'owner_epoch' else 'review_changed')


@pytest.mark.parametrize('change', ['owner_epoch', 'result_content'])
def test_long_result_changed_after_read_prevents_final_answer(room_client, change):
    import re
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, 'Changed before final')
    ada, ben = team(client, owner, room)
    epoch=['owner-a']
    client.app.state.owner_epoch_provider=SimpleNamespace(snapshot=lambda:object(),current=lambda _owner:epoch[0])
    async def provider(request):
        if request.agent_id == ben:
            return ProviderResult(text='A' * 4100)
        if 'You have resumed' in request.system:
            if request.observations:
                if change == 'owner_epoch':
                    epoch[0]='owner-b'
                else:
                    with client.app.state.session_factory() as db:
                        from app.models.teammate import TeammateHandoff
                        row=db.query(TeammateHandoff).filter_by(room_id=room).one()
                        event=db.get(RoomEvent,row.result_event_id)
                        event.payload={**event.payload,'content':'B'*4100}; db.commit()
                return ProviderResult(text='MUST NOT SAVE')
            match=re.search(r'with digest ([0-9a-f]{64}) and offset (\d+)',request.system)
            return ProviderResult(tool_calls=[ToolCall('read-final','read_teammate_result',
                {'offset':int(match.group(2)),'digest':match.group(1)})],continuation={'call_ids':['read-final']})
        if request.observations:
            return ProviderResult(text='Requested work')
        return send_call(ben,'Return long result')
    client.app.state.scheduler.provider=provider
    sent=client.post(f'/api/v1/conversations/{room}/messages',headers=headers(client,owner),json={'content':'@Ada ask Ben'})
    assert sent.status_code==502, sent.text
    assert sent.json()['detail']['code']=='teammate_result_changed'


def test_short_teammate_return_retry_remains_available_but_missing_result_is_blocked(room_client):
    from app.services.providers.base import ProviderFailure
    client, (owner, other, project, _, _) = room_client
    room = create_room(client, owner, project, 'Short return retry')
    ada, ben = team(client, owner, room)
    async def provider(request):
        if request.agent_id == ben:
            return ProviderResult(text='Short actual result')
        if 'You have resumed' in request.system:
            raise ProviderFailure('provider_unavailable', 'Synthetic provider unavailable')
        if request.observations:
            return ProviderResult(text='Requested work')
        return send_call(ben, 'Return a short result')
    client.app.state.scheduler.provider = provider
    sent=client.post(f'/api/v1/conversations/{room}/messages', headers=headers(client,owner),
                     json={'content':'@Ada ask Ben'})
    assert sent.status_code == 502, sent.text
    row, = client.get(f'/api/v1/conversations/{room}/teammates',headers=headers(client,owner)).json()['handoffs']
    url=f"/api/v1/conversations/{room}/failures/{row['return_event_id']}/retry"
    preview=client.get(url,headers=headers(client,owner))
    assert preview.status_code == 200 and preview.json()['eligible'],preview.text
    assert client.get(url,headers=headers(client,other)).status_code == 404
    retry=client.post(url,headers={**headers(client,owner),'Idempotency-Key':'short-retry'},
        json={'plan_hash':preview.json()['plan_hash']})
    assert retry.status_code == 201, retry.text
    assert retry.json()['source_failure_event_id'] == row['return_event_id']
    with client.app.state.session_factory() as db:
        from app.models.teammate import TeammateHandoff
        handoff=db.get(TeammateHandoff,row['handoff_id'])
        handoff.result_event_id=None;db.commit()
    assert_incomplete_retry_blocked(client,owner,room,row['root_request_event_id'],row['return_event_id'])

@pytest.mark.parametrize('case', ['forged_slot', 'changed_child', 'short_changed', 'missing_child', 'owner_epoch', 'scope_deadline'])
def test_nested_result_denies_forgery_mutation_and_missing(room_client, monkeypatch, case):
    import re
    from app.services import teammates
    from app.models.teammate import TeammateHandoff
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, 'Nested denied')
    ada, ben, cy = team(client, owner, room, ('Ada', 'Ben', 'Cy'))
    epoch = ['synthetic-epoch']
    client.app.state.owner_epoch_provider = SimpleNamespace(snapshot=lambda: object(), current=lambda _owner: epoch[0])
    original = teammates.finish_job
    def finish(db, job, event):
        original(db, job, event)
        if case == 'missing_child' and job['phase'] == 'return':
            row = db.get(TeammateHandoff, job['handoff_id'])
            if row and row.parent_handoff_id:
                row.return_event_id = None
    monkeypatch.setattr(teammates, 'finish_job', finish)
    async def provider(request):
        if request.agent_id == cy:
            return ProviderResult(text='CY_SHORT')
        if 'You have resumed' in request.system:
            if request.agent_id == ben:
                return ProviderResult(text='SHORT_CHILD' if case == 'short_changed' else ('B' * 4001) + 'TAIL')
            assert case != 'missing_child', 'missing source must not reach provider'
            if case == 'short_changed':
                with client.app.state.session_factory() as db:
                    child = db.query(TeammateHandoff).filter(TeammateHandoff.parent_handoff_id.isnot(None)).one()
                    event = db.get(RoomEvent, child.return_event_id)
                    event.payload = {**event.payload, 'content':'CHANGED'}; db.commit()
                return ProviderResult(text='STALE_ANSWER_MUST_NOT_SAVE')
            match = re.search(r'slot ([0-9a-f-]+), digest ([0-9a-f]{64}), offset (\d+)', request.system)
            assert match
            if case == 'owner_epoch':
                epoch[0] = 'other-epoch'
            if case == 'scope_deadline':
                from datetime import datetime, timedelta
                from app.models.execution_scope import ExecutionScope
                with client.app.state.session_factory() as db:
                    for scope in db.query(ExecutionScope).filter_by(root_room_id=room).all():
                        scope.deadline_at = datetime.utcnow() - timedelta(seconds=1)
                    db.commit()
            if case == 'changed_child':
                with client.app.state.session_factory() as db:
                    child = db.query(TeammateHandoff).filter(TeammateHandoff.parent_handoff_id.isnot(None)).one()
                    event = db.get(RoomEvent, child.return_event_id)
                    event.payload = {**event.payload, 'content':'Z' * 4005}; db.commit()
            slot = 'forged' if case == 'forged_slot' else match.group(1)
            return ProviderResult(tool_calls=[ToolCall('read-nested', 'read_teammate_result',
                {'slot': slot, 'digest': match.group(2), 'offset': int(match.group(3))})],
                continuation={'call_ids':['read-nested']})
        if request.observations:
            return ProviderResult(text='Requested work')
        return send_call(ben if request.agent_id == ada else cy, 'Inspect task', str(request.agent_id))
    client.app.state.scheduler.provider = provider
    sent = client.post(f'/api/v1/conversations/{room}/messages', headers=headers(client, owner),
                       json={'content': '@Ada finish nested task'})
    assert sent.status_code == 502, sent.text
    assert sent.json()['detail']['code'] in {'teammate_result_changed', 'teammate_result_incomplete', 'review_changed'}


@pytest.mark.parametrize('acknowledged', [True, False])
def test_nested_short_child_failure_retry_reviews_all_sources(room_client, acknowledged):
    from app.services.providers.base import ProviderFailure
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, 'Nested short retry blocked')
    ada, ben, cy = team(client, owner, room, ('Ada', 'Ben', 'Cy'))
    client.app.state.owner_epoch_provider = SimpleNamespace(snapshot=lambda: object(), current=lambda _owner: 'synthetic-epoch')
    async def provider(request):
        if request.agent_id == cy:
            return ProviderResult(text='CY_SHORT')
        if 'You have resumed' in request.system:
            if request.agent_id == ben:
                return ProviderResult(text='BEN_SHORT')
            if acknowledged and not request.observations:
                return ProviderResult(tool_calls=[ToolCall('roster-check', 'list_teammates', {})],
                                      continuation={'call_ids': ['roster-check']})
            raise ProviderFailure('provider_unavailable', 'Synthetic unavailable')
        if request.observations:
            return ProviderResult(text='Requested work')
        return send_call(ben if request.agent_id == ada else cy, 'Inspect task', str(request.agent_id))
    client.app.state.scheduler.provider = provider
    sent = client.post(f'/api/v1/conversations/{room}/messages', headers=headers(client, owner),
                       json={'content': '@Ada finish nested task'})
    assert sent.status_code == 502, sent.text
    rows = client.get(f'/api/v1/conversations/{room}/teammates', headers=headers(client, owner)).json()['handoffs']
    failure = next(row for row in rows if row['sender_agent_id'] == ada and row['return_event_id'])
    url = f"/api/v1/conversations/{room}/failures/{failure['return_event_id']}/retry"
    preview = client.get(url, headers=headers(client, owner))
    if not acknowledged:
        assert preview.status_code == 200 and preview.json()['eligible'] is False, preview.text
        assert preview.json()['blocked_reason'] == 'teammate_result_incomplete'
        return
    assert preview.status_code == 200 and preview.json()['eligible'] is True, preview.text
    assert preview.json()['teammate_recovery']['nested_count'] == 1
    original_schedule = client.app.state.scheduler.schedule_user_event
    async def parked(*_args, **_kwargs):
        return None
    client.app.state.scheduler.schedule_user_event = parked
    posted = client.post(url, headers={**headers(client, owner), 'Idempotency-Key': 'nested-recovery'},
                         json={'plan_hash': preview.json()['plan_hash']})
    assert posted.status_code == 201, posted.text
    seen = {}
    async def recover(request):
        assert 'explicit user retry' in request.system
        sources = json.JSONDecoder().raw_decode(request.system.split('Sources: ', 1)[1])[0]
        if request.observations:
            part = json.loads(request.observations[-1].text)
            seen[part['slot']] = seen.get(part['slot'], '') + part['content']
            if part['next_offset'] is not None:
                chosen = next(item for item in sources if item['slot'] == part['slot'])
                offset = part['next_offset']
            else:
                chosen = next((item for item in sources if item['slot'] not in seen and item['length']), None)
                offset = 0
        else:
            chosen = next(item for item in sources if item['length'])
            offset = 0
        if chosen is None:
            assert len(seen) == 2 and any('BEN_SHORT' in value for value in seen.values())
            return ProviderResult(text='ALL_NESTED_SOURCES_USED')
        call_id = 'nested-recovery-' + chosen['slot'] + '-' + str(offset)
        return ProviderResult(tool_calls=[ToolCall(call_id, 'read_teammate_result',
            {'slot': chosen['slot'], 'digest': chosen['digest'], 'offset': offset})],
            continuation={'call_ids': [call_id]})
    client.app.state.scheduler.provider = recover
    client.app.state.scheduler.schedule_user_event = original_schedule
    from functools import partial
    client.portal.call(partial(original_schedule, room, posted.json()['retry_event_id'], wait=True))
    with client.app.state.session_factory() as db:
        assert any((event.payload or {}).get('content') == 'ALL_NESTED_SOURCES_USED'
                   for event in db.query(RoomEvent).filter_by(room_id=room, kind='message').all())

@pytest.mark.parametrize('kind', ['message', 'batch'])
@pytest.mark.parametrize('invalid', ['none', 'owner', 'root_room', 'missing_event'])
def test_nested_cross_child_source_is_bound_and_missing_fails(kind, invalid):
    from types import SimpleNamespace as NS
    from app.services import teammates
    from app.services.llm_service import LLMFailure
    from app.models.bot_message import BotMessage
    from app.models.bot_batch import BotBatch
    from app.models.teammate import TeammateHandoff
    row = NS(handoff_id='parent', owner_id=11, room_id=22, root_request_event_id='root', recipient_agent_id=33)
    event = NS(event_id='event', room_id=22, actor_type='agent', actor_id=33, seq=9, kind='message', payload={'content':'text'})
    child = NS(owner_id=99 if invalid=='owner' else 11, root_room_id=99 if invalid=='root_room' else 22,
               root_request_event_id='root', sender_room_id=22, sender_agent_id=33,
               parent_job_id='parent', parent_kind='teammate', reply_state='delivered',
               message_id='msg', batch_id='batch', return_event_id=None if invalid=='missing_event' else 'event')
    class Query:
        def __init__(self, model): self.model=model; self.filters={}
        def filter_by(self, **kwargs): self.filters=kwargs; return self
        def all(self):
            values = [child] if (self.model is BotMessage and kind=='message') or (self.model is BotBatch and kind=='batch') else []
            return [item for item in values if all(getattr(item, key)==value for key,value in self.filters.items())]
    class DB:
        def query(self, model): return Query(model)
        def get(self, model, key): return event if model is RoomEvent and key=='event' else None
    if invalid == 'none':
        sources=teammates._nested_sources(DB(), row)
        assert len(sources)==1 and sources[0]['source_type']==kind and sources[0]['event_id']=='event'
    else:
        with pytest.raises(LLMFailure) as failure:
            teammates._nested_sources(DB(), row)
        assert failure.value.code=='teammate_result_changed'
