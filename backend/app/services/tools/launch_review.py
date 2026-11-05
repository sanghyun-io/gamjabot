"""Bounded, read-only broker-to-guardian launch metadata over an inherited FD.

No argv/env/account labels become authority here. Namespace and metadata
checks immediately before path-based Popen are not atomic content CAS or
proof of the services/effects performed by the launched program.
This module uses only stdlib so a source guardian can import it directly.
"""

import contextlib
try:
    import fcntl
except ImportError:  # Windows has no trusted POSIX launch FD observation.
    fcntl = None
import json
import os
import stat
import tempfile
from uuid import UUID

LIMIT = 65_536
PARENT_KEYS = {"component", "dev", "ino", "mode", "uid", "gid"}
LEAF_KEYS = {"dev", "ino", "mode", "uid", "gid", "size", "mtime_ns", "ctime_ns"}


def _id(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Invalid launch identity")


def _numbers(value, keys):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Invalid launch metadata")
    if any(type(value[k]) is not int for k in keys):
        raise ValueError("Invalid launch metadata types")


def validate(value, command):
    if (not isinstance(value, dict) or set(value) != {"version", "action_id", "review_id", "euid", "targets"}
            or type(value["version"]) is not int or value["version"] != 1
            or type(value["euid"]) is not int or value["euid"] != os.geteuid()):
        raise ValueError("Launch authority is unavailable")
    _id(value["action_id"]); _id(value["review_id"])
    if UUID(value["review_id"]).version != 4:
        raise ValueError("Invalid launch review")
    targets = value["targets"]
    if not isinstance(targets, list) or len(targets) != 2:
        raise ValueError("Launch needs executable and cwd")
    for target, role in zip(targets, ("executable", "cwd")):
        if (not isinstance(target, dict) or set(target) != {"role", "path", "status", "parents", "leaf"}
                or target["role"] != role or target["status"] != "verified"):
            raise ValueError("Unsupported launch target")
        path = target["path"]
        if (not isinstance(path, str) or not os.path.isabs(path) or len(path) > 8192
                or "\0" in path or "//" in path
                or any(x in (".", "..") for x in path.split("/"))
                or path == "/" and role != "cwd"):
            raise ValueError("Invalid launch path")
        parts = [x for x in path.split("/") if x]
        parents = target["parents"]
        components = ["/"] + parts[:-1]
        if (not isinstance(parents, list) or len(parents) != len(components)
                or len(parents) > 128):
            raise ValueError("Incomplete launch parent chain")
        for parent, component in zip(parents, components):
            if not isinstance(parent, dict) or parent.get("component") != component:
                raise ValueError("Launch parent changed")
            _numbers({k: v for k, v in parent.items() if k != "component"}, PARENT_KEYS - {"component"})
        _numbers(target["leaf"], LEAF_KEYS)
    if not command or command[0] != targets[0]["path"]:
        raise ValueError("Launch executable changed")
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate launch field")
        result[key] = value
    return result


def read_review(fd, command):
    info = os.fstat(fd)
    if (fcntl is None or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o400
            or info.st_uid != os.geteuid() or info.st_nlink != 0 or not 1 <= info.st_size <= LIMIT
            or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY):
        raise ValueError("Unprotected launch precondition FD")
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, LIMIT + 1)
    if len(raw) != info.st_size:
        raise ValueError("Launch preconditions changed")
    return validate(json.loads(raw, object_pairs_hook=_unique), command)


@contextlib.contextmanager
def protected_review(value):
    """Only a read-only unlinked descriptor survives into the guardian."""
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    if len(raw) > LIMIT:
        raise ValueError("Launch review exceeds its bound")
    write_fd, path = tempfile.mkstemp(prefix="gamja-launch-review-")
    original = os.fstat(write_fd)
    read_fd = None
    try:
        with os.fdopen(write_fd, "wb") as out:
            write_fd = None
            out.write(raw); out.flush(); os.fsync(out.fileno())
            os.fchmod(out.fileno(), 0o400)
            read_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            if os.fstat(read_fd).st_ino != os.fstat(out.fileno()).st_ino:
                raise ValueError("Launch review file was replaced")
            os.unlink(path)
        yield read_fd
    finally:
        if write_fd is not None:
            os.close(write_fd)
        if read_fd is not None:
            os.close(read_fd)
        # The owned name is unlinked before yield. Never sweep a replacement.
        try:
            current = os.stat(path, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
                os.unlink(path)
        except FileNotFoundError:
            pass


def _matches(info, expected):
    fields = {"dev": info.st_dev, "ino": info.st_ino, "mode": info.st_mode,
              "uid": info.st_uid, "gid": info.st_gid, "size": info.st_size,
              "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}
    return all(fields[k] == v for k, v in expected.items() if k != "component")


@contextlib.contextmanager
def checked_targets(value):
    """Hold all checked parent/leaf FDs across the final synchronous launch."""
    descriptors = []
    leaves = []
    try:
        for target in value["targets"]:
            parents = target["parents"]
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(fd)
            if not _matches(os.fstat(fd), parents[0]):
                raise ValueError("Launch root changed")
            for parent in parents[1:]:
                fd = os.open(parent["component"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                descriptors.append(fd)
                if not _matches(os.fstat(fd), parent):
                    raise ValueError("Launch parent changed")
            name = os.path.basename(target["path"]) or "."
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if target["role"] == "cwd":
                flags |= os.O_DIRECTORY
            leaf = os.open(name, flags, dir_fd=fd)
            descriptors.append(leaf)
            if not _matches(os.fstat(leaf), target["leaf"]):
                raise ValueError("Launch target changed")
            leaves.append(leaf)
        yield leaves
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def recheck(value, leaves):
    # Rewalk canonical paths while retaining the first opened objects. Parent
    # namespace replacement cannot make an old held FD authorize a new path.
    with checked_targets(value):
        if any(not _matches(os.fstat(fd), target["leaf"])
               for target, fd in zip(value["targets"], leaves)):
            raise ValueError("Opened launch target changed")
