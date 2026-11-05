"""Actual operator acceptance primitives, without launching the GUI or mounts."""
import importlib.util
from io import BytesIO
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import pytest


def load(name):
    path = Path(__file__).resolve().parents[2] / 'scripts' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(path.parent))
        spec.loader.exec_module(module)
    return module


def test_live_owned_child_cannot_accept_unexpected_candidate_or_default_core(monkeypatch):
    checks = load('gui_gate_checks')
    h = load('verify_cli_core')
    baseline = [{'pid': 10, 'started': 'owner-start', 'kind': 'gui', 'executable': '/Applications/Owner.app/Contents/MacOS/gamja-bot'}]
    calls = []
    original = h.os.killpg
    def signal_owned(*values):
        calls.append(values)
        return original(*values)
    monkeypatch.setattr(h.os, 'killpg', signal_owned)
    with h.PrivateFixture() as fixture:
        child = fixture.spawn([sys.executable, '-c', 'import time; time.sleep(3)'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert not h.child_exited(child)
        owned = {'pid': child.pid, 'started': 'fixture-start', 'kind': 'gui',
            'executable': '/fixture/Gamja.app/Contents/MacOS/gamja-bot'}
        checks.validate_inventory(h, baseline, [owned], baseline + [owned])
        for kind in ('gui', 'core'):
            replacement = {'pid': 21, 'started': 'replacement-start', 'kind': kind,
                'executable': '/fixture/' + ('gamja-bot' if kind == 'gui' else 'gamja')}
            with pytest.raises(h.GateFailure, match='unexpected_gui_or_core_instance'):
                checks.validate_inventory(h, baseline, [owned], baseline + [owned, replacement])
        assert calls == []
    assert all(fixture.cleanup.values())
    assert all(values[0] == child.pid for values in calls)


@pytest.mark.parametrize('phase', ['gui-closed', 'reopened-closed'])
@pytest.mark.parametrize('exit_code,panic', [(0, False), (7, False), (-6, False), (0, True)])
def test_controller_close_requires_normal_exit_and_no_panic(phase, exit_code, panic):
    checks = load('gui_gate_checks')
    h = load('verify_cli_core')
    code = "import sys,time,os,signal; time.sleep(.05); "
    if panic:
        code += "sys.stderr.write(\"thread 'main' panicked at source.rs:17:2:\\nlate panic\\n\"); "
    code += f'os.kill(os.getpid(), signal.SIGABRT)' if exit_code == -6 else f'sys.exit({exit_code})'
    fixture = h.PrivateFixture()
    with fixture:
        with tempfile.TemporaryFile(dir=fixture.root / 'tmp') as log:
            child = fixture.spawn([sys.executable, '-c', code], stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            until = time.monotonic() + 3
            while not h.child_exited(child):
                assert time.monotonic() < until
                time.sleep(.005)
            if exit_code == 0 and not panic:
                checks.accept_normal_close(h, fixture, child, log, phase=phase)
            else:
                with pytest.raises(h.GateFailure, match='gui_abnormal_close'):
                    checks.accept_normal_close(h, fixture, child, log, phase=phase)
            assert child.returncode == exit_code
    assert all(fixture.cleanup.values())


def test_normal_exit_with_truncated_log_cannot_prove_no_panic():
    checks = load('gui_gate_checks')
    h = load('verify_cli_core')
    child = type('Child', (), {'returncode': 0})()
    fixture = type('Fixture', (), {'reap': lambda *_: None})()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(h, 'child_exited', lambda _: True)
        with pytest.raises(h.GateFailure, match='gui_abnormal_close'):
            checks.accept_normal_close(h, fixture, child, BytesIO(b'x' * 40000), phase='gui-closed')


@pytest.mark.parametrize('elapsed', [55, 56])
def test_controller_inventory_crossing_deadline_reports_typed_timeout(elapsed):
    # Execute only the real checkpoint definition: importing this operator would
    # launch the sealed GUI. The inventory itself consumes the remaining budget.
    import ast
    from types import SimpleNamespace
    h = load('verify_cli_core')
    path = Path(__file__).resolve().parents[2] / 'scripts/verify_gui_core.py'
    node = next(node for node in ast.parse(path.read_text()).body
                if isinstance(node, ast.FunctionDef) and node.name == 'checkpoint')
    clock = SimpleNamespace(now=0)
    calls = []
    def inventory(stage):
        calls.append(stage)
        if stage.endswith('_waiting'):
            clock.now = elapsed
    def forbidden_select(*args):
        pytest.fail('expired absolute deadline must not enter select')
    namespace = {'time': SimpleNamespace(monotonic=lambda: clock.now),
        'select': SimpleNamespace(select=forbidden_select),
        'sys': SimpleNamespace(stdin=None), 'attest_instance': inventory,
        'stage': lambda *_args, **_kwargs: None, 'children': [], 'h': h, 'receipt': {}}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    with pytest.raises(h.GateFailure, match='^cua_stage_timeout$'):
        namespace['checkpoint']('fixture-visible')
    assert calls == ['fixture-visible_before', 'fixture-visible_waiting']


def controller_checkpoint_namespace(**overrides):
    import ast
    from types import SimpleNamespace
    h = load('verify_cli_core')
    path = Path(__file__).resolve().parents[2] / 'scripts/verify_gui_core.py'
    tree = ast.parse(path.read_text())
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'checkpoint')
    values = {'time': SimpleNamespace(monotonic=lambda: 0), 'select': SimpleNamespace(select=lambda *_: ([], [], [])),
        'sys': SimpleNamespace(stdin=SimpleNamespace(fileno=lambda: 0)), 'os': SimpleNamespace(read=lambda *_: b''),
        'attest_instance': lambda _: None, 'stage': lambda *_args, **_kwargs: None,
        'children': [], 'h': h, 'receipt': {}}
    values.update(overrides)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), values)
    return values, tree


def test_visibility_budget_is_55_and_close_budgets_unchanged():
    import ast
    values, tree = controller_checkpoint_namespace()
    assert values['checkpoint'].__defaults__ == (55,)
    calls = {node.args[0].value: {keyword.arg: keyword.value.value for keyword in node.keywords}
        for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == 'checkpoint' and isinstance(node.args[0], ast.Constant)}
    assert set(calls) == {'fixture-visible', 'reopened-visible'}
    closes = {next(keyword.value.value for keyword in node.keywords if keyword.arg == 'phase'):
              next(keyword.value.value for keyword in node.keywords if keyword.arg == 'exit_seconds')
              for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == 'polite_close'}
    assert closes == {'gui-closed': 18, 'reopened-closed': 30}


def test_checkpoint_rejects_batched_protocol_commands():
    import os
    import select
    from types import SimpleNamespace
    read, write = os.pipe()
    try:
        os.write(write, b'cua-check\ncua-observed\n')
        values, _ = controller_checkpoint_namespace(os=os, select=select,
            sys=SimpleNamespace(stdin=SimpleNamespace(fileno=lambda: read)))
        with pytest.raises(values['h'].GateFailure, match='^operator_input_invalid$'):
            values['checkpoint']('fixture-visible')
        assert values['receipt']['cua_checkpoints'][0]['preflights'] == 0
    finally:
        os.close(read)
        os.close(write)


def test_cua_preflight_does_not_reset_absolute_visibility_deadline():
    from types import SimpleNamespace
    clock = SimpleNamespace(now=0, waits=0)
    def inventory(stage):
        if stage.endswith('_waiting'):
            clock.waits += 1
            clock.now = 54 if clock.waits == 1 else 56
    calls = []
    def selected(inputs, _write, _errors, timeout):
        calls.append(timeout)
        return inputs, [], []
    h = load('verify_cli_core')
    h.child_exited = lambda _: False
    values, _ = controller_checkpoint_namespace(h=h, time=SimpleNamespace(monotonic=lambda: clock.now),
        attest_instance=inventory, select=SimpleNamespace(select=selected),
        os=SimpleNamespace(read=lambda *_: b'cua-check\n'), children=[SimpleNamespace(pid=123)])
    with pytest.raises(h.GateFailure, match='^cua_stage_timeout$'):
        values['checkpoint']('fixture-visible')
    assert calls == [1]
    assert values['receipt']['cua_checkpoints'][0] == {'phase': 'fixture-visible', 'budget_seconds': 55,
        'elapsed_ms': 56000, 'preflights': 1, 'postflights': 0, 'completed': False}


def test_acceptance_inventory_cannot_finish_after_absolute_deadline():
    from types import SimpleNamespace
    clock = SimpleNamespace(now=0)
    commands = iter((b'cua-check\n', b'cua-observed\n', b'fixture-visible\n'))
    def inventory(stage):
        if stage.endswith('_accepted'):
            clock.now = 56
    h = load('verify_cli_core')
    h.child_exited = lambda _: False
    values, _ = controller_checkpoint_namespace(h=h, time=SimpleNamespace(monotonic=lambda: clock.now),
        attest_instance=inventory, select=SimpleNamespace(select=lambda inputs, *_: (inputs, [], [])),
        os=SimpleNamespace(read=lambda *_: next(commands)), children=[SimpleNamespace(pid=123)])
    with pytest.raises(h.GateFailure, match='^cua_stage_timeout$'):
        values['checkpoint']('fixture-visible')
    record = values['receipt']['cua_checkpoints'][0]
    assert record['elapsed_ms'] == 56000 and record['preflights'] == record['postflights'] == 1
    assert record['completed'] is False


def test_initial_visible_receipt_is_distinct_and_does_not_claim_visual_replies():
    import ast
    path = Path(__file__).resolve().parents[2] / 'scripts/verify_gui_core.py'
    tree = ast.parse(path.read_text())
    strings = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    receipt = 'docs/.logs/task-8-sealed-default-arm-source-fixture-root-owned-20261001.json'
    assert receipt in strings
    assert 'be4447beb400d66ca642b8c2a9dadb519d6cb1c5b36a0ce4750476bccfc5144a' in strings
    assert 'docs/.logs/task-8-default-arm-sealed-app-inventory-20261001.json' in strings
    assert 'docs/.logs/task-8-sealed-gui-source-fixture-native-quit-20261001.json' not in strings
    assert all('docs/.logs/task-8-sealed-gui-source-fixture-' + stem + '-20260930.json' not in strings
               for stem in ('retained', 'checkpoint-corrected', 'visibility-budget', 'initial-visible'))
    assert 'actual_gui_reopen_fixture_account' in strings
    assert 'actual_gui_reopen_existing_fixture_replies' not in strings
