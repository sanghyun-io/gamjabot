"""File effects anchored to opened parents, with no link traversal."""

import asyncio
import ctypes
import errno
import os
import stat
import sys
import uuid
from contextlib import ExitStack
from dataclasses import dataclass

from app.services.action_review import ReviewChanged, _file_version, _parent_identity
from app.services.file_preconditions import FilePostconditions
from app.services.review_snapshot import ActionBinding, ReviewSnapshot, StoredTarget
from app.services.review_execution import canonical_review_id

from app.services.tool_broker import ActionResult


class PartialMove(Exception):
    pass


class FileCancelled(Exception):
    pass


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise FileCancelled


def _rename_exclusive(source_fd, source, dest_fd, destination):
    """Atomically rename without replacing an existing destination name."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        call = libc.renameatx_np
        result = call(source_fd, os.fsencode(source), dest_fd, os.fsencode(destination), 0x4)
    elif sys.platform.startswith("linux"):
        call = libc.renameat2
        result = call(source_fd, os.fsencode(source), dest_fd, os.fsencode(destination), 0x1)
    else:
        raise OSError(errno.ENOTSUP, "Exclusive rename unavailable")
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _parts(path):
    if not os.path.isabs(path) or "\0" in path or ".." in path.split(os.sep):
        raise ValueError("Invalid file path")
    return [part for part in path.split(os.sep) if part and part != "."]


def _parent(path):
    parts = _parts(path)
    if not parts:
        raise ValueError("Cannot use filesystem root")
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def _reject_link(fd, name):
    try:
        mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise ValueError("Target is not a regular file")


def _same_inode(left, right):
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _capture_bound(parent_fd, directory_name, directory_fd):
    try:
        return _same_inode(os.stat(directory_name, dir_fd=parent_fd, follow_symlinks=False),
                           os.fstat(directory_fd))
    except FileNotFoundError:
        return False


def _capture_directory(parent_fd):
    name = ".gamja-move-capture"
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or not _capture_bound(parent_fd, name, fd)):
            raise ValueError("Capture namespace is not private and owned")
    except BaseException:
        os.close(fd)
        raise
    return name, fd


def _recovery_location(args, parent_fd, directory_name, directory_fd, payload_name, source_info):
    try:
        captured = os.stat(payload_name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        captured = None
    if captured is None or not _same_inode(captured, source_info):
        return f"capture {directory_name} changed; original file location is unknown"
    if not _capture_bound(parent_fd, directory_name, directory_fd):
        return f"capture {directory_name} was moved or replaced; current recovery path is unknown"
    try:
        parent_path = os.path.dirname(args["source"])
        if not _same_inode(os.stat(parent_path, follow_symlinks=False), os.fstat(parent_fd)):
            return f"source parent moved; capture {directory_name} current recovery path is unknown"
    except OSError:
        return f"source parent moved; capture {directory_name} current recovery path is unknown"
    return os.path.join(parent_path, directory_name, payload_name)


class FileChanged(ValueError):
    pass


class FileUncertain(Exception):
    pass


def _private_bound(parent_fd, directory_name, directory_fd):
    info = os.fstat(directory_fd)
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and not stat.S_IMODE(info.st_mode) & 0o077
            and _capture_bound(parent_fd, directory_name, directory_fd))


def _version(info):
    return _file_version(info)


def _same_after_rename(left, right):
    # Our own rename changes ctime. Other version fields still have to match.
    return left.model_dump(exclude={"ctime_ns"}) == right.model_dump(exclude={"ctime_ns"})


class BoundTarget:
    def __init__(self, target):
        self.target, self.fd, self.leaf_fd = target, None, None
        parts = _parts(target.path)
        if (target.status not in ("verified", "absent") or not parts
                or len(target.parents) != len(parts)):
            raise FileChanged("Target parent is unavailable")
        self.name = parts[-1]
        try:
            self.fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            if _parent_identity(os.fstat(self.fd), os.sep) != target.parents[0]:
                raise FileChanged("Root identity changed")
            for component, expected in zip(parts[:-1], target.parents[1:]):
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
                os.close(self.fd)
                self.fd = child
                if _parent_identity(os.fstat(child), component) != expected:
                    raise FileChanged("Parent identity changed")
            self.check_leaf(target.leaf)
            if target.leaf is not None:
                self.leaf_fd = os.open(self.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                       dir_fd=self.fd)
                if _version(os.fstat(self.leaf_fd)) != target.leaf:
                    raise FileChanged("Opened file version changed")
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        for key in ("leaf_fd", "fd"):
            fd = getattr(self, key)
            if fd is not None:
                os.close(fd)
                setattr(self, key, None)

    def check_leaf(self, expected):
        try:
            info = os.stat(self.name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if expected is None:
            if info is not None:
                raise FileChanged("An absent file was replaced")
        elif info is None or not stat.S_ISREG(info.st_mode) or _version(info) != expected:
            raise FileChanged("File version changed")

    def check_parents(self):
        # Reopen from the actual root: a retained directory FD alone does not
        # prove the approved pathname still names this parent.
        target = self.target
        parts = _parts(target.path)
        fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if _parent_identity(os.fstat(fd), os.sep) != target.parents[0]:
                raise FileChanged("Root changed")
            for component, expected in zip(parts[:-1], target.parents[1:]):
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                if _parent_identity(os.fstat(fd), component) != expected:
                    raise FileChanged("Parent path changed")
            if not _same_inode(os.fstat(fd), os.fstat(self.fd)):
                raise FileChanged("Parent was replaced")
        finally:
            os.close(fd)

    def check(self, expected):
        self.check_parents()
        self.check_leaf(expected)

    def post(self, expected):
        self.check(expected)
        return StoredTarget(role=self.target.role, path=self.target.path,
                            status="verified" if expected is not None else "absent",
                            parents=self.target.parents, leaf=expected)


@dataclass
class Capture:
    bound: BoundTarget
    directory_name: str
    fd: int
    name: str
    version: object
    present: bool = False

    def check(self):
        if not _private_bound(self.bound.fd, self.directory_name, self.fd):
            raise FileChanged("Private capture namespace changed")
        current = os.stat(self.name, dir_fd=self.fd, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or _version(current) != self.version:
            raise FileChanged("Captured file version changed")

    def restore(self):
        self.check()
        _rename_exclusive(self.fd, self.name, self.bound.fd, self.bound.name)
        self.present = False

    def discard(self):
        self.check()
        os.unlink(self.name, dir_fd=self.fd)
        self.present = False

    def location(self):
        return _recovery_location({"source": self.bound.target.path}, self.bound.fd,
                                  self.directory_name, self.fd, self.name,
                                  SimpleStat(self.version.dev, self.version.ino))


@dataclass
class SimpleStat:
    st_dev: int
    st_ino: int


def _capture(bound, stack, captures):
    bound.check(bound.target.leaf)
    if bound.target.leaf is None:
        return None
    directory, fd = _capture_directory(bound.fd)
    stack.callback(os.close, fd)
    captured = Capture(bound, directory, fd, f"payload-{uuid.uuid4().hex}", bound.target.leaf)
    # Track before validating: even an unexpected captured replacement must
    # be retained/restored, never silently unlinked as a failed operation.
    _rename_exclusive(bound.fd, bound.name, fd, captured.name)
    captured.present = True
    captures.append(captured)
    info = os.stat(captured.name, dir_fd=fd, follow_symlinks=False)
    actual = _version(info)
    captured.version = actual
    if (not stat.S_ISREG(info.st_mode) or not _same_after_rename(actual, bound.target.leaf)
            or _version(os.fstat(bound.leaf_fd)) != actual):
        raise FileChanged("Source changed during capture")
    captured.check()
    return captured


def _temporary(bound, stack):
    directory, fd = _capture_directory(bound.fd)
    stack.callback(os.close, fd)
    name = f"temp-{uuid.uuid4().hex}"
    leaf = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    stack.callback(os.close, leaf)
    return directory, fd, name, leaf


def _owned_temp(directory, parent, fd, name, leaf, expected=None):
    if not _private_bound(parent, directory, fd):
        raise FileChanged("Temporary namespace moved")
    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or _version(info) != _version(os.fstat(leaf)):
        raise FileChanged("Owned temporary file changed")
    if expected is not None and _version(info) != expected:
        raise FileChanged("Prepared temporary version changed")


def _effect(family, args, cancel=None, *, targets):
    """Conditional namespace operations; not arbitrary open-FD writer CAS."""
    if family not in ("read_file", "write_file", "delete_file", "move_file"):
        raise FileChanged("Unsupported file operation")
    _check_cancel(cancel)
    captures, temporary, committed = [], None, False
    with ExitStack() as stack:
        bound = [stack.enter_context(BoundTarget(t)) for t in targets]
        try:
            first = bound[0]
            if family == "read_file":
                if first.leaf_fd is None:
                    raise FileChanged("Read target is absent")
                first.check(first.target.leaf)
                data = bytearray()
                while len(data) <= 1_000_000:
                    _check_cancel(cancel)
                    chunk = os.read(first.leaf_fd, min(65536, 1_000_001 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                if len(data) > 1_000_000:
                    raise FileChanged("Read exceeds one megabyte")
                if _version(os.fstat(first.leaf_fd)) != first.target.leaf:
                    raise FileChanged("File changed while reading")
                return data.decode("utf-8", "replace"), tuple(t.post(t.target.leaf) for t in bound)
            if family == "move_file" and args["source"] == args["destination"]:
                if first.leaf_fd is None:
                    raise FileChanged("Move source is absent")
                return "Source and destination are the same file.", tuple(t.post(t.target.leaf) for t in bound)
            if family in ("delete_file", "move_file") and first.leaf_fd is None:
                raise FileChanged("Source file is absent")
            destination = bound[1] if family == "move_file" else first
            if family == "move_file" and os.fstat(first.fd).st_dev != os.fstat(destination.fd).st_dev:
                raise FileChanged("Cross-volume move is unsupported")
            source_capture = _capture(first, stack, captures)
            if family == "move_file":
                _capture(destination, stack, captures)
            _check_cancel(cancel)
            for item in bound:
                item.check(None)
            if family == "delete_file":
                source_capture.discard()
                committed = True
                return "Deleted file.", (first.post(None),)
            temporary = _temporary(destination, stack)
            directory, fd, name, leaf = temporary
            if family == "write_file":
                data = args["content"].encode("utf-8")
                offset = 0
                while offset < len(data):
                    _check_cancel(cancel)
                    offset += os.write(leaf, data[offset:])
            else:
                while True:
                    _check_cancel(cancel)
                    data = os.read(first.leaf_fd, 65536)
                    if not data:
                        break
                    offset = 0
                    while offset < len(data):
                        _check_cancel(cancel)
                        offset += os.write(leaf, data[offset:])
                if _version(os.fstat(first.leaf_fd)) != source_capture.version:
                    raise FileChanged("Move source changed during read")
                os.fchmod(leaf, stat.S_IMODE(first.target.leaf.mode))
            os.fsync(leaf)
            before_commit = _version(os.fstat(leaf))
            _check_cancel(cancel)
            for captured in captures:
                captured.check()
            for item in bound:
                item.check(None)
            _owned_temp(directory, destination.fd, fd, name, leaf, before_commit)
            # A non-EEXIST error can leave the commit unconfirmed. Keep the
            # approved originals for recovery instead of claiming no effect.
            committed = True
            try:
                _rename_exclusive(fd, name, destination.fd, destination.name)
            except OSError as error:
                if error.errno == errno.EEXIST:
                    committed = False
                raise
            temporary = None
            actual = _version(os.fstat(leaf))
            if not _same_after_rename(actual, before_commit):
                raise FileChanged("Committed file changed")
            destination.check(actual)
            for captured in captures:
                captured.discard()
            _check_cancel(cancel)
            after = ((first.post(None), destination.post(actual)) if family == "move_file"
                     else (destination.post(actual),))
            if _version(os.fstat(leaf)) != actual:
                raise FileChanged("File changed during postcondition observation")
            return "Moved file." if family == "move_file" else "Wrote file.", after
        except BaseException as error:
            unresolved = []
            for captured in reversed(captures):
                if not captured.present:
                    continue
                if not committed:
                    try:
                        captured.restore()
                    except (OSError, ValueError):
                        unresolved.append(captured.location())
                else:
                    unresolved.append(captured.location())
            if temporary is not None:
                directory, fd, name, leaf = temporary
                try:
                    _owned_temp(directory, destination.fd, fd, name, leaf)
                    os.unlink(name, dir_fd=fd)
                except FileNotFoundError:
                    pass
                except (OSError, ValueError):
                    unresolved.append("owned temporary namespace changed; recovery location is unconfirmed")
            if committed or unresolved:
                details = "; ".join(unresolved)
                raise FileUncertain("File effect is uncertain." + (" Recovery: " + details if details else "")) from error
            raise


def _reviewed_targets(action, grant):
    review = getattr(grant, "review", None)
    if (action.family not in ("read_file", "write_file", "delete_file", "move_file")
            or not canonical_review_id(getattr(action, "review_id", None))
            or not isinstance(review, ReviewSnapshot) or getattr(grant, "review_id", None) != getattr(action, "review_id", None)
            or review.action != ActionBinding(**{n: getattr(action, n) for n in ActionBinding.model_fields})
            or (grant.room_id, grant.agent_id, grant.session_id) != (action.room_id, action.agent_id, action.session_id)
            or review.authority.os_user_status != "verified" or review.authority.euid != os.geteuid()):
        raise FileChanged("Trusted file review is missing")
    from app.services.tool_broker import prepare
    prepared = prepare(action.family, action.arguments, action.workflow_id)
    if any(getattr(prepared, k) != getattr(review.action, k) for k in
           ("family", "target", "scope_target", "expected_effect", "payload_digest")):
        raise FileChanged("File payload changed")
    if not all(isinstance(t, StoredTarget) for t in review.targets):
        raise FileChanged("File target type changed")
    return review.targets


async def execute_file(action, grant, cancel):
    if cancel.is_set():
        return ActionResult("cancelled", "Action cancelled before file effect.")
    try:
        if os.name == "nt":
            raise FileChanged("Conditional file identity is unsupported")
        targets = _reviewed_targets(action, grant)
    except (OSError, ValueError, AttributeError, TypeError):
        return ActionResult("failed", "File review changed or is unavailable.", error_code="review_changed")
    def run():
        finished_effect = False
        try:
            text, after = _effect(action.family, action.arguments, cancel, targets=targets)
            finished_effect = True
            if cancel.is_set():
                return ActionResult("uncertain", "File effect finished during cancellation.")
            post = FilePostconditions(action_id=action.action_id, review_id=action.review_id,
                family=action.family, before_targets=targets, after_targets=after)
            return ActionResult("completed", text, file_postconditions=post)
        except FileCancelled:
            return ActionResult("cancelled", "File effect stopped before commit.")
        except FileUncertain as error:
            return ActionResult("uncertain", str(error))
        except (OSError, ValueError):
            state = "uncertain" if finished_effect and action.family != "read_file" else "failed"
            return ActionResult(state, "File operation failed or target changed.", error_code="review_changed")
    worker = asyncio.create_task(asyncio.to_thread(run))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancel.set()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancel.set()
        raise
