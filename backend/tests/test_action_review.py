"""Real fixture metadata/DB authority checks; no installed runtime or credentials."""

import os
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from test_room_state import room_client, create_room
from test_scheduler import ready_agents
from app.models.collaboration import ProviderConnection, RoomMembership
from app.models.models import Agent, Conversation, Project, User
from app.services.action_review import (
    FileTarget, ProposerBinding, ReviewChanged, frozen_proposer,
    observe_authority, observe_file_target,
)

posix_metadata = pytest.mark.skipif(os.name == "nt", reason="POSIX metadata fixture; Windows observation is explicitly unsupported")

@posix_metadata
def test_file_changes_and_replacements_are_distinct_from_payload(tmp_path):
    p = tmp_path / "target"
    p.write_bytes(b"AAAA")
    before = observe_file_target(str(p))
    assert before.status == "verified"
    time.sleep(.002)
    p.write_bytes(b"BBBB")
    changed = observe_file_target(str(p))
    assert changed.leaf.size == before.leaf.size
    assert changed != before
    other = tmp_path / "replacement"
    other.write_bytes(b"BBBB")
    os.replace(other, p)
    assert observe_file_target(str(p)).leaf.ino != changed.leaf.ino
    p.unlink()
    assert observe_file_target(str(p)).status == "absent"
    p.write_bytes(b"BBBB")
    assert observe_file_target(str(p)).status == "verified"


@posix_metadata
def test_absence_is_not_permission_failure_and_ancestor_replacement_changes_it(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    p = parent / "missing"
    first = observe_file_target(str(p))
    assert first.status == "absent"
    os.rename(parent, tmp_path / "original")
    parent.mkdir()
    assert observe_file_target(str(p)) != first
    parent.rmdir()
    parent.write_text("not a directory")
    assert observe_file_target(str(p)).status == "unavailable"


@posix_metadata
def test_unrelated_parent_writes_do_not_change_file_version(tmp_path):
    p = tmp_path / "target"
    p.write_text("unchanged")
    first = observe_file_target(str(p))
    (tmp_path / "unrelated").write_text("new")
    assert observe_file_target(str(p)) == first


@posix_metadata
def test_leaf_and_ancestor_links_are_not_verified(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "file").write_text("private fixture")
    leaf = tmp_path / "leaf"
    leaf.symlink_to(real / "file")
    ancestor = tmp_path / "ancestor"
    ancestor.symlink_to(real, target_is_directory=True)
    assert observe_file_target(str(leaf)).status == "unavailable"
    assert observe_file_target(str(ancestor / "file")).status == "unavailable"


@posix_metadata
def test_mode_change_and_cwd_type_are_observed(tmp_path):
    p = tmp_path / "target"
    p.write_text("same")
    p.chmod(0o600)
    before = observe_file_target(str(p))
    p.chmod(0o400)
    assert observe_file_target(str(p)) != before
    assert observe_file_target(str(tmp_path), "cwd").status == "verified"
    assert observe_file_target(str(tmp_path)).status == "unavailable"


@posix_metadata
def test_change_between_observation_walks_is_unavailable(tmp_path, monkeypatch):
    import app.services.action_review as review
    p = tmp_path / "target"
    p.write_text("before")
    original = review._observe_path
    calls = 0

    def observe(path, role):
        nonlocal calls
        calls += 1
        value = original(path, role)
        if calls == 1:
            p.write_text("after")
        return value

    monkeypatch.setattr(review, "_observe_path", observe)
    assert observe_file_target(str(p)).status == "unavailable"
    assert calls == 2


@posix_metadata
def test_observation_never_opens_file_content(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("TOKEN=fixture-not-a-real-credential")
    original = os.open
    opened = []

    def opened_directory(path, flags, *args, **kwargs):
        assert flags & os.O_DIRECTORY
        opened.append(path)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", opened_directory)
    result = observe_file_target(str(p)).model_dump_json()
    assert opened
    assert "fixture-not-a-real-credential" not in result
    assert "content" not in result
    assert "hash" not in result


@pytest.mark.parametrize("path", ["relative", "/", "//", "///", "/a/../b", "/a/./b", "/bad\0name"])
@posix_metadata
def test_invalid_path_is_not_reinterpreted(path):
    with pytest.raises(ValueError):
        observe_file_target(path)


def _authority_fixture(client, owner, project):
    room = create_room(client, owner, project, "review fixture")
    agent_id = ready_agents(client, owner, room, ("reviewer",))[0]
    with client.app.state.session_factory() as db:
        agent = db.get(Agent, agent_id)
        row = db.get(ProviderConnection, agent.connection_id)
        binding = ProposerBinding(owner_id=owner, agent_id=agent_id, room_id=room,
                                  connection_id=row.id, model_id=agent.model_id,
                                  config_revision=row.readiness_revision,
                                  auth_grant_revision=row.auth_grant_revision,
                                  provider_id=row.provider_id, protocol_id=row.protocol_id)
    return room, agent_id, binding


def test_frozen_proposer_does_not_read_secret_fields():
    class PublicConnection:
        owner_id = 1
        connection_id = 7
        config_revision = 3
        auth_grant_revision = 4
        provider_id = "fixture"
        protocol_id = "fixture_protocol"

        def __getattr__(self, name):
            raise AssertionError("No other connection fields may be read: " + name)

    binding = frozen_proposer(SimpleNamespace(connection=PublicConnection(), model_id="fixture_model",
                                              agent_id=2, room_id=3), expected_owner_id=1)
    assert binding.connection_id == 7
    assert "secret" not in binding.model_dump_json()
    with pytest.raises(ReviewChanged):
        frozen_proposer(SimpleNamespace(connection=PublicConnection(), model_id="fixture_model",
                                        agent_id=2, room_id=3), expected_owner_id=9)


@pytest.mark.parametrize("change", ["model", "connection", "config", "auth", "provider", "protocol", "disabled", "owner", "user", "membership", "room", "project"])
def test_live_changes_do_not_bless_old_proposer(room_client, change):
    client, (owner, other, project, _, _) = room_client
    room, agent_id, binding = _authority_fixture(client, owner, project)
    with client.app.state.session_factory() as db:
        before = observe_authority(db, room_id=room, agent_id=agent_id,
                                   session_id="fixture-session", proposer=binding)
        agent = db.get(Agent, agent_id)
        connection = db.get(ProviderConnection, binding.connection_id)
        if change == "model": agent.model_id = "different-model"
        elif change == "connection": agent.connection_id = None
        elif change == "config": connection.readiness_revision += 1
        elif change == "auth": connection.auth_grant_revision = (connection.auth_grant_revision or 0) + 1
        elif change == "provider": connection.provider_id = "changed-provider"
        elif change == "protocol": connection.protocol_id = "changed-protocol"
        elif change == "disabled": connection.enabled = False
        elif change == "owner": agent.user_id = other
        elif change == "user": db.get(User, owner).is_active = False
        elif change == "membership": db.get(RoomMembership, (room, agent_id)).is_active = False
        elif change == "room": db.get(Conversation, room).is_active = False
        elif change == "project": db.get(Project, project).is_active = False
        db.commit()
        with pytest.raises(ReviewChanged):
            observe_authority(db, room_id=room, agent_id=agent_id,
                              session_id=before.session_id, proposer=binding)


def test_display_rename_is_not_an_account_change(room_client):
    client, (owner, _, project, _, _) = room_client
    room, agent_id, binding = _authority_fixture(client, owner, project)
    with client.app.state.session_factory() as db:
        before = observe_authority(db, room_id=room, agent_id=agent_id,
                                   session_id="fixture-session", proposer=binding)
        db.get(Agent, agent_id).name = "display changed"
        db.get(ProviderConnection, binding.connection_id).name = "display changed"
        db.commit()
        assert observe_authority(db, room_id=room, agent_id=agent_id,
                                 session_id=before.session_id, proposer=binding) == before


@pytest.mark.parametrize("change", ["model", "config", "auth"])
def test_cached_orm_rows_do_not_hide_another_session_change(room_client, change):
    client, (owner, _, project, _, _) = room_client
    room, agent_id, binding = _authority_fixture(client, owner, project)
    with client.app.state.session_factory() as cached:
        # Hold strong references so SQLAlchemy's identity map keeps the rows.
        old_agent = cached.get(Agent, agent_id)
        old_connection = cached.get(ProviderConnection, binding.connection_id)
        observe_authority(cached, room_id=room, agent_id=agent_id,
                          session_id="fixture-session", proposer=binding)
        with client.app.state.session_factory() as other:
            if change == "model": other.get(Agent, agent_id).model_id = "different-model"
            elif change == "config": other.get(ProviderConnection, binding.connection_id).readiness_revision += 1
            else: other.get(ProviderConnection, binding.connection_id).auth_grant_revision = 9
            other.commit()
        assert old_agent.model_id == binding.model_id
        assert old_connection.readiness_revision == binding.config_revision
        with pytest.raises(ReviewChanged):
            observe_authority(cached, room_id=room, agent_id=agent_id,
                              session_id="fixture-session", proposer=binding)


def test_transferring_all_live_rows_does_not_change_the_frozen_owner(room_client):
    client, (owner, other, project, _, _) = room_client
    room, agent_id, binding = _authority_fixture(client, owner, project)
    with client.app.state.session_factory() as db:
        db.get(Conversation, room).user_id = other
        db.get(Project, project).user_id = other
        db.get(Agent, agent_id).user_id = other
        db.get(ProviderConnection, binding.connection_id).user_id = other
        db.commit()
        with pytest.raises(ReviewChanged):
            observe_authority(db, room_id=room, agent_id=agent_id,
                              session_id="fixture-session", proposer=binding)


def test_types_reject_caller_labels_and_false_verified_state():
    frame = {"owner_id": 1, "agent_id": 2, "room_id": 3}
    with pytest.raises(ValidationError):
        ProposerBinding(**frame, connection_id=True, model_id="m", config_revision=1,
                        auth_grant_revision=None, provider_id=None, protocol_id=None)
    with pytest.raises(ValidationError):
        ProposerBinding(**frame, connection_id=1, model_id="m", config_revision=1,
                        auth_grant_revision=None, provider_id=None, protocol_id=None,
                        account="caller supplied")
    with pytest.raises(ValidationError):
        FileTarget(role="path", path="/fixture", status="verified", parents=[], leaf=None)
    with pytest.raises(ValidationError):
        ProposerBinding(**frame, connection_id=1, model_id="m", config_revision=0,
                        auth_grant_revision=None, provider_id=None, protocol_id=None)
