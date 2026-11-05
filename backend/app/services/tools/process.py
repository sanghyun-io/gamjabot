"""Explicit-argv processes with owned, bounded lifetime and tree cleanup."""

import asyncio
import os
import select
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from uuid import uuid4

from app.services.diagnostic_detail import make_diagnostic
from app.services.tool_broker import ActionResult

GUARDIAN_READY_SECONDS = 30


def launch_review(action, grant):
    """Use only the broker-retained typed review, never caller metadata."""
    from app.services.review_snapshot import ReviewSnapshot, _binding
    from app.services.review_execution import canonical_review_id, execution_supported
    from app.services.tool_broker import prepare
    review = grant.review
    if (not isinstance(review, ReviewSnapshot) or not canonical_review_id(grant.review_id)
            or action.review_id != grant.review_id or review.action != _binding(action)
            or (grant.room_id, grant.agent_id, grant.session_id) !=
               (action.room_id, action.agent_id, action.session_id)
            or review.authority.euid != os.geteuid()):
        raise ValueError("Process review changed")
    execution_supported(review)
    prepared = prepare(action.family, action.arguments, action.workflow_id)
    if any(getattr(prepared, k) != getattr(review.action, k) for k in
           ("family", "target", "scope_target", "expected_effect", "payload_digest")):
        raise ValueError("Process payload changed")
    return {"version": 1, "action_id": action.action_id, "review_id": grant.review_id,
            "euid": review.authority.euid,
            "targets": [t.model_dump(mode="json") for t in review.targets]}


def child_environment(approved=None):
    if sys.platform == "win32":
        baseline = ("SystemRoot", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE")
    else:
        baseline = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "HOME")
    result = {key: os.environ[key] for key in baseline if key in os.environ}
    for key, value in (approved or {}).items():
        upper = key.upper()
        if (not key.isidentifier() or not isinstance(value, str) or "\0" in value
                or upper in {name.upper() for name in baseline}
                or upper.startswith(("GAMJA_", "CODEX_", "OPENAI_", "ANTHROPIC_", "GOOGLE_", "XAI_",
                                     "PYTHON", "DYLD_", "LD_"))
                or any(word in upper for word in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "SIGNING", "JWT", "IPC", "SESSION"))):
            raise ValueError("Reserved or invalid environment name")
        result[key] = value
    return result


@dataclass
class OwnedProcess:
    handle: str
    process: subprocess.Popen
    room_id: int
    agent_id: int
    grant_id: int | None
    approval_scope: str | None
    session_id: str | None
    workflow_id: str | None
    generation: int | None
    deadline: float
    control_fd: int | None
    monitor: asyncio.Task | None = None
    authority_ref: object | None = None
    execution_binding: object | None = None


class ProcessExecutor:
    def __init__(self, sessions=None, *, owner_epochs=None):
        self.sessions = sessions
        self.owner_epochs = owner_epochs
        self.running = {}
        self._starting_cleanup = set()

    async def __call__(self, action, grant, cancel):
        def detail(code, error=None):
            return make_diagnostic(
                error, stage="tool_execution", failure_code=code,
                room_id=action.room_id, agent_id=action.agent_id,
                connection_id=None, trigger_event_id=getattr(action, "trigger_event_id", None),
                action_id=getattr(action, "action_id", None))

        if action.family == "cancel_process":
            owned = self.running.get(action.arguments["process_id"])
            if (owned is None or (owned.room_id, owned.agent_id) != (action.room_id, action.agent_id)
                    or owned.session_id != grant.session_id or owned.workflow_id != action.workflow_id):
                return ActionResult("failed", "Process handle is not owned by this room and Agent.")
            await self._stop(owned)
            return ActionResult("completed", f"Stopped owned process {owned.handle}.")
        args = action.arguments
        if cancel.is_set():
            return ActionResult("cancelled", "Action cancelled before launch.")
        lifetime = args.get("background_seconds")
        if lifetime is not None and not 1 <= lifetime <= 300:
            return ActionResult("failed", "Invalid background lifetime.")
        try:
            if sys.platform == "win32":
                # The current trusted observer has no Windows version proof.
                return ActionResult("failed", "Trusted process review is unsupported.", error_code="review_changed")
            try:
                from app.services.tools.launch_review import validate, protected_review, checked_targets
                review = launch_review(action, grant)
                validate(review, [args["executable"]])
                with checked_targets(review):
                    pass
            except (OSError, ValueError, TypeError, AttributeError):
                return ActionResult("failed", "Process review changed or is unavailable.", error_code="review_changed")
            environment = child_environment(args.get("env"))
            cwd = args.get("cwd") or os.getcwd()
            if not os.path.isdir(cwd):
                return ActionResult("failed", "Working directory is unavailable.")
            generation = self._generation(action.room_id, action.agent_id)
            from app.services.resource_authority import require_scope_start
            execution_binding=require_scope_start(self.sessions,action,grant)
            if self.sessions is not None and (grant.run_generation is None or grant.run_generation != generation):
                return ActionResult("failed", "Process run changed before launch.", error_code="review_changed")
            if sys.platform == "win32":
                from app.services.tools.windows import launch_job_process
                process, control_fd = await asyncio.to_thread(
                    launch_job_process, args["executable"], args.get("argv", []), cwd, environment,
                    lifetime or 3600)
            else:
                read_fd, write_fd = os.pipe()
                ready_read, ready_write = os.pipe()
                os.set_inheritable(write_fd, False)
                try:
                    entry = ([sys.executable, "--process-guardian"] if getattr(sys, "frozen", False)
                             else [sys.executable, "-B", os.path.join(os.path.dirname(__file__), "guardian.py")])
                    with protected_review(review) as review_fd:
                        process = subprocess.Popen(
                            [*entry, str(read_fd), str(ready_write), str(review_fd),
                             str(time.monotonic() + (lifetime or 3600)), args["executable"], *args.get("argv", [])],
                            cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            close_fds=True, pass_fds=(read_fd, ready_write, review_fd), start_new_session=True)
                except BaseException:
                    os.close(write_fd)
                    os.close(ready_read)
                    raise
                finally:
                    os.close(read_fd)
                    os.close(ready_write)
                control_fd = write_fd
                ready_task = asyncio.create_task(asyncio.to_thread(_guardian_ready, ready_read))
                defer_ready_close = False
                try:
                    while not ready_task.done():
                        if cancel.is_set():
                            self._abandon_start(process, control_fd)
                            control_fd = None
                        if control_fd is None:
                            break
                        await asyncio.sleep(.02)
                    ready = await asyncio.shield(ready_task)
                except asyncio.CancelledError:
                    if control_fd is not None:
                        os.close(control_fd)
                        control_fd = None
                    # Withdraw promptly even during frozen startup. Retain
                    # the ready FD until its reader has finished, and keep
                    # cleanup tracked so close() also waits for reaping.
                    cleanup = asyncio.create_task(_finish_abandoned_start(process, ready_task, ready_read))
                    self._starting_cleanup.add(cleanup)
                    cleanup.add_done_callback(self._starting_cleanup.discard)
                    defer_ready_close = True
                    raise
                finally:
                    if not defer_ready_close:
                        os.close(ready_read)
                if control_fd is None:
                    return ActionResult("uncertain", "Process launch was withdrawn; earlier effects are unknown.")
                if ready != b"1":
                    self._abandon_start(process, control_fd)
                    control_fd = None
                    if ready in (b"N", b"P", b"0", b"R"):
                        code, text = {
                            b"N": ("process_executable_not_found", "Executable was not found. Use its absolute path."),
                            b"P": ("process_executable_not_permitted", "Executable cannot be run with this app's permissions."),
                            b"0": ("process_launch_failed", "Could not launch owned process."),
                            b"R": ("review_changed", "Process review changed before launch."),
                        }[ready]
                        return ActionResult("failed", text, error_code=code, diagnostic=detail(code))
                    return ActionResult("uncertain", "Owned process launch stopped; earlier effects are unknown.",
                                        error_code="process_launch_unconfirmed")
            handle = uuid4().hex
            owned = OwnedProcess(handle, process, action.room_id, action.agent_id, grant.grant_id,
                                 grant.scope, grant.session_id, action.workflow_id, generation,
                                 time.monotonic() + (lifetime or 3600), control_fd)
            owned.authority_ref = grant
            owned.execution_binding=execution_binding
            self.running[handle] = owned
            owned.monitor = asyncio.create_task(self._monitor(owned))
            if lifetime is not None:
                return ActionResult("completed", f"Owned process handle {handle} started; maximum lifetime {lifetime} seconds.")
            while owned.handle in self.running:
                if cancel.is_set():
                    await self._stop(owned)
                    return ActionResult("uncertain", "Process stopped after start; earlier effects are unknown.")
                await asyncio.sleep(.05)
            output = getattr(owned, "output", b"")
            code = process.returncode
            text = output.decode("utf-8", "replace")[:16000].strip()
            for value in (args.get("env") or {}).values():
                if value:
                    text = text.replace(value, "[approved environment value redacted]")
            return ActionResult("completed" if code == 0 else "failed", text)
        except asyncio.CancelledError:
            if "owned" in locals():
                await asyncio.shield(self._stop(owned))
            elif "process" in locals():
                if control_fd is not None:
                    self._abandon_start(process, control_fd)
            raise
        except (OSError, ValueError, RuntimeError) as error:
            if "process" in locals() and "owned" not in locals():
                if "control_fd" in locals() and control_fd is not None:
                    self._abandon_start(process, control_fd)
                return ActionResult("uncertain", "Owned process launch stopped; earlier effects are unknown.",
                                    error_code="process_launch_unconfirmed",
                                    diagnostic=detail("process_launch_unconfirmed", error))
            return ActionResult("failed", "Could not launch owned process.",
                                error_code="process_guardian_launch_failed",
                                diagnostic=detail("process_guardian_launch_failed", error))

    def _generation(self, room_id, agent_id):
        from app.services.resource_authority import run_generation
        return run_generation(self.sessions, room_id, agent_id)

    def _authorized(self, owned):
        return resource_authorized(self.sessions, owned, owner_epochs=self.owner_epochs)

    async def _monitor(self, owned):
        proc = owned.process
        # Pipe draining runs independently of the broker's one-shot budget.
        drain_stop = threading.Event()
        reader = asyncio.create_task(asyncio.to_thread(_drain_output, proc, drain_stop))
        try:
            while proc.poll() is None:
                if time.monotonic() >= owned.deadline or not self._authorized(owned):
                    await self._terminate(owned)
                    break
                await asyncio.sleep(.1)
            if sys.platform == "win32":
                await asyncio.to_thread(proc.terminate_job)
            drain_stop.set()
            owned.output = await reader
        finally:
            drain_stop.set()
            if not reader.done():
                await reader
            if proc.stdout is not None:
                proc.stdout.close()
            self.running.pop(owned.handle, None)
            if owned.control_fd is not None:
                os.close(owned.control_fd)
                owned.control_fd = None
            if sys.platform == "win32":
                await asyncio.to_thread(proc.close)

    async def _terminate(self, owned):
        if owned.process.poll() is not None:
            return
        if sys.platform == "win32":
            await asyncio.to_thread(owned.process.terminate_job)
        else:
            if owned.control_fd is not None:
                os.close(owned.control_fd)
                owned.control_fd = None
            try:
                await asyncio.wait_for(asyncio.to_thread(owned.process.wait), 1)
            except asyncio.TimeoutError:
                try:
                    os.killpg(owned.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    async def _stop(self, owned):
        await self._terminate(owned)
        if owned.monitor:
            await owned.monitor

    async def close(self):
        await asyncio.gather(*(self._stop(owned) for owned in list(self.running.values())))
        await asyncio.gather(*list(self._starting_cleanup))

    def _abandon_start(self, process, control_fd):
        os.close(control_fd)
        cleanup = asyncio.create_task(asyncio.to_thread(_reap_abandoned, process))
        self._starting_cleanup.add(cleanup)
        cleanup.add_done_callback(self._starting_cleanup.discard)

    async def stop_room(self, room_id, agent_id=None):
        await asyncio.gather(*(self._stop(owned) for owned in list(self.running.values())
                               if owned.room_id == room_id and (agent_id is None or owned.agent_id == agent_id)))

    async def stop_grant(self, grant_id):
        await asyncio.gather(*(self._stop(owned) for owned in list(self.running.values())
                               if owned.grant_id == grant_id))

    async def stop_scopes(self, bindings):
        from app.services.resource_binding import revocation_map
        revoked=revocation_map(bindings)
        await asyncio.gather(*(self._stop(owned) for owned in list(self.running.values())
            if owned.execution_binding and owned.execution_binding.affected(revoked)))


def resource_authorized(sessions, owned, *, owner_epochs=None):
    from app.services.resource_authority import resource_live
    ref = getattr(owned, "authority_ref", None)
    if sessions is not None:
        try:
            from app.services.resource_binding import freeze_binding
            if freeze_binding(getattr(ref,'execution_binding',None)) != getattr(owned,'execution_binding',None):
                return False
            original = ref.review.action
            if ((owned.room_id, owned.agent_id, owned.session_id, owned.workflow_id,
                 owned.grant_id, owned.approval_scope) !=
                    (original.room_id, original.agent_id, original.session_id,
                     original.workflow_id, ref.grant_id, ref.scope)):
                return False
        except (AttributeError, TypeError, ValueError):
            return False
    return resource_live(sessions, ref,
                         getattr(owned, "generation", None), owner_epochs=owner_epochs)


def _drain_output(process, stop):
    chunks = []
    remaining = 16000
    fd = process.stdout.fileno()
    if sys.platform != "win32":
        os.set_blocking(fd, False)
        while True:
            ready, _, _ = select.select([fd], [], [], 0 if stop.is_set() else .1)
            if not ready:
                if stop.is_set():
                    break
                continue
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if not data:
                break
            if remaining:
                chunks.append(data[:remaining])
                remaining -= min(len(data), remaining)
            if stop.is_set():
                break
    else:
        while data := process.stdout.read(65536):
            if remaining:
                chunks.append(data[:remaining])
                remaining -= min(len(data), remaining)
    return b"".join(chunks)


def _guardian_ready(fd):
    ready, _, _ = select.select([fd], [], [], GUARDIAN_READY_SECONDS)
    return os.read(fd, 1) if ready else b""


async def _finish_abandoned_start(process, ready_task, ready_fd):
    try:
        while not ready_task.done():
            try:
                await asyncio.shield(ready_task)
            except asyncio.CancelledError:
                pass
        ready_task.result()
    finally:
        os.close(ready_fd)
        waiter = asyncio.create_task(asyncio.to_thread(_reap_abandoned, process))
        while not waiter.done():
            try:
                await asyncio.shield(waiter)
            except asyncio.CancelledError:
                pass
        waiter.result()


def _reap_abandoned(process):
    # The guardian must finish its child-group cleanup. Never kill only its
    # group here, which could strand the child's separate session.
    try:
        process.wait()
    finally:
        if process.stdout is not None:
            process.stdout.close()
