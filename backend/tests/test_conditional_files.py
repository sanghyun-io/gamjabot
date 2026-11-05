"""Real descriptor/namespace effects in owned pytest temporary directories."""
import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.action_review import Authority, ProposerBinding
from app.services.grants import GrantRef
from app.services.file_preconditions import validate_file_postconditions
from app.services.review_snapshot import capture_review
from app.services.tool_broker import prepare
from app.services.tools import files as file_tools


def reviewed(family, args, scope="once"):
    prepared = prepare(family, args, "owned-fixture")
    session = str(uuid4())
    action = SimpleNamespace(action_id=str(uuid4()), room_id=1, agent_id=2,
        trigger_event_id=None, call_id="fixture", workflow_id="owned-fixture", session_id=session,
        family=family, target=prepared.target, scope_target=prepared.scope_target,
        expected_effect=prepared.expected_effect, payload_digest=prepared.payload_digest,
        arguments=prepared.raw_arguments, review_id=str(uuid4()))
    proposer = ProposerBinding(owner_id=1, agent_id=2, room_id=1, connection_id=3,
        model_id="synthetic", config_revision=1, auth_grant_revision=0,
        provider_id="synthetic", protocol_id="synthetic")
    authority = Authority(owner_id=1, room_id=1, agent_id=2, session_id=session,
        os_user_status="verified", euid=os.geteuid(), proposer=proposer)
    snapshot = capture_review(action, prepared, authority)
    ref = GrantRef(None if scope=="broad" else 1, scope, 1, 2, session, (family,),
                   (action.scope_target,), None, action.review_id, snapshot, action.review_id)
    return action, ref


async def run(action, ref, cancel=None):
    return await file_tools.execute_file(action, ref, cancel or asyncio.Event())


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["read_file", "write_file", "delete_file", "move_file"])
@pytest.mark.parametrize("mode", ["once", "room_session", "broad"])
async def test_real_file_effect_has_bound_postconditions(tmp_path, family, mode):
    source = tmp_path/"source"; destination=tmp_path/"destination"
    source.write_text("original-owned-payload");destination.write_text("old-owned-destination")
    args={"source":str(source),"destination":str(destination)} if family=="move_file" else {"path":str(source)}
    if family=="write_file":args["content"]="new-owned-payload"
    action,ref=reviewed(family,args,mode)
    result=await run(action,ref)
    assert result.state=="completed",result.text
    value=validate_file_postconditions(result.file_postconditions,action,ref)
    assert value.content_CAS=="unsupported" and value.check=="fd_namespace_conditional"
    encoded=value.model_dump_json()
    assert "original-owned-payload" not in encoded and "new-owned-payload" not in encoded
    if family=="read_file":assert result.text=="original-owned-payload"
    elif family=="write_file":assert source.read_text()=="new-owned-payload"
    elif family=="delete_file":assert not source.exists()
    else:assert not source.exists() and destination.read_text()=="original-owned-payload"
    private=tmp_path/".gamja-move-capture"
    if private.exists():assert not list(private.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["once","room_session","broad"])
@pytest.mark.parametrize("change", ["same-size","inode","missing","parent","destination"])
async def test_every_mode_rejects_executor_boundary_drift(tmp_path,mode,change):
    directory=tmp_path/"parent";directory.mkdir();source=directory/"source";destination=directory/"destination"
    source.write_text("AAAA");destination.write_text("BBBB")
    action,ref=reviewed("move_file",{"source":str(source),"destination":str(destination)},mode)
    if change=="same-size":source.write_text("CCCC")
    elif change=="inode":source.rename(directory/"saved");source.write_text("CCCC")
    elif change=="missing":source.unlink()
    elif change=="destination":destination.write_text("CCCC")
    else:directory.rename(tmp_path/"moved");directory.mkdir();source.write_text("CCCC");destination.write_text("DDDD")
    before={p.name:p.read_bytes() for p in directory.iterdir() if p.is_file()}
    result=await run(action,ref)
    assert result.state=="failed" and result.error_code=="review_changed"
    assert before=={p.name:p.read_bytes() for p in directory.iterdir() if p.is_file()}
    assert not list(directory.glob(".gamja-*-capture"))


@pytest.mark.asyncio
async def test_missing_review_and_payload_replacement_never_execute(tmp_path):
    path=tmp_path/"owned";path.write_text("original")
    action,ref=reviewed("write_file",{"path":str(path),"content":"approved"})
    assert (await run(action,GrantRef(scope="broad"))).state=="failed"
    action.arguments["content"]="changed"
    assert (await run(action,ref)).state=="failed"
    assert path.read_text()=="original"


@pytest.mark.asyncio
@pytest.mark.parametrize("existing",[False,True])
async def test_write_exclusive_commit_preserves_late_destination(tmp_path,monkeypatch,existing):
    path=tmp_path/"owned"
    if existing:path.write_text("approved-old")
    action,ref=reviewed("write_file",{"path":str(path),"content":"approved-new"})
    rename=file_tools._rename_exclusive
    def late(source_fd,source,dest_fd,destination):
        if source.startswith("temp-"):
            path.write_text("late-replacement")
        return rename(source_fd,source,dest_fd,destination)
    monkeypatch.setattr(file_tools,"_rename_exclusive",late)
    result=await run(action,ref)
    assert result.state==("uncertain" if existing else "failed")
    assert path.read_text()=="late-replacement"
    payloads=list((tmp_path/".gamja-move-capture").glob("payload-*"))
    if existing:assert len(payloads)==1 and payloads[0].read_text()=="approved-old" and str(payloads[0]) in result.text
    else:assert not payloads
    assert not list((tmp_path/".gamja-move-capture").glob("temp-*"))


@pytest.mark.asyncio
async def test_existing_move_destination_and_source_recovered_without_overwrite(tmp_path,monkeypatch):
    source=tmp_path/"source";destination=tmp_path/"destination"
    source.write_text("source-original");destination.write_text("destination-original")
    action,ref=reviewed("move_file",{"source":str(source),"destination":str(destination)})
    rename=file_tools._rename_exclusive
    def late(source_fd,name,dest_fd,target):
        if name.startswith("temp-"):destination.write_text("replacement")
        return rename(source_fd,name,dest_fd,target)
    monkeypatch.setattr(file_tools,"_rename_exclusive",late)
    result=await run(action,ref)
    assert result.state=="uncertain" and source.read_text()=="source-original"
    assert destination.read_text()=="replacement"
    payloads=list((tmp_path/".gamja-move-capture").glob("payload-*"))
    assert len(payloads)==1 and payloads[0].read_text()=="destination-original"


@pytest.mark.asyncio
async def test_source_swap_at_capture_preserves_both_files(tmp_path,monkeypatch):
    source=tmp_path/"source";parked=tmp_path/"parked"
    source.write_text("approved-original")
    action,ref=reviewed("delete_file",{"path":str(source)})
    rename=file_tools._rename_exclusive
    def swap(source_fd,name,dest_fd,target):
        if name==source.name and target.startswith("payload-"):
            source.rename(parked);source.write_text("replacement")
        return rename(source_fd,name,dest_fd,target)
    monkeypatch.setattr(file_tools,"_rename_exclusive",swap)
    result=await run(action,ref)
    assert result.state=="failed"
    assert source.read_text()=="replacement" and parked.read_text()=="approved-original"


@pytest.mark.asyncio
async def test_read_version_change_never_returns_unverified_bytes(tmp_path,monkeypatch):
    path=tmp_path/"owned";path.write_text("original")
    action,ref=reviewed("read_file",{"path":str(path)})
    read=file_tools.os.read
    def changed(fd,size):
        data=read(fd,size);path.write_text("changed!");return data
    monkeypatch.setattr(file_tools.os,"read",changed)
    result=await run(action,ref)
    assert result.state=="failed" and "original" not in result.text and "changed!" not in result.text
    assert result.file_postconditions is None


@pytest.mark.asyncio
async def test_parent_swap_after_commit_is_uncertain_and_qualified(tmp_path,monkeypatch):
    parent=tmp_path/"parent";moved=tmp_path/"moved";parent.mkdir();path=parent/"owned"
    path.write_text("original")
    action,ref=reviewed("write_file",{"path":str(path),"content":"new"})
    rename=file_tools._rename_exclusive
    def moved_parent(source_fd,name,dest_fd,target):
        if name.startswith("temp-"):
            parent.rename(moved);parent.mkdir();path.write_text("replacement")
        return rename(source_fd,name,dest_fd,target)
    monkeypatch.setattr(file_tools,"_rename_exclusive",moved_parent)
    result=await run(action,ref)
    assert result.state=="uncertain" and path.read_text()=="replacement"
    assert (moved/"owned").read_text()=="new"
    assert "unknown" in result.text
    assert result.file_postconditions is None


@pytest.mark.asyncio
async def test_cancelled_copy_restores_owned_originals(tmp_path,monkeypatch):
    source=tmp_path/"source";destination=tmp_path/"destination"
    source.write_bytes(b"A"*200000);destination.write_text("old")
    action,ref=reviewed("move_file",{"source":str(source),"destination":str(destination)})
    cancel=asyncio.Event();read=file_tools.os.read
    def stop(fd,size):cancel.set();return read(fd,size)
    monkeypatch.setattr(file_tools.os,"read",stop)
    result=await run(action,ref,cancel)
    assert result.state=="cancelled"
    assert source.read_bytes()==b"A"*200000 and destination.read_text()=="old"
    assert not list((tmp_path/".gamja-move-capture").iterdir())


@pytest.mark.asyncio
async def test_noop_move_has_exact_unchanged_evidence(tmp_path):
    path=tmp_path/"owned";path.write_text("kept")
    action,ref=reviewed("move_file",{"source":str(path),"destination":str(path)})
    result=await run(action,ref)
    assert result.state=="completed"
    assert validate_file_postconditions(result.file_postconditions,action,ref)
    assert path.read_text()=="kept"


@pytest.mark.asyncio
async def test_private_capture_permission_change_is_never_completed(tmp_path,monkeypatch):
    path=tmp_path/"owned";path.write_text("approved-old")
    action,ref=reviewed("write_file",{"path":str(path),"content":"approved-new"})
    owned=file_tools._owned_temp
    def made_public(directory,parent,fd,name,leaf,expected=None):
        if expected is not None:os.fchmod(fd,0o777)
        return owned(directory,parent,fd,name,leaf,expected)
    monkeypatch.setattr(file_tools,"_owned_temp",made_public)
    result=await run(action,ref)
    assert result.state=="uncertain" and not path.exists()
    payloads=list((tmp_path/".gamja-move-capture").glob("payload-*"))
    assert len(payloads)==1 and payloads[0].read_text()=="approved-old"
    assert result.file_postconditions is None


@pytest.mark.asyncio
async def test_prepared_temp_version_change_is_rejected_before_commit(tmp_path,monkeypatch):
    path=tmp_path/"owned";path.write_text("approved-old")
    action,ref=reviewed("write_file",{"path":str(path),"content":"approved-new"})
    owned=file_tools._owned_temp
    def changed(directory,parent,fd,name,leaf,expected=None):
        if expected is not None:os.pwrite(leaf,b"unauthorized",0)
        return owned(directory,parent,fd,name,leaf,expected)
    monkeypatch.setattr(file_tools,"_owned_temp",changed)
    result=await run(action,ref)
    assert result.state=="failed" and path.read_text()=="approved-old"
    assert not list((tmp_path/".gamja-move-capture").iterdir())


@pytest.mark.asyncio
async def test_noncanonical_review_is_rejected_before_effect(tmp_path):
    from dataclasses import replace
    path=tmp_path/"owned";path.write_text("old")
    action,ref=reviewed("write_file",{"path":str(path),"content":"new"})
    action.review_id="not-a-review"
    result=await run(action,replace(ref,review_id=action.review_id))
    assert result.state=="failed" and path.read_text()=="old"
    assert not (tmp_path/".gamja-move-capture").exists()


@pytest.mark.asyncio
async def test_postcondition_construction_failure_after_effect_is_uncertain(tmp_path,monkeypatch):
    path=tmp_path/"owned";path.write_text("old")
    action,ref=reviewed("write_file",{"path":str(path),"content":"new"})
    def invalid(**_):raise ValueError("owned fixture cannot confirm evidence")
    monkeypatch.setattr(file_tools,"FilePostconditions",invalid)
    result=await run(action,ref)
    assert result.state=="uncertain" and path.read_text()=="new"
    assert result.file_postconditions is None
