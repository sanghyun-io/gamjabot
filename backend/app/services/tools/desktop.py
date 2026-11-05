"""Host-wide desktop workflow ownership and authenticated native transport."""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import sqlite3
import socket
import struct
import sys
import zlib
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from PIL import Image

from app.services.diagnostic_detail import make_diagnostic


RELEASE_WAIT_SECONDS = 0.5
RELEASE_RETRY_SECONDS = 0.1
RELEASE_SHUTDOWN_SECONDS = 15


def host_lock_path():
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path.home() / "Library" / "Application Support" if sys.platform == "darwin" else Path.home() / ".local" / "share"
    return root / "Gamja Bot" / "desktop-workflow.lock"


def native_lock_path():
    return host_lock_path().with_name("desktop-workflow.native.lock")


def _try_lock(handle):
    if sys.platform == "win32":
        import msvcrt
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(handle):
    if sys.platform == "win32":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_UN)


def validate_screenshot_png(image, expected=None):
    if len(image) > 10_000_000 or image[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
        raise ValueError("Invalid screenshot")
    width, height = struct.unpack(">II", image[16:24])
    if (expected is not None and (width, height) != expected) or width < 1 or height < 1 or width * height > 25_000_000:
        raise ValueError("Screenshot dimensions disagree")
    _validate_png_chunks(image)
    with Image.open(BytesIO(image)) as decoded:
        if decoded.format != "PNG" or decoded.size != (width, height):
            raise ValueError("Invalid screenshot")
        decoded.verify()
    with Image.open(BytesIO(image)) as decoded:
        decoded.load()
    return width, height


def save_screenshot_png(app_data, action, image):
    directory = app_data / "rooms" / str(action.room_id) / "screenshots"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for part in (app_data / "rooms", directory.parent, directory):
        if part.is_symlink():
            raise ValueError("Screenshot artifact directory is a symlink")
        os.chmod(part, 0o700)
    path = directory / f"{action.action_id}.png"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(image)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return f"rooms/{action.room_id}/screenshots/{action.action_id}.png"


class NativeRefusal(Exception):
    def __init__(self, text, code=None):
        super().__init__(text)
        self.code = code


class BackendStartupLock:
    """Prevent a second backend from marking a live backend's actions crashed."""

    def __init__(self, database_path):
        self.path = Path(str(Path(database_path).resolve()) + ".backend.lock")
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if not _try_lock(handle):
            handle.close()
            raise RuntimeError("Gamja backend is already running for this data directory")
        self.handle = handle
        return self

    def __exit__(self, *_):
        if self.handle is not None:
            _unlock(self.handle)
            self.handle.close()
            self.handle = None


class DesktopLeaseManager:
    def __init__(self, database_path: str, lock_path: str | None = None, native=None):
        self.database_path = str(Path(database_path).resolve()) if database_path != ":memory:" else database_path
        self.lock_path = Path(lock_path) if lock_path else host_lock_path()
        self._handle = None
        self._owner = None
        self._mutex = asyncio.Lock()
        self._release_task = None
        self.native = native
        self._proof={}

    def owns(self, owner, lease_id):
        """Check the still-held local and persisted lease before dispatch."""
        if (self._owner != owner or self._lease_id != lease_id or self._release_task is not None
                or self._handle is None or self._handle.closed):
            return False
        try:
            with sqlite3.connect(self.database_path, timeout=3) as db:
                row = db.execute("SELECT room_id,agent_id,workflow_id,session_id,lease_id,expires_at FROM desktop_lease WHERE id=1").fetchone()
            if row is None or tuple(row[:4]) != owner or row[4] != lease_id:
                return False
            expiry = datetime.fromisoformat(row[5])
            return expiry.tzinfo is not None and expiry > datetime.now(timezone.utc)
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return False

    def current_owner(self):
        return self._read_locked_owner(self.lock_path) or self._read_locked_owner(native_lock_path())

    @staticmethod
    def _read_locked_owner(path):
        if not path.exists():
            return None
        try:
            with path.open("r+b") as probe:
                if _try_lock(probe):
                    _unlock(probe)
                    return None
                probe.seek(0)
                data = json.load(probe)
                return {key: data[key] for key in ("room_id", "agent_id", "workflow_id")}
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None

    async def acquire(self, room_id, agent_id, workflow_id, session_id, cancel, proof=None):
        owner = (room_id, agent_id, workflow_id, session_id)
        if not workflow_id:
            raise ValueError("Desktop actions require a workflow ID")
        while True:
            if cancel.is_set():
                raise asyncio.CancelledError()
            async with self._mutex:
                if self._owner == owner and self._release_task is None:
                    return self._lease_id
                if self._owner is None:
                    self.lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    handle = self.lock_path.open("a+b")
                    if _try_lock(handle):
                        lease_id = uuid4().hex
                        try:
                            with sqlite3.connect(self.database_path, timeout=3) as db:
                                db.execute("INSERT INTO desktop_lease (id, room_id, agent_id, workflow_id, session_id, lease_id, acquired_at, expires_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                                           (room_id, agent_id, workflow_id, session_id, lease_id,
                                            datetime.now(timezone.utc).isoformat(),
                                            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()))
                            handle.seek(0)
                            handle.truncate()
                            handle.write(json.dumps({"room_id": room_id, "agent_id": agent_id,
                                                     "workflow_id": workflow_id}).encode())
                            handle.flush()
                            os.fsync(handle.fileno())
                            if self.native is not None:
                                response = await self.native.call({"operation": "lease_acquire", "room_id": room_id,
                                    "agent_id": agent_id, "workflow_id": workflow_id,
                                    "lease_id": lease_id, **(proof or {})})
                                if response["state"] != "completed":
                                    raise NativeRefusal(str(response.get("text", "Native lease denied")),
                                                        response.get("code"))
                        except BaseException:
                            with sqlite3.connect(self.database_path, timeout=3) as db:
                                db.execute("DELETE FROM desktop_lease WHERE id=1 AND lease_id=?", (lease_id,))
                            _unlock(handle)
                            handle.close()
                            raise
                        self._owner, self._lease_id, self._handle = owner, lease_id, handle
                        self._proof=json.loads(json.dumps(proof or {}))
                        return lease_id
                    handle.close()
            await asyncio.sleep(.05)

    async def release(self, room_id, agent_id, workflow_id, session_id):
        owner = (room_id, agent_id, workflow_id, session_id)
        async with self._mutex:
            if self._owner != owner:
                return False
            if self._release_task is None:
                self._release_task = asyncio.create_task(self._release_owned(owner, self._lease_id,dict(self._proof)))
            task = self._release_task
        try:
            return await asyncio.wait_for(asyncio.shield(task), RELEASE_WAIT_SECONDS)
        except asyncio.TimeoutError:
            return None  # The owner and both locks remain held while native work settles.

    async def _release_owned(self, owner, lease_id, proof):
        while True:
            try:
                if self.native is not None:
                    response = await self.native.call({"operation": "lease_release", "room_id": owner[0],
                        "agent_id": owner[1], "workflow_id": owner[2], "lease_id": lease_id,
                        **{key:proof[key] for key in ('execution_binding','scope_deadline_ms') if key in proof}})
                    if response["state"] != "completed":
                        raise NativeRefusal(str(response.get("text", "Native lease release denied")),
                                            response.get("code"))
                break
            except (OSError, TimeoutError, ConnectionError, NativeRefusal):
                # The native server serializes release behind the current effect. A
                # timed-out request is never replayed as an effect; retry only this
                # owner-bound, idempotent control operation with a fresh deadline.
                await asyncio.sleep(RELEASE_RETRY_SECONDS)
        async with self._mutex:
            if self._owner != owner or self._lease_id != lease_id:
                raise RuntimeError("Desktop release owner changed before native acknowledgement")
            with sqlite3.connect(self.database_path, timeout=3) as db:
                db.execute("DELETE FROM desktop_lease WHERE id=1 AND lease_id=?", (lease_id,))
            handle = self._handle
            handle.seek(0)
            handle.truncate()
            handle.flush()
            _unlock(handle)
            handle.close()
            self._owner = self._handle = self._lease_id = None
            self._proof={}
            self._release_task = None
            return True

    async def close(self):
        if self._owner is not None and self._release_task is None:
            await self.release(*self._owner)
        task = self._release_task
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), RELEASE_SHUTDOWN_SECONDS)
            except asyncio.TimeoutError as error:
                raise RuntimeError("Native desktop lease release remains unconfirmed at shutdown") from error

class NativeDesktopClient:
    def __init__(self, endpoint: str, key: str):
        self.context_id = uuid4().hex
        self.endpoint, self.key = endpoint, bytes.fromhex(key)
        if len(self.key) != 32:
            raise ValueError("Invalid desktop IPC key")

    async def call(self, body, *, start_deadline_ms=None, preflight=None):
        from app.services.action_review import ReviewChanged
        deadline = int(datetime.now(timezone.utc).timestamp() * 1000) + 10_000
        if start_deadline_ms is not None:
            if type(start_deadline_ms) is not int:
                raise ReviewChanged("Native start deadline is unavailable")
            deadline = min(deadline, start_deadline_ms)
        if preflight is not None:
            preflight()
        payload = {**body, "version": 1, "request_id": uuid4().hex,
                   "deadline_ms": deadline}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        envelope = json.dumps({"body": base64.b64encode(raw).decode(),
                               "mac": hmac.new(self.key, raw, hashlib.sha256).hexdigest()}).encode()
        if len(envelope) > 1_100_000:
            raise ValueError("Native request too large")
        if preflight is not None or start_deadline_ms is not None:
            return await asyncio.to_thread(self._call_sync, envelope, preflight, deadline)
        return await asyncio.to_thread(self._call_sync, envelope)

    async def scope_capabilities(self):
        response=await self.call({'operation':'scope_capabilities'})
        try:payload=json.loads(response.get('text',''))
        except (ValueError,TypeError):return False
        return response.get('state')=='completed' and payload.get('capability')=='root_scope_revocation_v1'

    def _call_sync(self, envelope, preflight=None, deadline_ms=None):
        def before_send():
            from app.services.action_review import ReviewChanged
            if preflight is not None:
                preflight()
            if deadline_ms is not None and int(datetime.now(timezone.utc).timestamp() * 1000) >= deadline_ms:
                raise ReviewChanged("Native operation start deadline expired")
        if sys.platform == "win32":
            from multiprocessing.connection import Client
            with Client(self.endpoint, family="AF_PIPE") as pipe:
                before_send()
                pipe.send_bytes(envelope)
                response = pipe.recv_bytes(15_000_000)
        else:
            with socket.socket(socket.AF_UNIX) as stream:
                stream.settimeout(12)
                stream.connect(self.endpoint)
                before_send()
                stream.sendall(struct.pack(">I", len(envelope)) + envelope)
                header = _read_exact(stream, 4)
                length = struct.unpack(">I", header)[0]
                if length > 15_000_000:
                    raise ValueError("Native response too large")
                response = _read_exact(stream, length)
        result = json.loads(response)
        if not isinstance(result, dict) or result.get("state") not in ("completed", "failed", "uncertain"):
            raise ValueError("Invalid native response")
        return result


def _read_exact(stream, count):
    chunks = bytearray()
    while len(chunks) < count:
        part = stream.recv(count - len(chunks))
        if not part:
            raise ConnectionError("Native desktop bridge disconnected")
        chunks.extend(part)
    return bytes(chunks)


def _validate_png_chunks(image):
    """Complement Pillow's decode with CRC validation through the final IEND."""
    offset = 8
    seen_data = False
    for _ in range(4096):
        if offset + 12 > len(image):
            raise ValueError("Invalid native screenshot")
        length = struct.unpack_from(">I", image, offset)[0]
        end = offset + 12 + length
        if end > len(image):
            raise ValueError("Invalid native screenshot")
        kind = image[offset + 4:offset + 8]
        expected = struct.unpack_from(">I", image, end - 4)[0]
        if zlib.crc32(image[offset + 4:end - 4]) != expected:
            raise ValueError("Invalid native screenshot")
        if kind == b"IDAT":
            seen_data = True
        if kind == b"IEND":
            if length != 0 or end != len(image) or not seen_data:
                raise ValueError("Invalid native screenshot")
            return
        offset = end
    raise ValueError("Invalid native screenshot")


class DesktopExecutor:
    def __init__(self, database_path, *, native=None, app_data=None, lock_path=None,
                 sessions=None, owner_epochs=None):
        self.native = native
        self.sessions = sessions
        self.owner_epochs = owner_epochs
        self.lease = DesktopLeaseManager(database_path, lock_path, native)
        self.app_data = Path(app_data or os.environ.get("GAMJA_APP_DATA_DIR", "."))
        self._scope_ready=False
        self._scope_adapter=None
        self._scope_binding=None
        self._scope_lease_id=None
        self._scope_revoked={}
        self._scope_releases={}

    @property
    def scoped_execution_ready(self):
        return self.native is not None and self.native is self._scope_adapter and self._scope_ready

    async def prepare_scoped_execution(self):
        if self.native is None:return False
        try:self._scope_ready=await self.native.scope_capabilities()
        except (OSError,ValueError,AttributeError,TimeoutError):self._scope_ready=False
        self._scope_adapter=self.native
        return self.scoped_execution_ready

    async def __call__(self, action, grant, cancel):
        from app.services.tool_broker import ActionResult
        def detail(code, error=None):
            return make_diagnostic(error, stage="tool_execution", failure_code=code,
                                   room_id=action.room_id, agent_id=action.agent_id,
                                   connection_id=None,
                                   trigger_event_id=getattr(action, "trigger_event_id", None),
                                   action_id=action.action_id)
        if self.native is None:
            return ActionResult("failed", "Native desktop control is unavailable in this launch.",
                                error_code="native_capability_unavailable",
                                diagnostic=detail("native_capability_unavailable"))
        if not action.workflow_id:
            return ActionResult("failed", "Desktop action requires a workflow ID.")
        from app.services.resource_authority import require_scope_start
        binding=require_scope_start(self.sessions,action,grant)
        if binding is not None and not self.scoped_execution_ready:
            return ActionResult('failed','Native scoped execution is unavailable.',error_code='native_capability_unavailable')
        from app.services.action_review import ReviewChanged
        from app.services.grants import OPAQUE_ACTIONS
        from app.services.review_operation import operation_start
        # Direct non-mutation executor fixtures retain their existing no-DB
        # behavior. Production always supplies sessions. Opaque inputs never
        # have a missing-review fallback, including direct launches.
        guarded = self.sessions is not None or action.family in OPAQUE_ACTIONS
        adapter = self.native
        context_id = getattr(adapter, "context_id", None)

        def checked_start(lease_id=None):
            if cancel.is_set() or self.native is not adapter or self.lease.native is not adapter:
                raise ReviewChanged("Native operation context changed")
            if binding is not None:
                require_scope_start(self.sessions,action,grant)
                if binding.affected(self._scope_revoked):raise ReviewChanged('Native resource scope revoked')
            if lease_id is not None and not self.lease.owns((
                    action.room_id, action.agent_id, action.workflow_id, grant.session_id), lease_id):
                raise ReviewChanged("Native operation lease changed")
            if getattr(adapter, "context_id", None) != context_id:
                raise ReviewChanged("Native adapter identity changed")
            return operation_start(action, grant, sessions=self.sessions,
                owner_epochs=self.owner_epochs, context_id=context_id)

        if guarded:
            try:
                checked_start()
            except ReviewChanged:
                return ActionResult("failed", "Native operation review changed.", error_code="review_changed")
        owner = self.lease._owner
        if owner and owner[:2]==(action.room_id,action.agent_id) and self._scope_binding!=binding:
            await self.lease.release(*owner)
            if self.lease._owner is not None:
                return ActionResult('uncertain','Prior desktop lease release is not confirmed.',error_code='native_lease_unconfirmed')
        if owner and owner != (action.room_id, action.agent_id, action.workflow_id, grant.session_id):
            if owner[:2] == (action.room_id, action.agent_id):
                await self.lease.release(*owner)
        proof = {"action_id": action.action_id, "grant_id": grant.grant_id,
                 "grant_scope": grant.scope, "payload_digest": action.payload_digest}
        if binding is not None:
            proof.update(execution_binding=binding.as_dict(),scope_deadline_ms=binding.deadline_ms)
        try:
            lease_id = await self.lease.acquire(action.room_id, action.agent_id, action.workflow_id,
                                                 grant.session_id, cancel, proof)
            self._scope_binding,self._scope_lease_id=binding,lease_id
        except (FileNotFoundError, ConnectionRefusedError):
            # The helper is known absent before any effect dispatch. A lost
            # response after an action remains uncertain in the separate path.
            return ActionResult("failed", "Native desktop control is unavailable in this launch.",
                                error_code="native_capability_unavailable",
                                diagnostic=detail("native_capability_unavailable"))
        except NativeRefusal as error:
            return ActionResult("failed", str(error), error_code=error.code,
                                diagnostic=detail(error.code))
        except (OSError, ValueError, sqlite3.Error) as error:
            return ActionResult("uncertain", "Native desktop lease could not be confirmed.",
                                error_code="native_lease_unconfirmed",
                                diagnostic=detail("native_lease_unconfirmed", error))
        if cancel.is_set():
            return ActionResult("cancelled", "Desktop action cancelled before native dispatch.")
        try:
            deadline = checked_start(lease_id) if guarded else None
            body = {"operation": "action", "lease_id": lease_id,
                "room_id": action.room_id, "agent_id": action.agent_id,
                "workflow_id": action.workflow_id, "action_id": action.action_id,
                "grant_id": grant.grant_id, "grant_scope": grant.scope,
                "payload_digest": action.payload_digest,
                "family": action.family, "arguments": action.arguments}
            if binding is not None:
                body.update(execution_binding=binding.as_dict(),scope_deadline_ms=binding.deadline_ms)
            if guarded:
                result = await adapter.call(body, start_deadline_ms=deadline,
                    preflight=lambda: checked_start(lease_id))
            else:
                result = await adapter.call(body)
        except ReviewChanged:
            # No action bytes were sent when the backend preflight refused.
            # Release only this confirmed owner; native release remains
            # serialized behind any earlier effect and keeps existing retry.
            await self.lease.release(action.room_id, action.agent_id, action.workflow_id, grant.session_id)
            return ActionResult("failed", "Native operation review changed before dispatch.",
                                error_code="review_changed")
        except (OSError, ValueError) as error:
            return ActionResult("uncertain", "Native desktop response could not be confirmed.",
                                error_code="native_response_unconfirmed",
                                diagnostic=detail("native_response_unconfirmed", error))
        if result["state"] != "completed" or action.family != "screenshot":
            code = result.get("code")
            return ActionResult(result["state"], str(result.get("text", ""))[:16000],
                                error_code=code, diagnostic=detail(code) if code else None)
        try:
            image = base64.b64decode(result["png_base64"], validate=True)
            width, height = validate_screenshot_png(image, (result.get("width"), result.get("height")))
        except (KeyError, TypeError, ValueError, OSError, SyntaxError, binascii.Error, struct.error) as error:
            return ActionResult("uncertain", "Native screen image was invalid; no screenshot was saved.",
                                error_code="native_screenshot_invalid",
                                diagnostic=detail("native_screenshot_invalid", error))
        try:
            ref = save_screenshot_png(self.app_data, action, image)
        except (OSError, ValueError) as error:
            return ActionResult("uncertain", "Screen image storage could not be confirmed in this app's data directory.",
                                error_code="screenshot_artifact_unavailable",
                                diagnostic=detail("screenshot_artifact_unavailable", error))
        return ActionResult("completed", str(result.get("text", "Screen captured."))[:15000] +
                            " Image available to vision-capable Agent.",
                            screenshot_ref=ref,
                            image_bytes=image, image_mime="image/png", image_width=width, image_height=height)

    async def release_turn(self, room_id, agent_id, *, generation=None, workflow_id=None):
        owner = self.lease._owner
        if self._scope_binding is not None and (generation is None or self._scope_binding.run_generation!=generation):return
        if workflow_id is not None and owner and owner[2]!=workflow_id:return
        if owner and owner[:2] == (room_id, agent_id):
            await self.lease.release(*owner)

    async def stop_scopes(self, bindings):
        from app.services.resource_binding import revocation_map
        revoked=revocation_map(bindings)
        self._scope_revoked.update({scope:max(epoch,self._scope_revoked.get(scope,-1)) for scope,epoch in revoked.items()})
        key=tuple(sorted(revoked.items()))
        task=self._scope_releases.get(key)
        if task is not None and task.done() and not task.cancelled() and task.exception() is not None:task=None
        if task is None:
            task=asyncio.create_task(self._revoke_scopes(key))
            self._scope_releases[key]=task
        try:
            await asyncio.wait_for(asyncio.shield(task),RELEASE_WAIT_SECONDS)
            state,code='settled',None
        except (asyncio.TimeoutError,OSError,ValueError,TypeError,AttributeError,NativeRefusal):
            state,code='unknown','native_scope_revocation_unconfirmed'
        return [{'scope_id':scope,'epoch':epoch,'state':state,**({'code':code} if code else {})} for scope,epoch in key]

    async def _revoke_scopes(self,key):
        if not self.scoped_execution_ready:raise NativeRefusal('Native scope revocation unavailable')
        expected=[{'scope_id':scope,'epoch':epoch} for scope,epoch in key]
        while True:
            try:
                response=await self.native.call({'operation':'scope_revoke','revocations':expected})
                payload=json.loads(response.get('text',''))
                if (response.get('state')!='completed' or payload.get('capability')!='root_scope_revocation_v1'
                        or payload.get('revocations')!=expected):
                    raise NativeRefusal('Native scope acknowledgement mismatch')
                if self._scope_binding and self._scope_binding.affected(dict(key)):
                    # Native revoke already released this exact lease. The
                    # ordinary release retry is owner-bound and idempotent.
                    owner=self.lease._owner
                    if owner and self.lease._lease_id==self._scope_lease_id:
                        result=await self.lease.release(*owner)
                        if result is not True:raise NativeRefusal('Local lease release still pending')
                return
            except (OSError,ValueError,TypeError,AttributeError,TimeoutError,NativeRefusal):
                await asyncio.sleep(RELEASE_RETRY_SECONDS)

    async def close(self):
        await self.lease.close()
        pending=[task for task in self._scope_releases.values() if not task.done()]
        if pending:
            await asyncio.wait_for(asyncio.shield(asyncio.gather(*pending)),RELEASE_SHUTDOWN_SECONDS)
