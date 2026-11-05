import asyncio
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from app.services.tools.browser import BrowserExecutor, origin


def png():
    out = BytesIO()
    Image.new('RGB', (2, 3), 'red').save(out, format='PNG')
    return out.getvalue()


class Page:
    url = 'https://example.com/'
    def __init__(self):
        self.calls = 0
        self.after = lambda: None
    async def screenshot(self, **kwargs):
        self.calls += 1
        self.after()
        return png()
    async def goto(self, url, **kwargs):
        self.url = url
        return SimpleNamespace(status=200)


@pytest.mark.asyncio
async def test_capture_stores_only_room_owned_png_and_denies_changed_authority(tmp_path):
    executor = BrowserExecutor(app_data=tmp_path)
    page = Page()
    live = [True]
    session = SimpleNamespace(lock=asyncio.Lock(), controller='agent', closing=False, page=page,
        authority=SimpleNamespace(active=lambda: live[0], allows=lambda value: value == origin('https://example.com/')),
        proxy=SimpleNamespace(blocked=0))
    action = SimpleNamespace(family='browser_screenshot', arguments={'url':page.url}, room_id=7, action_id=str(uuid4()))
    cancel = asyncio.Event()
    result = await executor._perform(session, action, cancel, origin(page.url))
    assert result.state == 'completed' and (result.image_width, result.image_height) == (2, 3)
    assert (tmp_path / result.screenshot_ref).read_bytes() == result.image_bytes
    assert not (tmp_path / 'rooms/8').exists()
    action.action_id = str(uuid4())
    page.after = lambda: live.__setitem__(0, False)
    result = await executor._perform(session, action, cancel, origin(page.url))
    assert result.state == 'cancelled'
    assert not (tmp_path / 'rooms/7/screenshots' / (action.action_id + '.png')).exists()


@pytest.mark.asyncio
async def test_owner_control_cross_origin_and_cancel_never_capture(tmp_path):
    executor = BrowserExecutor(app_data=tmp_path)
    page = Page()
    session = SimpleNamespace(lock=asyncio.Lock(), controller='user', closing=False, page=page,
        authority=SimpleNamespace(active=lambda: True, allows=lambda value: True), proxy=SimpleNamespace(blocked=0))
    action = SimpleNamespace(family='browser_screenshot', arguments={'url':page.url}, room_id=7, action_id=str(uuid4()))
    cancel = asyncio.Event()
    assert (await executor._perform(session, action, cancel, origin(page.url))).state == 'denied'
    session.controller = 'agent'
    assert (await executor._perform(session, action, cancel, origin('https://other.example/'))).state == 'failed'
    cancel.set()
    assert (await executor._perform(session, action, cancel, origin(page.url))).state == 'uncertain'
    assert page.calls == 0
