"""Typed durable reviews, not a grant or an executor boundary.

The broker must still retain the original proposer, recheck live authority,
claim atomically and pass these preconditions to the conditional executor.
Unavailable external accounts/versions remain unsupported. Local workflow
identities never turn them into verified service accounts.
"""

import json
import os
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator, model_serializer

from app.services.action_review import (
    Authority, FileTarget, Observation, ParentIdentity, Positive, ReviewChanged,
    observe_file_target,
)
from app.services.review_runtime import (
    BrowserTarget, DesktopTarget, OwnedProcessTarget, MCPReviewTarget, VaultProfileTarget, VoiceMemoTarget, observe_runtime_target,
)

MAX_SNAPSHOT_BYTES = 65_536
CanonicalID = Annotated[StrictStr, Field(min_length=36, max_length=36)]
ShortValue = Annotated[StrictStr, Field(min_length=1, max_length=200)]
TargetValue = Annotated[StrictStr, Field(min_length=1, max_length=16388)]
Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
VersionOne = Annotated[StrictInt, Field(ge=1, le=1)]


def _canonical_id(value):
    if str(UUID(value)) != value:
        raise ValueError("Non-canonical review identity")
    return value


class ActionBinding(Observation):
    action_id: CanonicalID
    room_id: Positive
    agent_id: Positive
    trigger_event_id: CanonicalID | None
    call_id: ShortValue | None
    workflow_id: ShortValue | None
    session_id: CanonicalID
    family: Literal["read_file", "write_file", "delete_file", "move_file", "start_process", "cancel_process",
                    "request_vault_login", "browser_navigate", "browser_inspect", "browser_interact", "browser_screenshot", "screenshot",
                    "pointer_click", "pointer_move", "pointer_scroll", "keyboard_text", "keyboard_chord", "mcp_tool_call", "synthesize_voice_memo"]
    target: TargetValue
    scope_target: TargetValue
    expected_effect: Annotated[StrictStr, Field(min_length=1, max_length=2048)]
    payload_digest: Digest
    execution_binding: dict | None = None
    mcp_binding: dict | None = None

    @model_serializer(mode='wrap')
    def legacy_shape(self, handler):
        value = handler(self)
        if self.execution_binding is None:
            value.pop('execution_binding', None)
        if self.mcp_binding is None:
            value.pop('mcp_binding', None)
        return value

    @field_validator("action_id", "trigger_event_id", "session_id")
    @classmethod
    def canonical_identity(cls, value):
        return _canonical_id(value) if value is not None else value


class StoredTarget(FileTarget):
    # Frozen models with mutable nested lists can be modified without
    # assignment. Persist immutable parent chains and target collections.
    parents: Annotated[tuple[ParentIdentity, ...], Field(max_length=128)]
    role: Literal["path", "source", "destination", "executable", "cwd"]

    @field_validator("path")
    @classmethod
    def safe_absolute_path(cls, value, info):
        parts = value.split(os.sep)
        if (not os.path.isabs(value) or "\0" in value
                or (not any(parts) and not (value == os.sep and info.data.get("role") == "cwd"))
                or any(part in (".", "..") for part in parts)
                or len([part for part in parts if part]) > 128):
            raise ValueError("Invalid stored target path")
        return value


class ExternalContext(Observation):
    account: Literal["not_applicable", "unsupported"] = "not_applicable"
    target_version: Literal["not_applicable", "unsupported"] = "not_applicable"


class ReviewChecks(Observation):
    target: Literal["metadata_checked", "runtime_observed"] = "metadata_checked"
    content_CAS: Literal["unsupported"] = "unsupported"
    rule_version: VersionOne = 1
    owner_authentication: Literal["verified", "unsupported"] = "unsupported"


class ReviewSnapshot(Observation):
    version: Annotated[StrictInt, Field(ge=1, le=2)] = 1
    captured_at: datetime
    action: ActionBinding
    authority: Authority
    targets: Annotated[tuple[StoredTarget | OwnedProcessTarget | BrowserTarget | DesktopTarget | MCPReviewTarget | VaultProfileTarget | VoiceMemoTarget, ...],
                       Field(min_length=1, max_length=2)]
    external_context: ExternalContext = Field(default_factory=ExternalContext)
    checks: ReviewChecks = Field(default_factory=ReviewChecks)

    @model_validator(mode="after")
    def consistent_bindings(self):
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset().total_seconds() != 0:
            raise ValueError("Review time must be UTC")
        a, authority = self.action, self.authority
        if self.version != (2 if a.execution_binding is not None else 1):
            raise ValueError('Review scope version changed')
        proposer = authority.proposer
        if (a.room_id != authority.room_id or a.agent_id != authority.agent_id
                or a.session_id != authority.session_id
                or proposer.owner_id != authority.owner_id
                or proposer.room_id != a.room_id or proposer.agent_id != a.agent_id):
            raise ValueError("Inconsistent execution identity")
        roles = tuple(t.role for t in self.targets)
        if a.family == "move_file":
            if roles != ("source", "destination"):
                raise ValueError("Move review needs both targets")
            target = f"{self.targets[0].path} → {self.targets[1].path}"
        elif a.family in ("read_file", "write_file", "delete_file"):
            if roles != ("path",):
                raise ValueError("File review needs its exact target")
            target = self.targets[0].path
        elif a.family == "start_process":
            if roles != ("executable", "cwd") or a.scope_target != self.targets[0].path:
                raise ValueError("Process review needs its executable and cwd")
            target = a.target  # Invocation remains bound by the original prepared payload.
        elif a.family == "mcp_tool_call":
            if roles not in (("mcp_server",), ("mcp_server", "executable")) or not isinstance(a.mcp_binding,dict):
                raise ValueError("MCP review proof is missing")
            t=self.targets[0];meta=a.mcp_binding
            if (meta.get('owner_id'),meta.get('agent_id'),meta.get('root_event_id')) != (authority.owner_id,a.agent_id,a.trigger_event_id):
                raise ValueError('MCP Owner origin changed')
            if (t.room_id,t.agent_id,t.session_id,t.workflow_id)!=(a.room_id,a.agent_id,a.session_id,a.workflow_id):raise ValueError("MCP ownership changed")
            for field in ("server_id","config_revision","binding_revision","tool_name","schema_sha256","transport_sha256"):
                if getattr(t,field)!=meta.get(field):raise ValueError("MCP target changed")
            if meta.get('transport_type')=='stdio' and roles!=("mcp_server","executable"):raise ValueError("MCP executable identity missing")
            if meta.get('transport_type')=='http' and roles!=("mcp_server",):raise ValueError("MCP transport changed")
            target=a.target
        else:
            role = ("voice_memo" if a.family == "synthesize_voice_memo" else "vault_profile" if a.family == "request_vault_login" else
                    "owned_process" if a.family == "cancel_process" else
                    "browser_context" if a.family.startswith("browser_") else "desktop_channel")
            if roles != (role,):
                raise ValueError("Runtime review target family changed")
            t = self.targets[0]
            if (t.room_id, t.agent_id, t.session_id, t.workflow_id) != (a.room_id, a.agent_id, a.session_id, a.workflow_id):
                raise ValueError("Runtime review target ownership changed")
            if a.family == "cancel_process" and a.target != f"process:{t.handle}":
                raise ValueError("Owned process handle changed")
            target = a.target
        files = a.family in ("read_file", "write_file", "delete_file", "move_file")
        expected_external = "not_applicable" if files else "unsupported"
        if (self.external_context.account != expected_external
                or self.external_context.target_version != expected_external
                or self.checks.owner_authentication != ("verified" if proposer.owner_epoch is not None else "unsupported")
                or self.checks.target != ("metadata_checked" if files or a.family == "start_process" else "runtime_observed")):
            raise ValueError("Review verification limits changed")
        display_target = (f"{target} [payload sha256:{a.payload_digest[:16]}]"
                          if a.family == "write_file" else target)
        if a.target != display_target or (files and a.scope_target != target):
            raise ValueError("Review target does not match the action")
        return self


def _binding(action) -> ActionBinding:
    return ActionBinding(**{name: getattr(action, name, None) for name in ActionBinding.model_fields})


def snapshot_value(snapshot: ReviewSnapshot) -> dict:
    """Validate again and detach bounded JSON before handing it to the ORM."""
    if not isinstance(snapshot, ReviewSnapshot):
        raise ReviewChanged("Trusted review snapshot is missing")
    encoded = snapshot.model_dump_json()
    if len(encoded.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ReviewChanged("Review snapshot exceeds its bound")
    try:
        validated = ReviewSnapshot.model_validate_json(encoded)
    except ValueError as exc:
        raise ReviewChanged("Review snapshot is invalid") from exc
    return json.loads(validated.model_dump_json())


def capture_file_review(action, prepared, authority: Authority) -> ReviewSnapshot:
    """Observe internal normalized file arguments without reading contents.

    No commit, approval or execution happens here. Even a verified result
    still needs the later broker/executor checks. Unknown observations remain
    unknown; callers may display them but must not infer an executable grant.
    """
    if action.family not in ("read_file", "write_file", "delete_file", "move_file"):
        raise ReviewChanged("File review family changed")
    return capture_review(action, prepared, authority)


def capture_review(action, prepared, authority: Authority, executor=None) -> ReviewSnapshot:
    binding = _binding(action)
    if any(getattr(binding, name) != getattr(prepared, name) for name in
           ("family", "target", "scope_target", "expected_effect", "payload_digest")):
        raise ReviewChanged("Original action payload changed")
    raw = prepared.raw_arguments
    if binding.family == "move_file":
        targets = (observe_file_target(raw["source"], "source"),
                   observe_file_target(raw["destination"], "destination"))
    elif binding.family in ("read_file", "write_file", "delete_file"):
        targets = (observe_file_target(raw["path"]),)
    elif binding.family == "start_process":
        targets = (observe_file_target(raw["executable"], "executable"),
                   observe_file_target(raw.get("cwd") or os.getcwd(), "cwd"))
    elif binding.family == "mcp_tool_call":
        meta=binding.mcp_binding
        if not isinstance(meta,dict):raise ReviewChanged("MCP binding unavailable")
        target=MCPReviewTarget(room_id=action.room_id,agent_id=action.agent_id,session_id=action.session_id,
            workflow_id=action.workflow_id,**{k:meta[k] for k in ("server_id","config_revision","binding_revision","tool_name","schema_sha256","transport_sha256")})
        targets=(target,observe_file_target(meta['executable'],'executable')) if meta.get('transport_type')=='stdio' else (target,)
    else:
        targets = (observe_runtime_target(action, prepared, executor),)
    stored = tuple(StoredTarget.model_validate_json(t.model_dump_json()) if isinstance(t, FileTarget) else t
                   for t in targets)
    files = binding.family in ("read_file", "write_file", "delete_file", "move_file")
    snapshot = ReviewSnapshot(version=2 if binding.execution_binding else 1, captured_at=datetime.now(timezone.utc), action=binding,
        authority=authority, targets=stored,
        external_context=ExternalContext(account="not_applicable" if files else "unsupported",
                                         target_version="not_applicable" if files else "unsupported"),
        checks=ReviewChecks(target="metadata_checked" if files or binding.family == "start_process" else "runtime_observed",
                           owner_authentication="verified" if authority.proposer.owner_epoch is not None else "unsupported"))
    snapshot_value(snapshot)
    return snapshot


def stage_initial_review(action, snapshot: ReviewSnapshot) -> str:
    """Stage an unapproved row only; leave commit/CAS to the broker transaction."""
    value = snapshot_value(snapshot)
    if (_binding(action) != snapshot.action or action.state not in ("pending", "waiting")
            or action.review_id is not None or action.review_snapshot is not None
            or action.approved_at is not None or action.approval_owner_id is not None):
        raise ReviewChanged("Action is not an unreviewed pending proposal")
    review_id = str(uuid4())
    action.review_id, action.review_snapshot = review_id, value
    return review_id


def retained_review(action) -> ReviewSnapshot:
    """Reject missing, malformed or cross-action durable snapshots, no backfill."""
    try:
        _canonical_id(action.review_id)
        if UUID(action.review_id).version != 4 or not isinstance(action.review_snapshot, dict):
            raise ValueError("Invalid retained review")
        encoded = json.dumps(action.review_snapshot, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise ValueError("Oversized retained review")
        snapshot = ReviewSnapshot.model_validate_json(encoded)
        if snapshot.action != _binding(action):
            raise ValueError("Retained review no longer matches action")
        return snapshot
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReviewChanged("Trusted retained review is missing or changed") from exc


def same_review_observations(before: ReviewSnapshot, after: ReviewSnapshot) -> bool:
    """Only capture time is ignored; identities, targets and limits still bind."""
    a, b = snapshot_value(before), snapshot_value(after)
    a.pop("captured_at"); b.pop("captured_at")
    return a == b
