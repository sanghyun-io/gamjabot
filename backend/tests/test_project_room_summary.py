"""Owner project activity summaries on disposable SQLite; no provider requests."""
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sql_event

from app.api.routes.auth import create_access_token
from app.api.routes.projects import _project_room_summaries
from app.main import create_app
from app.models.collaboration import RoomEvent
from app.models.models import Conversation, Project, User


def day(n):
    return datetime(2026, 10, n, 12, 0, 0)


@pytest.fixture
def summary_client(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'summary-synthetic-token')
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            owner = User(email='summary@test.invalid', username='summary', hashed_password='x')
            other = User(email='other-summary@test.invalid', username='other-summary', hashed_password='x')
            db.add_all([owner, other]); db.flush()
            projects = [Project(user_id=owner.id, name=n, is_active=n != 'Archived', updated_at=day(20))
                        for n in ('Mixed', 'No events', 'Empty', 'Archived')]
            foreign = Project(user_id=other.id, name='Foreign project')
            db.add_all([*projects, foreign]); db.flush()
            rooms = [Conversation(user_id=owner.id, project_id=projects[0].id, title='Active', created_at=day(1), is_active=True),
                     Conversation(user_id=owner.id, project_id=projects[0].id, title='Closed', created_at=day(1), is_active=False),
                     Conversation(user_id=owner.id, project_id=projects[1].id, title='No events', created_at=day(5)),
                     Conversation(user_id=owner.id, project_id=projects[3].id, title='Archived room', created_at=day(4), is_active=False),
                     Conversation(user_id=other.id, project_id=foreign.id, title='Foreign private room', created_at=day(22)),
                     Conversation(user_id=other.id, project_id=projects[0].id, title='Inconsistent foreign room', created_at=day(23))]
            db.add_all(rooms); db.flush()
            for index, seq, at in [(0, 1, 10), (0, 2, 2), (1, 1, 3), (4, 1, 24), (5, 1, 25)]:
                db.add(RoomEvent(event_id=str(uuid4()), room_id=rooms[index].id, seq=seq,
                                 actor_type='system', actor_name_snapshot='System', kind='activity',
                                 payload={'private': 'never include event content'}, created_at=day(at)))
            db.commit()
            ids = {'owner': owner.id, 'other': other.id,
                   **{p.name: p.id for p in projects}, 'foreign': foreign.id}
        yield client, ids


def headers(client, owner):
    return {'X-Gamja-Session': client.app.state.local_session_token,
            'Authorization': 'Bearer '+create_access_token({'sub': owner})}


def listing(client, owner, status='active'):
    response = client.get('/api/v1/projects/', params={'status': status}, headers=headers(client, owner))
    assert response.status_code == 200
    return {r['name']: r for r in response.json()}


def test_activity_uses_latest_sequence_per_room_closed_rooms_and_no_event_creation(summary_client):
    client, ids = summary_client
    rows = listing(client, ids['owner'])
    assert rows['Mixed']['room_summary'] == {'total_rooms': 2, 'active_rooms': 1,
                                            'latest_activity_at': day(3).isoformat()}
    # A deliberately later seq1 clock and project metadata timestamp are not latest room activity.
    assert rows['No events']['room_summary'] == {'total_rooms': 1, 'active_rooms': 1,
                                                'latest_activity_at': day(5).isoformat()}
    assert rows['Empty']['room_summary'] == {'total_rooms': 0, 'active_rooms': 0,
                                            'latest_activity_at': None}


def test_archived_all_and_empty_owner_lists_preserve_existing_filters(summary_client):
    client, ids = summary_client
    rows = listing(client, ids['owner'], 'archived')
    assert set(rows) == {'Archived'}
    assert rows['Archived']['room_summary'] == {'total_rooms': 1, 'active_rooms': 0,
                                               'latest_activity_at': day(4).isoformat()}
    assert len(listing(client, ids['owner'], 'all')) == 4
    with client.app.state.session_factory() as db:
        empty = User(email='empty-summary@test.invalid', username='empty-summary', hashed_password='x')
        db.add(empty); db.commit(); owner = empty.id
    assert listing(client, owner) == {}


def test_room_and_project_ownership_are_both_required_and_payload_is_not_exported(summary_client):
    client, ids = summary_client
    own = listing(client, ids['owner'])
    assert 'Foreign project' not in own and own['Mixed']['room_summary']['total_rooms'] == 2
    assert set(listing(client, ids['other'])) == {'Foreign project'}
    with client.app.state.session_factory() as db:
        assert _project_room_summaries(db, ids['other'], [ids['Mixed']]) == {}
        assert _project_room_summaries(db, ids['owner'], [ids['foreign']]) == {}
    response = client.get('/api/v1/projects/', headers=headers(client, ids['owner']))
    assert 'never include event content' not in response.text
    assert 'Foreign private room' not in response.text and 'Inconsistent foreign room' not in response.text
    assert client.get('/api/v1/projects/').status_code in {401, 403}


def test_summary_is_one_read_query_for_many_projects_and_does_not_write(summary_client):
    client, ids = summary_client
    with client.app.state.session_factory() as db:
        before = [(r.event_id, r.seq, r.payload, r.created_at) for r in db.query(RoomEvent).order_by(RoomEvent.event_id)]
        many = [Project(user_id=ids['owner'], name='Synthetic '+str(i)) for i in range(25)]
        db.add_all(many); db.commit()
        statements = []
        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)
        connection = db.connection()
        sql_event.listen(connection, 'before_cursor_execute', capture)
        try:
            result = _project_room_summaries(db, ids['owner'], [ids['Mixed'], ids['Empty'], *[p.id for p in many]])
        finally:
            sql_event.remove(connection, 'before_cursor_execute', capture)
        # Expired ORM identity loads above are separate existing session reads; the aggregate itself is one SELECT.
        aggregates = [s for s in statements if 'GROUP BY' in s.upper()]
        assert len(aggregates) == 1 and all(s.lstrip().upper().startswith('SELECT') for s in statements)
        assert result[ids['Mixed']]['total_rooms'] == 2 and ids['Empty'] not in result
        assert before == [(r.event_id, r.seq, r.payload, r.created_at) for r in db.query(RoomEvent).order_by(RoomEvent.event_id)]
        assert _project_room_summaries(db, ids['owner'], []) == {}


def test_existing_project_detail_accepts_optional_uncomputed_summary(summary_client):
    client, ids = summary_client
    response = client.get(f"/api/v1/projects/{ids['Mixed']}", headers=headers(client, ids['owner']))
    assert response.status_code == 200 and response.json()['room_summary'] is None
    assert response.json()['name'] == 'Mixed' and response.json()['agents'] == []
