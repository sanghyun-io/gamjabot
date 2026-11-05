import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import plistlib
import runpy
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "scripts/macos_engine_constraints.py"
BUILD_CORE = ROOT / "scripts/build_core.py"
SPEC = ROOT / "backend/gamja-engine.spec"
SIGNER = ROOT / "scripts/macos_exact_signer.c"


def load_module():
    spec = importlib.util.spec_from_file_location("macos_engine_constraints", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_build_core():
    spec = importlib.util.spec_from_file_location("build_core_constraint_test", BUILD_CORE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def macho_bytes(payload=b"payload"):
    return b"\xcf\xfa\xed\xfe" + payload


def test_constraint_is_only_sha256_candidate_cdhash_data():
    module = load_module()
    first, second = "11" * 20, "aa" * 20
    decoded = plistlib.loads(module.constraint_plist([second, first, second]))
    assert decoded == {"cdhash": {"$in": [bytes.fromhex(first), bytes.fromhex(second)]}}
    with pytest.raises(ValueError, match="candidate_cdhash_invalid"):
        module.constraint_plist(["11" * 32])


def test_postprocess_hook_uses_exact_pyinstaller_api_symbol_and_task_owned_copy(tmp_path):
    module = load_module()
    source = tmp_path / "cache" / "extension.so"
    source.parent.mkdir()
    source.write_bytes(macho_bytes())
    staging = tmp_path / "signed"
    manifest = tmp_path / "inventory.json"
    calls = []

    def original(src_name, dest_name, use_strip=False, use_upx=False, upx_exclude=None,
                 target_arch=None, codesign_identity=None, entitlements_file=None,
                 strict_arch_validation=False):
        calls.append((src_name, dest_name))
        return str(source)

    fake_api = SimpleNamespace(process_collected_binary=original)
    fake_pyinstaller = SimpleNamespace(__version__="6.22.3")
    module.install_postprocess_hook(
        fake_api, fake_pyinstaller, staging, manifest,
        helper_destinations=set(),
        sign_library=lambda path: path.write_bytes(path.read_bytes() + b"-signed"),
        inspect_library=lambda path: {"arches": {"arm64": "12" * 20}},
        inspect_helper=lambda _path: pytest.fail("library misclassified as helper"),
    )
    result = fake_api.process_collected_binary("original.so", "pkg/extension.so")

    assert calls == [("original.so", "pkg/extension.so")]
    assert Path(result).is_relative_to(staging)
    assert Path(result).read_bytes() == macho_bytes() + b"-signed"
    record = json.loads(manifest.read_text())
    assert record["entries"] == [{
        "destination": "pkg/extension.so",
        "kind": "library",
        "sha256": hashlib.sha256(macho_bytes() + b"-signed").hexdigest(),
        "arches": {"arm64": "12" * 20},
    }]
    assert source.read_bytes() == macho_bytes()


def test_postprocess_hook_rejects_version_or_signature_drift(tmp_path):
    module = load_module()

    def exact(src_name, dest_name, use_strip=False, use_upx=False, upx_exclude=None,
              target_arch=None, codesign_identity=None, entitlements_file=None,
              strict_arch_validation=False):
        return src_name

    with pytest.raises(ValueError, match="pyinstaller_version"):
        module.install_postprocess_hook(
            SimpleNamespace(process_collected_binary=exact),
            SimpleNamespace(__version__="6.22.4"), tmp_path / "stage", tmp_path / "manifest",
            helper_destinations=set(), sign_library=lambda _path: None,
            inspect_library=lambda _path: {}, inspect_helper=lambda _path: {},
        )

    def drift(src_name, dest_name, new_option=False):
        return src_name

    with pytest.raises(ValueError, match="pyinstaller_process_signature"):
        module.install_postprocess_hook(
            SimpleNamespace(process_collected_binary=drift),
            SimpleNamespace(__version__="6.22.3"), tmp_path / "stage", tmp_path / "manifest",
            helper_destinations=set(), sign_library=lambda _path: None,
            inspect_library=lambda _path: {}, inspect_helper=lambda _path: {},
        )


def test_archive_verification_compares_authoritative_native_bytes_and_inventory(tmp_path):
    module = load_module()
    first = macho_bytes(b"one-signed")
    second = macho_bytes(b"two-signed")
    archive = SimpleNamespace(
        toc={"one.so": (0, 0, 0, 0, "b"), "two.dylib": (0, 0, 0, 0, "b"),
             "plain.txt": (0, 0, 0, 0, "x")},
        extract=lambda name: {"one.so": first, "two.dylib": second,
                              "plain.txt": b"plain"}[name],
    )
    records = {"entries": [
        {"destination": "one.so", "sha256": hashlib.sha256(first).hexdigest(),
         "kind": "library", "arches": {"arm64": "11" * 20}},
        {"destination": "two.dylib", "sha256": hashlib.sha256(second).hexdigest(),
         "kind": "library", "arches": {"arm64": "22" * 20}},
    ]}
    inspected = []

    result = module.verify_carchive_native_entries(
        archive, records, tmp_path / "extract",
        inspect_library=lambda path: inspected.append(path.name) or {
            "arches": {"arm64": "11" * 20 if path.name == "one.so" else "22" * 20}
        },
        inspect_helper=lambda _path: pytest.fail("library misclassified as helper"),
    )
    assert result == ["11" * 20, "22" * 20]
    assert inspected == ["one.so", "two.dylib"]

    changed = {"entries": [dict(records["entries"][0]), dict(records["entries"][1])]}
    changed["entries"][1]["arches"] = {"arm64": "33" * 20}
    with pytest.raises(ValueError, match="archive_native_signature_mismatch"):
        module.verify_carchive_native_entries(
            archive, changed, tmp_path / "changed",
            inspect_library=lambda path: {"arches": {
                "arm64": "11" * 20 if path.name == "one.so" else "22" * 20
            }},
            inspect_helper=lambda _path: pytest.fail("library misclassified as helper"),
        )


def test_archive_verification_rejects_unaccounted_macho_in_nonbinary_toc_entry(tmp_path):
    module = load_module()
    hidden = macho_bytes(b"hidden-native")
    archive = SimpleNamespace(
        toc={"declared.txt": (0, 0, 0, 0, "x")},
        extract=lambda _name: hidden,
    )
    with pytest.raises(ValueError, match="archive_native_inventory_mismatch"):
        module.verify_carchive_native_entries(
            archive, {"format": 1, "entries": []}, tmp_path / "extract",
            inspect_library=lambda _path: pytest.fail("unaccounted Mach-O must fail first"),
            inspect_helper=lambda _path: pytest.fail("unaccounted Mach-O must fail first"),
        )


def test_playwright_payload_partitions_native_driver_as_binary(tmp_path):
    module = load_module()
    node = tmp_path / "driver" / "node"
    plain = tmp_path / "driver" / "package" / "browsers.json"
    node.parent.mkdir(parents=True)
    plain.parent.mkdir(parents=True)
    node.write_bytes(macho_bytes(b"node"))
    plain.write_text("{}")

    binaries, datas = module.partition_native_payload([
        (str(node), "playwright/driver"),
        (str(plain), "playwright/driver/package"),
    ])

    assert binaries == [(str(node), "playwright/driver")]
    assert datas == [(str(plain), "playwright/driver/package")]
    assert module.playwright_helper_destinations(binaries) == {"playwright/driver/node"}
    with pytest.raises(ValueError, match="playwright_native_inventory"):
        module.playwright_helper_destinations(
            binaries + [(str(node), "playwright/driver/extra")])


def test_postprocess_hook_preserves_upstream_playwright_node_bytes(tmp_path):
    module = load_module()
    node = tmp_path / "node"
    node.write_bytes(macho_bytes(b"upstream-signed-node"))
    stage = tmp_path / "stage"
    manifest = tmp_path / "inventory.json"
    original = node.read_bytes()

    def forbidden_original(src_name, dest_name, use_strip=False, use_upx=False,
                           upx_exclude=None, target_arch=None, codesign_identity=None,
                           entitlements_file=None, strict_arch_validation=False):
        pytest.fail("Playwright Node must bypass PyInstaller Mach-O rewriting")

    api = SimpleNamespace(process_collected_binary=forbidden_original)
    module.install_postprocess_hook(
        api, SimpleNamespace(__version__="6.22.3"), stage, manifest,
        helper_destinations={"playwright/driver/node"},
        sign_library=lambda _path: pytest.fail("Playwright Node must not be re-signed"),
        inspect_library=lambda _path: pytest.fail("Playwright Node is not a library"),
        inspect_helper=lambda _path: {"arches": {"arm64": "44" * 20}},
    )
    result = Path(api.process_collected_binary(str(node), "playwright/driver/node"))
    assert result.read_bytes() == original
    assert json.loads(manifest.read_text())["entries"] == [{
        "arches": {"arm64": "44" * 20},
        "destination": "playwright/driver/node",
        "kind": "helper",
        "sha256": hashlib.sha256(original).hexdigest(),
    }]


def test_signature_policy_requires_engine_dlv_and_constraint_but_core_has_neither():
    module = load_module()
    dlv = {"com.apple.security.cs.disable-library-validation": True}
    module.require_final_policy("engine", dlv, [b"constraint-slot"])
    module.require_final_policy("core", {}, [])
    with pytest.raises(ValueError, match="engine_library_constraint_missing"):
        module.require_final_policy("engine", dlv, [])
    with pytest.raises(ValueError, match="core_entitlements_forbidden"):
        module.require_final_policy("core", dlv, [])
    with pytest.raises(ValueError, match="core_library_constraint_forbidden"):
        module.require_final_policy("core", {}, [b"slot"])


def test_macos_minimum_parser_requires_14():
    module = load_module()
    assert module.require_macos14_build_version(
        "cmd LC_BUILD_VERSION\n      platform 1\n         minos 14.0\n"
    )
    with pytest.raises(ValueError, match="macos_minimum_too_old"):
        module.require_macos14_build_version(
            "cmd LC_BUILD_VERSION\n      platform 1\n         minos 13.5\n"
        )


def test_spec_installs_postprocess_hook_after_analysis_before_exe():
    source = SPEC.read_text()
    assert "partition_native_payload" in source
    assert "playwright_helper_destinations" in source
    assert "binaries=playwright_binaries" in source
    assert "datas=playwright_datas" in source
    hook = "install_pyinstaller_constraint_hook(a.binaries, playwright_helpers)"
    assert hook in source
    assert source.index("a = Analysis(") < source.index(hook)
    assert source.index(hook) < source.index("exe = EXE(")
    assert source.index("exe = EXE(") < source.index("finalize_pyinstaller_constraint_hook()")


@pytest.mark.parametrize("platform_name", ["win32", "darwin"])
def test_engine_spec_executes_signing_hook_only_on_darwin(monkeypatch, platform_name):
    import PyInstaller.utils.hooks as hooks

    payload = [("/fixture/playwright/driver/node", "playwright/driver")]
    monkeypatch.setattr(hooks, "collect_data_files", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(hooks, "collect_submodules", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(sys, "platform", platform_name)
    calls = []

    def partition(entries):
        calls.append("partition")
        return ([entries[0]], [])

    fake_constraints = SimpleNamespace(
        partition_native_payload=partition,
        playwright_helper_destinations=lambda entries: calls.append("helpers") or {
            "playwright/driver/node"
        },
        install_pyinstaller_constraint_hook=lambda *_args: calls.append("install"),
        finalize_pyinstaller_constraint_hook=lambda: calls.append("finalize"),
    )
    monkeypatch.setitem(sys.modules, "macos_engine_constraints", fake_constraints)
    observed = {}

    def analysis(_scripts, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(pure=[], scripts=[], binaries=kwargs["binaries"],
                               datas=kwargs["datas"])

    runpy.run_path(str(SPEC), init_globals={
        "SPECPATH": str(ROOT / "backend"),
        "Analysis": analysis,
        "PYZ": lambda *_args: object(),
        "EXE": lambda *_args, **_kwargs: object(),
    })

    if platform_name == "darwin":
        assert calls == ["partition", "helpers", "install", "finalize"]
        assert observed["binaries"] == payload
        assert observed["datas"] == []
    else:
        assert calls == []
        assert observed["binaries"] == []
        assert observed["datas"] == payload


def test_signature_verification_is_semantic_and_per_arch(monkeypatch, tmp_path):
    module = load_build_core()
    binary = tmp_path / "library.dylib"
    binary.write_bytes(macho_bytes())
    certificate = tmp_path / "leaf.cer"
    certificate.write_bytes(b"exact-leaf-der")
    monkeypatch.setattr(module, "MAC_CORE_CERTIFICATE", certificate)
    monkeypatch.setattr(module, "mac_signing_fingerprint", lambda: "AA" * 20)
    calls = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[:2] == ["/usr/bin/lipo", "-archs"]:
            return SimpleNamespace(returncode=0, stdout="arm64 x86_64\n", stderr="")
        if "--extract-certificates" in " ".join(arguments):
            option = next(value for value in arguments if value.startswith("--extract-certificates="))
            Path(option.split("=", 1)[1] + "0").write_bytes(certificate.read_bytes())
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "--entitlements" in arguments:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if "--verbose=4" in arguments:
            arch = arguments[arguments.index("--arch") + 1]
            return SimpleNamespace(
                returncode=0,
                stdout="",
                stderr=(f"Identifier={module.MAC_LIBRARY_IDENTIFIER}\n"
                        "CodeDirectory v=20500 size=1 flags=0x10000(runtime) hashes=1+0 location=embedded\n"),
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    module.verify_macos_signature(binary, module.MAC_LIBRARY_IDENTIFIER)

    semantic = [call for call in calls if "--verify" in call]
    assert len(semantic) == 2
    assert {call[call.index("--arch") + 1] for call in semantic} == {"arm64", "x86_64"}
    assert all(any(value.startswith("-R=anchor = H\"") for value in call) for call in semantic)
    extracts = [call for call in calls if "--extract-certificates" in " ".join(call)]
    assert len(extracts) == 2
    assert all(any(value.startswith("--extract-certificates=") for value in call)
               for call in extracts)
    assert not any("-dr" in call for call in calls)


def test_core_and_engine_reject_universal_binary_until_slot_parser_is_per_arch(monkeypatch, tmp_path):
    module = load_build_core()
    binary = tmp_path / "core"
    binary.write_bytes(macho_bytes())
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout="arm64 x86_64\n", stderr=""))
    monkeypatch.setattr(module, "mac_designated_requirement",
                        lambda identifier: f'designated => identifier "{identifier}"')
    with pytest.raises(ValueError, match="single native architecture"):
        module.verify_macos_signature(binary, module.MAC_CORE_IDENTIFIER)


def test_production_signer_preserves_constraint_only_for_engine():
    source = SIGNER.read_text()
    assert 'LOAD_KEY(preserve_metadata, "kSecCodeSignerPreserveMetadata")' in source
    assert "kSecCodeSignerPreserveEntitlements | kSecCodeSignerPreserveLibraryConstraints" in source
    assert "preserve_engine_policy" in source
    assert "restrict_access_to_sign" not in source


def test_build_core_passes_secret_by_fd_and_verifies_finished_carchive(monkeypatch, tmp_path):
    module = load_build_core()
    observed = {}
    secret = b"private-value-93847"

    def run(arguments, **kwargs):
        observed.update(arguments=arguments, kwargs=kwargs)
        descriptor = kwargs["pass_fds"][0]
        observed["secret"] = __import__("os").read(descriptor, 4096)
        (tmp_path / "engine-dist").mkdir()
        (tmp_path / "engine-dist/gamja-engine").write_bytes(b"finished-onefile")
        inventory = Path(kwargs["env"]["GAMJA_ENGINE_NATIVE_INVENTORY"])
        inventory.write_text(json.dumps({"format": 1, "entries": [{
            "destination": "native.so", "sha256": "1" * 64,
            "kind": "library", "arches": {"arm64": "2" * 40},
        }]}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.MAC_CONSTRAINTS, "verify_carchive_native_entries",
                        lambda archive, inventory, root, inspect_library, inspect_helper:
                        ["2" * 40])
    import PyInstaller.archive.readers as readers
    monkeypatch.setattr(readers, "CArchiveReader", lambda _path: object())

    engine, hashes = module.build_macos_frozen_engine(tmp_path, bytearray(secret))

    assert engine.read_bytes() == b"finished-onefile"
    assert hashes == ["2" * 40]
    assert observed["secret"] == secret
    assert secret.decode() not in "\0".join(observed["arguments"])
    assert all(secret.decode() not in str(value) for value in observed["kwargs"]["env"].values())
    assert observed["arguments"][-1] == "gamja-engine.spec"
    assert not any(str(engine) == argument for argument in observed["arguments"])


def test_source_inventory_includes_constraint_producer_and_macos14_tauri_config():
    module = load_build_core()
    paths = module.build_input_paths(with_helper=True, macos=True)
    for expected in (
        "scripts/macos_engine_constraints.py",
        "scripts/macos_exact_signer.c",
        "backend/gamja-engine.spec",
        "backend/requirements.txt",
        "backend/requirements-build.txt",
        "core-host/Cargo.toml",
        "core-host/Cargo.lock",
        "frontend/src-tauri/Cargo.toml",
        "frontend/src-tauri/Cargo.lock",
        "frontend/src-tauri/tauri.conf.json",
    ):
        assert expected in paths
