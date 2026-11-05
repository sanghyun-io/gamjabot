"""Build one native desktop target using the shared, pinned local toolchain.

Run on a native host for each target. Browser payloads must already be present
in Playwright's cache; this recipe never downloads them at runtime or build time.
"""

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TAURI = ROOT / "frontend" / "src-tauri"
ARTIFACT_ROOT = ROOT / "artifact-module"
ARTIFACT_TARGET = "aarch64-apple-darwin"
ARTIFACT_IDENTIFIER = "top.gamja.artifact"
SOURCE_BASE = "68f99247039c4aacc5e7c2554a1c9a1cc7547ace"
DIAGNOSTICS_HELP = ROOT / "scripts/Diagnostics Help.txt"
BUILD_INPUT_FREEZE = ROOT / "docs/.logs/screenshot-artifact-build-input-freeze-20261002.json"
CANDIDATE_RECEIPT = ROOT / "docs/.logs/screenshot-artifact-candidate-receipt-20261002.json"
R2_BUILD_INPUT_FREEZE = ROOT / "docs/.logs/screenshot-artifact-build-input-freeze-r2-20261002.json"
R2_CANDIDATE_RECEIPT = ROOT / "docs/.logs/screenshot-artifact-candidate-receipt-r2-20261002.json"
R2_INSTALLED_APP = Path("/Applications/Gamja Bot.app")
R2_INSTALLED_MAIN_SHA256 = "1be7db54d7f33509d229461da2b097afd030910c41e5b5a9fd4f06833fee4c19"
R2_INSTALLED_TREE_SHA256 = "e6e6eaf6f1dbb63071d52fc7c623b93fd0b55cd0b296918fa1e2b76944343a95"
R2_ARTIFACT_SHA256 = "4e9dfd5619402c4e9460dfe69f53937f659e31f21ed45ec9cd0084a94a2e2427"
R3_BUILD_INPUT_FREEZE = ROOT / "docs/.logs/screenshot-artifact-build-input-freeze-r3-20261002.json"
R3_CANDIDATE_RECEIPT = ROOT / "docs/.logs/screenshot-artifact-candidate-receipt-r3-20261002.json"
R3_INSTALLED_APP = Path("/Applications/Gamja Bot.app")
R3_INSTALLED_MAIN_SHA256 = "562417201bbaec20f26f83f6299632aab095cf7fb49ef1c0cd6c9226d90c51ef"
R3_INSTALLED_TREE_SHA256 = "b51f5ba343c0a98fa287ffb712244429074970c5ce8cb7bae2db8e3788a290ae"
R3_ARTIFACT_SHA256 = R2_ARTIFACT_SHA256
R4_BUILD_INPUT_FREEZE = ROOT / "docs/.logs/screenshot-artifact-build-input-freeze-r4-20261002.json"
R4_CANDIDATE_RECEIPT = ROOT / "docs/.logs/screenshot-artifact-candidate-receipt-r4-20261002.json"
R4_INSTALLED_APP = Path("/Applications/Gamja Bot.app")
R4_INSTALLED_MAIN_SHA256 = "903bf7151021d7f4b516c2749e71ae5e6ff76d740da77122a6e78c537d9d7879"
R4_INSTALLED_TREE_SHA256 = "845d524fe16f319a0e1b72cb1bbe5ef54f24a169e0bd797d219bdffd7db122d0"
R4_ARTIFACT_SHA256 = R3_ARTIFACT_SHA256
R5_BUILD_INPUT_FREEZE = ROOT / "docs/.logs/screenshot-artifact-build-input-freeze-r5-20261002.json"
R5_CANDIDATE_RECEIPT = ROOT / "docs/.logs/screenshot-artifact-candidate-receipt-r5-20261002.json"
R5_INSTALLED_APP = Path("/Applications/Gamja Bot.app")
R5_INSTALLED_MAIN_SHA256 = "d8be695f94428d6364f1bb1db3eb0eac755d47f9a3fe8210825f3232aecb125c"
R5_INSTALLED_TREE_SHA256 = "943d8b48413d1c94235f2eb3e0959553f30c5ffd993ea4c917e8480dccaa3a43"
R5_ARTIFACT_SHA256 = R4_ARTIFACT_SHA256
R6_BUILD_INPUT_FREEZE = ROOT / "docs/.logs/screenshot-artifact-build-input-freeze-r6-20261003.json"
R6_CANDIDATE_RECEIPT = ROOT / "docs/.logs/screenshot-artifact-candidate-receipt-r6-20261003.json"
R6_INSTALLED_APP = Path("/Applications/Gamja Bot.app")
R6_INSTALLED_MAIN_SHA256 = "1d0fc2b2d5347fcbd3fb1684adc8b1c73c0ba2dd12b564661bc4ca0296c3a43f"
R6_INSTALLED_TREE_SHA256 = "956e6b53d7c8c0b22bdbf344da5500e946b6726eb17618e3b0e131b8e87295f0"
R6_ARTIFACT_SHA256 = R5_ARTIFACT_SHA256
R7_BUILD_INPUT_FREEZE = ROOT / "docs/.logs/screenshot-artifact-build-input-freeze-r7-20261003.json"
R7_CANDIDATE_RECEIPT = ROOT / "docs/.logs/screenshot-artifact-candidate-receipt-r7-20261003.json"
R7_INSTALLED_APP = Path("/Applications/Gamja Bot.app")
R7_INSTALLED_MAIN_SHA256 = "da1c9de38641e7740f32ff0e3ee11f03373c46ef3ea8862a07b5acd11c04f044"
R7_INSTALLED_TREE_SHA256 = "2aca2b69e4683834d46878c96bddf081c580b3052e86b3e8079a66ffd9ea27ac"
R7_ARTIFACT_SHA256 = R6_ARTIFACT_SHA256
R8_BUILD_INPUT_FREEZE = ROOT / "docs/.logs/screenshot-artifact-build-input-freeze-r8-20261003.json"
R8_CANDIDATE_RECEIPT = ROOT / "docs/.logs/screenshot-artifact-candidate-receipt-r8-20261003.json"
R8_INSTALLED_APP = Path("/Applications/Gamja Bot.app")
R8_INSTALLED_MAIN_SHA256 = "59bb1e18391215bcf656dc140f2176e84e8a70871f6c10f88ccd7a8aedf4ff3c"
R8_INSTALLED_TREE_SHA256 = "1132361ec1668c3490dd6c559d7a84f46a54a1e1bc1e5099ee1bdf5f34e7eff7"
R8_ARTIFACT_SHA256 = R7_ARTIFACT_SHA256
DEFAULT_ARM_CORE_PACKAGE = ROOT / "dist/core/aarch64-apple-darwin/gamja-core"
FIXED_BOOTSTRAP_MANIFEST_SHA256 = "c7db6f970b456e75eb23359dfdbe79e2aa1cb16c4c72eed5649bae3a3a11b27f"
FIXED_BOOTSTRAP_CORE_SHA256 = "b885e14bbacfdcbe7c68e4ff051db2d33ade88733d69471897fe4ed55da10a31"
FIXED_BOOTSTRAP_ENGINE_SHA256 = "2f6b534d61ef0212248ec76b6f92b9a9e5dc9e15dae21622c8ab785fa2bfded0"
FIXED_BOOTSTRAP_HELPER_SHA256 = "0d10afdab1573a67b5be78c9746512e56a2a81f1d7afd6d77efa7c8a00ae0d5d"
TARGETS = {
    ("Darwin", "arm64"): ("aarch64-apple-darwin", "dmg"),
    ("Darwin", "x86_64"): ("x86_64-apple-darwin", "dmg"),
    ("Windows", "AMD64"): ("x86_64-pc-windows-msvc", "nsis"),
}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_sha256(root):
    """Match the reviewed signing controller's complete regular-file tree pin."""
    root = Path(root)
    digest = hashlib.sha256()
    entries = sorted((path for path in root.rglob("*") if path.is_file()),
                     key=lambda item: item.relative_to(root).as_posix())
    for path in entries:
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256(path)))
    return digest.hexdigest()


def _real_owned_directory(path, code):
    path = Path(path).absolute()
    try:
        status = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        raise RuntimeError(code) from None
    if (not stat.S_ISDIR(status.st_mode) or path.is_symlink() or resolved != path
            or (os.name == "posix" and status.st_uid != os.geteuid())
            or status.st_mode & 0o022):
        raise RuntimeError(code)
    return path


def _real_owned_file(path, code, *, executable=False):
    path = Path(path).absolute()
    try:
        status = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        raise RuntimeError(code) from None
    if (not stat.S_ISREG(status.st_mode) or path.is_symlink() or resolved != path
            or status.st_nlink != 1
            or (os.name == "posix" and status.st_uid != os.geteuid())
            or status.st_mode & 0o022 or (executable and not status.st_mode & 0o111)):
        raise RuntimeError(code)
    return path


def artifact_source_snapshot(source=ARTIFACT_ROOT):
    """Freeze the complete standalone crate inputs, including the tracked lock."""
    source = _real_owned_directory(source, "artifact source directory invalid")
    required = (source / "Cargo.toml", source / "Cargo.lock")
    for path in required:
        _real_owned_file(path, "artifact source file missing or unsafe")
    files = [*required, *sorted((source / "src").glob("*.rs"))]
    if len(files) == len(required):
        raise RuntimeError("artifact source files missing")
    return {path.relative_to(source).as_posix(): sha256(path) for path in files}


def require_unchanged_artifact_source(source, expected):
    if artifact_source_snapshot(source) != expected:
        raise RuntimeError("artifact source drift during build")


_FORBIDDEN_ARTIFACT_PACKAGES = {
    "keychain", "security-framework", "security-framework-sys",
    "rusqlite", "libsqlite3-sys", "sqlite", "sqlite3-sys", "sqlx",
    "diesel", "sea-orm", "pyo3", "pyo3-ffi", "python", "python3-sys",
    "openai", "async-openai", "anthropic", "aws-sdk-bedrockruntime",
    "google-generative-ai-rs", "genai", "llm",
}


def verify_artifact_dependency_report(lock, report):
    """Reject credential, database, Python, and provider dependency packages."""
    lock = _real_owned_file(lock, "artifact source lock missing or unsafe")
    lock_text = lock.read_text(encoding="utf-8")
    names = set(re.findall(r'^name = "([^"]+)"$', lock_text, flags=re.MULTILINE))
    for line in report.splitlines():
        match = re.match(r"^([^\s]+)(?:\s+v?\d|$)", line.strip())
        if match:
            names.add(match.group(1))
    normalized = {name.lower().replace("_", "-") for name in names}
    forbidden = {name for name in normalized if name in _FORBIDDEN_ARTIFACT_PACKAGES}
    if forbidden:
        raise RuntimeError("forbidden artifact dependency: " + ", ".join(sorted(forbidden)))
    return sha256(lock)


def verify_artifact_lock_tracked(*, run=subprocess.run):
    """Require the reviewed lock blob, not an untracked or dirty local substitute."""
    relative = "artifact-module/Cargo.lock"
    for arguments in (
        ["git", "ls-files", "--error-unmatch", "--", relative],
        ["git", "diff", "--quiet", "HEAD", "--", relative],
    ):
        result = run(arguments, cwd=ROOT, stdin=subprocess.DEVNULL,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if result.returncode != 0:
            raise RuntimeError("artifact Cargo.lock is missing, untracked, or drifted")


def build_artifact_binary(shared_target, *, run=subprocess.run):
    """Build the standalone module once with the shared cache and locked graph."""
    source = _real_owned_directory(ARTIFACT_ROOT, "artifact source directory invalid")
    target = _real_owned_directory(shared_target, "shared Cargo target invalid")
    if source == ROOT / "artifact-module":
        verify_artifact_lock_tracked()
    snapshot = artifact_source_snapshot(source)
    cargo = shutil.which("cargo")
    if cargo is None:
        raise RuntimeError("cargo is not installed")
    manifest = source / "Cargo.toml"
    environment = {**os.environ, "CARGO_TARGET_DIR": str(target)}
    tree_args = [
        cargo, "tree", "--locked", "--manifest-path", str(manifest),
        "--target", ARTIFACT_TARGET, "--edges", "normal", "--prefix", "none",
        "--format", "{p}",
    ]
    tree = run(tree_args, cwd=ROOT, env=environment, check=False,
               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
    if tree.returncode != 0:
        raise RuntimeError("artifact locked dependency graph failed")
    verify_artifact_dependency_report(source / "Cargo.lock", tree.stdout)
    require_unchanged_artifact_source(source, snapshot)
    build_args = [
        cargo, "build", "--locked", "--release", "--target", ARTIFACT_TARGET,
        "-j", "2", "--manifest-path", str(manifest),
    ]
    built = run(build_args, cwd=ROOT, env=environment, check=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1800)
    if built.returncode != 0:
        raise RuntimeError("artifact locked build failed")
    require_unchanged_artifact_source(source, snapshot)
    output = target / ARTIFACT_TARGET / "release/gamja-artifact"
    return _real_owned_file(output, "artifact build output missing or unsafe", executable=True)


def _artifact_tool_output(run, arguments, code):
    result = run(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                 stderr=subprocess.PIPE, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(code)
    return (result.stdout or "") + (result.stderr or "")


def verify_artifact_binary(binary, expected_sha256, *, run=subprocess.run):
    """Bind exact bytes, forbidden native linkage/imports, and the ad-hoc seal."""
    binary = _real_owned_file(binary, "artifact binary missing or unsafe", executable=True)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or "") or sha256(binary) != expected_sha256:
        raise RuntimeError("artifact hash mismatch")
    linkage = _artifact_tool_output(run, ["/usr/bin/otool", "-L", str(binary)],
                                    "artifact linkage inspection failed")
    symbols = _artifact_tool_output(run, ["/usr/bin/nm", "-u", str(binary)],
                                    "artifact symbol inspection failed")
    forbidden = re.compile(
        r"keychain|security\.framework|sqlite|python|pyo3|openai|anthropic|bedrock|generativeai",
        re.IGNORECASE,
    )
    if forbidden.search(linkage + "\n" + symbols):
        raise RuntimeError("forbidden artifact linkage or import")
    _artifact_tool_output(
        run, ["/usr/bin/codesign", "--verify", "--strict", "--all-architectures", str(binary)],
        "artifact signature invalid",
    )
    details = _artifact_tool_output(
        run, ["/usr/bin/codesign", "-d", "--verbose=4", str(binary)],
        "artifact signature details unavailable",
    )
    lines = details.splitlines()
    if (f"Identifier={ARTIFACT_IDENTIFIER}" not in lines
            or "Signature=adhoc" not in lines
            or any(line.startswith("Timestamp=") for line in lines)):
        raise RuntimeError("artifact signature policy invalid")
    return expected_sha256


def sign_artifact_ad_hoc(binary, *, run=subprocess.run):
    binary = _real_owned_file(binary, "artifact binary missing or unsafe", executable=True)
    result = run([
        "/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none",
        "--identifier", ARTIFACT_IDENTIFIER, str(binary),
    ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError("artifact ad-hoc signing failed")
    digest = sha256(binary)
    verify_artifact_binary(binary, digest, run=run)
    return digest


def verify_r2_installed_source():
    """Bind the already-reviewed r1 app used only as the r2 artifact source."""
    app = _real_owned_directory(R2_INSTALLED_APP, "r2 installed app invalid")
    main = _real_owned_file(app / "Contents/MacOS/gamja-bot",
                            "r2 installed app invalid", executable=True)
    artifact = _real_owned_file(app / "Contents/MacOS/gamja-artifact",
                                "r2 installed artifact invalid", executable=True)
    if sha256(main) != R2_INSTALLED_MAIN_SHA256 or tree_sha256(app) != R2_INSTALLED_TREE_SHA256:
        raise RuntimeError("r2 installed app changed")
    verify_artifact_binary(artifact, R2_ARTIFACT_SHA256)
    return artifact


def verify_r3_installed_source():
    """Bind the reviewed installed r2 app used only as the r3 artifact source."""
    app = _real_owned_directory(R3_INSTALLED_APP, "r3 installed app invalid")
    main = _real_owned_file(app / "Contents/MacOS/gamja-bot",
                            "r3 installed app invalid", executable=True)
    artifact = _real_owned_file(app / "Contents/MacOS/gamja-artifact",
                                "r3 installed artifact invalid", executable=True)
    if sha256(main) != R3_INSTALLED_MAIN_SHA256 or tree_sha256(app) != R3_INSTALLED_TREE_SHA256:
        raise RuntimeError("r3 installed app changed")
    verify_artifact_binary(artifact, R3_ARTIFACT_SHA256)
    return artifact


def verify_r4_installed_source():
    """Bind the reviewed installed r3 app used only as the r4 artifact source."""
    app = _real_owned_directory(R4_INSTALLED_APP, "r4 installed app invalid")
    main = _real_owned_file(app / "Contents/MacOS/gamja-bot",
                            "r4 installed app invalid", executable=True)
    artifact = _real_owned_file(app / "Contents/MacOS/gamja-artifact",
                                "r4 installed artifact invalid", executable=True)
    if sha256(main) != R4_INSTALLED_MAIN_SHA256 or tree_sha256(app) != R4_INSTALLED_TREE_SHA256:
        raise RuntimeError("r4 installed app changed")
    verify_artifact_binary(artifact, R4_ARTIFACT_SHA256)
    return artifact


def verify_r5_installed_source():
    """Bind the reviewed installed r4 app used only as the r5 artifact source."""
    app = _real_owned_directory(R5_INSTALLED_APP, "r5 installed app invalid")
    main = _real_owned_file(app / "Contents/MacOS/gamja-bot",
                            "r5 installed app invalid", executable=True)
    artifact = _real_owned_file(app / "Contents/MacOS/gamja-artifact",
                                "r5 installed artifact invalid", executable=True)
    if sha256(main) != R5_INSTALLED_MAIN_SHA256 or tree_sha256(app) != R5_INSTALLED_TREE_SHA256:
        raise RuntimeError("r5 installed app changed")
    verify_artifact_binary(artifact, R5_ARTIFACT_SHA256)
    return artifact


def verify_r6_installed_source():
    """Bind the reviewed installed r5 app used only as the r6 artifact source."""
    app = _real_owned_directory(R6_INSTALLED_APP, "r6 installed app invalid")
    main = _real_owned_file(app / "Contents/MacOS/gamja-bot",
                            "r6 installed app invalid", executable=True)
    artifact = _real_owned_file(app / "Contents/MacOS/gamja-artifact",
                                "r6 installed artifact invalid", executable=True)
    if sha256(main) != R6_INSTALLED_MAIN_SHA256 or tree_sha256(app) != R6_INSTALLED_TREE_SHA256:
        raise RuntimeError("r6 installed app changed")
    verify_artifact_binary(artifact, R6_ARTIFACT_SHA256)
    return artifact


def verify_r7_installed_source():
    """Bind the reviewed installed r6 app used only as the r7 artifact source."""
    app = _real_owned_directory(R7_INSTALLED_APP, "r7 installed app invalid")
    main = _real_owned_file(app / "Contents/MacOS/gamja-bot",
                            "r7 installed app invalid", executable=True)
    artifact = _real_owned_file(app / "Contents/MacOS/gamja-artifact",
                                "r7 installed artifact invalid", executable=True)
    if sha256(main) != R7_INSTALLED_MAIN_SHA256 or tree_sha256(app) != R7_INSTALLED_TREE_SHA256:
        raise RuntimeError("r7 installed app changed")
    verify_artifact_binary(artifact, R7_ARTIFACT_SHA256)
    return artifact


def verify_r8_installed_source():
    """Bind the reviewed installed r7 app used only as the r8 artifact source."""
    app = _real_owned_directory(R8_INSTALLED_APP, "r8 installed app invalid")
    main = _real_owned_file(app / "Contents/MacOS/gamja-bot",
                            "r8 installed app invalid", executable=True)
    artifact = _real_owned_file(app / "Contents/MacOS/gamja-artifact",
                                "r8 installed artifact invalid", executable=True)
    if sha256(main) != R8_INSTALLED_MAIN_SHA256 or tree_sha256(app) != R8_INSTALLED_TREE_SHA256:
        raise RuntimeError("r8 installed app changed")
    verify_artifact_binary(artifact, R8_ARTIFACT_SHA256)
    return artifact


def select_release_artifact(shared_target, *, reuse_installed_r2=False,
                            reuse_installed_r3=False, reuse_installed_r4=False,
                            reuse_installed_r5=False, reuse_installed_r6=False,
                            reuse_installed_r7=False, reuse_installed_r8=False):
    """Select the original build path or the one fixed, reviewed r2 leaf."""
    if sum((reuse_installed_r2, reuse_installed_r3, reuse_installed_r4,
            reuse_installed_r5, reuse_installed_r6, reuse_installed_r7,
            reuse_installed_r8)) > 1:
        raise RuntimeError("release artifact source is ambiguous")
    if reuse_installed_r8:
        verify_r8_installed_source()
        artifact = _real_owned_file(
            R8_INSTALLED_APP / "Contents/MacOS/gamja-artifact",
            "r8 installed artifact invalid", executable=True)
        status = artifact.stat()
        binding = (status.st_dev, status.st_ino, status.st_uid,
                   stat.S_IMODE(status.st_mode), sha256(artifact))
        if binding[-1] != R8_ARTIFACT_SHA256:
            raise RuntimeError("r8 installed artifact changed")
        return artifact, R8_ARTIFACT_SHA256, binding
    if reuse_installed_r7:
        verify_r7_installed_source()
        artifact = _real_owned_file(
            R7_INSTALLED_APP / "Contents/MacOS/gamja-artifact",
            "r7 installed artifact invalid", executable=True)
        status = artifact.stat()
        binding = (status.st_dev, status.st_ino, status.st_uid,
                   stat.S_IMODE(status.st_mode), sha256(artifact))
        if binding[-1] != R7_ARTIFACT_SHA256:
            raise RuntimeError("r7 installed artifact changed")
        return artifact, R7_ARTIFACT_SHA256, binding
    if reuse_installed_r6:
        verify_r6_installed_source()
        artifact = _real_owned_file(
            R6_INSTALLED_APP / "Contents/MacOS/gamja-artifact",
            "r6 installed artifact invalid", executable=True)
        status = artifact.stat()
        binding = (status.st_dev, status.st_ino, status.st_uid,
                   stat.S_IMODE(status.st_mode), sha256(artifact))
        if binding[-1] != R6_ARTIFACT_SHA256:
            raise RuntimeError("r6 installed artifact changed")
        return artifact, R6_ARTIFACT_SHA256, binding
    if reuse_installed_r5:
        verify_r5_installed_source()
        artifact = _real_owned_file(
            R5_INSTALLED_APP / "Contents/MacOS/gamja-artifact",
            "r5 installed artifact invalid", executable=True)
        status = artifact.stat()
        binding = (status.st_dev, status.st_ino, status.st_uid,
                   stat.S_IMODE(status.st_mode), sha256(artifact))
        if binding[-1] != R5_ARTIFACT_SHA256:
            raise RuntimeError("r5 installed artifact changed")
        return artifact, R5_ARTIFACT_SHA256, binding
    if reuse_installed_r4:
        verify_r4_installed_source()
        artifact = _real_owned_file(
            R4_INSTALLED_APP / "Contents/MacOS/gamja-artifact",
            "r4 installed artifact invalid", executable=True)
        status = artifact.stat()
        binding = (status.st_dev, status.st_ino, status.st_uid,
                   stat.S_IMODE(status.st_mode), sha256(artifact))
        if binding[-1] != R4_ARTIFACT_SHA256:
            raise RuntimeError("r4 installed artifact changed")
        return artifact, R4_ARTIFACT_SHA256, binding
    if reuse_installed_r3:
        verify_r3_installed_source()
        artifact = _real_owned_file(
            R3_INSTALLED_APP / "Contents/MacOS/gamja-artifact",
            "r3 installed artifact invalid", executable=True)
        status = artifact.stat()
        binding = (status.st_dev, status.st_ino, status.st_uid,
                   stat.S_IMODE(status.st_mode), sha256(artifact))
        if binding[-1] != R3_ARTIFACT_SHA256:
            raise RuntimeError("r3 installed artifact changed")
        return artifact, R3_ARTIFACT_SHA256, binding
    if reuse_installed_r2:
        verify_r2_installed_source()
        artifact = _real_owned_file(
            R2_INSTALLED_APP / "Contents/MacOS/gamja-artifact",
            "r2 installed artifact invalid", executable=True)
        status = artifact.stat()
        binding = (status.st_dev, status.st_ino, status.st_uid,
                   stat.S_IMODE(status.st_mode), sha256(artifact))
        if binding[-1] != R2_ARTIFACT_SHA256:
            raise RuntimeError("r2 installed artifact changed")
        return artifact, R2_ARTIFACT_SHA256, binding
    artifact = build_artifact_binary(shared_target)
    return artifact, sign_artifact_ad_hoc(artifact), None


def require_reused_r2_artifact_unchanged(expected):
    artifact = _real_owned_file(
        R2_INSTALLED_APP / "Contents/MacOS/gamja-artifact",
        "reused artifact changed", executable=True)
    status = artifact.stat()
    current = (status.st_dev, status.st_ino, status.st_uid,
               stat.S_IMODE(status.st_mode), sha256(artifact))
    if current != expected:
        raise RuntimeError("reused artifact changed")


def require_reused_r3_artifact_unchanged(expected):
    artifact = _real_owned_file(
        R3_INSTALLED_APP / "Contents/MacOS/gamja-artifact",
        "reused artifact changed", executable=True)
    status = artifact.stat()
    current = (status.st_dev, status.st_ino, status.st_uid,
               stat.S_IMODE(status.st_mode), sha256(artifact))
    if current != expected:
        raise RuntimeError("reused artifact changed")


def require_reused_r4_artifact_unchanged(expected):
    artifact = _real_owned_file(
        R4_INSTALLED_APP / "Contents/MacOS/gamja-artifact",
        "reused artifact changed", executable=True)
    status = artifact.stat()
    current = (status.st_dev, status.st_ino, status.st_uid,
               stat.S_IMODE(status.st_mode), sha256(artifact))
    if current != expected:
        raise RuntimeError("reused artifact changed")


def require_reused_r5_artifact_unchanged(expected):
    artifact = _real_owned_file(
        R5_INSTALLED_APP / "Contents/MacOS/gamja-artifact",
        "reused artifact changed", executable=True)
    status = artifact.stat()
    current = (status.st_dev, status.st_ino, status.st_uid,
               stat.S_IMODE(status.st_mode), sha256(artifact))
    if current != expected:
        raise RuntimeError("reused artifact changed")


def require_reused_r6_artifact_unchanged(expected):
    artifact = _real_owned_file(
        R6_INSTALLED_APP / "Contents/MacOS/gamja-artifact",
        "reused artifact changed", executable=True)
    status = artifact.stat()
    current = (status.st_dev, status.st_ino, status.st_uid,
               stat.S_IMODE(status.st_mode), sha256(artifact))
    if current != expected:
        raise RuntimeError("reused artifact changed")


def require_reused_r7_artifact_unchanged(expected):
    artifact = _real_owned_file(
        R7_INSTALLED_APP / "Contents/MacOS/gamja-artifact",
        "reused artifact changed", executable=True)
    status = artifact.stat()
    current = (status.st_dev, status.st_ino, status.st_uid,
               stat.S_IMODE(status.st_mode), sha256(artifact))
    if current != expected:
        raise RuntimeError("reused artifact changed")


def require_reused_r8_artifact_unchanged(expected):
    artifact = _real_owned_file(
        R8_INSTALLED_APP / "Contents/MacOS/gamja-artifact",
        "reused artifact changed", executable=True)
    status = artifact.stat()
    current = (status.st_dev, status.st_ino, status.st_uid,
               stat.S_IMODE(status.st_mode), sha256(artifact))
    if current != expected:
        raise RuntimeError("reused artifact changed")


def stage_artifact_binary(source, expected_sha256):
    source = _real_owned_file(source, "artifact staging source invalid", executable=True)
    if sha256(source) != expected_sha256:
        raise RuntimeError("artifact staging hash mismatch")
    parent = _real_owned_directory(TAURI / "binaries", "artifact staging directory invalid")
    destination = parent / f"gamja-artifact-{ARTIFACT_TARGET}"
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("artifact staging collision")
    temporary = parent / (".gamja-artifact-stage-" + secrets.token_hex(12))
    created_identity = None
    completed = False
    try:
        with source.open("rb") as incoming:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
            with os.fdopen(fd, "wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        if sha256(temporary) != expected_sha256:
            raise RuntimeError("artifact staging copy drift")
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            raise RuntimeError("artifact staging collision") from None
        linked = destination.lstat()
        created_identity = (linked.st_dev, linked.st_ino)
        temporary.unlink()
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        staged = _real_owned_file(destination, "artifact staged file invalid", executable=True)
        if staged.stat().st_ino == source.stat().st_ino or sha256(staged) != expected_sha256:
            raise RuntimeError("artifact staging copy drift")
        completed = True
        return staged, created_identity
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()

        if not completed and created_identity is not None:
            try:
                current = destination.lstat()
            except FileNotFoundError:
                current = None
            if current is not None and (current.st_dev, current.st_ino) == created_identity:
                destination.unlink()


def remove_staged_artifact(staged, expected_sha256, expected_identity):
    expected = TAURI / "binaries" / f"gamja-artifact-{ARTIFACT_TARGET}"
    staged = Path(staged)
    status = _real_owned_file(
        staged, "refusing to remove unowned staged artifact", executable=True
    ).stat()
    if (staged != expected or (status.st_dev, status.st_ino) != tuple(expected_identity)
            or sha256(staged) != expected_sha256):
        raise RuntimeError("refusing to remove unowned staged artifact")
    staged.unlink()
    descriptor = os.open(staged.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def cleanup_release_staging(*, staged_artifact=None, artifact_sha256=None,
                            artifact_identity=None, staged_core=None):
    """Attempt both owned cleanups and report every failure after both attempts."""
    errors = []
    if staged_artifact is not None:
        try:
            remove_staged_artifact(staged_artifact, artifact_sha256, artifact_identity)
        except BaseException as error:
            errors.append(("artifact", error))
    if staged_core is not None:
        try:
            remove_staged_core(staged_core)
        except BaseException as error:
            errors.append(("core", error))
    if errors:
        detail = ", ".join(f"{name}: {error}" for name, error in errors)
        raise RuntimeError("release staging cleanup failed: " + detail) from errors[0][1]


def verify_diagnostics_help(app):
    source = _real_owned_file(DIAGNOSTICS_HELP, "diagnostics help source invalid")
    embedded = _real_owned_file(
        Path(app) / "Contents/Resources/Diagnostics Help.txt",
        "diagnostics help missing or unsafe",
    )
    if embedded.read_bytes() != source.read_bytes():
        raise RuntimeError("diagnostics help drift")
    return sha256(embedded)


def verified_runtime_verifier_binding(core_package, runtime_verifier, expected_sha256):
    """Bind the operator-built verifier beside the selected nonhistorical Core."""
    if runtime_verifier is None or expected_sha256 is None:
        raise RuntimeError("runtime verifier binding is required")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise RuntimeError("runtime verifier hash invalid")
    package = Path(core_package).absolute()
    supplied = Path(runtime_verifier)
    expected = package.with_name(package.name + ".runtime-verifier")
    if not supplied.is_absolute() or supplied != expected:
        raise RuntimeError("runtime verifier path invalid")
    verifier = _real_owned_file(supplied, "runtime verifier file invalid", executable=True)
    actual_sha256 = sha256(verifier)
    if actual_sha256 != expected_sha256:
        raise RuntimeError("runtime verifier hash mismatch")
    return {"path": str(verifier), "sha256": actual_sha256}


def candidate_receipt_payload(
    app, core_package, artifact_sha256, build_input_freeze, *,
    runtime_verifier=None, runtime_verifier_sha256=None,
):
    """Bind the complete unsigned candidate using public facts only."""
    app = Path(app).absolute()
    root_status = _real_owned_directory(app, "candidate app invalid").stat()
    info = _real_owned_file(app / "Contents/Info.plist", "candidate metadata invalid")
    try:
        metadata = plistlib.loads(info.read_bytes())
    except plistlib.InvalidFileException:
        raise RuntimeError("candidate metadata invalid") from None
    if (metadata.get("CFBundleIdentifier") != "top.gamja.bot"
            or metadata.get("CFBundleExecutable") != "gamja-bot"):
        raise RuntimeError("candidate metadata invalid")
    paths = {
        "main": app / "Contents/MacOS/gamja-bot",
        "manifest": app / "Contents/Resources/gamja-core/core-manifest.json",
        "core": app / "Contents/Resources/gamja-core/bin/gamja",
        "engine": app / "Contents/Resources/gamja-core/libexec/gamja/gamja-engine",
        "helper": app / "Contents/Resources/gamja-core/libexec/gamja/gamja-platform-helper",
        "artifact": app / "Contents/MacOS/gamja-artifact",
    }
    for role, path in paths.items():
        _real_owned_file(path, "candidate payload invalid", executable=role != "manifest")
    hashes = {role: sha256(path) for role, path in paths.items()}
    binding = verified_core_binding(core_package)
    expected = {
        "manifest": binding["manifest"],
        "core": binding["core"],
        "engine": binding["engine"],
        "helper": binding["helper"],
        "artifact": artifact_sha256,
    }
    if any(hashes[role] != digest for role, digest in expected.items()):
        raise RuntimeError("candidate payload hash mismatch")
    verify_artifact_binary(paths["artifact"], artifact_sha256)
    verify_diagnostics_help(app)
    freeze = _real_owned_file(build_input_freeze, "build input freeze missing or unsafe")
    if stat.S_IMODE(freeze.stat().st_mode) != 0o600:
        raise RuntimeError("build input freeze mode invalid")
    fixed = {
        "manifest": FIXED_BOOTSTRAP_MANIFEST_SHA256, "core": FIXED_BOOTSTRAP_CORE_SHA256,
        "engine": FIXED_BOOTSTRAP_ENGINE_SHA256, "helper": FIXED_BOOTSTRAP_HELPER_SHA256,
    }
    historical = binding == fixed
    if historical:
        if runtime_verifier is not None or runtime_verifier_sha256 is not None:
            raise RuntimeError("runtime verifier binding is not allowed for historical candidate")
    else:
        binding = dict(binding)
        binding["runtime_verifier"] = verified_runtime_verifier_binding(
            core_package, runtime_verifier, runtime_verifier_sha256,
        )
    build = {
        "target": ARTIFACT_TARGET,
        "cargo_lock_sha256": sha256(ARTIFACT_ROOT / "Cargo.lock"),
        "cargo_locked": True,
        "cargo_jobs": 2,
        "recipe_sha256": sha256(Path(__file__)),
        "tauri_config_sha256": sha256(TAURI / "tauri.core.conf.json"),
        "core_package": str(Path(core_package).absolute()),
    }
    if historical:
        build["source_base"] = SOURCE_BASE
    else:
        # The caller-supplied freeze is an artifact input whose bytes are bound
        # below. It is not parsed here and must not be represented as a clean
        # Git/source authority for a dirty worktree candidate.
        build["recipe_lineage_base"] = SOURCE_BASE
        build["source_freeze_validation"] = "hash_only"
    return {
        "version": 1,
        "kind": ("unsigned-screenshot-artifact-gui-candidate"
                 if historical else "unsigned-gamja-gui-candidate"),
        "status": "ready-for-stable-outer-signing",
        "candidate": str(app),
        "identity": [root_status.st_dev, root_status.st_ino, root_status.st_uid,
                     stat.S_IMODE(root_status.st_mode)],
        "bundle": {"identifier": "top.gamja.bot", "executable": "gamja-bot"},
        "tree_sha256": tree_sha256(app),
        "hashes": hashes,
        "core_binding": binding,
        "artifact_signature": {
            "strict": True, "adhoc": True, "identifier": ARTIFACT_IDENTIFIER,
        },
        "build_input_freeze_sha256": sha256(freeze),
        "build": build,
    }


def emit_candidate_receipt(
    output, app, core_package, artifact_sha256, build_input_freeze, *,
    runtime_verifier=None, runtime_verifier_sha256=None,
):
    """Create one durable private receipt without replacing any existing path."""
    output = Path(output).absolute()
    parent = _real_owned_directory(output.parent, "candidate receipt directory invalid")
    if output.exists() or output.is_symlink():
        raise RuntimeError("candidate receipt collision")
    payload = candidate_receipt_payload(
        app, core_package, artifact_sha256, build_input_freeze,
        runtime_verifier=runtime_verifier,
        runtime_verifier_sha256=runtime_verifier_sha256,
    )
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        with stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            status = output.lstat()
        except FileNotFoundError:
            status = None
        if status is not None and stat.S_ISREG(status.st_mode) and status.st_nlink == 1:
            output.unlink()
        raise
    if stat.S_IMODE(output.stat().st_mode) != 0o600:
        raise RuntimeError("candidate receipt mode invalid")
    return payload


def verify_fixed_bootstrap_package(package):
    """Require the one reviewed package accepted by the fixed runtime resolver."""
    from build_core import verify_package
    package = Path(package)
    manifest = verify_package(package, "aarch64-apple-darwin")
    expected = {
        "core-manifest.json": FIXED_BOOTSTRAP_MANIFEST_SHA256,
        "bin/gamja": FIXED_BOOTSTRAP_CORE_SHA256,
        "libexec/gamja/gamja-engine": FIXED_BOOTSTRAP_ENGINE_SHA256,
    }
    if FIXED_BOOTSTRAP_HELPER_SHA256 is not None:
        expected["libexec/gamja/gamja-platform-helper"] = FIXED_BOOTSTRAP_HELPER_SHA256
    for relative, digest in expected.items():
        if sha256(package / relative) != digest:
            raise RuntimeError(f"fixed bootstrap hash mismatch: {relative}")
    if manifest["files"]["bin/gamja"]["sha256"] != FIXED_BOOTSTRAP_CORE_SHA256:
        raise RuntimeError("fixed bootstrap Core manifest pin mismatch")
    if manifest["files"]["libexec/gamja/gamja-engine"]["sha256"] != FIXED_BOOTSTRAP_ENGINE_SHA256:
        raise RuntimeError("fixed bootstrap engine manifest pin mismatch")
    return manifest


def verified_core_binding(package):
    """Return exact public artifact inputs for one already verified ARM Core."""
    from build_core import verify_package
    package = Path(package)
    manifest = verify_package(package, "aarch64-apple-darwin")
    binding = {
        "manifest": sha256(package / "core-manifest.json"),
        "core": sha256(package / "bin/gamja"),
        "engine": sha256(package / "libexec/gamja/gamja-engine"),
        "helper": (sha256(package / "libexec/gamja/gamja-platform-helper")
                   if (package / "libexec/gamja/gamja-platform-helper").is_file() else None),
    }
    if (manifest["files"]["bin/gamja"]["sha256"] != binding["core"]
            or manifest["files"]["libexec/gamja/gamja-engine"]["sha256"] != binding["engine"]):
        raise RuntimeError("verified Core manifest binding mismatch")
    if binding["helper"] is not None:
        entry = manifest["files"].get("libexec/gamja/gamja-platform-helper")
        if not isinstance(entry, dict) or entry.get("sha256") != binding["helper"]:
            raise RuntimeError("verified Core helper binding mismatch")
    return binding


def verify_bound_core_package(package, binding):
    actual = verified_core_binding(package)
    if actual != binding:
        raise RuntimeError("verified Core binding changed")
    return actual


def npm_executable():
    """Resolve the native npm shim explicitly for subprocess without a shell."""
    name = "npm.cmd" if platform.system() == "Windows" else "npm"
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"{name} is not installed on this build host")
    return executable


def restore_embedded_core(source, app, bundle, target):
    """Replace only the build-owned app resource with the verified original.

    Tauri's generic resource copier dereferences/drops framework links. Never
    bless that transformed payload: use the canonical symlink-preserving copier.
    Stable outer signing is left to the reviewed controller after receipt binding.
    """
    from build_core import stage_verified_package, verify_package
    source, app, bundle = Path(source), Path(app), Path(bundle)
    if (target != "aarch64-apple-darwin" or not bundle.is_absolute()
            or bundle.parts[-3:] != (target, "release", "bundle")
            or app.parent != bundle / "macos" or not app.name.endswith(".app")
            or app.name == ".app" or bundle.resolve() != bundle):
        raise ValueError("core restoration requires an owned build app path")
    for directory in [bundle, bundle / "macos", app, app / "Contents", app / "Contents/Resources"]:
        if (directory.is_symlink() or not directory.is_dir()
                or (os.name == "posix" and directory.stat().st_uid != os.geteuid())):
            raise ValueError("core restoration requires an owned build directory")
    destination = app / "Contents/Resources/gamja-core"
    if (destination.is_symlink() or not destination.is_dir()
            or (os.name == "posix" and destination.stat().st_uid != os.geteuid())):
        raise ValueError("core restoration requires a real resource destination")
    manifest = verify_package(source, target)
    if source.resolve().is_relative_to(app) or app.is_relative_to(source.resolve()):
        raise ValueError("core restoration requires separate source and build paths")
    payload_bytes = sum(item.get("bytes", 0) for item in manifest["files"].values())
    if shutil.disk_usage(bundle).free - payload_bytes < 2 * 1024**3:
        raise ValueError("insufficient disk for verified core restoration")
    # Atomic replacement keeps the prior resource until copy/hash verification
    # succeeds, then cleans its task-owned temporary backup on context exit.
    stage_verified_package(source, destination, target)
    return destination


def remove_staged_core(staged):
    """Remove only this recipe's verified intermediate after Tauri bundling."""
    staged = Path(staged)
    expected = TAURI / "resources/gamja-core"
    if (staged != expected or staged.is_symlink() or not staged.is_dir()
            or (os.name == "posix" and staged.stat().st_uid != os.geteuid())):
        raise RuntimeError("refusing to remove an unowned staged core")
    shutil.rmtree(staged)


def separate_core_source(source, staged, *build_outputs):
    """Resolve and preserve the canonical prefix outside mutated build paths."""
    try:
        source = Path(source).resolve(strict=True)
        build_paths = [Path(path).resolve() for path in (staged, *build_outputs)]
        overlap = False
        for build_path in build_paths:
            source_is_ancestor = any(
                candidate.exists() and os.path.samefile(source, candidate)
                for candidate in (build_path, *build_path.parents)
            )
            build_is_ancestor = build_path.exists() and any(
                os.path.samefile(build_path, candidate)
                for candidate in (source, *source.parents)
            )
            if (source_is_ancestor or build_is_ancestor or source == build_path
                    or source.is_relative_to(build_path) or build_path.is_relative_to(source)):
                overlap = True
                break
    except (OSError, RuntimeError):
        raise RuntimeError("cannot resolve separate canonical core and staging/build paths") from None
    if overlap:
        raise RuntimeError("canonical core and staging/build paths overlap")
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sidecar-only", action="store_true", help="stage frozen sidecar and browser for smoke tests")
    parser.add_argument("--core-package", type=Path, help="override the required verified standalone core for Mac ARM")
    parser.add_argument("--artifact-release-app-only", action="store_true",
                        help="build the one unsigned GUI candidate; package only after stable signing")
    parser.add_argument("--artifact-release-gui-fix-r2", action="store_true",
                        help="reuse the exact installed signed artifact leaf for the fixed r2 GUI")
    parser.add_argument("--artifact-release-gui-refresh-r3", action="store_true",
                        help="reuse the exact installed r2 artifact leaf for the reviewed r3 GUI")
    parser.add_argument("--artifact-release-gui-refresh-r4", action="store_true",
                        help="reuse the exact installed r3 artifact leaf for the reviewed r4 GUI")
    parser.add_argument("--artifact-release-gui-refresh-r5", action="store_true",
                        help="reuse the exact installed r4 artifact leaf for the reviewed r5 GUI")
    parser.add_argument("--artifact-release-gui-refresh-r6", action="store_true",
                        help="reuse the exact installed r5 artifact leaf for the reviewed r6 GUI")
    parser.add_argument("--artifact-release-gui-refresh-r7", action="store_true",
                        help="reuse the exact installed r6 artifact leaf for the reviewed r7 GUI")
    parser.add_argument("--artifact-release-gui-refresh-r8", action="store_true",
                        help="reuse the exact installed r7 artifact leaf for the reviewed r8 GUI")
    parser.add_argument("--candidate-receipt", type=Path, default=CANDIDATE_RECEIPT,
                        help="exclusive private receipt for the unsigned artifact GUI candidate")
    parser.add_argument("--build-input-freeze", type=Path, default=BUILD_INPUT_FREEZE,
                        help="immutable reviewed build-input freeze bound into the candidate receipt")
    parser.add_argument("--runtime-verifier", type=Path,
                        help="required operator-built sibling verifier for a nonhistorical app candidate")
    parser.add_argument("--runtime-verifier-sha256",
                        help="required exact SHA-256 for --runtime-verifier")
    args = parser.parse_args()
    if args.artifact_release_gui_fix_r2:
        if not args.artifact_release_app_only:
            parser.error("--artifact-release-gui-fix-r2 requires --artifact-release-app-only")
        if args.candidate_receipt == CANDIDATE_RECEIPT:
            args.candidate_receipt = R2_CANDIDATE_RECEIPT
        if args.build_input_freeze == BUILD_INPUT_FREEZE:
            args.build_input_freeze = R2_BUILD_INPUT_FREEZE
    if args.artifact_release_gui_refresh_r3:
        if not args.artifact_release_app_only:
            parser.error("--artifact-release-gui-refresh-r3 requires --artifact-release-app-only")
        if args.artifact_release_gui_fix_r2:
            parser.error("choose exactly one GUI release generation")
        if args.candidate_receipt == CANDIDATE_RECEIPT:
            args.candidate_receipt = R3_CANDIDATE_RECEIPT
        if args.build_input_freeze == BUILD_INPUT_FREEZE:
            args.build_input_freeze = R3_BUILD_INPUT_FREEZE
    if args.artifact_release_gui_refresh_r4:
        if not args.artifact_release_app_only:
            parser.error("--artifact-release-gui-refresh-r4 requires --artifact-release-app-only")
        if args.artifact_release_gui_fix_r2 or args.artifact_release_gui_refresh_r3:
            parser.error("choose exactly one GUI release generation")
        if args.candidate_receipt == CANDIDATE_RECEIPT:
            args.candidate_receipt = R4_CANDIDATE_RECEIPT
        if args.build_input_freeze == BUILD_INPUT_FREEZE:
            args.build_input_freeze = R4_BUILD_INPUT_FREEZE
    if args.artifact_release_gui_refresh_r5:
        if not args.artifact_release_app_only:
            parser.error("--artifact-release-gui-refresh-r5 requires --artifact-release-app-only")
        if (args.artifact_release_gui_fix_r2 or args.artifact_release_gui_refresh_r3
                or args.artifact_release_gui_refresh_r4):
            parser.error("choose exactly one GUI release generation")
        if args.candidate_receipt == CANDIDATE_RECEIPT:
            args.candidate_receipt = R5_CANDIDATE_RECEIPT
        if args.build_input_freeze == BUILD_INPUT_FREEZE:
            args.build_input_freeze = R5_BUILD_INPUT_FREEZE
    if args.artifact_release_gui_refresh_r6:
        if not args.artifact_release_app_only:
            parser.error("--artifact-release-gui-refresh-r6 requires --artifact-release-app-only")
        if (args.artifact_release_gui_fix_r2 or args.artifact_release_gui_refresh_r3
                or args.artifact_release_gui_refresh_r4 or args.artifact_release_gui_refresh_r5):
            parser.error("choose exactly one GUI release generation")
        if args.candidate_receipt == CANDIDATE_RECEIPT:
            args.candidate_receipt = R6_CANDIDATE_RECEIPT
        if args.build_input_freeze == BUILD_INPUT_FREEZE:
            args.build_input_freeze = R6_BUILD_INPUT_FREEZE
    if args.artifact_release_gui_refresh_r7:
        if not args.artifact_release_app_only:
            parser.error("--artifact-release-gui-refresh-r7 requires --artifact-release-app-only")
        if (args.artifact_release_gui_fix_r2 or args.artifact_release_gui_refresh_r3
                or args.artifact_release_gui_refresh_r4 or args.artifact_release_gui_refresh_r5
                or args.artifact_release_gui_refresh_r6):
            parser.error("choose exactly one GUI release generation")
        if args.candidate_receipt == CANDIDATE_RECEIPT:
            args.candidate_receipt = R7_CANDIDATE_RECEIPT
        if args.build_input_freeze == BUILD_INPUT_FREEZE:
            args.build_input_freeze = R7_BUILD_INPUT_FREEZE
    if args.artifact_release_gui_refresh_r8:
        if not args.artifact_release_app_only:
            parser.error("--artifact-release-gui-refresh-r8 requires --artifact-release-app-only")
        if (args.artifact_release_gui_fix_r2 or args.artifact_release_gui_refresh_r3
                or args.artifact_release_gui_refresh_r4 or args.artifact_release_gui_refresh_r5
                or args.artifact_release_gui_refresh_r6 or args.artifact_release_gui_refresh_r7):
            parser.error("choose exactly one GUI release generation")
        if args.candidate_receipt == CANDIDATE_RECEIPT:
            args.candidate_receipt = R8_CANDIDATE_RECEIPT
        if args.build_input_freeze == BUILD_INPUT_FREEZE:
            args.build_input_freeze = R8_BUILD_INPUT_FREEZE
    try:
        triple, bundle = TARGETS[(platform.system(), platform.machine())]
    except KeyError:
        parser.error(f"unsupported native host: {platform.system()} {platform.machine()}")
    if args.core_package and triple != "aarch64-apple-darwin":
        parser.error("standalone core GUI ownership is limited to Apple Silicon; Intel/Windows retain legacy defaults")
    if (triple == "aarch64-apple-darwin" and not args.sidecar_only
            and not args.artifact_release_app_only):
        parser.error("Apple Silicon release builds require --artifact-release-app-only; package only after stable signing")
    if triple == "aarch64-apple-darwin":
        core_package = args.core_package or DEFAULT_ARM_CORE_PACKAGE
        if not core_package.is_dir():
            raise RuntimeError(f"missing verified canonical core package: {core_package}")
        from build_core import stage_verified_package, verify_package
        staged = TAURI / "resources/gamja-core"
        build_outputs = []
        if not args.sidecar_only:
            if not os.environ.get("CARGO_TARGET_DIR"):
                parser.error("set CARGO_TARGET_DIR to the existing shared Cargo target")
            config = json.loads((TAURI / "tauri.conf.json").read_text())
            target_dir = Path(os.environ["CARGO_TARGET_DIR"]) / triple / "release/bundle"
            app = target_dir / "macos" / f"{config['productName']}.app"
            build_outputs = [ROOT / "frontend/dist", app]
        core_package = separate_core_source(core_package, staged, *build_outputs)
        historical_mode = any((
            args.artifact_release_gui_fix_r2, args.artifact_release_gui_refresh_r3,
            args.artifact_release_gui_refresh_r4, args.artifact_release_gui_refresh_r5,
            args.artifact_release_gui_refresh_r6, args.artifact_release_gui_refresh_r7,
            args.artifact_release_gui_refresh_r8,
        ))
        verifier_option_present = (
            args.runtime_verifier is not None or args.runtime_verifier_sha256 is not None
        )
        if (args.runtime_verifier is None) != (args.runtime_verifier_sha256 is None):
            parser.error("--runtime-verifier and --runtime-verifier-sha256 are required together")
        verifier_required = (
            args.artifact_release_app_only and not historical_mode and args.core_package is not None
        )
        if verifier_required and not verifier_option_present:
            parser.error("nonhistorical app candidates require a runtime verifier binding")
        if not verifier_required and verifier_option_present:
            parser.error("runtime verifier binding is limited to nonhistorical app candidates")
        if historical_mode or args.core_package is None:
            verify_fixed_bootstrap_package(core_package)
            core_binding = {
                "manifest": FIXED_BOOTSTRAP_MANIFEST_SHA256,
                "core": FIXED_BOOTSTRAP_CORE_SHA256,
                "engine": FIXED_BOOTSTRAP_ENGINE_SHA256,
                "helper": FIXED_BOOTSTRAP_HELPER_SHA256,
            }
        else:
            core_binding = verified_core_binding(core_package)
        stage_verified_package(core_package, staged, triple)
        if args.sidecar_only:
            verify_bound_core_package(staged, core_binding)
            print(f"embedded exact verified core package: {staged}")
            return
        staged_artifact = None
        artifact_identity = None
        try:
            verify_bound_core_package(staged, core_binding)
            print(f"embedded exact verified core package: {staged}")
            runtime_relatives = [
                "core-manifest.json", "bin/gamja", "libexec/gamja/gamja-engine",
            ]
            if core_binding["helper"] is not None:
                runtime_relatives.append("libexec/gamja/gamja-platform-helper")
            runtime_before = {
                relative: (path.stat().st_dev, path.stat().st_ino, sha256(path))
                for relative in runtime_relatives
                for path in [core_package / relative]
            }
            artifact_binary, artifact_sha256, reused_artifact_binding = select_release_artifact(
                Path(os.environ["CARGO_TARGET_DIR"]),
                reuse_installed_r2=args.artifact_release_gui_fix_r2,
                reuse_installed_r3=args.artifact_release_gui_refresh_r3,
                reuse_installed_r4=args.artifact_release_gui_refresh_r4,
                reuse_installed_r5=args.artifact_release_gui_refresh_r5,
                reuse_installed_r6=args.artifact_release_gui_refresh_r6,
                reuse_installed_r7=args.artifact_release_gui_refresh_r7,
                reuse_installed_r8=args.artifact_release_gui_refresh_r8,
            )
            staged_artifact, artifact_identity = stage_artifact_binary(
                artifact_binary, artifact_sha256
            )
            if reused_artifact_binding is not None:
                (require_reused_r8_artifact_unchanged
                 if args.artifact_release_gui_refresh_r8
                 else require_reused_r7_artifact_unchanged
                 if args.artifact_release_gui_refresh_r7
                 else require_reused_r6_artifact_unchanged
                 if args.artifact_release_gui_refresh_r6
                 else require_reused_r5_artifact_unchanged
                 if args.artifact_release_gui_refresh_r5
                 else require_reused_r4_artifact_unchanged
                 if args.artifact_release_gui_refresh_r4
                 else require_reused_r3_artifact_unchanged
                 if args.artifact_release_gui_refresh_r3
                 else require_reused_r2_artifact_unchanged)(reused_artifact_binding)
            subprocess.run([npm_executable(), "run", "tauri", "--", "build", "--target", triple,
                            "--bundles", "app", "--config", "src-tauri/tauri.core.conf.json"],
                           cwd=ROOT / "frontend", check=True,
                           env={**os.environ, "GAMJA_ARTIFACT_SHA256": artifact_sha256,
                                "GAMJA_CORE_MANIFEST_SHA256": core_binding["manifest"],
                                "GAMJA_CORE_SHA256": core_binding["core"],
                                "GAMJA_ENGINE_SHA256": core_binding["engine"]})
            if reused_artifact_binding is not None:
                (require_reused_r8_artifact_unchanged
                 if args.artifact_release_gui_refresh_r8
                 else require_reused_r7_artifact_unchanged
                 if args.artifact_release_gui_refresh_r7
                 else require_reused_r6_artifact_unchanged
                 if args.artifact_release_gui_refresh_r6
                 else require_reused_r5_artifact_unchanged
                 if args.artifact_release_gui_refresh_r5
                 else require_reused_r4_artifact_unchanged
                 if args.artifact_release_gui_refresh_r4
                 else require_reused_r3_artifact_unchanged
                 if args.artifact_release_gui_refresh_r3
                 else require_reused_r2_artifact_unchanged)(reused_artifact_binding)
        finally:
            cleanup_release_staging(
                staged_artifact=staged_artifact,
                artifact_sha256=artifact_sha256 if staged_artifact is not None else None,
                artifact_identity=artifact_identity,
                staged_core=staged,
            )
        embedded = restore_embedded_core(core_package, app, target_dir, triple)
        verify_bound_core_package(embedded, core_binding)
        verify_artifact_binary(app / "Contents/MacOS/gamja-artifact", artifact_sha256)
        verify_diagnostics_help(app)
        if (embedded / "core-manifest.json").read_bytes() != (core_package / "core-manifest.json").read_bytes():
            raise RuntimeError("desktop signing changed the canonical core manifest")
        runtime_after = {
            relative: (path.stat().st_dev, path.stat().st_ino, sha256(path))
            for relative in runtime_before
            for path in [core_package / relative]
        }
        if runtime_after != runtime_before:
            raise RuntimeError("fixed runtime source identity changed")
        verifier_receipt_options = ({
            "runtime_verifier": args.runtime_verifier,
            "runtime_verifier_sha256": args.runtime_verifier_sha256,
        } if verifier_required else {})
        emit_candidate_receipt(args.candidate_receipt, app, core_package,
                               artifact_sha256, args.build_input_freeze,
                               **verifier_receipt_options)
        print(f"unsigned artifact GUI candidate ready: {app} artifact SHA-256 {artifact_sha256}")
        return
    if sys.version_info[:2] != (3, 11):
        parser.error("use target-native Python 3.11 and its matching wheels")
    import importlib.metadata as metadata
    for name, required in (("playwright", "1.58.0"), ("pyinstaller", "6.22.3"),
                           ("pyinstaller-hooks-contrib", "2026.7")):
        if metadata.version(name) != required:
            parser.error(f"{name} must be {required}; found {metadata.version(name)}")
    import playwright
    browsers = json.loads((Path(playwright.__file__).parent / "driver" / "package" / "browsers.json").read_text())
    revisions = {item["name"]: item["revision"] for item in browsers["browsers"]}
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or (
        Path.home() / "Library/Caches/ms-playwright" if platform.system() == "Darwin"
        else Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"))
    staged_browsers = TAURI / "resources" / "playwright-browsers"
    payloads = ("chromium", "chromium-headless-shell")
    for name in payloads:
        if not (cache / f"{name.replace('-', '_')}-{revisions[name]}" / "INSTALLATION_COMPLETE").is_file():
            parser.error(f"missing matching {name} revision {revisions[name]} in {cache}; preinstall on build host")
    if staged_browsers.exists():
        shutil.rmtree(staged_browsers)
    staged_browsers.mkdir(parents=True)
    for name in payloads:
        source = cache / f"{name.replace('-', '_')}-{revisions[name]}"
        shutil.copytree(source, staged_browsers / source.name, symlinks=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "backend.spec"],
                   cwd=ROOT / "backend", check=True)
    suffix = ".exe" if platform.system() == "Windows" else ""
    source = ROOT / "backend" / "dist" / f"backend{suffix}"
    if not source.is_file():
        raise RuntimeError(f"PyInstaller did not produce {source}")
    staged_sidecar = TAURI / "binaries" / f"backend-{triple}{suffix}"
    staged_sidecar.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, staged_sidecar)
    print(f"staged {staged_sidecar} and {', '.join(payloads)} revision {revisions['chromium']}")
    if not args.sidecar_only:
        if not os.environ.get("CARGO_TARGET_DIR"):
            parser.error("set CARGO_TARGET_DIR to the existing shared Cargo target")
        npm = npm_executable()
        if bundle == "dmg":
            subprocess.run([npm, "run", "tauri", "--", "build", "--target", triple, "--bundles", "app"],
                           cwd=ROOT / "frontend", check=True)
            config = json.loads((TAURI / "tauri.conf.json").read_text())
            target_dir = Path(os.environ["CARGO_TARGET_DIR"]) / triple / "release" / "bundle"
            app = target_dir / "macos" / f"{config['productName']}.app"
            output = target_dir / "dmg" / f"{config['productName']}_{config['version']}_{triple.split('-')[0]}.dmg"
            subprocess.run(["bash", str(ROOT / "scripts" / "package_macos_dmg.sh"), str(app), str(output)], check=True)
        else:
            subprocess.run([npm, "run", "tauri", "--", "build", "--target", triple, "--bundles", bundle],
                           cwd=ROOT / "frontend", check=True)


if __name__ == "__main__":
    main()
