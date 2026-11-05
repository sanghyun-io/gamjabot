import base64
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import threading
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest

from app import owner_cli


def token(expiry, owner=1):
    payload = base64.urlsafe_b64encode(json.dumps({'sub': str(owner), 'exp': expiry}).encode()).decode().rstrip('=')
    return 'synthetic.' + payload + '.signature'


@pytest.fixture
def transport(monkeypatch):
    clock = {'wall': 1000.0, 'mono': 0.0}
    monkeypatch.setattr(owner_cli.time, 'time', lambda: clock['wall'])
    monkeypatch.setattr(owner_cli.time, 'monotonic', lambda: clock['mono'])
    calls = []
    handlers = {}

    class Response(BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()

    class Opener:
        def open(self, request, timeout):
            path = urlsplit(request.full_url).path.removeprefix('/api/v1')
            entry = {'path': path, 'method': request.method, 'timeout': timeout,
                     'body': json.loads(request.data) if request.data else None,
                     'authorization': request.get_header('Authorization')}
            calls.append(entry)
            result = handlers.get(path, lambda entry: {'ok': True})(entry)
            return Response(json.dumps(result).encode())

    monkeypatch.setattr(owner_cli, 'build_opener', lambda *args: Opener())
    client = owner_cli.Client({'base_url': 'http://127.0.0.1:8100', 'capability': 'synthetic-local-only'})
    client.token = token(999)
    handlers['/auth/restore'] = lambda entry: {'access_token': token(3000)}
    handlers['/auth/me'] = lambda entry: {'id': 1}
    return client, calls, handlers, clock


def denied(entry):
    raise HTTPError('http://127.0.0.1:8100', 401, 'Unauthorized', {}, BytesIO(b'{}'))


@pytest.mark.parametrize('expiry', [999, 1040])
def test_renewal_precedes_one_original_mutation(transport, expiry):
    client, calls, handlers, clock = transport
    client.token = token(expiry)
    assert client.call('POST', '/conversations/1/messages', {'content': 'one'}, authenticated=True) == {'ok': True}
    assert [call['path'] for call in calls] == ['/auth/restore', '/auth/me', '/conversations/1/messages']
    assert calls[0]['body'] == {'expected_owner_id': 1} and calls[0]['authorization'] is None
    assert calls[1]['authorization'] == calls[2]['authorization'] == 'Bearer ' + token(3000)


def test_expired_failed_renewal_never_sends_business_request(transport):
    client, calls, handlers, clock = transport
    handlers['/auth/restore'] = denied
    with pytest.raises(owner_cli.RestoreHttpFailure):
        client.call('POST', '/conversations/1/messages', {}, authenticated=True)
    assert [call['path'] for call in calls] == ['/auth/restore']


def test_still_valid_nonremembered_session_has_bounded_cooldown(transport):
    client, calls, handlers, clock = transport
    client.token = token(1040)
    handlers['/auth/restore'] = denied
    client.call('GET', '/dashboard', authenticated=True)
    client.call('GET', '/dashboard', authenticated=True)
    assert [call['path'] for call in calls] == ['/auth/restore', '/dashboard', '/dashboard']
    clock['mono'] = 31
    clock['wall'] = 1031
    client.call('GET', '/dashboard', authenticated=True)
    assert [call['path'] for call in calls][-2:] == ['/auth/restore', '/dashboard']
    clock['wall'] = 1041
    with pytest.raises(RuntimeError):
        client.call('POST', '/conversations/1/messages', {}, authenticated=True)
    assert all(call['path'] != '/conversations/1/messages' for call in calls)


def test_foreign_verified_profile_is_not_adopted(transport):
    client, calls, handlers, clock = transport
    old = client.token
    handlers['/auth/me'] = lambda entry: {'id': 2}
    with pytest.raises(RuntimeError, match='Saved login is unavailable'):
        client.call('POST', '/conversations/1/messages', {}, authenticated=True)
    assert client.token == old
    assert [call['path'] for call in calls] == ['/auth/restore', '/auth/me']


def test_owner_change_while_restoring_never_overwrites_or_dispatches(transport):
    client, calls, handlers, clock = transport
    def changed(entry):
        client.token = token(3000, 2)
        return {'access_token': token(3000)}
    handlers['/auth/restore'] = changed
    with pytest.raises(RuntimeError, match='session changed'):
        client.call('POST', '/conversations/1/messages', {}, authenticated=True)
    assert client.token == token(3000, 2)
    assert [call['path'] for call in calls] == ['/auth/restore']


def test_renewal_consumes_original_deadline(transport):
    client, calls, handlers, clock = transport
    def slow(entry):
        clock['mono'] += 6
        return {'access_token': token(3000)}
    handlers['/auth/restore'] = slow
    with pytest.raises(RuntimeError, match='unavailable'):
        client.call('POST', '/conversations/1/messages', {}, authenticated=True, request_timeout=5)
    assert [call['path'] for call in calls] == ['/auth/restore']
    assert calls[0]['timeout'] <= 5


def test_business_401_is_not_replayed(transport):
    client, calls, handlers, clock = transport
    client.token = token(3000)
    handlers['/conversations/1/messages'] = denied
    with pytest.raises(RuntimeError, match='Request failed'):
        client.call('POST', '/conversations/1/messages', {}, authenticated=True)
    assert [call['path'] for call in calls] == ['/conversations/1/messages']


def test_concurrent_calls_share_same_owner_renewal(transport):
    client, calls, handlers, clock = transport
    started, release = threading.Event(), threading.Event()
    def waiting(entry):
        started.set()
        assert release.wait(5)
        return {'access_token': token(3000)}
    handlers['/auth/restore'] = waiting
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.call, 'POST', '/conversations/1/messages', {}, authenticated=True)
        assert started.wait(5)
        second = pool.submit(client.call, 'POST', '/conversations/2/messages', {}, authenticated=True)
        release.set()
        assert first.result(timeout=5) == second.result(timeout=5) == {'ok': True}
    assert sum(call['path'] == '/auth/restore' for call in calls) == 1
    assert sum(call['path'] == '/auth/me' for call in calls) == 1
    assert sum(call['path'].endswith('/messages') for call in calls) == 2
