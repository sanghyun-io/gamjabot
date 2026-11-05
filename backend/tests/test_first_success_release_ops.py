import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import stat
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.auth import UserCreate

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def load_operator(name):
    path = ROOT / f".superpowers/sdd/2026-10-06-first-success/ops/{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def core_fixture(root, content=b"core"):
    for relative, value in {
        "bin/gamja": content, "libexec/gamja/gamja-engine": b"engine",
        "libexec/gamja/gamja-platform-helper": b"helper",
        "THIRD-PARTY-NOTICES.txt": b"notices",
    }.items():
        path = root / relative; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        if relative != "THIRD-PARTY-NOTICES.txt": path.chmod(0o700)
    recipe = load("build_core")
    recipe.verify_macos_signature = lambda *_: None
    recipe.write_manifest(root, target="aarch64-apple-darwin", version="1.0.0", provenance={})
    return root


def app_fixture(root, core):
    app = root / "Gamja Bot.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Resources").mkdir()
    (app / "Contents/MacOS/gamja-bot").write_bytes(b"app")
    (app / "Contents/MacOS/gamja-bot").chmod(0o700)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "top.gamja.bot", "CFBundleExecutable": "gamja-bot"}))
    recipe = load("build_core"); recipe.verify_macos_signature = lambda *_: None
    recipe.stage_verified_package(core, app / "Contents/Resources/gamja-core",
                                  "aarch64-apple-darwin")
    return app


def db_fixture(path, version):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
        database.execute("INSERT INTO schema_version VALUES (?)", (version,))
        database.execute("CREATE TABLE durable(value TEXT)")
        database.execute("INSERT INTO durable VALUES ('kept')")
    path.chmod(0o600)
    return path


def release_layout(tmp_path):
    old_core = core_fixture(tmp_path / "live-core", b"old-core")
    new_core = core_fixture(tmp_path / "candidate-core", b"new-core")
    old_app = app_fixture(tmp_path / "live-app", old_core)
    new_app = app_fixture(tmp_path / "candidate-app", new_core)
    app_data = tmp_path / "appdata"; app_data.mkdir(mode=0o700)
    database = db_fixture(app_data / "gamja_bot.db", 21)
    paths = {"app": old_app, "core": old_core, "app_data": app_data, "database": database}
    return paths, new_app, new_core


def fixture_recipe():
    recipe = load("build_core")
    recipe.verify_macos_signature = lambda *_: None
    return recipe


def test_fake_provider_is_text_only_bounded_and_cleans_up():
    smoke = load("verify_first_success_core")
    provider = smoke.FakeProvider()
    with provider:
        connection = http.client.HTTPConnection("127.0.0.1", provider.server.server_address[1])
        body = json.dumps({"model": "exact-smoke", "messages": []}).encode()
        connection.request("POST", "/v1/chat/completions", body,
                           {"Content-Type": "application/json", "Content-Length": str(len(body))})
        assert connection.getresponse().status == 200
        bad = json.dumps({"model": "exact-smoke", "messages": [], "tools": [{"type": "function"}]}).encode()
        connection.request("POST", "/v1/chat/completions", bad,
                           {"Content-Type": "application/json", "Content-Length": str(len(bad))})
        assert connection.getresponse().status == 422
    assert provider.count == 1
    assert provider.hashes == [hashlib.sha256(body).hexdigest()]
    assert not provider.thread.is_alive()


def test_packaged_smoke_requires_schema22():
    smoke = load("verify_first_success_core")

    smoke.require_schema_head(22)
    with pytest.raises(smoke.GateFailure, match="schema_head_invalid"):
        smoke.require_schema_head(21)


def test_smoke_registration_address_passes_the_canonical_user_schema():
    smoke = load("verify_first_success_core")

    registration = UserCreate(
        email=smoke.SMOKE_EMAIL,
        username="smoke",
        password="synthetic-only-password",
    )
    assert str(registration.email) == smoke.SMOKE_EMAIL
    with pytest.raises(ValidationError):
        UserCreate(
            email="first-success-smoke@example.invalid",
            username="smoke",
            password="synthetic-only-password",
        )


def test_smoke_route_allowlist_rejects_unrelated_or_secret_routes():
    smoke = load("verify_first_success_core")
    with pytest.raises(smoke.GateFailure, match="smoke_route_invalid"):
        smoke.api({"api_url": "http://127.0.0.1:1", "capability": "a" * 64},
                  "GET", "/api/v1/workspace/blueprint")


def test_smoke_receipt_completion_is_an_exact_safe_allowlist():
    smoke = load("verify_first_success_core")
    completion = {"state": "completed", "outcome_status": "succeeded",
                  "result_available": True, "completed_rounds": 2,
                  "pending_agent_count": 0}
    assert smoke.safe_completion({"completion": completion}) == completion
    with pytest.raises(smoke.GateFailure, match="completion_shape_invalid"):
        smoke.safe_completion({"completion": {**completion, "raw_provider_body": "private"}})


def exact_first_success_results():
    source = {
        "completion": {"state": "completed", "outcome_status": "succeeded",
                       "result_available": True, "completed_rounds": 2,
                       "pending_agent_count": 0},
        "execution": {"selected_agent_count": 2,
                      "selected_agents": [{"agent_id": 8}, {"agent_id": 9}],
                      "policy": {"max_rounds": 2, "max_tool_calls": 0,
                                 "max_executor_seconds": 60}},
        "outcomes": [{"agent_id": agent, "round": round_number,
                      "kind": "message", "phase": "succeeded"}
                     for round_number in (1, 2) for agent in (8, 9)],
        "outcome_count": 4, "outcomes_truncated": False,
        "actions": [], "action_count": 0, "actions_truncated": False,
    }
    final = {
        "completion": {"state": "completed", "outcome_status": "succeeded",
                       "result_available": True, "completed_rounds": 1,
                       "pending_agent_count": 0},
        "execution": {"selected_agent_count": 1, "selected_agents": [{"agent_id": 8}],
                      "policy": {"max_rounds": 1, "max_tool_calls": 0,
                                 "max_executor_seconds": 60}},
        "outcomes": [{"agent_id": 8, "round": 1,
                      "kind": "message", "phase": "succeeded"}],
        "outcome_count": 1, "outcomes_truncated": False,
        "actions": [], "action_count": 0, "actions_truncated": False,
    }
    return source, final


@pytest.mark.parametrize("defect", ["wrong_round", "failed_outcome", "wrong_request_count"])
def test_smoke_requires_exact_two_agent_rounds_successful_finalizer_and_six_calls(defect):
    smoke = load("verify_first_success_core")
    source, final = exact_first_success_results()
    count, hashes = 6, ["a" * 64] * 6
    if defect == "wrong_round":
        source["completion"]["completed_rounds"] = 1
    elif defect == "failed_outcome":
        final["completion"]["outcome_status"] = "failed"
    else:
        count, hashes = 5, ["a" * 64] * 5
    with pytest.raises(smoke.GateFailure):
        smoke.require_first_success_shape(source, final, [8, 9], count, hashes)
    clean_source, clean_final = exact_first_success_results()
    smoke.require_first_success_shape(clean_source, clean_final, [8, 9], 6,
                                      ["a" * 64] * 6)


def test_smoke_rejects_a_manifest_covered_artifact_mismatch_before_launch(tmp_path):
    smoke = load("verify_first_success_core")
    package = core_fixture(tmp_path / "package")
    (package / "bin/gamja").write_bytes(b"tampered-after-manifest")
    receipt = tmp_path / "must-not-exist.json"
    with pytest.raises(ValueError, match="manifest/hash coverage mismatch"):
        smoke.run_smoke(package, receipt)
    assert not receipt.exists()


def test_cutover_plan_binds_exact_app_core_database_and_detects_tamper(tmp_path):
    cutover = load("cutover_gamja")
    cutover.load_core_recipe = fixture_recipe
    cutover.verify_app_signature = lambda *_: None
    paths, candidate_app, candidate_core = release_layout(tmp_path)
    plan = cutover.make_plan(candidate_app, candidate_core, live_paths=paths)
    assert plan["phase"] == "prepared" and plan["plan_sha256"] == cutover.plan_digest(plan)
    assert plan["candidate"]["core"]["manifest_sha256"] == cutover.sha256(candidate_core / "core-manifest.json")
    assert plan["candidate"]["core"]["core_sha256"] == cutover.sha256(candidate_core / "bin/gamja")
    assert plan["candidate"]["core"]["tree_sha256"] == cutover.tree_sha256(candidate_core)
    assert plan["candidate"]["core"]["browser_payload"] is False
    assert plan["candidate"]["core"]["helper_sha256"] == cutover.sha256(
        candidate_core / "libexec/gamja/gamja-platform-helper")
    assert plan["candidate"]["core"]["file_count"] >= 3
    output = tmp_path / "plan.json"
    cutover.write_exclusive(output, plan)
    assert output.stat().st_mode & 0o077 == 0
    assert cutover.read_plan(output, plan["plan_sha256"]) == plan
    (candidate_core / "bin/gamja").write_bytes(b"tampered")
    with pytest.raises((cutover.CutoverError, ValueError), match="candidate_core_changed|hash|manifest"):
        cutover.assert_unchanged(plan)


def test_cutover_apply_and_rollback_preserve_exact_old_identity_and_order(monkeypatch, tmp_path):
    cutover = load("cutover_gamja")
    monkeypatch.setattr(cutover, "load_core_recipe", fixture_recipe)
    monkeypatch.setattr(cutover, "verify_app_signature", lambda *_: None)
    paths, candidate_app, candidate_core = release_layout(tmp_path)
    plan = cutover.make_plan(candidate_app, candidate_core, live_paths=paths)
    calls = []
    rolling_back = False
    def runtime(core, data, *args):
        # The backend lifetime lock must be free before either Core starts.
        probe = cutover.backend_lock(data); probe.close()
        if rolling_back and args == ("start", "--if-needed"):
            with sqlite3.connect(paths["database"]) as database:
                assert database.execute("SELECT value FROM durable").fetchone() == ("kept",)
        calls.append((Path(core), args))
    monkeypatch.setattr(cutover, "run_core", runtime)
    monkeypatch.setattr(cutover.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    old_app = plan["live"]["app"]
    old_core = plan["live"]["core"]
    applied = cutover.apply(plan)
    assert applied["phase"] == "healthy"
    assert calls[-2:] == [(paths["core"], ("start", "--if-needed")),
                          (paths["core"], ("status",))]
    recovery = Path(plan["recovery"]) / plan["operation_id"]
    assert (recovery / "Gamja Bot.app").is_dir() and (recovery / "core-runtime").is_dir()
    assert recovery.parent.stat().st_mode & 0o077 == 0
    assert (recovery / "database/gamja_bot.db").stat().st_mode & 0o077 == 0
    with sqlite3.connect(paths["database"]) as database:
        database.execute("UPDATE durable SET value='candidate-change'")
    rolling_back = True
    rolled = cutover.rollback(plan)
    assert rolled["phase"] == "rolled_back_healthy"
    assert cutover.app_identity(paths["app"]) == old_app
    assert cutover.core_identity(paths["core"]) == old_core
    with sqlite3.connect(paths["database"]) as database:
        assert database.execute("SELECT value FROM durable").fetchone() == ("kept",)


def test_cutover_activation_failure_keeps_recoverable_journal(monkeypatch, tmp_path):
    cutover = load("cutover_gamja")
    monkeypatch.setattr(cutover, "load_core_recipe", fixture_recipe)
    monkeypatch.setattr(cutover, "verify_app_signature", lambda *_: None)
    paths, candidate_app, candidate_core = release_layout(tmp_path)
    plan = cutover.make_plan(candidate_app, candidate_core, live_paths=paths)
    monkeypatch.setattr(cutover.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    recipe = cutover.load_core_recipe()
    monkeypatch.setattr(recipe, "stage_verified_package",
                        lambda *args: (_ for _ in ()).throw(RuntimeError("injected")))
    monkeypatch.setattr(cutover, "load_core_recipe", lambda: recipe)
    with pytest.raises(RuntimeError, match="injected"):
        cutover.apply(plan)
    journal = json.loads((Path(plan["recovery"]) / plan["operation_id"] / "journal.json").read_text())
    assert journal["phase"] == "activation_failed"
    assert journal["database_backup"]["sha256"]
    monkeypatch.setattr(cutover, "run_core", lambda *_: None)
    rolled = cutover.rollback(plan)
    assert rolled["phase"] == "rolled_back_healthy"
    assert cutover.app_identity(paths["app"]) == plan["live"]["app"]
    assert cutover.core_identity(paths["core"]) == plan["live"]["core"]


def test_cutover_mid_candidate_copy_failure_leaves_old_live_and_rollback_is_noop(monkeypatch, tmp_path):
    cutover = load("cutover_gamja")
    monkeypatch.setattr(cutover, "load_core_recipe", fixture_recipe)
    monkeypatch.setattr(cutover, "verify_app_signature", lambda *_: None)
    paths, candidate_app, candidate_core = release_layout(tmp_path)
    plan = cutover.make_plan(candidate_app, candidate_core, live_paths=paths)
    monkeypatch.setattr(cutover.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    def partial_copy(_source, destination, **_kwargs):
        destination.mkdir()
        (destination / "partial-copy").write_bytes(b"incomplete")
        raise OSError("injected mid-copy failure")
    monkeypatch.setattr(cutover.shutil, "copytree", partial_copy)
    with pytest.raises(OSError, match="mid-copy"):
        cutover.apply(plan)
    assert cutover.app_identity(paths["app"]) == plan["live"]["app"]
    assert cutover.core_identity(paths["core"]) == plan["live"]["core"]
    rolled = cutover.rollback(plan)
    assert rolled["phase"] == "rollback_not_required"
    assert cutover.app_identity(paths["app"]) == plan["live"]["app"]


def test_cutover_pre_move_failure_rollback_never_removes_valid_old_install(monkeypatch, tmp_path):
    cutover = load("cutover_gamja")
    monkeypatch.setattr(cutover, "load_core_recipe", fixture_recipe)
    monkeypatch.setattr(cutover, "verify_app_signature", lambda *_: None)
    paths, candidate_app, candidate_core = release_layout(tmp_path)
    plan = cutover.make_plan(candidate_app, candidate_core, live_paths=paths)
    # pgrep reports a still-open GUI after the private backup, before either old artifact moves.
    monkeypatch.setattr(cutover.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    with pytest.raises(cutover.CutoverError, match="gamja_not_idle"):
        cutover.apply(plan)
    old_app, old_core = plan["live"]["app"], plan["live"]["core"]
    result = cutover.rollback(plan)
    assert result["phase"] == "rollback_not_required"
    assert cutover.app_identity(paths["app"]) == old_app
    assert cutover.core_identity(paths["core"]) == old_core


def test_cutover_partial_old_move_restores_without_deleting_unmoved_core(monkeypatch, tmp_path):
    cutover = load("cutover_gamja")
    monkeypatch.setattr(cutover, "load_core_recipe", fixture_recipe)
    monkeypatch.setattr(cutover, "verify_app_signature", lambda *_: None)
    paths, candidate_app, candidate_core = release_layout(tmp_path)
    plan = cutover.make_plan(candidate_app, candidate_core, live_paths=paths)
    monkeypatch.setattr(cutover.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    original_rename = Path.rename
    def fail_core_move(path, target):
        if path == paths["core"]:
            raise OSError("injected core move failure")
        return original_rename(path, target)
    monkeypatch.setattr(Path, "rename", fail_core_move)
    with pytest.raises(OSError, match="injected"):
        cutover.apply(plan)
    monkeypatch.setattr(Path, "rename", original_rename)
    calls = []
    monkeypatch.setattr(cutover, "run_core", lambda core, data, *args: calls.append(args))
    result = cutover.rollback(plan)
    assert result["phase"] == "rolled_back_healthy"
    assert cutover.app_identity(paths["app"]) == plan["live"]["app"]
    assert cutover.core_identity(paths["core"]) == plan["live"]["core"]
    assert calls[-2:] == [("start", "--if-needed"), ("status",)]


def test_cutover_rollback_refuses_unknown_live_identity_before_deletion(monkeypatch, tmp_path):
    cutover = load("cutover_gamja")
    monkeypatch.setattr(cutover, "load_core_recipe", fixture_recipe)
    monkeypatch.setattr(cutover, "verify_app_signature", lambda *_: None)
    paths, candidate_app, candidate_core = release_layout(tmp_path)
    plan = cutover.make_plan(candidate_app, candidate_core, live_paths=paths)
    monkeypatch.setattr(cutover.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    monkeypatch.setattr(cutover, "run_core", lambda *_: None)
    cutover.apply(plan)
    unknown = paths["app"] / "Contents/MacOS/gamja-bot"
    unknown.write_bytes(b"unknown-live-app")
    with pytest.raises(cutover.CutoverError, match="rollback_identity_unknown"):
        cutover.rollback(plan)
    assert unknown.read_bytes() == b"unknown-live-app"
    assert (Path(plan["recovery"]) / plan["operation_id"] / "Gamja Bot.app").is_dir()


BROKER_SOURCE_FILES = (
    "credential-broker/Cargo.toml",
    "credential-broker/Cargo.lock",
    "credential-broker/src/lib.rs",
    "credential-broker/src/main.rs",
    "credential-broker/tests/protocol.rs",
    "scripts/macos_exact_signer.c",
    "scripts/build_core.py",
    "backend/tests/test_package_recipe.py",
)
BROKER_ANCHOR = "12035b8bbab3e028e3433edf37449688b3930438"


def broker_operator_fixture(tmp_path):
    supervisor = load_operator("first_success_build_supervisor")
    root = tmp_path / "repo"
    root.mkdir(mode=0o700)
    files = []
    for index, relative in enumerate(BROKER_SOURCE_FILES):
        path = root / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(f"accepted-{index}".encode())
        path.chmod(0o644)
        files.append({
            "path": relative,
            "bytes": path.stat().st_size,
            "mode": "0644",
            "sha256": supervisor.sha256(path),
        })
    material = "".join(
        f"{item['sha256']}  {item['bytes']}  {item['mode']}  {item['path']}\n"
        for item in files
    ).encode()
    inventory = {
        "version": 1,
        "kind": "stable-credential-broker-source-inventory",
        "file_count": len(files),
        "root_sha256": hashlib.sha256(material).hexdigest(),
        "files": files,
    }
    evidence = root / "docs/evidence"
    evidence.mkdir(mode=0o700, parents=True)
    inventory_path = evidence / "inventory.json"
    inventory_path.write_text(json.dumps(inventory, sort_keys=True))
    inventory_path.chmod(0o600)
    helper = evidence / "signer"
    helper.write_bytes(b"accepted-helper")
    helper.chmod(0o700)
    certificate = root / "core-host/macos-credential-core.cer"
    certificate.parent.mkdir(mode=0o700)
    certificate.write_bytes((ROOT / "core-host/macos-credential-core.cer").read_bytes())
    certificate.chmod(0o644)
    helper_receipt = evidence / "helper-receipt.json"
    helper_receipt.write_text(json.dumps({
        "version": 1,
        "kind": "stable-credential-broker-public-signer-reproduction",
        "status": "verified_public_build_only_helper",
        "source": {
            "path": "scripts/macos_exact_signer.c",
            "sha256": supervisor.sha256(root / "scripts/macos_exact_signer.c"),
        },
        "helper": {
            "architecture": "Mach-O 64-bit executable arm64",
            "bytes": helper.stat().st_size,
            "sha256": supervisor.sha256(helper),
            "codesign": "ad_hoc_strict_verified",
            "mode": "0700",
            "nlink": 1,
            "owner_current": True,
        },
        "identifiers": {
            "broker": "top.gamja.credential-broker.v1",
            "release_authority": "top.gamja.credential-release-authority.v1",
        },
        "execution": {
            "authority_signed": False,
            "broker_signed": False,
            "helper_invoked": False,
            "keychain_queried": False,
            "private_identity_used": False,
        },
        "modes": {
            "added": ["broker", "release-authority"],
            "existing": ["engine", "core", "library", "gui"],
        },
    }, sort_keys=True))
    helper_receipt.chmod(0o600)
    authority = {
        "version": 1,
        "kind": "stable-credential-broker-source-authority",
        "status": "source_and_signing_modes_independently_accepted",
        "inventory": {
            "path": str(inventory_path.relative_to(root)),
            "sha256": supervisor.sha256(inventory_path),
            "root_sha256": inventory["root_sha256"],
            "file_count": len(files),
        },
        "signer": {
            "anchor_sha1": BROKER_ANCHOR,
            "certificate_sha256": supervisor.sha256(certificate),
            "helper_sha256": supervisor.sha256(helper),
            "helper_receipt_sha256": supervisor.sha256(helper_receipt),
        },
        "reviews": {
            "broker_source": "a" * 64,
            "signing_modes": "b" * 64,
        },
    }
    authority_path = evidence / "authority.json"
    authority_path.write_text(json.dumps(authority, sort_keys=True))
    authority_path.chmod(0o600)
    keychain = evidence / "build.keychain-db"
    keychain.write_bytes(b"private-keychain-placeholder")
    keychain.chmod(0o600)
    cargo_target = tmp_path / "cargo-target"
    cargo_target.mkdir(mode=0o700)
    output = tmp_path / "outputs"
    output.mkdir(mode=0o700)
    config = supervisor.BrokerConfig(
        root=root,
        source_authority=authority_path,
        source_authority_sha256=supervisor.sha256(authority_path),
        helper=helper,
        helper_sha256=supervisor.sha256(helper),
        helper_receipt=helper_receipt,
        helper_receipt_sha256=supervisor.sha256(helper_receipt),
        certificate=certificate,
        certificate_sha256=supervisor.sha256(certificate),
        keychain=keychain,
        cargo_target_dir=cargo_target,
        broker_output=output / "gamja-credential-broker",
        signed_receipt=output / "signed-broker.json",
        journal=output / "broker-journal.json",
        signer_anchor_sha1=BROKER_ANCHOR,
        timeout_seconds=120,
        enforce_release_pins=False,
    )
    return supervisor, config, authority_path, inventory_path


def test_broker_config_is_closed_and_retained_controller_accepts_only_exact_shape(tmp_path):
    supervisor, config, _authority, _inventory = broker_operator_fixture(tmp_path)
    controller = load_operator("first_success_retained_identity_build")
    assert controller.PINNED_MODULES["supervisor"][1] == hashlib.sha256(
        (ROOT / ".superpowers/sdd/2026-10-06-first-success/ops/first_success_build_supervisor.py").read_bytes()
    ).hexdigest()
    fields = {
        name: str(getattr(config, name))
        for name in controller.BROKER_CONFIG_PATH_FIELDS
    }
    fields.update({name: getattr(config, name) for name in controller.BROKER_CONFIG_SHA_FIELDS})
    fields.update({
        "signer_anchor_sha1": BROKER_ANCHOR,
        "timeout_seconds": 120,
        "enforce_release_pins": True,
    })
    path = tmp_path / "broker-config.json"
    path.write_text(json.dumps({
        "version": 1,
        "kind": "stable-credential-broker-supervisor-config",
        "config": fields,
    }))
    path.chmod(0o600)
    deps = SimpleNamespace(supervisor=supervisor)
    loaded = controller.load_supervisor_config(path, deps=deps)
    assert isinstance(loaded, supervisor.BrokerConfig)
    assert loaded.signer_anchor_sha1 == BROKER_ANCHOR
    invalid = json.loads(path.read_text())
    invalid["config"]["unknown"] = "forbidden"
    path.write_text(json.dumps(invalid))
    with pytest.raises(controller.ControllerError, match="config_schema"):
        controller.load_supervisor_config(path, deps=deps)


def test_broker_source_authority_binds_exact_eight_files_and_signer_inputs(tmp_path):
    supervisor, config, authority, _inventory = broker_operator_fixture(tmp_path)
    verified = supervisor.verify_broker_source(config)
    assert verified["root_sha256"]
    assert verified["file_count"] == 8
    source = config.root / "credential-broker/src/main.rs"
    source.write_bytes(b"drift")
    with pytest.raises(supervisor.SupervisorError, match="broker_source_drift"):
        supervisor.verify_broker_source(config)
    source.write_bytes(b"accepted-3")
    source.chmod(0o644)
    tampered = json.loads(authority.read_text())
    tampered["signer"]["anchor_sha1"] = "0" * 40
    authority.write_text(json.dumps(tampered, sort_keys=True))
    config = supervisor.BrokerConfig(**{
        **config.__dict__, "source_authority_sha256": supervisor.sha256(authority)
    })
    with pytest.raises(supervisor.SupervisorError, match="broker_authority_binding"):
        supervisor.verify_broker_source(config)


def test_broker_execute_builds_and_signs_once_then_reuses_exact_artifact(tmp_path):
    supervisor, config, _authority, _inventory = broker_operator_fixture(tmp_path)
    events = []

    def build(current):
        events.append("build")
        current.broker_output.write_bytes(b"unsigned-broker")
        current.broker_output.chmod(0o700)

    def sign(current, password):
        events.append(("sign", bytes(password)))
        current.broker_output.write_bytes(b"signed-broker")
        current.broker_output.chmod(0o700)

    def verify(current):
        status = current.broker_output.stat()
        return {
            "path": str(current.broker_output),
            "identity": [status.st_dev, status.st_ino, status.st_uid, stat.S_IMODE(status.st_mode)],
            "sha256": supervisor.sha256(current.broker_output),
            "identifier": "top.gamja.credential-broker.v1",
            "designated_requirement": (
                f'designated => anchor = H"{BROKER_ANCHOR.upper()}" '
                'and identifier "top.gamja.credential-broker.v1"'
            ),
            "runtime": True,
            "timestamp": False,
        }

    result = supervisor.execute(
        config,
        prompt_password=lambda: bytearray(b"one-use-secret"),
        build_broker=build,
        sign_broker=sign,
        verify_broker=verify,
        verify_helper=lambda _path: None,
    )
    assert result["phase"] == "complete"
    assert events == ["build", ("sign", b"one-use-secret")]
    second = supervisor.execute(
        config,
        prompt_password=lambda: (_ for _ in ()).throw(AssertionError("must not prompt")),
        build_broker=lambda _config: (_ for _ in ()).throw(AssertionError("must not rebuild")),
        sign_broker=lambda *_: (_ for _ in ()).throw(AssertionError("must not resign")),
        verify_broker=verify,
        verify_helper=lambda _path: None,
    )
    assert second == result


def test_broker_public_drift_fails_before_build_sign_or_keychain_query(tmp_path):
    supervisor, config, _authority, _inventory = broker_operator_fixture(tmp_path)
    bad = supervisor.BrokerConfig(**{**config.__dict__, "signer_anchor_sha1": "0" * 40})
    events = []
    with pytest.raises(supervisor.SupervisorError, match="broker_(release_input_pin|authority_binding)"):
        supervisor.execute(
            bad,
            prompt_password=lambda: events.append("secret"),
            build_broker=lambda _config: events.append("build"),
            sign_broker=lambda *_: events.append("sign"),
            verify_broker=lambda _config: events.append("verify"),
            verify_helper=lambda _path: None,
        )
    assert events == []


@pytest.mark.parametrize("stdout,stderr,should_pass", [
    (b"", b"", True),  # absent, empty stderr PASS
    (b"", lambda p: f"Executable={p}\n".encode(), True),  # absent, expected stderr PASS
    (plistlib.dumps({}), lambda p: f"Executable={p}\n".encode(), True),  # empty XML PASS
    (plistlib.dumps({"com.apple.security.cs.allow-unsigned-executable-memory": True}), lambda p: f"Executable={p}\n".encode(), False),  # nonempty dict FAIL
    (b"<not-plist>", lambda p: f"Executable={p}\n".encode(), False),  # malformed FAIL
    (plistlib.dumps(["item"]), lambda p: f"Executable={p}\n".encode(), False),  # list FAIL
])
def test_broker_entitlements_response_cases(tmp_path, monkeypatch, stdout, stderr, should_pass):
    supervisor = load_operator("first_success_build_supervisor")
    path = tmp_path / "broker"
    path.write_bytes(b"signed-broker")
    stderr_value = stderr(path) if callable(stderr) else stderr
    monkeypatch.setattr(supervisor.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr_value))
    if should_pass:
        supervisor._verify_empty_broker_entitlements(path)
    else:
        with pytest.raises(supervisor.SupervisorError, match="broker_entitlements"):
            supervisor._verify_empty_broker_entitlements(path)


@pytest.mark.parametrize("failure_factory", [
    lambda s: s.subprocess.TimeoutExpired(cmd=[], timeout=60),
    lambda s: OSError("injected"),
])
def test_broker_entitlements_subprocess_failures(tmp_path, monkeypatch, failure_factory):
    supervisor = load_operator("first_success_build_supervisor")
    path = tmp_path / "broker"
    path.write_bytes(b"signed-broker")
    def raise_failure(*a, **k):
        raise failure_factory(supervisor)
    monkeypatch.setattr(supervisor.subprocess, "run", raise_failure)
    with pytest.raises(supervisor.SupervisorError, match="broker_entitlements"):
        supervisor._verify_empty_broker_entitlements(path)


def test_broker_entitlements_exit_failure(tmp_path, monkeypatch):
    supervisor = load_operator("first_success_build_supervisor")
    path = tmp_path / "broker"
    path.write_bytes(b"signed-broker")
    monkeypatch.setattr(supervisor.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=b"", stderr=b""))
    with pytest.raises(supervisor.SupervisorError, match="broker_entitlements"):
        supervisor._verify_empty_broker_entitlements(path)


def test_broker_entitlements_unknown_stderr(tmp_path, monkeypatch):
    supervisor = load_operator("first_success_build_supervisor")
    path = tmp_path / "broker"
    path.write_bytes(b"signed-broker")
    monkeypatch.setattr(supervisor.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"", stderr=b"unknown\n"))
    with pytest.raises(supervisor.SupervisorError, match="broker_entitlements"):
        supervisor._verify_empty_broker_entitlements(path)


def test_broker_entitlements_oversize(tmp_path, monkeypatch):
    supervisor = load_operator("first_success_build_supervisor")
    path = tmp_path / "broker"
    path.write_bytes(b"signed-broker")
    monkeypatch.setattr(supervisor.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"x" * 33000, stderr=b""))
    with pytest.raises(supervisor.SupervisorError, match="broker_entitlements"):
        supervisor._verify_empty_broker_entitlements(path)


def test_cutover_core_identity_counts_real_browser_payload_and_preserves_symlinks(tmp_path):
    cutover = load("cutover_gamja")
    cutover.load_core_recipe = fixture_recipe
    package = core_fixture(tmp_path / "package")
    recipe = fixture_recipe()
    browsers = package / "libexec/gamja/playwright-browsers"
    for name, revision in recipe.BROWSER_REVISIONS.items():
        complete = browsers / f"{name.replace('-', '_')}-{revision}/INSTALLATION_COMPLETE"
        complete.parent.mkdir(parents=True, exist_ok=True)
        complete.write_bytes(b"complete")
    link = browsers / "linked-notices"
    link.symlink_to("../../../THIRD-PARTY-NOTICES.txt")
    recipe.write_manifest(package, target="aarch64-apple-darwin", version="1.0.0", provenance={})
    identity = cutover.core_identity(package)
    manifest = json.loads((package / "core-manifest.json").read_text())
    assert manifest["files"][link.relative_to(package).as_posix()] == {
        "symlink": "../../../THIRD-PARTY-NOTICES.txt"}
    expected_bytes = sum(path.stat().st_size for path in package.rglob("*")
                         if path.is_file() and not path.is_symlink()
                         and path.name != "core-manifest.json")
    assert identity["payload_bytes"] == expected_bytes
    assert identity["file_count"] == len(manifest["files"])
    assert identity["tree_sha256"] == cutover.tree_sha256(package)
    link.unlink()
    link.symlink_to("../../../bin/gamja")
    with pytest.raises(ValueError, match="manifest/hash coverage mismatch"):
        cutover.core_identity(package)
