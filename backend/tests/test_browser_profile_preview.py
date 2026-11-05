"""Synthetic route checks for owned previews across async capture waits."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routes import tools


class Page:
    def __init__(self):
        self.captures = 0
        self.after_capture = lambda: None

    def is_closed(self):
        return False

    async def screenshot(self, **options):
        assert options == dict(type='jpeg', quality=60, full_page=False, timeout=5000)
        self.captures += 1
        self.after_capture()
        return b'synthetic-jpeg'


@pytest.fixture
def fixture(monkeypatch):
    state = SimpleNamespace(epoch='owner-epoch', revision=3)
    page = Page()
    session = SimpleNamespace(page=page, context_id='context-A', controller='agent',
                              closing=False, lock=asyncio.Lock(), authority=SimpleNamespace(active=lambda: True))
    state.session = session

    def read(profile_id, owner_id, room_id):
        assert (profile_id, owner_id, room_id) == ('profile-A', 1, 2)
        return {'revision': state.revision}

    store = SimpleNamespace(read=read, session=lambda _: ('key-A', state.session),
                            browser=SimpleNamespace(owner_epochs=SimpleNamespace(current=lambda _: state.epoch)))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        local_executor=SimpleNamespace(browser=SimpleNamespace(profiles=store)))))
    monkeypatch.setattr(tools.room_service, 'require_room', lambda *args, **kwargs: None)
    db, owner = SimpleNamespace(rollback=lambda: None), SimpleNamespace(id=1)

    async def capture(revision=3):
        return await tools.browser_profile_preview(2, 'profile-A', request, db, owner, revision)

    return state, session, page, capture


@pytest.mark.asyncio
async def test_preview_identifies_the_exact_profile_revision_and_context(fixture):
    _, _, page, capture = fixture
    response = await capture()
    assert response.body == b'synthetic-jpeg' and page.captures == 1
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-browser-profile-id'] == 'profile-A'
    assert response.headers['x-browser-profile-revision'] == '3'
    assert response.headers['x-browser-context-id'] == 'context-A'
    assert response.headers['x-captured-at']


@pytest.mark.asyncio
async def test_stale_revision_never_captures_pixels(fixture):
    _, _, page, capture = fixture
    with pytest.raises(HTTPException) as error:
        await capture(revision=2)
    assert error.value.detail['code'] == 'browser_profile_changed'
    assert page.captures == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['epoch', 'session'])
async def test_waiting_for_capture_lock_does_not_keep_stale_authority(fixture, change):
    state, session, page, capture = fixture
    await session.lock.acquire()
    task = asyncio.create_task(capture())
    await asyncio.sleep(0)
    if change == 'epoch':
        state.epoch = 'replacement-epoch'
    else:
        state.session = SimpleNamespace(context_id='replacement-context')
    session.lock.release()
    with pytest.raises(HTTPException):
        await task
    assert page.captures == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['epoch', 'revision', 'controller'])
async def test_capture_result_is_not_returned_after_authority_or_target_changes(fixture, change):
    state, session, page, capture = fixture

    def changed():
        if change == 'epoch':
            state.epoch = 'replacement-epoch'
        elif change == 'revision':
            state.revision += 1
        else:
            session.controller = 'user'

    page.after_capture = changed
    with pytest.raises(HTTPException):
        await capture()
    assert page.captures == 1
