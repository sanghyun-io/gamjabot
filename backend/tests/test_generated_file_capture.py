"""Approved disposable writes and generated-copy settlement; no real accounts."""
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4
import pytest
from test_review_preview import preview_fixture
from test_room_state import room_client
from test_review_claim import inspected
from test_file_review_finish import outcome
from app.models.collaboration import RoomEvent, ToolAction
from app.services.generated_files import GeneratedFiles
from app.services.tools.files import execute_file


def make_case(preview_fixture, tmp_path, executor=execute_file):
    client, owner, broker, path, proposal = preview_fixture
    action = proposal('write_file', {'path': str(path), 'content': 'approved payload'}, executor)
    broker.executor = SimpleNamespace(generated_files=GeneratedFiles(tmp_path), __call__=executor)
    # Special methods are resolved on the class, not instance.
    class Executor:
        generated_files = broker.executor.generated_files
        async def __call__(self, *args): return await executor(*args)
    broker.executor = Executor()
    inspected(client, owner, broker, action, 'once')
    return owner, broker, path, action

def test_verified_write_and_idempotent_result(preview_fixture, tmp_path):
    owner, broker, path, action = make_case(preview_fixture, tmp_path)
    assert outcome(broker, action).state == 'completed'
    with broker.sessions() as db:
        saved = db.get(ToolAction, action.action_id)
        terminal = db.query(RoomEvent).filter_by(idempotency_key=f'tool-result:{action.action_id}').one()
        public = saved.observation['generated_file']
        assert terminal.payload['generated_file'] == public
    path.write_text('changed outside')
    assert broker.executor.generated_files.read(owner, action.room_id, action.action_id, public) == b'approved payload'
    original = broker.executor.generated_files.capture
    def forbidden(*args): raise AssertionError('prior result must not recapture')
    broker.executor.generated_files.capture = forbidden
    assert broker._finish_file(action.action_id, 'completed', '', None, None, 'normal', {}).payload['generated_file'] == public
    broker.executor.generated_files.capture = original

def test_unconfirmed_postcondition_never_archives(preview_fixture, tmp_path):
    async def bad(action, ref, cancel):
        result = await execute_file(action, ref, cancel)
        return replace(result, file_postconditions=result.file_postconditions.model_copy(update={'action_id': str(uuid4())}))
    owner, broker, path, action = make_case(preview_fixture, tmp_path, bad)
    assert outcome(broker, action).state == 'uncertain'
    assert not broker.executor.generated_files.root.exists()

def test_rollback_discards_new_capture(preview_fixture, tmp_path, monkeypatch):
    import app.services.tool_broker as module
    owner, broker, path, action = make_case(preview_fixture, tmp_path)
    stage = module.stage_event
    def fail(db, room, actor, kind, payload, key, **kwargs):
        if key == f'file-review-result:{action.action_id}': raise RuntimeError('fixture')
        return stage(db, room, actor, kind, payload, key, **kwargs)
    monkeypatch.setattr(module, 'stage_event', fail)
    assert outcome(broker, action).state == 'uncertain'
    assert not (broker.executor.generated_files.root / str(action.room_id) / (action.action_id + '.json')).exists()

@pytest.mark.asyncio
async def test_private_archive_and_ancestors_are_denied(tmp_path):
    from app.services.tools import LocalExecutor
    executor = LocalExecutor(database_path=str(tmp_path / 'app.sqlite'))
    try:
        for target in (tmp_path, executor.generated_files.root, executor.generated_files.root / 'child'):
            action = SimpleNamespace(family='read_file', arguments={'path': str(target)})
            result = await executor._execute(action, None, None)
            assert result.state == 'denied'
    finally:
        await executor.close()
