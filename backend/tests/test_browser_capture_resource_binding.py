"""Screenshot resource proof uses the disposable DB, never an installed browser."""
from dataclasses import replace
from uuid import uuid4

import pytest

from test_resource_authority import resource
from test_review_preview import preview_fixture
from test_room_state import room_client
from app.models.collaboration import RoomGrant
from app.services.resource_authority import resource_live


@pytest.mark.parametrize('mode', ['once', 'room_session'])
def test_screenshot_requires_original_authority_and_grant(preview_fixture, mode):
    _, _, broker, _, ref, epochs = resource(preview_fixture, mode, 'browser_screenshot')
    assert resource_live(broker.sessions, ref, 7, owner_epochs=epochs)
    assert not resource_live(broker.sessions, replace(ref, room_id=ref.room_id + 1), 7, owner_epochs=epochs)
    assert not resource_live(broker.sessions, replace(ref, grant_id=str(uuid4())), 7, owner_epochs=epochs)
    epochs.value = 'b' * 64
    assert not resource_live(broker.sessions, ref, 7, owner_epochs=epochs)
    epochs.value = 'a' * 64
    with broker.sessions() as db:
        db.get(RoomGrant, ref.grant_id).decision = 'revoked'
        db.commit()
    assert not resource_live(broker.sessions, ref, 7, owner_epochs=epochs)


def test_screenshot_delegated_root_proof_and_revocation(room_client):
    from types import SimpleNamespace
    from test_execution_scopes import seed
    from test_review_claim import inspected, locked_claim
    from app.models.collaboration import ToolAction, ProviderConnection
    from app.models.models import Agent
    from app.services import execution_scopes as scopes
    from app.services.action_review import ProposerBinding, observe_authority
    from app.services.review_snapshot import capture_review, stage_initial_review

    client, (owner, _, project, _, _) = room_client
    context = seed(client, owner, project)
    from app.services.tool_broker import ToolBroker
    broker = ToolBroker(client.app.state.session_factory, SimpleNamespace(browser=SimpleNamespace(workflows={})))
    client.app.state.scheduler.broker = broker
    broker.owner_epochs = SimpleNamespace(current=lambda _: 'a' * 64)
    prepared = broker.prepare('browser_screenshot', {'url': 'https://example.com'}, 'owned-workflow')
    proof = context.proof
    with broker.sessions() as db:
        agent = db.get(Agent, proof['agent_id'])
        con = db.get(ProviderConnection, agent.connection_id)
        proposer = ProposerBinding(owner_id=owner, agent_id=agent.id, room_id=proof['execution_room_id'],
            connection_id=con.id, model_id=agent.model_id, config_revision=con.readiness_revision,
            auth_grant_revision=con.auth_grant_revision, provider_id=con.provider_id, protocol_id=con.protocol_id, owner_epoch='a' * 64)
        authority = observe_authority(db, room_id=proof['execution_room_id'], agent_id=agent.id,
            session_id=broker.session_id, proposer=proposer, owner_epochs=broker.owner_epochs)
        action = ToolAction(action_id=str(uuid4()), room_id=proof['execution_room_id'], agent_id=agent.id,
            trigger_event_id=context.root, call_id=str(uuid4()), family=prepared.family,
            arguments=prepared.public_arguments, target=prepared.target, scope_target=prepared.scope_target,
            payload_digest=prepared.payload_digest, expected_effect=prepared.expected_effect,
            session_id=broker.session_id, workflow_id='owned-workflow', state='waiting', execution_binding=proof)
        scopes.claim_proposal(db, action)
        stage_initial_review(action, capture_review(action, prepared, authority, broker.executor))
        db.add(action); db.commit(); db.refresh(action); db.expunge(action)
    broker.retain(action.action_id, prepared, proposer=proposer)
    inspected(client, owner, broker, action)
    ref = locked_claim(broker, action)
    assert ref is not None
    from app.services.grants import now
    with broker.sessions() as db:
        saved = db.get(ToolAction, action.action_id)
        saved.state = 'completed'; saved.started_at = now(); saved.completed_at = now(); db.commit()
    assert resource_live(broker.sessions, ref, context.gen, owner_epochs=broker.owner_epochs)
    altered = dict(proof, root_event_id=str(uuid4()))
    assert not resource_live(broker.sessions, replace(ref, execution_binding=altered), context.gen, owner_epochs=broker.owner_epochs)
    with broker.sessions() as db:
        scopes.revoke_room(db, context.room); db.commit()
    assert not resource_live(broker.sessions, ref, context.gen, owner_epochs=broker.owner_epochs)
