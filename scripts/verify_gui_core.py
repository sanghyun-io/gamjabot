#!/usr/bin/env python3
"""Bounded operator controller executing the reviewed Task8 sealed-GUI runbook."""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from gui_gate_diagnostics import snapshot_gui_diagnostics
from gui_gate_checks import process_inventory, validate_inventory
from gui_gate_lifetime import kernel_start, polite_close, cleanup_signal_shield

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('harness', ROOT / 'scripts/verify_cli_core.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
DMG = Path(os.environ.get('GAMJA_GUI_DMG', str(ROOT / 'frontend/src-tauri/target/aarch64-apple-darwin/release/bundle/dmg/Gamja Bot_1.0.0_aarch64_core.dmg')))
RECEIPT = ROOT / 'docs/.logs/task-8-sealed-default-arm-source-fixture-root-owned-20261001.json'
EXPECTED_ARTIFACT_SHA256 = ''  # Filled with the reviewed candidate binding in Task 5 Phase B.
receipt = {'format': 1, 'scope': 'sealed_packaged_gui_core_source_fixture_engine', 'status': 'failed', 'failure': None,
    'artifact_hashes': {}, 'runtime': [], 'gates': {}, 'cleanup': {},
    'limitations': ['Source-only fake provider and BOTH memory credential stores; effects denied',
                    'Fully frozen account/control gate recorded separately', 'No external vendor/OS store/native effects',
                    'Schedule limits and durable trigger queue; no time-based cron', 'No Owner installation/cutover']}
fixture = None
mount = None
mounted = False
endpoint = None
children = []
gui_logs = []
baseline = []
owned_instances = []
gui_identities = {}
previous_term = signal.getsignal(signal.SIGTERM)
def interrupted(*_):
    raise KeyboardInterrupt
signal.signal(signal.SIGTERM, interrupted)

def stage(name, **values):
    print(json.dumps({'stage': name, **values}), flush=True)

def attest_instance(stage_name):
    observed = process_inventory(h)
    record = receipt.setdefault('process_isolation', {'baseline': [], 'checks': 0})
    record['checks'] += 1
    record['latest_stage'] = stage_name
    record['latest'] = [{key: row[key] for key in ('pid', 'started', 'kind')} for row in observed]
    validate_inventory(h, baseline, owned_instances, observed)
    return observed

def register_owned_instance(child, executable):
    observed = process_inventory(h)
    matches = [row for row in observed if row['pid'] == child.pid and row['executable'] == str(executable.resolve())]
    h.require(len(matches) == 1, 'process_isolation_unproven')
    owned_instances.append(matches[0])
    attest_instance('owned_instance_registered')
    return matches[0]

def checkpoint(expected, timeout=55):
    attest_instance(expected + '_before')
    pending_cua = False
    observed_cua = False
    started = time.monotonic()
    deadline = started + timeout
    records = receipt.setdefault('cua_checkpoints', [])
    h.require(len(records) < 4, 'operator_stage_failed')
    record = {'phase': expected, 'budget_seconds': timeout, 'elapsed_ms': 0,
              'preflights': 0, 'postflights': 0, 'completed': False}
    records.append(record)
    def remaining_budget():
        remaining = deadline - time.monotonic()
        record['elapsed_ms'] = round((timeout - remaining) * 1000)
        h.require(remaining > 0, 'cua_stage_timeout')
        return remaining
    stage('await_cua', expected=expected)
    while time.monotonic() < deadline:
        attest_instance(expected + '_waiting')
        if children and expected not in {'gui-closed', 'reopened-closed'}:
            h.require(not h.child_exited(children[-1]), 'gui_exited_during_cua_stage')
        remaining = remaining_budget()
        ready, _, _ = select.select([sys.stdin.fileno()], [], [], min(1, remaining))
        if ready:
            # One bounded ASCII line per command; no TextIO read-ahead can hide
            # a queued line from select. Batches and partial lines fail closed.
            raw = os.read(sys.stdin.fileno(), 256)
            remaining_budget()
            h.require(raw, 'operator_input_ended')
            h.require(len(raw) <= 64 and raw.endswith(b'\n') and raw.count(b'\n') == 1
                      and raw[:-1].isascii() and b'\r' not in raw, 'operator_input_invalid')
            value = raw[:-1].decode('ascii')
            if value == 'cua-check':
                h.require(not pending_cua, 'cua_postflight_missing')
                h.require(children and not h.child_exited(children[-1]), 'gui_exited_before_cua')
                attest_instance(expected + '_before_cua')
                remaining_budget()
                pending_cua = True
                record['preflights'] += 1
                stage('candidate_alive_for_cua', gui_pid=children[-1].pid)
                continue
            if value == 'cua-observed':
                h.require(pending_cua, 'cua_preflight_missing')
                attest_instance(expected + '_after_cua')
                remaining_budget()
                pending_cua = False
                observed_cua = True
                record['postflights'] += 1
                stage('candidate_isolation_after_cua', gui_pid=children[-1].pid)
                continue
            h.require(value == expected, 'operator_stage_failed')
            h.require(observed_cua and not pending_cua, 'cua_preflight_missing')
            attest_instance(expected + '_accepted')
            remaining_budget()
            record['completed'] = True
            return
    record['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    raise h.GateFailure('cua_stage_timeout')

def checked(args, timeout=30):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    h.require(result.returncode == 0, 'artifact_or_mount_command_failed')
    return result.stdout

try:
    h.require(not RECEIPT.exists(), 'receipt_exists')
    expected_hash = 'be4447beb400d66ca642b8c2a9dadb519d6cb1c5b36a0ce4750476bccfc5144a'
    h.require(DMG.is_file() and not DMG.is_symlink() and h.sha256(DMG) == expected_hash, 'dmg_hash_mismatch')
    receipt['artifact_hashes']['dmg'] = expected_hash
    mount = Path(tempfile.mkdtemp(prefix='gamja-task8-gui-mount-'))
    mount.chmod(0o700)
    mount = mount.resolve()
    checked(['/usr/bin/hdiutil', 'attach', '-readonly', '-nobrowse', '-mountpoint', str(mount), str(DMG)], timeout=45)
    mounted = True
    app = mount / 'Gamja Bot.app'
    gui = app / 'Contents/MacOS/gamja-bot'
    artifact = app / 'Contents/MacOS/gamja-artifact'
    h.require(len(EXPECTED_ARTIFACT_SHA256) == 64 and artifact.is_file()
              and not artifact.is_symlink() and h.sha256(artifact) == EXPECTED_ARTIFACT_SHA256,
              'artifact_hash_mismatch')
    checked(['/usr/bin/codesign', '--verify', '--strict', '--all-architectures', str(artifact)])
    artifact_detail_result = subprocess.run(
        ['/usr/bin/codesign', '-dv', '--verbose=4', str(artifact)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    h.require(artifact_detail_result.returncode == 0, 'artifact_signature_policy')
    artifact_details = (artifact_detail_result.stdout + artifact_detail_result.stderr).decode(
        'utf-8', 'replace')
    h.require('Identifier=top.gamja.artifact' in artifact_details
              and 'Signature=adhoc' in artifact_details
              and 'Timestamp=' not in artifact_details, 'artifact_signature_policy')
    checked(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)])
    expected = json.loads((ROOT / 'docs/.logs/task-8-default-arm-sealed-app-inventory-20261001.json').read_text())
    actual = {}
    for path in app.rglob('*'):
        relative = path.relative_to(app).as_posix()
        if path.is_symlink():
            actual[relative] = {'symlink': os.readlink(path)}
        elif path.is_file():
            info = path.stat()
            actual[relative] = {'sha256': h.sha256(path), 'bytes': info.st_size, 'executable': bool(info.st_mode & 0o111)}
    h.require(actual == expected, 'sealed_app_inventory_mismatch')
    core, _, hashes, _ = h.artifacts(SimpleNamespace(package=app / 'Contents/Resources/gamja-core'))
    receipt['artifact_hashes'].update(hashes)
    receipt['artifact_hashes']['gui'] = h.sha256(gui)
    receipt['artifact_hashes']['gamja_artifact'] = EXPECTED_ARTIFACT_SHA256
    entry = ROOT / 'scripts/fixtures/gui_core_engine.py'
    receipt['artifact_hashes']['source_fixture'] = h.sha256(entry)
    receipt['artifact_hashes']['source_native_quit'] = h.sha256(ROOT / 'scripts/gui_gate_native_quit.swift')
    receipt['artifact_hashes']['source_lifetime'] = h.sha256(ROOT / 'scripts/gui_gate_lifetime.py')
    receipt['gates']['sealed_dmg_signature_and_exact_inventory'] = 'pass'
    fixture = h.PrivateFixture()
    fixture.__enter__()
    fixture.root = fixture.root.resolve()
    fixture.env["TMPDIR"] = str(fixture.root / "tmp")
    fixture.env["XDG_CONFIG_HOME"] = str(fixture.root / "home")
    data = fixture.root / 'appdata'
    python = Path(sys.executable)
    words = [str(core), '--app-data', str(data), '--engine', str(python), '--engine-arg', str(entry),
             '--helper', str(fixture.root / 'missing-native-helper'), '--startup-timeout-ms', '15000']
    endpoint = None
    def control(operation):
        global endpoint
        endpoint = h.read_endpoint(data / 'owner-cli.json')
        fixture.endpoint = endpoint
        for field in ('api_url', 'control_url'):
            port = h.loopback(endpoint[field])
            if port not in fixture.resources['loopback_ports']:
                fixture.resources['loopback_ports'].append(port)
        status, value = h.local_json(endpoint['control_url'], '/control', endpoint['capability'], {'operation': operation})
        h.require(status == 200, 'fixture_control_failed')
        return value
    def launch_core():
        child = fixture.spawn([*words, 'serve'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            h.require(not h.child_exited(child), 'core_exited_before_ready')
            if (data / 'owner-cli.json').exists():
                health = control('health')
                if health.get('engine') == 'ready':
                    h.require(health['core_pid'] == child.pid and health.get('native_helper') == 'unavailable', 'runtime_identity_mismatch')
                    identity = h.process_identity(health['engine_pid'])
                    h.require(identity is not None, 'runtime_identity_mismatch')
                    fixture.engines.append((health['engine_pid'], identity))
                    fixture.resources['engine_process_pids'].append(health['engine_pid'])
                    receipt['runtime'].append({'core_pid': child.pid, 'engine_pid': health['engine_pid']})
                    return child, health
            time.sleep(.05)
        raise h.GateFailure('core_ready_timeout')
    baseline = process_inventory(h)
    receipt['process_isolation'] = {'baseline': [{key: row[key] for key in ('pid', 'started', 'kind')} for row in baseline], 'checks': 0}
    core_child, first = launch_core()
    register_owned_instance(core_child, core)
    session = control('create_ui_session')['session_token']
    headers = {'X-Gamja-Session': session, 'Origin': 'tauri://localhost'}
    def api(method, path, body=None):
        status, value = h.local_api_json(endpoint['api_url'], '/api/v1' + path,
            headers['X-Gamja-Session'], body, method=method,
            jwt=headers.get('Authorization', '').removeprefix('Bearer ') or None)
        h.require(status < 400, 'canonical_api_fixture_failed')
        return value
    password = secrets.token_urlsafe(24)
    account = {'email': 'task8-isolated@example.com', 'username': 'Task8 isolated fixture', 'password': password}
    api('POST', '/auth/register', account)
    token = api('POST', '/auth/login', {'email': account['email'], 'password': password, 'remember_device': True})['access_token']
    headers['Authorization'] = 'Bearer ' + token
    api('POST', '/auth/restore')
    connection = api('POST', '/connections', {'provider_kind': 'compatible', 'label': 'Task8 local fake',
        'base_url': 'http://127.0.0.1:8765/v1', 'allow_local_http': True, 'auth_required': False,
        'default_model_id': 'exact'})['id']
    h.require(api('POST', f'/connections/{connection}/test')['ok'], 'fake_probe_failed')
    agent = api('POST', '/agents/', {'name': 'Task8 fixture worker', 'description': 'Synthetic local fixture',
        'system_prompt': 'Synthetic local work', 'connection_id': connection, 'model_id': 'exact'})['id']
    project = api('POST', '/projects/', {'name': 'Task8 isolated fixture project'})['id']
    api('POST', f'/projects/{project}/agents/{agent}')
    room = api('POST', '/conversations/', {'project_id': project, 'title': 'Task8 lifetime fixture room'})['id']
    api('POST', f'/conversations/{room}/members/{agent}')
    limits = {'max_rounds': 1, 'max_tool_calls': 1, 'max_executor_seconds': 3}
    h.require(api('PUT', f'/conversations/{room}/schedule', limits) == limits, 'schedule_limits_failed')
    db_path = data / 'gamja_bot.db'
    db_identity = (db_path.stat().st_dev, db_path.stat().st_ino)
    receipt['database_identity'] = {'device': db_identity[0], 'inode': db_identity[1]}
    def launch_gui():
        log = tempfile.TemporaryFile(dir=fixture.root / 'tmp')
        gui_logs.append(log)
        child = fixture.spawn([str(gui), '--app-data', str(data)], stdin=subprocess.DEVNULL,
                              stdout=log, stderr=log)
        children.append(child)
        identity = register_owned_instance(child, gui)
        gui_identities[child.pid] = {**identity, 'kernel_start': kernel_start(h, child.pid)}
        time.sleep(1)
        if h.child_exited(child):
            fixture.reap(child)
            receipt['gui_exit_code'] = child.returncode
            stage('owned_gui_exited', exit_code=child.returncode)
            raise h.GateFailure('gui_exited_before_observation')
        return child
    gui_child = launch_gui()
    stage('gui_ready', app_path=str(app), gui_pid=gui_child.pid, core_pid=first['core_pid'], engine_pid=first['engine_pid'], private_data=str(data))
    checkpoint('fixture-visible')
    receipt['gates']['actual_fixture_gui_loaded'] = 'pass'
    api('POST', f'/conversations/{room}/events', {'content': 'Task8 first delayed fixture job'})
    api('POST', f'/conversations/{room}/events', {'content': 'Task8 queued delayed fixture job'})
    job_deadline = time.monotonic() + 55
    def states():
        with sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=1) as db:
            return [row[0] for row in db.execute('SELECT state FROM room_triggers WHERE room_id = ? ORDER BY created_at', (room,))]
    deadline = time.monotonic() + 3
    while sorted(states()) != ['pending', 'running'] and time.monotonic() < deadline:
        time.sleep(.05)
    h.require(sorted(states()) == ['pending', 'running'], 'running_pending_queue_failed')
    receipt['queue_before_gui_close'] = ['pending', 'running']
    stage('jobs_running_pending', gui_pid=gui_child.pid, core_pid=first['core_pid'], engine_pid=first['engine_pid'])
    receipt.setdefault('gui_closes', []).append(polite_close(h, fixture, gui_child, gui_logs[-1],
        gui_identities[gui_child.pid], ROOT / 'scripts/gui_gate_native_quit.swift',
        phase='gui-closed', exit_seconds=18, attest=attest_instance,
        secret_values=(password, token, session, endpoint['capability'])))
    attest_instance('gui_closed_normal_exit')
    health = control('health')
    h.require(health['core_pid'] == first['core_pid'] and health['engine_pid'] == first['engine_pid'], 'gui_close_runtime_changed')
    h.require((db_path.stat().st_dev, db_path.stat().st_ino) == db_identity, 'gui_close_database_changed')
    h.require(api('GET', f'/conversations/{room}/schedule') == limits, 'gui_close_schedule_changed')
    receipt['gates']['actual_gui_close_same_core_engine_database'] = 'pass'
    stage('gui_closed_core_persistent', states=states())
    while time.monotonic() < job_deadline and states() != ['done', 'done']:
        time.sleep(.25)
    h.require(states() == ['done', 'done'], 'queued_jobs_timeout')
    events = api('GET', f'/conversations/{room}/events')
    h.require(sum(event['actor_type'] == 'agent' and event['kind'] == 'message' for event in events) == 2, 'job_replies_missing')
    receipt['gates']['jobs_and_durable_queue_complete_without_gui'] = 'pass'
    receipt['queue_after_gui_close'] = ['done', 'done']
    gui_child = launch_gui()
    stage('gui_reopened', app_path=str(app), gui_pid=gui_child.pid, core_pid=first['core_pid'], engine_pid=first['engine_pid'])
    checkpoint('reopened-visible')
    receipt['gates']['actual_gui_reopen_fixture_account'] = 'pass'
    receipt.setdefault('gui_closes', []).append(polite_close(h, fixture, gui_child, gui_logs[-1],
        gui_identities[gui_child.pid], ROOT / 'scripts/gui_gate_native_quit.swift',
        phase='reopened-closed', exit_seconds=30, attest=attest_instance,
        secret_values=(password, token, session, endpoint['capability'])))
    attest_instance('reopened_closed_normal_exit')
    fixture.command([*words, 'stop'])
    fixture.reap(core_child, grace=8)
    core_child, second = launch_core()
    register_owned_instance(core_child, core)
    session = control('create_ui_session')['session_token']
    headers['X-Gamja-Session'] = session
    headers.pop('Authorization')
    headers['Authorization'] = 'Bearer ' + api('POST', '/auth/login', {'email': account['email'], 'password': password})['access_token']
    h.require(api('GET', f'/conversations/{room}/schedule') == limits and states() == ['done', 'done'], 'restart_schedule_queue_failed')
    receipt['gates']['core_restart_schedule_limits_and_queue_preserved'] = 'pass'
    fixture.command([*words, 'stop'])
    fixture.reap(core_child, grace=8)
    receipt['gates']['public_explicit_stop'] = 'pass'
    attest_instance('final_gate_acceptance')
    receipt['gates']['process_instance_isolation'] = 'pass'
    receipt['status'] = 'passed_scoped_gui_gate'
except h.GateFailure as error:
    receipt['failure'] = str(error)
except KeyboardInterrupt:
    receipt['failure'] = 'operator_interrupted'
except Exception:
    receipt['failure'] = 'operator_execution_failed'
finally:
    with cleanup_signal_shield(receipt) as commit_receipt:
        def retain_owned_logs(phase):
            for index, (log, child) in enumerate(zip(gui_logs, children)):
                try:
                    # Only this controller's Popen stream, never global/Owner logs.
                    secret_values = tuple(globals().get(key) for key in ('password', 'token', 'session'))
                    if isinstance(endpoint, dict):
                        secret_values += (endpoint.get('capability'),)
                    value = snapshot_gui_diagnostics(log, exit_code=child.returncode, secret_values=secret_values)
                    destination = RECEIPT.with_name(RECEIPT.stem + f'-gui-{index}-{phase}.json')
                    h.write_receipt(destination, value)
                except Exception:
                    receipt['failure'] = receipt['failure'] or 'gui_diagnostics_retention_failed'
        # Preserve an initial signature even if process cleanup itself fails.
        if gui_logs:
            retain_owned_logs('before-cleanup')
        if fixture is not None:
            try:
                fixture.__exit__(None, None, None)
            except Exception:
                receipt['failure'] = receipt['failure'] or 'fixture_cleanup_failed'
            receipt['cleanup'].update(fixture.cleanup)
            receipt['resources'] = fixture.resources
        # The reviewed sole final reap has supplied the actual exit code now; FD
        # remains readable after its private root is removed. Persist before close.
        if gui_logs:
            retain_owned_logs('after-cleanup')
        if mount is not None:
            # Also attempt detach after a timed-out attach; mount completion may race.
            if mounted or os.path.ismount(mount):
                try:
                    checked(['/usr/bin/hdiutil', 'detach', str(mount)], timeout=30)
                    mounted = False
                except Exception:
                    receipt['failure'] = receipt['failure'] or 'owned_mount_detach_failed'
            receipt['cleanup']['owned_mount_detached'] = not mounted and not os.path.ismount(mount)
            if receipt['cleanup']['owned_mount_detached']:
                shutil.rmtree(mount)
            receipt['cleanup']['owned_mount_root_removed'] = not mount.exists()
        for log in gui_logs:
            log.close()
        if not receipt['cleanup'] or not all(receipt['cleanup'].values()):
            receipt['failure'] = receipt['failure'] or 'cleanup_incomplete'
        if receipt['failure']:
            receipt['status'] = 'failed'
        commit_receipt(h, RECEIPT)
        stage('finished', status=receipt['status'], failure=receipt['failure'], cleanup=receipt['cleanup'])
    signal.signal(signal.SIGTERM, previous_term)
sys.exit(0 if receipt['status'] == 'passed_scoped_gui_gate' else 1)
