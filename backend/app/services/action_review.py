"""Read-only server observations for consent, never authorization or effects.

File metadata is a version observation, not an atomic content CAS. Executors
must independently check the observed state at their own effect boundary.
No credential value, reference, cookie or content fingerprint is captured.
"""

import os
import stat
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from app.models.collaboration import ProviderConnection, RoomMembership
from app.models.models import Agent, Conversation, Project, User


UInt = Annotated[StrictInt, Field(ge=0)]
Positive = Annotated[StrictInt, Field(gt=0)]
Name = Annotated[StrictStr, Field(min_length=1, max_length=255)]
PathValue = Annotated[StrictStr, Field(min_length=1, max_length=8192)]


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, protected_namespaces=())


class ProposerBinding(Observation):
    """Public identity frozen before the provider call, not caller input."""
    owner_id: Positive
    agent_id: Positive
    room_id: Positive
    connection_id: Positive
    model_id: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    config_revision: Positive | None
    auth_grant_revision: UInt | None
    provider_id: Annotated[StrictStr, Field(max_length=64)] | None
    protocol_id: Annotated[StrictStr, Field(max_length=64)] | None
    # Core's public authentication generation, never a credential fingerprint.
    owner_epoch: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] | None = None


class Authority(Observation):
    owner_id: Positive
    room_id: Positive
    agent_id: Positive
    session_id: Annotated[StrictStr, Field(min_length=1, max_length=36)]
    os_user_status: Literal["verified", "unsupported"]
    euid: UInt | None
    proposer: ProposerBinding

    @model_validator(mode="after")
    def truthful_os_identity(self):
        if (self.os_user_status == "verified") != (self.euid is not None):
            raise ValueError("OS user observation is inconsistent")
        return self


class ParentIdentity(Observation):
    component: Name
    dev: UInt
    ino: UInt
    mode: UInt
    uid: UInt
    gid: UInt


class FileVersion(Observation):
    dev: UInt
    ino: UInt
    mode: UInt
    uid: UInt
    gid: UInt
    size: UInt
    mtime_ns: StrictInt
    ctime_ns: StrictInt


class FileTarget(Observation):
    role: Literal["path", "source", "destination", "executable", "cwd"]
    path: PathValue
    status: Literal["verified", "absent", "unavailable", "unsupported"]
    parents: Annotated[list[ParentIdentity], Field(max_length=128)]
    leaf: FileVersion | None = None

    @model_validator(mode="after")
    def truthful_target(self):
        if (self.status == "verified") != (self.leaf is not None):
            raise ValueError("Target observation is inconsistent")
        if self.status in ("verified", "absent") and not self.parents:
            raise ValueError("Target observation has no parent anchor")
        return self


class ReviewChanged(ValueError):
    """The trusted action proposer or execution authority is no longer live."""


def frozen_proposer(request, *, expected_owner_id: int, owner_epochs=None) -> ProposerBinding:
    """Read only non-secret fields of an internally built ProviderRequest."""
    connection = request.connection
    if connection.owner_id is not None and connection.owner_id != expected_owner_id:
        raise ReviewChanged("Provider request Owner changed")
    return ProposerBinding(
        owner_id=expected_owner_id, agent_id=request.agent_id, room_id=request.room_id,
        connection_id=connection.connection_id, model_id=request.model_id,
        config_revision=connection.config_revision,
        auth_grant_revision=connection.auth_grant_revision,
        provider_id=connection.provider_id, protocol_id=connection.protocol_id,
        owner_epoch=observed_owner_epoch(owner_epochs, expected_owner_id),
    )


def observed_owner_epoch(provider, owner_id):
    if provider is None:
        return None  # Source-only launches do not have a Core auth channel.
    value = provider.current(owner_id)
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ReviewChanged("Owner authentication binding is unavailable")
    return value


def observe_authority(db, *, room_id: int, agent_id: int, session_id: str,
                      proposer: ProposerBinding, owner_epochs=None) -> Authority:
    """Require the live DB assignment to match the *original* proposer.

    Reading current assignment and calling it the old proposal's identity
    would silently bless a connection change while a provider was answering.
    This function neither resolves credentials nor issues a grant.
    """
    if not isinstance(proposer, ProposerBinding):
        raise ReviewChanged("Trusted proposer binding is missing")
    # A caller may already have these rows in its ORM identity map. Re-read
    # them rather than accepting the objects cached before a provider call.
    room = db.get(Conversation, room_id, populate_existing=True)
    agent = db.get(Agent, agent_id, populate_existing=True)
    member = db.get(RoomMembership, (room_id, agent_id), populate_existing=True)
    owner = db.get(User, room.user_id, populate_existing=True) if room is not None else None
    project = db.get(Project, room.project_id, populate_existing=True) if room is not None else None
    connection = db.get(ProviderConnection, proposer.connection_id, populate_existing=True)
    if (room is None or not room.is_active or owner is None or not owner.is_active
            or proposer.room_id != room_id or proposer.agent_id != agent_id
            or proposer.owner_id != room.user_id
            or project is None or project.user_id != room.user_id or not project.is_active
            or agent is None or not agent.is_active or agent.user_id != room.user_id
            or member is None or not member.is_active
            or agent.connection_id != proposer.connection_id
            or agent.model_id != proposer.model_id
            or connection is None or not connection.enabled or connection.user_id != room.user_id):
        raise ReviewChanged("Execution authority changed")
    current = ProposerBinding(
        owner_id=room.user_id, agent_id=agent_id, room_id=room_id,
        connection_id=connection.id, model_id=agent.model_id,
        config_revision=connection.readiness_revision,
        auth_grant_revision=connection.auth_grant_revision,
        provider_id=connection.provider_id, protocol_id=connection.protocol_id,
        owner_epoch=observed_owner_epoch(owner_epochs, proposer.owner_id),
    )
    if current != proposer:
        raise ReviewChanged("Proposer connection changed")
    euid = os.geteuid() if hasattr(os, "geteuid") else None
    return Authority(owner_id=room.user_id, room_id=room_id, agent_id=agent_id,
                     session_id=session_id, os_user_status="verified" if euid is not None else "unsupported",
                     euid=euid, proposer=proposer)


def _parent_identity(info, component):
    return ParentIdentity(component=component, dev=info.st_dev, ino=info.st_ino,
                          mode=info.st_mode, uid=info.st_uid, gid=info.st_gid)


def _file_version(info):
    return FileVersion(dev=info.st_dev, ino=info.st_ino, mode=info.st_mode,
                       uid=info.st_uid, gid=info.st_gid, size=info.st_size,
                       mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)


def _observe_path(path: str, role: str) -> FileTarget:
    """Open every ancestor without links and close every descriptor."""
    parts = [part for part in path.split(os.sep) if part]
    parents = []
    fd = None
    try:
        fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parents.append(_parent_identity(os.fstat(fd), os.sep))
        if not parts and role == "cwd":
            return FileTarget(role=role, path=path, status="verified", parents=parents,
                              leaf=_file_version(os.fstat(fd)))
        for component in parts[:-1]:
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return FileTarget(role=role, path=path, status="absent", parents=parents)
            old, fd = fd, child
            os.close(old)
            parents.append(_parent_identity(os.fstat(fd), component))
        try:
            leaf = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return FileTarget(role=role, path=path, status="absent", parents=parents)
        regular = stat.S_ISDIR(leaf.st_mode) if role == "cwd" else stat.S_ISREG(leaf.st_mode)
        if not regular:
            return FileTarget(role=role, path=path, status="unavailable", parents=parents)
        return FileTarget(role=role, path=path, status="verified", parents=parents,
                          leaf=_file_version(leaf))
    except OSError:
        # Includes symlinks, inaccessible ancestors and non-directory parents.
        return FileTarget(role=role, path=path, status="unavailable", parents=parents)
    finally:
        if fd is not None:
            os.close(fd)


def observe_file_target(path: str, role: str = "path") -> FileTarget:
    """Bounded no-content metadata observations, explicitly not atomic CAS.

    Two independent walks reject a target or parent changing *during* the
    observation. Changes after it returns still require an executor recheck.
    Parent mtime/ctime are deliberately excluded: unrelated file creation
    must not revoke a consent bound to another file in the same directory.
    """
    parts = [p for p in path.split(os.sep) if p] if isinstance(path, str) else []
    if (not isinstance(path, str) or not os.path.isabs(path) or len(path) > 8192
            or "\0" in path or any(part in (".", "..") for part in path.split(os.sep))
            or (not parts and not (path == os.sep and role == "cwd")) or len(parts) > 128
            or role not in ("path", "source", "destination", "executable", "cwd")):
        raise ValueError("Invalid review target")
    if os.name == "nt" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        return FileTarget(role=role, path=path, status="unsupported", parents=[])
    first = _observe_path(path, role)
    if first.status not in ("verified", "absent"):
        return first
    second = _observe_path(path, role)
    if first != second:
        return FileTarget(role=role, path=path, status="unavailable", parents=[])
    return first
