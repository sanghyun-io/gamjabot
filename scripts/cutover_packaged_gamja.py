#!/usr/bin/env python3
"""Owner-preserving Gamja-only cutover from a pinned signed DMG.

The preflight records artifact and live identities. Apply mounts and verifies
those exact inputs again, publishes only the canonical app/runtime, and invokes
the independent Core CLI installer. Recovery retains the old runtime inodes and
receipt. Existing automatic login is checked through the normal no-UI CLI
session before and after cutover. A regression restores the pinned old runtime
and checks its login again. No GUI launch, secret export or system changes.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
from uuid import uuid4

APP = Path("/Applications/Gamja Bot.app")
BUNDLE_ID = "top.gamja.bot"
TARGET_SCHEMA = 25
REPO = Path(__file__).resolve().parents[1]
CANONICAL_BUILD_CORE = REPO / "scripts/build_core.py"
CANONICAL_BUILD_CORE_SHA256 = "fbe90956277026addba4ce668775b343aa21a7a338f703308cc0723e7ac10508"
MAC_CORE_CERTIFICATE = REPO / "core-host/macos-credential-core.cer"
MAC_CORE_CERTIFICATE_SHA256 = "393c4ec28063dcda78bd50bb02cb0bf0cad8b64e6bbef532767aa145fd022f0d"
IMMUTABLE_BROKER_SHA256 = "2aa9a05ee834302b8eee08589642844611d1ad013248902f1ebd1813c1419553"
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_CLI_OUTPUT = 32768
MAX_LOGIN_SNAPSHOT_BYTES = 32 * 1024 * 1024


class CutoverError(RuntimeError):
    pass


def require(value, code):
    if not value:
        raise CutoverError(code)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_sha256(root):
    """Bind regular files and symlink targets without following symlink trees."""
    digest = hashlib.sha256()
    entries = []
    for directory, names, files in os.walk(root, followlinks=False):
        for name in names + files:
            entries.append(Path(directory) / name)
    for path in sorted(entries, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big")); digest.update(relative)
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode):
            target = os.readlink(path).encode()
            digest.update(b"L"); digest.update(len(target).to_bytes(4, "big")); digest.update(target)
        elif stat.S_ISREG(status.st_mode):
            digest.update(b"F"); digest.update(bytes.fromhex(sha256(path)))
        elif stat.S_ISDIR(status.st_mode):
            digest.update(b"D")
        else:
            raise CutoverError("unsafe_path")
    return digest.hexdigest()


def private_real(path, *, directory=False):
    path = Path(path).absolute()
    info = path.lstat()
    require(not path.is_symlink() and info.st_uid == os.geteuid(), "unsafe_path")
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), "unsafe_path")
    require(not info.st_mode & 0o022 and path.resolve() == path, "unsafe_path")
    return path


def _regular_snapshot(path):
    path = private_real(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        path_before = path.lstat()
        identity = lambda value: (
            value.st_dev, value.st_ino, value.st_uid, value.st_nlink,
            stat.S_IFMT(value.st_mode), stat.S_IMODE(value.st_mode), value.st_size,
        )
        require(identity(before) == identity(path_before)
                and stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
                "unsafe_path")
        digest = hashlib.sha256()
        for block in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
            digest.update(block)
        after = os.fstat(descriptor)
        path_after = path.lstat()
        require(identity(before) == identity(after) == identity(path_after),
                "unsafe_path")
        return {
            "sha256": digest.hexdigest(),
            "bytes": before.st_size,
            "device": before.st_dev,
            "inode": before.st_ino,
            "uid": before.st_uid,
            "mode": stat.S_IMODE(before.st_mode),
        }
    finally:
        os.close(descriptor)


def canonical_paths(home=None):
    home = Path(home or Path.home()).resolve()
    app_data = home / "Library/Application Support/top.gamja.bot"
    return {"app": APP, "core": home / ".local/share/gamja-bot/core/runtime",
            "app_data": app_data, "database": app_data / "gamja_bot.db"}


def app_identity(app):
    app = private_real(app, directory=True)
    info = private_real(app / "Contents/Info.plist")
    metadata = plistlib.loads(info.read_bytes())
    require(metadata.get("CFBundleIdentifier") == BUNDLE_ID, "app_identity_invalid")
    executable = metadata.get("CFBundleExecutable")
    require(isinstance(executable, str) and "/" not in executable, "app_identity_invalid")
    main = private_real(app / "Contents/MacOS" / executable)
    verify_app_signature(app)
    return {"path": str(app), "bundle_id": BUNDLE_ID, "executable": executable,
            "main_sha256": sha256(main), "tree_sha256": tree_sha256(app)}


def core_identity(core, *, installed=False, runtime_verifier=None):
    core = private_real(core, directory=True)
    recipe = load_core_recipe()
    receipt = core / recipe.RUNTIME_RECEIPT
    allow_runtime_receipt = installed and (receipt.exists() or receipt.is_symlink())
    if allow_runtime_receipt:
        require(isinstance(runtime_verifier, dict), "runtime_verifier_required")
        verifier_path = Path(runtime_verifier.get("path", ""))
        verifier_sha256 = runtime_verifier.get("sha256")
        require(verifier_path.is_absolute() and isinstance(verifier_sha256, str)
                and len(verifier_sha256) == 64, "runtime_verifier_required")
        verifier_status = private_real(verifier_path).stat()
        require(verifier_status.st_nlink == 1 and verifier_status.st_mode & 0o111
                and sha256(verifier_path) == verifier_sha256,
                "runtime_verifier_invalid")
    manifest = recipe.verify_package(
        core, "aarch64-apple-darwin",
        allow_runtime_receipt=allow_runtime_receipt,
        runtime_verifier=verifier_path if allow_runtime_receipt else None,
        runtime_verifier_sha256=verifier_sha256 if allow_runtime_receipt else None,
    )
    files = manifest["files"]
    helper = files.get("libexec/gamja/gamja-platform-helper")
    identity = {"path": str(core), "manifest_sha256": sha256(core / "core-manifest.json"),
            "tree_sha256": tree_sha256(core),
            "core_sha256": sha256(core / "bin/gamja"),
            "engine_sha256": sha256(core / "libexec/gamja/gamja-engine"),
            "helper_sha256": helper.get("sha256") if isinstance(helper, dict) else None,
            "browser_payload": manifest["browser_payload"],
            "file_count": len(files),
            "payload_bytes": sum(
                item["bytes"] for item in files.values()
                if isinstance(item, dict) and "symlink" not in item
            ),
            "version": manifest["version"]}
    if allow_runtime_receipt:
        root_status = core.stat()
        identity["runtime_snapshot"] = {
            "root": {
                "device": root_status.st_dev,
                "inode": root_status.st_ino,
                "uid": root_status.st_uid,
                "mode": stat.S_IMODE(root_status.st_mode),
            },
            "receipt": _regular_snapshot(receipt),
            "core": _regular_snapshot(core / "bin/gamja"),
            "engine": _regular_snapshot(core / "libexec/gamja/gamja-engine"),
        }
    return identity


def database_identity(path):
    path = private_real(path)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
        require(database.execute("PRAGMA quick_check").fetchone() == ("ok",), "database_invalid")
        version = database.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    return {"path": str(path), "sha256": sha256(path), "bytes": path.stat().st_size,
            "mode": stat.S_IMODE(path.stat().st_mode), "schema": version}


def expected_schema_from_plan(plan):
    require(isinstance(plan, dict) and plan.get("version") == 1,
            "expected_schema_invalid")
    value = plan.get("expected_schema", TARGET_SCHEMA)
    require(type(value) is int and value > 0, "expected_schema_invalid")
    return value


def verify_database_schema(plan, path):
    identity = database_identity(path)
    require(identity["schema"] == expected_schema_from_plan(plan),
            "database_schema_mismatch")
    return identity


def parse_expected_schema(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("expected schema must be a positive integer") from None
    if str(parsed) != value or parsed <= 0:
        raise argparse.ArgumentTypeError("expected schema must be a positive integer")
    return parsed


def plan_digest(value):
    payload = {key: item for key, item in value.items() if key != "plan_sha256"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def same_identity(first, second):
    """Compare artifact facts while allowing an exact artifact to move paths."""
    return ({key: value for key, value in first.items() if key != "path"}
            == {key: value for key, value in second.items() if key != "path"})


def same_core_payload(first, second):
    keys = (
        "manifest_sha256", "core_sha256", "engine_sha256", "helper_sha256",
        "browser_payload", "file_count", "payload_bytes", "version",
    )
    return {key: first.get(key) for key in keys} == {key: second.get(key) for key in keys}


def write_exclusive(path, value):
    raw = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(raw); output.flush(); os.fsync(output.fileno())
    fsync_directory(Path(path).parent)


def sqlite_backup(source, destination):
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
    with sqlite3.connect(source) as original, sqlite3.connect(destination) as backup:
        original.backup(backup)
        require(backup.execute("PRAGMA quick_check").fetchone() == ("ok",), "database_backup_invalid")
    destination.chmod(0o600)
    fsync_file(destination); fsync_directory(destination.parent)
    return database_identity(destination)


def preserve_candidate_database_before_rollback(paths, recovery, journal, state):
    """Keep the candidate's data before restoring the older runtime's DB.

    The caller holds the backend lifetime lock after stopping the candidate.
    A prior, journaled snapshot is immutable across interrupted rollbacks;
    an unjournaled/partial snapshot blocks restoration for manual recovery.
    """
    snapshot = Path(recovery) / "database-after-candidate/gamja_bot.db"
    key = "candidate_database_before_rollback"
    if key in state:
        require(isinstance(state[key], dict)
                and database_identity(snapshot) == state[key],
                "candidate_database_backup_invalid")
        return state[key]
    source = private_real(paths["database"])
    require(not snapshot.parent.exists() and not snapshot.parent.is_symlink(),
            "candidate_database_backup_unjournaled")
    identity = sqlite_backup(source, snapshot)
    state[key] = identity
    # This durable journal commit precedes removing either candidate binary
    # or replacing the live DB. No snapshot failure may erase newer data.
    journal_write(journal, state)
    return identity


def fsync_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def journal_write(path, value):
    temporary = path.with_name("." + path.name + ".pending")
    raw = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(raw); output.flush(); os.fsync(output.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def backend_lock(app_data):
    path = Path(app_data) / "gamja_bot.db.backend.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    status = os.fstat(descriptor)
    if (not stat.S_ISREG(status.st_mode) or status.st_uid != os.geteuid()
            or status.st_mode & 0o022):
        os.close(descriptor); raise CutoverError("unsafe_path")
    handle = os.fdopen(descriptor, "a+b")
    try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close(); raise CutoverError("gamja_not_idle") from None
    return handle


def operation_lock(app_data):
    path = Path(app_data) / "gamja-cutover.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    status = os.fstat(descriptor)
    if not stat.S_ISREG(status.st_mode) or status.st_uid != os.geteuid():
        os.close(descriptor); raise CutoverError("unsafe_path")
    os.fchmod(descriptor, 0o600)
    handle = os.fdopen(descriptor, "a+b")
    try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close(); raise CutoverError("cutover_already_running") from None
    return handle


def identity_kind(path, old, candidate, identify):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return "missing"
    try:
        actual = identify(path)
    except (CutoverError, OSError, ValueError, KeyError):
        return "unknown"
    if same_identity(actual, old):
        return "old"
    if same_identity(actual, candidate):
        return "candidate"
    return "unknown"


def core_identity_kind(path, old, candidate, runtime_verifier):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return "missing"
    try:
        actual = core_identity(
            path, installed=True, runtime_verifier=runtime_verifier
        )
    except (CutoverError, OSError, ValueError, KeyError):
        return "unknown"
    if same_identity(actual, old):
        return "old"
    if same_core_payload(actual, candidate):
        return "candidate"
    return "unknown"


def quarantined_old_core_kind(path, old):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return "missing"
    try:
        root = private_real(path, directory=True)
        root_status = root.stat()
        snapshot = {
            "root": {
                "device": root_status.st_dev,
                "inode": root_status.st_ino,
                "uid": root_status.st_uid,
                "mode": stat.S_IMODE(root_status.st_mode),
            },
            "receipt": _regular_snapshot(root / ".gamja-runtime-receipt.json"),
            "core": _regular_snapshot(root / "bin/gamja"),
            "engine": _regular_snapshot(root / "libexec/gamja/gamja-engine"),
        }
        if (snapshot != old.get("runtime_snapshot")
                or sha256(root / "core-manifest.json") != old["manifest_sha256"]
                or tree_sha256(root) != old["tree_sha256"]):
            return "unknown"
        return "old"
    except (CutoverError, OSError, ValueError, KeyError, TypeError):
        return "unknown"


def restore_database(backup, destination):
    backup = private_real(backup)
    destination = Path(destination)
    temporary = destination.with_name("." + destination.name + ".rollback-" + uuid4().hex)
    require(not temporary.exists(), "rollback_database_invalid")
    try:
        shutil.copyfile(backup, temporary)
        temporary.chmod(0o600)
        fsync_file(temporary)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(destination) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                status = sidecar.lstat()
                require(sidecar.parent == destination.parent and stat.S_ISREG(status.st_mode)
                        and not sidecar.is_symlink(), "rollback_database_invalid")
                sidecar.unlink()
        os.replace(temporary, destination)
        fsync_directory(destination.parent)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def run_core(core, app_data, *arguments):
    result = subprocess.run([str(Path(core) / "bin/gamja"), "--app-data", str(app_data), *arguments],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=30, text=True)
    require(result.returncode == 0, "core_health_failed")


def remembered_login_owner(database):
    """Read only the owner binding of an active remembered login, never its value."""
    with sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True) as db:
        present = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                             "AND name='remembered_login'").fetchone()
        if present is None:
            return None
        rows = db.execute("SELECT r.owner_id,r.expires_at,u.is_active "
                          "FROM remembered_login r JOIN users u ON u.id=r.owner_id "
                          "WHERE r.id=1").fetchall()
    require(len(rows) <= 1, "remembered_login_binding_invalid")
    if not rows:
        return None
    owner, expiry, active = rows[0]
    require(type(owner) is int and owner > 0 and isinstance(expiry, str),
            "remembered_login_binding_invalid")
    try:
        expiry = datetime.fromisoformat(expiry)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
    except ValueError:
        raise CutoverError("remembered_login_binding_invalid") from None
    return owner if active == 1 and expiry > datetime.now(timezone.utc) else None


def login_continuity_probe(core, app_data, expected_owner):
    """One protected restore→me session; retain only booleans, never CLI bodies."""
    result = {"authenticated": False, "same_owner": False, "passed": False}
    with tempfile.TemporaryDirectory(prefix="gamja-login-continuity-") as temporary:
        commands = Path(temporary) / "commands.ndjson"
        descriptor = os.open(commands, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write('{"command":["restore"]}\n{"command":["me"]}\n')
        try:
            code, output, _stderr = load_core_recipe()._run_bounded_core_cli(
                ["/usr/bin/env", "HOME=" + str(Path.home()),
                 str(Path(core) / "bin/gamja"), "--app-data", str(app_data),
                 "--batch", str(commands)], timeout_seconds=30,
            )
            if code != 0 or len(output) > MAX_CLI_OUTPUT:
                return result
            rows = [json.loads(line, object_pairs_hook=_unique_object)
                    for line in output.decode("utf-8").splitlines()]
            if (len(rows) != 2 or not isinstance(rows[0], dict)
                    or set(rows[0]) != {"authenticated"}
                    or rows[0]["authenticated"] is not True):
                return result
            result["authenticated"] = True
            result["same_owner"] = (isinstance(rows[1], dict)
                                    and type(rows[1].get("id")) is int
                                    and rows[1]["id"] == expected_owner)
            result["passed"] = result["same_owner"]
        except (CutoverError, ValueError, TypeError, UnicodeError, OSError,
                subprocess.SubprocessError):
            # Native errors, tokens and account details stay inside this process.
            return result
    return result


def old_login_baseline(plan, paths):
    """Verify the quiescent plan still holds after the normal old CLI check."""
    assert_unchanged(plan)
    owner = remembered_login_owner(paths["database"])
    if owner is None:
        return None
    idle_lock = backend_lock(paths["app_data"])
    idle_lock.close()
    require(subprocess.run(["/usr/bin/pgrep", "-x", "gamja-bot"],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL).returncode != 0, "gamja_not_idle")
    try:
        run_core(paths["core"], paths["app_data"], "start", "--if-needed")
        probe = login_continuity_probe(paths["core"], paths["app_data"], owner)
        require(probe["passed"], "old_automatic_login_unverified")
    finally:
        run_core(paths["core"], paths["app_data"], "stop")
    # A reconciliation or concurrent change requires a new reviewed plan.
    assert_unchanged(plan)
    return owner


def upgrade_database(profile):
    profile = private_real(profile, directory=True)
    require(stat.S_IMODE(profile.stat().st_mode) == 0o700, "login_upgrade_profile_unsafe")
    database = private_real(profile / "gamja_bot.db")
    require(database.stat().st_nlink == 1
            and stat.S_IMODE(database.stat().st_mode) == 0o600,
            "login_upgrade_profile_unsafe")
    require(all(not Path(str(database) + suffix).exists()
                and not Path(str(database) + suffix).is_symlink()
                for suffix in ("-wal", "-shm", "-journal")), "login_upgrade_not_quiescent")
    return database


def checked_upgrade_rows(original, upgraded):
    """Compare all schema/data in memory; only three login fields may differ."""
    schema_query = "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    require(original.execute(schema_query).fetchall() == upgraded.execute(schema_query).fetchall(),
            "login_upgrade_schema_changed")
    tables = [r[0] for r in original.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    quote = lambda name: '"' + name.replace('"', '""') + '"'
    for name in tables:
        columns = [r[1] for r in original.execute("PRAGMA table_info(" + quote(name) + ")")]
        if name == "remembered_login":
            columns = [c for c in columns if c not in {"secret_ref", "secret_hash", "revision"}]
        require(columns, "login_upgrade_schema_changed")
        query = "SELECT " + ",".join(map(quote, columns)) + " FROM " + quote(name)
        require(Counter(original.execute(query).fetchall()) == Counter(upgraded.execute(query).fetchall()),
                "login_upgrade_data_changed")
    query = "SELECT id,owner_id,secret_ref,secret_hash,revision,expires_at FROM remembered_login"
    old, new = original.execute(query).fetchall(), upgraded.execute(query).fetchall()
    require(len(old) == len(new) == 1 and old[0][0] == new[0][0] == 1,
            "login_upgrade_binding_invalid")
    old, new = old[0], new[0]
    require(type(old[4]) is int and 0 < old[4] < 2147483647
            and type(new[4]) is int and new[4] == old[4] + 1
            and new[1] == old[1] and new[5] == old[5]
            and isinstance(new[2], str) and 0 < len(new[2]) <= 512 and new[2] != old[2]
            and isinstance(new[3], str) and len(new[3]) == 64
            and all(c in "0123456789abcdef" for c in new[3]) and new[3] != old[3],
            "login_upgrade_binding_invalid")
    return old, new


def compare_upgrade_databases(original_path, upgraded_path):
    upgraded, _snapshot = pinned_upgrade_connection(upgraded_path)
    try:
        with sqlite3.connect(Path(original_path).as_uri() + "?mode=ro", uri=True) as original:
            checked_upgrade_rows(original, upgraded)
    finally:
        upgraded.close()


def pinned_upgrade_connection(path):
    """Read one bounded, no-follow FD snapshot; never query a changing source path."""
    requested = Path(path).absolute()
    path = upgrade_database(requested.parent)
    require(path == requested, "login_upgrade_profile_unsafe")
    before = _regular_snapshot(path)
    require(0 < before["bytes"] <= MAX_LOGIN_SNAPSHOT_BYTES, "login_upgrade_snapshot_too_large")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        require((info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode), info.st_size)
                == (before["device"], before["inode"], before["uid"], before["mode"], before["bytes"])
                and stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                "login_upgrade_source_changed")
        raw = bytearray()
        while len(raw) <= MAX_LOGIN_SNAPSHOT_BYTES:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            raw.extend(chunk)
        require(len(raw) == before["bytes"] and hashlib.sha256(raw).hexdigest() == before["sha256"]
                and _regular_snapshot(path) == before, "login_upgrade_source_changed")
    finally:
        os.close(descriptor)
    upgraded = sqlite3.connect(":memory:")
    try:
        upgraded.deserialize(raw)
        upgraded.execute("PRAGMA query_only=ON")
        return upgraded, before
    except BaseException:
        upgraded.close()
        raise


def verify_upgrade_login(plan, candidate_core, owner):
    """Fresh normal no-UI login from the exact candidate before canonical writes."""
    upgrade = plan.get("login_upgrade")
    if upgrade is None:
        return
    require(owner is not None and upgrade.get("authenticated_same_owner") is True,
            "login_upgrade_auth_unverified")
    source = upgrade_database(upgrade["profile"])
    require(database_identity(source) == upgrade["database"], "login_upgrade_source_changed")
    paths = plan["canonical"]
    require(source != Path(paths["database"])
            and not source.is_relative_to(Path(paths["app_data"])), "login_upgrade_profile_unsafe")
    lock = backend_lock(source.parent)
    try:
        require(database_identity(upgrade_database(source.parent)) == upgrade["database"],
                "login_upgrade_source_changed")
        compare_upgrade_databases(paths["database"], source)
        require(remembered_login_owner(source) == owner, "login_upgrade_owner_changed")
    finally:
        lock.close()
    # The candidate Core itself must acquire this profile's backend lifetime
    # lock during normal authentication. Keeping our lock here would deadlock it.
    try:
        run_core(candidate_core, source.parent, "start", "--if-needed")
        require(login_continuity_probe(candidate_core, source.parent, owner)["passed"],
                "login_upgrade_auth_unverified")
    finally:
        run_core(candidate_core, source.parent, "stop")
    lock = backend_lock(source.parent)
    try:
        require(database_identity(upgrade_database(source.parent)) == upgrade["database"],
                "login_upgrade_source_changed")
        compare_upgrade_databases(paths["database"], source)
    finally:
        lock.close()


def adopt_upgrade_login(plan, expected_owner):
    """CAS only the authenticated login binding; the backend lifetime lock is held."""
    upgrade = plan.get("login_upgrade")
    if upgrade is None:
        return False
    source = upgrade_database(upgrade["profile"])
    require(database_identity(source) == upgrade["database"], "login_upgrade_source_changed")
    source_lock = backend_lock(source.parent)
    try:
        database = Path(plan["canonical"]["database"])
        require(database_identity(database) == plan["live"]["database"], "live_database_changed")
        require(remembered_login_owner(source) == expected_owner
                and remembered_login_owner(database) == expected_owner, "login_upgrade_owner_changed")
        upgraded, source_snapshot = pinned_upgrade_connection(source)
        try:
            require(source_snapshot["sha256"] == upgrade["database"]["sha256"],
                    "login_upgrade_source_changed")
            with sqlite3.connect(database) as original:
                original.execute("BEGIN IMMEDIATE")
                old, new = checked_upgrade_rows(original, upgraded)
                count = original.execute(
                    "UPDATE remembered_login SET secret_ref=?,secret_hash=?,revision=? "
                    "WHERE id=? AND owner_id=? AND secret_ref=? AND secret_hash=? AND revision=? AND expires_at=?",
                    (new[2], new[3], new[4], *old),
                ).rowcount
                require(count == 1, "login_upgrade_state_changed")
                require(original.execute("SELECT id,owner_id,secret_ref,secret_hash,revision,expires_at "
                                         "FROM remembered_login").fetchall() == [new],
                        "login_upgrade_state_changed")
                require(_regular_snapshot(upgrade_database(source.parent)) == source_snapshot
                        and database_identity(source) == upgrade["database"], "login_upgrade_source_changed")
        finally:
            upgraded.close()
        fsync_file(database)
        return True
    finally:
        source_lock.close()


def _apply(plan, mounted_app, *, operation_held=False):
    paths = {key: Path(value) for key, value in plan["canonical"].items()}
    private_real(paths["app_data"], directory=True)
    install_lock = None if operation_held else operation_lock(paths["app_data"])
    try:
        expected_owner = old_login_baseline(plan, paths)
        data_lock = backend_lock(paths["app_data"])
        recovery_base = Path(plan["recovery"])
        if not recovery_base.exists():
            recovery_base.mkdir(mode=0o700)
        private_real(recovery_base, directory=True)
        recovery = recovery_base / plan["operation_id"]
        require(not recovery.exists(), "recovery_collision")
        recovery.mkdir(mode=0o700)
        journal = recovery / "journal.json"
        state = {"version": 1, "operation_id": plan["operation_id"],
                 "plan_sha256": plan["plan_sha256"], "phase": "prepared",
                 "old": plan["live"], "new": plan["candidate"],
                 "login_continuity": {"required": expected_owner is not None,
                                      "old_login_verified": expected_owner is not None,
                                      "new_login_verified": False}}
        journal_write(journal, state)
        try:
            assert_unchanged(plan)
            backup = sqlite_backup(paths["database"], recovery / "database" / "gamja_bot.db")
            state.update(phase="backed_up", database_backup=backup); journal_write(journal, state)
            verify_upgrade_login(plan, mounted_app / "Contents/Resources/gamja-core", expected_owner)
            require(subprocess.run(["/usr/bin/pgrep", "-x", "gamja-bot"],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL).returncode != 0,
                    "gamja_not_idle")
            candidate_stage = recovery / "candidate-app.stage"
            require(candidate_stage.parent.stat().st_dev == paths["app"].parent.stat().st_dev,
                    "candidate_stage_not_atomic")
            state["phase"] = "copying_candidate_app"; journal_write(journal, state)
            shutil.copytree(mounted_app, candidate_stage,
                            symlinks=True)
            require(same_identity(app_identity(candidate_stage), plan["candidate"]["app"]),
                    "candidate_stage_mismatch")
            require(signed_tree(candidate_stage) == signing_receipt(
                plan["source"]["signing_receipt"],
                plan["source"]["signing_receipt_sha256"],
            )["full_signed_tree"], "candidate_stage_tree_mismatch")
            state["phase"] = "candidate_app_staged"; journal_write(journal, state)
            state["phase"] = "moving_old_app"; journal_write(journal, state)
            paths["app"].rename(recovery / "Gamja Bot.app")
            state["phase"] = "old_app_preserved"; journal_write(journal, state)
            state["phase"] = "moving_old_core"; journal_write(journal, state)
            paths["core"].rename(recovery / "core-runtime")
            state["phase"] = "old_preserved"; journal_write(journal, state)
            candidate_stage.rename(paths["app"])
            state["phase"] = "candidate_app_installed"; journal_write(journal, state)
            installed_core = install_candidate_core_with_cli(
                plan["candidate"]["app"], paths["core"], plan["candidate"]["core"],
                plan["runtime_verifier"],
            )
            state["phase"] = "candidate_installed"; journal_write(journal, state)
            require(same_identity(app_identity(paths["app"]), plan["candidate"]["app"]),
                    "activation_mismatch")
            require(same_core_payload(installed_core, plan["candidate"]["core"]),
                    "activation_mismatch")
            if plan.get("login_upgrade") is not None:
                state["phase"] = "adopting_remembered_login"; journal_write(journal, state)
                state["login_continuity"]["binding_adopted"] = adopt_upgrade_login(plan, expected_owner)
            state["phase"] = "activated"; journal_write(journal, state)
        except BaseException:
            state.update(failed_from=state["phase"], phase="activation_failed")
            journal_write(journal, state)
            raise
        finally:
            data_lock.close()
        # The Core acquires the backend lifetime lock. Keep only the separate
        # cutover-operation lock while starting and checking the new runtime.
        run_core(paths["core"], paths["app_data"], "start", "--if-needed")
        run_core(paths["core"], paths["app_data"], "status")
        new_database = verify_database_schema(plan, paths["database"])
        if expected_owner is not None:
            probe = login_continuity_probe(paths["core"], paths["app_data"], expected_owner)
            state["login_continuity"]["candidate"] = probe
            journal_write(journal, state)
            require(probe["passed"], "automatic_login_not_preserved")
            state["login_continuity"]["new_login_verified"] = True
        state.update(phase="healthy", new_database=new_database)
        journal_write(journal, state)
        return state
    except BaseException:
        if "state" in locals() and state.get("phase") != "activation_failed":
            state.update(failed_from=state["phase"], phase="activation_failed")
            journal_write(journal, state)
        raise
    finally:
        if "data_lock" in locals() and not data_lock.closed:
            data_lock.close()
        if install_lock is not None:
            install_lock.close()


def _rollback(plan, *, operation_held=False):
    require(isinstance(plan.get("runtime_verifier"), dict)
            and isinstance(plan.get("live", {}).get("core", {}).get(
                "runtime_snapshot"), dict),
            "historical_recovery_requires_review")
    paths = {key: Path(value) for key, value in plan["canonical"].items()}
    private_real(paths["app_data"], directory=True)
    install_lock = None if operation_held else operation_lock(paths["app_data"])
    recovery = Path(plan["recovery"]) / plan["operation_id"]
    try:
        recovery = private_real(recovery, directory=True)
        journal = private_real(recovery / "journal.json")
        state = json.loads(journal.read_text())
        require(state.get("plan_sha256") == plan["plan_sha256"], "journal_invalid")
        backup_path = private_real(recovery / "database/gamja_bot.db")
        backup = database_identity(backup_path)
        require(backup == state.get("database_backup"), "database_backup_invalid")
        app_kind = identity_kind(paths["app"], plan["live"]["app"],
                                 plan["candidate"]["app"], app_identity)
        runtime_verifier = plan["runtime_verifier"]
        core_kind = core_identity_kind(
            paths["core"], plan["live"]["core"], plan["candidate"]["core"],
            runtime_verifier,
        )
        recovery_app = recovery / "Gamja Bot.app"
        recovery_core = recovery / "core-runtime"
        recovery_app_kind = identity_kind(recovery_app, plan["live"]["app"],
                                          plan["candidate"]["app"], app_identity)
        recovery_core_kind = quarantined_old_core_kind(recovery_core, plan["live"]["core"])
        require(app_kind in {"old", "candidate", "missing"}
                and core_kind in {"old", "candidate", "missing"}, "rollback_identity_unknown")
        require(app_kind == "old" or recovery_app_kind == "old", "rollback_app_missing")
        require(core_kind == "old" or recovery_core_kind == "old", "rollback_core_missing")
        if app_kind == "old" and core_kind == "old":
            require(database_identity(paths["database"]) == plan["live"]["database"],
                    "rollback_database_changed")
            state["phase"] = "rollback_not_required"; journal_write(journal, state)
            return state
        if core_kind == "candidate":
            try: run_core(paths["core"], paths["app_data"], "stop")
            except CutoverError: pass
        data_lock = backend_lock(paths["app_data"])
        try:
            preserve_candidate_database_before_rollback(
                paths, recovery, journal, state)
            if app_kind == "candidate":
                require(recovery_app_kind == "old" and not paths["app"].is_symlink(),
                        "rollback_app_missing")
                shutil.rmtree(paths["app"])
            if core_kind == "candidate":
                require(recovery_core_kind == "old" and not paths["core"].is_symlink(),
                        "rollback_core_missing")
                shutil.rmtree(paths["core"])
            restore_database(backup_path, paths["database"])
            if app_kind != "old": recovery_app.rename(paths["app"])
            if core_kind != "old": recovery_core.rename(paths["core"])
            require(same_identity(app_identity(paths["app"]), plan["live"]["app"]),
                    "rollback_app_mismatch")
            require(same_identity(core_identity(
                paths["core"], installed=True,
                runtime_verifier=runtime_verifier), plan["live"]["core"]),
                    "rollback_core_mismatch")
            require(database_identity(paths["database"])["sha256"] == backup["sha256"],
                    "rollback_database_mismatch")
            state["phase"] = "restored"; journal_write(journal, state)
        finally:
            data_lock.close()
        # The database backup is durable and the exact old app/Core are back
        # before the old runtime can acquire its lifetime lock.
        run_core(paths["core"], paths["app_data"], "start", "--if-needed")
        run_core(paths["core"], paths["app_data"], "status")
        if state.get("login_continuity", {}).get("required") is True:
            owner = remembered_login_owner(backup_path)
            require(owner is not None, "rollback_login_binding_invalid")
            probe = login_continuity_probe(paths["core"], paths["app_data"], owner)
            state["login_continuity"]["recovered"] = probe
            state["phase"] = "rollback_authentication_unverified"
            journal_write(journal, state)
            require(probe["passed"], "rollback_automatic_login_unverified")
        state["phase"] = "rolled_back_healthy"; journal_write(journal, state)
        return state
    finally:
        if install_lock is not None:
            install_lock.close()



def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def read_bounded_json(path):
    path = private_real(path)
    snapshot = _regular_snapshot(path)
    require(snapshot["bytes"] <= MAX_JSON_BYTES, "json_too_large")
    raw = path.read_bytes()
    require(len(raw) == snapshot["bytes"] and sha256(path) == snapshot["sha256"],
            "json_changed")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)


def load_core_recipe():
    require(sha256(CANONICAL_BUILD_CORE) == CANONICAL_BUILD_CORE_SHA256
            and sha256(MAC_CORE_CERTIFICATE) == MAC_CORE_CERTIFICATE_SHA256,
            "verification_source_changed")
    spec = importlib.util.spec_from_file_location("gamja_packaged_build_core", CANONICAL_BUILD_CORE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_app_signature(app):
    require(sha256(MAC_CORE_CERTIFICATE) == MAC_CORE_CERTIFICATE_SHA256,
            "certificate_changed")
    checked = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", "--all-architectures", str(app)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
    )
    require(checked.returncode == 0, "app_signature_invalid")
    requirement = 'anchor = H"12035B8BBAB3E028E3433EDF37449688B3930438" and identifier "top.gamja.bot"'
    checked = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--strict", "--all-architectures", "-R=" + requirement, str(app)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
    )
    require(checked.returncode == 0, "app_requirement_mismatch")
    detail = subprocess.run(
        ["/usr/bin/codesign", "-d", "--verbose=4", str(app)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    lines = (detail.stdout + detail.stderr).decode("utf-8", errors="replace").splitlines()
    require(detail.returncode == 0 and f"Identifier={BUNDLE_ID}" in lines,
            "app_identifier_mismatch")
    require(not any(line.startswith("Timestamp=") for line in lines)
            and any("flags=0x10000(runtime)" in line for line in lines), "app_signature_policy")
    with tempfile.TemporaryDirectory(prefix="gamja-public-signature-") as temp:
        prefix = Path(temp) / "certificate"
        extracted = subprocess.run(
            ["/usr/bin/codesign", "-d", "--extract-certificates=" + str(prefix), str(app)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        leaf = prefix.with_name(prefix.name + "0")
        require(extracted.returncode == 0 and leaf.is_file()
                and leaf.read_bytes() == MAC_CORE_CERTIFICATE.read_bytes(),
                "app_certificate_mismatch")


def signed_tree(root):
    """Exact signing-supervisor inventory, without following any symlink."""
    result = {}
    root = Path(root)
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(directory) / name
            info = path.lstat()
            key = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                result[key] = ["link", os.readlink(path)]
            elif stat.S_ISREG(info.st_mode):
                require(info.st_nlink == 1, "signed_tree_hardlink")
                result[key] = ["file", stat.S_IMODE(info.st_mode), info.st_size, sha256(path)]
            elif stat.S_ISDIR(info.st_mode):
                result[key] = ["directory", stat.S_IMODE(info.st_mode)]
            else:
                raise CutoverError("signed_tree_unsafe")
    return result


def signing_receipt(path, expected_sha256):
    require(sha256(path) == expected_sha256, "signing_receipt_changed")
    value = read_bounded_json(path)
    require(isinstance(value, dict)
            and value.get("status") == "success" and value.get("phase") == "complete"
            and value.get("error") is None and value.get("installed") is False
            and value.get("core_broker_artifact_unchanged") is True
            and value.get("immutable_broker_sha256") == IMMUTABLE_BROKER_SHA256,
            "signing_receipt_invalid")
    tree = value.get("full_signed_tree")
    require(isinstance(tree, dict) and bool(tree), "signed_tree_missing")
    for relative, entry in tree.items():
        require(isinstance(relative, str) and relative
                and not PurePosixPath(relative).is_absolute()
                and PurePosixPath(relative).as_posix() == relative
                and ".." not in PurePosixPath(relative).parts
                and isinstance(entry, list) and entry
                and entry[0] in {"file", "directory", "link"}, "signed_tree_invalid")
    require(type(value.get("dmg_bytes")) is int and value["dmg_bytes"] > 0,
            "signing_receipt_invalid")
    return value


def validate_verifier(authority):
    require(isinstance(authority, dict) and set(authority) == {"path", "sha256"},
            "runtime_verifier_invalid")
    verifier = private_real(authority["path"])
    require(verifier.stat().st_nlink == 1 and verifier.stat().st_mode & 0o111
            and sha256(verifier) == authority["sha256"]
            and all(not verifier.is_relative_to(root) for root in (
                APP, canonical_paths()["core"], canonical_paths()["app_data"],
            )), "runtime_verifier_invalid")
    recipe = load_core_recipe()
    recipe.verify_macos_signature(verifier, recipe.MAC_CORE_IDENTIFIER)
    return authority


@contextmanager
def mounted_candidate(source):
    receipt = signing_receipt(source["signing_receipt"], source["signing_receipt_sha256"])
    dmg = private_real(source["dmg"])
    snapshot = _regular_snapshot(dmg)
    require(snapshot["sha256"] == source["dmg_sha256"] == receipt["dmg_sha256"]
            and snapshot["bytes"] == source["dmg_bytes"] == receipt["dmg_bytes"],
            "dmg_changed")
    mount = Path(tempfile.mkdtemp(prefix="gamja-packaged-readonly-")).resolve()
    attached = False
    try:
        result = subprocess.run(
            ["/usr/bin/hdiutil", "attach", str(dmg), "-mountpoint", str(mount),
             "-readonly", "-nobrowse", "-noautoopen", "-plist"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        # Even a partial attach must be detached. Never remove a mounted tree.
        attached = os.path.ismount(mount)
        require(result.returncode == 0 and attached, "dmg_mount_failed")
        app = mount / "Gamja Bot.app"
        require(app.is_dir() and not app.is_symlink(), "mounted_app_missing")
        require(signed_tree(app) == receipt["full_signed_tree"], "signed_tree_mismatch")
        identity = app_identity(app)
        require(identity["main_sha256"] == receipt["signed_main_sha256"], "main_signature_changed")
        require(_regular_snapshot(dmg) == snapshot, "dmg_changed")
        yield app
        require(_regular_snapshot(dmg) == snapshot, "dmg_changed")
    finally:
        if attached or os.path.ismount(mount):
            try:
                subprocess.run(
                    ["/usr/bin/hdiutil", "detach", str(mount)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                )
            except subprocess.SubprocessError:
                pass  # The observed final mount state controls safe cleanup.
        # A still-mounted volume must be retained, never recursively removed.
        require(not os.path.ismount(mount), "dmg_cleanup_uncertain")
        shutil.rmtree(mount)


def validate_plan(plan):
    require(isinstance(plan, dict) and plan.get("version") == 1
            and plan.get("kind") == "gamja-packaged-cli-cutover"
            and plan.get("plan_sha256") == plan_digest(plan), "plan_invalid")
    expected_schema_from_plan(plan)
    require(plan.get("operator_sha256") == sha256(__file__), "operator_changed")
    paths = canonical_paths()
    require(plan.get("canonical") == {key: str(path) for key, path in paths.items()}
            and plan.get("recovery") == str(paths["app_data"] / "recovery.noindex")
            and isinstance(plan.get("operation_id"), str)
            and len(plan["operation_id"]) == 32
            and all(c in "0123456789abcdef" for c in plan["operation_id"]), "plan_paths_invalid")
    require(plan["live"]["app"]["path"] == str(paths["app"])
            and plan["live"]["core"]["path"] == str(paths["core"])
            and plan["live"]["database"]["path"] == str(paths["database"])
            and plan["candidate"]["app"]["path"] == str(paths["app"]), "plan_paths_invalid")
    require(plan["expected_schema"] >= plan["live"]["database"]["schema"],
            "schema_downgrade_unsupported")
    validate_verifier(plan["runtime_verifier"])
    upgrade = plan.get("login_upgrade")
    if upgrade is not None:
        require(isinstance(upgrade, dict) and set(upgrade) == {
            "profile", "database", "authenticated_same_owner"}
            and upgrade["authenticated_same_owner"] is True
            and isinstance(upgrade["database"], dict), "login_upgrade_plan_invalid")
        source = upgrade_database(upgrade["profile"])
        require(source != paths["database"] and not source.is_relative_to(paths["app_data"])
                and upgrade["database"].get("path") == str(source), "login_upgrade_profile_unsafe")




def assert_unchanged(plan):
    live = plan["live"]
    require(app_identity(live["app"]["path"]) == live["app"], "live_app_changed")
    require(core_identity(live["core"]["path"], installed=True,
                          runtime_verifier=plan["runtime_verifier"]) == live["core"],
            "live_core_changed")
    require(database_identity(live["database"]["path"]) == live["database"], "live_database_changed")


def read_plan(path, expected):
    plan = read_bounded_json(path)
    require(isinstance(plan, dict) and plan.get("plan_sha256") == expected, "plan_invalid")
    validate_plan(plan)
    return plan


def install_candidate_core_with_cli(app, runtime, candidate, runtime_verifier):
    require(not Path(runtime).exists() and not Path(runtime).is_symlink(), "runtime_not_absent")
    validate_verifier(runtime_verifier)
    source = private_real(Path(app["path"]) / "Contents/Resources/gamja-core", directory=True)
    require(same_core_payload(core_identity(source), candidate), "installer_source_changed")
    completed = subprocess.run(
        [runtime_verifier["path"], "runtime", "install", "--source", str(source),
         "--manifest-sha256", candidate["manifest_sha256"],
         "--core-sha256", candidate["core_sha256"],
         "--engine-sha256", candidate["engine_sha256"]],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
    )
    require(completed.returncode == 0 and len(completed.stdout) <= MAX_CLI_OUTPUT
            and len(completed.stderr) <= MAX_CLI_OUTPUT, "runtime_install_failed")
    value = json.loads(completed.stdout, object_pairs_hook=_unique_object)
    require(value == {"version": 1, "operation": "install", "status": "installed"},
            "runtime_install_result_invalid")
    installed = core_identity(runtime, installed=True, runtime_verifier=runtime_verifier)
    require(same_core_payload(installed, candidate), "installed_payload_mismatch")
    return installed


def preflight(dmg_path, signing_receipt_path, signing_receipt_sha256,
              runtime_verifier_path, runtime_verifier_sha256, expected_schema, plan_output,
              login_upgrade_profile=None):
    paths = canonical_paths()
    authority = validate_verifier({"path": str(Path(runtime_verifier_path).absolute()),
                                   "sha256": runtime_verifier_sha256})
    receipt = signing_receipt(signing_receipt_path, signing_receipt_sha256)
    source = {"dmg": str(Path(dmg_path).absolute()), "dmg_sha256": receipt["dmg_sha256"],
              "dmg_bytes": receipt["dmg_bytes"],
              "signing_receipt": str(Path(signing_receipt_path).absolute()),
              "signing_receipt_sha256": signing_receipt_sha256}
    with mounted_candidate(source) as app:
        require(not Path(authority["path"]).is_relative_to(app), "runtime_verifier_invalid")
        candidate_app = app_identity(app)
        candidate_app["path"] = str(paths["app"])
        candidate_core = core_identity(app / "Contents/Resources/gamja-core")
        candidate_core.pop("path")
        plan = {"version": 1, "kind": "gamja-packaged-cli-cutover", "operation_id": uuid4().hex,
                "operator_sha256": sha256(__file__),
                "created_at": int(time.time()), "phase": "prepared", "source": source,
                "candidate": {"app": candidate_app, "core": candidate_core},
                "live": {"app": app_identity(paths["app"]),
                         "core": core_identity(paths["core"], installed=True, runtime_verifier=authority),
                         "database": database_identity(paths["database"])},
                "canonical": {key: str(path) for key, path in paths.items()},
                "runtime_verifier": authority, "expected_schema": expected_schema,
                "recovery": str(paths["app_data"] / "recovery.noindex")}
        if login_upgrade_profile is not None:
            source_db = upgrade_database(login_upgrade_profile)
            plan["login_upgrade"] = {"profile": str(source_db.parent),
                "database": database_identity(source_db), "authenticated_same_owner": True}
            verify_upgrade_login(plan, app / "Contents/Resources/gamja-core",
                                 remembered_login_owner(paths["database"]))
            assert_unchanged(plan)
        plan["plan_sha256"] = plan_digest(plan)
        validate_plan(plan)
        output = Path(plan_output).absolute()
        private_real(output.parent, directory=True)
        require(all(output != root and not output.is_relative_to(root) for root in (
            paths["app"], paths["core"], paths["app_data"], app,
        )), "plan_output_unsafe")
        require(output not in {Path(v) for k, v in source.items() if k in {"dmg", "signing_receipt"}}
                and output != Path(authority["path"]), "plan_output_unsafe")
    write_exclusive(output, plan)
    return {"status": "prepared", "plan_sha256": plan["plan_sha256"]}


def apply(plan):
    validate_plan(plan)
    app_data = private_real(plan["canonical"]["app_data"], directory=True)
    install_lock = operation_lock(app_data)
    try:
        with mounted_candidate(plan["source"]) as app:
            require(same_identity(app_identity(app), plan["candidate"]["app"])
                    and same_core_payload(core_identity(app / "Contents/Resources/gamja-core"),
                                          plan["candidate"]["core"]), "candidate_changed")
            try:
                return _apply(plan, app, operation_held=True)
            except CutoverError as error:
                if str(error) != "automatic_login_not_preserved":
                    raise
                recovered = _rollback(plan, operation_held=True)
                require(recovered["phase"] == "rolled_back_healthy", "login_recovery_unverified")
                raise CutoverError("automatic_login_not_preserved_rolled_back") from None
    finally:
        install_lock.close()


def rollback(plan):
    validate_plan(plan)
    return _rollback(plan)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    pre = sub.add_parser("preflight")
    for name in ("dmg", "signing-receipt", "runtime-verifier", "plan"):
        pre.add_argument("--" + name, type=Path, required=True)
    for name in ("signing-receipt-sha256", "runtime-verifier-sha256"):
        pre.add_argument("--" + name, required=True)
    pre.add_argument("--expected-schema", type=parse_expected_schema, required=True)
    pre.add_argument("--login-upgrade-profile", type=Path)
    for name in ("apply", "rollback"):
        command = sub.add_parser(name)
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--plan-sha256", required=True)
        command.add_argument("--acknowledge-developer-pilot", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.operation == "preflight":
            result = preflight(args.dmg, args.signing_receipt, args.signing_receipt_sha256,
                               args.runtime_verifier, args.runtime_verifier_sha256,
                               args.expected_schema, args.plan, args.login_upgrade_profile)
        else:
            require(args.acknowledge_developer_pilot, "acknowledgement_required")
            plan = read_plan(args.plan, args.plan_sha256)
            state = apply(plan) if args.operation == "apply" else rollback(plan)
            result = {"status": state["phase"], "plan_sha256": plan["plan_sha256"]}
        print(json.dumps(result))
        return 0
    except (CutoverError, OSError, ValueError, KeyError, TypeError,
            sqlite3.Error, subprocess.SubprocessError) as error:
        # Never emit subprocess output or Owner data on errors.
        code = str(error) if isinstance(error, CutoverError) else type(error).__name__
        print(json.dumps({"status": "failed", "code": code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
