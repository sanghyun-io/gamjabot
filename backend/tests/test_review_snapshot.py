"""Synthetic file reviews and durable rows; no installed runtime or effects."""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from test_room_state import room_client
from test_action_review import _authority_fixture, posix_metadata
from app.models.collaboration import ToolAction
from app.services.action_review import ReviewChanged, observe_authority
from app.services.review_snapshot import (
    ReviewSnapshot, capture_file_review, retained_review, same_review_observations,
    snapshot_value, stage_initial_review,
)
from app.services.tool_broker import prepare


@pytest.fixture
def review_fixture(room_client, tmp_path):
    client, (owner, _, project, _, _) = room_client
    room, agent_id, proposer = _authority_fixture(client, owner, project)
    path = tmp_path.resolve() / "file"
    path.write_text("fixture content, never copied into a review")
    prepared = prepare("read_file", {"path": str(path)})
    session = str(uuid4())
    with client.app.state.session_factory() as db:
        authority = observe_authority(db, room_id=room, agent_id=agent_id,
                                      session_id=session, proposer=proposer)
    action = ToolAction(action_id=str(uuid4()), room_id=room, agent_id=agent_id,
                        trigger_event_id=str(uuid4()), call_id="fixture-call", workflow_id=None,
                        session_id=session, state="waiting", family=prepared.family,
                        arguments=prepared.public_arguments, target=prepared.target,
                        scope_target=prepared.scope_target, expected_effect=prepared.expected_effect,
                        payload_digest=prepared.payload_digest)
    return client, action, prepared, authority, path


@posix_metadata
def test_round_trip_persists_binding_but_no_content_or_provider_secret(review_fixture):
    client, action, prepared, authority, path = review_fixture
    snapshot = capture_file_review(action, prepared, authority)
    review_id = stage_initial_review(action, snapshot)
    action_id = action.action_id
    with client.app.state.session_factory() as db:
        db.add(action); db.commit()
    with client.app.state.session_factory() as db:
        row = db.get(ToolAction, action_id)
        assert row.review_id == review_id
        assert retained_review(row) == snapshot
        assert row.approved_at is None and row.approval_owner_id is None
        encoded = json.dumps(row.review_snapshot)
        assert path.read_text() not in encoded
        assert "secret_ref" not in encoded and "arguments" not in encoded
        assert "auth_grant_revision" in encoded
        assert row.review_snapshot["checks"]["content_CAS"] == "unsupported"


@posix_metadata
def test_unique_ids_cannot_link_two_different_actions(review_fixture):
    client, action, prepared, authority, _ = review_fixture
    stage_initial_review(action, capture_file_review(action, prepared, authority))
    with client.app.state.session_factory() as db:
        db.add(action); db.commit()
        other = ToolAction(action_id=str(uuid4()), room_id=action.room_id, agent_id=action.agent_id,
                           family="read_file", arguments={}, state="waiting", review_id=action.review_id)
        db.add(other)
        with pytest.raises(IntegrityError): db.commit()
        db.rollback()
        assert db.get(ToolAction, other.action_id) is None
        assert db.get(ToolAction, action.action_id).review_id == action.review_id


@posix_metadata
def test_staging_is_detached_and_does_not_refresh_an_existing_or_approved_row(review_fixture):
    _, action, prepared, authority, _ = review_fixture
    snapshot = capture_file_review(action, prepared, authority)
    stage_initial_review(action, snapshot)
    action.review_snapshot["targets"][0]["leaf"]["size"] += 1
    assert snapshot.targets[0].leaf.size != action.review_snapshot["targets"][0]["leaf"]["size"]
    with pytest.raises(ReviewChanged): stage_initial_review(action, snapshot)
    action.review_id = action.review_snapshot = None
    action.state = "approved"
    with pytest.raises(ReviewChanged): stage_initial_review(action, snapshot)
    action.state = "waiting"
    action.approved_at = datetime.now(timezone.utc)
    with pytest.raises(ReviewChanged): stage_initial_review(action, snapshot)
    assert action.review_id is None


@pytest.mark.parametrize("change", ["legacy", "id", "action", "room", "agent", "session", "digest", "extra", "cross_binding", "version", "oversized"])
@posix_metadata
def test_retained_legacy_or_tampered_reviews_fail_closed(review_fixture, change):
    _, action, prepared, authority, _ = review_fixture
    stage_initial_review(action, capture_file_review(action, prepared, authority))
    if change == "legacy": action.review_id = action.review_snapshot = None
    elif change == "id": action.review_id = str(uuid4()).upper()
    elif change == "action": action.action_id = str(uuid4())
    elif change == "room": action.room_id += 1
    elif change == "agent": action.agent_id += 1
    elif change == "session": action.session_id = str(uuid4())
    elif change == "digest": action.payload_digest = "f" * 64
    elif change == "extra": action.review_snapshot["credential_ref"] = "not-allowed"
    elif change == "cross_binding": action.review_snapshot["authority"]["proposer"]["owner_id"] += 1
    elif change == "version": action.review_snapshot["version"] = True
    elif change == "oversized": action.review_snapshot["padding"] = "x" * 65_536
    with pytest.raises(ReviewChanged): retained_review(action)


@posix_metadata
def test_changed_payload_cannot_be_relabelled_as_original_file_review(review_fixture):
    _, action, prepared, authority, path = review_fixture
    different = prepare("write_file", {"path": str(path), "content": "fixture"})
    with pytest.raises(ReviewChanged): capture_file_review(action, different, authority)
    assert action.review_id is None


@posix_metadata
def test_write_review_binds_the_existing_digest_suffix_and_bare_scope_path(review_fixture):
    _, action, _, authority, path = review_fixture
    prepared = prepare("write_file", {"path": str(path), "content": "synthetic private content"})
    for name in ("family", "target", "scope_target", "expected_effect", "payload_digest"):
        setattr(action, name, getattr(prepared, name))
    snapshot = capture_file_review(action, prepared, authority)
    assert snapshot.action.scope_target == str(path)
    assert snapshot.action.target == f"{path} [payload sha256:{prepared.payload_digest[:16]}]"
    assert "synthetic private content" not in json.dumps(snapshot_value(snapshot))
    stage_initial_review(action, snapshot)
    assert retained_review(action) == snapshot
    value = snapshot_value(snapshot)
    value["action"]["target"] = str(path)
    with pytest.raises(ValidationError): ReviewSnapshot.model_validate_json(json.dumps(value))


@posix_metadata
def test_capture_time_is_ignored_but_file_changes_remain_different(review_fixture):
    _, action, prepared, authority, path = review_fixture
    before = capture_file_review(action, prepared, authority)
    after = capture_file_review(action, prepared, authority)
    assert before.captured_at != after.captured_at
    assert same_review_observations(before, after)
    (path.parent / "unrelated").write_text("unrelated")
    assert same_review_observations(before, capture_file_review(action, prepared, authority))
    path.write_bytes(b"x" * path.stat().st_size)
    assert not same_review_observations(before, capture_file_review(action, prepared, authority))


@posix_metadata
def test_move_observes_both_exact_targets_and_retains_absence(review_fixture):
    _, action, _, authority, path = review_fixture
    destination = path.parent / "destination"
    prepared = prepare("move_file", {"source": str(path), "destination": str(destination)})
    for name in ("family", "target", "scope_target", "expected_effect", "payload_digest"):
        setattr(action, name, getattr(prepared, name))
    snapshot = capture_file_review(action, prepared, authority)
    assert [(t.role, t.status) for t in snapshot.targets] == [("source", "verified"), ("destination", "absent")]
    destination.write_text("other file")
    assert not same_review_observations(snapshot, capture_file_review(action, prepared, authority))
    value = snapshot_value(snapshot)
    value["targets"] = value["targets"][:1]
    with pytest.raises(ValidationError): ReviewSnapshot.model_validate_json(json.dumps(value))


@posix_metadata
def test_unsupported_observation_is_not_filled_or_authorized(review_fixture, monkeypatch):
    import app.services.review_snapshot as module
    from app.services.action_review import FileTarget
    _, action, prepared, authority, path = review_fixture
    monkeypatch.setattr(module, "observe_file_target", lambda p, role="path":
                        FileTarget(path=p, role=role, status="unsupported", parents=[]))
    snapshot = capture_file_review(action, prepared, authority)
    assert snapshot.targets[0].status == "unsupported" and snapshot.targets[0].leaf is None
    assert action.state == "waiting" and action.review_id is None


@posix_metadata
def test_nested_sequences_are_immutable_and_non_utc_times_are_rejected(review_fixture):
    _, action, prepared, authority, _ = review_fixture
    snapshot = capture_file_review(action, prepared, authority)
    with pytest.raises(TypeError): snapshot.targets[0] = snapshot.targets[0]
    with pytest.raises(TypeError): snapshot.targets[0].parents[0] = snapshot.targets[0].parents[0]
    for value in [datetime.now(), datetime.now(timezone(timedelta(hours=9)))]:
        fields = {**snapshot.model_dump(), "captured_at": value}
        with pytest.raises(ValidationError): ReviewSnapshot.model_validate(fields)
