"""Build a native, frontend-independent core prefix using shared toolchain caches.

No downloads, Tauri, frontend build, installation or publication occur here.
The optional helper is native OS integration; browser runtime JS stays inside the
pinned Chromium payload/frozen engine and is not a GUI resource.
"""
import argparse
import ctypes
import hashlib
import importlib.metadata as metadata
import importlib.util
import json
import os
import platform
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_CONSTRAINTS_SPEC = importlib.util.spec_from_file_location(
    "gamja_macos_engine_constraints", ROOT / "scripts/macos_engine_constraints.py")
MAC_CONSTRAINTS = importlib.util.module_from_spec(_CONSTRAINTS_SPEC)
_CONSTRAINTS_SPEC.loader.exec_module(MAC_CONSTRAINTS)
TARGETS = {("Darwin", "arm64"): "aarch64-apple-darwin", ("Darwin", "x86_64"): "x86_64-apple-darwin", ("Windows", "AMD64"): "x86_64-pc-windows-msvc"}
PYTHON_VERSION = (3, 11, 15)
PINS = {"playwright": "1.58.0", "pyinstaller": "6.22.3", "pyinstaller-hooks-contrib": "2026.7"}
BROWSER_REVISIONS = {"chromium": "1208", "chromium-headless-shell": "1208"}
MANIFEST = "core-manifest.json"
MAC_CORE_CERTIFICATE = ROOT / "core-host/macos-credential-core.cer"
MAC_CORE_IDENTIFIER = "top.gamja.credential-core"
MAC_ENGINE_IDENTIFIER = "top.gamja.engine"
MAC_LIBRARY_IDENTIFIER = MAC_CONSTRAINTS.LIBRARY_IDENTIFIER
MAC_BROKER_IDENTIFIER = "top.gamja.credential-broker.v1"
MAC_RELEASE_AUTHORITY_IDENTIFIER = "top.gamja.credential-release-authority.v1"
MAC_SLACK_BROKER_IDENTIFIER = "top.gamja.slack-credential-broker.v1"
MAC_SLACK_RELEASE_AUTHORITY_IDENTIFIER = "top.gamja.slack-credential-release-authority.v1"
MAC_MCP_BROKER_IDENTIFIER = "top.gamja.mcp-credential-broker.v1"
MAC_MCP_RELEASE_AUTHORITY_IDENTIFIER = "top.gamja.mcp-credential-release-authority.v1"
MAC_PUBLISHER_BROKER_IDENTIFIER = "top.gamja.template-publisher-credential-broker.v1"
MAC_PUBLISHER_RELEASE_AUTHORITY_IDENTIFIER = "top.gamja.template-publisher-credential-release-authority.v1"
MAC_VAULT_BROKER_IDENTIFIER = "top.gamja.vault-credential-broker.v1"
MAC_VAULT_RELEASE_AUTHORITY_IDENTIFIER = "top.gamja.vault-credential-release-authority.v1"
# Qualified StageA MCP broker; keep these bytes stable across Core/GUI rebuilds.
# Set only from the first verified, signed Slack broker artifact.
ACCEPTED_SLACK_BROKER_SHA256 = "e45c1e629155df8cb80c6b3f40a21b5f0f9120b93699867910e2c773f116192a"
ACCEPTED_MCP_BROKER_SHA256 = "5c7b2de05bed758ade133d9d7a15eff8ab14d3b4483e914314536e0a237d1587"
ACCEPTED_PUBLISHER_BROKER_SHA256 = "6b9f35b37be97a6eacb933f8d6af92c49faec4c538c6666079cbce500c8ba999"
ACCEPTED_BROKER_SHA256 = "2aa9a05ee834302b8eee08589642844611d1ad013248902f1ebd1813c1419553"
BROKER_PATH = "libexec/gamja/gamja-credential-broker"
AUTHORITY_PATH = "libexec/gamja/gamja-credential-release-authority"
SLACK_BROKER_PATH = "libexec/gamja/gamja-slack-credential-broker"
SLACK_AUTHORITY_PATH = "libexec/gamja/gamja-slack-credential-release-authority"
MCP_BROKER_PATH = "libexec/gamja/gamja-mcp-credential-broker"
MCP_AUTHORITY_PATH = "libexec/gamja/gamja-mcp-credential-release-authority"
PUBLISHER_BROKER_PATH = "libexec/gamja/gamja-template-publisher-credential-broker"
PUBLISHER_AUTHORITY_PATH = "libexec/gamja/gamja-template-publisher-credential-release-authority"
VAULT_BROKER_PATH = "libexec/gamja/gamja-vault-credential-broker"
VAULT_AUTHORITY_PATH = "libexec/gamja/gamja-vault-credential-release-authority"
MAC_SIGNER_SOURCE = ROOT / "scripts/macos_exact_signer.c"
SIGNER_TIMEOUT = 30
BUILD_HARD_FLOOR = 2 * 1024**3


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _real_owned_file(path, label, *, private=False):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a real file")
    status = path.stat()
    if status.st_uid != os.geteuid() or status.st_nlink != 1 or status.st_mode & 0o022:
        raise ValueError(f"{label} ownership or permissions are unsafe")
    if private and status.st_mode & 0o077:
        raise ValueError(f"{label} must be private")
    return path


def _real_owned_directory(path, label):
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} must be a real directory")
    status = path.stat()
    if status.st_uid != os.geteuid() or status.st_mode & 0o022:
        raise ValueError(f"{label} ownership or permissions are unsafe")
    if path.absolute() != path.resolve(strict=True):
        raise ValueError(f"{label} may not have symlink ancestors")
    return path


def mac_signing_fingerprint():
    certificate = _real_owned_file(MAC_CORE_CERTIFICATE, "pinned Core signing certificate")
    return hashlib.sha1(certificate.read_bytes()).hexdigest().upper()


def mac_designated_requirement(identifier):
    if identifier not in {
        MAC_CORE_IDENTIFIER,
        MAC_ENGINE_IDENTIFIER,
        MAC_LIBRARY_IDENTIFIER,
        MAC_BROKER_IDENTIFIER,
        MAC_RELEASE_AUTHORITY_IDENTIFIER,
        MAC_MCP_BROKER_IDENTIFIER,
        MAC_MCP_RELEASE_AUTHORITY_IDENTIFIER,
        MAC_SLACK_BROKER_IDENTIFIER,
        MAC_SLACK_RELEASE_AUTHORITY_IDENTIFIER,
        MAC_PUBLISHER_BROKER_IDENTIFIER,
        MAC_PUBLISHER_RELEASE_AUTHORITY_IDENTIFIER,
        MAC_VAULT_BROKER_IDENTIFIER,
        MAC_VAULT_RELEASE_AUTHORITY_IDENTIFIER,
    }:
        raise ValueError("unapproved macOS signing identifier")
    return f'designated => anchor = H"{mac_signing_fingerprint()}" and identifier "{identifier}"'


def mac_requirement_expression(identifier):
    requirement = mac_designated_requirement(identifier)
    return requirement.removeprefix("designated => ")


def _macho_arches(binary):
    result = subprocess.run(["/usr/bin/lipo", "-archs", str(binary)],
                            capture_output=True, text=True)
    arches = result.stdout.split()
    if result.returncode or not arches or len(arches) != len(set(arches)):
        raise ValueError("canonical macOS architecture inventory invalid")
    return arches


def _read_macos_entitlements(binary, arch):
    result = subprocess.run(
        ["/usr/bin/codesign", "-d", "--arch", arch, "--entitlements", "-", "--xml",
         str(binary)], capture_output=True,
    )
    if result.returncode:
        raise ValueError("canonical macOS entitlements read failed")
    if not result.stdout.strip():
        return {}
    import plistlib
    try:
        return plistlib.loads(result.stdout)
    except Exception as error:
        raise ValueError("canonical macOS entitlements invalid") from error


def verify_macos_signature(binary, identifier):
    binary = Path(binary)
    if identifier == MAC_BROKER_IDENTIFIER and sha256(binary) != ACCEPTED_BROKER_SHA256:
        raise ValueError("credential broker accepted artifact mismatch")
    if identifier == MAC_SLACK_BROKER_IDENTIFIER and sha256(binary) != _accepted_slack_broker_pin():
        raise ValueError("Slack credential broker accepted artifact mismatch")
    if identifier == MAC_MCP_BROKER_IDENTIFIER and sha256(binary) != _accepted_mcp_broker_pin():
        raise ValueError("MCP credential broker accepted artifact mismatch")
    if identifier == MAC_PUBLISHER_BROKER_IDENTIFIER and sha256(binary) != _accepted_publisher_broker_pin():
        raise ValueError("Publisher credential broker accepted artifact mismatch")
    if identifier == MAC_VAULT_BROKER_IDENTIFIER and sha256(binary) != _accepted_vault_broker_pin():
        raise ValueError("Vault credential broker accepted artifact mismatch")
    arches = _macho_arches(binary)
    if identifier in {
        MAC_CORE_IDENTIFIER,
        MAC_ENGINE_IDENTIFIER,
        MAC_BROKER_IDENTIFIER,
        MAC_RELEASE_AUTHORITY_IDENTIFIER,
        MAC_SLACK_BROKER_IDENTIFIER,
        MAC_SLACK_RELEASE_AUTHORITY_IDENTIFIER,
        MAC_MCP_BROKER_IDENTIFIER,
        MAC_MCP_RELEASE_AUTHORITY_IDENTIFIER,
        MAC_PUBLISHER_BROKER_IDENTIFIER,
        MAC_PUBLISHER_RELEASE_AUTHORITY_IDENTIFIER,
        MAC_VAULT_BROKER_IDENTIFIER,
        MAC_VAULT_RELEASE_AUTHORITY_IDENTIFIER,
    } and len(arches) != 1:
        raise ValueError("native credential executables require a single architecture")
    expression = mac_requirement_expression(identifier)
    entitlement_values = []
    for arch in arches:
        verified = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--strict", "--arch", arch,
             f"-R={expression}", str(binary)], capture_output=True, text=True)
        if verified.returncode:
            raise ValueError("canonical macOS signature requirement failed")
        details = subprocess.run(
            ["/usr/bin/codesign", "-d", "--verbose=4", "--arch", arch, str(binary)],
            capture_output=True, text=True)
        detail_lines = set((details.stdout + details.stderr).splitlines())
        if (details.returncode or f"Identifier={identifier}" not in detail_lines
                or not any(line.startswith("CodeDirectory v=")
                           and "flags=0x10000(runtime)" in line for line in detail_lines)
                or any(line.startswith("Timestamp=") for line in detail_lines)):
            raise ValueError("canonical macOS hardened signature policy mismatch")
        with tempfile.TemporaryDirectory(prefix=".gamja-core-cert-") as temporary:
            prefix = Path(temporary) / "certificate"
            extracted = subprocess.run(
                ["/usr/bin/codesign", "-d", "--arch", arch,
                 f"--extract-certificates={prefix}", str(binary)],
                capture_output=True, text=True)
            leaf = prefix.with_name(prefix.name + "0")
            if (extracted.returncode or not leaf.is_file()
                    or leaf.read_bytes() != MAC_CORE_CERTIFICATE.read_bytes()):
                raise ValueError("canonical macOS signing certificate mismatch")
        entitlement_values.append(_read_macos_entitlements(binary, arch))
    if identifier != MAC_LIBRARY_IDENTIFIER:
        build_version = subprocess.run(["/usr/bin/otool", "-l", str(binary)],
                                       capture_output=True, text=True)
        if build_version.returncode:
            raise ValueError("canonical macOS build version read failed")
        # Preserve the immutable legacy broker's accepted minos11 bytes/caller.
        # New MCP broker/authority and all other executable roles require macOS14.
        if identifier != MAC_BROKER_IDENTIFIER:
            MAC_CONSTRAINTS.require_macos14_build_version(build_version.stdout)
        _require_system_dependencies_only(binary)
    if identifier == MAC_ENGINE_IDENTIFIER:
        slots = MAC_CONSTRAINTS.constraint_slots_from_thin_macho(binary)
        MAC_CONSTRAINTS.require_final_policy("engine", entitlement_values[0], slots)
    elif identifier == MAC_CORE_IDENTIFIER:
        slots = MAC_CONSTRAINTS.constraint_slots_from_thin_macho(binary)
        MAC_CONSTRAINTS.require_final_policy("core", entitlement_values[0], slots)
    elif identifier == MAC_LIBRARY_IDENTIFIER:
        if any(entitlement_values):
            raise ValueError("embedded library signature policy invalid")
    elif identifier in {MAC_BROKER_IDENTIFIER, MAC_RELEASE_AUTHORITY_IDENTIFIER,
                        MAC_SLACK_BROKER_IDENTIFIER, MAC_SLACK_RELEASE_AUTHORITY_IDENTIFIER,
                        MAC_MCP_BROKER_IDENTIFIER, MAC_MCP_RELEASE_AUTHORITY_IDENTIFIER,
                        MAC_PUBLISHER_BROKER_IDENTIFIER, MAC_PUBLISHER_RELEASE_AUTHORITY_IDENTIFIER,
                        MAC_VAULT_BROKER_IDENTIFIER, MAC_VAULT_RELEASE_AUTHORITY_IDENTIFIER}:
        if any(entitlement_values):
            raise ValueError("credential authority signature policy invalid")
    else:
        raise ValueError("unapproved macOS signing identifier")
    return arches


def clear_secret(value):
    for index in range(len(value)):
        value[index] = 0


SIGNER_FAILURES = {
    "SecCodeSigner SPI unavailable": "dynamic_spi_unavailable",
    "Security framework unavailable": "dynamic_spi_unavailable",
    "SecCodeSigner SPI constant unavailable": "dynamic_spi_unavailable",
    "SPI signing failed": "sign_failed",
    "signed code exact requirement failed": "verify_failed",
    "signed code information failed": "verify_failed",
    "signed code policy mismatch": "verify_failed",
}


def classify_signer_failure(stderr):
    """Discard raw helper stderr and return only a fixed stage plus numeric OSStatus."""
    text = stderr if isinstance(stderr, str) else ""
    if len(text.encode("utf-8", errors="ignore")) > 256:
        return "unclassified", None
    match = re.fullmatch(r"([^\n:]{1,120})(?:: OSStatus=(-?[0-9]{1,12}))?\n?", text)
    if not match:
        return "unclassified", None
    stage = SIGNER_FAILURES.get(match.group(1), "preflight_failed")
    return stage, int(match.group(2)) if match.group(2) is not None else None


def read_secret_fd(descriptor):
    if descriptor is None or descriptor < 3:
        raise ValueError("dedicated signing password descriptor required")
    value = bytearray()
    try:
        while True:
            if len(value) >= 4096:
                raise ValueError("signing password is too large")
            block = os.read(descriptor, 4096 - len(value))
            if not block:
                break
            value.extend(block)
    finally:
        os.close(descriptor)
    if not value:
        raise ValueError("signing password is empty")
    return value


def _owned_process_group_absent(pid):
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return
    except PermissionError:
        raise ValueError("native signer process group is unverifiable") from None
    os.killpg(pid, signal.SIGKILL)
    raise ValueError("native signer left a child process")


def _run_macos_signer(arguments, password, expected_stdout, *, supervised_group=False):
    read_fd, write_fd = os.pipe()
    child = None
    try:
        os.write(write_fd, password)
        os.close(write_fd)
        write_fd = -1
        argv = [value if value != "{password_fd}" else str(read_fd) for value in arguments]
        child = subprocess.Popen(argv, pass_fds=(read_fd,), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=not supervised_group)
        try:
            stdout, stderr = child.communicate(timeout=SIGNER_TIMEOUT)
        except subprocess.TimeoutExpired:
            (child.kill if supervised_group else lambda: os.killpg(child.pid, signal.SIGKILL))()
            child.communicate(timeout=2)
            raise ValueError("native signer timed out") from None
        if not supervised_group:
            _owned_process_group_absent(child.pid)
        if child.returncode:
            stage, status = classify_signer_failure(stderr)
            suffix = f" OSStatus={status}" if status is not None else ""
            raise ValueError(f"native signer failed ({stage}{suffix})")
        if stdout != expected_stdout or stderr:
            raise ValueError("native signer returned unexpected output")
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        try:
            os.close(read_fd)
        except OSError:
            pass
        if child is not None and child.poll() is None:
            (child.kill if supervised_group else lambda: os.killpg(child.pid, signal.SIGKILL))()
            child.wait(timeout=2)


def sign_macos_binary(binary, identifier, password, *, supervised_group=False):
    keychain, signer, signer_hash = validate_macos_signing_inputs()
    modes = {MAC_ENGINE_IDENTIFIER: "engine", MAC_CORE_IDENTIFIER: "core",
             MAC_LIBRARY_IDENTIFIER: "library", MAC_BROKER_IDENTIFIER: "broker",
             MAC_RELEASE_AUTHORITY_IDENTIFIER: "release-authority",
             MAC_SLACK_BROKER_IDENTIFIER: "slack-broker",
             MAC_SLACK_RELEASE_AUTHORITY_IDENTIFIER: "slack-release-authority",
             MAC_MCP_BROKER_IDENTIFIER: "mcp-broker",
             MAC_MCP_RELEASE_AUTHORITY_IDENTIFIER: "mcp-release-authority",
             MAC_PUBLISHER_BROKER_IDENTIFIER: "publisher-broker",
             MAC_PUBLISHER_RELEASE_AUTHORITY_IDENTIFIER: "publisher-release-authority",
             MAC_VAULT_BROKER_IDENTIFIER: "vault-broker",
             MAC_VAULT_RELEASE_AUTHORITY_IDENTIFIER: "vault-release-authority"}
    if identifier not in modes:
        clear_secret(password)
        raise ValueError("unapproved macOS signing identifier")
    try:
        arguments = [
            str(signer), "sign", modes[identifier], str(keychain), str(MAC_CORE_CERTIFICATE),
            "{password_fd}", signer_hash, str(Path(binary).absolute()),
        ]
        if supervised_group:
            _run_macos_signer(
                arguments, password, f"signed-{modes[identifier]}-exact-identity:1\n",
                supervised_group=True,
            )
        else:
            _run_macos_signer(
                arguments, password, f"signed-{modes[identifier]}-exact-identity:1\n")
    finally:
        clear_secret(password)
    # The signer locks before exiting; public verification happens after process exit.
    verify_macos_signature(binary, identifier)


def validate_macos_signing_inputs():
    keychain_name = os.environ.get("GAMJA_CORE_SIGNING_KEYCHAIN")
    if not keychain_name:
        raise ValueError("set GAMJA_CORE_SIGNING_KEYCHAIN to the private build keychain")
    keychain = _real_owned_file(keychain_name, "Core signing keychain", private=True)
    signer_name = os.environ.get("GAMJA_CORE_SIGNING_HELPER")
    signer_digest = os.environ.get("GAMJA_CORE_SIGNING_HELPER_SHA256", "")
    if not signer_name or not re.fullmatch(r"[0-9a-f]{64}", signer_digest):
        raise ValueError("exact native signing helper is required")
    signer = _real_owned_file(signer_name, "exact native signing helper", private=True)
    if sha256(signer) != signer_digest:
        raise ValueError("exact native signing helper changed")
    mac_signing_fingerprint()
    return keychain, signer, signer_digest


def validate_supervised_signing(parent_pid, target, engine_fd, core_fd):
    """Accept containment only from the exact isolated supervisor process group."""
    if parent_pid is None:
        return False
    if (platform.system() != "Darwin" or platform.machine() != "arm64"
            or target != "aarch64-apple-darwin"
            or not isinstance(parent_pid, int) or parent_pid <= 1
            or os.getppid() != parent_pid
            or os.getsid(0) != os.getpid() or os.getpgrp() != os.getpid()
            or not isinstance(engine_fd, int) or engine_fd < 3
            or not isinstance(core_fd, int) or core_fd < 3):
        raise ValueError("invalid supervised signing context")
    try:
        os.fstat(engine_fd)
        os.fstat(core_fd)
    except OSError:
        raise ValueError("invalid supervised signing descriptors") from None
    return True


def inspect_macos_library(path):
    values = verify_macos_signature(path, MAC_LIBRARY_IDENTIFIER)
    result = {}
    for arch in values:
        details = subprocess.run(
            ["/usr/bin/codesign", "-d", "--verbose=4", "--arch", arch, str(path)],
            capture_output=True, text=True,
        )
        if details.returncode:
            raise ValueError("embedded Mach-O signature details unavailable")
        result[arch] = MAC_CONSTRAINTS.strongest_sha256_cdhash(
            details.stdout + details.stderr)
    return {"arches": result}


def inspect_macos_playwright_node(path):
    path = Path(path)
    arches = _macho_arches(path)
    result = {}
    requirement = ('anchor apple generic and identifier "node" and '
                   'certificate leaf[subject.OU] = "HX7739G8FX"')
    for arch in arches:
        verified = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--strict", "--arch", arch,
             f"-R={requirement}", str(path)], capture_output=True, text=True)
        details = subprocess.run(
            ["/usr/bin/codesign", "-d", "--verbose=4", "--arch", arch, str(path)],
            capture_output=True, text=True)
        lines = set((details.stdout + details.stderr).splitlines())
        if (verified.returncode or details.returncode or "Identifier=node" not in lines
                or "TeamIdentifier=HX7739G8FX" not in lines
                or not any(line.startswith("CodeDirectory v=")
                           and "flags=0x10000(runtime)" in line for line in lines)):
            raise ValueError("Playwright Node upstream signature policy mismatch")
        if _read_macos_entitlements(path, arch) != MAC_CONSTRAINTS.PLAYWRIGHT_NODE_ENTITLEMENTS:
            raise ValueError("Playwright Node upstream entitlements changed")
        result[arch] = MAC_CONSTRAINTS.strongest_sha256_cdhash(details.stdout + details.stderr)
    return {"arches": result}


def _write_pipe(value):
    read_fd, write_fd = os.pipe()
    try:
        view = memoryview(value)
        while view:
            view = view[os.write(write_fd, view):]
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
    return read_fd


def build_macos_frozen_engine(work, password):
    security_root = work / "engine-security"
    security_root.mkdir(mode=0o700)
    signed_stage = security_root / "signed"
    cache = security_root / "pyinstaller-cache"
    signed_stage.mkdir(mode=0o700)
    cache.mkdir(mode=0o700)
    inventory_path = security_root / "native-inventory.json"
    descriptor = _write_pipe(password)
    environment = os.environ.copy()
    environment.update({
        "GAMJA_ENGINE_SIGNING_PASSWORD_FD": str(descriptor),
        "GAMJA_ENGINE_SIGNED_STAGE": str(signed_stage),
        "GAMJA_ENGINE_NATIVE_INVENTORY": str(inventory_path),
        "GAMJA_ENGINE_PYINSTALLER_CACHE": str(cache),
        "MACOSX_DEPLOYMENT_TARGET": "14.0",
    })
    try:
        subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
             "--distpath", str(work / "engine-dist"),
             "--workpath", str(work / "engine-build"), "gamja-engine.spec"],
            cwd=ROOT / "backend", check=True, env=environment, pass_fds=(descriptor,),
        )
    finally:
        os.close(descriptor)
    engine = work / "engine-dist/gamja-engine"
    if not engine.is_file() or inventory_path.is_symlink() or not inventory_path.is_file():
        raise ValueError("frozen engine native inventory missing")
    inventory = json.loads(inventory_path.read_text())
    from PyInstaller.archive.readers import CArchiveReader
    extract_root = security_root / "archive-extract"
    cdhashes = MAC_CONSTRAINTS.verify_carchive_native_entries(
        CArchiveReader(str(engine)), inventory, extract_root,
        inspect_library=inspect_macos_library,
        inspect_helper=inspect_macos_playwright_node,
    )
    return engine, cdhashes


def _set_macos14_build_version(binary):
    output = Path(binary).with_name(f".{Path(binary).name}.macos14")
    result = subprocess.run(
        ["/usr/bin/vtool", "-set-build-version", "macos", "14.0", "14.0",
         "-replace", "-output", str(output), str(binary)],
        capture_output=True, text=True,
    )
    if result.returncode or not output.is_file():
        raise ValueError("macOS 14 build version update failed")
    output.chmod(Path(binary).stat().st_mode & 0o777)
    os.replace(output, binary)


def _require_system_dependencies_only(binary):
    result = subprocess.run(["/usr/bin/otool", "-L", str(binary)],
                            capture_output=True, text=True)
    if result.returncode:
        raise ValueError("engine dependency inventory failed")
    dependencies = [line.strip().split(" (", 1)[0]
                    for line in result.stdout.splitlines()[1:] if line.strip()]
    if not dependencies or any(not (path.startswith("/usr/lib/")
                                     or path.startswith("/System/Library/"))
                               for path in dependencies):
        raise ValueError("engine has non-system direct dependency")


def prepare_macos_engine(engine, password, cdhashes, work, *, supervised_group=False):
    constraint = work / "engine-library-constraint.plist"
    entitlements = work / "engine-entitlements.plist"
    constraint.write_bytes(MAC_CONSTRAINTS.constraint_plist(cdhashes))
    entitlements.write_bytes(MAC_CONSTRAINTS.engine_entitlements_plist())
    validated = subprocess.run(["/usr/bin/codesign", "--validate-constraint", str(constraint)],
                               capture_output=True, text=True)
    if validated.returncode:
        clear_secret(password)
        raise ValueError("engine library constraint validation failed")
    _set_macos14_build_version(engine)
    _require_system_dependencies_only(engine)
    compiled = subprocess.run(
        ["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none",
         "--options", "runtime", "--entitlements", str(entitlements),
         "--library-constraint", str(constraint), "--enforce-constraint-validity",
         str(engine)],
        capture_output=True, text=True,
    )
    if compiled.returncode:
        clear_secret(password)
        raise ValueError("engine library constraint compile failed")
    before = MAC_CONSTRAINTS.constraint_slots_from_thin_macho(engine)
    if len(before) != 1:
        clear_secret(password)
        raise ValueError("engine library constraint slot missing")
    if supervised_group:
        sign_macos_binary(engine, MAC_ENGINE_IDENTIFIER, password,
                          supervised_group=True)
    else:
        sign_macos_binary(engine, MAC_ENGINE_IDENTIFIER, password)
    after = MAC_CONSTRAINTS.constraint_slots_from_thin_macho(engine)
    if after != before:
        raise ValueError("engine library constraint changed during final signing")
    return sha256(engine)


def validate_toolchain(target, *, python_version, rust_host, versions):
    if tuple(python_version) != PYTHON_VERSION:
        raise ValueError("use pinned target-native Python 3.11.15")
    if rust_host != target:
        raise ValueError(f"native Rust host {rust_host} does not match {target}")
    for name, required in PINS.items():
        if versions.get(name) != required:
            raise ValueError(f"{name} must be {required}; found {versions.get(name)}")


RUNTIME_RECEIPT = ".gamja-runtime-receipt.json"
MAX_RUNTIME_CLI_OUTPUT = 4096
RUNTIME_CLI_TIMEOUT = 30


def _run_bounded_core_cli(argv, *, pass_fds=(), timeout_seconds=None):
    child = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin"},
        pass_fds=pass_fds,
    )
    streams = {child.stdout: bytearray(), child.stderr: bytearray()}
    selector = selectors.DefaultSelector()
    try:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + (RUNTIME_CLI_TIMEOUT if timeout_seconds is None else timeout_seconds)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                child.kill()
                child.wait()
                raise ValueError("canonical runtime verification timed out")
            for key, _events in selector.select(min(remaining, 0.25)):
                chunk = os.read(key.fileobj.fileno(), 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output = streams[key.fileobj]
                output.extend(chunk)
                if len(output) > MAX_RUNTIME_CLI_OUTPUT:
                    child.kill()
                    child.wait()
                    raise ValueError("canonical runtime verification output exceeded limit")
        try:
            returncode = child.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            child.kill()
            child.wait()
            raise ValueError("canonical runtime verification timed out") from error
    finally:
        selector.close()
        for stream in streams:
            stream.close()
        if child.poll() is None:
            child.kill()
            child.wait()
    return returncode, *(bytes(value) for value in streams.values())


def _runtime_asset_identity(status):
    return (
        status.st_dev,
        status.st_ino,
        status.st_uid,
        stat.S_IMODE(status.st_mode),
        status.st_nlink,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _hash_descriptor(descriptor):
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    try:
        for block in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
            digest.update(block)
        return digest.hexdigest()
    finally:
        os.lseek(descriptor, 0, os.SEEK_SET)


class _FrozenRuntimeAsset:
    def __init__(self, path):
        self.path = Path(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            self.descriptor = os.open(self.path, flags)
        except OSError:
            raise ValueError("runtime candidate asset snapshot failed") from None
        try:
            status = os.fstat(self.descriptor)
            path_status = os.lstat(self.path)
            if (not stat.S_ISREG(status.st_mode)
                    or _runtime_asset_identity(status) != _runtime_asset_identity(path_status)
                    or status.st_nlink != 1):
                raise ValueError("runtime candidate asset is not a stable regular file")
            self.identity = _runtime_asset_identity(status)
            self.digest = _hash_descriptor(self.descriptor)
            self._recheck()
        except OSError:
            os.close(self.descriptor)
            raise ValueError("runtime candidate asset snapshot failed") from None
        except BaseException:
            os.close(self.descriptor)
            raise

    def _recheck(self):
        try:
            changed = (
                _runtime_asset_identity(os.fstat(self.descriptor)) != self.identity
                or _runtime_asset_identity(os.lstat(self.path)) != self.identity
                or _hash_descriptor(self.descriptor) != self.digest
            )
        except OSError:
            changed = True
        if changed:
            raise ValueError("runtime candidate changed during canonical verification")

    def close(self):
        os.close(self.descriptor)


def _runtime_root_identity(root):
    try:
        status = os.lstat(root)
    except OSError:
        raise ValueError("runtime candidate root snapshot failed") from None
    if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
        raise ValueError("runtime candidate root is not a stable directory")
    return _runtime_asset_identity(status)


def _operator_runtime_verifier(root, verifier, expected_sha256):
    if verifier is None or expected_sha256 is None:
        raise ValueError("independent runtime verifier is required")
    verifier = Path(verifier)
    if not verifier.is_absolute():
        raise ValueError("independent runtime verifier path must be absolute")
    try:
        verifier = verifier.resolve(strict=True)
    except OSError:
        raise ValueError("independent runtime verifier is unavailable") from None
    if verifier.is_relative_to(root):
        raise ValueError("independent runtime verifier may not come from candidate")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("independent runtime verifier SHA-256 pin is invalid")
    _real_owned_directory(verifier.parent, "independent runtime verifier directory")
    asset = _FrozenRuntimeAsset(verifier)
    status = os.fstat(asset.descriptor)
    if (status.st_uid != os.geteuid() or status.st_mode & 0o022
            or not status.st_mode & stat.S_IXUSR):
        asset.close()
        raise ValueError("independent runtime verifier ownership or permissions are unsafe")
    if asset.digest != expected_sha256:
        asset.close()
        raise ValueError("independent runtime verifier SHA-256 mismatch")
    return verifier, asset


def verify_runtime_receipt_with_core_cli(
        root, target, *, verifier=None, verifier_sha256=None,
        verify_signatures=True, expected_pins=None):
    root = Path(root).resolve(strict=True)
    if not target.endswith("apple-darwin"):
        raise ValueError("independent runtime verification is unavailable for this target")
    suffix = ".exe" if target.endswith("windows-msvc") else ""
    core = root / f"bin/gamja{suffix}"
    engine = root / f"libexec/gamja/gamja-engine{suffix}"
    manifest = root / MANIFEST
    verifier, verifier_asset = _operator_runtime_verifier(
        root, verifier, verifier_sha256)
    root_identity = _runtime_root_identity(root)
    assets = []
    try:
        for path in (manifest, core, engine):
            assets.append(_FrozenRuntimeAsset(path))
        manifest_asset, core_asset, engine_asset = assets
        if verifier_asset.identity[:3] == core_asset.identity[:3]:
            raise ValueError("independent runtime verifier may not be candidate Core")
        actual_pins = {
            "manifest": manifest_asset.digest,
            "core": core_asset.digest,
            "engine": engine_asset.digest,
        }
        if expected_pins is not None and actual_pins != expected_pins:
            raise ValueError("runtime candidate changed after package verification")
        if verify_signatures:
            verify_macos_signature(core, MAC_CORE_IDENTIFIER)
            verify_macos_signature(engine, MAC_ENGINE_IDENTIFIER)
            verify_macos_signature(verifier, MAC_CORE_IDENTIFIER)
        if _runtime_root_identity(root) != root_identity:
            raise ValueError("runtime candidate changed during canonical verification")
        for asset in (*assets, verifier_asset):
            asset._recheck()
        argv = [
            str(verifier), "runtime", "verify", "--source", str(root),
            "--manifest-sha256", manifest_asset.digest,
            "--core-sha256", core_asset.digest,
            "--engine-sha256", engine_asset.digest,
        ]
        try:
            returncode, stdout, _stderr = _run_bounded_core_cli(argv)
        finally:
            if _runtime_root_identity(root) != root_identity:
                raise ValueError("runtime candidate changed during canonical verification")
            for asset in (*assets, verifier_asset):
                asset._recheck()
    finally:
        for asset in assets:
            asset.close()
        verifier_asset.close()
    if returncode != 0:
        raise ValueError("canonical runtime verification failed")
    try:
        response = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("canonical runtime verification response invalid") from error
    if response != {"version": 1, "operation": "verify", "status": "valid"}:
        raise ValueError("canonical runtime verification response invalid")


def inventory(root, *, allow_runtime_receipt=False):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("package root must be a real directory")
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == RUNTIME_RECEIPT:
            status = path.lstat()
            if (allow_runtime_receipt and stat.S_ISREG(status.st_mode)
                    and not path.is_symlink() and status.st_uid == os.geteuid()
                    and status.st_nlink == 1 and stat.S_IMODE(status.st_mode) == 0o600):
                continue
            raise ValueError(f"unexpected package asset: {relative}")
        # Browser distributions contain their own runtime web assets/frameworks.
        allowed = {"bin", "bin/gamja", "bin/gamja.exe", "libexec", "libexec/gamja", "libexec/gamja/gamja-engine", "libexec/gamja/gamja-engine.exe", "libexec/gamja/gamja-platform-helper", "libexec/gamja/gamja-platform-helper.exe", "libexec/gamja/playwright-browsers", "libexec/gamja/gamja_core_host.dll", MANIFEST, "THIRD-PARTY-NOTICES.txt", BROKER_PATH, AUTHORITY_PATH, SLACK_BROKER_PATH, SLACK_AUTHORITY_PATH, MCP_BROKER_PATH, MCP_AUTHORITY_PATH, PUBLISHER_BROKER_PATH, PUBLISHER_AUTHORITY_PATH, VAULT_BROKER_PATH, VAULT_AUTHORITY_PATH}
        if relative not in allowed and not relative.startswith("libexec/gamja/playwright-browsers/"):
            raise ValueError(f"unexpected package asset: {relative}")
        browser = relative.startswith("libexec/gamja/playwright-browsers/")
        if not browser and (any(part.endswith(".app") or part in {"frontend", "src-tauri", "node_modules", "dist"} for part in path.relative_to(root).parts)
                            or path.suffix.lower() in {".html", ".js", ".css", ".tsx", ".jsx"}):
            raise ValueError(f"GUI asset is forbidden: {relative}")
        if path.is_symlink():
            resolved = path.resolve()
            if not resolved.is_relative_to(root.resolve()) or not resolved.exists():
                raise ValueError(f"external or broken symlink: {relative}")
            result[relative] = {"symlink": os.readlink(path)}
        elif path.is_file():
            if relative == MANIFEST:
                continue
            result[relative] = {"sha256": sha256(path), "bytes": path.stat().st_size, "executable": bool(path.stat().st_mode & 0o111)}
        elif not path.is_dir():
            raise ValueError(f"unsupported package asset: {relative}")
    return result


def _read_broker_authority(path):
    """Read only the bounded, compiled public claims of a verified authority."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate authority field")
            result[key] = value
        return result

    try:
        # Fresh macOS bundle copies can take longer during initial signature checks.
        code, stdout, stderr = _run_bounded_core_cli([str(path)], timeout_seconds=10)
        if code or stderr or len(stdout) > MAX_RUNTIME_CLI_OUTPUT:
            raise ValueError("authority response invalid")
        claims = json.loads(stdout, object_pairs_hook=unique_object)
        if (not isinstance(claims, dict)
                or set(claims) != {"protocol_version", "build_id", "broker_sha256",
                                   "core_sha256", "engine_sha256"}
                or type(claims["protocol_version"]) is not int
                or claims["protocol_version"] != 1
                or not isinstance(claims["build_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", claims["build_id"])
                or any(not isinstance(claims[key], str)
                       or not re.fullmatch(r"[0-9a-f]{64}", claims[key])
                       for key in ("broker_sha256", "core_sha256", "engine_sha256"))):
            raise ValueError("authority claims invalid")
    except (OSError, ValueError, TypeError):
        raise ValueError("credential broker authority response invalid") from None
    return claims


def _accepted_mcp_broker_pin():
    pin = ACCEPTED_MCP_BROKER_SHA256
    if (not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{64}", pin)
            or pin == ACCEPTED_BROKER_SHA256):
        raise ValueError("MCP broker artifact has not been qualified and pinned")
    return pin


def _validate_mcp_broker(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError("MCP broker path must be absolute without symlink ancestors")
    _real_owned_file(path, "MCP broker")
    if not path.stat().st_mode & stat.S_IXUSR or sha256(path) != _accepted_mcp_broker_pin():
        raise ValueError("MCP broker accepted artifact mismatch")
    verify_macos_signature(path, MAC_MCP_BROKER_IDENTIFIER)
    return path


def _accepted_slack_broker_pin():
    pin = ACCEPTED_SLACK_BROKER_SHA256
    if (not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{64}", pin)
            or pin in (ACCEPTED_BROKER_SHA256, ACCEPTED_MCP_BROKER_SHA256, ACCEPTED_PUBLISHER_BROKER_SHA256, _accepted_vault_broker_pin())):
        raise ValueError("SLACK broker artifact has not been qualified and pinned")
    return pin


def _validate_slack_broker(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError("SLACK broker path must be absolute without symlink ancestors")
    _real_owned_file(path, "SLACK broker")
    if not path.stat().st_mode & stat.S_IXUSR or sha256(path) != _accepted_slack_broker_pin():
        raise ValueError("SLACK broker accepted artifact mismatch")
    verify_macos_signature(path, MAC_SLACK_BROKER_IDENTIFIER)
    return path


def _accepted_publisher_broker_pin():
    pin = ACCEPTED_PUBLISHER_BROKER_SHA256
    if (not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{64}", pin)
            or pin in (ACCEPTED_BROKER_SHA256, ACCEPTED_MCP_BROKER_SHA256)):
        raise ValueError("Publisher broker artifact has not been qualified and pinned")
    return pin


def _validate_publisher_broker(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError("Publisher broker path must be absolute without symlink ancestors")
    _real_owned_file(path, "Publisher broker")
    if not path.stat().st_mode & stat.S_IXUSR or sha256(path) != _accepted_publisher_broker_pin():
        raise ValueError("Publisher broker accepted artifact mismatch")
    verify_macos_signature(path, MAC_PUBLISHER_BROKER_IDENTIFIER)
    return path


def _accepted_vault_broker_pin():
    # P96: card namespace ARM64 artifact from source b92c7a1; static signature qualification only.
    return "d78a32bea552c25fff8e276c9fe36b33ebb503f93189e3892425fd4078fc414e"


def _validate_vault_broker(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError("Vault broker path must be absolute without symlink ancestors")
    _real_owned_file(path, "Vault broker")
    if not path.stat().st_mode & stat.S_IXUSR or sha256(path) != _accepted_vault_broker_pin():
        raise ValueError("Vault broker accepted artifact mismatch")
    verify_macos_signature(path, MAC_VAULT_BROKER_IDENTIFIER)
    return path



def _validate_stable_broker(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError("credential broker path must be absolute without symlink ancestors")
    _real_owned_file(path, "credential broker")
    if not path.stat().st_mode & stat.S_IXUSR or sha256(path) != ACCEPTED_BROKER_SHA256:
        raise ValueError("credential broker accepted artifact mismatch")
    verify_macos_signature(path, MAC_BROKER_IDENTIFIER)
    return path


def _credential_broker_binding(root, target, files, provenance, *, verify_signatures):
    present = {name for name in (BROKER_PATH, AUTHORITY_PATH) if name in files}
    if not present:
        return None
    if len(present) != 2 or target != "aarch64-apple-darwin":
        raise ValueError("credential broker requires the complete native ARM pair")
    names = ("bin/gamja", "libexec/gamja/gamja-engine", BROKER_PATH, AUTHORITY_PATH)
    assets = []
    try:
        for name in names:
            path = _real_owned_file(root / name, "credential broker package executable")
            entry = files.get(name, {})
            if (set(entry) != {"sha256", "bytes", "executable"}
                    or entry["executable"] is not True or not path.stat().st_mode & stat.S_IXUSR):
                raise ValueError("credential broker executable inventory invalid")
            asset = _FrozenRuntimeAsset(path)
            assets.append(asset)
            if asset.digest != entry["sha256"] or asset.identity[5] != entry["bytes"]:
                raise ValueError("credential broker package changed")
        if files[BROKER_PATH]["sha256"] != ACCEPTED_BROKER_SHA256:
            raise ValueError("credential broker accepted artifact mismatch")
        if verify_signatures:
            verify_macos_signature(root / BROKER_PATH, MAC_BROKER_IDENTIFIER)
            verify_macos_signature(root / AUTHORITY_PATH, MAC_RELEASE_AUTHORITY_IDENTIFIER)
        for asset in assets:
            asset._recheck()
        try:
            claims = _read_broker_authority(root / AUTHORITY_PATH)
        finally:
            for asset in assets:
                asset._recheck()
        expected = {"protocol_version": 1, "build_id": (provenance or {}).get("build_id"),
                    "broker_sha256": files[BROKER_PATH]["sha256"],
                    "core_sha256": files["bin/gamja"]["sha256"],
                    "engine_sha256": files["libexec/gamja/gamja-engine"]["sha256"]}
        if claims != expected:
            raise ValueError("credential broker authority binding mismatch")
        return {"version": 1, "build_id": claims["build_id"],
                "broker_sha256": claims["broker_sha256"],
                "authority_sha256": files[AUTHORITY_PATH]["sha256"]}
    finally:
        for asset in assets:
            asset.close()


def _mcp_credential_broker_binding(root, target, files, provenance, *, verify_signatures):
    present = {name for name in (MCP_BROKER_PATH, MCP_AUTHORITY_PATH) if name in files}
    if not present:
        return None
    if any(name not in files for name in (BROKER_PATH, AUTHORITY_PATH)):
        raise ValueError("MCP credentials require the separate immutable legacy pair")
    if len(present) != 2 or target != "aarch64-apple-darwin":
        raise ValueError("credential broker requires the complete native ARM pair")
    names = ("bin/gamja", "libexec/gamja/gamja-engine", MCP_BROKER_PATH, MCP_AUTHORITY_PATH)
    assets = []
    try:
        for name in names:
            path = _real_owned_file(root / name, "credential broker package executable")
            entry = files.get(name, {})
            if (set(entry) != {"sha256", "bytes", "executable"}
                    or entry["executable"] is not True or not path.stat().st_mode & stat.S_IXUSR):
                raise ValueError("credential broker executable inventory invalid")
            asset = _FrozenRuntimeAsset(path)
            assets.append(asset)
            if asset.digest != entry["sha256"] or asset.identity[5] != entry["bytes"]:
                raise ValueError("credential broker package changed")
        if files[MCP_BROKER_PATH]["sha256"] != _accepted_mcp_broker_pin():
            raise ValueError("credential broker accepted artifact mismatch")
        if verify_signatures:
            verify_macos_signature(root / MCP_BROKER_PATH, MAC_MCP_BROKER_IDENTIFIER)
            verify_macos_signature(root / MCP_AUTHORITY_PATH, MAC_MCP_RELEASE_AUTHORITY_IDENTIFIER)
        for asset in assets:
            asset._recheck()
        try:
            claims = _read_broker_authority(root / MCP_AUTHORITY_PATH)
        finally:
            for asset in assets:
                asset._recheck()
        expected = {"protocol_version": 1, "build_id": (provenance or {}).get("build_id"),
                    "broker_sha256": files[MCP_BROKER_PATH]["sha256"],
                    "core_sha256": files["bin/gamja"]["sha256"],
                    "engine_sha256": files["libexec/gamja/gamja-engine"]["sha256"]}
        if claims != expected:
            raise ValueError("credential broker authority binding mismatch")
        return {"version": 1, "build_id": claims["build_id"],
                "broker_sha256": claims["broker_sha256"],
                "authority_sha256": files[MCP_AUTHORITY_PATH]["sha256"]}
    finally:
        for asset in assets:
            asset.close()


def _slack_credential_broker_binding(root, target, files, provenance, *, verify_signatures):
    present = {name for name in (SLACK_BROKER_PATH, SLACK_AUTHORITY_PATH) if name in files}
    if not present:
        return None
    if any(name not in files for name in (BROKER_PATH, AUTHORITY_PATH)):
        raise ValueError("SLACK credentials require the separate immutable legacy pair")
    if len(present) != 2 or target != "aarch64-apple-darwin":
        raise ValueError("credential broker requires the complete native ARM pair")
    names = ("bin/gamja", "libexec/gamja/gamja-engine", SLACK_BROKER_PATH, SLACK_AUTHORITY_PATH)
    assets = []
    try:
        for name in names:
            path = _real_owned_file(root / name, "credential broker package executable")
            entry = files.get(name, {})
            if (set(entry) != {"sha256", "bytes", "executable"}
                    or entry["executable"] is not True or not path.stat().st_mode & stat.S_IXUSR):
                raise ValueError("credential broker executable inventory invalid")
            asset = _FrozenRuntimeAsset(path)
            assets.append(asset)
            if asset.digest != entry["sha256"] or asset.identity[5] != entry["bytes"]:
                raise ValueError("credential broker package changed")
        if files[SLACK_BROKER_PATH]["sha256"] != _accepted_slack_broker_pin():
            raise ValueError("credential broker accepted artifact mismatch")
        if verify_signatures:
            verify_macos_signature(root / SLACK_BROKER_PATH, MAC_SLACK_BROKER_IDENTIFIER)
            verify_macos_signature(root / SLACK_AUTHORITY_PATH, MAC_SLACK_RELEASE_AUTHORITY_IDENTIFIER)
        for asset in assets:
            asset._recheck()
        try:
            claims = _read_broker_authority(root / SLACK_AUTHORITY_PATH)
        finally:
            for asset in assets:
                asset._recheck()
        expected = {"protocol_version": 1, "build_id": (provenance or {}).get("build_id"),
                    "broker_sha256": files[SLACK_BROKER_PATH]["sha256"],
                    "core_sha256": files["bin/gamja"]["sha256"],
                    "engine_sha256": files["libexec/gamja/gamja-engine"]["sha256"]}
        if claims != expected:
            raise ValueError("credential broker authority binding mismatch")
        return {"version": 1, "build_id": claims["build_id"],
                "broker_sha256": claims["broker_sha256"],
                "authority_sha256": files[SLACK_AUTHORITY_PATH]["sha256"]}
    finally:
        for asset in assets:
            asset.close()


def _publisher_credential_broker_binding(root, target, files, provenance, *, verify_signatures):
    present = {name for name in (PUBLISHER_BROKER_PATH, PUBLISHER_AUTHORITY_PATH) if name in files}
    if not present:
        return None
    if any(name not in files for name in (BROKER_PATH, AUTHORITY_PATH)):
        raise ValueError("Publisher credentials require the separate immutable legacy pair")
    if len(present) != 2 or target != "aarch64-apple-darwin":
        raise ValueError("credential broker requires the complete native ARM pair")
    names = ("bin/gamja", "libexec/gamja/gamja-engine", PUBLISHER_BROKER_PATH, PUBLISHER_AUTHORITY_PATH)
    assets = []
    try:
        for name in names:
            path = _real_owned_file(root / name, "credential broker package executable")
            entry = files.get(name, {})
            if (set(entry) != {"sha256", "bytes", "executable"}
                    or entry["executable"] is not True or not path.stat().st_mode & stat.S_IXUSR):
                raise ValueError("credential broker executable inventory invalid")
            asset = _FrozenRuntimeAsset(path)
            assets.append(asset)
            if asset.digest != entry["sha256"] or asset.identity[5] != entry["bytes"]:
                raise ValueError("credential broker package changed")
        if files[PUBLISHER_BROKER_PATH]["sha256"] != _accepted_publisher_broker_pin():
            raise ValueError("credential broker accepted artifact mismatch")
        if verify_signatures:
            verify_macos_signature(root / PUBLISHER_BROKER_PATH, MAC_PUBLISHER_BROKER_IDENTIFIER)
            verify_macos_signature(root / PUBLISHER_AUTHORITY_PATH, MAC_PUBLISHER_RELEASE_AUTHORITY_IDENTIFIER)
        for asset in assets:
            asset._recheck()
        try:
            claims = _read_broker_authority(root / PUBLISHER_AUTHORITY_PATH)
        finally:
            for asset in assets:
                asset._recheck()
        expected = {"protocol_version": 1, "build_id": (provenance or {}).get("build_id"),
                    "broker_sha256": files[PUBLISHER_BROKER_PATH]["sha256"],
                    "core_sha256": files["bin/gamja"]["sha256"],
                    "engine_sha256": files["libexec/gamja/gamja-engine"]["sha256"]}
        if claims != expected:
            raise ValueError("credential broker authority binding mismatch")
        return {"version": 1, "build_id": claims["build_id"],
                "broker_sha256": claims["broker_sha256"],
                "authority_sha256": files[PUBLISHER_AUTHORITY_PATH]["sha256"]}
    finally:
        for asset in assets:
            asset.close()


def _vault_credential_broker_binding(root, target, files, provenance, *, verify_signatures):
    present = {name for name in (VAULT_BROKER_PATH, VAULT_AUTHORITY_PATH) if name in files}
    if not present:
        return None
    if any(name not in files for name in (BROKER_PATH, AUTHORITY_PATH)):
        raise ValueError("Vault credentials require the separate immutable legacy pair")
    if len(present) != 2 or target != "aarch64-apple-darwin":
        raise ValueError("credential broker requires the complete native ARM pair")
    names = ("bin/gamja", "libexec/gamja/gamja-engine", VAULT_BROKER_PATH, VAULT_AUTHORITY_PATH)
    assets = []
    try:
        for name in names:
            path = _real_owned_file(root / name, "credential broker package executable")
            entry = files.get(name, {})
            if (set(entry) != {"sha256", "bytes", "executable"}
                    or entry["executable"] is not True or not path.stat().st_mode & stat.S_IXUSR):
                raise ValueError("credential broker executable inventory invalid")
            asset = _FrozenRuntimeAsset(path)
            assets.append(asset)
            if asset.digest != entry["sha256"] or asset.identity[5] != entry["bytes"]:
                raise ValueError("credential broker package changed")
        if files[VAULT_BROKER_PATH]["sha256"] != _accepted_vault_broker_pin():
            raise ValueError("credential broker accepted artifact mismatch")
        if verify_signatures:
            verify_macos_signature(root / VAULT_BROKER_PATH, MAC_VAULT_BROKER_IDENTIFIER)
            verify_macos_signature(root / VAULT_AUTHORITY_PATH, MAC_VAULT_RELEASE_AUTHORITY_IDENTIFIER)
        for asset in assets:
            asset._recheck()
        try:
            claims = _read_broker_authority(root / VAULT_AUTHORITY_PATH)
        finally:
            for asset in assets:
                asset._recheck()
        expected = {"protocol_version": 1, "build_id": (provenance or {}).get("build_id"),
                    "broker_sha256": files[VAULT_BROKER_PATH]["sha256"],
                    "core_sha256": files["bin/gamja"]["sha256"],
                    "engine_sha256": files["libexec/gamja/gamja-engine"]["sha256"]}
        if claims != expected:
            raise ValueError("credential broker authority binding mismatch")
        return {"version": 1, "build_id": claims["build_id"],
                "broker_sha256": claims["broker_sha256"],
                "authority_sha256": files[VAULT_AUTHORITY_PATH]["sha256"]}
    finally:
        for asset in assets:
            asset.close()


def write_manifest(root, *, target, version, provenance):
    suffix = ".exe" if target.endswith("windows-msvc") else ""
    data = {"format": 1, "version": version, "target": target, "python": "3.11.15", "build_tools": PINS,
            "browsers": BROWSER_REVISIONS, "browser_payload": (root / "libexec/gamja/playwright-browsers").is_dir(), "core_protocol": 1, "engine_protocol": 1,
            "helper": (root / f"libexec/gamja/gamja-platform-helper{suffix}").is_file(),
            "provenance": provenance, "files": inventory(root)}
    binding = _credential_broker_binding(root, target, data["files"], provenance,
                                         verify_signatures=True)
    if binding is not None:
        data["credential_broker"] = binding
    mcp_binding = _mcp_credential_broker_binding(root, target, data["files"], provenance,
                                                 verify_signatures=True)
    if mcp_binding is not None:
        data["mcp_credential_broker"] = mcp_binding
    slack_binding = _slack_credential_broker_binding(root, target, data["files"], provenance, verify_signatures=True)
    if slack_binding is not None:
        data["slack_credential_broker"] = slack_binding
    publisher_binding = _publisher_credential_broker_binding(root, target, data["files"], provenance,
                                                              verify_signatures=True)
    if publisher_binding is not None:
        data["publisher_credential_broker"] = publisher_binding
    vault_binding = _vault_credential_broker_binding(root, target, data["files"], provenance,
                                                        verify_signatures=True)
    if vault_binding is not None:
        data["vault_credential_broker"] = vault_binding
    (root / MANIFEST).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def verify_package(root, target, *, require_credential_signature=True,
                   allow_runtime_receipt=False, runtime_verifier=None,
                   runtime_verifier_sha256=None):
    root = Path(root)
    files = inventory(root, allow_runtime_receipt=allow_runtime_receipt)
    data = json.loads((root / MANIFEST).read_text())
    if data.get("format") != 1 or data.get("target") != target or target not in TARGETS.values():
        raise ValueError("package target/format mismatch")
    if data.get("python") != "3.11.15" or data.get("build_tools") != PINS or data.get("browsers") != BROWSER_REVISIONS:
        raise ValueError("package manifest toolchain pin mismatch")
    if data.get("files") != files:
        raise ValueError("package manifest/hash coverage mismatch")
    suffix = ".exe" if target.endswith("windows-msvc") else ""
    required = [f"bin/gamja{suffix}", f"libexec/gamja/gamja-engine{suffix}", "THIRD-PARTY-NOTICES.txt"]
    if suffix:
        required.append("libexec/gamja/gamja_core_host.dll")
    if data.get("browser_payload") != (root / "libexec/gamja/playwright-browsers").is_dir():
        raise ValueError("package browser manifest mismatch")
    if data["browser_payload"]:
        required += [f"libexec/gamja/playwright-browsers/{name.replace('-', '_')}-{revision}/INSTALLATION_COMPLETE" for name, revision in BROWSER_REVISIONS.items()]
    if any(name not in files for name in required):
        raise ValueError("package manifest is missing required core assets")
    if data.get("helper") != (f"libexec/gamja/gamja-platform-helper{suffix}" in files):
        raise ValueError("package helper manifest mismatch")
    binding = _credential_broker_binding(root, target, files, data.get("provenance"),
                                         verify_signatures=require_credential_signature)
    metadata = data.get("credential_broker")
    if (metadata != binding or (binding is not None and
            (not isinstance(metadata, dict) or type(metadata.get("version")) is not int))
            or (binding is None and "credential_broker" in data)):
        raise ValueError("credential broker manifest binding mismatch")
    mcp_binding = _mcp_credential_broker_binding(root, target, files, data.get("provenance"),
                                                 verify_signatures=require_credential_signature)
    mcp_metadata = data.get("mcp_credential_broker")
    if (mcp_metadata != mcp_binding or (mcp_binding is not None and
            (not isinstance(mcp_metadata, dict) or type(mcp_metadata.get("version")) is not int))
            or (mcp_binding is None and "mcp_credential_broker" in data)):
        raise ValueError("MCP broker manifest binding mismatch")
    slack_binding = _slack_credential_broker_binding(root, target, files, data.get("provenance"), verify_signatures=require_credential_signature)
    slack_metadata = data.get("slack_credential_broker")
    if (slack_metadata != slack_binding or (slack_binding is not None and
            (not isinstance(slack_metadata, dict) or type(slack_metadata.get("version")) is not int))
            or (slack_binding is None and "slack_credential_broker" in data)):
        raise ValueError("Slack broker manifest binding mismatch")
    publisher_binding = _publisher_credential_broker_binding(root, target, files, data.get("provenance"),
                                                              verify_signatures=require_credential_signature)
    publisher_metadata = data.get("publisher_credential_broker")
    if (publisher_metadata != publisher_binding or (publisher_binding is not None and
            (not isinstance(publisher_metadata, dict) or type(publisher_metadata.get("version")) is not int))
            or (publisher_binding is None and "publisher_credential_broker" in data)):
        raise ValueError("Publisher broker manifest binding mismatch")
    vault_binding = _vault_credential_broker_binding(root, target, files, data.get("provenance"),
                                                        verify_signatures=require_credential_signature)
    vault_metadata = data.get("vault_credential_broker")
    if (vault_metadata != vault_binding or (vault_binding is not None and
            (not isinstance(vault_metadata, dict) or type(vault_metadata.get("version")) is not int))
            or (vault_binding is None and "vault_credential_broker" in data)):
        raise ValueError("Vault broker manifest binding mismatch")
    if (target.endswith("apple-darwin") and require_credential_signature
            and not allow_runtime_receipt):
        verify_macos_signature(root / "bin/gamja", MAC_CORE_IDENTIFIER)
        verify_macos_signature(root / "libexec/gamja/gamja-engine", MAC_ENGINE_IDENTIFIER)
    if allow_runtime_receipt:
        expected_pins = {
            "manifest": sha256(root / MANIFEST),
            "core": files[f"bin/gamja{suffix}"]["sha256"],
            "engine": files[f"libexec/gamja/gamja-engine{suffix}"]["sha256"],
        }
        verify_runtime_receipt_with_core_cli(
            root,
            target,
            verifier=runtime_verifier,
            verifier_sha256=runtime_verifier_sha256,
            verify_signatures=require_credential_signature,
            expected_pins=expected_pins,
        )
    return data


def replace_directory(stage, destination):
    """Replace only an explicit real directory, retaining the prior copy on failure."""
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ValueError(f"refusing non-directory destination: {destination}")
    previous = stage.parent / "previous"
    if destination.exists():
        destination.rename(previous)
    try:
        stage.rename(destination)
    except BaseException:
        if previous.exists():
            previous.rename(destination)
        raise


def write_notices(stage, version):
    """Write dependency notices deterministically across host encodings."""
    notices = [f"Gamja core {version}\nPython 3.11.15\n", subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True)]
    for distribution in metadata.distributions():
        for entry in distribution.files or []:
            if entry.name.lower().startswith(("license", "copying", "notice")):
                path = distribution.locate_file(entry)
                if path.is_file():
                    notices.append(f"\n--- {distribution.metadata['Name']}: {entry} ---\n" + path.read_text(encoding="utf-8", errors="replace"))
    (stage / "THIRD-PARTY-NOTICES.txt").write_text("\n".join(notices), encoding="utf-8")


def build_input_paths(*, with_helper, macos):
    paths = [
        "backend/requirements.txt", "backend/requirements-build.txt", "backend/gamja-engine.spec",
        "backend/run.py", "core-host/Cargo.lock", "core-host/Cargo.toml", "core-host/build.rs",
        "platform-helper/Cargo.toml", "scripts/build_core.py", "scripts/macos_exact_signer.c",
        "scripts/macos_engine_constraints.py", "frontend/src-tauri/Cargo.toml",
        "frontend/src-tauri/Cargo.lock", "frontend/src-tauri/build.rs",
        "frontend/src-tauri/tauri.conf.json",
    ]
    if macos:
        paths.append(MAC_CORE_CERTIFICATE.relative_to(ROOT).as_posix())
        paths += ["slack-credential-broker/Cargo.toml", "slack-credential-broker/Cargo.lock"]
        paths += [path.relative_to(ROOT).as_posix() for path in sorted((ROOT/"slack-credential-broker/src").rglob("*.rs"))]
        paths += ["mcp-credential-broker/Cargo.toml", "mcp-credential-broker/Cargo.lock"]
        paths += [path.relative_to(ROOT).as_posix()
                  for path in sorted((ROOT/"mcp-credential-broker/src").rglob("*.rs"))]
        paths += ["publisher-credential-broker/Cargo.toml", "publisher-credential-broker/Cargo.lock"]
        paths += [path.relative_to(ROOT).as_posix()
                  for path in sorted((ROOT/"publisher-credential-broker/src").rglob("*.rs"))]
        paths += ["vault-credential-broker/Cargo.toml", "vault-credential-broker/Cargo.lock"]
        paths += [path.relative_to(ROOT).as_posix()
                  for path in sorted((ROOT/"vault-credential-broker/src").rglob("*.rs"))]
    paths += [
        path.relative_to(ROOT).as_posix()
        for base in ("backend/app", "core-host/src", "platform-helper/src", "frontend/src-tauri/src")
        for path in sorted((ROOT / base).rglob("*"))
        if path.suffix in {".py", ".rs", ".c"}
    ]
    if with_helper:
        paths += ["platform-helper/Cargo.toml", "platform-helper/Cargo.lock"]
    return sorted(set(paths))


def stage_verified_package(source, destination, target):
    source, destination = Path(source), Path(destination)
    verify_package(source, target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".gamja-core-copy-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "payload"
        shutil.copytree(source, stage, symlinks=True)
        verify_package(stage, target)
        if (stage / MANIFEST).read_bytes() != (source / MANIFEST).read_bytes():
            raise ValueError("core manifest changed during copy")
        replace_directory(stage, destination)


def _clone_relink_file(source, destination):
    """Clone a verified local input into a distinct inode; never fall back to copying.

    The enclosing relink still verifies the complete package and its source pins.
    Callers must separately reserve space for every file subsequently changed.
    """
    source, destination = Path(source), Path(destination)
    _real_owned_file(source, "Core clone source")
    _real_owned_directory(source.parent, "Core clone source parent")
    _real_owned_directory(destination.parent, "Core clone destination parent")
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    clone = library.fclonefileat
    clone.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    clone.restype = ctypes.c_int
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    parent_fd = None
    try:
        before = os.fstat(source_fd)
        if (not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid() or before.st_nlink != 1
                or before.st_mode & 0o022
                or _runtime_asset_identity(before) != _runtime_asset_identity(source.stat())):
            raise ValueError("Core clone source changed")
        parent_fd = os.open(destination.parent,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        if (_runtime_asset_identity(os.fstat(parent_fd))
                != _runtime_asset_identity(destination.parent.stat())):
            raise ValueError("Core clone destination parent changed")
        # A directory fd and a basename avoid resolving destination ancestors in C.
        if clone(source_fd, parent_fd, os.fsencode(destination.name), 0):
            raise OSError(ctypes.get_errno(), "Core clone refused; no copy fallback")
        copied = destination.lstat()
        if (not stat.S_ISREG(copied.st_mode) or copied.st_uid != os.geteuid()
                or copied.st_nlink != 1 or copied.st_dev != before.st_dev
                or copied.st_ino == before.st_ino or copied.st_size != before.st_size
                or stat.S_IMODE(copied.st_mode) != stat.S_IMODE(before.st_mode)
                or _runtime_asset_identity(os.fstat(source_fd)) != _runtime_asset_identity(before)
                or _runtime_asset_identity(source.stat()) != _runtime_asset_identity(before)):
            raise ValueError("Core clone identity changed")
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(source_fd)
    return str(destination)


def stage_core_relink_source(source, stage, target, expected_manifest_sha256,
                             expected_engine_sha256, *, clone_files=False):
    """Copy one verified package for a Core-only relink without hardlinks."""
    source, stage = Path(source), Path(stage)
    if (type(clone_files) is not bool
            or (clone_files and (platform.system() != "Darwin"
                                or target != "aarch64-apple-darwin"))
            or not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_engine_sha256)
            or stage.exists() or stage.is_symlink()):
        raise ValueError("invalid Core-only relink request")
    source_manifest = source / MANIFEST
    if sha256(source_manifest) != expected_manifest_sha256:
        raise ValueError("Core-only manifest hash mismatch")
    manifest = verify_package(source, target)
    engine = source / "libexec/gamja/gamja-engine"
    if sha256(engine) != expected_engine_sha256:
        raise ValueError("Core-only engine hash mismatch")
    shutil.copytree(source, stage, symlinks=True,
                    copy_function=_clone_relink_file if clone_files else shutil.copy2)
    copied = stage / "libexec/gamja/gamja-engine"
    verify_package(stage, target)
    if (sha256(stage / MANIFEST) != expected_manifest_sha256
            or (stage / MANIFEST).read_bytes() != source_manifest.read_bytes()
            or sha256(copied) != expected_engine_sha256):
        raise ValueError("Core-only package changed during copy")
    for path in stage.rglob("*"):
        if path.is_file() and not path.is_symlink():
            original = source / path.relative_to(stage)
            if path.stat().st_nlink != 1 or path.stat().st_ino == original.stat().st_ino:
                raise ValueError("Core-only package copy must not contain hardlinks")
    return manifest


def validate_helper_repack_source(source, target, expected_manifest_sha256,
                                  expected_core_sha256, expected_engine_sha256,
                                  expected_helper_sha256):
    source = _real_owned_directory(Path(source).absolute(), "helper-only source prefix")
    expected = {
        "manifest": expected_manifest_sha256,
        "core": expected_core_sha256,
        "engine": expected_engine_sha256,
        "helper": expected_helper_sha256,
    }
    if (target != "aarch64-apple-darwin"
            or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in expected.values())
            or (source / ".gamja-runtime-receipt.json").exists()
            or (source / ".gamja-runtime-receipt.json").is_symlink()):
        if (source / ".gamja-runtime-receipt.json").exists() or (
                source / ".gamja-runtime-receipt.json").is_symlink():
            raise ValueError("helper-only source contains a runtime receipt")
        raise ValueError("invalid helper-only repack request")
    paths = {
        "manifest": source / MANIFEST,
        "core": source / "bin/gamja",
        "engine": source / "libexec/gamja/gamja-engine",
        "helper": source / "libexec/gamja/gamja-platform-helper",
    }
    if sha256(paths["manifest"]) != expected["manifest"]:
        raise ValueError("helper-only manifest hash mismatch")
    manifest = verify_package(source, target)
    for name in ("core", "engine", "helper"):
        if sha256(paths[name]) != expected[name]:
            raise ValueError(f"helper-only {name} hash mismatch")
    return source, manifest


def stage_helper_repack_source(source, stage, target, expected_manifest_sha256,
                               expected_core_sha256, expected_engine_sha256,
                               expected_helper_sha256):
    """Copy one verified package for a helper-only rebuild without hardlinks."""
    source, manifest = validate_helper_repack_source(
        source, target, expected_manifest_sha256, expected_core_sha256,
        expected_engine_sha256, expected_helper_sha256)
    stage = Path(stage)
    if stage.exists() or stage.is_symlink():
        raise ValueError("invalid helper-only repack request")
    expected = {
        "manifest": expected_manifest_sha256,
        "core": expected_core_sha256,
        "engine": expected_engine_sha256,
        "helper": expected_helper_sha256,
    }
    shutil.copytree(source, stage, symlinks=True)
    verify_package(stage, target)
    for name, relative in {
        "manifest": MANIFEST,
        "core": "bin/gamja",
        "engine": "libexec/gamja/gamja-engine",
        "helper": "libexec/gamja/gamja-platform-helper",
    }.items():
        copied = stage / relative
        if sha256(copied) != expected[name]:
            raise ValueError(f"helper-only {name} changed during copy")
    for path in stage.rglob("*"):
        if path.is_file() and not path.is_symlink():
            original = source / path.relative_to(stage)
            if path.stat().st_nlink != 1 or path.stat().st_ino == original.stat().st_ino:
                raise ValueError("helper-only package copy must not contain hardlinks")
    return manifest


def publish_directory_exclusive(source, destination):
    source, destination = Path(source), Path(destination)
    if source.stat().st_dev != destination.parent.stat().st_dev:
        raise ValueError("helper-only publish requires one filesystem")
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    result = libc.renameatx_np(
        -2, os.fsencode(source), -2, os.fsencode(destination), 0x00000004)
    if result != 0:
        raise ValueError("helper-only output publish collision")
    descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sign_helper_ad_hoc(path):
    path = _real_owned_file(path, "helper-only native helper")
    subprocess.run(
        ["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", str(path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(path)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def repack_helper_package(source, output, built_helper, *, expected_manifest_sha256,
                          expected_core_sha256, expected_engine_sha256,
                          expected_helper_sha256, build_id, source_commit,
                          source_sha256):
    """Replace only the helper, then regenerate and verify the full manifest."""
    source = _real_owned_directory(Path(source).absolute(), "helper-only source prefix")
    output_argument = Path(output).absolute()
    output_parent = _real_owned_directory(output_argument.parent,
                                          "helper-only output parent")
    output = output_parent / output_argument.name
    built_helper = Path(built_helper).absolute()
    live = Path.home().joinpath(".local/share/gamja-bot/core/runtime").resolve(strict=False)
    if (output.exists() or output.is_symlink() or output == source
            or output.is_relative_to(source) or source.is_relative_to(output)
            or output == live or output.is_relative_to(live) or live.is_relative_to(output)
            or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{7,63}", build_id or "")
            or not re.fullmatch(r"[0-9a-f]{40}", source_commit or "")
            or not isinstance(source_sha256, dict) or not source_sha256
            or any(not isinstance(name, str) or not name
                   or not re.fullmatch(r"[0-9a-f]{64}", digest or "")
                   for name, digest in source_sha256.items())):
        raise ValueError("invalid helper-only package output or provenance")
    _real_owned_file(built_helper, "built native helper")
    with tempfile.TemporaryDirectory(prefix=".gamja-helper-repack-", dir=output.parent) as temporary:
        stage = Path(temporary) / "payload"
        prior = stage_helper_repack_source(
            source, stage, "aarch64-apple-darwin", expected_manifest_sha256,
            expected_core_sha256, expected_engine_sha256, expected_helper_sha256)
        helper = stage / "libexec/gamja/gamja-platform-helper"
        shutil.copy2(built_helper, helper)
        helper.chmod(0o755)
        sign_helper_ad_hoc(helper)
        if sha256(helper) == expected_helper_sha256:
            raise ValueError("helper-only rebuild did not change helper bytes")
        provenance = dict(prior.get("provenance") or {})
        provenance.update(build_id=build_id, source_commit=source_commit,
                          source_sha256=dict(sorted(source_sha256.items())),
                          reused_core_sha256=expected_core_sha256,
                          reused_engine_sha256=expected_engine_sha256)
        write_manifest(stage, target="aarch64-apple-darwin", version=prior["version"],
                       provenance=provenance)
        verify_package(stage, "aarch64-apple-darwin")
        for relative, digest in {
            "bin/gamja": expected_core_sha256,
            "libexec/gamja/gamja-engine": expected_engine_sha256,
        }.items():
            if sha256(stage / relative) != digest:
                raise ValueError("helper-only fixed executable changed")
        publish_directory_exclusive(stage, output)


def build_helper_only(source, output, *, expected_manifest_sha256,
                      expected_core_sha256, expected_engine_sha256,
                      expected_helper_sha256, build_id):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("helper-only repack requires native Apple Silicon")
    cargo_target_name = os.environ.get("CARGO_TARGET_DIR")
    if not cargo_target_name:
        raise ValueError("set CARGO_TARGET_DIR to the existing shared Cargo target")
    cargo_target = Path(cargo_target_name).resolve()
    source_paths = build_input_paths(with_helper=True, macos=True)
    source_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    source_sha256 = {name: sha256(ROOT / name) for name in source_paths}
    validate_helper_repack_source(
        source, "aarch64-apple-darwin", expected_manifest_sha256,
        expected_core_sha256, expected_engine_sha256, expected_helper_sha256)
    environment = os.environ.copy()
    environment["MACOSX_DEPLOYMENT_TARGET"] = "14.0"
    target = TARGETS[(platform.system(), platform.machine())]
    subprocess.run([
        "cargo", "build", "--locked", "--release", "--target", target,
        "--manifest-path", str(ROOT / "platform-helper/Cargo.toml"),
    ], check=True, env=environment)
    if (subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            != source_commit
            or {name: sha256(ROOT / name) for name in source_paths} != source_sha256):
        raise ValueError("helper-only source drift during build")
    validate_helper_repack_source(
        source, "aarch64-apple-darwin", expected_manifest_sha256,
        expected_core_sha256, expected_engine_sha256, expected_helper_sha256)
    repack_helper_package(
        source, output, cargo_target / target / "release/gamja-platform-helper",
        expected_manifest_sha256=expected_manifest_sha256,
        expected_core_sha256=expected_core_sha256,
        expected_engine_sha256=expected_engine_sha256,
        expected_helper_sha256=expected_helper_sha256,
        build_id=build_id,
        source_commit=source_commit, source_sha256=source_sha256)
    print(f"native helper-only package built: {output} ({target}); installed verification NOT performed")


def build_core_only(source, output, *, expected_manifest_sha256, expected_engine_sha256,
                    core_signing_password_fd, build_id):
    """Relink and sign Core while retaining one exact verified engine package."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("Core-only relink requires native Apple Silicon")
    if not isinstance(build_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{7,63}", build_id):
        raise ValueError("invalid Core-only build ID")
    source, output = Path(source).absolute(), Path(output).absolute()
    if (output.exists() or output.is_symlink() or output == source
            or output.is_relative_to(source) or source.is_relative_to(output)):
        raise ValueError("invalid Core-only output")
    cargo_target_name = os.environ.get("CARGO_TARGET_DIR")
    if not cargo_target_name:
        raise ValueError("set CARGO_TARGET_DIR to the existing shared Cargo target")
    cargo_target = Path(cargo_target_name).resolve()
    validate_macos_signing_inputs()
    password = read_secret_fd(core_signing_password_fd)
    target = TARGETS[(platform.system(), platform.machine())]
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".gamja-core-relink-", dir=output.parent) as temporary:
            stage = Path(temporary) / "payload"
            prior = stage_core_relink_source(
                source, stage, target, expected_manifest_sha256, expected_engine_sha256)
            environment = os.environ.copy()
            environment.update(
                MACOSX_DEPLOYMENT_TARGET="14.0",
                GAMJA_CREDENTIAL_ENGINE_SHA256=expected_engine_sha256,
                GAMJA_CREDENTIAL_ENGINE_REQUIREMENT=mac_designated_requirement(MAC_ENGINE_IDENTIFIER),
                GAMJA_BUILD_ID=build_id,
            )
            subprocess.run([
                "cargo", "build", "--locked", "--release", "--target", target,
                "--manifest-path", str(ROOT / "core-host/Cargo.toml"),
            ], check=True, env=environment)
            core = stage / "bin/gamja"
            shutil.copy2(cargo_target / target / "release/gamja", core)
            sign_macos_binary(core, MAC_CORE_IDENTIFIER, bytearray(password))
            source_paths = build_input_paths(with_helper=bool(prior.get("helper")), macos=True)
            provenance = dict(prior.get("provenance") or {})
            provenance.update(
                build_id=build_id,
                source_commit=subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                source_sha256={name: sha256(ROOT / name) for name in source_paths},
                reused_engine_sha256=expected_engine_sha256,
            )
            write_manifest(stage, target=target, version=prior["version"], provenance=provenance)
            verify_package(stage, target)
            if sha256(stage / "libexec/gamja/gamja-engine") != expected_engine_sha256:
                raise ValueError("Core-only engine changed after relink")
            replace_directory(stage, output)
    finally:
        clear_secret(password)
    print(f"native Core-only package built: {output} ({target}); installed verification NOT performed")


def build_stable_broker_core_only(
        source, output, *, expected_manifest_sha256, expected_engine_sha256,
        broker_source, core_signing_password_fd, build_id, public_receipt_path,
        supervisor_pid=None, mcp_broker_source=None, publisher_broker_source=None,
        vault_broker_source=None, slack_broker_source=None, clone_relink_source=False):
    """Reuse the accepted broker bytes; relink and sign only Core and authority.

    Optional MCP credentials use a separately signed, exact pinned broker and
    authority; the immutable legacy broker is never rebuilt or re-signed.
    This creates a candidate, not an installed or migrated runtime. The public
    receipt is exclusive and outside the prefix; it binds the actual published
    manifest and authority without adding circular claims to the authority.
    """
    if type(clone_relink_source) is not bool:
        raise ValueError("invalid Core relink copy mode")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("stable broker build requires native Apple Silicon")
    if supervisor_pid is not None and (
            type(supervisor_pid) is not int or supervisor_pid <= 1
            or os.getppid() != supervisor_pid or os.getsid(0) != os.getpid()
            or os.getpgrp() != os.getpid()):
        raise ValueError("stable broker build supervisor binding invalid")
    if not isinstance(build_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{7,63}", build_id):
        raise ValueError("invalid stable broker build ID")
    source = _real_owned_directory(Path(source).absolute(), "stable broker source")
    output, receipt = Path(output).absolute(), Path(public_receipt_path).absolute()
    live = Path.home().joinpath(".local/share/gamja-bot/core/runtime").resolve(strict=False)
    for protected in (source, live):
        if (output == protected or output.is_relative_to(protected)
                or protected.is_relative_to(output)):
            raise ValueError("stable broker output overlaps a protected prefix")
    if (output.exists() or output.is_symlink() or receipt.exists() or receipt.is_symlink()
            or any(receipt == root or receipt.is_relative_to(root) for root in (output, source, live))):
        raise ValueError("stable broker output or public receipt is invalid")
    for parent, label in ((output.parent, "stable broker output"),
                          (receipt.parent, "stable broker public receipt")):
        ancestor = parent
        while not ancestor.exists() and not ancestor.is_symlink():
            ancestor = ancestor.parent
        _real_owned_directory(ancestor, f"{label} ancestor")
        parent.mkdir(parents=True, exist_ok=True)
        _real_owned_directory(parent, f"{label} parent")
    if output.parent.stat().st_dev != receipt.parent.stat().st_dev:
        raise ValueError("stable broker public receipt requires the output filesystem")
    broker_source = _validate_stable_broker(Path(broker_source).absolute())
    if slack_broker_source is not None:
        slack_broker_source = _validate_slack_broker(Path(slack_broker_source).absolute())
    if mcp_broker_source is not None:
        mcp_broker_source = _validate_mcp_broker(Path(mcp_broker_source).absolute())
    if publisher_broker_source is not None:
        publisher_broker_source = _validate_publisher_broker(Path(publisher_broker_source).absolute())
    if vault_broker_source is not None:
        vault_broker_source = _validate_vault_broker(Path(vault_broker_source).absolute())
    cargo_target_name = os.environ.get("CARGO_TARGET_DIR")
    if not cargo_target_name:
        raise ValueError("set CARGO_TARGET_DIR to the existing shared Cargo target")
    cargo_target = Path(cargo_target_name).resolve(strict=True)
    validate_macos_signing_inputs()
    source_paths = build_input_paths(with_helper=True, macos=True)
    source_hashes = {name: sha256(ROOT / name) for name in source_paths}
    source_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    target = "aarch64-apple-darwin"
    assets, password = [], None
    try:
        for path in (broker_source, source / MANIFEST, source / "bin/gamja",
                     source / "libexec/gamja/gamja-engine"):
            assets.append(_FrozenRuntimeAsset(path))
        if (assets[0].digest != ACCEPTED_BROKER_SHA256
                or assets[1].digest != expected_manifest_sha256
                or assets[3].digest != expected_engine_sha256):
            raise ValueError("stable broker source pin mismatch")
        if slack_broker_source is not None:
            assets.append(_FrozenRuntimeAsset(slack_broker_source))
        if mcp_broker_source is not None:
            assets.append(_FrozenRuntimeAsset(mcp_broker_source))
        if publisher_broker_source is not None:
            assets.append(_FrozenRuntimeAsset(publisher_broker_source))
        if vault_broker_source is not None:
            assets.append(_FrozenRuntimeAsset(vault_broker_source))
        password = read_secret_fd(core_signing_password_fd)
        with tempfile.TemporaryDirectory(prefix=".gamja-broker-package-", dir=output.parent) as temporary:
            stage = Path(temporary) / "payload"
            prior = stage_core_relink_source(
                source, stage, target, expected_manifest_sha256, expected_engine_sha256,
                clone_files=clone_relink_source)
            broker = stage / BROKER_PATH
            shutil.copy2(broker_source, broker)
            _validate_stable_broker(broker)
            if slack_broker_source is not None:
                shutil.copy2(slack_broker_source, stage / SLACK_BROKER_PATH)
                _validate_slack_broker(stage / SLACK_BROKER_PATH)
            elif any((stage / name).exists() for name in (SLACK_BROKER_PATH, SLACK_AUTHORITY_PATH)):
                raise ValueError("explicit Slack broker input is required for relinking a Slack package")
            if mcp_broker_source is not None:
                shutil.copy2(mcp_broker_source, stage / MCP_BROKER_PATH)
                _validate_mcp_broker(stage / MCP_BROKER_PATH)
            elif any((stage / name).exists() for name in (MCP_BROKER_PATH, MCP_AUTHORITY_PATH)):
                raise ValueError("explicit MCP broker input is required for relinking an MCP package")
            if publisher_broker_source is not None:
                shutil.copy2(publisher_broker_source, stage / PUBLISHER_BROKER_PATH)
                _validate_publisher_broker(stage / PUBLISHER_BROKER_PATH)
            elif any((stage / name).exists() for name in (PUBLISHER_BROKER_PATH, PUBLISHER_AUTHORITY_PATH)):
                raise ValueError("explicit Publisher broker input is required for relinking a Publisher package")
            if vault_broker_source is not None:
                shutil.copy2(vault_broker_source, stage / VAULT_BROKER_PATH)
                _validate_vault_broker(stage / VAULT_BROKER_PATH)
            elif any((stage / name).exists() for name in (VAULT_BROKER_PATH, VAULT_AUTHORITY_PATH)):
                raise ValueError("explicit Vault broker input is required for relinking a Vault package")
            environment = os.environ.copy()
            for name in ("GAMJA_BROKER_SHA256", "GAMJA_SLACK_BROKER_SHA256", "GAMJA_MCP_BROKER_SHA256", "GAMJA_PUBLISHER_BROKER_SHA256", "GAMJA_VAULT_BROKER_SHA256", "GAMJA_CORE_SHA256", "GAMJA_ENGINE_SHA256"):
                environment.pop(name, None)
            environment.update(
                MACOSX_DEPLOYMENT_TARGET="14.0", GAMJA_BUILD_ID=build_id,
                GAMJA_CREDENTIAL_ENGINE_SHA256=expected_engine_sha256,
                GAMJA_CREDENTIAL_ENGINE_REQUIREMENT=mac_designated_requirement(MAC_ENGINE_IDENTIFIER))
            if slack_broker_source is not None:
                environment["GAMJA_SLACK_BROKER_SHA256"] = _accepted_slack_broker_pin()
            if mcp_broker_source is not None:
                environment["GAMJA_MCP_BROKER_SHA256"] = _accepted_mcp_broker_pin()
            if publisher_broker_source is not None:
                environment["GAMJA_PUBLISHER_BROKER_SHA256"] = _accepted_publisher_broker_pin()
            if vault_broker_source is not None:
                environment["GAMJA_VAULT_BROKER_SHA256"] = _accepted_vault_broker_pin()
            def compile_binary(name):
                subprocess.run([
                    "cargo", "build", "--offline", "--locked", "-j2", "--release",
                    "--target", target, "--manifest-path", str(ROOT / "core-host/Cargo.toml"),
                    "--bin", name,
                ], check=True, env=environment)
                return _real_owned_file(cargo_target / target / "release" / name,
                                        "stable broker compiled executable")

            core = stage / "bin/gamja"
            shutil.copy2(compile_binary("gamja"), core)
            signing_options = {"supervised_group": True} if supervisor_pid is not None else {}
            sign_macos_binary(core, MAC_CORE_IDENTIFIER, bytearray(password), **signing_options)
            environment.update(GAMJA_BROKER_SHA256=ACCEPTED_BROKER_SHA256,
                               GAMJA_CORE_SHA256=sha256(core),
                               GAMJA_ENGINE_SHA256=expected_engine_sha256)
            authority = stage / AUTHORITY_PATH
            shutil.copy2(compile_binary("gamja-credential-release-authority"), authority)
            sign_macos_binary(authority, MAC_RELEASE_AUTHORITY_IDENTIFIER, bytearray(password), **signing_options)
            if slack_broker_source is not None:
                slack_authority = stage / SLACK_AUTHORITY_PATH
                shutil.copy2(compile_binary("gamja-slack-credential-release-authority"), slack_authority)
                sign_macos_binary(slack_authority, MAC_SLACK_RELEASE_AUTHORITY_IDENTIFIER, bytearray(password), **signing_options)
            if mcp_broker_source is not None:
                mcp_authority = stage / MCP_AUTHORITY_PATH
                shutil.copy2(compile_binary("gamja-mcp-credential-release-authority"), mcp_authority)
                sign_macos_binary(mcp_authority, MAC_MCP_RELEASE_AUTHORITY_IDENTIFIER, bytearray(password), **signing_options)
            if publisher_broker_source is not None:
                publisher_authority = stage / PUBLISHER_AUTHORITY_PATH
                shutil.copy2(compile_binary("gamja-template-publisher-credential-release-authority"), publisher_authority)
                sign_macos_binary(publisher_authority, MAC_PUBLISHER_RELEASE_AUTHORITY_IDENTIFIER, bytearray(password), **signing_options)
            if vault_broker_source is not None:
                vault_authority = stage / VAULT_AUTHORITY_PATH
                shutil.copy2(compile_binary("gamja-vault-credential-release-authority"), vault_authority)
                sign_macos_binary(vault_authority, MAC_VAULT_RELEASE_AUTHORITY_IDENTIFIER, bytearray(password), **signing_options)
            if ({name: sha256(ROOT / name) for name in source_paths} != source_hashes
                    or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
                    != source_commit):
                raise ValueError("stable broker build source drift")
            for asset in assets:
                asset._recheck()
            verify_package(source, target)
            provenance = dict(prior.get("provenance") or {})
            provenance.update(build_id=build_id, source_commit=source_commit,
                              source_sha256=source_hashes, reused_engine_sha256=expected_engine_sha256)
            write_manifest(stage, target=target, version=prior["version"], provenance=provenance)
            manifest = verify_package(stage, target)
            if (sha256(stage / "libexec/gamja/gamja-engine") != expected_engine_sha256
                    or sha256(broker) != ACCEPTED_BROKER_SHA256 or broker.stat().st_nlink != 1):
                raise ValueError("stable broker reused assets changed")
            if slack_broker_source is not None:
                _validate_slack_broker(stage / SLACK_BROKER_PATH)
                if (stage / SLACK_BROKER_PATH).stat().st_nlink != 1:
                    raise ValueError("Slack broker must not be hardlinked")
            if mcp_broker_source is not None:
                _validate_mcp_broker(stage / MCP_BROKER_PATH)
                if (stage / MCP_BROKER_PATH).stat().st_nlink != 1:
                    raise ValueError("MCP broker must not be hardlinked")
            if publisher_broker_source is not None:
                _validate_publisher_broker(stage / PUBLISHER_BROKER_PATH)
                if (stage / PUBLISHER_BROKER_PATH).stat().st_nlink != 1:
                    raise ValueError("Publisher broker must not be hardlinked")
            if vault_broker_source is not None:
                _validate_vault_broker(stage / VAULT_BROKER_PATH)
                if (stage / VAULT_BROKER_PATH).stat().st_nlink != 1:
                    raise ValueError("Vault broker must not be hardlinked")
            record = {"version": 1, "operation": "stable_broker_package", "build_id": build_id,
                      "source_copy_mode": "clonefile" if clone_relink_source else "copy2",
                      "manifest_sha256": sha256(stage / MANIFEST),
                      "broker_sha256": ACCEPTED_BROKER_SHA256,
                      "authority_sha256": sha256(authority), "core_sha256": sha256(core),
                      "engine_sha256": expected_engine_sha256,
                      "installed_verification": False}
            if slack_broker_source is not None:
                record.update(slack_broker_sha256=_accepted_slack_broker_pin(), slack_authority_sha256=sha256(stage / SLACK_AUTHORITY_PATH))
            if mcp_broker_source is not None:
                record.update(mcp_broker_sha256=_accepted_mcp_broker_pin(),
                              mcp_authority_sha256=sha256(stage / MCP_AUTHORITY_PATH),
                              legacy_broker_artifact_unchanged=True)
            if publisher_broker_source is not None:
                record.update(publisher_broker_sha256=_accepted_publisher_broker_pin(),
                              publisher_authority_sha256=sha256(stage / PUBLISHER_AUTHORITY_PATH))
            if vault_broker_source is not None:
                record.update(vault_broker_sha256=_accepted_vault_broker_pin(),
                              vault_authority_sha256=sha256(stage / VAULT_AUTHORITY_PATH))
            receipt_stage = Path(temporary) / "receipt.json"
            with receipt_stage.open("x", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(json.dumps(record, sort_keys=True, indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                receipt_identity = _runtime_asset_identity(os.fstat(stream.fileno()))[:2]
            package_identity = _runtime_asset_identity(stage.stat())[:2]
            try:
                for asset in assets:
                    asset._recheck()
                publish_directory_exclusive(stage, output)
                if verify_package(output, target) != manifest:
                    raise ValueError("stable broker published package changed")
                if sha256(output / MANIFEST) != record["manifest_sha256"]:
                    raise ValueError("stable broker published manifest changed")
                publish_exclusive_file(receipt_stage, receipt)
            except BaseException:
                # Remove only artifacts still proven to belong to this attempt.
                if receipt.exists() and _runtime_asset_identity(receipt.lstat())[:2] == receipt_identity:
                    receipt.unlink()
                if output.exists() and _runtime_asset_identity(output.lstat())[:2] == package_identity:
                    if (inventory(output) != manifest["files"]
                            or sha256(output / MANIFEST) != record["manifest_sha256"]):
                        raise ValueError("stable broker publication cleanup uncertain") from None
                    output.rename(stage)
                raise
    finally:
        if password is not None:
            clear_secret(password)
        for asset in assets:
            asset.close()
    print(f"native stable broker package built: {output}; installed verification NOT performed")


def candidate_builds(output, target, *, build_id=None, second_output=None,
                     second_build_id=None):
    output = Path(output).absolute()
    if second_output is None:
        if second_build_id is not None:
            raise ValueError("second build ID requires a second output")
        return [(output, build_id)]
    if not target.endswith("apple-darwin"):
        raise ValueError("dual signed candidates require native macOS")
    second_output = Path(second_output).absolute()
    ids = (build_id, second_build_id)
    if (any(not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{7,63}", value)
            for value in ids) or ids[0] == ids[1]):
        raise ValueError("dual candidate build IDs must be distinct bounded public values")
    if (output == second_output or output.is_relative_to(second_output)
            or second_output.is_relative_to(output)):
        raise ValueError("dual candidate outputs overlap")
    return [(output, build_id), (second_output, second_build_id)]


def operator_verifier_request(output, target, candidates, verifier_output,
                              verifier_build_id):
    if verifier_output is None and verifier_build_id is None:
        return None
    if verifier_output is None or verifier_build_id is None:
        raise ValueError("operator verifier requires both output and build ID")
    if target != "aarch64-apple-darwin":
        raise ValueError("operator runtime verifier requires native macOS arm64")
    output = Path(output).absolute()
    verifier_output = Path(verifier_output).absolute()
    if verifier_output.parent != output.parent:
        raise ValueError("operator verifier must be a sibling of the Core output")
    if (not re.fullmatch(r"[a-z0-9][a-z0-9._-]{7,63}", verifier_build_id)
            or verifier_build_id in {candidate_id for _path, candidate_id in candidates}):
        raise ValueError("operator verifier build ID must be bounded and distinct")
    for candidate, _candidate_id in candidates:
        if (verifier_output == candidate or verifier_output.is_relative_to(candidate)
                or candidate.is_relative_to(verifier_output)):
            raise ValueError("operator verifier output overlaps a candidate")
    return verifier_output, verifier_build_id


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_exclusive_file(stage, destination):
    stage = Path(stage)
    destination = Path(destination)
    try:
        os.link(stage, destination)
    except FileExistsError:
        raise ValueError("operator verifier output already exists") from None
    stage.unlink()
    fsync_directory(destination.parent)


def build(output, *, with_helper=False, with_browsers=True,
          engine_signing_password_fd=None, core_signing_password_fd=None,
          build_id=None, second_output=None, second_build_id=None,
          supervised_signing=False, runtime_verifier_output=None,
          runtime_verifier_build_id=None):
    target = TARGETS.get((platform.system(), platform.machine()))
    if target is None:
        raise ValueError("unsupported native host")
    rust = subprocess.check_output(["rustc", "-vV"], text=True)
    rust_host = next(line.removeprefix("host: ") for line in rust.splitlines() if line.startswith("host: "))
    versions = {name: metadata.version(name) for name in PINS}
    validate_toolchain(target, python_version=sys.version_info[:3], rust_host=rust_host, versions=versions)
    cargo_target = os.environ.get("CARGO_TARGET_DIR")
    if not cargo_target:
        raise ValueError("set CARGO_TARGET_DIR to the existing shared Cargo target")
    cargo_target = Path(cargo_target).resolve()
    import playwright
    entries = json.loads((Path(playwright.__file__).parent / "driver/package/browsers.json").read_text())["browsers"]
    revisions = {item["name"]: item["revision"] for item in entries}
    if any(revisions.get(name) != revision for name, revision in BROWSER_REVISIONS.items()):
        raise ValueError("Playwright browser revision mismatch")
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or (Path.home() / "Library/Caches/ms-playwright" if platform.system() == "Darwin" else Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"))
    payloads = [cache / f"{name.replace('-', '_')}-{revision}" for name, revision in BROWSER_REVISIONS.items()] if with_browsers else []
    if any(not (path / "INSTALLATION_COMPLETE").is_file() for path in payloads):
        raise ValueError("missing pinned browser payload; preinstall it on the build host")
    candidates = candidate_builds(output, target, build_id=build_id,
                                  second_output=second_output,
                                  second_build_id=second_build_id)
    verifier_request = operator_verifier_request(
        output, target, candidates, runtime_verifier_output,
        runtime_verifier_build_id,
    )
    for destination, _candidate_id in candidates:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise ValueError("candidate output already exists")
    if verifier_request is not None:
        verifier_output, _verifier_id = verifier_request
        if verifier_output.exists() or verifier_output.is_symlink():
            raise ValueError("operator verifier output already exists")
    output = candidates[0][0]
    suffix = ".exe" if target.endswith("windows-msvc") else ""
    version = tomllib.loads((ROOT / "core-host/Cargo.toml").read_text())["package"]["version"]
    if platform.system() == "Darwin":
        validate_macos_signing_inputs()
    with tempfile.TemporaryDirectory(prefix=".gamja-core-build-", dir=output.parent) as temporary:
        work = Path(temporary)
        engine_password = None
        core_password = None
        if platform.system() == "Darwin":
            engine_password = read_secret_fd(engine_signing_password_fd)
            try:
                core_password = read_secret_fd(core_signing_password_fd)
                frozen_engine, embedded_cdhashes = build_macos_frozen_engine(work, engine_password)
                if supervised_signing:
                    signed_engine_hash = prepare_macos_engine(
                        frozen_engine, engine_password, embedded_cdhashes, work,
                        supervised_group=True)
                else:
                    signed_engine_hash = prepare_macos_engine(
                        frozen_engine, engine_password, embedded_cdhashes, work)
            except BaseException:
                clear_secret(engine_password)
                if core_password is not None:
                    clear_secret(core_password)
                raise
        else:
            subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                            "--distpath", str(work / "engine-dist"),
                            "--workpath", str(work / "engine-build"), "gamja-engine.spec"],
                           cwd=ROOT / "backend", check=True)
            frozen_engine = work / "engine-dist" / f"gamja-engine{suffix}"
        manifests = ["core-host/Cargo.toml"] + (["platform-helper/Cargo.toml"] if with_helper else [])
        cargo_environment = os.environ.copy()
        if platform.system() == "Darwin":
            cargo_environment["MACOSX_DEPLOYMENT_TARGET"] = "14.0"
            # The hash authenticates the exact constrained and signed frozen engine bytes.
            cargo_environment["GAMJA_CREDENTIAL_ENGINE_SHA256"] = signed_engine_hash
            cargo_environment["GAMJA_CREDENTIAL_ENGINE_REQUIREMENT"] = mac_designated_requirement(MAC_ENGINE_IDENTIFIER)
        source_paths = build_input_paths(with_helper=with_helper, macos=platform.system() == "Darwin")
        common_provenance = {"source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(), "source_sha256": {name: sha256(ROOT / name) for name in source_paths}, "rust": rust.strip(), "python": sys.version, "pip_freeze": subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True).splitlines(), "installed_verification": "not performed"}
        staged = []
        try:
            verifier_stage = None
            if verifier_request is not None:
                verifier_output, verifier_id = verifier_request
                verifier_environment = cargo_environment.copy()
                verifier_environment["GAMJA_BUILD_ID"] = verifier_id
                subprocess.run(
                    ["cargo", "build", "--locked", "--release", "--target", target,
                     "--manifest-path", str(ROOT / "core-host/Cargo.toml")],
                    check=True,
                    env=verifier_environment,
                )
                verifier_stage = work / "runtime-verifier"
                shutil.copy2(cargo_target / target / "release" / "gamja", verifier_stage)
                if supervised_signing:
                    sign_macos_binary(
                        verifier_stage, MAC_CORE_IDENTIFIER, bytearray(core_password),
                        supervised_group=True,
                    )
                else:
                    sign_macos_binary(
                        verifier_stage, MAC_CORE_IDENTIFIER, bytearray(core_password)
                    )
            for index, (destination, candidate_id) in enumerate(candidates):
                if shutil.disk_usage(ROOT).free < BUILD_HARD_FLOOR:
                    raise ValueError("core build hard disk floor")
                stage = work / f"payload-{index}"
                private = stage / "libexec/gamja"
                private.mkdir(parents=True)
                (stage / "bin").mkdir()
                shutil.copy2(frozen_engine, private / f"gamja-engine{suffix}")
                candidate_environment = cargo_environment.copy()
                if candidate_id is not None:
                    candidate_environment["GAMJA_BUILD_ID"] = candidate_id
                for manifest in manifests:
                    subprocess.run(["cargo", "build", "--locked", "--release", "--target", target, "--manifest-path", str(ROOT / manifest)], check=True, env=candidate_environment)
                shutil.copy2(cargo_target / target / "release" / f"gamja{suffix}", stage / "bin" / f"gamja{suffix}")
                if suffix:
                    shutil.copy2(cargo_target / target / "release/gamja_core_host.dll", private / "gamja_core_host.dll")
                if with_helper:
                    shutil.copy2(cargo_target / target / "release" / f"gamja-platform-helper{suffix}", private / f"gamja-platform-helper{suffix}")
                if platform.system() == "Darwin":
                    if supervised_signing:
                        sign_macos_binary(stage / "bin/gamja", MAC_CORE_IDENTIFIER,
                                          bytearray(core_password),
                                          supervised_group=True)
                    else:
                        sign_macos_binary(stage / "bin/gamja", MAC_CORE_IDENTIFIER,
                                          bytearray(core_password))
                    if with_helper:
                        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none",
                                       str(private / "gamja-platform-helper")], check=True)
                for source in payloads:
                    shutil.copytree(source, private / "playwright-browsers" / source.name, symlinks=True)
                write_notices(stage, version)
                provenance = dict(common_provenance)
                provenance["build_id"] = candidate_id or "default"
                write_manifest(stage, target=target, version=version, provenance=provenance)
                verify_package(stage, target)
                if shutil.disk_usage(ROOT).free < BUILD_HARD_FLOOR:
                    raise ValueError("core build hard disk floor")
                staged.append((stage, destination))
            if verifier_stage is not None:
                for stage, _destination in staged:
                    if sha256(verifier_stage) == sha256(stage / "bin/gamja"):
                        raise ValueError("operator verifier and candidate Core bytes are identical")
            if len(staged) == 2:
                first, second = (stage for stage, _destination in staged)
                if sha256(first / f"libexec/gamja/gamja-engine{suffix}") != sha256(second / f"libexec/gamja/gamja-engine{suffix}"):
                    raise ValueError("dual candidate engine bytes differ")
                if sha256(first / f"bin/gamja{suffix}") == sha256(second / f"bin/gamja{suffix}"):
                    raise ValueError("dual candidate Core bytes are identical")
            if verifier_stage is not None:
                publish_exclusive_file(verifier_stage, verifier_output)
            for stage, destination in staged:
                replace_directory(stage, destination)
        finally:
            if core_password is not None:
                clear_secret(core_password)
    built = ", ".join(str(destination) for destination, _candidate_id in candidates)
    print(f"native source package built: {built} ({target}); installed verification NOT performed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--with-helper", action="store_true", help="include optional native helper")
    parser.add_argument("--without-browsers", action="store_true", help="omit optional browser capability payload")
    parser.add_argument("--verify", type=Path, help="verify an existing prefix without building")
    parser.add_argument("--verify-runtime-receipt", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--runtime-verifier", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--runtime-verifier-sha256", help=argparse.SUPPRESS)
    parser.add_argument("--runtime-verifier-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--runtime-verifier-build-id", help=argparse.SUPPRESS)
    parser.add_argument("--target", choices=sorted(TARGETS.values()))
    parser.add_argument("--engine-signing-password-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--core-signing-password-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--supervised-signing-parent-pid", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--build-id", help=argparse.SUPPRESS)
    parser.add_argument("--second-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--second-build-id", help=argparse.SUPPRESS)
    parser.add_argument("--reuse-prefix", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--helper-repack-prefix", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--expected-engine-sha256", help=argparse.SUPPRESS)
    parser.add_argument("--expected-core-sha256", help=argparse.SUPPRESS)
    parser.add_argument("--expected-helper-sha256", help=argparse.SUPPRESS)
    parser.add_argument("--expected-manifest-sha256", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.verify:
            if (args.output is not None or args.with_helper or args.without_browsers
                    or args.engine_signing_password_fd is not None
                    or args.core_signing_password_fd is not None or args.build_id is not None
                    or args.supervised_signing_parent_pid is not None
                    or args.second_output is not None or args.second_build_id is not None
                    or args.runtime_verifier_output is not None
                    or args.runtime_verifier_build_id is not None
                    or args.reuse_prefix is not None or args.helper_repack_prefix is not None
                    or args.expected_manifest_sha256 is not None
                    or args.expected_core_sha256 is not None
                    or args.expected_engine_sha256 is not None
                    or args.expected_helper_sha256 is not None):
                raise ValueError("verify mode rejects build and reuse options")
            target = args.target or TARGETS.get((platform.system(), platform.machine()))
            if args.verify_runtime_receipt and target != "aarch64-apple-darwin":
                raise ValueError("runtime receipt verification is restricted to macOS arm64")
            if args.verify_runtime_receipt:
                if args.runtime_verifier is None or args.runtime_verifier_sha256 is None:
                    raise ValueError("independent runtime verifier is required")
            elif args.runtime_verifier is not None or args.runtime_verifier_sha256 is not None:
                raise ValueError("runtime verifier options require --verify-runtime-receipt")
            verify_package(args.verify, target,
                           allow_runtime_receipt=args.verify_runtime_receipt,
                           runtime_verifier=args.runtime_verifier,
                           runtime_verifier_sha256=args.runtime_verifier_sha256)
            print(f"package layout and all hashes verified: {args.verify}")
        else:
            if (args.verify_runtime_receipt or args.runtime_verifier is not None
                    or args.runtime_verifier_sha256 is not None):
                raise ValueError("runtime receipt verification requires --verify")
            if args.target and args.target != TARGETS.get((platform.system(), platform.machine())):
                raise ValueError("cross-target builds are prohibited; use a native host")
            target = TARGETS.get((platform.system(), platform.machine()), "unsupported")
            output = args.output or ROOT / "dist/core" / target / "gamja-core"
            if args.reuse_prefix is not None and args.helper_repack_prefix is not None:
                raise ValueError("select one bounded package reuse operation")
            if args.helper_repack_prefix is not None:
                if (args.with_helper or args.without_browsers
                        or args.engine_signing_password_fd is not None
                        or args.core_signing_password_fd is not None
                        or args.supervised_signing_parent_pid is not None
                        or args.second_output is not None or args.second_build_id is not None
                        or args.runtime_verifier_output is not None
                        or args.runtime_verifier_build_id is not None
                        or not args.expected_manifest_sha256 or not args.expected_core_sha256
                        or not args.expected_engine_sha256 or not args.expected_helper_sha256
                        or not args.build_id):
                    raise ValueError("invalid helper-only repack options")
                build_helper_only(
                    args.helper_repack_prefix, output,
                    expected_manifest_sha256=args.expected_manifest_sha256,
                    expected_core_sha256=args.expected_core_sha256,
                    expected_engine_sha256=args.expected_engine_sha256,
                    expected_helper_sha256=args.expected_helper_sha256,
                    build_id=args.build_id,
                )
            elif args.reuse_prefix is not None:
                if (args.with_helper or args.without_browsers
                        or args.engine_signing_password_fd is not None
                        or args.supervised_signing_parent_pid is not None
                        or args.second_output is not None or args.second_build_id is not None
                        or args.runtime_verifier_output is not None
                        or args.runtime_verifier_build_id is not None
                        or args.expected_core_sha256 is not None
                        or args.expected_helper_sha256 is not None
                        or not args.expected_manifest_sha256 or not args.expected_engine_sha256):
                    raise ValueError("invalid Core-only relink options")
                build_core_only(
                    args.reuse_prefix, output,
                    expected_manifest_sha256=args.expected_manifest_sha256,
                    expected_engine_sha256=args.expected_engine_sha256,
                    core_signing_password_fd=args.core_signing_password_fd,
                    build_id=args.build_id,
                )
            else:
                if (args.expected_manifest_sha256 is not None
                        or args.expected_core_sha256 is not None
                        or args.expected_engine_sha256 is not None
                        or args.expected_helper_sha256 is not None):
                    raise ValueError("full build rejects package reuse pins")
                supervised_signing = validate_supervised_signing(
                    args.supervised_signing_parent_pid,
                    target,
                    args.engine_signing_password_fd,
                    args.core_signing_password_fd,
                )
                build(output, with_helper=args.with_helper,
                      with_browsers=not args.without_browsers,
                      engine_signing_password_fd=args.engine_signing_password_fd,
                      core_signing_password_fd=args.core_signing_password_fd,
                      build_id=args.build_id, second_output=args.second_output,
                      second_build_id=args.second_build_id,
                      supervised_signing=supervised_signing,
                      runtime_verifier_output=args.runtime_verifier_output,
                      runtime_verifier_build_id=args.runtime_verifier_build_id)
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
