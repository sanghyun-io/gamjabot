"""Owned-process launch failures retain safe local diagnostics."""

import asyncio
from types import SimpleNamespace

import pytest

from app.services.tools.process import ProcessExecutor


@pytest.mark.asyncio
async def test_guardian_spawn_oserror_keeps_errno_class_without_private_path(monkeypatch):
    def fail_spawn(*_args, **_kwargs):
        raise FileNotFoundError(2, "private launch path", "/Users/alice/private-executable")

    monkeypatch.setattr("app.services.tools.process.subprocess.Popen", fail_spawn)
    action = SimpleNamespace(family="start_process", room_id=3, agent_id=5,
                             action_id="a" * 32, trigger_event_id="b" * 32,
                             workflow_id=None, arguments={"executable": "/usr/bin/true"})
    grant = SimpleNamespace(session_id="synthetic", grant_id=1, scope="once")
    from test_process_review import reviewed_process
    from app.services.grants import GrantRef
    action, grant = reviewed_process(action, GrantRef(grant_id=1, scope="once"))
    result = await ProcessExecutor()(action, grant, asyncio.Event())
    assert result.state == "failed" and result.error_code == "process_guardian_launch_failed"
    assert result.diagnostic["reason"] == "not_found"
    assert result.diagnostic["exception_chain"][0] == {"module": "builtins", "class": "FileNotFoundError"}
    assert "private-executable" not in str(result.diagnostic)
