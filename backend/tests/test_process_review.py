"""Owned local guardian/FD fixtures; not installed app or external accounts."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

import pytest

from app.services.action_review import Authority, ProposerBinding
from app.services.grants import GrantRef
from app.services.review_snapshot import capture_review
from app.services.tool_broker import prepare
from app.services.tools.process import ProcessExecutor, launch_review
from app.services.tools.launch_review import protected_review, read_review

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX trusted guardian FD observations")


def reviewed_process(original, ref=None, executor=None):
    """Synthetic review for direct effect fixtures, never production auth."""
    ref = ref or GrantRef()
    prepared = prepare(original.family, original.arguments, original.workflow_id)
    try:
        session = str(UUID(ref.session_id))
    except (ValueError, TypeError, AttributeError):
        session = str(uuid5(NAMESPACE_URL, ref.session_id or "owned-process-fixture"))
    action = SimpleNamespace(action_id=str(uuid4()), room_id=original.room_id, agent_id=original.agent_id,
        trigger_event_id=None, call_id="owned-fixture", workflow_id=original.workflow_id,
        session_id=session, review_id=str(uuid4()), arguments=prepared.raw_arguments,
        **{k: getattr(prepared, k) for k in
           ("family", "target", "scope_target", "expected_effect", "payload_digest")})
    proposer = ProposerBinding(owner_id=1, agent_id=action.agent_id, room_id=action.room_id,
        connection_id=3, model_id="synthetic", config_revision=1, auth_grant_revision=0,
        provider_id="synthetic", protocol_id="synthetic")
    authority = Authority(owner_id=1, agent_id=action.agent_id, room_id=action.room_id,
        session_id=session, os_user_status="verified", euid=os.geteuid(), proposer=proposer)
    review = capture_review(action, prepared, authority, executor)
    return action, replace(ref, scope=ref.scope or "once", room_id=action.room_id,
        agent_id=action.agent_id, session_id=session, review_id=action.review_id,
        review=review, approval_review_id=action.review_id)


def request(tmp_path, **extra):
    return SimpleNamespace(family="start_process", room_id=1, agent_id=2, workflow_id="fixture",
        arguments={"executable":sys.executable, "argv":["-c", "print('owned-ok')"],
                   "cwd":str(tmp_path), **extra})


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["once", "room_session", "broad"])
async def test_actual_guardian_launch_has_retained_review_and_no_proof_fd_in_child(tmp_path, scope):
    action, ref = reviewed_process(request(tmp_path), GrantRef(scope=scope))
    executor = ProcessExecutor()
    try:
        result = await executor(action, ref, asyncio.Event())
        assert result.state == "completed" and result.text == "owned-ok"
        assert not executor.running
    finally:
        await executor.close()
    assert not list(Path(os.environ.get("TMPDIR", "/tmp")).glob("gamja-launch-review-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "payload", "review", "action", "euid"])
async def test_parent_refuses_missing_or_changed_review_before_guardian(tmp_path, monkeypatch, change):
    action, ref = reviewed_process(request(tmp_path))
    if change == "missing": ref = GrantRef()
    elif change == "payload": action.arguments["argv"] = ["-c", "print('changed')"]
    elif change == "review": action.review_id = str(uuid4())
    elif change == "action": action.action_id = str(uuid4())
    else: ref = replace(ref, review=ref.review.model_copy(update={"authority":ref.review.authority.model_copy(update={"euid":os.geteuid()+1})}))
    def forbidden(*args, **kwargs): raise AssertionError("No guardian may launch")
    monkeypatch.setattr("app.services.tools.process.subprocess.Popen", forbidden)
    result = await ProcessExecutor()(action, ref, asyncio.Event())
    assert result.state == "failed" and result.error_code == "review_changed"


def _direct_guardian(tmp_path, value, command, *, before="", deadline=None, close_control=False, unsafe=False):
    control_r, control_w = os.pipe();ready_r, ready_w = os.pipe()
    wrapper = tmp_path.parent/("guardian-entry-"+uuid4().hex+".py")
    wrapper.write_text("import sys,os,time\n" +
        f"sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r})\n" +
        "from app.services.tools.guardian import main\n" + before + "\nsys.exit(main())\n")
    proc = None
    try:
        with protected_review(value) as fd:
            assert read_review(fd, command) == value
            if unsafe:
                # A caller-authored linked file is not a protected channel.
                linked = tmp_path/"untrusted.json";linked.write_text(json.dumps(value));linked.chmod(0o400)
                bad = os.open(linked, os.O_RDONLY);channel = bad
            else: channel = fd
            try:
                proc = subprocess.Popen([sys.executable, str(wrapper), str(control_r), str(ready_w),
                    str(channel), str(deadline if deadline is not None else time.monotonic()+5), *command],
                    pass_fds=(control_r,ready_w,channel), cwd=tmp_path,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            finally:
                if unsafe: os.close(bad)
        os.close(control_r);control_r=None;os.close(ready_w);ready_w=None
        if close_control:os.close(control_w);control_w=None
        stdout,stderr=proc.communicate(timeout=8)
        ready=os.read(ready_r,1)
        assert b"Traceback" not in stderr,stderr.decode()
        return proc.returncode,ready,stdout
    finally:
        wrapper.unlink(missing_ok=True)
        for fd in (control_r,control_w,ready_r,ready_w):
            if fd is not None:os.close(fd)
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid,9);proc.wait()


@pytest.mark.parametrize("change", ["executable-version","executable-inode","executable-link","cwd-version","cwd-parent"])
def test_guardian_rejects_changes_after_parent_check(tmp_path, change):
    cwd=tmp_path/"cwd";cwd.mkdir();executable=tmp_path/"executable"
    marker=tmp_path/"effect"
    executable.write_text("#!/bin/sh\nprintf effect > "+str(marker)+"\n#AAAA\n");executable.chmod(0o700)
    action,ref=reviewed_process(request(cwd, executable=str(executable), argv=[]))
    value=launch_review(action,ref)
    if change=="executable-version":before=f"p={str(executable)!r};s=open(p).read();open(p,'w').write(s.replace('AAAA','BBBB'))"
    elif change=="executable-inode":before=f"os.rename({str(executable)!r},{str(tmp_path/'original')!r});open({str(executable)!r},'w').write('changed')"
    elif change=="executable-link":before=f"os.rename({str(executable)!r},{str(tmp_path/'original')!r});os.symlink({str(tmp_path/'original')!r},{str(executable)!r})"
    elif change=="cwd-version":before=f"open({str(cwd/'other')!r},'w').write('changed')"
    else:before=f"os.rename({str(cwd)!r},{str(tmp_path/'saved')!r});os.mkdir({str(cwd)!r})"
    code,ready,_=_direct_guardian(tmp_path,value,[str(executable)],before=before)
    assert code==1 and ready==b"R" and not marker.exists()


@pytest.mark.parametrize("kind", ["expired","withdrawn","linked"])
def test_guardian_no_launch_after_deadline_withdrawal_or_untrusted_fd(tmp_path, kind):
    marker=tmp_path/"effect"
    action,ref=reviewed_process(request(tmp_path,argv=["-c",f"open({str(marker)!r},'w').write('effect')"]))
    command=[action.arguments["executable"],*action.arguments["argv"]]
    code,ready,_=_direct_guardian(tmp_path,launch_review(action,ref),command,
        deadline=time.monotonic()-1 if kind=="expired" else None,
        close_control=kind=="withdrawn",unsafe=kind=="linked")
    assert code==1 and ready in (b"",b"R") and not marker.exists()


def test_readonly_unlinked_review_and_target_fds_do_not_reach_command(tmp_path):
    action,ref=reviewed_process(request(tmp_path))
    # Enumerate the actual target command FDs. Only its stdio and directory
    # enumeration FD may remain; guardian/control/review FDs are close_fds.
    command=[action.arguments['executable'],"-c","import os;print(sorted(int(x) for x in os.listdir('/dev/fd')))"]
    code,ready,out=_direct_guardian(tmp_path,launch_review(action,ref),command)
    assert code==0 and ready==b"1" and all(x<=3 for x in json.loads(out))


def test_protected_fd_is_readonly_and_rejects_noncanonical_metadata(tmp_path):
    action,ref=reviewed_process(request(tmp_path));value=launch_review(action,ref)
    with protected_review(value) as fd:
        with pytest.raises(OSError):os.write(fd,b"x")
        assert os.fstat(fd).st_nlink==0
    # Noncanonical and bool-equal metadata never become authority.
    from app.services.tools.launch_review import validate
    for modified in [{**value,"version":True},{**value,"review_id":"x"*36},
                     {**value,"targets":[{**value['targets'][0],'leaf':{**value['targets'][0]['leaf'],'uid':True}},value['targets'][1]]}]:
        with pytest.raises(ValueError):validate(modified,[action.arguments['executable']])


def test_unsupported_fd_observer_never_validates_channel(tmp_path, monkeypatch):
    from app.services.tools import launch_review as channel
    action,ref=reviewed_process(request(tmp_path))
    with protected_review(launch_review(action,ref)) as fd:
        monkeypatch.setattr(channel,"fcntl",None)
        with pytest.raises(ValueError):read_review(fd,[action.arguments['executable']])
