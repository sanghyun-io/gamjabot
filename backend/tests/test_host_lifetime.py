"""The desktop host owns the backend and every process it starts."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _alive(pid):
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return result.returncode == 0 and bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX frozen-style launcher fixture")
def test_host_kill_ends_frozen_style_backend_and_owned_tool(tmp_path):
    """Removing the host-held pipe must end the bootloader, worker, and tool."""
    backend_dir = Path(__file__).resolve().parents[1]
    launcher = tmp_path / "launcher.py"
    host = tmp_path / "host.py"
    launcher.write_text("""
import asyncio, os, signal, sys, time
from pathlib import Path
from types import SimpleNamespace
from app.services.grants import GrantRef
from app.services.tools import LocalExecutor
import run
sys.path.insert(0, str(Path(run.__file__).resolve().parent / 'tests'))
from test_process_review import reviewed_process

folder = Path(sys.argv[1])
worker = os.fork()
if worker:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (folder / 'launcher.pid').write_text(str(os.getpid()))
    os.waitpid(worker, 0)
    while True: time.sleep(60)
(folder / 'worker.pid').write_text(str(os.getpid()))
if hasattr(run, 'start_owner_watchdog'):
    run.start_owner_watchdog()

async def main():
    executor = LocalExecutor()
    tool_code = "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(60)"
    action = SimpleNamespace(family='start_process', room_id=1, agent_id=2,
        action_id='host-lifetime-fixture', workflow_id=None,
        arguments={'executable':sys.executable, 'argv':['-c',tool_code,str(folder / 'tool.pid')],
                   'cwd':str(folder), 'background_seconds':10})
    action, ref = reviewed_process(action, GrantRef())
    result = await executor(action, ref, asyncio.Event())
    assert result.state == 'completed'
    for _ in range(200):
        if (folder / 'tool.pid').exists(): break
        await asyncio.sleep(.01)
    (folder / 'ready').write_text('1')
    await asyncio.Event().wait()

asyncio.run(main())
""")
    host.write_text("""
import os, subprocess, sys, time
from pathlib import Path
folder = Path(sys.argv[1])
backend = subprocess.Popen([sys.executable, str(folder / 'launcher.py'), str(folder)],
    cwd=sys.argv[2], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL, start_new_session=True,
    env={**os.environ, 'PYTHONPATH':sys.argv[2]})
backend.stdin.write(f'{backend.pid}\\n'.encode())
backend.stdin.flush()
(folder / 'backend.pgid').write_text(str(backend.pid))
while True: time.sleep(60)
""")
    owner = subprocess.Popen([sys.executable, str(host), str(tmp_path), str(backend_dir)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pgid = None
    tool_pid = None
    try:
        deadline = time.monotonic() + 8
        while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
            if owner.poll() is not None:
                pytest.fail("fixture host exited before backend was ready")
            time.sleep(.02)
        assert (tmp_path / "ready").exists(), "fixture backend did not become ready"
        pgid = int((tmp_path / "backend.pgid").read_text())
        worker_pid = int((tmp_path / "worker.pid").read_text())
        tool_pid = int((tmp_path / "tool.pid").read_text())
        assert all(_alive(pid) for pid in (pgid, worker_pid, tool_pid))
        os.kill(owner.pid, signal.SIGKILL)
        owner.wait(timeout=2)
        deadline = time.monotonic() + 5
        while any(_alive(pid) for pid in (pgid, worker_pid, tool_pid)) and time.monotonic() < deadline:
            time.sleep(.05)
        assert not any(_alive(pid) for pid in (pgid, worker_pid, tool_pid)), \
            "backend launcher, worker, or owned tool survived host death"
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=2)
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if tool_pid is not None and _alive(tool_pid):
            os.kill(tool_pid, signal.SIGKILL)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process group fixture")
def test_owner_watchdog_rejects_wrong_group_without_killing_bystander(tmp_path):
    backend_dir = Path(__file__).resolve().parents[1]
    script = """
import os, subprocess, sys, time
from pathlib import Path
import run
folder = Path(sys.argv[1])
bystander = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
(folder / 'bystander.pid').write_text(str(bystander.pid))
run.start_owner_watchdog()
time.sleep(60)
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)],
        cwd=backend_dir, env={**os.environ, "PYTHONPATH": str(backend_dir)},
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    bystander_pid = None
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "bystander.pid").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (tmp_path / "bystander.pid").exists()
        bystander_pid = int((tmp_path / "bystander.pid").read_text())
        process.stdin.write(b"0\n")
        process.stdin.close()
        process.wait(timeout=3)
        assert _alive(bystander_pid), "invalid group handshake killed a process it did not own"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        if bystander_pid is not None and _alive(bystander_pid):
            os.kill(bystander_pid, signal.SIGKILL)
