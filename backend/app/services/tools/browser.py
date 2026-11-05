"""Dedicated Chromium workflows behind a grant-aware loopback proxy."""

import asyncio
import base64
import hashlib
import ipaddress
import os
import secrets
import shutil
import socket
import tempfile
import time
from pathlib import Path
from dataclasses import dataclass, field
from uuid import uuid4
from urllib.parse import urlsplit

from app.services.tool_broker import ActionResult
from app.services.action_review import ReviewChanged
from app.services.tools.browser_start import BrowserStart
from app.services.tools.browser_profiles import BrowserProfiles, ProfileAuthority, UserAuthority


def origin(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password):
        raise ValueError("Unsupported browser URL")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise ValueError("Invalid browser port") from None
    host = parsed.hostname.lower()
    if host.endswith("."):
        host = host[:-1]
    if not host or ".." in host:
        raise ValueError("Invalid browser host")
    return parsed.scheme, host, port


def public_url(url):
    parsed = urlsplit(url)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"
    return base + (f" [parameters sha256:{hashlib.sha256(parsed.query.encode()).hexdigest()[:16]}]"
                   if parsed.query else "")


async def safe_address(host, port, allow_private=False):
    try:
        return str(ipaddress.ip_address(host))  # A literal address is an explicit target.
    except ValueError:
        pass
    if host == "localhost":
        return "127.0.0.1"
    addresses = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    values = [ipaddress.ip_address(item[4][0]) for item in addresses]
    if not values or (not allow_private and any(not value.is_global for value in values)):
        raise ValueError("Private or unresolvable destination")
    return str(values[0])


async def _relay(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


def _proxy_diagnostic(proxy, before, page, response, allowed):
    reasons=[{'reason':reason,'count':min(count-before.get(reason,0),1000000)}
             for reason,count in sorted(proxy.blocked_reasons.items()) if count>before.get(reason,0)]
    try:main_origin='approved' if origin(page.url)==allowed else 'other'
    except Exception:main_origin='unavailable'
    try:
        status=response.status if response is not None else None
        main_response=('ok' if status<400 else 'http_error') if type(status) is int else 'unavailable'
    except Exception:main_response='unavailable'
    return {'browser_proxy':reasons,'main_frame_origin':main_origin,'main_response':main_response}


def _approved_navigation_warning(session, before, page, response, allowed):
    """Permit only a successful reviewed main response with other-origin blocks."""
    diagnostic = _proxy_diagnostic(session.proxy, before, page, response, allowed)
    reasons = diagnostic['browser_proxy']
    try:
        status = response.status if response is not None else None
        valid = (type(status) is int and 200 <= status < 300
                 and diagnostic['main_frame_origin'] == 'approved'
                 and bool(reasons) and all(item['reason'] == 'authority_other_origin' for item in reasons)
                 and session.authority.active() and session.authority.allows(allowed)
                 and not session.closing and session.controller == 'agent')
    except Exception:
        valid = False
    return diagnostic if valid else None


class WorkflowAuthority:
    def __init__(self, sessions, room_id, agent_id, *, owner_epochs=None, workflow_id=None, execution_binding=None):
        self.sessions = sessions
        self.room_id = room_id
        self.agent_id = agent_id
        self.owner_epochs = owner_epochs
        self.workflow_id = workflow_id
        from app.services.resource_authority import run_generation
        self.generation = run_generation(sessions, room_id, agent_id)
        self.entries = []
        from app.services.resource_binding import freeze_binding
        self.execution_binding=freeze_binding(execution_binding)

    def add(self, allowed, grant):
        from app.services.resource_binding import freeze_binding
        if freeze_binding(getattr(grant,'execution_binding',None))!=self.execution_binding:
            raise ValueError('Browser workflow resource scope changed')
        if not any(item[0] == allowed and item[1].grant_id == grant.grant_id for item in self.entries):
            self.entries.append((allowed, grant))

    def revoke(self, grant_id):
        self.entries = [(allowed, grant) for allowed, grant in self.entries if grant.grant_id != grant_id]

    def _live(self, grant):
        from app.services.resource_authority import resource_live
        from app.services.resource_binding import freeze_binding
        try:
            if freeze_binding(getattr(grant,'execution_binding',None))!=self.execution_binding:return False
        except (ValueError,TypeError,AttributeError):return False
        if self.sessions is None:
            from app.services.grants import now
            return grant.expires_at is None or grant.expires_at > now()
        if (grant.room_id, grant.agent_id) != (self.room_id, self.agent_id):
            return False
        if grant.review is None or grant.review.action.workflow_id != self.workflow_id:
            return False
        return resource_live(self.sessions, grant, self.generation, owner_epochs=self.owner_epochs)

    def active(self):
        return any(self._live(grant) for _, grant in self.entries)

    def broad_active(self):
        return any(grant.scope == "broad" and self._live(grant) for _, grant in self.entries)

    def allows(self, requested):
        return any(self._live(grant) and (grant.scope == "broad" or allowed == requested)
                   for allowed, grant in self.entries)


class OriginProxy:
    def __init__(self, authority):
        self.authority = authority
        self.secret = secrets.token_urlsafe(32)
        self.server = None
        self.blocked = 0
        self.blocked_reasons = {}
        self.reviewed_origin = None
        self._proxy_tasks = set()
        self._proxy_writers = set()
        self._drain_lock = asyncio.Lock()
        self._quiescing = False
        self._closing = False
        self._starting = None

    async def start(self):
        if self._closing or self._starting is not None or self.server is not None:
            raise RuntimeError('Browser proxy cannot start')
        self._starting = asyncio.get_running_loop().create_future()
        try:
            server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
            if self._closing:
                server.close()
                await server.wait_closed()
                raise RuntimeError('Browser proxy already closed')
            self.server = server
            return self
        finally:
            self._starting.set_result(None)

    def _accept(self, reader, writer):
        # Registration is synchronous: even handlers that have not started yet
        # are included when a grant is revoked or the listener is closed.
        if self._closing or self._quiescing:
            writer.transport.abort()
            return
        self._proxy_writers.add(writer)
        task = asyncio.create_task(self._serve(reader, writer))
        self._proxy_tasks.add(task)
        def finished(done):
            self._proxy_tasks.discard(done)
            self._proxy_writers.discard(writer)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)

    def _allows(self, requested):
        return not (self._closing or self._quiescing) and self.authority.allows(requested)

    @property
    def port(self):
        return self.server.sockets[0].getsockname()[1]

    async def _serve(self, reader, writer):
        upstream = None
        phase = 'request'
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            if len(header) > 65536:
                raise ValueError
            lines = header.decode("latin1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            expected = "Basic " + base64.b64encode(f"gamja:{self.secret}".encode()).decode()
            if not any(line.lower() == f"proxy-authorization: {expected}".lower() for line in lines[1:]):
                raise PermissionError
            if method == "CONNECT":
                scheme, host, port = origin("https://" + target + "/")
                path = None
            else:
                scheme, host, port = origin(target)
                parsed = urlsplit(target)
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
            requested = (scheme, host, port)
            phase = 'authority'
            if not self._allows(requested):
                raise ValueError
            phase = 'host'
            hosts = [line.split(":", 1)[1].strip() for line in lines[1:]
                     if line.lower().startswith("host:")]
            if hosts and (len(hosts) != 1 or origin(f"{scheme}://{hosts[0]}/") != requested):
                raise ValueError
            phase = 'destination'
            address = await safe_address(host, port, self.authority.broad_active())
            phase = 'authority'
            if not self._allows(requested):
                raise ValueError
            phase = 'connect'
            upstream_reader, upstream = await asyncio.wait_for(asyncio.open_connection(address, port), 5)
            self._proxy_writers.add(upstream)
            phase = 'authority'
            if not self._allows(requested):
                raise ValueError
            phase = 'transfer'
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                forwarded = [f"{method} {path} {version}"]
                forwarded += [line for line in lines[1:] if line and not line.lower().startswith((
                    "proxy-authorization:", "proxy-connection:", "connection:"))]
                forwarded += ["Connection: close", "", ""]
                upstream.write("\r\n".join(forwarded).encode("latin1"))
            await writer.drain()
            await upstream.drain()
            await asyncio.gather(_relay(reader, upstream), _relay(upstream_reader, writer))
            return
        except PermissionError:
            try:
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=gamja\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
            except (ConnectionError, OSError):
                pass
        except (ValueError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as error:
            self.blocked += 1
            if isinstance(error, asyncio.TimeoutError):reason='timeout'
            elif phase == 'request':reason='request_invalid'
            elif phase == 'authority':
                reviewed=self.reviewed_origin
                if reviewed is None:reason='authority_scope_unknown'
                elif requested!=reviewed:reason='authority_other_origin'
                else:
                    try:
                        active=self.authority.active()
                        reason=('authority_same_active' if active is True else
                                'authority_same_inactive' if active is False else 'authority_same_unknown')
                    except Exception:reason='authority_same_unknown' 
            elif phase == 'host':reason='host_mismatch'
            elif phase == 'destination':reason='destination_policy' if isinstance(error,ValueError) else 'dns_failed'
            elif phase == 'connect':reason='connect_failed'
            else:reason='transfer_failed'
            self.blocked_reasons[reason]=self.blocked_reasons.get(reason,0)+1
            try:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
            except (ConnectionError, OSError):
                pass
        finally:
            writer.close()
            if upstream is not None:
                upstream.close()
                self._proxy_writers.discard(upstream)
            for stream in (writer, upstream):
                if stream is not None:
                    try:
                        await stream.wait_closed()
                    except (ConnectionError, OSError):
                        pass

    async def abort_connections(self, *, keep_paused=False):
        async with self._drain_lock:
            self._quiescing = True
            for writer in tuple(self._proxy_writers):
                writer.transport.abort()
            # Do not cancel DNS/connect handlers: cancellation can leave their
            # underlying work running.  They recheck the fence before use.
            tasks = tuple(self._proxy_tasks)
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=10)
                if pending:
                    raise RuntimeError('Browser proxy shutdown unconfirmed')
            if not self._closing and not keep_paused:
                self._quiescing = False

    def resume(self, authority):
        if self._closing or not self._quiescing or self._proxy_tasks:
            raise RuntimeError('Browser proxy cannot resume')
        self.authority = authority
        self._quiescing = False

    async def close(self):
        self._closing = True
        if self._starting is not None:
            await asyncio.shield(self._starting)
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        await self.abort_connections()


@dataclass
class BrowserSession:
    context: object
    playwright: object
    proxy: OriginProxy
    profile: str
    page: object
    authority: WorkflowAuthority
    room_id: int
    agent_id: int
    workflow_id: str | None
    deadline: float
    lock: asyncio.Lock
    context_id: str = field(default_factory=lambda: uuid4().hex)
    profile_id: str | None = None
    profile_lease: int | None = None
    controller: str = 'agent'
    closing: bool = False
    close_retry_at: float = 0.0
    execution_binding: object | None = None
    trigger_event_id: str | None = None
    run_generation: int | None = None
    retired: bool = False



class BrowserExecutor:
    def __init__(self, engine="installed", sessions=None, *, owner_epochs=None, app_data=None):
        self.engine = engine
        self.sessions = sessions
        self.owner_epochs = owner_epochs
        self.app_data = Path(app_data) if app_data is not None else None
        self.workflows = {}
        self.parked = {}
        self._pending_creations = {}
        self._watcher = None
        self._close_lock = asyncio.Lock()
        self.profiles = BrowserProfiles(self, app_data)

    def _host_identity_valid(self, metadata):
        host = self.parked.get(metadata['id'])
        try:
            return bool(host and not host['session'].closing and host['session'].retired
                and host['revision'] == metadata['revision']
                and self.owner_epochs.current(metadata['owner_id']) == host['epoch']
                and self.profiles.claims.get(metadata['id']) == host['session'].profile_lease
                and host['session'].proxy._quiescing and not host['session'].proxy._closing
                and host['session'].context.browser.is_connected())
        except Exception:
            return False

    def parked_valid(self, metadata):
        try:
            return self._host_identity_valid(metadata) and not self.parked[metadata['id']]['session'].context.pages
        except Exception:
            return False

    async def _empty_targets(self, host):
        await host['cdp'].send('Browser.getVersion')
        targets = await host['cdp'].send('Target.getTargets')
        if targets.get('targetInfos') != [] or host['session'].context.pages:
            raise RuntimeError('Browser targets not empty')

    async def _park(self, key, session):
        async with session.lock:
            async with self._close_lock:
                if session.retired or self.workflows.get(key) is not session:
                    return
                session.closing = True
                try:
                    metadata = self.profiles.read(session.profile_id)
                    epoch = self.owner_epochs.current(metadata['owner_id'])
                    if not epoch or not session.authority.active():
                        raise RuntimeError('Browser authority changed')
                    await session.proxy.abort_connections(keep_paused=True)
                    cdp = await session.context.browser.new_browser_cdp_session()
                    for page in list(session.context.pages):
                        await page.close()
                    host = {'session': session, 'cdp': cdp, 'epoch': epoch,
                            'revision': metadata['revision'] + 1, 'park_id': uuid4().hex}
                    await self._empty_targets(host)
                    if (not session.authority.active() or self.owner_epochs.current(metadata['owner_id']) != epoch
                            or self.profiles.read(session.profile_id) != metadata):
                        raise RuntimeError('Browser profile changed')
                    metadata['revision'] += 1
                    metadata['last_used_at'] = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
                    self.profiles._save(metadata)
                    session.retired = True
                    session.closing = False
                    self.parked[session.profile_id] = host
                    self.workflows.pop(key, None)
                except BaseException:
                    session.deadline = 0
                    session.close_retry_at = time.monotonic() + 5
                    raise

    async def park_completed(self, room_id, agent_id, trigger_event_id, generation, execution_binding, workflow_ids):
        from app.services.resource_binding import freeze_binding
        proof = freeze_binding(execution_binding)
        for key, session in list(self.workflows.items()):
            if (session.profile_id and session.controller == 'agent'
                    and (session.room_id, session.agent_id, session.trigger_event_id, session.run_generation)
                    == (room_id, agent_id, trigger_event_id, generation)
                    and session.workflow_id in workflow_ids
                    and freeze_binding(session.execution_binding) == proof):
                await self._park(key, session)
        from app.services.vault_profile_handoff import park_completed_custodies
        await park_completed_custodies(self,room_id,agent_id,trigger_event_id,generation,execution_binding,workflow_ids)

    async def _borrow(self, metadata, action, grant, allowed, execution_binding, manual, manual_epoch):
        from app.services.vault_profile_handoff import hold
        async with self._close_lock:
            host = self.parked.get(metadata['id'])
            if (not self.parked_valid(metadata) or hold(self.profiles, metadata['id'])
                    or (not manual and metadata['id'] in self.profiles.transitions)
                    or list(allowed) not in metadata['origins']):
                raise ReviewChanged('Parked browser changed')
            old = host['session']
            if manual:
                authority = UserAuthority(self.profiles, metadata, manual_epoch, action.room_id)
            else:
                if grant.scope != 'once':raise ReviewChanged('Fresh once review required')
                authority = WorkflowAuthority(self.sessions, action.room_id, action.agent_id,
                    owner_epochs=self.owner_epochs, workflow_id=action.workflow_id, execution_binding=execution_binding)
                authority.add(allowed, grant)
                authority = ProfileAuthority(authority, metadata, self.profiles, action.room_id, action.agent_id)
            await self._empty_targets(host)
            page = await old.context.new_page()
            try:
                # The new page is deliberately blank and the proxy remains
                # sealed until identity and authority are rechecked.
                if page.url != 'about:blank' or not self._host_identity_valid(metadata) or not authority.active():
                    raise ReviewChanged('Parked browser changed')
                page_cdp = await old.context.new_cdp_session(page)
                target_id = (await page_cdp.send('Target.getTargetInfo'))['targetInfo']['targetId']
                targets = (await host['cdp'].send('Target.getTargets')).get('targetInfos')
                if not isinstance(targets, list) or len(targets) != 1 or targets[0].get('targetId') != target_id:
                    raise ReviewChanged('Unexpected browser target')
                await page_cdp.detach()
                targets = (await host['cdp'].send('Target.getTargets')).get('targetInfos')
                if not isinstance(targets, list) or len(targets) != 1 or targets[0].get('targetId') != target_id:
                    raise ReviewChanged('Unexpected browser target')
                if not self._host_identity_valid(metadata) or not authority.active():
                    raise ReviewChanged('Parked browser authority changed')
                session = BrowserSession(old.context, old.playwright, old.proxy, old.profile, page, authority,
                    action.room_id, action.agent_id, action.workflow_id,
                    time.monotonic() + (1800 if manual else 300), asyncio.Lock(),
                    profile_id=metadata['id'], profile_lease=old.profile_lease,
                    controller='user' if manual else 'agent', execution_binding=execution_binding,
                    trigger_event_id=getattr(action,'trigger_event_id',None),
                    run_generation=None if manual else authority.base.generation)
                old.proxy.resume(authority)
                self.parked.pop(metadata['id'])
                return session
            except BaseException:
                await page.close()
                raise

    async def _create(self, action, grant, allowed, manual=None, manual_epoch=None):
        from playwright.async_api import async_playwright
        from app.services.resource_authority import require_scope_start
        if manual:
            from app.services.resource_binding import freeze_binding
            if freeze_binding(getattr(action,'execution_binding',None)) is not None:
                raise ReviewChanged('Scoped manual browser is unsupported')
        execution_binding=None if manual else require_scope_start(self.sessions,action,grant)
        delegated=execution_binding and execution_binding.as_dict()['context_mode']=='delegated'
        if delegated and (manual or getattr(grant,'scope',None)!='once'):raise ReviewChanged('Delegated browser requires once review')
        metadata = manual or self.profiles.selected(action,execution_binding)
        if not manual:
            review=getattr(grant,'review',None);target=review.targets[0] if review and review.targets else None
            current=(metadata['id'],metadata['revision']) if metadata else (None,None)
            if current!=(getattr(target,'profile_id',None),getattr(target,'profile_revision',None)):
                raise ReviewChanged('Reviewed browser profile changed')
        if metadata and metadata['id'] in self.parked:
            return await self._borrow(metadata, action, grant, allowed, execution_binding, manual, manual_epoch)
        lease = None
        if metadata:
            if list(allowed) not in metadata['origins']:
                raise ValueError('Browser origin is outside the selected profile scope')
            lease = self.profiles.claim(metadata)
            profile = str(self.profiles.directory(metadata['id'])/'data')
        else:
            profile = tempfile.mkdtemp(prefix="gamja-browser-")
        try:
            if manual:authority = UserAuthority(self.profiles, metadata, manual_epoch, action.room_id)
            else:
                authority = WorkflowAuthority(self.sessions, action.room_id, action.agent_id,
                    owner_epochs=self.owner_epochs, workflow_id=action.workflow_id,execution_binding=execution_binding)
                authority.add(allowed, grant)
                if metadata:authority = ProfileAuthority(authority, metadata, self.profiles, action.room_id, action.agent_id)
        except BaseException:
            if metadata:self.profiles.release(metadata['id'],lease)
            else:shutil.rmtree(profile,ignore_errors=True)
            raise
        proxy = OriginProxy(authority)
        playwright = None
        context = None
        try:
            if self.sessions is not None and not authority.active():
                raise ValueError("Browser resource authority changed before context launch")
            await proxy.start()
            playwright = await async_playwright().start()
            options = {"user_data_dir": profile, "headless": not bool(metadata or manual),
                       "accept_downloads": False, "service_workers": "block",
                       "proxy": {"server": f"http://127.0.0.1:{proxy.port}",
                                 "username": "gamja", "password": proxy.secret},
                       "args": ["--proxy-bypass-list=<-loopback>", "--disable-http2", "--disable-quic"]}
            # Prefer the already bundled Chromium for profiles; never install a browser here.
            bundled = metadata and os.path.isfile(playwright.chromium.executable_path)
            if self.engine == "installed" and not bundled:
                channels = ("msedge", "chrome") if os.name == "nt" else ("chrome", "msedge")
                last_error = None
                for channel in channels:
                    try:
                        context = await playwright.chromium.launch_persistent_context(**options, channel=channel)
                        break
                    except Exception as error:
                        last_error = error
                if context is None:
                    raise last_error
            else:
                context = await playwright.chromium.launch_persistent_context(**options)
            if metadata and not authority.active():raise ValueError('Browser authority changed during launch')
            context.set_default_timeout(10000)
            context.set_default_navigation_timeout(15000)
            page = context.pages[0] if context.pages else await context.new_page()
            session = BrowserSession(context, playwright, proxy, profile, page, authority,
                                  action.room_id, action.agent_id, action.workflow_id,
                                  time.monotonic() + (1800 if manual else 300), asyncio.Lock(),
                                  profile_id=metadata['id'] if metadata else None, profile_lease=lease,
                                  controller='user' if manual else 'agent',execution_binding=execution_binding,
                                  trigger_event_id=getattr(action,'trigger_event_id',None),
                                  run_generation=None if manual else (authority.base.generation if metadata else authority.generation))
            if metadata:
                metadata['last_used_at'] = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
                self.profiles._save(metadata)
            return session
        except BaseException:
            cleanup_id = uuid4().hex
            self._pending_creations[cleanup_id] = {
                'proxy': proxy, 'context': context, 'playwright': playwright,
                'profile': profile, 'profile_id': metadata['id'] if metadata else None,
                'lease': lease, 'retry_at': 0,
            }
            if self._watcher is None:
                self._watcher = asyncio.create_task(self._watch())
            try:
                await self._cleanup_creation(cleanup_id)
            except Exception:
                pass  # Retained for the owned-resource watcher and explicit close.
            raise

    async def _cleanup_creation(self, cleanup_id):
        async with self._close_lock:
            pending = self._pending_creations.get(cleanup_id)
            if pending is None:
                return
            failed = False
            # Attempt all three even if the proxy drain fails.  Never release
            # the profile lease until every required shutdown is confirmed.
            for name, method in (('proxy', 'close'), ('context', 'close'), ('playwright', 'stop')):
                resource = pending[name]
                if resource is None:
                    continue
                try:
                    await getattr(resource, method)()
                    pending[name] = None
                except Exception:
                    failed = True
            if failed:
                pending['retry_at'] = time.monotonic() + 5
                raise RuntimeError('Browser creation cleanup unconfirmed')
            try:
                if pending['profile_id']:
                    self.profiles.release(pending['profile_id'], pending['lease'])
                elif os.path.exists(pending['profile']):
                    shutil.rmtree(pending['profile'])
            except Exception:
                pending['retry_at'] = time.monotonic() + 5
                raise
            self._pending_creations.pop(cleanup_id, None)

    async def __call__(self, action, grant, cancel):
        try:
            allowed = origin(action.arguments["url"])
        except ValueError:
            return ActionResult("failed", "Invalid browser target.")
        if cancel.is_set():
            return ActionResult("cancelled", "Action cancelled before browser effect.")
        from app.services.resource_authority import require_scope_start
        execution_binding=require_scope_start(self.sessions,action,grant)
        delegated=execution_binding and execution_binding.as_dict()['context_mode']=='delegated'
        metadata = self.profiles.selected(action,execution_binding)
        review = getattr(grant, 'review', None)
        target = review.targets[0] if review and review.targets else None
        if delegated and (target is None or getattr(grant,'scope',None)!='once'):
            return ActionResult('failed','Delegated browser requires a fresh once review.',error_code='review_changed')
        if metadata or getattr(target,'profile_id',None):
            current=(metadata['id'],metadata['revision']) if metadata else (None,None)
            if current != (getattr(target,'profile_id',None),getattr(target,'profile_revision',None)):
                return ActionResult('failed','Selected browser profile changed; review this action again.',error_code='review_changed')
        if metadata:
            from app.services.vault_profile_handoff import hold,execute_custody
            if hold(self.profiles,metadata['id']):
                try:return await execute_custody(self,action,grant,cancel,metadata,allowed,execution_binding)
                except ReviewChanged:return ActionResult('failed','Browser review changed; review this request again.',error_code='review_changed')
        key = (action.room_id, action.agent_id, action.workflow_id or action.action_id)
        session = self.workflows.get(key)
        temporary = action.workflow_id is None
        created_here = False
        try:
            start = BrowserStart(self, key, action, grant, session) if action.family == "browser_interact" else None
            if session is not None:
                from app.services.resource_binding import freeze_binding
                if (getattr(session,'profile_id',None) != (metadata['id'] if metadata else None)
                        or freeze_binding(getattr(session,'execution_binding',None))!=execution_binding or getattr(session,'controller',None)!='agent'):
                    return ActionResult('failed','Reviewed browser context changed.',error_code='review_changed')
                if not session.authority.active():
                    await self._close(key, session)
                    session = None
            if session is None:
                session = await self._create(action, grant, allowed)
                self.workflows[key] = session
                created_here = True
                if self._watcher is None:
                    self._watcher = asyncio.create_task(self._watch())
                if start is not None:
                    start.created(session)
            else:
                session.authority.add(allowed, grant)
            operation = asyncio.create_task(self._perform(session, action, cancel, allowed, start))
            result = await asyncio.shield(operation)
            if created_here and action.family == "browser_screenshot" and result.state != "completed":
                await self._close(key, session, owned_created=True)
                created_here = False
            return result
        except asyncio.CancelledError:
            if session:
                await asyncio.shield(self._close(key, session, owned_created=created_here))
                created_here = False
            if "operation" in locals():
                await asyncio.gather(operation, return_exceptions=True)
            raise
        except ReviewChanged:
            if created_here:
                await self._close(key, session, owned_created=True)
                created_here = False
            return ActionResult("failed", "Browser review changed; review this request again.",
                                error_code="review_changed")
        except Exception:
            if created_here and action.family == "browser_screenshot":
                await self._close(key, session, owned_created=True)
                created_here = False
            return ActionResult("failed", "Browser navigation or interaction failed within approved origin.")
        finally:
            if session and (temporary or cancel.is_set()
                            or (created_here and self.workflows.get(key) is not session)):
                if (temporary and session.profile_id and not cancel.is_set()
                        and 'result' in locals() and result.state == 'completed'
                        and self.workflows.get(key) is session):
                    await self._park(key, session)
                else:
                    await self._close(key, session, owned_created=created_here)

    async def _perform(self, session, action, cancel, allowed, start=None):
        async with session.lock:
            if session.controller != 'agent' or session.closing:
                return ActionResult('denied', 'Owner has browser control; return it explicitly before Agent input.')
            if cancel.is_set():
                return ActionResult("uncertain", "Browser action stopped after launch.")
            if start is not None:
                start.check(session)
            session.proxy.reviewed_origin=allowed
            blocked_before = session.proxy.blocked
            reason_before = dict(session.proxy.blocked_reasons)
            page = session.page
            requested = action.arguments["url"]
            if action.family == "browser_navigate":
                response = await page.goto(requested, wait_until="domcontentloaded")
            elif page.url != requested:
                response = await page.goto(requested, wait_until="domcontentloaded")
            else:
                response = None
            if response and response.status >= 400:
                diagnostic=(_proxy_diagnostic(session.proxy,reason_before,page,response,allowed)
                            if session.proxy.blocked > blocked_before else None)
                return ActionResult("failed", "Browser request was denied or failed.",diagnostic=diagnostic)
            if action.family == "browser_interact":
                if cancel.is_set():
                    return ActionResult("uncertain", "Browser action stopped after navigation.")
                start.check(session)
                element = page.locator(action.arguments["selector"]).first
                operation = action.arguments["operation"]
                # Python preflight only: Playwright's internal actionability
                # waits still need their separate mutation-start guard.
                start.check(session)
                if operation == "fill":
                    await element.fill(action.arguments.get("text", ""))
                elif operation == "click":
                    await element.click()
                else:
                    await element.evaluate("el => el.form?.requestSubmit(el)")
                await page.wait_for_timeout(100)
            if not session.authority.allows(origin(page.url)):
                diagnostic=(_proxy_diagnostic(session.proxy,reason_before,page,response,allowed)
                            if session.proxy.blocked > blocked_before else None)
                return ActionResult("failed", "Browser left the approved origin.",diagnostic=diagnostic)
            if session.proxy.blocked > blocked_before:
                warning = (_approved_navigation_warning(session,reason_before,page,response,allowed)
                           if action.family == 'browser_navigate' else None)
                if warning is None:
                    return ActionResult("failed", "Browser blocked an out-of-scope request.",
                                        diagnostic=_proxy_diagnostic(session.proxy,reason_before,page,response,allowed))
            if action.family == "browser_screenshot":
                if origin(page.url) != allowed:
                    return ActionResult("failed", "Browser left the reviewed origin.")
                if cancel.is_set() or not session.authority.active():
                    return ActionResult("cancelled", "Browser capture permission ended.")
                image = await page.screenshot(type="png", full_page=False, timeout=5000)
                if (cancel.is_set() or session.closing or session.controller != "agent"
                        or not session.authority.active() or not session.authority.allows(origin(page.url))
                        or origin(page.url) != allowed or session.proxy.blocked > blocked_before):
                    return ActionResult("cancelled", "Browser capture permission ended.")
                from app.services.tools.desktop import validate_screenshot_png, save_screenshot_png
                try:
                    width, height = validate_screenshot_png(image)
                    if self.app_data is None:
                        raise ValueError("Screenshot storage unavailable")
                    ref = save_screenshot_png(self.app_data, action, image)
                except (OSError, ValueError, TypeError, SyntaxError) as error:
                    return ActionResult("failed", "Browser screenshot could not be validated or saved.",
                                        error_code="screenshot_artifact_unavailable")
                return ActionResult("completed", "Browser page captured.", screenshot_ref=ref,
                                    image_bytes=image, image_mime="image/png", image_width=width, image_height=height)
            title = await page.title()
            body = (await page.locator("body").inner_text(timeout=5000))[:12000]
            controls = await page.locator("a,button,input,textarea,select").evaluate_all(
                "els => els.slice(0,80).map(e => ({tag:e.tagName.toLowerCase(),text:(e.innerText||'').slice(0,80),id:e.id,name:e.name,placeholder:e.placeholder}))")
            warning = None
            if action.family == 'browser_navigate':
                try:
                    still_allowed = (not session.closing and session.controller == 'agent'
                                     and origin(page.url) == allowed and session.authority.active()
                                     and session.authority.allows(allowed))
                except Exception:
                    still_allowed = False
                if not still_allowed:
                    return ActionResult('cancelled', 'Browser permission ended before the page could be returned.')
            if action.family == 'browser_navigate' and session.proxy.blocked > blocked_before:
                warning = _approved_navigation_warning(session,reason_before,page,response,allowed)
                if warning is None:
                    return ActionResult('failed', 'Browser blocked an out-of-scope request.',
                                        diagnostic=_proxy_diagnostic(session.proxy,reason_before,page,response,allowed))
            notice = ('Some requests to other sites were blocked; the page may be incomplete.\n' if warning else '')
            return ActionResult("uncertain" if cancel.is_set() else "completed",
                                f"{notice}URL: {('profile-scoped page; URL parameters omitted' if session.profile_id else public_url(page.url))}\nTitle: {title}\nText: {body}\nControls: {str(controls)[:3000]}",
                                diagnostic=warning)

    async def _close(self, key, session, *, owned_created=False, handoff_last_used_at=None):
        async with self._close_lock:
            # A concurrent creation may replace the mapping while this
            # invocation waits. Dispose only the object it actually created;
            # never remove or close the replacement mapped by another call.
            if session.retired or (self.workflows.get(key) is not session and not owned_created):
                return
            if session.profile_id:
                session.closing = True
                try:
                    await session.proxy.close()
                    await session.context.close()
                    await session.playwright.stop()
                    metadata=self.profiles.read(session.profile_id)
                    metadata['revision']+=1
                    metadata['last_used_at']=handoff_last_used_at or __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
                    self.profiles._save(metadata)
                    self.profiles.release(session.profile_id,session.profile_lease)
                    if self.workflows.get(key) is session:self.workflows.pop(key,None)
                except BaseException:
                    session.deadline=0;session.close_retry_at=time.monotonic()+5
                    raise
                return
            session.closing = True
            try:
                await session.proxy.close()
                await session.context.close()
                await session.playwright.stop()
                if os.path.exists(session.profile):shutil.rmtree(session.profile)
                if self.workflows.get(key) is session:self.workflows.pop(key, None)
            except BaseException:
                session.deadline=0;session.close_retry_at=time.monotonic()+5
                raise

    async def close_parked(self, profile_id, *, expected_host, handoff_last_used_at=None):
        async with self._close_lock:
            host = self.parked.get(profile_id)
            if host is not expected_host:return False
            session = host['session']
            session.closing = True
            try:
                await session.proxy.close()
                await session.context.close()
                await session.playwright.stop()
                metadata = self.profiles.read(profile_id)
                if metadata['revision'] != host['revision']:
                    raise RuntimeError('Parked browser revision changed')
                metadata['revision'] += 1
                metadata['last_used_at'] = handoff_last_used_at or __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
                self.profiles._save(metadata)
                self.profiles.release(profile_id, session.profile_lease)
                self.parked.pop(profile_id, None)
                return True
            except BaseException:
                session.close_retry_at = time.monotonic() + 5
                raise

    async def _watch(self):
        try:
            while (self.workflows or self.parked or self._pending_creations or
                   any(slot.get('state') in ('sealed','borrowing','borrowed','ending','parking') for slot in getattr(self.profiles,'custody_slots',{}).values())):
                for slot in list(getattr(self.profiles,'custody_slots',{}).values()):
                    if slot.get('state') in ('sealed','borrowing','borrowed','ending','parking'):
                        try:current=self.owner_epochs.current(slot['owner']) if self.owner_epochs else None
                        except Exception:current=None
                        if current!=slot['epoch']:
                            try:
                                from app.services.vault_profile_handoff import stop_custodies
                                await stop_custodies(self,lambda candidate:candidate is slot)
                            except Exception:pass
                for profile_id, host in list(self.parked.items()):
                    if time.monotonic() >= host['session'].close_retry_at:
                        try:
                            try:metadata=self.profiles.read(profile_id)
                            except Exception:metadata=None
                            if host['session'].closing or metadata is None or not self.parked_valid(metadata):
                                await self.close_parked(profile_id,expected_host=host)
                        except Exception:pass
                for cleanup_id, pending in list(self._pending_creations.items()):
                    if time.monotonic() >= pending['retry_at']:
                        try:await self._cleanup_creation(cleanup_id)
                        except Exception:pass
                for key, session in list(self.workflows.items()):
                    if time.monotonic() >= session.close_retry_at and (time.monotonic() >= session.deadline or session.page.is_closed() or not session.authority.active()):
                        try:await self._close(key, session)
                        except Exception:pass
                await asyncio.sleep(.25)
        finally:
            self._watcher = None

    async def close(self):
        from app.services.vault_profile_handoff import stop_custodies
        custody_error=None
        try:await stop_custodies(self,lambda slot:True)
        except Exception as error:custody_error=error
        for cleanup_id in list(self._pending_creations):
            await self._cleanup_creation(cleanup_id)
        for key, session in list(self.workflows.items()):
            await self._close(key, session)
        for profile_id,host in list(self.parked.items()):
            await self.close_parked(profile_id,expected_host=host)
        if custody_error:raise custody_error
        if self._watcher:
            await self._watcher

    async def stop_room(self, room_id, agent_id=None):
        from app.services.vault_profile_handoff import stop_custodies
        custody_error=None
        try:await stop_custodies(self,lambda slot:any(row and row.get('room_id')==room_id and (agent_id is None or row.get('agent_id')==agent_id) for row in (slot,slot.get('active'))) or bool(slot.get('workflow') and slot['workflow'][0]==room_id and (agent_id is None or slot['workflow'][1]==agent_id)) or bool(slot.get('workflow_identity') and slot['workflow_identity'].affected_by_stop(room_id,agent_id)))
        except Exception as error:custody_error=error
        for key, session in list(self.workflows.items()):
            if session.room_id == room_id and (agent_id is None or session.agent_id == agent_id):
                await self._close(key, session)
        for profile_id, host in list(self.parked.items()):
            session=host['session']
            if session.room_id == room_id and (agent_id is None or session.agent_id == agent_id):
                await self.close_parked(profile_id,expected_host=host)
        if custody_error:raise custody_error

    async def stop_grant(self, grant_id):
        from app.services.vault_profile_handoff import stop_custodies
        custody_error=None
        try:await stop_custodies(self,lambda slot:grant_id in slot.get('workflow_grants',set()) or bool(slot.get('active') and slot['active'].get('grant_id')==grant_id))
        except Exception as error:custody_error=error
        for profile_id, host in list(self.parked.items()):
            authority=host['session'].authority
            if any(grant.grant_id == grant_id for _, grant in getattr(authority,'entries',())):
                await self.close_parked(profile_id,expected_host=host)
        for key, session in list(self.workflows.items()):
            session.authority.revoke(grant_id)
            try:
                await session.proxy.abort_connections()
            except BaseException:
                session.closing=True;session.deadline=0;session.close_retry_at=time.monotonic()+5
                if self._watcher is None:self._watcher=asyncio.create_task(self._watch())
                raise
            if not session.authority.active():
                await self._close(key, session)
                host=self.parked.get(session.profile_id)
                if host and host['session'] is session:await self.close_parked(session.profile_id,expected_host=host)
        if custody_error:raise custody_error

    async def stop_scopes(self, bindings):
        from app.services.resource_binding import revocation_map
        revoked=revocation_map(bindings)
        from app.services.vault_profile_handoff import stop_custodies
        custody_error=None
        try:await stop_custodies(self,lambda slot:bool(slot.get('workflow_proof') and slot['workflow_proof'].affected(revoked)) or bool(slot.get('active') and slot['active'].get('execution_binding') and slot['active']['execution_binding'].affected(revoked)))
        except Exception as error:custody_error=error
        for profile_id,host in list(self.parked.items()):
            session=host['session']
            if session.controller=='agent' and session.execution_binding and session.execution_binding.affected(revoked):
                await self.close_parked(profile_id,expected_host=host)
        for key,session in list(self.workflows.items()):
            if session.controller=='agent' and session.execution_binding and session.execution_binding.affected(revoked):
                await self._close(key,session)
                host=self.parked.get(session.profile_id)
                if host and host['session'] is session:await self.close_parked(session.profile_id,expected_host=host)
        if custody_error:raise custody_error
