"""Real tiny prefixes pressure-test archive integrity and reversible CLI delivery."""
import hashlib
from contextlib import contextmanager
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import build_core


@pytest.fixture
def op():
    spec = importlib.util.spec_from_file_location("deliver_core", ROOT / "scripts/deliver_core.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def package(path, payload):
    (path / "bin").mkdir(parents=True)
    (path / "libexec/gamja").mkdir(parents=True)
    for name in ("bin/gamja", "libexec/gamja/gamja-engine", "libexec/gamja/gamja-platform-helper"):
        (path / name).write_bytes(payload)
        (path / name).chmod(0o755)
    (path / "THIRD-PARTY-NOTICES.txt").write_text("fixture notices")
    build_core.write_manifest(path, target="aarch64-apple-darwin", version="1.0.0", provenance={})
    return build_core.sha256(path / "core-manifest.json")


def test_launcher_execs_release_target_with_exact_arguments(op, tmp_path):
    home = tmp_path / "owner home"
    target = home / ".local/share/gamja bot/core/releases/reviewed/bin/gamja"
    target.parent.mkdir(parents=True)
    target.write_text('#!/bin/sh\nprintf "<%s>\\n" "$@"\n')
    target.chmod(0o755)
    launcher = home / ".local/bin/gamja"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(op.launcher_bytes(target, home))
    launcher.chmod(0o755)

    result = subprocess.run(
        [str(launcher), "", "two words", "quote'\"$HOME"],
        env={"HOME": "/hostile-home", "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=5, check=True)

    assert result.stdout.splitlines() == ["<>", "<two words>", "<quote'\"$HOME>"]
    assert result.stderr == ""


def test_launcher_rejects_target_outside_physical_home(op, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside/gamja"
    outside.parent.mkdir(); outside.write_bytes(b"x")
    with pytest.raises(ValueError, match="cli_target_invalid"):
        op.launcher_bytes(outside, home)


def test_restore_command_completes_short_regular_file_writes(op, tmp_path, monkeypatch):
    destination = tmp_path / "gamja"
    content = b"#!/bin/sh\nexec '/fixed/core' \"$@\"\n"
    real_write = os.write

    def short_write(descriptor, data):
        return real_write(descriptor, data[:1])

    monkeypatch.setattr(op.os, "write", short_write)
    op.restore_command(destination, (None, "launcher", content))

    assert destination.read_bytes() == content
    assert stat.S_IMODE(destination.stat().st_mode) == 0o755


@pytest.fixture
def layout(tmp_path, monkeypatch):
    monkeypatch.setattr(build_core, "verify_macos_signature", lambda *_: None)
    source = tmp_path / "gamja-core"
    new_hash = package(source, b"new reviewed bytes")
    releases = tmp_path / "core/releases"
    releases.mkdir(parents=True)
    old = releases / "d849a9d6826c3f92"
    old_hash = package(old, b"old rollback bytes")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    link = bindir / "gamja"
    link.symlink_to(old / "bin/gamja")
    return dict(source=source, core_root=releases.parent, link=link, anchor=tmp_path,
                new_hash=new_hash, old_hash=old_hash, old_name=old.name)


def test_archive_roundtrip_modes_and_no_extract(op, layout, tmp_path, monkeypatch):
    monkeypatch.setattr(op, "verify_native", lambda _: None)  # tiny unsigned fixture only
    out = tmp_path / "core.tar.gz"
    receipt = tmp_path / "archive.json"
    op.archive_prefix(layout["source"], out, receipt, layout["new_hash"], anchor=tmp_path)
    with tarfile.open(out) as archive:
        assert archive.extractfile("gamja-core/bin/gamja").read() == b"new reviewed bytes"
        assert archive.getmember("gamja-core/bin/gamja").mode == 0o755
    assert json.loads(receipt.read_text())["status"] == "passed"
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "extracted").exists()


def test_archive_cap_removes_only_own_partial(op, layout, tmp_path, monkeypatch):
    monkeypatch.setattr(op, "verify_native", lambda _: None)
    out = tmp_path / "core.tar.gz"
    with pytest.raises(ValueError, match="archive_limit"):
        op.archive_prefix(layout["source"], out, tmp_path / "receipt.json", layout["new_hash"], anchor=tmp_path, cap=20)
    assert not out.exists()
    assert json.loads((tmp_path / "receipt.json").read_text())["status"] == "failed"
    assert (layout["source"] / "bin/gamja").read_bytes() == b"new reviewed bytes"


def test_archive_existing_output_preserved(op, layout, tmp_path, monkeypatch):
    monkeypatch.setattr(op, "verify_native", lambda _: None)
    out = tmp_path / "core.tar.gz"
    out.write_bytes(b"previous")
    with pytest.raises(FileExistsError):
        op.archive_prefix(layout["source"], out, tmp_path / "receipt.json", layout["new_hash"], anchor=tmp_path)
    assert out.read_bytes() == b"previous"


@pytest.mark.parametrize("bad", ["../escape", "gamja-core/extra", "gamja-core/bin/gamja"])
def test_archive_rejects_traversal_extra_duplicate(op, layout, tmp_path, bad):
    out = tmp_path / "bad.tar.gz"
    with tarfile.open(out, "w:gz") as archive:
        for name in ([bad, bad] if bad.endswith("bin/gamja") else [bad]):
            info = tarfile.TarInfo(name)
            info.size = 3
            archive.addfile(info, io.BytesIO(b"bad"))
    with pytest.raises(ValueError):
        op.verify_archive(out, layout["source"], layout["new_hash"])


def test_update_and_rollback_keep_both_releases(op, layout, tmp_path, monkeypatch):
    monkeypatch.setattr(op, "verify_native", lambda _: None)
    monkeypatch.setattr(op, "no_refs", lambda _: None)
    monkeypatch.setattr(op, "verified", lambda root, _expected:
                        json.loads((root / build_core.MANIFEST).read_text()))
    receipt = tmp_path / "update.json"
    op.update_cli(**layout, receipt=receipt)
    new = layout["core_root"] / "releases" / layout["new_hash"][:16]
    assert layout["link"].is_file() and not layout["link"].is_symlink()
    assert stat.S_IMODE(layout["link"].stat().st_mode) == 0o755
    assert layout["link"].read_bytes() == op.launcher_bytes(
        new / "bin/gamja", layout["anchor"])
    assert (new / "bin/gamja").read_bytes() == b"new reviewed bytes"
    assert (layout["core_root"] / "releases/d849a9d6826c3f92/bin/gamja").read_bytes() == b"old rollback bytes"
    op.rollback_cli(**layout, receipt=tmp_path / "rollback.json")
    assert layout["link"].is_file() and not layout["link"].is_symlink()
    assert layout["link"].read_bytes() == op.launcher_bytes(
        layout["core_root"] / "releases/d849a9d6826c3f92/bin/gamja",
        layout["anchor"])
    assert new.exists()
    assert json.loads(receipt.read_text())["status"] == "passed"


def test_copy_failure_preserves_old_link_and_cleans_new(op, layout, tmp_path, monkeypatch):
    monkeypatch.setattr(op, "verify_native", lambda _: None)
    monkeypatch.setattr(op, "no_refs", lambda _: None)
    def fail(*_):
        raise ValueError("copy_failed")
    monkeypatch.setattr(op, "copy_file", fail)
    before = os.readlink(layout["link"])
    with pytest.raises(ValueError, match="copy_failed"):
        op.update_cli(**layout, receipt=tmp_path / "failed.json")
    assert os.readlink(layout["link"]) == before
    assert not (layout["core_root"] / "releases" / layout["new_hash"][:16]).exists()
    assert not list(layout["core_root"].glob("*.lock"))


def test_changed_link_not_replaced_or_deleted(op, layout, tmp_path, monkeypatch):
    monkeypatch.setattr(op, "verify_native", lambda _: None)
    monkeypatch.setattr(op, "no_refs", lambda _: None)
    real_copy = op.copy_file
    changed = False
    def copy_and_change(a, b):
        nonlocal changed
        result = real_copy(a, b)
        if not changed:
            layout["link"].unlink()
            layout["link"].symlink_to(tmp_path / "user-command")
            changed = True
        return result
    monkeypatch.setattr(op, "copy_file", copy_and_change)
    with pytest.raises(ValueError, match="command_changed"):
        op.update_cli(**layout, receipt=tmp_path / "failed.json")
    assert os.readlink(layout["link"]) == str(tmp_path / "user-command")


def test_signature_failure_before_activation_keeps_rollback(op, layout, tmp_path, monkeypatch):
    monkeypatch.setattr(op, "no_refs", lambda _: None)
    def fail(_):
        raise ValueError("signature_failed")
    monkeypatch.setattr(op, "verify_native", fail)
    before = os.readlink(layout["link"])
    with pytest.raises(ValueError, match="signature_failed"):
        op.update_cli(**layout, receipt=tmp_path / "failed.json")
    assert os.readlink(layout["link"]) == before


def test_receipt_write_failure_rolls_back_activated_link(op, layout, tmp_path, monkeypatch):
    monkeypatch.setattr(op, "verify_native", lambda _: None)
    monkeypatch.setattr(op, "no_refs", lambda _: None)
    before = os.readlink(layout["link"])
    real_write = op.finish_receipt
    def fail_once(fd, value):
        if value["status"] == "passed":
            raise OSError("write_failed")
        real_write(fd, value)
    monkeypatch.setattr(op, "finish_receipt", fail_once)
    with pytest.raises(OSError, match="write_failed"):
        op.update_cli(**layout, receipt=tmp_path / "failed.json")
    assert os.readlink(layout["link"]) == before
    assert json.loads((tmp_path / "failed.json").read_text())["status"] == "failed"


def test_existing_new_release_never_overwritten(op, layout, tmp_path, monkeypatch):
    monkeypatch.setattr(op, "verify_native", lambda _: None)
    monkeypatch.setattr(op, "no_refs", lambda _: None)
    new = layout["core_root"] / "releases" / layout["new_hash"][:16]
    new.mkdir()
    (new / "keep").write_text("user")
    with pytest.raises(FileExistsError):
        op.update_cli(**layout, receipt=tmp_path / "failed.json")
    assert (new / "keep").read_text() == "user"


def test_low_disk_refuses_copy(op, layout, tmp_path, monkeypatch):
    monkeypatch.setattr(op, "verify_native", lambda _: None)
    monkeypatch.setattr(op, "no_refs", lambda _: None)
    monkeypatch.setattr(op, "free_bytes", lambda _: 1)
    before = os.readlink(layout["link"])
    with pytest.raises(ValueError, match="disk_floor"):
        op.update_cli(**layout, receipt=tmp_path / "failed.json")
    assert os.readlink(layout["link"]) == before


def test_cleanup_shields_real_sigint(op):
    previous = signal.getsignal(signal.SIGINT)
    with op.cleanup_shield():
        os.kill(os.getpid(), signal.SIGINT)
    assert signal.getsignal(signal.SIGINT) == previous


def test_postwrite_sigterm_keeps_durable_success_and_cleanup(op, layout, tmp_path, monkeypatch):
    monkeypatch.setattr(op, "verify_native", lambda _: None)
    monkeypatch.setattr(op, "no_refs", lambda _: None)
    real_write = op.finish_receipt
    def write_then_signal(fd, value):
        real_write(fd, value)
        os.kill(os.getpid(), signal.SIGTERM)
    monkeypatch.setattr(op, "finish_receipt", write_then_signal)
    result = op.update_cli(**layout, receipt=tmp_path / "success.json")
    assert result["status"] == json.loads((tmp_path / "success.json").read_text())["status"] == "passed"
    assert not list(layout["core_root"].glob("*.lock"))


def test_signal_after_commit_shield_keeps_status_and_cleans_lock(op, layout, tmp_path, monkeypatch):
    monkeypatch.setattr(op, "verify_native", lambda _: None)
    monkeypatch.setattr(op, "no_refs", lambda _: None)
    real_write, real_shield = op.finish_receipt, op.cleanup_shield
    written = fired = False
    def write(fd, value):
        nonlocal written
        real_write(fd, value)
        written = value["status"] == "passed"
    @contextmanager
    def shield():
        nonlocal fired
        with real_shield():
            yield
        if written and not fired:
            fired = True
            os.kill(os.getpid(), signal.SIGTERM)
    monkeypatch.setattr(op, "finish_receipt", write)
    monkeypatch.setattr(op, "cleanup_shield", shield)
    result = op.update_cli(**layout, receipt=tmp_path / "success.json")
    assert fired
    assert result["status"] == json.loads((tmp_path / "success.json").read_text())["status"] == "passed"
    assert not list(layout["core_root"].glob("*.lock"))


def test_copy_sigint_becomes_failed_receipt_and_owned_cleanup(op, layout, tmp_path, monkeypatch):
    monkeypatch.setattr(op, "verify_native", lambda _: None)
    monkeypatch.setattr(op, "no_refs", lambda _: None)
    def interrupt(*_):
        os.kill(os.getpid(), signal.SIGINT)
    monkeypatch.setattr(op, "copy_file", interrupt)
    before = os.readlink(layout["link"])
    with pytest.raises(InterruptedError):
        op.update_cli(**layout, receipt=tmp_path / "interrupted.json")
    assert os.readlink(layout["link"]) == before
    assert json.loads((tmp_path / "interrupted.json").read_text())["status"] == "failed"
    assert not (layout["core_root"] / "releases" / layout["new_hash"][:16]).exists()


def test_stale_lock_fails_closed_without_unlink(op, layout, tmp_path, monkeypatch):
    lock = layout["core_root"] / (".cli-update-" + layout["new_hash"][:16] + ".lock")
    lock.write_bytes(b"other operator")
    with pytest.raises(FileExistsError):
        op.update_cli(**layout, receipt=tmp_path / "failure.json")
    assert lock.read_bytes() == b"other operator"


def test_symlink_ancestor_rejected_before_receipt(op, layout, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(layout["core_root"], target_is_directory=True)
    layout["core_root"] = alias
    with pytest.raises(ValueError, match="unsafe_owned_ancestor"):
        op.update_cli(**layout, receipt=tmp_path / "failure.json")
    assert not (tmp_path / "failure.json").exists()


def test_archive_preserves_confined_browser_symlink(op, layout, tmp_path, monkeypatch):
    monkeypatch.setattr(op, "verify_native", lambda _: None)
    browsers = layout["source"] / "libexec/gamja/playwright-browsers"
    for name in ("chromium-1208", "chromium_headless_shell-1208"):
        (browsers / name).mkdir(parents=True)
        (browsers / name / "INSTALLATION_COMPLETE").touch()
    (browsers / "chromium-1208/asset").write_bytes(b"browser fixture")
    (browsers / "chromium-1208/alias").symlink_to("asset")
    build_core.write_manifest(layout["source"], target="aarch64-apple-darwin", version="1.0.0", provenance={})
    expected = build_core.sha256(layout["source"] / "core-manifest.json")
    out = tmp_path / "core.tar.gz"
    op.archive_prefix(layout["source"], out, tmp_path / "receipt.json", expected, anchor=tmp_path)
    with tarfile.open(out) as archive:
        alias = archive.getmember("gamja-core/libexec/gamja/playwright-browsers/chromium-1208/alias")
        assert alias.issym() and alias.linkname == "asset"


def test_archive_tampered_bytes_fail_strong_hash(op, layout, tmp_path, monkeypatch):
    monkeypatch.setattr(op, "verify_native", lambda _: None)
    original, corrupt = tmp_path / "original.tar.gz", tmp_path / "corrupt.tar.gz"
    op.archive_prefix(layout["source"], original, tmp_path / "receipt.json", layout["new_hash"], anchor=tmp_path)
    with tarfile.open(original) as src, tarfile.open(corrupt, "w:gz") as dst:
        for member in src:
            data = src.extractfile(member).read() if member.isfile() else None
            if member.name == "gamja-core/bin/gamja":
                data = b"X" * member.size
            dst.addfile(member, io.BytesIO(data) if data is not None else None)
    with pytest.raises(ValueError, match="archive_hash"):
        op.verify_archive(corrupt, layout["source"], layout["new_hash"])


@pytest.mark.parametrize("boundary", ["prefix", "lock", "pending", "receipt"])
def test_creation_signal_registered_before_failure_cleanup_and_retry(op, layout, tmp_path, monkeypatch, boundary):
    monkeypatch.setattr(op, "verify_native", lambda _: None)
    monkeypatch.setattr(op, "no_refs", lambda _: None)
    new = layout["core_root"] / "releases" / layout["new_hash"][:16]
    receipt = tmp_path / "boundary.json"
    real_open, real_mkdir = os.open, Path.mkdir
    real_copy, real_assert, real_launcher = op.copy_file, op.assert_link, op.write_launcher
    fired = later_interruption = False
    captured_fds = []
    def interrupt():
        nonlocal fired
        fired = True
        os.kill(os.getpid(), signal.SIGINT)
    def opened(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if not fired and ((boundary == "lock" and str(path).endswith(".lock")) or
                          (boundary == "receipt" and Path(path) == receipt)):
            captured_fds.append(fd)
            interrupt()
        return fd
    def mkdir(path, *args, **kwargs):
        result = real_mkdir(path, *args, **kwargs)
        if not fired and boundary == "prefix" and path == new:
            interrupt()
        return result
    def launcher(*args, **kwargs):
        result = real_launcher(*args, **kwargs)
        if not fired and boundary == "pending":
            interrupt()
        return result
    def copy(path, destination):
        nonlocal later_interruption
        if fired and boundary != "pending" and not later_interruption:
            later_interruption = True
            os.kill(os.getpid(), signal.SIGINT)  # Long work remains interruptible.
        return real_copy(path, destination)
    def check_link(path, state):
        nonlocal later_interruption
        if fired and boundary == "pending" and not later_interruption:
            later_interruption = True
            os.kill(os.getpid(), signal.SIGINT)
        return real_assert(path, state)
    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(op, "write_launcher", launcher)
    monkeypatch.setattr(op, "copy_file", copy)
    monkeypatch.setattr(op, "assert_link", check_link)
    with pytest.raises(InterruptedError):
        op.update_cli(**layout, receipt=receipt)
    assert fired
    assert later_interruption
    assert json.loads(receipt.read_text())["status"] == "failed"
    assert not new.exists()
    assert not list(layout["core_root"].glob("*.lock"))
    assert not list(layout["link"].parent.glob("*.pending"))
    for fd in captured_fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert os.readlink(layout["link"]) == str(layout["core_root"] / "releases/d849a9d6826c3f92/bin/gamja")
    op.update_cli(**layout, receipt=tmp_path / "retry.json")
    assert json.loads((tmp_path / "retry.json").read_text())["status"] == "passed"


def test_archive_open_signal_bound_and_failed_partial_removed(op, layout, tmp_path, monkeypatch):
    monkeypatch.setattr(op, "verify_native", lambda _: None)
    out = tmp_path / "archive.tar.gz"
    real_open = os.open
    captured = None
    def opened(path, *args, **kwargs):
        nonlocal captured
        fd = real_open(path, *args, **kwargs)
        if Path(path) == out:
            captured = fd
            os.kill(os.getpid(), signal.SIGINT)
        return fd
    monkeypatch.setattr(os, "open", opened)
    with pytest.raises(ValueError, match="archive_limit"):
        op.archive_prefix(layout["source"], out, tmp_path / "failure.json", layout["new_hash"], anchor=tmp_path, cap=20)
    assert not out.exists()
    with pytest.raises(OSError):
        os.fstat(captured)
    monkeypatch.setattr(os, "open", real_open)
    op.archive_prefix(layout["source"], out, tmp_path / "retry.json", layout["new_hash"], anchor=tmp_path)
    assert json.loads((tmp_path / "retry.json").read_text())["status"] == "passed"
