"""Source recipe safety checks; they do not substitute for target-native builds."""

import importlib.util
import hashlib
import json
import os
import plistlib
import stat
import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def pin_desktop_fixture(monkeypatch, desktop, root):
    monkeypatch.setattr(
        desktop,
        "FIXED_BOOTSTRAP_MANIFEST_SHA256",
        desktop.sha256(root / "core-manifest.json"),
    )
    monkeypatch.setattr(
        desktop, "FIXED_BOOTSTRAP_CORE_SHA256", desktop.sha256(root / "bin/gamja")
    )
    monkeypatch.setattr(
        desktop,
        "FIXED_BOOTSTRAP_ENGINE_SHA256",
        desktop.sha256(root / "libexec/gamja/gamja-engine"),
    )
    helper = root / "libexec/gamja/gamja-platform-helper"
    monkeypatch.setattr(
        desktop, "FIXED_BOOTSTRAP_HELPER_SHA256",
        desktop.sha256(helper) if helper.is_file() else None,
    )


def test_signing_password_fd_is_bounded_nonempty_and_closed():
    recipe = core_recipe()
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"synthetic-password")
    os.close(write_fd)
    assert recipe.read_secret_fd(read_fd) == bytearray(b"synthetic-password")
    with pytest.raises(OSError):
        os.read(read_fd, 1)

    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    with pytest.raises(ValueError, match="empty"):
        recipe.read_secret_fd(read_fd)

    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"x" * 4096)
    os.close(write_fd)
    with pytest.raises(ValueError, match="too large"):
        recipe.read_secret_fd(read_fd)


def test_screen_helper_release_updates_only_manifest_pin():
    spec = importlib.util.spec_from_file_location(
        "build_desktop", ROOT / "scripts/build_desktop.py"
    )
    desktop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(desktop)
    manifest = "c7db6f970b456e75eb23359dfdbe79e2aa1cb16c4c72eed5649bae3a3a11b27f"
    core = "b885e14bbacfdcbe7c68e4ff051db2d33ade88733d69471897fe4ed55da10a31"
    engine = "2f6b534d61ef0212248ec76b6f92b9a9e5dc9e15dae21622c8ab785fa2bfded0"
    rust = (ROOT / "frontend/src-tauri/src/core_runtime.rs").read_text()
    shell = (ROOT / "scripts/package_macos_dmg.sh").read_text()

    assert desktop.FIXED_BOOTSTRAP_MANIFEST_SHA256 == manifest
    assert desktop.FIXED_BOOTSTRAP_CORE_SHA256 == core
    assert desktop.FIXED_BOOTSTRAP_ENGINE_SHA256 == engine
    build = (ROOT / "frontend/src-tauri/build.rs").read_text()
    assert 'env!("GAMJA_CORE_MANIFEST_SHA256")' in rust
    assert 'env!("GAMJA_CORE_SHA256")' in rust
    assert 'env!("GAMJA_ENGINE_SHA256")' in rust
    assert manifest in build and core in build and engine in build
    assert f"fixed_manifest={manifest}" in shell
    assert f"fixed_core={core}" in shell
    assert f"fixed_engine={engine}" in shell


def test_failed_dmg_verification_keeps_previous_candidate(tmp_path):
    app = tmp_path / "Gamja Bot.app"
    binary = app / "Contents/MacOS/backend"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"sidecar")
    output = tmp_path / "output/Gamja.dmg"
    output.parent.mkdir()
    output.write_bytes(b"previous verified candidate")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "codesign").write_text("#!/bin/sh\nexit 0\n")
    (stubs / "ditto").write_text("#!/bin/sh\ncp -R \"$1\" \"$2\"\n")
    (stubs / "hdiutil").write_text(
        "#!/bin/sh\n"
        "case $1 in\n"
        "  create) while [ $# -gt 1 ]; do [ \"$1\" = -ov ] && { shift; printf new > \"$1\"; exit 0; }; shift; done;;\n"
        "  attach) exit 42;;\n"
        "  detach) exit 0;;\n"
        "esac\nexit 1\n"
    )
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    result = subprocess.run(["bash", str(ROOT / "scripts/package_macos_dmg.sh"), str(app), str(output)],
                            env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "TMPDIR": str(tmp_path)},
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert output.read_bytes() == b"previous verified candidate"
    assert not list(output.parent.glob(".gamja-dmg.*"))
    assert not list(tmp_path.glob("gamja-dmg-stage.*"))
    assert not list(tmp_path.glob("gamja-dmg-mount.*"))
    assert not (app / "Contents/Resources/Diagnostics Help.txt").exists()


def test_dmg_core_requires_complete_lowercase_hash_set_before_external_tools(tmp_path):
    module = core_recipe()
    core = core_fixture(tmp_path / "alternate")
    module.write_manifest(
        core, target="aarch64-apple-darwin", version="1.0.0", provenance={}
    )
    app = tmp_path / "Fixture.app"
    output = tmp_path / "Fixture.dmg"
    marker = tmp_path / "external-tool-ran"
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name in ("codesign", "ditto", "hdiutil", "shasum"):
        stub = stubs / name
        stub.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 90\n')
        stub.chmod(0o755)
    base = [
        "bash",
        str(ROOT / "scripts/package_macos_dmg.sh"),
        str(app),
        str(output),
        str(core),
        "aarch64-apple-darwin",
    ]
    environment = {
        **os.environ,
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
    }
    missing = subprocess.run(base, env=environment, capture_output=True, text=True)
    alternate = subprocess.run(
        base
        + [
            module.sha256(core / "core-manifest.json"),
            module.sha256(core / "bin/gamja"),
            module.sha256(core / "libexec/gamja/gamja-engine"),
        ],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert missing.returncode != 0
    assert alternate.returncode != 0
    assert "exact core and artifact hashes are required" in missing.stderr
    assert "exact core and artifact hashes are required" in alternate.stderr
    assert not marker.exists()
    assert not list(tmp_path.glob("gamja-dmg-stage.*"))
    assert not list(tmp_path.glob("gamja-dmg-mount.*"))

    generic = subprocess.run(
        base + ["1" * 64, "2" * 64, "3" * 64, "4" * 64, "5" * 64],
        env=environment, capture_output=True, text=True,
    )
    assert generic.returncode != 0
    assert "exact core and artifact hashes are required" not in generic.stderr
    assert "missing app" in generic.stderr


def test_windows_recipe_resolves_npm_cmd(monkeypatch):
    spec = importlib.util.spec_from_file_location("build_desktop", ROOT / "scripts/build_desktop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    found = []
    monkeypatch.setattr(module.shutil, "which", lambda name: found.append(name) or "C:/Node/npm.cmd")
    assert module.npm_executable() == "C:/Node/npm.cmd"
    assert found == ["npm.cmd"]


def test_windows_workflow_smokes_installed_nsis_payload():
    workflow = (ROOT / ".github/workflows/windows-installer.yml").read_text()

    assert "Smoke installed NSIS payload" in workflow
    assert "'backend.exe'" in workflow
    assert "$browsers = Join-Path $installRoot 'playwright-browsers'" in workflow
    assert "chrome-win64/chrome.exe" in workflow
    assert "chrome-headless-shell-win64/chrome-headless-shell.exe" in workflow
    assert "Get-CimInstance Win32_Process" in workflow
    assert "$installPrefix" in workflow
    assert "NoProcessFoundForGivenId" in workflow
    assert "Unverified installer evidence" in workflow
    assert "& $sidecar --package-smoke" in workflow
    assert "Uninstall.exe" in workflow


def test_windows_workflow_smokes_frozen_sidecar_before_nsis_bundle():
    workflow = (ROOT / ".github/workflows/windows-installer.yml").read_text()

    assert workflow.index("Build and stage frozen sidecar") < workflow.index("Smoke frozen sidecar with bundled browser")
    assert workflow.index("Smoke frozen sidecar with bundled browser") < workflow.index("Build native NSIS installer")


def test_windows_workflow_builds_verified_python_31115_source_before_use():
    workflow = (ROOT / ".github/workflows/windows-installer.yml").read_text()

    assert "python-version: '3.11.15'" not in workflow
    assert "python-version: '3.11.9'" in workflow
    assert "Set up bootstrap Python 3.11.9 (build tooling only)" in workflow
    assert "$env:HOST_PYTHON = $bootstrapPython.Source" in workflow
    assert "https://www.python.org/ftp/python/3.11.15/Python-3.11.15.tgz" in workflow
    assert "f4de1b10bd6c70cbb9fa1cd71fc5038b832747a74ee59d599c69ce4846defb50" in workflow
    assert workflow.index("Get-FileHash") < workflow.index("tar -xzf")
    assert "PCbuild/build.bat') -p x64 -c Release -e" in workflow
    assert "sys.version_info[:3] == (3, 11, 15)" in workflow
    assert "platform.machine() == 'AMD64'" in workflow
    assert "sysconfig.get_platform() == 'win-amd64'" in workflow
    assert "import ctypes, ensurepip, platform, sqlite3, ssl, sys, sysconfig, venv" in workflow
    assert "Join-Path $env:RUNNER_TEMP 'gamja-python-3.11.15'" in workflow
    assert "Join-Path $venv 'Scripts'" in workflow
    assert "Out-File -FilePath $env:GITHUB_PATH" in workflow
    assert workflow.index("Bootstrap CPython 3.11.15 from verified source") < workflow.index("Install locked Python dependencies")

    assert "workflow_dispatch:" in workflow
    assert workflow.count("runs-on:") == 1
    assert "runs-on: windows-2022" in workflow
    assert "timeout-minutes: 90" in workflow
    assert "contents: read" in workflow
    assert "Native private-DACL concurrent-start and frozen-core gates" in workflow
    assert "Smoke installed NSIS payload" in workflow


def test_windows_workflow_core_only_is_fail_closed_and_skips_legacy_installer():
    workflow = (ROOT / ".github/workflows/windows-installer.yml").read_text()

    assert "core_only:" in workflow
    assert "type: boolean" in workflow
    assert "default: false" in workflow
    assert "Require successful Core path for core-only run" in workflow
    assert "inputs.core_only && (steps.core_build.outcome != 'success' || steps.core_gates.outcome != 'success')" in workflow

    endpoint = "$endpoint = Join-Path $fixture 'owner-cli.json'"
    gate_try = workflow.index("          try {", workflow.index("Native private-DACL concurrent-start and frozen-core gates"))
    assert workflow.index(endpoint) < gate_try
    assert "if (Test-Path -LiteralPath $endpoint) {\n              & $core stop --app-data $fixture\n            }" in workflow
    assert "if ($LASTEXITCODE -ne 0 -or (Test-Path -LiteralPath $endpoint)) { throw 'Explicit core stop/endpoint cleanup failed' }" in workflow

    for name in [
        "Set up Node for legacy installer",
        "Install locked frontend dependencies",
        "Build and stage frozen sidecar",
        "Smoke frozen sidecar with bundled browser",
        "Build native NSIS installer",
        "Smoke installed NSIS payload",
        "Stage installer and provenance",
        "Upload legacy Windows installer candidate",
        "Preserve Unverified installer evidence",
        "Upload Unverified installer evidence",
    ]:
        step = workflow.index(f"- name: {name}")
        assert "if: ${{ !inputs.core_only }}" in workflow[step:step + 260] or "if: ${{ failure() && !inputs.core_only }}" in workflow[step:step + 260]


def test_frozen_recipe_collects_asyncio_submodules():
    spec = (ROOT / "backend/backend.spec").read_text()

    assert 'collect_submodules("asyncio")' in spec


def test_windows_frozen_smoke_environment_keeps_system_root_and_scopes_temp(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("smoke_frozen", ROOT / "scripts/smoke_frozen.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "os", SimpleNamespace(environ=os.environ, name="nt"))
    monkeypatch.setenv("SystemRoot", r"C:\\Windows")
    monkeypatch.setenv("WINDIR", r"C:\\Windows")

    env = module.process_env(tmp_path, tmp_path / "browsers")

    assert env["SystemRoot"] == r"C:\\Windows"
    assert env["WINDIR"] == r"C:\\Windows"
    assert env["TEMP"] == str(tmp_path)
    assert env["TMP"] == str(tmp_path)
    assert env["USERPROFILE"] == str(tmp_path)


def test_frozen_smoke_cleanup_retries_a_transient_windows_lock(monkeypatch):
    spec = importlib.util.spec_from_file_location("smoke_frozen", ROOT / "scripts/smoke_frozen.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Temporary:
        calls = 0

        def cleanup(self):
            self.calls += 1
            if self.calls == 1:
                raise PermissionError

    temporary = Temporary()
    monkeypatch.setattr(module.time, "monotonic", lambda: 0)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    module.cleanup_temporary(temporary)

    assert temporary.calls == 2


def test_windows_frozen_smoke_stops_only_its_backend_process_tree(monkeypatch):
    spec = importlib.util.spec_from_file_location("smoke_frozen", ROOT / "scripts/smoke_frozen.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    calls = []
    monkeypatch.setattr(module.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)))

    class Backend:
        pid = 321

        def wait(self, timeout):
            calls.append(("wait", timeout))

    module.stop_backend(Backend())

    assert calls[0][0] == ["taskkill", "/PID", "321", "/T", "/F"]
    assert calls[1] == ("wait", 5)


def test_frozen_smoke_closes_the_migration_database_connection(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("smoke_frozen", ROOT / "scripts/smoke_frozen.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    closed = []

    class Connection:
        def execute(self, query):
            assert query == "SELECT MAX(version) FROM schema_version"
            return SimpleNamespace(fetchone=lambda: (22,))

        def close(self):
            closed.append(True)

    monkeypatch.setattr(module.sqlite3, "connect", lambda _: Connection())

    module.verify_migrations(tmp_path / "gamja_bot.db")

    assert closed == [True]


def core_recipe(*, verify_signatures=False, monkeypatch=None):
    path = ROOT / "scripts/build_core.py"
    assert path.is_file(), "standalone core recipe is missing"
    spec = importlib.util.spec_from_file_location("build_core", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not verify_signatures:
        module.verify_macos_signature = lambda *_args, **_kwargs: None
    if monkeypatch is not None:
        monkeypatch.setitem(sys.modules, "build_core", module)
    return module


def core_fixture(tmp_path):
    root = tmp_path / "gamja-core"
    for relative, content in {
        "bin/gamja": b"host", "libexec/gamja/gamja-engine": b"engine",
        "libexec/gamja/playwright-browsers/chromium-1208/INSTALLATION_COMPLETE": b"",
        "libexec/gamja/playwright-browsers/chromium_headless_shell-1208/INSTALLATION_COMPLETE": b"",
        "THIRD-PARTY-NOTICES.txt": b"licenses",
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def operator_runtime_verifier(tmp_path, content=b"operator-built-canonical-verifier"):
    verifier = tmp_path / "operator-tools/gamja-runtime-verifier"
    verifier.parent.mkdir(parents=True)
    verifier.write_bytes(content)
    verifier.chmod(0o700)
    return verifier


def test_runtime_receipt_uses_independent_canonical_cli_with_exact_package_pins(
        monkeypatch, tmp_path):
    module = core_recipe()
    root = core_fixture(tmp_path)
    manifest = root / module.MANIFEST
    manifest.write_text("{}")
    verifier = operator_runtime_verifier(tmp_path)
    observed = []

    def invoke(argv, **kwargs):
        executable = Path(argv[0])
        observed.append((
            executable,
            executable.read_bytes(),
            argv[1:],
            kwargs,
        ))
        return 0, b'{"version":1,"operation":"verify","status":"valid"}\n', b""

    monkeypatch.setattr(module, "_run_bounded_core_cli", invoke)
    module.verify_runtime_receipt_with_core_cli(
        root,
        "aarch64-apple-darwin",
        verifier=verifier,
        verifier_sha256=module.sha256(verifier),
    )

    assert len(observed) == 1
    executable, content, arguments, keyword_arguments = observed[0]
    assert executable == verifier.resolve()
    assert content == b"operator-built-canonical-verifier"
    assert arguments == [
        "runtime", "verify", "--source", str(root.resolve()),
        "--manifest-sha256", module.sha256(manifest),
        "--core-sha256", module.sha256(root / "bin/gamja"),
        "--engine-sha256", module.sha256(root / "libexec/gamja/gamja-engine"),
    ]
    assert "pass_fds" not in keyword_arguments


def test_runtime_receipt_rejects_noncanonical_cli_output(monkeypatch, tmp_path):
    module = core_recipe()
    root = core_fixture(tmp_path)
    (root / module.MANIFEST).write_text("{}")
    verifier = operator_runtime_verifier(tmp_path)
    monkeypatch.setattr(
        module,
        "_run_bounded_core_cli",
        lambda _argv, **_kwargs: (
            0,
            b'{"version":1,"operation":"verify","status":"valid","receipt":{}}',
            b"",
        ),
    )

    with pytest.raises(ValueError, match="response invalid"):
        module.verify_runtime_receipt_with_core_cli(
            root,
            "aarch64-apple-darwin",
            verifier=verifier,
            verifier_sha256=module.sha256(verifier),
        )


def test_runtime_receipt_executes_only_the_signature_verified_operator_verifier(
        monkeypatch, tmp_path):
    module = core_recipe(verify_signatures=True)
    root = core_fixture(tmp_path)
    verifier = operator_runtime_verifier(tmp_path)
    module.write_manifest(
        root,
        target="aarch64-apple-darwin",
        version="1.0.0",
        provenance={},
    )
    signed = []
    executed = []

    def verify_signature(path, identifier):
        signed.append((Path(path), identifier, Path(path).read_bytes()))

    def invoke(argv, **kwargs):
        executable = Path(argv[0])
        assert executable == verifier.resolve()
        assert executable.read_bytes() == b"operator-built-canonical-verifier"
        assert argv[4] == str(root.resolve())
        assert "pass_fds" not in kwargs
        executed.append(executable)
        return 0, b'{"version":1,"operation":"verify","status":"valid"}\n', b""

    monkeypatch.setattr(module, "verify_macos_signature", verify_signature)
    monkeypatch.setattr(module, "_run_bounded_core_cli", invoke)
    module.verify_package(
        root,
        "aarch64-apple-darwin",
        allow_runtime_receipt=True,
        runtime_verifier=verifier,
        runtime_verifier_sha256=module.sha256(verifier),
    )

    assert [(identifier, content) for _path, identifier, content in signed] == [
        (module.MAC_CORE_IDENTIFIER, b"host"),
        (module.MAC_ENGINE_IDENTIFIER, b"engine"),
        (module.MAC_CORE_IDENTIFIER, b"operator-built-canonical-verifier"),
    ]
    assert signed[2][0] == executed[0]


def test_runtime_receipt_never_executes_candidate_core(monkeypatch, tmp_path):
    module = core_recipe()
    root = core_fixture(tmp_path)
    candidate_marker = tmp_path / "candidate-executed"
    candidate = root / "bin/gamja"
    candidate.write_text(f"#!/bin/sh\ntouch '{candidate_marker}'\n")
    candidate.chmod(0o700)
    module.write_manifest(
        root,
        target="aarch64-apple-darwin",
        version="1.0.0",
        provenance={},
    )
    verifier = operator_runtime_verifier(
        tmp_path,
        b'#!/bin/sh\nprintf \'%s\\n\' \'{"version":1,"operation":"verify","status":"valid"}\'\n',
    )

    module.verify_package(
        root,
        "aarch64-apple-darwin",
        require_credential_signature=False,
        allow_runtime_receipt=True,
        runtime_verifier=verifier,
        runtime_verifier_sha256=module.sha256(verifier),
    )

    assert not candidate_marker.exists()


def test_runtime_receipt_fails_closed_without_independent_verifier(tmp_path):
    module = core_recipe()
    root = core_fixture(tmp_path)
    module.write_manifest(
        root,
        target="aarch64-apple-darwin",
        version="1.0.0",
        provenance={},
    )

    with pytest.raises(ValueError, match="independent runtime verifier is required"):
        module.verify_package(
            root,
            "aarch64-apple-darwin",
            require_credential_signature=False,
            allow_runtime_receipt=True,
        )


def test_runtime_receipt_rejects_verifier_from_candidate_tree(tmp_path):
    module = core_recipe()
    root = core_fixture(tmp_path)
    module.write_manifest(
        root,
        target="aarch64-apple-darwin",
        version="1.0.0",
        provenance={},
    )
    verifier = root / "bin/gamja"

    with pytest.raises(ValueError, match="independent"):
        module.verify_package(
            root,
            "aarch64-apple-darwin",
            require_credential_signature=False,
            allow_runtime_receipt=True,
            runtime_verifier=verifier,
            runtime_verifier_sha256=module.sha256(verifier),
        )


def test_runtime_receipt_rejects_unpinned_operator_verifier(monkeypatch, tmp_path):
    module = core_recipe()
    root = core_fixture(tmp_path)
    module.write_manifest(
        root,
        target="aarch64-apple-darwin",
        version="1.0.0",
        provenance={},
    )
    verifier = operator_runtime_verifier(tmp_path)
    monkeypatch.setattr(
        module,
        "_run_bounded_core_cli",
        lambda *_args, **_kwargs: pytest.fail("unpinned verifier must not execute"),
    )

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        module.verify_package(
            root,
            "aarch64-apple-darwin",
            require_credential_signature=False,
            allow_runtime_receipt=True,
            runtime_verifier=verifier,
            runtime_verifier_sha256="0" * 64,
        )


def test_runtime_receipt_cli_requires_and_forwards_operator_verifier(
        monkeypatch, tmp_path, capsys):
    module = core_recipe()
    root = tmp_path / "candidate"
    root.mkdir()
    verifier = operator_runtime_verifier(tmp_path)
    verifier_hash = module.sha256(verifier)
    observed = []
    monkeypatch.setattr(
        module,
        "verify_package",
        lambda *args, **kwargs: observed.append((args, kwargs)) or {},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_core.py",
            "--verify", str(root),
            "--verify-runtime-receipt",
            "--runtime-verifier", str(verifier),
            "--runtime-verifier-sha256", verifier_hash,
            "--target", "aarch64-apple-darwin",
        ],
    )

    module.main()

    assert observed == [
        (
            (root, "aarch64-apple-darwin"),
            {
                "allow_runtime_receipt": True,
                "runtime_verifier": verifier,
                "runtime_verifier_sha256": verifier_hash,
            },
        )
    ]
    assert "package layout and all hashes verified" in capsys.readouterr().out

    observed.clear()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_core.py",
            "--verify", str(root),
            "--verify-runtime-receipt",
            "--target", "aarch64-apple-darwin",
        ],
    )
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
    assert observed == []


def test_full_build_operator_verifier_is_distinct_bounded_and_sibling(tmp_path):
    module = core_recipe()
    output = tmp_path / "out/core"
    output.parent.mkdir()
    verifier = output.with_name("core.runtime-verifier")
    candidates = [(output, "first-success-candidate")]

    assert module.operator_verifier_request(
        output,
        "aarch64-apple-darwin",
        candidates,
        verifier,
        "first-success-verifier",
    ) == (verifier.absolute(), "first-success-verifier")

    with pytest.raises(ValueError, match="both output and build ID"):
        module.operator_verifier_request(
            output, "aarch64-apple-darwin", candidates, verifier, None
        )
    with pytest.raises(ValueError, match="distinct"):
        module.operator_verifier_request(
            output,
            "aarch64-apple-darwin",
            candidates,
            verifier,
            "first-success-candidate",
        )
    with pytest.raises(ValueError, match="sibling"):
        module.operator_verifier_request(
            output,
            "aarch64-apple-darwin",
            candidates,
            tmp_path / "elsewhere/verifier",
            "first-success-verifier",
        )


def test_operator_verifier_publication_is_exclusive_and_fsynced(monkeypatch, tmp_path):
    module = core_recipe()
    source = tmp_path / "stage"
    destination = tmp_path / "verifier"
    source.write_bytes(b"verified")
    source.chmod(0o700)
    synced = []
    monkeypatch.setattr(module, "fsync_directory", lambda path: synced.append(Path(path)))

    module.publish_exclusive_file(source, destination)

    assert destination.read_bytes() == b"verified"
    assert not source.exists()
    assert synced == [tmp_path]
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"attacker")
    with pytest.raises(ValueError, match="already exists"):
        module.publish_exclusive_file(replacement, destination)
    assert destination.read_bytes() == b"verified"


def test_full_build_cli_forwards_operator_verifier_registration(monkeypatch, tmp_path):
    module = core_recipe()
    output = tmp_path / "core"
    verifier = tmp_path / "core.runtime-verifier"
    calls = []
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(module, "validate_supervised_signing", lambda *_args: True)
    monkeypatch.setattr(
        module, "build", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setattr(sys, "argv", [
        "build_core.py",
        "--output", str(output),
        "--target", "aarch64-apple-darwin",
        "--build-id", "first-success-candidate",
        "--runtime-verifier-output", str(verifier),
        "--runtime-verifier-build-id", "first-success-verifier",
        "--engine-signing-password-fd", "31",
        "--core-signing-password-fd", "32",
        "--supervised-signing-parent-pid", "100",
    ])

    module.main()

    assert calls[0][0] == (output,)
    assert calls[0][1]["runtime_verifier_output"] == verifier
    assert calls[0][1]["runtime_verifier_build_id"] == "first-success-verifier"
    assert calls[0][1]["supervised_signing"] is True


def test_runtime_receipt_rejects_signature_to_execution_candidate_replacement(
        monkeypatch, tmp_path):
    module = core_recipe(verify_signatures=True)
    root = core_fixture(tmp_path)
    verifier = operator_runtime_verifier(tmp_path)
    module.write_manifest(
        root,
        target="aarch64-apple-darwin",
        version="1.0.0",
        provenance={},
    )
    invoked = []

    def verify_signature(_path, identifier):
        if identifier == module.MAC_ENGINE_IDENTIFIER:
            (root / "bin/gamja").write_bytes(b"unsigned-replacement")

    monkeypatch.setattr(module, "verify_macos_signature", verify_signature)
    monkeypatch.setattr(
        module,
        "_run_bounded_core_cli",
        lambda argv, **_kwargs: invoked.append(argv) or (
            0,
            b'{"version":1,"operation":"verify","status":"valid"}\n',
            b"",
        ),
    )

    with pytest.raises(ValueError, match="candidate changed"):
        module.verify_package(
            root,
            "aarch64-apple-darwin",
            allow_runtime_receipt=True,
            runtime_verifier=verifier,
            runtime_verifier_sha256=module.sha256(verifier),
        )
    assert invoked == []


def test_canonical_runtime_cli_output_is_bounded_and_timeout_is_finite(monkeypatch):
    module = core_recipe()
    with pytest.raises(ValueError, match="output exceeded limit"):
        module._run_bounded_core_cli([
            sys.executable,
            "-c",
            "import os; os.write(1, b'x' * 4097)",
        ])

    monkeypatch.setattr(module, "RUNTIME_CLI_TIMEOUT", 0.01)
    with pytest.raises(ValueError, match="timed out"):
        module._run_bounded_core_cli([
            sys.executable,
            "-c",
            "import time; time.sleep(2)",
        ])


def test_core_build_input_inventory_is_complete_and_deterministic():
    module = core_recipe()

    first = module.build_input_paths(with_helper=True, macos=True)
    second = module.build_input_paths(with_helper=True, macos=True)

    assert first == second == sorted(set(first))
    for critical in (
        "scripts/build_core.py",
        "core-host/macos-credential-core.cer",
        "backend/gamja-engine.spec",
        "backend/requirements.txt",
        "backend/requirements-build.txt",
        "backend/run.py",
        "core-host/Cargo.toml",
        "core-host/Cargo.lock",
        "core-host/build.rs",
        "core-host/src/upgrade_native.c",
        "core-host/src/upgrade_login.rs",
        "backend/app/services/core_upgrade_login.py",
        "platform-helper/Cargo.toml",
        "platform-helper/Cargo.lock",
        "core-host/src/credential.rs",
        "backend/app/services/secrets.py",
        "platform-helper/src/main.rs",
    ):
        assert critical in first

    signer_path = ROOT / "reporting/ops/proposed/credential_release_signer.py"
    signer_spec = importlib.util.spec_from_file_location("credential_release_signer_inventory_test", signer_path)
    signer = importlib.util.module_from_spec(signer_spec)
    signer_spec.loader.exec_module(signer)
    signer_only = {
        "reporting/ops/proposed/credential_release_signer.py",
        "reporting/ops/proposed/credential_keychain_helper.c",
        "reporting/ops/proposed/credential_release_restore.py",
    }
    assert signer.expected_source_files() == set(first) | signer_only


def test_macos_core_signing_uses_exact_native_helper_then_public_verification(monkeypatch, tmp_path):
    module = core_recipe(verify_signatures=True)
    keychain = tmp_path / "private.keychain-db"
    signer = tmp_path / "exact-signer"
    certificate = tmp_path / "credential-core.cer"
    binary = tmp_path / "gamja"
    for path, value in ((keychain, b"keychain"), (signer, b"signer"),
                        (certificate, b"certificate"), (binary, b"core")):
        path.write_bytes(value)
        path.chmod(0o600)
    monkeypatch.setattr(module, "MAC_CORE_CERTIFICATE", certificate)
    monkeypatch.setattr(module, "validate_macos_signing_inputs",
                        lambda: (keychain, signer, hashlib.sha256(b"signer").hexdigest()))
    events = []

    def run(arguments, password, expected_stdout):
        events.append((arguments, bytes(password), expected_stdout))

    monkeypatch.setattr(module, "_run_macos_signer", run)
    monkeypatch.setattr(module, "verify_macos_signature",
                        lambda path, identifier: events.append(("verify", path, identifier)))
    password = bytearray(b"private-password")
    module.sign_macos_binary(binary, module.MAC_CORE_IDENTIFIER, password)

    arguments, secret, expected = events[0]
    assert arguments == [str(signer), "sign", "core", str(keychain), str(certificate),
                         "{password_fd}", hashlib.sha256(b"signer").hexdigest(), str(binary.absolute())]
    assert secret == b"private-password"
    assert expected == "signed-core-exact-identity:1\n"
    assert events[1] == ("verify", binary, module.MAC_CORE_IDENTIFIER)
    assert password == bytearray(len(password))


@pytest.mark.parametrize(
    ("constant", "identifier", "mode"),
    [
        ("MAC_BROKER_IDENTIFIER", "top.gamja.credential-broker.v1", "broker"),
        (
            "MAC_RELEASE_AUTHORITY_IDENTIFIER",
            "top.gamja.credential-release-authority.v1",
            "release-authority",
        ),
    ],
)
def test_stable_credential_signing_modes_are_exact_and_use_the_pinned_requirement(
    monkeypatch, tmp_path, constant, identifier, mode
):
    module = core_recipe(verify_signatures=True)
    keychain = tmp_path / "private.keychain-db"
    signer = tmp_path / "exact-signer"
    certificate = tmp_path / "credential-core.cer"
    binary = tmp_path / mode
    for path, value in (
        (keychain, b"keychain"),
        (signer, b"signer"),
        (certificate, b"certificate"),
        (binary, b"unsigned"),
    ):
        path.write_bytes(value)
        path.chmod(0o600)
    monkeypatch.setattr(module, "MAC_CORE_CERTIFICATE", certificate)
    monkeypatch.setattr(
        module,
        "validate_macos_signing_inputs",
        lambda: (keychain, signer, hashlib.sha256(b"signer").hexdigest()),
    )
    events = []
    monkeypatch.setattr(
        module,
        "_run_macos_signer",
        lambda arguments, password, expected_stdout: events.append(
            (arguments, bytes(password), expected_stdout)
        ),
    )
    monkeypatch.setattr(
        module,
        "verify_macos_signature",
        lambda path, actual_identifier: events.append(
            ("verify", path, actual_identifier)
        ),
    )

    assert getattr(module, constant) == identifier
    requirement = module.mac_designated_requirement(identifier)
    assert requirement == (
        f'designated => anchor = H"{module.mac_signing_fingerprint()}" '
        f'and identifier "{identifier}"'
    )
    password = bytearray(b"private-password")
    module.sign_macos_binary(binary, identifier, password)

    arguments, secret, expected = events[0]
    assert arguments == [
        str(signer),
        "sign",
        mode,
        str(keychain),
        str(certificate),
        "{password_fd}",
        hashlib.sha256(b"signer").hexdigest(),
        str(binary.absolute()),
    ]
    assert secret == b"private-password"
    assert expected == f"signed-{mode}-exact-identity:1\n"
    assert events[1] == ("verify", binary, identifier)
    assert password == bytearray(len(password))


def test_stable_credential_signing_modes_do_not_open_a_free_form_requirement():
    module = core_recipe()
    with pytest.raises(ValueError, match="unapproved macOS signing identifier"):
        module.mac_designated_requirement("true")
    with pytest.raises(ValueError, match="unapproved macOS signing identifier"):
        module.mac_designated_requirement("top.gamja.credential-broker.v2")

    signer_source = (ROOT / "scripts/macos_exact_signer.c").read_text()
    assert 'strcmp(mode, "broker") == 0' in signer_source
    assert 'identifier = "top.gamja.credential-broker.v1"' in signer_source
    assert 'strcmp(mode, "release-authority") == 0' in signer_source
    assert 'identifier = "top.gamja.credential-release-authority.v1"' in signer_source
    assert "signed-broker-exact-identity:1" in signer_source
    assert "signed-release-authority-exact-identity:1" in signer_source


def test_supervised_signing_requires_exact_parent_group_and_dedicated_descriptors(monkeypatch):
    module = core_recipe()
    assert module.validate_supervised_signing(None, "unsupported", None, None) is False
    engine_read, engine_write = os.pipe()
    core_read, core_write = os.pipe()
    try:
        monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(module.platform, "machine", lambda: "arm64")
        monkeypatch.setattr(module.os, "getpid", lambda: 700)
        monkeypatch.setattr(module.os, "getppid", lambda: 600)
        monkeypatch.setattr(module.os, "getsid", lambda _pid: 700)
        monkeypatch.setattr(module.os, "getpgrp", lambda: 700)
        assert module.validate_supervised_signing(
            600, "aarch64-apple-darwin", engine_read, core_read
        ) is True
        with pytest.raises(ValueError, match="invalid supervised signing context"):
            module.validate_supervised_signing(
                601, "aarch64-apple-darwin", engine_read, core_read
            )
        monkeypatch.setattr(module.os, "getsid", lambda _pid: 699)
        with pytest.raises(ValueError, match="invalid supervised signing context"):
            module.validate_supervised_signing(
                600, "aarch64-apple-darwin", engine_read, core_read
            )
    finally:
        for descriptor in (engine_read, engine_write, core_read, core_write):
            os.close(descriptor)


def test_macos_native_signer_failure_clears_password_and_skips_verification(monkeypatch, tmp_path):
    module = core_recipe(verify_signatures=True)
    binary = tmp_path / "gamja"
    binary.write_bytes(b"core")
    monkeypatch.setattr(module, "validate_macos_signing_inputs",
                        lambda: (tmp_path / "keychain", tmp_path / "signer", "a" * 64))
    monkeypatch.setattr(module, "_run_macos_signer",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("native signer failed")))
    verified = []
    monkeypatch.setattr(module, "verify_macos_signature", lambda *_: verified.append(True))
    password = bytearray(b"password")

    with pytest.raises(ValueError, match="native signer failed"):
        module.sign_macos_binary(binary, module.MAC_CORE_IDENTIFIER, password)
    assert password == bytearray(len(password))
    assert verified == []


def test_macos_engine_constraint_compiles_before_final_spi_sign_and_hash(monkeypatch, tmp_path):
    module = core_recipe(verify_signatures=True)
    engine = tmp_path / "gamja-engine"
    engine.write_bytes(b"unsigned-engine")
    events = []
    slots = iter([[b"compiled-slot"], [b"compiled-slot"]])

    def run(arguments, **kwargs):
        events.append(arguments)
        if arguments[0] == "/usr/bin/vtool":
            output = Path(arguments[arguments.index("-output") + 1])
            output.write_bytes(engine.read_bytes() + b"-macos14")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def sign(path, identifier, password):
        events.append(["spi-sign", str(path), identifier])
        path.write_bytes(path.read_bytes() + b"-signed")
        module.clear_secret(password)

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module, "_require_system_dependencies_only",
                        lambda path: events.append(["system-deps", str(path)]))
    monkeypatch.setattr(module.MAC_CONSTRAINTS, "constraint_slots_from_thin_macho",
                        lambda _path: next(slots))
    monkeypatch.setattr(module, "sign_macos_binary", sign)
    password = bytearray(b"password")
    digest = module.prepare_macos_engine(engine, password, ["11" * 20], tmp_path)

    validate_index = next(i for i, event in enumerate(events) if "--validate-constraint" in event)
    compile_index = next(i for i, event in enumerate(events) if "--library-constraint" in event)
    sign_index = next(i for i, event in enumerate(events) if event[0] == "spi-sign")
    assert validate_index < compile_index < sign_index
    assert plistlib.loads((tmp_path / "engine-entitlements.plist").read_bytes()) == {
        "com.apple.security.cs.disable-library-validation": True}
    assert plistlib.loads((tmp_path / "engine-library-constraint.plist").read_bytes()) == {
        "cdhash": {"$in": [bytes.fromhex("11" * 20)]}}
    assert digest == hashlib.sha256(engine.read_bytes()).hexdigest()
    assert password == bytearray(len(password))


def test_macos_package_verification_requires_the_credential_signature_by_default(monkeypatch, tmp_path):
    module = core_recipe(verify_signatures=True)
    root = core_fixture(tmp_path)
    module.write_manifest(root, target='aarch64-apple-darwin', version='1.0.0', provenance={})
    monkeypatch.setattr(module, 'verify_macos_signature', lambda *_: (_ for _ in ()).throw(ValueError('credential signature invalid')))

    with pytest.raises(ValueError, match='signature'):
        module.verify_package(root, 'aarch64-apple-darwin')
    assert module.verify_package(root, 'aarch64-apple-darwin', require_credential_signature=False)['target'] == 'aarch64-apple-darwin'


def test_core_notices_use_utf8_under_legacy_default_encoding(monkeypatch, tmp_path):
    module = core_recipe()
    license_path = tmp_path / "LICENSE"
    license_path.write_bytes("Copyright Café\n".encode("utf-8") + b"Invalid: \x81\n")
    distribution = SimpleNamespace(
        files=[Path("LICENSE")],
        locate_file=lambda _entry: license_path,
        metadata={"Name": "fixture-package"},
    )
    monkeypatch.setattr(module.metadata, "distributions", lambda: [distribution])
    monkeypatch.setattr(module.subprocess, "check_output", lambda *_args, **_kwargs: "fixture-package==1.0\n")

    original_read_text = Path.read_text
    original_write_text = Path.write_text

    def legacy_read_text(path, *args, **kwargs):
        kwargs.setdefault("encoding", "cp1252")
        return original_read_text(path, *args, **kwargs)

    def legacy_write_text(path, data, *args, **kwargs):
        kwargs.setdefault("encoding", "cp1252")
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", legacy_read_text)
    monkeypatch.setattr(Path, "write_text", legacy_write_text)

    stage = tmp_path / "stage"
    stage.mkdir()
    module.write_notices(stage, "1.0.0")

    notices = (stage / "THIRD-PARTY-NOTICES.txt").read_bytes().decode("utf-8")
    assert "Copyright Café" in notices
    assert "Invalid: \ufffd" in notices


def test_core_package_manifest_covers_every_byte_and_optional_helper(tmp_path):
    module = core_recipe()
    root = core_fixture(tmp_path)
    module.write_manifest(root, target="aarch64-apple-darwin", version="1.0.0", provenance={"source": "fixture"})
    manifest = module.verify_package(root, "aarch64-apple-darwin")
    assert manifest["helper"] is False
    assert manifest["files"]["bin/gamja"]["sha256"] == "4740ae6347b0172c01254ff55bae5aff5199f4446e7f6d643d40185b3f475145"
    (root / "libexec/gamja/gamja-engine").write_bytes(b"changed")
    import pytest
    with pytest.raises(ValueError, match="hash|manifest"):
        module.verify_package(root, "aarch64-apple-darwin")


def test_core_package_rejects_wrong_target_extra_web_assets_and_external_symlinks(tmp_path):
    import pytest
    module = core_recipe()
    root = core_fixture(tmp_path)
    module.write_manifest(root, target="aarch64-apple-darwin", version="1.0.0", provenance={})
    with pytest.raises(ValueError, match="target"):
        module.verify_package(root, "x86_64-apple-darwin")
    (root / "index.html").write_text("GUI")
    with pytest.raises(ValueError, match="asset|manifest"):
        module.verify_package(root, "aarch64-apple-darwin")
    (root / "index.html").unlink()
    (root / "libexec/gamja/playwright-browsers/outside").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        module.verify_package(root, "aarch64-apple-darwin")


def test_core_package_rejects_gui_even_with_fresh_manifest(tmp_path):
    import pytest
    module = core_recipe()
    root = core_fixture(tmp_path)
    (root / "Something.app").mkdir()
    with pytest.raises(ValueError, match="asset"):
        module.write_manifest(root, target="aarch64-apple-darwin", version="1.0.0", provenance={})


def test_desktop_core_copy_is_exact_and_rejects_mutation(tmp_path):
    import pytest
    module = core_recipe()
    root = core_fixture(tmp_path)
    module.write_manifest(root, target="aarch64-apple-darwin", version="1.0.0", provenance={})
    destination = tmp_path / "desktop/resources/gamja-core"
    module.stage_verified_package(root, destination, "aarch64-apple-darwin")
    assert (destination / "core-manifest.json").read_bytes() == (root / "core-manifest.json").read_bytes()
    assert (destination / "libexec/gamja/gamja-engine").read_bytes() == b"engine"
    (root / "bin/gamja").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash|manifest"):
        module.stage_verified_package(root, destination, "aarch64-apple-darwin")
    assert (destination / "bin/gamja").read_bytes() == b"host"


def test_core_only_relink_stages_exact_engine_without_hardlinks(tmp_path):
    module = core_recipe()
    source = core_fixture(tmp_path)
    module.write_manifest(source, target="aarch64-apple-darwin", version="1.0.0", provenance={})
    expected_manifest = module.sha256(source / "core-manifest.json")
    expected = module.sha256(source / "libexec/gamja/gamja-engine")
    stage = tmp_path / "core-relink-stage"

    manifest = module.stage_core_relink_source(
        source, stage, "aarch64-apple-darwin", expected_manifest, expected)

    assert manifest["target"] == "aarch64-apple-darwin"
    assert module.sha256(stage / "libexec/gamja/gamja-engine") == expected
    for copied in stage.rglob("*"):
        if copied.is_file() and not copied.is_symlink():
            original = source / copied.relative_to(stage)
            assert copied.stat().st_ino != original.stat().st_ino
            assert copied.stat().st_nlink == 1

    changed = "0" * 64 if expected != "0" * 64 else "1" * 64
    with pytest.raises(ValueError, match="engine hash"):
        module.stage_core_relink_source(
            source, tmp_path / "rejected-stage", "aarch64-apple-darwin",
            expected_manifest, changed)

    (source / "core-manifest.json").write_text("{}")
    with pytest.raises(ValueError, match="manifest hash"):
        module.stage_core_relink_source(
            source, tmp_path / "changed-manifest", "aarch64-apple-darwin",
            expected_manifest, expected)


def test_core_only_relink_rejects_payload_changed_after_bound_manifest(tmp_path):
    module = core_recipe()
    source = core_fixture(tmp_path)
    module.write_manifest(source, target="aarch64-apple-darwin", version="1.0.0", provenance={})
    expected_manifest = module.sha256(source / "core-manifest.json")
    expected_engine = module.sha256(source / "libexec/gamja/gamja-engine")
    (source / "THIRD-PARTY-NOTICES.txt").write_bytes(b"changed after manifest")
    with pytest.raises(ValueError, match="manifest|hash"):
        module.stage_core_relink_source(
            source, tmp_path / "changed-payload", "aarch64-apple-darwin",
            expected_manifest, expected_engine)


def test_helper_only_repack_preserves_core_engine_and_rewrites_full_manifest(
        monkeypatch, tmp_path):
    module = core_recipe()
    source = core_fixture(tmp_path)
    helper = source / "libexec/gamja/gamja-platform-helper"
    helper.write_bytes(b"old-helper")
    module.write_manifest(
        source, target="aarch64-apple-darwin", version="1.0.0",
        provenance={"build_id": "old"})
    expected = {
        "manifest": module.sha256(source / "core-manifest.json"),
        "core": module.sha256(source / "bin/gamja"),
        "engine": module.sha256(source / "libexec/gamja/gamja-engine"),
        "helper": module.sha256(helper),
    }
    built = tmp_path / "built-helper"
    built.write_bytes(b"new-helper")
    built.chmod(0o755)
    output = tmp_path / "helper-repack"

    def sign(path):
        assert path == output.parent / ".unused" or path.name == "gamja-platform-helper"
        path.write_bytes(path.read_bytes() + b"-adhoc")
        path.chmod(0o755)

    monkeypatch.setattr(module, "sign_helper_ad_hoc", sign)
    module.repack_helper_package(
        source, output, built,
        expected_manifest_sha256=expected["manifest"],
        expected_core_sha256=expected["core"],
        expected_engine_sha256=expected["engine"],
        expected_helper_sha256=expected["helper"],
        build_id="screen-eof-fixed-v1",
        source_commit="a" * 40,
        source_sha256={"platform-helper/src/server.rs": "b" * 64},
    )

    manifest = module.verify_package(output, "aarch64-apple-darwin")
    assert (output / "bin/gamja").read_bytes() == b"host"
    assert (output / "libexec/gamja/gamja-engine").read_bytes() == b"engine"
    assert (output / "libexec/gamja/gamja-platform-helper").read_bytes() == b"new-helper-adhoc"
    assert manifest["provenance"]["build_id"] == "screen-eof-fixed-v1"
    assert manifest["provenance"]["source_commit"] == "a" * 40
    assert manifest["provenance"]["source_sha256"] == {
        "platform-helper/src/server.rs": "b" * 64}
    for relative, key in (("bin/gamja", "core"),
                          ("libexec/gamja/gamja-engine", "engine")):
        assert module.sha256(output / relative) == expected[key]
        assert (output / relative).stat().st_ino != (source / relative).stat().st_ino
        assert (output / relative).stat().st_nlink == 1


def test_helper_only_repack_rejects_changed_fixed_input_and_runtime_receipt(tmp_path):
    module = core_recipe()
    source = core_fixture(tmp_path)
    helper = source / "libexec/gamja/gamja-platform-helper"
    helper.write_bytes(b"old-helper")
    module.write_manifest(source, target="aarch64-apple-darwin", version="1.0.0",
                          provenance={})
    expected_manifest = module.sha256(source / "core-manifest.json")
    expected_core = module.sha256(source / "bin/gamja")
    expected_engine = module.sha256(source / "libexec/gamja/gamja-engine")
    expected_helper = module.sha256(helper)
    (source / ".gamja-runtime-receipt.json").write_text("{}")

    with pytest.raises(ValueError, match="runtime receipt"):
        module.stage_helper_repack_source(
            source, tmp_path / "stage", "aarch64-apple-darwin",
            expected_manifest, expected_core, expected_engine, expected_helper)

    (source / ".gamja-runtime-receipt.json").unlink()
    helper.write_bytes(b"changed")
    with pytest.raises(ValueError, match="helper hash|manifest|hash"):
        module.stage_helper_repack_source(
            source, tmp_path / "changed", "aarch64-apple-darwin",
            expected_manifest, expected_core, expected_engine, expected_helper)


def test_helper_only_cli_routes_exact_pins_without_core_signing(monkeypatch, tmp_path):
    module = core_recipe()
    source = tmp_path / "source"
    output = tmp_path / "output"
    calls = []
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(module, "build_helper_only",
                        lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(sys, "argv", [
        "build_core.py", "--output", str(output),
        "--helper-repack-prefix", str(source),
        "--expected-manifest-sha256", "a" * 64,
        "--expected-core-sha256", "b" * 64,
        "--expected-engine-sha256", "c" * 64,
        "--expected-helper-sha256", "d" * 64,
        "--build-id", "screen-eof-fixed-v1",
    ])

    module.main()

    assert calls == [((source, output), {
        "expected_manifest_sha256": "a" * 64,
        "expected_core_sha256": "b" * 64,
        "expected_engine_sha256": "c" * 64,
        "expected_helper_sha256": "d" * 64,
        "build_id": "screen-eof-fixed-v1",
    })]


@pytest.mark.parametrize("arguments", [
    ["--verify", "fixture", "--expected-helper-sha256", "d" * 64],
    ["--output", "fixture", "--expected-helper-sha256", "d" * 64],
    ["--output", "fixture", "--expected-core-sha256", "c" * 64],
    ["--output", "fixture", "--helper-repack-prefix", "source",
     "--expected-manifest-sha256", "a" * 64,
     "--expected-core-sha256", "b" * 64,
     "--expected-engine-sha256", "c" * 64],
])
def test_helper_only_cli_rejects_stray_or_incomplete_mode_options(
        monkeypatch, arguments):
    module = core_recipe()
    called = []
    monkeypatch.setattr(module, "build", lambda *_a, **_kw: called.append("build"))
    monkeypatch.setattr(module, "build_helper_only",
                        lambda *_a, **_kw: called.append("helper"))
    monkeypatch.setattr(module, "verify_package",
                        lambda *_a, **_kw: called.append("verify"))
    monkeypatch.setattr(sys, "argv", ["build_core.py", *arguments])

    with pytest.raises(SystemExit):
        module.main()
    assert called == []


def test_helper_only_build_rejects_source_drift_before_publication(
        monkeypatch, tmp_path):
    module = core_recipe()
    root = tmp_path / "repo"
    source_file = root / "platform-helper/src/server.rs"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("before")
    cargo_target = tmp_path / "target"
    built = cargo_target / "aarch64-apple-darwin/release/gamja-platform-helper"
    prefix = tmp_path / "prefix"
    output = tmp_path / "output"
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.platform, "machine", lambda: "arm64")
    monkeypatch.setenv("CARGO_TARGET_DIR", str(cargo_target))
    monkeypatch.setattr(module, "build_input_paths",
                        lambda **_kwargs: ["platform-helper/src/server.rs"])
    monkeypatch.setattr(module, "validate_helper_repack_source",
                        lambda *_args, **_kwargs: {"version": "1.0.0"}, raising=False)
    published = []
    monkeypatch.setattr(module, "repack_helper_package",
                        lambda *_args, **_kwargs: published.append(True))
    monkeypatch.setattr(module.subprocess, "check_output",
                        lambda *_args, **_kwargs: "a" * 40 + "\n")

    def run(_arguments, **_kwargs):
        built.parent.mkdir(parents=True)
        built.write_bytes(b"helper")
        source_file.write_text("after")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)

    with pytest.raises(ValueError, match="source drift"):
        module.build_helper_only(
            prefix, output, expected_manifest_sha256="1" * 64,
            expected_core_sha256="2" * 64, expected_engine_sha256="3" * 64,
            expected_helper_sha256="4" * 64, build_id="screen-eof-fixed-v1")
    assert published == []
    assert not output.exists()


def test_helper_only_repack_rejects_symlink_parent_and_late_collision(
        monkeypatch, tmp_path):
    module = core_recipe()
    source = core_fixture(tmp_path / "input")
    helper = source / "libexec/gamja/gamja-platform-helper"
    helper.write_bytes(b"old-helper")
    module.write_manifest(source, target="aarch64-apple-darwin", version="1.0.0",
                          provenance={})
    pins = {
        "expected_manifest_sha256": module.sha256(source / "core-manifest.json"),
        "expected_core_sha256": module.sha256(source / "bin/gamja"),
        "expected_engine_sha256": module.sha256(source / "libexec/gamja/gamja-engine"),
        "expected_helper_sha256": module.sha256(helper),
    }
    built = tmp_path / "built-helper"
    built.write_bytes(b"new-helper"); built.chmod(0o755)
    real_parent = tmp_path / "real-parent"; real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"; linked_parent.symlink_to(real_parent)
    with pytest.raises(ValueError, match="parent|symlink|output"):
        module.repack_helper_package(
            source, linked_parent / "candidate", built, **pins,
            build_id="screen-eof-fixed-v1", source_commit="a" * 40,
            source_sha256={"platform-helper/src/server.rs": "b" * 64})

    output = real_parent / "candidate"
    def collide(path):
        path.write_bytes(path.read_bytes() + b"-adhoc"); path.chmod(0o755)
        output.mkdir(); (output / "owner-marker").write_text("keep")
    monkeypatch.setattr(module, "sign_helper_ad_hoc", collide)
    with pytest.raises(ValueError, match="collision|publish"):
        module.repack_helper_package(
            source, output, built, **pins, build_id="screen-eof-fixed-v1",
            source_commit="a" * 40,
            source_sha256={"platform-helper/src/server.rs": "b" * 64})
    assert (output / "owner-marker").read_text() == "keep"


def test_core_toolchain_refuses_unpinned_python_and_cross_target():
    import pytest
    module = core_recipe()
    with pytest.raises(ValueError, match="Python"):
        module.validate_toolchain("aarch64-apple-darwin", python_version=(3, 11, 0), rust_host="aarch64-apple-darwin", versions={})
    with pytest.raises(ValueError, match="native"):
        module.validate_toolchain("aarch64-apple-darwin", python_version=(3, 11, 15), rust_host="x86_64-apple-darwin", versions={})
    with pytest.raises(ValueError, match="playwright"):
        module.validate_toolchain("aarch64-apple-darwin", python_version=(3, 11, 15), rust_host="aarch64-apple-darwin", versions={"playwright": "1.57.0"})


def test_core_package_optional_helper_and_pin_tampering(tmp_path):
    import json
    import pytest
    module = core_recipe()
    root = core_fixture(tmp_path)
    (root / "libexec/gamja/gamja-platform-helper").write_bytes(b"native helper")
    module.write_manifest(root, target="aarch64-apple-darwin", version="1.0.0", provenance={})
    assert module.verify_package(root, "aarch64-apple-darwin")["helper"] is True
    data = json.loads((root / "core-manifest.json").read_text())
    data["browsers"]["chromium"] = "9999"
    (root / "core-manifest.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="pin"):
        module.verify_package(root, "aarch64-apple-darwin")


def test_core_stage_refuses_alias_destination_and_preserves_it(tmp_path):
    import pytest
    module = core_recipe()
    root = core_fixture(tmp_path)
    module.write_manifest(root, target="aarch64-apple-darwin", version="1.0.0", provenance={})
    alias = tmp_path / "protected-alias"
    alias.write_bytes(b"Finder Alias placeholder")
    with pytest.raises(ValueError, match="non-directory"):
        module.stage_verified_package(root, alias, "aarch64-apple-darwin")
    assert alias.read_bytes() == b"Finder Alias placeholder"
    assert not list(tmp_path.glob(".gamja-core-copy-*"))


def test_core_package_rejects_unknown_runtime_file(tmp_path):
    import pytest
    module = core_recipe()
    root = core_fixture(tmp_path)
    (root / "bin/tauri").write_bytes(b"GUI binary")
    with pytest.raises(ValueError, match="asset"):
        module.write_manifest(root, target="aarch64-apple-darwin", version="1.0.0", provenance={})


def test_dual_candidate_outputs_require_distinct_public_build_ids_and_paths(tmp_path):
    import pytest
    module = core_recipe()
    first = tmp_path / "continuity"
    second = tmp_path / "final"
    assert module.candidate_builds(
        first, "aarch64-apple-darwin",
        build_id="credential-recovery-continuity-v1",
        second_output=second,
        second_build_id="credential-recovery-final-v1",
    ) == [
        (first.absolute(), "credential-recovery-continuity-v1"),
        (second.absolute(), "credential-recovery-final-v1"),
    ]
    with pytest.raises(ValueError, match="distinct"):
        module.candidate_builds(first, "aarch64-apple-darwin", build_id="same-build-id",
                                second_output=second, second_build_id="same-build-id")
    with pytest.raises(ValueError, match="overlap"):
        module.candidate_builds(first, "aarch64-apple-darwin", build_id="candidate-one",
                                second_output=first / "nested", second_build_id="candidate-two")
    with pytest.raises(ValueError, match="native macOS"):
        module.candidate_builds(first, "x86_64-pc-windows-msvc", build_id="candidate-one",
                                second_output=second, second_build_id="candidate-two")


def test_windows_package_requires_secure_reader_dll(tmp_path):
    import pytest
    module = core_recipe()
    root = core_fixture(tmp_path)
    (root / "bin/gamja").rename(root / "bin/gamja.exe")
    (root / "libexec/gamja/gamja-engine").rename(root / "libexec/gamja/gamja-engine.exe")
    module.write_manifest(root, target="x86_64-pc-windows-msvc", version="1.0.0", provenance={})
    with pytest.raises(ValueError, match="required"):
        module.verify_package(root, "x86_64-pc-windows-msvc")
    (root / "libexec/gamja/gamja_core_host.dll").write_bytes(b"secure Rust reader")
    module.write_manifest(root, target="x86_64-pc-windows-msvc", version="1.0.0", provenance={})
    assert "libexec/gamja/gamja_core_host.dll" in module.verify_package(root, "x86_64-pc-windows-msvc")["files"]


def test_desktop_arm_default_stages_core_without_rebuilding_engine(monkeypatch, tmp_path):
    module = core_recipe(monkeypatch=monkeypatch)
    root = core_fixture(tmp_path)
    module.write_manifest(root, target="aarch64-apple-darwin", version="1.0.0", provenance={})
    source_inventory = module.inventory(root)
    source_manifest = (root / "core-manifest.json").read_bytes()
    spec = importlib.util.spec_from_file_location("build_desktop", ROOT / "scripts/build_desktop.py")
    desktop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(desktop)
    pin_desktop_fixture(monkeypatch, desktop, root)
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(desktop.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(desktop, "TAURI", tmp_path / "tauri")
    monkeypatch.setattr(desktop, "DEFAULT_ARM_CORE_PACKAGE", root)
    monkeypatch.setattr(sys, "argv", ["build_desktop.py", "--sidecar-only"])
    def unexpected_build(*args, **kwargs):
        raise AssertionError("embedding a verified core must not build an engine")
    monkeypatch.setattr(desktop.subprocess, "run", unexpected_build)
    desktop.main()
    assert (tmp_path / "tauri/resources/gamja-core/libexec/gamja/gamja-engine").read_bytes() == b"engine"
    assert not (tmp_path / "tauri/binaries").exists()
    assert module.inventory(root) == source_inventory
    assert (root / "core-manifest.json").read_bytes() == source_manifest


def test_desktop_fixed_bootstrap_rejects_an_internally_valid_alternate_package(
    monkeypatch, tmp_path
):
    module = core_recipe(monkeypatch=monkeypatch)
    root = core_fixture(tmp_path)
    module.write_manifest(
        root, target="aarch64-apple-darwin", version="1.0.0", provenance={}
    )
    spec = importlib.util.spec_from_file_location(
        "build_desktop", ROOT / "scripts/build_desktop.py"
    )
    desktop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(desktop)
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))

    with pytest.raises(RuntimeError, match="fixed bootstrap hash mismatch"):
        desktop.verify_fixed_bootstrap_package(root)


def test_desktop_explicit_candidate_derives_verified_compile_time_binding(monkeypatch, tmp_path):
    module = core_recipe(monkeypatch=monkeypatch)
    root = core_fixture(tmp_path / "candidate")
    module.write_manifest(root, target="aarch64-apple-darwin", version="1.0.0", provenance={})
    spec = importlib.util.spec_from_file_location("build_desktop", ROOT / "scripts/build_desktop.py")
    desktop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(desktop)
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(desktop.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(desktop, "TAURI", tmp_path / "tauri")
    monkeypatch.setattr(sys, "argv", ["build_desktop.py", "--sidecar-only",
                                      "--core-package", str(root)])
    desktop.main()
    staged = tmp_path / "tauri/resources/gamja-core"
    assert desktop.verified_core_binding(staged) == desktop.verified_core_binding(root)


def test_desktop_arm_default_fails_closed_when_canonical_core_is_missing(monkeypatch, tmp_path):
    import pytest
    spec = importlib.util.spec_from_file_location("build_desktop", ROOT / "scripts/build_desktop.py")
    desktop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(desktop)
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(desktop.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(desktop, "TAURI", tmp_path / "tauri")
    monkeypatch.setattr(desktop, "DEFAULT_ARM_CORE_PACKAGE", tmp_path / "missing-core")
    monkeypatch.setattr(sys, "argv", ["build_desktop.py", "--sidecar-only"])
    monkeypatch.setattr(desktop.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not build a fallback"))

    with pytest.raises((RuntimeError, ValueError, FileNotFoundError), match="manifest|package|exist|missing"):
        desktop.main()

    assert not (tmp_path / "tauri/binaries").exists()


def test_desktop_arm_rejects_canonical_staging_overlap_before_mutation(monkeypatch, tmp_path):
    import pytest
    module = core_recipe()
    spec = importlib.util.spec_from_file_location("build_desktop", ROOT / "scripts/build_desktop.py")
    desktop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(desktop)
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(desktop.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        desktop.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("overlap must fail before build"),
    )

    cases = []

    equal_tauri = tmp_path / "equal/tauri"
    equal_core = core_fixture(equal_tauri / "resources")
    cases.append(("equal", equal_tauri, equal_core))

    descendant_tauri = tmp_path / "descendant/tauri"
    descendant_core = core_fixture(descendant_tauri / "resources/gamja-core")
    cases.append(("canonical-descendant", descendant_tauri, descendant_core))

    ancestor_core = core_fixture(tmp_path / "ancestor")
    ancestor_tauri = ancestor_core / "gui"
    cases.append(("canonical-ancestor", ancestor_tauri, ancestor_core))

    alias_root = tmp_path / "alias"
    alias_core = core_fixture(alias_root)
    alias_tauri = alias_root / "tauri"
    (alias_tauri / "resources").mkdir(parents=True)
    (alias_tauri / "resources/gamja-core").symlink_to(alias_core, target_is_directory=True)
    cases.append(("symlink-resolved", alias_tauri, alias_core))

    for name, tauri, core in cases:
        module.write_manifest(core, target="aarch64-apple-darwin", version="1.0.0", provenance={})
        before_inventory = module.inventory(core)
        before_manifest = (core / "core-manifest.json").read_bytes()
        staged = tauri / "resources/gamja-core"
        staged_was_symlink = staged.is_symlink()
        monkeypatch.setattr(desktop, "TAURI", tauri)
        monkeypatch.setattr(sys, "argv", ["build_desktop.py", "--sidecar-only", "--core-package", str(core)])

        with pytest.raises(RuntimeError, match="canonical core.*staging.*overlap"):
            desktop.main()

        assert module.inventory(core) == before_inventory, name
        assert (core / "core-manifest.json").read_bytes() == before_manifest, name
        assert staged.is_symlink() is staged_was_symlink, name


def test_desktop_arm_rejects_case_alias_overlap_when_filesystem_has_it(monkeypatch, tmp_path):
    import pytest
    module = core_recipe()
    probe = tmp_path / "case-probe"
    probe.mkdir()
    if not probe.with_name("CASE-PROBE").is_dir():
        pytest.skip("filesystem is case-sensitive")
    spec = importlib.util.spec_from_file_location("build_desktop", ROOT / "scripts/build_desktop.py")
    desktop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(desktop)
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(desktop.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(desktop.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not build"))

    equal_tauri = tmp_path / "equal/tauri"
    equal_core = core_fixture(equal_tauri / "resources")
    cases = [("equal", equal_tauri, equal_core.with_name("GAMJA-CORE"), equal_core)]

    ancestor_core = core_fixture(tmp_path / "ancestor/canonical")
    ancestor_tauri = ancestor_core / "gui"
    ancestor_alias = ancestor_core.parent.with_name("CANONICAL") / ancestor_core.name
    cases.append(("case-variant-ancestor", ancestor_tauri,
                  ancestor_alias, ancestor_core))

    descendant_tauri = tmp_path / "descendant/tauri"
    staged = descendant_tauri / "resources/gamja-core"
    descendant_core = core_fixture(staged / "canonical")
    descendant_alias = staged.with_name("GAMJA-CORE") / "canonical"
    cases.append(("case-variant-descendant", descendant_tauri,
                  descendant_alias, descendant_core))

    for name, tauri, core_argument, actual_core in cases:
        module.write_manifest(actual_core, target="aarch64-apple-darwin", version="1.0.0", provenance={})
        before_inventory = module.inventory(actual_core)
        before_manifest = (actual_core / "core-manifest.json").read_bytes()
        monkeypatch.setattr(desktop, "TAURI", tauri)
        monkeypatch.setattr(sys, "argv", [
            "build_desktop.py", "--sidecar-only", "--core-package", str(core_argument),
        ])

        with pytest.raises(RuntimeError, match="canonical core.*staging.*overlap"):
            desktop.main()

        assert module.inventory(actual_core) == before_inventory, name
        assert (actual_core / "core-manifest.json").read_bytes() == before_manifest, name


def test_desktop_arm_rejects_core_inside_mutated_build_outputs_before_staging(monkeypatch, tmp_path):
    import json
    import pytest
    module = core_recipe()
    spec = importlib.util.spec_from_file_location("build_desktop", ROOT / "scripts/build_desktop.py")
    desktop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(desktop)
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(desktop.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(desktop.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not build"))

    cases = []
    for name in ("frontend-output", "app-bundle"):
        project = tmp_path / name
        tauri = project / "frontend/src-tauri"
        tauri.mkdir(parents=True)
        (tauri / "tauri.conf.json").write_text(json.dumps({"productName": "Gamja Bot"}))
        target = project / "target"
        if name == "frontend-output":
            core = core_fixture(project / "frontend/dist/canonical")
        else:
            app = target / "aarch64-apple-darwin/release/bundle/macos/Gamja Bot.app"
            core = core_fixture(app / "Contents/Resources/canonical")
        cases.append((name, project, tauri, target, core))

    for name, project, tauri, target, core in cases:
        module.write_manifest(core, target="aarch64-apple-darwin", version="1.0.0", provenance={})
        before_inventory = module.inventory(core)
        before_manifest = (core / "core-manifest.json").read_bytes()
        monkeypatch.setattr(desktop, "ROOT", project)
        monkeypatch.setattr(desktop, "TAURI", tauri)
        monkeypatch.setenv("CARGO_TARGET_DIR", str(target))
        monkeypatch.setattr(sys, "argv", [
            "build_desktop.py", "--artifact-release-app-only", "--core-package", str(core)
        ])

        with pytest.raises(RuntimeError, match="canonical core.*staging/build.*overlap"):
            desktop.main()

        assert module.inventory(core) == before_inventory, name
        assert (core / "core-manifest.json").read_bytes() == before_manifest, name
        assert not (tauri / "resources/gamja-core").exists(), name


def test_desktop_staged_core_cleanup_refuses_a_redirect(monkeypatch, tmp_path):
    import pytest
    spec = importlib.util.spec_from_file_location("build_desktop", ROOT / "scripts/build_desktop.py")
    desktop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(desktop)
    tauri = tmp_path / "tauri"
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel").write_bytes(b"unchanged")
    (tauri / "resources").mkdir(parents=True)
    staged = tauri / "resources/gamja-core"
    staged.symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(desktop, "TAURI", tauri)

    with pytest.raises(RuntimeError, match="unowned"):
        desktop.remove_staged_core(staged)

    assert (external / "sentinel").read_bytes() == b"unchanged"


def test_desktop_windows_core_cutover_stays_gated(monkeypatch, tmp_path):
    import pytest
    spec = importlib.util.spec_from_file_location("build_desktop", ROOT / "scripts/build_desktop.py")
    desktop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(desktop)
    monkeypatch.setattr(desktop.platform, "system", lambda: "Windows")
    monkeypatch.setattr(desktop.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(sys, "argv", ["build_desktop.py", "--core-package", str(tmp_path)])
    with pytest.raises(SystemExit) as error:
        desktop.main()
    assert error.value.code == 2


def test_desktop_arm_default_refuses_pre_stable_sign_packaging(monkeypatch):
    desktop = load_desktop_recipe()
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(desktop.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(sys, "argv", ["build_desktop.py"])
    monkeypatch.setattr(
        desktop.subprocess, "run",
        lambda *_args, **_kwargs: pytest.fail("default ARM path must stop before build/package"),
    )
    with pytest.raises(SystemExit) as error:
        desktop.main()
    assert error.value.code == 2


def test_core_package_without_browser_or_helper_still_verifies(tmp_path):
    import shutil
    module = core_recipe()
    root = core_fixture(tmp_path)
    shutil.rmtree(root / "libexec/gamja/playwright-browsers")
    module.write_manifest(root, target="aarch64-apple-darwin", version="1.0.0", provenance={})
    manifest = module.verify_package(root, "aarch64-apple-darwin")
    assert manifest["browser_payload"] is False
    assert manifest["helper"] is False


def test_core_package_allows_native_browser_app_only_inside_payload(tmp_path):
    module = core_recipe()
    root = core_fixture(tmp_path)
    binary = root / "libexec/gamja/playwright-browsers/chromium-1208/chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"pinned browser")
    module.write_manifest(root, target="aarch64-apple-darwin", version="1.0.0", provenance={})
    assert module.verify_package(root, "aarch64-apple-darwin")["browser_payload"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="synthetic macOS framework fixture requires POSIX symlinks")
def test_desktop_arm_release_app_only_uses_core_config_without_feature_escape(monkeypatch, tmp_path):
    import json
    import sys
    module = core_recipe(monkeypatch=monkeypatch)
    root = core_fixture(tmp_path)
    framework = root / 'libexec/gamja/playwright-browsers/chromium-1208/Fixture.framework'
    version = framework / 'Versions/A'
    version.mkdir(parents=True)
    (version / 'Fixture').write_bytes(b'canonical browser executable')
    for name in ['Helpers', 'Libraries', 'Resources']:
        (version / name).mkdir()
        (version / name / 'fixture').write_bytes(name.encode())
        (framework / name).symlink_to('Versions/Current/' + name)
    (framework / 'Versions/Current').symlink_to('A')
    (framework / 'Fixture').symlink_to('Versions/Current/Fixture')
    module.write_manifest(root, target="aarch64-apple-darwin", version="1.0.0", provenance={})
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("build_desktop", ROOT / "scripts/build_desktop.py")
    desktop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(desktop)
    pin_desktop_fixture(monkeypatch, desktop, root)
    target = tmp_path / 'cargo'
    target.mkdir()
    tauri = tmp_path / 'tauri'
    tauri.mkdir()
    (tauri / 'binaries').mkdir()
    (tauri / 'tauri.conf.json').write_text(json.dumps({'productName': 'Fixture', 'version': '1.0.0'}))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(desktop.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(desktop, "TAURI", tauri)
    monkeypatch.setattr(desktop, "DEFAULT_ARM_CORE_PACKAGE", root)
    monkeypatch.setattr(desktop, "npm_executable", lambda: 'synthetic-npm')
    artifact_binary = target / 'aarch64-apple-darwin/release/gamja-artifact'
    artifact_binary.parent.mkdir(parents=True)
    artifact_binary.write_bytes(b'signed-artifact')
    artifact_binary.chmod(0o700)
    artifact_hash = desktop.sha256(artifact_binary)
    monkeypatch.setattr(desktop, 'build_artifact_binary', lambda value: artifact_binary)
    monkeypatch.setattr(desktop, 'sign_artifact_ad_hoc', lambda value: artifact_hash)
    monkeypatch.setattr(desktop, 'verify_artifact_binary', lambda value, expected: expected)
    monkeypatch.setenv('CARGO_TARGET_DIR', str(target))
    monkeypatch.setattr(sys, "argv", ['build_desktop.py', '--artifact-release-app-only'])
    calls = []
    receipts = []
    monkeypatch.setattr(
        desktop, 'emit_candidate_receipt',
        lambda *args: receipts.append(args) or {'status': 'ready-for-stable-outer-signing'},
    )
    original_restore = desktop.restore_embedded_core
    def restore_after_staging_cleanup(*args, **kwargs):
        assert not (tauri / 'resources/gamja-core').exists()
        return original_restore(*args, **kwargs)
    monkeypatch.setattr(desktop, 'restore_embedded_core', restore_after_staging_cleanup)
    def run(arguments, **kwargs):
        calls.append(arguments)
        if arguments[0] == 'synthetic-npm':
            assert kwargs['env']['GAMJA_CORE_MANIFEST_SHA256'] == desktop.sha256(root / 'core-manifest.json')
            assert kwargs['env']['GAMJA_CORE_SHA256'] == desktop.sha256(root / 'bin/gamja')
            assert kwargs['env']['GAMJA_ENGINE_SHA256'] == desktop.sha256(root / 'libexec/gamja/gamja-engine')
            assert '--config' in arguments
            assert arguments[arguments.index('--config') + 1] == 'src-tauri/tauri.core.conf.json'
            assert '--features' not in arguments
            assert '--no-default-features' not in arguments
            embedded = target / 'aarch64-apple-darwin/release/bundle/macos/Fixture.app/Contents/Resources/gamja-core'
            module.stage_verified_package(root, embedded, 'aarch64-apple-darwin')
            bundled_artifact = embedded.parents[2] / 'MacOS/gamja-artifact'
            bundled_artifact.parent.mkdir(parents=True)
            bundled_artifact.write_bytes(b'signed-artifact')
            bundled_artifact.chmod(0o700)
            (embedded.parent / 'Diagnostics Help.txt').write_bytes(
                (ROOT / 'scripts/Diagnostics Help.txt').read_bytes()
            )
            copied = embedded / framework.relative_to(root)
            executable_bytes = (copied / 'Fixture').read_bytes()
            (copied / 'Fixture').unlink()
            (copied / 'Fixture').write_bytes(executable_bytes)
            for link in ['Helpers', 'Libraries', 'Resources', 'Versions/Current']:
                (copied / link).unlink()
    monkeypatch.setattr(desktop.subprocess, 'run', run)
    desktop.main()
    assert len(calls) == 1
    assert receipts and receipts[0][1:4] == (app := target / 'aarch64-apple-darwin/release/bundle/macos/Fixture.app', root, artifact_hash)
    assert not (tauri / 'resources/gamja-core').exists()

    embedded = app / 'Contents/Resources/gamja-core'
    assert module.inventory(embedded) == module.inventory(root)
    assert (embedded / framework.relative_to(root) / 'Fixture').is_symlink()
    assert not list(embedded.parent.glob('.gamja-core-copy-*'))


def restore_fixture(tmp_path, monkeypatch):
    module = core_recipe(monkeypatch=monkeypatch)
    source = core_fixture(tmp_path)
    module.write_manifest(source, target="aarch64-apple-darwin", version="1.0.0", provenance={})
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("build_desktop", ROOT / "scripts/build_desktop.py")
    desktop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(desktop)
    bundle = tmp_path / 'cargo/aarch64-apple-darwin/release/bundle'
    app = bundle / 'macos/Fixture.app'
    destination = app / 'Contents/Resources/gamja-core'
    module.stage_verified_package(source, destination, 'aarch64-apple-darwin')
    return desktop, module, source, bundle, app, destination


def test_restore_embedded_core_rejects_symlink_app_parent(tmp_path, monkeypatch):
    import pytest
    desktop, _, source, bundle, app, destination = restore_fixture(tmp_path, monkeypatch)
    resources = destination.parent
    resources.rename(app / 'Contents/original-resources')
    resources.symlink_to(app / 'Contents/original-resources')
    with pytest.raises(ValueError, match='owned build'):
        desktop.restore_embedded_core(source, app, bundle, 'aarch64-apple-darwin')
    assert destination.joinpath('bin/gamja').read_bytes() == b'host'


def test_restore_embedded_core_rejects_destination_symlink(tmp_path, monkeypatch):
    import pytest
    desktop, _, source, bundle, app, destination = restore_fixture(tmp_path, monkeypatch)
    destination.rename(destination.with_name('original'))
    destination.symlink_to(destination.with_name('original'))
    with pytest.raises(ValueError, match='destination'):
        desktop.restore_embedded_core(source, app, bundle, 'aarch64-apple-darwin')
    assert destination.joinpath('bin/gamja').read_bytes() == b'host'


def test_restore_embedded_core_rejects_wrong_target_and_app_location(tmp_path, monkeypatch):
    import pytest
    desktop, _, source, bundle, app, destination = restore_fixture(tmp_path, monkeypatch)
    for path, target in [(app, 'x86_64-apple-darwin'), (app.parent / 'elsewhere/Fixture.app', 'aarch64-apple-darwin')]:
        with pytest.raises(ValueError, match='owned build'):
            desktop.restore_embedded_core(source, path, bundle, target)
    assert destination.joinpath('bin/gamja').read_bytes() == b'host'


def test_restore_embedded_core_refuses_mutated_source_before_replacing(tmp_path, monkeypatch):
    import pytest
    desktop, _, source, bundle, app, destination = restore_fixture(tmp_path, monkeypatch)
    source.joinpath('bin/gamja').write_bytes(b'mutation')
    with pytest.raises(ValueError, match='hash|manifest'):
        desktop.restore_embedded_core(source, app, bundle, 'aarch64-apple-darwin')
    assert destination.joinpath('bin/gamja').read_bytes() == b'host'
    assert not list(destination.parent.glob('.gamja-core-copy-*'))


def test_restore_embedded_core_reserves_disk_floor_before_copy(tmp_path, monkeypatch):
    import pytest
    desktop, _, source, bundle, app, destination = restore_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(desktop.shutil, 'disk_usage', lambda _: SimpleNamespace(free=2 * 1024**3))
    with pytest.raises(ValueError, match='disk'):
        desktop.restore_embedded_core(source, app, bundle, 'aarch64-apple-darwin')
    assert destination.joinpath('bin/gamja').read_bytes() == b'host'
    assert not list(destination.parent.glob('.gamja-core-copy-*'))


@pytest.mark.skipif(sys.platform == "win32", reason="macOS DMG signing fixture requires POSIX shell paths")
def test_dmg_package_never_resigns_or_mutates_stable_candidate(tmp_path):
    """Packaging only copies and verifies the already complete stable app."""
    module = core_recipe()
    source = core_fixture(tmp_path)
    helper = source / 'libexec/gamja/gamja-platform-helper'
    helper.write_bytes(b'helper')
    helper.chmod(0o700)
    module.write_manifest(source, target='aarch64-apple-darwin', version='1.0.0', provenance={})
    app = tmp_path / 'Fixture.app'
    app.joinpath('Contents/MacOS').mkdir(parents=True)
    artifact = app / 'Contents/MacOS/gamja-artifact'
    artifact.write_bytes(b'signed-artifact')
    artifact.chmod(0o700)
    embedded = app / 'Contents/Resources/gamja-core'
    module.stage_verified_package(source, embedded, 'aarch64-apple-darwin')
    embedded_help = app / 'Contents/Resources/Diagnostics Help.txt'
    embedded_help.write_bytes((ROOT / 'scripts/Diagnostics Help.txt').read_bytes())
    output = tmp_path / 'output/candidate.dmg'
    output.parent.mkdir()
    output.write_bytes(b'previous verified installer')
    stubs = tmp_path / 'stubs'
    stubs.mkdir()
    forced = stubs / 'codesign-force-attempted'
    image = stubs / 'image-attempted'
    (stubs / 'ditto').write_text('#!/bin/sh\ncp -R "$1" "$2"\n')
    (stubs / 'codesign').write_text(
        '#!/bin/sh\n'
        f'case "$*" in\n*--verify*gamja-artifact*) exit 0;;\n'
        '*-dv*gamja-artifact*) echo Identifier=top.gamja.artifact >&2; echo Signature=adhoc >&2; exit 0;;\n'
        '*--verify*) exit 0;;\n'
        f'*--force*) touch "{forced}"; exit 90;;\nesac\nexit 1\n')
    (stubs / 'hdiutil').write_text(f'#!/bin/sh\ntouch "{image}"\nexit 91\n')
    verifier = stubs / 'synthetic-python'
    verifier.write_text('#!/bin/sh\n[ "$2" = --verify ] || exit 91\n[ "$(cat "$3/bin/gamja")" = host ]\n')
    synthetic_recipe = tmp_path / 'synthetic-package-macos-dmg.sh'
    recipe = (ROOT / 'scripts/package_macos_dmg.sh').read_text()
    replacements = {
        'c7db6f970b456e75eb23359dfdbe79e2aa1cb16c4c72eed5649bae3a3a11b27f':
            module.sha256(source / 'core-manifest.json'),
        'b885e14bbacfdcbe7c68e4ff051db2d33ade88733d69471897fe4ed55da10a31':
            module.sha256(source / 'bin/gamja'),
        '2f6b534d61ef0212248ec76b6f92b9a9e5dc9e15dae21622c8ab785fa2bfded0':
            module.sha256(source / 'libexec/gamja/gamja-engine'),
        '0d10afdab1573a67b5be78c9746512e56a2a81f1d7afd6d77efa7c8a00ae0d5d':
            module.sha256(helper),
    }
    for fixed, fixture in replacements.items():
        assert recipe.count(fixed) == 1
        recipe = recipe.replace(fixed, fixture)
    synthetic_recipe.write_text(recipe)
    synthetic_recipe.chmod(0o700)
    (tmp_path / 'Diagnostics Help.txt').write_bytes(
        (ROOT / 'scripts/Diagnostics Help.txt').read_bytes()
    )
    for stub in stubs.iterdir(): stub.chmod(0o755)
    result = subprocess.run(['bash', str(synthetic_recipe), str(app), str(output),
                             str(source), 'aarch64-apple-darwin', *replacements.values(),
                             module.sha256(artifact)], capture_output=True, text=True,
                            env={**os.environ, 'PATH': f"{stubs}:{os.environ['PATH']}",
                                 'TMPDIR': str(tmp_path),
                                 'GAMJA_BUILD_PYTHON': str(verifier)})
    assert result.returncode != 0
    assert not forced.exists(), 'Packaging must never replace the stable outer signature'
    assert image.exists(), 'The fixture should reach image creation without changing the app'
    assert output.read_bytes() == b'previous verified installer'
    assert embedded.joinpath('bin/gamja').read_bytes() == b'host'
    assert source.joinpath('bin/gamja').read_bytes() == b'host'
    assert embedded_help.read_bytes() == (ROOT / 'scripts/Diagnostics Help.txt').read_bytes()
    assert not list(tmp_path.glob('gamja-dmg-stage.*'))
    assert not list(tmp_path.glob('gamja-dmg-mount.*'))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell fixture")
@pytest.mark.parametrize("embedded", [None, b"changed-help"])
def test_dmg_package_rejects_missing_or_changed_embedded_help_before_tools(tmp_path, embedded):
    recipe = tmp_path / "package_macos_dmg.sh"
    recipe.write_bytes((ROOT / "scripts/package_macos_dmg.sh").read_bytes())
    recipe.chmod(0o700)
    help_source = tmp_path / "Diagnostics Help.txt"
    help_source.write_bytes((ROOT / "scripts/Diagnostics Help.txt").read_bytes())
    app = tmp_path / "Fixture.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    if embedded is not None:
        destination = app / "Contents/Resources/Diagnostics Help.txt"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(embedded)
    marker = tmp_path / "tool-called"
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name in ("ditto", "codesign", "hdiutil"):
        stub = stubs / name
        stub.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 90\n')
        stub.chmod(0o700)

    result = subprocess.run(
        ["bash", str(recipe), str(app), str(tmp_path / "candidate.dmg")],
        capture_output=True, text=True,
        env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "TMPDIR": str(tmp_path)},
    )

    assert result.returncode != 0
    assert not marker.exists(), "help binding must fail before candidate copy/sign/image tools"


def load_desktop_recipe():
    spec = importlib.util.spec_from_file_location(
        "build_desktop_artifact_tests", ROOT / "scripts/build_desktop.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def artifact_source_fixture(tmp_path):
    root = tmp_path / "artifact-module"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "gamja-artifact"\nversion = "1.0.0"\n'
    )
    (root / "Cargo.lock").write_text(
        'version = 3\n\n[[package]]\nname = "gamja-artifact"\nversion = "1.0.0"\n'
    )
    (root / "src/main.rs").write_text("fn main() {}\n")
    return root


@pytest.mark.parametrize(
    "forbidden",
    ["security-framework", "rusqlite", "pyo3", "async-openai"],
)
def test_artifact_dependency_gate_rejects_forbidden_packages_and_lock_drift(
    tmp_path, forbidden
):
    desktop = load_desktop_recipe()
    source = artifact_source_fixture(tmp_path)
    lock = source / "Cargo.lock"

    with pytest.raises(RuntimeError, match="forbidden artifact dependency"):
        desktop.verify_artifact_dependency_report(lock, f"gamja-artifact v1.0.0\n{forbidden} v1.0.0\n")

    before = desktop.artifact_source_snapshot(source)
    lock.write_text(lock.read_text() + "# drift\n")
    with pytest.raises(RuntimeError, match="artifact source drift"):
        desktop.require_unchanged_artifact_source(source, before)
    lock.unlink()
    with pytest.raises(RuntimeError, match="artifact source"):
        desktop.artifact_source_snapshot(source)


def test_artifact_build_uses_locked_shared_target_and_two_jobs(monkeypatch, tmp_path):
    desktop = load_desktop_recipe()
    source = artifact_source_fixture(tmp_path)
    target = tmp_path / "shared-cargo-target"
    target.mkdir()
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        if arguments[1] == "tree":
            return SimpleNamespace(returncode=0, stdout="gamja-artifact v1.0.0\n", stderr="")
        built = target / desktop.ARTIFACT_TARGET / "release/gamja-artifact"
        built.parent.mkdir(parents=True)
        built.write_bytes(b"artifact-binary")
        built.chmod(0o700)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(desktop, "ARTIFACT_ROOT", source)
    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/shared/bin/cargo")
    built = desktop.build_artifact_binary(target, run=run)

    assert built == target / desktop.ARTIFACT_TARGET / "release/gamja-artifact"
    assert calls[0][0] == [
        "/shared/bin/cargo", "tree", "--locked", "--manifest-path",
        str(source / "Cargo.toml"), "--target", desktop.ARTIFACT_TARGET,
        "--edges", "normal", "--prefix", "none", "--format", "{p}",
    ]
    assert calls[1][0] == [
        "/shared/bin/cargo", "build", "--locked", "--release", "--target",
        desktop.ARTIFACT_TARGET, "-j", "2", "--manifest-path",
        str(source / "Cargo.toml"),
    ]
    assert calls[1][1]["env"]["CARGO_TARGET_DIR"] == str(target)


def test_r2_release_reuses_bound_installed_artifact_without_build_or_sign(
    monkeypatch, tmp_path
):
    desktop = load_desktop_recipe()
    app = tmp_path / "Applications/Gamja Bot.app"
    artifact = app / "Contents/MacOS/gamja-artifact"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"reviewed-signed-artifact")
    artifact.chmod(0o700)
    expected = desktop.sha256(artifact)
    monkeypatch.setattr(desktop, "R2_INSTALLED_APP", app)
    monkeypatch.setattr(desktop, "R2_ARTIFACT_SHA256", expected)
    monkeypatch.setattr(desktop, "verify_r2_installed_source", lambda: None)
    monkeypatch.setattr(
        desktop, "build_artifact_binary",
        lambda *_args: pytest.fail("r2 must not rebuild the artifact module"),
    )
    monkeypatch.setattr(
        desktop, "sign_artifact_ad_hoc",
        lambda *_args: pytest.fail("r2 must not re-sign the artifact module"),
    )

    selected, digest, binding = desktop.select_release_artifact(
        tmp_path, reuse_installed_r2=True
    )

    assert selected == artifact and digest == expected
    assert binding[0:2] == (artifact.stat().st_dev, artifact.stat().st_ino)
    desktop.require_reused_r2_artifact_unchanged(binding)
    replacement = artifact.with_name("replacement")
    replacement.write_bytes(artifact.read_bytes())
    replacement.chmod(0o700)
    replacement.replace(artifact)
    with pytest.raises(RuntimeError, match="reused artifact changed"):
        desktop.require_reused_r2_artifact_unchanged(binding)


def test_r3_release_reuses_bound_installed_r2_artifact_without_build_or_sign(
    monkeypatch, tmp_path
):
    desktop = load_desktop_recipe()
    app = tmp_path / "Applications/Gamja Bot.app"
    artifact = app / "Contents/MacOS/gamja-artifact"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"reviewed-r2-artifact")
    artifact.chmod(0o700)
    expected = desktop.sha256(artifact)
    monkeypatch.setattr(desktop, "R3_INSTALLED_APP", app)
    monkeypatch.setattr(desktop, "R3_ARTIFACT_SHA256", expected)
    monkeypatch.setattr(desktop, "verify_r3_installed_source", lambda: None)
    monkeypatch.setattr(
        desktop, "build_artifact_binary",
        lambda *_args: pytest.fail("r3 must not rebuild the artifact module"),
    )
    monkeypatch.setattr(
        desktop, "sign_artifact_ad_hoc",
        lambda *_args: pytest.fail("r3 must not re-sign the artifact module"),
    )

    selected, digest, binding = desktop.select_release_artifact(
        tmp_path, reuse_installed_r3=True
    )

    assert selected == artifact and digest == expected
    assert binding[0:2] == (artifact.stat().st_dev, artifact.stat().st_ino)
    desktop.require_reused_r3_artifact_unchanged(binding)
    replacement = artifact.with_name("replacement")
    replacement.write_bytes(artifact.read_bytes())
    replacement.chmod(0o700)
    replacement.replace(artifact)
    with pytest.raises(RuntimeError, match="reused artifact changed"):
        desktop.require_reused_r3_artifact_unchanged(binding)


def test_r4_release_reuses_bound_installed_r3_artifact_without_build_or_sign(
    monkeypatch, tmp_path
):
    desktop = load_desktop_recipe()
    app = tmp_path / "Applications/Gamja Bot.app"
    artifact = app / "Contents/MacOS/gamja-artifact"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"reviewed-r3-artifact")
    artifact.chmod(0o700)
    expected = desktop.sha256(artifact)
    monkeypatch.setattr(desktop, "R4_INSTALLED_APP", app)
    monkeypatch.setattr(desktop, "R4_ARTIFACT_SHA256", expected)
    monkeypatch.setattr(desktop, "verify_r4_installed_source", lambda: None)
    monkeypatch.setattr(
        desktop, "build_artifact_binary",
        lambda *_args: pytest.fail("r4 must not rebuild the artifact module"),
    )
    monkeypatch.setattr(
        desktop, "sign_artifact_ad_hoc",
        lambda *_args: pytest.fail("r4 must not re-sign the artifact module"),
    )

    selected, digest, binding = desktop.select_release_artifact(
        tmp_path, reuse_installed_r4=True
    )

    assert selected == artifact and digest == expected
    assert binding[0:2] == (artifact.stat().st_dev, artifact.stat().st_ino)
    desktop.require_reused_r4_artifact_unchanged(binding)
    replacement = artifact.with_name("replacement")
    replacement.write_bytes(artifact.read_bytes())
    replacement.chmod(0o700)
    replacement.replace(artifact)
    with pytest.raises(RuntimeError, match="reused artifact changed"):
        desktop.require_reused_r4_artifact_unchanged(binding)


def test_r5_release_reuses_bound_installed_r4_artifact_without_build_or_sign(
    monkeypatch, tmp_path
):
    desktop = load_desktop_recipe()
    app = tmp_path / "Applications/Gamja Bot.app"
    artifact = app / "Contents/MacOS/gamja-artifact"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"reviewed-r4-artifact")
    artifact.chmod(0o700)
    expected = desktop.sha256(artifact)
    monkeypatch.setattr(desktop, "R5_INSTALLED_APP", app)
    monkeypatch.setattr(desktop, "R5_ARTIFACT_SHA256", expected)
    monkeypatch.setattr(desktop, "verify_r5_installed_source", lambda: None)
    monkeypatch.setattr(
        desktop, "build_artifact_binary",
        lambda *_args: pytest.fail("r5 must not rebuild the artifact module"),
    )
    monkeypatch.setattr(
        desktop, "sign_artifact_ad_hoc",
        lambda *_args: pytest.fail("r5 must not re-sign the artifact module"),
    )

    selected, digest, binding = desktop.select_release_artifact(
        tmp_path, reuse_installed_r5=True
    )

    assert selected == artifact and digest == expected
    assert binding[0:2] == (artifact.stat().st_dev, artifact.stat().st_ino)
    desktop.require_reused_r5_artifact_unchanged(binding)
    replacement = artifact.with_name("replacement-r5")
    replacement.write_bytes(artifact.read_bytes())
    replacement.chmod(0o700)
    replacement.replace(artifact)
    with pytest.raises(RuntimeError, match="reused artifact changed"):
        desktop.require_reused_r5_artifact_unchanged(binding)


def test_r6_release_reuses_bound_installed_r5_artifact_without_build_or_sign(
    tmp_path, monkeypatch
):
    desktop = load_desktop_recipe()
    app = tmp_path / "Applications/Gamja Bot.app"
    artifact = app / "Contents/MacOS/gamja-artifact"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"reviewed-r6-artifact")
    artifact.chmod(0o700)
    expected = desktop.sha256(artifact)
    monkeypatch.setattr(desktop, "R6_INSTALLED_APP", app)
    monkeypatch.setattr(desktop, "R6_ARTIFACT_SHA256", expected)
    monkeypatch.setattr(desktop, "verify_r6_installed_source", lambda: None)
    monkeypatch.setattr(
        desktop, "build_artifact_binary",
        lambda *_args: pytest.fail("r6 must not rebuild the artifact module"),
    )
    monkeypatch.setattr(
        desktop, "sign_artifact_ad_hoc",
        lambda *_args: pytest.fail("r6 must not re-sign the artifact module"),
    )

    selected, digest, binding = desktop.select_release_artifact(
        tmp_path, reuse_installed_r6=True
    )
    assert selected == artifact and digest == expected
    assert binding[0:2] == (artifact.stat().st_dev, artifact.stat().st_ino)
    desktop.require_reused_r6_artifact_unchanged(binding)
    replacement = artifact.with_name("replacement-r6")
    replacement.write_bytes(artifact.read_bytes())
    replacement.chmod(0o700)
    replacement.replace(artifact)
    with pytest.raises(RuntimeError, match="reused artifact changed"):
        desktop.require_reused_r6_artifact_unchanged(binding)


def test_r7_release_reuses_bound_installed_r6_artifact_without_build_or_sign(
    tmp_path, monkeypatch
):
    desktop = load_desktop_recipe()
    app = tmp_path / "Applications/Gamja Bot.app"
    artifact = app / "Contents/MacOS/gamja-artifact"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"reviewed-r7-artifact")
    artifact.chmod(0o700)
    expected = desktop.sha256(artifact)
    monkeypatch.setattr(desktop, "R7_INSTALLED_APP", app)
    monkeypatch.setattr(desktop, "R7_ARTIFACT_SHA256", expected)
    monkeypatch.setattr(desktop, "verify_r7_installed_source", lambda: None)
    monkeypatch.setattr(
        desktop, "build_artifact_binary",
        lambda *_args: pytest.fail("r7 must not rebuild the artifact module"),
    )
    monkeypatch.setattr(
        desktop, "sign_artifact_ad_hoc",
        lambda *_args: pytest.fail("r7 must not re-sign the artifact module"),
    )

    selected, digest, binding = desktop.select_release_artifact(
        tmp_path, reuse_installed_r7=True
    )
    assert selected == artifact and digest == expected
    assert binding[0:2] == (artifact.stat().st_dev, artifact.stat().st_ino)
    desktop.require_reused_r7_artifact_unchanged(binding)
    replacement = artifact.with_name("replacement-r7")
    replacement.write_bytes(artifact.read_bytes())
    replacement.chmod(0o700)
    replacement.replace(artifact)
    with pytest.raises(RuntimeError, match="reused artifact changed"):
        desktop.require_reused_r7_artifact_unchanged(binding)


def test_r8_release_reuses_bound_installed_r7_artifact_without_build_or_sign(
    tmp_path, monkeypatch
):
    desktop = load_desktop_recipe()
    assert desktop.SOURCE_BASE == "68f99247039c4aacc5e7c2554a1c9a1cc7547ace"
    app = tmp_path / "Applications/Gamja Bot.app"
    artifact = app / "Contents/MacOS/gamja-artifact"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"reviewed-r8-artifact")
    artifact.chmod(0o700)
    expected = desktop.sha256(artifact)
    monkeypatch.setattr(desktop, "R8_INSTALLED_APP", app)
    monkeypatch.setattr(desktop, "R8_ARTIFACT_SHA256", expected)
    monkeypatch.setattr(desktop, "verify_r8_installed_source", lambda: None)
    monkeypatch.setattr(
        desktop, "build_artifact_binary",
        lambda *_args: pytest.fail("r8 must not rebuild the artifact module"),
    )
    monkeypatch.setattr(
        desktop, "sign_artifact_ad_hoc",
        lambda *_args: pytest.fail("r8 must not re-sign the artifact module"),
    )

    selected, digest, binding = desktop.select_release_artifact(
        tmp_path, reuse_installed_r8=True
    )
    assert selected == artifact and digest == expected
    assert binding[0:2] == (artifact.stat().st_dev, artifact.stat().st_ino)
    desktop.require_reused_r8_artifact_unchanged(binding)
    replacement = artifact.with_name("replacement-r8")
    replacement.write_bytes(artifact.read_bytes())
    replacement.chmod(0o700)
    replacement.replace(artifact)
    with pytest.raises(RuntimeError, match="reused artifact changed"):
        desktop.require_reused_r8_artifact_unchanged(binding)


def test_artifact_lock_must_be_tracked_and_clean():
    desktop = load_desktop_recipe()
    calls = []

    def clean(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    desktop.verify_artifact_lock_tracked(run=clean)
    assert [call[0] for call in calls] == [
        ["git", "ls-files", "--error-unmatch", "--", "artifact-module/Cargo.lock"],
        ["git", "diff", "--quiet", "HEAD", "--", "artifact-module/Cargo.lock"],
    ]

    with pytest.raises(RuntimeError, match="Cargo.lock"):
        desktop.verify_artifact_lock_tracked(
            run=lambda *_a, **_k: SimpleNamespace(returncode=1, stdout=b"", stderr=b"")
        )


def test_artifact_stage_rejects_alias_collision_and_removes_only_exact_copy(
    monkeypatch, tmp_path
):
    desktop = load_desktop_recipe()
    source = tmp_path / "built/gamja-artifact"
    source.parent.mkdir()
    source.write_bytes(b"signed-artifact")
    source.chmod(0o700)
    tauri = tmp_path / "tauri"
    (tauri / "binaries").mkdir(parents=True)
    monkeypatch.setattr(desktop, "TAURI", tauri)
    expected = desktop.sha256(source)

    staged, identity = desktop.stage_artifact_binary(source, expected)
    assert staged.read_bytes() == source.read_bytes()
    assert staged.stat().st_ino != source.stat().st_ino
    assert staged.stat().st_nlink == 1
    with pytest.raises(RuntimeError, match="collision"):
        desktop.stage_artifact_binary(source, expected)
    desktop.remove_staged_artifact(staged, expected, identity)
    assert not staged.exists() and source.read_bytes() == b"signed-artifact"

    outside = tmp_path / "outside"
    outside.mkdir()
    (tauri / "binaries").rmdir()
    (tauri / "binaries").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="staging"):
        desktop.stage_artifact_binary(source, expected)
    assert not list(outside.iterdir())


def test_artifact_cleanup_rejects_same_hash_inode_replacement_and_still_cleans_core(
    monkeypatch, tmp_path
):
    desktop = load_desktop_recipe()
    source = tmp_path / "built/gamja-artifact"
    source.parent.mkdir()
    source.write_bytes(b"signed-artifact")
    source.chmod(0o700)
    tauri = tmp_path / "tauri"
    (tauri / "binaries").mkdir(parents=True)
    core = tauri / "resources/gamja-core"
    core.mkdir(parents=True)
    monkeypatch.setattr(desktop, "TAURI", tauri)
    expected = desktop.sha256(source)
    staged, identity = desktop.stage_artifact_binary(source, expected)
    replacement = staged.with_name("replacement")
    replacement.write_bytes(source.read_bytes())
    replacement.chmod(0o700)
    replacement.replace(staged)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        desktop.cleanup_release_staging(
            staged_artifact=staged,
            artifact_sha256=expected,
            artifact_identity=identity,
            staged_core=core,
        )

    assert staged.exists(), "same-hash replacement must not be unlinked as owned staging"
    assert not core.exists(), "Core cleanup must run even when artifact cleanup fails"


@pytest.mark.parametrize("failure_point", ["build", "sign", "stage"])
def test_release_prebuild_failures_cleanup_staged_core(
    monkeypatch, tmp_path, failure_point
):
    import json
    import sys

    module = core_recipe(monkeypatch=monkeypatch)
    root = core_fixture(tmp_path)
    helper = root / "libexec/gamja/gamja-platform-helper"
    helper.write_bytes(b"helper")
    helper.chmod(0o700)
    module.write_manifest(root, target="aarch64-apple-darwin", version="1.0.0", provenance={})
    desktop = load_desktop_recipe()
    pin_desktop_fixture(monkeypatch, desktop, root)
    tauri = tmp_path / "tauri"
    (tauri / "binaries").mkdir(parents=True)
    (tauri / "tauri.conf.json").write_text(json.dumps({"productName": "Fixture", "version": "1.0.0"}))
    target = tmp_path / "cargo"
    target.mkdir()
    artifact = target / "aarch64-apple-darwin/release/gamja-artifact"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"artifact")
    artifact.chmod(0o700)
    artifact_hash = desktop.sha256(artifact)
    monkeypatch.setattr(desktop, "TAURI", tauri)
    monkeypatch.setattr(desktop, "DEFAULT_ARM_CORE_PACKAGE", root)
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(desktop.platform, "machine", lambda: "arm64")
    monkeypatch.setenv("CARGO_TARGET_DIR", str(target))
    monkeypatch.setattr(sys, "argv", ["build_desktop.py", "--artifact-release-app-only"])

    def fail(name):
        raise RuntimeError(name + " failed")

    monkeypatch.setattr(
        desktop, "build_artifact_binary",
        (lambda _target: fail("build")) if failure_point == "build" else (lambda _target: artifact),
    )
    monkeypatch.setattr(
        desktop, "sign_artifact_ad_hoc",
        (lambda _artifact: fail("sign")) if failure_point == "sign" else (lambda _artifact: artifact_hash),
    )
    monkeypatch.setattr(
        desktop, "stage_artifact_binary",
        (lambda *_args: fail("stage")) if failure_point == "stage"
        else lambda *_args: pytest.fail("desktop build must not continue past selected failure"),
    )

    with pytest.raises(RuntimeError, match=failure_point + " failed"):
        desktop.main()
    assert not (tauri / "resources/gamja-core").exists()


def test_candidate_receipt_is_exclusive_private_and_durable(monkeypatch, tmp_path):
    desktop = load_desktop_recipe()
    app = (tmp_path / "Gamja Bot.app").resolve()
    paths = {
        "main": app / "Contents/MacOS/gamja-bot",
        "artifact": app / "Contents/MacOS/gamja-artifact",
        "manifest": app / "Contents/Resources/gamja-core/core-manifest.json",
        "core": app / "Contents/Resources/gamja-core/bin/gamja",
        "engine": app / "Contents/Resources/gamja-core/libexec/gamja/gamja-engine",
        "helper": app / "Contents/Resources/gamja-core/libexec/gamja/gamja-platform-helper",
    }
    for role, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((role + "-bytes").encode())
        path.chmod(0o600 if role == "manifest" else 0o700)
    import plistlib
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "top.gamja.bot", "CFBundleExecutable": "gamja-bot",
    }))
    help_source = tmp_path / "Diagnostics Help.txt"
    help_source.write_text("frozen help\n")
    embedded_help = app / "Contents/Resources/Diagnostics Help.txt"
    embedded_help.write_bytes(help_source.read_bytes())
    freeze = tmp_path / "source-freeze.json"
    freeze.write_text("{}\n")
    freeze.chmod(0o600)
    artifact_root = tmp_path / "artifact-module"
    artifact_root.mkdir()
    (artifact_root / "Cargo.lock").write_text("version = 3\n")
    tauri = tmp_path / "tauri"
    tauri.mkdir()
    (tauri / "tauri.core.conf.json").write_text("{}\n")
    monkeypatch.setattr(desktop, "DIAGNOSTICS_HELP", help_source)
    monkeypatch.setattr(desktop, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(desktop, "TAURI", tauri)
    monkeypatch.setattr(desktop, "FIXED_BOOTSTRAP_MANIFEST_SHA256", desktop.sha256(paths["manifest"]))
    monkeypatch.setattr(desktop, "FIXED_BOOTSTRAP_CORE_SHA256", desktop.sha256(paths["core"]))
    monkeypatch.setattr(desktop, "FIXED_BOOTSTRAP_ENGINE_SHA256", desktop.sha256(paths["engine"]))
    monkeypatch.setattr(desktop, "FIXED_BOOTSTRAP_HELPER_SHA256", desktop.sha256(paths["helper"]))
    binding = {role: desktop.sha256(paths[role]) for role in ("manifest", "core", "engine", "helper")}
    monkeypatch.setattr(desktop, "verified_core_binding", lambda _path: binding)
    monkeypatch.setattr(desktop, "verify_artifact_binary", lambda path, digest: digest)
    fsync_calls = []
    actual_fsync = desktop.os.fsync
    monkeypatch.setattr(desktop.os, "fsync", lambda descriptor: (
        fsync_calls.append(descriptor), actual_fsync(descriptor)
    )[-1])
    receipt = tmp_path / "candidate.json"

    payload = desktop.emit_candidate_receipt(
        receipt, app, tmp_path / "verified-core", desktop.sha256(paths["artifact"]), freeze
    )

    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert len(fsync_calls) >= 2, "receipt file and parent directory must both be fsynced"
    assert set(payload["hashes"]) == {"main", "manifest", "core", "engine", "helper", "artifact"}
    assert payload["build"]["source_base"] == desktop.SOURCE_BASE
    monkeypatch.setattr(desktop, "FIXED_BOOTSTRAP_CORE_SHA256", "0" * 64)
    runtime_verifier = tmp_path / "verified-core.runtime-verifier"
    runtime_verifier.write_bytes(b"operator-built-runtime-verifier")
    runtime_verifier.chmod(0o700)
    generic = desktop.emit_candidate_receipt(
        tmp_path / "generic-candidate.json", app, tmp_path / "verified-core",
        desktop.sha256(paths["artifact"]), freeze,
        runtime_verifier=runtime_verifier,
        runtime_verifier_sha256=desktop.sha256(runtime_verifier),
    )
    assert generic["kind"] == "unsigned-gamja-gui-candidate"
    assert set(generic["core_binding"]) == {
        "manifest", "core", "engine", "helper", "runtime_verifier",
    }
    assert generic["core_binding"]["runtime_verifier"] == {
        "path": str(runtime_verifier),
        "sha256": desktop.sha256(runtime_verifier),
    }
    assert "source_base" not in generic["build"]
    assert generic["build"]["recipe_lineage_base"] == desktop.SOURCE_BASE
    assert generic["build"]["source_freeze_validation"] == "hash_only"
    before = receipt.read_bytes()
    with pytest.raises(RuntimeError, match="collision"):
        desktop.emit_candidate_receipt(
            receipt, app, tmp_path / "verified-core", desktop.sha256(paths["artifact"]), freeze
        )
    assert receipt.read_bytes() == before


@pytest.mark.parametrize("failure", [
    "missing", "path_only", "hash_only", "wrong_path", "wrong_hash", "symlink", "nonexec",
])
def test_nonhistorical_candidate_rejects_invalid_runtime_verifier_before_receipt(
    monkeypatch, tmp_path, failure
):
    desktop = load_desktop_recipe()
    app = (tmp_path / "Gamja Bot.app").resolve()
    paths = {
        "main": app / "Contents/MacOS/gamja-bot",
        "artifact": app / "Contents/MacOS/gamja-artifact",
        "manifest": app / "Contents/Resources/gamja-core/core-manifest.json",
        "core": app / "Contents/Resources/gamja-core/bin/gamja",
        "engine": app / "Contents/Resources/gamja-core/libexec/gamja/gamja-engine",
        "helper": app / "Contents/Resources/gamja-core/libexec/gamja/gamja-platform-helper",
    }
    for role, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((role + "-bytes").encode())
        path.chmod(0o600 if role == "manifest" else 0o700)
    import plistlib
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "top.gamja.bot", "CFBundleExecutable": "gamja-bot",
    }))
    help_source = tmp_path / "Diagnostics Help.txt"
    help_source.write_text("frozen help\n")
    (app / "Contents/Resources/Diagnostics Help.txt").write_bytes(help_source.read_bytes())
    freeze = tmp_path / "source-freeze.json"
    freeze.write_text("{}\n")
    freeze.chmod(0o600)
    artifact_root = tmp_path / "artifact-module"
    artifact_root.mkdir()
    (artifact_root / "Cargo.lock").write_text("version = 3\n")
    tauri = tmp_path / "tauri"
    tauri.mkdir()
    (tauri / "tauri.core.conf.json").write_text("{}\n")
    monkeypatch.setattr(desktop, "DIAGNOSTICS_HELP", help_source)
    monkeypatch.setattr(desktop, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(desktop, "TAURI", tauri)
    binding = {role: desktop.sha256(paths[role]) for role in ("manifest", "core", "engine", "helper")}
    monkeypatch.setattr(desktop, "verified_core_binding", lambda _path: binding)
    monkeypatch.setattr(desktop, "FIXED_BOOTSTRAP_CORE_SHA256", "0" * 64)
    monkeypatch.setattr(desktop, "verify_artifact_binary", lambda path, digest: digest)
    core_package = tmp_path / "verified-core"
    verifier = tmp_path / "verified-core.runtime-verifier"
    verifier.write_bytes(b"operator-built-runtime-verifier")
    verifier.chmod(0o700)
    verifier_hash = desktop.sha256(verifier)
    kwargs = {"runtime_verifier": verifier, "runtime_verifier_sha256": verifier_hash}
    if failure == "missing":
        kwargs = {}
    elif failure == "path_only":
        kwargs.pop("runtime_verifier_sha256")
    elif failure == "hash_only":
        kwargs.pop("runtime_verifier")
    elif failure == "wrong_path":
        wrong = tmp_path / "other.runtime-verifier"
        wrong.write_bytes(verifier.read_bytes())
        wrong.chmod(0o700)
        kwargs["runtime_verifier"] = wrong
    elif failure == "wrong_hash":
        kwargs["runtime_verifier_sha256"] = "0" * 64
    elif failure == "symlink":
        target = tmp_path / "real-runtime-verifier"
        verifier.replace(target)
        verifier.symlink_to(target)
    elif failure == "nonexec":
        verifier.chmod(0o600)
    receipt = tmp_path / "candidate.json"

    with pytest.raises(RuntimeError, match="runtime verifier"):
        desktop.emit_candidate_receipt(
            receipt, app, core_package, desktop.sha256(paths["artifact"]), freeze, **kwargs
        )

    assert not receipt.exists()


@pytest.mark.parametrize("arguments", [
    [],
    ["--runtime-verifier", "/absolute/core.runtime-verifier"],
    ["--runtime-verifier-sha256", "a" * 64],
])
def test_nonhistorical_app_cli_requires_paired_runtime_verifier_flags(
    monkeypatch, tmp_path, arguments
):
    desktop = load_desktop_recipe()
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    core = tmp_path / "core"
    core.mkdir()
    tauri = tmp_path / "tauri"
    tauri.mkdir()
    (tauri / "tauri.conf.json").write_text('{"productName":"Fixture"}\n')
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(desktop, "TAURI", tauri)
    monkeypatch.setattr(desktop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(desktop.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(desktop, "separate_core_source", lambda source, *_paths: source)
    monkeypatch.setenv("CARGO_TARGET_DIR", str(target))
    monkeypatch.setattr(sys, "argv", [
        "build_desktop.py", "--artifact-release-app-only", "--core-package", str(core),
        *arguments,
    ])

    with pytest.raises(SystemExit):
        desktop.main()


def test_runtime_receipt_validation_is_delegated_to_the_canonical_core_cli():
    source = (ROOT / "scripts/build_core.py").read_text()
    assert "def validate_runtime_receipt(" not in source
    assert '"runtime", "verify", "--source"' in source
    for private_receipt_field in (
        '"core_device"',
        '"core_inode"',
        '"engine_device"',
        '"engine_inode"',
    ):
        assert private_receipt_field not in source


def test_macos_package_path_records_final_binding_while_readonly_dmg_is_mounted():
    source = (ROOT / "scripts/package_macos_dmg.sh").read_text()

    bind = source.index("bind-package")
    mounted = source.index("mounted=1")
    detach = source.index('hdiutil detach "$mount"', mounted)
    publish = source.index('mv -f "$pending" "$output"')
    assert mounted < bind < detach < publish
    for argument in (
        "--signed-receipt", "--candidate-app", "--mounted-app",
        "--dmg", "--published-dmg", "--package-binding",
    ):
        assert argument in source[bind:detach]


def test_gui_and_cli_are_thin_callers_of_the_shared_runtime_authority():
    gui = (ROOT / "frontend/src-tauri/src/core_runtime.rs").read_text()
    cli = (ROOT / "core-host/src/cli.rs").read_text()
    for caller in (gui, cli):
        assert "runtime_install" in caller
    for duplicate in (
        "struct Receipt",
        "fn write_receipt",
        "fn verify_receipt",
        "fn copy_package",
        "fn publish_exclusive",
    ):
        assert duplicate not in gui


def test_artifact_linkage_and_signature_gate_rejects_unsigned_drift(monkeypatch, tmp_path):
    desktop = load_desktop_recipe()
    binary = tmp_path / "gamja-artifact"
    binary.write_bytes(b"artifact")
    binary.chmod(0o700)
    expected = desktop.sha256(binary)

    outputs = iter([
        SimpleNamespace(returncode=0, stdout="/usr/lib/libSystem.B.dylib\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="_malloc\n", stderr=""),
        SimpleNamespace(returncode=1, stdout="", stderr="unsigned"),
    ])
    with pytest.raises(RuntimeError, match="artifact signature"):
        desktop.verify_artifact_binary(binary, expected, run=lambda *_a, **_k: next(outputs))

    drifted = "0" * 64 if expected != "0" * 64 else "1" * 64
    with pytest.raises(RuntimeError, match="artifact hash"):
        desktop.verify_artifact_binary(
            binary, drifted,
            run=lambda *_a, **_k: pytest.fail("hash drift must fail before tools"),
        )

    forbidden = iter([
        SimpleNamespace(returncode=0, stdout="/usr/lib/libsqlite3.dylib\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="", stderr=""),
    ])
    with pytest.raises(RuntimeError, match="forbidden artifact linkage"):
        desktop.verify_artifact_binary(binary, expected, run=lambda *_a, **_k: next(forbidden))

# Stable broker package tests use synthetic executables and no native credential operation.
def broker_package_fixture(module, tmp_path, monkeypatch):
    root = core_fixture(tmp_path)
    private = root / 'libexec/gamja'
    broker = private / 'gamja-credential-broker'
    authority = private / 'gamja-credential-release-authority'
    broker.write_bytes(b'fixed-synthetic-broker')
    authority.write_bytes(b'public-authority')
    for path in [root / 'bin/gamja', private / 'gamja-engine', broker, authority]:
        path.chmod(0o700)
    monkeypatch.setattr(module, 'ACCEPTED_BROKER_SHA256', module.sha256(broker), raising=False)
    claims = {'protocol_version': 1, 'build_id': 'test-broker-v1', 'broker_sha256': module.sha256(broker),
              'core_sha256': module.sha256(root / 'bin/gamja'), 'engine_sha256': module.sha256(private / 'gamja-engine')}
    monkeypatch.setattr(module, '_read_broker_authority', lambda path: dict(claims), raising=False)
    return root, claims


def test_stable_broker_manifest_and_ordinary_verifier_bind_all_four_files(monkeypatch, tmp_path):
    module = core_recipe()
    root, claims = broker_package_fixture(module, tmp_path, monkeypatch)
    module.write_manifest(root, target='aarch64-apple-darwin', version='1', provenance={'build_id': claims['build_id']})
    data = module.verify_package(root, 'aarch64-apple-darwin', require_credential_signature=False)
    assert data['credential_broker'] == {'version': 1, 'build_id': claims['build_id'],
        'broker_sha256': claims['broker_sha256'], 'authority_sha256': module.sha256(root / 'libexec/gamja/gamja-credential-release-authority')}
    assert 'manifest_sha256' not in claims
    assert module.inventory(root) == data['files']


@pytest.mark.parametrize('field', ['build_id', 'broker_sha256', 'core_sha256', 'engine_sha256'])
def test_stable_broker_ordinary_verify_rejects_drifted_authority(monkeypatch, tmp_path, field):
    module = core_recipe()
    root, claims = broker_package_fixture(module, tmp_path, monkeypatch)
    module.write_manifest(root, target='aarch64-apple-darwin', version='1', provenance={'build_id': claims['build_id']})
    claims[field] = 'wrong-id' if field == 'build_id' else 'a' * 64
    with pytest.raises(ValueError, match='authority|broker'):
        module.verify_package(root, 'aarch64-apple-darwin', require_credential_signature=False)


@pytest.mark.parametrize('case', ['partial', 'nonarm', 'metadata_extra', 'metadata_bool', 'tampered'])
def test_stable_broker_rejects_invalid_pair_even_without_signature(monkeypatch, tmp_path, case):
    module = core_recipe()
    root, claims = broker_package_fixture(module, tmp_path, monkeypatch)
    module.write_manifest(root, target='aarch64-apple-darwin', version='1', provenance={'build_id': claims['build_id']})
    target = 'aarch64-apple-darwin'
    if case == 'partial':
        (root / 'libexec/gamja/gamja-credential-release-authority').unlink()
    elif case == 'nonarm':
        target = 'x86_64-apple-darwin'
        data = json.loads((root / module.MANIFEST).read_text()); data['target'] = target
        (root / module.MANIFEST).write_text(json.dumps(data))
    elif case == 'tampered':
        (root / 'libexec/gamja/gamja-credential-broker').write_bytes(b'tampered')
        with pytest.raises(ValueError, match='broker'):
            module.write_manifest(root, target=target, version='1', provenance={'build_id': claims['build_id']})
        return
    else:
        data = json.loads((root / module.MANIFEST).read_text())
        if case == 'metadata_extra': data['credential_broker']['extra'] = True
        else: data['credential_broker']['version'] = True
        (root / module.MANIFEST).write_text(json.dumps(data))
    with pytest.raises(ValueError, match='broker|inventory|coverage'):
        module.verify_package(root, target, require_credential_signature=False)


@pytest.mark.parametrize('case', ['bool', 'extra', 'duplicate', 'stderr', 'long', 'wrong_hash'])
def test_stable_broker_authority_parser_is_closed_and_bounded(monkeypatch, tmp_path, case):
    module = core_recipe()
    data = {'protocol_version': 1, 'build_id': 'test-v1', 'broker_sha256': 'a'*64, 'core_sha256': 'b'*64, 'engine_sha256': 'c'*64}
    if case == 'bool': data['protocol_version'] = True
    if case == 'extra': data['extra'] = 'not accepted'
    if case == 'wrong_hash': data['core_sha256'] = 'A'*64
    body = json.dumps(data).encode()
    if case == 'duplicate': body = body[:-1] + b',"protocol_version":1}'
    if case == 'long': body = b' ' * 4097
    observed = []
    def run(argv, **kwargs):
        observed.append((argv,kwargs)); return 0,body,b'error' if case == 'stderr' else b''
    monkeypatch.setattr(module, '_run_bounded_core_cli', run)
    with pytest.raises(ValueError, match='authority'):
        module._read_broker_authority(tmp_path / 'authority')
    assert observed == [([str(tmp_path / 'authority')], {'timeout_seconds': 10})]


def test_stable_broker_source_rejects_symlink_ancestors_and_wrong_bytes(monkeypatch, tmp_path):
    module = core_recipe()
    folder = tmp_path / 'real'; folder.mkdir(); broker = folder / 'broker'; broker.write_bytes(b'fixed'); broker.chmod(0o700)
    monkeypatch.setattr(module, 'ACCEPTED_BROKER_SHA256', module.sha256(broker), raising=False)
    module._validate_stable_broker(broker)
    alias = tmp_path / 'alias'; alias.symlink_to(folder, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink|broker'):
        module._validate_stable_broker(alias / 'broker')
    broker.write_bytes(b'changed')
    with pytest.raises(ValueError, match='broker'):
        module._validate_stable_broker(broker)


@pytest.mark.parametrize('failure', [None, 'receipt_publish', 'source_drift', 'output_collision'])
def test_stable_broker_build_exercises_real_copy_manifest_verification_and_receipt(monkeypatch, tmp_path, failure):
    module = core_recipe()
    source = core_fixture(tmp_path)
    for path in [source / 'bin/gamja', source / 'libexec/gamja/gamja-engine']: path.chmod(0o700)
    module.write_manifest(source, target='aarch64-apple-darwin', version='1', provenance={})
    engine_sha = module.sha256(source / 'libexec/gamja/gamja-engine')
    manifest_sha = module.sha256(source / module.MANIFEST)
    broker = tmp_path / 'accepted-broker'; broker.write_bytes(b'fixed-broker'); broker.chmod(0o700)
    monkeypatch.setattr(module, 'ACCEPTED_BROKER_SHA256', module.sha256(broker), raising=False)
    target = tmp_path / 'target'; release = target / 'aarch64-apple-darwin/release'; release.mkdir(parents=True)
    monkeypatch.setenv('CARGO_TARGET_DIR', str(target))
    monkeypatch.setattr(module.platform, 'system', lambda:'Darwin'); monkeypatch.setattr(module.platform, 'machine', lambda:'arm64')
    monkeypatch.setattr(module, 'validate_macos_signing_inputs', lambda:None)
    monkeypatch.setattr(module, 'read_secret_fd', lambda fd:bytearray(b'synthetic-only'))
    monkeypatch.setattr(module, 'build_input_paths', lambda **kwargs:[])
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *a, **k:'source-head\n')
    builds=[]; signs=[]
    def compile_(argv, **kwargs):
        builds.append((argv,dict(kwargs['env'])))
        binary = argv[argv.index('--bin')+1]
        if binary == 'gamja': content=b'new-core'
        else:
            env=kwargs['env']; content=json.dumps({'protocol_version':1,'build_id':env['GAMJA_BUILD_ID'],'broker_sha256':env['GAMJA_BROKER_SHA256'],'core_sha256':env['GAMJA_CORE_SHA256'],'engine_sha256':env['GAMJA_ENGINE_SHA256']}).encode()
        f=release / binary;f.write_bytes(content);f.chmod(0o700)
        if binary == 'gamja-credential-release-authority' and failure == 'source_drift':
            broker.write_bytes(b'drifted')
        if binary == 'gamja-credential-release-authority' and failure == 'output_collision':
            (tmp_path / 'output').mkdir()
            (tmp_path / 'output/owner-file').write_bytes(b'keep')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess,'run',compile_)
    def sign(path, role, password): signs.append(role);path.write_bytes(path.read_bytes()+b'#signed')
    monkeypatch.setattr(module,'sign_macos_binary',sign)
    monkeypatch.setattr(module,'_read_broker_authority',lambda path:json.loads(path.read_bytes().split(b'#signed')[0]))
    output=tmp_path/'output';receipt=tmp_path/'receipts/build.json'
    if failure == 'receipt_publish':
        def reject_receipt(*args):
            receipt.write_bytes(b'preexisting-owner-receipt')
            raise ValueError('receipt publication collision')
        monkeypatch.setattr(module, 'publish_exclusive_file', reject_receipt)
    if failure is not None:
        with pytest.raises(ValueError):
            module.build_stable_broker_core_only(source,output,expected_manifest_sha256=manifest_sha,expected_engine_sha256=engine_sha,broker_source=broker,core_signing_password_fd=8,build_id='test-broker-v1',public_receipt_path=receipt)
        assert module.sha256(source / module.MANIFEST) == manifest_sha
        assert module.sha256(source / 'libexec/gamja/gamja-engine') == engine_sha
        if failure == 'output_collision':
            assert (output / 'owner-file').read_bytes() == b'keep'
        else:
            assert not output.exists()
        if failure == 'receipt_publish':
            assert receipt.read_bytes() == b'preexisting-owner-receipt'
        else:
            assert not receipt.exists()
        assert not list(tmp_path.glob('.gamja-broker-package-*'))
        return
    module.build_stable_broker_core_only(source,output,expected_manifest_sha256=manifest_sha,expected_engine_sha256=engine_sha,broker_source=broker,core_signing_password_fd=8,build_id='test-broker-v1',public_receipt_path=receipt)
    assert [args[args.index('--bin')+1] for args,env in builds] == ['gamja','gamja-credential-release-authority']
    assert signs == [module.MAC_CORE_IDENTIFIER,module.MAC_RELEASE_AUTHORITY_IDENTIFIER]
    assert module.MAC_BROKER_IDENTIFIER not in signs
    assert all('--offline' in args and '--locked' in args and '-j2' in args for args,env in builds)
    assert builds[1][1]['GAMJA_CORE_SHA256'] == module.sha256(output/'bin/gamja')
    assert (output/'libexec/gamja/gamja-credential-broker').read_bytes() == broker.read_bytes()
    assert (output/'libexec/gamja/gamja-credential-broker').stat().st_nlink == 1
    assert module.sha256(output/'libexec/gamja/gamja-engine') == engine_sha
    record=json.loads(receipt.read_text())
    assert record['manifest_sha256']==module.sha256(output/module.MANIFEST)
    assert record['authority_sha256']==module.sha256(output/'libexec/gamja/gamja-credential-release-authority')
    assert record['broker_sha256']==module.sha256(broker)
    assert stat.S_IMODE(receipt.stat().st_mode)==0o600 and receipt.stat().st_nlink==1
    assert module.verify_package(output,'aarch64-apple-darwin')['credential_broker']['build_id']=='test-broker-v1'
    assert source.joinpath(module.MANIFEST).read_bytes() != output.joinpath(module.MANIFEST).read_bytes()


@pytest.mark.parametrize('case', ['inside_output', 'inside_source', 'existing', 'symlink_parent'])
def test_stable_broker_receipt_preflight_preserves_owner_files(monkeypatch, tmp_path, case):
    module = core_recipe()
    source = core_fixture(tmp_path)
    output = tmp_path / 'out'
    receipt = tmp_path / 'receipt.json'
    monkeypatch.setattr(module.platform, 'system', lambda:'Darwin')
    monkeypatch.setattr(module.platform, 'machine', lambda:'arm64')
    monkeypatch.setattr(module, 'read_secret_fd', lambda *_:pytest.fail('no secret may be read'))
    if case == 'inside_output': receipt = output / 'build.json'
    if case == 'inside_source': receipt = source / 'build.json'
    if case == 'existing': receipt.write_bytes(b'keep')
    if case == 'symlink_parent':
        real = tmp_path / 'real'; real.mkdir()
        alias = tmp_path / 'alias'; alias.symlink_to(real, target_is_directory=True)
        receipt = alias / 'build.json'
    with pytest.raises(ValueError, match='receipt|symlink'):
        module.build_stable_broker_core_only(source, output, expected_manifest_sha256='a'*64,
            expected_engine_sha256='b'*64, broker_source=tmp_path/'unused',
            core_signing_password_fd=9, build_id='test-broker-v1', public_receipt_path=receipt)
    if case == 'existing': assert receipt.read_bytes() == b'keep'
    assert not output.exists()


@pytest.mark.parametrize('role', ['broker', 'core', 'release-authority'])
def test_stable_broker_native_platform_gate_exempts_only_exact_accepted_broker(monkeypatch, tmp_path, role):
    module = core_recipe(verify_signatures=True)
    binary = tmp_path / 'binary'; binary.write_bytes(b'fixed-synthetic-native-code')
    cert = tmp_path / 'leaf.cer'; cert.write_bytes(b'pinned-leaf')
    monkeypatch.setattr(module, 'MAC_CORE_CERTIFICATE', cert)
    monkeypatch.setattr(module, 'ACCEPTED_BROKER_SHA256', module.sha256(binary))
    monkeypatch.setattr(module, '_macho_arches', lambda *_:['arm64'])
    monkeypatch.setattr(module, '_read_macos_entitlements', lambda *_:{})
    dependencies=[]
    monkeypatch.setattr(module, '_require_system_dependencies_only', lambda path:dependencies.append(path))
    identifier={'broker':module.MAC_BROKER_IDENTIFIER, 'core':module.MAC_CORE_IDENTIFIER,
                'release-authority':module.MAC_RELEASE_AUTHORITY_IDENTIFIER}[role]
    def native(argv, **kwargs):
        stdout=''
        if argv[0]=='/usr/bin/otool': stdout='Load command 0\n cmd LC_BUILD_VERSION\n platform 1\n minos 11.0\n sdk 27.0\n'
        elif '--verbose=4' in argv: stdout=f'Identifier={identifier}\nCodeDirectory v=20500 flags=0x10000(runtime)\n'
        else:
            prefix=next((x.split('=',1)[1] for x in argv if x.startswith('--extract-certificates=')),None)
            if prefix: Path(prefix+'0').write_bytes(cert.read_bytes())
        return SimpleNamespace(returncode=0, stdout=stdout, stderr='')
    monkeypatch.setattr(module.subprocess, 'run', native)
    if role=='broker':
        assert module.verify_macos_signature(binary, identifier)==['arm64']
        assert dependencies==[binary]
        binary.write_bytes(b'drifted-synthetic-code')
        with pytest.raises(ValueError, match='broker.*artifact'):
            module.verify_macos_signature(binary, identifier)
    else:
        with pytest.raises(ValueError, match='macos_minimum_too_old'):
            module.verify_macos_signature(binary, identifier)
