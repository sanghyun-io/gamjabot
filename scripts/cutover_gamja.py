#!/usr/bin/env python3
"""Reviewed Gamja-only developer-pilot cutover with exact rollback assets.

`preflight` is read-only except for its exclusive 0600 plan output. `apply` and
`rollback` require that exact plan hash and an explicit acknowledgement. They
operate only on the canonical Gamja app/Core/database paths and never inspect
user rows or credential values.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import stat
import subprocess
import time
from uuid import uuid4


APP = Path("/Applications/Gamja Bot.app")
BUNDLE_ID = "top.gamja.bot"


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


def verify_app_signature(app):
    verified = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=60, text=True)
    require(verified.returncode == 0, "app_signature_invalid")
    details = subprocess.run(["/usr/bin/codesign", "-d", "--verbose=4", str(app)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=30, text=True)
    output = (details.stdout or "") + (details.stderr or "")
    require(details.returncode == 0 and "Identifier=top.gamja.bot" in output
            and "Authority=Gamja Credential Core Signing v1" in output,
            "app_signature_invalid")


def private_real(path, *, directory=False):
    path = Path(path).absolute()
    info = path.lstat()
    require(not path.is_symlink() and info.st_uid == os.geteuid(), "unsafe_path")
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), "unsafe_path")
    require(not info.st_mode & 0o022 and path.resolve() == path, "unsafe_path")
    return path


def canonical_paths(home=None):
    home = Path(home or Path.home()).resolve()
    app_data = home / "Library/Application Support/top.gamja.bot"
    return {"app": APP, "core": home / ".local/share/gamja-bot/core/runtime",
            "app_data": app_data, "database": app_data / "gamja_bot.db"}


def load_core_recipe():
    path = Path(__file__).with_name("build_core.py")
    spec = importlib.util.spec_from_file_location("gamja_cutover_build_core", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


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


def core_identity(core):
    core = private_real(core, directory=True)
    recipe = load_core_recipe()
    manifest = recipe.verify_package(core, "aarch64-apple-darwin")
    files = manifest["files"]
    helper = files.get("libexec/gamja/gamja-platform-helper")
    return {"path": str(core), "manifest_sha256": sha256(core / "core-manifest.json"),
            "tree_sha256": tree_sha256(core),
            "core_sha256": sha256(core / "bin/gamja"),
            "engine_sha256": sha256(core / "libexec/gamja/gamja-engine"),
            "helper_sha256": helper.get("sha256") if isinstance(helper, dict) else None,
            "browser_payload": manifest["browser_payload"],
            "file_count": len(files),
            "payload_bytes": sum(item["bytes"] for item in files.values() if "symlink" not in item),
            "version": manifest["version"]}


def database_identity(path):
    path = private_real(path)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
        require(database.execute("PRAGMA quick_check").fetchone() == ("ok",), "database_invalid")
        version = database.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    return {"path": str(path), "sha256": sha256(path), "bytes": path.stat().st_size,
            "mode": stat.S_IMODE(path.stat().st_mode), "schema": version}


def plan_digest(value):
    payload = {key: item for key, item in value.items() if key != "plan_sha256"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def same_identity(first, second):
    """Compare artifact facts while allowing an exact artifact to move paths."""
    return ({key: value for key, value in first.items() if key != "path"}
            == {key: value for key, value in second.items() if key != "path"})


def make_plan(candidate_app, candidate_core, *, home=None, live_paths=None):
    paths = live_paths or canonical_paths(home)
    candidate_app = app_identity(candidate_app)
    candidate_core = core_identity(candidate_core)
    embedded = core_identity(Path(candidate_app["path"]) / "Contents/Resources/gamja-core")
    require({key: embedded[key] for key in ("manifest_sha256", "core_sha256", "engine_sha256")}
            == {key: candidate_core[key] for key in ("manifest_sha256", "core_sha256", "engine_sha256")},
            "candidate_binding_mismatch")
    plan = {"version": 1, "operation_id": uuid4().hex, "created_at": int(time.time()),
            "live": {"app": app_identity(paths["app"]), "core": core_identity(paths["core"]),
                     "database": database_identity(paths["database"])},
            "candidate": {"app": candidate_app, "core": candidate_core},
            "canonical": {key: str(value) for key, value in paths.items()},
            "recovery": str(paths["app_data"] / "recovery.noindex"),
            "phase": "prepared"}
    plan["plan_sha256"] = plan_digest(plan)
    return plan


def write_exclusive(path, value):
    raw = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(raw); output.flush(); os.fsync(output.fileno())
    fsync_directory(Path(path).parent)


def read_plan(path, expected):
    path = private_real(path)
    require(path.stat().st_size <= 65536, "plan_invalid")
    value = json.loads(path.read_text())
    require(isinstance(value, dict) and value.get("version") == 1
            and value.get("plan_sha256") == expected == plan_digest(value), "plan_invalid")
    return value


def sqlite_backup(source, destination):
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
    with sqlite3.connect(source) as original, sqlite3.connect(destination) as backup:
        original.backup(backup)
        require(backup.execute("PRAGMA quick_check").fetchone() == ("ok",), "database_backup_invalid")
    destination.chmod(0o600)
    fsync_file(destination); fsync_directory(destination.parent)
    return database_identity(destination)


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


def assert_unchanged(plan):
    live = plan["live"]
    require(app_identity(live["app"]["path"]) == live["app"], "live_app_changed")
    require(core_identity(live["core"]["path"]) == live["core"], "live_core_changed")
    require(database_identity(live["database"]["path"]) == live["database"], "live_database_changed")
    require(app_identity(plan["candidate"]["app"]["path"]) == plan["candidate"]["app"], "candidate_app_changed")
    require(core_identity(plan["candidate"]["core"]["path"]) == plan["candidate"]["core"], "candidate_core_changed")


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


def apply(plan):
    paths = {key: Path(value) for key, value in plan["canonical"].items()}
    private_real(paths["app_data"], directory=True)
    install_lock = operation_lock(paths["app_data"])
    try:
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
                 "old": plan["live"], "new": plan["candidate"]}
        journal_write(journal, state)
        try:
            assert_unchanged(plan)
            backup = sqlite_backup(paths["database"], recovery / "database" / "gamja_bot.db")
            state.update(phase="backed_up", database_backup=backup); journal_write(journal, state)
            require(subprocess.run(["/usr/bin/pgrep", "-x", "gamja-bot"],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL).returncode != 0,
                    "gamja_not_idle")
            candidate_stage = recovery / "candidate-app.stage"
            require(candidate_stage.parent.stat().st_dev == paths["app"].parent.stat().st_dev,
                    "candidate_stage_not_atomic")
            state["phase"] = "copying_candidate_app"; journal_write(journal, state)
            shutil.copytree(plan["candidate"]["app"]["path"], candidate_stage,
                            symlinks=True)
            require(same_identity(app_identity(candidate_stage), plan["candidate"]["app"]),
                    "candidate_stage_mismatch")
            state["phase"] = "candidate_app_staged"; journal_write(journal, state)
            state["phase"] = "moving_old_app"; journal_write(journal, state)
            paths["app"].rename(recovery / "Gamja Bot.app")
            state["phase"] = "old_app_preserved"; journal_write(journal, state)
            state["phase"] = "moving_old_core"; journal_write(journal, state)
            paths["core"].rename(recovery / "core-runtime")
            state["phase"] = "old_preserved"; journal_write(journal, state)
            candidate_stage.rename(paths["app"])
            state["phase"] = "candidate_app_installed"; journal_write(journal, state)
            load_core_recipe().stage_verified_package(Path(plan["candidate"]["core"]["path"]),
                                                      paths["core"], "aarch64-apple-darwin")
            state["phase"] = "candidate_installed"; journal_write(journal, state)
            require(same_identity(app_identity(paths["app"]), plan["candidate"]["app"]),
                    "activation_mismatch")
            require(same_identity(core_identity(paths["core"]), plan["candidate"]["core"]),
                    "activation_mismatch")
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
        state.update(phase="healthy", new_database=database_identity(paths["database"]))
        journal_write(journal, state)
        return state
    except BaseException:
        if "state" in locals() and state.get("phase") != "activation_failed":
            state.update(failed_from=state["phase"], phase="activation_failed")
            journal_write(journal, state)
        raise
    finally:
        install_lock.close()


def rollback(plan):
    paths = {key: Path(value) for key, value in plan["canonical"].items()}
    private_real(paths["app_data"], directory=True)
    install_lock = operation_lock(paths["app_data"])
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
        core_kind = identity_kind(paths["core"], plan["live"]["core"],
                                  plan["candidate"]["core"], core_identity)
        recovery_app = recovery / "Gamja Bot.app"
        recovery_core = recovery / "core-runtime"
        recovery_app_kind = identity_kind(recovery_app, plan["live"]["app"],
                                          plan["candidate"]["app"], app_identity)
        recovery_core_kind = identity_kind(recovery_core, plan["live"]["core"],
                                            plan["candidate"]["core"], core_identity)
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
            require(same_identity(core_identity(paths["core"]), plan["live"]["core"]),
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
        state["phase"] = "rolled_back_healthy"; journal_write(journal, state)
        return state
    finally:
        install_lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    prepare = sub.add_parser("preflight")
    prepare.add_argument("--candidate-app", type=Path, required=True)
    prepare.add_argument("--candidate-core", type=Path, required=True)
    prepare.add_argument("--plan", type=Path, required=True)
    for name in ("apply", "rollback"):
        command = sub.add_parser(name)
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--plan-sha256", required=True)
        command.add_argument("--acknowledge-developer-pilot", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.operation == "preflight":
            value = make_plan(args.candidate_app, args.candidate_core)
            write_exclusive(args.plan, value)
            print(json.dumps({"status": "prepared", "plan_sha256": value["plan_sha256"]}))
        else:
            require(args.acknowledge_developer_pilot, "acknowledgement_required")
            value = read_plan(args.plan, args.plan_sha256)
            result = apply(value) if args.operation == "apply" else rollback(value)
            print(json.dumps({"status": result["phase"], "plan_sha256": value["plan_sha256"]}))
        return 0
    except (CutoverError, OSError, ValueError, KeyError):
        print(json.dumps({"status": "failed", "code": "cutover_refused"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
