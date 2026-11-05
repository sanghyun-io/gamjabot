"""Activity wire provenance must agree with the actual scoped preview; fake DB only."""
from uuid import uuid4
from test_room_state import room_client,headers
from test_execution_scopes import seed,proposal,FakeExecutor
from app.models.collaboration import ToolAction,RoomGrant
from app.models.execution_scope import ExecutionScope
from app.services.tool_broker import ToolBroker
from app.services import execution_scopes as scopes

FIELDS=('execution_binding','root_ref','scope_status','revocation_state')


def scoped_action(client,owner,project,tmp_path):
    context=seed(client,owner,project)
    fake=FakeExecutor();broker=ToolBroker(client.app.state.session_factory,fake)
    client.app.state.scheduler.broker=broker
    path=tmp_path/'activity-probe-only.txt';path.write_text('Task-owned synthetic review target; never executed.')
    action=proposal(client,owner,context,path,broker)
    return context,action,fake


def test_activity_matches_real_v2_preview_root_projection_and_legacy_owner_boundaries(room_client,tmp_path):
    client,(owner,other,project,_,_)=room_client
    context,action,fake=scoped_action(client,owner,project,tmp_path)
    with client.app.state.session_factory() as db:
        legacy=ToolAction(action_id=str(uuid4()),room_id=action.room_id,agent_id=action.agent_id,
            family='read_file',arguments={'path':'legacy-history'},state='completed')
        db.add(legacy);db.commit();legacy_id=legacy.action_id
    path=f'/api/v1/tools/rooms/{action.room_id}/activity'
    response=client.get(path,headers=headers(client,owner))
    assert response.status_code==200,response.text
    rows=response.json();row=next(a for a in rows if a['action_id']==action.action_id)
    preview=client.get(f'/api/v1/tools/rooms/{action.room_id}/actions/{action.action_id}/preview',headers=headers(client,owner))
    assert preview.status_code==200,preview.text
    preview=preview.json()
    projection=client.get(f'/api/v1/bots/messages/{context.message_id}/actions',headers=headers(client,owner)).json()
    root_row=next(a for a in projection['actions'] if a['action_id']==action.action_id)
    assert {key:row[key] for key in FIELDS}=={key:preview[key] for key in FIELDS}=={key:root_row[key] for key in FIELDS}
    assert preview['review']['version']==2 and preview['approval_policy']['scopes']==['once']
    assert preview['review']['action']['execution_binding']==row['execution_binding']==context.proof
    assert row['room_id']==context.personal[context.ben] and row['agent_id']==context.ben
    assert row['root_ref']['room_id']==context.room and row['root_ref']['event_id']==context.root
    assert row['root_ref']['actor_type']=='user' and row['root_ref']['actor_id']==owner
    assert row['execution_binding']['context_mode']=='delegated' and row['scope_status']=='active'
    assert row['execution_binding']['deadline_at']==context.proof['deadline_at']
    assert row['trigger_event_id']==context.root and row['review_id']==preview['review_id']
    old=next(a for a in rows if a['action_id']==legacy_id)
    assert all(old[key] is None for key in FIELDS)
    assert old['state']=='completed' and old['review_status']=='unavailable' and old['arguments']=={'path':'legacy-history'}
    assert client.get(path,headers=headers(client,other)).status_code==404
    assert client.get(f'/api/v1/tools/rooms/{context.room}/activity',headers=headers(client,owner)).json()==[]
    assert fake.effects==[]
    with client.app.state.session_factory() as db:assert db.query(RoomGrant).count()==0


def test_revoked_scope_stays_visible_but_stale_review_cannot_be_approved(room_client,tmp_path):
    client,(owner,_,project,_,_)=room_client
    context,action,fake=scoped_action(client,owner,project,tmp_path)
    with client.app.state.session_factory() as db:
        scopes.revoke_room(db,context.room);db.commit()
    path=f'/api/v1/tools/rooms/{action.room_id}/activity'
    row=next(a for a in client.get(path,headers=headers(client,owner)).json() if a['action_id']==action.action_id)
    assert row['execution_binding']==context.proof
    assert row['scope_status']=='revoked' and row['revocation_state']=='pending'
    assert row['root_ref']['event_id']==context.root
    assert client.get(f'/api/v1/tools/rooms/{action.room_id}/actions/{action.action_id}/preview',headers=headers(client,owner)).status_code==409
    decision=client.post(f'/api/v1/tools/rooms/{action.room_id}/actions/{action.action_id}/decision',
        headers=headers(client,owner),json={'decision':'approve','scope':'once','review_id':action.review_id})
    assert decision.status_code==409,decision.text
    assert fake.effects==[]
    with client.app.state.session_factory() as db:
        assert db.query(RoomGrant).count()==0
        leaf=db.get(ExecutionScope,context.proof['scope_id']);leaf.revocation_state='unknown';db.commit()
    row=next(a for a in client.get(path,headers=headers(client,owner)).json() if a['action_id']==action.action_id)
    assert row['scope_status']=='revoked' and row['revocation_state']=='unknown'
