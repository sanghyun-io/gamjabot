import os
import json
import socket
import subprocess
import sys


PREFIX = b"GAMJA_DIAGNOSTIC_V1:"


def socket_output(channel, script):
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        stdout=channel, stderr=subprocess.DEVNULL, check=True, timeout=10,
    )


def test_native_unix_stream_transports_code_and_detail_without_closing_stdout():
    reader, writer = socket.socketpair()
    with reader, writer:
        socket_output(writer, '''
from app.services.diagnostics import emit_diagnostic_failure, emit_detailed_diagnostic
emit_diagnostic_failure("backend_init_failed", "startup")
emit_detailed_diagnostic({"failure_code": "provider_unavailable"})
emit_diagnostic_failure("backend_runtime_failed", "runtime")
''')
        writer.close()
        reader.settimeout(2)
        output = b""
        while chunk := reader.recv(4096):
            output += chunk
    lines = output.splitlines()
    assert lines[0] == PREFIX + b'{"code":"backend_init_failed","stage":"startup"}'
    assert lines[1].startswith(b"GAMJA_DIAGNOSTIC_V2:")
    assert json.loads(lines[1].split(b":", 1)[1]) == {
        "code": "backend_runtime_failed", "stage": "runtime",
        "diagnostic": {"failure_code": "provider_unavailable"},
    }
    assert lines[2] == PREFIX + b'{"code":"backend_runtime_failed","stage":"runtime"}'


def test_unknown_code_is_silent_on_native_unix_stream():
    reader, writer = socket.socketpair()
    with reader, writer:
        socket_output(writer, '''
from app.services.diagnostics import emit_diagnostic_failure
emit_diagnostic_failure("private-api-key", "startup")
''')
        writer.close()
        reader.settimeout(2)
        assert reader.recv(4096) == b""


def test_datagram_stdout_cannot_transport_diagnostics():
    reader, writer = socket.socketpair(type=socket.SOCK_DGRAM)
    with reader, writer:
        socket_output(writer, '''
from app.services.diagnostics import emit_diagnostic_failure, emit_detailed_diagnostic
emit_diagnostic_failure("backend_init_failed", "startup")
emit_detailed_diagnostic({"failure_code": "provider_unavailable"})
''')
        reader.setblocking(False)
        try:
            output = reader.recv(4096)
        except BlockingIOError:
            output = b""
        assert output == b""


def test_tcp_stdout_cannot_transport_diagnostics():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.create_connection(listener.getsockname()) as writer:
            reader, _ = listener.accept()
            with reader:
                socket_output(writer, '''
from app.services.diagnostics import emit_diagnostic_failure, emit_detailed_diagnostic
emit_diagnostic_failure("backend_init_failed", "startup")
emit_detailed_diagnostic({"failure_code": "provider_unavailable"})
''')
                writer.close()
                reader.settimeout(2)
                assert reader.recv(4096) == b""


def call_helper(code, stage):
    script = ("from app.services.diagnostics import emit_diagnostic_failure; "
              f"emit_diagnostic_failure({code!r}, {stage!r})")
    return subprocess.run([sys.executable, "-c", script], cwd=os.path.join(os.path.dirname(__file__), ".."),
                          capture_output=True, timeout=10)


def test_emits_only_fixed_fields_to_pipe():
    output = call_helper("backend_init_failed", "startup").stdout
    assert output == PREFIX + b'{"code":"backend_init_failed","stage":"startup"}\n'


def test_rejects_unknown_or_injected_values():
    assert call_helper("backend_init_failed /Users/alice/token", "startup").stdout == b""
    assert call_helper("backend_init_failed", "startup\nSECRET").stdout == b""


def test_pre_app_import_failure_emits_code_without_traceback_on_stdout(tmp_path):
    # Invalid port fails before app.main imports, with a path/secret in the environment.
    env = os.environ.copy()
    env.update({
        "GAMJA_LOCAL_SESSION_TOKEN": "SENSITIVE-SECRET-" * 3,
        "GAMJA_APP_DATA_DIR": str(tmp_path / "private-data"),
        "GAMJA_PORT": "not-a-port",
    })
    result = subprocess.run(
        [sys.executable, "run.py"], cwd=os.path.join(os.path.dirname(__file__), ".."),
        env=env, capture_output=True, timeout=10,
    )
    assert result.returncode != 0
    assert result.stdout == PREFIX + b'{"code":"backend_init_failed","stage":"startup"}\n'
    assert b"SENSITIVE-SECRET" not in result.stdout
    assert str(tmp_path).encode() not in result.stdout


def test_no_pipe_is_noop(tmp_path):
    output = tmp_path / "stdout.txt"
    with output.open("wb") as file:
        subprocess.run([sys.executable, "-c", "from app.services.diagnostics import emit_diagnostic_failure; emit_diagnostic_failure('backend_init_failed', 'startup')"],
                       cwd=os.path.join(os.path.dirname(__file__), ".."), stdout=file, check=True)
    assert output.read_bytes() == b""


def test_settings_import_failure_still_emits_code(tmp_path):
    env = os.environ.copy()
    env.update({
        "GAMJA_LOCAL_SESSION_TOKEN": "s" * 32,
        "GAMJA_APP_DATA_DIR": str(tmp_path / "private-data"),
        "GAMJA_PORT": "8765",
        "DEBUG": "not-a-boolean",
    })
    result = subprocess.run([sys.executable, "run.py"],
                            cwd=os.path.join(os.path.dirname(__file__), ".."),
                            env=env, capture_output=True, timeout=10)
    assert result.returncode != 0
    assert result.stdout == PREFIX + b'{"code":"backend_init_failed","stage":"startup"}\n'
    assert b"private-data" not in result.stdout


def test_live_request_failure_emits_one_code_only_event_and_service_survives(tmp_path):
    secret = "SECRET-token-/Users/alice/private.txt"
    database_url = f"sqlite:///{tmp_path / 'runtime.sqlite'}"
    script = f'''
from fastapi.testclient import TestClient
from app.main import create_app

app = create_app(database_url={database_url!r})
def fail():
    raise RuntimeError({secret!r})
app.add_api_route("/boom", fail)
def number(value: int):
    return {{"value": value}}
app.add_api_route("/number/{{value}}", number, methods=["GET"])
with TestClient(app, raise_server_exceptions=False, headers={{"X-Gamja-Session": "test-only-token"}}) as client:
    assert client.get("/boom").status_code == 500
    assert client.get("/health").json() == {{"status": "healthy"}}
    assert client.get("/missing").status_code == 404
    assert client.get("/number/not-an-int").status_code == 422
'''
    env = os.environ.copy()
    env.update({"PYTHONPATH": os.path.join(os.path.dirname(__file__), ".."),
                "GAMJA_LOCAL_SESSION_TOKEN": "test-only-token"})
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path,
                            env=env, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout == PREFIX + b'{"code":"backend_runtime_failed","stage":"runtime"}\n'
    assert secret.encode() not in result.stdout
