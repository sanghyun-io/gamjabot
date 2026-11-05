"""Mac release operators. No engine, browser, helper, GUI or credential access.

CLI pins are deliberately fixed. Private function parameters permit tiny unsigned
unit fixtures, not a shipped auth/test mode. Arbitrary same-UID concurrent writers
remain outside the cooperative exclusive-lock contract.
"""
import argparse
from contextlib import contextmanager
import gzip
import hashlib
import json
import os
import pwd
from pathlib import Path, PurePosixPath
import shutil
import shlex
import signal
import stat
import subprocess
import tarfile

from build_core import MANIFEST, sha256, verify_package

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "dist/core/aarch64-apple-darwin/gamja-core"
NEW_HASH = "5c824c2f483dd0b9e3d62e88d6ffe9820ca9b086e08a71408716b7c782e54e60"
OLD_HASH = "d849a9d6826c3f92bbd11f17ff429fa5a3c4d04724ba0b76d4fbf2fb15470aa7"
OLD_NAME = OLD_HASH[:16]
TARGET = "aarch64-apple-darwin"
FLOOR = 2 * 1024**3
ARCHIVE_CAP = 512 * 1024**2
COPY_RESERVE = 716800000
HOME_PATH = Path(pwd.getpwuid(os.geteuid()).pw_dir).resolve(strict=True)
CORE_ROOT = HOME_PATH / ".local/share/gamja-bot/core"
LINK = HOME_PATH / ".local/bin/gamja"
ARCHIVE = ROOT / "docs/.logs/gamja-core_1.0.0_aarch64-apple-darwin_preview.2026-10-01.tar.gz"


def require(condition, code):
    if not condition:
        raise ValueError(code)


def launcher_bytes(target, home=HOME_PATH):
    target, home = Path(target), Path(home)
    try:
        physical_home = home.resolve(strict=True)
        physical_target = target.resolve(strict=True)
        contained = physical_target.is_relative_to(physical_home)
    except (OSError, RuntimeError):
        contained = False
    require(target.is_absolute() and home.is_absolute() and contained
            and physical_target != physical_home,
            "cli_target_invalid")
    return ("#!/bin/sh\nexec " + shlex.quote(str(physical_target))
            + ' "$@"\n').encode("utf-8")


def free_bytes(path):
    return shutil.disk_usage(path).free


def disk_check(path, reserve=0):
    require(free_bytes(path) >= FLOOR + reserve, "disk_floor")


def identity(path):
    s = path.lstat()
    return s.st_dev, s.st_ino, s.st_uid, stat.S_IFMT(s.st_mode)


def owned_chain(path, anchor, *, leaf=False):
    path, anchor = Path(path).absolute(), Path(anchor).absolute()
    require(path.is_relative_to(anchor), "outside_owned_anchor")
    for parent in anchor.parents:
        s = parent.lstat()
        require(stat.S_ISDIR(s.st_mode) and s.st_uid in (0, os.geteuid()), "unsafe_system_ancestor")
    current = path if leaf else path.parent
    while True:
        s = current.lstat()
        require(stat.S_ISDIR(s.st_mode) and s.st_uid == os.geteuid(), "unsafe_owned_ancestor")
        if current == anchor:
            break
        current = current.parent
    return path


def verified(root, expected):
    require(sha256(root / MANIFEST) == expected, "manifest_pin")
    manifest = verify_package(root, TARGET)
    require(root.stat().st_uid == os.geteuid(), "package_owner")
    require(all(path.lstat().st_uid == os.geteuid() for path in root.rglob("*")), "package_asset_owner")
    return manifest


def verify_native(root):
    for name in ("bin/gamja", "libexec/gamja/gamja-engine", "libexec/gamja/gamja-platform-helper"):
        result = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(root / name)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        require(result.returncode == 0, "signature_failed")


def no_refs(root):
    result = subprocess.run(["/usr/sbin/lsof", "-nP", "-Fpn", "+D", str(root)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    require(result.returncode == 1 and not result.stdout and not result.stderr, "prefix_in_use_or_unproven")


@contextmanager
def cleanup_shield():
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextmanager
def interruptible():
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    def cancel(*_):
        raise InterruptedError("operator_interrupted")
    try:
        for sig in previous:
            signal.signal(sig, cancel)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def reserve_receipt(path, anchor):
    path = owned_chain(path, anchor)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return fd


def finish_receipt(fd, value):
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    require(len(data) <= 16384, "receipt_limit")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    while data:
        data = data[os.write(fd, data):]
    os.fsync(fd)


def expected_members(source, manifest):
    result = {"gamja-core": {"directory": True}}
    for name, info in {**manifest["files"], MANIFEST: {
            "sha256": sha256(source / MANIFEST), "bytes": (source / MANIFEST).stat().st_size,
            "executable": False}}.items():
        relative = PurePosixPath(name)
        require(not relative.is_absolute() and ".." not in relative.parts and str(relative) == name, "unsafe_member")
        result["gamja-core/" + name] = info
        for parent in relative.parents:
            if str(parent) != ".":
                result["gamja-core/" + str(parent)] = {"directory": True}
    return result


def verify_archive(archive, source, expected_hash):
    manifest = verified(source, expected_hash)
    expected = expected_members(source, manifest)
    seen = set()
    with tarfile.open(archive, "r|gz") as stream:
        for member in stream:
            require(member.name in expected and member.name not in seen, "archive_member")
            seen.add(member.name)
            item = expected[member.name]
            require(not member.mode & 0o7000, "archive_mode")
            if "directory" in item:
                require(member.isdir() and member.size == 0, "archive_directory")
            elif "symlink" in item:
                require(member.issym() and member.linkname == item["symlink"] and member.size == 0, "archive_symlink")
            else:
                require(member.isfile() and member.size == item["bytes"]
                        and bool(member.mode & 0o111) == item["executable"], "archive_file")
                digest = hashlib.sha256()
                content = stream.extractfile(member)
                remaining = member.size
                while remaining:
                    block = content.read(min(1048576, remaining))
                    require(bool(block), "archive_truncated")
                    digest.update(block)
                    remaining -= len(block)
                require(digest.hexdigest() == item["sha256"], "archive_hash")
    require(seen == set(expected), "archive_missing")
    return len(seen)


class BoundedWriter:
    def __init__(self, file, path, cap):
        self.file, self.path, self.cap, self.count = file, path, cap, 0

    def write(self, data):
        require(self.count + len(data) <= self.cap, "archive_limit")
        disk_check(self.path.parent)
        self.count += len(data)
        return self.file.write(data)

    def flush(self):
        self.file.flush()


@interruptible()
def archive_prefix(source, output, receipt, expected_hash, *, anchor=HOME_PATH, cap=ARCHIVE_CAP):
    source = owned_chain(source, anchor, leaf=True)
    output = owned_chain(output, anchor)
    require(not output.is_relative_to(source) and not Path(receipt).absolute().is_relative_to(source), "archive_inside_source")
    fd = rawfd = raw = None
    output_id = None
    committed = False
    result = {"status": "failed", "operation": "archive", "manifest_sha256": expected_hash}
    try:
        with cleanup_shield():
            fd = reserve_receipt(receipt, anchor)
        with interruptible():
            manifest = verified(source, expected_hash)
            verify_native(source)
            no_refs(source)
            disk_check(output.parent, cap)
            with cleanup_shield():
                rawfd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                output_id = identity(output)
                raw = os.fdopen(rawfd, "wb")
                rawfd = None  # The tracked file object now owns this descriptor.
            with raw:
                bounded = BoundedWriter(raw, output, cap)
                with gzip.GzipFile(filename="", fileobj=bounded, mode="wb", mtime=0) as compressed:
                    with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as stream:
                        for name, item in sorted(expected_members(source, manifest).items()):
                            info = tarfile.TarInfo(name)
                            info.mode = 0o755 if item.get("directory") or item.get("executable") else 0o644
                            if "directory" in item:
                                info.type = tarfile.DIRTYPE
                                stream.addfile(info)
                            elif "symlink" in item:
                                info.type, info.linkname = tarfile.SYMTYPE, item["symlink"]
                                stream.addfile(info)
                            else:
                                info.size = item["bytes"]
                                file = source / name.removeprefix("gamja-core/")
                                with cleanup_shield():
                                    contentfd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW)
                                    try:
                                        content = os.fdopen(contentfd, "rb")
                                    except BaseException:
                                        os.close(contentfd)
                                        raise
                                with content:
                                    require(stat.S_ISREG(os.fstat(content.fileno()).st_mode), "source_file")
                                    stream.addfile(info, content)
                raw.flush()
                os.fsync(raw.fileno())
            verified(source, expected_hash)
            members = verify_archive(output, source, expected_hash)
            require(identity(output) == output_id, "archive_changed")
            result.update(status="passed", bytes=output.stat().st_size, sha256=sha256(output),
                          md5=file_digest(output, "md5"), verified_members=members)
        with cleanup_shield():
            finish_receipt(fd, result)  # Durable commit: later cancellation cannot change status.
            committed = True
    except BaseException as error:
        if committed and isinstance(error, InterruptedError):
            return result
        with cleanup_shield():
            if output_id is not None:
                require(identity(output) == output_id, "archive_cleanup_identity")
                output.unlink()
            result.update(status="failed", failure=type(error).__name__)
            if fd is not None:
                finish_receipt(fd, result)
        raise
    finally:
        with cleanup_shield():
            if raw is not None:
                raw.close()
            if rawfd is not None:
                os.close(rawfd)
            if fd is not None:
                os.close(fd)
    return result


def file_digest(path, algorithm):
    h = hashlib.new(algorithm)
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def copy_file(source, destination):
    source, destination = Path(source), Path(destination)
    disk_check(destination.parent)
    src = dst = srcfd = dstfd = None
    try:
        with cleanup_shield():
            srcfd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            src = os.fdopen(srcfd, "rb")
            srcfd = None
        mode = os.fstat(src.fileno()).st_mode
        require(stat.S_ISREG(mode), "copy_source")
        with cleanup_shield():
            dstfd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode & 0o777)
            dst = os.fdopen(dstfd, "wb")
            dstfd = None
        while block := src.read(1048576):
            disk_check(destination.parent, len(block))
            dst.write(block)
    finally:
        with cleanup_shield():
            for stream in (dst, src):
                if stream is not None:
                    stream.close()
            for filefd in (dstfd, srcfd):
                if filefd is not None:
                    os.close(filefd)
    os.chmod(destination, mode & 0o777)
    return str(destination)


def command_state(path):
    path = Path(path)
    state = identity(path)
    require(state[2] == os.geteuid(), "unsafe_command")
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        return state, "symlink", os.readlink(path)
    require(stat.S_ISREG(mode) and path.stat().st_nlink == 1
            and stat.S_IMODE(mode) == 0o755 and path.stat().st_size <= 4096,
            "unsafe_command")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        require((opened.st_dev, opened.st_ino, opened.st_uid,
                 stat.S_IFMT(opened.st_mode)) == state,
                "command_changed")
        content = os.read(descriptor, 4097)
        require(len(content) <= 4096 and os.read(descriptor, 1) == b"",
                "unsafe_command")
    finally:
        os.close(descriptor)
    require(identity(path) == state, "command_changed")
    return state, "launcher", content


def assert_command(path, state):
    require(command_state(path) == state, "command_changed")


def assert_link(path, state):
    """Compatibility hook for transition fault-injection tests."""
    assert_command(path, state)


def command_points_to(path, target, home):
    state = command_state(path)
    if state[1] == "symlink":
        return state[2] == str(target)
    return state[2] == launcher_bytes(target, home)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_launcher(path, target, home):
    content = launcher_bytes(target, home)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755)
    try:
        os.fchmod(descriptor, 0o755)
        written = 0
        while written < len(content):
            written += os.write(descriptor, content[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    sync_directory(Path(path).parent)
    return command_state(path)


def restore_command(path, state):
    if state[1] == "symlink":
        os.symlink(state[2], path)
    else:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755)
        try:
            os.fchmod(descriptor, 0o755)
            written = 0
            while written < len(state[2]):
                written += os.write(descriptor, state[2][written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    sync_directory(Path(path).parent)
    return command_state(path)


def remove_created_prefix(path, state, source_manifest):
    require(identity(path) == state, "prefix_cleanup_identity")
    allowed = {MANIFEST, *source_manifest["files"]}
    for name in list(allowed):
        allowed.update(str(parent) for parent in PurePosixPath(name).parents if str(parent) != ".")
    for child in path.rglob("*"):
        name = child.relative_to(path).as_posix()
        require(name in allowed, "prefix_cleanup_extra")
        require(child.lstat().st_uid == os.geteuid(), "prefix_cleanup_owner")
        if child.is_symlink():
            require(source_manifest["files"].get(name, {}).get("symlink") == os.readlink(child), "prefix_cleanup_symlink")
    no_refs(path)
    shutil.rmtree(path)


@interruptible()
def cli_transition(*, source, core_root, link, anchor, new_hash, old_hash, old_name, receipt, rollback=False):
    source = owned_chain(source, anchor, leaf=True)
    core_root = owned_chain(core_root, anchor, leaf=True)
    link = owned_chain(link, anchor)
    releases = owned_chain(core_root / "releases", anchor, leaf=True)
    old, new = releases / old_name, releases / new_hash[:16]
    manifest = verified(source, new_hash)
    previous_target, target = (new, old) if rollback else (old, new)
    old_state = command_state(link)
    require(command_points_to(link, previous_target / "bin/gamja", anchor),
            "unexpected_command_target")
    fd = None
    lock = core_root / (".cli-update-" + new_hash[:16] + ".lock")
    pending = link.parent / (".gamja-" + (old_name if rollback else new_hash[:16]) + ".pending")
    lock_fd = None
    committed = False
    lock_id = pending_id = prefix_id = active_state = None
    result = {"status": "failed", "operation": "rollback" if rollback else "update",
              "manifest_sha256": old_hash if rollback else new_hash, "old_release_retained": True}
    try:
        with cleanup_shield():
            fd = reserve_receipt(receipt, anchor)
        with interruptible():
            with cleanup_shield():
                lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                lock_id = identity(lock)
            verified(old, old_hash)
            for path in (source, old):
                no_refs(path)
            verify_native(old if rollback else source)
            if rollback:
                verified(new, new_hash)
                no_refs(new)
            else:
                disk_check(releases, COPY_RESERVE)
                with cleanup_shield():
                    new.mkdir(mode=0o700)  # Exclusive; no overwrite/merge of an existing release.
                    prefix_id = identity(new)
                # Copy only manifest-covered files and their required parents.
                # copytree would merge unchecked empty directories and aggregate
                # InterruptedError into shutil.Error while continuing the copy.
                for name, item in sorted(expected_members(source, manifest).items()):
                    relative = name.removeprefix("gamja-core/")
                    if name == "gamja-core":
                        continue
                    destination = new / relative
                    if "directory" in item:
                        destination.mkdir(mode=0o755)
                    elif "symlink" in item:
                        os.symlink(item["symlink"], destination)
                    else:
                        copy_file(source / relative, destination)
                verified(source, new_hash)
                verified(new, new_hash)
                verify_native(new)
            assert_link(link, old_state)
            with cleanup_shield():
                pending_id = write_launcher(pending, target / "bin/gamja", anchor)[0]
            assert_link(link, old_state)
            with cleanup_shield():
                os.replace(pending, link)
                pending_id = None
                sync_directory(link.parent)
                active_state = command_state(link)
                require(command_points_to(link, target / "bin/gamja", anchor),
                        "command_activation_failed")
            disk_check(releases)
        with cleanup_shield():
            assert_link(link, active_state)
            result.update(status="passed", active_release=target.name, retained_rollback=old_name,
                          free_bytes=free_bytes(releases), same_uid_race_limit="cooperative exclusive lock")
            finish_receipt(fd, result)  # Explicit durable success commit.
            committed = True
    except BaseException as error:
        if committed and isinstance(error, InterruptedError):
            return result
        with cleanup_shield():
            if active_state is not None:
                assert_link(link, active_state)
                pending_id = restore_command(pending, old_state)[0]
                assert_link(link, active_state)
                os.replace(pending, link)
                pending_id = None
                sync_directory(link.parent)
                require(command_points_to(link, previous_target / "bin/gamja", anchor),
                        "command_restore_failed")
            if prefix_id is not None:
                require(not command_points_to(link, new / "bin/gamja", anchor),
                        "active_prefix_cleanup_forbidden")
                remove_created_prefix(new, prefix_id, manifest)
            result.update(status="failed", failure=type(error).__name__, previous_link_restored=active_state is not None)
            if fd is not None:
                finish_receipt(fd, result)
        raise
    finally:
        with cleanup_shield():
            if pending_id is not None:
                require(identity(pending) == pending_id, "pending_cleanup_identity")
                pending.unlink()
            if lock_fd is not None:
                require(identity(lock) == lock_id, "lock_cleanup_identity")
                lock.unlink()
                os.close(lock_fd)
            if fd is not None:
                os.close(fd)
    return result


def update_cli(**kwargs):
    return cli_transition(**kwargs)


def rollback_cli(**kwargs):
    return cli_transition(**kwargs, rollback=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("archive", "update", "rollback"))
    parser.add_argument("--receipt", type=Path, required=True, help="exclusive new0600 receipt path")
    args = parser.parse_args()
    try:
        if args.operation == "archive":
            result = archive_prefix(SOURCE, ARCHIVE, args.receipt, NEW_HASH)
        else:
            result = (rollback_cli if args.operation == "rollback" else update_cli)(
                source=SOURCE, core_root=CORE_ROOT, link=LINK, anchor=HOME_PATH,
                new_hash=NEW_HASH, old_hash=OLD_HASH, old_name=OLD_NAME, receipt=args.receipt)
        print(json.dumps({"status": result["status"], "operation": args.operation}))
    except BaseException as error:
        print(json.dumps({"status": "failed", "code": type(error).__name__}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
