"""Memory-boundary fixtures only; Mac stubs do not verify Windows DACLs."""
import ctypes
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from app import owner_cli


ENDPOINT = {'version': 1, 'api_url': 'http://127.0.0.1:41001',
    'control_url': 'http://127.0.0.1:41002', 'capability': 'a' * 64,
    'core_version': 1, 'engine_version': 1}


class StubReader:
    def __init__(self, payload=None, code=0, written=None, terminate=True):
        self.payload = json.dumps(ENDPOINT).encode() if payload is None else payload
        self.code, self.length, self.terminate = code, written, terminate
        self.calls = []

    def __call__(self, path, path_units, output, capacity, written):
        self.calls.append((path, path_units, output, capacity, written))
        self.directory = bytes(ctypes.string_at(path, path_units * 2)).decode('utf-16-le')
        count = len(self.payload) if self.length is None else self.length
        ctypes.memmove(output, self.payload, min(len(self.payload), capacity))
        if not self.terminate and count < capacity:
            output[count] = 65
        ctypes.cast(written, ctypes.POINTER(ctypes.c_size_t))[0] = count
        return self.code


def fixture_dll(tmp_path, monkeypatch, reader):
    dll = tmp_path / 'gamja_core_host.dll'
    dll.touch()
    loads = []
    def load(path, **kwargs):
        loads.append((path, kwargs))
        return SimpleNamespace(gamja_core_read_endpoint=reader)
    monkeypatch.setattr(ctypes, 'CDLL', load)
    return dll, loads


def test_windows_memory_reader_uses_bounded_cdecl_and_wipes_buffers(tmp_path, monkeypatch, capsys):
    reader = StubReader()
    dll, loads = fixture_dll(tmp_path, monkeypatch, reader)
    path = tmp_path / 'owner-cli.json'
    result = owner_cli._windows_capability(path, _fixture_dll=dll)
    assert result == {**ENDPOINT, 'base_url': ENDPOINT['api_url']}
    assert loads == [(str(dll), {'winmode': 0x900})]
    assert reader.directory == str(tmp_path)
    assert reader.argtypes == [ctypes.POINTER(ctypes.c_uint16), ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    assert reader.restype is ctypes.c_uint32
    _, units, output, capacity, _ = reader.calls[0]
    assert units == len(str(tmp_path).encode('utf-16-le')) // 2
    assert capacity == 4097 and bytes(output) == b'\0' * capacity
    assert not capsys.readouterr().out
    assert not path.exists()  # No Python endpoint read fallback.


@pytest.mark.parametrize('payload', [
    b'not-json sentinel-secret', b'[]', b'null', b'{"version":1,"version":1}',
    json.dumps({'base_url': ENDPOINT['api_url'], 'capability': ENDPOINT['capability']}).encode(),
    json.dumps({**ENDPOINT, 'extra': 'sentinel-secret'}).encode(),
    json.dumps({**ENDPOINT, 'version': True}).encode(),
    json.dumps({**ENDPOINT, 'api_url': 'https://example.com'}).encode(),
    json.dumps({**ENDPOINT, 'capability': 'sentinel-secret'}).encode(),
    json.dumps({**ENDPOINT, 'core_version': 99}).encode(),
    json.dumps({**ENDPOINT, 'api_url': '\nhttp://127.0.0.1:41001'}).encode(),
    json.dumps({**ENDPOINT, 'capability': 'A' * 64}).encode(),
    b'\xff', json.dumps(ENDPOINT).encode() + b'\0extra',
])
def test_windows_reader_revalidates_exact_versioned_json_schema(tmp_path, monkeypatch, capsys, payload):
    reader = StubReader(payload=payload)
    dll, _ = fixture_dll(tmp_path, monkeypatch, reader)
    with pytest.raises(owner_cli.EndpointReadFailure) as failure:
        owner_cli._windows_capability(tmp_path / 'owner-cli.json', _fixture_dll=dll)
    assert failure.value.code == 'invalid_endpoint'
    assert 'sentinel-secret' not in str(failure.value)
    assert bytes(reader.calls[0][2]) == b'\0' * 4097
    output = capsys.readouterr()
    assert not output.out and not output.err


@pytest.mark.parametrize('code,expected', [
    (1, 'invalid_endpoint_request'), (2, 'endpoint_unavailable'),
    (3, 'invalid_endpoint'), (4, 'endpoint_buffer_unavailable'),
    (5, 'endpoint_reader_unavailable'), (999, 'endpoint_reader_unavailable'),
])
def test_windows_reader_maps_only_fixed_numeric_errors_and_wipes(tmp_path, monkeypatch, code, expected):
    reader = StubReader(payload=b'sentinel-secret', code=code)
    dll, _ = fixture_dll(tmp_path, monkeypatch, reader)
    with pytest.raises(owner_cli.EndpointReadFailure) as failure:
        owner_cli._windows_capability(tmp_path / 'owner-cli.json', _fixture_dll=dll)
    assert failure.value.code == expected
    assert 'sentinel-secret' not in str(failure.value)
    assert bytes(reader.calls[0][2]) == b'\0' * 4097


@pytest.mark.parametrize('count,terminate', [(0, True), (4097, True), (100000, True), (None, False)])
def test_windows_reader_rejects_invalid_lengths_or_missing_nul(tmp_path, monkeypatch, count, terminate):
    reader = StubReader(written=count, terminate=terminate)
    dll, _ = fixture_dll(tmp_path, monkeypatch, reader)
    with pytest.raises(owner_cli.EndpointReadFailure) as failure:
        owner_cli._windows_capability(tmp_path / 'owner-cli.json', _fixture_dll=dll)
    assert failure.value.code == 'invalid_endpoint'
    assert bytes(reader.calls[0][2]) == b'\0' * 4097


@pytest.mark.parametrize('problem', ['missing', 'load', 'export'])
def test_windows_missing_or_bad_dll_fails_without_unverified_file_read(tmp_path, monkeypatch, problem):
    path = tmp_path / 'owner-cli.json'
    path.write_text(json.dumps(ENDPOINT))
    dll = tmp_path / 'gamja_core_host.dll'
    if problem != 'missing':
        dll.touch()
    def load(*_a, **_k):
        if problem == 'load':
            raise OSError('sentinel-secret path detail')
        return SimpleNamespace()
    monkeypatch.setattr(ctypes, 'CDLL', load)
    monkeypatch.setattr(owner_cli.os, 'open', lambda *_a, **_k: pytest.fail('no POSIX/plain read fallback'))
    with pytest.raises(owner_cli.EndpointReadFailure) as failure:
        owner_cli._windows_capability(path, _fixture_dll=dll)
    assert failure.value.code == 'endpoint_reader_unavailable'
    assert 'sentinel-secret' not in str(failure.value)


def test_frozen_windows_loads_only_absolute_engine_neighbor(tmp_path, monkeypatch):
    engine = tmp_path / 'gamja-engine.exe'
    engine.touch()
    reader = StubReader()
    dll, loads = fixture_dll(tmp_path, monkeypatch, reader)
    monkeypatch.setattr(owner_cli.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(owner_cli.sys, 'executable', str(engine))
    assert owner_cli._windows_capability(tmp_path / 'owner-cli.json')['api_url'] == ENDPOINT['api_url']
    assert loads[0][0] == str(dll)
    with pytest.raises(owner_cli.EndpointReadFailure):
        owner_cli._windows_capability(tmp_path / 'owner-cli.json', _fixture_dll=dll)


@pytest.mark.parametrize('path_name', ['other.json', 'nested\0owner-cli.json'])
def test_windows_reader_rejects_bad_path_before_ffi(tmp_path, monkeypatch, path_name):
    reader = StubReader()
    dll, loads = fixture_dll(tmp_path, monkeypatch, reader)
    with pytest.raises(owner_cli.EndpointReadFailure):
        owner_cli._windows_capability(tmp_path / path_name, _fixture_dll=dll)
    assert not reader.calls and not loads


def test_windows_reader_has_no_public_source_or_environment_dll_override(tmp_path, monkeypatch):
    dll = tmp_path / 'gamja_core_host.dll'
    dll.touch()
    monkeypatch.setenv('GAMJA_CORE_HOST_DLL', str(dll))
    monkeypatch.setattr(owner_cli.sys, 'frozen', False, raising=False)
    monkeypatch.setattr(ctypes, 'CDLL', lambda *_a, **_k: pytest.fail('source CLI must not load an implicit DLL'))
    with pytest.raises(owner_cli.EndpointReadFailure) as failure:
        owner_cli._windows_capability(tmp_path / 'owner-cli.json')
    assert failure.value.code == 'endpoint_reader_unavailable'


def test_windows_capability_dispatches_native_reader_without_getuid(tmp_path, monkeypatch):
    expected = {**ENDPOINT, 'base_url': ENDPOINT['api_url']}
    calls = []
    monkeypatch.setattr(owner_cli.sys, 'platform', 'win32')
    monkeypatch.setattr(owner_cli.os, 'open', lambda *_a, **_k: pytest.fail('never open unverified Windows endpoint'))
    monkeypatch.setattr(owner_cli, '_windows_capability', lambda path: calls.append(path) or expected)
    path = tmp_path / 'owner-cli.json'
    assert owner_cli.capability(path) == expected and calls == [path]


@pytest.mark.parametrize('problem', ['relative_endpoint', 'oversize', 'bad_utf16', 'relative_dll'])
def test_windows_invalid_path_size_or_fixture_dll_is_rejected_before_loading(tmp_path, monkeypatch, problem):
    reader = StubReader()
    dll, loads = fixture_dll(tmp_path, monkeypatch, reader)
    path = tmp_path / 'owner-cli.json'
    if problem == 'relative_endpoint':
        path = Path('owner-cli.json')
    elif problem == 'oversize':
        path = Path('/' + 'a' * 32768) / 'owner-cli.json'
    elif problem == 'bad_utf16':
        path = tmp_path / '\ud800' / 'owner-cli.json'
    else:
        dll = Path('gamja_core_host.dll')
    with pytest.raises(owner_cli.EndpointReadFailure):
        owner_cli._windows_capability(path, _fixture_dll=dll)
    assert not reader.calls and not loads


def test_windows_reader_counts_utf16_units_for_non_bmp_paths(tmp_path, monkeypatch):
    reader = StubReader()
    dll, _ = fixture_dll(tmp_path, monkeypatch, reader)
    path = tmp_path / 'fixture-\U0001f955' / 'owner-cli.json'
    assert owner_cli._windows_capability(path, _fixture_dll=dll)['capability'] == ENDPOINT['capability']
    assert reader.directory == str(path.parent)
    assert reader.calls[0][1] == len(str(path.parent).encode('utf-16-le')) // 2


def test_windows_reader_ffi_exception_wipes_buffers_and_hides_native_detail(tmp_path, monkeypatch, capsys):
    class FailingReader(StubReader):
        def __call__(self, *args):
            super().__call__(*args)
            raise OSError('sentinel-secret raw native detail')
    reader = FailingReader(payload=b'sentinel-secret')
    dll, _ = fixture_dll(tmp_path, monkeypatch, reader)
    with pytest.raises(owner_cli.EndpointReadFailure) as failure:
        owner_cli._windows_capability(tmp_path / 'owner-cli.json', _fixture_dll=dll)
    assert failure.value.code == 'endpoint_reader_unavailable'
    assert 'sentinel-secret' not in str(failure.value)
    assert bytes(reader.calls[0][2]) == b'\0' * 4097
    output = capsys.readouterr()
    assert not output.out and not output.err
