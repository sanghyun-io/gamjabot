"""Synthetic owner-scoped library search and keyset contract."""
from datetime import datetime, timezone
from uuid import uuid4

from app.models.models import Conversation, Project
from app.models.collaboration import RoomEvent
from app.models.room_result import RoomResult, RoomResultVersion
from test_room_results import results, headers


def test_title_search_covers_all_current_versions_and_binds_cursor(results):
    client, ids = results
    now = datetime.now(timezone.utc)
    with client.app.state.session_factory() as db:
        def create(title, *, kind='page', bot=None, room=None, previous=None):
            room = room or ids['room'];bot = bot or ids['author'];identifier=str(uuid4())
            source = ids['answer'] if room==ids['room'] else None
            if source is None:
                source=str(uuid4());db.add(RoomEvent(event_id=source,room_id=room,seq=1,actor_type='agent',actor_id=bot,
                    actor_name_snapshot='Synthetic',kind='message',payload={'content':'fixture'},created_at=now));db.flush()
            root=RoomResult(result_id=identifier,owner_id=ids['owner'],room_id=room,current_version=2 if previous else 1,created_at=now,updated_at=now)
            db.add(root);db.flush()
            for number,name in ([(1,previous),(2,title)] if previous else [(1,title)]):
                db.add(RoomResultVersion(result_id=identifier,version=number,room_id=room,kind=kind,title=name,
                    source_event_id=source,source_seq=2 if room==ids['room'] else 1,selector=f'fixture:{identifier}:{number}',
                    author_agent_id=bot,author_name_snapshot='Synthetic',execution={},content='fixture',file_ref=None,evidence=[],
                    state='draft',state_revision=1,link_report='unchecked',created_at=now))
            return identifier
        expected={create(f'Page {i:02d}',kind='text' if i==0 else 'page') for i in range(55)}
        literal=create('Marker 100%_ok \\ exact')
        korean=create('보고서 최신')
        create('current unrelated',previous='Page obsolete')
        create('Page other bot',bot=ids['reviewer'])
        create('Page archived',room=ids['second'])
        db.get(Conversation,ids['second']).is_active=False
        closed_project=Project(user_id=ids['owner'],name='Closed',is_active=False);db.add(closed_project);db.flush()
        closed_room=Conversation(user_id=ids['owner'],project_id=closed_project.id,title='Unavailable');db.add(closed_room);db.flush()
        create('Page closed project',room=closed_room.id)
        db.commit()
    path=f"/api/v1/bots/{ids['author']}/library"
    first=client.get(path,params={'q':'pAgE','limit':50},headers=headers(client,ids));assert first.status_code==200,first.text
    one=first.json();assert len(one['items'])==50 and one['next_cursor'] and '~' in one['next_cursor']
    second=client.get(path,params={'q':'pAgE','limit':50,'after':one['next_cursor']},headers=headers(client,ids));assert second.status_code==200,second.text
    two=second.json();assert two['next_cursor'] is None
    assert {item['result_id'] for item in one['items']+two['items']}==expected
    assert {item['kind'] for item in one['items']+two['items']}=={'page','text'}
    for query in ('Page','different'):
        bad=client.get(path,params={'q':query,'after':one['next_cursor']},headers=headers(client,ids))
        assert bad.status_code==422 and bad.json()['detail']['code']=='result_library_search_cursor_invalid'
    bad=client.get(f"/api/v1/bots/{ids['reviewer']}/library",params={'q':'pAgE','after':one['next_cursor']},headers=headers(client,ids))
    assert bad.status_code==422
    assert client.get(path,params={'q':'pAgE'},headers=headers(client,ids,owner=ids['other'])).status_code==404
    for query,identifier in (('%_ok',literal),('\\ exact',literal),('보고서',korean)):
        value=client.get(path,params={'q':query},headers=headers(client,ids));assert value.status_code==200
        assert [item['result_id'] for item in value.json()['items']]==[identifier]
    assert client.get(path,params={'q':'x'*101},headers=headers(client,ids)).status_code==422
    assert client.get(path,params={'q':'line\nbreak'},headers=headers(client,ids)).status_code==422
    unfiltered=client.get(path,params={'limit':2},headers=headers(client,ids));assert unfiltered.status_code==200
    assert '~' not in unfiltered.json()['next_cursor']
    assert client.get(path,params={'after':unfiltered.json()['next_cursor'],'limit':2},headers=headers(client,ids)).status_code==200
