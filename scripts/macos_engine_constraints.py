"""macOS 14 Library Constraint build helpers; imported only by the build recipe."""

import hashlib
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import sys


PYINSTALLER_VERSION = "6.22.3"
LIBRARY_IDENTIFIER = "top.gamja.engine.library"
PLAYWRIGHT_NODE_IDENTIFIER = "node"
PLAYWRIGHT_NODE_TEAM = "HX7739G8FX"
PLAYWRIGHT_NODE_ENTITLEMENTS = {
    "com.apple.security.cs.allow-dyld-environment-variables": True,
    "com.apple.security.cs.allow-jit": True,
    "com.apple.security.cs.allow-unsigned-executable-memory": True,
    "com.apple.security.cs.disable-executable-page-protection": True,
    "com.apple.security.cs.disable-library-validation": True,
    "com.apple.security.get-task-allow": True,
}
DLV_ENTITLEMENT = "com.apple.security.cs.disable-library-validation"
PROCESS_SIGNATURE = (
    "(src_name, dest_name, use_strip=False, use_upx=False, upx_exclude=None, "
    "target_arch=None, codesign_identity=None, entitlements_file=None, "
    "strict_arch_validation=False)"
)
_ACTIVE_SESSION = None


def require(condition, code):
    if not condition:
        raise ValueError(code)


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def is_macho_bytes(value):
    return value[:4] in {
        b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
        b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
    }


def constraint_plist(cdhashes):
    values = sorted(set(cdhashes))
    require(values, "candidate_cdhash_missing")
    require(all(re.fullmatch(r"[0-9a-f]{40}", value) for value in values),
            "candidate_cdhash_invalid")
    return plistlib.dumps(
        {"cdhash": {"$in": [bytes.fromhex(value) for value in values]}},
        fmt=plistlib.FMT_XML,
        sort_keys=True,
    )


def engine_entitlements_plist():
    return plistlib.dumps({DLV_ENTITLEMENT: True}, fmt=plistlib.FMT_XML, sort_keys=True)


def require_macos14_build_version(output):
    blocks = re.findall(r"cmd LC_BUILD_VERSION\n(.*?)(?=Load command|\Z)", output, re.DOTALL)
    require(len(blocks) == 1 and re.search(r"\bplatform 1\b", blocks[0]),
            "macos_build_version_missing")
    match = re.search(r"\bminos (\d+)\.(\d+)(?:\.(\d+))?", blocks[0])
    require(match is not None, "macos_build_version_missing")
    version = tuple(int(value or "0") for value in match.groups())
    require(version >= (14, 0, 0), "macos_minimum_too_old")
    return True


def strongest_sha256_cdhash(details):
    choices = re.findall(r"^Hash choices=(.+)$", details, re.MULTILINE)
    candidates = re.findall(r"^CandidateCDHash sha256=([0-9a-f]{40})$", details, re.MULTILINE)
    full = re.findall(r"^CandidateCDHashFull sha256=([0-9a-f]{64})$", details, re.MULTILINE)
    selected = re.findall(r"^CDHash=([0-9a-f]{40})$", details, re.MULTILINE)
    require(choices == ["sha256"] and len(candidates) == 1 and selected == candidates,
            "sha256_candidate_unverified")
    require(len(full) == 1 and full[0][:40] == candidates[0],
            "sha256_candidate_not_truncated")
    return candidates[0]


def require_final_policy(kind, entitlements, constraint_slots):
    if kind == "engine":
        require(entitlements == {DLV_ENTITLEMENT: True}, "engine_entitlements_not_exact_dlv")
        require(len(constraint_slots) == 1, "engine_library_constraint_missing")
    elif kind == "core":
        require(entitlements == {}, "core_entitlements_forbidden")
        require(not constraint_slots, "core_library_constraint_forbidden")
    else:
        raise ValueError("signature_policy_kind")
    return True


def constraint_slots_from_thin_macho(path):
    data = Path(path).read_bytes()
    require(len(data) >= 32 and data[:4] in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"),
            "constraint_slot_requires_thin_macho")
    endian = "<" if data[:4] == b"\xcf\xfa\xed\xfe" else ">"
    ncmds = struct.unpack_from(endian + "I", data, 16)[0]
    offset = 32
    signatures = []
    for _ in range(ncmds):
        require(offset + 8 <= len(data), "macho_load_command_invalid")
        command, size = struct.unpack_from(endian + "II", data, offset)
        require(size >= 8 and offset + size <= len(data), "macho_load_command_invalid")
        if command == 0x1D:
            data_offset, data_size = struct.unpack_from(endian + "II", data, offset + 8)
            require(data_offset + data_size <= len(data), "code_signature_bounds_invalid")
            signatures.append(data[data_offset:data_offset + data_size])
        offset += size
    require(len(signatures) == 1, "code_signature_command_count")
    signature = signatures[0]
    require(len(signature) >= 12, "code_signature_missing")
    magic, length, count = struct.unpack_from(">III", signature, 0)
    require(magic == 0xFADE0CC0 and length <= len(signature)
            and 12 + count * 8 <= length, "superblob_invalid")
    slots = []
    for index in range(count):
        slot_type, slot_offset = struct.unpack_from(">II", signature, 12 + index * 8)
        if slot_type != 11:
            continue
        require(slot_offset + 8 <= length, "constraint_slot_bounds_invalid")
        _slot_magic, slot_length = struct.unpack_from(">II", signature, slot_offset)
        require(slot_length >= 8 and slot_offset + slot_length <= length,
                "constraint_slot_bounds_invalid")
        slots.append(signature[slot_offset:slot_offset + slot_length])
    return slots


def _owned_directory(path, code):
    path = Path(path)
    status = path.lstat()
    require(stat.S_ISDIR(status.st_mode) and not path.is_symlink()
            and status.st_uid == os.geteuid() and stat.S_IMODE(status.st_mode) == 0o700,
            code)
    return path


def _safe_destination(staging, destination):
    pure = PurePosixPath(destination)
    require(not pure.is_absolute() and destination == pure.as_posix()
            and pure.parts and ".." not in pure.parts and "." not in pure.parts,
            "pyinstaller_destination_unsafe")
    target = staging.joinpath(*pure.parts)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(target.is_relative_to(staging) and not target.exists() and not target.is_symlink(),
            "pyinstaller_signed_copy_collision")
    return target


def _atomic_manifest(path, entries):
    path = Path(path)
    pending = path.with_name(f".{path.name}.{os.getpid()}.pending")
    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        value = (json.dumps({"format": 1, "entries": entries}, sort_keys=True, indent=2) + "\n").encode()
        view = memoryview(value)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(pending, path)


def partition_native_payload(entries):
    binaries, datas = [], []
    for source, destination in entries:
        path = Path(source)
        require(path.is_file() and not path.is_symlink(), "pyinstaller_payload_file")
        with path.open("rb") as handle:
            native = is_macho_bytes(handle.read(4))
        (binaries if native else datas).append((source, destination))
    return binaries, datas


def payload_destinations(entries):
    return {
        (PurePosixPath(destination) / Path(source).name).as_posix()
        for source, destination in entries
    }


def playwright_helper_destinations(entries):
    destinations = payload_destinations(entries)
    require(destinations == {"playwright/driver/node"}, "playwright_native_inventory")
    return destinations


def install_postprocess_hook(api, pyinstaller, staging, manifest, *, helper_destinations,
                             sign_library, inspect_library, inspect_helper):
    require(pyinstaller.__version__ == PYINSTALLER_VERSION, "pyinstaller_version")
    original = api.process_collected_binary
    require(str(inspect.signature(original)) == PROCESS_SIGNATURE,
            "pyinstaller_process_signature")
    staging = Path(staging)
    if not staging.exists():
        staging.mkdir(parents=True, mode=0o700)
    _owned_directory(staging, "pyinstaller_signed_stage")
    manifest = Path(manifest)
    require(manifest.parent == staging.parent and not manifest.exists(),
            "pyinstaller_inventory_path")
    entries = []
    helper_destinations = set(helper_destinations)

    def wrapped(src_name, dest_name, use_strip=False, use_upx=False, upx_exclude=None,
                target_arch=None, codesign_identity=None, entitlements_file=None,
                strict_arch_validation=False):
        helper = dest_name in helper_destinations
        if helper:
            processed = src_name
        else:
            processed = original(
                src_name, dest_name, use_strip=use_strip, use_upx=use_upx,
                upx_exclude=upx_exclude, target_arch=target_arch,
                codesign_identity=codesign_identity, entitlements_file=entitlements_file,
                strict_arch_validation=strict_arch_validation,
            )
        source = Path(processed)
        data = source.read_bytes()
        if not is_macho_bytes(data):
            return processed
        target = _safe_destination(staging, dest_name)
        shutil.copyfile(source, target)
        target.chmod(0o700)
        if not helper:
            sign_library(target)
        signed = target.read_bytes()
        metadata = (inspect_helper if helper else inspect_library)(target)
        arches = metadata.get("arches")
        require(isinstance(arches, dict) and arches
                and all(isinstance(arch, str) and re.fullmatch(r"[0-9a-f]{40}", digest)
                        for arch, digest in arches.items()),
                "signed_macho_metadata")
        entries.append({"destination": dest_name, "kind": "helper" if helper else "library",
                        "sha256": sha256_bytes(signed),
                        "arches": dict(sorted(arches.items()))})
        entries.sort(key=lambda item: item["destination"])
        _atomic_manifest(manifest, entries)
        return str(target)

    api.process_collected_binary = wrapped
    return original, entries


def verify_carchive_native_entries(archive, inventory, extract_root, *, inspect_library,
                                   inspect_helper):
    records = inventory.get("entries")
    require(inventory.get("format", 1) == 1 and isinstance(records, list),
            "archive_native_inventory_format")
    expected = {entry["destination"]: entry for entry in records}
    require(len(expected) == len(records), "archive_native_inventory_duplicate")
    actual = {}
    for name in archive.toc:
        data = archive.extract(name)
        if isinstance(data, bytes) and is_macho_bytes(data):
            actual[name] = data
    require(set(actual) == set(expected), "archive_native_inventory_mismatch")
    extract_root = Path(extract_root)
    extract_root.mkdir(parents=True, mode=0o700)
    _owned_directory(extract_root, "archive_extract_root")
    cdhashes = []
    for name in sorted(actual):
        data = actual[name]
        record = expected[name]
        require(sha256_bytes(data) == record.get("sha256"), "archive_native_bytes_mismatch")
        target_dir = extract_root / hashlib.sha256(name.encode()).hexdigest()
        target_dir.mkdir(mode=0o700)
        target = target_dir / PurePosixPath(name).name
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view):]
        finally:
            os.close(descriptor)
        kind = record.get("kind")
        require(kind in {"library", "helper"}, "archive_native_kind_invalid")
        metadata = (inspect_helper if kind == "helper" else inspect_library)(target)
        require(metadata.get("arches") == record.get("arches"),
                "archive_native_signature_mismatch")
        cdhashes.extend(metadata["arches"].values())
    return sorted(set(cdhashes))


def inspect_signed_library(path):
    # Late import avoids build-script cycles while the spec is loading.
    import build_core
    build_core.verify_macos_signature(path, LIBRARY_IDENTIFIER)
    arches = subprocess.run(["/usr/bin/lipo", "-archs", str(path)], capture_output=True,
                            text=True, check=True).stdout.split()
    require(arches and len(arches) == len(set(arches)), "macho_arch_inventory_invalid")
    values = {}
    for arch in arches:
        details = subprocess.run(
            ["/usr/bin/codesign", "-d", "--verbose=4", "--arch", arch, str(path)],
            capture_output=True, text=True, check=True,
        ).stderr
        values[arch] = strongest_sha256_cdhash(details)
    return {"arches": values}


def inspect_playwright_node(path):
    import build_core
    return build_core.inspect_macos_playwright_node(path)


def install_pyinstaller_constraint_hook(_analysis_binaries, helper_destinations):
    global _ACTIVE_SESSION
    require(_ACTIVE_SESSION is None, "pyinstaller_hook_already_active")
    import PyInstaller
    from PyInstaller.building import api
    from PyInstaller.config import CONF
    import build_core

    staging = Path(os.environ["GAMJA_ENGINE_SIGNED_STAGE"])
    manifest = Path(os.environ["GAMJA_ENGINE_NATIVE_INVENTORY"])
    cache = Path(os.environ["GAMJA_ENGINE_PYINSTALLER_CACHE"])
    _owned_directory(cache, "pyinstaller_private_cache")
    CONF["cachedir"] = str(cache)
    descriptor = int(os.environ.pop("GAMJA_ENGINE_SIGNING_PASSWORD_FD"))
    password = build_core.read_secret_fd(descriptor)

    def sign_library(path):
        build_core.sign_macos_binary(path, LIBRARY_IDENTIFIER, bytearray(password))

    original, entries = install_postprocess_hook(
        api, PyInstaller, staging, manifest, helper_destinations=helper_destinations,
        sign_library=sign_library, inspect_library=inspect_signed_library,
        inspect_helper=inspect_playwright_node)
    _ACTIVE_SESSION = (api, original, password, entries, manifest)


def finalize_pyinstaller_constraint_hook():
    global _ACTIVE_SESSION
    require(_ACTIVE_SESSION is not None, "pyinstaller_hook_not_active")
    api, original, password, entries, manifest = _ACTIVE_SESSION
    try:
        require(entries and manifest.is_file(), "pyinstaller_native_inventory_empty")
    finally:
        api.process_collected_binary = original
        for index in range(len(password)):
            password[index] = 0
        _ACTIVE_SESSION = None
