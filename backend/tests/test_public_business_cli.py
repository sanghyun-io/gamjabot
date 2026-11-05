"""Public Rust CLI -> real engine CLI -> local canonical API, synthetic adapters only."""
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic, sleep

import httpx
import pytest
from fastapi.testclient import TestClient
from app import owner_cli
from app.main import RuntimeConfig, create_app
from app.services.providers import ProviderResult, ToolCall
from test_connections import FakeSecrets

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / 'core-host/target/debug/gamja'


class ExactRequestClient:
    def __init__(self, inspections):
        self.inspections = iter(inspections)
        self.calls = []

    def call(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body, kwargs))
        if method == 'POST':
            return {'event_id': '11111111-1111-4111-8111-111111111111', 'seq': 7}
        return next(self.inspections)


def inspection(state='running'):
    return {'version': 1, 'request': {'event_id': '11111111-1111-4111-8111-111111111111'},
            'completion': {'state': state, 'outcome_status': 'pending' if state == 'running' else 'succeeded',
                           'result_available': state == 'completed', 'completed_rounds': int(state == 'completed'),
                           'pending_agent_count': int(state == 'running')}}


def test_exact_request_run_emits_uuid_before_polling_and_reuses_exact_request(monkeypatch, capsys):
    client = ExactRequestClient([inspection('running'), inspection('completed')])
    monkeypatch.setattr(owner_cli.sys, 'stdin', io.StringIO('decide together\n'))
    monkeypatch.setattr(owner_cli.time, 'sleep', lambda _seconds: None)
    owner_cli.command(client, ['run', '5', '--stdin', '--idempotency-key', 'stable-key', '--timeout', '10'])
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[0] == {'request_uuid': '11111111-1111-4111-8111-111111111111'}
    assert output[1]['completion']['state'] == 'completed'
    assert client.calls[0] == ('POST', '/conversations/5/events', {'content': 'decide together\n'},
                               {'authenticated': True, 'idempotency_key': 'stable-key'})
    assert [call[1] for call in client.calls[1:]] == [
        '/conversations/5/requests/11111111-1111-4111-8111-111111111111',
        '/conversations/5/requests/11111111-1111-4111-8111-111111111111',
    ]


def test_exact_request_wait_timeout_is_local_and_ctrl_c_detaches_without_mutation(monkeypatch, capsys):
    request_id = '11111111-1111-4111-8111-111111111111'
    timeout_client = ExactRequestClient([inspection('running')])
    ticks = iter((0.0, 2.0))
    monkeypatch.setattr(owner_cli.time, 'monotonic', lambda: next(ticks))
    owner_cli.command(timeout_client, ['request', 'wait', '5', request_id, '--timeout', '1'])
    timed_out = json.loads(capsys.readouterr().out)
    assert timed_out['wait_status'] == 'timed_out'
    assert timed_out['completion']['state'] == 'running'
    assert timeout_client.calls == [('GET', f'/conversations/5/requests/{request_id}', None,
                                     {'authenticated': True})]

    detached_client = ExactRequestClient([inspection('running')])
    monkeypatch.setattr(owner_cli.time, 'monotonic', lambda: 0.0)
    monkeypatch.setattr(owner_cli.time, 'sleep', lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
    owner_cli.command(detached_client, ['request', 'wait', '5', request_id, '--timeout', '10'])
    detached = json.loads(capsys.readouterr().out)
    assert detached['wait_status'] == 'detached'
    assert detached['completion']['state'] == 'running'
    assert all(call[0] == 'GET' for call in detached_client.calls)


def test_request_finalize_cli_forwards_exact_preview_and_required_idempotent_execute(capsys):
    class Client:
        def __init__(self): self.calls = []
        def call(self, method, path, body=None, **kwargs):
            self.calls.append((method, path, body, kwargs)); return {"ok": True}
    client = Client()
    request_id = '11111111-1111-4111-8111-111111111111'
    owner_cli.command(client, ['request', 'finalize-preview', '5', request_id, '7'])
    owner_cli.command(client, ['request', 'finalize', '5', request_id, '7', 'a' * 64,
                               '--idempotency-key', 'finalize-key'])
    assert client.calls == [
        ('POST', f'/conversations/5/requests/{request_id}/finalize/preview',
         {'agent_id': 7}, {'authenticated': True}),
        ('POST', f'/conversations/5/requests/{request_id}/finalize',
         {'agent_id': 7, 'plan_hash': 'a' * 64},
         {'authenticated': True, 'idempotency_key': 'finalize-key'}),
    ]
    with pytest.raises(RuntimeError):
        owner_cli.command(client, ['request', 'finalize', '5', request_id, '7', 'a' * 64])


@pytest.mark.parametrize('words', [
    ['run', '5', '--stdin', '--idempotency-key', 'key'],
    ['run', '5', '--stdin', '--timeout', '1'],
    ['run', '5', '--idempotency-key', 'key', '--timeout', '1'],
    ['run', '5', '--stdin', '--idempotency-key', 'key', '--timeout', '0'],
    ['run', '5', '--stdin', '--idempotency-key', 'key', '--timeout', '3601'],
    ['request', 'wait', '5', 'NOT-A-UUID', '--timeout', '1'],
])
def test_exact_request_cli_rejects_ambiguous_or_unbounded_input(monkeypatch, words):
    monkeypatch.setattr(owner_cli.sys, 'stdin', io.StringIO('text'))
    with pytest.raises(RuntimeError):
        owner_cli.command(ExactRequestClient([]), words)


def test_provider_operation_source_cli_preserves_exact_restored_state_and_safe_error(capsys):
    operation_id = 'e' * 64
    restored = {
        'version': 1, 'operation_id': operation_id, 'state': 'interrupted',
        'result': {'credential_state': 'locked', 'revoke_state': 'skipped'},
        'error_code': 'operation_interrupted',
    }
    client = ExactRequestClient([restored])

    owner_cli.command(client, ['provider', 'auth', 'status', '9', operation_id])

    assert client.calls == [
        ('GET', f'/connections/9/auth/operations/{operation_id}', None,
         {'authenticated': True}),
    ]
    assert json.loads(capsys.readouterr().out) == restored


def test_provider_auth_source_cli_preserves_present_success_without_calling_test(capsys):
    operation_id = 'f' * 64
    success = {
        'version': 1, 'operation_id': operation_id, 'state': 'succeeded',
        'result': {'credential_state': 'present'},
    }
    client = ExactRequestClient([success])

    owner_cli.command(client, ['provider', 'auth', 'status', '9', operation_id])

    assert client.calls == [
        ('GET', f'/connections/9/auth/operations/{operation_id}', None,
         {'authenticated': True}),
    ]
    assert json.loads(capsys.readouterr().out) == success


def test_setup_cli_calls_only_canonical_connection_test_and_starter_apis(tmp_path, capsys, monkeypatch):
    class Client:
        def __init__(self): self.calls = []
        def call(self, method, path, body=None, **kwargs):
            self.calls.append((method, path, body, kwargs))
            return {"ok": True}
    client = Client()
    public_connection = {"kind": "openai", "name": "First provider", "default_model_id": "m"}
    path = tmp_path / "connection.json"; path.write_text(json.dumps(public_connection))
    monkeypatch.setattr(owner_cli, "read_secret", lambda *_args, **_kwargs: "synthetic-secret")
    owner_cli.command(client, ["setup", "connection-add", "--json", str(path), "--secret"])
    intent = {"project_title": "First", "room_title": "Decision", "agents": [
        {"role": "proposer", "name": "P", "connection_id": 4, "model_id": "m"},
        {"role": "reviewer", "name": "R", "connection_id": 4, "model_id": "m"}]}
    path = tmp_path / "intent.json"; path.write_text(json.dumps(intent))
    owner_cli.command(client, ["setup", "preview", "--json", str(path)])
    owner_cli.command(client, ["setup", "test", "4", "m"])
    applied = {**intent, "plan_hash": "a" * 64}; path.write_text(json.dumps(applied))
    owner_cli.command(client, ["setup", "apply", "--json", str(path), "--idempotency-key", "stable"])
    owner_cli.command(client, ["setup", "last-result"])
    assert client.calls == [
        ("POST", "/connections", {**public_connection, "api_key": "synthetic-secret"}, {"authenticated": True}),
        ("POST", "/starter/preview", intent, {"authenticated": True}),
        ("GET", "/connections/4/test-preview?model_id=m&profile=text", None, {"authenticated": True}),
        ("POST", "/connections/4/test?model_id=m&profile=text", {}, {"authenticated": True}),
        ("POST", "/starter/apply", applied, {"authenticated": True, "idempotency_key": "stable"}),
        ("GET", "/starter/last-result", None, {"authenticated": True}),
    ]


@pytest.mark.parametrize("model_id,encoded", [
    ("vendor/exact-model", "vendor%2Fexact-model"),
    (
        "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.amazon.nova-pro-v1:0",
        "arn%3Aaws%3Abedrock%3Aus-east-1%3A123456789012%3Ainference-profile%2Fus.amazon.nova-pro-v1%3A0",
    ),
])
def test_setup_test_cli_forwards_canonical_slash_model_ids(model_id, encoded, capsys):
    from app.schemas.provider_models import valid_model_id

    assert valid_model_id(model_id)
    client = ExactRequestClient([{"ok": True}, {"ok": True}])

    owner_cli.command(client, ["setup", "test", "4", model_id])

    assert client.calls == [
        ("GET", f"/connections/4/test-preview?model_id={encoded}&profile=text", None,
         {"authenticated": True}),
        ("POST", f"/connections/4/test?model_id={encoded}&profile=text", {},
         {"authenticated": True}),
    ]
    assert len(capsys.readouterr().out.splitlines()) == 2


def invoke(root, words, stdin=''):
    assert BINARY.is_file(), 'Build the public source CLI before this gate'
    command = [str(BINARY), '--app-data', str(root), '--engine', sys.executable,
               '--engine-arg', str(ROOT / 'backend/run.py'), *words]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, start_new_session=True) as child:
        try:
            out, err = child.communicate(stdin, timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            raise
    assert 'synthetic-password' not in out + err
    assert 'synthetic-provider-secret' not in out + err
    return child.returncode, out, err


@pytest.mark.parametrize('platform,environment,suffix', [
    ('darwin', {}, 'Library/Application Support/top.gamja.bot/owner-cli.json'),
    ('linux', {}, '.local/share/top.gamja.bot/owner-cli.json'),
    ('win32', {}, 'AppData/Roaming/top.gamja.bot/owner-cli.json'),
    ('linux', {'XDG_DATA_HOME': 'relative-unsafe'}, '.local/share/top.gamja.bot/owner-cli.json'),
    ('win32', {'APPDATA': 'relative-unsafe'}, 'AppData/Roaming/top.gamja.bot/owner-cli.json'),
])
def test_direct_engine_default_uses_safe_platform_data_directory(monkeypatch, tmp_path, platform, environment, suffix):
    monkeypatch.setattr(owner_cli.sys, 'platform', platform)
    monkeypatch.setattr(owner_cli.Path, 'home', lambda: tmp_path)
    monkeypatch.delenv('APPDATA', raising=False)
    monkeypatch.delenv('XDG_DATA_HOME', raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert owner_cli._default_capability() == tmp_path / suffix


@pytest.mark.parametrize('platform,name', [('linux', 'XDG_DATA_HOME'), ('win32', 'APPDATA')])
def test_direct_engine_default_accepts_absolute_os_data_root(monkeypatch, tmp_path, platform, name):
    monkeypatch.setattr(owner_cli.sys, 'platform', platform)
    monkeypatch.setenv(name, str(tmp_path))
    assert owner_cli._default_capability() == tmp_path / 'top.gamja.bot/owner-cli.json'


@pytest.mark.skipif(os.name != 'posix', reason='POSIX source cleanup; native Windows is a separate gate')
def test_public_cli_bootstraps_canonical_job_approval_and_stop(sqlite_url, tmp_path):
    credential = 'a' * 64
    app = create_app(runtime_config=RuntimeConfig(database_url=sqlite_url,
        cli_capability_hash=hashlib.sha256(credential.encode()).hexdigest()))
    with TestClient(app) as api:
        app.state.secret_store = FakeSecrets()
        def wire(request):
            body = json.loads(request.content)
            assert request.headers['authorization'] == 'Bearer synthetic-provider-secret'
            message = {'content': 'OK'}
            if body.get('tools') and not any(m.get('role') == 'tool' for m in body['messages']):
                message = {'content': None, 'tool_calls': [{'id': 'probe', 'type': 'function',
                    'function': {'name': body['tools'][0]['function']['name'], 'arguments': '{}'}}]}
            return httpx.Response(200, json={'choices': [{'message': message}]})
        app.state.connection_transport = httpx.MockTransport(wire)
        executed = []
        async def provider(request):
            if not request.observations:
                return ProviderResult(tool_calls=[ToolCall('synthetic-call', 'read_file', {'path': 'synthetic.txt'})],
                    continuation={'call_ids': ['synthetic-call']})
            return ProviderResult(text='synthetic complete')
        async def executor(action, grant, cancel):
            executed.append(action.action_id)
            return 'synthetic observation'
        app.state.scheduler.provider = provider
        app.state.scheduler.broker.executor = executor
        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                response = api.request(self.command, self.path, headers=dict(self.headers),
                    content=self.rfile.read(int(self.headers.get('Content-Length', 0))))
                self.send_response(response.status_code)
                self.send_header('Content-Length', str(len(response.content)))
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(response.content)
            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_request
            def log_message(self, *_): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            endpoint = tmp_path / 'owner-cli.json'
            endpoint.write_text(json.dumps({'version': 1, 'api_url': f'http://127.0.0.1:{port}',
                'control_url': f'http://127.0.0.1:{port}', 'capability': credential, 'core_version': 1, 'engine_version': 1}))
            endpoint.chmod(0o600)
            def batch(rows, secrets='synthetic-password\n', alias=False):
                source = tmp_path / 'commands.ndjson'
                source.write_text('\n'.join(json.dumps(row) for row in rows))
                code, out, err = invoke(tmp_path, ['batch' if alias else '--batch', str(source)], secrets)
                assert code == 0, err
                assert credential not in out + err
                return [json.loads(line) for line in out.splitlines()]
            def row(words, body=None):
                return {'command': words, **({'input': body} if body is not None else {})}
            login = row(['login', '--secret-stdin'], {'email': 'public-cli@example.com'})
            outputs = batch([
                row(['register', '--secret-stdin'], {'email': 'public-cli@example.com', 'username': 'Public'}), login,
                row(['provider', 'add', '--secret-stdin'], {'provider_kind': 'compatible', 'label': 'Synthetic',
                    'base_url': 'http://127.0.0.1:8765/v1', 'allow_local_http': True,
                    'default_model_id': 'exact', 'capabilities': {'tools': True}}),
                row(['provider', 'test', '1', 'exact']),
                row(['agent', 'add'], {'name': 'Ada', 'description': 'Synthetic', 'system_prompt': 'Work safely',
                    'connection_id': 1, 'model_id': 'exact'}),
                row(['project', 'add'], {'name': 'Public fixture'}),
                row(['project-agent', 'add', '1', '1']),
                row(['room', 'add'], {'project_id': 1, 'title': 'Public room'}),
                row(['member-add', '1', '1']),
                row(['room-settings', 'set', '1'], {'max_rounds': 1, 'max_tool_calls': 2, 'max_executor_seconds': 3}),
                row(['send', '1'], {'content': 'Inspect synthetic data'}), row(['wait', '1', '1']),
            ], 'synthetic-password\nsynthetic-password\nsynthetic-provider-secret\n', alias=True)
            assert outputs[5]['name'] == 'Public fixture'
            waiting = next(item for output in outputs if isinstance(output, dict) and 'activity' in output
                           for item in output['activity'] if item['state'] == 'waiting')
            assert not executed
            batch([login, row(['preview', '1', waiting['action_id']]), row(['decide', '1', waiting['action_id'], 'approve'])])
            deadline = monotonic() + 3
            while not executed:
                assert monotonic() < deadline
                sleep(.02)
            assert executed == [waiting['action_id']]
            assert batch([login, row(['room', 'stop', '1']), row(['logout'])])[1] is None
            for words in ([], ['configure']):
                code, out, err = invoke(tmp_path, words, 'status\nquit\n')
                assert code == 0 and 'healthy' in out and endpoint.is_file()
            code, out, err = invoke(tmp_path, ['api-status'])
            assert code == 0 and json.loads(out)['status'] == 'healthy'
            code, out, err = invoke(tmp_path, ['send', '1', '--stdin'], 'synthetic public text')
            assert code == 1 and 'Run login or restore first' in err  # JWT never crosses processes.
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            assert not thread.is_alive()


def test_private_engine_global_options_require_exact_names(monkeypatch, capsys):
    monkeypatch.setattr(owner_cli.sys, 'argv', ['engine', '--capability-f', 'synthetic-global-secret'])
    monkeypatch.setattr(owner_cli, 'capability', lambda _: pytest.fail('Rejected parser arguments must not read a capability'))
    with pytest.raises(SystemExit) as error:
        owner_cli.main()
    assert error.value.code == 2
    assert 'synthetic-global-secret' not in capsys.readouterr().err
