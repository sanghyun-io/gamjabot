"""Python await-boundary fixtures, not Playwright renderer-start proof."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_room_state import room_client
from test_review_preview import preview_fixture
from test_review_claim import inspected, locked_claim
from app.models.collaboration import ProviderConnection, RoomAgentRun, RoomGrant, ToolAction
from app.services.action_review import ReviewChanged
from app.services.grants import now
from app.services.tools.browser import BrowserExecutor
from app.services.tools.browser_start import BrowserStart


class OwnedPage:
    url = "http://127.0.0.1:12345/old"
    def __init__(self):
        self.effects = []
        self.after_navigation = self.after_effect = None
        self.closed = False
    def is_closed(self): return self.closed
    async def goto(self, url, **kwargs):
        await asyncio.sleep(0)
        self.url = url
        if self.after_navigation: self.after_navigation()
        return SimpleNamespace(status=200)
    def locator(self, selector): return self
    @property
    def first(self): return self
    async def click(self):
        self.effects.append("click")
        if self.after_effect: self.after_effect()
    async def fill(self, text):
        self.effects.append(("fill", text))
        if self.after_effect: self.after_effect()
    async def evaluate(self, source):
        self.effects.append("submit")
        if self.after_effect: self.after_effect()
    async def wait_for_timeout(self, _): await asyncio.sleep(0)
    async def title(self): return "owned fixture"
    async def inner_text(self, **kwargs): return "owned fixture"
    async def evaluate_all(self, source): return []


async def noop(): pass


def pending(preview_fixture, *, absent=False, operation="click"):
    client, owner, broker, path, proposal = preview_fixture
    browser = BrowserExecutor(sessions=broker.sessions, owner_epochs=broker.owner_epochs)
    first = proposal("browser_navigate", {"url":"http://127.0.0.1:12345/"},
                     SimpleNamespace(browser=browser), "owned-browser")
    page = OwnedPage()
    context = SimpleNamespace(close=noop)
    session = SimpleNamespace(page=page, context=context, context_id="c"*32, profile_id=None, profile_lease=None, controller="agent", closing=False, close_retry_at=0, execution_binding=None,
        room_id=first.room_id, agent_id=first.agent_id, workflow_id=first.workflow_id,
        deadline=time.monotonic()+300, lock=asyncio.Lock(), profile=str(path.parent/"no-profile"),
        playwright=SimpleNamespace(stop=noop),
        proxy=SimpleNamespace(blocked=0, close=noop),
        authority=SimpleNamespace(active=lambda:True, _live=lambda _:True,
                                  add=lambda *_:None, allows=lambda _:True))
    key = (first.room_id, first.agent_id, first.workflow_id)
    if not absent: browser.workflows[key] = session
    args = {"url":"http://127.0.0.1:12345/", "selector":"#owned", "operation":operation}
    if operation == "fill": args["text"] = "owned text"
    action = proposal("browser_interact", args, SimpleNamespace(browser=browser), first.workflow_id)
    with broker.sessions() as db:
        run = db.get(RoomAgentRun,(action.room_id,action.agent_id))
        run.generation=9;run.trigger_event_id=action.trigger_event_id;run.state="idle";run.paused=False
        db.commit()
    inspected(client, owner, broker, action)
    ref = locked_claim(broker, action)
    with broker.sessions() as db:
        row=db.get(ToolAction,action.action_id);row.state="started";row.started_at=now();db.commit()
    action.arguments=broker._previews[action.action_id].raw_arguments
    return broker, browser, key, session, action, ref


@pytest.mark.parametrize("change", ["missing","payload","review","euid","context","page","mapping","connection","completed","scope","expired"])
def test_original_browser_review_context_authority_and_time_are_not_rebased(preview_fixture, change):
    broker,browser,key,session,action,ref=pending(preview_fixture)
    start=BrowserStart(browser,key,action,ref,session)
    if change=="missing":start.ref=replace(ref,review=None)
    elif change=="payload":action.arguments["selector"]="#other"
    elif change=="review":action.review_id=str(uuid4())
    elif change=="euid":start.ref=replace(ref,review=ref.review.model_copy(update={"authority":ref.review.authority.model_copy(update={"euid":ref.review.authority.euid+1})}))
    elif change=="context":session.context_id="d"*32
    elif change=="page":session.page=OwnedPage()
    elif change=="mapping":browser.workflows[key]=SimpleNamespace()
    elif change=="scope":start.ref=replace(ref,scope="broad")
    elif change=="expired":start.ref=replace(ref,approved_at=now()-timedelta(seconds=60))
    else:
        with broker.sessions() as db:
            if change=="connection":db.get(ProviderConnection,ref.review.authority.proposer.connection_id).readiness_revision+=1
            else:db.get(ToolAction,action.action_id).state="completed"
            db.commit()
    with pytest.raises(ReviewChanged):start.check(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["lock","navigation","create"])
async def test_rechecks_after_await_without_mutation_or_automatic_retry(preview_fixture, monkeypatch, boundary):
    broker,browser,key,session,action,ref=pending(preview_fixture,absent=boundary=="create")
    def changed():
        with broker.sessions() as db:
            db.get(ProviderConnection,ref.review.authority.proposer.connection_id).readiness_revision+=1;db.commit()
    async def create(*_):
        await asyncio.sleep(0);changed();return session
    if boundary=="create":monkeypatch.setattr(browser,"_create",create)
    elif boundary=="navigation":session.page.after_navigation=changed
    else:await session.lock.acquire()
    try:
        task=asyncio.create_task(browser(action,ref,asyncio.Event()))
        if boundary=="lock":
            await asyncio.sleep(0);await asyncio.sleep(0);changed();session.lock.release()
        result=await task
        assert result.state=="failed" and result.error_code=="review_changed"
        assert session.page.effects==[]
        if boundary=="create":assert key not in browser.workflows
        with broker.sessions() as db:
            assert db.get(ToolAction,action.action_id).state=="started"
            assert db.get(RoomGrant,ref.grant_id).decision=="consumed"
    finally:await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["click","fill","submit"])
@pytest.mark.parametrize("absent", [False,True])
async def test_owned_verified_or_reviewed_absent_context_keeps_original_operation(preview_fixture, monkeypatch, operation, absent):
    _,browser,key,session,action,ref=pending(preview_fixture,absent=absent,operation=operation)
    async def create(*_):await asyncio.sleep(0);return session
    monkeypatch.setattr(browser,"_create",create)
    try:
        result=await browser(action,ref,asyncio.Event())
        assert result.state=="completed" and len(session.page.effects)==1
        assert session.page.effects[0]==(("fill","owned text") if operation=="fill" else operation)
    finally:await browser.close()


@pytest.mark.asyncio
async def test_missing_verified_context_is_not_silently_recreated(preview_fixture,monkeypatch):
    _,browser,key,session,action,ref=pending(preview_fixture)
    browser.workflows.pop(key)
    async def forbidden(*_):raise AssertionError("No replacement browser")
    monkeypatch.setattr(browser,"_create",forbidden)
    result=await browser(action,ref,asyncio.Event())
    assert result.error_code=="review_changed" and session.page.effects==[]


@pytest.mark.asyncio
async def test_elapsed_time_after_effect_does_not_reclassify_or_repeat_started_action(preview_fixture,monkeypatch):
    _,browser,_,session,action,ref=pending(preview_fixture)
    clock=ref.approved_at+timedelta(seconds=59)
    monkeypatch.setattr("app.services.tools.browser_start.now",lambda:clock)
    def effect():
        nonlocal clock
        clock+=timedelta(seconds=2)
    session.page.after_effect=effect
    try:
        result=await browser(action,ref,asyncio.Event())
        assert result.state=="completed" and session.page.effects==["click"]
    finally:await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["lock","navigation"])
async def test_original_deadline_is_checked_after_wait_not_restarted(preview_fixture,monkeypatch,boundary):
    _,browser,_,session,action,ref=pending(preview_fixture)
    clock=ref.approved_at+timedelta(seconds=59)
    monkeypatch.setattr("app.services.tools.browser_start.now",lambda:clock)
    def expired():
        nonlocal clock
        clock+=timedelta(seconds=1)
    if boundary=="lock":await session.lock.acquire()
    else:session.page.after_navigation=expired
    try:
        task=asyncio.create_task(browser(action,ref,asyncio.Event()))
        if boundary=="lock":
            await asyncio.sleep(0);await asyncio.sleep(0);expired();session.lock.release()
        result=await task
        assert result.error_code=="review_changed" and session.page.effects==[]
    finally:await browser.close()


@pytest.mark.asyncio
async def test_bundled_playwright_preserves_fill_trusted_click_disabled_and_readonly():
    # Real owned bundled browser/local HTTP; synthetic direct reviews only.
    # No guard inside Playwright's later auto-wait is claimed by this test.
    from pathlib import Path
    from test_tool_executors import LocalExecutor, action
    from app.services.grants import GrantRef
    body=b'''<body><input id="editable"><input id="readonly" readonly value="kept">
        <button id="button" disabled>Owned</button><script>
        window.clicks=[];document.querySelector('#button').addEventListener('click',e=>clicks.push(e.isTrusted));
        </script></body>'''
    async def serve(reader,writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "+str(len(body)).encode()+b"\r\nConnection: close\r\n\r\n"+body)
            await writer.drain()
        finally:writer.close()
    server=await asyncio.start_server(serve,"127.0.0.1",0)
    url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
    executor=LocalExecutor(browser_engine="bundled");profile=None
    async def interact(selector,operation,**extra):
        return await executor(action("browser_interact",url=url,selector=selector,operation=operation,**extra),GrantRef(),asyncio.Event())
    try:
        assert (await executor(action("browser_navigate",url=url),GrantRef(),asyncio.Event())).state=="completed"
        session=next(iter(executor.browser.workflows.values()));profile=Path(session.profile)
        session.page.set_default_timeout(120)
        assert (await interact("#editable","fill",text="owned text")).state=="completed"
        assert await session.page.locator("#editable").input_value()=="owned text"
        assert (await interact("#readonly","fill",text="changed")).state=="failed"
        assert await session.page.locator("#readonly").input_value()=="kept"
        assert (await interact("#button","click")).state=="failed"
        assert await session.page.evaluate("clicks")==[]
        await session.page.evaluate("document.querySelector('#button').disabled=false")
        assert (await interact("#button","click")).state=="completed"
        assert await session.page.evaluate("clicks")==[True]
    finally:
        await executor.close();server.close();await server.wait_closed()
    assert profile is not None and not profile.exists()


@pytest.mark.asyncio
async def test_displaced_fresh_context_is_disposed_without_closing_replacement(monkeypatch,tmp_path):
    # Two direct synthetic actions reviewed absence before either created a
    # context. This reproduces owned cleanup, not broker duplicate execution.
    from test_process_review import reviewed_process
    from app.services.grants import GrantRef
    browser=BrowserExecutor()
    request=SimpleNamespace(family="browser_interact",room_id=1,agent_id=2,workflow_id="race",
        arguments={"url":"http://127.0.0.1:12345/","selector":"#owned","operation":"click"})
    first,ref1=reviewed_process(request,GrantRef(),executor=SimpleNamespace(browser=browser))
    second,ref2=reviewed_process(request,GrantRef(),executor=SimpleNamespace(browser=browser))
    ref1=replace(ref1,approved_at=now());ref2=replace(ref2,approved_at=now())
    key=(1,2,"race");sessions=[];created=asyncio.Event()
    async def create(*_):
        page=OwnedPage();context=SimpleNamespace(closed=False)
        async def close():context.closed=True
        context.close=close
        session=SimpleNamespace(page=page,context=context,context_id=uuid4().hex,profile_id=None,profile_lease=None,controller="agent",closing=False,close_retry_at=0,execution_binding=None,
            room_id=1,agent_id=2,workflow_id="race",deadline=time.monotonic()+300,
            lock=asyncio.Lock(),profile=str(tmp_path/uuid4().hex),playwright=SimpleNamespace(stop=noop),
            proxy=SimpleNamespace(blocked=0,close=noop),
            authority=SimpleNamespace(active=lambda:True,_live=lambda _:True,add=lambda *_:None,allows=lambda _:True))
        sessions.append(session)
        if len(sessions)==1:await session.lock.acquire()
        created.set();return session
    monkeypatch.setattr(browser,"_create",create)
    # Ensure both calls observed absence before creation finishes.
    release_create=asyncio.Event()
    async def delayed_create(*args):await release_create.wait();return await create(*args)
    monkeypatch.setattr(browser,"_create",delayed_create)
    task1=asyncio.create_task(browser(first,ref1,asyncio.Event()))
    task2=asyncio.create_task(browser(second,ref2,asyncio.Event()))
    try:
        await asyncio.sleep(0);release_create.set();await created.wait()
        result2=await task2
        assert result2.state=="completed" and browser.workflows[key] is sessions[1]
        sessions[0].lock.release()
        result1=await task1
        assert result1.error_code=="review_changed" and sessions[0].page.effects==[]
        assert sessions[0].context.closed and not sessions[1].context.closed
    finally:
        if sessions and sessions[0].lock.locked():sessions[0].lock.release()
        await asyncio.gather(task1,task2,return_exceptions=True)
        await browser.close()
    assert sessions[1].context.closed
