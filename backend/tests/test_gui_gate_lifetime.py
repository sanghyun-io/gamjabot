"""Controller-owned polite Quit and cancellation-safe cleanup acceptance."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import pytest


def load(name):
    path = Path(__file__).resolve().parents[2] / 'scripts' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(path.parent))
        spec.loader.exec_module(module)
    return module


def test_cleanup_interrupt_is_deferred_until_owned_children_and_root_removed(monkeypatch, tmp_path):
    import json
    lifetime = load('gui_gate_lifetime')
    h = load('verify_cli_core')
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    receipt = {'failure': None}
    fixture = h.PrivateFixture()
    fixture.__enter__()
    child = fixture.spawn([sys.executable, '-c', 'import time; time.sleep(30)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    original_sleep = time.sleep
    sent = []
    def interrupt_cleanup(seconds):
        if not sent:
            sent.append(True)
            os.kill(os.getpid(), signal.SIGINT)
            os.kill(os.getpid(), signal.SIGTERM)
        original_sleep(seconds)
    try:
        with lifetime.cleanup_signal_shield(receipt):
            with monkeypatch.context() as patch:
                patch.setattr(h.time, 'sleep', interrupt_cleanup)
                fixture.__exit__(None, None, None)
            h.write_receipt(tmp_path / 'cleanup.json', receipt)
        assert child.returncode is not None and all(fixture.cleanup.values())
        assert receipt['cleanup_interrupts'] == 2 and receipt['failure'] == 'operator_interrupted'
        assert json.loads((tmp_path / 'cleanup.json').read_text())['failure'] == 'operator_interrupted'
        assert all(signal.getsignal(sig) == handler for sig, handler in previous.items())
    finally:
        if child.pid not in fixture.finalized:
            fixture.__exit__(None, None, None)


def test_polite_quit_rejects_kernel_identity_mismatch_before_native_request(monkeypatch):
    lifetime = load('gui_gate_lifetime')
    h = load('verify_cli_core')
    child = SimpleNamespace(pid=123)
    fixture = SimpleNamespace(children=[child], finalized=set(), command=lambda *_args, **_kw: pytest.fail('no native request'))
    monkeypatch.setattr(h, 'child_exited', lambda _: False)
    monkeypatch.setattr(lifetime, 'kernel_start', lambda *_: (2, 2))
    monkeypatch.setattr(lifetime, 'process_inventory', lambda *_: [dict(pid=123, executable='/fixture/gamja-bot', started='same')])
    with pytest.raises(h.GateFailure, match='gui_identity_unproven'):
        lifetime.polite_close(h, fixture, child, None,
            {'pid': 123, 'started': 'same', 'executable': '/fixture/gamja-bot', 'kernel_start': (1, 1)},
            Path('/fixture/helper.swift'), phase='gui-closed', exit_seconds=18, attest=lambda _: None)


@pytest.mark.parametrize('exit_code,expected', [(0, None), (7, 'gui_abnormal_close'), (-6, 'gui_abnormal_close')])
def test_native_quit_requires_actual_exit_zero_without_panic(monkeypatch, exit_code, expected):
    lifetime = load('gui_gate_lifetime')
    h = load('verify_cli_core')
    from io import BytesIO
    child = SimpleNamespace(pid=123, returncode=exit_code)
    fixture = SimpleNamespace(children=[child], finalized=set(), reap=lambda *_: None)
    monkeypatch.setattr(lifetime, 'request_polite_quit', lambda *_: {'requested': True,
        'request_monotonic_ns': time.monotonic_ns(), 'launch_date_available': False})
    monkeypatch.setattr(h, 'child_exited', lambda _: True)
    if expected:
        with pytest.raises(h.GateFailure, match=expected):
            lifetime.polite_close(h, fixture, child, BytesIO(b''), {}, Path('unused'),
                phase='gui-closed', exit_seconds=18, attest=lambda _: None)
    else:
        value = lifetime.polite_close(h, fixture, child, BytesIO(b''), {}, Path('unused'),
            phase='gui-closed', exit_seconds=18, attest=lambda _: None)
        assert value['exit_code'] == 0 and value['exit_budget_seconds'] == 18


def test_native_quit_exit_deadline_uses_request_time_not_ack_time(monkeypatch):
    lifetime = load('gui_gate_lifetime')
    h = load('verify_cli_core')
    child = SimpleNamespace(pid=123)
    fixture = SimpleNamespace(children=[child], finalized=set())
    monkeypatch.setattr(lifetime, 'request_polite_quit', lambda *_: {'requested': True,
        'request_monotonic_ns': 10_000_000_000, 'launch_date_available': False})
    monkeypatch.setattr(lifetime.time, 'monotonic_ns', lambda: 29_000_000_000)
    monkeypatch.setattr(h, 'child_exited', lambda _: True)
    with pytest.raises(h.GateFailure, match='gui_normal_exit_timeout'):
        lifetime.polite_close(h, fixture, child, None, {}, Path('unused'),
            phase='gui-closed', exit_seconds=18, attest=lambda _: None)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS kernel identity fixture')
def test_kernel_identity_of_actual_held_child_is_stable_and_quit_helper_fails_closed():
    lifetime = load('gui_gate_lifetime')
    h = load('verify_cli_core')
    with h.PrivateFixture() as fixture:
        child = fixture.spawn([sys.executable, '-c', 'import time; time.sleep(30)'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        first = lifetime.kernel_start(h, child.pid)
        assert lifetime.kernel_start(h, child.pid) == first
        helper = Path(__file__).resolve().parents[2] / 'scripts/gui_gate_native_quit.swift'
        # Invalid candidate path/start tuple rejects before any native request.
        result = subprocess.run(['/usr/bin/swift', str(helper), str(child.pid),
            str(first[0]+1), str(first[1]), '/invalid/fixture.app', '/invalid/fixture.app/Contents/MacOS/gamja-bot'],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=8, env=fixture.env)
        assert result.returncode == 2 and result.stdout == b'{"code":"gui_identity_unproven"}\n'
        assert not h.child_exited(child)
    assert all(fixture.cleanup.values())


def test_native_quit_source_has_no_force_termination_and_finally_is_fully_shielded():
    import ast
    root = Path(__file__).resolve().parents[2]
    native = (root / 'scripts/gui_gate_native_quit.swift').read_text()
    assert 'app.terminate()' in native and 'forceTerminate' not in native
    tree = ast.parse((root / 'scripts/verify_gui_core.py').read_text())
    outer = next(node for node in tree.body if isinstance(node, ast.Try))
    shield = outer.finalbody[0]
    assert isinstance(shield, ast.With) and shield.items[0].context_expr.func.id == 'cleanup_signal_shield'
    calls = [node for node in ast.walk(shield) if isinstance(node, ast.Call)]
    assert any(isinstance(node.func, ast.Attribute) and node.func.attr == '__exit__' for node in calls)
    assert any(isinstance(node.func, ast.Name) and node.func.id == 'commit_receipt' for node in calls)


@pytest.mark.parametrize('arrival', ['during_final_write', 'after_final_write', 'before_commit'])
def test_final_receipt_cancellation_has_one_authoritative_result(monkeypatch, tmp_path, arrival):
    import json
    lifetime = load('gui_gate_lifetime')
    h = load('verify_cli_core')
    receipt = {'status': 'passed_scoped_gui_gate', 'failure': None, 'cleanup': {'complete': True}}
    destination = tmp_path / 'final.json'
    original = h.write_receipt
    def final_write(path, value):
        original(path, value)
        if arrival == 'during_final_write':
            os.kill(os.getpid(), signal.SIGTERM)
    with monkeypatch.context() as patch:
        patch.setattr(h, 'write_receipt', final_write)
        with lifetime.cleanup_signal_shield(receipt) as commit_receipt:
            if arrival == 'before_commit':
                os.kill(os.getpid(), signal.SIGINT)
            commit_receipt(h, destination)
            if arrival == 'after_final_write':
                os.kill(os.getpid(), signal.SIGTERM)
            assert json.loads(destination.read_text()) == receipt
            assert destination.stat().st_mode & 0o777 == 0o600
    expected = 'failed' if arrival == 'before_commit' else 'passed_scoped_gui_gate'
    assert receipt['status'] == expected
    assert (receipt['failure'] == 'operator_interrupted') == (arrival == 'before_commit')
    assert (0 if receipt['status'] == 'passed_scoped_gui_gate' else 1) == (1 if arrival == 'before_commit' else 0)


def test_actual_controller_postwrite_stage_signal_preserves_committed_outcome(tmp_path):
    import ast
    import json
    lifetime = load('gui_gate_lifetime')
    h = load('verify_cli_core')
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / 'scripts/verify_gui_core.py').read_text())
    finalbody = next(node for node in tree.body if isinstance(node, ast.Try)).finalbody
    destination = tmp_path / 'controller.json'
    receipt = {'status': 'passed_scoped_gui_gate', 'failure': None, 'cleanup': {'complete': True}}
    def stage(*_args, **_kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
    namespace = dict(cleanup_signal_shield=lifetime.cleanup_signal_shield, receipt=receipt,
        gui_logs=[], fixture=None, mount=None, h=h, RECEIPT=destination,
        signal=signal, previous_term=signal.getsignal(signal.SIGTERM), stage=stage)
    exec(compile(ast.Module(body=finalbody, type_ignores=[]), str(root / 'scripts/verify_gui_core.py'), 'exec'), namespace)
    assert json.loads(destination.read_text()) == receipt
    assert receipt['status'] == 'passed_scoped_gui_gate' and receipt['failure'] is None
