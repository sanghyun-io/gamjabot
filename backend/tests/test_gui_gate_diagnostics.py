"""Bounded direct-owned GUI stderr retention, including crashes after readiness."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def load(name):
    path = Path(__file__).resolve().parents[2] / 'scripts' / f'{name}.py'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_late_crash_signature_survives_reviewed_fixture_cleanup(tmp_path):
    helper = load('gui_gate_diagnostics')
    harness = load('verify_cli_core')
    receipt = tmp_path / 'late-crash.json'
    with harness.PrivateFixture() as fixture:
        with tempfile.TemporaryFile(dir=fixture.root / 'tmp') as log:
            child = fixture.spawn([sys.executable, '-c',
                "import sys,time; print('ready',flush=True); time.sleep(.15); "
                "sys.stderr.write(\"thread 'main' panicked at /private/source/tauri/src/manager/mod.rs:730:5:\\nstate() called before manage() for fixture::HostRuntime\\n\"); sys.exit(23)"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            deadline = time.monotonic() + 3
            while True:
                log.seek(0)
                if log.read(32).startswith(b'ready'):
                    break
                assert time.monotonic() < deadline
                time.sleep(.005)
            assert not harness.child_exited(child)
            while not harness.child_exited(child):
                assert time.monotonic() < deadline
                time.sleep(.005)
            harness.PrivateFixture.reap(fixture, child)
            value = helper.snapshot_gui_diagnostics(log, exit_code=child.returncode)
            harness.write_receipt(receipt, value)
    assert all(fixture.cleanup.values())
    value = json.loads(receipt.read_text())
    assert value['exit_code'] == 23
    assert value['panics'] == [{'file': 'mod.rs', 'line': 730, 'column': 5,
        'message': 'state() called before manage() for fixture::HostRuntime'}]
    assert receipt.stat().st_mode & 0o777 == 0o600


def test_signature_redacts_known_and_shaped_secrets_and_only_keeps_panic():
    helper = load('gui_gate_diagnostics')
    from io import BytesIO
    raw = ("ordinary private owner text\nthread 'main' panicked at /Users/owner/private/app.rs:15:9:\n"
           'state failed synthetic-password bearer abc.def.xyz cap=' + 'a'*64 +
           ' jwt=eyJabc.eyJdef.signature user=private@example.com path=/Users/owner/secret.txt\n')
    value = helper.snapshot_gui_diagnostics(BytesIO(raw.encode()), exit_code=-6,
        secret_values=('synthetic-password',))
    output = json.dumps(value)
    assert all(word not in output for word in ('synthetic-password', 'abc.def.xyz', 'a'*64,
        'eyJabc.eyJdef.signature', 'private@example.com', '/Users/owner', 'ordinary private owner text'))
    assert value['panics'][0]['file'] == 'app.rs'


def test_bounded_scan_keeps_late_panic_and_fixed_json_codes():
    helper = load('gui_gate_diagnostics')
    from io import BytesIO
    raw = b'x' * 50000 + b'\n{"code":"invalid_gui_arguments","private":"must not persist"}\n'
    raw += b"thread 'main' panicked at source.rs:99:2:\nlate launch failure\n"
    value = helper.snapshot_gui_diagnostics(BytesIO(raw), exit_code=1)
    assert value['input_truncated'] is True
    assert value['typed_codes'] == ['invalid_gui_arguments']
    assert value['panics'][0]['message'] == 'late launch failure'
    assert len(json.dumps(value)) < 2048


def test_contiguous_head_tail_boundary_cannot_hide_panic():
    helper = load('gui_gate_diagnostics')
    from io import BytesIO
    raw = b'x' * (helper.SCAN_BYTES - 10) + b"\nthread 'main' panicked at source.rs:99:2:\nlaunch panic\n"
    value = helper.snapshot_gui_diagnostics(BytesIO(raw), exit_code=0)
    assert value['input_truncated'] is False
    assert value['panics'][0]['message'] == 'launch panic'
