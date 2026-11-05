import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from contextlib import contextmanager
import pytest

def load():
    path = Path(__file__).resolve().parents[2] / "scripts/cutover_packaged_gamja.py"
    spec = importlib.util.spec_from_file_location("cutover_packaged_gamja", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def core_tree(root: Path):
    (root / "bin").mkdir(parents=True)
    (root / "libexec/gamja").mkdir(parents=True)
    (root / "bin/gamja").write_bytes(b"core")
    (root / "libexec/gamja/gamja-engine").write_bytes(b"engine")
    (root / "core-manifest.json").write_bytes(b"manifest")


def receipt_bound_runtime(module, tmp_path, monkeypatch, *, label="new", body=b"new"):
    recipe_path = module.REPO / "scripts/build_core.py"
    spec = importlib.util.spec_from_file_location("cutover_test_build_core", recipe_path)
    recipe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recipe)
    recipe.verify_macos_signature = lambda *_args, **_kwargs: None
    recipe._run_bounded_core_cli = lambda *_args, **_kwargs: (
        0, b'{"version":1,"operation":"verify","status":"valid"}\n', b""
    )
    monkeypatch.setattr(module, "load_core_recipe", lambda: recipe)
    candidate = tmp_path / f"{label}-candidate"
    for relative, body, mode in (
        ("bin/gamja", body + b"-core", 0o700),
        ("libexec/gamja/gamja-engine", body + b"-engine", 0o755),
        ("THIRD-PARTY-NOTICES.txt", b"notices", 0o600),
    ):
        path = candidate / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        path.chmod(mode)
    recipe.write_manifest(candidate, target="aarch64-apple-darwin", version="test", provenance={})
    expected = module.core_identity(candidate)
    runtime = tmp_path / "runtime"
    recipe.stage_verified_package(candidate, runtime, "aarch64-apple-darwin")
    core = (runtime / "bin/gamja").stat()
    engine = (runtime / "libexec/gamja/gamja-engine").stat()
    receipt = {
        "version": 1,
        "runtime_path": str(runtime.resolve()),
        "manifest_sha256": recipe.sha256(runtime / "core-manifest.json"),
        "core_sha256": recipe.sha256(runtime / "bin/gamja"),
        "engine_sha256": recipe.sha256(runtime / "libexec/gamja/gamja-engine"),
        "core_device": core.st_dev,
        "core_inode": core.st_ino,
        "core_uid": core.st_uid,
        "core_mode": core.st_mode & 0o777,
        "engine_device": engine.st_dev,
        "engine_inode": engine.st_ino,
        "engine_uid": engine.st_uid,
        "engine_mode": engine.st_mode & 0o777,
    }
    receipt_path = runtime / recipe.RUNTIME_RECEIPT
    receipt_path.write_text(json.dumps(receipt))
    receipt_path.chmod(0o600)
    verifier = tmp_path / f"operator-verifier-{label}"
    verifier.write_bytes(b"operator verifier " + label.encode())
    verifier.chmod(0o700)
    authority = {"path": str(verifier), "sha256": recipe.sha256(verifier)}
    return runtime, expected, authority


class Recipe:
    RUNTIME_RECEIPT = ".gamja-runtime-receipt.json"

    def __init__(self):
        self.allow = []

    def verify_package(self, _root, _target, *, allow_runtime_receipt=False,
                       runtime_verifier=None, runtime_verifier_sha256=None):
        self.allow.append((allow_runtime_receipt, runtime_verifier, runtime_verifier_sha256))
        return {
            "files": {
                "bin/gamja": {"bytes": 4, "sha256": "0" * 64},
                "libexec/gamja/gamja-engine": {"bytes": 6, "sha256": "1" * 64},
                "framework/Current": {"symlink": "A"},
            },
            "browser_payload": True,
            "version": "test",
        }


def test_candidate_symlink_entries_are_strict_and_not_counted_as_payload(tmp_path, monkeypatch):
    module = load()
    core_tree(tmp_path)
    recipe = Recipe()
    monkeypatch.setattr(module, "load_core_recipe", lambda: recipe)

    identity = module.core_identity(tmp_path)

    assert recipe.allow == [(False, None, None)]
    assert identity["payload_bytes"] == 10


def test_runtime_receipt_is_allowed_only_for_installed_core(tmp_path, monkeypatch):
    module = load()
    core_tree(tmp_path)
    (tmp_path / ".gamja-runtime-receipt.json").write_text("{}")
    recipe = Recipe()
    monkeypatch.setattr(module, "load_core_recipe", lambda: recipe)

    module.core_identity(tmp_path)
    verifier = tmp_path.parent / "operator-verifier"
    verifier.write_bytes(b"verifier")
    verifier.chmod(0o700)
    authority = {"path": str(verifier), "sha256": hashlib.sha256(b"verifier").hexdigest()}
    module.core_identity(tmp_path, installed=True, runtime_verifier=authority)

    assert recipe.allow == [
        (False, None, None),
        (True, verifier, authority["sha256"]),
    ]


def test_installed_core_requires_external_verifier_authority_before_recipe_call(tmp_path, monkeypatch):
    module = load()
    core_tree(tmp_path)
    (tmp_path / ".gamja-runtime-receipt.json").write_text("{}")
    recipe = Recipe()
    monkeypatch.setattr(module, "load_core_recipe", lambda: recipe)

    with pytest.raises(module.CutoverError, match="runtime_verifier_required"):
        module.core_identity(tmp_path, installed=True)

    assert recipe.allow == []


def test_rollback_classifies_genuine_receipt_bound_candidate_by_exact_payload(tmp_path, monkeypatch):
    module = load()
    runtime, candidate, authority = receipt_bound_runtime(module, tmp_path, monkeypatch)
    old = {**candidate, "path": str(tmp_path / "old"), "core_sha256": "0" * 64}

    assert module.core_identity_kind(runtime, old, candidate, authority) == "candidate"


@pytest.mark.parametrize("tamper", ["payload", "extra"])
def test_rollback_refuses_tampered_or_extra_receipt_bound_candidate(tmp_path, monkeypatch, tamper):
    module = load()
    runtime, candidate, authority = receipt_bound_runtime(module, tmp_path, monkeypatch)
    old = {**candidate, "path": str(tmp_path / "old"), "core_sha256": "0" * 64}
    if tamper == "payload":
        (runtime / "bin/gamja").write_bytes(b"tampered")
    else:
        (runtime / "unexpected").write_bytes(b"extra")

    assert module.core_identity_kind(runtime, old, candidate, authority) == "unknown"


@pytest.mark.parametrize("continuity", [
    "not_required", "restored", "failed", "snapshot_failure", "journal_failure",
])
def test_receipt_bound_candidate_failure_restores_old_runtime_receipt_and_database(
    tmp_path, monkeypatch, continuity
):
    module = load()
    live = tmp_path / "live"
    old_runtime, _, authority = receipt_bound_runtime(module, live, monkeypatch, label="old", body=b"old")
    old_receipt_inode = (old_runtime / ".gamja-runtime-receipt.json").stat().st_ino
    old_core = module.core_identity(
        old_runtime, installed=True, runtime_verifier=authority
    )
    app_data = tmp_path / "appdata"
    app_data.mkdir(mode=0o700)
    database = app_data / "gamja_bot.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
        db.execute("INSERT INTO schema_version VALUES (21)")
        db.execute("CREATE TABLE durable(value TEXT)")
        db.execute("INSERT INTO durable VALUES ('kept')")
    database.chmod(0o600)
    if continuity != "not_required":
        login_database(database)
    old_app = tmp_path / "Gamja Bot.app"
    old_app.mkdir(mode=0o700)
    (old_app / "identity").write_bytes(b"old-app")

    def app_identity(path):
        path = Path(path)
        return {"path": str(path), "tree_sha256": module.tree_sha256(path)}

    monkeypatch.setattr(module, "app_identity", app_identity)
    operation_id = "a" * 32
    recovery = app_data / "recovery.noindex" / operation_id
    recovery.mkdir(mode=0o700, parents=True)
    (recovery / "database").mkdir(mode=0o700)
    backup_path = recovery / "database/gamja_bot.db"
    shutil.copyfile(database, backup_path)
    backup_path.chmod(0o600)
    backup = module.database_identity(backup_path)
    old_app_identity = app_identity(old_app)
    old_database = module.database_identity(database)
    old_app.rename(recovery / "Gamja Bot.app")
    old_runtime.rename(recovery / "core-runtime")
    old_app.mkdir(mode=0o700)
    (old_app / "identity").write_bytes(b"new-app")
    candidate_runtime, candidate_core, candidate_authority = receipt_bound_runtime(
        module, live, monkeypatch, label="candidate", body=b"candidate"
    )
    assert candidate_authority != authority
    candidate_app = app_identity(old_app)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE durable SET value='candidate-change'")
    plan = {
        "version": 1,
        "operation_id": operation_id,
        "created_at": 1,
        "live": {"app": old_app_identity, "core": old_core, "database": old_database},
        "candidate": {"app": candidate_app, "core": candidate_core},
        "runtime_verifier": authority,
        "canonical": {
            "app": str(old_app), "core": str(candidate_runtime),
            "app_data": str(app_data), "database": str(database),
        },
        "recovery": str(app_data / "recovery.noindex"),
        "phase": "prepared",
    }
    plan["plan_sha256"] = module.plan_digest(plan)
    journal = recovery / "journal.json"
    journal.write_text(json.dumps({
        "version": 1, "operation_id": operation_id,
        "plan_sha256": plan["plan_sha256"], "phase": "activation_failed",
        "old": plan["live"], "new": plan["candidate"], "database_backup": backup,
        "login_continuity":{"required":continuity != "not_required"},
    }))
    journal.chmod(0o600)
    monkeypatch.setattr(module, "run_core", lambda *_args: None)
    def login_probe(_core, _app_data, owner):
        assert owner == 7
        assert (candidate_runtime / ".gamja-runtime-receipt.json").stat().st_ino == old_receipt_inode
        return {"authenticated":continuity == "restored",
                "same_owner":continuity == "restored", "passed":continuity == "restored"}
    monkeypatch.setattr(module, "login_continuity_probe", login_probe)

    if continuity in {"snapshot_failure", "journal_failure"}:
        def fail(*_args, **_kwargs):
            raise OSError("injected_snapshot_failure")
        monkeypatch.setattr(module, "sqlite_backup" if continuity == "snapshot_failure"
                            else "journal_write", fail)
        monkeypatch.setattr(module, "restore_database", lambda *_args: pytest.fail(
            "must preserve candidate data before restoring old DB"))
        monkeypatch.setattr(module.shutil, "rmtree", lambda *_args: pytest.fail(
            "must preserve candidate data before removing candidate binaries"))
        with pytest.raises(OSError, match="^injected_snapshot_failure$"):
            module._rollback(plan)
        assert app_identity(old_app) == candidate_app
        assert module.core_identity_kind(candidate_runtime, old_core,
                                         candidate_core, authority) == "candidate"
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT value FROM durable").fetchone() == ("candidate-change",)
        assert "candidate_database_before_rollback" not in json.loads(journal.read_text())
        return

    if continuity == "failed":
        with pytest.raises(module.CutoverError, match="^rollback_automatic_login_unverified$"):
            module._rollback(plan)
        rolled = json.loads(journal.read_text())
        assert rolled["phase"] == "rollback_authentication_unverified"
    else:
        rolled = module._rollback(plan)
        assert rolled["phase"] == "rolled_back_healthy"
    assert module.core_identity(
        candidate_runtime, installed=True, runtime_verifier=authority
    ) == old_core
    assert (candidate_runtime / ".gamja-runtime-receipt.json").stat().st_ino == old_receipt_inode
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT value FROM durable").fetchone() == ("kept",)
    # Rollback must retain the candidate's newer data separately, even though
    # the live database is restored for the older runtime.
    snapshot = recovery / "database-after-candidate/gamja_bot.db"
    assert rolled["candidate_database_before_rollback"] == module.database_identity(snapshot)
    with sqlite3.connect(snapshot) as db:
        assert db.execute("SELECT value FROM durable").fetchone() == ("candidate-change",)


def candidate_backup_fixture(tmp_path):
    module = load()
    recovery = tmp_path / "recovery"
    recovery.mkdir(mode=0o700)
    database = tmp_path / "gamja_bot.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
        db.execute("INSERT INTO schema_version VALUES (25)")
        db.execute("CREATE TABLE durable(value TEXT)")
        db.execute("INSERT INTO durable VALUES ('before')")
    database.chmod(0o600)
    journal = recovery / "journal.json"
    state = {"phase": "activation_failed"}
    module.journal_write(journal, state)
    return module, {"database": database}, recovery, journal, state


def test_post26_snapshot_survives_pre25_restore_and_repeated_rollback(tmp_path):
    module, paths, recovery, journal, state = candidate_backup_fixture(tmp_path)
    original = recovery / "database/gamja_bot.db"
    module.sqlite_backup(paths["database"], original)
    # Additive schema/data changes, including committed WAL rows, belong in
    # the separately retained candidate DB, not in the older runtime's DB.
    with sqlite3.connect(paths["database"]) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("ALTER TABLE durable ADD COLUMN candidate_note TEXT")
        db.execute("INSERT INTO schema_version VALUES (26)")
        db.execute("INSERT INTO durable VALUES ('after', 'keep me')")
        db.commit()
        kept = module.preserve_candidate_database_before_rollback(
            paths, recovery, journal, state)
    assert kept["schema"] == 26 and kept["mode"] == 0o600
    assert json.loads(journal.read_text())["candidate_database_before_rollback"] == kept
    snapshot = recovery / "database-after-candidate/gamja_bot.db"
    inode = snapshot.stat().st_ino
    module.restore_database(original, paths["database"])
    assert module.database_identity(paths["database"])["schema"] == 25
    assert module.preserve_candidate_database_before_rollback(
        paths, recovery, journal, state) == kept
    assert snapshot.stat().st_ino == inode
    with sqlite3.connect(snapshot) as db:
        assert db.execute("SELECT * FROM durable").fetchall() == [
            ("before", None), ("after", "keep me")]
    with sqlite3.connect(paths["database"]) as db:
        assert db.execute("SELECT * FROM durable").fetchall() == [("before",)]


@pytest.mark.parametrize("tamper", ["bytes", "journal", "symlink"])
def test_post_candidate_snapshot_cannot_be_replaced_or_rebound(tmp_path, tamper):
    module, paths, recovery, journal, state = candidate_backup_fixture(tmp_path)
    module.preserve_candidate_database_before_rollback(paths, recovery, journal, state)
    snapshot = recovery / "database-after-candidate/gamja_bot.db"
    if tamper == "bytes":
        with sqlite3.connect(snapshot) as db:
            db.execute("UPDATE durable SET value='changed'")
    elif tamper == "journal":
        state["candidate_database_before_rollback"]["path"] = str(paths["database"])
    else:
        snapshot.rename(snapshot.with_name("kept.db"))
        snapshot.symlink_to(paths["database"])
    before = paths["database"].read_bytes()
    with pytest.raises(module.CutoverError):
        module.preserve_candidate_database_before_rollback(paths, recovery, journal, state)
    assert paths["database"].read_bytes() == before


def test_unjournaled_candidate_snapshot_blocks_restore_without_deletion(tmp_path):
    module, paths, recovery, journal, state = candidate_backup_fixture(tmp_path)
    snapshot = recovery / "database-after-candidate/gamja_bot.db"
    snapshot.parent.mkdir(mode=0o700)
    snapshot.write_bytes(b"partial snapshot kept for recovery")
    before = paths["database"].read_bytes()
    with pytest.raises(module.CutoverError, match="candidate_database_backup_unjournaled"):
        module.preserve_candidate_database_before_rollback(paths, recovery, journal, state)
    assert snapshot.read_bytes() == b"partial snapshot kept for recovery"
    assert paths["database"].read_bytes() == before


@pytest.mark.parametrize("tamper", ["receipt_path", "payload_mode", "extra"])
def test_quarantined_old_runtime_rejects_receipt_binding_or_inventory_tamper(
    tmp_path, monkeypatch, tamper
):
    module = load()
    runtime, _, authority = receipt_bound_runtime(module, tmp_path, monkeypatch, label="old", body=b"old")
    old = module.core_identity(
        runtime, installed=True, runtime_verifier=authority
    )
    quarantined = tmp_path / "quarantined"
    runtime.rename(quarantined)
    if tamper == "receipt_path":
        receipt_path = quarantined / ".gamja-runtime-receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["runtime_path"] = str(tmp_path / "arbitrary")
        receipt_path.write_text(json.dumps(receipt))
        receipt_path.chmod(0o600)
    elif tamper == "payload_mode":
        (quarantined / "bin/gamja").chmod(0o777)
    else:
        (quarantined / "unexpected").write_bytes(b"extra")

    assert module.quarantined_old_core_kind(quarantined, old) == "unknown"


def test_historical_recovery_without_new_snapshot_refuses_before_mutation(tmp_path):
    module = load()
    app_data = tmp_path / "appdata"
    app_data.mkdir(mode=0o700)
    plan = {
        "canonical": {"app_data": str(app_data)},
        "live": {"core": {"tree_sha256": "1" * 64}},
    }

    with pytest.raises(
        module.CutoverError, match="historical_recovery_requires_review"
    ):
        module._rollback(plan)

    assert list(app_data.iterdir()) == []


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"x":{"a":1,"a":2}}'])
def test_json_rejects_duplicate_keys_in_each_object(tmp_path, raw):
    module = load()
    source = tmp_path / "receipt.json"
    source.write_bytes(raw)
    with pytest.raises(module.CutoverError, match="duplicate_json_key"):
        module.read_bounded_json(source)


def test_json_accepts_shared_field_names_and_large_complete_receipt(tmp_path):
    module = load()
    source = tmp_path / "receipt.json"
    value = {"one": {"path": "first"}, "two": {"path": "second"}, "padding": "p" * 70000}
    source.write_text(json.dumps(value))
    assert module.read_bounded_json(source) == value
    source.write_bytes(b" " * (module.MAX_JSON_BYTES + 1))
    with pytest.raises(module.CutoverError, match="json_too_large"):
        module.read_bounded_json(source)


def receipt_fixture(module, tmp_path, tree):
    receipt = tmp_path / "signing-receipt.json"
    value = {"status": "success", "phase": "complete", "error": None,
             "installed": False, "core_broker_artifact_unchanged": True,
             "immutable_broker_sha256": module.IMMUTABLE_BROKER_SHA256,
             "dmg_sha256": module.sha256(tmp_path / "candidate.dmg"),
             "dmg_bytes": (tmp_path / "candidate.dmg").stat().st_size,
             "signed_main_sha256": "a" * 64, "full_signed_tree": tree}
    receipt.write_text(json.dumps(value))
    receipt.chmod(0o600)
    return receipt, value


def test_receipt_requires_complete_inventory_and_immutable_broker(tmp_path):
    module = load()
    (tmp_path / "candidate.dmg").write_bytes(b"dmg")
    receipt, value = receipt_fixture(module, tmp_path, {"Contents": ["directory", 0o755]})
    assert module.signing_receipt(receipt, module.sha256(receipt)) == value
    value["full_signed_tree_sample"] = value.pop("full_signed_tree")
    receipt.write_text(json.dumps(value))
    with pytest.raises(module.CutoverError, match="signed_tree_missing"):
        module.signing_receipt(receipt, module.sha256(receipt))
    value["full_signed_tree"] = value.pop("full_signed_tree_sample")
    value["immutable_broker_sha256"] = "b" * 64
    receipt.write_text(json.dumps(value))
    with pytest.raises(module.CutoverError, match="signing_receipt_invalid"):
        module.signing_receipt(receipt, module.sha256(receipt))


def mounted_fixture(module, tmp_path, monkeypatch):
    fixture = tmp_path / "fixture.app"
    (fixture / "Contents").mkdir(parents=True)
    (fixture / "Contents/file").write_bytes(b"signed file")
    (fixture / "Contents/Current").symlink_to("file")
    dmg = tmp_path / "candidate.dmg"
    dmg.write_bytes(b"dmg")
    receipt, _value = receipt_fixture(module, tmp_path, module.signed_tree(fixture))
    source = {"dmg": str(dmg), "dmg_sha256": module.sha256(dmg), "dmg_bytes": dmg.stat().st_size,
              "signing_receipt": str(receipt), "signing_receipt_sha256": module.sha256(receipt)}
    calls = []
    active = set()
    owned = []
    def make_temp(*args, **kwargs):
        path = tmp_path / (kwargs["prefix"] + str(len(owned)))
        path.mkdir(mode=0o700)
        owned.append(path)
        return str(path)
    def run(command, **kwargs):
        calls.append(command)
        mount = Path(command[command.index("-mountpoint") + 1]) if "attach" in command else Path(command[2])
        if "attach" in command:
            active.add(mount)
            shutil.copytree(fixture, mount / "Gamja Bot.app", symlinks=True)
        else:
            active.remove(mount)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    monkeypatch.setattr(module.tempfile, "mkdtemp", make_temp)
    monkeypatch.setattr(module.os.path, "ismount", lambda p: Path(p) in active)
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module, "app_identity", lambda p: {"path": str(p), "main_sha256": "a" * 64})
    return fixture, source, calls, owned, active


def test_complete_mounted_tree_checks_links_and_always_detaches(tmp_path, monkeypatch):
    module = load()
    fixture, source, calls, owned, active = mounted_fixture(module, tmp_path, monkeypatch)
    with module.mounted_candidate(source) as app:
        assert (app / "Contents/Current").is_symlink()
        assert module.signed_tree(app) == module.signed_tree(fixture)
    assert not active and all(not p.exists() for p in owned)
    assert "-readonly" in calls[0] and "-noautoopen" in calls[0]
    assert calls[-1][1] == "detach"


@pytest.mark.parametrize("tamper", ["extra", "link", "mode", "bytes"])
def test_mounted_tree_tamper_fails_before_use_and_detaches(tmp_path, monkeypatch, tamper):
    module = load()
    fixture, source, calls, owned, active = mounted_fixture(module, tmp_path, monkeypatch)
    if tamper == "extra":
        (fixture / "Contents/extra").write_bytes(b"extra")
    elif tamper == "link":
        (fixture / "Contents/Current").unlink()
        (fixture / "Contents/Current").symlink_to("different")
    elif tamper == "mode":
        (fixture / "Contents/file").chmod(0o777)
    else:
        (fixture / "Contents/file").write_bytes(b"changed")
    with pytest.raises(module.CutoverError, match="signed_tree_mismatch"):
        with module.mounted_candidate(source):
            pytest.fail("unverified candidate reached consumer")
    assert not active and all(not p.exists() for p in owned)
    assert calls[-1][1] == "detach"


def test_cli_installer_installs_canonical_source_and_independently_verifies_receipt(tmp_path, monkeypatch):
    module = load()
    app = tmp_path / "Gamja Bot.app"
    source = app / "Contents/Resources/gamja-core"
    source.mkdir(parents=True)
    runtime = tmp_path / "runtime"
    pins = {"manifest_sha256": "a" * 64, "core_sha256": "b" * 64, "engine_sha256": "c" * 64}
    authority = {"path": str(tmp_path / "independent-core"), "sha256": "d" * 64}
    calls = []
    def identify(path, **kwargs):
        calls.append((path, kwargs))
        return dict(pins)
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=b'{"version":1,"operation":"install","status":"installed"}', stderr=b"")
    monkeypatch.setattr(module, "validate_verifier", lambda a: a)
    monkeypatch.setattr(module, "core_identity", identify)
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: pytest.fail("GUI must never launch"))
    assert module.install_candidate_core_with_cli({"path": str(app)}, runtime, pins, authority) == pins
    command = calls[1]
    assert command[:3] == [authority["path"], "runtime", "install"]
    assert command[command.index("--source") + 1] == str(source)
    assert calls[-1] == (runtime, {"installed": True, "runtime_verifier": authority})


def test_apply_rechecks_candidate_before_live_mutation(tmp_path, monkeypatch):
    module = load()
    plan = {"source": {}, "candidate": {"app": {"main_sha256": "a"}, "core": {}},
            "canonical":{"app_data":str(tmp_path)}}
    @contextmanager
    def mount(source):
        yield tmp_path
    monkeypatch.setattr(module, "validate_plan", lambda p: None)
    monkeypatch.setattr(module, "mounted_candidate", mount)
    monkeypatch.setattr(module, "app_identity", lambda p: {"main_sha256": "changed"})
    monkeypatch.setattr(module, "_apply", lambda *a: pytest.fail("mutation before artifact check"))
    with pytest.raises(module.CutoverError, match="candidate_changed"):
        module.apply(plan)


def test_plan_canonical_paths_reject_arbitrary_database_before_verifier(tmp_path, monkeypatch):
    module = load()
    paths = module.canonical_paths()
    plan = {"version": 1, "kind": "gamja-packaged-cli-cutover", "expected_schema": 25,
            "operator_sha256": module.sha256(module.__file__),
            "operation_id": "a" * 32, "canonical": {k: str(v) for k, v in paths.items()},
            "recovery": str(paths["app_data"] / "recovery.noindex")}
    plan["canonical"]["database"] = str(tmp_path / "other.db")
    plan["plan_sha256"] = module.plan_digest(plan)
    monkeypatch.setattr(module, "validate_verifier", lambda p: pytest.fail("must reject paths first"))
    with pytest.raises(module.CutoverError, match="plan_paths_invalid"):
        module.validate_plan(plan)


def test_snapshot_rejects_symlink_replacement_without_following_it(tmp_path, monkeypatch):
    module = load()
    source = tmp_path / "source"
    source.write_bytes(b"public test data")
    moved = tmp_path / "original"
    real_open = module.os.open
    def replaced(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        source.rename(moved)
        source.symlink_to(moved.name)
        return descriptor
    monkeypatch.setattr(module.os, "open", replaced)
    with pytest.raises(module.CutoverError, match="unsafe_path"):
        module._regular_snapshot(source)
    assert source.is_symlink() and moved.read_bytes() == b"public test data"


def test_concurrent_operation_lock_is_rejected(tmp_path):
    module = load()
    first = module.operation_lock(tmp_path)
    try:
        with pytest.raises(module.CutoverError, match="cutover_already_running"):
            module.operation_lock(tmp_path)
    finally:
        first.close()


def test_partial_attach_failure_detaches_owned_mount_before_error(tmp_path, monkeypatch):
    module = load()
    fixture, source, calls, owned, active = mounted_fixture(module, tmp_path, monkeypatch)
    real_run = module.subprocess.run
    def partial(command, **kwargs):
        result = real_run(command, **kwargs)
        if "attach" in command:
            result.returncode = 1
        return result
    monkeypatch.setattr(module.subprocess, "run", partial)
    with pytest.raises(module.CutoverError, match="dmg_mount_failed"):
        with module.mounted_candidate(source):
            pytest.fail("partial mount was accepted")
    assert not active and all(not p.exists() for p in owned)


def test_detach_error_with_unmounted_volume_still_removes_temp(tmp_path, monkeypatch):
    module = load()
    fixture, source, calls, owned, active = mounted_fixture(module, tmp_path, monkeypatch)
    real_run = module.subprocess.run
    def detached_with_error(command, **kwargs):
        result = real_run(command, **kwargs)
        if "detach" in command:
            result.returncode = 1
        return result
    monkeypatch.setattr(module.subprocess, "run", detached_with_error)
    with module.mounted_candidate(source):
        pass
    assert not active and all(not p.exists() for p in owned)


def test_mounted_volume_is_retained_when_detach_fails(tmp_path, monkeypatch):
    module = load()
    fixture, source, calls, owned, active = mounted_fixture(module, tmp_path, monkeypatch)
    real_run = module.subprocess.run
    def cannot_detach(command, **kwargs):
        if "detach" in command:
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")
        return real_run(command, **kwargs)
    monkeypatch.setattr(module.subprocess, "run", cannot_detach)
    with pytest.raises(module.CutoverError, match="dmg_cleanup_uncertain"):
        with module.mounted_candidate(source):
            pass
    assert active and owned[0].exists()
    assert (owned[0] / "Gamja Bot.app/Contents/file").read_bytes() == b"signed file"
    active.clear()  # Fake mounts only; pytest owns and removes the fixture.


def test_verifier_signature_uses_canonical_identifier(tmp_path, monkeypatch):
    module = load()
    verifier = tmp_path / "external-core"
    verifier.write_bytes(b"public verifier fixture")
    verifier.chmod(0o700)
    seen = []
    recipe = SimpleNamespace(MAC_CORE_IDENTIFIER="top.gamja.credential-core",
                             verify_macos_signature=lambda p, i: seen.append((p, i)))
    monkeypatch.setattr(module, "load_core_recipe", lambda: recipe)
    authority = {"path": str(verifier), "sha256": module.sha256(verifier)}
    assert module.validate_verifier(authority) == authority
    assert seen == [(verifier, "top.gamja.credential-core")]


def test_public_leaf_extraction_uses_correct_argument_and_enforces_pin(tmp_path, monkeypatch):
    module = load()
    certificate = tmp_path / "public.cer"
    certificate.write_bytes(b"public certificate fixture")
    monkeypatch.setattr(module, "MAC_CORE_CERTIFICATE", certificate)
    monkeypatch.setattr(module, "MAC_CORE_CERTIFICATE_SHA256", module.sha256(certificate))
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        for arg in command:
            if arg.startswith("--extract-certificates="):
                Path(arg.split("=", 1)[1] + "0").write_bytes(certificate.read_bytes())
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"Identifier=top.gamja.bot\nflags=0x10000(runtime)\n")
    monkeypatch.setattr(module.subprocess, "run", run)
    module.verify_app_signature(tmp_path)
    assert any(any(arg.startswith("--extract-certificates=") for arg in cmd) for cmd in commands)
    assert any(any(arg.startswith("-R=anchor = H") for arg in cmd) for cmd in commands)


def login_database(path, *, active=1, expired=False):
    expiry = datetime.now(timezone.utc) + timedelta(days=-1 if expired else 1)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE users(id INTEGER PRIMARY KEY,is_active INTEGER)")
        db.execute("CREATE TABLE remembered_login(id INTEGER,owner_id INTEGER,expires_at TEXT)")
        db.execute("INSERT INTO users VALUES(7,?)", (active,))
        db.execute("INSERT INTO remembered_login VALUES(1,7,?)", (expiry.isoformat(),))


@pytest.mark.parametrize("active,expired,expected", [(1,False,7), (0,False,None), (1,True,None)])
def test_login_binding_requires_active_unexpired_owner(tmp_path, active, expired, expected):
    module = load()
    path = tmp_path / "database.sqlite3"
    login_database(path, active=active, expired=expired)
    before = path.read_bytes()
    assert module.remembered_login_owner(path) == expected
    assert path.read_bytes() == before


def test_missing_login_table_needs_no_credential_probe(tmp_path, monkeypatch):
    module = load()
    database = tmp_path / "database.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE schema_version(version INTEGER)")
    monkeypatch.setattr(module, "assert_unchanged", lambda plan: None)
    monkeypatch.setattr(module, "run_core", lambda *_: pytest.fail("must not start a core"))
    assert module.old_login_baseline({}, {"database": database}) is None


@pytest.mark.parametrize("code,output,passed", [
    (0,b'{"authenticated":true}\n{"id":7,"email":"private@example.test"}\n',True),
    (0,b'{"authenticated":true}\n{"id":8}\n',False),
    (0,b'{"authenticated":1}\n{"id":7}\n',False),
    (0,b'{"authenticated":true}\n{"id":true}\n',False),
    (0,b'{"authenticated":true}\n{"id":7,"id":8}\n',False),
    (0,b'{"authenticated":true}\n{"id":7}\n{"extra":true}\n',False),
    (0,b'not-json\n',False),
    (1,b'{"code":"credential_locked"}\n',False),
])
def test_no_ui_restore_and_me_are_one_bounded_private_session(
    tmp_path, monkeypatch, code, output, passed
):
    module = load()
    seen = []
    def run(argv, *, timeout_seconds):
        commands = Path(argv[-1])
        seen.append(commands.parent)
        assert argv[:-1] == ["/usr/bin/env", "HOME="+str(Path.home()),
                            str(tmp_path / "core/bin/gamja"), "--app-data", str(tmp_path), "--batch"]
        assert timeout_seconds == 30
        assert commands.stat().st_mode & 0o777 == 0o600
        assert commands.read_text() == '{"command":["restore"]}\n{"command":["me"]}\n'
        return code, output, b"private-native-detail"
    monkeypatch.setattr(module, "load_core_recipe", lambda: SimpleNamespace(_run_bounded_core_cli=run))
    result = module.login_continuity_probe(tmp_path / "core", tmp_path, 7)
    assert result["passed"] is passed
    assert set(result) == {"authenticated", "same_owner", "passed"}
    assert all(type(value) is bool for value in result.values())
    assert "private" not in json.dumps(result)
    assert len(seen) == 1 and not seen[0].exists()


def test_login_probe_timeout_is_failure_and_cleans_private_batch(tmp_path, monkeypatch):
    module = load()
    seen = []
    def run(argv, **kwargs):
        seen.append(Path(argv[-1]).parent)
        raise subprocess.TimeoutExpired(argv, 30, output=b"private")
    monkeypatch.setattr(module, "load_core_recipe", lambda: SimpleNamespace(_run_bounded_core_cli=run))
    assert module.login_continuity_probe(tmp_path, tmp_path, 7)["passed"] is False
    assert not seen[0].exists()


def test_old_probe_stops_core_even_on_login_failure(tmp_path, monkeypatch):
    module = load()
    database = tmp_path / "database.sqlite3"
    login_database(database)
    monkeypatch.setattr(module, "assert_unchanged", lambda _: None)
    monkeypatch.setattr(module, "backend_lock", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(module.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1))
    monkeypatch.setattr(module, "login_continuity_probe", lambda *_: {"passed":False})
    commands = []
    monkeypatch.setattr(module, "run_core", lambda *args: commands.append(args[2:]))
    with pytest.raises(module.CutoverError, match="old_automatic_login_unverified"):
        module.old_login_baseline({}, {"database":database,"core":tmp_path,"app_data":tmp_path})
    assert commands == [("start","--if-needed"), ("stop",)]


def cutover_wrapper(module, monkeypatch):
    monkeypatch.setattr(module, "validate_plan", lambda _: None)
    @contextmanager
    def candidate(_source):
        yield Path("/fixture/Gamja Bot.app")
    monkeypatch.setattr(module, "mounted_candidate", candidate)
    monkeypatch.setattr(module, "app_identity", lambda _: {})
    monkeypatch.setattr(module, "core_identity", lambda _: {})
    monkeypatch.setattr(module, "same_identity", lambda *_: True)
    monkeypatch.setattr(module, "same_core_payload", lambda *_: True)
    monkeypatch.setattr(module, "private_real", lambda path, **_: Path(path))
    lock = SimpleNamespace(closed=False)
    def close():lock.closed=True
    lock.close=close
    monkeypatch.setattr(module, "operation_lock", lambda _: lock)
    return {"source":{},"candidate":{"app":{},"core":{}},
            "canonical":{"app_data":"/fixture/private"},"test_lock":lock}


def test_public_apply_automatically_recovers_login_regression(monkeypatch):
    module = load()
    plan = cutover_wrapper(module, monkeypatch)
    events = []
    def apply(*_, operation_held=False):
        assert operation_held and not plan["test_lock"].closed
        events.append("apply")
        raise module.CutoverError("automatic_login_not_preserved")
    def rollback(_, *, operation_held=False):
        assert operation_held and not plan["test_lock"].closed
        events.append("rollback")
        return {"phase":"rolled_back_healthy"}
    monkeypatch.setattr(module, "_apply", apply)
    monkeypatch.setattr(module, "_rollback", rollback)
    with pytest.raises(module.CutoverError, match="^automatic_login_not_preserved_rolled_back$"):
        module.apply(plan)
    assert events == ["apply","rollback"]
    assert plan["test_lock"].closed


def test_public_apply_does_not_mask_uncertain_login_recovery(monkeypatch):
    module = load()
    plan = cutover_wrapper(module, monkeypatch)
    def apply(*_, **_kwargs):raise module.CutoverError("automatic_login_not_preserved")
    def rollback(_, **_kwargs):raise module.CutoverError("rollback_automatic_login_unverified")
    monkeypatch.setattr(module, "_apply", apply)
    monkeypatch.setattr(module, "_rollback", rollback)
    with pytest.raises(module.CutoverError, match="^rollback_automatic_login_unverified$"):
        module.apply(plan)


def test_public_apply_leaves_unrelated_identity_failures_for_review(monkeypatch):
    module = load()
    plan = cutover_wrapper(module, monkeypatch)
    def apply(*_, **_kwargs):raise module.CutoverError("live_core_changed")
    monkeypatch.setattr(module, "_apply", apply)
    monkeypatch.setattr(module, "_rollback", lambda _: pytest.fail("no broad destructive recovery"))
    with pytest.raises(module.CutoverError, match="live_core_changed"):
        module.apply(plan)


def upgraded_login_fixture(tmp_path):
    module = load()
    live = tmp_path / "live"
    upgraded = tmp_path / "upgraded"
    live.mkdir(mode=0o700)
    upgraded.mkdir(mode=0o700)
    source = live / "gamja_bot.db"
    expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with sqlite3.connect(source) as db:
        db.executescript("CREATE TABLE schema_version(version INTEGER);"
                         "INSERT INTO schema_version VALUES(25);"
                         "CREATE TABLE users(id INTEGER PRIMARY KEY,is_active INTEGER);"
                         "INSERT INTO users VALUES(7,1);"
                         "CREATE TABLE remembered_login(id INTEGER PRIMARY KEY,owner_id INTEGER,"
                         "secret_ref TEXT,secret_hash TEXT,revision INTEGER,expires_at TEXT);"
                         "CREATE TABLE conversations(id INTEGER,body TEXT);")
        db.execute("INSERT INTO remembered_login VALUES(1,7,?,?,3,?)",
                   ("synthetic-old-ref", "a" * 64, expiry))
        db.execute("INSERT INTO conversations VALUES(1,?)", ("synthetic room context",))
    destination = upgraded / "gamja_bot.db"
    shutil.copyfile(source, destination)
    source.chmod(0o600)
    destination.chmod(0o600)
    with sqlite3.connect(destination) as db:
        db.execute("UPDATE remembered_login SET secret_ref=?,secret_hash=?,revision=4",
                   ("synthetic-new-ref", "b" * 64))
    plan = {"canonical": {"app_data": str(live), "database": str(source)},
            "live": {"database": module.database_identity(source)},
            "login_upgrade": {"profile": str(upgraded),
                "database": module.database_identity(destination), "authenticated_same_owner": True}}
    return module, plan, source, destination


def test_adoption_preserves_all_business_rows_and_owner_expiry(tmp_path):
    module, plan, source, destination = upgraded_login_fixture(tmp_path)
    destination_before = destination.read_bytes()
    assert module.adopt_upgrade_login(plan, 7) is True
    with sqlite3.connect(source) as original, sqlite3.connect(destination) as upgraded:
        for table in ("users", "conversations", "schema_version", "remembered_login"):
            assert original.execute("SELECT * FROM " + table).fetchall() == upgraded.execute(
                "SELECT * FROM " + table).fetchall()
    assert destination.read_bytes() == destination_before
    assert "synthetic" not in json.dumps(plan)
    with pytest.raises(module.CutoverError, match="live_database_changed"):
        module.adopt_upgrade_login(plan, 7)


@pytest.mark.parametrize("query", [
    "UPDATE conversations SET body='other room context'",
    "INSERT INTO conversations VALUES(2,'extra row')",
    "DELETE FROM conversations",
    "UPDATE users SET is_active=0",
    "UPDATE remembered_login SET owner_id=8",
    "UPDATE remembered_login SET expires_at='2099-01-01'",
    "UPDATE remembered_login SET revision=3",
    "UPDATE remembered_login SET revision=5",
    "UPDATE remembered_login SET secret_ref='synthetic-old-ref'",
    "UPDATE remembered_login SET secret_hash='invalid'",
    "ALTER TABLE conversations ADD COLUMN extra TEXT",
    "CREATE TRIGGER unrelated AFTER UPDATE ON conversations BEGIN SELECT 1; END",
])
def test_adoption_rejects_unrelated_or_invalid_changes_without_writing(tmp_path, query):
    module, plan, source, destination = upgraded_login_fixture(tmp_path)
    with sqlite3.connect(destination) as db:
        db.execute(query)
    plan["login_upgrade"]["database"] = module.database_identity(destination)
    before = source.read_bytes()
    with pytest.raises(module.CutoverError):
        module.adopt_upgrade_login(plan, 7)
    assert source.read_bytes() == before


def test_adoption_rejects_source_changed_after_preflight(tmp_path):
    module, plan, source, destination = upgraded_login_fixture(tmp_path)
    with sqlite3.connect(destination) as db:
        db.execute("UPDATE conversations SET body='changed after proof'")
    before = source.read_bytes()
    with pytest.raises(module.CutoverError, match="login_upgrade_source_changed"):
        module.adopt_upgrade_login(plan, 7)
    assert source.read_bytes() == before


def test_adoption_rolls_back_before_commit_if_source_snapshot_changes(tmp_path, monkeypatch):
    module, plan, source, destination = upgraded_login_fixture(tmp_path)
    before = source.read_bytes()
    calls = []
    original_snapshot = module._regular_snapshot
    def changed_snapshot(path):
        assert path == destination
        calls.append(1)
        snapshot = original_snapshot(path)
        if len(calls) >= 3:
            snapshot["sha256"] = "f" * 64
        return snapshot
    monkeypatch.setattr(module, "_regular_snapshot", changed_snapshot)
    with pytest.raises(module.CutoverError, match="login_upgrade_source_changed"):
        module.adopt_upgrade_login(plan, 7)
    assert source.read_bytes() == before


def test_upgrade_login_requires_fresh_normal_auth_and_always_stops(tmp_path, monkeypatch):
    module, plan, source, destination = upgraded_login_fixture(tmp_path)
    seen = []
    monkeypatch.setattr(module, "run_core", lambda *a: seen.append(a[2:]))
    monkeypatch.setattr(module, "login_continuity_probe", lambda *_: {"passed": False})
    before = source.read_bytes()
    with pytest.raises(module.CutoverError, match="login_upgrade_auth_unverified"):
        module.verify_upgrade_login(plan, tmp_path / "candidate", 7)
    assert seen == [("start", "--if-needed"), ("stop",)]
    assert source.read_bytes() == before


def test_upgrade_profile_refuses_canonical_profile_and_link_or_journal(tmp_path, monkeypatch):
    module, plan, source, destination = upgraded_login_fixture(tmp_path)
    plan["canonical"] = {"app_data": str(destination.parent), "database": str(destination)}
    monkeypatch.setattr(module, "run_core", lambda *_: pytest.fail("no native action"))
    with pytest.raises(module.CutoverError, match="login_upgrade_profile_unsafe"):
        module.verify_upgrade_login(plan, tmp_path / "core", 7)
    journal = Path(str(destination) + "-journal")
    journal.write_bytes(b"synthetic")
    with pytest.raises(module.CutoverError, match="login_upgrade_not_quiescent"):
        module.upgrade_database(destination.parent)
    journal.unlink()
    duplicate = destination.with_name("hardlink")
    os.link(destination, duplicate)
    with pytest.raises(module.CutoverError, match="login_upgrade_profile_unsafe"):
        module.upgrade_database(destination.parent)


def test_upgrade_plan_contains_no_login_values_and_probe_does_not_mutate(tmp_path, monkeypatch):
    module, plan, source, destination = upgraded_login_fixture(tmp_path)
    before = source.read_bytes(), destination.read_bytes()
    monkeypatch.setattr(module, "run_core", lambda *_: None)
    monkeypatch.setattr(module, "login_continuity_probe", lambda *_: {"passed": True})
    module.verify_upgrade_login(plan, tmp_path / "candidate", 7)
    assert (source.read_bytes(), destination.read_bytes()) == before
    assert "secret_ref" not in json.dumps(plan) and "secret_hash" not in json.dumps(plan)


def test_quoted_schema_names_are_literal_identifiers_and_preserve_data(tmp_path):
    module, plan, source, destination = upgraded_login_fixture(tmp_path)
    name = 'odd name"; DROP TABLE users;--'
    quoted = '"' + name.replace('"', '""') + '"'
    for path in (source, destination):
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE " + quoted + "(value TEXT)")
            db.execute("INSERT INTO " + quoted + " VALUES(?)", ("synthetic literal",))
    plan["live"]["database"] = module.database_identity(source)
    plan["login_upgrade"]["database"] = module.database_identity(destination)
    assert module.adopt_upgrade_login(plan, 7) is True
    with sqlite3.connect(source) as db:
        assert db.execute("SELECT * FROM " + quoted).fetchall() == [("synthetic literal",)]
        assert db.execute("SELECT COUNT(*) FROM users").fetchone() == (1,)


def test_source_snapshot_is_bounded_and_read_only(tmp_path, monkeypatch):
    module, plan, source, destination = upgraded_login_fixture(tmp_path)
    connection, snapshot = module.pinned_upgrade_connection(destination)
    try:
        assert snapshot["sha256"] == plan["login_upgrade"]["database"]["sha256"]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM users")
    finally:
        connection.close()
    monkeypatch.setattr(module, "MAX_LOGIN_SNAPSHOT_BYTES", 1)
    with pytest.raises(module.CutoverError, match="login_upgrade_snapshot_too_large"):
        module.pinned_upgrade_connection(destination)
