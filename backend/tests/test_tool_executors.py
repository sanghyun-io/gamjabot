import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.grants import GrantRef
from app.services.tools import LocalExecutor as RealLocalExecutor
from app.services.tools import files as file_tools
from app.services.tool_broker import TOOL_SCHEMAS, prepare
from app.services.providers.base import normalized_call, wire_tool_name


class LocalExecutor(RealLocalExecutor):
    """Test-only retained reviews for direct legacy effect fixtures.

    Production effects still reject missing reviews. These direct fixture
    reviews are not DB/account authorization or installed native evidence.
    """
    async def __call__(self, action, grant, cancel):
        if action.family in ("read_file", "write_file", "delete_file", "move_file"):
            from test_conditional_files import reviewed
            action, grant = reviewed(action.family, action.arguments, grant.scope or "once")
        elif action.family == "start_process":
            from test_process_review import reviewed_process
            action, grant = reviewed_process(action, grant)
        elif action.family == "browser_interact":
            from dataclasses import replace
            from app.services.grants import now
            from test_process_review import reviewed_process
            action, grant = reviewed_process(action, grant, executor=self)
            grant = replace(grant, approved_at=now())
        elif action.family == "cancel_process":
            from dataclasses import replace
            from uuid import uuid5, NAMESPACE_URL
            grant = replace(grant, session_id=str(uuid5(NAMESPACE_URL, grant.session_id or "owned-process-fixture")))
        return await super().__call__(action, grant, cancel)


def _live_process(pid):
    status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return status.returncode == 0 and bool(status.stdout.strip()) and not status.stdout.strip().startswith("Z")


def action(family, room=1, agent=2, **arguments):
    return SimpleNamespace(family=family, room_id=room, agent_id=agent,
                           action_id=f"{family}-fixture", arguments=arguments,
                           workflow_id="fixture")


def test_provider_wire_carries_workflow_and_opaque_process_handle():
    url = "http://127.0.0.1:9876/"
    call = normalized_call("native-1", wire_tool_name("browser_navigate"),
                           {"url": url, "workflow_id": "research-1"}, TOOL_SCHEMAS)
    assert call.workflow_id == "research-1"
    assert call.arguments == {"url": url}
    assert prepare(call.family, call.arguments, call.workflow_id).raw_arguments == {"url": url}
    handle = "a" * 32
    cancelled = normalized_call("native-2", wire_tool_name("cancel_process"),
                                {"process_id": handle, "workflow_id": "research-1"}, TOOL_SCHEMAS)
    assert cancelled.arguments["process_id"] == handle
    assert cancelled.workflow_id == "research-1"


def test_process_consent_rejects_reserved_environment_names():
    with pytest.raises(ValueError):
        prepare("start_process", {"executable": "screencapture"})
    with pytest.raises(ValueError):
        prepare("start_process", {"executable": sys.executable,
                                  "env": {"GAMJA_LOCAL_SESSION_TOKEN": "synthetic"}})
    with pytest.raises(ValueError):
        prepare("start_process", {"executable": sys.executable,
                                  "env": {"DYLD_INSERT_LIBRARIES": "synthetic"}})


@pytest.mark.asyncio
async def test_process_uses_clean_environment_and_reports_output(monkeypatch, tmp_path):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "private-synthetic-value")
    executor = LocalExecutor()
    command = "import os; print('private-synthetic-value' in str(os.environ)); print('process complete')"
    result = await executor(action("start_process", executable=sys.executable,
                                   argv=["-c", command], cwd=str(tmp_path)), GrantRef(), asyncio.Event())
    assert result.state == "completed"
    assert result.text == "False\nprocess complete"


@pytest.mark.asyncio
async def test_process_reports_launch_failure_without_owned_handle(tmp_path):
    executor = LocalExecutor()
    result = await executor(action("start_process", executable=str(tmp_path / "missing"),
                                   cwd=str(tmp_path), background_seconds=5), GrantRef(), asyncio.Event())
    assert result.state == "failed"
    if sys.platform != "win32":
        assert result.error_code == "review_changed"
        assert result.text == "Process review changed or is unavailable."
    assert not executor.processes.running


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX guardian reports launch errno")
@pytest.mark.asyncio
async def test_process_reports_non_executable_target_without_exposing_path(tmp_path):
    blocked = tmp_path / "private-target"
    blocked.write_text("not executable")
    blocked.chmod(0o600)
    executor = LocalExecutor()
    result = await executor(action("start_process", executable=str(blocked),
                                   cwd=str(tmp_path), background_seconds=5), GrantRef(), asyncio.Event())
    assert result.state == "failed"
    assert result.error_code == "process_executable_not_permitted"
    assert str(blocked) not in result.text
    assert not executor.processes.running


@pytest.mark.asyncio
async def test_process_cancel_reaps_owned_descendant(tmp_path):
    executor = LocalExecutor()
    pid_path = tmp_path / "child.pid"
    command = ("import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
               "open(sys.argv[1],'w').write(str(p.pid)); time.sleep(60)")
    cancelled = asyncio.Event()
    running = asyncio.create_task(executor(action("start_process", executable=sys.executable,
                                                 argv=["-c", command, str(pid_path)], cwd=str(tmp_path)),
                                          GrantRef(), cancelled))
    try:
        for _ in range(100):
            if pid_path.exists():
                break
            await asyncio.sleep(.05)
        assert pid_path.exists()
        cancelled.set()
        result = await asyncio.wait_for(running, 5)
        assert result.state == "uncertain"
        assert not executor.processes.running
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_path.read_text()), 0)
    finally:
        cancelled.set()
        await executor.close()


@pytest.mark.asyncio
async def test_task_cancellation_waits_for_owned_process_cleanup(tmp_path):
    executor = LocalExecutor()
    pid_path = tmp_path / "owned.pid"
    command = "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(60)"
    running = asyncio.create_task(executor(action("start_process", executable=sys.executable,
                                                 argv=["-c", command, str(pid_path)], cwd=str(tmp_path)),
                                          GrantRef(), asyncio.Event()))
    try:
        for _ in range(100):
            if pid_path.exists():
                break
            await asyncio.sleep(.02)
        assert pid_path.exists()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert not executor.processes.running
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_path.read_text()), 0)
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_background_process_has_opaque_handle_and_same_agent_cancel(tmp_path):
    executor = LocalExecutor()
    command = "import time; time.sleep(60)"
    try:
        started = await executor(action("start_process", executable=sys.executable,
                                        argv=["-c", command], cwd=str(tmp_path),
                                        background_seconds=5), GrantRef(), asyncio.Event())
        assert started.state == "completed"
        handle = next(iter(executor.processes.running))
        assert len(handle) == 32 and handle.isalnum()
        assert (await executor(action("cancel_process", agent=3, process_id=handle),
                               GrantRef(), asyncio.Event())).state == "failed"
        assert (await executor(action("cancel_process", process_id=handle),
                               GrantRef(), asyncio.Event())).state == "completed"
        assert not executor.processes.running
        replacement = LocalExecutor()
        assert (await replacement(action("cancel_process", process_id=handle),
                                  GrantRef(), asyncio.Event())).state == "failed"
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_background_process_expires(tmp_path):
    executor = LocalExecutor()
    try:
        result = await executor(action("start_process", executable=sys.executable,
                                       argv=["-c", "import time; time.sleep(60)"], cwd=str(tmp_path),
                                       background_seconds=1), GrantRef(), asyncio.Event())
        assert result.state == "completed"
        for _ in range(40):
            if not executor.processes.running:
                break
            await asyncio.sleep(.05)
        assert not executor.processes.running
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_revoke_and_room_stop_release_owned_processes(tmp_path):
    executor = LocalExecutor()
    async def start(grant_id):
        result = await executor(action("start_process", executable=sys.executable,
                                       argv=["-c", "import time; time.sleep(60)"], cwd=str(tmp_path),
                                       background_seconds=20),
                                GrantRef(grant_id=grant_id, scope="room_session", session_id="launch"),
                                asyncio.Event())
        assert result.state == "completed"
    try:
        await start(10)
        await executor.stop_grant(10)
        assert not executor.processes.running
        await start(11)
        await executor.stop_room(1)
        assert not executor.processes.running
    finally:
        await executor.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX guardian test")
def test_guardian_ends_child_after_backend_crash(tmp_path):
    pid_path = tmp_path / "crash-child.pid"
    script = f"""
import asyncio, os, sys
from types import SimpleNamespace
from app.services.tools import LocalExecutor
from app.services.grants import GrantRef
from test_process_review import reviewed_process
async def main():
    executor = LocalExecutor()
    command = "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(60)"
    action = SimpleNamespace(family='start_process', room_id=1, agent_id=2, action_id='crash-fixture',
        arguments={{'executable':sys.executable,'argv':['-c',command,{str(pid_path)!r}],
                   'cwd':{str(tmp_path)!r},'background_seconds':5}}, workflow_id=None)
    action, ref = reviewed_process(action, GrantRef())
    result = await executor(action, ref, asyncio.Event())
    assert result.state == 'completed'
    for _ in range(100):
        if os.path.exists({str(pid_path)!r}): break
        await asyncio.sleep(.01)
    os._exit(0)
asyncio.run(main())
"""
    subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                   check=True, timeout=10)
    child_pid = int(pid_path.read_text())
    for _ in range(100):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        import time
        time.sleep(.02)
    else:
        os.kill(child_pid, 9)
        pytest.fail("Guardian did not end owned child after backend crash")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX frozen guardian fixture")
@pytest.mark.asyncio
async def test_slow_frozen_guardian_timeout_prevents_late_child_without_touching_bystander(
        monkeypatch, tmp_path):
    from app.services.tools import process as process_tools

    backend_dir = Path(__file__).resolve().parents[1]
    frozen = tmp_path / "slow-frozen-backend"
    frozen.write_text(f"#!{sys.executable}\n"
                      "import sys,time\n"
                      "time.sleep(1.5)\n"
                      f"sys.path.insert(0,{str(backend_dir)!r})\n"
                      "from app.services.tools.guardian import main\n"
                      "sys.argv=[sys.argv[0],*sys.argv[2:]]\n"
                      "sys.exit(main())\n")
    frozen.chmod(0o700)
    monkeypatch.setattr(process_tools, "sys", SimpleNamespace(
        platform=sys.platform, frozen=True, executable=str(frozen)))
    monkeypatch.setattr(process_tools, "GUARDIAN_READY_SECONDS", 1)

    pid_path = tmp_path / "late-child.pid"
    command = "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)"
    bystander = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        executor = LocalExecutor()
        result = await executor(action("start_process", executable=sys.executable,
                                       argv=["-c", command, str(pid_path)], cwd=str(tmp_path),
                                       background_seconds=20), GrantRef(), asyncio.Event())
        assert result.state == "uncertain"
        assert not executor.processes.running
        await asyncio.sleep(1)
        assert not pid_path.exists(), "withdrawn frozen launch started a late child"
        assert _live_process(bystander.pid), "unrelated process was terminated"
    finally:
        bystander.terminate()
        bystander.wait(timeout=2)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX frozen guardian fixture")
@pytest.mark.asyncio
async def test_slow_frozen_guardian_within_readiness_budget_starts_and_stops(monkeypatch, tmp_path):
    from app.services.tools import process as process_tools

    backend_dir = Path(__file__).resolve().parents[1]
    frozen = tmp_path / "slow-frozen-backend"
    frozen.write_text(f"#!{sys.executable}\n"
                      "import sys,time\n"
                      "time.sleep(5.5)\n"
                      f"sys.path.insert(0,{str(backend_dir)!r})\n"
                      "from app.services.tools.guardian import main\n"
                      "sys.argv=[sys.argv[0],*sys.argv[2:]]\n"
                      "sys.exit(main())\n")
    frozen.chmod(0o700)
    monkeypatch.setattr(process_tools, "sys", SimpleNamespace(
        platform=sys.platform, frozen=True, executable=str(frozen)))

    pid_path = tmp_path / "child.pid"
    command = "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)"
    executor = LocalExecutor()
    child_pid = None
    try:
        result = await executor(action("start_process", executable=sys.executable,
                                       argv=["-c", command, str(pid_path)], cwd=str(tmp_path),
                                       background_seconds=20), GrantRef(), asyncio.Event())
        assert result.state == "completed"
        for _ in range(100):
            if pid_path.exists():
                break
            await asyncio.sleep(.05)
        assert pid_path.exists(), "frozen-style guardian did not start the child"
        child_pid = int(pid_path.read_text())
        assert _live_process(child_pid)
        await executor.close()
        assert not _live_process(child_pid)
    finally:
        await executor.close()
        if child_pid is not None and _live_process(child_pid):
            os.killpg(child_pid, 9)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX frozen guardian fixture")
@pytest.mark.asyncio
async def test_cancel_during_frozen_guardian_start_withdraws_launch(monkeypatch, tmp_path):
    from app.services.tools import process as process_tools

    backend_dir = Path(__file__).resolve().parents[1]
    frozen = tmp_path / "slow-frozen-backend"
    frozen.write_text(f"#!{sys.executable}\n"
                      "import sys,time\n"
                      "time.sleep(1.5)\n"
                      f"sys.path.insert(0,{str(backend_dir)!r})\n"
                      "from app.services.tools.guardian import main\n"
                      "sys.argv=[sys.argv[0],*sys.argv[2:]]\n"
                      "sys.exit(main())\n")
    frozen.chmod(0o700)
    monkeypatch.setattr(process_tools, "sys", SimpleNamespace(
        platform=sys.platform, frozen=True, executable=str(frozen)))

    pid_path = tmp_path / "child.pid"
    command = "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)"
    executor = LocalExecutor()
    started = asyncio.create_task(executor(action("start_process", executable=sys.executable,
                                                  argv=["-c", command, str(pid_path)], cwd=str(tmp_path),
                                                  background_seconds=20), GrantRef(), asyncio.Event()))
    try:
        await asyncio.sleep(.1)
        started.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(started, 1)
        await asyncio.sleep(2)
        assert not pid_path.exists(), "cancelled launch started a child"
        assert not executor.processes.running
    finally:
        await executor.close()
        if pid_path.exists() and _live_process(int(pid_path.read_text())):
            os.killpg(int(pid_path.read_text()), 9)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX guardian launch race")
@pytest.mark.asyncio
async def test_withdrawal_during_guardian_popen_reports_uncertain_and_reaps_child(monkeypatch, tmp_path):
    from app.services.tools import process as process_tools

    backend_dir = Path(__file__).resolve().parents[1]
    wrapper = tmp_path / "paused-guardian"
    entered = tmp_path / "popen-entered"
    release = tmp_path / "release-popen"
    child_pid_path = tmp_path / "child.pid"
    effect = tmp_path / "effect"
    wrapper.write_text(f"#!{sys.executable}\n"
                       "import os,sys,time\n"
                       f"sys.path.insert(0,{str(backend_dir)!r})\n"
                       "from app.services.tools import guardian\n"
                       "spawn=guardian.subprocess.Popen\n"
                       "def paused(*args,**kwargs):\n"
                       f"    open({str(entered)!r},'w').close()\n"
                       f"    while not os.path.exists({str(release)!r}): time.sleep(.01)\n"
                       "    child=spawn(*args,**kwargs)\n"
                       f"    open({str(child_pid_path)!r},'w').write(str(child.pid))\n"
                       "    deadline=time.monotonic()+2\n"
                       f"    while not os.path.exists({str(effect)!r}) and time.monotonic()<deadline: time.sleep(.01)\n"
                       "    return child\n"
                       "guardian.subprocess.Popen=paused\n"
                       "sys.argv=[sys.argv[0],*sys.argv[2:]]\n"
                       "sys.exit(guardian.main())\n")
    wrapper.chmod(0o700)
    monkeypatch.setattr(process_tools, "sys", SimpleNamespace(
        platform=sys.platform, frozen=True, executable=str(wrapper)))
    monkeypatch.setattr(process_tools, "GUARDIAN_READY_SECONDS", 1.5)
    command = "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('effect'); time.sleep(30)"
    executor = LocalExecutor()
    try:
        running = asyncio.create_task(executor(action("start_process", executable=sys.executable,
                                                      argv=["-c", command, str(effect)], cwd=str(tmp_path),
                                                      background_seconds=20), GrantRef(), asyncio.Event()))
        for _ in range(300):
            if entered.exists():
                break
            await asyncio.sleep(.01)
        assert entered.exists(), "fixture never entered Popen"
        result = await asyncio.wait_for(running, 3)
        assert result.state == "uncertain"
        release.touch()
        for _ in range(200):
            if effect.exists() and child_pid_path.exists() and not _live_process(int(child_pid_path.read_text())):
                break
            await asyncio.sleep(.02)
        assert effect.read_text() == "effect", "fixture did not execute after withdrawal"
        assert not _live_process(int(child_pid_path.read_text())), "guardian did not reap effect-bearing child"
        assert not executor.processes.running
    finally:
        release.touch()
        await executor.close()
        if child_pid_path.exists() and _live_process(int(child_pid_path.read_text())):
            os.killpg(int(child_pid_path.read_text()), 9)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX guardian pipe fixture")
def test_guardian_reaps_child_if_parent_closes_readiness_pipe(tmp_path):
    control_read, control_write = os.pipe()
    ready_read, ready_write = os.pipe()
    pid_path = tmp_path / "child.pid"
    backend_dir = Path(__file__).resolve().parents[1]
    wrapper = tmp_path / "guardian-wrapper.py"
    wrapper.write_text("import sys\n"
                       f"sys.path.insert(0,{str(backend_dir)!r})\n"
                       "from app.services.tools import guardian\n"
                       "spawn=guardian.subprocess.Popen\n"
                       "def record(*args,**kwargs):\n"
                       "    child=spawn(*args,**kwargs)\n"
                       f"    open({str(pid_path)!r},'w').write(str(child.pid))\n"
                       "    return child\n"
                       "guardian.subprocess.Popen=record\n"
                       "sys.exit(guardian.main())\n")
    from test_process_review import reviewed_process
    from app.services.tools.process import launch_review
    from app.services.tools.launch_review import protected_review
    pending, ref = reviewed_process(action("start_process", executable="/bin/sleep", argv=["30"], cwd=str(tmp_path)))
    with protected_review(launch_review(pending, ref)) as review_fd:
        process = subprocess.Popen(
            [sys.executable, str(wrapper), str(control_read), str(ready_write), str(review_fd),
             str(time.monotonic() + 20), pending.arguments['executable'], "30"],
            pass_fds=(control_read, ready_write, review_fd), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(control_read)
    os.close(ready_write)
    os.close(ready_read)
    child_pid = None
    try:
        process.wait(timeout=5)
        assert pid_path.exists(), "fixture did not start the child before readiness failed"
        child_pid = int(pid_path.read_text())
        assert not _live_process(child_pid), "guardian left a child after readiness pipe closed"
    finally:
        os.close(control_write)
        if process.poll() is None:
            os.killpg(process.pid, 9)
            process.wait(timeout=2)
        if child_pid is not None and _live_process(child_pid):
            os.killpg(child_pid, 9)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX detached stdout fixture")
@pytest.mark.asyncio
async def test_process_stop_does_not_wait_for_escaped_stdout_holder(tmp_path):
    executor = LocalExecutor()
    pid_path = tmp_path / "escaped.pid"
    script = ("import os,sys,time; child=os.fork(); "
              "os.setsid() if child == 0 else None; "
              "open(sys.argv[1],'w').write(str(os.getpid())) if child == 0 else None; "
              "time.sleep(60)")
    try:
        result = await executor(action("start_process", executable=sys.executable,
                                       argv=["-c", script, str(pid_path)], cwd=str(tmp_path),
                                       background_seconds=10), GrantRef(), asyncio.Event())
        assert result.state == "completed"
        for _ in range(100):
            if pid_path.exists():
                break
            await asyncio.sleep(.02)
        assert pid_path.exists()
        await asyncio.wait_for(executor.close(), 2)
        assert not executor.processes.running
    finally:
        if pid_path.exists():
            escaped_pid = int(pid_path.read_text())
            try:
                os.kill(escaped_pid, 9)
            except ProcessLookupError:
                pass
        await asyncio.wait_for(executor.close(), 3)


@pytest.mark.asyncio
async def test_files_work_and_reject_symlink_swap(tmp_path):
    executor = LocalExecutor()
    inside = tmp_path / "inside"
    outside = tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    target = inside / "a.txt"
    grant = GrantRef(scope="broad")
    assert (await executor(action("write_file", path=str(target), content="hello"), grant,
                           asyncio.Event())).state == "completed"
    assert (await executor(action("read_file", path=str(target)), grant, asyncio.Event())).text == "hello"
    assert (await executor(action("move_file", source=str(target), destination=str(inside / "b.txt")),
                           grant, asyncio.Event())).state == "completed"
    assert (await executor(action("delete_file", path=str(inside / "b.txt")), grant,
                           asyncio.Event())).state == "completed"
    (outside / "secret").write_text("outside")
    (inside / "link").mkdir()
    (inside / "link" / "secret").write_text("owned-before-swap")
    from test_conditional_files import reviewed
    pending, inspected = reviewed("read_file", {"path": str(inside / "link" / "secret")})
    (inside / "link").rename(inside / "previous-link")
    (inside / "link").symlink_to(outside, target_is_directory=True)
    # Swap after the original canonical review; re-preparing the already
    # linked path would instead explicitly review its outside destination.
    result = await RealLocalExecutor()(pending, inspected, asyncio.Event())
    assert result.state == "failed"
    assert "outside" not in result.text


@pytest.mark.asyncio
async def test_file_parent_rename_cannot_redirect_write(tmp_path, monkeypatch):
    inside = tmp_path / "inside"
    outside = tmp_path / "outside"
    moved = tmp_path / "moved"
    inside.mkdir()
    outside.mkdir()
    (outside / "target").write_text("outside")
    original_rename = file_tools._rename_exclusive
    def swap_parent(source_fd, source, dest_fd, destination):
        if source.startswith("temp-"):
            inside.rename(moved)
            inside.symlink_to(outside, target_is_directory=True)
        return original_rename(source_fd, source, dest_fd, destination)
    monkeypatch.setattr(file_tools, "_rename_exclusive", swap_parent)
    executor = LocalExecutor()
    result = await executor(action("write_file", path=str(inside / "target"), content="inside"),
                            GrantRef(scope="broad"), asyncio.Event())
    # The held FD prevents redirection; the now-invalid canonical parent
    # makes completion unconfirmed under the new reviewed-path contract.
    assert result.state == "uncertain"
    assert (outside / "target").read_text() == "outside"
    assert (moved / "target").read_text() == "inside"


@pytest.mark.asyncio
async def test_move_preserves_late_regular_source_replacement(tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    parked = tmp_path / "parked"
    source.write_text("original")
    original_rename = file_tools._rename_exclusive
    def swap_before_destination(source_fd, source_name, dest_fd, destination_name):
        if source_name.startswith("temp-") and destination_name == destination.name:
            if source.exists():
                source.rename(parked)
            source.write_text("replacement")
        return original_rename(source_fd, source_name, dest_fd, destination_name)
    monkeypatch.setattr(file_tools, "_rename_exclusive", swap_before_destination)
    executor = LocalExecutor()
    result = await executor(action("move_file", source=str(source), destination=str(destination)),
                            GrantRef(scope="broad"), asyncio.Event())
    assert result.state == "uncertain"
    assert destination.read_text() == "original"
    assert source.read_text() == "replacement"
    if parked.exists():
        assert parked.read_text() == "original"


@pytest.mark.asyncio
async def test_move_does_not_delete_replacement_quarantine_entry(tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    parked = tmp_path / "parked"
    source.write_text("original")
    original_rename = file_tools._rename_exclusive
    payload_names = []
    def swap_before_destination(source_fd, source_name, dest_fd, destination_name):
        if source_name.startswith("temp-") and destination_name == destination.name:
            claims = list(tmp_path.glob(".gamja-claim-*"))
            private = list(tmp_path.glob(".gamja-move-*"))
            assert len(claims) + len(private) == 1
            if claims:
                claims[0].rename(parked)
                claims[0].write_text("replacement")
            else:
                payloads = list(private[0].glob("payload-*"))
                assert len(payloads) == 1
                payload_names.append(payloads[0].name)
                private[0].rename(parked)
                private[0].mkdir()
                (private[0] / payload_names[0]).write_text("replacement")
        return original_rename(source_fd, source_name, dest_fd, destination_name)
    monkeypatch.setattr(file_tools, "_rename_exclusive", swap_before_destination)
    result = await LocalExecutor()(action("move_file", source=str(source), destination=str(destination)),
                                   GrantRef(scope="broad"), asyncio.Event())
    assert result.state == "uncertain"
    assert destination.read_text() == "original"
    if parked.is_dir():
        assert (parked / payload_names[0]).read_text() == "original"
        replacement = next(tmp_path.glob(".gamja-move-*"))
        assert (replacement / payload_names[0]).read_text() == "replacement"
    else:
        assert parked.read_text() == "original"
        replacement = next(tmp_path.glob(".gamja-claim-*"))
        assert replacement.read_text() == "replacement"


@pytest.mark.asyncio
async def test_move_never_removes_capture_directory_by_parent_name(tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    parked = tmp_path / "parked"
    source.write_text("original")
    original_rmdir = os.rmdir
    removals = []
    def replace_before_rmdir(name, **kwargs):
        if isinstance(name, str) and name.startswith(".gamja-move-"):
            removals.append(name)
            (tmp_path / name).rename(parked)
            (tmp_path / name).mkdir()
        return original_rmdir(name, **kwargs)
    monkeypatch.setattr(os, "rmdir", replace_before_rmdir)
    result = await LocalExecutor()(action("move_file", source=str(source), destination=str(destination)),
                                   GrantRef(scope="broad"), asyncio.Event())
    assert result.state == "completed"
    assert destination.read_text() == "original"
    assert not removals
    assert len(list(tmp_path.glob(".gamja-move-*"))) == 1
    assert not parked.exists()


@pytest.mark.asyncio
async def test_move_rejects_reopened_public_capture_directory(tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    parked = tmp_path / "parked"
    source.write_text("original")
    original_open = os.open
    replaced = []
    def swap_before_capture_open(name, flags, *args, **kwargs):
        if isinstance(name, str) and name.startswith(".gamja-move-") and flags & os.O_DIRECTORY:
            (tmp_path / name).rename(parked)
            (tmp_path / name).mkdir(mode=0o777)
            (tmp_path / name).chmod(0o777)
            replaced.append(name)
        return original_open(name, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", swap_before_capture_open)
    result = await LocalExecutor()(action("move_file", source=str(source), destination=str(destination)),
                                   GrantRef(scope="broad"), asyncio.Event())
    assert replaced
    assert result.state == "failed"
    assert source.read_text() == "original"
    assert not destination.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["symlink", "public_directory", "regular_file"])
async def test_move_rejects_incompatible_existing_capture_namespace(tmp_path, kind):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    namespace = tmp_path / ".gamja-move-capture"
    source.write_text("original")
    if kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        namespace.symlink_to(target)
    elif kind == "public_directory":
        namespace.mkdir(mode=0o777)
        namespace.chmod(0o777)
        (namespace / "keep").write_text("untouched")
    else:
        namespace.write_text("untouched")
    result = await LocalExecutor()(action("move_file", source=str(source), destination=str(destination)),
                                   GrantRef(scope="broad"), asyncio.Event())
    assert result.state == "failed"
    assert source.read_text() == "original"
    assert not destination.exists()
    if kind == "public_directory":
        assert (namespace / "keep").read_text() == "untouched"
        assert namespace.stat().st_mode & 0o077 == 0o077
    elif kind == "regular_file":
        assert namespace.read_text() == "untouched"
    else:
        assert namespace.is_symlink()


@pytest.mark.asyncio
async def test_repeated_moves_reuse_capture_namespace_without_sweeping_contents(tmp_path):
    executor = LocalExecutor()
    namespace = tmp_path / ".gamja-move-capture"
    for index in (1, 2):
        source = tmp_path / f"source-{index}"
        destination = tmp_path / f"destination-{index}"
        source.write_text(f"original-{index}")
        result = await executor(action("move_file", source=str(source), destination=str(destination)),
                                GrantRef(scope="broad"), asyncio.Event())
        assert result.state == "completed"
        assert not source.exists()
        assert destination.read_text() == f"original-{index}"
        if index == 1:
            (namespace / "unrelated").write_text("kept")
    for index in (3, 4):
        (tmp_path / f"source-{index}").write_text(f"original-{index}")
    concurrent = await asyncio.gather(*(
        executor(action("move_file", source=str(tmp_path / f"source-{index}"),
                        destination=str(tmp_path / f"destination-{index}")),
                 GrantRef(scope="broad"), asyncio.Event()) for index in (3, 4)))
    assert all(result.state == "completed" for result in concurrent)
    assert (tmp_path / "destination-3").read_text() == "original-3"
    assert (tmp_path / "destination-4").read_text() == "original-4"
    assert namespace.is_dir()
    assert (namespace / "unrelated").read_text() == "kept"
    assert len(list(tmp_path.glob(".gamja-move-*"))) == 1
    assert not list(namespace.glob("payload-*"))


@pytest.mark.asyncio
async def test_slow_move_keeps_loop_live_and_stops_before_commit(tmp_path, monkeypatch):
    source = tmp_path / "large-source"
    destination = tmp_path / "destination"
    source.write_bytes(b"a" * 200_000)
    original_read = file_tools.os.read
    entered = threading.Event()
    release = threading.Event()
    def slow_read(fd, size):
        entered.set()
        release.wait(2)
        return original_read(fd, size)
    monkeypatch.setattr(file_tools.os, "read", slow_read)
    executor = LocalExecutor()
    cancel = asyncio.Event()
    timer = threading.Timer(.4, release.set)
    timer.start()
    try:
        started = time.monotonic()
        task = asyncio.create_task(executor(action("move_file", source=str(source), destination=str(destination)),
                                            GrantRef(scope="broad"), cancel))
        await asyncio.sleep(.05)
        assert time.monotonic() - started < .25
        assert entered.is_set()
        cancel.set()
        result = await asyncio.wait_for(task, 3)
        assert result.state in ("cancelled", "uncertain")
        assert source.exists()
        assert not destination.exists()
    finally:
        release.set()
        timer.join()
        await executor.close()


@pytest.mark.asyncio
async def test_move_cancel_collision_keeps_replacement_and_recovery_claim(tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_text("original")
    cancel = asyncio.Event()
    original_read = file_tools.os.read
    def replace_on_read(fd, size):
        source.write_text("replacement")
        cancel.set()
        return original_read(fd, size)
    monkeypatch.setattr(file_tools.os, "read", replace_on_read)
    result = await LocalExecutor()(action("move_file", source=str(source), destination=str(destination)),
                                   GrantRef(scope="broad"), cancel)
    assert result.state == "uncertain"
    assert source.read_text() == "replacement"
    assert not destination.exists()
    captures = list(tmp_path.glob(".gamja-move-*"))
    assert len(captures) == 1
    payloads = list(captures[0].glob("payload-*"))
    assert len(payloads) == 1
    assert payloads[0].read_text() == "original"
    assert str(payloads[0]) in result.text


@pytest.mark.asyncio
async def test_cancelled_move_awaits_owned_worker_before_return(tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"a" * 200_000)
    entered = threading.Event()
    release = threading.Event()
    original_read = file_tools.os.read
    def held_read(fd, size):
        entered.set()
        release.wait(2)
        return original_read(fd, size)
    monkeypatch.setattr(file_tools.os, "read", held_read)
    task = asyncio.create_task(LocalExecutor()(action("move_file", source=str(source), destination=str(destination)),
                                               GrantRef(scope="broad"), asyncio.Event()))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(.01)
        assert entered.is_set()
        task.cancel()
        await asyncio.sleep(.05)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert source.exists()
        assert not destination.exists()
        captures = list(tmp_path.glob(".gamja-move-*"))
        assert len(captures) == 1
        assert not list(captures[0].iterdir())
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_browser_denied_redirect_never_reaches_target(tmp_path):
    hit = []
    allowed_hit = []
    async def denied(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        hit.append(True)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\nConnection: close\r\n\r\ndenied")
        await writer.drain()
        writer.close()
    denied_server = await asyncio.start_server(denied, "127.0.0.1", 0)
    denied_url = f"http://127.0.0.1:{denied_server.sockets[0].getsockname()[1]}/"
    async def allowed(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        allowed_hit.append(True)
        writer.write(f"HTTP/1.1 302 Found\r\nLocation: {denied_url}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        writer.close()
    allowed_server = await asyncio.start_server(allowed, "127.0.0.1", 0)
    allowed_url = f"http://127.0.0.1:{allowed_server.sockets[0].getsockname()[1]}/"
    executor = LocalExecutor(browser_engine="bundled")
    try:
        result = await executor(action("browser_navigate", url=allowed_url), GrantRef(scope="room_session"),
                                asyncio.Event())
        assert result.state == "failed", result
        assert allowed_hit
        assert not hit
    finally:
        await executor.close()
        allowed_server.close()
        denied_server.close()
        await allowed_server.wait_closed()
        await denied_server.wait_closed()


@pytest.mark.asyncio
async def test_browser_workflow_keeps_state_and_removes_profile():
    async def page(reader, writer):
        first = (await reader.readuntil(b"\r\n\r\n")).split(b"\r\n", 1)[0]
        if b"/other" in first:
            body = b"<body><script>document.body.textContent=localStorage.getItem('v')</script></body>"
        else:
            body = b"<body><button id='save' onclick=\"localStorage.setItem('v','kept')\">Save</button></body>"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: " +
                     str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(page, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
    executor = LocalExecutor(browser_engine="bundled")
    try:
        assert (await executor(action("browser_navigate", url=url), GrantRef(), asyncio.Event())).state == "completed"
        assert (await executor(action("browser_interact", url=url, selector="#save", operation="click"),
                               GrantRef(), asyncio.Event())).state == "completed"
        observed = await executor(action("browser_inspect", url=url + "other"), GrantRef(), asyncio.Event())
        assert observed.state == "completed"
        assert "Text: kept" in observed.text
        profile = next(iter(executor.browser.workflows.values())).profile
        await executor.stop_room(1)
        assert not Path(profile).exists()
    finally:
        await executor.close()
        server.close()
        await server.wait_closed()
    assert not Path(profile).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("broad", [False, True])
async def test_browser_workflow_honors_second_approved_origin_and_broad_assets(broad):
    assets = []
    async def asset(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        assets.append(True)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
    asset_server = await asyncio.start_server(asset, "127.0.0.1", 0)
    asset_url = f"http://127.0.0.1:{asset_server.sockets[0].getsockname()[1]}/asset.js"
    async def first(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        body = b"<body>first</body>"
        writer.write(b"HTTP/1.1 200 OK\r\nSet-Cookie: shared=kept; Path=/\r\nContent-Type: text/html\r\nContent-Length: " +
                     str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()
    first_server = await asyncio.start_server(first, "127.0.0.1", 0)
    first_url = f"http://127.0.0.1:{first_server.sockets[0].getsockname()[1]}/"
    cookies = []
    async def second(reader, writer):
        header = await reader.readuntil(b"\r\n\r\n")
        cookies.append(header)
        body = (f"<body>second{f'<script src={asset_url}></script>' if broad else ''}</body>").encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: " +
                     str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()
    second_server = await asyncio.start_server(second, "127.0.0.1", 0)
    second_url = f"http://127.0.0.1:{second_server.sockets[0].getsockname()[1]}/"
    executor = LocalExecutor(browser_engine="bundled")
    try:
        first_grant = GrantRef(grant_id=1, scope="broad" if broad else "room_session")
        second_grant = GrantRef(grant_id=2, scope="broad" if broad else "room_session")
        assert (await executor(action("browser_navigate", url=first_url), first_grant, asyncio.Event())).state == "completed"
        result = await executor(action("browser_navigate", url=second_url), second_grant, asyncio.Event())
        assert result.state == "completed", result
        assert cookies and b"shared=kept" in cookies[-1]
        assert len(executor.browser.workflows) == 1
        assert bool(assets) == broad
        if not broad:
            page = next(iter(executor.browser.workflows.values())).page
            await page.evaluate("url => fetch(url).catch(() => {})", asset_url)
            assert not assets
        profile = next(iter(executor.browser.workflows.values())).profile
        await executor.stop_grant(1)
        assert len(executor.browser.workflows) == 1
        await executor.stop_grant(2)
        assert not executor.browser.workflows
        assert not Path(profile).exists()
    finally:
        await executor.close()
        for server in (first_server, second_server, asset_server):
            server.close()
            await server.wait_closed()


@pytest.mark.asyncio
async def test_browser_popup_cannot_reach_unapproved_origin():
    hit = []
    async def denied(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        hit.append(True)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
    denied_server = await asyncio.start_server(denied, "127.0.0.1", 0)
    denied_url = f"http://127.0.0.1:{denied_server.sockets[0].getsockname()[1]}/"
    async def allowed(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        body = f"<a id='popup' target='_blank' href='{denied_url}'>Open</a>".encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: " +
                     str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()
    allowed_server = await asyncio.start_server(allowed, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{allowed_server.sockets[0].getsockname()[1]}/"
    executor = LocalExecutor(browser_engine="bundled")
    try:
        assert (await executor(action("browser_navigate", url=url), GrantRef(), asyncio.Event())).state == "completed"
        result = await executor(action("browser_interact", url=url, selector="#popup", operation="click"),
                                GrantRef(), asyncio.Event())
        assert result.state == "failed"
        assert not hit
    finally:
        await executor.close()
        allowed_server.close()
        denied_server.close()
        await allowed_server.wait_closed()
        await denied_server.wait_closed()


@pytest.mark.asyncio
async def test_browser_cancellation_closes_owned_profile():
    accepted = asyncio.Event()
    release = asyncio.Event()
    async def hanging(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        accepted.set()
        await release.wait()
        writer.close()
    server = await asyncio.start_server(hanging, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
    executor = LocalExecutor(browser_engine="bundled")
    running = asyncio.create_task(executor(action("browser_navigate", url=url), GrantRef(), asyncio.Event()))
    try:
        await asyncio.wait_for(accepted.wait(), 5)
        profile = next(iter(executor.browser.workflows.values())).profile
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert not executor.browser.workflows
        assert not Path(profile).exists()
    finally:
        release.set()
        await executor.close()
        server.close()
        await server.wait_closed()
